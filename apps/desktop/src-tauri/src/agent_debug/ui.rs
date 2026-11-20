//! 复用当前正式窗口的调试接口，所有浏览器调用绑定到具体受信窗口。
mod native;

use super::{field, AgentDebug};
use crate::{Desktop, FrontendMode};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    sync::Mutex,
    time::{Duration, Instant},
};
use tauri::{
    ipc::{CallbackFn, InvokeBody, InvokeResponse, InvokeResponseBody},
    AppHandle, Manager, WebviewWindow,
};
use tokio::sync::oneshot;
use webview2_com::CallDevToolsProtocolMethodCompletedHandler;
use windows::core::PCWSTR;

const COMMANDS: &str = include_str!(concat!(env!("OUT_DIR"), "/desktop-commands.txt"));

#[derive(Default)]
pub struct State {
    actions: Mutex<HashMap<String, u64>>,
    recent: Mutex<HashMap<String, (u64, Instant)>>,
}

struct Action<'a>(&'a State, &'a str);
impl Drop for Action<'_> {
    fn drop(&mut self) {
        self.0.actions.lock().unwrap().remove(self.1);
    }
}

impl State {
    pub fn action_id(&self, label: &str) -> Option<u64> {
        self.actions
            .lock()
            .unwrap()
            .get(label)
            .copied()
            .or_else(|| {
                self.recent
                    .lock()
                    .unwrap()
                    .get(label)
                    .filter(|(_, at)| at.elapsed() < Duration::from_millis(600))
                    .map(|(id, _)| *id)
            })
    }

