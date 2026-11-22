//! SystemFeature（系统功能）：与工作区文件组件分离的 Windows 主机能力。
use base64::Engine;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

pub const OPENSSH_SERVER_ID: &str = "openssh-server"; // OPENSSH_SERVER_ID（OpenSSH Server 功能编号）。
pub const OPENSSH_SERVER_JOB_ID: &str = "system-feature-openssh-server"; // OPENSSH_SERVER_JOB_ID（OpenSSH Server 受管任务编号）。
const OPENSSH_CAPABILITY: &str = "OpenSSH.Server~~~~0.0.1.0"; // OPENSSH_CAPABILITY（Windows 可选功能固定名称）。
pub const DEVICE_PAIRING_PORT: u16 = 18_766; // DEVICE_PAIRING_PORT（仅限局域网的 OPAQUE 配对端口）。
pub const OPENSSH_SERVER_UAC_CANCELLED_ERROR: &str = "已取消 Windows 管理员授权；系统维护未执行。"; // OPENSSH_SERVER_UAC_CANCELLED_ERROR（用户拒绝 UAC 后的固定结果）。
const MAX_EXISTING_SSH_RULES: usize = 8; // MAX_EXISTING_SSH_RULES（界面最多显示的其他 SSH 入站候选规则）。
const MAX_SSH_RULE_DETAIL_CHARS: usize = 180; // MAX_SSH_RULE_DETAIL_CHARS（单条防火墙规则摘要最大字符数）。
const OPENSSH_SERVICE_SCRIPT: &str = include_str!("openssh_service.ps1"); // OPENSSH_SERVICE_SCRIPT（官方服务定位及命令参数检查）。

/// current_user_sid（当前进程账户 SID）：从 Windows 访问令牌读取，不从环境变量猜测。
pub fn current_user_sid() -> Result<String, String> {
    #[cfg(windows)]
    {
        use std::{mem::size_of, ptr};
        use windows_sys::Win32::{
            Foundation::{CloseHandle, HANDLE},
            Security::{GetLengthSid, GetTokenInformation, TokenUser, TOKEN_QUERY, TOKEN_USER},
            System::Threading::{GetCurrentProcess, OpenProcessToken},
        };
        unsafe {
            let mut token: HANDLE = ptr::null_mut();
            if OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) == 0 {
                return Err("无法读取当前 Windows 登录账户的访问令牌。".into());
            }
            let mut required = 0u32;
            let _ = GetTokenInformation(token, TokenUser, ptr::null_mut(), 0, &mut required);
            if required < size_of::<TOKEN_USER>() as u32 || required > 64 * 1024 {
                let _ = CloseHandle(token);
                return Err("当前 Windows 账户 SID（安全标识符）缓冲区无效。".into());
            }
            let mut storage = vec![0u8; required as usize];
            let read = GetTokenInformation(
                token,
                TokenUser,
                storage.as_mut_ptr().cast(),
                required,
                &mut required,
            );
            let _ = CloseHandle(token);
            if read == 0 {
                return Err("无法读取当前 Windows 账户 SID（安全标识符）。".into());
            }
            let token_user = &*(storage.as_ptr().cast::<TOKEN_USER>());
            let sid = token_user.User.Sid;
            if sid.is_null() {
                return Err("Windows 账户访问令牌缺少 SID（安全标识符）。".into());
            }
            let length = GetLengthSid(sid) as usize;
            if !(8..=68).contains(&length) {
                return Err("Windows 账户 SID（安全标识符）长度无效。".into());
            }
            let bytes = std::slice::from_raw_parts(sid.cast::<u8>(), length);
            format_sid(bytes)
        }
    }
    #[cfg(not(windows))]
    {
        Err("读取 Windows 登录账户 SID 只支持 Windows 主机。".into())
    }
}

fn format_sid(bytes: &[u8]) -> Result<String, String> {
    if bytes.len() < 8 {
        return Err("SID（安全标识符）数据被截断。".into());
    }
    let sub_count = bytes[1] as usize;
    if sub_count > 15 || bytes.len() != 8 + sub_count * 4 {
        return Err("SID（安全标识符）子权限结构无效。".into());
    }
    let authority = bytes[2..8]
        .iter()
        .fold(0u64, |value, byte| (value << 8) | u64::from(*byte));
    let mut value = format!("S-{}-{authority}", bytes[0]);
    for chunk in bytes[8..].chunks_exact(4) {
        value.push('-');
        value.push_str(&u32::from_le_bytes(chunk.try_into().unwrap()).to_string());
    }
    Ok(value)
}

/// OpenSshServerStatus（OpenSSH Server 状态）：只读检查，不启动或修改系统服务。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OpenSshServerStatus {
    pub feature_id: String,                // feature_id（固定 Windows 功能编号）。
    pub capability_state: String,          // capability_state（Windows 可选功能状态）。
    pub service_present: bool,             // service_present（sshd 系统服务是否存在）。
    pub service_state: Option<String>,     // service_state（sshd 当前运行状态）。
    pub startup_type: Option<String>,      // startup_type（现有 sshd 启动类型；只读取）。
    pub source_path: Option<String>,       // source_path（微软 OpenSSH Server 程序文件路径）。
    pub source_version: Option<String>,    // source_version（OpenSSH Server 文件版本）。
    pub signature_status: Option<String>,  // signature_status（微软程序签名校验状态）。
    pub signature_subject: Option<String>, // signature_subject（程序签名主体）。
    pub official_binary: bool,             // official_binary（程序路径及微软签名是否通过校验）。
    pub service_path_official: bool, // service_path_official（sshd 服务是否指向微软程序路径）。
    pub effective_port: Option<u16>, // effective_port（sshd 有效配置端口）。
    pub listening_ports: Vec<u16>,   // listening_ports（sshd 进程当前实际监听端口）。
    pub configuration_valid: bool, // configuration_valid（sshd -T 通过，或未变化的官方运行服务已加载有效配置）。
    pub configuration_path: Option<String>, // configuration_path（服务实际使用的配置文件，包含 -f 指定路径）。
    pub authorized_keys_path: Option<String>, // authorized_keys_path（当前账户使用的授权公钥文件位置）。
    pub authorized_keys_exists: bool, // authorized_keys_exists（当前账户授权公钥文件是否存在）。
    pub account_name: Option<String>, // account_name（当前 Windows 登录账户名称）。
    pub account_ssh_username: Option<String>, // account_ssh_username（由 SID 解析的 OpenSSH 登录名，不从用户目录名或邮箱推断）。
    pub account_sid: Option<String>,          // account_sid（当前 Windows 登录账户 SID）。
    pub account_is_admin: bool,               // account_is_admin（当前账户是否属于本机管理员组）。
    pub account_is_entra: bool, // account_is_entra（是否为 Windows OpenSSH 不支持的 Entra 身份）。
    pub ready: bool,            // ready（官方程序、配置、服务及实际端口均通过基础就绪检查）。
    pub project_access_enabled: bool, // project_access_enabled（本程序局域网入站规则是否启用）。
    pub project_access_valid: bool, // project_access_valid（SSH 程序、端口、协议、配置文件和精确局域网范围均匹配）。
    pub project_network_scope: Vec<String>, // project_network_scope（本程序规则允许的活动局域网子网）。
    pub pairing_access_enabled: bool, // pairing_access_enabled（本程序配对端口规则是否启用）。
    pub pairing_access_valid: bool, // pairing_access_valid（程序、固定端口、协议和精确局域网范围均匹配）。
    pub pairing_network_scope: Vec<String>, // pairing_network_scope（配对端口规则允许的活动局域网子网）。
    pub discovery_access_enabled: bool, // discovery_access_enabled（程序 mDNS（多播发现）规则是否启用）。
    pub discovery_access_valid: bool, // discovery_access_valid（UDP（数据报协议）5353 发现规则是否精确匹配）。
    pub existing_ssh_inbound_rules: Vec<String>, // existing_ssh_inbound_rules（非本程序拥有的启用 SSH 入站候选规则，只读列出）。
    pub local_lan_addresses: Vec<String>, // local_lan_addresses（活动物理局域网接口上的私有地址）。
    pub local_lan_scopes: Vec<String>, // local_lan_scopes（活动物理局域网接口对应的精确 CIDR 子网）。
    pub fields: BTreeMap<String, String>, // fields（状态字段的中文说明）。
}

impl OpenSshServerStatus {
    fn from_json(value: serde_json::Value) -> Result<Self, String> {
        let capability_state = value
            .get("capability_state")
            .and_then(serde_json::Value::as_str)
            .ok_or("OpenSSH Server 状态缺少 capability_state（可选功能状态）。")?;
        if !matches!(
            capability_state,
            "Installed"
                | "NotPresent"
                | "InstallPending"
                | "UninstallPending"
                | "Staged"
                | "Removed"
                | "Superseded"
                | "PartiallyInstalled"
                | "Unknown"
        ) {
            return Err("Windows 返回了未知的 OpenSSH Server 功能状态。".into());
        }

        let service_present = value
            .get("service_present")
            .and_then(serde_json::Value::as_bool)
            .ok_or("OpenSSH Server 状态缺少 service_present（服务是否存在）。")?;
        let service_state = optional_string(&value, "service_state")?;
        let source_path = optional_string_if_present(&value, "source_path")?;
        let source_version = optional_string_if_present(&value, "source_version")?;
        let signature_status = optional_string_if_present(&value, "signature_status")?;
        let signature_subject = optional_string_if_present(&value, "signature_subject")?;
        let official_binary_hint = value
            .get("official_binary")
            .and_then(serde_json::Value::as_bool)
            .unwrap_or(false);
        let service_path_official = value
            .get("service_path_official")
            .and_then(serde_json::Value::as_bool)
            .unwrap_or(false);
        let effective_port = value
            .get("effective_port")
            .and_then(serde_json::Value::as_u64)
            .and_then(|port| u16::try_from(port).ok())
            .filter(|port| *port > 0);
        let listening_ports: Vec<u16> = value
            .get("listening_ports")
            .and_then(serde_json::Value::as_array)
            .map(|ports| {
                ports
                    .iter()
                    .filter_map(serde_json::Value::as_u64)
                    .filter_map(|port| u16::try_from(port).ok())
                    .collect()
            })
            .unwrap_or_default();
        let configuration_valid = value
            .get("configuration_valid")
            .and_then(serde_json::Value::as_bool)
            .unwrap_or(false);
        let authorized_keys_path = optional_string_if_present(&value, "authorized_keys_path")?;
        let authorized_keys_exists = value
            .get("authorized_keys_exists")
            .and_then(serde_json::Value::as_bool)
            .unwrap_or(false);
        let account_name = optional_string_if_present(&value, "account_name")?;
        let account_ssh_username = optional_string_if_present(&value, "account_ssh_username")?;
        let account_sid = optional_string_if_present(&value, "account_sid")?;
        let account_is_admin = value
            .get("account_is_admin")
            .and_then(serde_json::Value::as_bool)
            .unwrap_or(false);
        let account_is_entra = value
            .get("account_is_entra")
            .and_then(serde_json::Value::as_bool)
            .unwrap_or(false);
        let project_access_enabled = value
            .get("project_access_enabled")
            .and_then(serde_json::Value::as_bool)
            .unwrap_or(false);
        let pairing_access_enabled = value
            .get("pairing_access_enabled")
            .and_then(serde_json::Value::as_bool)
            .unwrap_or(false);
        let project_access_valid = value
            .get("project_access_valid")
            .and_then(serde_json::Value::as_bool)
            .unwrap_or(false);
        let pairing_access_valid = value
            .get("pairing_access_valid")
            .and_then(serde_json::Value::as_bool)
            .unwrap_or(false);
        let project_network_scope = value
            .get("project_network_scope")
            .and_then(serde_json::Value::as_array)
            .map(|rows| {
                rows.iter()
                    .filter_map(serde_json::Value::as_str)
                    .map(str::to_owned)
                    .collect()
            })
            .unwrap_or_default();
        let local_lan_addresses = value
            .get("local_lan_addresses")
            .and_then(serde_json::Value::as_array)
            .map(|rows| {
                rows.iter()
                    .filter_map(serde_json::Value::as_str)
                    .map(str::to_owned)
                    .collect()
            })
            .unwrap_or_default();
        let local_lan_scopes = value
            .get("local_lan_scopes")
            .and_then(serde_json::Value::as_array)
            .map(|rows| {
                rows.iter()
                    .filter_map(serde_json::Value::as_str)
                    .map(str::to_owned)
                    .collect()
            })
            .unwrap_or_default();
        let pairing_network_scope = value
            .get("pairing_network_scope")
            .and_then(serde_json::Value::as_array)
            .map(|rows| {
                rows.iter()
                    .filter_map(serde_json::Value::as_str)
                    .map(str::to_owned)
                    .collect()
            })
            .unwrap_or_default();
        let existing_ssh_inbound_rules = value
            .get("existing_ssh_inbound_rules")
            .and_then(serde_json::Value::as_array)
            .map(|rows| {
                rows.iter()
                    .filter_map(serde_json::Value::as_str)
                    .take(MAX_EXISTING_SSH_RULES)
                    .map(bounded_ssh_rule_detail)
                    .collect()
            })
            .unwrap_or_default();
        // Win32_Service.StartMode（系统服务启动模式）使用 Auto；公开状态统一为 Automatic（自动）。
        let startup_type = optional_string(&value, "startup_type")?.map(|mode| {
            if mode == "Auto" {
                "Automatic".into()
            } else {
                mode
            }
        });
        if service_present != service_state.is_some() {
            return Err("OpenSSH Server 服务状态字段互相矛盾。".into());
        }
        if let Some(state) = service_state.as_deref() {
            if !matches!(
                state,
                "Stopped"
                    | "StartPending"
                    | "StopPending"
                    | "Running"
                    | "ContinuePending"
                    | "PausePending"
                    | "Paused"
            ) {
                return Err("Windows 返回了未知的 sshd 服务状态。".into());
            }
        }
        if let Some(startup_type) = startup_type.as_deref() {
            if !matches!(
                startup_type,
                "Boot" | "System" | "Automatic" | "Manual" | "Disabled" | "Unknown"
            ) {
                return Err("Windows 返回了未知的 sshd 启动类型。".into());
            }
        }

        let official_path = source_path.as_deref().is_some_and(official_openssh_path);
        let official_version = source_version
            .as_deref()
            .is_some_and(|version| !version.trim().is_empty());
        let microsoft_signature = signature_status.as_deref() == Some("Valid")
            && signature_subject.as_deref().is_some_and(|subject| {
                let subject = subject.to_ascii_lowercase();
                subject.contains("microsoft corporation") || subject.contains("microsoft windows")
            });
        let official_binary =
            official_binary_hint && official_path && official_version && microsoft_signature;
        // Unknown（只读查询权限不足）时，只有微软程序、服务路径、配置和实际监听都完整核验后才可推断就绪；
        // 这样已就绪服务不会仅因 Get-WindowsCapability 需要提权而反复要求管理员授权。
        // 微软独立安装版不登记 Windows 可选功能；不能因此安装第二份或覆盖已有服务。
        let independent_install = source_path.as_deref().is_some_and(|path| {
            official_openssh_path(path)
                && !path
                    .replace('/', "\\")
                    .to_ascii_lowercase()
                    .contains("\\system32\\")
        });
        let capability_confirmed = matches!(capability_state, "Installed" | "Unknown")
            || independent_install && matches!(capability_state, "NotPresent" | "Removed");
        let ready = capability_confirmed
            && service_present
            && service_state.as_deref() == Some("Running")
            && official_binary
            && service_path_official
            && configuration_valid
            && effective_port.is_some_and(|port| listening_ports.contains(&port));
        Ok(Self {
            feature_id: OPENSSH_SERVER_ID.into(),
            capability_state: capability_state.into(),
            service_present,
            service_state,
            startup_type,
            source_path,
            source_version,
            signature_status,
            signature_subject,
            official_binary,
            service_path_official,
            effective_port,
            listening_ports,
            configuration_valid,
            configuration_path: optional_string_if_present(&value, "configuration_path")?,
            authorized_keys_path,
            authorized_keys_exists,
            account_name,
            account_ssh_username,
            account_sid,
            account_is_admin,
            account_is_entra,
            ready,
            project_access_enabled,
            project_access_valid,
            project_network_scope,
            pairing_access_enabled,
            pairing_access_valid,
            pairing_network_scope,
            discovery_access_enabled: value["discovery_access_enabled"].as_bool().unwrap_or(false),
            discovery_access_valid: value["discovery_access_valid"].as_bool().unwrap_or(false),
            existing_ssh_inbound_rules,
            local_lan_addresses,
            local_lan_scopes,
            fields: [
                ("feature_id".into(), "Windows 系统功能固定编号".into()),
                ("discovery_access_enabled".into(), "本程序多播发现规则是否启用".into()),
                ("discovery_access_valid".into(), "发现规则的程序、UDP 5353 端口和精确局域网范围是否匹配".into()),
                ("capability_state".into(), "OpenSSH Server 可选功能状态；Unknown 表示本次只读查询未提权".into()),
                ("service_present".into(), "sshd 系统服务是否存在".into()),
                ("service_state".into(), "sshd 当前运行状态".into()),
                ("startup_type".into(), "sshd 现有启动类型；本检查只读取".into()),
                ("source_path".into(), "现有微软系统版或独立安装版 OpenSSH 服务程序位置".into()),
                ("source_version".into(), "OpenSSH Server 文件版本".into()),
                ("signature_status".into(), "OpenSSH Server 程序签名验证状态".into()),
                ("signature_subject".into(), "OpenSSH Server 程序签名主体".into()),
                ("official_binary".into(), "微软默认安装路径、非空文件版本及微软签名证据是否齐全".into()),
                ("service_path_official".into(), "sshd 服务路径是否指向微软系统程序".into()),
                ("effective_port".into(), "sshd 有效配置端口".into()),
                ("listening_ports".into(), "sshd 进程实际监听端口".into()),
                ("configuration_valid".into(), "sshd -T 通过，或未变化的官方运行服务已加载有效配置".into()),
                ("configuration_path".into(), "sshd 服务实际加载的配置文件路径".into()),
                ("authorized_keys_path".into(), "当前 Windows 账户公钥文件路径".into()),
                ("authorized_keys_exists".into(), "当前账户公钥文件是否存在".into()),
                ("account_name".into(), "当前 Windows 登录账户名称；不通过用户目录名推断".into()),
                ("account_ssh_username".into(), "由当前账户 SID 解析的 OpenSSH 登录名；不使用用户目录名或邮箱".into()),
                ("account_sid".into(), "当前登录账户安全标识符；用于配对密钥绑定".into()),
                ("account_is_admin".into(), "当前账户是否属于本机 Administrators 组".into()),
                ("account_is_entra".into(), "当前登录身份是否为 Windows OpenSSH 不支持的 Entra 账户".into()),
                ("ready".into(), "功能已安装且微软路径、版本、签名、服务路径、有效配置和实际监听端口均通过基础检查".into()),
                ("project_access_enabled".into(), "Voice Terminal 专用入站规则是否启用".into()),
                ("project_access_valid".into(), "微软 sshd 程序、当前有效端口和精确局域网范围是否匹配本程序规则".into()),
                ("project_network_scope".into(), "Voice Terminal 专用规则限定的局域网子网".into()),
                ("pairing_access_enabled".into(), "Voice Terminal 配对 TCP 入口规则是否启用".into()),
                ("pairing_access_valid".into(), "本程序配对程序、固定端口 18766 和精确局域网范围是否匹配".into()),
                ("pairing_network_scope".into(), "配对入口规则允许的活动局域网子网".into()),
                ("existing_ssh_inbound_rules".into(), "其他主体拥有且可能匹配 SSH 端口的启用规则；只读报告，不由本程序修改".into()),
                ("local_lan_addresses".into(), "当前可用于局域网发现的活动物理网卡私有地址".into()),
                ("local_lan_scopes".into(), "本程序可限制到的活动物理网卡精确局域网子网".into()),
            ]
            .into_iter()
            .collect(),
        })
    }
}

