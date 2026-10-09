#![cfg_attr(not(debug_assertions),windows_subsystem="windows")]
#[allow(dead_code)] // 安装器复用窗口图标，不使用共享模块的托盘函数。
#[path="../../src-tauri/src/app_icon.rs"] mod app_icon;
use std::{fs,path::PathBuf,sync::{Arc,Mutex,atomic::{AtomicBool,Ordering}}};
use serde_json::{Value,json};
use tauri::{Manager,WebviewWindow,WebviewWindowBuilder,WebviewUrl};
use voice_desktop_common::{installation,native_ui,workspace,shortcuts};
const PACKAGE:&[u8]=include_bytes!(concat!(env!("OUT_DIR"),"/program.zip"));
struct Installer{
    target:Mutex<Option<PathBuf>>, // target（所选工作区根目录）。
    status:Mutex<Value>, // status（共享启动页阶段、进度和错误）。
    busy:AtomicBool, // busy（禁止重复安装任务）。
    cancel:Arc<AtomicBool>, // cancel（展开和校验的取消请求）。
    installed:AtomicBool, // installed（本次目标已启用，不因交接失败再次覆盖）。
    exiting:AtomicBool, // exiting（取消后退出请求）。
    shortcut:bool, // shortcut（隔离验收可关闭桌面快捷方式）。
    report:Option<PathBuf>, // report（隔离验收结果位置）。
}
fn trusted(window:&WebviewWindow)->Result<(),String>{let url=window.url().map_err(|e|e.to_string())?;if window.label()=="startup"&&["/","/index.html"].contains(&url.path())&&((url.scheme()=="tauri"&&url.host_str()==Some("localhost"))||(url.scheme()=="http"&&url.host_str()==Some("tauri.localhost")&&url.port().is_none())){Ok(())}else{Err("此页面没有安装权限。".into())}}
#[tauri::command]fn desktop_status(window:WebviewWindow,state:tauri::State<Arc<Installer>>)->Result<Value,String>{trusted(&window)?;let status=state.status.lock().unwrap().clone();Ok(json!({"role":"installer","frontend_mode":"installer","development":false,"ready":false,"workspace_ready":false,"workspace_busy":state.busy.load(Ordering::Acquire),"initialization_failed":status["error"].is_string(),"error":status["error"],"initialization":status,"fields":{"role":"入口职责：installer（安装器）","development":"安装器不启用源码调试","initialization":"本体安装阶段"}}))}
fn run_install(app:&tauri::AppHandle,state:Arc<Installer>,reselect:bool)->Result<Value,String>{
    if state.busy.swap(true,Ordering::SeqCst){return Err("安装任务正在执行。".into());}
    state.cancel.store(false,Ordering::Release);
    let result=(||{
        let current=if reselect{None}else{state.target.lock().unwrap().clone()};
        let root=match current{Some(root)=>root,None=>native_ui::choose_folder("选择语音终端工作区（空文件夹或已有工作区，支持中文路径）")?.ok_or("已取消目录选择")?};
        if state.target.lock().unwrap().as_ref()!=Some(&root){state.installed.store(false,Ordering::Release);}
        *state.target.lock().unwrap()=Some(root.clone());
        let mut progress=|phase:&str,done:u64,total:u64|{*state.status.lock().unwrap()=json!({"phase":phase,"completed_bytes":done,"total_bytes":total,"can_cancel":phase.starts_with("正在展开")||phase.starts_with("正在校验"),"error":null,"fields":{"phase":"安装阶段","completed_bytes":"已处理字节","total_bytes":"总字节数","can_cancel":"启用前允许取消","error":"失败原因"}});};
        if !state.installed.load(Ordering::Acquire){progress("正在检查工作区",0,0);installation::install(PACKAGE,&root,state.cancel.clone(),&mut progress)?;state.installed.store(true,Ordering::Release);}
        progress("创建桌面快捷方式",0,0);
        if state.shortcut{shortcuts::create(&root,None)?;}
        progress("正在启动正式程序",0,0);let pid=installation::launch(&root)?;
        let record=json!({"ok":true,"target_root":root,"product_pid":pid,"installer_pid":std::process::id(),"fields":{"ok":"首次窗口交接成功","target_root":"目标工作区","product_pid":"独立正式宿主进程编号","installer_pid":"交接后退出的安装器进程编号"}});
        if let Some(path)=&state.report{workspace::atomic_json(path,&record)?;}
        state.exiting.store(true,Ordering::Release);app.exit(0);Ok(record)
    })();
    state.busy.store(false,Ordering::Release);
    if let Err(error)=&result{state.status.lock().unwrap()["error"]=json!(error);if let Some(path)=&state.report{let _=workspace::atomic_json(path,&json!({"ok":false,"error":error,"fields":{"ok":"安装未完成","error":"失败原因"}}));}}
    if state.exiting.load(Ordering::Acquire){app.exit(0);}result
}
#[tauri::command]async fn desktop_initialize(window:WebviewWindow,app:tauri::AppHandle,state:tauri::State<'_,Arc<Installer>>,reselect:bool)->Result<Value,String>{trusted(&window)?;let state=state.inner().clone();tauri::async_runtime::spawn_blocking(move||run_install(&app,state,reselect)).await.map_err(|e|e.to_string())?}
#[tauri::command]fn desktop_cancel_init(window:WebviewWindow,state:tauri::State<Arc<Installer>>)->Result<(),String>{trusted(&window)?;if state.status.lock().unwrap()["can_cancel"]!=true{return Err("当前阶段需要完成启用及交接，请稍候。".into());}state.cancel.store(true,Ordering::Release);Ok(())}
fn exit(app:&tauri::AppHandle,state:&Installer){if state.busy.load(Ordering::Acquire){if state.status.lock().unwrap()["can_cancel"]==true{state.exiting.store(true,Ordering::Release);state.cancel.store(true,Ordering::Release);}}else{state.exiting.store(true,Ordering::Release);app.exit(0);}}
#[tauri::command]fn exit_application(window:WebviewWindow,app:tauri::AppHandle,state:tauri::State<Arc<Installer>>)->Result<(),String>{trusted(&window)?;if state.busy.load(Ordering::Acquire)&&state.status.lock().unwrap()["can_cancel"]!=true{return Err("启用及启动交接阶段暂不能退出。".into());}exit(&app,&state);Ok(())}
fn run()->Result<(),String>{
    if PACKAGE.is_empty(){return Err("安装器未内嵌正式程序包，请重新构建。".into());}
    let args=std::env::args().skip(1).collect::<Vec<_>>();let mut target=None;let mut report=None;let mut shortcut=true;let mut auto=false;
    if !args.is_empty(){
        if !cfg!(feature="qualification")||args.len()!=2||args[0]!="--qualification-config"{return Err("安装器不接受路径或产品运行参数。".into());}
        let config=voice_desktop_common::resources::load(std::path::Path::new(&args[1]))?;
        let root=PathBuf::from(config["target_root"].as_str().ok_or("验收配置缺少工作区")?);
        let config_path=voice_desktop_common::canonical_path(std::path::Path::new(&args[1]))?;
        let sandbox=config_path.parent().ok_or("验收目录无效")?;
        workspace::Locator::local(&root).validate_target(&root)?;
        if !root.starts_with(sandbox)||root==sandbox{return Err("自动验收目标须位于配置目录的独立子目录内。".into());}
        target=Some(root);report=Some(sandbox.join("installer-result.json"));shortcut=config["shortcut"]!=false;auto=true;
    }
    std::env::remove_var("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER");
    if voice_desktop_common::webview::version().is_none(){voice_desktop_common::webview::guide("未检测到系统 WebView2（网页运行引擎）。安装后重新运行安装器。");return Ok(());}
    let cache=std::env::temp_dir().join(format!("voice-initialization-installer-{}",std::process::id()));fs::create_dir_all(&cache).map_err(|e|e.to_string())?;
    let state=Arc::new(Installer{target:Mutex::new(target),status:Mutex::new(json!({"phase":"请选择文件夹作为整个工作区","error":null,"can_cancel":false})),busy:AtomicBool::new(false),cancel:Arc::new(AtomicBool::new(false)),installed:AtomicBool::new(false),exiting:AtomicBool::new(false),shortcut,report});
    let setup_state=state.clone();let cache_window=cache.clone();
    let app=tauri::Builder::default().manage(state.clone()).invoke_handler(tauri::generate_handler![desktop_status,desktop_initialize,desktop_cancel_init,exit_application])
        .setup(move|app|{let window=WebviewWindowBuilder::new(app,"startup",WebviewUrl::App("index.html".into())).title("Voice Terminal · 安装语音终端").inner_size(1000.,640.).min_inner_size(400.,480.).incognito(true).data_directory(cache_window.clone()).on_navigation(|url| (url.scheme()=="tauri"&&url.host_str()==Some("localhost"))||(url.scheme()=="http"&&url.host_str()==Some("tauri.localhost")&&url.port().is_none())).build()?;app_icon::apply(window.hwnd()?.0).map_err(std::io::Error::other)?;
            if auto{let handle=app.handle().clone();let state=setup_state.clone();std::thread::spawn(move||{let _=run_install(&handle,state,false);});}Ok(())})
        .on_window_event(|window,event|{if let tauri::WindowEvent::CloseRequested{api,..}=event{let state=window.state::<Arc<Installer>>();api.prevent_close();exit(window.app_handle(),&state);}})
        .build(tauri::generate_context!()).map_err(|e|e.to_string())?;
    app.run(move|app,event|{if let tauri::RunEvent::ExitRequested{api,..}=event{if !state.exiting.load(Ordering::Acquire){api.prevent_exit();exit(app,&state);}}});
    for _ in 0..20{if workspace::remove_owned(&cache,cache.parent().unwrap()).is_ok(){break;}std::thread::sleep(std::time::Duration::from_millis(100));}Ok(())
}
fn main(){if let Err(error)=run(){voice_desktop_common::windows::error(&error);std::process::exit(1);}}
