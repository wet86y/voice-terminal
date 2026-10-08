//! Qualification（隔离验收）：仅验收构建包含，文件操作限定于用户指定的测试目录。
use std::{path::PathBuf,sync::{Arc,atomic::{AtomicBool,Ordering}}};
use serde_json::{Value,json};
use tauri::{Manager,WebviewWindow,webview::PageLoadEvent};
use voice_desktop_common::{LaunchContext,workspace::{self,Locator},resources,runtime};
use super::Desktop;

fn configuration(locator:&Locator)->Result<Value,String>{
    resources::load(locator.qualification.as_ref().ok_or("需要独立验收配置")?)
}
fn checked_path(locator:&Locator,text:&str)->Result<PathBuf,String>{
    let path=PathBuf::from(text);locator.validate_target(path.parent().ok_or("验收路径缺少父目录")?)?;
    if !path.is_absolute(){return Err("验收文件需要绝对路径".into());}Ok(path)
}
pub fn run_if_requested()->Option<Result<(),String>>{
    let args:Vec<String>=std::env::args().skip(1).collect();
    let index=args.iter().position(|value|value=="--native-check")?;
    let result=(||{
        let locator=Locator::from_args(&args)?;
        let input=checked_path(&locator,args.get(index+1).ok_or("缺少验收输入文件")?)?;
        let request=resources::load(&input)?;
        let root=locator.resolve().ok().or(locator.initial_root.clone()).ok_or("缺少测试工作区")?;
        locator.validate_target(&root)?;
        let cancel=Arc::new(AtomicBool::new(false));
        let mut phases=vec![];
        let output=(||{
            if request["operation"]=="initialize" {
                workspace::initialize(&root)?;locator.link(&root)?;
                locator.prepare(&root,cancel.clone(),&mut|phase,_,_|{if phases.last().is_none_or(|value|value!=phase){phases.push(phase.to_string());}})?;
                let context=LaunchContext::portable(&root)?;
                return Ok(json!({"workspace":root,"runtime":runtime::status(&context)?,"resources":resources::inventory(&context)?,"phases":phases}));
            }
            let context=LaunchContext::portable(&root)?;resources::recover(&context)?;
            let mut progress=|phase:&str,_:u64,_:u64|{if phases.last().is_none_or(|value|value!=phase){phases.push(phase.to_string());}};
            match request["operation"].as_str().unwrap_or("") {
                "snapshot"=>Ok(json!({"runtime":runtime::status(&context)?,"resources":resources::inventory(&context)?,"channel":context.channel.label(),"data":context.data})),
                "configure"=>{
                    std::env::set_var("VOICE_QUALIFICATION_NO_SYSTEM_REUSE","1");
                    let installed=runtime::configure(&context,request["group"].as_str().unwrap_or("voice-core"),cancel,&mut progress)?;
                    Ok(json!({"installed":installed,"runtime":runtime::status(&context)?,"phases":phases}))
                },
                "import-environment"=>{
                    let source=checked_path(&locator,request["source"].as_str().ok_or("缺少导入来源")?)?;
                    Ok(json!({"installed":runtime::import(&context,&source,cancel,&mut progress)?,"phases":phases}))
                },
                "import-resources"=>{
                    let source=checked_path(&locator,request["source"].as_str().ok_or("缺少导入来源")?)?;
                    Ok(json!({"installed":resources::import(&context,&source,request["resource_id"].as_str(),cancel,&mut progress)?,"phases":phases}))
                },
                "worker"=>{
                    let python=checked_path(&locator,request["python"].as_str().ok_or("缺少验收解释器")?)?;
                    let args=request["args"].as_array().into_iter().flatten().filter_map(Value::as_str).map(str::to_string).collect::<Vec<_>>();
                    // 解释器工作进程继承当前验收工作区，不直接执行任意用户命令。
                    Ok(runtime::worker(&context,&python,request["worker_operation"].as_str().ok_or("缺少工作进程操作")?,&args,&cancel)?)
                },
                "download-file"=>{
                    let spec:voice_desktop_common::download::DownloadFile=serde_json::from_value(request["file"].clone()).map_err(|e|e.to_string())?;
                    if !spec.url.starts_with("http://127.0.0.1:")||spec.size>8*1024*1024{return Err("下载验收仅接受最多 8 MiB（兆二进制字节）的本地回环文件".into());}
                    let target=root.join("tmp/qualification-download.bin");let signal=cancel.clone();let limit=request["cancel_after_bytes"].as_u64();
                    voice_desktop_common::download::download(&spec,&target,cancel,&mut|done,_|{if limit.is_some_and(|limit|done>=limit){signal.store(true,Ordering::Release);}},0)?;
                    Ok(json!({"sha256":workspace::file_hash(&target)?,"size":target.metadata().map_err(|e|e.to_string())?.len()}))
                },
                _=>Err("不支持的定向验收操作".into())
            }
        })();
        let value=match &output {Ok(value)=>json!({"ok":true,"result":value}),Err(error)=>json!({"ok":false,"error":error,"phases":phases})};
        workspace::atomic_json(&input.with_extension("result.json"),&value)?;
        output.map(|_|())
    })();
    Some(result)
}
pub fn simulate_missing_webview(locator:&Locator)->Result<bool,String>{
    let Some(path)=&locator.qualification else{return Ok(false);};
    if configuration(locator)?["simulate_webview_missing"]!=true{return Ok(false);}
    workspace::atomic_json(&path.with_extension("webview-result.json"),&json!({"required":"WebView2 Evergreen Runtime（自动更新网页引擎）","online":voice_desktop_common::webview::ONLINE,"offline":voice_desktop_common::webview::OFFLINE,"action":"引导后退出","system_modified":false}))?;
    Ok(true)
}
pub fn page_loaded(window:&WebviewWindow,state:&Arc<Desktop>,event:PageLoadEvent,url:&tauri::Url){
    if event!=PageLoadEvent::Finished||url.path()!="/management.html"{return;}
    let Some(locator)=&state.locator else{return;};
    if configuration(locator).ok().is_none_or(|value|value["desktop_check"]!=true){return;}
    let Some(path)=locator.qualification.clone() else{return;};
    let Some(context)=state.context() else{return;};
    let state=state.clone();let app=window.app_handle().clone();
    std::thread::spawn(move||{
        std::thread::sleep(std::time::Duration::from_secs(2));
        let _=workspace::atomic_json(&path.with_extension("desktop-result.json"),&json!({"ok":true,"page":"management.html","channel":context.channel.label(),"workspace":context.install,
            "runtime":runtime::status(&context).ok(),"resources":resources::inventory(&context).ok(),"service_started":state.service.lock().unwrap().is_some(),"error":*state.error.lock().unwrap(),
            "fields":{"page":"已加载原生管理页面","service_started":"是否启动业务后端","error":"桌面错误"}}));
        state.shutdown(&app);
    });
}
