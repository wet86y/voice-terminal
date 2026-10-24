//! Compatibility（兼容性组件）：规则随程序分发，记录由当前工作区即时盘点生成。
use std::{fs, path::Path, sync::{Mutex, MutexGuard, atomic::AtomicBool}, time::{SystemTime, UNIX_EPOCH}};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use crate::{LaunchContext, resources::{self, Progress}, workspace::{atomic_json, safe_path}};

pub const TRANSACTION_PATHS: [&str; 2] = ["data/state/compatibility.json", "data/config/product-settings.json"];
const DEFAULT_RULES: &str = include_str!("../../../../config/compatibility.json");
static EVALUATION: Mutex<()> = Mutex::new(()); // EVALUATION（评估互斥）：扫描和写回串行，旧扫描不能晚写覆盖新结果。
#[path = "compatibility_repair.rs"]
mod forced_repair;
pub use forced_repair::{enforce_upgrade, verify_and_repair, finish_deferred_settings, MaintenanceOutcome};

fn lock() -> Result<MutexGuard<'static, ()>, String> { EVALUATION.lock().map_err(|_| "兼容性评估锁不可用。".into()) }
fn now() -> u128 { SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis() }
fn digest(value: &Value) -> String { format!("{:x}", Sha256::digest(serde_json::to_vec(value).unwrap_or_default())) }
fn state_path(context: &LaunchContext) -> Result<std::path::PathBuf,String> {
    if crate::workspace::metadata_reparse(&fs::symlink_metadata(&context.data).map_err(|e|e.to_string())?) {return Err("兼容状态的数据根目录不能是联接或链接。".into());}
    safe_path(&context.data,"state/compatibility.json")
}

fn read(path: &Path) -> Result<Value, String> {
    if fs::metadata(path).map_err(|e| e.to_string())?.len() > 4 * 1024 * 1024 { return Err("兼容性元数据超过大小限制。".into()); }
    resources::load(path)
}

pub fn is_transaction_path(relative: &str) -> bool {
    TRANSACTION_PATHS.contains(&relative) || relative.strip_prefix("data/state/compatibility-preserved-")
        .and_then(|name|name.strip_suffix(".json")).is_some_and(|hash|hash.len()==64 && hash.bytes().all(|ch|ch.is_ascii_hexdigit()))
}

fn rules(program: &Path, source_debug: bool) -> Result<Value, String> {
    let path = safe_path(program, "config/compatibility.json")?;
    let release = if source_debug { Value::Null } else { read(&program.join("release.json"))? };
    if !source_debug && (release["compatibility_engine"]["schema"] != 1
        || release["compatibility_engine"]["rules_file"] != "config/compatibility.json" || !path.is_file()) {
        return Err("程序声明的兼容协议不受支持或规则文件缺失，请修复程序本体。".into());
    }
    if path.exists() && !source_debug {
        let entry = release["files"].as_array().and_then(|files|files.iter().find(|entry|entry["path"]=="config/compatibility.json")).ok_or("兼容规则未登记在程序文件清单中。")?;
        if fs::metadata(&path).map_err(|e|e.to_string())?.len() != entry["size"].as_u64().ok_or("兼容规则大小记录无效。")?
            || crate::workspace::file_hash(&path)? != entry["sha256"].as_str().ok_or("兼容规则摘要记录无效。")? {
            return Err("兼容规则与程序发布清单不符，请修复程序本体。".into());
        }
    }
    let value = if path.exists() { read(&path)? } else { serde_json::from_str(DEFAULT_RULES).map_err(|e| e.to_string())? };
    if value["schema"] != 1 || value["revision"].as_u64().is_none() || value["settings_schema_max"].as_u64().unwrap_or(0) < 1
        || !value["settings_rules"].is_array() || !value["upgrade_handlers"].is_array() { return Err("不支持的兼容规则格式。".into()); }
    let mut handlers = std::collections::HashSet::new();
    for handler in value["upgrade_handlers"].as_array().unwrap() {
        if !matches!(handler.as_str(), Some("speaker" | "tts_audiocpp")) || !handlers.insert(handler.as_str().unwrap()) { return Err("兼容规则包含未知或重复的迁移处理器。".into()); }
    }
    let mut ids = std::collections::HashSet::new();
    for rule in value["settings_rules"].as_array().unwrap() {
        let id = rule["id"].as_str().ok_or("设置迁移缺少规则编号。")?;
        if id.is_empty() || id.len() > 128 || !ids.insert(id) { return Err("设置迁移规则编号为空、过长或重复。".into()); }
        if !matches!(rule["action"].as_str(), Some("rename_field" | "remove_field" | "replace_value"))
            || rule.get("expected").is_none() || rule["min_schema"].as_u64().is_none()
            || rule["max_schema"].as_u64().is_none() || rule["min_schema"].as_u64() > rule["max_schema"].as_u64() {
            return Err("设置迁移动作或适用格式范围无效。".into());
        }
        field(rule["field"].as_str().ok_or("设置迁移缺少字段。")?)?;
        if rule["action"] == "rename_field" { field(rule["target"].as_str().ok_or("字段改名缺少目标。")?)?; }
        if rule["action"] == "replace_value" && rule.get("value").is_none() { return Err("字段替换缺少新值。".into()); }
    }
    Ok(value)
}

