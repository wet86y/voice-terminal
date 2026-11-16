//! Client MiniMax TTS（客户端 MiniMax 语音合成）：凭据只在本机进程内使用。

use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use std::time::Duration;
use tokio::sync::{mpsc, watch};
use tokio::task::JoinHandle;
use tokio_tungstenite::{
    MaybeTlsStream, WebSocketStream, connect_async_with_config,
    tungstenite::{
        client::IntoClientRequest,
        http::{HeaderValue, header::AUTHORIZATION},
        protocol::{Message, WebSocketConfig},
    },
};
use zeroize::{Zeroize, Zeroizing};

const MINIMAX_BIDI_URL: &str = "wss://api.minimaxi.com/ws/v1/t2a_v2_bidi"; // MiniMax Bidi（双向流式合成）端点。
const EVENT_QUEUE_DEPTH: usize = 8; // EVENT_QUEUE_DEPTH（音频与状态事件有界队列容量）。
const MAX_REQUEST_TEXT_CHARS: usize = 9_999; // MAX_REQUEST_TEXT_CHARS（MiniMax 文本长度上限）。
const MAX_KEY_BYTES: usize = 4_096; // MAX_KEY_BYTES（本机凭据最大字节数）。
const MAX_MESSAGE_BYTES: usize = 1_048_576; // MAX_MESSAGE_BYTES（云端单个 WebSocket 消息上限）。
const MAX_AUDIO_CHUNK_BYTES: usize = 262_144; // MAX_AUDIO_CHUNK_BYTES（单个 PCM 音频分块上限）。
const MAX_TOTAL_AUDIO_BYTES: u64 = 64 * 1024 * 1024; // MAX_TOTAL_AUDIO_BYTES（单次合成 PCM 总量上限）。
const MAX_ALIGNMENT_EVENTS: usize = 512; // MAX_ALIGNMENT_EVENTS（单次合成对齐事件上限）。
const CONNECT_TIMEOUT: Duration = Duration::from_secs(12); // CONNECT_TIMEOUT（连接和启动握手时限）。
const RECEIVE_TIMEOUT: Duration = Duration::from_secs(120); // RECEIVE_TIMEOUT（等待合成回执的无活动时限）。
const CANCEL_TIMEOUT: Duration = Duration::from_secs(2); // CANCEL_TIMEOUT（云端取消确认时限）。

type ClientSocket = WebSocketStream<MaybeTlsStream<tokio::net::TcpStream>>;

/// MiniMaxTtsRequest（客户端合成请求）：不含接口密钥，字段只描述一次语音事务。
#[derive(Debug, Clone)]
pub struct MiniMaxTtsRequest {
    pub task_id: String,         // task_id（核心分配且跨连接稳定的语音任务编号）。
    pub text: String,            // text（送往 MiniMax 的待合成文本）。
    pub model: String,           // model（MiniMax 合成模型编号）。
    pub voice_id: String,        // voice_id（MiniMax 音色编号）。
    pub sample_rate: u32,        // sample_rate（PCM 输出采样率）。
    pub speed: f64,              // speed（语速控制值）。
    pub volume: f64,             // volume（音量控制值）。
    pub emotion: Option<String>, // emotion（可选语音情绪）。
}

impl MiniMaxTtsRequest {
    fn validate(&self) -> Result<(), String> {
        if !valid_identifier(&self.task_id, 128)
            || !valid_identifier(&self.model, 64)
            || !valid_identifier(&self.voice_id, 128)
        {
            return Err("客户端合成任务、模型或音色编号无效。".into());
        }
        if self.text.trim().is_empty()
            || self.text.chars().count() > MAX_REQUEST_TEXT_CHARS
            || self.text.contains('\0')
        {
            return Err("客户端合成文本为空、超长或含有无效字符。".into());
        }
        if !matches!(self.sample_rate, 16_000 | 24_000 | 32_000 | 48_000) {
            return Err("MiniMax PCM 采样率必须为 16、24、32 或 48 kHz。".into());
        }
        if !self.speed.is_finite()
            || !(0.5..=2.0).contains(&self.speed)
            || !self.volume.is_finite()
            || !(0.0..=10.0).contains(&self.volume)
        {
            return Err("MiniMax 语速或音量控制值超出支持范围。".into());
        }
        if self.emotion.as_deref().is_some_and(|value| {
            !matches!(
                value,
                "calm" | "happy" | "sad" | "angry" | "fearful" | "disgusted" | "surprised"
            )
        }) {
            return Err("MiniMax 情绪控制值不受支持。".into());
        }
        Ok(())
    }
}

