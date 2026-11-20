//! PowerShell（脚本宿主）：按用户选择和任务能力发现解释器，写入失败后不切换版本重复执行。
use std::{
    collections::HashMap,
    path::{Path, PathBuf},
    sync::{Mutex, OnceLock},
    time::Duration,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Purpose {
    System,      // System（系统签名和网络管理模块）。
    Desktop,     // Desktop（单线程桌面自动化组件）。
    Launch,      // Launch（跨会话启动所需的计划任务模块）。
    Maintenance, // Maintenance（提权维护：只使用受系统目录保护的已验证解释器）。
}

#[derive(Clone, Debug)]
pub struct Host {
    pub executable: PathBuf, // executable（已验证的解释器绝对路径）。
    pub module_path: String, // module_path（解释器自身及 Windows 系统模块路径）。
}

impl Host {
    pub fn command(&self) -> std::process::Command {
        let mut command = std::process::Command::new(&self.executable);
        command
            .args(["-NoLogo", "-NoProfile", "-NonInteractive"])
            .env("PSModulePath", &self.module_path);
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW（不创建控制台窗口）。
            if let Ok(root) = windows_root() {
                command.env("SystemRoot", root);
            }
        }
        command
    }
}

#[cfg(windows)]
pub(crate) fn windows_root() -> Result<PathBuf, String> {
    use std::os::windows::ffi::OsStringExt;
    let mut buffer = vec![0; 32768];
    let length = unsafe {
        windows_sys::Win32::System::SystemInformation::GetWindowsDirectoryW(
            buffer.as_mut_ptr(),
            buffer.len() as u32,
        )
    } as usize;
    if length == 0 || length >= buffer.len() {
        return Err("Windows 系统目录查询失败。".into());
    }
    Ok(PathBuf::from(std::ffi::OsString::from_wide(
        &buffer[..length],
    )))
}

#[cfg(windows)]
pub(crate) fn known_folder(id: &windows_sys::core::GUID) -> Result<PathBuf, String> {
    use std::{os::windows::ffi::OsStringExt, ptr};
    let mut value = ptr::null_mut();
    if unsafe {
        windows_sys::Win32::UI::Shell::SHGetKnownFolderPath(id, 0, ptr::null_mut(), &mut value)
    } < 0
        || value.is_null()
    {
        return Err("系统受管目录查询失败。".into());
    }
    let mut length = 0;
    unsafe {
        while length < 32768 && *value.add(length) != 0 {
            length += 1;
        }
        let result = PathBuf::from(std::ffi::OsString::from_wide(std::slice::from_raw_parts(
            value, length,
        )));
        windows_sys::Win32::System::Com::CoTaskMemFree(value.cast());
        if length == 32768 {
            return Err("系统目录长度无效。".into());
        }
        Ok(result)
    }
}

#[cfg(windows)]
fn protected_host(path: &Path) -> Result<bool, String> {
    let program_files = known_folder(&windows_sys::Win32::UI::Shell::FOLDERID_ProgramFiles)?;
    let roots = [
        windows_root()?.join("System32/WindowsPowerShell"),
        program_files.join("PowerShell"),
        program_files.join("WindowsApps"),
    ];
    let path = path.to_string_lossy().to_lowercase();
    Ok(roots.iter().any(|root| {
        crate::canonical_path(root).is_ok_and(|root| {
            path.starts_with(
                &(root
                    .to_string_lossy()
                    .to_lowercase()
                    .trim_end_matches('\\')
                    .to_owned()
                    + "\\"),
            )
        })
    }))
}

