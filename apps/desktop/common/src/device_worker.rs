//! Device worker（设备工作进程）：独立于图形框架和解释器的控制入口。
//!
//! 标准输入输出由父进程创建的匿名管道承载，首条消息必须持有本次启动凭据。
//! 设备能力必须通过已认证的有界消息显式启停；默认状态查询不访问麦克风或扬声器。
use serde::{Deserialize, Serialize};
use std::io::{self, Read, Write};

pub const ARGUMENT: &str = "--device-worker"; // ARGUMENT（设备工作进程参数）。
const TOKEN_ENV: &str = "VOICE_DEVICE_WORKER_TOKEN"; // TOKEN_ENV（单次启动凭据环境变量）。
pub const HELPER_PATH_ENV: &str = "VOICE_DEVICE_HELPER_PATH"; // HELPER_PATH_ENV（受管 DSH 辅助脚本路径）。
const MAX_FRAME: usize = 512 * 1024; // MAX_FRAME（控制消息最大字节数）。
const MAX_PLAYBACK_PCM_BYTES: usize = 8 * 1024; // MAX_PLAYBACK_PCM_BYTES（单次 IPC 播放采样上限）。

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    protocol_version: u32, // protocol_version（控制协议版本）。
    request_id: String,    // request_id（请求编号）。
    operation: String,     // operation（操作名称）。
    #[serde(default)]
    token: Option<String>, // token（仅首条消息使用的启动凭据）。
    #[serde(default)]
    wait_ms: Option<u32>, // wait_ms（采集帧读取等待上限，毫秒）。
    #[serde(default)]
    sample_rate: Option<u32>, // sample_rate（PCM 播放输入采样率）。
    #[serde(default)]
    pcm_le_bytes: Option<Vec<u8>>, // pcm_le_bytes（小端 16 位单声道 PCM 播放字节）。
    #[serde(default)]
    playback_key: Option<String>, // playback_key（固定播放事务编号）。
    #[serde(default)]
    replacement_id: Option<String>, // replacement_id（播放替换事务编号）。
    #[serde(default)]
    max_ms: Option<u32>, // max_ms（旧音收尾上限，毫秒）。
    #[serde(default)]
    reason: Option<String>, // reason（替换撤销原因）。
    #[serde(default)]
    helper_request_id: Option<String>, // helper_request_id（常驻辅助进程任务编号）。
    #[serde(default)]
    helper_operation: Option<String>, // helper_operation（有界桌面辅助操作名称）。
    #[serde(default)]
    helper_arguments: Option<serde_json::Value>, // helper_arguments（冻结 JSON 桌面辅助参数）。
    #[serde(default)]
    helper_timeout_ms: Option<u32>, // helper_timeout_ms（桌面操作时限，毫秒）。
}

#[derive(Serialize)]
struct Response<'a> {
    protocol_version: u32, // protocol_version（控制协议版本）。
    request_id: &'a str,   // request_id（请求编号）。
    ok: bool,              // ok（操作是否成功）。
    state: &'a str,        // state（工作进程状态）。
    #[serde(skip_serializing_if = "Option::is_none")]
    data: Option<serde_json::Value>, // data（状态以外的纯数据响应）。
}

#[cfg(windows)]
struct AudioProcessingSlot {
    status: serde_json::Value, // status（前处理健康与受保护打断状态）。
    #[cfg(feature = "native-audio")]
    processor: Option<crate::native_audio_processing::NativeAudioProcessor>, // processor（AEC/NS/VAD 原生处理实例）。
}

#[cfg(windows)]
impl AudioProcessingSlot {
    fn new() -> Self {
        Self {
            status: serde_json::json!({"state":"stopped","fields":{"state":"原生 AEC、降噪与近端 VAD 状态"}}),
            #[cfg(feature = "native-audio")]
            processor: None,
        }
    }

    fn start_capture(&mut self) -> serde_json::Value {
        #[cfg(feature = "native-audio")]
        {
            if self.processor.is_none() {
                match crate::native_audio_processing::NativeAudioProcessor::new(2) {
                    Ok(processor) => self.processor = Some(processor),
                    Err(error) => {
                        self.status = serde_json::json!({"state":"unavailable","error":error,
                            "fields":{"state":"原生 AEC、降噪与近端 VAD 状态","error":"初始化故障原因"}});
                        return self.status.clone();
                    }
                }
            }
            self.refresh();
            return self.status.clone();
        }
        #[cfg(not(feature = "native-audio"))]
        {
            self.status = serde_json::json!({"state":"unavailable","error":"当前构建未包含 native-audio 特性",
                "fields":{"state":"原生 AEC、降噪与近端 VAD 状态","error":"构建能力说明"}});
            self.status.clone()
        }
    }

    fn process_capture(
        &mut self,
        frame: &mut crate::wasapi_capture::CaptureFrame,
        buffered_ms: Option<u64>, // buffered_ms（当前播放快照的缓冲时长，缺少快照时为空）。
    ) -> serde_json::Value {
        #[cfg(feature = "native-audio")]
        {
            if let Some(processor) = self.processor.as_mut() {
                let _ = processor.update_playback_delay(buffered_ms);
                let result = processor.process_capture(frame);
                frame.pcm = result.pcm;
                self.refresh();
                return serde_json::json!({
                    "near_end_voice":result.near_end_voice,
                    "protected_interruption_available":result.protected_interruption_available,
                    "state":result.processor_state,
                    "reverse_generation":result.reverse_generation,
                    "error":result.error,
                    "fields":{"near_end_voice":"近端 10 毫秒 WebRTC VAD 判断汇总","protected_interruption_available":"反向参考新鲜且时延有效时才允许受保护打断","state":"AEC、降噪与 VAD 健康状态","reverse_generation":"播放参考格式代次","error":"前处理错误；健康时为空"}
                });
            }
            self.status.clone()
        }
        #[cfg(not(feature = "native-audio"))]
        {
            let _ = (frame, buffered_ms);
            self.status.clone()
        }
    }

    fn push_reference(
        &mut self,
        sample_rate: u32,
        samples: &[i16],
        buffered_ms: Option<u64>,
    ) -> serde_json::Value {
        #[cfg(feature = "native-audio")]
        {
            if let Some(processor) = self.processor.as_mut() {
                let processed_frames = processor.push_playback_reference(sample_rate, samples);
                let delay_result = processor.update_playback_delay(buffered_ms);
                if let Err(error) = processed_frames.and(delay_result) {
                    self.status = serde_json::json!({"state":"degraded","error":error,
                        "protected_interruption_available":false,
                        "fields":{"state":"原生 AEC、降噪与近端 VAD 状态","error":"播放参考处理故障","protected_interruption_available":"是否允许受保护打断"}});
                } else {
                    self.refresh();
                }
                return self.status.clone();
            }
        }
        #[cfg(not(feature = "native-audio"))]
        {
            let _ = (sample_rate, samples, buffered_ms);
        }
        self.status.clone()
    }

    fn reset_alignment(&mut self) {
        #[cfg(feature = "native-audio")]
        if let Some(processor) = self.processor.as_mut() {
            if let Err(error) = processor.reset_stream() {
                self.status = serde_json::json!({"state":"degraded","error":error,
                    "protected_interruption_available":false,
                    "fields":{"state":"原生 AEC、降噪与近端 VAD 状态","error":"设备流重置失败","protected_interruption_available":"是否允许受保护打断"}});
                return;
            }
            self.refresh();
        }
    }

    fn stop_capture(&mut self) {
        #[cfg(feature = "native-audio")]
        {
            self.processor = None;
        }
        self.status = serde_json::json!({"state":"stopped","fields":{"state":"原生 AEC、降噪与近端 VAD 状态"}});
    }

    #[cfg(feature = "native-audio")]
    fn refresh(&mut self) {
        if let Some(processor) = self.processor.as_ref() {
            self.status = processor.status();
        }
    }
}

fn read_frame(input: &mut impl Read) -> Result<Option<Vec<u8>>, String> {
    let mut prefix = [0_u8; 4];
    match input.read(&mut prefix[..1]) {
        Ok(0) => return Ok(None),
        Ok(_) => (),
        Err(error) => return Err(format!("读取设备控制消息失败：{error}")),
    }
    input
        .read_exact(&mut prefix[1..])
        .map_err(|_| "设备控制消息长度不完整")?;
    let length = u32::from_be_bytes(prefix) as usize;
    if length == 0 || length > MAX_FRAME {
        return Err("设备控制消息长度无效".into());
    }
    let mut frame = vec![0; length];
    input
        .read_exact(&mut frame)
        .map_err(|_| "设备控制消息不完整")?;
    Ok(Some(frame))
}

fn write_frame(output: &mut impl Write, response: &Response<'_>) -> Result<(), String> {
    let bytes = serde_json::to_vec(response).map_err(|_| "设备控制回执编码失败")?;
    if bytes.is_empty() || bytes.len() > MAX_FRAME {
        return Err("设备控制回执超过大小限制".into());
    }
    output
        .write_all(&(bytes.len() as u32).to_be_bytes())
        .and_then(|_| output.write_all(&bytes))
        .and_then(|_| output.flush())
        .map_err(|_| "设备控制回执写入失败".into())
}

