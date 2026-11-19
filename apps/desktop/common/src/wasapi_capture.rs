//! WASAPI capture（WASAPI 采集）：20 毫秒、16 kHz、单声道 PCM（脉冲编码采样）。
use std::{
    sync::atomic::{AtomicBool, Ordering},
    sync::mpsc,
    sync::Arc,
    sync::Mutex,
    thread::JoinHandle,
    time::Duration,
};

pub const TARGET_RATE: u32 = 16_000; // TARGET_RATE（核心上传采样率，每秒采样数）。
pub const FRAME_SAMPLES: usize = 320; // FRAME_SAMPLES（20 毫秒帧的单声道采样数）。
pub const CAPTURE_QUEUE_FRAMES: usize = 6; // CAPTURE_QUEUE_FRAMES（设备侧有界采集队列容量）。

/// monotonic_device_time_us（设备单调时钟）：与 WASAPI 音频包 QPC 时间戳同源。
#[cfg(windows)]
pub fn monotonic_device_time_us() -> Result<u64, String> {
    use windows_sys::Win32::System::Performance::{
        QueryPerformanceCounter, QueryPerformanceFrequency,
    };
    let mut counter = 0_i64;
    let mut frequency = 0_i64;
    if unsafe { QueryPerformanceCounter(&mut counter) } == 0
        || unsafe { QueryPerformanceFrequency(&mut frequency) } == 0
        || counter < 0
        || frequency <= 0
    {
        return Err("Windows 高精度单调时钟不可用。".into());
    }
    let microseconds = (counter as u128)
        .checked_mul(1_000_000)
        .ok_or("Windows 单调时钟换算溢出。")?
        / frequency as u128;
    u64::try_from(microseconds).map_err(|_| "Windows 单调时钟超出设备协议范围。".into())
}

/// monotonic_device_time_us（设备单调时钟）：非 Windows 不提供设备音频时钟。
#[cfg(not(windows))]
pub fn monotonic_device_time_us() -> Result<u64, String> {
    Err("设备 QPC 时钟只在 Windows 验收构建中提供。".into())
}

/// CaptureFrame（采集帧）：PCM 按小端 int16（16 位有符号采样）编码。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CaptureFrame {
    pub sequence: u64,              // sequence（连续帧编号）。
    pub start_sample: u64,          // start_sample（设备流中的首个 16 kHz 采样位置）。
    pub device_time_us: u64,        // device_time_us（设备单调时钟时刻，微秒）。
    pub discontinuity_before: bool, // discontinuity_before（本帧前检测到设备采样缺口）。
    pub pcm: Vec<i16>,              // pcm（固定 320 个单声道原始采样）。
}

impl CaptureFrame {
    pub fn pcm_le_bytes(&self) -> Vec<u8> {
        let mut bytes = Vec::with_capacity(self.pcm.len() * 2);
        for sample in &self.pcm {
            bytes.extend_from_slice(&sample.to_le_bytes());
        }
        bytes
    }
}

/// SampleEncoding（采样编码）：支持 Windows 默认混音格式的常见 PCM 与 float。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SampleEncoding {
    PcmUnsigned8, // pcm_unsigned_8（8 位无符号 PCM）。
    PcmSigned16,  // pcm_signed_16（16 位有符号 PCM）。
    PcmSigned24,  // pcm_signed_24（24 位有符号 PCM）。
    PcmSigned32,  // pcm_signed_32（32 位有符号 PCM）。
    Float32,      // float_32（32 位浮点 PCM）。
}

/// CaptureFormat（采集格式）：按 WASAPI mix format（系统混音格式）解释原始包。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CaptureFormat {
    pub sample_rate: u32,         // sample_rate（输入端点采样率）。
    pub channels: u16,            // channels（输入端点声道数）。
    pub block_align: u16,         // block_align（每输入音频帧字节数）。
    pub encoding: SampleEncoding, // encoding（输入 PCM 编码）。
}

impl CaptureFormat {
    pub fn validate(self) -> Result<Self, String> {
        let bytes_per_sample = match self.encoding {
            SampleEncoding::PcmUnsigned8 => 1,
            SampleEncoding::PcmSigned16 => 2,
            SampleEncoding::PcmSigned24 => 3,
            SampleEncoding::PcmSigned32 | SampleEncoding::Float32 => 4,
        };
        if !(8_000..=192_000).contains(&self.sample_rate)
            || !(1..=8).contains(&self.channels)
            || self.block_align as usize != bytes_per_sample * self.channels as usize
        {
            return Err("WASAPI 默认采集格式或通道布局不受支持".into());
        }
        Ok(self)
    }
}

/// Pcm16CaptureConverter（采集转换器）：保留低通状态、采样位置及跨包重采样相位。
pub struct Pcm16CaptureConverter {
    format: CaptureFormat,
    filter: LowPass,
    previous: Option<f32>,
    expected_device_position: Option<u64>,
    next_output_position: f64,
    output_step: f64,
    pending: Vec<i16>,
    pending_start_sample: u64,
    pending_device_time_us: u64,
    pending_discontinuity: bool,
    discontinuity_pending: bool,
    preserve_position_on_next_packet: bool, // preserve_position_on_next_packet（更换默认端点后保留逻辑采样位置）。
    next_sequence: u64,
    next_start_sample: u64,
}

