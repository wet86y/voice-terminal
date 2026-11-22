//! Device terminal（设备终端）：仅运行固定只读命令配置，不接受 shell 脚本或任意程序路径。
use serde_json::{json, Value};
use std::{
    collections::{HashMap, VecDeque},
    io::Read,
    process::{Command, Stdio},
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};

const MAX_SESSIONS: usize = 16; // MAX_SESSIONS（单个设备最多保留的终端会话数）。
const MAX_ACTIVE_COMMANDS: usize = 2; // MAX_ACTIVE_COMMANDS（设备上并行命令任务上限）。
const MAX_OUTPUT_BYTES: usize = 256 * 1024; // MAX_OUTPUT_BYTES（单终端会话保留输出上限）。
const MAX_READ_BYTES: usize = 32 * 1024; // MAX_READ_BYTES（单次增量读取上限）。
const COMMAND_TIMEOUT: Duration = Duration::from_secs(30); // COMMAND_TIMEOUT（单条只读命令的最长执行时间）。
const SESSION_IDLE: Duration = Duration::from_secs(30 * 60); // SESSION_IDLE（终端空闲回收时限）。

/// DeviceTerminalManager（设备终端管理器）：保留设备代次、限量输出和可取消的只读命令。
#[derive(Clone, Default)]
pub struct DeviceTerminalManager {
    inner: Arc<Inner>,
}

#[derive(Default)]
struct Inner {
    sessions: Mutex<HashMap<String, Arc<TerminalSession>>>,
    active_commands: AtomicUsize,
}

struct TerminalSession {
    connection_generation: u64,
    executor_generation: u64,
    state: Arc<Mutex<SessionState>>,
}

struct SessionState {
    created_at: Instant,
    touched_at: Instant,
    state: &'static str,
    command_id: Option<String>,
    exit_code: Option<i32>,
    output: VecDeque<u8>,
    output_base: u64,
    output_total: u64,
    cancel: Option<Arc<AtomicBool>>,
    error: Option<String>,
}

impl DeviceTerminalManager {
    /// execute（执行终端任务）：任务名、参数与设备代次均由调用端固定。
    pub fn execute(
        &self,
        operation: &str,
        arguments: &Value,
        connection_generation: u64,
        executor_generation: u64,
    ) -> Result<Value, String> {
        if connection_generation == 0 || executor_generation == 0 {
            return Err("终端任务缺少有效连接或执行器代次。".into());
        }
        let arguments = arguments
            .as_object()
            .ok_or("终端任务参数必须是 JSON（结构化数据）对象。")?;
        match operation {
            "terminal.create" => {
                exact_fields(arguments, &["terminal_id"])?;
                let terminal_id = required_id(arguments.get("terminal_id"), "terminal_id")?;
                self.create(terminal_id, connection_generation, executor_generation)
            }
            "terminal.input" => {
                exact_fields(arguments, &["terminal_id", "command_id"])?;
                let terminal_id = required_id(arguments.get("terminal_id"), "terminal_id")?;
                let command_id = arguments
                    .get("command_id")
                    .and_then(Value::as_str)
                    .ok_or("command_id（命令编号）无效。")?;
                self.start_command(
                    terminal_id,
                    command_id,
                    connection_generation,
                    executor_generation,
                )
            }
            "terminal.output" => {
                exact_fields(arguments, &["terminal_id", "offset"])?;
                let terminal_id = required_id(arguments.get("terminal_id"), "terminal_id")?;
                let offset = arguments
                    .get("offset")
                    .and_then(Value::as_u64)
                    .ok_or("offset（输出字节偏移）无效。")?;
                self.output(
                    terminal_id,
                    offset,
                    connection_generation,
                    executor_generation,
                )
            }
            "terminal.status" => {
                exact_fields(arguments, &["terminal_id"])?;
                let terminal_id = required_id(arguments.get("terminal_id"), "terminal_id")?;
                self.status(terminal_id, connection_generation, executor_generation)
            }
            "terminal.cancel" => {
                exact_fields(arguments, &["terminal_id"])?;
                let terminal_id = required_id(arguments.get("terminal_id"), "terminal_id")?;
                self.cancel(terminal_id, connection_generation, executor_generation)
            }
            "terminal.close" => {
                exact_fields(arguments, &["terminal_id"])?;
                let terminal_id = required_id(arguments.get("terminal_id"), "terminal_id")?;
                self.close(terminal_id, connection_generation, executor_generation)
            }
            _ => Err("设备终端任务不在受限操作清单中。".into()),
        }
    }

