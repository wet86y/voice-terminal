//! VoiceTerminalUIDemo（前端演示启动程序）：增量构建调试宿主，再启动纯前端演示。
#![cfg_attr(windows, windows_subsystem = "windows")]

use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

fn source_root(executable: &Path) -> Result<PathBuf, String> {
    executable.ancestors().skip(1).find(|directory| {
        directory.join("apps/frontend/package.json").is_file()
            && directory.join("scripts/start-desktop.ps1").is_file()
    }).map(Path::to_path_buf).ok_or_else(||
        "无法定位前端源码。请将 VoiceTerminalUIDemo.exe（演示入口）放在项目目录或其 .runtime/delivery（本机交付目录）中。".into())
}

fn start() -> Result<(), String> {
    if std::env::args_os().len() != 1 {
        return Err("演示入口不接受业务参数；直接双击即可启动前端演示。".into());
    }
    let root = source_root(&std::env::current_exe().map_err(|e| e.to_string())?)?;
    let logs = root.join(".runtime/desktop-ui-preview");
    fs::create_dir_all(&logs).map_err(|e| e.to_string())?;
    let log = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(logs.join("launcher.log"))
        .map_err(|e| e.to_string())?;
    let system = std::env::var_os("SystemRoot").ok_or("无法定位系统脚本环境。")?;
    let mut command =
        Command::new(PathBuf::from(system).join("System32/WindowsPowerShell/v1.0/powershell.exe"));
    command
        .args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"])
        .arg(root.join("scripts/start-desktop.ps1"))
        .arg("-UiPreview")
        .current_dir(&root)
        .stdin(Stdio::null())
        .stderr(log.try_clone().map_err(|e| e.to_string())?)
        .stdout(log);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000); // CREATE_NO_WINDOW（隐藏构建控制台）。
    }
    let result = command.status().map_err(|e| format!("演示启动失败：{e}"))?;
    if !result.success() {
        return Err(format!("前端演示启动失败。请检查 Node.js（脚本运行时）、原生构建工具和 5174 端口；日志位于 {}。", logs.join("launcher.log").display()));
    }
    Ok(())
}

fn main() {
    if let Err(message) = start() {
        #[cfg(windows)]
        voice_desktop_common::windows::error(&message);
        #[cfg(not(windows))]
        eprintln!("{message}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_source_from_nested_delivery_without_business_environment() {
        let root = std::env::temp_dir().join(format!("voice-ui-launcher-{}", std::process::id()));
        fs::create_dir_all(root.join("apps/frontend")).unwrap();
        fs::create_dir_all(root.join("scripts")).unwrap();
        fs::write(root.join("apps/frontend/package.json"), b"{}").unwrap();
        fs::write(root.join("scripts/start-desktop.ps1"), b"").unwrap();
        assert_eq!(
            source_root(&root.join(".runtime/delivery/VoiceTerminalUIDemo.exe")).unwrap(),
            root
        );
        fs::remove_dir_all(&root).unwrap();
        assert!(source_root(&root.join("VoiceTerminalUIDemo.exe")).is_err());
    }
}
