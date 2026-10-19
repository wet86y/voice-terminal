#![cfg_attr(not(debug_assertions),windows_subsystem="windows")]
#[allow(dead_code)] // 安装器复用窗口图标，不使用共享模块的托盘函数。
#[path="../../src-tauri/src/app_icon.rs"] mod app_icon;
use std::{fs,io::Write,path::{Path,PathBuf},sync::{Arc,Mutex,atomic::{AtomicBool,Ordering}},time::{Duration,Instant}};
use serde_json::{Value,json};
use tauri::{Manager,WebviewWindow,WebviewWindowBuilder,WebviewUrl};
use voice_desktop_common::{installation,native_ui,workspace,shortcuts};
const PACKAGE:&[u8]=include_bytes!(concat!(env!("OUT_DIR"),"/program.zip"));
#[derive(Clone)]
struct UpdateParent{pid:u32,create_time:u64,executable:PathBuf} // UpdateParent（待退出的旧程序进程身份）。
struct Installer{
    target:Mutex<Option<PathBuf>>, // target（所选工作区根目录）。
    status:Mutex<Value>, // status（共享启动页阶段、进度和错误）。
    busy:AtomicBool, // busy（禁止重复安装任务）。
    cancel:Arc<AtomicBool>, // cancel（展开和校验的取消请求）。
    installed:AtomicBool, // installed（本次目标已启用，不因交接失败再次覆盖）。
    exiting:AtomicBool, // exiting（取消后退出请求）。
    shortcut:bool, // shortcut（隔离验收可关闭桌面快捷方式）。
    report:Option<PathBuf>, // report（隔离验收结果位置）。
    update_parent:Option<UpdateParent>, // update_parent（应用更新需正常退出的旧程序）。
}
fn trusted(window:&WebviewWindow)->Result<(),String>{let url=window.url().map_err(|e|e.to_string())?;if window.label()=="startup"&&["/","/index.html"].contains(&url.path())&&((url.scheme()=="tauri"&&url.host_str()==Some("localhost"))||(url.scheme()=="http"&&url.host_str()==Some("tauri.localhost")&&url.port().is_none())){Ok(())}else{Err("此页面没有安装权限。".into())}}
#[tauri::command]fn desktop_status(window:WebviewWindow,state:tauri::State<Arc<Installer>>)->Result<Value,String>{trusted(&window)?;let status=state.status.lock().unwrap().clone();Ok(json!({"role":"installer","frontend_mode":"installer","development":false,"ready":false,"workspace_ready":false,"workspace_busy":state.busy.load(Ordering::Acquire),"initialization_failed":status["error"].is_string(),"error":status["error"],"initialization":status,"fields":{"role":"入口职责：installer（安装器）","development":"安装器不启用源码调试","initialization":"本体安装阶段"}}))}
fn run_install(app:&tauri::AppHandle,state:Arc<Installer>,reselect:bool)->Result<Value,String>{
    if state.busy.swap(true,Ordering::SeqCst){return Err("安装任务正在执行。".into());}
    state.cancel.store(false,Ordering::Release);
    let result=(||{
        let current=if reselect{None}else{state.target.lock().unwrap().clone()};
        let root=match current{Some(root)=>root,None=>native_ui::choose_folder("选择语音终端工作区（空文件夹或已有工作区，支持中文路径）")?.ok_or("已取消目录选择")?};
        if state.target.lock().unwrap().as_ref()!=Some(&root){state.installed.store(false,Ordering::Release);}
        *state.target.lock().unwrap()=Some(root.clone());
        let mut progress=|phase:&str,done:u64,total:u64|{*state.status.lock().unwrap()=json!({"phase":phase,"completed_bytes":done,"total_bytes":total,"can_cancel":phase.starts_with("正在展开")||phase.starts_with("正在校验"),"error":null,"fields":{"phase":"安装阶段","completed_bytes":"已处理字节","total_bytes":"总字节数","can_cancel":"启用前允许取消","error":"失败原因"}});};
        if let Some(parent)=&state.update_parent{progress("正在等待旧程序退出并释放工作区",0,0);wait_for_update_parent(&root,parent,&mut progress)?;}
        if !state.installed.load(Ordering::Acquire){progress("正在检查工作区",0,0);installation::install(PACKAGE,&root,state.cancel.clone(),&mut progress)?;state.installed.store(true,Ordering::Release);}
        progress("创建桌面快捷方式",0,0);
        if state.shortcut{shortcuts::create(&root,None)?;}
        progress("正在启动正式程序",0,0);let pid=installation::launch(&root)?;
        let record=json!({"ok":true,"target_root":root,"product_pid":pid,"installer_pid":std::process::id(),"fields":{"ok":"首次窗口交接成功","target_root":"目标工作区","product_pid":"独立正式宿主进程编号","installer_pid":"交接后退出的安装器进程编号"}});
        if let Some(path)=&state.report{workspace::atomic_json(path,&record)?;}
        state.exiting.store(true,Ordering::Release);app.exit(0);Ok(record)
    })();
    state.busy.store(false,Ordering::Release);
    if let Err(error)=&result{state.status.lock().unwrap()["error"]=json!(error);if let Some(path)=&state.report{let _=workspace::atomic_json(path,&json!({"ok":false,"error":error,"fields":{"ok":"安装未完成","error":"失败原因"}}));}}
    if state.exiting.load(Ordering::Acquire){app.exit(0);}result
}
#[tauri::command]async fn desktop_initialize(window:WebviewWindow,app:tauri::AppHandle,state:tauri::State<'_,Arc<Installer>>,reselect:bool)->Result<Value,String>{trusted(&window)?;let state=state.inner().clone();tauri::async_runtime::spawn_blocking(move||run_install(&app,state,reselect)).await.map_err(|e|e.to_string())?}
#[tauri::command]fn desktop_cancel_init(window:WebviewWindow,state:tauri::State<Arc<Installer>>)->Result<(),String>{trusted(&window)?;if state.status.lock().unwrap()["can_cancel"]!=true{return Err("当前阶段需要完成启用及交接，请稍候。".into());}state.cancel.store(true,Ordering::Release);Ok(())}
fn exit(app:&tauri::AppHandle,state:&Installer){if state.busy.load(Ordering::Acquire){if state.status.lock().unwrap()["can_cancel"]==true{state.exiting.store(true,Ordering::Release);state.cancel.store(true,Ordering::Release);}}else{state.exiting.store(true,Ordering::Release);app.exit(0);}}
#[tauri::command]fn exit_application(window:WebviewWindow,app:tauri::AppHandle,state:tauri::State<Arc<Installer>>)->Result<(),String>{trusted(&window)?;if state.busy.load(Ordering::Acquire)&&state.status.lock().unwrap()["can_cancel"]!=true{return Err("启用及启动交接阶段暂不能退出。".into());}exit(&app,&state);Ok(())}

