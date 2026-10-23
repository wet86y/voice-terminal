//! Native resources（原生资源）：模型文件与 DSH 安装不需要 Python（后端解释器）。
use std::{fs, io::{Read, Write}, path::{Path, PathBuf}, sync::{mpsc,Arc,Mutex, atomic::{AtomicBool,AtomicU64,AtomicUsize, Ordering}}, process::{Command, Stdio},time::{Duration,Instant}};
use serde_json::{Value, json};
use sha2::{Digest, Sha256, Sha512};
use base64::Engine;
use crate::{LaunchContext, download::DownloadFile, workspace::{safe_path, file_hash, atomic_json, remove_owned}};
pub type Progress<'a> = dyn FnMut(&str,u64,u64) + 'a; // Progress（阶段与字节进度回调）。
pub type Activation<'a> = dyn FnMut(&Path,&str)->Result<(),String> + 'a; // Activation（启用回调）：候选目录与固定目标，宿主在此短暂停机并串行替换。

pub struct PendingActivation {
    relative:String, // relative（受管目标相对路径）。
    target:PathBuf, // target（已切换的新目录）。
    old:PathBuf, // old（尚未清理的旧目录备份）。
    journal:PathBuf, // journal（崩溃恢复记录）。
    original:bool, // original（切换前是否已有安装）。
    swapped:bool, // swapped（新目录是否已交换到固定目标）。
    committed:bool, // committed（宿主恢复就绪后是否已提交）。
    finished:bool, // finished（备份/记录是否已完成清理）。
}
impl PendingActivation {
    pub fn is_committed(&self)->bool{self.committed} // is_committed（资源替换是否已持久提交）。
}

pub fn load(path:&Path)->Result<Value,String>{
    let bytes=fs::read(path).map_err(|e|e.to_string())?;
    serde_json::from_slice(bytes.strip_prefix(&[0xef,0xbb,0xbf]).unwrap_or(&bytes)).map_err(|e|e.to_string())
}
/// audio_enhancements（音频增强设置）：与运行中 Python（解释器）共用同一持久化文件。
pub fn audio_enhancements(data:&Path,request:Option<&Value>)->Result<Value,String>{
    let path=data.join("config/product-settings.json");
    let mut settings=if path.exists(){load(&path)?}else{json!({})};
    if !settings.is_object(){return Err("产品设置文件必须是对象，请先修复配置。".into());}
    let mut legacy=true;
    if let Ok(text)=fs::read_to_string(data.join("config/local.yaml")){
        let mut in_section=false;
        for raw in text.lines(){
            let body=raw.split('#').next().unwrap_or("").trim();
            if body.is_empty(){continue;}
            let indentation=raw.chars().take_while(|c|c.is_whitespace()).count();
            if indentation==0{in_section=body=="smart_turn:";continue;}
            if in_section&&indentation==2{if let Some((key,value))=body.split_once(':'){
                if key.trim()=="sensevoice"{legacy=value.trim()!="false";}
            }}
        }
    }
    let mut selected=json!({"dual_pass":legacy,"emotion":legacy,"acoustic_events":legacy});
    for key in ["dual_pass","emotion","acoustic_events"]{
        if let Some(value)=settings["audio_enhancements"][key].as_bool(){selected[key]=json!(value);}
    }
    if let Some(update)=request{
        let object=update.as_object().ok_or("音频增强设置必须是对象。")?;
        for (key,value) in object{
            if !matches!(key.as_str(),"dual_pass"|"emotion"|"acoustic_events")||!value.is_boolean(){return Err("音频增强开关必须是有效的布尔字段。".into());}
            selected[key]=value.clone();
        }
        settings["audio_enhancements"]=selected.clone();
        settings["schema_version"]=json!(16);
        atomic_json(&path,&settings)?;
    }
    Ok(selected)
}
pub fn specs(context:&LaunchContext)->Result<Vec<Value>,String>{
    let mut items=load(&context.installed_program().join("config/resources/catalog.json"))?["resources"].as_array().ok_or("资源目录格式无效")?.clone();
    let settings=load(&context.data.join("config/product-settings.json")).unwrap_or(Value::Null);
    let enhancements=audio_enhancements(&context.data,None)?;
    let runtime=load(&context.install.join("runtimes/python/runtime.json")).unwrap_or(Value::Null);
    let registration=load(&context.data.join("config/gpu-environment.json")).unwrap_or(Value::Null);
    let device=settings["compute"]["devices"]["smart_turn"].as_str().unwrap_or("auto");
    let gpu_environment=runtime["compatible_gpu"]==true||runtime["profile"]=="cu128"||registration["models"]["smart_turn"]["gpu_available"]==true;
    for item in &mut items{
        let selected=match item["resource_id"].as_str(){Some("paraformer-offline")=>enhancements["dual_pass"]==true,Some("emotion2vec-plus-base")=>enhancements["emotion"]==true,Some("yamnet")=>enhancements["acoustic_events"]==true,_=>false};
        item["selected_enhancement"]=json!(selected);
        if item["kind"]=="dsh" && item["origin"]=="npm_package" {let target=crate::dsh::target(&context.installed_program())?;item["version"]=target["version"].clone();item["source"]["distribution"]=target;continue;}
        if item["kind"]!="model"{continue;}
        let installed=destination(context,item)?;let smart_turn=item["resource_id"]=="smart-turn";
        if let Some(files)=item["files"].as_array_mut(){for file in files{
            if file["optional"]!=true{continue;}
            let path=file["path"].as_str().ok_or("模型文件名称缺失")?;
            // 已有可选权重必须随更新保留；明确显卡选择或兼容显卡环境需要对应的显卡权重。
            let gpu_selected=smart_turn&&path.ends_with("-gpu.onnx")&&(device=="cuda"||(device!="cpu"&&gpu_environment));
            if gpu_selected||safe_path(&installed,path)?.is_file(){file["optional"]=json!(false);}
        }}
    }Ok(items)
}
pub fn spec(context:&LaunchContext,id:&str)->Result<Value,String>{specs(context)?.into_iter().find(|v|v["resource_id"]==id).ok_or("不支持的资源编号".into())}
/// download_bytes（最大远端下载字节）：随包补丁不算网络流量。
pub fn download_bytes(item:&Value)->u64{
    let transfer=&item["source"]["transfer"];
    if let Some(artifacts)=transfer["artifacts"].as_array(){return artifacts.iter().filter_map(|row|row["size_bytes"].as_u64()).sum();}
    transfer["artifact"]["size_bytes"].as_u64().unwrap_or_else(||item["files"].as_array().into_iter().flatten()
        .filter(|row|row["optional"]!=true&&!row["asset"].is_string()).filter_map(|row|row["size_bytes"].as_u64()).sum())
}
pub(crate) fn destination(context:&LaunchContext,spec:&Value)->Result<PathBuf,String>{
    let relative=spec["destination"].as_str().ok_or("资源缺少固定目录")?;
    safe_path(&context.install,&if spec["kind"]=="model"{format!("models/{relative}")}else{relative.into()})
}
fn file_spec(value:&Value,url:String)->Result<DownloadFile,String>{
    let download_variants=serde_json::from_value::<Vec<crate::download::DownloadVariant>>(value.get("download_variants").cloned().unwrap_or_else(||json!([])))
        .map_err(|_|"文件的等价下载版本配置无效")?;
    if download_variants.iter().any(|variant|variant.source_host.is_empty()||variant.source_host.contains('/')||variant.size==0||
        variant.sha256.len()!=64||!variant.sha256.bytes().all(|byte|byte.is_ascii_hexdigit())||variant.normalization!="remove_one_final_line_feed"){
        return Err("文件的等价下载版本配置无效".into());
    }
    Ok(DownloadFile{path:value["path"].as_str().or(value["filename"].as_str()).ok_or("文件缺少名称")?.into(),url,
        size:value["size_bytes"].as_u64().ok_or("文件缺少大小")?,sha256:value["sha256"].as_str().unwrap_or("").into(),
        sha512:value["sha512_base64"].as_str().unwrap_or("").into(),download_variants})
}
pub fn walk(root:&Path)->Result<Vec<PathBuf>,String>{
    fn visit(base:&Path,folder:&Path,out:&mut Vec<PathBuf>)->Result<(),String>{
        for entry in fs::read_dir(folder).map_err(|e|e.to_string())? {
            let entry=entry.map_err(|e|e.to_string())?;let path=entry.path();let metadata=entry.metadata().map_err(|e|e.to_string())?;
            let relative=path.strip_prefix(base).map_err(|e|e.to_string())?.to_string_lossy().replace('\\',"/");
            crate::workspace::relative_path(&relative)?;
            if crate::workspace::metadata_reparse(&metadata){return Err("组件包含目录联接或文件链接。".into());}
            if metadata.is_dir(){visit(base,&path,out)?;}else if metadata.is_file(){out.push(path);}else{return Err("组件包含非普通文件。".into());}
        }Ok(())
    }
    if crate::workspace::metadata_reparse(&fs::symlink_metadata(root).map_err(|e|e.to_string())?){return Err("组件根目录不能是链接。".into());}
    let mut out=Vec::new();visit(root,root,&mut out)?;out.sort();Ok(out)
}
pub fn manifest(root:&Path,kind:&str,build:&str)->Result<Value,String>{
    crate::workspace::manifest_with_progress(root,kind,build,&AtomicBool::new(false),&mut|_,_,_|{})
}
pub fn model_validate(context:&LaunchContext,spec:&Value,source:&Path)->Result<(),String>{
    validate_model_files(context,spec,source,false)
}
fn resource_asset(context:&LaunchContext,asset:&str)->Result<PathBuf,String>{
    let current=safe_path(&context.program.join("config/resources"),asset)?;
    if current.is_file(){return Ok(current);}
    // 兼容旧程序布局；两条路径仍经过下方同一锁定大小与 SHA-256 校验。
    safe_path(&context.program,asset)
}
fn validate_model_files(context:&LaunchContext,spec:&Value,source:&Path,fill_assets:bool)->Result<(),String>{
    validate_model_files_with_progress(context,spec,source,fill_assets,&AtomicBool::new(false),&mut|_,_,_|{})
}
fn validate_model_files_with_progress(context:&LaunchContext,spec:&Value,source:&Path,fill_assets:bool,cancel:&AtomicBool,progress:&mut Progress)->Result<(),String>{
    let mut records=vec![];
    for file in spec["files"].as_array().ok_or("模型文件清单无效")? {
        check_cancel(cancel)?;
        let name=file["path"].as_str().ok_or("缺少模型文件名称")?;
        let path=if fill_assets&&file["asset"].is_string(){resource_asset(context,file["asset"].as_str().unwrap())?}else{safe_path(source,name)?};
        if !path.is_file()&&file["optional"]==true{continue;}
        if !path.is_file(){return Err(format!("缺少文件：{name}"));}
        if path.metadata().map_err(|e|e.to_string())?.len()!=file["size_bytes"].as_u64().ok_or("锁定大小缺失")? {
            return Err(format!("版本、大小或摘要不匹配：{name}"));
        }
        if fill_assets&&file["asset"].is_string(){
            if crate::workspace::file_hash_with_progress(&path,cancel,progress)?!=file["sha256"].as_str().ok_or("锁定摘要缺失")?{return Err(format!("随包模型资产摘要不匹配：{name}"));}
        }else{records.push(json!({"path":name,"size":file["size_bytes"],"sha256":file["sha256"]}));}
    }
    crate::workspace::verify_records_with_progress(source,&records,false,cancel,progress)
}
const MODEL_RECEIPT: &str = "resource-install.json"; // MODEL_RECEIPT（模型安装收据文件名）。

#[derive(Clone)]
struct InstalledModel {
    version: Option<String>, // version（已安装版本）；旧根清单没有版本字段。
    files: Vec<Value>, // files（已安装文件的路径、大小、摘要和可选标记）。
}

fn valid_sha256(value:&str)->bool{value.len()==64&&value.bytes().all(|byte|byte.is_ascii_hexdigit())}

fn receipt_digest(resource_id:&str,files:&[Value])->Result<String,String>{
    let mut rows=files.to_vec();rows.sort_by(|a,b|a["path"].as_str().unwrap_or("").cmp(b["path"].as_str().unwrap_or("")));
    // Python json.dumps(sort_keys=True, ensure_ascii=False, separators=(",", ":"))（按键排序且紧凑编码）
    // 的收据摘要不能依赖 serde_json Map（JSON 对象映射）在不同构建特性下的迭代顺序。
    fn sort_object_keys(value:Value)->Value{
        match value{
            Value::Array(items)=>Value::Array(items.into_iter().map(sort_object_keys).collect()),
            Value::Object(items)=>{
                let sorted:std::collections::BTreeMap<String,Value>=items.into_iter().map(|(key,value)|(key,sort_object_keys(value))).collect();
                let mut result=serde_json::Map::new();for (key,value) in sorted{result.insert(key,value);}Value::Object(result)
            }
            other=>other,
        }
    }
    let encoded=serde_json::to_vec(&sort_object_keys(json!({"resource_id":resource_id,"files":rows}))).map_err(|e|e.to_string())?;
    use sha2::Sha256;let mut hash=Sha256::new();hash.update(encoded);Ok(format!("{:x}",hash.finalize()))
}

pub(crate) fn write_model_receipt(stage:&Path,spec:&Value,mut files:Vec<Value>)->Result<(),String>{
    let resource_id=spec["resource_id"].as_str().ok_or("模型编号缺失")?;
    let version=spec["version"].as_str().ok_or("模型版本缺失")?;
    // 文件记录来自复制时的源摘要及目标读回复核，不再为签发收据重读全部权重。
    files.sort_by(|a,b|a["path"].as_str().unwrap_or("").cmp(b["path"].as_str().unwrap_or("")));
    let digest=receipt_digest(resource_id,&files)?;
    atomic_json(&stage.join(MODEL_RECEIPT),&json!({"schema":1,"resource_id":resource_id,"version":version,
        "content_sha256":digest,"files":files,"fields":{"schema":"收据格式版本","resource_id":"模型资源编号","version":"已安装模型版本",
            "content_sha256":"已安装文件记录的 SHA-256（内容摘要）","files":"已安装模型文件","path":"模型目录内相对路径",
            "size":"文件字节数","sha256":"文件 SHA-256（摘要）","optional":"是否为可选文件","fields":"字段中文说明"}}))
}

/// read_model_receipt（读取安装收据）：格式、资源编号、内容摘要与文件记录均校验。
/// 组件收据是该组件已安装文件的权威记录；总清单只是某次签发时的快照，两者不一致时以收据为准。
pub(crate) fn read_model_receipt(component:&Path)->Result<(String,Option<String>,Vec<Value>),String>{
    let receipt=safe_path(component,MODEL_RECEIPT)?;
    if !receipt.is_file(){return Err("模型安装收据不存在。".into());}
    let value=load(&receipt)?;
    let resource_id=value["resource_id"].as_str().filter(|id|!id.is_empty()).ok_or("模型安装收据格式无效。")?.to_owned();
    if value["schema"]!=1||value["version"].as_str().is_none()||!value["files"].is_array(){return Err("模型安装收据格式无效。".into());}
    let files=value["files"].as_array().unwrap().clone();
    if value["content_sha256"].as_str()!=Some(receipt_digest(&resource_id,&files)?.as_str()){return Err("模型安装收据内容摘要不匹配。".into());}
    let mut seen=std::collections::HashSet::new();
    for row in &files{
        let relative=row["path"].as_str().ok_or("模型安装文件记录缺少路径")?;
        if relative==MODEL_RECEIPT||!seen.insert(relative.to_lowercase())||row["size"].as_u64().is_none()||
            !row["sha256"].as_str().is_some_and(valid_sha256)||(row.get("optional").is_some()&&row["optional"].as_bool().is_none()){
            return Err("模型安装文件记录无效或重复".into());
        }
        safe_path(component,relative)?;
    }
    Ok((resource_id,value["version"].as_str().map(str::to_owned),files))
}

fn inspect_installed_model(context:&LaunchContext,spec:&Value,verify_hashes:bool)->Result<InstalledModel,String>{
    let resource_id=spec["resource_id"].as_str().ok_or("模型编号缺失")?;
    let root=destination(context,spec)?;if !root.is_dir(){return Err("模型目录不存在".into());}
    let receipt=safe_path(&root,MODEL_RECEIPT)?;
    let (version,files)=if receipt.is_file(){
        let (installed_id,version,files)=read_model_receipt(&root)?;
        if installed_id!=resource_id{return Err("模型安装收据格式无效。".into());}
        (version,files)
    }else{
        let manifest=load(&context.install.join("models/manifest.json"))?;
        if manifest["schema"]!=1||manifest["kind"]!="models"||!manifest["files"].is_array(){return Err("模型安装清单格式无效".into());}
        let prefix=format!("{}/",spec["destination"].as_str().ok_or("模型目录缺失")?);
        let mut rows=vec![];
        for row in manifest["files"].as_array().unwrap(){
            let Some(path)=row["path"].as_str() else{continue};
            if let Some(relative)=path.strip_prefix(&prefix){if relative!=MODEL_RECEIPT{
                rows.push(json!({"path":relative,"size":row["size"],"sha256":row["sha256"],"optional":false}));
            }}
        }
        // legacy TTS（旧本地语音模型）迁入时没有更新总清单；只接受锁定目标记录，不信任未登记大小。
        if rows.is_empty()&&(resource_id=="tts-voxcpm1"||resource_id=="tts-voxcpm2"){
            for row in spec["files"].as_array().ok_or("本地语音文件清单缺失")?{
                rows.push(json!({"path":row["path"],"size":row["size_bytes"],"sha256":row["sha256"],"optional":row["optional"]==true}));
            }
        }
        (None,rows)
    };
    inspect_model_files(&root,&files,spec,verify_hashes)?;
    Ok(InstalledModel{version,files})
}

fn inspect_model_files(root:&Path,files:&[Value],spec:&Value,verify_hashes:bool)->Result<(),String>{
    if files.is_empty(){return Err("模型安装清单没有该组件的文件记录".into());}
    // 当前选择可以把旧收据中的可选权重提升为必需；存在性以本轮要求为准。
    let required=spec["files"].as_array().into_iter().flatten().filter(|row|row["optional"]!=true)
        .filter_map(|row|row["path"].as_str()).collect::<std::collections::HashSet<_>>();
    let mut seen=std::collections::HashSet::new();
    for row in files{
        let relative=row["path"].as_str().ok_or("模型安装文件记录缺少路径")?;
        if relative==MODEL_RECEIPT||!seen.insert(relative.to_lowercase())||row["size"].as_u64().is_none()||
            !row["sha256"].as_str().is_some_and(valid_sha256)||
            (row.get("optional").is_some()&&row["optional"].as_bool().is_none()) {return Err("模型安装文件记录无效或重复".into());}
        let path=safe_path(&root,relative)?;let optional=row["optional"]==true&&!required.contains(relative);
        if !path.is_file()&&optional{continue;}
        if !path.is_file()||path.metadata().map_err(|e|e.to_string())?.len()!=row["size"].as_u64().unwrap(){return Err(format!("已安装模型文件缺失或大小不符：{relative}"));}
        if verify_hashes&&file_hash(&path)?!=row["sha256"].as_str().unwrap(){return Err(format!("已安装模型文件摘要不符：{relative}"));}
    }
    Ok(())
}

