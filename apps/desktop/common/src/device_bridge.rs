//! DeviceBridge（设备桥接）：SSH 强制命令早期入口，仅把有界会话转给当前受管端点。
use crate::{
    device_config::{self, LoadResult, RunMode},
    device_management_contract::DeviceBridgePrelude,
    device_trust::DeviceTrustStore,
    service_endpoints::{self, BridgeChannel, ServiceEndpoint},
    LaunchContext,
};
use std::{
    io::{self, Read, Write},
    net::{IpAddr, TcpStream},
};

const CONTROL_MARKER: &str = "voice-terminal-v1-control"; // CONTROL_MARKER（固定控制通道标记）。
const MEDIA_MARKER: &str = "voice-terminal-v1-media"; // MEDIA_MARKER（固定媒体通道标记）。
const MANAGEMENT_MARKER: &str = "voice-terminal-v1-management"; // MANAGEMENT_MARKER（固定独立管理通道标记）。
const FILE_MARKER: &str = "voice-terminal-v1-file"; // FILE_MARKER（固定文件通道标记）。

#[derive(Debug)]
struct BridgeArguments {
    workspace_id: String, // workspace_id（被项目密钥绑定的工作区编号）。
    owner_sid: String,    // owner_sid（授权密钥登记的 Windows 登录账户 SID）。
    device_id: String,    // device_id（已经通过配对登记的客户端编号）。
    protocol: u32,        // protocol（长期授权桥接协议版本）。
}

/// dispatch（SSH 早期分派）：命中专用入口时在 Tauri、网页和 Python 启动前处理。
pub fn dispatch() -> Option<Result<(), String>> {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    if args.first().map(String::as_str) != Some("--ssh-device-bridge") {
        return None;
    }
    Some(run(&args))
}

fn run(args: &[String]) -> Result<(), String> {
    let arguments = parse_arguments(args)?;
    let executable =
        std::env::current_exe().map_err(|error| format!("无法定位 SSH 桥接程序：{error}"))?;
    let metadata = std::fs::symlink_metadata(&executable)
        .map_err(|error| format!("无法读取 SSH 桥接程序：{error}"))?;
    if crate::workspace::metadata_reparse(&metadata) || !metadata.is_file() {
        return Err("SSH 桥接程序路径不是受管普通文件。".into());
    }
    let context = LaunchContext::from_executable(&executable, &[])?;
    let expected_executable = context.program.join("VoiceTerminalDesktop.exe");
    if crate::canonical_path(&expected_executable)? != crate::canonical_path(&executable)? {
        return Err("SSH 桥接只允许从正式 VoiceTerminalDesktop.exe 运行。".into());
    }
    if context.workspace_id != arguments.workspace_id {
        return Err("SSH 密钥绑定的工作区身份与当前安装不匹配。".into());
    }
    if crate::system_feature::current_user_sid()? != arguments.owner_sid {
        return Err("当前 SSH 登录账户 SID 与项目密钥登记账户不匹配。".into());
    }
    let release = crate::resources::load(&context.program.join("release.json"))?;
    crate::compatibility::require_device_bridge_protocol(arguments.protocol, &release)?;
    let executable_records = release["files"]
        .as_array()
        .ok_or("正式程序文件清单不可用。")?
        .iter()
        .filter(|record| record["path"] == "VoiceTerminalDesktop.exe")
        .cloned()
        .collect::<Vec<_>>();
    if executable_records.len() != 1 {
        return Err("正式程序清单缺少唯一桌面入口。".into());
    }
    crate::workspace::verify_fixed_files_with_progress(
        &context.program,
        &executable_records,
        &std::sync::atomic::AtomicBool::new(false),
        &mut |_, _, _| {},
    )?;
    let current_build = release["build"]
        .as_str()
        .filter(|value| valid_ascii_token(value, 128))
        .ok_or("当前正式程序缺少有效构建身份。")?
        .to_owned();
    match device_config::load_from_root(&context.data) {
        LoadResult::Valid(config) if config.mode == RunMode::Local && config.broadcast_enabled => {}
        _ => return Err("本机不处于已保存的本地广播模式；SSH 桥接拒绝网络会话。".into()),
    }
    let trust = DeviceTrustStore::open(&context.data)?;
    if trust.snapshot().state != "valid" || trust.trusted_public_key(&arguments.device_id).is_none()
    {
        return Err("客户端设备未获本机信任；SSH 桥接拒绝会话。".into());
    }

    let marker = std::env::var("SSH_ORIGINAL_COMMAND")
        .map_err(|_| "OpenSSH 未提供原始会话标记；交互式 shell 被拒绝。".to_string())?;
    let (channel, send_prelude) = bridge_target(&marker)?;
    verify_ssh_connection_scope(&marker)?;
    let endpoint = with_queryable_identity(&arguments.owner_sid, || {
        service_endpoints::query(&context.install, &expected_executable, channel)
    })?;
    let prelude = if send_prelude {
        let prelude = DeviceBridgePrelude {
            message_type: "bridge.prelude".into(),
            protocol_version: 1,
            workspace_id: arguments.workspace_id,
            owner_sid: arguments.owner_sid,
            client_device_id: arguments.device_id,
            // 前导帧仍报告当前运行构建，后续本机服务继续核对真实发行身份。
            build: current_build,
            fields: std::collections::BTreeMap::from([
                ("type".into(), "正式 SSH 强制命令桥接类型".into()),
                ("protocol_version".into(), "桥接前导协议版本".into()),
                ("workspace_id".into(), "本机验证的正式工作区编号".into()),
                ("owner_sid".into(), "项目密钥所属 Windows 登录 SID".into()),
                (
                    "client_device_id".into(),
                    "SSH 强制命令绑定的受信客户端编号".into(),
                ),
                ("build".into(), "本机验证的正式程序构建编号".into()),
            ]),
        };
        prelude.validate()?;
        Some(prelude)
    } else {
        None
    };
    relay_registered_loopback(&endpoint, prelude.as_ref())
}

