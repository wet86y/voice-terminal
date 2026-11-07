//! Component reset（组件重置）：明确目录白名单、持久异常及可续做删除。
use crate::{
    component_recovery::{self, Event},
    resources,
    workspace::{atomic_json, remove_owned, safe_path},
    LaunchContext,
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{fs, sync::Mutex};
static LOCK: Mutex<()> = Mutex::new(()); // LOCK（重置记录写入互斥）。
fn path(c: &LaunchContext) -> Result<std::path::PathBuf, String> {
    safe_path(&c.data, "state/component-reset.json")
}
fn load(c: &LaunchContext) -> Result<Value, String> {
    let p = path(c)?;
    if !p.exists() {
        return Ok(
            json!({"schema":1,"workspace_id":c.workspace_id,"faults":{},"pending":null,"fields":{"schema":"重置记录格式","workspace_id":"所属工作区编号","faults":"未解决的组件维护异常","pending":"用户已确认、尚待完成的重置范围"}}),
        );
    }
    let v = resources::load(&p)?;
    if v["schema"] != 1 || v["workspace_id"] != c.workspace_id || !v["faults"].is_object() {
        return Err("组件重置记录格式或工作区身份无效。".into());
    }
    Ok(v)
}
fn ids(id: &str) -> Vec<String> {
    if matches!(
        id,
        "voice-server" | "voice-runtime-cpu" | "voice-runtime-gpu"
    ) {
        vec![
            "voice-server".into(),
            "voice-runtime-cpu".into(),
            "voice-runtime-gpu".into(),
        ]
    } else {
        vec![id.into()]
    }
}
fn targets(c: &LaunchContext, id: &str) -> Result<Vec<String>, String> {
    if id == "tts-aniflive-runtime" {
        // 隔离依赖目录与模型、输入缓存、音色资料分开重置。
        resources::spec(c, id)?;
        return Ok(vec!["runtimes/aniflive-tts".into()]);
    }
    if matches!(
        id,
        "voice-server" | "voice-runtime-cpu" | "voice-runtime-gpu"
    ) {
        return Ok(vec![
            "runtimes/python".into(),
            "cache/dependencies".into(),
            "tmp/voice-environment".into(),
            "tmp/voice-import".into(),
        ]);
    }
    let item = resources::spec(c, id)?;
    let target = resources::destination(c, &item)?;
    if (id == "dsh" && target != c.install.join("runtimes/dsh"))
        || (item["kind"] == "model"
            && (!target.starts_with(c.install.join("models"))
                || target == c.install.join("models")))
    {
        return Err("重置目标不是独立受管组件目录。".into());
    }
    for other in resources::specs(c)?
        .iter()
        .filter(|other| other["resource_id"] != id)
    {
        let other = resources::destination(c, other)?;
        if other.starts_with(&target) {
            return Err("重置目标覆盖其他组件目录，拒绝扩大删除范围。".into());
        }
    }

    let mut result = vec![
        target
            .strip_prefix(&c.install)
            .map_err(|_| "组件路径越界")?
            .to_string_lossy()
            .replace('\\', "/"),
        format!("tmp/resource-downloads/{id}"),
    ];
    if id == "dsh" {
        result.extend(
            [
                "cache/npm",
                "tmp/dsh-npm",
                "cache/component-validation/dsh.json",
            ]
            .map(str::to_owned),
        );
    }
    Ok(result)
}
pub fn fault(c: &LaunchContext, id: &str, reason: &str) -> Result<(), String> {
    if !matches!(
        component_recovery::classify(reason),
        Event::Content | Event::Consistency | Event::Unknown
    ) {
        return Ok(());
    }
    if targets(c, id).is_err() {
        return Ok(());
    }
    let _g = LOCK.lock().map_err(|_| "重置记录锁不可用")?;
    let mut v = load(c)?;
    for id in ids(id) {
        v["faults"][id] = json!(reason);
    }
    atomic_json(&path(c)?, &v)
}
/// transaction_fault（记录可确认归属的旧事务异常）：不执行事务中的任意路径。
pub fn transaction_fault(c: &LaunchContext, reason: &str) -> Result<(), String> {
    let record = resources::load(&safe_path(&c.install, "tmp/native-install.json")?)?;
    let Some(target) = record["target"].as_str() else {
        return Ok(());
    };
    let owner = if matches!(target, "runtimes/python" | "runtime-file-update") {
        Some("voice-runtime-cpu".to_owned())
    } else if target == "runtimes/dsh" {
        Some("dsh".to_owned())
    } else {
        resources::specs(c)?.iter().find_map(|r| {
            resources::destination(c, r)
                .ok()
                .and_then(|p| {
                    p.strip_prefix(&c.install)
                        .ok()
                        .map(|v| v.to_string_lossy().replace('\\', "/") == target)
                })
                .filter(|same| *same)
                .and_then(|_| r["resource_id"].as_str().map(str::to_owned))
        })
    };
    if let Some(id) = owner {
        fault(c, &id, reason)?;
    }
    Ok(())
}
pub fn clear(c: &LaunchContext, resources: &[String]) -> Result<(), String> {
    let _g = LOCK.lock().map_err(|_| "重置记录锁不可用")?;
    let mut v = load(c)?;
    for id in resources {
        v["faults"].as_object_mut().unwrap().remove(id);
    }
    atomic_json(&path(c)?, &v)
}
pub fn apply(c: &LaunchContext, rows: &mut [Value]) -> Result<(), String> {
    let v = load(c)?;
    fn project(v: &Value, rows: &mut [Value]) {
        for row in rows {
            let reason = &v["faults"][row["resource_id"].as_str().unwrap_or("")];
            row["reset_available"] = json!(reason.is_string());
            row["reset_reason"] = reason.clone();
            if let Some(children) = row["runtime_components"].as_array_mut() {
                project(v, children);
            }
        }
    }
    project(&v, rows);
    Ok(())
}
pub fn plan(c: &LaunchContext, id: &str) -> Result<Value, String> {
    let v = load(c)?;
    if !v["faults"][id].is_string() {
        return Err("当前组件没有待重置的维护异常。".into());
    }
    let paths = targets(c, id)?;
    for relative in &paths {
        safe_path(&c.install, relative)?;
    }
    let journal = c.install.join("tmp/native-install.json");
    let transaction = if journal.exists() {
        crate::workspace::file_hash(&journal)?
    } else {
        String::new()
    };
    let release = resources::load(&c.installed_program().join("release.json"))?;
    let token = format!(
        "{:x}",
        Sha256::digest(
            json!([
                c.workspace_id,
                release["build"],
                id,
                paths,
                transaction,
                v["faults"][id]
            ])
            .to_string()
            .as_bytes()
        )
    );
    Ok(
        json!({"resource_id":id,"resource_ids":ids(id),"paths":paths,"token":token,"reason":v["faults"][id],"message":if ids(id).len()>1{"将删除整个 CPU（中央处理器）和 GPU（图形处理器）共享环境及下载缓存；模型与用户资料保留。"}else{"将删除此组件及对应下载缓存；其他组件和用户资料保留。"},"fields":{"resource_id":"请求的组件编号","resource_ids":"实际重置的组件组","paths":"工作区内受管删除目录","token":"本次范围确认身份","reason":"维护异常原因","message":"删除影响中文说明"}}),
    )
}
pub fn pending(c: &LaunchContext) -> Result<Option<Value>, String> {
    let v = load(c)?;
    Ok(v["pending"].is_object().then(|| v["pending"].clone()))
}
pub fn execute(
    c: &LaunchContext,
    id: &str,
    token: &str,
    progress: &mut resources::Progress,
) -> Result<Vec<String>, String> {
    let fresh = plan(c, id)?;
    let existing = pending(c)?;
    if existing.is_none() && fresh["token"] != token {
        return Err("组件范围已经变化，请重新确认重置。".into());
    }
    if let Some(p) = &existing {
        if p["paths"] != fresh["paths"] {
            return Err("受管目录已变化，请先处理上次重置记录并重新确认。".into());
        }
        if p["resource_id"] != id {
            return Err("另一个组件的重置尚未完成。".into());
        }
    }

    {
        let _g = LOCK.lock().map_err(|_| "重置记录锁不可用")?;
        let mut v = load(c)?;
        v["pending"] = fresh.clone();
        atomic_json(&path(c)?, &v)?;
    }
    // 已确认意图先落盘，再处理可能改变组件内容的旧事务。
    if let Some(fault) = resources::recover_for_integrity(c)? {
        if !fault
            .resource_id
            .as_deref()
            .is_some_and(|owner| ids(id).contains(&owner.to_owned()))
        {
            return Err("无法确认旧事务属于本次组件，请先校验完整性。".into());
        }
        let journal = resources::load(&c.install.join("tmp/native-install.json"))?;
        if journal["companions"]
            .as_array()
            .into_iter()
            .flatten()
            .any(|r| {
                !matches!(
                    r["path"].as_str(),
                    Some("models/manifest.json" | "app/release.json")
                )
            })
        {
            return Err("旧事务包含需保护的用户资料，请先校验完整性。".into());
        }
        resources::archive_failed_activation_for(c, &fault, "reset")?;
    }
    if ids(id).len() > 1 {
        let state = safe_path(&c.data, "state/compatibility.json")?;
        let mut v = resources::load(&state)?;
        v["enforcement"] = json!({"phase":"settings_pending","reset_devices":true,"reason":"用户重置共享运行环境，重新安装后完成设备设置维护。"});
        atomic_json(&state, &v)?;
    }
    for relative in fresh["paths"].as_array().unwrap() {
        let relative = relative.as_str().unwrap();
        progress(&format!("正在重置：{relative}（受管路径）"), 0, 0);
        remove_owned(&safe_path(&c.install, relative)?, &c.install)?;
    }
    progress("更新组件登记", 0, 0);
    let removed = ids(id);
    let release_path = c.installed_program().join("release.json");
    let mut release = resources::load(&release_path)?;
    let kind = if removed.len() > 1 {
        "python"
    } else if id == "dsh" {
        "dsh"
    } else {
        ""
    };
    if let Some(rows) = release["components"].as_array_mut() {
        rows.retain(|r| r["kind"] != kind);
    }
    atomic_json(&release_path, &release)?;
    if kind.is_empty() && id != "tts-aniflive-runtime" {
        let item = resources::spec(c, id)?;
        let prefix = format!("{}/", item["destination"].as_str().ok_or("模型目录缺失")?);
        let p = c.install.join("models/manifest.json");
        let mut m = match resources::load(&p) {
            Ok(v) if v["files"].is_array() => v,
            _ => {
                if p.exists() {
                    let evidence = safe_path(
                        &c.install,
                        &format!(
                            "tmp/component-recovery/reset-manifest-{}.json",
                            crate::workspace::file_hash(&p)?
                        ),
                    )?;
                    if !evidence.exists() {
                        fs::create_dir_all(evidence.parent().unwrap())
                            .map_err(|e| e.to_string())?;
                        fs::copy(&p, evidence).map_err(|e| e.to_string())?;
                    }
                }
                let mut files = vec![];
                for other in resources::specs(c)?
                    .iter()
                    .filter(|r| r["kind"] == "model" && r["resource_id"] != id)
                {
                    let root = resources::destination(c, other)?;
                    if let Ok((owner, _, rows)) = resources::read_model_receipt(&root) {
                        if other["resource_id"] != owner {
                            continue;
                        }
                        for row in rows {
                            let Some(name) = row["path"].as_str() else {
                                continue;
                            };
                            let file = safe_path(&root, name)?;
                            if file
                                .metadata()
                                .is_ok_and(|m| Some(m.len()) == row["size"].as_u64())
                            {
                                files.push(json!({"path":format!("{}/{}",other["destination"].as_str().unwrap_or(""),name),"size":row["size"],"sha256":row["sha256"]}));
                            }
                        }
                    }
                }
                json!({"schema":1,"kind":"models","build":crate::workspace::new_id(),"files":files})
            }
        };
        m["files"]
            .as_array_mut()
            .ok_or("模型总清单无效，请校验完整性")?
            .retain(|r| !r["path"].as_str().unwrap_or("").starts_with(&prefix));
        atomic_json(&p, &m)?;
        let mut binding = c.clone();
        binding.program = c.installed_program();
        resources::bind_component(&binding, "models", "models", &c.install.join("models"))?;
    }
    if id == "dsh" {
        let p = safe_path(&c.data, "state/native-dsh-binding.json")?;
        if p.exists() {
            fs::remove_file(p).map_err(|e| e.to_string())?;
        }
    }
    if removed.len() > 1 {
        let p = safe_path(&c.data, "config/gpu-environment.json")?;
        if p.exists() {
            fs::remove_file(p).map_err(|e| e.to_string())?;
        }
    }
    crate::component_maintenance::resolved(c, &removed)?;
    {
        let _g = LOCK.lock().map_err(|_| "重置记录锁不可用")?;
        let mut v = load(c)?;
        for id in &removed {
            v["faults"].as_object_mut().unwrap().remove(id);
        }
        v["pending"] = Value::Null;
        atomic_json(&path(c)?, &v)?;
    }
    Ok(removed)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn aniflive_runtime_reset_scope_preserves_models_inputs_and_recordings() {
        let (root,c)=fixture();
        let catalog=root.join("app/config/resources/catalog.json");
        fs::create_dir_all(catalog.parent().unwrap()).unwrap();
        atomic_json(&catalog,&json!({"resources":[{"resource_id":"tts-aniflive-runtime","kind":"model","destination":"aniflive-tts/runtime-registration","preparation":"aniflive_runtime"}]})).unwrap();
        // 资源目录实际格式由规范读取；此处直接核对固定特殊范围。
        let result=targets(&c,"tts-aniflive-runtime");
        assert!(result.is_ok(),"{result:?}");
        assert_eq!(result.unwrap(),vec!["runtimes/aniflive-tts"]);
        crate::workspace::remove_owned(&root,root.parent().unwrap()).unwrap();
    }
    pub(super) fn fixture() -> (std::path::PathBuf, LaunchContext) {
        let owner = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../../.local/tmp/component-reset-tests");
        fs::create_dir_all(&owner).unwrap();
        let root = crate::canonical_path(&owner)
            .unwrap()
            .join(crate::workspace::new_id());
        crate::workspace::initialize(&root).unwrap();
        fs::create_dir_all(root.join("app/config")).unwrap();
        fs::create_dir_all(root.join("app/scripts")).unwrap();
        fs::write(root.join("app/config/default.yaml"), "{}").unwrap();
        fs::write(root.join("app/scripts/backend-entry.py"), "# fixture").unwrap();
        let c = LaunchContext::portable(&root).unwrap();
        atomic_json(
            &root.join("app/release.json"),
            &json!({"build":"test","components":[]}),
        )
        .unwrap();
        atomic_json(
            &c.data.join("state/compatibility.json"),
            &json!({"schema":1}),
        )
        .unwrap();
        (root, c)
    }
    #[test]
    fn reset_runtime_preserves_models_and_user_data_and_rejects_stale_plan() {
        let (root, c) = fixture();
        for name in [
            "runtimes/python/python.exe",
            "cache/dependencies/wheels/bad.whl",
            "models/keep/model.bin",
            "data/history/keep.txt",
        ] {
            let p = root.join(name);
            fs::create_dir_all(p.parent().unwrap()).unwrap();
            fs::write(p, b"keep").unwrap();
        }
        fault(&c, "voice-runtime-gpu", "TypeError: unknown").unwrap();
        let p = plan(&c, "voice-runtime-gpu").unwrap();
        assert!(execute(&c, "voice-runtime-gpu", "stale", &mut |_, _, _| {}).is_err());
        assert!(root.join("runtimes/python/python.exe").exists());
        let removed = execute(
            &c,
            "voice-runtime-gpu",
            p["token"].as_str().unwrap(),
            &mut |_, _, _| {},
        )
        .unwrap();
        assert_eq!(removed.len(), 3);
        assert!(!root.join("runtimes/python").exists());
        assert!(!root.join("cache/dependencies").exists());
        assert_eq!(
            fs::read(root.join("models/keep/model.bin")).unwrap(),
            b"keep"
        );
        assert_eq!(
            fs::read(root.join("data/history/keep.txt")).unwrap(),
            b"keep"
        );
        assert!(pending(&c).unwrap().is_none());
        assert!(plan(&c, "voice-runtime-gpu").is_err());
        crate::workspace::remove_owned(&root, root.parent().unwrap()).unwrap();
    }
    #[test]
    fn routine_failures_do_not_offer_reset() {
        let (root, c) = fixture();
        for reason in [
            "下载失败 HTTP 503",
            "拒绝访问 os error 5",
            "空间不足",
            "文件被占用 os error 32",
        ] {
            fault(&c, "voice-runtime-cpu", reason).unwrap();
            assert!(plan(&c, "voice-runtime-cpu").is_err());
        }
        crate::workspace::remove_owned(&root, root.parent().unwrap()).unwrap();
    }
}

#[cfg(test)]
mod reset_scenarios {
    use super::*;
    fn setup() -> (std::path::PathBuf, LaunchContext) {
        let (root, c) = super::tests::fixture();
        fs::create_dir_all(root.join("app/config/resources")).unwrap();
        atomic_json(&root.join("app/config/resources/catalog.json"),&json!({"resources":[
   {"resource_id":"model-a","kind":"model","destination":"test/a","files":[]},
   {"resource_id":"model-b","kind":"model","destination":"test/b","files":[]},
   {"resource_id":"tts-aniflive-runtime","kind":"model","destination":"aniflive-tts/runtime-registration","preparation":"aniflive_runtime","files":[]},
   {"resource_id":"dsh","kind":"dsh","destination":"runtimes/dsh"}
  ]})).unwrap();
        fs::create_dir_all(root.join("models")).unwrap();
        atomic_json(&root.join("models/manifest.json"),&json!({"schema":1,"kind":"models","build":"fixture","files":[{"path":"test/a/model.bin","size":4,"sha256":"00"},{"path":"test/b/model.bin","size":4,"sha256":"11"}]})).unwrap();
        (root, c)
    }
    fn put(root: &std::path::Path, name: &str) {
        let p = root.join(name);
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        fs::write(p, b"keep").unwrap();
    }
    fn reset(c: &LaunchContext, id: &str) -> Result<Vec<String>, String> {
        fault(c, id, "TypeError: internal failure")?;
        let p = plan(c, id)?;
        execute(c, id, p["token"].as_str().unwrap(), &mut |_, _, _| {})
    }
    #[test]
    fn model_reset_preserves_other_files_and_removes_only_its_manifest_prefix() {
        let (root, c) = setup();
        for p in [
            "models/test/a/model.bin",
            "models/test/b/model.bin",
            "tmp/resource-downloads/model-a/bad.part",
            "data/tts/voice.wav",
        ] {
            put(&root, p);
        }
        reset(&c, "model-a").unwrap();
        assert!(!root.join("models/test/a").exists());
        assert!(root.join("models/test/b/model.bin").exists());
        assert!(root.join("data/tts/voice.wav").exists());
        assert_eq!(
            resources::load(&root.join("models/manifest.json")).unwrap()["files"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        remove_owned(&root, root.parent().unwrap()).unwrap();
    }
    #[test]
    fn dsh_reset_keeps_user_runtime_cache() {
        let (root, c) = setup();
        for p in [
            "runtimes/dsh/node.exe",
            "cache/npm/package",
            "data/dsh/cache/npm/user",
            "data/dsh/pnpm/store/user",
        ] {
            put(&root, p);
        }
        reset(&c, "dsh").unwrap();
        assert!(!root.join("runtimes/dsh").exists());
        assert!(!root.join("cache/npm").exists());
        assert!(root.join("data/dsh/cache/npm/user").exists());
        assert!(root.join("data/dsh/pnpm/store/user").exists());
        remove_owned(&root, root.parent().unwrap()).unwrap();
    }
    #[test]
    fn aniflive_reset_keeps_legacy_runtime_weights_and_recordings() {
        let (root, c) = setup();
        for p in [
            "models/audiocpp/runtime/cuda/audiocpp_server.exe",
            "runtimes/audiocpp/cuda/audiocpp_server.exe",
            "models/audiocpp/tts/model.gguf",
            "runtimes/aniflive-tts/packages/model.dll",
            "data/tts/aniflive/voices/assets/user.wav",
        ] {
            put(&root, p);
        }
        assert!(reset(&c, "tts-audiocpp-runtime").is_err());
        reset(&c, "tts-aniflive-runtime").unwrap();
        assert!(!root.join("runtimes/aniflive-tts").exists());
        assert!(root.join("runtimes/audiocpp/cuda/audiocpp_server.exe").exists());
        assert!(root.join("models/audiocpp/runtime/cuda/audiocpp_server.exe").exists());
        assert!(root.join("data/tts/aniflive/voices/assets/user.wav").exists());
        assert!(root.join("models/audiocpp/tts/model.gguf").exists());
        remove_owned(&root, root.parent().unwrap()).unwrap();
    }
    #[test]
    fn partial_failure_keeps_confirmed_intent_and_can_resume() {
        let (root, c) = setup();
        put(&root, "runtimes/python/python.exe");
        fs::remove_file(c.data.join("state/compatibility.json")).unwrap();
        assert!(reset(&c, "voice-runtime-cpu").is_err());
        assert!(pending(&c).unwrap().is_some());
        assert!(root.join("runtimes/python/python.exe").exists());
        atomic_json(
            &c.data.join("state/compatibility.json"),
            &json!({"schema":1}),
        )
        .unwrap();
        let p = pending(&c).unwrap().unwrap();
        execute(
            &c,
            "voice-runtime-cpu",
            p["token"].as_str().unwrap(),
            &mut |_, _, _| {},
        )
        .unwrap();
        assert!(!root.join("runtimes/python").exists());
        remove_owned(&root, root.parent().unwrap()).unwrap();
    }
    #[test]
    fn unknown_transaction_never_deletes_component_or_user_data() {
        let (root, c) = setup();
        put(&root, "runtimes/python/python.exe");
        put(&root, "data/history/user");
        atomic_json(
            &root.join("tmp/native-install.json"),
            &json!({"target":"data/history"}),
        )
        .unwrap();
        assert!(reset(&c, "voice-runtime-cpu")
            .unwrap_err()
            .contains("无法确认"));
        assert!(root.join("runtimes/python/python.exe").exists());
        assert!(root.join("data/history/user").exists());
        assert!(root.join("tmp/native-install.json").exists());
        remove_owned(&root, root.parent().unwrap()).unwrap();
    }
    #[test]
    fn known_broken_transaction_is_preserved_as_reset_evidence() {
        let (root, c) = setup();
        put(&root, "runtimes/python/python.exe");
        atomic_json(
            &root.join("tmp/native-install.json"),
            &json!({"target":"runtime-file-update","delta":null,"companions":[]}),
        )
        .unwrap();
        reset(&c, "voice-runtime-cpu").unwrap();
        let records = fs::read_dir(root.join("tmp/component-recovery"))
            .unwrap()
            .map(|r| r.unwrap().path())
            .collect::<Vec<_>>();
        assert_eq!(records.len(), 1);
        let evidence = resources::load(&records[0].join("evidence.json")).unwrap();
        assert_eq!(evidence["operation"], "reset");
        assert!(evidence["scan_scope"].is_null());
        assert!(records[0].join("native-install.json").exists());
        remove_owned(&root, root.parent().unwrap()).unwrap();
    }
}