impl Pcm16CaptureConverter {
    pub fn new(format: CaptureFormat) -> Result<Self, String> {
        let format = format.validate()?;
        Ok(Self {
            format,
            filter: LowPass::new(format.sample_rate),
            previous: None,
            expected_device_position: None,
            next_output_position: 0.0,
            output_step: format.sample_rate as f64 / TARGET_RATE as f64,
            pending: Vec::with_capacity(FRAME_SAMPLES),
            pending_start_sample: 0,
            pending_device_time_us: 0,
            pending_discontinuity: false,
            discontinuity_pending: false,
            preserve_position_on_next_packet: false,
            next_sequence: 0,
            next_start_sample: 0,
        })
    }

    /// push_packet（处理端点包）：每次输入包结束后必须先释放 WASAPI 缓冲再调用。
    pub fn push_packet(
        &mut self,
        bytes: Option<&[u8]>,
        frames: u32,
        device_position: u64,
        device_time_us: u64,
        force_discontinuity: bool,
    ) -> Result<Vec<CaptureFrame>, String> {
        let frames = frames as usize;
        if frames == 0 {
            return Ok(Vec::new());
        }
        let required_bytes = frames
            .checked_mul(self.format.block_align as usize)
            .ok_or("WASAPI 包长度溢出")?;
        if let Some(payload) = bytes {
            if payload.len() != required_bytes {
                return Err("WASAPI 包长度与采样格式不一致".into());
            }
        }
        if let Some(expected) = self.expected_device_position {
            if device_position != expected || force_discontinuity {
                self.reset_at(device_position);
                self.discontinuity_pending = true;
            }
        } else {
            if self.preserve_position_on_next_packet {
                self.next_output_position = device_position as f64;
                self.preserve_position_on_next_packet = false;
                self.discontinuity_pending = true;
            } else {
                self.reset_at(device_position);
                // 首包没有同一流的先前采样位置。端点重建走上方保留连续性证据的分支。
                self.discontinuity_pending = false;
            }
        }
        self.expected_device_position = Some(device_position.saturating_add(frames as u64));
        let mut complete = Vec::new();
        let block_align = self.format.block_align as usize;
        for frame_index in 0..frames {
            let sample = if let Some(payload) = bytes {
                self.decode_mono(payload, frame_index, block_align)?
            } else {
                0.0
            };
            let sample = self.filter.process(sample);
            let absolute = device_position.saturating_add(frame_index as u64);
            if let Some(previous) = self.previous {
                let lower = absolute.saturating_sub(1) as f64;
                while self.next_output_position <= absolute as f64 + 1e-9 {
                    let fraction = (self.next_output_position - lower).clamp(0.0, 1.0);
                    let output = previous + (sample - previous) * fraction as f32;
                    let offset = ((self.next_output_position - device_position as f64).max(0.0)
                        * 1_000_000.0
                        / self.format.sample_rate as f64)
                        .round() as u64;
                    self.append_output(
                        output,
                        device_time_us.saturating_add(offset),
                        &mut complete,
                    );
                    self.next_output_position += self.output_step;
                }
            } else {
                self.next_output_position = absolute as f64;
                self.append_output(sample, device_time_us, &mut complete);
                self.next_output_position += self.output_step;
            }
            self.previous = Some(sample);
        }
        Ok(complete)
    }

    fn decode_mono(
        &self,
        packet: &[u8],
        frame_index: usize,
        block_align: usize,
    ) -> Result<f32, String> {
        let channels = self.format.channels as usize;
        let bytes = match self.format.encoding {
            SampleEncoding::PcmUnsigned8 => 1,
            SampleEncoding::PcmSigned16 => 2,
            SampleEncoding::PcmSigned24 => 3,
            SampleEncoding::PcmSigned32 | SampleEncoding::Float32 => 4,
        };
        let frame_start = frame_index * block_align;
        let mut mixed = 0.0_f32;
        for channel in 0..channels {
            let start = frame_start + channel * bytes;
            let sample = match self.format.encoding {
                SampleEncoding::PcmUnsigned8 => (packet[start] as f32 - 128.0) / 128.0,
                SampleEncoding::PcmSigned16 => {
                    i16::from_le_bytes([packet[start], packet[start + 1]]) as f32 / 32768.0
                }
                SampleEncoding::PcmSigned24 => {
                    let raw = (packet[start] as i32)
                        | ((packet[start + 1] as i32) << 8)
                        | ((packet[start + 2] as i32) << 16);
                    let signed = (raw << 8) >> 8;
                    signed as f32 / 8_388_608.0
                }
                SampleEncoding::PcmSigned32 => {
                    i32::from_le_bytes([
                        packet[start],
                        packet[start + 1],
                        packet[start + 2],
                        packet[start + 3],
                    ]) as f32
                        / 2_147_483_648.0
                }
                SampleEncoding::Float32 => {
                    let value = f32::from_le_bytes([
                        packet[start],
                        packet[start + 1],
                        packet[start + 2],
                        packet[start + 3],
                    ]);
                    if value.is_finite() {
                        value.clamp(-1.0, 1.0)
                    } else {
                        0.0
                    }
                }
            };
            mixed += sample;
        }
        Ok((mixed / channels as f32).clamp(-1.0, 1.0))
    }