fn bridge_target(marker: &str) -> Result<(BridgeChannel, bool), String> {
    BridgeChannel::from_marker(marker)
}

// with_queryable_identity（跨登录会话身份读取）：仅向当前账户开放本桥接进程的有限查询权，
// 使桌面宿主仍能严格核验可执行文件；查询后恢复原 DACL（访问控制列表）。不开放写入或终止权。
#[cfg(windows)]
fn with_queryable_identity<T>(owner_sid: &str, operation: impl FnOnce() -> Result<T, String>) -> Result<T, String> {
    use std::{ffi::c_void, ptr, sync::Mutex};
    use windows_sys::Win32::{
        Foundation::LocalFree,
        Security::{ACL, DACL_SECURITY_INFORMATION, Authorization::{
            ConvertStringSidToSidW, GetSecurityInfo, SetEntriesInAclW, SetSecurityInfo,
            EXPLICIT_ACCESS_W, GRANT_ACCESS, SE_KERNEL_OBJECT, TRUSTEE_IS_SID, TRUSTEE_IS_USER, TRUSTEE_W,
        }},
        System::Threading::{GetCurrentProcess, PROCESS_QUERY_LIMITED_INFORMATION},
    };
    static TRANSITION: Mutex<()> = Mutex::new(()); // TRANSITION（当前进程权限临时变更串行锁）。
    let _transition = TRANSITION.lock().map_err(|_| "桥接进程身份权限状态不可用。")?;
    if owner_sid != crate::system_feature::current_user_sid()? {
        return Err("只能为当前账户提供桥接进程身份读取。".into());
    }
    struct LocalMemory(*mut c_void);
    impl Drop for LocalMemory {
        fn drop(&mut self) { if !self.0.is_null() { unsafe { LocalFree(self.0); } } }
    }
    struct ProcessDacl {
        descriptor: *mut c_void, // descriptor（保留原 DACL 存储的系统安全描述符）。
        acl: *mut ACL, // acl（原访问控制列表，不修改其内容）。
        changed: bool, // changed（当前进程是否暂时追加有限查询授权）。
    }
    impl ProcessDacl {
        fn restore(&mut self) -> Result<(), String> {
            if self.changed {
                let code = unsafe { SetSecurityInfo(GetCurrentProcess(), SE_KERNEL_OBJECT,
                    DACL_SECURITY_INFORMATION, ptr::null_mut(), ptr::null_mut(), self.acl, ptr::null()) };
                if code != 0 { return Err(format!("桥接进程原身份权限恢复失败，系统错误码：{code}。")); }
                self.changed = false;
            }
            Ok(())
        }
    }
    impl Drop for ProcessDacl {
        fn drop(&mut self) {
            let _ = self.restore();
            if !self.descriptor.is_null() { unsafe { LocalFree(self.descriptor); } }
        }
    }
    let mut original = ProcessDacl { descriptor: ptr::null_mut(), acl: ptr::null_mut(), changed: false };
    let code = unsafe { GetSecurityInfo(GetCurrentProcess(), SE_KERNEL_OBJECT, DACL_SECURITY_INFORMATION,
        ptr::null_mut(), ptr::null_mut(), &mut original.acl, ptr::null_mut(), &mut original.descriptor) };
    if code != 0 { return Err(format!("无法读取桥接进程身份权限，系统错误码：{code}。")); }
    // null DACL（无限制列表）本来就允许查询，不把它替换为更窄的列表。
    if !original.acl.is_null() {
        let wide = owner_sid.encode_utf16().chain(Some(0)).collect::<Vec<_>>();
        let mut sid = ptr::null_mut();
        if unsafe { ConvertStringSidToSidW(wide.as_ptr(), &mut sid) } == 0 {
            return Err("无法解析当前桥接账户安全标识。".into());
        }
        let _sid = LocalMemory(sid);
        let access = EXPLICIT_ACCESS_W {
            grfAccessPermissions: PROCESS_QUERY_LIMITED_INFORMATION, grfAccessMode: GRANT_ACCESS,
            grfInheritance: 0, Trustee: TRUSTEE_W { TrusteeForm: TRUSTEE_IS_SID,
                TrusteeType: TRUSTEE_IS_USER, ptstrName: sid.cast(), ..Default::default() },
        };
        let mut acl = ptr::null_mut();
        let code = unsafe { SetEntriesInAclW(1, &access, original.acl, &mut acl) };
        let _acl = LocalMemory(acl.cast());
        if code != 0 { return Err(format!("无法准备桥接进程有限查询权限，系统错误码：{code}。")); }
        let code = unsafe { SetSecurityInfo(GetCurrentProcess(), SE_KERNEL_OBJECT,
            DACL_SECURITY_INFORMATION, ptr::null_mut(), ptr::null_mut(), acl, ptr::null()) };
        if code != 0 { return Err(format!("无法提供桥接进程有限查询权限，系统错误码：{code}。")); }
        original.changed = true;
    }
    let result = operation();
    original.restore()?;
    result
}

