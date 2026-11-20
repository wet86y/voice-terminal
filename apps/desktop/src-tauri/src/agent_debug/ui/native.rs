//! 原生对话框仅遍历正式宿主所属的可见窗口。
use serde_json::{json, Value};
use windows::{
    core::{BOOL, BSTR},
    Win32::{
        Foundation::{HWND, LPARAM},
        System::Com::{
            CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_INPROC_SERVER,
            COINIT_MULTITHREADED,
        },
        UI::{Accessibility::*, WindowsAndMessaging::*},
    },
};

fn dialogs() -> Result<Vec<HWND>, String> {
    unsafe extern "system" fn collect(window: HWND, context: LPARAM) -> BOOL {
        let mut pid = 0;
        let mut name = [0u16; 64];
        unsafe {
            GetWindowThreadProcessId(window, Some(&mut pid));
            let count = GetClassNameW(window, &mut name).max(0) as usize;
            if pid == std::process::id()
                && IsWindowVisible(window).as_bool()
                && String::from_utf16_lossy(&name[..count]) == "#32770"
            {
                (*(context.0 as *mut Vec<HWND>)).push(window);
            }
        }
        true.into()
    }
    let mut windows = Vec::new();
    unsafe {
        EnumWindows(
            Some(collect),
            LPARAM(&mut windows as *mut Vec<HWND> as isize),
        )
    }
    .map_err(|error| error.to_string())?;
    Ok(windows)
}

fn label(window: HWND) -> String {
    format!("native:{:x}", window.0 as usize)
}

pub fn list() -> Result<Vec<Value>, String> {
    dialogs().map(|windows| windows.into_iter().map(|window| {
        let mut title = [0u16; 512];
        let length = unsafe { GetWindowTextW(window, &mut title) }.max(0) as usize;
        json!({"window":label(window),"visible":true,"kind":"native","title":String::from_utf16_lossy(&title[..length])})
    }).collect())
}

struct Com;
impl Drop for Com {
    fn drop(&mut self) {
        unsafe { CoUninitialize() }
    }
}

fn inspect<T>(
    id: &str,
    action: impl FnOnce(Vec<(String, IUIAutomationElement)>) -> Result<T, String>,
) -> Result<T, String> {
    let window = dialogs()?
        .into_iter()
        .find(|window| label(*window) == id)
        .ok_or("原生对话框已关闭。")?;
    unsafe { CoInitializeEx(None, COINIT_MULTITHREADED).ok() }
        .map_err(|error| error.to_string())?;
    let _com = Com;
    let automation: IUIAutomation =
        unsafe { CoCreateInstance(&CUIAutomation, None, CLSCTX_INPROC_SERVER) }
            .map_err(|error| error.to_string())?;
    let walker = unsafe { automation.ControlViewWalker() }.map_err(|error| error.to_string())?;
    let element =
        unsafe { automation.ElementFromHandle(window) }.map_err(|error| error.to_string())?;
    let mut result = Vec::new();
    let mut pending = vec![("0".to_owned(), element)];
    while let Some((path, element)) = pending.pop() {
        let mut child = unsafe { walker.GetFirstChildElement(&element) }.ok();
        let mut children = Vec::new();
        while let Some(element) = child {
            if children.len() >= 512 {
                break;
            }
            child = unsafe { walker.GetNextSiblingElement(&element) }.ok();
            children.push((format!("{path}.{}", children.len()), element));
        }
        pending.extend(children.into_iter().rev());
        result.push((path, element));
        if result.len() >= 1024 {
            break;
        }
    }
    action(result)
}

