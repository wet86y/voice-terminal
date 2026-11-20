//! SystemMaintenance（系统维护宿主）：本次运行按需提权一次，只接收正式登记的固定管理操作。
use serde::{Deserialize, Serialize};

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "operation", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum Operation {
    PrepareOpenSsh {}, // PrepareOpenSsh（安装或启动经核验的官方服务）。
    ProjectAccess {
        enabled: bool,   // enabled（项目规则目标启用状态）。
        ssh: bool,       // ssh（维护 SSH 规则）。
        pairing: bool,   // pairing（维护配对规则）。
        discovery: bool, // discovery（维护设备发现规则）。
    },
    #[cfg(feature = "ssh-device")]
    DeviceKey {
        add: bool,                   // add（登记或撤销受限公钥）。
        public_key: String,          // public_key（已经配对验证的设备公钥）。
        workspace_id: String,        // workspace_id（工作区稳定编号）。
        device_id: String,           // device_id（受管设备编号）。
        build: String,               // build（请求所属正式构建身份，不写入长期授权）。
        owner_sid: String,           // owner_sid（原交互账户身份）。
        network_scopes: Vec<String>, // network_scopes（本轮实际局域网子网）。
    },
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    id: u64,                        // id（串行维护请求编号）。
    powershell: std::path::PathBuf, // powershell（原用户选定的解释器，提权后重新核验签名和能力）。
    action: Operation,              // action（固定操作及有界参数，不接收脚本或任意目标路径）。
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Response {
    id: u64,                     // id（对应请求编号）。
    result: Result<u32, String>, // result（任务退出码或明确错误）。
}

#[cfg(windows)]
mod native {
    use super::*;
    use std::{
        ffi::c_void,
        io,
        mem::size_of,
        os::windows::{ffi::OsStrExt, io::AsRawHandle},
        ptr,
        sync::Mutex,
        time::{Duration, Instant},
    };
    use tokio::{
        io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt},
        net::windows::named_pipe::{ClientOptions, NamedPipeServer, ServerOptions},
    };
    use windows_sys::Win32::{
        Foundation::{CloseHandle, GetLastError, LocalFree, ERROR_CANCELLED},
        Security::{
            Authorization::ConvertStringSecurityDescriptorToSecurityDescriptorW,
            GetTokenInformation, TokenElevation, SECURITY_ATTRIBUTES, TOKEN_ELEVATION, TOKEN_QUERY,
        },
        System::{
            Pipes::{GetNamedPipeClientProcessId, GetNamedPipeServerProcessId},
            Threading::{GetCurrentProcess, GetProcessId, OpenProcessToken},
        },
        UI::{
            Shell::{
                ShellExecuteExW, SEE_MASK_NOASYNC, SEE_MASK_NOCLOSEPROCESS, SHELLEXECUTEINFOW,
            },
            WindowsAndMessaging::SW_HIDE,
        },
    };

    const ARGUMENT: &str = "--system-maintenance-host"; // ARGUMENT（产品内部维护入口）。
    const MAX_FRAME: usize = 64 * 1024; // MAX_FRAME（维护消息最大字节数）。
    static SESSION: Mutex<Option<Session>> = Mutex::new(None);

    pub fn elevated() -> Result<bool, String> {
        unsafe {
            let mut token = ptr::null_mut();
            if OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) == 0 {
                return Err("无法读取进程管理权限。".into());
            }
            let mut value = TOKEN_ELEVATION { TokenIsElevated: 0 };
            let mut length = 0;
            let ok = GetTokenInformation(
                token,
                TokenElevation,
                (&mut value as *mut TOKEN_ELEVATION).cast(),
                size_of::<TOKEN_ELEVATION>() as u32,
                &mut length,
            );
            CloseHandle(token);
            if ok == 0 {
                return Err("无法确认进程管理权限。".into());
            }
            Ok(value.TokenIsElevated != 0)
        }
    }

    struct Session {
        runtime: tokio::runtime::Runtime, // runtime（专用管道运行时，不嵌套桌面异步运行时）。
        pipe: NamedPipeServer,            // pipe（仅本机且绑定双方进程身份的会话）。
        process: usize,                   // process（授权子进程句柄，存活期间防止进程编号复用）。
        sequence: u64,                    // sequence（单调递增的请求编号）。
        failed: bool,                     // failed（管道失败后不自动重复提权或重放管理任务）。
    }
    impl Drop for Session {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.process as _);
            }
        }
    }

    fn wide(text: &std::ffi::OsStr) -> Vec<u16> {
        text.encode_wide().chain(Some(0)).collect()
    }

    fn create_pipe(name: &str) -> Result<NamedPipeServer, String> {
        let sid = crate::system_feature::current_user_sid()?;
        let descriptor = wide(std::ffi::OsStr::new(&format!(
            "D:P(A;;GA;;;{sid})(A;;GA;;;BA)(A;;GA;;;SY)"
        )));
        unsafe {
            let mut security = ptr::null_mut();
            if ConvertStringSecurityDescriptorToSecurityDescriptorW(
                descriptor.as_ptr(),
                1,
                &mut security,
                ptr::null_mut(),
            ) == 0
            {
                return Err("无法建立维护管道访问控制。".into());
            }
            let mut attributes = SECURITY_ATTRIBUTES {
                nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
                lpSecurityDescriptor: security,
                bInheritHandle: 0,
            };
            let result = ServerOptions::new()
                .first_pipe_instance(true)
                .reject_remote_clients(true)
                .create_with_security_attributes_raw(
                    name,
                    (&mut attributes as *mut SECURITY_ATTRIBUTES).cast::<c_void>(),
                );
            LocalFree(security);
            result.map_err(|error| format!("无法创建系统维护管道：{error}"))
        }
    }

    fn verify_client(pipe: &NamedPipeServer, expected: u32) -> Result<(), String> {
        let mut peer = 0;
        if unsafe { GetNamedPipeClientProcessId(pipe.as_raw_handle().cast(), &mut peer) } == 0
            || peer != expected
        {
            return Err("维护管道连接者不是本轮授权的产品进程。".into());
        }
        Ok(())
    }

    impl Session {
        fn start() -> Result<Self, String> {
            let executable = std::env::current_exe().map_err(|error| error.to_string())?;
            if executable.file_name().and_then(|value| value.to_str())
                != Some("VoiceTerminalDesktop.exe")
            {
                return Err("系统维护只能由正式桌面程序发起。".into());
            }
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .map_err(|error| error.to_string())?;
            let name = format!(
                r"\\.\pipe\VoiceTerminal-Maintenance-{}",
                crate::workspace::new_id()
            );
            let pipe = {
                let _enter = runtime.enter();
                create_pipe(&name)?
            };
            let verb = wide(std::ffi::OsStr::new("runas"));
            let file = wide(executable.as_os_str());
            let parameters = wide(std::ffi::OsStr::new(&format!(
                "{ARGUMENT} {} {name}",
                std::process::id()
            )));
            let mut info = SHELLEXECUTEINFOW {
                cbSize: size_of::<SHELLEXECUTEINFOW>() as u32,
                fMask: SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC,
                lpVerb: verb.as_ptr(),
                lpFile: file.as_ptr(),
                lpParameters: parameters.as_ptr(),
                nShow: SW_HIDE,
                ..Default::default()
            };
            if unsafe { ShellExecuteExW(&mut info) } == 0 {
                return Err(if unsafe { GetLastError() } == ERROR_CANCELLED {
                    "已取消 Windows 管理员授权；系统维护未执行。".into()
                } else {
                    format!("无法启动系统维护宿主：{}", io::Error::last_os_error())
                });
            }
            if info.hProcess.is_null() {
                return Err("系统未返回维护宿主进程身份。".into());
            }
            let session = Self {
                runtime,
                pipe,
                process: info.hProcess as usize,
                sequence: 0,
                failed: false,
            };
            session.runtime.block_on(async {
                tokio::time::timeout(Duration::from_secs(30), session.pipe.connect())
                    .await
                    .map_err(|_| "等待维护宿主超时。")?
                    .map_err(|error| error.to_string())?;
                verify_client(&session.pipe, unsafe { GetProcessId(info.hProcess) })?;
                Ok::<_, String>(())
            })?;
            Ok(session)
        }

        fn call(
            &mut self,
            action: Operation,
            powershell: std::path::PathBuf,
            on_wait: &mut dyn FnMut(Duration),
        ) -> Result<u32, String> {
            if self.failed {
                return Err(
                    "本次系统维护宿主已失效；未重新申请授权或重复执行，请重启程序后重试。".into(),
                );
            }
            self.sequence += 1;
            let request = Request {
                id: self.sequence,
                powershell,
                action,
            };
            let result = self.runtime.block_on(async {
                let started = Instant::now();
                let exchange = async {
                    write_frame(&mut self.pipe, &request).await?;
                    let response: Response = read_frame(&mut self.pipe).await?;
                    if response.id != request.id { return Err("系统维护回执编号不匹配。".into()); }
                    Ok(response.result)
                };
                tokio::pin!(exchange);
                loop {
                    tokio::select! {
                        result = &mut exchange => break result,
                        _ = tokio::time::sleep(Duration::from_secs(15)) => {
                            on_wait(started.elapsed());
                            if started.elapsed() >= Duration::from_secs(660) { break Err("系统维护回执超时，结果未知；未自动重放操作。".into()); }
                        }
                    }
                }
            });
            match result {
                Ok(result) => result,
                Err(error) => {
                    self.failed = true;
                    Err(error)
                }
            }
        }
    }

    pub fn execute(action: Operation, on_wait: &mut dyn FnMut(Duration)) -> Result<u32, String> {
        let powershell =
            crate::powershell::select(crate::powershell::Purpose::Maintenance)?.executable;
        let mut session = SESSION.lock().map_err(|_| "系统维护会话状态不可用。")?;
        if session.is_none() {
            *session = Some(Session::start()?);
        }
        session.as_mut().unwrap().call(action, powershell, on_wait)
    }

    async fn write_frame<W: AsyncWrite + Unpin, T: Serialize>(
        stream: &mut W,
        value: &T,
    ) -> Result<(), String> {
        let bytes = serde_json::to_vec(value).map_err(|_| "系统维护消息编码失败。")?;
        if bytes.is_empty() || bytes.len() > MAX_FRAME {
            return Err("系统维护消息超过大小限制。".into());
        }
        stream
            .write_u32(bytes.len() as u32)
            .await
            .map_err(|error| error.to_string())?;
        stream
            .write_all(&bytes)
            .await
            .map_err(|error| error.to_string())?;
        stream.flush().await.map_err(|error| error.to_string())
    }
    async fn read_frame<R: AsyncRead + Unpin, T: for<'a> Deserialize<'a>>(
        stream: &mut R,
    ) -> Result<T, String> {
        let length = stream.read_u32().await.map_err(|error| error.to_string())? as usize;
        if length == 0 || length > MAX_FRAME {
            return Err("系统维护消息长度无效。".into());
        }
        let mut bytes = vec![0; length];
        stream
            .read_exact(&mut bytes)
            .await
            .map_err(|error| error.to_string())?;
        serde_json::from_slice(&bytes).map_err(|_| "系统维护操作未登记或参数格式无效。".into())
    }

    pub fn dispatch() -> Option<Result<(), String>> {
        let arguments = std::env::args().skip(1).collect::<Vec<_>>();
        if arguments.first().map(String::as_str) != Some(ARGUMENT) {
            return None;
        }
        Some(run(&arguments))
    }

    fn run(arguments: &[String]) -> Result<(), String> {
        if arguments.len() != 3
            || !arguments[2].starts_with(r"\\.\pipe\VoiceTerminal-Maintenance-")
            || arguments[2].len() > 160
            || !arguments[2][r"\\.\pipe\VoiceTerminal-Maintenance-".len()..]
                .bytes()
                .all(|value| value.is_ascii_hexdigit() || value == b'-')
        {
            return Err("系统维护启动参数无效。".into());
        }
        let parent: u32 = arguments[1]
            .parse()
            .map_err(|_| "系统维护父进程编号无效。")?;
        if !elevated()? {
            return Err("系统维护宿主尚未获得管理员授权。".into());
        }
        let system_root = crate::powershell::windows_root()?;
        std::env::set_var("SystemRoot", &system_root);
        std::env::set_var(
            "ProgramData",
            crate::powershell::known_folder(&windows_sys::Win32::UI::Shell::FOLDERID_ProgramData)?,
        );
        std::env::set_var(
            "PATH",
            format!(
                "{};{}",
                system_root.join("System32").display(),
                system_root.display()
            ),
        );
        std::env::set_current_dir(system_root.join("System32"))
            .map_err(|error| error.to_string())?;
        let executable =
            crate::canonical_path(&std::env::current_exe().map_err(|error| error.to_string())?)?;
        let (_, parent_path) =
            crate::workspace::process_identity(parent)?.ok_or("发起系统维护的程序已退出。")?;
        if crate::canonical_path(&parent_path)? != executable {
            return Err("系统维护父进程不是同一产品程序。".into());
        }
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|error| error.to_string())?;
        runtime.block_on(async {
            let deadline = Instant::now() + Duration::from_secs(30);
            let mut pipe = loop {
                match ClientOptions::new().open(&arguments[2]) {
                    Ok(pipe) => break pipe,
                    Err(_) if Instant::now() < deadline => {
                        tokio::time::sleep(Duration::from_millis(50)).await
                    }
                    Err(error) => return Err(format!("系统维护管道连接失败：{error}")),
                }
            };
            let mut server = 0;
            if unsafe { GetNamedPipeServerProcessId(pipe.as_raw_handle().cast(), &mut server) } == 0
                || server != parent
            {
                return Err("系统维护管道不属于原发起进程。".into());
            }
            let mut previous = 0;
            let mut selected_host = None;
            loop {
                let request: Request = match read_frame(&mut pipe).await {
                    Ok(value) => value,
                    Err(_) => break,
                };
                if request.id != previous + 1 {
                    return Err("系统维护请求重复或顺序无效。".into());
                }
                previous = request.id;
                if let Some(path) = selected_host.as_ref() {
                    if path != &request.powershell {
                        return Err(
                            "本次维护宿主不在管理任务之间切换解释器，请重启程序后应用新选择。"
                                .into(),
                        );
                    }
                } else {
                    let host = crate::powershell::probe(
                        &request.powershell,
                        crate::powershell::Purpose::Maintenance,
                    )?;
                    std::env::set_var("VOICE_POWERSHELL_PATH", &host.executable);
                    selected_host = Some(request.powershell.clone());
                }
                let result = crate::system_feature::execute_maintenance_operation(request.action);
                if write_frame(
                    &mut pipe,
                    &Response {
                        id: request.id,
                        result,
                    },
                )
                .await
                .is_err()
                {
                    break;
                }
            }
            Ok(())
        })
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[tokio::test]
        async fn local_pipe_verifies_actual_peer_and_preserves_request_boundaries() {
            let name = format!(
                r"\\.\pipe\VoiceTerminal-Maintenance-{}",
                crate::workspace::new_id()
            );
            let server = create_pipe(&name).unwrap();
            let mut client = ClientOptions::new().open(&name).unwrap();
            server.connect().await.unwrap();
            verify_client(&server, std::process::id()).unwrap();
            assert!(verify_client(&server, u32::MAX).is_err());
            let mut server = server;
            write_frame(
                &mut client,
                &Request {
                    id: 1,
                    powershell: "C:/test/pwsh.exe".into(),
                    action: Operation::PrepareOpenSsh {},
                },
            )
            .await
            .unwrap();
            let first: Request = read_frame(&mut server).await.unwrap();
            assert_eq!(first.id, 1);
            write_frame(
                &mut server,
                &Response {
                    id: first.id,
                    result: Ok(0),
                },
            )
            .await
            .unwrap();
            let response: Response = read_frame(&mut client).await.unwrap();
            assert_eq!(response.id, 1);
            assert_eq!(response.result, Ok(0));
            client.write_u32((MAX_FRAME + 1) as u32).await.unwrap();
            let error = read_frame::<_, Request>(&mut server).await.err().unwrap();
            assert!(error.contains("长度无效"));
        }
    }
}

#[cfg(windows)]
pub use native::dispatch;
#[cfg(windows)]
pub(crate) use native::{elevated, execute};

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn maintenance_accepts_only_registered_actions_and_exact_fields() {
        assert!(serde_json::from_value::<Request>(serde_json::json!({"id":1,"powershell":"C:/test/pwsh.exe","action":{"operation":"prepare_open_ssh"}})).is_ok());
        for action in [
            serde_json::json!({"operation":"powershell","script":"exit"}),
            serde_json::json!({"operation":"prepare_open_ssh","script":"exit"}),
            serde_json::json!({"operation":"project_access","enabled":true,"ssh":true,"pairing":true,"discovery":true,"path":"C:/other.exe"}),
        ] {
            assert!(serde_json::from_value::<Request>(
                serde_json::json!({"id":1,"powershell":"C:/test/pwsh.exe","action":action})
            )
            .is_err());
        }
    }
}
