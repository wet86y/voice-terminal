//! 悬浮窗原生控制器：正式窗口与独立测试台共用定位、动画与展示状态。
use crate::floating_geometry::{
    Bounds, Dock, Placement, Point, WorkArea, DISPLAY_HEIGHT, DISPLAY_WIDTH, ORB_HEIGHT, ORB_WIDTH,
};
use crate::floating_motion::{window_bounds, DisplayClip, Motion, WindowMotion};
use crate::{agent_debug, floating_double_click_ms, set_floating_menu_checked, Desktop};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::sync::{atomic::Ordering, Arc, Mutex};
use tauri::{
    AppHandle, Emitter, Manager, Monitor, WebviewUrl, WebviewWindow, WebviewWindowBuilder,
};

pub const PAGE: &str = "/floating-preview.html"; // PAGE（独立测试界面）。
const ORB: &str = "preview-orb"; // ORB（测试悬浮球窗口）。
const DISPLAY: &str = "preview-display"; // DISPLAY（测试展示窗口）。
const EVENT: &str = "voice-terminal:floating-preview"; // EVENT（本机测试状态快照）。
pub const PRODUCT_ORB: &str = "floating"; // PRODUCT_ORB（正式悬浮球窗口）。
pub const PRODUCT_DISPLAY: &str = "floating-display"; // PRODUCT_DISPLAY（正式图文窗口）。
const PRODUCT_EVENT: &str = "voice-terminal:floating-layout"; // PRODUCT_EVENT（正式悬浮布局快照）。
const MODE_EVENT: &str = "voice-terminal:floating-mode"; // MODE_EVENT（正式窗口模式）。
const RESULT_EVENT: &str = "voice-terminal:floating-display-result"; // RESULT_EVENT（正式展示回执）。

#[derive(Clone, Copy, Default, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum VoiceState {
    #[default]
    Standby,
    Listening,
    Thinking,
    Speaking,
}

#[derive(Serialize)]
struct DisplayRequest {
    id: u64,              // id（当前展示事务编号）。
    content: String,      // content（固定排版图文正文）。
    duration_seconds: u8, // duration_seconds（初始展示秒数）。
    #[serde(skip)]
    delivery: Option<u64>, // delivery（语音核心分配的投递序号）。
}

#[derive(Clone, Copy, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum DisplayStatus {
    Visible,
    Rejected,
    Expired,
    Cleared,
}

#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Action {
    Attach,
    Snapshot,
    ModeStatus,
    InvalidateDisplay,
    Submit {
        generation: u64,              // generation（正式浮窗模式代次）。
        delivery: u64,                // delivery（语音投递序号）。
        content: Option<String>,      // content（空值代表取消展示）。
        duration_seconds: Option<u8>, // duration_seconds（展示时长）。
    },
    Ready,
    Mode {
        enabled: bool,
    },
    Place {
        dock: Dock,
    },
    Voice {
        state: VoiceState,
        caption: String,
    },
    Show {
        content: String,
        duration_seconds: u8,
    },
    Clear,
    Return,
    Toggle,
    Drag {
        x: f64, // x（窗口内按下横坐标，逻辑像素）。
        y: f64, // y（窗口内按下纵坐标，逻辑像素）。
    },
    Hover {
        id: u64,
        focused: bool,
    },
    DisplayStatus {
        id: u64,
        status: DisplayStatus,
        reason: Option<String>,
    },
}

#[derive(Default)]
struct Session {
    revision: u64,                   // revision（快照递增序号）。
    active: bool,                    // active（已进入悬浮测试模式）。
    hide_console: bool,              // hide_console（窗口就绪后隐藏测试台）。
    mode_generation: u64,            // mode_generation（窗口模式代次）。
    presented: bool,                 // presented（悬浮球已实际显示）。
    display_ready: bool,             // display_ready（图文页面已订阅）。
    visible_delivery: Option<u64>,   // visible_delivery（已显示卡片的语音投递序号）。
    last_delivery: u64,              // last_delivery（本代次最新受理投递序号）。
    orb_ready: bool,                 // orb_ready（悬浮球已订阅状态）。
    place: Option<Placement>,        // place（当前吸附边与物理位置）。
    drag: Option<u64>,               // drag（当前拖动代次）。
    voice: VoiceState,               // voice（模拟语音状态）。
    caption: String,                 // caption（模拟识别或播报字幕）。
    caption_revision: u64,           // caption_revision（字幕测试轮次）。
    request: Option<DisplayRequest>, // request（最近一次展示提交）。
    visible: Option<u64>,            // visible（已通过排版校验的活动展示编号）。
    focused: bool,                   // focused（展示框处于悬停关注）。
    notice: String,                  // notice（最近一次展示校验结果）。
}