fn valid_identifier(value: &str, maximum_bytes: usize) -> bool {
    !value.is_empty()
        && value.len() <= maximum_bytes
        && value.trim() == value
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

/// ClientTtsEvent（客户端合成事件）：音频保持独立 PCM 数据，不能 JSON/Base64 序列化。
#[derive(Debug, Clone, PartialEq)]
pub enum ClientTtsEvent {
    Started {
        task_id: String, // task_id（已获 MiniMax task_started 确认的语音任务编号）。
    },
    Audio {
        task_id: String,   // task_id（固定语音任务编号）。
        sequence: u64,     // sequence（此任务内单调递增的音频分块序号）。
        start_sample: u64, // start_sample（此任务 PCM 时间线起始采样位置）。
        sample_count: u64, // sample_count（此 PCM 分块有效采样数）。
        sample_rate: u32,  // sample_rate（PCM 采样率）。
        pcm: Vec<u8>,      // pcm（二进制小端 int16 单声道 PCM 数据）。
    },
    ProviderEvent {
        task_id: String,            // task_id（固定语音任务编号）。
        event: String,              // event（MiniMax 句子边界或流式回执类型）。
        sample_cursor: u64,         // sample_cursor（事件发生时已接收 PCM 的样本位置）。
        sentence_index: u32,        // sentence_index（当前句子从零开始的序号）。
        sentence_start_sample: u64, // sentence_start_sample（当前句子的 PCM 起始采样位置）。
        alignment: Option<Value>,   // alignment（提供方原始对齐片段，经结构边界验证）。
    },
    SegmentFinished {
        task_id: String,   // task_id（固定语音任务编号）。
        text_chars: usize, // text_chars（本次文本分段的 Unicode 字符数）。
        end_sample: u64,   // end_sample（文本分段最终 PCM 采样位置）。
    },
    Completed {
        task_id: String,    // task_id（固定语音任务编号）。
        total_samples: u64, // total_samples（云端确认完成的 PCM 样本数）。
    },
    Cancelled {
        task_id: String, // task_id（已获云端取消确认的语音任务编号）。
    },
    Failed {
        task_id: String, // task_id（失败语音任务编号）。
        error: String,   // error（不含密钥或请求正文的诊断）。
    },
}

/// MiniMaxTtsHandle（客户端合成句柄）：队列有界，丢弃句柄会请求云端取消。
pub struct MiniMaxTtsHandle {
    events: mpsc::Receiver<ClientTtsEvent>, // events（有界音频/控制回执队列）。
    cancel: watch::Sender<bool>,            // cancel（本机取消信号，不携带文本或凭据）。
    task: Option<JoinHandle<()>>,           // task（拥有唯一 WebSocket 的受管异步任务）。
}

/// MiniMaxTtsCancellation（受管取消信号）：由媒体通道持有，不暴露提供方套接字。
#[derive(Clone)]
pub struct MiniMaxTtsCancellation {
    cancel: watch::Sender<bool>, // cancel（通知唯一合成任务执行有确认的云端取消）。
}

impl MiniMaxTtsCancellation {
    /// request_cancel（请求取消）：真正结果仍由事件流中的 Cancelled/Failed 回执决定。
    pub fn request_cancel(&self) {
        let _ = self.cancel.send(true);
    }
}

impl MiniMaxTtsHandle {
    /// start（启动本机在线合成）：api_key（接口密钥）只保留在原生任务内。
    pub fn start(request: MiniMaxTtsRequest, api_key: Zeroizing<String>) -> Result<Self, String> {
        Self::start_at_endpoint(request, api_key, MINIMAX_BIDI_URL)
    }

    /// start_with_local_credential（使用客户端本机凭据启动）：密钥只在原生库内读取和保留。
    pub fn start_with_local_credential(
        request: MiniMaxTtsRequest,
        data_root: &std::path::Path,
    ) -> Result<Self, String> {
        let api_key = crate::credentials::load_provider_credential(data_root, "minimax")?;
        Self::start(request, api_key)
    }

    fn start_at_endpoint(
        request: MiniMaxTtsRequest,
        api_key: Zeroizing<String>,
        endpoint: &str,
    ) -> Result<Self, String> {
        request.validate()?;
        validate_api_key(&api_key)?;
        if !(endpoint.starts_with("wss://") || cfg!(test) && endpoint.starts_with("ws://")) {
            return Err("客户端 MiniMax WebSocket 端点必须使用 TLS。".into());
        }
        let runtime = tokio::runtime::Handle::try_current()
            .map_err(|_| "客户端在线合成必须由受管异步运行时启动。".to_string())?;
        let (event_sender, events) = mpsc::channel(EVENT_QUEUE_DEPTH);
        let (cancel, cancel_receiver) = watch::channel(false);
        let task_id = request.task_id.clone();
        let endpoint = endpoint.to_owned();
        let task = runtime.spawn(async move {
            match run_synthesis(
                request,
                api_key,
                &endpoint,
                cancel_receiver,
                event_sender.clone(),
            )
            .await
            {
                Ok(RunResult::Completed {
                    task_id,
                    total_samples,
                }) => {
                    let _ = event_sender
                        .send(ClientTtsEvent::Completed {
                            task_id,
                            total_samples,
                        })
                        .await;
                }
                Ok(RunResult::Cancelled { task_id }) => {
                    let _ = event_sender
                        .send(ClientTtsEvent::Cancelled { task_id })
                        .await;
                }
                Err(error) => {
                    let _ = event_sender
                        .send(ClientTtsEvent::Failed { task_id, error })
                        .await;
                }
            }
        });
        Ok(Self {
            events,
            cancel,
            task: Some(task),
        })
    }

    /// recv（读取下一个客户端合成事件）：None 表示受管合成任务已收尾。
    pub async fn recv(&mut self) -> Option<ClientTtsEvent> {
        self.events.recv().await
    }

    /// cancellation_handle（取得受管取消信号副本）：连接任务可在独立接收循环中取消。
    pub fn cancellation_handle(&self) -> MiniMaxTtsCancellation {
        MiniMaxTtsCancellation {
            cancel: self.cancel.clone(),
        }
    }

    /// request_cancel（请求取消）：取消结果仍须等待 Cancelled（云端确认）或 Failed（结果未知）。
    pub fn request_cancel(&self) {
        let _ = self.cancel.send(true);
    }

    /// join（收拢合成任务）：只在终态事件被消费后调用。
    pub async fn join(&mut self) -> Result<(), String> {
        let Some(task) = self.task.take() else {
            return Ok(());
        };
        task.await
            .map_err(|_| "客户端语音合成任务异常退出。".to_string())
    }

    /// abort_after_cancel_timeout（取消确认超时后回收）：只在媒体连接关闭收尾时使用。
    pub async fn abort_after_cancel_timeout(&mut self) {
        self.request_cancel();
        if let Some(task) = self.task.take() {
            task.abort();
            let _ = task.await;
        }
    }
}

impl Drop for MiniMaxTtsHandle {
    fn drop(&mut self) {
        let _ = self.cancel.send(true);
    }
}

/// ClientTtsPcmFrame（客户端合成 PCM 帧）：供媒体协议添加设备身份与代次后发送。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClientTtsPcmFrame {
    pub sequence: u64,     // sequence（该请求内单调递增帧序号）。
    pub start_sample: u64, // start_sample（该请求 PCM 时间线位置）。
    pub sample_count: u64, // sample_count（帧内有效采样数）。
    pub sample_rate: u32,  // sample_rate（PCM 采样率）。
    pub pcm: Vec<u8>,      // pcm（二进制小端 int16 单声道 PCM）。
}