fn official_openssh_path(path: &str) -> bool {
    let normalized = path.replace('/', "\\").to_ascii_lowercase();
    let system_root = std::env::var("SystemRoot").unwrap_or_else(|_| "C:\\Windows".into());
    if normalized
        == format!(
            "{}\\system32\\openssh\\sshd.exe",
            system_root.trim_end_matches('\\')
        )
        .to_ascii_lowercase()
    {
        return true;
    }
    ["ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"]
        .iter()
        .filter_map(|name| std::env::var(name).ok())
        .chain(["C:\\Program Files".into(), "C:\\Program Files (x86)".into()])
        .any(|root| {
            ["OpenSSH", "OpenSSH-Win64", "OpenSSH-Win32", "OpenSSH-ARM64"]
                .iter()
                .any(|folder| {
                    normalized
                        == format!("{}\\{folder}\\sshd.exe", root.trim_end_matches('\\'))
                            .to_ascii_lowercase()
                })
        })
}

fn optional_string(value: &serde_json::Value, field: &str) -> Result<Option<String>, String> {
    let item = value
        .get(field)
        .ok_or_else(|| format!("OpenSSH Server 状态缺少 {field}（字段必须存在，可为 null）。"))?;
    match item {
        serde_json::Value::Null => Ok(None),
        serde_json::Value::String(value) => Ok(Some(value.clone())),
        _ => Err(format!("OpenSSH Server 状态中的 {field}（字段）类型无效。")),
    }
}

/// bounded_ssh_rule_detail（裁剪 SSH 规则摘要）：压平换行并限制字符数，避免系统规则内容淹没设置页。
fn bounded_ssh_rule_detail(value: &str) -> String {
    let normalized = value.split_whitespace().collect::<Vec<_>>().join(" ");
    let characters = normalized.chars().collect::<Vec<_>>();
    if characters.len() > MAX_SSH_RULE_DETAIL_CHARS {
        characters
            .iter()
            .take(MAX_SSH_RULE_DETAIL_CHARS - 1)
            .copied()
            .chain(std::iter::once('…'))
            .collect()
    } else {
        normalized
    }
}

fn optional_string_if_present(
    value: &serde_json::Value,
    field: &str,
) -> Result<Option<String>, String> {
    match value.get(field) {
        None | Some(serde_json::Value::Null) => Ok(None),
        Some(serde_json::Value::String(value)) => Ok(Some(value.clone())),
        Some(_) => Err(format!("OpenSSH Server 状态中的 {field}（字段）类型无效。")),
    }
}

/// require_local_runtime（本地服务门禁）：仅本地运行或未配置时允许检查本机 SSH 服务。
pub fn require_local_runtime(
    snapshot: &crate::device_config::RuntimeSnapshot,
) -> Result<(), String> {
    if snapshot.config_state == "missing"
        || snapshot.config_state == "valid"
            && snapshot.mode == Some(crate::device_config::RunMode::Local)
    {
        Ok(())
    } else {
        Err("仅允许在设备配置有效且已选择“本地运行”模式时管理 OpenSSH Server。".into())
    }
}

/// open_ssh_server_status（检查主机功能）：仅查询 capability、服务存在性和当前服务状态。
pub fn open_ssh_server_status() -> Result<OpenSshServerStatus, String> {
    open_ssh_server_status_with_details(true)
}

/// open_ssh_server_runtime_status（运行所需检查）：只检查官方服务与项目规则，不枚举全机防火墙。
static RUNTIME_STATUS_CACHE: std::sync::Mutex<Option<(std::time::Instant, OpenSshServerStatus)>> =
    std::sync::Mutex::new(None);

pub fn open_ssh_server_runtime_status() -> Result<OpenSshServerStatus, String> {
    cached_open_ssh_server_runtime_status(false)
}

/// cached_open_ssh_server_runtime_status（合并同时检查）：短时复用同进程快照；显式刷新绕过缓存。
pub fn cached_open_ssh_server_runtime_status(refresh: bool) -> Result<OpenSshServerStatus, String> {
    let mut cache = RUNTIME_STATUS_CACHE
        .lock()
        .map_err(|_| "SSH 状态检查锁不可用。")?;
    if !refresh {
        if let Some((checked, status)) = cache.as_ref() {
            if checked.elapsed() < std::time::Duration::from_secs(10) {
                return Ok(status.clone());
            }
        }
    }
    let status = open_ssh_server_status_with_details(false)?;
    *cache = Some((std::time::Instant::now(), status.clone()));
    Ok(status)
}

fn remember_runtime_status(status: &OpenSshServerStatus) {
    if let Ok(mut cache) = RUNTIME_STATUS_CACHE.lock() {
        *cache = Some((std::time::Instant::now(), status.clone()));
    }
}

