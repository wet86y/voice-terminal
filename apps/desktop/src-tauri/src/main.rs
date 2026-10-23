#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod app_icon;
mod integrity_commands;
use integrity_commands::{app_integrity_status,app_integrity_download,app_integrity_check,MaintenanceAction,maintenance_payload};
mod resource_commands;
mod initialization;
mod runtime_recovery;
#[cfg(feature="qualification")] mod qualification;
mod environment_commands;
mod credential_commands;
use credential_commands::{model_credentials_status,model_credentials_save,tts_selection_status,tts_selection_save,audio_enhancements_settings};
use initialization::{desktop_initialize,desktop_manage,desktop_start_service,desktop_cancel_init};
use environment_commands::{resource_install_plan,voice_environment_status,voice_environment_plan,voice_environment_configure,voice_environment_import,gpu_environment_register,gpu_environment_probe,environment_guide};
use std::{
    fs::{self, File},
    io::{BufRead, BufReader, Write},
    os::windows::{io::AsRawHandle, process::CommandExt},
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, Stdio},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Condvar, Mutex,
    },
    time::{Duration, Instant},
};
use tauri::{
    menu::{CheckMenuItem, Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    webview::PageLoadEvent,
    Emitter, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder,
};
use voice_desktop_common::{windows::Job, LaunchContext, workspace::{self, Locator}};
use resource_commands::{ResourceManager,resource_import_confirm,resource_inventory,resource_jobs,resource_download,resource_download_required,resource_import,resource_open_source,resource_cancel};

const APP_UPDATE_PROGRESS_EVENT:&str="desktop://app-update-progress";
struct AppUpdateManager{
    status:Mutex<serde_json::Value>, // status（界面展示的更新阶段与进度）。
    manifest:Mutex<Option<voice_desktop_common::app_update::AppUpdateManifest>>, // manifest（已校验的发行清单）。
    busy:AtomicBool, // busy（更新任务互斥标记）。
    cancel:Arc<AtomicBool>, // cancel（安装器下载取消信号）。
}
impl AppUpdateManager{
    fn new()->Self{Self{status:Mutex::new(serde_json::Value::Null),manifest:Mutex::new(None),busy:AtomicBool::new(false),cancel:Arc::new(AtomicBool::new(false))}}
    // request_cancel（取消准入）与 begin_handoff（交接门闩）共用状态锁，成功取消不会再启动安装器。
    fn request_cancel(&self)->Result<(),String>{
        let status=self.status.lock().map_err(|_|"更新状态不可用。")?;
        if !self.busy.load(Ordering::Acquire)||status["can_cancel"]!=true{return Err("当前更新阶段不允许取消。".into());}
        self.cancel.store(true,Ordering::Release);Ok(())
    }
    fn begin_handoff(&self,payload:serde_json::Value)->Result<(),String>{
        let mut status=self.status.lock().map_err(|_|"更新状态不可用。")?;
        if self.cancel.load(Ordering::Acquire){return Err("更新交接已取消。".into());}
        *status=payload;Ok(())
    }
}

struct Service {
    child: Child,
    input: ChildStdin,
    job: Job,
}

struct LogFlushState {
    completed_request: u64, // completed_request（已完成刷新请求编号）。
    failed_request: u64, // failed_request（刷新失败请求编号）。
}

struct ResetFlag<'a>(&'a AtomicBool);
impl Drop for ResetFlag<'_> {
    fn drop(&mut self) { self.0.store(false, Ordering::Release); }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum FrontendMode {
    Product,   // Product（正式产品界面）。
    UiPreview, // UI preview（桌面样例界面）。
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum DesktopCommand {
    Status,      // status（桌面状态读取）。
    SkipStartup, // skip startup（跳过加载画面，服务继续启动）。
    ExportLogs,  // export logs（打包并导出产品日志）。
    OpenConsole, // open console（打开产品控制台）。
    Exit,        // exit（退出当前桌面实例）。
    FocusMain,   // focus main（聚焦产品主窗口）。
    PublishFloatingModel, // publish floating model（发布悬浮窗只读状态）。
}

fn command_allowed(mode: FrontendMode, command: DesktopCommand) -> bool {
    match command {
        DesktopCommand::Status | DesktopCommand::SkipStartup | DesktopCommand::Exit => true,
        DesktopCommand::ExportLogs | DesktopCommand::OpenConsole | DesktopCommand::FocusMain | DesktopCommand::PublishFloatingModel => {
            mode == FrontendMode::Product
        }
    }
}

fn require_command(mode: FrontendMode, command: DesktopCommand) -> Result<(), String> {
    if command_allowed(mode, command) {
        return Ok(());
    }
    let name = match command {
        DesktopCommand::Status => "状态读取",
        DesktopCommand::SkipStartup => "跳过加载画面",
        DesktopCommand::ExportLogs => "导出产品日志",
        DesktopCommand::OpenConsole => "产品控制台",
        DesktopCommand::Exit => "退出应用",
        DesktopCommand::FocusMain => "产品主窗口",
        DesktopCommand::PublishFloatingModel => "悬浮窗状态",
    };
    Err(format!("桌面样例不支持{name}命令。"))
}

impl FrontendMode {
    fn label(self) -> &'static str {
        match self {
            Self::Product => "product",
            Self::UiPreview => "ui-preview",
        }
    }
}

/// frontend_capability（业务页面权限）：只授权当前实例的主窗口与实际页面来源。
fn frontend_capability(mode: FrontendMode, normalized_origin: &str, epoch: u64) -> serde_json::Value {
    if mode == FrontendMode::UiPreview {
        serde_json::json!({"identifier":"owned-ui-preview", "description":"桌面样例的状态读取、加载画面和退出操作", "local":false,"windows":["main"],"remote":{"urls":[format!("{normalized_origin}/*")]},"permissions":["allow-desktop-status","allow-skip-startup","allow-exit-application"]})
    } else {
        serde_json::json!({"identifier":format!("owned-main-{epoch}"), "description":"当前实例产品页面的受限桌面操作", "local":false,"windows":["main"],"remote":{"urls":[format!("{normalized_origin}/*")]},"permissions":["allow-desktop-status","allow-desktop-page-ready","allow-skip-startup","allow-export-logs","allow-open-console","allow-exit-application","allow-focus-main","allow-workspace-info","allow-workspace-choose","allow-workspace-link","allow-workspace-migrate","allow-resource-inventory","allow-resource-jobs","allow-resource-download", "allow-resource-download-required", "allow-tts-selection-status", "allow-tts-selection-save","allow-audio-enhancements-settings","allow-resource-import","allow-resource-import-confirm","allow-resource-open-source","allow-resource-cancel","allow-desktop-manage","allow-desktop-start-service","allow-desktop-cancel-init","allow-model-credentials-status","allow-model-credentials-save","allow-voice-environment-status","allow-voice-environment-plan","allow-resource-install-plan","allow-voice-environment-configure","allow-voice-environment-import","allow-gpu-environment-register","allow-gpu-environment-probe","allow-environment-guide","allow-app-integrity-status","allow-app-integrity-download","allow-app-integrity-check","allow-app-update-check","allow-app-update-download","allow-app-update-cancel","allow-project-open-repository","allow-open-minimax-voice-library","core:event:allow-listen","core:event:allow-unlisten"]})
    }
}

/// floating_capability（悬浮窗权限）：仅授权本实例悬浮窗展示、输入启停及窗口交互。
fn floating_capability(normalized_origin: &str, epoch: u64) -> serde_json::Value {
    serde_json::json!({"identifier":format!("owned-floating-{epoch}"), "description":"当前实例悬浮窗的展示、语音输入与窗口切换权限", "local":false,"windows":["floating"],"remote":{"urls":[format!("{normalized_origin}/*")]},"permissions":["allow-get-floating-model","allow-return-to-workbench","allow-start-floating-drag","allow-control-floating-session","core:event:allow-listen","core:event:allow-unlisten"]})
}

/// floating_publisher_capability（悬浮窗状态发布权限）：主窗口只能更新浮窗展示快照。
fn floating_publisher_capability(normalized_origin: &str, epoch: u64) -> serde_json::Value {
    serde_json::json!({"identifier":format!("owned-floating-publisher-{epoch}"), "description":"当前实例主工作台的悬浮窗动效发布权限", "local":false,"windows":["main"],"remote":{"urls":[format!("{normalized_origin}/*")]},"permissions":["allow-publish-floating-model"]})
}

struct Desktop {
    context: Mutex<Option<LaunchContext>>, // context（产品模式的业务启动上下文）；预览模式不读取。
    initialization:Mutex<serde_json::Value>, // initialization（启动画面状态）。
    initialization_cancel:Arc<AtomicBool>, // initialization_cancel（本体展开取消信号）。
    program: PathBuf,               // program（当前前端项目根目录）。
    data: PathBuf,                  // data（当前模式隔离的 WebView2 缓存目录）。
    webview2: Option<PathBuf>,      // webview2（可选的固定网页运行时）。
    mode: FrontendMode,             // mode（前端模式：产品或桌面样例）。
    development: bool,              // development（是否显式源码调试）。
    instance_identifier: String,    // instance_identifier（工作区单实例标识；演示单独隔离）。
    service: Mutex<Option<Service>>,
    service_transition: Mutex<()>, // service_transition（启动检查与维护停机共用的交接锁）。
    credentials_guard: Mutex<()>, // credentials_guard（接口配置互斥锁）：保存与启动共用。
    service_epoch: AtomicU64, // service_epoch（后台服务代次，防止旧读取线程控制新进程）。
    service_ready_epoch: AtomicU64, // service_ready_epoch（已完成私有入口握手的服务代次）。
    locator: Option<Locator>, // locator（当前工作区本地身份，不读写系统定位）。
    workspace_guard: Mutex<Option<voice_desktop_common::installation::WorkspaceGuard>>, // workspace_guard（程序运行期间的本体互斥锁）。
    workspace_busy: AtomicBool, // workspace_busy（防止并行切换或迁移）。
    maintenance_gate: Mutex<()>, // maintenance_gate（维护准入锁）：更新与工作区交接互斥。
    log_export_busy: AtomicBool, // log_export_busy（防止重复打包日志）。
    log_flush_sequence: AtomicU64, // log_flush_sequence（刷新请求递增编号）。
    log_flush_state: Mutex<LogFlushState>, // log_flush_state（最新刷新请求结果）。
    log_flush_changed: Condvar, // log_flush_changed（唤醒等待刷新结果的导出任务）。
    app_update:AppUpdateManager, // app_update（应用发行检查、下载及安装交接）。
    service_requested: AtomicBool, // service_requested（主窗口已请求启动服务，刷新不重复启动）。
    service_starting: AtomicBool, // service_starting（本次启动正在校验或建立入口）：拒绝重复启动。
    startup_skipped: AtomicBool,   // startup_skipped（当前窗口跳过加载画面的选择）。
    recovery: Mutex<runtime_recovery::RuntimeRecovery>, // recovery（同一窗口的组件热更新与连续会话）。
    origin: Mutex<Option<String>>,
    dsh_origin: Mutex<Option<String>>, // dsh_origin（当前受管 DSH 的实际来源，不含令牌）。
    launch_token: Mutex<Option<String>>, // launch_token（当前入口的一次性启动凭据）。
    error: Mutex<Option<String>>,
    stopping: AtomicBool,
    stopped: AtomicBool,
    tray: AtomicBool,
    floating_mode: AtomicBool, // floating_mode（是否正在以悬浮窗呈现）。
    floating_ready: AtomicBool, // floating_ready（悬浮窗页面是否已加载）。
    floating_transition: Mutex<()>, // floating_transition（模式切换互斥锁）。
    floating_revision: AtomicU64, // floating_revision（快照递增版本）。
    floating_model: Mutex<serde_json::Value>, // floating_model（最近一次语音动效快照）。
    floating_menu: Mutex<Option<CheckMenuItem<tauri::Wry>>>, // floating_menu（托盘悬浮窗勾选项）。
}

impl Desktop {
    fn context(&self) -> Option<LaunchContext> { self.context.lock().unwrap().clone() }

    fn publish_recovery(&self,app:&tauri::AppHandle){
        let value=self.recovery.lock().unwrap().snapshot();
        let _=app.emit("desktop://runtime-recovery",value);
    }