    pub async fn snapshot(
        &self,
        app: &AppHandle,
        state: &Desktop,
        params: &Value,
    ) -> Result<Value, String> {
        let mut windows = Vec::new();
        for window in app.webview_windows().into_values() {
            if owned(&window, state).is_ok() {
                windows.push(json!({
                    "window":window.label(),
                    "title":window.title().unwrap_or_default(),
                    "visible":window.is_visible().unwrap_or(false),
                    "kind":"webview"
                }));
            }
        }
        windows.sort_by_key(|window| window["window"].as_str().unwrap_or_default().to_owned());
        let mut native = tauri::async_runtime::spawn_blocking(native::list)
            .await
            .map_err(|error| error.to_string())??;
        windows.append(&mut native);
        if state.tray.load(std::sync::atomic::Ordering::Acquire) {
            windows
                .push(json!({"window":"tray","title":"通知区域菜单","visible":true,"kind":"tray"}));
        }
        let Some(label) = params["window"].as_str() else {
            return Ok(json!({"windows":windows}));
        };
        if label == "tray" {
            let controls = super::super::AGENT_TRAY_ITEMS.iter()
                .map(|(id, title)| json!({"id":id,"name":title,"role":"menuitem","actions":["click"]}))
                .collect::<Vec<_>>();
            return Ok(json!({"window":label,"controls":controls}));
        }
        if label.starts_with("native:") {
            let label = label.to_owned();
            return tauri::async_runtime::spawn_blocking(move || native::snapshot(&label))
                .await
                .map_err(|error| error.to_string())?;
        }
        let window = target(app, state, Some(label))?;
        let revision = revision(&window).await?;
        let nodes = cdp(&window, "Accessibility.getFullAXTree", json!({})).await?;
        let controls = nodes["nodes"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(accessible)
            .collect::<Vec<_>>();
        let screenshot = if params["screenshot"] == true {
            cdp(
                &window,
                "Page.captureScreenshot",
                json!({"format":"jpeg","quality":70}),
            )
            .await?["data"]
                .clone()
        } else {
            Value::Null
        };
        let current = self::revision(&window).await?;
        if revision != current {
            return Err("窗口正在切换，请重新读取控件。".into());
        }
        Ok(
            json!({"window":label,"revision":revision,"controls":controls,"screenshot_jpeg":screenshot}),
        )
    }

    pub async fn act(
        &self,
        app: &AppHandle,
        state: &std::sync::Arc<Desktop>,
        debug: &AgentDebug,
        params: &Value,
    ) -> Result<Value, String> {
        let label = field(params, "window")?;
        let action = field(params, "action")?;
        let id = debug.next_id();
        {
            let mut actions = self.actions.lock().unwrap();
            if actions.contains_key(label) {
                return Err("该窗口已有操作正在进行。".into());
            }
            actions.insert(label.into(), id);
        }
        let _guard = Action(self, label);
        debug.observe("ui.act", "input", id, params);
        if label == "tray" {
            let item = field(params, "control")?;
            if action != "click"
                || !super::super::AGENT_TRAY_ITEMS
                    .iter()
                    .any(|(name, _)| *name == item)
            {
                return Err("通知区域操作无效。".into());
            }
            let (app_handle, desktop, item) = (app.clone(), state.clone(), item.to_owned());
            app.run_on_main_thread(move || {
                super::super::handle_tray_item(&app_handle, &desktop, &item)
            })
            .map_err(|error| error.to_string())?;
        } else if label.starts_with("native:") {
            let params = params.clone();
            tauri::async_runtime::spawn_blocking(move || native::act(&params))
                .await
                .map_err(|error| error.to_string())??;
        } else {
            let window = target(app, state, Some(label))?;
            let document = revision(&window).await?;
            if field(params, "revision")? != document {
                return Err("窗口已切换，请重新读取控件。".into());
            }
            if let Some(node) = params["control"].as_u64() {
                let fresh = cdp(
                    &window,
                    "Accessibility.getPartialAXTree",
                    json!({"backendNodeId":node,"fetchRelatives":false}),
                )
                .await?;
                let control = fresh["nodes"]
                    .as_array()
                    .and_then(|nodes| {
                        nodes
                            .iter()
                            .find(|current| current["backendDOMNodeId"].as_u64() == Some(node))
                    })
                    .and_then(accessible)
                    .ok_or("控件已移除或不可用，请重新读取控件。")?;
                if control["disabled"] == true {
                    return Err("当前控件处于禁用状态。".into());
                }
            }
            web_action(&window, action, params).await?;
        }
        self.recent
            .lock()
            .unwrap()
            .insert(label.into(), (id, Instant::now()));
        debug.observe("ui.act", "result", id, &Value::Null);
        Ok(json!({"call_id":id}))
    }
}

fn owned(window: &WebviewWindow, state: &Desktop) -> Result<(), String> {
    if state.mode != FrontendMode::Product {
        return Err("当前窗口不是正式产品窗口。".into());
    }
    if window.label() == "floating" {
        crate::trusted_floating(window, state)
    } else {
        crate::trusted(window, state)
    }
}

fn target(app: &AppHandle, state: &Desktop, label: Option<&str>) -> Result<WebviewWindow, String> {
    let window = match label {
        Some(label) => app.get_webview_window(label),
        None => app
            .get_webview_window("main")
            .or_else(|| app.get_webview_window("startup")),
    }
    .ok_or("没有匹配的正式页面。")?;
    owned(&window, state)?;
    Ok(window)
}

fn accessible(node: &Value) -> Option<Value> {
    if node["ignored"] == true {
        return None;
    }
    let role = node["role"]["value"].as_str()?;
    let id = node["backendDOMNodeId"].as_u64();
    let properties = node["properties"].as_array();
    let property = |name| {
        properties?
            .iter()
            .find(|item| item["name"] == name)
            .map(|item| item["value"]["value"].clone())
    };
    let focusable = property("focusable") == Some(Value::Bool(true));
    let actions = match role {
        "textbox" | "searchbox" | "spinbutton" => vec!["click", "focus", "type", "press"],
        "button" | "link" | "tab" | "checkbox" | "radio" | "switch" | "menuitem" | "combobox"
        | "option" | "slider" | "treeitem" | "DisclosureTriangle" => vec!["click", "press"],
        _ if focusable => vec!["click", "focus", "press"],
        _ => vec![],
    };
    let name = node["name"]["value"].as_str().unwrap_or_default();
    let protected = property("protected") == Some(Value::Bool(true))
        || ["密码", "密钥", "password", "api key"]
            .iter()
            .any(|term| name.to_lowercase().contains(term));
    if id.is_none() || (actions.is_empty() && name.is_empty()) {
        return None;
    }
    let disabled = property("disabled") == Some(Value::Bool(true));
    Some(json!({
        "id": id, "name":name,"role":role,
        "state":{"checked":property("checked"),"selected":property("selected"),
            "expanded":property("expanded"),"value":if protected {Value::Null} else {node["value"]["value"].clone()}},
        "disabled":disabled,"actions":if disabled {vec![]} else {actions}
    }))
}

async fn revision(window: &WebviewWindow) -> Result<String, String> {
    let tree = cdp(window, "Page.getFrameTree", json!({})).await?;
    Ok(field(&tree["frameTree"]["frame"], "loaderId")?.to_owned())
}

async fn web_action(window: &WebviewWindow, action: &str, params: &Value) -> Result<(), String> {
    let node = params["control"].as_u64();
    match action {
        "focus" => {
            cdp(
                window,
                "DOM.focus",
                json!({"backendNodeId":node.ok_or("缺少控件编号。")?}),
            )
            .await?;
        }
        "type" => {
            cdp(
                window,
                "DOM.focus",
                json!({"backendNodeId":node.ok_or("缺少控件编号。")?}),
            )
            .await?;
            cdp(
                window,
                "Input.insertText",
                json!({"text":field(params, "text")?}),
            )
            .await?;
        }
        "press" => {
            if let Some(node) = node {
                cdp(window, "DOM.focus", json!({"backendNodeId":node})).await?;
            }
            let key = field(params, "key")?;
            let virtual_key = match key {
                "Enter" => 13,
                "Escape" => 27,
                "Tab" => 9,
                " " => 32,
                "Backspace" => 8,
                "Delete" => 46,
                "ArrowLeft" => 37,
                "ArrowUp" => 38,
                "ArrowRight" => 39,
                "ArrowDown" => 40,
                "Home" => 36,
                "End" => 35,
                _ => return Err("不支持该按键。文字输入请使用 type（输入文字）。".into()),
            };
            for kind in ["keyDown", "keyUp"] {
                cdp(
                    window,
                    "Input.dispatchKeyEvent",
                    json!({"type":kind,"key":key,"windowsVirtualKeyCode":virtual_key}),
                )
                .await?;
            }
        }
        "click" => {
            let node = node.ok_or("缺少控件编号。")?;
            cdp(
                window,
                "DOM.scrollIntoViewIfNeeded",
                json!({"backendNodeId":node}),
            )
            .await?;
            let content = cdp(window, "DOM.getContentQuads", json!({"backendNodeId":node})).await?;
            let quad = content["quads"]
                .as_array()
                .and_then(|quads| quads.first())
                .and_then(Value::as_array)
                .filter(|quad| quad.len() == 8)
                .ok_or("控件没有可点击区域。")?;
            let x = quad
                .iter()
                .step_by(2)
                .filter_map(Value::as_f64)
                .sum::<f64>()
                / 4.0;
            let y = quad
                .iter()
                .skip(1)
                .step_by(2)
                .filter_map(Value::as_f64)
                .sum::<f64>()
                / 4.0;
            for kind in ["mousePressed", "mouseReleased"] {
                cdp(
                    window,
                    "Input.dispatchMouseEvent",
                    json!({
                        "type":kind,"button":"left","clickCount":1,"x":x,"y":y
                    }),
                )
                .await?;
            }
        }
        _ => return Err("未知的控件操作。".into()),
    }
    Ok(())
}

fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(Some(0)).collect()
}

