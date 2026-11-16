//! Native audio processing（原生音频前处理）：AEC、NS（降噪）和近端 WebRTC VAD。
use crate::{native_aec::NativeAecProcessor, native_vad::WebRtcVad, wasapi_capture::CaptureFrame};
use std::time::{Duration, Instant};

const CAPTURE_RATE: u32 = 16_000; // CAPTURE_RATE（设备工作进程对核心输出的采样率）。
const CAPTURE_FRAME_SAMPLES: usize = 320; // CAPTURE_FRAME_SAMPLES（20 毫秒采集帧采样数）。
const PROCESS_FRAME_SAMPLES: usize = 160; // PROCESS_FRAME_SAMPLES（10 毫秒前处理帧采样数）。
const MAX_REFERENCE_AGE: Duration = Duration::from_millis(500); // MAX_REFERENCE_AGE（允许受保护打断的反向参考新鲜度）。
const MAX_RENDER_DELAY_MS: u32 = 500; // MAX_RENDER_DELAY_MS（AEC 支持的最大参考延迟）。

/// ProcessedCaptureFrame（前处理结果）：采集音频与健康证据绑定在同一帧。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessedCaptureFrame {
    pub pcm: Vec<i16>,                // pcm（AEC/降噪后的 16 kHz 单声道采样）。
    pub near_end_voice: Option<bool>, // near_end_voice（近端 VAD 结论；不可用时为空）。
    pub protected_interruption_available: bool, // protected_interruption_available（AEC 反向参考及时且健康）。
    pub processor_state: &'static str,          // processor_state（ready、degraded 或 error）。
    pub reverse_generation: u64,                // reverse_generation（反向播放格式代次）。
    pub error: Option<String>,                  // error（前处理故障详情，不得伪装健康）。
}

/// NativeAudioProcessor（原生音频前处理器）：仅由设备工作进程的控制线程顺序调用。
pub struct NativeAudioProcessor {
    aec: NativeAecProcessor,
    vad: WebRtcVad,
    vad_mode: u8,      // vad_mode（近端 VAD 敏感度）。
    reverse_rate: u32, // reverse_rate（当前反向参考源采样率）。
    reverse_resampler: ReferenceResampler,
    reverse_remainder: Vec<i16>, // reverse_remainder（尚不足一个 10 毫秒参考帧的尾部）。
    reverse_generation: u64,     // reverse_generation（播放参考重设代次）。
    last_reverse_at: Option<Instant>, // last_reverse_at（最近完整参考帧到达时刻）。
    playback_delay_ms: Option<u32>, // playback_delay_ms（最近一次端点缓冲延迟估计）。
    protected_delay_valid: bool, // protected_delay_valid（延迟在 AEC 可支持范围内）。
    state: &'static str,         // state（前处理健康状态）。
    error: Option<String>,       // error（最近一次不可恢复的处理错误）。
}

impl NativeAudioProcessor {
    /// new（创建前处理器）：AEC 与降噪开启、AGC（自动增益）关闭。
    pub fn new(vad_mode: u8) -> Result<Self, String> {
        let mut aec = NativeAecProcessor::new(true, true, false)?;
        aec.set_formats(CAPTURE_RATE, CAPTURE_RATE)?;
        Ok(Self {
            aec,
            vad: WebRtcVad::new(vad_mode)?,
            vad_mode,
            reverse_rate: CAPTURE_RATE,
            reverse_resampler: ReferenceResampler::new(CAPTURE_RATE),
            reverse_remainder: Vec::with_capacity(PROCESS_FRAME_SAMPLES),
            reverse_generation: 0,
            last_reverse_at: None,
            playback_delay_ms: None,
            protected_delay_valid: true,
            state: "ready",
            error: None,
        })
    }