    fn flush_service_logs(&self) -> Result<(), String> {
        if self.service.lock().map_err(|error| error.to_string())?.is_none() {
            return Ok(());
        }
        let request_id = self.log_flush_sequence.fetch_add(1, Ordering::AcqRel) + 1;
        self.send(serde_json::json!({"protocol":1,"kind":"flush_logs","request_id":request_id}))?;
        let deadline = Instant::now() + Duration::from_secs(5);
        let mut result = self.log_flush_state.lock().map_err(|error| error.to_string())?;
        while result.completed_request < request_id {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err("日志刷新超时，请重试打包。".into());
            }
            let (next, timeout) = self.log_flush_changed.wait_timeout(result, remaining)
                .map_err(|error| error.to_string())?;
            result = next;
            if timeout.timed_out() && result.completed_request < request_id {
                return Err("日志刷新超时，请重试打包。".into());
            }
        }
        if result.failed_request == request_id {
            return Err("无法将最新日志刷新到磁盘，请重试打包。".into());
        }
        Ok(())
    }

    fn stop_service(&self) -> Result<(),String> {
        self.service_epoch.fetch_add(1,Ordering::SeqCst);
        let _=self.send(serde_json::json!({"protocol":1,"kind":"shutdown","budget_ms":14000}));
        let deadline=Instant::now()+Duration::from_secs(15);
        loop {
            let exited=self.service.lock().map_err(|e|e.to_string())?.as_mut().map(|v|v.child.try_wait().ok().flatten().is_some()).unwrap_or(true);
            if exited||Instant::now()>=deadline{break;}
            std::thread::sleep(Duration::from_millis(80));
        }
        if let Some(mut service)=self.service.lock().map_err(|e|e.to_string())?.take(){service.job.terminate();let _=service.child.kill();let _=service.child.wait();}
        *self.launch_token.lock().unwrap()=None;*self.dsh_origin.lock().unwrap()=None;
        if let Some(context)=self.context(){let _=fs::remove_file(context.data.join("state/workspace-ready.json"));}
        Ok(())
    }
    fn allows_frontend_url(&self, url: &tauri::Url) -> bool {
        if self.origin.lock().unwrap().as_deref()
            != Some(url.origin().ascii_serialization().as_str())
        {
            return false;
        }
        let token = self.launch_token.lock().unwrap();
        match self.mode {
            FrontendMode::Product => {
                product_entry_url_allowed(url, token.as_deref()) || product_page_url_allowed(url)
            }
            FrontendMode::UiPreview => {
                preview_entry_url_allowed(url, token.as_deref()) || preview_page_url_allowed(url)
            }
        }
    }

    fn allows_floating_url(&self, url: &tauri::Url) -> bool {
        self.mode == FrontendMode::Product
            && self.origin.lock().unwrap().as_deref()
                == Some(url.origin().ascii_serialization().as_str())
            && floating_page_url_allowed(url)
    }

    fn is_settled_frontend_url(&self, url: &tauri::Url) -> bool {
        match self.mode {
            FrontendMode::Product => product_page_url_allowed(url),
            FrontendMode::UiPreview => preview_page_url_allowed(url),
        }
    }
}

impl Desktop {
    fn send(&self, value: serde_json::Value) -> Result<(), String> {
        let mut guard = self.service.lock().map_err(|e| e.to_string())?;
        let service = guard.as_mut().ok_or("服务入口未建立")?;
        writeln!(service.input, "{value}")
            .and_then(|_| service.input.flush())
            .map_err(|e| e.to_string())
    }
    fn shutdown(self: &Arc<Self>, app: &tauri::AppHandle) {
        if self.stopping.swap(true, Ordering::SeqCst) {
            return;
        }
        self.floating_mode.store(false, Ordering::Release);
        if let Some(floating) = app.get_webview_window("floating") {
            let _ = floating.hide();
        }
        // exit motion（退出动画）：所有原生退出入口通知同一页面，后台回收同时进行。
        let motion_started = Instant::now(); // motion_started（一秒过渡的起始时刻）。
        let has_window = app.get_webview_window("main").or_else(|| app.get_webview_window("startup"))
            .map(|window| {
                let _ = window.eval("window.dispatchEvent(new Event('voice-terminal:exiting'))");
                true
            }).unwrap_or(false);
        *self.origin.lock().unwrap() = None;
        let state = self.clone();
        let app = app.clone();
        std::thread::spawn(move || {
            if let Some(resources)=app.try_state::<Arc<ResourceManager>>(){let _=resources.pause_and_wait();}
            let _=state.stop_service();
            // 清理较快时补足窗口展示时间；较慢时保留退出滚动条，不能提前切断后台进程回收。
            if has_window {
                if let Some(remaining) = Duration::from_millis(1000).checked_sub(motion_started.elapsed()) {
                    std::thread::sleep(remaining);
                }
            }
            state.stopped.store(true, Ordering::SeqCst);
            app.exit(0);
        });
    }
}

fn boot_page_url(url: &tauri::Url) -> bool {
    ["/", "/index.html", "/startup.html", "/management.html"].contains(&url.path())
        && ((url.scheme() == "tauri" && url.host_str() == Some("localhost"))
            || (url.scheme() == "http"
                && url.host_str() == Some("tauri.localhost")
                && url.port().is_none()))
}

// request_service_start（首次主页面加载后请求服务）：刷新及业务导航不会再次启动。
fn request_service_start(
    url: &tauri::Url,
    event: PageLoadEvent,
    requested: &AtomicBool,
    stopping: &AtomicBool,
) -> bool {
    event == PageLoadEvent::Finished
        && boot_page_url(url)
        && !stopping.load(Ordering::SeqCst)
        && !requested.swap(true, Ordering::SeqCst)
}

fn trusted(window: &WebviewWindow, state: &Desktop) -> Result<(), String> {
    if !["main","startup"].contains(&window.label()) {
        return Err("此窗口没有桌面操作权限。".into());
    }
    let url = window.url().map_err(|e| e.to_string())?;
    if boot_page_url(&url) {
        return Ok(());
    }
    let service_failed=state.error.lock().unwrap().is_some();
    let retained_page=state.recovery.lock().unwrap().preserves_page();
    if !state.stopping.load(Ordering::SeqCst)
        && (!service_failed || retained_page)
        && state.allows_frontend_url(&url)
    {
        Ok(())
    } else {
        Err("页面地址不属于当前应用实例。".into())
    }
}

fn trusted_floating(window: &WebviewWindow, state: &Desktop) -> Result<(), String> {
    if state.mode != FrontendMode::Product || window.label() != "floating" {
        return Err("此窗口没有悬浮窗权限。".into());
    }
    let url = window.url().map_err(|error| error.to_string())?;
    if !state.stopping.load(Ordering::Acquire) && state.allows_floating_url(&url)
    {
        Ok(())
    } else {
        Err("悬浮窗页面不属于当前应用实例。".into())
    }
}

fn set_floating_menu_checked(state: &Desktop, checked: bool) {
    if let Some(item) = state.floating_menu.lock().unwrap().as_ref() {
        let _ = item.set_checked(checked);
    }
}

fn emit_floating_model(app: &tauri::AppHandle, state: &Desktop) {
    if app.get_webview_window("floating").is_none() {
        return;
    }
    let model = state.floating_model.lock().unwrap().clone();
    let _ = app.emit_to("floating", "voice-terminal:floating-model", model);
}

fn set_floating_mode(app: &tauri::AppHandle, state: &Desktop, enabled: bool) -> Result<(), String> {
    let _transition = state.floating_transition.lock().map_err(|error| error.to_string())?;
    if state.stopping.load(Ordering::Acquire) {
        return Err("应用正在退出。".into());
    }
    if !enabled {
        state.floating_mode.store(false, Ordering::Release);
        set_floating_menu_checked(state, false);
        if let Some(floating) = app.get_webview_window("floating") {
            let _ = floating.hide();
        }
        if let Some(main) = app.get_webview_window("main").or_else(|| app.get_webview_window("startup")) {
            let _ = main.show();
            let _ = main.unminimize();
            let _ = main.set_focus();
        }
        return Ok(());
    }

    if state.mode != FrontendMode::Product || state.origin.lock().unwrap().is_none() {
        return Err("工作台尚未就绪，暂时无法打开悬浮窗。".into());
    }
    let main = app.get_webview_window("main").ok_or("主工作台窗口尚未建立。")?;
    let floating = match app.get_webview_window("floating") {
        Some(window) => window,
        None => create_floating_window(app, state)?,
    };
    state.floating_mode.store(true, Ordering::Release);
    set_floating_menu_checked(state, true);
    if state.floating_ready.load(Ordering::Acquire) {
        if let Err(error) = floating.show() {
            state.floating_mode.store(false, Ordering::Release);
            set_floating_menu_checked(state, false);
            return Err(error.to_string());
        }
        if let Err(error) = main.hide() {
            let _ = floating.hide();
            state.floating_mode.store(false, Ordering::Release);
            set_floating_menu_checked(state, false);
            return Err(error.to_string());
        }
        emit_floating_model(app, state);
    }
    Ok(())
}

fn create_floating_window(app: &tauri::AppHandle, state: &Desktop) -> Result<WebviewWindow, String> {
    let origin = state.origin.lock().map_err(|error| error.to_string())?.clone()
        .ok_or("工作台尚未建立受控界面来源。")?;
    let url = format!("{origin}/floating.html").parse::<tauri::Url>().map_err(|error| error.to_string())?;
    let navigation_state = app.state::<Arc<Desktop>>().inner().clone();
    let page_state = navigation_state.clone();
    let window = WebviewWindowBuilder::new(app, "floating", WebviewUrl::External(url))
        .title("Voice Terminal · 悬浮窗")
        // 主体占位由 300×282 缩为 150×141，底部另留 39 像素单行字幕空间。
        .inner_size(150., 180.)
        .resizable(false)
        .decorations(false)
        .transparent(true)
        .background_color(tauri::window::Color(0, 0, 0, 0))
        .shadow(false)
        .initialization_script(voice_desktop_common::webview::DISABLE_CONTEXT_MENU)
        .always_on_top(true)
        .skip_taskbar(true)
        .visible(false)
        .data_directory(state.context().map(|context| context.install.join("cache/webview2")).unwrap_or_else(|| state.data.clone()))
        .on_navigation(move |url| {
            let available=navigation_state.recovery.lock().unwrap().allows_navigation();
            available && navigation_state.allows_floating_url(url)
        })
        .on_page_load(move |window, payload| {
            if payload.event()==PageLoadEvent::Started {
                page_state.floating_ready.store(false,Ordering::Release);
                let _=window.hide();
                if page_state.floating_mode.load(Ordering::Acquire)&&!page_state.stopping.load(Ordering::Acquire){
                    if let Some(main)=window.app_handle().get_webview_window("main"){let _=main.show();}
                }
            }
            if payload.event() != PageLoadEvent::Finished
                || payload.url().path() != "/floating.html"
                || !page_state.allows_floating_url(payload.url())
            {
                return;
            }
            if page_state.floating_ready.load(Ordering::Acquire)&&page_state.floating_mode.load(Ordering::Acquire) && !page_state.stopping.load(Ordering::Acquire) {
                if window.show().is_ok() {
                    if let Some(main) = window.app_handle().get_webview_window("main") {
                        let _ = main.hide();
                    }
                    emit_floating_model(window.app_handle(), &page_state);
                } else {
                    let _ = set_floating_mode(window.app_handle(), &page_state, false);
                }
            }
        })
        .build()
        .map_err(|error| error.to_string())?;
    Ok(window)
}

/// floating_caption_projection（纯语音字幕边界）：未知消息或截图附件不进入悬浮窗。
fn floating_caption_projection(value: Option<&serde_json::Value>) -> serde_json::Value {
    (|| {
        let item = value?.as_object()?;
        if item.contains_key("attachments") || item.contains_key("images") { return None; }
        let role = item.get("role")?.as_str()?;
        if !["user", "assistant"].contains(&role) { return None; }
        let key = item.get("key")?.as_str()?;
        let text = item.get("text")?.as_str()?;
        let length = text.encode_utf16().count() as u64;
        if text.trim().is_empty() || length > 32000 || key.len() > 512 { return None; }
        let start = item.get("focusStart")?.as_u64()?;
        let end = item.get("focusEnd")?.as_u64()?;
        if start > end || end > length { return None; }
        Some(serde_json::json!({"key":key,"role":role,"text":text,"focusStart":start,"focusEnd":end,
            "active":item.get("active").and_then(serde_json::Value::as_bool).unwrap_or(false)}))
    })().unwrap_or(serde_json::Value::Null)
}

#[tauri::command(rename_all = "snake_case")]
fn publish_floating_model(
    window: WebviewWindow,
    app: tauri::AppHandle,
    state: tauri::State<Arc<Desktop>>,
    model: serde_json::Value,
) -> Result<(), String> {
    require_command(state.mode, DesktopCommand::PublishFloatingModel)?;
    trusted(&window, &state)?;
    if window.label() != "main" {
        return Err("只有主工作台可以发布悬浮窗状态。".into());
    }
    let status = model.get("state").and_then(serde_json::Value::as_str)
        .filter(|value| ["standby", "listening", "thinking", "speaking", "offline", "error"].contains(value))
        .ok_or("悬浮窗运行状态无效。")?;
    let session_active = model.get("sessionActive").and_then(serde_json::Value::as_bool)
        .ok_or("悬浮窗会话状态无效。")?;
    let levels = model.get("audioLevels").and_then(serde_json::Value::as_array)
        .filter(|levels| levels.len() == 5).ok_or("悬浮窗音量数据无效。")?;
    let normalized_levels = levels.iter().map(|level| {
        level.as_f64().filter(|value| value.is_finite())
            .map(|value| value.clamp(0.0, 1.0)).ok_or("悬浮窗音量数据无效。")
    }).collect::<Result<Vec<_>, _>>()?;
    let revision = state.floating_revision.fetch_add(1, Ordering::AcqRel) + 1;
    let projection = serde_json::json!({
        "revision": revision,
        // doubleClickMs（系统双击间隔）：尊重慢速双击设置，不提前执行首击。
        "doubleClickMs": floating_double_click_ms(),
        "state": status,
        "sessionActive": session_active,
        "audioLevels": normalized_levels,
        "silenced": model.get("silenced").and_then(serde_json::Value::as_bool).unwrap_or(!session_active),
        "canControl": model.get("canControl").and_then(serde_json::Value::as_bool).unwrap_or(false),
        "caption": floating_caption_projection(model.get("caption")),
    });
    *state.floating_model.lock().map_err(|error| error.to_string())? = projection.clone();
    let _ = app.emit_to("floating", "voice-terminal:floating-model", projection);
    Ok(())
}

#[tauri::command]
fn get_floating_model(window: WebviewWindow, state: tauri::State<Arc<Desktop>>) -> Result<serde_json::Value, String> {
    trusted_floating(&window, &state)?;
    // 此请求由悬浮界面挂载后发出；单纯网页加载完成不足以隐藏主工作台。
    if !state.floating_ready.swap(true,Ordering::AcqRel)&&state.floating_mode.load(Ordering::Acquire){
        set_floating_mode(window.app_handle(),&state,true)?;
    }
    Ok(state.floating_model.lock().map_err(|error| error.to_string())?.clone())
}

