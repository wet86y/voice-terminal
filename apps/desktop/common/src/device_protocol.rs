//! Device media framing（设备音频消息封装）：与核心服务共享版本及长度边界。
use serde::{Deserialize, Serialize};

pub const PROTOCOL_VERSION: u32 = 1; // PROTOCOL_VERSION（设备控制协议版本，保持不变）。
pub const MEDIA_PROTOCOL_VERSION: u32 = 2; // MEDIA_PROTOCOL_VERSION（设备媒体协议版本）。
pub const MAX_HEADER_BYTES: usize = 4096; // MAX_HEADER_BYTES（帧头最大字节数）。
pub const MAX_PAYLOAD_BYTES: usize = 256 * 1024; // MAX_PAYLOAD_BYTES（帧负载最大字节数）。
pub const MAX_CONTROL_BYTES: usize = 16 * 1024; // MAX_CONTROL_BYTES（JSON 控制消息最大字节数）。
const MAX_SAFE_INTEGER: u64 = (1_u64 << 53) - 1; // MAX_SAFE_INTEGER（跨语言安全整数上限）。

/// NullableMetadata（必需但可空的元数据字段）：缺字段与显式 null（空值）不同。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(transparent)]
pub struct NullableMetadata<T>(pub Option<T>); // NullableMetadata（协议中必须出现、内容可为空的值）。

impl<'de, T> Deserialize<'de> for NullableMetadata<T>
where
    T: Deserialize<'de>,
{
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        Option::<T>::deserialize(deserializer).map(Self)
    }
}

/// AudioProcessingHealth（设备前处理健康状态）：AEC（回声消除）与近端 VAD（语音活动检测）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AudioProcessingHealth {
    Ready,       // ready（AEC/NS/VAD 均报告就绪）。
    Degraded,    // degraded（处理或参考质量降级）。
    Error,       // error（处理发生故障）。
    Unavailable, // unavailable（没有可用前处理器）。
}

/// AudioProcessingMetadata（同帧音频前处理证据）：不得由核心主机的另一套处理器替代。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AudioProcessingMetadata {
    pub state: AudioProcessingHealth, // state（AEC、降噪及 VAD 健康状态）。
    pub near_end_voice: NullableMetadata<bool>, // near_end_voice（近端语音结论；故障时为 null）。
    pub protected_interruption_available: bool, // protected_interruption_available（是否具备受保护打断资格）。
    pub reverse_generation: u64, // reverse_generation（设备本地播放反向参考格式代次）。
    pub error: NullableMetadata<String>, // error（前处理故障摘要；健康时为 null）。
    pub fields: std::collections::BTreeMap<String, String>, // fields（逐字段中文说明）。
}

impl AudioProcessingMetadata {
    fn validate(&self) -> Result<(), &'static str> {
        if self.reverse_generation > MAX_SAFE_INTEGER {
            return Err("设备前处理反向参考代次超出安全整数范围");
        }
        if let Some(error) = self.error.0.as_deref() {
            if error.is_empty() || error.len() > 300 || error.contains('\0') {
                return Err("设备前处理故障摘要为空或超过长度上限");
            }
        }
        match self.state {
            AudioProcessingHealth::Ready => {
                if self.near_end_voice.0.is_none() || self.error.0.is_some() {
                    return Err("健康的设备前处理必须提供近端语音结论且不能包含故障");
                }
            }
            AudioProcessingHealth::Degraded
            | AudioProcessingHealth::Error
            | AudioProcessingHealth::Unavailable => {
                if self.near_end_voice.0.is_some() || self.protected_interruption_available {
                    return Err("不健康的设备前处理不能提供近端语音结论或受保护打断资格");
                }
            }
        }
        let expected = [
            "error",
            "near_end_voice",
            "protected_interruption_available",
            "reverse_generation",
            "state",
        ];
        if self.fields.len() != expected.len()
            || expected.iter().any(|name| !self.fields.contains_key(*name))
            || self.fields.values().any(|label| {
                label.is_empty() || label.len() > 100 || !label.chars().any(is_chinese_label_char)
            })
        {
            return Err("设备前处理元数据缺少精确字段集或逐字段中文说明");
        }
        Ok(())
    }
}

fn is_chinese_label_char(value: char) -> bool {
    ('\u{4e00}'..='\u{9fff}').contains(&value)
}

/// AudioCodec（音频编解码模式）：旧版本头部缺失时按 PCM（原始采样）兼容。
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AudioCodec {
    #[default]
    Pcm, // pcm（未压缩 PCM 帧）。
    Opus, // opus（单个 20 ms raw Opus 包）。
}

impl AudioCodec {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Pcm => "pcm",
            Self::Opus => "opus",
        }
    }
}

/// MediaHeader（音频帧头）：设备时间保留微秒，不能直接当作主机时间。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MediaHeader {
    pub protocol_version: u32,       // protocol_version（设备音频协议版本）。
    pub device_id: String,           // device_id（设备编号）。
    pub core_session_id: String,     // core_session_id（权威核心运行编号）。
    pub connection_generation: u64,  // connection_generation（连接代次）。
    pub executor_generation: u64,    // executor_generation（执行器代次）。
    pub interaction_generation: u64, // interaction_generation（语音占用代次）。
    pub stream_id: String,           // stream_id（连续音频流编号）。
    pub sequence: u64,               // sequence（本流连续帧序号）。
    pub start_sample: u64,           // start_sample（16 kHz 采样起始位置）。
    pub sample_count: u64,           // sample_count（帧内有效采样数）。
    pub device_time_us: u64,         // device_time_us（设备单调采集时刻，微秒）。
    #[serde(default)]
    pub audio_codec: AudioCodec, // audio_codec（协商后的上行音频负载格式）。
    pub discontinuity_before: bool,  // discontinuity_before（本帧前是否发生采集缺口）。
    pub audio_processing: AudioProcessingMetadata, // audio_processing（本帧设备端 AEC/NS/VAD 证据）。
}

