//! GitHub acceleration qualification（代码托管平台加速隔离验收）：原始来源判定与真实上游网络算法。
use super::*;
use sha2::{Digest, Sha256};
use std::{fs, io::{Read, Write}, net::TcpListener, thread::{self, JoinHandle}, time::Instant};

#[derive(Clone, Copy, PartialEq)]
enum Behavior { Ranges, IgnoreRange, BrokenRange, Stall, Slow, StallAll } // Behavior（范围支持、忽略范围、错误范围、分片停滞、较慢来源或探测停滞）。
struct Fixture {
    url: String, // url（本机隔离来源）。
    body: Arc<Vec<u8>>, // body（固定文件内容）。
    requests: Arc<Mutex<Vec<Option<(usize, usize)>>>>, // requests（实际请求范围；空值代表整文件请求）。
    active: Arc<AtomicUsize>, // active（在途文件分片数）。
    peak: Arc<AtomicUsize>, // peak（真实分片并发峰值）。
    stop: Arc<AtomicBool>, // stop（停止夹具服务）。
    worker: Option<JoinHandle<()>>, // worker（监听及连接回收线程）。
}
impl Fixture {
    fn new(behavior: Behavior) -> Self {
        Self::sized(behavior,37,GITHUB_PARALLEL_THRESHOLD as usize)
    }
    fn sized(behavior:Behavior,value:u8,size:usize)->Self {
        let body=Arc::new(vec![value;size]);
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();listener.set_nonblocking(true).unwrap();
        let url=format!("http://{}/fixture.bin",listener.local_addr().unwrap());
        let requests=Arc::new(Mutex::new(vec![]));let active=Arc::new(AtomicUsize::new(0));let peak=Arc::new(AtomicUsize::new(0));let stop=Arc::new(AtomicBool::new(false));
        let (content,log,load,maximum,stopping)=(body.clone(),requests.clone(),active.clone(),peak.clone(),stop.clone());
        let worker=thread::spawn(move||{
            let mut connections=vec![];
            while !stopping.load(Ordering::Acquire) {
                let(mut socket,_)=match listener.accept(){Ok(value)=>value,Err(error) if error.kind()==std::io::ErrorKind::WouldBlock=>{thread::sleep(Duration::from_millis(5));continue;},Err(error)=>panic!("本机下载服务失败：{error}")};
                let(content,log,load,maximum,stopping)=(content.clone(),log.clone(),load.clone(),maximum.clone(),stopping.clone());
                connections.push(thread::spawn(move||{
                    socket.set_read_timeout(Some(Duration::from_secs(3))).unwrap();socket.set_write_timeout(Some(Duration::from_secs(3))).unwrap();
                    let mut request=vec![];let mut buffer=[0u8;4096];
                    while !request.ends_with(b"\r\n\r\n") {match socket.read(&mut buffer){Ok(0)|Err(_)=>return,Ok(count)=>request.extend_from_slice(&buffer[..count])}}
                    let text=String::from_utf8_lossy(&request);
                    let range=text.lines().find_map(|line|{
                        let(name,value)=line.split_once(':')?;
                        if !name.eq_ignore_ascii_case("range"){return None;}
                        let(first,last)=value.trim().strip_prefix("bytes=")?.split_once('-')?;
                        Some((first.parse::<usize>().ok()?,last.parse::<usize>().ok()?))
                    });
                    log.lock().unwrap().push(range);
                    if behavior==Behavior::StallAll {let started=Instant::now();while !stopping.load(Ordering::Acquire)&&started.elapsed()<Duration::from_secs(3){thread::sleep(Duration::from_millis(10));}return;}
                    if behavior==Behavior::Slow {thread::sleep(Duration::from_millis(120));}
                    let segment=range.is_some_and(|(first,last)|last-first+1==content.len()/4);
                    if segment {
                        let now=load.fetch_add(1,Ordering::AcqRel)+1;maximum.fetch_max(now,Ordering::AcqRel);
                        // 留出重叠传输时间，验收的是实际四路并发，不只检查请求总数。
                        if behavior==Behavior::Stall {let started=Instant::now();while !stopping.load(Ordering::Acquire)&&started.elapsed()<Duration::from_secs(3){thread::sleep(Duration::from_millis(10));}}
                        else {thread::sleep(Duration::from_millis(150));}
                    }
                    if behavior!=Behavior::Stall||!segment {
                        if let Some((first,last))=range.filter(|_|behavior!=Behavior::IgnoreRange) {
                            let reported_first=if behavior==Behavior::BrokenRange&&segment&&first==0{1}else{first};
                            let _=write!(socket,"HTTP/1.1 206 OK\r\nContent-Length: {}\r\nContent-Range: bytes {reported_first}-{last}/{}\r\nConnection: close\r\n\r\n",last-first+1,content.len());
                            let _=socket.write_all(&content[first..=last]);
                        } else {
                            let _=write!(socket,"HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",content.len());let _=socket.write_all(&content);
                        }
                    }
                    if segment {load.fetch_sub(1,Ordering::AcqRel);}
                }));
            }
            for connection in connections {connection.join().unwrap();}
        });
        Self{url,body,requests,active,peak,stop,worker:Some(worker)}
    }
    fn file(&self) -> DownloadFile {
        DownloadFile{url:"https://github.com/example/resource/releases/download/v1/fixture.bin".into(),size:self.body.len() as u64,
            sha256:format!("{:x}",Sha256::digest(&*self.body)),sha512:String::new(),path:"fixture.bin".into(),download_variants:vec![]}
    }
}
impl Drop for Fixture {fn drop(&mut self){self.stop.store(true,Ordering::Release);self.worker.take().unwrap().join().unwrap();}}
fn temporary_target(label:&str)->PathBuf {
    let root=std::env::temp_dir().join(format!("voice-github-{label}-{}",crate::workspace::new_id()));fs::create_dir_all(&root).unwrap();root.join("带 空格的下载.bin")
}
fn cleanup(target:&Path) {
    let root=target.parent().unwrap();
    for round in 0..20 {match fs::remove_dir_all(root){Ok(())=>return,Err(error) if round<19&&matches!(error.raw_os_error(),Some(32|33))=>thread::sleep(Duration::from_millis(50)),Err(error)=>panic!("下载夹具未释放：{error}")}}
}