fn floating_double_click_ms() -> u32 {
    #[cfg(windows)]
    { unsafe { windows_sys::Win32::UI::Input::KeyboardAndMouse::GetDoubleClickTime().clamp(500, 5000) } }
    #[cfg(not(windows))]
    { 500 }
}

#[tauri::command]
fn return_to_workbench(window: WebviewWindow, state: tauri::State<Arc<Desktop>>) -> Result<(), String> {
    trusted_floating(&window, &state)?;
    window.app_handle().emit_to("main", "voice-terminal:floating-workbench", ()).map_err(|error| error.to_string())?;
    set_floating_mode(&window.app_handle(), &state, false)
}

#[tauri::command]
fn control_floating_session(window: WebviewWindow, state: tauri::State<Arc<Desktop>>, action: String) -> Result<(), String> {
    trusted_floating(&window, &state)?;
    if !["silence", "start"].contains(&action.as_str()) { return Err("悬浮窗语音操作无效。".into()); }
    if !state.floating_mode.load(Ordering::Acquire) { return Err("悬浮窗尚未启用。".into()); }
    window.app_handle().emit_to("main", "voice-terminal:floating-control", action).map_err(|error| error.to_string())
}

#[tauri::command]
fn start_floating_drag(window: WebviewWindow, state: tauri::State<Arc<Desktop>>) -> Result<(), String> {
    trusted_floating(&window, &state)?;
    window.start_dragging().map_err(|error| error.to_string())
}
#[tauri::command]
fn desktop_status(
    window: WebviewWindow,
    state: tauri::State<Arc<Desktop>>,
) -> Result<serde_json::Value, String> {
    require_command(state.mode, DesktopCommand::Status)?;
    trusted(&window, &state)?;
    if let Ok(url) = window.url() {
        if state.is_settled_frontend_url(&url) {
            state.launch_token.lock().unwrap().take();
        }
    }
    // ready（是否就绪）、error（错误信息）、frontend_mode（前端模式）、development（是否显式源码调试）。
    let runtime_recovery=state.recovery.lock().unwrap().snapshot();
    Ok(serde_json::json!({
        "ready": state.origin.lock().unwrap().is_some(),
        "startup_skipped": state.startup_skipped.load(Ordering::Acquire),
        "runtime_recovery": runtime_recovery, // runtime_recovery（运行期热更新状态，不含私有会话）。
        "error": *state.error.lock().unwrap(),
        "role": state.mode.label(), // role（入口职责）：product（正式程序）或 ui-preview（界面演示）。
        "frontend_mode": state.mode.label(),
        "development": state.development,
        "workspace_ready":state.context().is_some(),
        "workspace_busy":state.workspace_busy.load(Ordering::Acquire),
        "initialization_failed":state.initialization.lock().unwrap()["failed"]==true,
        "initialization":*state.initialization.lock().unwrap(),
        "runtime":state.context().and_then(|context|voice_desktop_common::runtime::status(&context).ok()),
        "fields":{"role":"product（正式程序）或 ui-preview（隔离演示）","frontend_mode":"被授权的界面入口","development":"是否显式源码调试","ready":"界面业务来源已建立","startup_skipped":"已选择跳过加载画面","runtime_recovery":"当前窗口的组件热更新与业务恢复","error":"启动失败原因","workspace_ready":"正式工作区已解析","workspace_busy":"正在切换或迁移","initialization_failed":"初始化失败标识","initialization":"启动阶段与进度","runtime":"本地语音环境状态"}
    }))
}
// desktop_page_ready（真实页面就绪）：界面挂载后确认；迁移不使用缓存启动状态。
#[tauri::command]
fn desktop_page_ready(window:WebviewWindow,state:tauri::State<Arc<Desktop>>)->Result<(),String>{
    trusted(&window,&state)?;require_command(state.mode,DesktopCommand::FocusMain)?;
    let url=window.url().map_err(|e|e.to_string())?;
    let management=url.path()=="/management.html"&&boot_page_url(&url);
    if !management&&!(state.is_settled_frontend_url(&url)&&state.allows_frontend_url(&url)){return Err("页面尚未进入管理界面或产品界面。".into());}
    let context=state.context().ok_or("缺少正式工作区")?;
    workspace::atomic_json(&context.data.join("state/workspace-ready.json"),&serde_json::json!({"schema":1,"pid":std::process::id(),"workspace_id":context.workspace_id,"role":"product","ready":true,"management_ready":management,"development":context.dev,"fields":{"schema":"页面就绪记录格式","pid":"桌面进程编号","workspace_id":"工作区编号","role":"product（正式程序）","ready":"界面已真实挂载","management_ready":"组件管理页已挂载","development":"显式源码调试"}}))?;
    if management{if let Some(manager)=window.app_handle().try_state::<Arc<ResourceManager>>(){
        if let Err(error)=manager.inner().resume_maintenance(){
            // 页面已经真实挂载；任务准入失败保留交接并显示原因，不能让前端卸载已确认的管理界面。
            *state.error.lock().unwrap()=Some(format!("组件维护任务未能启动，交接记录已保留，可在组件列表重试：{error}"));
        }
    }}
    window.show().map_err(|error|error.to_string())?;
    // 新界面完成挂载及绘制后才撤走临时启动窗口，加载失败时旧画面仍可见。
    if window.label()=="main" {
        if let Some(startup)=window.app_handle().get_webview_window("startup"){
            startup.destroy().map_err(|error|error.to_string())?;
            let cache=state.data.clone();std::thread::spawn(move||cleanup_initialization_cache(&cache));
        }
    }
    Ok(())
}
#[tauri::command]
fn skip_startup(window: WebviewWindow, state: tauri::State<Arc<Desktop>>) -> Result<(), String> {
    require_command(state.mode, DesktopCommand::SkipStartup)?;
    trusted(&window, &state)?;
    if state.stopping.load(Ordering::Acquire) {
        return Err("应用正在退出。".into());
    }
    state.startup_skipped.store(true, Ordering::Release);
    Ok(())
}
#[tauri::command]
async fn export_logs(window: WebviewWindow, state: tauri::State<'_, Arc<Desktop>>) -> Result<Option<String>, String> {
    require_command(state.mode, DesktopCommand::ExportLogs)?;
    trusted(&window, &state)?;
    if state.stopping.load(Ordering::Acquire) { return Err("应用正在退出。".into()); }
    if state.workspace_busy.load(Ordering::Acquire) { return Err("工作区正在切换，请稍后打包日志。".into()); }
    if state.log_export_busy.swap(true, Ordering::AcqRel) { return Err("日志正在打包，请稍候。".into()); }
    let _reset = ResetFlag(&state.log_export_busy);
    let operation = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        // service fallback（服务回退）：后端已退出时仍继续同步并导出磁盘上的日志。
        let _ = operation.flush_service_logs();
        let Some(destination) = voice_desktop_common::native_ui::choose_folder("选择日志压缩包保存文件夹")? else {
            return Ok(None);
        };
        let context = operation.context().ok_or("缺少产品工作区")?;
        let archive = voice_desktop_common::logs::archive_logs(&context.install.join("cache/logs"), &destination)?;
        Ok(Some(archive.to_string_lossy().into_owned()))
    }).await.map_err(|error| error.to_string())?
}
#[tauri::command]
fn focus_main(window: WebviewWindow, state: tauri::State<Arc<Desktop>>) -> Result<(), String> {
    require_command(state.mode, DesktopCommand::FocusMain)?;
    trusted(&window, &state)?;
    show(&window.app_handle());
    Ok(())
}
#[tauri::command]
fn exit_application(
    window: WebviewWindow,
    app: tauri::AppHandle,
    state: tauri::State<Arc<Desktop>>,
) -> Result<(), String> {
    require_command(state.mode, DesktopCommand::Exit)?;
    trusted(&window, &state)?;
    state.inner().shutdown(&app);
    Ok(())
}
#[tauri::command]
fn open_console(
    window: WebviewWindow,
    state: tauri::State<Arc<Desktop>>,
    url: String,
) -> Result<(), String> {
    require_command(state.mode, DesktopCommand::OpenConsole)?;
    trusted(&window, &state)?;
    let url = console_url(&url)?;
    if state.dsh_origin.lock().unwrap().as_deref()!=Some(url.origin().ascii_serialization().as_str()){
        return Err("控制台端口不属于当前工作区的受管 DSH（代理宿主）。".into());
    }
    // 通过系统默认浏览器打开 DSH（代理执行宿主）网页，使用完整令牌地址登录。
    voice_desktop_common::windows::open_url(url.as_str())
}

// console_url（控制台地址校验）：限制桌面入口只能打开当前本机代理控制台。
fn console_url(value: &str) -> Result<tauri::Url, String> {
    let url: tauri::Url = value.parse().map_err(|_| "控制台地址无效")?;
    // 服务返回的短期令牌地址允许动态分配的本机 DSH（代理执行宿主）端口。
    let query: Vec<_> = url.query_pairs().collect();
    if url.scheme() != "http"
        || url.host_str() != Some("127.0.0.1")
        || url.port().filter(|v|*v>0).is_none()
        || url.path() != "/"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
        || query.len() != 1
        || query[0].0 != "token"
        || query[0].1.is_empty()
    {
        return Err("控制台地址不符合本机入口约束。".into());
    }
    Ok(url)
}
#[tauri::command]
fn workspace_info(window:WebviewWindow,state:tauri::State<Arc<Desktop>>)->Result<serde_json::Value,String>{
    require_command(state.mode,DesktopCommand::FocusMain)?;trusted(&window,&state)?;
    let context=state.context().ok_or("缺少产品工作区")?;
    Ok(serde_json::json!({"root":context.install,"data_dir":context.data,
        "version":env!("CARGO_PKG_VERSION"),"can_manage":true,
        "fields":{"root":"当前工作区根目录","data_dir":"用户数据目录","version":"程序版本","can_manage":"允许管理工作区"}}))
}
fn require_workspace(window:&WebviewWindow,state:&Desktop)->Result<(),String>{
    trusted(window,state)?;require_command(state.mode,DesktopCommand::FocusMain)?;
    if state.stopping.load(Ordering::Acquire){return Err("应用正在退出。".into());}Ok(())
}
#[tauri::command]
async fn workspace_choose(window:WebviewWindow,state:tauri::State<'_,Arc<Desktop>>)->Result<Option<serde_json::Value>,String>{
    require_workspace(&window,&state)?;
    let locator=state.locator.clone().ok_or("缺少本地工作区身份")?;
    tauri::async_runtime::spawn_blocking(move||{
        let chosen=voice_desktop_common::native_ui::choose_folder("选择工作区（新工作区须为空，可使用中文文件夹）")?;
        if let Some(root)=&chosen{locator.validate_target(root)?;}Ok(chosen.map(|root|serde_json::json!({"root":root})))
    }).await.map_err(|e|e.to_string())?
}
async fn workspace_operation(window:WebviewWindow,state:Arc<Desktop>,app:tauri::AppHandle,target_root:String,kind:&'static str)->Result<serde_json::Value,String>{
    require_workspace(&window,&state)?;
    reserve_maintenance(&state.maintenance_gate,&state.workspace_busy,&state.app_update.busy,false)?;
    let operation_state=state.clone();
    let recovery_app=app.clone(); // recovery_app（失败时重新开放资源任务）。
    let result=tauri::async_runtime::spawn_blocking(move||{
        // workspace_busy（迁移门闩）已关闭新保存；等待此前正在写入的接口/语音配置完成。
        drop(operation_state.credentials_guard.lock().map_err(|_|"配置写入锁不可用。")?);
        let context=operation_state.context().ok_or("缺少工作区上下文")?;
        let locator=operation_state.locator.as_ref().ok_or("缺少本地工作区身份")?;
        let target=PathBuf::from(target_root);locator.validate_target(&target)?;
        if let Some(manager)=app.try_state::<Arc<ResourceManager>>(){manager.pause_and_wait()?;}
        let prepared=workspace::prepare_task(&context.install,&target,kind,std::process::id(),locator);
        let journal=prepared?;
        let record:serde_json::Value=serde_json::from_slice(&fs::read(&journal).map_err(|e|e.to_string())?).map_err(|e|e.to_string())?;
        let job_id=record["job_id"].as_str().ok_or("工作区事务缺少编号")?.to_string();
        let helper=journal.parent().unwrap().join("workspace-helper.exe");
        let mut command=Command::new(helper);context.configure_command(&mut command);
        let spawn=command.arg("--workspace-task").arg(&journal).args(locator.child_args()).creation_flags(0x08000000).current_dir(journal.parent().unwrap()).spawn();
        if let Err(error)=spawn{return Err(format!("无法启动工作区事务：{error}"));}
        let _=app.emit("desktop://workspace-progress",serde_json::json!({"job_id":job_id,"state":"running","phase":"原生迁移窗口已打开，正在退出当前工作区","can_cancel":false,"completed_bytes":0,"total_bytes":0}));
        let exit_state=operation_state.clone();let exit_app=app.clone();
        std::thread::spawn(move||{std::thread::sleep(Duration::from_millis(350));exit_state.shutdown(&exit_app);});
        Ok(serde_json::json!({"job_id":job_id}))
    }).await.map_err(|e|e.to_string()).and_then(|v|v);
    if let Err(mut error)=result{
        if let Some(manager)=recovery_app.try_state::<Arc<ResourceManager>>(){if let Err(recovery)=manager.resume_accepting_jobs(){error=format!("{error}；恢复维护前服务失败：{recovery}");}}
        state.workspace_busy.store(false,Ordering::Release);return Err(error);
    }result
}

