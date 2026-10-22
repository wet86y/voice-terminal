//! Environment commands（环境命令）：原生配置语音服务、登记与检测显卡环境。
use std::{path::PathBuf,sync::Arc};
use serde_json::{Value,json};use tauri::{State,WebviewWindow};
use voice_desktop_common::{runtime,workspace::{atomic_json,safe_path},resources};
use super::{Desktop,ResourceManager};
fn allowed(window:&WebviewWindow,desktop:&Desktop)->Result<(),String>{super::trusted(window,desktop)?;super::require_command(desktop.mode,super::DesktopCommand::FocusMain)}
fn dependency_plan(context:&voice_desktop_common::LaunchContext,group:&str)->Result<Value,String>{
    if matches!(group,"audiocpp"|"tts-audiocpp-runtime"){
        let item=resources::spec(context,"tts-audiocpp-runtime")?;
        return Ok(json!({"resource_id":"tts-audiocpp-runtime","group":"audiocpp","required":false,"already_ready":resources::native_runtime_status(context)?["can_start"],"wheels":[],"system_candidates":[],"total_bytes":resources::download_bytes(&item),"source":item["source"],
            "fields":{"resource_id":"可选原生语音运行依赖编号","group":"依赖卡片类型","required":"不独立阻塞整体启动","already_ready":"当前原生运行依赖已就绪","wheels":"此依赖不使用解释器预编译包","system_candidates":"此依赖不复用系统解释器","total_bytes":"原生运行依赖最大下载字节数","source":"固定官方来源及随包补丁"}}));
    }
    runtime::plan(context,group)
}
#[tauri::command(rename_all="snake_case")]
pub async fn resource_install_plan(window:WebviewWindow,desktop:State<'_,Arc<Desktop>>,manager:State<'_,Arc<ResourceManager>>,resource_id:String)->Result<Value,String>{
    allowed(&window,&desktop)?;let context=manager.context();let source_id=resource_id.clone();
    tauri::async_runtime::spawn_blocking(move||{
        if matches!(resource_id.as_str(),"voice-server"|runtime::CPU_RESOURCE|runtime::GPU_RESOURCE){return Ok(json!({"resource_id":resource_id,"title":if resource_id==runtime::GPU_RESOURCE{"GPU（图形处理器）运行依赖"}else if resource_id==runtime::CPU_RESOURCE{"CPU（中央处理器）运行依赖"}else{"Voice Server（语音服务）运行环境"},"model_bytes":0,"environment":runtime::plan(&context,if resource_id==runtime::GPU_RESOURCE{"gpu"}else{"cpu"})?,
            "fields":{"resource_id":"固定资源编号","title":"资源名称","model_bytes":"模型文件下载字节数","environment":"完整语音运行环境安装计划"}}));}
        let spec=resources::spec(&context,&resource_id)?;
        let model_bytes=resources::download_bytes(&spec);
        let mut shared_resources=vec![];
        if let Some(shared)=spec["runtime_resource"].as_str().filter(|_| !matches!(resource_id.as_str(),"tts-voxcpm1"|"tts-voxcpm2")){
            let ready=resources::inventory(&context)?.iter().any(|row|row["resource_id"]==shared&&row["can_start"]==true);
            if !ready{let item=resources::spec(&context,shared)?;shared_resources.push(json!({"resource_id":shared,"title":item["title"],"download_bytes":resources::download_bytes(&item)}));}
        }
        let environment=if spec["dependency_group"].as_str().is_some(){runtime::plan(&context,"all")?}else{Value::Null};
        Ok(json!({"resource_id":resource_id,"title":spec["title"],"model_bytes":model_bytes,"environment":environment,"shared_resources":shared_resources,
            "fields":{"resource_id":"固定资源编号","title":"资源名称","model_bytes":"模型或组件最大下载字节数","shared_resources":"需要先安装的共用原生运行程序","download_bytes":"共用组件最大远端下载字节数","environment":"组件专用依赖组对应的完整共享运行环境更新计划；空值表示无需单独更新依赖"}}))
    }).await.map_err(|e|e.to_string()).and_then(|result|result.map(|mut plan:Value|{
        let sources=if matches!(source_id.as_str(),"voice-server"|runtime::CPU_RESOURCE|runtime::GPU_RESOURCE){
            let mut hosts=vec![];
            for url in std::iter::once(&plan["environment"]["python"]["url"]).chain(plan["environment"]["wheels"].as_array().into_iter().flatten().map(|row|&row["url"])){
                if let Some(url)=url.as_str().filter(|url|!url.is_empty()){for candidate in voice_desktop_common::download_sources::candidates(url){let host=voice_desktop_common::download_sources::host(&candidate).to_owned();if !hosts.contains(&host){hosts.push(host);}}}
            }hosts
        }else{resources::spec(&manager.context(),&source_id).map(|spec|voice_desktop_common::download_sources::plan_sources(&spec)).unwrap_or_default()};
        plan["download_sources"]=json!(sources);
        plan["fields"]["download_sources"]=json!("各类资源使用的镜像和原站域名，下载失败时按资源切换备用来源");plan
    }))
}
#[tauri::command]
pub async fn voice_environment_status(window:WebviewWindow,desktop:State<'_,Arc<Desktop>>,manager:State<'_,Arc<ResourceManager>>)->Result<Value,String>{
    allowed(&window,&desktop)?;let manager=manager.inner().clone();
    tauri::async_runtime::spawn_blocking(move||{
        let context=manager.context();let mut status=runtime::status(&context)?;
        status["jobs"]=json!(manager.snapshots());status["gpu"]=resources::load(&context.data.join("config/gpu-environment.json")).unwrap_or(Value::Null);
        if let Some(relative)=status["gpu"]["path"].as_str(){
            let root=safe_path(&context.install,relative)?;
            let unchanged=if status["gpu"]["validation_identity"].is_object(){runtime::validation_identity(&context.install,&root).is_ok_and(|identity|status["gpu"]["validation_identity"]==identity)}else{root.join("python.exe").is_file()};
            if !unchanged{if let Some(models)=status["gpu"]["models"].as_object_mut(){for capability in models.values_mut(){capability["gpu_available"]=json!(false);capability["reason"]=json!("环境或模型文件已变化，请重新检测。");}}}
        }
        Ok(status)
    }).await.map_err(|e|e.to_string())?
}
#[tauri::command(rename_all="snake_case")]
pub async fn voice_environment_plan(window:WebviewWindow,desktop:State<'_,Arc<Desktop>>,manager:State<'_,Arc<ResourceManager>>,group:String)->Result<Value,String>{
    allowed(&window,&desktop)?;let context=manager.context();tauri::async_runtime::spawn_blocking(move||dependency_plan(&context,&group)).await.map_err(|e|e.to_string())?
}
#[tauri::command(rename_all="snake_case")]
pub fn voice_environment_configure(window:WebviewWindow,desktop:State<Arc<Desktop>>,manager:State<Arc<ResourceManager>>,group:String)->Result<Value,String>{allowed(&window,&desktop)?;manager.inner().configure_runtime(group)}
#[tauri::command]
pub fn voice_environment_import(window:WebviewWindow,desktop:State<Arc<Desktop>>,manager:State<Arc<ResourceManager>>)->Result<Value,String>{allowed(&window,&desktop)?;manager.inner().import_runtime()}
#[tauri::command]
pub async fn gpu_environment_register(window:WebviewWindow,desktop:State<'_,Arc<Desktop>>,manager:State<'_,Arc<ResourceManager>>)->Result<Value,String>{
    allowed(&window,&desktop)?;let context=manager.context();
    tauri::async_runtime::spawn_blocking(move||{
        let selected=voice_desktop_common::native_ui::choose_folder("登记当前工作区 runtimes/python-gpu（显卡环境）文件夹")?.ok_or("已取消登记")?;
        let selected=voice_desktop_common::canonical_path(&selected)?;
        let root=voice_desktop_common::canonical_path(&context.install)?;
        let relative=selected.strip_prefix(&root).map_err(|_|"显卡环境必须保存在当前工作区内部")?.to_string_lossy().replace('\\',"/");
        if !["runtimes/python-gpu","runtimes/python"].contains(&relative.as_str()){return Err("登记目录必须是 runtimes/python-gpu（显卡环境）或现用兼容环境。".into());}
        safe_path(&root,&format!("{relative}/python.exe"))?;
        if !selected.join("python.exe").is_file(){return Err("登记目录缺少解释器".into());}
        let record=json!({"schema":1,"path":relative,"models":{},"fields":{"schema":"显卡登记格式","path":"工作区内相对目录","models":"逐模型实际推理检测结果","environment_fingerprint":"环境文件版本指纹","models_fingerprint":"模型文件版本指纹"}});
        atomic_json(&context.data.join("config/gpu-environment.json"),&record)?;Ok(record)
    }).await.map_err(|e|e.to_string())?
}
#[tauri::command]
pub fn gpu_environment_probe(window:WebviewWindow,desktop:State<Arc<Desktop>>,manager:State<Arc<ResourceManager>>)->Result<Value,String>{allowed(&window,&desktop)?;manager.inner().probe_gpu()}
#[tauri::command]
pub fn environment_guide(window:WebviewWindow,desktop:State<Arc<Desktop>>,manager:State<Arc<ResourceManager>>)->Result<(),String>{
    allowed(&window,&desktop)?;let context=manager.context();let path=context.data.join("docs/模型运行环境配置指南.md");
    if !path.is_file(){std::fs::create_dir_all(path.parent().unwrap()).map_err(|e|e.to_string())?;std::fs::copy(context.program.join("docs/模型运行环境配置指南.md"),&path).map_err(|e|e.to_string())?;}
    voice_desktop_common::windows::open(&PathBuf::from(path))
}
