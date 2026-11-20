//! 正式宿主的代理调试会话。通信线程不占用桌面事件循环或音频线程。
mod ui;

use super::{Desktop, FrontendMode};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet, HashMap, HashSet},
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};
use tauri::{AppHandle, Manager};
use tokio::{
    io::BufReader,
    net::windows::named_pipe::NamedPipeServer,
    sync::{broadcast, mpsc, oneshot},
    task::JoinSet,
};
use voice_desktop_common::agent_debug as wire;

const MAX_CONNECTIONS: usize = 24;
const MAX_REQUESTS: usize = 16;
const HOLD: Duration = Duration::from_secs(20);

struct Pending {
    owner: u64,
    resume: oneshot::Sender<Option<Value>>,
}

#[derive(Default)]
struct Hooks {
    armed: HashMap<(String, String), (u64, Option<u64>)>,
    pending: HashMap<u64, Pending>,
}

pub struct AgentDebug {
    data: PathBuf,
    mode: bool,
    allowed: AtomicBool,
    setting_write: Mutex<()>,
    capability: String,
    sequence: AtomicU64,
    revision: tokio::sync::watch::Sender<u64>,
    events: broadcast::Sender<Value>,
    hooks: Mutex<Hooks>,
    points: Mutex<BTreeMap<String, BTreeSet<String>>>,
    pub ui: ui::State,
}

impl AgentDebug {
    pub fn install(app: &AppHandle, state: Arc<Desktop>, mode: bool) -> Result<(), String> {
        let context = state.context().ok_or("代理调试缺少正式工作区。")?;
        let endpoint = wire::Endpoint::new(&context.install)?;
        let session = wire::session_id()?;
        let capability = if mode {
            let mut bytes = [0u8; 32];
            use rand::TryRng;
            rand::rngs::SysRng
                .try_fill_bytes(&mut bytes)
                .map_err(|_| "无法生成调试进程凭据。")?;
            bytes.iter().map(|byte| format!("{byte:02x}")).collect()
        } else {
            String::new()
        };
        let allowed = wire::allowed(&context.data).unwrap_or(false);
        let debug = Arc::new(Self {
            data: context.data,
            mode,
            allowed: AtomicBool::new(allowed),
            setting_write: Mutex::new(()),
            capability,
            sequence: AtomicU64::new(1),
            revision: tokio::sync::watch::channel(0).0,
            events: broadcast::channel(256).0,
            hooks: Mutex::new(Hooks::default()),
            points: Mutex::new(BTreeMap::new()),
            ui: ui::State::default(),
        });
        let first = tauri::async_runtime::block_on(async { endpoint.server(true) })?;
        app.manage(debug.clone());
        let app = app.clone();
        tauri::async_runtime::spawn(async move {
            let mut listener = first;
            let permits = Arc::new(tokio::sync::Semaphore::new(MAX_CONNECTIONS));
            loop {
                if listener.connect().await.is_err() {
                    break;
                }
                let Ok(next) = endpoint.server(false) else {
                    break;
                };
                let pipe = std::mem::replace(&mut listener, next);
                let (Ok(permit), Ok((_, peer_session))) =
                    (permits.clone().try_acquire_owned(), wire::peer(&pipe))
                else {
                    continue;
                };
                let debug = debug.clone();
                let app = app.clone();
                let state = state.clone();
                tauri::async_runtime::spawn(async move {
                    let _permit = permit;
                    debug
                        .serve(pipe, app, state, session != 0 && session == peer_session)
                        .await;
                });
            }
        });
        Ok(())
    }

    pub fn allowed(&self) -> bool {
        self.allowed.load(Ordering::Acquire)
    }