// reserve_maintenance（原子预留维护任务）：阻止网络下载期间插入工作区迁移。
fn reserve_maintenance(gate:&Mutex<()>,workspace:&AtomicBool,update:&AtomicBool,updating:bool)->Result<(),String>{
    let _guard=gate.lock().map_err(|_|"维护操作锁不可用。")?;
    if workspace.load(Ordering::Acquire){return Err("工作区正在切换或迁移，请等待完成。".into());}
    if update.load(Ordering::Acquire){return Err("程序更新正在下载或交接，请等待完成或取消更新。".into());}
    if updating{update.store(true,Ordering::Release);}else{workspace.store(true,Ordering::Release);}Ok(())
}
#[tauri::command(rename_all = "snake_case")]
async fn workspace_link(window:WebviewWindow,state:tauri::State<'_,Arc<Desktop>>,app:tauri::AppHandle,target_root:String)->Result<serde_json::Value,String>{workspace_operation(window,state.inner().clone(),app,target_root,"link").await}
#[tauri::command(rename_all = "snake_case")]
async fn workspace_migrate(window:WebviewWindow,state:tauri::State<'_,Arc<Desktop>>,app:tauri::AppHandle,target_root:String)->Result<serde_json::Value,String>{workspace_operation(window,state.inner().clone(),app,target_root,"migrate").await}

fn app_update_payload(state_name:&str,manifest:Option<&voice_desktop_common::app_update::AppUpdateManifest>,done:u64,total:u64,phase:Option<&str>,error:Option<&str>,can_cancel:bool)->serde_json::Value{
    serde_json::json!({"state":state_name,"current_version":env!("CARGO_PKG_VERSION"),"target_version":manifest.map(|value|value.version.as_str()),
        "release_notes":manifest.map(|value|value.release_notes.as_str()),"completed_bytes":done,"total_bytes":total,"phase":phase,"error":error,"can_cancel":can_cancel,
        "fields":{"state":"更新阶段","current_version":"当前程序版本","target_version":"目标程序版本","release_notes":"发行说明","phase":"当前处理阶段",
            "completed_bytes":"已下载字节数","total_bytes":"安装器总字节数","error":"更新错误","can_cancel":"当前是否允许取消"}})
}
fn emit_app_update(state:&Desktop,app:&tauri::AppHandle,payload:serde_json::Value){
    *state.app_update.status.lock().unwrap()=payload.clone();let _=app.emit(APP_UPDATE_PROGRESS_EVENT,payload);
}
#[tauri::command]
fn project_open_repository(window:WebviewWindow,state:tauri::State<'_,Arc<Desktop>>)->Result<(),String>{
    require_workspace(&window,&state)?;
    voice_desktop_common::windows::open_url("https://github.com/wet86y/voice-terminal")
}
#[tauri::command]
fn open_minimax_voice_library(window:WebviewWindow,state:tauri::State<'_,Arc<Desktop>>)->Result<(),String>{
    require_workspace(&window,&state)?;
    voice_desktop_common::windows::open_url("https://solutions.minimaxi.com/voices")
        .map_err(|_|"无法打开 MiniMax 官方音色库。".into())
}
#[tauri::command]
async fn app_update_check(window:WebviewWindow,state:tauri::State<'_,Arc<Desktop>>,app:tauri::AppHandle)->Result<serde_json::Value,String>{
    require_workspace(&window,&state)?;
    if state.app_update.busy.compare_exchange(false,true,Ordering::AcqRel,Ordering::Acquire).is_err(){return Err("应用维护任务正在执行，请等待完成。".into());}
    let _reset=ResetFlag(&state.app_update.busy);
    *state.app_update.manifest.lock().unwrap()=None;
    emit_app_update(&state,&app,app_update_payload("checking",None,0,0,Some("正在读取最新发行清单"),None,false));
    let checked=tauri::async_runtime::spawn_blocking(move||voice_desktop_common::app_update::check_latest(env!("CARGO_PKG_VERSION"))).await.map_err(|error|error.to_string())?;
    let payload=match checked{
        Ok(Some(manifest))=>{
            *state.app_update.manifest.lock().unwrap()=Some(manifest.clone());
            app_update_payload("available",Some(&manifest),0,manifest.size,None,None,false)
        }
        Ok(None)=>app_update_payload("current",None,0,0,None,None,false),
        Err(error)=>app_update_payload("failed",None,0,0,None,Some(&error),false),
    };
    emit_app_update(&state,&app,payload.clone());Ok(payload)
}
#[tauri::command]
async fn app_update_download(window:WebviewWindow,state:tauri::State<'_,Arc<Desktop>>,app:tauri::AppHandle)->Result<serde_json::Value,String>{
    require_workspace(&window,&state)?;
    let manifest=state.app_update.manifest.lock().unwrap().clone().ok_or("请先检查更新并确认有可用版本。")?;
    if !manifest.is_newer_than(env!("CARGO_PKG_VERSION"))?{return Err("当前没有可安装的新版本。".into());}
    reserve_maintenance(&state.maintenance_gate,&state.workspace_busy,&state.app_update.busy,true)?;
    state.app_update.cancel.store(false,Ordering::Release);
    let payload=app_update_payload("downloading",Some(&manifest),0,manifest.size,Some("正在连接 GitHub 并校验安装器"),None,true);
    emit_app_update(&state,&app,payload.clone());
    let desktop=state.inner().clone();let handle=app.clone();
    std::thread::spawn(move||download_and_handoff_update(desktop,handle,manifest,MaintenanceAction::Update));
    Ok(payload)
}
#[tauri::command]
fn app_update_cancel(window:WebviewWindow,state:tauri::State<Arc<Desktop>>)->Result<(),String>{
    require_workspace(&window,&state)?;
    state.app_update.request_cancel()
}
fn download_and_handoff_update(state:Arc<Desktop>,app:tauri::AppHandle,manifest:voice_desktop_common::app_update::AppUpdateManifest,action:MaintenanceAction){
    let result=(||{
        let context=state.context().ok_or("缺少当前工作区上下文")?;
        if state.workspace_busy.load(Ordering::Acquire){return Err("工作区正在切换或迁移，更新已停止。".into());}
        let installer=if action==MaintenanceAction::Update{
            voice_desktop_common::app_update::update_installer_path(&context.install)?
        }else{
            voice_desktop_common::app_update::installer_path(&context.install)?
        };
        fs::create_dir_all(installer.parent().ok_or("分发器目录无效")?).map_err(|error|format!("无法准备分发器目录：{error}"))?;
        let installer=if action==MaintenanceAction::Update{
            voice_desktop_common::app_update::update_installer_path(&context.install)?
        }else{
            voice_desktop_common::app_update::installer_path(&context.install)?
        };
        let retained=if action==MaintenanceAction::CheckIntegrity{
            voice_desktop_common::app_update::retained(&context.install,env!("CARGO_PKG_VERSION"),&integrity_commands::local_build(&context)?)?
        }else{voice_desktop_common::app_update::delivery(&manifest)?};
        if action!=MaintenanceAction::Update&&!retained.matches(env!("CARGO_PKG_VERSION"),&integrity_commands::local_build(&context)?){return Err("发行校验程序与当前本地构建不一致；不会下载或运行其他构建。".into());}
        let source=manifest.asset_url();
        let file=voice_desktop_common::download::DownloadFile{url:source,size:manifest.size,sha256:manifest.sha256.clone(),sha512:String::new(),path:manifest.asset.clone(),download_variants:vec![]};
        if action!=MaintenanceAction::CheckIntegrity{ for attempt in 1..=3{
            let result=voice_desktop_common::download::with_attempt(Some(attempt),||voice_desktop_common::download::download_with_progress(&file,&installer,state.app_update.cancel.clone(),&mut|phase,done,total|{
                let phase=format!("第 {attempt} / 3 轮 · {phase}");
                let payload=maintenance_payload(action,"downloading",Some(&manifest),done,total,Some(&phase),None,true);emit_app_update(&state,&app,payload);
            },0));
            match result{
                Ok(_)=>break,
                Err(error) if attempt<3&&resource_commands::retryable_failure(&error)&&!state.app_update.cancel.load(Ordering::Acquire)=>{
                    let phase=format!("第 {attempt} / 3 轮失败，等待自动恢复：{error}");
                    emit_app_update(&state,&app,maintenance_payload(action,"downloading",Some(&manifest),0,0,Some(&phase),None,true));
                    for _ in 0..if attempt==1{10}else{30}{
                        if state.app_update.cancel.load(Ordering::Acquire){return Err("更新下载已取消。".into());}
                        std::thread::sleep(Duration::from_millis(100));
                    }
                },
                Err(error)=>return Err(error),
            }
        }}
        if state.app_update.cancel.load(Ordering::Acquire){return Err("更新下载已取消。".into());}
        if action==MaintenanceAction::CheckIntegrity&&(fs::metadata(&installer).map_err(|error|error.to_string())?.len()!=manifest.size||workspace::file_hash(&installer)?!=manifest.sha256.to_lowercase()){
            let _=fs::remove_file(&installer);return Err("分发安装器大小或 SHA-256 摘要校验失败。".into());
        }
        if action!=MaintenanceAction::Update{voice_desktop_common::app_update::save_retained(&context.install,&retained)?;}
        if action==MaintenanceAction::DownloadIntegrity{
            emit_app_update(&state,&app,maintenance_payload(action,"current",Some(&manifest),manifest.size,manifest.size,Some("当前版本校验程序已准备；点击校验完整性开始检查与修复。"),None,false));return Ok(());
        }
        if let Some(manager)=app.try_state::<Arc<ResourceManager>>(){manager.pause_and_wait()?;}
        if state.app_update.cancel.load(Ordering::Acquire){return Err("更新交接已取消。".into());}
        let pid=std::process::id();let (parent_create_time,parent_executable)=workspace::process_identity(pid)?.ok_or("当前程序进程身份不可用。")?;
        let expected=voice_desktop_common::canonical_path(&context.install.join("app/VoiceTerminalDesktop.exe"))?;
        if !parent_executable.to_string_lossy().eq_ignore_ascii_case(&expected.to_string_lossy()){
            return Err("当前程序身份与工作区不匹配，拒绝启动更新安装器。".into());
        }
        let nonce=workspace::new_id();let config_path=workspace::safe_path(&context.install,&format!("tmp/app-update-{nonce}.json"))?;
        let consumed_path=config_path.with_extension("json.consumed");
        if config_path.exists()||consumed_path.exists(){return Err("更新配置编号重复，拒绝继续。".into());}
        let config=serde_json::json!({"schema":1,"nonce":nonce,"target_root":context.install,"workspace_id":context.workspace_id,
            "parent_pid":pid,"parent_create_time":parent_create_time,"parent_executable":parent_executable,
            "version":manifest.version,"tag":manifest.tag,"installer_size":manifest.size,"installer_sha256":manifest.sha256,"build":retained.build,
            "fields":{"schema":"一次性应用更新配置格式","nonce":"单次使用随机编号","target_root":"当前有效工作区根目录",
                "workspace_id":"必须保持不变的工作区身份编号","parent_pid":"当前主程序进程编号","parent_create_time":"当前主程序创建时间",
                "parent_executable":"当前主程序可执行文件位置","version":"待安装产品版本","tag":"对应 GitHub 发行标签",
                "build":"目标程序构建编号","installer_size":"已校验分发安装器字节数","installer_sha256":"已校验分发安装器 SHA-256 摘要"}});
        workspace::atomic_json(&config_path,&config)?;
        let handing_off=maintenance_payload(action,"handing_off",Some(&manifest),manifest.size,manifest.size,Some("正在交接已校验的更新安装器"),None,false);
        if let Err(error)=state.app_update.begin_handoff(handing_off.clone()){let _=fs::remove_file(&config_path);return Err(error);}
        let _=app.emit(APP_UPDATE_PROGRESS_EVENT,handing_off);
        let mut command=Command::new(&installer);command.arg(if action==MaintenanceAction::CheckIntegrity{"--repair-config"}else{"--app-update-config"}).arg(&config_path).current_dir(&context.install)
            .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).creation_flags(0x08000000);
        if let Err(error)=command.spawn(){let _=fs::remove_file(&config_path);return Err(format!("无法启动更新分发器：{error}"));}
        emit_app_update(&state,&app,maintenance_payload(action,"handing_off",Some(&manifest),manifest.size,manifest.size,Some("正在关闭旧程序并启动安装器"),None,false));
        state.shutdown(&app);Ok::<(),String>(())
    })();
    if let Err(mut error)=result{
        if let Some(manager)=app.try_state::<Arc<ResourceManager>>(){if let Err(recovery)=manager.resume_accepting_jobs(){error=format!("{error}；恢复维护前服务失败：{recovery}");}}
        let cancelled=state.app_update.cancel.load(Ordering::Acquire)||error.contains("已取消");
        let payload=maintenance_payload(action,if cancelled{"cancelled"}else{"failed"},Some(&manifest),0,manifest.size,None,Some(&error),false);
        emit_app_update(&state,&app,payload);
    }
    state.app_update.busy.store(false,Ordering::Release);
}

fn show(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<Arc<Desktop>>() {
        let _ = set_floating_mode(app, state.inner(), false);
    }
    if let Some(window) = app.get_webview_window("main").or_else(||app.get_webview_window("startup")) {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn valid_launch_token(value: &str) -> bool {
    (43..=128).contains(&value.len())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
}

fn valid_instance_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
}

fn entry_url_allowed(url: &tauri::Url, expected_token: Option<&str>) -> bool {
    if url.path() != "/desktop-entry" || url.fragment().is_some() {
        return false;
    }
    let Some(expected) = expected_token else {
        return false;
    };
    let pairs: Vec<_> = url.query_pairs().collect();
    pairs.len() == 1 && pairs[0].0 == "token" && pairs[0].1 == expected
}

fn page_url_allowed(url: &tauri::Url, path: &str, allowed_keys: &[&str]) -> bool {
    if url.path() != path || url.fragment().is_some() {
        return false;
    }
    let mut seen = std::collections::HashSet::new();
    url.query_pairs().all(|(key, value)| {
        allowed_keys.contains(&key.as_ref()) && value.len() <= 64 && seen.insert(key.into_owned())
    })
}

fn product_entry_url_allowed(url: &tauri::Url, token: Option<&str>) -> bool {
    entry_url_allowed(url, token)
}

fn product_page_url_allowed(url: &tauri::Url) -> bool {
    page_url_allowed(url, "/", &["page", "settings"])
        || page_url_allowed(url, "/index.html", &["page", "settings"])
}

fn floating_page_url_allowed(url: &tauri::Url) -> bool {
    page_url_allowed(url, "/floating.html", &[])
}

fn preview_entry_url_allowed(url: &tauri::Url, token: Option<&str>) -> bool {
    entry_url_allowed(url, token)
}

fn preview_page_url_allowed(url: &tauri::Url) -> bool {
    page_url_allowed(
        url,
        "/ui-demo.html",
        &["page", "scene", "runtime", "overlay", "replay", "session"],
    )
        // session（演示会话状态）由前端保存；允许活动状态页面刷新，仍校验固定页面与参数。
        && url
            .query_pairs()
            .all(|(key, value)| key != "session" || value == "active")
}

fn frontend_entry_url(origin: &str, token: &str, _mode: FrontendMode) -> Result<tauri::Url, String> {
    if !valid_launch_token(token) {
        return Err("服务入口启动凭据无效。".into());
    }
    let mut url: tauri::Url = origin
        .parse()
        .map_err(|_| "服务入口地址无效。".to_string())?;
    if url.scheme() != "http"
        || url.host_str() != Some("127.0.0.1")
        || url.port().filter(|v|*v>0).is_none()
        || url.path() != "/"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err("服务入口地址不符合本机页面约束。".into());
    }
    url.set_path("/desktop-entry");
    tauri::Url::parse_with_params(url.as_str(), [("token", token)])
        .map_err(|_| "服务入口启动凭据无法编码。".to_string())
}

fn instance_identifier(mode: FrontendMode, source_root: Option<&Path>) -> String {
    let Some(source_root) = source_root else {
        return "com.voiceterminal.desktop".into();
    };
    // FNV-1a（稳定路径散列）确保相同源码根目录跨进程得到同一标识。
    let normalized = source_root.to_string_lossy().to_lowercase();
    let hash = normalized
        .bytes()
        .fold(0xcbf29ce484222325u64, |hash, byte| {
            (hash ^ u64::from(byte)).wrapping_mul(0x100000001b3)
        });
    let suffix = format!("{hash:016x}");
    match mode {
        FrontendMode::Product => format!("com.voiceterminal.desktop.source.{suffix}"),
        FrontendMode::UiPreview => format!("com.voiceterminal.desktop.ui-preview.{suffix}"),
    }
}

fn arg_value<'a>(args: &'a [String], key: &str) -> Result<Option<&'a str>, String> {
    let positions: Vec<_> = args
        .iter()
        .enumerate()
        .filter_map(|(index, arg)| (arg == key).then_some(index))
        .collect();
    if positions.len() > 1 {
        return Err(format!("{key} 不能重复。"));
    }
    positions
        .first()
        .map(|index| {
            args.get(index + 1)
                .map(String::as_str)
                .filter(|next| !next.starts_with("--"))
                .ok_or_else(|| format!("{key} 缺少路径。"))
        })
        .transpose()
}