    fn append_output(
        &mut self,
        sample: f32,
        device_time_us: u64,
        complete: &mut Vec<CaptureFrame>,
    ) {
        if self.pending.is_empty() {
            self.pending_start_sample = self.next_start_sample;
            self.pending_device_time_us = device_time_us;
            self.pending_discontinuity = self.discontinuity_pending;
            self.discontinuity_pending = false;
        }
        let scaled = (sample.clamp(-1.0, 1.0) * 32768.0)
            .round()
            .clamp(-32768.0, 32767.0);
        self.pending.push(scaled as i16);
        if self.pending.len() == FRAME_SAMPLES {
            complete.push(CaptureFrame {
                sequence: self.next_sequence,
                start_sample: self.pending_start_sample,
                device_time_us: self.pending_device_time_us,
                discontinuity_before: self.pending_discontinuity,
                pcm: std::mem::replace(&mut self.pending, Vec::with_capacity(FRAME_SAMPLES)),
            });
            self.next_sequence = self.next_sequence.saturating_add(1);
            self.next_start_sample = self.next_start_sample.saturating_add(FRAME_SAMPLES as u64);
        }
    }

    fn reset_at(&mut self, device_position: u64) {
        self.pending.clear();
        self.previous = None;
        self.filter.reset();
        self.next_output_position = device_position as f64;
        let output_sample = (device_position as u128 * TARGET_RATE as u128
            / self.format.sample_rate as u128)
            .min(u64::MAX as u128) as u64;
        self.next_start_sample = output_sample;
        self.discontinuity_pending = false;
        self.preserve_position_on_next_packet = false;
    }

    /// continuation_position（连续采样位置）：返回端点重建后应沿用的序号与逻辑采样位置。
    fn continuation_position(&self) -> (u64, u64) {
        let skip_partial = if self.pending.is_empty() {
            0
        } else {
            FRAME_SAMPLES as u64
        };
        (
            self.next_sequence,
            self.next_start_sample.saturating_add(skip_partial),
        )
    }

    /// resume_after_endpoint_change（默认端点更换后恢复）：清除旧滤波/相位状态并标记缺口。
    fn resume_after_endpoint_change(
        &mut self,
        format: CaptureFormat,  // format（新默认端点混音格式）。
        next_sequence: u64,     // next_sequence（后续连续采集帧编号）。
        next_start_sample: u64, // next_start_sample（后续逻辑采样位置）。
    ) -> Result<(), String> {
        self.format = format.validate()?;
        self.filter = LowPass::new(format.sample_rate);
        self.previous = None;
        self.expected_device_position = None;
        self.next_output_position = 0.0;
        self.output_step = format.sample_rate as f64 / TARGET_RATE as f64;
        self.pending.clear();
        self.pending_start_sample = next_start_sample;
        self.pending_device_time_us = 0;
        self.pending_discontinuity = true;
        self.discontinuity_pending = true;
        self.preserve_position_on_next_packet = true;
        self.next_sequence = next_sequence;
        self.next_start_sample = next_start_sample;
        Ok(())
    }
}

struct LowPass {
    b0: f64,
    b1: f64,
    b2: f64,
    a1: f64,
    a2: f64,
    x1: f64,
    x2: f64,
    y1: f64,
    y2: f64,
}

impl LowPass {
    fn new(sample_rate: u32) -> Self {
        if sample_rate <= TARGET_RATE {
            return Self {
                b0: 1.0,
                b1: 0.0,
                b2: 0.0,
                a1: 0.0,
                a2: 0.0,
                x1: 0.0,
                x2: 0.0,
                y1: 0.0,
                y2: 0.0,
            };
        }
        let cutoff = 7_000.0_f64.min(sample_rate as f64 * 0.45);
        let omega = 2.0 * std::f64::consts::PI * cutoff / sample_rate as f64;
        let cosine = omega.cos();
        let alpha = omega.sin() / 2.0_f64.sqrt();
        let a0 = 1.0 + alpha;
        let b0 = ((1.0 - cosine) / 2.0) / a0;
        let b1 = (1.0 - cosine) / a0;
        let b2 = b0;
        let a1 = (-2.0 * cosine) / a0;
        let a2 = (1.0 - alpha) / a0;
        Self {
            b0,
            b1,
            b2,
            a1,
            a2,
            x1: 0.0,
            x2: 0.0,
            y1: 0.0,
            y2: 0.0,
        }
    }

    fn process(&mut self, sample: f32) -> f32 {
        let x = sample as f64;
        let y = self.b0 * x + self.b1 * self.x1 + self.b2 * self.x2
            - self.a1 * self.y1
            - self.a2 * self.y2;
        self.x2 = self.x1;
        self.x1 = x;
        self.y2 = self.y1;
        self.y1 = y;
        y as f32
    }

    fn reset(&mut self) {
        self.x1 = 0.0;
        self.x2 = 0.0;
        self.y1 = 0.0;
        self.y2 = 0.0;
    }
}