/// model_receipt_current（静态组件复用）：读取既有收据、目标记录与文件大小，不重读权重内容。
pub(crate) fn model_receipt_current(component:&Path,spec:&Value)->bool{
    let Ok((id,version,files))=read_model_receipt(component)else{return false;};
    id==spec["resource_id"].as_str().unwrap_or("")
        &&!model_update_available(spec,&InstalledModel{version,files:files.clone()})
        &&inspect_model_files(component,&files,spec,false).is_ok()
}

/// current_for_upgrade（更新时复用）：仅核对当前目标及必要入口；完整内容验证由维修或实际写入负责。
pub(crate) fn current_for_upgrade(context:&LaunchContext,item:&Value)->bool{
    if item["kind"]=="model"{
        inspect_installed_model(context,item,false).is_ok_and(|installed|!model_update_available(item,&installed))
    }else if item["kind"]=="dsh"{
        destination(context,item).is_ok_and(|root|dsh_validate_with_progress(context,&root,false,&AtomicBool::new(false),&mut|_,_,_|{}).is_ok())
    }else{false}
}

fn installed_model_content_matches(spec:&Value,installed:&InstalledModel)->bool{
    let prior:std::collections::HashMap<&str,&Value>=installed.files.iter().filter_map(|row|row["path"].as_str().map(|path|(path,row))).collect();
    let mut target_paths=std::collections::HashSet::new();
    for item in spec["files"].as_array().into_iter().flatten(){
        let Some(path)=item["path"].as_str() else{return false};target_paths.insert(path);
        let Some(old)=prior.get(path) else{if item["optional"]==true{continue;}return false;};
        if old["size"].as_u64()!=item["size_bytes"].as_u64()||old["sha256"]!=item["sha256"]{return false;}
    }
    !installed.files.iter().any(|row|row["path"].as_str().is_some_and(|path|!target_paths.contains(path)))
}

fn model_update_available(spec:&Value,installed:&InstalledModel)->bool{
    if installed.version.as_deref().is_some_and(|version|Some(version)!=spec["version"].as_str()){return true;}
    !installed_model_content_matches(spec,installed)
}

/// native_runtime_status（可选原生运行依赖卡片）：只核对收据，不引入新的启动门槛。
pub fn native_runtime_status(context:&LaunchContext)->Result<Value,String>{
    let item=spec(context,"tts-audiocpp-runtime").ok();
    let inspected=item.as_ref().map(|item|inspect_installed_model(context,item,false));
    let installed=inspected.as_ref().is_some_and(|value|value.is_ok());
    let update=item.as_ref().zip(inspected.as_ref()).is_some_and(|(item,value)|value.as_ref().is_ok_and(|value|model_update_available(item,value)));
    let present=context.install.join("models/audiocpp/runtime").exists();
    Ok(json!({"resource_id":"tts-audiocpp-runtime","parent_resource_id":"voice-server","label":"audio.cpp（语音合成引擎）运行依赖","required":false,
        "installation_state":if installed{"installed"}else if present{"incomplete"}else{"missing"},"files_installed":installed,"runtime_ready":installed&&!update,"can_start":installed&&!update,
        "update_available":update,"version":item.as_ref().map(|value|value["version"].clone()),
        "error":inspected.and_then(Result::err),
        "fields":{"resource_id":"原生运行包资源编号","parent_resource_id":"所属语音服务组件","label":"依赖名称","required":"可选依赖，不独立阻止整体启动","installation_state":"运行包安装状态","files_installed":"运行包文件完整","runtime_ready":"运行包满足当前清单","can_start":"运行依赖已就绪","update_available":"运行包需要更新","version":"目标版本","error":"未就绪原因"}}))
}

pub fn inventory(context:&LaunchContext)->Result<Vec<Value>,String>{
    crate::compatibility::inventory(context)
}
pub(crate) fn inventory_raw(context:&LaunchContext)->Result<Vec<Value>,String>{
    let runtime=crate::runtime::status(context)?;let groups=runtime["groups"].clone();
    let ready=runtime["full_ready"]==true;
    let mut rows=vec![json!({"resource_id":"voice-server","label":"Voice Server（语音服务）","installation_state":if runtime["interpreter_ready"]==true{"installed"}else{"missing"},
        "required":true,"files_installed":runtime["interpreter_ready"],"runtime_ready":ready,"can_start":ready,"runtime_group":"all","python":runtime["python"],"version":runtime["version"],
        "runtime_components":runtime["components"],"update_available":runtime["update_available"],"source_url":"https://www.python.org/downloads/release/python-31210/","error":if ready{Value::Null}else if runtime["update_available"]==true{json!("语音运行依赖有更新，请更新完整运行环境后再启动。")}else{json!("请下载或导入完整 Python（后端运行环境），程序将一次检测并补齐全部语音依赖。")},
        "fields":{"files_installed":"私有解释器已安装","runtime_ready":"全部必需语音依赖已按当前锁定版本就绪","can_start":"具备启动条件且没有待更新必需组件","runtime_components":"语音服务下的中央处理器、原生语音与显卡运行依赖卡片","runtime_group":"完整语音依赖","python":"私有解释器路径","version":"解释器版本","update_available":"运行环境依赖锁版本可更新"}})];
    let catalog=specs(context)?;
    for item in &catalog {
        let root=destination(context,&item)?;
        // 先以收据/旧总清单的字节数快检；只有目标文件记录变化时才复核旧文件 SHA-256（摘要）。
        let (result,installed_version,update_available)=if item["kind"]=="model"{
            match inspect_installed_model(context,&item,false){
                Ok(installed)=>{
                    let update=model_update_available(&item,&installed);(Ok(()),installed.version,update)
                }
                Err(error)=>(Err(error),None,false)
            }
        }else{
            match inspect_dsh_installed(context,&root,false){
                Ok(metadata)=>{
                    let current=if item["origin"]=="npm_package"{crate::dsh::current(&metadata,&crate::dsh::target(&context.installed_program())?)}else{
                        let target=item["source"]["transfer"]["artifact"]["sha512_base64"].as_str().and_then(|value|base64::engine::general_purpose::STANDARD.decode(value).ok()).map(|bytes|bytes.iter().map(|byte|format!("{byte:02x}")).collect::<String>());
                        metadata["source_kind"]=="official_desktop"&&metadata["version"]==item["version"]&&target.as_deref()==metadata["source_sha512"].as_str()
                    };
                    (Ok(()),metadata["version"].as_str().map(str::to_owned),!current)
                }
                Err(error)=>{
                    let metadata=load(&root.join("runtime.json")).unwrap_or(Value::Null);
                    let version=metadata["version"].as_str().map(str::to_owned);
                    let update=metadata.is_object()&&item["origin"]=="npm_package"&&!crate::dsh::current(&metadata,&crate::dsh::target(&context.installed_program())?);
                    (Err(error),version,update)
                }
            }
        };
        let group=item["dependency_group"].as_str().unwrap_or_else(||crate::runtime::model_group(item["resource_id"].as_str().unwrap_or("")));
        let node_status=if item["kind"]=="dsh"&&item["origin"]=="npm_package"{crate::dsh::discover(&crate::dsh::target(&context.installed_program())?,false).map(|_|())}else{Ok(())};
        let native_tts=item["resource_id"].as_str().is_some_and(|id|id.starts_with("tts-"));
        let dependencies=if native_tts{true}else if item["kind"]=="dsh"{node_status.is_ok()}else{groups[group]["ready"]==true};
        let can_start=result.is_ok()&&dependencies&&!update_available;
        let error=result.as_ref().err().cloned().or_else(||node_status.err()).or_else(||if update_available{Some("该组件的版本或适配要求有更新，请先完成更新。".into())}else{None});
        rows.push(json!({"resource_id":item["resource_id"],"label":item["title"],"installation_state":if result.is_ok(){"installed"}else if root.exists(){"incomplete"}else{"missing"},
            "required":item["required"]!=false,"files_installed":result.is_ok(),"runtime_ready":dependencies,"can_start":can_start,
            "source_url":if item["kind"]=="dsh"&&!dependencies&&!crate::dsh::node_installed(){crate::download_sources::policy()["node_download_page"].clone()}else{item["source"]["page_url"].clone()},"error":error,"runtime_group":group,
            "version":item["version"],"installed_version":installed_version,"update_available":update_available,
            "fields":{"required":"是否为阻塞启动的必需组件","files_installed":"模型或组件文件已安装","runtime_ready":"共享运行依赖已就绪","can_start":"具备启动条件","runtime_group":"启动所需共享依赖组",
                "version":"随程序提供的目标模型或组件版本","installed_version":"实际安装的模型或组件版本；旧清单未知时为空","update_available":"目标模型或组件内容、版本可更新"}}));
    }
    if catalog.iter().any(|item|item["runtime_resource"]=="tts-audiocpp-runtime"){
        rows.push(json!({"resource_id":"tts","label":"TTS（语音合成）","required":true}));
        update_tts_readiness(context,&mut rows)?;
    }
    let updates_pending=rows.iter().any(|row|row["required"]!=false&&row["update_available"]==true);
    let other_components_ready=rows.iter().skip(1).filter(|row|row["required"]!=false).all(|row|row["can_start"]==true);
    if let Some(server)=rows.first_mut(){
        server["can_start"]=json!(ready&&!updates_pending&&other_components_ready);
        server["updates_pending"]=json!(updates_pending);
        if server["can_start"]!=true&&server["error"].is_null(){
            server["error"]=json!("有组件尚未就绪或需要更新，请完成组件更新后再启动。");
        }
        if !server["fields"].is_object(){server["fields"]=json!({});}
        server["fields"]["updates_pending"]=json!("是否仍有需要完成的组件更新");
    }
    Ok(rows)
}
/// update_tts_readiness（语音合成目标门禁）：按已选后端与具体模型检查。
pub fn update_tts_readiness(context:&LaunchContext,rows:&mut [Value])->Result<(),String>{
    if !rows.iter().any(|row|row["resource_id"]=="tts"){return Ok(());}
    let runtime=rows.iter().any(|row|row["resource_id"]=="tts-audiocpp-runtime"&&row["can_start"]==true)
        || legacy_tts_runtime_ready(context)?;
    for row in rows.iter_mut().filter(|row|matches!(row["resource_id"].as_str(),Some("tts-voxcpm1"|"tts-voxcpm2"))){
        row["runtime_ready"]=json!(runtime);row["can_start"]=json!(runtime&&row["files_installed"]==true&&row["update_available"]!=true);
    }
    let local=runtime&&rows.iter().any(|row|matches!(row["resource_id"].as_str(),Some("tts-voxcpm1"|"tts-voxcpm2"))&&row["can_start"]==true);
    let selection=crate::credentials::configured_tts_selection(&context.data);
    let selected_id=format!("tts-{}",selection["local_model"].as_str().unwrap_or("voxcpm2"));
    let selected_local=runtime&&rows.iter().any(|row|row["resource_id"]==selected_id&&row["can_start"]==true);
    let credentials=crate::credentials::status(&context.data)?;
    let online=credentials["providers"]["minimax"]["configured"]==true;
    let ready=if selection["backend"]=="voxcpm1"{selected_local}else{online};
    if let Some(row)=rows.iter_mut().find(|row|row["resource_id"]=="tts"){
        *row=json!({"resource_id":"tts","label":"TTS（语音合成）","required":true,"installation_state":if ready{"installed"}else{"missing"},
            "files_installed":local,"runtime_ready":ready,"can_start":ready,"local_ready":local,"selected_local_ready":selected_local,"online_ready":online,"selection":selection,
            "error":if ready{Value::Null}else{json!(if selection["backend"]=="voxcpm1"{"所选本地语音模型及共用运行程序未就绪，请在组件管理补齐。"}else{"所选在线语音接口未配置，请填写密钥或显式选择已就绪的本地模型。"})},
            "fields":{"resource_id":"固定组件编号","label":"组件名称","required":"是否参与整体启动门槛","installation_state":"任选条件是否已满足",
                "files_installed":"本地运行程序及至少一款模型完整","runtime_ready":"所选语音后端可用","can_start":"所选语音合成目标可以启动",
                "selected_local_ready":"所选本地模型及框架可用","selection":"持久化选择的语音后端与本地模型","local_ready":"至少一款本地模型及共用运行程序可用","online_ready":"在线语音接口密钥已配置","error":"所选目标尚未就绪的原因"}});
    }Ok(())
}
/// legacy_tts_runtime_ready（旧原生安装兼容）：仅认可随包补丁对应的程序与模型规格摘要。
fn legacy_tts_runtime_ready(context:&LaunchContext)->Result<bool,String>{
    if context.install.join("models/audiocpp/runtime/cuda/audiocpp_server.exe").is_file(){return Ok(false);}
    let Ok(item)=spec(context,"tts-audiocpp-runtime")else{return Ok(false)};
    if item["files"].as_array().is_none_or(|rows|rows.iter().filter(|row|row["archive_id"]=="patch").count()<3){return Ok(false);}
    for row in item["files"].as_array().into_iter().flatten().filter(|row|row["archive_id"]=="patch"){
        let Some(name)=row["path"].as_str()else{return Ok(false)};
        let path=if name=="cuda/audiocpp_server.exe"{context.install.join("runtimes/audiocpp").join(name)}
            else{context.install.join("sources/audio.cpp").join(name)};
        if !path.is_file()||path.metadata().map_err(|e|e.to_string())?.len()!=row["size_bytes"].as_u64().unwrap_or(0){return Ok(false);}
    }
    // 旧自编译程序静态链接 CUDA runtime（计算运行时），其必需运行库与官方归档的动态布局不同。
    Ok(["ggml.dll","ggml-base.dll","ggml-cuda.dll","cublas64_13.dll","cublasLt64_13.dll"].iter()
        .all(|name|context.install.join("runtimes/audiocpp/cuda").join(name).is_file()))
}
/// startup_readiness（启动组件检查）：只读取本地清单与文件，不启动解释器、服务或下载。
pub fn startup_readiness(context:&LaunchContext)->Result<Value,String>{
    startup_readiness_with_progress(context,&AtomicBool::new(false),&mut|_,_,_|{})
}
/// startup_readiness_with_progress（启动校验进度）：只盘点必要入口及兼容状态，不读取大组件完整内容。
pub fn startup_readiness_with_progress(context:&LaunchContext,cancel:&AtomicBool,progress:&mut Progress)->Result<Value,String>{
    check_cancel(cancel)?;progress("检查必要入口与兼容状态",0,0);
    let rows=inventory(context)?;check_cancel(cancel)?;
    let mut result=readiness_from_inventory(&rows);
    result["integrity_notice"]=json!(if result["ready"]==true{Value::Null}else{json!("检测到组件问题，可在应用维护中下载当前版本校验程序并校验完整性。")});
    Ok(result)
}