fn open_ssh_server_status_with_details(details: bool) -> Result<OpenSshServerStatus, String> {
    #[cfg(windows)]
    {
        let product_executable =
            std::env::current_exe().map_err(|error| format!("无法定位本程序正式入口：{error}"))?;
        let product_executable_b64 = base64::engine::general_purpose::STANDARD
            .encode(product_executable.to_string_lossy().as_bytes());
        let script = r#"
$systemModuleRoot = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\Modules'
# PSModulePath（PowerShell 模块搜索路径）：使用所选解释器及 Windows 自带模块，不继承其他解释器的模块路径。
$env:PSModulePath = (Join-Path $PSHOME 'Modules') + ';' + $systemModuleRoot
# PSModuleAutoLoadingPreference（模块自动加载策略）：只在本次只读脚本进程中启用模块自动加载。
$PSModuleAutoLoadingPreference = 'All'
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
{PROJECT_RULE_HELPERS}
{OPENSSH_SERVICE_HELPERS}
$service = Get-Service -Name 'sshd' -ErrorAction SilentlyContinue
$nativeService = $null
$startupType = $null; $servicePathOfficial = $false; $serviceProcessId = 0
if ($null -ne $service) {
    $nativeService = Get-CimInstance -ClassName Win32_Service -Filter "Name='sshd'" -ErrorAction SilentlyContinue
    if ($null -ne $nativeService) {
        $startupType = [string]$nativeService.StartMode
        $serviceProcessId = [int]$nativeService.ProcessId
    }
}
$layout = Get-ProjectOpenSshLayout $nativeService
$systemOpenSsh = if ($layout.SourcePath) { $layout.SourcePath } else { Join-Path $env:SystemRoot 'System32\OpenSSH\sshd.exe' }
$sourcePath = $layout.SourcePath; $sourceVersion = $layout.SourceVersion
$signatureStatus = $layout.SignatureStatus; $signatureSubject = $layout.SignatureSubject
$officialBinary = $layout.OfficialBinary; $servicePathOfficial = $layout.ServicePathOfficial
$sshdConfigPath = $layout.ConfigPath
$capabilityState = 'Unknown'
if (-not $officialBinary -or $null -eq $service) {
    try { $capabilityState = [string](Get-WindowsCapability -Online -Name '{OPENSSH_CAPABILITY}' -ErrorAction Stop).State } catch { }
}
$physicalInterfaces = @(Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'Up' } |
    Select-Object -ExpandProperty ifIndex -Unique)
$privateInterfaces = @(Get-NetConnectionProfile -ErrorAction SilentlyContinue |
    Where-Object { [string]$_.NetworkCategory -in @('Private','DomainAuthenticated') } | Select-Object -ExpandProperty InterfaceIndex -Unique)
$localLanAddresses = @(); $localLanScopes = @()
foreach ($address in @(Get-NetIPAddress -AddressFamily IPv4,IPv6 -Type Unicast -ErrorAction SilentlyContinue)) {
    if ($physicalInterfaces -notcontains $address.InterfaceIndex -or $privateInterfaces -notcontains $address.InterfaceIndex -or $address.PrefixLength -le 0) { continue }
    $ip = [System.Net.IPAddress]::Parse([string]$address.IPAddress)
    if ([System.Net.IPAddress]::IsLoopback($ip) -or $ip.IsIPv6LinkLocal -or $ip.IsIPv6Multicast -or
        ([string]$address.IPAddress -like '169.254.*')) { continue }
    if ($ip.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and
        [string]$address.IPAddress -notmatch '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)') { continue }
    if ($ip.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetworkV6 -and
        [string]$address.IPAddress -notmatch '^(?i:fc|fd)') { continue }
    $localLanAddresses += [string]$address.IPAddress
    $bytes = $ip.GetAddressBytes()
    $remaining = [int]$address.PrefixLength
    for ($index=0; $index -lt $bytes.Length; $index++) {
        $take = [Math]::Min(8, [Math]::Max(0, $remaining))
        $mask = if ($take -eq 0) { 0 } else { 256 - [Math]::Pow(2, 8 - $take) }
        $bytes[$index] = [byte]([int]$bytes[$index] -band [int]$mask)
        $remaining -= $take
    }
    $localLanScopes += "$([System.Net.IPAddress]::new($bytes).ToString())/$($address.PrefixLength)"
}
$localLanAddresses = @($localLanAddresses | Sort-Object -Unique)
$localLanScopes = @($localLanScopes | Sort-Object -Unique)
$configurationValid = $false; $effectivePort = $null
if ($officialBinary -and $layout.ArgumentsValid) {
    # Windows PowerShell 5 treats native stderr as a NativeCommandError. The
    # OpenSSH probe can emit stderr when this unelevated account cannot read
    # protected host keys; that must not abort the whole read-only snapshot.
    $probe = Invoke-ProjectOpenSshProbe $layout
    $effective = $probe.Lines; $effectiveExitCode = $probe.ExitCode
    $configurationValid = $effectiveExitCode -eq 0
    if ($configurationValid) {
        $portLine = $effective | Where-Object { $_ -match '^port\s+[0-9]+$' } | Select-Object -First 1
        if ($portLine) { $effectivePort = [int](($portLine -split '\s+')[1]) }
        if ($null -eq $effectivePort) { $configurationValid = $false }
    }
}
$listeningPorts = @()
if ($serviceProcessId -gt 0) {
    $listeningPorts = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.OwningProcess -eq $serviceProcessId } | Select-Object -ExpandProperty LocalPort -Unique)
}
# 普通用户通常不能读取 ProgramData 下的 SSH 私钥，因此 sshd -T 可能拒绝输出。
# 若微软服务已在运行、主配置从服务启动后未变且没有 Include（外部配置），可用实际监听端口核验已加载配置。
if (-not $configurationValid -and $officialBinary -and $layout.ArgumentsValid -and $layout.SimpleConfig -and $serviceProcessId -gt 0 -and $null -ne $service -and $service.Status -eq 'Running' -and $servicePathOfficial) {
    $serviceProcess = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId=$serviceProcessId" -ErrorAction SilentlyContinue
    $sshdConfig = Get-Item -LiteralPath $sshdConfigPath -ErrorAction SilentlyContinue
    if ($null -ne $serviceProcess -and $null -ne $sshdConfig -and $sshdConfig.LastWriteTimeUtc -le ([datetime]$serviceProcess.CreationDate).ToUniversalTime()) {
        # 只有完整读取且读取前后的文件元数据一致时，才用主配置推断已加载端口；读取失败必须保持未验证。
        $configReadSuccessfully = $false
        $configContents = ''
        $serviceStartedAt = ([datetime]$serviceProcess.CreationDate).ToUniversalTime()
        try {
            $configBeforeRead = Get-Item -LiteralPath $sshdConfigPath -ErrorAction Stop
            if (($configBeforeRead.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'sshd_config must not be a reparse point' }
            if ($configBeforeRead.LastWriteTimeUtc -gt $serviceStartedAt) { throw 'sshd_config changed after sshd started' }
            $configContents = [string](Get-Content -LiteralPath $sshdConfigPath -Raw -ErrorAction Stop)
            $configAfterRead = Get-Item -LiteralPath $sshdConfigPath -ErrorAction Stop
            if (($configAfterRead.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
                $configBeforeRead.Length -ne $configAfterRead.Length -or
                $configBeforeRead.LastWriteTimeUtc -ne $configAfterRead.LastWriteTimeUtc -or
                $configAfterRead.LastWriteTimeUtc -gt $serviceStartedAt) { throw 'sshd_config changed while being read' }
            $configReadSuccessfully = $true
        } catch { $configReadSuccessfully = $false }
        if ($configReadSuccessfully) {
            $activeConfigLines = @(($configContents -split "`r?`n") |
                ForEach-Object { ([string]$_ -replace '#.*$', '').Trim() } | Where-Object { $_ })
            $hasConfigInclude = @($activeConfigLines | Where-Object { $_ -match '^Include\s+' }).Count -gt 0
            $portLines = @($activeConfigLines | Where-Object { $_ -match '^Port\s+[0-9]+\s*$' })
            if (-not $hasConfigInclude -and $portLines.Count -le 1) {
                $loadedPort = 22
                if ($portLines.Count -eq 1) { $loadedPort = [int](($portLines[0] -split '\s+')[1]) }
                if ($null -ne $layout.PortOverride) { $loadedPort = $layout.PortOverride }
                if ($loadedPort -gt 0 -and $listeningPorts -contains $loadedPort) {
                    $configurationValid = $true
                    $effectivePort = $loadedPort
                }
            }
        }
    }
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$accountName = [string]$identity.Name
$accountSid = [string]$identity.User.Value
$accountIsAdmin = Test-ProjectAdministratorAccount $identity
$accountIsEntra = $accountName -match '^(?i:AzureAD)\\'
$localAccount = $null
if ($null -ne (Get-Command Get-LocalUser -ErrorAction SilentlyContinue)) {
    $localAccount = Get-LocalUser -SID $identity.User.Value -ErrorAction SilentlyContinue
}
$sshUsername = if ($null -ne $localAccount) { [string]$localAccount.Name } else { $accountName }
if (-not $sshUsername -or $sshUsername -notmatch '^[A-Za-z0-9._-]+(?:\\[A-Za-z0-9._-]+)?$') { $sshUsername = $null }
$profile = Get-CimInstance Win32_UserProfile -Filter "SID='$($identity.User.Value)'" -ErrorAction SilentlyContinue
$authorizedKeysPath = Resolve-ProjectOpenSshAuthorizedKeys $layout $accountIsAdmin $profile.LocalPath
$authorizedKeysExists = $authorizedKeysPath -and (Test-Path -LiteralPath $authorizedKeysPath -PathType Leaf)
# 只取项目三条规则；批量管道复用提供程序，避免三次规则枚举和重复过滤器查询。
$ownedRules = @(Get-NetFirewallRule -PolicyStore ActiveStore -Name @('VoiceTerminal-OpenSSH-Server-In-TCP','VoiceTerminal-Device-Pairing-TCP-In','VoiceTerminal-Device-Discovery-UDP-In') -ErrorAction SilentlyContinue)
$projectRule = $ownedRules | Where-Object { $_.Name -eq 'VoiceTerminal-OpenSSH-Server-In-TCP' } | Select-Object -First 1
$pairingRule = $ownedRules | Where-Object { $_.Name -eq 'VoiceTerminal-Device-Pairing-TCP-In' } | Select-Object -First 1
$discoveryRule = $ownedRules | Where-Object { $_.Name -eq 'VoiceTerminal-Device-Discovery-UDP-In' } | Select-Object -First 1
$script:ProjectRuleFilters = @{}
foreach ($rule in $ownedRules) { $script:ProjectRuleFilters[[string]$rule.InstanceID] = @{} }
foreach ($filter in @($ownedRules | Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue)) {
    $script:ProjectRuleFilters[[string]$filter.InstanceID]['Application'] = $filter
}
foreach ($filter in @($ownedRules | Get-NetFirewallPortFilter -ErrorAction SilentlyContinue)) {
    $script:ProjectRuleFilters[[string]$filter.InstanceID]['Port'] = $filter
}
foreach ($filter in @($ownedRules | Get-NetFirewallAddressFilter -ErrorAction SilentlyContinue)) {
    $script:ProjectRuleFilters[[string]$filter.InstanceID]['Address'] = $filter
}
$expectedDesktopPath = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{PRODUCT_EXE_B64}'))
$projectScope = @()
$pairingScope = @()
if ($null -ne $projectRule) {
    $projectScope = @($script:ProjectRuleFilters[[string]$projectRule.InstanceID]['Address'].RemoteAddress | Where-Object { $_ -and $_ -ne 'Any' })
}
if ($null -ne $pairingRule) {
    $pairingScope = @($script:ProjectRuleFilters[[string]$pairingRule.InstanceID]['Address'].RemoteAddress | Where-Object { $_ -and $_ -ne 'Any' })
}
$projectAccessValid = Test-ProjectFirewallRule $projectRule $systemOpenSsh $effectivePort $localLanScopes
$pairingAccessValid = Test-ProjectFirewallRule $pairingRule $expectedDesktopPath {DEVICE_PAIRING_PORT} $localLanScopes
$discoveryAccessValid = Test-ProjectFirewallRule $discoveryRule $expectedDesktopPath 5353 $localLanScopes 'UDP'
if (-not ($projectAccessValid -and $pairingAccessValid -and $discoveryAccessValid)) {
    $activeProfiles = @(Get-NetConnectionProfile -ErrorAction SilentlyContinue | Where-Object {
        $physicalInterfaces -contains $_.InterfaceIndex -and [string]$_.NetworkCategory -in @('Private','DomainAuthenticated')
    } | ForEach-Object { if ([string]$_.NetworkCategory -eq 'Private') { 'Private' } else { 'Domain' } } | Sort-Object -Unique)
    foreach ($profileName in $activeProfiles) {
        $policy = Get-NetFirewallProfile -PolicyStore ActiveStore -Name $profileName -ErrorAction Stop
        if ([string]$policy.Enabled -eq 'True' -and ([string]$policy.AllowLocalFirewallRules -eq 'False' -or [string]$policy.AllowInboundRules -eq 'False')) {
            throw '当前防火墙策略不允许本机入站规则生效；请由系统管理员检查策略，程序未反复申请授权或覆盖其他规则。'
        }
    }
}
$otherSshInboundRules = @()
if ({INCLUDE_EXTERNAL_RULES}) {
$sshProbePort = if ($null -ne $effectivePort) { [string]$effectivePort } else { '22' }
$enabledInboundRules = @(Get-NetFirewallRule -Direction Inbound -Enabled True -ErrorAction SilentlyContinue)
$portFiltersByRule = @{}
foreach ($filter in @(Get-NetFirewallPortFilter -All -ErrorAction SilentlyContinue)) {
    if ($filter.InstanceID) { $portFiltersByRule[[string]$filter.InstanceID] = $filter }
}
$applicationFiltersByRule = @{}
foreach ($filter in @(Get-NetFirewallApplicationFilter -All -ErrorAction SilentlyContinue)) {
    if ($filter.InstanceID) { $applicationFiltersByRule[[string]$filter.InstanceID] = $filter }
}
$addressFiltersByRule = @{}
foreach ($filter in @(Get-NetFirewallAddressFilter -All -ErrorAction SilentlyContinue)) {
    if ($filter.InstanceID) { $addressFiltersByRule[[string]$filter.InstanceID] = $filter }
}
foreach ($candidate in $enabledInboundRules) {
    if ($candidate.Name -in @('VoiceTerminal-OpenSSH-Server-In-TCP','VoiceTerminal-Device-Pairing-TCP-In')) { continue }
    $candidateId = [string]$candidate.InstanceID
    $portFilter = $portFiltersByRule[$candidateId]
    if ($null -eq $portFilter) { continue }
    $candidatePorts = @($portFilter.LocalPort | ForEach-Object { [string]$_ })
    $protocol = [string]$portFilter.Protocol
    if ($protocol -notin @('TCP', '6', 'Any', '256')) { continue }
    $portMatchesSsh = $candidatePorts -contains $sshProbePort -or $candidatePorts -contains 'Any'
    if (-not $portMatchesSsh) { continue }
    $applicationFilter = $applicationFiltersByRule[$candidateId]
    $candidatePrograms = if ($null -ne $applicationFilter) { @([string]$applicationFilter.Program) } else { @() }
    $matchesSshd = $candidatePrograms | Where-Object {
        $_ -and $_ -ne 'Any' -and [string]::Equals([IO.Path]::GetFullPath($_), [IO.Path]::GetFullPath($systemOpenSsh), [StringComparison]::OrdinalIgnoreCase)
    }
    $matchesSshRuleName = [string]$candidate.Name -match '(?i)(ssh|sshd|openssh)'
    $allowsAnyProgramOnSshPort = ($candidatePrograms.Count -eq 0 -or $candidatePrograms -contains 'Any') -and
        ($candidatePorts -contains $sshProbePort -or $candidatePorts -contains 'Any')
    if (-not ($matchesSshd -or $matchesSshRuleName -or $allowsAnyProgramOnSshPort)) { continue }
    $addressFilter = $addressFiltersByRule[$candidateId]
    $candidateRemote = if ($null -ne $addressFilter) {
        @($addressFilter.RemoteAddress | ForEach-Object { [string]$_ } | Where-Object { $_ }) -join ','
    } else { '' }
    $candidateProfile = @($candidate.Profile) -join ','
    $otherSshInboundRules += "$($candidate.Name) (Profile=$candidateProfile; LocalPort=$($candidatePorts -join ','); RemoteAddress=$candidateRemote)"
    if ($otherSshInboundRules.Count -ge {MAX_EXISTING_SSH_RULES}) { break }
}
}
@{
    capability_state = $capabilityState
    service_present = ($null -ne $service)
    service_state = if ($null -ne $service) { [string]$service.Status } else { $null }
    startup_type = $startupType
    source_path = $sourcePath
    source_version = $sourceVersion
    signature_status = $signatureStatus
    signature_subject = $signatureSubject
    official_binary = $officialBinary
    service_path_official = $servicePathOfficial
    effective_port = $effectivePort
    listening_ports = @($listeningPorts)
    configuration_valid = $configurationValid
    configuration_path = $sshdConfigPath
    authorized_keys_path = $authorizedKeysPath
    authorized_keys_exists = [bool]$authorizedKeysExists
    account_name = $accountName
    account_ssh_username = $sshUsername
    account_sid = $accountSid
    account_is_admin = [bool]$accountIsAdmin
    account_is_entra = [bool]$accountIsEntra
    project_access_enabled = ($null -ne $projectRule -and $projectRule.Enabled -eq 'True')
    project_access_valid = [bool]$projectAccessValid
    project_network_scope = @($projectScope)
    pairing_access_enabled = ($null -ne $pairingRule -and $pairingRule.Enabled -eq 'True')
    pairing_access_valid = [bool]$pairingAccessValid
    pairing_network_scope = @($pairingScope)
    discovery_access_enabled = ($null -ne $discoveryRule -and $discoveryRule.Enabled -eq 'True')
    discovery_access_valid = [bool]$discoveryAccessValid
    existing_ssh_inbound_rules = @($otherSshInboundRules)
    local_lan_addresses = @($localLanAddresses)
    local_lan_scopes = @($localLanScopes)
} | ConvertTo-Json -Compress
"#.replace("{OPENSSH_CAPABILITY}", OPENSSH_CAPABILITY)
            .replace("{INCLUDE_EXTERNAL_RULES}", if details { "$true" } else { "$false" })
            .replace("{PROJECT_RULE_HELPERS}", FIREWALL_VALIDATION_SCRIPT)
            .replace("{OPENSSH_SERVICE_HELPERS}", OPENSSH_SERVICE_SCRIPT)
            .replace("{PRODUCT_EXE_B64}", &product_executable_b64)
            .replace("{DEVICE_PAIRING_PORT}", &DEVICE_PAIRING_PORT.to_string())
            .replace("{MAX_EXISTING_SSH_RULES}", &MAX_EXISTING_SSH_RULES.to_string());
        let script = format!("{ACCOUNT_IDENTITY_SCRIPT}\n{script}");
        let output = run_powershell(&script, std::time::Duration::from_secs(60))?;
        let value = serde_json::from_str(&output)
            .map_err(|_| "OpenSSH Server 状态查询返回了无效 JSON。".to_owned())?;
        OpenSshServerStatus::from_json(value)
    }
    #[cfg(not(windows))]
    {
        Err("OpenSSH Server 系统功能管理只支持 Windows 主机。".into())
    }
}

/// enable_open_ssh_server（显式启用主机功能）：仅由共享主机专用管理任务调用并触发系统 UAC。
/// Windows 安装可选功能时可能创建微软默认 SSH 入站规则；仅本次全新生成且此前不存在的规则会被禁用。
/// 保留既有启动类型；显式准备禁用的服务时改为手动，启动失败则恢复禁用。
/// 不覆盖程序专用防火墙规则、账户公钥或配置文件。
pub fn enable_open_ssh_server() -> Result<OpenSshServerStatus, String> {
    enable_open_ssh_server_with_progress(|_| {})
}

/// enable_open_ssh_server_with_progress（显式启用主机功能并报告等待心跳）：不改变提权或系统安装行为。
pub fn enable_open_ssh_server_with_progress<F>(on_wait: F) -> Result<OpenSshServerStatus, String>
where
    F: FnMut(std::time::Duration),
{
    #[cfg(windows)]
    {
        let status = prepare_open_ssh_server_with_progress(
            open_ssh_server_status,
            |_script, on_wait| {
                let code = if crate::system_maintenance::elevated()? {
                    run_maintenance_script(
                        &open_ssh_server_install_script(),
                        crate::system_maintenance::Operation::PrepareOpenSsh {}.execution_timeout(),
                        on_wait,
                    )?
                } else {
                    crate::system_maintenance::execute(
                        crate::system_maintenance::Operation::PrepareOpenSsh {},
                        on_wait,
                    )?
                };
                elevated_exit_result(code)
            },
            on_wait,
        )?;
        remember_runtime_status(&status);
        Ok(status)
    }
    #[cfg(not(windows))]
    {
        let _ = on_wait;
        Err("OpenSSH Server 系统功能管理只支持 Windows 主机。".into())
    }
}

fn capability_is_pending(state: &str) -> bool {
    matches!(state, "InstallPending" | "UninstallPending" | "Staged")
}

fn open_ssh_server_not_ready_error(status: &OpenSshServerStatus) -> String {
    if capability_is_pending(&status.capability_state) {
        "OpenSSH Server Windows 功能仍处于待处理状态；请完成 Windows 维护或重启后刷新状态，再决定是否重试。".into()
    } else if status.service_state.as_deref() == Some("Running")
        && status.official_binary
        && status.service_path_official
        && !status.configuration_valid
    {
        "微软 OpenSSH Server 已安装并运行，但有效配置检查未通过或当前账户无法读取主机密钥；程序未将其判定为就绪，也未扩大网络访问范围。请检查 sshd 配置、主机密钥权限及 Windows 策略后重试。".into()
    } else {
        "OpenSSH Server 管理任务已退出，但状态复核未确认 sshd 正在运行。请刷新状态并检查 Windows 功能源、服务状态及策略。".into()
    }
}

#[cfg(test)]
fn prepare_open_ssh_server_with<Q, E>(query: Q, elevate: E) -> Result<OpenSshServerStatus, String>
where
    Q: FnMut() -> Result<OpenSshServerStatus, String>,
    E: FnOnce(&str) -> Result<(), String>,
{
    prepare_open_ssh_server_with_progress(query, |script, _| elevate(script), |_| {})
}

fn prepare_open_ssh_server_with_progress<Q, E, P>(
    mut query: Q,
    elevate: E,
    mut on_wait: P,
) -> Result<OpenSshServerStatus, String>
where
    Q: FnMut() -> Result<OpenSshServerStatus, String>,
    E: FnOnce(&str, &mut dyn FnMut(std::time::Duration)) -> Result<(), String>,
    P: FnMut(std::time::Duration),
{
    let before = query()?;
    if before.ready {
        return Ok(before);
    }
    if capability_is_pending(&before.capability_state) {
        return Err(
            "OpenSSH Server Windows 功能正在维护或等待重启；请刷新状态，维护完成后再重试。".into(),
        );
    }
    if before.service_present && !before.service_path_official {
        return Err(
            "现有 sshd 服务路径未指向微软 OpenSSH Server；为避免启动可疑服务，未提权或修改系统。"
                .into(),
        );
    }
    if before.service_present && !before.official_binary {
        return Err("现有 sshd 服务的程序路径、版本或微软签名检查未通过；为避免启动可疑服务，未提权或修改系统。".into());
    }
    if before.capability_state == "Installed" && !before.official_binary {
        return Err("OpenSSH Server 已安装，但程序路径或微软签名检查未通过；为保留现有系统服务，未尝试启动或替换它。".into());
    }

    let script = open_ssh_server_install_script();
    elevate(&script, &mut on_wait)?;
    let after = query()?;
    if !after.ready {
        return Err(open_ssh_server_not_ready_error(&after));
    }
    Ok(after)
}

fn open_ssh_server_install_script() -> String {
    format!(
        r#"
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$systemModuleRoot = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\Modules'
# PSModulePath（PowerShell 模块搜索路径）：使用所选解释器及系统模块，供官方 DISM（映像维护）与防火墙命令使用。
$env:PSModulePath = (Join-Path $PSHOME 'Modules') + ';' + $systemModuleRoot
$PSModuleAutoLoadingPreference = 'All'
{service_helpers}
try {{
    $existingService = Get-Service -Name 'sshd' -ErrorAction SilentlyContinue
    $nativeService = if ($null -ne $existingService) {{ Get-CimInstance Win32_Service -Filter "Name='sshd'" -ErrorAction Stop }} else {{ $null }}
    $layout = Get-ProjectOpenSshLayout $nativeService
    if ($null -ne $existingService -and (-not $layout.OfficialBinary -or -not $layout.ServicePathOfficial -or -not $layout.ArgumentsValid)) {{ exit 15 }}
    # 已有微软独立安装版同样复用，不调用 DISM（系统映像维护）安装另一份系统服务。
    $capability = if ($null -ne $existingService) {{ @{{ State = 'Installed' }} }} else {{ Get-WindowsCapability -Online -Name '{OPENSSH_CAPABILITY}' -ErrorAction Stop }}
    if ($capability.State -in @('InstallPending', 'UninstallPending', 'Staged')) {{ exit 12 }}
    $wasInstalled = $capability.State -eq 'Installed'
    function Get-OpenSshDefaultFirewallRules {{
        @(Get-NetFirewallRule -ErrorAction Stop | Where-Object {{ [string]$_.Name -eq 'OpenSSH-Server-In-TCP' }})
    }}
    function Disable-NewOpenSshDefaultFirewallRule {{
        if ($wasInstalled -or $hadMicrosoftRule) {{ return }}
        $rules = @(Get-OpenSshDefaultFirewallRules)
        if ($rules.Count -eq 0) {{ return }}
        Set-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -Enabled False -ErrorAction Stop
        $verifiedRules = @(Get-OpenSshDefaultFirewallRules)
        if ($verifiedRules.Count -eq 0 -or @($verifiedRules | Where-Object {{ [string]$_.Enabled -eq 'True' }}).Count -gt 0) {{
            throw 'New Microsoft OpenSSH firewall rule could not be confirmed disabled'
        }}
    }}
    $hadMicrosoftRule = $false
    if (-not $wasInstalled) {{
        # 完整枚举成功且未找到同名规则，才记录为安装前不存在；查询故障时停止，不猜测。
        $hadMicrosoftRule = @(Get-OpenSshDefaultFirewallRules).Count -gt 0
    }}
    if (-not $wasInstalled) {{
        try {{
            $installed = Add-WindowsCapability -Online -Name '{OPENSSH_CAPABILITY}' -ErrorAction Stop
        }} catch {{
            try {{ Disable-NewOpenSshDefaultFirewallRule }} catch {{ exit 14 }}
            exit 13
        }}
        $installedCapability = Get-WindowsCapability -Online -Name '{OPENSSH_CAPABILITY}' -ErrorAction Stop
        if ($installed.RestartNeeded -or $installedCapability.State -in @('InstallPending', 'UninstallPending', 'Staged')) {{
            Disable-NewOpenSshDefaultFirewallRule
            exit 12
        }}
        if ($installedCapability.State -ne 'Installed') {{
            Disable-NewOpenSshDefaultFirewallRule
            exit 13
        }}
    }}
    # 仅对本任务全新安装生成的微软默认规则执行 fail-closed（默认拒绝）处理；既有规则保持原状。
    Disable-NewOpenSshDefaultFirewallRule
    # 下载后及启动前重新读取服务登记和微软签名，不能仅信任提权前的检查结果。
    $nativeService = Get-CimInstance Win32_Service -Filter "Name='sshd'" -ErrorAction Stop
    $layout = Get-ProjectOpenSshLayout $nativeService
    if ($null -eq $nativeService -or -not $layout.OfficialBinary -or -not $layout.ServicePathOfficial -or -not $layout.ArgumentsValid) {{ exit 15 }}
    if (-not $wasInstalled) {{ Set-Service -Name 'sshd' -StartupType Automatic -ErrorAction Stop }}
    $service = Get-Service -Name 'sshd' -ErrorAction Stop
    if ($service.Status -ne 'Running') {{
        $ports = if ($null -ne $layout.PortOverride) {{ @($layout.PortOverride) }} else {{ @(22) }}
        if ($wasInstalled -and (Test-Path -LiteralPath $layout.ConfigPath -PathType Leaf)) {{
            $probe = Invoke-ProjectOpenSshProbe $layout
            if ($probe.ExitCode -ne 0) {{ exit 17 }}
            $configuredPorts = @($probe.Lines | Where-Object {{ $_ -match '^port\s+[0-9]+$' }} | ForEach-Object {{ [int](($_ -split '\s+')[1]) }})
            if (-not $configuredPorts.Count) {{ exit 17 }}
            $ports = $configuredPorts
        }}
        if (@(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object {{ $ports -contains $_.LocalPort -and $_.OwningProcess -ne $nativeService.ProcessId }}).Count) {{ exit 16 }}
        $wasDisabled = [string]$nativeService.StartMode -eq 'Disabled'
        if ($wasDisabled) {{ Set-Service -Name 'sshd' -StartupType Manual -ErrorAction Stop }}
        try {{ Start-Service -Name 'sshd' -ErrorAction Stop }} catch {{
            if ($wasDisabled) {{ Set-Service -Name 'sshd' -StartupType Disabled -ErrorAction Stop }}
            throw
        }}
    }}
    exit 0
}} catch {{
    exit 1
}}
"#,
        service_helpers = OPENSSH_SERVICE_SCRIPT,
    )
}

fn elevated_exit_result(exit_code: u32) -> Result<(), String> {
    match exit_code {
        0 => Ok(()),
        12 => Err("OpenSSH Server 正处于 Windows 功能维护或重启等待状态；请刷新状态，待维护结束后再操作。".into()),
        13 => Err("Windows 官方 OpenSSH Server 功能源安装失败；请检查 Windows 功能源、Windows Update 或组织策略后重试。".into()),
        14 => Err("OpenSSH Server 安装产生的默认入站规则未能安全确认已关闭；服务未启动，请在 SSH 设置中检查规则后重试。".into()),
        15 => Err("现有或安装后的 SSH（安全外壳协议）服务路径、微软签名或服务参数复核失败；未启动或替换该服务，请检查原有安装。".into()),
        16 => Err("SSH（安全外壳协议）所需端口已被其他进程占用；未终止现有进程或修改端口，请处理冲突后重试。".into()),
        17 => Err("现有 SSH（安全外壳协议）服务的有效配置检查失败；未启动服务或覆盖配置，请检查配置与主机密钥权限。".into()),
        _ => Err("OpenSSH Server 系统功能安装或 sshd 启动失败；请刷新状态并检查 Windows 管理员权限、功能源和策略。".into()),
    }
}

/// OpenSshServerIdentity（官方 SSH 主机身份）：只读取微软 OpenSSH 主机公钥，不读取私钥。
#[cfg(feature = "ssh-device")]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OpenSshServerIdentity {
    pub public_key: String, // public_key（微软 OpenSSH Ed25519 主机公钥）。
    pub fingerprint_sha256: String, // fingerprint_sha256（主机公钥 SHA-256 指纹）。
}

/// open_ssh_server_identity（读取运行中主机公钥）：回环握手不认证、不读系统密钥文件、不请求授权。
#[cfg(feature = "ssh-device")]
pub async fn open_ssh_server_identity(port: u16) -> Result<OpenSshServerIdentity, String> {
    let public_key =
        crate::ssh_device::probe_loopback_host_public_key(port, std::time::Duration::from_secs(3))
            .await?;
    Ok(OpenSshServerIdentity {
        fingerprint_sha256: public_key
            .fingerprint(russh::keys::HashAlg::Sha256)
            .to_string(),
        public_key: public_key
            .to_openssh()
            .map_err(|_| "无法规范编码微软 OpenSSH 主机公钥。".to_string())?,
    })
}

// Windows 防火墙会把 CIDR（前缀子网）规范为点分掩码；统一语义后仍要求精确范围。
#[cfg(any(windows, test))]
const FIREWALL_VALIDATION_SCRIPT: &str = r#"
function ConvertTo-ProjectNetworkScope([string]$value) {
    $parts = $value.Split('/')
    if ($parts.Length -ne 2) { throw '必须是精确子网' }
    $address = [Net.IPAddress]::Parse($parts[0])
    $bytes = $address.GetAddressBytes()
    $prefix = 0
    if ($parts[1] -match '^\d+$') { $prefix = [int]$parts[1] }
    elseif ($bytes.Length -eq 4) {
        $mask = [Net.IPAddress]::Parse($parts[1]).GetAddressBytes()
        if ($mask.Length -ne 4) { throw '掩码地址族不匹配' }
        $zeroSeen = $false
        foreach ($byte in $mask) {
            for ($bit = 7; $bit -ge 0; $bit--) {
                if (($byte -band (1 -shl $bit)) -ne 0) {
                    if ($zeroSeen) { throw '掩码不连续' }
                    $prefix++
                } else { $zeroSeen = $true }
            }
        }
    } else { throw '子网前缀无效' }
    if ($prefix -le 0 -or $prefix -gt $bytes.Length * 8) { throw '子网前缀越界' }
    $remaining = $prefix
    for ($index = 0; $index -lt $bytes.Length; $index++) {
        $take = [Math]::Min(8, [Math]::Max(0, $remaining))
        $maskByte = if ($take -eq 0) { 0 } else { 256 - [Math]::Pow(2, 8 - $take) }
        $bytes[$index] = [byte]([int]$bytes[$index] -band [int]$maskByte)
        $remaining -= $take
    }
    return "$([Net.IPAddress]::new($bytes).ToString())/$prefix"
}
function Test-ProjectFirewallRule($rule, [string]$program, $port, $scopes, [string]$protocol = 'TCP') {
    try {
        if ($null -eq $rule -or [string]$rule.Enabled -ne 'True' -or
            [string]$rule.Direction -ne 'Inbound' -or [string]$rule.Action -ne 'Allow' -or
            $null -eq $port -or [int]$port -le 0) { return $false }
        $profiles = @(([string]$rule.Profile -split ',') | ForEach-Object { $_.Trim() } | Sort-Object -Unique)
        if (($profiles -join ',') -ne 'Domain,Private') { return $false }
        if ($null -ne $script:ProjectRuleFilters) {
            $cached = $script:ProjectRuleFilters[[string]$rule.InstanceID]
            if ($null -eq $cached) { return $false }
            $app = $cached['Application']; $filter = $cached['Port']; $remote = $cached['Address']
        } else {
            $app = Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $rule -ErrorAction Stop
            $filter = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $rule -ErrorAction Stop
            $remote = Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $rule -ErrorAction Stop
        }
        if ($null -eq $app -or $null -eq $filter -or $null -eq $remote -or
            -not [string]::Equals([IO.Path]::GetFullPath([string]$app.Program), [IO.Path]::GetFullPath($program), [StringComparison]::OrdinalIgnoreCase) -or
            [string]$filter.Protocol -notin @($protocol, $(if ($protocol -eq 'TCP') { '6' } else { '17' })) -or @($filter.LocalPort).Count -ne 1 -or
            [string]$filter.LocalPort -ne [string]$port) { return $false }
        $actual = @($remote.RemoteAddress | ForEach-Object { ConvertTo-ProjectNetworkScope ([string]$_) } | Sort-Object -Unique)
        $expected = @($scopes | ForEach-Object { ConvertTo-ProjectNetworkScope ([string]$_) } | Sort-Object -Unique)
        return $expected.Count -gt 0 -and ($actual -join ',') -eq ($expected -join ',')
    } catch { return $false }
}
"#;