impl Session {
    fn snapshot(&self) -> Value {
        let size = self.caption.encode_utf16().count();
        let start = self
            .caption
            .char_indices()
            .rev()
            .take(6)
            .last()
            .map_or(0, |(offset, _)| {
                self.caption[..offset].encode_utf16().count()
            });
        let role = if matches!(self.voice, VoiceState::Speaking) {
            "assistant"
        } else {
            "user"
        };
        let caption = (!self.caption.is_empty()).then(|| {
            json!({
                "key": format!("preview-{}", self.caption_revision), "role": role,
                "text": self.caption, "focusStart": start, "focusEnd": size, "active": true,
            })
        });
        let listening = !matches!(self.voice, VoiceState::Standby);
        let levels = match self.voice {
            VoiceState::Listening | VoiceState::Speaking => [0.35, 0.6, 0.9, 0.62, 0.44],
            _ => [0.; 5],
        };
        let display_focus = self.visible.map(|_| self.focused);
        json!({
            "revision":self.revision, "active":self.active, "dock":self.place.map_or(Dock::Free, |place| place.dock),
            "request":self.request, "visible":self.visible, "focused":self.focused, "notice":self.notice,
            "orb_frame":self.place.and_then(|place| place.orb_frame(display_focus)),
            "display_frame":self.place.map(|place| place.display_surface().frame(self.focused)),
            "model":{"revision":self.revision,"doubleClickMs":floating_double_click_ms(),
                "state":self.voice,"sessionActive":listening,"silenced":!listening,
                "canControl":true,"audioLevels":levels,"caption":caption},
        })
    }
}

#[derive(Default)]
pub struct FloatingWindows {
    product: bool,                    // product（使用正式窗口和权限）。
    session: Mutex<Session>,          // session（独立窗口与展示状态）。
    creating: tokio::sync::Mutex<()>, // creating（子窗口创建顺序）。
    orb_motion: WindowMotion,         // orb_motion（菱形窗口的位置与裁切过渡）。
    display_motion: WindowMotion,     // display_motion（图文画布随悬浮球定位时的过渡）。
    display_clip: DisplayClip,        // display_clip（图文卡片可见形状和输入区域）。
}

pub fn page_allowed(url: &tauri::Url, label: &str) -> bool {
    let query = match label {
        "main" => None,
        ORB => Some("view=orb"),
        DISPLAY => Some("view=display"),
        _ => return false,
    };
    url.path() == PAGE
        && url.query() == query
        && url.fragment().is_none()
        && url.username().is_empty()
        && url.password().is_none()
}

pub fn capability(origin: &str) -> Value {
    json!({
        "identifier":"owned-floating-preview", "description":"当前隔离实例的悬浮窗测试操作",
        "local":false, "windows":["main",ORB,DISPLAY], "remote":{"urls":[format!("{origin}/*")]},
        "permissions":["allow-floating-preview-command","allow-agent-debug-trace-native",
            "core:event:allow-listen","core:event:allow-unlisten"],
    })
}

pub fn trusted(window: &WebviewWindow, desktop: &Desktop) -> Result<(), String> {
    let url = window.url().map_err(|error| error.to_string())?;
    if desktop.floating_preview
        && !desktop.stopping.load(std::sync::atomic::Ordering::Acquire)
        && page_allowed(&url, window.label())
        && desktop.origin.lock().unwrap().as_deref()
            == Some(url.origin().ascii_serialization().as_str())
    {
        return Ok(());
    }
    Err("当前窗口没有悬浮窗测试权限。".into())
}

fn area(monitor: Monitor) -> WorkArea {
    let bounds = monitor.work_area();
    WorkArea {
        scale: monitor.scale_factor(),
        bounds: Bounds {
            origin: Point {
                x: f64::from(bounds.position.x),
                y: f64::from(bounds.position.y),
            },
            width: f64::from(bounds.size.width),
            height: f64::from(bounds.size.height),
        },
    }
}

fn monitor_at(window: &WebviewWindow, point: Point) -> Option<WorkArea> {
    window
        .monitor_from_point(point.x, point.y)
        .ok()
        .flatten()
        .or_else(|| window.current_monitor().ok().flatten())
        .or_else(|| window.primary_monitor().ok().flatten())
        .map(area)
}

impl FloatingWindows {
    pub fn product() -> Self {
        Self {
            product: true,
            ..Self::default()
        }
    }