fn has_chinese(value:&str)->bool{value.chars().any(|ch|('\u{4e00}'..='\u{9fff}').contains(&ch))}
/// validate_update_nonce（校验单次编号）：兼容已发布程序的“十六进制时间戳-进程号”。
fn validate_update_nonce(nonce:&str,parent_pid:u32)->Result<(),String>{
    let legacy=nonce.split_once('-').is_some_and(|(time,pid)|{
        !time.is_empty()&&time.len()<=32&&time.bytes().all(|byte|byte.is_ascii_hexdigit())
            &&u128::from_str_radix(time,16).is_ok_and(|value|value>0)
            &&!pid.is_empty()&&pid.len()<=8&&pid.bytes().all(|byte|byte.is_ascii_hexdigit())
            &&u32::from_str_radix(pid,16)==Ok(parent_pid)&&parent_pid!=0
    });
    // UUID（通用唯一标识）采用标准分段，禁止路径字符和任意连字符组合。
    let uuid=nonce.len()==36&&nonce.bytes().enumerate().all(|(index,byte)|{
        if [8,13,18,23].contains(&index){byte==b'-'}else{byte.is_ascii_hexdigit()}
    });
    if legacy||uuid{Ok(())}else{Err("应用更新单次编号无效。".into())}
}

/// validate_update_parent（校验旧程序身份）：旧版启动安装器后会立即正常退出。
fn validate_update_parent(parent:&UpdateParent,actual:Option<(u64,PathBuf)>)->Result<(),String>{
    if let Some((created,executable))=actual{
        if created!=parent.create_time||!executable.to_string_lossy().eq_ignore_ascii_case(&parent.executable.to_string_lossy()){
            return Err("旧程序进程身份已变化，已拒绝更新。".into());
        }
    }
    // 进程已退出时继续使用前面核对的工作区、安装器摘要及配置路径；
    // 启用前仍由 wait_for_update_parent（等待旧程序）检查占用并取得工作区锁。
    Ok(())
}