/// set_open_ssh_project_access（设置程序 SSH 入站范围）：只维护本程序独占规则。
pub fn set_open_ssh_project_access(enabled: bool) -> Result<OpenSshServerStatus, String> {
    configure_project_access(enabled, true, false, false)
}

/// set_device_pairing_access（设置配对端口范围）：只维护本程序固定配对规则。
pub fn set_device_pairing_access(enabled: bool) -> Result<OpenSshServerStatus, String> {
    configure_project_access(enabled, false, true, false)
}

/// set_device_broadcast_access（设置广播网络规则）：一次授权维护 SSH、配对与多播发现规则。
pub fn set_device_broadcast_access(enabled: bool) -> Result<OpenSshServerStatus, String> {
    configure_project_access(enabled, true, true, true)
}

#[derive(Default)]
struct BroadcastRepairGate {
    failed: bool, // failed（本轮维修失败或取消后，等待用户主动重试）。
}

impl BroadcastRepairGate {
    fn repair<T>(
        &mut self,
        explicit: bool,
        action: impl FnOnce() -> Result<T, String>,
    ) -> Result<T, String> {
        if !explicit && self.failed {
            return Err("本机广播配置修复已失败或取消；已停止自动申请授权，请在设备设置中重新开启广播以重试。".into());
        }
        let result = action();
        self.failed = result.is_err();
        result
    }
}

static BROADCAST_REPAIR: std::sync::Mutex<BroadcastRepairGate> =
    std::sync::Mutex::new(BroadcastRepairGate { failed: false });

/// prepare_device_broadcast_access（广播检查与必要维修）：有效规则直接复用；只合并修复本程序拥有的系统配置。
pub fn prepare_device_broadcast_access(explicit: bool) -> Result<OpenSshServerStatus, String> {
    // 合并同时发生的自动启动、核心就绪和手动保存，避免相同故障产生多次 UAC（管理员授权）。
    let mut gate = BROADCAST_REPAIR
        .lock()
        .map_err(|_| "广播维修状态不可用。")?;
    let before = cached_open_ssh_server_runtime_status(explicit)?;
    prepare_broadcast_with(&mut gate, explicit, before, |_| {
        set_device_broadcast_access(true)
    })
}

fn prepare_broadcast_with(
    gate: &mut BroadcastRepairGate,
    explicit: bool,
    before: OpenSshServerStatus,
    repair: impl FnOnce(&OpenSshServerStatus) -> Result<OpenSshServerStatus, String>,
) -> Result<OpenSshServerStatus, String> {
    if before.ready
        && before.project_access_valid
        && before.pairing_access_valid
        && before.discovery_access_valid
    {
        gate.failed = false;
        return Ok(before);
    }
    // 广播启动只读检查依赖；下载与启用必须经过正式系统功能任务的用户确认。
    if !before.ready {
        return Err("微软 SSH（安全外壳协议）服务尚未就绪；请在设备设置中开启广播或点击“检查 SSH 服务”，确认下载或启动后再重试。".into());
    }
    if before.local_lan_addresses.is_empty() || before.local_lan_scopes.is_empty() {
        return Err(
            "没有可用的私有或域物理局域网；未申请管理员授权，请检查网络连接和网络类型。".into(),
        );
    }
    if before.account_is_entra || before.account_ssh_username.is_none() {
        return Err("当前 Windows 账户不支持受管 SSH 登录；未申请管理员授权。".into());
    }
    gate.repair(explicit, || repair(&before))
}

fn configure_project_access(
    enabled: bool,   // enabled（是否启用所选程序规则）。
    ssh: bool,       // ssh（是否维护 SSH 规则）。
    pairing: bool,   // pairing（是否维护配对规则）。
    discovery: bool, // discovery（是否维护多播发现规则）。
) -> Result<OpenSshServerStatus, String> {
    #[cfg(windows)]
    {
        let before = open_ssh_server_runtime_status()?;
        if enabled {
            if !before.ready {
                return Err("请先通过“检查 SSH 服务”确认微软 OpenSSH Server 基础就绪。".into());
            }
            if before.account_is_entra || before.account_ssh_username.is_none() {
                return Err(
                    "当前 Windows 账户没有可用的受管 OpenSSH 登录名；未创建入站规则。".into(),
                );
            }
            if before.local_lan_scopes.is_empty() || before.local_lan_addresses.is_empty() {
                return Err("没有可用于受限访问的私有或域物理局域网；请检查网络连接及 Windows 网络配置文件。".into());
            }
            if (!ssh || before.project_access_valid)
                && (!pairing || before.pairing_access_valid)
                && (!discovery || before.discovery_access_valid)
            {
                return Ok(before);
            }
        } else if (!ssh || !before.project_access_enabled)
            && (!pairing || !before.pairing_access_enabled)
            && (!discovery || !before.discovery_access_enabled)
        {
            return Ok(before);
        }
        let executable =
            std::env::current_exe().map_err(|error| format!("无法定位本程序正式入口：{error}"))?;
        let script = project_access_script(enabled, ssh, pairing, discovery, &before, &executable);
        let outcome = (|| {
            run_admin_operation(
                crate::system_maintenance::Operation::ProjectAccess {
                    enabled,
                    ssh,
                    pairing,
                    discovery,
                },
                &script,
            )?;
            let after = cached_open_ssh_server_runtime_status(true)?;
            if enabled
                && ((!ssh || after.project_access_valid)
                    && (!pairing || after.pairing_access_valid)
                    && (!discovery || after.discovery_access_valid))
                || !enabled
                    && ((!ssh || !after.project_access_enabled)
                        && (!pairing || !after.pairing_access_enabled)
                        && (!discovery || !after.discovery_access_enabled))
            {
                Ok(after)
            } else {
                Err("系统维护已结束，但本程序网络规则尚未实际生效；请检查防火墙策略。".into())
            }
        })();
        if let Err(error) = outcome {
            if let Ok(mut cache) = RUNTIME_STATUS_CACHE.lock() {
                *cache = None;
            }
            return Err(format!("本程序局域网防火墙规则配置失败：{error}"));
        }
        outcome
    }
    #[cfg(not(windows))]
    {
        let _ = (enabled, ssh, pairing, discovery);
        Err("OpenSSH Server 系统功能管理只支持 Windows 主机。".into())
    }
}

