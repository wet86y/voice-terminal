#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
use std::{
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{atomic::AtomicBool, Arc},
};
use voice_desktop_common::{
    installation,
    workspace::{self, Locator},
    LaunchContext,
};
mod workspace_ui;
fn value<'a>(args: &'a [String], key: &str) -> Option<&'a str> {
    args.iter()
        .position(|v| v == key)
        .and_then(|i| args.get(i + 1))
        .map(String::as_str)
}
fn run_task(path: &Path) -> Result<(), String> {
    // 根入口恢复时先交给目标暂存区的正式辅助程序，避免原入口被自身占用。
    let executable = std::env::current_exe().map_err(|e| e.to_string())?;
    let helper = path
        .parent()
        .ok_or("工作区事务缺少暂存目录")?
        .join("workspace-helper.exe");
    if voice_desktop_common::canonical_path(&executable)?
        != workspace::safe_path(path.parent().unwrap(), "workspace-helper.exe")?
    {
        let locator = Locator::for_task(path)?;
        let target = locator.handoff_root(path).or_else(|_| {
            let record = voice_desktop_common::resources::load(path)?;
            let target = Path::new(record["target_root"].as_str().ok_or("事务缺少目标目录")?);
            locator.validate_target(target)?;
            if voice_desktop_common::canonical_path(path)?
                != voice_desktop_common::canonical_path(&target.join("tmp/workspace-task.json"))?
            {
                return Err("工作区恢复记录位置无效。".into());
            }
            voice_desktop_common::canonical_path(target)
        })?;
        if helper != target.join("tmp/workspace-helper.exe") {
            return Err("恢复辅助程序位置无效。".into());
        }
        std::fs::copy(&executable, &helper).map_err(|e| e.to_string())?;
        let mut command = Command::new(&helper);
        command
            .arg("--workspace-task")
            .arg(path)
            .current_dir(helper.parent().unwrap());
        command
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }
        command
            .spawn()
            .map_err(|e| format!("无法启动正式恢复辅助程序：{e}"))?;
        return Ok(());
    }
    let locator = Locator::for_task(path)?;
    let task = path.to_path_buf();
    workspace_ui::run(task, locator)
}
fn desktop_executable(context: &LaunchContext) -> Result<PathBuf, String> {
    let desktop = context.installed_program().join("VoiceTerminalDesktop.exe");
    if !desktop.is_file() {
        return Err(format!(
            "正式本体缺少桌面主程序：{}；请使用安装器重装。",
            desktop.display()
        ));
    }
    Ok(desktop)
}
fn start() -> Result<(), String> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args == ["--verify-package"] {
        return installation::verify_installed(
            std::env::current_exe()
                .map_err(|e| e.to_string())?
                .parent()
                .ok_or("入口缺少工作区")?,
        );
    }
    if let Some(path) = value(&args, "--workspace-task") {
        return run_task(Path::new(path));
    }
    if let Some(source) = value(&args, "--retain-installer") {
        let root = Path::new(value(&args, "--target-root").ok_or("缺少校验程序目标工作区")?);
        let package = Path::new(value(&args, "--program-package").ok_or("缺少同包交付的程序包")?);
        return voice_desktop_common::app_update::retain_local_delivery(
            root,
            Path::new(source),
            package,
        );
    }
    // install-package（本地发布辅助模式）：与安装器共用本体事务及强制兼容处理。
    if let Some(package) = value(&args, "--install-package") {
        let root = Path::new(value(&args, "--target-root").ok_or("本地发布缺少目标工作区")?);
        let payload = std::fs::read(package).map_err(|e| e.to_string())?;
        let cancel = Arc::new(AtomicBool::new(false));
        let mut previous = String::new();
        let mut reported = std::time::Instant::now();
        let mut progress = |phase: &str, done, total| {
            if previous != phase
                || done == total
                || reported.elapsed() >= std::time::Duration::from_secs(1)
            {
                println!("{phase}：{done}/{total}");
                previous = phase.to_owned();
                reported = std::time::Instant::now();
            }
        };
        installation::install(&payload, root, cancel.clone(), &mut progress)?;
        return voice_desktop_common::compatibility::enforce_upgrade(root, cancel, &mut progress);
    }
    let executable = std::env::current_exe().map_err(|e| e.to_string())?;
    let root = executable.parent().ok_or("正式入口缺少工作区目录")?;
    if args == ["--agent-stdio"] {
        return voice_desktop_common::agent_debug::relay(root);
    }
    if args == ["--exit"] {
        return voice_desktop_common::agent_debug::request_exit(root);
    }
    if let Some(task) = workspace::pending_task(root)? {
        return run_task(&task);
    }
    if root.join("tmp/update-transaction.json").exists() {
        installation::ensure_idle(root)?;
        let _guard = installation::WorkspaceGuard::acquire(root)?;
        workspace::recover_install(root)?;
    }
    let context = LaunchContext::from_executable(&executable, &args)?;
    if args.iter().any(|arg| arg == "--agent-debug") && value(&args, "--startup-handoff").is_none()
    {
        return start_with_receipt(root, &executable, &context, &args, true);
    }
    if args.is_empty() && voice_desktop_common::agent_debug::session_id()? == 0 {
        return start_with_receipt(root, &executable, &context, &args, false);
    }
    spawn_desktop(&context, &args)
}

