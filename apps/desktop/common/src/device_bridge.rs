//! DeviceBridge（设备桥接）：SSH 强制命令早期入口，仅把有界会话转给本机固定网关。
use crate::{
    device_config::{self, LoadResult, RunMode},
    device_management_contract::DeviceBridgePrelude,
    device_trust::DeviceTrustStore,
    LaunchContext,
};
use serde::Deserialize;
use std::{
    io::{self, Write},
    net::IpAddr,
};

const CONTROL_MARKER: &str = "voice-terminal-v1-control"; // CONTROL_MARKER（固定控制通道标记）。
const MEDIA_MARKER: &str = "voice-terminal-v1-media"; // MEDIA_MARKER（固定媒体通道标记）。
const MANAGEMENT_MARKER: &str = "voice-terminal-v1-management"; // MANAGEMENT_MARKER（固定独立管理通道标记）。
const FILE_MARKER: &str = "voice-terminal-v1-file"; // FILE_MARKER（固定文件通道标记）。
const LOCAL_GATEWAY: &str = "127.0.0.1:18765"; // LOCAL_GATEWAY（唯一允许访问的本机回环业务入口）。
const LOCAL_MANAGEMENT: &str = "127.0.0.1:18768"; // LOCAL_MANAGEMENT（独立管理服务的本机回环入口）。
const LOCAL_FILE_CHANNEL: &str = "127.0.0.1:18769"; // LOCAL_FILE_CHANNEL（受限资源文件通道的本机回环入口）。

#[derive(Debug)]
struct BridgeArguments {
    workspace_id: String, // workspace_id（被项目密钥绑定的工作区编号）。
    owner_sid: String,    // owner_sid（授权密钥登记的 Windows 登录账户 SID）。
    device_id: String,    // device_id（已经通过配对登记的客户端编号）。
    build: String,        // build（密钥授权时登记的程序构建编号）。
}

#[derive(Deserialize)]
struct ReleaseIdentity {
    build: String, // build（正式程序构建编号）。
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
    let release: ReleaseIdentity = serde_json::from_value(crate::resources::load(
        &context.program.join("release.json"),
    )?)
    .map_err(|_| "当前程序缺少有效 release.json 构建身份。".to_string())?;
    if release.build != arguments.build {
        return Err("SSH 密钥绑定的程序构建与当前受管程序不匹配。".into());
    }
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
    let (target, send_prelude) = bridge_target(&marker)?;
    verify_ssh_connection_scope(&marker)?;
    let prelude = if send_prelude {
        let prelude = DeviceBridgePrelude {
            message_type: "bridge.prelude".into(),
            protocol_version: 1,
            workspace_id: arguments.workspace_id,
            owner_sid: arguments.owner_sid,
            client_device_id: arguments.device_id,
            build: arguments.build,
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
    relay_fixed_loopback(target, prelude.as_ref())
}

fn bridge_target(marker: &str) -> Result<(&'static str, bool), String> {
    match marker {
        CONTROL_MARKER | MEDIA_MARKER => Ok((LOCAL_GATEWAY, false)),
        MANAGEMENT_MARKER => Ok((LOCAL_MANAGEMENT, true)),
        FILE_MARKER => Ok((LOCAL_FILE_CHANNEL, true)),
        _ => Err("OpenSSH 请求了未登记的桥接通道；命令、shell 和子系统均被拒绝。".into()),
    }
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
        "--build",
        "",
    ];
    if args.len() != EXPECTED.len()
        || args[0] != EXPECTED[0]
        || args[1] != EXPECTED[1]
        || args[3] != EXPECTED[3]
        || args[5] != EXPECTED[5]
        || args[7] != EXPECTED[7]
    {
        return Err("SSH 桥接参数必须是强制命令中固定的工作区、账户、设备和构建身份。".into());
    }
    let workspace_id = &args[2];
    let owner_sid = &args[4];
    let device_id = &args[6];
    let build = &args[8];
    if !valid_hex_id(workspace_id, 32)
        || !valid_sid(owner_sid)
        || !valid_device_id(device_id)
        || !valid_ascii_token(build, 128)
    {
        return Err("SSH 桥接身份参数格式无效。".into());
    }
    Ok(BridgeArguments {
        workspace_id: workspace_id.clone(),
        owner_sid: owner_sid.clone(),
        device_id: device_id.clone(),
        build: build.clone(),
    })
}

fn verify_ssh_connection_scope(marker: &str) -> Result<(), String> {
    let connection = std::env::var("SSH_CONNECTION")
        .map_err(|_| "OpenSSH 未提供连接来源信息；桥接拒绝会话。".to_string())?;
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
    let local_port = parts[3]
        .parse::<u16>()
        .map_err(|_| "SSH 服务端端口字段无效。".to_string())?;
    if remote_ip.is_loopback() {
        return if local_port > 0 {
            Ok(())
        } else {
            Err("SSH 本机连接端口无效。".into())
        };
    }
    let status = crate::system_feature::open_ssh_server_status()?;
    if status.effective_port != Some(local_port)
        || !status
            .local_lan_scopes
            .iter()
            .any(|scope| ip_in_scope(remote_ip, scope))
    {
        return Err("SSH 来源地址或端口不属于本机当前受限局域网范围。".into());
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

fn relay_fixed_loopback(target: &str, prelude: Option<&DeviceBridgePrelude>) -> Result<(), String> {
    let mut upstream = std::net::TcpStream::connect(target)
        .map_err(|_| "本机受限设备网关尚未运行；SSH 桥接没有访问其他地址。".to_string())?;
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
    let mut reader = upstream
        .try_clone()
        .map_err(|_| "无法建立本机桥接回读通道。".to_string())?;
    let input_task = std::thread::spawn(move || {
        let mut input = io::stdin().lock();
        let _ = io::copy(&mut input, &mut upstream);
    });
    let mut output = io::stdout().lock();
    let copy_result =
        io::copy(&mut reader, &mut output).map_err(|_| "桥接回读通道中断。".to_string());
    let _ = input_task.join();
    copy_result.map(|_| ())
}

fn valid_hex_id(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
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
            "--build",
            "b20261117",
        ]
        .into_iter()
        .map(str::to_owned)
        .collect::<Vec<_>>();
        assert!(parse_arguments(&args).is_ok());
        assert!(parse_arguments(&[args.clone(), vec!["extra".into()]].concat()).is_err());
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
    fn bridge_markers_route_only_to_fixed_loopback_services() {
        assert_eq!(bridge_target(CONTROL_MARKER), Ok((LOCAL_GATEWAY, false)));
        assert_eq!(bridge_target(MEDIA_MARKER), Ok((LOCAL_GATEWAY, false)));
        assert_eq!(
            bridge_target(MANAGEMENT_MARKER),
            Ok((LOCAL_MANAGEMENT, true))
        );
        assert_eq!(bridge_target(FILE_MARKER), Ok((LOCAL_FILE_CHANNEL, true)));
        assert!(bridge_target("127.0.0.1:9999").is_err());
        assert!(bridge_target("arbitrary-command").is_err());
    }
}