/// ClientTtsPcmFramer（客户端 PCM 分帧器）：缓冲区只留一个短于 20 ms 的尾块。
pub struct ClientTtsPcmFramer {
    sample_rate: u32,   // sample_rate（固定 PCM 采样率）。
    frame_bytes: usize, // frame_bytes（一个 20 ms 帧的字节数）。
    pending: Vec<u8>,   // pending（尚未凑满 20 ms 的有界 PCM 尾部）。
    next_sequence: u64, // next_sequence（下一个帧序号）。
    next_sample: u64,   // next_sample（下一个帧的起始采样位置）。
}

impl ClientTtsPcmFramer {
    /// new（创建分帧器）：保持在线合成的原生采样率，不重采样。
    pub fn new(sample_rate: u32) -> Result<Self, String> {
        if !matches!(sample_rate, 16_000 | 24_000 | 32_000 | 48_000) {
            return Err("客户端合成 PCM 采样率必须为 16、24、32 或 48 kHz。".into());
        }
        Ok(Self {
            sample_rate,
            frame_bytes: (sample_rate as usize / 50) * 2,
            pending: Vec::new(),
            next_sequence: 0,
            next_sample: 0,
        })
    }

    /// push（追加合成 PCM）：返回完整 20 ms 帧，余量只保留至下次调用。
    pub fn push(&mut self, pcm: &[u8]) -> Result<Vec<ClientTtsPcmFrame>, String> {
        if pcm.len() > MAX_AUDIO_CHUNK_BYTES || pcm.len() % 2 != 0 {
            return Err("MiniMax PCM 分块长度超过上限或不是完整 int16 样本。".into());
        }
        let mut combined = Vec::with_capacity(self.pending.len() + pcm.len());
        combined.extend_from_slice(&self.pending);
        combined.extend_from_slice(pcm);
        self.pending.clear();
        let mut cursor = 0_usize;
        let mut frames = Vec::with_capacity(combined.len() / self.frame_bytes);
        while combined.len() - cursor >= self.frame_bytes {
            let block = combined[cursor..cursor + self.frame_bytes].to_vec();
            frames.push(self.make_frame(block));
            cursor += self.frame_bytes;
        }
        self.pending.extend_from_slice(&combined[cursor..]);
        Ok(frames)
    }

    /// finish（结束分段）：保留最后不足 20 ms 的有效样本数，不补造静音。
    pub fn finish(&mut self) -> Option<ClientTtsPcmFrame> {
        if self.pending.is_empty() {
            return None;
        }
        let tail = std::mem::take(&mut self.pending);
        Some(self.make_frame(tail))
    }

    fn make_frame(&mut self, pcm: Vec<u8>) -> ClientTtsPcmFrame {
        let sample_count = (pcm.len() / 2) as u64;
        let frame = ClientTtsPcmFrame {
            sequence: self.next_sequence,
            start_sample: self.next_sample,
            sample_count,
            sample_rate: self.sample_rate,
            pcm,
        };
        self.next_sequence = self.next_sequence.saturating_add(1);
        self.next_sample = self.next_sample.saturating_add(sample_count);
        frame
    }
}

