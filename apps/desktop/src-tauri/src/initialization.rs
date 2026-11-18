//! Initialization（桌面初始化）：统一启动画面，管理能力不要求 Python。
use super::{Desktop, ResourceManager};
use serde_json::{json, Value};
use std::{
    sync::{atomic::Ordering, Arc},
    thread,
    time::{Duration, Instant},
};
use tauri::{Manager, WebviewWindow};
use voice_desktop_common::LaunchContext;

pub fn install_manager(
    app: &tauri::AppHandle,
    state: Arc<Desktop>,
    context: &LaunchContext,
) -> Result<(), String> {
    let control_state = state.clone();
    let control_app = app.clone();
    let control: super::resource_commands::ServiceControl = Arc::new(move |start| {
        if start {
            let result = resume_service(&control_app, &control_state, true);
            {
                let mut recovery = control_state.recovery.lock().unwrap();
                if result.is_err() {
                    recovery.suspend();
                }
                if let Err(error) = &result {
                    recovery.finish(Some(error));
                } else {
                    recovery.restored();
                }
            }
            control_state.publish_recovery(&control_app);
            return result;
        }
        let _transition = control_state
            .service_transition
            .lock()
            .map_err(|_| "服务交接锁不可用。")?;
        let running = control_state
            .service
            .lock()
            .map_err(|_| "服务状态不可用。")?
            .as_mut()
            .map(|service| service.child.try_wait().map(|status| status.is_none()))
            .transpose()
            .map_err(|_| "无法确认语音服务是否已停止，组件保持原状。")?
            .unwrap_or(false);
        if running {
            if control_state.recovery.lock().unwrap().preserves_page() {
                control_state.recovery.lock().unwrap().suspend();
                control_state.publish_recovery(&control_app);
            } else if !control_state.stopping.load(Ordering::Acquire) {
                manage(&control_app, &control_state)?;
            }
            control_state.stop_service()?;
        }
        Ok(running)
    });
    let recovered = voice_desktop_common::resources::recover(context);
    let manager = Arc::new(ResourceManager::new(context.clone(), app.clone(), control)?);
    if let Err(error) = &recovered {
        manager.record_recovery_failure(error)?;
        *state.error.lock().map_err(|_| "启动错误状态不可用")? = Some(error.clone());
        state.startup_skipped.store(true, Ordering::Release);
    }
    manager.start_import_dispatcher();
    #[cfg(feature = "ssh-device")]
    {
        *state
            .remote_services
            .lock()
            .map_err(|_| "远程原生服务状态不可用。")? = Some(Arc::new(
            super::remote_services::RemoteServices::new(manager.clone()),
        ));
    }
    app.manage(manager);
    // 页面真正加载后才登记就绪，迁移不能把窗口创建之前的阶段视为启动成功。
    Ok(())
}