async fn cdp(window: &WebviewWindow, method: &'static str, params: Value) -> Result<Value, String> {
    let (send, reply) = oneshot::channel();
    // 回调与同步失败都在同一桌面线程收尾，调用方通过有界等待接收结果。
    window
        .with_webview(move |view| {
            let send = std::rc::Rc::new(std::cell::RefCell::new(Some(send)));
            let completion = send.clone();
            let handler = CallDevToolsProtocolMethodCompletedHandler::create(Box::new(
                move |status, value| {
                    let result = status.map_err(|error| error.to_string()).and_then(|_| {
                        serde_json::from_str(&value).map_err(|error| error.to_string())
                    });
                    if let Some(send) = completion.borrow_mut().take() {
                        let _ = send.send(result);
                    }
                    Ok(())
                },
            ));
            let method = wide(method);
            let params = wide(&params.to_string());
            let result = unsafe {
                view.controller().CoreWebView2().and_then(|core| {
                    core.CallDevToolsProtocolMethod(
                        PCWSTR(method.as_ptr()),
                        PCWSTR(params.as_ptr()),
                        &handler,
                    )
                })
            };
            if let Err(error) = result {
                if let Some(send) = send.borrow_mut().take() {
                    let _ = send.send(Err(error.to_string()));
                }
            }
        })
        .map_err(|error| error.to_string())?;
    let timeout = if method == "Runtime.evaluate" { 120 } else { 8 };
    tokio::time::timeout(Duration::from_secs(timeout), reply)
        .await
        .map_err(|_| "等待网页调试回执超时。")?
        .map_err(|_| "网页调试窗口已关闭。")?
}

async fn evaluate(window: &WebviewWindow, expression: String) -> Result<Value, String> {
    let result = cdp(
        window,
        "Runtime.evaluate",
        json!({"expression":expression,"awaitPromise":true,"returnByValue":true}),
    )
    .await?;
    if result.get("exceptionDetails").is_some() {
        return Err("当前页面无法完成业务接口调用。".into());
    }
    Ok(result["result"]["value"].clone())
}