/// PlaybackMediaHeader（下行播放帧头）：送达设备和事务目标随每帧固定。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlaybackMediaHeader {
    pub protocol_version: u32,       // protocol_version（设备媒体协议版本）。
    pub direction: String,           // direction（固定为 playback 下行方向）。
    pub device_id: String,           // device_id（通过认证的远端设备身份）。
    pub delivery_device_id: String,  // delivery_device_id（固定播放送达设备）。
    pub core_session_id: String,     // core_session_id（权威核心运行编号）。
    pub connection_generation: u64,  // connection_generation（设备连接代次）。
    pub executor_generation: u64,    // executor_generation（设备执行器代次）。
    pub interaction_generation: u64, // interaction_generation（媒体会话交互代次）。
    pub stream_id: String,           // stream_id（连续媒体通道编号）。
    pub playback_key: String,        // playback_key（固定播放事务编号）。
    pub sequence: u64,               // sequence（媒体通道内单调递增帧序号）。
    pub start_sample: u64,           // start_sample（本播放事务内首采样位置）。
    pub sample_count: u64,           // sample_count（本帧有效采样数）。
    pub sample_rate: u32,            // sample_rate（PCM 采样率或 Opus 解码后采样率）。
    pub host_send_time_us: u64,      // host_send_time_us（核心单调时钟发送时刻，微秒）。
    #[serde(default)]
    pub audio_codec: AudioCodec, // audio_codec（下行负载格式；旧版缺省为 PCM）。
}

impl PlaybackMediaHeader {
    fn validate(&self, payload: &[u8]) -> Result<(), &'static str> {
        if self.protocol_version != MEDIA_PROTOCOL_VERSION || self.direction != "playback" {
            return Err("下行播放帧版本或方向不受支持");
        }
        for value in [
            &self.device_id,
            &self.delivery_device_id,
            &self.core_session_id,
            &self.stream_id,
            &self.playback_key,
        ] {
            validate_id(value)?;
        }
        if self.device_id != self.delivery_device_id {
            return Err("播放送达目标与设备身份不一致");
        }
        if [
            self.connection_generation,
            self.executor_generation,
            self.interaction_generation,
        ]
        .iter()
        .any(|value| *value == 0 || *value > MAX_SAFE_INTEGER)
            || [
                self.sequence,
                self.start_sample,
                self.sample_count,
                self.host_send_time_us,
            ]
            .iter()
            .any(|value| *value > MAX_SAFE_INTEGER)
        {
            return Err("下行播放帧代次或采样字段无效");
        }
        let valid_audio = match self.audio_codec {
            AudioCodec::Pcm => {
                matches!(self.sample_rate, 16_000 | 24_000 | 32_000 | 48_000)
                    && self.sample_count <= u64::from(self.sample_rate / 50)
                    && payload.len() == self.sample_count as usize * 2
                    && payload.len() <= MAX_PAYLOAD_BYTES
            }
            AudioCodec::Opus => {
                self.sample_rate == 48_000
                    && self.sample_count <= 960
                    && (1..=1275).contains(&payload.len())
            }
        };
        if !valid_audio
            || self.sample_count == 0
            || self.start_sample.checked_add(self.sample_count).is_none()
        {
            return Err("下行音频帧采样率、有效时长或编码负载无效");
        }
        Ok(())
    }
}

/// ClientTtsAudioHeader（旧版客户端合成帧头）：仅为兼容拒绝入口保留，版本 2 不可序列化。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClientTtsAudioHeader {
    pub protocol_version: u32,       // protocol_version（设备媒体协议版本）。
    pub direction: String,           // direction（固定为 tts_audio 客户端合成上行方向）。
    pub device_id: String,           // device_id（已认证客户端设备编号）。
    pub core_session_id: String,     // core_session_id（权威核心运行编号）。
    pub connection_generation: u64,  // connection_generation（设备连接代次）。
    pub executor_generation: u64,    // executor_generation（设备执行器代次）。
    pub interaction_generation: u64, // interaction_generation（主机授予的语音占用代次）。
    pub stream_id: String,           // stream_id（SSH 媒体通道编号）。
    pub task_id: String,             // task_id（固定语音事务编号）。
    pub request_id: String,          // request_id（本次 MiniMax 文本分段请求编号）。
    pub sequence: u64,               // sequence（该 TTS 请求内单调递增的 PCM 帧序号）。
    pub start_sample: u64,           // start_sample（该 TTS 请求 PCM 时间线位置）。
    pub sample_count: u64,           // sample_count（帧内有效采样数）。
    pub sample_rate: u32,            // sample_rate（PCM 采样率）。
    pub device_time_us: u64,         // device_time_us（客户端单调合成/封帧时刻，微秒）。
}

/// ClientTtsAuthorizationMetadata（客户端合成授权元数据）：不含密钥正文，可用于结构化校验。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClientTtsAuthorizationMetadata {
    pub authorization_id: String, // authorization_id（服务端签发的临时授权编号）。
    pub playback_key: String,     // playback_key（授权固定绑定的播放事务编号）。
    pub provider: String,         // provider（固定为 minimax 的在线供应商编号）。
    pub ttl_ms: u64,              // ttl_ms（授权从客户端接收时起算的有效毫秒数）。
}

impl ClientTtsAuthorizationMetadata {
    pub fn validate(&self) -> Result<(), &'static str> {
        validate_id(&self.authorization_id)?;
        if self.playback_key.is_empty()
            || self.playback_key.len() > 128
            || self.playback_key.trim() != self.playback_key
            || self.playback_key.contains('\0')
            || self.provider != "minimax"
            || !(1..=300_000).contains(&self.ttl_ms)
        {
            return Err("客户端合成授权播放键、供应商或有效时长无效");
        }
        Ok(())
    }
}

/// ClientTtsPlaybackProgress（客户端播放进度）：只能来自固定播放键对应的原生设备时钟。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClientTtsPlaybackProgress {
    pub playback_key: String,  // playback_key（固定播放事务编号）。
    pub accepted_samples: u64, // accepted_samples（设备已接纳的累计 PCM 样本数）。
    pub played_samples: u64,   // played_samples（设备时钟确认已输出的累计 PCM 样本数）。
    pub buffered_samples: u64, // buffered_samples（仍由设备缓冲的 PCM 样本数）。
    pub output_complete: bool, // output_complete（设备确认该固定播放事务已经输出完成）。
    pub device_time_us: u64,   // device_time_us（客户端原生设备时钟单调微秒值）。
}

impl ClientTtsPlaybackProgress {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.playback_key.is_empty()
            || self.playback_key.len() > 128
            || self.playback_key.trim() != self.playback_key
            || self.playback_key.contains('\0')
            || [
                self.accepted_samples,
                self.played_samples,
                self.buffered_samples,
            ]
            .iter()
            .any(|value| *value > MAX_SAFE_INTEGER)
            || self.played_samples > self.accepted_samples
            || self.buffered_samples > self.accepted_samples.saturating_sub(self.played_samples)
            || self.output_complete
                && (self.buffered_samples != 0 || self.played_samples != self.accepted_samples)
            || self.device_time_us > MAX_SAFE_INTEGER
        {
            return Err("客户端设备播放进度与播放事务样本位置不一致");
        }
        Ok(())
    }
}