    /// process_capture（处理一个 20 毫秒采集帧）：AEC 和 VAD 均以 10 毫秒帧运行。
    pub fn process_capture(&mut self, frame: &CaptureFrame) -> ProcessedCaptureFrame {
        if frame.pcm.len() != CAPTURE_FRAME_SAMPLES {
            self.degrade("原生前处理收到非 20 毫秒采集帧".into());
            return self.capture_passthrough(frame);
        }
        if frame.discontinuity_before {
            if let Err(error) = self.reset_stream() {
                self.degrade(error);
                return self.capture_passthrough(frame);
            }
        }
        if self.state != "ready" {
            return self.capture_passthrough(frame);
        }

        let mut processed = Vec::with_capacity(CAPTURE_FRAME_SAMPLES);
        let mut voice = false;
        for input in frame.pcm.chunks_exact(PROCESS_FRAME_SAMPLES) {
            let output = match self.aec.process_capture(input) {
                Ok(output) => output,
                Err(error) => {
                    self.degrade(error);
                    return self.capture_passthrough(frame);
                }
            };
            match self.vad.is_speech(CAPTURE_RATE, &output) {
                Ok(frame_voice) => voice |= frame_voice,
                Err(error) => {
                    self.degrade(error);
                    return self.capture_passthrough(frame);
                }
            }
            processed.extend_from_slice(&output);
        }
        ProcessedCaptureFrame {
            pcm: processed,
            near_end_voice: Some(voice),
            protected_interruption_available: self.protected_interruption_available(),
            processor_state: self.state,
            reverse_generation: self.reverse_generation,
            error: self.error.clone(),
        }
    }

    /// push_playback_reference（送入播放参考）：先入 WASAPI 队列后调用，拒绝伪造未受理参考。
    pub fn push_playback_reference(
        &mut self,
        sample_rate: u32,
        samples: &[i16],
    ) -> Result<u32, String> {
        if self.state != "ready" {
            return Err(self
                .error
                .clone()
                .unwrap_or_else(|| "原生音频前处理不可用".into()));
        }
        if !matches!(sample_rate, 16_000 | 24_000 | 32_000 | 48_000) || samples.is_empty() {
            return Err("AEC 反向参考必须是非空的 16/24/32/48 kHz 单声道 PCM".into());
        }
        if self.reverse_rate != sample_rate {
            self.reclock_reverse(sample_rate)?;
        }
        let downsampled = self.reverse_resampler.push(samples)?;
        self.reverse_remainder.extend(downsampled);
        let mut processed_frames = 0_u32;
        while self.reverse_remainder.len() >= PROCESS_FRAME_SAMPLES {
            let frame: Vec<i16> = self
                .reverse_remainder
                .drain(..PROCESS_FRAME_SAMPLES)
                .collect();
            if let Err(error) = self.aec.process_reverse(&frame) {
                self.degrade(error.clone());
                return Err(error);
            }
            processed_frames = processed_frames.saturating_add(1);
            self.last_reverse_at = Some(Instant::now());
        }
        Ok(processed_frames)
    }

    /// update_playback_delay（更新播放参考延迟）：缓冲超限时关闭打断保护资格。
    pub fn update_playback_delay(&mut self, buffered_ms: u64) -> Result<(), String> {
        if self.state != "ready" {
            return Err(self
                .error
                .clone()
                .unwrap_or_else(|| "原生音频前处理不可用".into()));
        }
        self.protected_delay_valid = buffered_ms <= MAX_RENDER_DELAY_MS as u64;
        let delay_ms = buffered_ms.min(MAX_RENDER_DELAY_MS as u64) as u32;
        self.aec.set_stream_delay_ms(delay_ms)?;
        self.playback_delay_ms = Some(delay_ms);
        Ok(())
    }

    /// protected_interruption_available（检查保护状态）：健康、近期参考和有效延迟估计缺一不可。
    pub fn protected_interruption_available(&self) -> bool {
        self.state == "ready"
            && self.protected_delay_valid
            && self.playback_delay_ms.is_some()
            && self
                .last_reverse_at
                .is_some_and(|last| last.elapsed() <= MAX_REFERENCE_AGE)
    }

