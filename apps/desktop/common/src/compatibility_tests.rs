//! 兼容性组件回归：即时性、并发、精确迁移和事务恢复。
use super::*;
use std::path::PathBuf;
use crate::workspace::{self, file_hash, remove_owned};

fn publish_policy(root: &Path, policy: &Value) {
    let path=root.join("app/config/compatibility.json");
    atomic_json(&path,policy).unwrap();
    let mut release=resources::load(&root.join("app/release.json")).unwrap_or(json!({"build":"first"}));
    release["compatibility_engine"]=json!({"schema":1,"rules_file":"config/compatibility.json"});
    release["files"]=json!([{"path":"config/compatibility.json","size":fs::metadata(&path).unwrap().len(),"sha256":file_hash(&path).unwrap()}]);
    atomic_json(&root.join("app/release.json"),&release).unwrap();
}

pub(super) struct Fixture { pub(super) root: PathBuf, pub(super) context: LaunchContext } // Fixture（受管微型工作区）：不复制模型或运行环境。
impl Fixture {
    pub(super) fn new() -> Self {
        let repo = crate::canonical_path(&Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..")).unwrap();
        let root = repo.join(".local/tmp/tasks").join(format!("compatibility-{}", workspace::new_id()));
        fs::create_dir_all(&root).unwrap(); workspace::initialize(&root).unwrap();
        fs::create_dir_all(root.join("app/config/resources")).unwrap();
        fs::create_dir_all(root.join("app/scripts")).unwrap();
        fs::write(root.join("app/config/default.yaml"), b"{}").unwrap();
        fs::write(root.join("app/scripts/backend-entry.py"), b"# fixture").unwrap();
        atomic_json(&root.join("app/config/resources/catalog.json"), &json!({"resources":[]})).unwrap();
        atomic_json(&root.join("app/release.json"), &json!({"build":"first"})).unwrap();
        publish_policy(&root, &serde_json::from_str(DEFAULT_RULES).unwrap());
        let context = LaunchContext::portable(&root).unwrap();
        Self { root, context }
    }
    fn state(&self) -> Value { read(&self.root.join(TRANSACTION_PATHS[0])).unwrap() }
    fn ready_runtime(&self) {
        let lock = self.root.join("app/config/runtime/voice-runtime.lock.json");
        atomic_json(&lock, &json!({"schema":1})).unwrap();
        fs::create_dir_all(self.root.join("runtimes/python")).unwrap();
        fs::write(&self.context.python, b"interpreter-fixture").unwrap();
        atomic_json(&self.root.join("runtimes/python/runtime.json"), &json!({"schema":2,"profile":"cpu-private",
            "lock_sha256":file_hash(&lock).unwrap(),"groups":["voice-core","torch-cpu","onnx-cpu","sherpa-cpu","modelscope-download","onnx-vad"]})).unwrap();
    }
}
impl Drop for Fixture {
    fn drop(&mut self) { if !std::thread::panicking() { remove_owned(&self.root, self.root.parent().unwrap()).unwrap(); } }
}

#[test]
fn current_inventory_supersedes_old_success_and_preserves_optional_semantics() {
    let fixture = Fixture::new();
    fixture.ready_runtime();
    let first = inventory(&fixture.context).unwrap();
    assert_eq!(first[0]["compatibility"]["status"], "compatible");
    assert_eq!(first[0]["can_start"], true);
    let first_revision = fixture.state()["revision"].as_u64().unwrap();
    fs::remove_file(&fixture.context.python).unwrap();
    let missing = inventory(&fixture.context).unwrap();
    assert_eq!(missing[0]["compatibility"]["status"], "needs_repair");
    assert_eq!(missing[0]["can_start"], false);
    assert!(fixture.state()["revision"].as_u64().unwrap() > first_revision);
    fixture.ready_runtime();
    assert_eq!(inventory(&fixture.context).unwrap()[0]["compatibility"]["status"], "compatible");
}

#[test]
fn changed_rules_build_and_settings_are_observed_without_restart() {
    let fixture = Fixture::new(); fixture.ready_runtime(); inventory(&fixture.context).unwrap();
    let prior = fixture.state();
    let mut policy = rules(&fixture.context.program,false).unwrap(); policy["revision"] = json!(3);
    publish_policy(&fixture.root, &policy);
    let mut release=read(&fixture.root.join("app/release.json")).unwrap();release["build"]=json!("second");
    atomic_json(&fixture.root.join("app/release.json"), &release).unwrap();
    let current = inventory(&fixture.context).unwrap();
    assert_eq!(current[0]["compatibility"]["target_build"], "second");
    assert_ne!(current[0]["compatibility"]["rules_digest"], prior["rules_digest"]);
    let settings = json!({"schema_version":999,"unknown_user_preference":"keep"});
    atomic_json(&fixture.root.join(TRANSACTION_PATHS[1]), &settings).unwrap();
    assert_eq!(inventory(&fixture.context).unwrap()[0]["can_start"], false);
    assert_eq!(read(&fixture.root.join(TRANSACTION_PATHS[1])).unwrap(), settings);
}

#[test]
fn active_transaction_never_reuses_a_ready_snapshot() {
    let fixture = Fixture::new(); fixture.ready_runtime(); inventory(&fixture.context).unwrap();
    atomic_json(&fixture.root.join("tmp/native-install.json"), &json!({"target":"runtimes/python","state":"prepared","original":true})).unwrap();
    invalidate(&fixture.context).unwrap();
    assert_eq!(fixture.state()["stable"], false);
    let rows = inventory(&fixture.context).unwrap();
    assert_eq!(rows[0]["compatibility"]["status"], "needs_check");
    assert_eq!(rows[0]["can_start"], false);
    fs::remove_file(fixture.root.join("tmp/native-install.json")).unwrap();
    assert_eq!(inventory(&fixture.context).unwrap()[0]["compatibility"]["status"], "compatible");
}

#[test]
fn parallel_readers_cannot_write_older_generations_over_new_ones() {
    let fixture = Fixture::new(); fixture.ready_runtime();
    std::thread::scope(|scope| {
        for _ in 0..8 { let context = &fixture.context; scope.spawn(move || { inventory(context).unwrap(); }); }
    });
    let current = fixture.state();
    assert_eq!(current["status"], "compatible");
    // 相同观测不反复落盘，也不凭空制造新代次。
    assert_eq!(current["revision"], 1);
}

#[test]
fn unknown_record_is_preserved_without_blocking_repairs_and_unknown_rules_block_start() {
    let fixture = Fixture::new();
    let future = json!({"schema":99,"future_information":"keep"});
    atomic_json(&fixture.root.join(TRANSACTION_PATHS[0]), &future).unwrap();
    inventory(&fixture.context).unwrap();
    let preserved=fixture.state()["preserved_record"].as_str().unwrap().to_owned();
    assert_eq!(read(&fixture.root.join(preserved)).unwrap(), future);
    fixture.ready_runtime();
    assert_eq!(inventory(&fixture.context).unwrap()[0]["can_start"], true);
    publish_policy(&fixture.root, &future);
    assert_eq!(inventory(&fixture.context).unwrap()[0]["compatibility"]["status"], "needs_repair");
    assert_eq!(inventory(&fixture.context).unwrap()[0]["can_start"], false);
}

#[test]
fn known_validation_failure_is_persisted_with_the_actual_inventory() {
    let fixture = Fixture::new(); fixture.ready_runtime();
    let rows = inventory_with(&fixture.context, |rows| {
        rows[0]["can_start"] = json!(false);
        rows[0]["error"] = json!("完整文件摘要复核失败");
        Ok(())
    }).unwrap();
    assert_eq!(rows[0]["compatibility"]["status"], "needs_repair");
    assert!(fixture.state()["tasks"].as_array().unwrap().iter().any(|task| task["reason"] == "完整文件摘要复核失败"));
}

#[test]
fn declared_rules_cannot_silently_fall_back_when_missing_or_unsupported() {
    let fixture = Fixture::new();
    fs::remove_file(fixture.root.join("app/config/compatibility.json")).unwrap();
    assert_eq!(inventory(&fixture.context).unwrap()[0]["compatibility"]["status"], "needs_repair");
    let mut policy: Value = serde_json::from_str(DEFAULT_RULES).unwrap();
    policy["upgrade_handlers"] = json!(["arbitrary_shell"]);
    publish_policy(&fixture.root, &policy);
    assert_eq!(inventory(&fixture.context).unwrap()[0]["compatibility"]["status"], "needs_repair");
}

#[test]
fn exact_setting_rules_are_idempotent_and_preserve_unrelated_fields() {
    let mut policy: Value = serde_json::from_str(DEFAULT_RULES).unwrap();
    policy["settings_rules"] = json!([
        {"id":"rename-old-device","action":"rename_field","min_schema":1,"max_schema":16,"field":"/compute/devices/wespeaker","target":"/compute/devices/campplus","expected":"cpu"},
        {"id":"remove-retired-field","action":"remove_field","min_schema":1,"max_schema":16,"field":"/tts/retired","expected":true},
        {"id":"replace-backend","action":"replace_value","min_schema":1,"max_schema":16,"field":"/tts/backend","expected":"cosyvoice","value":"minimax"}
    ]);
    let mut settings = json!({"schema_version":6,"compute":{"devices":{"wespeaker":"cpu"}},"tts":{"retired":true,"backend":"cosyvoice"},"unknown":{"user_data":"keep"}});
    assert_eq!(transform(&mut settings, &policy).unwrap().len(), 3);
    assert_eq!(settings["compute"]["devices"]["campplus"], "cpu");
    assert!(settings.pointer("/compute/devices/wespeaker").is_none());
    assert_eq!(settings["unknown"]["user_data"], "keep");
    assert!(transform(&mut settings, &policy).unwrap().is_empty());
    assert!(field("/credentials/key").is_err());
    assert!(field("/speaker/../owner").is_err());
    let mut occupied = json!({"compute":{"devices":{"wespeaker":"cpu","campplus":"cuda"}}});
    transform(&mut occupied, &policy).unwrap();
    assert_eq!(occupied["compute"]["devices"]["wespeaker"], "cpu");
    assert_eq!(occupied["compute"]["devices"]["campplus"], "cuda");
    for schema in [json!("invalid"), json!(0), json!(999)] {
        let mut unsupported = json!({"schema_version":schema,"tts":{"backend":"cosyvoice"}});
        assert!(transform(&mut unsupported, &policy).unwrap().is_empty());
        assert_eq!(unsupported["tts"]["backend"], "cosyvoice");
    }
}

#[test]
fn release_identity_and_rule_content_must_match_before_confirming_compatibility() {
    let fixture=Fixture::new();fixture.ready_runtime();
    let mut policy:Value=serde_json::from_str(DEFAULT_RULES).unwrap();policy["settings_schema_max"]=json!(999);
    // 只修改文件，没有新程序清单授权，不能据此接受未来设置。
    atomic_json(&fixture.root.join("app/config/compatibility.json"),&policy).unwrap();
    let rows=inventory(&fixture.context).unwrap();
    assert_eq!(rows[0]["can_start"],false);
    assert_eq!(rows[0]["compatibility"]["status"],"needs_repair");
    fs::remove_file(fixture.root.join("app/release.json")).unwrap();
    let rows=inventory(&fixture.context).unwrap();
    assert_eq!(rows[0]["compatibility"]["status"],"needs_repair");
    assert_eq!(rows[0]["can_start"],false);
}

#[test]
fn damaged_derived_record_does_not_prevent_resource_repair() {
    let fixture=Fixture::new();fixture.ready_runtime();
    let bytes=b"interrupted or damaged compatibility JSON";
    fs::write(fixture.root.join(TRANSACTION_PATHS[0]),bytes).unwrap();
    invalidate(&fixture.context).unwrap();
    assert_eq!(fs::read(fixture.root.join(TRANSACTION_PATHS[0])).unwrap(),bytes);
    let rows=inventory(&fixture.context).unwrap();
    assert_eq!(rows[0]["can_start"],true);
    let preserved=fixture.state()["preserved_record"].as_str().unwrap().to_owned();
    assert_eq!(fs::read(fixture.root.join(preserved)).unwrap(),bytes);
}

#[test]
fn shipped_settings_compatibility_tracks_the_product_schema() {
    let source = include_str!("../../../../voice_terminal/server/product.py");
    let schema: u64 = source.split("PRODUCT_SETTINGS_SCHEMA_VERSION = ").nth(1).unwrap().lines().next().unwrap().trim().parse().unwrap();
    let policy: Value = serde_json::from_str(DEFAULT_RULES).unwrap();
    assert_eq!(policy["settings_schema_max"], schema);
}

#[test]
fn compatibility_record_and_settings_restore_with_program_transaction() {
    let fixture = Fixture::new();
    let original = json!({"schema_version":6,"tts":{"backend":"cosyvoice"},"unknown":"keep"});
    atomic_json(&fixture.root.join(TRANSACTION_PATHS[1]), &original).unwrap();
    let stage = fixture.root.join("tmp/publish/new");
    fs::create_dir_all(stage.join("app/config")).unwrap();
    let mut policy: Value = serde_json::from_str(DEFAULT_RULES).unwrap();
    policy["upgrade_handlers"] = json!([]);
    policy["settings_rules"] = json!([{"id":"backend","action":"replace_value","min_schema":1,"max_schema":16,"field":"/tts/backend","expected":"cosyvoice","value":"minimax"}]);
    publish_policy(&stage, &policy);
    let mut release = json!({"build":"new"});
    let rows = prepare_upgrade(&fixture.root, &stage, &mut release, &AtomicBool::new(false), &mut |_,_,_|{}).unwrap();
    assert_eq!(rows.len(), 3); // 原配置归档、迁移候选和兼容记录同事务提交。
    assert_eq!(read(&stage.join(TRANSACTION_PATHS[0])).unwrap()["status"], "needs_check");
    assert_eq!(read(&stage.join(TRANSACTION_PATHS[1])).unwrap()["tts"]["backend"], "minimax");
    let old = fixture.root.join("tmp/publish/old");
    for row in &rows {
        let relative = row["path"].as_str().unwrap();
        let target = fixture.root.join(relative);
        if target.exists() { fs::create_dir_all(old.join(relative).parent().unwrap()).unwrap(); fs::rename(&target, old.join(relative)).unwrap(); }
        fs::create_dir_all(target.parent().unwrap()).unwrap(); fs::rename(stage.join(relative), target).unwrap();
    }
    atomic_json(&fixture.root.join("tmp/update-transaction.json"), &json!({"state":"prepared","items":rows})).unwrap();
    workspace::recover_install(&fixture.root).unwrap();
    assert_eq!(read(&fixture.root.join(TRANSACTION_PATHS[1])).unwrap(), original);
    assert!(!fixture.root.join(TRANSACTION_PATHS[0]).exists());
}

#[test]
fn future_record_preservation_rolls_back_with_the_installation() {
    let fixture=Fixture::new();
    let original=json!({"schema":99,"future_information":"keep"});
    atomic_json(&fixture.root.join(TRANSACTION_PATHS[0]),&original).unwrap();
    let stage=fixture.root.join("tmp/publish/new");
    let mut policy:Value=serde_json::from_str(DEFAULT_RULES).unwrap();policy["upgrade_handlers"]=json!([]);
    publish_policy(&stage,&policy);
    let mut release=read(&stage.join("app/release.json")).unwrap();
    let rows=prepare_upgrade(&fixture.root,&stage,&mut release,&AtomicBool::new(false),&mut |_,_,_|{}).unwrap();
    assert_eq!(rows.len(),3);
    let old=fixture.root.join("tmp/publish/old");
    for row in &rows {
        let relative=row["path"].as_str().unwrap();let target=fixture.root.join(relative);
        if target.exists(){fs::create_dir_all(old.join(relative).parent().unwrap()).unwrap();fs::rename(&target,old.join(relative)).unwrap();}
        fs::create_dir_all(target.parent().unwrap()).unwrap();fs::rename(stage.join(relative),target).unwrap();
    }
    atomic_json(&fixture.root.join("tmp/update-transaction.json"),&json!({"state":"prepared","items":rows})).unwrap();
    workspace::recover_install(&fixture.root).unwrap();
    assert_eq!(fixture.state(),original);
    assert_eq!(fs::read_dir(fixture.root.join("data/state")).unwrap().filter_map(Result::ok).filter(|entry|entry.file_name().to_string_lossy().starts_with("compatibility-preserved-")).count(),0);
    assert!(!is_transaction_path("data/state/compatibility-preserved-../settings.json"));
}

#[test]
fn invalid_settings_cannot_prevent_preparing_the_forced_update() {
    for bytes in [b"damaged JSON".as_slice(),br#"{"schema_version":999,"private_preference":"keep"}"#] {
        let fixture=Fixture::new();
        let source=fixture.root.join(TRANSACTION_PATHS[1]);fs::write(&source,bytes).unwrap();
        let stage=fixture.root.join("tmp/publish/new");
        let mut policy:Value=serde_json::from_str(DEFAULT_RULES).unwrap();policy["upgrade_handlers"]=json!([]);
        publish_policy(&stage,&policy);
        let mut release=read(&stage.join("app/release.json")).unwrap();
        let rows=prepare_upgrade(&fixture.root,&stage,&mut release,&AtomicBool::new(false),&mut|_,_,_|{}).unwrap();
        assert_eq!(read(&stage.join(TRANSACTION_PATHS[1])).unwrap(),json!({}));
        assert_eq!(fs::read(&source).unwrap(),bytes); // 本体事务提交前不动原件。
        let state=read(&stage.join(TRANSACTION_PATHS[0])).unwrap();
        assert_eq!(state["enforcement"]["phase"],"pending");
        assert_eq!(fs::read(stage.join(state["preserved_settings"].as_str().unwrap())).unwrap(),bytes);
        assert_eq!(rows.len(),3);
    }
}

#[test]
fn oversized_derived_record_is_stream_preserved_and_rebuilt() {
    let fixture=Fixture::new();fixture.ready_runtime();
    let source=fixture.root.join(TRANSACTION_PATHS[0]);
    fs::write(&source,vec![b'x';4*1024*1024+1]).unwrap();
    invalidate(&fixture.context).unwrap();
    let rows=inventory(&fixture.context).unwrap();
    assert_eq!(rows[0]["can_start"],true);
    let preserved=fixture.state()["preserved_record"].as_str().unwrap().to_owned();
    assert_eq!(fs::metadata(fixture.root.join(preserved)).unwrap().len(),4*1024*1024+1);
}

#[test]
fn configuration_changed_outside_validated_save_cannot_inherit_upgrade_success() {
    let fixture=Fixture::new();fixture.ready_runtime();
    let settings=fixture.root.join(TRANSACTION_PATHS[1]);
    atomic_json(&settings,&json!({"schema_version":16,"wake":{"enabled":true}})).unwrap();
    inventory(&fixture.context).unwrap();
    let mut state=fixture.state();state["enforcement"]=json!({"phase":"complete"});
    atomic_json(&fixture.root.join(TRANSACTION_PATHS[0]),&state).unwrap();
    assert_eq!(inventory(&fixture.context).unwrap()[0]["can_start"],true);
    atomic_json(&settings,&json!({"schema_version":16,"wake":{"enabled":"invalid"}})).unwrap();
    for _ in 0..2 {assert_eq!(inventory(&fixture.context).unwrap()[0]["can_start"],false);}
    atomic_json(&settings,&json!({"schema_version":16,"wake":{"enabled":false}})).unwrap();
    atomic_json(&fixture.root.join("data/state/settings-validation.json"),&json!({"schema":1,"settings_schema":16,"sha256":file_hash(&settings).unwrap()})).unwrap();
    assert_eq!(inventory(&fixture.context).unwrap()[0]["can_start"],true);
}

#[test]
fn restarting_installer_preserves_unfinished_device_fallback() {
    let fixture=Fixture::new();
    atomic_json(&fixture.root.join(TRANSACTION_PATHS[0]),&json!({"schema":1,"enforcement":{"phase":"repair_pending","reset_devices":true}})).unwrap();
    let stage=fixture.root.join("tmp/publish/new");
    let mut policy:Value=serde_json::from_str(DEFAULT_RULES).unwrap();policy["upgrade_handlers"]=json!([]);publish_policy(&stage,&policy);
    let mut release=read(&stage.join("app/release.json")).unwrap();
    prepare_upgrade(&fixture.root,&stage,&mut release,&AtomicBool::new(false),&mut|_,_,_|{}).unwrap();
    assert_eq!(read(&stage.join(TRANSACTION_PATHS[0])).unwrap()["enforcement"]["reset_devices"],true);
}
