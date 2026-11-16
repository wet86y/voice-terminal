//! Client TTS media relay（客户端在线合成媒体中继）：凭据始终留在客户端原生进程。
use serde_json::{Map, Value, json};
use std::{collections::BTreeMap, path::Path, sync::OnceLock, time::Instant};
use tokio::sync::mpsc;

use voice_desktop_common::{
    client_tts::{
        ClientTtsEvent, ClientTtsPcmFramer, MiniMaxTtsCancellation, MiniMaxTtsHandle,
        MiniMaxTtsRequest,
    },
    device_protocol::{ClientTtsAudioHeader, encode_client_tts_audio_frame},
};

use crate::MediaWriteCommand;

pub(super) const MAX_ACTIVE_REQUESTS: usize = 4; // MAX_ACTIVE_REQUESTS（单媒体租约同时运行的合成任务上限）。
pub(super) const MAX_SEEN_REQUESTS: usize = 4096; // MAX_SEEN_REQUESTS（单媒体租约去重请求记录上限）。

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MediaIdentity {
    pub device_id: String,           // device_id（已配对客户端设备编号）。
    pub core_session_id: String,     // core_session_id（共享主机权威核心运行编号）。
    pub connection_generation: u64,  // connection_generation（SSH 设备连接代次）。
    pub executor_generation: u64,    // executor_generation（设备工作进程代次）。
    pub interaction_generation: u64, // interaction_generation（主机授予的语音占用代次）。
    pub stream_id: String,           // stream_id（当前 SSH 连续音频流编号）。
}

