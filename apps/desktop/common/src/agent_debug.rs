//! 正式入口与宿主共用的代理调试授权和本机传输。
use crate::{installation, system_feature, workspace};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    ffi::c_void,
    fs, io,
    io::{BufRead, Read},
    os::windows::io::AsRawHandle,
    path::{Path, PathBuf},
    ptr,
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncBufRead, AsyncBufReadExt, AsyncReadExt, AsyncWrite, AsyncWriteExt, BufReader},
    net::windows::named_pipe::{ClientOptions, NamedPipeClient, NamedPipeServer, ServerOptions},
};
use windows_sys::Win32::{
    Foundation::LocalFree,
    Security::{
        Authorization::ConvertStringSecurityDescriptorToSecurityDescriptorW, SECURITY_ATTRIBUTES,
    },
    System::{
        Pipes::{
            GetNamedPipeClientProcessId, GetNamedPipeClientSessionId, GetNamedPipeServerProcessId,
        },
        RemoteDesktop::ProcessIdToSessionId,
    },
};

pub const MAX_FRAME: usize = 8 * 1024 * 1024; // MAX_FRAME（单条调试消息字节上限）。
const SETTING: &str = "config/agent-debug.json";

pub fn allowed(data: &Path) -> Result<bool, String> {
    let path = workspace::safe_path(data, SETTING)?;
    let value: Value = match fs::read(path) {
        Ok(bytes) => serde_json::from_slice(&bytes).map_err(|_| "外部调试设置格式无效。")?,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(true),
        Err(error) => return Err(error.to_string()),
    };
    let settings = value.as_object().ok_or("外部调试设置格式无效。")?;
    match settings.get("allow_external_debug") {
        None => Ok(true),
        Some(value) => value
            .as_bool()
            .ok_or_else(|| "外部调试设置格式无效。".into()),
    }
}

pub fn save_allowed(data: &Path, allowed: bool) -> Result<(), String> {
    workspace::atomic_json(
        &workspace::safe_path(data, SETTING)?,
        &json!({"allow_external_debug":allowed,"fields":{"allow_external_debug":"允许外部调试"}}),
    )
}

pub struct Endpoint {
    pub name: String, // name（只在本机使用的命名管道名称）。
    sid: String,
    desktop: PathBuf,
}

impl Endpoint {
    pub fn new(root: &Path) -> Result<Self, String> {
        let root = crate::canonical_path(root)?;
        let sid = system_feature::current_user_sid()?;
        let name = format!(
            r"\\.\pipe\VoiceTerminal-Agent-{:x}",
            Sha256::digest(format!("{sid}\n{}", root.to_string_lossy().to_lowercase()))
        );
        let desktop = workspace::safe_path(&root, "app/VoiceTerminalDesktop.exe")?;
        Ok(Self { name, sid, desktop })
    }

    pub fn server(&self, first: bool) -> Result<NamedPipeServer, String> {
        // 当前账户的 SID（安全标识符）覆盖其本机交互和 SSH 登录会话。
        let sddl: Vec<u16> = format!("D:P(A;;GA;;;{})", self.sid)
            .encode_utf16()
            .chain(Some(0))
            .collect();
        let mut descriptor = ptr::null_mut();
        let server = unsafe {
            if ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl.as_ptr(),
                1,
                &mut descriptor,
                ptr::null_mut(),
            ) == 0
            {
                return Err(io::Error::last_os_error().to_string());
            }
            let mut attributes = SECURITY_ATTRIBUTES {
                nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
                lpSecurityDescriptor: descriptor,
                bInheritHandle: 0,
            };
            let server = ServerOptions::new()
                .first_pipe_instance(first)
                .reject_remote_clients(true)
                .create_with_security_attributes_raw(
                    &self.name,
                    (&mut attributes as *mut SECURITY_ATTRIBUTES).cast::<c_void>(),
                );
            LocalFree(descriptor);
            server
        };
        server.map_err(|error| format!("无法建立本机代理调试通道：{error}"))
    }

    pub async fn connect(&self) -> Result<(NamedPipeClient, u32), String> {
        let deadline = Instant::now() + Duration::from_secs(2);
        let client = loop {
            match ClientOptions::new().open(&self.name) {
                Ok(client) => break client,
                Err(error)
                    if matches!(error.raw_os_error(), Some(231 | 232 | 233))
                        && Instant::now() < deadline =>
                {
                    tokio::time::sleep(Duration::from_millis(30)).await;
                }
                Err(error) => return Err(format!("本机代理调试通道不可用：{error}")),
            }
        };
        let mut pid = 0;
        if unsafe { GetNamedPipeServerProcessId(client.as_raw_handle().cast(), &mut pid) } == 0
            || !workspace::process_identity(pid)?.is_some_and(|(_, path)| {
                path.to_string_lossy()
                    .eq_ignore_ascii_case(&self.desktop.to_string_lossy())
            })
        {
            return Err("本机调试通道的正式宿主身份无法确认。".into());
        }
        Ok((client, pid))
    }
}

