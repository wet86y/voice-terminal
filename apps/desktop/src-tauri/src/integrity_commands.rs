//! 完整性维护：指定本地构建，与查询最新发行及更新分别操作，共用下载与安全交接。
use super::*;
use voice_desktop_common::app_update;

#[derive(Clone,Copy,PartialEq,Eq)]
pub(super) enum MaintenanceAction { Update, DownloadIntegrity, CheckIntegrity } // MaintenanceAction（更新、下载校验器、执行完整性维护）。
pub(super) fn maintenance_payload(action:MaintenanceAction,state_name:&str,manifest:Option<&app_update::AppUpdateManifest>,done:u64,total:u64,phase:Option<&str>,error:Option<&str>,can_cancel:bool)->serde_json::Value{
    let mut value=app_update_payload(state_name,manifest,done,total,phase,error,can_cancel);
    value["operation"]=serde_json::json!(if action==MaintenanceAction::Update{"update"}else{"integrity"});
    value["installer_ready"]=serde_json::json!((action==MaintenanceAction::CheckIntegrity&&!matches!(state_name,"failed"|"cancelled"))||state_name=="current");
    value["fields"]["operation"]=serde_json::json!("更新或当前版本完整性维护");
    value["fields"]["installer_ready"]=serde_json::json!("当前版本校验程序是否已准备");value
}
pub(super) fn local_build(context:&LaunchContext)->Result<String,String>{
    voice_desktop_common::app_update::installed_build(&context.install,env!("CARGO_PKG_VERSION"))
}
#[tauri::command]
pub fn app_integrity_status(window:WebviewWindow,state:tauri::State<'_,Arc<Desktop>>)->Result<serde_json::Value,String>{
    require_workspace(&window,&state)?;
    let context=state.context().ok_or("缺少工作区")?;
    let ready=app_update::retained(&context.install,env!("CARGO_PKG_VERSION"),&local_build(&context)?).is_ok();
    let mut payload=maintenance_payload(if ready{MaintenanceAction::CheckIntegrity}else{MaintenanceAction::DownloadIntegrity},if ready{"current"}else{"idle"},None,0,0,None,None,false);
    payload["phase"]=serde_json::json!(if ready{"将关闭程序，完整校验并修复受管组件，完成后重新启动。"}else{"尚未准备与本地版本一致的校验程序；下载固定版本，不会自动获取最新版。"});Ok(payload)
}
#[tauri::command]
pub fn app_integrity_download(window:WebviewWindow,state:tauri::State<'_,Arc<Desktop>>,app:tauri::AppHandle)->Result<serde_json::Value,String>{
    require_workspace(&window,&state)?;
    reserve_maintenance(&state.maintenance_gate,&state.workspace_busy,&state.app_update.busy,true)?;
    state.app_update.cancel.store(false,Ordering::Release);
    let action=MaintenanceAction::DownloadIntegrity;
    let payload=maintenance_payload(action,"checking",None,0,0,Some("正在查询本地版本的校验程序"),None,true);
    emit_app_update(&state,&app,payload.clone());let desktop=state.inner().clone();
    std::thread::spawn(move||{
        match app_update::check_version(env!("CARGO_PKG_VERSION")){
            Ok(manifest)=>download_and_handoff_update(desktop,app,manifest,action),
            Err(error)=>{emit_app_update(&desktop,&app,maintenance_payload(action,"failed",None,0,0,None,Some(&format!("无法取得本地版本 {} 的校验程序：{error} 不会下载其他版本。",env!("CARGO_PKG_VERSION"))),false));desktop.app_update.busy.store(false,Ordering::Release);}
        }
    });Ok(payload)
}
#[tauri::command]
pub fn app_integrity_check(window:WebviewWindow,state:tauri::State<'_,Arc<Desktop>>,app:tauri::AppHandle)->Result<serde_json::Value,String>{
    require_workspace(&window,&state)?;
    let context=state.context().ok_or("缺少工作区")?;
    let retained=app_update::retained(&context.install,env!("CARGO_PKG_VERSION"),&local_build(&context)?)?;
    reserve_maintenance(&state.maintenance_gate,&state.workspace_busy,&state.app_update.busy,true)?;
    state.app_update.cancel.store(false,Ordering::Release);
    let action=MaintenanceAction::CheckIntegrity;
    let payload=maintenance_payload(action,"checking",Some(&retained.manifest),0,retained.manifest.size,Some("正在验证本地校验程序身份"),None,false);
    emit_app_update(&state,&app,payload.clone());let desktop=state.inner().clone();
    std::thread::spawn(move||download_and_handoff_update(desktop,app,retained.manifest,action));Ok(payload)
}
