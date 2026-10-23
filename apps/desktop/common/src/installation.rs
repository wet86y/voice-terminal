//! installation（正式本体安装）：统一程序包、可恢复事务与首次窗口交接。
use std::{collections::HashSet,fs,io::{Cursor,Read,Write},path::Path,process::Command,sync::{Arc,atomic::{AtomicBool,Ordering}},time::{Duration,Instant}};
use serde_json::{json,Value};
use sha2::{Digest,Sha256};
use crate::{canonical_path,workspace::{self,atomic_json,safe_path,remove_owned,file_hash}};
pub const ITEMS:&[&str]=&["app","VoiceTerminal.exe","Verify-Package.ps1","使用与环境说明.md"];
pub(crate) const RETIRED_PROFILE_PATHS:&[&str]=&[
    "data/speaker/wespeaker-cnceleb-resnet34", // 旧模型专属档案。
    "data/speaker/registry.json", "data/speaker/templates", "data/speaker/probe.npy", // 已确认属于WeSpeaker的旧根档案。
];
const LEGACY_MODEL:&str="models/speaker/wespeaker-cnceleb-resnet34"; // 旧声纹模型资源。
const CURRENT_MODEL:&str="models/speaker/campplus-zh-cn-common"; // 新声纹模型资源。
pub type Progress<'a>=dyn FnMut(&str,u64,u64)+'a; // Progress（阶段及字节进度回调）。

// WorkspaceGuard（工作区互斥句柄）：产品运行与本体替换共用，避免检查后再次启动。
pub struct WorkspaceGuard { _file:fs::File }
impl WorkspaceGuard {
    pub fn acquire(root:&Path)->Result<Self,String>{
        let path=safe_path(root,"tmp/program.lock")?;
        fs::create_dir_all(path.parent().unwrap()).map_err(|e|e.to_string())?;
        let mut options=fs::OpenOptions::new();options.write(true).read(true).create(true).truncate(false);
        #[cfg(windows)] {use std::os::windows::fs::OpenOptionsExt;options.share_mode(0);}
        let file=options.open(path).map_err(|_|"此工作区的正式程序或安装任务正在运行，请退出后重试。".to_string())?;
        Ok(Self{_file:file})
    }
}
pub fn ensure_idle(root:&Path)->Result<(),String>{
    #[cfg(windows)] unsafe {
        use windows_sys::Win32::{Foundation::{CloseHandle,INVALID_HANDLE_VALUE},System::Diagnostics::ToolHelp::{CreateToolhelp32Snapshot,Process32FirstW,Process32NextW,PROCESSENTRY32W,TH32CS_SNAPPROCESS}};
        let snapshot=CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS,0);
        if snapshot==INVALID_HANDLE_VALUE{return Err("无法检查目标进程占用。".into());}
        let mut row:PROCESSENTRY32W=std::mem::zeroed();row.dwSize=std::mem::size_of::<PROCESSENTRY32W>() as u32;
        let mut has=Process32FirstW(snapshot,&mut row);
        let expected=canonical_path(&root.join("app/VoiceTerminalDesktop.exe")).ok();
        let result:Result<(),String>=(||{while has!=0{
            if row.th32ProcessID!=std::process::id(){
                let name=String::from_utf16_lossy(&row.szExeFile).trim_end_matches('\0').to_owned();
                if name.eq_ignore_ascii_case("VoiceTerminalDesktop.exe"){
                    if let Some((_,path))=workspace::process_identity(row.th32ProcessID)?{
                        if expected.as_ref().is_some_and(|expected|path.to_string_lossy().eq_ignore_ascii_case(&expected.to_string_lossy())){
                            return Err("目标正式程序正在运行，请退出后重试。".into());
                        }
                    }
                }
            }
            has=Process32NextW(snapshot,&mut row);
        }Ok(())})();CloseHandle(snapshot);result?;
    }
    Ok(())
}
pub fn validate_target(root:&Path)->Result<(),String>{
    workspace::Locator::local(root).validate_target(root)?;
    if root.exists(){
        if root.join("portable.json").is_file(){
            let value=crate::resources::load(&safe_path(root,"portable.json")?)?;
            if value["schema"]!=2||value["app"]!="app"||value["data_dir"]!="data"{return Err("目标不是兼容的已有工作区。".into());}
            if let Some(task)=workspace::pending_task(root)?{if crate::resources::load(&task)?["helper_pid"].as_u64()!=Some(std::process::id() as u64){return Err("存在未完成工作区事务，请从原正式入口恢复后重试。".into());}}
            for relative in ["app","data","models","runtimes","tmp","portable.json"]{safe_path(root,relative)?;}
        }else if fs::read_dir(root).map_err(|e|e.to_string())?.next().is_some(){return Err("请选择空文件夹或已有工作区，不能向无关非空目录展开。".into());}
    }Ok(())
}
fn cancelled(cancel:&AtomicBool)->Result<(),String>{if cancel.load(Ordering::Acquire){Err("本体展开已取消，可重新尝试。".into())}else{Ok(())}}

/// package_version（读取统一程序包版本）：用于绑定一次性更新配置与内嵌程序包。
pub fn package_version(payload:&[u8])->Result<String,String>{
    let mut archive=zip::ZipArchive::new(Cursor::new(payload)).map_err(|error|error.to_string())?;
    let mut file=archive.by_name("program-package.json").map_err(|_|"正式程序包缺少版本记录。")?;
    let mut bytes=Vec::new();file.read_to_end(&mut bytes).map_err(|error|error.to_string())?;
    let record:Value=serde_json::from_slice(&bytes).map_err(|error|error.to_string())?;
    if record["schema"]!=1||record["role"]!="product"{return Err("正式程序包记录无效。".into());}
    let version=record["version"].as_str().ok_or("正式程序包缺少产品版本。")?.to_owned();
    semver::Version::parse(&version).map_err(|_|"正式程序包版本无效。")?;Ok(version)
}

/// package_build（内嵌构建身份）：完整性维护必须绑定同版本同构建。
pub fn package_build(payload:&[u8])->Result<String,String>{
    let mut archive=zip::ZipArchive::new(Cursor::new(payload)).map_err(|e|e.to_string())?;
    let mut bytes=Vec::new();archive.by_name("program-package.json").map_err(|e|e.to_string())?.read_to_end(&mut bytes).map_err(|e|e.to_string())?;
    let record:Value=serde_json::from_slice(&bytes).map_err(|e|e.to_string())?;
    record["build"].as_str().filter(|v|!v.is_empty()).map(str::to_owned).ok_or("内嵌程序包缺少构建编号。".into())
}
/// verify_current_package（按内嵌可信清单验证本体）：正常本体无需重复解包和替换。
pub fn verify_current_package(payload:&[u8],root:&Path,cancel:&AtomicBool,progress:&mut Progress)->Result<(),String>{
    let mut archive=zip::ZipArchive::new(Cursor::new(payload)).map_err(|e|e.to_string())?;
    let mut bytes=Vec::new();archive.by_name("app/release.json").map_err(|e|e.to_string())?.read_to_end(&mut bytes).map_err(|e|e.to_string())?;
    let expected:Value=serde_json::from_slice(&bytes).map_err(|e|e.to_string())?;
    bytes.clear();archive.by_name("program-package.json").map_err(|e|e.to_string())?.read_to_end(&mut bytes).map_err(|e|e.to_string())?;
    let record:Value=serde_json::from_slice(&bytes).map_err(|e|e.to_string())?;
    if record["schema"]!=1||record["role"]!="product"||record["build"]!=expected["build"]||record["version"]!=expected["version"]{return Err("内嵌程序包身份无效。".into());}
    verify_package_maps(&record,&expected)?;
    let actual=crate::resources::load(&safe_path(root,"app/release.json")?)?;
    let immutable=|mut value:Value|{if let Some(map)=value.as_object_mut(){map.remove("components");map.remove("deferred_migrations");if map.get_mut("fields").and_then(Value::as_object_mut).is_some_and(|fields|{fields.remove("deferred_migrations");fields.is_empty()}){map.remove("fields");}}value};
    if immutable(actual)!=immutable(expected.clone()){return Err("现用本体清单与维护程序不一致。".into());}
    workspace::verify_records_with_progress(&root.join("app"),expected["files"].as_array().ok_or("程序文件清单缺失")?,true,cancel,progress)?;
    workspace::verify_fixed_files_with_progress(root,expected["root_files"].as_array().ok_or("程序入口清单缺失")?,cancel,progress)
}

pub fn verify_installed(root:&Path)->Result<(),String>{
    workspace::verify_tree(&root.join("app"),"release.json")?;
    let release=crate::resources::load(&root.join("app/release.json"))?;
    for item in release["root_files"].as_array().ok_or("正式本体缺少入口清单")?{
        let relative=item["path"].as_str().ok_or("入口路径无效")?;
        if !ITEMS.contains(&relative){return Err("入口清单越出固定本体范围。".into());}
        let path=safe_path(root,relative)?;
        if fs::metadata(&path).map_err(|e|e.to_string())?.len()!=item["size"].as_u64().ok_or("入口大小无效")?||file_hash(&path)?!=item["sha256"].as_str().ok_or("入口摘要无效")?{return Err(format!("正式入口校验失败：{relative}"));}
    }Ok(())
}