/// prepare_maintenance_verification（单轮维护计划）：大组件内容汇总到统一并行池，后续只修复异常项。
pub(crate) fn prepare_maintenance_verification(context:&LaunchContext,cancel:&AtomicBool,progress:&mut Progress)->Result<(),String>{
    let mut plans=Vec::new();let environment=context.install.join("runtimes/python");
    if let Ok(manifest)=load(&environment.join("manifest.json")){if let Some(records)=manifest["files"].as_array(){
        plans.push(crate::verification::Batch{root:environment,records:records.clone(),exact:true,python:manifest["kind"]=="python"});
    }}
    for item in specs(context).unwrap_or_default(){
        let root=destination(context,&item)?;if !root.is_dir(){continue;}
        if item["kind"]=="model"{
            let records=item["files"].as_array().ok_or("模型文件清单缺失")?.iter()
                .filter(|file|file["optional"]!=true||root.join(file["path"].as_str().unwrap_or("")).is_file())
                .map(|file|json!({"path":file["path"],"size":file["size_bytes"],"sha256":file["sha256"]})).collect();
            plans.push(crate::verification::Batch{root,records,exact:false,python:false});
        }else if let Ok(manifest)=load(&root.join("manifest.json")){if let Some(records)=manifest["files"].as_array(){
            plans.push(crate::verification::Batch{root,records:records.clone(),exact:true,python:false});
        }}
    }
    crate::verification::check(plans,cancel,progress)
}
#[cfg(test)] // 旧启动算法仅供回归对照，正式启动不再执行。
pub(crate) fn validate_inventory_hashes(context:&LaunchContext,rows:&mut [Value],cancel:&AtomicBool,progress:&mut Progress)->Result<(),String>{
    let environment=context.install.join("runtimes/python");
    let runtime_budget:Result<(u64,u64),String>=(||{
        let manifest=load(&environment.join("manifest.json"))?;
        let records=manifest["files"].as_array().ok_or("组件清单缺少文件集合")?;
        let total=records.iter().try_fold(0u64,|sum,file|sum.checked_add(file["size"].as_u64().ok_or("清单大小无效")?).ok_or("清单大小溢出"))?;
        Ok((total,records.len() as u64))
    })();
    let (runtime_total,runtime_files)=match runtime_budget{
        Ok(value)=>value,
        Err(error)=>{if let Some(server)=rows.first_mut(){server["can_start"]=json!(false);server["installation_state"]=json!("incomplete");server["error"]=json!(format!("语音环境文件清单无效：{error}"));}return Ok(());}
    };

    let mut plans=Vec::new();
    for index in 1..rows.len(){
        let row=&rows[index];
        let local_tts=row["resource_id"].as_str().is_some_and(|id|id.starts_with("tts-"));
        if (row["required"]==false&&!local_tts)||row["files_installed"]!=true||row["resource_id"]=="tts"{continue;}
        let Some(id)=row["resource_id"].as_str()else{continue;};
        let label=row["label"].as_str().unwrap_or(id).to_owned();
        let budget:Result<u64,String>=(||{
            let item=spec(context,id)?;let root=destination(context,&item)?;
            let records=if item["kind"]=="model"{
                inspect_installed_model(context,&item,false)?.files.into_iter()
                    .filter(|file|file["optional"]!=true||root.join(file["path"].as_str().unwrap_or("")).is_file())
                    .collect::<Vec<_>>()
            }else{
                load(&root.join("manifest.json"))?["files"].as_array().ok_or("组件清单缺少文件集合")?.clone()
            };
            Ok(records.iter().try_fold(0u64,|sum,file|sum.checked_add(file["size"].as_u64().ok_or("清单大小无效")?).ok_or("清单大小溢出"))?)
        })();
        match budget{
            Ok(total)=>plans.push((index,id.to_owned(),label,total)),
            Err(error)=>{
                let row=&mut rows[index];row["files_installed"]=json!(false);row["installation_state"]=json!("incomplete");row["can_start"]=json!(false);
                row["error"]=json!(format!("无法准备组件摘要复核：{error}"));
            }
        }
    }
    let total=plans.iter().try_fold(runtime_total,|sum,(_,_,_,size)|sum.checked_add(*size).ok_or("组件校验总大小溢出"))?;
    let result=crate::workspace::verify_tree_sizes_with_progress(&environment,"manifest.json",cancel,&mut|_,done,files|{
        let checked=if runtime_files==0||files==0{0}else{((runtime_total as u128)*(done as u128)/(files as u128)) as u64};
        progress("正在校验：Voice Server（语音服务）",checked.min(runtime_total),total);
    });
    check_cancel(cancel)?;
    if let Err(error)=result{
        if let Some(server)=rows.first_mut(){server["can_start"]=json!(false);server["installation_state"]=json!("incomplete");server["error"]=json!(format!("语音环境文件清单或大小校验失败：{error}"));}
        return Ok(());
    }
    if runtime_total>0{progress("正在校验：Voice Server（语音服务）",runtime_total,total);}
    let mut completed=runtime_total;
    for (index,id,label,component_total) in plans{
        check_cancel(cancel)?;
        progress(&format!("正在校验：{label}"),completed,total);
        let result=(||{
            let item=spec(context,&id)?;let root=destination(context,&item)?;
            let mut report=|_:&str,done:u64,_:u64|progress(&format!("正在校验：{label}"),completed.saturating_add(done.min(component_total)).min(total),total);
            if item["kind"]=="model"{
                let installed=inspect_installed_model(context,&item,false)?;
                let files=installed.files.into_iter().filter(|file|file["optional"]!=true||root.join(file["path"].as_str().unwrap()).is_file()).collect::<Vec<_>>();
                crate::workspace::verify_records_with_progress(&root,&files,false,cancel,&mut report)
            }else{
                let cache=safe_path(&context.install,"cache/component-validation/dsh.json")?;
                crate::workspace::verify_cached_tree_with_progress(&root,"manifest.json",&cache,cancel,&mut report)
            }
        })();
        check_cancel(cancel)?;
        if let Err(error)=result{
            let row=&mut rows[index];row["files_installed"]=json!(false);row["installation_state"]=json!("incomplete");row["can_start"]=json!(false);
            row["error"]=json!(format!("已安装组件完整摘要复核失败：{error}"));
        }
        completed=completed.saturating_add(component_total).min(total);
    }
    if total>0{progress("组件校验完成",total,total);}
    update_tts_readiness(context,rows)?;
    if rows.iter().skip(1).any(|row|row["required"]!=false&&row["can_start"]!=true){
        if let Some(server)=rows.first_mut(){server["can_start"]=json!(false);server["error"]=json!("有组件文件损坏、尚未就绪或需要更新，请完成修复后再启动。");}
    }
    Ok(())
}
fn readiness_from_inventory(rows:&[Value])->Value{
    let missing=rows.iter().filter(|row|row["required"]!=false&&row["can_start"]!=true).map(|row|json!({
        "resource_id":row["resource_id"],"label":row["label"],
        "reason":row["error"].as_str().unwrap_or(if row["update_available"]==true{"组件有更新，需先完成更新"}else{"组件文件或共享运行依赖尚未完备"})
    })).collect::<Vec<_>>();
    let invalid=rows.iter().filter(|row|row["error"].as_str().is_some_and(|error|error.contains("完整摘要复核失败")))
        .map(|row|json!({"resource_id":row["resource_id"],"reason":row["error"]})).collect::<Vec<_>>();
    json!({"ready":!rows.is_empty()&&missing.is_empty(),"missing":missing,"invalid_resources":invalid,"checked_components":rows.len(),
        "fields":{"ready":"全部组件具备启动条件","missing":"待用户手动补齐的组件","invalid_resources":"含可选资源在内的完整摘要失败记录","checked_components":"已检查组件数量","resource_id":"组件编号","label":"组件名称","reason":"缺失原因"}})
}
pub fn unpack_zip(source:&Path,target:&Path,cancel:&AtomicBool,progress:&mut Progress)->Result<(),String>{
    let mut archive=zip::ZipArchive::new(fs::File::open(source).map_err(|e|e.to_string())?).map_err(|e|e.to_string())?;
    let mut total=0u64;let mut names=std::collections::HashSet::new();
    for index in 0..archive.len(){let file=archive.by_index(index).map_err(|e|e.to_string())?;
        safe_path(target,file.name().trim_end_matches('/'))?;
        if !names.insert(file.name().to_lowercase())||file.unix_mode().is_some_and(|v|v&0o170000==0o120000){return Err("归档包含链接或重复文件".into());}
        total=total.checked_add(file.size()).ok_or("归档大小溢出")?;
        if total>64*1024*1024*1024{return Err("归档展开超过 64 GiB（吉二进制字节）上限".into());}
    }
    fs::create_dir_all(target).map_err(|e|e.to_string())?;
    if crate::workspace::available_space(target)?<total{return Err("解包空间不足".into());}
    let mut done=0;let mut buffer=vec![0;65536];
    for index in 0..archive.len(){let mut file=archive.by_index(index).map_err(|e|e.to_string())?;let path=safe_path(target,file.name().trim_end_matches('/'))?;
        if file.is_dir(){fs::create_dir_all(path).map_err(|e|e.to_string())?;continue;}
        fs::create_dir_all(path.parent().unwrap()).map_err(|e|e.to_string())?;let mut output=fs::File::create(path).map_err(|e|e.to_string())?;
        loop {check_cancel(cancel)?;let count=file.read(&mut buffer).map_err(|e|e.to_string())?;if count==0{break;}
            output.write_all(&buffer[..count]).map_err(|e|e.to_string())?;done+=count as u64;progress("正在解包",done,total);}
    }Ok(())
}
pub fn check_cancel(cancel:&AtomicBool)->Result<(),String>{if cancel.load(Ordering::Acquire){Err("操作已取消；下载缓存保留。".into())}else{Ok(())}}
pub fn copy_file(source:&Path,target:&Path,cancel:&AtomicBool,progress:&mut Progress)->Result<(),String>{
    copy_file_buffered(source,target,cancel,&AtomicBool::new(false),None,&mut vec![0;1024*1024],progress).map(|_|())
}

fn copy_file_buffered(source:&Path,target:&Path,cancel:&AtomicBool,stop:&AtomicBool,expected:Option<&str>,buffer:&mut[u8],progress:&mut Progress)->Result<String,String>{
    check_cancel(cancel)?;
    fs::create_dir_all(target.parent().ok_or("复制目标缺少父目录")?).map_err(|e|e.to_string())?;
    let mut options=fs::OpenOptions::new();options.read(true);
    #[cfg(windows)] {use std::os::windows::fs::OpenOptionsExt;options.custom_flags(0x00200000).share_mode(1);} // 读取期间不允许修改源文件或跟随链接。
    let mut input=options.open(source).map_err(|e|e.to_string())?;let metadata=input.metadata().map_err(|e|e.to_string())?;
    if !metadata.is_file()||crate::workspace::metadata_reparse(&metadata){return Err("复制源必须是普通文件。".into());}
    let total=metadata.len().checked_mul(2).ok_or("复制大小溢出")?;let mut done=0u64;
    let mut output=fs::File::create(target).map_err(|e|e.to_string())?;let mut source_hash=Sha256::new();let mut last_report=Instant::now();
    progress("正在复制并校验",0,total);
    loop {check_cancel(cancel)?;if stop.load(Ordering::Acquire){return Err("另一复制文件失败，候选组件保持未启用。".into());}
        let count=input.read(buffer).map_err(|e|e.to_string())?;if count==0{break;}
        output.write_all(&buffer[..count]).map_err(|e|e.to_string())?;source_hash.update(&buffer[..count]);done+=count as u64;
        if last_report.elapsed()>=Duration::from_millis(100){progress("正在复制并校验",done,total);last_report=Instant::now();}}
    if done!=metadata.len(){return Err("复制期间源文件大小发生变化。".into());}
    output.sync_all().map_err(|e|e.to_string())?;drop(output);
    let digest=format!("{:x}",source_hash.finalize());
    if expected.is_some_and(|expected|expected!=digest){return Err("复制源文件与固定摘要不符。".into());}
    let mut output=options.open(target).map_err(|e|e.to_string())?;
    let metadata=output.metadata().map_err(|e|e.to_string())?;
    if !metadata.is_file()||crate::workspace::metadata_reparse(&metadata)||metadata.len()!=total/2{return Err("复制目标文件大小或类型不符。".into());}
    let mut target_hash=Sha256::new();
    loop {check_cancel(cancel)?;if stop.load(Ordering::Acquire){return Err("另一复制文件失败，候选组件保持未启用。".into());}
        let count=output.read(buffer).map_err(|e|e.to_string())?;if count==0{break;}target_hash.update(&buffer[..count]);done+=count as u64;
        if last_report.elapsed()>=Duration::from_millis(100){progress("正在复制并校验",done,total);last_report=Instant::now();}}
    if done!=total||format!("{:x}",target_hash.finalize())!=digest{return Err("复制校验失败".into());}
    progress("正在复制并校验",total,total);check_cancel(cancel)?;Ok(digest)
}
pub fn copy_verified(source:&Path,target:&Path,cancel:&AtomicBool,progress:&mut Progress)->Result<(),String>{
    check_cancel(cancel)?;progress("扫描待复制组件文件",0,0);
    let files=walk(source)?;copy_selected_verified(source,target,files,cancel,progress)
}
/// copy_selected_verified（复制固定文件集合）：解释器筛选与组件完整复制共用相同目标摘要复核。
pub(crate) fn copy_selected_verified(source:&Path,target:&Path,files:Vec<PathBuf>,cancel:&AtomicBool,progress:&mut Progress)->Result<(),String>{
    let mut tasks=vec![];let mut total=0u64;
    for file in files{check_cancel(cancel)?;let size=file.metadata().map_err(|e|e.to_string())?.len();
        total=total.checked_add(size.checked_mul(2).ok_or("复制大小溢出")?).ok_or("复制大小溢出")?;
        let relative=file.strip_prefix(source).unwrap().to_string_lossy().replace('\\',"/");tasks.push((file,safe_path(target,&relative)?));}
    let next=AtomicUsize::new(0);let done=AtomicU64::new(0);let stop=AtomicBool::new(false);let failure=Mutex::new(None::<String>);
    let count=tasks.len().min(4);let(sender,receiver)=mpsc::channel();progress("正在复制并校验",0,total);
    std::thread::scope(|scope|{
        let mut workers=vec![];
        for _ in 0..count{let sender=sender.clone();let tasks=&tasks;let next=&next;let done=&done;let stop=&stop;let failure=&failure;
            workers.push(scope.spawn(move||{
                let result=std::panic::catch_unwind(std::panic::AssertUnwindSafe(||{
                    let mut buffer=vec![0;1024*1024];
                    loop{check_cancel(cancel)?;if stop.load(Ordering::Acquire){break;}
                        let index=next.fetch_add(1,Ordering::Relaxed);let Some((input,output))=tasks.get(index)else{break;};let mut reported=0;
                        copy_file_buffered(input,output,cancel,stop,None,&mut buffer,&mut|_,bytes,_|{done.fetch_add(bytes.saturating_sub(reported),Ordering::Relaxed);reported=bytes;})?;
                    }Ok::<(),String>(())
                })).unwrap_or_else(|_|Err("组件复制工作线程异常退出。".into()));
                if let Err(error)=result{let mut failure=failure.lock().unwrap();if failure.is_none(){*failure=Some(error);}stop.store(true,Ordering::Release);}let _=sender.send(());
            }));
        }
        drop(sender);let mut finished=0;let mut last_report=Instant::now();
        while finished<count{match receiver.recv_timeout(Duration::from_millis(100)){Ok(())=>finished+=1,Err(mpsc::RecvTimeoutError::Timeout)=>{},Err(mpsc::RecvTimeoutError::Disconnected)=>break}
            if last_report.elapsed()>=Duration::from_millis(100){progress("正在复制并校验",done.load(Ordering::Relaxed).min(total),total);last_report=Instant::now();}}
        for worker in workers{if worker.join().is_err(){*failure.lock().unwrap()=Some("组件复制工作线程异常退出。".into());}}
    });
    check_cancel(cancel)?;if let Some(error)=failure.into_inner().map_err(|_|"复制状态不可用。")?{return Err(error);}
    progress("复制与摘要复核完成",total,total);check_cancel(cancel)?;Ok(())
}
fn refresh_target_binding(context:&LaunchContext,relative:&str,target:&Path)->Result<(),String>{
    if relative.starts_with("models/"){refresh_models(context,Some(relative))}
    else if relative=="runtimes/dsh"{bind_dsh(context)}
    else if relative=="runtimes/python"{bind_component(context,"python","runtimes/python",target)}
    else{Err("安装记录目标不在受管资源范围".into())}
}

// recover（事务恢复）：未提交替换回滚；只有明确写入 committed（已提交）后才保留新目录。
pub fn recover(context:&LaunchContext)->Result<(),String>{
    let journal=context.install.join("tmp/native-install.json");if !journal.is_file(){return Ok(());}
    let record=load(&journal)?;let relative=record["target"].as_str().ok_or("安装记录缺少目标")?;
    if !(relative.starts_with("models/")||["runtimes/python","runtimes/dsh"].contains(&relative)){return Err("安装记录目标不在受管资源范围".into());}
    let target=safe_path(&context.install,relative)?;let old=safe_path(&context.install,"tmp/native-old")?;
    let committed=record["state"]=="committed";
    if !committed {
        if old.exists(){remove_owned(&target,&context.install)?;fs::create_dir_all(target.parent().unwrap()).map_err(|e|e.to_string())?;fs::rename(&old,&target).map_err(|e|e.to_string())?;}
        else if record["original"]==false{remove_owned(&target,&context.install)?;}
        if record["state"]=="swapped"{refresh_target_binding(context,relative,&target)?;}
    }else{refresh_target_binding(context,relative,&target)?;}
    remove_owned(&old,&context.install)?;fs::remove_file(journal).map_err(|e|e.to_string())?;Ok(())
}

pub fn begin_activation(context:&LaunchContext,stage:&Path,relative:&str)->Result<PendingActivation,String>{
    let dsh_proof=if relative=="runtimes/dsh"{load(&stage.with_extension("verified.json")).ok().filter(|record|record["root"].as_str().is_some_and(|path|Path::new(path)==stage))}else{None};
    recover(context)?;let target=safe_path(&context.install,relative)?;let old=safe_path(&context.install,"tmp/native-old")?;
    crate::verification::invalidate(&target);
    if old.exists(){return Err("上次资源备份尚未清理，无法开始新的激活。".into());}
    let journal=context.install.join("tmp/native-install.json");let original=target.exists();
    atomic_json(&journal,&json!({"target":relative,"state":"prepared","original":original,
        "fields":{"target":"受管相对路径","state":"启用阶段","original":"原目录是否存在"}}))?;
    if let Err(error)=crate::compatibility::invalidate(context){let _=fs::remove_file(&journal);return Err(error);}
    if original{if let Err(error)=fs::rename(&target,&old){let _=fs::remove_file(&journal);return Err(error.to_string());}}
    let mut transaction=PendingActivation{relative:relative.into(),target:target.clone(),old,journal,original,swapped:false,committed:false,finished:false};
    if let Err(error)=fs::create_dir_all(target.parent().unwrap()).map_err(|e|e.to_string()).and_then(|_|fs::rename(stage,&target).map_err(|e|e.to_string())){
        let recovery=rollback_activation(context,&mut transaction);
        return Err(match recovery{Ok(())=>error,Err(rollback)=>format!("{error}；恢复旧资源失败：{rollback}")});
    }
    transaction.swapped=true;
    // 候选已按写入路径验证；换入后立即接续监视，最终交接不遗漏新组件的外部变更。
    crate::verification::observe_activation(&target);
    if let Err(error)=atomic_json(&transaction.journal,&json!({"target":relative,"state":"swapped","original":original})){
        let recovery=rollback_activation(context,&mut transaction);
        return Err(match recovery{Ok(())=>error,Err(rollback)=>format!("{error}；恢复旧资源失败：{rollback}")});
    }
    if let Err(error)=refresh_target_binding(context,relative,&target){
        let recovery=rollback_activation(context,&mut transaction);
        return Err(match recovery{Ok(())=>error,Err(rollback)=>format!("{error}；恢复旧资源失败：{rollback}")});
    }
    if let Some(proof)=dsh_proof{
        // cache handoff（复核结果交接）：不匹配就留待启动完整复核，不绕过任何兼容门禁。
        let _=crate::workspace::seed_verified_tree_cache(&target,&context.install.join("cache/component-validation/dsh.json"),&proof,&AtomicBool::new(false));
    }
    Ok(transaction)
}

pub fn rollback_activation(context:&LaunchContext,transaction:&mut PendingActivation)->Result<(),String>{
    if transaction.finished||transaction.committed{return Err("已提交或清理完成的资源事务不能回滚。".into());}
    if transaction.old.exists(){
        remove_owned(&transaction.target,&context.install)?;
        fs::create_dir_all(transaction.target.parent().unwrap()).map_err(|e|e.to_string())?;
        fs::rename(&transaction.old,&transaction.target).map_err(|e|e.to_string())?;
    }else if !transaction.original&&transaction.swapped{remove_owned(&transaction.target,&context.install)?;}
    if transaction.swapped{refresh_target_binding(context,&transaction.relative,&transaction.target)?;}
    remove_owned(&transaction.old,&context.install)?;
    if transaction.journal.exists(){fs::remove_file(&transaction.journal).map_err(|e|e.to_string())?;}
    transaction.finished=true;Ok(())
}

pub fn commit_activation(context:&LaunchContext,transaction:&mut PendingActivation)->Result<(),String>{
    if transaction.finished{return Err("资源事务已清理。".into());}
    if !transaction.committed{
        atomic_json(&transaction.journal,&json!({"target":transaction.relative,"state":"committed","original":transaction.original}))?;
        transaction.committed=true;
    }
    remove_owned(&transaction.old,&context.install)?;
    if transaction.journal.exists(){fs::remove_file(&transaction.journal).map_err(|e|e.to_string())?;}
    transaction.finished=true;Ok(())
}