fn spawn_desktop(context: &LaunchContext, args: &[String]) -> Result<(), String> {
    let mut command = Command::new(desktop_executable(&context)?);
    context.configure_command(&mut command);
    // 独立桌面进程不继承调用者管道，正式启动命令与安装器都能完成退出。
    command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        use windows_sys::Win32::{
            Foundation::{SetHandleInformation, HANDLE_FLAG_INHERIT, INVALID_HANDLE_VALUE},
            System::Console::{
                GetStdHandle, STD_ERROR_HANDLE, STD_INPUT_HANDLE, STD_OUTPUT_HANDLE,
            },
        };
        // 新桌面进程使用自己的空句柄。关闭根入口的标准句柄继承位，SSH 和调用方才能收到文件结束。
        for standard in [STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, STD_ERROR_HANDLE] {
            unsafe {
                let handle = GetStdHandle(standard);
                if !handle.is_null() && handle != INVALID_HANDLE_VALUE {
                    SetHandleInformation(handle, HANDLE_FLAG_INHERIT, 0);
                }
            }
        }
        command.creation_flags(0x08000000);
    }
    command
        .current_dir(context.installed_program())
        .args(args)
        .spawn()
        .map_err(|e| format!("无法建立正式桌面窗口：{e}"))?;
    Ok(())
}

fn start_with_receipt(
    root: &Path,
    entry: &Path,
    context: &LaunchContext,
    args: &[String],
    debug: bool,
) -> Result<(), String> {
    use voice_desktop_common::agent_debug;
    if !agent_debug::allowed(&context.data)? {
        return Err("请在设置的关于页允许外部调试。".into());
    }
    installation::ensure_idle(root)?;
    drop(installation::WorkspaceGuard::acquire(root)?);
    let pid = if agent_debug::session_id()? == 0 {
        if debug && args != ["--agent-debug"] {
            return Err(
                "跨会话启动使用 --agent-debug（调试模式），设备角色沿用当前工作区设置。".into(),
            );
        }
        start_in_desktop(root, entry, debug)?
    } else {
        installation::launch_with_handoff(root, |token| {
            let mut launch_args = args.to_vec();
            launch_args.extend(["--startup-handoff".into(), token.into()]);
            spawn_desktop(context, &launch_args)
        })?
    };
    println!(
        "{}",
        serde_json::json!({"pid":pid,"mode":if debug {"agent-debug"} else {"normal"},"fields":{"pid":"正式宿主进程号","mode":"启动模式"}})
    );
    Ok(())
}