#[test]
fn locked_upstream_uses_four_ranges_for_original_github_and_caches_verified_file() {
    let fixture=Fixture::new(Behavior::Ranges);let file=fixture.file();let target=temporary_target("四路加速");
    let caller=thread::current().id();let mut phases=vec![];
    download_with_sources(&file,&target,Arc::new(AtomicBool::new(false)),&mut|phase,done,total|{
        assert_eq!(thread::current().id(),caller);assert!(done<=total);phases.push(phase.to_owned());
    },0,&[fixture.url.clone()]).unwrap();
    assert_eq!(fixture.peak.load(Ordering::Acquire),4);
    let requests=fixture.requests.lock().unwrap().clone();assert_eq!(requests.len(),5);assert_eq!(requests[0],Some((0,65535)));
    assert!(requests.iter().skip(1).all(|range|range.is_some_and(|(first,last)|last-first+1==fixture.body.len()/4)));
    assert_eq!(crate::workspace::file_hash(&target).unwrap(),file.sha256);
    assert!(phases.iter().any(|phase|phase.contains("四路加速")));
    // 已校验缓存不再访问任何来源，且不启动上游并行工作线程。
    download_with_sources(&file,&target,Arc::new(AtomicBool::new(false)),&mut|_,_,_|{},0,&["http://127.0.0.1:1/unavailable".into()]).unwrap();
    assert!(!target.with_extension("bin.part.identity").exists());drop(fixture);cleanup(&target);
}

