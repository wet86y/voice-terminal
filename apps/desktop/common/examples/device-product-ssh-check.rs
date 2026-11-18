//! Product device SSH acceptance helper（产品设备 SSH 验收辅助程序）：只在本机构建和执行。
use std::{
    env,
    io::Read,
    path::Path,
    sync::Arc,
    time::{Duration, Instant},
};

use base64::Engine as _;
use russh::client::AuthResult;
use russh::{
    client,
    keys::{HashAlg, PrivateKey, PrivateKeyWithHashAlg, PublicKeyOrCertificate},
    ChannelMsg, Disconnect,
};

use voice_desktop_common::ssh_device::{
    create_device_identity, delete_device_identity, delete_transport_identity,
    device_identity_status, import_transport_identity, transport_identity_status, SshDeviceManager,
    SshDeviceTarget, DEFAULT_GATEWAY_PORT,
};
use zeroize::Zeroize;

fn main() {
    if let Err(error) = run() {
        eprintln!("产品设备 SSH 验收失败：{error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), String> {
    let args = env::args().skip(1).collect::<Vec<_>>();
    match args.first().map(String::as_str) {
        Some("inspect-device-config") if args.len() == 2 => {
            let report = inspect_isolated_device_config(Path::new(&args[1]))?;
            println!("{}", serde_json::to_string(&report).map_err(|_| "设备配置检查结果编码失败。")?);
            Ok(())
        }
        Some("inspect-device-config-base64") if args.len() == 2 => {
            let report = inspect_isolated_device_config(Path::new(&args[1]))?;
            let bytes = serde_json::to_vec(&report).map_err(|_| "设备配置检查结果编码失败。")?;
            println!("{}", base64::engine::general_purpose::STANDARD.encode(bytes));
            Ok(())
        }
        Some("inspect-remote-device-config") if args.len() == 7 => {
            let port = args[2].parse::<u16>().map_err(|_| "SSH 端口无效。")?;
            let runtime = tokio::runtime::Runtime::new()
                .map_err(|_| "无法创建远端设备配置检查运行时。")?;
            let report = runtime.block_on(inspect_remote_device_config(
                &args[1], port, &args[3], &args[4], &args[5], &args[6],
            ))?;
            println!("{report}");
            Ok(())
        }
        Some("pairing-request") if args.len() == 2 => {
            let report = submit_pairing_request_from_isolated_config(Path::new(&args[1]))?;
            println!("{}", serde_json::to_string(&report).map_err(|_| "签名配对申请结果编码失败。")?);
            Ok(())
        }
        Some("identity-status") if args.len() == 3 => {
            let device_id = &args[1];
            let transport_ref = &args[2];
            let identity = device_identity_status(device_id)?;
            let transport = transport_identity_status(transport_ref)?;
            let report = serde_json::json!({
                "device_id": device_id,
                "identity_present": identity.is_some(),
                "identity_fingerprint": identity.as_ref().map(|value| value.fingerprint.as_str()),
                "transport_present": transport.is_some(),
                "transport_fingerprint": transport.as_ref().map(|value| value.fingerprint.as_str()),
                "fields": {
                    "device_id": "本机产品设备编号",
                    "identity_present": "本机凭据库中是否存在产品签名身份",
                    "identity_fingerprint": "产品签名公钥指纹；不返回私钥",
                    "transport_present": "本机凭据库中是否存在 SSH 登录身份",
                    "transport_fingerprint": "SSH 登录公钥指纹；不返回私钥",
                },
            });
            println!("{}", serde_json::to_string(&report).map_err(|_| "身份状态结果编码失败。")?);
            Ok(())
        }
        Some("create-device-identity") if args.len() == 2 => {
            let device_id = &args[1]; // device_id（本机临时固定文件通道验收设备编号）。
            let identity = create_device_identity(device_id)?;
            let result = serde_json::json!({
                "device_id": device_id,
                "credential_ref": identity.credential_ref,
                "fingerprint": identity.fingerprint,
                "fields": {
                    "device_id": "本机临时验收设备编号",
                    "credential_ref": "仅本机 Windows 凭据库中的私钥引用",
                    "fingerprint": "临时验收设备公钥 SHA-256 指纹；不返回私钥",
                },
            });
            println!("{}", serde_json::to_string(&result).map_err(|_| "设备身份结果编码失败。")?);
            Ok(())
        }
        Some("delete-device-identity") if args.len() == 2 => {
            delete_device_identity(&args[1])?;
            println!("{}", serde_json::json!({
                "deleted": true,
                "device_id": args[1],
                "fields": {"deleted":"是否仅删除了指定临时产品设备身份", "device_id":"被删除身份绑定的设备编号"},
            }));
            Ok(())
        }
        Some("prepare") if args.len() == 3 => {
            let device_id = &args[1]; // device_id（临时验收设备编号）。
            let transport = import_transport_identity(Path::new(&args[2]))?;
            let identity = create_device_identity(device_id)?;
            let result = serde_json::json!({
                "device_id": device_id,
                "identity": identity,
                "transport": transport,
                "fields": {
                    "device_id": "临时验收设备编号",
                    "identity": "产品请求签名身份的公钥与本机凭据引用",
                    "transport": "SSH 登录身份的公钥与本机凭据引用",
                },
            });
            println!("{}", serde_json::to_string(&result).map_err(|_| "验收身份结果编码失败。")?);
            Ok(())
        }
        Some("request") if args.len() == 8 => {
            let device_id = args[1].clone(); // device_id（已获主机批准的产品设备编号）。
            let port = args[3].parse::<u16>().map_err(|_| "SSH 端口无效。")?;
            let identity_ref = args[5].clone(); // identity_ref（产品设备签名凭据引用）。
            let transport_ref = args[6].clone(); // transport_ref（SSH 登录凭据引用）。
            let target = SshDeviceTarget {
                address: args[2].clone(), // address（SSH 主机名称）。
                port,
                username: args[4].clone(), // username（主机 SSH 登录用户）。
                host_key_sha256: args[7].clone(), // host_key_sha256（已核验的主机指纹）。
                gateway_port: DEFAULT_GATEWAY_PORT,
            };
            let manager = SshDeviceManager::default();
            let runtime = tokio::runtime::Runtime::new().map_err(|_| "无法创建本机 SSH 验收运行时。")?;
            let result = runtime.block_on(async {
                manager.connect(
                    &identity_ref,
                    Some(&transport_ref),
                    &device_id,
                    target,
                    Duration::from_secs(10),
                ).await?;
                let status = manager.post_json(
                    "/v1/status",
                    &serde_json::json!({"protocol_version": 1}),
                    Duration::from_secs(10),
                ).await;
                let devices = manager.post_json(
                    "/v1/devices",
                    &serde_json::json!({"protocol_version": 1}),
                    Duration::from_secs(10),
                ).await;
                let disconnected = manager.disconnect().await;
                Ok::<_, String>((status?, devices?, disconnected?))
            })?;
            let report = serde_json::json!({
                "device_id": device_id,
                "status": result.0,
                "devices": result.1,
                "disconnected": result.2,
                "fields": {
                    "device_id": "产品设备签名编号",
                    "status": "经 SSH 隧道与设备身份签名的核心状态回执",
                    "devices": "经 SSH 隧道与设备身份签名的设备快照回执",
                    "disconnected": "验收完成后是否释放 SSH 通道",
                },
            });
            println!("{}", serde_json::to_string(&report).map_err(|_| "SSH 验收报告编码失败。")?);
            Ok(())
        }
        Some("session-smoke") if args.len() == 8 => {
            let device_id = args[1].clone(); // device_id（已获主机批准的产品设备编号）。
            let port = args[3].parse::<u16>().map_err(|_| "SSH 端口无效。")?;
            let identity_ref = args[5].clone(); // identity_ref（产品设备签名凭据引用）。
            let transport_ref = args[6].clone(); // transport_ref（SSH 登录凭据引用）。
            let target = SshDeviceTarget {
                address: args[2].clone(), // address（SSH 主机名称）。
                port,
                username: args[4].clone(), // username（主机 SSH 登录用户）。
                host_key_sha256: args[7].clone(), // host_key_sha256（已核验的主机指纹）。
                gateway_port: DEFAULT_GATEWAY_PORT,
            };
            let manager = SshDeviceManager::default();
            let runtime = tokio::runtime::Runtime::new().map_err(|_| "无法创建本机 SSH 验收运行时。")?;
            let report = runtime.block_on(async {
                manager.connect(
                    &identity_ref,
                    Some(&transport_ref),
                    &device_id,
                    target,
                    Duration::from_secs(10),
                ).await?;
                let lease = match manager.open_remote_device_session(Duration::from_secs(10)).await {
                    Ok(lease) => lease,
                    Err(error) => {
                        let _ = manager.disconnect().await;
                        return Err(error);
                    }
                };
                let smoke = async {
                    let devices = manager.post_json(
                        "/v1/devices",
                        &serde_json::json!({"protocol_version": 1}),
                        Duration::from_secs(10),
                    ).await?;
                    if devices.status != 200 {
                        return Err(format!("设备会话打开后设备快照失败，HTTP 状态码 {}。", devices.status));
                    }
                    let rows = devices.body.get("devices").and_then(serde_json::Value::as_array)
                        .ok_or("主机设备快照缺少设备列表。")?;
                    let row = rows.iter().find(|row| row.get("device_id").and_then(serde_json::Value::as_str)
                        == Some(device_id.as_str())).ok_or("主机设备快照未登记当前设备。")?;
                    let has_desktop_control = row.get("capabilities").and_then(serde_json::Value::as_array)
                        .is_some_and(|items| items.iter().any(|item| item.as_str() == Some("desktop_control")));
                    if row.get("connected").and_then(serde_json::Value::as_bool) != Some(true)
                        || !has_desktop_control
                    {
                        return Err("主机设备快照未报告当前连接及桌面控制能力。".into());
                    }
                    let pending = manager.poll_remote_device_task(
                        &lease, 100, Duration::from_secs(5),
                    ).await?;
                    if pending.is_some() {
                        return Err("空闲会话烟测意外取得待执行设备任务；未执行该任务。".into());
                    }
                    let closed = manager.close_remote_device_session(&lease, Duration::from_secs(10)).await?;
                    if !closed {
                        return Err("主机未确认关闭本次设备会话。".into());
                    }
                    let after_close = manager.post_json(
                        "/v1/devices",
                        &serde_json::json!({"protocol_version": 1}),
                        Duration::from_secs(10),
                    ).await?;
                    if after_close.status != 200 {
                        return Err(format!("会话关闭后设备快照失败，HTTP 状态码 {}。", after_close.status));
                    }
                    let disconnected = after_close.body.get("devices").and_then(serde_json::Value::as_array)
                        .and_then(|items| items.iter().find(|row| row.get("device_id")
                            .and_then(serde_json::Value::as_str) == Some(device_id.as_str())))
                        .and_then(|row| row.get("connected"))
                        .and_then(serde_json::Value::as_bool) == Some(false);
                    if !disconnected {
                        return Err("会话关闭后主机设备快照仍报告设备在线。".into());
                    }
                    Ok(serde_json::json!({
                        "device_id": device_id,
                        "core_session_id": lease.core_session_id,
                        "connection_generation": lease.connection_generation,
                        "executor_generation": lease.executor_generation,
                        "capabilities": lease.capabilities,
                        "empty_poll": true,
                        "session_closed": closed,
                        "device_disconnected": disconnected,
                        "fields": {
                            "device_id": "固定远端设备编号",
                            "core_session_id": "本次权威核心运行编号",
                            "connection_generation": "主机授予的连接代次",
                            "executor_generation": "主机授予的执行器代次",
                            "capabilities": "主机登记的设备能力",
                            "empty_poll": "空闲时没有派发任务",
                            "session_closed": "主机已确认关闭任务会话",
                            "device_disconnected": "设备快照已显示客户端断开",
                        },
                    }))
                }.await;
                if smoke.is_err() {
                    let _ = manager.close_remote_device_session(&lease, Duration::from_secs(5)).await;
                }
                let disconnected = manager.disconnect().await;
                let report = smoke?;
                disconnected?;
                Ok::<_, String>(report)
            })?;
            println!("{}", serde_json::to_string(&report).map_err(|_| "会话烟测报告编码失败。")?);
            Ok(())
        }
        Some("heartbeat-smoke") if args.len() == 8 => {
            let device_id = args[1].clone();
            let port = args[3].parse::<u16>().map_err(|_| "SSH 端口无效。")?;
            let identity_ref = args[5].clone();
            let transport_ref = args[6].clone();
            let target = SshDeviceTarget {
                address: args[2].clone(),
                port,
                username: args[4].clone(),
                host_key_sha256: args[7].clone(),
                gateway_port: DEFAULT_GATEWAY_PORT,
            };
            let manager = SshDeviceManager::default();
            let runtime = tokio::runtime::Runtime::new().map_err(|_| "无法创建本机 SSH 心跳验收运行时。")?;
            let report = runtime.block_on(async {
                manager.connect(&identity_ref, Some(&transport_ref), &device_id, target,
                    Duration::from_secs(10)).await?;
                let lease = match manager.open_remote_device_session_with_capabilities(
                    vec!["file_transfer".into(), "terminal_control".into()], Duration::from_secs(10),
                ).await {
                    Ok(lease) => lease,
                    Err(error) => { let _ = manager.disconnect().await; return Err(error); }
                };
                let result = async {
                    manager.heartbeat_remote_device_session(&lease, Duration::from_secs(10)).await?;
                    let devices = manager.post_json("/v1/devices",
                        &serde_json::json!({"protocol_version":1}), Duration::from_secs(10)).await?;
                    if devices.status != 200 {
                        return Err(format!("设备会话心跳后设备快照失败，HTTP 状态码 {}。", devices.status));
                    }
                    let poll = manager.poll_remote_device_task(&lease, 100, Duration::from_secs(5)).await?;
                    if poll.is_some() { return Err("心跳烟测意外取得设备任务；未执行该任务。".into()); }
                    Ok::<_, String>(())
                }.await;
                let close_result = manager.close_remote_device_session(&lease, Duration::from_secs(10)).await;
                let disconnected = manager.disconnect().await;
                result?;
                let closed = close_result?;
                disconnected?;
                if !closed { return Err("主机未确认心跳烟测会话关闭。".into()); }
                Ok::<_, String>(serde_json::json!({
                    "device_id":device_id,"core_session_id":lease.core_session_id,
                    "connection_generation":lease.connection_generation,
                    "executor_generation":lease.executor_generation,"capabilities":lease.capabilities,
                    "heartbeat_confirmed":true,"empty_poll":true,"session_closed":closed,
                    "fields":{"device_id":"固定远端设备编号","core_session_id":"本次权威核心运行编号",
                        "connection_generation":"主机授予的连接代次","executor_generation":"主机授予的执行器代次",
                        "capabilities":"本次远端会话登记的安全文件/终端能力","heartbeat_confirmed":"签名租约续接获得确认",
                        "empty_poll":"未取得或执行任务","session_closed":"主机确认关闭远端会话"},
                }))
            })?;
            println!("{}", serde_json::to_string(&report).map_err(|_| "心跳烟测报告编码失败。")?);
            Ok(())
        }
        Some("sftp-smoke" | "sftp-download") => Err("独立 SFTP 已停用；文件验收必须由 DSH（代理执行宿主）授权任务建立 FILE（固定文件通道）票据后使用正式设备文件接口。".into()),
        Some("verify-client-package") if args.len() == 10 => {
            let port = args[2].parse::<u16>().map_err(|_| "SSH 端口无效。")?;
            let runtime = tokio::runtime::Runtime::new().map_err(|_| "无法创建本机远端包校验运行时。")?;
            let report = runtime.block_on(verify_remote_client_package(
                &args[1], port, &args[3], &args[4], &args[5], &args[6], &args[7], &args[8], &args[9],
            ))?;
            println!("{report}");
            Ok(())
        }
        Some("verify-program-package") if args.len() == 9 => {
            let port = args[2].parse::<u16>().map_err(|_| "SSH 端口无效。")?;
            let runtime = tokio::runtime::Runtime::new()
                .map_err(|_| "无法创建正式程序包远端校验运行时。")?;
            let report = runtime.block_on(verify_remote_program_package(
                &args[1], port, &args[3], &args[4], &args[5], &args[6], &args[7], &args[8],
            ))?;
            println!("{report}");
            Ok(())
        }
        Some("launch-client-workspace") if args.len() == 8 => {
            let port = args[2].parse::<u16>().map_err(|_| "SSH 端口无效。")?;
            let runtime = tokio::runtime::Runtime::new()
                .map_err(|_| "无法创建远端轻量客户端启动运行时。")?;
            let report = runtime.block_on(launch_remote_client_workspace(
                &args[1], port, &args[3], &args[4], &args[5], &args[6], &args[7],
            ))?;
            println!("{report}");
            Ok(())
        }
        Some("read-client-package-report") if args.len() == 8 => {
            let port = args[2].parse::<u16>().map_err(|_| "SSH 端口无效。")?;
            let runtime = tokio::runtime::Runtime::new().map_err(|_| "无法创建本机验收报告读取运行时。")?;
            let report = runtime.block_on(read_remote_client_package_report(
                &args[1], port, &args[3], &args[4], &args[5], &args[6], &args[7],
            ))?;
            println!("{report}");
            Ok(())
        }
        Some("inspect-client-host") if args.len() == 6 => {
            let port = args[2].parse::<u16>().map_err(|_| "SSH 端口无效。")?;
            let runtime = tokio::runtime::Runtime::new().map_err(|_| "无法创建主机只读预检运行时。")?;
            let report = runtime.block_on(inspect_remote_client_host(
                &args[1], port, &args[3], &args[4], &args[5],
            ))?;
            println!("{report}");
            Ok(())
        }
        Some("cleanup") if args.len() == 3 => {
            delete_device_identity(&args[1])?;
            delete_transport_identity(&args[2])?;
            println!("本机临时产品签名身份与 SSH 登录凭据已清理。");
            Ok(())
        }
        _ => Err("用法：inspect-device-config|inspect-device-config-base64 <cleanup 内的隔离数据目录>；inspect-remote-device-config <host> <port> <username> <transport_credential_ref> <host_key_sha256> <cleanup 内的数据目录>；pairing-request <cleanup 内的有效 RemoteClient 数据目录>（配对码只从标准输入读取）；identity-status <device_id> <transport_credential_ref>；create-device-identity|delete-device-identity <device_id>；prepare <device_id> <local_private_key_path>；request|session-smoke|heartbeat-smoke <device_id> <host> <port> <username> <identity_credential_ref> <transport_credential_ref> <host_key_sha256>；verify-client-package <host> <port> <username> <transport_credential_ref> <host_key_sha256> <source_build> <client_build> <archive_sha256> <attempt_id>；verify-program-package <host> <port> <username> <transport_credential_ref> <host_key_sha256> <program_build> <archive_sha256> <attempt_id>；launch-client-workspace <host> <port> <username> <transport_credential_ref> <host_key_sha256> <client_build> <attempt_id>；read-client-package-report <host> <port> <username> <transport_credential_ref> <host_key_sha256> <client_build> <attempt_id>；inspect-client-host <host> <port> <username> <transport_credential_ref> <host_key_sha256>；cleanup <device_id> <transport_credential_ref>".into()),
    }
}

const ACCEPTANCE_HOST: &str = "EXAMPLE-TEST-HOST"; // ACCEPTANCE_HOST（唯一授权产品验收主机）。
const ACCEPTANCE_USER: &str = "example-test-user"; // ACCEPTANCE_USER（获准主机的 SSH 验收账户）。
const ACCEPTANCE_FINGERPRINT: &str = "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"; // ACCEPTANCE_FINGERPRINT（固定主机公钥指纹）。
const ACCEPTANCE_CLEANUP_ROOT: &str = r"D:\AI\example-test\VoiceTerminal-DeviceValidation-20261115\cleanup"; // ACCEPTANCE_CLEANUP_ROOT（唯一允许写入的远端验收目录）。

/// inspect_isolated_device_config（读取隔离设备配置）：只检查 cleanup 内的有效配置，不创建或修改。
fn inspect_isolated_device_config(data_root: &Path) -> Result<serde_json::Value, String> {
    let cleanup_root = Path::new(ACCEPTANCE_CLEANUP_ROOT)
        .canonicalize()
        .map_err(|_| "获准验收 cleanup 根目录不存在，拒绝检查其他位置。".to_string())?;
    let data_root = data_root
        .canonicalize()
        .map_err(|_| "隔离设备数据目录不存在，未创建或修改配置。".to_string())?;
    if !data_root.starts_with(&cleanup_root) || !data_root.is_dir() {
        return Err("设备配置检查仅允许读取获准 cleanup 根目录内的现存目录。".into());
    }
    let (config_state, config) =
        match voice_desktop_common::device_config::load_from_root(&data_root) {
            voice_desktop_common::device_config::LoadResult::Missing => ("missing", None),
            voice_desktop_common::device_config::LoadResult::Invalid(_) => ("invalid", None),
            voice_desktop_common::device_config::LoadResult::Valid(config) => {
                ("valid", Some(config))
            }
        };
    let device_id = config.as_ref().map(|value| value.device_id.clone());
    let role = config.as_ref().map(|value| match value.desired_role {
        voice_desktop_common::device_config::Role::Standalone => "standalone",
        voice_desktop_common::device_config::Role::SharedHost => "shared_host",
        voice_desktop_common::device_config::Role::RemoteClient => "remote_client",
    });
    let has_host = config.as_ref().is_some_and(|value| value.host.is_some());
    let acceptance_client_target = config
        .as_ref()
        .and_then(|value| value.host.as_ref())
        .is_some_and(|host| {
            matches!(
                host.address.as_str(),
                "127.0.0.1" | "localhost" | ACCEPTANCE_HOST
            ) && host.port == 22
                && host.username == ACCEPTANCE_USER
                && host.host_key_sha256 == ACCEPTANCE_FINGERPRINT
                && host.transport_credential_ref.is_some()
        });
    let (product_identity_state, transport_identity_state) = match config.as_ref() {
        Some(config) => {
            let product_state = match device_identity_status(&config.device_id) {
                Ok(Some(_)) => "present",
                Ok(None) => "missing",
                Err(_) => "unavailable",
            };
            let transport_state = match config
                .host
                .as_ref()
                .and_then(|host| host.transport_credential_ref.as_deref())
            {
                Some(reference) => match transport_identity_status(reference) {
                    Ok(Some(_)) => "present",
                    Ok(None) => "missing",
                    Err(_) => "unavailable",
                },
                None => "not_configured",
            };
            (product_state, transport_state)
        }
        None => ("not_checked", "not_checked"),
    };
    Ok(serde_json::json!({
        "config_state": config_state,
        "device_id": device_id,
        "desired_role": role,
        "has_host": has_host,
        "acceptance_client_target": acceptance_client_target,
        "product_identity_state": product_identity_state,
        "transport_identity_state": transport_identity_state,
        "fields": {
            "config_state": "隔离设备配置状态；无效配置原件保持不变",
            "device_id": "有效配置中的设备稳定编号",
            "desired_role": "有效配置中已保存的期望角色",
            "has_host": "配置是否含已信任主机资料",
            "acceptance_client_target": "是否为固定验收主机、固定 SSH 用户/端口/指纹且使用独立 SSH 凭据引用",
            "product_identity_state": "该设备产品签名身份在当前 Windows 凭据库中的状态；不返回密钥或指纹",
            "transport_identity_state": "配置所引用的独立 SSH 登录身份在当前 Windows 凭据库中的状态；不返回凭据引用或指纹",
        }
    }))
}

/// submit_pairing_request_from_isolated_config（隔离客户端签名申请）：复用正式 SSH manager 和有效设备配置。
fn submit_pairing_request_from_isolated_config(
    data_root: &Path,
) -> Result<serde_json::Value, String> {
    let cleanup_root = Path::new(ACCEPTANCE_CLEANUP_ROOT)
        .canonicalize()
        .map_err(|_| "获准验收 cleanup 根目录不存在，拒绝连接。".to_string())?;
    let data_root = data_root
        .canonicalize()
        .map_err(|_| "隔离设备数据目录不存在；未创建或修改配置。".to_string())?;
    if !data_root.starts_with(&cleanup_root) || !data_root.is_dir() {
        return Err("配对申请仅允许使用获准 cleanup 根目录内的现存配置。".into());
    }
    let config = match voice_desktop_common::device_config::load_from_root(&data_root) {
        voice_desktop_common::device_config::LoadResult::Valid(config) => config,
        voice_desktop_common::device_config::LoadResult::Missing => {
            return Err("隔离目录中没有已保存的设备配置；未生成 RemoteClient 配置。".into());
        }
        voice_desktop_common::device_config::LoadResult::Invalid(error) => {
            return Err(format!("隔离设备配置无效且保持原样：{error}"));
        }
    };
    if config.desired_role != voice_desktop_common::device_config::Role::RemoteClient {
        return Err("配对申请要求产品已保存 RemoteClient（远端客户端）角色；未更改角色。".into());
    }
    let host = config
        .host
        .ok_or("有效 RemoteClient 配置缺少主机资料；未更改配置。")?;
    if !matches!(
        host.address.as_str(),
        "127.0.0.1" | "localhost" | ACCEPTANCE_HOST
    ) || host.port != 22
        || host.username != ACCEPTANCE_USER
        || host.host_key_sha256 != ACCEPTANCE_FINGERPRINT
    {
        return Err(
            "已保存客户端目标不匹配本次固定验收主机、SSH 用户、端口或指纹；未连接。".into(),
        );
    }
    let transport_ref = host
        .transport_credential_ref
        .as_deref()
        .ok_or("隔离客户端配置没有独立 SSH 登录凭据引用；未复用产品签名密钥。")?;
    let target = SshDeviceTarget {
        address: host.address,
        port: host.port,
        username: host.username,
        host_key_sha256: host.host_key_sha256,
        gateway_port: DEFAULT_GATEWAY_PORT,
    };
    let manager = SshDeviceManager::default();
    let runtime =
        tokio::runtime::Runtime::new().map_err(|_| "无法创建本机 SSH 配对验收运行时。")?;
    let (response, disconnected) = runtime.block_on(async {
        manager
            .connect(
                &host.credential_ref,
                Some(transport_ref),
                &config.device_id,
                target,
                Duration::from_secs(15),
            )
            .await?;
        eprint!("请输入主机本地配对窗口显示的 8 位配对码；输入只从标准输入读取：");
        let stdin = std::io::stdin();
        let mut stdin = stdin.lock();
        let mut code = read_pairing_code_from(&mut stdin)?;
        let request = manager
            .submit_pairing_request(&config.device_id, &code, Duration::from_secs(10))
            .await;
        code.zeroize();
        let disconnected = manager.disconnect().await;
        Ok::<_, String>((request?, disconnected?))
    })?;
    if response.status != 202 {
        return Err(format!(
            "配对申请返回非预期 HTTP 状态：{}。",
            response.status
        ));
    }
    let request_id = response
        .body
        .get("request_id")
        .and_then(serde_json::Value::as_str)
        .ok_or("主机配对申请回执缺少 request_id（申请编号）。")?;
    let device_id = response
        .body
        .get("device_id")
        .and_then(serde_json::Value::as_str)
        .ok_or("主机配对申请回执缺少 device_id（设备编号）。")?;
    let fingerprint = response
        .body
        .get("fingerprint")
        .and_then(serde_json::Value::as_str)
        .ok_or("主机配对申请回执缺少 fingerprint（公钥指纹）。")?;
    Ok(serde_json::json!({
        "status": response.status,
        "request_id": request_id,
        "device_id": device_id,
        "fingerprint": fingerprint,
        "disconnected": disconnected,
        "fields": {
            "status": "主机通过产品网关返回的 HTTP 状态码；202 表示待主机批准",
            "request_id": "待主机正常批准的配对申请编号",
            "device_id": "从已保存 RemoteClient 配置读取的稳定设备编号",
            "fingerprint": "产品签名设备公钥 SHA-256 指纹，不含私钥",
            "disconnected": "配对申请后是否关闭产品 SSH 通道",
        }
    }))
}

/// read_pairing_code_from（从标准输入读取配对码）：限定 8 位数字，不进入命令行参数。
fn read_pairing_code_from(input: &mut impl Read) -> Result<String, String> {
    let mut bytes = Vec::with_capacity(8);
    let mut byte = [0u8; 1];
    loop {
        input
            .read_exact(&mut byte)
            .map_err(|_| "未从标准输入收到完整配对码。".to_string())?;
        match byte[0] {
            b'\n' => break,
            b'\r' if bytes.len() == 8 => {
                input
                    .read_exact(&mut byte)
                    .map_err(|_| "配对码行结束符不完整。".to_string())?;
                if byte[0] != b'\n' {
                    return Err("配对码行结束符无效。".into());
                }
                break;
            }
            digit if digit.is_ascii_digit() && bytes.len() < 8 => bytes.push(digit),
            _ => return Err("配对码必须恰好为 8 位数字。".into()),
        }
    }
    if bytes.len() != 8 {
        return Err("配对码必须恰好为 8 位数字。".into());
    }
    String::from_utf8(bytes).map_err(|_| "配对码编码无效。".into())
}

#[cfg(test)]
mod pairing_cli_tests {
    use super::read_pairing_code_from;
    use std::io::Cursor;

    #[test]
    fn pairing_code_is_bounded_and_requires_exact_ascii_digits() {
        let mut input = Cursor::new(b"01234567\n");
        assert_eq!(read_pairing_code_from(&mut input).unwrap(), "01234567");

        let mut crlf = Cursor::new(b"87654321\r\n");
        assert_eq!(read_pairing_code_from(&mut crlf).unwrap(), "87654321");

        for invalid in [
            b"1234567\n".as_slice(),
            b"123456789\n",
            b"1234x678\n",
            b"12345678",
        ] {
            let mut input = Cursor::new(invalid);
            assert!(read_pairing_code_from(&mut input).is_err());
        }
    }
}

#[derive(Clone)]
struct PinnedRemoteCommandHandler {
    fingerprint: String, // fingerprint（连接前固定核对的 SSH 主机公钥指纹）。
}

impl client::Handler for PinnedRemoteCommandHandler {
    type Error = russh::Error;

    async fn check_server_key(
        &mut self,
        server_public_key: &PublicKeyOrCertificate,
    ) -> Result<bool, Self::Error> {
        Ok(server_public_key
            .public_key()
            .fingerprint(HashAlg::Sha256)
            .to_string()
            == self.fingerprint)
    }
}

/// verify_remote_client_package（远端产品包验收）：仅展开到固定 cleanup 并运行只读正式包校验。
async fn verify_remote_client_package(
    host: &str,
    port: u16,
    username: &str,
    transport_ref: &str,
    fingerprint: &str,
    source_build: &str,
    client_build: &str,
    archive_sha256: &str,
    attempt_id: &str,
) -> Result<String, String> {
    if host != ACCEPTANCE_HOST
        || port != 22
        || username != ACCEPTANCE_USER
        || fingerprint != ACCEPTANCE_FINGERPRINT
    {
        return Err("远端包校验仅允许访问本计划登记的固定验收主机、SSH 账户及指纹。".into());
    }
    let Some(source_hash) = source_build.strip_prefix("product-") else {
        return Err("正式程序构建编号格式无效。".into());
    };
    let Some(client_suffix) = client_build.strip_prefix("client-") else {
        return Err("轻量客户端构建编号格式无效。".into());
    };
    let Some((client_hash, revision)) = client_suffix.rsplit_once("-r") else {
        return Err("轻量客户端修订号格式无效。".into());
    };
    if source_hash.len() != 16
        || !source_hash
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        || client_hash != source_hash
        || revision.is_empty()
        || !revision.bytes().all(|byte| byte.is_ascii_digit())
        || revision.starts_with('0')
        || archive_sha256.len() != 64
        || !archive_sha256
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        || attempt_id.is_empty()
        || attempt_id.len() > 32
        || !attempt_id
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
        || attempt_id.starts_with('-')
        || attempt_id.ends_with('-')
        || !transport_ref.starts_with("VoiceTerminal/ssh-transport/")
        || transport_ref.len() != "VoiceTerminal/ssh-transport/".len() + 64
    {
        return Err("远端轻量客户端候选或验收凭据引用格式无效。".into());
    }
    let suffix = transport_ref
        .strip_prefix("VoiceTerminal/ssh-transport/")
        .unwrap_or_default();
    if !suffix
        .bytes()
        .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err("SSH 登录凭据引用格式无效。".into());
    }

    let command =
        remote_package_verify_command(source_build, client_build, archive_sha256, attempt_id)?;
    let private_key = voice_desktop_common::credentials::load_device_ssh_key(transport_ref)?;
    let private_key = PrivateKey::from_openssh(private_key.as_str())
        .map_err(|_| "本机 Windows 凭据库中的 SSH 登录密钥无法解析。".to_string())?;
    if private_key.is_encrypted() {
        return Err("SSH 登录密钥仍处于加密状态。".into());
    }

    let handler = PinnedRemoteCommandHandler {
        fingerprint: fingerprint.to_owned(),
    };
    let mut handle = tokio::time::timeout(
        Duration::from_secs(15),
        client::connect(Arc::new(client::Config::default()), (host, port), handler),
    )
    .await
    .map_err(|_| "固定验收主机 SSH 握手超时。".to_string())?
    .map_err(|_| "固定验收主机 SSH 指纹不匹配或连接失败。".to_string())?;
    let key = PrivateKeyWithHashAlg::new(Arc::new(private_key), Some(HashAlg::Sha256));
    let auth = tokio::time::timeout(
        Duration::from_secs(15),
        handle.authenticate_publickey(username.to_owned(), key),
    )
    .await
    .map_err(|_| "固定验收主机 SSH 公钥认证超时。".to_string())?
    .map_err(|_| "固定验收主机 SSH 公钥认证失败。".to_string())?;
    if !matches!(auth, AuthResult::Success) {
        let _ = handle
            .disconnect(
                Disconnect::ByApplication,
                "package verification authentication failed",
                "",
            )
            .await;
        return Err("固定验收主机未接受本机 SSH 登录身份。".into());
    }

    let mut channel = tokio::time::timeout(Duration::from_secs(15), handle.channel_open_session())
        .await
        .map_err(|_| "固定验收主机 SSH 命令通道开启超时。".to_string())?
        .map_err(|_| "固定验收主机不允许打开 SSH 命令通道。".to_string())?;
    tokio::time::timeout(
        Duration::from_secs(15),
        channel.exec(true, command.into_bytes()),
    )
    .await
    .map_err(|_| "固定验收主机 SSH 包校验命令未及时接纳。".to_string())?
    .map_err(|_| "固定验收主机未接纳受限的包校验命令。".to_string())?;

    let deadline = Instant::now() + Duration::from_secs(300);
    let mut stdout = Vec::new();
    let mut stderr = Vec::new();
    let mut exit_status = None;
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            let _ = channel.close().await;
            let _ = handle
                .disconnect(
                    Disconnect::ByApplication,
                    "package verification timed out",
                    "",
                )
                .await;
            return Err("固定验收主机包校验等待超时；候选留在 cleanup 中供后续复查。".into());
        }
        match tokio::time::timeout(remaining, channel.wait()).await {
            Err(_) => {
                let _ = channel.close().await;
                let _ = handle
                    .disconnect(
                        Disconnect::ByApplication,
                        "package verification timed out",
                        "",
                    )
                    .await;
                return Err("固定验收主机包校验等待超时；候选留在 cleanup 中供后续复查。".into());
            }
            Ok(None) => break,
            Ok(Some(ChannelMsg::Data { data })) => stdout.extend_from_slice(&data),
            Ok(Some(ChannelMsg::ExtendedData { data, ext: 1 })) => stderr.extend_from_slice(&data),
            Ok(Some(ChannelMsg::ExitStatus { exit_status: value })) => exit_status = Some(value),
            Ok(Some(ChannelMsg::ExitSignal { signal_name, .. })) => {
                let _ = channel.close().await;
                let _ = handle
                    .disconnect(
                        Disconnect::ByApplication,
                        "package verification signaled",
                        "",
                    )
                    .await;
                return Err(format!("固定验收主机包校验收到信号：{signal_name:?}"));
            }
            Ok(Some(ChannelMsg::Close)) => break,
            Ok(Some(_)) => {}
        }
        if stdout.len().saturating_add(stderr.len()) > 64 * 1024 {
            let _ = channel.close().await;
            let _ = handle
                .disconnect(
                    Disconnect::ByApplication,
                    "package verification output limit",
                    "",
                )
                .await;
            return Err("固定验收主机包校验输出超过 64 KiB 上限。".into());
        }
    }
    let _ = channel.close().await;
    let _ = handle
        .disconnect(
            Disconnect::ByApplication,
            "package verification complete",
            "",
        )
        .await;
    let exit_status = exit_status.ok_or("固定验收主机未返回包校验退出状态。")?;
    let stdout = String::from_utf8_lossy(&stdout).trim().to_owned();
    let stderr = String::from_utf8_lossy(&stderr).trim().to_owned();
    if exit_status != 0 {
        return Err(format!(
            "固定验收主机正式包校验失败（退出码 {exit_status}）；stdout={stdout}; stderr={stderr}"
        ));
    }
    if !stdout.contains(&format!("VERIFY_EXIT=0;BUILD={client_build}")) {
        return Err("远端校验退出码为零，但缺少预期的构建编号回执。".into());
    }
    Ok(stdout)
}