#[cfg(windows)]
mod windows_capture {
    use super::*;
    use std::{ffi::c_void, ptr, sync::mpsc::SyncSender};
    use windows::core::GUID;
    use windows::Win32::{
        Foundation::HANDLE as ComHandle,
        Media::Audio::{
            eCapture, eConsole, IAudioCaptureClient, IAudioClient, IMMDeviceEnumerator,
            MMDeviceEnumerator, AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY, AUDCLNT_BUFFERFLAGS_SILENT,
            AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR, AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_EVENTCALLBACK, WAVEFORMATEX, WAVEFORMATEXTENSIBLE,
        },
        System::Com::{
            CoCreateInstance, CoInitializeEx, CoTaskMemFree, CoUninitialize, CLSCTX_ALL,
            COINIT_MULTITHREADED,
        },
    };
    use windows_sys::Win32::{
        Foundation::{CloseHandle, HANDLE, WAIT_OBJECT_0, WAIT_TIMEOUT},
        System::Threading::{CreateEventW, WaitForSingleObject},
    };

    const WAVE_FORMAT_IEEE_FLOAT: u16 = 3;
    const WAVE_FORMAT_EXTENSIBLE: u16 = 0xfffe;
    const PCM_SUBFORMAT: GUID = GUID::from_u128(0x00000001_0000_0010_8000_00aa00389b71);
    const FLOAT_SUBFORMAT: GUID = GUID::from_u128(0x00000003_0000_0010_8000_00aa00389b71);

    struct ComApartment;
    impl Drop for ComApartment {
        fn drop(&mut self) {
            unsafe { CoUninitialize() };
        }
    }

    struct AudioEvent(HANDLE);
    impl Drop for AudioEvent {
        fn drop(&mut self) {
            unsafe { CloseHandle(self.0) };
        }
    }

    pub struct WasapiCaptureStream {
        stop: Arc<AtomicBool>,
        frames: Arc<Mutex<mpsc::Receiver<Result<CaptureFrame, String>>>>, // frames（有界采集帧队列，重建时清除旧端点帧）。
        reconnecting: Arc<AtomicBool>, // reconnecting（默认采集端点正在重建）。
        terminal_error: Arc<Mutex<Option<String>>>,
        thread: Option<JoinHandle<()>>,
    }

    impl WasapiCaptureStream {
        /// start_default（启动默认采集）：只在显式调用时打开系统默认麦克风。
        pub fn start_default() -> Result<Self, String> {
            let stop = Arc::new(AtomicBool::new(false));
            let thread_stop = stop.clone();
            let terminal_error = Arc::new(Mutex::new(None));
            let thread_error = terminal_error.clone();
            let (frame_tx, frame_receiver) = mpsc::sync_channel(CAPTURE_QUEUE_FRAMES);
            let frames = Arc::new(Mutex::new(frame_receiver));
            let thread_frames = frames.clone();
            let reconnecting = Arc::new(AtomicBool::new(false));
            let thread_reconnecting = reconnecting.clone();
            let (ready_tx, ready_rx) = mpsc::sync_channel(1);
            let thread = std::thread::Builder::new()
                .name("wasapi-capture".into())
                .spawn(move || {
                    let result = run_capture(
                        thread_stop,
                        frame_tx.clone(),
                        thread_frames,
                        thread_reconnecting,
                        &ready_tx,
                    );
                    if let Err(error) = result {
                        if let Ok(mut terminal) = thread_error.lock() {
                            *terminal = Some(error.clone());
                        }
                        let _ = frame_tx.try_send(Err(error));
                    }
                })
                .map_err(|error| format!("无法启动 WASAPI 采集线程：{error}"))?;
            match ready_rx.recv_timeout(Duration::from_secs(10)) {
                Ok(Ok(())) => Ok(Self {
                    stop,
                    frames,
                    reconnecting,
                    terminal_error,
                    thread: Some(thread),
                }),
                Ok(Err(error)) => {
                    let _ = thread.join();
                    Err(error)
                }
                Err(error) => {
                    stop.store(true, Ordering::Release);
                    drop(thread); // 超时后交由采集线程观察停止信号，不阻塞调用方等待设备驱动。
                    Err(format!("WASAPI 默认采集启动超时：{error}"))
                }
            }
        }

        /// recv_frame（读取采集帧）：超时返回 None，不会阻塞控制线程超过指定时长。
        pub fn recv_frame(&self, timeout: Duration) -> Result<Option<CaptureFrame>, String> {
            let receiver = self
                .frames
                .lock()
                .map_err(|_| "WASAPI 采集帧接收队列不可用".to_owned())?;
            match receiver.recv_timeout(timeout) {
                Ok(Ok(frame)) => Ok(Some(frame)),
                Ok(Err(error)) => Err(error),
                Err(mpsc::RecvTimeoutError::Timeout) => self.terminal_error(),
                Err(mpsc::RecvTimeoutError::Disconnected) => self.terminal_error(),
            }
        }

        pub fn is_reconnecting(&self) -> bool {
            self.reconnecting.load(Ordering::Acquire)
        }

        fn terminal_error(&self) -> Result<Option<CaptureFrame>, String> {
            match self.terminal_error.lock() {
                Ok(error) => match error.as_ref() {
                    Some(error) => Err(error.clone()),
                    None => Ok(None),
                },
                Err(_) => Err("WASAPI 采集错误状态不可用".into()),
            }
        }

