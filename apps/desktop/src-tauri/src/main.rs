#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
use std::{
    fs::{self, File},
    io::{BufRead, BufReader, Write},
    os::windows::{io::AsRawHandle, process::CommandExt},
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant},
};
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder,
};
use voice_desktop_common::{windows::Job, LaunchContext};

struct Service {
    child: Child,
    input: ChildStdin,
    job: Job,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum FrontendMode {
    Product,   // Product（正式产品界面）。
    UiPreview, // UI preview（桌面样例界面）。
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum DesktopCommand {
    Status,      // status（桌面状态读取）。
    OpenLogs,    // open logs（打开产品日志）。
    OpenConsole, // open console（打开产品控制台）。
    Exit,        // exit（退出当前桌面实例）。
    FocusMain,   // focus main（聚焦产品主窗口）。
}

fn command_allowed(mode: FrontendMode, command: DesktopCommand) -> bool {
    match command {
        DesktopCommand::Status | DesktopCommand::Exit => true,
        DesktopCommand::OpenLogs | DesktopCommand::OpenConsole | DesktopCommand::FocusMain => {
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
        DesktopCommand::OpenLogs => "产品日志",
        DesktopCommand::OpenConsole => "产品控制台",
        DesktopCommand::Exit => "退出应用",
        DesktopCommand::FocusMain => "产品主窗口",
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

struct Desktop {
    context: Option<LaunchContext>, // context（产品模式的业务启动上下文）；预览模式不读取。
    program: PathBuf,               // program（当前前端项目根目录）。
    data: PathBuf,                  // data（当前模式隔离的 WebView2 缓存目录）。
    webview2: Option<PathBuf>,      // webview2（可选的固定网页运行时）。
    mode: FrontendMode,             // mode（前端模式：产品或桌面样例）。
    development: bool,              // development（是否由调试构建运行）。
    instance_identifier: String,    // instance_identifier（按模式和源码根目录隔离的单实例标识）。
    service: Mutex<Option<Service>>,
    origin: Mutex<Option<String>>,
    launch_token: Mutex<Option<String>>, // launch_token（当前入口的一次性启动凭据）。
    error: Mutex<Option<String>>,
    stopping: AtomicBool,
    stopped: AtomicBool,
    tray: AtomicBool,
}

impl Desktop {
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
        *self.origin.lock().unwrap() = None;
        let state = self.clone();
        let app = app.clone();
        std::thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(15);
            let _ =
                state.send(serde_json::json!({"protocol":1,"kind":"shutdown","budget_ms":14000}));
            while Instant::now() < deadline {
                let exited = state
                    .service
                    .lock()
                    .unwrap()
                    .as_mut()
                    .map(|s| s.child.try_wait().ok().flatten().is_some())
                    .unwrap_or(true);
                if exited {
                    break;
                }
                std::thread::sleep(Duration::from_millis(80));
            }
            if let Some(mut service) = state.service.lock().unwrap().take() {
                service.job.terminate();
                let _ = service.child.kill();
            }
            state.stopped.store(true, Ordering::SeqCst);
            app.exit(0);
        });
    }
}

fn trusted(window: &WebviewWindow, state: &Desktop) -> Result<(), String> {
    if window.label() != "main" {
        return Err("此窗口没有桌面操作权限。".into());
    }
    let url = window.url().map_err(|e| e.to_string())?;
    if (url.scheme() == "tauri" && url.host_str() == Some("localhost"))
        || (url.scheme() == "http"
            && url.host_str() == Some("tauri.localhost")
            && url.port().is_none())
    {
        return Ok(());
    }
    if !state.stopping.load(Ordering::SeqCst)
        && state.error.lock().unwrap().is_none()
        && state.allows_frontend_url(&url)
    {
        Ok(())
    } else {
        Err("页面地址不属于当前应用实例。".into())
    }
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
    // ready（是否就绪）、error（错误信息）、frontend_mode（前端模式）、development（是否调试构建）。
    Ok(serde_json::json!({
        "ready": state.origin.lock().unwrap().is_some(),
        "error": *state.error.lock().unwrap(),
        "frontend_mode": state.mode.label(),
        "development": state.development
    }))
}
#[tauri::command]
fn open_logs(window: WebviewWindow, state: tauri::State<Arc<Desktop>>) -> Result<(), String> {
    require_command(state.mode, DesktopCommand::OpenLogs)?;
    trusted(&window, &state)?;
    let context = state.context.as_ref().ok_or("桌面样例没有产品日志目录。")?;
    voice_desktop_common::windows::open(&context.data.join("logs"))
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
    // 通过系统默认浏览器打开 DSH（代理执行宿主）网页，使用完整令牌地址登录。
    voice_desktop_common::windows::open_url(url.as_str())
}

// console_url（控制台地址校验）：限制桌面入口只能打开当前本机代理控制台。
fn console_url(value: &str) -> Result<tauri::Url, String> {
    let url: tauri::Url = value.parse().map_err(|_| "控制台地址无效")?;
    // 服务返回的短期令牌地址仅允许固定的本机 DSH（代理执行宿主）入口。
    let query: Vec<_> = url.query_pairs().collect();
    if url.scheme() != "http"
        || url.host_str() != Some("127.0.0.1")
        || url.port() != Some(3081)
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
fn show(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
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

fn preview_entry_url_allowed(url: &tauri::Url, token: Option<&str>) -> bool {
    entry_url_allowed(url, token)
}

fn preview_page_url_allowed(url: &tauri::Url) -> bool {
    page_url_allowed(
        url,
        "/ui-demo.html",
        &["page", "scene", "runtime", "overlay", "replay"],
    )
}

fn frontend_entry_url(origin: &str, token: &str, mode: FrontendMode) -> Result<tauri::Url, String> {
    if !valid_launch_token(token) {
        return Err("服务入口启动凭据无效。".into());
    }
    let mut url: tauri::Url = origin
        .parse()
        .map_err(|_| "服务入口地址无效。".to_string())?;
    if url.scheme() != "http"
        || url.host_str() != Some("127.0.0.1")
        || url.port().is_none()
        || url.path() != "/"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || (mode == FrontendMode::UiPreview && url.port() != Some(5174))
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
            && !["--ui-preview", "--root", "--webview2"].contains(&arg.as_str())
        {
            return Err(format!("桌面样例不接受产品启动参数：{arg}"));
        }
    }
    let root = arg_value(args, "--root")?.ok_or("桌面样例必须指定 --root（项目根目录）。")?;
    let program = voice_desktop_common::canonical_path(Path::new(root))?;
    let data = program.join(".runtime/desktop-ui-preview");
    fs::create_dir_all(&data).map_err(|e| format!("桌面样例缓存目录不可用：{e}"))?;
    let data = voice_desktop_common::canonical_path(&data)?;
    if !data.starts_with(&program) {
        return Err("桌面样例缓存必须位于独立的项目运行目录。".into());
    }
    let webview2 = arg_value(args, "--webview2")?
        .map(|path| voice_desktop_common::canonical_path(&program.join(path)))
        .transpose()?;
    if let Some(engine) = &webview2 {
        if !engine.join("msedgewebview2.exe").is_file() {
            return Err("指定的 WebView2（网页运行时）缺少 msedgewebview2.exe。".into());
        }
    }
    Ok((program, webview2, data))
}

fn start_service(app: tauri::AppHandle, state: Arc<Desktop>) -> Result<(), String> {
    let logs = if let Some(context) = &state.context {
        context.data.join("logs")
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
        command = Command::new("node");
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
        let context = state
            .context
            .as_ref()
            .ok_or("产品服务缺少桌面启动上下文。")?;
        command = Command::new(&context.python);
        command
            .args(["-X", "utf8", "-X"])
            .arg(format!(
                "pycache_prefix={}",
                context.data.join("cache/python").display()
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
            .current_dir(&context.program)
            .envs(context.environment());
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
    *state.service.lock().unwrap() = Some(Service { child, input, job });
    // protocol（私有协议版本）、kind（消息类型）；Node（脚本运行时）与 Python（后端语言）均接收 start（启动）。
    state.send(serde_json::json!({"protocol":1,"kind":"start"}))?;
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
                    let capability = if read_state.mode == FrontendMode::UiPreview {
                        serde_json::json!({"identifier":"owned-ui-preview", "description":"桌面样例的状态读取和退出操作", "local":false,"windows":["main"],"remote":{"urls":[format!("{normalized_origin}/*")]},"permissions":["allow-desktop-status","allow-exit-application"]})
                    } else {
                        serde_json::json!({"identifier":"owned-main", "description":"当前实例产品页面的受限桌面操作", "local":false,"windows":["main"],"remote":{"urls":[format!("{normalized_origin}/*")]},"permissions":["allow-desktop-status","allow-open-logs","allow-open-console","allow-exit-application","allow-focus-main"]})
                    };
                    if let Err(error) = read_app.add_capability(capability.to_string()) {
                        *read_state.error.lock().unwrap() =
                            Some(format!("页面权限配置失败：{error}"));
                        break;
                    }
                    *read_state.launch_token.lock().unwrap() = Some(token.into());
                    *read_state.origin.lock().unwrap() = Some(normalized_origin);
                    if let Some(window) = read_app.get_webview_window("main") {
                        if let Err(error) = window.navigate(entry) {
                            *read_state.error.lock().unwrap() = Some(error.to_string());
                        }
                    }
                }
                Some("exit_requested") => read_state.shutdown(&read_app),
                Some("stopped") => {}
                _ => {}
            }
        }
        if !read_state.stopping.load(Ordering::SeqCst) {
            *read_state.origin.lock().unwrap() = None;
            *read_state.launch_token.lock().unwrap() = None;
            read_state.error.lock().unwrap().get_or_insert_with(|| {
                if read_state.mode == FrontendMode::UiPreview {
                    "桌面样例启动服务已异常结束，请检查独立缓存目录中的 frontend-dev.log（样例服务日志）。".into()
                } else {
                    "服务入口已异常结束，请检查 logs（日志目录）中的 desktop-service.log（服务入口日志）。".into()
                }
            });
            if let Some(window) = read_app.get_webview_window("main") {
                let _ = window.navigate("tauri://localhost/index.html".parse().unwrap());
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
        if timeout_state.origin.lock().unwrap().is_none()
            && !timeout_state.stopping.load(Ordering::SeqCst)
        {
            timeout_state.error.lock().unwrap().get_or_insert_with(|| {
                if timeout_state.mode == FrontendMode::UiPreview {
                    "等待桌面样例页面超时，请检查 5174 端口及 Node（脚本运行时）服务。".into()
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
    if !cfg!(debug_assertions)
        && args
            .iter()
            .any(|arg| ["--ui-preview", "--dev", "--diagnostic"].contains(&arg.as_str()))
    {
        return Err(
            "正式桌面程序不接受 --ui-preview、--dev 或 --diagnostic（调试启动参数）。".into(),
        );
    }
    let (context, mode, program, data, webview2) = if preview {
        let (program, webview2, data) = ui_preview_context(&args)?;
        (None, FrontendMode::UiPreview, program, data, webview2)
    } else {
        let context = LaunchContext::from_args()?;
        context.prepare_webview_access()?;
        for (name, value) in context.environment() {
            std::env::set_var(name, value);
        }
        let program = context.program.clone();
        let data = context
            .data
            .join(if args.iter().any(|arg| arg == "--root") {
                "cache/webview2-source"
            } else {
                "cache/webview2"
            });
        let webview2 = context.webview2.clone();
        (
            Some(context),
            FrontendMode::Product,
            program,
            data,
            webview2,
        )
    };
    if preview {
        voice_desktop_common::prepare_webview_access_paths(&data, webview2.as_deref())?;
    }
    if let Some(engine) = &webview2 {
        std::env::set_var("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", engine);
    }
    let source_root = if preview || args.iter().any(|arg| arg == "--root") {
        Some(program.as_path())
    } else {
        None
    };
    let instance_identifier = instance_identifier(mode, source_root);
    let state = Arc::new(Desktop {
        context,
        program,
        data,
        webview2,
        mode,
        development: cfg!(debug_assertions),
        instance_identifier,
        service: Mutex::new(None),
        origin: Mutex::new(None),
        launch_token: Mutex::new(None),
        error: Mutex::new(None),
        stopping: AtomicBool::new(false),
        stopped: AtomicBool::new(false),
        tray: AtomicBool::new(false),
    });
    let build_state = state.clone();
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| show(app)))
        .manage(state.clone())
        .invoke_handler(tauri::generate_handler![
            desktop_status,
            open_logs,
            open_console,
            exit_application,
            focus_main
        ])
        .setup(move |app| {
            let navigation_state = build_state.clone();
            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title(if build_state.mode == FrontendMode::UiPreview {
                    "Voice Terminal · 前端演示（无业务后端）"
                } else {
                    "Voice Terminal · 语音终端"
                })
                .inner_size(1380., 860.)
                .min_inner_size(980., 640.)
                .data_directory(build_state.data.clone())
                .on_navigation(move |url| {
                    (url.scheme() == "tauri" && url.host_str() == Some("localhost"))
                        || (url.scheme() == "http"
                            && url.host_str() == Some("tauri.localhost")
                            && url.port().is_none())
                        || navigation_state.allows_frontend_url(url)
                })
                .build()?;
            if build_state.mode == FrontendMode::Product {
                let show_item = MenuItem::with_id(app, "show", "打开工作台", true, None::<&str>)?;
                let exit_item = MenuItem::with_id(app, "exit", "退出应用", true, None::<&str>)?;
                let menu = Menu::with_items(app, &[&show_item, &exit_item])?;
                let pixels: Vec<u8> = (0..32 * 32)
                    .flat_map(|i| {
                        let x = i % 32;
                        let y = i / 32;
                        if (x as i32 - 16).pow(2) + (y as i32 - 16).pow(2) < 180 {
                            [178, 186, 255, 255]
                        } else {
                            [0, 0, 0, 0]
                        }
                    })
                    .collect();
                let exit_state = build_state.clone();
                let tray = TrayIconBuilder::new()
                    .icon(tauri::image::Image::new_owned(pixels, 32, 32))
                    .tooltip("Voice Terminal · 语音终端")
                    .menu(&menu)
                    .on_menu_event(move |app, event| match event.id.as_ref() {
                        "show" => show(app),
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
            if let Err(error) = start_service(app.handle().clone(), build_state.clone()) {
                *build_state.error.lock().unwrap() = Some(error);
            }
            Ok(())
        })
        .on_window_event(|window, event| {
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
                }
            }
        })
        .build({
            let mut context = tauri::generate_context!();
            context.config_mut().identifier = state.instance_identifier.clone();
            context
        })
        .map_err(|e| format!("桌面运行时不可用：{e}。请检查 WebView2（网页运行时）配置。"))?;
    app.run(move |app, event| {
        if let tauri::RunEvent::ExitRequested { api, .. } = event {
            if !state.stopped.load(Ordering::SeqCst) {
                api.prevent_exit();
                state.shutdown(app);
            }
        }
    });
    Ok(())
}
fn main() {
    if let Err(message) = run() {
        voice_desktop_common::windows::error(&message);
    }
}

#[cfg(test)]
mod tests {
    use super::{
        command_allowed, console_url, frontend_entry_url, instance_identifier,
        preview_entry_url_allowed, preview_page_url_allowed, product_entry_url_allowed,
        product_page_url_allowed, require_command, ui_preview_context, valid_instance_id,
        valid_launch_token, DesktopCommand, FrontendMode,
    };

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
            "http://127.0.0.1:5173/?token=value",
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
    fn frontend_entry_uses_fixed_preview_origin_and_encoded_token() {
        let token = "a".repeat(42) + "_";
        let entry =
            frontend_entry_url("http://127.0.0.1:5174/", &token, FrontendMode::UiPreview).unwrap();
        assert_eq!(entry.path(), "/desktop-entry");
        let query: Vec<_> = entry.query_pairs().collect();
        assert_eq!(query.len(), 1);
        assert_eq!(query[0].0, "token");
        assert_eq!(query[0].1, token);
        for origin in [
            "http://127.0.0.1:5173/",
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
        ] {
            assert!(preview_page_url_allowed(&tauri::Url::parse(address).unwrap()));
        }
        for address in [
            "http://127.0.0.1:5174/",
            "http://127.0.0.1:5174/index.html",
            "http://127.0.0.1:5174/ui-demo.html?open_console=1",
            "http://127.0.0.1:5174/ui-demo.html?page=home&page=wake",
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
    fn preview_exposes_only_status_and_exit_commands() {
        for command in [DesktopCommand::Status, DesktopCommand::Exit] {
            assert!(command_allowed(FrontendMode::UiPreview, command));
            assert!(require_command(FrontendMode::UiPreview, command).is_ok());
        }
        for command in [
            DesktopCommand::OpenLogs,
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
