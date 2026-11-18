//! Device configuration（设备配置）：独立于产品设置版本，无法识别时保留原文件。
use base64::Engine;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{fs, io::Read, path::Path};

pub const SCHEMA_VERSION: u32 = 3; // SCHEMA_VERSION（设备配置格式版本）。
const MAX_BYTES: u64 = 64 * 1024; // MAX_BYTES（配置大小上限）。

/// Role（期望角色）：实际角色只能在目标模式准备完成后变更。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    Standalone,
    SharedHost,
    RemoteClient,
}

/// RunMode（用户可选运行模式）：共享主机和独立运行统一为本地运行。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RunMode {
    Local,
    Remote,
}

impl Default for RunMode {
    fn default() -> Self {
        Self::Local
    }
}

/// AudioMode（音频模式）：压缩档在质量验收前仍是实验功能。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AudioMode {
    Pcm,
    OpusExperimental,
}

/// HostTrust（主机公开信任资料）：凭据只保存系统凭据引用。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HostTrust {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub host_id: Option<String>, // host_id（由局域网发现得到的稳定服务端编号）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub display_name: Option<String>, // display_name（服务端显示名称）。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub addresses: Vec<String>, // addresses（配对时发现的局域网服务地址记录）。
    pub address: String,         // address（主机地址）。
    pub port: u16,               // port（SSH 安全外壳端口）。
    pub username: String,        // username（SSH 用户名）。
    pub host_key_sha256: String, // host_key_sha256（已确认的主机公钥指纹）。
    pub credential_ref: String,  // credential_ref（系统凭据引用，不是凭据本身）。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub transport_credential_ref: Option<String>, // transport_credential_ref（可选 SSH 登录凭据引用；缺省沿用设备身份）。
}

/// DeviceConfig（设备配置）：不保存会话台账或临时授权。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DeviceConfig {
    pub schema_version: u32, // schema_version（格式版本）。
    pub device_id: String,   // device_id（稳定设备编号）。
    #[serde(default)]
    pub mode: RunMode, // mode（本地运行或远程配对）。
    #[serde(default)]
    pub broadcast_enabled: bool, // broadcast_enabled（本机作为服务端向局域网广播）。
    #[serde(default = "default_true")]
    pub client_online_tts_local: bool, // client_online_tts_local（客户端在线语音本地合成和播放开关）。
    #[serde(default)]
    pub audio_compression: bool, // audio_compression（远程音频是否使用 Opus 压缩）。
    #[serde(default)]
    pub reconnect_suspended_reason: Option<String>, // reconnect_suspended_reason（自动重连暂停原因）。
    pub desired_role: Role,    // desired_role（期望角色）。
    pub audio_mode: AudioMode, // audio_mode（音频模式）。
    #[serde(rename = "paired_host", alias = "host")]
    pub host: Option<HostTrust>, // paired_host（已配对服务端身份、地址及非秘密凭据引用）。
}

fn default_true() -> bool {
    true
}

/// LoadResult（读取结果）：缺失采用单机；异常必须展示管理界面，不能写入默认值。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LoadResult {
    Missing,
    Valid(DeviceConfig),
    Invalid(String),
}

/// RuntimeSnapshot（运行角色快照）：只公开设备标识与模式，不返回凭据或 SSH 私密资料。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RuntimeSnapshot {
    pub config_state: &'static str,    // config_state（设备配置状态）。
    pub device_id: Option<String>,     // device_id（稳定设备编号）。
    pub mode: Option<RunMode>,         // mode（本地运行或远程配对）。
    pub broadcast_enabled: bool,       // broadcast_enabled（本机服务广播意图）。
    pub desired_role: Option<Role>,    // desired_role（用户选择的目标角色）。
    pub audio_mode: Option<AudioMode>, // audio_mode（原始或实验压缩档）。
    pub error: Option<String>,         // error（需要在管理界面处理的原因）。
}

