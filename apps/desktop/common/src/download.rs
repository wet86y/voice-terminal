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
    use std::{os::windows::fs::OpenOptionsExt, time::Duration};

    /// download_with_temporary_lock（短暂占用下载夹具）：覆盖分片和已有缓存的共享冲突。
    fn download_with_temporary_lock(lock_target: bool) { // lock_target（占用最终文件；否则占用下载分片）。
        let body = b"verified dependency wheel";
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            let mut request = [0; 4096];socket.read(&mut request).unwrap();
            write!(socket, "HTTP/1.1 206 OK\r\nContent-Length: {}\r\nContent-Range: bytes 0-{}/{}\r\nConnection: close\r\n\r\n", body.len(), body.len() - 1, body.len()).unwrap();
            socket.write_all(body).unwrap();
        });
        let root = std::env::temp_dir().join(format!("voice-download-占用测试-{}", crate::workspace::new_id()));
        fs::create_dir_all(&root).unwrap();let target = root.join("依赖 文件.whl");
        fs::write(&target, b"old incomplete cache").unwrap();
        let part = target.with_extension("whl.part"); // part（下载暂存文件）。
        let checkpoint = target.with_extension("whl.part.identity"); // checkpoint（固定大小及摘要断点）。
        let file = DownloadFile { url: format!("http://{address}/dependency.whl"), size: body.len() as u64,
            sha256: format!("{:x}", Sha256::digest(body)), sha512: String::new(), path: "dependency.whl".into() };
        let mut release = None; // release（模拟其他进程短暂占用后释放句柄）。
        let result = download(&file, &target, Arc::new(AtomicBool::new(false)), &mut |done, _| {
            if done == file.size && release.is_none() {
                // share_mode（共享模式）允许读写，但禁止其他句柄删除或重命名。
                let held = fs::OpenOptions::new().read(true).share_mode(3).open(if lock_target { &target } else { &part }).unwrap();
                release = Some(std::thread::spawn(move || { std::thread::sleep(Duration::from_millis(350));drop(held); }));
            }
        }, 0);
        server.join().unwrap();release.unwrap().join().unwrap();
        let installed = fs::read(&target).ok(); // installed（最终缓存内容）。
        let clean = !part.exists() && !checkpoint.exists(); // clean（完成后断点已清理）。
        fs::remove_dir_all(&root).unwrap();
        assert!(result.is_ok(), "短暂文件占用不应中断依赖下载：{result:?}");
        assert_eq!(installed.as_deref(), Some(body.as_slice()));assert!(clean);
    }

    #[test]
    fn native_download_retries_temporary_file_locks_without_losing_verified_bytes() {
        // 不同下载同时遇到文件占用，仍须各自完成校验、替换与断点清理。
        std::thread::scope(|scope| {
            for lock_target in [false, true] {
                scope.spawn(move || download_with_temporary_lock(lock_target));
            }
        });
    }

    #[test]
    fn native_download_cancels_locked_promotion_and_resumes_without_network() {
        let body = b"completed dependency wheel";
        let root = std::env::temp_dir().join(format!("voice-download-断点测试-{}", crate::workspace::new_id()));
        fs::create_dir_all(&root).unwrap();let target = root.join("dependency.whl");
        let part = target.with_extension("whl.part");let checkpoint = target.with_extension("whl.part.identity");
        let file = DownloadFile { url: "http://127.0.0.1:1/dependency.whl".into(), size: body.len() as u64,
            sha256: format!("{:x}", Sha256::digest(body)), sha512: String::new(), path: "dependency.whl".into() };
        fs::write(&part, body).unwrap();fs::write(&checkpoint, format!("{}:sha256:{}", file.size, file.sha256)).unwrap();
        fs::write(&target, b"keep previous cache").unwrap();
        let held = fs::OpenOptions::new().read(true).share_mode(3).open(&part).unwrap();
        let cancel = Arc::new(AtomicBool::new(false));let mut samples = 0;
        let result = download(&file, &target, cancel.clone(), &mut |_, _| {
            samples += 1;if samples >= 3 { cancel.store(true, Ordering::Release); }
        }, 0);
        let preserved = fs::read(&part).unwrap() == body && checkpoint.exists() && fs::read(&target).ok().as_deref() == Some(b"keep previous cache".as_slice()); // preserved（取消后保留完整断点与旧缓存）。
        drop(held);
        let resumed = download(&file, &target, Arc::new(AtomicBool::new(false)), &mut |_, _| {}, 0);
        let complete = fs::read(&target).unwrap() == body && !part.exists() && !checkpoint.exists();
        fs::remove_dir_all(&root).unwrap();
        assert!(result.unwrap_err().contains("任务已取消"));assert!(preserved);
        assert!(resumed.is_ok());assert!(complete);
    }

    #[test]
    fn native_download_bounds_file_lock_wait_and_preserves_previous_cache() {
        let body = b"verified replacement wheel";
        let root = std::env::temp_dir().join(format!("voice-download-超时测试-{}", crate::workspace::new_id()));
        fs::create_dir_all(&root).unwrap();let target = root.join("dependency.whl");
        let part = target.with_extension("whl.part");let checkpoint = target.with_extension("whl.part.identity");
        let file = DownloadFile { url: "http://127.0.0.1:1/dependency.whl".into(), size: body.len() as u64,
            sha256: format!("{:x}", Sha256::digest(body)), sha512: String::new(), path: "dependency.whl".into() };
        fs::write(&part, body).unwrap();fs::write(&checkpoint, format!("{}:sha256:{}", file.size, file.sha256)).unwrap();
        fs::write(&target, b"keep previous cache").unwrap();
        let held = fs::OpenOptions::new().read(true).share_mode(3).open(&target).unwrap();
        let started = std::time::Instant::now(); // started（文件占用等待的开始时间）。
        let result = download(&file, &target, Arc::new(AtomicBool::new(false)), &mut |_, _| {}, 0);
        let elapsed = started.elapsed(); // elapsed（实际等待时长，避免无限重试）。
        let preserved = fs::read(&part).unwrap() == body && checkpoint.exists() && fs::read(&target).unwrap() == b"keep previous cache";
        drop(held);fs::remove_dir_all(&root).unwrap();
        // Windows（系统）对已存在的占用目标也可能返回访问拒绝，两种结果都要限时保留断点。
        let error = result.unwrap_err();assert!(error.contains("下载文件仍被其他进程占用") || error.contains("系统仍拒绝启用下载文件"), "{error}");assert!(error.contains("断点已保留"));
        assert!(elapsed >= Duration::from_secs(5) && elapsed < Duration::from_secs(10));assert!(preserved);
    }

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