    fn orb(&self) -> &'static str {
        if self.product {
            PRODUCT_ORB
        } else {
            ORB
        }
    }
    fn display(&self) -> &'static str {
        if self.product {
            PRODUCT_DISPLAY
        } else {
            DISPLAY
        }
    }

    fn mode_snapshot(&self) -> Value {
        let session = self.session.lock().unwrap();
        json!({"enabled":session.presented,"generation":session.mode_generation})
    }

    fn emit_mode(&self, app: &AppHandle) {
        if self.product {
            let _ = app.emit_to("main", MODE_EVENT, self.mode_snapshot());
        }
    }

    fn emit_result(
        &self,
        app: &AppHandle,
        delivery: Option<u64>,
        status: DisplayStatus,
        reason: Option<&str>,
    ) {
        if let Some(delivery) = delivery {
            let generation = self.session.lock().unwrap().mode_generation;
            let _ = app.emit_to(
                "main",
                RESULT_EVENT,
                json!({
                    "generation":generation,"delivery":delivery,"status":status,"reason":reason,
                }),
            );
        }
    }

    pub fn load_started(&self, app: &AppHandle, label: &str) {
        if !self.product {
            return;
        }
        if label == self.orb() {
            let presented = {
                let mut session = self.session.lock().unwrap();
                session.orb_ready = false;
                let presented = session.presented;
                if presented {
                    session.presented = false;
                    session.mode_generation += 1;
                    session.last_delivery = 0;
                }
                presented
            };
            if presented {
                self.conceal(app);
                self.clear(app);
                if let Some(main) = app.get_webview_window("main") {
                    let _ = main.show();
                }
                self.emit_mode(app);
            }
        } else if label == self.display() {
            let was_ready = {
                let mut session = self.session.lock().unwrap();
                std::mem::take(&mut session.display_ready)
            };
            if was_ready {
                self.clear(app);
            }
        }
    }

    pub fn prepare_product(
        self: &Arc<Self>,
        app: &AppHandle,
        desktop: &Arc<Desktop>,
    ) -> Result<(), String> {
        self.mode(app, desktop, true, true)?;
        if app.get_webview_window(self.orb()).is_some()
            && app.get_webview_window(self.display()).is_some()
        {
            return Ok(());
        }
        let generation = self.session.lock().unwrap().mode_generation;
        let windows = self.clone();
        let app = app.clone();
        let desktop = desktop.clone();
        tauri::async_runtime::spawn(async move {
            let attached = windows.attach(&app, &desktop).await;
            let reply_app = app.clone();
            let _ = app.run_on_main_thread(move || {
                if windows.session.lock().unwrap().mode_generation != generation
                    || desktop.stopping.load(Ordering::Acquire)
                {
                    return;
                }
                if let Err(error) = attached.and_then(|()| windows.present_orb(&reply_app)) {
                    let _ = windows.mode(&reply_app, &desktop, false, false);
                    let _ = reply_app.emit_to("main", "voice-terminal:floating-error", error);
                }
            });
        });
        Ok(())
    }

    fn publish(&self, app: &AppHandle) -> Value {
        if self.product {
            self.publish_to(app, &[self.orb(), self.display()])
        } else {
            self.publish_to(app, &["main", ORB, DISPLAY])
        }
    }

    fn publish_to(&self, app: &AppHandle, labels: &[&str]) -> Value {
        let snapshot = {
            let mut session = self.session.lock().unwrap();
            session.revision += 1;
            session.snapshot()
        };
        for &label in labels {
            if self.product {
                let _ = app.emit_to(label, PRODUCT_EVENT, product_snapshot(&snapshot, label));
            } else {
                let _ = app.emit_to(label, EVENT, &snapshot);
            }
        }
        snapshot
    }

    async fn attach(&self, app: &AppHandle, desktop: &Desktop) -> Result<(), String> {
        let _guard = self.creating.lock().await;
        let origin = desktop
            .origin
            .lock()
            .unwrap()
            .clone()
            .ok_or("测试界面正在准备。")?;
        for (label, view, title, width, height) in [
            (
                self.orb(),
                "orb",
                if self.product {
                    "语音终端 · 悬浮窗"
                } else {
                    "悬浮球测试"
                },
                ORB_WIDTH,
                ORB_HEIGHT,
            ),
            (
                self.display(),
                "display",
                if self.product {
                    "语音终端 · 图文展示"
                } else {
                    "图文展示测试"
                },
                DISPLAY_WIDTH,
                DISPLAY_HEIGHT,
            ),
        ] {
            if app.get_webview_window(label).is_some() {
                continue;
            }
            let path = match (self.product, view) {
                (true, "orb") => "/floating.html".to_owned(),
                (true, _) => "/floating.html?view=display".to_owned(),
                (false, _) => format!("{PAGE}?view={view}"),
            };
            let url = format!("{origin}{path}")
                .parse::<tauri::Url>()
                .map_err(|error| error.to_string())?;
            let trusted_origin = origin.clone();
            let product = self.product;
            let navigation_state = app.state::<Arc<Desktop>>().inner().clone();
            let directory = if product {
                desktop
                    .context()
                    .map(|context| context.install.join("cache/webview2"))
                    .unwrap_or_else(|| desktop.data.clone())
            } else {
                desktop.data.clone()
            };
            WebviewWindowBuilder::new(app, label, WebviewUrl::External(url))
                .title(title)
                .inner_size(width, height)
                .resizable(false)
                .decorations(false)
                .transparent(true)
                .background_color(tauri::window::Color(0, 0, 0, 0))
                .always_on_top(true)
                .skip_taskbar(true)
                .shadow(false)
                .visible(false)
                .focused(false)
                .focusable(label == self.orb())
                .data_directory(directory)
                .initialization_script(voice_desktop_common::webview::DISABLE_CONTEXT_MENU)
                .initialization_script(agent_debug::AgentDebug::window_script(app))
                .on_navigation(move |url| {
                    if product {
                        navigation_state
                            .recovery
                            .lock()
                            .unwrap()
                            .allows_navigation()
                            && navigation_state.allows_floating_url(url, label)
                    } else {
                        url.origin().ascii_serialization() == trusted_origin
                            && page_allowed(url, label)
                    }
                })
                .on_page_load(move |window, payload| {
                    if product && payload.event() == tauri::webview::PageLoadEvent::Started {
                        if let Some(windows) = window.try_state::<Arc<FloatingWindows>>() {
                            windows.load_started(window.app_handle(), label);
                        }
                    }
                })
                .build()
                .map_err(|error| error.to_string())?;
        }
        Ok(())
    }

    fn update_bounds(&self, app: &AppHandle, move_orb: bool, motion: Motion) -> Result<(), String> {
        let (place, visible, focused) = {
            let session = self.session.lock().unwrap();
            (session.place, session.visible.is_some(), session.focused)
        };
        let Some(place) = place else {
            return Ok(());
        };
        if move_orb {
            if let Some(window) = app.get_webview_window(self.orb()) {
                self.orb_motion
                    .apply(&window, place.orb_bounds(), motion, false)?;
            }
        }
        if visible {
            if let Some(window) = app.get_webview_window(self.display()) {
                let surface = place.display_surface();
                let animate_clip = motion == Motion::Animate
                    || (motion == Motion::Follow && self.display_motion.active());
                self.display_motion
                    .apply(&window, surface.canvas, motion, true)?;
                self.display_clip
                    .apply(&window, surface.region(focused), animate_clip)?;
            }
        }
        Ok(())
    }

    fn place(&self, app: &AppHandle, dock: Dock) -> Result<(), String> {
        let window = app.get_webview_window("main").ok_or("测试台尚未就绪。")?;
        let existing = self.session.lock().unwrap().place;
        let monitor = window
            .current_monitor()
            .ok()
            .flatten()
            .or_else(|| window.primary_monitor().ok().flatten())
            .ok_or("当前没有可用的显示器。")?;
        let area = area(monitor);
        let anchor = existing
            .filter(|place| place.area == area)
            .map_or_else(|| area.bounds.center(), |place| place.anchor);
        let mut session = self.session.lock().unwrap();
        session.place = Some(Placement::new(anchor, dock, area));
        session.drag = None;
        drop(session);
        self.publish(app);
        self.update_bounds(app, true, Motion::Animate)?;
        Ok(())
    }

    fn present_orb(&self, app: &AppHandle) -> Result<(), String> {
        let (ready, active, hide, placed) = {
            let session = self.session.lock().unwrap();
            (
                session.orb_ready,
                session.active,
                session.hide_console,
                session.place.is_some(),
            )
        };
        if !ready || !active || (self.product && app.get_webview_window(self.display()).is_none()) {
            return Ok(());
        }
        if !placed {
            self.place(app, Dock::Free)?;
        }
        if let Some(window) = app.get_webview_window(self.orb()) {
            self.update_bounds(app, true, Motion::Instant)?;
            window.show().map_err(|error| error.to_string())?;
            if hide {
                if let Some(main) = app.get_webview_window("main") {
                    if let Err(error) = main.hide() {
                        let _ = window.hide();
                        return Err(error.to_string());
                    }
                }
            }
            let changed = {
                let mut session = self.session.lock().unwrap();
                !std::mem::replace(&mut session.presented, true)
            };
            if changed {
                self.emit_mode(app);
            }
        }
        Ok(())
    }

    pub fn toggle_mode(&self, app: &AppHandle, desktop: &Desktop) -> Result<(), String> {
        let enabled = !self.session.lock().unwrap().active;
        self.mode(app, desktop, enabled, true)
    }

    pub fn mode(
        &self,
        app: &AppHandle,
        desktop: &Desktop,
        enabled: bool,
        hide: bool,
    ) -> Result<(), String> {
        if enabled && !self.product && app.get_webview_window(self.orb()).is_none() {
            return Err("悬浮窗测试正在准备。".into());
        }
        {
            let mut session = self.session.lock().unwrap();
            if session.active != enabled {
                session.mode_generation += 1;
                session.last_delivery = 0;
            }
            session.active = enabled;
            if !enabled {
                session.presented = false;
            }
            session.hide_console = hide;
            if !enabled {
                session.drag = None;
            }
        }
        if self.product {
            desktop.floating_mode.store(enabled, Ordering::Release);
        }
        set_floating_menu_checked(desktop, enabled);
        if enabled {
            if let Err(error) = self.present_orb(app) {
                let _ = self.mode(app, desktop, false, false);
                return Err(error);
            }
        } else {
            self.orb_motion.cancel();
            self.clear(app);
            self.emit_mode(app);
            if let Some(window) = app.get_webview_window(self.orb()) {
                let _ = window.hide();
            }
            if let Some(main) = app.get_webview_window("main") {
                let _ = main.show();
                let _ = main.unminimize();
                let _ = main.set_focus();
            }
        }
        self.publish(app);
        Ok(())
    }

    fn clear(&self, app: &AppHandle) {
        self.display_motion.cancel();
        self.display_clip.cancel();
        let mut session = self.session.lock().unwrap();
        session.request = None;
        session.visible = None;
        session.visible_delivery = None;
        session.focused = false;
        session.notice.clear();
        drop(session);
        if let Some(window) = app.get_webview_window(self.display()) {
            let _ = window.hide();
        }
        self.publish(app);
    }

    pub fn conceal(&self, app: &AppHandle) {
        self.session.lock().unwrap().drag = None;
        self.orb_motion.cancel();
        self.display_motion.cancel();
        self.display_clip.cancel();
        for label in [self.orb(), self.display()] {
            if let Some(window) = app.get_webview_window(label) {
                let _ = window.hide();
            }
        }
    }

    pub fn refresh_monitor(&self, app: &AppHandle, label: &str) {
        if label != self.orb() || self.orb_motion.active() {
            return;
        }
        let previous = {
            let session = self.session.lock().unwrap();
            if !session.active || session.drag.is_some() {
                return;
            }
            session.place
        };
        let Some((place, monitor)) = previous.zip(
            app.get_webview_window(self.orb())
                .and_then(|window| window.current_monitor().ok().flatten()),
        ) else {
            return;
        };
        let area = area(monitor);
        if area == place.area {
            return;
        }
        self.session.lock().unwrap().place = Some(Placement::new(place.anchor, place.dock, area));
        let _ = self.update_bounds(app, true, Motion::Instant);
        self.publish(app);
    }

    pub fn close_window(&self, app: &AppHandle, desktop: &Desktop, label: &str) {
        match label {
            "main" => {
                let _ = self.mode(app, desktop, true, true);
            }
            _ if label == self.orb() => {
                let _ = self.mode(app, desktop, false, false);
            }
            _ if label == self.display() => self.clear(app),
            _ => {}
        }
    }

    pub fn begin_drag(self: &Arc<Self>, window: &WebviewWindow, origin: Point) -> Result<(), String> {
        #[cfg(windows)]
        {
            use crate::floating_geometry::{pulled_inward, snap};
            use crate::floating_motion::start_window_timer;
            use windows_sys::Win32::UI::Input::KeyboardAndMouse::{GetAsyncKeyState, VK_LBUTTON};
            let bounds = window_bounds(window)?;
            let (id, original, pointer) = {
                let mut session = self.session.lock().unwrap();
                if !session.active || session.drag.is_some() {
                    return Ok(());
                }
                let original = session.place.ok_or("悬浮球位置尚未就绪。")?;
                let scale = original.area.scale;
                if !(0. ..=bounds.width / scale).contains(&origin.x)
                    || !(0. ..=bounds.height / scale).contains(&origin.y)
                {
                    return Err("拖动起点超出悬浮球窗口。".into());
                }
                // 即使松手发生在命令到达前，仍以真实按下位置结算这个拖动。
                let pointer = tauri::PhysicalPosition::new(
                    bounds.origin.x + origin.x * scale,
                    bounds.origin.y + origin.y * scale,
                );
                session.revision += 1;
                let id = session.revision;
                session.drag = Some(id);
                (id, original, pointer)
            };
            let preview = self.clone();
            let source = window.clone();
            let mut retained = original.dock;
            let mut previous = pointer;
            let mut dragged_away = false;
            // 直接在桌面线程读取最新输入。窗口过渡繁忙时让系统合并计时消息。
            let mut sample = move || {
                if preview.session.lock().unwrap().drag != Some(id) {
                    return false;
                }
                let pressed = (unsafe { GetAsyncKeyState(i32::from(VK_LBUTTON)) }) < 0;
                let current = source.cursor_position().unwrap_or(previous);
                if current == previous && pressed {
                    return true;
                }
                previous = current;
                let point = Point {
                    x: current.x,
                    y: current.y,
                };
                let area = monitor_at(&source, point).unwrap_or(original.area);
                let dx = current.x - pointer.x;
                let dy = current.y - pointer.y;
                if retained != Dock::Free
                    && (area != original.area
                        || pulled_inward(retained, dx, dy, original.area.scale))
                {
                    retained = Dock::Free;
                    dragged_away = true;
                }
                let scale_ratio = area.scale / original.area.scale;
                let anchor = Point {
                    x: current.x + (original.anchor.x - pointer.x) * scale_ratio,
                    y: current.y + (original.anchor.y - pointer.y) * scale_ratio,
                };
                let mut dock = retained;
                if !pressed && dock == Dock::Free {
                    let edge = snap(anchor, area);
                    // 刚拉过脱离阈值便松开时，保留这次脱离意图。
                    if !(dragged_away && edge == original.dock && area == original.area) {
                        dock = edge;
                    }
                }
                preview.drag_frame(
                    source.app_handle(),
                    id,
                    Placement::new(anchor, dock, area),
                    pressed,
                );
                pressed
            };
            if sample() {
                if let Err(error) = start_window_timer(window, 16, sample) {
                    let mut session = self.session.lock().unwrap();
                    if session.drag == Some(id) {
                        session.drag = None;
                    }
                    return Err(error);
                }
            }
            Ok(())
        }
        #[cfg(not(windows))]
        {
            let _ = origin;
            window.start_dragging().map_err(|error| error.to_string())
        }
    }

    #[cfg(windows)]
    fn drag_frame(&self, app: &AppHandle, id: u64, place: Placement, pressed: bool) {
        let (arrangement_changed, frame_changed) = {
            let mut session = self.session.lock().unwrap();
            if session.drag != Some(id) {
                return;
            }
            let previous = session.place;
            let arrangement_changed =
                previous.is_none_or(|previous| !previous.same_arrangement(place));
            let display_focus = session.visible.map(|_| session.focused);
            let frame_changed = previous.and_then(|previous| previous.orb_frame(display_focus))
                != place.orb_frame(display_focus)
                || (session.visible.is_some()
                    && previous.map(|p| p.display_surface().frame(session.focused))
                        != Some(place.display_surface().frame(session.focused)));
            session.place = Some(place);
            if !pressed {
                session.drag = None;
            }
            (arrangement_changed, frame_changed)
        };
        if arrangement_changed || !pressed {
            self.publish(app);
        } else if frame_changed {
            self.publish_to(app, &[self.orb(), self.display()]);
        }
        let _ = self.update_bounds(
            app,
            true,
            if arrangement_changed {
                Motion::Animate
            } else {
                Motion::Follow
            },
        );
    }
}

