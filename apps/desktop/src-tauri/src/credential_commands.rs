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
