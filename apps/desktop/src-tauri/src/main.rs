#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
use std::{
    fs::{self, File},
    io::{BufRead, BufReader, Write},
    os::windows::{io::AsRawHandle, process::CommandExt},
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
struct Desktop {
    context: LaunchContext,
    service: Mutex<Option<Service>>,
    origin: Mutex<Option<String>>,
    error: Mutex<Option<String>>,
    stopping: AtomicBool,
    stopped: AtomicBool,
    tray: AtomicBool,
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
        && state.origin.lock().unwrap().as_deref()
            == Some(url.origin().ascii_serialization().as_str())
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
    trusted(&window, &state)?;
    Ok(
        serde_json::json!({"ready":state.origin.lock().unwrap().is_some(),"error":*state.error.lock().unwrap()}),
    )
}
#[tauri::command]
fn open_logs(window: WebviewWindow, state: tauri::State<Arc<Desktop>>) -> Result<(), String> {
    trusted(&window, &state)?;
    voice_desktop_common::windows::open(&state.context.data.join("logs"))
}
#[tauri::command]
fn focus_main(window: WebviewWindow, state: tauri::State<Arc<Desktop>>) -> Result<(), String> {
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

fn start_service(app: tauri::AppHandle, state: Arc<Desktop>) -> Result<(), String> {
    let context = &state.context;
    let logs = context.data.join("logs");
    fs::create_dir_all(&logs).map_err(|e| e.to_string())?;
    let output = File::options()
        .append(true)
        .create(true)
        .open(logs.join("desktop-service.log"))
        .map_err(|e| e.to_string())?;
    let mut command = Command::new(&context.python);
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
        .envs(context.environment())
        .creation_flags(0x08000000)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(output);
    if context.dev {
        command.arg("--dev");
    }
    if context.diagnostic {
        command.arg("--diagnostic");
    }
    let job = Job::new()?;
    let mut child = command
        .spawn()
        .map_err(|e| format!("服务管理进程启动失败：{e}"))?;
    if let Err(error) = job.assign(child.as_raw_handle()) {
        let _ = child.kill();
        let _ = child.wait();
        return Err(format!("无法建立进程所有权：{error}"));
    }
    let reader = BufReader::new(child.stdout.take().ok_or("缺少服务控制管道")?);
    let input = child.stdin.take().ok_or("缺少服务控制管道")?;
    *state.service.lock().unwrap() = Some(Service { child, input, job });
    state.send(serde_json::json!({"protocol":1,"kind":"start"}))?;
    state.send(
        serde_json::json!({"protocol":1,"kind":"desktop_process","pid":std::process::id()}),
    )?;
    let read_state = state.clone();
    let read_app = app.clone();
    std::thread::spawn(move || {
        for line in reader.lines() {
            let Ok(line) = line else { break };
            if line.len() > 16384 {
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
                    let Some(origin) = message["origin"].as_str() else {
                        break;
                    };
                    let Ok(url) = origin.parse::<tauri::Url>() else {
                        break;
                    };
                    if url.scheme() != "http"
                        || url.host_str() != Some("127.0.0.1")
                        || url.port().is_none()
                        || url.path() != "/"
                        || !url.username().is_empty()
                        || url.password().is_some()
                        || url.query().is_some()
                        || url.fragment().is_some()
                    {
                        break;
                    }
                    let capability = serde_json::json!({"identifier":"owned-main", "description":"当前实例正式页面的受限桌面操作", "local":false,"windows":["main"],"remote":{"urls":[format!("{origin}/*")]},"permissions":["allow-desktop-status","allow-open-logs","allow-open-console","allow-exit-application","allow-focus-main"]});
                    if let Err(error) = read_app.add_capability(capability.to_string()) {
                        *read_state.error.lock().unwrap() =
                            Some(format!("页面权限配置失败：{error}"));
                        break;
                    }
                    *read_state.origin.lock().unwrap() = Some(url.origin().ascii_serialization());
                    if let Some(window) = read_app.get_webview_window("main") {
                        if let Err(error) = window.navigate(url) {
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
            *read_state.error.lock().unwrap() = Some("服务入口已异常结束，请检查 logs（日志目录）中的 desktop-service.log（服务入口日志）。".into());
            if let Some(window) = read_app.get_webview_window("main") {
                let _ = window.navigate("tauri://localhost/index.html".parse().unwrap());
                let _ = window.show();
            }
            if let Some(service) = read_state.service.lock().unwrap().as_mut() {
                service.job.terminate();
            }
        }
    });
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_secs(30));
        if state.origin.lock().unwrap().is_none() && !state.stopping.load(Ordering::SeqCst) {
            *state.error.lock().unwrap() =
                Some("等待服务入口超时。请检查业务环境与端口占用。".into());
            if let Some(service) = state.service.lock().unwrap().as_mut() {
                service.job.terminate();
            }
        }
    });
    Ok(())
}

fn run() -> Result<(), String> {
    let context = LaunchContext::from_args()?;
    context.prepare_webview_access()?;
    for (name, value) in context.environment() {
        std::env::set_var(name, value);
    }
    let state = Arc::new(Desktop {
        context,
        service: Mutex::new(None),
        origin: Mutex::new(None),
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
                .title("Voice Terminal · 语音终端")
                .inner_size(1380., 860.)
                .min_inner_size(980., 640.)
                .data_directory(build_state.context.data.join("cache/webview2"))
                .on_navigation(move |url| {
                    (url.scheme() == "tauri" && url.host_str() == Some("localhost"))
                        || (url.scheme() == "http"
                            && url.host_str() == Some("tauri.localhost")
                            && url.port().is_none())
                        || navigation_state.origin.lock().unwrap().as_deref()
                            == Some(url.origin().ascii_serialization().as_str())
                })
                .build()?;
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
        .build(tauri::generate_context!())
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
    use super::console_url;

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
}