fn validate_api_key(api_key: &str) -> Result<(), String> {
    if api_key.trim().is_empty() || api_key.len() > MAX_KEY_BYTES || api_key.contains('\0') {
        return Err("本机 MiniMax 接口密钥为空或无效。".into());
    }
    Ok(())
}

enum RunResult {
    Completed { task_id: String, total_samples: u64 },
    Cancelled { task_id: String },
}

enum Incoming {
    Json(Value),
    Cancelled,
}

async fn run_synthesis(
    request: MiniMaxTtsRequest,
    api_key: Zeroizing<String>,
    endpoint: &str,
    mut cancel: watch::Receiver<bool>,
    event_sender: mpsc::Sender<ClientTtsEvent>,
) -> Result<RunResult, String> {
    let mut websocket_request = endpoint
        .into_client_request()
        .map_err(|_| "客户端 MiniMax WebSocket 请求格式无效。".to_string())?;
    let mut bearer = Zeroizing::new(format!("Bearer {}", api_key.as_str()));
    let mut authorization = HeaderValue::from_str(&bearer)
        .map_err(|_| "本机 MiniMax 接口密钥不能用于 WebSocket 身份验证。".to_string())?;
    authorization.set_sensitive(true);
    websocket_request
        .headers_mut()
        .insert(AUTHORIZATION, authorization);

    let config = WebSocketConfig::default()
        .max_message_size(Some(MAX_MESSAGE_BYTES))
        .max_frame_size(Some(MAX_MESSAGE_BYTES));
    let connect = connect_async_with_config(websocket_request, Some(config), false);
    let connection = tokio::select! {
        changed = cancel.changed() => {
            let _ = changed;
            return Ok(RunResult::Cancelled { task_id: request.task_id });
        }
        outcome = tokio::time::timeout(CONNECT_TIMEOUT, connect) => {
            outcome.map_err(|_| "MiniMax WebSocket 连接超时。".to_string())?
                .map_err(|_| "MiniMax WebSocket 连接失败；未记录密钥或请求内容。".to_string())?
        }
    };
    bearer.zeroize();
    drop(api_key);
    let (mut socket, _response) = connection;

    let connected = match next_json_or_cancel(&mut socket, &mut cancel).await? {
        Incoming::Cancelled => {
            close_socket(&mut socket).await;
            return Ok(RunResult::Cancelled {
                task_id: request.task_id,
            });
        }
        Incoming::Json(value) => value,
    };
    if event_name(&connected) == Some("task_failed") {
        return Err(provider_error(&connected));
    }
    if event_name(&connected) != Some("connected_success") {
        return Err("MiniMax 未返回 connected_success 握手回执。".into());
    }

    let mut start_payload = json!({
        "event":"task_start",
        "model":request.model,
        "language_boost":"Chinese",
        "voice_setting":{
            "voice_id":request.voice_id,
            "speed":request.speed,
            "vol":request.volume,
            "pitch":0,
        },
        "audio_setting":{
            "sample_rate":request.sample_rate,
            "bitrate":128000,
            "format":"pcm",
            "channel":1,
        },
        "subtitle_enable":true,
        "subtitle_type":"word_streaming",
    });
    if let Some(emotion) = request.emotion.as_deref() {
        start_payload["voice_setting"]["emotion"] = json!(emotion);
    }
    send_json(&mut socket, &start_payload).await?;
    loop {
        match next_json_or_cancel(&mut socket, &mut cancel).await? {
            Incoming::Cancelled => {
                close_socket(&mut socket).await;
                return Ok(RunResult::Cancelled {
                    task_id: request.task_id,
                });
            }
            Incoming::Json(value) if event_name(&value) == Some("task_started") => break,
            Incoming::Json(value) if event_name(&value) == Some("task_failed") => {
                return Err(provider_error(&value));
            }
            Incoming::Json(_) => {}
        }
    }

    if !emit_or_cancel(
        &event_sender,
        &mut cancel,
        ClientTtsEvent::Started {
            task_id: request.task_id.clone(),
        },
    )
    .await
    {
        cancel_minimax_task(&mut socket).await?;
        return Ok(RunResult::Cancelled {
            task_id: request.task_id,
        });
    }

    send_json(
        &mut socket,
        &json!({"event":"task_continue","text":request.text}),
    )
    .await?;
    send_json(&mut socket, &json!({"event":"task_flush"})).await?;

    let mut total_bytes = 0_u64;
    let mut sequence = 0_u64;
    let mut sentence_index = 0_u32;
    let mut sentence_start_sample = 0_u64;
    let mut alignment_events = 0_usize;
    let mut segment_finished = false;
    loop {
        let response = match next_json_or_cancel(&mut socket, &mut cancel).await? {
            Incoming::Json(response) => response,
            Incoming::Cancelled => {
                cancel_minimax_task(&mut socket).await?;
                return Ok(RunResult::Cancelled {
                    task_id: request.task_id,
                });
            }
        };
        let event = event_name(&response).unwrap_or("");
        if event == "task_failed" {
            return Err(provider_error(&response));
        }
        if event == "task_canceled" {
            return Ok(RunResult::Cancelled {
                task_id: request.task_id,
            });
        }
        if event == "sentence_start" {
            sentence_index = sentence_index.saturating_add(1);
            sentence_start_sample = total_bytes / 2;
        }
        let alignment = extract_alignment(&response);
        if event == "sentence_start" || event == "sentence_end" || alignment.is_some() {
            alignment_events = alignment_events.saturating_add(usize::from(alignment.is_some()));
            if alignment_events > MAX_ALIGNMENT_EVENTS {
                return Err("MiniMax 字词对齐事件超过本机安全上限。".into());
            }
            let provider_event = ClientTtsEvent::ProviderEvent {
                task_id: request.task_id.clone(),
                event: event.to_owned(),
                sample_cursor: total_bytes / 2,
                sentence_index: sentence_index.saturating_sub(1),
                sentence_start_sample,
                alignment,
            };
            if !emit_or_cancel(&event_sender, &mut cancel, provider_event).await {
                cancel_minimax_task(&mut socket).await?;
                return Ok(RunResult::Cancelled {
                    task_id: request.task_id,
                });
            }
        }

        if let Some(audio_hex) = response["data"]["audio"]
            .as_str()
            .filter(|audio| !audio.is_empty())
        {
            let pcm = decode_pcm_hex(audio_hex)?;
            total_bytes = total_bytes.saturating_add(pcm.len() as u64);
            if total_bytes > MAX_TOTAL_AUDIO_BYTES {
                return Err("MiniMax PCM 音频总量超过本机安全上限。".into());
            }
            let sample_count = (pcm.len() / 2) as u64;
            let audio = ClientTtsEvent::Audio {
                task_id: request.task_id.clone(),
                sequence,
                start_sample: total_bytes / 2 - sample_count,
                sample_count,
                sample_rate: request.sample_rate,
                pcm,
            };
            if !emit_or_cancel(&event_sender, &mut cancel, audio).await {
                cancel_minimax_task(&mut socket).await?;
                return Ok(RunResult::Cancelled {
                    task_id: request.task_id,
                });
            }
            sequence = sequence.saturating_add(1);
        }

        if response["is_final"] == true && !segment_finished {
            segment_finished = true;
            let finished = ClientTtsEvent::SegmentFinished {
                task_id: request.task_id.clone(),
                text_chars: request.text.chars().count(),
                end_sample: total_bytes / 2,
            };
            if !emit_or_cancel(&event_sender, &mut cancel, finished).await {
                cancel_minimax_task(&mut socket).await?;
                return Ok(RunResult::Cancelled {
                    task_id: request.task_id,
                });
            }
        }
        if event == "task_flushed" {
            if !segment_finished {
                return Err("MiniMax 未确认当前文本分段完成。".into());
            }
            break;
        }
    }

    send_json(&mut socket, &json!({"event":"task_finish"})).await?;
    loop {
        match next_json_or_cancel(&mut socket, &mut cancel).await? {
            Incoming::Cancelled => {
                cancel_minimax_task(&mut socket).await?;
                return Ok(RunResult::Cancelled {
                    task_id: request.task_id,
                });
            }
            Incoming::Json(value) if event_name(&value) == Some("task_finished") => break,
            Incoming::Json(value) if event_name(&value) == Some("task_failed") => {
                return Err(provider_error(&value));
            }
            Incoming::Json(_) => {}
        }
    }
    close_socket(&mut socket).await;
    Ok(RunResult::Completed {
        task_id: request.task_id,
        total_samples: total_bytes / 2,
    })
}

