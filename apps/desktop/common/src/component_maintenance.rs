//! Component maintenance（组件校验交接）：分发器记录问题，本体通过既有组件任务执行修复。
use std::{collections::HashSet, fs, sync::Mutex};
use serde_json::{json, Value};
use sha2::{Digest,Sha256};
use crate::{LaunchContext, resources, workspace::{atomic_json, safe_path}};

const RECORD: &str = "state/component-maintenance.json"; // RECORD（组件校验交接记录）：相对于用户数据目录。
static RECORD_LOCK: Mutex<()> = Mutex::new(()); // RECORD_LOCK（本体内记录写入互斥）。

fn target(context:&LaunchContext,id:&str)->Result<String,String>{
    let value=if matches!(id,crate::runtime::CPU_RESOURCE|crate::runtime::GPU_RESOURCE){
        let file=if id==crate::runtime::GPU_RESOURCE{"config/runtime/voice-gpu.lock.json"}else{"config/runtime/voice-runtime.lock.json"};
        resources::load(&context.installed_program().join(file)).unwrap_or(Value::Null)
    }else{let item=resources::spec(context,id)?;json!([item["version"],item["files"],item["source"]["distribution"]])};
    Ok(format!("{:x}",Sha256::digest(value.to_string().as_bytes())))
}

/// load（读取本次构建的交接）：过期结果不参与当前组件判断，不接受路径或下载地址。
pub fn load(context:&LaunchContext)->Result<Option<Value>,String>{
    let path=safe_path(&context.data,RECORD)?;
    if !path.exists(){return Ok(None);}
    if fs::metadata(&path).map_err(|e|e.to_string())?.len()>1024*1024{return Err("组件校验交接记录超过大小限制。".into());}
    let Ok(value)=resources::load(&path)else{return Ok(None)};
    let release=resources::load(&context.installed_program().join("release.json"))?;
    if value["schema"]!=1||value["workspace_id"]!=context.workspace_id||value["build"]!=release["build"]{return Ok(None);}
    let issues=value["issues"].as_array().filter(|rows|rows.len()<=128).ok_or("组件校验交接清单无效。")?;
    let mut seen=HashSet::new();
    for issue in issues{
        let id=issue["resource_id"].as_str().ok_or("组件校验交接缺少资源编号。")?;
        if !seen.insert(id)||!issue["reason"].is_string()
            ||!issue["content_failure"].is_boolean()||!issue["dispatched"].is_boolean(){return Err("组件校验交接包含未知或重复组件。".into());}
    }
    if let Some(migrations)=value.get("migrations"){
        let migrations=migrations.as_array().filter(|rows|rows.len()<=2).ok_or("离线迁移交接清单无效。")?;
        for row in migrations{
            if !matches!(row["resource_id"].as_str(),Some("campplus-zh-cn-common"|"tts-audiocpp-runtime"))||!row["dispatched"].is_boolean(){return Err("离线迁移交接包含未知组件。".into());}
        }
    }
    Ok(Some(value))
}

/// save（保存校验结论）：普通更新不签发自动修复请求，也不能抹掉已确认的同长度内容损坏。
pub(crate) fn save(context:&LaunchContext,mut issues:Vec<Value>,integrity:bool)->Result<(),String>{
    let _guard=RECORD_LOCK.lock().map_err(|_|"组件校验记录锁不可用。")?;
    for issue in &mut issues{issue["target"]=json!(target(context,issue["resource_id"].as_str().ok_or("组件编号缺失")?)?);}
    if !integrity{if let Ok(prior)=resources::load(&safe_path(&context.data,RECORD)?){
        if prior["workspace_id"]==context.workspace_id{
        for issue in prior["issues"].as_array().into_iter().flatten().filter(|issue|issue["content_failure"]==true){
            if issue["resource_id"].as_str().is_some_and(|id|target(context,id).is_ok_and(|identity|issue["target"]==identity)){
                issues.retain(|row|row["resource_id"]!=issue["resource_id"]);issues.push(issue.clone());
            }
        }
    }}}
    let release=resources::load(&context.installed_program().join("release.json"))?;
    let migrations=crate::component_upgrade::pending(context)?.into_iter().map(|id|json!({"resource_id":id,"dispatched":false})).collect::<Vec<_>>();
    atomic_json(&safe_path(&context.data,RECORD)?,&json!({
        "schema":1,"workspace_id":context.workspace_id,"build":release["build"],
        "operation":if integrity{"integrity"}else{"update"},"issues":issues,"migrations":migrations,
        "fields":{"schema":"组件校验交接格式","workspace_id":"所属工作区编号","build":"接受检查的本体构建",
            "operation":"普通更新或用户主动完整性维修","issues":"组件问题及本体任务派发状态","target":"组件目标内容身份，普通本体换版时保留未变化目标的内容故障",
            "resource_id":"当前资源目录内的组件编号","reason":"校验问题中文说明","content_failure":"是否有完整内容校验失败证据",
            "dispatched":"对应组件任务已实际创建；失败或取消后由用户重试","migrations":"本次交接的随包离线兼容迁移，不允许网络下载"}
    }))
}