impl MediaHeader {
    fn validate(&self) -> Result<(), &'static str> {
        if self.protocol_version != MEDIA_PROTOCOL_VERSION {
            return Err("设备音频协议版本不受支持");
        }
        for value in [&self.device_id, &self.core_session_id, &self.stream_id] {
            if value.is_empty() || value.chars().count() > 128 || value.trim() != value {
                return Err("音频帧编号无效");
            }
        }
        if self.sample_count == 0
            || self.sample_count > 320
            || [
                self.connection_generation,
                self.executor_generation,
                self.interaction_generation,
            ]
            .iter()
            .any(|value| *value == 0)
            || [
                self.sequence,
                self.start_sample,
                self.sample_count,
                self.device_time_us,
                self.connection_generation,
                self.executor_generation,
                self.interaction_generation,
            ]
            .iter()
            .any(|value| *value > MAX_SAFE_INTEGER)
        {
            return Err("音频采样字段无效");
        }
        self.audio_processing.validate()?;
        Ok(())
    }

    fn validate_payload(&self, payload: &[u8]) -> Result<(), &'static str> {
        match self.audio_codec {
            AudioCodec::Pcm if payload.len() == self.sample_count as usize * 2 => Ok(()),
            AudioCodec::Pcm => Err("PCM 负载字节数与有效采样数不一致"),
            AudioCodec::Opus if self.sample_count == 320 && (1..=1275).contains(&payload.len()) => {
                Ok(())
            }
            AudioCodec::Opus => Err("Opus 上行帧必须为 320 个采样位置及一个有界 raw 包"),
        }
    }
}

/// PlaybackCommand（播放控制命令）：事务编号和播放键在设备间稳定关联。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "operation", content = "arguments", rename_all = "snake_case")]
pub enum PlaybackCommand {
    Pause(PlaybackKeyArgs),                     // pause（暂停固定播放事务）。
    Resume(PlaybackKeyArgs),                    // resume（恢复固定播放事务）。
    StopAndSnapshot(StopAndSnapshotArgs),       // stop_and_snapshot（停止并冻结播放快照）。
    PrepareReplacement(PrepareReplacementArgs), // prepare_replacement（撤销旧流并受理替换）。
    BindReplacement(BindReplacementArgs),       // bind_replacement（绑定固定后继播放事务）。
    AbortReplacement(AbortReplacementArgs),     // abort_replacement（结束替换过渡并返回事实）。
    WaitReplacement(WaitReplacementArgs),       // wait_replacement（等待原替换编号的设备结算）。
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlaybackKeyArgs {
    pub playback_key: String, // playback_key（固定播放事务编号）。
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StopAndSnapshotArgs {
    #[serde(default)]
    pub playback_key: Option<String>, // playback_key（固定播放事务键）。
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PrepareReplacementArgs {
    #[serde(default)]
    pub playback_key: Option<String>, // playback_key（要替换的旧播放事务键）。
    pub max_ms: u32, // max_ms（旧音收尾最大时长，毫秒）。
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BindReplacementArgs {
    pub replacement_id: String, // replacement_id（替换事务编号）。
    pub playback_key: String,   // playback_key（固定后继播放事务键）。
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AbortReplacementArgs {
    pub replacement_id: String, // replacement_id（替换事务编号）。
    pub reason: String,         // reason（撤销原因）。
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WaitReplacementArgs {
    pub replacement_id: String, // replacement_id（要等待结算的替换事务编号）。
}

/// DeviceControlRequest（设备控制请求）：每条命令绑定来源核心及两个设备代次。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DeviceControlRequest {
    pub protocol_version: u32,      // protocol_version（设备协议版本）。
    pub device_id: String,          // device_id（目标设备编号）。
    pub core_session_id: String,    // core_session_id（权威核心运行编号）。
    pub connection_generation: u64, // connection_generation（连接代次）。
    pub executor_generation: u64,   // executor_generation（执行器代次）。
    pub request_id: String,         // request_id（控制请求编号）。
    #[serde(default)]
    pub task_id: Option<String>, // task_id（关联的持久任务编号）。
    pub command: PlaybackCommand,   // command（受限播放控制命令）。
}

impl DeviceControlRequest {
    fn validate(&self) -> Result<(), &'static str> {
        if self.protocol_version != PROTOCOL_VERSION {
            return Err("设备控制协议版本不受支持");
        }
        for value in [&self.device_id, &self.core_session_id, &self.request_id] {
            validate_id(value)?;
        }
        if let Some(task_id) = &self.task_id {
            validate_id(task_id)?;
        }
        if self.connection_generation == 0
            || self.executor_generation == 0
            || self.connection_generation > MAX_SAFE_INTEGER
            || self.executor_generation > MAX_SAFE_INTEGER
        {
            return Err("设备控制代次无效");
        }
        match &self.command {
            PlaybackCommand::Pause(args) | PlaybackCommand::Resume(args) => {
                validate_id(&args.playback_key)?;
            }
            PlaybackCommand::StopAndSnapshot(args) => {
                validate_optional_key(args.playback_key.as_deref())?
            }
            PlaybackCommand::PrepareReplacement(args) => {
                validate_optional_key(args.playback_key.as_deref())?;
                if args.max_ms == 0 || args.max_ms > 600 {
                    return Err("替换收尾时长无效");
                }
            }
            PlaybackCommand::BindReplacement(args) => {
                validate_id(&args.replacement_id)?;
                validate_id(&args.playback_key)?;
            }
            PlaybackCommand::AbortReplacement(args) => {
                validate_id(&args.replacement_id)?;
                validate_id(&args.reason)?;
            }
            PlaybackCommand::WaitReplacement(args) => validate_id(&args.replacement_id)?,
        }
        Ok(())
    }
}

/// PlaybackControlStatus（播放控制状态）：unknown（未知）必须等待原事务查询。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PlaybackControlStatus {
    Accepted,  // accepted（设备已受理）。
    Completed, // completed（设备已完成并带回执）。
    Rejected,  // rejected（设备明确拒绝）。
    Unknown,   // unknown（执行结果未知，须查询原请求）。
}

/// ReceiptPrecision（回执精度）：标出快照时钟证据的来源与精度。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReceiptPrecision {
    Callback,   // callback（音频回调采样精度）。
    DeviceTime, // device_time（设备时钟映射精度）。
    Estimated,  // estimated（估算精度）。
    Unknown,    // unknown（无法确认精度）。
}

/// PlaybackSnapshotReceipt（播放状态回执）：设备时间与采样进度独立保留。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlaybackSnapshotReceipt {
    pub playback_key: Option<String>, // playback_key（原播放事务键）。
    pub enqueued_ms: u64,             // enqueued_ms（已入队时长，毫秒）。
    pub played_ms: u64,               // played_ms（设备回调已消费时长，毫秒）。
    pub buffered_ms: u64,             // buffered_ms（设备缓冲时长，毫秒）。
    pub output_estimated_ms: u64,     // output_estimated_ms（估计已输出时长，毫秒）。
    pub started: bool,                // started（设备是否确认开始输出）。
    pub device_time_us: u64,          // device_time_us（设备单调时刻，微秒）。
    pub precision: ReceiptPrecision,  // precision（快照时钟精度）。
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlaybackStateReceipt {
    pub playback_key: String, // playback_key（固定播放事务编号）。
    pub paused: bool,         // paused（设备确认的暂停状态）。
}

/// PlaybackReplacementTicketReceipt（替换票据回执）：准备阶段只受理旧流并返回编号。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlaybackReplacementTicketReceipt {
    pub replacement_id: String,            // replacement_id（替换事务编号）。
    pub old_playback_key: Option<String>,  // old_playback_key（固定旧播放事务键）。
    pub prepared_device_time_us: u64,      // prepared_device_time_us（设备单调准备时刻，微秒）。
    pub snapshot: PlaybackSnapshotReceipt, // snapshot（受理替换时的可信状态）。
}

/// PlaybackReplacementReceipt（替换回执）：无进程内播放器对象的最终旧音事实。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlaybackReplacementReceipt {
    pub replacement_id: String,       // replacement_id（替换事务编号）。
    pub reason: String,               // reason（旧音结束原因）。
    pub started_device_time_us: u64,  // started_device_time_us（设备单调受理时刻，微秒）。
    pub finished_device_time_us: u64, // finished_device_time_us（设备确认结束时刻，微秒）。
    pub events_device_time_us: std::collections::BTreeMap<String, u64>, // events_device_time_us（设备单调时序事实，微秒）。
    pub attenuated_range_ms: [u32; 2], // attenuated_range_ms（旧音不确定听达区间，毫秒）。
    pub snapshot: PlaybackSnapshotReceipt, // snapshot（旧音停止后的可信状态）。
}