async fn emit_or_cancel(
    sender: &mpsc::Sender<ClientTtsEvent>,
    cancel: &mut watch::Receiver<bool>,
    event: ClientTtsEvent,
) -> bool {
    if *cancel.borrow() {
        return false;
    }
    tokio::select! {
        changed = cancel.changed() => {
            let _ = changed;
            false
        }
        result = sender.send(event) => result.is_ok(),
    }
}

async fn next_json_or_cancel(
    socket: &mut ClientSocket,
    cancel: &mut watch::Receiver<bool>,
) -> Result<Incoming, String> {
    loop {
        if *cancel.borrow() {
            return Ok(Incoming::Cancelled);
        }
        tokio::select! {
            changed = cancel.changed() => {
                let _ = changed;
                if *cancel.borrow() {
                    return Ok(Incoming::Cancelled);
                }
            }
            received = tokio::time::timeout(RECEIVE_TIMEOUT, socket.next()) => {
                let message = received
                    .map_err(|_| "等待 MiniMax Bidi 响应超时。".to_string())?
                    .ok_or_else(|| "MiniMax WebSocket 已提前关闭。".to_string())?
                    .map_err(|_| "读取 MiniMax WebSocket 消息失败。".to_string())?;
                match message {
                    Message::Text(text) => {
                        let value = serde_json::from_str(text.as_ref())
                            .map_err(|_| "MiniMax 返回了无效 JSON。".to_string())?;
                        return Ok(Incoming::Json(value));
                    }
                    Message::Binary(bytes) => {
                        let text = std::str::from_utf8(&bytes)
                            .map_err(|_| "MiniMax 返回了非文本 Bidi 消息。".to_string())?;
                        let value = serde_json::from_str(text)
                            .map_err(|_| "MiniMax 返回了无效 JSON。".to_string())?;
                        return Ok(Incoming::Json(value));
                    }
                    Message::Ping(payload) => {
                        tokio::time::timeout(CONNECT_TIMEOUT, socket.send(Message::Pong(payload)))
                            .await
                            .map_err(|_| "MiniMax WebSocket 心跳响应超时。".to_string())?
                            .map_err(|_| "MiniMax WebSocket 心跳响应失败。".to_string())?;
                    }
                    Message::Pong(_) | Message::Frame(_) => {}
                    Message::Close(_) => return Err("MiniMax 在任务确认前关闭了 WebSocket。".into()),
                }
            }
        }
    }
}