pub fn session_id() -> Result<u32, String> {
    process_session_id(std::process::id())
}

pub fn process_session_id(pid: u32) -> Result<u32, String> {
    let mut id = 0;
    if unsafe { ProcessIdToSessionId(pid, &mut id) } == 0 {
        return Err(io::Error::last_os_error().to_string());
    }
    Ok(id)
}

pub fn interactive_session() -> Result<u32, String> {
    use windows_sys::Win32::{
        Security::{Authorization::ConvertSidToStringSidW, LookupAccountNameW},
        System::RemoteDesktop::{
            WTSActive, WTSDomainName, WTSEnumerateSessionsW, WTSFreeMemory,
            WTSQuerySessionInformationW, WTSUserName, WTS_CURRENT_SERVER_HANDLE, WTS_INFO_CLASS,
            WTS_SESSION_INFOW,
        },
    };
    struct WtsMemory(*mut c_void);
    impl Drop for WtsMemory {
        fn drop(&mut self) {
            unsafe { WTSFreeMemory(self.0) }
        }
    }
    fn text(session: u32, field: WTS_INFO_CLASS) -> Option<String> {
        let mut buffer = ptr::null_mut();
        let mut bytes = 0;
        unsafe {
            if WTSQuerySessionInformationW(
                WTS_CURRENT_SERVER_HANDLE,
                session,
                field,
                &mut buffer,
                &mut bytes,
            ) == 0
            {
                return None;
            }
            let _memory = WtsMemory(buffer.cast());
            let wide = std::slice::from_raw_parts(buffer, bytes as usize / 2);
            Some(String::from_utf16_lossy(
                wide.split(|unit| *unit == 0).next()?,
            ))
        }
    }
    fn account_sid(account: &str) -> Option<String> {
        let account: Vec<u16> = account.encode_utf16().chain(Some(0)).collect();
        let mut sid_size = 0;
        let mut name_size = 0;
        let mut usage = 0;
        unsafe {
            LookupAccountNameW(
                ptr::null(),
                account.as_ptr(),
                ptr::null_mut(),
                &mut sid_size,
                ptr::null_mut(),
                &mut name_size,
                &mut usage,
            );
            if sid_size == 0 {
                return None;
            }
            let mut sid = vec![0u32; (sid_size as usize).div_ceil(4)];
            let mut domain = vec![0u16; name_size as usize];
            if LookupAccountNameW(
                ptr::null(),
                account.as_ptr(),
                sid.as_mut_ptr().cast(),
                &mut sid_size,
                domain.as_mut_ptr(),
                &mut name_size,
                &mut usage,
            ) == 0
            {
                return None;
            }
            let mut printable = ptr::null_mut();
            if ConvertSidToStringSidW(sid.as_mut_ptr().cast(), &mut printable) == 0 {
                return None;
            }
            let length = (0..).find(|length| *printable.add(*length) == 0)?;
            let result = String::from_utf16_lossy(std::slice::from_raw_parts(printable, length));
            LocalFree(printable.cast());
            Some(result)
        }
    }
    let owner = system_feature::current_user_sid()?;
    let mut sessions: *mut WTS_SESSION_INFOW = ptr::null_mut();
    let mut count = 0;
    unsafe {
        if WTSEnumerateSessionsW(WTS_CURRENT_SERVER_HANDLE, 0, 1, &mut sessions, &mut count) == 0 {
            return Err("无法枚举交互桌面会话。".into());
        }
    }
    let _memory = WtsMemory(sessions.cast());
    let mut matches = Vec::new();
    if count > 0 {
        for session in unsafe { std::slice::from_raw_parts(sessions, count as usize) } {
            if session.SessionId == 0 || session.State != WTSActive {
                continue;
            }
            let Some(user) = text(session.SessionId, WTSUserName).filter(|name| !name.is_empty())
            else {
                continue;
            };
            let domain = text(session.SessionId, WTSDomainName).unwrap_or_default();
            let account = if domain.is_empty() {
                user
            } else {
                format!("{domain}\\{user}")
            };
            if account_sid(&account).as_deref() == Some(&owner) {
                matches.push(session.SessionId);
            }
        }
    }
    match matches.as_slice() {
        [session] => Ok(*session),
        [] => Err("当前账户没有活动的交互桌面，请先登录目标电脑桌面。".into()),
        _ => Err("当前账户有多个活动桌面，无法确定调试窗口应在哪个会话启动。".into()),
    }
}