    fn create(
        &self,
        terminal_id: &str,
        connection_generation: u64,
        executor_generation: u64,
    ) -> Result<Value, String> {
        self.expire_idle();
        let mut sessions = self
            .inner
            .sessions
            .lock()
            .map_err(|_| "终端会话台账不可用。")?;
        if let Some(existing) = sessions.get(terminal_id) {
            validate_generation(existing, connection_generation, executor_generation)?;
            return Ok(session_snapshot(terminal_id, existing)?);
        }
        if sessions.len() >= MAX_SESSIONS {
            return Err("设备终端会话已达上限；请关闭已完成会话后重试。".into());
        }
        let now = Instant::now();
        let session = Arc::new(TerminalSession {
            connection_generation,
            executor_generation,
            state: Arc::new(Mutex::new(SessionState {
                created_at: now,
                touched_at: now,
                state: "ready",
                command_id: None,
                exit_code: None,
                output: VecDeque::new(),
                output_base: 0,
                output_total: 0,
                cancel: None,
                error: None,
            })),
        });
        sessions.insert(terminal_id.to_owned(), session.clone());
        session_snapshot(terminal_id, &session)
    }

    fn start_command(
        &self,
        terminal_id: &str,
        command_id: &str,
        connection_generation: u64,
        executor_generation: u64,
    ) -> Result<Value, String> {
        if !matches!(
            command_id,
            "hostname" | "identity" | "processes" | "network"
        ) {
            return Err("command_id（命令编号）不在受限只读命令清单中。".into());
        }
        let session = self.get(terminal_id, connection_generation, executor_generation)?;
        let mut state = session.state.lock().map_err(|_| "终端会话状态不可用。")?;
        if state.state == "running" {
            return Err("终端会话已有运行中的命令任务。".into());
        }
        if state.state == "closed" {
            return Err("终端会话已关闭。".into());
        }
        loop {
            let active = self.inner.active_commands.load(Ordering::Acquire);
            if active >= MAX_ACTIVE_COMMANDS {
                return Err("设备上的并行终端命令已达上限。".into());
            }
            if self
                .inner
                .active_commands
                .compare_exchange(active, active + 1, Ordering::AcqRel, Ordering::Acquire)
                .is_ok()
            {
                break;
            }
        }
        let cancel = Arc::new(AtomicBool::new(false));
        state.state = "running";
        state.command_id = Some(command_id.to_owned());
        state.exit_code = None;
        state.error = None;
        state.cancel = Some(cancel.clone());
        state.touched_at = Instant::now();
        drop(state);

        let session_for_thread = session.clone();
        let inner = self.inner.clone();
        let command_id = command_id.to_owned();
        let spawn = thread::Builder::new()
            .name("voice-device-terminal".into())
            .spawn(move || {
                let outcome = run_fixed_command(&command_id, &session_for_thread, &cancel);
                if let Ok(mut state) = session_for_thread.state.lock() {
                    state.cancel = None;
                    state.touched_at = Instant::now();
                    match outcome {
                        Ok((exit_code, cancelled, timed_out)) => {
                            state.exit_code = Some(exit_code);
                            state.state = if cancelled {
                                "cancelled"
                            } else if timed_out {
                                "timed_out"
                            } else if exit_code == 0 {
                                "completed"
                            } else {
                                "failed"
                            };
                        }
                        Err(error) => {
                            state.state = "failed";
                            state.error = Some(error.chars().take(240).collect());
                        }
                    }
                }
                inner.active_commands.fetch_sub(1, Ordering::AcqRel);
            });
        if let Err(error) = spawn {
            self.inner.active_commands.fetch_sub(1, Ordering::AcqRel);
            if let Ok(mut state) = session.state.lock() {
                state.state = "failed";
                state.cancel = None;
                state.error = Some(format!("无法启动受管终端任务：{error}"));
            }
            return Err("无法启动受管终端任务。".into());
        }
        session_snapshot(terminal_id, &session)
    }