fn start_in_desktop(root: &Path, entry: &Path, debug: bool) -> Result<u32, String> {
    use std::os::windows::process::CommandExt;
    let session = voice_desktop_common::agent_debug::interactive_session()?;
    let task = format!("VoiceTerminal-Agent-{}", workspace::new_id());
    let shell = voice_desktop_common::powershell::select(
        voice_desktop_common::powershell::Purpose::Launch,
    )?;
    let run = |script: &str, token: &str| {
        shell
            .command()
            .args([
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ])
            .env("VOICE_AGENT_LAUNCH_TASK", &task)
            .env("VOICE_AGENT_LAUNCH_ENTRY", entry)
            .env("VOICE_AGENT_LAUNCH_TOKEN", token)
            .env(
                "VOICE_AGENT_LAUNCH_MODE",
                if debug { "--agent-debug " } else { "" },
            )
            .creation_flags(0x08000000)
            .output()
            .map_err(|error| error.to_string())
    };
    let result = installation::launch_with_handoff(root, |token| {
        let output = run(
            r#"
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$arguments = $env:VOICE_AGENT_LAUNCH_MODE + '--startup-handoff ' + $env:VOICE_AGENT_LAUNCH_TOKEN
$action = New-ScheduledTaskAction -Execute $env:VOICE_AGENT_LAUNCH_ENTRY -Argument $arguments
$principal = New-ScheduledTaskPrincipal -UserId $identity.User.Value -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $env:VOICE_AGENT_LAUNCH_TASK -Action $action -Principal $principal -Settings $settings | Out-Null
Start-ScheduledTask -TaskName $env:VOICE_AGENT_LAUNCH_TASK
"#,
            token,
        )?;
        if output.status.success() {
            Ok(())
        } else {
            Err("无法为当前账户的交互桌面启动正式调试实例。请确认该账户已登录桌面并具有按需计划任务权限。".into())
        }
    });
    let cleanup = run("Unregister-ScheduledTask -TaskName $env:VOICE_AGENT_LAUNCH_TASK -Confirm:$false -ErrorAction SilentlyContinue", "");
    if result.is_ok() && !cleanup.is_ok_and(|output| output.status.success()) {
        eprintln!("调试启动交接已完成，计划任务清理调用失败。");
    }
    let pid = result?;
    if voice_desktop_common::agent_debug::process_session_id(pid)? != session {
        return Err("窗口交接来自另一交互会话。".into());
    }
    Ok(pid)
}
fn main() {
    if let Err(message) = start() {
        eprintln!("{message}");
        if voice_desktop_common::agent_debug::session_id() == Ok(0)
            || std::env::args().any(|arg| {
                matches!(
                    arg.as_str(),
                    "--install-package"
                        | "--verify-package"
                        | "--retain-installer"
                        | "--exit"
                        | "--agent-debug"
                        | "--agent-stdio"
                )
            })
        {
            std::process::exit(1);
        }
        voice_desktop_common::windows::error(&message);
        std::process::exit(1);
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn debug_and_normal_use_same_installed_host() {
        let root = std::env::temp_dir().join(format!("voice-entry-{}", workspace::new_id()));
        std::fs::create_dir_all(root.join("app")).unwrap();
        std::fs::write(root.join("app/VoiceTerminalDesktop.exe"), b"formal").unwrap();
        let mut context = LaunchContext {
            channel: voice_desktop_common::LaunchChannel::Product,
            workspace_id: "test".into(),
            install: root.clone(),
            program: root.join("source"),
            data: root.join("data"),
            python: root.join("runtimes/python/python.exe"),
            models: None,
            webview2: None,
            dsh: None,
            node: None,
            dev: true,
            diagnostic: false,
        };
        assert_eq!(
            desktop_executable(&context).unwrap(),
            root.join("app/VoiceTerminalDesktop.exe")
        );
        context.dev = false;
        context.program = root.join("app");
        assert_eq!(
            desktop_executable(&context).unwrap(),
            root.join("app/VoiceTerminalDesktop.exe")
        );
        std::fs::remove_dir_all(root).unwrap();
    }
}