/// PlaybackControlResult（播放控制结果）：命令完成后可返回快照或替换结算。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", content = "value", rename_all = "snake_case")]
pub enum PlaybackControlResult {
    State(PlaybackStateReceipt),
    Snapshot(PlaybackSnapshotReceipt),
    ReplacementPrepared(PlaybackReplacementTicketReceipt),
    Replacement(PlaybackReplacementReceipt),
}

/// DeviceControlReceipt（设备控制回执）：相关编号、代次和回执序号均须对应原请求。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DeviceControlReceipt {
    pub protocol_version: u32,         // protocol_version（设备协议版本）。
    pub device_id: String,             // device_id（实际回执设备编号）。
    pub core_session_id: String,       // core_session_id（原权威核心运行编号）。
    pub connection_generation: u64,    // connection_generation（原连接代次）。
    pub executor_generation: u64,      // executor_generation（原执行器代次）。
    pub request_id: String,            // request_id（对应的控制请求编号）。
    pub receipt_sequence: u64,         // receipt_sequence（该设备连接的回执序号）。
    pub status: PlaybackControlStatus, // status（命令受理、完成、拒绝或未知）。
    pub precision: ReceiptPrecision,   // precision（回执时钟精度）。
    #[serde(default)]
    pub result: Option<PlaybackControlResult>, // result（已确认时的纯数据结果）。
    #[serde(default)]
    pub error: Option<String>, // error（不含敏感凭据的错误说明）。
}

impl DeviceControlReceipt {
    fn validate(&self) -> Result<(), &'static str> {
        if self.protocol_version != PROTOCOL_VERSION {
            return Err("设备回执协议版本不受支持");
        }
        for value in [&self.device_id, &self.core_session_id, &self.request_id] {
            validate_id(value)?;
        }
        if self.connection_generation == 0
            || self.executor_generation == 0
            || self.receipt_sequence == 0
            || [
                self.connection_generation,
                self.executor_generation,
                self.receipt_sequence,
            ]
            .iter()
            .any(|value| *value > MAX_SAFE_INTEGER)
        {
            return Err("设备回执代次或序号无效");
        }
        if self.status == PlaybackControlStatus::Unknown && self.result.is_some() {
            return Err("未知设备回执不得包含已确认结果");
        }
        if self.status == PlaybackControlStatus::Unknown
            && self.precision != ReceiptPrecision::Unknown
        {
            return Err("未知设备回执必须标记未知精度");
        }
        if self.status == PlaybackControlStatus::Completed && self.result.is_none() {
            return Err("已完成设备回执缺少结果快照");
        }
        if let Some(result) = &self.result {
            match result {
                PlaybackControlResult::State(state) => validate_id(&state.playback_key)?,
                PlaybackControlResult::Snapshot(snapshot) => snapshot.validate()?,
                PlaybackControlResult::ReplacementPrepared(ticket) => ticket.validate()?,
                PlaybackControlResult::Replacement(replacement) => replacement.validate()?,
            }
        }
        if let Some(error) = &self.error {
            validate_text(error, 512)?;
        }
        Ok(())
    }
}

impl PlaybackSnapshotReceipt {
    fn validate(&self) -> Result<(), &'static str> {
        validate_optional_key(self.playback_key.as_deref())?;
        if [
            self.enqueued_ms,
            self.played_ms,
            self.buffered_ms,
            self.output_estimated_ms,
            self.device_time_us,
        ]
        .iter()
        .any(|value| *value > MAX_SAFE_INTEGER)
        {
            return Err("播放快照数值超出安全范围");
        }
        Ok(())
    }
}

impl PlaybackReplacementTicketReceipt {
    fn validate(&self) -> Result<(), &'static str> {
        validate_id(&self.replacement_id)?;
        validate_optional_key(self.old_playback_key.as_deref())?;
        if self.prepared_device_time_us > MAX_SAFE_INTEGER {
            return Err("替换票据设备时刻无效");
        }
        self.snapshot.validate()
    }
}

impl PlaybackReplacementReceipt {
    fn validate(&self) -> Result<(), &'static str> {
        validate_id(&self.replacement_id)?;
        validate_id(&self.reason)?;
        if self.finished_device_time_us < self.started_device_time_us
            || [self.started_device_time_us, self.finished_device_time_us]
                .iter()
                .any(|value| *value > MAX_SAFE_INTEGER)
            || self.attenuated_range_ms[1] < self.attenuated_range_ms[0]
        {
            return Err("替换结算时间或区间无效");
        }
        for (name, value) in &self.events_device_time_us {
            validate_id(name)?;
            if *value > MAX_SAFE_INTEGER {
                return Err("替换事件时间超出安全范围");
            }
        }
        self.snapshot.validate()
    }
}