// 正式窗口收到自身布局，只有图文子窗口读取展示正文。
fn product_snapshot(snapshot: &Value, label: &str) -> Value {
    if label == PRODUCT_ORB {
        json!({"revision":snapshot["revision"],"orb_frame":snapshot["orb_frame"]})
    } else {
        json!({
            "revision":snapshot["revision"],"active":snapshot["active"],
            "request":snapshot["request"],"visible":snapshot["visible"],
            "focused":snapshot["focused"],"display_frame":snapshot["display_frame"],
        })
    }
}

pub fn product_trusted(window: &WebviewWindow, desktop: &Desktop) -> Result<(), String> {
    if desktop.mode != crate::FrontendMode::Product || desktop.stopping.load(Ordering::Acquire) {
        return Err("当前不能操作正式悬浮窗。".into());
    }
    if window.label() == "main" {
        return crate::trusted(window, desktop);
    }
    let url = window.url().map_err(|error| error.to_string())?;
    if desktop.allows_floating_url(&url, window.label()) {
        Ok(())
    } else {
        Err("该页面没有正式悬浮窗权限。".into())
    }
}

#[tauri::command]
pub async fn floating_window_command(
    window: WebviewWindow,
    desktop: tauri::State<'_, Arc<Desktop>>,
    windows: tauri::State<'_, Arc<FloatingWindows>>,
    action: Action,
) -> Result<Value, String> {
    product_trusted(&window, &desktop)?;
    let app = window.app_handle().clone();
    let desktop = desktop.inner().clone();
    let windows = windows.inner().clone();
    let (reply, done) = tokio::sync::oneshot::channel();
    app.run_on_main_thread(move || {
        let result = product_trusted(&window, &desktop)
            .and_then(|()| apply_product_action(&window, &desktop, &windows, action));
        let _ = reply.send(result);
    })
    .map_err(|error| error.to_string())?;
    done.await.map_err(|_| "悬浮窗已关闭。".to_string())?
}

