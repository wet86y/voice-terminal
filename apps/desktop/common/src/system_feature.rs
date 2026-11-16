//! SystemFeature（系统功能）：与工作区文件组件分离的 Windows 主机能力。
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

pub const OPENSSH_SERVER_ID: &str = "openssh-server"; // OPENSSH_SERVER_ID（OpenSSH Server 功能编号）。
pub const OPENSSH_SERVER_JOB_ID: &str = "system-feature-openssh-server"; // OPENSSH_SERVER_JOB_ID（OpenSSH Server 受管任务编号）。
const OPENSSH_CAPABILITY: &str = "OpenSSH.Server~~~~0.0.1.0"; // OPENSSH_CAPABILITY（Windows 可选功能固定名称）。

/// OpenSshServerStatus（OpenSSH Server 状态）：只读检查，不启动或修改系统服务。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OpenSshServerStatus {
    pub feature_id: String, // feature_id（固定 Windows 功能编号）。
    pub capability_state: String, // capability_state（Windows 可选功能状态）。
    pub service_present: bool, // service_present（sshd 系统服务是否存在）。
    pub service_state: Option<String>, // service_state（sshd 当前运行状态）。
    pub startup_type: Option<String>, // startup_type（现有 sshd 启动类型；只读取）。
    pub ready: bool, // ready（功能已安装且 sshd 正在运行）。
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
            "Installed" | "NotPresent" | "InstallPending" | "UninstallPending" | "Staged" | "Unknown"
        ) {
            return Err("Windows 返回了未知的 OpenSSH Server 功能状态。".into());
        }

        let service_present = value
            .get("service_present")
            .and_then(serde_json::Value::as_bool)
            .ok_or("OpenSSH Server 状态缺少 service_present（服务是否存在）。")?;
        let service_state = optional_string(&value, "service_state")?;
        // Win32_Service.StartMode（系统服务启动模式）使用 Auto；公开状态统一为 Automatic（自动）。
        let startup_type = optional_string(&value, "startup_type")?
            .map(|mode| if mode == "Auto" { "Automatic".into() } else { mode });
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
            if !matches!(startup_type, "Boot" | "System" | "Automatic" | "Manual" | "Disabled" | "Unknown") {
                return Err("Windows 返回了未知的 sshd 启动类型。".into());
            }
        }

        let ready = matches!(capability_state, "Installed" | "Unknown")
            && service_present
            && service_state.as_deref() == Some("Running");
        Ok(Self {
            feature_id: OPENSSH_SERVER_ID.into(),
            capability_state: capability_state.into(),
            service_present,
            service_state,
            startup_type,
            ready,
            fields: [
                ("feature_id".into(), "Windows 系统功能固定编号".into()),
                ("capability_state".into(), "OpenSSH Server 可选功能状态；Unknown 表示本次只读查询未提权".into()),
                ("service_present".into(), "sshd 系统服务是否存在".into()),
                ("service_state".into(), "sshd 当前运行状态".into()),
                ("startup_type".into(), "sshd 现有启动类型；本检查只读取".into()),
                ("ready".into(), "系统功能已安装且 sshd 正在运行".into()),
            ]
            .into_iter()
            .collect(),
        })
    }
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

/// require_shared_host（共享主机门禁）：系统级 SSH 服务只能由已保存的共享主机管理。
pub fn require_shared_host(snapshot: &crate::device_config::RuntimeSnapshot) -> Result<(), String> {
    if snapshot.config_state == "valid"
        && snapshot.desired_role == Some(crate::device_config::Role::SharedHost)
    {
        Ok(())
    } else {
        Err("仅允许在设备配置有效且已保存为“共享主机”角色时管理 OpenSSH Server。".into())
    }
}

