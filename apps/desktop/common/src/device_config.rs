//! Device configuration（设备配置）：独立于产品设置版本，无法识别时保留原文件。
use base64::Engine;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{fs, io::Read, path::Path};

pub const SCHEMA_VERSION: u32 = 2; // SCHEMA_VERSION（设备配置格式版本）。
const MAX_BYTES: u64 = 64 * 1024; // MAX_BYTES（配置大小上限）。

/// Role（期望角色）：实际角色只能在目标模式准备完成后变更。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    Standalone,
    SharedHost,
    RemoteClient,
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
    pub schema_version: u32,     // schema_version（格式版本）。
    pub device_id: String,       // device_id（稳定设备编号）。
    pub desired_role: Role,      // desired_role（期望角色）。
    pub audio_mode: AudioMode,   // audio_mode（音频模式）。
    pub host: Option<HostTrust>, // host（已信任主机的公开资料与凭据引用）。
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
    value.strip_prefix("VoiceTerminal/ssh-transport/")
        .is_some_and(|suffix| suffix.len() == 64 && suffix.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)))
}

/// credential_reference_for_device（设备凭据引用）：使用稳定编号摘要生成本机凭据名。
pub fn credential_reference_for_device(device_id: &str) -> Result<String, String> {
    if !valid_identifier(device_id, 128) {
        return Err("设备编号无效，无法建立凭据引用。".into());
    }
    let digest = Sha256::digest(device_id.as_bytes());
    let suffix = digest.iter().map(|byte| format!("{byte:02x}")).collect::<String>();
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
        if let Some(host) = &self.host {
            let expected_credential = credential_reference_for_device(&self.device_id)?;
            if !valid_identifier(&host.address, 255)
                || !host.address.bytes().all(|byte| {
                    byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-' | b'_' | b':' | b'[' | b']')
                })
                || host.port == 0
                || host.username.is_empty()
                || host.username.len() > 128
                || !host.username.bytes().all(|byte| {
                    byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-')
                })
                || !valid_identifier(&host.credential_ref, 256)
                || host.credential_ref != expected_credential
                || !host
                    .credential_ref
                    .strip_prefix("VoiceTerminal/device/")
                    .is_some_and(|suffix| {
                        suffix.len() == 64 && suffix.bytes().all(|byte| byte.is_ascii_hexdigit())
                    })
                || host.transport_credential_ref.as_ref()
                    .is_some_and(|reference| !valid_transport_credential_reference(reference))
                || !host
                    .host_key_sha256
                    .strip_prefix("SHA256:")
                    .is_some_and(|encoded| {
                        base64::engine::general_purpose::STANDARD_NO_PAD
                            .decode(encoded)
                            .is_ok_and(|value| value.len() == 32)
                    })
            {
                return Err("主机地址、指纹或系统凭据引用无效".into());
            }
        }
        if self.desired_role == Role::RemoteClient && self.host.is_none() {
            return Err("远端客户端配置缺少已信任主机".into());
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
    let value: DeviceConfig = match serde_json::from_slice(&bytes) {
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
        let archive_relative = format!("archives/device-runtime-{}.json", crate::workspace::new_id());
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
                return Err(format!("设备配置写入失败，原件保留在 {}：{error}", archive.display()));
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
            desired_role: Some(Role::Standalone),
            audio_mode: Some(AudioMode::Pcm),
            error: None,
        },
        LoadResult::Valid(value) => RuntimeSnapshot {
            config_state: "valid",
            device_id: Some(value.device_id),
            desired_role: Some(value.desired_role),
            audio_mode: Some(value.audio_mode),
            error: None,
        },
        LoadResult::Invalid(error) => RuntimeSnapshot {
            config_state: "invalid",
            device_id: None,
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
        Some(Role::RemoteClient) => {
            Some("已选择远端客户端角色，但受信连接尚未准备就绪；当前停留在本地管理界面。".into())
        }
        None => Some("设备角色无法确认，请检查设备配置；原件已保留。".into()),
    }
}

/// remote_client_connect_block_reason（远端客户端连接门禁）：不得并存本机权威核心与远端核心。
pub fn remote_client_connect_block_reason(role: Option<Role>, local_service_running: bool) -> Option<&'static str> {
    match role {
        Some(Role::RemoteClient) if !local_service_running => None,
        Some(Role::RemoteClient) => Some("本机语音核心仍在运行；关闭本机核心后才能连接远端客户端。"),
        _ => Some("只有已选择远端客户端角色的配置可以连接共享主机。"),
    }
}