fn field(pointer: &str) -> Result<(&str, &str), String> {
    let parts: Vec<_> = pointer.split('/').collect();
    if parts.len() < 3 || parts[0] != "" || !matches!(parts[1], "speaker" | "compute" | "tts" | "audio_enhancements" | "wake" | "silence" | "shadow" | "quiet_hours" | "screen_capture" | "approvals")
        || parts[1..].iter().any(|part| part.is_empty() || part.len() > 128 || !part.bytes().all(|ch| ch.is_ascii_alphanumeric() || ch == b'_' || ch == b'-')) {
        return Err("设置迁移必须指定允许分区内的精确对象字段。".into());
    }
    pointer.rsplit_once('/').ok_or_else(|| "设置迁移字段无效。".into())
}

fn transform(settings: &mut Value, policy: &Value) -> Result<Vec<String>, String> {
    let mut applied = Vec::new();
    let schema = match settings.get("schema_version") { None => 1, Some(value) => match value.as_u64() { Some(value) if value > 0 => value, _ => return Ok(applied) } };
    if !settings.is_object() || schema > policy["settings_schema_max"].as_u64().unwrap() { return Ok(applied); }
    for rule in policy["settings_rules"].as_array().unwrap() {
        if schema < rule["min_schema"].as_u64().unwrap() || schema > rule["max_schema"].as_u64().unwrap() { continue; }
        let source = rule["field"].as_str().unwrap();
        let Some(value) = settings.pointer(source).cloned() else { continue; };
        if value != rule["expected"] { continue; }
        let (parent, key) = field(source)?;
        match rule["action"].as_str().unwrap() {
            "rename_field" => {
                let target = rule["target"].as_str().unwrap();
                // 新字段已有值时保留两份，避免把用户的新偏好或身份不明资料覆盖掉。
                if settings.pointer(target).is_some() { continue; }
                let (target_parent, target_key) = field(target)?;
                let Some(object) = settings.pointer_mut(target_parent).and_then(Value::as_object_mut) else { continue; };
                object.insert(target_key.into(), value);
                settings.pointer_mut(parent).and_then(Value::as_object_mut).ok_or("迁移源字段不是对象。")?.remove(key);
            }
            "remove_field" => { settings.pointer_mut(parent).and_then(Value::as_object_mut).ok_or("迁移字段不是对象。")?.remove(key); }
            "replace_value" => { *settings.pointer_mut(source).unwrap() = rule["value"].clone(); }
            _ => return Err("不支持的设置迁移动作。".into()),
        }
        applied.push(rule["id"].as_str().unwrap().into());
    }
    Ok(applied)
}

fn previous(path: &Path) -> Result<(Value, Option<std::path::PathBuf>), String> {
    if !path.exists() { return Ok((Value::Null, None)); }
    // 超大派生记录不读入内存、不作为升级门槛；流式保留后重新建立。
    match read(path) { Ok(value) if value["schema"]==1 => Ok((value,None)), _ => Ok((Value::Null,Some(path.into()))) }
}

fn preserve_file(source: &Path, destination_root: &Path) -> Result<String,String> {
    let hash=crate::workspace::file_hash(source)?;
    let relative=format!("data/state/compatibility-preserved-{hash}.json");
    let target=safe_path(destination_root,&relative)?;
    if target.exists(){if crate::workspace::file_hash(&target)?!=hash{return Err("兼容归档身份不符。".into());}}
    else {
        fs::create_dir_all(target.parent().unwrap()).map_err(|e|e.to_string())?;
        let temporary=target.with_extension(format!("{}.tmp",crate::workspace::new_id()));
        fs::copy(source,&temporary).map_err(|e|e.to_string())?;
        if crate::workspace::file_hash(&temporary)?!=hash {let _=fs::remove_file(temporary);return Err("归档时源文件发生变化。".into());}
        fs::OpenOptions::new().write(true).open(&temporary).and_then(|file|file.sync_all()).map_err(|e|e.to_string())?;
        fs::rename(temporary,target).map_err(|e|e.to_string())?;
    }
    Ok(relative)
}

