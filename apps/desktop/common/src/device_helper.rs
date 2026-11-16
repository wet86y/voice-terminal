//! Device helper（设备桌面辅助进程）：一条有界管道串行处理桌面操作。
use serde_json::{json, Value};
use std::{
    io::{Read, Write},
    path::Path,
    process::{Child, ChildStdin, Command, Stdio},
    sync::mpsc::{self, Receiver},
    thread::{self, JoinHandle},
    time::Duration,
};

const MAX_REQUEST: usize = 256 * 1024; // MAX_REQUEST（请求最大字节数）。
const MAX_RESPONSE: usize = 8 * 1024 * 1024; // MAX_RESPONSE（回执最大字节数）。
pub(crate) const ALLOWED_OPS: &[&str] = &[
    "health",
    "windows",
    "apps",
    "snapshot",
    "shot",
    "tree",
    "click",
    "type",
    "scroll",
    "key",
    "move",
    "windowControl",
    "invokePattern",
    "readText",
    "readTable",
    "clipboard",
    "process",
    "displays",
    "cursorPosition",
    "notify",
];
// launch（启动应用）暂不开放：受管作业关闭会结束其子进程，须先实现独立的应用进程生命周期。

/// supports_control_operation（支持受管操作）：不允许通过桌面控制通道调用健康或任意脚本入口。
pub(crate) fn supports_control_operation(operation: &str) -> bool {
    ALLOWED_OPS.contains(&operation) && operation != "health"
}

/// require_interactive_session（检查交互会话）：Session 0（会话零）没有用户桌面。
fn require_interactive_session(session_id: u32) -> Result<(), String> {
    if session_id == 0 {
        Err("当前进程位于 Session 0（会话零），没有可用的交互式桌面。".into())
    } else {
        Ok(())
    }
}

/// DeviceHelper（设备辅助进程）：调用者必须在动作前完成设备授权与观察复核。
pub struct DeviceHelper {
    #[cfg(windows)]
    job: crate::windows::Job, // job（受管作业）：拥有辅助进程及其受管后代。
    child: Child,
    input: Option<ChildStdin>,
    responses: Option<Receiver<Result<Vec<u8>, String>>>,
    reader: Option<JoinHandle<()>>,
    failed: bool,
}

impl DeviceHelper {
    /// start（启动）：绝对路径必须指向已由程序包校验的辅助脚本。
    #[cfg(windows)]
    pub fn start(helper: &Path) -> Result<Self, String> {
        use std::os::windows::process::CommandExt;
        use windows_sys::Win32::System::RemoteDesktop::ProcessIdToSessionId;
        let mut session_id = 0_u32;
        if unsafe { ProcessIdToSessionId(std::process::id(), &mut session_id) } == 0 {
            return Err(format!(
                "无法读取当前 Windows（操作系统）会话编号：{}",
                std::io::Error::last_os_error()
            ));
        }
        require_interactive_session(session_id)?;
        if !helper.is_absolute() {
            return Err("设备辅助脚本路径无效".into());
        }
        let metadata = std::fs::symlink_metadata(helper)
            .map_err(|error| format!("无法读取设备辅助脚本：{error}"))?;
        if crate::workspace::metadata_reparse(&metadata) || !metadata.is_file() {
            return Err("设备辅助脚本必须是普通文件，不能是重解析点。".into());
        }
        let helper = crate::canonical_path(helper)
            .map_err(|error| format!("无法解析设备辅助脚本：{error}"))?;
        let root = std::env::var_os("SystemRoot").ok_or("无法定位 Windows 系统目录")?;
        let powershell = Path::new(&root).join("System32/WindowsPowerShell/v1.0/powershell.exe");
        if !powershell.is_file() {
            return Err("系统 PowerShell（命令行）不可用".into());
        }
        let job =
            crate::windows::Job::new().map_err(|error| format!("无法建立设备进程作业：{error}"))?;
        let mut command = Command::new(powershell);
        command
            .args(["-NoProfile", "-NonInteractive", "-STA", "-File"])
            .arg(helper)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .creation_flags(0x0800_0000); // CREATE_NO_WINDOW（不创建控制台窗口）。
        let mut child = job
            .spawn(&mut command)
            .map_err(|error| format!("无法启动设备辅助进程：{error}"))?;
        let input = match child.stdin.take() {
            Some(value) => value,
            None => {
                let _ = child.kill();
                let _ = child.wait();
                return Err("设备输入管道不可用".into());
            }
        };
        let mut output = match child.stdout.take() {
            Some(value) => value,
            None => {
                let _ = child.kill();
                let _ = child.wait();
                return Err("设备输出管道不可用".into());
            }
        };
        let (sender, receiver) = mpsc::sync_channel(1);
        let reader = thread::spawn(move || loop {
            let mut prefix = [0_u8; 4];
            if output.read_exact(&mut prefix).is_err() {
                let _ = sender.send(Err("设备辅助进程输出已关闭".into()));
                break;
            }
            let length = u32::from_be_bytes(prefix) as usize;
            if length == 0 || length > MAX_RESPONSE {
                let _ = sender.send(Err("设备辅助进程回执长度无效".into()));
                break;
            }
            let mut bytes = vec![0; length];
            if output.read_exact(&mut bytes).is_err() {
                let _ = sender.send(Err("设备辅助进程回执不完整".into()));
                break;
            }
            if sender.send(Ok(bytes)).is_err() {
                break;
            }
        });
        Ok(Self {
            job,
            child,
            input: Some(input),
            responses: Some(receiver),
            reader: Some(reader),
            failed: false,
        })
    }