    pub fn window_script(app: &AppHandle) -> &'static str {
        if app.try_state::<Arc<Self>>().is_some_and(|debug| debug.mode) {
            "Object.defineProperty(window,'__VOICE_AGENT_DEBUG__',{value:true});"
        } else {
            ""
        }
    }

    pub fn set_allowed(&self, allowed: bool) -> Result<(), String> {
        let _guard = self.setting_write.lock().unwrap();
        wire::save_allowed(&self.data, allowed)?;
        self.allowed.store(allowed, Ordering::Release);
        if !allowed {
            self.revoke();
        }
        Ok(())
    }

    pub fn revoke(&self) {
        *self.hooks.lock().unwrap() = Hooks::default();
        self.revision.send_modify(|revision| *revision += 1);
    }

    pub fn next_id(&self) -> u64 {
        self.sequence.fetch_add(1, Ordering::Relaxed)
    }

    pub fn configure_worker(
        &self,
        command: &mut std::process::Command,
        root: &std::path::Path,
    ) -> Result<(), String> {
        if self.mode {
            command
                .env("VOICE_AGENT_PIPE", wire::Endpoint::new(root)?.name)
                .env("VOICE_AGENT_TOKEN", &self.capability);
        }
        Ok(())
    }

    fn release(&self, session: u64) {
        let mut hooks = self.hooks.lock().unwrap();
        hooks.armed.retain(|_, (owner, _)| *owner != session);
        hooks.pending.retain(|_, pending| pending.owner != session);
    }

    pub fn observe(&self, point: &str, stage: &str, call: u64, data: &Value) {
        if !self.mode || !self.allowed() {
            return;
        }
        if !point.is_empty() {
            let mut points = self.points.lock().unwrap();
            if points.len() < 1024 || points.contains_key(point) {
                points.entry(point.into()).or_default().insert(stage.into());
            }
        }
        if self.events.receiver_count() > 0 {
            let _ = self.events.send(json!({
                "jsonrpc":"2.0","method":"trace.event",
                "params":{"point":point,"stage":stage,"call_id":call,"data":redact(data)}
            }));
        }
    }

    pub async fn point(&self, point: &str, stage: &str, call: u64, data: &Value) -> Option<Value> {
        if !self.mode || !self.allowed() {
            return None;
        }
        let wait = {
            let mut hooks = self.hooks.lock().unwrap();
            let key = (point.into(), stage.into());
            let matched = hooks
                .armed
                .get(&key)
                .is_some_and(|(_, target)| target.is_none_or(|target| target == call));
            (if matched {
                hooks.armed.remove(&key)
            } else {
                None
            })
            .map(|(owner, _)| {
                let (resume, wait) = oneshot::channel();
                let pause = self.next_id();
                hooks.pending.insert(pause, Pending { owner, resume });
                (pause, wait)
            })
        };
        self.observe(point, stage, call, data);
        let (pause, wait) = wait?;
        let _ = self.events.send(json!({
            "jsonrpc":"2.0","method":"hook.paused",
            "params":{"pause_id":pause,"point":point,"stage":stage,"call_id":call,"data":redact(data)}
        }));
        let replacement = tokio::time::timeout(HOLD, wait)
            .await
            .ok()
            .and_then(Result::ok)
            .flatten();
        self.hooks.lock().unwrap().pending.remove(&pause);
        replacement
    }

    async fn serve(
        self: Arc<Self>,
        pipe: NamedPipeServer,
        app: AppHandle,
        state: Arc<Desktop>,
        same_session: bool,
    ) {
        let session = self.next_id();
        let (reader, mut writer) = tokio::io::split(pipe);
        let mut reader = BufReader::new(reader);
        let mut bytes = Vec::new();
        let (output, mut messages) = mpsc::channel(64);
        let mut writer_task = tokio::spawn(async move {
            while let Some(value) = messages.recv().await {
                wire::write_frame(&mut writer, &value).await?;
            }
            Ok::<_, std::io::Error>(())
        });
        let mut revision = self.revision.subscribe();
        let mut subscription = None;
        let mut requests = JoinSet::new();
        let mut ids = HashSet::new();
        let mut finish = false;
        let mut stopping = false;
        loop {
            tokio::select! {
                _ = &mut writer_task => break,
                _ = revision.changed() => break,
                done = requests.join_next(), if !requests.is_empty() => {
                    if let Some(Ok(id)) = done { ids.remove(&id); }
                }
                event = next_event(&mut subscription), if subscription.is_some() => {
                    if let Some(event) = event {
                        if output.try_send(event).is_err() { break; }
                    }
                }
                frame = wire::read_frame_into(&mut reader, &mut bytes), if !finish => {
                    let Ok(Some(frame)) = frame else { break; };
                    let request: Value = match serde_json::from_slice(&frame) {
                        Ok(request) => request,
                        Err(_) => {
                            if output.try_send(reply(Value::Null, Err("调试消息不是有效的 JSON。".into()))).is_err() { break; }
                            continue;
                        }
                    };
                    let id = request["id"].clone();
                    let method = request["method"].as_str().unwrap_or_default();
                    let params = &request["params"];
                    if request["jsonrpc"] != "2.0" {
                        if output.try_send(reply(id, Err("调试协议版本无效。".into()))).is_err() { break; }
                        continue;
                    }
                    if method == "transport.finish" {
                        finish = true;
                        break;
                    }
                    if !id.is_string() && !id.is_i64() && !id.is_u64() {
                        if output.try_send(reply(Value::Null, Err("调试请求需要字符串或整数编号。".into()))).is_err() { break; }
                        continue;
                    }
                    let enabled = self.allowed();
                    let immediate = match method {
                        "instance.status" if enabled => Ok(json!({
                            "mode":if self.mode {"agent-debug"} else {"normal"},
                            "pid":std::process::id(),
                            "stopping":state.stopping.load(Ordering::Acquire),
                            "window_ready":app.webview_windows().values().any(|window|window.is_visible().unwrap_or(false))
                        })),
                        "instance.exit" if enabled || same_session => {
                            stopping = true;
                            Ok(json!({"accepted":true}))
                        }
                        _ if !enabled || !self.mode => Err("当前实例未开放该调试操作。".into()),
                        "trace.subscribe" => {
                            subscription = Some(self.events.subscribe());
                            Ok(json!({"subscribed":true}))
                        }
                        "hook.arm" => {
                            subscription.get_or_insert_with(|| self.events.subscribe());
                            self.arm(session, params)
                        }
                        "hook.resume" => self.resume(session, params),
                        _ => {
                            if requests.len() >= MAX_REQUESTS || !ids.insert(id.clone()) {
                                if output.try_send(reply(id, Err("调试请求过多或请求编号重复。".into()))).is_err() { break; }
                                continue;
                            }
                            let (debug, state, app, output) = (self.clone(), state.clone(), app.clone(), output.clone());
                            requests.spawn(async move {
                                let outcome = debug.dispatch(&app, &state, &request).await;
                                let _ = output.send(reply(id.clone(), outcome)).await;
                                id
                            });
                            continue;
                        }
                    };
                    if output.try_send(reply(id, immediate)).is_err() { break; }
                    if stopping { break; }
                }
            }
        }
        self.release(session);
        if finish {
            while requests.join_next().await.is_some() {}
            let _ = output
                .send(json!({"jsonrpc":"2.0","method":"transport.finished"}))
                .await;
        } else {
            requests.abort_all();
        }
        drop(output);
        if !writer_task.is_finished()
            && tokio::time::timeout(Duration::from_secs(2), &mut writer_task)
                .await
                .is_err()
        {
            writer_task.abort();
        }
        if finish || stopping {
            let _ =
                tokio::time::timeout(Duration::from_secs(1), wire::read_frame(&mut reader)).await;
        }
        if stopping {
            state.shutdown(&app);
        }
    }

    fn arm(&self, session: u64, params: &Value) -> Result<Value, String> {
        let point = field(params, "point")?;
        let stage = field(params, "stage")?;
        if point.len() > 240 || !matches!(stage, "input" | "result") {
            return Err("介入节点或阶段无效。".into());
        }
        let mut hooks = self.hooks.lock().unwrap();
        let key = (point.into(), stage.into());
        if hooks.armed.len() >= 32 || hooks.armed.contains_key(&key) {
            return Err("该节点已设置介入或介入数量已达上限。".into());
        }
        hooks
            .armed
            .insert(key, (session, params["call_id"].as_u64()));
        Ok(json!({"armed":true}))
    }

    fn resume(&self, session: u64, params: &Value) -> Result<Value, String> {
        let pause = params["pause_id"].as_u64().ok_or("缺少暂停编号。")?;
        let mut hooks = self.hooks.lock().unwrap();
        if !hooks
            .pending
            .get(&pause)
            .is_some_and(|pending| pending.owner == session)
        {
            return Err("暂停调用已结束或不属于当前连接。".into());
        }
        let pending = hooks.pending.remove(&pause).unwrap();
        pending
            .resume
            .send(params.get("replacement").cloned())
            .map_err(|_| "暂停调用已结束。")?;
        Ok(json!({"resumed":true}))
    }

    async fn dispatch(
        &self,
        app: &AppHandle,
        state: &Arc<Desktop>,
        request: &Value,
    ) -> Result<Value, String> {
        let params = &request["params"];
        if state.stopping.load(Ordering::Acquire) {
            return Err("应用正在退出。".into());
        }
        match request["method"].as_str().unwrap_or_default() {
            "ui.snapshot" => self.ui.snapshot(app, state, params).await,
            "ui.act" => self.ui.act(app, state, self, params).await,
            "module.list" => {
                let mut modules = ui::modules(app, state, params).await?;
                modules["observed_points"] = json!(*self.points.lock().unwrap());
                Ok(modules)
            }
            "module.call" => {
                let point = field(params, "operation")?;
                let call = self.next_id();
                let data = object(params.get("arguments"))?;
                let replacement = if params["surface"] == "service" {
                    None
                } else {
                    self.point(point, "input", call, &data).await
                };
                let arguments = object(replacement.as_ref().or(Some(&data)))?;
                let result = ui::call(app, state, params, call, arguments).await;
                match result {
                    Ok(result) => {
                        self.observe(point, "result", call, &result);
                        Ok(json!({"call_id":call,"value":result}))
                    }
                    Err(error) => {
                        self.observe(point, "error", call, &Value::Null);
                        Err(error)
                    }
                }
            }
            "worker.point" if params["capability"] == self.capability => {
                let point = field(params, "point")?;
                let stage = field(params, "stage")?;
                let call = params["call_id"].as_u64().unwrap_or_else(|| self.next_id());
                let replacement = self.point(point, stage, call, &params["data"]).await;
                Ok(
                    json!({"call_id":call,"replacement":replacement,"replaced":replacement.is_some()}),
                )
            }
            _ => Err("未知的代理调试方法。".into()),
        }
    }
}

