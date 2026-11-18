//! Client direct TTS（客户端直放语音合成）：服务端临时授权，PCM 只进入客户端原生播放器。
use serde_json::{json, Map, Value};
use std::{
    collections::{BTreeMap, HashMap, HashSet},
    future::Future,
    pin::Pin,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio::sync::mpsc;
use voice_desktop_common::{
    client_tts::{
        ClientTtsEvent, ClientTtsPcmFramer, MiniMaxTtsAuthorization, MiniMaxTtsCancellation,
        MiniMaxTtsCredential, MiniMaxTtsHandle, MiniMaxTtsRequest,
    },
    device_protocol::{
        ClientTtsAuthorizationMetadata, ClientTtsPlaybackProgress, MEDIA_PROTOCOL_VERSION,
    },
};

use crate::MediaWriteCommand;

pub(super) const MAX_ACTIVE_REQUESTS: usize = 1; // MAX_ACTIVE_REQUESTS（每个媒体租约同时处理的直放分段数）。
pub(super) const MAX_SEEN_REQUESTS: usize = 4096; // MAX_SEEN_REQUESTS（单媒体租约去重的请求编号上限）。
const MIN_AUDIO_OUTPUT_MS: u64 = 80; // MIN_AUDIO_OUTPUT_MS（有效音频输出的最短设备确认时长）。
const OUTPUT_PROGRESS_INTERVAL: Duration = Duration::from_millis(100); // OUTPUT_PROGRESS_INTERVAL（实际输出进度回执间隔）。
const OUTPUT_COMPLETION_TIMEOUT: Duration = Duration::from_secs(120); // OUTPUT_COMPLETION_TIMEOUT（等待真实设备输出完成的时限）。
const MAX_CONTROL_BYTES: usize = 16 * 1024; // MAX_CONTROL_BYTES（含单次凭据的媒体直放控制消息上限）。
const MAX_ALIGNMENT_BYTES: usize = 2048; // MAX_ALIGNMENT_BYTES（单条对齐数据的字节上限）。

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MediaIdentity {
    pub device_id: String,           // device_id（已配对客户端设备编号）。
    pub core_session_id: String,     // core_session_id（共享主机权威核心运行编号）。
    pub connection_generation: u64,  // connection_generation（SSH 设备连接代次）。
    pub executor_generation: u64,    // executor_generation（设备工作进程代次）。
    pub interaction_generation: u64, // interaction_generation（主机授予的语音占用代次）。
    pub stream_id: String,           // stream_id（当前 SSH 连续音频流编号）。
}

#[derive(Clone)]
pub struct ParsedRequest {
    pub authorization_id: String, // authorization_id（服务端签发的临时授权编号）。
    pub playback_key: String,     // playback_key（固定播放事务编号）。
    pub request_id: String,       // request_id（播放事务内单个文本分段编号）。
    pub segment_index: u64,       // segment_index（播放事务内从零递增的文本分段序号）。
    pub playback_start_sample: u64, // playback_start_sample（本段应接续的播放事务累计采样位置）。
    pub request: MiniMaxTtsRequest, // request（不含接口密钥的 MiniMax 合成参数）。
}

pub struct ParsedCancel {
    pub authorization_id: String, // authorization_id（被撤销或取消操作关联的授权编号）。
    pub playback_key: String,     // playback_key（固定播放事务编号）。
    pub request_id: String,       // request_id（需停止的分段请求编号）。
    pub segment_index: u64,       // segment_index（需停止的分段序号）。
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct PlaybackSequence {
    next_segment_index: u64, // next_segment_index（设备接纳的下一个分段序号）。
    next_sample: u64,        // next_sample（设备接纳的下一个播放事务累计采样位置）。
    active: bool,            // active（当前是否已有该播放键的音频生产者）。
    terminal: bool,          // terminal（当前播放键是否已取消、失败或撤销）。
}

struct AuthorizationEntry {
    grant: MiniMaxTtsAuthorization, // grant（带过期时间和清零密钥的临时授权）。
    playback: Arc<Mutex<PlaybackSequence>>, // playback（防止同键重复生产的序号台账）。
    request_ids: HashSet<String>,   // request_ids（此授权已处理的分段请求编号）。
}

/// ClientTtsAuthorizationStore（客户端临时授权台账）：关闭连接或接管时必须调用 clear。
pub struct ClientTtsAuthorizationStore {
    by_id: HashMap<String, AuthorizationEntry>, // by_id（授权编号到临时密钥及播放台账）。
    max_seen_requests: usize,                   // max_seen_requests（请求去重记录上限）。
}

impl Default for ClientTtsAuthorizationStore {
    fn default() -> Self {
        Self {
            by_id: HashMap::new(),
            max_seen_requests: MAX_SEEN_REQUESTS,
        }
    }
}

impl ClientTtsAuthorizationStore {
    pub fn new(max_seen_requests: usize) -> Result<Self, String> {
        if !(1..=MAX_SEEN_REQUESTS).contains(&max_seen_requests) {
            return Err("客户端直放请求去重上限无效。".into());
        }
        Ok(Self {
            by_id: HashMap::new(),
            max_seen_requests,
        })
    }

    pub fn authorize(&mut self, grant: MiniMaxTtsAuthorization) -> Result<(), String> {
        if self.by_id.contains_key(&grant.authorization_id)
            || self
                .by_id
                .values()
                .any(|entry| entry.grant.playback_key == grant.playback_key)
        {
            return Err("客户端拒绝重复授权编号或重复绑定播放事务。".into());
        }
        let authorization_id = grant.authorization_id.clone();
        self.by_id.insert(
            authorization_id,
            AuthorizationEntry {
                grant,
                playback: Arc::new(Mutex::new(PlaybackSequence {
                    next_segment_index: 0,
                    next_sample: 0,
                    active: false,
                    terminal: false,
                })),
                request_ids: HashSet::new(),
            },
        );
        Ok(())
    }

    /// revoke（撤销授权）：drop 受 Zeroizing 管理的密钥；调用方还须按返回键取消正在执行的请求。
    pub fn revoke(
        &mut self,
        authorization_id: &str,
        playback_key: &str,
    ) -> Result<Option<String>, String> {
        let Some(entry) = self.by_id.get(authorization_id) else {
            return Ok(None);
        };
        if entry.grant.playback_key != playback_key {
            return Err("临时授权撤销消息的播放事务编号不匹配。".into());
        }
        let playback_key = entry.grant.playback_key.clone();
        if let Ok(mut playback) = entry.playback.lock() {
            playback.terminal = true;
        }
        self.by_id.remove(authorization_id);
        Ok(Some(playback_key))
    }

    /// clear（清除全部授权）：断线和新客户端接管的清理入口。
    pub fn clear(&mut self) {
        for entry in self.by_id.values() {
            if let Ok(mut playback) = entry.playback.lock() {
                playback.terminal = true;
            }
        }
        self.by_id.clear();
    }

    fn begin_request(
        &mut self,
        request: &ParsedRequest,
    ) -> Result<(MiniMaxTtsCredential, RequestPermit), String> {
        if self
            .by_id
            .values()
            .map(|entry| entry.request_ids.len())
            .sum::<usize>()
            >= self.max_seen_requests
        {
            return Err("客户端直放请求去重台账已满。".into());
        }
        if self
            .by_id
            .get(&request.authorization_id)
            .is_some_and(|entry| entry.grant.is_expired())
        {
            self.by_id.remove(&request.authorization_id);
            return Err("客户端 MiniMax 临时授权已过期。".into());
        }
        let entry = self
            .by_id
            .get_mut(&request.authorization_id)
            .ok_or("客户端没有服务端授权的 MiniMax 临时凭据。")?;
        if entry.grant.playback_key != request.playback_key {
            return Err("客户端合成请求与临时授权的固定播放事务不匹配。".into());
        }
        if !entry.request_ids.insert(request.request_id.clone()) {
            return Err("客户端合成分段请求编号重复，未创建第二个音频生产者。".into());
        }
        let mut sequence = entry
            .playback
            .lock()
            .map_err(|_| "客户端播放事务台账不可用。")?;
        if sequence.terminal
            || sequence.active
            || sequence.next_segment_index != request.segment_index
            || sequence.next_sample != request.playback_start_sample
        {
            entry.request_ids.remove(&request.request_id);
            return Err("客户端拒绝旧播放代次、重复生产或非连续分段。".into());
        }
        sequence.active = true;
        let permit = RequestPermit {
            playback: entry.playback.clone(),
            request_id: request.request_id.clone(),
            segment_index: request.segment_index,
            start_sample: request.playback_start_sample,
            settled: false,
        };
        drop(sequence);
        let credential = entry
            .grant
            .credential_for_request(&request.authorization_id, &request.playback_key)?;
        Ok((credential, permit))
    }
}

impl Drop for ClientTtsAuthorizationStore {
    fn drop(&mut self) {
        self.clear();
    }
}

struct RequestPermit {
    playback: Arc<Mutex<PlaybackSequence>>, // playback（该键唯一的生产顺序状态）。
    request_id: String,                     // request_id（此生产者的唯一分段编号）。
    segment_index: u64,                     // segment_index（此生产者持有的分段顺序）。
    start_sample: u64,                      // start_sample（此生产者持有的累计采样起点）。
    settled: bool,                          // settled（成功输出完成或终止状态已记账）。
}

impl RequestPermit {
    fn complete(mut self, sample_count: u64) -> Result<u64, String> {
        if sample_count == 0 {
            self.fail();
            return Err("客户端合成没有产生可播放的非空 PCM。".into());
        }
        let end_sample = self
            .start_sample
            .checked_add(sample_count)
            .ok_or("客户端播放事务采样位置溢出。")?;
        let playback_state = self.playback.clone();
        let mut playback = playback_state
            .lock()
            .map_err(|_| "客户端播放事务台账不可用。")?;
        if !playback.active
            || playback.terminal
            || playback.next_segment_index != self.segment_index
            || playback.next_sample != self.start_sample
        {
            return Err("客户端播放事务在输出期间已被撤销或替换。".into());
        }
        playback.next_segment_index = playback.next_segment_index.saturating_add(1);
        playback.next_sample = end_sample;
        playback.active = false;
        self.settled = true;
        Ok(end_sample)
    }

    fn fail(&mut self) {
        let playback_state = self.playback.clone();
        if let Ok(mut playback) = playback_state.lock() {
            playback.active = false;
            playback.terminal = true;
        }
        self.settled = true;
    }
}

impl Drop for RequestPermit {
    fn drop(&mut self) {
        if !self.settled {
            self.fail();
        }
    }
}

pub type PlaybackFuture<T> = Pin<Box<dyn Future<Output = Result<T, String>> + Send + 'static>>; // PlaybackFuture（可跨线程的播放器回调 Future）。

pub type EnqueuePcmCallback = Arc<dyn Fn(String, u32, Vec<u8>) -> PlaybackFuture<()> + Send + Sync>; // EnqueuePcmCallback（按播放键提交本地 PCM 的受管回调）。
pub type PlaybackProgressCallback =
    Arc<dyn Fn(String, u32) -> PlaybackFuture<ClientTtsPlaybackProgress> + Send + Sync>; // PlaybackProgressCallback（读取按键设备输出时钟快照的回调）。
pub type StopPlaybackCallback =
    Arc<dyn Fn(String) -> PlaybackFuture<ClientTtsPlaybackProgress> + Send + Sync>; // StopPlaybackCallback（按键停止并确认输出结果的回调）。

/// DirectPlaybackCallbacks（客户端原生播放器回调）：PCM 不离开客户端进程。
#[derive(Clone)]
pub struct DirectPlaybackCallbacks {
    pub enqueue_pcm: EnqueuePcmCallback, // enqueue_pcm（按固定播放事务加入既有原生播放器）。
    pub progress: PlaybackProgressCallback, // progress（读取该播放键在设备时钟上的实际输出进度）。
    pub stop_and_snapshot: StopPlaybackCallback, // stop_and_snapshot（按键停止并读取真实设备结果）。
}

pub fn parse_authorization(
    value: &mut Value,
    identity: &MediaIdentity,
) -> Result<MiniMaxTtsAuthorization, String> {
    let keys = [
        "type",
        "protocol_version",
        "device_id",
        "core_session_id",
        "connection_generation",
        "executor_generation",
        "interaction_generation",
        "stream_id",
        "authorization_id",
        "playback_key",
        "provider",
        "ttl_ms",
        "credential",
        "fields",
    ];
    validate_envelope(value, "media.tts.authorize", identity, &keys)?;
    let credential = match value
        .as_object_mut()
        .and_then(|object| object.get_mut("credential"))
    {
        Some(slot) => match std::mem::replace(slot, Value::Null) {
            Value::String(secret) => secret,
            _ => return Err("服务端 MiniMax 临时凭据字段无效。".into()),
        },
        None => return Err("服务端 MiniMax 授权缺少临时凭据。".into()),
    };
    let metadata = ClientTtsAuthorizationMetadata {
        authorization_id: string_field(value, "authorization_id")?,
        playback_key: string_field(value, "playback_key")?,
        provider: string_field(value, "provider")?,
        ttl_ms: value["ttl_ms"]
            .as_u64()
            .ok_or("客户端合成授权时限字段无效。")?,
    };
    let authorization = MiniMaxTtsAuthorization::new(
        metadata.authorization_id.clone(),
        metadata.playback_key.clone(),
        metadata.ttl_ms,
        credential,
    )?;
    metadata.validate().map_err(str::to_owned)?;
    Ok(authorization)
}

pub fn parse_request(value: &Value, identity: &MediaIdentity) -> Result<ParsedRequest, String> {
    let keys = [
        "type",
        "protocol_version",
        "device_id",
        "core_session_id",
        "connection_generation",
        "executor_generation",
        "interaction_generation",
        "stream_id",
        "authorization_id",
        "playback_key",
        "request_id",
        "segment_index",
        "playback_start_sample",
        "text",
        "model",
        "voice_id",
        "sample_rate",
        "speed",
        "volume",
        "emotion",
        "fields",
    ];
    validate_envelope(value, "media.tts.request", identity, &keys)?;
    let authorization_id = string_field(value, "authorization_id")?;
    let playback_key = string_field(value, "playback_key")?;
    let request_id = string_field(value, "request_id")?;
    validate_id(&authorization_id, "authorization_id")?;
    validate_playback_key(&playback_key)?;
    validate_id(&request_id, "request_id")?;
    let segment_index = safe_integer(value, "segment_index")?;
    let playback_start_sample = safe_integer(value, "playback_start_sample")?;
    let sample_rate = value["sample_rate"]
        .as_u64()
        .and_then(|rate| u32::try_from(rate).ok())
        .ok_or("客户端合成采样率字段无效。")?;
    let speed = value["speed"].as_f64().ok_or("客户端语速字段无效。")?;
    let volume = value["volume"].as_f64().ok_or("客户端音量字段无效。")?;
    let emotion = match &value["emotion"] {
        Value::Null => None,
        Value::String(value) => Some(value.clone()),
        _ => return Err("客户端合成情绪字段无效。".into()),
    };
    Ok(ParsedRequest {
        authorization_id,
        playback_key,
        request_id: request_id.clone(),
        segment_index,
        playback_start_sample,
        request: MiniMaxTtsRequest {
            task_id: request_id,
            text: string_field(value, "text")?,
            model: string_field(value, "model")?,
            voice_id: string_field(value, "voice_id")?,
            sample_rate,
            speed,
            volume,
            emotion,
        },
    })
}

pub fn parse_cancel(value: &Value, identity: &MediaIdentity) -> Result<ParsedCancel, String> {
    let keys = [
        "type",
        "protocol_version",
        "device_id",
        "core_session_id",
        "connection_generation",
        "executor_generation",
        "interaction_generation",
        "stream_id",
        "authorization_id",
        "playback_key",
        "request_id",
        "segment_index",
        "reason",
        "fields",
    ];
    validate_envelope(value, "media.tts.cancel", identity, &keys)?;
    let cancel = ParsedCancel {
        authorization_id: string_field(value, "authorization_id")?,
        playback_key: string_field(value, "playback_key")?,
        request_id: string_field(value, "request_id")?,
        segment_index: safe_integer(value, "segment_index")?,
    };
    validate_id(&cancel.authorization_id, "authorization_id")?;
    validate_playback_key(&cancel.playback_key)?;
    validate_id(&cancel.request_id, "request_id")?;
    let reason = string_field(value, "reason")?;
    if reason.len() > 64
        || !matches!(
            reason.as_str(),
            "stop" | "replace" | "interrupt" | "authorization_revoked"
        )
    {
        return Err("客户端直放取消原因字段无效。".into());
    }
    Ok(cancel)
}

pub fn parse_revocation(
    value: &Value,
    identity: &MediaIdentity,
) -> Result<(String, String), String> {
    let keys = [
        "type",
        "protocol_version",
        "device_id",
        "core_session_id",
        "connection_generation",
        "executor_generation",
        "interaction_generation",
        "stream_id",
        "authorization_id",
        "playback_key",
        "fields",
    ];
    validate_envelope(value, "media.tts.authorization_revoked", identity, &keys)?;
    let authorization_id = string_field(value, "authorization_id")?;
    let playback_key = string_field(value, "playback_key")?;
    validate_id(&authorization_id, "authorization_id")?;
    validate_playback_key(&playback_key)?;
    Ok((authorization_id, playback_key))
}

pub async fn send_authorization_accepted(
    writer: &mpsc::Sender<MediaWriteCommand>,
    identity: &MediaIdentity,
    authorization_id: &str,
    playback_key: &str,
) -> Result<(), String> {
    send_control(
        writer,
        identity,
        "media.tts.authorization_accepted",
        json!({
            "authorization_id": authorization_id,
            "playback_key": playback_key,
        }),
        labels(&[
            ("authorization_id", "服务端签发的临时授权编号"),
            ("playback_key", "授权固定绑定的播放事务编号"),
        ]),
    )
    .await
}

pub async fn send_start_failure(
    writer: &mpsc::Sender<MediaWriteCommand>,
    identity: &MediaIdentity,
    request: &ParsedRequest,
    error: &str,
) -> Result<(), String> {
    send_request_status(
        writer,
        identity,
        request,
        "media.tts.accepted",
        json!({
            "accepted": false,
            "error": safe_error(error),
        }),
        labels(&[
            ("accepted", "客户端是否接纳此播放分段"),
            ("error", "不含凭据或请求正文的错误摘要"),
        ]),
    )
    .await
}

pub async fn start_request(
    parsed: ParsedRequest,
    identity: MediaIdentity,
    authorizations: &mut ClientTtsAuthorizationStore,
    playback: DirectPlaybackCallbacks,
    writer: mpsc::Sender<MediaWriteCommand>,
    finished: mpsc::UnboundedSender<String>,
) -> Result<(MiniMaxTtsCancellation, tauri::async_runtime::JoinHandle<()>), String> {
    let (credential, permit) = authorizations.begin_request(&parsed)?;
    let mut handle = MiniMaxTtsHandle::start(parsed.request.clone(), credential)?;
    let cancellation = handle.cancellation_handle();
    send_request_status(
        &writer,
        &identity,
        &parsed,
        "media.tts.accepted",
        json!({"accepted":true}),
        labels(&[("accepted", "客户端已固定接纳此播放事务分段")]),
    )
    .await?;
    let request_id = parsed.request_id.clone();
    let task = tauri::async_runtime::spawn(async move {
        if forward_events(&mut handle, &parsed, &identity, &playback, &writer, permit)
            .await
            .is_err()
        {
            handle.request_cancel();
            let stop_receipt = (playback.stop_and_snapshot)(parsed.playback_key.clone())
                .await
                .ok()
                .filter(|progress| validate_progress(progress, &parsed.playback_key).is_ok());
            let mut failure = json!({
                "error":"客户端合成或设备输出失败，未取得完整输出回执。",
            });
            let mut failure_labels = labels(&[("error", "不含凭据或请求正文的固定失败摘要")]);
            if let Some(progress) = stop_receipt.as_ref() {
                merge_object(&mut failure, progress_json(progress));
                failure_labels.extend(progress_labels());
            }
            let _ = send_request_status(
                &writer,
                &identity,
                &parsed,
                "media.tts.failed",
                failure,
                failure_labels,
            )
            .await;
            let mut terminal = false;
            while !terminal {
                match tokio::time::timeout(Duration::from_secs(3), handle.recv()).await {
                    Ok(Some(
                        ClientTtsEvent::Completed { .. }
                        | ClientTtsEvent::Cancelled { .. }
                        | ClientTtsEvent::Failed { .. },
                    )) => terminal = true,
                    Ok(Some(_)) => {}
                    Ok(None) | Err(_) => break,
                }
            }
            if !terminal {
                handle.abort_after_cancel_timeout().await;
                let _ = finished.send(request_id);
                return;
            }
        }
        let _ = handle.join().await;
        let _ = finished.send(request_id);
    });
    Ok((cancellation, task))
}

async fn forward_events(
    handle: &mut MiniMaxTtsHandle,
    request: &ParsedRequest,
    identity: &MediaIdentity,
    playback: &DirectPlaybackCallbacks,
    writer: &mpsc::Sender<MediaWriteCommand>,
    mut permit: RequestPermit,
) -> Result<(), String> {
    let mut framer = ClientTtsPcmFramer::new(request.request.sample_rate)?;
    let mut generated_samples = 0_u64;
    let mut provider_total_samples = None;
    let mut segment_finished = None;
    let mut provider_terminal = false;
    let mut progress_sequence = 0_u64;
    let mut last_progress = None;
    let mut progress_tick = tokio::time::interval(OUTPUT_PROGRESS_INTERVAL);
    progress_tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

    let initial_progress =
        (playback.progress)(request.playback_key.clone(), request.request.sample_rate).await?;
    validate_progress(&initial_progress, &request.playback_key)?;
    if initial_progress.accepted_samples != request.playback_start_sample {
        return Err("客户端播放器接纳位置与固定播放事务的分段起点不一致。".into());
    }

    while !provider_terminal {
        tokio::select! {
            event = handle.recv() => {
                match event.ok_or("MiniMax 合成事件流在终态回执前关闭。")? {
                    ClientTtsEvent::Started { task_id } => {
                        if task_id != request.request_id { return Err("MiniMax 合成事件任务编号不匹配。".into()); }
                        send_request_status(writer, identity, request, "media.tts.started", Value::Null, labels(&[])).await?;
                    }
                    ClientTtsEvent::Audio { task_id, sample_rate, pcm, .. } => {
                        if task_id != request.request_id || sample_rate != request.request.sample_rate
                            || pcm.is_empty() || pcm.len() % 2 != 0
                        { return Err("MiniMax PCM 分段编号、采样率或长度无效。".into()); }
                        let samples = u64::try_from(pcm.len() / 2).map_err(|_| "MiniMax PCM 样本数溢出。")?;
                        generated_samples = generated_samples.checked_add(samples).ok_or("MiniMax PCM 样本位置溢出。")?;
                        for frame in framer.push(&pcm)? {
                            enqueue_frame(playback, request, frame.sample_rate, frame.pcm).await?;
                        }
                    }
                    ClientTtsEvent::ProviderEvent { task_id, event, sample_cursor, sentence_index,
                        sentence_start_sample, alignment } => {
                        if task_id != request.request_id || sample_cursor > generated_samples
                            || sentence_start_sample > sample_cursor
                        { return Err("MiniMax 对齐事件编号或样本位置无效。".into()); }
                        let mut bounded_alignment = alignment.unwrap_or(Value::Null);
                        if serde_json::to_vec(&bounded_alignment).map_or(true, |bytes| bytes.len() > MAX_ALIGNMENT_BYTES) {
                            bounded_alignment = Value::Null;
                        }
                        send_request_status(writer, identity, request, "media.tts.alignment", json!({
                            "event":clip(&event, 64),
                            "sample_cursor":request.playback_start_sample.saturating_add(sample_cursor),
                            "generated_samples":generated_samples,
                            "sentence_index":sentence_index,
                            "sentence_start_sample":request.playback_start_sample.saturating_add(sentence_start_sample),
                            "alignment":bounded_alignment,
                        }), labels(&[
                            ("event", "MiniMax 提供方事件类型"),
                            ("sample_cursor", "播放事务累计 PCM 样本位置"),
                            ("generated_samples", "客户端本段已经生成的 PCM 样本数"),
                            ("sentence_index", "句子顺序编号"),
                            ("sentence_start_sample", "播放事务累计句子起始采样位置"),
                            ("alignment", "经边界约束的字词对齐数据"),
                        ])).await?;
                    }
                    ClientTtsEvent::SegmentFinished { task_id, text_chars, end_sample } => {
                        if task_id != request.request_id || end_sample != generated_samples || end_sample == 0 {
                            return Err("MiniMax 文本段完成位置与实际 PCM 采样数不一致。".into());
                        }
                        if let Some(frame) = framer.finish() {
                            enqueue_frame(playback, request, frame.sample_rate, frame.pcm).await?;
                        }
                        if end_sample < minimum_samples(request.request.sample_rate) {
                            return Err("MiniMax 合成音频短于最小有效设备输出时长。".into());
                        }
                        segment_finished = Some((text_chars, end_sample));
                        send_request_status(writer, identity, request, "media.tts.segment_finished", json!({
                            "text_chars":text_chars,
                            "start_sample":request.playback_start_sample,
                            "end_sample":request.playback_start_sample.saturating_add(end_sample),
                        }), labels(&[
                            ("text_chars", "已确认文本字符数"),
                            ("start_sample", "播放事务累计本段起始采样位置"),
                            ("end_sample", "播放事务累计本段结束采样位置"),
                        ])).await?;
                    }
                    ClientTtsEvent::Completed { task_id, total_samples } => {
                        if task_id != request.request_id || total_samples != generated_samples
                            || segment_finished.is_none()
                        { return Err("MiniMax 合成完成回执与分段 PCM 数量不匹配。".into()); }
                        provider_total_samples = Some(total_samples);
                        provider_terminal = true;
                    }
                    ClientTtsEvent::Cancelled { task_id } => {
                        if task_id != request.request_id { return Err("MiniMax 取消回执任务编号不匹配。".into()); }
                        let progress = (playback.stop_and_snapshot)(request.playback_key.clone()).await?;
                        validate_progress(&progress, &request.playback_key)?;
                        send_request_status(writer, identity, request, "media.tts.cancelled", progress_json(&progress), progress_labels()).await?;
                        permit.fail();
                        return Ok(());
                    }
                    ClientTtsEvent::Failed { task_id, .. } => {
                        if task_id != request.request_id { return Err("MiniMax 失败回执任务编号不匹配。".into()); }
                        return Err("MiniMax 客户端合成失败。".into());
                    }
                }
            }
            _ = progress_tick.tick() => {
                if generated_samples > 0 {
                    send_output_progress(playback, writer, identity, request, generated_samples,
                        &mut progress_sequence, &mut last_progress).await?;
                }
            }
        }
    }

    let total_samples = provider_total_samples.ok_or("MiniMax 未确认有效 PCM 合成完成。")?;
    let end_sample = request
        .playback_start_sample
        .checked_add(total_samples)
        .ok_or("客户端播放事务结束采样位置溢出。")?;
    let deadline = Instant::now() + OUTPUT_COMPLETION_TIMEOUT;
    let mut last_device_progress = None;
    loop {
        let progress = send_output_progress(
            playback,
            writer,
            identity,
            request,
            generated_samples,
            &mut progress_sequence,
            &mut last_progress,
        )
        .await?;
        if progress.accepted_samples >= end_sample
            && progress.played_samples >= end_sample
            && progress.buffered_samples == 0
            && progress.output_complete
        {
            last_device_progress = Some(progress);
            break;
        }
        if Instant::now() >= deadline {
            return Err("客户端设备没有在时限内确认完整播放输出。".into());
        }
        tokio::time::sleep(OUTPUT_PROGRESS_INTERVAL).await;
    }
    let progress = last_device_progress.ok_or("客户端缺少设备输出进度回执。")?;
    let final_sample = permit.complete(total_samples)?;
    let mut completed = progress_json(&progress);
    completed["total_samples"] = json!(total_samples);
    completed["playback_end_sample"] = json!(end_sample);
    send_request_status(
        writer,
        identity,
        request,
        "media.tts.completed",
        completed,
        {
            let mut value = progress_labels();
            value.insert("total_samples", "MiniMax 合成确认的本段 PCM 样本数");
            value.insert(
                "playback_end_sample",
                "设备时钟确认完成的播放事务累计采样位置",
            );
            value
        },
    )
    .await?;
    if final_sample != end_sample {
        return Err("设备输出完成位置与播放事务结束位置不一致。".into());
    }
    Ok(())
}

async fn enqueue_frame(
    playback: &DirectPlaybackCallbacks,
    request: &ParsedRequest,
    sample_rate: u32,
    pcm: Vec<u8>,
) -> Result<(), String> {
    if pcm.is_empty() || pcm.len() % 2 != 0 {
        return Err("客户端拒绝空或不完整 PCM 帧，不创建静音补帧。".into());
    }
    (playback.enqueue_pcm)(request.playback_key.clone(), sample_rate, pcm).await
}

async fn send_output_progress(
    playback: &DirectPlaybackCallbacks,
    writer: &mpsc::Sender<MediaWriteCommand>,
    identity: &MediaIdentity,
    request: &ParsedRequest,
    generated_samples: u64,
    sequence: &mut u64,
    previous: &mut Option<ClientTtsPlaybackProgress>,
) -> Result<ClientTtsPlaybackProgress, String> {
    let progress =
        (playback.progress)(request.playback_key.clone(), request.request.sample_rate).await?;
    validate_progress(&progress, &request.playback_key)?;
    let max_accepted = request
        .playback_start_sample
        .saturating_add(generated_samples);
    if progress.accepted_samples > max_accepted
        || progress.accepted_samples < request.playback_start_sample
        || progress.played_samples < request.playback_start_sample
        || previous.as_ref().is_some_and(|prior| {
            progress.accepted_samples < prior.accepted_samples
                || progress.played_samples < prior.played_samples
        })
    {
        return Err("客户端设备播放进度跨越了固定键或 PCM 样本单调性边界。".into());
    }
    if previous.as_ref() != Some(&progress) {
        *sequence = sequence.saturating_add(1);
        let mut extra = progress_json(&progress);
        extra["progress_sequence"] = json!(*sequence);
        extra["generated_samples"] = json!(generated_samples);
        send_request_status(
            writer,
            identity,
            request,
            "media.tts.output_progress",
            extra,
            {
                let mut value = progress_labels();
                value.insert("progress_sequence", "当前分段内单调递增的输出进度序号");
                value.insert("generated_samples", "客户端本段已经生成的 PCM 样本数");
                value
            },
        )
        .await?;
        *previous = Some(progress.clone());
    }
    Ok(progress)
}

async fn send_request_status(
    writer: &mpsc::Sender<MediaWriteCommand>,
    identity: &MediaIdentity,
    request: &ParsedRequest,
    kind: &str,
    extra: Value,
    extra_labels: BTreeMap<&'static str, &'static str>,
) -> Result<(), String> {
    let mut message = base_message(identity, kind);
    message["authorization_id"] = json!(request.authorization_id);
    message["playback_key"] = json!(request.playback_key);
    message["request_id"] = json!(request.request_id);
    message["segment_index"] = json!(request.segment_index);
    merge_object(&mut message, extra);
    let mut field_labels = base_labels();
    field_labels.extend([
        ("authorization_id", "服务端签发的临时授权编号"),
        ("playback_key", "固定播放事务编号"),
        ("request_id", "单个文本分段请求编号"),
        ("segment_index", "播放事务内从零递增的文本分段序号"),
    ]);
    field_labels.extend(extra_labels);
    write_json(writer, message, field_labels).await
}

async fn send_control(
    writer: &mpsc::Sender<MediaWriteCommand>,
    identity: &MediaIdentity,
    kind: &str,
    extra: Value,
    extra_labels: BTreeMap<&'static str, &'static str>,
) -> Result<(), String> {
    let mut message = base_message(identity, kind);
    merge_object(&mut message, extra);
    let mut field_labels = base_labels();
    field_labels.extend(extra_labels);
    write_json(writer, message, field_labels).await
}

async fn write_json(
    writer: &mpsc::Sender<MediaWriteCommand>,
    mut message: Value,
    field_labels: BTreeMap<&'static str, &'static str>,
) -> Result<(), String> {
    message["fields"] = Value::Object(
        field_labels
            .into_iter()
            .map(|(key, explanation)| (key.to_owned(), Value::String(explanation.to_owned())))
            .collect::<Map<_, _>>(),
    );
    let serialized =
        serde_json::to_string(&message).map_err(|_| "客户端直放回执编码失败。".to_string())?;
    if serialized.len() > MAX_CONTROL_BYTES {
        return Err("客户端直放回执超过 16 KiB 字节上限。".into());
    }
    writer
        .send(MediaWriteCommand::Text(serialized))
        .await
        .map_err(|_| "SSH 媒体写入队列已关闭，客户端直放回执未能发送。".into())
}

fn base_message(identity: &MediaIdentity, kind: &str) -> Value {
    json!({
        "type":kind,
        "protocol_version":MEDIA_PROTOCOL_VERSION,
        "device_id":identity.device_id,
        "core_session_id":identity.core_session_id,
        "connection_generation":identity.connection_generation,
        "executor_generation":identity.executor_generation,
        "interaction_generation":identity.interaction_generation,
        "stream_id":identity.stream_id,
    })
}

fn base_labels() -> BTreeMap<&'static str, &'static str> {
    BTreeMap::from([
        ("type", "设备媒体控制消息类型"),
        ("protocol_version", "设备媒体协议版本，必须为 2"),
        ("device_id", "固定目标设备编号"),
        ("core_session_id", "权威核心运行编号"),
        ("connection_generation", "SSH 设备连接代次"),
        ("executor_generation", "设备工作进程代次"),
        ("interaction_generation", "主机授予的语音占用代次"),
        ("stream_id", "SSH 连续媒体流编号"),
    ])
}

fn validate_envelope(
    value: &Value,
    expected_type: &str,
    identity: &MediaIdentity,
    expected_keys: &[&str],
) -> Result<(), String> {
    let object = value
        .as_object()
        .ok_or("客户端直放控制消息必须为 JSON 对象。")?;
    if object.len() != expected_keys.len()
        || expected_keys.iter().any(|key| !object.contains_key(*key))
        || value["type"].as_str() != Some(expected_type)
        || value["protocol_version"].as_u64() != Some(u64::from(MEDIA_PROTOCOL_VERSION))
        || value["device_id"].as_str() != Some(identity.device_id.as_str())
        || value["core_session_id"].as_str() != Some(identity.core_session_id.as_str())
        || value["stream_id"].as_str() != Some(identity.stream_id.as_str())
        || value["connection_generation"].as_u64() != Some(identity.connection_generation)
        || value["executor_generation"].as_u64() != Some(identity.executor_generation)
        || value["interaction_generation"].as_u64() != Some(identity.interaction_generation)
    {
        return Err("客户端直放控制消息身份、媒体代次或字段与当前租约不匹配。".into());
    }
    let fields = value["fields"]
        .as_object()
        .ok_or("客户端直放消息缺少逐字段中文说明。")?;
    if fields.len() != object.len() - 1
        || object
            .keys()
            .any(|key| key != "fields" && !fields.contains_key(key))
        || fields.values().any(|field| {
            field.as_str().is_none_or(|explanation| {
                explanation.is_empty()
                    || !explanation
                        .chars()
                        .any(|character| ('\u{4e00}'..='\u{9fff}').contains(&character))
            })
        })
    {
        return Err("客户端直放消息字段中文说明不完整。".into());
    }
    Ok(())
}

fn validate_progress(
    progress: &ClientTtsPlaybackProgress,
    playback_key: &str,
) -> Result<(), String> {
    progress.validate().map_err(str::to_owned)?;
    if progress.playback_key != playback_key {
        return Err("客户端设备播放进度没有绑定当前播放事务编号。".into());
    }
    Ok(())
}

fn progress_json(progress: &ClientTtsPlaybackProgress) -> Value {
    json!({
        "accepted_samples":progress.accepted_samples,
        "played_samples":progress.played_samples,
        "buffered_samples":progress.buffered_samples,
        "output_complete":progress.output_complete,
        "device_time_us":progress.device_time_us,
    })
}

fn progress_labels() -> BTreeMap<&'static str, &'static str> {
    BTreeMap::from([
        (
            "accepted_samples",
            "固定播放事务内设备已接纳的累计 PCM 样本数",
        ),
        (
            "played_samples",
            "固定播放事务内设备时钟确认已输出的累计 PCM 样本数",
        ),
        (
            "buffered_samples",
            "该固定播放事务仍在设备缓冲的 PCM 样本数",
        ),
        ("output_complete", "设备是否已确认固定播放事务输出完成"),
        ("device_time_us", "客户端设备单调时钟微秒值"),
    ])
}

fn safe_error(_error: &str) -> String {
    "客户端 MiniMax 授权或设备播放器未能接纳本次分段。".into()
}

fn labels(values: &[(&'static str, &'static str)]) -> BTreeMap<&'static str, &'static str> {
    values.iter().copied().collect()
}

fn merge_object(target: &mut Value, extra: Value) {
    if let (Value::Object(target), Value::Object(extra)) = (target, extra) {
        target.extend(extra);
    }
}

fn string_field(value: &Value, key: &str) -> Result<String, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or_else(|| format!("客户端直放字段 {key}（文本）无效。"))
}

fn safe_integer(value: &Value, key: &str) -> Result<u64, String> {
    value
        .get(key)
        .and_then(Value::as_u64)
        .filter(|value| *value <= ((1_u64 << 53) - 1))
        .ok_or_else(|| format!("客户端直放字段 {key}（安全整数）无效。"))
}

fn validate_id(value: &str, field: &str) -> Result<(), String> {
    if value.is_empty()
        || value.len() > 128
        || value.trim() != value
        || !value.is_ascii()
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
    {
        return Err(format!("客户端直放字段 {field}（编号）无效。"));
    }
    Ok(())
}

fn validate_playback_key(value: &str) -> Result<(), String> {
    if value.is_empty() || value.len() > 128 || value.trim() != value || value.contains('\0') {
        return Err("客户端直放 playback_key（播放事务编号）无效。".into());
    }
    Ok(())
}

fn minimum_samples(sample_rate: u32) -> u64 {
    u64::from(sample_rate).saturating_mul(MIN_AUDIO_OUTPUT_MS) / 1_000
}

fn clip(value: &str, maximum: usize) -> String {
    value.chars().take(maximum).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn identity() -> MediaIdentity {
        MediaIdentity {
            device_id: "client-a".into(),
            core_session_id: "core-a".into(),
            connection_generation: 2,
            executor_generation: 3,
            interaction_generation: 4,
            stream_id: "media-a".into(),
        }
    }

    fn fields(value: &mut Value) {
        let fields = value
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .map(|key| (key, Value::String("设备媒体字段中文说明".into())))
            .collect();
        value["fields"] = Value::Object(fields);
    }

    fn authorization() -> Value {
        let mut value = json!({
            "type":"media.tts.authorize","protocol_version":2,"device_id":"client-a",
            "core_session_id":"core-a","connection_generation":2,"executor_generation":3,
            "interaction_generation":4,"stream_id":"media-a","authorization_id":"auth-a",
            "playback_key":"speech-a","provider":"minimax","ttl_ms":120000,
            "credential":"provider-secret-that-must-not-be-serialized-again",
        });
        fields(&mut value);
        value
    }

    fn request() -> Value {
        let mut value = json!({
            "type":"media.tts.request","protocol_version":2,"device_id":"client-a",
            "core_session_id":"core-a","connection_generation":2,"executor_generation":3,
            "interaction_generation":4,"stream_id":"media-a","authorization_id":"auth-a",
            "playback_key":"speech-a","request_id":"request-a","segment_index":0,
            "playback_start_sample":0,"text":"你好。","model":"speech-2.8-hd",
            "voice_id":"voice-a","sample_rate":32000,"speed":1.2,"volume":0.8,"emotion":"happy",
        });
        fields(&mut value);
        value
    }

    #[test]
    fn parses_v2_authorization_without_leaving_a_serializable_credential_copy() {
        let mut value = authorization();
        let authorization = parse_authorization(&mut value, &identity()).unwrap();
        assert_eq!(authorization.authorization_id, "auth-a");
        assert_eq!(authorization.playback_key, "speech-a");
        assert!(value["credential"].is_null());
        assert!(!format!("{authorization:?}").contains("provider-secret"));
    }

    #[test]
    fn parses_v2_request_and_rejects_stale_generations_or_extra_fields() {
        let parsed = parse_request(&request(), &identity()).unwrap();
        assert_eq!(parsed.playback_key, "speech-a");
        assert_eq!(parsed.request_id, "request-a");
        assert_eq!(parsed.segment_index, 0);
        assert_eq!(parsed.request.task_id, "request-a");
        let mut stale = request();
        stale["interaction_generation"] = json!(99);
        assert!(parse_request(&stale, &identity()).is_err());
        let mut extra = request();
        extra["credential"] = json!("forbidden-on-request");
        assert!(parse_request(&extra, &identity()).is_err());
    }

    #[test]
    fn request_permit_prevents_same_key_duplicate_and_requires_sequential_segments() {
        let grant = MiniMaxTtsAuthorization::new(
            "auth-a".into(),
            "speech-a".into(),
            60_000,
            "not-a-real-provider-secret".into(),
        )
        .unwrap();
        let mut store = ClientTtsAuthorizationStore::new(8).unwrap();
        store.authorize(grant).unwrap();
        let parsed = parse_request(&request(), &identity()).unwrap();
        let (_credential, permit) = store.begin_request(&parsed).unwrap();
        assert!(store.begin_request(&parsed).is_err());
        permit.complete(3_200).unwrap();
        let mut next = parsed;
        next.request_id = "request-b".into();
        next.request.task_id = "request-b".into();
        next.segment_index = 1;
        next.playback_start_sample = 3_200;
        let (_credential, second) = store.begin_request(&next).unwrap();
        second.complete(3_200).unwrap();
    }

    #[test]
    fn revoking_and_clearing_authorizations_make_the_old_key_unusable() {
        let grant = MiniMaxTtsAuthorization::new(
            "auth-a".into(),
            "speech-a".into(),
            60_000,
            "not-a-real-provider-secret".into(),
        )
        .unwrap();
        let mut store = ClientTtsAuthorizationStore::new(8).unwrap();
        store.authorize(grant).unwrap();
        assert_eq!(
            store.revoke("auth-a", "speech-a").unwrap().as_deref(),
            Some("speech-a")
        );
        assert!(store
            .begin_request(&parse_request(&request(), &identity()).unwrap())
            .is_err());
        store
            .authorize(
                MiniMaxTtsAuthorization::new(
                    "auth-b".into(),
                    "speech-b".into(),
                    60_000,
                    "another-test-key".into(),
                )
                .unwrap(),
            )
            .unwrap();
        store.clear();
        assert!(store.by_id.is_empty());
    }
}