fn validate_id(value: &str) -> Result<(), &'static str> {
    validate_text(value, 128).map_err(|_| "设备控制编号无效")
}

fn validate_text(value: &str, max_chars: usize) -> Result<(), &'static str> {
    if value.is_empty() || value.chars().count() > max_chars || value.trim() != value {
        return Err("设备控制文本无效");
    }
    Ok(())
}

fn validate_optional_key(value: Option<&str>) -> Result<(), &'static str> {
    if let Some(value) = value {
        validate_id(value)?;
    }
    Ok(())
}

/// encode_control（编码控制请求）：在序列化前后都限制 JSON 大小。
pub fn encode_control(request: &DeviceControlRequest) -> Result<Vec<u8>, String> {
    request.validate().map_err(str::to_owned)?;
    let bytes = serde_json::to_vec(request).map_err(|_| "设备控制请求无法编码".to_owned())?;
    if bytes.len() > MAX_CONTROL_BYTES {
        return Err("设备控制请求过大".into());
    }
    Ok(bytes)
}

/// decode_control（解码控制请求）：先限制消息长度，再做严格结构与代次校验。
pub fn decode_control(bytes: &[u8]) -> Result<DeviceControlRequest, String> {
    if bytes.is_empty() || bytes.len() > MAX_CONTROL_BYTES {
        return Err("设备控制消息长度无效".into());
    }
    let request: DeviceControlRequest = serde_json::from_slice(bytes)
        .map_err(|_| "设备控制消息不是有效 JSON（结构化数据）".to_owned())?;
    request.validate().map_err(str::to_owned)?;
    Ok(request)
}

/// encode_receipt（编码设备回执）：回执长度上限与请求相同。
pub fn encode_receipt(receipt: &DeviceControlReceipt) -> Result<Vec<u8>, String> {
    receipt.validate().map_err(str::to_owned)?;
    let bytes = serde_json::to_vec(receipt).map_err(|_| "设备回执无法编码".to_owned())?;
    if bytes.len() > MAX_CONTROL_BYTES {
        return Err("设备回执过大".into());
    }
    Ok(bytes)
}

/// decode_receipt（解码设备回执）：拒绝超长、未知字段和不对应的未知结果负载。
pub fn decode_receipt(bytes: &[u8]) -> Result<DeviceControlReceipt, String> {
    if bytes.is_empty() || bytes.len() > MAX_CONTROL_BYTES {
        return Err("设备回执长度无效".into());
    }
    let receipt: DeviceControlReceipt = serde_json::from_slice(bytes)
        .map_err(|_| "设备回执不是有效 JSON（结构化数据）".to_owned())?;
    receipt.validate().map_err(str::to_owned)?;
    Ok(receipt)
}

/// encode_frame（编码帧）：头部与负载按计划使用大端长度及二进制采样。
pub fn encode_frame(header: &MediaHeader, payload: &[u8]) -> Result<Vec<u8>, String> {
    header.validate().map_err(str::to_owned)?;
    header.validate_payload(payload).map_err(str::to_owned)?;
    if payload.is_empty() || payload.len() > MAX_PAYLOAD_BYTES {
        return Err("音频负载长度无效".into());
    }
    let encoded = serde_json::to_vec(header).map_err(|_| "音频帧头无法编码".to_owned())?;
    if encoded.len() > MAX_HEADER_BYTES {
        return Err("音频帧头过大".into());
    }
    let mut result = Vec::with_capacity(4 + encoded.len() + payload.len());
    result.extend_from_slice(&(encoded.len() as u32).to_be_bytes());
    result.extend_from_slice(&encoded);
    result.extend_from_slice(payload);
    Ok(result)
}

/// decode_frame（解码帧）：先限制消息长度，再解析结构化帧头。
pub fn decode_frame(message: &[u8]) -> Result<(MediaHeader, &[u8]), String> {
    if message.len() < 4 || message.len() > 4 + MAX_HEADER_BYTES + MAX_PAYLOAD_BYTES {
        return Err("音频消息长度无效".into());
    }
    let header_size = u32::from_be_bytes([message[0], message[1], message[2], message[3]]) as usize;
    if header_size == 0 || header_size > MAX_HEADER_BYTES || message.len() <= 4 + header_size {
        return Err("音频帧头或负载长度无效".into());
    }
    let header: MediaHeader = serde_json::from_slice(&message[4..4 + header_size])
        .map_err(|_| "音频帧头不是有效的版本 1 结构".to_owned())?;
    header.validate().map_err(str::to_owned)?;
    let payload = &message[4 + header_size..];
    header.validate_payload(payload).map_err(str::to_owned)?;
    if payload.len() > MAX_PAYLOAD_BYTES {
        return Err("音频负载过大".into());
    }
    Ok((header, payload))
}

/// encode_playback_frame（编码下行 PCM）：帧头和小端 int16 音频保持二进制分离。
pub fn encode_playback_frame(
    header: &PlaybackMediaHeader,
    payload: &[u8],
) -> Result<Vec<u8>, String> {
    header.validate(payload).map_err(str::to_owned)?;
    let encoded = serde_json::to_vec(header).map_err(|_| "下行播放帧头无法编码".to_owned())?;
    if encoded.is_empty() || encoded.len() > MAX_HEADER_BYTES {
        return Err("下行播放帧头超过长度上限".into());
    }
    let mut result = Vec::with_capacity(4 + encoded.len() + payload.len());
    result.extend_from_slice(&(encoded.len() as u32).to_be_bytes());
    result.extend_from_slice(&encoded);
    result.extend_from_slice(payload);
    Ok(result)
}

/// decode_playback_frame（解码下行 PCM）：先限制总长，再核验设备目标和采样数。
pub fn decode_playback_frame(message: &[u8]) -> Result<(PlaybackMediaHeader, &[u8]), String> {
    if message.len() < 4 || message.len() > 4 + MAX_HEADER_BYTES + MAX_PAYLOAD_BYTES {
        return Err("下行播放消息长度无效".into());
    }
    let header_size = u32::from_be_bytes([message[0], message[1], message[2], message[3]]) as usize;
    if header_size == 0 || header_size > MAX_HEADER_BYTES || message.len() <= 4 + header_size {
        return Err("下行播放帧头或 PCM 负载长度无效".into());
    }
    let header: PlaybackMediaHeader = serde_json::from_slice(&message[4..4 + header_size])
        .map_err(|_| "下行播放帧头不是有效 JSON 结构".to_owned())?;
    let payload = &message[4 + header_size..];
    header.validate(payload).map_err(str::to_owned)?;
    Ok((header, payload))
}