/// update_parent_identity（严格查询旧程序）：仅明确不存在或已退出的进程可视为已退出。
fn update_parent_identity(pid:u32)->Result<Option<(u64,PathBuf)>,String>{
    use windows_sys::Win32::{Foundation::{CloseHandle,FILETIME,GetLastError,ERROR_INVALID_PARAMETER},System::Threading::{OpenProcess,GetProcessTimes,QueryFullProcessImageNameW,GetExitCodeProcess}};
    unsafe{
        let handle=OpenProcess(0x1000,0,pid);
        if handle.is_null(){
            let error=GetLastError();
            return if error==ERROR_INVALID_PARAMETER{Ok(None)}else{Err(format!("无法核对旧程序进程身份：{}",std::io::Error::from_raw_os_error(error as i32)))};
        }
        let result=(||{
            let mut code=0;if GetExitCodeProcess(handle,&mut code)==0{return Err(std::io::Error::last_os_error().to_string());}if code!=259{return Ok(None);}
            let mut created:FILETIME=std::mem::zeroed();let mut exited:FILETIME=std::mem::zeroed();let mut kernel:FILETIME=std::mem::zeroed();let mut user:FILETIME=std::mem::zeroed();
            if GetProcessTimes(handle,&mut created,&mut exited,&mut kernel,&mut user)==0{return Err(std::io::Error::last_os_error().to_string());}
            let mut name=vec![0u16;32768];let mut count=name.len() as u32;
            if QueryFullProcessImageNameW(handle,0,name.as_mut_ptr(),&mut count)==0{return Err(std::io::Error::last_os_error().to_string());}
            Ok(Some((((created.dwHighDateTime as u64)<<32)|created.dwLowDateTime as u64,PathBuf::from(String::from_utf16_lossy(&name[..count as usize])))))
        })();CloseHandle(handle);result
    }
}

fn mark_update_consumed(path:&Path,nonce:&str)->Result<(),String>{
    let consumed=path.with_extension("json.consumed");
    let mut marker=fs::OpenOptions::new().write(true).create_new(true).open(&consumed).map_err(|_|"此应用更新配置已使用或正在处理。")?;
    marker.write_all(nonce.as_bytes()).map_err(|error|error.to_string())?;marker.sync_all().map_err(|error|error.to_string())?;drop(marker);
    fs::remove_file(path).map_err(|error|format!("无法消费一次性更新配置：{error}"))
}