        /// stop（停止采集）：事件等待最多 50 毫秒后释放 WASAPI 流。
        pub fn stop(&mut self) -> Result<(), String> {
            self.stop.store(true, Ordering::Release);
            if let Some(thread) = self.thread.take() {
                if thread.join().is_err() {
                    return Err("WASAPI 采集线程异常退出".into());
                }
            }
            match self.terminal_error.lock() {
                Ok(error) => match error.as_ref() {
                    Some(error) => Err(error.clone()),
                    None => Ok(()),
                },
                Err(_) => Err("WASAPI 采集错误状态不可用".into()),
            }
        }
    }

    impl Drop for WasapiCaptureStream {
        fn drop(&mut self) {
            let _ = self.stop();
        }
    }

    struct Session {
        enumerator: IMMDeviceEnumerator, // enumerator（用于监视系统默认端点的枚举器）。
        client: IAudioClient,
        capture: IAudioCaptureClient,
        event: AudioEvent,
        converter: Pcm16CaptureConverter,
        endpoint_id: String, // endpoint_id（当前采集流绑定的 Windows 端点编号）。
    }

    fn run_capture(
        stop: Arc<AtomicBool>,
        frames: SyncSender<Result<CaptureFrame, String>>,
        frame_receiver: Arc<Mutex<mpsc::Receiver<Result<CaptureFrame, String>>>>,
        reconnecting: Arc<AtomicBool>,
        ready: &SyncSender<Result<(), String>>,
    ) -> Result<(), String> {
        unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) }
            .ok()
            .map_err(|error| format!("WASAPI COM 初始化失败：{error}"))?;
        let _apartment = ComApartment;
        let mut session = match Session::open(&stop, None) {
            Ok(session) => session,
            Err(error) => {
                let _ = ready.send(Err(error.clone()));
                return Err(error);
            }
        };
        if ready.send(Ok(())).is_err() {
            let _ = unsafe { session.client.Stop() };
            return Err("采集启动等待方已退出".into());
        }
        let mut queue_discontinuity = false;
        let mut next_endpoint_check = std::time::Instant::now() + Duration::from_millis(500);
        let mut result = Ok(());
        while !stop.load(Ordering::Acquire) {
            let mut rebuild_reason = None;
            let wait = unsafe { WaitForSingleObject(session.event.0, 50) };
            if wait == WAIT_OBJECT_0 {
                if let Err(error) = drain_capture(&mut session, &frames, &mut queue_discontinuity) {
                    if crate::device_audio::is_wasapi_device_invalidated(&error) {
                        rebuild_reason = Some(error);
                    } else {
                        result = Err(error);
                        break;
                    }
                }
            } else if wait != WAIT_TIMEOUT {
                result = Err("等待 WASAPI 采集事件失败".into());
                break;
            }
            if rebuild_reason.is_none() && std::time::Instant::now() >= next_endpoint_check {
                next_endpoint_check = std::time::Instant::now() + Duration::from_millis(500);
                match session.default_endpoint_id() {
                    Ok(endpoint_id) if endpoint_id == session.endpoint_id => {}
                    Ok(_) => rebuild_reason = Some("系统默认麦克风已更换".to_owned()),
                    Err(error) => rebuild_reason = Some(format!("系统默认麦克风不可用：{error}")),
                }
            }
            if let Some(reason) = rebuild_reason {
                let (next_sequence, next_start_sample) = session.converter.continuation_position();
                let _ = unsafe { session.client.Stop() };
                drop(session);
                flush_capture_queue(&frame_receiver);
                queue_discontinuity = true;
                reconnecting.store(true, Ordering::Release);
                let mut retry_delay = Duration::from_millis(100);
                let mut recovered = None;
                while !stop.load(Ordering::Acquire) {
                    match Session::open(&stop, Some((next_sequence, next_start_sample))) {
                        Ok(next_session) => {
                            recovered = Some(next_session);
                            break;
                        }
                        Err(_error) => {
                            let until = std::time::Instant::now() + retry_delay;
                            while !stop.load(Ordering::Acquire) && std::time::Instant::now() < until
                            {
                                std::thread::sleep(Duration::from_millis(20));
                            }
                            retry_delay = (retry_delay * 2).min(Duration::from_secs(2));
                        }
                    }
                }
                if stop.load(Ordering::Acquire) {
                    reconnecting.store(false, Ordering::Release);
                    return Ok(());
                }
                let Some(next_session) = recovered else {
                    reconnecting.store(false, Ordering::Release);
                    return Err(format!("默认麦克风重建失败：{reason}"));
                };
                flush_capture_queue(&frame_receiver);
                session = next_session;
                reconnecting.store(false, Ordering::Release);
                next_endpoint_check = std::time::Instant::now() + Duration::from_millis(500);
            }
        }
        let stopped = unsafe { session.client.Stop() }
            .map_err(|error| format!("停止 WASAPI 采集流失败：{error}"));
        result?;
        stopped?;
        Ok(())
    }

    impl Session {
        fn open(stop: &AtomicBool, continuation: Option<(u64, u64)>) -> Result<Self, String> {
            let enumerator: IMMDeviceEnumerator =
                unsafe { CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL) }
                    .map_err(|error| format!("WASAPI 设备枚举器不可用：{error}"))?;
            let device = unsafe { enumerator.GetDefaultAudioEndpoint(eCapture, eConsole) }
                .map_err(|error| format!("系统默认麦克风不可用：{error}"))?;
            let endpoint_id = crate::device_audio::endpoint_id_from_device(&device)?;
            let client: IAudioClient = unsafe { device.Activate(CLSCTX_ALL, None) }
                .map_err(|error| format!("打开默认麦克风失败：{error}"))?;
            let format_ptr = unsafe { client.GetMixFormat() }
                .map_err(|error| format!("读取默认麦克风混音格式失败：{error}"))?;
            if format_ptr.is_null() {
                return Err("WASAPI 返回空麦克风格式".into());
            }
            let format = match parse_mix_format(format_ptr) {
                Ok(format) => format,
                Err(error) => {
                    unsafe { CoTaskMemFree(Some(format_ptr.cast::<c_void>())) };
                    return Err(error);
                }
            };
            let initialized = unsafe {
                client.Initialize(
                    AUDCLNT_SHAREMODE_SHARED,
                    AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
                    0,
                    0,
                    format_ptr,
                    None,
                )
            };
            unsafe { CoTaskMemFree(Some(format_ptr.cast::<c_void>())) };
            initialized.map_err(|error| format!("初始化共享模式采集流失败：{error}"))?;
            let event = unsafe { CreateEventW(ptr::null(), 0, 0, ptr::null()) };
            if event.is_null() {
                return Err("无法创建 WASAPI 采集事件".into());
            }
            let event = AudioEvent(event);
            unsafe { client.SetEventHandle(ComHandle(event.0)) }
                .map_err(|error| format!("注册 WASAPI 采集事件失败：{error}"))?;
            let capture: IAudioCaptureClient = unsafe { client.GetService() }
                .map_err(|error| format!("获取 WASAPI 采集缓冲失败：{error}"))?;
            if stop.load(Ordering::Acquire) {
                return Err("WASAPI 采集启动已取消".into());
            }
            unsafe { client.Start() }.map_err(|error| {
                crate::device_audio::format_wasapi_error("启动 WASAPI 采集流失败", error)
            })?;
            let mut converter = Pcm16CaptureConverter::new(format)?;
            if let Some((next_sequence, next_start_sample)) = continuation {
                converter.resume_after_endpoint_change(format, next_sequence, next_start_sample)?;
            }
            Ok(Self {
                enumerator,
                client,
                capture,
                event,
                converter,
                endpoint_id,
            })
        }

        fn default_endpoint_id(&self) -> Result<String, String> {
            let device = unsafe { self.enumerator.GetDefaultAudioEndpoint(eCapture, eConsole) }
                .map_err(|error| format!("读取当前默认麦克风失败：{error}"))?;
            crate::device_audio::endpoint_id_from_device(&device)
        }
    }

    fn flush_capture_queue(receiver: &Mutex<mpsc::Receiver<Result<CaptureFrame, String>>>) {
        if let Ok(receiver) = receiver.lock() {
            while receiver.try_recv().is_ok() {}
        }
    }

    pub fn parse_mix_format(format_ptr: *mut WAVEFORMATEX) -> Result<CaptureFormat, String> {
        let format = unsafe { format_ptr.read_unaligned() };
        let encoding = match format.wFormatTag {
            1 => pcm_encoding(format.wBitsPerSample)?,
            WAVE_FORMAT_IEEE_FLOAT if format.wBitsPerSample == 32 => SampleEncoding::Float32,
            WAVE_FORMAT_EXTENSIBLE => {
                if format.cbSize < 22 {
                    return Err("WAVEFORMATEXTENSIBLE 扩展长度无效".into());
                }
                let extensible =
                    unsafe { format_ptr.cast::<WAVEFORMATEXTENSIBLE>().read_unaligned() };
                match extensible.SubFormat {
                    PCM_SUBFORMAT => pcm_encoding(format.wBitsPerSample)?,
                    FLOAT_SUBFORMAT if format.wBitsPerSample == 32 => SampleEncoding::Float32,
                    _ => return Err("默认麦克风混音编码暂不支持".into()),
                }
            }
            _ => return Err("默认麦克风混音格式暂不支持".into()),
        };
        CaptureFormat {
            sample_rate: format.nSamplesPerSec,
            channels: format.nChannels,
            block_align: format.nBlockAlign,
            encoding,
        }
        .validate()
    }

    fn pcm_encoding(bits: u16) -> Result<SampleEncoding, String> {
        match bits {
            8 => Ok(SampleEncoding::PcmUnsigned8),
            16 => Ok(SampleEncoding::PcmSigned16),
            24 => Ok(SampleEncoding::PcmSigned24),
            32 => Ok(SampleEncoding::PcmSigned32),
            _ => Err("默认麦克风 PCM 位数暂不支持".into()),
        }
    }

    fn drain_capture(
        session: &mut Session,
        frames: &SyncSender<Result<CaptureFrame, String>>,
        queue_discontinuity: &mut bool,
    ) -> Result<(), String> {
        loop {
            let available = unsafe { session.capture.GetNextPacketSize() }.map_err(|error| {
                crate::device_audio::format_wasapi_error("读取 WASAPI 采集包大小失败", error)
            })?;
            if available == 0 {
                return Ok(());
            }
            let mut data = ptr::null_mut();
            let mut count = 0_u32;
            let mut flags = 0_u32;
            let mut device_position = 0_u64;
            let mut qpc_position = 0_u64;
            unsafe {
                session.capture.GetBuffer(
                    &mut data,
                    &mut count,
                    &mut flags,
                    Some(&mut device_position),
                    Some(&mut qpc_position),
                )
            }
            .map_err(|error| {
                crate::device_audio::format_wasapi_error("读取 WASAPI 采集缓冲失败", error)
            })?;
            if count == 0 {
                unsafe { session.capture.ReleaseBuffer(0) }.map_err(|error| {
                    crate::device_audio::format_wasapi_error("释放空 WASAPI 采集缓冲失败", error)
                })?;
                continue;
            }
            let silent = flags & AUDCLNT_BUFFERFLAGS_SILENT.0 as u32 != 0;
            let discontinuity = flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY.0 as u32 != 0;
            let timestamp_valid = flags & AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR.0 as u32 == 0;
            let length =
                (count as usize).checked_mul(session.converter.format.block_align as usize);
            let payload_result = if length.is_none() {
                Err("WASAPI 采集包长度溢出".to_owned())
            } else if silent {
                Ok(None)
            } else if data.is_null() {
                Err("WASAPI 采集包缺少数据".to_owned())
            } else {
                Ok(Some(
                    unsafe { std::slice::from_raw_parts(data, length.unwrap()) }.to_vec(),
                ))
            };
            unsafe { session.capture.ReleaseBuffer(count) }.map_err(|error| {
                crate::device_audio::format_wasapi_error("释放 WASAPI 采集缓冲失败", error)
            })?;
            let payload = payload_result?;
            let timestamp = if timestamp_valid {
                qpc_position / 10
            } else {
                0
            };
            let converted = session.converter.push_packet(
                payload.as_deref(),
                count,
                device_position,
                timestamp,
                discontinuity,
            )?;
            for frame in converted {
                enqueue_frame(frames, frame, queue_discontinuity)?;
            }
        }
    }

    pub(super) fn enqueue_frame(
        frames: &SyncSender<Result<CaptureFrame, String>>,
        mut frame: CaptureFrame,
        queue_discontinuity: &mut bool,
    ) -> Result<(), String> {
        frame.discontinuity_before |= *queue_discontinuity;
        match frames.try_send(Ok(frame)) {
            Ok(()) => {
                *queue_discontinuity = false;
                Ok(())
            }
            Err(mpsc::TrySendError::Full(_)) => {
                // 队列满时整帧丢弃；下一帧显式标记缺口，接收端必须重建连续流。
                *queue_discontinuity = true;
                Ok(())
            }
            Err(mpsc::TrySendError::Disconnected(_)) => Err("采集帧接收端已关闭".into()),
        }
    }

    #[cfg(test)]
    mod recovery_tests {
        use super::*;

        #[test]
        fn endpoint_recovery_discards_frames_queued_by_the_old_device() {
            let (sender, receiver) = mpsc::sync_channel(CAPTURE_QUEUE_FRAMES);
            for sequence in 0..3 {
                sender
                    .try_send(Ok(CaptureFrame {
                        sequence,
                        start_sample: sequence * FRAME_SAMPLES as u64,
                        device_time_us: sequence * 20_000,
                        discontinuity_before: false,
                        pcm: vec![0; FRAME_SAMPLES],
                    }))
                    .unwrap();
            }
            let receiver = Mutex::new(receiver);
            flush_capture_queue(&receiver);
            assert!(matches!(
                receiver.lock().unwrap().try_recv(),
                Err(mpsc::TryRecvError::Empty)
            ));
        }
    }
}

