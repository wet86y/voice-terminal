#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
use std::process::Command;
use voice_desktop_common::LaunchContext;

fn start() -> Result<(), String> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.iter().any(|arg| arg == "--ui-preview") {
        return Err("桌面样例只能从调试版 VoiceTerminalDesktop.exe（桌面主程序）启动。".into());
    }
    if !cfg!(debug_assertions)
        && args
            .iter()
            .any(|arg| ["--dev", "--diagnostic"].contains(&arg.as_str()))
    {
        return Err("正式根入口不接受 --dev 或 --diagnostic（调试启动参数）。".into());
    }
    let context = LaunchContext::from_args()?;
    let desktop = context.program.join("VoiceTerminalDesktop.exe");
    if !desktop.is_file() {
        return Err("程序包缺少 VoiceTerminalDesktop.exe（桌面主程序）。".into());
    }
    Command::new(desktop)
        .envs(context.environment())
        .current_dir(&context.program)
        .args(args)
        .spawn()
        .map_err(|e| format!("无法建立桌面窗口：{e}"))?;
    Ok(()) // 根入口不保留第二套托盘／服务管理进程。
}
fn main() {
    if let Err(message) = start() {
        voice_desktop_common::windows::error(&message);
    }
}
