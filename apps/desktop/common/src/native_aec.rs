//! Native WebRTC AEC（原生 WebRTC 回声消除）：固定版本 C++ 库的受限 C ABI。
use std::ffi::c_void;

#[link(name = "voice_native_aec", kind = "static")]
unsafe extern "C" {
    fn voice_native_aec_create(enable_aec: i32, enable_ns: i32, enable_agc: i32) -> *mut c_void;
    fn voice_native_aec_destroy(handle: *mut c_void);
    fn voice_native_aec_set_formats(
        handle: *mut c_void,
        capture_rate: i32,
        render_rate: i32,
    ) -> i32;
    fn voice_native_aec_set_delay_ms(handle: *mut c_void, delay_ms: i32) -> i32;
    fn voice_native_aec_reset(handle: *mut c_void) -> i32;
    fn voice_native_aec_process_capture(
        handle: *mut c_void,
        input: *const i16,
        sample_count: usize,
        output: *mut i16,
    ) -> i32;
    fn voice_native_aec_process_reverse(
        handle: *mut c_void,
        input: *const i16,
        sample_count: usize,
        output: *mut i16,
    ) -> i32;
}

/// NativeAecProcessor（原生前处理器）：同一实例由单一受管锁串行调用。
pub struct NativeAecProcessor {
    handle: *mut c_void,       // handle（C ABI AEC 实例句柄）。
    capture_rate: Option<u32>, // capture_rate（采集端采样率）。
    render_rate: Option<u32>,  // render_rate（参考播放采样率）。
}

impl NativeAecProcessor {
    /// new（创建处理器）：对应现用组件的 AEC、NS（降噪）及可选 AGC（自动增益）。
    pub fn new(enable_aec: bool, enable_ns: bool, enable_agc: bool) -> Result<Self, String> {
        let handle = unsafe {
            voice_native_aec_create(
                i32::from(enable_aec),
                i32::from(enable_ns),
                i32::from(enable_agc),
            )
        };
        if handle.is_null() {
            return Err("无法创建原生 WebRTC AEC 实例".into());
        }
        Ok(Self {
            handle,
            capture_rate: None,
            render_rate: None,
        })
    }

    /// set_formats（设置流格式）：AEC 输入为 16 kHz，反向参考可为支持的合成采样率。
    pub fn set_formats(&mut self, capture_rate: u32, render_rate: u32) -> Result<(), String> {
        if capture_rate != 16_000 || !matches!(render_rate, 16_000 | 24_000 | 32_000 | 48_000) {
            return Err("AEC 只接受 16 kHz 采集和 16/24/32/48 kHz 播放参考".into());
        }
        if unsafe {
            voice_native_aec_set_formats(self.handle, capture_rate as i32, render_rate as i32)
        } != 0
        {
            return Err("WebRTC AEC 拒绝采样格式".into());
        }
        self.capture_rate = Some(capture_rate);
        self.render_rate = Some(render_rate);
        Ok(())
    }

    /// set_stream_delay_ms（设置播放参考延迟）：AEC 使用已测得的反向流延迟。
    pub fn set_stream_delay_ms(&mut self, delay_ms: u32) -> Result<(), String> {
        if delay_ms > 500 {
            return Err("AEC 参考延迟必须不超过 500 毫秒".into());
        }
        if unsafe { voice_native_aec_set_delay_ms(self.handle, delay_ms as i32) } != 0 {
            return Err("WebRTC AEC 无法更新播放参考延迟".into());
        }
        Ok(())
    }

    /// process_reverse（处理播放参考）：每次输入一个完整的 10 ms 单声道 PCM 帧。
    pub fn process_reverse(&mut self, samples: &[i16]) -> Result<Vec<i16>, String> {
        let sample_rate = self
            .render_rate
            .ok_or_else(|| "AEC 尚未设置播放参考格式".to_owned())?;
        let expected = sample_rate as usize / 100;
        if samples.len() != expected {
            return Err("AEC 播放参考必须是完整 10 毫秒帧".into());
        }
        let mut output = vec![0_i16; samples.len()];
        let status = unsafe {
            voice_native_aec_process_reverse(
                self.handle,
                samples.as_ptr(),
                samples.len(),
                output.as_mut_ptr(),
            )
        };
        if status != 0 {
            return Err(format!("WebRTC AEC 反向参考处理失败（{status}）"));
        }
        Ok(output)
    }

    /// process_capture（处理麦克风）：每次输入一个完整的 10 ms 16 kHz 单声道 PCM 帧。
    pub fn process_capture(&mut self, samples: &[i16]) -> Result<Vec<i16>, String> {
        let capture_rate = self
            .capture_rate
            .ok_or_else(|| "AEC 尚未设置采集格式".to_owned())?;
        let expected = capture_rate as usize / 100;
        if samples.len() != expected {
            return Err("AEC 麦克风输入必须是完整 10 毫秒帧".into());
        }
        let mut output = vec![0_i16; samples.len()];
        let status = unsafe {
            voice_native_aec_process_capture(
                self.handle,
                samples.as_ptr(),
                samples.len(),
                output.as_mut_ptr(),
            )
        };
        if status != 0 {
            return Err(format!("WebRTC AEC 麦克风处理失败（{status}）"));
        }
        Ok(output)
    }

    /// reset（重置处理状态）：保留配置并清除流滤波器历史。
    pub fn reset(&mut self) -> Result<(), String> {
        if unsafe { voice_native_aec_reset(self.handle) } != 0 {
            return Err("WebRTC AEC 状态重置失败".into());
        }
        Ok(())
    }
}

impl Drop for NativeAecProcessor {
    fn drop(&mut self) {
        if !self.handle.is_null() {
            unsafe { voice_native_aec_destroy(self.handle) };
            self.handle = std::ptr::null_mut();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn processes_synthetic_capture_and_reverse_frames_without_python() {
        let mut aec = NativeAecProcessor::new(true, true, false).unwrap();
        aec.set_formats(16_000, 48_000).unwrap();
        aec.set_stream_delay_ms(30).unwrap();
        let reverse = vec![0_i16; 480];
        let capture = vec![0_i16; 160];
        assert_eq!(aec.process_reverse(&reverse).unwrap(), reverse);
        assert_eq!(aec.process_capture(&capture).unwrap(), capture);
        aec.reset().unwrap();
        assert_eq!(aec.process_capture(&capture).unwrap(), capture);
    }

    #[test]
    fn rejects_wrong_capture_rate_and_non_ten_millisecond_frames() {
        let mut aec = NativeAecProcessor::new(true, false, false).unwrap();
        assert!(aec.set_formats(24_000, 48_000).is_err());
        aec.set_formats(16_000, 32_000).unwrap();
        assert!(aec.process_capture(&vec![0_i16; 80]).is_err());
        assert!(aec.process_reverse(&vec![0_i16; 160]).is_err());
        assert!(aec.set_stream_delay_ms(501).is_err());
    }
}