/// actual_role（实际角色）：只根据受管核心/网关或固定主机 SSH 连接的真实运行事实判定。
pub fn actual_role(
    service_running: bool, // service_running（本机受管核心进程是否运行）。
    core_ready: bool, // core_ready（本机权威核心是否完成就绪）。
    gateway_enabled: bool, // gateway_enabled（本次启动是否请求共享网关）。
    gateway_ready: bool, // gateway_ready（共享网关是否已监听）。
    remote_client_selected: bool, // remote_client_selected（保存的期望角色是否为远端客户端）。
    remote_client_connected: bool, // remote_client_connected（固定主机 SSH 会话是否仍打开）。
) -> &'static str {
    if remote_client_selected {
        return if !service_running && !core_ready && remote_client_connected { "remote_client" } else { "none" };
    }
    if !service_running || !core_ready {
        return "none";
    }
    if gateway_enabled {
        if gateway_ready { "shared_host" } else { "none" }
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
    fn validates_role_host_and_credential_reference() {
        let mut config = DeviceConfig {
            schema_version: SCHEMA_VERSION,
            device_id: "device-1".into(),
            desired_role: Role::RemoteClient,
            audio_mode: AudioMode::Pcm,
            host: None,
        };
        assert!(config.validate().is_err());
        config.host = Some(HostTrust {
            address: "host.example".into(),
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
        config.host.as_mut().unwrap().transport_credential_ref = Some(
            format!("VoiceTerminal/ssh-transport/{}", "b".repeat(64)),
        );
        assert!(config.validate().is_ok());
        config.host.as_mut().unwrap().transport_credential_ref = Some("VoiceTerminal/device/not-a-transport-key".into());
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
    fn missing_configuration_is_read_only_and_remote_client_stays_gated_until_ready() {
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
            desired_role: Role::RemoteClient,
            audio_mode: AudioMode::Pcm,
            host: Some(HostTrust {
                address: "host.example".into(),
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
        assert!(startup_block_reason(&selected)
            .unwrap()
            .contains("受信连接尚未准备就绪"));
        assert_eq!(fs::read(&path).unwrap(), bytes);

        let mut shared = remote;
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
        assert_eq!(actual_role(true, true, false, false, false, false), "standalone");
        assert_eq!(actual_role(true, true, true, true, false, false), "shared_host");
        assert_eq!(actual_role(true, true, true, false, false, false), "none");
        assert_eq!(actual_role(true, false, true, true, false, false), "none");
        assert_eq!(actual_role(false, true, false, false, false, false), "none");
        assert_eq!(actual_role(false, false, false, false, true, true), "remote_client");
        assert_eq!(actual_role(false, false, false, false, true, false), "none");
        assert_eq!(actual_role(true, true, false, false, true, true), "none");
        assert_eq!(actual_role(false, true, false, false, true, true), "none");
    }

    #[test]
    fn remote_client_cannot_connect_while_the_local_authoritative_core_runs() {
        assert!(remote_client_connect_block_reason(Some(Role::RemoteClient), false).is_none());
        assert!(remote_client_connect_block_reason(Some(Role::RemoteClient), true)
            .unwrap().contains("本机语音核心仍在运行"));
        assert!(remote_client_connect_block_reason(Some(Role::Standalone), false)
            .unwrap().contains("只有已选择远端客户端角色"));
    }
}