#[test]
fn locked_upstream_falls_back_on_ignored_or_broken_ranges() {
    for behavior in [Behavior::IgnoreRange,Behavior::BrokenRange] {
        let fixture=Fixture::new(behavior);let file=fixture.file();let target=temporary_target("单路回退");let mut phases=vec![];
        download_with_sources(&file,&target,Arc::new(AtomicBool::new(false)),&mut|phase,_,_|phases.push(phase.to_owned()),0,&[fixture.url.clone()]).unwrap();
        assert_eq!(crate::workspace::file_hash(&target).unwrap(),file.sha256);
        let requests=fixture.requests.lock().unwrap().clone();assert_eq!(requests.iter().filter(|range|range.is_none()).count(),1);
        if behavior==Behavior::BrokenRange {assert!(phases.iter().any(|phase|phase.contains("已回退单路")));}
        else {assert_eq!(requests.len(),2);assert_eq!(fixture.peak.load(Ordering::Acquire),0);}
        drop(fixture);cleanup(&target);
    }
}

#[test]
fn locked_upstream_preserves_old_cache_on_bad_digest_and_accepts_sha512() {
    let fixture=Fixture::new(Behavior::Ranges);let mut file=fixture.file();let target=temporary_target("摘要验证");
    fs::write(&target,b"previous verified cache").unwrap();file.sha256="0".repeat(64);
    let result=download_with_sources(&file,&target,Arc::new(AtomicBool::new(false)),&mut|_,_,_|{},0,&[fixture.url.clone()]);
    assert!(result.unwrap_err().contains("摘要不符"));assert_eq!(fs::read(&target).unwrap(),b"previous verified cache");
    file.sha256.clear();file.sha512=format!("{:x}",sha2::Sha512::digest(&*fixture.body));
    download_with_sources(&file,&target,Arc::new(AtomicBool::new(false)),&mut|_,_,_|{},0,&[fixture.url.clone()]).unwrap();
    assert_eq!(fs::read(&target).unwrap(),*fixture.body);drop(fixture);cleanup(&target);
}

#[test]
fn locked_upstream_cancels_four_stalled_connections_and_releases_files() {
    let fixture=Fixture::new(Behavior::Stall);let file=fixture.file();let target=temporary_target("四路取消");
    let cancel=Arc::new(AtomicBool::new(false));let(flag,active)=(cancel.clone(),fixture.active.clone());
    let trigger=thread::spawn(move||{let started=Instant::now();while active.load(Ordering::Acquire)<4&&started.elapsed()<Duration::from_secs(5){thread::sleep(Duration::from_millis(5));}flag.store(true,Ordering::Release);Instant::now()});
    let result=download_with_sources(&file,&target,cancel,&mut|_,_,_|{},0,&[fixture.url.clone()]);let cancelled_at=trigger.join().unwrap();
    assert!(result.unwrap_err().contains("取消"));assert!(cancelled_at.elapsed()<Duration::from_secs(2));assert!(!target.exists());
    for index in 0..4 {assert!(!PathBuf::from(format!("{}.part.part{index}",target.display())).exists());}
    drop(fixture);cleanup(&target);
}

#[test]
fn github_contiguous_checkpoint_resumes_without_restarting_parallel_download() {
    let fixture=Fixture::new(Behavior::Ranges);let file=fixture.file();let target=temporary_target("连续断点");let prefix=1024*1024;
    fs::write(target.with_extension("bin.part"),&fixture.body[..prefix]).unwrap();
    fs::write(target.with_extension("bin.part.identity"),format!("{}:sha256:{}",file.size,file.sha256)).unwrap();
    let mut phases=vec![];download_with_sources(&file,&target,Arc::new(AtomicBool::new(false)),&mut|phase,_,_|phases.push(phase.to_owned()),0,&[fixture.url.clone()]).unwrap();
    assert_eq!(fixture.requests.lock().unwrap()[0],Some((prefix,prefix+8*1024*1024-1)));
    assert!(!phases.iter().any(|phase|phase.contains("四路加速")));assert_eq!(crate::workspace::file_hash(&target).unwrap(),file.sha256);
    drop(fixture);cleanup(&target);
}