fn resume_service(
    app: &tauri::AppHandle,
    state: &Arc<Desktop>,
    activation: bool,
) -> Result<bool, String> {
    super::start_service_for(
        app.clone(),
        state.clone(),
        if activation {
            super::ServiceStartKind::ResourceActivation
        } else {
            super::ServiceStartKind::RuntimeRecovery
        },
    )?;
    let epoch = state.service_epoch.load(Ordering::Acquire);
    // startup_result（启动尝试结束）覆盖模型加载；ready（入口握手）不能提前清除旧资源备份。
    let deadline = Instant::now() + Duration::from_secs(600);
    loop {
        if state.stopping.load(Ordering::Acquire) {
            return Err("应用正在退出，停止组件恢复。".into());
        }
        let error = state.error.lock().map_err(|e| e.to_string())?.clone();
        if let Some(error) = error {
            let _ = state.stop_service();
            return Err(format!("恢复语音服务失败：{error}"));
        }
        let result = state
            .recovery
            .lock()
            .unwrap()
            .result
            .clone()
            .filter(|(generation, _)| *generation == epoch);
        if let Some((_, result)) = result {
            if result["core_available"] == true || !state.recovery.lock().unwrap().require_core {
                return Ok(true);
            }
            let _ = state.stop_service();
            return Err("组件已换入，但业务能力未能恢复；正在恢复原组件。".into());
        }
        let running = state
            .service
            .lock()
            .map_err(|e| e.to_string())?
            .as_mut()
            .is_some_and(|service| matches!(service.child.try_wait(), Ok(None)));
        if !running {
            let _ = state.stop_service();
            return Err("服务进程在组件恢复完成前退出。".into());
        }
        if Instant::now() >= deadline {
            let _ = state.stop_service();
            return Err("等待组件恢复超时；已停止未就绪进程。".into());
        }
        thread::sleep(Duration::from_millis(50));
    }
}
pub fn auto_start(app: tauri::AppHandle, state: Arc<Desktop>) -> Result<(), String> {
    if state.mode == super::FrontendMode::UiPreview {
        return super::start_service(app, state);
    }
    super::start_service(app, state.clone())?;
    #[cfg(feature = "ssh-device")]
    if let Some(context) = state.context() {
        let snapshot = voice_desktop_common::device_config::inspect(&context.data);
        if snapshot.mode == Some(voice_desktop_common::device_config::RunMode::Local)
            && snapshot.broadcast_enabled
        {
            let background_state = state.clone();
            tauri::async_runtime::spawn(async move {
                let preparation = async {
                    let status = tauri::async_runtime::spawn_blocking(voice_desktop_common::system_feature::open_ssh_server_status)
                        .await.map_err(|_| "SSH 服务状态检查任务无法启动。".to_string())??;
                    if !status.ready {
                        return Err("本机语音核心已启动，但微软 SSH 服务尚未就绪；请在设备设置中点击“检查 SSH 服务”后重试广播。".into());
                    }
                    if !status.project_access_enabled || !status.pairing_access_enabled {
                        tauri::async_runtime::spawn_blocking(|| voice_desktop_common::system_feature::set_device_broadcast_access(true))
                            .await.map_err(|_| "局域网受限防火墙准备任务无法启动。".to_string())??;
                    }
                    super::sync_local_discovery(background_state.clone()).await
                }.await;
                if let Err(error) = preparation {
                    *background_state.error.lock().unwrap() = Some(error);
                }
            });
        }
    }
    Ok(())
}

/// product_page_path（选择已保存运行模式的首屏）：远程客户端启动产品界面，本地角色启动管理页。
fn product_page_path(remote_product: bool) -> &'static str {
    if remote_product {
        "/index.html"
    } else {
        "/management.html"
    }
}

pub(super) fn manage(app: &tauri::AppHandle, state: &Arc<Desktop>) -> Result<(), String> {
    if state.context().is_none() {
        return Err("请先选择工作区并完成本体展开。".into());
    }
    if app.get_webview_window("startup").is_some() {
        let target_app = app.clone();
        let target_state = state.clone();
        app.run_on_main_thread(move || {
            match super::create_window(&target_app, target_state.clone(), true) {
                Ok(_) => {} // 旧窗口由 desktop_page_ready（新页面挂载确认）收尾。
                Err(error) => {
                    *target_state.error.lock().unwrap() = Some(error);
                }
            }
        })
        .map_err(|e| e.to_string())?;
        return Ok(());
    }
    let window = app.get_webview_window("main").ok_or("主窗口不可用")?;
    let remote_product = state.context().is_some_and(|context| {
        voice_desktop_common::device_config::inspect(&context.data).mode
            == Some(voice_desktop_common::device_config::RunMode::Remote)
    });
    let mut url = window.url().map_err(|e| e.to_string())?;
    let page_path = product_page_path(remote_product);
    url.set_path(page_path);
    url.set_query(None);
    url.set_fragment(None);
    if url.host_str() != Some("localhost") && url.host_str() != Some("tauri.localhost") {
        url = tauri::Url::parse(&format!("http://tauri.localhost{page_path}"))
            .map_err(|e| e.to_string())?;
    }
    window.navigate(url).map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::product_page_path;

    #[test]
    fn remote_mode_opens_the_product_page_while_local_mode_keeps_management_page() {
        assert_eq!(product_page_path(true), "/index.html");
        assert_eq!(product_page_path(false), "/management.html");
    }
}