pub struct ParsedRequest {
    pub request_id: String, // request_id（本次 MiniMax 文本分段请求编号）。
    pub request: MiniMaxTtsRequest, // request（不含接口密钥的合成参数）。
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
        "task_id",
        "request_id",
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
    let task_id = string_field(value, "task_id")?;
    let request_id = string_field(value, "request_id")?;
    validate_id(&task_id, "task_id")?;
    validate_id(&request_id, "request_id")?;
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
        request_id,
        request: MiniMaxTtsRequest {
            task_id,
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

pub fn parse_cancel(value: &Value, identity: &MediaIdentity) -> Result<(String, String), String> {
    let keys = [
        "type",
        "protocol_version",
        "device_id",
        "core_session_id",
        "connection_generation",
        "executor_generation",
        "interaction_generation",
        "stream_id",
        "task_id",
        "request_id",
        "fields",
    ];
    validate_envelope(value, "media.tts.cancel", identity, &keys)?;
    let task_id = string_field(value, "task_id")?;
    let request_id = string_field(value, "request_id")?;
    validate_id(&task_id, "task_id")?;
    validate_id(&request_id, "request_id")?;
    Ok((task_id, request_id))
}

pub fn start_request(
    parsed: ParsedRequest,
    data_root: &Path,
    identity: MediaIdentity,
    writer: mpsc::Sender<MediaWriteCommand>,
    finished: mpsc::UnboundedSender<String>,
) -> Result<(MiniMaxTtsCancellation, tauri::async_runtime::JoinHandle<()>), String> {
    let mut handle =
        MiniMaxTtsHandle::start_with_local_credential(parsed.request.clone(), data_root)?;
    let cancellation = handle.cancellation_handle();
    let request_id = parsed.request_id.clone();
    let task_id = parsed.request.task_id.clone();
    let sample_rate = parsed.request.sample_rate;
    let relay_writer = writer.clone();
    let task = tauri::async_runtime::spawn(async move {
        let outcome = forward_events(
            &mut handle,
            &parsed.request_id,
            &identity,
            sample_rate,
            writer,
        )
        .await;
        if let Err(error) = outcome {
            handle.request_cancel();
            let _ = send_status(
                &relay_writer,
                &identity,
                &task_id,
                &request_id,
                "media.tts.failed",
                json!({"error": clip(&error, 300)}),
                labels(&[("error", "不含接口密钥和请求正文的错误摘要")]),
            )
            .await;
            let mut terminal = false;
            while !terminal {
                match tokio::time::timeout(std::time::Duration::from_secs(3), handle.recv()).await {
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
                return;
            }
        }
        let _ = handle.join().await;
        let _ = finished.send(request_id);
    });
    Ok((cancellation, task))
}

pub async fn send_start_failure(
    writer: &mpsc::Sender<MediaWriteCommand>,
    identity: &MediaIdentity,
    task_id: &str,
    request_id: &str,
    error: &str,
) -> Result<(), String> {
    send_status(
        writer,
        identity,
        task_id,
        request_id,
        "media.tts.failed",
        json!({"error": clip(error, 300)}),
        labels(&[("error", "不含接口密钥和请求正文的错误摘要")]),
    )
    .await
}

async fn forward_events(
    handle: &mut MiniMaxTtsHandle,
    request_id: &str,
    identity: &MediaIdentity,
    sample_rate: u32,
    writer: mpsc::Sender<MediaWriteCommand>,
) -> Result<(), String> {
    let mut framer = ClientTtsPcmFramer::new(sample_rate)?;
    let mut terminal = false;
    while let Some(event) = handle.recv().await {
        match event {
            ClientTtsEvent::Started { task_id } => {
                send_status(
                    &writer,
                    identity,
                    &task_id,
                    request_id,
                    "media.tts.started",
                    Value::Null,
                    labels(&[]),
                )
                .await?;
            }
            ClientTtsEvent::Audio {
                task_id,
                sample_rate: event_rate,
                pcm,
                ..
            } => {
                if event_rate != sample_rate {
                    return Err("MiniMax 合成请求在任务中途改变了采样率。".into());
                }
                for frame in framer.push(&pcm)? {
                    send_audio_frame(
                        &writer,
                        identity,
                        &task_id,
                        request_id,
                        sample_rate,
                        frame.sequence,
                        frame.start_sample,
                        frame.sample_count,
                        &frame.pcm,
                    )
                    .await?;
                }
            }
            ClientTtsEvent::ProviderEvent {
                task_id,
                event,
                sample_cursor,
                sentence_index,
                sentence_start_sample,
                alignment,
            } => {
                let mut event_alignment = alignment.unwrap_or(Value::Null);
                if serde_json::to_vec(&event_alignment).map_or(true, |bytes| bytes.len() > 2048) {
                    event_alignment = Value::Null;
                }
                send_status(
                    &writer,
                    identity,
                    &task_id,
                    request_id,
                    "media.tts.provider_event",
                    json!({"event":clip(&event, 64), "sample_cursor":sample_cursor,
                           "sentence_index":sentence_index,
                           "sentence_start_sample":sentence_start_sample,
                           "alignment":event_alignment}),
                    labels(&[
                        ("event", "MiniMax 提供方事件类型"),
                        ("sample_cursor", "提供方事件对应的 PCM 样本位置"),
                        ("sentence_index", "句子顺序编号"),
                        ("sentence_start_sample", "句子起始采样位置"),
                        ("alignment", "经边界约束的字词对齐数据"),
                    ]),
                )
                .await?;
            }
            ClientTtsEvent::SegmentFinished {
                task_id,
                text_chars,
                end_sample,
            } => {
                if let Some(frame) = framer.finish() {
                    send_audio_frame(
                        &writer,
                        identity,
                        &task_id,
                        request_id,
                        sample_rate,
                        frame.sequence,
                        frame.start_sample,
                        frame.sample_count,
                        &frame.pcm,
                    )
                    .await?;
                }
                send_status(
                    &writer,
                    identity,
                    &task_id,
                    request_id,
                    "media.tts.segment_finished",
                    json!({"text_chars":text_chars,"end_sample":end_sample}),
                    labels(&[
                        ("text_chars", "已确认文本字符数"),
                        ("end_sample", "文本段结束 PCM 采样位置"),
                    ]),
                )
                .await?;
            }
            ClientTtsEvent::Completed {
                task_id,
                total_samples,
            } => {
                send_status(
                    &writer,
                    identity,
                    &task_id,
                    request_id,
                    "media.tts.completed",
                    json!({"total_samples":total_samples}),
                    labels(&[("total_samples", "提供方确认的 PCM 总采样数")]),
                )
                .await?;
                terminal = true;
                break;
            }
            ClientTtsEvent::Cancelled { task_id } => {
                send_status(
                    &writer,
                    identity,
                    &task_id,
                    request_id,
                    "media.tts.cancelled",
                    Value::Null,
                    labels(&[]),
                )
                .await?;
                terminal = true;
                break;
            }
            ClientTtsEvent::Failed { task_id, error } => {
                send_status(
                    &writer,
                    identity,
                    &task_id,
                    request_id,
                    "media.tts.failed",
                    json!({"error":clip(&error, 300)}),
                    labels(&[("error", "不含接口密钥和请求正文的错误摘要")]),
                )
                .await?;
                terminal = true;
                break;
            }
        }
    }
    if !terminal {
        return Err("MiniMax 合成事件流在终态回执前关闭。".into());
    }
    Ok(())
}

async fn send_audio_frame(
    writer: &mpsc::Sender<MediaWriteCommand>,
    identity: &MediaIdentity,
    task_id: &str,
    request_id: &str,
    sample_rate: u32,
    sequence: u64,
    start_sample: u64,
    sample_count: u64,
    pcm: &[u8],
) -> Result<(), String> {
    let header = ClientTtsAudioHeader {
        protocol_version: 1,
        direction: "tts_audio".into(),
        device_id: identity.device_id.clone(),
        core_session_id: identity.core_session_id.clone(),
        connection_generation: identity.connection_generation,
        executor_generation: identity.executor_generation,
        interaction_generation: identity.interaction_generation,
        stream_id: identity.stream_id.clone(),
        task_id: task_id.into(),
        request_id: request_id.into(),
        sequence,
        start_sample,
        sample_count,
        sample_rate,
        device_time_us: monotonic_time_us(),
    };
    let bytes = encode_client_tts_audio_frame(&header, pcm)?;
    writer
        .send(MediaWriteCommand::Binary(bytes))
        .await
        .map_err(|_| "SSH 媒体写入队列已关闭，客户端合成 PCM 未能回传。".to_string())
}

async fn send_status(
    writer: &mpsc::Sender<MediaWriteCommand>,
    identity: &MediaIdentity,
    task_id: &str,
    request_id: &str,
    kind: &str,
    extra: Value,
    extra_labels: BTreeMap<&'static str, &'static str>,
) -> Result<(), String> {
    let mut message = json!({
        "type":kind,"protocol_version":1,
        "device_id":identity.device_id,"core_session_id":identity.core_session_id,
        "connection_generation":identity.connection_generation,
        "executor_generation":identity.executor_generation,
        "interaction_generation":identity.interaction_generation,
        "stream_id":identity.stream_id,"task_id":task_id,"request_id":request_id,
    });
    if let Value::Object(extra_fields) = extra {
        for (key, value) in extra_fields {
            message[&key] = value;
        }
    }
    let mut field_labels = BTreeMap::from([
        ("type", "控制消息类型"),
        ("protocol_version", "设备媒体协议版本"),
        ("device_id", "固定目标设备编号"),
        ("core_session_id", "权威核心运行编号"),
        ("connection_generation", "SSH 设备连接代次"),
        ("executor_generation", "设备工作进程代次"),
        ("interaction_generation", "主机授予的语音占用代次"),
        ("stream_id", "SSH 连续音频流编号"),
        ("task_id", "固定播放事务编号"),
        ("request_id", "本次 MiniMax 文本分段请求编号"),
    ]);
    field_labels.extend(extra_labels);
    let fields = field_labels
        .into_iter()
        .map(|(key, value)| (key.to_owned(), Value::String(value.to_owned())))
        .collect::<Map<_, _>>();
    message["fields"] = Value::Object(fields);
    let json =
        serde_json::to_string(&message).map_err(|_| "客户端合成回执编码失败。".to_string())?;
    if json.len() > 4096 {
        return Err("客户端合成回执超过 4096 字节上限。".into());
    }
    writer
        .send(MediaWriteCommand::Text(json))
        .await
        .map_err(|_| "SSH 媒体写入队列已关闭，客户端合成回执未能回传。".to_string())
}

fn validate_envelope(
    value: &Value,
    expected_type: &str,
    identity: &MediaIdentity,
    expected_keys: &[&str],
) -> Result<(), String> {
    let object = value
        .as_object()
        .ok_or("客户端合成控制消息必须为 JSON 对象。")?;
    if object.len() != expected_keys.len()
        || expected_keys.iter().any(|key| !object.contains_key(*key))
        || value["type"].as_str() != Some(expected_type)
        || value["protocol_version"].as_u64() != Some(1)
        || value["device_id"].as_str() != Some(identity.device_id.as_str())
        || value["core_session_id"].as_str() != Some(identity.core_session_id.as_str())
        || value["stream_id"].as_str() != Some(identity.stream_id.as_str())
        || value["connection_generation"].as_u64() != Some(identity.connection_generation)
        || value["executor_generation"].as_u64() != Some(identity.executor_generation)
        || value["interaction_generation"].as_u64() != Some(identity.interaction_generation)
    {
        return Err("客户端合成控制消息身份、代次或字段与当前媒体租约不匹配。".into());
    }
    let fields = value["fields"]
        .as_object()
        .ok_or("客户端合成消息缺少字段中文说明。")?;
    if fields.len() != object.len() - 1
        || object
            .keys()
            .any(|key| key != "fields" && !fields.contains_key(key))
        || fields
            .values()
            .any(|field| field.as_str().is_none_or(str::is_empty))
    {
        return Err("客户端合成消息字段说明不完整。".into());
    }
    Ok(())
}

fn string_field(value: &Value, key: &str) -> Result<String, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or_else(|| format!("客户端合成字段 {key}（文本）无效。"))
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
        return Err(format!("客户端合成字段 {field}（编号）无效。"));
    }
    Ok(())
}

fn labels(values: &[(&'static str, &'static str)]) -> BTreeMap<&'static str, &'static str> {
    values.iter().copied().collect()
}

fn clip(value: &str, maximum: usize) -> String {
    value.chars().take(maximum).collect()
}

fn monotonic_time_us() -> u64 {
    static ORIGIN: OnceLock<Instant> = OnceLock::new();
    ORIGIN
        .get_or_init(Instant::now)
        .elapsed()
        .as_micros()
        .min(u128::from(u64::MAX)) as u64
}

#[cfg(test)]
mod tests {
    use super::*;

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

