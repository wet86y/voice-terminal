#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod app_icon;
mod agent_debug;
mod integrity_commands;
use integrity_commands::{
    app_integrity_check, app_integrity_download, app_integrity_status, maintenance_payload,
    MaintenanceAction,
};
#[cfg(feature = "ssh-device")]
mod client_tts_relay;
mod credential_commands;
mod device_operation;
mod environment_commands;
mod initialization;
#[cfg(feature = "qualification")]
mod qualification;
mod remote_import;
#[cfg(feature = "ssh-device")]
mod remote_management;
#[cfg(feature = "ssh-device")]
mod remote_services;
mod resource_commands;
mod runtime_recovery;
use credential_commands::{
    audio_enhancements_settings, inference_selection_save, model_credentials_save,
    model_credentials_status, tts_selection_save, tts_selection_status,
};
use environment_commands::{
    environment_guide, gpu_environment_probe, gpu_environment_register, resource_install_plan,
    voice_environment_configure, voice_environment_import, voice_environment_plan,
    voice_environment_status,
};
use initialization::{
    desktop_cancel_init, desktop_initialize, desktop_manage, desktop_start_service,
};
use resource_commands::{
    aniflive_import_choose_folder, aniflive_import_start, resource_cancel, resource_download,
    resource_download_required, resource_import, resource_import_choose_sources,
    resource_import_confirm, resource_import_start, resource_inventory, resource_jobs,
    resource_open_source, resource_reset, resource_reset_plan, resource_system_feature_access,
    resource_system_feature_enable, resource_system_feature_status, ResourceManager,
};
use std::{
    collections::{HashMap, HashSet, VecDeque},
    fs::{self, File},
    future::Future,
    io::{BufRead, BufReader, Write},
    os::windows::process::CommandExt,
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, Stdio},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Condvar, Mutex,
    },
    time::{Duration, Instant},
};
use tauri::{
    menu::{CheckMenuItem, Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    webview::PageLoadEvent,
    Emitter, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder,
};
use voice_desktop_common::{
    windows::Job,
    workspace::{self, Locator},
    LaunchContext,
};

const APP_UPDATE_PROGRESS_EVENT: &str = "desktop://app-update-progress";
const MAX_DESKTOP_CONTROL_MESSAGE_BYTES: usize = 512 * 1024; // MAX_DESKTOP_CONTROL_MESSAGE_BYTES（受管宿主控制消息最大字节数）。
struct AppUpdateManager {
    status: Mutex<serde_json::Value>, // status（界面展示的更新阶段与进度）。
    manifest: Mutex<Option<voice_desktop_common::app_update::AppUpdateManifest>>, // manifest（已校验的发行清单）。
    busy: AtomicBool,        // busy（更新任务互斥标记）。
    cancel: Arc<AtomicBool>, // cancel（安装器下载取消信号）。
}
impl AppUpdateManager {
    fn new() -> Self {
        Self {
            status: Mutex::new(serde_json::Value::Null),
            manifest: Mutex::new(None),
            busy: AtomicBool::new(false),
            cancel: Arc::new(AtomicBool::new(false)),
        }
    }
    // request_cancel（取消准入）与 begin_handoff（交接门闩）共用状态锁，成功取消不会再启动安装器。
    fn request_cancel(&self) -> Result<(), String> {
        let status = self.status.lock().map_err(|_| "更新状态不可用。")?;
        if !self.busy.load(Ordering::Acquire) || status["can_cancel"] != true {
            return Err("当前更新阶段不允许取消。".into());
        }
        self.cancel.store(true, Ordering::Release);
        Ok(())
    }
    fn begin_handoff(&self, payload: serde_json::Value) -> Result<(), String> {
        let mut status = self.status.lock().map_err(|_| "更新状态不可用。")?;
        if self.cancel.load(Ordering::Acquire) {
            return Err("更新交接已取消。".into());
        }
        *status = payload;
        Ok(())
    }
}

struct Service {
    child: Child,
    input: ChildStdin,
    job: Job,
}

#[cfg(feature = "ssh-device")]
struct RemoteDeviceTaskChannel {
    stop: Arc<AtomicBool>,      // stop（停止标记）：只在当前设备通道退出时读取。
    running: Arc<AtomicBool>,   // running（轮询任务是否仍在运行）。
    device_id: String,          // device_id（本机登记到服务端的稳定设备编号）。
    connection_generation: u64, // connection_generation（主机授予的管理连接代次）。
    executor_generation: u64,   // executor_generation（主机授予的客户端执行器代次）。
    desktop_control: Result<(), String>, // desktop_control（登记时的桌面辅助能力及失败原因）。
    task: tauri::async_runtime::JoinHandle<()>, // task（受管异步轮询任务）。
}

#[cfg(feature = "ssh-device")]
fn remote_device_task_status(
    channel: Option<&RemoteDeviceTaskChannel>,
    error: Option<&str>,
) -> serde_json::Value {
    let running = channel.is_some_and(|channel| channel.running.load(Ordering::Acquire));
    let desktop_control = channel
        .filter(|_| running)
        .map(|channel| &channel.desktop_control);
    serde_json::json!({
        "state": if running { "running" } else if error.is_some() { "error" } else { "stopped" },
        "device_id": channel.map(|channel| &channel.device_id),
        "connection_generation": channel.map(|channel| channel.connection_generation),
        "executor_generation": channel.map(|channel| channel.executor_generation),
        "error": error,
        "desktop_control": desktop_control.map(Result::is_ok),
        "desktop_control_error": desktop_control.and_then(|status| status.as_ref().err()),
        "fields": {
            "state": "受管远端任务通道状态",
            "device_id": "当前已登记的客户端设备编号",
            "connection_generation": "主机授予的设备连接代次",
            "executor_generation": "主机授予的客户端执行器代次",
            "error": "最近一次任务通道错误",
            "desktop_control": "当前任务通道是否已登记桌面控制能力；空值表示通道未运行",
            "desktop_control_error": "桌面能力登记失败原因"
        }
    })
}

#[cfg(feature = "ssh-device")]
struct RemoteDeviceMediaChannel {
    stop: Arc<AtomicBool>,       // stop（停止媒体发送与接收的信号）。
    running: Arc<AtomicBool>,    // running（媒体通道是否仍在运行）。
    capture_active: Arc<AtomicBool>, // capture_active（远端物理麦克风是否实际启动）。
    device_id: String,           // device_id（当前选为语音设备的本机编号）。
    stream_id: String,           // stream_id（当前连续音频流编号）。
    connection_generation: u64,  // connection_generation（主机登记的设备连接代次）。
    executor_generation: u64,    // executor_generation（主机登记的设备执行器代次）。
    interaction_generation: u64, // interaction_generation（主机授予的语音交互代次）。
    task: tauri::async_runtime::JoinHandle<()>, // task（采集、WebSocket 收发及资源回收任务）。
}

#[cfg(feature = "ssh-device")]
struct RemoteVoiceForwarder {
    url: String, // url（仅本机回环、带随机路径票据的服务端语音 WebSocket 地址）。
    running: Arc<AtomicBool>, // running（转发 listener 是否仍在运行）。
    stop: tokio::sync::watch::Sender<bool>, // stop（停止本机 listener 与所有连接的信号）。
    task: tauri::async_runtime::JoinHandle<()>, // task（固定 SSH control 桥接 listener）。
}

#[cfg(feature = "ssh-device")]
enum MediaWriteCommand {
    Binary(Arc<Vec<u8>>), // binary（有界设备音频帧）。
    Text(String),    // text（有界媒体确认控制消息）。
    Pong(Vec<u8>),   // pong（回应主机 WebSocket 保活探测）。
    Close,           // close（关闭媒体 WebSocket）。
}

#[cfg(feature = "ssh-device")]
enum MediaReadEvent {
    Message(voice_desktop_common::ssh_device::SshMediaMessage), // message（主机媒体控制或下行数据）。
    Failed(String),                                             // failed（媒体读通道错误）。
}

#[cfg(feature = "ssh-device")]
#[derive(Clone, Copy)]
struct PlaybackSnapshotBaseline {
    enqueued_ms: u64,         // enqueued_ms（当前播放事务开始前的设备累计入队时长）。
    played_ms: u64,           // played_ms（当前播放事务开始前的设备累计提交时长）。
    output_estimated_ms: u64, // output_estimated_ms（当前播放事务开始前的设备时钟输出位置）。
    isolated: bool,           // isolated（开始前无旧音缓冲，快照可归属当前播放键）。
}

#[cfg(feature = "ssh-device")]
impl PlaybackSnapshotBaseline {
    fn measure(self, current: PlaybackSnapshotView, accepted_ms: u64) -> (u64, u64, bool) {
        let submitted = current.played_ms.saturating_sub(self.played_ms);
        let output = current.output_estimated_ms.saturating_sub(self.output_estimated_ms);
        let drained = self.isolated && current.buffered_ms == 0
            && output >= current.enqueued_ms.saturating_sub(self.enqueued_ms);
        if drained {
            return (accepted_ms, accepted_ms, current.started);
        }
        (submitted.min(accepted_ms), output.min(submitted).min(accepted_ms), output > 0)
    }
}

#[cfg(feature = "ssh-device")]
#[derive(Clone, Copy, Default)]
struct PlaybackSnapshotView {
    enqueued_ms: u64,         // enqueued_ms（设备累计接受时长）。
    played_ms: u64,           // played_ms（已提交给 WASAPI 的累计时长）。
    buffered_ms: u64,         // buffered_ms（端点和本地队列缓冲时长）。
    output_estimated_ms: u64, // output_estimated_ms（按设备时钟估计的输出位置）。
    started: bool,            // started（设备输出时钟是否已推进）。
    device_time_us: u64,      // device_time_us（WASAPI 对应的 QPC 时刻，微秒）。
}

#[cfg(feature = "ssh-device")]
#[derive(Clone, Copy)]
struct ClientTtsPlaybackLedger {
    sample_rate: u32,                   // sample_rate（固定播放事务的 PCM 采样率）。
    accepted_samples: u64,              // accepted_samples（该事务已加入原生播放器的累计样本数）。
    played_samples: u64,                // played_samples（该事务已经确认输出的累计样本数）。
    baseline: PlaybackSnapshotBaseline, // baseline（首次入队前采集的设备时钟基线）。
}

#[cfg(feature = "ssh-device")]
#[derive(Clone)]
struct RemotePlaybackStopReceipt {
    snapshot: serde_json::Value, // snapshot（用于通用停播任务的设备快照）。
    client_tts: Option<voice_desktop_common::device_protocol::ClientTtsPlaybackProgress>, // client_tts（直放停播时的精确采样进度）。
}

#[cfg(feature = "ssh-device")]
#[derive(Clone)]
struct RemoteReplacementBinding {
    replacement_id: String,   // replacement_id（远端设备端替换编号）。
    old_playback_key: String, // old_playback_key（被替换且拒绝迟到帧的旧播放键）。
    successor_playback_key: Option<String>, // successor_playback_key（已确认绑定的后继播放键）。
    candidate_successor_key: Option<String>, // candidate_successor_key（绑定前已被拒收、可能随后绑定的后继键）。
    candidate_next_sample: u64, // candidate_next_sample（该待绑定后继已拒收帧后的下一个源采样位置）。
}

#[cfg(feature = "ssh-device")]
const MAX_REMOTE_PLAYBACK_KEYS: usize = 256; // MAX_REMOTE_PLAYBACK_KEYS（单条媒体通道可跟踪的未结算播放键上限）。

#[cfg(feature = "ssh-device")]
const MAX_REMOTE_PLAYBACK_TOMBSTONES: usize = 256; // MAX_REMOTE_PLAYBACK_TOMBSTONES（拒收迟到帧的有界终态编号数）。

#[cfg(feature = "ssh-device")]
const MAX_REMOTE_OPUS_DECODER_STATES: usize = 512; // MAX_REMOTE_OPUS_DECODER_STATES（每条媒体流的 Opus 解码状态上限）。

#[cfg(feature = "ssh-device")]
struct RemoteOpusPlaybackDecoder {
    #[cfg(all(windows, feature = "native-audio"))]
    decoder: voice_desktop_common::native_opus::OpusDecoder, // decoder（该播放键独占的 48 kHz Opus 解码状态）。
    #[cfg(all(windows, feature = "native-audio"))]
    pre_skip_remaining: u32, // pre_skip_remaining（首包需丢弃的编码器预延迟样本数）。
}

#[cfg(feature = "ssh-device")]
impl RemoteOpusPlaybackDecoder {
    fn new(pre_skip_samples: u32) -> Result<Self, String> {
        if pre_skip_samples == 0 || pre_skip_samples > 960 {
            return Err("Opus 下行预延迟样本数无效。".into());
        }
        #[cfg(all(windows, feature = "native-audio"))]
        {
            Ok(Self {
                decoder: voice_desktop_common::native_opus::OpusDecoder::new(48_000)?,
                pre_skip_remaining: pre_skip_samples,
            })
        }
        #[cfg(not(all(windows, feature = "native-audio")))]
        {
            let _ = pre_skip_samples;
            Err("当前构建没有原生 Opus 下行解码能力。".into())
        }
    }

    fn decode_valid_pcm(&mut self, packet: &[u8], valid_samples: u64) -> Result<Vec<u8>, String> {
        #[cfg(all(windows, feature = "native-audio"))]
        {
            let decoded = self.decoder.decode(packet)?;
            let skip = usize::try_from(self.pre_skip_remaining)
                .map_err(|_| "Opus 预延迟样本数超出设备范围")?;
            if skip > decoded.len() {
                return Err("Opus 首帧预延迟超过解码样本数。".into());
            }
            let valid =
                usize::try_from(valid_samples).map_err(|_| "Opus 有效采样数超出设备范围")?;
            let end = skip.checked_add(valid).ok_or("Opus 有效采样范围溢出")?;
            if end > decoded.len() {
                return Err("Opus 帧有效采样映射超出解码样本范围。".into());
            }
            self.pre_skip_remaining = 0;
            let mut pcm = Vec::with_capacity(valid * 2);
            for sample in &decoded[skip..end] {
                pcm.extend_from_slice(&sample.to_le_bytes());
            }
            Ok(pcm)
        }
        #[cfg(not(all(windows, feature = "native-audio")))]
        {
            let _ = (packet, valid_samples);
            Err("当前构建没有原生 Opus 下行解码能力。".into())
        }
    }
}

#[cfg(feature = "ssh-device")]
#[derive(Default)]
struct RemotePlaybackControlState {
    device_id: Option<String>, // device_id（当前控制状态绑定的本机设备编号）。
    core_session_id: Option<String>, // core_session_id（当前媒体流所属权威核心编号）。
    connection_generation: u64, // connection_generation（固定媒体流连接代次）。
    executor_generation: u64,  // executor_generation（固定设备执行器代次）。
    interaction_generation: u64, // interaction_generation（固定语音媒体租约代次）。
    downlink_codec: voice_desktop_common::device_protocol::AudioCodec, // downlink_codec（签名握手固定的下行编码模式）。
    downlink_encoder_pre_skip_samples: u32, // downlink_encoder_pre_skip_samples（下行首包应丢弃的预延迟样本数）。
    active_key: Option<String>,             // active_key（当前正在写入原生播放队列的播放键）。
    paused: bool,                           // paused（已由本机设备线程确认的暂停状态）。
    control_uncertain: bool, // control_uncertain（暂停/恢复结果未知时隔离后续 PCM）。
    stopped_keys: HashSet<String>, // stopped_keys（已确认停止并丢弃迟到帧的播放键）。
    stopped_key_order: VecDeque<String>, // stopped_key_order（已停止播放键的有界淘汰顺序）。
    stopped_receipts: HashMap<String, RemotePlaybackStopReceipt>, // stopped_receipts（已确认停止的原始回执）。
    replaced_keys: HashSet<String>, // replaced_keys（已进入替换事务并丢弃迟到帧的旧播放键）。
    replaced_key_order: VecDeque<String>, // replaced_key_order（已替换播放键的有界淘汰顺序）。
    pending_replacement: Option<RemoteReplacementBinding>, // pending_replacement（需按设备事实结算的单一替换事务）。
    settled_replacements: HashMap<String, serde_json::Value>, // settled_replacements（有界窗口内可重复读取的替换结算回执）。
    settled_replacement_order: VecDeque<String>, // settled_replacement_order（替换回执的有界淘汰顺序）。
    baselines: HashMap<String, PlaybackSnapshotBaseline>, // baselines（播放键开始前的设备快照）。
    samples: HashMap<String, (u32, u64)>,        // samples（各播放键采样率与实际接收入队采样数）。
    client_tts: HashMap<String, ClientTtsPlaybackLedger>, // client_tts（同一媒体租约内的直放采样台账）。
    client_tts_cancellation: Option<(String, voice_desktop_common::client_tts::MiniMaxTtsCancellation)>, // client_tts_cancellation（当前唯一直放分段的取消信号）。
    opus_decoders: HashMap<String, RemoteOpusPlaybackDecoder>, // opus_decoders（按播放键隔离的下行 Opus 解码状态）。
}

#[cfg(feature = "ssh-device")]
impl RemotePlaybackControlState {
    fn forget_client_tts(&mut self, playback_key: &str) {
        if self.client_tts.remove(playback_key).is_some()
            && self.active_key.as_deref() == Some(playback_key)
        {
            self.active_key = None;
        }
        if self
            .client_tts_cancellation
            .as_ref()
            .is_some_and(|(key, _)| key == playback_key)
        {
            self.client_tts_cancellation = None;
        }
    }

    fn snapshot_value(
        &self,
        playback_key: &str,
        current: PlaybackSnapshotView,
    ) -> Result<serde_json::Value, String> {
        let (baseline, sample_rate, sample_count) =
            if let Some(tts) = self.client_tts.get(playback_key) {
                (tts.baseline, tts.sample_rate, tts.accepted_samples)
            } else {
                let baseline = *self
                    .baselines
                    .get(playback_key)
                    .ok_or("播放键缺少设备快照基线")?;
                let (rate, count) = *self
                    .samples
                    .get(playback_key)
                    .ok_or("播放键缺少已接收采样计数")?;
                (baseline, rate, count)
            };
        if sample_rate == 0 {
            return Err("播放键采样率无效".into());
        }
        let enqueued_ms = sample_count.saturating_mul(1_000) / u64::from(sample_rate);
        let (played_ms, output_estimated_ms, started) = baseline.measure(current, enqueued_ms);
        Ok(serde_json::json!({
            "playback_key":playback_key,
            "enqueued_ms":enqueued_ms,
            "played_ms":played_ms,
            "buffered_ms":current.buffered_ms,
            "output_estimated_ms":output_estimated_ms,
            "started":baseline.isolated && !self.control_uncertain && started,
            "device_time_us":current.device_time_us,
            "precision":if baseline.isolated && !self.control_uncertain{"device_time"}else{"unknown"},
        }))
    }
}

struct LogFlushState {
    completed_request: u64, // completed_request（已完成刷新请求编号）。
    failed_request: u64,    // failed_request（刷新失败请求编号）。
}

struct ResetFlag<'a>(&'a AtomicBool);
impl Drop for ResetFlag<'_> {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum FrontendMode {
    Product,   // Product（正式产品界面）。
    UiPreview, // UI preview（桌面样例界面）。
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum DesktopCommand {
    Status,               // status（桌面状态读取）。
    SkipStartup,          // skip startup（跳过加载画面，服务继续启动）。
    ExportLogs,           // export logs（打包并导出产品日志）。
    OpenConsole,          // open console（打开产品控制台）。
    Exit,                 // exit（退出当前桌面实例）。
    FocusMain,            // focus main（聚焦产品主窗口）。
    PublishFloatingModel, // publish floating model（发布悬浮窗只读状态）。
}

fn command_allowed(mode: FrontendMode, command: DesktopCommand) -> bool {
    match command {
        DesktopCommand::Status | DesktopCommand::SkipStartup | DesktopCommand::Exit => true,
        DesktopCommand::ExportLogs
        | DesktopCommand::OpenConsole
        | DesktopCommand::FocusMain
        | DesktopCommand::PublishFloatingModel => mode == FrontendMode::Product,
    }
}

fn require_command(mode: FrontendMode, command: DesktopCommand) -> Result<(), String> {
    if command_allowed(mode, command) {
        return Ok(());
    }
    let name = match command {
        DesktopCommand::Status => "状态读取",
        DesktopCommand::SkipStartup => "跳过加载画面",
        DesktopCommand::ExportLogs => "导出产品日志",
        DesktopCommand::OpenConsole => "产品控制台",
        DesktopCommand::Exit => "退出应用",
        DesktopCommand::FocusMain => "产品主窗口",
        DesktopCommand::PublishFloatingModel => "悬浮窗状态",
    };
    Err(format!("桌面样例不支持{name}命令。"))
}

impl FrontendMode {
    fn label(self) -> &'static str {
        match self {
            Self::Product => "product",
            Self::UiPreview => "ui-preview",
        }
    }
}

/// frontend_capability（业务页面权限）：只授权当前实例的主窗口与实际页面来源。
fn frontend_capability(
    mode: FrontendMode,
    normalized_origin: &str,
    epoch: u64,
) -> serde_json::Value {
    let mut capability = if mode == FrontendMode::UiPreview {
        serde_json::json!({"identifier":"owned-ui-preview", "description":"桌面样例的状态读取、加载画面和退出操作", "local":false,"windows":["main"],"remote":{"urls":[format!("{normalized_origin}/*")]},"permissions":["allow-desktop-status","allow-skip-startup","allow-exit-application"]})
    } else {
        serde_json::json!({"identifier":format!("owned-main-{epoch}"), "description":"当前实例产品页面的受限桌面操作", "local":false,"windows":["main"],"remote":{"urls":[format!("{normalized_origin}/*")]},"permissions":["allow-desktop-status","allow-desktop-page-ready","allow-skip-startup","allow-export-logs","allow-open-console","allow-exit-application","allow-focus-main","allow-workspace-info","allow-workspace-choose","allow-workspace-link","allow-workspace-migrate","allow-workspace-cleanup","allow-resource-inventory","allow-resource-jobs","allow-resource-system-feature-status","allow-resource-system-feature-enable","allow-resource-system-feature-access","allow-device-discovery-search","allow-resource-download", "allow-resource-download-required", "allow-inference-selection-save","allow-tts-selection-status", "allow-tts-selection-save","allow-audio-enhancements-settings","allow-resource-reset-plan","allow-resource-reset","allow-resource-import","allow-resource-import-choose-sources","allow-resource-import-start","allow-resource-import-confirm","allow-aniflive-import-choose-folder","allow-aniflive-import-start","allow-resource-open-source","allow-resource-cancel","allow-desktop-manage","allow-desktop-start-service","allow-desktop-cancel-init","allow-model-credentials-status","allow-model-credentials-save","allow-voice-environment-status","allow-voice-environment-plan","allow-resource-install-plan","allow-voice-environment-configure","allow-voice-environment-import","allow-gpu-environment-register","allow-gpu-environment-probe","allow-environment-guide","allow-app-integrity-status","allow-app-integrity-download","allow-app-integrity-check","allow-app-update-check","allow-app-update-download","allow-app-update-cancel","allow-project-open-repository","allow-open-minimax-voice-library","core:event:allow-listen","core:event:allow-unlisten"]})
    };
    if mode == FrontendMode::Product {
        if let Some(permissions) = capability["permissions"].as_array_mut() {
            permissions.push(serde_json::json!("allow-agent-debug-set-allowed"));
            permissions.push(serde_json::json!("allow-agent-debug-trace-native"));
            permissions.push(serde_json::json!("allow-device-config-status"));
            permissions.push(serde_json::json!("allow-device-config-save"));
            permissions.push(serde_json::json!("allow-device-worker-start"));
            permissions.push(serde_json::json!("allow-device-worker-status"));
            permissions.push(serde_json::json!("allow-device-worker-audio-devices"));
            permissions.push(serde_json::json!(
                "allow-device-worker-desktop-helper-health"
            ));
            permissions.push(serde_json::json!("allow-device-worker-capture-start"));
            permissions.push(serde_json::json!("allow-device-worker-capture-next"));
            permissions.push(serde_json::json!("allow-device-worker-capture-stop"));
            permissions.push(serde_json::json!("allow-device-worker-playback-start"));
            permissions.push(serde_json::json!("allow-device-worker-playback-enqueue"));
            permissions.push(serde_json::json!("allow-device-worker-playback-snapshot"));
            permissions.push(serde_json::json!("allow-device-worker-playback-stop"));
            #[cfg(feature = "ssh-device")]
            for permission in [
                "allow-device-pairing-window-open",
                "allow-device-pairing-window-close",
                "allow-device-pairing-window-status",
                "allow-device-pairing-connect",
                "allow-device-ssh-probe",
                "allow-device-ssh-identity-create",
                "allow-device-ssh-identity-status",
                "allow-device-ssh-identity-delete",
                "allow-device-ssh-connect",
                "allow-device-ssh-request",
                "allow-device-ssh-management-request",
                "allow-device-ssh-import-start",
                "allow-device-ssh-import-cancel",
                "allow-device-ssh-tts-asset-import",
                "allow-device-ssh-pairing-request",
                "allow-device-ssh-transport-identity-import",
                "allow-device-ssh-transport-identity-status",
                "allow-device-ssh-transport-identity-delete",
                "allow-device-ssh-disconnect",
                "allow-device-ssh-status",
                "allow-device-ssh-task-channel",
                "allow-device-ssh-media-channel",
                "allow-device-ssh-voice-forwarder",
            ] {
                permissions.push(serde_json::json!(permission));
            }
        }
    }
    capability
}

/// floating_capability（悬浮窗权限）：仅授权本实例悬浮窗展示、输入启停及窗口交互。
fn floating_capability(normalized_origin: &str, epoch: u64) -> serde_json::Value {
    let embedded = normalized_origin == REMOTE_STATIC_PRODUCT_ORIGIN;
    serde_json::json!({"identifier":format!("owned-floating-{epoch}"), "description":"当前实例悬浮窗的展示、语音输入与窗口切换权限", "local":embedded,"windows":["floating"],"remote":{"urls":[format!("{normalized_origin}/*")]},"permissions":["allow-get-floating-model","allow-return-to-workbench","allow-start-floating-drag","allow-control-floating-session","allow-agent-debug-trace-native","core:event:allow-listen","core:event:allow-unlisten"]})
}

/// floating_publisher_capability（悬浮窗状态发布权限）：主窗口只能更新浮窗展示快照。
fn floating_publisher_capability(normalized_origin: &str, epoch: u64) -> serde_json::Value {
    serde_json::json!({"identifier":format!("owned-floating-publisher-{epoch}"), "description":"当前实例主工作台的悬浮窗动效发布权限", "local":false,"windows":["main"],"remote":{"urls":[format!("{normalized_origin}/*")]},"permissions":["allow-publish-floating-model"]})
}

struct Desktop {
    context: Mutex<Option<LaunchContext>>, // context（产品模式的业务启动上下文）；预览模式不读取。
    initialization: Mutex<serde_json::Value>, // initialization（启动画面状态）。
    initialization_cancel: Arc<AtomicBool>, // initialization_cancel（本体展开取消信号）。
    program: PathBuf,                      // program（当前前端项目根目录）。
    data: PathBuf,                         // data（当前模式隔离的 WebView2 缓存目录）。
    webview2: Option<PathBuf>,             // webview2（可选的固定网页运行时）。
    mode: FrontendMode,                    // mode（前端模式：产品或桌面样例）。
    development: bool,                     // development（是否显式源码调试）。
    instance_identifier: String, // instance_identifier（工作区单实例标识；演示单独隔离）。
    service: Mutex<Option<Service>>,
    device_worker: Mutex<Option<voice_desktop_common::device_worker::ManagedWorker>>, // device_worker（设备工作进程）：独立于核心服务的受管生命周期。
    #[cfg(feature = "ssh-device")]
    remote_desktop_helper: Mutex<Option<voice_desktop_common::device_helper::DeviceHelper>>, // remote_desktop_helper（远端桌面辅助进程）：与录放音隔离。
    #[cfg(all(test, feature = "ssh-device"))]
    playback_worker_test_responses: Mutex<VecDeque<serde_json::Value>>, // playback_worker_test_responses（Tauri 替换任务单元测试的设备回执序列）。
    #[cfg(all(test, feature = "ssh-device"))]
    playback_worker_test_operations: Mutex<Vec<String>>, // playback_worker_test_operations（已调用的替换设备操作顺序）。
    #[cfg(feature = "ssh-device")]
    ssh_device: voice_desktop_common::ssh_device::SshDeviceManager, // ssh_device（SSH 设备管理器）：私钥仅由原生层读取。
    #[cfg(feature = "ssh-device")]
    pairing_window: Mutex<
        Option<
            Arc<std::sync::Mutex<Option<voice_desktop_common::device_pairing::PairingCodeWindow>>>,
        >,
    >, // pairing_window（本机内存 OPAQUE 配对窗口，可原子关闭）。
    #[cfg(feature = "ssh-device")]
    pairing_listener:
        tokio::sync::Mutex<Option<voice_desktop_common::device_pairing::PairingListener>>, // pairing_listener（受限局域网配对监听器）。
    #[cfg(feature = "ssh-device")]
    discovery_publication:
        Mutex<Option<voice_desktop_common::device_discovery::DiscoveryPublication>>, // discovery_publication（当前 mDNS 公告租约）。
    #[cfg(feature = "ssh-device")]
    remote_services: Mutex<Option<Arc<remote_services::RemoteServices>>>, // remote_services（广播开启期间的原生管理与文件回环服务）。
    #[cfg(feature = "ssh-device")]
    remote_import_transfers: Mutex<HashMap<String, Arc<AtomicBool>>>, // remote_import_transfers（客户端当前 FILE 上传及取消信号）。
    #[cfg(feature = "ssh-device")]
    discovery_targets: Mutex<
        std::collections::HashMap<String, voice_desktop_common::device_discovery::DiscoveredDevice>,
    >, // discovery_targets（最近一次 mDNS 搜索结果，原生连接会回查此表）。
    discovery_search_expires_at_ms: AtomicU64, // discovery_search_expires_at_ms（最近一次发现结果的有限有效期）。
    #[cfg(feature = "ssh-device")]
    device_terminal: voice_desktop_common::device_terminal::DeviceTerminalManager, // device_terminal（设备终端）：受限命令任务与增量输出。
    #[cfg(feature = "ssh-device")]
    remote_task_channel: Mutex<Option<RemoteDeviceTaskChannel>>, // remote_task_channel（当前远端设备任务通道）。
    #[cfg(feature = "ssh-device")]
    remote_task_channel_starting: AtomicBool, // remote_task_channel_starting（防止并行建立会话）。
    #[cfg(feature = "ssh-device")]
    remote_task_channel_error: Mutex<Option<String>>, // remote_task_channel_error（最近一次通道错误，界面可见）。
    #[cfg(feature = "ssh-device")]
    remote_media_channel: Mutex<Option<RemoteDeviceMediaChannel>>, // remote_media_channel（当前远端语音媒体连接）。
    #[cfg(feature = "ssh-device")]
    remote_media_channel_starting: AtomicBool, // remote_media_channel_starting（防止并行切换语音媒体流）。
    remote_media_stop_revision: AtomicU64, // remote_media_stop_revision（拒绝在停止后发布迟到的媒体连接）。
    #[cfg(feature = "ssh-device")]
    remote_media_channel_error: Mutex<Option<String>>, // remote_media_channel_error（最近一次音频通道错误）。
    #[cfg(feature = "ssh-device")]
    remote_voice_forwarder: Mutex<Option<RemoteVoiceForwarder>>, // remote_voice_forwarder（本机 loopback 上受随机票据保护的语音事件 WebSocket 转发）。
    #[cfg(feature = "ssh-device")]
    remote_playback_control: tokio::sync::Mutex<RemotePlaybackControlState>, // remote_playback_control（任务通道与媒体流共用的播放控制状态）。
    #[cfg(feature = "ssh-device")]
    remote_playback_stop_epoch: AtomicU64, // remote_playback_stop_epoch（停止后通知媒体循环重新打开原生播放端点）。
    service_transition: Mutex<()>, // service_transition（启动检查与维护停机共用的交接锁）。
    #[cfg(feature = "ssh-device")]
    gateway_control: device_operation::GatewayControl, // gateway_control（当前核心网关开关的私有管道回执）。
    #[cfg(feature = "ssh-device")]
    broadcast_access: Mutex<Option<voice_desktop_common::system_feature::OpenSshServerStatus>>, // broadcast_access（当前广播生命周期内已核验的官方服务与精确规则）。
    broadcast_error: Mutex<Option<String>>, // broadcast_error（附属网络入口失败，不改变本地语音核心状态）。
    device_operation: device_operation::DeviceOperation, // device_operation（跨页面可读的互斥设备设置任务）。
    #[cfg(feature = "ssh-device")]
    discovery_transition: tokio::sync::Mutex<()>, // discovery_transition（公告发布、配对窗口和停止入口的串行交接）。
    credentials_guard: Mutex<()>, // credentials_guard（接口配置互斥锁）：保存与启动共用。
    service_epoch: AtomicU64,     // service_epoch（后台服务代次，防止旧读取线程控制新进程）。
    service_ready_epoch: AtomicU64, // service_ready_epoch（已完成私有入口握手的服务代次）。
    device_gateway_status: Mutex<serde_json::Value>, // device_gateway_status（业务核心上报的共享网关启用、就绪及错误状态）。
    remote_client_connected: AtomicBool, // remote_client_connected（本机角色配置为远端客户端时已认证的固定主机连接）。
    locator: Option<Locator>,            // locator（当前工作区本地身份，不读写系统定位）。
    workspace_guard: Mutex<Option<voice_desktop_common::installation::WorkspaceGuard>>, // workspace_guard（程序运行期间的本体互斥锁）。
    workspace_busy: AtomicBool,  // workspace_busy（防止并行切换或迁移）。
    maintenance_gate: Mutex<()>, // maintenance_gate（维护准入锁）：更新与工作区交接互斥。
    log_export_busy: AtomicBool, // log_export_busy（防止重复打包日志）。
    log_flush_sequence: AtomicU64, // log_flush_sequence（刷新请求递增编号）。
    log_flush_state: Mutex<LogFlushState>, // log_flush_state（最新刷新请求结果）。
    log_flush_changed: Condvar,  // log_flush_changed（唤醒等待刷新结果的导出任务）。
    app_update: AppUpdateManager, // app_update（应用发行检查、下载及安装交接）。
    service_requested: AtomicBool, // service_requested（主窗口已请求启动服务，刷新不重复启动）。
    service_starting: AtomicBool, // service_starting（本次启动正在校验或建立入口）：拒绝重复启动。
    startup_skipped: AtomicBool, // startup_skipped（当前窗口跳过加载画面的选择）。
    recovery: Mutex<runtime_recovery::RuntimeRecovery>, // recovery（同一窗口的组件热更新与连续会话）。
    origin: Mutex<Option<String>>,
    dsh_origin: Mutex<Option<String>>, // dsh_origin（当前受管 DSH 的实际来源，不含令牌）。
    launch_token: Mutex<Option<String>>, // launch_token（当前入口的一次性启动凭据）。
    error: Mutex<Option<String>>,
    stopping: AtomicBool,
    stopped: AtomicBool,
    tray: AtomicBool,
    floating_mode: AtomicBool,  // floating_mode（是否正在以悬浮窗呈现）。
    floating_ready: AtomicBool, // floating_ready（悬浮窗页面是否已加载）。
    floating_transition: Mutex<()>, // floating_transition（模式切换互斥锁）。
    floating_revision: AtomicU64, // floating_revision（快照递增版本）。
    floating_model: Mutex<serde_json::Value>, // floating_model（最近一次语音动效快照）。
    floating_menu: Mutex<Option<CheckMenuItem<tauri::Wry>>>, // floating_menu（托盘悬浮窗勾选项）。
}

impl Desktop {
    fn context(&self) -> Option<LaunchContext> {
        self.context.lock().unwrap().clone()
    }

    fn publish_recovery(&self, app: &tauri::AppHandle) {
        let value = self.recovery.lock().unwrap().snapshot();
        let _ = app.emit("desktop://runtime-recovery", value);
    }

    fn flush_service_logs(&self) -> Result<(), String> {
        if self
            .service
            .lock()
            .map_err(|error| error.to_string())?
            .is_none()
        {
            return Ok(());
        }
        let request_id = self.log_flush_sequence.fetch_add(1, Ordering::AcqRel) + 1;
        self.send(serde_json::json!({"protocol":1,"kind":"flush_logs","request_id":request_id}))?;
        let deadline = Instant::now() + Duration::from_secs(5);
        let mut result = self
            .log_flush_state
            .lock()
            .map_err(|error| error.to_string())?;
        while result.completed_request < request_id {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err("日志刷新超时，请重试打包。".into());
            }
            let (next, timeout) = self
                .log_flush_changed
                .wait_timeout(result, remaining)
                .map_err(|error| error.to_string())?;
            result = next;
            if timeout.timed_out() && result.completed_request < request_id {
                return Err("日志刷新超时，请重试打包。".into());
            }
        }
        if result.failed_request == request_id {
            return Err("无法将最新日志刷新到磁盘，请重试打包。".into());
        }
        Ok(())
    }

    fn stop_service(&self) -> Result<(), String> {
        self.service_epoch.fetch_add(1, Ordering::SeqCst);
        let _ = self.send(serde_json::json!({"protocol":1,"kind":"shutdown","budget_ms":14000}));
        let deadline = Instant::now() + Duration::from_secs(15);
        loop {
            let exited = self
                .service
                .lock()
                .map_err(|e| e.to_string())?
                .as_mut()
                .map(|v| v.child.try_wait().ok().flatten().is_some())
                .unwrap_or(true);
            if exited || Instant::now() >= deadline {
                break;
            }
            std::thread::sleep(Duration::from_millis(80));
        }
        if let Some(mut service) = self.service.lock().map_err(|e| e.to_string())?.take() {
            service.job.terminate();
            let _ = service.child.kill();
            let _ = service.child.wait();
        }
        *self.launch_token.lock().unwrap() = None;
        *self.dsh_origin.lock().unwrap() = None;
        if let Some(context) = self.context() {
            let _ = fs::remove_file(context.data.join("state/workspace-ready.json"));
        }
        Ok(())
    }
    fn allows_frontend_url(&self, url: &tauri::Url) -> bool {
        if self.origin.lock().unwrap().as_deref()
            != Some(url.origin().ascii_serialization().as_str())
        {
            return false;
        }
        let token = self.launch_token.lock().unwrap();
        match self.mode {
            FrontendMode::Product => {
                product_entry_url_allowed(url, token.as_deref()) || product_page_url_allowed(url)
            }
            FrontendMode::UiPreview => {
                preview_entry_url_allowed(url, token.as_deref()) || preview_page_url_allowed(url)
            }
        }
    }

    fn allows_floating_url(&self, url: &tauri::Url) -> bool {
        self.mode == FrontendMode::Product
            && self.origin.lock().unwrap().as_deref()
                == Some(url.origin().ascii_serialization().as_str())
            && floating_page_url_allowed(url)
    }

    fn is_settled_frontend_url(&self, url: &tauri::Url) -> bool {
        match self.mode {
            FrontendMode::Product => product_page_url_allowed(url),
            FrontendMode::UiPreview => preview_page_url_allowed(url),
        }
    }
}

impl Desktop {
    fn send(&self, value: serde_json::Value) -> Result<(), String> {
        let mut guard = self.service.lock().map_err(|e| e.to_string())?;
        let service = guard.as_mut().ok_or("服务入口未建立")?;
        writeln!(service.input, "{value}")
            .and_then(|_| service.input.flush())
            .map_err(|e| e.to_string())
    }
    fn shutdown(self: &Arc<Self>, app: &tauri::AppHandle) {
        if self.stopping.swap(true, Ordering::SeqCst) {
            return;
        }
        if let Some(debug) = app.try_state::<Arc<agent_debug::AgentDebug>>() {
            debug.revoke();
        }
        #[cfg(feature = "ssh-device")]
        self.device_terminal.shutdown();
        #[cfg(feature = "ssh-device")]
        if let Ok(channel) = self.remote_task_channel.lock() {
            if let Some(channel) = channel.as_ref() {
                channel.stop.store(true, Ordering::Release);
            }
        }
        #[cfg(feature = "ssh-device")]
        if let Ok(channel) = self.remote_media_channel.lock() {
            if let Some(channel) = channel.as_ref() {
                channel.stop.store(true, Ordering::Release);
            }
        }
        self.floating_mode.store(false, Ordering::Release);
        if let Some(floating) = app.get_webview_window("floating") {
            let _ = floating.hide();
        }
        // exit motion（退出动画）：所有原生退出入口通知同一页面，后台回收同时进行。
        let motion_started = Instant::now(); // motion_started（一秒过渡的起始时刻）。
        let has_window = app
            .get_webview_window("main")
            .or_else(|| app.get_webview_window("startup"))
            .map(|window| {
                let _ = window.eval("window.dispatchEvent(new Event('voice-terminal:exiting'))");
                true
            })
            .unwrap_or(false);
        *self.origin.lock().unwrap() = None;
        let state = self.clone();
        let app = app.clone();
        std::thread::spawn(move || {
            if let Some(resources) = app.try_state::<Arc<ResourceManager>>() {
                let _ = resources.pause_and_wait();
            }
            #[cfg(feature = "ssh-device")]
            {
                if let Ok(transfers) = state.remote_import_transfers.lock() {
                    for cancel in transfers.values() {
                        cancel.store(true, Ordering::Release);
                    }
                }
                // 同一受控交接负责撤销 mDNS（局域网公告）、配对窗口、管理/文件入口、媒体与任务通道、SSH（安全外壳协议）及本机设备工作进程。
                let _ = tauri::async_runtime::block_on(stop_remote_device_runtime(&state));
                if let Ok(mut helper) = state.remote_desktop_helper.lock() {
                    helper.take();
                }
            }
            let _ = state.stop_service();
            if let Ok(mut worker) = state.device_worker.lock() {
                worker.take();
            }
            // 清理较快时补足窗口展示时间；较慢时保留退出滚动条，不能提前切断后台进程回收。
            if has_window {
                if let Some(remaining) =
                    Duration::from_millis(1000).checked_sub(motion_started.elapsed())
                {
                    std::thread::sleep(remaining);
                }
            }
            state.stopped.store(true, Ordering::SeqCst);
            app.exit(0);
        });
    }
}

fn boot_page_url(url: &tauri::Url) -> bool {
    ["/", "/index.html", "/startup.html", "/management.html"].contains(&url.path())
        && ((url.scheme() == "tauri" && url.host_str() == Some("localhost"))
            || (url.scheme() == "http"
                && url.host_str() == Some("tauri.localhost")
                && url.port().is_none()))
}

/// remote_static_product_page_url（识别内置远程产品页）：接受受控 Tauri 根路径和 index.html。
fn remote_static_product_page_url(url: &tauri::Url) -> bool {
    boot_page_url(url) && matches!(url.path(), "/" | "/index.html")
}

const REMOTE_STATIC_PRODUCT_ORIGIN: &str = "http://tauri.localhost"; // 远程客户端内置页面来源。

// request_service_start（首次主页面加载后请求服务）：刷新及业务导航不会再次启动。
fn request_service_start(
    url: &tauri::Url,
    event: PageLoadEvent,
    requested: &AtomicBool,
    stopping: &AtomicBool,
) -> bool {
    event == PageLoadEvent::Finished
        && boot_page_url(url)
        && !stopping.load(Ordering::SeqCst)
        && !requested.swap(true, Ordering::SeqCst)
}

fn trusted(window: &WebviewWindow, state: &Desktop) -> Result<(), String> {
    if !["main", "startup"].contains(&window.label()) {
        return Err("此窗口没有桌面操作权限。".into());
    }
    let url = window.url().map_err(|e| e.to_string())?;
    if boot_page_url(&url) {
        return Ok(());
    }
    let service_failed = state.error.lock().unwrap().is_some();
    let retained_page = state.recovery.lock().unwrap().preserves_page();
    if !state.stopping.load(Ordering::SeqCst)
        && (!service_failed || retained_page)
        && state.allows_frontend_url(&url)
    {
        Ok(())
    } else {
        Err("页面地址不属于当前应用实例。".into())
    }
}

fn trusted_floating(window: &WebviewWindow, state: &Desktop) -> Result<(), String> {
    if state.mode != FrontendMode::Product || window.label() != "floating" {
        return Err("此窗口没有悬浮窗权限。".into());
    }
    let url = window.url().map_err(|error| error.to_string())?;
    if !state.stopping.load(Ordering::Acquire) && state.allows_floating_url(&url) {
        Ok(())
    } else {
        Err("悬浮窗页面不属于当前应用实例。".into())
    }
}

fn set_floating_menu_checked(state: &Desktop, checked: bool) {
    if let Some(item) = state.floating_menu.lock().unwrap().as_ref() {
        let _ = item.set_checked(checked);
    }
}

fn emit_floating_model(app: &tauri::AppHandle, state: &Desktop) {
    if app.get_webview_window("floating").is_none() {
        return;
    }
    let model = state.floating_model.lock().unwrap().clone();
    let _ = app.emit_to("floating", "voice-terminal:floating-model", model);
}

fn set_floating_mode(app: &tauri::AppHandle, state: &Desktop, enabled: bool) -> Result<(), String> {
    let _transition = state
        .floating_transition
        .lock()
        .map_err(|error| error.to_string())?;
    if state.stopping.load(Ordering::Acquire) {
        return Err("应用正在退出。".into());
    }
    if !enabled {
        state.floating_mode.store(false, Ordering::Release);
        set_floating_menu_checked(state, false);
        if let Some(floating) = app.get_webview_window("floating") {
            let _ = floating.hide();
        }
        if let Some(main) = app
            .get_webview_window("main")
            .or_else(|| app.get_webview_window("startup"))
        {
            let _ = main.show();
            let _ = main.unminimize();
            let _ = main.set_focus();
        }
        return Ok(());
    }

    if state.mode != FrontendMode::Product || state.origin.lock().unwrap().is_none() {
        return Err("工作台尚未就绪，暂时无法打开悬浮窗。".into());
    }
    let main = app
        .get_webview_window("main")
        .ok_or("主工作台窗口尚未建立。")?;
    let floating = match app.get_webview_window("floating") {
        Some(window) => window,
        None => create_floating_window(app, state)?,
    };
    state.floating_mode.store(true, Ordering::Release);
    set_floating_menu_checked(state, true);
    if state.floating_ready.load(Ordering::Acquire) {
        if let Err(error) = floating.show() {
            state.floating_mode.store(false, Ordering::Release);
            set_floating_menu_checked(state, false);
            return Err(error.to_string());
        }
        if let Err(error) = main.hide() {
            let _ = floating.hide();
            state.floating_mode.store(false, Ordering::Release);
            set_floating_menu_checked(state, false);
            return Err(error.to_string());
        }
        emit_floating_model(app, state);
    }
    Ok(())
}

fn create_floating_window(
    app: &tauri::AppHandle,
    state: &Desktop,
) -> Result<WebviewWindow, String> {
    let origin = state
        .origin
        .lock()
        .map_err(|error| error.to_string())?
        .clone()
        .ok_or("工作台尚未建立受控界面来源。")?;
    let url = format!("{origin}/floating.html")
        .parse::<tauri::Url>()
        .map_err(|error| error.to_string())?;
    let navigation_state = app.state::<Arc<Desktop>>().inner().clone();
    let page_state = navigation_state.clone();
    let window = WebviewWindowBuilder::new(app, "floating", WebviewUrl::External(url))
        .title("Voice Terminal · 悬浮窗")
        // 主体占位由 300×282 缩为 150×141，底部另留 39 像素单行字幕空间。
        .inner_size(150., 180.)
        .resizable(false)
        .decorations(false)
        .transparent(true)
        .background_color(tauri::window::Color(0, 0, 0, 0))
        .shadow(false)
        .initialization_script(voice_desktop_common::webview::DISABLE_CONTEXT_MENU)
    .initialization_script(agent_debug::AgentDebug::window_script(app))
        .always_on_top(true)
        .skip_taskbar(true)
        .visible(false)
        .data_directory(
            state
                .context()
                .map(|context| context.install.join("cache/webview2"))
                .unwrap_or_else(|| state.data.clone()),
        )
        .on_navigation(move |url| {
            let available = navigation_state
                .recovery
                .lock()
                .unwrap()
                .allows_navigation();
            available && navigation_state.allows_floating_url(url)
        })
        .on_page_load(move |window, payload| {
            if payload.event() == PageLoadEvent::Started {
                page_state.floating_ready.store(false, Ordering::Release);
                let _ = window.hide();
                if page_state.floating_mode.load(Ordering::Acquire)
                    && !page_state.stopping.load(Ordering::Acquire)
                {
                    if let Some(main) = window.app_handle().get_webview_window("main") {
                        let _ = main.show();
                    }
                }
            }
            if payload.event() != PageLoadEvent::Finished
                || payload.url().path() != "/floating.html"
                || !page_state.allows_floating_url(payload.url())
            {
                return;
            }
            if page_state.floating_ready.load(Ordering::Acquire)
                && page_state.floating_mode.load(Ordering::Acquire)
                && !page_state.stopping.load(Ordering::Acquire)
            {
                if window.show().is_ok() {
                    if let Some(main) = window.app_handle().get_webview_window("main") {
                        let _ = main.hide();
                    }
                    emit_floating_model(window.app_handle(), &page_state);
                } else {
                    let _ = set_floating_mode(window.app_handle(), &page_state, false);
                }
            }
        })
        .build()
        .map_err(|error| error.to_string())?;
    Ok(window)
}

/// floating_caption_projection（纯语音字幕边界）：未知消息或截图附件不进入悬浮窗。
fn floating_caption_projection(value: Option<&serde_json::Value>) -> serde_json::Value {
    (|| {
        let item = value?.as_object()?;
        if item.contains_key("attachments") || item.contains_key("images") {
            return None;
        }
        let role = item.get("role")?.as_str()?;
        if !["user", "assistant"].contains(&role) {
            return None;
        }
        let key = item.get("key")?.as_str()?;
        let text = item.get("text")?.as_str()?;
        let length = text.encode_utf16().count() as u64;
        if text.trim().is_empty() || length > 32000 || key.len() > 512 {
            return None;
        }
        let start = item.get("focusStart")?.as_u64()?;
        let end = item.get("focusEnd")?.as_u64()?;
        if start > end || end > length {
            return None;
        }
        Some(
            serde_json::json!({"key":key,"role":role,"text":text,"focusStart":start,"focusEnd":end,
            "active":item.get("active").and_then(serde_json::Value::as_bool).unwrap_or(false)}),
        )
    })()
    .unwrap_or(serde_json::Value::Null)
}

#[tauri::command(rename_all = "snake_case")]
fn publish_floating_model(
    window: WebviewWindow,
    app: tauri::AppHandle,
    state: tauri::State<Arc<Desktop>>,
    model: serde_json::Value,
) -> Result<(), String> {
    require_command(state.mode, DesktopCommand::PublishFloatingModel)?;
    trusted(&window, &state)?;
    if window.label() != "main" {
        return Err("只有主工作台可以发布悬浮窗状态。".into());
    }
    let status = model
        .get("state")
        .and_then(serde_json::Value::as_str)
        .filter(|value| {
            [
                "standby",
                "listening",
                "thinking",
                "speaking",
                "offline",
                "error",
            ]
            .contains(value)
        })
        .ok_or("悬浮窗运行状态无效。")?;
    let session_active = model
        .get("sessionActive")
        .and_then(serde_json::Value::as_bool)
        .ok_or("悬浮窗会话状态无效。")?;
    let levels = model
        .get("audioLevels")
        .and_then(serde_json::Value::as_array)
        .filter(|levels| levels.len() == 5)
        .ok_or("悬浮窗音量数据无效。")?;
    let normalized_levels = levels
        .iter()
        .map(|level| {
            level
                .as_f64()
                .filter(|value| value.is_finite())
                .map(|value| value.clamp(0.0, 1.0))
                .ok_or("悬浮窗音量数据无效。")
        })
        .collect::<Result<Vec<_>, _>>()?;
    let revision = state.floating_revision.fetch_add(1, Ordering::AcqRel) + 1;
    let projection = serde_json::json!({
        "revision": revision,
        // doubleClickMs（系统双击间隔）：尊重慢速双击设置，不提前执行首击。
        "doubleClickMs": floating_double_click_ms(),
        "state": status,
        "sessionActive": session_active,
        "audioLevels": normalized_levels,
        "silenced": model.get("silenced").and_then(serde_json::Value::as_bool).unwrap_or(!session_active),
        "canControl": model.get("canControl").and_then(serde_json::Value::as_bool).unwrap_or(false),
        "caption": floating_caption_projection(model.get("caption")),
    });
    *state
        .floating_model
        .lock()
        .map_err(|error| error.to_string())? = projection.clone();
    let _ = app.emit_to("floating", "voice-terminal:floating-model", projection);
    Ok(())
}

#[tauri::command]
fn get_floating_model(
    window: WebviewWindow,
    state: tauri::State<Arc<Desktop>>,
) -> Result<serde_json::Value, String> {
    trusted_floating(&window, &state)?;
    // 此请求由悬浮界面挂载后发出；单纯网页加载完成不足以隐藏主工作台。
    if !state.floating_ready.swap(true, Ordering::AcqRel)
        && state.floating_mode.load(Ordering::Acquire)
    {
        set_floating_mode(window.app_handle(), &state, true)?;
    }
    Ok(state
        .floating_model
        .lock()
        .map_err(|error| error.to_string())?
        .clone())
}

fn floating_double_click_ms() -> u32 {
    #[cfg(windows)]
    {
        unsafe {
            windows_sys::Win32::UI::Input::KeyboardAndMouse::GetDoubleClickTime().clamp(500, 5000)
        }
    }
    #[cfg(not(windows))]
    {
        500
    }
}

#[tauri::command]
fn return_to_workbench(
    window: WebviewWindow,
    state: tauri::State<Arc<Desktop>>,
) -> Result<(), String> {
    trusted_floating(&window, &state)?;
    window
        .app_handle()
        .emit_to("main", "voice-terminal:floating-workbench", ())
        .map_err(|error| error.to_string())?;
    set_floating_mode(&window.app_handle(), &state, false)
}

#[tauri::command]
fn control_floating_session(
    window: WebviewWindow,
    state: tauri::State<Arc<Desktop>>,
    action: String,
) -> Result<(), String> {
    trusted_floating(&window, &state)?;
    if !["silence", "start"].contains(&action.as_str()) {
        return Err("悬浮窗语音操作无效。".into());
    }
    if !state.floating_mode.load(Ordering::Acquire) {
        return Err("悬浮窗尚未启用。".into());
    }
    window
        .app_handle()
        .emit_to("main", "voice-terminal:floating-control", action)
        .map_err(|error| error.to_string())
}

#[tauri::command]
fn start_floating_drag(
    window: WebviewWindow,
    state: tauri::State<Arc<Desktop>>,
) -> Result<(), String> {
    trusted_floating(&window, &state)?;
    window.start_dragging().map_err(|error| error.to_string())
}

fn device_runtime_status(state: &Desktop, data_root: &Path) -> serde_json::Value {
    let snapshot = voice_desktop_common::device_config::inspect(data_root);
    let core_ready = state
        .recovery
        .lock()
        .map(|recovery| recovery.current_core == Some(true))
        .unwrap_or(false);
    let service_alive = state
        .service
        .lock()
        .ok()
        .and_then(|mut service| {
            service
                .as_mut()
                .and_then(|value| matches!(value.child.try_wait(), Ok(None)).then_some(()))
        })
        .is_some();
    let gateway = state
        .device_gateway_status
        .lock()
        .map(|value| value.clone())
        .unwrap_or(serde_json::Value::Null);
    let remote_client_selected =
        snapshot.desired_role == Some(voice_desktop_common::device_config::Role::RemoteClient);
    let remote_client_connected = state.remote_client_connected.load(Ordering::Acquire);
    let actual_role = voice_desktop_common::device_config::actual_role(
        service_alive,
        core_ready,
        gateway["enabled"] == true,
        gateway["ready"] == true,
        remote_client_selected,
        remote_client_connected,
    );
    let remote_client_ready =
        remote_client_selected && remote_client_connected && !service_alive && !core_ready;
    let (actual_mode, switch_phase, mut runtime_error) = mode_runtime_projection(
        snapshot.mode,
        actual_role,
        service_alive,
        state.error.lock().ok().and_then(|error| error.clone()),
        snapshot.error.clone(),
    );
    if runtime_error.is_none() {
        runtime_error = state
            .broadcast_error
            .lock()
            .ok()
            .and_then(|error| error.clone());
    }
    #[cfg(feature = "ssh-device")]
    let broadcast_actual = state
        .discovery_publication
        .lock()
        .ok()
        .is_some_and(|publication| publication.is_some())
        && state.remote_services.lock().ok().is_some_and(|services| {
            services
                .as_ref()
                .is_some_and(|current| current.is_running())
        });
    #[cfg(not(feature = "ssh-device"))]
    let broadcast_actual = false;
    let revision = state.service_epoch.load(Ordering::Acquire);
    serde_json::json!({"config_state":snapshot.config_state,"device_id":snapshot.device_id,
        "mode":snapshot.mode,"broadcast_enabled":snapshot.broadcast_enabled,"operation":state.device_operation.snapshot(),
        "expected_mode":snapshot.mode,"actual_mode":actual_mode,"switch_phase":switch_phase,
        "broadcast_requested":snapshot.broadcast_enabled,
        "broadcast_actual":broadcast_actual,
        "revision":revision,
        "desired_role":snapshot.desired_role,"audio_mode":snapshot.audio_mode,"actual_role":actual_role,
        "core_ready":core_ready,"device_gateway":gateway,
        "remote_client_connected":remote_client_connected,
        "config_accepted":voice_desktop_common::device_config::startup_block_reason(&snapshot).is_none() || remote_client_ready,
        "error":runtime_error,
        "fields":{"config_state":"设备配置当前状态","device_id":"本机设备编号；缺少配置时为单机临时标识",
            "mode":"用户选择的本地运行或远程配对模式","broadcast_enabled":"本机广播开关保存状态",
            "expected_mode":"已保存的目标运行模式","actual_mode":"当前真实运行模式；尚未就绪时为空",
            "switch_phase":"运行模式切换状态：ready（已就绪）、waiting_for_remote（等待服务端）、starting_local（正在启动）、stopped（未运行）或 failed（切换/启动失败）",
            "broadcast_requested":"保存的局域网广播意图","broadcast_actual":"原生 mDNS 局域网公告当前是否已发布",
            "revision":"运行模式状态修订号，用于拒绝过期状态回写","operation":"原生设备设置任务，切换页面后继续读取进度与终态",
            "desired_role":"兼容设备角色投影","audio_mode":"兼容音频传输字段",
            "actual_role":"根据本机核心、共享网关或远端已认证 SSH 会话确认的实际角色",
            "core_ready":"当前业务核心能力是否已就绪","device_gateway":"共享设备网关启用及监听状态",
            "remote_client_connected":"本机远端客户端角色是否持有已认证的固定主机连接",
            "config_accepted":"配置格式有效且当前支持该角色的核心启动","error":"最近一次运行模式切换、服务启动或配置错误；失败时保留当前实际模式"}})
}

fn mode_runtime_projection(
    expected_mode: Option<voice_desktop_common::device_config::RunMode>,
    actual_role: &str,
    service_alive: bool,
    runtime_error: Option<String>,
    config_error: Option<String>,
) -> (Option<&'static str>, &'static str, Option<String>) {
    let actual_mode = match actual_role {
        "standalone" | "shared_host" => Some("local"),
        "remote_client" => Some("remote"),
        _ => None,
    };
    let error = runtime_error.or(config_error);
    let switch_phase = if error.is_some() {
        "failed"
    } else if actual_mode.is_some_and(|value| {
        Some(value)
            == expected_mode.map(|mode| match mode {
                voice_desktop_common::device_config::RunMode::Local => "local",
                voice_desktop_common::device_config::RunMode::Remote => "remote",
            })
    }) {
        "ready"
    } else if expected_mode == Some(voice_desktop_common::device_config::RunMode::Remote) {
        "waiting_for_remote"
    } else if service_alive {
        "starting_local"
    } else {
        "stopped"
    };
    (actual_mode, switch_phase, error)
}

/// desktop_status_ready（桌面预检就绪状态）：remote_static_product_page（受控远程静态入口）无需等待本地服务来源。
fn desktop_status_ready(
    mode: FrontendMode,               // mode（当前桌面窗口的入口模式）。
    origin_ready: bool,               // origin_ready（本机受控产品服务来源是否已建立）。
    remote_static_product_page: bool, // remote_static_product_page（已核验的远程静态产品页是否已加载）。
) -> bool {
    origin_ready || (mode == FrontendMode::Product && remote_static_product_page)
}

#[tauri::command]
fn desktop_status(
    window: WebviewWindow,
    state: tauri::State<Arc<Desktop>>,
) -> Result<serde_json::Value, String> {
    require_command(state.mode, DesktopCommand::Status)?;
    trusted(&window, &state)?;
    let current_url = window.url().ok();
    if let Some(url) = current_url.as_ref() {
        if state.is_settled_frontend_url(&url) {
            state.launch_token.lock().unwrap().take();
        }
    }
    let remote_static_product_page = state.mode == FrontendMode::Product
        && current_url
            .as_ref()
            .is_some_and(remote_static_product_page_url)
        && state.context().is_some_and(|context| {
            voice_desktop_common::device_config::inspect(&context.data).mode
                == Some(voice_desktop_common::device_config::RunMode::Remote)
        });
    let ready = desktop_status_ready(
        state.mode,
        state.origin.lock().unwrap().is_some(),
        remote_static_product_page,
    );
    // ready（是否就绪）、error（错误信息）、frontend_mode（前端模式）、development（是否显式源码调试）。
    let runtime_recovery = state.recovery.lock().unwrap().snapshot();
    Ok(serde_json::json!({
        "ready": ready,
        "startup_skipped": state.startup_skipped.load(Ordering::Acquire),
        "runtime_recovery": runtime_recovery, // runtime_recovery（运行期热更新状态，不含私有会话）。
        "error": *state.error.lock().unwrap(),
        "role": state.mode.label(), // role（入口职责）：product（正式程序）或 ui-preview（界面演示）。
        "frontend_mode": state.mode.label(),
        "development": state.development,
        "workspace_ready":state.context().is_some(),
        "workspace_busy":state.workspace_busy.load(Ordering::Acquire),
        "initialization_failed":state.initialization.lock().unwrap()["failed"]==true,
        "initialization":*state.initialization.lock().unwrap(),
        "runtime":state.context().and_then(|context|voice_desktop_common::runtime::status(&context).ok()),
        "device_runtime":state.context().map(|context|device_runtime_status(state.inner().as_ref(),&context.data)),
        "fields":{"role":"product（正式程序）或 ui-preview（隔离演示）","frontend_mode":"被授权的界面入口","development":"是否显式源码调试","ready":"受控远程静态入口已加载，或本机产品服务来源已建立","startup_skipped":"已选择跳过加载画面","runtime_recovery":"当前窗口的组件热更新与业务恢复","error":"启动失败原因","workspace_ready":"正式工作区已解析","workspace_busy":"正在切换或迁移","initialization_failed":"初始化失败标识","initialization":"启动阶段与进度","runtime":"本地语音环境状态","device_runtime":"设备配置、期望及实际模式、切换阶段与运行错误"}
    }))
}

/// device_worker_start（按需启动设备进程）：不依赖语音服务，也不隐式启动核心。
#[tauri::command]
async fn device_worker_start(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序可以启动设备工作进程。".into());
    }
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut slot = state
            .device_worker
            .lock()
            .map_err(|error| error.to_string())?;
        if state.stopping.load(Ordering::Acquire) {
            return Err("应用正在退出。".into());
        }
        if slot.is_none() {
            let helper = state
                .context()
                .and_then(|context| packaged_device_helper_path(&context));
            *slot = Some(
                voice_desktop_common::device_worker::ManagedWorker::start_with_helper_path(
                    helper.as_deref(),
                )?,
            );
        }
        if state.stopping.load(Ordering::Acquire) {
            slot.take();
            return Err("应用正在退出。".into());
        }
        let result = slot.as_mut().expect("设备进程已建立").status();
        if result.is_err() {
            slot.take();
        }
        result.map(|_| serde_json::json!({"state":"ready", "fields":{"state":"设备工作进程状态"}}))
    })
    .await
    .map_err(|error| error.to_string())?
}

/// packaged_device_helper_path（已安装桌面辅助脚本）：只接受应用内受管插件包中的普通文件。
fn packaged_device_helper_path(context: &LaunchContext) -> Option<PathBuf> {
    let root = context.installed_program();
    let candidate = root.join(
        "config/dsh/profile/node_modules/@voice-terminal/dsh-plugin/native/win32/device-helper.ps1",
    );
    let relative = candidate.strip_prefix(&root).ok()?;
    let mut current = root.clone();
    for component in relative.components() {
        current.push(component);
        let metadata = fs::symlink_metadata(&current).ok()?;
        if is_reparse_point(&metadata) {
            return None;
        }
    }
    let metadata = fs::symlink_metadata(&candidate).ok()?;
    if !metadata.is_file() {
        return None;
    }
    let canonical_root = root.canonicalize().ok()?;
    let canonical_candidate = candidate.canonicalize().ok()?;
    canonical_candidate
        .starts_with(canonical_root)
        .then_some(canonical_candidate)
}

/// valid_device_rpc_id（桌面设备请求编号）：回执匹配使用的有界编号。
fn valid_device_rpc_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.bytes().enumerate().all(|(index, byte)| {
            byte.is_ascii_alphanumeric() || (index > 0 && matches!(byte, b'.' | b'_' | b':' | b'-'))
        })
}

/// is_reparse_point（重解析点检查）：拒绝符号链接、目录联接及其它 Windows 重解析项。
fn is_reparse_point(metadata: &fs::Metadata) -> bool {
    use std::os::windows::fs::MetadataExt;
    metadata.file_type().is_symlink() || metadata.file_attributes() & 0x400 != 0
}

/// device_worker_desktop_helper_health（桌面辅助进程健康检查）：只调用无副作用 health 操作。
#[tauri::command]
async fn device_worker_desktop_helper_health(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序可以查询桌面辅助进程。".into());
    }
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut slot = state
            .device_worker
            .lock()
            .map_err(|error| error.to_string())?;
        match slot.as_mut() {
            None => {
                Ok(serde_json::json!({"state":"stopped","fields":{"state":"设备工作进程状态"}}))
            }
            Some(worker) => worker.desktop_helper_health(),
        }
    })
    .await
    .map_err(|error| error.to_string())?
}

/// device_worker_status（设备进程状态）：查询不会隐式启动或重建设备。
#[tauri::command]
async fn device_worker_status(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序可以查询设备工作进程。".into());
    }
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut slot = state
            .device_worker
            .lock()
            .map_err(|error| error.to_string())?;
        match slot.as_mut() {
            None => {
                Ok(serde_json::json!({"state":"stopped", "fields":{"state":"设备工作进程状态"}}))
            }
            Some(worker) => {
                let result = worker.status();
                if result.is_err() {
                    slot.take();
                }
                result.map(
                    |_| serde_json::json!({"state":"ready", "fields":{"state":"设备工作进程状态"}}),
                )
            }
        }
    })
    .await
    .map_err(|error| error.to_string())?
}

/// device_worker_audio_devices（默认音频端点状态）：仅读取格式与周期，不启动录放音。
#[tauri::command]
async fn device_worker_audio_devices(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序可以读取设备音频能力。".into());
    }
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut slot = state.device_worker.lock().map_err(|error| error.to_string())?;
        match slot.as_mut() {
            None => Ok(serde_json::json!({"state":"stopped","devices":[],
                "fields":{"state":"设备工作进程状态","devices":"默认音频端点格式列表"}})),
            Some(worker) => worker.audio_devices().map(|value| serde_json::json!({
                "state":"ready","devices":value.get("devices").cloned().unwrap_or(serde_json::Value::Null),
                "fields":value.get("fields").cloned().unwrap_or(serde_json::Value::Null),
            })),
        }
    }).await.map_err(|error| error.to_string())?
}

/// device_worker_capture_start（显式开始麦克风采集）：没有隐式启动工作进程。
#[tauri::command]
async fn device_worker_capture_start(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序可以启动设备采集。".into());
    }
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut slot = state.device_worker.lock().map_err(|error| error.to_string())?;
        let worker = slot.as_mut().ok_or("设备工作进程尚未启动")?;
        let result = worker.capture_start()?;
        Ok(serde_json::json!({"state":"capturing","capturing":true,
            "audio_processing":result.get("audio_processing").cloned().unwrap_or(serde_json::Value::Null),
            "fields":{"state":"采集流状态；capturing 表示麦克风已打开","audio_processing":"AEC、降噪、近端 VAD 健康与参考状态"}}))
    }).await.map_err(|error| error.to_string())?
}

/// device_worker_capture_next（读取一帧）：最多等待指定毫秒，不采集时返回错误。
#[tauri::command]
async fn device_worker_capture_next(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    wait_ms: u32, /* wait_ms（采集帧读取等待上限，毫秒） */
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序可以读取设备采集帧。".into());
    }
    if !(1..=250).contains(&wait_ms) {
        return Err("采集帧等待时间只允许在 1 至 250 毫秒之间。".into());
    }
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut slot = state.device_worker.lock().map_err(|error| error.to_string())?;
        let worker = slot.as_mut().ok_or("设备工作进程尚未启动")?;
        let frame = worker.capture_next(wait_ms)?;
        Ok(serde_json::json!({"frame":frame,"fields":{"frame":"本次读取到的一帧 16 kHz 单声道 PCM 或空值",
            "sequence":"连续采集帧编号","start_sample":"16 kHz 设备流首个采样位置",
            "device_time_us":"设备单调采集时刻，微秒","discontinuity_before":"帧前是否检测到采样缺口",
            "pcm_le_bytes":"小端 16 位单声道 PCM 字节数组"}}))
    }).await.map_err(|error| error.to_string())?
}

/// device_worker_capture_stop（停止麦克风采集）：释放默认输入端点及采集线程。
#[tauri::command]
async fn device_worker_capture_stop(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序可以停止设备采集。".into());
    }
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut slot = state
            .device_worker
            .lock()
            .map_err(|error| error.to_string())?;
        match slot.as_mut() {
            None => Ok(serde_json::json!({"state":"stopped","fields":{"state":"采集流状态"}})),
            Some(worker) => {
                worker.capture_stop()?;
                Ok(serde_json::json!({"state":"stopped","fields":{"state":"采集流状态"}}))
            }
        }
    })
    .await
    .map_err(|error| error.to_string())?
}

/// device_worker_playback_start（显式启动扬声器）：不会隐式启动设备工作进程。
#[tauri::command]
async fn device_worker_playback_start(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序可以启动设备播放。".into());
    }
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut slot = state.device_worker.lock().map_err(|error| error.to_string())?;
        let worker = slot.as_mut().ok_or("设备工作进程尚未启动")?;
        let result = worker.playback_start()?;
        Ok(serde_json::json!({"state":"playing","playing":true,
            "audio_processing":result.get("audio_processing").cloned().unwrap_or(serde_json::Value::Null),
            "fields":{"state":"播放流状态；playing 表示默认输出端点已打开","audio_processing":"AEC 反向参考状态"}}))
    }).await.map_err(|error| error.to_string())?
}

/// device_worker_playback_enqueue（加入 PCM 播放）：只接受明确采样率和有界小端采样。
#[tauri::command]
async fn device_worker_playback_enqueue(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    sample_rate: u32,      /* sample_rate（16/24/32/48 kHz 输入采样率） */
    pcm_le_bytes: Vec<u8>, /* pcm_le_bytes（小端 16 位单声道 PCM 数据） */
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序可以向设备发送播放数据。".into());
    }
    if pcm_le_bytes.is_empty()
        || pcm_le_bytes.len() > 8 * 1024
        || pcm_le_bytes.len() % 2 != 0
        || !matches!(sample_rate, 16_000 | 24_000 | 32_000 | 48_000)
    {
        return Err("PCM 播放采样率或数据长度无效。".into());
    }
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut slot = state
            .device_worker
            .lock()
            .map_err(|error| error.to_string())?;
        let worker = slot.as_mut().ok_or("设备工作进程尚未启动")?;
        worker.playback_enqueue(sample_rate, &pcm_le_bytes)
    })
    .await
    .map_err(|error| error.to_string())?
}

/// device_worker_playback_snapshot（播放状态快照）：读取设备线程缓存的时钟和储备。
#[tauri::command]
async fn device_worker_playback_snapshot(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序可以读取设备播放状态。".into());
    }
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut slot = state
            .device_worker
            .lock()
            .map_err(|error| error.to_string())?;
        match slot.as_mut() {
            None => Ok(
                serde_json::json!({"playing":false,"fields":{"playing":"默认输出端点是否已打开"}}),
            ),
            Some(worker) => worker.playback_snapshot(),
        }
    })
    .await
    .map_err(|error| error.to_string())?
}

/// device_worker_playback_stop（停止默认播放）：释放扬声器端点和工作线程。
#[tauri::command]
async fn device_worker_playback_stop(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序可以停止设备播放。".into());
    }
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut slot = state.device_worker.lock().map_err(|error| error.to_string())?;
        match slot.as_mut() {
            None => Ok(serde_json::json!({"playing":false,"fields":{"playing":"默认输出端点是否已打开"}})),
            Some(worker) => {
                worker.playback_stop()?;
                Ok(serde_json::json!({"playing":false,"fields":{"playing":"默认输出端点是否已打开"}}))
            }
        }
    }).await.map_err(|error| error.to_string())?
}
/// device_config_status（读取设备角色配置）：不返回密钥，只返回主机公开信任字段。
#[tauri::command]
fn device_config_status(
    window: WebviewWindow,
    state: tauri::State<Arc<Desktop>>,
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序可以读取设备角色配置。".into());
    }
    let context = state.context().ok_or("缺少正式工作区")?;
    let snapshot = voice_desktop_common::device_config::inspect(&context.data);
    let config = match voice_desktop_common::device_config::load_from_root(&context.data) {
        voice_desktop_common::device_config::LoadResult::Valid(config) => {
            serde_json::to_value(config).ok()
        }
        _ => None,
    };
    let device_runtime = device_runtime_status(state.inner().as_ref(), &context.data);
    Ok(
        serde_json::json!({"runtime":snapshot,"device_config":config,"device_runtime":device_runtime,
        "local_core_available":context.python.is_file(),
        "fields":{"runtime":"设备配置状态、模式与广播意图","device_config":"已保存的非私密设备与主机公开身份",
            "local_core_available":"本机核心解释器入口是否存在；模型及其他组件仍由正式启动流程验证",
            "device_runtime":"期望模式、实际角色、切换阶段、广播实际状态与错误摘要"}}),
    )
}

/// device_config_save（保存设备角色配置）：有效配置替换必须使用管理页明确提交。
#[tauri::command]
async fn device_config_save(
    window: WebviewWindow,
    app: tauri::AppHandle,
    state: tauri::State<'_, Arc<Desktop>>,
    config: voice_desktop_common::device_config::DeviceConfig, // config（设备角色、音频档及已确认主机身份）。
    preserve_invalid: Option<bool>, // preserve_invalid（明确确认后归档未知或损坏原件再修复）。
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序可以保存设备角色配置。".into());
    }
    if state.stopping.load(Ordering::Acquire) || state.workspace_busy.load(Ordering::Acquire) {
        return Err("应用退出或工作区切换期间不能保存设备配置。".into());
    }
    let operation = {
        let _gate = state
            .maintenance_gate
            .lock()
            .map_err(|_| "维护操作锁不可用。")?;
        if state.workspace_busy.load(Ordering::Acquire)
            || state.app_update.busy.load(Ordering::Acquire)
        {
            return Err("工作区或程序更新正在交接，请等待完成后保存设备设置。".into());
        }
        state.device_operation.begin(
            if config.mode == voice_desktop_common::device_config::RunMode::Local {
                "local"
            } else {
                "remote"
            },
            config.broadcast_enabled,
        )?
    };
    let outcome = save_device_config_inner(
        app,
        state.inner().clone(),
        config,
        preserve_invalid,
        &operation,
        false,
    )
    .await;
    operation.finish(outcome.as_ref().err().cloned());
    outcome
}

/// device_core_restart_required（核心重启条件）：广播与本地共享角色由附属入口热交接。
fn device_core_restart_required(
    previous: Option<&voice_desktop_common::device_config::DeviceConfig>,
    next: &voice_desktop_common::device_config::DeviceConfig,
    force: bool,
) -> bool {
    force
        || previous.map_or(
            next.mode == voice_desktop_common::device_config::RunMode::Remote,
            |old| {
                old.mode != next.mode
                    || old.device_id != next.device_id
                    || next.mode == voice_desktop_common::device_config::RunMode::Remote
                        && old.host != next.host
            },
        )
}

#[cfg(feature = "ssh-device")]
async fn set_local_device_gateway(state: &Arc<Desktop>, enabled: bool) -> Result<(), String> {
    let epoch = state.service_epoch.load(Ordering::Acquire);
    let (id, receive) = state.gateway_control.begin()?;
    let sent = state.send(serde_json::json!({"protocol":1,"kind":"set_device_gateway","request_id":id,"enabled":enabled}));
    let result = if let Err(error) = sent {
        Err(error)
    } else {
        tokio::time::timeout(Duration::from_secs(12), receive)
            .await
            .map_err(|_| "设备入口交接超时，本地语音仍在运行；请刷新广播状态。".to_string())
            .and_then(|value| value.map_err(|_| "设备入口交接通道已关闭。".to_string()))
    };
    state.gateway_control.forget(id);
    let reply = result?;
    if epoch != state.service_epoch.load(Ordering::Acquire) {
        return Err("语音服务代次已变化，已忽略旧网关回执。".into());
    }
    if reply["ok"] != true {
        return Err(reply["error"]
            .as_str()
            .unwrap_or("设备入口交接失败。")
            .into());
    }
    let status = reply["status"].clone();
    *state
        .device_gateway_status
        .lock()
        .map_err(|_| "设备网关状态不可用。")? = status.clone();
    if status["enabled"] != enabled || enabled && status["ready"] != true {
        return Err(status["error"]
            .as_str()
            .filter(|value| !value.is_empty())
            .unwrap_or("设备网关监听尚未就绪。")
            .into());
    }
    Ok(())
}

async fn save_device_config_inner(
    app: tauri::AppHandle,
    state: Arc<Desktop>,
    config: voice_desktop_common::device_config::DeviceConfig,
    preserve_invalid: Option<bool>,
    operation: &device_operation::DeviceOperationGuard<'_>,
    force_restart: bool, // force_restart（启动参数显式要求把已保存模式重新应用到现有实例）。
) -> Result<serde_json::Value, String> {
    config.validate()?; // 非法配置必须在系统授权、停止监听或保存之前拒绝。
    let context = state.context().ok_or("缺少正式工作区")?;
    voice_desktop_common::device_config::require_role_runtime(
        config.desired_role,
        context.python.is_file(),
    )?;
    let old_config = match voice_desktop_common::device_config::load_from_root(&context.data) {
        voice_desktop_common::device_config::LoadResult::Valid(previous) => Some(previous),
        voice_desktop_common::device_config::LoadResult::Missing => None,
        voice_desktop_common::device_config::LoadResult::Invalid(error)
            if preserve_invalid.unwrap_or(false) =>
        {
            let _ = error;
            None
        }
        voice_desktop_common::device_config::LoadResult::Invalid(error) => {
            return Err(format!("已有设备配置无效且原件已保留：{error}"));
        }
    };
    let runtime_change = device_core_restart_required(old_config.as_ref(), &config, force_restart);
    // 必须在触发 UAC（管理员授权）前完成准入，避免授权后才发现切换不可执行。
    if runtime_change {
        if state.service_starting.load(Ordering::Acquire) {
            return Err("正在启动或检查语音服务，请等待完成后切换运行模式。".into());
        }
        if app
            .try_state::<Arc<ResourceManager>>()
            .is_some_and(|manager| manager.has_active_jobs())
        {
            return Err("组件任务尚未结束，请等待任务完成后切换运行模式。".into());
        }
    }
    if config.mode == voice_desktop_common::device_config::RunMode::Local
        && config.broadcast_enabled
    {
        operation.phase(
            "preparing_access",
            "正在配置局域网规则；如出现 Windows 管理员授权，请完成确认",
        );
        // 合并 SSH（安全远程连接）与配对规则：由同一轮状态复核决定是否需要授权。
        let access = tauri::async_runtime::spawn_blocking(|| {
            voice_desktop_common::system_feature::set_device_broadcast_access(true)
        })
        .await
        .map_err(|_| "局域网受限防火墙准备任务无法启动。".to_string())??;
        #[cfg(feature = "ssh-device")]
        {
            *state
                .broadcast_access
                .lock()
                .map_err(|_| "广播检查状态不可用。")? = Some(access);
        }
    }
    if runtime_change {
        if state.service_starting.load(Ordering::Acquire) {
            return Err("正在启动或检查语音服务，请等待完成后切换运行模式。".into());
        }
        if app
            .try_state::<Arc<ResourceManager>>()
            .is_some_and(|manager| manager.has_active_jobs())
        {
            return Err("组件任务尚未结束，请等待任务完成后切换运行模式。".into());
        }
        operation.phase("stopping", "正在停止旧设备角色与连接");
        stop_remote_device_runtime(&state).await?;
        {
            let _transition = state
                .service_transition
                .lock()
                .map_err(|_| "服务交接锁不可用。")?;
            state.stop_service()?;
        }
    }
    let snapshot = match voice_desktop_common::device_config::save_with_recovery(
        &context.data,
        &config,
        preserve_invalid.unwrap_or(false),
    ) {
        Ok(snapshot) => snapshot,
        Err(error) => {
            if runtime_change
                && old_config.as_ref().is_some_and(|previous| {
                    previous.mode == voice_desktop_common::device_config::RunMode::Local
                })
            {
                let _ = start_service(app.clone(), state.clone());
            }
            return Err(error);
        }
    };
    client_event(&state, "client.settings_saved", serde_json::json!({
        "device_id":config.device_id,"mode":if config.mode == voice_desktop_common::device_config::RunMode::Remote {"remote"}else{"local"},
        "client_online_tts_local":config.client_online_tts_local,"audio_compression":config.audio_compression,
        "previous_client_online_tts_local":old_config.as_ref().map(|value| value.client_online_tts_local),
        "previous_audio_compression":old_config.as_ref().map(|value| value.audio_compression),"trigger":"device-config-save"}));
    if runtime_change {
        operation.phase("starting", "正在启动目标运行模式");
        state
            .remote_client_connected
            .store(false, Ordering::Release);
        *state.error.lock().map_err(|_| "运行模式错误状态不可用。")? = None;
        match start_service(app.clone(), state.clone()) {
            Ok(()) => {
                if config.mode == voice_desktop_common::device_config::RunMode::Remote {
                    *state
                        .initialization
                        .lock()
                        .map_err(|_| "启动状态不可用。")? = serde_json::json!({
                        "phase":"远程配对模式已启动，等待搜索并连接服务端",
                        "can_cancel":false,"failed":false,
                        "fields":{"phase":"客户端不启动本机推理核心，等待发现和连接服务端","can_cancel":"当前模式持续可用","failed":"初始化失败标记"}
                    });
                }
            }
            Err(error) => {
                if let Some(previous) = old_config {
                    let restore =
                        voice_desktop_common::device_config::save(&context.data, &previous);
                    client_event(&state, "client.settings_restored", serde_json::json!({
                        "device_id":previous.device_id,"client_online_tts_local":previous.client_online_tts_local,
                        "result":if restore.is_ok(){"restored"}else{"failed"},"reason":"目标模式启动失败"}));
                    if restore.is_ok()
                        && previous.mode == voice_desktop_common::device_config::RunMode::Local
                    {
                        let _ = start_service(app.clone(), state.clone());
                    }
                    *state.error.lock().map_err(|_| "运行模式错误状态不可用。")? =
                        Some(error.clone());
                    return Err(format!(
                        "目标模式启动失败：{error}；{}",
                        if restore.is_ok() {
                            "已恢复先前配置"
                        } else {
                            "先前配置恢复失败，请检查设备状态"
                        }
                    ));
                }
                *state.error.lock().map_err(|_| "运行模式错误状态不可用。")? = Some(error.clone());
                return Err(format!("目标模式启动失败：{error}"));
            }
        }
    }
    #[cfg(feature = "ssh-device")]
    if config.mode == voice_desktop_common::device_config::RunMode::Local
        && config.broadcast_enabled
    {
        if !runtime_change {
            operation.phase("opening_gateway", "正在开启设备网关，本地语音继续运行");
            set_local_device_gateway(&state, true).await?;
        }
        operation.phase("publishing", "正在发布局域网设备公告");
        if let Err(error) = sync_local_discovery(state.clone()).await {
            *state
                .broadcast_error
                .lock()
                .map_err(|_| "广播错误状态不可用。")? = Some(error.clone());
            return Err(format!(
                "本地运行模式已保存，但局域网广播尚未实际发布：{error}"
            ));
        }
    } else if old_config
        .as_ref()
        .is_some_and(|previous| previous.broadcast_enabled)
        || force_restart && !config.broadcast_enabled
    {
        operation.phase("closing_access", "正在关闭设备入口，本地语音继续运行");
        #[cfg(feature = "ssh-device")]
        sync_local_discovery(state.clone()).await?;
        if !runtime_change {
            set_local_device_gateway(&state, false).await?;
        }
        let disable = tauri::async_runtime::spawn_blocking(|| {
            voice_desktop_common::system_feature::set_device_broadcast_access(false)
        })
        .await
        .map_err(|_| "局域网规则关闭任务无法启动。".to_string())?;
        if let Err(error) = disable {
            *state
                .broadcast_error
                .lock()
                .map_err(|_| "广播错误状态不可用。")? = Some(error.clone());
            return Err(format!("目标运行模式已保存，应用已停止接受远程连接，但本轮创建的防火墙规则未能全部关闭：{error}"));
        }
    }
    *state
        .broadcast_error
        .lock()
        .map_err(|_| "广播错误状态不可用。")? = None;
    serde_json::to_value(snapshot).map_err(|_| "设备配置结果编码失败。".into())
}

/// device_discovery_search（搜索局域网服务端）：只读 mDNS 发现，不排除同机不同工作区。
#[tauri::command]
async fn device_discovery_search(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    timeout_ms: Option<u64>, // timeout_ms（搜索等待时长，毫秒；原生层限制不超过十秒）。
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product || state.stopping.load(Ordering::Acquire) {
        return Err("当前应用状态不能搜索局域网设备。".into());
    }
    let context = state.context().ok_or("缺少正式工作区")?;
    match voice_desktop_common::device_config::load_from_root(&context.data) {
        voice_desktop_common::device_config::LoadResult::Valid(config)
            if config.mode == voice_desktop_common::device_config::RunMode::Remote => {}
        voice_desktop_common::device_config::LoadResult::Missing if !context.python.is_file() => {}
        voice_desktop_common::device_config::LoadResult::Invalid(error) => return Err(error),
        _ => return Err("请先切换到“远程配对”模式，再搜索局域网服务端。".into()),
    }
    #[cfg(feature = "ssh-device")]
    {
        let timeout =
            std::time::Duration::from_millis(timeout_ms.unwrap_or(3_000).clamp(1_000, 10_000));
        let desktop = state.inner().clone();
        let devices = tauri::async_runtime::spawn_blocking(move || {
            voice_desktop_common::device_discovery::search(timeout)
        })
        .await
        .map_err(|_| "局域网设备搜索任务无法启动。".to_string())??;
        let mut targets = desktop
            .discovery_targets
            .lock()
            .map_err(|_| "发现设备缓存不可用。")?;
        *targets = devices
            .iter()
            .cloned()
            .map(|device| (device.host_id.clone(), device))
            .collect();
        let expires_at_ms = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|_| "系统时间无效，无法固定发现结果时限。")?
            .as_millis()
            .saturating_add(120_000)
            .min(u128::from(u64::MAX)) as u64;
        desktop
            .discovery_search_expires_at_ms
            .store(expires_at_ms, Ordering::Release);
        let result = serde_json::json!({
            "protocol_version":voice_desktop_common::device_discovery::DISCOVERY_PROTOCOL_VERSION,
            "devices":devices,
            "fields":{"protocol_version":"固定 mDNS 发现协议版本","devices":"搜索到的服务端身份、配对入口、能力及局域网地址记录"}
        });
        Ok(result)
    }
    #[cfg(not(feature = "ssh-device"))]
    {
        let _ = timeout_ms;
        Err("当前程序包未包含局域网设备发现能力。".into())
    }
}

/// device_pairing_window_open（开启本机六位码窗口）：配对码只回传一次，不写入日志或状态。
#[tauri::command]
async fn device_pairing_window_open(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product || state.stopping.load(Ordering::Acquire) {
        return Err("当前应用状态不能开启设备配对窗口。".into());
    }
    #[cfg(feature = "ssh-device")]
    {
        let _discovery = state.discovery_transition.lock().await;
        if state.device_operation.snapshot()["active"] == true {
            return Err("设备设置任务尚未完成，请等待广播就绪后再获取配对码。".into());
        }
        let context = state.context().ok_or("缺少正式工作区")?;
        let config = match voice_desktop_common::device_config::load_from_root(&context.data) {
            voice_desktop_common::device_config::LoadResult::Valid(config) => config,
            voice_desktop_common::device_config::LoadResult::Missing => {
                return Err("请先保存本地运行模式和广播开关。".into())
            }
            voice_desktop_common::device_config::LoadResult::Invalid(error) => return Err(error),
        };
        if config.mode != voice_desktop_common::device_config::RunMode::Local
            || !config.broadcast_enabled
        {
            return Err("配对窗口只在本地运行且已开启广播时可用。".into());
        }
        let status = state
            .broadcast_access
            .lock()
            .map_err(|_| "广播检查状态不可用。")?
            .clone()
            .ok_or("广播没有已验证的服务记录，请重新开启广播。")?;
        if !status.ready || !status.project_access_valid || !status.pairing_access_valid {
            return Err("微软 SSH 服务或本程序受限局域网规则尚未就绪；请先检查 SSH 服务并重新保存广播设置。".into());
        }
        if !state
            .remote_services
            .lock()
            .map_err(|_| "远程原生服务状态不可用。")?
            .as_ref()
            .is_some_and(|services| services.is_running())
        {
            return Err("原生管理和文件服务尚未实际就绪；请关闭后重新开启广播再重试。".into());
        }
        if status.account_is_entra || status.account_sid.as_deref().is_none_or(str::is_empty) {
            return Err("当前 Windows 账户无法用于受限 OpenSSH 登录；没有自动更换账户。".into());
        }
        let username = status
            .account_ssh_username
            .clone()
            .ok_or("无法从当前账户 SID 解析受支持的 OpenSSH 登录名；没有使用邮箱或用户目录名。")?;
        let host_identity = voice_desktop_common::system_feature::open_ssh_server_identity(
            status.effective_port.ok_or("微软 SSH 有效端口尚未验证。")?,
        )
        .await?;
        let mut old_listener = state.pairing_listener.lock().await.take();
        if let Some(listener) = old_listener.take() {
            listener.stop().await;
        }
        *state
            .pairing_window
            .lock()
            .map_err(|_| "配对窗口状态不可用。")? = None;
        let (code_window, opened) =
            voice_desktop_common::device_pairing::PairingCodeWindow::open()?;
        let window_slot = Arc::new(std::sync::Mutex::new(Some(code_window)));
        let addresses = status
            .local_lan_addresses
            .iter()
            .filter_map(|address| address.parse::<std::net::IpAddr>().ok())
            .collect::<Vec<_>>();
        let display_name = format!(
            "Voice Terminal {}",
            config
                .device_id
                .chars()
                .rev()
                .take(6)
                .collect::<String>()
                .chars()
                .rev()
                .collect::<String>()
        );
        let server_info = voice_desktop_common::device_pairing::PairingServerInfo {
            host_id: config.device_id.clone(),
            display_name,
            username,
            ssh_port: status.effective_port.ok_or("微软 SSH 有效端口尚未验证。")?,
            host_public_key: host_identity.public_key,
            host_key_sha256: host_identity.fingerprint_sha256,
            protocol_version: 1,
            fields: [
                ("host_id".into(), "稳定服务端设备编号".into()),
                ("display_name".into(), "服务端显示名称".into()),
                (
                    "username".into(),
                    "由当前 Windows 账户 SID 解析的 OpenSSH 登录名".into(),
                ),
                ("ssh_port".into(), "微软 OpenSSH Server 实际端口".into()),
                (
                    "host_public_key".into(),
                    "微软 OpenSSH Ed25519 主机公钥".into(),
                ),
                ("host_key_sha256".into(), "主机公钥 SHA-256 指纹".into()),
                (
                    "protocol_version".into(),
                    "固定 SSH 设备桥接协议版本".into(),
                ),
            ]
            .into_iter()
            .collect(),
        };
        let data_root = context.data.clone();
        let workspace_id = context.workspace_id.clone();
        let owner_sid = status
            .account_sid
            .clone()
            .ok_or("当前 Windows 账户 SID 不可用。")?;
        let build = voice_desktop_common::resources::load(&context.program.join("release.json"))?
            .get("build")
            .and_then(serde_json::Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or("正式程序缺少有效构建编号。")?
            .to_owned();
        let executable =
            std::env::current_exe().map_err(|error| format!("无法定位正式程序：{error}"))?;
        let scopes = status.local_lan_scopes.clone();
        let pairing_desktop = Arc::downgrade(state.inner());
        let registering_window = window_slot.clone();
        let register: Arc<
            dyn Fn(&voice_desktop_common::device_pairing::PairingIdentity) -> Result<(), String>
                + Send
                + Sync,
        > = Arc::new(move |identity| {
            let desktop = pairing_desktop.upgrade().ok_or("配对宿主已关闭。")?;
            let operation = {
                let _gate = desktop
                    .maintenance_gate
                    .lock()
                    .map_err(|_| "维护操作锁不可用。")?;
                if desktop.stopping.load(Ordering::Acquire)
                    || desktop.workspace_busy.load(Ordering::Acquire)
                    || desktop.app_update.busy.load(Ordering::Acquire)
                {
                    return Err("程序退出或工作区交接期间不能登记设备公钥。".into());
                }
                if !desktop
                    .pairing_window
                    .lock()
                    .map_err(|_| "配对窗口状态不可用。")?
                    .as_ref()
                    .is_some_and(|current| Arc::ptr_eq(current, &registering_window))
                {
                    return Err("原配对窗口已关闭，未登记设备公钥。".into());
                }
                desktop.device_operation.begin("local", true)?
            };
            operation.phase(
                "registering_key",
                "配对身份已验证，正在登记受限公钥；请在服务端完成可能出现的管理员授权",
            );
            let result = (|| {
                let mut trust =
                    voice_desktop_common::device_trust::DeviceTrustStore::open(&data_root)?;
                let snapshot = trust.snapshot();
                if snapshot.state == "invalid" {
                    return Err("服务端设备信任文件无效且已保留；产品授权保持拒绝。".into());
                }
                let old_record = snapshot.devices.iter().find(|record| {
                    record.device_id == identity.device_id && record.state == "trusted"
                });
                if old_record.is_some_and(|record| record.public_key != identity.public_key) {
                    return Err(
                        "该设备编号已经绑定另一把公钥；请先在服务端撤销旧信任再重新配对。".into(),
                    );
                }
                voice_desktop_common::system_feature::install_project_device_key(
                    &identity.public_key,
                    &workspace_id,
                    &identity.device_id,
                    &build,
                    &executable,
                    &owner_sid,
                    &scopes,
                )?;
                if desktop.stopping.load(Ordering::Acquire) {
                    return Err("配对期间程序开始退出，设备尚未加入信任。".into());
                }
                if old_record.is_none() {
                    if let Err(error) = trust.approve(&identity.device_id, &identity.public_key) {
                        // 公钥只允许受限桥接，桥接还会核对独立设备信任；登记失败保持拒绝。
                        // 不在错误清理中再发起 UAC（管理员授权），下次相同身份配对可幂等修复该受管行。
                        return Err(format!("受限公钥已写入，但设备信任保存失败，本次设备未获程序授权；请检查资料目录后重新配对：{error}"));
                    }
                }
                Ok(())
            })();
            operation.finish(result.as_ref().err().cloned());
            result
        });
        let listener = voice_desktop_common::device_pairing::start_pairing_listener(
            &addresses,
            window_slot.clone(),
            server_info,
            register,
        )
        .await?;
        let expires_in_seconds = opened.expires_in_seconds;
        let pairing_code = opened.pairing_code;
        *state
            .pairing_window
            .lock()
            .map_err(|_| "配对窗口状态不可用。")? = Some(window_slot);
        *state.pairing_listener.lock().await = Some(listener);
        Ok(
            serde_json::json!({"pairing_code":pairing_code,"expires_in_seconds":expires_in_seconds,
            "fields":{"pairing_code":"只在本次开启结果返回的六位一次性配对码","expires_in_seconds":"配对窗口有效秒数"}}),
        )
    }
    #[cfg(not(feature = "ssh-device"))]
    {
        Err("当前程序未包含原生六位码配对能力。".into())
    }
}

/// device_pairing_window_close（关闭本机配对窗口）：关闭码、监听器和等待中的尝试。
#[tauri::command]
async fn device_pairing_window_close(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
) -> Result<(), String> {
    trusted(&window, &state)?;
    #[cfg(feature = "ssh-device")]
    {
        let _discovery = state.discovery_transition.lock().await;
        if state.device_operation.snapshot()["phase"] == "registering_key"
            && state.device_operation.snapshot()["active"] == true
        {
            return Err("正在登记设备公钥，请完成或取消当前管理员授权后再关闭配对窗口。".into());
        }
        *state
            .pairing_window
            .lock()
            .map_err(|_| "配对窗口状态不可用。")? = None;
        if let Some(listener) = state.pairing_listener.lock().await.take() {
            listener.stop().await;
        }
    }
    Ok(())
}

/// device_pairing_window_status（读取配对窗口状态）：不返回配对码、密钥或认证尝试材料。
#[tauri::command]
fn device_pairing_window_status(
    window: WebviewWindow,
    state: tauri::State<Arc<Desktop>>,
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    #[cfg(feature = "ssh-device")]
    {
        let slot = state
            .pairing_window
            .lock()
            .map_err(|_| "配对窗口状态不可用。")?
            .clone();
        let (open, expires_in_ms) = if let Some(slot) = slot {
            let window = slot.lock().map_err(|_| "配对窗口状态不可用。")?;
            let open = window
                .as_ref()
                .is_some_and(voice_desktop_common::device_pairing::PairingCodeWindow::is_open);
            let expires = window
                .as_ref()
                .map(voice_desktop_common::device_pairing::PairingCodeWindow::expires_in_ms)
                .unwrap_or(0);
            (open, expires)
        } else {
            (false, 0)
        };
        Ok(
            serde_json::json!({"open":open,"expires_in_ms":expires_in_ms,
            "fields":{"open":"本机六位码窗口是否仍能接受配对","expires_in_ms":"配对窗口剩余有效毫秒数；不包含配对码"}}),
        )
    }
    #[cfg(not(feature = "ssh-device"))]
    {
        Ok(
            serde_json::json!({"open":false,"expires_in_ms":0,"fields":{"open":"当前不支持配对","expires_in_ms":"剩余有效时间"}}),
        )
    }
}

/// device_pairing_connect（用已发现服务端和六位码配对）：不接受手工地址、用户或主机指纹。
#[tauri::command]
async fn device_pairing_connect(
    window: WebviewWindow,
    app: tauri::AppHandle,
    state: tauri::State<'_, Arc<Desktop>>,
    host_id: String,      // host_id（本次原生 mDNS 搜索返回的服务端编号）。
    pairing_code: String, // pairing_code（用户从服务端本机获取的六位一次性码）。
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product || state.stopping.load(Ordering::Acquire) {
        return Err("当前应用状态不能进行设备配对。".into());
    }
    #[cfg(feature = "ssh-device")]
    {
        let operation = {
            let _gate = state
                .maintenance_gate
                .lock()
                .map_err(|_| "维护操作锁不可用。")?;
            if state.workspace_busy.load(Ordering::Acquire)
                || state.app_update.busy.load(Ordering::Acquire)
            {
                return Err("工作区或程序更新正在交接，暂不能配对。".into());
            }
            state.device_operation.begin("remote", false)?
        };
        operation.phase("pairing", "正在验证配对，等待服务端完成受限公钥登记。");
        let outcome = async {
        if pairing_code.len() != 6 || !pairing_code.bytes().all(|byte| byte.is_ascii_digit()) {
            return Err("配对码必须为六位数字。".into());
        }
        let context = state.context().ok_or("缺少正式工作区")?;
        let previous = match voice_desktop_common::device_config::load_from_root(&context.data) {
            voice_desktop_common::device_config::LoadResult::Valid(config)
                if config.mode == voice_desktop_common::device_config::RunMode::Remote =>
            {
                Some(config)
            }
            voice_desktop_common::device_config::LoadResult::Valid(_) => {
                return Err("请先切换到远程配对模式。".into())
            }
            voice_desktop_common::device_config::LoadResult::Missing => {
                return Err("请先保存远程配对模式，再搜索服务端。".into())
            },
            voice_desktop_common::device_config::LoadResult::Invalid(error) => return Err(error),
        };
        if app
            .try_state::<Arc<ResourceManager>>()
            .is_some_and(|manager| manager.has_active_jobs())
        {
            return Err("组件任务正在运行，暂不能切换到远程配对。".into());
        }
        if state.ssh_device.connected().await {
            return Err("当前已经连接到服务端；请先断开当前连接，再配对另一台设备。".into());
        }
        let now_ms = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|_| "系统时间无效，无法检查发现结果有效期。")?
            .as_millis()
            .min(u128::from(u64::MAX)) as u64;
        if now_ms > state.discovery_search_expires_at_ms.load(Ordering::Acquire) {
            return Err("设备搜索结果已过期；请重新搜索后再连接。".into());
        }
        let target = state
            .discovery_targets
            .lock()
            .map_err(|_| "发现设备缓存不可用。")?
            .get(&host_id)
            .cloned()
            .ok_or("所选服务端不属于最近一次原生设备搜索结果；请重新搜索。")?;
        if !target.compatible || target.state == "unavailable" {
            return Err("服务端协议版本不兼容或当前不可用；请更新服务端或重新搜索。".into());
        }
        let device_id = previous
            .as_ref()
            .map(|config| config.device_id.clone())
            .unwrap_or_else(|| format!("remote-{}", voice_desktop_common::workspace::new_id()));
        let identity = match voice_desktop_common::ssh_device::device_identity_status_in(
            Some(&state.context().ok_or("缺少正式工作区")?.data),
            &device_id,
        )? {
            Some(identity) => identity,
            None => voice_desktop_common::ssh_device::create_device_identity_in(
                Some(&state.context().ok_or("缺少正式工作区")?.data),
                &device_id,
            )?,
        };
        let mut addresses = target
            .addresses
            .iter()
            .filter_map(|address| address.parse::<std::net::IpAddr>().ok())
            .filter(|address| {
                voice_desktop_common::device_discovery::is_private_lan_address(*address)
            })
            .collect::<Vec<_>>();
        addresses.sort();
        addresses.dedup();
        addresses.truncate(16);
        if addresses.is_empty() {
            return Err("服务端没有可用的私有局域网地址；没有尝试其他网络地址。".into());
        }
        let device_for_signature = device_id.clone();
        let signing_data_root = context.data.clone();
        let (server, connected_address) =
            voice_desktop_common::device_pairing::connect_pairing_with_address(
                &addresses,
                target.pairing_port,
                &pairing_code,
                &device_id,
                &identity.public_key,
                move |challenge| {
                    voice_desktop_common::ssh_device::sign_device_pairing_challenge_in(
                        Some(&signing_data_root),
                        &device_for_signature,
                        challenge,
                    )
                },
            )
            .await?;
        if server.host_id != target.host_id || server.ssh_port != target.ssh_port {
            return Err("配对回执的服务端编号或 SSH 端口与刚才发现的设备不一致。".into());
        }
        if state.stopping.load(Ordering::Acquire)
            || !matches!(voice_desktop_common::device_config::load_from_root(&context.data),
                voice_desktop_common::device_config::LoadResult::Valid(ref current) if previous.as_ref() == Some(current))
                && previous.is_some()
        {
            return Err("配对期间设备配置已变化或应用正在退出；未覆盖新的设置。".into());
        }
        let mut config = previous.unwrap_or(voice_desktop_common::device_config::DeviceConfig {
            schema_version: voice_desktop_common::device_config::SCHEMA_VERSION,
            device_id: device_id.clone(),
            mode: voice_desktop_common::device_config::RunMode::Remote,
            broadcast_enabled: false,
            client_online_tts_local: true,
            audio_compression: false,
            reconnect_suspended_reason: None,
            desired_role: voice_desktop_common::device_config::Role::RemoteClient,
            audio_mode: voice_desktop_common::device_config::AudioMode::Pcm,
            host: None,
        });
        config.host = Some(voice_desktop_common::device_config::HostTrust {
            host_id: Some(server.host_id.clone()),
            display_name: Some(server.display_name.clone()),
            addresses: addresses.iter().map(ToString::to_string).collect(),
            address: connected_address.to_string(),
            port: server.ssh_port,
            username: server.username,
            host_key_sha256: server.host_key_sha256,
            credential_ref: identity.credential_ref.clone(),
            transport_credential_ref: None,
        });
        config.reconnect_suspended_reason = None;
        voice_desktop_common::device_config::save(&context.data, &config)?;
        operation.phase("connecting", "配对身份已保存，正在连接已验证的服务端。");
        let mut connection_error = None;
        if !state.remote_client_connected.load(Ordering::Acquire) {
            let host = config.host.as_ref().ok_or("配对后缺少已验证服务端身份。")?;
            let ssh_target = voice_desktop_common::ssh_device::SshDeviceTarget {
                address: host.address.clone(),
                port: host.port,
                username: host.username.clone(),
                host_key_sha256: host.host_key_sha256.clone(),
                gateway_port: voice_desktop_common::ssh_device::DEFAULT_GATEWAY_PORT,
            };
            if let Err(error) = state
                .ssh_device
                .connect(
                    &identity.credential_ref,
                    None,
                    &device_id,
                    ssh_target,
                    std::time::Duration::from_secs(15),
                )
                .await
            {
                connection_error = Some(error);
            }
        }
        let connected = connection_error.is_none();
        state
            .remote_client_connected
            .store(connected, Ordering::Release);
        Ok(
            serde_json::json!({"state":"paired","device_id":device_id,"host_id":target.host_id,
            "display_name":target.display_name,"ssh_port":target.ssh_port,"address":connected_address.to_string(),
            "connected":connected,"connection_error":connection_error,
            "fields":{"state":"六位码配对结果","device_id":"本机稳定设备编号","host_id":"已固定服务端设备编号",
                "display_name":"配对服务端名称","ssh_port":"已验证微软 SSH 服务端口","address":"此次选中的局域网地址",
                "connected":"SSH 桥接当前是否已认证","connection_error":"配对完成后 SSH 连接失败的非敏感原因"}}),
        )
        }.await;
        operation.finish(outcome.as_ref().err().cloned());
        outcome
    }
    #[cfg(not(feature = "ssh-device"))]
    {
        let _ = (app, host_id, pairing_code);
        Err("当前产品未包含原生六位码配对能力。".into())
    }
}

/// sync_local_discovery（同步本地广播公告）：只在正式本地广播配置和受限网络规则就绪时发布。
#[cfg(feature = "ssh-device")]
async fn sync_local_discovery(state: Arc<Desktop>) -> Result<(), String> {
    let _discovery = state.discovery_transition.lock().await;
    let operation = state.device_operation.snapshot();
    if state.workspace_busy.load(Ordering::Acquire) {
        return Ok(());
    }
    if operation["active"] == true
        && operation["phase"] != "publishing"
        && operation["phase"] != "closing_access"
    {
        return Ok(());
    }
    if state.mode != FrontendMode::Product || state.stopping.load(Ordering::Acquire) {
        return Err("当前应用状态不能发布局域网服务。".into());
    }
    let context = state.context().ok_or("缺少正式工作区")?;
    let config = match voice_desktop_common::device_config::load_from_root(&context.data) {
        voice_desktop_common::device_config::LoadResult::Valid(config) => config,
        voice_desktop_common::device_config::LoadResult::Missing => {
            if let Some(publication) = state
                .discovery_publication
                .lock()
                .map_err(|_| "局域网公告状态不可用。")?
                .take()
            {
                publication.stop()?;
            }
            stop_local_remote_services(&state).await?;
            return Ok(());
        }
        voice_desktop_common::device_config::LoadResult::Invalid(error) => return Err(error),
    };
    if config.mode != voice_desktop_common::device_config::RunMode::Local
        || !config.broadcast_enabled
    {
        if let Some(publication) = state
            .discovery_publication
            .lock()
            .map_err(|_| "局域网公告状态不可用。")?
            .take()
        {
            publication.stop()?;
        }
        stop_local_remote_services(&state).await?;
        return Ok(());
    }
    let gateway_ready = state
        .device_gateway_status
        .lock()
        .map(|snapshot| snapshot["ready"] == true)
        .unwrap_or(false);
    let state_name = if gateway_ready { "ready" } else { "starting" };
    let remote_services_active = state
        .remote_services
        .lock()
        .map_err(|_| "远程原生服务状态不可用。")?
        .as_ref()
        .is_some_and(|services| services.is_running());
    if remote_services_active
        && state
            .discovery_publication
            .lock()
            .map_err(|_| "局域网公告状态不可用。")?
            .as_ref()
            .is_some_and(|publication| publication.state_name() == state_name)
    {
        return Ok(());
    }
    let epoch = state.service_epoch.load(Ordering::Acquire);
    let cached = state
        .broadcast_access
        .lock()
        .map_err(|_| "广播检查状态不可用。")?
        .clone();
    let status = if let Some(status) = cached {
        status
    } else {
        let status = tauri::async_runtime::spawn_blocking(
            voice_desktop_common::system_feature::open_ssh_server_runtime_status,
        )
        .await
        .map_err(|_| "SSH 服务状态检查任务无法启动。".to_string())??;
        *state
            .broadcast_access
            .lock()
            .map_err(|_| "广播检查状态不可用。")? = Some(status.clone());
        status
    };
    if state.stopping.load(Ordering::Acquire)
        || state.workspace_busy.load(Ordering::Acquire)
        || epoch != state.service_epoch.load(Ordering::Acquire)
        || state.device_operation.snapshot()["revision"] != operation["revision"]
        || voice_desktop_common::device_config::load_from_root(&context.data)
            != voice_desktop_common::device_config::LoadResult::Valid(config.clone())
    {
        return Ok(()); // 丢弃耗时检查期间已过期的角色或公告，不能重新打开旧入口。
    }
    if !status.ready
        || !status.project_access_valid
        || !status.pairing_access_valid
        || !status.discovery_access_valid
    {
        return Err("微软 SSH 服务及本程序精确局域网规则尚未就绪；当前没有发布 mDNS 公告。".into());
    }
    if status.account_is_entra || status.account_ssh_username.is_none() {
        return Err(
            "当前 Windows 账户没有可用的微软 OpenSSH 登录名；本地业务仍可使用，但未发布远程服务。"
                .into(),
        );
    }
    let addresses = status
        .local_lan_addresses
        .iter()
        .filter_map(|address| address.parse::<std::net::IpAddr>().ok())
        .filter(|address| voice_desktop_common::device_discovery::is_private_lan_address(*address))
        .collect::<Vec<_>>();
    if addresses.is_empty() {
        return Err("没有符合当前局域网范围的有效地址；未启动远程服务或发布设备公告。".into());
    }
    let remote_services = state
        .remote_services
        .lock()
        .map_err(|_| "远程原生服务状态不可用。")?
        .clone()
        .ok_or("远程原生服务控制器尚未初始化。")?;
    remote_services.start(&context.data, &config.device_id)?;
    let token = config
        .device_id
        .chars()
        .filter(|ch| ch.is_ascii_alphanumeric())
        .take(32)
        .collect::<String>();
    if token.is_empty() {
        return Err("本机设备编号不能用于局域网服务名称。".into());
    }
    let host_id = config.device_id.clone();
    let display_name = format!(
        "Voice Terminal {}",
        host_id
            .chars()
            .rev()
            .take(6)
            .collect::<String>()
            .chars()
            .rev()
            .collect::<String>()
    );
    let hostname = format!("voice-terminal-{token}.local.");
    let publication = tauri::async_runtime::spawn_blocking(move || {
        voice_desktop_common::device_discovery::publish(
            &host_id,
            &display_name,
            &hostname,
            &addresses,
            voice_desktop_common::device_pairing::DEFAULT_PAIRING_PORT,
            status.effective_port.unwrap_or(22),
            state_name,
            &["remote_management", "device_tasks", "audio"],
        )
    })
    .await
    .map_err(|_| "mDNS 公告准备任务无法启动。".to_string())?;
    let publication = match publication {
        Ok(publication) => publication,
        Err(error) => {
            let _ = remote_services.stop();
            return Err(error);
        }
    };
    if state.stopping.load(Ordering::Acquire)
        || state.workspace_busy.load(Ordering::Acquire)
        || epoch != state.service_epoch.load(Ordering::Acquire)
    {
        drop(publication);
        return Ok(());
    }
    let mut slot = state
        .discovery_publication
        .lock()
        .map_err(|_| "局域网公告状态不可用。")?;
    if let Some(previous) = slot.take() {
        previous.stop()?;
    }
    *slot = Some(publication);
    *state
        .broadcast_error
        .lock()
        .map_err(|_| "广播错误状态不可用。")? = None;
    Ok(())
}

/// stop_local_remote_services（关闭本机项目远程入口）：仅关闭项目监听器和配对窗口，不停止系统 SSH 服务。
#[cfg(feature = "ssh-device")]
async fn stop_local_remote_services(state: &Arc<Desktop>) -> Result<(), String> {
    *state
        .broadcast_access
        .lock()
        .map_err(|_| "广播检查状态不可用。")? = None;
    *state
        .pairing_window
        .lock()
        .map_err(|_| "配对窗口状态不可用。")? = None;
    if let Some(listener) = state.pairing_listener.lock().await.take() {
        listener.stop().await;
    }
    if let Some(services) = state
        .remote_services
        .lock()
        .map_err(|_| "远程原生服务状态不可用。")?
        .as_ref()
        .cloned()
    {
        services.stop()?;
    }
    Ok(())
}

/// stop_remote_device_runtime（停止本机远端角色）：先停止媒体、任务和 SSH，再回收设备工作进程。
async fn stop_remote_device_runtime(state: &Arc<Desktop>) -> Result<(), String> {
    #[cfg(feature = "ssh-device")]
    {
        let _discovery = state.discovery_transition.lock().await;
        state.device_terminal.shutdown();
        state
            .discovery_search_expires_at_ms
            .store(0, Ordering::Release);
        state
            .discovery_targets
            .lock()
            .map_err(|_| "发现设备缓存不可用。")?
            .clear();
        if let Ok(transfers) = state.remote_import_transfers.lock() {
            for cancel in transfers.values() {
                cancel.store(true, Ordering::Release);
            }
        }
        stop_local_remote_services(state).await?;
        if let Some(publication) = state
            .discovery_publication
            .lock()
            .map_err(|_| "局域网公告状态不可用。")?
            .take()
        {
            publication.stop()?;
        }
        stop_remote_voice_forwarder(state).await?;
        stop_remote_media_channel(state).await?;
        stop_remote_device_task_channel(state).await?;
        state.ssh_device.disconnect().await?;
    }
    state
        .remote_client_connected
        .store(false, Ordering::Release);
    let mut worker = state
        .device_worker
        .lock()
        .map_err(|_| "设备工作进程状态不可用。")?;
    if let Some(worker) = worker.as_mut() {
        worker.shutdown()?;
    }
    *worker = None;
    Ok(())
}

/// apply_saved_device_runtime（幂等应用启动参数）：广播和相同模式不重启健康核心，缺失核心才恢复。
async fn apply_saved_device_runtime(
    app: tauri::AppHandle,
    state: Arc<Desktop>,
    arguments: Vec<String>, // arguments（同实例再次启动时明确提交的设备模式参数）。
) -> Result<(), String> {
    if state.mode != FrontendMode::Product || state.stopping.load(Ordering::Acquire) {
        return Err("当前应用状态不能切换设备运行模式。".into());
    }
    let context = state.context().ok_or("缺少正式工作区")?;
    let config = launch_device_config(&context.data, &arguments)?.ok_or("未指定设备运行模式。")?;
    let operation = {
        let _gate = state
            .maintenance_gate
            .lock()
            .map_err(|_| "维护操作锁不可用。")?;
        if state.workspace_busy.load(Ordering::Acquire)
            || state.app_update.busy.load(Ordering::Acquire)
        {
            return Err("工作区或程序更新正在交接，请等待完成后切换设备模式。".into());
        }
        state.device_operation.begin(
            if config.mode == voice_desktop_common::device_config::RunMode::Local {
                "local"
            } else {
                "remote"
            },
            config.broadcast_enabled,
        )?
    };
    let local_core_missing = config.mode == voice_desktop_common::device_config::RunMode::Local
        && state
            .service
            .lock()
            .map_err(|_| "服务状态不可用。")?
            .as_mut()
            .is_none_or(|service| !matches!(service.child.try_wait(), Ok(None)));
    let result = save_device_config_inner(
        app,
        state.clone(),
        config,
        None,
        &operation,
        local_core_missing,
    )
    .await;
    operation.finish(result.as_ref().err().cloned());
    result.map(|_| ())
}

/// device_ssh_probe（读取 SSH 主机指纹候选）：不认证、不执行命令、不自动信任。
#[tauri::command]
async fn device_ssh_probe(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    address: String, // address（主机名称或 IP 地址）。
    port: u16,       // port（SSH 端口）。
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序可以探测 SSH 主机。".into());
    }
    if state.stopping.load(Ordering::Acquire) {
        return Err("应用正在退出。".into());
    }
    #[cfg(feature = "ssh-device")]
    {
        let fingerprint = voice_desktop_common::ssh_device::probe_host_key(
            &address,
            port,
            std::time::Duration::from_secs(10),
        )
        .await?;
        Ok(
            serde_json::json!({"fingerprint":fingerprint,"trusted":false,
            "fields":{"fingerprint":"待用户核对并明确接受的 SSH 主机 SHA-256 指纹","trusted":"该候选尚未写入信任配置"}}),
        )
    }
    #[cfg(not(feature = "ssh-device"))]
    {
        let _ = (address, port);
        Err("当前产品构建未包含 SSH 设备连接能力。".into())
    }
}

/// device_ssh_identity_create（生成本机 SSH 身份）：仅返回公钥，不返回私钥。
#[tauri::command]
async fn device_ssh_identity_create(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    device_id: String, // device_id（本机稳定设备编号）。
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序可以管理 SSH 设备身份。".into());
    }
    #[cfg(feature = "ssh-device")]
    {
        if state.ssh_device.connected().await {
            return Err("请先断开 SSH 主机连接，再轮换设备身份。".into());
        }
        let identity = voice_desktop_common::ssh_device::create_device_identity_in(
            Some(&state.context().ok_or("缺少正式工作区")?.data),
            &device_id,
        )?;
        serde_json::to_value(identity).map_err(|_| "设备 SSH 公钥结果编码失败。".into())
    }
    #[cfg(not(feature = "ssh-device"))]
    {
        let _ = device_id;
        Err("当前产品构建未包含 SSH 设备连接能力。".into())
    }
}

/// device_ssh_identity_status（读取本机 SSH 公钥）：不返回私钥，不修改凭据库。
#[tauri::command]
fn device_ssh_identity_status(
    window: WebviewWindow,
    state: tauri::State<Arc<Desktop>>,
    device_id: String, // device_id（本机稳定设备编号）。
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序可以读取 SSH 设备身份。".into());
    }
    #[cfg(feature = "ssh-device")]
    {
        let identity = voice_desktop_common::ssh_device::device_identity_status_in(
            Some(&state.context().ok_or("缺少正式工作区")?.data),
            &device_id,
        )?;
        serde_json::to_value(identity).map_err(|_| "设备 SSH 身份状态编码失败。".into())
    }
    #[cfg(not(feature = "ssh-device"))]
    {
        let _ = device_id;
        Ok(serde_json::Value::Null)
    }
}

/// device_ssh_identity_delete（删除本机 SSH 身份）：只清除该设备的本机私钥。
#[tauri::command]
async fn device_ssh_identity_delete(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    device_id: String, // device_id（本机稳定设备编号）。
) -> Result<(), String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序可以管理 SSH 设备身份。".into());
    }
    #[cfg(feature = "ssh-device")]
    {
        if state.ssh_device.connected().await {
            return Err("请先断开 SSH 主机连接，再删除设备身份。".into());
        }
        voice_desktop_common::ssh_device::delete_device_identity_in(
            Some(&state.context().ok_or("缺少正式工作区")?.data),
            &device_id,
        )
    }
    #[cfg(not(feature = "ssh-device"))]
    {
        let _ = device_id;
        Err("当前产品构建未包含 SSH 设备连接能力。".into())
    }
}

/// device_ssh_transport_identity_import（导入 SSH 登录身份）：原生层读取本地密钥文件，界面只收到公钥。
#[tauri::command]
async fn device_ssh_transport_identity_import(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    path: String, // path（用户指定的本地 OpenSSH 私钥文件路径；不含私钥内容）。
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序管理页可以导入 SSH 登录密钥。".into());
    }
    if state.stopping.load(Ordering::Acquire) {
        return Err("应用正在退出。".into());
    }
    #[cfg(feature = "ssh-device")]
    {
        if state.ssh_device.connected().await {
            return Err("导入 SSH 登录密钥前，请先断开主机连接。".into());
        }
        let identity = voice_desktop_common::ssh_device::import_transport_identity_in(
            Some(&state.context().ok_or("缺少正式工作区")?.data),
            Path::new(&path),
        )?;
        serde_json::to_value(identity).map_err(|_| "SSH 登录公钥结果编码失败。".into())
    }
    #[cfg(not(feature = "ssh-device"))]
    {
        let _ = path;
        Err("当前产品构建未包含 SSH 设备连接能力。".into())
    }
}

/// device_ssh_transport_identity_status（读取 SSH 登录身份）：只返回公开公钥资料。
#[tauri::command]
fn device_ssh_transport_identity_status(
    window: WebviewWindow,
    state: tauri::State<Arc<Desktop>>,
    credential_ref: String, // credential_ref（设备配置中保存的 SSH 登录凭据引用）。
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序管理页可以读取 SSH 登录身份。".into());
    }
    #[cfg(feature = "ssh-device")]
    {
        let identity = voice_desktop_common::ssh_device::transport_identity_status_in(
            Some(&state.context().ok_or("缺少正式工作区")?.data),
            &credential_ref,
        )?;
        serde_json::to_value(identity).map_err(|_| "SSH 登录身份状态编码失败。".into())
    }
    #[cfg(not(feature = "ssh-device"))]
    {
        let _ = credential_ref;
        Ok(serde_json::Value::Null)
    }
}

/// device_ssh_transport_identity_delete（删除 SSH 登录身份）：只移除本机系统凭据。
#[tauri::command]
async fn device_ssh_transport_identity_delete(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    credential_ref: String, // credential_ref（仅删除该条 SSH 登录密钥凭据）。
) -> Result<(), String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序管理页可以删除 SSH 登录身份。".into());
    }
    #[cfg(feature = "ssh-device")]
    {
        if state.ssh_device.connected().await {
            return Err("删除 SSH 登录密钥前，请先断开主机连接。".into());
        }
        voice_desktop_common::ssh_device::delete_transport_identity_in(
            Some(&state.context().ok_or("缺少正式工作区")?.data),
            &credential_ref,
        )
    }
    #[cfg(not(feature = "ssh-device"))]
    {
        let _ = credential_ref;
        Err("当前产品构建未包含 SSH 设备连接能力。".into())
    }
}

/// device_ssh_connect（连接 SSH 主机）：固定设备身份及 SHA-256 主机指纹。
#[tauri::command]
async fn device_ssh_connect(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    timeout_ms: Option<u64>, // timeout_ms（连接超时，毫秒）。
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序可以连接 SSH 主机。".into());
    }
    if state.stopping.load(Ordering::Acquire) {
        return Err("应用正在退出。".into());
    }
    #[cfg(feature = "ssh-device")]
    {
        let operation = {
            let _gate = state
                .maintenance_gate
                .lock()
                .map_err(|_| "维护操作锁不可用。")?;
            if state.workspace_busy.load(Ordering::Acquire)
                || state.app_update.busy.load(Ordering::Acquire)
            {
                return Err("工作区或程序更新正在交接，暂不能连接服务端。".into());
            }
            state.device_operation.begin("remote", false)?
        };
        operation.phase("connecting", "正在验证已配对服务端并建立安全连接。");
        client_event(&state, "client.connect_attempt", serde_json::json!({"state":"connecting"}));
        let debug_call = agent_debug::window_operation(&window);
        let outcome = async {
        let context = state.context().ok_or("缺少正式工作区")?;
        let config = match voice_desktop_common::device_config::load_from_root(&context.data) {
            voice_desktop_common::device_config::LoadResult::Valid(config) => config,
            voice_desktop_common::device_config::LoadResult::Missing => {
                return Err("尚未保存设备角色配置。".into());
            }
            voice_desktop_common::device_config::LoadResult::Invalid(error) => return Err(error),
        };
        let local_service_running = match state.service.lock() {
            Ok(mut service) => service
                .as_mut()
                .is_some_and(|value| matches!(value.child.try_wait(), Ok(None))),
            Err(_) => true,
        };
        if let Some(reason) =
            voice_desktop_common::device_config::remote_client_connect_block_reason(
                Some(config.desired_role),
                local_service_running,
            )
        {
            return Err(reason.into());
        }
        if config.reconnect_suspended_reason.is_some() {
            return Err("自动重连已暂停；请先通过设备设置恢复连接。".into());
        }
        let device_id = config.device_id.clone();
        let host = config
            .host
            .clone()
            .ok_or("远端客户端配置缺少已确认主机身份")?;
        let identity_credential_ref = host.credential_ref.clone();
        let transport_credential_ref = host.transport_credential_ref.clone();
        let target = voice_desktop_common::ssh_device::SshDeviceTarget {
            address: host.address.clone(),
            port: host.port,
            username: host.username,
            host_key_sha256: host.host_key_sha256.clone(),
            gateway_port: voice_desktop_common::ssh_device::DEFAULT_GATEWAY_PORT,
        };
        state
            .remote_client_connected
            .store(false, Ordering::Release);
        let mut timeout = timeout_ms.unwrap_or(10_000);
        if let Some((debug, call)) = &debug_call {
            if let Some(value) = debug.point("device.connection", "input", *call, &serde_json::json!({"timeout_ms":timeout})).await {
                if let Some(replacement) = value["timeout_ms"].as_u64().filter(|value| (1..=60_000).contains(value)) {
                    timeout = replacement;
                } else {
                    debug.observe("device.connection", "rejected", *call, &serde_json::json!({"reason":"连接等待时间无效。"}));
                }
            }
        }
        state
            .ssh_device
            .connect(
                &identity_credential_ref,
                transport_credential_ref.as_deref(),
                &device_id,
                target,
                std::time::Duration::from_millis(timeout),
            )
            .await?;
        if !matches!(voice_desktop_common::device_config::load_from_root(&context.data),
            voice_desktop_common::device_config::LoadResult::Valid(ref current) if current == &config)
        {
            let _ = state.ssh_device.disconnect().await;
            return Err("远端客户端配置在 SSH 认证期间发生变化；连接已关闭。".into());
        }
        state.remote_client_connected.store(true, Ordering::Release);
        Ok(
            serde_json::json!({"state":"ready","device_id":device_id,"address":host.address,"port":host.port,
            "host_key_sha256":host.host_key_sha256,"gateway_port":voice_desktop_common::ssh_device::DEFAULT_GATEWAY_PORT,
            "transport_credential_ref":transport_credential_ref,
            "fields":{"state":"原生 SSH 设备连接状态","device_id":"本机设备编号","address":"已连接主机名称或 IP 地址",
                "port":"SSH 端口","host_key_sha256":"已确认的主机公钥指纹","gateway_port":"主机回环业务网关端口",
                "transport_credential_ref":"单独用于 OpenSSH 账户登录的凭据引用；为空时沿用产品设备身份"}}),
        )
        }.await;
        if let Some((debug, call)) = debug_call {
            match &outcome {
                Ok(result) => debug.observe("device.connection", "result", call, result),
                Err(error) => debug.observe("device.connection", "error", call, &serde_json::json!({"reason":error})),
            }
        }
        client_event(&state, if outcome.is_ok() { "client.connected" } else { "client.connect_failed" },
            serde_json::json!({"reason":outcome.as_ref().err(),"state":if outcome.is_ok(){"connected"}else{"error"}}));
        operation.finish(outcome.as_ref().err().cloned());
        outcome
    }
    #[cfg(not(feature = "ssh-device"))]
    {
        let _ = timeout_ms;
        Err("当前产品构建未包含 SSH 设备连接能力。".into())
    }
}

/// device_ssh_pairing_request（提交设备配对申请）：本机私钥在原生层签署，不进入界面或请求日志。
#[tauri::command]
async fn device_ssh_pairing_request(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    pairing_code: String,    // pairing_code（主机本机显示的一次性配对码）。
    timeout_ms: Option<u64>, // timeout_ms（配对回执时限，毫秒）。
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序可以提交设备配对申请。".into());
    }
    if state.stopping.load(Ordering::Acquire) {
        return Err("应用正在退出。".into());
    }
    #[cfg(feature = "ssh-device")]
    {
        let context = state.context().ok_or("缺少正式工作区")?;
        let config = match voice_desktop_common::device_config::load_from_root(&context.data) {
            voice_desktop_common::device_config::LoadResult::Valid(config) => config,
            voice_desktop_common::device_config::LoadResult::Missing => {
                return Err("尚未保存设备角色配置。".into());
            }
            voice_desktop_common::device_config::LoadResult::Invalid(error) => return Err(error),
        };
        if config.desired_role != voice_desktop_common::device_config::Role::RemoteClient {
            return Err("只有远端客户端角色可以提交设备配对申请。".into());
        }
        let _host = config.host.ok_or("远端客户端配置缺少已确认主机身份")?;
        let response = state
            .ssh_device
            .submit_pairing_request(
                &config.device_id,
                &pairing_code,
                std::time::Duration::from_millis(timeout_ms.unwrap_or(20_000)),
            )
            .await?;
        Ok(
            serde_json::json!({"status":response.status,"body":response.body,
            "fields":{"status":"SSH 配对申请 HTTP 状态码","body":"主机验证后返回的申请状态与编号"}}),
        )
    }
    #[cfg(not(feature = "ssh-device"))]
    {
        let _ = (pairing_code, timeout_ms);
        Err("当前产品构建未包含 SSH 设备配对能力。".into())
    }
}

/// device_ssh_request（提交 SSH 业务请求）：请求由主机网关再次验证权限。
#[tauri::command]
async fn device_ssh_request(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    path: String,            // path（受限版本化网关路径）。
    body: serde_json::Value, // body（有界 JSON 业务请求）。
    timeout_ms: Option<u64>, // timeout_ms（回执等待时限，毫秒）。
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序可以提交 SSH 业务请求。".into());
    }
    if state.stopping.load(Ordering::Acquire) {
        return Err("应用正在退出。".into());
    }
    #[cfg(feature = "ssh-device")]
    {
        let reply = state
            .ssh_device
            .post_json(
                &path,
                &body,
                std::time::Duration::from_millis(timeout_ms.unwrap_or(30_000)),
            )
            .await?;
        serde_json::to_value(reply).map_err(|_| "SSH 业务回执编码失败。".into())
    }
    #[cfg(not(feature = "ssh-device"))]
    {
        let _ = (path, body, timeout_ms);
        Err("当前产品构建未包含 SSH 设备连接能力。".into())
    }
}

/// device_ssh_management_request（提交远程业务操作）：固定目标、操作枚举和管理会话代次。
#[cfg(feature = "ssh-device")]
/// remote_business_operation_allowed（检查远程业务操作是否位于固定白名单中）。
const REMOTE_BUSINESS_OPERATIONS: &[&str] = &[
        "management.status",
        "resource.inventory",
        "resource.jobs",
        "resource.job.read",
        "resource.job.cancel",
        "resource.job.start",
        "resource.import.source.reserve",
        "resource.import.source.commit",
        "resource.import.source.abort",
        "resource.import.confirm",
        "resource.install.plan",
        "resource.install.start",
        "resource.install.required",
        "resource.reset.plan",
        "resource.reset",
        "system.ssh.status",
        "system.ssh.enable",
        "product.devices.select",
        "settings.screen_capture.save",
        "product.snapshot",
        "product.devices",
        "product.compute_status",
        "product.compute_status.refresh",
        "product.dsh_status",
        "dsh.approval.decide",
        "product.wake_tasks",
        "wake_task.create",
        "wake_task.update",
        "wake_task.delete",
        "wake_task.cancel",
        "settings.hotwords.read",
        "settings.hotwords.save",
        "settings.wake.save",
        "settings.silence.save",
        "settings.quiet_hours.save",
        "settings.shadow.save",
        "settings.approvals.save",
        "settings.persona.save",
        "settings.custom_instructions.save",
        "settings.speaker.save",
        "settings.compute.save",
        "settings.audio_enhancements.save",
        "settings.tts.save",
        "settings.inference.save",
        "models.registry.read",
        "models.scan",
        "models.validate",
        "models.providers.add",
        "models.assignments.apply",
        "models.credentials.read",
        "models.credentials.save",
        "provider.usage",
        "model.update",
        "model.delete",
        "model.hide",
        "model.restore",
        "hotwords.import",
        "hotwords.preview",
        "tts.minimax.voice_name",
        "tts.operation.preview",
        "tts.operation.minimax_preview",
        "tts.operation.play",
        "tts.operation.transcribe",
        "tts.operation.read",
        "tts.operation.cancel",
        "tts.voices.read",
        "tts.voices.create",
        "tts.voices.update",
        "tts.voices.delete",
        "tts.voices.select",
        "tts.voices.restore_default",
        "tts.voices.default_reference",
        "tts.assets.clip",
        "tts.aniflive.references.import",
        "tts.voices.capabilities",
        "tts.aniflive.model.rename",
        "tts.aniflive.import.pending",
        "tts.aniflive.import.scan",
        "tts.aniflive.import.start",
        "tts.aniflive.import.read",
        "tts.aniflive.import.reference",
        "tts.status",
        "speaker.status",
        "speaker.profile",
        "speaker.enrollment.start",
        "speaker.enrollment.segment",
        "speaker.enrollment.capture",
        "speaker.enrollment.capture_stop",
        "speaker.enrollment.commit",
        "speaker.enrollment.delete",
        "speaker.test.capture",
        "speaker.test.capture_stop",
        "speaker.test.run",
        "speaker.template.delete",
        "speaker.template.update",
    ];
#[cfg(feature = "ssh-device")]
fn remote_business_operation_allowed(operation: &str) -> bool {
    REMOTE_BUSINESS_OPERATIONS.contains(&operation)
}

#[tauri::command(rename_all = "snake_case")]
async fn device_ssh_management_request(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    operation: String,            // operation（服务端固定业务操作编号）。
    arguments: serde_json::Value, // arguments（与操作编号对应的 JSON 参数）。
    request_id: Option<String>, // request_id（可重试业务沿用的稳定请求编号；缺省时由原生层生成）。
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product || state.stopping.load(Ordering::Acquire) {
        return Err("当前窗口或应用状态不允许远程业务操作。".into());
    }
    #[cfg(feature = "ssh-device")]
    {
        if !remote_business_operation_allowed(&operation) {
            return Err("远程业务操作不在客户端原生白名单中。".into());
        }
        if !arguments.is_object()
            || serde_json::to_vec(&arguments)
                .map_err(|_| "远程业务参数无法编码。")?
                .len()
                > 512 * 1024
        {
            return Err("远程业务参数必须是小于 512 KiB 的 JSON 对象。".into());
        }
        let context = state.context().ok_or("缺少正式工作区")?;
        let config = match voice_desktop_common::device_config::load_from_root(&context.data) {
            voice_desktop_common::device_config::LoadResult::Valid(config)
                if config.mode == voice_desktop_common::device_config::RunMode::Remote =>
            {
                config
            }
            voice_desktop_common::device_config::LoadResult::Valid(_) => {
                return Err("只有远程配对模式可以提交服务端业务操作。".into());
            }
            voice_desktop_common::device_config::LoadResult::Missing => {
                return Err("尚未保存远程配对设备配置。".into());
            }
            voice_desktop_common::device_config::LoadResult::Invalid(error) => return Err(error),
        };
        let expected_config = config.clone();
        let host = config.host.ok_or("远程配置缺少配对服务端身份。")?;
        let host_id = host.host_id.ok_or("配对服务端身份缺少稳定设备编号。")?;
        if !state.ssh_device.connected().await {
            return Err("SSH 服务端连接已断开，请重新连接后重试。".into());
        }
        let request_id = match request_id {
            Some(value)
                if voice_desktop_common::device_management_contract::valid_request_id(&value) =>
            {
                value
            }
            Some(_) => return Err("远程管理请求编号必须是 32 位小写十六进制值。".into()),
            None => rand::random::<[u8; 16]>()
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect::<String>(),
        };
        // 管理状态与资源任务走独立原生服务；业务设置仍调用核心现有处理器。
        if operation == "management.status"
            || operation.starts_with("resource.")
            || operation.starts_with("system.ssh.")
        {
            let response = state
                .ssh_device
                .management_request(
                    &request_id,
                    &host_id,
                    &operation,
                    arguments,
                    Duration::from_secs(60),
                )
                .await?;
            require_remote_request_context(state.inner(), &context.data, &expected_config)?;
            return Ok(serde_json::json!({
                "request_id": response.request_id,
                "target_host_id": response.target_host_id,
                "session_generation": response.session_generation,
                "session_kind": "management",
                "operation": response.operation,
                "status": response.status,
                "body": response.body,
                "fields": {
                    "request_id": "服务端确认的原操作编号",
                    "target_host_id": "本次操作所属的配对服务端",
                    "session_generation": "独立管理服务授予的会话代次，不依赖语音核心或任务通道",
                    "session_kind": "本次回执所属会话类型；management 表示原生管理通道",
                    "operation": "本次服务端业务操作编号",
                    "status": "服务端固定操作回执的原状态码",
                    "body": "服务端原生管理服务返回的结果或明确错误",
                }
            }));
        }
        let (connection_generation, executor_generation) = {
            let channel = state
                .remote_task_channel
                .lock()
                .map_err(|_| "远程管理会话状态不可用。")?;
            let channel = channel
                .as_ref()
                .filter(|channel| channel.running.load(Ordering::Acquire))
                .ok_or("服务端业务操作需要就绪的任务通道。")?;
            (channel.connection_generation, channel.executor_generation)
        };
        let request = serde_json::json!({
            "protocol_version": 1,
            "request_id": request_id,
            "target_host_id": host_id,
            "session_generation": connection_generation,
            "operation": operation,
            "arguments": arguments,
            "fields": {
                "protocol_version": "兼容业务代理协议版本",
                "request_id": "本次远程业务操作的唯一编号",
                "target_host_id": "目标服务端稳定设备编号",
                "session_generation": "本次核心任务连接代次；只用于依赖核心的业务代理",
                "operation": "固定服务端业务操作编号",
                "arguments": "与固定操作对应的类型化参数",
            }
        });
        let gateway = state
            .ssh_device
            .post_json("/v1/management", &request, Duration::from_secs(60))
            .await?;
        require_remote_request_context(state.inner(), &context.data, &expected_config)?;
        if gateway.status != 200 {
            return Ok(serde_json::json!({
                "request_id":request_id,"target_host_id":host_id,"session_kind":"task",
                "session_generation":connection_generation,"connection_generation":connection_generation,
                "executor_generation":executor_generation,"operation":operation,
                "status": gateway.status,
                "body": gateway.body,
                "fields": {"status": "核心业务管理代理状态码", "body": "服务端核心网关返回的明确错误"},
            }));
        }
        let body = gateway.body;
        if body["protocol_version"] != 1
            || body["request_id"] != request_id
            || body["target_host_id"] != host_id
            || body["session_generation"] != connection_generation
            || body["operation"] != operation
            || body["status"]
                .as_u64()
                .is_none_or(|status| !(100..=599).contains(&status))
            || !body["fields"].is_object()
        {
            return Err("服务端核心业务回执与目标、操作或任务代次不匹配。".into());
        }
        Ok(serde_json::json!({
            "request_id": request_id,
            "target_host_id": host_id,
            "session_generation": connection_generation,
            "session_kind": "task",
            "connection_generation": connection_generation,
            "executor_generation": executor_generation,
            "operation": operation,
            "status": body["status"],
            "body": body["body"],
            "fields": {
                "request_id": "服务端确认的原操作编号",
                "target_host_id": "本次操作所属的配对服务端",
                "session_generation": "本次核心业务操作绑定的任务通道连接代次",
                "session_kind": "本次回执所属会话类型；task 表示核心任务通道",
                "connection_generation": "核心任务通道连接代次，与管理会话代次分开",
                "executor_generation": "客户端执行器代次，不作为服务端业务目标",
                "operation": "本次服务端业务操作编号",
                "status": "服务端现有业务处理器的 HTTP 状态码",
                "body": "服务端现有业务处理器返回的正文或校验错误",
            }
        }))
    }
    #[cfg(not(feature = "ssh-device"))]
    {
        let _ = (operation, arguments, request_id);
        Err("当前产品构建未包含 SSH 远程业务管理能力。".into())
    }
}

/// require_remote_request_context（远端回执所属工作区与配置）：交接后的旧回执不进入新页面。
#[cfg(feature = "ssh-device")]
fn require_remote_request_context(
    state: &Arc<Desktop>,
    data: &std::path::Path,
    config: &voice_desktop_common::device_config::DeviceConfig,
) -> Result<(), String> {
    if state.stopping.load(Ordering::Acquire)
        || !state.context().is_some_and(|current| current.data == data)
        || !matches!(voice_desktop_common::device_config::load_from_root(data),
            voice_desktop_common::device_config::LoadResult::Valid(ref current) if current == config)
    {
        return Err("远程请求期间工作区或设备配置已切换，已丢弃旧回执。".into());
    }
    Ok(())
}

#[cfg(feature = "ssh-device")]
async fn remote_core_management_request(
    state: &Arc<Desktop>,
    target_host_id: &str,
    request_id: &str,
    connection_generation: u64,
    operation: &str,
    arguments: serde_json::Value,
) -> Result<serde_json::Value, String> {
    if !voice_desktop_common::device_management_contract::valid_request_id(request_id)
        || !voice_desktop_common::device_management_contract::valid_host_id(target_host_id)
        || !matches!(
            operation,
            "tts.assets.import.prepare"
                | "tts.assets.import.commit"
                | "tts.assets.import.abort"
        )
    {
        return Err("远程 TTS 文件操作编号、服务端目标或固定操作无效。".into());
    }
    {
        let channel = state
            .remote_task_channel
            .lock()
            .map_err(|_| "远程任务通道状态不可用。")?;
        let channel = channel
            .as_ref()
            .filter(|channel| channel.running.load(Ordering::Acquire))
            .ok_or("远程 TTS 资产操作需要就绪的服务端任务通道。")?;
        if channel.connection_generation != connection_generation {
            return Err("远程 TTS 资产请求连接代次已变化，请重新连接服务端。".into());
        }
    }
    let request = serde_json::json!({
        "protocol_version": 1,
        "request_id": request_id,
        "target_host_id": target_host_id,
        "session_generation": connection_generation,
        "operation": operation,
        "arguments": arguments,
        "fields": {
            "protocol_version": "设备网关协议版本",
            "request_id": "本次远程 TTS 文件业务操作编号",
            "target_host_id": "固定配对服务端设备编号",
            "session_generation": "当前任务通道连接代次",
            "operation": "服务端固定 TTS 文件操作枚举",
            "arguments": "与固定操作对应的类型化参数",
        },
    });
    let response = state
        .ssh_device
        .post_json("/v1/management", &request, Duration::from_secs(60))
        .await?;
    if response.status != 200 {
        return Err(format!(
            "服务端 TTS 资产管理网关返回 HTTP {}。",
            response.status
        ));
    }
    let envelope = response.body;
    let status = envelope
        .get("status")
        .and_then(serde_json::Value::as_u64)
        .and_then(|value| u16::try_from(value).ok())
        .ok_or("服务端 TTS 资产操作缺少有效状态码。")?;
    if envelope
        .get("request_id")
        .and_then(serde_json::Value::as_str)
        != Some(request_id)
        || envelope
            .get("target_host_id")
            .and_then(serde_json::Value::as_str)
            != Some(target_host_id)
        || envelope
            .get("operation")
            .and_then(serde_json::Value::as_str)
            != Some(operation)
        || envelope
            .get("session_generation")
            .and_then(serde_json::Value::as_u64)
            != Some(connection_generation)
    {
        return Err("服务端 TTS 资产回执与原始目标、操作或连接代次不匹配。".into());
    }
    let body = envelope
        .get("body")
        .cloned()
        .unwrap_or(serde_json::Value::Null);
    if !(200..300).contains(&status) {
        return Err(body
            .get("detail")
            .and_then(serde_json::Value::as_str)
            .or_else(|| body.get("message").and_then(serde_json::Value::as_str))
            .or_else(|| body.get("error").and_then(serde_json::Value::as_str))
            .map(str::to_owned)
            .unwrap_or_else(|| format!("服务端 TTS 资产操作失败（HTTP {status}）。")));
    }
    Ok(body)
}

/// validate_tts_asset_upload_receipt（校验 TTS 上传回执）：校验原传输编号、长度与小写摘要。
#[cfg(feature = "ssh-device")]
fn validate_tts_asset_upload_receipt(
    receipt: voice_desktop_common::ssh_device::SftpTransferReceipt,
    transfer_id: &str,
    expected_bytes: u64,
) -> Result<voice_desktop_common::ssh_device::SftpTransferReceipt, String> {
    let valid_sha256 = receipt.sha256.len() == 64
        && receipt
            .sha256
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte));
    if receipt.path == format!("transfer:{transfer_id}")
        && receipt.bytes == expected_bytes
        && valid_sha256
    {
        Ok(receipt)
    } else {
        Err("服务端 TTS 文件完整性回执与原文件票据不匹配。".into())
    }
}

/// recover_tts_asset_upload_receipt（恢复上传回执）：只接受原票据重新认证返回的完整摘要。
#[cfg(feature = "ssh-device")]
async fn recover_tts_asset_upload_receipt<F, Fut>(
    transfer_id: &str,
    expected_bytes: u64,
    first_error: &str,
    retry_upload: F,
) -> Result<voice_desktop_common::ssh_device::SftpTransferReceipt, String>
where
    F: FnOnce() -> Fut,
    Fut: std::future::Future<
        Output = Result<voice_desktop_common::ssh_device::SftpTransferReceipt, String>,
    >,
{
    let receipt = retry_upload().await.map_err(|_| {
        format!(
            "TTS 文件上传回执不明确，无法从原传输票据取得已核验摘要；原票据已保留，可重试恢复。首次上传错误：{first_error}"
        )
    })?;
    validate_tts_asset_upload_receipt(receipt, transfer_id, expected_bytes).map_err(|_| {
        "TTS 文件上传回执不明确，返回内容未能绑定原传输票据；原票据已保留，可重试恢复。".into()
    })
}

/// recover_tts_asset_import_commit（恢复 TTS 提交回执）：以同一 request_id（请求编号）重放一次幂等提交。
#[cfg(feature = "ssh-device")]
async fn recover_tts_asset_import_commit<F, Fut>(
    first_result: Result<serde_json::Value, String>,
    retry_commit: F,
) -> Result<serde_json::Value, String>
where
    F: FnOnce() -> Fut,
    Fut: std::future::Future<Output = Result<serde_json::Value, String>>,
{
    match first_result {
        Ok(result) => Ok(result),
        Err(first_error) => retry_commit().await.map_err(|retry_error| {
            format!(
                "TTS 音频提交未获得成功回执；程序已使用原传输票据和请求编号重试一次。请先刷新服务端音色列表确认结果，再决定是否重新导入。首次结果：{first_error}；重试结果：{retry_error}"
            )
        }),
    }
}

/// device_ssh_tts_asset_import（客户端原生选择并导入 TTS 参考音频）：绝对路径只留在本机原生进程。
#[tauri::command(rename_all = "snake_case")]
async fn device_ssh_tts_asset_import(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    model_id: String, // model_id（服务端固定 TTS 音色库编号）。
    purpose: String,  // purpose（asset、source 或 source_and_asset 固定导入用途）。
) -> Result<serde_json::Value, String> {
    #[cfg(feature = "ssh-device")]
    {
        trusted(&window, &state)?;
        if state.mode != FrontendMode::Product || state.stopping.load(Ordering::Acquire) {
            return Err("当前窗口或应用状态不允许远程 TTS 文件导入。".into());
        }
        if model_id.is_empty()
            || model_id.len() > 64
            || !model_id
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
            || !matches!(purpose.as_str(), "asset" | "source" | "source_and_asset")
        {
            return Err("服务端 TTS 音色库或固定导入用途无效。".into());
        }
        let context = state.context().ok_or("缺少正式工作区")?;
        let config = match voice_desktop_common::device_config::load_from_root(&context.data) {
            voice_desktop_common::device_config::LoadResult::Valid(config)
                if config.mode == voice_desktop_common::device_config::RunMode::Remote =>
            {
                config
            }
            voice_desktop_common::device_config::LoadResult::Valid(_) => {
                return Err("请先切换到远程配对模式。".into())
            }
            voice_desktop_common::device_config::LoadResult::Missing => {
                return Err("尚未保存远程配对配置。".into())
            }
            voice_desktop_common::device_config::LoadResult::Invalid(error) => return Err(error),
        };
        if !state.ssh_device.connected().await {
            return Err("服务端连接已断开；音频文件未选择或上传。".into());
        }
        let (connection_generation, executor_generation) = {
            let channel = state
                .remote_task_channel
                .lock()
                .map_err(|_| "远程任务通道状态不可用。")?;
            let channel = channel
                .as_ref()
                .filter(|channel| channel.running.load(Ordering::Acquire))
                .ok_or("TTS 文件导入需要就绪的服务端文件传输通道。")?;
            (channel.connection_generation, channel.executor_generation)
        };
        let selected =
            tauri::async_runtime::spawn_blocking(voice_desktop_common::native_ui::choose_tts_asset)
                .await
                .map_err(|_| "客户端本机音频选择器无法启动。")??;
        let Some(local_path) = selected else {
            return Ok(
                serde_json::json!({"cancelled":true,"fields":{"cancelled":"用户取消客户端本机参考音频选择"}}),
            );
        };
        voice_desktop_common::device_file_channel::reject_reparse_chain(&local_path)?;
        let metadata = std::fs::symlink_metadata(&local_path)
            .map_err(|_| "客户端所选音频文件不可访问。".to_string())?;
        if metadata.file_type().is_symlink() || !metadata.is_file() {
            return Err("客户端 TTS 参考音频必须是普通本地文件，不能是链接或特殊文件。".into());
        }
        const MAX_TTS_ASSET_BYTES: u64 = 20 * 1024 * 1024; // MAX_TTS_ASSET_BYTES（沿用服务端参考音频 20 MiB 上限）。
        let expected_bytes = metadata.len();
        if expected_bytes == 0 || expected_bytes > MAX_TTS_ASSET_BYTES {
            return Err("客户端 TTS 参考音频必须为 1 至 20 MiB。".into());
        }
        let filename = local_path
            .file_name()
            .and_then(|name| name.to_str())
            .filter(|name| {
                !name.is_empty() && name.len() <= 240 && !name.chars().any(char::is_control)
            })
            .ok_or("客户端 TTS 参考音频文件名无效。")?
            .to_owned();
        let extension = local_path
            .extension()
            .and_then(|value| value.to_str())
            .unwrap_or_default()
            .to_ascii_lowercase();
        if !matches!(extension.as_str(), "wav" | "mp3" | "flac" | "ogg") {
            return Err("客户端参考音频只支持 WAV、MP3、FLAC 或 OGG 文件。".into());
        }
        let parent_request_id = rand::random::<[u8; 16]>()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        let target_host_id = config
            .host
            .as_ref()
            .and_then(|host| host.host_id.as_deref())
            .ok_or("远程配置缺少配对服务端编号。")?
            .to_owned();
        let prepared = remote_core_management_request(
            state.inner(),
            &target_host_id,
            &parent_request_id,
            connection_generation,
            "tts.assets.import.prepare",
            serde_json::json!({
                "model_id":model_id,
                "purpose":purpose,
                "filename":filename,
                "expected_bytes":expected_bytes,
                "parent_request_id":parent_request_id,
            }),
        )
        .await?;
        let transfer_id = prepared
            .get("transfer_id")
            .and_then(serde_json::Value::as_str)
            .filter(|value| {
                voice_desktop_common::device_management_contract::valid_request_id(value)
            })
            .ok_or("服务端没有签发有效 TTS 文件传输编号。")?
            .to_owned();
        if prepared
            .get("parent_request_id")
            .and_then(serde_json::Value::as_str)
            != Some(parent_request_id.as_str())
            || prepared
                .get("connection_generation")
                .and_then(serde_json::Value::as_u64)
                != Some(connection_generation)
            || prepared
                .get("executor_generation")
                .and_then(serde_json::Value::as_u64)
                != Some(executor_generation)
            || prepared
                .get("expected_bytes")
                .and_then(serde_json::Value::as_u64)
                != Some(expected_bytes)
        {
            return Err("服务端 TTS 文件票据与原请求、代次或文件大小不匹配。".into());
        }
        let upload = state
            .ssh_device
            .device_file_upload(
                &local_path,
                &transfer_id,
                &parent_request_id,
                connection_generation,
                executor_generation,
                MAX_TTS_ASSET_BYTES,
                Duration::from_secs(300),
            )
            .await;
        let upload = match upload {
            Ok(receipt) => {
                validate_tts_asset_upload_receipt(receipt, &transfer_id, expected_bytes)?
            }
            Err(upload_error) => {
                recover_tts_asset_upload_receipt(
                    &transfer_id,
                    expected_bytes,
                    &upload_error,
                    || {
                        state.ssh_device.device_file_upload(
                            &local_path,
                            &transfer_id,
                            &parent_request_id,
                            connection_generation,
                            executor_generation,
                            MAX_TTS_ASSET_BYTES,
                            Duration::from_secs(300),
                        )
                    },
                )
                .await?
            }
        };
        let commit_request_id = tts_asset_commit_request_id(&transfer_id);
        let arguments = serde_json::json!({
            "model_id":model_id,
            "purpose":purpose,
            "filename":filename,
            "expected_bytes":upload.bytes,
            "expected_sha256":upload.sha256,
            "parent_request_id":parent_request_id,
            "transfer_id":transfer_id,
        });
        if !arguments.is_object() {
            return Err("TTS 文件提交参数无法编码。".into());
        }
        let first_commit = remote_core_management_request(
            state.inner(),
            &target_host_id,
            &commit_request_id,
            connection_generation,
            "tts.assets.import.commit",
            arguments.clone(),
        )
        .await;
        recover_tts_asset_import_commit(first_commit, || async {
            remote_core_management_request(
                state.inner(),
                &target_host_id,
                &commit_request_id,
                connection_generation,
                "tts.assets.import.commit",
                arguments,
            )
            .await
        })
        .await
    }
    #[cfg(not(feature = "ssh-device"))]
    {
        let _ = (window, state, model_id, purpose);
        Err("当前程序未包含固定 SSH TTS 文件导入能力。".into())
    }
}

#[cfg(feature = "ssh-device")]
fn tts_asset_commit_request_id(transfer_id: &str) -> String {
    use sha2::Digest as _;
    let digest = sha2::Sha256::digest(format!("tts-asset-commit:{transfer_id}").as_bytes());
    digest[..16]
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// device_ssh_import_start（从本机选择来源并上传到服务端）：路径只留在客户端原生进程。
#[tauri::command(rename_all = "snake_case")]
async fn device_ssh_import_start(
    window: WebviewWindow,
    app: tauri::AppHandle,
    state: tauri::State<'_, Arc<Desktop>>,
    resource_id: Option<String>, // resource_id（服务端目标组件编号；空值表示批量导入）。
) -> Result<serde_json::Value, String> {
    #[cfg(feature = "ssh-device")]
    {
        trusted(&window, &state)?;
        if state.mode != FrontendMode::Product || state.stopping.load(Ordering::Acquire) {
            return Err("当前窗口或应用状态不允许远程资源导入。".into());
        }
        let context = state.context().ok_or("缺少正式工作区")?;
        let config = match voice_desktop_common::device_config::load_from_root(&context.data) {
            voice_desktop_common::device_config::LoadResult::Valid(config)
                if config.mode == voice_desktop_common::device_config::RunMode::Remote =>
            {
                config
            }
            voice_desktop_common::device_config::LoadResult::Valid(_) => {
                return Err("请先切换到远程配对模式。".into())
            }
            voice_desktop_common::device_config::LoadResult::Missing => {
                return Err("尚未保存远程配对配置。".into())
            }
            voice_desktop_common::device_config::LoadResult::Invalid(error) => return Err(error),
        };
        if !state.ssh_device.connected().await {
            return Err("服务端连接已断开；文件未选择或上传。".into());
        }
        if resource_id.as_ref().is_some_and(|id| {
            id.is_empty()
                || id.len() > 128
                || !id.bytes().all(|byte| {
                    byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-')
                })
        }) {
            return Err("服务端组件编号无效。".into());
        }
        if resource_id
            .as_deref()
            .is_some_and(|id| id.starts_with("tts-aniflive-") && id != "tts-aniflive-runtime")
        {
            return Err(
                "远程设备的 AnifLive 模型仅支持服务端从在线网址导入；客户端模型目录上传暂不支持。"
                    .into(),
            );
        }
        let selection_resource = resource_id.clone();
        let sources = tauri::async_runtime::spawn_blocking(move || {
            voice_desktop_common::native_ui::choose_resource(selection_resource.as_deref())
        })
        .await
        .map_err(|_| "客户端资源来源选择器无法启动。")??;
        if sources.is_empty() {
            return Ok(
                serde_json::json!({"cancelled":true,"fields":{"cancelled":"用户取消客户端本机文件或文件夹选择"}}),
            );
        }
        let request_id = rand::random::<[u8; 16]>()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        let host_id = config
            .host
            .as_ref()
            .and_then(|host| host.host_id.as_deref())
            .ok_or("远程配置缺少配对服务端编号。")?
            .to_owned();
        let reservation = state
            .ssh_device
            .management_request(
                &request_id,
                &host_id,
                "resource.import.source.reserve",
                serde_json::json!({"resource_id":resource_id.clone()}),
                Duration::from_secs(60),
            )
            .await?;
        if reservation.status != 201 {
            return Err(reservation.body["message"]
                .as_str()
                .or_else(|| reservation.body["error"].as_str())
                .unwrap_or("服务端未能预留受管导入来源。")
                .to_owned());
        }
        let source_id = reservation.body["source_id"]
            .as_str()
            .filter(|value| voice_desktop_common::remote_import_contract::valid_source_id(value))
            .ok_or("服务端来源预留回执缺少有效编号。")?
            .to_owned();
        let transfer_id = format!("remote-import-{request_id}");
        let cancel = Arc::new(AtomicBool::new(false));
        let transfer_capacity_full = {
            let mut transfers = state
                .remote_import_transfers
                .lock()
                .map_err(|_| "文件传输状态不可用。")?;
            if transfers.len() >= 16 {
                true
            } else {
                transfers.insert(transfer_id.clone(), cancel.clone());
                false
            }
        };
        if transfer_capacity_full {
            let _ = state
                .ssh_device
                .management_request(
                    &rand::random::<[u8; 16]>()
                        .iter()
                        .map(|byte| format!("{byte:02x}"))
                        .collect::<String>(),
                    &host_id,
                    "resource.import.source.abort",
                    serde_json::json!({"source_id":source_id}),
                    Duration::from_secs(20),
                )
                .await;
            return Err("同时进行的客户端远程导入已达上限。".into());
        }
        let desktop = state.inner().clone();
        let transfer_app = app.clone();
        let transfer_host_id = host_id.clone();
        let transfer_request_id = request_id.clone();
        let transfer_source_id = source_id.clone();
        let transfer_resource_id = resource_id.clone().unwrap_or_else(|| "batch".into());
        let response_transfer_id = transfer_id.clone();
        tauri::async_runtime::spawn(async move {
            let progress_id = transfer_id.clone();
            let progress_resource = transfer_resource_id.clone();
            let progress_app = transfer_app.clone();
            let transfer_result = desktop.ssh_device.upload_remote_import(
            &transfer_source_id, &transfer_request_id, &sources, &cancel,
            move |progress| {
                let _ = progress_app.emit("desktop://resource-progress", serde_json::json!({
                    "job_id":progress_id,
                    "resource_id":progress_resource,
                    "state":"running",
                    "phase":format!("正在上传第 {} / {} 个文件（{} / {} 字节）", progress.files_completed + 1, progress.total_files, progress.transferred_bytes, progress.total_bytes),
                    "completed_bytes":progress.transferred_bytes,
                    "total_bytes":progress.total_bytes,
                    "can_cancel":true,
                    "fields":{"job_id":"客户端本次远程文件传输编号","resource_id":"服务端目标资源编号","state":"文件传输状态","phase":"服务端确认的有界分块上传进度","completed_bytes":"服务端确认接收的原始字节数","total_bytes":"已校验客户端来源清单的总字节数","can_cancel":"文件传输是否可取消"}
                }));
            },
        ).await;
            match transfer_result {
                Ok(receipt) => {
                    let _ = transfer_app.emit("desktop://resource-progress", serde_json::json!({
                    "job_id":transfer_id,
                    "resource_id":transfer_resource_id,
                    "state":"complete",
                    "phase":"文件已在服务端通过长度与 SHA-256 校验并提交导入任务",
                    "completed_bytes":receipt.total_bytes,
                    "total_bytes":receipt.total_bytes,
                    "can_cancel":false,
                    "fields":{"job_id":"客户端本次远程文件传输编号","resource_id":"服务端目标资源编号","state":"服务端文件通道完成状态","phase":"服务端完成摘要复核并接受后续原有导入任务","completed_bytes":"服务端复核通过的实际原始字节数","total_bytes":"本次上传原始字节数","can_cancel":"文件传输已完成"}
                }));
                    if let Some(job_id) = receipt.result["job_id"].as_str() {
                        let _ = transfer_app.emit("desktop://resource-progress", serde_json::json!({
                        "job_id":job_id,
                        "resource_id":receipt.result["resource_id"].as_str().unwrap_or(&transfer_resource_id),
                        "state":receipt.result["state"].as_str().unwrap_or("queued"),
                        "phase":receipt.result["phase"].as_str().unwrap_or("已提交服务端资源导入任务"),
                        "can_cancel":true,
                        "fields":{"job_id":"服务端正式资源导入任务编号","resource_id":"服务端导入的资源编号","state":"服务端任务状态","phase":"服务端原有导入处理阶段","can_cancel":"是否可取消服务端导入任务"}
                    }));
                    }
                }
                Err(error) => {
                    let _ = desktop
                        .ssh_device
                        .management_request(
                            &rand::random::<[u8; 16]>()
                                .iter()
                                .map(|byte| format!("{byte:02x}"))
                                .collect::<String>(),
                            &transfer_host_id,
                            "resource.import.source.abort",
                            serde_json::json!({"source_id":transfer_source_id}),
                            Duration::from_secs(20),
                        )
                        .await;
                    let _ = transfer_app.emit("desktop://resource-progress", serde_json::json!({
                    "job_id":transfer_id,
                    "resource_id":transfer_resource_id,
                    "state":if cancel.load(Ordering::Acquire){"cancelled"}else{"failed"},
                    "phase":"客户端来源上传未完成",
                    "error":error,
                    "can_cancel":false,
                    "fields":{"job_id":"客户端本次远程文件传输编号","resource_id":"服务端目标资源编号","state":"上传失败或取消","phase":"尚未提交服务端正式导入任务","error":"受限错误原因，不包含客户端或服务端绝对路径","can_cancel":"传输已终止"}
                }));
                }
            }
            if let Ok(mut transfers) = desktop.remote_import_transfers.lock() {
                transfers.remove(&transfer_id);
            }
        });
        Ok(serde_json::json!({
            "job_id":response_transfer_id,
            "resource_id":resource_id.unwrap_or_else(|| "batch".into()),
            "state":"queued",
            "phase":"等待客户端受管 FILE 上传",
            "can_cancel":true,
            "fields":{"job_id":"本次客户端远程文件传输任务编号","resource_id":"服务端目标组件编号；batch 表示批量导入","state":"文件上传任务状态","phase":"来源已在客户端选择并在服务端预留","can_cancel":"上传期间可请求取消"}
        }))
    }
    #[cfg(not(feature = "ssh-device"))]
    {
        let _ = (window, app, state, resource_id);
        Err("当前产品构建未包含 SSH 远程文件通道。".into())
    }
}

/// device_ssh_import_cancel（取消客户端远程文件传输）：按本机原生任务编号设置传输停止信号。
#[tauri::command(rename_all = "snake_case")]
fn device_ssh_import_cancel(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    job_id: String, // job_id（本机原生签发的 remote-import 传输任务编号）。
) -> Result<(), String> {
    #[cfg(feature = "ssh-device")]
    {
        trusted(&window, &state)?;
        if !job_id.starts_with("remote-import-") || job_id.len() != "remote-import-".len() + 32 {
            return Err("客户端远程文件传输编号无效。".into());
        }
        let transfers = state
            .remote_import_transfers
            .lock()
            .map_err(|_| "文件传输状态不可用。")?;
        let cancel = transfers
            .get(&job_id)
            .ok_or("文件传输已结束或编号不存在。")?;
        cancel.store(true, Ordering::Release);
        Ok(())
    }
    #[cfg(not(feature = "ssh-device"))]
    {
        let _ = (window, state, job_id);
        Err("当前产品构建未包含 SSH 远程文件通道。".into())
    }
}

/// device_ssh_disconnect（断开 SSH 连接）：不重放连接中断的业务请求。
#[tauri::command]
async fn device_ssh_disconnect(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序可以断开 SSH 连接。".into());
    }
    #[cfg(feature = "ssh-device")]
    {
        let voice_forwarder_stopped = stop_remote_voice_forwarder(state.inner()).await?;
        let media_channel_stopped = stop_remote_media_channel(state.inner()).await?;
        let task_channel_stopped = stop_remote_device_task_channel(state.inner()).await?;
        let disconnect_result = state.ssh_device.disconnect().await;
        state
            .remote_client_connected
            .store(false, Ordering::Release);
        let disconnected = disconnect_result?;
        Ok(
            serde_json::json!({"state":"disconnected","disconnected":disconnected,
            "voice_forwarder_stopped":voice_forwarder_stopped,
            "media_channel_stopped":media_channel_stopped,"task_channel_stopped":task_channel_stopped,
            "fields":{"state":"原生 SSH 设备连接状态","disconnected":"本次是否释放了连接",
                "voice_forwarder_stopped":"断开前是否关闭服务端语音 WebSocket 的本机回环转发",
                "media_channel_stopped":"断开前是否关闭了独立语音媒体连接",
                "task_channel_stopped":"断开前是否停止受管远端桌面任务通道"}}),
        )
    }
    #[cfg(not(feature = "ssh-device"))]
    {
        Ok(
            serde_json::json!({"state":"unavailable","disconnected":false,
        "fields":{"state":"原生 SSH 设备连接状态","disconnected":"当前构建不支持 SSH 设备连接"}}),
        )
    }
}

/// device_ssh_status（读取 SSH 连接状态）：不发起网络请求、不续租。
#[tauri::command]
async fn device_ssh_status(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序可以查询 SSH 连接状态。".into());
    }
    #[cfg(feature = "ssh-device")]
    {
        let connected = state.ssh_device.connected().await;
        state
            .remote_client_connected
            .store(connected, Ordering::Release);
        Ok(
            serde_json::json!({"state":if connected{"connected"}else{"disconnected"},"connected":connected,
            "fields":{"state":"原生 SSH 设备连接状态","connected":"连接对象是否仍由本机原生层持有"}}),
        )
    }
    #[cfg(not(feature = "ssh-device"))]
    {
        Ok(serde_json::json!({"state":"unavailable","connected":false,
        "fields":{"state":"原生 SSH 设备连接状态","connected":"当前构建不支持 SSH 设备连接"}}))
    }
}

#[cfg(feature = "ssh-device")]
async fn ensure_remote_desktop_helper(state: Arc<Desktop>) -> Result<(), String> {
    let result = remote_desktop_helper_call(
        state, "remote-health".into(), "health".into(), serde_json::json!({}), 5_000,
    ).await?;
    let session_id = result.pointer("/result/session_id").and_then(serde_json::Value::as_u64);
    if result["ok"] != true || session_id.is_none_or(|session_id| session_id == 0) {
        return Err("远端桌面辅助进程没有可用的交互用户会话。".into());
    }
    Ok(())
}

#[cfg(feature = "ssh-device")]
async fn remote_desktop_helper_call(
    state: Arc<Desktop>,
    request_id: String,
    operation: String,
    arguments: serde_json::Value,
    timeout_ms: u32,
) -> Result<serde_json::Value, String> {
    if state.stopping.load(Ordering::Acquire) {
        return Err("应用正在退出，远端桌面任务结果未知。".into());
    }
    let context = state.context().ok_or("缺少正式工作区")?;
    let helper_path = packaged_device_helper_path(&context).ok_or("缺少已安装桌面辅助脚本。")?;
    tauri::async_runtime::spawn_blocking(move || {
        let mut slot = state.remote_desktop_helper.lock().map_err(|error| error.to_string())?;
        if slot.is_none() {
            *slot = Some(voice_desktop_common::device_helper::DeviceHelper::start(&helper_path)?);
        }
        let helper = slot.as_mut().expect("远端桌面辅助进程已建立");
        let response = helper.call(
            &request_id, &operation, &arguments, Duration::from_millis(timeout_ms.into()),
        );
        if response.is_err() {
            slot.take();
        }
        response
    }).await.map_err(|error| error.to_string())?
}

#[cfg(feature = "ssh-device")]
struct ScreenCaptureFile {
    path: PathBuf, // path（客户端双图暂存文件）：由本机生成，生命周期绑定原截图任务。
    local: PathBuf, // local（合并前的局部图文件）：用于写失败时的作用域回收。
}

#[cfg(feature = "ssh-device")]
impl Drop for ScreenCaptureFile {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.local);
        let _ = fs::remove_file(&self.path);
        if let Some(directory) = self.path.parent() {
            let _ = fs::remove_dir(directory);
        }
    }
}

#[cfg(feature = "ssh-device")]
async fn capture_remote_screen_file(
    state: Arc<Desktop>,
    task: &voice_desktop_common::ssh_device::RemoteDeviceTask,
) -> Result<serde_json::Value, String> {
    use serde_json::{json, Value};
    let arguments = task.arguments.as_object().ok_or("截屏任务参数必须是对象。")?;
    let transfer_id = arguments.get("transfer_id").and_then(Value::as_str)
        .filter(|value| value.len() == 32 && value.bytes().all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase()))
        .ok_or("截屏文件传输编号无效。")?;
    let dimension = |field: &str, range: std::ops::RangeInclusive<u64>| {
        arguments.get(field).and_then(Value::as_u64)
            .filter(|value| range.contains(value)).ok_or("截图尺寸超出设置范围。")
    };
    let max_side = dimension("max_side", 1280..=4096)?;
    let crop_width = dimension("crop_width", 256..=2048)?;
    let crop_height = dimension("crop_height", 256..=2048)?;
    if arguments.len() != 4 || state.stopping.load(Ordering::Acquire) {
        return Err("截屏参数无效或应用已开始退出。".into());
    }
    let context = state.context().ok_or("缺少正式工作区")?;
    let (file, bytes, capture) = tauri::async_runtime::spawn_blocking(move || -> Result<_, String> {
        let root = context.install.join("tmp");
        voice_desktop_common::device_file_channel::reject_reparse_chain(&root)?;
        fs::create_dir_all(&root).map_err(|error| error.to_string())?;
        let suffix: String = rand::random::<[u8; 16]>()
            .iter()
            .map(|value| format!("{value:02x}"))
            .collect();
        let directory = root.join(format!("remote-screen-{suffix}"));
        fs::create_dir(&directory).map_err(|error| error.to_string())?;
        let file = ScreenCaptureFile {
            path: directory.join("capture.bin"),
            local: directory.join("local.png"),
        };
        let capture = voice_desktop_common::native_screen::capture_to_file(
            &file.path, &file.local, max_side as i32, (crop_width as i32, crop_height as i32),
        )?;
        let bytes = fs::metadata(&file.path).map_err(|error| error.to_string())?.len();
        if !capture.get("global_bytes").and_then(Value::as_u64).is_some_and(|first| first > 0 && first < bytes) {
            return Err("原生截屏返回了无效的双图文件偏移。".into());
        }
        let _ = fs::remove_file(&file.local);
        Ok((file, bytes, capture))
    })
    .await
    .map_err(|error| error.to_string())??;
    let receipt = state.ssh_device.device_file_upload(
        &file.path, transfer_id, &task.task_id, task.connection_generation, task.executor_generation, bytes,
        std::time::Duration::from_secs(25),
    ).await?;
    if receipt.path != format!("transfer:{transfer_id}") || receipt.bytes != bytes {
        return Err("双图文件上传回执与原任务不匹配。".into());
    }
    Ok(json!({"transfer_id":transfer_id,"bytes":receipt.bytes,"sha256":receipt.sha256,"capture":capture,
        "fields":{"transfer_id":"原双图上传编号","bytes":"双图的完整字节数","sha256":"完整双图文件摘要",
        "capture":"产生双图时的屏幕与裁剪信息"}}))
}

#[cfg(feature = "ssh-device")]
async fn execute_remote_device_task(
    state: Arc<Desktop>,
    task: voice_desktop_common::ssh_device::RemoteDeviceTask,
) -> Result<voice_desktop_common::ssh_device::RemoteDeviceTaskResult, String> {
    if task.operation.starts_with("playback.") {
        return execute_remote_playback_task(state, task).await;
    }
    if task.operation == "screen.capture_file" {
        let outcome = capture_remote_screen_file(state, &task).await;
        return Ok(voice_desktop_common::ssh_device::RemoteDeviceTaskResult {
            succeeded: outcome.is_ok(),
            result: outcome.unwrap_or_else(|error| serde_json::json!({
                "error": error.chars().take(240).collect::<String>(),
                "fields": {"error":"截图采集或双图文件上传失败原因"}
            })),
        });
    }
    if task.operation.starts_with("terminal.") {
        let outcome = state.device_terminal.execute(
            &task.operation,
            &task.arguments,
            task.connection_generation,
            task.executor_generation,
        );
        return Ok(voice_desktop_common::ssh_device::RemoteDeviceTaskResult {
            succeeded: outcome.is_ok(),
            result: match outcome {
                Ok(value) => value,
                Err(error) => {
                    serde_json::json!({"error":error,"fields":{"error":"受限终端任务拒绝或失败原因"}})
                }
            },
        });
    }
    if task.operation.starts_with("file.") {
        if task.operation == "file.sftp.upload" || task.operation == "file.sftp.download" {
            let arguments = task
                .arguments
                .as_object()
                .ok_or("固定文件通道任务参数必须是对象。")?;
            let read_string = |key: &str| {
                arguments
                    .get(key)
                    .and_then(serde_json::Value::as_str)
                    .filter(|value| {
                        !value.is_empty()
                            && value.len() <= 4096
                            && !value.chars().any(char::is_control)
                    })
                    .ok_or_else(|| format!("固定文件通道任务的 {key} 字段无效。"))
            };
            let transfer_id = read_string("transfer_id")?;
            let parent_request_id = read_string("parent_request_id")?;
            let maximum_bytes = arguments
                .get("maximum_bytes")
                .and_then(serde_json::Value::as_u64)
                .filter(|value| (1..=4 * 1024 * 1024 * 1024).contains(value))
                .ok_or("固定文件通道任务大小上限无效。")?;
            let generations = (task.connection_generation, task.executor_generation);
            let receipt = if task.operation == "file.sftp.upload" {
                if arguments.len() != 4 || !arguments.contains_key("local_path") {
                    return Err("固定文件通道上传任务字段无效。".into());
                }
                let local_path = std::path::PathBuf::from(read_string("local_path")?);
                state
                    .ssh_device
                    .device_file_upload(
                        &local_path,
                        transfer_id,
                        parent_request_id,
                        generations.0,
                        generations.1,
                        maximum_bytes,
                        std::time::Duration::from_secs(480),
                    )
                    .await?
            } else {
                if arguments.len() != 5
                    || !arguments.contains_key("local_path")
                    || !arguments.contains_key("sha256")
                {
                    return Err("固定文件通道下载任务字段无效。".into());
                }
                let local_path = std::path::PathBuf::from(read_string("local_path")?);
                let sha256 = read_string("sha256")?;
                state
                    .ssh_device
                    .device_file_download(
                        &local_path,
                        transfer_id,
                        parent_request_id,
                        generations.0,
                        generations.1,
                        sha256,
                        maximum_bytes,
                        std::time::Duration::from_secs(480),
                    )
                    .await?
            };
            return Ok(voice_desktop_common::ssh_device::RemoteDeviceTaskResult {
                succeeded: true,
                result: serde_json::json!({"path":receipt.path,"bytes":receipt.bytes,"sha256":receipt.sha256,
                    "transfer_id":transfer_id,"fields":{"path":"固定传输编号，不含远端文件系统路径","bytes":"已复核的原始字节数",
                        "sha256":"已复核的原始字节摘要","transfer_id":"不可重用的原传输编号"}}),
            });
        }
        let outcome = voice_desktop_common::device_files::DeviceFileManager.execute(
            &task.operation,
            &task.arguments,
            task.connection_generation,
            task.executor_generation,
        );
        return Ok(voice_desktop_common::ssh_device::RemoteDeviceTaskResult {
            succeeded: outcome.is_ok(),
            result: match outcome {
                Ok(value) => value,
                Err(error) => {
                    serde_json::json!({"error":error,"fields":{"error":"受限文件任务拒绝或失败原因"}})
                }
            },
        });
    }
    if task.operation != "multi_action" {
        let timeout_ms = match task.operation.as_str() {
            "windows" | "apps" => 15_000,
            "tree" | "snapshot" | "shot" => 20_000,
            _ => 30_000,
        };
        let response = remote_desktop_helper_call(
            state,
            task.task_id,
            task.operation,
            task.arguments,
            timeout_ms,
        )
        .await?;
        let succeeded = response["ok"].as_bool().unwrap_or(false);
        let result = if succeeded {
            response
                .get("result")
                .cloned()
                .unwrap_or(serde_json::Value::Null)
        } else {
            response.get("error").cloned().unwrap_or(response)
        };
        return Ok(voice_desktop_common::ssh_device::RemoteDeviceTaskResult { succeeded, result });
    }

    let steps = task
        .arguments
        .get("steps")
        .and_then(serde_json::Value::as_array)
        .ok_or("远端批量桌面任务缺少 steps（步骤列表）。")?;
    let window_id = task
        .arguments
        .get("window_id")
        .and_then(serde_json::Value::as_u64)
        .ok_or("远端批量桌面任务缺少有效窗口编号。")?;
    let continue_on_error = task
        .arguments
        .get("continue_on_error")
        .and_then(serde_json::Value::as_bool)
        .unwrap_or(false);
    if !(1..=32).contains(&steps.len()) || window_id == 0 {
        return Err("远端批量桌面任务超过范围或窗口编号无效。".into());
    }
    let mut completed = Vec::new();
    for (index, step) in steps.iter().enumerate() {
        let kind = step
            .get("kind")
            .and_then(serde_json::Value::as_str)
            .ok_or("远端批量任务步骤缺少操作类型。")?;
        if !matches!(kind, "click" | "type") {
            return Err("远端批量桌面任务包含不支持的步骤。".into());
        }
        let mut request = serde_json::Map::new();
        request.insert("windowId".into(), serde_json::json!(window_id));
        if kind == "click" {
            for key in ["elementId", "x", "y", "button", "selectionMode"] {
                if let Some(value) = step.get(key) {
                    request.insert(key.into(), value.clone());
                }
            }
            let by_element = request
                .get("elementId")
                .and_then(serde_json::Value::as_str)
                .is_some_and(|value| !value.is_empty());
            let by_point = request.contains_key("x") && request.contains_key("y");
            if by_element == by_point {
                return Err("批量点击必须指定元素编号或坐标。".into());
            }
        } else {
            let element_id = step
                .get("elementId")
                .and_then(serde_json::Value::as_str)
                .ok_or("批量输入缺少界面元素编号。")?;
            let text = step
                .get("text")
                .and_then(serde_json::Value::as_str)
                .ok_or("批量输入缺少文本。")?;
            request.insert("elementId".into(), serde_json::json!(element_id));
            request.insert("text".into(), serde_json::json!(text));
            request.insert("rollback".into(), serde_json::Value::Bool(true));
        }
        let helper_arguments = serde_json::json!({"request":request,"focusFallback":false});
        let helper_id = format!("{}:{}", task.task_id, index + 1);
        let response = remote_desktop_helper_call(
            state.clone(),
            helper_id,
            kind.to_string(),
            helper_arguments,
            30_000,
        )
        .await?;
        let succeeded = response["ok"].as_bool() == Some(true);
        completed.push(serde_json::json!({
            "index": index,
            "ok": succeeded,
            "result": if succeeded { response.get("result").cloned().unwrap_or(serde_json::Value::Null) }
                     else { response.get("error").cloned().unwrap_or(serde_json::Value::Null) },
        }));
        if !succeeded && !continue_on_error {
            break;
        }
    }
    let succeeded =
        completed.iter().all(|step| step["ok"] == true) && completed.len() == steps.len();
    Ok(voice_desktop_common::ssh_device::RemoteDeviceTaskResult {
        succeeded,
        result: serde_json::json!({
            "completed_steps": completed,
            "completed_count": completed.iter().filter(|step| step["ok"] == true).count(),
            "total_steps": steps.len(),
            "continued_after_failure": continue_on_error,
        }),
    })
}

#[cfg(feature = "ssh-device")]
async fn execute_remote_playback_task(
    state: Arc<Desktop>,
    task: voice_desktop_common::ssh_device::RemoteDeviceTask,
) -> Result<voice_desktop_common::ssh_device::RemoteDeviceTaskResult, String> {
    use voice_desktop_common::ssh_device::RemoteDeviceTaskResult;
    if task.operation == "playback.pause" {
        return execute_remote_playback_pause_resume(state, task, true).await;
    }
    if task.operation == "playback.resume" {
        return execute_remote_playback_pause_resume(state, task, false).await;
    }
    if task.operation == "playback.prepare_replacement" {
        return execute_remote_playback_replacement_prepare(state, task).await;
    }
    if task.operation == "playback.bind_replacement" {
        return execute_remote_playback_replacement_bind(state, task).await;
    }
    if task.operation == "playback.abort_replacement" {
        return execute_remote_playback_replacement_abort(state, task).await;
    }
    if task.operation == "playback.wait_replacement" {
        return execute_remote_playback_replacement_wait(state, task).await;
    }
    if task.operation != "playback.stop_and_snapshot" {
        return Ok(RemoteDeviceTaskResult {
            succeeded: false,
            result: serde_json::json!({"error":"此客户端版本尚未实现该播放控制操作。",
                "fields":{"error":"受支持的远端播放控制范围；此请求结果必须视为未知"}}),
        });
    }
    let arguments = task
        .arguments
        .as_object()
        .ok_or("远端播放控制参数必须是对象。")?;
    if arguments.len() != 1 || !arguments.contains_key("playback_key") {
        return Err("远端停止快照任务字段不匹配。".into());
    }
    let requested_key = match arguments.get("playback_key") {
        Some(serde_json::Value::Null) => None,
        Some(serde_json::Value::String(value))
            if !value.is_empty() && value.len() <= 128 && value.trim() == value =>
        {
            Some(value.clone())
        }
        _ => return Err("远端停止快照的播放键无效。".into()),
    };
    let mut control = state.remote_playback_control.lock().await;
    let stopping_key = requested_key.clone().or_else(|| control.active_key.clone());
    let was_active = stopping_key.is_some() && stopping_key == control.active_key;
    let snapshot_state = state.clone();
    let stop_state = state.clone();
    let result =
        execute_remote_playback_stop(
            &mut control,
            &task,
            requested_key,
            move || read_remote_playback_snapshot(snapshot_state),
            move || async move {
                device_worker_action(stop_state, |worker| worker.playback_stop()).await
            },
        )
        .await;
    if was_active
        && stopping_key
            .as_ref()
            .is_some_and(|key| control.stopped_keys.contains(key))
    {
        state
            .remote_playback_stop_epoch
            .fetch_add(1, Ordering::AcqRel);
    }
    result
}

#[cfg(feature = "ssh-device")]
fn remote_playback_task_context_matches(
    control: &RemotePlaybackControlState,
    task: &voice_desktop_common::ssh_device::RemoteDeviceTask,
) -> bool {
    control.device_id.as_deref() == Some(task.device_id.as_str())
        && control.core_session_id.as_deref() == Some(task.core_session_id.as_str())
        && control.connection_generation == task.connection_generation
        && control.executor_generation == task.executor_generation
}

#[cfg(feature = "ssh-device")]
fn cache_settled_replacement(
    control: &mut RemotePlaybackControlState,
    replacement_id: &str,
    result: serde_json::Value,
) {
    control
        .settled_replacement_order
        .retain(|value| value != replacement_id);
    control
        .settled_replacement_order
        .push_back(replacement_id.to_owned());
    control
        .settled_replacements
        .insert(replacement_id.to_owned(), result);
    while control.settled_replacement_order.len() > 128 {
        if let Some(expired) = control.settled_replacement_order.pop_front() {
            control.settled_replacements.remove(&expired);
        }
    }
}

#[cfg(feature = "ssh-device")]
fn remember_remote_playback_tombstone(
    keys: &mut HashSet<String>,
    order: &mut VecDeque<String>,
    key: &str,
) {
    order.retain(|existing| existing != key);
    keys.insert(key.to_owned());
    order.push_back(key.to_owned());
    while order.len() > MAX_REMOTE_PLAYBACK_TOMBSTONES {
        if let Some(expired) = order.pop_front() {
            keys.remove(&expired);
        }
    }
}

#[cfg(feature = "ssh-device")]
fn remember_remote_stopped_key(control: &mut RemotePlaybackControlState, key: &str) {
    let RemotePlaybackControlState {
        stopped_keys,
        stopped_key_order,
        ..
    } = control;
    remember_remote_playback_tombstone(stopped_keys, stopped_key_order, key);
    control
        .stopped_receipts
        .retain(|key, _| control.stopped_keys.contains(key));
}

#[cfg(feature = "ssh-device")]
fn remember_remote_replaced_key(control: &mut RemotePlaybackControlState, key: &str) {
    let RemotePlaybackControlState {
        replaced_keys,
        replaced_key_order,
        ..
    } = control;
    remember_remote_playback_tombstone(replaced_keys, replaced_key_order, key);
}

#[cfg(feature = "ssh-device")]
fn remote_playback_key_is_terminal(control: &RemotePlaybackControlState, key: &str) -> bool {
    control.stopped_keys.contains(key) || control.replaced_keys.contains(key)
}

#[cfg(feature = "ssh-device")]
fn prune_remote_playback_tracking(
    control: &RemotePlaybackControlState,
    sample_positions: &mut HashMap<String, u64>,
    latest_sequences: &mut HashMap<String, u64>,
    baselines: &mut HashMap<String, PlaybackSnapshotBaseline>,
) {
    let terminal: HashSet<String> = sample_positions
        .keys()
        .chain(latest_sequences.keys())
        .chain(baselines.keys())
        .filter(|key| remote_playback_key_is_terminal(control, key))
        .cloned()
        .collect();
    for key in terminal {
        sample_positions.remove(&key);
        latest_sequences.remove(&key);
        baselines.remove(&key);
    }
}

#[cfg(feature = "ssh-device")]
fn prune_remote_opus_decoder_states(
    control: &mut RemotePlaybackControlState,
    sample_positions: &HashMap<String, u64>,
    latest_sequences: &HashMap<String, u64>,
    baselines: &HashMap<String, PlaybackSnapshotBaseline>,
) {
    let mut retained = control
        .samples
        .keys()
        .chain(sample_positions.keys())
        .chain(latest_sequences.keys())
        .chain(baselines.keys())
        .chain(control.stopped_keys.iter())
        .chain(control.replaced_keys.iter())
        .cloned()
        .collect::<HashSet<_>>();
    if let Some(replacement) = control.pending_replacement.as_ref() {
        retained.insert(replacement.old_playback_key.clone());
        if let Some(key) = replacement.successor_playback_key.as_ref() {
            retained.insert(key.clone());
        }
        if let Some(key) = replacement.candidate_successor_key.as_ref() {
            retained.insert(key.clone());
        }
    }
    control
        .opus_decoders
        .retain(|key, _| retained.contains(key));
}

#[cfg(feature = "ssh-device")]
fn replacement_task_failure(
    error: String,
) -> voice_desktop_common::ssh_device::RemoteDeviceTaskResult {
    voice_desktop_common::ssh_device::RemoteDeviceTaskResult {
        succeeded: false,
        result: serde_json::json!({"error":error,
            "fields":{"error":"设备明确拒绝的播放替换请求原因"}}),
    }
}

#[cfg(feature = "ssh-device")]
fn worker_response_error(response: &serde_json::Value, fallback: &str) -> String {
    response
        .get("data")
        .and_then(|value| value.get("error"))
        .and_then(serde_json::Value::as_str)
        .unwrap_or(fallback)
        .to_owned()
}

#[cfg(feature = "ssh-device")]
fn worker_replacement_progress(
    response: &serde_json::Value,
    replacement_id: &str,
) -> Result<voice_desktop_common::wasapi_render::PlaybackReplacementProgress, String> {
    if response.get("ok").and_then(serde_json::Value::as_bool) != Some(true)
        || response.get("state").and_then(serde_json::Value::as_str) != Some("ready")
    {
        return Err(worker_response_error(response, "设备未确认播放替换状态"));
    }
    let value = response
        .get("data")
        .and_then(|data| data.get("replacement"))
        .cloned()
        .ok_or("设备替换回执缺少替换状态")?;
    let progress: voice_desktop_common::wasapi_render::PlaybackReplacementProgress =
        serde_json::from_value(value).map_err(|_| "设备替换状态字段无效")?;
    if progress.replacement_id != replacement_id {
        return Err("设备替换回执与原替换编号不匹配".into());
    }
    Ok(progress)
}

#[cfg(feature = "ssh-device")]
fn remote_replacement_result_value(
    control: &RemotePlaybackControlState,
    progress: &voice_desktop_common::wasapi_render::PlaybackReplacementProgress,
    snapshot: PlaybackSnapshotView,
) -> Result<serde_json::Value, String> {
    if !progress.done {
        return Err("设备尚未确认旧播放键结算完成".into());
    }
    let reason = progress
        .reason
        .as_deref()
        .filter(|value| !value.is_empty() && value.trim() == *value)
        .ok_or("替换结算缺少已确认结束原因")?;
    let finished_device_time_us = progress
        .finished_device_time_us
        .filter(|value| *value >= progress.started_device_time_us)
        .ok_or("替换结算缺少有效结束设备时钟")?;
    let baseline = control
        .baselines
        .get(&progress.old_playback_key)
        .ok_or("替换旧播放键缺少设备输出基线")?;
    let enqueued_ms = control
        .samples
        .get(&progress.old_playback_key)
        .map(|(rate, samples)| samples.saturating_mul(1_000) / u64::from((*rate).max(1)))
        .ok_or("替换旧播放键缺少已接收采样计数")?;
    let mut snapshot_value = control.snapshot_value(&progress.old_playback_key, snapshot)?;
    let finished_output_estimated_ms = progress
        .finished_output_estimated_ms
        .filter(|value| *value >= baseline.output_estimated_ms)
        .ok_or("替换结算缺少旧音结束时的设备输出估计")?;
    snapshot_value["played_ms"] = serde_json::json!(snapshot_value["played_ms"]
        .as_u64()
        .unwrap_or(0)
        .min(enqueued_ms));
    snapshot_value["buffered_ms"] = serde_json::json!(0);
    snapshot_value["output_estimated_ms"] = serde_json::json!(finished_output_estimated_ms
        .saturating_sub(baseline.output_estimated_ms)
        .min(enqueued_ms));
    snapshot_value["device_time_us"] = serde_json::json!(finished_device_time_us);
    let snapshot = snapshot_value;
    let attenuated_start_ms = u64::from(progress.attenuated_range_ms[0])
        .saturating_sub(baseline.played_ms)
        .min(enqueued_ms) as u32;
    let attenuated_end_ms = u64::from(progress.attenuated_range_ms[1])
        .saturating_sub(baseline.played_ms)
        .min(enqueued_ms)
        .max(u64::from(attenuated_start_ms)) as u32;
    Ok(serde_json::json!({
        "replacement_id":progress.replacement_id,
        "reason":reason,
        "started_device_time_us":progress.started_device_time_us,
        "finished_device_time_us":finished_device_time_us,
        "events_device_time_us":progress.events_device_time_us,
        "attenuated_range_ms":[attenuated_start_ms,attenuated_end_ms],
        "snapshot":snapshot,
    }))
}

#[cfg(feature = "ssh-device")]
async fn execute_remote_playback_replacement_prepare(
    state: Arc<Desktop>,
    task: voice_desktop_common::ssh_device::RemoteDeviceTask,
) -> Result<voice_desktop_common::ssh_device::RemoteDeviceTaskResult, String> {
    use voice_desktop_common::ssh_device::RemoteDeviceTaskResult;
    let arguments = task
        .arguments
        .as_object()
        .ok_or("远端替换准备参数必须是对象。")?;
    if arguments.len() != 2
        || !arguments.contains_key("playback_key")
        || !arguments.contains_key("max_ms")
    {
        return Err("远端替换准备任务字段不匹配。".into());
    }
    let old_playback_key = arguments
        .get("playback_key")
        .and_then(serde_json::Value::as_str)
        .filter(|value| !value.is_empty() && value.len() <= 128 && value.trim() == *value)
        .ok_or("远端替换准备缺少有效旧播放键。")?
        .to_owned();
    let max_ms = arguments
        .get("max_ms")
        .and_then(serde_json::Value::as_u64)
        .filter(|value| (1..=600).contains(value))
        .ok_or("远端替换准备的收尾时限无效。")? as u32;
    let replacement_id = task.task_id.clone();
    if replacement_id.is_empty()
        || replacement_id.len() > 128
        || replacement_id.trim() != replacement_id
    {
        return Err("远端替换准备任务编号无效。".into());
    }
    let mut control = state.remote_playback_control.lock().await;
    if !remote_playback_task_context_matches(&control, &task) {
        return Err("替换请求与当前媒体设备、核心或连接/执行器代次不匹配。".into());
    }
    if control.control_uncertain {
        return Err("现有播放控制结果未知，不能安全准备替换。".into());
    }
    if control.pending_replacement.is_some() {
        return Ok(replacement_task_failure(
            "当前设备已有未结算的播放替换事务。".into(),
        ));
    }
    if control.active_key.as_deref() != Some(old_playback_key.as_str())
        || control.stopped_keys.contains(&old_playback_key)
        || control.replaced_keys.contains(&old_playback_key)
    {
        return Ok(replacement_task_failure(
            "旧播放键不再是当前设备的活跃输出目标。".into(),
        ));
    }
    remember_remote_replaced_key(&mut control, &old_playback_key);
    control.pending_replacement = Some(RemoteReplacementBinding {
        replacement_id: replacement_id.clone(),
        old_playback_key: old_playback_key.clone(),
        successor_playback_key: None,
        candidate_successor_key: None,
        candidate_next_sample: 0,
    });

    let worker_state = state.clone();
    let worker_replacement_id = replacement_id.clone();
    let worker_old_playback_key = old_playback_key.clone();
    let worker_response =
        match remote_playback_worker_action(worker_state, "prepare_replacement", move |worker| {
            worker.playback_prepare_replacement(
                &worker_replacement_id,
                &worker_old_playback_key,
                max_ms,
            )
        })
        .await
        {
            Ok(response) => response,
            Err(error) => return Err(format!("替换准备设备回执未知：{error}")),
        };
    let response_state = worker_response
        .get("state")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("unknown");
    if worker_response
        .get("ok")
        .and_then(serde_json::Value::as_bool)
        != Some(true)
        || response_state != "ready"
    {
        let error = worker_response_error(&worker_response, "设备未确认替换准备");
        if matches!(response_state, "rejected" | "unavailable" | "unsupported") {
            if control
                .pending_replacement
                .as_ref()
                .is_some_and(|item| item.replacement_id == task.task_id)
            {
                control.pending_replacement = None;
                control.replaced_keys.remove(&old_playback_key);
                control
                    .replaced_key_order
                    .retain(|key| key != &old_playback_key);
            }
            return Ok(replacement_task_failure(error));
        }
        return Err(format!("替换准备设备回执未知：{error}"));
    }

    let progress = worker_replacement_progress(&worker_response, &task.task_id)?;
    if progress.old_playback_key != old_playback_key {
        return Err("设备替换回执的旧播放键与原请求不匹配。".into());
    }
    let raw_snapshot = worker_response
        .get("data")
        .and_then(|data| data.get("snapshot"))
        .cloned()
        .ok_or("替换准备回执缺少设备时钟快照")?;
    let current_snapshot =
        decode_worker_playback_snapshot(&serde_json::json!({"snapshot":raw_snapshot}))?;
    if !remote_playback_task_context_matches(&control, &task)
        || !control.pending_replacement.as_ref().is_some_and(|item| {
            item.replacement_id == task.task_id && item.old_playback_key == old_playback_key
        })
    {
        return Err("替换准备后设备或任务代次已变化，结果未知。".into());
    }
    let ticket_snapshot = control.snapshot_value(&old_playback_key, current_snapshot)?;
    Ok(RemoteDeviceTaskResult {
        succeeded: true,
        result: serde_json::json!({
            "replacement_id":progress.replacement_id,
            "old_playback_key":progress.old_playback_key,
            "prepared_device_time_us":progress.started_device_time_us,
            "snapshot":ticket_snapshot,
        }),
    })
}

#[cfg(feature = "ssh-device")]
async fn execute_remote_playback_replacement_bind(
    state: Arc<Desktop>,
    task: voice_desktop_common::ssh_device::RemoteDeviceTask,
) -> Result<voice_desktop_common::ssh_device::RemoteDeviceTaskResult, String> {
    use voice_desktop_common::ssh_device::RemoteDeviceTaskResult;
    let arguments = task
        .arguments
        .as_object()
        .ok_or("远端后继绑定参数必须是对象。")?;
    if arguments.len() != 2
        || !arguments.contains_key("replacement_id")
        || !arguments.contains_key("playback_key")
    {
        return Err("远端后继绑定任务字段不匹配。".into());
    }
    let replacement_id = arguments
        .get("replacement_id")
        .and_then(serde_json::Value::as_str)
        .filter(|value| !value.is_empty() && value.len() <= 128 && value.trim() == *value)
        .ok_or("远端后继绑定缺少有效替换编号。")?
        .to_owned();
    let successor_key = arguments
        .get("playback_key")
        .and_then(serde_json::Value::as_str)
        .filter(|value| !value.is_empty() && value.len() <= 128 && value.trim() == *value)
        .ok_or("远端后继绑定缺少有效播放键。")?
        .to_owned();
    let mut control = state.remote_playback_control.lock().await;
    if !remote_playback_task_context_matches(&control, &task) {
        return Err("后继绑定与当前设备或连接/执行器代次不匹配。".into());
    }
    let Some(pending) = control.pending_replacement.as_ref() else {
        return Ok(replacement_task_failure(
            "设备没有可绑定的替换事务。".into(),
        ));
    };
    if pending.replacement_id != replacement_id || pending.old_playback_key == successor_key {
        return Ok(replacement_task_failure(
            "后继播放键与原替换事务不匹配。".into(),
        ));
    }
    if pending
        .successor_playback_key
        .as_deref()
        .is_some_and(|value| value != successor_key)
    {
        return Ok(replacement_task_failure(
            "替换事务已绑定到不同的后继播放键。".into(),
        ));
    }
    let worker_state = state.clone();
    let worker_replacement_id = replacement_id.clone();
    let worker_successor_key = successor_key.clone();
    let response = remote_playback_worker_action(worker_state, "bind_replacement", move |worker| {
        worker.playback_bind_replacement(&worker_replacement_id, &worker_successor_key)
    })
    .await
    .map_err(|error| format!("后继绑定设备回执未知：{error}"))?;
    if response.get("ok").and_then(serde_json::Value::as_bool) != Some(true)
        || response.get("state").and_then(serde_json::Value::as_str) != Some("ready")
    {
        let error = worker_response_error(&response, "设备未确认后继绑定");
        if matches!(
            response.get("state").and_then(serde_json::Value::as_str),
            Some("rejected" | "unavailable" | "unsupported")
        ) {
            return Ok(replacement_task_failure(error));
        }
        return Err(format!("后继绑定设备回执未知：{error}"));
    }
    let data = response.get("data").ok_or("后继绑定回执缺少数据")?;
    if data.get("bound").and_then(serde_json::Value::as_bool) != Some(true) {
        return Ok(replacement_task_failure(
            "设备拒绝该后继播放键绑定。".into(),
        ));
    }
    let pending = control
        .pending_replacement
        .as_mut()
        .ok_or("替换事务在设备绑定过程中已变化")?;
    if pending.replacement_id != replacement_id {
        return Err("替换事务在设备绑定过程中已变化，结果未知。".into());
    }
    if pending.candidate_successor_key.as_deref() != Some(successor_key.as_str()) {
        pending.candidate_successor_key = None;
        pending.candidate_next_sample = 0;
    }
    pending.successor_playback_key = Some(successor_key);
    Ok(RemoteDeviceTaskResult {
        succeeded: true,
        result: serde_json::json!({"bound":true}),
    })
}

#[cfg(feature = "ssh-device")]
async fn execute_remote_playback_replacement_abort(
    state: Arc<Desktop>,
    task: voice_desktop_common::ssh_device::RemoteDeviceTask,
) -> Result<voice_desktop_common::ssh_device::RemoteDeviceTaskResult, String> {
    let arguments = task
        .arguments
        .as_object()
        .ok_or("远端替换撤销参数必须是对象。")?;
    if arguments.len() != 2
        || !arguments.contains_key("replacement_id")
        || !arguments.contains_key("reason")
    {
        return Err("远端替换撤销任务字段不匹配。".into());
    }
    let replacement_id = arguments
        .get("replacement_id")
        .and_then(serde_json::Value::as_str)
        .filter(|value| !value.is_empty() && value.len() <= 128 && value.trim() == *value)
        .ok_or("远端替换撤销编号无效。")?
        .to_owned();
    let reason = arguments
        .get("reason")
        .and_then(serde_json::Value::as_str)
        .filter(|value| !value.is_empty() && value.len() <= 128 && value.trim() == *value)
        .ok_or("远端替换撤销原因无效。")?
        .to_owned();
    let old_playback_key = {
        let control = state.remote_playback_control.lock().await;
        if !remote_playback_task_context_matches(&control, &task) {
            return Err("替换撤销与当前设备或连接/执行器代次不匹配。".into());
        }
        if let Some(result) = control.settled_replacements.get(&replacement_id) {
            return Ok(voice_desktop_common::ssh_device::RemoteDeviceTaskResult {
                succeeded: true,
                result: result.clone(),
            });
        }
        let Some(pending) = control
            .pending_replacement
            .as_ref()
            .filter(|item| item.replacement_id == replacement_id)
        else {
            return Err("替换撤销没有可查询的原设备事务，结果未知。".into());
        };
        pending.old_playback_key.clone()
    };
    let worker_state = state.clone();
    let worker_replacement_id = replacement_id.clone();
    let response =
        remote_playback_worker_action(worker_state, "abort_replacement", move |worker| {
            worker.playback_abort_replacement(&worker_replacement_id, &reason)
        })
        .await
        .map_err(|error| format!("替换撤销设备回执未知：{error}"))?;
    let progress = worker_replacement_progress(&response, &replacement_id)
        .map_err(|error| format!("替换撤销设备回执未知：{error}"))?;
    if progress.old_playback_key != old_playback_key {
        return Err("替换撤销回执的旧播放键不匹配，结果未知。".into());
    }
    let snapshot_state = state.clone();
    let snapshot_value =
        remote_playback_worker_action(snapshot_state, "playback_snapshot", |worker| {
            worker.playback_snapshot()
        })
        .await
        .map_err(|error| format!("替换撤销后设备快照未知：{error}"))?;
    let snapshot = decode_worker_playback_snapshot(&snapshot_value)?;
    let mut control = state.remote_playback_control.lock().await;
    if !remote_playback_task_context_matches(&control, &task)
        || !control
            .pending_replacement
            .as_ref()
            .is_some_and(|item| item.replacement_id == replacement_id)
    {
        return Err("替换撤销后设备任务代次已变化，结果未知。".into());
    }
    let result = remote_replacement_result_value(&control, &progress, snapshot)?;
    cache_settled_replacement(&mut control, &replacement_id, result.clone());
    control.baselines.remove(&old_playback_key);
    control.samples.remove(&old_playback_key);
    control.pending_replacement = None;
    Ok(voice_desktop_common::ssh_device::RemoteDeviceTaskResult {
        succeeded: true,
        result,
    })
}

#[cfg(feature = "ssh-device")]
async fn execute_remote_playback_replacement_wait(
    state: Arc<Desktop>,
    task: voice_desktop_common::ssh_device::RemoteDeviceTask,
) -> Result<voice_desktop_common::ssh_device::RemoteDeviceTaskResult, String> {
    let arguments = task
        .arguments
        .as_object()
        .ok_or("远端替换等待参数必须是对象。")?;
    if arguments.len() != 1 || !arguments.contains_key("replacement_id") {
        return Err("远端替换等待任务字段不匹配。".into());
    }
    let replacement_id = arguments
        .get("replacement_id")
        .and_then(serde_json::Value::as_str)
        .filter(|value| !value.is_empty() && value.len() <= 128 && value.trim() == *value)
        .ok_or("远端替换等待编号无效。")?
        .to_owned();
    {
        let control = state.remote_playback_control.lock().await;
        if !remote_playback_task_context_matches(&control, &task) {
            return Err("替换等待与当前设备或连接/执行器代次不匹配。".into());
        }
        if let Some(result) = control.settled_replacements.get(&replacement_id) {
            return Ok(voice_desktop_common::ssh_device::RemoteDeviceTaskResult {
                succeeded: true,
                result: result.clone(),
            });
        }
        if !control
            .pending_replacement
            .as_ref()
            .is_some_and(|item| item.replacement_id == replacement_id)
        {
            return Err("替换等待没有可查询的原设备事务，结果未知。".into());
        }
    }
    let deadline = Instant::now() + Duration::from_secs(4);
    let progress = loop {
        let worker_state = state.clone();
        let worker_replacement_id = replacement_id.clone();
        let response =
            remote_playback_worker_action(worker_state, "replacement_status", move |worker| {
                worker.playback_replacement_status(&worker_replacement_id)
            })
            .await
            .map_err(|error| format!("替换等待设备状态未知：{error}"))?;
        let progress = worker_replacement_progress(&response, &replacement_id)
            .map_err(|error| format!("替换等待设备状态未知：{error}"))?;
        if progress.done {
            break progress;
        }
        if Instant::now() >= deadline {
            return Err("替换旧音结算超时，原设备事务仍须查询。".into());
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    };
    let snapshot_state = state.clone();
    let raw_snapshot =
        remote_playback_worker_action(snapshot_state, "playback_snapshot", |worker| {
            worker.playback_snapshot()
        })
        .await
        .map_err(|error| format!("替换结算后设备快照未知：{error}"))?;
    let snapshot = decode_worker_playback_snapshot(&raw_snapshot)?;
    let mut control = state.remote_playback_control.lock().await;
    if !remote_playback_task_context_matches(&control, &task)
        || !control
            .pending_replacement
            .as_ref()
            .is_some_and(|item| item.replacement_id == replacement_id)
    {
        return Err("替换结算期间设备或原事务发生变化，结果未知。".into());
    }
    let result = remote_replacement_result_value(&control, &progress, snapshot)?;
    cache_settled_replacement(&mut control, &replacement_id, result.clone());
    control.baselines.remove(&progress.old_playback_key);
    control.samples.remove(&progress.old_playback_key);
    control.pending_replacement = None;
    Ok(voice_desktop_common::ssh_device::RemoteDeviceTaskResult {
        succeeded: true,
        result,
    })
}

#[cfg(feature = "ssh-device")]
async fn execute_remote_playback_pause_resume(
    state: Arc<Desktop>,
    task: voice_desktop_common::ssh_device::RemoteDeviceTask,
    requested_paused: bool,
) -> Result<voice_desktop_common::ssh_device::RemoteDeviceTaskResult, String> {
    let arguments = task
        .arguments
        .as_object()
        .ok_or("远端播放暂停/恢复参数必须是对象。")?;
    if arguments.len() != 1 || !arguments.contains_key("playback_key") {
        return Err("远端播放暂停/恢复任务字段不匹配。".into());
    }
    let playback_key = arguments
        .get("playback_key")
        .and_then(serde_json::Value::as_str)
        .filter(|value| !value.is_empty() && value.len() <= 128 && value.trim() == *value)
        .ok_or("远端播放暂停/恢复任务的播放键无效。")?
        .to_owned();
    let mut control = state.remote_playback_control.lock().await;
    let worker_state = state.clone();
    execute_remote_playback_transition(
        &mut control,
        &task,
        playback_key,
        requested_paused,
        move || async move {
            let value = if requested_paused {
                device_worker_action(worker_state, |worker| worker.playback_pause()).await?
            } else {
                device_worker_action(worker_state, |worker| worker.playback_resume()).await?
            };
            if value.get("playing").and_then(serde_json::Value::as_bool) == Some(true)
                && value.get("paused").and_then(serde_json::Value::as_bool)
                    == Some(requested_paused)
            {
                Ok(())
            } else {
                Err("设备播放暂停/恢复回执与操作目标不匹配".into())
            }
        },
    )
    .await
}

#[cfg(feature = "ssh-device")]
async fn execute_remote_playback_transition<F, FFuture>(
    control: &mut RemotePlaybackControlState,
    task: &voice_desktop_common::ssh_device::RemoteDeviceTask,
    playback_key: String,
    requested_paused: bool,
    transition: F,
) -> Result<voice_desktop_common::ssh_device::RemoteDeviceTaskResult, String>
where
    F: FnOnce() -> FFuture,
    FFuture: Future<Output = Result<(), String>>,
{
    use voice_desktop_common::ssh_device::RemoteDeviceTaskResult;
    if control.device_id.as_deref() != Some(task.device_id.as_str())
        || control.core_session_id.as_deref() != Some(task.core_session_id.as_str())
        || control.connection_generation != task.connection_generation
        || control.executor_generation != task.executor_generation
    {
        return Err("播放暂停/恢复请求与当前媒体设备、核心或执行器代次不匹配。".into());
    }
    if control.active_key.as_deref() != Some(playback_key.as_str())
        || control.stopped_keys.contains(&playback_key)
    {
        return Ok(RemoteDeviceTaskResult {
            succeeded: false,
            result: serde_json::json!({"error":"指定播放键不再拥有当前设备输出。",
                "fields":{"error":"不能改变其它或已停止播放键的设备状态"}}),
        });
    }
    if control.control_uncertain {
        return Err("播放控制结果未知，须先通过停止任务隔离当前播放键。".into());
    }
    if control.paused == requested_paused {
        return Ok(RemoteDeviceTaskResult {
            succeeded: true,
            result: serde_json::json!({"playback_key":playback_key,"paused":requested_paused,
                "fields":{"playback_key":"固定播放事务编号","paused":"设备确认的暂停状态"}}),
        });
    }
    if let Err(error) = transition().await {
        control.control_uncertain = true;
        return Err(format!("设备播放暂停/恢复回执未知：{error}"));
    }
    control.paused = requested_paused;
    Ok(RemoteDeviceTaskResult {
        succeeded: true,
        result: serde_json::json!({"playback_key":playback_key,"paused":requested_paused,
            "fields":{"playback_key":"固定播放事务编号","paused":"设备线程已确认的暂停状态"}}),
    })
}

#[cfg(feature = "ssh-device")]
async fn execute_remote_playback_stop<FRead, FReadFuture, FStop, FStopFuture>(
    control: &mut RemotePlaybackControlState,
    task: &voice_desktop_common::ssh_device::RemoteDeviceTask,
    requested_key: Option<String>,
    read_snapshot: FRead,
    stop_device: FStop,
) -> Result<voice_desktop_common::ssh_device::RemoteDeviceTaskResult, String>
where
    FRead: FnOnce() -> FReadFuture,
    FReadFuture: std::future::Future<Output = Result<PlaybackSnapshotView, String>>,
    FStop: FnOnce() -> FStopFuture,
    FStopFuture: std::future::Future<Output = Result<(), String>>,
{
    use voice_desktop_common::ssh_device::RemoteDeviceTaskResult;
    if control.device_id.as_deref() != Some(task.device_id.as_str())
        || control.core_session_id.as_deref() != Some(task.core_session_id.as_str())
        || control.connection_generation != task.connection_generation
        || control.executor_generation != task.executor_generation
    {
        return Err("播放控制请求与当前媒体设备、核心或执行器代次不匹配。".into());
    }
    let playback_key = requested_key
        .or_else(|| control.active_key.clone())
        .ok_or("没有可停止的当前播放事务。")?;
    let stopped = stop_remote_playback(control, &playback_key, read_snapshot, stop_device).await?;
    Ok(match stopped {
        Some(receipt) => RemoteDeviceTaskResult {
            succeeded: true,
            result: receipt.snapshot,
        },
        None => RemoteDeviceTaskResult {
            succeeded: false,
            result: serde_json::json!({"error":"指定播放键不再拥有当前设备输出；停止结果未知。",
                "fields":{"error":"不能停止其它播放键或推断其输出状态"}}),
        },
    })
}

#[cfg(feature = "ssh-device")]
async fn stop_remote_playback<FRead, FReadFuture, FStop, FStopFuture>(
    control: &mut RemotePlaybackControlState,
    playback_key: &str,
    read_snapshot: FRead,
    stop_device: FStop,
) -> Result<Option<RemotePlaybackStopReceipt>, String>
where
    FRead: FnOnce() -> FReadFuture,
    FReadFuture: Future<Output = Result<PlaybackSnapshotView, String>>,
    FStop: FnOnce() -> FStopFuture,
    FStopFuture: Future<Output = Result<(), String>>,
{
    if let Some(receipt) = control.stopped_receipts.get(playback_key) {
        return Ok(Some(receipt.clone()));
    }
    let owns_output = control.active_key.as_deref() == Some(playback_key);
    let unplayed_client_tts = control
        .client_tts
        .get(playback_key)
        .is_some_and(|tts| tts.accepted_samples == 0);
    if control.stopped_keys.contains(playback_key) || (!owns_output && !unplayed_client_tts) {
        return Ok(None);
    }
    if let Some((key, cancellation)) = &control.client_tts_cancellation {
        if key == playback_key {
            cancellation.request_cancel();
        }
    }
    let current = if owns_output {
        read_snapshot().await
    } else {
        Ok(PlaybackSnapshotView::default())
    };
    let receipt = current.and_then(|current| {
        let snapshot = control.snapshot_value(playback_key, current)?;
        let client_tts = control
            .client_tts
            .get_mut(playback_key)
            .map(|tts| tts.progress(playback_key, current, true))
            .transpose()?;
        Ok(RemotePlaybackStopReceipt {
            snapshot,
            client_tts,
        })
    });
    remember_remote_stopped_key(control, playback_key);
    if owns_output {
        control.active_key = None;
    }
    if owns_output {
        if let Err(error) = stop_device().await {
            // 设备可能已经停止，保留终态标记以拒绝迟到帧。
            control.control_uncertain = true;
            return Err(format!("设备停止回执未知：{error}"));
        }
        if let Some(replacement) = control.pending_replacement.take() {
            remember_remote_stopped_key(control, &replacement.old_playback_key);
            control.baselines.remove(&replacement.old_playback_key);
            control.samples.remove(&replacement.old_playback_key);
            if let Some(successor) = replacement.successor_playback_key {
                remember_remote_stopped_key(control, &successor);
                control.baselines.remove(&successor);
                control.samples.remove(&successor);
            }
        }
        control.paused = false;
        control.control_uncertain = false;
    }
    control.baselines.remove(playback_key);
    control.samples.remove(playback_key);
    let receipt = receipt?;
    control
        .stopped_receipts
        .insert(playback_key.to_owned(), receipt.clone());
    Ok(Some(receipt))
}

#[cfg(feature = "ssh-device")]
async fn device_worker_action<T, F>(state: Arc<Desktop>, action: F) -> Result<T, String>
where
    T: Send + 'static,
    F: FnOnce(&mut voice_desktop_common::device_worker::ManagedWorker) -> Result<T, String>
        + Send
        + 'static,
{
    tauri::async_runtime::spawn_blocking(move || {
        let mut slot = state
            .device_worker
            .lock()
            .map_err(|error| error.to_string())?;
        let worker = slot.as_mut().ok_or("设备工作进程尚未启动")?;
        action(worker)
    })
    .await
    .map_err(|error| error.to_string())?
}

#[cfg(feature = "ssh-device")]
async fn remote_playback_worker_action<F>(
    state: Arc<Desktop>,
    operation: &'static str,
    action: F,
) -> Result<serde_json::Value, String>
where
    F: FnOnce(
            &mut voice_desktop_common::device_worker::ManagedWorker,
        ) -> Result<serde_json::Value, String>
        + Send
        + 'static,
{
    #[cfg(test)]
    {
        let response = state
            .playback_worker_test_responses
            .lock()
            .map_err(|_| "播放设备测试回执队列不可用")?
            .pop_front();
        if let Some(response) = response {
            state
                .playback_worker_test_operations
                .lock()
                .map_err(|_| "播放设备测试操作记录不可用")?
                .push(operation.to_owned());
            return Ok(response);
        }
    }
    let _ = operation;
    device_worker_action(state, action).await
}

#[cfg(feature = "ssh-device")]
fn encode_worker_media_frame(
    device_id: &str,
    core_session_id: &str,
    stream_id: &str,
    connection_generation: u64,
    executor_generation: u64,
    interaction_generation: u64,
    frame: &serde_json::Value,
) -> Result<Vec<u8>, String> {
    let (_, _, _, pcm) = decode_worker_capture_pcm(frame)?;
    encode_worker_media_packet(
        device_id,
        core_session_id,
        stream_id,
        connection_generation,
        executor_generation,
        interaction_generation,
        frame,
        voice_desktop_common::device_protocol::AudioCodec::Pcm,
        pcm,
    )
}

#[cfg(feature = "ssh-device")]
fn encode_worker_media_packet(
    device_id: &str,
    core_session_id: &str,
    stream_id: &str,
    connection_generation: u64,
    executor_generation: u64,
    interaction_generation: u64,
    frame: &serde_json::Value,
    audio_codec: voice_desktop_common::device_protocol::AudioCodec,
    payload: Vec<u8>,
) -> Result<Vec<u8>, String> {
    let sequence = frame
        .get("sequence")
        .and_then(serde_json::Value::as_u64)
        .ok_or("设备采集帧缺少序号")?;
    let start_sample = frame
        .get("start_sample")
        .and_then(serde_json::Value::as_u64)
        .ok_or("设备采集帧缺少采样位置")?;
    let device_time_us = frame
        .get("device_time_us")
        .and_then(serde_json::Value::as_u64)
        .ok_or("设备采集帧缺少 QPC 时钟")?;
    let audio_processing: voice_desktop_common::device_protocol::AudioProcessingMetadata =
        serde_json::from_value(
            frame
                .get("audio_processing")
                .cloned()
                .ok_or("设备采集帧缺少 audio_processing（设备前处理状态）")?,
        )
        .map_err(|_| "设备采集帧的 audio_processing（设备前处理状态）字段无效")?;
    let header = voice_desktop_common::device_protocol::MediaHeader {
        protocol_version: voice_desktop_common::device_protocol::MEDIA_PROTOCOL_VERSION,
        device_id: device_id.to_owned(),
        core_session_id: core_session_id.to_owned(),
        connection_generation,
        executor_generation,
        interaction_generation,
        stream_id: stream_id.to_owned(),
        sequence,
        start_sample,
        sample_count: 320,
        device_time_us,
        audio_codec,
        discontinuity_before: frame.get("discontinuity_before")
            .and_then(serde_json::Value::as_bool)
            .ok_or("设备采集帧缺少间断标记。")?,
        audio_processing,
    };
    voice_desktop_common::device_protocol::encode_frame(&header, &payload)
}

#[cfg(feature = "ssh-device")]
fn decode_worker_capture_pcm(
    frame: &serde_json::Value,
) -> Result<(u64, u64, u64, Vec<u8>), String> {
    let sequence = frame
        .get("sequence")
        .and_then(serde_json::Value::as_u64)
        .ok_or("设备采集帧缺少序号")?;
    let start_sample = frame
        .get("start_sample")
        .and_then(serde_json::Value::as_u64)
        .ok_or("设备采集帧缺少采样位置")?;
    let device_time_us = frame
        .get("device_time_us")
        .and_then(serde_json::Value::as_u64)
        .ok_or("设备采集帧缺少 QPC 时钟")?;
    let raw_pcm = frame
        .get("pcm_le_bytes")
        .and_then(serde_json::Value::as_array)
        .ok_or("设备采集帧缺少 PCM 字节")?;
    if raw_pcm.len() != 640 {
        return Err("设备采集帧必须包含 320 个 16 位单声道样本。".into());
    }
    let mut pcm = Vec::with_capacity(raw_pcm.len());
    for value in raw_pcm {
        let byte = value
            .as_u64()
            .filter(|byte| *byte <= 255)
            .ok_or("设备采集帧包含无效 PCM 字节")?;
        pcm.push(byte as u8);
    }
    Ok((sequence, start_sample, device_time_us, pcm))
}

#[cfg(all(feature = "ssh-device", feature = "native-audio"))]
enum OpusEncoderRequest {
    Encode {
        pcm: Vec<u8>, // pcm（一个 20 毫秒的 16 kHz 单声道小端 PCM 帧）。
        response: tokio::sync::oneshot::Sender<Result<Vec<u8>, String>>, // response（与该输入帧一一对应的编码结果回执）。
    },
}

#[cfg(all(feature = "ssh-device", feature = "native-audio"))]
struct OpusEncoderWorker {
    sender: Option<std::sync::mpsc::SyncSender<OpusEncoderRequest>>, // sender（容量为 1 的编码任务队列）。
    thread: Option<std::thread::JoinHandle<()>>, // thread（唯一拥有 libopus 状态的编码线程）。
}

#[cfg(all(feature = "ssh-device", feature = "native-audio"))]
impl OpusEncoderWorker {
    fn start(expected_pre_skip_samples: u32) -> Result<Self, String> {
        let (sender, receiver) = std::sync::mpsc::sync_channel::<OpusEncoderRequest>(1);
        let (ready_sender, ready_receiver) =
            std::sync::mpsc::sync_channel::<Result<u32, String>>(1);
        let thread = std::thread::Builder::new()
            .name("voice-opus-uplink".into())
            .spawn(move || {
                let mut encoder =
                    match voice_desktop_common::native_opus::OpusEncoder::new(16_000, None) {
                        Ok(encoder) => encoder,
                        Err(error) => {
                            let _ = ready_sender.send(Err(error));
                            return;
                        }
                    };
                let pre_skip_samples = encoder.lookahead_samples();
                if ready_sender.send(Ok(pre_skip_samples)).is_err() {
                    return;
                }
                while let Ok(request) = receiver.recv() {
                    match request {
                        OpusEncoderRequest::Encode { pcm, response } => {
                            let result = if pcm.len() != 640 || pcm.len() % 2 != 0 {
                                Err("Opus 编码工作线程收到的 PCM 帧长度无效。".into())
                            } else {
                                let samples = pcm
                                    .chunks_exact(2)
                                    .map(|pair| i16::from_le_bytes([pair[0], pair[1]]))
                                    .collect::<Vec<_>>();
                                encoder.encode(&samples)
                            };
                            let _ = response.send(result);
                        }
                    }
                }
            })
            .map_err(|error| format!("无法启动 Opus 编码工作线程：{error}"))?;
        let pre_skip_samples = ready_receiver
            .recv()
            .map_err(|_| "Opus 编码工作线程未返回预延迟状态。".to_string())??;
        if pre_skip_samples != expected_pre_skip_samples {
            drop(sender);
            let _ = thread.join();
            return Err("Opus 编码器预延迟与签名媒体握手不匹配。".into());
        }
        Ok(Self {
            sender: Some(sender),
            thread: Some(thread),
        })
    }

    async fn encode(&self, pcm: Vec<u8>) -> Result<Vec<u8>, String> {
        let (response, result) = tokio::sync::oneshot::channel();
        self.sender
            .as_ref()
            .ok_or("Opus 编码工作线程已经关闭。")?
            .try_send(OpusEncoderRequest::Encode { pcm, response })
            .map_err(|error| match error {
                std::sync::mpsc::TrySendError::Full(_) => {
                    "Opus 编码工作线程队列已满；为保持采样连续性停止当前流。".to_string()
                }
                std::sync::mpsc::TrySendError::Disconnected(_) => {
                    "Opus 编码工作线程已经退出。".to_string()
                }
            })?;
        result
            .await
            .map_err(|_| "Opus 编码工作线程未返回帧结果。".to_string())?
    }
}

#[cfg(all(feature = "ssh-device", feature = "native-audio"))]
impl Drop for OpusEncoderWorker {
    fn drop(&mut self) {
        self.sender.take();
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

#[cfg(all(feature = "ssh-device", feature = "native-audio"))]
fn encode_worker_media_frame_with_codec(
    device_id: &str,
    core_session_id: &str,
    stream_id: &str,
    connection_generation: u64,
    executor_generation: u64,
    interaction_generation: u64,
    frame: &serde_json::Value,
    audio_codec: voice_desktop_common::device_protocol::AudioCodec,
    mut opus_encoder: Option<&mut voice_desktop_common::native_opus::OpusEncoder>,
) -> Result<Vec<u8>, String> {
    let (_, _, _, pcm) = decode_worker_capture_pcm(frame)?;
    let payload = match (audio_codec, opus_encoder.as_deref_mut()) {
        (voice_desktop_common::device_protocol::AudioCodec::Pcm, None) => pcm,
        (voice_desktop_common::device_protocol::AudioCodec::Pcm, Some(_)) => {
            return Err("PCM 媒体帧不能绑定 Opus 编码器。".into());
        }
        (voice_desktop_common::device_protocol::AudioCodec::Opus, Some(encoder)) => {
            let samples = pcm
                .chunks_exact(2)
                .map(|pair| i16::from_le_bytes([pair[0], pair[1]]))
                .collect::<Vec<_>>();
            encoder.encode(&samples)?
        }
        (voice_desktop_common::device_protocol::AudioCodec::Opus, None) => {
            return Err("Opus 媒体帧缺少该流的有状态编码器。".into());
        }
    };
    encode_worker_media_packet(
        device_id,
        core_session_id,
        stream_id,
        connection_generation,
        executor_generation,
        interaction_generation,
        frame,
        audio_codec,
        payload,
    )
}

#[cfg(feature = "ssh-device")]
fn decode_worker_playback_snapshot(
    value: &serde_json::Value,
) -> Result<PlaybackSnapshotView, String> {
    let snapshot = value
        .get("snapshot")
        .ok_or("设备播放快照缺少设备时钟数据")?;
    let counter = |name: &str| -> Result<u64, String> {
        snapshot
            .get(name)
            .and_then(serde_json::Value::as_u64)
            .ok_or_else(|| format!("设备播放快照缺少有效字段 {name}（数值）"))
    };
    let started = snapshot
        .get("started")
        .and_then(serde_json::Value::as_bool)
        .ok_or("设备播放快照缺少 started（是否起播）状态")?;
    Ok(PlaybackSnapshotView {
        enqueued_ms: counter("enqueued_ms")?,
        played_ms: counter("played_ms")?,
        buffered_ms: counter("buffered_ms")?,
        output_estimated_ms: counter("output_estimated_ms")?,
        started,
        device_time_us: counter("device_time_us")?,
    })
}

#[cfg(feature = "ssh-device")]
impl ClientTtsPlaybackLedger {
    fn progress(
        &mut self,
        playback_key: &str,
        snapshot: PlaybackSnapshotView,
        stopped: bool,
    ) -> Result<voice_desktop_common::device_protocol::ClientTtsPlaybackProgress, String> {
        let output_ms = snapshot
            .output_estimated_ms
            .saturating_sub(self.baseline.output_estimated_ms);
        let drained = self.accepted_samples > 0
            && self.baseline.isolated
            && snapshot.started
            && snapshot.device_time_us > 0
            && snapshot.buffered_ms == 0
            && output_ms
                >= snapshot
                    .enqueued_ms
                    .saturating_sub(self.baseline.enqueued_ms);
        let played_samples = if drained {
            self.accepted_samples
        } else {
            (output_ms.saturating_mul(u64::from(self.sample_rate)) / 1_000)
                .min(self.accepted_samples)
        }
        .max(self.played_samples);
        self.played_samples = played_samples;
        let buffered_samples = if stopped {
            0
        } else {
            (snapshot
                .buffered_ms
                .saturating_mul(u64::from(self.sample_rate))
                / 1_000)
                .min(self.accepted_samples - played_samples)
        };
        let progress = voice_desktop_common::device_protocol::ClientTtsPlaybackProgress {
            playback_key: playback_key.to_owned(),
            accepted_samples: self.accepted_samples,
            played_samples,
            buffered_samples,
            output_complete: drained && !stopped,
            device_time_us: snapshot.device_time_us,
        };
        progress.validate().map_err(str::to_owned)?;
        Ok(progress)
    }
}

#[cfg(feature = "ssh-device")]
async fn read_remote_playback_snapshot(
    state: Arc<Desktop>,
) -> Result<PlaybackSnapshotView, String> {
    let value = device_worker_action(state, |worker| worker.playback_snapshot()).await?;
    decode_worker_playback_snapshot(&value)
}

#[cfg(feature = "ssh-device")]
async fn client_tts_playback_control<'a>(
    state: &'a Desktop,
    identity: &client_tts_relay::MediaIdentity,
) -> Result<tokio::sync::MutexGuard<'a, RemotePlaybackControlState>, String> {
    let control = state.remote_playback_control.lock().await;
    if control.device_id.as_deref() != Some(identity.device_id.as_str())
        || control.core_session_id.as_deref() != Some(identity.core_session_id.as_str())
        || control.connection_generation != identity.connection_generation
        || control.executor_generation != identity.executor_generation
        || control.interaction_generation != identity.interaction_generation
    {
        return Err("客户端直放的媒体租约已失效。".into());
    }
    Ok(control)
}

#[cfg(feature = "ssh-device")]
fn client_tts_playback_callbacks(
    state: Arc<Desktop>,
    identity: client_tts_relay::MediaIdentity,
) -> client_tts_relay::DirectPlaybackCallbacks {
    let identity = Arc::new(identity);
    let begin_state = state.clone();
    let begin_identity = identity.clone();
    let begin = Arc::new(
        move |playback_key: String, sample_rate: u32, cancellation| {
            let state = begin_state.clone();
            let identity = begin_identity.clone();
            Box::pin(async move {
                let mut control = client_tts_playback_control(&state, &identity).await?;
                if remote_playback_key_is_terminal(&control, &playback_key) {
                    return Err("客户端直放播放键已经终止。".into());
                }
                if let Some(entry) = control.client_tts.get(&playback_key) {
                    if entry.sample_rate != sample_rate {
                        return Err("同一客户端播放键不能切换 PCM（原始音频采样）采样率。".into());
                    }
                } else {
                    if control.client_tts.len() >= MAX_REMOTE_PLAYBACK_KEYS {
                        return Err("客户端直放播放台账已达本媒体连接的固定容量。".into());
                    }
                    control.client_tts.insert(
                        playback_key.clone(),
                        ClientTtsPlaybackLedger {
                            sample_rate,
                            accepted_samples: 0,
                            played_samples: 0,
                            baseline: PlaybackSnapshotBaseline {
                                enqueued_ms: 0,
                                played_ms: 0,
                                output_estimated_ms: 0,
                                isolated: true,
                            },
                        },
                    );
                }
                control.client_tts_cancellation = Some((playback_key, cancellation));
                Ok(())
            }) as client_tts_relay::PlaybackFuture<()>
        },
    );

    let enqueue_state = state.clone();
    let enqueue_identity = identity.clone();
    let enqueue_pcm = Arc::new(
        move |playback_key: String, sample_rate: u32, pcm: Vec<u8>| {
            let state = enqueue_state.clone();
            let identity = enqueue_identity.clone();
            Box::pin(async move {
                if pcm.is_empty() || pcm.len() % 2 != 0 {
                    return Err("客户端直放拒绝空或不完整 PCM（原始音频采样）。".into());
                }
                let mut control = client_tts_playback_control(&state, &identity).await?;
                if control.control_uncertain
                    || remote_playback_key_is_terminal(&control, &playback_key)
                {
                    return Err("客户端直放播放键已经终止或设备控制结果未知。".into());
                }
                let entry = control
                    .client_tts
                    .get(&playback_key)
                    .ok_or("客户端直放播放键已经清除。")?;
                if entry.sample_rate != sample_rate {
                    return Err("客户端直放采样率与播放键不匹配。".into());
                }
                let first_audio = entry.accepted_samples == 0;
                let accepted_samples = entry
                    .accepted_samples
                    .checked_add((pcm.len() / 2) as u64)
                    .ok_or("客户端直放播放键累计样本数溢出。")?;
                if first_audio {
                    device_worker_action(state.clone(), |worker| worker.playback_start()).await?;
                    let snapshot = read_remote_playback_snapshot(state.clone()).await?;
                    let entry = control
                        .client_tts
                        .get_mut(&playback_key)
                        .ok_or("客户端直放播放键已经清除。")?;
                    entry.baseline = PlaybackSnapshotBaseline {
                        enqueued_ms: snapshot.enqueued_ms,
                        played_ms: snapshot.played_ms,
                        output_estimated_ms: snapshot.output_estimated_ms,
                        isolated: snapshot.buffered_ms == 0,
                    };
                } else if control.active_key.as_deref() != Some(playback_key.as_str()) {
                    return Err("客户端直放播放键已失去设备输出。".into());
                }
                let enqueue_key = playback_key.clone();
                device_worker_action(state.clone(), move |worker| {
                    worker
                        .playback_enqueue_for_key(&enqueue_key, sample_rate, &pcm)
                        .map(|_| ())
                })
                .await?;
                let entry = control
                    .client_tts
                    .get_mut(&playback_key)
                    .ok_or("客户端直放播放键已经清除。")?;
                entry.accepted_samples = accepted_samples;
                control.active_key = Some(playback_key);
                Ok(())
            }) as client_tts_relay::PlaybackFuture<()>
        },
    );

    let progress_state = state.clone();
    let progress_identity = identity.clone();
    let progress = Arc::new(move |playback_key: String, sample_rate: u32| {
        let state = progress_state.clone();
        let identity = progress_identity.clone();
        Box::pin(async move {
            let mut control = client_tts_playback_control(&state, &identity).await?;
            let owns_output = control.active_key.as_deref() == Some(playback_key.as_str());
            let entry = control
                .client_tts
                .get_mut(&playback_key)
                .ok_or("客户端直放播放键已经清除。")?;
            if sample_rate != entry.sample_rate {
                return Err("客户端直放采样率与播放键不匹配。".into());
            }
            let snapshot = if entry.accepted_samples == 0 {
                PlaybackSnapshotView::default()
            } else if owns_output {
                read_remote_playback_snapshot(state.clone()).await?
            } else {
                return Err("客户端直放播放键已失去设备输出。".into());
            };
            entry.progress(&playback_key, snapshot, false)
        })
            as client_tts_relay::PlaybackFuture<
                voice_desktop_common::device_protocol::ClientTtsPlaybackProgress,
            >
    });

    let stop_and_snapshot = Arc::new(move |playback_key: String| {
        let state = state.clone();
        let identity = identity.clone();
        Box::pin(async move {
            let mut control = client_tts_playback_control(&state, &identity).await?;
            let was_active = control.active_key.as_deref() == Some(playback_key.as_str());
            let result = stop_remote_playback(
                &mut control,
                &playback_key,
                || read_remote_playback_snapshot(state.clone()),
                || device_worker_action(state.clone(), |worker| worker.playback_stop()),
            )
            .await;
            if was_active && control.stopped_keys.contains(&playback_key) {
                state
                    .remote_playback_stop_epoch
                    .fetch_add(1, Ordering::AcqRel);
            }
            result?
                .and_then(|receipt| receipt.client_tts)
                .ok_or_else(|| "客户端直放播放键没有已确认的停止回执。".into())
        })
            as client_tts_relay::PlaybackFuture<
                voice_desktop_common::device_protocol::ClientTtsPlaybackProgress,
            >
    });

    client_tts_relay::DirectPlaybackCallbacks {
        begin,
        enqueue_pcm,
        progress,
        stop_and_snapshot,
    }
}

#[cfg(feature = "ssh-device")]
fn remote_playback_progress_value(
    device_id: &str,
    core_session_id: &str,
    stream_id: &str,
    connection_generation: u64,
    executor_generation: u64,
    interaction_generation: u64,
    playback_key: &str,
    sequence: u64,
    receipt_sequence: u64,
    baseline: PlaybackSnapshotBaseline,
    snapshot: PlaybackSnapshotView,
    accepted_ms: u64,
) -> serde_json::Value {
    let (played_ms, output_estimated_ms, started) = baseline.measure(snapshot, accepted_ms);
    serde_json::json!({
        "type":"playback.progress", "protocol_version":voice_desktop_common::device_protocol::MEDIA_PROTOCOL_VERSION,
        "device_id":device_id, "delivery_device_id":device_id,
        "core_session_id":core_session_id,
        "connection_generation":connection_generation,
        "executor_generation":executor_generation,
        "interaction_generation":interaction_generation,
        "stream_id":stream_id, "playback_key":playback_key,
        "receipt_sequence":receipt_sequence, "sequence":sequence,
        "enqueued_ms":accepted_ms,
        "played_ms":played_ms,
        "buffered_ms":snapshot.buffered_ms,
        "output_estimated_ms":output_estimated_ms,
        "started":baseline.isolated && started,
        "device_time_us":snapshot.device_time_us,
        "precision":if baseline.isolated{"device_time"}else{"unknown"},
        "fields":{"type":"控制消息类型","protocol_version":"设备媒体协议版本",
            "device_id":"通过认证的远端设备编号","delivery_device_id":"固定播放输出设备编号",
            "core_session_id":"权威核心运行编号","connection_generation":"设备连接代次",
            "executor_generation":"设备执行器代次","interaction_generation":"语音媒体租约代次",
            "stream_id":"连续媒体通道编号","playback_key":"固定播放事务编号",
            "receipt_sequence":"本连接内递增的快照回执序号","sequence":"最近已接纳播放帧序号",
            "enqueued_ms":"该播放键的设备入队时长","played_ms":"该播放键已提交给输出端点的时长",
            "buffered_ms":"设备当前缓冲时长","output_estimated_ms":"按设备时钟估算已输出时长",
            "started":"当前播放键是否有可归属的实际输出证据",
            "device_time_us":"WASAPI 设备时钟映射到的 QPC 时刻，微秒",
            "precision":"设备时钟证据精度"}
    })
}

#[cfg(feature = "ssh-device")]
fn decode_remote_playback_frame_for_lease(
    bytes: &[u8],
    device_id: &str,
    core_session_id: &str,
    stream_id: &str,
    connection_generation: u64,
    executor_generation: u64,
    interaction_generation: u64,
    next_sequence: u64,
) -> Result<
    (
        voice_desktop_common::device_protocol::PlaybackMediaHeader,
        Vec<u8>,
    ),
    String,
> {
    let (header, pcm) = voice_desktop_common::device_protocol::decode_playback_frame(bytes)
        .map_err(|error| format!("主机下行播放帧无效：{error}"))?;
    if header.device_id != device_id
        || header.delivery_device_id != device_id
        || header.core_session_id != core_session_id
        || header.stream_id != stream_id
        || header.connection_generation != connection_generation
        || header.executor_generation != executor_generation
        || header.interaction_generation != interaction_generation
        || header.sequence != next_sequence
    {
        return Err("主机下行播放帧与固定设备、事务通道或代次不匹配。".into());
    }
    Ok((header, pcm.to_vec()))
}

#[cfg(feature = "ssh-device")]
async fn enqueue_remote_playback_to_worker<
    Start,
    StartFuture,
    Snapshot,
    SnapshotFuture,
    Enqueue,
    EnqueueFuture,
>(
    playback_control: &mut RemotePlaybackControlState,
    playback_baselines: &mut HashMap<String, PlaybackSnapshotBaseline>,
    playback_started: &mut bool,
    header: &voice_desktop_common::device_protocol::PlaybackMediaHeader,
    sample_rate: u32,
    pcm: Vec<u8>,
    start_playback: Start,
    read_snapshot: Snapshot,
    enqueue: Enqueue,
) -> Result<(), String>
where
    Start: FnOnce() -> StartFuture,
    StartFuture: Future<Output = Result<(), String>> + Send,
    Snapshot: FnOnce() -> SnapshotFuture,
    SnapshotFuture: Future<Output = Result<PlaybackSnapshotView, String>> + Send,
    Enqueue: FnOnce(String, u32, Vec<u8>) -> EnqueueFuture,
    EnqueueFuture: Future<Output = Result<(), String>> + Send,
{
    let playback_key = &header.playback_key;
    if !playback_control.samples.contains_key(playback_key)
        && playback_control.samples.len() >= MAX_REMOTE_PLAYBACK_KEYS
    {
        return Err("未结算远端播放键达到上限；为保留原设备事务已拒绝新播放。".into());
    }
    if playback_control
        .samples
        .get(playback_key)
        .is_some_and(|(rate, _)| *rate != sample_rate)
    {
        return Err("同一播放键在输出期间更换了采样率。".into());
    }
    if !*playback_started {
        start_playback().await?;
        *playback_started = true;
    }
    if !playback_baselines.contains_key(playback_key) {
        let before = read_snapshot().await.ok();
        let baseline = before
            .map(|snapshot| PlaybackSnapshotBaseline {
                enqueued_ms: snapshot.enqueued_ms,
                played_ms: snapshot.played_ms,
                output_estimated_ms: snapshot.output_estimated_ms,
                isolated: snapshot.buffered_ms == 0,
            })
            .unwrap_or(PlaybackSnapshotBaseline {
                enqueued_ms: 0,
                played_ms: 0,
                output_estimated_ms: 0,
                isolated: false,
            });
        if !playback_baselines.is_empty() {
            for previous in playback_baselines.values_mut() {
                previous.isolated = false;
            }
        }
        playback_baselines.insert(playback_key.clone(), baseline);
        playback_control
            .baselines
            .insert(playback_key.clone(), baseline);
    }
    enqueue(playback_key.clone(), sample_rate, pcm).await?;
    let entry = playback_control
        .samples
        .entry(playback_key.clone())
        .or_insert((sample_rate, 0));
    entry.1 = entry.1.saturating_add(u64::from(header.sample_count));
    playback_control.active_key = Some(playback_key.clone());
    Ok(())
}

#[cfg(feature = "ssh-device")]
enum RemotePlaybackFrameOutcome {
    Cancelled {
        message: String,
        error: Option<String>,
    }, // message（播放键已停止后的丢弃回执）。
    Accepted {
        message: String,      // message（帧已加入播放队列的接纳回执）。
    },
}

#[cfg(feature = "ssh-device")]
fn cancel_remote_playback_frame(
    header: &voice_desktop_common::device_protocol::PlaybackMediaHeader,
    reason: &str,
    next_playback_sequence: &mut u64,
) -> RemotePlaybackFrameOutcome {
    *next_playback_sequence = next_playback_sequence.saturating_add(1);
    let message = serde_json::json!({
        "type":"playback.cancelled", "protocol_version":voice_desktop_common::device_protocol::MEDIA_PROTOCOL_VERSION,
        "device_id":header.device_id, "delivery_device_id":header.delivery_device_id,
        "core_session_id":header.core_session_id,
        "connection_generation":header.connection_generation,
        "executor_generation":header.executor_generation,
        "interaction_generation":header.interaction_generation,
        "stream_id":header.stream_id, "sequence":header.sequence, "reason":reason,
        "fields":{"type":"控制消息类型","protocol_version":"设备媒体协议版本",
            "device_id":"通过认证的远端设备编号","delivery_device_id":"固定播放输出设备编号",
            "core_session_id":"权威核心运行编号","connection_generation":"设备连接代次",
            "executor_generation":"设备执行器代次","interaction_generation":"语音媒体租约代次",
            "stream_id":"连续媒体流编号","sequence":"未加入输出队列的媒体帧序号",
            "reason":"设备拒收该帧的稳定原因编号"}
    })
    .to_string();
    RemotePlaybackFrameOutcome::Cancelled { message, error: None }
}

#[cfg(feature = "ssh-device")]
fn fail_remote_playback(control: &mut RemotePlaybackControlState, key: &str) {
    remember_remote_stopped_key(control, key);
    control.samples.remove(key);
    control.baselines.remove(key);
    if control.active_key.as_deref() == Some(key) {
        control.active_key = None;
    }
}

#[cfg(feature = "ssh-device")]
async fn process_remote_playback_frame_for_lease<
    Start,
    StartFuture,
    Snapshot,
    SnapshotFuture,
    Enqueue,
    EnqueueFuture,
>(
    bytes: &[u8],
    device_id: &str,
    core_session_id: &str,
    stream_id: &str,
    connection_generation: u64,
    executor_generation: u64,
    interaction_generation: u64,
    next_playback_sequence: &mut u64,
    playback_sample_positions: &mut HashMap<String, u64>,
    playback_latest_sequences: &mut HashMap<String, u64>,
    playback_control: &mut RemotePlaybackControlState,
    playback_baselines: &mut HashMap<String, PlaybackSnapshotBaseline>,
    playback_started: &mut bool,
    start_playback: Start,
    read_snapshot: Snapshot,
    enqueue: Enqueue,
) -> Result<RemotePlaybackFrameOutcome, String>
where
    Start: FnOnce() -> StartFuture,
    StartFuture: Future<Output = Result<(), String>> + Send,
    Snapshot: FnOnce() -> SnapshotFuture,
    SnapshotFuture: Future<Output = Result<PlaybackSnapshotView, String>> + Send,
    Enqueue: FnOnce(String, u32, Vec<u8>) -> EnqueueFuture,
    EnqueueFuture: Future<Output = Result<(), String>> + Send,
{
    let (header, encoded_payload) = decode_remote_playback_frame_for_lease(
        bytes,
        device_id,
        core_session_id,
        stream_id,
        connection_generation,
        executor_generation,
        interaction_generation,
        *next_playback_sequence,
    )?;
    let control_matches = playback_control.device_id.as_deref() == Some(device_id)
        && playback_control.core_session_id.as_deref() == Some(core_session_id)
        && playback_control.connection_generation == connection_generation
        && playback_control.executor_generation == executor_generation
        && playback_control.interaction_generation == interaction_generation;
    if !control_matches {
        return Err("播放控制状态与当前媒体租约不匹配。".into());
    }
    if header.audio_codec != playback_control.downlink_codec {
        return Err("主机下行帧编码与签名媒体握手协商结果不一致。".into());
    }
    prune_remote_playback_tracking(
        playback_control,
        playback_sample_positions,
        playback_latest_sequences,
        playback_baselines,
    );
    prune_remote_opus_decoder_states(
        playback_control,
        playback_sample_positions,
        playback_latest_sequences,
        playback_baselines,
    );
    if playback_control.control_uncertain {
        return Ok(cancel_remote_playback_frame(&header, "output_unavailable", next_playback_sequence));
    }
    let pcm = match header.audio_codec {
        voice_desktop_common::device_protocol::AudioCodec::Pcm => encoded_payload,
        voice_desktop_common::device_protocol::AudioCodec::Opus => {
            if !playback_control
                .opus_decoders
                .contains_key(&header.playback_key)
                && playback_control.opus_decoders.len() >= MAX_REMOTE_OPUS_DECODER_STATES
            {
                return Err("远端 Opus 播放解码状态达到上限；媒体流已拒绝。".into());
            }
            if !playback_control
                .opus_decoders
                .contains_key(&header.playback_key)
            {
                playback_control.opus_decoders.insert(
                    header.playback_key.clone(),
                    RemoteOpusPlaybackDecoder::new(
                        playback_control.downlink_encoder_pre_skip_samples,
                    )?,
                );
            }
            playback_control
                .opus_decoders
                .get_mut(&header.playback_key)
                .ok_or("Opus 播放解码状态未建立")?
                .decode_valid_pcm(&encoded_payload, header.sample_count)?
        }
    };
    if playback_control
        .replaced_keys
        .contains(&header.playback_key)
    {
        return Ok(cancel_remote_playback_frame(
            &header,
            "superseded",
            next_playback_sequence,
        ));
    }
    if playback_control.stopped_keys.contains(&header.playback_key) {
        return Ok(cancel_remote_playback_frame(
            &header,
            "stopped",
            next_playback_sequence,
        ));
    }
    if let Some(replacement) = playback_control.pending_replacement.as_mut() {
        match replacement.successor_playback_key.as_deref() {
            Some(successor) if successor != header.playback_key => {
                return Ok(cancel_remote_playback_frame(
                    &header,
                    "replacement_target_mismatch",
                    next_playback_sequence,
                ));
            }
            None => {
                if replacement.candidate_successor_key.is_none() {
                    replacement.candidate_successor_key = Some(header.playback_key.clone());
                    replacement.candidate_next_sample = playback_sample_positions
                        .get(&header.playback_key)
                        .copied()
                        .unwrap_or(0);
                }
                if replacement.candidate_successor_key.as_deref()
                    == Some(header.playback_key.as_str())
                {
                    if header.start_sample != replacement.candidate_next_sample {
                        return Err("未绑定替换后继帧采样位置不连续。".into());
                    }
                    replacement.candidate_next_sample = replacement
                        .candidate_next_sample
                        .checked_add(u64::from(header.sample_count))
                        .ok_or("未绑定替换后继采样位置溢出")?;
                }
                return Ok(cancel_remote_playback_frame(
                    &header,
                    "replacement_not_bound",
                    next_playback_sequence,
                ));
            }
            _ => {}
        }
    }

    let expected_sample = playback_control
        .pending_replacement
        .as_ref()
        .filter(|replacement| {
            replacement.successor_playback_key.as_deref() == Some(header.playback_key.as_str())
                && replacement.candidate_successor_key.as_deref()
                    == Some(header.playback_key.as_str())
        })
        .map(|replacement| replacement.candidate_next_sample)
        .or_else(|| playback_sample_positions.get(&header.playback_key).copied())
        .unwrap_or(0);
    if header.start_sample != expected_sample {
        return Err("主机下行播放帧采样位置不连续。".into());
    }
    let next_sample = expected_sample
        .checked_add(u64::from(header.sample_count))
        .ok_or("主机下行播放帧采样位置溢出")?;

    let enqueue_result = enqueue_remote_playback_to_worker(
        playback_control,
        playback_baselines,
        playback_started,
        &header,
        header.sample_rate,
        pcm,
        start_playback,
        read_snapshot,
        enqueue,
    )
    .await;
    if let Err(error) = enqueue_result {
        fail_remote_playback(playback_control, &header.playback_key);
        let mut receipt = cancel_remote_playback_frame(&header, "output_unavailable", next_playback_sequence);
        if let RemotePlaybackFrameOutcome::Cancelled { error: reason, .. } = &mut receipt {
            *reason = Some(error);
        }
        return Ok(receipt);
    }
    playback_sample_positions.insert(header.playback_key.clone(), next_sample);
    playback_latest_sequences.insert(header.playback_key.clone(), header.sequence);
    if let Some(replacement) = playback_control
        .pending_replacement
        .as_mut()
        .filter(|replacement| {
            replacement.candidate_successor_key.as_deref() == Some(header.playback_key.as_str())
        })
    {
        replacement.candidate_successor_key = None;
        replacement.candidate_next_sample = 0;
    }
    *next_playback_sequence = next_playback_sequence.saturating_add(1);
    let message = serde_json::json!({
        "type":"playback.accepted", "protocol_version":voice_desktop_common::device_protocol::MEDIA_PROTOCOL_VERSION,
        "device_id":header.device_id, "delivery_device_id":header.delivery_device_id,
        "core_session_id":header.core_session_id,
        "connection_generation":header.connection_generation,
        "executor_generation":header.executor_generation,
        "interaction_generation":header.interaction_generation,
        "stream_id":header.stream_id, "sequence":header.sequence,
        "fields":{"type":"控制消息类型","protocol_version":"设备媒体协议版本",
            "device_id":"通过认证的远端设备编号","delivery_device_id":"固定播放输出设备编号",
            "core_session_id":"权威核心运行编号","connection_generation":"设备连接代次",
            "executor_generation":"设备执行器代次","interaction_generation":"语音媒体租约代次",
            "stream_id":"连续媒体流编号","sequence":"已加入本机有界 WASAPI 播放队列的帧序号"}
    })
    .to_string();
    Ok(RemotePlaybackFrameOutcome::Accepted {
        message,
    })
}

fn client_event(state: &Desktop, event: &str, details: serde_json::Value) {
    if let Some(context) = state.context() {
        // 诊断落盘失败不能中止连接或音频；日志只接收固定元数据，不接收报文正文。
        let _ = voice_desktop_common::logs::record_client_event(&context.install, event, &details);
    }
}

#[cfg(feature = "ssh-device")]
async fn run_remote_media_channel(
    state: Arc<Desktop>,
    channel: voice_desktop_common::ssh_device::SshMediaChannel,
    stop: Arc<AtomicBool>,
    physical_capture: Arc<AtomicBool>,
    app: tauri::AppHandle,
) -> Result<(), String> {
    use voice_desktop_common::ssh_device::{SshMediaMessage, SshMediaWriter};
    let device_id = channel.device_id.clone();
    let stream_id = channel.stream_id.clone();
    let core_session_id = channel.core_session_id.clone();
    let connection_generation = channel.connection_generation;
    let executor_generation = channel.executor_generation;
    let interaction_generation = channel.interaction_generation;
    let uplink_codec = channel.uplink_codec;
    let downlink_codec = channel.downlink_codec;
    let downlink_encoder_pre_skip_samples = channel.downlink_encoder_pre_skip_samples;
    #[cfg(feature = "native-audio")]
    let encoder_pre_skip_samples = channel.encoder_pre_skip_samples;
    #[cfg(feature = "native-audio")]
    let mut uplink_encoder = match uplink_codec {
        voice_desktop_common::device_protocol::AudioCodec::Pcm => None,
        voice_desktop_common::device_protocol::AudioCodec::Opus => {
            Some(OpusEncoderWorker::start(encoder_pre_skip_samples)?)
        }
    };
    #[cfg(not(feature = "native-audio"))]
    if uplink_codec == voice_desktop_common::device_protocol::AudioCodec::Opus {
        return Err("当前构建没有原生 Opus 编码能力。".into());
    }
    #[cfg(not(feature = "native-audio"))]
    if downlink_codec == voice_desktop_common::device_protocol::AudioCodec::Opus {
        return Err("当前构建没有原生 Opus 下行解码能力。".into());
    }
    {
        let mut playback = state.remote_playback_control.lock().await;
        *playback = RemotePlaybackControlState {
            device_id: Some(device_id.clone()),
            core_session_id: Some(core_session_id.clone()),
            connection_generation,
            executor_generation,
            interaction_generation,
            downlink_codec,
            downlink_encoder_pre_skip_samples,
            ..RemotePlaybackControlState::default()
        };
    }
    let mut playback_stop_epoch = state.remote_playback_stop_epoch.load(Ordering::Acquire);
    let mut capture_active = channel.capture_active;
    let mut capture_stream_id = stream_id.clone(); // capture_stream_id（当前输入编号）：独立于固定播放媒体连接。
    let media_started_at = Instant::now();
    client_event(&state, "client.media_ready", serde_json::json!({
        "device_id":device_id,"stream_id":stream_id,"core_session_id":core_session_id,
        "connection_generation":connection_generation,"executor_generation":executor_generation,
        "interaction_generation":interaction_generation,"state":"ready"}));
    let (mut reader, writer, guard) = channel.into_parts();
    let (write_sender, mut write_receiver) = tokio::sync::mpsc::channel::<MediaWriteCommand>(6);
    let (audio_sender, mut audio_receiver) = tokio::sync::watch::channel::<Option<Arc<Vec<u8>>>>(None);
    let (read_sender, mut read_receiver) = tokio::sync::mpsc::channel::<MediaReadEvent>(16);
    let (writer_error_sender, mut writer_error_receiver) = tokio::sync::mpsc::channel::<String>(1);
    let reader_writer = write_sender.clone();

    let reader_task = tauri::async_runtime::spawn(async move {
        loop {
            match reader.receive().await {
                Ok(SshMediaMessage::Ping(payload)) => {
                    if reader_writer
                        .send(MediaWriteCommand::Pong(payload))
                        .await
                        .is_err()
                    {
                        break;
                    }
                }
                Ok(SshMediaMessage::Pong(_payload)) => {}
                Ok(SshMediaMessage::Closed) => {
                    let _ = read_sender
                        .send(MediaReadEvent::Message(SshMediaMessage::Closed))
                        .await;
                    break;
                }
                Ok(message) => {
                    let closed = matches!(message, SshMediaMessage::Closed);
                    if read_sender
                        .send(MediaReadEvent::Message(message))
                        .await
                        .is_err()
                        || closed
                    {
                        break;
                    }
                }
                Err(error) => {
                    let _ = read_sender.send(MediaReadEvent::Failed(error)).await;
                    break;
                }
            }
        }
    });

    let writer_log_state = state.clone();
    let mut writer: SshMediaWriter = writer;
    let mut writer_task = tauri::async_runtime::spawn(async move {
        loop {
            let command = tokio::select! {
                biased;
                command = write_receiver.recv() => match command {
                    Some(command) => command,
                    None => break,
                },
                changed = audio_receiver.changed() => {
                    if changed.is_err() { break; }
                    match audio_receiver.borrow_and_update().clone() {
                        Some(bytes) => MediaWriteCommand::Binary(bytes),
                        None => continue,
                    }
                }
            };
            let closing = matches!(&command, MediaWriteCommand::Close);
            let result = tokio::time::timeout(Duration::from_secs(2), async {
                match command {
                MediaWriteCommand::Binary(bytes) => writer.send_binary(&bytes).await,
                MediaWriteCommand::Text(value) => {
                    let result = writer.send_text(&value).await;
                    if let Ok(control) = serde_json::from_str::<serde_json::Value>(&value) {
                        let kind = control["type"].as_str().unwrap_or("");
                        if matches!(kind, "media.tts.authorization_accepted" | "media.tts.started" | "media.tts.completed" | "media.tts.failed" | "media.tts.cancelled") {
                            client_event(&writer_log_state, "client.tts_receipt", serde_json::json!({
                                "device_id":control["device_id"],"stream_id":control["stream_id"],
                                "playback_key":control["playback_key"],"request_id":control["request_id"],
                                "state":kind,"result":if result.is_ok(){"sent"}else{"failed"}}));
                        }
                    }
                    result
                },
                MediaWriteCommand::Pong(payload) => writer.send_pong(&payload).await,
                    MediaWriteCommand::Close => writer.send_close().await,
                }
            }).await.unwrap_or_else(|_| Err("语音媒体写入超时。".into()));
            if let Err(error) = result {
                let _ = writer_error_sender.send(error).await;
                break;
            }
            if closing { break; }
        }
    });

    let mut capture_started = false;
    let mut playback_started = false;
    let mut next_playback_sequence = 0_u64;
    let mut playback_sample_positions: HashMap<String, u64> = HashMap::new();
    let mut playback_latest_sequences: HashMap<String, u64> = HashMap::new(); // playback_latest_sequences（各播放键最近接纳帧序号）。
    let mut playback_baselines: HashMap<String, PlaybackSnapshotBaseline> = HashMap::new();
    let mut playback_receipt_sequence = 1_u64;
    let mut last_playback_snapshot_at = Instant::now(); // last_playback_snapshot_at（最近设备时钟采样时刻）。
    let mut last_buffer_report = (0_u64, 0_u64, false); // last_buffer_report（上次上报的接收边界、设备储备和暂停状态）。
    let tts_identity = client_tts_relay::MediaIdentity {
        device_id: device_id.clone(),
        core_session_id: core_session_id.clone(),
        connection_generation,
        executor_generation,
        interaction_generation,
        stream_id: stream_id.clone(),
    };
    let mut client_tts_authorizations = client_tts_relay::ClientTtsAuthorizationStore::default();
    let mut revoked_client_tts_keys = HashSet::new(); // revoked_client_tts_keys（撤销后等待当前分段退出的播放键）。
    let client_tts_playback = client_tts_playback_callbacks(state.clone(), tts_identity.clone());
    let mut client_tts_cancellations: HashMap<
        String,
        (
            String,
            String,
            u64,
            voice_desktop_common::client_tts::MiniMaxTtsCancellation,
        ),
    > = HashMap::new();
    let mut client_tts_tasks: HashMap<String, tauri::async_runtime::JoinHandle<()>> =
        HashMap::new();
    let (client_tts_finished_sender, mut client_tts_finished_receiver) =
        tokio::sync::mpsc::unbounded_channel::<String>();
    let mut outcome = Ok(());
    let mut capture_retry_at = Instant::now();
    let mut capture_failure = None::<String>;
    let mut capture_sequence_base = 0_u64;
    let mut capture_sample_base = 0_u64;
    let mut next_capture_sequence = 0_u64;
    let mut next_capture_sample = 0_u64;
    let mut capture_gap_pending = false;

    while outcome.is_ok() && !stop.load(Ordering::Acquire) {
        while let Ok(request_id) = client_tts_finished_receiver.try_recv() {
            if let Some((_, key, _, _)) = client_tts_cancellations.remove(&request_id) {
                if revoked_client_tts_keys.remove(&key) {
                    if let Ok(mut control) = client_tts_playback_control(&state, &tts_identity).await {
                        control.forget_client_tts(&key);
                    }
                }
            }
            if let Some(task) = client_tts_tasks.remove(&request_id) {
                let _ = task.await;
            }
        }
        if let Ok(error) = writer_error_receiver.try_recv() {
            outcome = Err(error);
            break;
        }
        loop {
            match read_receiver.try_recv() {
                Ok(MediaReadEvent::Failed(error)) => {
                    outcome = Err(error);
                    break;
                }
                Ok(MediaReadEvent::Message(SshMediaMessage::Closed)) => {
                    outcome = Err("主机关闭了语音媒体通道。".into());
                    break;
                }
                Ok(MediaReadEvent::Message(SshMediaMessage::Binary(bytes))) => {
                    let stop_epoch = state.remote_playback_stop_epoch.load(Ordering::Acquire);
                    if stop_epoch != playback_stop_epoch {
                        playback_started = false;
                        playback_stop_epoch = stop_epoch;
                    }
                    let mut playback_control = state.remote_playback_control.lock().await;
                    let playback_state = state.clone();
                    let enqueue_state = playback_state.clone();
                    let frame_outcome = process_remote_playback_frame_for_lease(
                        &bytes,
                        &device_id,
                        &core_session_id,
                        &stream_id,
                        connection_generation,
                        executor_generation,
                        interaction_generation,
                        &mut next_playback_sequence,
                        &mut playback_sample_positions,
                        &mut playback_latest_sequences,
                        &mut playback_control,
                        &mut playback_baselines,
                        &mut playback_started,
                        || {
                            let state = playback_state.clone();
                            async move {
                                device_worker_action(state, |worker| {
                                    worker.playback_start().map(|_| ())
                                })
                                .await
                            }
                        },
                        || {
                            let state = playback_state.clone();
                            async move {
                                let value = device_worker_action(state, |worker| {
                                    worker.playback_snapshot()
                                })
                                .await?;
                                decode_worker_playback_snapshot(&value)
                            }
                        },
                        move |playback_key, sample_rate, pcm| {
                            let state = enqueue_state.clone();
                            async move {
                                device_worker_action(state, move |worker| {
                                    worker
                                        .playback_enqueue_for_key(&playback_key, sample_rate, &pcm)
                                        .map(|_| ())
                                })
                                .await
                            }
                        },
                    )
                    .await;
                    let frame_outcome = match frame_outcome {
                        Ok(frame_outcome) => frame_outcome,
                        Err(error) => {
                            outcome = Err(error);
                            break;
                        }
                    };
                    drop(playback_control);
                    match frame_outcome {
                        RemotePlaybackFrameOutcome::Cancelled { message, error } => {
                            if write_sender
                                .send(MediaWriteCommand::Text(message))
                                .await
                                .is_err()
                            {
                                outcome = Err("无法提交设备播放丢弃回执。".into());
                                break;
                            }
                            if let Some(error) = error {
                                let _ = device_worker_action(state.clone(), |worker| worker.playback_stop()).await;
                                playback_started = false;
                                client_event(&state, "client.playback_failed", serde_json::json!({
                                    "device_id":device_id,"stream_id":stream_id,"reason":error,
                                }));
                            }
                            continue;
                        }
                        RemotePlaybackFrameOutcome::Accepted {
                            message,
                            ..
                        } => {
                            if write_sender
                                .send(MediaWriteCommand::Text(message))
                                .await
                                .is_err()
                            {
                                outcome = Err("无法提交设备播放接纳回执。".into());
                                break;
                            }
                        }
                    }
                }
                Ok(MediaReadEvent::Message(SshMediaMessage::Text(mut text))) => {
                    let mut message = match serde_json::from_str::<serde_json::Value>(&text) {
                        Ok(value) => value,
                        Err(_) => {
                            outcome = Err("主机媒体控制消息不是有效 JSON。".into());
                            break;
                        }
                    };
                    match message.get("type").and_then(serde_json::Value::as_str) {
                        Some("clock.sync.request") => {
                            let response = (|| {
                                let received = voice_desktop_common::wasapi_capture::monotonic_device_time_us()?;
                                let sent = voice_desktop_common::wasapi_capture::monotonic_device_time_us()?;
                                voice_desktop_common::ssh_device::media_clock_response(&message, received, sent)
                            })();
                            match response {
                                Ok(response) => {
                                    if write_sender.send(MediaWriteCommand::Text(response)).await.is_err() {
                                        outcome = Err("无法发送媒体时钟续测回执。".into());
                                        break;
                                    }
                                }
                                Err(error) => { outcome = Err(error); break; }
                            }
                        }
                        Some("media.capture_state")
                            if message.get("device_id").and_then(serde_json::Value::as_str)
                                == Some(device_id.as_str())
                                && message.get("stream_id").and_then(serde_json::Value::as_str)
                                    == Some(stream_id.as_str()) =>
                        {
                            let desired = message
                                .get("capture_active")
                                .and_then(serde_json::Value::as_bool);
                            let Some(desired) = desired else {
                                outcome = Err("主机采集状态消息缺少布尔字段。".into());
                                break;
                            };
                            let next_stream_id = message.get("capture_stream_id")
                                .and_then(serde_json::Value::as_str).unwrap_or(&stream_id).to_owned();
                            if next_stream_id.is_empty() || next_stream_id.len() > 128 {
                                outcome = Err("主机输入流编号为空或超过长度上限。".into());
                                break;
                            }
                            let input_changed = next_stream_id != capture_stream_id;
                            if desired != capture_active || input_changed {
                                if capture_started && (!desired || input_changed) {
                                    let _ = device_worker_action(state.clone(), |worker| worker.capture_stop()).await;
                                    capture_started = false;
                                    physical_capture.store(false, Ordering::Release);
                                }
                                if input_changed {
                                    #[cfg(feature = "native-audio")]
                                    if desired && uplink_codec == voice_desktop_common::device_protocol::AudioCodec::Opus {
                                        uplink_encoder = match OpusEncoderWorker::start(encoder_pre_skip_samples) {
                                            Ok(encoder) => Some(encoder),
                                            Err(error) => { outcome = Err(error); break; }
                                        };
                                    }
                                    next_capture_sequence = 0;
                                    next_capture_sample = 0;
                                    capture_gap_pending = false;
                                }
                                audio_sender.send_replace(None);
                                capture_retry_at = Instant::now();
                                capture_failure = None;
                                capture_active = desired;
                                capture_stream_id = next_stream_id;
                                client_event(&state, "client.capture_state", serde_json::json!({
                                    "device_id":device_id,"stream_id":stream_id,"capture_stream_id":capture_stream_id,
                                    "state":if desired {"requested"} else {"stopped"}}));
                            }
                        }
                        Some("media.pong") => {}
                        Some("media.tts.authorize") => {
                            let authorization =
                                client_tts_relay::parse_authorization(&mut message, &tts_identity);
                            voice_desktop_common::client_tts::zeroize_temporary_secret_buffer(
                                &mut text,
                            );
                            let authorization = match authorization {
                                Ok(authorization) => authorization,
                                Err(error) => {
                                    outcome = Err(error);
                                    break;
                                }
                            };
                            let authorization_id = authorization.authorization_id.clone();
                            let playback_key = authorization.playback_key.clone();
                            if let Err(error) = client_tts_authorizations.authorize(authorization) {
                                outcome = Err(error);
                                break;
                            }
                            if let Err(error) = client_tts_relay::send_authorization_accepted(
                                &write_sender,
                                &tts_identity,
                                &authorization_id,
                                &playback_key,
                            )
                            .await
                            {
                                outcome = Err(error);
                                break;
                            }
                        }
                        Some("media.tts.authorization_revoked") => {
                            let (authorization_id, playback_key) =
                                match client_tts_relay::parse_revocation(&message, &tts_identity) {
                                    Ok(value) => value,
                                    Err(error) => {
                                        outcome = Err(error);
                                        break;
                                    }
                                };
                            match client_tts_authorizations.revoke(&authorization_id, &playback_key)
                            {
                                Ok(Some(revoked_key)) => {
                                    let mut active = false;
                                    for (active_authorization, active_key, _, cancellation) in
                                        client_tts_cancellations.values()
                                    {
                                        if active_authorization == &authorization_id
                                            && active_key == &revoked_key
                                        {
                                            cancellation.request_cancel();
                                            active = true;
                                        }
                                    }
                                    if active {
                                        let _ = (client_tts_playback.stop_and_snapshot)(revoked_key.clone()).await;
                                        revoked_client_tts_keys.insert(revoked_key);
                                    } else if let Ok(mut control) = client_tts_playback_control(&state, &tts_identity).await {
                                        control.forget_client_tts(&revoked_key);
                                    }
                                }
                                Ok(None) => {}
                                Err(error) => {
                                    outcome = Err(error);
                                    break;
                                }
                            }
                        }
                        Some("media.tts.request") => {
                            let parsed =
                                match client_tts_relay::parse_request(&message, &tts_identity) {
                                    Ok(parsed) => parsed,
                                    Err(error) => {
                                        outcome = Err(error);
                                        break;
                                    }
                                };
                            let request_id = parsed.request_id.clone();
                            let authorization_id = parsed.authorization_id.clone();
                            let playback_key = parsed.playback_key.clone();
                            let segment_index = parsed.segment_index;
                            client_event(&state, "client.tts_request", serde_json::json!({
                                "device_id":device_id,"stream_id":stream_id,"playback_key":playback_key,
                                "request_id":request_id,"state":"received"}));
                            let failure_request = parsed.clone();
                            if client_tts_tasks.len() >= client_tts_relay::MAX_ACTIVE_REQUESTS {
                                let _ = client_tts_relay::send_start_failure(
                                    &write_sender,
                                    &tts_identity,
                                    &parsed,
                                    "客户端合成并发数已达上限。",
                                )
                                .await;
                                continue;
                            }
                            match client_tts_relay::start_request(
                                parsed,
                                tts_identity.clone(),
                                &mut client_tts_authorizations,
                                client_tts_playback.clone(),
                                write_sender.clone(),
                                client_tts_finished_sender.clone(),
                            )
                            .await
                            {
                                Ok((cancellation, task)) => {
                                    client_tts_cancellations.insert(
                                        request_id.clone(),
                                        (
                                            authorization_id,
                                            playback_key,
                                            segment_index,
                                            cancellation,
                                        ),
                                    );
                                    client_tts_tasks.insert(request_id, task);
                                }
                                Err(_) => {
                                    let _ = client_tts_relay::send_start_failure(
                                        &write_sender,
                                        &tts_identity,
                                        &failure_request,
                                        "客户端 MiniMax 合成任务未能启动。",
                                    )
                                    .await;
                                }
                            }
                        }
                        Some("media.tts.cancel") => {
                            let parsed =
                                match client_tts_relay::parse_cancel(&message, &tts_identity) {
                                    Ok(parsed) => parsed,
                                    Err(error) => {
                                        outcome = Err(error);
                                        break;
                                    }
                                };
                            let request_id = parsed.request_id.clone();
                            if let Some((
                                authorization_id,
                                playback_key,
                                segment_index,
                                cancellation,
                            )) = client_tts_cancellations.get(&request_id)
                            {
                                if authorization_id != &parsed.authorization_id
                                    || playback_key != &parsed.playback_key
                                    || segment_index != &parsed.segment_index
                                {
                                    outcome = Err("客户端取消请求的播放任务编号不匹配。".into());
                                    break;
                                }
                                cancellation.request_cancel();
                                let _ =
                                    (client_tts_playback.stop_and_snapshot)(playback_key.clone())
                                        .await;
                            }
                        }
                        _ => {
                            outcome = Err("主机媒体控制消息类型不受支持。".into());
                            break;
                        }
                    }
                }
                Ok(MediaReadEvent::Message(SshMediaMessage::Ping(_)))
                | Ok(MediaReadEvent::Message(SshMediaMessage::Pong(_))) => {}
                Err(tokio::sync::mpsc::error::TryRecvError::Empty) => break,
                Err(tokio::sync::mpsc::error::TryRecvError::Disconnected) => {
                    outcome = Err("SSH 媒体读取任务已退出。".into());
                    break;
                }
            }
        }
        if outcome.is_err() {
            break;
        }
        let stop_epoch = state.remote_playback_stop_epoch.load(Ordering::Acquire);
        if stop_epoch != playback_stop_epoch {
            playback_started = false;
            playback_stop_epoch = stop_epoch;
        }
        if last_playback_snapshot_at.elapsed() >= Duration::from_millis(80)
            && (next_playback_sequence != last_buffer_report.0
                || last_buffer_report.1 > 0 || !playback_latest_sequences.is_empty())
        {
            let (buffered_ms, paused, progress, failed) = {
                let mut control = state.remote_playback_control.lock().await;
                prune_remote_playback_tracking(
                    &control,
                    &mut playback_sample_positions,
                    &mut playback_latest_sequences,
                    &mut playback_baselines,
                );
                prune_remote_opus_decoder_states(
                    &mut control,
                    &playback_sample_positions,
                    &playback_latest_sequences,
                    &playback_baselines,
                );
                if !playback_started {
                    (0, false, None, None)
                } else {
                    let result = device_worker_action(state.clone(), |worker| worker.playback_snapshot())
                        .await.and_then(|value| decode_worker_playback_snapshot(&value));
                    match result {
                        Ok(snapshot) => {
                            let progress = control.active_key.as_ref().and_then(|key| {
                                let sequence = *playback_latest_sequences.get(key)?;
                                let baseline = *playback_baselines.get(key)?;
                                let (rate, samples) = control.samples.get(key)?;
                                let accepted_ms = samples.saturating_mul(1_000) / u64::from(*rate);
                                let value = remote_playback_progress_value(
                                    &device_id, &core_session_id, &stream_id, connection_generation,
                                    executor_generation, interaction_generation, key, sequence,
                                    playback_receipt_sequence, baseline, snapshot, accepted_ms,
                                );
                                if snapshot.buffered_ms == 0 {
                                    playback_latest_sequences.remove(key);
                                }
                                Some(value)
                            });
                            (snapshot.buffered_ms, control.paused, progress, None)
                        }
                        Err(error) => {
                            playback_started = false;
                            let failed = control.active_key.clone().map(|key| {
                                fail_remote_playback(&mut control, &key);
                                (key, error)
                            });
                            (0, false, None, failed)
                        }
                    }
                }
            };
            let buffer_report = (next_playback_sequence, buffered_ms, paused);
            if buffer_report != last_buffer_report || buffered_ms > 0 {
                let buffer = serde_json::json!({
                    "type":"playback.buffer",
                    "next_sequence":next_playback_sequence, "buffered_ms":buffered_ms, "paused":paused,
                    "fields":{"type":"播放储备回执", "next_sequence":"下一播放帧序号",
                        "buffered_ms":"未输出的内容时长，毫秒", "paused":"设备是否已确认暂停"},
                });
                if write_sender.send(MediaWriteCommand::Text(buffer.to_string())).await.is_err() {
                    outcome = Err("无法提交设备播放储备回执。".into());
                    break;
                }
                last_buffer_report = buffer_report;
            }
            if let Some((key, error)) = failed {
                client_event(&state, "client.playback_failed", serde_json::json!({
                    "device_id":device_id,"stream_id":stream_id,"reason":error,
                }));
                let failure = serde_json::json!({
                    "type":"playback.failed", "playback_key":key,
                    "fields":{"type":"设备播放失败回执", "playback_key":"受影响的播放事务编号"},
                });
                if write_sender.send(MediaWriteCommand::Text(failure.to_string())).await.is_err() {
                    outcome = Err("无法提交设备播放故障回执。".into());
                    break;
                }
            }
            if let Some(progress) = progress {
                if write_sender.send(MediaWriteCommand::Text(progress.to_string())).await.is_err() {
                    outcome = Err("无法提交设备播放时钟回执。".into());
                    break;
                }
                playback_receipt_sequence = playback_receipt_sequence.saturating_add(1);
            }
            last_playback_snapshot_at = Instant::now();
        }
        if outcome.is_err() {
            break;
        }
        if !capture_active {
            tokio::time::sleep(Duration::from_millis(15)).await;
            continue;
        }
        if !capture_started {
            if Instant::now() < capture_retry_at {
                tokio::time::sleep(Duration::from_millis(20)).await;
                continue;
            }
            match device_worker_action(state.clone(), |worker| worker.capture_start().map(|_| ())).await {
                Ok(()) => {
                    capture_started = true;
                    physical_capture.store(true, Ordering::Release);
                    capture_failure = None;
                    capture_sequence_base = next_capture_sequence;
                    capture_sample_base = next_capture_sample;
                    client_event(&state, "client.capture_state", serde_json::json!({
                        "device_id":device_id,"stream_id":stream_id,"capture_stream_id":capture_stream_id,"state":"started"}));
                }
                Err(error) => {
                    if capture_failure.as_ref() != Some(&error) {
                        client_event(&state, "client.capture_unavailable", serde_json::json!({
                            "device_id":device_id,"stream_id":stream_id,"capture_stream_id":capture_stream_id,"reason":error}));
                    }
                    capture_failure = Some(error);
                    capture_retry_at = Instant::now() + Duration::from_secs(1);
                    continue;
                }
            }
        }
        let next_state = state.clone();
        let frame = match device_worker_action(next_state, |worker| worker.capture_next(20)).await {
            Ok(frame) => frame,
            Err(error) => {
                let _ = device_worker_action(state.clone(), |worker| worker.capture_stop()).await;
                physical_capture.store(false, Ordering::Release);
                capture_started = false;
                audio_sender.send_replace(None);
                capture_gap_pending = true;
                capture_retry_at = Instant::now() + Duration::from_secs(1);
                client_event(&state, "client.capture_unavailable", serde_json::json!({
                    "device_id":device_id,"stream_id":stream_id,"capture_stream_id":capture_stream_id,"reason":error}));
                continue;
            }
        };
        let Some(mut frame) = frame else {
            continue;
        };
        if let Some(sequence) = frame["sequence"].as_u64() {
            let sequence = sequence.saturating_add(capture_sequence_base);
            frame["sequence"] = serde_json::json!(sequence);
            next_capture_sequence = sequence.saturating_add(1);
        }
        if let Some(sample) = frame["start_sample"].as_u64() {
            let sample = sample.saturating_add(capture_sample_base);
            frame["start_sample"] = serde_json::json!(sample);
            next_capture_sample = sample.saturating_add(320);
        }
        if std::mem::take(&mut capture_gap_pending) {
            frame["discontinuity_before"] = serde_json::json!(true);
        }
        #[cfg(feature = "native-audio")]
        let binary_result = match uplink_codec {
            voice_desktop_common::device_protocol::AudioCodec::Pcm => encode_worker_media_frame(
                &device_id,
                &core_session_id,
                &capture_stream_id,
                connection_generation,
                executor_generation,
                interaction_generation,
                &frame,
            ),
            voice_desktop_common::device_protocol::AudioCodec::Opus => {
                match decode_worker_capture_pcm(&frame) {
                    Ok((_, _, _, pcm)) => match uplink_encoder.as_ref() {
                        Some(encoder) => match encoder.encode(pcm).await {
                            Ok(packet) => encode_worker_media_packet(
                                &device_id,
                                &core_session_id,
                                &capture_stream_id,
                                connection_generation,
                                executor_generation,
                                interaction_generation,
                                &frame,
                                voice_desktop_common::device_protocol::AudioCodec::Opus,
                                packet,
                            ),
                            Err(error) => Err(error),
                        },
                        None => Err("Opus 媒体通道缺少独占编码工作线程。".into()),
                    },
                    Err(error) => Err(error),
                }
            }
        };
        #[cfg(not(feature = "native-audio"))]
        let binary_result = encode_worker_media_frame(
            &device_id,
            &core_session_id,
            &capture_stream_id,
            connection_generation,
            executor_generation,
            interaction_generation,
            &frame,
        );
        let binary = match binary_result {
            Ok(binary) => binary,
            Err(error) => {
                outcome = Err(error);
                break;
            }
        };
        audio_sender.send_replace(Some(Arc::new(binary)));
    }
    physical_capture.store(false, Ordering::Release);

    if capture_started {
        if let Err(error) =
            device_worker_action(state.clone(), |worker| worker.capture_stop()).await
        {
            if outcome.is_ok() {
                outcome = Err(error);
            }
        }
    }
    for (_, _, _, cancellation) in client_tts_cancellations.values() {
        cancellation.request_cancel();
    }
    let active_client_tts_key = {
        let control = client_tts_playback_control(&state, &tts_identity).await;
        control.ok().and_then(|control| {
            control
                .active_key
                .as_ref()
                .filter(|key| control.client_tts.contains_key(*key))
                .cloned()
        })
    };
    if let Some(key) = active_client_tts_key {
        if let Err(error) = (client_tts_playback.stop_and_snapshot)(key).await {
            if outcome.is_ok() {
                outcome = Err(error);
            }
        }
    } else if playback_started {
        if let Err(error) =
            device_worker_action(state.clone(), |worker| worker.playback_stop()).await
        {
            if outcome.is_ok() {
                outcome = Err(error);
            }
        }
    }
    for (_, mut task) in client_tts_tasks.drain() {
        if tokio::time::timeout(Duration::from_secs(3), &mut task)
            .await
            .is_err()
        {
            task.abort();
            let _ = task.await;
        }
    }
    client_tts_authorizations.clear();
    if let Ok(mut control) = client_tts_playback_control(&state, &tts_identity).await {
        control.client_tts.clear();
        control.client_tts_cancellation = None;
    }
    audio_sender.send_replace(None);
    let _ = write_sender.try_send(MediaWriteCommand::Close);
    drop(write_sender);
    let mut reader_task = reader_task;
    if tokio::time::timeout(Duration::from_secs(2), &mut reader_task)
        .await
        .is_err()
    {
        reader_task.abort();
        let _ = reader_task.await;
    }
    if tokio::time::timeout(Duration::from_secs(2), &mut writer_task).await.is_err() {
        writer_task.abort();
        let _ = writer_task.await;
    }
    if let Ok(Err(error)) = tokio::time::timeout(Duration::from_secs(2), guard.close()).await {
        if outcome.is_ok() {
            outcome = Err(error);
        }
    }
    let final_state = if outcome.is_ok() { "stopped" } else { "error" };
    let error = outcome
        .as_ref()
        .err()
        .map(|value| value.chars().take(240).collect::<String>());
    if let Ok(mut slot) = state.remote_media_channel_error.lock() {
        *slot = error.clone();
    }
    client_event(&state, "client.media_closed", serde_json::json!({
        "device_id":device_id,"stream_id":stream_id,"core_session_id":core_session_id,
        "connection_generation":connection_generation,"executor_generation":executor_generation,
        "interaction_generation":interaction_generation,"state":final_state,"reason":error,
        "duration_ms":media_started_at.elapsed().as_millis() as u64}));
    let _ = app.emit(
        "desktop://remote-media-channel",
        serde_json::json!({
            "state":final_state,"device_id":device_id,"stream_id":stream_id,"error":error,
            "fields":{"state":"受管远端媒体通道状态","device_id":"固定语音设备编号",
                "stream_id":"本次连续音频流编号","error":"最近一次媒体错误"}
        }),
    );
    outcome
}

#[cfg(feature = "ssh-device")]
async fn stop_remote_media_channel(state: &Arc<Desktop>) -> Result<bool, String> {
    let channel = {
        let mut slot = state.remote_media_channel.lock().map_err(|error| error.to_string())?;
        state.remote_media_stop_revision.fetch_add(1, Ordering::AcqRel);
        slot.take()
    };
    let Some(channel) = channel else {
        return Ok(false);
    };
    channel.stop.store(true, Ordering::Release);
    channel.task.await.map_err(|error| error.to_string())?;
    Ok(true)
}

#[cfg(feature = "ssh-device")]
async fn stop_remote_voice_forwarder(state: &Arc<Desktop>) -> Result<bool, String> {
    let forwarder = state
        .remote_voice_forwarder
        .lock()
        .map_err(|_| "远程语音 WebSocket 转发状态不可用。")?
        .take();
    let Some(forwarder) = forwarder else {
        return Ok(false);
    };
    let _ = forwarder.stop.send(true);
    forwarder.task.await.map_err(|error| error.to_string())?;
    Ok(true)
}

#[cfg(feature = "ssh-device")]
async fn stop_remote_device_task_channel(state: &Arc<Desktop>) -> Result<bool, String> {
    let channel = state
        .remote_task_channel
        .lock()
        .map_err(|error| error.to_string())?
        .take();
    let Some(channel) = channel else {
        return Ok(false);
    };
    channel.stop.store(true, Ordering::Release);
    channel.task.await.map_err(|error| error.to_string())?;
    Ok(true)
}

/// remote_client_tts_playback_enabled（远端客户端原生直放能力）：不依赖客户端供应商凭据。
#[cfg(feature = "ssh-device")]
fn remote_client_tts_playback_enabled(
    config: &voice_desktop_common::device_config::DeviceConfig, // config（客户端设备配置，读取本地合成播放开关）。
) -> bool {
    // Windows（视窗操作系统）构建包含远程媒体工作器使用的原生 PCM/WASAPI 播放器。
    config.client_online_tts_local && cfg!(windows)
}

/// device_ssh_task_channel（远端桌面任务通道）：仅启动本机受管辅助执行，不载入 Python。
#[tauri::command]
async fn device_ssh_task_channel(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    action: String, // action（start、status 或 stop）。
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序可以管理远端桌面任务通道。".into());
    }
    #[cfg(feature = "ssh-device")]
    {
        let desktop = state.inner().clone();
        match action.as_str() {
            "status" => {
                let channel = desktop
                    .remote_task_channel
                    .lock()
                    .map_err(|error| error.to_string())?;
                let error = desktop
                    .remote_task_channel_error
                    .lock()
                    .map_err(|error| error.to_string())?;
                return Ok(remote_device_task_status(
                    channel.as_ref(),
                    error.as_deref(),
                ));
            }
            "stop" => {
                let stopped = stop_remote_device_task_channel(&desktop).await?;
                return Ok(serde_json::json!({"state":"stopped","stopped":stopped,
                    "fields":{"state":"远端设备任务通道状态","stopped":"本次是否停止了受管任务轮询"}}));
            }
            "start" => {}
            _ => return Err("远端设备任务通道操作必须为 start、status 或 stop。".into()),
        }
        if desktop.stopping.load(Ordering::Acquire) {
            return Err("应用正在退出。".into());
        }
        if !desktop.ssh_device.connected().await {
            return Err("请先连接并认证共享主机。".into());
        }
        {
            let channel = desktop
                .remote_task_channel
                .lock()
                .map_err(|error| error.to_string())?;
            if let Some(active) = channel
                .as_ref()
                .filter(|channel| channel.running.load(Ordering::Acquire))
            {
                return Ok(remote_device_task_status(Some(active), None));
            }
        }
        if desktop
            .remote_task_channel_starting
            .swap(true, Ordering::AcqRel)
        {
            return Err("远端设备任务通道正在建立。".into());
        }
        let _starting = ResetFlag(&desktop.remote_task_channel_starting);
        let context = desktop.context().ok_or("缺少正式工作区")?;
        let receipt_journal = Arc::new(
            voice_desktop_common::device_receipts::DeviceTaskReceiptJournal::new(
                context.data.clone(),
            ),
        );
        let config = match voice_desktop_common::device_config::load_from_root(&context.data) {
            voice_desktop_common::device_config::LoadResult::Valid(config) => config,
            voice_desktop_common::device_config::LoadResult::Missing => {
                return Err("尚未保存设备角色配置。".into());
            }
            voice_desktop_common::device_config::LoadResult::Invalid(error) => return Err(error),
        };
        if config.desired_role != voice_desktop_common::device_config::Role::RemoteClient {
            return Err("只有远端客户端角色可以开启桌面任务通道。".into());
        }
        let desktop_control = ensure_remote_desktop_helper(desktop.clone())
            .await
            .map_err(|error| error.chars().take(240).collect::<String>());
        let mut capabilities = vec![
            "audio_stream".to_owned(),
            "file_transfer".to_owned(),
            "terminal_control".to_owned(),
        ];
        if desktop_control.is_ok() {
            capabilities.push("desktop_control".to_owned());
        }
        if voice_desktop_common::native_screen::interactive_session() {
            capabilities.push("screen_capture".to_owned());
        }
        // client_tts（客户端原生直放能力）由开关和原生播放器支持决定；供应商凭据由服务端在线语音设置提供。
        if remote_client_tts_playback_enabled(&config) {
            capabilities.push("client_tts".to_owned());
        }
        capabilities.sort();
        let lease = desktop
            .ssh_device
            .open_remote_device_session_with_capabilities(
                capabilities,
                std::time::Duration::from_secs(10),
            )
            .await?;
        let connection_generation = lease.connection_generation;
        let executor_generation = lease.executor_generation;
        *desktop
            .remote_task_channel_error
            .lock()
            .map_err(|error| error.to_string())? = None;
        let stop = Arc::new(AtomicBool::new(false));
        let task_stop = stop.clone();
        let running = Arc::new(AtomicBool::new(true));
        let task_running = running.clone();
        let task_state = desktop.clone();
        let task_receipt_journal = receipt_journal.clone();
        let task_app = window.app_handle().clone();
        let loop_handle = tauri::async_runtime::spawn(async move {
            let executor_state = task_state.clone();
            let outcome = task_state
                .ssh_device
                .run_remote_device_task_channel_for_lease_with_receipts(
                    lease,
                    task_stop.clone(),
                    &task_receipt_journal,
                    move |task| {
                        let state = executor_state.clone();
                        async move { execute_remote_device_task(state, task).await }
                    },
                )
                .await;
            task_stop.store(true, Ordering::Release);
            task_running.store(false, Ordering::Release);
            let error = outcome
                .err()
                .map(|error| error.chars().take(240).collect::<String>());
            if let Ok(mut slot) = task_state.remote_task_channel_error.lock() {
                *slot = error.clone();
            }
            let state_name = if error.is_some() { "error" } else { "stopped" };
            let _ = task_app.emit(
                "desktop://remote-device-task-channel",
                serde_json::json!({
                    "state":state_name,"error":error,
                    "fields":{"state":"受管远端桌面任务通道状态","error":"最近一次通道错误"}
                }),
            );
        });
        let channel = RemoteDeviceTaskChannel {
            stop,
            running,
            device_id: config.device_id,
            connection_generation,
            executor_generation,
            desktop_control,
            task: loop_handle,
        };
        let response = remote_device_task_status(Some(&channel), None);
        *desktop
            .remote_task_channel
            .lock()
            .map_err(|error| error.to_string())? = Some(channel);
        Ok(response)
    }
    #[cfg(not(feature = "ssh-device"))]
    {
        let _ = action;
        Err("当前产品构建未包含 SSH 设备任务通道能力。".into())
    }
}

#[cfg(feature = "ssh-device")]
async fn abort_remote_interaction_switch(state: &Arc<Desktop>, generation: u64) {
    let body = serde_json::json!({"protocol_version":1,"interaction_generation":generation});
    let _ = state
        .ssh_device
        .post_json("/v1/interaction/abort", &body, Duration::from_secs(10))
        .await;
}

/// device_ssh_media_channel（远端语音媒体通道）：显式准备交互租约后开启独立 SSH WebSocket。
#[cfg(feature = "ssh-device")]
#[tauri::command]
async fn device_ssh_media_channel(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    action: String, // action（start、status 或 stop）。
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product {
        return Err("只有正式程序可以管理远端语音媒体通道。".into());
    }
    let desktop = state.inner().clone();
    let stop_revision = desktop.remote_media_stop_revision.load(Ordering::Acquire);
    match action.as_str() {
        "status" => {
            let slot = desktop
                .remote_media_channel
                .lock()
                .map_err(|error| error.to_string())?;
            let running = slot
                .as_ref()
                .is_some_and(|channel| channel.running.load(Ordering::Acquire));
            let device_id = slot.as_ref().map(|channel| channel.device_id.clone());
            let stream_id = slot.as_ref().map(|channel| channel.stream_id.clone());
            let connection_generation = slot.as_ref().map(|channel| channel.connection_generation);
            let executor_generation = slot.as_ref().map(|channel| channel.executor_generation);
            let interaction_generation =
                slot.as_ref().map(|channel| channel.interaction_generation);
            let capture_active = slot.as_ref()
                .is_some_and(|channel| channel.capture_active.load(Ordering::Acquire));
            let error = desktop
                .remote_media_channel_error
                .lock()
                .map_err(|error| error.to_string())?
                .clone();
            return Ok(
                serde_json::json!({"state":if running{"running"}else if error.is_some(){"error"}else{"stopped"},
                "device_id":device_id,"stream_id":stream_id,"connection_generation":connection_generation,
                "executor_generation":executor_generation,"interaction_generation":interaction_generation,
                "capture_active":capture_active,"error":error,
                "fields":{"state":"受管远端语音媒体通道状态","device_id":"当前远端语音设备编号",
                    "stream_id":"当前连续音频流编号","connection_generation":"主机授予的设备连接代次",
                    "executor_generation":"主机授予的设备执行器代次","interaction_generation":"主机授予的语音交互代次",
                    "capture_active":"本机麦克风已实际打开","error":"最近一次媒体通道错误"}}),
            );
        }
        "stop" => {
            let stopped = stop_remote_media_channel(&desktop).await?;
            return Ok(serde_json::json!({"state":"stopped","stopped":stopped,
                "fields":{"state":"远端语音媒体通道状态","stopped":"本次是否完成媒体通道停止与资源回收"}}));
        }
        "start" => {}
        _ => return Err("远端语音媒体通道操作必须为 start、status 或 stop。".into()),
    }
    if desktop.stopping.load(Ordering::Acquire) {
        return Err("应用正在退出。".into());
    }
    if !desktop.ssh_device.connected().await {
        return Err("请先连接并认证共享主机。".into());
    }
    if desktop
        .remote_media_channel_starting
        .swap(true, Ordering::AcqRel)
    {
        return Err("远端语音媒体通道正在建立。".into());
    }
    let _starting = ResetFlag(&desktop.remote_media_channel_starting);
    {
        let slot = desktop.remote_media_channel.lock().map_err(|error| error.to_string())?;
        if let Some(channel) = slot.as_ref().filter(|channel| channel.running.load(Ordering::Acquire)) {
            return Ok(serde_json::json!({"state":"running","device_id":channel.device_id,
                "stream_id":channel.stream_id,"connection_generation":channel.connection_generation,
                "executor_generation":channel.executor_generation,"interaction_generation":channel.interaction_generation,
                "capture_active":channel.capture_active.load(Ordering::Acquire),
                "fields":{"state":"受管远端语音媒体通道状态","device_id":"本机稳定设备编号","stream_id":"媒体连接编号",
                    "connection_generation":"设备连接代次","executor_generation":"设备执行器代次",
                    "interaction_generation":"语音占用代次","capture_active":"本机麦克风已实际打开"}}));
        }
    }
    let task_connection = desktop.remote_task_channel.lock().map_err(|error| error.to_string())?
        .as_ref().filter(|channel| channel.running.load(Ordering::Acquire))
        .map(|channel| channel.connection_generation)
        .ok_or("请先启动已登记音频流能力的远端设备任务会话。")?;
    let context = desktop.context().ok_or("缺少正式工作区")?;
    let config = match voice_desktop_common::device_config::load_from_root(&context.data) {
        voice_desktop_common::device_config::LoadResult::Valid(config) => config,
        voice_desktop_common::device_config::LoadResult::Missing => {
            return Err("尚未保存设备角色配置。".into());
        }
        voice_desktop_common::device_config::LoadResult::Invalid(error) => return Err(error),
    };
    if config.desired_role != voice_desktop_common::device_config::Role::RemoteClient {
        return Err("只有远端客户端角色可以连接语音媒体流。".into());
    }
    let (uplink_codec, downlink_codec, encoder_pre_skip_samples, downlink_encoder_pre_skip_samples) =
        match config.audio_mode {
            voice_desktop_common::device_config::AudioMode::Pcm => (
                voice_desktop_common::device_protocol::AudioCodec::Pcm,
                voice_desktop_common::device_protocol::AudioCodec::Pcm,
                0,
                0,
            ),
            voice_desktop_common::device_config::AudioMode::OpusExperimental => {
                #[cfg(feature = "native-audio")]
                {
                    let uplink_encoder =
                        voice_desktop_common::native_opus::OpusEncoder::new(16_000, None)?;
                    let downlink_encoder =
                        voice_desktop_common::native_opus::OpusEncoder::new(48_000, None)?;
                    (
                        voice_desktop_common::device_protocol::AudioCodec::Opus,
                        voice_desktop_common::device_protocol::AudioCodec::Opus,
                        uplink_encoder.lookahead_samples(),
                        downlink_encoder.lookahead_samples(),
                    )
                }
                #[cfg(not(feature = "native-audio"))]
                {
                    return Err("当前产品构建没有原生 Opus 编解码能力；不会自动降级为 PCM。".into());
                }
            }
        };
    let device_id = config.device_id.clone();
    let prepare_body = serde_json::json!({"protocol_version":1, "connection_generation":task_connection});
    let prepared = desktop
        .ssh_device
        .post_json(
            "/v1/interaction/prepare",
            &prepare_body,
            Duration::from_secs(15),
        )
        .await?;
    if prepared.status != 200 {
        return Err("共享主机拒绝语音设备切换；请刷新设备状态后重试。".into());
    }
    let lease = prepared.body;
    if lease["result"] == "standby" {
        return Ok(serde_json::json!({"state":"standby",
            "fields":{"state":"已连接，等待服务端选择本设备作为语音输入"}}));
    }
    let interaction_generation = lease
        .get("interaction_generation")
        .and_then(serde_json::Value::as_u64)
        .ok_or("语音交互租约缺少占用代次；主机切换事务将于 60 秒后自动失效。")?;
    let lease_fields = (|| -> Result<(String, u64, u64), String> {
        if lease.get("device_id").and_then(serde_json::Value::as_str) != Some(device_id.as_str()) {
            return Err("共享主机返回的语音交互租约与本机不匹配。".into());
        }
        let core_session_id = lease
            .get("core_session_id")
            .and_then(serde_json::Value::as_str)
            .ok_or("语音交互租约缺少权威核心运行编号。")?
            .to_owned();
        let connection_generation = lease
            .get("connection_generation")
            .and_then(serde_json::Value::as_u64)
            .ok_or("语音交互租约缺少连接代次。")?;
        let executor_generation = lease
            .get("executor_generation")
            .and_then(serde_json::Value::as_u64)
            .ok_or("语音交互租约缺少执行器代次。")?;
        if connection_generation == 0 || executor_generation == 0 || interaction_generation == 0 {
            return Err("共享主机返回了零值设备代次。".into());
        }
        Ok((core_session_id, connection_generation, executor_generation))
    })();
    let (core_session_id, connection_generation, executor_generation) = match lease_fields {
        Ok(fields) => fields,
        Err(error) => {
            abort_remote_interaction_switch(&desktop, interaction_generation).await;
            return Err(error);
        }
    };
    let stream_id = match std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH) {
        Ok(now) => format!("media-{}-{interaction_generation}", now.as_millis()),
        Err(_) => {
            abort_remote_interaction_switch(&desktop, interaction_generation).await;
            return Err("系统时钟早于 Unix 纪元，语音切换已放弃。".into());
        }
    };

    let worker_state = desktop.clone();
    let worker_start = tauri::async_runtime::spawn_blocking(move || {
        let mut slot = worker_state
            .device_worker
            .lock()
            .map_err(|error| error.to_string())?;
        if slot.is_none() {
            *slot = Some(voice_desktop_common::device_worker::ManagedWorker::start()?);
        }
        Ok::<(), String>(()) // 工作进程只在此预热；媒体计时不再以额外 IPC（进程通信）读数作准入条件。
    })
    .await;
    let worker_start = match worker_start {
        Ok(result) => result,
        Err(error) => Err(error.to_string()),
    };
    if let Err(error) = worker_start {
        abort_remote_interaction_switch(&desktop, interaction_generation).await;
        return Err(error);
    }

    client_event(&desktop, "client.media_opening", serde_json::json!({"device_id":config.device_id,
        "stream_id":stream_id,"core_session_id":core_session_id,"connection_generation":connection_generation,
        "executor_generation":executor_generation,"interaction_generation":interaction_generation}));
    let mut media = match desktop
        .ssh_device
        .open_media_channel_with_codecs(
            &stream_id,
            connection_generation,
            executor_generation,
            interaction_generation,
            uplink_codec,
            downlink_codec,
            encoder_pre_skip_samples,
            downlink_encoder_pre_skip_samples,
            Duration::from_secs(15),
        )
        .await
    {
        Ok(media) => media,
        Err(error) => {
            client_event(&desktop, "client.media_failed", serde_json::json!({"device_id":config.device_id,
                "stream_id":stream_id,"reason":error,"state":"opening"}));
            abort_remote_interaction_switch(&desktop, interaction_generation).await;
            return Err(error);
        }
    };
    if media.core_session_id != core_session_id {
        let _ = media.close().await;
        abort_remote_interaction_switch(&desktop, interaction_generation).await;
        return Err("共享主机在交互租约准备后重启；拒绝跨核心代次建立媒体流。".into());
    }
    let synchronized = tokio::time::timeout(
        Duration::from_secs(4),
        media.synchronize_clock(|| async { voice_desktop_common::wasapi_capture::monotonic_device_time_us() }),
    ).await.unwrap_or_else(|_| Err("远端语音媒体时钟握手超时。".into()));
    if let Err(error) = synchronized {
        client_event(&desktop, "client.media_failed", serde_json::json!({"device_id":config.device_id,
            "stream_id":stream_id,"reason":error,"state":"clock_sync"}));
        let _ = media.close().await;
        abort_remote_interaction_switch(&desktop, interaction_generation).await;
        return Err(error);
    }
    let stop = Arc::new(AtomicBool::new(false));
    let running = Arc::new(AtomicBool::new(true));
    let capture_active = Arc::new(AtomicBool::new(false));
    let mut pending_media = Some(media);
    {
        let mut slot = desktop.remote_media_channel.lock().map_err(|error| error.to_string())?;
        if desktop.remote_media_stop_revision.load(Ordering::Acquire) == stop_revision {
            *desktop.remote_media_channel_error.lock().map_err(|error| error.to_string())? = None;
            let task_stop = stop.clone();
            let task_running = running.clone();
            let task_capture = capture_active.clone();
            let task_state = desktop.clone();
            let task_app = window.app_handle().clone();
            let media = pending_media.take().expect("media pending");
            let task = tauri::async_runtime::spawn(async move {
                let outcome = run_remote_media_channel(
                    task_state.clone(), media, task_stop.clone(), task_capture, task_app,
                ).await;
                task_stop.store(true, Ordering::Release);
                task_running.store(false, Ordering::Release);
                if let Err(error) = outcome {
                    if let Ok(mut slot) = task_state.remote_media_channel_error.lock() {
                        *slot = Some(error.chars().take(240).collect());
                    }
                }
            });
            *slot = Some(RemoteDeviceMediaChannel {
                stop, running, capture_active: capture_active.clone(),
                device_id: device_id.clone(), stream_id: stream_id.clone(),
                connection_generation, executor_generation, interaction_generation, task,
            });
        }
    }
    if let Some(media) = pending_media {
        let _ = tokio::time::timeout(Duration::from_secs(2), media.close()).await;
        abort_remote_interaction_switch(&desktop, interaction_generation).await;
        return Err("远端媒体启动已取消。".into());
    }
    Ok(
        serde_json::json!({"state":"running","device_id":device_id,"stream_id":stream_id,
        "connection_generation":connection_generation,"executor_generation":executor_generation,
        "interaction_generation":interaction_generation,
        "capture_active":capture_active.load(Ordering::Acquire),
        "fields":{"state":"受管远端语音媒体通道状态","device_id":"本机稳定设备编号",
            "stream_id":"本次连续音频流编号","connection_generation":"设备连接代次",
            "executor_generation":"设备执行器代次","interaction_generation":"语音占用代次",
            "capture_active":"本机麦克风已实际打开"}}),
    )
}

#[cfg(not(feature = "ssh-device"))]
#[tauri::command]
async fn device_ssh_media_channel(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    action: String, // action（start、status 或 stop）。
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    let _ = action;
    Err("当前产品构建未包含 SSH 远端语音媒体通道能力。".into())
}

/// device_ssh_voice_forwarder（本机回环转发服务端语音事件）：随机路径仅在已配对客户端内存中返回。
#[cfg(feature = "ssh-device")]
#[tauri::command]
async fn device_ssh_voice_forwarder(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    action: String, // action（start、status 或 stop）。
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    if state.mode != FrontendMode::Product || state.stopping.load(Ordering::Acquire) {
        return Err("当前窗口或应用状态不能管理远程语音转发。".into());
    }
    let desktop = state.inner().clone();
    match action.as_str() {
        "status" => {
            let slot = desktop
                .remote_voice_forwarder
                .lock()
                .map_err(|_| "远程语音转发状态不可用。")?;
            let running = slot
                .as_ref()
                .is_some_and(|forwarder| forwarder.running.load(Ordering::Acquire));
            let url = slot
                .as_ref()
                .filter(|forwarder| forwarder.running.load(Ordering::Acquire))
                .map(|forwarder| forwarder.url.clone());
            return Ok(serde_json::json!({
                "state":if running {"running"} else {"stopped"},
                "url":url,
                "fields":{"state":"服务端语音 WebSocket 本机回环转发状态",
                    "url":"仅当前已配对客户端内存可见、带随机路径票据的本机地址"}
            }));
        }
        "stop" => {
            let stopped = stop_remote_voice_forwarder(&desktop).await?;
            return Ok(serde_json::json!({"state":"stopped","stopped":stopped,
                "fields":{"state":"服务端语音 WebSocket 本机回环转发状态",
                    "stopped":"本次是否关闭了本机转发 listener 与活动 SSH 桥接"}}));
        }
        "start" => {}
        _ => return Err("语音转发操作必须为 start、status 或 stop。".into()),
    }
    if !desktop.ssh_device.connected().await {
        return Err("请先连接并认证共享主机。".into());
    }
    let context = desktop.context().ok_or("缺少正式工作区")?;
    let _config = match voice_desktop_common::device_config::load_from_root(&context.data) {
        voice_desktop_common::device_config::LoadResult::Valid(config)
            if config.mode == voice_desktop_common::device_config::RunMode::Remote
                && config
                    .host
                    .as_ref()
                    .is_some_and(|host| host.host_id.is_some()) =>
        {
            config
        }
        voice_desktop_common::device_config::LoadResult::Valid(_) => {
            return Err("语音转发只允许已配对的远程客户端模式。".into());
        }
        voice_desktop_common::device_config::LoadResult::Missing => {
            return Err("尚未保存远程配对配置。".into())
        }
        voice_desktop_common::device_config::LoadResult::Invalid(error) => return Err(error),
    };
    {
        let mut slot = desktop
            .remote_voice_forwarder
            .lock()
            .map_err(|_| "远程语音转发状态不可用。")?;
        if let Some(forwarder) = slot
            .as_ref()
            .filter(|forwarder| forwarder.running.load(Ordering::Acquire))
        {
            return Ok(
                serde_json::json!({"state":"running","url":forwarder.url.clone(),
                "fields":{"state":"服务端语音 WebSocket 本机回环转发状态",
                    "url":"仅当前已配对客户端内存可见、带随机路径票据的本机地址"}}),
            );
        }
        if slot.is_some() {
            slot.take();
        }

        let listener = std::net::TcpListener::bind(("127.0.0.1", 0))
            .map_err(|error| format!("无法建立本机语音 WebSocket 回环 listener：{error}"))?;
        listener
            .set_nonblocking(true)
            .map_err(|_| "无法配置本机语音 WebSocket 回环 listener。")?;
        let listener = tokio::net::TcpListener::from_std(listener)
            .map_err(|error| format!("无法接管本机语音 WebSocket 回环 listener：{error}"))?;
        let address = listener
            .local_addr()
            .map_err(|_| "无法读取本机语音 WebSocket 回环地址。")?;
        if !address.ip().is_loopback() {
            return Err("语音 WebSocket 转发意外绑定到非回环地址。".into());
        }
        let token = rand::random::<[u8; 32]>()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        let url = format!("ws://{address}/{token}/ws");
        let (stop, mut stop_receiver) = tokio::sync::watch::channel(false);
        let task_state = desktop.clone();
        let listener_token = token;
        let running = Arc::new(AtomicBool::new(true));
        let task_running = running.clone();
        let task = tauri::async_runtime::spawn(async move {
            let slots = Arc::new(tokio::sync::Semaphore::new(2));
            loop {
                tokio::select! {
                    changed = stop_receiver.changed() => {
                        if changed.is_err() || *stop_receiver.borrow() { break; }
                    }
                    accepted = listener.accept() => {
                        let Ok((stream, peer)) = accepted else { continue };
                        if !peer.ip().is_loopback() { continue; }
                        let Ok(slot) = slots.clone().try_acquire_owned() else { continue };
                        let task_state = task_state.clone();
                        let token = listener_token.clone();
                        let stop = stop_receiver.clone();
                        tauri::async_runtime::spawn(async move {
                            let _slot = slot;
                            let _ = task_state.ssh_device.relay_voice_websocket(stream, &token, stop).await;
                        });
                    }
                }
            }
            task_running.store(false, Ordering::Release);
        });
        *slot = Some(RemoteVoiceForwarder {
            url: url.clone(),
            running,
            stop,
            task,
        });
        Ok(serde_json::json!({"state":"running","url":url,
            "fields":{"state":"服务端语音 WebSocket 本机回环转发状态",
                "url":"仅当前已配对客户端内存可见、带随机路径票据的本机地址"}}))
    }
}

#[cfg(not(feature = "ssh-device"))]
#[tauri::command]
async fn device_ssh_voice_forwarder(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    action: String, // action（start、status 或 stop）。
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    let _ = action;
    Err("当前产品构建未包含 SSH 语音 WebSocket 转发能力。".into())
}

// desktop_page_ready（真实页面就绪）：界面挂载后确认；迁移不使用缓存启动状态。
#[tauri::command]
fn desktop_page_ready(
    window: WebviewWindow,
    state: tauri::State<Arc<Desktop>>,
) -> Result<(), String> {
    trusted(&window, &state)?;
    require_command(state.mode, DesktopCommand::FocusMain)?;
    let url = window.url().map_err(|e| e.to_string())?;
    let management = url.path() == "/management.html" && boot_page_url(&url);
    let context = state.context().ok_or("缺少正式工作区")?;
    let remote_static_product = remote_static_product_page_url(&url)
        && voice_desktop_common::device_config::inspect(&context.data).mode
            == Some(voice_desktop_common::device_config::RunMode::Remote);
    if !management
        && !remote_static_product
        && !(state.is_settled_frontend_url(&url) && state.allows_frontend_url(&url))
    {
        return Err("页面尚未进入管理界面或产品界面。".into());
    }
    workspace::atomic_json(
        &context.data.join("state/workspace-ready.json"),
        &serde_json::json!({"schema":1,"pid":std::process::id(),"workspace_id":context.workspace_id,"role":"product","ready":true,"management_ready":management,"development":context.dev,"fields":{"schema":"页面就绪记录格式","pid":"桌面进程编号","workspace_id":"工作区编号","role":"product（正式程序）","ready":"界面已真实挂载","management_ready":"组件管理页已挂载","development":"显式源码调试"}}),
    )?;
    if management {
        if let Some(manager) = window.app_handle().try_state::<Arc<ResourceManager>>() {
            if let Err(error) = manager.inner().resume_maintenance() {
                // 页面已经真实挂载；任务准入失败保留交接并显示原因，不能让前端卸载已确认的管理界面。
                *state.error.lock().unwrap() = Some(format!(
                    "组件维护任务未能启动，交接记录已保留，可在组件列表重试：{error}"
                ));
            }
        }
    }
    window.show().map_err(|error| error.to_string())?;
    // 新界面完成挂载及绘制后才撤走临时启动窗口，加载失败时旧画面仍可见。
    if window.label() == "main" {
        if let Some(startup) = window.app_handle().get_webview_window("startup") {
            startup.destroy().map_err(|error| error.to_string())?;
            let cache = state.data.clone();
            std::thread::spawn(move || cleanup_initialization_cache(&cache));
        }
    }
    Ok(())
}
#[tauri::command]
fn skip_startup(window: WebviewWindow, state: tauri::State<Arc<Desktop>>) -> Result<(), String> {
    require_command(state.mode, DesktopCommand::SkipStartup)?;
    trusted(&window, &state)?;
    if state.stopping.load(Ordering::Acquire) {
        return Err("应用正在退出。".into());
    }
    state.startup_skipped.store(true, Ordering::Release);
    Ok(())
}
#[tauri::command]
async fn export_logs(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
) -> Result<Option<String>, String> {
    require_command(state.mode, DesktopCommand::ExportLogs)?;
    trusted(&window, &state)?;
    if state.stopping.load(Ordering::Acquire) {
        return Err("应用正在退出。".into());
    }
    if state.workspace_busy.load(Ordering::Acquire) {
        return Err("工作区正在切换，请稍后打包日志。".into());
    }
    if state.log_export_busy.swap(true, Ordering::AcqRel) {
        return Err("日志正在打包，请稍候。".into());
    }
    let _reset = ResetFlag(&state.log_export_busy);
    let operation = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        // service fallback（服务回退）：后端已退出时仍继续同步并导出磁盘上的日志。
        let _ = operation.flush_service_logs();
        let Some(destination) =
            voice_desktop_common::native_ui::choose_folder("选择日志压缩包保存文件夹")?
        else {
            return Ok(None);
        };
        let context = operation.context().ok_or("缺少产品工作区")?;
        let archive = voice_desktop_common::logs::archive_logs(
            &context.install.join("cache/logs"),
            &destination,
        )?;
        Ok(Some(archive.to_string_lossy().into_owned()))
    })
    .await
    .map_err(|error| error.to_string())?
}
#[tauri::command]
fn focus_main(window: WebviewWindow, state: tauri::State<Arc<Desktop>>) -> Result<(), String> {
    require_command(state.mode, DesktopCommand::FocusMain)?;
    trusted(&window, &state)?;
    show(&window.app_handle());
    Ok(())
}
#[tauri::command]
fn exit_application(
    window: WebviewWindow,
    app: tauri::AppHandle,
    state: tauri::State<Arc<Desktop>>,
) -> Result<(), String> {
    require_command(state.mode, DesktopCommand::Exit)?;
    trusted(&window, &state)?;
    state.inner().shutdown(&app);
    Ok(())
}
#[tauri::command]
fn open_console(
    window: WebviewWindow,
    state: tauri::State<Arc<Desktop>>,
    url: String,
) -> Result<(), String> {
    require_command(state.mode, DesktopCommand::OpenConsole)?;
    trusted(&window, &state)?;
    let url = console_url(&url)?;
    if state.dsh_origin.lock().unwrap().as_deref()
        != Some(url.origin().ascii_serialization().as_str())
    {
        return Err("控制台端口不属于当前工作区的受管 DSH（代理宿主）。".into());
    }
    // 通过系统默认浏览器打开 DSH（代理执行宿主）网页，使用完整令牌地址登录。
    voice_desktop_common::windows::open_url(url.as_str())
}

// console_url（控制台地址校验）：限制桌面入口只能打开当前本机代理控制台。
fn console_url(value: &str) -> Result<tauri::Url, String> {
    let url: tauri::Url = value.parse().map_err(|_| "控制台地址无效")?;
    // 服务返回的短期令牌地址允许动态分配的本机 DSH（代理执行宿主）端口。
    let query: Vec<_> = url.query_pairs().collect();
    if url.scheme() != "http"
        || url.host_str() != Some("127.0.0.1")
        || url.port().filter(|v| *v > 0).is_none()
        || url.path() != "/"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
        || query.len() != 1
        || query[0].0 != "token"
        || query[0].1.is_empty()
    {
        return Err("控制台地址不符合本机入口约束。".into());
    }
    Ok(url)
}
#[tauri::command]
fn workspace_info(
    window: WebviewWindow,
    state: tauri::State<Arc<Desktop>>,
) -> Result<serde_json::Value, String> {
    require_command(state.mode, DesktopCommand::FocusMain)?;
    trusted(&window, &state)?;
    let context = state.context().ok_or("缺少产品工作区")?;
    let allow_external_debug = voice_desktop_common::agent_debug::allowed(&context.data).unwrap_or(false);
    Ok(
        serde_json::json!({"root":context.install,"data_dir":context.data,
        "version":env!("CARGO_PKG_VERSION"),"can_manage":true,"allow_external_debug":allow_external_debug,
        "fields":{"root":"当前工作区根目录","data_dir":"用户数据目录","version":"程序版本","can_manage":"允许管理工作区","allow_external_debug":"允许外部调试"}}),
    )
}
#[tauri::command]
fn agent_debug_set_allowed(
    window: WebviewWindow,
    state: tauri::State<Arc<Desktop>>,
    allowed: bool,
) -> Result<(), String> {
    agent_debug::set_allowed(window, state, allowed)
}
fn require_workspace(window: &WebviewWindow, state: &Desktop) -> Result<(), String> {
    trusted(window, state)?;
    require_command(state.mode, DesktopCommand::FocusMain)?;
    require_local_device_target(state)?;
    if state.stopping.load(Ordering::Acquire) {
        return Err("应用正在退出。".into());
    }
    Ok(())
}

/// require_local_device_target（本机目标门禁）：远程配对窗口不得借本机工作区接口修改客户端。
fn require_local_device_target(state: &Desktop) -> Result<(), String> {
    let context = state.context().ok_or("缺少工作区上下文。")?;
    require_local_device_root(&context.data)
}

/// require_local_device_root（设备数据目录本地模式门禁）：未知配置拒绝本机变更。
fn require_local_device_root(data_root: &Path) -> Result<(), String> {
    let snapshot = voice_desktop_common::device_config::inspect(data_root);
    if let Some(error) = snapshot.error {
        return Err(error);
    }
    if snapshot.mode == Some(voice_desktop_common::device_config::RunMode::Remote) {
        return Err("此操作只针对本机运行设备；远程配对时请通过服务端管理操作。".into());
    }
    Ok(())
}
#[tauri::command]
async fn workspace_choose(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
) -> Result<Option<serde_json::Value>, String> {
    require_workspace(&window, &state)?;
    let locator = state.locator.clone().ok_or("缺少本地工作区身份")?;
    tauri::async_runtime::spawn_blocking(move || {
        let chosen = voice_desktop_common::native_ui::choose_folder(
            "选择工作区（新工作区须为空，可使用中文文件夹）",
        )?;
        if let Some(root) = &chosen {
            locator.validate_target(root)?;
        }
        Ok(chosen.map(|root| serde_json::json!({"root":root})))
    })
    .await
    .map_err(|e| e.to_string())?
}
async fn workspace_operation(
    window: WebviewWindow,
    state: Arc<Desktop>,
    app: tauri::AppHandle,
    target_root: String,
    kind: &'static str,
) -> Result<serde_json::Value, String> {
    require_workspace(&window, &state)?;
    reserve_maintenance(
        &state.maintenance_gate,
        &state.workspace_busy,
        &state.app_update.busy,
        false,
        Some(&state.device_operation),
    )?;
    let operation_state = state.clone();
    let recovery_app = app.clone(); // recovery_app（失败时重新开放资源任务）。
    let result=tauri::async_runtime::spawn_blocking(move||{
        // workspace_busy（迁移门闩）已关闭新保存；等待此前正在写入的接口/语音配置完成。
        drop(operation_state.credentials_guard.lock().map_err(|_|"配置写入锁不可用。")?);
        let context=operation_state.context().ok_or("缺少工作区上下文")?;
        let locator=operation_state.locator.as_ref().ok_or("缺少本地工作区身份")?;
        let target=PathBuf::from(target_root);locator.validate_target(&target)?;
        if let Some(manager)=app.try_state::<Arc<ResourceManager>>(){manager.pause_and_wait()?;}
        let prepared=workspace::prepare_task(&context.install,&target,kind,std::process::id(),locator);
        let journal=prepared?;
        let record:serde_json::Value=serde_json::from_slice(&fs::read(&journal).map_err(|e|e.to_string())?).map_err(|e|e.to_string())?;
        let job_id=record["job_id"].as_str().ok_or("工作区事务缺少编号")?.to_string();
        let helper=journal.parent().unwrap().join("workspace-helper.exe");
        let mut command=Command::new(helper);context.configure_command(&mut command);
        let spawn=command.arg("--workspace-task").arg(&journal).args(locator.child_args()).creation_flags(0x08000000).current_dir(journal.parent().unwrap()).spawn();
        if let Err(error)=spawn{return Err(format!("无法启动工作区事务：{error}"));}
        let _=app.emit("desktop://workspace-progress",serde_json::json!({"job_id":job_id,"state":"running","phase":"原生迁移窗口已打开，正在退出当前工作区","can_cancel":false,"completed_bytes":0,"total_bytes":0}));
        let exit_state=operation_state.clone();let exit_app=app.clone();
        std::thread::spawn(move||{std::thread::sleep(Duration::from_millis(350));exit_state.shutdown(&exit_app);});
        Ok(serde_json::json!({"job_id":job_id}))
    }).await.map_err(|e|e.to_string()).and_then(|v|v);
    if let Err(mut error) = result {
        if let Some(manager) = recovery_app.try_state::<Arc<ResourceManager>>() {
            if let Err(recovery) = manager.resume_accepting_jobs() {
                error = format!("{error}；恢复维护前服务失败：{recovery}");
            }
        }
        state.workspace_busy.store(false, Ordering::Release);
        return Err(error);
    }
    result
}

// reserve_maintenance（原子预留维护任务）：阻止网络下载期间插入工作区迁移。
fn reserve_maintenance(
    gate: &Mutex<()>,
    workspace: &AtomicBool,
    update: &AtomicBool,
    updating: bool,
    device_operation: Option<&device_operation::DeviceOperation>, // device_operation（同一准入锁下检查设备授权与切换任务）。
) -> Result<(), String> {
    let _guard = gate.lock().map_err(|_| "维护操作锁不可用。")?;
    if device_operation.is_some_and(|operation| operation.snapshot()["active"] == true) {
        return Err("设备设置任务正在授权或切换，请等待完成后执行维护。".into());
    }
    if workspace.load(Ordering::Acquire) {
        return Err("工作区正在切换或迁移，请等待完成。".into());
    }
    if update.load(Ordering::Acquire) {
        return Err("程序更新正在下载或交接，请等待完成或取消更新。".into());
    }
    if updating {
        update.store(true, Ordering::Release);
    } else {
        workspace.store(true, Ordering::Release);
    }
    Ok(())
}
#[tauri::command(rename_all = "snake_case")]
async fn workspace_link(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    app: tauri::AppHandle,
    target_root: String,
) -> Result<serde_json::Value, String> {
    workspace_operation(window, state.inner().clone(), app, target_root, "link").await
}
#[tauri::command(rename_all = "snake_case")]
async fn workspace_migrate(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    app: tauri::AppHandle,
    target_root: String,
) -> Result<serde_json::Value, String> {
    workspace_operation(window, state.inner().clone(), app, target_root, "migrate").await
}

fn clean_workspace(
    state: &Desktop,
    manager: &ResourceManager,
    context: &LaunchContext,
    confirmation: Option<&str>,
    manual: bool,
) -> Result<serde_json::Value, String> {
    let _gate = state
        .maintenance_gate
        .try_lock()
        .map_err(|_| "另一项维护正在执行。")?;
    if state.workspace_busy.load(Ordering::Acquire)
        || state.app_update.busy.load(Ordering::Acquire)
        || state.stopping.load(Ordering::Acquire)
    {
        return Err("工作区正在迁移、更新或退出，请稍后清理。".into());
    }
    manager.with_idle_cleanup(|| {
        voice_desktop_common::workspace_cleanup::run(
            context,
            env!("CARGO_PKG_VERSION"),
            confirmation,
            manual,
        )
    })
}

#[tauri::command(rename_all = "snake_case")]
async fn workspace_cleanup(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    manager: tauri::State<'_, Arc<ResourceManager>>,
    confirmation: Option<String>,
) -> Result<serde_json::Value, String> {
    require_workspace(&window, &state)?;
    let context = state.context().ok_or("缺少工作区上下文")?;
    let state = state.inner().clone();
    let manager = manager.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        clean_workspace(&state, &manager, &context, confirmation.as_deref(), true)
    })
    .await
    .map_err(|error| error.to_string())?
}

fn app_update_payload(
    state_name: &str,
    manifest: Option<&voice_desktop_common::app_update::AppUpdateManifest>,
    done: u64,
    total: u64,
    phase: Option<&str>,
    error: Option<&str>,
    can_cancel: bool,
) -> serde_json::Value {
    serde_json::json!({"state":state_name,"current_version":env!("CARGO_PKG_VERSION"),"target_version":manifest.map(|value|value.version.as_str()),
        "release_notes":manifest.map(|value|value.release_notes.as_str()),"completed_bytes":done,"total_bytes":total,"phase":phase,"error":error,"can_cancel":can_cancel,
        "fields":{"state":"更新阶段","current_version":"当前程序版本","target_version":"目标程序版本","release_notes":"发行说明","phase":"当前处理阶段",
            "completed_bytes":"已下载字节数","total_bytes":"安装器总字节数","error":"更新错误","can_cancel":"当前是否允许取消"}})
}
fn emit_app_update(state: &Desktop, app: &tauri::AppHandle, payload: serde_json::Value) {
    *state.app_update.status.lock().unwrap() = payload.clone();
    let _ = app.emit(APP_UPDATE_PROGRESS_EVENT, payload);
}
#[tauri::command]
fn project_open_repository(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
) -> Result<(), String> {
    require_workspace(&window, &state)?;
    voice_desktop_common::windows::open_url("https://github.com/wet86y/voice-terminal")
}
#[tauri::command]
fn open_minimax_voice_library(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
) -> Result<(), String> {
    require_workspace(&window, &state)?;
    voice_desktop_common::windows::open_url("https://solutions.minimaxi.com/voices")
        .map_err(|_| "无法打开 MiniMax 官方音色库。".into())
}
#[tauri::command]
async fn app_update_check(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    app: tauri::AppHandle,
) -> Result<serde_json::Value, String> {
    require_workspace(&window, &state)?;
    if state
        .app_update
        .busy
        .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
        .is_err()
    {
        return Err("应用维护任务正在执行，请等待完成。".into());
    }
    let _reset = ResetFlag(&state.app_update.busy);
    *state.app_update.manifest.lock().unwrap() = None;
    emit_app_update(
        &state,
        &app,
        app_update_payload(
            "checking",
            None,
            0,
            0,
            Some("正在读取最新发行清单"),
            None,
            false,
        ),
    );
    let checked = tauri::async_runtime::spawn_blocking(move || {
        voice_desktop_common::app_update::check_latest(env!("CARGO_PKG_VERSION"))
    })
    .await
    .map_err(|error| error.to_string())?;
    let payload = match checked {
        Ok(Some(manifest)) => {
            *state.app_update.manifest.lock().unwrap() = Some(manifest.clone());
            app_update_payload(
                "available",
                Some(&manifest),
                0,
                manifest.size,
                None,
                None,
                false,
            )
        }
        Ok(None) => app_update_payload("current", None, 0, 0, None, None, false),
        Err(error) => app_update_payload("failed", None, 0, 0, None, Some(&error), false),
    };
    emit_app_update(&state, &app, payload.clone());
    Ok(payload)
}
#[tauri::command]
async fn app_update_download(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
    app: tauri::AppHandle,
) -> Result<serde_json::Value, String> {
    require_workspace(&window, &state)?;
    let manifest = state
        .app_update
        .manifest
        .lock()
        .unwrap()
        .clone()
        .ok_or("请先检查更新并确认有可用版本。")?;
    if !manifest.is_newer_than(env!("CARGO_PKG_VERSION"))? {
        return Err("当前没有可安装的新版本。".into());
    }
    reserve_maintenance(
        &state.maintenance_gate,
        &state.workspace_busy,
        &state.app_update.busy,
        true,
        Some(&state.device_operation),
    )?;
    state.app_update.cancel.store(false, Ordering::Release);
    let payload = app_update_payload(
        "downloading",
        Some(&manifest),
        0,
        manifest.size,
        Some("正在连接 GitHub 并校验安装器"),
        None,
        true,
    );
    emit_app_update(&state, &app, payload.clone());
    let desktop = state.inner().clone();
    let handle = app.clone();
    std::thread::spawn(move || {
        download_and_handoff_update(desktop, handle, manifest, MaintenanceAction::Update)
    });
    Ok(payload)
}
#[tauri::command]
fn app_update_cancel(
    window: WebviewWindow,
    state: tauri::State<Arc<Desktop>>,
) -> Result<(), String> {
    require_workspace(&window, &state)?;
    state.app_update.request_cancel()
}
fn download_and_handoff_update(
    state: Arc<Desktop>,
    app: tauri::AppHandle,
    manifest: voice_desktop_common::app_update::AppUpdateManifest,
    action: MaintenanceAction,
) {
    let result = (|| {
        let context = state.context().ok_or("缺少当前工作区上下文")?;
        if state.workspace_busy.load(Ordering::Acquire) {
            return Err("工作区正在切换或迁移，更新已停止。".into());
        }
        let installer = if action == MaintenanceAction::Update {
            voice_desktop_common::app_update::update_installer_path(&context.install)?
        } else {
            voice_desktop_common::app_update::installer_path(&context.install)?
        };
        fs::create_dir_all(installer.parent().ok_or("分发器目录无效")?)
            .map_err(|error| format!("无法准备分发器目录：{error}"))?;
        let installer = if action == MaintenanceAction::Update {
            voice_desktop_common::app_update::update_installer_path(&context.install)?
        } else {
            voice_desktop_common::app_update::installer_path(&context.install)?
        };
        let retained = if action == MaintenanceAction::CheckIntegrity {
            voice_desktop_common::app_update::retained(
                &context.install,
                env!("CARGO_PKG_VERSION"),
                &integrity_commands::local_build(&context)?,
            )?
        } else {
            voice_desktop_common::app_update::delivery(&manifest)?
        };
        if action != MaintenanceAction::Update
            && !retained.matches(
                env!("CARGO_PKG_VERSION"),
                &integrity_commands::local_build(&context)?,
            )
        {
            return Err("发行校验程序与当前本地构建不一致；不会下载或运行其他构建。".into());
        }
        let source = manifest.asset_url();
        let file = voice_desktop_common::download::DownloadFile {
            url: source,
            size: manifest.size,
            sha256: manifest.sha256.clone(),
            sha512: String::new(),
            path: manifest.asset.clone(),
            download_variants: vec![],
        };
        if action != MaintenanceAction::CheckIntegrity {
            for attempt in 1..=3 {
                let result = voice_desktop_common::download::with_attempt(Some(attempt), || {
                    voice_desktop_common::download::download_with_progress(
                        &file,
                        &installer,
                        state.app_update.cancel.clone(),
                        &mut |phase, done, total| {
                            let phase = format!("第 {attempt} / 3 轮 · {phase}");
                            let payload = maintenance_payload(
                                action,
                                "downloading",
                                Some(&manifest),
                                done,
                                total,
                                Some(&phase),
                                None,
                                true,
                            );
                            emit_app_update(&state, &app, payload);
                        },
                        0,
                    )
                });
                match result {
                    Ok(_) => break,
                    Err(error)
                        if attempt < 3
                            && resource_commands::retryable_failure(&error)
                            && !state.app_update.cancel.load(Ordering::Acquire) =>
                    {
                        let phase = format!("第 {attempt} / 3 轮失败，等待自动恢复：{error}");
                        emit_app_update(
                            &state,
                            &app,
                            maintenance_payload(
                                action,
                                "downloading",
                                Some(&manifest),
                                0,
                                0,
                                Some(&phase),
                                None,
                                true,
                            ),
                        );
                        for _ in 0..if attempt == 1 { 10 } else { 30 } {
                            if state.app_update.cancel.load(Ordering::Acquire) {
                                return Err("更新下载已取消。".into());
                            }
                            std::thread::sleep(Duration::from_millis(100));
                        }
                    }
                    Err(error) => return Err(error),
                }
            }
        }
        if state.app_update.cancel.load(Ordering::Acquire) {
            return Err("更新下载已取消。".into());
        }
        if action == MaintenanceAction::CheckIntegrity
            && (fs::metadata(&installer)
                .map_err(|error| error.to_string())?
                .len()
                != manifest.size
                || workspace::file_hash(&installer)? != manifest.sha256.to_lowercase())
        {
            let _ = fs::remove_file(&installer);
            return Err("分发安装器大小或 SHA-256 摘要校验失败。".into());
        }
        if action != MaintenanceAction::Update {
            voice_desktop_common::app_update::save_retained(&context.install, &retained)?;
        }
        if action == MaintenanceAction::DownloadIntegrity {
            emit_app_update(
                &state,
                &app,
                maintenance_payload(
                    action,
                    "current",
                    Some(&manifest),
                    manifest.size,
                    manifest.size,
                    Some("当前版本校验程序已准备；点击校验完整性开始检查与修复。"),
                    None,
                    false,
                ),
            );
            return Ok(());
        }
        if let Some(manager) = app.try_state::<Arc<ResourceManager>>() {
            manager.pause_and_wait()?;
        }
        if state.app_update.cancel.load(Ordering::Acquire) {
            return Err("更新交接已取消。".into());
        }
        let pid = std::process::id();
        let (parent_create_time, parent_executable) =
            workspace::process_identity(pid)?.ok_or("当前程序进程身份不可用。")?;
        let expected = voice_desktop_common::canonical_path(
            &context.install.join("app/VoiceTerminalDesktop.exe"),
        )?;
        if !parent_executable
            .to_string_lossy()
            .eq_ignore_ascii_case(&expected.to_string_lossy())
        {
            return Err("当前程序身份与工作区不匹配，拒绝启动更新安装器。".into());
        }
        let nonce = workspace::new_id();
        let config_path =
            workspace::safe_path(&context.install, &format!("tmp/app-update-{nonce}.json"))?;
        let consumed_path = config_path.with_extension("json.consumed");
        if config_path.exists() || consumed_path.exists() {
            return Err("更新配置编号重复，拒绝继续。".into());
        }
        let config = serde_json::json!({"schema":1,"nonce":nonce,"target_root":context.install,"workspace_id":context.workspace_id,
            "parent_pid":pid,"parent_create_time":parent_create_time,"parent_executable":parent_executable,
            "version":manifest.version,"tag":manifest.tag,"installer_size":manifest.size,"installer_sha256":manifest.sha256,"build":retained.build,
            "fields":{"schema":"一次性应用更新配置格式","nonce":"单次使用随机编号","target_root":"当前有效工作区根目录",
                "workspace_id":"必须保持不变的工作区身份编号","parent_pid":"当前主程序进程编号","parent_create_time":"当前主程序创建时间",
                "parent_executable":"当前主程序可执行文件位置","version":"待安装产品版本","tag":"对应 GitHub 发行标签",
                "build":"目标程序构建编号","installer_size":"已校验分发安装器字节数","installer_sha256":"已校验分发安装器 SHA-256 摘要"}});
        workspace::atomic_json(&config_path, &config)?;
        let handing_off = maintenance_payload(
            action,
            "handing_off",
            Some(&manifest),
            manifest.size,
            manifest.size,
            Some("正在交接已校验的更新安装器"),
            None,
            false,
        );
        if let Err(error) = state.app_update.begin_handoff(handing_off.clone()) {
            let _ = fs::remove_file(&config_path);
            return Err(error);
        }
        let _ = app.emit(APP_UPDATE_PROGRESS_EVENT, handing_off);
        let mut command = Command::new(&installer);
        command
            .arg(if action == MaintenanceAction::CheckIntegrity {
                "--repair-config"
            } else {
                "--app-update-config"
            })
            .arg(&config_path)
            .current_dir(&context.install)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .creation_flags(0x08000000);
        if let Err(error) = command.spawn() {
            let _ = fs::remove_file(&config_path);
            return Err(format!("无法启动更新分发器：{error}"));
        }
        emit_app_update(
            &state,
            &app,
            maintenance_payload(
                action,
                "handing_off",
                Some(&manifest),
                manifest.size,
                manifest.size,
                Some("正在关闭旧程序并启动安装器"),
                None,
                false,
            ),
        );
        state.shutdown(&app);
        Ok::<(), String>(())
    })();
    if let Err(mut error) = result {
        if let Some(manager) = app.try_state::<Arc<ResourceManager>>() {
            if let Err(recovery) = manager.resume_accepting_jobs() {
                error = format!("{error}；恢复维护前服务失败：{recovery}");
            }
        }
        let cancelled = state.app_update.cancel.load(Ordering::Acquire) || error.contains("已取消");
        let payload = maintenance_payload(
            action,
            if cancelled { "cancelled" } else { "failed" },
            Some(&manifest),
            0,
            manifest.size,
            None,
            Some(&error),
            false,
        );
        emit_app_update(&state, &app, payload);
    }
    state.app_update.busy.store(false, Ordering::Release);
}

fn show(app: &tauri::AppHandle) {
    if let Some(state) = app.try_state::<Arc<Desktop>>() {
        let _ = set_floating_mode(app, state.inner(), false);
    }
    if let Some(window) = app
        .get_webview_window("main")
        .or_else(|| app.get_webview_window("startup"))
    {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn valid_launch_token(value: &str) -> bool {
    (43..=128).contains(&value.len())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
}

fn valid_instance_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
}

fn entry_url_allowed(url: &tauri::Url, expected_token: Option<&str>) -> bool {
    if url.path() != "/desktop-entry" || url.fragment().is_some() {
        return false;
    }
    let Some(expected) = expected_token else {
        return false;
    };
    let pairs: Vec<_> = url.query_pairs().collect();
    pairs.len() == 1 && pairs[0].0 == "token" && pairs[0].1 == expected
}

fn page_url_allowed(url: &tauri::Url, path: &str, allowed_keys: &[&str]) -> bool {
    if url.path() != path || url.fragment().is_some() {
        return false;
    }
    let mut seen = std::collections::HashSet::new();
    url.query_pairs().all(|(key, value)| {
        allowed_keys.contains(&key.as_ref()) && value.len() <= 64 && seen.insert(key.into_owned())
    })
}

fn product_entry_url_allowed(url: &tauri::Url, token: Option<&str>) -> bool {
    entry_url_allowed(url, token)
}

fn product_page_url_allowed(url: &tauri::Url) -> bool {
    page_url_allowed(url, "/", &["page", "settings"])
        || page_url_allowed(url, "/index.html", &["page", "settings"])
}

fn floating_page_url_allowed(url: &tauri::Url) -> bool {
    page_url_allowed(url, "/floating.html", &[])
}

fn preview_entry_url_allowed(url: &tauri::Url, token: Option<&str>) -> bool {
    entry_url_allowed(url, token)
}

fn preview_page_url_allowed(url: &tauri::Url) -> bool {
    page_url_allowed(
        url,
        "/ui-demo.html",
        &["page", "scene", "runtime", "overlay", "replay", "session"],
    )
        // session（演示会话状态）由前端保存；允许活动状态页面刷新，仍校验固定页面与参数。
        && url
            .query_pairs()
            .all(|(key, value)| key != "session" || value == "active")
}

fn frontend_entry_url(
    origin: &str,
    token: &str,
    _mode: FrontendMode,
) -> Result<tauri::Url, String> {
    if !valid_launch_token(token) {
        return Err("服务入口启动凭据无效。".into());
    }
    let mut url: tauri::Url = origin
        .parse()
        .map_err(|_| "服务入口地址无效。".to_string())?;
    if url.scheme() != "http"
        || url.host_str() != Some("127.0.0.1")
        || url.port().filter(|v| *v > 0).is_none()
        || url.path() != "/"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err("服务入口地址不符合本机页面约束。".into());
    }
    url.set_path("/desktop-entry");
    tauri::Url::parse_with_params(url.as_str(), [("token", token)])
        .map_err(|_| "服务入口启动凭据无法编码。".to_string())
}

fn instance_identifier(mode: FrontendMode, source_root: Option<&Path>) -> String {
    let Some(source_root) = source_root else {
        return "com.voiceterminal.desktop".into();
    };
    // FNV-1a（稳定路径散列）确保相同源码根目录跨进程得到同一标识。
    let normalized = source_root.to_string_lossy().to_lowercase();
    let hash = normalized
        .bytes()
        .fold(0xcbf29ce484222325u64, |hash, byte| {
            (hash ^ u64::from(byte)).wrapping_mul(0x100000001b3)
        });
    let suffix = format!("{hash:016x}");
    match mode {
        FrontendMode::Product => format!("com.voiceterminal.desktop.source.{suffix}"),
        FrontendMode::UiPreview => format!("com.voiceterminal.desktop.ui-preview.{suffix}"),
    }
}

fn arg_value<'a>(args: &'a [String], key: &str) -> Result<Option<&'a str>, String> {
    let positions: Vec<_> = args
        .iter()
        .enumerate()
        .filter_map(|(index, arg)| (arg == key).then_some(index))
        .collect();
    if positions.len() > 1 {
        return Err(format!("{key} 不能重复。"));
    }
    positions
        .first()
        .map(|index| {
            args.get(index + 1)
                .map(String::as_str)
                .filter(|next| !next.starts_with("--"))
                .ok_or_else(|| format!("{key} 缺少路径。"))
        })
        .transpose()
}

fn ui_preview_context(args: &[String]) -> Result<(PathBuf, Option<PathBuf>, PathBuf), String> {
    for arg in args {
        if arg.starts_with("--") && !["--ui-preview", "--root", "--exit"].contains(&arg.as_str()) {
            return Err(format!("桌面样例不接受产品启动参数：{arg}"));
        }
    }
    let root = arg_value(args, "--root")?.ok_or("桌面样例必须指定 --root（项目根目录）。")?;
    let program = voice_desktop_common::canonical_path(Path::new(root))?;
    let data = program.join(".local/cache/ui-demo");
    fs::create_dir_all(&data).map_err(|e| format!("桌面样例缓存目录不可用：{e}"))?;
    let data = voice_desktop_common::canonical_path(&data)?;
    if !data.starts_with(&program) {
        return Err("桌面样例缓存必须位于独立的项目运行目录。".into());
    }
    let shared = LaunchContext::portable(&program.join(".local"))?;
    let webview2 = shared.webview2;
    Ok((program, webview2, data))
}

#[derive(Clone, Copy, PartialEq)]
enum ServiceStartKind {
    Startup,
    ResourceActivation,
    RuntimeRecovery,
} // ServiceStartKind（首次启动、受控资源换入、故障恢复）。

fn pending_start_transaction(install: &Path, kind: ServiceStartKind) -> bool {
    if install.join("tmp/update-transaction.json").exists() {
        return true;
    }
    let journal = install.join("tmp/native-install.json");
    if !journal.exists() {
        return false;
    }
    // 只有持有资源启用锁的内部恢复，才能验证本次已交换、尚待提交的候选。
    kind != ServiceStartKind::ResourceActivation
        || voice_desktop_common::resources::load(&journal)
            .map(|value| value["state"] != "swapped")
            .unwrap_or(true)
}

fn start_service(app: tauri::AppHandle, state: Arc<Desktop>) -> Result<(), String> {
    start_service_for(app, state, ServiceStartKind::Startup)
}

fn start_service_for(
    app: tauri::AppHandle,
    state: Arc<Desktop>,
    kind: ServiceStartKind,
) -> Result<(), String> {
    let resource_recovery = kind != ServiceStartKind::Startup;
    let _transition = state
        .service_transition
        .lock()
        .map_err(|_| "服务交接锁不可用。")?;
    if state.service_starting.swap(true, Ordering::AcqRel) {
        return Ok(());
    }
    struct StartingGuard<'a>(&'a AtomicBool); // StartingGuard（启动生命周期标记）：成功、失败及取消均释放。
    impl Drop for StartingGuard<'_> {
        fn drop(&mut self) {
            self.0.store(false, Ordering::Release);
        }
    }
    let _starting = StartingGuard(&state.service_starting);
    let _credentials_guard = state
        .credentials_guard
        .lock()
        .map_err(|_| "接口配置锁不可用。")?;
    if state.stopping.load(Ordering::Acquire) {
        return Err("桌面正在退出。".into());
    }
    if !resource_recovery
        && app
            .try_state::<Arc<ResourceManager>>()
            .is_some_and(|manager| manager.has_active_jobs())
    {
        return Err("组件任务尚未结束，请等待任务完成后启动。".into());
    }
    {
        let mut service = state.service.lock().map_err(|_| "服务状态不可用。")?;
        if service
            .as_mut()
            .is_some_and(|value| matches!(value.child.try_wait(), Ok(None)))
        {
            return Ok(());
        }
    }
    if state.mode == FrontendMode::Product {
        let Some(context) = state.context() else {
            return Ok(());
        };
        let context = context.refreshed_components()?;
        *state.context.lock().unwrap() = Some(context.clone());
        #[cfg(feature = "ssh-device")]
        state.ssh_device.set_key_root(&context.data)?;
        *state.error.lock().unwrap() = None;
        let device_snapshot = voice_desktop_common::device_config::inspect(&context.data);
        if product_startup_kind(device_snapshot.mode) == ProductStartupKind::RemoteClient {
            let build = voice_desktop_common::resources::load(&context.program.join("release.json")).ok()
                .and_then(|value| value.get("build").cloned());
            client_event(&state, "client.start", serde_json::json!({"version":env!("CARGO_PKG_VERSION"),
                "build":build,"device_id":device_snapshot.device_id,"mode":"remote"}));
            if resource_recovery {
                return Err("当前处于远程配对模式；资源任务不能启动本机语音核心。".into());
            }
            state
                .remote_client_connected
                .store(false, Ordering::Release);
            // origin（静态产品页来源）：让桌面预检确认远程产品资产就绪，无需启动本地 Python 核心。
            *state.origin.lock().unwrap() = Some(REMOTE_STATIC_PRODUCT_ORIGIN.into());
            *state.initialization.lock().unwrap() = serde_json::json!({
                "phase":"远程配对模式已启动，等待搜索并连接服务端",
                "can_cancel":false,"failed":false,
                "fields":{"phase":"客户端不启动本机推理核心，等待发现和连接服务端","can_cancel":"当前模式持续可用","failed":"初始化失败标记"}
            });
            initialization::manage(&app, &state)?;
            return Ok(());
        }
        if let Some(reason) =
            voice_desktop_common::device_config::startup_block_reason(&device_snapshot)
        {
            if resource_recovery {
                return Err(reason);
            }
            *state.error.lock().unwrap() = Some(reason.clone());
            *state.initialization.lock().unwrap() = serde_json::json!({"phase":reason,"can_cancel":false,"failed":false,
                "fields":{"phase":"设备配置或目标角色的就绪问题","can_cancel":"当前阶段不能取消","failed":"初始化失败标记"}});
            initialization::manage(&app, &state)?;
            return Ok(());
        }
        *state.initialization.lock().unwrap() =
            serde_json::json!({"phase":"检查必要启动条件","can_cancel":true});
        // 资源恢复只接纳管理器已验证并换入的候选，仍保留解释器、外部事务及凭据门禁。
        if !resource_recovery {
            let readiness = voice_desktop_common::resources::startup_readiness_with_progress(
                &context,
                &state.initialization_cancel,
                &mut |phase, done, total| {
                    *state.initialization.lock().unwrap() = serde_json::json!({"phase":phase,"completed_bytes":done,"total_bytes":total,"can_cancel":true,
                "fields":{"phase":"启动组件校验阶段","completed_bytes":"全组件累计进度量（语音环境按文件数折算）","total_bytes":"全组件清单字节权重总量","can_cancel":"是否可取消校验"}});
                },
            );
            if state.initialization_cancel.load(Ordering::Acquire) {
                *state.initialization.lock().unwrap() =
                    serde_json::json!({"phase":"启动校验已取消，可以重新启动","can_cancel":false});
                initialization::manage(&app, &state)?;
                return Ok(());
            }
            let readiness = readiness?;
            if let Some(manager) = app.try_state::<Arc<ResourceManager>>() {
                manager.record_validation(&readiness);
            }
            *state.initialization.lock().unwrap() = serde_json::json!({
                "phase":if readiness["ready"]==true{"组件校验通过，正在启动服务"}else{"部分组件需要处理，继续启动可用功能；可手动校验完整性"},
                "components_ready":readiness["ready"],"missing_components":readiness["missing"],"can_cancel":false,
                "fields":{"phase":"启动阶段","components_ready":"所有组件具备启动条件","missing_components":"待手动补齐的组件","can_cancel":"当前阶段是否可取消"}
            });
        }
        // 普通组件问题只作提示，业务按实际加载结果降级；解释器缺失或未完成事务仍保留管理界面。
        let pending = pending_start_transaction(&context.install, kind);
        let compatibility =
            voice_desktop_common::resources::load(&context.data.join("state/compatibility.json"))
                .unwrap_or(serde_json::Value::Null);
        let unfinished = compatibility["enforcement"].is_object()
            && compatibility["enforcement"]["phase"] != "complete";
        if !context.python.is_file() || pending || unfinished {
            if resource_recovery {
                return Err("运行环境或外部维护事务尚未就绪，无法恢复业务服务。".into());
            }
            initialization::manage(&app, &state)?;
            return Ok(());
        }
        let credentials = match voice_desktop_common::credentials::status_for_context(&context) {
            Ok(value) => value,
            Err(error) => {
                if resource_recovery {
                    return Err(format!("接口配置读取失败：{error}"));
                }
                *state.initialization.lock().unwrap() = serde_json::json!({"phase":"接口密钥配置需要处理，请进入模型管理页","components_ready":true,"credentials_ready":false,"credentials_error":error,"can_cancel":false,
                    "fields":{"phase":"启动阶段","components_ready":"所有组件具备启动条件","credentials_ready":"固定服务接口密钥已配置","credentials_error":"接口配置读取错误","can_cancel":"当前阶段是否可取消"}});
                initialization::manage(&app, &state)?;
                return Ok(());
            }
        };
        if !voice_desktop_common::credentials::ready(&credentials) {
            if resource_recovery {
                return Err("接口密钥尚未配置，无法恢复业务服务。".into());
            }
            *state.initialization.lock().unwrap() = serde_json::json!({"phase":"请先在模型管理页填写接口密钥","components_ready":true,"credentials_ready":false,"can_cancel":false,
                "fields":{"phase":"启动阶段","components_ready":"所有组件具备启动条件","credentials_ready":"固定服务接口密钥已配置","can_cancel":"当前阶段是否可取消"}});
            initialization::manage(&app, &state)?;
            return Ok(());
        }
    }
    let logs = if let Some(context) = state.context() {
        context.install.join("cache/logs")
    } else {
        state.data.clone()
    };
    fs::create_dir_all(&logs).map_err(|e| e.to_string())?;
    let output_name = if state.mode == FrontendMode::UiPreview {
        "frontend-dev.log"
    } else {
        "desktop-service.log"
    };
    let output = File::options()
        .append(true)
        .create(true)
        .open(logs.join(output_name))
        .map_err(|e| e.to_string())?;
    let mut command;
    if state.mode == FrontendMode::UiPreview {
        let helper = state.program.join("scripts/frontend-dev.mjs");
        if !helper.is_file() {
            return Err("缺少桌面样例启动器 scripts/frontend-dev.mjs。".into());
        }
        let shared = LaunchContext::portable(&state.program.join(".local"))?;
        command = Command::new(shared.node.ok_or("宿主清单缺少 Node（脚本解释器）")?);
        command
            .arg(helper)
            .arg("--root")
            .arg(&state.program)
            .args(["--mode", "ui-preview"])
            .current_dir(&state.program);
        if let Some(engine) = &state.webview2 {
            command.env("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", engine);
        }
    } else {
        let context = state.context().ok_or("产品服务缺少桌面启动上下文。")?;
        command = voice_desktop_common::process_host::python_command(&context.python);
        context.configure_command(&mut command);
        if let Some(debug) = app.try_state::<Arc<agent_debug::AgentDebug>>() {
            debug.configure_worker(&mut command, &context.install)?;
        }
        if let voice_desktop_common::device_config::LoadResult::Valid(device_config) =
            voice_desktop_common::device_config::load_from_root(&context.data)
        {
            if device_config.mode == voice_desktop_common::device_config::RunMode::Local {
                command.env("VOICE_TERMINAL_HOST_ID", &device_config.device_id);
            }
        }
        command
            .args(["-X", "utf8", "-X"])
            .arg(format!(
                "pycache_prefix={}",
                context.install.join("cache/python").display()
            ))
            .arg(context.program.join("scripts/backend-entry.py"))
            .arg("voice_terminal.application.launch")
            .arg("--root")
            .arg(&context.program)
            .arg("--python")
            .arg(&context.python)
            .arg("--data-dir")
            .arg(&context.data)
            .arg("--desktop-managed")
            .current_dir(&context.program);
        if context.dev {
            command.arg("--dev");
        }
        if voice_desktop_common::device_config::inspect(&context.data).broadcast_enabled {
            command.arg("--share-device-gateway");
        }
        if context.diagnostic {
            command.arg("--diagnostic");
        }
    }
    command
        .creation_flags(0x08000000)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(output);
    let job = Job::new()?;
    // 与退出流程共用锁，防止主窗口提前关闭后仍建立后台服务。
    let mut service_slot = state.service.lock().map_err(|error| error.to_string())?;
    if state.stopping.load(Ordering::SeqCst) {
        return Ok(());
    }
    if service_slot
        .as_mut()
        .is_some_and(|service| service.child.try_wait().ok().flatten().is_some())
    {
        service_slot.take();
    }
    if service_slot.is_some() {
        return Ok(());
    }
    let epoch = state.service_epoch.fetch_add(1, Ordering::SeqCst) + 1;
    state
        .recovery
        .lock()
        .map_err(|_| "运行状态不可用。")?
        .current_core = None;
    *state
        .device_gateway_status
        .lock()
        .map_err(|_| "共享网关状态不可用。")? =
        serde_json::json!({"enabled":false,"ready":false,"error":""});
    let preserve_page = resource_recovery && state.recovery.lock().unwrap().preserves_page();
    if !preserve_page {
        *state.origin.lock().unwrap() = None;
    }
    *state.error.lock().unwrap() = None;
    state.recovery.lock().unwrap().result = None;
    *state.dsh_origin.lock().unwrap() = None;
    let mut child = job.spawn(&mut command).map_err(|e| {
        format!(
            "{} 启动失败：{e}",
            if state.mode == FrontendMode::UiPreview {
                "桌面样例 Node（脚本运行时）"
            } else {
                "服务管理进程"
            }
        )
    })?;
    let reader = BufReader::new(child.stdout.take().ok_or("缺少服务控制管道")?);
    let input = child.stdin.take().ok_or("缺少服务控制管道")?;
    *service_slot = Some(Service { child, input, job });
    drop(service_slot);
    // protocol（私有协议版本）、kind（消息类型）；Node（脚本运行时）与 Python（后端语言）均接收 start（启动）。
    let continuity = if preserve_page {
        state.recovery.lock().unwrap().continuity.clone()
    } else {
        None
    };
    state.send(serde_json::json!({"protocol":1,"kind":"start","continuity":continuity}))?;
    if state.mode == FrontendMode::Product {
        // pid（桌面宿主进程号）用于产品运行状态展示。
        state.send(serde_json::json!({
            "protocol": 1,
            "kind": "desktop_process",
            "pid": std::process::id()
        }))?;
    }
    let read_state = state.clone();
    let read_app = app.clone();
    std::thread::spawn(move || {
        for line in reader.lines() {
            if read_state.service_epoch.load(Ordering::Acquire) != epoch {
                return;
            }
            let Ok(line) = line else { break };
            if line.len() > MAX_DESKTOP_CONTROL_MESSAGE_BYTES {
                *read_state.error.lock().unwrap() = Some("服务入口协议消息过长。".into());
                break;
            }
            let Ok(message) = serde_json::from_str::<serde_json::Value>(&line) else {
                continue;
            };
            if message["protocol"] != 1 {
                continue;
            }
            match message["kind"].as_str() {
                Some("ready") => {
                    // origin（本机页面来源）、launch_token（一次性启动凭据）、instance_id（服务实例标识）。
                    let Some(origin) = message["origin"].as_str() else {
                        *read_state.error.lock().unwrap() = Some("服务入口地址缺失。".into());
                        break;
                    };
                    let Some(token) = message["launch_token"].as_str() else {
                        *read_state.error.lock().unwrap() = Some("服务入口启动凭据缺失。".into());
                        break;
                    };
                    let Some(instance_id) = message["instance_id"].as_str() else {
                        *read_state.error.lock().unwrap() = Some("服务入口实例标识缺失。".into());
                        break;
                    };
                    if !valid_instance_id(instance_id) {
                        *read_state.error.lock().unwrap() = Some("服务入口实例标识无效。".into());
                        break;
                    }
                    let entry = match frontend_entry_url(origin, token, read_state.mode) {
                        Ok(url) => url,
                        Err(error) => {
                            *read_state.error.lock().unwrap() = Some(error);
                            break;
                        }
                    };
                    let normalized_origin = entry.origin().ascii_serialization();
                    {
                        let mut recovery = read_state.recovery.lock().unwrap();
                        if preserve_page
                            && recovery
                                .continuity
                                .as_ref()
                                .is_none_or(|value| value["frontend_origin"] != normalized_origin)
                        {
                            *read_state.error.lock().unwrap() =
                                Some("恢复后的页面来源与当前窗口不匹配。".into());
                            break;
                        }
                        if message["continuity"].is_object() {
                            recovery.continuity = Some(message["continuity"].clone());
                        }
                    }
                    let capability =
                        frontend_capability(read_state.mode, &normalized_origin, epoch);
                    if let Err(error) = read_app.add_capability(capability.to_string()) {
                        *read_state.error.lock().unwrap() =
                            Some(format!("页面权限配置失败：{error}"));
                        break;
                    }
                    if read_state.mode == FrontendMode::Product {
                        if let Err(error) = read_app.add_capability(
                            floating_capability(&normalized_origin, epoch).to_string(),
                        ) {
                            *read_state.error.lock().unwrap() =
                                Some(format!("悬浮窗权限配置失败：{error}"));
                            break;
                        }
                        if let Err(error) = read_app.add_capability(
                            floating_publisher_capability(&normalized_origin, epoch).to_string(),
                        ) {
                            *read_state.error.lock().unwrap() =
                                Some(format!("悬浮窗状态发布权限配置失败：{error}"));
                            break;
                        }
                    }
                    *read_state.launch_token.lock().unwrap() = Some(token.into());
                    *read_state.origin.lock().unwrap() = Some(normalized_origin);
                    read_state
                        .service_ready_epoch
                        .store(epoch, Ordering::Release);
                    if preserve_page {
                        read_state.recovery.lock().unwrap().ready();
                        read_state.publish_recovery(&read_app);
                    } else if let Some(window) = read_app.get_webview_window("main") {
                        if let Err(error) = window.navigate(entry) {
                            *read_state.error.lock().unwrap() = Some(error.to_string());
                        }
                    }
                }
                #[cfg(feature = "ssh-device")]
                Some("device_gateway_result") => {
                    if let Some(id) = message["request_id"].as_u64() {
                        read_state.gateway_control.settle(id, message);
                    }
                }
                Some("startup_result") => {
                    let mut recovery = read_state.recovery.lock().unwrap();
                    recovery.current_core = message["core_available"].as_bool();
                    recovery.started = true;
                    recovery.result = Some((epoch, message.clone()));
                    *read_state.device_gateway_status.lock().unwrap() = serde_json::json!({
                        "enabled":message["device_gateway_enabled"].as_bool().unwrap_or(false),
                        "ready":message["device_gateway_ready"].as_bool().unwrap_or(false),
                        "error":message["device_gateway_error"].as_str().unwrap_or("")});
                    #[cfg(feature = "ssh-device")]
                    {
                        let discovery_state = read_state.clone();
                        tauri::async_runtime::spawn(async move {
                            if let Err(error) = sync_local_discovery(discovery_state.clone()).await
                            {
                                *discovery_state.broadcast_error.lock().unwrap() = Some(error);
                            }
                        });
                    }
                }
                Some("runtime_status") => {
                    read_state.recovery.lock().unwrap().current_core =
                        message["core_available"].as_bool();
                    *read_state.device_gateway_status.lock().unwrap() = serde_json::json!({
                        "enabled":message["device_gateway_enabled"].as_bool().unwrap_or(false),
                        "ready":message["device_gateway_ready"].as_bool().unwrap_or(false),
                        "error":message["device_gateway_error"].as_str().unwrap_or("")});
                    #[cfg(feature = "ssh-device")]
                    {
                        let discovery_state = read_state.clone();
                        tauri::async_runtime::spawn(async move {
                            if let Err(error) = sync_local_discovery(discovery_state.clone()).await
                            {
                                *discovery_state.broadcast_error.lock().unwrap() = Some(error);
                            }
                        });
                    }
                }
                Some("exit_requested") => read_state.shutdown(&read_app),
                Some("logs_flushed") | Some("logs_flush_failed") => {
                    // request_id（刷新请求编号）绑定桌面宿主的落盘确认。
                    if let Some(request_id) = message["request_id"].as_u64() {
                        let mut result = read_state.log_flush_state.lock().unwrap();
                        if message["kind"] == "logs_flush_failed" {
                            result.failed_request = request_id;
                        }
                        result.completed_request = result.completed_request.max(request_id);
                        read_state.log_flush_changed.notify_all();
                    }
                }
                Some("device_helper_request") => {
                    let request_id = message["request_id"].as_str().unwrap_or("").to_owned();
                    let device_id = message["device_id"].as_str().unwrap_or("").to_owned();
                    let operation = message["operation"].as_str().unwrap_or("").to_owned();
                    let arguments = message["arguments"].clone();
                    let timeout_ms = message["timeout_ms"]
                        .as_u64()
                        .and_then(|value| u32::try_from(value).ok());
                    let connection_generation = message["connection_generation"].as_u64();
                    let executor_generation = message["executor_generation"].as_u64();
                    let allowed_fields = [
                        "protocol",
                        "kind",
                        "request_id",
                        "device_id",
                        "operation",
                        "arguments",
                        "timeout_ms",
                        "connection_generation",
                        "executor_generation",
                    ];
                    let exact = message.as_object().is_some_and(|fields| {
                        fields
                            .keys()
                            .all(|key| allowed_fields.contains(&key.as_str()))
                            && fields.len() == allowed_fields.len()
                    });
                    let valid = read_state.mode == FrontendMode::Product
                        && exact
                        && valid_device_rpc_id(&request_id)
                        && device_id == "local"
                        && !operation.is_empty()
                        && arguments.is_object()
                        && timeout_ms.is_some_and(|value| (1..=600_000).contains(&value))
                        && connection_generation.is_some_and(|value| value > 0)
                        && executor_generation.is_some_and(|value| value > 0);
                    let response_state = read_state.clone();
                    std::thread::spawn(move || {
                        let result = if !valid {
                            Err("桌面设备请求字段、设备目标或时限无效。".to_owned())
                        } else if response_state.stopping.load(Ordering::Acquire) {
                            Err("应用正在退出。".to_owned())
                        } else if operation.starts_with("file.") {
                            voice_desktop_common::device_files::DeviceFileManager.execute(
                                &operation, &arguments, connection_generation.unwrap_or_default(),
                                executor_generation.unwrap_or_default(),
                            ).map(|result| serde_json::json!({"protocol_version":1,"request_id":request_id,
                                "ok":true,"result":result,"fields":{"result":"本机受限文件任务结果"}}))
                        } else if operation.starts_with("terminal.") {
                            #[cfg(feature = "ssh-device")]
                            {
                                response_state.device_terminal.execute(
                                    &operation, &arguments, connection_generation.unwrap_or_default(),
                                    executor_generation.unwrap_or_default(),
                                ).map(|result| serde_json::json!({"protocol_version":1,"request_id":request_id,
                                    "ok":true,"result":result,"fields":{"result":"本机受限终端任务结果"}}))
                            }
                            #[cfg(not(feature = "ssh-device"))]
                            {
                                Err("本机构建未包含受限终端任务能力。".to_owned())
                            }
                        } else {
                            (|| {
                                let context = response_state.context().ok_or("缺少正式工作区")?;
                                let helper = packaged_device_helper_path(&context);
                                let mut slot = response_state
                                    .device_worker
                                    .lock()
                                    .map_err(|error| error.to_string())?;
                                if slot.is_none() {
                                    *slot=Some(voice_desktop_common::device_worker::ManagedWorker::start_with_helper_path(helper.as_deref())?);
                                }
                                slot.as_mut()
                                    .expect("设备工作进程已建立")
                                    .desktop_helper_call(
                                        &request_id,
                                        &operation,
                                        &arguments,
                                        timeout_ms.unwrap_or(10_000),
                                    )
                            })()
                        };
                        let response = match result {
                            Ok(helper_response) => {
                                serde_json::json!({"protocol":1,"kind":"device_helper_response","request_id":request_id,
                                "ok":true,"response":helper_response,"fields":{"response":"受管设备桌面辅助原始回执"}})
                            }
                            Err(error) => {
                                serde_json::json!({"protocol":1,"kind":"device_helper_response","request_id":request_id,
                                "ok":false,"error":error,"fields":{"error":"设备桌面请求未能取得可信回执"}})
                            }
                        };
                        let _ = response_state.send(response);
                    });
                }
                Some("endpoints") => {
                    let origin = message["endpoints"]["dsh"]["url"]
                        .as_str()
                        .and_then(|value| value.parse::<tauri::Url>().ok())
                        .filter(|url| {
                            url.scheme() == "http"
                                && url.host_str() == Some("127.0.0.1")
                                && url.port().filter(|v| *v > 0).is_some()
                                && url.query().is_none()
                                && url.username().is_empty()
                                && url.password().is_none()
                        })
                        .map(|url| url.origin().ascii_serialization());
                    *read_state.dsh_origin.lock().unwrap() = origin;
                }
                Some("stopped") => {}
                _ => {}
            }
        }
        if read_state.service_epoch.load(Ordering::Acquire) == epoch
            && !read_state.stopping.load(Ordering::SeqCst)
        {
            if !read_state.recovery.lock().unwrap().preserves_page() {
                *read_state.origin.lock().unwrap() = None;
            }
            *read_state.launch_token.lock().unwrap() = None;
            read_state.error.lock().unwrap().get_or_insert_with(|| {
                if read_state.mode == FrontendMode::UiPreview {
                    "桌面样例启动服务已异常结束，请检查独立缓存目录中的 frontend-dev.log（样例服务日志）。".into()
                } else {
                    "服务入口已异常结束，请检查 logs（日志目录）中的 desktop-service.log（服务入口日志）。".into()
                }
            });
            if read_state.recovery.lock().unwrap().preserves_page() {
                let error = read_state
                    .error
                    .lock()
                    .unwrap()
                    .clone()
                    .unwrap_or_else(|| "业务服务异常退出".into());
                {
                    let mut recovery = read_state.recovery.lock().unwrap();
                    recovery.suspend();
                    recovery.finish(Some(&error));
                }
                read_state.publish_recovery(&read_app);
            } else if let Some(window) = read_app.get_webview_window("main") {
                let _ = window.navigate("http://tauri.localhost/management.html".parse().unwrap());
                let _ = window.show();
            }
            if let Some(service) = read_state.service.lock().unwrap().as_mut() {
                service.job.terminate();
            }
        }
    });
    let timeout_state = state.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_secs(30));
        if timeout_state.service_epoch.load(Ordering::Acquire) == epoch
            && timeout_state.service_ready_epoch.load(Ordering::Acquire) != epoch
            && !timeout_state.stopping.load(Ordering::SeqCst)
        {
            timeout_state.error.lock().unwrap().get_or_insert_with(|| {
                if timeout_state.mode == FrontendMode::UiPreview {
                    "等待桌面样例页面超时，请检查 Node（脚本运行时）服务日志。".into()
                } else {
                    "等待服务入口超时。请检查业务环境与端口占用。".into()
                }
            });
            if let Some(service) = timeout_state.service.lock().unwrap().as_mut() {
                service.job.terminate();
            }
        }
    });
    Ok(())
}

// create_window（统一窗口）：选定工作区后销毁临时窗口，将网页缓存切换到工作区。
fn create_window(
    app: &tauri::AppHandle,
    build_state: Arc<Desktop>,
    management: bool,
) -> Result<WebviewWindow, String> {
    let remote_product = management
        && build_state.context().is_some_and(|context| {
            voice_desktop_common::device_config::inspect(&context.data).mode
                == Some(voice_desktop_common::device_config::RunMode::Remote)
        });
    let repair_handoff = management
        && build_state.context().is_some_and(|context| {
            context.install.join("tmp/native-install.json").exists()
                || voice_desktop_common::component_maintenance::pending_repairs(&context)
                    .unwrap_or(false)
        });
    let pending = build_state.mode == FrontendMode::Product && build_state.context().is_none();
    let navigation_state = build_state.clone();
    let page_state = build_state.clone();
    let main_window = WebviewWindowBuilder::new(
        app,
        if pending { "startup" } else { "main" },
        WebviewUrl::App(if management {
            if remote_product {
                "index.html".into()
            } else {
                "management.html".into()
            }
        } else {
            "startup.html".into()
        }),
    )
    .background_color(tauri::window::Color(14, 17, 24, 255))
    .visible(false)
    .initialization_script(voice_desktop_common::webview::DISABLE_CONTEXT_MENU)
    .initialization_script(agent_debug::AgentDebug::window_script(app))
    .title(if build_state.mode == FrontendMode::UiPreview {
        "Voice Terminal · 前端演示（无业务后端）"
    } else {
        "Voice Terminal · 语音终端"
    })
    // inner_size（默认内容尺寸）：小窗口启动，前端按可用空间逐级收起侧栏。
    .inner_size(1000., 640.)
    .min_inner_size(400., 480.)
    .data_directory(
        build_state
            .context()
            .map(|context| context.install.join("cache/webview2"))
            .unwrap_or_else(|| build_state.data.clone()),
    )
    .incognito(build_state.context().is_none())
    .on_navigation(move |url| {
        let available = navigation_state
            .recovery
            .lock()
            .unwrap()
            .allows_navigation();
        (url.scheme() == "tauri" && url.host_str() == Some("localhost"))
            || (url.scheme() == "http"
                && url.host_str() == Some("tauri.localhost")
                && url.port().is_none())
            || (available && navigation_state.allows_frontend_url(url))
    })
    .on_page_load(move |window, payload| {
        #[cfg(feature = "qualification")]
        qualification::page_loaded(&window, &page_state, payload.event(), payload.url());
        // 首个受控窗口即可交接，管理页与产品页共用确认，不等待业务服务。
        if payload.event() == PageLoadEvent::Finished
            && !page_state.stopping.load(Ordering::Acquire)
            && (boot_page_url(payload.url()) || page_state.allows_frontend_url(payload.url()))
        {
            let _ = window.show();
            if let Some(context) = page_state.context() {
                let arguments = std::env::args().skip(1).collect::<Vec<_>>();
                let _ =
                    voice_desktop_common::installation::acknowledge(&context.install, &arguments);
            }
        }
        if repair_handoff {
            return;
        }
        if !request_service_start(
            payload.url(),
            payload.event(),
            &page_state.service_requested,
            &page_state.stopping,
        ) {
            return;
        }
        // 最终主窗口的本地界面已加载，再从后台线程准备业务或演示服务。
        let service_app = window.app_handle().clone();
        let service_state = page_state.clone();
        std::thread::spawn(move || {
            if let Err(error) = initialization::auto_start(service_app, service_state.clone()) {
                *service_state.error.lock().unwrap() = Some(error);
            }
        });
    })
    .build()
    .map_err(|e| e.to_string())?;
    app_icon::apply(main_window.hwnd().map_err(|e| e.to_string())?.0).map_err(|e| e.to_string())?;
    Ok(main_window)
}
fn cleanup_initialization_cache(path: &Path) {
    if !path
        .file_name()
        .is_some_and(|name| name.to_string_lossy().starts_with("voice-initialization-"))
    {
        return;
    }
    for _ in 0..25 {
        if !path.exists()
            || voice_desktop_common::workspace::remove_owned(path, path.parent().unwrap()).is_ok()
        {
            break;
        }
        std::thread::sleep(Duration::from_millis(200));
    }
}

#[cfg(feature = "qualification")]
fn locator_for_check(args: &[String]) -> Result<Locator, String> {
    Locator::from_args(args)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ProductStartupKind {
    LocalCore,
    RemoteClient,
} // ProductStartupKind（正式程序启动路径：本地核心或远程客户端）。

/// product_startup_kind（选择正式程序服务路径）：远程客户端不经过本地核心启动门禁。
fn product_startup_kind(
    mode: Option<voice_desktop_common::device_config::RunMode>,
) -> ProductStartupKind {
    if mode == Some(voice_desktop_common::device_config::RunMode::Remote) {
        ProductStartupKind::RemoteClient
    } else {
        ProductStartupKind::LocalCore
    }
}

/// launch_mode_arguments（校验并读取模式参数）：重复、缺值和非法组合在写入配置前拒绝。
fn launch_mode_arguments(args: &[String]) -> Result<(Option<&str>, Option<&str>), String> {
    let mut requested_mode = None;
    let mut requested_broadcast = None;
    for (index, arg) in args.iter().enumerate() {
        let target = match arg.as_str() {
            "--mode" => &mut requested_mode,
            "--broadcast" => &mut requested_broadcast,
            _ => continue,
        };
        if target.is_some() {
            return Err(format!("启动参数重复：{arg}"));
        }
        let selected = args
            .get(index + 1)
            .filter(|value| !value.starts_with("--"))
            .map(String::as_str)
            .ok_or_else(|| format!("启动参数缺少值：{arg}"))?;
        match arg.as_str() {
            "--mode" if !matches!(selected, "local" | "remote") => {
                return Err(
                    "--mode（运行模式）仅接受 local（本地运行）或 remote（远程配对）。".into(),
                );
            }
            "--broadcast" if !matches!(selected, "on" | "off") => {
                return Err("--broadcast（局域网广播）仅接受 on（开启）或 off（关闭）。".into());
            }
            _ => *target = Some(selected),
        }
    }
    if requested_mode == Some("remote") && requested_broadcast == Some("on") {
        return Err(
            "--mode remote（远程配对）不能与 --broadcast on（开启本机广播）同时使用。".into(),
        );
    }
    Ok((requested_mode, requested_broadcast))
}

/// apply_launch_mode_override（应用正式启动模式覆盖）：只在参数明确请求时保存到设备配置。
fn apply_launch_mode_override(data_root: &Path, args: &[String]) -> Result<(), String> {
    if let Some(config) = launch_device_config(data_root, args)? {
        voice_desktop_common::device_config::save(data_root, &config)?;
    }
    Ok(())
}

fn launch_device_config(
    data_root: &Path,
    args: &[String],
) -> Result<Option<voice_desktop_common::device_config::DeviceConfig>, String> {
    let (requested_mode, requested_broadcast) = launch_mode_arguments(args)?;
    if requested_mode.is_none() && requested_broadcast.is_none() {
        return Ok(None);
    }

    use voice_desktop_common::device_config::{
        self, AudioMode, DeviceConfig, LoadResult, Role, RunMode,
    };
    let mut config = match device_config::load_from_root(data_root) {
        LoadResult::Valid(config) => config,
        LoadResult::Invalid(error) => return Err(format!("无法安全应用启动模式：{error}")),
        LoadResult::Missing => DeviceConfig {
            schema_version: device_config::SCHEMA_VERSION,
            device_id: format!("device-{}", voice_desktop_common::workspace::new_id()),
            mode: RunMode::Local,
            broadcast_enabled: false,
            client_online_tts_local: true,
            audio_compression: false,
            reconnect_suspended_reason: None,
            desired_role: Role::Standalone,
            audio_mode: AudioMode::Pcm,
            host: None,
        },
    };
    if let Some(mode) = requested_mode {
        config.mode = match mode {
            "local" => RunMode::Local,
            "remote" => RunMode::Remote,
            _ => return Err("--mode（运行模式）参数无效。".into()),
        };
    }
    if let Some(broadcast) = requested_broadcast {
        config.broadcast_enabled = match broadcast {
            "on" => true,
            "off" => false,
            _ => return Err("--broadcast（局域网广播）参数无效。".into()),
        };
    }
    if config.mode == RunMode::Remote && requested_broadcast == Some("on") {
        return Err("远程配对模式不能同时开启本机广播。".into());
    }
    if config.mode == RunMode::Remote {
        config.broadcast_enabled = false;
    }
    config.desired_role = match config.mode {
        RunMode::Remote => Role::RemoteClient,
        RunMode::Local if config.broadcast_enabled => Role::SharedHost,
        RunMode::Local => Role::Standalone,
    };
    config.schema_version = device_config::SCHEMA_VERSION;
    Ok(Some(config))
}

const AGENT_TRAY_ITEMS: [(&str, &str); 3] = [
    ("show", "打开工作台"), ("floating", "悬浮窗模式"), ("exit", "退出应用"),
];

fn handle_tray_item(app: &tauri::AppHandle, state: &Arc<Desktop>, id: &str) {
    match id {
        "show" => show(app),
        "floating" => {
            let enabled = !state.floating_mode.load(Ordering::Acquire);
            if let Err(error) = set_floating_mode(app, state, enabled) {
                set_floating_menu_checked(state, false);
                if enabled {
                    let _ = app.emit_to("main", "voice-terminal:floating-error", error);
                }
            }
        }
        "exit" => state.shutdown(app),
        _ => {}
    }
}

fn run() -> Result<(), String> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let preview_count = args
        .iter()
        .filter(|arg| arg.as_str() == "--ui-preview")
        .count();
    if preview_count > 1 {
        return Err("--ui-preview 不能重复。".into());
    }
    let preview = preview_count == 1;
    let exit_requested = args.iter().any(|arg| arg == "--exit");
    let agent_mode = args.iter().any(|arg| arg == "--agent-debug");
    let (context, mode, program, data, webview2) = if preview {
        let (program, _, data) = ui_preview_context(&args)?;
        voice_desktop_common::prepare_webview_access_paths(&data, None)?;
        (None, FrontendMode::UiPreview, program, data, None)
    } else {
        let context = LaunchContext::from_args()?;
        if agent_mode && (!voice_desktop_common::agent_debug::allowed(&context.data)?
            || voice_desktop_common::agent_debug::session_id()? == 0 || context.diagnostic || exit_requested)
        {
            return Err("调试模式需要已授权的正式交互桌面。".into());
        }
        voice_desktop_common::compatibility::migrate_device_runtime_config(&context.data)?;
        apply_launch_mode_override(&context.data, &args)?;
        context.prepare_webview_access()?;
        for (name, value) in context.environment() {
            std::env::set_var(name, value);
        }
        let program = context.program.clone();
        let data = context.install.join("cache/webview2");
        (Some(context), FrontendMode::Product, program, data, None)
    };
    std::env::remove_var("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER");
    if voice_desktop_common::webview::version().is_none() {
        voice_desktop_common::webview::guide("未检测到系统 WebView2（网页运行引擎）。");
        return Ok(());
    }
    // 管理页无需脚本引擎就绪；运行环境沿用分发器检测，管理页不重复弹安装引导。
    let locator = context.as_ref().map(|value| Locator::local(&value.install));
    let instance_identifier = instance_identifier(
        mode,
        if preview {
            Some(program.as_path())
        } else {
            context.as_ref().map(|value| value.install.as_path())
        },
    );
    let development = preview || context.as_ref().is_some_and(|value| value.dev);
    let state = Arc::new(Desktop {
        context: Mutex::new(context),
        initialization: Mutex::new(
            serde_json::json!({"phase":"正在检查工作区","completed_bytes":0,"total_bytes":0}),
        ),
        initialization_cancel: Arc::new(AtomicBool::new(false)),
        program,
        data,
        webview2,
        mode,
        development,
        instance_identifier,
        service: Mutex::new(None),
        device_worker: Mutex::new(None),
        #[cfg(feature = "ssh-device")]
        remote_desktop_helper: Mutex::new(None),
        #[cfg(all(test, feature = "ssh-device"))]
        playback_worker_test_responses: Mutex::new(VecDeque::new()),
        #[cfg(all(test, feature = "ssh-device"))]
        playback_worker_test_operations: Mutex::new(Vec::new()),
        #[cfg(feature = "ssh-device")]
        ssh_device: voice_desktop_common::ssh_device::SshDeviceManager::default(),
        #[cfg(feature = "ssh-device")]
        pairing_window: Mutex::new(None),
        #[cfg(feature = "ssh-device")]
        pairing_listener: tokio::sync::Mutex::new(None),
        #[cfg(feature = "ssh-device")]
        discovery_publication: Mutex::new(None),
        #[cfg(feature = "ssh-device")]
        remote_services: Mutex::new(None),
        #[cfg(feature = "ssh-device")]
        remote_import_transfers: Mutex::new(HashMap::new()),
        #[cfg(feature = "ssh-device")]
        discovery_targets: Mutex::new(std::collections::HashMap::new()),
        discovery_search_expires_at_ms: AtomicU64::new(0),
        #[cfg(feature = "ssh-device")]
        device_terminal: voice_desktop_common::device_terminal::DeviceTerminalManager::default(),
        #[cfg(feature = "ssh-device")]
        remote_task_channel: Mutex::new(None),
        #[cfg(feature = "ssh-device")]
        remote_task_channel_starting: AtomicBool::new(false),
        #[cfg(feature = "ssh-device")]
        remote_task_channel_error: Mutex::new(None),
        #[cfg(feature = "ssh-device")]
        remote_media_channel: Mutex::new(None),
        #[cfg(feature = "ssh-device")]
        remote_media_channel_starting: AtomicBool::new(false),
        remote_media_stop_revision: AtomicU64::new(0),
        #[cfg(feature = "ssh-device")]
        remote_media_channel_error: Mutex::new(None),
        #[cfg(feature = "ssh-device")]
        remote_voice_forwarder: Mutex::new(None),
        #[cfg(feature = "ssh-device")]
        remote_playback_control: tokio::sync::Mutex::new(RemotePlaybackControlState::default()),
        #[cfg(feature = "ssh-device")]
        remote_playback_stop_epoch: AtomicU64::new(0),
        service_transition: Mutex::new(()),
        #[cfg(feature = "ssh-device")]
        gateway_control: device_operation::GatewayControl::default(),
        #[cfg(feature = "ssh-device")]
        broadcast_access: Mutex::new(None),
        broadcast_error: Mutex::new(None),
        device_operation: device_operation::DeviceOperation::default(),
        #[cfg(feature = "ssh-device")]
        discovery_transition: tokio::sync::Mutex::new(()),
        credentials_guard: Mutex::new(()),
        service_epoch: AtomicU64::new(0),
        service_ready_epoch: AtomicU64::new(0),
        device_gateway_status: Mutex::new(
            serde_json::json!({"enabled":false,"ready":false,"error":""}),
        ),
        remote_client_connected: AtomicBool::new(false),
        locator,
        workspace_guard: Mutex::new(None),
        workspace_busy: AtomicBool::new(false),
        maintenance_gate: Mutex::new(()),
        log_export_busy: AtomicBool::new(false),
        log_flush_sequence: AtomicU64::new(0),
        log_flush_state: Mutex::new(LogFlushState {
            completed_request: 0,
            failed_request: 0,
        }),
        log_flush_changed: Condvar::new(),
        app_update: AppUpdateManager::new(),
        service_requested: AtomicBool::new(false),
        service_starting: AtomicBool::new(false),
        startup_skipped: AtomicBool::new(false),
        recovery: Mutex::new(runtime_recovery::RuntimeRecovery::default()),
        origin: Mutex::new(None),
        dsh_origin: Mutex::new(None),
        launch_token: Mutex::new(None),
        error: Mutex::new(None),
        stopping: AtomicBool::new(false),
        stopped: AtomicBool::new(false),
        tray: AtomicBool::new(false),
        floating_mode: AtomicBool::new(false),
        floating_ready: AtomicBool::new(false),
        floating_transition: Mutex::new(()),
        floating_revision: AtomicU64::new(0),
        floating_model: Mutex::new(
            serde_json::json!({"revision":0,"doubleClickMs":floating_double_click_ms(),"state":"standby","sessionActive":false,"audioLevels":[0,0,0,0,0],"silenced":true,"canControl":false,"caption":null}),
        ),
        floating_menu: Mutex::new(None),
    });
    // qualification-exit（验收正常退出）只接受独立验收配置，沿用实例自己的退出流程。
    let qualification_control = cfg!(feature = "qualification")
        && state
            .locator
            .as_ref()
            .is_some_and(|value| value.qualification.is_some());
    let qualification_exit =
        qualification_control && args.iter().any(|value| value == "--qualification-exit");
    let instance_state = state.clone();
    let build_state = state.clone();
    let handler: fn(tauri::ipc::Invoke) -> bool = tauri::generate_handler![
        agent_debug_set_allowed,agent_debug::agent_debug_trace_native,
        desktop_status,desktop_page_ready,device_config_status,device_config_save,device_discovery_search,device_pairing_window_open,device_pairing_window_close,device_pairing_window_status,device_pairing_connect,device_worker_start,device_worker_status,device_worker_audio_devices,device_worker_desktop_helper_health,
            device_worker_capture_start,device_worker_capture_next,device_worker_capture_stop,
        device_worker_playback_start,device_worker_playback_enqueue,device_worker_playback_snapshot,device_worker_playback_stop,
            device_ssh_probe,device_ssh_identity_create,device_ssh_identity_status,device_ssh_identity_delete,
            device_ssh_transport_identity_import,device_ssh_transport_identity_status,device_ssh_transport_identity_delete,
            device_ssh_connect,device_ssh_pairing_request,device_ssh_request,device_ssh_management_request,device_ssh_import_start,device_ssh_import_cancel,device_ssh_tts_asset_import,device_ssh_disconnect,device_ssh_status,device_ssh_task_channel,device_ssh_media_channel,device_ssh_voice_forwarder,
            publish_floating_model,get_floating_model,return_to_workbench,start_floating_drag,control_floating_session,
            skip_startup,
            export_logs,
            open_console,
            exit_application,
            focus_main,
            desktop_initialize,desktop_manage,desktop_start_service,desktop_cancel_init,
            model_credentials_status,model_credentials_save,inference_selection_save,tts_selection_status,tts_selection_save,audio_enhancements_settings,
            resource_install_plan,voice_environment_status,voice_environment_plan,voice_environment_configure,voice_environment_import,gpu_environment_register,gpu_environment_probe,environment_guide,
            workspace_info,workspace_choose,workspace_link,workspace_migrate,workspace_cleanup,app_integrity_status,app_integrity_download,app_integrity_check,app_update_check,app_update_download,app_update_cancel,project_open_repository,open_minimax_voice_library,
            resource_inventory,resource_jobs,resource_system_feature_status,resource_system_feature_enable,resource_system_feature_access,resource_download,resource_download_required,resource_import,resource_import_choose_sources,resource_import_start,resource_import_confirm,resource_open_source,resource_cancel,resource_reset_plan,resource_reset,
            aniflive_import_choose_folder,aniflive_import_start
    ];
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(move |app, arguments, _| {
            if arguments.iter().any(|value|value=="--exit")||qualification_control
                && arguments.iter().any(|value| value == "--qualification-exit")
            {
                instance_state.shutdown(app);
            } else if instance_state.mode == FrontendMode::Product
                && arguments.iter().any(|value| value == "--agent-debug") != agent_mode
            {
                show(app);
            } else if arguments.iter().any(|value| matches!(value.as_str(), "--mode" | "--broadcast"))
                && instance_state.mode == FrontendMode::Product
            {
                show(app);
                let state = instance_state.clone();
                let handle = app.clone();
                tauri::async_runtime::spawn(async move {
                    if let Err(error) = apply_saved_device_runtime(handle.clone(), state.clone(), arguments).await {
                        *state.error.lock().unwrap() = Some(error.clone());
                        let _ = handle.emit("desktop://device-runtime-error", serde_json::json!({
                            "error":error,"fields":{"error":"启动参数运行模式切换失败原因"}
                        }));
                    }
                });
            } else if instance_state.mode==FrontendMode::Product&&arguments.iter().any(|value|value=="--dev")!=instance_state.development {
                if let Some(context)=instance_state.context(){let _=workspace::atomic_json(&context.data.join("state/instance-conflict.json"),&serde_json::json!({"pid":std::process::id(),"existing_development":instance_state.development,"requested_development":arguments.iter().any(|value|value=="--dev"),"fields":{"pid":"已有正式进程编号","existing_development":"当前显式调试模式","requested_development":"请求的显式调试模式"}}));}
                std::thread::spawn(||voice_desktop_common::windows::error("当前工作区已运行另一种业务模式，请退出已有实例后重新启动。"));
            } else {
                show(app);
                if let Some(context)=instance_state.context(){let _=voice_desktop_common::installation::acknowledge(&context.install,&arguments);}
            }
        }))
        .manage(state.clone())
        .invoke_handler(handler)
        .setup(move |app| {
            if qualification_exit||exit_requested {
                build_state.shutdown(app.handle());
                return Ok(());
            }
            if let Some(context)=build_state.context(){
                *build_state.workspace_guard.lock().unwrap()=Some(voice_desktop_common::installation::WorkspaceGuard::acquire(&context.install).map_err(std::io::Error::other)?);
                initialization::install_manager(app.handle(),build_state.clone(),&context).map_err(std::io::Error::other)?;
                agent_debug::AgentDebug::install(app.handle(), build_state.clone(), agent_mode).map_err(std::io::Error::other)?;
            }
            let maintenance=build_state.context().map(|context|voice_desktop_common::component_maintenance::pending_repairs(&context).map(|pending|pending||context.install.join("tmp/native-install.json").exists())).transpose().map_err(std::io::Error::other)?.unwrap_or(false);
            create_window(app.handle(),build_state.clone(),maintenance).map_err(std::io::Error::other)?;
            if build_state.mode == FrontendMode::Product {
                app.add_capability(floating_capability(REMOTE_STATIC_PRODUCT_ORIGIN, 0).to_string())?;
                let [(show_id, show_label), (float_id, float_label), (exit_id, exit_label)] = AGENT_TRAY_ITEMS;
                let show_item = MenuItem::with_id(app, show_id, show_label, true, None::<&str>)?;
                let floating_item = CheckMenuItem::with_id(app, float_id, float_label, true, false, None::<&str>)?;
                let exit_item = MenuItem::with_id(app, exit_id, exit_label, true, None::<&str>)?;
                let menu = Menu::with_items(app, &[&show_item, &floating_item, &exit_item])?;
                *build_state.floating_menu.lock().unwrap() = Some(floating_item);
                let icon = app_icon::tray();
                let tray_state = build_state.clone();
                let tray = TrayIconBuilder::with_id("voice-terminal")
                    .icon(icon)
                    .tooltip("Voice Terminal · 语音终端")
                    .menu(&menu)
                    .on_menu_event(move |app, event| handle_tray_item(app, &tray_state, event.id.as_ref()))
                    .on_tray_icon_event(|tray, event| {
                        if matches!(
                            event,
                            TrayIconEvent::Click {
                                button: MouseButton::Left,
                                button_state: MouseButtonState::Up,
                                ..
                            }
                        ) {
                            show(tray.app_handle());
                        }
                    })
                    .build(app);
                build_state.tray.store(tray.is_ok(), Ordering::SeqCst);
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if matches!(event, tauri::WindowEvent::ScaleFactorChanged { .. }) {
                if let Ok(handle) = window.hwnd() {
                    // 跨屏幕改变显示缩放时，重新选择标题栏与任务栏的原生图标尺寸。
                    let _ = app_icon::apply(handle.0);
                    if let Some(tray) = window.app_handle().tray_by_id("voice-terminal") {
                        let _ = tray.set_icon(Some(app_icon::tray()));
                    }
                }
            }
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "main" {
                    let state = window.state::<Arc<Desktop>>();
                    if !state.stopped.load(Ordering::SeqCst) {
                        api.prevent_close();
                        if state.tray.load(Ordering::SeqCst) {
                            let _ = window.hide();
                        } else {
                            state.inner().shutdown(window.app_handle());
                        }
                    }
                } else if window.label() == "floating" {
                    let state = window.state::<Arc<Desktop>>();
                    if !state.stopped.load(Ordering::SeqCst) && !state.stopping.load(Ordering::SeqCst) {
                        api.prevent_close();
                        let _ = set_floating_mode(window.app_handle(), state.inner(), false);
                    }
                }
            }
        })
        .build({
            let mut context = tauri::generate_context!();
            context.config_mut().identifier = state.instance_identifier.clone();
            context
        })
        .map_err(|e| format!("桌面运行时不可用：{e}。请检查 WebView2（网页运行时）配置。"))?;
    let initialization_cache = state.data.clone();
    app.run(move |app, event| {
        if let tauri::RunEvent::ExitRequested { api, .. } = event {
            if !state.stopped.load(Ordering::SeqCst) {
                api.prevent_exit();
                state.shutdown(app);
            }
        }
    });
    cleanup_initialization_cache(&initialization_cache);
    Ok(())
}
fn main() {
    #[cfg(feature = "ssh-device")]
    if let Some(result) = voice_desktop_common::device_bridge::dispatch() {
        let code = match result {
            Ok(()) => 0,
            Err(message) => {
                eprintln!("SSH 设备桥接拒绝请求：{message}");
                1
            }
        };
        std::process::exit(code);
    }
    if let Some(result) = voice_desktop_common::device_worker::dispatch() {
        let code = match result {
            Ok(()) => 0,
            Err(message) => {
                eprintln!("{message}");
                1
            }
        };
        std::process::exit(code);
    }
    if let Some(result) = voice_desktop_common::process_host::dispatch() {
        let code = match result {
            Ok(code) => code,
            Err(message) => {
                eprintln!("{message}");
                1
            }
        };
        std::process::exit(code);
    }
    if let Some(result) = voice_desktop_common::windows::run_url_helper_if_requested() {
        if result.is_err() {
            std::process::exit(1);
        }
        return;
    }
    #[cfg(feature = "qualification")]
    if qualification::run_if_requested().is_some() {
        return;
    }
    voice_desktop_common::process_host::set_identity();
    if let Err(message) = run() {
        if message.contains("WebView2")
            || message.contains("webview")
            || message.contains("网页引擎")
        {
            voice_desktop_common::webview::guide(&message);
        } else {
            voice_desktop_common::windows::error(&message);
        }
    }
}

#[cfg(test)]
mod tests {
    #[cfg(feature = "ssh-device")]
    use super::recover_tts_asset_import_commit;
    #[cfg(feature = "ssh-device")]
    use super::recover_tts_asset_upload_receipt;
    #[cfg(feature = "ssh-device")]
    use super::remote_business_operation_allowed;
    #[cfg(feature = "ssh-device")]
    use super::remote_client_tts_playback_enabled;
    use super::{
        apply_launch_mode_override, command_allowed, console_url, desktop_status_ready,
        floating_capability, floating_page_url_allowed, floating_publisher_capability,
        frontend_capability, frontend_entry_url, instance_identifier, launch_mode_arguments,
        mode_runtime_projection, preview_entry_url_allowed, preview_page_url_allowed,
        product_entry_url_allowed, product_page_url_allowed, product_startup_kind,
        remote_static_product_page_url, request_service_start, require_command,
        require_local_device_root, ui_preview_context, valid_instance_id, valid_launch_token,
        DesktopCommand, FrontendMode, ProductStartupKind,
    };
    use std::sync::atomic::{AtomicBool, Ordering};
    #[cfg(feature = "ssh-device")]
    use std::sync::Arc;
    #[cfg(feature = "ssh-device")]
    use std::{path::PathBuf, time::Duration};
    use tauri::webview::PageLoadEvent;

    #[cfg(feature = "ssh-device")]
    #[test]
    fn dsh_approval_decision_is_a_fixed_remote_business_operation() {
        assert!(remote_business_operation_allowed("dsh.approval.decide"));
        assert!(!remote_business_operation_allowed("dsh.approval.execute"));
        assert!(!remote_business_operation_allowed(
            "dsh.approval.decide?url=anything"
        ));
    }

    #[test]
    fn launch_mode_override_rejects_broadcast_on_remote_without_mutating_saved_config() {
        use voice_desktop_common::device_config::{
            self, AudioMode, DeviceConfig, LoadResult, Role, RunMode,
        };
        let root = isolated_launch_mode_root();
        let remote = DeviceConfig {
            schema_version: device_config::SCHEMA_VERSION,
            device_id: "device-launch-test".into(),
            mode: RunMode::Remote,
            broadcast_enabled: false,
            client_online_tts_local: true,
            audio_compression: false,
            reconnect_suspended_reason: None,
            desired_role: Role::RemoteClient,
            audio_mode: AudioMode::Pcm,
            host: None,
        };
        device_config::save(&root, &remote).unwrap();

        assert!(apply_launch_mode_override(&root, &["--broadcast".into(), "on".into()],).is_err());
        assert!(
            matches!(device_config::load_from_root(&root), LoadResult::Valid(value) if value == remote)
        );

        assert!(apply_launch_mode_override(
            &root,
            &[
                "--mode".into(),
                "local".into(),
                "--mode".into(),
                "remote".into()
            ],
        )
        .is_err());
        assert!(
            apply_launch_mode_override(&root, &["--broadcast".into(), "maybe".into()]).is_err()
        );
        assert!(
            matches!(device_config::load_from_root(&root), LoadResult::Valid(value) if value == remote)
        );

        apply_launch_mode_override(
            &root,
            &[
                "--mode".into(),
                "local".into(),
                "--broadcast".into(),
                "on".into(),
            ],
        )
        .unwrap();
        assert!(
            matches!(device_config::load_from_root(&root), LoadResult::Valid(value)
            if value.mode == RunMode::Local && value.broadcast_enabled && value.desired_role == Role::SharedHost)
        );

        assert!(apply_launch_mode_override(
            &root,
            &[
                "--mode".into(),
                "remote".into(),
                "--broadcast".into(),
                "on".into()
            ],
        )
        .is_err());
        assert!(
            matches!(device_config::load_from_root(&root), LoadResult::Valid(value)
            if value.mode == RunMode::Local && value.broadcast_enabled)
        );
        std::fs::remove_dir_all(root).unwrap();
    }

    fn isolated_launch_mode_root() -> std::path::PathBuf {
        let root = std::env::temp_dir().join(format!(
            "voice-launch-mode-{}-{}",
            std::process::id(),
            voice_desktop_common::workspace::new_id(),
        ));
        std::fs::create_dir_all(&root).unwrap();
        root
    }

    #[test]
    fn native_local_resource_gate_rejects_remote_config_and_allows_local_default() {
        use voice_desktop_common::device_config::{self, AudioMode, DeviceConfig, Role, RunMode};
        let root = isolated_launch_mode_root();
        assert!(
            require_local_device_root(&root).is_ok(),
            "missing config uses the documented local default"
        );

        let remote = DeviceConfig {
            schema_version: device_config::SCHEMA_VERSION,
            device_id: "device-remote-gate".into(),
            mode: RunMode::Remote,
            broadcast_enabled: false,
            client_online_tts_local: true,
            audio_compression: false,
            reconnect_suspended_reason: None,
            desired_role: Role::RemoteClient,
            audio_mode: AudioMode::Pcm,
            host: None,
        };
        device_config::save(&root, &remote).unwrap();
        assert!(require_local_device_root(&root)
            .unwrap_err()
            .contains("只针对本机运行设备"));

        let mut local = remote;
        local.mode = RunMode::Local;
        local.desired_role = Role::Standalone;
        device_config::save(&root, &local).unwrap();
        assert!(require_local_device_root(&root).is_ok());
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn remote_launch_from_an_empty_data_workspace_selects_the_client_path_without_local_components()
    {
        use voice_desktop_common::device_config::{self, LoadResult, Role, RunMode};

        let root = isolated_launch_mode_root();
        apply_launch_mode_override(&root, &["--mode".into(), "remote".into()]).unwrap();

        let config = match device_config::load_from_root(&root) {
            LoadResult::Valid(config) => config,
            other => panic!("远程启动参数应在隔离空数据目录保存有效配置：{other:?}"),
        };
        assert_eq!(config.mode, RunMode::Remote);
        assert!(!config.broadcast_enabled);
        assert_eq!(config.desired_role, Role::RemoteClient);
        assert_eq!(
            product_startup_kind(Some(config.mode)),
            ProductStartupKind::RemoteClient
        );
        assert!(!root.join("python").exists());
        assert!(!root.join("models").exists());
        assert!(!root.join("credentials").exists());

        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn local_launch_with_broadcast_off_selects_the_local_core_path_consistently() {
        use voice_desktop_common::device_config::{self, LoadResult, Role, RunMode};

        let root = isolated_launch_mode_root();
        apply_launch_mode_override(
            &root,
            &[
                "--mode".into(),
                "local".into(),
                "--broadcast".into(),
                "off".into(),
            ],
        )
        .unwrap();

        let config = match device_config::load_from_root(&root) {
            LoadResult::Valid(config) => config,
            other => panic!("本地关闭广播参数应保存有效配置：{other:?}"),
        };
        assert_eq!(config.mode, RunMode::Local);
        assert!(!config.broadcast_enabled);
        assert_eq!(config.desired_role, Role::Standalone);
        assert_eq!(
            product_startup_kind(Some(config.mode)),
            ProductStartupKind::LocalCore
        );

        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn failed_mode_switch_keeps_the_runtime_error_and_the_observed_actual_mode() {
        use voice_desktop_common::device_config::RunMode;

        let (actual_mode, phase, error) = mode_runtime_projection(
            Some(RunMode::Remote),
            "standalone",
            true,
            Some("远程客户端服务启动失败：模拟启动错误。".into()),
            None,
        );
        assert_eq!(actual_mode, Some("local"));
        assert_eq!(phase, "failed");
        assert_eq!(
            error.as_deref(),
            Some("远程客户端服务启动失败：模拟启动错误。")
        );
    }

    #[test]
    fn remote_mode_waits_for_a_host_without_claiming_an_actual_mode() {
        use voice_desktop_common::device_config::RunMode;

        let (actual_mode, phase, error) =
            mode_runtime_projection(Some(RunMode::Remote), "none", false, None, None);
        assert_eq!(actual_mode, None);
        assert_eq!(phase, "waiting_for_remote");
        assert_eq!(error, None);
    }

    #[test]
    fn remote_static_product_entry_passes_preflight_before_local_service_origin_exists() {
        assert!(desktop_status_ready(FrontendMode::Product, false, true,));
        assert!(!desktop_status_ready(FrontendMode::Product, false, false,));
        assert!(!desktop_status_ready(FrontendMode::UiPreview, false, true,));
        assert!(desktop_status_ready(FrontendMode::UiPreview, true, false,));
    }

    #[test]
    fn bundled_remote_product_url_accepts_the_tauri_root_and_index_paths_only() {
        let tauri_root = tauri::Url::parse("tauri://localhost/").unwrap();
        let tauri_index = tauri::Url::parse("tauri://localhost/index.html").unwrap();
        let management = tauri::Url::parse("tauri://localhost/management.html").unwrap();
        let external = tauri::Url::parse("http://127.0.0.1/index.html").unwrap();

        assert!(remote_static_product_page_url(&tauri_root));
        assert!(remote_static_product_page_url(&tauri_index));
        assert!(!remote_static_product_page_url(&management));
        assert!(!remote_static_product_page_url(&external));
    }

    #[test]
    fn mode_argument_parser_rejects_duplicate_values_before_any_config_write() {
        assert!(launch_mode_arguments(&[
            "--mode".into(),
            "local".into(),
            "--mode".into(),
            "remote".into(),
        ])
        .is_err());
        assert!(launch_mode_arguments(&[
            "--mode".into(),
            "remote".into(),
            "--broadcast".into(),
            "on".into(),
        ])
        .is_err());
    }

    #[cfg(feature = "ssh-device")]
    #[test]
    fn client_tts_capability_uses_playback_setting_not_supplier_credentials() {
        use voice_desktop_common::device_config::{
            AudioMode, DeviceConfig, Role, RunMode, SCHEMA_VERSION,
        };

        let mut config = DeviceConfig {
            schema_version: SCHEMA_VERSION,
            device_id: "device-client-tts".into(),
            mode: RunMode::Remote,
            broadcast_enabled: false,
            client_online_tts_local: true,
            audio_compression: false,
            reconnect_suspended_reason: None,
            desired_role: Role::RemoteClient,
            audio_mode: AudioMode::Pcm,
            host: None,
        };

        // 此判断只接受设备配置，不读取客户端本机凭据库；服务端另行检查自己的在线语音凭据。
        assert_eq!(remote_client_tts_playback_enabled(&config), cfg!(windows));
        config.client_online_tts_local = false;
        assert!(!remote_client_tts_playback_enabled(&config));
    }

    #[cfg(feature = "ssh-device")]
    #[tokio::test]
    async fn tts_upload_recovery_requires_a_matching_authenticated_receipt() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        use voice_desktop_common::ssh_device::SftpTransferReceipt;

        let transfer_id = "a".repeat(32);
        let expected_bytes = 123;
        let retry_calls = AtomicUsize::new(0);
        let recovered = recover_tts_asset_upload_receipt(
            &transfer_id,
            expected_bytes,
            "初次上传回执丢失",
            || {
                retry_calls.fetch_add(1, Ordering::AcqRel);
                async {
                    Ok(SftpTransferReceipt {
                        path: format!("transfer:{transfer_id}"),
                        bytes: expected_bytes,
                        sha256: "b".repeat(64),
                    })
                }
            },
        )
        .await
        .unwrap();
        assert_eq!(retry_calls.load(Ordering::Acquire), 1);
        assert_eq!(recovered.path, format!("transfer:{transfer_id}"));
        assert_eq!(recovered.sha256, "b".repeat(64));

        let ambiguous = recover_tts_asset_upload_receipt(
            &transfer_id,
            expected_bytes,
            "初次上传回执丢失",
            || async { Err("文件服务暂不可用。".into()) },
        )
        .await
        .unwrap_err();
        assert!(ambiguous.contains("原票据已保留"));

        let invalid_receipt = recover_tts_asset_upload_receipt(
            &transfer_id,
            expected_bytes,
            "初次上传回执丢失",
            || async {
                Ok(SftpTransferReceipt {
                    path: format!("transfer:{}", "c".repeat(32)),
                    bytes: expected_bytes,
                    sha256: "B".repeat(64),
                })
            },
        )
        .await
        .unwrap_err();
        assert!(invalid_receipt.contains("原票据已保留"));
    }

    #[cfg(feature = "ssh-device")]
    #[tokio::test]
    async fn tts_commit_recovery_replays_the_same_idempotent_result_once() {
        use std::sync::atomic::{AtomicUsize, Ordering};

        let retry_calls = AtomicUsize::new(0);
        let committed = serde_json::json!({"asset":{"asset_id":"asset-1"}});
        let recovered =
            recover_tts_asset_import_commit(Err("首次提交响应连接中断".into()), || {
                retry_calls.fetch_add(1, Ordering::AcqRel);
                let committed = committed.clone();
                async move { Ok(committed) }
            })
            .await
            .unwrap();
        assert_eq!(retry_calls.load(Ordering::Acquire), 1);
        assert_eq!(recovered, committed);

        let confirmed = recover_tts_asset_import_commit(Ok(committed.clone()), || async {
            panic!("known successful commit must not be replayed")
        })
        .await
        .unwrap();
        assert_eq!(confirmed, committed);

        let unresolved = recover_tts_asset_import_commit(Err("首次响应未知".into()), || async {
            Err("同一请求重试仍未确认".into())
        })
        .await
        .unwrap_err();
        assert!(unresolved.contains("原传输票据和请求编号重试一次"));
        assert!(unresolved.contains("首次结果"));
        assert!(unresolved.contains("重试结果"));
    }

    #[cfg(all(feature = "ssh-device", feature = "native-audio"))]
    #[test]
    fn remote_capture_opus_encoder_preserves_twenty_millisecond_frame_metadata() {
        use super::encode_worker_media_frame_with_codec;
        use voice_desktop_common::{
            device_protocol::{decode_frame, AudioCodec},
            native_opus::{OpusDecoder, OpusEncoder},
        };

        let make_frame = |sequence: u64| {
            let pcm_le_bytes = (0..320_u64)
                .flat_map(|index| {
                    let sample = (8_000.0_f64
                        * (2.0 * std::f64::consts::PI * 330.0 * (sequence * 320 + index) as f64
                            / 16_000.0)
                            .sin()) as i16;
                    sample.to_le_bytes()
                })
                .map(u8::from)
                .collect::<Vec<_>>();
            serde_json::json!({
                "sequence":sequence,
                "start_sample":sequence * 320,
                "device_time_us":(sequence + 1) * 20_000,
                "discontinuity_before":false,
                "pcm_le_bytes":pcm_le_bytes,
                "audio_processing":{
                    "state":"ready", "near_end_voice":false,
                    "protected_interruption_available":true, "reverse_generation":4, "error":null,
                    "fields":{"state":"AEC、降噪与 VAD 健康状态",
                        "near_end_voice":"近端 VAD 判断汇总",
                        "protected_interruption_available":"是否允许受保护打断",
                        "reverse_generation":"播放参考格式代次", "error":"前处理故障摘要"}
                },
            })
        };

        let mut encoder = OpusEncoder::new(16_000, None).unwrap();
        let mut decoder = OpusDecoder::new(16_000).unwrap();
        assert!(encoder.lookahead_samples() > 0);
        for sequence in 0..8_u64 {
            let bytes = encode_worker_media_frame_with_codec(
                "device-a",
                "core-a",
                "media-a",
                2,
                3,
                4,
                &make_frame(sequence),
                AudioCodec::Opus,
                Some(&mut encoder),
            )
            .unwrap();
            let (header, packet) = decode_frame(&bytes).unwrap();
            assert_eq!(header.audio_codec, AudioCodec::Opus);
            assert_eq!(header.sequence, sequence);
            assert_eq!(header.start_sample, sequence * 320);
            assert_eq!(header.sample_count, 320);
            assert_eq!(header.audio_processing.reverse_generation, 4);
            assert_eq!(header.audio_processing.near_end_voice.0, Some(false));
            assert!(!packet.is_empty() && packet.len() <= 1275);
            assert_eq!(decoder.decode(packet).unwrap().len(), 320);
        }

        assert!(encode_worker_media_frame_with_codec(
            "device-a",
            "core-a",
            "media-a",
            2,
            3,
            4,
            &make_frame(8),
            AudioCodec::Opus,
            None,
        )
        .is_err());
    }

    #[cfg(feature = "ssh-device")]
    #[test]
    fn remote_capture_media_encoder_requires_valid_same_frame_processing_metadata() {
        use super::encode_worker_media_frame;
        use voice_desktop_common::device_protocol::{decode_frame, AudioCodec};

        let frame = |processing: serde_json::Value| {
            serde_json::json!({
                "sequence":0, "start_sample":0, "device_time_us":20_000,
                "discontinuity_before":false, "pcm_le_bytes":vec![0_u8; 640],
                "audio_processing":processing,
            })
        };
        let healthy = serde_json::json!({
            "state":"ready", "near_end_voice":true, "protected_interruption_available":true,
            "reverse_generation":7, "error":null,
            "fields":{"state":"AEC 与降噪健康状态", "near_end_voice":"近端语音结论",
                "protected_interruption_available":"受保护打断资格",
                "reverse_generation":"反向参考代次", "error":"前处理错误摘要"}
        });
        let bytes =
            encode_worker_media_frame("device-a", "core-a", "stream-a", 1, 2, 3, &frame(healthy))
                .unwrap();
        let (header, _) = decode_frame(&bytes).unwrap();
        assert_eq!(header.audio_codec, AudioCodec::Pcm);
        assert_eq!(header.audio_processing.reverse_generation, 7);
        assert_eq!(header.audio_processing.near_end_voice.0, Some(true));

        let missing_field = serde_json::json!({
            "state":"ready", "protected_interruption_available":true,
            "reverse_generation":7, "error":null,
            "fields":{"state":"AEC 状态", "near_end_voice":"近端语音",
                "protected_interruption_available":"打断保护", "reverse_generation":"参考代次",
                "error":"故障摘要"}
        });
        assert!(encode_worker_media_frame(
            "device-a",
            "core-a",
            "stream-a",
            1,
            2,
            3,
            &frame(missing_field),
        )
        .is_err());
        let invalid_health = serde_json::json!({
            "state":"degraded", "near_end_voice":null, "protected_interruption_available":true,
            "reverse_generation":7, "error":"处理器降级",
            "fields":{"state":"AEC 状态", "near_end_voice":"近端语音",
                "protected_interruption_available":"打断保护", "reverse_generation":"参考代次",
                "error":"故障摘要"}
        });
        assert!(encode_worker_media_frame(
            "device-a",
            "core-a",
            "stream-a",
            1,
            2,
            3,
            &frame(invalid_health),
        )
        .is_err());
    }

    #[cfg(all(feature = "ssh-device", feature = "native-audio"))]
    #[test]
    fn opus_uplink_worker_keeps_the_stateful_encoder_on_its_owned_thread() {
        use super::OpusEncoderWorker;
        use voice_desktop_common::native_opus::{OpusDecoder, OpusEncoder};

        let expected_pre_skip = OpusEncoder::new(16_000, None).unwrap().lookahead_samples();
        let worker = OpusEncoderWorker::start(expected_pre_skip).unwrap();
        let mut decoder = OpusDecoder::new(16_000).unwrap();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        runtime.block_on(async {
            let mut total_decoded_samples = 0;
            for sequence in 0..8_u64 {
                let pcm = (0..320_u64)
                    .flat_map(|index| {
                        let sample = (7_000.0_f64
                            * (2.0
                                * std::f64::consts::PI
                                * 220.0
                                * (sequence * 320 + index) as f64
                                / 16_000.0)
                                .sin()) as i16;
                        sample.to_le_bytes()
                    })
                    .collect::<Vec<_>>();
                let packet = worker.encode(pcm).await.unwrap();
                total_decoded_samples += decoder.decode(&packet).unwrap().len();
            }
            assert_eq!(total_decoded_samples, 8 * 320);
        });
    }

    #[cfg(feature = "ssh-device")]
    #[test]
    fn task_channel_status_reports_desktop_capability_separately() {
        tauri::async_runtime::block_on(async {
            let mut channel = super::RemoteDeviceTaskChannel {
                stop: Arc::new(AtomicBool::new(false)),
                running: Arc::new(AtomicBool::new(true)),
                device_id: "device-test".into(),
                connection_generation: 2,
                executor_generation: 3,
                desktop_control: Err("当前没有交互用户会话。".into()),
                task: tauri::async_runtime::spawn(async {}),
            };
            let limited = super::remote_device_task_status(Some(&channel), None);
            assert_eq!(limited["state"], "running");
            assert_eq!(limited["desktop_control"], false);
            assert_eq!(limited["desktop_control_error"], "当前没有交互用户会话。");
            assert!(limited["error"].is_null());

            channel.desktop_control = Ok(());
            let ready = super::remote_device_task_status(Some(&channel), None);
            assert_eq!(ready["desktop_control"], true);
            assert!(ready["desktop_control_error"].is_null());
            channel.running.store(false, Ordering::Release);
            let stopped = super::remote_device_task_status(Some(&channel), None);
            assert_eq!(stopped["state"], "stopped");
            assert!(stopped["desktop_control"].is_null());
            channel.task.await.unwrap();
        });
    }

    #[cfg(feature = "ssh-device")]
    fn replacement_test_desktop(responses: Vec<serde_json::Value>) -> Arc<super::Desktop> {
        replacement_test_desktop_with_manager(
            responses,
            voice_desktop_common::ssh_device::SshDeviceManager::default(),
        )
    }

    #[cfg(feature = "ssh-device")]
    fn replacement_test_desktop_with_manager(
        responses: Vec<serde_json::Value>,
        ssh_device: voice_desktop_common::ssh_device::SshDeviceManager,
    ) -> Arc<super::Desktop> {
        Arc::new(super::Desktop {
            context: std::sync::Mutex::new(None),
            initialization: std::sync::Mutex::new(serde_json::json!({"phase":"test"})),
            initialization_cancel: Arc::new(AtomicBool::new(false)),
            program: std::path::PathBuf::new(),
            data: std::path::PathBuf::new(),
            webview2: None,
            mode: FrontendMode::Product,
            development: false,
            instance_identifier: "replacement-test".into(),
            service: std::sync::Mutex::new(None),
            device_worker: std::sync::Mutex::new(None),
            #[cfg(feature = "ssh-device")]
            remote_desktop_helper: std::sync::Mutex::new(None),
            playback_worker_test_responses: std::sync::Mutex::new(responses.into()),
            playback_worker_test_operations: std::sync::Mutex::new(Vec::new()),
            ssh_device,
            pairing_window: std::sync::Mutex::new(None),
            pairing_listener: tokio::sync::Mutex::new(None),
            discovery_publication: std::sync::Mutex::new(None),
            remote_services: std::sync::Mutex::new(None),
            remote_import_transfers: std::sync::Mutex::new(std::collections::HashMap::new()),
            discovery_targets: std::sync::Mutex::new(std::collections::HashMap::new()),
            discovery_search_expires_at_ms: std::sync::atomic::AtomicU64::new(0),
            device_terminal: voice_desktop_common::device_terminal::DeviceTerminalManager::default(
            ),
            remote_task_channel: std::sync::Mutex::new(None),
            remote_task_channel_starting: AtomicBool::new(false),
            remote_task_channel_error: std::sync::Mutex::new(None),
            remote_media_channel: std::sync::Mutex::new(None),
            remote_media_channel_starting: AtomicBool::new(false),
            remote_media_stop_revision: std::sync::atomic::AtomicU64::new(0),
            remote_media_channel_error: std::sync::Mutex::new(None),
            remote_voice_forwarder: std::sync::Mutex::new(None),
            remote_playback_control: tokio::sync::Mutex::new(
                super::RemotePlaybackControlState::default(),
            ),
            remote_playback_stop_epoch: std::sync::atomic::AtomicU64::new(0),
            service_transition: std::sync::Mutex::new(()),
            gateway_control: super::device_operation::GatewayControl::default(),
            broadcast_access: std::sync::Mutex::new(None),
            broadcast_error: std::sync::Mutex::new(None),
            device_operation: super::device_operation::DeviceOperation::default(),
            discovery_transition: tokio::sync::Mutex::new(()),
            credentials_guard: std::sync::Mutex::new(()),
            service_epoch: std::sync::atomic::AtomicU64::new(0),
            service_ready_epoch: std::sync::atomic::AtomicU64::new(0),
            device_gateway_status: std::sync::Mutex::new(
                serde_json::json!({"enabled":false,"ready":false,"error":""}),
            ),
            remote_client_connected: AtomicBool::new(false),
            locator: None,
            workspace_guard: std::sync::Mutex::new(None),
            workspace_busy: AtomicBool::new(false),
            maintenance_gate: std::sync::Mutex::new(()),
            log_export_busy: AtomicBool::new(false),
            log_flush_sequence: std::sync::atomic::AtomicU64::new(0),
            log_flush_state: std::sync::Mutex::new(super::LogFlushState {
                completed_request: 0,
                failed_request: 0,
            }),
            log_flush_changed: std::sync::Condvar::new(),
            app_update: super::AppUpdateManager::new(),
            service_requested: AtomicBool::new(false),
            service_starting: AtomicBool::new(false),
            startup_skipped: AtomicBool::new(false),
            recovery: std::sync::Mutex::new(super::runtime_recovery::RuntimeRecovery::default()),
            origin: std::sync::Mutex::new(None),
            dsh_origin: std::sync::Mutex::new(None),
            launch_token: std::sync::Mutex::new(None),
            error: std::sync::Mutex::new(None),
            stopping: AtomicBool::new(false),
            stopped: AtomicBool::new(false),
            tray: AtomicBool::new(false),
            floating_mode: AtomicBool::new(false),
            floating_ready: AtomicBool::new(false),
            floating_transition: std::sync::Mutex::new(()),
            floating_revision: std::sync::atomic::AtomicU64::new(0),
            floating_model: std::sync::Mutex::new(serde_json::json!({})),
            floating_menu: std::sync::Mutex::new(None),
        })
    }

    #[cfg(feature = "ssh-device")]
    #[derive(Clone)]
    struct TauriTestForwardSshServer {
        gateway: std::net::SocketAddr, // gateway（测试时固定 exec 桥接连接的回环产品网关）。
        channels: Arc<
            tokio::sync::Mutex<
                std::collections::HashMap<russh::ChannelId, russh::Channel<russh::server::Msg>>,
            >,
        >, // channels（只暂存已接受的测试 SSH 会话通道）。
    }

    #[cfg(feature = "ssh-device")]
    impl russh::server::Server for TauriTestForwardSshServer {
        type Handler = Self;

        fn new_client(&mut self, _peer: Option<std::net::SocketAddr>) -> Self::Handler {
            self.clone()
        }
    }

    #[cfg(feature = "ssh-device")]
    impl russh::server::Handler for TauriTestForwardSshServer {
        type Error = russh::Error;

        async fn channel_open_session(
            &mut self,
            channel: russh::Channel<russh::server::Msg>,
            reply: russh::server::ChannelOpenHandle,
            _session: &mut russh::server::Session,
        ) -> Result<(), Self::Error> {
            let channel_id = channel.id();
            self.channels.lock().await.insert(channel_id, channel);
            reply.accept().await;
            Ok(())
        }

        async fn auth_publickey(
            &mut self,
            _user: &str,
            _key: russh::server::VerifiedPublicKey<'_>,
        ) -> Result<russh::server::Auth, Self::Error> {
            Ok(russh::server::Auth::Accept)
        }

        async fn exec_request(
            &mut self,
            channel_id: russh::ChannelId,
            command: &[u8],
            session: &mut russh::server::Session,
        ) -> Result<(), Self::Error> {
            if command != b"voice-terminal-v1-control" && command != b"voice-terminal-v1-media" {
                self.channels.lock().await.remove(&channel_id);
                let _ = session.channel_failure(channel_id);
                return Ok(());
            }
            let Some(channel) = self.channels.lock().await.remove(&channel_id) else {
                let _ = session.channel_failure(channel_id);
                return Ok(());
            };
            let target = match tokio::net::TcpStream::connect(self.gateway).await {
                Ok(target) => target,
                Err(_) => {
                    let _ = session.channel_failure(channel_id);
                    return Ok(());
                }
            };
            let _ = session.channel_success(channel_id);
            tokio::spawn(async move {
                let mut channel = channel.into_stream();
                let mut target = target;
                let _ = tokio::io::copy_bidirectional(&mut channel, &mut target).await;
            });
            Ok(())
        }
    }

    #[cfg(feature = "ssh-device")]
    struct TauriTestSshTunnel {
        handle: Option<russh::server::RunningServerHandle>, // handle（仅用于结束测试 SSH 服务）。
        task: Option<tokio::task::JoinHandle<Result<(), std::io::Error>>>, // task（仅测试回环 SSH 服务任务）。
        port: u16,               // port（动态分配的测试 SSH 入站端口）。
        host_key_sha256: String, // host_key_sha256（客户端实际核对的临时主机指纹）。
    }

    #[cfg(feature = "ssh-device")]
    impl TauriTestSshTunnel {
        async fn start(gateway: std::net::SocketAddr) -> Self {
            use russh::keys::{Algorithm, HashAlg, PrivateKey};
            use tokio::sync::oneshot;

            let host_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
            let host_key_sha256 = host_key
                .public_key()
                .fingerprint(HashAlg::Sha256)
                .to_string();
            let host_config = Arc::new(russh::server::Config {
                keys: vec![host_key],
                inactivity_timeout: Some(Duration::from_secs(30)),
                auth_rejection_time: Duration::ZERO,
                auth_rejection_time_initial: Some(Duration::ZERO),
                ..Default::default()
            });
            let (address_tx, address_rx) = oneshot::channel();
            let task = tokio::spawn(async move {
                let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
                let address = listener.local_addr()?;
                let mut server = TauriTestForwardSshServer {
                    gateway,
                    channels: Arc::new(tokio::sync::Mutex::new(std::collections::HashMap::new())),
                };
                let running =
                    russh::server::Server::run_on_socket(&mut server, host_config, &listener);
                let handle = running.handle();
                let _ = address_tx.send((address, handle));
                running.await
            });
            let (address, handle) = address_rx.await.expect("SSH 测试隧道应启动");
            Self {
                handle: Some(handle),
                task: Some(task),
                port: address.port(),
                host_key_sha256,
            }
        }

        async fn shutdown(mut self) {
            if let Some(handle) = self.handle.take() {
                handle.shutdown("Tauri SSH integration completed".into());
            }
            if let Some(task) = self.task.take() {
                task.await
                    .expect("SSH 测试服务应正常结束")
                    .expect("SSH 测试服务应正常关闭");
            }
        }
    }

    #[cfg(feature = "ssh-device")]
    impl Drop for TauriTestSshTunnel {
        fn drop(&mut self) {
            if let Some(handle) = self.handle.take() {
                handle.shutdown("Tauri SSH integration dropped".into());
            }
            if let Some(task) = self.task.take() {
                task.abort();
            }
        }
    }

    #[cfg(feature = "ssh-device")]
    struct TauriTestPythonGateway {
        child: Option<std::process::Child>, // child（只绑定本机回环端口的隔离测试服务进程）。
        gateway_port: u16,                  // gateway_port（SSH 隧道目标回环端口）。
        control_port: u16,                  // control_port（仅供测试启动/查询/退出的回环端口）。
    }

    #[cfg(feature = "ssh-device")]
    impl TauriTestPythonGateway {
        async fn start(public_key: &str) -> Self {
            use std::process::{Command, Stdio};
            use tokio::net::TcpStream;

            async fn available_port() -> u16 {
                let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
                let port = listener.local_addr().unwrap().port();
                drop(listener);
                port
            }

            // 集成测试为每个独立 Python 网关分配回环临时端口，避免并行测试互相抢占 18765。
            let gateway_port = available_port().await;
            let control_port = available_port().await;
            let repository = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("../../..")
                .canonicalize()
                .unwrap();
            let python = repository.join(".local/runtimes/python/python.exe");
            let fixture = repository.join("tests/fixtures/device-gateway-ssh-task-e2e-server.py");
            let mut child = Command::new(python)
                .args(["-B", "-X", "utf8"])
                .arg(fixture)
                .current_dir(&repository)
                .env("VOICE_DEVICE_E2E_ID", "test-device")
                .env("VOICE_DEVICE_E2E_PUBLIC_KEY", public_key)
                .env("VOICE_TERMINAL_HOST_ID", "test-host")
                .env("VOICE_DEVICE_E2E_GATEWAY_PORT", gateway_port.to_string())
                .env("VOICE_DEVICE_E2E_CONTROL_PORT", control_port.to_string())
                .stdout(Stdio::null())
                .stderr(Stdio::piped())
                .spawn()
                .expect("本机 Python 产品网关集成夹具应启动");
            for _ in 0..200 {
                if let Some(status) = child.try_wait().expect("读取 Python 产品网关状态失败")
                {
                    use std::io::Read;
                    let mut stderr = String::new();
                    if let Some(mut pipe) = child.stderr.take() {
                        let _ = pipe.read_to_string(&mut stderr);
                    }
                    panic!("Python 产品网关集成夹具提前退出：{status}；{stderr}");
                }
                let gateway_ready = TcpStream::connect(("127.0.0.1", gateway_port))
                    .await
                    .is_ok();
                let control_ready = TcpStream::connect(("127.0.0.1", control_port))
                    .await
                    .is_ok();
                if gateway_ready && control_ready {
                    return Self {
                        child: Some(child),
                        gateway_port,
                        control_port,
                    };
                }
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
            let _ = child.kill();
            let _ = child.wait();
            panic!("Python 产品网关集成夹具未能在限定时间内就绪");
        }

        async fn request(&self, method: &str, path: &str) -> (u16, serde_json::Value) {
            use tokio::io::{AsyncReadExt, AsyncWriteExt};
            let mut stream = tokio::net::TcpStream::connect(("127.0.0.1", self.control_port))
                .await
                .unwrap();
            let request = format!(
                "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                self.control_port
            );
            stream.write_all(request.as_bytes()).await.unwrap();
            let mut response = Vec::new();
            tokio::time::timeout(Duration::from_secs(15), stream.read_to_end(&mut response))
                .await
                .expect("Python 产品网关控制回执应返回")
                .expect("读取 Python 产品网关控制回执失败");
            let text = String::from_utf8(response).expect("测试控制回执应是 UTF-8");
            let (headers, body) = text
                .split_once("\r\n\r\n")
                .expect("HTTP 控制回执应包含消息体");
            let status = headers
                .lines()
                .next()
                .and_then(|line| line.split_whitespace().nth(1))
                .and_then(|value| value.parse::<u16>().ok())
                .expect("HTTP 状态码应有效");
            (
                status,
                serde_json::from_str(body).expect("测试控制回执应是 JSON"),
            )
        }

        async fn shutdown(mut self) {
            let _ = self.request("POST", "/__test__/shutdown").await;
            if let Some(mut child) = self.child.take() {
                for _ in 0..100 {
                    if child.try_wait().unwrap().is_some() {
                        return;
                    }
                    tokio::time::sleep(Duration::from_millis(25)).await;
                }
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }

    #[cfg(feature = "ssh-device")]
    impl Drop for TauriTestPythonGateway {
        fn drop(&mut self) {
            if let Some(mut child) = self.child.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }

    #[cfg(feature = "ssh-device")]
    #[test]
    fn tauri_replacement_handler_executes_real_tasks_over_a_pinned_ssh_channel() {
        use russh::keys::ssh_key::LineEnding;
        use russh::keys::{Algorithm, PrivateKey};
        use std::collections::HashMap;
        use std::sync::atomic::AtomicUsize;
        use voice_desktop_common::{
            ssh_device::{RemoteDeviceTaskResult, SshDeviceTarget},
            wasapi_render::PlaybackReplacementProgress,
        };

        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .unwrap();
        runtime.block_on(async {
            let product_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
            let product_key_text = product_key.to_openssh(LineEnding::LF).unwrap();
            let public_key = product_key.public_key().to_openssh().unwrap();
            let gateway = TauriTestPythonGateway::start(&public_key).await;
            let tunnel = TauriTestSshTunnel::start(
                std::net::SocketAddr::from(([127, 0, 0, 1], gateway.gateway_port)),
            ).await;
            let login_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
            let login_key_text = login_key.to_openssh(LineEnding::LF).unwrap();
            let target = SshDeviceTarget {
                address: "127.0.0.1".into(),
                port: tunnel.port,
                username: "test-device".into(),
                host_key_sha256: tunnel.host_key_sha256.clone(),
                gateway_port: gateway.gateway_port,
            };
            let manager = voice_desktop_common::ssh_device::SshDeviceManager::default();
            manager.connect_with_test_credentials(
                "test-device", target, &product_key_text, &login_key_text, Duration::from_secs(10),
            ).await.expect("Tauri 测试客户端应通过 SSH 固定主机指纹并连接产品网关");
            let lease = manager.open_remote_device_session_with_capabilities(
                vec!["audio_stream".into()], Duration::from_secs(10),
            ).await.expect("Python 产品网关应登记当前设备会话");
            assert_eq!(lease.device_id, "test-device");
            assert_eq!(lease.connection_generation, 1);
            assert_eq!(lease.executor_generation, 1);

            let management_request_id = "a".repeat(32);
            let management = manager.post_json("/v1/management", &serde_json::json!({
                "protocol_version":1,
                "request_id":management_request_id,
                "target_host_id":"test-host",
                "session_generation":lease.connection_generation,
                "operation":"product.snapshot",
                "arguments":{},
                "fields":{
                    "protocol_version":"远程管理协议版本",
                    "request_id":"唯一操作编号",
                    "target_host_id":"目标服务端设备编号",
                    "session_generation":"当前管理会话代次",
                    "operation":"固定业务操作编号",
                    "arguments":"类型化操作参数",
                },
            }), Duration::from_secs(10)).await
                .expect("固定 SSH 通道应把远程产品读取送到服务端业务处理器");
            assert_eq!(management.status, 200);
            assert_eq!(management.body["request_id"], "a".repeat(32));
            assert_eq!(management.body["target_host_id"], "test-host");
            assert_eq!(management.body["session_generation"], lease.connection_generation);
            assert_eq!(management.body["operation"], "product.snapshot");
            assert_eq!(management.body["status"], 200);
            assert!(management.body["body"]["settings"].is_object());

            let mut events = std::collections::BTreeMap::new();
            events.insert("cancel_accepted".into(), 9_000_000);
            events.insert("old_audio_silenced".into(), 9_240_000);
            let completed_progress = PlaybackReplacementProgress {
                replacement_id:"replace-a".into(), old_playback_key:"speech-old".into(),
                successor_playback_key:Some("speech-new".into()), reason:Some("early_handoff".into()),
                started_device_time_us:9_000_000, finished_device_time_us:Some(9_240_000),
                finished_output_estimated_ms:Some(400), events_device_time_us:events,
                attenuated_range_ms:[120,300], done:true,
            };
            let prepared_progress = PlaybackReplacementProgress {
                reason:None, finished_device_time_us:None, finished_output_estimated_ms:None,
                events_device_time_us:std::collections::BTreeMap::from([("cancel_accepted".into(), 9_000_000)]),
                attenuated_range_ms:[120,500], done:false,
                ..completed_progress.clone()
            };
            let prepared_snapshot = serde_json::json!({
                "enqueued_ms":500,"played_ms":200,"buffered_ms":300,
                "output_estimated_ms":200,"started":true,"device_time_us":9_000_000,
            });
            let final_snapshot = serde_json::json!({
                "enqueued_ms":800,"played_ms":400,"buffered_ms":40,
                "output_estimated_ms":400,"started":true,"device_time_us":9_240_000,
            });
            let desktop = replacement_test_desktop_with_manager(vec![
                serde_json::json!({"ok":true,"state":"ready","data":{
                    "replacement":prepared_progress,"snapshot":prepared_snapshot,
                }}),
                serde_json::json!({"ok":true,"state":"ready","data":{"bound":true}}),
                serde_json::json!({"ok":true,"state":"ready","data":{"replacement":{
                    "replacement_id":"replace-a","old_playback_key":"speech-old",
                    "successor_playback_key":"speech-new","reason":"early_handoff",
                    "started_device_time_us":9_000_000,"finished_device_time_us":9_240_000,
                    "finished_output_estimated_ms":400,
                    "events_device_time_us":{"cancel_accepted":9_000_000,"old_audio_silenced":9_240_000},
                    "attenuated_range_ms":[120,300],"done":true,
                }}}),
                serde_json::json!({"snapshot":final_snapshot}),
            ], manager);
            {
                let mut control = desktop.remote_playback_control.lock().await;
                *control = super::RemotePlaybackControlState {
                    device_id:Some(lease.device_id.clone()),
                    core_session_id:Some(lease.core_session_id.clone()),
                    connection_generation:lease.connection_generation,
                    executor_generation:lease.executor_generation,
                    interaction_generation:4,
                    active_key:Some("speech-old".into()),
                    baselines:HashMap::from([("speech-old".into(), super::PlaybackSnapshotBaseline {
                        enqueued_ms:100, played_ms:80, output_estimated_ms:100, isolated:true,
                    })]),
                    samples:HashMap::from([("speech-old".into(), (16_000, 8_000))]),
                    ..super::RemotePlaybackControlState::default()
                };
            }
            let (dispatch_status, dispatch_body) = gateway.request("POST", "/__test__/dispatch-replacement").await;
            assert_eq!(dispatch_status, 200, "{dispatch_body}");

            let stop = Arc::new(AtomicBool::new(false));
            let execute_stop = stop.clone();
            let execute_count = Arc::new(AtomicUsize::new(0));
            let execute_count_ref = execute_count.clone();
            let execute_state = desktop.clone();
            desktop.ssh_device.run_remote_device_task_channel_for_lease(
                lease, stop, move |task| {
                    let state = execute_state.clone();
                    let stop = execute_stop.clone();
                    let count = execute_count_ref.clone();
                    async move {
                        assert_eq!(task.device_id, "test-device");
                        assert_eq!(task.connection_generation, 1);
                        assert_eq!(task.executor_generation, 1);
                        let finish = task.operation == "playback.wait_replacement";
                        count.fetch_add(1, Ordering::AcqRel);
                        let result = super::execute_remote_device_task(state, task).await?;
                        if finish { stop.store(true, Ordering::Release); }
                        Ok::<RemoteDeviceTaskResult, String>(result)
                    }
                },
            ).await.expect("Tauri 实际设备任务分派器应经 SSH 轮询、执行并提交原任务回执");
            assert_eq!(execute_count.load(Ordering::Acquire), 3);
            assert_eq!(*desktop.playback_worker_test_operations.lock().unwrap(), vec![
                "prepare_replacement","bind_replacement","replacement_status","playback_snapshot",
            ]);
            desktop.ssh_device.disconnect().await.unwrap();
            tunnel.shutdown().await;

            let status = loop {
                let (status, body) = gateway.request("GET", "/__test__/replacement-status").await;
                if status != 202 { break (status, body); }
                tokio::time::sleep(Duration::from_millis(20)).await;
            };
            assert_eq!(status.0, 200, "{}", status.1);
            assert_eq!(status.1["state"], "succeeded");
            assert_eq!(status.1["replacement"]["bound"], true);
            let replacement_id = status.1["replacement"]["ticket"]["replacement_id"].as_str().unwrap();
            assert_eq!(status.1["replacement"]["result"]["replacement_id"], replacement_id);
            assert_eq!(status.1["replacement"]["result"]["reason"], "early_handoff");
            assert_eq!(status.1["replacement"]["result"]["snapshot"]["playback_key"], "speech-old");
            assert_eq!(status.1["replacement"]["result"]["snapshot"]["buffered_ms"], 0);
            gateway.shutdown().await;
        });
    }

    #[cfg(feature = "ssh-device")]
    #[test]
    fn tauri_task_result_survives_disconnect_and_reconciles_without_reexecution() {
        use russh::keys::ssh_key::LineEnding;
        use russh::keys::{Algorithm, PrivateKey};
        use voice_desktop_common::{
            device_receipts::DeviceTaskReceiptJournal, ssh_device::SshDeviceTarget,
        };

        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .unwrap();
        runtime.block_on(async {
            let product_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
            let product_key_text = product_key.to_openssh(LineEnding::LF).unwrap();
            let public_key = product_key.public_key().to_openssh().unwrap();
            let gateway = TauriTestPythonGateway::start(&public_key).await;
            let tunnel = TauriTestSshTunnel::start(std::net::SocketAddr::from((
                [127, 0, 0, 1],
                gateway.gateway_port,
            )))
            .await;
            let login_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
            let login_key_text = login_key.to_openssh(LineEnding::LF).unwrap();
            let target = SshDeviceTarget {
                address: "127.0.0.1".into(),
                port: tunnel.port,
                username: "test-device".into(),
                host_key_sha256: tunnel.host_key_sha256.clone(),
                gateway_port: gateway.gateway_port,
            };
            let manager = voice_desktop_common::ssh_device::SshDeviceManager::default();
            manager
                .connect_with_test_credentials(
                    "test-device",
                    target,
                    &product_key_text,
                    &login_key_text,
                    Duration::from_secs(10),
                )
                .await
                .expect("Tauri 回执恢复客户端应验证 SSH 主机指纹");
            let first_lease = manager
                .open_remote_device_session_with_capabilities(
                    vec!["audio_stream".into()],
                    Duration::from_secs(10),
                )
                .await
                .unwrap();
            let desktop = replacement_test_desktop_with_manager(Vec::new(), manager);
            {
                let mut control = desktop.remote_playback_control.lock().await;
                *control = super::RemotePlaybackControlState {
                    device_id: Some(first_lease.device_id.clone()),
                    core_session_id: Some(first_lease.core_session_id.clone()),
                    connection_generation: first_lease.connection_generation,
                    executor_generation: first_lease.executor_generation,
                    active_key: Some("speech-reconcile-e2e".into()),
                    paused: true,
                    ..super::RemotePlaybackControlState::default()
                };
            }

            let (dispatch_status, dispatch_body) = gateway
                .request("POST", "/__test__/dispatch-receipt-reconciliation")
                .await;
            assert_eq!(dispatch_status, 200, "{dispatch_body}");
            let task = loop {
                if let Some(task) = desktop
                    .ssh_device
                    .poll_remote_device_task(&first_lease, 1000, Duration::from_secs(8))
                    .await
                    .unwrap()
                {
                    break task;
                }
            };
            let outcome = super::execute_remote_device_task(desktop.clone(), task.clone())
                .await
                .unwrap();
            assert!(outcome.succeeded);
            assert_eq!(outcome.result["paused"], true);

            let journal_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("../../../.local/tmp")
                .join(format!(
                    "tauri-receipts-{}",
                    voice_desktop_common::workspace::new_id()
                ));
            let receipt_journal = DeviceTaskReceiptJournal::new(journal_root.clone());
            receipt_journal.record(&task, &outcome).unwrap();
            assert!(desktop
                .ssh_device
                .close_remote_device_session(&first_lease, Duration::from_secs(5),)
                .await
                .unwrap());

            let unknown = loop {
                let (status, body) = gateway
                    .request("GET", "/__test__/receipt-reconciliation-status")
                    .await;
                if status == 200 && body["state"] == "unknown" {
                    break body;
                }
                tokio::time::sleep(Duration::from_millis(20)).await;
            };
            assert_eq!(unknown["task_id"], task.task_id);
            assert_eq!(unknown["run_id"], first_lease.core_session_id);

            drop(receipt_journal);
            let receipt_journal = DeviceTaskReceiptJournal::new(journal_root.clone());
            let reopened = receipt_journal.pending_for_device("test-device").unwrap();
            assert_eq!(
                reopened.len(),
                1,
                "重建回执日志句柄后应从磁盘恢复原任务结果"
            );
            assert_eq!(reopened[0].task_id, task.task_id);

            let second_lease = desktop
                .ssh_device
                .open_remote_device_session_with_capabilities(
                    vec!["audio_stream".into()],
                    Duration::from_secs(10),
                )
                .await
                .unwrap();
            assert_ne!(
                second_lease.connection_generation,
                first_lease.connection_generation
            );
            desktop
                .ssh_device
                .run_remote_device_task_channel_for_lease_with_receipts(
                    second_lease,
                    Arc::new(AtomicBool::new(true)),
                    &receipt_journal,
                    move |_task| async move { panic!("重连恢复不得领取或重放新任务") },
                )
                .await
                .expect("Tauri 客户端应只把持久回执结算到原任务");
            assert!(receipt_journal
                .pending_for_device("test-device")
                .unwrap()
                .is_empty());

            let (status, settled) = gateway
                .request("GET", "/__test__/receipt-reconciliation-status")
                .await;
            assert_eq!(status, 200, "{settled}");
            assert_eq!(settled["state"], "succeeded");
            assert_eq!(settled["result"]["paused"], true);
            desktop.ssh_device.disconnect().await.unwrap();
            tunnel.shutdown().await;
            gateway.shutdown().await;
            let _ = std::fs::remove_dir_all(journal_root);
        });
    }

    #[cfg(feature = "ssh-device")]
    #[test]
    fn tauri_task_channel_does_not_poll_when_receipt_journal_is_full_or_corrupt() {
        use russh::keys::ssh_key::LineEnding;
        use russh::keys::{Algorithm, PrivateKey};
        use voice_desktop_common::{
            device_receipts::DeviceTaskReceiptJournal,
            ssh_device::{RemoteDeviceTask, RemoteDeviceTaskResult, SshDeviceTarget},
        };

        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .unwrap();
        runtime.block_on(async {
            let product_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
            let product_key_text = product_key.to_openssh(LineEnding::LF).unwrap();
            let public_key = product_key.public_key().to_openssh().unwrap();
            let gateway = TauriTestPythonGateway::start(&public_key).await;
            let tunnel = TauriTestSshTunnel::start(std::net::SocketAddr::from((
                [127, 0, 0, 1],
                gateway.gateway_port,
            )))
            .await;
            let login_key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519).unwrap();
            let login_key_text = login_key.to_openssh(LineEnding::LF).unwrap();
            let target = SshDeviceTarget {
                address: "127.0.0.1".into(),
                port: tunnel.port,
                username: "test-device".into(),
                host_key_sha256: tunnel.host_key_sha256.clone(),
                gateway_port: gateway.gateway_port,
            };
            let manager = voice_desktop_common::ssh_device::SshDeviceManager::default();
            manager
                .connect_with_test_credentials(
                    "test-device",
                    target,
                    &product_key_text,
                    &login_key_text,
                    Duration::from_secs(10),
                )
                .await
                .expect("回执准入测试应验证 SSH 主机指纹");

            let journal_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("../../../.local/tmp")
                .join(format!(
                    "tauri-receipt-admission-{}",
                    voice_desktop_common::workspace::new_id()
                ));
            let journal = DeviceTaskReceiptJournal::new(journal_root.clone());
            let outcome = RemoteDeviceTaskResult {
                succeeded: true,
                result: serde_json::json!({"ok":true}),
            };
            for sequence in 1..=64 {
                let id = format!("other-device-task-{sequence}");
                journal
                    .record(
                        &RemoteDeviceTask {
                            request_id: id.clone(),
                            device_id: "other-device".into(),
                            core_session_id: "prior-core".into(),
                            task_id: id,
                            connection_generation: 1,
                            executor_generation: 1,
                            sequence,
                            operation: "playback.stop_and_snapshot".into(),
                            arguments: serde_json::json!({}),
                        },
                        &outcome,
                    )
                    .unwrap();
            }
            assert!(journal.ensure_room().is_err());

            let first_lease = manager
                .open_remote_device_session_with_capabilities(
                    vec!["audio_stream".into()],
                    Duration::from_secs(10),
                )
                .await
                .unwrap();
            let stop_after_full = Arc::new(AtomicBool::new(false));
            let stop_timer = stop_after_full.clone();
            tokio::spawn(async move {
                tokio::time::sleep(Duration::from_millis(250)).await;
                stop_timer.store(true, Ordering::Release);
            });
            let full_result = tokio::time::timeout(
                Duration::from_secs(5),
                manager.run_remote_device_task_channel_for_lease_with_receipts(
                    first_lease,
                    stop_after_full,
                    &journal,
                    move |_task| async move {
                        Err::<RemoteDeviceTaskResult, String>("满额日志不得执行任务".into())
                    },
                ),
            )
            .await
            .expect("满额日志的任务通道应及时退出");
            let full_error = full_result.expect_err("满额回执日志必须阻止新任务轮询");
            assert!(full_error.contains("上限"), "{full_error}");
            assert_eq!(
                journal.pending_for_device("other-device").unwrap().len(),
                64
            );
            let (status, poll_status) =
                gateway.request("GET", "/__test__/session-poll-count").await;
            assert_eq!(status, 200, "{poll_status}");
            assert_eq!(poll_status["count"], 0, "满额时不得向主机轮询下一项任务");

            let receipt_path = journal_root.join("state/device-task-receipts.json");
            std::fs::write(&receipt_path, b"{broken").unwrap();
            let second_lease = manager
                .open_remote_device_session_with_capabilities(
                    vec!["audio_stream".into()],
                    Duration::from_secs(10),
                )
                .await
                .unwrap();
            let stop_after_corrupt = Arc::new(AtomicBool::new(false));
            let stop_timer = stop_after_corrupt.clone();
            tokio::spawn(async move {
                tokio::time::sleep(Duration::from_millis(250)).await;
                stop_timer.store(true, Ordering::Release);
            });
            let corrupt_result = tokio::time::timeout(
                Duration::from_secs(5),
                manager.run_remote_device_task_channel_for_lease_with_receipts(
                    second_lease,
                    stop_after_corrupt,
                    &journal,
                    move |_task| async move {
                        Err::<RemoteDeviceTaskResult, String>("损坏日志不得执行任务".into())
                    },
                ),
            )
            .await
            .expect("损坏日志的任务通道应及时退出");
            let corrupt_error = corrupt_result.expect_err("损坏回执日志必须阻止新任务轮询");
            assert!(corrupt_error.contains("损坏"), "{corrupt_error}");
            assert_eq!(
                std::fs::read(&receipt_path).unwrap(),
                b"{broken",
                "损坏日志原件必须保留"
            );
            let (status, poll_status) =
                gateway.request("GET", "/__test__/session-poll-count").await;
            assert_eq!(status, 200, "{poll_status}");
            assert_eq!(poll_status["count"], 0, "损坏时不得向主机轮询下一项任务");

            manager.disconnect().await.unwrap();
            tunnel.shutdown().await;
            gateway.shutdown().await;
            let _ = std::fs::remove_dir_all(journal_root);
        });
    }

    #[test]
    fn startup_only_admits_the_controlled_swapped_resource_transaction() {
        use super::{pending_start_transaction, ServiceStartKind};
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../.local/tmp")
            .join(format!(
                "startup-transaction-{}",
                voice_desktop_common::workspace::new_id()
            ));
        std::fs::create_dir_all(root.join("tmp")).unwrap();
        let journal = root.join("tmp/native-install.json");
        for phase in ["prepared", "swapped", "committed"] {
            std::fs::write(
                &journal,
                serde_json::json!({"state":phase,"target":"models/optional"}).to_string(),
            )
            .unwrap();
            assert!(pending_start_transaction(&root, ServiceStartKind::Startup));
            assert!(pending_start_transaction(
                &root,
                ServiceStartKind::RuntimeRecovery
            ));
            assert_eq!(
                pending_start_transaction(&root, ServiceStartKind::ResourceActivation),
                phase != "swapped"
            );
        }
        std::fs::write(&journal, "invalid").unwrap();
        assert!(pending_start_transaction(
            &root,
            ServiceStartKind::ResourceActivation
        ));
        std::fs::write(&journal, r#"{"state":"swapped"}"#).unwrap();
        std::fs::write(root.join("tmp/update-transaction.json"), "{}").unwrap();
        assert!(pending_start_transaction(
            &root,
            ServiceStartKind::ResourceActivation
        ));
        std::fs::remove_file(journal).unwrap();
        std::fs::remove_file(root.join("tmp/update-transaction.json")).unwrap();
        std::fs::remove_dir(root.join("tmp")).unwrap();
        std::fs::remove_dir(root).unwrap();
    }

    #[test]
    fn update_and_workspace_handoffs_reserve_a_single_maintenance_slot() {
        use super::reserve_maintenance;
        use std::sync::Mutex;
        let gate = Mutex::new(());
        let workspace = AtomicBool::new(false);
        let update = AtomicBool::new(false);
        let start = std::sync::Barrier::new(2);
        let outcomes = std::thread::scope(|scope| {
            let first = scope.spawn(|| {
                start.wait();
                reserve_maintenance(&gate, &workspace, &update, false, None).is_ok()
            });
            let second = scope.spawn(|| {
                start.wait();
                reserve_maintenance(&gate, &workspace, &update, true, None).is_ok()
            });
            [first.join().unwrap(), second.join().unwrap()]
        });
        assert_eq!(outcomes.iter().filter(|value| **value).count(), 1);
        assert_ne!(
            workspace.load(Ordering::Acquire),
            update.load(Ordering::Acquire)
        );
        workspace.store(false, Ordering::Release);
        update.store(false, Ordering::Release);
        reserve_maintenance(&gate, &workspace, &update, true, None).unwrap();
        assert!(reserve_maintenance(&gate, &workspace, &update, false, None).is_err());
        update.store(false, Ordering::Release);
        reserve_maintenance(&gate, &workspace, &update, false, None).unwrap();
        assert!(reserve_maintenance(&gate, &workspace, &update, true, None).is_err());
        workspace.store(false, Ordering::Release);
        let operation = super::device_operation::DeviceOperation::default();
        let pending = operation.begin("local", true).unwrap();
        assert!(reserve_maintenance(&gate, &workspace, &update, true, Some(&operation)).is_err());
        assert!(!update.load(Ordering::Acquire));
        pending.finish(None);
        reserve_maintenance(&gate, &workspace, &update, true, Some(&operation)).unwrap();
    }

    #[test]
    fn accepted_update_cancellation_and_installer_handoff_are_mutually_exclusive() {
        for _ in 0..64 {
            let manager = super::AppUpdateManager::new();
            manager.busy.store(true, Ordering::Release);
            *manager.status.lock().unwrap() =
                serde_json::json!({"state":"downloading","can_cancel":true});
            let start = std::sync::Barrier::new(2);
            let (cancelled, handing_off) = std::thread::scope(|scope| {
                let cancel = scope.spawn(|| {
                    start.wait();
                    manager.request_cancel().is_ok()
                });
                let handoff = scope.spawn(|| {
                    start.wait();
                    manager
                        .begin_handoff(
                            serde_json::json!({"state":"handing_off","can_cancel":false}),
                        )
                        .is_ok()
                });
                (cancel.join().unwrap(), handoff.join().unwrap())
            });
            assert_ne!(cancelled, handing_off);
            assert_eq!(manager.cancel.load(Ordering::Acquire), cancelled);
            assert_eq!(
                manager.status.lock().unwrap()["state"] == "handing_off",
                handing_off
            );
        }
    }

    #[test]
    fn startup_credentials_are_authorized_only_for_owned_local_windows() {
        let mut context: tauri::Context<tauri::Wry> = tauri::generate_context!();
        let authority = context.runtime_authority_mut(); // authority（实际编译的桌面权限解析器）。
        for command in [
            "workspace_cleanup",
            "resource_reset_plan",
            "resource_reset",
            "model_credentials_status",
            "model_credentials_save",
            "inference_selection_save",
            "tts_selection_status",
            "tts_selection_save",
            "audio_enhancements_settings",
            "resource_download_required",
            "aniflive_import_choose_folder",
            "aniflive_import_start",
            "app_integrity_status",
            "app_integrity_download",
            "app_integrity_check",
            "app_update_check",
            "app_update_download",
            "app_update_cancel",
            "project_open_repository",
            "open_minimax_voice_library",
        ] {
            for window in ["main", "startup"] {
                assert!(
                    authority
                        .resolve_access(command, window, window, &tauri::ipc::Origin::Local)
                        .is_some(),
                    "启动前密钥命令缺少权限：{command}"
                );
            }
            assert!(authority
                .resolve_access(command, "foreign", "foreign", &tauri::ipc::Origin::Local)
                .is_none());
            let origin = tauri::ipc::Origin::Remote {
                url: tauri::Url::parse("http://127.0.0.1:45001/").unwrap(),
            }; // origin（尚未授权的业务页面来源）。
            assert!(authority
                .resolve_access(command, "main", "main", &origin)
                .is_none());
        }
    }

    #[test]
    fn device_worker_commands_are_scoped_to_the_owned_page() {
        for mode in [FrontendMode::Product, FrontendMode::UiPreview] {
            let mut context: tauri::Context<tauri::Wry> = tauri::generate_context!();
            let authority = context.runtime_authority_mut();
            authority
                .add_capability(frontend_capability(mode, "http://127.0.0.1:45001", 7).to_string())
                .unwrap();
            for command in [
                "device_worker_start",
                "device_worker_status",
                "device_worker_audio_devices",
                "device_worker_desktop_helper_health",
                "device_worker_capture_start",
                "device_worker_capture_next",
                "device_worker_capture_stop",
                "device_worker_playback_start",
                "device_worker_playback_enqueue",
                "device_worker_playback_snapshot",
                "device_worker_playback_stop",
                "device_config_status",
                "device_config_save",
                "device_discovery_search",
                "device_pairing_window_open",
                "device_pairing_window_close",
                "device_pairing_window_status",
                "device_pairing_connect",
                "device_ssh_probe",
                "device_ssh_identity_create",
                "device_ssh_identity_status",
                "device_ssh_identity_delete",
                "device_ssh_transport_identity_import",
                "device_ssh_transport_identity_status",
                "device_ssh_transport_identity_delete",
                "device_ssh_connect",
                "device_ssh_pairing_request",
                "device_ssh_request",
                "device_ssh_management_request",
                "device_ssh_import_start",
                "device_ssh_import_cancel",
                "device_ssh_tts_asset_import",
                "device_ssh_disconnect",
                "device_ssh_status",
                "device_ssh_task_channel",
                "device_ssh_media_channel",
                "device_ssh_voice_forwarder",
            ] {
                let startup_access = authority
                    .resolve_access(command, "startup", "startup", &tauri::ipc::Origin::Local)
                    .is_some();
                let startup_expected = !matches!(
                    command,
                    "device_ssh_pairing_request"
                        | "device_ssh_import_start"
                        | "device_ssh_import_cancel"
                        | "device_ssh_tts_asset_import"
                );
                assert_eq!(
                    startup_access, startup_expected,
                    "启动页权限与产品页专属操作分离：{command}"
                );
                if matches!(
                    command,
                    "device_ssh_import_start" | "device_ssh_import_cancel" | "device_ssh_tts_asset_import"
                ) {
                    assert!(authority
                        .resolve_access(command, "main", "main", &tauri::ipc::Origin::Local)
                        .is_some(), "内置远程工作台缺少导入权限：{command}");
                }
                let owned = tauri::ipc::Origin::Remote {
                    url: tauri::Url::parse("http://127.0.0.1:45001/").unwrap(),
                };
                let remote_product_allowed = mode == FrontendMode::Product
                    && (!command.starts_with("device_ssh_") || cfg!(feature = "ssh-device"));
                let mode_name = if mode == FrontendMode::Product {
                    "Product"
                } else {
                    "UiPreview"
                };
                assert_eq!(
                    authority
                        .resolve_access(command, "main", "main", &owned)
                        .is_some(),
                    remote_product_allowed,
                    "模式 {mode_name} 对命令 {command} 的页面授权不符合能力声明"
                );
                let foreign = tauri::ipc::Origin::Remote {
                    url: tauri::Url::parse("http://127.0.0.1:45002/").unwrap(),
                };
                assert!(authority
                    .resolve_access(command, "main", "main", &foreign)
                    .is_none());
            }
        }
    }

    #[test]
    fn product_credentials_remain_scoped_to_current_origin_and_preview_cannot_use_them() {
        for mode in [FrontendMode::Product, FrontendMode::UiPreview] {
            let mut context: tauri::Context<tauri::Wry> = tauri::generate_context!();
            let authority = context.runtime_authority_mut();
            authority
                .add_capability(frontend_capability(mode, "http://127.0.0.1:45001", 7).to_string())
                .unwrap();
            for command in [
                "workspace_cleanup",
                "resource_reset_plan",
                "resource_reset",
                "model_credentials_status",
                "model_credentials_save",
                "inference_selection_save",
                "tts_selection_status",
                "tts_selection_save",
                "audio_enhancements_settings",
                "resource_download_required",
                "aniflive_import_choose_folder",
                "aniflive_import_start",
                "app_integrity_status",
                "app_integrity_download",
                "app_integrity_check",
                "app_update_check",
                "app_update_download",
                "app_update_cancel",
                "project_open_repository",
                "open_minimax_voice_library",
            ] {
                let owned = tauri::ipc::Origin::Remote {
                    url: tauri::Url::parse("http://127.0.0.1:45001/").unwrap(),
                }; // owned（当前实例业务页面）。
                assert_eq!(
                    authority
                        .resolve_access(command, "main", "main", &owned)
                        .is_some(),
                    mode == FrontendMode::Product
                );
                assert!(authority
                    .resolve_access(command, "startup", "startup", &owned)
                    .is_none());
                for address in ["http://127.0.0.1:45002/", "https://example.com/"] {
                    let foreign = tauri::ipc::Origin::Remote {
                        url: tauri::Url::parse(address).unwrap(),
                    }; // foreign（其他实例或外部页面）。
                    assert!(authority
                        .resolve_access(command, "main", "main", &foreign)
                        .is_none());
                }
            }
        }
    }

    #[test]
    fn floating_window_commands_are_scoped_to_their_owned_windows_and_origin() {
        let mut context: tauri::Context<tauri::Wry> = tauri::generate_context!();
        let authority = context.runtime_authority_mut();
        let origin_text = "http://127.0.0.1:45001";
        let origin = tauri::ipc::Origin::Remote {
            url: tauri::Url::parse("http://127.0.0.1:45001/floating.html").unwrap(),
        };
        authority
            .add_capability(floating_capability(origin_text, 9).to_string())
            .unwrap();
        authority
            .add_capability(floating_publisher_capability(origin_text, 9).to_string())
            .unwrap();
        authority
            .add_capability(floating_capability(super::REMOTE_STATIC_PRODUCT_ORIGIN, 0).to_string())
            .unwrap();

        for command in [
            "get_floating_model",
            "return_to_workbench",
            "start_floating_drag",
            "control_floating_session",
        ] {
            assert!(authority
                .resolve_access(command, "floating", "floating", &tauri::ipc::Origin::Local)
                .is_some());
            assert!(authority
                .resolve_access(command, "main", "main", &tauri::ipc::Origin::Local)
                .is_none());
            assert!(
                authority
                    .resolve_access(command, "floating", "floating", &origin)
                    .is_some(),
                "悬浮窗缺少受限权限：{command}"
            );
            assert!(
                authority
                    .resolve_access(command, "main", "main", &origin)
                    .is_none(),
                "主窗口不应拥有悬浮窗专属权限：{command}"
            );
            let foreign = tauri::ipc::Origin::Remote {
                url: tauri::Url::parse("http://127.0.0.1:45002/floating.html").unwrap(),
            };
            assert!(
                authority
                    .resolve_access(command, "floating", "floating", &foreign)
                    .is_none(),
                "其他来源不应拥有悬浮窗权限：{command}"
            );
        }
        assert!(authority
            .resolve_access("publish_floating_model", "main", "main", &origin)
            .is_some());
        assert!(authority
            .resolve_access("publish_floating_model", "floating", "floating", &origin)
            .is_none());
        assert!(authority
            .resolve_access("exit_application", "floating", "floating", &origin)
            .is_none());
    }

    #[test]
    fn floating_captions_reject_images_and_invalid_text_boundaries() {
        let valid = serde_json::json!({"key":"speech:1","role":"assistant","text":"你😊好","focusStart":1,"focusEnd":3,"active":true});
        assert_eq!(super::floating_caption_projection(Some(&valid)), valid);
        for invalid in [
            serde_json::json!({"key":"image:1","role":"tool","text":"截图","focusStart":0,"focusEnd":2}),
            serde_json::json!({"key":"speech:1","role":"assistant","text":"截图","focusStart":0,"focusEnd":2,"attachments":[]}),
            serde_json::json!({"key":"speech:1","role":"assistant","text":"你好","focusStart":0,"focusEnd":3}),
            serde_json::json!({"key":"speech:1","role":"assistant","text":" ","focusStart":0,"focusEnd":1}),
        ] {
            assert!(super::floating_caption_projection(Some(&invalid)).is_null());
        }
        assert!(super::floating_caption_projection(None).is_null());
    }

    #[test]
    fn services_start_only_once_after_the_final_main_window_page_loaded() {
        for address in [
            "http://tauri.localhost/",
            "tauri://localhost/",
            "http://tauri.localhost/index.html",
            "tauri://localhost/index.html",
        ] {
            let url = tauri::Url::parse(address).unwrap();
            let requested = AtomicBool::new(false);
            let stopping = AtomicBool::new(false);
            assert!(!request_service_start(
                &url,
                PageLoadEvent::Started,
                &requested,
                &stopping
            ));
            assert!(request_service_start(
                &url,
                PageLoadEvent::Finished,
                &requested,
                &stopping
            ));
            assert!(!request_service_start(
                &url,
                PageLoadEvent::Finished,
                &requested,
                &stopping
            ));
        }
    }

    #[test]
    fn closing_main_window_and_remote_pages_never_start_services() {
        let requested = AtomicBool::new(false);
        let stopping = AtomicBool::new(false);
        for address in [
            "http://127.0.0.1:5174/ui-demo.html",
            "http://127.0.0.1:5173/",
            "http://tauri.localhost/other",
        ] {
            assert!(!request_service_start(
                &tauri::Url::parse(address).unwrap(),
                PageLoadEvent::Finished,
                &requested,
                &stopping
            ));
        }
        stopping.store(true, Ordering::SeqCst);
        assert!(!request_service_start(
            &tauri::Url::parse("http://tauri.localhost/index.html").unwrap(),
            PageLoadEvent::Finished,
            &requested,
            &stopping
        ));
        assert!(!requested.load(Ordering::SeqCst));
    }

    #[test]
    fn console_url_preserves_launch_token() {
        let address = "http://127.0.0.1:3081/?token=launch%2Btoken%2Fvalue%3D";
        let url = console_url(address).unwrap();
        assert_eq!(url.as_str(), address);
        assert_eq!(url.query_pairs().next().unwrap().1, "launch+token/value=");
    }

    #[test]
    fn console_url_rejects_untrusted_or_incomplete_addresses() {
        for address in [
            "not a URL",
            "https://127.0.0.1:3081/?token=value",
            "http://example.com:3081/?token=value",
            "http://127.0.0.1:0/?token=value",
            "http://127.0.0.1:3081/other?token=value",
            "http://user:password@127.0.0.1:3081/?token=value",
            "http://127.0.0.1:3081/?token=value#fragment",
            "http://127.0.0.1:3081/",
            "http://127.0.0.1:3081/?token=",
            "http://127.0.0.1:3081/?token=value&token=other",
            "http://127.0.0.1:3081/?token=value&redirect=other",
        ] {
            assert!(console_url(address).is_err(), "应拒绝控制台地址：{address}");
        }
    }

    #[test]
    fn launch_token_and_instance_id_require_url_safe_values() {
        let token = "a".repeat(42) + "_";
        assert!(valid_launch_token(&token));
        let invalid_slash = "a".repeat(42) + "/";
        for token in ["", "short", invalid_slash.as_str()] {
            assert!(!valid_launch_token(token));
        }
        assert!(valid_instance_id("a1b2c3d4-e5f6"));
        assert!(!valid_instance_id("instance id"));
        assert!(!valid_instance_id(""));
    }

    #[test]
    fn frontend_entry_accepts_dynamic_loopback_port_and_encoded_token() {
        let token = "a".repeat(42) + "_";
        let entry =
            frontend_entry_url("http://127.0.0.1:5174/", &token, FrontendMode::UiPreview).unwrap();
        assert_eq!(entry.path(), "/desktop-entry");
        let query: Vec<_> = entry.query_pairs().collect();
        assert_eq!(query.len(), 1);
        assert_eq!(query[0].0, "token");
        assert_eq!(query[0].1, token);
        assert!(
            frontend_entry_url("http://127.0.0.1:45001/", &token, FrontendMode::UiPreview).is_ok()
        );
        for origin in [
            "http://127.0.0.1:0/",
            "http://localhost:5174/",
            "https://127.0.0.1:5174/",
            "http://example.com:5174/",
        ] {
            assert!(frontend_entry_url(origin, &token, FrontendMode::UiPreview).is_err());
        }
    }

    #[test]
    fn product_and_preview_navigation_allow_only_fixed_local_pages() {
        let product_entry = tauri::Url::parse(
            "http://127.0.0.1:5173/desktop-entry?token=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        .unwrap();
        assert!(product_entry_url_allowed(
            &product_entry,
            Some("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        ));
        assert!(!product_entry_url_allowed(&product_entry, None));

        for address in [
            "http://127.0.0.1:5173/",
            "http://127.0.0.1:5173/index.html?settings=system",
        ] {
            assert!(product_page_url_allowed(
                &tauri::Url::parse(address).unwrap()
            ));
        }
        let floating_page = tauri::Url::parse("http://127.0.0.1:5173/floating.html").unwrap();
        assert!(floating_page_url_allowed(&floating_page));
        assert!(!product_page_url_allowed(&floating_page));
        for address in [
            "http://127.0.0.1:5173/other",
            "http://127.0.0.1:5173/?token=secret",
            "http://127.0.0.1:5173/?settings=system&settings=silence",
        ] {
            assert!(!product_page_url_allowed(
                &tauri::Url::parse(address).unwrap()
            ));
        }

        let preview_entry = tauri::Url::parse(
            "http://127.0.0.1:5174/desktop-entry?token=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        .unwrap();
        assert!(preview_entry_url_allowed(
            &preview_entry,
            Some("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        ));
        for address in [
            "http://127.0.0.1:5174/ui-demo.html?page=wake&scene=error&runtime=speaking&overlay=confirm",
            "http://127.0.0.1:5174/ui-demo.html?replay=voice-shadow",
            "http://127.0.0.1:5174/ui-demo.html?page=wakes&session=active",
        ] {
            assert!(preview_page_url_allowed(
                &tauri::Url::parse(address).unwrap()
            ));
        }
        for address in [
            "http://127.0.0.1:5174/",
            "http://127.0.0.1:5174/index.html",
            "http://127.0.0.1:5174/ui-demo.html?open_console=1",
            "http://127.0.0.1:5174/ui-demo.html?page=home&page=wake",
            "http://127.0.0.1:5174/ui-demo.html?session=inactive",
            "http://127.0.0.1:5174/ui-demo.html?session=active&session=active",
        ] {
            assert!(!preview_page_url_allowed(
                &tauri::Url::parse(address).unwrap()
            ));
        }
    }

    #[test]
    fn source_product_preview_and_packaged_product_have_separate_instances() {
        let root = std::path::Path::new(r"D:\project\source");
        let other_root = std::path::Path::new(r"D:\project\other");
        let product = instance_identifier(FrontendMode::Product, Some(root));
        let preview = instance_identifier(FrontendMode::UiPreview, Some(root));
        assert_ne!(product, preview);
        assert_ne!(
            preview,
            instance_identifier(FrontendMode::UiPreview, Some(other_root))
        );
        assert_eq!(
            instance_identifier(FrontendMode::Product, None),
            "com.voiceterminal.desktop"
        );
        assert_eq!(
            product,
            instance_identifier(FrontendMode::Product, Some(root))
        );
    }

    #[test]
    fn preview_rejects_product_runtime_arguments_before_reading_root() {
        for args in [
            vec![
                "--ui-preview".into(),
                "--python".into(),
                "python.exe".into(),
            ],
            vec!["--ui-preview".into(), "--data-dir".into(), "data".into()],
            vec!["--ui-preview".into(), "--diagnostic".into()],
        ] {
            assert!(ui_preview_context(&args).is_err());
        }
    }

    #[test]
    fn preview_exposes_only_loading_status_and_exit_commands() {
        for command in [
            DesktopCommand::Status,
            DesktopCommand::SkipStartup,
            DesktopCommand::Exit,
        ] {
            assert!(command_allowed(FrontendMode::UiPreview, command));
            assert!(require_command(FrontendMode::UiPreview, command).is_ok());
            assert!(command_allowed(FrontendMode::Product, command));
        }
        for command in [
            DesktopCommand::ExportLogs,
            DesktopCommand::OpenConsole,
            DesktopCommand::FocusMain,
        ] {
            assert!(!command_allowed(FrontendMode::UiPreview, command));
            assert!(require_command(FrontendMode::UiPreview, command).is_err());
            assert!(command_allowed(FrontendMode::Product, command));
            assert!(require_command(FrontendMode::Product, command).is_ok());
        }
    }

    #[cfg(feature = "ssh-device")]
    #[test]
    fn remote_playback_worker_adapter_starts_snapshots_and_queues_in_order() {
        use super::{
            enqueue_remote_playback_to_worker, PlaybackSnapshotBaseline, PlaybackSnapshotView,
            RemotePlaybackControlState, MAX_REMOTE_PLAYBACK_KEYS,
        };
        use std::collections::HashMap;
        use std::sync::atomic::AtomicUsize;
        use std::sync::{Arc, Mutex};
        use voice_desktop_common::device_protocol::PlaybackMediaHeader;

        let header = |sequence, start_sample| PlaybackMediaHeader {
            protocol_version: voice_desktop_common::device_protocol::MEDIA_PROTOCOL_VERSION,
            direction: "playback".into(),
            device_id: "device-a".into(),
            delivery_device_id: "device-a".into(),
            core_session_id: "core-a".into(),
            connection_generation: 2,
            executor_generation: 3,
            interaction_generation: 4,
            stream_id: "media-a".into(),
            playback_key: "speech-a".into(),
            sequence,
            start_sample,
            sample_count: 320,
            sample_rate: 16_000,
            host_send_time_us: 100 + sequence,
            audio_codec: voice_desktop_common::device_protocol::AudioCodec::Pcm,
        };
        let mut control = RemotePlaybackControlState {
            device_id: Some("device-a".into()),
            core_session_id: Some("core-a".into()),
            connection_generation: 2,
            executor_generation: 3,
            interaction_generation: 4,
            ..RemotePlaybackControlState::default()
        };
        let mut baselines: HashMap<String, PlaybackSnapshotBaseline> = HashMap::new();
        let mut started = false;
        let starts = Arc::new(AtomicUsize::new(0));
        let snapshots = Arc::new(AtomicUsize::new(0));
        let enqueued = Arc::new(Mutex::new(Vec::<(String, u32, Vec<u8>)>::new()));
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();

        for (sequence, start_sample) in [(0, 0), (1, 320)] {
            let start_count = starts.clone();
            let snapshot_count = snapshots.clone();
            let enqueue_rows = enqueued.clone();
            runtime
                .block_on(enqueue_remote_playback_to_worker(
                    &mut control,
                    &mut baselines,
                    &mut started,
                    &header(sequence, start_sample),
                    16_000,
                    vec![sequence as u8; 640],
                    move || async move {
                        start_count.fetch_add(1, Ordering::AcqRel);
                        Ok(())
                    },
                    move || async move {
                        snapshot_count.fetch_add(1, Ordering::AcqRel);
                        Ok(PlaybackSnapshotView {
                            enqueued_ms: 40,
                            played_ms: 40,
                            buffered_ms: 0,
                            output_estimated_ms: 40,
                            started: true,
                            device_time_us: 9_000_000,
                        })
                    },
                    move |playback_key, sample_rate, pcm| async move {
                        enqueue_rows
                            .lock()
                            .unwrap()
                            .push((playback_key, sample_rate, pcm));
                        Ok(())
                    },
                ))
                .unwrap();
        }

        assert!(started);
        assert_eq!(starts.load(Ordering::Acquire), 1);
        assert_eq!(snapshots.load(Ordering::Acquire), 1);
        assert_eq!(baselines["speech-a"].isolated, true);
        assert_eq!(control.samples["speech-a"], (16_000, 640));
        assert_eq!(control.active_key.as_deref(), Some("speech-a"));
        let rows = enqueued.lock().unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0], ("speech-a".into(), 16_000, vec![0; 640]));
        assert_eq!(rows[1], ("speech-a".into(), 16_000, vec![1; 640]));

        for index in 0..(MAX_REMOTE_PLAYBACK_KEYS - control.samples.len()) {
            control
                .samples
                .insert(format!("unsettled-{index}"), (16_000, 320));
        }
        assert_eq!(control.samples.len(), MAX_REMOTE_PLAYBACK_KEYS);
        let mut over_capacity = header(2, 640);
        over_capacity.playback_key = "speech-over-capacity".into();
        let rejected_calls = Arc::new(AtomicUsize::new(0));
        let start_count = rejected_calls.clone();
        let snapshot_count = rejected_calls.clone();
        let enqueue_count = rejected_calls.clone();
        let rejected = runtime.block_on(enqueue_remote_playback_to_worker(
            &mut control,
            &mut baselines,
            &mut started,
            &over_capacity,
            16_000,
            vec![2; 640],
            move || async move {
                start_count.fetch_add(1, Ordering::AcqRel);
                Ok(())
            },
            move || async move {
                snapshot_count.fetch_add(1, Ordering::AcqRel);
                Ok(PlaybackSnapshotView {
                    enqueued_ms: 40,
                    played_ms: 40,
                    buffered_ms: 0,
                    output_estimated_ms: 40,
                    started: true,
                    device_time_us: 9_000_000,
                })
            },
            move |_, _, _| async move {
                enqueue_count.fetch_add(1, Ordering::AcqRel);
                Ok(())
            },
        ));
        assert!(rejected.unwrap_err().contains("播放键达到上限"));
        assert_eq!(rejected_calls.load(Ordering::Acquire), 0);
        assert!(!control.samples.contains_key("speech-over-capacity"));
    }

    #[cfg(feature = "ssh-device")]
    #[test]
    fn remote_playback_frame_injected_endpoint_queues_and_emits_receipts() {
        use super::{
            process_remote_playback_frame_for_lease, remote_playback_progress_value,
            PlaybackSnapshotBaseline, PlaybackSnapshotView, RemotePlaybackControlState,
            RemotePlaybackFrameOutcome,
        };
        use std::collections::HashMap;
        use std::sync::atomic::AtomicUsize;
        use std::sync::{Arc, Mutex};
        use voice_desktop_common::device_protocol::{encode_playback_frame, PlaybackMediaHeader};

        let header = PlaybackMediaHeader {
            protocol_version: voice_desktop_common::device_protocol::MEDIA_PROTOCOL_VERSION,
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
            host_send_time_us: 100,
            audio_codec: voice_desktop_common::device_protocol::AudioCodec::Pcm,
        };
        let pcm = vec![7_u8; 640];
        let bytes = encode_playback_frame(&header, &pcm).unwrap();
        let mut control = RemotePlaybackControlState {
            device_id: Some("device-a".into()),
            core_session_id: Some("core-a".into()),
            connection_generation: 2,
            executor_generation: 3,
            interaction_generation: 4,
            ..RemotePlaybackControlState::default()
        };
        let mut next_sequence = 0;
        let mut sample_positions = HashMap::new();
        let mut latest_sequences = HashMap::new();
        let mut baselines: HashMap<String, PlaybackSnapshotBaseline> = HashMap::new();
        let mut started = false;
        let starts = Arc::new(AtomicUsize::new(0));
        let snapshots = Arc::new(AtomicUsize::new(0));
        let enqueued = Arc::new(Mutex::new(Vec::<(String, u32, Vec<u8>)>::new()));
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();

        let start_count = starts.clone();
        let snapshot_count = snapshots.clone();
        let enqueue_rows = enqueued.clone();
        let outcome = runtime
            .block_on(process_remote_playback_frame_for_lease(
                &bytes,
                "device-a",
                "core-a",
                "media-a",
                2,
                3,
                4,
                &mut next_sequence,
                &mut sample_positions,
                &mut latest_sequences,
                &mut control,
                &mut baselines,
                &mut started,
                move || async move {
                    start_count.fetch_add(1, Ordering::AcqRel);
                    Ok(())
                },
                move || async move {
                    snapshot_count.fetch_add(1, Ordering::AcqRel);
                    Ok(PlaybackSnapshotView {
                        enqueued_ms: 0,
                        played_ms: 0,
                        buffered_ms: 0,
                        output_estimated_ms: 0,
                        started: false,
                        device_time_us: 9_000_000,
                    })
                },
                move |playback_key, sample_rate, pcm| async move {
                    enqueue_rows
                        .lock()
                        .unwrap()
                        .push((playback_key, sample_rate, pcm));
                    Ok(())
                },
            ))
            .unwrap();

        let RemotePlaybackFrameOutcome::Accepted { message } = outcome
        else {
            panic!("模拟播放端点应接纳有效帧");
        };
        let accepted: serde_json::Value = serde_json::from_str(&message).unwrap();
        assert_eq!(accepted["type"], "playback.accepted");
        assert_eq!(accepted["delivery_device_id"], "device-a");
        assert_eq!(accepted["connection_generation"], 2);
        assert_eq!(accepted["executor_generation"], 3);
        assert_eq!(accepted["interaction_generation"], 4);
        assert_eq!(accepted["sequence"], 0);
        assert_eq!(next_sequence, 1);
        assert_eq!(sample_positions["speech-a"], 320);
        assert_eq!(latest_sequences["speech-a"], 0);
        assert!(started);
        assert_eq!(starts.load(Ordering::Acquire), 1);
        assert_eq!(snapshots.load(Ordering::Acquire), 1);
        assert_eq!(control.samples["speech-a"], (16_000, 320));
        assert_eq!(
            enqueued.lock().unwrap().as_slice(),
            &[("speech-a".into(), 16_000, pcm.clone())]
        );

        let progress = remote_playback_progress_value(
            "device-a",
            "core-a",
            "media-a",
            2,
            3,
            4,
            "speech-a",
            0,
            1,
            baselines["speech-a"],
            PlaybackSnapshotView {
                enqueued_ms: 20,
                played_ms: 20,
                buffered_ms: 0,
                output_estimated_ms: 20,
                started: true,
                device_time_us: 9_020_000,
            },
            20,
        );
        assert_eq!(progress["type"], "playback.progress");
        assert_eq!(progress["delivery_device_id"], "device-a");
        assert_eq!(progress["playback_key"], "speech-a");
        assert_eq!(progress["receipt_sequence"], 1);
        assert_eq!(progress["sequence"], 0);
        assert_eq!(progress["enqueued_ms"], 20);
        assert_eq!(progress["played_ms"], 20);
        assert_eq!(progress["output_estimated_ms"], 20);
        assert_eq!(progress["started"], true);
        assert_eq!(progress["device_time_us"], 9_020_000);
        assert_eq!(progress["precision"], "device_time");

        control.control_uncertain = true;
        let mut next_header = header;
        next_header.sequence = 1;
        next_header.start_sample = 320;
        let next_bytes = encode_playback_frame(&next_header, &pcm).unwrap();
        let forbidden_calls = Arc::new(AtomicUsize::new(0));
        let start_count = forbidden_calls.clone();
        let snapshot_count = forbidden_calls.clone();
        let enqueue_count = forbidden_calls.clone();
        let dropped = runtime
            .block_on(process_remote_playback_frame_for_lease(
                &next_bytes,
                "device-a",
                "core-a",
                "media-a",
                2,
                3,
                4,
                &mut next_sequence,
                &mut sample_positions,
                &mut latest_sequences,
                &mut control,
                &mut baselines,
                &mut started,
                move || async move {
                    start_count.fetch_add(1, Ordering::AcqRel);
                    Ok(())
                },
                move || async move {
                    snapshot_count.fetch_add(1, Ordering::AcqRel);
                    Ok(PlaybackSnapshotView {
                        enqueued_ms: 20,
                        played_ms: 20,
                        buffered_ms: 0,
                        output_estimated_ms: 20,
                        started: true,
                        device_time_us: 9_020_000,
                    })
                },
                move |_playback_key, _sample_rate, _pcm| async move {
                    enqueue_count.fetch_add(1, Ordering::AcqRel);
                    Ok(())
                },
            ))
            .unwrap();
        let RemotePlaybackFrameOutcome::Cancelled { message, error: None } = dropped else {
            panic!("停止结果未知时必须返回该帧的明确取消回执");
        };
        let receipt: serde_json::Value = serde_json::from_str(&message).unwrap();
        assert_eq!(receipt["type"], "playback.cancelled");
        assert_eq!(receipt["sequence"], 1);
        assert_eq!(receipt["reason"], "output_unavailable");
        assert_eq!(forbidden_calls.load(Ordering::Acquire), 0);

        control.control_uncertain = false;
        let mut discontinuous_header = next_header.clone();
        discontinuous_header.sequence = 2;
        discontinuous_header.start_sample = 321;
        let discontinuous_bytes = encode_playback_frame(&discontinuous_header, &pcm).unwrap();
        let discontinuity_calls = Arc::new(AtomicUsize::new(0));
        let start_count = discontinuity_calls.clone();
        let snapshot_count = discontinuity_calls.clone();
        let enqueue_count = discontinuity_calls.clone();
        assert!(runtime
            .block_on(process_remote_playback_frame_for_lease(
                &discontinuous_bytes,
                "device-a",
                "core-a",
                "media-a",
                2,
                3,
                4,
                &mut next_sequence,
                &mut sample_positions,
                &mut latest_sequences,
                &mut control,
                &mut baselines,
                &mut started,
                move || async move {
                    start_count.fetch_add(1, Ordering::AcqRel);
                    Ok(())
                },
                move || async move {
                    snapshot_count.fetch_add(1, Ordering::AcqRel);
                    Ok(PlaybackSnapshotView {
                        enqueued_ms: 20,
                        played_ms: 20,
                        buffered_ms: 0,
                        output_estimated_ms: 20,
                        started: true,
                        device_time_us: 9_020_000,
                    })
                },
                move |_playback_key, _sample_rate, _pcm| async move {
                    enqueue_count.fetch_add(1, Ordering::AcqRel);
                    Ok(())
                },
            ))
            .is_err());
        assert_eq!(discontinuity_calls.load(Ordering::Acquire), 0);
        assert_eq!(next_sequence, 2);
    }

    #[cfg(feature = "ssh-device")]
    #[test]
    fn remote_replacement_drops_old_and_unbound_frames_then_accepts_the_fixed_successor() {
        use super::{
            process_remote_playback_frame_for_lease, remember_remote_replaced_key,
            PlaybackSnapshotBaseline, PlaybackSnapshotView, RemotePlaybackControlState,
            RemotePlaybackFrameOutcome, RemoteReplacementBinding,
        };
        use std::collections::HashMap;
        use std::sync::atomic::AtomicUsize;
        use std::sync::{Arc, Mutex};
        use voice_desktop_common::device_protocol::{encode_playback_frame, PlaybackMediaHeader};

        let make_frame = |key: &str, sequence: u64, start_sample: u64| {
            let header = PlaybackMediaHeader {
                protocol_version: voice_desktop_common::device_protocol::MEDIA_PROTOCOL_VERSION,
                direction: "playback".into(),
                device_id: "device-a".into(),
                delivery_device_id: "device-a".into(),
                core_session_id: "core-a".into(),
                connection_generation: 2,
                executor_generation: 3,
                interaction_generation: 4,
                stream_id: "media-a".into(),
                playback_key: key.into(),
                sequence,
                start_sample,
                sample_count: 320,
                sample_rate: 16_000,
                host_send_time_us: 100 + sequence,
                audio_codec: voice_desktop_common::device_protocol::AudioCodec::Pcm,
            };
            encode_playback_frame(&header, &vec![5; 640]).unwrap()
        };
        let mut control = RemotePlaybackControlState {
            device_id: Some("device-a".into()),
            core_session_id: Some("core-a".into()),
            connection_generation: 2,
            executor_generation: 3,
            interaction_generation: 4,
            pending_replacement: Some(RemoteReplacementBinding {
                replacement_id: "replacement-a".into(),
                old_playback_key: "speech-old".into(),
                successor_playback_key: None,
                candidate_successor_key: None,
                candidate_next_sample: 0,
            }),
            ..RemotePlaybackControlState::default()
        };
        remember_remote_replaced_key(&mut control, "speech-old");
        let mut next_sequence = 0;
        let mut sample_positions = HashMap::new();
        let mut latest_sequences = HashMap::new();
        let mut baselines: HashMap<String, PlaybackSnapshotBaseline> = HashMap::new();
        let mut started = false;
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let calls = Arc::new(AtomicUsize::new(0));
        let start_count = calls.clone();
        let snapshot_count = calls.clone();
        let enqueue_count = calls.clone();
        let old_result = runtime
            .block_on(process_remote_playback_frame_for_lease(
                &make_frame("speech-old", 0, 0),
                "device-a",
                "core-a",
                "media-a",
                2,
                3,
                4,
                &mut next_sequence,
                &mut sample_positions,
                &mut latest_sequences,
                &mut control,
                &mut baselines,
                &mut started,
                move || async move {
                    start_count.fetch_add(1, Ordering::AcqRel);
                    Ok(())
                },
                move || async move {
                    snapshot_count.fetch_add(1, Ordering::AcqRel);
                    Err("must not read".into())
                },
                move |_, _, _| async move {
                    enqueue_count.fetch_add(1, Ordering::AcqRel);
                    Err("must not enqueue".into())
                },
            ))
            .unwrap();
        let RemotePlaybackFrameOutcome::Cancelled { message, .. } = old_result else {
            panic!("旧播放键必须被拒绝")
        };
        let old_ack: serde_json::Value = serde_json::from_str(&message).unwrap();
        assert_eq!(old_ack["reason"], "superseded");
        assert!(!sample_positions.contains_key("speech-old"));
        assert_eq!(next_sequence, 1);
        assert_eq!(calls.load(Ordering::Acquire), 0);

        let unbound_calls = Arc::new(AtomicUsize::new(0));
        let start_count = unbound_calls.clone();
        let snapshot_count = unbound_calls.clone();
        let enqueue_count = unbound_calls.clone();
        let unbound_result = runtime
            .block_on(process_remote_playback_frame_for_lease(
                &make_frame("speech-new", 1, 0),
                "device-a",
                "core-a",
                "media-a",
                2,
                3,
                4,
                &mut next_sequence,
                &mut sample_positions,
                &mut latest_sequences,
                &mut control,
                &mut baselines,
                &mut started,
                move || async move {
                    start_count.fetch_add(1, Ordering::AcqRel);
                    Ok(())
                },
                move || async move {
                    snapshot_count.fetch_add(1, Ordering::AcqRel);
                    Err("must not read".into())
                },
                move |_, _, _| async move {
                    enqueue_count.fetch_add(1, Ordering::AcqRel);
                    Err("must not enqueue".into())
                },
            ))
            .unwrap();
        let RemotePlaybackFrameOutcome::Cancelled { message, .. } = unbound_result else {
            panic!("未绑定后继键必须被拒绝")
        };
        let unbound_ack: serde_json::Value = serde_json::from_str(&message).unwrap();
        assert_eq!(unbound_ack["reason"], "replacement_not_bound");
        assert!(!sample_positions.contains_key("speech-new"));
        assert_eq!(
            control
                .pending_replacement
                .as_ref()
                .unwrap()
                .candidate_next_sample,
            320
        );
        assert_eq!(next_sequence, 2);
        assert_eq!(unbound_calls.load(Ordering::Acquire), 0);

        control
            .pending_replacement
            .as_mut()
            .unwrap()
            .successor_playback_key = Some("speech-new".into());
        let accepted_rows = Arc::new(Mutex::new(Vec::<(String, u32, Vec<u8>)>::new()));
        let enqueue_rows = accepted_rows.clone();
        let accepted = runtime
            .block_on(process_remote_playback_frame_for_lease(
                &make_frame("speech-new", 2, 320),
                "device-a",
                "core-a",
                "media-a",
                2,
                3,
                4,
                &mut next_sequence,
                &mut sample_positions,
                &mut latest_sequences,
                &mut control,
                &mut baselines,
                &mut started,
                || async { Ok(()) },
                || async {
                    Ok(PlaybackSnapshotView {
                        enqueued_ms: 0,
                        played_ms: 0,
                        buffered_ms: 0,
                        output_estimated_ms: 0,
                        started: false,
                        device_time_us: 9_000_000,
                    })
                },
                move |key, rate, pcm| async move {
                    enqueue_rows.lock().unwrap().push((key, rate, pcm));
                    Ok(())
                },
            ))
            .unwrap();
        assert!(matches!(
            accepted,
            RemotePlaybackFrameOutcome::Accepted { .. }
        ));
        assert_eq!(accepted_rows.lock().unwrap()[0].0, "speech-new");
        assert_eq!(sample_positions["speech-new"], 640);
        assert_eq!(control.samples["speech-new"], (16_000, 320));
        assert_eq!(next_sequence, 3);
    }

    #[cfg(feature = "ssh-device")]
    #[test]
    fn remote_replacement_tasks_execute_once_and_reuse_the_settled_receipt() {
        use super::{
            execute_remote_playback_replacement_bind, execute_remote_playback_replacement_prepare,
            execute_remote_playback_replacement_wait, PlaybackSnapshotBaseline,
            RemotePlaybackControlState,
        };
        use std::collections::HashMap;
        use voice_desktop_common::{
            ssh_device::RemoteDeviceTask, wasapi_render::PlaybackReplacementProgress,
        };

        let mut events = std::collections::BTreeMap::new();
        events.insert("cancel_accepted".into(), 9_000_000);
        events.insert("old_audio_silenced".into(), 9_240_000);
        let completed_progress = PlaybackReplacementProgress {
            replacement_id: "replace-a".into(),
            old_playback_key: "speech-old".into(),
            successor_playback_key: Some("speech-new".into()),
            reason: Some("early_handoff".into()),
            started_device_time_us: 9_000_000,
            finished_device_time_us: Some(9_240_000),
            finished_output_estimated_ms: Some(400),
            events_device_time_us: events,
            attenuated_range_ms: [120, 300],
            done: true,
        };
        let prepared_progress = PlaybackReplacementProgress {
            reason: None,
            finished_device_time_us: None,
            finished_output_estimated_ms: None,
            events_device_time_us: std::collections::BTreeMap::from([(
                "cancel_accepted".into(),
                9_000_000,
            )]),
            attenuated_range_ms: [120, 500],
            done: false,
            ..completed_progress.clone()
        };
        let worker_snapshot = serde_json::json!({
            "enqueued_ms":500,"played_ms":200,"buffered_ms":300,
            "output_estimated_ms":200,"started":true,"device_time_us":9_000_000,
        });
        let final_worker_snapshot = serde_json::json!({
            "enqueued_ms":800,"played_ms":400,"buffered_ms":40,
            "output_estimated_ms":400,"started":true,"device_time_us":9_240_000,
        });
        let state = replacement_test_desktop(vec![
            serde_json::json!({"ok":true,"state":"ready","data":{
                "replacement":prepared_progress,"snapshot":worker_snapshot,
            }}),
            serde_json::json!({"ok":true,"state":"ready","data":{"bound":true}}),
            serde_json::json!({"ok":true,"state":"ready","data":{"replacement":completed_progress}}),
            serde_json::json!({"snapshot":final_worker_snapshot}),
        ]);
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        runtime.block_on(async {
            let mut control = state.remote_playback_control.lock().await;
            *control = RemotePlaybackControlState {
                device_id: Some("device-a".into()),
                core_session_id: Some("core-a".into()),
                connection_generation: 2,
                executor_generation: 3,
                interaction_generation: 4,
                active_key: Some("speech-old".into()),
                baselines: HashMap::from([(
                    "speech-old".into(),
                    PlaybackSnapshotBaseline {
                        enqueued_ms: 100,
                        played_ms: 80,
                        output_estimated_ms: 100,
                        isolated: true,
                    },
                )]),
                samples: HashMap::from([("speech-old".into(), (16_000, 8_000))]),
                ..RemotePlaybackControlState::default()
            };
        });
        let task =
            |request_id: &str, sequence: u64, operation: &str, arguments: serde_json::Value| {
                RemoteDeviceTask {
                    request_id: request_id.into(),
                    device_id: "device-a".into(),
                    core_session_id: "core-a".into(),
                    task_id: request_id.into(),
                    connection_generation: 2,
                    executor_generation: 3,
                    sequence,
                    operation: operation.into(),
                    arguments,
                }
            };

        let ticket_result = runtime
            .block_on(execute_remote_playback_replacement_prepare(
                state.clone(),
                task(
                    "replace-a",
                    1,
                    "playback.prepare_replacement",
                    serde_json::json!({"playback_key":"speech-old","max_ms":300}),
                ),
            ))
            .unwrap();
        assert!(ticket_result.succeeded);
        assert_eq!(ticket_result.result["replacement_id"], "replace-a");
        assert_eq!(
            ticket_result.result["snapshot"]["playback_key"],
            "speech-old"
        );

        let bind_result = runtime
            .block_on(execute_remote_playback_replacement_bind(
                state.clone(),
                task(
                    "bind-a",
                    2,
                    "playback.bind_replacement",
                    serde_json::json!({"replacement_id":"replace-a","playback_key":"speech-new"}),
                ),
            ))
            .unwrap();
        assert!(bind_result.succeeded);
        assert_eq!(bind_result.result["bound"], true);

        let wait_task = || {
            task(
                "wait-a",
                3,
                "playback.wait_replacement",
                serde_json::json!({"replacement_id":"replace-a"}),
            )
        };
        let wait_result = runtime
            .block_on(execute_remote_playback_replacement_wait(
                state.clone(),
                wait_task(),
            ))
            .unwrap();
        assert!(wait_result.succeeded);
        assert_eq!(wait_result.result["reason"], "early_handoff");
        assert_eq!(
            wait_result.result["attenuated_range_ms"],
            serde_json::json!([40, 220])
        );
        assert_eq!(wait_result.result["snapshot"]["output_estimated_ms"], 300);
        assert_eq!(wait_result.result["snapshot"]["buffered_ms"], 0);

        let repeated = runtime
            .block_on(execute_remote_playback_replacement_wait(
                state.clone(),
                task(
                    "wait-again",
                    4,
                    "playback.wait_replacement",
                    serde_json::json!({"replacement_id":"replace-a"}),
                ),
            ))
            .unwrap();
        assert_eq!(repeated, wait_result);
        assert_eq!(
            *state.playback_worker_test_operations.lock().unwrap(),
            vec![
                "prepare_replacement",
                "bind_replacement",
                "replacement_status",
                "playback_snapshot",
            ]
        );
    }

    #[cfg(feature = "ssh-device")]
    #[test]
    fn remote_replacement_result_matches_the_strict_python_wire_shape() {
        use super::{
            remote_replacement_result_value, PlaybackSnapshotBaseline, PlaybackSnapshotView,
            RemotePlaybackControlState,
        };
        use std::collections::HashMap;
        use voice_desktop_common::wasapi_render::PlaybackReplacementProgress;

        let mut control = RemotePlaybackControlState::default();
        control.baselines.insert(
            "speech-old".into(),
            PlaybackSnapshotBaseline {
                enqueued_ms: 0,
                played_ms: 80,
                output_estimated_ms: 0,
                isolated: true,
            },
        );
        control.samples.insert("speech-old".into(), (16_000, 8_000));
        let mut events = HashMap::new();
        events.insert("cancel_accepted".into(), 9_000_000);
        events.insert("old_audio_silenced".into(), 9_250_000);
        let progress = PlaybackReplacementProgress {
            replacement_id: "replacement-a".into(),
            old_playback_key: "speech-old".into(),
            successor_playback_key: Some("speech-new".into()),
            reason: Some("early_handoff".into()),
            started_device_time_us: 9_000_000,
            finished_device_time_us: Some(9_250_000),
            finished_output_estimated_ms: Some(300),
            events_device_time_us: events.into_iter().collect(),
            attenuated_range_ms: [120, 300],
            done: true,
        };
        let result = remote_replacement_result_value(
            &control,
            &progress,
            PlaybackSnapshotView {
                enqueued_ms: 500,
                played_ms: 320,
                buffered_ms: 30,
                output_estimated_ms: 300,
                started: true,
                device_time_us: 9_250_000,
            },
        )
        .unwrap();
        assert_eq!(
            result
                .as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect::<std::collections::BTreeSet<_>>(),
            [
                "replacement_id",
                "reason",
                "started_device_time_us",
                "finished_device_time_us",
                "events_device_time_us",
                "attenuated_range_ms",
                "snapshot"
            ]
            .into_iter()
            .collect()
        );
        assert_eq!(result["snapshot"]["playback_key"], "speech-old");
        assert_eq!(result["snapshot"]["precision"], "device_time");
        assert_eq!(result["attenuated_range_ms"], serde_json::json!([40, 220]));
    }

    #[cfg(feature = "ssh-device")]
    #[test]
    fn remote_media_playback_frame_is_bound_to_lease_and_global_sequence() {
        use super::decode_remote_playback_frame_for_lease;
        use voice_desktop_common::device_protocol::{encode_playback_frame, PlaybackMediaHeader};

        let header = PlaybackMediaHeader {
            protocol_version: voice_desktop_common::device_protocol::MEDIA_PROTOCOL_VERSION,
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
            host_send_time_us: 100,
            audio_codec: voice_desktop_common::device_protocol::AudioCodec::Pcm,
        };
        let pcm = [7_u8; 640];
        let bytes = encode_playback_frame(&header, &pcm).unwrap();
        let (decoded, received) = decode_remote_playback_frame_for_lease(
            &bytes, "device-a", "core-a", "media-a", 2, 3, 4, 0,
        )
        .unwrap();
        assert_eq!(decoded.playback_key, "speech-a");
        assert_eq!(received, pcm);

        let mut wrong_target = header.clone();
        wrong_target.device_id = "device-b".into();
        wrong_target.delivery_device_id = "device-b".into();
        let bytes = encode_playback_frame(&wrong_target, &pcm).unwrap();
        assert!(decode_remote_playback_frame_for_lease(
            &bytes, "device-a", "core-a", "media-a", 2, 3, 4, 0,
        )
        .is_err());

        let mut wrong_generation = header.clone();
        wrong_generation.executor_generation += 1;
        let bytes = encode_playback_frame(&wrong_generation, &pcm).unwrap();
        assert!(decode_remote_playback_frame_for_lease(
            &bytes, "device-a", "core-a", "media-a", 2, 3, 4, 0,
        )
        .is_err());

        let mut out_of_sequence = header.clone();
        out_of_sequence.sequence = 1;
        let bytes = encode_playback_frame(&out_of_sequence, &pcm).unwrap();
        assert!(decode_remote_playback_frame_for_lease(
            &bytes, "device-a", "core-a", "media-a", 2, 3, 4, 0,
        )
        .is_err());
    }

    #[cfg(feature = "ssh-device")]
    #[test]
    fn remote_playback_rejects_opus_that_was_not_negotiated() {
        use super::{
            process_remote_playback_frame_for_lease, PlaybackSnapshotBaseline,
            PlaybackSnapshotView, RemotePlaybackControlState,
        };
        use std::collections::HashMap;
        use std::sync::{
            atomic::{AtomicUsize, Ordering},
            Arc,
        };
        use voice_desktop_common::device_protocol::{
            encode_playback_frame, AudioCodec, PlaybackMediaHeader,
        };

        let header = PlaybackMediaHeader {
            protocol_version: voice_desktop_common::device_protocol::MEDIA_PROTOCOL_VERSION,
            direction: "playback".into(),
            device_id: "device-a".into(),
            delivery_device_id: "device-a".into(),
            core_session_id: "core-a".into(),
            connection_generation: 2,
            executor_generation: 3,
            interaction_generation: 4,
            stream_id: "media-a".into(),
            playback_key: "speech-opus".into(),
            sequence: 0,
            start_sample: 0,
            sample_count: 648,
            sample_rate: 48_000,
            host_send_time_us: 100,
            audio_codec: AudioCodec::Opus,
        };
        let packet = vec![0x5a; 500];
        let bytes = encode_playback_frame(&header, &packet).unwrap();
        let mut next_sequence = 0;
        let mut sample_positions = HashMap::new();
        let mut latest_sequences = HashMap::new();
        let mut playback_control = RemotePlaybackControlState {
            device_id: Some("device-a".into()),
            core_session_id: Some("core-a".into()),
            connection_generation: 2,
            executor_generation: 3,
            interaction_generation: 4,
            ..RemotePlaybackControlState::default()
        };
        let mut baselines = HashMap::<String, PlaybackSnapshotBaseline>::new();
        let mut playback_started = false;
        let invoked = Arc::new(AtomicUsize::new(0));
        let start_invoked = invoked.clone();
        let snapshot_invoked = invoked.clone();
        let enqueue_invoked = invoked.clone();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let result = runtime.block_on(process_remote_playback_frame_for_lease(
            &bytes,
            "device-a",
            "core-a",
            "media-a",
            2,
            3,
            4,
            &mut next_sequence,
            &mut sample_positions,
            &mut latest_sequences,
            &mut playback_control,
            &mut baselines,
            &mut playback_started,
            move || async move {
                start_invoked.fetch_add(1, Ordering::AcqRel);
                Ok(())
            },
            move || async move {
                snapshot_invoked.fetch_add(1, Ordering::AcqRel);
                Ok(PlaybackSnapshotView {
                    enqueued_ms: 0,
                    played_ms: 0,
                    buffered_ms: 0,
                    output_estimated_ms: 0,
                    started: false,
                    device_time_us: 100,
                })
            },
            move |_key, _rate, _pcm| async move {
                enqueue_invoked.fetch_add(1, Ordering::AcqRel);
                Ok(())
            },
        ));
        let error = result
            .err()
            .expect("与签名握手不匹配的 Opus 播放编码应被拒绝");
        assert!(error.contains("协商结果不一致"));
        assert_eq!(invoked.load(Ordering::Acquire), 0);
        assert_eq!(next_sequence, 0);
        assert!(!playback_started);
        assert!(playback_control.samples.is_empty());
    }

    #[cfg(all(feature = "ssh-device", feature = "native-audio"))]
    #[test]
    fn remote_playback_decodes_opus_per_key_drops_pre_skip_once_and_advances_cancelled_frames() {
        use super::{
            process_remote_playback_frame_for_lease, PlaybackSnapshotBaseline,
            PlaybackSnapshotView, RemotePlaybackControlState, RemotePlaybackFrameOutcome,
        };
        use std::collections::HashMap;
        use std::sync::{Arc, Mutex};
        use voice_desktop_common::{
            device_protocol::{encode_playback_frame, AudioCodec, PlaybackMediaHeader},
            native_opus::{OpusDecoder, OpusEncoder},
        };

        let mut encoder = OpusEncoder::new(48_000, None).unwrap();
        let pre_skip = encoder.lookahead_samples();
        let first_source = (0..960)
            .map(|index| ((index as f64 * 0.07).sin() * 7_000.0) as i16)
            .collect::<Vec<_>>();
        let second_source = (960..1_920)
            .map(|index| ((index as f64 * 0.07).sin() * 7_000.0) as i16)
            .collect::<Vec<_>>();
        let first_packet = encoder.encode(&first_source).unwrap();
        let second_packet = encoder.encode(&second_source).unwrap();
        let third_source = (1_920..2_880)
            .map(|index| ((index as f64 * 0.07).sin() * 7_000.0) as i16)
            .collect::<Vec<_>>();
        let third_packet = encoder.encode(&third_source).unwrap();
        let mut reference_decoder = OpusDecoder::new(48_000).unwrap();
        let first_reference = reference_decoder.decode(&first_packet).unwrap();
        let second_reference = reference_decoder.decode(&second_packet).unwrap();
        let first_expected = first_reference[pre_skip as usize..]
            .iter()
            .flat_map(|sample| sample.to_le_bytes())
            .collect::<Vec<_>>();
        let second_expected = second_reference
            .iter()
            .flat_map(|sample| sample.to_le_bytes())
            .collect::<Vec<_>>();

        let header = |sequence, start_sample, sample_count, payload: &[u8]| {
            let header = PlaybackMediaHeader {
                protocol_version: voice_desktop_common::device_protocol::MEDIA_PROTOCOL_VERSION,
                direction: "playback".into(),
                device_id: "device-a".into(),
                delivery_device_id: "device-a".into(),
                core_session_id: "core-a".into(),
                connection_generation: 2,
                executor_generation: 3,
                interaction_generation: 4,
                stream_id: "media-a".into(),
                playback_key: "speech-opus".into(),
                sequence,
                start_sample,
                sample_count,
                sample_rate: 48_000,
                host_send_time_us: 100 + sequence,
                audio_codec: AudioCodec::Opus,
            };
            encode_playback_frame(&header, payload).unwrap()
        };
        let packets = [
            (
                header(0, 0, 960 - u64::from(pre_skip), &first_packet),
                first_expected,
            ),
            (
                header(1, 960 - u64::from(pre_skip), 960, &second_packet),
                second_expected,
            ),
        ];
        let mut next_sequence = 0;
        let mut sample_positions = HashMap::new();
        let mut latest_sequences = HashMap::new();
        let mut playback_control = RemotePlaybackControlState {
            device_id: Some("device-a".into()),
            core_session_id: Some("core-a".into()),
            connection_generation: 2,
            executor_generation: 3,
            interaction_generation: 4,
            downlink_codec: AudioCodec::Opus,
            downlink_encoder_pre_skip_samples: pre_skip,
            ..RemotePlaybackControlState::default()
        };
        let mut baselines = HashMap::<String, PlaybackSnapshotBaseline>::new();
        let mut playback_started = false;
        let captured = Arc::new(Mutex::new(Vec::<(u32, Vec<u8>)>::new()));
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();

        for (bytes, _expected) in &packets {
            let output = captured.clone();
            let result = runtime
                .block_on(process_remote_playback_frame_for_lease(
                    bytes,
                    "device-a",
                    "core-a",
                    "media-a",
                    2,
                    3,
                    4,
                    &mut next_sequence,
                    &mut sample_positions,
                    &mut latest_sequences,
                    &mut playback_control,
                    &mut baselines,
                    &mut playback_started,
                    || async { Ok(()) },
                    || async {
                        Ok(PlaybackSnapshotView {
                            enqueued_ms: 0,
                            played_ms: 0,
                            buffered_ms: 0,
                            output_estimated_ms: 0,
                            started: false,
                            device_time_us: 100,
                        })
                    },
                    move |_key, rate, pcm| async move {
                        output.lock().unwrap().push((rate, pcm));
                        Ok(())
                    },
                ))
                .unwrap();
            assert!(matches!(
                result,
                RemotePlaybackFrameOutcome::Accepted { .. }
            ));
        }
        let captured = captured.lock().unwrap();
        assert_eq!(captured.len(), 2);
        assert_eq!(captured[0], (48_000, packets[0].1.clone()));
        assert_eq!(captured[1], (48_000, packets[1].1.clone()));
        assert_eq!(playback_control.samples["speech-opus"], (48_000, 1_608));
        assert_eq!(next_sequence, 2);

        playback_control.stopped_keys.insert("speech-opus".into());
        let late_frame = header(2, 1_608, 960, &third_packet);
        let cancelled = runtime
            .block_on(process_remote_playback_frame_for_lease(
                &late_frame,
                "device-a",
                "core-a",
                "media-a",
                2,
                3,
                4,
                &mut next_sequence,
                &mut sample_positions,
                &mut latest_sequences,
                &mut playback_control,
                &mut baselines,
                &mut playback_started,
                || async { panic!("已停止的 Opus 播放键不能重启输出") },
                || async { panic!("已停止的 Opus 播放键不能查询输出快照") },
                |_key, _rate, _pcm| async {
                    panic!("已停止的 Opus 帧不能进入输出队列")
                },
            ))
            .unwrap();
        assert!(matches!(
            cancelled,
            RemotePlaybackFrameOutcome::Cancelled { .. }
        ));
        assert_eq!(next_sequence, 3);
        assert_eq!(
            playback_control.opus_decoders["speech-opus"].pre_skip_remaining,
            0
        );
    }

    #[cfg(feature = "ssh-device")]
    #[test]
    fn remote_playback_tracking_prunes_terminal_maps_and_bounds_tombstones() {
        use super::{
            prune_remote_playback_tracking, remember_remote_stopped_key, PlaybackSnapshotBaseline,
            RemotePlaybackControlState,
        };
        use std::collections::HashMap;

        let mut control = RemotePlaybackControlState::default();
        let mut sample_positions = HashMap::new();
        let mut latest_sequences = HashMap::new();
        let mut baselines = HashMap::new();
        for index in 0_u64..300 {
            let key = format!("speech-{index}");
            remember_remote_stopped_key(&mut control, &key);
            sample_positions.insert(key.clone(), index);
            latest_sequences.insert(key.clone(), index);
            baselines.insert(
                key.clone(),
                PlaybackSnapshotBaseline {
                    enqueued_ms: 20,
                    played_ms: 0,
                    output_estimated_ms: 0,
                    isolated: false,
                },
            );
            prune_remote_playback_tracking(
                &control,
                &mut sample_positions,
                &mut latest_sequences,
                &mut baselines,
            );
        }
        assert_eq!(control.stopped_keys.len(), 256);
        assert_eq!(control.stopped_key_order.len(), 256);
        assert!(!control.stopped_keys.contains("speech-0"));
        assert!(control.stopped_keys.contains("speech-299"));
        assert!(sample_positions.is_empty());
        assert!(latest_sequences.is_empty());
        assert!(baselines.is_empty());
    }

    #[cfg(feature = "ssh-device")]
    #[test]
    fn remote_playback_stop_snapshot_is_bound_to_the_key_baseline() {
        use super::{PlaybackSnapshotBaseline, PlaybackSnapshotView, RemotePlaybackControlState};
        let mut state = RemotePlaybackControlState::default();
        state.baselines.insert(
            "speech-key".into(),
            PlaybackSnapshotBaseline {
                enqueued_ms: 120,
                played_ms: 90,
                output_estimated_ms: 80,
                isolated: true,
            },
        );
        state.samples.insert("speech-key".into(), (16_000, 320));
        let snapshot = state
            .snapshot_value(
                "speech-key",
                PlaybackSnapshotView {
                    enqueued_ms: 140,
                    played_ms: 110,
                    buffered_ms: 30,
                    output_estimated_ms: 100,
                    started: true,
                    device_time_us: 9_000_000,
                },
            )
            .unwrap();
        assert_eq!(snapshot["playback_key"], "speech-key");
        assert_eq!(snapshot["enqueued_ms"], 20);
        assert_eq!(snapshot["played_ms"], 20);
        assert_eq!(snapshot["buffered_ms"], 30);
        assert_eq!(snapshot["output_estimated_ms"], 20);
        assert_eq!(snapshot["started"], true);
        assert_eq!(snapshot["device_time_us"], 9_000_000);
        assert_eq!(snapshot["precision"], "device_time");
    }

    #[cfg(feature = "ssh-device")]
    #[test]
    fn remote_playback_pause_resume_is_idempotent_and_quarantines_unknown() {
        use super::{execute_remote_playback_transition, RemotePlaybackControlState};
        use std::sync::{atomic::AtomicUsize, Arc};
        use voice_desktop_common::ssh_device::RemoteDeviceTask;

        let task = RemoteDeviceTask {
            request_id: "request-a".into(),
            task_id: "task-a".into(),
            device_id: "device-a".into(),
            core_session_id: "core-a".into(),
            connection_generation: 2,
            executor_generation: 3,
            sequence: 1,
            operation: "playback.pause".into(),
            arguments: serde_json::json!({"playback_key":"speech-a"}),
        };
        let mut control = RemotePlaybackControlState {
            device_id: Some("device-a".into()),
            core_session_id: Some("core-a".into()),
            connection_generation: 2,
            executor_generation: 3,
            active_key: Some("speech-a".into()),
            ..RemotePlaybackControlState::default()
        };
        let calls = Arc::new(AtomicUsize::new(0));
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();

        let transition_calls = calls.clone();
        let paused = runtime
            .block_on(execute_remote_playback_transition(
                &mut control,
                &task,
                "speech-a".into(),
                true,
                move || async move {
                    transition_calls.fetch_add(1, Ordering::AcqRel);
                    Ok(())
                },
            ))
            .unwrap();
        assert!(paused.succeeded);
        assert_eq!(paused.result["paused"], true);
        assert!(control.paused);

        let transition_calls = calls.clone();
        let repeated = runtime
            .block_on(execute_remote_playback_transition(
                &mut control,
                &task,
                "speech-a".into(),
                true,
                move || async move {
                    transition_calls.fetch_add(1, Ordering::AcqRel);
                    Ok(())
                },
            ))
            .unwrap();
        assert!(repeated.succeeded);
        assert_eq!(calls.load(Ordering::Acquire), 1);

        let failed = runtime.block_on(execute_remote_playback_transition(
            &mut control,
            &task,
            "speech-a".into(),
            false,
            || async { Err("test transport lost acknowledgment".into()) },
        ));
        assert!(failed.is_err());
        assert!(control.control_uncertain);
        assert!(control.paused);

        let untouched = Arc::new(AtomicUsize::new(0));
        let callback_count = untouched.clone();
        assert!(runtime
            .block_on(execute_remote_playback_transition(
                &mut control,
                &task,
                "speech-a".into(),
                false,
                move || async move {
                    callback_count.fetch_add(1, Ordering::AcqRel);
                    Ok(())
                },
            ))
            .is_err());
        assert_eq!(untouched.load(Ordering::Acquire), 0);
    }

    #[cfg(feature = "ssh-device")]
    #[test]
    fn remote_stop_task_stops_only_the_current_matching_playback_key() {
        use super::{
            execute_remote_playback_stop, PlaybackSnapshotBaseline, PlaybackSnapshotView,
            RemotePlaybackControlState,
        };
        use voice_desktop_common::ssh_device::RemoteDeviceTask;
        let task = RemoteDeviceTask {
            request_id: "request-1".into(),
            device_id: "device-a".into(),
            core_session_id: "core-a".into(),
            task_id: "task-1".into(),
            connection_generation: 2,
            executor_generation: 3,
            sequence: 1,
            operation: "playback.stop_and_snapshot".into(),
            arguments: serde_json::json!({"playback_key":"speech-key"}),
        };
        let mut control = RemotePlaybackControlState {
            device_id: Some("device-a".into()),
            core_session_id: Some("core-a".into()),
            connection_generation: 2,
            executor_generation: 3,
            active_key: Some("speech-key".into()),
            ..RemotePlaybackControlState::default()
        };
        control.baselines.insert(
            "speech-key".into(),
            PlaybackSnapshotBaseline {
                enqueued_ms: 0,
                played_ms: 0,
                output_estimated_ms: 0,
                isolated: true,
            },
        );
        control.samples.insert("speech-key".into(), (16_000, 320));
        let stop_called = std::sync::Arc::new(AtomicBool::new(false));
        let stop_observed = stop_called.clone();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let result = runtime
            .block_on(execute_remote_playback_stop(
                &mut control,
                &task,
                Some("speech-key".into()),
                || async {
                    Ok(PlaybackSnapshotView {
                        enqueued_ms: 20,
                        played_ms: 8,
                        buffered_ms: 12,
                        output_estimated_ms: 7,
                        started: true,
                        device_time_us: 9_000_000,
                    })
                },
                move || async move {
                    stop_observed.store(true, Ordering::Release);
                    Ok(())
                },
            ))
            .unwrap();

        assert!(result.succeeded);
        assert_eq!(result.result["playback_key"], "speech-key");
        assert_eq!(result.result["enqueued_ms"], 20);
        assert!(stop_called.load(Ordering::Acquire));
        assert!(control.stopped_keys.contains("speech-key"));
        assert_eq!(control.active_key, None);
    }

    #[cfg(feature = "ssh-device")]
    #[test]
    fn remote_stop_of_direct_tts_reuses_the_device_receipt_after_the_next_playback_starts() {
        use super::{
            execute_remote_playback_stop, stop_remote_playback, ClientTtsPlaybackLedger,
            PlaybackSnapshotBaseline, PlaybackSnapshotView, RemotePlaybackControlState,
        };
        use voice_desktop_common::ssh_device::RemoteDeviceTask;

        let task = RemoteDeviceTask {
            request_id: "request-1".into(),
            device_id: "device-a".into(),
            core_session_id: "core-a".into(),
            task_id: "task-1".into(),
            connection_generation: 2,
            executor_generation: 3,
            sequence: 1,
            operation: "playback.stop_and_snapshot".into(),
            arguments: serde_json::json!({"playback_key":"direct-key"}),
        };
        let mut control = RemotePlaybackControlState {
            device_id: Some(task.device_id.clone()),
            core_session_id: Some(task.core_session_id.clone()),
            connection_generation: task.connection_generation,
            executor_generation: task.executor_generation,
            active_key: Some("direct-key".into()),
            ..RemotePlaybackControlState::default()
        };
        control.client_tts.insert(
            "direct-key".into(),
            ClientTtsPlaybackLedger {
                sample_rate: 16_000,
                accepted_samples: 16_000,
                played_samples: 8_000,
                baseline: PlaybackSnapshotBaseline {
                    enqueued_ms: 200,
                    played_ms: 200,
                    output_estimated_ms: 200,
                    isolated: true,
                },
            },
        );
        let runtime = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        runtime.block_on(async {
            let result = execute_remote_playback_stop(
                &mut control,
                &task,
                Some("direct-key".into()),
                || async {
                    Ok(PlaybackSnapshotView {
                        enqueued_ms: 1_200,
                        played_ms: 840,
                        buffered_ms: 400,
                        output_estimated_ms: 800,
                        started: true,
                        device_time_us: 9_000_000,
                    })
                },
                || async { Ok(()) },
            )
            .await
            .unwrap();
            assert!(result.succeeded);
            assert_eq!(result.result["output_estimated_ms"], 600);
            assert!(control.stopped_keys.contains("direct-key"));
            assert!(control.active_key.is_none());

            // 旧直放分段已撤销并开启下一段，迟到的媒体取消只读取原停播回执。
            control.forget_client_tts("direct-key");
            control.active_key = Some("next-key".into());
            let cached = stop_remote_playback(
                &mut control,
                "direct-key",
                || async { panic!("重复取消不应读取新一次播放的快照") },
                || async { panic!("重复取消不应停止下一次播放") },
            )
            .await
            .unwrap()
            .unwrap();
            let progress = cached.client_tts.unwrap();
            assert_eq!(progress.accepted_samples, 16_000);
            assert_eq!(progress.played_samples, 9_600);
            assert_eq!(progress.buffered_samples, 0);
            assert!(!progress.output_complete);
            assert_eq!(cached.snapshot, result.result);
            assert_eq!(control.active_key.as_deref(), Some("next-key"));
        });
    }

    #[cfg(feature = "ssh-device")]
    #[test]
    fn remote_stop_task_rejects_a_different_key_without_stopping_output() {
        use super::{
            execute_remote_playback_stop, PlaybackSnapshotBaseline, PlaybackSnapshotView,
            RemotePlaybackControlState,
        };
        use voice_desktop_common::ssh_device::RemoteDeviceTask;
        let task = RemoteDeviceTask {
            request_id: "request-2".into(),
            device_id: "device-a".into(),
            core_session_id: "core-a".into(),
            task_id: "task-2".into(),
            connection_generation: 2,
            executor_generation: 3,
            sequence: 2,
            operation: "playback.stop_and_snapshot".into(),
            arguments: serde_json::json!({"playback_key":"old-key"}),
        };
        let mut control = RemotePlaybackControlState {
            device_id: Some("device-a".into()),
            core_session_id: Some("core-a".into()),
            connection_generation: 2,
            executor_generation: 3,
            active_key: Some("current-key".into()),
            ..RemotePlaybackControlState::default()
        };
        control.baselines.insert(
            "old-key".into(),
            PlaybackSnapshotBaseline {
                enqueued_ms: 0,
                played_ms: 0,
                output_estimated_ms: 0,
                isolated: true,
            },
        );
        control.samples.insert("old-key".into(), (16_000, 320));
        let stop_called = std::sync::Arc::new(AtomicBool::new(false));
        let stop_observed = stop_called.clone();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let result = runtime
            .block_on(execute_remote_playback_stop(
                &mut control,
                &task,
                Some("old-key".into()),
                || async {
                    Ok(PlaybackSnapshotView {
                        enqueued_ms: 20,
                        played_ms: 8,
                        buffered_ms: 12,
                        output_estimated_ms: 7,
                        started: true,
                        device_time_us: 9_000_000,
                    })
                },
                move || async move {
                    stop_observed.store(true, Ordering::Release);
                    Ok(())
                },
            ))
            .unwrap();

        assert!(!result.succeeded);
        assert!(!stop_called.load(Ordering::Acquire));
        assert_eq!(control.active_key.as_deref(), Some("current-key"));
    }

    #[cfg(feature = "ssh-device")]
    #[test]
    fn remote_stop_uncertain_result_quarantines_late_audio_frames() {
        use super::{
            execute_remote_playback_stop, PlaybackSnapshotBaseline, PlaybackSnapshotView,
            RemotePlaybackControlState,
        };
        use voice_desktop_common::ssh_device::RemoteDeviceTask;
        let task = RemoteDeviceTask {
            request_id: "request-3".into(),
            device_id: "device-a".into(),
            core_session_id: "core-a".into(),
            task_id: "task-3".into(),
            connection_generation: 2,
            executor_generation: 3,
            sequence: 3,
            operation: "playback.stop_and_snapshot".into(),
            arguments: serde_json::json!({"playback_key":"speech-key"}),
        };
        let mut control = RemotePlaybackControlState {
            device_id: Some("device-a".into()),
            core_session_id: Some("core-a".into()),
            connection_generation: 2,
            executor_generation: 3,
            active_key: Some("speech-key".into()),
            ..RemotePlaybackControlState::default()
        };
        control.baselines.insert(
            "speech-key".into(),
            PlaybackSnapshotBaseline {
                enqueued_ms: 0,
                played_ms: 0,
                output_estimated_ms: 0,
                isolated: true,
            },
        );
        control.samples.insert("speech-key".into(), (16_000, 320));
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let result = runtime.block_on(execute_remote_playback_stop(
            &mut control,
            &task,
            Some("speech-key".into()),
            || async {
                Ok(PlaybackSnapshotView {
                    enqueued_ms: 20,
                    played_ms: 8,
                    buffered_ms: 12,
                    output_estimated_ms: 7,
                    started: true,
                    device_time_us: 9_000_000,
                })
            },
            || async { Err("stop acknowledgement timed out".into()) },
        ));

        assert!(result.is_err());
        assert!(control.stopped_keys.contains("speech-key"));
        assert_eq!(control.active_key, None);
    }
}

#[cfg(test)]
mod broadcast_lifecycle_tests {
    #[test]
    fn broadcast_and_local_preferences_do_not_restart_the_core() {
        use voice_desktop_common::device_config::*;
        let local = DeviceConfig {
            schema_version: 3,
            device_id: "host-a".into(),
            mode: RunMode::Local,
            broadcast_enabled: false,
            client_online_tts_local: true,
            audio_compression: false,
            reconnect_suspended_reason: None,
            desired_role: Role::Standalone,
            audio_mode: AudioMode::Pcm,
            host: None,
        };
        let mut broadcast = local.clone();
        broadcast.broadcast_enabled = true;
        broadcast.desired_role = Role::SharedHost;
        assert!(!super::device_core_restart_required(
            Some(&local),
            &broadcast,
            false
        ));
        assert!(!super::device_core_restart_required(
            Some(&broadcast),
            &local,
            false
        ));
        assert!(!super::device_core_restart_required(
            None, &broadcast, false
        ));
        let mut remote = local.clone();
        remote.mode = RunMode::Remote;
        assert!(super::device_core_restart_required(
            Some(&local),
            &remote,
            false
        ));
        assert!(super::device_core_restart_required(
            Some(&remote),
            &local,
            false
        ));
        assert!(super::device_core_restart_required(
            Some(&local),
            &local,
            true
        ));
    }
}