fn valid_identifier(value: &str, limit: usize) -> bool {
    !value.is_empty()
        && value.len() <= limit
        && value.trim() == value
        && !value.chars().any(char::is_control)
}

fn valid_transport_credential_reference(value: &str) -> bool {
    value
        .strip_prefix("VoiceTerminal/ssh-transport/")
        .is_some_and(|suffix| {
            suffix.len() == 64
                && suffix
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
}

/// is_private_lan_address（检查局域网地址）：独立配置校验不能依赖可选的 SSH 发现模块。
fn is_private_lan_address(address: std::net::IpAddr) -> bool {
    match address {
        std::net::IpAddr::V4(ip) => {
            ip.is_private() && !ip.is_loopback() && !ip.is_link_local() && !ip.is_multicast()
        }
        std::net::IpAddr::V6(ip) => {
            let segments = ip.segments();
            (segments[0] & 0xfe00) == 0xfc00 && !ip.is_loopback() && !ip.is_multicast()
        }
    }
}

/// credential_reference_for_device（设备凭据引用）：使用稳定编号摘要生成本机凭据名。
pub fn credential_reference_for_device(device_id: &str) -> Result<String, String> {
    if !valid_identifier(device_id, 128) {
        return Err("设备编号无效，无法建立凭据引用。".into());
    }
    let digest = Sha256::digest(device_id.as_bytes());
    let suffix = digest
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    Ok(format!("VoiceTerminal/device/{suffix}"))
}

impl DeviceConfig {
    /// validate（校验）：不尝试修复、不接收未知版本或隐式改变角色。
    pub fn validate(&self) -> Result<(), String> {
        if self.schema_version != SCHEMA_VERSION {
            return Err("设备配置版本不受支持".into());
        }
        if !valid_identifier(&self.device_id, 128) {
            return Err("设备编号无效".into());
        }
        if self
            .reconnect_suspended_reason
            .as_ref()
            .is_some_and(|reason| {
                reason.is_empty()
                    || reason.len() > 256
                    || reason.trim() != reason
                    || reason.chars().any(char::is_control)
            })
        {
            return Err("自动重连暂停原因无效".into());
        }
        let expected_role = match self.mode {
            RunMode::Remote => Role::RemoteClient,
            RunMode::Local if self.broadcast_enabled => Role::SharedHost,
            RunMode::Local => Role::Standalone,
        };
        if self.desired_role != expected_role {
            return Err("运行模式、广播开关与兼容角色字段不一致".into());
        }
        if self.audio_compression != (self.audio_mode == AudioMode::OpusExperimental) {
            return Err("音频压缩开关与兼容音频字段不一致".into());
        }
        if self.mode == RunMode::Remote && self.broadcast_enabled {
            return Err("远程配对模式不能启用本地广播".into());
        }
        if let Some(host) = &self.host {
            let expected_credential = credential_reference_for_device(&self.device_id)?;
            let discovery_identity_valid = match (&host.host_id, &host.display_name) {
                (Some(host_id), Some(display_name)) => {
                    valid_identifier(host_id, 128)
                        && !display_name.trim().is_empty()
                        && display_name.len() <= 128
                        && !display_name.chars().any(char::is_control)
                        && !host.addresses.is_empty()
                        && host.addresses.len() <= 16
                        && host.addresses.iter().all(|value| {
                            value
                                .parse::<std::net::IpAddr>()
                                .is_ok_and(is_private_lan_address)
                        })
                        && host.addresses.contains(&host.address)
                }
                (None, None) => self.mode != RunMode::Remote && host.addresses.is_empty(),
                _ => false,
            };
            if !valid_identifier(&host.address, 255)
                || !host.address.bytes().all(|byte| {
                    byte.is_ascii_alphanumeric()
                        || matches!(byte, b'.' | b'-' | b'_' | b':' | b'[' | b']')
                })
                || host.port == 0
                || host.username.is_empty()
                || host.username.len() > 128
                || !host.username.bytes().all(|byte| {
                    byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-' | b'\\')
                })
                || !valid_identifier(&host.credential_ref, 256)
                || host.credential_ref != expected_credential
                || !host
                    .credential_ref
                    .strip_prefix("VoiceTerminal/device/")
                    .is_some_and(|suffix| {
                        suffix.len() == 64 && suffix.bytes().all(|byte| byte.is_ascii_hexdigit())
                    })
                || host
                    .transport_credential_ref
                    .as_ref()
                    .is_some_and(|reference| !valid_transport_credential_reference(reference))
                || !host
                    .host_key_sha256
                    .strip_prefix("SHA256:")
                    .is_some_and(|encoded| {
                        base64::engine::general_purpose::STANDARD_NO_PAD
                            .decode(encoded)
                            .is_ok_and(|value| value.len() == 32)
                    })
                || !discovery_identity_valid
            {
                return Err("主机地址、指纹或系统凭据引用无效".into());
            }
        }
        Ok(())
    }
}

/// load（读取）：不创建、迁移或覆盖文件；IO（输入输出）故障与缺失分开。
pub fn load(path: &Path) -> LoadResult {
    let metadata = match fs::symlink_metadata(path) {
        Ok(value) => value,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return LoadResult::Missing,
        Err(error) => return LoadResult::Invalid(format!("无法读取设备配置：{error}")),
    };
    if crate::workspace::metadata_reparse(&metadata)
        || !metadata.is_file()
        || metadata.len() > MAX_BYTES
    {
        return LoadResult::Invalid("设备配置类型、链接或大小无效；原件已保留".into());
    }
    // read_to_end（有界读取）：即使文件在 metadata（文件信息）检查后变大，也只额外读一个字节。
    let file = match fs::File::open(path) {
        Ok(value) => value,
        Err(error) => return LoadResult::Invalid(format!("无法读取设备配置：{error}")),
    };
    let mut bytes = Vec::with_capacity(metadata.len() as usize);
    if let Err(error) = file.take(MAX_BYTES + 1).read_to_end(&mut bytes) {
        return LoadResult::Invalid(format!("无法读取设备配置：{error}"));
    }
    if bytes.len() as u64 > MAX_BYTES {
        return LoadResult::Invalid("设备配置超过大小限制；原件已保留".into());
    }
    let mut raw: serde_json::Value = match serde_json::from_slice(&bytes) {
        Ok(value) => value,
        Err(_) => return LoadResult::Invalid("设备配置格式无效或不受支持；原件已保留".into()),
    };
    // v2（旧设备配置）只做确定性的内存兼容转换；未知字段和原件仍由正式保存流程处理。
    if raw
        .get("schema_version")
        .and_then(serde_json::Value::as_u64)
        == Some(2)
    {
        let role = raw
            .get("desired_role")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("");
        let remote = role == "remote_client";
        let broadcast = role == "shared_host";
        raw["schema_version"] = serde_json::json!(SCHEMA_VERSION);
        raw["mode"] = serde_json::json!(if remote { "remote" } else { "local" });
        raw["broadcast_enabled"] = serde_json::json!(broadcast);
        raw["client_online_tts_local"] = serde_json::json!(true);
        raw["audio_compression"] = serde_json::json!(
            raw.get("audio_mode").and_then(serde_json::Value::as_str) == Some("opus_experimental")
        );
        raw["reconnect_suspended_reason"] = serde_json::Value::Null;
        raw["desired_role"] = serde_json::json!(if remote {
            "remote_client"
        } else if broadcast {
            "shared_host"
        } else {
            "standalone"
        });
        // 旧远端地址和登录引用不再是配对凭据；保留原件供审计，但要求用户通过新配对流程登记。
        if remote {
            raw["host"] = serde_json::Value::Null;
        }
    }
    let value: DeviceConfig = match serde_json::from_value(raw) {
        Ok(value) => value,
        Err(_) => return LoadResult::Invalid("设备配置格式无效或不受支持；原件已保留".into()),
    };
    match value.validate() {
        Ok(()) => LoadResult::Valid(value),
        Err(error) => LoadResult::Invalid(error),
    }
}

/// load_from_root（按工作区读取）：使用受控 data（本机数据）路径。
pub fn load_from_root(data_root: &Path) -> LoadResult {
    match crate::workspace::safe_path(data_root, "config/device-runtime.json") {
        Ok(path) => load(&path),
        Err(error) => LoadResult::Invalid(error),
    }
}

/// save（保存期望设备模式）：校验原件并通过同盘事务写入。
pub fn save(data_root: &Path, config: &DeviceConfig) -> Result<RuntimeSnapshot, String> {
    save_with_recovery(data_root, config, false)
}

/// save_with_recovery（显式修复并存档）：只在用户确认后移动未知原件到受管归档。
pub fn save_with_recovery(
    data_root: &Path,
    config: &DeviceConfig,
    preserve_invalid: bool,
) -> Result<RuntimeSnapshot, String> {
    config.validate()?;
    let value = serde_json::to_value(config).map_err(|_| "设备配置编码失败。".to_string())?;
    let path = crate::workspace::safe_path(data_root, "config/device-runtime.json")?;
    let mut archive = None;
    if let LoadResult::Invalid(error) = load(&path) {
        if !preserve_invalid {
            return Err(format!("已有设备配置无效，原件已保留且未覆盖：{error}"));
        }
        let archive_relative = format!(
            "archives/device-runtime-{}.json",
            crate::workspace::new_id()
        );
        let archive_path = crate::workspace::safe_path(data_root, &archive_relative)?;
        if archive_path.exists() {
            return Err("设备配置原件归档目标已存在，未改动原件。".into());
        }
        fs::create_dir_all(archive_path.parent().ok_or("设备配置归档缺少父目录")?)
            .map_err(|error| format!("无法建立设备配置原件归档目录：{error}"))?;
        fs::rename(&path, &archive_path)
            .map_err(|error| format!("无法归档无效设备配置原件，未覆盖：{error}"))?;
        archive = Some(archive_path);
    }
    if let Err(error) = crate::workspace::atomic_json(&path, &value) {
        if let Some(archive) = archive.as_ref() {
            let _ = fs::remove_file(&path);
            if fs::rename(archive, &path).is_err() {
                return Err(format!(
                    "设备配置写入失败，原件保留在 {}：{error}",
                    archive.display()
                ));
            }
        }
        return Err(error);
    }
    match load(&path) {
        LoadResult::Valid(saved) if saved == *config => Ok(inspect(data_root)),
        _ => Err("设备配置写入后未通过回读验证。".into()),
    }
}

/// inspect（读取运行状态）：缺失配置仅产生临时单机默认值，不写入用户目录。
pub fn inspect(data_root: &Path) -> RuntimeSnapshot {
    let path = match crate::workspace::safe_path(data_root, "config/device-runtime.json") {
        Ok(path) => path,
        Err(error) => {
            return RuntimeSnapshot {
                config_state: "invalid",
                device_id: None,
                mode: None,
                broadcast_enabled: false,
                desired_role: None,
                audio_mode: None,
                error: Some(error),
            }
        }
    };
    match load(&path) {
        LoadResult::Missing => RuntimeSnapshot {
            config_state: "missing",
            device_id: Some("local".into()),
            mode: Some(RunMode::Local),
            broadcast_enabled: false,
            desired_role: Some(Role::Standalone),
            audio_mode: Some(AudioMode::Pcm),
            error: None,
        },
        LoadResult::Valid(value) => RuntimeSnapshot {
            config_state: "valid",
            device_id: Some(value.device_id),
            mode: Some(value.mode),
            broadcast_enabled: value.broadcast_enabled,
            desired_role: Some(value.desired_role),
            audio_mode: Some(value.audio_mode),
            error: None,
        },
        LoadResult::Invalid(error) => RuntimeSnapshot {
            config_state: "invalid",
            device_id: None,
            mode: None,
            broadcast_enabled: false,
            desired_role: None,
            audio_mode: None,
            error: Some(error),
        },
    }
}

/// startup_block_reason（启动阻止原因）：保留错误配置与未就绪客户端的安全门禁。
pub fn startup_block_reason(snapshot: &RuntimeSnapshot) -> Option<String> {
    if let Some(error) = &snapshot.error {
        return Some(error.clone());
    }
    match snapshot.desired_role {
        Some(Role::Standalone) => None,
        // 共享主机先启动本机权威核心；网关可用性单独上报，不得因 18765 端口占用阻塞本机业务。
        Some(Role::SharedHost) => None,
        // 远程配对本身不要求安装本地语音组件；未连接时保持管理界面等待配对。
        Some(Role::RemoteClient) => None,
        None => Some("设备角色无法确认，请检查设备配置；原件已保留。".into()),
    }
}

/// remote_client_connect_block_reason（远端客户端连接门禁）：不得并存本机权威核心与远端核心。
pub fn remote_client_connect_block_reason(
    role: Option<Role>,
    local_service_running: bool,
) -> Option<&'static str> {
    match role {
        Some(Role::RemoteClient) if !local_service_running => None,
        Some(Role::RemoteClient) => {
            Some("本机语音核心仍在运行；关闭本机核心后才能连接远端客户端。")
        }
        _ => Some("只有已选择远端客户端角色的配置可以连接共享主机。"),
    }
}

/// actual_role（实际角色）：只根据受管核心/网关或固定主机 SSH 连接的真实运行事实判定。
pub fn actual_role(
    service_running: bool,         // service_running（本机受管核心进程是否运行）。
    core_ready: bool,              // core_ready（本机权威核心是否完成就绪）。
    gateway_enabled: bool,         // gateway_enabled（本次启动是否请求共享网关）。
    gateway_ready: bool,           // gateway_ready（共享网关是否已监听）。
    remote_client_selected: bool,  // remote_client_selected（保存的期望角色是否为远端客户端）。
    remote_client_connected: bool, // remote_client_connected（固定主机 SSH 会话是否仍打开）。
) -> &'static str {
    if remote_client_selected {
        return if !service_running && !core_ready && remote_client_connected {
            "remote_client"
        } else {
            "none"
        };
    }
    if !service_running || !core_ready {
        return "none";
    }
    if gateway_enabled {
        if gateway_ready {
            "shared_host"
        } else {
            "none"
        }
    } else {
        "standalone"
    }
}