fn desktop_commands(window: &WebviewWindow, state: &Desktop) -> Result<Vec<&'static str>, String> {
    let url = window.url().map_err(|error| error.to_string())?;
    let mut capability = if window.label() == "floating" {
        crate::floating_capability(&url.origin().ascii_serialization(), 0)
    } else if crate::boot_page_url(&url) {
        serde_json::from_str(include_str!("../../capabilities/boot.json"))
            .map_err(|error| error.to_string())?
    } else {
        crate::frontend_capability(state.mode, &url.origin().ascii_serialization(), 0)
    };
    if window.label() == "main"
        && crate::remote_static_product_page_url(&url)
        && state.context().is_some_and(|context| {
            voice_desktop_common::device_config::inspect(&context.data).mode
                == Some(voice_desktop_common::device_config::RunMode::Remote)
        })
    {
        let imports: Value = serde_json::from_str(include_str!("../../capabilities/remote-import.json"))
            .map_err(|error| error.to_string())?;
        if let (Some(commands), Some(imports)) = (
            capability["permissions"].as_array_mut(),
            imports["permissions"].as_array(),
        ) {
            commands.extend(imports.iter().cloned());
        }
    }
    Ok(COMMANDS
        .lines()
        .filter(|command| {
            if *command == "agent_debug_trace_native" {
                return false;
            }
            let permission = format!("allow-{}", command.replace('_', "-"));
            capability["permissions"]
                .as_array()
                .is_some_and(|entries| entries.iter().any(|entry| entry == &permission))
        })
        .collect())
}

pub fn check_command(window: &WebviewWindow, state: &Desktop, point: &str) -> Result<(), String> {
    owned(window, state)?;
    if !desktop_commands(window, state)?.contains(&point) {
        return Err("该原生命令不属于当前窗口。".into());
    }
    Ok(())
}

pub async fn modules(app: &AppHandle, state: &Desktop, params: &Value) -> Result<Value, String> {
    let window = target(app, state, params["window"].as_str())?;
    let desktop = desktop_commands(&window, state)?;
    let service = evaluate(&window, format!("({SERVICE})(null)")).await?;
    let remote: &[&str] = &[];
    #[cfg(feature = "ssh-device")]
    let remote = if desktop.contains(&"device_ssh_management_request")
        && state.context().is_some_and(|context| {
            voice_desktop_common::device_config::inspect(&context.data).mode
                == Some(voice_desktop_common::device_config::RunMode::Remote)
        }) {
        super::super::REMOTE_BUSINESS_OPERATIONS
    } else {
        remote
    };
    Ok(json!({"desktop":desktop,"service":service,"remote":remote,
        "remote_connected":state.remote_client_connected.load(std::sync::atomic::Ordering::Acquire)}))
}

pub async fn call(
    app: &AppHandle,
    state: &Desktop,
    params: &Value,
    call: u64,
    arguments: Value,
) -> Result<Value, String> {
    let window = target(app, state, params["window"].as_str())?;
    let mut operation = field(params, "operation")?;
    let mut arguments = arguments;
    if params["surface"] == "service" {
        let value = evaluate(
            &window,
            format!(
                "({SERVICE})({})",
                json!({"operation":operation,"arguments":arguments,"call_id":call})
            ),
        )
        .await?;
        return if let Some(error) = value["error"].as_str() {
            Err(error.into())
        } else {
            Ok(value)
        };
    }
    if params["surface"] == "remote" {
        arguments =
            json!({"operation":operation,"arguments":arguments,"request_id":params["request_id"]});
        operation = "device_ssh_management_request";
    }
    if !desktop_commands(&window, state)?.contains(&operation) {
        return Err("该原生命令不属于当前窗口。".into());
    }
    let url = window.url().map_err(|error| error.to_string())?;
    let webview = window.as_ref().clone();
    let request = tauri::webview::InvokeRequest {
        cmd: operation.to_owned(),
        callback: CallbackFn(0),
        error: CallbackFn(1),
        url,
        body: InvokeBody::Json(arguments),
        headers: Default::default(),
        invoke_key: app.invoke_key().to_owned(),
    };
    let (send, receive) = oneshot::channel();
    app.run_on_main_thread(move || {
        webview.on_message(
            request,
            Box::new(move |_, _, result, _, _| {
                let result = match result {
                    InvokeResponse::Ok(InvokeResponseBody::Json(body)) => {
                        serde_json::from_str(&body).map_err(|error| error.to_string())
                    }
                    InvokeResponse::Ok(InvokeResponseBody::Raw(_)) => {
                        Err("操作返回二进制原生结果。".into())
                    }
                    InvokeResponse::Err(error) => {
                        Err(error.0.as_str().unwrap_or("原生命令执行失败。").into())
                    }
                };
                let _ = send.send(result);
            }),
        )
    })
    .map_err(|error| error.to_string())?;
    tokio::time::timeout(Duration::from_secs(120), receive)
        .await
        .map_err(|_| "原生命令回执超时，结果需重新查询。")?
        .map_err(|_| "原生命令已取消。")?
}

// 仅使用正式页面的同源宿主和本次运行配置中的核心地址，沿用现有会话门禁。
const SERVICE: &str = include_str!("service.js");