fn ui_preview_context(args: &[String]) -> Result<(PathBuf, Option<PathBuf>, PathBuf), String> {
    for arg in args {
        if arg.starts_with("--")
            && !["--ui-preview", "--root", "--exit"].contains(&arg.as_str())
        {
            return Err(format!("桌面样例不接受产品启动参数：{arg}"));
        }
    }
    let root = arg_value(args, "--root")?.ok_or("桌面样例必须指定 --root（项目根目录）。")?;
    let program = voice_desktop_common::canonical_path(Path::new(root))?;
    let data = program.join(".local/cache/ui-demo");
    fs::create_dir_all(&data).map_err(|e| format!("桌面样例缓存目录不可用：{e}"))?;
    let data = voice_desktop_common::canonical_path(&data)?;
    if !data.starts_with(&program) {
        return Err("桌面样例缓存必须位于独立的项目运行目录。".into());
    }
    let shared = LaunchContext::portable(&program.join(".local"))?;
    let webview2 = shared.webview2;
    Ok((program, webview2, data))
}

#[derive(Clone,Copy,PartialEq)]
enum ServiceStartKind { Startup, ResourceActivation, RuntimeRecovery } // ServiceStartKind（首次启动、受控资源换入、故障恢复）。

fn pending_start_transaction(install:&Path,kind:ServiceStartKind)->bool{
    if install.join("tmp/update-transaction.json").exists(){return true;}
    let journal=install.join("tmp/native-install.json");
    if !journal.exists(){return false;}
    // 只有持有资源启用锁的内部恢复，才能验证本次已交换、尚待提交的候选。
    kind!=ServiceStartKind::ResourceActivation
        || voice_desktop_common::resources::load(&journal).map(|value|value["state"]!="swapped").unwrap_or(true)
}

fn start_service(app: tauri::AppHandle, state: Arc<Desktop>) -> Result<(), String> {
    start_service_for(app,state,ServiceStartKind::Startup)
}