fn record(workspace: &str, build: &Value, policy: &Value, prior: &Value, origin: &str) -> Value {
    json!({"schema":1,"workspace_id":workspace,"target_build":build,"rules_revision":policy["revision"],
        "rules_digest":digest(policy),"revision":prior["revision"].as_u64().unwrap_or(0).saturating_add(1),
        "checked_at_ms":now(),"origin":origin,"stable":false,"status":"needs_check","tasks":[],
        "fields":{"schema":"兼容状态格式","workspace_id":"所属工作区编号","target_build":"本次核对的程序构建",
            "rules_revision":"规则修订号","rules_digest":"规则内容摘要","revision":"状态单调代次","checked_at_ms":"实际核对时间，毫秒时间戳",
            "origin":"本次核对来源","stable":"核对时是否没有未完成组件事务","status":"当前兼容结论，不代表模型推理健康",
            "tasks":"当前组件与设置的处理事项","resource_id":"组件编号","required":"是否阻塞启动","action":"受支持的处理建议，不是任意执行指令",
            "reason":"中文原因","observation_digest":"本次规则、程序、组件和设置盘点摘要","applied_settings_rules":"安装事务实际执行的设置规则编号",
            "preserved_record":"损坏或未来格式的旧派生记录保留路径；不作为待执行规则",
            "preserved_settings":"默认回退前的原产品设置保留位置","enforcement":"强制升级阶段及恢复进度",
            "settings_sha256":"最近实际校验通过的配置摘要，外部修改后必须重新校验"}})
}

/// prepare_upgrade（准备本体升级）：精确设置与交接记录同事务换入，组件安装更新由本体承担。
pub fn prepare_upgrade(root: &Path, stage: &Path, release: &mut Value, cancel: &AtomicBool, progress: &mut Progress) -> Result<Vec<Value>, String> {
    let policy = rules(&stage.join("app"), false)?;
    let mut rows = Vec::new();
    resources::check_cancel(cancel)?;
    let path = safe_path(root, TRANSACTION_PATHS[0])?;
    let (prior, preserve) = previous(&path)?;
    let portable = resources::load(&root.join("portable.json"))?;
    let mut state = record(portable["workspace_id"].as_str().ok_or("工作区编号缺失。")?, &release["build"], &policy, &prior, "installer");
    if let Some(source)=preserve {
        let relative=format!("data/state/compatibility-preserved-{}.json",crate::workspace::file_hash(&source)?);
        let existing=safe_path(root,&relative)?;
        if existing.exists(){preserve_file(&source,root)?;}else{
            preserve_file(&source,stage)?;
            rows.push(json!({"path":relative,"original":false}));
        }
        state["preserved_record"]=json!(relative);
    }
    let settings_path = safe_path(root, TRANSACTION_PATHS[1])?;
    let original=read(&settings_path).ok();
    let mut settings=original.clone().filter(|value|value.is_object() && value.get("schema_version").is_none_or(|schema|schema.as_u64().is_some_and(|schema|schema>0&&schema<=policy["settings_schema_max"].as_u64().unwrap()))).unwrap_or(json!({}));
    let applied=transform(&mut settings,&policy)?;
    // 先保证资源管理器可读取设置；完整字段修复由当前程序的权威设置入口完成。
    if settings.get("audio_enhancements").is_some_and(|value|!value.is_object() || value.as_object().unwrap().iter().any(|(key,value)|!matches!(key.as_str(),"dual_pass"|"emotion"|"acoustic_events")||!value.is_boolean())) {settings.as_object_mut().unwrap().remove("audio_enhancements");}
    if settings_path.exists() && original.as_ref()!=Some(&settings) {
        let relative=preserve_file(&settings_path,stage)?;
        if !safe_path(root,&relative)?.exists() {rows.push(json!({"path":relative,"original":false}));}
        state["preserved_settings"]=json!(relative);
    }
    atomic_json(&safe_path(stage,TRANSACTION_PATHS[1])?,&settings)?;
    rows.push(json!({"path":TRANSACTION_PATHS[1],"original":settings_path.exists()}));
    state["applied_settings_rules"]=json!(applied);
    state["enforcement"]=json!({"phase":"pending","reset_devices":prior["enforcement"]["phase"]!="complete"&&prior["enforcement"]["reset_devices"]==true,
        "reason":"本体已准备，等待本体配置检查及组件状态交接"});
    atomic_json(&safe_path(stage, TRANSACTION_PATHS[0])?, &state)?;
    rows.push(json!({"path":TRANSACTION_PATHS[0],"original":path.exists()}));
    progress("准备兼容性规则与交接记录", 1, 1);
    Ok(rows)
}