pub fn activate(context:&LaunchContext,stage:&Path,relative:&str)->Result<(),String>{
    let mut transaction=begin_activation(context,stage,relative)?;
    if let Err(error)=commit_activation(context,&mut transaction){
        if !transaction.committed{let _=rollback_activation(context,&mut transaction);}
        return Err(error);
    }
    Ok(())
}
fn install_and_activate_dsh(
    candidate:&Path,
    cancel:&AtomicBool,
    progress:&mut Progress,
    installer:&mut dyn FnMut(&Path,&AtomicBool,&mut Progress)->Result<(),String>,
    enable:&mut Activation,
)->Result<Vec<String>,String>{
    installer(candidate,cancel,progress)?;
    check_cancel(cancel)?;
    progress("正在启用 DSH（代理宿主）组件",1,1);
    enable(candidate,"runtimes/dsh")?;
    Ok(vec!["dsh".into()])
}
pub fn import(context:&LaunchContext,source:&Path,id:Option<&str>,cancel:Arc<AtomicBool>,progress:&mut Progress)->Result<Vec<String>,String>{
    import_review(context,source,id,cancel,progress,&mut|_|Ok(()))
}
pub fn import_review(context:&LaunchContext,source:&Path,id:Option<&str>,cancel:Arc<AtomicBool>,progress:&mut Progress,review:&mut dyn FnMut(&[Value])->Result<(),String>)->Result<Vec<String>,String>{
    import_review_with_activation(context,source,id,cancel,progress,review,&mut|stage,relative|activate(context,stage,relative))
}
pub fn import_review_with_activation(context:&LaunchContext,source:&Path,id:Option<&str>,cancel:Arc<AtomicBool>,progress:&mut Progress,review:&mut dyn FnMut(&[Value])->Result<(),String>,enable:&mut Activation)->Result<Vec<String>,String>{
    // 每个下载或导入任务独立暂存，网络传输、解包和校验可以并行。
    let task=safe_path(&context.install,&format!("tmp/native-resources-{}",crate::workspace::new_id()))?;fs::create_dir_all(&task).map_err(|e|e.to_string())?;
    struct TaskCleanup<'a>{path:&'a Path,owner:&'a Path} // TaskCleanup（独立候选清理）：失败和取消同样回收。
    impl Drop for TaskCleanup<'_>{fn drop(&mut self){let _=remove_owned(self.path,self.owner);}}
    let _cleanup=TaskCleanup{path:&task,owner:&context.install};
    let mut inputs=vec![];
    if source.is_file()&&source.extension().is_some_and(|v|v.eq_ignore_ascii_case("zip")) {
        let unpacked=task.join("bundle");unpack_zip(source,&unpacked,&cancel,progress)?;
        let index=load(&unpacked.join("resources.json"))?;
        if index["schema_version"]!=1{return Err("资源包格式版本不支持".into());}
        let mut ids=std::collections::HashSet::new();
        for entry in index["resources"].as_array().ok_or("资源包清单无效")? {
            let key=entry["resource_id"].as_str().ok_or("资源包缺少编号")?;
            if !ids.insert(key){return Err("资源包重复编号".into());}if id.is_some_and(|v|v!=key){continue;}
            let resource=spec(context,key)?;if resource["version"]!=entry["version"]&&key!="dsh"{return Err(format!("资源版本不匹配：{key}"));}
            let stage=task.join(format!("candidate-{key}"));fs::create_dir_all(&stage).map_err(|e|e.to_string())?;
            for row in entry["files"].as_array().ok_or("资源包缺少文件映射")? {
                let input=safe_path(&unpacked,row["package_path"].as_str().ok_or("资源包文件来源缺失")?)?;
                let output=safe_path(&stage,row["path"].as_str().ok_or("资源包文件名称缺失")?)?;
                copy_file(&input,&output,&cancel,progress)?;
            }
            inputs.push((key.to_owned(),stage));
        }
    }else{inputs.push((id.ok_or("文件夹导入需要指定组件")?.into(),source.to_path_buf()));}
    if inputs.is_empty(){return Err("没有识别到可导入资源".into());}
    // 整包预校验在替换任何现有资源之前完成。
    for (key,path) in &inputs {progress(&format!("预校验 {key}（资源编号）"),0,0);
        if key=="dsh" {if path.is_dir(){inspect_dsh_with_progress(context,path,true,&cancel,progress)?;}else{verify_installer(context,path,&cancel,progress)?;}}
        else{validate_model_files_with_progress(context,&spec(context,key)?,path,true,&cancel,progress)?;}}
    let review_items=inputs.iter().map(|(key,_)|{
        let resource=spec(context,key)?;
        Ok(json!({"resource_id":key,"title":resource["title"],"version":resource["version"],"files_verified":true,
            "fields":{"resource_id":"已识别资源编号","title":"组件名称","version":"固定版本","files_verified":"整包预校验已通过"}}))
    }).collect::<Result<Vec<Value>,String>>()?;
    review(&review_items)?;check_cancel(&cancel)?;
    let mut installed=vec![];
    for (key,path) in inputs {check_cancel(&cancel)?;let resource=spec(context,&key)?;let candidate=task.join(format!("install-{key}"));
        let current=destination(context,&resource)?;
        let identical=if key=="dsh" {path.is_dir()&&file_hash(&path.join("manifest.json")).ok()==file_hash(&current.join("manifest.json")).ok()&&dsh_validate_with_progress(context,&current,true,&cancel,progress).is_ok()}
            else{validate_model_files_with_progress(context,&resource,&current,false,&cancel,progress).is_ok()&&inspect_installed_model(context,&resource,false).is_ok_and(|installed|!model_update_available(&resource,&installed))&&resource["files"].as_array().unwrap().iter().filter(|v|v["optional"]==true).all(|v|{
                let name=v["path"].as_str().unwrap_or("");!path.join(name).is_file()||current.join(name).is_file()
            })};
        check_cancel(&cancel)?;
        if identical{progress(&format!("跳过相同完整资源：{key}（编号）"),1,1);installed.push(key);continue;}
        if key=="dsh"&&path.is_file(){extract_dsh(context,&path,&candidate,&cancel,progress)?;}
        else if key=="dsh"{copy_verified(&path,&candidate,&cancel,progress)?;inspect_dsh_with_progress(context,&candidate,true,&cancel,progress)?;}
        else {
            let mut verified=vec![];let mut buffer=vec![0;1024*1024];
            for row in resource["files"].as_array().unwrap(){let name=row["path"].as_str().unwrap();let from=if let Some(asset)=row["asset"].as_str(){resource_asset(context,asset)?}else{safe_path(&path,name)?};
                if !from.is_file()&&row["optional"]==true{continue;}check_cancel(&cancel)?;let to=safe_path(&candidate,name)?;
                if from.metadata().map_err(|e|e.to_string())?.len()!=row["size_bytes"].as_u64().ok_or("模型大小缺失")?{return Err("模型源文件大小不匹配。".into());}
                let digest=copy_file_buffered(&from,&to,&cancel,&AtomicBool::new(false),Some(row["sha256"].as_str().ok_or("模型摘要缺失")?),&mut buffer,progress)?;
                verified.push(json!({"path":name,"size":row["size_bytes"],"sha256":digest,"optional":row["optional"]==true}));}
            write_model_receipt(&candidate,&resource,verified)?;
        }
        check_cancel(&cancel)?;progress("正在启用资源",1,1);check_cancel(&cancel)?;
        let relative=destination(context,&resource)?.strip_prefix(&context.install).unwrap().to_string_lossy().replace('\\',"/");enable(&candidate,&relative)?;
        installed.push(key);
    }
    remove_owned(&task,&context.install)?;Ok(installed)
}
fn refresh_models(context:&LaunchContext,activated:Option<&str>)->Result<(),String>{
    let root=context.install.join("models");let manifest_path=root.join("manifest.json");
    let mut known=std::collections::HashMap::<String,Value>::new();
    // 启用时复用已签发清单与新组件的复制校验结果；手动维护统一核验文件内容。
    if let Ok(record)=load(&manifest_path){for row in record["files"].as_array().into_iter().flatten(){
        if let Some(path)=row["path"].as_str(){if row["sha256"].as_str().is_some_and(valid_sha256){known.insert(path.to_owned(),row.clone());}}
    }}
    // activated_prefix（本次换入的组件前缀）：该组件的文件以新收据为准，不能沿用旧总清单摘要。
    let activated_prefix=activated.and_then(|relative|relative.strip_prefix("models/")).map(|relative|format!("{}/",relative.trim_end_matches('/')));
    let mut verified=std::collections::HashSet::new();
    for resource in specs(context)?.iter().filter(|item|item["kind"]=="model"){
        if let Ok(installed)=inspect_installed_model(context,resource,false){let prefix=resource["destination"].as_str().ok_or("模型目录缺失")?;
            for row in installed.files{let path=format!("{prefix}/{}",row["path"].as_str().unwrap());
                if activated_prefix.as_deref().is_some_and(|activated|path.starts_with(activated)){verified.insert(path.clone());}
                known.insert(path.clone(),json!({"path":path,"size":row["size"],"sha256":row["sha256"]}));}
        }
    }
    // manifest_time（上次签发清单时间）：在此之后被改写的内容不能沿用旧摘要。
    let manifest_time=fs::metadata(&manifest_path).and_then(|metadata|metadata.modified()).ok();
    let mut files=vec![];
    for path in walk(&root)?{if path==manifest_path{continue;}
        let relative=path.strip_prefix(&root).unwrap().to_string_lossy().replace('\\',"/");let size=path.metadata().map_err(|e|e.to_string())?.len();
        let digest=if let Some(row)=known.get(&relative){
            let same_size=row["size"].as_u64()==Some(size);
            let in_activated=activated_prefix.as_deref().is_some_and(|activated|relative.starts_with(activated));
            // 刚换入组件里未被新收据登记的文件（安装收据本身）必须重新读取内容。
            let rewritten=path.metadata().ok().and_then(|metadata|metadata.modified().ok()).is_some_and(|modified|manifest_time.is_some_and(|time|modified>time));
            if same_size&&!verified.contains(&relative)&&(in_activated||rewritten){json!(file_hash(&path)?)}else if same_size{row["sha256"].clone()}else{json!(file_hash(&path)?)}
        }else{json!(file_hash(&path)?)};
        files.push(json!({"path":relative,"size":size,"sha256":digest}));
    }
    atomic_json(&manifest_path,&json!({"schema":1,"kind":"models","build":crate::workspace::new_id(),"files":files,
        "fields":{"schema":"清单格式","kind":"资源类别","build":"启用登记编号","files":"已校验的安装文件记录","path":"相对路径","size":"字节数","sha256":"文件摘要；手动完整性维护时核验"}}))?;
    bind_component(context,"models","models",&root)
}
pub fn download_resource(context:&LaunchContext,id:&str,cancel:Arc<AtomicBool>,progress:&mut Progress)->Result<Vec<String>,String>{
    download_resource_with_activation(context,id,cancel,progress,&mut|stage,relative|activate(context,stage,relative))
}
pub fn download_resource_with_activation(context:&LaunchContext,id:&str,cancel:Arc<AtomicBool>,progress:&mut Progress,enable:&mut Activation)->Result<Vec<String>,String>{
    let item=spec(context,id)?;
    let installed=destination(context,&item)?;
    check_cancel(&cancel)?;progress("检查现有组件能否复用",0,0);
    let current=if id=="dsh"{
        // target gate（目标门禁）：需要迁移的旧来源或版本不必先完整读取其文件内容。
        let matches=if item["origin"]=="npm_package"{load(&installed.join("runtime.json")).is_ok_and(|metadata|
            crate::dsh::target(&context.installed_program()).is_ok_and(|target|crate::dsh::current(&metadata,&target)))}else{true};
        matches&&dsh_validate_with_progress(context,&installed,true,&cancel,progress).is_ok()
            &&(item["origin"]!="npm_package"||crate::dsh::target(&context.installed_program()).is_ok_and(|spec|crate::dsh::discover(&spec,true).is_ok()))
    }
        else{inspect_installed_model(context,&item,false).is_ok_and(|current|
            !model_update_available(&item,&current)&&validate_model_files_with_progress(context,&item,&installed,false,&cancel,progress).is_ok())};
    check_cancel(&cancel)?;
    if current {progress("复用已校验资源",1,1);return Ok(vec![id.into()]);}
    // 收据损坏不代表权重损坏：按目标内容复核后从本机重建，沿用既有候选及启用事务。
    // 不直接并发改写现用收据或模型总清单，也不为修复登记重新下载完整组件。
    if item["kind"]=="model"&&installed.is_dir()&&inspect_installed_model(context,&item,false).is_err(){
        let valid=validate_model_files_with_progress(context,&item,&installed,false,&cancel,progress).is_ok();
        check_cancel(&cancel)?;
        if valid{
            progress("组件内容完整，使用本机文件修复安装记录",1,1);
            return import_review_with_activation(context,&installed,Some(id),cancel,progress,&mut|_|Ok(()),enable);
        }
    }
    if item["source"]["transfer"]["kind"]=="official_audiocpp_patch"{
        return download_patched_audiocpp(context,&item,cancel,progress,enable);
    }
    if id=="dsh"&&item["origin"]=="npm_package"{
        let root=safe_path(&context.install,"tmp/resource-downloads/dsh")?;fs::create_dir_all(&root).map_err(|e|e.to_string())?;
        let candidate=root.join(crate::dsh::candidate_name(&crate::dsh::target(&context.installed_program())?)?);
        let mut installer=|candidate:&Path,cancel:&AtomicBool,progress:&mut Progress|crate::dsh::install(context,candidate,cancel,progress);
        let result=install_and_activate_dsh(&candidate,&cancel,progress,&mut installer,enable);
        // retry candidate（重试候选）：中断或失败保留候选及归档缓存，重试仍核对相同目标。
        return result;
    }
    let root=safe_path(&context.install,&format!("tmp/resource-downloads/{id}"))?;fs::create_dir_all(&root).map_err(|e|e.to_string())?;
    if let Some(artifact)=item["source"]["transfer"]["artifact"].as_object(){
        let value=Value::Object(artifact.clone());let spec=file_spec(&value,value["url"].as_str().ok_or("缺少下载来源")?.into())?;let path=safe_path(&root,&spec.path)?;
        crate::download::download_with_progress(&spec,&path,cancel.clone(),&mut|phase,done,total|progress(&format!("固定附件：{phase}"),done,total),0)?;
        if id=="dsh"{return import_review_with_activation(context,&path,Some(id),cancel,progress,&mut|_|Ok(()),enable);}
        let seven=context.program.join("tools/7zip/7z.exe");let out=root.join("extracted");remove_owned(&out,&context.install)?;run_seven(&seven,&path,&out,&cancel)?;
        let tar=walk(&out)?.into_iter().find(|p|p.extension().is_some_and(|v|v=="tar")).ok_or("模型归档缺少 tar（内层归档）")?;
        let model=root.join("model");remove_owned(&model,&context.install)?;run_seven(&seven,&tar,&model,&cancel)?;
        let folder=walk(&model)?.into_iter().find(|p|p.file_name().is_some_and(|v|v=="tokens.txt")).ok_or("唤醒词归档不完整")?.parent().unwrap().to_path_buf();
        return import_review_with_activation(context,&folder,Some(id),cancel,progress,&mut|_|Ok(()),enable);
    }
    let files=item["files"].as_array().ok_or("下载文件清单缺失")?;
    let mut downloads=vec![];
    let old=inspect_installed_model(context,&item,false).ok();
    for row in files {
        if row["optional"]==true||row["asset"].is_string(){continue;}
        let url=model_file_url(&item,row)?;
        let file=file_spec(row,url)?;let path=safe_path(&root,&file.path)?;fs::create_dir_all(path.parent().unwrap()).map_err(|e|e.to_string())?;
        // 版本更新只改变少量资产时，优先复用旧组件里摘要相同的文件；源与目标都核验。
        if !path.is_file()&&old.as_ref().is_some_and(|old|old.files.iter().any(|row|row["path"]==file.path&&row["size"].as_u64()==Some(file.size)&&row["sha256"]==file.sha256)){
            let source=safe_path(&installed,&file.path)?;
            let result=copy_file_buffered(&source,&path,&cancel,&AtomicBool::new(false),Some(&file.sha256),&mut vec![0;1024*1024],&mut|phase,done,total|progress(&format!("复用现有模型：{phase}"),done,total));
            check_cancel(&cancel)?;
            if result.is_err(){let _=fs::remove_file(&path);}
        }
        downloads.push((file,path));
    }
    crate::download::download_batch(&downloads,cancel.clone(),&mut|phase,done,total|progress(phase,done,total))?;
    import_review_with_activation(context,&root,Some(id),cancel,progress,&mut|_|Ok(()),enable)
}
/// download_patched_audiocpp（官方原生包加随包补丁）：逐层校验后才原子启用，无客户机编译。
fn download_patched_audiocpp(context:&LaunchContext,item:&Value,cancel:Arc<AtomicBool>,progress:&mut Progress,enable:&mut Activation)->Result<Vec<String>,String>{
    let transfer=&item["source"]["transfer"];
    let task=safe_path(&context.install,"tmp/resource-downloads/tts-audiocpp-runtime")?;
    fs::create_dir_all(&task).map_err(|e|e.to_string())?;
    let patch=&transfer["patch"];
    let patch_file=resource_asset(context,patch["asset"].as_str().ok_or("原生覆盖补丁缺失，请修复本体安装。")?)?;
    if patch_file.metadata().map_err(|e|e.to_string())?.len()!=patch["size_bytes"].as_u64().ok_or("补丁大小缺失")?
        ||crate::workspace::file_hash_with_progress(&patch_file,&cancel,progress)?!=patch["sha256"].as_str().ok_or("补丁摘要缺失")?{
        return Err("audio.cpp（原生语音框架）随包补丁损坏，请修复本体安装。".into());
    }
    let archives=transfer["artifacts"].as_array().ok_or("官方原生归档清单缺失")?;
    for artifact in archives{
        check_cancel(&cancel)?;
        let file=file_spec(artifact,artifact["url"].as_str().ok_or("官方原生包地址缺失")?.into())?;
        let path=safe_path(&task,&file.path)?;
        crate::download::download_with_progress(&file,&path,cancel.clone(),&mut|phase,done,total|progress(&format!("官方原生运行包：{phase}"),done,total),0)?;
        let archive_id=artifact["id"].as_str().ok_or("官方归档编号缺失")?;
        let out=safe_path(&task,&format!("unpacked/{archive_id}"))?;
        remove_owned(&out,&context.install)?;
        unpack_zip(&path,&out,&cancel,progress)?;
    }
    progress("正在应用本地语音补丁",0,0);
    remove_owned(&task.join("unpacked/patch"),&context.install)?;
    unpack_zip(&patch_file,&task.join("unpacked/patch"),&cancel,progress)?;
    let candidate=task.join("candidate");
    if candidate.exists(){remove_owned(&candidate,&context.install)?;}
    fs::create_dir_all(&candidate).map_err(|e|e.to_string())?;
    let mut verified=vec![];let mut buffer=vec![0;1024*1024];
    for row in item["files"].as_array().ok_or("原生最终文件清单缺失")?{
        let archive_id=row["archive_id"].as_str().ok_or("原生文件来源编号缺失")?;
        if archive_id!="patch"&&!archives.iter().any(|entry|entry["id"]==archive_id){return Err("原生文件引用未锁定归档。".into());}
        let source=safe_path(&task,&format!("unpacked/{archive_id}/{}",row["source_path"].as_str().ok_or("原生文件来源路径缺失")?))?;
        let name=row["path"].as_str().ok_or("原生文件安装路径缺失")?;
        let target=safe_path(&candidate,name)?;
        if source.metadata().map_err(|e|e.to_string())?.len()!=row["size_bytes"].as_u64().ok_or("原生文件大小缺失")?{return Err(format!("原生补丁安装文件大小不符：{name}"));}
        let hash=copy_file_buffered(&source,&target,&cancel,&AtomicBool::new(false),Some(row["sha256"].as_str().ok_or("原生文件摘要缺失")?),&mut buffer,progress)?;
        verified.push(json!({"path":name,"size":row["size_bytes"],"sha256":hash,"optional":false}));
    }
    write_model_receipt(&candidate,item,verified)?;
    check_cancel(&cancel)?;progress("正在启用本地语音运行程序",1,1);
    enable(&candidate,"models/audiocpp/runtime")?;
    Ok(vec!["tts-audiocpp-runtime".into()])
}
/// model_file_url（模型文件来源）：直接访问固定版本，下载过程不调用 Python（运行环境）。
fn model_file_url(item:&Value,row:&Value)->Result<String,String>{
    let repo=item["source"]["repo_id"].as_str().ok_or("仓库缺失")?;
    let revision=item["source"]["revision"].as_str().ok_or("版本缺失")?;
    let path=row["source_path"].as_str().or(row["path"].as_str()).ok_or("文件名缺失")?;
    let encode=|text:&str,slash:bool|text.bytes().map(|byte|{
        if byte.is_ascii_alphanumeric()||b"-._~".contains(&byte)||(slash&&byte==b'/'){(byte as char).to_string()}else{format!("%{byte:02X}")}
    }).collect::<String>();
    match item["source"]["platform"].as_str(){
        Some("modelscope")=>Ok(format!("https://modelscope.cn/api/v1/models/{}/repo?Revision={}&FilePath={}",encode(repo,true),encode(revision,false),encode(path,false))),
        Some("huggingface")=>Ok(format!("https://huggingface.co/{}/resolve/{}/{}",encode(repo,true),encode(revision,false),encode(path,true))),
        _=>Err("该模型缺少受支持的直接文件来源".into()),
    }
}
fn verify_installer(context:&LaunchContext,path:&Path,cancel:&AtomicBool,progress:&mut Progress)->Result<String,String>{
    let item=spec(context,"dsh")?;let artifact=if item["origin"]=="npm_package"{&item["source"]["official_transfer"]["artifact"]}else{&item["source"]["transfer"]["artifact"]};
    if path.metadata().map_err(|e|e.to_string())?.len()!=artifact["size_bytes"].as_u64().unwrap_or(0){return Err("DSH 官方安装器大小不匹配".into());}
    let total=path.metadata().map_err(|e|e.to_string())?.len();let mut done=0;let mut last_report=Instant::now();progress("校验官方安装器摘要",0,total);
    let mut stream=fs::File::open(path).map_err(|e|e.to_string())?;let mut digest=Sha512::new();let mut buffer=vec![0;1024*1024];
    loop {check_cancel(cancel)?;let count=stream.read(&mut buffer).map_err(|e|e.to_string())?;if count==0{break;}digest.update(&buffer[..count]);done+=count as u64;
        if last_report.elapsed()>=Duration::from_millis(100){progress("校验官方安装器摘要",done,total);last_report=Instant::now();}}
    progress("校验官方安装器摘要",done,total);check_cancel(cancel)?;
    let value=digest.finalize();if base64::engine::general_purpose::STANDARD.encode(value)!=artifact["sha512_base64"].as_str().unwrap_or(""){return Err("DSH 官方安装器摘要不匹配".into());}Ok(format!("{value:x}"))
}
fn run_seven(exe:&Path,source:&Path,target:&Path,cancel:&AtomicBool)->Result<(),String>{
    #[cfg(windows)] use std::os::windows::{process::CommandExt,io::AsRawHandle};
    fs::create_dir_all(target).map_err(|e|e.to_string())?;let mut command=Command::new(exe);
    command.arg("x").arg(source).arg(format!("-o{}",target.display())).args(["-y","-bd"]).stdout(Stdio::null()).stderr(Stdio::null());
    #[cfg(windows)] command.creation_flags(0x08000000);
    let mut child=command.spawn().map_err(|e|e.to_string())?;
    #[cfg(windows)] let job=crate::windows::Job::new()?;
    #[cfg(windows)] job.assign(child.as_raw_handle())?;
    loop {if cancel.load(Ordering::Acquire){let _=child.kill();let _=child.wait();return Err("解包已取消".into());}
        if let Some(status)=child.try_wait().map_err(|e|e.to_string())? {if !status.success(){return Err("7-Zip（压缩工具）解包失败".into());}break;}
        std::thread::sleep(std::time::Duration::from_millis(80));}
    walk(target)?;Ok(())
}
fn extract_dsh(context:&LaunchContext,source:&Path,target:&Path,cancel:&AtomicBool,progress:&mut Progress)->Result<(),String>{
    let sha=verify_installer(context,source,cancel,progress)?;let seven=context.program.join("tools/7zip/7z.exe");let staging=target.with_extension("extract");let outer=staging.join("nsis");
    remove_owned(&staging,&context.install)?;
    progress("正在提取官方安装器",0,0);run_seven(&seven,source,&outer,cancel)?;
    let matches:Vec<_>=walk(&outer)?.into_iter().filter(|p|p.file_name().is_some_and(|n|n=="app-64.7z")).collect();if matches.len()!=1{return Err("官方安装器中无法唯一识别应用负载".into());}
    let inner=staging.join("payload");run_seven(&seven,&matches[0],&inner,cancel)?;
    let candidates:Vec<_>=walk(&inner)?.into_iter().filter(|p|p.file_name().is_some_and(|n|n=="DeepSeek Harness.exe")&&p.parent().unwrap().join("resources/app.asar").is_file()).collect();
    if candidates.len()!=1{return Err("官方应用目录无法唯一识别".into());}copy_verified(candidates[0].parent().unwrap(),target,cancel,progress)?;
    let item=spec(context,"dsh")?;
    let lock=if item["source"]["official_compatibility"].is_object(){item["source"]["official_compatibility"].clone()}else{load(&context.program.join("config/dsh/runtime.lock.json"))?};
    atomic_json(&target.join("runtime.json"),&json!({"schema":2,"kind":"official_desktop","source_kind":"official_desktop","version":lock["version"],"bridge_protocol":"2.0","compat_expected_commit":lock["commit"],"source_sha512":sha,
        "executable":"DeepSeek Harness.exe","app_asar":"resources/app.asar","entry":"resources/app.asar/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js",
        "schedule":"resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-schedule/lib/index.js",
        "office_cli":"resources/app.asar.unpacked/dsh/node_modules/@deepseek-ai/libreoffice-kit/lib/cli.js",
        "office_engine":"resources/app.asar.unpacked/dsh/node_modules/@deepseek-ai/libreoffice-kit-win32-x64","asar_unpacked_root":"resources/app.asar.unpacked/dsh"}))?;
    atomic_json(&target.join("manifest.json"),&crate::workspace::manifest_with_progress(target,"dsh",&format!("official-{}-{}",lock["version"].as_str().unwrap_or(""),&sha[..16]),cancel,progress)?)?;
    remove_owned(&staging,&context.install)?;Ok(())
}
fn inspect_dsh_installed(context:&LaunchContext,source:&Path,full:bool)->Result<Value,String>{
    inspect_dsh_with_progress(context,source,full,&AtomicBool::new(false),&mut|_,_,_|{})
}
/// validate_dsh_activation（DSH 启用后验收）：在旧目录事务提交前复核实际版本、兼容记录、完整摘要和启动状态。
pub fn validate_dsh_activation(context:&LaunchContext)->Result<(),String>{
    let program=context.installed_program();
    let target=crate::dsh::target(&program)?;
    let root=context.install.join("runtimes/dsh");
    let metadata=inspect_dsh_installed(context,&root,true)?;
    if !crate::dsh::current(&metadata,&target){return Err("已启用的 DSH（代理宿主）未达到程序锁定目标版本。".into());}
    crate::dsh::check_plugin(&program,&target)?;
    let rows=inventory(context)?;
    let row=rows.iter().find(|row|row["resource_id"]=="dsh").ok_or("DSH（代理宿主）组件状态缺失")?;
    validate_dsh_activation_state(row,target["version"].as_str().ok_or("DSH 目标版本缺失")?)
}
fn validate_dsh_activation_state(row:&Value,target_version:&str)->Result<(),String>{
    if row["installed_version"].as_str()!=Some(target_version)
        || row["update_available"]!=json!(false)
        || row["can_start"]!=json!(true)
    {return Err("DSH（代理宿主）启用后状态未就绪；版本、更新标记或启动条件未通过。".into());}
    Ok(())
}
fn inspect_dsh_with_progress(context:&LaunchContext,source:&Path,full:bool,cancel:&AtomicBool,progress:&mut Progress)->Result<Value,String>{
    check_cancel(cancel)?;
    let metadata=load(&source.join("runtime.json"))?;let digest=file_hash(&source.join("manifest.json"))?;let item=spec(context,"dsh")?;
    if metadata["source_kind"]=="npm_package" {
        crate::dsh::inspect(&context.installed_program(),source)?;
        if source==context.install.join("runtimes/dsh"){
            let release=load(&context.installed_program().join("release.json"))?;let manifest=load(&source.join("manifest.json"))?;
            if !release["components"].as_array().is_some_and(|rows|rows.iter().any(|row|row["kind"]=="dsh"&&row["path"]=="runtimes/dsh"&&row["build"]==manifest["build"]&&row["manifest_sha256"]==digest)){
                return Err("DSH（代理宿主）安装清单绑定不一致，请修复组件。".into());
            }
        }
    }else if metadata["source_kind"]=="official_desktop" {
        let version=metadata["version"].as_str().ok_or("DSH 安装版本缺失")?;
        let source_sha=metadata["source_sha512"].as_str().ok_or("DSH 官方来源摘要缺失")?;
        if source_sha.len()!=128||!source_sha.bytes().all(|byte|byte.is_ascii_hexdigit()){return Err("DSH 官方来源摘要格式无效".into());}
        let trusted=load(&context.program.join("config/dsh/official-runtime.lock.json")).ok();
        let installed=load(&context.data.join("state/native-dsh-binding.json")).ok();
        if ![trusted,installed].iter().flatten().any(|v|v["manifest_sha256"]==digest&&v["version"]==version&&v["source_sha512"]==source_sha){return Err("提取目录缺少程序认可的可信摘要，请导入锁定官方安装器".into());}
    }else if item["legacy_export"]["supported"]!=true||item["legacy_export"]["manifest_sha256"]!=digest||metadata["version"].as_str().is_none(){
        return Err("旧 DSH 目录与兼容清单不匹配".into());
    }
    // 日常启动与状态读取仅验证上述身份和必要入口；完整目录检查只在维护任务执行。
    if full{crate::workspace::verify_tree_with_progress(source,"manifest.json",cancel,progress)?;}
    check_cancel(cancel)?;
    Ok(metadata)
}
#[cfg(test)]
fn dsh_validate(context:&LaunchContext,source:&Path,full:bool)->Result<(),String>{
    dsh_validate_with_progress(context,source,full,&AtomicBool::new(false),&mut|_,_,_|{})
}
fn dsh_validate_with_progress(context:&LaunchContext,source:&Path,full:bool,cancel:&AtomicBool,progress:&mut Progress)->Result<(),String>{
    let metadata=inspect_dsh_with_progress(context,source,full,cancel,progress)?;let item=spec(context,"dsh")?;
    if item["origin"]=="npm_package"{
        if !crate::dsh::current(&metadata,&crate::dsh::target(&context.installed_program())?){return Err("DSH（代理宿主）版本或来源需要更新，请使用组件下载流程迁移。".into());}
        crate::dsh::check_plugin(&context.installed_program(),&crate::dsh::target(&context.installed_program())?)?;return Ok(());
    }
    if metadata["source_kind"]!="official_desktop"{return Err("旧版 DSH 代理宿主需要从官方来源更新后才能启动".into());}
    let expected=base64::engine::general_purpose::STANDARD.decode(item["source"]["transfer"]["artifact"]["sha512_base64"].as_str().ok_or("官方摘要缺失")?).map_err(|e|e.to_string())?;
    let expected=expected.iter().map(|byte|format!("{byte:02x}")).collect::<String>();
    if metadata["version"]!=item["version"]||metadata["source_sha512"]!=expected{return Err("DSH（代理宿主）版本或官方来源不匹配".into());}
    Ok(())
}
fn bind_dsh(context:&LaunchContext)->Result<(),String>{
    let root=context.install.join("runtimes/dsh");let metadata=load(&root.join("runtime.json"))?;
    if metadata["source_kind"]=="npm_package"{crate::dsh::bind(context,&root)?;}else if metadata["source_kind"]=="official_desktop"{atomic_json(&context.data.join("state/native-dsh-binding.json"),&json!({"version":metadata["version"],"source_sha512":metadata["source_sha512"],"manifest_sha256":file_hash(&root.join("manifest.json"))?,"fields":{"version":"固定 DSH 版本","source_sha512":"已核验官方安装器摘要","manifest_sha256":"程序签发的提取目录清单摘要"}}))?;}
    bind_component(context,"dsh","runtimes/dsh",&root)
}
pub fn bind_component(context:&LaunchContext,kind:&str,relative:&str,root:&Path)->Result<(),String>{
    let path=context.program.join("release.json");if !path.is_file(){return Ok(());}
    let mut release=load(&path)?;let manifest=load(&root.join("manifest.json"))?;
    if !release["components"].is_array(){release["components"]=json!([]);}
    let entries=release["components"].as_array_mut().unwrap();entries.retain(|v|v["kind"]!=kind);
    entries.push(json!({"kind":kind,"path":relative,"build":manifest["build"],"manifest_sha256":file_hash(&root.join("manifest.json"))?,"optional":true}));
    atomic_json(&path,&release)
}

