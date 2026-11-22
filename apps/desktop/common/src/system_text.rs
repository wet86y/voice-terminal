//! SystemText（系统文本）：将本机控制台代码页输出增量转换为 UTF-8（统一字符编码）。

pub(crate) fn console_code_page() -> u32 {
    #[cfg(windows)]
    {
        unsafe { windows_sys::Win32::Globalization::GetOEMCP() }
    }
    #[cfg(not(windows))]
    {
        65001
    }
}

fn decode(bytes: &[u8], code_page: u32) -> String {
    if bytes.is_empty() {
        return String::new();
    }
    if code_page == 65001 {
        return String::from_utf8_lossy(bytes).into_owned();
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::Globalization::MultiByteToWideChar;
        let length = i32::try_from(bytes.len()).unwrap_or(0);
        let count = unsafe {
            MultiByteToWideChar(
                code_page,
                0,
                bytes.as_ptr(),
                length,
                std::ptr::null_mut(),
                0,
            )
        };
        if count > 0 {
            let mut wide = vec![0; count as usize];
            let read = unsafe {
                MultiByteToWideChar(
                    code_page,
                    0,
                    bytes.as_ptr(),
                    length,
                    wide.as_mut_ptr(),
                    count,
                )
            };
            if read > 0 {
                return String::from_utf16_lossy(&wide[..read as usize]);
            }
        }
    }
    String::from_utf8_lossy(bytes).into_owned()
}

pub(crate) struct Decoder {
    code_page: u32,   // code_page（输出来源的系统代码页）。
    pending: Vec<u8>, // pending（尚未收齐的多字节字符）。
}

impl Decoder {
    pub(crate) fn new(code_page: u32) -> Self {
        Self {
            code_page,
            pending: Vec::new(),
        }
    }

    pub(crate) fn push(&mut self, bytes: &[u8], finish: bool) -> String {
        self.pending.extend_from_slice(bytes);
        let mut length = self.pending.len();
        if !finish {
            if self.code_page == 65001 {
                // 只保留最后一个未完成字符；完整但非法的序列仍明确替换。
                let mut offset = 0;
                while let Err(error) = std::str::from_utf8(&self.pending[offset..]) {
                    offset += error.valid_up_to();
                    match error.error_len() {
                        Some(invalid) => offset += invalid,
                        None => {
                            length = offset;
                            break;
                        }
                    }
                }
            } else {
                #[cfg(windows)]
                {
                    let mut offset = 0;
                    while offset < length {
                        let lead = unsafe {
                            windows_sys::Win32::Globalization::IsDBCSLeadByteEx(
                                self.code_page,
                                self.pending[offset],
                            )
                        } != 0;
                        if lead && offset + 1 == length {
                            length = offset;
                            break;
                        }
                        offset += if lead { 2 } else { 1 };
                    }
                }
            }
        }
        let text = decode(&self.pending[..length], self.code_page);
        self.pending.drain(..length);
        text
    }
}

// diagnostic（远端诊断）：优先接受完整 UTF-8；旧解释器回退到本机代码页时明确标注推断。
pub(crate) fn diagnostic(bytes: &[u8]) -> String {
    match std::str::from_utf8(bytes) {
        Ok(text) => text.to_owned(),
        // SSH（安全远程连接）诊断有字节上限；只在末尾截断时保留已确认的 UTF-8 前缀。
        Err(error) if error.error_len().is_none() && error.valid_up_to() > 0 => {
            String::from_utf8_lossy(bytes).into_owned()
        }
        Err(_) => format!(
            "{}（远端未声明编码，按本机系统代码页解码）",
            decode(bytes, console_code_page())
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn truncated_utf8_diagnostic_does_not_reinterpret_the_valid_prefix() {
        let bytes = "中文诊断🙂".as_bytes();
        assert_eq!(diagnostic(&bytes[..bytes.len() - 1]), "中文诊断�");
    }

    #[test]
    fn utf8_characters_survive_every_stream_boundary() {
        let bytes = "中文🙂输出".as_bytes();
        for split in 0..=bytes.len() {
            let mut decoder = Decoder::new(65001);
            assert_eq!(
                decoder.push(&bytes[..split], false) + &decoder.push(&bytes[split..], true),
                "中文🙂输出"
            );
        }
    }

    #[cfg(windows)]
    #[test]
    fn chinese_console_bytes_and_diagnostics_remain_readable() {
        let bytes = [0xd6, 0xd0, 0xce, 0xc4]; // bytes（代码页 936 中的“中文”）。
        for split in 0..=bytes.len() {
            let mut decoder = Decoder::new(936);
            assert_eq!(
                decoder.push(&bytes[..split], false) + &decoder.push(&bytes[split..], true),
                "中文"
            );
        }
        assert_eq!(diagnostic("中文诊断".as_bytes()), "中文诊断");
        assert!(diagnostic(&[0xff]).contains("远端未声明编码"));
    }
}