    fn output(
        &self,
        terminal_id: &str,
        offset: u64,
        connection_generation: u64,
        executor_generation: u64,
    ) -> Result<Value, String> {
        let session = self.get(terminal_id, connection_generation, executor_generation)?;
        let mut state = session.state.lock().map_err(|_| "终端会话状态不可用。")?;
        state.touched_at = Instant::now();
        if offset < state.output_base || offset > state.output_total {
            return Err("终端输出偏移已过期或超出当前输出。".into());
        }
        let start =
            usize::try_from(offset - state.output_base).map_err(|_| "终端输出偏移无效。")?;
        if state
            .output
            .get(start)
            .is_some_and(|byte| byte & 0xc0 == 0x80)
        {
            return Err("终端输出偏移必须位于 UTF-8（统一字符编码）字符边界。".into());
        }
        let mut length = (state.output.len().saturating_sub(start)).min(MAX_READ_BYTES);
        while state
            .output
            .get(start + length)
            .is_some_and(|byte| byte & 0xc0 == 0x80)
        {
            length -= 1;
        }
        let bytes = state
            .output
            .iter()
            .skip(start)
            .take(length)
            .copied()
            .collect::<Vec<_>>();
        let next_offset = offset + length as u64;
        Ok(json!({
            "terminal_id": terminal_id,
            "state": state.state,
            "offset": offset,
            "next_offset": next_offset,
            "output": std::str::from_utf8(&bytes).map_err(|_| "终端输出编码状态无效。")?,
            "output_encoding": "UTF-8（统一字符编码）",
            "more": next_offset < state.output_total,
            "fields": {"terminal_id":"设备终端会话编号","state":"命令会话状态","offset":"请求输出字节偏移",
                "next_offset":"下次增量读取字节偏移","output":"受限长度的命令输出增量","output_encoding":"规范化后的输出编码",
                "more":"是否还有未读取输出"}
        }))
    }

    fn status(
        &self,
        terminal_id: &str,
        connection_generation: u64,
        executor_generation: u64,
    ) -> Result<Value, String> {
        let session = self.get(terminal_id, connection_generation, executor_generation)?;
        session_snapshot(terminal_id, &session)
    }

    fn cancel(
        &self,
        terminal_id: &str,
        connection_generation: u64,
        executor_generation: u64,
    ) -> Result<Value, String> {
        let session = self.get(terminal_id, connection_generation, executor_generation)?;
        let mut state = session.state.lock().map_err(|_| "终端会话状态不可用。")?;
        state.touched_at = Instant::now();
        if let Some(cancel) = &state.cancel {
            cancel.store(true, Ordering::Release);
        }
        Ok(session_snapshot_locked(terminal_id, &state))
    }

    fn close(
        &self,
        terminal_id: &str,
        connection_generation: u64,
        executor_generation: u64,
    ) -> Result<Value, String> {
        let session = self.get(terminal_id, connection_generation, executor_generation)?;
        {
            let mut state = session.state.lock().map_err(|_| "终端会话状态不可用。")?;
            if let Some(cancel) = &state.cancel {
                cancel.store(true, Ordering::Release);
            }
            state.state = "closed";
            state.touched_at = Instant::now();
            state.output.clear();
            state.output_base = state.output_total;
        }
        self.inner
            .sessions
            .lock()
            .map_err(|_| "终端会话台账不可用。")?
            .remove(terminal_id);
        Ok(
            json!({"terminal_id":terminal_id,"state":"closed","fields":{"terminal_id":"设备终端会话编号","state":"关闭状态"}}),
        )
    }

    fn get(
        &self,
        terminal_id: &str,
        connection_generation: u64,
        executor_generation: u64,
    ) -> Result<Arc<TerminalSession>, String> {
        validate_id(terminal_id)?;
        let sessions = self
            .inner
            .sessions
            .lock()
            .map_err(|_| "终端会话台账不可用。")?;
        let session = sessions
            .get(terminal_id)
            .cloned()
            .ok_or("设备终端会话不存在、已过期或执行器已重建。")?;
        validate_generation(&session, connection_generation, executor_generation)?;
        Ok(session)
    }

