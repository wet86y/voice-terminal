#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
use std::{fs, path::Path, process::Command, sync::{Arc, atomic::AtomicBool}};
use voice_desktop_common::{LaunchChannel, LaunchContext, native_ui, workspace::{self, Locator}};

fn value<'a>(args:&'a [String],key:&str)->Option<&'a str>{args.iter().position(|v|v==key).and_then(|i|args.get(i+1)).map(String::as_str)}
fn run_workspace_task(task:&Path,locator:&Locator,title:&str)->Result<(),String>{
    if locator.headless{return workspace::run_task(task,locator,Arc::new(AtomicBool::new(false)),&mut|phase,current,total|println!("{}",serde_json::json!({"phase":phase,"completed_bytes":current,"total_bytes":total})));}
    let task=task.to_path_buf();let selected=locator.clone();
    native_ui::run(title,move|progress|workspace::run_task(&task,&selected,progress.cancel.clone(),&mut|phase,current,total|progress.update(phase,current,total)))
}

fn start() -> Result<(), String> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if LaunchChannel::current()==LaunchChannel::Distribution {
        if args.iter().any(|arg|["--root","--install","--data-dir","--python","--models","--webview2","--dev","--diagnostic","--ui-preview"].contains(&arg.as_str())) {
            return Err("分发入口不接受开发路径或调试参数。".into());
        }
        let locator=Locator::from_args(&args)?;
        if let Some(journal)=value(&args,"--workspace-task") {
            let journal=Path::new(journal);locator.validate_target(journal.parent().and_then(Path::parent).ok_or("工作区事务位置无效")?)?;
            return run_workspace_task(journal,&locator,"工作区切换／迁移");
        }
        if let Ok(root)=locator.resolve(){
            if let Some(task)=workspace::pending_task(&root)? {
                return run_workspace_task(&task,&locator,"恢复工作区事务");
            }
        }
        let root=if locator.headless {
            locator.ensure(Arc::new(AtomicBool::new(false)),&mut|phase,current,total|println!("{}",serde_json::json!({"phase":phase,"completed_bytes":current,"total_bytes":total})))?
        }else{
            let selected=locator.clone();
            native_ui::run("初始化语音终端工作区",move|progress|selected.ensure(progress.cancel.clone(),&mut|phase,current,total|progress.update(phase,current,total)))?
        };
        let context=LaunchContext::portable(&root)?;
        let release:serde_json::Value=serde_json::from_slice(&fs::read(context.program.join("release.json")).map_err(|e|e.to_string())?).map_err(|e|e.to_string())?;
        if release["channel"]!="distribution"{return Err("本体包与分发启动渠道不一致。".into());}
        let executable=std::env::current_exe().map_err(|e|e.to_string())?;
        let managed=root.join("VoiceTerminal.exe");
        if voice_desktop_common::canonical_path(&executable).ok()!=voice_desktop_common::canonical_path(&managed).ok(){
            fs::copy(&executable,&managed).map_err(|e|format!("无法保存工作区入口：{e}"))?;
        }
        if root.join("tmp/resource-transaction.json").is_file(){
            let mut repair=Command::new(&context.python);context.configure_command(&mut repair);
            let output=repair.args(["-X","utf8"]).arg(context.program.join("scripts/manage-resources.py")).args(["status","--root"]).arg(&root).arg("--program").arg(&context.program).current_dir(&context.program).output().map_err(|e|e.to_string())?;
            if !output.status.success(){return Err("资源安装事务未能恢复，请使用组件导入重试。".into());}
        }
        if let Some(target)=value(&args,"--prepare-workspace-task"){
            if !cfg!(feature="qualification")||locator.qualification.is_none()||!locator.headless{return Err("事务准备实验仅供无窗口分发验收。".into());}
            if root.join("data/state/workspace-ready.json").is_file(){return Err("请先关闭测试工作区桌面，再准备事务实验。".into());}
            let kind=value(&args,"--task-kind").unwrap_or("migrate");
            let journal=workspace::prepare_task(&root,Path::new(target),kind,0,&locator)?;
            workspace::atomic_json(&root.join("data/state/qualification-workspace-task.json"),&serde_json::json!({"journal":journal,"kind":kind,"fields":{"journal":"验收事务记录位置","kind":"迁移或切换实验"}}))?;
            return Ok(());
        }
        if let Some(url)=value(&args,"--probe-url"){
            if !cfg!(feature="qualification")||locator.qualification.is_none(){return Err("受限连通性探测仅供分发验收。".into());}
            let spec=voice_desktop_common::download::DownloadFile{url:url.into(),size:0,sha256:String::new(),sha512:String::new(),path:String::new()};
            let bytes=voice_desktop_common::download::download(&spec,&root.join("tmp/probe.bin"),Arc::new(AtomicBool::new(false)),&mut|_,_|{},65536)?;
            workspace::atomic_json(&root.join("data/state/last-probe.json"),&serde_json::json!({"ok":true,"bytes":bytes,"limit_bytes":65536,"fields":{"bytes":"实际读取正文","limit_bytes":"单次读取上限"}}))?;
            println!("{}",serde_json::json!({"ok":true,"bytes":bytes}));return Ok(());
        }
        if let Some(fixture)=value(&args,"--download-fixture"){
            if !cfg!(feature="qualification")||locator.qualification.is_none(){return Err("模拟下载仅供分发验收。".into());}
            let fixture=voice_desktop_common::canonical_path(Path::new(fixture))?;
            locator.validate_target(fixture.parent().ok_or("模拟来源缺少目录")?)?;
            let spec:voice_desktop_common::download::DownloadFile=serde_json::from_slice(&fs::read(&fixture).map_err(|e|e.to_string())?).map_err(|e|e.to_string())?;
            if !spec.url.starts_with("http://127.0.0.1:")||spec.size>8*1024*1024||spec.size==0{return Err("模拟下载须为测试回环地址和最多 8 MiB（兆二进制字节）文件。".into());}
            let target=root.join("tmp/download-fixture.bin");let cancel=Arc::new(AtomicBool::new(false));
            let limit=value(&args,"--cancel-after-bytes").and_then(|v|v.parse::<u64>().ok());let progress_cancel=cancel.clone();
            let result=voice_desktop_common::download::download(&spec,&target,cancel,&mut|current,_|{if limit.is_some_and(|limit|current>=limit){progress_cancel.store(true,std::sync::atomic::Ordering::Release);}},0);
            workspace::atomic_json(&root.join("data/state/download-fixture-result.json"),&serde_json::json!({"ok":result.is_ok(),"error":result.as_ref().err(),"exists":target.is_file(),"sha256":if target.is_file(){workspace::file_hash(&target).ok()}else{None}}))?;
            return result.map(|_|());
        }
        if locator.headless {
            workspace::atomic_json(&root.join("data/state/initialization-result.json"),&serde_json::json!({"ok":true,"channel":"distribution","workspace_root":root,"workspace_id":context.workspace_id}))?;
            println!("{}",serde_json::json!({"ok":true,"workspace_root":root}));return Ok(());
        }
    }else if args.iter().any(|arg|arg=="--ui-preview"){
        return Err("桌面样例请通过开发桌面主程序启动。".into());
    }
    let context = LaunchContext::from_args()?;
    let sibling=std::env::current_exe().map_err(|e|e.to_string())?.parent().ok_or("启动程序缺少目录")?.join("VoiceTerminalDesktop.exe");
    let desktop = if context.channel==LaunchChannel::Development&&sibling.is_file(){sibling}else{context.program.join("VoiceTerminalDesktop.exe")};
    if !desktop.is_file() {
        return Err("程序包缺少 VoiceTerminalDesktop.exe（桌面主程序）。".into());
    }
    let mut command=Command::new(desktop);context.configure_command(&mut command);
    command.current_dir(&context.program).args(args).spawn().map_err(|e|format!("无法建立桌面窗口：{e}"))?;
    Ok(()) // 根入口不保留第二套托盘／服务管理进程。
}
fn main() {
    loop{
        match start(){Ok(())=>return,Err(message)=>{
            eprintln!("{message}");
            let qualification=std::env::args().any(|arg|arg=="--qualification-config");
            if !qualification&&!message.contains("已取消")&&native_ui::retry(&message){continue;}
            std::process::exit(1);
        }}
    }
}