pub fn set_allowed(
    window: tauri::WebviewWindow,
    state: tauri::State<Arc<Desktop>>,
    allowed: bool,
) -> Result<(), String> {
    super::trusted(&window, &state)?;
    if state.mode != FrontendMode::Product || state.stopping.load(Ordering::Acquire) {
        return Err("当前窗口不能修改外部调试授权。".into());
    }
    window.state::<Arc<AgentDebug>>().set_allowed(allowed)
}

pub fn window_operation(window: &tauri::WebviewWindow) -> Option<(Arc<AgentDebug>, u64)> {
    let debug = window.try_state::<Arc<AgentDebug>>()?.inner().clone();
    if !debug.mode || !debug.allowed() {
        return None;
    }
    let call = debug
        .ui
        .action_id(window.label())
        .unwrap_or_else(|| debug.next_id());
    Some((debug, call))
}

#[tauri::command]
pub async fn agent_debug_trace_native(
    window: tauri::WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    point: String,        // point（实际原生命令编号）。
    stage: String,        // stage（输入、结果或失败阶段）。
    value: Value,         // value（该阶段的结构化业务数据）。
    call_id: Option<u64>, // call_id（关联调用编号）。
) -> Result<Value, String> {
    let Some((debug, default_call)) = window_operation(&window) else {
        return Ok(Value::Null);
    };
    ui::check_command(&window, &state, &point)?;
    let id = call_id.unwrap_or(default_call);
    let replacement = match stage.as_str() {
        "input" => debug
            .point(&point, &stage, id, &value)
            .await
            .filter(Value::is_object),
        "result" | "error" => {
            debug.observe(&point, &stage, id, &value);
            None
        }
        _ => return Err("无效的原生命令调试阶段。".into()),
    };
    Ok(json!({"call_id":id,"replacement":replacement}))
}

