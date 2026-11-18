//! Default audio endpoint probe（默认音频端点探测）：只读取 WASAPI 格式，不启动录放音。
use serde::Serialize;

/// AudioEndpointInfo（音频端点信息）：读取系统默认端点的设备格式和时钟周期。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AudioEndpointInfo {
    pub flow: String,         // flow（端点方向）：capture（采集）或 render（播放）。
    pub endpoint_id: String,  // endpoint_id（Windows 音频端点标识）。
    pub sample_rate: u32,     // sample_rate（系统混音采样率，每秒采样数）。
    pub channels: u16,        // channels（系统混音声道数）。
    pub bits_per_sample: u16, // bits_per_sample（每采样位数）。
    pub format_tag: u16,      // format_tag（WAVEFORMATEX 格式编号）。
    pub block_align: u16,     // block_align（每音频帧字节数）。
    pub average_bytes_per_second: u32, // average_bytes_per_second（每秒平均字节数）。
    pub default_period_100ns: i64, // default_period_100ns（默认设备周期，100 纳秒单位）。
    pub minimum_period_100ns: i64, // minimum_period_100ns（最小设备周期，100 纳秒单位）。
}

fn endpoint_info(
    flow: &str,
    endpoint_id: String,
    sample_rate: u32,
    channels: u16,
    bits_per_sample: u16,
    format_tag: u16,
    block_align: u16,
    average_bytes_per_second: u32,
    default_period_100ns: i64,
    minimum_period_100ns: i64,
) -> Result<AudioEndpointInfo, String> {
    if !matches!(flow, "capture" | "render")
        || endpoint_id.is_empty()
        || sample_rate == 0
        || channels == 0
        || block_align == 0
        || average_bytes_per_second == 0
        || default_period_100ns <= 0
        || minimum_period_100ns <= 0
        || minimum_period_100ns > default_period_100ns
    {
        return Err("默认音频端点格式或设备周期无效".into());
    }
    Ok(AudioEndpointInfo {
        flow: flow.into(),
        endpoint_id,
        sample_rate,
        channels,
        bits_per_sample,
        format_tag,
        block_align,
        average_bytes_per_second,
        default_period_100ns,
        minimum_period_100ns,
    })
}

#[cfg(windows)]
pub fn probe_default_audio_endpoints() -> Result<Vec<AudioEndpointInfo>, String> {
    use windows::Win32::Media::Audio::{
        eCapture, eConsole, eRender, IMMDevice, IMMDeviceEnumerator, MMDeviceEnumerator,
    };
    use windows::Win32::System::Com::{
        CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_ALL, COINIT_MULTITHREADED,
    };

    struct ComApartment;
    impl Drop for ComApartment {
        fn drop(&mut self) {
            unsafe { CoUninitialize() };
        }
    }

    unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) }
        .ok()
        .map_err(|error| format!("WASAPI COM 初始化失败：{error}"))?;
    let _apartment = ComApartment;
    let enumerator: IMMDeviceEnumerator =
        unsafe { CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL) }
            .map_err(|error| format!("WASAPI 设备枚举器不可用：{error}"))?;

    fn inspect(device: IMMDevice, flow: &str) -> Result<AudioEndpointInfo, String> {
        use std::ffi::c_void;
        use windows::Win32::Media::Audio::IAudioClient;
        use windows::Win32::System::Com::{CoTaskMemFree, CLSCTX_ALL};

        let endpoint_id = endpoint_id_from_device(&device)?;

        let client: IAudioClient = unsafe { device.Activate(CLSCTX_ALL, None) }
            .map_err(|error| format!("打开默认音频端点格式失败：{error}"))?;
        let format_ptr = unsafe { client.GetMixFormat() }
            .map_err(|error| format!("读取默认音频混音格式失败：{error}"))?;
        if format_ptr.is_null() {
            return Err("WASAPI 返回空混音格式".into());
        }
        let format = unsafe { format_ptr.read_unaligned() };
        unsafe { CoTaskMemFree(Some(format_ptr.cast::<c_void>())) };

        let mut default_period = 0_i64;
        let mut minimum_period = 0_i64;
        unsafe { client.GetDevicePeriod(Some(&mut default_period), Some(&mut minimum_period)) }
            .map_err(|error| format!("读取默认音频设备周期失败：{error}"))?;
        endpoint_info(
            flow,
            endpoint_id,
            format.nSamplesPerSec,
            format.nChannels,
            format.wBitsPerSample,
            format.wFormatTag,
            format.nBlockAlign,
            format.nAvgBytesPerSec,
            default_period,
            minimum_period,
        )
    }

    let capture = unsafe { enumerator.GetDefaultAudioEndpoint(eCapture, eConsole) }
        .map_err(|error| format!("系统默认麦克风不可用：{error}"))?;
    let render = unsafe { enumerator.GetDefaultAudioEndpoint(eRender, eConsole) }
        .map_err(|error| format!("系统默认扬声器不可用：{error}"))?;
    Ok(vec![
        inspect(capture, "capture")?,
        inspect(render, "render")?,
    ])
}