fn verify_package(stage:&Path,cancel:&AtomicBool,progress:&mut Progress)->Result<(),String>{
    let record=crate::resources::load(&stage.join("program-package.json"))?;
    if record["schema"]!=1||record["role"]!="product"{return Err("正式程序包格式无效。".into());}
    let rows=record["files"].as_array().ok_or("正式程序包缺少文件清单")?;
    let mut expected=HashSet::new();
    for row in rows{
        cancelled(cancel)?;
        let relative=row["path"].as_str().ok_or("程序包路径无效")?;
        if !ITEMS.contains(&relative)&&!relative.starts_with("app/"){return Err("程序包包含固定本体之外的内容。".into());}
        if !expected.insert(relative.to_lowercase()){return Err("程序包清单有重复路径。".into());}
    }
    workspace::verify_fixed_files_with_progress(stage,rows,cancel,&mut|_,done,total|progress("正在校验程序本体",done,total))?;
    fn collect(root:&Path,path:&Path,out:&mut HashSet<String>)->Result<(),String>{
        for entry in fs::read_dir(path).map_err(|e|e.to_string())?{let path=entry.map_err(|e|e.to_string())?.path();let relative=path.strip_prefix(root).unwrap().to_string_lossy().replace('\\',"/");safe_path(root,&relative)?;if path.is_dir(){collect(root,&path,out)?;}else if relative!="program-package.json"{out.insert(relative.to_lowercase());}}
        Ok(())
    }
    let mut actual=HashSet::new();collect(stage,stage,&mut actual)?;
    if actual!=expected{return Err("程序包实际文件与清单不一致。".into());}
    for entry in ITEMS{if !stage.join(entry).exists(){return Err(format!("正式程序包缺少 {entry}（固定本体文件）。"));}}
    let release=crate::resources::load(&stage.join("app/release.json"))?;
    if release["build"]!=record["build"]{return Err("正式程序包的构建编号不一致。".into());}
    verify_package_maps(&record,&release)
}
fn verify_package_maps(record:&Value,release:&Value)->Result<(),String>{
    fn mapped(rows:&[Value])->Result<std::collections::BTreeMap<String,(u64,String)>,String>{
        let mut map=std::collections::BTreeMap::new();
        for row in rows{
            let path=row["path"].as_str().ok_or("清单路径无效")?;workspace::relative_path(path)?;
            let size=row["size"].as_u64().ok_or("清单大小无效")?;
            let hash=row["sha256"].as_str().filter(|hash|hash.len()==64&&hash.bytes().all(|byte|byte.is_ascii_hexdigit())).ok_or("清单摘要无效")?;
            if map.insert(path.to_lowercase(),(size,hash.to_lowercase())).is_some(){return Err("清单存在重复或大小写冲突路径。".into());}
        }Ok(map)
    }
    let package=mapped(record["files"].as_array().ok_or("程序包文件清单缺失")?)?;
    let app=mapped(release["files"].as_array().ok_or("本体文件清单缺失")?)?;
    let roots=mapped(release["root_files"].as_array().ok_or("本体入口清单缺失")?)?;
    let actual_app=package.iter().filter_map(|(path,value)|path.strip_prefix("app/").filter(|path|*path!="release.json").map(|path|(path.to_owned(),value.clone()))).collect::<std::collections::BTreeMap<_,_>>();
    let actual_roots=package.iter().filter(|(path,_)|!path.starts_with("app/")).map(|(path,value)|(path.clone(),value.clone())).collect::<std::collections::BTreeMap<_,_>>();
    let required_roots=ITEMS.iter().filter(|path|**path!="app").map(|path|path.to_lowercase()).collect::<std::collections::BTreeSet<_>>();
    if app!=actual_app||roots!=actual_roots||roots.keys().cloned().collect::<std::collections::BTreeSet<_>>()!=required_roots{return Err("程序包与本体发布清单文件集合或摘要不一致。".into());}Ok(())
}

pub(crate) fn prepare_speaker_migration(root:&Path,stage:&Path,release:&mut Value,progress:&mut Progress)->Result<Vec<Value>,String>{
    let migration=&release["speaker_migration"];
    if migration.is_null(){return Ok(vec![]);}
    if migration["revision"]!=1||migration["from_model"]!="wespeaker-cnceleb-resnet34"||migration["to_model"]!="campplus-zh-cn-common"||migration["delete_legacy_profiles"]!=true{
        return Err("声纹升级规则不受支持，原组件与档案保持不变。".into());
    }
    let catalog=crate::resources::load(&stage.join("app/config/resources/catalog.json"))?;
    let spec=catalog["resources"].as_array().and_then(|rows|rows.iter().find(|row|row["resource_id"]=="campplus-zh-cn-common")).ok_or("升级包缺少CAM++资源描述")?;
    if spec["version"]!="v1.0.0"||spec["destination"]!="speaker/campplus-zh-cn-common"{return Err("CAM++升级目标不匹配。".into());}
    let legacy_registry=safe_path(root,"data/speaker/registry.json")?;
    let legacy_flat=legacy_registry.is_file()&&crate::resources::load(&legacy_registry).is_ok_and(|value|value["model_id"]=="wespeaker-cnceleb-resnet34");
    let retired=safe_path(root,LEGACY_MODEL)?.exists()||safe_path(root,RETIRED_PROFILE_PATHS[0])?.exists()||legacy_flat;
    if !retired&&crate::resources::model_receipt_current(&safe_path(root,CURRENT_MODEL)?,spec){
        progress("声纹迁移已完成，复用当前组件及模型清单",1,1);
        return Ok(vec![]);
    }
    let candidate=safe_path(root,&format!("tmp/publish/new/{CURRENT_MODEL}"))?;
    fs::create_dir_all(&candidate).map_err(|e|e.to_string())?;
    let mut receipt_files=vec![];
    for row in spec["files"].as_array().ok_or("CAM++升级文件清单无效")?{
        if row["optional"]==true{continue;}
        let name=row["path"].as_str().ok_or("升级权重名称缺失")?;
        let source=safe_path(stage,&format!("app/resources/model-seeds/campplus-zh-cn-common/{name}"))?;
        let destination=safe_path(&candidate,name)?;
        let size=row["size_bytes"].as_u64().ok_or("升级权重大小缺失")?;
        let sha=row["sha256"].as_str().ok_or("升级权重摘要缺失")?;
        if fs::metadata(&source).map_err(|e|e.to_string())?.len()!=size||file_hash(&source)?!=sha{return Err("CAM++升级种子权重校验失败，旧组件保持不变。".into());}
        fs::create_dir_all(destination.parent().unwrap()).map_err(|e|e.to_string())?;
        fs::copy(&source,&destination).map_err(|e|e.to_string())?;
        if file_hash(&destination)?!=sha{return Err("CAM++候选权重读回校验失败。".into());}
        receipt_files.push(json!({"path":name,"size":size,"sha256":sha,"optional":false}));
    }
    if receipt_files.is_empty(){return Err("CAM++升级种子没有有效权重。".into());}
    crate::resources::write_model_receipt(&candidate,spec,receipt_files)?;
    let mut rows=vec![json!({"path":CURRENT_MODEL,"original":safe_path(root,CURRENT_MODEL)?.exists()})];
    let old=safe_path(root,LEGACY_MODEL)?;
    if old.exists(){rows.push(json!({"path":LEGACY_MODEL,"original":true,"delete_only":true}));}
    for relative in RETIRED_PROFILE_PATHS{
        let path=safe_path(root,relative)?;
        if path.exists()&&(*relative==RETIRED_PROFILE_PATHS[0]||legacy_flat){rows.push(json!({"path":relative,"original":true,"delete_only":true}));}
    }
    // models（模型目录）的总清单和本体组件登记一并换入，不能留下旧模型摘要记录。
    let old_manifest=crate::resources::load(&root.join("models/manifest.json")).unwrap_or(Value::Null);
    let known=old_manifest["files"].as_array().cloned().unwrap_or_default();
    // 保留模型文件先按内容重算；旧总清单只是上次签发的快照，组件的安装收据才是权威记录。
    // 变化必须能被所属组件的收据确认，才按实际内容重新登记；否则保留原模型及声纹，
    // 延后整个声纹迁移，但不阻止本体更新，让新版组件管理页仍可修复旧组件。
    let mut retained:Vec<(String,u64,String)>=vec![];
    if root.join("models").is_dir(){for path in crate::resources::walk(&root.join("models"))?{
        let relative=path.strip_prefix(root).unwrap().to_string_lossy().replace('\\',"/");
        if relative=="models/manifest.json"||relative.starts_with(&format!("{LEGACY_MODEL}/"))||relative.starts_with(&format!("{CURRENT_MODEL}/")){continue;}
        let name=relative.strip_prefix("models/").unwrap().to_owned();
        let size=path.metadata().map_err(|e|e.to_string())?.len();
        let sha=file_hash(&path)?;
        retained.push((name,size,sha));
    }}
    let actual:std::collections::HashMap<&str,(u64,&str)>=retained.iter().map(|(name,size,sha)|(name.as_str(),(*size,sha.as_str()))).collect();
    let mut confirmed=HashSet::new();
    for (name,size,sha) in &retained{
        let changed=known.iter().any(|row|row["path"]==name.as_str()&&(row["size"].as_u64()!=Some(*size)||row["sha256"].as_str()!=Some(sha.as_str())));
        if !changed{continue;}
        let validation=(||{
            let component=retained_component(root,name).ok_or_else(||format!("保留模型文件校验失败，旧声纹与档案保持不变：{name}"))?;
            if confirmed.insert(component.clone()){
                verify_component_receipt(root,&component,&actual)?;
                progress(&format!("保留模型文件按安装收据重新核验：{component}"),1,1);
            }
            Ok::<(),String>(())
        })();
        if let Err(reason)=validation{
            if !release["fields"].is_object(){release["fields"]=json!({});}
            release["fields"]["deferred_migrations"]=json!("旧组件需修复，暂缓迁移但允许本体升级的记录");
            release["deferred_migrations"]=json!({"speaker":{"reason":reason,"action":"在新版组件管理中修复模型后重新运行安装器完成声纹迁移"},
                "fields":{"speaker":"暂缓的声纹迁移","reason":"旧模型无法确认完整性的原因","action":"修复和继续迁移的方法"}});
            progress("旧模型需要修复：保留模型与声纹资料，先更新程序本体",1,1);
            return Ok(vec![]);
        }
    }
    let mut files=retained.into_iter().map(|(name,size,sha)|json!({"path":name,"size":size,"sha256":sha})).collect::<Vec<_>>();
    for path in crate::resources::walk(&candidate)?{files.push(json!({"path":format!("speaker/campplus-zh-cn-common/{}",path.strip_prefix(&candidate).unwrap().to_string_lossy().replace('\\',"/")),"size":path.metadata().map_err(|e|e.to_string())?.len(),"sha256":file_hash(&path)?}));}
    files.sort_by(|a,b|a["path"].as_str().cmp(&b["path"].as_str()));
    let build=format!("models-{:x}",Sha256::digest(serde_json::to_vec(&files).map_err(|e|e.to_string())?));
    let manifest=safe_path(root,"tmp/publish/new/models/manifest.json")?;
    atomic_json(&manifest,&json!({"schema":1,"kind":"models","build":build,"files":files,
        "fields":{"schema":"模型清单版本","kind":"模型资源类别","build":"模型集合摘要编号","files":"安装文件记录","path":"相对路径","size":"字节数","sha256":"文件摘要"}}))?;
    if !release["components"].is_array(){release["components"]=json!([]);}
    let components=release["components"].as_array_mut().unwrap();components.retain(|row|row["kind"]!="models");
    components.push(json!({"kind":"models","path":"models","build":build,"manifest_sha256":file_hash(&manifest)?,"optional":true}));
    rows.push(json!({"path":"models/manifest.json","original":root.join("models/manifest.json").exists()}));
    progress("CAM++候选权重已校验，准备卸载旧声纹组件及档案",1,1);
    Ok(rows)
}
// retained_component（保留文件的所属组件）：从最深的目录向上找到带安装收据的组件目录。
fn retained_component(root:&Path,name:&str)->Option<String>{
    let parts:Vec<&str>=name.split('/').collect();
    for index in (1..parts.len()).rev(){
        let directory=parts[..index].join("/");
        if root.join("models").join(directory.split('/').collect::<std::path::PathBuf>()).join("resource-install.json").is_file(){return Some(directory);}
    }
    None
}
// verify_component_receipt（按收据核验组件）：收据格式、内容摘要有效，且登记文件与磁盘内容一致。
// 只有这样的组件才接受总清单之外的摘要变化；旧总清单摘要失效属于可修复的登记差异，不是文件损坏。
fn verify_component_receipt(root:&Path,component:&str,actual:&std::collections::HashMap<&str,(u64,&str)>)->Result<(),String>{
    let folder=root.join("models").join(component.split('/').collect::<std::path::PathBuf>());
    let (_,_,records)=crate::resources::read_model_receipt(&folder).map_err(|error|format!("保留模型文件校验失败，旧声纹与档案保持不变：{component}（{error}）"))?;
    for row in records{
        let relative=row["path"].as_str().ok_or("安装收据记录缺少路径")?;
        let full=format!("{component}/{relative}");
        match actual.get(full.as_str()){
            None if row["optional"]==true=>continue,
            None=>return Err(format!("保留模型文件校验失败，旧声纹与档案保持不变：{full}（安装收据登记的文件缺失）")),
            Some((size,sha))=>if *size!=row["size"].as_u64().unwrap_or(u64::MAX)||*sha!=row["sha256"].as_str().unwrap_or(""){
                return Err(format!("保留模型文件校验失败，旧声纹与档案保持不变：{full}（与安装收据不符，需要重装该组件）"));
            },
        }
    }
    Ok(())
}