async fn next_event(subscription: &mut Option<broadcast::Receiver<Value>>) -> Option<Value> {
    match subscription.as_mut()?.recv().await {
        Ok(value) => Some(value),
        Err(broadcast::error::RecvError::Lagged(dropped)) => {
            Some(json!({"jsonrpc":"2.0","method":"trace.gap","params":{"dropped":dropped}}))
        }
        Err(broadcast::error::RecvError::Closed) => None,
    }
}

fn reply(id: Value, result: Result<Value, String>) -> Value {
    match result {
        Ok(value) => json!({"jsonrpc":"2.0","id":id,"result":value}),
        Err(error) => json!({"jsonrpc":"2.0","id":id,"error":{"code":-32000,"message":error}}),
    }
}

fn field<'a>(value: &'a Value, key: &str) -> Result<&'a str, String> {
    value[key]
        .as_str()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| format!("调试请求缺少有效字段 {key}。"))
}

fn object(value: Option<&Value>) -> Result<Value, String> {
    match value {
        Some(value) if value.is_object() => Ok(value.clone()),
        None => Ok(json!({})),
        _ => Err("模块参数必须是结构化对象。".into()),
    }
}

fn redact(value: &Value) -> Value {
    match value {
        Value::Object(map) => Value::Object(
            map.iter()
                .map(|(key, value)| {
                    let normalized = key.to_lowercase().replace(['_', '-'], "");
                    let private = [
                        "token",
                        "secret",
                        "password",
                        "apikey",
                        "authorization",
                        "privatekey",
                        "credential",
                        "pairingcode",
                        "cookie",
                        "signature",
                    ]
                    .iter()
                    .any(|term| normalized.contains(term));
                    (
                        key.clone(),
                        if private {
                            Value::String("已隐藏".into())
                        } else {
                            redact(value)
                        },
                    )
                })
                .collect(),
        ),
        Value::Array(values) => Value::Array(values.iter().take(64).map(redact).collect()),
        Value::String(value) => Value::String(value.chars().take(2000).collect()),
        _ => value.clone(),
    }
}
