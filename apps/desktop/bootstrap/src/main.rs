#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
use std::{path::{Path,PathBuf},process::{Command,Stdio},sync::{Arc,atomic::AtomicBool}};
use voice_desktop_common::{LaunchContext,workspace::{self,Locator},installation};
mod workspace_ui;
fn value<'a>(args:&'a [String],key:&str)->Option<&'a str>{args.iter().position(|v|v==key).and_then(|i|args.get(i+1)).map(String::as_str)}
fn run_task(path:&Path)->Result<(),String>{
    // 根入口恢复时先交给目标暂存区的正式辅助程序，避免原入口被自身占用。
    let executable=std::env::current_exe().map_err(|e|e.to_string())?;
    let helper=path.parent().ok_or("工作区事务缺少暂存目录")?.join("workspace-helper.exe");
    if voice_desktop_common::canonical_path(&executable)?!=workspace::safe_path(path.parent().unwrap(),"workspace-helper.exe")?{
        let locator=Locator::for_task(path)?;
        let target=locator.handoff_root(path).or_else(|_|{
            let record=voice_desktop_common::resources::load(path)?;
            let target=Path::new(record["target_root"].as_str().ok_or("事务缺少目标目录")?);
            locator.validate_target(target)?;
            if voice_desktop_common::canonical_path(path)?!=voice_desktop_common::canonical_path(&target.join("tmp/workspace-task.json"))?{return Err("工作区恢复记录位置无效。".into());}
            voice_desktop_common::canonical_path(target)
        })?;
        if helper!=target.join("tmp/workspace-helper.exe"){return Err("恢复辅助程序位置无效。".into());}
        std::fs::copy(&executable,&helper).map_err(|e|e.to_string())?;
        let mut command=Command::new(&helper);command.arg("--workspace-task").arg(path).current_dir(helper.parent().unwrap());
        command.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
        #[cfg(windows)]{use std::os::windows::process::CommandExt;command.creation_flags(0x08000000);}
        command.spawn().map_err(|e|format!("无法启动正式恢复辅助程序：{e}"))?;return Ok(());
    }
    let locator=Locator::for_task(path)?;let task=path.to_path_buf();
    workspace_ui::run(task,locator)
}
fn desktop_executable(context:&LaunchContext)->Result<PathBuf,String>{
    let desktop=context.installed_program().join("VoiceTerminalDesktop.exe");
    if !desktop.is_file(){return Err(format!("正式本体缺少桌面主程序：{}；请使用安装器重装。",desktop.display()));}Ok(desktop)
}
fn start()->Result<(),String>{
    let args:Vec<String>=std::env::args().skip(1).collect();
    if args==["--verify-package"]{return installation::verify_installed(std::env::current_exe().map_err(|e|e.to_string())?.parent().ok_or("入口缺少工作区")?);}
    if let Some(path)=value(&args,"--workspace-task"){return run_task(Path::new(path));}
    // install-package（本地发布辅助模式）：与安装器共用本体事务及强制兼容处理。
    if let Some(package)=value(&args,"--install-package"){
        let root=Path::new(value(&args,"--target-root").ok_or("本地发布缺少目标工作区")?);
        let payload=std::fs::read(package).map_err(|e|e.to_string())?;
        let cancel=Arc::new(AtomicBool::new(false));
        let mut previous=String::new();
        let mut reported=std::time::Instant::now();
        let mut progress=|phase:&str,done,total|{
            if previous!=phase||done==total||reported.elapsed()>=std::time::Duration::from_secs(1){
                println!("{phase}：{done}/{total}");previous=phase.to_owned();reported=std::time::Instant::now();
            }
        };
        installation::install(&payload,root,cancel.clone(),&mut progress)?;
        return voice_desktop_common::compatibility::enforce_upgrade(root,cancel,&mut progress);
    }
    let executable=std::env::current_exe().map_err(|e|e.to_string())?;
    let root=executable.parent().ok_or("正式入口缺少工作区目录")?;
    if let Some(task)=workspace::pending_task(root)?{return run_task(&task);}
    if root.join("tmp/update-transaction.json").exists(){installation::ensure_idle(root)?;let _guard=installation::WorkspaceGuard::acquire(root)?;workspace::recover_install(root)?;}
    let context=LaunchContext::from_executable(&executable,&args)?;
    let mut command=Command::new(desktop_executable(&context)?);context.configure_command(&mut command);
    // 独立桌面进程不继承调用者管道，正式启动命令与安装器都能完成退出。
    command.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
    #[cfg(windows)]{use std::os::windows::process::CommandExt;command.creation_flags(0x08000000);}
    command.current_dir(context.installed_program()).args(args).spawn().map_err(|e|format!("无法建立正式桌面窗口：{e}"))?;
    Ok(())
}
fn main(){if let Err(message)=start(){eprintln!("{message}");if std::env::args().any(|arg|arg=="--install-package"||arg=="--verify-package"){std::process::exit(1);}voice_desktop_common::windows::error(&message);std::process::exit(1);}}
#[cfg(test)]mod tests{use super::*;
    #[test]fn debug_and_normal_use_same_installed_host(){let root=std::env::temp_dir().join(format!("voice-entry-{}",workspace::new_id()));std::fs::create_dir_all(root.join("app")).unwrap();std::fs::write(root.join("app/VoiceTerminalDesktop.exe"),b"formal").unwrap();let mut context=LaunchContext{channel:voice_desktop_common::LaunchChannel::Product,workspace_id:"test".into(),install:root.clone(),program:root.join("source"),data:root.join("data"),python:root.join("runtimes/python/python.exe"),models:None,webview2:None,dsh:None,node:None,dev:true,diagnostic:false};assert_eq!(desktop_executable(&context).unwrap(),root.join("app/VoiceTerminalDesktop.exe"));context.dev=false;context.program=root.join("app");assert_eq!(desktop_executable(&context).unwrap(),root.join("app/VoiceTerminalDesktop.exe"));std::fs::remove_dir_all(root).unwrap();}
}