/// select（能力选择）：用户指定 VOICE_POWERSHELL_PATH（解释器绝对路径）时先验证；自动模式在执行前探测兼容版本。
pub fn select(purpose: Purpose) -> Result<Host, String> {
    static CACHE: OnceLock<Mutex<HashMap<(Purpose, Option<std::ffi::OsString>), Host>>> =
        OnceLock::new();
    let preference = std::env::var_os("VOICE_POWERSHELL_PATH");
    let key = (purpose, preference.clone());
    let mut cache = CACHE
        .get_or_init(Default::default)
        .lock()
        .map_err(|_| "脚本宿主选择状态不可用。")?;
    if let Some(host) = cache.get(&key).filter(|host| host.executable.is_file()) {
        return Ok(host.clone());
    }
    let mut failures = Vec::new();
    for path in candidates(preference.as_deref()) {
        match probe(&path, purpose) {
            Ok(host) => {
                cache.insert(key, host.clone());
                return Ok(host);
            }
            Err(error) => failures.push(error),
        }
    }
    Err(format!(
        "没有满足当前任务能力的 PowerShell（脚本运行环境）；{}",
        failures
            .last()
            .map(String::as_str)
            .unwrap_or("请检查本机安装或用户指定路径")
    ))
}

fn candidates(preference: Option<&std::ffi::OsStr>) -> Vec<PathBuf> {
    if let Some(path) = preference {
        return vec![PathBuf::from(path)];
    }
    let mut paths = Vec::new();
    if let Some(search) = std::env::var_os("PATH") {
        for directory in std::env::split_paths(&search).filter(|path| path.is_absolute()) {
            paths.push(directory.join("pwsh.exe"));
        }
    }
    for variable in ["ProgramFiles", "LOCALAPPDATA"] {
        if let Some(root) = std::env::var_os(variable) {
            let root = PathBuf::from(root);
            let base = if variable == "LOCALAPPDATA" {
                root.join("Microsoft/PowerShell")
            } else {
                root.join("PowerShell")
            };
            if let Ok(entries) = std::fs::read_dir(base) {
                let mut installed = entries
                    .flatten()
                    .map(|entry| entry.path().join("pwsh.exe"))
                    .collect::<Vec<_>>();
                installed.sort();
                installed.reverse();
                paths.extend(installed);
            }
        }
    }
    if let Some(root) = std::env::var_os("SystemRoot") {
        paths.push(PathBuf::from(root).join("System32/WindowsPowerShell/v1.0/powershell.exe"));
    }
    let mut seen = std::collections::HashSet::new();
    paths.retain(|path| path.is_file() && seen.insert(path.to_string_lossy().to_lowercase()));
    paths
}

pub(crate) fn probe(path: &Path, purpose: Purpose) -> Result<Host, String> {
    if !path.is_absolute() {
        return Err("用户指定的 PowerShell 路径必须是绝对路径。".into());
    }
    let path = crate::canonical_path(path)?;
    #[cfg(windows)]
    if purpose == Purpose::Maintenance && !protected_host(&path)? {
        return Err("系统维护需要位于 Windows 或 Program Files（受保护安装目录）的 PowerShell；用户便携目录仅用于普通权限任务。".into());
    }
    if !matches!(
        path.file_name()
            .and_then(|name| name.to_str())
            .map(str::to_ascii_lowercase)
            .as_deref(),
        Some("powershell.exe" | "pwsh.exe")
    ) {
        return Err("脚本宿主必须是 powershell.exe 或 pwsh.exe（正式解释器）。".into());
    }
    #[cfg(windows)]
    verify_signature(&path)?;
    let system = PathBuf::from(std::env::var_os("SystemRoot").ok_or("Windows 系统目录不可用。")?)
        .join("System32/WindowsPowerShell/v1.0/Modules");
    let host = Host {
        module_path: format!(
            "{};{}",
            path.parent()
                .ok_or("解释器目录不可用。")?
                .join("Modules")
                .display(),
            system.display()
        ),
        executable: path,
    };
    let capabilities = match purpose {
        Purpose::System | Purpose::Maintenance => "Get-Command Get-AuthenticodeSignature,Get-NetFirewallRule,Get-NetAdapter,Get-NetIPAddress,Get-NetConnectionProfile,Get-WindowsCapability -ErrorAction Stop | Out-Null",
        Purpose::Desktop => "Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes,System.Drawing,System.Windows.Forms -ErrorAction Stop",
        Purpose::Launch => "Get-Command Register-ScheduledTask,Unregister-ScheduledTask,Start-ScheduledTask -ErrorAction Stop | Out-Null",
    };
    let script = format!(
        r#"[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$ErrorActionPreference='Stop'
if (-not [Environment]::Is64BitProcess) {{ throw '需要 64 位脚本宿主' }}
$version=$PSVersionTable.PSVersion
if (($version.Major -ne 5 -or $version.Minor -lt 1) -and $version.Major -ne 7) {{ throw '解释器版本未通过支持范围检查' }}
if ([string]$version -match '-') {{ throw '预览版本不用于产品系统维护' }}
{capabilities}
Write-Output 'VoiceTerminalPowerShellReady'"#
    );
    let (code, output, _) = execute(&host, &script, Duration::from_secs(15), &mut |_| {})?;
    if code != 0 || output.trim() != "VoiceTerminalPowerShellReady" {
        return Err(format!(
            "{} 未通过任务能力检查（退出码 {code}）。",
            host.executable.display()
        ));
    }
    Ok(host)
}