/// pending_repairs（待交接维修）：仅用户主动完整性维修会派发；普通更新保持手动组件操作。
pub fn pending_repairs(context:&LaunchContext)->Result<bool,String>{
    Ok(!pending_requests(context)?.is_empty())
}

/// pending_requests（尚未创建任务的交接）：离线迁移与主动维修分别保留执行策略。
pub fn pending_requests(context:&LaunchContext)->Result<Vec<Value>,String>{
    let Some(record)=load(context)?else{return Ok(vec![])};
    let mut requests=vec![];let mut seen=HashSet::new();
    if record["operation"]=="integrity"{for issue in record["issues"].as_array().unwrap().iter().filter(|row|row["dispatched"]==false){
        if let Some(id)=issue["resource_id"].as_str(){seen.insert(id.to_owned());requests.push(json!({"resource_id":id,"offline":false,"fields":{"resource_id":"待处理组件编号","offline":"仅可使用随包或本机资源，不允许网络下载"}}));}
    }}
    for row in record["migrations"].as_array().into_iter().flatten().filter(|row|row["dispatched"]==false){
        if let Some(id)=row["resource_id"].as_str(){if seen.insert(id.to_owned()){requests.push(json!({"resource_id":id,"offline":true,"fields":{"resource_id":"待处理组件编号","offline":"仅可使用随包或本机资源，不允许网络下载"}}));}}
    }
    Ok(requests)
}

/// dispatched（实际创建后确认）：父任务准入失败、前置任务失败和取消都不能吞掉尚未创建的任务。
pub fn dispatched(context:&LaunchContext,id:&str)->Result<(),String>{
    let _guard=RECORD_LOCK.lock().map_err(|_|"组件校验记录锁不可用。")?;
    let Some(mut record)=load(context)?else{return Ok(())};
    for field in ["issues","migrations"]{if let Some(rows)=record[field].as_array_mut(){for row in rows{
        if row["resource_id"]==id{row["dispatched"]=json!(true);}
    }}}
    atomic_json(&safe_path(&context.data,RECORD)?,&record)
}

/// resolved（启用成功后清除）：轻量盘点或任务结束本身不能撤销完整校验失败。
pub fn resolved(context:&LaunchContext,ids:&[String])->Result<(),String>{
    let _guard=RECORD_LOCK.lock().map_err(|_|"组件校验记录锁不可用。")?;
    let Some(mut record)=load(context)?else{return Ok(())};
    record["issues"].as_array_mut().unwrap().retain(|issue|!ids.iter().any(|id|issue["resource_id"]==*id));
    if let Some(rows)=record["migrations"].as_array_mut(){rows.retain(|row|!ids.iter().any(|id|row["resource_id"]==*id));}
    atomic_json(&safe_path(&context.data,RECORD)?,&record)
}

/// apply（合并已知内容故障）：保留完整检查证据，普通启动仍只做既有轻量盘点。
pub(crate) fn apply(context:&LaunchContext,rows:&mut [Value])->Result<(),String>{
    let Some(record)=load(context)?else{return Ok(())};
    for issue in record["issues"].as_array().unwrap().iter().filter(|issue|issue["content_failure"]==true){
        let id=issue["resource_id"].as_str().unwrap();
        for row in rows.iter_mut(){
            if row["resource_id"]==id||(id==crate::runtime::CPU_RESOURCE&&row["resource_id"]=="voice-server"){
                row["can_start"]=json!(false);row["files_installed"]=json!(false);row["installation_state"]=json!("incomplete");row["error"]=issue["reason"].clone();
            }
            if let Some(children)=row["runtime_components"].as_array_mut(){for child in children{
                if child["resource_id"]==id{child["can_start"]=json!(false);child["error"]=issue["reason"].clone();}
            }}
        }
    }
    resources::update_tts_readiness(context,rows)?;
    if rows.iter().skip(1).any(|row|row["required"]!=false&&row["can_start"]!=true){
        if let Some(server)=rows.first_mut(){server["can_start"]=json!(false);}
    }
    Ok(())
}