fn activate(root:&Path,stage:&Path,cancel:&AtomicBool,progress:&mut Progress)->Result<(),String>{
    // components（组件登记）属于工作区状态；固定本体仍来自同一程序包。
    let incoming=stage.join("app/release.json");let mut release=crate::resources::load(&incoming)?;
    if let Ok(old)=crate::resources::load(&root.join("app/release.json")){
        if let Some(rows)=old["components"].as_array(){release["components"]=json!(rows.iter().filter(|row|matches!((row["kind"].as_str(),row["path"].as_str()),(Some("python"),Some("runtimes/python"))|(Some("models"),Some("models"))|(Some("dsh"),Some("runtimes/dsh"))|(Some("webview2"),Some("runtimes/webview2")))).collect::<Vec<_>>());atomic_json(&incoming,&release)?;}
    }
    let mut migration_rows=crate::compatibility::prepare_upgrade(root,stage,&mut release,cancel,progress)?;
    let mut seen=HashSet::new();migration_rows.retain(|row|seen.insert(row["path"].as_str().unwrap_or("").to_lowercase()));
    cancelled(cancel)?;
    atomic_json(&incoming,&release)?;
    let journal=root.join("tmp/update-transaction.json");
    let mut rows=ITEMS.iter().map(|name|json!({"path":name,"original":root.join(name).exists()})).collect::<Vec<_>>();
    rows.extend(migration_rows);
    let mut record=json!({"schema":1,"state":"prepared","items":rows,"fields":{"schema":"本体替换事务格式","state":"准备或提交阶段","items":"固定受管文件","path":"相对路径","original":"是否存在原文件"}});
    atomic_json(&journal,&record)?;
    progress("启用程序本体",1,1); // 从这里开始禁止取消；失败按日志恢复原本体。
    let result:Result<(),String>=(||{
        for row in record["items"].as_array().unwrap(){
            let name=row["path"].as_str().unwrap();
            let target=safe_path(root,name)?;let old=safe_path(root,&format!("tmp/publish/old/{name}"))?;
            fs::create_dir_all(old.parent().unwrap()).map_err(|e|e.to_string())?;
            if target.exists(){fs::rename(&target,&old).map_err(|e|format!("目标文件被占用，无法启用：{e}"))?;}
            if row["delete_only"]!=true{
                // Fresh workspaces have `models/` but not the nested `models/speaker/` directory.
                // Create every target parent before moving a seeded model into the live workspace.
                fs::create_dir_all(target.parent().unwrap()).map_err(|e|e.to_string())?;
                fs::rename(stage.join(name),target).map_err(|e|e.to_string())?;
            }
        }
        workspace::normalize_layout(root)?;
        record["state"]=json!("committed");atomic_json(&journal,&record)?;Ok(())
    })();
    match result{Ok(())=>workspace::recover_install(root),Err(error)=>{workspace::recover_install(root).map_err(|recovery|format!("{error}；恢复原本体失败：{recovery}"))?;Err(error)}}
}
pub fn install(payload:&[u8],root:&Path,cancel:Arc<AtomicBool>,progress:&mut Progress)->Result<(),String>{
    validate_target(root)?;fs::create_dir_all(root).map_err(|e|e.to_string())?;
    let root=canonical_path(root)?;ensure_idle(&root)?;
    // 空目录已在上方验证；初始化与兼容转换也必须在同一互斥句柄内完成。
    let _guard=WorkspaceGuard::acquire(&root)?;
    if !root.join("portable.json").exists(){workspace::initialize_guarded(&root,&_guard)?;}
    workspace::recover_install(&root)?;workspace::recover_base(&root)?;
    workspace::normalize_layout(&root)?;
    let mut archive=zip::ZipArchive::new(Cursor::new(payload)).map_err(|e|e.to_string())?;
    let total=(0..archive.len()).try_fold(0u64,|sum,i|sum.checked_add(archive.by_index(i).map_err(|e|e.to_string())?.size()).ok_or_else(||"程序包大小溢出".to_string()))?;
    if workspace::available_space(&root)?<total.saturating_mul(2).saturating_add(16*1024*1024){return Err("工作区空间不足，无法暂存及替换本体。".into());}
    let stage=safe_path(&root,"tmp/publish/new")?;remove_owned(&stage,&root)?;fs::create_dir_all(&stage).map_err(|e|e.to_string())?;
    let result=(||{
        let mut seen=HashSet::new();let mut done=0;let mut buffer=[0;65536];
        for index in 0..archive.len(){
            cancelled(&cancel)?;let mut entry=archive.by_index(index).map_err(|e|e.to_string())?;let relative=entry.name().trim_end_matches('/').to_owned();
            if !seen.insert(relative.to_lowercase())||entry.unix_mode().is_some_and(|mode|mode&0o170000==0o120000){return Err("程序包含有重复路径或链接。".into());}
            if !ITEMS.contains(&relative.as_str())&&!relative.starts_with("app/")&&relative!="program-package.json"{return Err("程序包含有用户数据或可选组件。".into());}
            let destination=safe_path(&stage,&relative)?;
            if entry.is_dir(){fs::create_dir_all(destination).map_err(|e|e.to_string())?;continue;}
            fs::create_dir_all(destination.parent().unwrap()).map_err(|e|e.to_string())?;
            let mut outgoing=fs::OpenOptions::new().write(true).create_new(true).open(destination).map_err(|e|e.to_string())?;
            loop{cancelled(&cancel)?;let count=entry.read(&mut buffer).map_err(|e|e.to_string())?;if count==0{break;}outgoing.write_all(&buffer[..count]).map_err(|e|e.to_string())?;done+=count as u64;progress("正在展开程序本体",done,total);}
            outgoing.sync_all().map_err(|e|e.to_string())?;
        }
        verify_package(&stage,&cancel,progress)?;cancelled(&cancel)?;activate(&root,&stage,&cancel,progress)?;
        let release=crate::resources::load(&safe_path(&root,"app/release.json")?)?;
        atomic_json(&safe_path(&root,"data/state/product-identity.json")?,&json!({"schema":1,"version":release["version"],"build":release["build"],"fields":{"schema":"已提交本体身份格式","version":"本体版本","build":"本体构建编号"}}))
    })();
    if !root.join("tmp/update-transaction.json").exists(){let _=remove_owned(&root.join("tmp/publish"),&root);}result
}
pub fn copy_program(source:&Path,target:&Path,cancel:Arc<AtomicBool>,progress:&mut Progress)->Result<(),String>{
    // 空工作区从已经发布的本体初始化，仅复制清单内文件；不读取业务源码或可选组件。
    workspace::verify_tree(&source.join("app"),"release.json")?;
    let release=crate::resources::load(&source.join("app/release.json"))?;
    let mut bytes=Cursor::new(Vec::new());
    {let mut archive=zip::ZipWriter::new(&mut bytes);let options=zip::write::SimpleFileOptions::default().compression_method(zip::CompressionMethod::Stored);
        let mut rows=Vec::new();
        for row in release["files"].as_array().ok_or("本体清单无效")?{let relative=format!("app/{}",row["path"].as_str().ok_or("本体路径无效")?);cancelled(&cancel)?;archive.start_file(&relative,options).map_err(|e|e.to_string())?;let data=fs::read(safe_path(source,&relative)?).map_err(|e|e.to_string())?;archive.write_all(&data).map_err(|e|e.to_string())?;rows.push(json!({"path":relative,"size":data.len(),"sha256":format!("{:x}",Sha256::digest(&data))}));}
        let mut clean=release.clone();clean["components"]=json!([]);let data=serde_json::to_vec_pretty(&clean).map_err(|e|e.to_string())?;archive.start_file("app/release.json",options).map_err(|e|e.to_string())?;archive.write_all(&data).map_err(|e|e.to_string())?;rows.push(json!({"path":"app/release.json","size":data.len(),"sha256":format!("{:x}",Sha256::digest(&data))}));
        for name in ITEMS.iter().filter(|name|**name!="app"){let path=safe_path(source,name)?;let expected=release["root_files"].as_array().and_then(|rows|rows.iter().find(|row|row["path"]==*name)).ok_or("正式入口缺少固定文件登记")?;if file_hash(&path)?!=expected["sha256"].as_str().ok_or("入口摘要无效")?{return Err("当前正式入口校验失败，不能初始化其他工作区。".into());}let data=fs::read(path).map_err(|e|e.to_string())?;archive.start_file(*name,options).map_err(|e|e.to_string())?;archive.write_all(&data).map_err(|e|e.to_string())?;rows.push(json!({"path":name,"size":data.len(),"sha256":format!("{:x}",Sha256::digest(&data))}));}
        archive.start_file("program-package.json",options).map_err(|e|e.to_string())?;archive.write_all(&serde_json::to_vec(&json!({"schema":1,"role":"product","build":release["build"],"files":rows})).unwrap()).map_err(|e|e.to_string())?;archive.finish().map_err(|e|e.to_string())?;
    }install(bytes.get_ref(),target,cancel.clone(),progress)?;
    crate::compatibility::enforce_upgrade(target,cancel,progress)
}