async fn send_json(socket: &mut ClientSocket, value: &Value) -> Result<(), String> {
    let text =
        serde_json::to_string(value).map_err(|_| "客户端 MiniMax 请求编码失败。".to_string())?;
    tokio::time::timeout(CONNECT_TIMEOUT, socket.send(Message::Text(text.into())))
        .await
        .map_err(|_| "发送 MiniMax 合成控制消息超时。".to_string())?
        .map_err(|_| "发送 MiniMax 合成控制消息失败。".to_string())
}

async fn cancel_minimax_task(socket: &mut ClientSocket) -> Result<(), String> {
    send_json(socket, &json!({"event":"task_cancel"})).await?;
    let confirmation = tokio::time::timeout(CANCEL_TIMEOUT, async {
        loop {
            let message = socket
                .next()
                .await
                .ok_or_else(|| "MiniMax 取消时 WebSocket 已关闭。".to_string())?
                .map_err(|_| "读取 MiniMax 取消确认失败。".to_string())?;
            let value = match message {
                Message::Text(text) => serde_json::from_str(text.as_ref()).ok(),
                Message::Binary(bytes) => std::str::from_utf8(&bytes)
                    .ok()
                    .and_then(|text| serde_json::from_str(text).ok()),
                Message::Ping(payload) => {
                    socket
                        .send(Message::Pong(payload))
                        .await
                        .map_err(|_| "MiniMax 取消心跳响应失败。".to_string())?;
                    None
                }
                Message::Pong(_) | Message::Frame(_) => None,
                Message::Close(_) => return Err("MiniMax 取消结果未知：连接提前关闭。".into()),
            };
            let Some(value) = value else { continue };
            match event_name(&value) {
                Some("task_canceled") => return Ok(()),
                Some("task_failed") => return Err(provider_error(&value)),
                _ => {}
            }
        }
    })
    .await
    .map_err(|_| "MiniMax 未确认取消，当前合成结果未知。".to_string())?;
    confirmation?;
    close_socket(socket).await;
    Ok(())
}

async fn close_socket(socket: &mut ClientSocket) {
    let _ = tokio::time::timeout(Duration::from_secs(1), socket.close(None)).await;
}

fn event_name(value: &Value) -> Option<&str> {
    value.get("event").and_then(Value::as_str)
}

fn provider_error(value: &Value) -> String {
    let code = value["base_resp"]["status_code"]
        .as_i64()
        .or_else(|| value["status_code"].as_i64());
    match code {
        Some(code) => format!("MiniMax 在线合成失败（错误码 {code}）。"),
        None => "MiniMax 在线合成失败；提供方未返回错误编号。".into(),
    }
}

fn extract_alignment(value: &Value) -> Option<Value> {
    const KEYS: [&str; 5] = [
        "subtitle",
        "subtitles",
        "alignment",
        "timestamps",
        "word_timestamps",
    ];
    for key in KEYS {
        if let Some(payload) = value.get(key) {
            return Some(json!({"source":key,"value":payload,"event":event_name(value)}));
        }
    }
    for key in KEYS {
        if let Some(payload) = value.get("data").and_then(|data| data.get(key)) {
            return Some(json!({"source":key,"value":payload,"event":event_name(value)}));
        }
    }
    None
}

fn decode_pcm_hex(encoded: &str) -> Result<Vec<u8>, String> {
    if encoded.len() > MAX_AUDIO_CHUNK_BYTES * 2 || encoded.len() % 4 != 0 {
        return Err("MiniMax PCM 十六进制音频分块长度无效。".into());
    }
    let mut pcm = Vec::with_capacity(encoded.len() / 2);
    for pair in encoded.as_bytes().chunks_exact(2) {
        let high = hex_nibble(pair[0])
            .ok_or_else(|| "MiniMax PCM 音频含有非十六进制字符。".to_string())?;
        let low = hex_nibble(pair[1])
            .ok_or_else(|| "MiniMax PCM 音频含有非十六进制字符。".to_string())?;
        pcm.push((high << 4) | low);
    }
    Ok(pcm)
}