#[test]
fn accelerated_github_reserves_global_four_connection_budget_and_returns_it_on_cancel() {
    let fixture=Fixture::new(Behavior::Stall);let file=fixture.file();let target=temporary_target("全局四路配额");
    let cancel=Arc::new(AtomicBool::new(false));
    thread::scope(|scope|{
        let transfer_cancel=cancel.clone();let url=fixture.url.clone();let target_path=&target;
        let transfer=scope.spawn(move||download_with_sources(&file,target_path,transfer_cancel,&mut|_,_,_|{},0,&[url]));
        let started=Instant::now();while fixture.active.load(Ordering::Acquire)<4&&started.elapsed()<Duration::from_secs(5){thread::sleep(Duration::from_millis(5));}
        assert_eq!(fixture.active.load(Ordering::Acquire),4);
        let waiter_cancel=AtomicBool::new(false);let mut waited=false;
        let lease=acquire_slot(&waiter_cancel,&mut|phase,_,_|{waited=phase.contains("等待下载通道");waiter_cancel.store(true,Ordering::Release);});
        assert!(lease.is_err());assert!(waited);
        cancel.store(true,Ordering::Release);assert!(transfer.join().unwrap().unwrap_err().contains("取消"));
        let full_lease=acquire_slots(MAX_DOWNLOADS,&AtomicBool::new(false),&mut|_,_,_|{}).unwrap();drop(full_lease);
    });
    drop(fixture);cleanup(&target);
}

#[test]
fn batch_counts_acceleration_and_fallback_bytes_before_verification(){
    let mut row=FileProgress::default();
    row.update("下载固定资源（四路加速）",60,200);
    assert_eq!(row.downloaded,60);assert!(!row.transferred);assert_eq!(row.verified,0);
    row.update("下载固定资源（四路加速失败，已回退单路下载）",20,200);
    assert_eq!(row.downloaded,20);assert!(row.network_phase.contains("已回退单路"));
    row.update("下载固定资源（四路加速失败，已回退单路下载）",200,200);
    assert_eq!(row.downloaded,200);assert!(row.transferred);
    row.update("校验下载文件",100,200);assert_eq!(row.verified,100);assert_eq!(row.downloaded,200);
}

#[test]
fn unverified_cache_never_marks_transfer_complete_during_node_sampling(){
    let mut row=FileProgress::default();row.update("校验现有下载缓存",200,200);
    assert!(row.checking_cache);assert!(!row.cached);assert!(!row.transferred);
    row.update("来源测速：GitHub（代码托管平台）原站",0,200);
    assert!(!row.checking_cache);assert!(!row.cached);assert!(!row.transferred);assert_eq!(row.verified,0);
    row.update("下载固定资源（四路加速）",50,200);assert_eq!(row.downloaded,50);assert!(!row.transferred);
    let mut cached=FileProgress::default();cached.update("校验现有下载缓存",200,200);cached.update("复用已校验下载缓存",200,200);
    assert!(cached.cached&&cached.transferred);assert!(!cached.checking_cache);
}

fn route_policy(file:&DownloadFile,mirror:&Fixture,direct:&Fixture,auto_select:bool)->serde_json::Value {
    let mirror_url=format!("https://mirror.invalid/{}",file.url);
    serde_json::json!({
        "application_id":format!("VoiceTerminalQualification-{}",crate::workspace::new_id()),
        "acceleration_enabled":true,"auto_select":auto_select,"probe_timeout_ms":1500,"sample_bytes":1048576,
        "nodes":[{"id":"mirror-test","template":"https://mirror.invalid/{url}","priority":10,"enabled":true},
            {"id":"github-direct","template":"{url}","priority":1000,"enabled":true}],
        "test_sources":{mirror_url:mirror.url,file.url.clone():direct.url},
    })
}
fn node_cache_path(policy:&serde_json::Value)->PathBuf {
    PathBuf::from(std::env::var_os("LOCALAPPDATA").unwrap()).join(policy["application_id"].as_str().unwrap()).join("update-node-cache.json")
}
fn cleanup_node_cache(policy:&serde_json::Value){
    let path=node_cache_path(policy);if path.exists(){fs::remove_dir_all(path.parent().unwrap()).unwrap();}
}