fn apply_product_action(
    window: &WebviewWindow,
    desktop: &Desktop,
    windows: &Arc<FloatingWindows>,
    action: Action,
) -> Result<Value, String> {
    let label = window.label();
    let app = window.app_handle();
    match (label, action) {
        ("main", Action::ModeStatus) => Ok(windows.mode_snapshot()),
        ("main", Action::InvalidateDisplay) => {
            let mut session = windows.session.lock().unwrap();
            if session.presented {
                session.mode_generation += 1;
                session.last_delivery = 0;
                drop(session);
                windows.clear(app);
                windows.emit_mode(app);
            } else {
                drop(session);
            }
            Ok(windows.mode_snapshot())
        }
        (
            "main",
            Action::Submit {
                generation,
                delivery,
                content,
                duration_seconds,
            },
        ) => {
            let mut session = windows.session.lock().unwrap();
            if !session.presented
                || generation != session.mode_generation
                || delivery <= session.last_delivery
            {
                return Err("展示投递已失效。".into());
            }
            let request = match (content, duration_seconds) {
                (None, None) => None,
                (Some(content), Some(duration_seconds))
                    if (5..=60).contains(&duration_seconds)
                        && !content.trim().is_empty()
                        && content.encode_utf16().count() <= 4096 =>
                {
                    session.revision += 1;
                    Some(DisplayRequest {
                        id: session.revision,
                        content,
                        duration_seconds,
                        delivery: Some(delivery),
                    })
                }
                _ => return Err("请提交有效的图文正文和 5–60 秒展示时长。".into()),
            };
            session.last_delivery = delivery;
            if let Some(request) = request {
                session.request = Some(request);
                drop(session);
                windows.publish(app);
            } else {
                drop(session);
                windows.clear(app);
                windows.emit_result(app, Some(delivery), DisplayStatus::Cleared, None);
            }
            Ok(Value::Null)
        }
        (PRODUCT_ORB, action @ (Action::Ready | Action::Drag { .. } | Action::Snapshot))
        | (
            PRODUCT_DISPLAY,
            action @ (Action::Snapshot | Action::Hover { .. } | Action::DisplayStatus { .. }),
        ) => apply_action(window, desktop, windows, action)
            .map(|snapshot| product_snapshot(&snapshot, label)),
        _ => Err("当前窗口没有此悬浮窗操作权限。".into()),
    }
}