fn start_service_for(app: tauri::AppHandle, state: Arc<Desktop>, kind:ServiceStartKind) -> Result<(), String> {
    let resource_recovery=kind!=ServiceStartKind::Startup;
    let _transition=state.service_transition.lock().map_err(|_|"服务交接锁不可用。")?;
    if state.service_starting.swap(true,Ordering::AcqRel){return Ok(());}
    struct StartingGuard<'a>(&'a AtomicBool); // StartingGuard（启动生命周期标记）：成功、失败及取消均释放。
    impl Drop for StartingGuard<'_>{fn drop(&mut self){self.0.store(false,Ordering::Release);}}
    let _starting=StartingGuard(&state.service_starting);
    let _credentials_guard=state.credentials_guard.lock().map_err(|_|"接口配置锁不可用。")?;
    if state.stopping.load(Ordering::Acquire){return Err("桌面正在退出。".into());}
    if !resource_recovery && app.try_state::<Arc<ResourceManager>>().is_some_and(|manager|manager.has_active_jobs()){
        return Err("组件任务尚未结束，请等待任务完成后启动。".into());
    }
    {
        let mut service=state.service.lock().map_err(|_|"服务状态不可用。")?;
        if service.as_mut().is_some_and(|value|matches!(value.child.try_wait(),Ok(None))){
            return Ok(());
        }
    }
    if state.mode==FrontendMode::Product {
        let Some(context)=state.context() else {return Ok(());};
        let context=context.refreshed_components()?;*state.context.lock().unwrap()=Some(context.clone());
        *state.error.lock().unwrap()=None;
        *state.initialization.lock().unwrap()=serde_json::json!({"phase":"检查必要启动条件","can_cancel":true});
        // 资源恢复只接纳管理器已验证并换入的候选，仍保留解释器、外部事务及凭据门禁。
        if !resource_recovery {
        let readiness=voice_desktop_common::resources::startup_readiness_with_progress(&context,&state.initialization_cancel,&mut|phase,done,total|{
            *state.initialization.lock().unwrap()=serde_json::json!({"phase":phase,"completed_bytes":done,"total_bytes":total,"can_cancel":true,
                "fields":{"phase":"启动组件校验阶段","completed_bytes":"全组件累计进度量（语音环境按文件数折算）","total_bytes":"全组件清单字节权重总量","can_cancel":"是否可取消校验"}});
        });
        if state.initialization_cancel.load(Ordering::Acquire){
            *state.initialization.lock().unwrap()=serde_json::json!({"phase":"启动校验已取消，可以重新启动","can_cancel":false});
            initialization::manage(&app,&state)?;return Ok(());
        }
        let readiness=readiness?;
        if let Some(manager)=app.try_state::<Arc<ResourceManager>>(){manager.record_validation(&readiness);}
        *state.initialization.lock().unwrap()=serde_json::json!({
            "phase":if readiness["ready"]==true{"组件校验通过，正在启动服务"}else{"部分组件需要处理，继续启动可用功能；可手动校验完整性"},
            "components_ready":readiness["ready"],"missing_components":readiness["missing"],"can_cancel":false,
            "fields":{"phase":"启动阶段","components_ready":"所有组件具备启动条件","missing_components":"待手动补齐的组件","can_cancel":"当前阶段是否可取消"}
        });
        }
        // 普通组件问题只作提示，业务按实际加载结果降级；解释器缺失或未完成事务仍保留管理界面。
        let pending=pending_start_transaction(&context.install,kind);
        let compatibility=voice_desktop_common::resources::load(&context.data.join("state/compatibility.json")).unwrap_or(serde_json::Value::Null);
        let unfinished=compatibility["enforcement"].is_object()&&compatibility["enforcement"]["phase"]!="complete";
        if !context.python.is_file()||pending||unfinished{
            if resource_recovery{return Err("运行环境或外部维护事务尚未就绪，无法恢复业务服务。".into());}
            initialization::manage(&app,&state)?;return Ok(());
        }
        let credentials=match voice_desktop_common::credentials::status_for_context(&context){
            Ok(value)=>value,
            Err(error)=>{
                if resource_recovery{return Err(format!("接口配置读取失败：{error}"));}
                *state.initialization.lock().unwrap()=serde_json::json!({"phase":"接口密钥配置需要处理，请进入模型管理页","components_ready":true,"credentials_ready":false,"credentials_error":error,"can_cancel":false,
                    "fields":{"phase":"启动阶段","components_ready":"所有组件具备启动条件","credentials_ready":"固定服务接口密钥已配置","credentials_error":"接口配置读取错误","can_cancel":"当前阶段是否可取消"}});
                initialization::manage(&app,&state)?;return Ok(());
            }
        };
        if !voice_desktop_common::credentials::ready(&credentials){
            if resource_recovery{return Err("接口密钥尚未配置，无法恢复业务服务。".into());}
            *state.initialization.lock().unwrap()=serde_json::json!({"phase":"请先在模型管理页填写接口密钥","components_ready":true,"credentials_ready":false,"can_cancel":false,
                "fields":{"phase":"启动阶段","components_ready":"所有组件具备启动条件","credentials_ready":"固定服务接口密钥已配置","can_cancel":"当前阶段是否可取消"}});
            initialization::manage(&app,&state)?;return Ok(());
        }
    }
    let logs = if let Some(context) = state.context() {
        context.install.join("cache/logs")
    } else {
        state.data.clone()
    };
    fs::create_dir_all(&logs).map_err(|e| e.to_string())?;
    let output_name = if state.mode == FrontendMode::UiPreview {
        "frontend-dev.log"
    } else {
        "desktop-service.log"
    };
    let output = File::options()
        .append(true)
        .create(true)
        .open(logs.join(output_name))
        .map_err(|e| e.to_string())?;
    let mut command;
    if state.mode == FrontendMode::UiPreview {
        let helper = state.program.join("scripts/frontend-dev.mjs");
        if !helper.is_file() {
            return Err("缺少桌面样例启动器 scripts/frontend-dev.mjs。".into());
        }
        let shared = LaunchContext::portable(&state.program.join(".local"))?;
        command = Command::new(shared.node.ok_or("宿主清单缺少 Node（脚本解释器）")?);
        command
            .arg(helper)
            .arg("--root")
            .arg(&state.program)
            .args(["--mode", "ui-preview"])
            .current_dir(&state.program);
        if let Some(engine) = &state.webview2 {
            command.env("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", engine);
        }
    } else {
        let context = state.context()
            .ok_or("产品服务缺少桌面启动上下文。")?;
        command = Command::new(&context.python);
        context.configure_command(&mut command);
        command
            .args(["-X", "utf8", "-X"])
            .arg(format!(
                "pycache_prefix={}",
                context.install.join("cache/python").display()
            ))
            .arg(context.program.join("scripts/backend-entry.py"))
            .arg("voice_terminal.application.launch")
            .arg("--root")
            .arg(&context.program)
            .arg("--python")
            .arg(&context.python)
            .arg("--data-dir")
            .arg(&context.data)
            .arg("--desktop-managed")
            .current_dir(&context.program);
        if context.dev {
            command.arg("--dev");
        }
        if context.diagnostic {
            command.arg("--diagnostic");
        }
    }
    command
        .creation_flags(0x08000000)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(output);
    let job = Job::new()?;
    // 与退出流程共用锁，防止主窗口提前关闭后仍建立后台服务。
    let mut service_slot = state.service.lock().map_err(|error| error.to_string())?;
    if state.stopping.load(Ordering::SeqCst) {
        return Ok(());
    }
    if service_slot.as_mut().is_some_and(|service|service.child.try_wait().ok().flatten().is_some()){service_slot.take();}
    if service_slot.is_some(){return Ok(());}
    let epoch=state.service_epoch.fetch_add(1,Ordering::SeqCst)+1;
    let preserve_page=resource_recovery && state.recovery.lock().unwrap().preserves_page();
    if !preserve_page{*state.origin.lock().unwrap()=None;}
    *state.error.lock().unwrap()=None;
    state.recovery.lock().unwrap().result=None;
    *state.dsh_origin.lock().unwrap()=None;
    let mut child = command.spawn().map_err(|e| {
        format!(
            "{} 启动失败：{e}",
            if state.mode == FrontendMode::UiPreview {
                "桌面样例 Node（脚本运行时）"
            } else {
                "服务管理进程"
            }
        )
    })?;
    if let Err(error) = job.assign(child.as_raw_handle()) {
        let _ = child.kill();
        let _ = child.wait();
        return Err(format!("无法建立进程所有权：{error}"));
    }
    let reader = BufReader::new(child.stdout.take().ok_or("缺少服务控制管道")?);
    let input = child.stdin.take().ok_or("缺少服务控制管道")?;
    *service_slot = Some(Service { child, input, job });
    drop(service_slot);
    // protocol（私有协议版本）、kind（消息类型）；Node（脚本运行时）与 Python（后端语言）均接收 start（启动）。
    let continuity=if preserve_page{state.recovery.lock().unwrap().continuity.clone()}else{None};
    state.send(serde_json::json!({"protocol":1,"kind":"start","continuity":continuity}))?;
    if state.mode == FrontendMode::Product {
        // pid（桌面宿主进程号）用于产品运行状态展示。
        state.send(serde_json::json!({
            "protocol": 1,
            "kind": "desktop_process",
            "pid": std::process::id()
        }))?;
    }
    let read_state = state.clone();
    let read_app = app.clone();
    std::thread::spawn(move || {
        for line in reader.lines() {
            if read_state.service_epoch.load(Ordering::Acquire)!=epoch{return;}
            let Ok(line) = line else { break };
            if line.len() > 16384 {
                *read_state.error.lock().unwrap() = Some("服务入口协议消息过长。".into());
                break;
            }
            let Ok(message) = serde_json::from_str::<serde_json::Value>(&line) else {
                continue;
            };
            if message["protocol"] != 1 {
                continue;
            }
            match message["kind"].as_str() {
                Some("ready") => {
                    // origin（本机页面来源）、launch_token（一次性启动凭据）、instance_id（服务实例标识）。
                    let Some(origin) = message["origin"].as_str() else {
                        *read_state.error.lock().unwrap() = Some("服务入口地址缺失。".into());
                        break;
                    };
                    let Some(token) = message["launch_token"].as_str() else {
                        *read_state.error.lock().unwrap() = Some("服务入口启动凭据缺失。".into());
                        break;
                    };
                    let Some(instance_id) = message["instance_id"].as_str() else {
                        *read_state.error.lock().unwrap() = Some("服务入口实例标识缺失。".into());
                        break;
                    };
                    if !valid_instance_id(instance_id) {
                        *read_state.error.lock().unwrap() = Some("服务入口实例标识无效。".into());
                        break;
                    }
                    let entry = match frontend_entry_url(origin, token, read_state.mode) {
                        Ok(url) => url,
                        Err(error) => {
                            *read_state.error.lock().unwrap() = Some(error);
                            break;
                        }
                    };
                    let normalized_origin = entry.origin().ascii_serialization();
                    {
                        let mut recovery=read_state.recovery.lock().unwrap();
                        if preserve_page && recovery.continuity.as_ref().is_none_or(|value|value["frontend_origin"]!=normalized_origin){
                            *read_state.error.lock().unwrap()=Some("恢复后的页面来源与当前窗口不匹配。".into());break;
                        }
                        if message["continuity"].is_object(){recovery.continuity=Some(message["continuity"].clone());}
                    }
                    let capability = frontend_capability(read_state.mode, &normalized_origin, epoch);
                    if let Err(error) = read_app.add_capability(capability.to_string()) {
                        *read_state.error.lock().unwrap() =
                            Some(format!("页面权限配置失败：{error}"));
                        break;
                    }
                    if read_state.mode == FrontendMode::Product {
                        if let Err(error) = read_app.add_capability(floating_capability(&normalized_origin, epoch).to_string()) {
                            *read_state.error.lock().unwrap() =
                                Some(format!("悬浮窗权限配置失败：{error}"));
                            break;
                        }
                        if let Err(error) = read_app.add_capability(floating_publisher_capability(&normalized_origin, epoch).to_string()) {
                            *read_state.error.lock().unwrap() =
                                Some(format!("悬浮窗状态发布权限配置失败：{error}"));
                            break;
                        }
                    }
                    *read_state.launch_token.lock().unwrap() = Some(token.into());
                    *read_state.origin.lock().unwrap() = Some(normalized_origin);
                    read_state.service_ready_epoch.store(epoch,Ordering::Release);
                    if preserve_page {
                        read_state.recovery.lock().unwrap().ready();
                        read_state.publish_recovery(&read_app);
                    } else if let Some(window) = read_app.get_webview_window("main") {
                        if let Err(error) = window.navigate(entry) {
                            *read_state.error.lock().unwrap() = Some(error.to_string());
                        }
                    }
                }
                Some("startup_result") => {
                    let mut recovery=read_state.recovery.lock().unwrap();
                    recovery.current_core=message["core_available"].as_bool();
                    recovery.started=true;recovery.result=Some((epoch,message.clone()));
                }
                Some("runtime_status")=>{read_state.recovery.lock().unwrap().current_core=message["core_available"].as_bool();}
                Some("exit_requested") => read_state.shutdown(&read_app),
                Some("logs_flushed") | Some("logs_flush_failed") => {
                    // request_id（刷新请求编号）绑定桌面宿主的落盘确认。
                    if let Some(request_id) = message["request_id"].as_u64() {
                        let mut result = read_state.log_flush_state.lock().unwrap();
                        if message["kind"] == "logs_flush_failed" {
                            result.failed_request = request_id;
                        }
                        result.completed_request = result.completed_request.max(request_id);
                        read_state.log_flush_changed.notify_all();
                    }
                }
                Some("endpoints")=>{
                    let origin=message["endpoints"]["dsh"]["url"].as_str().and_then(|value|value.parse::<tauri::Url>().ok()).filter(|url|url.scheme()=="http"&&url.host_str()==Some("127.0.0.1")&&url.port().filter(|v|*v>0).is_some()&&url.query().is_none()&&url.username().is_empty()&&url.password().is_none()).map(|url|url.origin().ascii_serialization());
                    *read_state.dsh_origin.lock().unwrap()=origin;
                },
                Some("stopped") => {}
                _ => {}
            }
        }
        if read_state.service_epoch.load(Ordering::Acquire)==epoch && !read_state.stopping.load(Ordering::SeqCst) {
            if !read_state.recovery.lock().unwrap().preserves_page(){*read_state.origin.lock().unwrap() = None;}
            *read_state.launch_token.lock().unwrap() = None;
            read_state.error.lock().unwrap().get_or_insert_with(|| {
                if read_state.mode == FrontendMode::UiPreview {
                    "桌面样例启动服务已异常结束，请检查独立缓存目录中的 frontend-dev.log（样例服务日志）。".into()
                } else {
                    "服务入口已异常结束，请检查 logs（日志目录）中的 desktop-service.log（服务入口日志）。".into()
                }
            });
            if read_state.recovery.lock().unwrap().preserves_page(){
                let error=read_state.error.lock().unwrap().clone().unwrap_or_else(||"业务服务异常退出".into());
                {let mut recovery=read_state.recovery.lock().unwrap();recovery.suspend();recovery.finish(Some(&error));}
                read_state.publish_recovery(&read_app);
            } else if let Some(window) = read_app.get_webview_window("main") {
                let _ = window.navigate("http://tauri.localhost/management.html".parse().unwrap());
                let _ = window.show();
            }
            if let Some(service) = read_state.service.lock().unwrap().as_mut() {
                service.job.terminate();
            }
        }
    });
    let timeout_state = state.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_secs(30));
        if timeout_state.service_epoch.load(Ordering::Acquire)==epoch && timeout_state.service_ready_epoch.load(Ordering::Acquire)!=epoch
            && !timeout_state.stopping.load(Ordering::SeqCst)
        {
            timeout_state.error.lock().unwrap().get_or_insert_with(|| {
                if timeout_state.mode == FrontendMode::UiPreview {
                    "等待桌面样例页面超时，请检查 Node（脚本运行时）服务日志。".into()
                } else {
                    "等待服务入口超时。请检查业务环境与端口占用。".into()
                }
            });
            if let Some(service) = timeout_state.service.lock().unwrap().as_mut() {
                service.job.terminate();
            }
        }
    });
    Ok(())
}

// create_window（统一窗口）：选定工作区后销毁临时窗口，将网页缓存切换到工作区。
fn create_window(app:&tauri::AppHandle,build_state:Arc<Desktop>,management:bool)->Result<WebviewWindow,String>{
    let repair_handoff=management&&build_state.context().is_some_and(|context|voice_desktop_common::component_maintenance::pending_repairs(&context).unwrap_or(false));
    let pending=build_state.mode==FrontendMode::Product&&build_state.context().is_none();
            let navigation_state = build_state.clone();
            let page_state = build_state.clone();
            let main_window =
                WebviewWindowBuilder::new(app, if pending {"startup"}else{"main"}, WebviewUrl::App(if management {"management.html".into()}else{"startup.html".into()}))
                    .background_color(tauri::window::Color(14,17,24,255))
                    .visible(false)
                    .initialization_script(voice_desktop_common::webview::DISABLE_CONTEXT_MENU)
                    .title(if build_state.mode == FrontendMode::UiPreview {
                        "Voice Terminal · 前端演示（无业务后端）"
                    } else {
                        "Voice Terminal · 语音终端"
                    })
                    // inner_size（默认内容尺寸）：小窗口启动，前端按可用空间逐级收起侧栏。
                    .inner_size(1000., 640.)
                    .min_inner_size(400., 480.)
                    .data_directory(build_state.context().map(|context|context.install.join("cache/webview2")).unwrap_or_else(||build_state.data.clone()))
                    .incognito(build_state.context().is_none())
                    .on_navigation(move |url| {
                        let available=navigation_state.recovery.lock().unwrap().allows_navigation();
                        (url.scheme() == "tauri" && url.host_str() == Some("localhost"))
                            || (url.scheme() == "http"
                                && url.host_str() == Some("tauri.localhost")
                                && url.port().is_none())
                            || (available && navigation_state.allows_frontend_url(url))
                    })
                    .on_page_load(move |window, payload| {
                        #[cfg(feature="qualification")]
                        qualification::page_loaded(&window,&page_state,payload.event(),payload.url());
                        // 首个受控窗口即可交接，管理页与产品页共用确认，不等待业务服务。
                        if payload.event()==PageLoadEvent::Finished&&!page_state.stopping.load(Ordering::Acquire)&&
                            (boot_page_url(payload.url())||page_state.allows_frontend_url(payload.url())) {
                            let _=window.show();
                            if let Some(context)=page_state.context(){let arguments=std::env::args().skip(1).collect::<Vec<_>>();let _=voice_desktop_common::installation::acknowledge(&context.install,&arguments);}
                        }
                        if repair_handoff{return;}
                        if !request_service_start(
                            payload.url(),
                            payload.event(),
                            &page_state.service_requested,
                            &page_state.stopping,
                        ) {
                            return;
                        }
                        // 最终主窗口的本地界面已加载，再从后台线程准备业务或演示服务。
                        let service_app = window.app_handle().clone();
                        let service_state = page_state.clone();
                        std::thread::spawn(move || {
                            if let Err(error) = initialization::auto_start(service_app, service_state.clone()) {
                                *service_state.error.lock().unwrap() = Some(error);
                            }
                        });
                    })
                    .build().map_err(|e|e.to_string())?;
            app_icon::apply(main_window.hwnd().map_err(|e|e.to_string())?.0).map_err(|e|e.to_string())?;
    Ok(main_window)
}
fn cleanup_initialization_cache(path:&Path){
    if !path.file_name().is_some_and(|name|name.to_string_lossy().starts_with("voice-initialization-")){return;}
    for _ in 0..25 {if !path.exists()||voice_desktop_common::workspace::remove_owned(path,path.parent().unwrap()).is_ok(){break;}std::thread::sleep(Duration::from_millis(200));}
}