#[test]
fn upstream_nodes_choose_current_faster_origin_or_mirror_and_cache_verified_success(){
    for direct_faster in [true,false]{
        let mirror=Fixture::new(if direct_faster{Behavior::Slow}else{Behavior::Ranges});
        let direct=Fixture::new(if direct_faster{Behavior::Ranges}else{Behavior::Slow});
        let file=direct.file();let policy=route_policy(&file,&mirror,&direct,true);let target=temporary_target("节点测速择优");let mut phases=vec![];
        let caller=thread::current().id();
        download_github_with_policy(&file,&target,Arc::new(AtomicBool::new(false)),&mut|phase,done,total|{
            assert_eq!(thread::current().id(),caller);assert!(done<=total);phases.push(phase.to_owned());
        },&policy.to_string()).unwrap();
        let selected=phases.iter().find(|phase|phase.starts_with("下载来源：")).unwrap();
        assert!(selected.contains(if direct_faster{"原站"}else{"mirror-test"}),"选择错误：{phases:?}");
        assert!(phases.iter().any(|phase|phase.contains("来源测速")&&phase.contains("KiB/秒")));
        assert!(phases.iter().any(|phase|phase.contains("下载固定资源")&&phase.contains("MiB/秒")));
        let cache:serde_json::Value=serde_json::from_slice(&fs::read(node_cache_path(&policy)).unwrap()).unwrap();
        assert_eq!(cache["lastSuccess"],if direct_faster{"github-direct"}else{"mirror-test"});
        assert_eq!(crate::workspace::file_hash(&target).unwrap(),file.sha256);
        // 完整缓存命中时不探测任何节点，也不更新来源缓存。
        let mut cache_phases=vec![];download_github_with_policy(&file,&target,Arc::new(AtomicBool::new(false)),&mut|phase,_,_|cache_phases.push(phase.to_owned()),&policy.to_string()).unwrap();
        assert!(!cache_phases.iter().any(|phase|phase.contains("来源")));
        // 上次成功节点不能压过当前实测速度，模拟切换 VPN（虚拟专用网络）后的旧缓存。
        let mut stale=cache;stale["lastSuccess"]=if direct_faster{"mirror-test"}else{"github-direct"}.into();
        fs::write(node_cache_path(&policy),stale.to_string()).unwrap();let refreshed_target=temporary_target("重新测速覆盖旧节点");let mut selected_now=String::new();
        download_github_with_policy(&file,&refreshed_target,Arc::new(AtomicBool::new(false)),&mut|phase,_,_|{if phase.starts_with("下载来源："){selected_now=phase.to_owned();}},&policy.to_string()).unwrap();
        assert!(selected_now.contains(if direct_faster{"原站"}else{"mirror-test"}));cleanup(&refreshed_target);
        cleanup_node_cache(&policy);drop((mirror,direct));cleanup(&target);
    }
}

#[test]
fn upstream_nodes_fall_back_after_bad_digest_without_publishing_or_caching_bad_source(){
    let mirror=Fixture::sized(Behavior::Ranges,99,GITHUB_PARALLEL_THRESHOLD as usize);let direct=Fixture::new(Behavior::Ranges);
    let file=direct.file();let target=temporary_target("节点摘要回退");fs::write(&target,b"previous cache").unwrap();
    let policy=route_policy(&file,&mirror,&direct,false);let mut phases=vec![];
    download_github_with_policy(&file,&target,Arc::new(AtomicBool::new(false)),&mut|phase,_,_|{
        if phase.contains("尝试下一来源"){assert_eq!(fs::read(&target).unwrap(),b"previous cache");}
        phases.push(phase.to_owned());
    },&policy.to_string()).unwrap();
    let sources=phases.iter().filter(|phase|phase.starts_with("下载来源：")).collect::<Vec<_>>();
    assert!(sources[0].contains("mirror-test"));assert!(sources[1].contains("原站"));
    assert_eq!(crate::workspace::file_hash(&target).unwrap(),file.sha256);
    let cache:serde_json::Value=serde_json::from_slice(&fs::read(node_cache_path(&policy)).unwrap()).unwrap();assert_eq!(cache["lastSuccess"],"github-direct");
    cleanup_node_cache(&policy);drop((mirror,direct));cleanup(&target);
}

