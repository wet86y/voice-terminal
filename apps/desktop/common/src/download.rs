//! Native download（原生下载）：静态 C ABI（C 调用接口）与固定摘要。
use std::{ffi::{CString, c_void}, path::Path, sync::{Arc, atomic::{AtomicBool, Ordering}}};
use serde::{Serialize, Deserialize};
use base64::Engine;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct DownloadFile {
    pub url: String, // url（固定原始来源地址）。
    pub size: u64, // size（预期字节数）。
    #[serde(default)] pub sha256: String, // sha256（十六进制摘要）。
    #[serde(default)] pub sha512: String, // sha512（十六进制或 Base64 编码摘要）。
    #[serde(default)] pub path: String, // path（安装相对文件名）。
}

type Callback = unsafe extern "C" fn(*mut c_void, u64, u64) -> i32;
#[cfg(windows)]
unsafe extern "C" {
    fn vt_download(url: *const i8, target: *const u16, size: u64, expected: *const i8,
        sha512: i32, probe_limit: u64, callback: Callback, context: *mut c_void,
        error: *mut i8, error_size: usize) -> i32;
}
struct Progress<'a> {
    cancel: Arc<AtomicBool>, // cancel（取消信号）。
    callback: &'a mut dyn FnMut(u64, u64), // callback（字节进度回调）。
}
unsafe extern "C" fn progress(context: *mut c_void, current: u64, total: u64) -> i32 {
    let state = unsafe { &mut *(context as *mut Progress<'_>) };
    (state.callback)(current, total);
    i32::from(state.cancel.load(Ordering::Acquire))
}

pub fn download(file: &DownloadFile, target: &Path, cancel: Arc<AtomicBool>,
    callback: &mut dyn FnMut(u64, u64), probe_limit: u64) -> Result<u64, String> {
    if !(file.url.starts_with("https://") || (cfg!(feature="qualification") && file.url.starts_with("http://127.0.0.1:"))) {
        return Err("资源来源必须为 HTTPS（加密传输），验收配置允许本机测试源。".into());
    }
    let (expected, algorithm) = if !file.sha512.is_empty() {
        let hash = if file.sha512.len() == 128 { file.sha512.to_lowercase() } else {
            base64::engine::general_purpose::STANDARD.decode(&file.sha512)
                .map_err(|_| "SHA-512（文件摘要）编码无效")?.iter().map(|v| format!("{v:02x}")).collect()
        }; (hash, 1)
    } else { (file.sha256.to_lowercase(), 0) };
    if probe_limit == 0 && (expected.len() != if algorithm == 1 {128} else {64} || !expected.bytes().all(|v| v.is_ascii_hexdigit())) {
        return Err("资源缺少可信文件摘要。".into());
    }
    let url = CString::new(file.url.as_str()).map_err(|_| "来源地址无效")?;
    let hash = CString::new(expected).map_err(|_| "摘要无效")?;
    #[cfg(windows)] {
        use std::os::windows::ffi::OsStrExt;
        let destination: Vec<_> = target.as_os_str().encode_wide().chain(Some(0)).collect();
        let mut message = [0i8; 1024];
        let mut state = Progress {cancel, callback};
        let result = unsafe { vt_download(url.as_ptr(), destination.as_ptr(), file.size, hash.as_ptr(), algorithm,
            probe_limit.min(65536), progress, &mut state as *mut _ as *mut c_void, message.as_mut_ptr(), message.len()) };
        if result < 0 {
            let bytes: Vec<_> = message.iter().take_while(|v| **v != 0).map(|v| *v as u8).collect();
            return Err(String::from_utf8_lossy(&bytes).to_string());
        }
        Ok(result as u64)
    }
    #[cfg(not(windows))] { let _=(target,cancel,callback,url,hash); Err("原生下载仅支持 Windows（操作系统）。".into()) }
}

#[cfg(all(test, windows, feature = "qualification"))]
mod tests {
    use super::*;
    use std::{fs, io::{Read, Write}, net::TcpListener};
    use sha2::{Digest, Sha256};

    #[test]
    fn native_download_accepts_success_with_exact_range_and_rejects_wrong_range() {
        for (status, valid) in [(200, true), (206, true), (200, false)] {
            let body = b"fixed model file";
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let address = listener.local_addr().unwrap();
            let server = std::thread::spawn(move || {
                let (mut socket, _) = listener.accept().unwrap();
                let mut request = [0; 4096];socket.read(&mut request).unwrap();
                let last = if valid { body.len() - 1 } else { body.len() - 2 };
                write!(socket, "HTTP/1.1 {status} OK\r\nContent-Length: {}\r\nContent-Range: bytes 0-{last}/{}\r\nConnection: close\r\n\r\n", body.len(), body.len()).unwrap();
                socket.write_all(body).unwrap();
            });
            let root = std::env::temp_dir().join(format!("voice-download-{}", crate::workspace::new_id()));
            fs::create_dir_all(&root).unwrap();let target = root.join("model.bin");
            let file = DownloadFile { url: format!("http://{address}/model.bin"), size: body.len() as u64,
                sha256: format!("{:x}", Sha256::digest(body)), sha512: String::new(), path: "model.bin".into() };
            let mut samples = vec![];
            let result = download(&file, &target, Arc::new(AtomicBool::new(false)), &mut |done, total| samples.push((done, total)), 0);
            assert_eq!(result.is_ok(), valid);
            if valid { assert_eq!(fs::read(&target).unwrap(), body);assert_eq!(samples.last(), Some(&(file.size, file.size))); }
            server.join().unwrap();
            // 失败返回后也必须释放分片文件句柄，否则同一进程无法清理或重试下载。
            for path in [target.clone(), target.with_extension("bin.part"), target.with_extension("bin.part.identity")] {
                if let Err(error) = fs::remove_file(&path) {
                    assert_eq!(error.kind(), std::io::ErrorKind::NotFound, "下载测试文件未释放：{}：{error}", path.display());
                }
            }
            fs::remove_dir(&root).expect("下载测试的暂存目录未释放");
        }
    }
}