fn remote_package_verify_command(
    source_build: &str,
    client_build: &str,
    archive_sha256: &str,
    attempt_id: &str,
) -> Result<String, String> {
    let template = r#"
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = '__ROOT__'
$sourceBuild = '__SOURCE_BUILD__'
$clientBuild = '__CLIENT_BUILD__'
$attemptId = '__ATTEMPT_ID__'
$expectedArchiveHash = '__ARCHIVE_SHA256__'
$archive = Join-Path $root ('fixtures\client-package-' + $clientBuild + '.zip')
$portableSource = Join-Path $root ('fixtures\client-payload-' + $clientBuild + '.portable.json')
$stage = Join-Path $root ('fixtures\client-payload-' + $clientBuild + '-' + $attemptId)
$report = Join-Path $root ('reports\' + $clientBuild + '-package-verification-' + $attemptId + '.json')
$stdoutPath = Join-Path $root ('reports\' + $clientBuild + '-package-verification-' + $attemptId + '.stdout.txt')
$stderrPath = Join-Path $root ('reports\' + $clientBuild + '-package-verification-' + $attemptId + '.stderr.txt')
$fields = @{
    schema = '验收报告格式版本'
    source_build = '正式程序构建编号'
    client_build = '轻量客户端候选构建编号'
    verification_attempt = '本轮不可覆盖的远端验收尝试编号'
    archive = '固定 cleanup 内的候选归档路径'
    archive_sha256 = '候选归档 SHA-256 摘要'
    portable_json = '独立于可复用归档的本机工作区配置'
    extraction_directory = 'cleanup 内的隔离解包目录'
    verify_exit_code = '正式入口只读包校验退出码'
    verified = '正式入口是否接受候选包'
    stdout_path = 'cleanup 内的校验标准输出日志'
    stderr_path = 'cleanup 内的校验错误输出日志'
    fields = '本报告字段中文说明'
}
$record = $null
try {
    foreach ($path in @('D:\AI', 'D:\AI\example-test', 'D:\AI\example-test\VoiceTerminal-DeviceValidation-20261115', $root, (Join-Path $root 'fixtures'), (Join-Path $root 'reports'))) {
        $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw '验收目录包含重解析点，拒绝继续。' }
    }
    if (!(Test-Path -LiteralPath $archive -PathType Leaf) -or !(Test-Path -LiteralPath $portableSource -PathType Leaf)) { throw '候选归档或工作区配置未完整暂存。' }
    if ((Test-Path -LiteralPath $stage) -or (Test-Path -LiteralPath $report) -or (Test-Path -LiteralPath $stdoutPath) -or (Test-Path -LiteralPath $stderrPath)) { throw '候选解包或报告路径已存在；拒绝覆盖。' }
    $archiveItem = Get-Item -LiteralPath $archive -ErrorAction Stop
    if ($archiveItem.Length -gt 16777216) { throw '候选归档超过 16 MiB 验收上限。' }
    $actualArchiveHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
    if ($actualArchiveHash -ne $expectedArchiveHash) { throw '远端候选归档摘要与本机构建记录不一致。' }
    $portableText = [IO.File]::ReadAllText($portableSource, [Text.Encoding]::UTF8)
    $portable = $portableText | ConvertFrom-Json -ErrorAction Stop
    if ($portable.schema -ne 2 -or $portable.data_dir -ne 'data' -or $portable.app -ne 'app' -or $portable.workspace_id -notmatch '^device-client-[0-9a-f]{32}$') { throw '客户端实例配置格式或工作区身份无效。' }
    Expand-Archive -LiteralPath $archive -DestinationPath $stage -ErrorAction Stop
    Copy-Item -LiteralPath $portableSource -Destination (Join-Path $stage 'portable.json') -ErrorAction Stop
    $executable = Join-Path $stage 'VoiceTerminal.exe'
    if (!(Test-Path -LiteralPath $executable -PathType Leaf)) { throw '客户端正式入口缺失。' }
    $process = Start-Process -FilePath $executable -ArgumentList '--verify-package' -WorkingDirectory $stage -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -ErrorAction Stop
    $verifyExitCode = [int]$process.ExitCode
    $record = [ordered]@{
        schema = 1
        source_build = $sourceBuild
        client_build = $clientBuild
        verification_attempt = $attemptId
        archive = $archive
        archive_size = $archiveItem.Length
        archive_sha256 = $actualArchiveHash
        portable_json = $portableSource
        portable_workspace_id = $portable.workspace_id
        extraction_directory = $stage
        verify_exit_code = $verifyExitCode
        verified = ($verifyExitCode -eq 0)
        stdout_path = $stdoutPath
        stderr_path = $stderrPath
        fields = $fields
    }
    [IO.File]::WriteAllText($report, ($record | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
    Write-Output ('VERIFY_EXIT={0};BUILD={1}' -f $verifyExitCode, $clientBuild)
    exit $verifyExitCode
} catch {
    $failureMessage = $_.Exception.Message
    if (!(Test-Path -LiteralPath $report)) {
        $record = [ordered]@{
            schema = 1
            source_build = $sourceBuild
            client_build = $clientBuild
            verification_attempt = $attemptId
            archive = $archive
            archive_sha256 = $expectedArchiveHash
            extraction_directory = $stage
            verify_exit_code = 1
            verified = $false
            error = $failureMessage
            fields = $fields + @{ error = '验收失败说明；cleanup 内其他现场保留' }
        }
        [IO.File]::WriteAllText($report, ($record | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
    }
    [Console]::Error.WriteLine('CLIENT_PACKAGE_VERIFY_FAILED')
    exit 1
}
"#;
    let script = template
        .replace("__ROOT__", ACCEPTANCE_CLEANUP_ROOT)
        .replace("__SOURCE_BUILD__", source_build)
        .replace("__CLIENT_BUILD__", client_build)
        .replace("__ATTEMPT_ID__", attempt_id)
        .replace("__ARCHIVE_SHA256__", archive_sha256);
    let utf16 = script
        .encode_utf16()
        .flat_map(u16::to_le_bytes)
        .collect::<Vec<_>>();
    let encoded = base64::engine::general_purpose::STANDARD.encode(utf16);
    Ok(format!(
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand {encoded}"
    ))
}

/// read_remote_client_package_report（读取远端验收报告）：固定 cleanup 路径，只读回传已落盘 JSON。
async fn read_remote_client_package_report(
    host: &str,
    port: u16,
    username: &str,
    transport_ref: &str,
    fingerprint: &str,
    client_build: &str,
    attempt_id: &str,
) -> Result<String, String> {
    if host != ACCEPTANCE_HOST
        || port != 22
        || username != ACCEPTANCE_USER
        || fingerprint != ACCEPTANCE_FINGERPRINT
    {
        return Err("验收报告读取仅允许访问本计划登记的固定主机、账户及指纹。".into());
    }
    let Some(client_suffix) = client_build.strip_prefix("client-") else {
        return Err("轻量客户端构建编号格式无效。".into());
    };
    let Some((client_hash, revision)) = client_suffix.rsplit_once("-r") else {
        return Err("轻量客户端修订号格式无效。".into());
    };
    if client_hash.len() != 16
        || !client_hash
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        || revision.is_empty()
        || !revision.bytes().all(|byte| byte.is_ascii_digit())
        || revision.starts_with('0')
        || attempt_id.is_empty()
        || attempt_id.len() > 32
        || !attempt_id
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
        || attempt_id.starts_with('-')
        || attempt_id.ends_with('-')
        || !transport_ref.starts_with("VoiceTerminal/ssh-transport/")
        || transport_ref.len() != "VoiceTerminal/ssh-transport/".len() + 64
    {
        return Err("客户端构建编号或 SSH 登录凭据引用格式无效。".into());
    }
    let suffix = transport_ref
        .strip_prefix("VoiceTerminal/ssh-transport/")
        .unwrap_or_default();
    if !suffix
        .bytes()
        .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err("SSH 登录凭据引用格式无效。".into());
    }

    let template = r#"
$ErrorActionPreference = 'Stop'
$report = Join-Path '__ROOT__' ('reports\' + '__CLIENT_BUILD__' + '-package-verification-' + '__ATTEMPT_ID__' + '.json')
if (!(Test-Path -LiteralPath $report -PathType Leaf)) { [Console]::Error.WriteLine('PACKAGE_REPORT_NOT_FOUND'); exit 3 }
$bytes = [IO.File]::ReadAllBytes($report)
[Console]::WriteLine([Convert]::ToBase64String($bytes))
exit 0
"#;
    let script = template
        .replace("__ROOT__", ACCEPTANCE_CLEANUP_ROOT)
        .replace("__CLIENT_BUILD__", client_build)
        .replace("__ATTEMPT_ID__", attempt_id);
    let utf16 = script
        .encode_utf16()
        .flat_map(u16::to_le_bytes)
        .collect::<Vec<_>>();
    let encoded = base64::engine::general_purpose::STANDARD.encode(utf16);
    let command = format!(
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand {encoded}"
    );

    let private_key = voice_desktop_common::credentials::load_device_ssh_key(transport_ref)?;
    let private_key = PrivateKey::from_openssh(private_key.as_str())
        .map_err(|_| "本机 Windows 凭据库中的 SSH 登录密钥无法解析。".to_string())?;
    if private_key.is_encrypted() {
        return Err("SSH 登录密钥仍处于加密状态。".into());
    }
    let handler = PinnedRemoteCommandHandler {
        fingerprint: fingerprint.to_owned(),
    };
    let mut handle = tokio::time::timeout(
        Duration::from_secs(15),
        client::connect(Arc::new(client::Config::default()), (host, port), handler),
    )
    .await
    .map_err(|_| "验收报告 SSH 握手超时。".to_string())?
    .map_err(|_| "验收报告 SSH 指纹不匹配或连接失败。".to_string())?;
    let key = PrivateKeyWithHashAlg::new(Arc::new(private_key), Some(HashAlg::Sha256));
    let auth = tokio::time::timeout(
        Duration::from_secs(15),
        handle.authenticate_publickey(username.to_owned(), key),
    )
    .await
    .map_err(|_| "验收报告 SSH 公钥认证超时。".to_string())?
    .map_err(|_| "验收报告 SSH 公钥认证失败。".to_string())?;
    if !matches!(auth, AuthResult::Success) {
        let _ = handle
            .disconnect(
                Disconnect::ByApplication,
                "report read authentication failed",
                "",
            )
            .await;
        return Err("验收报告读取未通过 SSH 公钥认证。".into());
    }
    let mut channel = tokio::time::timeout(Duration::from_secs(15), handle.channel_open_session())
        .await
        .map_err(|_| "验收报告 SSH 通道开启超时。".to_string())?
        .map_err(|_| "验收报告 SSH 只读通道无法开启。".to_string())?;
    tokio::time::timeout(
        Duration::from_secs(15),
        channel.exec(true, command.into_bytes()),
    )
    .await
    .map_err(|_| "验收报告读取命令未及时接纳。".to_string())?
    .map_err(|_| "验收报告只读命令未被主机接纳。".to_string())?;

    let mut stdout = Vec::new();
    let mut stderr = Vec::new();
    let mut exit_status = None;
    // HOST_INSPECTION_TIMEOUT_SECS（远端主机预检上限，秒）：cleanup 下有多轮候选和报告时，有限目录扫描可能超过原 30 秒。
    let deadline = Instant::now() + Duration::from_secs(120);
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        match tokio::time::timeout(remaining, channel.wait()).await {
            Err(_) | Ok(None) | Ok(Some(ChannelMsg::Close)) => break,
            Ok(Some(ChannelMsg::Data { data })) => stdout.extend_from_slice(&data),
            Ok(Some(ChannelMsg::ExtendedData { data, ext: 1 })) => stderr.extend_from_slice(&data),
            Ok(Some(ChannelMsg::ExitStatus { exit_status: value })) => exit_status = Some(value),
            Ok(Some(_)) => {}
        }
        if stdout.len().saturating_add(stderr.len()) > 64 * 1024 {
            break;
        }
    }
    let _ = channel.close().await;
    let _ = handle
        .disconnect(Disconnect::ByApplication, "report read complete", "")
        .await;
    let exit_status = exit_status.ok_or_else(|| {
        format!(
            "远端验收报告读取未返回退出状态：{}",
            String::from_utf8_lossy(&stderr).trim()
        )
    })?;
    if exit_status != 0 {
        return Err(format!(
            "远端验收报告读取失败（退出码 {exit_status}）：{}",
            String::from_utf8_lossy(&stderr).trim()
        ));
    }
    let encoded_report = String::from_utf8_lossy(&stdout).trim().to_owned();
    let report_bytes = base64::engine::general_purpose::STANDARD
        .decode(encoded_report)
        .map_err(|_| "远端报告 Base64（文本编码）格式无效。".to_string())?;
    String::from_utf8(report_bytes).map_err(|_| "远端验收报告不是 UTF-8 编码。".into())
}

/// inspect_remote_client_host（远端主机只读预检）：不启动进程或写入主机，仅读取服务、交互 shell 与 cleanup 候选状态。
async fn inspect_remote_client_host(
    host: &str,
    port: u16,
    username: &str,
    transport_ref: &str,
    fingerprint: &str,
) -> Result<String, String> {
    if host != ACCEPTANCE_HOST
        || port != 22
        || username != ACCEPTANCE_USER
        || fingerprint != ACCEPTANCE_FINGERPRINT
    {
        return Err("主机预检仅允许访问本计划登记的固定主机、账户及指纹。".into());
    }
    if !transport_ref.starts_with("VoiceTerminal/ssh-transport/")
        || transport_ref.len() != "VoiceTerminal/ssh-transport/".len() + 64
        || !transport_ref
            .strip_prefix("VoiceTerminal/ssh-transport/")
            .unwrap_or_default()
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err("SSH 登录凭据引用格式无效。".into());
    }

    let template = r#"
$ErrorActionPreference = 'Stop'
$cleanupRoot = '__ROOT__'
$testRoot = 'D:\AI\example-test'
$sshd = Get-Service -Name 'sshd' -ErrorAction SilentlyContinue
$shells = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='explorer.exe'" -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty SessionId -Unique | Where-Object { $_ -gt 0 })
$candidateProcesses = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='VoiceTerminal.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($cleanupRoot, [StringComparison]::OrdinalIgnoreCase) })
$candidateDesktopProcesses = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='VoiceTerminalDesktop.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($cleanupRoot, [StringComparison]::OrdinalIgnoreCase) })
$serverPayloads = @(Get-ChildItem -LiteralPath (Join-Path $cleanupRoot 'fixtures') -Directory -Filter 'server-payload-*' -ErrorAction SilentlyContinue |
    Sort-Object Name | Select-Object -First 16 | ForEach-Object {
        $top = @(Get-ChildItem -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue |
            Sort-Object Name | Select-Object -First 32 | ForEach-Object {
                [ordered]@{ name = $_.Name; is_directory = [bool]$_.PSIsContainer }
            })
        [ordered]@{ name = $_.Name; entrypoint_exists = (Test-Path -LiteralPath (Join-Path $_.FullName 'VoiceTerminal.exe') -PathType Leaf); top_level_entries = $top }
    })
$candidateWorkspaces = @(Get-ChildItem -LiteralPath (Join-Path $cleanupRoot 'fixtures') -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like 'pre-product-*' -or $_.Name -like 'server-before-*' -or $_.Name -like 'server-app-*' } |
    Sort-Object Name | Select-Object -First 12 | ForEach-Object {
        $top = @(Get-ChildItem -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue |
            Sort-Object Name | Select-Object -First 24 | ForEach-Object {
                [ordered]@{ name = $_.Name; is_directory = [bool]$_.PSIsContainer }
            })
        $pythonRoots = @((Join-Path $_.FullName 'runtimes\python'),
            (Join-Path $_.FullName 'app\runtimes\python'), (Join-Path $_.FullName 'server\runtimes\python'))
        $pythonRoot = $pythonRoots | Where-Object { Test-Path -LiteralPath (Join-Path $_ 'python.exe') -PathType Leaf } | Select-Object -First 1
        $pythonManifest = $null
        if ($pythonRoot -and (Test-Path -LiteralPath (Join-Path $pythonRoot 'runtime.json') -PathType Leaf)) {
            try {
                $value = [IO.File]::ReadAllText((Join-Path $pythonRoot 'runtime.json'), [Text.Encoding]::UTF8) | ConvertFrom-Json
                $pythonManifest = [ordered]@{ build = $value.build; manifest_sha256 = $value.manifest_sha256; component_state = $value.state }
            } catch { $pythonManifest = [ordered]@{ state = 'invalid-or-unreadable' } }
        }
        [ordered]@{ name = $_.Name; top_level_entries = $top; python_runtime_path = if ($pythonRoot) { $pythonRoot.Substring($cleanupRoot.Length).TrimStart('\') } else { $null }; python_runtime = $pythonManifest }
    })
$serverPackageReports = @(Get-ChildItem -LiteralPath (Join-Path $cleanupRoot 'reports') -File -Filter 'server-package-product-*-verification-*.json' -ErrorAction SilentlyContinue |
    Sort-Object Name | Select-Object -First 16 | ForEach-Object {
        try {
            $value = [IO.File]::ReadAllText($_.FullName, [Text.Encoding]::UTF8) | ConvertFrom-Json
            $failureText = [string]$value.failure
            if ($failureText.Length -gt 400) { $failureText = $failureText.Substring(0, 400) }
            $stdoutExcerpt = ''
            $stderrExcerpt = ''
            if ($value.stdout_path) {
                $logPath = Join-Path $cleanupRoot ([string]$value.stdout_path)
                if (Test-Path -LiteralPath $logPath -PathType Leaf) { $stdoutExcerpt = [IO.File]::ReadAllText($logPath, [Text.Encoding]::UTF8) }
            }
            if ($value.stderr_path) {
                $logPath = Join-Path $cleanupRoot ([string]$value.stderr_path)
                if (Test-Path -LiteralPath $logPath -PathType Leaf) { $stderrExcerpt = [IO.File]::ReadAllText($logPath, [Text.Encoding]::UTF8) }
            }
            if ($stdoutExcerpt.Length -gt 1000) { $stdoutExcerpt = $stdoutExcerpt.Substring(0, 1000) }
            if ($stderrExcerpt.Length -gt 1000) { $stderrExcerpt = $stderrExcerpt.Substring(0, 1000) }
            [ordered]@{
                name = $_.Name
                build = $value.build
                verify_exit_code = $value.verify_exit_code
                result = $value.result
                failure = $failureText
                stdout_path = $value.stdout_path
                stderr_path = $value.stderr_path
                stdout_excerpt = $stdoutExcerpt
                stderr_excerpt = $stderrExcerpt
            }
        } catch { [ordered]@{ name = $_.Name; result = 'invalid-or-unreadable' } }
    })
$clientLaunchReports = @(Get-ChildItem -LiteralPath (Join-Path $cleanupRoot 'reports') -File -Filter 'client-workspace-launch-*.json' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 12 | ForEach-Object {
        try {
            $value = [IO.File]::ReadAllText($_.FullName, [Text.Encoding]::UTF8) | ConvertFrom-Json
            [ordered]@{
                name = $_.Name
                client_build = $value.client_build
                result = $value.result
                failure = if ($value.failure) { ([string]$value.failure).Substring(0, [Math]::Min(400, ([string]$value.failure).Length)) } else { $null }
                workspace_id = $value.workspace_id
                desktop_host_started = $value.desktop_host_started
                desktop_session_id = $value.desktop_session_id
                interactive_session = $value.interactive_session
                main_window_handle_present = $value.main_window_handle_present
                launcher_exit_code = $value.launcher_exit_code
                exit_request_exit_code = $value.exit_request_exit_code
                forced_stop_count = $value.forced_stop_count
                remaining_workspace_process_count = $value.remaining_workspace_process_count
            }
        } catch { [ordered]@{ name = $_.Name; result = 'invalid-or-unreadable' } }
    })
$cleanupEntries = @(Get-ChildItem -LiteralPath $cleanupRoot -Force -ErrorAction SilentlyContinue |
    Sort-Object Name | Select-Object -First 64 | ForEach-Object {
        [ordered]@{ name = $_.Name; is_directory = [bool]$_.PSIsContainer }
    })
$nestedEntries = @()
foreach ($parentName in @('fixtures', 'temp')) {
    $parentPath = Join-Path $cleanupRoot $parentName
    if (Test-Path -LiteralPath $parentPath -PathType Container) {
        $children = @(Get-ChildItem -LiteralPath $parentPath -Force -ErrorAction SilentlyContinue |
            Sort-Object Name | Select-Object -First 64 | ForEach-Object {
                [ordered]@{ name = $_.Name; is_directory = [bool]$_.PSIsContainer }
            })
        $nestedEntries += [ordered]@{ parent = $parentName; entries = $children }
    }
}
$testRootEntries = @(Get-ChildItem -LiteralPath $testRoot -Force -ErrorAction SilentlyContinue |
    Sort-Object Name | Select-Object -First 64 | ForEach-Object {
        [ordered]@{ name = $_.Name; is_directory = [bool]$_.PSIsContainer }
    })
$deviceConfigs = [System.Collections.ArrayList]::new()
$pending = [System.Collections.Generic.Queue[object]]::new()
$pending.Enqueue([PSCustomObject]@{ path = $cleanupRoot; depth = 0 })
$visitedDirectories = 0
$skipNames = @('app', 'logs', 'models', 'node_modules', 'reports', 'runtimes')
while ($pending.Count -gt 0 -and $visitedDirectories -lt 512 -and $deviceConfigs.Count -lt 32) {
    $entry = $pending.Dequeue()
    $visitedDirectories++
    $configPath = Join-Path $entry.path 'config\device-runtime.json'
    if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        try {
            $file = Get-Item -LiteralPath $configPath -Force
            if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0 -and $file.Length -le 65536) {
                $config = [IO.File]::ReadAllText($configPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
                $relative = $entry.path.Substring($cleanupRoot.Length).TrimStart('\')
                [void]$deviceConfigs.Add([ordered]@{
                    data_root = $relative
                    config_state = 'parsed'
                    schema_version = $config.schema_version
                    desired_role = $config.desired_role
                    host_present = ($null -ne $config.host)
                    transport_credential_present = ($null -ne $config.host -and $null -ne $config.host.transport_credential_ref)
                })
            }
        } catch {
            $relative = $entry.path.Substring($cleanupRoot.Length).TrimStart('\')
            [void]$deviceConfigs.Add([ordered]@{ data_root = $relative; config_state = 'invalid-or-unreadable' })
        }
    }
    if ($entry.depth -lt 5) {
        $children = @(Get-ChildItem -LiteralPath $entry.path -Directory -Force -ErrorAction SilentlyContinue |
            Where-Object { ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0 -and $skipNames -notcontains $_.Name } |
            Sort-Object Name)
        foreach ($child in $children) {
            $pending.Enqueue([PSCustomObject]@{ path = $child.FullName; depth = $entry.depth + 1 })
        }
    }
}
$record = [ordered]@{
    computer = $env:COMPUTERNAME
    sshd_service_state = if ($sshd) { $sshd.Status.ToString() } else { 'not-visible' }
    explorer_session_ids = @($shells)
    test_root_entries = $testRootEntries
    cleanup_root_exists = Test-Path -LiteralPath $cleanupRoot -PathType Container
    cleanup_entries = $cleanupEntries
    cleanup_nested_entries = $nestedEntries
    device_runtime_configs = @($deviceConfigs.ToArray())
    config_scan_truncated = ($pending.Count -gt 0 -or $visitedDirectories -ge 512 -or $deviceConfigs.Count -ge 32)
    candidate_process_count = $candidateProcesses.Count
    candidate_session_ids = @($candidateProcesses | Select-Object -ExpandProperty SessionId -Unique)
    candidate_desktop_process_count = $candidateDesktopProcesses.Count
    candidate_desktop_session_ids = @($candidateDesktopProcesses | Select-Object -ExpandProperty SessionId -Unique)
    server_payloads = $serverPayloads
    candidate_workspaces = $candidateWorkspaces
    server_package_reports = $serverPackageReports
    client_workspace_launch_reports = $clientLaunchReports
    fields = [ordered]@{
        computer = '主机名'
        sshd_service_state = 'OpenSSH 服务状态；not-visible 表示服务不存在或当前账户不可见'
        explorer_session_ids = '当前 explorer shell 所在的非零 Windows 交互会话编号；不包含用户名'
        test_root_entries = '获准 D:\AI\example-test 根目录最多 64 个直接子项名称与类型；仅读取名称，不读取内容'
        cleanup_root_exists = '获准远端 cleanup 目录是否存在'
        cleanup_entries = '获准 cleanup 根目录的最多 64 个直接子项名称与类型；仅读取名称，不读取内容'
        cleanup_nested_entries = 'fixtures 和 temp 子目录的最多 64 个直接子项名称与类型；仅读取名称，不读取内容'
        device_runtime_configs = 'cleanup 内最多 32 个 device-runtime.json 的有限只读扫描结果；输出相对数据目录、角色和是否配置独立 SSH 凭据，不输出编号/地址/凭据引用'
        config_scan_truncated = '有限目录扫描是否达到目录或配置条目上限'
        candidate_process_count = 'cleanup 目录内 VoiceTerminal.exe 进程数量'
        candidate_session_ids = 'cleanup 候选进程所在会话编号'
        candidate_desktop_process_count = 'cleanup 目录内 VoiceTerminalDesktop.exe 进程数量'
        candidate_desktop_session_ids = 'cleanup 桌面宿主进程所在会话编号'
        server_payloads = 'cleanup 内 server-payload-* 隔离解包目录名称、正式入口是否存在及最多 32 个直接子项；不读取文件内容'
        candidate_workspaces = 'cleanup 内旧验收工作区最多 12 个目录的顶层名称及应用管理的 Python 运行组件登记；不读取用户数据'
        server_package_reports = 'cleanup/reports 内正式程序包校验报告的文件名、构建号、退出码及结果；不返回日志内容'
        client_workspace_launch_reports = 'cleanup/reports 内轻量客户端启动尝试的有限摘要；不返回日志或凭据'
    }
}
$json = $record | ConvertTo-Json -Depth 4 -Compress
[Console]::WriteLine([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))
"#;
    let script = template.replace("__ROOT__", ACCEPTANCE_CLEANUP_ROOT);
    let encoded = base64::engine::general_purpose::STANDARD.encode(
        script
            .encode_utf16()
            .flat_map(u16::to_le_bytes)
            .collect::<Vec<_>>(),
    );
    let command = format!(
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand {encoded}"
    );

    let private_key = voice_desktop_common::credentials::load_device_ssh_key(transport_ref)?;
    let private_key = PrivateKey::from_openssh(private_key.as_str())
        .map_err(|_| "本机 Windows 凭据库中的 SSH 登录密钥无法解析。".to_string())?;
    if private_key.is_encrypted() {
        return Err("SSH 登录密钥仍处于加密状态。".into());
    }
    let handler = PinnedRemoteCommandHandler {
        fingerprint: fingerprint.to_owned(),
    };
    let mut handle = tokio::time::timeout(
        Duration::from_secs(15),
        client::connect(Arc::new(client::Config::default()), (host, port), handler),
    )
    .await
    .map_err(|_| "主机预检 SSH 握手超时。".to_string())?
    .map_err(|_| "主机预检 SSH 指纹不匹配或连接失败。".to_string())?;
    let key = PrivateKeyWithHashAlg::new(Arc::new(private_key), Some(HashAlg::Sha256));
    let auth = tokio::time::timeout(
        Duration::from_secs(15),
        handle.authenticate_publickey(username.to_owned(), key),
    )
    .await
    .map_err(|_| "主机预检 SSH 公钥认证超时。".to_string())?
    .map_err(|_| "主机预检 SSH 公钥认证失败。".to_string())?;
    if !matches!(auth, AuthResult::Success) {
        let _ = handle
            .disconnect(
                Disconnect::ByApplication,
                "host inspection authentication failed",
                "",
            )
            .await;
        return Err("主机预检未通过 SSH 公钥认证。".into());
    }
    let mut channel = tokio::time::timeout(Duration::from_secs(15), handle.channel_open_session())
        .await
        .map_err(|_| "主机预检 SSH 通道开启超时。".to_string())?
        .map_err(|_| "主机预检 SSH 只读通道无法开启。".to_string())?;
    tokio::time::timeout(
        Duration::from_secs(15),
        channel.exec(true, command.into_bytes()),
    )
    .await
    .map_err(|_| "主机预检只读命令未及时接纳。".to_string())?
    .map_err(|_| "主机未接纳只读预检命令。".to_string())?;

    let mut stdout = Vec::new();
    let mut stderr = Vec::new();
    let mut exit_status = None;
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        match tokio::time::timeout(remaining, channel.wait()).await {
            Err(_) | Ok(None) | Ok(Some(ChannelMsg::Close)) => break,
            Ok(Some(ChannelMsg::Data { data })) => stdout.extend_from_slice(&data),
            Ok(Some(ChannelMsg::ExtendedData { data, ext: 1 })) => stderr.extend_from_slice(&data),
            Ok(Some(ChannelMsg::ExitStatus { exit_status: value })) => exit_status = Some(value),
            Ok(Some(_)) => {}
        }
        // HOST_INSPECTION_OUTPUT_LIMIT_BYTES（主机只读预检输出上限，字节）：有较多历史报告时容纳截断后的摘要列表。
        if stdout.len().saturating_add(stderr.len()) > 64 * 1024 {
            break;
        }
    }
    let _ = channel.close().await;
    let _ = handle
        .disconnect(Disconnect::ByApplication, "host inspection complete", "")
        .await;
    let exit_status = exit_status.ok_or_else(|| {
        format!(
            "主机预检未返回退出状态：{}",
            String::from_utf8_lossy(&stderr).trim()
        )
    })?;
    if exit_status != 0 {
        return Err(format!(
            "主机预检失败（退出码 {exit_status}）：{}",
            String::from_utf8_lossy(&stderr).trim()
        ));
    }
    let encoded_report = String::from_utf8(stdout)
        .map_err(|_| "主机预检 Base64 结果不是 UTF-8 编码。".to_string())?;
    let report_bytes = base64::engine::general_purpose::STANDARD
        .decode(encoded_report.trim())
        .map_err(|_| "主机预检 Base64 结果格式无效。".to_string())?;
    String::from_utf8(report_bytes)
        .map(|value| value.trim().to_owned())
        .map_err(|_| "主机预检结果不是 UTF-8 编码。".into())
}

/// inspect_remote_device_config（远端正式加载器检查）：运行 cleanup 中的本机同版本验收辅助程序。
async fn inspect_remote_device_config(
    host: &str,
    port: u16,
    username: &str,
    transport_ref: &str,
    fingerprint: &str,
    data_root: &str,
) -> Result<String, String> {
    if host != ACCEPTANCE_HOST
        || port != 22
        || username != ACCEPTANCE_USER
        || fingerprint != ACCEPTANCE_FINGERPRINT
    {
        return Err("远端设备配置检查仅允许本计划固定的主机、账户及指纹。".into());
    }
    if !transport_ref.starts_with("VoiceTerminal/ssh-transport/")
        || transport_ref.len() != "VoiceTerminal/ssh-transport/".len() + 64
        || !transport_ref
            .strip_prefix("VoiceTerminal/ssh-transport/")
            .unwrap_or_default()
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err("SSH 登录凭据引用格式无效。".into());
    }
    let root = ACCEPTANCE_CLEANUP_ROOT.trim_end_matches('\\');
    let lower_root = root.to_ascii_lowercase();
    let lower_data_root = data_root.to_ascii_lowercase();
    if !lower_data_root.starts_with(&(lower_root + "\\"))
        || data_root
            .split(['\\', '/'])
            .any(|part| matches!(part, "." | ".."))
        || data_root.chars().any(|character| {
            character == '\'' || character == '`' || character == ';' || character == '|'
        })
    {
        return Err("远端配置数据目录必须位于获准 cleanup 内；路径格式无效时拒绝执行。".into());
    }

    let escaped_data_root = data_root.replace('\'', "''");
    let template = r#"
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = '__ROOT__'
$helper = Join-Path $root 'temp\device-product-ssh-check-r2.exe'
$dataRoot = '__DATA_ROOT__'
if (!(Test-Path -LiteralPath $helper -PathType Leaf)) { [Console]::Error.WriteLine('ACCEPTANCE_HELPER_NOT_FOUND'); exit 3 }
$result = & $helper inspect-device-config-base64 $dataRoot
if ($LASTEXITCODE -ne 0) { [Console]::Error.WriteLine('DEVICE_CONFIG_INSPECTION_FAILED'); exit $LASTEXITCODE }
$encodedJson = ($result -join '').Trim()
$payload = [Convert]::FromBase64String($encodedJson)
[Console]::WriteLine([Convert]::ToBase64String($payload))
exit 0
"#;
    let script = template
        .replace("__ROOT__", ACCEPTANCE_CLEANUP_ROOT)
        .replace("__DATA_ROOT__", &escaped_data_root);
    let encoded = base64::engine::general_purpose::STANDARD.encode(
        script
            .encode_utf16()
            .flat_map(u16::to_le_bytes)
            .collect::<Vec<_>>(),
    );
    let command = format!(
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand {encoded}"
    );

    let private_key = voice_desktop_common::credentials::load_device_ssh_key(transport_ref)?;
    let private_key = PrivateKey::from_openssh(private_key.as_str())
        .map_err(|_| "本机 Windows 凭据库中的 SSH 登录密钥无法解析。".to_string())?;
    if private_key.is_encrypted() {
        return Err("SSH 登录密钥仍处于加密状态。".into());
    }
    let handler = PinnedRemoteCommandHandler {
        fingerprint: fingerprint.to_owned(),
    };
    let mut handle = tokio::time::timeout(
        Duration::from_secs(15),
        client::connect(Arc::new(client::Config::default()), (host, port), handler),
    )
    .await
    .map_err(|_| "远端设备配置检查 SSH 握手超时。".to_string())?
    .map_err(|_| "远端设备配置检查 SSH 指纹不匹配或连接失败。".to_string())?;
    let key = PrivateKeyWithHashAlg::new(Arc::new(private_key), Some(HashAlg::Sha256));
    let auth = tokio::time::timeout(
        Duration::from_secs(15),
        handle.authenticate_publickey(username.to_owned(), key),
    )
    .await
    .map_err(|_| "远端设备配置检查 SSH 公钥认证超时。".to_string())?
    .map_err(|_| "远端设备配置检查 SSH 公钥认证失败。".to_string())?;
    if !matches!(auth, AuthResult::Success) {
        let _ = handle
            .disconnect(
                Disconnect::ByApplication,
                "device config inspection authentication failed",
                "",
            )
            .await;
        return Err("远端设备配置检查未通过固定账户 SSH 公钥认证。".into());
    }
    let mut channel = tokio::time::timeout(Duration::from_secs(15), handle.channel_open_session())
        .await
        .map_err(|_| "远端设备配置检查 SSH 通道开启超时。".to_string())?
        .map_err(|_| "远端设备配置检查 SSH 通道无法开启。".to_string())?;
    tokio::time::timeout(
        Duration::from_secs(15),
        channel.exec(true, command.into_bytes()),
    )
    .await
    .map_err(|_| "远端设备配置检查命令未及时接纳。".to_string())?
    .map_err(|_| "主机未接纳远端设备配置检查命令。".to_string())?;

    let mut stdout = Vec::new();
    let mut stderr = Vec::new();
    let mut exit_status = None;
    let deadline = Instant::now() + Duration::from_secs(45);
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        match tokio::time::timeout(remaining, channel.wait()).await {
            Err(_) | Ok(None) | Ok(Some(ChannelMsg::Close)) => break,
            Ok(Some(ChannelMsg::Data { data })) => stdout.extend_from_slice(&data),
            Ok(Some(ChannelMsg::ExtendedData { data, ext: 1 })) => stderr.extend_from_slice(&data),
            Ok(Some(ChannelMsg::ExitStatus { exit_status: value })) => exit_status = Some(value),
            Ok(Some(_)) => {}
        }
        if stdout.len().saturating_add(stderr.len()) > 16 * 1024 {
            break;
        }
    }
    let _ = channel.close().await;
    let _ = handle
        .disconnect(
            Disconnect::ByApplication,
            "device config inspection complete",
            "",
        )
        .await;
    let exit_status = exit_status.ok_or_else(|| {
        format!(
            "远端设备配置检查未返回退出状态：{}",
            String::from_utf8_lossy(&stderr).trim()
        )
    })?;
    if exit_status != 0 {
        return Err(format!(
            "远端设备配置检查失败（退出码 {exit_status}）：{}",
            String::from_utf8_lossy(&stderr).trim()
        ));
    }
    let encoded = String::from_utf8(stdout)
        .map_err(|_| "远端设备配置检查结果不是 UTF-8 编码。".to_string())?;
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(encoded.trim())
        .map_err(|_| "远端设备配置检查 Base64 结果格式无效。".to_string())?;
    String::from_utf8(bytes)
        .map(|value| value.trim().to_owned())
        .map_err(|_| "远端设备配置检查结果不是 UTF-8 编码。".into())
}

/// verify_remote_program_package（远端统一程序包校验）：仅解包到 cleanup 并运行正式只读校验入口。
async fn verify_remote_program_package(
    host: &str,
    port: u16,
    username: &str,
    transport_ref: &str,
    fingerprint: &str,
    build: &str,
    archive_sha256: &str,
    attempt_id: &str,
) -> Result<String, String> {
    if host != ACCEPTANCE_HOST
        || port != 22
        || username != ACCEPTANCE_USER
        || fingerprint != ACCEPTANCE_FINGERPRINT
    {
        return Err("正式程序包校验仅允许本计划固定的主机、账户、端口及指纹。".into());
    }
    let Some(build_hash) = build.strip_prefix("product-") else {
        return Err("正式程序包构建编号格式无效。".into());
    };
    if build_hash.len() != 16
        || !build_hash
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        || archive_sha256.len() != 64
        || !archive_sha256
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        || attempt_id.is_empty()
        || attempt_id.len() > 32
        || attempt_id.starts_with('-')
        || attempt_id.ends_with('-')
        || !attempt_id
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
    {
        return Err("正式程序包摘要或验收编号格式无效。".into());
    }
    if !transport_ref.starts_with("VoiceTerminal/ssh-transport/")
        || transport_ref.len() != "VoiceTerminal/ssh-transport/".len() + 64
        || !transport_ref
            .strip_prefix("VoiceTerminal/ssh-transport/")
            .unwrap_or_default()
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err("SSH 登录凭据引用格式无效。".into());
    }

    let template = r#"
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = '__ROOT__'
$build = '__BUILD__'
$expectedSha = '__SHA256__'
$attempt = '__ATTEMPT__'
$archive = Join-Path $root ('fixtures\server-program-package-' + $build + '.zip')
$destination = Join-Path $root ('fixtures\server-payload-' + $build + '-' + $attempt)
$reportPath = Join-Path $root ('reports\server-package-' + $build + '-verification-' + $attempt + '.json')
$stdoutPath = Join-Path $root ('reports\server-package-' + $build + '-verification-' + $attempt + '-stdout.log')
$stderrPath = Join-Path $root ('reports\server-package-' + $build + '-verification-' + $attempt + '-stderr.log')
foreach ($path in @($reportPath, $stdoutPath, $stderrPath, $destination)) {
    if (Test-Path -LiteralPath $path) { throw '验收目标已存在；未覆盖既有文件或目录。' }
}
if (!(Test-Path -LiteralPath $archive -PathType Leaf)) { throw 'cleanup 内的正式程序包归档不存在。' }
$archiveInfo = Get-Item -LiteralPath $archive -Force
if (($archiveInfo.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $archiveInfo.Length -gt 536870912) {
    throw '程序包类型或压缩大小超过 512 MiB 上限。'
}
$actualSha = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualSha -ne $expectedSha) { throw '程序包摘要与本机已核验文件传输回执不一致。' }
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::OpenRead($archive)
try {
    $expandedBytes = [long]0
    $hasEntryPoint = $false
    foreach ($entry in $zip.Entries) {
        $name = $entry.FullName.Replace('\', '/')
        if ($name.StartsWith('/') -or $name -match '(^|/)\.\.(/|$)' -or $name -match '^[A-Za-z]:') {
            throw '程序包包含越界路径；未解包。'
        }
        $expandedBytes += [long]$entry.Length
        if ($name -eq 'VoiceTerminal.exe') { $hasEntryPoint = $true }
    }
    if (!$hasEntryPoint) { throw '程序包根目录缺少正式 VoiceTerminal.exe 入口。' }
} finally { $zip.Dispose() }
$drive = Get-PSDrive -Name ([IO.Path]::GetPathRoot($root).Substring(0,1))
$freeBytes = [long]$drive.Free
if ($freeBytes -lt ($expandedBytes + 268435456)) { throw 'D 盘剩余空间不足以安全解包并保留 256 MiB 余量。' }
Expand-Archive -LiteralPath $archive -DestinationPath $destination | Out-Null
$entryPoint = Join-Path $destination 'VoiceTerminal.exe'
$verifyExitCode = -1
$failure = $null
try {
    $process = Start-Process -FilePath $entryPoint -ArgumentList '--verify-package' -WorkingDirectory $destination `
        -WindowStyle Hidden -PassThru -Wait -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -ErrorAction Stop
    $verifyExitCode = [int]$process.ExitCode
} catch { $failure = $_.Exception.Message }
$result = if ($verifyExitCode -eq 0) { 'passed' } else { 'failed' }
if ($result -eq 'failed' -and !$failure) { $failure = '正式入口报告非零退出码。' }
$record = [ordered]@{
    schema = 1
    build = $build
    archive_path = $archive.Substring($root.Length).TrimStart('\')
    archive_bytes = $archiveInfo.Length
    archive_sha256 = $actualSha
    expanded_bytes = $expandedBytes
    free_bytes_before = $freeBytes
    extraction_path = $destination.Substring($root.Length).TrimStart('\')
    verify_exit_code = $verifyExitCode
    stdout_path = $stdoutPath.Substring($root.Length).TrimStart('\')
    stderr_path = $stderrPath.Substring($root.Length).TrimStart('\')
    failure = $failure
    result = $result
    fields = [ordered]@{
        schema = '远端正式程序包验证报告版本'
        build = '本机固定程序包构建编号'
        archive_path = '获准 cleanup 内的归档相对路径'
        archive_bytes = '固定摘要校验的压缩包字节数'
        archive_sha256 = '远端重算的压缩包 SHA-256 摘要'
        expanded_bytes = '归档目录表计算的解包总字节数'
        free_bytes_before = '解包前 D 盘剩余空间字节数'
        extraction_path = 'cleanup 内的唯一解包目录'
        verify_exit_code = 'VoiceTerminal.exe --verify-package 退出码；0 表示通过'
        stdout_path = 'cleanup 内保留的正式包校验标准输出日志'
        stderr_path = 'cleanup 内保留的正式包校验错误输出日志'
        failure = '校验失败原因；成功时为 null'
        result = '包摘要、隔离解包和正式入口检查结果'
    }
}
$json = $record | ConvertTo-Json -Depth 6 -Compress
[IO.File]::WriteAllText($reportPath, $json, [Text.UTF8Encoding]::new($false))
[Console]::WriteLine([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))
if ($result -ne 'passed') { exit 5 }
exit 0
"#;
    let script = template
        .replace("__ROOT__", ACCEPTANCE_CLEANUP_ROOT)
        .replace("__BUILD__", build)
        .replace("__SHA256__", archive_sha256)
        .replace("__ATTEMPT__", attempt_id);
    let encoded = base64::engine::general_purpose::STANDARD.encode(
        script
            .encode_utf16()
            .flat_map(u16::to_le_bytes)
            .collect::<Vec<_>>(),
    );
    let command = format!(
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand {encoded}"
    );

    let private_key = voice_desktop_common::credentials::load_device_ssh_key(transport_ref)?;
    let private_key = PrivateKey::from_openssh(private_key.as_str())
        .map_err(|_| "本机 Windows 凭据库中的 SSH 登录密钥无法解析。".to_string())?;
    if private_key.is_encrypted() {
        return Err("SSH 登录密钥仍处于加密状态。".into());
    }
    let handler = PinnedRemoteCommandHandler {
        fingerprint: fingerprint.to_owned(),
    };
    let mut handle = tokio::time::timeout(
        Duration::from_secs(15),
        client::connect(Arc::new(client::Config::default()), (host, port), handler),
    )
    .await
    .map_err(|_| "正式程序包验收 SSH 握手超时。".to_string())?
    .map_err(|_| "正式程序包验收 SSH 指纹不匹配或连接失败。".to_string())?;
    let key = PrivateKeyWithHashAlg::new(Arc::new(private_key), Some(HashAlg::Sha256));
    let auth = tokio::time::timeout(
        Duration::from_secs(15),
        handle.authenticate_publickey(username.to_owned(), key),
    )
    .await
    .map_err(|_| "正式程序包验收 SSH 公钥认证超时。".to_string())?
    .map_err(|_| "正式程序包验收 SSH 公钥认证失败。".to_string())?;
    if !matches!(auth, AuthResult::Success) {
        let _ = handle
            .disconnect(
                Disconnect::ByApplication,
                "program package verification authentication failed",
                "",
            )
            .await;
        return Err("正式程序包验收未通过固定账户 SSH 公钥认证。".into());
    }
    let mut channel = tokio::time::timeout(Duration::from_secs(15), handle.channel_open_session())
        .await
        .map_err(|_| "正式程序包验收 SSH 通道开启超时。".to_string())?
        .map_err(|_| "正式程序包验收 SSH 通道无法开启。".to_string())?;
    tokio::time::timeout(
        Duration::from_secs(15),
        channel.exec(true, command.into_bytes()),
    )
    .await
    .map_err(|_| "正式程序包校验命令未及时接纳。".to_string())?
    .map_err(|_| "主机未接纳正式程序包校验命令。".to_string())?;

    let mut stdout = Vec::new();
    let mut stderr = Vec::new();
    let mut exit_status = None;
    let mut output_overflow = false;
    let deadline = Instant::now() + Duration::from_secs(240);
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        match tokio::time::timeout(remaining, channel.wait()).await {
            Err(_) | Ok(None) | Ok(Some(ChannelMsg::Close)) => break,
            Ok(Some(ChannelMsg::Data { data })) => {
                if !output_overflow
                    && stdout
                        .len()
                        .saturating_add(stderr.len())
                        .saturating_add(data.len())
                        <= 64 * 1024
                {
                    stdout.extend_from_slice(&data);
                } else {
                    output_overflow = true;
                }
            }
            Ok(Some(ChannelMsg::ExtendedData { data, ext: 1 })) => {
                if !output_overflow
                    && stdout
                        .len()
                        .saturating_add(stderr.len())
                        .saturating_add(data.len())
                        <= 64 * 1024
                {
                    stderr.extend_from_slice(&data);
                } else {
                    output_overflow = true;
                }
            }
            Ok(Some(ChannelMsg::ExitStatus { exit_status: value })) => exit_status = Some(value),
            Ok(Some(_)) => {}
        }
    }
    let _ = channel.close().await;
    let _ = handle
        .disconnect(
            Disconnect::ByApplication,
            "program package verification complete",
            "",
        )
        .await;
    if output_overflow {
        return Err("正式程序包验收远端输出超过 64 KiB；远端报告和材料保留。".into());
    }
    let exit_status = exit_status.ok_or_else(|| {
        format!(
            "正式程序包验收未返回退出状态：{}",
            String::from_utf8_lossy(&stderr).trim()
        )
    })?;
    let encoded_report = String::from_utf8(stdout)
        .map_err(|_| "正式程序包验收报告 Base64 结果不是 UTF-8 编码。".to_string())?;
    let report_bytes = base64::engine::general_purpose::STANDARD
        .decode(encoded_report.trim())
        .map_err(|_| "正式程序包验收报告 Base64 格式无效。".to_string())?;
    let report = String::from_utf8(report_bytes)
        .map_err(|_| "正式程序包验收报告不是 UTF-8 编码。".to_string())?;
    if exit_status != 0 {
        return Err(format!("远端正式包校验未通过；远端已保留报告：{report}"));
    }
    Ok(report)
}

/// launch_remote_client_workspace（从分发器展开的轻量客户端工作区启动原生桌面宿主）。
async fn launch_remote_client_workspace(
    host: &str,
    port: u16,
    username: &str,
    transport_ref: &str,
    fingerprint: &str,
    client_build: &str,
    attempt_id: &str,
) -> Result<String, String> {
    if host != ACCEPTANCE_HOST
        || port != 22
        || username != ACCEPTANCE_USER
        || fingerprint != ACCEPTANCE_FINGERPRINT
    {
        return Err("轻量客户端启动仅允许本计划固定的主机、账户、端口及指纹。".into());
    }
    let Some(client_suffix) = client_build.strip_prefix("client-") else {
        return Err("轻量客户端构建编号格式无效。".into());
    };
    let Some((source_hash, revision)) = client_suffix.rsplit_once("-r") else {
        return Err("轻量客户端修订号格式无效。".into());
    };
    if source_hash.len() != 16
        || !source_hash
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        || revision.is_empty()
        || revision.starts_with('0')
        || !revision.bytes().all(|byte| byte.is_ascii_digit())
        || attempt_id.is_empty()
        || attempt_id.len() > 32
        || attempt_id.starts_with('-')
        || attempt_id.ends_with('-')
        || !attempt_id
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
    {
        return Err("轻量客户端构建编号或启动尝试编号格式无效。".into());
    }
    let Some(transport_suffix) = transport_ref.strip_prefix("VoiceTerminal/ssh-transport/") else {
        return Err("SSH 登录凭据引用格式无效。".into());
    };
    if transport_suffix.len() != 64
        || !transport_suffix
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err("SSH 登录凭据引用格式无效。".into());
    }

    let template = r#"
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = '__ROOT__'
$clientBuild = '__CLIENT_BUILD__'
$attempt = '__ATTEMPT__'
$fixtures = Join-Path $root 'fixtures'
$reportPath = Join-Path $root ('reports\client-workspace-launch-' + $clientBuild + '-' + $attempt + '.json')
$launchStdout = Join-Path $root ('reports\client-workspace-launch-' + $clientBuild + '-' + $attempt + '-launcher.stdout.log')
$launchStderr = Join-Path $root ('reports\client-workspace-launch-' + $clientBuild + '-' + $attempt + '-launcher.stderr.log')
$exitStdout = Join-Path $root ('reports\client-workspace-launch-' + $clientBuild + '-' + $attempt + '-exit.stdout.log')
$exitStderr = Join-Path $root ('reports\client-workspace-launch-' + $clientBuild + '-' + $attempt + '-exit.stderr.log')
$fields = [ordered]@{
    schema = '客户端工作区启动报告格式'
    client_build = '轻量客户端构建编号'
    workspace_id = 'portable.json 内的独立验收工作区编号'
    workspace = '直接启动的 cleanup 内展开目录'
    client_profile = '应用清单声明的客户端职责'
    component_count = '所需、可选及已安装的模型/DSH/Python 组件数'
    python_or_model_or_dsh_present = '工作区是否意外带有重量组件'
    system_webview2_present = '系统已有网页引擎是否可用；本次不安装'
    launcher_exit_code = '正式 VoiceTerminal.exe 根入口移交退出码'
    desktop_host_started = 'VoiceTerminalDesktop.exe 是否从该工作区启动'
    desktop_session_id = '原生桌面宿主所在 Windows 会话编号'
    interactive_session = '宿主是否处于非零交互会话'
    main_window_handle_present = '系统是否报告主窗口句柄；不代表可见 UI 已验收'
    python_process_count = '本次客户端工作区内启动的 Python 进程数'
    exit_request_exit_code = '正式 --exit 退出请求入口结果'
    forced_stop_count = '仅对本工作区进程执行强制回收的数量'
    remaining_workspace_process_count = '关闭后仍运行在本工作区下的进程数'
    result = '轻量工作区启动与关闭结果；partial（部分完成）表示 GUI（图形界面）尚未在交互桌面验收'
    failure = '启动/关闭失败原因；成功时为 null'
}
$record = $null
$failure = $null
$workspace = $null
$launchExitCode = $null
$exitRequestExitCode = $null
$desktopHostStarted = $false
$desktopSessionId = $null
$interactiveSession = $false
$mainWindowHandlePresent = $false
$pythonProcessCount = 0
$forcedStopCount = 0
$remaining = @()
$workspaceId = $null
$clientProfile = $null
$componentCount = $null
$pythonOrModelOrDshPresent = $false
$webview2Present = $false

function Get-WorkspaceProcesses([string]$path) {
    @(Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($path, [StringComparison]::OrdinalIgnoreCase) })
}

try {
    foreach ($path in @('D:\AI', 'D:\AI\example-test', 'D:\AI\example-test\VoiceTerminal-DeviceValidation-20261115', $root, $fixtures, (Join-Path $root 'reports'))) {
        $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw '验收目录包含重解析点；未启动客户端。' }
    }
    foreach ($path in @($reportPath, $launchStdout, $launchStderr, $exitStdout, $exitStderr)) {
        if (Test-Path -LiteralPath $path) { throw '本次客户端启动报告或日志路径已存在；拒绝覆盖。' }
    }
    $prefix = 'client-payload-' + $clientBuild + '-'
    $workspaces = @(Get-ChildItem -LiteralPath $fixtures -Directory -Filter ($prefix + '*') -ErrorAction SilentlyContinue |
        Where-Object { (Test-Path -LiteralPath (Join-Path $_.FullName 'VoiceTerminal.exe') -PathType Leaf) -and
            (Test-Path -LiteralPath (Join-Path $_.FullName 'portable.json') -PathType Leaf) -and
            (Test-Path -LiteralPath (Join-Path $_.FullName 'app\release.json') -PathType Leaf) })
    if ($workspaces.Count -ne 1) { throw '当前客户端构建必须有且仅有一个已展开并带 portable.json 的工作区。' }
    $workspace = $workspaces[0].FullName
    $workspaceItem = Get-Item -LiteralPath $workspace -Force
    if (($workspaceItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw '客户端工作区是重解析点；拒绝启动。' }

    $portable = [IO.File]::ReadAllText((Join-Path $workspace 'portable.json'), [Text.Encoding]::UTF8) | ConvertFrom-Json -ErrorAction Stop
    $release = [IO.File]::ReadAllText((Join-Path $workspace 'app\release.json'), [Text.Encoding]::UTF8) | ConvertFrom-Json -ErrorAction Stop
    if ($portable.schema -ne 2 -or $portable.app -ne 'app' -or $portable.data_dir -ne 'data' -or
        $portable.workspace_id -notmatch '^device-client-[0-9a-f]{32}$' -or
        $release.build -ne $clientBuild -or $release.client_profile -ne 'remote_device_client' -or
        @($release.required_components).Count -ne 0 -or @($release.optional_components).Count -ne 0 -or
        @($release.components).Count -ne 0) {
        throw '轻量客户端清单或工作区身份与候选不匹配。'
    }
    $catalog = [IO.File]::ReadAllText((Join-Path $workspace 'app\config\resources\catalog.json'), [Text.Encoding]::UTF8) | ConvertFrom-Json -ErrorAction Stop
    if (@($catalog.resources).Count -ne 0) { throw '轻量客户端资源目录仍登记模型或 DSH 组件。' }
    $pythonPath = Join-Path $workspace 'runtimes\python\python.exe'
    $modelsPath = Join-Path $workspace 'models'
    $dshPath = Join-Path $workspace 'runtimes\dsh'
    $pythonOrModelOrDshPresent = (Test-Path -LiteralPath $pythonPath) -or (Test-Path -LiteralPath $modelsPath) -or (Test-Path -LiteralPath $dshPath)
    if ($pythonOrModelOrDshPresent) { throw '轻量客户端工作区含有 Python、模型或 DSH 运行组件；拒绝继续。' }
    $workspaceId = [string]$portable.workspace_id
    $clientProfile = [string]$release.client_profile
    $componentCount = @($release.required_components).Count + @($release.optional_components).Count + @($release.components).Count

    $webviewRoots = @()
    $programFilesX86 = [Environment]::GetFolderPath('ProgramFilesX86')
    if ($programFilesX86) { $webviewRoots += Join-Path $programFilesX86 'Microsoft\EdgeWebView\Application' }
    $programFiles = [Environment]::GetFolderPath('ProgramFiles')
    if ($programFiles) { $webviewRoots += Join-Path $programFiles 'Microsoft\EdgeWebView\Application' }
    $webview2Present = @($webviewRoots | Where-Object {
        (Test-Path -LiteralPath $_ -PathType Container) -and
        (Get-ChildItem -LiteralPath $_ -Directory -ErrorAction SilentlyContinue | Where-Object {
            Test-Path -LiteralPath (Join-Path $_.FullName 'msedgewebview2.exe') -PathType Leaf
        } | Select-Object -First 1)
    }).Count -gt 0
    if (!$webview2Present) { throw '目标主机未检测到系统 WebView2；客户端启动未尝试，不安装网页运行时。' }

    $dataRoot = Join-Path $workspace 'data'
    $deviceConfig = Join-Path $dataRoot 'config\device-runtime.json'
    if (Test-Path -LiteralPath $deviceConfig) { throw '当前客户端验收工作区已有设备角色配置；保留原件，拒绝覆盖。' }
    $beforeProcesses = Get-WorkspaceProcesses $workspace
    if ($beforeProcesses.Count -gt 0) { throw '当前客户端工作区已有进程；不并行启动。' }

    $entry = Join-Path $workspace 'VoiceTerminal.exe'
    $launcher = Start-Process -FilePath $entry -WorkingDirectory $workspace -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $launchStdout -RedirectStandardError $launchStderr -ErrorAction Stop
    if (!$launcher.WaitForExit(30000)) { throw '正式客户端根入口未在 30 秒内完成宿主移交。' }
    $launchExitCode = [int]$launcher.ExitCode
    if ($launchExitCode -ne 0) { throw "正式客户端根入口退出码为 $launchExitCode。" }

    $desktop = @()
    for ($index = 0; $index -lt 40; $index++) {
        $desktop = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='VoiceTerminalDesktop.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($workspace, [StringComparison]::OrdinalIgnoreCase) })
        if ($desktop.Count -gt 0) { break }
        Start-Sleep -Milliseconds 500
    }
    if ($desktop.Count -ne 1) { throw '轻量客户端未建立唯一的原生桌面宿主进程。' }
    $desktopHostStarted = $true
    $desktopSessionId = [int]$desktop[0].SessionId
    $interactiveSession = ($desktopSessionId -gt 0)
    try {
        $desktopProcess = Get-Process -Id ([int]$desktop[0].ProcessId) -ErrorAction Stop
        $mainWindowHandlePresent = ($desktopProcess.MainWindowHandle -ne [IntPtr]::Zero)
    } catch {}
    Start-Sleep -Seconds 3
    $pythonProcessCount = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($workspace, [StringComparison]::OrdinalIgnoreCase) }).Count

    $exitRequest = Start-Process -FilePath $entry -ArgumentList '--exit' -WorkingDirectory $workspace -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $exitStdout -RedirectStandardError $exitStderr -ErrorAction Stop
    if (!$exitRequest.WaitForExit(30000)) { throw '正式 --exit 请求入口未在 30 秒内完成。' }
    $exitRequestExitCode = [int]$exitRequest.ExitCode
    if ($exitRequestExitCode -ne 0) { throw "正式 --exit 请求入口退出码为 $exitRequestExitCode。" }
    $exitDeadline = [DateTime]::UtcNow.AddSeconds(45)
    do {
        Start-Sleep -Milliseconds 500
        $remaining = Get-WorkspaceProcesses $workspace
    } while ($remaining.Count -gt 0 -and [DateTime]::UtcNow -lt $exitDeadline)
    if ($remaining.Count -gt 0) { throw '客户端收到正式退出请求后未能关闭工作进程。' }
} catch {
    $failure = $_.Exception.Message
} finally {
    if ($workspace -and (Test-Path -LiteralPath $workspace -PathType Container)) {
        $remaining = Get-WorkspaceProcesses $workspace
        foreach ($owned in $remaining) { try { Stop-Process -Id ([int]$owned.ProcessId) -Force -ErrorAction Stop; $forcedStopCount++ } catch {} }
        Start-Sleep -Milliseconds 250
        $remaining = Get-WorkspaceProcesses $workspace
        $pythonProcessCount = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($workspace, [StringComparison]::OrdinalIgnoreCase) }).Count
    }
    $result = 'failed'
    if (!$failure -and $desktopHostStarted -and $launchExitCode -eq 0 -and $exitRequestExitCode -eq 0 -and
        $interactiveSession -and $mainWindowHandlePresent -and $forcedStopCount -eq 0 -and
        $remaining.Count -eq 0 -and $pythonProcessCount -eq 0) {
        $result = 'passed'
    } elseif ($desktopHostStarted -and $launchExitCode -eq 0) {
        $result = 'partial'
    }
    if ($result -eq 'partial' -and !$failure) {
        if (!$interactiveSession) {
            $failure = '原生桌面宿主由 SSH Session 0 启动，非交互会话没有可见窗口；本结果只证明轻量入口已启动。'
        } elseif (!$mainWindowHandlePresent) {
            $failure = '原生桌面宿主没有主窗口句柄；不能作为可见桌面验收。'
        } elseif ($forcedStopCount -gt 0) {
            $failure = "官方退出请求后仍有 $forcedStopCount 个工作区进程需要受管回收。"
        } else {
            $failure = '客户端工作区进程已清理，但交互桌面验收条件未全部满足。'
        }
    }
    $record = [ordered]@{
        schema = 1
        client_build = $clientBuild
        verification_attempt = $attempt
        workspace_id = $workspaceId
        workspace = $workspace
        client_profile = $clientProfile
        component_count = $componentCount
        python_or_model_or_dsh_present = $pythonOrModelOrDshPresent
        system_webview2_present = $webview2Present
        launcher_exit_code = $launchExitCode
        desktop_host_started = $desktopHostStarted
        desktop_session_id = $desktopSessionId
        interactive_session = $interactiveSession
        main_window_handle_present = $mainWindowHandlePresent
        python_process_count = $pythonProcessCount
        exit_request_exit_code = $exitRequestExitCode
        forced_stop_count = $forcedStopCount
        remaining_workspace_process_count = $remaining.Count
        result = $result
        failure = if ($failure) { $failure.Substring(0, [Math]::Min(500, $failure.Length)) } else { $null }
        launcher_stdout = $launchStdout
        launcher_stderr = $launchStderr
        exit_stdout = $exitStdout
        exit_stderr = $exitStderr
        fields = $fields
    }
    try { [IO.File]::WriteAllText($reportPath, ($record | ConvertTo-Json -Depth 6 -Compress), [Text.UTF8Encoding]::new($false)) } catch {}
}
$json = $record | ConvertTo-Json -Depth 6 -Compress
[Console]::WriteLine([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))
if ($record.result -ne 'passed') { exit 5 }
exit 0
"#;
    let script = template
        .replace("__ROOT__", ACCEPTANCE_CLEANUP_ROOT)
        .replace("__CLIENT_BUILD__", client_build)
        .replace("__ATTEMPT__", attempt_id);
    let encoded = base64::engine::general_purpose::STANDARD.encode(
        script
            .encode_utf16()
            .flat_map(u16::to_le_bytes)
            .collect::<Vec<_>>(),
    );
    let command = format!(
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand {encoded}"
    );

    let private_key = voice_desktop_common::credentials::load_device_ssh_key(transport_ref)?;
    let private_key = PrivateKey::from_openssh(private_key.as_str())
        .map_err(|_| "本机 Windows 凭据库中的 SSH 登录密钥无法解析。".to_string())?;
    if private_key.is_encrypted() {
        return Err("SSH 登录密钥仍处于加密状态。".into());
    }
    let handler = PinnedRemoteCommandHandler {
        fingerprint: fingerprint.to_owned(),
    };
    let mut handle = tokio::time::timeout(
        Duration::from_secs(15),
        client::connect(Arc::new(client::Config::default()), (host, port), handler),
    )
    .await
    .map_err(|_| "远端轻量客户端启动 SSH 握手超时。".to_string())?
    .map_err(|_| "远端轻量客户端启动 SSH 指纹不匹配或连接失败。".to_string())?;
    let key = PrivateKeyWithHashAlg::new(Arc::new(private_key), Some(HashAlg::Sha256));
    let auth = tokio::time::timeout(
        Duration::from_secs(15),
        handle.authenticate_publickey(username.to_owned(), key),
    )
    .await
    .map_err(|_| "远端轻量客户端启动 SSH 公钥认证超时。".to_string())?
    .map_err(|_| "远端轻量客户端启动 SSH 公钥认证失败。".to_string())?;
    if !matches!(auth, AuthResult::Success) {
        let _ = handle
            .disconnect(
                Disconnect::ByApplication,
                "client workspace launch authentication failed",
                "",
            )
            .await;
        return Err("远端轻量客户端启动未通过固定账户 SSH 公钥认证。".into());
    }
    let mut channel = tokio::time::timeout(Duration::from_secs(15), handle.channel_open_session())
        .await
        .map_err(|_| "远端轻量客户端启动 SSH 通道开启超时。".to_string())?
        .map_err(|_| "远端轻量客户端启动 SSH 通道无法开启。".to_string())?;
    tokio::time::timeout(
        Duration::from_secs(15),
        channel.exec(true, command.into_bytes()),
    )
    .await
    .map_err(|_| "远端轻量客户端启动命令未及时接纳。".to_string())?
    .map_err(|_| "主机未接纳受限轻量客户端启动命令。".to_string())?;

    let mut stdout = Vec::new();
    let mut stderr = Vec::new();
    let mut exit_status = None;
    let mut output_overflow = false;
    let deadline = Instant::now() + Duration::from_secs(180);
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        match tokio::time::timeout(remaining, channel.wait()).await {
            Err(_) | Ok(None) | Ok(Some(ChannelMsg::Close)) => break,
            Ok(Some(ChannelMsg::Data { data })) => {
                if stdout
                    .len()
                    .saturating_add(stderr.len())
                    .saturating_add(data.len())
                    <= 64 * 1024
                {
                    stdout.extend_from_slice(&data);
                } else {
                    output_overflow = true;
                }
            }
            Ok(Some(ChannelMsg::ExtendedData { data, ext: 1 })) => {
                if stdout
                    .len()
                    .saturating_add(stderr.len())
                    .saturating_add(data.len())
                    <= 64 * 1024
                {
                    stderr.extend_from_slice(&data);
                } else {
                    output_overflow = true;
                }
            }
            Ok(Some(ChannelMsg::ExitStatus { exit_status: value })) => exit_status = Some(value),
            Ok(Some(ChannelMsg::ExitSignal { signal_name, .. })) => {
                return Err(format!("远端轻量客户端启动命令收到信号：{signal_name:?}"));
            }
            Ok(Some(_)) => {}
        }
    }
    let _ = channel.close().await;
    let _ = handle
        .disconnect(
            Disconnect::ByApplication,
            "client workspace launch complete",
            "",
        )
        .await;
    if output_overflow {
        return Err("轻量客户端启动报告超过 64 KiB；远端启动报告和日志保留。".into());
    }
    let exit_status = exit_status.ok_or_else(|| {
        format!(
            "远端轻量客户端启动命令未返回退出状态：{}",
            String::from_utf8_lossy(&stderr).trim()
        )
    })?;
    let encoded = String::from_utf8(stdout)
        .map_err(|_| "轻量客户端启动报告 Base64 结果不是 UTF-8 编码。".to_string())?;
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(encoded.trim())
        .map_err(|_| "轻量客户端启动报告 Base64 格式无效。".to_string())?;
    let report =
        String::from_utf8(bytes).map_err(|_| "轻量客户端启动报告不是 UTF-8 编码。".to_string())?;
    let value: serde_json::Value = serde_json::from_str(&report).map_err(|error| {
        let preview = report.chars().take(240).collect::<String>();
        let stderr_preview = String::from_utf8_lossy(&stderr)
            .chars()
            .take(1200)
            .collect::<String>();
        format!(
            "轻量客户端启动结果不是有效 JSON（SSH 退出码 {exit_status}；解码长度 {}；解析位置 {}；内容预览 {:?}；远端错误输出预览 {:?}）。",
            report.len(),
            error,
            preview,
            stderr_preview
        )
    })?;
    if exit_status != 0 || value["result"] != "passed" {
        return Err(format!(
            "远端轻量客户端工作区启动未通过；报告已保留：{report}"
        ));
    }
    Ok(report)
}