fn consume_update_config(path:&Path)->Result<(PathBuf,UpdateParent),String>{
    let supplied=voice_desktop_common::canonical_path(path)?;
    let config=voice_desktop_common::resources::load(&supplied)?;
    if config["schema"]!=1{return Err("应用更新配置格式无效。".into());}
    let allowed=["schema","nonce","target_root","workspace_id","parent_pid","parent_create_time","parent_executable","version","tag","installer_size","installer_sha256","fields"];
    if !config.as_object().is_some_and(|object|object.keys().all(|key|allowed.contains(&key.as_str()))){return Err("应用更新配置含有未识别字段。".into());}
    let nonce=config["nonce"].as_str().ok_or("应用更新配置缺少单次编号")?;
    let pid=config["parent_pid"].as_u64().and_then(|value|u32::try_from(value).ok()).filter(|value|*value!=0).ok_or("旧程序进程编号无效")?;
    validate_update_nonce(nonce,pid)?;
    for key in ["schema","nonce","target_root","workspace_id","parent_pid","parent_create_time","parent_executable","version","tag","installer_size","installer_sha256"]{
        if !config["fields"][key].as_str().is_some_and(has_chinese){return Err(format!("应用更新配置字段 {key} 缺少中文说明。"));}
    }
    let root=voice_desktop_common::canonical_path(Path::new(config["target_root"].as_str().ok_or("应用更新配置缺少工作区")?))?;
    installation::validate_target(&root)?;
    let config_name=format!("app-update-{nonce}.json");
    let expected=voice_desktop_common::workspace::safe_path(&root,&format!("tmp/{config_name}"))?;
    if supplied!=voice_desktop_common::canonical_path(&expected)?||supplied.parent()!=Some(root.join("tmp").as_path()){
        return Err("应用更新配置必须位于目标工作区的临时目录。".into());
    }
    let portable=voice_desktop_common::resources::load(&voice_desktop_common::workspace::safe_path(&root,"portable.json")?)?;
    let workspace_id=portable["workspace_id"].as_str().filter(|value|!value.is_empty()).ok_or("目标目录不是有效工作区")?;
    if config["workspace_id"].as_str()!=Some(workspace_id){return Err("工作区身份不匹配，已拒绝更新。".into());}
    let version=config["version"].as_str().ok_or("更新配置缺少目标版本")?;
    semver::Version::parse(version).map_err(|_|"更新配置版本无效。")?;
    if config["tag"].as_str()!=Some(&format!("v{version}"))||installation::package_version(PACKAGE)?!=version{
        return Err("发行标签、更新配置与内嵌程序版本不匹配。".into());
    }
    let installer_size=config["installer_size"].as_u64().filter(|size|*size>0).ok_or("更新配置缺少安装器字节数")?;
    let installer_sha256=config["installer_sha256"].as_str().filter(|hash|hash.len()==64&&hash.bytes().all(|byte|byte.is_ascii_hexdigit())).ok_or("更新配置缺少安装器摘要")?;
    let self_path=voice_desktop_common::canonical_path(&std::env::current_exe().map_err(|error|error.to_string())?)?;
    if fs::metadata(&self_path).map_err(|error|error.to_string())?.len()!=installer_size||!workspace::file_hash(&self_path)?.eq_ignore_ascii_case(installer_sha256){
        return Err("当前分发器与更新配置声明的已校验安装器不匹配。".into());
    }
    if pid==std::process::id(){return Err("旧程序进程身份无效。".into());}
    let create_time=config["parent_create_time"].as_u64().ok_or("更新配置缺少旧程序创建时间")?;
    let executable=voice_desktop_common::canonical_path(Path::new(config["parent_executable"].as_str().ok_or("更新配置缺少旧程序位置")?))?;
    let expected_executable=voice_desktop_common::canonical_path(&root.join("app/VoiceTerminalDesktop.exe"))?;
    if executable!=expected_executable{return Err("旧程序不属于目标工作区，已拒绝更新。".into());}
    let parent=UpdateParent{pid,create_time,executable};
    validate_update_parent(&parent,update_parent_identity(pid)?)?;
    mark_update_consumed(&supplied,nonce)?;
    Ok((root,parent))
}