#[cfg(windows)]
pub(crate) use windows_capture::parse_mix_format;
#[cfg(windows)]
pub use windows_capture::WasapiCaptureStream;

#[cfg(test)]
mod tests {
    use super::*;

    fn format() -> CaptureFormat {
        CaptureFormat {
            sample_rate: 48_000,
            channels: 2,
            block_align: 4,
            encoding: SampleEncoding::PcmSigned16,
        }
    }

    fn stereo_packet(frames: usize, left: i16, right: i16) -> Vec<u8> {
        let mut bytes = Vec::with_capacity(frames * 4);
        for _ in 0..frames {
            bytes.extend_from_slice(&left.to_le_bytes());
            bytes.extend_from_slice(&right.to_le_bytes());
        }
        bytes
    }

    #[test]
    fn emits_mono_16khz_twenty_millisecond_frames_from_stereo_packets() {
        let mut converter = Pcm16CaptureConverter::new(format()).unwrap();
        let packet = stereo_packet(960, 16_000, 16_000);
        let frames = converter
            .push_packet(Some(&packet), 960, 0, 2_000_000, false)
            .unwrap();
        assert_eq!(frames.len(), 1);
        assert_eq!(frames[0].pcm.len(), FRAME_SAMPLES);
        assert_eq!(frames[0].sequence, 0);
        assert_eq!(frames[0].start_sample, 0);
        assert_eq!(frames[0].device_time_us, 2_000_000);
        assert_eq!(frames[0].pcm_le_bytes().len(), FRAME_SAMPLES * 2);
        assert!((frames[0].pcm[300] - 16_000).abs() < 100);
        assert!(!frames[0].discontinuity_before);
    }