/// execute（有界执行）：长脚本经输入管道传递，不落盘；同时排空标准和错误输出并保留退出码。
pub fn execute(
    host: &Host,
    script: &str,
    timeout: Duration,
    on_wait: &mut dyn FnMut(Duration),
) -> Result<(u32, String, String), String> {
    use std::{
        io::{Read, Write},
        process::Stdio,
        thread,
        time::Instant,
    };
    let mut child = host.command().args(["-STA", "-Command", "[Console]::InputEncoding=[Text.UTF8Encoding]::new($false); [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); & ([scriptblock]::Create([Console]::In.ReadToEnd()))"])
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().map_err(|error| format!("无法启动脚本宿主：{error}"))?;
    let mut input = child.stdin.take().ok_or("脚本输入管道不可用。")?;
    let script = script.as_bytes().to_vec();
    let writer = thread::spawn(move || input.write_all(&script));
    let mut output = child.stdout.take().ok_or("脚本输出管道不可用。")?;
    let mut errors = child.stderr.take().ok_or("脚本错误管道不可用。")?;
    let output = thread::spawn(move || {
        let mut bytes = Vec::new();
        output.read_to_end(&mut bytes).map(|_| bytes)
    });
    let errors = thread::spawn(move || {
        let mut bytes = Vec::new();
        errors.read_to_end(&mut bytes).map(|_| bytes)
    });
    let started = Instant::now();
    let mut reported = started;
    loop {
        if let Some(status) = child
            .try_wait()
            .map_err(|error| format!("无法读取脚本状态：{error}"))?
        {
            writer
                .join()
                .map_err(|_| "脚本写入线程异常。")?
                .map_err(|error| format!("脚本写入失败：{error}"))?;
            let stdout = output
                .join()
                .map_err(|_| "脚本输出线程异常。")?
                .map_err(|error| error.to_string())?;
            let stderr = errors
                .join()
                .map_err(|_| "脚本错误线程异常。")?
                .map_err(|error| error.to_string())?;
            return Ok((
                status.code().unwrap_or(1) as u32,
                String::from_utf8_lossy(&stdout).trim().to_owned(),
                String::from_utf8_lossy(&stderr).trim().to_owned(),
            ));
        }
        if started.elapsed() >= timeout {
            let _ = child.kill();
            let _ = child.wait();
            let _ = writer.join();
            let _ = output.join();
            let _ = errors.join();
            return Err("脚本执行超时；结果未知，未切换解释器重复执行。".into());
        }
        if reported.elapsed() >= Duration::from_secs(15) {
            on_wait(started.elapsed());
            reported = Instant::now();
        }
        thread::sleep(Duration::from_millis(50));
    }
}