#[cfg(any(windows, test))]
fn project_access_script(
    enabled: bool,                // enabled（规则目标启用状态）。
    ssh: bool,                    // ssh（包含 SSH 规则）。
    pairing: bool,                // pairing（包含配对规则）。
    discovery: bool,              // discovery（包含多播发现规则）。
    status: &OpenSshServerStatus, // status（本轮只读复核结果）。
    executable: &std::path::Path, // executable（正式桌面程序）。
) -> String {
    let mut rules = Vec::new();
    if ssh {
        rules.push(format!("@{{ Name='VoiceTerminal-OpenSSH-Server-In-TCP'; Program=$sshProgram; Port={}; Protocol='TCP' }}", status.effective_port.unwrap_or(22)));
    }
    if pairing {
        rules.push(format!("@{{ Name='VoiceTerminal-Device-Pairing-TCP-In'; Program=$desktopProgram; Port={DEVICE_PAIRING_PORT}; Protocol='TCP' }}"));
    }
    if discovery {
        rules.push("@{ Name='VoiceTerminal-Device-Discovery-UDP-In'; Program=$desktopProgram; Port=5353; Protocol='UDP' }".into());
    }
    let program_b64 =
        base64::engine::general_purpose::STANDARD.encode(executable.to_string_lossy().as_bytes());
    let ssh_program_b64 = base64::engine::general_purpose::STANDARD
        .encode(status.source_path.as_deref().unwrap_or_default().as_bytes());
    let scope_b64 = base64::engine::general_purpose::STANDARD
        .encode(status.local_lan_scopes.join(",").as_bytes());
    let enabled = if enabled { "$true" } else { "$false" };
    let rules = rules.join("\n");
    format!(
        r#"
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
{helpers}
$desktopProgram = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{program_b64}'))
$sshProgram = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{ssh_program_b64}'))
$remoteScope = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{scope_b64}')) -split ','
$rules = @(
{rules}
)
$newlyEnabled = @()
try {{
foreach ($spec in $rules) {{
    $rule = Get-NetFirewallRule -Name $spec.Name -ErrorAction SilentlyContinue
    if (-not {enabled}) {{
        if ($null -ne $rule) {{
            Set-NetFirewallRule -Name $spec.Name -Enabled False -ErrorAction Stop
            $disabled = Get-NetFirewallRule -Name $spec.Name -ErrorAction Stop
            if ([string]$disabled.Enabled -ne 'False') {{ throw '程序规则关闭后的复核未通过' }}
        }}
        continue
    }}
    if ($spec.Port -le 0 -or $spec.Port -gt 65535) {{ throw '程序规则端口无效' }}
    if ($null -eq $rule -or [string]$rule.Enabled -ne 'True') {{ $newlyEnabled += $spec.Name }}
    if ($null -eq $rule) {{
        New-NetFirewallRule -Name $spec.Name -DisplayName $spec.Name -Group 'Voice Terminal' `
            -Program $spec.Program -Direction Inbound -Protocol $spec.Protocol -LocalPort $spec.Port `
            -RemoteAddress $remoteScope -Profile Private,Domain -Action Allow -Enabled True -ErrorAction Stop | Out-Null
    }} else {{
        Set-NetFirewallRule -Name $spec.Name -Enabled True -Direction Inbound -Action Allow -Profile Private,Domain -ErrorAction Stop
        Set-NetFirewallApplicationFilter -InputObject (Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $rule) `
            -Program $spec.Program -ErrorAction Stop
        Set-NetFirewallPortFilter -InputObject (Get-NetFirewallPortFilter -AssociatedNetFirewallRule $rule) `
            -Protocol $spec.Protocol -LocalPort $spec.Port -ErrorAction Stop
        Set-NetFirewallAddressFilter -InputObject (Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $rule) `
            -RemoteAddress $remoteScope -ErrorAction Stop
    }}
    $updated = Get-NetFirewallRule -Name $spec.Name -ErrorAction Stop
    if (-not (Test-ProjectFirewallRule $updated $spec.Program $spec.Port $remoteScope $spec.Protocol)) {{ throw '程序规则配置后的程序、端口或子网复核未通过' }}
}}
}} catch {{
    # 回收也在同一授权进程内完成，失败后不会再弹出第二轮 UAC（管理员授权）。
    foreach ($name in $newlyEnabled) {{ Set-NetFirewallRule -Name $name -Enabled False -ErrorAction Continue }}
    throw
}}
"#,
        helpers = FIREWALL_VALIDATION_SCRIPT
    )
}

/// install_project_device_key（登记受限项目公钥）：只增删带工作区和设备标记的授权行。
#[cfg(feature = "ssh-device")]
pub fn install_project_device_key(
    public_key: &str,
    workspace_id: &str,
    device_id: &str,
    build: &str,
    executable: &std::path::Path,
    owner_sid: &str,
    network_scopes: &[String],
) -> Result<(), String> {
    manage_project_device_key(
        true,
        public_key,
        workspace_id,
        device_id,
        build,
        executable,
        owner_sid,
        network_scopes,
    )
}

/// remove_project_device_key（撤销受限项目公钥）：只删除拥有标记和预期公钥均吻合的授权行。
#[cfg(feature = "ssh-device")]
pub fn remove_project_device_key(
    expected_public_key: &str, // expected_public_key（本轮已撤销登记的公钥，保护等待授权期间登记的新身份）。
    workspace_id: &str,
    device_id: &str,
    build: &str,
    executable: &std::path::Path,
    owner_sid: &str,
) -> Result<(), String> {
    manage_project_device_key(
        false,
        expected_public_key,
        workspace_id,
        device_id,
        build,
        executable,
        owner_sid,
        &[],
    )
}

#[cfg(feature = "ssh-device")]
fn manage_project_device_key(
    add: bool,
    public_key: &str,
    workspace_id: &str,
    device_id: &str,
    build: &str,
    executable: &std::path::Path,
    owner_sid: &str,
    network_scopes: &[String],
) -> Result<(), String> {
    if !crate::workspace::valid_workspace_id(workspace_id) {
        return Err("工作区编号格式无效。".into());
    }
    validate_ascii_id(device_id, 128, "设备编号")?;
    validate_ascii_id(build, 128, "程序构建编号")?;
    validate_sid(owner_sid)?;
    let key = russh::keys::PublicKey::from_openssh(public_key)
            .map_err(|_| "项目授权公钥不是有效 OpenSSH 格式。".to_string())?;
    if key.algorithm() != russh::keys::Algorithm::Ed25519 {
        return Err("项目受限 SSH 密钥只接受 Ed25519 公钥。".into());
    }
    let canonical_public_key = key.to_openssh().map_err(|_| "项目授权公钥无法规范编码。")?;
    let public_key = canonical_public_key.as_str();
    if add {
        if network_scopes.is_empty()
            || network_scopes
                .iter()
                .any(|scope| !valid_private_cidr(scope))
        {
            return Err("项目 authorized_keys 的来源必须是实际活动局域网子网。".into());
        }
    }
    let status = open_ssh_server_runtime_status()?;
    if !status.ready {
        return Err("微软 OpenSSH Server 尚未通过基础就绪检查。".into());
    }
    if status.account_is_entra {
        return Err(
            "当前是 Microsoft Entra 账户，Windows OpenSSH 不支持此账户的公钥登录；没有更换账户。"
                .into(),
        );
    }
    if status.account_sid.as_deref() != Some(owner_sid) {
        return Err("配对窗口账户 SID 与当前 Windows 登录账户不匹配。".into());
    }
    let authorized_keys_path = status.authorized_keys_path.as_deref().ok_or(
        "现有 SSH（安全外壳协议）配置使用自定义授权文件、条件或命令，或配置无法读取；程序未修改任何公钥，请核对微软默认公钥存储配置后重试。",
    )?;
    if add {
        let mut requested = network_scopes.to_vec();
        requested.sort();
        requested.dedup();
        let mut active = status.local_lan_scopes.clone();
        active.sort();
        active.dedup();
        if requested != active {
            return Err("设备公钥来源范围与当前实际局域网不匹配，请重新获取配对码。".into());
        }
    }
    let metadata = std::fs::symlink_metadata(executable)
        .map_err(|error| format!("无法检查桥接可执行文件：{error}"))?;
    if crate::workspace::metadata_reparse(&metadata) || !metadata.is_file() {
        return Err("SSH 强制命令目标必须是受管安装内的普通程序文件。".into());
    }
    let executable = crate::canonical_path(executable)?;
    let marker = format!("VoiceTerminal:{workspace_id}:{device_id}");
    let scopes = if add {
        let mut scopes = network_scopes.to_vec();
        scopes.extend(["127.0.0.1/32".into(), "::1/128".into()]);
        scopes.sort();
        scopes.dedup();
        scopes.join(",")
    } else {
        String::new()
    };
    let line = if add {
        let command =
            format!(
            "\"{}\" --ssh-device-bridge --workspace-id {} --owner-sid {} --device-id {} --bridge-protocol {}",
            executable.display(), workspace_id, owner_sid, device_id, crate::compatibility::DEVICE_BRIDGE_PROTOCOL_VERSION,
        );
        let escaped_command = command.replace('\\', "\\\\").replace('"', "\\\"");
        format!("from=\"{scopes}\",restrict,command=\"{escaped_command}\" {public_key} {marker}")
    } else {
        String::new()
    };
    let values = [
        ("ACTION", if add { "add" } else { "remove" }),
        ("EXPECTED_SID", owner_sid),
        ("MARKER", &marker),
        ("PUBLIC_KEY", public_key),
        ("LINE", &line),
        ("KEY_PATH", authorized_keys_path),
        (
            "ADMIN",
            if status.account_is_admin {
                "true"
            } else {
                "false"
            },
        ),
    ];
    let mut script = format!("{ACCOUNT_IDENTITY_SCRIPT}\n{AUTHORIZED_KEYS_SCRIPT}");
    for (name, value) in values {
        let encoded = base64::engine::general_purpose::STANDARD.encode(value.as_bytes());
        script = script.replace(&format!("@@{name}@@"), &encoded);
    }
    if status.account_is_admin {
        run_admin_operation(
            crate::system_maintenance::Operation::DeviceKey {
                add,
                public_key: public_key.into(),
                workspace_id: workspace_id.into(),
                device_id: device_id.into(),
                build: build.into(),
                owner_sid: owner_sid.into(),
                network_scopes: network_scopes.to_vec(),
            },
            &script,
        )?;
    } else {
        let output = run_powershell(&script, std::time::Duration::from_secs(30))?;
        if !output.contains("VoiceTerminalAuthorizedKeyUpdated") {
            return Err("当前账户授权公钥维护缺少固定完成回执。".into());
        }
    }
    Ok(())
}

#[cfg(feature = "ssh-device")]
fn validate_ascii_id(value: &str, maximum: usize, label: &str) -> Result<(), String> {
    let valid = !value.is_empty()
        && value.len() <= maximum
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'));
    if valid {
        Ok(())
    } else {
        Err(format!("{label}格式无效。"))
    }
}

#[cfg(feature = "ssh-device")]
fn validate_sid(value: &str) -> Result<(), String> {
    let fields = value.split('-').collect::<Vec<_>>();
    if fields.len() >= 4
        && fields[0] == "S"
        && fields
            .iter()
            .skip(1)
            .all(|field| !field.is_empty() && field.bytes().all(|byte| byte.is_ascii_digit()))
    {
        Ok(())
    } else {
        Err("当前 Windows 账户 SID（安全标识符）格式无效。".into())
    }
}

#[cfg(feature = "ssh-device")]
fn valid_private_cidr(value: &str) -> bool {
    let Some((address, prefix)) = value.split_once('/') else {
        return false;
    };
    let (Ok(address), Ok(prefix)) = (address.parse::<std::net::IpAddr>(), prefix.parse::<u8>())
    else {
        return false;
    };
    match address {
        std::net::IpAddr::V4(address) => address.is_private() && prefix <= 32,
        std::net::IpAddr::V6(address) => {
            (address.segments()[0] & 0xfe00) == 0xfc00 && prefix <= 128
        }
    }
}

/// ACCOUNT_IDENTITY_SCRIPT（账户组归属）：同时读取有效组及 UAC（用户账户控制）仅拒绝组。
/// 此判断只决定 SSH（安全远程连接）公钥存储位置，不代表当前进程已获管理员权限。
#[cfg(windows)]
const ACCOUNT_IDENTITY_SCRIPT: &str = r#"
function Test-ProjectAdministratorAccount {
    param($Identity)
    $groupTypes = @([Security.Claims.ClaimTypes]::GroupSid, [Security.Claims.ClaimTypes]::DenyOnlySid)
    return [bool](@($Identity.Claims | Where-Object {
        $groupTypes -contains $_.Type -and $_.Value -eq 'S-1-5-32-544'
    }).Count -gt 0)
}
"#;

#[cfg(windows)]
const AUTHORIZED_KEYS_SCRIPT: &str = r#"
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$temp = $null
try {
    $action = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('@@ACTION@@'))
    $expectedSid = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('@@EXPECTED_SID@@'))
    $marker = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('@@MARKER@@'))
    $publicKey = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('@@PUBLIC_KEY@@'))
    $line = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('@@LINE@@'))
    $adminExpected = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('@@ADMIN@@')) -eq 'true'
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if ([string]$identity.User.Value -ne $expectedSid) { throw '当前登录账户 SID 与受限公钥目标不匹配。' }
    if ([string]$identity.Name -match '^(?i:AzureAD)\\') { throw 'Microsoft Entra 账户不受当前 Windows OpenSSH 支持。' }
    $isAdmin = Test-ProjectAdministratorAccount $identity
    if ($isAdmin -ne $adminExpected) { throw '管理员公钥存储与当前账户组身份不匹配。' }
    $path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('@@KEY_PATH@@'))
    $userKeysPath = Join-Path $env:USERPROFILE '.ssh/authorized_keys'
    $adminKeysPath = Join-Path $env:ProgramData 'ssh/administrators_authorized_keys'
    if (-not [string]::Equals([IO.Path]::GetFullPath($path), [IO.Path]::GetFullPath($userKeysPath), [StringComparison]::OrdinalIgnoreCase) -and
        -not ($isAdmin -and [string]::Equals([IO.Path]::GetFullPath($path), [IO.Path]::GetFullPath($adminKeysPath), [StringComparison]::OrdinalIgnoreCase))) {
        throw '授权公钥目标不是已验证的微软默认公钥文件。'
    }
    $fullPath = [IO.Path]::GetFullPath($path)
    $cursor = [IO.DirectoryInfo]([IO.Path]::GetDirectoryName($fullPath))
    while ($null -ne $cursor) {
        if ($cursor.Exists -and ($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw '授权公钥父目录含符号链接或目录联接。' }
        $cursor = $cursor.Parent
    }
    $parent = [IO.Path]::GetDirectoryName($fullPath)
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        if ($action -eq 'remove') { Write-Output 'VoiceTerminalAuthorizedKeyUpdated'; exit 0 }
        [IO.Directory]::CreateDirectory($parent) | Out-Null
    }
    if (Test-Path -LiteralPath $fullPath) {
        $item = Get-Item -LiteralPath $fullPath -Force
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw '授权公钥目标不是普通文件。' }
        $oldBytes = [IO.File]::ReadAllBytes($fullPath)
        if ($oldBytes.Length -gt 1048576) { throw '授权公钥文件超过大小上限。' }
        $text = [Text.Encoding]::UTF8.GetString($oldBytes)
        $rows = @($text -split "`r?`n" | Where-Object { $_.Length -gt 0 })
        $acl = Get-Acl -LiteralPath $fullPath
        $allowed = if ($isAdmin) { @('S-1-5-18','S-1-5-32-544') } else { @($expectedSid,'S-1-5-18') }
        foreach ($entry in $acl.Access) {
            $entrySid = $null
            try { $entrySid = $entry.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } catch { throw '授权公钥 ACL 无法识别。' }
            if ($entry.IsInherited -or $allowed -notcontains $entrySid) { throw '授权公钥 ACL 含其他账户或继承权限；程序未覆盖现有规则。' }
        }
        if ($isAdmin -and -not ($acl.Access | Where-Object { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq 'S-1-5-18' })) {
            throw '管理员授权公钥文件 ACL 缺少 SYSTEM 权限。'
        }
    } else {
        if ($action -eq 'remove') { Write-Output 'VoiceTerminalAuthorizedKeyUpdated'; exit 0 }
        $oldBytes = [byte[]]@()
        $text = ''
        $rows = @()
    }
    # Windows PowerShell 5.1 使用 .NET Framework（系统运行库），不提供静态 HashData（摘要计算）。
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try { $beforeHash = [Convert]::ToBase64String($sha256.ComputeHash($oldBytes)) } finally { $sha256.Dispose() }
    $prefix = 'VoiceTerminal:'
    $owned = @($rows | Where-Object { $_.EndsWith(" $marker", [StringComparison]::Ordinal) })
    $otherRows = @($rows | Where-Object { -not $_.EndsWith(" $marker", [StringComparison]::Ordinal) })
    if ($action -eq 'add' -and $owned.Count -eq 1 -and [string]::Equals([string]$owned[0], $line, [StringComparison]::Ordinal)) {
        Write-Output 'VoiceTerminalAuthorizedKeyUpdated'; exit 0
    }
    if ($action -eq 'add') {
        if ($otherRows | Where-Object { $_.IndexOf($publicKey, [StringComparison]::Ordinal) -ge 0 }) { throw '该公钥已在非本程序授权行中出现；为避免通用 SSH 权限，未登记。' }
        $rows = @($otherRows + $line)
    } elseif ($action -eq 'remove') {
        if ($owned | Where-Object { -not $_.EndsWith(" $publicKey $marker", [StringComparison]::Ordinal) }) {
            throw '该设备的授权公钥已变化，拒绝移除新身份；请刷新后重新确认。'
        }
        $rows = $otherRows
    } else { throw '不支持的固定授权公钥操作。' }
    if ($action -eq 'remove' -and $owned.Count -eq 0) { Write-Output 'VoiceTerminalAuthorizedKeyUpdated'; exit 0 }
    $newline = if ($text.Contains("`r`n")) { "`r`n" } else { "`n" }
    $newText = if ($rows.Count -eq 0) { '' } else { ($rows -join $newline) + $newline }
    $temp = Join-Path $parent ('.voice-terminal-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $utf8 = [Text.UTF8Encoding]::new($false)
    [IO.File]::WriteAllText($temp, $newText, $utf8)
    if ($isAdmin) {
        & icacls.exe $temp '/inheritance:r' '/grant' '*S-1-5-32-544:F' '/grant' '*S-1-5-18:F' | Out-Null
    } else {
        & icacls.exe $temp '/inheritance:r' '/grant' (('*' + $expectedSid + ':F')) '/grant' '*S-1-5-18:R' | Out-Null
    }
    if ($LASTEXITCODE -ne 0) { Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue; throw '无法设置微软 OpenSSH 要求的授权公钥 ACL。' }
    if (Test-Path -LiteralPath $fullPath) {
        $currentBytes = [IO.File]::ReadAllBytes($fullPath)
        $sha256 = [Security.Cryptography.SHA256]::Create()
        try { $currentHash = [Convert]::ToBase64String($sha256.ComputeHash($currentBytes)) } finally { $sha256.Dispose() }
        if ($currentHash -ne $beforeHash) { Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue; throw '授权公钥文件在写入前发生变化，拒绝覆盖。' }
        # NullString（真正的空字符串引用）避免 PowerShell 5.1 把 $null 转为空路径。
        [IO.File]::Replace($temp, $fullPath, [System.Management.Automation.Language.NullString]::Value)
    } else { [IO.File]::Move($temp, $fullPath) }
    Write-Output 'VoiceTerminalAuthorizedKeyUpdated'
} catch {
    $failure = $_.Exception.Message
    if ($temp -and [IO.File]::Exists($temp)) { Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue }
    [Console]::Error.WriteLine($failure); exit 1
}
"#;

#[cfg(all(windows, test))]
fn encode_powershell(script: &str) -> String {
    use base64::Engine;
    let encoded = script
        .encode_utf16()
        .flat_map(u16::to_le_bytes)
        .collect::<Vec<_>>();
    base64::engine::general_purpose::STANDARD.encode(encoded)
}

#[cfg(all(windows, test))]
fn powershell_executable() -> Result<std::path::PathBuf, String> {
    Ok(crate::powershell::select(crate::powershell::Purpose::System)?.executable)
}

#[cfg(windows)]
fn run_powershell(script: &str, timeout: std::time::Duration) -> Result<String, String> {
    let host = crate::powershell::select(crate::powershell::Purpose::System)?;
    let (code, output, diagnostic) =
        crate::powershell::execute(&host, script, timeout, &mut |_| {})?;
    if code != 0 {
        let diagnostic = diagnostic
            .chars()
            .filter(|ch| !ch.is_control() || *ch == ' ')
            .take(240)
            .collect::<String>();
        return Err(format!(
            "系统脚本执行失败（PowerShell 退出码 {code}）；{diagnostic}"
        ));
    }
    Ok(output)
}

#[cfg(windows)]
fn run_maintenance_script(
    script: &str,
    timeout: std::time::Duration, // timeout（按正式维护操作确定的执行时限）。
    on_wait: &mut dyn FnMut(std::time::Duration),
) -> Result<u32, String> {
    let host = crate::powershell::select(crate::powershell::Purpose::Maintenance)?;
    let (code, _, diagnostic) =
        crate::powershell::execute(&host, script, timeout, on_wait)?;
    if code == 1 {
        let diagnostic = diagnostic
            .chars()
            .filter(|ch| !ch.is_control() || *ch == ' ')
            .take(240)
            .collect::<String>();
        return Err(format!("系统维护未完成；{diagnostic}"));
    }
    Ok(code)
}

#[cfg(windows)]
fn run_admin_operation(
    operation: crate::system_maintenance::Operation,
    script: &str,
) -> Result<(), String> {
    let code = if crate::system_maintenance::elevated()? {
        run_maintenance_script(script, operation.execution_timeout(), &mut |_| {})?
    } else {
        crate::system_maintenance::execute(operation, &mut |_| {})?
    };
    if code == 0 {
        Ok(())
    } else {
        Err(format!("程序网络规则或设备公钥维护失败（退出码 {code}）。"))
    }
}

/// execute_maintenance_operation（维护操作正式分派）：参数重新经生产校验，不接受脚本和可执行路径。
#[cfg(windows)]
pub(crate) fn execute_maintenance_operation(
    operation: crate::system_maintenance::Operation,
) -> Result<u32, String> {
    if !crate::system_maintenance::elevated()? {
        return Err("系统维护进程未获管理员授权。".into());
    }
    match operation {
        crate::system_maintenance::Operation::PrepareOpenSsh {} => {
            let status = open_ssh_server_runtime_status()?;
            if status.ready {
                return Ok(0);
            }
            // 复用生产安装准入，拒绝未知或签名不合格的现有系统服务。
            prepare_open_ssh_server_with_progress(
                open_ssh_server_runtime_status,
                |script, _| {
                    elevated_exit_result(run_maintenance_script(
                        script,
                        crate::system_maintenance::Operation::PrepareOpenSsh {}.execution_timeout(),
                        &mut |_| {},
                    )?)
                },
                |_| {},
            )?;
            Ok(0)
        }
        crate::system_maintenance::Operation::ProjectAccess {
            enabled,
            ssh,
            pairing,
            discovery,
        } => {
            configure_project_access(enabled, ssh, pairing, discovery)?;
            Ok(0)
        }
        #[cfg(feature = "ssh-device")]
        crate::system_maintenance::Operation::DeviceKey {
            add,
            public_key,
            workspace_id,
            device_id,
            build,
            owner_sid,
            network_scopes,
        } => {
            let executable = std::env::current_exe().map_err(|error| error.to_string())?;
            let context = crate::LaunchContext::from_executable(&executable, &[])?;
            if context.workspace_id != workspace_id {
                return Err("设备公钥维护工作区与当前正式程序不匹配。".into());
            }
            let release = crate::resources::load(&context.program.join("release.json"))?;
            if release["build"].as_str() != Some(build.as_str()) {
                return Err("设备公钥维护请求不属于当前正式程序构建。".into());
            }
            manage_project_device_key(
                add,
                &public_key,
                &workspace_id,
                &device_id,
                &build,
                &executable,
                &owner_sid,
                &network_scopes,
            )?;
            Ok(0)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn valid_broadcast_permission_is_reused_without_any_maintenance() {
        let mut status = OpenSshServerStatus::from_json(complete_server_status()).unwrap();
        status.project_access_valid = true;
        status.pairing_access_valid = true;
        status.discovery_access_valid = true;
        assert!(status.ready);
        let mut gate = BroadcastRepairGate { failed: true };
        let result = prepare_broadcast_with(&mut gate, false, status.clone(), |_| {
            panic!("有效网络许可不能触发系统维护")
        })
        .unwrap();
        assert_eq!(result, status);
        assert!(!gate.failed);
    }

    #[test]
    fn missing_ssh_never_installs_from_manual_or_automatic_broadcast_start() {
        for explicit in [false, true] {
            let mut status = OpenSshServerStatus::from_json(complete_server_status()).unwrap();
            status.ready = false;
            let error = prepare_broadcast_with(
                &mut BroadcastRepairGate::default(),
                explicit,
                status,
                |_| panic!("广播入口不能绕过系统功能下载确认"),
            )
            .unwrap_err();
            assert!(error.contains("确认下载或启动"));
        }
    }

    #[test]
    fn unavailable_lan_does_not_request_admin_and_missing_discovery_rule_can_be_repaired() {
        let mut status = OpenSshServerStatus::from_json(complete_server_status()).unwrap();
        let mut gate = BroadcastRepairGate::default();
        status.local_lan_addresses.clear();
        assert!(
            prepare_broadcast_with(&mut gate, false, status.clone(), |_| panic!(
                "网络断开不应申请管理员权限"
            ))
            .unwrap_err()
            .contains("没有可用")
        );
        status.local_lan_addresses = vec!["192.168.1.2".into()];
        status.local_lan_scopes = vec!["192.168.1.0/24".into()];
        status.project_access_valid = true;
        status.pairing_access_valid = true;
        status.discovery_access_valid = false;
        assert!(
            prepare_broadcast_with(&mut gate, false, status.clone(), |_| panic!(
                "不支持的账户不应申请管理员权限"
            ))
            .unwrap_err()
            .contains("账户不支持")
        );
        status.account_ssh_username = Some("lan-user".into());
        let result = prepare_broadcast_with(&mut gate, false, status, |before| {
            let mut after = before.clone();
            after.discovery_access_valid = true;
            Ok(after)
        })
        .unwrap();
        assert!(result.discovery_access_valid);
    }

    #[test]
    fn failed_automatic_broadcast_repair_is_not_repeated_until_explicit_retry() {
        let mut gate = BroadcastRepairGate::default();
        let attempts = std::cell::Cell::new(0);
        let mut repair = || {
            attempts.set(attempts.get() + 1);
            Err::<(), _>("管理员取消授权".into())
        };
        assert!(gate.repair(false, &mut repair).is_err());
        assert!(gate
            .repair(false, &mut repair)
            .unwrap_err()
            .contains("停止自动申请"));
        assert_eq!(attempts.get(), 1);
        assert!(gate.repair(true, &mut repair).is_err());
        assert_eq!(attempts.get(), 2);
        gate.repair(true, || Ok(())).unwrap();
        assert!(!gate.failed);
    }
    use crate::device_config::{Role, RuntimeSnapshot};
    use serde_json::json;
    use std::{
        cell::{Cell, RefCell},
        collections::VecDeque,
    };

    fn status(config_state: &'static str, desired_role: Option<Role>) -> RuntimeSnapshot {
        let mode = desired_role.map(|role| {
            if role == Role::RemoteClient {
                crate::device_config::RunMode::Remote
            } else {
                crate::device_config::RunMode::Local
            }
        });
        let broadcast_enabled = desired_role == Some(Role::SharedHost);
        RuntimeSnapshot {
            config_state,
            device_id: None,
            mode,
            broadcast_enabled,
            desired_role,
            audio_mode: None,
            error: None,
        }
    }

    fn complete_server_status() -> serde_json::Value {
        json!({
            "capability_state":"Installed",
            "service_present":true,
            "service_state":"Running",
            "startup_type":"Manual",
            "source_path":"C:\\Windows\\System32\\OpenSSH\\sshd.exe",
            "source_version":"9.5.0.0",
            "signature_status":"Valid",
            "signature_subject":"CN=Microsoft Windows",
            "official_binary":true,
            "service_path_official":true,
            "effective_port":22,
            "listening_ports":[22],
            "configuration_valid":true,
            "authorized_keys_path":null,
            "authorized_keys_exists":false,
            "project_access_enabled":false,
            "project_access_valid":false,
            "pairing_access_enabled":false,
            "pairing_access_valid":false,
        })
    }

    #[test]
    fn requires_a_valid_saved_local_mode() {
        assert!(require_local_runtime(&status("valid", Some(Role::SharedHost))).is_ok());
        assert!(require_local_runtime(&status("valid", Some(Role::Standalone))).is_ok());
        assert!(require_local_runtime(&status("missing", None)).is_ok());
        assert!(require_local_runtime(&status("invalid", Some(Role::SharedHost))).is_err());
        assert!(require_local_runtime(&status("valid", Some(Role::RemoteClient))).is_err());
    }

    #[test]
    fn formats_access_token_sid_without_account_name_guessing() {
        let mut sid = vec![1, 2, 0, 0, 0, 0, 0, 5];
        sid.extend_from_slice(&18u32.to_le_bytes());
        sid.extend_from_slice(&42u32.to_le_bytes());
        assert_eq!(format_sid(&sid).unwrap(), "S-1-5-18-42");
        assert!(format_sid(&[1, 16, 0, 0, 0, 0, 0, 5]).is_err());
    }

    #[test]
    fn other_ssh_rule_details_are_bounded_and_single_line() {
        let mut value = complete_server_status();
        value["existing_ssh_inbound_rules"] = json!((0..20)
            .map(|index| format!("rule-{index}\r\n{}", "long-detail ".repeat(50)))
            .collect::<Vec<_>>());
        let status = OpenSshServerStatus::from_json(value).unwrap();
        assert_eq!(
            status.existing_ssh_inbound_rules.len(),
            MAX_EXISTING_SSH_RULES
        );
        assert!(status.existing_ssh_inbound_rules.iter().all(|rule| {
            !rule.contains('\n')
                && !rule.contains('\r')
                && rule.chars().count() <= MAX_SSH_RULE_DETAIL_CHARS
        }));
        assert!(status.existing_ssh_inbound_rules[0].ends_with('…'));
    }

    #[test]
    fn parses_not_installed_capability_without_claiming_service_readiness() {
        let status = OpenSshServerStatus::from_json(json!({
            "capability_state":"NotPresent",
            "service_present":false,
            "service_state":null,
            "startup_type":null
        }))
        .unwrap();
        assert_eq!(status.feature_id, OPENSSH_SERVER_ID);
        assert!(!status.ready);
        assert_eq!(status.capability_state, "NotPresent");
        assert!(status.fields["ready"].contains("签名"));
    }

    #[test]
    fn reports_ready_only_when_official_program_config_service_and_listener_are_verified() {
        let status = OpenSshServerStatus::from_json(complete_server_status()).unwrap();
        assert!(status.ready);
        assert_eq!(status.startup_type.as_deref(), Some("Manual"));
        assert!(!status.project_access_enabled);
        assert!(!status.project_access_valid);
        assert!(!status.pairing_access_enabled);
        assert!(!status.pairing_access_valid);
    }

    #[test]
    fn signed_independent_microsoft_server_is_reused_without_windows_capability() {
        for folder in ["OpenSSH", "OpenSSH-Win64", "OpenSSH-ARM64"] {
            let mut value = complete_server_status();
            value["source_path"] = json!(format!("C:\\Program Files\\{folder}\\sshd.exe"));
            value["capability_state"] = json!("NotPresent");
            let status = OpenSshServerStatus::from_json(value).unwrap();
            assert!(status.ready);
            prepare_open_ssh_server_with(
                || Ok(status.clone()),
                |_| panic!("已有官方独立版不应下载安装另一份"),
            )
            .unwrap();
        }
        for path in [
            "D:\\Downloads\\OpenSSH\\sshd.exe",
            "C:\\Users\\tester\\System32\\OpenSSH\\sshd.exe",
            "C:\\Program Files\\OpenSSH-old\\sshd.exe",
        ] {
            let mut value = complete_server_status();
            value["source_path"] = json!(path);
            assert!(!OpenSshServerStatus::from_json(value).unwrap().ready);
        }
    }

    #[test]
    fn readiness_requires_each_verified_official_server_layer() {
        let mut variants = Vec::new();
        for (field, replacement) in [
            ("capability_state", json!("NotPresent")),
            ("service_state", json!("Stopped")),
            ("source_path", json!("C:\\Temp\\sshd.exe")),
            ("source_version", serde_json::Value::Null),
            ("signature_status", json!("NotSigned")),
            ("signature_subject", json!("CN=Unknown Publisher")),
            ("official_binary", json!(false)),
            ("service_path_official", json!(false)),
            ("effective_port", serde_json::Value::Null),
            ("listening_ports", json!([])),
            ("configuration_valid", json!(false)),
        ] {
            let mut status_json = complete_server_status();
            status_json[field] = replacement;
            variants.push(status_json);
        }
        for value in variants {
            let status = OpenSshServerStatus::from_json(value).unwrap();
            assert!(
                !status.ready,
                "incomplete official SSH state must fail closed"
            );
        }
    }

    #[test]
    fn unknown_capability_does_not_claim_full_system_readiness() {
        let status = OpenSshServerStatus::from_json(json!({
            "capability_state":"Unknown",
            "service_present":true,
            "service_state":"Running",
            "startup_type":"Manual"
        }))
        .unwrap();
        assert!(!status.ready);
        assert_eq!(status.capability_state, "Unknown");
        assert!(status.fields["capability_state"].contains("未提权"));
    }

    #[test]
    fn parses_all_documented_windows_capability_states_as_non_ready_when_appropriate() {
        for state in ["Removed", "Superseded", "PartiallyInstalled"] {
            let status = OpenSshServerStatus::from_json(json!({
                "capability_state":state,
                "service_present":false,
                "service_state":null,
                "startup_type":null
            }))
            .unwrap();
            assert!(
                !status.ready,
                "{state} is a known capability state but not a ready service"
            );
        }
    }

    #[test]
    fn unknown_capability_can_be_ready_when_every_official_runtime_layer_is_verified() {
        let mut value = complete_server_status();
        value["capability_state"] = json!("Unknown");
        let status = OpenSshServerStatus::from_json(value).unwrap();
        assert!(status.ready, "a fully verified Microsoft service should not prompt UAC only to read capability metadata");
        assert_eq!(status.capability_state, "Unknown");
    }

    #[test]
    fn normalizes_cim_auto_start_mode_without_losing_running_service() {
        let status = OpenSshServerStatus::from_json(json!({
            "capability_state":"Unknown",
            "service_present":true,
            "service_state":"Running",
            "startup_type":"Auto"
        }))
        .unwrap();
        assert!(!status.ready);
        assert_eq!(status.startup_type.as_deref(), Some("Automatic"));
    }

    #[test]
    fn rejects_unknown_or_contradictory_system_status() {
        assert!(OpenSshServerStatus::from_json(json!({
            "capability_state":"Mystery",
            "service_present":false,
            "service_state":null,
            "startup_type":null
        }))
        .is_err());
        assert!(OpenSshServerStatus::from_json(json!({
            "capability_state":"Installed",
            "service_present":false,
            "service_state":"Running",
            "startup_type":null
        }))
        .is_err());
        assert!(OpenSshServerStatus::from_json(json!({
            "capability_state":"Installed",
            "service_present":true,
            "service_state":"FutureState",
            "startup_type":"Manual"
        }))
        .is_err());
    }

    #[test]
    fn requires_complete_status_json_shape() {
        assert!(OpenSshServerStatus::from_json(json!({
            "capability_state":"Installed",
            "service_present":false
        }))
        .is_err());
        assert!(OpenSshServerStatus::from_json(json!({
            "capability_state":"Installed",
            "service_present":false,
            "service_state":12,
            "startup_type":null
        }))
        .is_err());
    }

    #[test]
    fn injected_ready_status_skips_elevation_and_post_install_query() {
        let ready = OpenSshServerStatus::from_json(complete_server_status()).unwrap();
        let query_count = Cell::new(0);
        let elevation_count = Cell::new(0);
        let result = prepare_open_ssh_server_with(
            || {
                query_count.set(query_count.get() + 1);
                Ok(ready.clone())
            },
            |_| {
                elevation_count.set(elevation_count.get() + 1);
                Ok(())
            },
        )
        .unwrap();
        assert!(result.ready);
        assert_eq!(query_count.get(), 1);
        assert_eq!(elevation_count.get(), 0);
    }

    #[test]
    fn injected_not_present_status_uses_official_install_script_then_rechecks() {
        let before = OpenSshServerStatus::from_json(json!({
            "capability_state":"NotPresent",
            "service_present":false,
            "service_state":null,
            "startup_type":null
        }))
        .unwrap();
        let after = OpenSshServerStatus::from_json(complete_server_status()).unwrap();
        let mut states = VecDeque::from([before, after.clone()]);
        let query_count = Cell::new(0);
        let script_seen = RefCell::new(None::<String>);
        let result = prepare_open_ssh_server_with(
            || {
                query_count.set(query_count.get() + 1);
                states
                    .pop_front()
                    .ok_or_else(|| "unexpected status query".to_owned())
            },
            |script| {
                *script_seen.borrow_mut() = Some(script.to_owned());
                Ok(())
            },
        )
        .unwrap();
        assert!(result.ready);
        assert_eq!(query_count.get(), 2);
        let script = script_seen.borrow();
        let script = script
            .as_deref()
            .expect("NotPresent must request the fixed install script");
        assert!(script.contains("Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'"));
        assert!(!script.contains("Invoke-WebRequest"));
        assert!(!script.contains("Start-BitsTransfer"));
        assert!(!script.to_ascii_lowercase().contains("winget"));
    }

    #[test]
    fn injected_elevation_wait_reports_elapsed_heartbeats_and_rechecks_once() {
        let before = OpenSshServerStatus::from_json(json!({
            "capability_state":"NotPresent",
            "service_present":false,
            "service_state":null,
            "startup_type":null
        }))
        .unwrap();
        let after = OpenSshServerStatus::from_json(complete_server_status()).unwrap();
        let mut states = VecDeque::from([before, after.clone()]);
        let query_count = Cell::new(0);
        let elevation_count = Cell::new(0);
        let progress = RefCell::new(Vec::new());
        let result = prepare_open_ssh_server_with_progress(
            || {
                query_count.set(query_count.get() + 1);
                states
                    .pop_front()
                    .ok_or_else(|| "unexpected status query".to_owned())
            },
            |script, on_wait| {
                assert!(script.contains("Add-WindowsCapability -Online"));
                elevation_count.set(elevation_count.get() + 1);
                on_wait(std::time::Duration::from_secs(15));
                on_wait(std::time::Duration::from_secs(30));
                Ok(())
            },
            |elapsed| progress.borrow_mut().push(elapsed),
        )
        .unwrap();

        assert!(result.ready);
        assert_eq!(query_count.get(), 2);
        assert_eq!(elevation_count.get(), 1);
        assert_eq!(
            *progress.borrow(),
            [
                std::time::Duration::from_secs(15),
                std::time::Duration::from_secs(30)
            ]
        );
    }

    #[test]
    fn injected_uac_cancellation_does_not_query_post_install_state() {
        let before = OpenSshServerStatus::from_json(json!({
            "capability_state":"NotPresent",
            "service_present":false,
            "service_state":null,
            "startup_type":null
        }))
        .unwrap();
        let query_count = Cell::new(0);
        let result = prepare_open_ssh_server_with(
            || {
                query_count.set(query_count.get() + 1);
                Ok(before.clone())
            },
            |_| Err(OPENSSH_SERVER_UAC_CANCELLED_ERROR.into()),
        );
        assert_eq!(result.unwrap_err(), OPENSSH_SERVER_UAC_CANCELLED_ERROR);
        assert_eq!(query_count.get(), 1);
    }

    #[test]
    fn pending_windows_capabilities_do_not_request_elevation() {
        for capability_state in ["InstallPending", "UninstallPending", "Staged"] {
            let mut value = complete_server_status();
            value["capability_state"] = json!(capability_state);
            let pending = OpenSshServerStatus::from_json(value).unwrap();
            assert!(!pending.ready);
            let query_count = Cell::new(0);
            let result = prepare_open_ssh_server_with(
                || {
                    query_count.set(query_count.get() + 1);
                    Ok(pending.clone())
                },
                |_| panic!("pending servicing must not invoke UAC"),
            );
            assert!(result.unwrap_err().contains("维护"));
            assert_eq!(query_count.get(), 1);
        }
    }

    #[test]
    fn existing_unofficial_service_path_fails_closed_before_elevation() {
        let mut value = complete_server_status();
        value["service_path_official"] = json!(false);
        let existing_service = OpenSshServerStatus::from_json(value).unwrap();
        assert!(!existing_service.ready);
        let query_count = Cell::new(0);
        let result = prepare_open_ssh_server_with(
            || {
                query_count.set(query_count.get() + 1);
                Ok(existing_service.clone())
            },
            |_| panic!("unverified sshd service path must not reach elevation/start"),
        );
        assert!(result.unwrap_err().contains("服务路径"));
        assert_eq!(query_count.get(), 1);
    }

    #[test]
    fn existing_service_with_unverified_microsoft_binary_fails_before_elevation() {
        let mut value = complete_server_status();
        value["official_binary"] = json!(false);
        let existing_service = OpenSshServerStatus::from_json(value).unwrap();
        assert!(!existing_service.ready);
        let query_count = Cell::new(0);
        let result = prepare_open_ssh_server_with(
            || {
                query_count.set(query_count.get() + 1);
                Ok(existing_service.clone())
            },
            |_| panic!("unverified existing sshd binary must not reach elevation/start"),
        );
        assert!(result.unwrap_err().contains("微软签名"));
        assert_eq!(query_count.get(), 1);
    }

    #[test]
    fn feature_source_failure_is_reported_without_a_success_recheck() {
        let before = OpenSshServerStatus::from_json(json!({
            "capability_state":"NotPresent",
            "service_present":false,
            "service_state":null,
            "startup_type":null
        }))
        .unwrap();
        let query_count = Cell::new(0);
        let result = prepare_open_ssh_server_with(
            || {
                query_count.set(query_count.get() + 1);
                Ok(before.clone())
            },
            |_| elevated_exit_result(13),
        );
        assert!(result.unwrap_err().contains("功能源"));
        assert_eq!(query_count.get(), 1);
        assert!(elevated_exit_result(12).unwrap_err().contains("重启等待"));
        assert!(elevated_exit_result(1)
            .unwrap_err()
            .contains("安装或 sshd 启动失败"));
    }

    #[test]
    fn running_official_service_with_unverified_config_reports_the_actual_blocker() {
        let mut value = complete_server_status();
        value["configuration_valid"] = json!(false);
        value["effective_port"] = serde_json::Value::Null;
        let status = OpenSshServerStatus::from_json(value).unwrap();
        assert!(!status.ready);
        let error = open_ssh_server_not_ready_error(&status);
        assert!(error.contains("已安装并运行"));
        assert!(error.contains("主机密钥"));
        assert!(error.contains("未扩大网络访问范围"));
    }

    #[test]
    fn successful_install_that_remains_pending_is_not_reported_ready() {
        let before = OpenSshServerStatus::from_json(json!({
            "capability_state":"NotPresent",
            "service_present":false,
            "service_state":null,
            "startup_type":null
        }))
        .unwrap();
        let mut pending_value = complete_server_status();
        pending_value["capability_state"] = json!("InstallPending");
        let pending_after = OpenSshServerStatus::from_json(pending_value).unwrap();
        let mut states = VecDeque::from([before, pending_after]);
        let result = prepare_open_ssh_server_with(
            || {
                states
                    .pop_front()
                    .ok_or_else(|| "unexpected status query".to_owned())
            },
            |_| Ok(()),
        );
        assert!(result.unwrap_err().contains("待处理"));
    }

    #[test]
    fn official_default_firewall_rule_is_disabled_only_for_a_fresh_install() {
        let script = open_ssh_server_install_script();
        assert!(script.contains("$wasInstalled = $capability.State -eq 'Installed'"));
        assert!(
            script.contains("$hadMicrosoftRule = @(Get-OpenSshDefaultFirewallRules).Count -gt 0")
        );
        assert!(script.contains("function Disable-NewOpenSshDefaultFirewallRule"));
        assert!(script.contains("if ($wasInstalled -or $hadMicrosoftRule) { return }"));
        assert!(script.contains("Set-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -Enabled False"));
        assert_eq!(script.matches("Set-NetFirewallRule").count(), 1);
        assert!(script.contains("$verifiedRules = @(Get-OpenSshDefaultFirewallRules)"));
        assert!(script.contains(
            "@($verifiedRules | Where-Object { [string]$_.Enabled -eq 'True' }).Count -gt 0"
        ));
        assert!(!script.contains("New-NetFirewallRule"));
        assert!(!script.contains("Remove-NetFirewallRule"));
        assert!(script.contains("Set-Service -Name 'sshd' -StartupType Automatic"));
        assert!(script.contains("if (-not $wasInstalled) { Set-Service"));
        let status = include_str!("system_feature.rs");
        assert!(status.contains("Get-NetFirewallPortFilter -All"));
        assert!(status.contains("Get-NetFirewallApplicationFilter -All"));
        assert!(status.contains("Get-NetFirewallAddressFilter -All"));
        let per_rule_port_query = [
            "Get-NetFirewallPortFilter -AssociatedNetFirewallRule ",
            "$candidate",
        ]
        .concat();
        let per_rule_application_query = [
            "Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule ",
            "$candidate",
        ]
        .concat();
        assert!(!status.contains(&per_rule_port_query));
        assert!(!status.contains(&per_rule_application_query));
        assert!(status.contains("$sshProbePort = if ($null -ne $effectivePort)"));
        assert!(OPENSSH_SERVICE_SCRIPT.contains("$preference = $ErrorActionPreference"));
        assert!(OPENSSH_SERVICE_SCRIPT.contains("$ErrorActionPreference = 'Continue'"));
        assert!(OPENSSH_SERVICE_SCRIPT.contains("$code = $LASTEXITCODE"));
        assert!(OPENSSH_SERVICE_SCRIPT.contains("$ErrorActionPreference = $preference"));
        assert!(status.contains("if ($null -eq $portFilter) { continue }"));
        assert!(status.contains("$candidatePorts -contains $sshProbePort"));
        assert!(status.contains("$otherSshInboundRules.Count -ge {MAX_EXISTING_SSH_RULES}"));
    }

    #[test]
    fn status_script_can_validate_an_unchanged_running_service_without_reading_private_keys() {
        let status = include_str!("system_feature.rs");
        assert!(status.contains("$service.Status -eq 'Running' -and $servicePathOfficial"));
        assert!(status.contains("$sshdConfig.LastWriteTimeUtc -le ([datetime]$serviceProcess.CreationDate).ToUniversalTime()"));
        assert!(status.contains("Get-Content -LiteralPath $sshdConfigPath -Raw -ErrorAction Stop"));
        assert!(status.contains("$configBeforeRead.Length -ne $configAfterRead.Length"));
        assert!(status
            .contains("$configBeforeRead.LastWriteTimeUtc -ne $configAfterRead.LastWriteTimeUtc"));
        assert!(status.contains("$configReadSuccessfully = $true"));
        assert!(status.contains("if ($configReadSuccessfully)"));
        assert!(status.contains("$hasConfigInclude = @($activeConfigLines | Where-Object { $_ -match '^Include\\s+' }).Count -gt 0"));
        assert!(status.contains("$portLines.Count -le 1"));
        assert!(status.contains("$listeningPorts -contains $loadedPort"));
        assert!(status.contains("$effectiveExitCode = $LASTEXITCODE"));
    }

    #[cfg(windows)]
    #[test]
    fn broadcast_rules_use_verified_ports_for_create_update_and_disable() {
        // 在独立进程内替换全部防火墙命令，验证实际脚本参数，不修改系统规则。
        let mut status = OpenSshServerStatus::from_json(complete_server_status()).unwrap();
        status.local_lan_scopes = vec!["192.168.1.0/24".into()];
        for (enabled, existing, fail) in [
            (true, false, false),
            (true, true, false),
            (false, true, false),
            (true, false, true),
            (true, true, true),
        ] {
            let setup = format!(
                r#"
$script:existing = {existing}
$script:failValidation = {fail}
$script:created = @{{}}
$script:disabled = @{{}}
$script:calls = @()
function Get-NetFirewallRule {{ param($Name,$ErrorAction) if ($script:existing -or $script:created.ContainsKey($Name)) {{ return @{{Name=$Name;Enabled=$(if ($script:disabled.ContainsKey($Name)) {{'False'}} else {{'True'}});Direction='Inbound';Action='Allow';Profile='Domain, Private'}} }} }}
function New-NetFirewallRule {{
    param($Name,$DisplayName,$Group,$Program,$Direction,$Protocol,$LocalPort,$RemoteAddress,$Profile,$Action,$Enabled,$ErrorAction)
    if (-not $LocalPort) {{ throw 'Mock rejected empty LocalPort' }}
    $script:calls += @{{name=$Name;port=$LocalPort;scope=($RemoteAddress -join ',');program=$Program}}
    $script:created[$Name]=$true
}}
function Set-NetFirewallRule {{ param($Name,$Enabled,$Direction,$Action,$Profile,$ErrorAction)
    if ([string]$Enabled -eq 'False') {{ $script:disabled[$Name]=$true; $script:calls += @{{name=$Name;disabled=$true}} }}
}}
function Get-NetFirewallApplicationFilter {{ param($AssociatedNetFirewallRule,$ErrorAction) return @{{Program=$(if ($AssociatedNetFirewallRule.Name -like '*OpenSSH*') {{Join-Path $env:SystemRoot 'System32/OpenSSH/sshd.exe'}} else {{$desktopProgram}})}} }}
function Get-NetFirewallPortFilter {{ param($AssociatedNetFirewallRule,$ErrorAction) return @{{Name=$AssociatedNetFirewallRule.Name;Protocol=$(if ($AssociatedNetFirewallRule.Name -like '*UDP*') {{'UDP'}} else {{'TCP'}});LocalPort=$(if ($AssociatedNetFirewallRule.Name -like '*OpenSSH*') {{'22'}} elseif ($AssociatedNetFirewallRule.Name -like '*UDP*') {{'5353'}} else {{'18766'}})}} }}
function Get-NetFirewallAddressFilter {{ param($AssociatedNetFirewallRule,$ErrorAction) return @{{RemoteAddress=@($(if ($script:failValidation -and $AssociatedNetFirewallRule.Name -like '*Pairing*') {{'Any'}} else {{'192.168.1.0/255.255.255.0'}}))}} }}
function Set-NetFirewallApplicationFilter {{ param($InputObject,$Program,$ErrorAction) }}
function Set-NetFirewallAddressFilter {{ param($InputObject,$RemoteAddress,$ErrorAction) }}
function Set-NetFirewallPortFilter {{ param($InputObject,$Protocol,$LocalPort,$ErrorAction)
    if (-not $LocalPort) {{ throw 'Mock rejected empty LocalPort' }}
    $script:calls += @{{name=$InputObject.Name;port=$LocalPort}}
}}
"#,
                existing = if existing { "$true" } else { "$false" },
                fail = if fail { "$true" } else { "$false" }
            );
            let script = setup + "\ntry {\n"
                + &project_access_script(
                    enabled,
                    true,
                    true,
                    true,
                    &status,
                    std::path::Path::new("C:/Program Files/Voice Terminal/Desktop.exe"),
                )
                + "\n} catch { $script:failed=$true }\n@{failed=[bool]$script:failed;calls=@($script:calls)} | ConvertTo-Json -Compress -Depth 4";
            let output = run_powershell(&script, std::time::Duration::from_secs(10)).unwrap();
            let value: serde_json::Value = serde_json::from_str(&output).unwrap();
            let calls = value["calls"].as_array().unwrap();
            assert_eq!(value["failed"], fail, "{value}");
            if fail {
                // 同一管理员脚本内回收新入口；已有启用规则必须保留，不发起第二次授权。
                let disabled = calls
                    .iter()
                    .filter(|call| call["disabled"] == true)
                    .collect::<Vec<_>>();
                assert_eq!(disabled.len(), if existing { 0 } else { 2 }, "{value}");
                if !existing {
                    assert_eq!(disabled[0]["name"], "VoiceTerminal-OpenSSH-Server-In-TCP");
                    assert_eq!(disabled[1]["name"], "VoiceTerminal-Device-Pairing-TCP-In");
                }
                continue;
            }
            assert_eq!(calls.len(), 3, "{value}");
            assert_eq!(calls[0]["name"], "VoiceTerminal-OpenSSH-Server-In-TCP");
            assert_eq!(calls[1]["name"], "VoiceTerminal-Device-Pairing-TCP-In");
            assert_eq!(calls[2]["name"], "VoiceTerminal-Device-Discovery-UDP-In");
            if enabled {
                assert_eq!(calls[0]["port"], 22);
                assert_eq!(calls[1]["port"], DEVICE_PAIRING_PORT);
                assert_eq!(calls[2]["port"], 5353);
                if !existing {
                    assert_eq!(calls[0]["scope"], "192.168.1.0/24");
                    assert_eq!(calls[1]["scope"], "192.168.1.0/24");
                    assert_eq!(
                        calls[1]["program"],
                        "C:/Program Files/Voice Terminal/Desktop.exe"
                    );
                }
            } else {
                assert_eq!(calls[0]["disabled"], true);
                assert_eq!(calls[1]["disabled"], true);
                assert_eq!(calls[2]["disabled"], true);
            }
        }
    }

    #[cfg(windows)]
    #[test]
    fn firewall_validation_normalizes_masks_and_rejects_broader_or_wrong_rules() {
        let script = FIREWALL_VALIDATION_SCRIPT.to_owned()
            + r#"
$ErrorActionPreference = 'Stop'
$rule = @{Enabled='True';Direction='Inbound';Action='Allow';Profile='Domain, Private'}
$script:program='C:\中文 程序\桌面.exe';$script:protocol='TCP';$script:ports=@('18766');$script:scope=@('192.168.253.0/255.255.255.0','fd12:3456::/64')
function Get-NetFirewallApplicationFilter { param($AssociatedNetFirewallRule,$ErrorAction) return @{Program=$script:program} }
function Get-NetFirewallPortFilter { param($AssociatedNetFirewallRule,$ErrorAction) return @{Protocol=$script:protocol;LocalPort=$script:ports} }
function Get-NetFirewallAddressFilter { param($AssociatedNetFirewallRule,$ErrorAction) return @{RemoteAddress=$script:scope} }
$expected=@('fd12:3456:0:0::/64','192.168.253.0/24')
if (-not (Test-ProjectFirewallRule $rule $script:program 18766 $expected)) { throw 'equivalent mask or IPv6 forms rejected' }
foreach ($bad in @('Any','LocalSubnet','192.168.0.0/16','192.168.253.0/255.0.255.0')) {
    $script:scope=@($bad,'fd12:3456::/64')
    if (Test-ProjectFirewallRule $rule $script:program 18766 $expected) { throw 'broader or invalid scope accepted' }
}
$script:scope=@('192.168.253.0/24','fd12:3456::/64')
$script:ports=@('18766','22')
if (Test-ProjectFirewallRule $rule $script:program 18766 $expected) { throw 'extra port accepted' }
$script:ports=@('18766')
foreach ($field in @('Direction','Action','Profile')) {
    $old=$rule[$field];$rule[$field]=@{Direction='Outbound';Action='Block';Profile='Any'}[$field]
    if (Test-ProjectFirewallRule $rule $script:program 18766 $expected) { throw 'wrong rule type accepted' }
    $rule[$field]=$old
}
$script:protocol='17';$script:ports=@('5353')
if (-not (Test-ProjectFirewallRule $rule $script:program 5353 $expected 'UDP')) { throw 'UDP protocol number rejected' }
Write-Output 'Validated'
"#;
        assert_eq!(
            run_powershell(&script, std::time::Duration::from_secs(10)).unwrap(),
            "Validated"
        );
    }

    #[cfg(windows)]
    #[test]
    fn powershell_account_membership_includes_deny_only_groups_but_not_other_sid_claims() {
        let script = format!("{ACCOUNT_IDENTITY_SCRIPT}\n")
            + r#"
$ErrorActionPreference = 'Stop'
foreach ($type in @([Security.Claims.ClaimTypes]::GroupSid, [Security.Claims.ClaimTypes]::DenyOnlySid)) {
    $identity = @{ Claims = @([Security.Claims.Claim]::new($type, 'S-1-5-32-544')) }
    if (-not (Test-ProjectAdministratorAccount $identity)) { throw 'Administrator account group rejected' }
}
foreach ($claims in @(
    @(),
    @([Security.Claims.Claim]::new([Security.Claims.ClaimTypes]::GroupSid, 'S-1-5-32-545')),
    @([Security.Claims.Claim]::new([Security.Claims.ClaimTypes]::PrimarySid, 'S-1-5-32-544')),
    @([Security.Claims.Claim]::new([Security.Claims.ClaimTypes]::DenyOnlyWindowsDeviceGroup, 'S-1-5-32-544'))
)) {
    $identity = @{ Claims = $claims; Groups = @('S-1-5-32-544') }
    if (Test-ProjectAdministratorAccount $identity) { throw 'Non-account-group claim accepted' }
}
Write-Output 'Validated'
"#;
        assert_eq!(
            run_powershell(&script, std::time::Duration::from_secs(10)).unwrap(),
            "Validated"
        );
    }

    #[cfg(windows)]
    #[test]
    fn real_account_membership_selects_the_authorized_key_store_without_system_writes() {
        // SAM（本机账户库）作独立只读参照；登记脚本保留真实身份判断，仅重定向文件与 ACL（访问控制）。
        let probe = format!("{ACCOUNT_IDENTITY_SCRIPT}\n")
            + r#"
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$directMember = @((Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction Stop) | ForEach-Object { $_.SID.Value }) -contains $identity.User.Value
$denyOnly = @($identity.Claims | Where-Object { $_.Type -eq [Security.Claims.ClaimTypes]::DenyOnlySid -and $_.Value -eq 'S-1-5-32-544' }).Count -gt 0
@{account_admin=(Test-ProjectAdministratorAccount $identity);sam_direct_member=$directMember;deny_only_admin=$denyOnly;
  fields=@{account_admin='真实账户身份判断';sam_direct_member='本机账户库直接成员身份';deny_only_admin='令牌中管理员仅拒绝组'}} | ConvertTo-Json -Compress
"#;
        let output = run_powershell(&probe, std::time::Duration::from_secs(10)).unwrap();
        let value: serde_json::Value = serde_json::from_str(&output).unwrap();
        let admin = value["account_admin"].as_bool().unwrap();
        if value["sam_direct_member"] == true || value["deny_only_admin"] == true {
            assert!(admin, "未提权的真实管理员账户不能被降为普通账户：{value}");
        }
        let temp_owner =
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../.local/tmp");
        let temp = temp_owner.join(format!("ssh-account-store-{}", crate::workspace::new_id()));
        std::fs::create_dir_all(&temp).unwrap();
        let admin_path = temp.join("管理员公钥");
        let user_path = temp.join("普通账户公钥");
        for path in [&admin_path, &user_path] {
            std::fs::write(path, "# 保留原有授权\n").unwrap();
        }
        let sid = current_user_sid().unwrap();
        let mut script = format!("{ACCOUNT_IDENTITY_SCRIPT}\n{AUTHORIZED_KEYS_SCRIPT}");
        for (source, path) in [
            (
                "Join-Path $env:ProgramData 'ssh/administrators_authorized_keys'",
                &admin_path,
            ),
            (
                "Join-Path $env:USERPROFILE '.ssh/authorized_keys'",
                &user_path,
            ),
        ] {
            let encoded =
                base64::engine::general_purpose::STANDARD.encode(path.to_string_lossy().as_bytes());
            script = script.replace(
                source,
                &format!(
                    "[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'))"
                ),
            );
        }
        let line = "command=test ssh-ed25519 TEST VoiceTerminal:real-account:device";
        for (name, value) in [
            ("ACTION", "add"),
            ("EXPECTED_SID", sid.as_str()),
            ("MARKER", "VoiceTerminal:real-account:device"),
            ("PUBLIC_KEY", "ssh-ed25519 TEST"),
            ("LINE", line),
            (
                "KEY_PATH",
                if admin {
                    admin_path.to_str().unwrap()
                } else {
                    user_path.to_str().unwrap()
                },
            ),
            ("ADMIN", if admin { "true" } else { "false" }),
        ] {
            script = script.replace(
                &format!("@@{name}@@"),
                &base64::engine::general_purpose::STANDARD.encode(value.as_bytes()),
            );
        }
        let setup = format!(
            r#"
function Get-Acl {{ param($LiteralPath) return @{{Access=@('{owner}','S-1-5-18') | ForEach-Object {{ @{{IdentityReference=[Security.Principal.SecurityIdentifier]::new($_);IsInherited=$false}} }} }} }}
function icacls.exe {{ $global:LASTEXITCODE=0 }}
"#,
            owner = if admin { "S-1-5-32-544" } else { sid.as_str() }
        );
        assert_eq!(
            run_powershell(&(setup + &script), std::time::Duration::from_secs(10)).unwrap(),
            "VoiceTerminalAuthorizedKeyUpdated"
        );
        let (selected, other) = if admin {
            (&admin_path, &user_path)
        } else {
            (&user_path, &admin_path)
        };
        assert!(std::fs::read_to_string(selected).unwrap().contains(line));
        assert_eq!(std::fs::read_to_string(other).unwrap(), "# 保留原有授权\n");
        crate::workspace::remove_owned(&temp, &temp_owner).unwrap();
    }

    #[cfg(windows)]
    #[test]
    fn selected_powershell_key_registration_preserves_other_lines_and_supports_unicode_paths() {
        // 仅在临时目录写测试公钥；系统身份仍用真实 SID（安全标识符），ACL（访问控制）命令全部替换。
        let temp_owner = std::env::temp_dir();
        let temp = temp_owner.join(format!("voice-ssh-key-{}", crate::workspace::new_id()));
        let parent = temp.join("中文 空格账户");
        std::fs::create_dir_all(&parent).unwrap();
        let target = parent.join("authorized_keys");
        let original = "# 保留其他用户配置\nssh-ed25519 OTHER unrelated\n";
        let sid = current_user_sid().unwrap();
        let target_b64 =
            base64::engine::general_purpose::STANDARD.encode(target.to_string_lossy().as_bytes());
        for admin in [false, true] {
            std::fs::write(&target, original).unwrap();
            for (index, action) in ["add", "add", "remove"].into_iter().enumerate() {
                let target_expression = format!(
                    "[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{target_b64}'))"
                );
                let mut script = format!("{ACCOUNT_IDENTITY_SCRIPT}\n{AUTHORIZED_KEYS_SCRIPT}")
                    .replace(
                        "$isAdmin = Test-ProjectAdministratorAccount $identity",
                        if admin {
                            "$isAdmin = $true"
                        } else {
                            "$isAdmin = $false"
                        },
                    )
                    .replace(
                        "Join-Path $env:USERPROFILE '.ssh/authorized_keys'",
                        &target_expression,
                    )
                    .replace(
                        "Join-Path $env:ProgramData 'ssh/administrators_authorized_keys'",
                        &target_expression,
                    );
                let line = format!(
                    "command=build-{index} ssh-ed25519 TEST VoiceTerminal:workspace:device"
                );
                for (name, value) in [
                    ("ACTION", action),
                    ("EXPECTED_SID", sid.as_str()),
                    ("MARKER", "VoiceTerminal:workspace:device"),
                    ("PUBLIC_KEY", "ssh-ed25519 TEST"),
                    ("LINE", line.as_str()),
                    ("KEY_PATH", target.to_str().unwrap()),
                    ("ADMIN", if admin { "true" } else { "false" }),
                ] {
                    script = script.replace(
                        &format!("@@{name}@@"),
                        &base64::engine::general_purpose::STANDARD.encode(value.as_bytes()),
                    );
                }
                let setup = format!(
                    r#"
function Get-Acl {{ param($LiteralPath) return @{{Access=@('{acl_owner}','S-1-5-18') | ForEach-Object {{ @{{IdentityReference=[Security.Principal.SecurityIdentifier]::new($_);IsInherited=$false}} }} }} }}
function icacls.exe {{ $global:LASTEXITCODE=0 }}
"#,
                    acl_owner = if admin { "S-1-5-32-544" } else { sid.as_str() }
                );
                if action == "remove" {
                    let original_bytes = std::fs::read(&target).unwrap();
                    let mismatched = script.replace(
                        &base64::engine::general_purpose::STANDARD.encode(b"ssh-ed25519 TEST"),
                        &base64::engine::general_purpose::STANDARD.encode(b"ssh-ed25519 DIFFERENT"));
                    assert!(run_powershell(&(setup.clone() + &mismatched), std::time::Duration::from_secs(10))
                        .unwrap_err().contains("授权公钥已变化"));
                    assert_eq!(std::fs::read(&target).unwrap(), original_bytes);
                }
                let result =
                    run_powershell(&(setup + &script), std::time::Duration::from_secs(10)).unwrap();
                assert_eq!(result, "VoiceTerminalAuthorizedKeyUpdated");
                let text = std::fs::read_to_string(&target).unwrap();
                assert!(text.starts_with(original));
                assert_eq!(
                    text.matches("VoiceTerminal:workspace:device").count(),
                    usize::from(action == "add")
                );
                if action == "add" {
                    assert!(text.contains(&line));
                }
            }
            assert_eq!(std::fs::read_to_string(&target).unwrap(), original);
        }
        crate::workspace::remove_owned(&temp, &temp_owner).unwrap();
    }

    #[cfg(windows)]
    #[test]
    fn generated_install_script_parses_without_executing_system_commands() {
        use base64::Engine as _;

        let source = open_ssh_server_install_script();
        let source_base64 = base64::engine::general_purpose::STANDARD.encode(source.as_bytes());
        let parser = format!(
            "$source = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{source_base64}')); [void][ScriptBlock]::Create($source);"
        );
        // 复用生产标准输入管道，避免双重编码超过 Windows（操作系统）命令行长度上限。
        run_powershell(&parser, std::time::Duration::from_secs(10)).unwrap();
    }

    #[cfg(windows)]
    #[test]
    fn service_layout_and_authorized_keys_follow_actual_supported_configuration() {
        let temp_owner =
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../.local/tmp");
        let temp = temp_owner.join(format!("ssh-layout-{}", crate::workspace::new_id()));
        std::fs::create_dir_all(&temp).unwrap();
        let config = temp.join("默认 配置");
        let custom = temp.join("自定义配置");
        std::fs::write(&config, "AuthorizedKeysFile .ssh/authorized_keys\nMatch Group administrators\n AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys\n").unwrap();
        std::fs::write(&custom, "AuthorizedKeysFile .ssh/custom_keys\n").unwrap();
        let fixture_b64 = base64::engine::general_purpose::STANDARD
            .encode(crate::canonical_path(&temp).unwrap().to_string_lossy().as_bytes());
        let cases = r#"
$fixture = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('@@FIXTURE@@'))
$script:SignatureValid = $true
function Test-Path {
    [CmdletBinding()] param([string]$LiteralPath, [string]$PathType)
    if ($LiteralPath -like '*\sshd.exe') { return $true }
    Microsoft.PowerShell.Management\Test-Path -LiteralPath $LiteralPath -PathType $PathType
}
function Get-Item {
    [CmdletBinding()] param([string]$LiteralPath)
    if ($LiteralPath -like '*\sshd.exe') { return [pscustomobject]@{ FullName=$LiteralPath; VersionInfo=@{FileVersion='9.5.6.4'} } }
    Microsoft.PowerShell.Management\Get-Item -LiteralPath $LiteralPath -ErrorAction Stop
}
function Get-AuthenticodeSignature {
    [CmdletBinding()] param([string]$FilePath)
    @{Status=$(if ($script:SignatureValid) { 'Valid' } else { 'NotTrusted' });SignerCertificate=@{Subject='CN=Microsoft Corporation'}}
}
$binary = Join-Path $env:ProgramFiles 'OpenSSH\sshd.exe'
$config = Join-Path $fixture '默认 配置'
$layout = Get-ProjectOpenSshLayout @{PathName=('"' + $binary + '" -f "' + $config + '" -p 2223')}
if (-not $layout.OfficialBinary -or -not $layout.ServicePathOfficial -or -not $layout.ArgumentsValid -or $layout.ConfigPath -ne $config -or $layout.PortOverride -ne 2223 -or ($layout.ProbeArguments -join '|') -ne "-T|-f|$config|-p|2223") { throw '实际服务路径和参数未保留' }
$adminPath = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
$userPath = Join-Path $fixture '.ssh\authorized_keys'
if ((Resolve-ProjectOpenSshAuthorizedKeys $layout $true $fixture) -ne $adminPath) { throw '管理员默认条件块未生效' }
if ((Resolve-ProjectOpenSshAuthorizedKeys $layout $false $fixture) -ne $userPath) { throw '普通账户被错误选择为管理员存储' }
$layout.ConfigPath = Join-Path $fixture '自定义配置'
if ($null -ne (Resolve-ProjectOpenSshAuthorizedKeys $layout $false $fixture)) { throw '自定义授权文件不能被静默替换成默认位置' }
$options = Get-ProjectOpenSshLayout @{PathName=($binary + ' -o "Port=2224"')}
if (-not $options.ArgumentsValid -or $options.SimpleConfig -or ($options.ProbeArguments -join '|') -ne '-T|-o|Port=2224') { throw '配置覆盖参数未传递' }
$unknown = Get-ProjectOpenSshLayout @{PathName=('"' + $binary + '" -Z')}
if ($unknown.ArgumentsValid) { throw '未知服务参数不能被忽略' }
$script:SignatureValid = $false
if ((Get-ProjectOpenSshLayout @{PathName=$binary}).OfficialBinary) { throw '无效签名被误判官方程序' }
$script:SignatureValid = $true
if ((Get-ProjectOpenSshLayout @{PathName=(Join-Path $fixture 'sshd.exe')}).OfficialBinary) { throw '非官方安装位置被放行' }
'Validated'
"#.replace("@@FIXTURE@@", &fixture_b64);
        let output = run_powershell(
            &(OPENSSH_SERVICE_SCRIPT.to_owned() + &cases),
            std::time::Duration::from_secs(15),
        )
        .unwrap();
        assert_eq!(output, "Validated");
        crate::workspace::remove_owned(&temp, &temp_owner).unwrap();
    }

    #[cfg(windows)]
    /// run_mocked_open_ssh_install_script（模拟运行 OpenSSH 安装脚本）：将 Windows 功能、服务和防火墙命令替换为进程内模拟函数。
    fn run_mocked_open_ssh_install_script(
        initial_capability: &str,
        existing_default_rule: bool,
        initial_service: &str,
        fail_feature_source: bool,
    ) -> std::process::Output {
        run_mocked_open_ssh_install_script_with_faults(
            initial_capability,
            existing_default_rule,
            initial_service,
            fail_feature_source,
            0,
            false,
            false,
            false,
        )
    }

    #[cfg(windows)]
    fn run_mocked_open_ssh_install_script_with_faults(
        initial_capability: &str,
        existing_default_rule: bool,
        initial_service: &str,
        fail_feature_source: bool,
        fail_rule_query_at: usize,
        partial_add_then_fail: bool,
        pending_after_add: bool,
        ignore_rule_disable: bool,
    ) -> std::process::Output {
        use std::process::Command;

        // 官方路径与签名定位单独执行脚本测试；此处只替换只读盘点，所有写入仍由下方模拟命令承接。
        let layout_mock = r#"
function Get-ProjectOpenSshLayout {
    param($NativeService)
    @{ OfficialBinary=($script:ServiceState -ne 'Unverified'); ServicePathOfficial=$true; ArgumentsValid=$true;
       SourcePath=(Join-Path $env:SystemRoot 'System32/OpenSSH/sshd.exe'); ProbeArguments=@('-T'); PortOverride=$null;
       ConfigPath=(Join-Path $env:SystemRoot 'win.ini') }
}
function Invoke-ProjectOpenSshProbe {
    @{ ExitCode=$(if ($script:ServiceState -eq 'InvalidConfig') { 1 } else { 0 }); Lines=@('port 22') }
}
"#;
        let source = open_ssh_server_install_script().replace(OPENSSH_SERVICE_SCRIPT, layout_mock).replace(
            "} catch {\n    exit 1\n}",
            "} catch {\n    [Console]::Out.WriteLine('MOCK:ERROR:' + $_.Exception.Message)\n    exit 1\n}",
        );
        let source_base64 = base64::engine::general_purpose::STANDARD.encode(source.as_bytes());
        let existing_rule_literal = if existing_default_rule {
            "$true"
        } else {
            "$false"
        };
        let source_failure_literal = if fail_feature_source {
            "$true"
        } else {
            "$false"
        };
        let partial_add_literal = if partial_add_then_fail {
            "$true"
        } else {
            "$false"
        };
        let pending_after_add_literal = if pending_after_add { "$true" } else { "$false" };
        let ignore_disable_literal = if ignore_rule_disable {
            "$true"
        } else {
            "$false"
        };
        // fail_service_start_literal（模拟 sshd 服务启动失败标志）：仅改变本进程内的模拟命令结果。
        let fail_service_start_literal =
            if matches!(initial_service, "StartFails" | "DisabledStartFails") {
                "$true"
            } else {
                "$false"
            };
        let initial_service_state = if initial_service == "StartFails" {
            "Stopped"
        } else {
            initial_service
        };
        let setup = format!(
            r#"
$script:CapabilityState = '{initial_capability}'
$script:ServicePresent = $script:CapabilityState -eq 'Installed'
$script:DefaultRulePresent = {existing_rule_literal}
$script:DefaultRuleEnabled = $true
$script:ServiceState = '{initial_service_state}'
$script:FailFeatureSource = {source_failure_literal}
$script:PartialAddThenFail = {partial_add_literal}
$script:PendingAfterAdd = {pending_after_add_literal}
$script:IgnoreRuleDisable = {ignore_disable_literal}
$script:FailServiceStart = {fail_service_start_literal}
$script:FailRuleQueryAt = {fail_rule_query_at}
$script:RuleQueryCount = 0
# MockNetSecurityEnabled（模拟微软防火墙规则启用状态枚举）：与真实 cmdlet（命令行命令）参数的 False/True 文本值对应。
enum MockNetSecurityEnabled {{ False; True }}
function Get-WindowsCapability {{
    [CmdletBinding()]
    param([switch]$Online, [string]$Name)
    if ($Name -ne 'OpenSSH.Server~~~~0.0.1.0') {{ throw 'unexpected capability' }}
    [Console]::Out.WriteLine('MOCK:Get-WindowsCapability')
    [pscustomobject]@{{ State = $script:CapabilityState }}
}}
function Add-WindowsCapability {{
    [CmdletBinding()]
    param([switch]$Online, [string]$Name)
    [Console]::Out.WriteLine('MOCK:Add-WindowsCapability')
    if ($script:FailFeatureSource) {{
        if ($script:PartialAddThenFail) {{ $script:CapabilityState = 'Installed'; $script:DefaultRulePresent = $true }}
        throw 'mock feature source unavailable'
    }}
    $script:CapabilityState = if ($script:PendingAfterAdd) {{ 'InstallPending' }} else {{ 'Installed' }}
    $script:ServicePresent = $true
    $script:DefaultRulePresent = $true
}}
function Get-NetFirewallRule {{
    [CmdletBinding()]
    param([string]$Name)
    if ($Name -and $Name -ne 'OpenSSH-Server-In-TCP') {{ throw 'unexpected firewall rule' }}
    $script:RuleQueryCount++
    if ($script:FailRuleQueryAt -eq $script:RuleQueryCount) {{ throw 'mock firewall rule query failed' }}
    [Console]::Out.WriteLine('MOCK:Get-NetFirewallRule')
    if ($script:DefaultRulePresent) {{ [pscustomobject]@{{ Name = 'OpenSSH-Server-In-TCP'; Enabled = $script:DefaultRuleEnabled }} }}
}}
function Set-NetFirewallRule {{
    [CmdletBinding()]
    param([string]$Name, [MockNetSecurityEnabled]$Enabled)
    if ($Name -ne 'OpenSSH-Server-In-TCP') {{ throw 'unexpected firewall mutation' }}
    if (-not $script:IgnoreRuleDisable) {{ $script:DefaultRuleEnabled = $Enabled -eq [MockNetSecurityEnabled]::True }}
    [Console]::Out.WriteLine("MOCK:Set-NetFirewallRule:$Enabled")
}}
function Set-Service {{
    [CmdletBinding()]
    param([string]$Name, [string]$StartupType)
    if ($Name -ne 'sshd' -or $StartupType -notin @('Automatic','Manual','Disabled')) {{ throw 'unexpected service mutation' }}
    [Console]::Out.WriteLine("MOCK:Set-Service:$StartupType")
}}
function Get-Service {{
    [CmdletBinding()]
    param([string]$Name)
    if ($Name -ne 'sshd') {{ throw 'unexpected service query' }}
    [Console]::Out.WriteLine('MOCK:Get-Service:sshd')
    if ($script:ServicePresent) {{ [pscustomobject]@{{ Status = $script:ServiceState }} }}
}}
function Get-CimInstance {{
    [CmdletBinding()]
    param([string]$ClassName, [string]$Filter)
    if ($ClassName -ne 'Win32_Service' -or $Filter -ne "Name='sshd'") {{ throw 'unexpected service metadata query' }}
    if ($script:ServicePresent) {{ [pscustomobject]@{{ ProcessId=0; StartMode=$(if ($script:ServiceState -like 'Disabled*') {{ 'Disabled' }} else {{ 'Manual' }}) }} }}
}}
function Get-NetTCPConnection {{
    [CmdletBinding()]
    param([string]$State)
    if ($script:ServiceState -eq 'PortConflict') {{ [pscustomobject]@{{ LocalPort=22; OwningProcess=99 }} }}
}}
function Start-Service {{
    [CmdletBinding()]
    param([string]$Name)
    if ($Name -ne 'sshd') {{ throw 'unexpected service start' }}
    [Console]::Out.WriteLine('MOCK:Start-Service:sshd')
    if ($script:FailServiceStart) {{ throw 'mock sshd service failed to start' }}
    $script:ServiceState = 'Running'
}}
$source = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{source_base64}'))
. ([ScriptBlock]::Create($source))
"#
        );
        let encoded = encode_powershell(&setup);
        Command::new(powershell_executable().expect("Windows PowerShell should be available"))
            .args([
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded.as_str(),
            ])
            .output()
            .expect("mocked OpenSSH install script process should start without elevation")
    }

    #[cfg(windows)]
    #[test]
    fn generated_install_script_runs_against_mocked_windows_commands() {
        let fresh_install =
            run_mocked_open_ssh_install_script("NotPresent", false, "Stopped", false);
        assert_eq!(
            fresh_install.status.code(),
            Some(0),
            "mocked installation output: {} / {}",
            String::from_utf8_lossy(&fresh_install.stdout),
            String::from_utf8_lossy(&fresh_install.stderr),
        );
        let fresh_output = String::from_utf8_lossy(&fresh_install.stdout);
        let add_index = fresh_output
            .find("MOCK:Add-WindowsCapability")
            .expect("fresh install should invoke the official capability mock");
        let firewall_index = fresh_output
            .find("MOCK:Set-NetFirewallRule:False")
            .expect("new default firewall rule should be disabled");
        let service_index = fresh_output
            .find("MOCK:Set-Service:Automatic")
            .expect("fresh install should enable the sshd service");
        let start_index = fresh_output
            .find("MOCK:Start-Service:sshd")
            .expect("fresh install should start sshd");
        assert!(
            add_index < firewall_index
                && firewall_index < service_index
                && service_index < start_index
        );

        let preexisting_rule =
            run_mocked_open_ssh_install_script("NotPresent", true, "Stopped", false);
        assert_eq!(preexisting_rule.status.code(), Some(0));
        let rule_output = String::from_utf8_lossy(&preexisting_rule.stdout);
        assert!(rule_output.contains("MOCK:Add-WindowsCapability"));
        assert!(!rule_output.contains("MOCK:Set-NetFirewallRule"));

        let existing_service =
            run_mocked_open_ssh_install_script("Installed", true, "Stopped", false);
        assert_eq!(existing_service.status.code(), Some(0));
        let existing_output = String::from_utf8_lossy(&existing_service.stdout);
        assert!(!existing_output.contains("MOCK:Add-WindowsCapability"));
        assert!(!existing_output.contains("MOCK:Set-NetFirewallRule"));
        assert!(!existing_output.contains("MOCK:Set-Service"));
        assert!(existing_output.contains("MOCK:Start-Service:sshd"));

        let already_running =
            run_mocked_open_ssh_install_script("Installed", true, "Running", false);
        assert_eq!(already_running.status.code(), Some(0));
        let running_output = String::from_utf8_lossy(&already_running.stdout);
        assert!(running_output.contains("MOCK:Get-Service:sshd"));
        assert!(!running_output.contains("MOCK:Start-Service:sshd"));
        assert!(!running_output.contains("MOCK:Set-Service"));

        let start_failure =
            run_mocked_open_ssh_install_script("Installed", true, "StartFails", false);
        assert_eq!(start_failure.status.code(), Some(1));
        let start_failure_output = String::from_utf8_lossy(&start_failure.stdout);
        assert!(start_failure_output.contains("MOCK:Start-Service:sshd"));
        assert!(start_failure_output.contains("MOCK:ERROR:mock sshd service failed to start"));

        let pending = run_mocked_open_ssh_install_script("InstallPending", false, "Stopped", false);
        assert_eq!(pending.status.code(), Some(12));
        let pending_output = String::from_utf8_lossy(&pending.stdout);
        assert!(!pending_output.contains("MOCK:Add-WindowsCapability"));
        assert!(!pending_output.contains("MOCK:Set-NetFirewallRule"));
        assert!(!pending_output.contains("MOCK:Start-Service"));

        let source_failure =
            run_mocked_open_ssh_install_script("NotPresent", false, "Stopped", true);
        assert_eq!(source_failure.status.code(), Some(13));
        let failure_output = String::from_utf8_lossy(&source_failure.stdout);
        assert!(failure_output.contains("MOCK:Add-WindowsCapability"));
        assert!(!failure_output.contains("MOCK:Set-Service"));
        assert!(!failure_output.contains("MOCK:Start-Service"));
    }

    #[cfg(windows)]
    #[test]
    fn existing_service_rejects_conflicts_and_invalid_configuration_and_can_enable_disabled_service(
    ) {
        for (state, code) in [
            ("Unverified", 15),
            ("PortConflict", 16),
            ("InvalidConfig", 17),
        ] {
            let output = run_mocked_open_ssh_install_script("Installed", true, state, false);
            assert_eq!(
                output.status.code(),
                Some(code),
                "{state}: {}",
                String::from_utf8_lossy(&output.stdout)
            );
            let stdout = String::from_utf8_lossy(&output.stdout);
            assert!(!stdout.contains("MOCK:Start-Service"));
            assert!(!stdout.contains("MOCK:Add-WindowsCapability"));
            assert!(!stdout.contains("MOCK:Set-NetFirewallRule"));
        }
        let output = run_mocked_open_ssh_install_script("Installed", true, "Disabled", false);
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stdout)
        );
        let stdout = String::from_utf8_lossy(&output.stdout);
        assert!(stdout.contains("MOCK:Set-Service:Manual"));
        assert!(stdout.contains("MOCK:Start-Service:sshd"));
        assert!(!stdout.contains("MOCK:Set-Service:Automatic"));
        let output =
            run_mocked_open_ssh_install_script("Installed", true, "DisabledStartFails", false);
        assert_eq!(output.status.code(), Some(1));
        let stdout = String::from_utf8_lossy(&output.stdout);
        assert!(stdout.contains("MOCK:Set-Service:Manual"));
        assert!(stdout.contains("MOCK:Set-Service:Disabled"));
    }

    #[cfg(windows)]
    #[test]
    fn install_script_fails_closed_on_firewall_query_and_partial_install_paths() {
        let initial_query_failure = run_mocked_open_ssh_install_script_with_faults(
            "NotPresent",
            false,
            "Stopped",
            false,
            1,
            false,
            false,
            false,
        );
        assert_ne!(initial_query_failure.status.code(), Some(0));
        let initial_output = String::from_utf8_lossy(&initial_query_failure.stdout);
        assert!(
            !initial_output.contains("MOCK:Add-WindowsCapability"),
            "query failure must stop before installation: {initial_output}"
        );

        let partial_failure = run_mocked_open_ssh_install_script_with_faults(
            "NotPresent",
            false,
            "Stopped",
            true,
            0,
            true,
            false,
            false,
        );
        assert_eq!(partial_failure.status.code(), Some(13));
        let partial_output = String::from_utf8_lossy(&partial_failure.stdout);
        assert!(
            partial_output.contains("MOCK:Set-NetFirewallRule:False"),
            "partial installation rule should be disabled: {partial_output}"
        );
        assert!(!partial_output.contains("MOCK:Start-Service"));

        let pending_after_add = run_mocked_open_ssh_install_script_with_faults(
            "NotPresent",
            false,
            "Stopped",
            false,
            0,
            false,
            true,
            false,
        );
        assert_eq!(pending_after_add.status.code(), Some(12));
        let pending_output = String::from_utf8_lossy(&pending_after_add.stdout);
        assert!(
            pending_output.contains("MOCK:Set-NetFirewallRule:False"),
            "pending installation rule should be disabled: {pending_output}"
        );
        assert!(!pending_output.contains("MOCK:Start-Service"));

        let post_add_query_failure = run_mocked_open_ssh_install_script_with_faults(
            "NotPresent",
            false,
            "Stopped",
            false,
            2,
            false,
            false,
            false,
        );
        assert_ne!(post_add_query_failure.status.code(), Some(0));
        let query_output = String::from_utf8_lossy(&post_add_query_failure.stdout);
        assert!(
            !query_output.contains("MOCK:Start-Service"),
            "uncertain firewall state must prevent service startup: {query_output}"
        );

        let disable_not_verified = run_mocked_open_ssh_install_script_with_faults(
            "NotPresent",
            false,
            "Stopped",
            false,
            0,
            false,
            false,
            true,
        );
        assert_ne!(disable_not_verified.status.code(), Some(0));
        let verification_output = String::from_utf8_lossy(&disable_not_verified.stdout);
        assert!(
            !verification_output.contains("MOCK:Start-Service"),
            "unverified rule mutation must prevent service startup: {verification_output}"
        );
    }

    #[cfg(windows)]
    #[test]
    fn powershell_status_runner_can_resolve_the_windows_signature_cmdlet() {
        let script = r#"
$systemModuleRoot = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\Modules'
$env:PSModulePath = (Join-Path $PSHOME 'Modules') + ';' + $systemModuleRoot
$PSModuleAutoLoadingPreference = 'All'
# 模块自动加载回归使用系统必备签名程序，不要求预先安装可选的 SSH（安全外壳协议）服务。
$signature = Get-AuthenticodeSignature -FilePath (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -ErrorAction Stop
$command = Get-Command Get-AuthenticodeSignature -ErrorAction SilentlyContinue
@{
    module_path = $env:PSModulePath
    module_available = (@(Get-Module -ListAvailable Microsoft.PowerShell.Security).Count -gt 0)
    module_loaded = (@(Get-Module Microsoft.PowerShell.Security).Count -gt 0)
    command_available = ($null -ne $command)
    signature_status = [string]$signature.Status
} | ConvertTo-Json -Compress
"#;
        let output = run_powershell(script, std::time::Duration::from_secs(10))
            .expect("Windows PowerShell signature command probe should return JSON");
        let value: serde_json::Value = serde_json::from_str(&output).unwrap();
        assert!(value["module_available"].as_bool().unwrap());
        assert!(
            value["command_available"].as_bool().unwrap(),
            "PowerShell command probe: {value}"
        );
        assert_eq!(value["signature_status"], "Valid");
    }

    #[cfg(all(windows, feature = "ssh-device"))]
    #[tokio::test]
    #[ignore = "人工只读验收：读取实际服务与回环公钥，不写系统密钥或防火墙"]
    async fn current_service_public_key_is_obtainable_without_protected_file_access() {
        let started = std::time::Instant::now();
        let status = open_ssh_server_runtime_status().unwrap();
        eprintln!("运行必要检查耗时: {:?}", started.elapsed());
        assert!(status.ready);
        assert!(status.existing_ssh_inbound_rules.is_empty());
        let cached_started = std::time::Instant::now();
        assert_eq!(
            open_ssh_server_runtime_status().unwrap().effective_port,
            status.effective_port
        );
        eprintln!("同轮复用检查耗时: {:?}", cached_started.elapsed());
        let started = std::time::Instant::now();
        let identity = open_ssh_server_identity(status.effective_port.unwrap())
            .await
            .unwrap();
        eprintln!("回环公钥握手耗时: {:?}", started.elapsed());
        assert!(identity.public_key.starts_with("ssh-ed25519 "));
        assert!(identity.fingerprint_sha256.starts_with("SHA256:"));
    }

    #[cfg(windows)]
    #[test]
    #[ignore = "人工只读验收：查询当前 Windows 可选功能与 sshd 状态，不修改系统"]
    fn reads_current_windows_open_ssh_server_state_without_changes() {
        let status = open_ssh_server_status().expect("当前 Windows OpenSSH Server 状态应可查询");
        assert_eq!(status.feature_id, OPENSSH_SERVER_ID);
        assert_eq!(
            status.ready,
            (status.capability_state == "Installed"
                || status.capability_state == "Unknown"
                    && status.official_binary
                    && status.service_path_official)
                && status.service_present
                && status.service_state.as_deref() == Some("Running")
                && status.official_binary
                && status.service_path_official
                && status.configuration_valid
                && status
                    .effective_port
                    .is_some_and(|port| status.listening_ports.contains(&port))
        );
        assert!(status.existing_ssh_inbound_rules.len() <= MAX_EXISTING_SSH_RULES);
        assert!(status
            .existing_ssh_inbound_rules
            .iter()
            .all(|rule| { rule.chars().count() <= MAX_SSH_RULE_DETAIL_CHARS }));
    }
}