    #[test]
    fn resampler_keeps_phase_across_wasapi_packet_boundaries() {
        let packet = stereo_packet(960, -8_000, 8_000);
        let mut whole = Pcm16CaptureConverter::new(format()).unwrap();
        let whole_frame = whole
            .push_packet(Some(&packet), 960, 0, 1_000_000, false)
            .unwrap()
            .remove(0);
        let mut split = Pcm16CaptureConverter::new(format()).unwrap();
        assert!(split
            .push_packet(Some(&packet[..480 * 4]), 480, 0, 1_000_000, false)
            .unwrap()
            .is_empty());
        let split_frame = split
            .push_packet(Some(&packet[480 * 4..]), 480, 480, 1_010_000, false)
            .unwrap()
            .remove(0);
        assert_eq!(split_frame.pcm, whole_frame.pcm);
        assert_eq!(split_frame.start_sample, whole_frame.start_sample);
        assert_eq!(split_frame.device_time_us, whole_frame.device_time_us);
    }

    #[test]
    fn silent_packets_make_silence_and_device_gaps_drop_partial_frames() {
        let mut converter = Pcm16CaptureConverter::new(format()).unwrap();
        assert!(converter
            .push_packet(None, 480, 0, 1_000_000, false)
            .unwrap()
            .is_empty());
        let frames = converter
            .push_packet(None, 960, 960, 1_020_000, false)
            .unwrap();
        assert_eq!(frames.len(), 1);
        assert!(frames[0].pcm.iter().all(|sample| *sample == 0));
        assert_eq!(frames[0].start_sample, 320);
        assert!(frames[0].discontinuity_before);
    }