#[cfg(windows)]
fn verify_signature(path: &Path) -> Result<(), String> {
    use std::{mem::size_of, os::windows::ffi::OsStrExt, ptr};
    use windows_sys::Win32::Security::WinTrust::*;
    let wide = path
        .as_os_str()
        .encode_wide()
        .chain(Some(0))
        .collect::<Vec<_>>();
    let mut file = WINTRUST_FILE_INFO {
        cbStruct: size_of::<WINTRUST_FILE_INFO>() as u32,
        pcwszFilePath: wide.as_ptr(),
        ..unsafe { std::mem::zeroed() }
    };
    let mut data = WINTRUST_DATA {
        cbStruct: size_of::<WINTRUST_DATA>() as u32,
        dwUIChoice: WTD_UI_NONE,
        fdwRevocationChecks: WTD_REVOKE_NONE,
        dwUnionChoice: WTD_CHOICE_FILE,
        Anonymous: WINTRUST_DATA_0 { pFile: &mut file },
        dwStateAction: WTD_STATEACTION_VERIFY,
        dwProvFlags: WTD_CACHE_ONLY_URL_RETRIEVAL,
        ..unsafe { std::mem::zeroed() }
    };
    let mut action = WINTRUST_ACTION_GENERIC_VERIFY_V2;
    let status = unsafe {
        WinVerifyTrust(
            ptr::null_mut(),
            &mut action,
            (&mut data as *mut WINTRUST_DATA).cast(),
        )
    };
    data.dwStateAction = WTD_STATEACTION_CLOSE;
    unsafe {
        WinVerifyTrust(
            ptr::null_mut(),
            &mut action,
            (&mut data as *mut WINTRUST_DATA).cast(),
        );
    }
    // Windows（操作系统）自带解释器通常由目录签名覆盖，而非文件内嵌签名。
    if status == 0 || status as u32 == 0x800b0100 && verify_catalog_signature(path) {
        return Ok(());
    }
    if status != 0 {
        return Err(format!(
            "脚本解释器签名未通过系统信任验证（状态 {status}），未执行。"
        ));
    }
    Ok(())
}

