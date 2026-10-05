//! VoiceTerminalUIDemo（前端演示启动程序）：直接打开已构建的演示主窗口。
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
    let root = voice_desktop_common::canonical_path(&source_root(
        &std::env::current_exe().map_err(|e| e.to_string())?,
    )?)?;
    let desktop = root.join("apps/desktop/target/debug/VoiceTerminalDesktop.exe");
    if !desktop.is_file() {
        return Err("演示主程序尚未构建。请先执行 npm run dev:ui（演示开发）或 npm run desktop:publish（构建并更新两入口），再双击演示入口。".into());
    }
    let logs = root.join(".runtime/desktop-ui-preview");
    fs::create_dir_all(&logs).map_err(|e| e.to_string())?;
    let log = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(logs.join("launcher.log"))
        .map_err(|e| e.to_string())?;
    let mut command = Command::new(desktop);
    command
        .arg("--root")
        .arg(&root)
        .arg("--ui-preview")
        .current_dir(&root)
        .stdin(Stdio::null())
        .stderr(log.try_clone().map_err(|e| e.to_string())?)
        .stdout(log);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000); // CREATE_NO_WINDOW（隐藏调试宿主的控制台）。
    }
    command
        .spawn()
        .map_err(|error| format!("无法打开演示主窗口：{error}"))?;
    // 主窗口负责单实例与前端服务，双击入口不构建源码、不等待服务加载。
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