    #[test]
    fn endpoint_rebuild_keeps_capture_sequence_and_marks_the_first_frame_discontinuous() {
        let mut converter = Pcm16CaptureConverter::new(format()).unwrap();
        let first_packet = stereo_packet(960, 1_000, 1_000);
        let first = converter
            .push_packet(Some(&first_packet), 960, 0, 1_000_000, false)
            .unwrap();
        assert_eq!(first.len(), 1);
        assert_eq!(first[0].sequence, 0);
        assert_eq!(first[0].start_sample, 0);

        let partial_packet = stereo_packet(480, 2_000, 2_000);
        assert!(converter
            .push_packet(Some(&partial_packet), 480, 960, 1_020_000, false)
            .unwrap()
            .is_empty());
        let (next_sequence, next_start_sample) = converter.continuation_position();
        assert_eq!(next_sequence, 1);
        assert_eq!(next_start_sample, 640);

        let changed_format = CaptureFormat {
            sample_rate: 44_100,
            channels: 2,
            block_align: 4,
            encoding: SampleEncoding::PcmSigned16,
        };
        converter
            .resume_after_endpoint_change(changed_format, next_sequence, next_start_sample)
            .unwrap();
        let resumed_packet = stereo_packet(882, 3_000, 3_000);
        let resumed = converter
            .push_packet(Some(&resumed_packet), 882, 0, 1_100_000, false)
            .unwrap();
        assert_eq!(resumed.len(), 1);
        assert_eq!(resumed[0].sequence, 1);
        assert_eq!(resumed[0].start_sample, 640);
        assert_eq!(resumed[0].device_time_us, 1_100_000);
        assert!(resumed[0].discontinuity_before);
    }

    #[cfg(windows)]
    #[test]
    fn capture_queue_overflow_marks_the_next_delivered_frame_discontinuous() {
        let (sender, receiver) = mpsc::sync_channel(CAPTURE_QUEUE_FRAMES);
        let mut queue_discontinuity = false;
        let frame = |sequence| CaptureFrame {
            sequence,
            start_sample: sequence * FRAME_SAMPLES as u64,
            device_time_us: sequence * 20_000,
            discontinuity_before: false,
            pcm: vec![0; FRAME_SAMPLES],
        };
        for sequence in 0..CAPTURE_QUEUE_FRAMES as u64 {
            windows_capture::enqueue_frame(&sender, frame(sequence), &mut queue_discontinuity)
                .unwrap();
        }
        windows_capture::enqueue_frame(
            &sender,
            frame(CAPTURE_QUEUE_FRAMES as u64),
            &mut queue_discontinuity,
        )
        .unwrap();
        assert!(queue_discontinuity);
        assert_eq!(receiver.recv().unwrap().unwrap().sequence, 0);
        windows_capture::enqueue_frame(
            &sender,
            frame(CAPTURE_QUEUE_FRAMES as u64 + 1),
            &mut queue_discontinuity,
        )
        .unwrap();
        let last = receiver.try_iter().last().unwrap().unwrap();
        assert_eq!(last.sequence, CAPTURE_QUEUE_FRAMES as u64 + 1);
        assert!(last.discontinuity_before);
        assert!(!queue_discontinuity);
    }
}