#[cfg(not(windows))]
fn with_queryable_identity<T>(_owner_sid: &str, operation: impl FnOnce() -> Result<T, String>) -> Result<T, String> {
    operation()
}

fn parse_arguments(args: &[String]) -> Result<BridgeArguments, String> {
    const EXPECTED: [&str; 9] = [
        "--ssh-device-bridge",
        "--workspace-id",
        "",
        "--owner-sid",
        "",
        "--device-id",
        "",
        "--bridge-protocol",
        "",
    ];
    if args.len() != EXPECTED.len()
        || args[0] != EXPECTED[0]
        || args[1] != EXPECTED[1]
        || args[3] != EXPECTED[3]
        || args[5] != EXPECTED[5]
        || args[7] != EXPECTED[7]
    {
        return Err("SSH 桥接参数必须是强制命令中固定的工作区、账户、设备和协议版本。".into());
    }
    let workspace_id = &args[2];
    let owner_sid = &args[4];
    let device_id = &args[6];
    let protocol = args[8]
        .parse::<u32>()
        .map_err(|_| "设备授权协议版本无效。")?;
    if !crate::workspace::valid_workspace_id(workspace_id)
        || !valid_sid(owner_sid)
        || !valid_device_id(device_id)
        || protocol != crate::compatibility::DEVICE_BRIDGE_PROTOCOL_VERSION
    {
        return Err("SSH 桥接身份参数格式无效。".into());
    }
    Ok(BridgeArguments {
        workspace_id: workspace_id.clone(),
        owner_sid: owner_sid.clone(),
        device_id: device_id.clone(),
        protocol,
    })
}

fn verify_ssh_connection_scope(marker: &str) -> Result<(), String> {
    let connection = std::env::var("SSH_CONNECTION")
        .map_err(|_| "OpenSSH 未提供连接来源信息；桥接拒绝会话。".to_string())?;
    validate_ssh_connection_scope(marker, &connection)
}

