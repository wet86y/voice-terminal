//! Credential commands（接口密钥命令）：业务服务启动前直接使用系统凭据库。
use std::sync::Arc;
use serde_json::Value;
use tauri::{State,WebviewWindow};
use voice_desktop_common::credentials::{self,CredentialUpdate};
use super::Desktop;

fn allowed(window:&WebviewWindow,desktop:&Desktop)->Result<(),String>{
    super::trusted(window,desktop)?;
    super::require_command(desktop.mode,super::DesktopCommand::FocusMain)
}
#[tauri::command]
pub async fn audio_enhancements_settings(window:WebviewWindow,desktop:State<'_,Arc<Desktop>>,request:Option<Value>)->Result<Value,String>{
    allowed(&window,&desktop)?;let desktop=desktop.inner().clone();
    tauri::async_runtime::spawn_blocking(move||{
        let _guard=desktop.credentials_guard.lock().map_err(|_|"音频增强配置锁不可用。")?;
        if request.is_some() && desktop.service.lock().map_err(|_|"服务状态锁不可用。")?.is_some(){return Err("程序已启动，请在组件详情中切换。".into());}
        if desktop.workspace_busy.load(std::sync::atomic::Ordering::Acquire){return Err("正在切换工作区，请稍后保存。".into());}
        let context=desktop.context().ok_or("请先选择工作区。")?;
        voice_desktop_common::resources::audio_enhancements(&context.data,request.as_ref())
    }).await.map_err(|_|"音频增强设置处理失败。".to_string())?
}
#[tauri::command]
pub async fn model_credentials_status(window:WebviewWindow,desktop:State<'_,Arc<Desktop>>)->Result<Value,String>{
    allowed(&window,&desktop)?;let desktop=desktop.inner().clone();
    tauri::async_runtime::spawn_blocking(move||{
        let _guard=desktop.credentials_guard.lock().map_err(|_|"接口配置锁不可用。")?;
        let context=desktop.context().ok_or("请先选择工作区。")?;
        credentials::status_for_context(&context)
    }).await.map_err(|_|"接口配置状态读取失败。".to_string())?
}
#[tauri::command]
pub async fn model_credentials_save(window:WebviewWindow,desktop:State<'_,Arc<Desktop>>,request:CredentialUpdate)->Result<Value,String>{
    allowed(&window,&desktop)?;let desktop=desktop.inner().clone();
    tauri::async_runtime::spawn_blocking(move||{
        let _guard=desktop.credentials_guard.lock().map_err(|_|"接口配置锁不可用。")?;
        // credentials_guard（接口配置互斥锁）也覆盖启动，避免原生与业务服务同时修改引用文件。
        if desktop.service.lock().map_err(|_|"服务状态锁不可用。")?.is_some(){return Err("程序已经启动，请在程序的模型管理页更新接口密钥。".into());}
        if desktop.workspace_busy.load(std::sync::atomic::Ordering::Acquire){return Err("正在切换工作区，请稍后保存接口密钥。".into());}
        let context=desktop.context().ok_or("请先选择工作区。")?;
        credentials::save(&context.data,request)?;
        credentials::status_for_context(&context)
    }).await.map_err(|_|"接口密钥保存失败。".to_string())?
}

#[tauri::command]
pub async fn tts_selection_status(window:WebviewWindow,desktop:State<'_,Arc<Desktop>>)->Result<Value,String>{
    allowed(&window,&desktop)?;let desktop=desktop.inner().clone();
    tauri::async_runtime::spawn_blocking(move||{
        let context=desktop.context().ok_or("请先选择工作区。")?;
        Ok(credentials::configured_tts_selection(&context.data))
    }).await.map_err(|_|"语音目标读取失败。".to_string())?
}

#[tauri::command]
pub async fn tts_selection_save(window:WebviewWindow,desktop:State<'_,Arc<Desktop>>,request:Value)->Result<Value,String>{
    allowed(&window,&desktop)?;let desktop=desktop.inner().clone();
    tauri::async_runtime::spawn_blocking(move||{
        let _guard=desktop.credentials_guard.lock().map_err(|_|"语音配置锁不可用。")?;
        if desktop.service.lock().map_err(|_|"服务状态锁不可用。")?.is_some(){return Err("程序已启动，请在语音与音色设置中切换。".into());}
        if desktop.workspace_busy.load(std::sync::atomic::Ordering::Acquire){return Err("正在切换工作区，请稍后保存。".into());}
        if !request.is_object()||request.as_object().unwrap().keys().any(|key|!matches!(key.as_str(),"backend"|"local_model"))
            ||!matches!(request["backend"].as_str(),Some("minimax"|"voxcpm1"))||!matches!(request["local_model"].as_str(),Some("voxcpm1"|"voxcpm2")){return Err("语音目标必须包含有效的 backend（语音后端）与 local_model（本地模型）。".into());}
        let context=desktop.context().ok_or("请先选择工作区。")?;
        if !context.python.is_file(){return Err("请先下载 CPU（中央处理器）基础运行依赖，再保存语音目标。".into());}
        voice_desktop_common::runtime::worker(&context,&context.python,"tts-selection",&["--program".into(),context.program.to_string_lossy().into_owned(),"--data".into(),context.data.to_string_lossy().into_owned(),"--selection".into(),request.to_string()],&std::sync::atomic::AtomicBool::new(false))?;
        Ok(credentials::configured_tts_selection(&context.data))
    }).await.map_err(|_|"语音目标保存失败。".to_string())?
}