fn wait_for_update_parent(root:&Path,parent:&UpdateParent,progress:&mut dyn FnMut(&str,u64,u64))->Result<(),String>{
    let deadline=Instant::now()+Duration::from_secs(120);
    loop{
        let actual=update_parent_identity(parent.pid)?;
        validate_update_parent(parent,actual.clone())?;
        let still_running=actual.is_some();
        if !still_running{break;}
        if Instant::now()>=deadline{return Err("旧程序未能在两分钟内退出；原工作区未变更。".into());}
        progress("正在等待旧程序正常退出",0,0);std::thread::sleep(Duration::from_millis(200));
    }
    let deadline=Instant::now()+Duration::from_secs(30);
    loop{
        installation::ensure_idle(root)?;
        match installation::WorkspaceGuard::acquire(root){
            Ok(guard)=>{drop(guard);return Ok(());}
            Err(error) if Instant::now()<deadline=>{progress("正在等待工作区锁释放",0,0);let _=error;std::thread::sleep(Duration::from_millis(200));}
            Err(error)=>return Err(format!("无法取得工作区更新锁：{error}")),
        }
    }
}
fn run()->Result<(),String>{
    if PACKAGE.is_empty(){return Err("安装器未内嵌正式程序包，请重新构建。".into());}
    let args=std::env::args().skip(1).collect::<Vec<_>>();let mut target=None;let mut report=None;let mut shortcut=true;let mut auto=false;let mut update_parent=None;
    if !args.is_empty(){
        if args.len()!=2{return Err("安装器参数数量无效。".into());}
        match args[0].as_str(){
            "--qualification-config"=>{
                if !cfg!(feature="qualification"){return Err("正式安装器不接受验收参数。".into());}
                let config=voice_desktop_common::resources::load(Path::new(&args[1]))?;
                let root=PathBuf::from(config["target_root"].as_str().ok_or("验收配置缺少工作区")?);
                let config_path=voice_desktop_common::canonical_path(Path::new(&args[1]))?;
                let sandbox=config_path.parent().ok_or("验收目录无效")?;
                workspace::Locator::local(&root).validate_target(&root)?;
                if !root.starts_with(sandbox)||root==sandbox{return Err("自动验收目标须位于配置目录的独立子目录内。".into());}
                target=Some(root);report=Some(sandbox.join("installer-result.json"));shortcut=config["shortcut"]!=false;auto=true;
            }
            "--app-update-config"=>{
                let (root,parent)=consume_update_config(Path::new(&args[1]))?;
                target=Some(root);update_parent=Some(parent);shortcut=false;auto=true;
            }
            _=>return Err("安装器不接受路径或产品运行参数。".into()),
        }
    }
    std::env::remove_var("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER");
    if voice_desktop_common::webview::version().is_none(){voice_desktop_common::webview::guide("未检测到系统 WebView2（网页运行引擎）。安装后重新运行安装器。");return Ok(());}
    let cache=std::env::temp_dir().join(format!("voice-initialization-installer-{}",std::process::id()));fs::create_dir_all(&cache).map_err(|e|e.to_string())?;
    let initial_phase=if update_parent.is_some(){"正在准备应用更新"}else{"请选择文件夹作为整个工作区"};
    let state=Arc::new(Installer{target:Mutex::new(target),status:Mutex::new(json!({"phase":initial_phase,"error":null,"can_cancel":false})),busy:AtomicBool::new(false),cancel:Arc::new(AtomicBool::new(false)),installed:AtomicBool::new(false),exiting:AtomicBool::new(false),shortcut,report,update_parent});
    let setup_state=state.clone();let cache_window=cache.clone();
    let app=tauri::Builder::default().manage(state.clone()).invoke_handler(tauri::generate_handler![desktop_status,desktop_initialize,desktop_cancel_init,exit_application])
        .setup(move|app|{let window=WebviewWindowBuilder::new(app,"startup",WebviewUrl::App("index.html".into())).initialization_script(voice_desktop_common::webview::DISABLE_CONTEXT_MENU).title("Voice Terminal · 安装语音终端").inner_size(1000.,640.).min_inner_size(400.,480.).incognito(true).data_directory(cache_window.clone()).on_navigation(|url| (url.scheme()=="tauri"&&url.host_str()==Some("localhost"))||(url.scheme()=="http"&&url.host_str()==Some("tauri.localhost")&&url.port().is_none())).build()?;app_icon::apply(window.hwnd()?.0).map_err(std::io::Error::other)?;
            if auto{let handle=app.handle().clone();let state=setup_state.clone();std::thread::spawn(move||{let _=run_install(&handle,state,false);});}Ok(())})
        .on_window_event(|window,event|{if let tauri::WindowEvent::CloseRequested{api,..}=event{let state=window.state::<Arc<Installer>>();api.prevent_close();exit(window.app_handle(),&state);}})
        .build(tauri::generate_context!()).map_err(|e|e.to_string())?;
    app.run(move|app,event|{if let tauri::RunEvent::ExitRequested{api,..}=event{if !state.exiting.load(Ordering::Acquire){api.prevent_exit();exit(app,&state);}}});
    for _ in 0..20{if workspace::remove_owned(&cache,cache.parent().unwrap()).is_ok(){break;}std::thread::sleep(std::time::Duration::from_millis(100));}Ok(())
}
fn main(){if let Err(error)=run(){voice_desktop_common::windows::error(&error);std::process::exit(1);}}