    /// status（查询状态）：返回状态、错误、参考代次和保护资格。
    pub fn status(&self) -> serde_json::Value {
        serde_json::json!({
            "state": self.state,
            "error": self.error.as_deref(),
            "reverse_generation": self.reverse_generation,
            "reverse_sample_rate": self.reverse_rate,
            "playback_delay_ms": self.playback_delay_ms,
            "protected_interruption_available": self.protected_interruption_available(),
            "fields": {
                "state": "AEC 与近端 VAD 前处理状态",
                "error": "前处理故障原因；健康时为空",
                "reverse_generation": "播放参考格式代次",
                "reverse_sample_rate": "当前反向参考源采样率",
                "playback_delay_ms": "受限到 500 毫秒的播放参考延迟估计",
                "protected_interruption_available": "是否具备近期且延迟范围内的 AEC 参考"
            }
        })
    }

    /// reset_stream（重置流）：采集缺口或播放端点重启时丢弃旧对齐状态。
    pub fn reset_stream(&mut self) -> Result<(), String> {
        self.aec.reset()?;
        self.vad = WebRtcVad::new(self.vad_mode)?;
        self.reverse_resampler = ReferenceResampler::new(self.reverse_rate);
        self.reverse_remainder.clear();
        self.last_reverse_at = None;
        self.playback_delay_ms = None;
        self.protected_delay_valid = true;
        self.reverse_generation = self.reverse_generation.saturating_add(1);
        self.state = "ready";
        self.error = None;
        Ok(())
    }

    fn reclock_reverse(&mut self, sample_rate: u32) -> Result<(), String> {
        // 所有播放参考在前处理边界重采样为 16 kHz，和当前 Python 前处理契约一致。
        self.aec.set_formats(CAPTURE_RATE, CAPTURE_RATE)?;
        self.reverse_rate = sample_rate;
        self.reverse_resampler = ReferenceResampler::new(sample_rate);
        self.reverse_remainder.clear();
        self.last_reverse_at = None;
        self.playback_delay_ms = None;
        self.protected_delay_valid = true;
        self.reverse_generation = self.reverse_generation.saturating_add(1);
        Ok(())
    }

    fn capture_passthrough(&self, frame: &CaptureFrame) -> ProcessedCaptureFrame {
        ProcessedCaptureFrame {
            pcm: frame.pcm.clone(),
            near_end_voice: None,
            protected_interruption_available: false,
            processor_state: self.state,
            reverse_generation: self.reverse_generation,
            error: self.error.clone(),
        }
    }

    fn degrade(&mut self, error: String) {
        self.state = "degraded";
        self.error = Some(error);
        self.last_reverse_at = None;
        self.playback_delay_ms = None;
    }
}

struct ReferenceResampler {
    source_rate: u32,          // source_rate（输入参考采样率）。
    alpha: f32,                // alpha（抗混叠低通的一阶滤波系数）。
    filter_state: Option<f32>, // filter_state（跨块低通历史）。
    previous: Option<f32>,     // previous（跨块插值前一采样）。
    next_output_position: f64, // next_output_position（下一个 16 kHz 源采样坐标）。
    total_input_samples: u64,  // total_input_samples（连续源采样位置）。
}

impl ReferenceResampler {
    fn new(source_rate: u32) -> Self {
        let alpha = if source_rate <= CAPTURE_RATE {
            1.0
        } else {
            let cutoff_hz = CAPTURE_RATE as f32 * 0.45;
            1.0 - (-2.0 * std::f32::consts::PI * cutoff_hz / source_rate as f32).exp()
        };
        Self {
            source_rate,
            alpha,
            filter_state: None,
            previous: None,
            next_output_position: 0.0,
            total_input_samples: 0,
        }
    }

