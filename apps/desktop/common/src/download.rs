//! Native download（原生下载）：静态 C ABI（C 调用接口）与固定摘要。
use std::{ffi::{CString, c_void}, path::{Path,PathBuf}, sync::{mpsc,Arc,Mutex,Condvar, atomic::{AtomicBool,AtomicU64,AtomicUsize, Ordering}},time::Duration};
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

type Callback = unsafe extern "C" fn(*mut c_void, i32, u64, u64) -> i32;
type CancelCheck=unsafe extern "C" fn(*const c_void)->i32; // CancelCheck（独立原子信号读取接口）。
unsafe extern "C" fn cancel_check(context:*const c_void)->i32{
    i32::from(unsafe{&*(context as *const AtomicBool)}.load(Ordering::Acquire))
}
const MAX_DOWNLOADS:usize=4; // MAX_DOWNLOADS（全局并行传输上限）：所有组件共享四个下载通道。
static DOWNLOAD_SLOTS:Mutex<usize>=Mutex::new(0);
static DOWNLOAD_CHANGED:Condvar=Condvar::new();
struct DownloadSlot; // DownloadSlot（下载通道租约）：失败和取消也归还通道。
impl Drop for DownloadSlot{fn drop(&mut self){if let Ok(mut count)=DOWNLOAD_SLOTS.lock(){*count=count.saturating_sub(1);DOWNLOAD_CHANGED.notify_one();}}}
fn acquire_slot(cancel:&AtomicBool,callback:&mut dyn FnMut(&str,u64,u64))->Result<DownloadSlot,String>{
    let mut count=DOWNLOAD_SLOTS.lock().map_err(|_|"下载通道状态不可用。")?;
    while *count>=MAX_DOWNLOADS{
        if cancel.load(Ordering::Acquire){return Err("任务已取消，断点已保留。".into());}
        drop(count);callback("等待下载通道",0,0);
        count=DOWNLOAD_SLOTS.lock().map_err(|_|"下载通道状态不可用。")?;
        if *count>=MAX_DOWNLOADS{count=DOWNLOAD_CHANGED.wait_timeout(count,Duration::from_millis(100)).map_err(|_|"下载通道状态不可用。")?.0;}
    }
    if cancel.load(Ordering::Acquire){return Err("任务已取消，断点已保留。".into());}
    *count+=1;Ok(DownloadSlot)
}
#[cfg(windows)]
unsafe extern "C" {
    fn vt_download(url: *const i8, target: *const u16, size: u64, expected: *const i8,
        sha512: i32, probe_limit: u64, callback: Callback, context: *mut c_void,
        cancel_check:CancelCheck,cancel_context:*const c_void,error: *mut i8, error_size: usize) -> i32;
}
struct Progress<'a> {
    cancel: Arc<AtomicBool>, // cancel（取消信号）。
    callback: &'a mut dyn FnMut(&str,u64, u64), // callback（阶段与字节进度回调）。
}
unsafe extern "C" fn progress(context: *mut c_void, phase:i32, current: u64, total: u64) -> i32 {
    let state = unsafe { &mut *(context as *mut Progress<'_>) };
    (state.callback)(match phase{2=>"校验下载文件",3=>"启用已校验缓存",4=>"校验现有下载缓存",_=>"下载固定资源"},current, total);
    i32::from(state.cancel.load(Ordering::Acquire))
}

pub fn download(file: &DownloadFile, target: &Path, cancel: Arc<AtomicBool>,
    callback: &mut dyn FnMut(u64, u64), probe_limit: u64) -> Result<u64, String> {
    download_with_progress(file,target,cancel,&mut|_,done,total|callback(done,total),probe_limit)
}
/// download_with_progress（分阶段下载）：网络、缓存完整摘要和启用阶段均有进度与取消。
pub fn download_with_progress(file:&DownloadFile,target:&Path,cancel:Arc<AtomicBool>,callback:&mut dyn FnMut(&str,u64,u64),probe_limit:u64)->Result<u64,String>{
    let _slot=acquire_slot(&cancel,callback)?;
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
            probe_limit.min(65536), progress, &mut state as *mut _ as *mut c_void, cancel_check, Arc::as_ptr(&state.cancel) as *const c_void, message.as_mut_ptr(), message.len()) };
        if result < 0 {
            let bytes: Vec<_> = message.iter().take_while(|v| **v != 0).map(|v| *v as u8).collect();
            return Err(String::from_utf8_lossy(&bytes).to_string());
        }
        Ok(result as u64)
    }
    #[cfg(not(windows))] { let _=(target,cancel,callback,url,hash); Err("原生下载仅支持 Windows（操作系统）。".into()) }
}