#[test]
fn upstream_nodes_respect_disabled_acceleration_and_share_safe_cache_for_small_component_files(){
    let mirror=Fixture::sized(Behavior::Ranges,37,512*1024);let direct=Fixture::sized(Behavior::Ranges,37,512*1024);
    let file=direct.file();let mut policy=route_policy(&file,&mirror,&direct,false);policy["acceleration_enabled"]=false.into();
    let targets=(0..4).map(|_|temporary_target("共享节点缓存")).collect::<Vec<_>>();
    thread::scope(|scope|{let mut transfers=vec![];for target in &targets{let(file,policy)=(&file,&policy);transfers.push(scope.spawn(move||download_github_with_policy(file,target,Arc::new(AtomicBool::new(false)),&mut|_,_,_|{},&policy.to_string())));}for transfer in transfers{transfer.join().unwrap().unwrap();}});
    assert!(mirror.requests.lock().unwrap().is_empty());
    let cache:serde_json::Value=serde_json::from_slice(&fs::read(node_cache_path(&policy)).unwrap()).unwrap();assert_eq!(cache["lastSuccess"],"github-direct");assert_eq!(cache["nodes"].as_array().unwrap().len(),2);
    assert!(!node_cache_path(&policy).with_extension("json.tmp").exists());
    cleanup_node_cache(&policy);drop((mirror,direct));for target in targets{cleanup(&target);}
}

#[test]
fn upstream_nodes_cancel_stalled_transfer_and_do_not_try_the_next_source(){
    let mirror=Fixture::new(Behavior::Stall);let direct=Fixture::new(Behavior::Ranges);let file=direct.file();
    let policy=route_policy(&file,&mirror,&direct,false);let target=temporary_target("节点取消");let cancel=Arc::new(AtomicBool::new(false));
    let(flag,active)=(cancel.clone(),mirror.active.clone());let trigger=thread::spawn(move||{
        let started=Instant::now();while active.load(Ordering::Acquire)<4&&started.elapsed()<Duration::from_secs(5){thread::sleep(Duration::from_millis(5));}flag.store(true,Ordering::Release);
    });
    let result=download_github_with_policy(&file,&target,cancel,&mut|_,_,_|{},&policy.to_string());trigger.join().unwrap();
    assert!(result.unwrap_err().contains("取消"));assert!(!target.exists());assert!(direct.requests.lock().unwrap().is_empty());assert!(!node_cache_path(&policy).exists());
    drop((mirror,direct));cleanup(&target);
}

#[test]
fn upstream_node_speed_probe_has_a_deadline_and_cancels_without_fallback(){
    for cancelled in [false,true]{
        let mirror=Fixture::new(Behavior::StallAll);let direct=Fixture::new(Behavior::Ranges);let file=direct.file();
        let mut policy=route_policy(&file,&mirror,&direct,true);policy["probe_timeout_ms"]=500.into();let target=temporary_target("探测总时限");
        let cancel=Arc::new(AtomicBool::new(false));let(flag,requests)=(cancel.clone(),mirror.requests.clone());
        let trigger=if cancelled{Some(thread::spawn(move||{let started=Instant::now();while requests.lock().unwrap().is_empty()&&started.elapsed()<Duration::from_secs(3){thread::sleep(Duration::from_millis(5));}flag.store(true,Ordering::Release);} ))}else{None};
        let started=Instant::now();let result=download_github_with_policy(&file,&target,cancel,&mut|_,_,_|{},&policy.to_string());
        assert!(started.elapsed()<Duration::from_secs(3));
        if let Some(trigger)=trigger{trigger.join().unwrap();assert!(result.unwrap_err().contains("取消"));assert!(direct.requests.lock().unwrap().is_empty());assert!(!target.exists());}
        else{result.unwrap();assert_eq!(crate::workspace::file_hash(&target).unwrap(),file.sha256);}
        cleanup_node_cache(&policy);drop((mirror,direct));cleanup(&target);
    }
}