#[tauri::command]
pub async fn desktop_initialize(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    reselect: bool,
) -> Result<Value, String> {
    super::trusted(&window, &state)?;
    let _ = reselect;
    Err("本体安装由分发安装器承担，请使用工作区正式入口启动。".into())
}
#[tauri::command]
pub fn desktop_manage(
    window: WebviewWindow,
    app: tauri::AppHandle,
    state: tauri::State<Arc<Desktop>>,
) -> Result<(), String> {
    super::trusted(&window, &state)?;
    manage(&app, state.inner())
}
#[tauri::command]
pub fn desktop_cancel_init(
    window: WebviewWindow,
    state: tauri::State<Arc<Desktop>>,
) -> Result<(), String> {
    super::trusted(&window, &state)?;
    if state
        .initialization
        .lock()
        .map_err(|_| "启动状态不可用。")?["can_cancel"]
        != true
    {
        return Err("当前启动阶段已结束校验，请进入组件管理或正常退出。".into());
    }
    state.initialization_cancel.store(true, Ordering::Release);
    Ok(())
}
#[tauri::command]
pub async fn desktop_start_service(
    window: WebviewWindow,
    app: tauri::AppHandle,
    state: tauri::State<'_, Arc<Desktop>>,
) -> Result<(), String> {
    super::trusted(&window, &state)?;
    let state = state.inner().clone();
    if state.service_starting.load(Ordering::Acquire) {
        return Ok(());
    }
    if tauri::Manager::try_state::<Arc<ResourceManager>>(&app)
        .is_some_and(|manager| manager.has_active_jobs())
    {
        return Err("组件下载、复制或启用任务尚未结束，请等待全部组件就绪后启动。".into());
    }
    let preserve_page = {
        let mut recovery = state.recovery.lock().unwrap();
        if recovery.preserves_page() {
            if recovery.snapshot()["operation"]["state"] == "running" {
                return Ok(());
            }
            recovery.suspend();
            true
        } else {
            false
        }
    };
    if preserve_page {
        state.publish_recovery(&app);
        let error_state = state.clone();
        let error_app = app.clone();
        let result =
            tauri::async_runtime::spawn_blocking(move || resume_service(&app, &state, false))
                .await
                .map_err(|e| e.to_string())?;
        if result.is_err() {
            error_state.recovery.lock().unwrap().suspend();
        }
        {
            let mut recovery = error_state.recovery.lock().unwrap();
            let error = result.as_ref().err().map(String::as_str).or_else(|| {
                recovery
                    .result
                    .as_ref()
                    .filter(|(_, value)| value["core_available"] != true)
                    .map(|_| "业务入口已恢复，部分语音能力仍不可用，请在组件页继续恢复。")
            });
            recovery.finish(error);
        }
        error_state.publish_recovery(&error_app);
        return result.map(|_| ());
    }
    state.initialization_cancel.store(false, Ordering::Release);
    state.startup_skipped.store(false, Ordering::Release);
    state.service_requested.store(true, Ordering::Release);
    *state.error.lock().unwrap() = None;
    *state.initialization.lock().unwrap() = json!({"phase":"正在准备启动并校验本地组件","can_cancel":true,"completed_bytes":0,"total_bytes":0});
    // 先导航到随包加载画面，再在线程中校验和启动；用户不必等待后台建立入口才看到响应。
    window
        .navigate(
            "http://tauri.localhost/startup.html"
                .parse()
                .map_err(|_| "启动画面地址无效。")?,
        )
        .map_err(|e| e.to_string())?;
    let error_state = state.clone();
    let result = tauri::async_runtime::spawn_blocking(move || super::start_service(app, state))
        .await
        .map_err(|e| e.to_string())?;
    if let Err(error) = &result {
        *error_state.error.lock().unwrap() = Some(error.clone());
    }
    result
}