/// download_batch（有界批量下载）：依赖与模型文件共用全局通道，回调只在调用线程执行。
pub fn download_batch(files:&[(DownloadFile,PathBuf)],cancel:Arc<AtomicBool>,callback:&mut dyn FnMut(&str,u64,u64))->Result<(),String>{
    let total=files.iter().try_fold(0u64,|sum,(file,_)|sum.checked_add(file.size.checked_mul(2).ok_or("下载大小溢出")?).ok_or("下载大小溢出"))?;
    let next=AtomicUsize::new(0);let done=AtomicU64::new(0);let checked=AtomicUsize::new(0);let stop=AtomicBool::new(false);
    let failure=Mutex::new(None::<String>);let (sender,receiver)=mpsc::channel();let count=files.len().min(MAX_DOWNLOADS);
    let transfer_cancel=Arc::new(AtomicBool::new(cancel.load(Ordering::Acquire))); // transfer_cancel（内部取消）：失败时收集其余传输线程，不改写用户取消状态。
    callback("下载与摘要校验",0,total);
    std::thread::scope(|scope|{
        let mut workers=vec![];
        for _ in 0..count{let sender=sender.clone();let next=&next;let done=&done;let checked=&checked;let stop=&stop;let failure=&failure;let cancel=cancel.clone();let transfer_cancel=transfer_cancel.clone();
            workers.push(scope.spawn(move||{
                let result=std::panic::catch_unwind(std::panic::AssertUnwindSafe(||{
                    loop{if cancel.load(Ordering::Acquire){return Err("任务已取消，断点已保留。".into());}if stop.load(Ordering::Acquire){break;}
                        let index=next.fetch_add(1,Ordering::Relaxed);let Some((file,target))=files.get(index)else{break;};let mut reported=0;
                        download_with_progress(file,target,transfer_cancel.clone(),&mut|phase,current,_|{
                            let current=match phase{"校验下载文件"=>file.size.saturating_add(current.min(file.size)),"启用已校验缓存"=>file.size.saturating_mul(2),"校验现有下载缓存"=>0,_=>current.min(file.size)};
                            let current=current.max(reported);done.fetch_add(current-reported,Ordering::Relaxed);reported=current;
                        },0)?;checked.fetch_add(1,Ordering::Relaxed);
                    }Ok::<(),String>(())
                })).unwrap_or_else(|_|Err("下载工作线程异常退出，断点已保留。".into()));
                if let Err(error)=result{let mut failure=failure.lock().unwrap();if failure.is_none(){*failure=Some(error);}stop.store(true,Ordering::Release);}let _=sender.send(());
            }));
        }
        drop(sender);let mut finished=0;
        while finished<count{match receiver.recv_timeout(Duration::from_millis(100)){Ok(())=>finished+=1,Err(mpsc::RecvTimeoutError::Timeout)=>{},Err(mpsc::RecvTimeoutError::Disconnected)=>break}
            if cancel.load(Ordering::Acquire)||stop.load(Ordering::Acquire){transfer_cancel.store(true,Ordering::Release);}
            callback(&format!("下载与摘要校验（{} / {} 个文件）",checked.load(Ordering::Relaxed),files.len()),done.load(Ordering::Relaxed).min(total),total);}
        for worker in workers{if worker.join().is_err(){*failure.lock().unwrap()=Some("下载工作线程异常退出，断点已保留。".into());}}
    });
    if cancel.load(Ordering::Acquire){return Err("任务已取消，断点已保留。".into());}
    if let Some(error)=failure.into_inner().map_err(|_|"下载状态不可用。")?{return Err(error);}
    callback("下载与摘要校验完成",total,total);Ok(())
}