    fn expire_idle(&self) {
        let Ok(mut sessions) = self.inner.sessions.lock() else {
            return;
        };
        let expired = sessions
            .iter()
            .filter_map(|(id, session)| {
                let state = session.state.lock().ok()?;
                (state.state != "running" && state.touched_at.elapsed() >= SESSION_IDLE)
                    .then(|| id.clone())
            })
            .collect::<Vec<_>>();
        for id in expired {
            sessions.remove(&id);
        }
    }

    /// shutdown（关闭管理器）：取消本机正在运行的受限命令。
    pub fn shutdown(&self) {
        let Ok(sessions) = self.inner.sessions.lock() else {
            return;
        };
        for session in sessions.values() {
            if let Ok(state) = session.state.lock() {
                if let Some(cancel) = &state.cancel {
                    cancel.store(true, Ordering::Release);
                }
            }
        }
    }
}

fn exact_fields(arguments: &serde_json::Map<String, Value>, fields: &[&str]) -> Result<(), String> {
    if arguments.len() != fields.len()
        || arguments.keys().any(|key| !fields.contains(&key.as_str()))
    {
        return Err("终端任务字段不符合严格参数结构。".into());
    }
    Ok(())
}

fn required_id<'a>(value: Option<&'a Value>, field: &str) -> Result<&'a str, String> {
    let value = value
        .and_then(Value::as_str)
        .ok_or_else(|| format!("{field}（编号）无效。"))?;
    validate_id(value)?;
    Ok(value)
}

fn validate_id(value: &str) -> Result<(), String> {
    if value.is_empty()
        || value.len() > 128
        || !value.bytes().enumerate().all(|(index, byte)| {
            byte.is_ascii_alphanumeric() || (index > 0 && matches!(byte, b'.' | b'_' | b':' | b'-'))
        })
    {
        return Err("终端编号格式无效。".into());
    }
    Ok(())
}

fn validate_generation(
    session: &TerminalSession,
    connection_generation: u64,
    executor_generation: u64,
) -> Result<(), String> {
    if session.connection_generation != connection_generation
        || session.executor_generation != executor_generation
    {
        return Err("终端会话所属设备连接或执行器代次已失效。".into());
    }
    Ok(())
}

fn session_snapshot(terminal_id: &str, session: &TerminalSession) -> Result<Value, String> {
    let mut state = session.state.lock().map_err(|_| "终端会话状态不可用。")?;
    state.touched_at = Instant::now();
    Ok(session_snapshot_locked(terminal_id, &state))
}

fn session_snapshot_locked(terminal_id: &str, state: &SessionState) -> Value {
    json!({"terminal_id":terminal_id,"state":state.state,"command_id":state.command_id,
        "exit_code":state.exit_code,"output_offset":state.output_total,"created_elapsed_ms":state.created_at.elapsed().as_millis(),
        "error":state.error,"fields":{"terminal_id":"设备终端会话编号","state":"终端命令状态",
            "command_id":"最近命令配置编号","exit_code":"最近命令进程退出码","output_offset":"最新输出字节偏移",
            "created_elapsed_ms":"终端会话创建后的毫秒数","error":"受限任务执行错误"}})
}

fn append_output(state: &Arc<Mutex<SessionState>>, text: &str) {
    if text.is_empty() {
        return;
    }
    let Ok(mut state) = state.lock() else { return };
    let mut start = text.len().saturating_sub(MAX_OUTPUT_BYTES);
    while !text.is_char_boundary(start) {
        start += 1;
    }
    state.output.extend(text[start..].as_bytes());
    state.output_total = state.output_total.saturating_add(text.len() as u64);
    while state.output.len() > MAX_OUTPUT_BYTES {
        state.output.pop_front();
    }
    while state.output.front().is_some_and(|byte| byte & 0xc0 == 0x80) {
        state.output.pop_front();
    }
    state.output_base = state.output_total - state.output.len() as u64;
}