fn validate_ssh_connection_scope(marker: &str, connection: &str) -> Result<(), String> {
    let parts = connection.split_ascii_whitespace().collect::<Vec<_>>();
    if parts.len() != 4 {
        return Err("OpenSSH 连接来源字段无效。".into());
    }
    let remote_ip = parts[0]
        .split('%')
        .next()
        .unwrap_or("")
        .parse::<IpAddr>()
        .map_err(|_| "SSH 客户端来源地址无效。".to_string())?;
    let local_ip = parts[2]
        .split('%')
        .next()
        .unwrap_or("")
        .parse::<IpAddr>()
        .map_err(|_| "SSH 服务端本机地址无效。".to_string())?;
    let local_port = parts[3]
        .parse::<u16>()
        .map_err(|_| "SSH 服务端端口字段无效。".to_string())?;
    let remote_port = parts[1]
        .parse::<u16>()
        .map_err(|_| "SSH 客户端端口字段无效。".to_string())?;
    if local_port == 0 || remote_port == 0 {
        return Err("SSH 连接端口必须有效。".into());
    }
    // SSH_CONNECTION（已接受连接端点）由微软 SSH 服务提供；精确来源子网由公钥的
    // from（来源范围）限制和仅私有/域网络启用的专用防火墙规则在接受连接时执行。
    // 每条请求只检查该可信元数据，不再启动 PowerShell（系统脚本）重复扫描系统、组件或防火墙。
    let private_lan = |address| {
        ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"]
            .iter()
            .any(|scope| ip_in_scope(address, scope))
    };
    let loopback_pair = remote_ip.is_loopback() && local_ip.is_loopback();
    if !loopback_pair && !(private_lan(remote_ip) && private_lan(local_ip)) {
        return Err("SSH 连接端点不属于受限局域网或本机回环。".into());
    }
    if !matches!(
        marker,
        CONTROL_MARKER | MEDIA_MARKER | MANAGEMENT_MARKER | FILE_MARKER
    ) {
        return Err("SSH 桥接通道标记不在固定白名单中。".into());
    }
    Ok(())
}

fn ip_in_scope(address: IpAddr, scope: &str) -> bool {
    let Some((network, prefix)) = scope.split_once('/') else {
        return false;
    };
    let (Ok(network), Ok(prefix)) = (network.parse::<IpAddr>(), prefix.parse::<u8>()) else {
        return false;
    };
    match (address, network) {
        (IpAddr::V4(address), IpAddr::V4(network)) if prefix <= 32 => {
            let mask = if prefix == 0 {
                0
            } else {
                u32::MAX << (32 - prefix)
            };
            u32::from(address) & mask == u32::from(network) & mask
        }
        (IpAddr::V6(address), IpAddr::V6(network)) if prefix <= 128 => {
            let address = u128::from(address);
            let network = u128::from(network);
            let mask = if prefix == 0 {
                0
            } else {
                u128::MAX << (128 - prefix)
            };
            address & mask == network & mask
        }
        _ => false,
    }
}

fn relay_registered_loopback(
    endpoint: &ServiceEndpoint,
    prelude: Option<&DeviceBridgePrelude>,
) -> Result<(), String> {
    let mut upstream = endpoint.connect()?;
    upstream
        .set_nodelay(true)
        .map_err(|_| "无法配置本机桥接套接字。".to_string())?;
    if let Some(prelude) = prelude {
        let mut bytes =
            serde_json::to_vec(prelude).map_err(|_| "SSH 桥接前导帧无法编码。".to_string())?;
        bytes.push(b'\n');
        if bytes.len() > 4096 {
            return Err("SSH 桥接前导帧超过 4096 字节上限。".into());
        }
        upstream
            .write_all(&bytes)
            .map_err(|_| "无法向本机受限服务提交 SSH 桥接前导帧。".to_string())?;
    }
    relay_streams(upstream, io::stdin(), io::stdout().lock())
}