pub fn peer(pipe: &NamedPipeServer) -> io::Result<(u32, u32)> {
    let mut pid = 0;
    let mut session = 0;
    let pipe = pipe.as_raw_handle().cast();
    if unsafe {
        GetNamedPipeClientProcessId(pipe, &mut pid) == 0
            || GetNamedPipeClientSessionId(pipe, &mut session) == 0
    } {
        return Err(io::Error::last_os_error());
    }
    Ok((pid, session))
}

pub async fn read_frame<R: AsyncBufRead + Unpin>(reader: &mut R) -> io::Result<Option<Vec<u8>>> {
    let mut bytes = Vec::new();
    read_frame_into(reader, &mut bytes).await
}

pub async fn read_frame_into<R: AsyncBufRead + Unpin>(
    reader: &mut R,
    bytes: &mut Vec<u8>,
) -> io::Result<Option<Vec<u8>>> {
    let allowance = (MAX_FRAME + 1).saturating_sub(bytes.len());
    reader
        .take(allowance as u64)
        .read_until(b'\n', bytes)
        .await?;
    if bytes.is_empty() {
        return Ok(None);
    }
    if bytes.len() > MAX_FRAME {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "调试消息过长。"));
    }
    Ok(Some(std::mem::take(bytes)))
}

pub async fn write_frame<W: AsyncWrite + Unpin>(writer: &mut W, value: &Value) -> io::Result<()> {
    let mut bytes = serde_json::to_vec(value)?;
    if bytes.len() >= MAX_FRAME {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "调试消息过长。"));
    }
    bytes.push(b'\n');
    writer.write_all(&bytes).await?;
    writer.flush().await
}

pub fn relay(root: &Path) -> Result<(), String> {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|error| error.to_string())?
        .block_on(async {
            let (pipe, _) = Endpoint::new(root)?.connect().await?;
            let (read, mut write) = tokio::io::split(pipe);
            let (forward, mut input) = tokio::sync::mpsc::channel(16);
            // 控制端撤销会话时，退出不等待另一个进程关闭它的标准输入。
            std::thread::spawn(move || {
                let input = io::stdin();
                let mut input = input.lock();
                loop {
                    let mut line = Vec::new();
                    let read = (&mut input)
                        .take((MAX_FRAME + 1) as u64)
                        .read_until(b'\n', &mut line);
                    let frame = match read {
                        Ok(0) => break,
                        Ok(_) if line.len() <= MAX_FRAME => Ok(line),
                        Ok(_) => Err(io::Error::new(io::ErrorKind::InvalidData, "调试消息过长。")),
                        Err(error) => Err(error),
                    };
                    let valid = frame.is_ok();
                    if forward.blocking_send(frame).is_err() || !valid {
                        break;
                    }
                }
            });
            let mut output = tokio::io::stdout();
            let mut reader = BufReader::new(read);
            let send = async {
                while let Some(line) = input.recv().await {
                    let mut line = line?;
                    if !line.ends_with(b"\n") {
                        line.push(b'\n');
                    }
                    write.write_all(&line).await?;
                }
                write_frame(
                    &mut write,
                    &json!({"jsonrpc":"2.0","method":"transport.finish"}),
                )
                .await
            };
            let receive = async {
                loop {
                    match read_frame(&mut reader).await {
                        Ok(Some(line)) => {
                            if serde_json::from_slice::<Value>(&line)
                                .is_ok_and(|value| value["method"] == "transport.finished")
                            {
                                break;
                            }
                            output.write_all(&line).await?;
                            output.flush().await?;
                        }
                        Ok(None) => break,
                        Err(error) if matches!(error.raw_os_error(), Some(109 | 232 | 233)) => {
                            break
                        }
                        Err(error) => return Err(error),
                    }
                }
                Ok::<_, io::Error>(())
            };
            tokio::pin!(send, receive);
            tokio::select! {
                result = &mut receive => result,
                result = &mut send => match result {
                    Ok(()) => receive.await,
                    Err(error) => Err(error),
                },
            }
            .map_err(|error| error.to_string())
        })
}