    fn push(&mut self, samples: &[i16]) -> Result<Vec<i16>, String> {
        if samples.is_empty() {
            return Ok(Vec::new());
        }
        let step = self.source_rate as f64 / CAPTURE_RATE as f64;
        let estimated = (samples.len() as f64 / step).ceil() as usize + 1;
        let mut output = Vec::with_capacity(estimated);
        for sample in samples {
            let input = *sample as f32 / 32768.0;
            let filtered = match self.filter_state {
                Some(previous) => previous + self.alpha * (input - previous),
                None => input,
            };
            self.filter_state = Some(filtered);
            let absolute = self.total_input_samples as f64;
            if let Some(previous) = self.previous {
                let lower = absolute - 1.0;
                while self.next_output_position <= absolute + 1e-9 {
                    let fraction = (self.next_output_position - lower).clamp(0.0, 1.0);
                    let value = previous + (filtered - previous) * fraction as f32;
                    output.push(float_to_i16(value));
                    self.next_output_position += step;
                }
            } else {
                output.push(float_to_i16(filtered));
                self.next_output_position = absolute + step;
            }
            self.previous = Some(filtered);
            self.total_input_samples = self.total_input_samples.saturating_add(1);
        }
        Ok(output)
    }
}

fn float_to_i16(value: f32) -> i16 {
    (value.clamp(-1.0, 1.0) * 32767.0).round() as i16
}

#[cfg(test)]
mod tests {
    use super::*;

    fn capture_frame(discontinuity_before: bool) -> CaptureFrame {
        CaptureFrame {
            sequence: 0,
            start_sample: 0,
            device_time_us: 0,
            discontinuity_before,
            pcm: vec![0; CAPTURE_FRAME_SAMPLES],
        }
    }

    #[test]
    fn processes_twenty_millisecond_capture_as_two_ten_millisecond_frames() {
        let mut processor = NativeAudioProcessor::new(1).unwrap();
        let result = processor.process_capture(&capture_frame(false));
        assert_eq!(result.pcm.len(), CAPTURE_FRAME_SAMPLES);
        assert_eq!(result.near_end_voice, Some(false));
        assert!(!result.protected_interruption_available);
        assert_eq!(result.processor_state, "ready");
    }

    #[test]
    fn resamples_playback_reference_across_chunks_and_requires_delay_evidence() {
        let mut processor = NativeAudioProcessor::new(1).unwrap();
        let reference = vec![0_i16; 480];
        assert_eq!(
            processor
                .push_playback_reference(48_000, &reference)
                .unwrap(),
            1
        );
        assert!(!processor.protected_interruption_available());
        processor.update_playback_delay(40).unwrap();
        assert!(processor.protected_interruption_available());
        assert_eq!(
            processor
                .push_playback_reference(48_000, &reference)
                .unwrap(),
            1
        );
    }

    #[test]
    fn rejects_out_of_range_playback_delay_as_protected_reference() {
        let mut processor = NativeAudioProcessor::new(1).unwrap();
        processor
            .push_playback_reference(32_000, &vec![0_i16; 320])
            .unwrap();
        processor.update_playback_delay(900).unwrap();
        assert!(!processor.protected_interruption_available());
        assert_eq!(processor.status()["playback_delay_ms"], 500);
    }

    #[test]
    fn capture_discontinuity_discards_old_reverse_alignment() {
        let mut processor = NativeAudioProcessor::new(1).unwrap();
        processor
            .push_playback_reference(24_000, &vec![0_i16; 240])
            .unwrap();
        processor.update_playback_delay(20).unwrap();
        assert!(processor.protected_interruption_available());
        let before = processor.reverse_generation;
        let result = processor.process_capture(&capture_frame(true));
        assert!(!result.protected_interruption_available);
        assert!(result.reverse_generation > before);
    }

    #[test]
    fn reference_resampler_preserves_sample_count_across_chunk_boundaries() {
        let mut resampler = ReferenceResampler::new(48_000);
        let first = resampler.push(&vec![100_i16; 480]).unwrap();
        let second = resampler.push(&vec![100_i16; 480]).unwrap();
        assert_eq!(first.len() + second.len(), 320);
        assert_eq!(first.len(), 160);
        assert_eq!(second.len(), 160);
    }
}