fn hex_nibble(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::net::TcpListener;
    use tokio_tungstenite::{
        accept_hdr_async,
        tungstenite::handshake::server::{Request as ServerRequest, Response as ServerResponse},
    };

    fn request(text: &str) -> MiniMaxTtsRequest {
        MiniMaxTtsRequest {
            task_id: "speech-test-1".into(),
            text: text.into(),
            model: "speech-2.8-hd".into(),
            voice_id: "female-shaonv".into(),
            sample_rate: 32_000,
            speed: 1.0,
            volume: 1.0,
            emotion: Some("calm".into()),
        }
    }

    #[test]
    fn rejects_invalid_requests_and_pcm_hex_before_network_access() {
        assert!(request("").validate().is_err());
        assert!(
            request(&"x".repeat(MAX_REQUEST_TEXT_CHARS + 1))
                .validate()
                .is_err()
        );
        let mut invalid_rate = request("你好");
        invalid_rate.sample_rate = 44_100;
        assert!(invalid_rate.validate().is_err());
        let mut invalid_speed = request("你好");
        invalid_speed.speed = f64::NAN;
        assert!(invalid_speed.validate().is_err());
        assert_eq!(decode_pcm_hex("01000200").unwrap(), [1, 0, 2, 0]);
        assert!(decode_pcm_hex("abc").is_err());
        assert!(decode_pcm_hex("0100zz00").is_err());
        assert!(decode_pcm_hex(&"00".repeat(MAX_AUDIO_CHUNK_BYTES + 1)).is_err());
    }

    #[test]
    fn pcm_framer_emits_twenty_millisecond_frames_and_an_exact_tail() {
        let mut framer = ClientTtsPcmFramer::new(16_000).unwrap();
        assert!(framer.push(&[1; 200]).unwrap().is_empty());
        assert!(framer.push(&[2; 300]).unwrap().is_empty());
        let frames = framer.push(&[3; 200]).unwrap();
        assert_eq!(frames.len(), 1);
        assert_eq!(frames[0].sequence, 0);
        assert_eq!(frames[0].start_sample, 0);
        assert_eq!(frames[0].sample_count, 320);
        assert_eq!(frames[0].pcm.len(), 640);
        let tail = framer.finish().unwrap();
        assert_eq!(tail.sequence, 1);
        assert_eq!(tail.start_sample, 320);
        assert_eq!(tail.sample_count, 30);
        assert_eq!(tail.pcm.len(), 60);
        assert!(framer.finish().is_none());
    }

    #[test]
    fn pcm_framer_rejects_bad_rates_and_partial_int16_bytes() {
        assert!(ClientTtsPcmFramer::new(44_100).is_err());
        let mut framer = ClientTtsPcmFramer::new(48_000).unwrap();
        assert!(framer.push(&[1, 2, 3]).is_err());
    }

    #[tokio::test]
    async fn streams_pcm_and_alignment_with_task_ids_and_secret_only_in_auth_header() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut socket = accept_hdr_async(
                stream,
                |request: &ServerRequest, response: ServerResponse| {
                    assert_eq!(request.uri().path(), "/ws/v1/t2a_v2_bidi");
                    assert_eq!(
                        request
                            .headers()
                            .get(AUTHORIZATION)
                            .and_then(|value| value.to_str().ok()),
                        Some("Bearer local-test-secret"),
                    );
                    Ok(response)
                },
            )
            .await
            .unwrap();
            socket
                .send(Message::Text(
                    json!({"event":"connected_success"}).to_string().into(),
                ))
                .await
                .unwrap();
            let start = socket.next().await.unwrap().unwrap().into_text().unwrap();
            let start: Value = serde_json::from_str(start.as_ref()).unwrap();
            assert_eq!(start["event"], "task_start");
            assert_eq!(start["audio_setting"]["sample_rate"], 32_000);
            assert_eq!(start["audio_setting"]["format"], "pcm");
            assert!(!start.to_string().contains("local-test-secret"));
            socket
                .send(Message::Text(
                    json!({"event":"task_started"}).to_string().into(),
                ))
                .await
                .unwrap();
            let text = socket.next().await.unwrap().unwrap().into_text().unwrap();
            let text: Value = serde_json::from_str(text.as_ref()).unwrap();
            assert_eq!(text["event"], "task_continue");
            assert_eq!(text["text"], "你好");
            assert!(!text.to_string().contains("local-test-secret"));
            let flush = socket.next().await.unwrap().unwrap().into_text().unwrap();
            let flush: Value = serde_json::from_str(flush.as_ref()).unwrap();
            assert_eq!(flush["event"], "task_flush");
            socket
                .send(Message::Text(
                    json!({"event":"sentence_start"}).to_string().into(),
                ))
                .await
                .unwrap();
            socket.send(Message::Text(json!({
                "event":"task_continued", "is_final":true,
                "data":{"audio":"01000200","subtitle":{"text":"你好","start_time":0,"end_time":20}}
            }).to_string().into())).await.unwrap();
            socket
                .send(Message::Text(
                    json!({"event":"task_flushed"}).to_string().into(),
                ))
                .await
                .unwrap();
            let finish = socket.next().await.unwrap().unwrap().into_text().unwrap();
            let finish: Value = serde_json::from_str(finish.as_ref()).unwrap();
            assert_eq!(finish["event"], "task_finish");
            socket
                .send(Message::Text(
                    json!({"event":"task_finished"}).to_string().into(),
                ))
                .await
                .unwrap();
        });

        let mut handle = MiniMaxTtsHandle::start_at_endpoint(
            request("你好"),
            Zeroizing::new("local-test-secret".to_owned()),
            &format!("ws://{address}/ws/v1/t2a_v2_bidi"),
        )
        .unwrap();
        let mut audio_seen = false;
        let mut alignment_seen = false;
        let mut segment_seen = false;
        let mut complete_seen = false;
        while let Some(event) = handle.recv().await {
            match event {
                ClientTtsEvent::Audio {
                    task_id,
                    sequence,
                    start_sample,
                    sample_count,
                    sample_rate,
                    pcm,
                } => {
                    assert_eq!(task_id, "speech-test-1");
                    assert_eq!(sequence, 0);
                    assert_eq!(start_sample, 0);
                    assert_eq!(sample_count, 2);
                    assert_eq!(sample_rate, 32_000);
                    assert_eq!(pcm, [1, 0, 2, 0]);
                    audio_seen = true;
                }
                ClientTtsEvent::ProviderEvent {
                    task_id,
                    event,
                    sentence_index,
                    alignment,
                    ..
                } => {
                    assert_eq!(task_id, "speech-test-1");
                    if event == "sentence_start" {
                        assert_eq!(sentence_index, 0);
                    }
                    if alignment.is_some() {
                        alignment_seen = true;
                    }
                }
                ClientTtsEvent::Started { task_id } => assert_eq!(task_id, "speech-test-1"),
                ClientTtsEvent::SegmentFinished {
                    text_chars,
                    end_sample,
                    ..
                } => {
                    assert_eq!(text_chars, 2);
                    assert_eq!(end_sample, 2);
                    segment_seen = true;
                }
                ClientTtsEvent::Completed {
                    task_id,
                    total_samples,
                } => {
                    assert_eq!(task_id, "speech-test-1");
                    assert_eq!(total_samples, 2);
                    complete_seen = true;
                }
                ClientTtsEvent::Cancelled { .. } => panic!("此合成不应被取消"),
                ClientTtsEvent::Failed { error, .. } => panic!("客户端 MiniMax 流失败：{error}"),
            }
        }
        handle.join().await.unwrap();
        server.await.unwrap();
        assert!(audio_seen && alignment_seen && segment_seen && complete_seen);
    }

    #[tokio::test]
    async fn cancellation_waits_for_the_cloud_task_canceled_receipt() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let address = listener.local_addr().unwrap();
        let (flush_seen_sender, flush_seen_receiver) = tokio::sync::oneshot::channel();
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut socket = accept_hdr_async(
                stream,
                |_request: &ServerRequest, response: ServerResponse| Ok(response),
            )
            .await
            .unwrap();
            socket
                .send(Message::Text(
                    json!({"event":"connected_success"}).to_string().into(),
                ))
                .await
                .unwrap();
            let _start = socket.next().await.unwrap().unwrap();
            socket
                .send(Message::Text(
                    json!({"event":"task_started"}).to_string().into(),
                ))
                .await
                .unwrap();
            let _text = socket.next().await.unwrap().unwrap();
            let _flush = socket.next().await.unwrap().unwrap();
            let _ = flush_seen_sender.send(());
            let cancel = socket.next().await.unwrap().unwrap().into_text().unwrap();
            let cancel: Value = serde_json::from_str(cancel.as_ref()).unwrap();
            assert_eq!(cancel["event"], "task_cancel");
            socket
                .send(Message::Text(
                    json!({"event":"task_canceled"}).to_string().into(),
                ))
                .await
                .unwrap();
        });
        let mut handle = MiniMaxTtsHandle::start_at_endpoint(
            request("你好"),
            Zeroizing::new("test-secret".to_owned()),
            &format!("ws://{address}/ws/v1/t2a_v2_bidi"),
        )
        .unwrap();
        flush_seen_receiver.await.unwrap();
        handle.request_cancel();
        let mut cancelled = false;
        while let Some(event) = handle.recv().await {
            match event {
                ClientTtsEvent::Cancelled { task_id } => {
                    assert_eq!(task_id, "speech-test-1");
                    cancelled = true;
                }
                ClientTtsEvent::Failed { error, .. } => panic!("取消意外失败：{error}"),
                ClientTtsEvent::Audio { .. } => panic!("取消后不应再交付合成音频"),
                ClientTtsEvent::Started { .. } => {}
                _ => {}
            }
        }
        handle.join().await.unwrap();
        server.await.unwrap();
        assert!(cancelled);
    }
}
