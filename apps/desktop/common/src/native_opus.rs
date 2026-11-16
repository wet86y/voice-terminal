//! Fixed libopus stream codec（固定版本 Opus 流式编解码）：每个实例只拥有一个流状态。
use std::{ffi::c_void, ptr::NonNull};

pub const FRAME_DURATION_MS: u32 = 20; // FRAME_DURATION_MS（每个 Opus 帧的时长，毫秒）。
pub const MAX_PACKET_BYTES: usize = 1275; // MAX_PACKET_BYTES（单个 raw Opus 包的字节上限）。
const DEFAULT_UPLINK_BITRATE: u32 = 48_000; // DEFAULT_UPLINK_BITRATE（上行默认码率，bit/s）。
const DEFAULT_DOWNLINK_BITRATE: u32 = 64_000; // DEFAULT_DOWNLINK_BITRATE（下行默认码率，bit/s）。

unsafe extern "C" {
    fn voice_opus_encoder_create(
        sample_rate: i32,
        bitrate: i32,
        error: *mut i32,
        lookahead_samples: *mut i32,
    ) -> *mut c_void;
    fn voice_opus_encode(
        state: *mut c_void,
        pcm: *const i16,
        frame_samples: i32,
        packet: *mut u8,
        packet_capacity: i32,
    ) -> i32;
    fn voice_opus_encoder_destroy(state: *mut c_void);
    fn voice_opus_decoder_create(sample_rate: i32, error: *mut i32) -> *mut c_void;
    fn voice_opus_decode(
        state: *mut c_void,
        packet: *const u8,
        packet_length: i32,
        pcm: *mut i16,
        frame_capacity: i32,
    ) -> i32;
    fn voice_opus_decoder_destroy(state: *mut c_void);
}

fn frame_samples(sample_rate: u32) -> Result<usize, String> {
    if !matches!(sample_rate, 16_000 | 48_000) {
        return Err(
            "Opus 编码率只接受 16 kHz 上行或 48 kHz 下行输入；32 kHz 合成音频须先重采样。".into(),
        );
    }
    Ok((sample_rate / 50) as usize)
}

fn opus_error(operation: &str, error: i32) -> String {
    format!("{operation}失败：libopus 错误码 {error}")
}

/// OpusEncoder（Opus 编码器）：持续保留同一流的编码状态与预延迟。
pub struct OpusEncoder {
    state: NonNull<c_void>,
    sample_rate: u32,
    frame_samples: usize,
    bitrate: u32,
    lookahead_samples: u32,
}

impl OpusEncoder {
    /// create（创建编码器）：默认上行 48 kbps、下行 64 kbps，DTX（不连续传输）关闭。
    pub fn new(sample_rate: u32, bitrate: Option<u32>) -> Result<Self, String> {
        let frame_samples = frame_samples(sample_rate)?;
        let default_bitrate = if sample_rate == 16_000 {
            DEFAULT_UPLINK_BITRATE
        } else {
            DEFAULT_DOWNLINK_BITRATE
        };
        let bitrate = bitrate.unwrap_or(default_bitrate);
        if !(6_000..=510_000).contains(&bitrate) {
            return Err("Opus 码率必须位于 6–510 kbps 的标准范围内。".into());
        }
        let mut error = 0_i32;
        let mut lookahead_samples = 0_i32;
        let state = unsafe {
            voice_opus_encoder_create(
                sample_rate as i32,
                bitrate as i32,
                &mut error,
                &mut lookahead_samples,
            )
        };
        let state = NonNull::new(state).ok_or_else(|| opus_error("创建 Opus 编码器", error))?;
        if lookahead_samples < 0 || lookahead_samples as u32 > sample_rate {
            unsafe { voice_opus_encoder_destroy(state.as_ptr()) };
            return Err("libopus 返回的编码预延迟采样数无效。".into());
        }
        Ok(Self {
            state,
            sample_rate,
            frame_samples,
            bitrate,
            lookahead_samples: lookahead_samples as u32,
        })
    }

    pub fn sample_rate(&self) -> u32 {
        self.sample_rate
    }

    pub fn frame_samples(&self) -> usize {
        self.frame_samples
    }

    pub fn bitrate(&self) -> u32 {
        self.bitrate
    }

    /// lookahead_samples（预延迟采样数）：由媒体首帧元数据承担结算映射。
    pub fn lookahead_samples(&self) -> u32 {
        self.lookahead_samples
    }

    /// encode（编码一帧）：输入必须是完整 20 ms 单声道 16 位 PCM。
    pub fn encode(&mut self, pcm: &[i16]) -> Result<Vec<u8>, String> {
        if pcm.len() != self.frame_samples {
            return Err(format!(
                "Opus 输入帧长度无效：期望 {} 个采样，实际 {} 个。",
                self.frame_samples,
                pcm.len()
            ));
        }
        let mut packet = vec![0_u8; MAX_PACKET_BYTES];
        let encoded = unsafe {
            voice_opus_encode(
                self.state.as_ptr(),
                pcm.as_ptr(),
                self.frame_samples as i32,
                packet.as_mut_ptr(),
                MAX_PACKET_BYTES as i32,
            )
        };
        if encoded <= 0 || encoded as usize > MAX_PACKET_BYTES {
            return Err(opus_error("编码 Opus 音频帧", encoded));
        }
        packet.truncate(encoded as usize);
        Ok(packet)
    }
}

