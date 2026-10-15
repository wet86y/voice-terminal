//! Workspace UI（工作区事务界面）：独立辅助进程直接加载共享启动页。
#[cfg(windows)]
#[allow(dead_code)]
#[path = "../../src-tauri/src/app_icon.rs"]
mod app_icon;

use serde_json::{json, Value};
use std::{path::PathBuf, sync::{Arc, Mutex, atomic::{AtomicBool, Ordering}}};
use tauri::{Manager, WebviewWindow, WebviewWindowBuilder, WebviewUrl};
use voice_desktop_common::workspace::{self, Locator};

struct TaskState {
    status: Mutex<Value>, // status（当前阶段、字节进度和错误）。
    cancel: Arc<AtomicBool>, // cancel（复制阶段的取消信号）。
    busy: AtomicBool, // busy（事务是否仍在执行）。
    requested: AtomicBool, // requested（页面加载后仅派发一次任务）。
    exiting: AtomicBool, // exiting（请求取消并在回滚结束后退出）。
}

fn cancellable(phase: &str) -> bool {
    !["启动目标", "目标已启动", "切换定位", "启用基础组件", "启用程序本体"]
        .iter().any(|prefix| phase.starts_with(prefix))
}

impl TaskState {
    fn new() -> Self {
        Self {
            status: Mutex::new(json!({"phase":"正在准备工作区迁移","completed_bytes":0,"total_bytes":0,"can_cancel":true,"error":null,
                "fields":{"phase":"当前事务阶段","completed_bytes":"已处理字节数","total_bytes":"当前阶段总字节数","can_cancel":"是否允许请求取消","error":"失败原因"}})),
            cancel: Arc::new(AtomicBool::new(false)), busy: AtomicBool::new(true),
            requested: AtomicBool::new(false), exiting: AtomicBool::new(false),
        }
    }

    fn update(&self, phase: &str, done: u64, total: u64) {
        let mut status = self.status.lock().unwrap();
        status["phase"] = json!(if self.cancel.load(Ordering::Acquire) { "正在取消迁移并保留原工作区" } else { phase });
        status["completed_bytes"] = json!(done);
        status["total_bytes"] = json!(total);
        status["can_cancel"] = json!(cancellable(phase) && !self.cancel.load(Ordering::Acquire));
    }

    fn request_cancel(&self) -> Result<(), String> {
        let mut status = self.status.lock().map_err(|_| "迁移状态不可用。")?;
        if status["can_cancel"] != true { return Err("当前阶段需要完成启动校验及交接，请稍候。".into()); }
        self.cancel.store(true, Ordering::Release);
        status["can_cancel"] = json!(false);
        status["phase"] = json!("正在取消迁移并保留原工作区");
        Ok(())
    }

    fn finish(&self, result: &Result<(), String>) {
        let mut status = self.status.lock().unwrap();
        status["can_cancel"] = json!(false);
        if let Err(error) = result { status["error"] = json!(error); }
        self.busy.store(false, Ordering::Release);
    }
}

fn trusted(window: &WebviewWindow) -> Result<(), String> {
    let url = window.url().map_err(|error| error.to_string())?;
    if window.label() == "startup" && ["/", "/index.html"].contains(&url.path())
        && url.query().is_none() && url.fragment().is_none()
        && ((url.scheme() == "tauri" && url.host_str() == Some("localhost"))
            || (url.scheme() == "http" && url.host_str() == Some("tauri.localhost") && url.port().is_none())) {
        Ok(())
    } else { Err("此页面没有工作区事务权限。".into()) }
}

#[tauri::command]
fn desktop_status(window: WebviewWindow, state: tauri::State<Arc<TaskState>>) -> Result<Value, String> {
    trusted(&window)?;
    let status = state.status.lock().map_err(|_| "迁移状态不可用。")?.clone();
    Ok(json!({"role":"workspace-task","workspace_busy":state.busy.load(Ordering::Acquire),"workspace_ready":false,
        "error":status["error"],"initialization":status,
        "fields":{"role":"入口职责：workspace-task（工作区切换及迁移辅助进程）","workspace_busy":"事务仍在执行","workspace_ready":"辅助页不开放组件管理","error":"失败原因","initialization":"当前阶段及字节进度"}}))
}