// acknowledge（首次受控窗口确认）：校验一次性凭据，仅写入工作区暂存区。
pub fn acknowledge(root:&Path,args:&[String])->Result<(),String>{
    if let Some(index)=args.iter().position(|arg|arg=="--startup-handoff"){
        let token=args.get(index+1).ok_or("启动交接缺少凭据")?;
        if token.is_empty()||!token.bytes().all(|b|b.is_ascii_hexdigit()||b==b'-'){return Err("启动交接凭据无效。".into());}
        let path=safe_path(root,&format!("tmp/handoff-{token}.json"))?;
        let request=crate::resources::load(&path)?;
        if request["token"]!=*token||request["state"]!="waiting"{return Err("启动交接已过期。".into());}
        let pid=std::process::id();let (created,executable)=workspace::process_identity(pid)?.ok_or("桌面进程身份不可用")?;
        let record=json!({"token":token,"state":"window-ready","pid":pid,"create_time":created,"executable":executable,"fields":{"token":"一次性启动凭据","state":"首个受控窗口已显示","pid":"正式桌面进程编号","create_time":"进程创建时间","executable":"正式宿主位置"}});
        write_acknowledgement(&path,&record)?;
    }Ok(())
}
// write_acknowledgement（交接确认写入）：轮询读取或安全扫描可能短暂阻止 Windows 原子替换。
fn write_acknowledgement(path:&Path,record:&Value)->Result<(),String>{
    let deadline=Instant::now()+Duration::from_secs(3);
    loop {
        match atomic_json(path,record){
            Ok(())=>return Ok(()),
            Err(error)=>{
                if Instant::now()>=deadline{
                    let _=fs::remove_file(path.with_extension(format!("{}.tmp",std::process::id())));
                    if let Some(root)=path.parent().and_then(Path::parent){
                        let logs=root.join("cache/logs");let _=fs::create_dir_all(&logs);
                        let _=fs::write(logs.join("startup-handoff-error.json"),serde_json::to_vec_pretty(&json!({"error":error,"pid":std::process::id(),"fields":{"error":"交接确认写入失败原因","pid":"正式桌面进程编号"}})).unwrap_or_default());
                    }
                    return Err(format!("正式窗口已打开，但交接确认写入失败：{error}"));
                }
                std::thread::sleep(Duration::from_millis(25));
            }
        }
    }
}
// read_acknowledgement（交接确认读取）：允许写入方原子替换，避免轮询占用确认文件。
fn read_acknowledgement(path:&Path)->Result<Value,String>{
    let mut options=fs::OpenOptions::new();options.read(true);
    #[cfg(windows)] {use std::os::windows::fs::OpenOptionsExt;options.share_mode(1|2|4);}
    let mut file=options.open(path).map_err(|e|e.to_string())?;let mut bytes=Vec::new();
    file.read_to_end(&mut bytes).map_err(|e|e.to_string())?;
    serde_json::from_slice(&bytes).map_err(|e|e.to_string())
}
pub fn launch(root:&Path)->Result<u32,String>{
    let expected=canonical_path(&root.join("app/VoiceTerminalDesktop.exe"))?;
    let token=workspace::new_id();let path=safe_path(root,&format!("tmp/handoff-{token}.json"))?;
    atomic_json(&path,&json!({"token":token,"state":"waiting","fields":{"token":"一次性启动凭据","state":"等待受控窗口显示"}}))?;
    let mut command=Command::new(root.join("VoiceTerminal.exe"));command.args(["--startup-handoff",&token]).current_dir(root);
    command.stdin(std::process::Stdio::null()).stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null());
    #[cfg(windows)] {use std::os::windows::process::CommandExt;command.creation_flags(0x08000000);}
    command.spawn().map_err(|e|format!("正式程序启动失败：{e}"))?;
    let deadline=Instant::now()+Duration::from_secs(45);
    while Instant::now()<deadline{
        if let Ok(record)=read_acknowledgement(&path){if record["token"]==token&&record["state"]=="window-ready"{
            let pid=record["pid"].as_u64().ok_or("交接进程编号无效")? as u32;
            if let Some((created,exe))=workspace::process_identity(pid)?{if record["create_time"].as_u64()==Some(created)&&exe.to_string_lossy().eq_ignore_ascii_case(&expected.to_string_lossy()){let _=fs::remove_file(path);return Ok(pid);}}
        }}std::thread::sleep(Duration::from_millis(100));
    }
    let _=fs::remove_file(path);Err("正式程序未确认窗口交接。已安装本体保留，请退出目标程序后重试或双击工作区正式入口。".into())
}