/// relay_streams（桥接双向字节流）：输出结束与客户端输入结束分别处理。
fn relay_streams<I: Read + Send + 'static, O: Write>(
    mut upstream: TcpStream,
    mut input: I,
    mut output: O,
) -> Result<(), String> {
    let mut reader = upstream
        .try_clone()
        .map_err(|_| "无法建立本机桥接回读通道。".to_string())?;
    let input_task = std::thread::spawn(move || {
        let _ = io::copy(&mut input, &mut upstream);
        // shutdown（半关闭写入）：客户端结束输入后，仍允许服务端返回剩余回执。
        let _ = upstream.shutdown(std::net::Shutdown::Write);
    });
    let copy_result = (|| {
        let mut bytes = [0_u8; 16 * 1024];
        loop {
            let count = reader.read(&mut bytes).map_err(|_| "桥接回读通道中断。")?;
            if count == 0 {
                return Ok(());
            }
            output
                .write_all(&bytes[..count])
                .map_err(|_| "桥接输出写入失败。")?;
            // flush（刷新）：管理 JSON（数据行）、HTTP（控制回执）及二进制音频都不能等待下一个换行。
            output.flush().map_err(|_| "桥接输出刷新失败。")?;
        }
    })();
    let _ = reader.shutdown(std::net::Shutdown::Both);
    // 此入口独占一个短生命周期进程。服务端输出结束即返回，让进程回收仍等待标准输入的线程；
    // join（等待线程退出）会与等待 SSH EOF（通道输出结束）的客户端形成互等。
    drop(input_task);
    copy_result
}

fn valid_sid(value: &str) -> bool {
    let parts = value.split('-').collect::<Vec<_>>();
    parts.len() >= 4
        && parts[0] == "S"
        && parts
            .iter()
            .skip(1)
            .all(|part| !part.is_empty() && part.bytes().all(|b| b.is_ascii_digit()))
}

fn valid_device_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}