#[cfg(feature="qualification")]
fn locator_for_check(args:&[String])->Result<Locator,String>{Locator::from_args(args)}
fn run() -> Result<(), String> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let preview_count = args
        .iter()
        .filter(|arg| arg.as_str() == "--ui-preview")
        .count();
    if preview_count > 1 {
        return Err("--ui-preview 不能重复。".into());
    }
    let preview = preview_count == 1;
    let exit_requested=args.iter().any(|arg|arg=="--exit");
    let (context, mode, program, data, webview2) = if preview {
        let (program, _, data) = ui_preview_context(&args)?;
        voice_desktop_common::prepare_webview_access_paths(&data,None)?;
        (None,FrontendMode::UiPreview,program,data,None)
    }else{
        let context=LaunchContext::from_args()?;
        context.prepare_webview_access()?;
        for (name,value) in context.environment(){std::env::set_var(name,value);}
        let program=context.program.clone();let data=context.install.join("cache/webview2");
        (Some(context),FrontendMode::Product,program,data,None)
    };
    std::env::remove_var("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER");
    if voice_desktop_common::webview::version().is_none(){voice_desktop_common::webview::guide("未检测到系统 WebView2（网页运行引擎）。");return Ok(());}
    // 管理页无需脚本引擎就绪；运行环境沿用分发器检测，管理页不重复弹安装引导。
    let locator=context.as_ref().map(|value|Locator::local(&value.install));
    let instance_identifier=instance_identifier(mode,if preview{Some(program.as_path())}else{context.as_ref().map(|value|value.install.as_path())});
    let development=preview||context.as_ref().is_some_and(|value|value.dev);
    let state = Arc::new(Desktop {
        context:Mutex::new(context),
        initialization:Mutex::new(serde_json::json!({"phase":"正在检查工作区","completed_bytes":0,"total_bytes":0})),
        initialization_cancel:Arc::new(AtomicBool::new(false)),
        program,
        data,
        webview2,
        mode,
        development,
        instance_identifier,
        service: Mutex::new(None),
        service_transition: Mutex::new(()),
        credentials_guard: Mutex::new(()),
        service_epoch: AtomicU64::new(0),
        service_ready_epoch: AtomicU64::new(0),
        locator,
        workspace_guard:Mutex::new(None),
        workspace_busy: AtomicBool::new(false),
        maintenance_gate: Mutex::new(()),
        log_export_busy: AtomicBool::new(false),
        log_flush_sequence: AtomicU64::new(0),
        log_flush_state: Mutex::new(LogFlushState { completed_request: 0, failed_request: 0 }),
        log_flush_changed: Condvar::new(),
        app_update:AppUpdateManager::new(),
        service_requested: AtomicBool::new(false),
        service_starting: AtomicBool::new(false),
        startup_skipped: AtomicBool::new(false),
        recovery: Mutex::new(runtime_recovery::RuntimeRecovery::default()),
        origin: Mutex::new(None),
        dsh_origin: Mutex::new(None),
        launch_token: Mutex::new(None),
        error: Mutex::new(None),
        stopping: AtomicBool::new(false),
        stopped: AtomicBool::new(false),
        tray: AtomicBool::new(false),
        floating_mode: AtomicBool::new(false),
        floating_ready: AtomicBool::new(false),
        floating_transition: Mutex::new(()),
        floating_revision: AtomicU64::new(0),
        floating_model: Mutex::new(serde_json::json!({"revision":0,"doubleClickMs":floating_double_click_ms(),"state":"standby","sessionActive":false,"audioLevels":[0,0,0,0,0],"silenced":true,"canControl":false,"caption":null})),
        floating_menu: Mutex::new(None),
    });
    // qualification-exit（验收正常退出）只接受独立验收配置，沿用实例自己的退出流程。
    let qualification_control = cfg!(feature = "qualification")
        && state.locator.as_ref().is_some_and(|value| value.qualification.is_some());
    let qualification_exit = qualification_control
        && args.iter().any(|value| value == "--qualification-exit");
    let instance_state = state.clone();
    let build_state = state.clone();
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(move |app, arguments, _| {
            if arguments.iter().any(|value|value=="--exit")||qualification_control
                && arguments.iter().any(|value| value == "--qualification-exit")
            {
                instance_state.shutdown(app);
            } else if instance_state.mode==FrontendMode::Product&&arguments.iter().any(|value|value=="--dev")!=instance_state.development {
                if let Some(context)=instance_state.context(){let _=workspace::atomic_json(&context.data.join("state/instance-conflict.json"),&serde_json::json!({"pid":std::process::id(),"existing_development":instance_state.development,"requested_development":arguments.iter().any(|value|value=="--dev"),"fields":{"pid":"已有正式进程编号","existing_development":"当前显式调试模式","requested_development":"请求的显式调试模式"}}));}
                std::thread::spawn(||voice_desktop_common::windows::error("当前工作区已运行另一种业务模式，请退出已有实例后重新启动。"));
            } else {
                show(app);
                if let Some(context)=instance_state.context(){let _=voice_desktop_common::installation::acknowledge(&context.install,&arguments);}
            }
        }))
        .manage(state.clone())
        .invoke_handler(tauri::generate_handler![
            desktop_status,desktop_page_ready,
            publish_floating_model,get_floating_model,return_to_workbench,start_floating_drag,control_floating_session,
            skip_startup,
            export_logs,
            open_console,
            exit_application,
            focus_main,
            desktop_initialize,desktop_manage,desktop_start_service,desktop_cancel_init,
            model_credentials_status,model_credentials_save,tts_selection_status,tts_selection_save,audio_enhancements_settings,
            resource_install_plan,voice_environment_status,voice_environment_plan,voice_environment_configure,voice_environment_import,gpu_environment_register,gpu_environment_probe,environment_guide,
            workspace_info,workspace_choose,workspace_link,workspace_migrate,app_integrity_status,app_integrity_download,app_integrity_check,app_update_check,app_update_download,app_update_cancel,project_open_repository,open_minimax_voice_library,
            resource_inventory,resource_jobs,resource_download,resource_download_required,resource_import,resource_import_confirm,resource_open_source,resource_cancel
        ])
        .setup(move |app| {
            if qualification_exit||exit_requested {
                build_state.shutdown(app.handle());
                return Ok(());
            }
            if let Some(context)=build_state.context(){
                *build_state.workspace_guard.lock().unwrap()=Some(voice_desktop_common::installation::WorkspaceGuard::acquire(&context.install).map_err(std::io::Error::other)?);
                initialization::install_manager(app.handle(),build_state.clone(),&context).map_err(std::io::Error::other)?;
            }
            let maintenance=build_state.context().map(|context|voice_desktop_common::component_maintenance::pending_repairs(&context)).transpose().map_err(std::io::Error::other)?.unwrap_or(false);
            create_window(app.handle(),build_state.clone(),maintenance).map_err(std::io::Error::other)?;
            if build_state.mode == FrontendMode::Product {
                let show_item = MenuItem::with_id(app, "show", "打开工作台", true, None::<&str>)?;
                let floating_item = CheckMenuItem::with_id(app, "floating", "悬浮窗模式", true, false, None::<&str>)?;
                let exit_item = MenuItem::with_id(app, "exit", "退出应用", true, None::<&str>)?;
                let menu = Menu::with_items(app, &[&show_item, &floating_item, &exit_item])?;
                *build_state.floating_menu.lock().unwrap() = Some(floating_item);
                let icon = app_icon::tray();
                let exit_state = build_state.clone();
                let floating_state = build_state.clone();
                let tray = TrayIconBuilder::with_id("voice-terminal")
                    .icon(icon)
                    .tooltip("Voice Terminal · 语音终端")
                    .menu(&menu)
                    .on_menu_event(move |app, event| match event.id.as_ref() {
                        "show" => show(app),
                        "floating" => {
                            let enabled = !floating_state.floating_mode.load(Ordering::Acquire);
                            if let Err(error) = set_floating_mode(app, &floating_state, enabled) {
                                set_floating_menu_checked(&floating_state, false);
                                if enabled {
                                    let _ = app.emit_to("main", "voice-terminal:floating-error", error);
                                }
                            }
                        }
                        "exit" => exit_state.shutdown(app),
                        _ => {}
                    })
                    .on_tray_icon_event(|tray, event| {
                        if matches!(
                            event,
                            TrayIconEvent::Click {
                                button: MouseButton::Left,
                                button_state: MouseButtonState::Up,
                                ..
                            }
                        ) {
                            show(tray.app_handle());
                        }
                    })
                    .build(app);
                build_state.tray.store(tray.is_ok(), Ordering::SeqCst);
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if matches!(event, tauri::WindowEvent::ScaleFactorChanged { .. }) {
                if let Ok(handle) = window.hwnd() {
                    // 跨屏幕改变显示缩放时，重新选择标题栏与任务栏的原生图标尺寸。
                    let _ = app_icon::apply(handle.0);
                    if let Some(tray) = window.app_handle().tray_by_id("voice-terminal") {
                        let _ = tray.set_icon(Some(app_icon::tray()));
                    }
                }
            }
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "main" {
                    let state = window.state::<Arc<Desktop>>();
                    if !state.stopped.load(Ordering::SeqCst) {
                        api.prevent_close();
                        if state.tray.load(Ordering::SeqCst) {
                            let _ = window.hide();
                        } else {
                            state.inner().shutdown(window.app_handle());
                        }
                    }
                } else if window.label() == "floating" {
                    let state = window.state::<Arc<Desktop>>();
                    if !state.stopped.load(Ordering::SeqCst) && !state.stopping.load(Ordering::SeqCst) {
                        api.prevent_close();
                        let _ = set_floating_mode(window.app_handle(), state.inner(), false);
                    }
                }
            }
        })
        .build({
            let mut context = tauri::generate_context!();
            context.config_mut().identifier = state.instance_identifier.clone();
            context
        })
        .map_err(|e| format!("桌面运行时不可用：{e}。请检查 WebView2（网页运行时）配置。"))?;
    let initialization_cache=state.data.clone();
    app.run(move |app, event| {
        if let tauri::RunEvent::ExitRequested { api, .. } = event {
            if !state.stopped.load(Ordering::SeqCst) {
                api.prevent_exit();
                state.shutdown(app);
            }
        }
    });
    cleanup_initialization_cache(&initialization_cache);
    Ok(())
}
fn main() {
    if let Some(result) = voice_desktop_common::windows::run_url_helper_if_requested() {
        if result.is_err() {
            std::process::exit(1);
        }
        return;
    }
    #[cfg(feature="qualification")]
    if qualification::run_if_requested().is_some(){return;}
    if let Err(message) = run() {
        if message.contains("WebView2") || message.contains("webview") || message.contains("网页引擎") {
            voice_desktop_common::webview::guide(&message);
        } else {voice_desktop_common::windows::error(&message);}
    }
}

#[cfg(test)]
mod tests {
    use super::{
        command_allowed, console_url, frontend_capability, frontend_entry_url, instance_identifier,
        floating_capability, floating_page_url_allowed, floating_publisher_capability,
        preview_entry_url_allowed, preview_page_url_allowed, product_entry_url_allowed,
        product_page_url_allowed, request_service_start, require_command, ui_preview_context,
        valid_instance_id, valid_launch_token, DesktopCommand, FrontendMode,
    };
    use std::sync::atomic::{AtomicBool, Ordering};
    use tauri::webview::PageLoadEvent;