#[cfg(test)]
mod tests{
    use super::*;
    use std::os::windows::process::CommandExt;

    #[test]
    fn accepts_nonce_generated_by_released_desktop(){
        assert!(validate_update_nonce(&workspace::new_id(),std::process::id()).is_ok());
        assert!(validate_update_nonce("18c2c23ff4c00000-2a",42).is_ok());
        assert!(validate_update_nonce("123e4567-e89b-12d3-a456-426614174000",42).is_ok());
    }

    #[test]
    fn rejects_nonce_paths_malformed_values_and_different_parent(){
        for nonce in ["","-2a","0-2a","xyz-2a","18c2c23ff4c00000-2b","18c2c23ff4c00000-10000002a","../../other-2a","18c2c23ff4c00000-2a/other","------------------------------------"]{
            assert!(validate_update_nonce(nonce,42).is_err(),"应拒绝单次编号：{nonce}");
        }
    }

    #[test]
    fn accepts_exited_legacy_parent_but_rejects_reused_process_identity(){
        let parent=UpdateParent{pid:42,create_time:123,executable:PathBuf::from("C:/工作区/app/VoiceTerminalDesktop.exe")};
        assert!(validate_update_parent(&parent,None).is_ok());
        assert!(validate_update_parent(&parent,Some((123,parent.executable.clone()))).is_ok());
        assert!(validate_update_parent(&parent,Some((456,parent.executable.clone()))).is_err());
        assert!(validate_update_parent(&parent,Some((123,PathBuf::from("C:/其他程序.exe")))).is_err());
    }

    #[test]
    fn strict_parent_query_identifies_current_process(){
        let actual=update_parent_identity(std::process::id()).unwrap().unwrap();
        assert!(actual.0>0);
        assert_eq!(voice_desktop_common::canonical_path(&actual.1).unwrap(),voice_desktop_common::canonical_path(&std::env::current_exe().unwrap()).unwrap());
    }

