//! Initialization（桌面初始化）：统一启动画面，管理能力不要求 Python。
use std::{path::PathBuf,sync::{Arc,atomic::Ordering}};
use tauri::{Manager,Emitter,WebviewWindow};
use serde_json::{Value,json};
use voice_desktop_common::{LaunchContext,LaunchChannel,workspace};
use super::{Desktop,ResourceManager};

pub fn install_manager(app:&tauri::AppHandle,state:Arc<Desktop>,context:&LaunchContext)->Result<(),String>{
    let control_state=state.clone();let control_app=app.clone();
    let control:Arc<dyn Fn(bool)->Result<(),String>+Send+Sync>=Arc::new(move|start|{
        if start {super::start_service(control_app.clone(),control_state.clone())}else{control_state.stop_service()}
    });
    voice_desktop_common::resources::recover(context)?;
    app.manage(Arc::new(ResourceManager::new(context.clone(),app.clone(),control)?));
    // 页面真正加载后才登记就绪，迁移不能把窗口创建之前的阶段视为启动成功。
    Ok(())
}
pub fn auto_start(app:tauri::AppHandle,state:Arc<Desktop>)->Result<(),String>{
    if state.mode==super::FrontendMode::UiPreview{return super::start_service(app,state);}
    if let Some(context)=state.context(){
        state.workspace_busy.store(true,Ordering::Release);
        if let Some(locator)=&state.locator {locator.prepare(&context.install,state.initialization_cancel.clone(),&mut|phase,done,total|{
            *state.initialization.lock().unwrap()=json!({"phase":phase,"completed_bytes":done,"total_bytes":total});
        }).map_err(|error|{state.workspace_busy.store(false,Ordering::Release);state.initialization.lock().unwrap()["failed"]=json!(true);error})?;}
        state.workspace_busy.store(false,Ordering::Release);
        if voice_desktop_common::runtime::status(&context)?["service_ready"]==true {super::start_service(app,state)?;}
        else {manage(&app,&state)?;}
    }else if let Some(root)=state.locator.as_ref().and_then(|v|v.resolve().ok().or(v.initial_root.clone())){
        initialize(&app,state,root)?;
    }
    Ok(())
}
// helper_if_requested（受管辅助入口）：工作区迁移与验收均复用单 EXE（程序入口）。
pub fn helper_if_requested()->Option<Result<(),String>> {
    let args:Vec<String>=std::env::args().skip(1).collect();
    let value=|name:&str|args.iter().position(|v|v==name).and_then(|i|args.get(i+1));
    if let Some(path)=value("--workspace-task") {
        return Some((||{
            if LaunchChannel::current()!=LaunchChannel::Distribution{return Err("开发渠道不接受迁移辅助参数".into());}
            let locator=workspace::Locator::from_args(&args)?;let journal=PathBuf::from(path);
            voice_desktop_common::native_ui::run("迁移语音终端工作区",move|progress|workspace::run_task(&journal,&locator,progress.cancel.clone(),&mut|phase,done,total|progress.update(phase,done,total)))
        })());
    }
    if LaunchChannel::current()==LaunchChannel::Distribution {
        if let Ok(locator)=workspace::Locator::from_args(&args){
            if let Ok(root)=locator.resolve(){match workspace::pending_task(&root){
                Ok(Some(task))=>return Some(voice_desktop_common::native_ui::run("恢复工作区事务",move|progress|workspace::run_task(&task,&locator,progress.cancel.clone(),&mut|phase,done,total|progress.update(phase,done,total)))),
                Err(error)=>return Some(Err(error)),_=>{}
            }}
        }
    }
    None
}
fn manage(app:&tauri::AppHandle,state:&Arc<Desktop>)->Result<(),String>{
    if state.context().is_none(){return Err("请先选择工作区并完成本体展开。".into());}
    if let Some(startup)=app.get_webview_window("startup"){
        let target_app=app.clone();let target_state=state.clone();
        app.run_on_main_thread(move||{
            match super::create_window(&target_app,target_state.clone(),true){
                Ok(_)=>{let _=startup.destroy();let cache=target_state.data.clone();std::thread::spawn(move||super::cleanup_initialization_cache(&cache));},
                Err(error)=>{*target_state.error.lock().unwrap()=Some(error);}
            }
        }).map_err(|e|e.to_string())?;
        return Ok(());
    }
    let window=app.get_webview_window("main").ok_or("主窗口不可用")?;
    let mut url=window.url().map_err(|e|e.to_string())?;
    url.set_path("/management.html");url.set_query(None);url.set_fragment(None);
    if url.host_str()!=Some("localhost") && url.host_str()!=Some("tauri.localhost") {
        url=tauri::Url::parse("http://tauri.localhost/management.html").map_err(|e|e.to_string())?;
    }
    window.navigate(url).map_err(|e|e.to_string())
}
fn initialize(app:&tauri::AppHandle,state:Arc<Desktop>,root:PathBuf)->Result<Value,String>{
    if state.workspace_busy.swap(true,Ordering::SeqCst){return Err("初始化正在执行。".into());}
    let result:Result<Value,String>=(||{
        let locator=state.locator.as_ref().ok_or("开发版本不需要选择工作区")?;
        locator.validate_target(&root)?;workspace::initialize(&root)?;locator.link(&root)?;
        state.initialization_cancel.store(false,Ordering::Release);
        let mut progress=|phase:&str,done:u64,total:u64|{
            let value=json!({"phase":phase,"completed_bytes":done,"total_bytes":total,"can_cancel":!phase.starts_with("启用"),"fields":{"phase":"初始化阶段","completed_bytes":"已展开字节","total_bytes":"总字节数","can_cancel":"是否可取消"}});
            *state.initialization.lock().unwrap()=value.clone();let _=app.emit("desktop://initialization-progress",value);
        };
        locator.prepare(&root,state.initialization_cancel.clone(),&mut progress)?;
        let mut context=LaunchContext::portable(&root)?;context.channel=LaunchChannel::Distribution;
        *state.context.lock().unwrap()=Some(context.clone());
        install_manager(app,state.clone(),&context)?;
        *state.error.lock().unwrap()=None;
        manage(app,&state)?;
        Ok(json!({"workspace_root":root}))
    })();
    state.workspace_busy.store(false,Ordering::Release);
    if let Err(error)=&result{*state.error.lock().unwrap()=Some(error.clone());state.initialization.lock().unwrap()["failed"]=json!(true);}result
}
#[tauri::command]
pub async fn desktop_initialize(window:WebviewWindow,app:tauri::AppHandle,state:tauri::State<'_,Arc<Desktop>>,reselect:bool)->Result<Value,String>{
    super::trusted(&window,&state)?;
    if LaunchChannel::current()!=LaunchChannel::Distribution{return Err("开发渠道固定使用项目工作区。".into());}
    let state=state.inner().clone();
    tauri::async_runtime::spawn_blocking(move||{
        let locator=state.locator.as_ref().ok_or("定位记录不可用")?;
        let root=if reselect {None}else{locator.resolve().ok().or(locator.initial_root.clone())};
        let root=match root {Some(root)=>root,None=>voice_desktop_common::native_ui::choose_folder("选择语音终端工作区（空文件夹或已有工作区，支持中文路径）")?.ok_or("已取消目录选择")?};
        initialize(&app,state,root)
    }).await.map_err(|e|e.to_string())?
}
#[tauri::command]
pub fn desktop_manage(window:WebviewWindow,app:tauri::AppHandle,state:tauri::State<Arc<Desktop>>)->Result<(),String>{super::trusted(&window,&state)?;manage(&app,state.inner())}
#[tauri::command]
pub fn desktop_cancel_init(window:WebviewWindow,state:tauri::State<Arc<Desktop>>)->Result<(),String>{super::trusted(&window,&state)?;state.initialization_cancel.store(true,Ordering::Release);Ok(())}
#[tauri::command]
pub async fn desktop_start_service(window:WebviewWindow,app:tauri::AppHandle,state:tauri::State<'_,Arc<Desktop>>)->Result<(),String>{
    super::trusted(&window,&state)?;let state=state.inner().clone();
    tauri::async_runtime::spawn_blocking(move||super::start_service(app,state)).await.map_err(|e|e.to_string())?
}