fn valid_ascii_token(value: &str, limit: usize) -> bool {
    !value.is_empty()
        && value.len() <= limit
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(windows)]
    #[test]
    fn bridge_identity_query_preserves_the_original_process_acl_on_success_and_failure() {
        use std::ptr;
        use windows_sys::Win32::{Foundation::LocalFree, Security::{ACL, DACL_SECURITY_INFORMATION,
            Authorization::{GetSecurityInfo, SE_KERNEL_OBJECT}}, System::Threading::GetCurrentProcess};
        fn snapshot() -> Vec<u8> {
            unsafe {
                let mut descriptor = ptr::null_mut();
                let mut acl: *mut ACL = ptr::null_mut();
                assert_eq!(GetSecurityInfo(GetCurrentProcess(), SE_KERNEL_OBJECT, DACL_SECURITY_INFORMATION,
                    ptr::null_mut(), ptr::null_mut(), &mut acl, ptr::null_mut(), &mut descriptor), 0);
                let bytes = if acl.is_null() { Vec::new() }
                    else { std::slice::from_raw_parts(acl.cast::<u8>(), (*acl).AclSize as usize).to_vec() };
                LocalFree(descriptor);
                bytes
            }
        }
        let owner = crate::system_feature::current_user_sid().unwrap();
        let before = snapshot();
        assert_eq!(with_queryable_identity(&owner, || Ok(7)).unwrap(), 7);
        assert_eq!(snapshot(), before);
        let result: Result<(), String> = with_queryable_identity(&owner, || Err("测试查询失败".into()));
        assert_eq!(result.unwrap_err(), "测试查询失败");
        assert_eq!(snapshot(), before);
        assert!(with_queryable_identity("S-1-5-18", || Ok(())).is_err());
        assert_eq!(snapshot(), before);
    }

    #[test]
    fn accepted_ssh_metadata_is_bounded_without_repeating_system_scans() {
        for connection in [
            "192.168.253.122 55198 192.168.253.174 22",
            "fd00::2 55198 fd00::1 22",
            "127.0.0.1 55198 127.0.0.1 22",
            "::1 55198 ::1 22",
        ] {
            assert!(validate_ssh_connection_scope(MANAGEMENT_MARKER, connection).is_ok());
        }
        for connection in [
            "8.8.8.8 55198 192.168.253.174 22",
            "192.168.253.122 55198 8.8.8.8 22",
            "169.254.1.2 55198 192.168.253.174 22",
            "127.0.0.1 55198 192.168.253.174 22",
            "192.168.253.122 0 192.168.253.174 22",
            "192.168.253.122 55198 192.168.253.174 0",
            "192.168.253.122 55198 192.168.253.174 22 extra",
        ] {
            assert!(
                validate_ssh_connection_scope(MANAGEMENT_MARKER, connection).is_err(),
                "{connection}"
            );
        }
        assert!(validate_ssh_connection_scope("shell", "127.0.0.1 55198 127.0.0.1 22").is_err());
    }

    struct HeldInput(std::sync::mpsc::Receiver<()>); // HeldInput（保持输入开放）：等待测试显式释放。
    impl Read for HeldInput {
        fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
            let _ = self.0.recv();
            Ok(0)
        }
    }

    #[test]
    fn upstream_eof_finishes_bridge_while_client_input_is_still_open() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let client = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
        let (mut peer, _) = listener.accept().unwrap();
        let (release, held) = std::sync::mpsc::channel();
        let (finished, result) = std::sync::mpsc::channel();
        let worker = std::thread::spawn(move || {
            let mut output = Vec::new();
            let outcome = relay_streams(client, HeldInput(held), &mut output);
            finished.send((outcome, output)).unwrap();
        });
        peer.write_all(b"response-without-newline").unwrap();
        peer.shutdown(std::net::Shutdown::Write).unwrap();
        let completed = result.recv_timeout(std::time::Duration::from_secs(1));
        release.send(()).unwrap(); // 即使回归失败，也先释放测试输入，避免留下工作线程。
        worker.join().unwrap();
        let (outcome, output) = completed.expect("服务端已结束输出时不能等待客户端输入结束");
        outcome.unwrap();
        assert_eq!(output, b"response-without-newline");
    }

    struct FlushOutput {
        pending: Vec<u8>,                       // pending（尚未刷新的输出字节）。
        sent: std::sync::mpsc::Sender<Vec<u8>>, // sent（测试观察到的实际刷新）。
    }
    impl Write for FlushOutput {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            self.pending.extend_from_slice(bytes);
            Ok(bytes.len())
        }
        fn flush(&mut self) -> io::Result<()> {
            if !self.pending.is_empty() {
                self.sent.send(std::mem::take(&mut self.pending)).unwrap();
            }
            Ok(())
        }
    }

    #[test]
    fn binary_output_flushes_before_upstream_or_client_input_closes() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let client = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
        let (mut peer, _) = listener.accept().unwrap();
        let (release, held) = std::sync::mpsc::channel();
        let (sent, flushed) = std::sync::mpsc::channel();
        let worker = std::thread::spawn(move || {
            relay_streams(
                client,
                HeldInput(held),
                FlushOutput {
                    pending: Vec::new(),
                    sent,
                },
            )
        });
        peer.write_all(&[0, 255, 128, 1]).unwrap();
        let output = flushed.recv_timeout(std::time::Duration::from_secs(1));
        peer.shutdown(std::net::Shutdown::Write).unwrap();
        release.send(()).unwrap();
        worker.join().unwrap().unwrap();
        assert_eq!(output.expect("二进制输出必须立即刷新"), [0, 255, 128, 1]);
    }

    #[test]
    fn client_eof_half_closes_request_and_preserves_the_response_direction() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let client = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
        let (mut peer, _) = listener.accept().unwrap();
        peer.set_read_timeout(Some(std::time::Duration::from_secs(1)))
            .unwrap();
        let worker = std::thread::spawn(move || {
            let mut output = Vec::new();
            relay_streams(client, io::Cursor::new(b"complete-request"), &mut output).unwrap();
            output
        });
        let mut request = Vec::new();
        let read = peer.read_to_end(&mut request);
        peer.write_all(b"remaining-response").unwrap();
        peer.shutdown(std::net::Shutdown::Write).unwrap();
        let response = worker.join().unwrap();
        read.expect("客户端结束输入后必须向服务端传递半关闭");
        assert_eq!(request, b"complete-request");
        assert_eq!(response, b"remaining-response");
    }

    #[test]
    fn real_workspace_identities_pass_every_pairing_and_bridge_format_gate() {
        let generated = crate::workspace::new_id();
        for workspace_id in [
            generated.as_str(),
            "18dcc2d519a4a938-81ec",
            "00112233445566778899aabbccddeeff",
            "9525dfbd-5a17-4b2b-ba2c-3744d87d9014",
            "device-client-00112233445566778899aabbccddeeff",
        ] {
            let args = [
                "--ssh-device-bridge",
                "--workspace-id",
                workspace_id,
                "--owner-sid",
                "S-1-5-21-42",
                "--device-id",
                "device-a",
                "--bridge-protocol",
                "1",
            ]
            .map(str::to_owned);
            let arguments = parse_arguments(&args).unwrap();
            let prelude = DeviceBridgePrelude {
                message_type: "bridge.prelude".into(),
                protocol_version: 1,
                workspace_id: arguments.workspace_id,
                owner_sid: arguments.owner_sid,
                client_device_id: arguments.device_id,
                build: "product-current".into(),
                fields: [
                    ("type", "桥接消息类型"),
                    ("protocol_version", "桥接协议版本"),
                    ("workspace_id", "工作区编号"),
                    ("owner_sid", "账户安全标识符"),
                    ("client_device_id", "客户端设备编号"),
                    ("build", "程序构建编号"),
                ]
                .into_iter()
                .map(|(key, value)| (key.into(), value.into()))
                .collect(),
            };
            prelude.validate().unwrap();
            // 用无效公钥止于纯参数检查，不查询系统服务、不提权或修改实际授权。
            let error = crate::system_feature::install_project_device_key(
                "invalid-public-key",
                workspace_id,
                "device-a",
                "product-test",
                std::path::Path::new("unused.exe"),
                "S-1-5-21-42",
                &[],
            )
            .unwrap_err();
            assert_eq!(
                error, "项目授权公钥不是有效 OpenSSH 格式。",
                "{workspace_id}"
            );
        }
    }

    #[test]
    fn workspace_identity_rejects_empty_oversized_and_command_characters() {
        assert!(crate::workspace::valid_workspace_id(&"a".repeat(128)));
        for workspace_id in [
            "",
            "-workspace",
            "工作区",
            "a b",
            "a\tb",
            "a\nb",
            "a\rb",
            "a:b",
            "a/b",
            "a\\b",
            "a\"b",
            "a'b",
            "a;b",
            "a&b",
            "a|b",
            "a$b",
            "a`b",
            "a%b",
            "a\0b",
            &"a".repeat(129),
        ] {
            assert!(
                !crate::workspace::valid_workspace_id(workspace_id),
                "{workspace_id:?}"
            );
            let args = [
                "--ssh-device-bridge",
                "--workspace-id",
                workspace_id,
                "--owner-sid",
                "S-1-5-21-42",
                "--device-id",
                "device-a",
                "--bridge-protocol",
                "1",
            ]
            .map(str::to_owned);
            assert!(parse_arguments(&args).is_err(), "{workspace_id:?}");
            assert_eq!(
                crate::system_feature::install_project_device_key(
                    "invalid-public-key",
                    workspace_id,
                    "device-a",
                    "product-test",
                    std::path::Path::new("unused.exe"),
                    "S-1-5-21-42",
                    &[],
                )
                .unwrap_err(),
                "工作区编号格式无效。"
            );
        }
    }

    #[test]
    fn bridge_arguments_and_scope_checks_are_fixed_and_bounded() {
        let args = [
            "--ssh-device-bridge",
            "--workspace-id",
            "00112233445566778899aabbccddeeff",
            "--owner-sid",
            "S-1-5-21-42",
            "--device-id",
            "device-a",
            "--bridge-protocol",
            "1",
        ]
        .into_iter()
        .map(str::to_owned)
        .collect::<Vec<_>>();
        assert!(parse_arguments(&args).is_ok());
        assert!(parse_arguments(&[args.clone(), vec!["extra".into()]].concat()).is_err());
        let mut invalid = args.clone();
        invalid[7] = "--build".into();
        invalid[8] = "product-old".into();
        assert!(parse_arguments(&invalid).is_err(), "不保留旧构建绑定入口");
        invalid[7] = "--bridge-protocol".into();
        invalid[8] = "2".into();
        assert!(parse_arguments(&invalid).is_err(), "未支持的协议必须拒绝");
        assert!(ip_in_scope(
            "192.168.1.24".parse().unwrap(),
            "192.168.1.0/24"
        ));
        assert!(!ip_in_scope(
            "192.168.2.24".parse().unwrap(),
            "192.168.1.0/24"
        ));
        assert!(ip_in_scope("fd00::12".parse().unwrap(), "fd00::/64"));
    }

    #[test]
    fn bridge_markers_route_only_to_registered_services() {
        assert_eq!(
            bridge_target(CONTROL_MARKER),
            Ok((BridgeChannel::Gateway, false))
        );
        assert_eq!(
            bridge_target(MEDIA_MARKER),
            Ok((BridgeChannel::Gateway, false))
        );
        assert_eq!(
            bridge_target(MANAGEMENT_MARKER),
            Ok((BridgeChannel::Management, true))
        );
        assert_eq!(bridge_target(FILE_MARKER), Ok((BridgeChannel::File, true)));
        assert!(bridge_target("127.0.0.1:9999").is_err());
        assert!(bridge_target("arbitrary-command").is_err());
    }
}