#[cfg(windows)]
fn verify_catalog_signature(path: &Path) -> bool {
    use std::{
        mem::size_of,
        os::windows::{ffi::OsStrExt, io::AsRawHandle},
        ptr,
    };
    use windows_sys::Win32::Security::{Cryptography::Catalog::*, WinTrust::*};
    let Ok(file) = std::fs::File::open(path) else {
        return false;
    };
    let filename = path
        .as_os_str()
        .encode_wide()
        .chain(Some(0))
        .collect::<Vec<_>>();
    struct CatalogAdmin(isize);
    impl Drop for CatalogAdmin {
        fn drop(&mut self) {
            unsafe {
                CryptCATAdminReleaseContext(self.0, 0);
            }
        }
    }
    // SHA256/SHA1（系统目录使用的摘要算法）均交给 Windows 信任提供程序校验，不因路径位于系统目录而跳过签名。
    for algorithm in ["SHA256", "SHA1"] {
        let algorithm = algorithm.encode_utf16().chain(Some(0)).collect::<Vec<_>>();
        let mut admin = 0;
        if unsafe {
            CryptCATAdminAcquireContext2(
                &mut admin,
                ptr::null(),
                algorithm.as_ptr(),
                ptr::null(),
                0,
            )
        } == 0
        {
            continue;
        }
        let admin = CatalogAdmin(admin);
        let mut length = 0;
        unsafe {
            CryptCATAdminCalcHashFromFileHandle2(
                admin.0,
                file.as_raw_handle().cast(),
                &mut length,
                ptr::null_mut(),
                0,
            );
        }
        if length == 0 || length > 128 {
            continue;
        }
        let mut hash = vec![0; length as usize];
        if unsafe {
            CryptCATAdminCalcHashFromFileHandle2(
                admin.0,
                file.as_raw_handle().cast(),
                &mut length,
                hash.as_mut_ptr(),
                0,
            )
        } == 0
        {
            continue;
        }
        let tag = hash
            .iter()
            .map(|byte| format!("{byte:02X}"))
            .collect::<String>()
            .encode_utf16()
            .chain(Some(0))
            .collect::<Vec<_>>();
        let mut previous = 0;
        loop {
            let catalog = unsafe {
                CryptCATAdminEnumCatalogFromHash(admin.0, hash.as_ptr(), length, 0, &mut previous)
            };
            if catalog == 0 {
                break;
            }
            previous = catalog;
            let mut info = CATALOG_INFO {
                cbStruct: size_of::<CATALOG_INFO>() as u32,
                ..unsafe { std::mem::zeroed() }
            };
            if unsafe { CryptCATCatalogInfoFromContext(catalog, &mut info, 0) } == 0 {
                continue;
            }
            let mut member = WINTRUST_CATALOG_INFO {
                cbStruct: size_of::<WINTRUST_CATALOG_INFO>() as u32,
                pcwszCatalogFilePath: info.wszCatalogFile.as_ptr(),
                pcwszMemberTag: tag.as_ptr(),
                pcwszMemberFilePath: filename.as_ptr(),
                hMemberFile: file.as_raw_handle().cast(),
                pbCalculatedFileHash: hash.as_mut_ptr(),
                cbCalculatedFileHash: length,
                hCatAdmin: admin.0,
                ..unsafe { std::mem::zeroed() }
            };
            let mut data = WINTRUST_DATA {
                cbStruct: size_of::<WINTRUST_DATA>() as u32,
                dwUIChoice: WTD_UI_NONE,
                fdwRevocationChecks: WTD_REVOKE_NONE,
                dwUnionChoice: WTD_CHOICE_CATALOG,
                Anonymous: WINTRUST_DATA_0 {
                    pCatalog: &mut member,
                },
                dwStateAction: WTD_STATEACTION_VERIFY,
                dwProvFlags: WTD_CACHE_ONLY_URL_RETRIEVAL,
                ..unsafe { std::mem::zeroed() }
            };
            let mut action = WINTRUST_ACTION_GENERIC_VERIFY_V2;
            let result = unsafe {
                WinVerifyTrust(
                    ptr::null_mut(),
                    &mut action,
                    (&mut data as *mut WINTRUST_DATA).cast(),
                )
            };
            data.dwStateAction = WTD_STATEACTION_CLOSE;
            unsafe {
                WinVerifyTrust(
                    ptr::null_mut(),
                    &mut action,
                    (&mut data as *mut WINTRUST_DATA).cast(),
                );
            }
            if result == 0 {
                unsafe {
                    CryptCATAdminReleaseCatalogContext(admin.0, catalog, 0);
                }
                return true;
            }
        }
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn explicit_preference_is_not_silently_replaced() {
        assert_eq!(
            candidates(Some(std::ffi::OsStr::new("C:/用户选择/pwsh.exe"))),
            vec![PathBuf::from("C:/用户选择/pwsh.exe")]
        );
        assert!(probe(Path::new("relative/pwsh.exe"), Purpose::System).is_err());
    }
    #[cfg(windows)]
    #[test]
    fn selected_system_host_reads_unicode_and_preserves_exit_code() {
        let host = select(Purpose::System).unwrap();
        let (code, output, _) = execute(&host, "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); Write-Output '中文回执'; exit 12", Duration::from_secs(10), &mut |_| {}).unwrap();
        assert_eq!(code, 12);
        assert_eq!(output, "中文回执");
    }

    #[cfg(windows)]
    #[test]
    fn installed_stable_hosts_are_probed_before_running_tasks() {
        let mut versions = std::collections::HashSet::new();
        let mut available = vec![select(Purpose::System).unwrap().executable];
        let system = windows_root()
            .unwrap()
            .join("System32/WindowsPowerShell/v1.0/powershell.exe");
        if system.is_file() {
            available.push(system);
        }
        for candidate in available {
            let host = probe(&candidate, Purpose::System).unwrap();
            let (code, output, _) = execute(&host, "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); Write-Output ($PSVersionTable.PSVersion.Major); Write-Output '双版本中文回执'", Duration::from_secs(10), &mut |_| {}).unwrap();
            assert_eq!(code, 0);
            assert!(output.ends_with("双版本中文回执"));
            versions.insert(output.lines().next().unwrap().to_owned());
        }
        assert!(!versions.is_empty());
        assert!(!protected_host(&windows_root().unwrap().join("Temp/pwsh.exe")).unwrap());
        let maintenance = select(Purpose::Maintenance).unwrap();
        assert!(protected_host(&maintenance.executable).unwrap());
    }
}