#[tauri::command]
fn desktop_cancel_init(window: WebviewWindow, state: tauri::State<Arc<TaskState>>) -> Result<(), String> {
    trusted(&window)?;
    state.request_cancel()
}

fn exit(app: &tauri::AppHandle, state: &TaskState) -> Result<(), String> {
    if state.busy.load(Ordering::Acquire) {
        state.request_cancel()?;
        state.exiting.store(true, Ordering::Release);
        // 任务可能已在取消请求和退出标记之间完成。
        if !state.busy.load(Ordering::Acquire) { app.exit(0); }
    } else {
        state.exiting.store(true, Ordering::Release);
        app.exit(0);
    }
    Ok(())
}

#[tauri::command]
fn exit_application(window: WebviewWindow, app: tauri::AppHandle, state: tauri::State<Arc<TaskState>>) -> Result<(), String> {
    trusted(&window)?;
    exit(&app, &state)
}

pub fn run(task: PathBuf, locator: Locator) -> Result<(), String> {
    std::env::remove_var("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER");
    if voice_desktop_common::webview::version().is_none() {
        voice_desktop_common::webview::guide("迁移界面需要系统 WebView2（网页运行引擎），安装后重新从原工作区入口恢复。");
        return Ok(());
    }
    // 暂存放在目标工作区，迁移收尾时不占用原目录；关闭窗口后清理本轮缓存。
    let temporary = task.parent().ok_or("工作区事务缺少暂存目录")?.to_path_buf();
    let cache = workspace::safe_path(&temporary, &format!("process/workspace-ui-{}", std::process::id()))?;
    std::fs::create_dir_all(&cache).map_err(|error| error.to_string())?;
    std::env::set_var("TEMP", &cache);
    std::env::set_var("TMP", &cache);
    let state = Arc::new(TaskState::new());
    let setup_state = state.clone();
    let window_cache = cache.clone();
    let app = tauri::Builder::default().manage(state.clone())
        .invoke_handler(tauri::generate_handler![desktop_status, desktop_cancel_init, exit_application])
        .setup(move |app| {
            let handle = app.handle().clone();
            let window = WebviewWindowBuilder::new(app, "startup", WebviewUrl::App("index.html".into()))
                .initialization_script(voice_desktop_common::webview::DISABLE_CONTEXT_MENU)
                .title("Voice Terminal · 程序迁移中").inner_size(1000., 640.).min_inner_size(400., 480.)
                .incognito(true).data_directory(window_cache)
                .on_navigation(|url| ["/", "/index.html"].contains(&url.path())
                    && ((url.scheme() == "tauri" && url.host_str() == Some("localhost"))
                        || (url.scheme() == "http" && url.host_str() == Some("tauri.localhost") && url.port().is_none())))
                .on_page_load(move |window, payload| {
                    if payload.event() != tauri::webview::PageLoadEvent::Finished || trusted(&window).is_err()
                        || setup_state.requested.swap(true, Ordering::SeqCst) { return; }
                    let state = setup_state.clone();
                    let app = handle.clone();
                    let task = task.clone();
                    let locator = locator.clone();
                    std::thread::spawn(move || {
                        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                            workspace::run_task(&task, &locator, state.cancel.clone(), &mut |phase, done, total| state.update(phase, done, total))
                        })).unwrap_or_else(|_| Err("工作区迁移任务异常退出，请从原入口恢复。".into()));
                        state.finish(&result);
                        if result.is_ok() || state.exiting.load(Ordering::Acquire) {
                            state.exiting.store(true, Ordering::Release);
                            app.exit(0);
                        }
                    });
                }).build()?;
            #[cfg(windows)]
            app_icon::apply(window.hwnd()?.0).map_err(std::io::Error::other)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let state = window.state::<Arc<TaskState>>();
                let _ = exit(window.app_handle(), &state);
            }
        }).build(tauri::generate_context!()).map_err(|error| error.to_string())?;
    app.run(move |app, event| {
        if let tauri::RunEvent::ExitRequested { api, .. } = event {
            if !state.exiting.load(Ordering::Acquire) { api.prevent_exit(); let _ = exit(app, &state); }
        }
    });
    for _ in 0..20 {
        if workspace::remove_owned(&cache, &temporary).is_ok() { break; }
        std::thread::sleep(std::time::Duration::from_millis(100));
    }
    Ok(())
}