/// invalidate（变更失效）：实际组件换入前撤销旧结论，之后由即时盘点重新确认。
pub fn invalidate(context: &LaunchContext) -> Result<(), String> {
    let _guard = lock()?;
    let path = state_path(context)?;
    // 旧程序/测试夹具没有采用本组件时，不因组件操作强制创建缺少构建身份的记录。
    if !path.exists() { return Ok(()); }
    let (prior, preserve) = previous(&path)?;
    if let Some(source)=preserve {preserve_file(&source,&context.install)?;return Ok(());}
    let mut state = prior.clone();
    state["revision"] = json!(prior["revision"].as_u64().unwrap_or(0).saturating_add(1));
    state["stable"] = json!(false); state["status"] = json!("needs_check");
    state["origin"] = json!("component_change"); state["checked_at_ms"] = json!(now());
    atomic_json(&path, &state)
}

/// inventory（即时盘点）：每次重新读取当前元数据；持久快照不作为启动许可。
pub fn inventory(context: &LaunchContext) -> Result<Vec<Value>, String> {
    inventory_with(context, |_| Ok(()))
}

/// inventory_with（带实际校验结论的盘点）：宿主的已知失败在同一次评估内合并，避免先记录成功再覆盖界面。
pub fn inventory_with(context: &LaunchContext, validate: impl FnOnce(&mut Vec<Value>) -> Result<(), String>) -> Result<Vec<Value>, String> {
    inventory_evaluated(context,validate,false)
}

/// complete_maintenance（最终盘点并提交）：在同一评估锁内生成兼容结论，成功后才标记维护完成。
fn complete_maintenance(context:&LaunchContext)->Result<Vec<Value>,String>{
    inventory_evaluated(context,|_|Ok(()),true)
}