impl Drop for OpusEncoder {
    fn drop(&mut self) {
        unsafe { voice_opus_encoder_destroy(self.state.as_ptr()) };
    }
}

/// OpusDecoder（Opus 解码器）：每个设备/媒体流单独实例化，不共享预测状态。
pub struct OpusDecoder {
    state: NonNull<c_void>,
    sample_rate: u32,
    frame_samples: usize,
}

// Safety: libopus decoder state has no creating-thread affinity. It is only
// accessed through &mut self, so moving the uniquely owned state between Tokio
// worker threads cannot introduce concurrent access.
unsafe impl Send for OpusDecoder {}

impl OpusDecoder {
    pub fn new(sample_rate: u32) -> Result<Self, String> {
        let frame_samples = frame_samples(sample_rate)?;
        let mut error = 0_i32;
        let state = unsafe { voice_opus_decoder_create(sample_rate as i32, &mut error) };
        let state = NonNull::new(state).ok_or_else(|| opus_error("创建 Opus 解码器", error))?;
        Ok(Self {
            state,
            sample_rate,
            frame_samples,
        })
    }

    pub fn sample_rate(&self) -> u32 {
        self.sample_rate
    }

    /// decode（解码一帧）：只接受完整 20 ms raw Opus 包，不接受 Ogg 封装。
    pub fn decode(&mut self, packet: &[u8]) -> Result<Vec<i16>, String> {
        if packet.is_empty() || packet.len() > MAX_PACKET_BYTES {
            return Err("Opus 包长度无效。".into());
        }
        let mut pcm = vec![0_i16; self.frame_samples];
        let decoded = unsafe {
            voice_opus_decode(
                self.state.as_ptr(),
                packet.as_ptr(),
                packet.len() as i32,
                pcm.as_mut_ptr(),
                self.frame_samples as i32,
            )
        };
        if decoded < 0 {
            return Err(opus_error("解码 Opus 音频帧", decoded));
        }
        if decoded as usize != self.frame_samples {
            return Err(format!(
                "Opus 解码时长不符：期望 {} 个采样，收到 {} 个。",
                self.frame_samples, decoded
            ));
        }
        Ok(pcm)
    }
}

impl Drop for OpusDecoder {
    fn drop(&mut self) {
        unsafe { voice_opus_decoder_destroy(self.state.as_ptr()) };
    }
}

#[cfg(test)]
mod tests {
    use super::{OpusDecoder, OpusEncoder};

    #[test]
    fn round_trips_eight_stateful_twenty_millisecond_frames_at_supported_rates() {
        for sample_rate in [16_000_u32, 48_000] {
            let mut encoder = OpusEncoder::new(sample_rate, None).unwrap();
            let mut decoder = OpusDecoder::new(sample_rate).unwrap();
            assert_eq!(encoder.sample_rate(), sample_rate);
            assert_eq!(encoder.frame_samples(), (sample_rate / 50) as usize);
            assert!(encoder.lookahead_samples() > 0);
            let mut nonzero = 0_usize;
            for frame in 0..8 {
                let pcm: Vec<i16> = (0..encoder.frame_samples())
                    .map(|index| {
                        let position = frame * encoder.frame_samples() + index;
                        (8_000.0
                            * (2.0 * std::f64::consts::PI * 440.0 * position as f64
                                / f64::from(sample_rate))
                            .sin()) as i16
                    })
                    .collect();
                let packet = encoder.encode(&pcm).unwrap();
                assert!(!packet.is_empty());
                assert!(packet.len() <= super::MAX_PACKET_BYTES);
                let decoded = decoder.decode(&packet).unwrap();
                assert_eq!(decoded.len(), pcm.len());
                nonzero += decoded.iter().filter(|sample| **sample != 0).count();
            }
            assert!(nonzero > encoder.frame_samples());
        }
    }

    #[test]
    fn rejects_unsupported_rates_and_non_full_frames() {
        assert!(OpusEncoder::new(32_000, None).is_err());
        assert!(OpusDecoder::new(24_000).is_err());
        let mut encoder = OpusEncoder::new(16_000, None).unwrap();
        assert!(encoder.encode(&vec![0; 319]).is_err());
    }

    #[test]
    fn silence_remains_a_packet_with_dtx_disabled() {
        let mut encoder = OpusEncoder::new(16_000, None).unwrap();
        assert!(!encoder.encode(&vec![0; 320]).unwrap().is_empty());
    }
}