fn details(path: &str, element: &IUIAutomationElement) -> Value {
    unsafe {
        let name = element.CurrentName().unwrap_or_default();
        let role = element.CurrentLocalizedControlType().unwrap_or_default();
        let enabled = element
            .CurrentIsEnabled()
            .is_ok_and(|enabled| enabled.as_bool());
        let private = element
            .CurrentIsPassword()
            .is_ok_and(|private| private.as_bool());
        let automation_id = element.CurrentAutomationId().unwrap_or_default();
        let mut actions = Vec::new();
        let mut state = json!({});
        if element
            .GetCurrentPatternAs::<IUIAutomationInvokePattern>(UIA_InvokePatternId)
            .is_ok()
        {
            actions.push("click");
        }
        if let Ok(value) =
            element.GetCurrentPatternAs::<IUIAutomationValuePattern>(UIA_ValuePatternId)
        {
            if !private {
                state["value"] = json!(value.CurrentValue().unwrap_or_default().to_string());
            }
            if value
                .CurrentIsReadOnly()
                .is_ok_and(|value| !value.as_bool())
            {
                actions.push("type");
            }
        }
        if let Ok(toggle) =
            element.GetCurrentPatternAs::<IUIAutomationTogglePattern>(UIA_TogglePatternId)
        {
            actions.push("toggle");
            state["checked"] = json!(toggle.CurrentToggleState().ok().map(|value| value.0));
        }
        if let Ok(selection) = element
            .GetCurrentPatternAs::<IUIAutomationSelectionItemPattern>(UIA_SelectionItemPatternId)
        {
            actions.push("select");
            state["selected"] = json!(selection
                .CurrentIsSelected()
                .is_ok_and(|value| value.as_bool()));
        }
        if element
            .GetCurrentPatternAs::<IUIAutomationExpandCollapsePattern>(UIA_ExpandCollapsePatternId)
            .is_ok()
        {
            actions.extend(["expand", "collapse"]);
        }
        json!({
            "id":format!("{path}|{role}|{automation_id}|{name}"),"name":name.to_string(),
            "role":role.to_string(),"disabled":!enabled,
            "actions":if enabled {actions} else {vec![]},
            "state":state
        })
    }
}

pub fn snapshot(id: &str) -> Result<Value, String> {
    inspect(id, |elements| {
        let controls = elements
            .into_iter()
            .map(|(path, element)| details(&path, &element))
            .collect::<Vec<_>>();
        Ok(json!({"window":id,"controls":controls}))
    })
}

pub fn act(params: &Value) -> Result<(), String> {
    let id = super::field(params, "window")?;
    let control = super::field(params, "control")?;
    let action = super::field(params, "action")?;
    inspect(id, |elements| {
        let (_, element) = elements
            .into_iter()
            .find(|(path, element)| {
                let details = details(path, element);
                details["id"] == control
                    && details["actions"]
                        .as_array()
                        .is_some_and(|actions| actions.iter().any(|value| value == action))
            })
            .ok_or("原生控件已变更或不支持该操作。")?;
        unsafe {
            match action {
                "click" => element
                    .GetCurrentPatternAs::<IUIAutomationInvokePattern>(UIA_InvokePatternId)
                    .and_then(|pattern| pattern.Invoke()),
                "toggle" => element
                    .GetCurrentPatternAs::<IUIAutomationTogglePattern>(UIA_TogglePatternId)
                    .and_then(|pattern| pattern.Toggle()),
                "select" => element
                    .GetCurrentPatternAs::<IUIAutomationSelectionItemPattern>(
                        UIA_SelectionItemPatternId,
                    )
                    .and_then(|pattern| pattern.Select()),
                "expand" => element
                    .GetCurrentPatternAs::<IUIAutomationExpandCollapsePattern>(
                        UIA_ExpandCollapsePatternId,
                    )
                    .and_then(|pattern| pattern.Expand()),
                "collapse" => element
                    .GetCurrentPatternAs::<IUIAutomationExpandCollapsePattern>(
                        UIA_ExpandCollapsePatternId,
                    )
                    .and_then(|pattern| pattern.Collapse()),
                "type" => {
                    let text = BSTR::from(params["text"].as_str().ok_or("缺少原生控件输入文字。")?);
                    element
                        .GetCurrentPatternAs::<IUIAutomationValuePattern>(UIA_ValuePatternId)
                        .and_then(|pattern| pattern.SetValue(&text))
                }
                _ => return Err("未知的原生控件操作。".into()),
            }
        }
        .map_err(|error| error.to_string())
    })
}