#[cfg(test)] mod tests{
    use super::*;
    #[test]
    fn repair_checks_all_immutable_metadata_and_allows_only_workspace_bindings(){
        let root=fixture();let payload=package("repair-build",false);run(&payload,&root).unwrap();
        let path=root.join("app/release.json");let mut release=crate::resources::load(&path).unwrap();
        release["components"]=json!([{"kind":"models","path":"models","build":"workspace"}]);atomic_json(&path,&release).unwrap();
        verify_current_package(&payload,&root,&AtomicBool::new(false),&mut|_,_,_|{}).unwrap();
        assert!(verify_current_package(&package("repair-build",true),&root,&AtomicBool::new(false),&mut|_,_,_|{}).is_err());
        release["required_components"]=json!(["unexpected"]);atomic_json(&path,&release).unwrap();
        assert!(verify_current_package(&payload,&root,&AtomicBool::new(false),&mut|_,_,_|{}).is_err());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn duplicated_inner_files_cannot_hide_an_unlisted_package_file(){
        let hash="a".repeat(64);let row=|path:&str|json!({"path":path,"size":1,"sha256":hash});
        let roots=vec![row("VoiceTerminal.exe"),row("Verify-Package.ps1"),row("使用与环境说明.md")];
        let mut files=vec![row("app/a"),row("app/b"),row("app/release.json")];files.extend(roots.clone());
        let record=json!({"files":files});let mut release=json!({"files":[row("a"),row("b")],"root_files":roots});
        verify_package_maps(&record,&release).unwrap();
        release["files"]=json!([row("a"),row("a")]);assert!(verify_package_maps(&record,&release).is_err());
        release["files"]=json!([row("a"),row("b")]);release["root_files"].as_array_mut().unwrap().pop();assert!(verify_package_maps(&record,&release).is_err());
    }
    #[cfg(windows)]
    #[test]
    #[ignore="实际正式程序交接验证；VOICE_HANDOFF_TEST_ROOT（本轮验证工作区）须指向已安装且未运行的工作区"]
    fn installed_product_handoff(){
        let root=PathBuf::from(std::env::var_os("VOICE_HANDOFF_TEST_ROOT").expect("须设置本轮验证工作区"));
        for _ in 0..3{
            let pid=launch(&root).expect("正式程序须确认窗口交接");
            assert!(workspace::process_identity(pid).unwrap().is_some());
            Command::new(root.join("VoiceTerminal.exe")).arg("--exit").status().unwrap();
            let deadline=Instant::now()+Duration::from_secs(30);
            while workspace::process_identity(pid).unwrap().is_some(){
                assert!(Instant::now()<deadline,"本轮验证正式程序须正常退出");
                std::thread::sleep(Duration::from_millis(100));
            }
        }
    }
    #[cfg(windows)]
    #[test]
    fn acknowledgement_retries_while_reader_blocks_replacement(){
        use std::os::windows::fs::OpenOptionsExt;
        let root=fixture();let path=root.join("handoff-test.json");
        atomic_json(&path,&json!({"state":"waiting"})).unwrap();
        // share_mode（文件共享模式）仅允许读取，模拟轮询持有的禁止删除句柄。
        let reader=fs::OpenOptions::new().read(true).share_mode(1).open(&path).unwrap();
        let target=path.clone();
        let writer=std::thread::spawn(move||write_acknowledgement(&target,&json!({"state":"window-ready"})));
        std::thread::sleep(Duration::from_millis(150));
        assert_eq!(crate::resources::load(&path).unwrap()["state"],"waiting");
        assert!(!writer.is_finished());drop(reader);
        writer.join().unwrap().unwrap();
        assert_eq!(crate::resources::load(&path).unwrap()["state"],"window-ready");
        assert!(!path.with_extension(format!("{}.tmp",std::process::id())).exists());
        fs::remove_dir_all(root).unwrap();
    }
    use std::path::PathBuf;
    fn fixture()->PathBuf{let root=std::env::temp_dir().join(format!("voice-install-中文 空格-{}",workspace::new_id()));fs::create_dir_all(&root).unwrap();root}
    fn package(build:&str,corrupt:bool)->Vec<u8>{
        package_options(build,corrupt,None)
    }
    fn package_options(build:&str,corrupt:bool,speaker:Option<bool>)->Vec<u8>{
        let mut payload=Cursor::new(Vec::new());
        let app_bytes=build.as_bytes();let hash=format!("{:x}",Sha256::digest(app_bytes));
        let mut app=json!({"schema":1,"kind":"app","version":env!("CARGO_PKG_VERSION"),"build":build,"files":[{"path":"business.py","size":app_bytes.len(),"sha256":hash}],"components":[],"required_components":[],"optional_components":["python","models","dsh","webview2"]});
        let mut entries=vec![("app/business.py",app_bytes.to_vec())];
        if let Some(invalid)=speaker{
            let mut weight=b"native-model-fixture".to_vec();let expected=format!("{:x}",Sha256::digest(&weight));if invalid{weight[0]^=1;}
            app["speaker_migration"]=json!({"revision":1,"from_model":"wespeaker-cnceleb-resnet34","to_model":"campplus-zh-cn-common","delete_legacy_profiles":true});
            let catalog=json!({"resources":[{"resource_id":"campplus-zh-cn-common","version":"v1.0.0","destination":"speaker/campplus-zh-cn-common","files":[{"path":"campplus_cn_common.bin","size_bytes":weight.len(),"sha256":expected}]}]});
            entries.push(("app/config/resources/catalog.json",serde_json::to_vec(&catalog).unwrap()));
            entries.push(("app/resources/model-seeds/campplus-zh-cn-common/campplus_cn_common.bin",weight));
            app["files"]=json!(entries.iter().map(|(name,data)|json!({"path":name.strip_prefix("app/").unwrap(),"size":data.len(),"sha256":format!("{:x}",Sha256::digest(data))})).collect::<Vec<_>>());
        }
        let policy=include_bytes!("../../../../config/compatibility.json").to_vec();
        app["compatibility_engine"]=json!({"schema":1,"rules_file":"config/compatibility.json"});
        app["files"].as_array_mut().unwrap().push(json!({"path":"config/compatibility.json","size":policy.len(),"sha256":format!("{:x}",Sha256::digest(&policy))}));
        entries.push(("app/config/compatibility.json",policy));
        app["root_files"]=json!([("VoiceTerminal.exe",b"formal-entry".as_slice()),("Verify-Package.ps1",b"verify".as_slice()),("使用与环境说明.md",b"instructions".as_slice())].iter().map(|(path,data)|json!({"path":path,"size":data.len(),"sha256":format!("{:x}",Sha256::digest(data))})).collect::<Vec<_>>());
        entries.extend([("app/release.json",serde_json::to_vec(&app).unwrap()),("VoiceTerminal.exe",b"formal-entry".to_vec()),("Verify-Package.ps1",b"verify".to_vec()),("使用与环境说明.md",b"instructions".to_vec())]);
        let rows=entries.iter().map(|(path,data)|json!({"path":path,"size":data.len(),"sha256":if corrupt&&*path=="app/business.py"{"0".repeat(64)}else{format!("{:x}",Sha256::digest(data))}})).collect::<Vec<_>>();
        {let mut zip=zip::ZipWriter::new(&mut payload);let options=zip::write::SimpleFileOptions::default();for (name,data) in entries{zip.start_file(name,options).unwrap();zip.write_all(&data).unwrap();}zip.start_file("program-package.json",options).unwrap();zip.write_all(&serde_json::to_vec(&json!({"schema":1,"role":"product","version":env!("CARGO_PKG_VERSION"),"build":build,"files":rows})).unwrap()).unwrap();zip.finish().unwrap();}payload.into_inner()
    }
    fn run(payload:&[u8],root:&Path)->Result<(),String>{install(payload,root,Arc::new(AtomicBool::new(false)),&mut|_,_,_|{})}
    fn package_with_tts(build:&str,speaker:bool,invalid_patch:bool)->Vec<u8>{
        // 微型原生组件覆盖真实安装事务，无网络、无需完整模型或显卡。
        let bytes=package_options(build,false,if speaker{Some(false)}else{None});
        let mut archive=zip::ZipArchive::new(Cursor::new(bytes)).unwrap();
        let mut entries=std::collections::BTreeMap::<String,Vec<u8>>::new();
        for index in 0..archive.len(){let mut entry=archive.by_index(index).unwrap();let mut bytes=vec![];entry.read_to_end(&mut bytes).unwrap();entries.insert(entry.name().into(),bytes);}
        let mut catalog=entries.get("app/config/resources/catalog.json").map(|bytes|serde_json::from_slice::<Value>(bytes).unwrap()).unwrap_or(json!({"resources":[]}));
        let mut patch=Cursor::new(vec![]);let mut files=vec![];
        {let mut zip=zip::ZipWriter::new(&mut patch);for (name,data) in [("cuda/audiocpp_server.exe",b"new-native".as_slice()),("model_specs/voxcpm2.json",b"{}".as_slice())]{
            zip.start_file(name,zip::write::SimpleFileOptions::default()).unwrap();zip.write_all(data).unwrap();
            files.push(json!({"path":name,"source_path":name,"archive_id":"patch","size_bytes":data.len(),"sha256":format!("{:x}",Sha256::digest(data))}));
        }zip.finish().unwrap();}
        files.push(json!({"path":"cuda/base.dll","archive_id":"binary","size_bytes":4,"sha256":format!("{:x}",Sha256::digest(b"base"))}));
        catalog["resources"].as_array_mut().unwrap().push(json!({"resource_id":"tts-audiocpp-runtime","destination":"audiocpp/runtime","version":"fixed",
            "files":files,"source":{"transfer":{"kind":"official_audiocpp_patch","patch":{"asset":"assets/audiocpp/voxcpm-patch.zip","size_bytes":patch.get_ref().len(),"sha256":if invalid_patch{"0".repeat(64)}else{format!("{:x}",Sha256::digest(patch.get_ref()))}}}}}));
        entries.insert("app/config/resources/catalog.json".into(),serde_json::to_vec(&catalog).unwrap());
        entries.insert("app/config/resources/assets/audiocpp/voxcpm-patch.zip".into(),patch.into_inner());
        let mut release:Value=serde_json::from_slice(&entries.remove("app/release.json").unwrap()).unwrap();
        entries.remove("program-package.json");
        release["root_files"]=json!(entries.iter().filter(|(name,_)|ITEMS.contains(&name.as_str())&&name.as_str()!="app").map(|(name,data)|json!({"path":name,"size":data.len(),"sha256":format!("{:x}",Sha256::digest(data))})).collect::<Vec<_>>());
        release["files"]=json!(entries.iter().filter_map(|(name,data)|name.strip_prefix("app/").map(|relative|json!({"path":relative,"size":data.len(),"sha256":format!("{:x}",Sha256::digest(data))}))).collect::<Vec<_>>());
        entries.insert("app/release.json".into(),serde_json::to_vec(&release).unwrap());
        let rows=entries.iter().map(|(name,data)|json!({"path":name,"size":data.len(),"sha256":format!("{:x}",Sha256::digest(data))})).collect::<Vec<_>>();
        entries.insert("program-package.json".into(),serde_json::to_vec(&json!({"schema":1,"role":"product","version":env!("CARGO_PKG_VERSION"),"build":build,"files":rows})).unwrap());
        let mut output=Cursor::new(vec![]);{let mut zip=zip::ZipWriter::new(&mut output);for (name,data) in entries{zip.start_file(name,zip::write::SimpleFileOptions::default()).unwrap();zip.write_all(&data).unwrap();}zip.finish().unwrap();}output.into_inner()
    }
    fn old_tts(root:&Path,legacy:bool){
        let folder=root.join(if legacy{"runtimes/audiocpp"}else{"models/audiocpp/runtime"});
        fs::create_dir_all(folder.join("cuda")).unwrap();fs::create_dir_all(folder.join("model_specs")).unwrap();
        fs::write(folder.join("cuda/audiocpp_server.exe"),b"old-native").unwrap();fs::write(folder.join("cuda/base.dll"),b"base").unwrap();fs::write(folder.join("model_specs/voxcpm2.json"),b"{}").unwrap();
        let records=crate::resources::walk(&folder).unwrap().iter().map(|path|json!({"path":path.strip_prefix(&folder).unwrap().to_string_lossy().replace('\\',"/"),"size":path.metadata().unwrap().len(),"sha256":file_hash(path).unwrap(),"optional":false})).collect();
        crate::resources::write_model_receipt(&folder,&json!({"resource_id":"tts-audiocpp-runtime","version":"old"}),records).unwrap();
        for (name,data) in [("models/audiocpp/tts/model.gguf",b"preserved-weight".as_slice()),("data/tts/voices/voice.json",b"preserved-voice".as_slice())]{let path=root.join(name);fs::create_dir_all(path.parent().unwrap()).unwrap();fs::write(path,data).unwrap();}
        atomic_json(&root.join("models/manifest.json"),&crate::resources::manifest(&root.join("models"),"models","old").unwrap()).unwrap();
    }
    #[test]fn bundled_tts_upgrade_is_offline_preserves_weights_and_combines_speaker_transaction(){
        for legacy in [false,true]{for speaker in [false,true]{
            let root=fixture();run(&package("old",false),&root).unwrap();old_tts(&root,legacy);
            if speaker{legacy_speaker(&root);}
            let payload=package_with_tts("new",speaker,false);
            for _ in 0..2{
                run(&payload,&root).unwrap();
                assert_eq!(fs::read(root.join("models/audiocpp/runtime/cuda/audiocpp_server.exe")).unwrap(),b"new-native");
                assert_eq!(fs::read(root.join("models/audiocpp/runtime/cuda/base.dll")).unwrap(),b"base");
                assert_eq!(crate::resources::read_model_receipt(&root.join("models/audiocpp/runtime")).unwrap().1.as_deref(),Some("fixed"));
                assert_eq!(fs::read(root.join("models/audiocpp/tts/model.gguf")).unwrap(),b"preserved-weight");
                assert_eq!(fs::read(root.join("data/tts/voices/voice.json")).unwrap(),b"preserved-voice");
                assert_eq!(fs::read(root.join("app/business.py")).unwrap(),b"new");
                workspace::verify_tree(&root.join("models"),"manifest.json").unwrap();
                let release=crate::resources::load(&root.join("app/release.json")).unwrap();
                let binding=release["components"].as_array().unwrap().iter().find(|row|row["kind"]=="models").unwrap();
                assert_eq!(binding["manifest_sha256"],file_hash(&root.join("models/manifest.json")).unwrap());
                assert!(!root.join("tmp/update-transaction.json").exists());
                if speaker{assert!(root.join(CURRENT_MODEL).is_dir());assert!(!root.join(LEGACY_MODEL).exists());}
            }
            if legacy{assert_eq!(fs::read(root.join("runtimes/audiocpp/cuda/audiocpp_server.exe")).unwrap(),b"old-native");}
            fs::remove_dir_all(root).unwrap();
        }}
    }
    #[test]fn tts_upgrade_repairs_bad_receipt_using_verified_base_and_skips_fresh_workspace(){
        let root=fixture();run(&package_with_tts("fresh",false,false),&root).unwrap();assert!(!root.join("models/audiocpp/runtime").exists());
        old_tts(&root,false);fs::write(root.join("models/audiocpp/runtime/resource-install.json"),b"invalid").unwrap();
        run(&package_with_tts("new",false,false),&root).unwrap();
        assert_eq!(crate::resources::read_model_receipt(&root.join("models/audiocpp/runtime")).unwrap().1.as_deref(),Some("fixed"));
        fs::remove_dir_all(root).unwrap();
    }
    #[test]fn incompatible_tts_base_defers_component_but_allows_program_repair(){
        let root=fixture();run(&package("old",false),&root).unwrap();old_tts(&root,false);
        fs::write(root.join("models/audiocpp/runtime/cuda/base.dll"),b"bad!").unwrap();let before=fs::read(root.join("models/manifest.json")).unwrap();
        run(&package_with_tts("new",false,false),&root).unwrap();
        assert_eq!(fs::read(root.join("app/business.py")).unwrap(),b"new");assert_eq!(fs::read(root.join("models/audiocpp/runtime/cuda/audiocpp_server.exe")).unwrap(),b"old-native");
        assert_eq!(fs::read(root.join("models/manifest.json")).unwrap(),before);
        assert!(crate::resources::load(&root.join("app/release.json")).unwrap()["deferred_migrations"]["tts_audiocpp"]["reason"].as_str().unwrap().contains("base.dll"));
        fs::remove_dir_all(root).unwrap();
    }
    #[test]fn invalid_bundled_tts_patch_preserves_old_program_and_runtime(){
        let root=fixture();run(&package("old",false),&root).unwrap();old_tts(&root,false);
        assert!(run(&package_with_tts("new",false,true),&root).unwrap_err().contains("补丁校验失败"));
        assert_eq!(fs::read(root.join("app/business.py")).unwrap(),b"old");assert_eq!(fs::read(root.join("models/audiocpp/runtime/cuda/audiocpp_server.exe")).unwrap(),b"old-native");
        workspace::verify_tree(&root.join("models"),"manifest.json").unwrap();fs::remove_dir_all(root).unwrap();
    }
    #[cfg(windows)]#[test]fn failed_manifest_activation_rolls_back_program_and_new_tts_together(){
        use std::os::windows::fs::OpenOptionsExt;
        let root=fixture();run(&package("old",false),&root).unwrap();old_tts(&root,false);
        let before=fs::read(root.join("models/manifest.json")).unwrap();
        let held=fs::OpenOptions::new().read(true).share_mode(1).open(root.join("models/manifest.json")).unwrap();
        assert!(run(&package_with_tts("new",false,false),&root).is_err());drop(held);
        assert_eq!(fs::read(root.join("app/business.py")).unwrap(),b"old");assert_eq!(fs::read(root.join("models/audiocpp/runtime/cuda/audiocpp_server.exe")).unwrap(),b"old-native");
        assert_eq!(fs::read(root.join("models/manifest.json")).unwrap(),before);
        assert!(!root.join("tmp/update-transaction.json").exists());workspace::verify_tree(&root.join("models"),"manifest.json").unwrap();fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn october_11_workspaces_upgrade_without_new_receipts_or_current_components(){
        // 2026-10-11（周日）发行使用第二版固定目录；较早包可能没有工作区编号，
        // webview2（网页引擎）字段也可能仍指向旧私有目录。组件版本不应阻止本体更新。
        for private_webview in [false,true]{for has_identity in [false,true]{for has_model_manifest in [false,true]{
            let root=fixture();
            let mut portable=json!({"schema":2,"app":"app","data_dir":"data","python":"runtimes/python/python.exe",
                "models":"models","dsh":"runtimes/dsh","webview2":if private_webview{json!("runtimes/webview2")}else{Value::Null}});
            if has_identity{portable["workspace_id"]=json!("sunday-workspace");}
            atomic_json(&root.join("portable.json"),&portable).unwrap();
            let old_components=json!([
                {"kind":"python","path":"runtimes/python","build":"old-python","manifest_sha256":"old-python-manifest"},
                {"kind":"dsh","path":"runtimes/dsh","build":"old-dsh","manifest_sha256":"old-dsh-manifest"},
                {"kind":"webview2","path":"runtimes/webview2","build":"old-webview","manifest_sha256":"old-webview-manifest"},
                {"kind":"models","path":"models","build":"old-models","manifest_sha256":"old-model-manifest"}]);
            atomic_json(&root.join("app/release.json"),&json!({"schema":1,"kind":"app","build":"product-5b9ebe8be7c9b254",
                "components":old_components,"files":[]})).unwrap();
            // 仅模拟旧内容，不执行解释器或宿主；本体升级不得检查旧依赖是否达到新版本。
            let preserved=[
                ("data/config/local.yaml",b"saved-input-and-output-settings".as_slice()),
                ("data/config/product.json",b"saved-model-choice-and-credentials-reference".as_slice()),
                ("data/persona/user.md",b"saved-persona".as_slice()),
                ("data/history/conversation.jsonl",b"saved-conversation".as_slice()),
                ("data/dsh/session.json",b"saved-background-session".as_slice()),
                ("data/speaker/campplus-zh-cn-common/registry.json",b"existing-current-profile".as_slice()),
                ("runtimes/python/python.exe",b"old-python-binary".as_slice()),
                ("runtimes/python/runtime.json",br#"{"schema":1,"profile":"cu128"}"#.as_slice()),
                ("runtimes/dsh/runtime.json",br#"{"schema":1,"source_kind":"legacy_export","version":"0.2.0-rc.2"}"#.as_slice()),
                ("runtimes/dsh/legacy.exe",b"old-background-host".as_slice()),
                ("runtimes/webview2/msedgewebview2.exe",b"old-private-webview".as_slice()),
                ("models/asr/old-model/model.bin",b"installed-old-model-without-receipt".as_slice()),
            ];
            for (relative,bytes) in &preserved{let path=root.join(relative);fs::create_dir_all(path.parent().unwrap()).unwrap();fs::write(path,bytes).unwrap();}
            legacy_speaker(&root);
            if has_model_manifest{atomic_json(&root.join("models/manifest.json"),&crate::resources::manifest(&root.join("models"),"models","sunday").unwrap()).unwrap();}
            assert!(!root.join("models/asr/old-model/resource-install.json").exists());
            run(&package_options("current",false,Some(false)),&root).unwrap();
            for (relative,bytes) in &preserved{assert_eq!(fs::read(root.join(relative)).unwrap(),*bytes,"保留文件：{relative}");}
            assert!(!root.join(LEGACY_MODEL).exists());assert!(root.join(CURRENT_MODEL).is_dir());
            let upgraded=crate::resources::load(&root.join("portable.json")).unwrap();
            assert!(upgraded["webview2"].is_null());
            let identity=upgraded["workspace_id"].as_str().filter(|id|!id.is_empty()).unwrap().to_owned();
            if has_identity{assert_eq!(identity,"sunday-workspace");}
            let release=crate::resources::load(&root.join("app/release.json")).unwrap();assert_eq!(release["build"],"current");
            for index in 0..3{assert_eq!(release["components"][index],old_components[index]);}
            workspace::verify_tree(&root.join("models"),"manifest.json").unwrap();
            // 再次安装同一包时保持新补齐的工作区编号、已有CAM++档案及旧组件。
            run(&package_options("current",false,Some(false)),&root).unwrap();
            assert_eq!(crate::resources::load(&root.join("portable.json")).unwrap()["workspace_id"],identity);
            for (relative,bytes) in &preserved{assert_eq!(fs::read(root.join(relative)).unwrap(),*bytes,"重复升级保留文件：{relative}");}
            fs::remove_dir_all(root).unwrap();
        }}}
    }
    #[test]fn reinstall_same_or_new_build_preserves_data_identity_and_components(){
        let root=fixture();run(&package("first",false),&root).unwrap();let id=crate::resources::load(&root.join("portable.json")).unwrap()["workspace_id"].clone();
        fs::write(root.join("data/settings.json"),b"user-settings").unwrap();fs::create_dir_all(root.join("runtimes/python")).unwrap();fs::write(root.join("runtimes/python/content.bin"),b"installed-python").unwrap();fs::write(root.join("models/model.bin"),b"installed-model").unwrap();
        let components=json!([{"kind":"python","path":"runtimes/python","build":"existing","manifest_sha256":"fixture"}]);let mut release=crate::resources::load(&root.join("app/release.json")).unwrap();release["components"]=components.clone();atomic_json(&root.join("app/release.json"),&release).unwrap();
        for build in ["first","second"]{run(&package(build,false),&root).unwrap();assert_eq!(fs::read(root.join("app/business.py")).unwrap(),build.as_bytes());assert_eq!(fs::read(root.join("data/settings.json")).unwrap(),b"user-settings");assert_eq!(fs::read(root.join("runtimes/python/content.bin")).unwrap(),b"installed-python");assert_eq!(fs::read(root.join("models/model.bin")).unwrap(),b"installed-model");assert_eq!(crate::resources::load(&root.join("portable.json")).unwrap()["workspace_id"],id);assert_eq!(crate::resources::load(&root.join("app/release.json")).unwrap()["components"],components);assert!(!root.join("tmp/update-transaction.json").exists());}
        fs::remove_dir_all(root).unwrap();
    }
    #[test]fn corrupt_package_and_cancel_do_not_replace_formal_program(){
        let root=fixture();run(&package("first",false),&root).unwrap();assert!(run(&package("broken",true),&root).is_err());
        let cancel=Arc::new(AtomicBool::new(false));let trigger=cancel.clone();let result=install(&package("cancelled",false),&root,cancel,&mut|phase,_,_|{if phase=="正在校验程序本体"{trigger.store(true,Ordering::Release);}});assert!(result.unwrap_err().contains("取消"));assert_eq!(fs::read(root.join("app/business.py")).unwrap(),b"first");fs::remove_dir_all(root).unwrap();
    }
    #[test]fn unrelated_nonempty_directory_is_rejected_without_mutation(){let root=fixture();fs::write(root.join("personal.txt"),b"personal").unwrap();assert!(run(&package("first",false),&root).unwrap_err().contains("非空"));assert!(!root.join("portable.json").exists());assert_eq!(fs::read(root.join("personal.txt")).unwrap(),b"personal");fs::remove_dir_all(root).unwrap();}
    #[cfg(windows)]#[test]fn workspace_lock_and_failed_activation_keep_original_program(){
        use std::os::windows::fs::OpenOptionsExt;
        let root=fixture();run(&package("first",false),&root).unwrap();let guard=WorkspaceGuard::acquire(&root).unwrap();assert!(run(&package("second",false),&root).unwrap_err().contains("正在运行"));drop(guard);
        let held=fs::OpenOptions::new().read(true).share_mode(0).open(root.join("VoiceTerminal.exe")).unwrap();assert!(run(&package("second",false),&root).is_err());drop(held);assert_eq!(fs::read(root.join("app/business.py")).unwrap(),b"first");assert_eq!(fs::read(root.join("VoiceTerminal.exe")).unwrap(),b"formal-entry");assert!(!root.join("tmp/update-transaction.json").exists());fs::remove_dir_all(root).unwrap();
    }
    #[test]fn interrupted_activation_restores_previous_body(){let root=fixture();run(&package("first",false),&root).unwrap();fs::create_dir_all(root.join("tmp/publish/old")).unwrap();fs::rename(root.join("app"),root.join("tmp/publish/old/app")).unwrap();fs::create_dir_all(root.join("app")).unwrap();fs::write(root.join("app/partial"),b"partial").unwrap();atomic_json(&root.join("tmp/update-transaction.json"),&json!({"schema":1,"state":"prepared","items":[{"path":"app","original":true}]})).unwrap();workspace::recover_install(&root).unwrap();assert_eq!(fs::read(root.join("app/business.py")).unwrap(),b"first");assert!(!root.join("app/partial").exists());fs::remove_dir_all(root).unwrap();}
    fn legacy_speaker(root:&Path){
        fs::create_dir_all(root.join(LEGACY_MODEL)).unwrap();fs::write(root.join(LEGACY_MODEL).join("cnceleb_resnet34.onnx"),b"legacy-weight").unwrap();
        fs::create_dir_all(root.join("data/speaker/templates")).unwrap();fs::write(root.join("data/speaker/templates/old.npy"),b"legacy-vector").unwrap();
        fs::write(root.join("data/speaker/probe.npy"),b"legacy-probe").unwrap();atomic_json(&root.join("data/speaker/registry.json"),&json!({"model_id":"wespeaker-cnceleb-resnet34","schema_version":2})).unwrap();
    }
    #[test]fn fresh_install_creates_model_parent_and_seeds_campplus(){
        let root=fixture();assert!(!root.join("models/speaker").exists());
        run(&package_options("fresh",false,Some(false)),&root).unwrap();
        assert_eq!(fs::read(root.join(CURRENT_MODEL).join("campplus_cn_common.bin")).unwrap(),b"native-model-fixture");
        let manifest=crate::resources::load(&root.join("models/manifest.json")).unwrap();
        assert!(manifest["files"].as_array().unwrap().iter().any(|row|row["path"]=="speaker/campplus-zh-cn-common/campplus_cn_common.bin"));
        let release=crate::resources::load(&root.join("app/release.json")).unwrap();
        let binding=release["components"].as_array().unwrap().iter().find(|row|row["kind"]=="models").unwrap();
        assert_eq!(binding["manifest_sha256"],file_hash(&root.join("models/manifest.json")).unwrap());
        workspace::verify_tree(&root.join("models"),"manifest.json").unwrap();
        fs::remove_dir_all(root).unwrap();
    }
    #[test]fn speaker_upgrade_deletes_legacy_assets_and_keeps_current_profiles_and_preferences(){
        let root=fixture();run(&package("old",false),&root).unwrap();legacy_speaker(&root);
        fs::create_dir_all(root.join("data/speaker/campplus-zh-cn-common")).unwrap();fs::write(root.join("data/speaker/campplus-zh-cn-common/registry.json"),b"current-registration").unwrap();
        fs::write(root.join("data/preferences.json"),b"saved-filter-thresholds").unwrap();fs::write(root.join("models/other.bin"),b"other-model").unwrap();
        run(&package_options("new",false,Some(false)),&root).unwrap();
        assert!(!root.join(LEGACY_MODEL).exists());for path in RETIRED_PROFILE_PATHS{assert!(!root.join(path).exists());}
        assert_eq!(fs::read(root.join(CURRENT_MODEL).join("campplus_cn_common.bin")).unwrap(),b"native-model-fixture");
        assert_eq!(fs::read(root.join("data/speaker/campplus-zh-cn-common/registry.json")).unwrap(),b"current-registration");assert_eq!(fs::read(root.join("data/preferences.json")).unwrap(),b"saved-filter-thresholds");
        let manifest=crate::resources::load(&root.join("models/manifest.json")).unwrap();assert!(!manifest["files"].as_array().unwrap().iter().any(|row|row["path"].as_str().unwrap().contains("wespeaker")));
        let release=crate::resources::load(&root.join("app/release.json")).unwrap();let binding=release["components"].as_array().unwrap().iter().find(|row|row["kind"]=="models").unwrap();assert_eq!(binding["manifest_sha256"],file_hash(&root.join("models/manifest.json")).unwrap());
        workspace::verify_tree(&root.join("models"),"manifest.json").unwrap();
        run(&package_options("new",false,Some(false)),&root).unwrap();assert_eq!(fs::read(root.join("data/speaker/campplus-zh-cn-common/registry.json")).unwrap(),b"current-registration");
        fs::remove_dir_all(root).unwrap();
    }
    #[test]fn completed_speaker_migration_does_not_rebuild_or_scan_unrelated_models(){
        let root=fixture();run(&package_options("first",false,Some(false)),&root).unwrap();
        let manifest=root.join("models/manifest.json");let before=fs::read(&manifest).unwrap();
        let receipt=root.join(CURRENT_MODEL).join("resource-install.json");let installed=fs::read(&receipt).unwrap();
        fs::write(root.join("models/unrelated.bin"),b"unrelated model outside this migration").unwrap();
        run(&package_options("next",false,Some(false)),&root).unwrap();
        assert_eq!(fs::read(&manifest).unwrap(),before);
        assert_eq!(fs::read(&receipt).unwrap(),installed);
        assert_eq!(fs::read(root.join("app/business.py")).unwrap(),b"next");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]fn invalid_seed_leaves_old_body_model_and_profile_untouched(){
        let root=fixture();run(&package("old",false),&root).unwrap();legacy_speaker(&root);
        assert!(run(&package_options("new",false,Some(true)),&root).is_err());
        assert_eq!(fs::read(root.join("app/business.py")).unwrap(),b"old");assert_eq!(fs::read(root.join("data/speaker/templates/old.npy")).unwrap(),b"legacy-vector");assert!(root.join(LEGACY_MODEL).is_dir());assert!(!root.join(CURRENT_MODEL).exists());fs::remove_dir_all(root).unwrap();
    }
    #[test]fn stale_unreceipted_model_defers_speaker_migration_without_blocking_program_upgrade(){
        let root=fixture();run(&package("old",false),&root).unwrap();legacy_speaker(&root);
        fs::write(root.join("models/other.bin"),b"valid").unwrap();let sha=file_hash(&root.join("models/other.bin")).unwrap();
        atomic_json(&root.join("models/manifest.json"),&json!({"files":[{"path":"other.bin","size":5,"sha256":sha}]})).unwrap();fs::write(root.join("models/other.bin"),b"wrong").unwrap();
        let old_manifest=fs::read(root.join("models/manifest.json")).unwrap();
        run(&package_options("new",false,Some(false)),&root).unwrap();
        assert_eq!(fs::read(root.join("app/business.py")).unwrap(),b"new");assert!(root.join(LEGACY_MODEL).is_dir());assert!(root.join("data/speaker/registry.json").is_file());
        assert_eq!(fs::read(root.join("models/other.bin")).unwrap(),b"wrong");
        assert_eq!(fs::read(root.join("models/manifest.json")).unwrap(),old_manifest);assert!(!root.join(CURRENT_MODEL).exists());
        let release=crate::resources::load(&root.join("app/release.json")).unwrap();
        assert!(release["deferred_migrations"]["speaker"]["reason"].as_str().unwrap().contains("保留模型文件校验失败"));
        // 修复旧组件后重装，迁移继续执行；旧声纹资料只在候选校验通过后清退。
        fs::write(root.join("models/other.bin"),b"valid").unwrap();
        run(&package_options("new",false,Some(false)),&root).unwrap();
        assert!(!root.join(LEGACY_MODEL).exists());assert!(root.join(CURRENT_MODEL).is_dir());
        assert!(crate::resources::load(&root.join("app/release.json")).unwrap()["deferred_migrations"].is_null());
        workspace::verify_tree(&root.join("models"),"manifest.json").unwrap();fs::remove_dir_all(root).unwrap();
    }
    #[test]fn unidentified_flat_profile_is_preserved_without_blocking_update(){
        let root=fixture();run(&package("old",false),&root).unwrap();legacy_speaker(&root);fs::write(root.join("data/speaker/registry.json"),b"unidentified-profile").unwrap();
        run(&package_options("new",false,Some(false)),&root).unwrap();assert_eq!(fs::read(root.join("data/speaker/registry.json")).unwrap(),b"unidentified-profile");assert!(!root.join(LEGACY_MODEL).exists());assert!(root.join(CURRENT_MODEL).is_dir());fs::remove_dir_all(root).unwrap();
    }
    #[test]fn interrupted_speaker_deletion_is_restored_and_unknown_profile_path_is_refused(){
        let root=fixture();run(&package("old",false),&root).unwrap();legacy_speaker(&root);
        let relative="data/speaker/registry.json";let backup=root.join("tmp/publish/old").join(relative);fs::create_dir_all(backup.parent().unwrap()).unwrap();fs::rename(root.join(relative),&backup).unwrap();
        let journal=root.join("tmp/update-transaction.json");atomic_json(&journal,&json!({"schema":1,"state":"prepared","items":[{"path":relative,"original":true,"delete_only":true},{"path":"data/speaker/campplus-zh-cn-common","original":true,"delete_only":true}]})).unwrap();
        assert!(workspace::recover_install(&root).is_err());assert!(backup.is_file());
        atomic_json(&journal,&json!({"schema":1,"state":"prepared","items":[{"path":relative,"original":true,"delete_only":true}]})).unwrap();workspace::recover_install(&root).unwrap();assert!(root.join(relative).is_file());assert!(!journal.exists());fs::remove_dir_all(root).unwrap();
    }
    #[cfg(windows)]#[test]fn failed_speaker_activation_restores_model_and_legacy_profile(){
        use std::os::windows::fs::OpenOptionsExt;
        let root=fixture();run(&package("old",false),&root).unwrap();legacy_speaker(&root);
        let held=fs::OpenOptions::new().read(true).share_mode(0).open(root.join("data/speaker/probe.npy")).unwrap();
        assert!(run(&package_options("new",false,Some(false)),&root).is_err());drop(held);
        assert_eq!(fs::read(root.join("app/business.py")).unwrap(),b"old");assert!(root.join(LEGACY_MODEL).is_dir());assert_eq!(fs::read(root.join("data/speaker/templates/old.npy")).unwrap(),b"legacy-vector");assert!(!root.join(CURRENT_MODEL).exists());assert!(!root.join("tmp/update-transaction.json").exists());fs::remove_dir_all(root).unwrap();
    }
    #[test]fn retained_model_change_confirmed_by_component_receipt_is_reissued(){
        let root=fixture();run(&package("old",false),&root).unwrap();legacy_speaker(&root);
        let folder=root.join("models/audiocpp/runtime");fs::create_dir_all(&folder).unwrap();fs::write(folder.join("model.bin"),b"bbbb").unwrap();
        let item=json!({"resource_id":"tts-audiocpp-runtime","version":"v1","destination":"audiocpp/runtime",
            "files":[{"path":"model.bin","size_bytes":4,"sha256":file_hash(&folder.join("model.bin")).unwrap()}]});
        crate::resources::write_model_receipt(&folder,&item,vec![json!({"path":"model.bin","size":4,"sha256":item["files"][0]["sha256"],"optional":false})]).unwrap();
        // 旧总清单记录的是换版前的收据与权重摘要；收据登记的文件与磁盘一致，属于可修复的登记差异。
        atomic_json(&root.join("models/manifest.json"),&json!({"files":[
            {"path":"audiocpp/runtime/model.bin","size":4,"sha256":"0".repeat(64)},
            {"path":"audiocpp/runtime/resource-install.json","size":1,"sha256":"1".repeat(64)}]})).unwrap();
        run(&package_options("new",false,Some(false)),&root).unwrap();
        assert!(!root.join(LEGACY_MODEL).exists());
        let manifest=crate::resources::load(&root.join("models/manifest.json")).unwrap();
        for path in ["audiocpp/runtime/model.bin","audiocpp/runtime/resource-install.json"]{
            let record=manifest["files"].as_array().unwrap().iter().find(|row|row["path"]==path).unwrap();
            assert_eq!(record["sha256"],json!(file_hash(&root.join("models").join(path)).unwrap()));
        }
        workspace::verify_tree(&root.join("models"),"manifest.json").unwrap();
        fs::remove_dir_all(root).unwrap();
    }
    #[test]fn mismatched_receipt_defers_migration_and_keeps_existing_model_registration(){
        let root=fixture();run(&package("old",false),&root).unwrap();legacy_speaker(&root);
        let folder=root.join("models/audiocpp/runtime");fs::create_dir_all(&folder).unwrap();fs::write(folder.join("model.bin"),b"bbbb").unwrap();
        let changed=file_hash(&folder.join("model.bin")).unwrap();
        // 收据登记的是同长度但不同的内容：暂缓声纹迁移，不能按实际内容重新登记。
        let item=json!({"resource_id":"tts-audiocpp-runtime","version":"v1","destination":"audiocpp/runtime",
            "files":[{"path":"model.bin","size_bytes":4,"sha256":"a".repeat(64)}]});
        crate::resources::write_model_receipt(&folder,&item,vec![json!({"path":"model.bin","size":4,"sha256":"a".repeat(64),"optional":false})]).unwrap();
        atomic_json(&root.join("models/manifest.json"),&json!({"files":[{"path":"audiocpp/runtime/model.bin","size":4,"sha256":"a".repeat(64)}]})).unwrap();
        let old_manifest=fs::read(root.join("models/manifest.json")).unwrap();
        let old_receipt=fs::read(folder.join("resource-install.json")).unwrap();
        run(&package_options("new",false,Some(false)),&root).unwrap();
        assert_eq!(fs::read(root.join("app/business.py")).unwrap(),b"new");assert!(root.join(LEGACY_MODEL).is_dir());assert!(!root.join(CURRENT_MODEL).exists());
        assert_eq!(fs::read(root.join("models/manifest.json")).unwrap(),old_manifest);
        assert_eq!(fs::read(folder.join("resource-install.json")).unwrap(),old_receipt);
        assert!(crate::resources::load(&root.join("app/release.json")).unwrap()["deferred_migrations"]["speaker"]["reason"].as_str().unwrap().contains("与安装收据不符"));
        assert_eq!(file_hash(&folder.join("model.bin")).unwrap(),changed);
        fs::remove_dir_all(root).unwrap();
    }
}