fn valid_token(token: &str) -> bool {
    token.len() == 64 && token.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn valid_identifier(value: &str) -> bool {
    !value.is_empty() && value.len() <= 128 && value.trim() == value
}

/// serve（处理控制流）：管道关闭时自然退出；不接收任意命令。
pub fn serve(input: &mut impl Read, output: &mut impl Write, token: &str) -> Result<(), String> {
    serve_with_helper_path(input, output, token, None)
}

/// serve_with_helper_path（处理控制流并登记辅助脚本）：脚本只在明确健康请求时启动。
pub fn serve_with_helper_path(
    input: &mut impl Read,
    output: &mut impl Write,
    token: &str,
    helper_path: Option<&std::path::Path>,
) -> Result<(), String> {
    if !valid_token(token) {
        return Err("设备工作进程启动凭据无效".into());
    }
    let mut authenticated = false;
    #[cfg(windows)]
    let helper_path = helper_path.map(std::path::Path::to_path_buf);
    #[cfg(not(windows))]
    let _ = helper_path;
    #[cfg(windows)]
    let mut desktop_helper: Option<crate::device_helper::DeviceHelper> = None;
    #[cfg(windows)]
    let mut capture_stream: Option<crate::wasapi_capture::WasapiCaptureStream> = None;
    #[cfg(windows)]
    let mut render_stream: Option<crate::wasapi_render::WasapiRenderStream> = None;
    #[cfg(windows)]
    let mut audio_processing = AudioProcessingSlot::new();
    loop {
        let Some(frame) = read_frame(input)? else {
            return if authenticated {
                Ok(())
            } else {
                Err("设备工作进程未完成认证".into())
            };
        };
        let request: Request =
            serde_json::from_slice(&frame).map_err(|_| "设备控制消息格式无效")?;
        if request.protocol_version != 1
            || request.request_id.is_empty()
            || request.request_id.len() > 128
        {
            return Err("设备控制消息版本或请求编号无效".into());
        }
        let capture_wait_invalid = request
            .wait_ms
            .is_some_and(|value| !(1..=250).contains(&value))
            || (request.operation != "capture_next" && request.wait_ms.is_some());
        let media_present = request.sample_rate.is_some() || request.pcm_le_bytes.is_some();
        let playback_enqueue = request.operation == "playback_enqueue";
        let playback_fields_present = request.playback_key.is_some()
            || request.replacement_id.is_some()
            || request.max_ms.is_some()
            || request.reason.is_some();
        let playback_control_invalid = match request.operation.as_str() {
            "playback_enqueue" => {
                request
                    .playback_key
                    .as_deref()
                    .is_some_and(|key| !valid_identifier(key))
                    || request.replacement_id.is_some()
                    || request.max_ms.is_some()
                    || request.reason.is_some()
            }
            "playback_prepare_replacement" => {
                !request
                    .playback_key
                    .as_deref()
                    .is_some_and(valid_identifier)
                    || !request
                        .replacement_id
                        .as_deref()
                        .is_some_and(valid_identifier)
                    || request
                        .max_ms
                        .is_none_or(|value| !(1..=600).contains(&value))
                    || request.reason.is_some()
            }
            "playback_bind_replacement" => {
                !request
                    .playback_key
                    .as_deref()
                    .is_some_and(valid_identifier)
                    || !request
                        .replacement_id
                        .as_deref()
                        .is_some_and(valid_identifier)
                    || request.max_ms.is_some()
                    || request.reason.is_some()
            }
            "playback_abort_replacement" => {
                !request
                    .replacement_id
                    .as_deref()
                    .is_some_and(valid_identifier)
                    || !request.reason.as_deref().is_some_and(valid_identifier)
                    || request.playback_key.is_some()
                    || request.max_ms.is_some()
            }
            "playback_replacement_status" | "playback_wait_replacement" => {
                !request
                    .replacement_id
                    .as_deref()
                    .is_some_and(valid_identifier)
                    || request.playback_key.is_some()
                    || request.max_ms.is_some()
                    || request.reason.is_some()
            }
            _ => playback_fields_present,
        };
        let helper_fields_present = request.helper_request_id.is_some()
            || request.helper_operation.is_some()
            || request.helper_arguments.is_some()
            || request.helper_timeout_ms.is_some();
        let helper_call = request.operation == "desktop_helper_call";
        let helper_payload_invalid = if helper_call {
            let request_id = request.helper_request_id.as_deref().unwrap_or("");
            !request_id.bytes().enumerate().all(|(index, byte)| {
                byte.is_ascii_alphanumeric()
                    || (index > 0 && matches!(byte, b'.' | b'_' | b':' | b'-'))
            }) || request_id.is_empty()
                || request_id.len() > 128
                || !request
                    .helper_operation
                    .as_deref()
                    .is_some_and(crate::device_helper::supports_control_operation)
                || !request
                    .helper_arguments
                    .as_ref()
                    .is_some_and(serde_json::Value::is_object)
                || request
                    .helper_timeout_ms
                    .is_none_or(|value| !(1..=600_000).contains(&value))
        } else {
            helper_fields_present
        };
        let playback_media_invalid = if playback_enqueue {
            request.sample_rate.is_none()
                || request.pcm_le_bytes.as_ref().is_none_or(|bytes| {
                    bytes.is_empty() || bytes.len() > MAX_PLAYBACK_PCM_BYTES || bytes.len() % 2 != 0
                })
                || !matches!(request.sample_rate, Some(16_000 | 24_000 | 32_000 | 48_000))
        } else {
            media_present
        };
        if capture_wait_invalid
            || playback_media_invalid
            || playback_control_invalid
            || helper_payload_invalid
        {
            return Err(if capture_wait_invalid {
                "采集帧等待时间只允许在 1 至 250 毫秒之间"
            } else {
                if playback_media_invalid {
                    "播放请求必须包含受限采样率和偶数长度 PCM 字节"
                } else {
                    if playback_control_invalid {
                        "播放事务编号或替换参数无效"
                    } else {
                        "桌面辅助操作必须具有允许的任务编号、操作、参数对象及时限"
                    }
                }
            }
            .into());
        }
        if !authenticated {
            let provided = request.token.as_deref().unwrap_or("");
            // 固定长度比较避免提前按字符返回；凭据只在认证消息中使用。
            let equal = provided.len() == token.len()
                && provided
                    .bytes()
                    .zip(token.bytes())
                    .fold(0_u8, |difference, (left, right)| {
                        difference | (left ^ right)
                    })
                    == 0;
            if request.operation != "authenticate" || !equal {
                return Err("设备工作进程认证失败".into());
            }
            authenticated = true;
            write_frame(
                output,
                &Response {
                    protocol_version: 1,
                    request_id: &request.request_id,
                    ok: true,
                    state: "ready",
                    data: None,
                },
            )?;
            continue;
        }
        if request.token.is_some() {
            return Err("设备启动凭据不可重复使用".into());
        }
        let (ok, state, stop, data) = match request.operation.as_str() {
            "status" => (true, "ready", false, None),
            "desktop_helper_health" => {
                #[cfg(windows)]
                {
                    let start_error = if desktop_helper.is_none() {
                        match helper_path
                            .as_deref()
                            .ok_or_else(|| "当前工作区没有已登记桌面辅助脚本".to_owned())
                            .and_then(crate::device_helper::DeviceHelper::start)
                        {
                            Ok(helper) => {
                                desktop_helper = Some(helper);
                                None
                            }
                            Err(error) => Some(error),
                        }
                    } else {
                        None
                    };
                    if let Some(error) = start_error {
                        (
                            false,
                            "unavailable",
                            false,
                            Some(serde_json::json!({
                                "error":error,
                                "fields":{"error":"桌面辅助进程启动失败或能力缺失原因"}
                            })),
                        )
                    } else {
                        match desktop_helper.as_mut().expect("辅助进程已创建").call(
                            &request.request_id,
                            "health",
                            &serde_json::json!({}),
                            std::time::Duration::from_secs(10),
                        ) {
                            Ok(health)
                                if health.get("ok").and_then(serde_json::Value::as_bool)
                                    == Some(true) =>
                            {
                                (
                                    true,
                                    "ready",
                                    false,
                                    Some(serde_json::json!({
                                        "health":health,
                                        "fields":{"health":"桌面辅助进程自身就绪回执"}
                                    })),
                                )
                            }
                            Ok(health) => {
                                desktop_helper.take();
                                (
                                    false,
                                    "unavailable",
                                    false,
                                    Some(serde_json::json!({
                                        "error":"桌面辅助进程没有报告健康",
                                        "health":health,
                                        "fields":{"error":"桌面辅助健康检查失败原因","health":"辅助进程回执"}
                                    })),
                                )
                            }
                            Err(error) => {
                                desktop_helper.take();
                                (
                                    false,
                                    "unavailable",
                                    false,
                                    Some(serde_json::json!({
                                        "error":error,
                                        "fields":{"error":"桌面辅助进程健康请求失败原因"}
                                    })),
                                )
                            }
                        }
                    }
                }
                #[cfg(not(windows))]
                {
                    (
                        false,
                        "unsupported",
                        false,
                        Some(serde_json::json!({
                            "error":"桌面辅助进程仅支持 Windows",
                            "fields":{"error":"平台能力说明"}
                        })),
                    )
                }
            }
            "desktop_helper_call" => {
                #[cfg(windows)]
                {
                    let start_error = if desktop_helper.is_none() {
                        match helper_path
                            .as_deref()
                            .ok_or_else(|| "当前工作区没有已登记桌面辅助脚本".to_owned())
                            .and_then(crate::device_helper::DeviceHelper::start)
                        {
                            Ok(helper) => {
                                desktop_helper = Some(helper);
                                None
                            }
                            Err(error) => Some(error),
                        }
                    } else {
                        None
                    };
                    if let Some(error) = start_error {
                        (
                            false,
                            "unavailable",
                            false,
                            Some(serde_json::json!({"error":error,
                            "fields":{"error":"桌面辅助进程启动失败或能力缺失原因"}})),
                        )
                    } else {
                        match desktop_helper.as_mut().expect("辅助进程已创建").call(
                            request.helper_request_id.as_deref().unwrap_or_default(),
                            request.helper_operation.as_deref().unwrap_or_default(),
                            request
                                .helper_arguments
                                .as_ref()
                                .expect("桌面辅助参数已验证"),
                            std::time::Duration::from_millis(
                                request.helper_timeout_ms.unwrap_or(10_000) as u64,
                            ),
                        ) {
                            Ok(helper_response) => (
                                true,
                                "ready",
                                false,
                                Some(serde_json::json!({
                                    "helper_response":helper_response,
                                    "fields":{"helper_response":"桌面辅助进程对原任务的结构化回执"}
                                })),
                            ),
                            Err(error) => {
                                desktop_helper.take();
                                (
                                    false,
                                    "unknown",
                                    false,
                                    Some(serde_json::json!({"error":error,
                                    "fields":{"error":"桌面辅助任务执行失败或结果未知"}})),
                                )
                            }
                        }
                    }
                }
                #[cfg(not(windows))]
                {
                    (
                        false,
                        "unsupported",
                        false,
                        Some(serde_json::json!({
                            "error":"桌面辅助进程仅支持 Windows",
                            "fields":{"error":"平台能力说明"}
                        })),
                    )
                }
            }
            "shutdown" => {
                #[cfg(windows)]
                {
                    let mut errors = Vec::new();
                    if let Some(mut stream) = capture_stream.take() {
                        if let Err(error) = stream.stop() {
                            errors.push(error);
                        }
                    }
                    if let Some(mut stream) = render_stream.take() {
                        if let Err(error) = stream.stop() {
                            errors.push(error);
                        }
                    }
                    audio_processing.stop_capture();
                    if errors.is_empty() {
                        (true, "stopping", true, None)
                    } else {
                        (
                            false,
                            "cleanup_failed",
                            true,
                            Some(serde_json::json!({"error":errors.join("；")})),
                        )
                    }
                }
                #[cfg(not(windows))]
                {
                    (true, "stopping", true, None)
                }
            }
            "clock_now" => match crate::wasapi_capture::monotonic_device_time_us() {
                Ok(device_time_us) => (
                    true,
                    "ready",
                    false,
                    Some(serde_json::json!({
                        "device_time_us":device_time_us,
                        "fields":{"device_time_us":"与 WASAPI 包时间戳同源的设备单调 QPC 时钟，微秒"}
                    })),
                ),
                Err(error) => (
                    false,
                    "unavailable",
                    false,
                    Some(serde_json::json!({"error":error,
                        "fields":{"error":"设备单调时钟读取失败原因"}})),
                ),
            },
            "audio_devices" => match crate::device_audio::probe_default_audio_endpoints() {
                Ok(devices) => (
                    true,
                    "ready",
                    false,
                    Some(serde_json::json!({
                        "devices": devices,
                        "fields": {"flow":"端点方向：capture 表示采集，render 表示播放",
                            "endpoint_id":"Windows 默认音频端点编号","sample_rate":"系统混音采样率",
                            "channels":"系统混音声道数","bits_per_sample":"每采样位数",
                            "format_tag":"WAVEFORMATEX 格式编号","block_align":"每音频帧字节数",
                            "average_bytes_per_second":"每秒平均字节数",
                            "default_period_100ns":"默认设备周期，100 纳秒单位",
                            "minimum_period_100ns":"最小设备周期，100 纳秒单位"}
                    })),
                ),
                Err(error) => (
                    false,
                    "unavailable",
                    false,
                    Some(serde_json::json!({"error": error,
                    "fields":{"error":"默认音频端点不可用原因"}})),
                ),
            },
            #[cfg(windows)]
            "capture_start" => {
                let processing_status = audio_processing.start_capture();
                if capture_stream.is_some() {
                    let capture_status = if capture_stream
                        .as_ref()
                        .is_some_and(|stream| stream.is_reconnecting())
                    {
                        "reconnecting"
                    } else {
                        "ready"
                    };
                    (
                        true,
                        "ready",
                        false,
                        Some(
                            serde_json::json!({"capturing": true,"capture_status":capture_status,"audio_processing":processing_status,
                                "fields":{"capture_status":"采集流当前状态：ready（就绪）或 reconnecting（正在重建默认设备）"}}),
                        ),
                    )
                } else {
                    match crate::wasapi_capture::WasapiCaptureStream::start_default() {
                        Ok(stream) => {
                            capture_stream = Some(stream);
                            (
                                true,
                                "ready",
                                false,
                                Some(
                                    serde_json::json!({"capturing": true,"capture_status":"ready","audio_processing":processing_status,
                                        "fields":{"capture_status":"采集流当前状态：ready（就绪）或 reconnecting（正在重建默认设备）"}}),
                                ),
                            )
                        }
                        Err(error) => {
                            audio_processing.stop_capture();
                            (
                                false,
                                "unavailable",
                                false,
                                Some(serde_json::json!({"error": error,
                                    "fields":{"error":"默认麦克风启动失败原因"}})),
                            )
                        }
                    }
                }
            }
            #[cfg(windows)]
            "capture_next" => match capture_stream.as_mut() {
                None => (
                    false,
                    "unavailable",
                    false,
                    Some(serde_json::json!({"error":"默认麦克风尚未启动"})),
                ),
                Some(stream) => {
                    let wait =
                        std::time::Duration::from_millis(request.wait_ms.unwrap_or(20) as u64);
                    match stream.recv_frame(wait) {
                        Ok(Some(mut frame)) => {
                            let buffered_ms = render_stream
                                .as_ref()
                                .and_then(|stream| stream.snapshot().ok())
                                .map(|snapshot| snapshot.buffered_ms);
                            let audio_processing_state =
                                audio_processing.process_capture(&mut frame, buffered_ms);
                            (
                                true,
                                "ready",
                                false,
                                Some(serde_json::json!({"frame":{
                                    "sequence":frame.sequence,
                                    "start_sample":frame.start_sample,
                                    "device_time_us":frame.device_time_us,
                                    "discontinuity_before":frame.discontinuity_before,
                                    "pcm_le_bytes":frame.pcm_le_bytes(),
                                    "audio_processing":audio_processing_state,
                                    "capture_status":"ready",
                                    "fields":{"sequence":"连续采集帧编号",
                                        "start_sample":"16 kHz 设备流首个采样位置",
                                        "device_time_us":"设备单调采集时刻，微秒",
                                        "discontinuity_before":"帧前是否检测到采样缺口",
                                        "pcm_le_bytes":"AEC 与降噪后的、小端 16 位单声道 PCM 字节数组",
                                        "audio_processing":"当前帧的前处理健康、近端语音和打断保护状态",
                                        "capture_status":"采集流状态；端点重建后的首帧标记 discontinuity_before"}
                                },"audio_processing_status":audio_processing.status})),
                            )
                        }
                        Ok(None) => (
                            true,
                            "ready",
                            false,
                            Some(
                                serde_json::json!({"frame":null,"capture_status":if stream.is_reconnecting(){"reconnecting"}else{"ready"},
                                "fields":{"capture_status":"采集流当前状态：ready（就绪）或 reconnecting（正在重建默认设备）"}}),
                            ),
                        ),
                        Err(error) => {
                            capture_stream.take();
                            (
                                false,
                                "unavailable",
                                false,
                                Some(serde_json::json!({"error":error})),
                            )
                        }
                    }
                }
            },
            #[cfg(not(windows))]
            "capture_start" | "capture_next" => (
                false,
                "unsupported",
                false,
                Some(serde_json::json!({"error":"WASAPI 仅适用于 Windows"})),
            ),
            #[cfg(windows)]
            "capture_stop" => match capture_stream.take() {
                None => {
                    audio_processing.stop_capture();
                    (
                        true,
                        "ready",
                        false,
                        Some(
                            serde_json::json!({"capturing": false,"audio_processing":audio_processing.status}),
                        ),
                    )
                }
                Some(mut stream) => match stream.stop() {
                    Ok(()) => {
                        audio_processing.stop_capture();
                        (
                            true,
                            "ready",
                            false,
                            Some(
                                serde_json::json!({"capturing": false,"audio_processing":audio_processing.status}),
                            ),
                        )
                    }
                    Err(error) => {
                        audio_processing.stop_capture();
                        (
                            false,
                            "cleanup_failed",
                            false,
                            Some(
                                serde_json::json!({"error":error,"audio_processing":audio_processing.status}),
                            ),
                        )
                    }
                },
            },
            #[cfg(not(windows))]
            "capture_stop" => (
                true,
                "ready",
                false,
                Some(serde_json::json!({"capturing": false})),
            ),
            #[cfg(windows)]
            "playback_start" => {
                let previous_result_unknown = render_stream
                    .as_ref()
                    .is_some_and(|stream| stream.ensure_healthy().is_err());
                if render_stream.is_some() && !previous_result_unknown {
                    (
                        true,
                        "ready",
                        false,
                        Some(serde_json::json!({"playing": true})),
                    )
                } else {
                    if previous_result_unknown {
                        if let Some(mut old_stream) = render_stream.take() {
                            let _ = old_stream.stop();
                        }
                        audio_processing.reset_alignment();
                    }
                    match crate::wasapi_render::WasapiRenderStream::start_default() {
                        Ok(stream) => {
                            render_stream = Some(stream);
                            audio_processing.reset_alignment();
                            (
                                true,
                                "ready",
                                false,
                                Some(serde_json::json!({"playing": true,
                                        "previous_playback_result": if previous_result_unknown {"unknown"} else {"none"},
                                        "audio_processing":audio_processing.status,
                                        "fields":{"previous_playback_result":"旧默认端点已失效时结果保持未知；新流不重放旧音频"}})),
                            )
                        }
                        Err(error) => (
                            false,
                            "unavailable",
                            false,
                            Some(serde_json::json!({"error":error,
                                "previous_playback_result": if previous_result_unknown {"unknown"} else {"none"},
                                "fields":{"error":"默认扬声器启动失败原因",
                                    "previous_playback_result":"旧默认端点已失效时结果保持未知；没有重放旧音频"}})),
                        ),
                    }
                }
            }
            #[cfg(windows)]
            "playback_pause" => match render_stream.as_ref() {
                None => (
                    false,
                    "unavailable",
                    false,
                    Some(serde_json::json!({"error":"默认扬声器尚未启动"})),
                ),
                Some(stream) => match stream.pause() {
                    Ok(()) => (
                        true,
                        "ready",
                        false,
                        Some(
                            serde_json::json!({"playing":true,"paused":stream.is_paused(),
                            "fields":{"playing":"播放端点是否仍被保留","paused":"设备线程已确认的暂停状态"}}),
                        ),
                    ),
                    Err(error) => (
                        false,
                        "unknown",
                        false,
                        Some(serde_json::json!({"error":error,
                            "fields":{"error":"暂停请求失败或结果未知"}})),
                    ),
                },
            },
            #[cfg(windows)]
            "playback_resume" => match render_stream.as_ref() {
                None => (
                    false,
                    "unavailable",
                    false,
                    Some(serde_json::json!({"error":"默认扬声器尚未启动"})),
                ),
                Some(stream) => match stream.resume() {
                    Ok(()) => (
                        true,
                        "ready",
                        false,
                        Some(
                            serde_json::json!({"playing":true,"paused":stream.is_paused(),
                            "fields":{"playing":"播放端点是否仍被保留","paused":"设备线程已确认的暂停状态"}}),
                        ),
                    ),
                    Err(error) => (
                        false,
                        "unknown",
                        false,
                        Some(serde_json::json!({"error":error,
                            "fields":{"error":"恢复请求失败或结果未知"}})),
                    ),
                },
            },
            #[cfg(windows)]
            "playback_enqueue" => match render_stream.as_ref() {
                None => (
                    false,
                    "unavailable",
                    false,
                    Some(serde_json::json!({"error":"默认扬声器尚未启动"})),
                ),
                Some(stream) => {
                    let bytes = request.pcm_le_bytes.as_deref().unwrap_or_default();
                    let samples: Vec<i16> = bytes
                        .chunks_exact(2)
                        .map(|chunk| i16::from_le_bytes([chunk[0], chunk[1]]))
                        .collect();
                    let sample_rate = request.sample_rate.unwrap_or_default();
                    let playback_key = request.playback_key.as_deref().unwrap_or("local-playback");
                    match stream.enqueue_pcm16_for_key(playback_key, sample_rate, &samples) {
                        Ok(enqueued_ms) => {
                            let snapshot = stream.snapshot();
                            let buffered_ms = snapshot
                                .as_ref()
                                .ok()
                                .map(|value| value.buffered_ms);
                            audio_processing.push_reference(sample_rate, &samples, buffered_ms);
                            (
                                true,
                                "ready",
                                false,
                                Some(serde_json::json!({"enqueued_ms":enqueued_ms,
                                    "audio_processing":audio_processing.status,
                                    "playback_snapshot_error":snapshot.err(),
                                    "fields":{"enqueued_ms":"已受理播放时长，毫秒","audio_processing":"AEC 反向参考和前处理健康状态","playback_snapshot_error":"读取本机播放延迟估计失败原因；不作为实际播放完成证据"}})),
                            )
                        }
                        Err(error) => (
                            false,
                            "rejected",
                            false,
                            Some(serde_json::json!({"error":error})),
                        ),
                    }
                }
            },
            #[cfg(windows)]
            "playback_prepare_replacement" => match render_stream.as_ref() {
                None => (
                    false,
                    "unavailable",
                    false,
                    Some(serde_json::json!({"error":"默认扬声器尚未启动"})),
                ),
                Some(stream) => match stream.prepare_replacement(
                    request.replacement_id.as_deref().unwrap_or_default(),
                    request.playback_key.as_deref().unwrap_or_default(),
                    request.max_ms.unwrap_or_default(),
                ) {
                    Ok(replacement) => match stream.snapshot() {
                        Ok(snapshot) => (
                            true,
                            "ready",
                            false,
                            Some(
                                serde_json::json!({"replacement":replacement,"snapshot":snapshot,
                                "fields":{"replacement":"按替换编号保存的设备端淡化状态","snapshot":"替换准备时的设备输出快照"}}),
                            ),
                        ),
                        Err(error) => (
                            false,
                            "unknown",
                            false,
                            Some(serde_json::json!({"error":error,
                                "fields":{"error":"设备已受理替换但准备快照读取失败，结果须查询原替换编号"}})),
                        ),
                    },
                    Err(error) => (
                        false,
                        "rejected",
                        false,
                        Some(serde_json::json!({"error":error})),
                    ),
                },
            },
            #[cfg(windows)]
            "playback_bind_replacement" => match render_stream.as_ref() {
                None => (
                    false,
                    "unavailable",
                    false,
                    Some(serde_json::json!({"error":"默认扬声器尚未启动"})),
                ),
                Some(stream) => match stream.bind_replacement(
                    request.replacement_id.as_deref().unwrap_or_default(),
                    request.playback_key.as_deref().unwrap_or_default(),
                ) {
                    Ok(true) => (
                        true,
                        "ready",
                        false,
                        Some(serde_json::json!({"bound":true,
                            "fields":{"bound":"后继播放键已固定到原替换事务"}})),
                    ),
                    Ok(false) => (
                        false,
                        "rejected",
                        false,
                        Some(serde_json::json!({"error":"替换事务不存在、已完成或后继绑定不匹配"})),
                    ),
                    Err(error) => (
                        false,
                        "rejected",
                        false,
                        Some(serde_json::json!({"error":error})),
                    ),
                },
            },
            #[cfg(windows)]
            "playback_abort_replacement" => match render_stream.as_ref() {
                None => (
                    false,
                    "unavailable",
                    false,
                    Some(serde_json::json!({"error":"默认扬声器尚未启动"})),
                ),
                Some(stream) => match stream.abort_replacement(
                    request.replacement_id.as_deref().unwrap_or_default(),
                    request.reason.as_deref().unwrap_or_default(),
                ) {
                    Ok(Some(replacement)) => (
                        true,
                        "ready",
                        false,
                        Some(serde_json::json!({"replacement":replacement})),
                    ),
                    Ok(None) => (
                        false,
                        "unknown",
                        false,
                        Some(serde_json::json!({"error":"替换编号没有可查询的设备端记录"})),
                    ),
                    Err(error) => (
                        false,
                        "unknown",
                        false,
                        Some(serde_json::json!({"error":error})),
                    ),
                },
            },
            #[cfg(windows)]
            "playback_replacement_status" | "playback_wait_replacement" => {
                match render_stream.as_ref() {
                    None => (
                        false,
                        "unavailable",
                        false,
                        Some(serde_json::json!({"error":"默认扬声器尚未启动"})),
                    ),
                    Some(stream) => match stream
                        .replacement_status(request.replacement_id.as_deref().unwrap_or_default())
                    {
                        Ok(Some(replacement)) => (
                            true,
                            "ready",
                            false,
                            Some(serde_json::json!({"replacement":replacement})),
                        ),
                        Ok(None) => (
                            false,
                            "unknown",
                            false,
                            Some(serde_json::json!({"error":"替换编号没有可查询的设备端记录"})),
                        ),
                        Err(error) => (
                            false,
                            "unknown",
                            false,
                            Some(serde_json::json!({"error":error})),
                        ),
                    },
                }
            }
            #[cfg(windows)]
            "playback_snapshot" => match render_stream.as_ref() {
                None => (
                    true,
                    "ready",
                    false,
                    Some(serde_json::json!({"playing":false})),
                ),
                Some(stream) => match stream.snapshot() {
                    Ok(snapshot) => (
                        true,
                        "ready",
                        false,
                        Some(serde_json::json!({"playing":true,"snapshot":snapshot,
                        "fields":{"enqueued_ms":"已受理播放时长，毫秒","played_ms":"已提交给 WASAPI 端点的内容时长，毫秒",
                            "buffered_ms":"端点与本地排队时长，毫秒","output_estimated_ms":"端点时钟输出估计，毫秒",
                            "started":"设备时钟是否已开始推进","device_time_us":"设备时钟对应 QPC 时刻，微秒"}})),
                    ),
                    Err(error) => {
                        render_stream.take();
                        (
                            false,
                            "unavailable",
                            false,
                            Some(serde_json::json!({"error":error})),
                        )
                    }
                },
            },
            #[cfg(windows)]
            "playback_stop" => match render_stream.take() {
                None => {
                    audio_processing.reset_alignment();
                    (
                        true,
                        "ready",
                        false,
                        Some(
                            serde_json::json!({"playing":false,"audio_processing":audio_processing.status}),
                        ),
                    )
                }
                Some(mut stream) => match stream.stop() {
                    Ok(()) => {
                        audio_processing.reset_alignment();
                        (
                            true,
                            "ready",
                            false,
                            Some(
                                serde_json::json!({"playing":false,"audio_processing":audio_processing.status}),
                            ),
                        )
                    }
                    Err(error) => {
                        audio_processing.reset_alignment();
                        (
                            false,
                            "cleanup_failed",
                            false,
                            Some(
                                serde_json::json!({"error":error,"audio_processing":audio_processing.status}),
                            ),
                        )
                    }
                },
            },
            #[cfg(not(windows))]
            "playback_start"
            | "playback_enqueue"
            | "playback_snapshot"
            | "playback_pause"
            | "playback_resume"
            | "playback_prepare_replacement"
            | "playback_bind_replacement"
            | "playback_abort_replacement"
            | "playback_replacement_status"
            | "playback_wait_replacement" => (
                false,
                "unsupported",
                false,
                Some(serde_json::json!({"error":"WASAPI 仅适用于 Windows"})),
            ),
            #[cfg(not(windows))]
            "playback_stop" => (
                true,
                "ready",
                false,
                Some(serde_json::json!({"playing":false})),
            ),
            _ => (false, "unsupported", false, None),
        };
        write_frame(
            output,
            &Response {
                protocol_version: 1,
                request_id: &request.request_id,
                ok,
                state,
                data,
            },
        )?;
        if stop {
            return Ok(());
        }
    }
}

/// dispatch（分流入口）：桌面框架和 Python（解释器）初始化之前执行。
pub fn dispatch() -> Option<Result<(), String>> {
    if std::env::args_os().nth(1).as_deref() != Some(std::ffi::OsStr::new(ARGUMENT)) {
        return None;
    }
    let token = std::env::var(TOKEN_ENV).unwrap_or_default();
    std::env::remove_var(TOKEN_ENV);
    let helper_path = std::env::var_os(HELPER_PATH_ENV).map(std::path::PathBuf::from);
    std::env::remove_var(HELPER_PATH_ENV);
    Some(serve_with_helper_path(
        &mut io::stdin().lock(),
        &mut io::stdout().lock(),
        &token,
        helper_path.as_deref(),
    ))
}

/// ManagedWorker（受管设备工作进程）：父进程持有匿名管道及作业对象。
#[cfg(windows)]
pub struct ManagedWorker {
    job: crate::windows::Job,
    child: std::process::Child,
    input: Option<std::process::ChildStdin>,
    replies: Option<std::sync::mpsc::Receiver<Result<Option<Vec<u8>>, String>>>,
    reader: Option<std::thread::JoinHandle<()>>,
    next_id: u64,
    shutdown_requested: bool, // shutdown_requested（已收到有效的优雅关闭回执）。
}

#[cfg(windows)]
impl ManagedWorker {
    /// start（启动）：只运行当前产品程序，系统随机数生成单次凭据。
    pub fn start() -> Result<Self, String> {
        let executable =
            std::env::current_exe().map_err(|error| format!("无法定位设备工作进程：{error}"))?;
        Self::start_with_executable(&executable)
    }

    /// start_with_helper_path（指定受管桌面辅助脚本）：脚本缺失时仍可启动音频设备工作进程。
    pub fn start_with_helper_path(helper_path: Option<&std::path::Path>) -> Result<Self, String> {
        let executable =
            std::env::current_exe().map_err(|error| format!("无法定位设备工作进程：{error}"))?;
        Self::start_with_executable_and_helper(&executable, helper_path)
    }

    /// start_with_executable（指定可执行文件启动）：供桌面候选验收工具验证产品分流入口。
    pub fn start_with_executable(executable: &std::path::Path) -> Result<Self, String> {
        Self::start_with_executable_and_helper(executable, None)
    }

    /// start_with_executable_and_helper（指定程序与受管脚本）：仅传递经父进程校验的脚本路径。
    pub fn start_with_executable_and_helper(
        executable: &std::path::Path,
        helper_path: Option<&std::path::Path>,
    ) -> Result<Self, String> {
        use std::process::{Command, Stdio};
        #[link(name = "bcrypt")]
        unsafe extern "system" {
            fn BCryptGenRandom(
                algorithm: *mut std::ffi::c_void,
                output: *mut u8,
                length: u32,
                flags: u32,
            ) -> i32;
        }
        let mut random = [0_u8; 32];
        // BCRYPT_USE_SYSTEM_PREFERRED_RNG（使用系统首选加密随机数生成器）。
        if unsafe {
            BCryptGenRandom(
                std::ptr::null_mut(),
                random.as_mut_ptr(),
                random.len() as u32,
                2,
            )
        } < 0
        {
            return Err("无法生成设备启动凭据".into());
        }
        let token: String = random.iter().map(|value| format!("{value:02x}")).collect();
        random.fill(0);
        let metadata = std::fs::symlink_metadata(executable)
            .map_err(|error| format!("无法读取设备工作进程候选：{error}"))?;
        if crate::workspace::metadata_reparse(&metadata) || !metadata.is_file() {
            return Err("设备工作进程候选必须是普通文件，不能是重解析点。".into());
        }
        let path = executable
            .canonicalize()
            .map_err(|error| format!("无法解析设备工作进程候选：{error}"))?;
        let helper_path = helper_path
            .map(|helper| {
                let metadata = std::fs::symlink_metadata(helper)
                    .map_err(|error| format!("无法读取桌面辅助脚本：{error}"))?;
                if crate::workspace::metadata_reparse(&metadata) || !metadata.is_file() {
                    return Err("桌面辅助脚本必须是普通文件，不能是重解析点。".into());
                }
                crate::canonical_path(helper)
                    .map_err(|error| format!("无法解析桌面辅助脚本：{error}"))
            })
            .transpose()?;
        let job = crate::windows::Job::new()?;
        let mut command = Command::new(path);
        command
            .arg(ARGUMENT)
            .env(TOKEN_ENV, &token)
            .env_remove(HELPER_PATH_ENV)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        if let Some(helper) = helper_path.as_ref() {
            command.env(HELPER_PATH_ENV, helper);
        }
        // Job::spawn（受管创建）在子进程运行前完成作业归属。
        let mut child = job.spawn(&mut command)?;
        let input = child.stdin.take().ok_or("设备工作进程输入管道不可用")?;
        let mut output = child.stdout.take().ok_or("设备工作进程输出管道不可用")?;
        let (sender, receiver) = std::sync::mpsc::sync_channel(1);
        let reader = std::thread::spawn(move || loop {
            let result = read_frame(&mut output);
            let finished = !matches!(&result, Ok(Some(_)));
            if sender.send(result).is_err() || finished {
                break;
            }
        });
        let mut worker = Self {
            job,
            child,
            input: Some(input),
            replies: Some(receiver),
            reader: Some(reader),
            next_id: 0,
            shutdown_requested: false,
        };
        let response = worker.exchange(
            "authenticate",
            Some(&token),
            std::time::Duration::from_secs(10),
        )?;
        if response.get("ok").and_then(serde_json::Value::as_bool) != Some(true)
            || response.get("state").and_then(serde_json::Value::as_str) != Some("ready")
        {
            return Err("设备工作进程认证回执无效".into());
        }
        Ok(worker)
    }

    fn exchange(
        &mut self,
        operation: &str,
        token: Option<&str>,
        timeout: std::time::Duration,
    ) -> Result<serde_json::Value, String> {
        self.exchange_with_wait(operation, token, None, timeout)
    }

    fn exchange_with_wait(
        &mut self,
        operation: &str,
        token: Option<&str>,
        wait_ms: Option<u32>,
        timeout: std::time::Duration,
    ) -> Result<serde_json::Value, String> {
        self.exchange_with_payload(
            operation, token, wait_ms, None, None, None, None, None, None, None, None, None, None,
            timeout,
        )
    }

    fn exchange_with_payload(
        &mut self,
        operation: &str,
        token: Option<&str>,
        wait_ms: Option<u32>,
        sample_rate: Option<u32>,
        pcm_le_bytes: Option<&[u8]>,
        helper_request_id: Option<&str>,
        helper_operation: Option<&str>,
        helper_arguments: Option<&serde_json::Value>,
        helper_timeout_ms: Option<u32>,
        playback_key: Option<&str>,
        replacement_id: Option<&str>,
        max_ms: Option<u32>,
        reason: Option<&str>,
        timeout: std::time::Duration,
    ) -> Result<serde_json::Value, String> {
        self.next_id = self.next_id.checked_add(1).ok_or("设备请求编号已用尽")?;
        let id = self.next_id.to_string();
        let mut request =
            serde_json::json!({"protocol_version":1,"request_id":id,"operation":operation});
        if let Some(token) = token {
            request["token"] = serde_json::Value::String(token.to_owned());
        }
        if let Some(wait_ms) = wait_ms {
            request["wait_ms"] = serde_json::Value::Number(wait_ms.into());
        }
        if let Some(sample_rate) = sample_rate {
            request["sample_rate"] = serde_json::Value::Number(sample_rate.into());
        }
        if let Some(pcm_le_bytes) = pcm_le_bytes {
            request["pcm_le_bytes"] =
                serde_json::to_value(pcm_le_bytes).map_err(|_| "PCM 播放数据无法编码")?;
        }
        if let Some(helper_request_id) = helper_request_id {
            request["helper_request_id"] = serde_json::Value::String(helper_request_id.to_owned());
        }
        if let Some(helper_operation) = helper_operation {
            request["helper_operation"] = serde_json::Value::String(helper_operation.to_owned());
        }
        if let Some(helper_arguments) = helper_arguments {
            request["helper_arguments"] = helper_arguments.clone();
        }
        if let Some(helper_timeout_ms) = helper_timeout_ms {
            request["helper_timeout_ms"] = serde_json::Value::Number(helper_timeout_ms.into());
        }
        if let Some(playback_key) = playback_key {
            request["playback_key"] = serde_json::Value::String(playback_key.to_owned());
        }
        if let Some(replacement_id) = replacement_id {
            request["replacement_id"] = serde_json::Value::String(replacement_id.to_owned());
        }
        if let Some(max_ms) = max_ms {
            request["max_ms"] = serde_json::Value::Number(max_ms.into());
        }
        if let Some(reason) = reason {
            request["reason"] = serde_json::Value::String(reason.to_owned());
        }
        let data = serde_json::to_vec(&request).map_err(|_| "设备请求编码失败")?;
        let input = self.input.as_mut().ok_or("设备工作进程已停止")?;
        input
            .write_all(&(data.len() as u32).to_be_bytes())
            .and_then(|_| input.write_all(&data))
            .and_then(|_| input.flush())
            .map_err(|_| "设备请求写入失败")?;
        let bytes = self
            .replies
            .as_ref()
            .ok_or("设备回执管道已关闭")?
            .recv_timeout(timeout)
            .map_err(|_| "设备工作进程回执超时")??
            .ok_or("设备工作进程已关闭")?;
        let response: serde_json::Value =
            serde_json::from_slice(&bytes).map_err(|_| "设备回执格式无效")?;
        if response
            .get("protocol_version")
            .and_then(serde_json::Value::as_u64)
            != Some(1)
            || response
                .get("request_id")
                .and_then(serde_json::Value::as_str)
                != Some(id.as_str())
            || response
                .get("ok")
                .and_then(serde_json::Value::as_bool)
                .is_none()
        {
            return Err("设备回执与原请求不匹配".into());
        }
        Ok(response)
    }

    /// status（状态）：仅返回当前工作进程自行确认的状态。
    pub fn status(&mut self) -> Result<serde_json::Value, String> {
        let result = self
            .exchange("status", None, std::time::Duration::from_secs(5))
            .and_then(|response| {
                if response.get("ok").and_then(serde_json::Value::as_bool) == Some(true)
                    && response.get("state").and_then(serde_json::Value::as_str) == Some("ready")
                {
                    Ok(response)
                } else {
                    Err("设备工作进程状态回执无效".into())
                }
            });
        if result.is_err() {
            self.stop();
        }
        result
    }

    /// desktop_helper_health（桌面辅助进程健康检查）：仅请求无副作用的 health 操作。
    pub fn desktop_helper_health(&mut self) -> Result<serde_json::Value, String> {
        let response = self.exchange(
            "desktop_helper_health",
            None,
            std::time::Duration::from_secs(12),
        )?;
        if response.get("ok").and_then(serde_json::Value::as_bool) == Some(true)
            && response.get("state").and_then(serde_json::Value::as_str) == Some("ready")
        {
            return Ok(response);
        }
        Ok(response)
    }

    /// desktop_helper_call（受管桌面操作）：调用受限辅助协议；动作审批必须先在 DSH 授权链完成。
    pub fn desktop_helper_call(
        &mut self,
        request_id: &str,
        operation: &str,
        arguments: &serde_json::Value,
        timeout_ms: u32,
    ) -> Result<serde_json::Value, String> {
        if !crate::device_helper::supports_control_operation(operation)
            || !arguments.is_object()
            || !(1..=600_000).contains(&timeout_ms)
        {
            return Err("桌面辅助操作、参数对象或时限无效。".into());
        }
        let response = self.exchange_with_payload(
            "desktop_helper_call",
            None,
            None,
            None,
            None,
            Some(request_id),
            Some(operation),
            Some(arguments),
            Some(timeout_ms),
            None,
            None,
            None,
            None,
            std::time::Duration::from_millis(timeout_ms as u64 + 3_000),
        )?;
        if response.get("ok").and_then(serde_json::Value::as_bool) == Some(true)
            && response.get("state").and_then(serde_json::Value::as_str) == Some("ready")
        {
            return response
                .get("data")
                .and_then(|data| data.get("helper_response"))
                .cloned()
                .ok_or_else(|| "桌面辅助回执缺少操作结果".to_owned());
        }
        Err(response
            .get("data")
            .and_then(|value| value.get("error"))
            .and_then(serde_json::Value::as_str)
            .unwrap_or("桌面辅助操作结果未知")
            .to_owned())
    }

    /// audio_devices（音频端点状态）：只读取系统格式，不启动采集或播放流。
    pub fn audio_devices(&mut self) -> Result<serde_json::Value, String> {
        let response = self.exchange("audio_devices", None, std::time::Duration::from_secs(5))?;
        if response.get("ok").and_then(serde_json::Value::as_bool) == Some(true)
            && response.get("state").and_then(serde_json::Value::as_str) == Some("ready")
        {
            return response
                .get("data")
                .cloned()
                .ok_or_else(|| "音频端点回执缺少数据".to_owned());
        }
        let error = response
            .get("data")
            .and_then(|value| value.get("error"))
            .and_then(serde_json::Value::as_str)
            .unwrap_or("默认音频端点不可用");
        Err(error.to_owned())
    }

    /// clock_now（读取设备时钟）：与 WASAPI 采集时间戳共享 QPC 时钟域。
    pub fn clock_now(&mut self) -> Result<u64, String> {
        let response = self.exchange("clock_now", None, std::time::Duration::from_secs(2))?;
        if response.get("ok").and_then(serde_json::Value::as_bool) == Some(true)
            && response.get("state").and_then(serde_json::Value::as_str) == Some("ready")
        {
            return response
                .get("data")
                .and_then(|data| data.get("device_time_us"))
                .and_then(serde_json::Value::as_u64)
                .ok_or_else(|| "设备时钟回执缺少单调时间戳".to_owned());
        }
        Err(response
            .get("data")
            .and_then(|data| data.get("error"))
            .and_then(serde_json::Value::as_str)
            .unwrap_or("设备单调时钟不可用")
            .to_owned())
    }

    /// capture_start（显式启动采集）：打开系统默认麦克风并建立有界 PCM 帧队列。
    pub fn capture_start(&mut self) -> Result<serde_json::Value, String> {
        let response = self.exchange("capture_start", None, std::time::Duration::from_secs(12))?;
        if response.get("ok").and_then(serde_json::Value::as_bool) == Some(true)
            && response.get("state").and_then(serde_json::Value::as_str) == Some("ready")
        {
            return response
                .get("data")
                .cloned()
                .ok_or_else(|| "采集启动回执缺少前处理状态".to_owned());
        }
        let error = response
            .get("data")
            .and_then(|value| value.get("error"))
            .and_then(serde_json::Value::as_str)
            .unwrap_or("默认麦克风启动失败");
        Err(error.to_owned())
    }

    /// capture_next（读取一帧）：等待范围 1 至 250 毫秒，返回小端 PCM 字节数组。
    pub fn capture_next(&mut self, wait_ms: u32) -> Result<Option<serde_json::Value>, String> {
        if !(1..=250).contains(&wait_ms) {
            return Err("采集帧等待时间只允许在 1 至 250 毫秒之间".into());
        }
        let response = self.exchange_with_wait(
            "capture_next",
            None,
            Some(wait_ms),
            std::time::Duration::from_millis(wait_ms as u64 + 3_000),
        )?;
        if response.get("ok").and_then(serde_json::Value::as_bool) != Some(true) {
            let error = response
                .get("data")
                .and_then(|value| value.get("error"))
                .and_then(serde_json::Value::as_str)
                .unwrap_or("默认麦克风帧读取失败");
            return Err(error.to_owned());
        }
        let data = response.get("data").cloned().ok_or("采集帧回执缺少数据")?;
        Ok(data.get("frame").filter(|frame| !frame.is_null()).cloned())
    }

    /// capture_stop（停止采集）：确认释放系统麦克风及采集线程。
    pub fn capture_stop(&mut self) -> Result<(), String> {
        let response = self.exchange("capture_stop", None, std::time::Duration::from_secs(3))?;
        if response.get("ok").and_then(serde_json::Value::as_bool) == Some(true)
            && response.get("state").and_then(serde_json::Value::as_str) == Some("ready")
        {
            return Ok(());
        } else {
            Err(response
                .get("data")
                .and_then(|value| value.get("error"))
                .and_then(serde_json::Value::as_str)
                .unwrap_or("设备工作进程未确认停止麦克风")
                .to_owned())
        }
    }

    /// playback_start（显式启动播放）：打开系统默认扬声器并创建共享模式流。
    pub fn playback_start(&mut self) -> Result<serde_json::Value, String> {
        let response = self.exchange("playback_start", None, std::time::Duration::from_secs(12))?;
        if response.get("ok").and_then(serde_json::Value::as_bool) == Some(true)
            && response.get("state").and_then(serde_json::Value::as_str) == Some("ready")
        {
            response
                .get("data")
                .cloned()
                .ok_or_else(|| "播放启动回执缺少前处理状态".to_owned())
        } else {
            Err(response
                .get("data")
                .and_then(|value| value.get("error"))
                .and_then(serde_json::Value::as_str)
                .unwrap_or("默认扬声器启动失败")
                .to_owned())
        }
    }

    /// playback_enqueue（加入播放）：小端 16 位单声道，单次数据不超过 8 KiB。
    pub fn playback_enqueue(
        &mut self,
        sample_rate: u32,
        pcm_le_bytes: &[u8],
    ) -> Result<serde_json::Value, String> {
        self.playback_enqueue_for_key("local-playback", sample_rate, pcm_le_bytes)
    }

    /// playback_enqueue_for_key（按播放事务加入播放）：设备拒绝复用旧键或迟到键。
    pub fn playback_enqueue_for_key(
        &mut self,
        playback_key: &str,
        sample_rate: u32,
        pcm_le_bytes: &[u8],
    ) -> Result<serde_json::Value, String> {
        if !matches!(sample_rate, 16_000 | 24_000 | 32_000 | 48_000)
            || pcm_le_bytes.is_empty()
            || pcm_le_bytes.len() > MAX_PLAYBACK_PCM_BYTES
            || pcm_le_bytes.len() % 2 != 0
            || !valid_identifier(playback_key)
        {
            return Err("PCM 播放事务编号、采样率或数据长度无效".into());
        }
        let response = self.exchange_with_payload(
            "playback_enqueue",
            None,
            None,
            Some(sample_rate),
            Some(pcm_le_bytes),
            None,
            None,
            None,
            None,
            Some(playback_key),
            None,
            None,
            None,
            std::time::Duration::from_secs(3),
        )?;
        if response.get("ok").and_then(serde_json::Value::as_bool) == Some(true)
            && response.get("state").and_then(serde_json::Value::as_str) == Some("ready")
        {
            let data = response
                .get("data")
                .cloned()
                .ok_or_else(|| "播放入队回执缺少状态".to_owned())?;
            if data
                .get("enqueued_ms")
                .and_then(serde_json::Value::as_u64)
                .is_none()
            {
                return Err("播放入队回执缺少时长".into());
            }
            return Ok(data);
        }
        Err(response
            .get("data")
            .and_then(|value| value.get("error"))
            .and_then(serde_json::Value::as_str)
            .unwrap_or("播放请求被设备拒绝")
            .to_owned())
    }

    /// playback_prepare_replacement（准备替换）：返回原始状态，让上层保留未知性。
    pub fn playback_prepare_replacement(
        &mut self,
        replacement_id: &str,
        old_playback_key: &str,
        max_ms: u32,
    ) -> Result<serde_json::Value, String> {
        if !valid_identifier(replacement_id)
            || !valid_identifier(old_playback_key)
            || !(1..=600).contains(&max_ms)
        {
            return Err("播放替换编号、旧播放键或收尾上限无效".into());
        }
        self.exchange_with_payload(
            "playback_prepare_replacement",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            Some(old_playback_key),
            Some(replacement_id),
            Some(max_ms),
            None,
            std::time::Duration::from_secs(3),
        )
    }

    /// playback_bind_replacement（绑定后继）：后续帧必须使用此前固定的播放键。
    pub fn playback_bind_replacement(
        &mut self,
        replacement_id: &str,
        successor_playback_key: &str,
    ) -> Result<serde_json::Value, String> {
        if !valid_identifier(replacement_id) || !valid_identifier(successor_playback_key) {
            return Err("播放替换编号或后继播放键无效".into());
        }
        self.exchange_with_payload(
            "playback_bind_replacement",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            Some(successor_playback_key),
            Some(replacement_id),
            None,
            None,
            std::time::Duration::from_secs(3),
        )
    }

    /// playback_abort_replacement（撤销替换）：未知设备状态以原始回执交给事务层处理。
    pub fn playback_abort_replacement(
        &mut self,
        replacement_id: &str,
        reason: &str,
    ) -> Result<serde_json::Value, String> {
        if !valid_identifier(replacement_id) || !valid_identifier(reason) {
            return Err("播放替换编号或撤销原因无效".into());
        }
        self.exchange_with_payload(
            "playback_abort_replacement",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            Some(replacement_id),
            None,
            Some(reason),
            std::time::Duration::from_secs(3),
        )
    }

    /// playback_replacement_status（查询替换）：使用原替换编号读取设备事实，不再次执行替换。
    pub fn playback_replacement_status(
        &mut self,
        replacement_id: &str,
    ) -> Result<serde_json::Value, String> {
        if !valid_identifier(replacement_id) {
            return Err("播放替换编号无效".into());
        }
        self.exchange_with_payload(
            "playback_replacement_status",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            Some(replacement_id),
            None,
            None,
            std::time::Duration::from_secs(3),
        )
    }

    /// playback_snapshot（读取播放快照）：读取缓存的端点时钟及队列状态。
    pub fn playback_snapshot(&mut self) -> Result<serde_json::Value, String> {
        let response =
            self.exchange("playback_snapshot", None, std::time::Duration::from_secs(3))?;
        if response.get("ok").and_then(serde_json::Value::as_bool) == Some(true)
            && response.get("state").and_then(serde_json::Value::as_str) == Some("ready")
        {
            response
                .get("data")
                .cloned()
                .ok_or_else(|| "播放快照回执缺少数据".to_owned())
        } else {
            Err(response
                .get("data")
                .and_then(|value| value.get("error"))
                .and_then(serde_json::Value::as_str)
                .unwrap_or("播放快照不可用")
                .to_owned())
        }
    }

    /// playback_pause（等待暂停确认）：播放队列保留，设备时钟在确认后冻结。
    pub fn playback_pause(&mut self) -> Result<serde_json::Value, String> {
        self.playback_transition("playback_pause", true)
    }

    /// playback_resume（等待恢复确认）：从 WASAPI 已冻结位置继续消耗原队列。
    pub fn playback_resume(&mut self) -> Result<serde_json::Value, String> {
        self.playback_transition("playback_resume", false)
    }

    fn playback_transition(
        &mut self,
        operation: &str,
        expected_paused: bool,
    ) -> Result<serde_json::Value, String> {
        let response = self.exchange(operation, None, std::time::Duration::from_secs(3))?;
        if response.get("ok").and_then(serde_json::Value::as_bool) == Some(true)
            && response.get("state").and_then(serde_json::Value::as_str) == Some("ready")
        {
            let data = response
                .get("data")
                .cloned()
                .ok_or("播放暂停/恢复回执缺少数据")?;
            if data.get("playing").and_then(serde_json::Value::as_bool) != Some(true)
                || data.get("paused").and_then(serde_json::Value::as_bool) != Some(expected_paused)
            {
                return Err("播放暂停/恢复回执与请求状态不匹配".into());
            }
            return Ok(data);
        }
        Err(response
            .get("data")
            .and_then(|value| value.get("error"))
            .and_then(serde_json::Value::as_str)
            .unwrap_or("设备未确认播放暂停/恢复状态")
            .to_owned())
    }

    /// playback_stop（停止播放）：释放默认输出端点及设备时钟。
    pub fn playback_stop(&mut self) -> Result<(), String> {
        let response = self.exchange("playback_stop", None, std::time::Duration::from_secs(3))?;
        if response.get("ok").and_then(serde_json::Value::as_bool) == Some(true)
            && response.get("state").and_then(serde_json::Value::as_str) == Some("ready")
        {
            return Ok(());
        } else {
            Err(response
                .get("data")
                .and_then(|value| value.get("error"))
                .and_then(serde_json::Value::as_str)
                .unwrap_or("设备工作进程未确认停止播放")
                .to_owned())
        }
    }

    /// shutdown（优雅关闭）：先请求工作进程释放能力，再等待进程自行退出。
    pub fn shutdown(&mut self) -> Result<(), String> {
        if self.shutdown_requested {
            return self.wait_for_exit(std::time::Duration::from_secs(2));
        }
        if self
            .child
            .try_wait()
            .map_err(|error| format!("查询设备工作进程失败：{error}"))?
            .is_some()
        {
            return Ok(());
        }
        let response = self.exchange("shutdown", None, std::time::Duration::from_secs(2))?;
        if response.get("ok").and_then(serde_json::Value::as_bool) != Some(true)
            || response.get("state").and_then(serde_json::Value::as_str) != Some("stopping")
        {
            return Err("设备工作进程未确认关闭请求".into());
        }
        self.shutdown_requested = true;
        self.input.take();
        self.wait_for_exit(std::time::Duration::from_secs(2))
    }

    fn wait_for_exit(&mut self, timeout: std::time::Duration) -> Result<(), String> {
        let deadline = std::time::Instant::now() + timeout;
        loop {
            if self
                .child
                .try_wait()
                .map_err(|error| format!("等待设备工作进程失败：{error}"))?
                .is_some()
            {
                return Ok(());
            }
            if std::time::Instant::now() >= deadline {
                return Err("设备工作进程优雅退出超时".into());
            }
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
    }

    /// stop（停止）：先请求资源释放，超时后由 Job Object（作业对象）强制回收。
    pub fn stop(&mut self) {
        let graceful = self.shutdown().is_ok();
        self.input.take();
        self.replies.take();
        if !graceful {
            self.job.terminate();
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
        if let Some(reader) = self.reader.take() {
            let _ = reader.join();
        }
    }
}

#[cfg(windows)]
impl Drop for ManagedWorker {
    fn drop(&mut self) {
        self.stop();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn frame(value: serde_json::Value) -> Vec<u8> {
        let bytes = serde_json::to_vec(&value).unwrap();
        [
            (bytes.len() as u32).to_be_bytes().as_slice(),
            bytes.as_slice(),
        ]
        .concat()
    }
    #[test]
    fn authentication_status_and_shutdown_use_bounded_frames() {
        let token = "a".repeat(64);
        let mut data = frame(
            serde_json::json!({"protocol_version":1,"request_id":"a","operation":"authenticate","token":token}),
        );
        data.extend(frame(
            serde_json::json!({"protocol_version":1,"request_id":"b","operation":"status"}),
        ));
        data.extend(frame(
            serde_json::json!({"protocol_version":1,"request_id":"c","operation":"shutdown"}),
        ));
        let mut output = Vec::new();
        serve(&mut data.as_slice(), &mut output, &token).unwrap();
        let mut reader = output.as_slice();
        for state in ["ready", "ready", "stopping"] {
            let result: serde_json::Value =
                serde_json::from_slice(&read_frame(&mut reader).unwrap().unwrap()).unwrap();
            assert_eq!(result["state"], state);
        }
    }
    #[test]
    fn rejects_wrong_token_reuse_and_oversized_frame() {
        let token = "a".repeat(64);
        let auth = frame(
            serde_json::json!({"protocol_version":1,"request_id":"a","operation":"authenticate","token":token}),
        );
        assert!(serve(&mut auth.as_slice(), &mut Vec::new(), &"b".repeat(64)).is_err());
        let repeated = [auth.as_slice(), auth.as_slice()].concat();
        assert!(serve(&mut repeated.as_slice(), &mut Vec::new(), &token).is_err());
        assert!(read_frame(&mut ((MAX_FRAME as u32 + 1).to_be_bytes().as_slice())).is_err());
    }

    #[cfg(windows)]
    #[test]
    fn desktop_helper_health_reports_missing_packaged_script_without_opening_desktop() {
        let token = "a".repeat(64);
        let mut data = frame(
            serde_json::json!({"protocol_version":1,"request_id":"a","operation":"authenticate","token":token}),
        );
        data.extend(frame(
            serde_json::json!({"protocol_version":1,"request_id":"b","operation":"desktop_helper_health"}),
        ));
        data.extend(frame(
            serde_json::json!({"protocol_version":1,"request_id":"c","operation":"shutdown"}),
        ));
        let mut output = Vec::new();
        serve_with_helper_path(&mut data.as_slice(), &mut output, &token, None).unwrap();
        let mut reader = output.as_slice();
        let _authenticated: serde_json::Value =
            serde_json::from_slice(&read_frame(&mut reader).unwrap().unwrap()).unwrap();
        let health: serde_json::Value =
            serde_json::from_slice(&read_frame(&mut reader).unwrap().unwrap()).unwrap();
        assert_eq!(health["ok"], false);
        assert_eq!(health["state"], "unavailable");
        assert!(health["data"]["error"]
            .as_str()
            .unwrap()
            .contains("没有已登记桌面辅助脚本"));
    }

    #[test]
    fn desktop_helper_control_rejects_unscoped_or_unbounded_operations() {
        let token = "a".repeat(64);
        for request in [
            serde_json::json!({"protocol_version":1,"request_id":"b","operation":"desktop_helper_call",
                "helper_request_id":"c","helper_operation":"powershell","helper_arguments":{"script":"exit"},"helper_timeout_ms":1000}),
            serde_json::json!({"protocol_version":1,"request_id":"b","operation":"desktop_helper_call",
                "helper_request_id":"c","helper_operation":"launch","helper_arguments":{"path":"notepad.exe"},"helper_timeout_ms":1000}),
            serde_json::json!({"protocol_version":1,"request_id":"b","operation":"desktop_helper_call",
                "helper_request_id":"c","helper_operation":"health","helper_arguments":{},"helper_timeout_ms":1000}),
            serde_json::json!({"protocol_version":1,"request_id":"b","operation":"desktop_helper_call",
                "helper_request_id":"c","helper_operation":"windows","helper_arguments":[],"helper_timeout_ms":1000}),
            serde_json::json!({"protocol_version":1,"request_id":"b","operation":"desktop_helper_call",
                "helper_request_id":"c","helper_operation":"windows","helper_arguments":{},"helper_timeout_ms":600001}),
            serde_json::json!({"protocol_version":1,"request_id":"b","operation":"status",
                "helper_request_id":"c","helper_operation":"windows","helper_arguments":{},"helper_timeout_ms":1000}),
        ] {
            let mut data = frame(
                serde_json::json!({"protocol_version":1,"request_id":"a","operation":"authenticate","token":token}),
            );
            data.extend(frame(request));
            assert!(serve(&mut data.as_slice(), &mut Vec::new(), &token).is_err());
        }
    }

    #[test]
    fn capture_reads_are_explicit_bounded_and_do_not_open_a_device_implicitly() {
        let token = "a".repeat(64);
        let mut data = frame(
            serde_json::json!({"protocol_version":1,"request_id":"a","operation":"authenticate","token":token}),
        );
        data.extend(frame(
            serde_json::json!({"protocol_version":1,"request_id":"b","operation":"capture_next","wait_ms":20}),
        ));
        data.extend(frame(
            serde_json::json!({"protocol_version":1,"request_id":"c","operation":"capture_stop"}),
        ));
        data.extend(frame(
            serde_json::json!({"protocol_version":1,"request_id":"d","operation":"shutdown"}),
        ));
        let mut output = Vec::new();
        serve(&mut data.as_slice(), &mut output, &token).unwrap();
        let mut reader = output.as_slice();
        let responses: Vec<serde_json::Value> = (0..4)
            .map(|_| serde_json::from_slice(&read_frame(&mut reader).unwrap().unwrap()).unwrap())
            .collect();
        assert_eq!(responses[0]["ok"], true);
        assert_eq!(responses[1]["ok"], false);
        assert_eq!(responses[1]["state"], "unavailable");
        assert_eq!(responses[2]["data"]["capturing"], false);
        assert_eq!(responses[3]["state"], "stopping");
    }

    #[test]
    fn capture_wait_cannot_be_attached_to_other_operations_or_exceed_limit() {
        let token = "a".repeat(64);
        for request in [
            serde_json::json!({"protocol_version":1,"request_id":"b","operation":"status","wait_ms":20}),
            serde_json::json!({"protocol_version":1,"request_id":"b","operation":"capture_next","wait_ms":251}),
            serde_json::json!({"protocol_version":1,"request_id":"b","operation":"capture_next","wait_ms":0}),
        ] {
            let mut data = frame(
                serde_json::json!({"protocol_version":1,"request_id":"a","operation":"authenticate","token":token}),
            );
            data.extend(frame(request));
            assert!(serve(&mut data.as_slice(), &mut Vec::new(), &token).is_err());
        }
    }

    #[test]
    fn playback_snapshot_and_stop_do_not_open_the_default_output_endpoint() {
        let token = "a".repeat(64);
        let mut data = frame(
            serde_json::json!({"protocol_version":1,"request_id":"a","operation":"authenticate","token":token}),
        );
        data.extend(frame(
            serde_json::json!({"protocol_version":1,"request_id":"b","operation":"playback_snapshot"}),
        ));
        data.extend(frame(
            serde_json::json!({"protocol_version":1,"request_id":"c","operation":"playback_pause"}),
        ));
        data.extend(frame(
            serde_json::json!({"protocol_version":1,"request_id":"d","operation":"playback_resume"}),
        ));
        data.extend(frame(
            serde_json::json!({"protocol_version":1,"request_id":"e","operation":"playback_stop"}),
        ));
        data.extend(frame(
            serde_json::json!({"protocol_version":1,"request_id":"f","operation":"shutdown"}),
        ));
        let mut output = Vec::new();
        serve(&mut data.as_slice(), &mut output, &token).unwrap();
        let mut reader = output.as_slice();
        let responses: Vec<serde_json::Value> = (0..6)
            .map(|_| serde_json::from_slice(&read_frame(&mut reader).unwrap().unwrap()).unwrap())
            .collect();
        assert_eq!(responses[1]["ok"], true);
        assert_eq!(responses[1]["data"]["playing"], false);
        assert_eq!(responses[2]["ok"], false);
        assert_eq!(responses[2]["state"], "unavailable");
        assert_eq!(responses[3]["ok"], false);
        assert_eq!(responses[3]["state"], "unavailable");
        assert_eq!(responses[4]["data"]["playing"], false);
    }

    #[test]
    fn playback_payload_requires_an_allowed_rate_and_bounded_even_pcm_bytes() {
        let token = "a".repeat(64);
        for request in [
            serde_json::json!({"protocol_version":1,"request_id":"b","operation":"playback_enqueue","sample_rate":22_050,"pcm_le_bytes":[0,0]}),
            serde_json::json!({"protocol_version":1,"request_id":"b","operation":"playback_enqueue","sample_rate":16_000,"pcm_le_bytes":[0]}),
            serde_json::json!({"protocol_version":1,"request_id":"b","operation":"playback_enqueue","sample_rate":16_000,"pcm_le_bytes":vec![0; MAX_PLAYBACK_PCM_BYTES + 1]}),
            serde_json::json!({"protocol_version":1,"request_id":"b","operation":"status","sample_rate":16_000,"pcm_le_bytes":[0,0]}),
        ] {
            let mut data = frame(
                serde_json::json!({"protocol_version":1,"request_id":"a","operation":"authenticate","token":token}),
            );
            data.extend(frame(request));
            assert!(serve(&mut data.as_slice(), &mut Vec::new(), &token).is_err());
        }
    }

    #[test]
    fn playback_replacement_requests_are_strict_and_never_claim_without_an_endpoint() {
        let token = "a".repeat(64);
        for request in [
            serde_json::json!({"protocol_version":1,"request_id":"b","operation":"playback_prepare_replacement",
                "replacement_id":"replacement-1","playback_key":"old-key"}),
            serde_json::json!({"protocol_version":1,"request_id":"b","operation":"playback_prepare_replacement",
                "replacement_id":"replacement-1 ","playback_key":"old-key","max_ms":300}),
            serde_json::json!({"protocol_version":1,"request_id":"b","operation":"playback_prepare_replacement",
                "replacement_id":"replacement-1","playback_key":"old-key","max_ms":601}),
            serde_json::json!({"protocol_version":1,"request_id":"b","operation":"playback_bind_replacement",
                "replacement_id":"replacement-1","playback_key":""}),
            serde_json::json!({"protocol_version":1,"request_id":"b","operation":"playback_abort_replacement",
                "replacement_id":"replacement-1","reason":""}),
            serde_json::json!({"protocol_version":1,"request_id":"b","operation":"playback_replacement_status",
                "replacement_id":"replacement-1","max_ms":300}),
            serde_json::json!({"protocol_version":1,"request_id":"b","operation":"status",
                "playback_key":"unexpected"}),
        ] {
            let mut data = frame(
                serde_json::json!({"protocol_version":1,"request_id":"a","operation":"authenticate","token":token}),
            );
            data.extend(frame(request));
            assert!(serve(&mut data.as_slice(), &mut Vec::new(), &token).is_err());
        }

        let mut data = frame(
            serde_json::json!({"protocol_version":1,"request_id":"a","operation":"authenticate","token":token}),
        );
        for request in [
            serde_json::json!({"protocol_version":1,"request_id":"b","operation":"playback_enqueue",
                "playback_key":"old-key","sample_rate":16_000,"pcm_le_bytes":[0,0]}),
            serde_json::json!({"protocol_version":1,"request_id":"c","operation":"playback_prepare_replacement",
                "replacement_id":"replacement-1","playback_key":"old-key","max_ms":300}),
            serde_json::json!({"protocol_version":1,"request_id":"d","operation":"playback_bind_replacement",
                "replacement_id":"replacement-1","playback_key":"new-key"}),
            serde_json::json!({"protocol_version":1,"request_id":"e","operation":"playback_abort_replacement",
                "replacement_id":"replacement-1","reason":"cancelled"}),
            serde_json::json!({"protocol_version":1,"request_id":"f","operation":"playback_replacement_status",
                "replacement_id":"replacement-1"}),
            serde_json::json!({"protocol_version":1,"request_id":"g","operation":"playback_wait_replacement",
                "replacement_id":"replacement-1"}),
            serde_json::json!({"protocol_version":1,"request_id":"h","operation":"shutdown"}),
        ] {
            data.extend(frame(request));
        }
        let mut output = Vec::new();
        serve(&mut data.as_slice(), &mut output, &token).unwrap();
        let mut reader = output.as_slice();
        let responses: Vec<serde_json::Value> = (0..8)
            .map(|_| serde_json::from_slice(&read_frame(&mut reader).unwrap().unwrap()).unwrap())
            .collect();
        for response in &responses[1..7] {
            assert!(matches!(
                response["state"].as_str(),
                Some("unavailable" | "unsupported")
            ));
            assert_eq!(response["ok"], false);
        }
    }

    #[cfg(all(windows, feature = "native-audio"))]
    #[test]
    fn native_audio_processing_slot_keeps_capture_and_playback_reference_status_together() {
        let mut slot = AudioProcessingSlot::new();
        assert_eq!(slot.status["state"], "stopped");
        assert_eq!(slot.start_capture()["state"], "ready");
        let reference = vec![0_i16; 320]; // 32 kHz（每秒 32,000 个采样）的 10 毫秒参考帧。
        let status = slot.push_reference(32_000, &reference, Some(600));
        assert_eq!(status["state"], "ready");
        assert_eq!(status["protected_interruption_available"], false);

        let mut frame = crate::wasapi_capture::CaptureFrame {
            sequence: 0,
            start_sample: 0,
            device_time_us: 1_000,
            discontinuity_before: false,
            pcm: vec![0_i16; 320],
        };
        let frame_status = slot.process_capture(&mut frame, Some(400));
        assert_eq!(frame_status["near_end_voice"], false);
        assert_eq!(frame_status["protected_interruption_available"], true);
        let frame_status = slot.process_capture(&mut frame, None);
        assert_eq!(frame_status["protected_interruption_available"], false);
        assert_eq!(frame.pcm.len(), 320);
        slot.reset_alignment();
        assert_eq!(slot.status["protected_interruption_available"], false);
        slot.stop_capture();
        assert_eq!(slot.status["state"], "stopped");
    }
}