    fn request() -> Value {
        let mut value = json!({
            "type":"media.tts.request","protocol_version":1,
            "device_id":"client-a","core_session_id":"core-a",
            "connection_generation":2,"executor_generation":3,
            "interaction_generation":4,"stream_id":"media-a",
            "task_id":"task-a","request_id":"request-a","text":"你好。",
            "model":"speech-2.8-hd","voice_id":"voice-a","sample_rate":32_000,
            "speed":1.2,"volume":0.8,"emotion":"happy",
        });
        let fields = value
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .map(|key| (key, Value::String("设备协议字段中文说明".into())))
            .collect::<Map<_, _>>();
        value["fields"] = Value::Object(fields);
        value
    }

    #[test]
    fn parses_client_tts_request_without_accepting_remote_identity_changes() {
        let parsed = parse_request(&request(), &identity()).unwrap();
        assert_eq!(parsed.request_id, "request-a");
        assert_eq!(parsed.request.task_id, "task-a");
        assert_eq!(parsed.request.text, "你好。");
        assert_eq!(parsed.request.sample_rate, 32_000);
        assert_eq!(parsed.request.speed, 1.2);
        assert_eq!(parsed.request.volume, 0.8);
        assert_eq!(parsed.request.emotion.as_deref(), Some("happy"));

        let mut stale = request();
        stale["executor_generation"] = json!(9);
        assert!(parse_request(&stale, &identity()).is_err());
        let mut extra = request();
        extra["api_key"] = json!("must-never-be-sent");
        assert!(parse_request(&extra, &identity()).is_err());
    }

    #[test]
    fn parses_cancel_only_for_the_current_media_lease() {
        let mut value = json!({
            "type":"media.tts.cancel","protocol_version":1,
            "device_id":"client-a","core_session_id":"core-a",
            "connection_generation":2,"executor_generation":3,
            "interaction_generation":4,"stream_id":"media-a",
            "task_id":"task-a","request_id":"request-a",
        });
        let fields = value
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .map(|key| (key, Value::String("设备协议字段中文说明".into())))
            .collect::<Map<_, _>>();
        value["fields"] = Value::Object(fields);
        assert_eq!(
            parse_cancel(&value, &identity()).unwrap(),
            ("task-a".into(), "request-a".into())
        );
        value["interaction_generation"] = json!(5);
        assert!(parse_cancel(&value, &identity()).is_err());
    }
}
