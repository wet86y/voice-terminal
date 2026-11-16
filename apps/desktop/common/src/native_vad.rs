//! Native WebRTC VAD（原生 WebRTC 语音活动检测）：直接链接固定 C 源码，不加载 Python。
use std::{ffi::c_void, marker::PhantomData, ptr::NonNull, rc::Rc};

#[allow(non_snake_case)]
unsafe extern "C" {
    fn WebRtcVad_Create(handle: *mut *mut c_void) -> i32;
    fn WebRtcVad_Free(handle: *mut c_void);
    fn WebRtcVad_Init(handle: *mut c_void) -> i32;
    fn WebRtcVad_set_mode(handle: *mut c_void, mode: i32) -> i32;
    fn WebRtcVad_Process(
        handle: *mut c_void,
        sample_rate: i32,
        samples: *const i16,
        sample_count: i32,
    ) -> i32;
    fn WebRtcVad_ValidRateAndFrameLength(sample_rate: i32, sample_count: i32) -> i32;
}

/// WebRtcVad（WebRTC 语音检测器）：句柄线程内顺序使用，释放由 Drop（作用域清理）负责。
pub struct WebRtcVad {
    handle: NonNull<c_void>,        // handle（WebRTC 原生 VAD 实例句柄）。
    _not_send: PhantomData<Rc<()>>, // _not_send（禁止把状态实例跨线程移动）。
}

impl WebRtcVad {
    /// new（创建实例）：mode（敏感度）范围为 0 至 3，越高越严格。
    pub fn new(mode: u8) -> Result<Self, String> {
        if mode > 3 {
            return Err("WebRTC VAD 灵敏度必须为 0 至 3".into());
        }
        let mut raw = std::ptr::null_mut();
        let created = unsafe { WebRtcVad_Create(&mut raw) };
        let handle = match NonNull::new(raw) {
            Some(handle) if created == 0 => handle,
            _ => {
                if !raw.is_null() {
                    unsafe { WebRtcVad_Free(raw) };
                }
                return Err("无法创建 WebRTC VAD 实例".into());
            }
        };
        let mut vad = Self {
            handle,
            _not_send: PhantomData,
        };
        if unsafe { WebRtcVad_Init(vad.handle.as_ptr()) } != 0 {
            return Err("无法初始化 WebRTC VAD 实例".into());
        }
        vad.set_mode(mode)?;
        Ok(vad)
    }

    /// set_mode（更改灵敏度）：0 至 3；更高模式减少误报并增加漏报。
    pub fn set_mode(&mut self, mode: u8) -> Result<(), String> {
        if mode > 3 {
            return Err("WebRTC VAD 灵敏度必须为 0 至 3".into());
        }
        if unsafe { WebRtcVad_set_mode(self.handle.as_ptr(), mode as i32) } != 0 {
            return Err("WebRTC VAD 拒绝设置灵敏度".into());
        }
        Ok(())
    }

    /// is_speech（检测当前帧）：只接受 WebRTC 规定的 8/16/32/48 kHz、10/20/30 ms 单声道 PCM。
    pub fn is_speech(&mut self, sample_rate: u32, samples: &[i16]) -> Result<bool, String> {
        let rate = i32::try_from(sample_rate).map_err(|_| "WebRTC VAD 采样率无效")?;
        let count = i32::try_from(samples.len()).map_err(|_| "WebRTC VAD 帧长度无效")?;
        if unsafe { WebRtcVad_ValidRateAndFrameLength(rate, count) } != 0 {
            return Err("WebRTC VAD 不支持该采样率或帧时长".into());
        }
        match unsafe { WebRtcVad_Process(self.handle.as_ptr(), rate, samples.as_ptr(), count) } {
            0 => Ok(false),
            1 => Ok(true),
            _ => Err("WebRTC VAD 帧处理失败".into()),
        }
    }
}

impl Drop for WebRtcVad {
    fn drop(&mut self) {
        unsafe { WebRtcVad_Free(self.handle.as_ptr()) };
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_standard_frame_rates_and_lengths_and_classifies_silence() {
        let mut vad = WebRtcVad::new(1).unwrap();
        for sample_rate in [8_000_u32, 16_000, 32_000, 48_000] {
            for duration_ms in [10_u32, 20, 30] {
                let samples = vec![0_i16; sample_rate as usize * duration_ms as usize / 1_000];
                assert!(!vad.is_speech(sample_rate, &samples).unwrap());
            }
        }
    }

    #[test]
    fn rejects_invalid_mode_and_nonstandard_frame_length() {
        assert!(WebRtcVad::new(4).is_err());
        let mut vad = WebRtcVad::new(0).unwrap();
        let invalid = vec![0_i16; 161];
        assert!(vad.is_speech(16_000, &invalid).is_err());
        assert!(vad.set_mode(5).is_err());
    }
}