    /// start（启动）：非 Windows 环境不提供桌面执行器。
    #[cfg(not(windows))]
    pub fn start(_helper: &Path) -> Result<Self, String> {
        Err("设备辅助进程仅支持 Windows".into())
    }

    /// call（请求）：超时或管道异常后结束辅助进程，调用方须将已派发动作标记为未知。
    pub fn call(
        &mut self,
        request_id: &str,
        operation: &str,
        arguments: &Value,
        timeout: Duration,
    ) -> Result<Value, String> {
        if self.failed {
            return Err("设备辅助进程已失效".into());
        }
        if request_id.is_empty()
            || request_id.len() > 128
            || !request_id.bytes().enumerate().all(|(index, byte)| {
                byte.is_ascii_alphanumeric()
                    || (index > 0 && matches!(byte, b'.' | b'_' | b':' | b'-'))
            })
            || !ALLOWED_OPS.contains(&operation)
            || timeout.is_zero()
            || timeout > Duration::from_secs(600)
        {
            return Err("设备请求编号、操作或超时无效".into());
        }
        let request = serde_json::to_vec(&json!({"protocol_version": 1, "request_id": request_id,
            "op": operation, "args": arguments}))
        .map_err(|_| "设备请求无法编码".to_owned())?;
        if request.is_empty() || request.len() > MAX_REQUEST {
            return Err("设备请求超过大小限制".into());
        }
        let sent = self
            .input
            .as_mut()
            .ok_or("设备输入管道已关闭")
            .and_then(|input| {
                input
                    .write_all(&(request.len() as u32).to_be_bytes())
                    .and_then(|_| input.write_all(&request))
                    .and_then(|_| input.flush())
                    .map_err(|_| "设备请求写入失败")
            });
        if sent.is_err() {
            self.stop();
            return Err("设备请求结果未知：输入管道失效".into());
        }
        let bytes = match self
            .responses
            .as_ref()
            .ok_or("设备输出管道已关闭")?
            .recv_timeout(timeout)
        {
            Ok(Ok(value)) => value,
            _ => {
                self.stop();
                return Err("设备请求结果未知：回执超时或管道失效".into());
            }
        };
        let response: Value = match serde_json::from_slice(&bytes) {
            Ok(value) => value,
            Err(_) => {
                self.stop();
                return Err("设备请求结果未知：回执无效".into());
            }
        };
        if response.get("protocol_version").and_then(Value::as_u64) != Some(1)
            || response.get("request_id").and_then(Value::as_str) != Some(request_id)
            || response.get("ok").and_then(Value::as_bool).is_none()
        {
            self.stop();
            return Err("设备请求结果未知：回执与请求不匹配".into());
        }
        Ok(response)
    }

    /// stop（停止）：关闭输入、结束进程并回收管道读取线程。
    pub fn stop(&mut self) {
        if self.failed {
            return;
        }
        self.failed = true;
        self.input.take();
        self.responses.take(); // 解除有界通道的发送阻塞。
        #[cfg(windows)]
        self.job.terminate(); // 确保后代不再持有管道，避免停止时读取线程悬挂。
        let _ = self.child.kill();
        let _ = self.child.wait();
        if let Some(reader) = self.reader.take() {
            let _ = reader.join();
        }
    }
}

impl Drop for DeviceHelper {
    fn drop(&mut self) {
        self.stop();
    }
}

#[cfg(all(test, windows))]
mod tests {
    use super::*;

    #[test]
    fn managed_helper_reuses_pipe_and_rejects_unscoped_powershell() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .ancestors()
            .nth(3)
            .unwrap();
        let helper = root.join("integrations/dsh/voice-terminal/native/win32/device-helper.ps1");
        let mut child = DeviceHelper::start(&helper).unwrap();
        let health = child
            .call("health-1", "health", &json!({}), Duration::from_secs(30))
            .unwrap();
        assert_eq!(health["ok"], true);
        assert!(child
            .call(
                "bad",
                "powershell",
                &json!({"script": "exit 99"}),
                Duration::from_secs(1)
            )
            .is_err());
        assert!(child
            .call("launch", "launch", &json!({}), Duration::from_secs(1))
            .is_err());
        assert_eq!(
            child
                .call("health-2", "health", &json!({}), Duration::from_secs(3))
                .unwrap()["ok"],
            true
        );
        child.stop();
    }

    #[test]
    fn timeout_closes_helper_and_does_not_retry() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .ancestors()
            .nth(3)
            .unwrap();
        let helper = root.join("integrations/dsh/voice-terminal/native/win32/device-helper.ps1");
        let mut child = DeviceHelper::start(&helper).unwrap();
        let error = child
            .call("health-1", "health", &json!({}), Duration::from_nanos(1))
            .unwrap_err();
        assert!(error.contains("结果未知"));
        assert!(child
            .call("health-1", "health", &json!({}), Duration::from_secs(1))
            .is_err());
    }
}

#[cfg(test)]
mod session_tests {
    use super::require_interactive_session;

    #[test]
    fn session_zero_is_rejected_and_user_sessions_are_allowed() {
        let error = require_interactive_session(0).unwrap_err();
        assert!(error.contains("交互式桌面"));
        assert!(require_interactive_session(1).is_ok());
    }
}