    #[test]
    fn consumes_legacy_config_once_and_rejects_replay(){
        let root=std::env::temp_dir().join(format!("voice-update-consume-{}",workspace::new_id()));
        fs::create_dir_all(&root).unwrap();
        let nonce=workspace::new_id();let path=root.join(format!("app-update-{nonce}.json"));
        fs::write(&path,b"{}").unwrap();
        mark_update_consumed(&path,&nonce).unwrap();
        assert!(!path.exists());
        assert_eq!(fs::read_to_string(path.with_extension("json.consumed")).unwrap(),nonce);
        fs::write(&path,b"{}").unwrap();
        assert!(mark_update_consumed(&path,&nonce).is_err());
        assert!(path.exists());
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    #[ignore="轻量隔离验收：需要已组装的内嵌程序包，核验完整更新配置及真实旧进程、工作区锁释放"]
    fn complete_update_config_and_parent_release_are_verified_in_isolation(){
        // parent fixture（旧进程夹具）：同一测试二进制的子进程只等待，不运行安装界面。
        if std::env::var_os("VOICE_UPDATE_PARENT_FIXTURE").is_some(){std::thread::sleep(Duration::from_secs(120));return;}
        assert!(!PACKAGE.is_empty(),"需要明确指定已组装的程序包");
        let owner=voice_desktop_common::canonical_path(&std::env::temp_dir()).unwrap();
        let root=owner.join(format!("voice-update-handoff-{}",workspace::new_id()));workspace::initialize(&root).unwrap();
        fs::create_dir_all(root.join("app")).unwrap();let parent_exe=root.join("app/VoiceTerminalDesktop.exe");
        let current=std::env::current_exe().unwrap();fs::copy(&current,&parent_exe).unwrap();
        struct OldParent(std::process::Child);
        impl Drop for OldParent{fn drop(&mut self){let _=self.0.kill();let _=self.0.wait();}}
        let mut child=OldParent(std::process::Command::new(&parent_exe).args(["--exact","tests::complete_update_config_and_parent_release_are_verified_in_isolation","--ignored"])
            .env("VOICE_UPDATE_PARENT_FIXTURE","1").stdin(std::process::Stdio::null()).stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null()).creation_flags(0x08000000).spawn().unwrap());
        let (created,executable)=update_parent_identity(child.0.id()).unwrap().unwrap();
        let nonce="123e4567-e89b-12d3-a456-426614174000";let path=root.join(format!("tmp/app-update-{nonce}.json"));
        let version=installation::package_version(PACKAGE).unwrap();let portable=voice_desktop_common::resources::load(&root.join("portable.json")).unwrap();
        let mut record=json!({"schema":1,"nonce":nonce,"target_root":root,"workspace_id":portable["workspace_id"],"parent_pid":child.0.id(),"parent_create_time":created,"parent_executable":executable,
            "version":version,"tag":format!("v{version}"),"installer_size":fs::metadata(&current).unwrap().len(),"installer_sha256":workspace::file_hash(&current).unwrap(),"fields":{}});
        for key in ["schema","nonce","target_root","workspace_id","parent_pid","parent_create_time","parent_executable","version","tag","installer_size","installer_sha256"]{record["fields"][key]=json!("隔离交接验收字段");}
        for (field,value,reason) in [("workspace_id",json!("wrong-workspace"),"工作区身份"),("version",json!("999.0.0"),"版本不匹配"),
            ("tag",json!("v999.0.0"),"版本不匹配"),("installer_size",json!(1),"安装器不匹配"),("installer_sha256",json!("0".repeat(64)),"安装器不匹配"),
            ("parent_create_time",json!(created+1),"进程身份")]{
            let mut changed=record.clone();changed[field]=value;workspace::atomic_json(&path,&changed).unwrap();
            assert!(consume_update_config(&path).err().unwrap().contains(reason),"必须拒绝 {field}（被篡改字段）");
            assert!(path.exists());assert!(!path.with_extension("json.consumed").exists());
        }
        workspace::atomic_json(&path,&record).unwrap();let (accepted,parent)=consume_update_config(&path).unwrap();assert_eq!(accepted,root);
        assert!(!path.exists());assert!(path.with_extension("json.consumed").is_file());
        workspace::atomic_json(&path,&record).unwrap();assert!(consume_update_config(&path).err().unwrap().contains("已使用"));
        let held=installation::WorkspaceGuard::acquire(&root).unwrap();
        let release=std::thread::spawn(move||{std::thread::sleep(Duration::from_millis(200));child.0.kill().unwrap();child.0.wait().unwrap();std::thread::sleep(Duration::from_millis(400));drop(held);});
        let mut phases=vec![];wait_for_update_parent(&root,&parent,&mut|phase,_,_|phases.push(phase.to_owned())).unwrap();release.join().unwrap();
        assert!(phases.iter().any(|phase|phase.contains("旧程序正常退出")));assert!(phases.iter().any(|phase|phase.contains("工作区锁释放")));
        workspace::remove_owned(&root,&owner).unwrap();
    }
}
