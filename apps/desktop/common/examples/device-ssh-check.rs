//! Device SSH acceptance helper（设备 SSH 验收辅助程序）：只在本机构建目录运行。
use russh::client::AuthResult;
use russh::{
    client,
    keys::{HashAlg, PrivateKey, PrivateKeyWithHashAlg, PublicKeyOrCertificate},
    ChannelMsg, Disconnect,
};
use std::sync::Arc;
use std::{env, fs, time::Duration};
use voice_desktop_common::ssh_device::{
    probe_host_key, SshDeviceConnection, SshDeviceTarget, DEFAULT_GATEWAY_PORT,
};
use zeroize::Zeroizing;

fn main() {
    if let Err(error) = run() {
        eprintln!("验收失败：{error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), String> {
    let args = env::args().skip(1).collect::<Vec<_>>();
    let runtime = tokio::runtime::Runtime::new().map_err(|error| error.to_string())?;
    match args.first().map(String::as_str) {
        Some("probe") if args.len() == 3 => {
            let port = args[2].parse::<u16>().map_err(|_| "SSH 端口无效。")?;
            let fingerprint = runtime.block_on(probe_host_key(
                &args[1], port, Duration::from_secs(10),
            ))?;
            println!("{fingerprint}");
            Ok(())
        }
        Some(mode @ ("status" | "devices")) if args.len() == 6 => {
            let port = args[2].parse::<u16>().map_err(|_| "SSH 端口无效。")?;
            let private_key = Zeroizing::new(
                fs::read_to_string(&args[5]).map_err(|_| "无法从本机凭据文件读取验收 SSH 私钥。")?,
            );
            let target = SshDeviceTarget {
                address: args[1].clone(),
                port,
                username: args[3].clone(),
                host_key_sha256: args[4].clone(),
                gateway_port: DEFAULT_GATEWAY_PORT,
            };
            runtime.block_on(async move {
                let mut connection = SshDeviceConnection::connect(
                    target,
                    &private_key,
                    Duration::from_secs(10),
                )
                .await?;
                let (path, payload) = if mode == "devices" {
                    ("/v1/devices", serde_json::json!({"protocol_version":1}))
                } else {
                    ("/v1/status", serde_json::json!({"protocol_version":1}))
                };
                let result = connection
                    .post_json(
                        path,
                        &payload,
                        Duration::from_secs(10),
                    )
                    .await;
                let close = connection.close().await;
                let result = result?;
                close?;
                println!("{}", serde_json::to_string(&result).map_err(|_| "回执编码失败。")?);
                Ok(())
            })
        }
        Some(mode @ ("status-raw" | "status-raw-noeof")) if args.len() == 6 => {
            let port = args[2].parse::<u16>().map_err(|_| "SSH 端口无效。")?;
            let private_key = Zeroizing::new(
                fs::read_to_string(&args[5]).map_err(|_| "无法从本机凭据文件读取验收 SSH 私钥。")?,
            );
            let output = runtime.block_on(raw_status(
                &args[1], port, &args[3], &args[4], private_key.as_str(), mode == "status-raw",
            ))?;
            println!("{output}");
            Ok(())
        }
        _ => Err("用法：device-ssh-check probe <host> <port>；device-ssh-check status|devices <host> <port> <user> <SHA256-fingerprint> <private-key-path>；device-ssh-check status-raw[|-noeof] <host> <port> <user> <SHA256-fingerprint> <private-key-path>".into()),
    }
}

#[derive(Clone)]
struct RawPinnedHandler {
    expected_fingerprint: String, // expected_fingerprint（已确认的 SSH 主机指纹）。
}

impl client::Handler for RawPinnedHandler {
    type Error = russh::Error;

    async fn check_server_key(
        &mut self,
        server_public_key: &PublicKeyOrCertificate,
    ) -> Result<bool, Self::Error> {
        Ok(server_public_key
            .public_key()
            .fingerprint(HashAlg::Sha256)
            .to_string()
            == self.expected_fingerprint)
    }
}

async fn raw_status(
    host: &str,
    port: u16,
    username: &str,
    fingerprint: &str,
    private_key: &str,
    send_eof: bool,
) -> Result<String, String> {
    let target = SshDeviceTarget {
        address: host.to_string(),
        port,
        username: username.to_string(),
        host_key_sha256: fingerprint.to_string(),
        gateway_port: DEFAULT_GATEWAY_PORT,
    };
    target.validate()?;
    let private_key = PrivateKey::from_openssh(private_key)
        .map_err(|_| "本机验收 SSH 私钥无法解析。".to_string())?;
    if private_key.is_encrypted() {
        return Err("本机验收 SSH 私钥仍处于加密状态。".into());
    }
    let handler = RawPinnedHandler {
        expected_fingerprint: fingerprint.to_string(),
    };
    let mut handle = tokio::time::timeout(
        Duration::from_secs(10),
        client::connect(Arc::new(client::Config::default()), (host, port), handler),
    )
    .await
    .map_err(|_| "SSH 验收握手超时。".to_string())?
    .map_err(|_| "SSH 验收握手失败或主机指纹不匹配。".to_string())?;
    let key = PrivateKeyWithHashAlg::new(Arc::new(private_key), Some(HashAlg::Sha256));
    let auth = tokio::time::timeout(
        Duration::from_secs(10),
        handle.authenticate_publickey(username.to_string(), key),
    )
    .await
    .map_err(|_| "SSH 验收公钥认证超时。".to_string())?
    .map_err(|_| "SSH 验收公钥认证失败。".to_string())?;
    if !matches!(auth, AuthResult::Success) {
        let _ = handle
            .disconnect(Disconnect::ByApplication, "raw status probe failed", "")
            .await;
        return Err("SSH 验收公钥未获主机接受。".into());
    }
    let mut channel = handle
        .channel_open_direct_tcpip("127.0.0.1", u32::from(DEFAULT_GATEWAY_PORT), "127.0.0.1", 0)
        .await
        .map_err(|_| "SSH 验收回环通道无法打开。".to_string())?;
    let request = b"POST /v1/status HTTP/1.1\r\nHost: voice-terminal-device-gateway\r\nAccept: application/json\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: 22\r\n\r\n{\"protocol_version\":1}";
    channel
        .data_bytes(request.as_slice())
        .await
        .map_err(|_| "SSH 验收状态请求写入失败。".to_string())?;
    if send_eof {
        channel
            .eof()
            .await
            .map_err(|_| "SSH 验收状态请求 EOF 发送失败。".to_string())?;
    }
    let mut response = Vec::new();
    let mut events = Vec::new();
    loop {
        match tokio::time::timeout(Duration::from_secs(10), channel.wait()).await {
            Err(_) => {
                return Err(format!(
                    "SSH 原始通道等待超时；已收到 {} 字节，事件={events:?}",
                    response.len()
                ))
            }
            Ok(None) => {
                events.push("channel-closed".to_string());
                break;
            }
            Ok(Some(ChannelMsg::Data { data })) => {
                events.push(format!("data:{}", data.len()));
                response.extend_from_slice(&data);
            }
            Ok(Some(ChannelMsg::WindowAdjusted { .. })) => {
                events.push("window-adjusted".to_string())
            }
            Ok(Some(ChannelMsg::Eof)) => {
                events.push("eof".to_string());
                break;
            }
            Ok(Some(ChannelMsg::Close)) => {
                events.push("close".to_string());
                break;
            }
            Ok(Some(other)) => events.push(format!("other:{other:?}")),
        }
        if response.len() > 16_384 {
            break;
        }
    }
    let preview_length = response.len().min(2048);
    let preview = String::from_utf8_lossy(&response[..preview_length]);
    let _ = channel.close().await;
    handle
        .disconnect(Disconnect::ByApplication, "raw status probe complete", "")
        .await
        .map_err(|_| "SSH 原始验收会话关闭失败。".to_string())?;
    Ok(format!(
        "events={events:?} response_bytes={} preview={preview}",
        response.len()
    ))
}