/// encode_client_tts_audio_frame（拒绝旧版客户端合成上行 PCM）：版本 2 不允许音频返回服务端。
pub fn encode_client_tts_audio_frame(
    _header: &ClientTtsAudioHeader,
    _payload: &[u8],
) -> Result<Vec<u8>, String> {
    Err("设备媒体协议第 2 版禁止客户端合成音频回传".into())
}

/// decode_client_tts_audio_frame（拒绝旧版客户端合成上行 PCM）：不解析或保留音频负载。
pub fn decode_client_tts_audio_frame(
    _message: &[u8],
) -> Result<(ClientTtsAudioHeader, &[u8]), String> {
    Err("设备媒体协议第 2 版禁止客户端合成音频回传".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn audio_processing() -> AudioProcessingMetadata {
        AudioProcessingMetadata {
            state: AudioProcessingHealth::Ready,
            near_end_voice: NullableMetadata(Some(false)),
            protected_interruption_available: true,
            reverse_generation: 4,
            error: NullableMetadata(None),
            fields: [
                ("state".into(), "AEC、降噪与 VAD 健康状态".into()),
                ("near_end_voice".into(), "近端 VAD 判断汇总".into()),
                (
                    "protected_interruption_available".into(),
                    "是否允许受保护打断".into(),
                ),
                ("reverse_generation".into(), "播放参考格式代次".into()),
                ("error".into(), "前处理故障摘要".into()),
            ]
            .into(),
        }
    }

    fn header() -> MediaHeader {
        MediaHeader {
            protocol_version: MEDIA_PROTOCOL_VERSION,
            device_id: "B".into(),
            core_session_id: "core".into(),
            connection_generation: 1,
            executor_generation: 1,
            interaction_generation: 1,
            stream_id: "设备-B".into(),
            sequence: 1,
            start_sample: 320,
            sample_count: 320,
            device_time_us: 20_000,
            audio_codec: AudioCodec::Pcm,
            discontinuity_before: false,
            audio_processing: audio_processing(),
        }
    }

    fn playback_header() -> PlaybackMediaHeader {
        PlaybackMediaHeader {
            protocol_version: MEDIA_PROTOCOL_VERSION,
            direction: "playback".into(),
            device_id: "device-a".into(),
            delivery_device_id: "device-a".into(),
            core_session_id: "core-a".into(),
            connection_generation: 2,
            executor_generation: 3,
            interaction_generation: 4,
            stream_id: "media-a".into(),
            playback_key: "speech-a".into(),
            sequence: 0,
            start_sample: 0,
            sample_count: 320,
            sample_rate: 16_000,
            host_send_time_us: 12_000,
            audio_codec: AudioCodec::Pcm,
        }
    }

    fn client_tts_audio_header() -> ClientTtsAudioHeader {
        ClientTtsAudioHeader {
            protocol_version: MEDIA_PROTOCOL_VERSION,
            direction: "tts_audio".into(),
            device_id: "device-a".into(),
            core_session_id: "core-a".into(),
            connection_generation: 2,
            executor_generation: 3,
            interaction_generation: 4,
            stream_id: "media-a".into(),
            task_id: "speech-a".into(),
            request_id: "tts-request-a".into(),
            sequence: 0,
            start_sample: 0,
            sample_count: 320,
            sample_rate: 16_000,
            device_time_us: 12_000,
        }
    }

    fn control_request() -> DeviceControlRequest {
        DeviceControlRequest {
            protocol_version: 1,
            device_id: "设备-B".into(),
            core_session_id: "core".into(),
            connection_generation: 2,
            executor_generation: 3,
            request_id: "request-1".into(),
            task_id: Some("task-1".into()),
            command: PlaybackCommand::PrepareReplacement(PrepareReplacementArgs {
                playback_key: Some("playback-1".into()),
                max_ms: 600,
            }),
        }
    }

    fn replacement_receipt() -> DeviceControlReceipt {
        DeviceControlReceipt {
            protocol_version: 1,
            device_id: "设备-B".into(),
            core_session_id: "core".into(),
            connection_generation: 2,
            executor_generation: 3,
            request_id: "request-1".into(),
            receipt_sequence: 8,
            status: PlaybackControlStatus::Completed,
            precision: ReceiptPrecision::DeviceTime,
            result: Some(PlaybackControlResult::Replacement(
                PlaybackReplacementReceipt {
                    replacement_id: "replacement-1".into(),
                    reason: "early_handoff".into(),
                    started_device_time_us: 20_000,
                    finished_device_time_us: 220_000,
                    events_device_time_us: [("old_audio_silenced".into(), 220_000)].into(),
                    attenuated_range_ms: [0, 200],
                    snapshot: PlaybackSnapshotReceipt {
                        playback_key: Some("playback-1".into()),
                        enqueued_ms: 500,
                        played_ms: 300,
                        buffered_ms: 0,
                        output_estimated_ms: 300,
                        started: true,
                        device_time_us: 220_000,
                        precision: ReceiptPrecision::DeviceTime,
                    },
                },
            )),
            error: None,
        }
    }

    #[test]
    fn playback_control_requests_round_trip_with_generation_and_ticket_identity() {
        let request = control_request();
        let encoded = encode_control(&request).unwrap();
        let decoded = decode_control(&encoded).unwrap();
        assert_eq!(decoded, request);
        assert!(String::from_utf8(encoded).unwrap().contains("replacement"));
    }

    #[test]
    fn playback_pause_resume_requests_and_confirmed_state_receipts_round_trip() {
        for command in [
            PlaybackCommand::Pause(PlaybackKeyArgs {
                playback_key: "playback-1".into(),
            }),
            PlaybackCommand::Resume(PlaybackKeyArgs {
                playback_key: "playback-1".into(),
            }),
        ] {
            let mut request = control_request();
            request.command = command;
            assert_eq!(
                decode_control(&encode_control(&request).unwrap()).unwrap(),
                request
            );
        }
        let mut receipt = replacement_receipt();
        receipt.result = Some(PlaybackControlResult::State(PlaybackStateReceipt {
            playback_key: "playback-1".into(),
            paused: true,
        }));
        assert_eq!(
            decode_receipt(&encode_receipt(&receipt).unwrap()).unwrap(),
            receipt
        );
    }

    #[test]
    fn playback_receipt_round_trip_preserves_device_time_and_unknown_status() {
        let receipt = replacement_receipt();
        let encoded = encode_receipt(&receipt).unwrap();
        assert_eq!(decode_receipt(&encoded).unwrap(), receipt);
        let mut unknown = receipt;
        unknown.status = PlaybackControlStatus::Unknown;
        unknown.precision = ReceiptPrecision::Unknown;
        unknown.result = None;
        assert_eq!(
            decode_receipt(&encode_receipt(&unknown).unwrap()).unwrap(),
            unknown
        );
    }

    #[test]
    fn replacement_prepare_receipt_returns_only_ticket_and_snapshot_data() {
        let mut receipt = replacement_receipt();
        receipt.result = Some(PlaybackControlResult::ReplacementPrepared(
            PlaybackReplacementTicketReceipt {
                replacement_id: "replacement-2".into(),
                old_playback_key: Some("playback-1".into()),
                prepared_device_time_us: 15_000,
                snapshot: PlaybackSnapshotReceipt {
                    playback_key: Some("playback-1".into()),
                    enqueued_ms: 500,
                    played_ms: 100,
                    buffered_ms: 400,
                    output_estimated_ms: 100,
                    started: true,
                    device_time_us: 15_000,
                    precision: ReceiptPrecision::DeviceTime,
                },
            },
        ));
        let decoded = decode_receipt(&encode_receipt(&receipt).unwrap()).unwrap();
        assert_eq!(decoded, receipt);
        let json = String::from_utf8(encode_receipt(&receipt).unwrap()).unwrap();
        assert!(!json.contains("transition"));
        assert!(json.contains("prepared_device_time_us"));
    }

    #[test]
    fn control_messages_reject_oversized_input_unknown_fields_and_invalid_generations() {
        assert!(decode_control(&vec![b'x'; MAX_CONTROL_BYTES + 1]).is_err());
        let mut request = control_request();
        request.connection_generation = 0;
        assert!(encode_control(&request).is_err());
        let unknown_field = br#"{"protocol_version":1,"device_id":"B","core_session_id":"core","connection_generation":1,"executor_generation":1,"request_id":"r","task_id":null,"command":{"operation":"wait_replacement","arguments":{"replacement_id":"x","extra":1}}}"#;
        assert!(decode_control(unknown_field).is_err());
        let mut receipt = replacement_receipt();
        receipt.status = PlaybackControlStatus::Unknown;
        assert!(encode_receipt(&receipt).is_err());
    }

    #[test]
    fn frame_round_trip_preserves_binary_and_unicode() {
        let payload: Vec<u8> = (0..=255).collect();
        let mut header = header();
        header.sample_count = 128;
        let bytes = encode_frame(&header, &payload).unwrap();
        let (decoded, decoded_payload) = decode_frame(&bytes).unwrap();
        assert_eq!(decoded, header);
        assert_eq!(decoded_payload, payload);
    }

    #[test]
    fn media_frame_round_trip_keeps_device_processing_state_and_reverse_generation() {
        let header = header();
        let encoded = encode_frame(&header, &[0; 640]).unwrap();
        let (decoded, _) = decode_frame(&encoded).unwrap();
        assert_eq!(decoded.audio_processing, header.audio_processing);
        assert_eq!(decoded.audio_processing.reverse_generation, 4);
    }

    #[test]
    fn media_frame_rejects_missing_or_inconsistent_processing_evidence() {
        let payload = vec![0_u8; 640];
        let encoded = encode_frame(&header(), &payload).unwrap();
        let prefix_len = 4;
        let header_len = u32::from_be_bytes(encoded[..prefix_len].try_into().unwrap()) as usize;
        let mut wire_header: serde_json::Value =
            serde_json::from_slice(&encoded[prefix_len..prefix_len + header_len]).unwrap();
        wire_header["audio_processing"]
            .as_object_mut()
            .unwrap()
            .remove("near_end_voice");
        let header_bytes = serde_json::to_vec(&wire_header).unwrap();
        let mut missing_field = (header_bytes.len() as u32).to_be_bytes().to_vec();
        missing_field.extend_from_slice(&header_bytes);
        missing_field.extend_from_slice(&payload);
        assert!(decode_frame(&missing_field).is_err());

        let mut invalid = header();
        invalid.audio_processing.state = AudioProcessingHealth::Degraded;
        assert!(encode_frame(&invalid, &payload).is_err());

        let mut invalid = header();
        invalid.audio_processing.reverse_generation = MAX_SAFE_INTEGER + 1;
        assert!(encode_frame(&invalid, &payload).is_err());
    }

    #[test]
    fn opus_frame_round_trip_preserves_codec_and_bounded_packet() {
        let mut header = header();
        header.audio_codec = AudioCodec::Opus;
        let payload = vec![0x5a; 1275];
        let bytes = encode_frame(&header, &payload).unwrap();
        let (decoded, decoded_payload) = decode_frame(&bytes).unwrap();
        assert_eq!(decoded, header);
        assert_eq!(decoded_payload, payload);

        let oversized_payload = vec![0x5a; 1276];
        assert!(encode_frame(&header, &oversized_payload).is_err());
    }

    #[test]
    fn playback_frame_round_trip_binds_fixed_device_key_and_pcm_samples() {
        let payload = vec![0x34; 640];
        let encoded = encode_playback_frame(&playback_header(), &payload).unwrap();
        let (header, decoded_payload) = decode_playback_frame(&encoded).unwrap();
        assert_eq!(header, playback_header());
        assert_eq!(decoded_payload, payload);
    }

    #[test]
    fn media_v2_rejects_client_tts_pcm_return() {
        let header = client_tts_audio_header();
        let payload = vec![0x34; 640];
        assert!(encode_client_tts_audio_frame(&header, &payload).is_err());
    }

    #[test]
    fn media_v2_validates_client_output_evidence_and_authorization_metadata() {
        let progress = ClientTtsPlaybackProgress {
            playback_key: "speech-a".into(),
            accepted_samples: 1_600,
            played_samples: 1_600,
            buffered_samples: 0,
            output_complete: true,
            device_time_us: 30_000,
        };
        assert!(progress.validate().is_ok());
        let forged = ClientTtsPlaybackProgress {
            played_samples: 1_700,
            ..progress
        };
        assert!(forged.validate().is_err());
        let metadata = ClientTtsAuthorizationMetadata {
            authorization_id: "auth-a".into(),
            playback_key: "speech-a".into(),
            provider: "minimax".into(),
            ttl_ms: 120_000,
        };
        assert!(metadata.validate().is_ok());
        let invalid = ClientTtsAuthorizationMetadata {
            ttl_ms: 300_001,
            ..metadata
        };
        assert!(invalid.validate().is_err());
    }

    #[test]
    fn client_tts_audio_frame_decoder_rejects_unknown_header_fields() {
        let header = serde_json::json!({
            "protocol_version":2,"direction":"tts_audio","device_id":"device-a",
            "core_session_id":"core-a","connection_generation":2,"executor_generation":3,
            "interaction_generation":4,"stream_id":"media-a","task_id":"speech-a",
            "request_id":"tts-request-a","sequence":0,"start_sample":0,
            "sample_count":320,"sample_rate":16_000,"device_time_us":12_000,
            "unexpected":"reject"
        })
        .to_string()
        .into_bytes();
        let mut message = (header.len() as u32).to_be_bytes().to_vec();
        message.extend_from_slice(&header);
        message.extend_from_slice(&[0_u8; 640]);
        assert!(decode_client_tts_audio_frame(&message).is_err());
        assert!(decode_client_tts_audio_frame(&vec![
            0_u8;
            4 + MAX_HEADER_BYTES + MAX_PAYLOAD_BYTES + 1
        ])
        .is_err());
    }

    #[test]
    fn playback_opus_frame_preserves_valid_sample_mapping_and_packet_bounds() {
        let mut header = playback_header();
        header.audio_codec = AudioCodec::Opus;
        header.sample_count = 648;
        header.sample_rate = 48_000;
        let packet = vec![0x5a; 1275];
        let encoded = encode_playback_frame(&header, &packet).unwrap();
        let (decoded, decoded_packet) = decode_playback_frame(&encoded).unwrap();
        assert_eq!(decoded, header);
        assert_eq!(decoded.sample_count, 648);
        assert_eq!(decoded_packet, packet);

        let mut invalid_header = header.clone();
        invalid_header.sample_rate = 32_000;
        assert!(encode_playback_frame(&invalid_header, &[0x5a; 100]).is_err());
        invalid_header = header;
        invalid_header.sample_count = 961;
        assert!(encode_playback_frame(&invalid_header, &[0x5a; 100]).is_err());
        invalid_header.sample_count = 648;
        assert!(encode_playback_frame(&invalid_header, &[0x5a; 1276]).is_err());
    }

    #[test]
    fn playback_pcm_frame_accepts_legacy_header_without_codec_tag() {
        let header = serde_json::to_value(playback_header()).unwrap();
        let mut header = header.as_object().unwrap().clone();
        header.remove("audio_codec");
        let header = serde_json::to_vec(&serde_json::Value::Object(header)).unwrap();
        let payload = vec![0x34; 640];
        let mut message = (header.len() as u32).to_be_bytes().to_vec();
        message.extend_from_slice(&header);
        message.extend_from_slice(&payload);
        let (decoded, decoded_payload) = decode_playback_frame(&message).unwrap();
        assert_eq!(decoded, playback_header());
        assert_eq!(decoded_payload, payload);
    }

    #[test]
    fn playback_frame_rejects_wrong_target_rate_length_and_unknown_header_fields() {
        let payload = vec![0_u8; 640];
        let mut header = playback_header();
        header.delivery_device_id = "device-b".into();
        assert!(encode_playback_frame(&header, &payload).is_err());
        header = playback_header();
        header.sample_rate = 22_050;
        assert!(encode_playback_frame(&header, &payload).is_err());
        header = playback_header();
        header.sample_count = 321;
        assert!(encode_playback_frame(&header, &payload).is_err());
        let encoded = serde_json::json!({
            "protocol_version":2,"direction":"playback","device_id":"device-a",
            "delivery_device_id":"device-a","core_session_id":"core-a",
            "connection_generation":2,"executor_generation":3,"interaction_generation":4,
            "stream_id":"media-a","playback_key":"speech-a","sequence":0,
            "start_sample":0,"sample_count":320,"sample_rate":16_000,"host_send_time_us":12_000,
            "unexpected":"rejected"
        })
        .to_string()
        .into_bytes();
        let mut message = (encoded.len() as u32).to_be_bytes().to_vec();
        message.extend_from_slice(&encoded);
        message.extend_from_slice(&payload);
        assert!(decode_playback_frame(&message).is_err());
    }

    #[test]
    fn python_json_header_is_accepted_and_duplicate_fields_rejected() {
        let json = r#"{"protocol_version":2,"device_id":"B","core_session_id":"core","connection_generation":1,"executor_generation":1,"interaction_generation":1,"stream_id":"s","sequence":0,"start_sample":0,"sample_count":1,"device_time_us":0,"discontinuity_before":false,"audio_processing":{"state":"ready","near_end_voice":false,"protected_interruption_available":true,"reverse_generation":0,"error":null,"fields":{"state":"AEC、降噪与 VAD 健康状态","near_end_voice":"近端 VAD 判断汇总","protected_interruption_available":"是否允许受保护打断","reverse_generation":"播放参考格式代次","error":"前处理故障摘要"}}}"#;
        let mut message = (json.len() as u32).to_be_bytes().to_vec();
        message.extend_from_slice(json.as_bytes());
        message.extend_from_slice(&[0, 0]);
        assert_eq!(decode_frame(&message).unwrap().0.stream_id, "s");
        let repeated = r#"{"protocol_version":2,"protocol_version":2,"device_id":"B","core_session_id":"core","connection_generation":1,"executor_generation":1,"interaction_generation":1,"stream_id":"s","sequence":0,"start_sample":0,"sample_count":1,"device_time_us":0,"discontinuity_before":false,"audio_processing":{"state":"ready","near_end_voice":false,"protected_interruption_available":true,"reverse_generation":0,"error":null,"fields":{"state":"AEC、降噪与 VAD 健康状态","near_end_voice":"近端 VAD 判断汇总","protected_interruption_available":"是否允许受保护打断","reverse_generation":"播放参考格式代次","error":"前处理故障摘要"}}}"#;
        let mut malformed = (repeated.len() as u32).to_be_bytes().to_vec();
        malformed.extend_from_slice(repeated.as_bytes());
        malformed.push(1);
        assert!(decode_frame(&malformed).is_err());
    }

    #[test]
    fn invalid_lengths_and_metadata_are_rejected() {
        assert!(decode_frame(&[]).is_err());
        assert!(decode_frame(&(MAX_HEADER_BYTES as u32 + 1).to_be_bytes()).is_err());
        assert!(encode_frame(&header(), &[]).is_err());
        assert!(encode_frame(&header(), &vec![0; MAX_PAYLOAD_BYTES + 1]).is_err());
        let mut bad = header();
        bad.sample_count = 0;
        assert!(encode_frame(&bad, &[0]).is_err());
        bad = header();
        bad.sequence = MAX_SAFE_INTEGER + 1;
        assert!(encode_frame(&bad, &[0]).is_err());
    }
}