/// open_ssh_server_status（检查主机功能）：仅查询 capability、服务存在性和当前服务状态。
pub fn open_ssh_server_status() -> Result<OpenSshServerStatus, String> {
    #[cfg(windows)]
    {
        let script = r#"
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$service = Get-Service -Name 'sshd' -ErrorAction SilentlyContinue
$startupType = $null
if ($null -ne $service) {
    $nativeService = Get-CimInstance -ClassName Win32_Service -Filter "Name='sshd'" -ErrorAction SilentlyContinue
    if ($null -ne $nativeService) { $startupType = [string]$nativeService.StartMode }
}
@{
    capability_state = 'Unknown'
    service_present = ($null -ne $service)
    service_state = if ($null -ne $service) { [string]$service.Status } else { $null }
    startup_type = $startupType
} | ConvertTo-Json -Compress
"#;
        let output = run_powershell(&script, std::time::Duration::from_secs(15))?;
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
/// Windows 安装可选功能时会自动创建并启用入站防火墙规则；管理页必须在调用前明示该行为。
/// 本脚本不单独创建、删除或调整防火墙规则，也不设置启动类型、修改账户公钥或配置文件。
pub fn enable_open_ssh_server() -> Result<OpenSshServerStatus, String> {
    #[cfg(windows)]
    {
        let before = open_ssh_server_status()?;
        if before.ready {
            return Ok(before);
        }
        let script = format!(
            r#"
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try {{
    $capability = Get-WindowsCapability -Online -Name '{OPENSSH_CAPABILITY}' -ErrorAction Stop
    if ($capability.State -in @('InstallPending', 'UninstallPending')) {{ exit 12 }}
    if ($capability.State -ne 'Installed') {{
        Add-WindowsCapability -Online -Name '{OPENSSH_CAPABILITY}' -ErrorAction Stop | Out-Null
    }}
    $service = Get-Service -Name 'sshd' -ErrorAction Stop
    if ($service.Status -ne 'Running') {{ Start-Service -Name 'sshd' -ErrorAction Stop }}
    exit 0
}} catch {{
    exit 1
}}
"#
        );
        run_elevated_powershell(&script)?;
        let status = open_ssh_server_status()?;
        if !status.ready {
            return Err("OpenSSH Server 管理任务已退出，但状态复核未确认 sshd 正在运行。请刷新状态并检查 Windows 功能源、服务状态及策略。".into());
        }
        Ok(status)
    }
    #[cfg(not(windows))]
    {
        Err("OpenSSH Server 系统功能管理只支持 Windows 主机。".into())
    }
}

#[cfg(windows)]
fn encode_powershell(script: &str) -> String {
    use base64::Engine;
    let encoded = script
        .encode_utf16()
        .flat_map(u16::to_le_bytes)
        .collect::<Vec<_>>();
    base64::engine::general_purpose::STANDARD.encode(encoded)
}

#[cfg(windows)]
fn powershell_executable() -> Result<std::path::PathBuf, String> {
    let system_root = std::env::var_os("SystemRoot")
        .ok_or("Windows SystemRoot 环境变量不可用。")?;
    let executable = std::path::PathBuf::from(system_root)
        .join("System32/WindowsPowerShell/v1.0/powershell.exe");
    if !executable.is_file() {
        return Err("系统 Windows PowerShell 不存在。".into());
    }
    Ok(executable)
}

#[cfg(windows)]
fn run_powershell(script: &str, timeout: std::time::Duration) -> Result<String, String> {
    use std::{
        os::windows::process::CommandExt,
        process::{Command, Stdio},
        thread,
        time::Instant,
    };
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    let executable = powershell_executable()?;
    let encoded = encode_powershell(script);
    let mut child = Command::new(executable)
        .args(["-NoProfile", "-NonInteractive", "-EncodedCommand", encoded.as_str()])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()
        .map_err(|error| format!("无法启动 Windows PowerShell：{error}"))?;
    let started = Instant::now();
    loop {
        if let Some(status) = child
            .try_wait()
            .map_err(|error| format!("无法读取 PowerShell 任务状态：{error}"))?
        {
            let output = child
                .wait_with_output()
                .map_err(|error| format!("无法读取 PowerShell 状态回执：{error}"))?;
            if !status.success() {
                return Err("OpenSSH Server 状态查询失败；请检查当前 Windows 用户读取系统功能状态的权限。".into());
            }
            return String::from_utf8(output.stdout)
                .map(|value| value.trim().to_owned())
                .map_err(|_| "OpenSSH Server 状态结果不是 UTF-8。".into());
        }
        if started.elapsed() >= timeout {
            let _ = child.kill();
            let _ = child.wait();
            return Err("OpenSSH Server 状态查询超时。".into());
        }
        thread::sleep(std::time::Duration::from_millis(50));
    }
}

#[cfg(windows)]
fn run_elevated_powershell(script: &str) -> Result<(), String> {
    use std::{mem::size_of, os::windows::ffi::OsStrExt};
    use windows_sys::Win32::{
        Foundation::{CloseHandle, GetLastError, ERROR_CANCELLED, WAIT_OBJECT_0},
        System::Threading::{GetExitCodeProcess, WaitForSingleObject},
        UI::{Shell::{ShellExecuteExW, SHELLEXECUTEINFOW, SEE_MASK_NOCLOSEPROCESS, SEE_MASK_NOASYNC},
            WindowsAndMessaging::SW_HIDE},
    };

    fn wide(value: &std::ffi::OsStr) -> Vec<u16> {
        value.encode_wide().chain(std::iter::once(0)).collect()
    }

    let executable = wide(powershell_executable()?.as_os_str());
    let verb = wide(std::ffi::OsStr::new("runas"));
    let encoded = encode_powershell(script);
    let parameters = wide(std::ffi::OsStr::new(&format!(
        "-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand {encoded}"
    )));
    let mut execute = SHELLEXECUTEINFOW {
        cbSize: size_of::<SHELLEXECUTEINFOW>() as u32,
        fMask: SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC,
        lpVerb: verb.as_ptr(),
        lpFile: executable.as_ptr(),
        lpParameters: parameters.as_ptr(),
        nShow: SW_HIDE,
        ..Default::default()
    };

    let launched = unsafe { ShellExecuteExW(&mut execute) };
    if launched == 0 {
        let code = unsafe { GetLastError() };
        return if code == ERROR_CANCELLED {
            Err("已取消 Windows 管理员授权；OpenSSH Server 未由本任务启动。".into())
        } else {
            Err(format!("无法启动 OpenSSH Server 管理任务（Windows 错误代码 {code}）。"))
        };
    }

    let process = execute.hProcess;
    if process.is_null() {
        return Err("Windows 未返回 OpenSSH Server 管理任务句柄，无法确认任务结束状态。".into());
    }
    let wait = unsafe { WaitForSingleObject(process, u32::MAX) };
    if wait != WAIT_OBJECT_0 {
        unsafe { CloseHandle(process) };
        return Err("等待 OpenSSH Server 管理任务时发生错误；最终状态未知，请先刷新状态。".into());
    }
    let mut exit_code = 1u32;
    let exit_read = unsafe { GetExitCodeProcess(process, &mut exit_code) };
    unsafe { CloseHandle(process) };
    if exit_read == 0 {
        return Err("无法读取 OpenSSH Server 管理任务退出状态；请刷新状态确认结果。".into());
    }
    if exit_code == 12 {
        return Err("OpenSSH Server 正处于 Windows 功能维护状态；请刷新状态，待维护结束后再操作。".into());
    }
    if exit_code != 0 {
        return Err("OpenSSH Server 系统功能安装或 sshd 启动失败；请刷新状态并检查 Windows 管理员权限、功能源和策略。".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::device_config::{Role, RuntimeSnapshot};
    use serde_json::json;

    fn status(config_state: &'static str, desired_role: Option<Role>) -> RuntimeSnapshot {
        RuntimeSnapshot {
            config_state,
            device_id: None,
            desired_role,
            audio_mode: None,
            error: None,
        }
    }

    #[test]
    fn requires_a_valid_saved_shared_host_role() {
        assert!(require_shared_host(&status("valid", Some(Role::SharedHost))).is_ok());
        assert!(require_shared_host(&status("missing", None)).is_err());
        assert!(require_shared_host(&status("invalid", Some(Role::SharedHost))).is_err());
        assert!(require_shared_host(&status("valid", Some(Role::Standalone))).is_err());
        assert!(require_shared_host(&status("valid", Some(Role::RemoteClient))).is_err());
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
        assert_eq!(status.fields["ready"], "系统功能已安装且 sshd 正在运行");
    }

    #[test]
    fn reports_ready_only_when_capability_and_service_are_running() {
        let status = OpenSshServerStatus::from_json(json!({
            "capability_state":"Installed",
            "service_present":true,
            "service_state":"Running",
            "startup_type":"Manual"
        }))
        .unwrap();
        assert!(status.ready);
        assert_eq!(status.startup_type.as_deref(), Some("Manual"));
    }

    #[test]
    fn read_only_status_can_report_service_readiness_without_elevated_capability_query() {
        let status = OpenSshServerStatus::from_json(json!({
            "capability_state":"Unknown",
            "service_present":true,
            "service_state":"Running",
            "startup_type":"Manual"
        }))
        .unwrap();
        assert!(status.ready);
        assert_eq!(status.capability_state, "Unknown");
        assert!(status.fields["capability_state"].contains("未提权"));
    }

    #[test]
    fn normalizes_cim_auto_start_mode_without_losing_running_service() {
        let status = OpenSshServerStatus::from_json(json!({
            "capability_state":"Unknown",
            "service_present":true,
            "service_state":"Running",
            "startup_type":"Auto"
        })).unwrap();
        assert!(status.ready);
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

    #[cfg(windows)]
    #[test]
    #[ignore = "人工只读验收：查询当前 Windows 可选功能与 sshd 状态，不修改系统"]
    fn reads_current_windows_open_ssh_server_state_without_changes() {
        let status = open_ssh_server_status().expect("当前 Windows OpenSSH Server 状态应可查询");
        assert_eq!(status.feature_id, OPENSSH_SERVER_ID);
        assert_eq!(
            status.ready,
            status.capability_state == "Installed"
                && status.service_present
                && status.service_state.as_deref() == Some("Running")
        );
    }
}