#[cfg(windows)]
fn run_fixed_command(
    command_id: &str,
    session: &TerminalSession,
    cancel: &AtomicBool,
) -> Result<(i32, bool, bool), String> {
    use std::os::windows::process::CommandExt;
    let system32 = crate::powershell::windows_root()?.join("System32");
    let (program, arguments): (&str, &[&str]) = match command_id {
        "hostname" => ("hostname.exe", &[]),
        "identity" => ("whoami.exe", &[]),
        "processes" => ("tasklist.exe", &["/FO", "CSV", "/NH"]),
        "network" => ("ipconfig.exe", &["/all"]),
        _ => return Err("终端命令配置不在只读命令清单中。".into()),
    };
    let mut command = Command::new(system32.join(program));
    command
        .args(arguments)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .creation_flags(0x0800_0000);
    let job = crate::windows::Job::new().map_err(|_| "无法建立终端命令作业对象。".to_string())?;
    let mut child = job
        .spawn(&mut command)
        .map_err(|_| "无法启动固定只读终端命令。".to_string())?;
    let stdout = child.stdout.take().ok_or("终端命令标准输出不可用。")?;
    let stderr = child.stderr.take().ok_or("终端命令诊断输出不可用。")?;
    let stdout_state = session.state.clone();
    let stderr_state = stdout_state.clone();
    let out = thread::spawn(move || copy_capped_output(stdout, stdout_state));
    let err = thread::spawn(move || copy_capped_output(stderr, stderr_state));
    let started = Instant::now();
    let mut cancelled = false;
    let mut timed_out = false;
    let status = loop {
        if cancel.load(Ordering::Acquire) {
            cancelled = true;
            job.terminate();
            let _ = child.kill();
            break child
                .wait()
                .map_err(|_| "终端命令取消后无法回收。".to_string())?;
        }
        if started.elapsed() >= COMMAND_TIMEOUT {
            timed_out = true;
            job.terminate();
            let _ = child.kill();
            break child
                .wait()
                .map_err(|_| "终端命令超时后无法回收。".to_string())?;
        }
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) => thread::sleep(Duration::from_millis(25)),
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err("终端命令状态查询失败。".into());
            }
        }
    };
    job.terminate(); // 脚本结束时也释放可能被后代保留的输出句柄。
    let _ = out.join();
    let _ = err.join();
    Ok((status.code().unwrap_or(-1), cancelled, timed_out))
}

#[cfg(windows)]
fn copy_capped_output(mut stream: impl Read, state: Arc<Mutex<SessionState>>) {
    let mut decoder = crate::system_text::Decoder::new(crate::system_text::console_code_page());
    let mut buffer = [0_u8; 4096];
    while let Ok(read) = stream.read(&mut buffer) {
        if read == 0 {
            break;
        }
        append_output(&state, &decoder.push(&buffer[..read], false));
    }
    append_output(&state, &decoder.push(&[], true));
}