#[cfg(test)]
mod download_tests {
    use super::*;

    #[test]
    fn audio_enhancement_switches_migrate_and_save_independently(){
        let root=std::env::temp_dir().join(format!("voice-enhancements-{}",crate::workspace::new_id()));
        fs::create_dir_all(root.join("config")).unwrap();
        fs::write(root.join("config/local.yaml"),"smart_turn:\n  sensevoice: false # 旧精修关闭\n").unwrap();
        assert_eq!(audio_enhancements(&root,None).unwrap(),json!({"dual_pass":false,"emotion":false,"acoustic_events":false}));
        let value=audio_enhancements(&root,Some(&json!({"emotion":true}))).unwrap();
        assert_eq!(value,json!({"dual_pass":false,"emotion":true,"acoustic_events":false}));
        assert_eq!(audio_enhancements(&root,None).unwrap(),value);
        assert!(audio_enhancements(&root,Some(&json!({"emotion":1}))).is_err());
        assert_eq!(audio_enhancements(&root,None).unwrap(),value);
        assert!(root.canonicalize().unwrap().starts_with(std::env::temp_dir().canonicalize().unwrap()));
        assert!(root.file_name().unwrap().to_string_lossy().starts_with("voice-enhancements-"));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    #[ignore="手动验收：仅使用已下载官方归档，在独立报告目录安装及复核原生补丁，不修改正式工作区"]
    fn install_official_audiocpp_patch_in_isolated_workspace(){
        let root=PathBuf::from(std::env::var_os("VOICE_TTS_TEST_ROOT").expect("需要 VOICE_TTS_TEST_ROOT（独立原生测试目录）"));
        let program=PathBuf::from(std::env::var_os("VOICE_TTS_SOURCE_ROOT").expect("需要 VOICE_TTS_SOURCE_ROOT（源码目录）"));
        let cache=PathBuf::from(std::env::var_os("VOICE_TTS_ARCHIVE_CACHE").expect("需要 VOICE_TTS_ARCHIVE_CACHE（已校验官方归档缓存）"));
        assert!(root.starts_with(program.join(".local/cache/reports/tts-distribution")));
        fs::create_dir_all(root.parent().unwrap()).unwrap();
        crate::workspace::initialize(&root).unwrap();
        fs::create_dir_all(root.join("app/config/resources/assets/audiocpp")).unwrap();fs::create_dir_all(root.join("app/scripts")).unwrap();
        fs::write(root.join("app/config/default.yaml"),b"{}").unwrap();fs::write(root.join("app/scripts/backend-entry.py"),b"# fixture").unwrap();
        let catalog=load(&program.join("config/resources/catalog.json")).unwrap();
        let entry=catalog["resources"].as_array().unwrap().iter().find(|row|row["resource_id"]=="tts-audiocpp-runtime").unwrap();
        atomic_json(&root.join("app/config/resources/catalog.json"),&json!({"resources":[entry]})).unwrap();
        fs::copy(program.join("config/resources/assets/audiocpp/voxcpm-patch.zip"),root.join("app/config/resources/assets/audiocpp/voxcpm-patch.zip")).unwrap();
        let context=LaunchContext::portable(&root).unwrap();
        let item=spec(&context,"tts-audiocpp-runtime").unwrap();let task=root.join("tmp/resource-downloads/tts-audiocpp-runtime");
        fs::create_dir_all(&task).unwrap();
        for artifact in item["source"]["transfer"]["artifacts"].as_array().unwrap(){
            let filename=artifact["filename"].as_str().unwrap();let verified=cache.join(filename.replace(".zip",".verified.zip"));
            let source=if verified.is_file(){verified}else{cache.join(filename)};
            let target=task.join(filename);if !target.is_file(){fs::copy(source,&target).unwrap();}
        }
        let result=download_resource(&context,"tts-audiocpp-runtime",Arc::new(AtomicBool::new(false)),&mut|_,_,_|{}).unwrap();
        assert_eq!(result,vec!["tts-audiocpp-runtime"]);
        let target=destination(&context,&item).unwrap();model_validate(&context,&item,&target).unwrap();
        let before=file_hash(&target.join(MODEL_RECEIPT)).unwrap();
        download_resource(&context,"tts-audiocpp-runtime",Arc::new(AtomicBool::new(false)),&mut|_,_,_|{}).unwrap();
        assert_eq!(file_hash(&target.join(MODEL_RECEIPT)).unwrap(),before);
        println!("已通过官方归档、随包补丁、最终文件及重试复用校验：{}",target.display());
    }

    #[test]
    fn selected_tts_survives_only_unselected_optional_model_corruption(){
        let root=std::env::temp_dir().join(format!("voice-tts-options-{}",crate::workspace::new_id()));
        crate::workspace::initialize(&root).unwrap();ready_test_runtime(&root);
        fs::create_dir_all(root.join("app/scripts")).unwrap();
        fs::write(root.join("app/config/default.yaml"),b"{}").unwrap();
        fs::write(root.join("app/scripts/backend-entry.py"),b"# fixture").unwrap();
        let mut entries=vec![];
        for id in ["tts-audiocpp-runtime","tts-voxcpm1","tts-voxcpm2"]{
            let relative=format!("audiocpp/{id}");let folder=root.join("models").join(&relative);
            fs::create_dir_all(&folder).unwrap();fs::write(folder.join("model.bin"),b"good").unwrap();
            let mut item=json!({"resource_id":id,"kind":"model","title":id,"destination":relative,"version":"1","required":false,
                "files":[{"path":"model.bin","size_bytes":4,"sha256":file_hash(&folder.join("model.bin")).unwrap()}]});
            if id!="tts-audiocpp-runtime"{item["runtime_resource"]=json!("tts-audiocpp-runtime");}
            write_model_receipt(&folder,&item,vec![json!({"path":"model.bin","size":4,"sha256":item["files"][0]["sha256"],"optional":false})]).unwrap();
            entries.push(item);
        }
        atomic_json(&root.join("app/config/resources/catalog.json"),&json!({"resources":entries})).unwrap();
        let context=LaunchContext::portable(&root).unwrap();
        atomic_json(&context.data.join("config/product-settings.json"),&json!({"schema_version":14,"tts":{"backend":"voxcpm1","local_model":"voxcpm1"}})).unwrap();
        let rows=inventory(&context).unwrap();assert_eq!(rows.iter().find(|row|row["resource_id"]=="tts").unwrap()["local_ready"],true);
        fs::write(root.join("models/audiocpp/tts-voxcpm2/model.bin"),b"bad!").unwrap();
        let state=startup_readiness(&context).unwrap();assert_eq!(state["ready"],true);
        fs::remove_file(root.join("models/audiocpp/tts-voxcpm1/model.bin")).unwrap();
        let mut rows=inventory(&context).unwrap();validate_inventory_hashes(&context,&mut rows,&AtomicBool::new(false),&mut|_,_,_|{}).unwrap();
        assert_eq!(rows.iter().find(|row|row["resource_id"]=="tts").unwrap()["local_ready"],false);
        crate::workspace::remove_owned(&root,&std::env::temp_dir()).unwrap();
    }

    #[test]
    #[ignore="手动验收：只读真实环境并在指定缓存中复制样本、对照旧流程和优化流程"]
    fn benchmark_resource_copy_and_validation(){
        let source=PathBuf::from(std::env::var_os("VOICE_PIPELINE_TEST_ROOT").expect("需要只读源组件目录"));
        let report=PathBuf::from(std::env::var_os("VOICE_PIPELINE_REPORT").expect("需要缓存报告路径"));let owner=report.parent().unwrap();fs::create_dir_all(owner).unwrap();
        let run=owner.join(format!("复制 真实样本-{}",crate::workspace::new_id()));let sample=run.join("source");fs::create_dir_all(&sample).unwrap();
        let original_manifest=file_hash(&source.join("manifest.json")).unwrap();
        let all=walk(&source).unwrap();let mut selected=all.iter().filter(|path|path.metadata().is_ok_and(|metadata|metadata.len()>0&&metadata.len()<=64*1024)).take(1000).cloned().collect::<Vec<_>>();
        let mut large=all.iter().filter(|path|path.metadata().is_ok_and(|metadata|metadata.len()>1024*1024&&metadata.len()<=8*1024*1024)).cloned().collect::<Vec<_>>();large.sort_by_key(|path|std::cmp::Reverse(path.metadata().unwrap().len()));selected.extend(large.into_iter().take(4));
        for path in selected{let relative=path.strip_prefix(&source).unwrap();let output=sample.join(relative);fs::create_dir_all(output.parent().unwrap()).unwrap();fs::copy(path,output).unwrap();}
        let manifest=manifest(&sample,"sample","benchmark").unwrap();let files=walk(&sample).unwrap();let sample_bytes=files.iter().map(|file|file.metadata().unwrap().len()).sum::<u64>();let mut rounds=vec![];
        for (round,order) in [["legacy","optimized"],["optimized","legacy"]].into_iter().enumerate(){for mode in order{
            let target=run.join(format!("{round}-{mode}"));fs::create_dir_all(&target).unwrap();let started=Instant::now();let caller=std::thread::current().id();let mut callbacks=0;
            if mode=="optimized"{copy_verified(&sample,&target,&AtomicBool::new(false),&mut|_,_,_|{assert_eq!(std::thread::current().id(),caller);callbacks+=1;}).unwrap();}
            else{for input in &files{let relative=input.strip_prefix(&sample).unwrap();let output=target.join(relative);fs::create_dir_all(output.parent().unwrap()).unwrap();
                let mut reader=fs::File::open(input).unwrap();let mut writer=fs::File::create(&output).unwrap();let mut buffer=vec![0;65536];
                loop{let count=reader.read(&mut buffer).unwrap();if count==0{break;}writer.write_all(&buffer[..count]).unwrap();callbacks+=1;}
                writer.sync_all().unwrap();drop(writer);assert_eq!(file_hash(input).unwrap(),file_hash(&output).unwrap());
            }}
            let elapsed=started.elapsed().as_secs_f64();atomic_json(&target.join("manifest.json"),&manifest).unwrap();crate::workspace::verify_tree(&target,"manifest.json").unwrap();
            rounds.push(json!({"round":round+1,"mode":mode,"seconds":elapsed,"callbacks":callbacks,"full_target_validation":true}));
        }}
        let cancel=AtomicBool::new(false);let started=Instant::now();let result=copy_verified(&sample,&run.join("cancelled"),&cancel,&mut|phase,_,_|{if phase=="正在复制并校验"{cancel.store(true,Ordering::Release);}});let cancel_seconds=started.elapsed().as_secs_f64();assert!(result.unwrap_err().contains("取消"));
        let mut validation_callbacks=0;let started=Instant::now();crate::workspace::verify_tree_with_progress(&source,"manifest.json",&AtomicBool::new(false),&mut|_,_,_|validation_callbacks+=1).unwrap();let full_validation_seconds=started.elapsed().as_secs_f64();
        assert_eq!(file_hash(&source.join("manifest.json")).unwrap(),original_manifest);
        atomic_json(&report,&json!({"success":true,"source":source,"sample_files":files.len(),"sample_bytes":sample_bytes,"rounds":rounds,"cancel_seconds":cancel_seconds,"full_source_validation_seconds":full_validation_seconds,"validation_callbacks":validation_callbacks,"source_manifest_preserved":true,
            "fields":{"source":"只读真实运行环境","sample_files":"实测样本文件数","sample_bytes":"样本字节数","rounds":"反转执行顺序的两轮完整复制对照","mode":"旧实现或优化实现","seconds":"完整复制并读回复核的秒数","callbacks":"进度回调次数","full_target_validation":"复制后再次独立完整复核","cancel_seconds":"取消样本复制并收集线程的秒数","full_source_validation_seconds":"真实源组件的完整集合及摘要核验秒数","source_manifest_preserved":"原组件清单摘要保持不变"}})).unwrap();
        crate::workspace::remove_owned(&run,owner).unwrap();
    }

    #[test]
    fn receipt_digest_matches_python_sorted_utf8_json_with_chinese_paths() {
        let digest=receipt_digest("voice-model",&[json!({
            "path":"模型/声纹.bin","size":7,"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","optional":false
        })]).unwrap();
        assert_eq!(digest,"5b67667fafbd99fcf6d2a71aea4f65f4a9540c3377a1e12f99fa5100cab209e9");
    }

    #[test]
    fn startup_requires_complete_files_and_shared_dependencies() {
        let complete=json!({"resource_id":"voice-server","label":"语音服务","can_start":true});
        let missing=json!({"resource_id":"paraformer-streaming","label":"流式识别模型","files_installed":false,"can_start":false,"error":"模型文件不完整"});
        let update=json!({"resource_id":"streaming-punctuation","label":"流式标点","files_installed":true,"can_start":false,"update_available":true});
        let missing_runtime=json!({"resource_id":"streaming-punctuation","label":"流式标点","files_installed":true,"runtime_ready":false,"can_start":false});
        let readiness=readiness_from_inventory(&[complete.clone(),missing,missing_runtime]);
        assert_eq!(readiness["ready"],false);
        assert_eq!(readiness["checked_components"],3);
        assert_eq!(readiness["missing"][0]["resource_id"],"paraformer-streaming");
        assert_eq!(readiness["missing"][0]["reason"],"模型文件不完整");
        assert_eq!(readiness["missing"][1]["reason"],"组件文件或共享运行依赖尚未完备");
        let update_readiness=readiness_from_inventory(&[complete.clone(),update]);
        assert_eq!(update_readiness["ready"],false);
        assert_eq!(update_readiness["missing"][0]["reason"],"组件有更新，需先完成更新");
        assert_eq!(readiness_from_inventory(&[complete])["ready"],true);
        assert_eq!(readiness_from_inventory(&[])["ready"],false);
    }

    #[test]
    fn dsh_activation_requires_exact_version_clear_update_flag_and_start_readiness(){
        let ready=json!({"installed_version":"0.2.1-rc.2","update_available":false,"can_start":true});
        assert!(validate_dsh_activation_state(&ready,"0.2.1-rc.2").is_ok());
        for changed in [
            json!({"installed_version":"0.2.0-rc.2","update_available":false,"can_start":true}),
            json!({"installed_version":"0.2.1-rc.2","update_available":true,"can_start":true}),
            json!({"installed_version":"0.2.1-rc.2","update_available":false,"can_start":false}),
        ] {assert!(validate_dsh_activation_state(&changed,"0.2.1-rc.2").is_err());}
    }

    #[test]
    fn optional_missing_or_outdated_components_do_not_block_startup() {
        let server=json!({"resource_id":"voice-server","required":true,"can_start":true});
        let catalog:Value=serde_json::from_str(include_str!("../../../../config/resources/catalog.json")).unwrap();
        for id in ["emotion2vec-plus-base","yamnet"] {
            let spec=catalog["resources"].as_array().unwrap().iter().find(|item|item["resource_id"]==id).unwrap();
            assert_eq!(spec["required"],false);
            assert_eq!(spec["download_in_required_batch"],false);
            for optional in [json!({"resource_id":id,"required":false,"can_start":false,"files_installed":false}),
                json!({"resource_id":id,"required":false,"can_start":false,"update_available":true})] {
                let readiness=readiness_from_inventory(&[server.clone(),optional]);
                assert_eq!(readiness["ready"],true);assert_eq!(readiness["missing"],json!([]));
            }
        }
    }

    #[test]
    fn startup_is_lightweight_and_maintenance_detects_same_size_model_corruption() {
        let owner=crate::canonical_path(&std::env::temp_dir()).unwrap();
        let root=owner.join(format!("voice-integrity-resources-{}",crate::workspace::new_id()));
        crate::workspace::initialize(&root).unwrap();
        fs::create_dir_all(root.join("app/config/resources")).unwrap();
        fs::create_dir_all(root.join("app/scripts")).unwrap();
        fs::write(root.join("app/config/default.yaml"),b"{}").unwrap();
        fs::write(root.join("app/scripts/backend-entry.py"),"# 测试启动上下文，不运行此脚本").unwrap();
        let source=root.join("tmp/source");fs::create_dir_all(&source).unwrap();
        let expected=vec![0x41u8;4096];fs::write(source.join("model.bin"),&expected).unwrap();
        let digest=file_hash(&source.join("model.bin")).unwrap();
        atomic_json(&root.join("app/config/resources/catalog.json"),&json!({"resources":[{
            "resource_id":"test-model","title":"测试模型","kind":"model","version":"1","destination":"test-model",
            "files":[{"path":"model.bin","size_bytes":expected.len(),"sha256":digest}]
        }]})).unwrap();
        ready_test_runtime(&root);
        let context=LaunchContext::portable(&root).unwrap();
        import_review_with_activation(&context,&source,Some("test-model"),Arc::new(AtomicBool::new(false)),&mut|_,_,_|{},&mut|_|Ok(()),&mut|stage,relative|activate(&context,stage,relative)).unwrap();
        let target=spec(&context,"test-model").unwrap();
        {
            let _session=crate::verification::VerificationSession::begin();
            prepare_maintenance_verification(&context,&AtomicBool::new(false),&mut|_,_,_|{}).unwrap();
            for _ in 0..2{
                let mut reread=false;
                validate_model_files_with_progress(&context,&target,&root.join("models/test-model"),false,&AtomicBool::new(false),&mut|phase,_,_|reread|=phase=="校验组件文件内容").unwrap();
                assert!(!reread,"全面维护的模型修复和复核应复用同轮验证结果");
            }
        }
        let installed=root.join("models/test-model/model.bin");
        fs::write(&installed,vec![0x42u8;expected.len()]).unwrap(); // 相同文件长度，仅 SHA-256（内容摘要）不同。
        let readiness=startup_readiness(&context).unwrap();
        assert_eq!(readiness["ready"],true);
        assert!(current_for_upgrade(&context,&target));
        assert!(model_receipt_current(&root.join("models/test-model"),&target));
        assert!(model_validate(&context,&target,&root.join("models/test-model")).is_err());
        fs::remove_file(&installed).unwrap();
        assert!(!current_for_upgrade(&context,&target));
        assert!(!model_receipt_current(&root.join("models/test-model"),&target));
        assert_eq!(root.parent(),Some(owner.as_path()));crate::workspace::remove_owned(&root,&owner).unwrap();
    }

    // Ready test runtime（就绪运行库替身）：仅用于门禁检查，不启动解释器或业务服务。
    fn ready_test_runtime(root:&Path){
        fs::create_dir_all(root.join("app/config/runtime")).unwrap();
        // 就绪工作区同时声明程序身份；缺少发布清单应由兼容门禁单独阻止启动。
        if !root.join("app/config/compatibility.json").exists(){
            let policy=root.join("app/config/compatibility.json");fs::write(&policy,include_bytes!("../../../../config/compatibility.json")).unwrap();
            let mut release=load(&root.join("app/release.json")).unwrap_or(json!({"build":"test-program"}));
            release["compatibility_engine"]=json!({"schema":1,"rules_file":"config/compatibility.json"});
            release["files"]=json!([{"path":"config/compatibility.json","size":fs::metadata(&policy).unwrap().len(),"sha256":file_hash(&policy).unwrap()}]);
            atomic_json(&root.join("app/release.json"),&release).unwrap();
        }
        let lock=root.join("app/config/runtime/voice-runtime.lock.json");
        atomic_json(&lock,&json!({"schema":1})).unwrap();
        let runtime=root.join("runtimes/python");fs::create_dir_all(&runtime).unwrap();
        fs::write(runtime.join("python.exe"),b"not-executed").unwrap();
        atomic_json(&runtime.join("runtime.json"),&json!({"schema":2,"python":"3.12.10","lock_sha256":file_hash(&lock).unwrap(),
            "groups":["voice-core","torch-cpu","onnx-cpu","sherpa-cpu","modelscope-download","onnx-vad"]})).unwrap();
        atomic_json(&runtime.join("manifest.json"),&manifest(&runtime,"python","test-runtime").unwrap()).unwrap();
    }

    #[test]
    fn startup_quickly_checks_runtime_inventory_and_cancellation_preserves_components(){
        let owner=crate::canonical_path(&std::env::temp_dir()).unwrap();let root=owner.join(format!("voice-startup-完整校验-{}",crate::workspace::new_id()));
        crate::workspace::initialize(&root).unwrap();fs::create_dir_all(root.join("app/config/resources")).unwrap();fs::create_dir_all(root.join("app/scripts")).unwrap();
        fs::write(root.join("app/config/default.yaml"),b"{}").unwrap();fs::write(root.join("app/scripts/backend-entry.py"),b"# not executed").unwrap();
        atomic_json(&root.join("app/config/resources/catalog.json"),&json!({"resources":[]})).unwrap();ready_test_runtime(&root);let context=LaunchContext::portable(&root).unwrap();
        assert_eq!(startup_readiness(&context).unwrap()["ready"],true);let before=file_hash(&root.join("runtimes/python/manifest.json")).unwrap();
        let cancel=AtomicBool::new(false);let caller=std::thread::current().id();
        let mut component_reports=0;let result=startup_readiness_with_progress(&context,&cancel,&mut|phase,_,_|{assert_eq!(std::thread::current().id(),caller);if phase=="检查必要入口与兼容状态"{component_reports+=1;cancel.store(true,Ordering::Release);}});
        assert!(result.unwrap_err().contains("取消"));assert_eq!(file_hash(&root.join("runtimes/python/manifest.json")).unwrap(),before);
        fs::write(root.join("runtimes/python/python.exe"),b"same-length!").unwrap();
        assert_eq!(inventory(&context).unwrap()[0]["can_start"],true);let readiness=startup_readiness(&context).unwrap();
        assert_eq!(readiness["ready"],true);
        fs::write(root.join("runtimes/python/python.exe"),b"different-size").unwrap();
        let readiness=startup_readiness(&context).unwrap();
        assert_eq!(readiness["ready"],true);
        assert!(crate::workspace::verify_tree(&root.join("runtimes/python"),"manifest.json").is_err());
        crate::workspace::remove_owned(&root,&owner).unwrap();
    }
    #[test]
    fn broken_model_receipt_is_rebuilt_from_valid_local_files_without_download(){
        let owner=crate::canonical_path(&std::env::temp_dir()).unwrap();
        let root=owner.join(format!("voice-local-receipt-{}",crate::workspace::new_id()));
        crate::workspace::initialize(&root).unwrap();ready_test_runtime(&root);
        fs::create_dir_all(root.join("app/config/resources")).unwrap();fs::create_dir_all(root.join("app/scripts")).unwrap();
        fs::write(root.join("app/config/default.yaml"),b"{}").unwrap();fs::write(root.join("app/scripts/backend-entry.py"),b"# not executed").unwrap();
        let component=root.join("models/test-model");fs::create_dir_all(&component).unwrap();fs::write(component.join("model.bin"),b"good").unwrap();
        let item=json!({"resource_id":"test-model","kind":"model","version":"1","destination":"test-model",
            "files":[{"path":"model.bin","size_bytes":4,"sha256":file_hash(&component.join("model.bin")).unwrap()}]});
        // 此微型清单没有网络来源；发生下载会立即失败。
        atomic_json(&root.join("app/config/resources/catalog.json"),&json!({"resources":[item.clone()]})).unwrap();
        fs::write(component.join(MODEL_RECEIPT),b"{broken receipt").unwrap();
        let context=LaunchContext::portable(&root).unwrap();let _session=crate::verification::VerificationSession::begin();
        assert!(!current_for_upgrade(&context,&item));
        prepare_maintenance_verification(&context,&AtomicBool::new(false),&mut|_,_,_|{}).unwrap();
        let mut rebuilt=false;
        download_resource(&context,"test-model",Arc::new(AtomicBool::new(false)),&mut|phase,_,_|rebuilt|=phase=="组件内容完整，使用本机文件修复安装记录").unwrap();
        assert!(rebuilt);assert!(current_for_upgrade(&context,&item));
        assert_eq!(fs::read(component.join("model.bin")).unwrap(),b"good");
        crate::verification::ensure_unchanged().unwrap();
        crate::workspace::remove_owned(&root,&owner).unwrap();
    }

    #[test]
    fn current_requirements_override_optional_flags_in_old_receipts(){
        let owner=crate::canonical_path(&std::env::temp_dir()).unwrap();
        let root=owner.join(format!("voice-required-file-{}",crate::workspace::new_id()));
        crate::workspace::initialize(&root).unwrap();ready_test_runtime(&root);
        fs::create_dir_all(root.join("app/config/resources")).unwrap();fs::create_dir_all(root.join("app/scripts")).unwrap();
        fs::write(root.join("app/config/default.yaml"),b"{}").unwrap();fs::write(root.join("app/scripts/backend-entry.py"),b"# not executed").unwrap();
        let component=root.join("models/test-model");fs::create_dir_all(&component).unwrap();
        fs::write(component.join("cpu.bin"),b"cpu").unwrap();fs::write(component.join("gpu.bin"),b"gpu").unwrap();
        let item=json!({"resource_id":"test-model","kind":"model","version":"1","destination":"test-model","files":[
            {"path":"cpu.bin","size_bytes":3,"sha256":file_hash(&component.join("cpu.bin")).unwrap()},
            {"path":"gpu.bin","size_bytes":3,"sha256":file_hash(&component.join("gpu.bin")).unwrap()}]});
        write_model_receipt(&component,&item,item["files"].as_array().unwrap().iter().map(|row|
            json!({"path":row["path"],"size":row["size_bytes"],"sha256":row["sha256"],"optional":row["path"]=="gpu.bin"})).collect()).unwrap();
        atomic_json(&root.join("app/config/resources/catalog.json"),&json!({"resources":[item.clone()]})).unwrap();
        let context=LaunchContext::portable(&root).unwrap();
        assert!(current_for_upgrade(&context,&item));assert!(model_receipt_current(&component,&item));
        fs::remove_file(component.join("gpu.bin")).unwrap();
        assert!(!current_for_upgrade(&context,&item));assert!(!model_receipt_current(&component,&item));
        assert_eq!(inventory(&context).unwrap()[1]["can_start"],false);
        let mut optional=item;optional["files"][1]["optional"]=json!(true);
        assert!(current_for_upgrade(&context,&optional));assert!(model_receipt_current(&component,&optional));
        crate::workspace::remove_owned(&root,&owner).unwrap();
    }

    #[test]
    fn gpu_choice_installs_missing_optional_weights_and_later_updates_preserve_them(){
        let owner=crate::canonical_path(&std::env::temp_dir()).unwrap();let root=owner.join(format!("voice-gpu-model-update-{}",crate::workspace::new_id()));
        crate::workspace::initialize(&root).unwrap();ready_test_runtime(&root);
        fs::create_dir_all(root.join("app/config/resources")).unwrap();fs::create_dir_all(root.join("app/scripts")).unwrap();
        fs::write(root.join("app/config/default.yaml"),b"{}").unwrap();fs::write(root.join("app/scripts/backend-entry.py"),b"# not executed").unwrap();
        let source=root.join("tmp/source");fs::create_dir_all(&source).unwrap();fs::write(source.join("cpu.onnx"),b"cpu model").unwrap();
        let cached=root.join("tmp/resource-downloads/smart-turn");fs::create_dir_all(&cached).unwrap();let gpu=cached.join("smart-turn-gpu.onnx");fs::write(&gpu,b"gpu model").unwrap();
        let mut item=json!({"resource_id":"smart-turn","title":"结束判断模型","kind":"model","version":"1","destination":"analysis/smart-turn",
            "source":{"platform":"huggingface","repo_id":"unused","revision":"fixed"},"files":[
                {"path":"cpu.onnx","size_bytes":9,"sha256":file_hash(&source.join("cpu.onnx")).unwrap()},
                {"path":"smart-turn-gpu.onnx","size_bytes":9,"sha256":file_hash(&gpu).unwrap(),"optional":true}]});
        let catalog=root.join("app/config/resources/catalog.json");atomic_json(&catalog,&json!({"resources":[item.clone()]})).unwrap();
        let context=LaunchContext::portable(&root).unwrap();let cancel=Arc::new(AtomicBool::new(false));
        assert_eq!(spec(&context,"smart-turn").unwrap()["files"][1]["optional"],true);
        import_review(&context,&source,Some("smart-turn"),cancel.clone(),&mut|_,_,_|{},&mut|_|Ok(())).unwrap();
        let settings=context.data.join("config/product-settings.json");atomic_json(&settings,&json!({"compute":{"devices":{"smart_turn":"cuda"}}})).unwrap();
        assert_eq!(spec(&context,"smart-turn").unwrap()["files"][1]["optional"],false);
        let rows=inventory(&context).unwrap();assert_eq!(rows[1]["update_available"],true);assert_eq!(rows[1]["can_start"],false);
        // 所需文件都在固定缓存或旧收据中；此路径必须完整启用候选，不访问网络。
        assert_eq!(download_resource(&context,"smart-turn",cancel.clone(),&mut|_,_,_|{}).unwrap(),vec!["smart-turn"]);
        let installed=root.join("models/analysis/smart-turn/smart-turn-gpu.onnx");assert_eq!(fs::read(&installed).unwrap(),b"gpu model");assert_eq!(inventory(&context).unwrap()[1]["can_start"],true);
        atomic_json(&settings,&json!({"compute":{"devices":{"smart_turn":"cpu"}}})).unwrap();item["version"]=json!("2");atomic_json(&catalog,&json!({"resources":[item]})).unwrap();
        assert_eq!(spec(&context,"smart-turn").unwrap()["files"][1]["optional"],false);
        download_resource(&context,"smart-turn",cancel,&mut|_,_,_|{}).unwrap();assert_eq!(fs::read(&installed).unwrap(),b"gpu model");
        assert_eq!(root.parent(),Some(owner.as_path()));crate::workspace::remove_owned(&root,&owner).unwrap();
    }

    #[test]
    fn component_copy_reuses_buffers_verifies_targets_and_cancels_before_activation(){
        let root=std::env::temp_dir().join(format!("voice-copy-组件-{}",crate::workspace::new_id()));let source=root.join("源 目录");let target=root.join("目标 目录");fs::create_dir_all(&source).unwrap();
        for index in 0..80{fs::write(source.join(format!("{index}.bin")),vec![index as u8;index*64]).unwrap();}fs::write(source.join("large.bin"),vec![21u8;8*1024*1024]).unwrap();
        let cancel=AtomicBool::new(false);let caller=std::thread::current().id();let mut samples=vec![];
        copy_verified(&source,&target,&cancel,&mut|_,done,total|{assert_eq!(std::thread::current().id(),caller);assert!(done<=total);if total>0{samples.push(done);}}).unwrap();
        assert!(samples.windows(2).all(|pair|pair[0]<=pair[1]));assert_eq!(walk(&source).unwrap().len(),walk(&target).unwrap().len());
        for path in walk(&source).unwrap(){assert_eq!(file_hash(&path).unwrap(),file_hash(&target.join(path.file_name().unwrap())).unwrap());}
        let cancelled=root.join("取消候选");let result=copy_verified(&source,&cancelled,&cancel,&mut|phase,_,_|{if phase=="正在复制并校验"{cancel.store(true,Ordering::Release);}});
        assert!(result.unwrap_err().contains("取消"));assert_eq!(file_hash(&source.join("large.bin")).unwrap(),file_hash(&target.join("large.bin")).unwrap());fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn model_update_reuses_unchanged_weights_without_network_and_removes_old_files(){
        let owner=crate::canonical_path(&std::env::temp_dir()).unwrap();let root=owner.join(format!("voice-update-复用-{}",crate::workspace::new_id()));crate::workspace::initialize(&root).unwrap();
        fs::create_dir_all(root.join("app/config/resources/assets")).unwrap();fs::create_dir_all(root.join("app/scripts")).unwrap();fs::write(root.join("app/config/default.yaml"),b"{}").unwrap();fs::write(root.join("app/scripts/backend-entry.py"),b"# not executed").unwrap();
        let source=root.join("tmp/source");fs::create_dir_all(&source).unwrap();fs::write(source.join("model.bin"),vec![13u8;1024*1024]).unwrap();
        let mut resource=json!({"resource_id":"reuse-model","title":"复用测试模型","kind":"model","version":"1","destination":"reuse-model",
            "source":{"platform":"huggingface","repo_id":"offline-fixture/must-use-verified-cache","revision":"fixed"},"files":[{"path":"model.bin","size_bytes":1024*1024,"sha256":file_hash(&source.join("model.bin")).unwrap()}]});
        atomic_json(&root.join("app/config/resources/catalog.json"),&json!({"resources":[resource.clone()]})).unwrap();let context=LaunchContext::portable(&root).unwrap();
        import_review_with_activation(&context,&source,Some("reuse-model"),Arc::new(AtomicBool::new(false)),&mut|_,_,_|{},&mut|_|Ok(()),&mut|stage,relative|activate(&context,stage,relative)).unwrap();
        let old=root.join("models/reuse-model/obsolete.bin");fs::write(&old,b"removed by new version").unwrap();let asset=root.join("app/config/resources/assets/new.json");fs::write(&asset,b"{\"new\":true}").unwrap();resource["version"]=json!("2");resource["files"].as_array_mut().unwrap().push(json!({"path":"new.json","asset":"assets/new.json","size_bytes":asset.metadata().unwrap().len(),"sha256":file_hash(&asset).unwrap()}));
        atomic_json(&root.join("app/config/resources/catalog.json"),&json!({"resources":[resource]})).unwrap();let mut reused=false;
        let installed=download_resource(&context,"reuse-model",Arc::new(AtomicBool::new(false)),&mut|phase,_,_|{if phase.starts_with("复用现有模型"){reused=true;}}).unwrap();
        assert_eq!(installed,vec!["reuse-model"]);assert!(reused);assert!(!old.exists());assert_eq!(load(&root.join("models/reuse-model/resource-install.json")).unwrap()["version"],"2");
        model_validate(&context,&spec(&context,"reuse-model").unwrap(),&root.join("models/reuse-model")).unwrap();crate::workspace::remove_owned(&root,&owner).unwrap();
    }

    #[test]
    fn pending_updates_defer_full_hashes_until_all_components_are_current(){
        let owner=crate::canonical_path(&std::env::temp_dir()).unwrap();
        let root=owner.join(format!("voice-deferred-integrity-{}",crate::workspace::new_id()));
        crate::workspace::initialize(&root).unwrap();
        fs::create_dir_all(root.join("app/config/resources")).unwrap();
        fs::create_dir_all(root.join("app/scripts")).unwrap();
        fs::write(root.join("app/config/default.yaml"),b"{}").unwrap();
        fs::write(root.join("app/scripts/backend-entry.py"),"# 测试门禁，不启动业务").unwrap();
        ready_test_runtime(&root);
        let source=root.join("tmp/source");fs::create_dir_all(&source).unwrap();
        fs::write(source.join("model.bin"),b"old-valid-content").unwrap();
        let catalog=root.join("app/config/resources/catalog.json");
        let mut spec=json!({"resource_id":"test-model","title":"测试模型","kind":"model","version":"1","destination":"test-model",
            "files":[{"path":"model.bin","size_bytes":17,"sha256":file_hash(&source.join("model.bin")).unwrap()}]});
        atomic_json(&catalog,&json!({"resources":[spec.clone()]})).unwrap();
        let context=LaunchContext::portable(&root).unwrap();
        import_review_with_activation(&context,&source,Some("test-model"),Arc::new(AtomicBool::new(false)),&mut|_,_,_|{},&mut|_|Ok(()),&mut|stage,relative|activate(&context,stage,relative)).unwrap();
        fs::write(root.join("models/test-model/model.bin"),b"bad-equal-length!").unwrap();
        spec["version"]=json!("2");atomic_json(&catalog,&json!({"resources":[spec.clone()]})).unwrap();
        let pending=startup_readiness(&context).unwrap();
        assert_eq!(pending["ready"],false);
        assert!(pending["missing"].as_array().unwrap().iter().any(|row|row["resource_id"]=="test-model"&&row["reason"].as_str().unwrap().contains("更新")));
        spec["version"]=json!("1");atomic_json(&catalog,&json!({"resources":[spec]})).unwrap();
        let corrupted=startup_readiness(&context).unwrap();
        assert_eq!(corrupted["ready"],true);
        assert!(model_validate(&context,&super::spec(&context,"test-model").unwrap(),&root.join("models/test-model")).is_err());
        assert_eq!(root.parent(),Some(owner.as_path()));crate::workspace::remove_owned(&root,&owner).unwrap();
    }

    #[test]
    fn dsh_cache_reuse_requires_full_hashes_and_supports_cancel() {
        let owner=crate::canonical_path(&std::env::temp_dir()).unwrap();
        let root=owner.join(format!("voice-dsh-cache-{}",crate::workspace::new_id()));
        crate::workspace::initialize(&root).unwrap();
        fs::create_dir_all(root.join("app/config/resources")).unwrap();
        fs::create_dir_all(root.join("app/config/dsh")).unwrap();
        fs::create_dir_all(root.join("app/scripts")).unwrap();
        fs::write(root.join("app/config/default.yaml"),b"{}").unwrap();
        fs::write(root.join("app/scripts/backend-entry.py"),"# 只测试缓存校验，不启动服务").unwrap();
        let installed=root.join("runtimes/dsh");fs::create_dir_all(&installed).unwrap();
        let executable=installed.join("test.exe");fs::write(&executable,vec![0x41;4*1024*1024]).unwrap();
        let sha512="00".repeat(64);
        atomic_json(&installed.join("runtime.json"),&json!({"source_kind":"official_desktop","version":"test","source_sha512":sha512})).unwrap();
        atomic_json(&installed.join("manifest.json"),&manifest(&installed,"dsh","test").unwrap()).unwrap();
        atomic_json(&root.join("app/config/dsh/official-runtime.lock.json"),&json!({"version":"test","source_sha512":sha512,"manifest_sha256":file_hash(&installed.join("manifest.json")).unwrap()})).unwrap();
        atomic_json(&root.join("app/config/resources/catalog.json"),&json!({"resources":[{"resource_id":"dsh","kind":"dsh","destination":"runtimes/dsh","version":"test","source":{"transfer":{"artifact":{
            "path":"test.exe","url":"file://blocked-test-source","size_bytes":1,"sha512_base64":base64::engine::general_purpose::STANDARD.encode([0u8;64])
        }}}}]})).unwrap();
        let context=LaunchContext::portable(&root).unwrap();let mut content_progress=false;
        assert_eq!(download_resource(&context,"dsh",Arc::new(AtomicBool::new(false)),&mut|phase,_,_|{content_progress|=phase=="校验组件文件内容";}).unwrap(),vec!["dsh"]);
        assert!(content_progress);
        let cancel=AtomicBool::new(false);
        let error=dsh_validate_with_progress(&context,&installed,true,&cancel,&mut|phase,_,_|{if phase=="校验组件文件内容"{cancel.store(true,Ordering::Release);}}).unwrap_err();
        assert!(error.contains("取消"));assert!(executable.is_file());
        fs::write(&executable,vec![0x42;4*1024*1024]).unwrap();
        dsh_validate(&context,&installed,false).unwrap();
        // 同长度损坏不能被当成缓存命中；无效来源让该用例立即失败，不发出网络请求。
        assert!(download_resource(&context,"dsh",Arc::new(AtomicBool::new(false)),&mut|_,_,_|{}).is_err());
        crate::workspace::remove_owned(&root,&owner).unwrap();
    }

    #[test]
    fn npm_migration_does_not_hash_known_incompatible_old_component(){
        let owner=crate::canonical_path(&std::env::temp_dir()).unwrap();let root=owner.join(format!("voice-dsh-target-first-{}",crate::workspace::new_id()));
        crate::workspace::initialize(&root).unwrap();fs::create_dir_all(root.join("app/scripts")).unwrap();fs::create_dir_all(root.join("app/config")).unwrap();fs::write(root.join("app/config/default.yaml"),b"{}").unwrap();
        fs::write(root.join("app/scripts/backend-entry.py"),"# 只测试更新前检查").unwrap();
        let installed=root.join("runtimes/dsh");fs::create_dir_all(&installed).unwrap();fs::write(installed.join("old.exe"),vec![0x41;1024]).unwrap();
        let sha512="00".repeat(64);atomic_json(&installed.join("runtime.json"),&json!({"source_kind":"official_desktop","version":"old","source_sha512":sha512})).unwrap();
        atomic_json(&installed.join("manifest.json"),&manifest(&installed,"dsh","old").unwrap()).unwrap();let old_hash=file_hash(&installed.join("manifest.json")).unwrap();
        atomic_json(&root.join("app/config/dsh/official-runtime.lock.json"),&json!({"version":"old","source_sha512":sha512,"manifest_sha256":old_hash})).unwrap();
        let lock:Value=serde_json::from_str(include_str!("../../../../config/dsh/runtime.lock.json")).unwrap();atomic_json(&root.join("app/config/dsh/runtime.lock.json"),&lock).unwrap();
        atomic_json(&root.join("app/config/resources/catalog.json"),&json!({"resources":[{"resource_id":"dsh","kind":"dsh","destination":"runtimes/dsh","origin":"npm_package","version":lock["version"]}]})).unwrap();
        // 缺少项目插件记录在安装前失败，不发出网络请求；此前逻辑会先完整读取旧组件。
        let context=LaunchContext::portable(&root).unwrap();let mut hashed=false;
        assert!(download_resource(&context,"dsh",Arc::new(AtomicBool::new(false)),&mut|phase,_,_|hashed|=phase=="校验组件文件内容").is_err());
        assert!(!hashed);assert_eq!(file_hash(&installed.join("manifest.json")).unwrap(),old_hash);crate::workspace::remove_owned(&root,&owner).unwrap();
    }

    #[test]
    fn legacy_dsh_is_installed_but_requires_update() {
        let owner=crate::canonical_path(&std::env::temp_dir()).unwrap();
        let root=owner.join(format!("voice-dsh-update-{}",crate::workspace::new_id()));
        crate::workspace::initialize(&root).unwrap();
        fs::create_dir_all(root.join("app/config/resources")).unwrap();
        fs::create_dir_all(root.join("app/scripts")).unwrap();
        fs::write(root.join("app/config/default.yaml"),b"{}").unwrap();
        fs::write(root.join("app/scripts/backend-entry.py"),"# 测试启动上下文，不运行此脚本").unwrap();
        let installed=root.join("runtimes/dsh");fs::create_dir_all(&installed).unwrap();
        fs::write(installed.join("legacy.exe"),b"old-dsh").unwrap();
        atomic_json(&installed.join("runtime.json"),&json!({"source_kind":"legacy_export","version":"0.1.0"})).unwrap();
        atomic_json(&installed.join("manifest.json"),&manifest(&installed,"dsh","legacy").unwrap()).unwrap();
        let digest=file_hash(&installed.join("manifest.json")).unwrap();
        atomic_json(&root.join("app/config/resources/catalog.json"),&json!({"resources":[{
            "resource_id":"dsh","title":"代理宿主","kind":"dsh","version":"0.2.1-rc.2","destination":"runtimes/dsh",
            "source":{"transfer":{"artifact":{"sha512_base64":base64::engine::general_purpose::STANDARD.encode([0u8;64])}}},
            "legacy_export":{"supported":true,"manifest_sha256":digest},"files":[]
        }]})).unwrap();
        let context=LaunchContext::portable(&root).unwrap();let rows=inventory(&context).unwrap();let row=rows.iter().find(|row|row["resource_id"]=="dsh").unwrap();
        assert_eq!(row["installation_state"],"installed");assert_eq!(row["files_installed"],true);
        assert_eq!(row["installed_version"],"0.1.0");assert_eq!(row["version"],"0.2.1-rc.2");
        assert_eq!(row["update_available"],true);assert_eq!(row["can_start"],false);
        assert_eq!(root.parent(),Some(owner.as_path()));crate::workspace::remove_owned(&root,&owner).unwrap();
    }

    #[test]
    fn parallel_imports_keep_independent_stages_and_both_installed_models() {
        use std::{sync::{Mutex,Condvar},time::Duration};
        let owner=crate::canonical_path(&std::env::temp_dir()).unwrap();
        let root=owner.join(format!("voice-parallel-resources-{}",crate::workspace::new_id()));
        crate::workspace::initialize(&root).unwrap();
        fs::create_dir_all(root.join("app/config/resources")).unwrap();
        fs::create_dir_all(root.join("app/scripts")).unwrap();
        fs::write(root.join("app/config/default.yaml"),b"{}").unwrap();
        fs::write(root.join("app/scripts/backend-entry.py"),"# 测试启动上下文，不运行此脚本").unwrap();
        let mut entries=vec![];
        for (id,marker) in [("first-model",1u8),("second-model",2u8)] {
            let source=root.join(format!("tmp/source-{id}"));fs::create_dir_all(&source).unwrap();
            let file=source.join("model.bin");fs::write(&file,vec![marker;256*1024]).unwrap();
            entries.push(json!({"resource_id":id,"title":"并行验收模型","kind":"model","version":"1","destination":id,
                "files":[{"path":"model.bin","size_bytes":256*1024,"sha256":file_hash(&file).unwrap()}]}));
        }
        atomic_json(&root.join("app/config/resources/catalog.json"),&json!({"resources":entries})).unwrap();
        let context=LaunchContext::portable(&root).unwrap();
        let copying=(Mutex::new(0usize),Condvar::new());
        let activation=Mutex::new(());let stages=Mutex::new(vec![]);
        std::thread::scope(|scope| {
            let context=&context;let root=&root;let copying=&copying;let activation=&activation;let stages=&stages;
            let threads=["first-model","second-model"].map(|id|scope.spawn(move|| {
                let mut reached=false;
                let installed=import_review_with_activation(&context,&root.join(format!("tmp/source-{id}")),Some(id),Arc::new(AtomicBool::new(false)),
                    &mut|phase,_,_|{
                        if !reached&&phase=="正在复制并校验" {
                            reached=true;let mut count=copying.0.lock().unwrap();*count+=1;copying.1.notify_all();
                            let (count,timeout)=copying.1.wait_timeout_while(count,Duration::from_secs(5),|count|*count<2).unwrap();
                            assert!(!timeout.timed_out()&&*count==2,"两个任务必须同时进入复制阶段");
                        }
                    },&mut|_|Ok(()),&mut|stage,relative|{
                        let _guard=activation.lock().unwrap();stages.lock().unwrap().push(stage.parent().unwrap().to_path_buf());
                        activate(&context,stage,relative)
                    }).unwrap();
                assert_eq!(installed,vec![id]);
            }));
            for thread in threads{thread.join().unwrap();}
        });
        let stages=stages.into_inner().unwrap();assert_eq!(stages.len(),2);assert_ne!(stages[0],stages[1]);
        for id in ["first-model","second-model"] {model_validate(&context,&spec(&context,id).unwrap(),&root.join(format!("models/{id}"))).unwrap();}
        let manifest_files=load(&root.join("models/manifest.json")).unwrap()["files"].as_array().unwrap().clone();
        assert_eq!(manifest_files.iter().filter(|row|row["path"].as_str().is_some_and(|path|!path.ends_with(MODEL_RECEIPT))).count(),2);
        assert_eq!(manifest_files.iter().filter(|row|row["path"].as_str().is_some_and(|path|path.ends_with(MODEL_RECEIPT))).count(),2);
        assert!(!root.join("tmp/native-install.json").exists());
        // 清理仅限本测试创建的独立子目录。
        assert_eq!(root.parent(),Some(owner.as_path()));crate::workspace::remove_owned(&root,&owner).unwrap();
    }

    #[test]
    #[cfg(windows)]
    #[ignore = "手动验收：在指定工作区同时下载流式识别和精修模型，保留原有数据与下载缓存"]
    fn download_streaming_and_refinement_in_parallel_without_python() {
        use std::{sync::{Mutex,Condvar},time::Duration};
        let root=PathBuf::from(std::env::var_os("VOICE_RUNTIME_TEST_ROOT").expect("请提供 VOICE_RUNTIME_TEST_ROOT（测试工作区）"));
        let mut context=LaunchContext::portable(&root).unwrap();context.python=root.join("不存在的解释器/python.exe");
        let before=crate::runtime::fingerprint(&root.join("runtimes")).unwrap();
        let transfers=(Mutex::new(0usize),Condvar::new());let activation=Mutex::new(());let barrier_failed=AtomicBool::new(false);
        std::thread::scope(|scope| {
            let context=&context;let transfers=&transfers;let activation=&activation;let barrier_failed=&barrier_failed;
            let threads=["paraformer-streaming","sensevoice-small"].map(|id|scope.spawn(move|| {
                let mut entered=false;let mut previous=String::new();let mut milestone=0;
                let installed=download_resource_with_activation(&context,id,Arc::new(AtomicBool::new(false)),&mut|phase,done,total|{
                    if phase=="正在下载模型文件"&&!entered {
                        entered=true;let mut count=transfers.0.lock().unwrap();*count+=1;transfers.1.notify_all();
                        let (count,timeout)=transfers.1.wait_timeout_while(count,Duration::from_secs(30),|count|*count<2).unwrap();
                        if timeout.timed_out()||*count!=2{barrier_failed.store(true,Ordering::Release);}
                    }
                    if previous!=phase||done/(50*1024*1024)>milestone {
                        println!("{id}（模型编号）：{phase}，{done} / {total}（字节）");previous=phase.into();milestone=done/(50*1024*1024);
                    }
                },&mut|stage,relative|{let _guard=activation.lock().unwrap();activate(&context,stage,relative)}).unwrap();
                assert_eq!(installed,vec![id]);
                model_validate(&context,&spec(&context,id).unwrap(),&destination(&context,&spec(&context,id).unwrap()).unwrap()).unwrap();
            }));
            for thread in threads{thread.join().unwrap();}
        });
        assert_eq!(*transfers.0.lock().unwrap(),2);
        assert!(!barrier_failed.load(Ordering::Acquire),"两项模型下载必须同时进入传输阶段");
        assert_eq!(before,crate::runtime::fingerprint(&root.join("runtimes")).unwrap());
        assert!(!context.python.exists());println!("两项模型并行下载、独立校验和串行启用通过，Python（运行环境）未被调用或修改");
    }

    #[test]
    fn modelscope_uses_pinned_direct_file_url_with_encoded_query() {
        let model=json!({"source":{"platform":"modelscope","repo_id":"iic/example","revision":"v2.0.4"}});
        let row=json!({"path":"目录/config & data.json"});
        assert_eq!(model_file_url(&model,&row).unwrap(),"https://modelscope.cn/api/v1/models/iic/example/repo?Revision=v2.0.4&FilePath=%E7%9B%AE%E5%BD%95%2Fconfig%20%26%20data.json");
    }

    #[test]
    fn all_pinned_model_files_have_native_download_sources() {
        let catalog:Value=serde_json::from_str(include_str!("../../../../config/resources/catalog.json")).unwrap();
        for model in catalog["resources"].as_array().unwrap().iter().filter(|item|item["kind"]=="model"&&!item["source"]["transfer"]["artifact"].is_object()) {
            if model["source"]["transfer"]["kind"]=="official_audiocpp_patch"{
                for artifact in model["source"]["transfer"]["artifacts"].as_array().unwrap(){
                    assert!(artifact["url"].as_str().unwrap().starts_with("https://github.com/0xShug0/audio.cpp/releases/download/v0.9.1/"));
                    assert!(valid_sha256(artifact["sha256"].as_str().unwrap()));
                }continue;
            }
            for row in model["files"].as_array().unwrap().iter().filter(|row|row["optional"]!=true&&!row["asset"].is_string()) {
                let file=file_spec(row,model_file_url(model,row).unwrap()).unwrap();
                assert!(file.size>0);assert_eq!(file.sha256.len(),64);assert!(file.url.starts_with("https://"));
            }
        }
    }

    #[test]
    #[cfg(windows)]
    #[ignore = "手动验收：只在指定测试工作区下载固定 FSMN-VAD（语音活动检测）模型文件"]
    fn download_model_without_python() {
        let root=PathBuf::from(std::env::var_os("VOICE_RUNTIME_TEST_ROOT").expect("请提供 VOICE_RUNTIME_TEST_ROOT（测试工作区）"));
        let mut context=LaunchContext::portable(&root).unwrap();context.python=root.join("不存在的解释器/python.exe");
        let runtime_before=crate::runtime::fingerprint(&root.join("runtimes")).unwrap();let mut previous=String::new();
        let installed=download_resource(&context,"fsmn-vad",Arc::new(AtomicBool::new(false)),&mut|phase,done,total|{
            if previous!=phase{println!("{phase}：{done} / {total}（字节）");previous=phase.into();}
        }).unwrap();
        assert_eq!(installed,vec!["fsmn-vad"]);
        model_validate(&context,&spec(&context,"fsmn-vad").unwrap(),&destination(&context,&spec(&context,"fsmn-vad").unwrap()).unwrap()).unwrap();
        assert_eq!(runtime_before,crate::runtime::fingerprint(&root.join("runtimes")).unwrap());
        assert!(!context.python.exists());println!("模型下载与校验通过，未调用或修改 Python（运行环境）");
    }
    #[test]
    fn same_size_component_update_reissues_receipt_digest_into_models_manifest(){
        let owner=crate::canonical_path(&std::env::temp_dir()).unwrap();
        let root=owner.join(format!("voice-refresh-receipt-{}",crate::workspace::new_id()));
        crate::workspace::initialize(&root).unwrap();
        let folder=root.join("models/audiocpp/runtime");fs::create_dir_all(&folder).unwrap();fs::write(folder.join("model.bin"),b"aaaa").unwrap();
        let mut item=json!({"resource_id":"tts-audiocpp-runtime","kind":"model","title":"音频运行程序","destination":"audiocpp/runtime",
            "version":"v0.9.1-aaaaaaaaaaaa","files":[{"path":"model.bin","size_bytes":4,"sha256":file_hash(&folder.join("model.bin")).unwrap()}]});
        atomic_json(&root.join("app/config/resources/catalog.json"),&json!({"resources":[item.clone()]})).unwrap();
        write_model_receipt(&folder,&item,vec![json!({"path":"model.bin","size":4,"sha256":item["files"][0]["sha256"],"optional":false})]).unwrap();
        fs::create_dir_all(root.join("app/scripts")).unwrap();fs::write(root.join("app/config/default.yaml"),b"{}").unwrap();fs::write(root.join("app/scripts/backend-entry.py"),b"# fixture").unwrap();
        let context=LaunchContext::portable(&root).unwrap();
        refresh_models(&context,None).unwrap();
        let old_receipt=file_hash(&folder.join(MODEL_RECEIPT)).unwrap();
        // 同尺寸换版：文件集合、每个文件的字节数位数与版本串长度都不变，只有内容摘要变化。
        let candidate=root.join("tmp/resource-downloads/candidate");fs::create_dir_all(&candidate).unwrap();fs::write(candidate.join("model.bin"),b"bbbb").unwrap();
        item["files"][0]["sha256"]=json!(file_hash(&candidate.join("model.bin")).unwrap());
        write_model_receipt(&candidate,&item,vec![json!({"path":"model.bin","size":4,"sha256":item["files"][0]["sha256"],"optional":false})]).unwrap();
        assert_eq!(fs::metadata(folder.join(MODEL_RECEIPT)).unwrap().len(),fs::metadata(candidate.join(MODEL_RECEIPT)).unwrap().len());
        assert_ne!(old_receipt,file_hash(&candidate.join(MODEL_RECEIPT)).unwrap());
        let session=crate::verification::VerificationSession::begin();
        let mut transaction=begin_activation(&context,&candidate,"models/audiocpp/runtime").unwrap();
        commit_activation(&context,&mut transaction).unwrap();
        // 收据不在自己的文件清单里：总清单必须以磁盘内容为准重新登记，不能沿用换版前的摘要。
        let manifest=load(&root.join("models/manifest.json")).unwrap();
        let path="audiocpp/runtime/resource-install.json";
        let record=manifest["files"].as_array().unwrap().iter().find(|row|row["path"]==path).unwrap();
        assert_eq!(record["sha256"],json!(file_hash(&root.join("models").join(path)).unwrap()));
        assert_ne!(record["sha256"],json!(old_receipt));
        crate::verification::ensure_unchanged().unwrap();
        fs::write(folder.join("model.bin"),b"cccc").unwrap();
        let deadline=Instant::now()+Duration::from_secs(2);
        while crate::verification::ensure_unchanged().is_ok(){assert!(Instant::now()<deadline,"新换入组件也必须纳入维护期间的变更检查");std::thread::sleep(Duration::from_millis(10));}
        drop(session);fs::write(folder.join("model.bin"),b"bbbb").unwrap();
        crate::workspace::verify_tree(&root.join("models"),"manifest.json").unwrap();
        crate::workspace::remove_owned(&root,&owner).unwrap();
    }

    #[test]
    fn dsh_install_failures_and_cancellation_never_reach_activation(){
        let root=std::env::temp_dir().join(format!("voice-dsh-candidate-{}",crate::workspace::new_id()));
        fs::create_dir_all(&root).unwrap();
        let marker=root.join("candidate-marker");fs::write(&marker,b"candidate").unwrap();
        let cancel=AtomicBool::new(false);let mut progress=|_:&str,_:u64,_:u64|{};let activations=std::cell::Cell::new(0);
        let mut failed_download=|_:&Path,_:&AtomicBool,_:&mut Progress|Err("模拟下载失败".to_owned());
        let mut enable=|_:&Path,_:&str|{activations.set(activations.get()+1);Ok(())};
        assert!(install_and_activate_dsh(&root,&cancel,&mut progress,&mut failed_download,&mut enable).unwrap_err().contains("下载失败"));
        assert_eq!(activations.get(),0);assert_eq!(fs::read(&marker).unwrap(),b"candidate");

        let mut interrupted_install=|_:&Path,cancel:&AtomicBool,_:&mut Progress|{cancel.store(true,Ordering::Release);Ok(())};
        assert!(install_and_activate_dsh(&root,&cancel,&mut progress,&mut interrupted_install,&mut enable).unwrap_err().contains("取消"));
        assert_eq!(activations.get(),0);assert_eq!(fs::read(&marker).unwrap(),b"candidate");

        cancel.store(false,Ordering::Release);
        let mut completed_install=|_:&Path,_:&AtomicBool,_:&mut Progress|Ok(());
        let failed_activation_calls=std::cell::Cell::new(0);
        let mut failed_activation=|_:&Path,_:&str|{failed_activation_calls.set(failed_activation_calls.get()+1);Err("模拟替换失败".to_owned())};
        assert!(install_and_activate_dsh(&root,&cancel,&mut progress,&mut completed_install,&mut failed_activation).unwrap_err().contains("替换失败"));
        assert_eq!(activations.get(),0);assert_eq!(failed_activation_calls.get(),1);assert_eq!(fs::read(&marker).unwrap(),b"candidate");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn failed_dsh_directory_replacement_restores_the_previous_component(){
        let owner=crate::canonical_path(&std::env::temp_dir()).unwrap();
        let root=owner.join(format!("voice-dsh-swap-{}",crate::workspace::new_id()));
        crate::workspace::initialize(&root).unwrap();
        fs::create_dir_all(root.join("app/config")).unwrap();
        fs::write(root.join("app/config/default.yaml"),b"{}").unwrap();
        fs::create_dir_all(root.join("app/scripts")).unwrap();
        fs::write(root.join("app/scripts/backend-entry.py"),b"# fixture").unwrap();
        let installed=root.join("runtimes/dsh");fs::create_dir_all(&installed).unwrap();
        fs::write(installed.join("runtime.bin"),b"old runtime").unwrap();
        atomic_json(&installed.join("runtime.json"),&json!({"schema":1,"source_kind":"legacy_export","version":"0.2.0-rc.2"})).unwrap();
        atomic_json(&installed.join("manifest.json"),&manifest(&installed,"dsh","old-runtime").unwrap()).unwrap();
        let old_manifest=file_hash(&installed.join("manifest.json")).unwrap();
        let context=LaunchContext::portable(&root).unwrap();
        let missing=root.join("tmp/failed-dsh-candidate");
        assert!(begin_activation(&context,&missing,"runtimes/dsh").is_err());
        assert_eq!(file_hash(&installed.join("manifest.json")).unwrap(),old_manifest);
        assert_eq!(fs::read(installed.join("runtime.bin")).unwrap(),b"old runtime");
        assert!(!root.join("tmp/native-old").exists());
        assert!(!root.join("tmp/native-install.json").exists());
        crate::workspace::remove_owned(&root,&owner).unwrap();
    }
}