pub fn request_exit(root: &Path) -> Result<(), String> {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|error| error.to_string())?;
    let identity = runtime.block_on(async {
        let (mut pipe, pid) = match Endpoint::new(root)?.connect().await {
            Ok(connection) => connection,
            Err(error) => {
                if installation::ensure_idle(root).is_ok()
                    && installation::WorkspaceGuard::acquire(root).is_ok()
                {
                    return Ok::<_, String>(None);
                }
                return Err(error);
            }
        };
        let identity = workspace::process_identity(pid)?;
        write_frame(
            &mut pipe,
            &json!({"jsonrpc":"2.0","id":1,"method":"instance.exit"}),
        )
        .await
        .map_err(|error| error.to_string())?;
        let mut reader = BufReader::new(&mut pipe);
        let line = tokio::time::timeout(Duration::from_secs(5), read_frame(&mut reader))
            .await
            .map_err(|_| "等待正常退出请求确认超时。")?
            .map_err(|error| error.to_string())?
            .ok_or("正常退出请求没有收到回执。")?;
        let reply: Value = serde_json::from_slice(&line).map_err(|error| error.to_string())?;
        if reply["id"] != 1 || reply["result"]["accepted"] != true {
            return Err(reply["error"]["message"]
                .as_str()
                .unwrap_or("正常退出请求未获确认。")
                .into());
        }
        Ok(identity.map(|identity| (pid, identity)))
    })?;
    let deadline = Instant::now() + Duration::from_secs(45);
    loop {
        let exited = match &identity {
            Some((pid, expected)) => workspace::process_identity(*pid)?.as_ref() != Some(expected),
            None => true,
        };
        if exited && installation::WorkspaceGuard::acquire(root).is_ok() {
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err("等待正式进程退出及工作区释放超时。".into());
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn agent_debug_preserves_explicit_denial_and_defaults_old_settings() {
        let root = std::env::temp_dir().join(format!("voice-agent-{}", workspace::new_id()));
        fs::create_dir(&root).unwrap();
        assert!(allowed(&root).unwrap());
        save_allowed(&root, false).unwrap();
        assert!(!allowed(&root).unwrap());
        fs::write(root.join(SETTING), b"{}").unwrap();
        assert!(allowed(&root).unwrap());
        fs::write(root.join(SETTING), br#"{"allow_external_debug":0}"#).unwrap();
        assert!(allowed(&root).is_err());
        fs::remove_file(root.join(SETTING)).unwrap();
        fs::remove_dir(root.join("config")).unwrap();
        fs::remove_dir(root).unwrap();
    }

    #[tokio::test]
    async fn agent_debug_keeps_frame_prefix_across_event_selection() {
        let (stream, mut sender) = tokio::io::duplex(128);
        let mut reader = BufReader::new(stream);
        let mut bytes = Vec::new();
        sender.write_all(b"{\"id\":").await.unwrap();
        let pending = tokio::time::timeout(
            Duration::from_millis(25),
            read_frame_into(&mut reader, &mut bytes),
        )
        .await;
        assert!(pending.is_err());
        sender.write_all(b"7}\n").await.unwrap();
        assert_eq!(
            read_frame_into(&mut reader, &mut bytes)
                .await
                .unwrap()
                .unwrap(),
            b"{\"id\":7}\n"
        );
    }
}