#[cfg(not(windows))]
fn run_fixed_command(
    _command_id: &str,
    _session: &TerminalSession,
    _cancel: &AtomicBool,
) -> Result<(i32, bool, bool), String> {
    Err("受限终端命令目前仅支持 Windows 设备。".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(windows)]
    #[test]
    fn native_console_stream_decodes_split_chinese_before_buffering() {
        let manager = DeviceTerminalManager::default();
        manager
            .execute(
                "terminal.create",
                &json!({"terminal_id":"native-text"}),
                1,
                1,
            )
            .unwrap();
        let session = manager.get("native-text", 1, 1).unwrap();
        // 构造跨 4096 字节读取边界的系统代码页文本；不读取或保存真实网络资料。
        if crate::system_text::console_code_page() != 936 {
            return;
        }
        let mut bytes = vec![b'a'; 4095];
        bytes.extend_from_slice(&[0xd6, 0xd0, 0xce, 0xc4]);
        copy_capped_output(std::io::Cursor::new(bytes), session.state.clone());
        let result = manager
            .execute(
                "terminal.output",
                &json!({"terminal_id":"native-text","offset":0}),
                1,
                1,
            )
            .unwrap();
        assert_eq!(result["output"], "a".repeat(4095) + "中文");
    }

    #[test]
    fn terminal_unicode_reads_and_rolling_buffer_keep_character_boundaries() {
        let manager = DeviceTerminalManager::default();
        manager
            .execute("terminal.create", &json!({"terminal_id":"unicode"}), 1, 1)
            .unwrap();
        let session = manager.get("unicode", 1, 1).unwrap();
        let text = "a".repeat(MAX_READ_BYTES - 1) + "中文🙂";
        append_output(&session.state, &text);
        let first = manager
            .execute(
                "terminal.output",
                &json!({"terminal_id":"unicode","offset":0}),
                1,
                1,
            )
            .unwrap();
        assert_eq!(first["next_offset"], (MAX_READ_BYTES - 1) as u64);
        let second = manager
            .execute(
                "terminal.output",
                &json!({"terminal_id":"unicode","offset":first["next_offset"]}),
                1,
                1,
            )
            .unwrap();
        assert_eq!(second["output"], "中文🙂");
        assert!(manager
            .execute(
                "terminal.output",
                &json!({"terminal_id":"unicode","offset":MAX_READ_BYTES}),
                1,
                1
            )
            .is_err());
        append_output(&session.state, &"中".repeat(MAX_OUTPUT_BYTES));
        let state = session.state.lock().unwrap();
        assert!(state.output.len() <= MAX_OUTPUT_BYTES);
        let bytes = state.output.iter().copied().collect::<Vec<_>>();
        assert!(std::str::from_utf8(&bytes).is_ok());
        assert_eq!(state.output_base + bytes.len() as u64, state.output_total);
    }

    #[test]
    fn terminal_actions_reject_unknown_operations_and_shell_commands() {
        let manager = DeviceTerminalManager::default();
        assert!(manager
            .execute("terminal.powershell", &json!({}), 1, 1)
            .is_err());
        let created = manager
            .execute("terminal.create", &json!({"terminal_id":"term-1"}), 1, 1)
            .unwrap();
        assert_eq!(created["state"], "ready");
        assert!(manager
            .execute(
                "terminal.input",
                &json!({"terminal_id":"term-1","command_id":"powershell"}),
                1,
                1
            )
            .is_err());
        assert!(manager
            .execute(
                "terminal.status",
                &json!({"terminal_id":"term-1","extra":true}),
                1,
                1
            )
            .is_err());
    }

    #[test]
    fn terminal_sessions_are_bound_to_connection_and_executor_generations() {
        let manager = DeviceTerminalManager::default();
        manager
            .execute("terminal.create", &json!({"terminal_id":"term-2"}), 3, 4)
            .unwrap();
        assert!(manager
            .execute("terminal.status", &json!({"terminal_id":"term-2"}), 4, 4)
            .is_err());
        assert!(manager
            .execute("terminal.status", &json!({"terminal_id":"term-2"}), 3, 5)
            .is_err());
    }

    #[cfg(windows)]
    #[test]
    fn terminal_fixed_command_streams_output_and_finishes_with_status() {
        let manager = DeviceTerminalManager::default();
        manager
            .execute(
                "terminal.create",
                &json!({"terminal_id":"term-command"}),
                1,
                1,
            )
            .unwrap();
        let started = manager
            .execute(
                "terminal.input",
                &json!({"terminal_id":"term-command","command_id":"hostname"}),
                1,
                1,
            )
            .unwrap();
        assert_eq!(started["terminal_id"], "term-command");
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            let status = manager
                .execute(
                    "terminal.status",
                    &json!({"terminal_id":"term-command"}),
                    1,
                    1,
                )
                .unwrap();
            if status["state"] != "running" {
                assert_eq!(status["state"], "completed");
                assert_eq!(status["exit_code"], 0);
                break;
            }
            assert!(
                Instant::now() < deadline,
                "固定主机名命令没有在测试期限内完成"
            );
            thread::sleep(Duration::from_millis(20));
        }
        let output = manager
            .execute(
                "terminal.output",
                &json!({"terminal_id":"term-command","offset":0}),
                1,
                1,
            )
            .unwrap();
        assert!(!output["output"]
            .as_str()
            .unwrap_or_default()
            .trim()
            .is_empty());
        manager
            .execute(
                "terminal.close",
                &json!({"terminal_id":"term-command"}),
                1,
                1,
            )
            .unwrap();
    }
}