#[cfg(all(test, windows, feature = "qualification"))]
mod tests {
    use super::*;
    use std::{fs, io::{Read, Write}, net::TcpListener};
    use sha2::{Digest, Sha256};
    use std::{os::windows::fs::OpenOptionsExt, time::Duration};

    #[test]
    fn cached_hash_reports_progress_and_cancels_without_touching_verified_file(){
        let root=std::env::temp_dir().join(format!("voice-download-摘要取消-{}",crate::workspace::new_id()));fs::create_dir_all(&root).unwrap();
        let body=vec![42u8;8*1024*1024];let target=root.join("缓存.bin");fs::write(&target,&body).unwrap();
        let file=DownloadFile{url:"http://127.0.0.1:1/cache.bin".into(),size:body.len() as u64,sha256:format!("{:x}",Sha256::digest(&body)),sha512:String::new(),path:"cache.bin".into()};
        let cancel=Arc::new(AtomicBool::new(false));let mut phases=vec![];let started=std::time::Instant::now();
        let result=download_with_progress(&file,&target,cancel.clone(),&mut|phase,done,_|{
            phases.push(phase.to_owned());if phase=="校验现有下载缓存"&&done>=1024*1024{cancel.store(true,Ordering::Release);}
        },0);
        assert!(result.unwrap_err().contains("任务已取消"));assert!(started.elapsed()<Duration::from_secs(2));
        assert!(phases.iter().any(|phase|phase=="校验现有下载缓存"));assert_eq!(fs::read(&target).unwrap(),body);
        download(&file,&target,Arc::new(AtomicBool::new(false)),&mut|_,_|{},0).unwrap();fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn stalled_network_request_is_cancelled_and_all_handles_are_released(){
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();let address=listener.local_addr().unwrap();
        let (arrived,accepted)=mpsc::channel();let (release,wait)=mpsc::channel();
        let server=std::thread::spawn(move||{let(mut socket,_)=listener.accept().unwrap();let mut request=[0;4096];socket.read(&mut request).unwrap();arrived.send(()).unwrap();let _=wait.recv_timeout(Duration::from_secs(3));});
        let root=std::env::temp_dir().join(format!("voice-download-网络停滞-{}",crate::workspace::new_id()));fs::create_dir_all(&root).unwrap();let target=root.join("model.bin");
        let file=DownloadFile{url:format!("http://{address}/model.bin"),size:1024,sha256:format!("{:x}",Sha256::digest(vec![1u8;1024])),sha512:String::new(),path:"model.bin".into()};
        let cancel=Arc::new(AtomicBool::new(false));let flag=cancel.clone();
        let trigger=std::thread::spawn(move||{accepted.recv_timeout(Duration::from_secs(3)).unwrap();let start=std::time::Instant::now();flag.store(true,Ordering::Release);start});
        let result=download(&file,&target,cancel,&mut|_,_|{},0);let started=trigger.join().unwrap();
        assert!(started.elapsed()<Duration::from_secs(2));assert!(result.unwrap_err().contains("任务已取消"));
        release.send(()).unwrap();server.join().unwrap();assert!(!target.exists());fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn ignored_range_restarts_full_stream_and_verifies_large_file(){
        let body=Arc::new(vec![19u8;9*1024*1024]);let expected=format!("{:x}",Sha256::digest(&*body));
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();let address=listener.local_addr().unwrap();let content=body.clone();
        let server=std::thread::spawn(move||{let mut requests=vec![];for _ in 0..2{
            let(mut socket,_)=listener.accept().unwrap();let mut request=[0;4096];let length=socket.read(&mut request).unwrap();requests.push(String::from_utf8_lossy(&request[..length]).to_string());
            write!(socket,"HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",content.len()).unwrap();let _=socket.write_all(&content);
        }requests});
        let root=std::env::temp_dir().join(format!("voice-download-无范围-{}",crate::workspace::new_id()));fs::create_dir_all(&root).unwrap();let target=root.join("large.bin");
        let file=DownloadFile{url:format!("http://{address}/large.bin"),size:body.len() as u64,sha256:expected.clone(),sha512:String::new(),path:"large.bin".into()};
        fs::write(target.with_extension("bin.part"),vec![99u8;1024*1024]).unwrap();fs::write(target.with_extension("bin.part.identity"),format!("{}:sha256:{}",file.size,expected)).unwrap();
        download(&file,&target,Arc::new(AtomicBool::new(false)),&mut|_,_|{},0).unwrap();let requests=server.join().unwrap();
        assert!(requests[0].contains("Range: bytes=1048576-"));assert!(!requests[1].contains("Range:"));assert_eq!(crate::workspace::file_hash(&target).unwrap(),expected);
        assert!(!target.with_extension("bin.part").exists());fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn independent_batches_share_global_concurrency_and_report_on_caller(){
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();let address=listener.local_addr().unwrap();
        let active=Arc::new(AtomicUsize::new(0));let peak=Arc::new(AtomicUsize::new(0));let load=active.clone();let maximum=peak.clone();
        let server=std::thread::spawn(move||{let mut workers=vec![];for _ in 0..12{
            let(mut socket,_)=listener.accept().unwrap();let load=load.clone();let maximum=maximum.clone();
            workers.push(std::thread::spawn(move||{let now=load.fetch_add(1,Ordering::AcqRel)+1;maximum.fetch_max(now,Ordering::AcqRel);
                let mut request=[0;4096];let length=socket.read(&mut request).unwrap();let request=String::from_utf8_lossy(&request[..length]);let index=request.split_whitespace().nth(1).unwrap().trim_start_matches('/').trim_end_matches(".bin").parse::<u8>().unwrap();
                std::thread::sleep(Duration::from_millis(75));let body=vec![index;256*1024];
                write!(socket,"HTTP/1.1 206 OK\r\nContent-Length: {}\r\nContent-Range: bytes 0-{}/{}\r\nConnection: close\r\n\r\n",body.len(),body.len()-1,body.len()).unwrap();socket.write_all(&body).unwrap();load.fetch_sub(1,Ordering::AcqRel);
            }));
        }for worker in workers{worker.join().unwrap();}});
        let root=std::env::temp_dir().join(format!("voice-download-并行-{}",crate::workspace::new_id()));fs::create_dir_all(&root).unwrap();
        let files=(0u8..12).map(|index|{let name=format!("{index}.bin");let body=vec![index;256*1024];
            (DownloadFile{url:format!("http://{address}/{name}"),size:body.len() as u64,sha256:format!("{:x}",Sha256::digest(&body)),sha512:String::new(),path:name.clone()},root.join(name))}).collect::<Vec<_>>();
        std::thread::scope(|scope|{let mut workers=vec![];for batch in files.chunks(6){workers.push(scope.spawn(move||{
            let caller=std::thread::current().id();let mut samples=vec![];
            download_batch(batch,Arc::new(AtomicBool::new(false)),&mut|_,done,total|{assert_eq!(std::thread::current().id(),caller);assert!(done<=total);samples.push((done,total));}).unwrap();
            assert!(samples.windows(2).all(|pair|pair[0].0<=pair[1].0));assert_eq!(samples.last().unwrap().0,samples.last().unwrap().1);
        }));}for worker in workers{worker.join().unwrap();}});server.join().unwrap();
        assert!((2..=MAX_DOWNLOADS).contains(&peak.load(Ordering::Acquire)));assert_eq!(active.load(Ordering::Acquire),0);
        // 服务端已关闭，全部文件仍能从完整缓存复用，不需要再次联网。
        download_batch(&files,Arc::new(AtomicBool::new(false)),&mut|_,_,_|{}).unwrap();fs::remove_dir_all(root).unwrap();
    }

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