#[cfg(windows)]
pub(crate) fn endpoint_id_from_device(
    device: &windows::Win32::Media::Audio::IMMDevice, // device（待读取编号的音频端点）。
) -> Result<String, String> {
    use std::ffi::c_void;
    use windows::Win32::System::Com::CoTaskMemFree;

    let id = unsafe { device.GetId() }.map_err(|error| format!("读取音频端点编号失败：{error}"))?;
    if id.is_null() {
        return Err("系统音频端点编号为空".into());
    }
    let endpoint_id =
        unsafe { id.to_string() }.map_err(|_| "系统音频端点编号不是有效 UTF-16".to_owned());
    unsafe { CoTaskMemFree(Some(id.as_ptr().cast::<c_void>())) };
    endpoint_id
}

#[cfg(windows)]
pub(crate) fn format_wasapi_error(
    context: &str,               // context（操作失败上下文）。
    error: windows::core::Error, // error（Windows 音频接口返回的 HRESULT 错误）。
) -> String {
    use windows::Win32::Media::Audio::AUDCLNT_E_DEVICE_INVALIDATED;
    if error.code() == AUDCLNT_E_DEVICE_INVALIDATED {
        format!("WASAPI_DEVICE_INVALIDATED:{context}: {error}")
    } else {
        format!("{context}: {error}")
    }
}

#[cfg(windows)]
pub(crate) fn is_wasapi_device_invalidated(message: &str) -> bool {
    // message（已格式化的 WASAPI 错误）。
    message.starts_with("WASAPI_DEVICE_INVALIDATED:")
}

#[cfg(not(windows))]
pub fn probe_default_audio_endpoints() -> Result<Vec<AudioEndpointInfo>, String> {
    Err("WASAPI 音频端点探测仅支持 Windows".into())
}

#[cfg(test)]
mod tests {
    #[test]
    fn accepts_valid_default_endpoint_format_and_rejects_invalid_periods() {
        let info = super::endpoint_info(
            "capture",
            "mic-id".into(),
            48_000,
            2,
            32,
            3,
            8,
            384_000,
            100_000,
            50_000,
        )
        .unwrap();
        assert_eq!(info.sample_rate, 48_000);
        assert_eq!(info.channels, 2);
        assert_eq!(info.minimum_period_100ns, 50_000);
        assert!(super::endpoint_info(
            "capture",
            "mic-id".into(),
            48_000,
            2,
            32,
            3,
            8,
            384_000,
            50_000,
            100_000
        )
        .is_err());
        assert!(super::endpoint_info(
            "unsupported",
            "mic-id".into(),
            48_000,
            2,
            32,
            3,
            8,
            384_000,
            100_000,
            50_000
        )
        .is_err());
    }
}