/// require_role_runtime（角色运行能力门禁）：保存本机核心角色前确认解释器入口存在。
/// 这里只检查最低启动能力，模型和组件就绪仍由正式启动流程验证。
pub fn require_role_runtime(role: Role, local_core_available: bool) -> Result<(), String> {
    if role != Role::RemoteClient && !local_core_available {
        return Err("当前工作区缺少本机核心运行组件，只能保存“远端客户端”角色；独立运行或共享主机请使用具备核心组件的完整工作区。".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn lightweight_workspace_cannot_save_a_local_core_role() {
        for role in [Role::Standalone, Role::SharedHost] {
            assert!(require_role_runtime(role, false).is_err());
            assert!(require_role_runtime(role, true).is_ok());
        }
        assert!(require_role_runtime(Role::RemoteClient, false).is_ok());
        assert!(require_role_runtime(Role::RemoteClient, true).is_ok());
    }

    #[test]
    fn remote_host_address_validation_works_without_the_optional_discovery_module() {
        assert!(is_private_lan_address("192.168.1.2".parse().unwrap()));
        assert!(is_private_lan_address("fd00::1".parse().unwrap()));
        assert!(!is_private_lan_address("8.8.8.8".parse().unwrap()));
        assert!(!is_private_lan_address("169.254.1.2".parse().unwrap()));
        assert!(!is_private_lan_address("fe80::1".parse().unwrap()));
    }

    #[test]
    fn missing_is_distinct_from_invalid_and_unknown_files_remain_untouched() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../.local/tmp")
            .join(format!("device-config-{}", crate::workspace::new_id()));
        fs::create_dir_all(&root).unwrap();
        let path = root.join("device.json");
        assert_eq!(load(&path), LoadResult::Missing);
        for content in ["{broken", r#"{"schema_version":99,"private":"保留"}"#] {
            fs::write(&path, content).unwrap();
            assert!(matches!(load(&path), LoadResult::Invalid(_)));
            assert_eq!(fs::read_to_string(&path).unwrap(), content);
        }
        let valid = DeviceConfig {
            schema_version: SCHEMA_VERSION,
            device_id: "device-1".into(),
            mode: RunMode::Local,
            broadcast_enabled: false,
            client_online_tts_local: true,
            audio_compression: false,
            reconnect_suspended_reason: None,
            desired_role: Role::Standalone,
            audio_mode: AudioMode::Pcm,
            host: None,
        };
        fs::write(&path, serde_json::to_vec(&valid).unwrap()).unwrap();
        assert_eq!(load(&path), LoadResult::Valid(valid));
        fs::remove_file(path).unwrap();
        fs::remove_dir(root).unwrap();
    }

    #[test]
    fn v2_roles_migrate_to_two_modes_without_mutating_the_source_file() {
        let root =
            std::env::temp_dir().join(format!("voice-device-v2-{}", crate::workspace::new_id()));
        fs::create_dir_all(&root).unwrap();
        let path = root.join("device.json");
        for (role, expected_mode, expected_broadcast, expected_audio) in [
            ("standalone", RunMode::Local, false, false),
            ("shared_host", RunMode::Local, true, false),
            ("remote_client", RunMode::Remote, false, true),
        ] {
            let source = serde_json::json!({
                "schema_version":2,"device_id":"legacy-device","desired_role":role,
                "audio_mode":if expected_audio {"opus_experimental"} else {"pcm"},"host":null
            });
            let bytes = serde_json::to_vec(&source).unwrap();
            fs::write(&path, &bytes).unwrap();
            let LoadResult::Valid(migrated) = load(&path) else {
                panic!("v2 {role} 配置应可迁移")
            };
            assert_eq!(migrated.schema_version, SCHEMA_VERSION);
            assert_eq!(migrated.mode, expected_mode);
            assert_eq!(migrated.broadcast_enabled, expected_broadcast);
            assert_eq!(migrated.audio_compression, expected_audio);
            assert_eq!(
                migrated.desired_role,
                match role {
                    "shared_host" => Role::SharedHost,
                    "remote_client" => Role::RemoteClient,
                    _ => Role::Standalone,
                }
            );
            assert_eq!(fs::read(&path).unwrap(), bytes, "兼容读取不能擅自改写原件");
        }
        fs::remove_file(path).unwrap();
        fs::remove_dir(root).unwrap();
    }

    #[test]
    fn v2_remote_host_is_not_implicitly_trusted_after_mode_migration() {
        let root = std::env::temp_dir().join(format!(
            "voice-device-v2-remote-{}",
            crate::workspace::new_id()
        ));
        fs::create_dir_all(&root).unwrap();
        let path = root.join("device.json");
        let old_host = serde_json::json!({
            "address":"old-host.local","port":22,"username":"old_user",
            "host_key_sha256":format!("SHA256:{}", base64::engine::general_purpose::STANDARD_NO_PAD.encode([4_u8;32])),
            "credential_ref":"VoiceTerminal/device/0000000000000000000000000000000000000000000000000000000000000000"
        });
        let source = serde_json::json!({
            "schema_version":2,"device_id":"legacy-device","desired_role":"remote_client",
            "audio_mode":"pcm","host":old_host
        });
        let bytes = serde_json::to_vec(&source).unwrap();
        fs::write(&path, &bytes).unwrap();
        let LoadResult::Valid(migrated) = load(&path) else {
            panic!("旧远程配置应能安全迁移为等待配对")
        };
        assert_eq!(migrated.mode, RunMode::Remote);
        assert!(migrated.host.is_none());
        assert_eq!(fs::read(&path).unwrap(), bytes, "只读迁移不覆盖旧配置原件");
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn validates_role_host_and_credential_reference() {
        let mut config = DeviceConfig {
            schema_version: SCHEMA_VERSION,
            device_id: "device-1".into(),
            mode: RunMode::Remote,
            broadcast_enabled: false,
            client_online_tts_local: true,
            audio_compression: false,
            reconnect_suspended_reason: None,
            desired_role: Role::RemoteClient,
            audio_mode: AudioMode::Pcm,
            host: None,
        };
        assert!(config.validate().is_ok(), "远程配对草稿允许尚未登记服务端");
        config.host = Some(HostTrust {
            host_id: Some("host-a".into()),
            display_name: Some("测试服务端".into()),
            addresses: vec!["192.168.1.2".into()],
            address: "192.168.1.2".into(),
            port: 22,
            username: "voice_terminal".into(),
            host_key_sha256: format!(
                "SHA256:{}",
                base64::engine::general_purpose::STANDARD_NO_PAD.encode([7_u8; 32])
            ),
            credential_ref: credential_reference_for_device("device-1").unwrap(),
            transport_credential_ref: None,
        });
        assert!(config.validate().is_ok());
        config.host.as_mut().unwrap().transport_credential_ref =
            Some(format!("VoiceTerminal/ssh-transport/{}", "b".repeat(64)));
        assert!(config.validate().is_ok());
        config.host.as_mut().unwrap().transport_credential_ref =
            Some("VoiceTerminal/device/not-a-transport-key".into());
        assert!(config.validate().is_err());
        config.host.as_mut().unwrap().transport_credential_ref = None;
        config.host.as_mut().unwrap().credential_ref = "actual-password".into();
        assert!(config.validate().is_err());
    }

    #[test]
    fn save_round_trips_current_schema_and_never_overwrites_invalid_original() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../.local/tmp")
            .join(format!("device-config-save-{}", crate::workspace::new_id()));
        let valid = DeviceConfig {
            schema_version: SCHEMA_VERSION,
            device_id: "local-save-test".into(),
            mode: RunMode::Local,
            broadcast_enabled: false,
            client_online_tts_local: true,
            audio_compression: false,
            reconnect_suspended_reason: None,
            desired_role: Role::Standalone,
            audio_mode: AudioMode::Pcm,
            host: None,
        };
        let saved = save(&root, &valid).unwrap();
        assert_eq!(saved.config_state, "valid");
        assert_eq!(load_from_root(&root), LoadResult::Valid(valid.clone()));
        let path = root.join("config/device-runtime.json");
        let unknown = br#"{"schema_version":91,"private":"keep"}"#;
        fs::write(&path, unknown).unwrap();
        assert!(save(&root, &valid).unwrap_err().contains("原件已保留"));
        assert_eq!(fs::read(&path).unwrap(), unknown);
        assert!(save_with_recovery(&root, &valid, true).is_ok());
        assert_eq!(load_from_root(&root), LoadResult::Valid(valid));
        let mut archived = fs::read_dir(root.join("archives"))
            .unwrap()
            .map(|entry| fs::read(entry.unwrap().path()).unwrap())
            .collect::<Vec<_>>();
        assert_eq!(archived.len(), 1);
        assert_eq!(archived.remove(0), unknown);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn missing_configuration_is_read_only_and_remote_mode_waits_without_local_core_gate() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../.local/tmp")
            .join(format!("device-config-{}", crate::workspace::new_id()));
        fs::create_dir_all(&root).unwrap();
        let missing = inspect(&root);
        assert_eq!(missing.config_state, "missing");
        assert_eq!(missing.desired_role, Some(Role::Standalone));
        assert!(startup_block_reason(&missing).is_none());
        assert!(!root.join("config/device-runtime.json").exists());

        let path = root.join("config/device-runtime.json");
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        let remote = DeviceConfig {
            schema_version: SCHEMA_VERSION,
            device_id: "remote-device".into(),
            mode: RunMode::Remote,
            broadcast_enabled: false,
            client_online_tts_local: true,
            audio_compression: false,
            reconnect_suspended_reason: None,
            desired_role: Role::RemoteClient,
            audio_mode: AudioMode::Pcm,
            host: Some(HostTrust {
                host_id: Some("host-a".into()),
                display_name: Some("测试服务端".into()),
                addresses: vec!["192.168.1.2".into()],
                address: "192.168.1.2".into(),
                port: 22,
                username: "voice_terminal".into(),
                host_key_sha256: format!(
                    "SHA256:{}",
                    base64::engine::general_purpose::STANDARD_NO_PAD.encode([8_u8; 32])
                ),
                credential_ref: credential_reference_for_device("remote-device").unwrap(),
                transport_credential_ref: None,
            }),
        };
        let bytes = serde_json::to_vec(&remote).unwrap();
        fs::write(&path, &bytes).unwrap();
        let selected = inspect(&root);
        assert_eq!(selected.desired_role, Some(Role::RemoteClient));
        assert!(startup_block_reason(&selected).is_none());
        assert_eq!(fs::read(&path).unwrap(), bytes);

        let mut shared = remote;
        shared.mode = RunMode::Local;
        shared.broadcast_enabled = true;
        shared.host = None;
        shared.desired_role = Role::SharedHost;
        shared.host = None;
        fs::write(&path, serde_json::to_vec(&shared).unwrap()).unwrap();
        let selected_shared = inspect(&root);
        assert_eq!(selected_shared.desired_role, Some(Role::SharedHost));
        assert!(startup_block_reason(&selected_shared).is_none());
        assert_eq!(load_from_root(&root), LoadResult::Valid(shared));

        fs::write(&path, b"{broken").unwrap();
        let invalid = inspect(&root);
        assert_eq!(invalid.config_state, "invalid");
        assert!(startup_block_reason(&invalid).unwrap().contains("保留"));
        assert_eq!(fs::read(&path).unwrap(), b"{broken");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn actual_role_uses_runtime_readiness_not_the_saved_desired_role() {
        assert_eq!(
            actual_role(true, true, false, false, false, false),
            "standalone"
        );
        assert_eq!(
            actual_role(true, true, true, true, false, false),
            "shared_host"
        );
        assert_eq!(actual_role(true, true, true, false, false, false), "none");
        assert_eq!(actual_role(true, false, true, true, false, false), "none");
        assert_eq!(actual_role(false, true, false, false, false, false), "none");
        assert_eq!(
            actual_role(false, false, false, false, true, true),
            "remote_client"
        );
        assert_eq!(actual_role(false, false, false, false, true, false), "none");
        assert_eq!(actual_role(true, true, false, false, true, true), "none");
        assert_eq!(actual_role(false, true, false, false, true, true), "none");
    }

    #[test]
    fn remote_client_cannot_connect_while_the_local_authoritative_core_runs() {
        assert!(remote_client_connect_block_reason(Some(Role::RemoteClient), false).is_none());
        assert!(
            remote_client_connect_block_reason(Some(Role::RemoteClient), true)
                .unwrap()
                .contains("本机语音核心仍在运行")
        );
        assert!(
            remote_client_connect_block_reason(Some(Role::Standalone), false)
                .unwrap()
                .contains("只有已选择远端客户端角色")
        );
    }
}