    #[test]
    fn startup_only_admits_the_controlled_swapped_resource_transaction(){
        use super::{pending_start_transaction,ServiceStartKind};
        let root=std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../.local/tmp")
            .join(format!("startup-transaction-{}",voice_desktop_common::workspace::new_id()));
        std::fs::create_dir_all(root.join("tmp")).unwrap();
        let journal=root.join("tmp/native-install.json");
        for phase in ["prepared","swapped","committed"]{
            std::fs::write(&journal,serde_json::json!({"state":phase,"target":"models/optional"}).to_string()).unwrap();
            assert!(pending_start_transaction(&root,ServiceStartKind::Startup));
            assert!(pending_start_transaction(&root,ServiceStartKind::RuntimeRecovery));
            assert_eq!(pending_start_transaction(&root,ServiceStartKind::ResourceActivation),phase!="swapped");
        }
        std::fs::write(&journal,"invalid").unwrap();
        assert!(pending_start_transaction(&root,ServiceStartKind::ResourceActivation));
        std::fs::write(&journal,r#"{"state":"swapped"}"#).unwrap();
        std::fs::write(root.join("tmp/update-transaction.json"),"{}").unwrap();
        assert!(pending_start_transaction(&root,ServiceStartKind::ResourceActivation));
        std::fs::remove_file(journal).unwrap();std::fs::remove_file(root.join("tmp/update-transaction.json")).unwrap();
        std::fs::remove_dir(root.join("tmp")).unwrap();std::fs::remove_dir(root).unwrap();
    }

    #[test]
    fn update_and_workspace_handoffs_reserve_a_single_maintenance_slot(){
        use super::reserve_maintenance;
        use std::sync::Mutex;
        let gate=Mutex::new(());let workspace=AtomicBool::new(false);let update=AtomicBool::new(false);
        let start=std::sync::Barrier::new(2);
        let outcomes=std::thread::scope(|scope|{
            let first=scope.spawn(||{start.wait();reserve_maintenance(&gate,&workspace,&update,false).is_ok()});
            let second=scope.spawn(||{start.wait();reserve_maintenance(&gate,&workspace,&update,true).is_ok()});
            [first.join().unwrap(),second.join().unwrap()]
        });
        assert_eq!(outcomes.iter().filter(|value|**value).count(),1);
        assert_ne!(workspace.load(Ordering::Acquire),update.load(Ordering::Acquire));
        workspace.store(false,Ordering::Release);update.store(false,Ordering::Release);
        reserve_maintenance(&gate,&workspace,&update,true).unwrap();
        assert!(reserve_maintenance(&gate,&workspace,&update,false).is_err());
        update.store(false,Ordering::Release);
        reserve_maintenance(&gate,&workspace,&update,false).unwrap();
        assert!(reserve_maintenance(&gate,&workspace,&update,true).is_err());
    }

    #[test]
    fn accepted_update_cancellation_and_installer_handoff_are_mutually_exclusive(){
        for _ in 0..64{
            let manager=super::AppUpdateManager::new();manager.busy.store(true,Ordering::Release);
            *manager.status.lock().unwrap()=serde_json::json!({"state":"downloading","can_cancel":true});
            let start=std::sync::Barrier::new(2);
            let (cancelled,handing_off)=std::thread::scope(|scope|{
                let cancel=scope.spawn(||{start.wait();manager.request_cancel().is_ok()});
                let handoff=scope.spawn(||{start.wait();manager.begin_handoff(serde_json::json!({"state":"handing_off","can_cancel":false})).is_ok()});
                (cancel.join().unwrap(),handoff.join().unwrap())
            });
            assert_ne!(cancelled,handing_off);
            assert_eq!(manager.cancel.load(Ordering::Acquire),cancelled);
            assert_eq!(manager.status.lock().unwrap()["state"]=="handing_off",handing_off);
        }
    }

    #[test]
    fn startup_credentials_are_authorized_only_for_owned_local_windows() {
        let mut context: tauri::Context<tauri::Wry> = tauri::generate_context!();
        let authority = context.runtime_authority_mut(); // authority（实际编译的桌面权限解析器）。
        for command in ["model_credentials_status", "model_credentials_save", "tts_selection_status", "tts_selection_save", "audio_enhancements_settings", "resource_download_required", "app_integrity_status", "app_integrity_download", "app_integrity_check", "app_update_check", "app_update_download", "app_update_cancel", "project_open_repository", "open_minimax_voice_library"] {
            for window in ["main", "startup"] {
                assert!(authority.resolve_access(command, window, window, &tauri::ipc::Origin::Local).is_some(), "启动前密钥命令缺少权限：{command}");
            }
            assert!(authority.resolve_access(command, "foreign", "foreign", &tauri::ipc::Origin::Local).is_none());
            let origin = tauri::ipc::Origin::Remote { url: tauri::Url::parse("http://127.0.0.1:45001/").unwrap() }; // origin（尚未授权的业务页面来源）。
            assert!(authority.resolve_access(command, "main", "main", &origin).is_none());
        }
    }

    #[test]
    fn product_credentials_remain_scoped_to_current_origin_and_preview_cannot_use_them() {
        for mode in [FrontendMode::Product, FrontendMode::UiPreview] {
            let mut context: tauri::Context<tauri::Wry> = tauri::generate_context!();
            let authority = context.runtime_authority_mut();
            authority.add_capability(frontend_capability(mode, "http://127.0.0.1:45001", 7).to_string()).unwrap();
            for command in ["model_credentials_status", "model_credentials_save", "tts_selection_status", "tts_selection_save", "audio_enhancements_settings", "resource_download_required", "app_integrity_status", "app_integrity_download", "app_integrity_check", "app_update_check", "app_update_download", "app_update_cancel", "project_open_repository", "open_minimax_voice_library"] {
                let owned = tauri::ipc::Origin::Remote { url: tauri::Url::parse("http://127.0.0.1:45001/").unwrap() }; // owned（当前实例业务页面）。
                assert_eq!(authority.resolve_access(command, "main", "main", &owned).is_some(), mode == FrontendMode::Product);
                assert!(authority.resolve_access(command, "startup", "startup", &owned).is_none());
                for address in ["http://127.0.0.1:45002/", "https://example.com/"] {
                    let foreign = tauri::ipc::Origin::Remote { url: tauri::Url::parse(address).unwrap() }; // foreign（其他实例或外部页面）。
                    assert!(authority.resolve_access(command, "main", "main", &foreign).is_none());
                }
            }
        }
    }

    #[test]
    fn floating_window_commands_are_scoped_to_their_owned_windows_and_origin() {
        let mut context: tauri::Context<tauri::Wry> = tauri::generate_context!();
        let authority = context.runtime_authority_mut();
        let origin_text = "http://127.0.0.1:45001";
        let origin = tauri::ipc::Origin::Remote {
            url: tauri::Url::parse("http://127.0.0.1:45001/floating.html").unwrap(),
        };
        authority.add_capability(floating_capability(origin_text, 9).to_string()).unwrap();
        authority.add_capability(floating_publisher_capability(origin_text, 9).to_string()).unwrap();

        for command in ["get_floating_model", "return_to_workbench", "start_floating_drag", "control_floating_session"] {
            assert!(authority.resolve_access(command, "floating", "floating", &origin).is_some(), "悬浮窗缺少受限权限：{command}");
            assert!(authority.resolve_access(command, "main", "main", &origin).is_none(), "主窗口不应拥有悬浮窗专属权限：{command}");
            let foreign = tauri::ipc::Origin::Remote { url: tauri::Url::parse("http://127.0.0.1:45002/floating.html").unwrap() };
            assert!(authority.resolve_access(command, "floating", "floating", &foreign).is_none(), "其他来源不应拥有悬浮窗权限：{command}");
        }
        assert!(authority.resolve_access("publish_floating_model", "main", "main", &origin).is_some());
        assert!(authority.resolve_access("publish_floating_model", "floating", "floating", &origin).is_none());
        assert!(authority.resolve_access("exit_application", "floating", "floating", &origin).is_none());
    }

    #[test]
    fn floating_captions_reject_images_and_invalid_text_boundaries() {
        let valid = serde_json::json!({"key":"speech:1","role":"assistant","text":"你😊好","focusStart":1,"focusEnd":3,"active":true});
        assert_eq!(super::floating_caption_projection(Some(&valid)), valid);
        for invalid in [
            serde_json::json!({"key":"image:1","role":"tool","text":"截图","focusStart":0,"focusEnd":2}),
            serde_json::json!({"key":"speech:1","role":"assistant","text":"截图","focusStart":0,"focusEnd":2,"attachments":[]}),
            serde_json::json!({"key":"speech:1","role":"assistant","text":"你好","focusStart":0,"focusEnd":3}),
            serde_json::json!({"key":"speech:1","role":"assistant","text":" ","focusStart":0,"focusEnd":1}),
        ] { assert!(super::floating_caption_projection(Some(&invalid)).is_null()); }
        assert!(super::floating_caption_projection(None).is_null());
    }

    #[test]
    fn services_start_only_once_after_the_final_main_window_page_loaded() {
        for address in [
            "http://tauri.localhost/",
            "tauri://localhost/",
            "http://tauri.localhost/index.html",
            "tauri://localhost/index.html",
        ] {
            let url = tauri::Url::parse(address).unwrap();
            let requested = AtomicBool::new(false);
            let stopping = AtomicBool::new(false);
            assert!(!request_service_start(
                &url,
                PageLoadEvent::Started,
                &requested,
                &stopping
            ));
            assert!(request_service_start(
                &url,
                PageLoadEvent::Finished,
                &requested,
                &stopping
            ));
            assert!(!request_service_start(
                &url,
                PageLoadEvent::Finished,
                &requested,
                &stopping
            ));
        }
    }

    #[test]
    fn closing_main_window_and_remote_pages_never_start_services() {
        let requested = AtomicBool::new(false);
        let stopping = AtomicBool::new(false);
        for address in [
            "http://127.0.0.1:5174/ui-demo.html",
            "http://127.0.0.1:5173/",
            "http://tauri.localhost/other",
        ] {
            assert!(!request_service_start(
                &tauri::Url::parse(address).unwrap(),
                PageLoadEvent::Finished,
                &requested,
                &stopping
            ));
        }
        stopping.store(true, Ordering::SeqCst);
        assert!(!request_service_start(
            &tauri::Url::parse("http://tauri.localhost/index.html").unwrap(),
            PageLoadEvent::Finished,
            &requested,
            &stopping
        ));
        assert!(!requested.load(Ordering::SeqCst));
    }

    #[test]
    fn console_url_preserves_launch_token() {
        let address = "http://127.0.0.1:3081/?token=launch%2Btoken%2Fvalue%3D";
        let url = console_url(address).unwrap();
        assert_eq!(url.as_str(), address);
        assert_eq!(url.query_pairs().next().unwrap().1, "launch+token/value=");
    }

    #[test]
    fn console_url_rejects_untrusted_or_incomplete_addresses() {
        for address in [
            "not a URL",
            "https://127.0.0.1:3081/?token=value",
            "http://example.com:3081/?token=value",
            "http://127.0.0.1:0/?token=value",
            "http://127.0.0.1:3081/other?token=value",
            "http://user:password@127.0.0.1:3081/?token=value",
            "http://127.0.0.1:3081/?token=value#fragment",
            "http://127.0.0.1:3081/",
            "http://127.0.0.1:3081/?token=",
            "http://127.0.0.1:3081/?token=value&token=other",
            "http://127.0.0.1:3081/?token=value&redirect=other",
        ] {
            assert!(console_url(address).is_err(), "应拒绝控制台地址：{address}");
        }
    }

    #[test]
    fn launch_token_and_instance_id_require_url_safe_values() {
        let token = "a".repeat(42) + "_";
        assert!(valid_launch_token(&token));
        let invalid_slash = "a".repeat(42) + "/";
        for token in ["", "short", invalid_slash.as_str()] {
            assert!(!valid_launch_token(token));
        }
        assert!(valid_instance_id("a1b2c3d4-e5f6"));
        assert!(!valid_instance_id("instance id"));
        assert!(!valid_instance_id(""));
    }

    #[test]
    fn frontend_entry_accepts_dynamic_loopback_port_and_encoded_token() {
        let token = "a".repeat(42) + "_";
        let entry =
            frontend_entry_url("http://127.0.0.1:5174/", &token, FrontendMode::UiPreview).unwrap();
        assert_eq!(entry.path(), "/desktop-entry");
        let query: Vec<_> = entry.query_pairs().collect();
        assert_eq!(query.len(), 1);
        assert_eq!(query[0].0, "token");
        assert_eq!(query[0].1, token);
        assert!(frontend_entry_url("http://127.0.0.1:45001/", &token, FrontendMode::UiPreview).is_ok());
        for origin in [
            "http://127.0.0.1:0/",
            "http://localhost:5174/",
            "https://127.0.0.1:5174/",
            "http://example.com:5174/",
        ] {
            assert!(frontend_entry_url(origin, &token, FrontendMode::UiPreview).is_err());
        }
    }

    #[test]
    fn product_and_preview_navigation_allow_only_fixed_local_pages() {
        let product_entry = tauri::Url::parse(
            "http://127.0.0.1:5173/desktop-entry?token=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        .unwrap();
        assert!(product_entry_url_allowed(
            &product_entry,
            Some("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        ));
        assert!(!product_entry_url_allowed(&product_entry, None));

        for address in [
            "http://127.0.0.1:5173/",
            "http://127.0.0.1:5173/index.html?settings=system",
        ] {
            assert!(product_page_url_allowed(
                &tauri::Url::parse(address).unwrap()
            ));
        }
        let floating_page = tauri::Url::parse("http://127.0.0.1:5173/floating.html").unwrap();
        assert!(floating_page_url_allowed(&floating_page));
        assert!(!product_page_url_allowed(&floating_page));
        for address in [
            "http://127.0.0.1:5173/other",
            "http://127.0.0.1:5173/?token=secret",
            "http://127.0.0.1:5173/?settings=system&settings=silence",
        ] {
            assert!(!product_page_url_allowed(
                &tauri::Url::parse(address).unwrap()
            ));
        }

        let preview_entry = tauri::Url::parse(
            "http://127.0.0.1:5174/desktop-entry?token=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        .unwrap();
        assert!(preview_entry_url_allowed(
            &preview_entry,
            Some("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        ));
        for address in [
            "http://127.0.0.1:5174/ui-demo.html?page=wake&scene=error&runtime=speaking&overlay=confirm",
            "http://127.0.0.1:5174/ui-demo.html?replay=voice-shadow",
            "http://127.0.0.1:5174/ui-demo.html?page=wakes&session=active",
        ] {
            assert!(preview_page_url_allowed(&tauri::Url::parse(address).unwrap()));
        }
        for address in [
            "http://127.0.0.1:5174/",
            "http://127.0.0.1:5174/index.html",
            "http://127.0.0.1:5174/ui-demo.html?open_console=1",
            "http://127.0.0.1:5174/ui-demo.html?page=home&page=wake",
            "http://127.0.0.1:5174/ui-demo.html?session=inactive",
            "http://127.0.0.1:5174/ui-demo.html?session=active&session=active",
        ] {
            assert!(!preview_page_url_allowed(
                &tauri::Url::parse(address).unwrap()
            ));
        }
    }

    #[test]
    fn source_product_preview_and_packaged_product_have_separate_instances() {
        let root = std::path::Path::new(r"D:\project\source");
        let other_root = std::path::Path::new(r"D:\project\other");
        let product = instance_identifier(FrontendMode::Product, Some(root));
        let preview = instance_identifier(FrontendMode::UiPreview, Some(root));
        assert_ne!(product, preview);
        assert_ne!(
            preview,
            instance_identifier(FrontendMode::UiPreview, Some(other_root))
        );
        assert_eq!(
            instance_identifier(FrontendMode::Product, None),
            "com.voiceterminal.desktop"
        );
        assert_eq!(
            product,
            instance_identifier(FrontendMode::Product, Some(root))
        );
    }

    #[test]
    fn preview_rejects_product_runtime_arguments_before_reading_root() {
        for args in [
            vec![
                "--ui-preview".into(),
                "--python".into(),
                "python.exe".into(),
            ],
            vec!["--ui-preview".into(), "--data-dir".into(), "data".into()],
            vec!["--ui-preview".into(), "--diagnostic".into()],
        ] {
            assert!(ui_preview_context(&args).is_err());
        }
    }

    #[test]
    fn preview_exposes_only_loading_status_and_exit_commands() {
        for command in [
            DesktopCommand::Status,
            DesktopCommand::SkipStartup,
            DesktopCommand::Exit,
        ] {
            assert!(command_allowed(FrontendMode::UiPreview, command));
            assert!(require_command(FrontendMode::UiPreview, command).is_ok());
            assert!(command_allowed(FrontendMode::Product, command));
        }
        for command in [
            DesktopCommand::ExportLogs,
            DesktopCommand::OpenConsole,
            DesktopCommand::FocusMain,
        ] {
            assert!(!command_allowed(FrontendMode::UiPreview, command));
            assert!(require_command(FrontendMode::UiPreview, command).is_err());
            assert!(command_allowed(FrontendMode::Product, command));
            assert!(require_command(FrontendMode::Product, command).is_ok());
        }
    }
}