/// inventory_evaluated（统一兼容评估）：completing（提交本轮维护完成）只在最终核对成功后生效。
fn inventory_evaluated(context:&LaunchContext,validate:impl FnOnce(&mut Vec<Value>)->Result<(),String>,completing:bool)->Result<Vec<Value>,String>{
    let _guard = lock()?;
    let path = state_path(context)?;
    let mut rows = resources::inventory_raw(context)?;
    crate::component_maintenance::apply(context,&mut rows)?;
    validate(&mut rows)?;
    let policy_result = rules(&context.program, context.dev);
    let mut program_issue = policy_result.as_ref().err().cloned();
    let policy = policy_result.unwrap_or_else(|_|serde_json::from_str(DEFAULT_RULES).unwrap());
    let release = read(&safe_path(&context.installed_program(), "release.json")?).unwrap_or(Value::Null);
    if release["build"].as_str().is_none_or(|build|build.is_empty()) {program_issue=Some("程序发布清单缺失、损坏或缺少构建身份，请修复程序本体。".into());}
    let (prior, preserve) = previous(&path)?;
    let mut state = record(&context.workspace_id, &release["build"], &policy, &prior, "live_inventory");
    if let Some(source)=preserve {
        state["preserved_record"]=json!(preserve_file(&source,&context.install)?);
    }else{state["preserved_record"]=prior["preserved_record"].clone();}
    let stable = !["tmp/update-transaction.json", "tmp/native-install.json"].iter().any(|name| context.install.join(name).exists());
    let settings = safe_path(&context.data, "config/product-settings.json")?;
    let settings_value = if settings.exists() { read(&settings) } else { Ok(json!({})) };
    let mut settings_issue = match &settings_value {
        Err(_) => Some("产品设置文件无法解析，请修复设置；原文件保持不变。"),
        Ok(value) if !value.is_object() || value.get("schema_version").is_some_and(|v| v.as_u64().is_none_or(|schema| schema == 0 || schema > policy["settings_schema_max"].as_u64().unwrap())) => Some("产品设置格式不受当前程序支持；原文件保持不变。"),
        _ => None,
    };
    let settings_hash=crate::workspace::file_hash(&settings).ok();
    let receipt=read(&safe_path(&context.data,"state/settings-validation.json")?).unwrap_or(Value::Null);
    let settings_validated=receipt["schema"]==1 && receipt["settings_schema"]==policy["settings_schema_max"]
        && settings_hash.is_some() && receipt["sha256"].as_str()==settings_hash.as_deref();
    if (completing && !settings_validated) || (prior["enforcement"]["phase"]=="complete"
        && !(settings_hash.as_deref()==prior["settings_sha256"].as_str() && settings_hash.is_some()) && !settings_validated) {
        settings_issue=Some("设置已在最近一次实际校验后发生变化，需要重新执行兼容修复。");
    }
    state["settings_sha256"]=if settings_issue.is_none(){json!(settings_hash)}else{prior["settings_sha256"].clone()};
    let runtime_components = rows.first().and_then(|row|row["runtime_components"].as_array()).cloned().unwrap_or_default();
    let mut tasks: Vec<Value> = rows.iter().chain(runtime_components.iter()).map(|row| {
        let ready = row["can_start"] == true;
        json!({"resource_id":row["resource_id"],"required":row["required"]!=false,"status":if ready{"compatible"}else{"needs_repair"},
            "action":if ready{"none"}else{"repair_component"},"reason":row["error"].as_str().unwrap_or(if ready{"当前组件元数据匹配"}else{"组件缺失、版本不符或依赖未就绪"})})
    }).collect();
    let settings_complete=settings_issue.is_none()&&settings_validated;
    let program_complete=program_issue.is_none();
    if let Some(reason) = settings_issue {
        tasks.push(json!({"resource_id":"product-settings","required":true,"status":"needs_repair","action":"force_repair","reason":reason}));
        if let Some(server) = rows.first_mut() { server["can_start"] = json!(false); server["error"] = json!(reason); }
    }
    if let Some(reason)=program_issue {
        tasks.push(json!({"resource_id":"program-body","required":true,"status":"needs_repair","action":"repair_program","reason":reason}));
        if let Some(server)=rows.first_mut(){server["can_start"]=json!(false);server["error"]=json!(reason);}
    }
    // 旧安装器明确延期的迁移仍保持延期，不把“组件任务完成”误当“迁移完成”。
    if let Some(deferred) = release["deferred_migrations"].as_object() { for (id, entry) in deferred {
        tasks.push(json!({"resource_id":id,"required":false,"status":"deferred","action":"review_migration","reason":entry["reason"]}));
    }}
    state["stable"] = json!(stable);
    state["status"] = json!(if !stable { "needs_check" } else if tasks.iter().any(|task| task["required"]==true && task["status"]!="compatible") { "needs_repair" } else { "compatible" });
    state["tasks"] = json!(tasks);
    if prior["workspace_id"] == state["workspace_id"] && prior["target_build"] == state["target_build"] && prior["rules_digest"] == state["rules_digest"] {
        state["applied_settings_rules"] = prior["applied_settings_rules"].clone();
        state["preserved_settings"] = prior["preserved_settings"].clone();
        state["enforcement"] = prior["enforcement"].clone();
        if !completing && prior["enforcement"].is_object() && prior["enforcement"]["phase"]!="complete" {
            state["status"]=json!("needs_repair");
            if let Some(server)=rows.first_mut(){server["can_start"]=json!(false);server["error"]=json!(if prior["enforcement"]["phase"]=="settings_pending"{"请在系统组件配置运行依赖，随后完成本次设置维护。"}else{"本体兼容维护尚未完成，请继续本次维护。"});}
        }
    }
    // 本体维护完成与业务组件就绪分别记录；组件问题继续按既有必要性门禁显示。
    if completing && stable && program_complete && settings_complete && prior["enforcement"]["phase"]!="settings_pending"{
        state["enforcement"]=prior["enforcement"].clone();
        state["enforcement"]["phase"]=json!("complete");
        state["enforcement"]["reason"]=Value::Null;
    }
    state["observation_digest"] = json!(digest(&json!({"rows":rows,"settings":settings_value.as_ref().ok().map(digest),"build":release["build"],"rules":policy,"stable":stable})));
    // 相同观测不反复刷盘；返回值的核对时间仍是本次真实读取时间。
    if prior["workspace_id"] != state["workspace_id"] || prior["target_build"] != state["target_build"]
        || prior["observation_digest"] != state["observation_digest"] || prior["stable"] != state["stable"] || prior["status"] != state["status"] || prior["enforcement"]!=state["enforcement"] {
        atomic_json(&path, &state)?;
    } else { state["revision"] = prior["revision"].clone(); }
    if let Some(server) = rows.first_mut() {
        if !stable { server["can_start"] = json!(false); server["error"] = json!("组件事务尚未结束，兼容性状态等待重新核对。"); }
        server["compatibility"] = state;
    }
    Ok(rows)
}

#[cfg(test)]
#[path = "compatibility_tests.rs"]
mod tests;