#[tauri::command]
pub async fn floating_preview_command(
    window: WebviewWindow,
    desktop: tauri::State<'_, Arc<Desktop>>,
    preview: tauri::State<'_, Arc<FloatingWindows>>,
    action: Action,
) -> Result<Value, String> {
    trusted(&window, &desktop)?;
    if matches!(&action, Action::Attach) {
        if window.label() != "main" {
            return Err("此操作不属于当前测试窗口。".into());
        }
        preview.attach(window.app_handle(), &desktop).await?;
        return Ok(preview.publish(window.app_handle()));
    }
    // 窗口在异步命令中创建，显隐和回执则跟随托盘操作在桌面线程依次应用。
    let app = window.app_handle().clone();
    let desktop = desktop.inner().clone();
    let preview = preview.inner().clone();
    let (reply, done) = tokio::sync::oneshot::channel();
    app.run_on_main_thread(move || {
        let result = trusted(&window, &desktop)
            .and_then(|()| apply_action(&window, &desktop, &preview, action));
        let _ = reply.send(result);
    })
    .map_err(|error| error.to_string())?;
    done.await
        .map_err(|_| "悬浮窗测试窗口已关闭。".to_string())?
}

fn apply_action(
    window: &WebviewWindow,
    desktop: &Desktop,
    preview: &Arc<FloatingWindows>,
    action: Action,
) -> Result<Value, String> {
    let app = window.app_handle();
    let label = window.label();
    match action {
        Action::Snapshot => {
            let mut session = preview.session.lock().unwrap();
            if label == preview.display() {
                session.display_ready = true;
            }
            return Ok(session.snapshot());
        }
        Action::Ready if label == preview.orb() => {
            preview.session.lock().unwrap().orb_ready = true;
            if let Err(error) = preview.present_orb(app) {
                if preview.product {
                    let _ = preview.mode(app, desktop, false, false);
                }
                return Err(error);
            }
        }
        Action::Mode { enabled } if label == "main" => {
            preview.mode(app, desktop, enabled, false)?
        }
        Action::Return if label == ORB => preview.mode(app, desktop, false, false)?,
        Action::Place { dock } if label == "main" => preview.place(app, dock)?,
        Action::Voice { state, caption } if label == "main" => {
            if caption.chars().count() > 500 {
                return Err("样例字幕请控制在 500 字符以内。".into());
            }
            let mut session = preview.session.lock().unwrap();
            session.voice = state;
            session.caption = caption;
            session.caption_revision += 1;
        }
        Action::Toggle if label == ORB => {
            let mut session = preview.session.lock().unwrap();
            session.voice = if matches!(session.voice, VoiceState::Standby) {
                VoiceState::Listening
            } else {
                VoiceState::Standby
            };
        }
        Action::Drag { x, y } if label == preview.orb() => {
            preview.begin_drag(window, Point { x, y })?;
            return Ok(preview.session.lock().unwrap().snapshot());
        }
        Action::Show {
            content,
            duration_seconds,
        } if label == "main" => {
            if !(5..=60).contains(&duration_seconds) {
                return Err("展示时间需为 5–60 秒。".into());
            }
            if content.trim().is_empty() || content.encode_utf16().count() > 4096 {
                return Err("请提交有效且限量的图文正文。".into());
            }
            let mut session = preview.session.lock().unwrap();
            if !session.active {
                return Err("请先进入悬浮窗测试模式。".into());
            }
            session.revision += 1;
            session.request = Some(DisplayRequest {
                id: session.revision,
                content,
                duration_seconds,
                delivery: None,
            });
            session.notice = "正在检查图文排版。".into();
        }
        Action::Clear if label == "main" => preview.clear(app),
        Action::Hover { id, focused } if label == preview.display() => {
            let place = {
                let session = preview.session.lock().unwrap();
                if session.visible != Some(id) || session.focused == focused {
                    return Ok(session.snapshot());
                }
                session.place.ok_or("图文展示位置尚未就绪。")?
            };
            // 悬停只切换合成层的目标比例和输入区，网页引擎的画布保持固定。
            preview.display_clip.apply(
                window,
                place.display_surface().region(focused),
                !focused,
            )?;
            preview.session.lock().unwrap().focused = focused;
        }
        Action::DisplayStatus { id, status, reason } if label == preview.display() => {
            let mut session = preview.session.lock().unwrap();
            let latest = session
                .request
                .as_ref()
                .is_some_and(|request| request.id == id);
            if !session.active
                || (!latest && (status != DisplayStatus::Expired || session.visible != Some(id)))
            {
                return Ok(session.snapshot());
            }
            let delivery = if latest {
                session
                    .request
                    .as_ref()
                    .and_then(|request| request.delivery)
            } else {
                session.visible_delivery
            };
            match status {
                DisplayStatus::Visible => {
                    session.visible = Some(id);
                    session.visible_delivery = delivery;
                    session.notice = "展示中。".into();
                    drop(session);
                    if let Err(error) = preview
                        .update_bounds(app, false, Motion::Follow)
                        .and_then(|()| window.show().map_err(|error| error.to_string()))
                    {
                        preview.clear(app);
                        preview.emit_result(
                            app,
                            delivery,
                            DisplayStatus::Rejected,
                            Some("图文窗口无法显示。"),
                        );
                        return Err(error);
                    }
                }
                DisplayStatus::Rejected => {
                    session.notice = reason
                        .as_deref()
                        .unwrap_or("图文排版未通过校验。")
                        .chars()
                        .take(160)
                        .collect();
                }
                DisplayStatus::Cleared => return Err("清除展示由宿主处理。".into()),
                DisplayStatus::Expired => {
                    if session.visible != Some(id) {
                        return Ok(session.snapshot());
                    }
                    session.visible = None;
                    session.visible_delivery = None;
                    preview.display_motion.cancel();
                    preview.display_clip.cancel();
                    session.focused = false;
                    if latest {
                        session.request = None;
                        session.notice = "展示已收起。".into();
                    }
                    drop(session);
                    let _ = window.hide();
                }
            }
            preview.emit_result(app, delivery, status, reason.as_deref());
        }
        _ => return Err("此操作不属于当前测试窗口。".into()),
    }
    Ok(preview.publish(app))
}
