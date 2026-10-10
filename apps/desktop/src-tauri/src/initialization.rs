//! Initialization（桌面初始化）：统一启动画面，管理能力不要求 Python。
use std::{sync::{Arc,atomic::Ordering},thread,time::{Duration,Instant}};
use tauri::{Manager,WebviewWindow};
use serde_json::{Value,json};
use voice_desktop_common::LaunchContext;
use super::{Desktop,ResourceManager};

pub fn install_manager(app:&tauri::AppHandle,state:Arc<Desktop>,context:&LaunchContext)->Result<(),String>{
    let control_state=state.clone();let control_app=app.clone();
    let control:super::resource_commands::ServiceControl=Arc::new(move|start|{
        if start {
            super::start_service(control_app.clone(),control_state.clone())?;
            // ready（入口握手就绪）只证明后端入口进程建立了受信来源，不代表模型已推理或云端已连通。
            let deadline=Instant::now()+Duration::from_secs(35);
            loop {
                if let Some(error)=control_state.error.lock().map_err(|e|e.to_string())?.clone(){
                    let _=control_state.stop_service();return Err(format!("恢复语音服务失败：{error}"));
                }
                if control_state.origin.lock().map_err(|e|e.to_string())?.is_some(){
                    let running=control_state.service.lock().map_err(|e|e.to_string())?.as_mut()
                        .is_some_and(|service|matches!(service.child.try_wait(),Ok(None)));
                    if running{return Ok(true);}
                    let _=control_state.stop_service();return Err("服务入口在握手就绪前退出。".into());
                }
                let running=control_state.service.lock().map_err(|e|e.to_string())?.as_mut()
                    .is_some_and(|service|matches!(service.child.try_wait(),Ok(None)));
                if !running{
                    let _=control_state.stop_service();return Err("服务进程未能保持运行到入口握手。".into());
                }
                if Instant::now()>=deadline{
                    let _=control_state.stop_service();return Err("等待语音服务入口握手超时；已停止未就绪进程。".into());
                }
                thread::sleep(Duration::from_millis(50));
            }
        }
        let running=control_state.service.lock().unwrap().is_some();
        if running {
            // 资源维护先进入独立管理页，再关闭后台，避免旧业务页停留在服务停止状态。
            if !control_state.stopping.load(Ordering::Acquire){manage(&control_app,&control_state)?;}
            control_state.stop_service()?;
        }
        Ok(running)
    });
    voice_desktop_common::resources::recover(context)?;
    app.manage(Arc::new(ResourceManager::new(context.clone(),app.clone(),control)?));
    // 页面真正加载后才登记就绪，迁移不能把窗口创建之前的阶段视为启动成功。
    Ok(())
}
pub fn auto_start(app:tauri::AppHandle,state:Arc<Desktop>)->Result<(),String>{
    if state.mode==super::FrontendMode::UiPreview{return super::start_service(app,state);}
    super::start_service(app,state)
}
pub(super) fn manage(app:&tauri::AppHandle,state:&Arc<Desktop>)->Result<(),String>{
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
#[tauri::command]
pub async fn desktop_initialize(window:WebviewWindow,state:tauri::State<'_,Arc<Desktop>>,reselect:bool)->Result<Value,String>{
    super::trusted(&window,&state)?;let _=reselect;Err("本体安装由分发安装器承担，请使用工作区正式入口启动。".into())
}
#[tauri::command]
pub fn desktop_manage(window:WebviewWindow,app:tauri::AppHandle,state:tauri::State<Arc<Desktop>>)->Result<(),String>{super::trusted(&window,&state)?;manage(&app,state.inner())}
#[tauri::command]
pub fn desktop_cancel_init(window:WebviewWindow,state:tauri::State<Arc<Desktop>>)->Result<(),String>{
    super::trusted(&window,&state)?;
    if state.initialization.lock().map_err(|_|"启动状态不可用。")?["can_cancel"]!=true{return Err("当前启动阶段已结束校验，请进入组件管理或正常退出。".into());}
    state.initialization_cancel.store(true,Ordering::Release);Ok(())
}
#[tauri::command]
pub async fn desktop_start_service(window:WebviewWindow,app:tauri::AppHandle,state:tauri::State<'_,Arc<Desktop>>)->Result<(),String>{
    super::trusted(&window,&state)?;let state=state.inner().clone();
    if state.service_starting.load(Ordering::Acquire){return Ok(());}
    if tauri::Manager::try_state::<Arc<ResourceManager>>(&app).is_some_and(|manager|manager.has_active_jobs()){
        return Err("组件下载、复制或启用任务尚未结束，请等待全部组件就绪后启动。".into());
    }
    state.initialization_cancel.store(false,Ordering::Release);state.startup_skipped.store(false,Ordering::Release);
    state.service_requested.store(true,Ordering::Release);*state.error.lock().unwrap()=None;
    *state.initialization.lock().unwrap()=json!({"phase":"正在准备启动并校验本地组件","can_cancel":true,"completed_bytes":0,"total_bytes":0});
    // 先导航到随包加载画面，再在线程中校验和启动；用户不必等待后台建立入口才看到响应。
    window.navigate("http://tauri.localhost/startup.html".parse().map_err(|_|"启动画面地址无效。")?).map_err(|e|e.to_string())?;
    let error_state=state.clone();
    let result=tauri::async_runtime::spawn_blocking(move||super::start_service(app,state)).await.map_err(|e|e.to_string())?;
    if let Err(error)=&result{*error_state.error.lock().unwrap()=Some(error.clone());}
    result
}
