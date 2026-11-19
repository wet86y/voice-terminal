//! 工作区清理只盘点程序拥有、可以重新生成或已经退役的路径。
use crate::{app_update, resources, workspace, LaunchContext};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::ErrorKind,
    path::Path,
    time::{Duration, SystemTime},
};

const WEEK: Duration = Duration::from_secs(7 * 24 * 3600); // WEEK（失败下载续传保留时间）。
const DAY: Duration = Duration::from_secs(24 * 3600); // DAY（近期写入保护时间）。
const GIB: u64 = 1024 * 1024 * 1024; // GIB（二进制吉字节）。
const RETIRED_DOWNLOADS: &[&str] = &["tts-voxcpm1", "tts-voxcpm2", "tts-audiocpp-runtime"];

struct Entry {
    relative: String,     // relative（工作区相对路径）。
    reason: &'static str, // reason（可回收原因）。
    size: u64,            // size（文件逻辑字节数）。
    newest: SystemTime,   // newest（最近写入时间）。
    identity: [u8; 32],   // identity（目录项和文件属性摘要，确认时重新核对）。
}

fn measure(root: &Path, relative: &str, reason: &'static str) -> Result<Option<Entry>, String> {
    fn walk(
        path: &Path,
        relative: &str,
        entry: &mut Entry,
        hash: &mut Sha256,
    ) -> Result<(), String> {
        let metadata = fs::symlink_metadata(path).map_err(|e| e.to_string())?;
        if workspace::metadata_reparse(&metadata) || (!metadata.is_dir() && !metadata.is_file()) {
            return Err("包含链接或特殊文件".into());
        }
        let modified = metadata.modified().map_err(|e| e.to_string())?;
        entry.newest = entry.newest.max(modified);
        hash.update((relative.len() as u64).to_le_bytes());
        hash.update(relative.as_bytes());
        hash.update([u8::from(metadata.is_dir())]);
        hash.update(metadata.len().to_le_bytes());
        hash.update(format!("{modified:?}").as_bytes());
        if metadata.is_file() {
            if relative.starts_with("cache/python/") && !relative.ends_with(".pyc") {
                return Err("字节码缓存中含有其他文件".into());
            }
            entry.size = entry.size.saturating_add(metadata.len());
        } else {
            let mut children = fs::read_dir(path)
                .and_then(|entries| entries.collect::<Result<Vec<_>, _>>())
                .map_err(|e| e.to_string())?;
            children.sort_by_key(|child| child.file_name());
            for child in children {
                let name = child.file_name();
                let name = name.to_str().ok_or("目录项名称无法识别")?;
                walk(&child.path(), &format!("{relative}/{name}"), entry, hash)?;
            }
        }
        Ok(())
    }

    let path = workspace::safe_path(root, relative)?;
    match fs::symlink_metadata(&path) {
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.to_string()),
        Ok(_) => {}
    }
    let mut entry = Entry {
        relative: relative.into(),
        reason,
        size: 0,
        newest: SystemTime::UNIX_EPOCH,
        identity: [0; 32],
    };
    let mut hash = Sha256::new();
    walk(&path, relative, &mut entry, &mut hash)?;
    entry.identity = hash.finalize().into();
    Ok(Some(entry))
}

fn remove_entry(root: &Path, entry: &Entry) -> Result<(), String> {
    let current = measure(root, &entry.relative, entry.reason)?.ok_or("文件已变化")?;
    if current.identity != entry.identity {
        return Err("文件已变化，请重新检查".into());
    }
    workspace::remove_owned(&workspace::safe_path(root, &entry.relative)?, root)
}

/// 回收已提交事务使用的专用下载目录，回收失败由下次清理处理。
pub(crate) fn reclaim_cache(root: &Path, relative: &str) {
    let result = measure(root, relative, "已完成任务的受管暂存").and_then(|entry| {
        entry.map_or(Ok(()), |entry| {
            workspace::remove_owned(&workspace::safe_path(root, &entry.relative)?, root)
        })
    });
    if let Err(error) = result {
        eprintln!("回收暂存 {relative} 失败：{error}");
    }
}

fn children(root: &Path, relative: &str, skipped: &mut Vec<String>) -> Vec<String> {
    let result = workspace::safe_path(root, relative).and_then(|path| match fs::read_dir(path) {
        Ok(rows) => rows
            .map(|row| {
                row.map_err(|e| e.to_string()).and_then(|row| {
                    row.file_name()
                        .into_string()
                        .map_err(|_| "目录项名称无法识别".into())
                })
            })
            .collect::<Result<Vec<_>, _>>(),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(vec![]),
        Err(error) => Err(error.to_string()),
    });
    match result {
        Ok(rows) => rows,
        Err(error) => {
            skipped.push(format!("{relative}：{error}"));
            vec![]
        }
    }
}

fn collect(
    root: &Path,
    relative: &str,
    reason: &'static str,
    entries: &mut Vec<Entry>,
    skipped: &mut Vec<String>,
) {
    match measure(root, relative, reason) {
        Ok(Some(entry)) => entries.push(entry),
        Ok(None) => {}
        Err(error) => skipped.push(format!("{relative}：{error}")),
    }
}

fn age(entry: &Entry) -> Duration {
    entry.newest.elapsed().unwrap_or_default()
}

fn limit(mut group: Vec<Entry>, budget: u64, candidates: &mut Vec<Entry>) {
    group.sort_by_key(|entry| std::cmp::Reverse(entry.newest));
    let mut retained = 0u64;
    for entry in group {
        if age(&entry) >= WEEK
            || (age(&entry) >= DAY && retained.saturating_add(entry.size) > budget)
        {
            candidates.push(entry);
        } else {
            retained = retained.saturating_add(entry.size);
        }
    }
}

fn component_guard(root: &Path) -> Result<[fs::File; 2], String> {
    let lock = |relative: &str| {
        let guard = fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(workspace::safe_path(root, relative)?)
            .map_err(|e| e.to_string())?;
        // 文件锁覆盖 Python（后端解释器）受管任务使用的字节区间。
        guard
            .try_lock()
            .map_err(|_| "组件正在准备、构建或提交，请等待任务结束。")?;
        Ok::<_, String>(guard)
    };
    let guards = [
        lock("tmp/aniflive-task.lock")?,
        lock("tmp/resource-distribution.lock")?,
    ];
    for relative in [
        "tmp/update-transaction.json",
        "tmp/native-install.json",
        "tmp/resource-transaction.json",
        "runtimes/aniflive-tts/dependency-transaction.json",
    ] {
        match fs::symlink_metadata(workspace::safe_path(root, relative)?) {
            Ok(_) => return Err("工作区有未收尾的安装事务，请完成或修复后再清理。".into()),
            Err(error) if error.kind() == ErrorKind::NotFound => {}
            Err(error) => return Err(error.to_string()),
        }
    }
    Ok(guards)
}

fn duplicate_update(root: &Path, version: &str) -> Result<Option<Entry>, String> {
    let Some(entry) = measure(
        root,
        app_update::UPDATE_INSTALLER,
        "已经保留的同版本更新分发器",
    )?
    else {
        return Ok(None);
    };
    let Ok(build) = app_update::installed_build(root, version) else {
        return Ok(None);
    };
    let Ok(retained) = app_update::retained(root, version, &build) else {
        return Ok(None);
    };
    if entry.size != retained.manifest.size {
        return Ok(None);
    }
    let hash = &retained.manifest.sha256;
    let pending = app_update::update_installer_path(root)?;
    let installed = app_update::installer_path(root)?;
    Ok(
        (workspace::file_hash(&pending)? == *hash && workspace::file_hash(&installed)? == *hash)
            .then_some(entry),
    )
}

fn scan(context: &LaunchContext, version: &str, manual: bool) -> (Vec<Entry>, Vec<String>) {
    let root = &context.install;
    let mut entries = vec![];
    let mut skipped = vec![];
    let mut retry = vec![];
    let catalog = workspace::safe_path(root, "app/config/resources/catalog.json")
        .and_then(|path| resources::load(&path));
    if let Err(error) = &catalog {
        skipped.push(format!("无法读取下载目录登记：{error}"));
    }
    let declared = catalog
        .as_ref()
        .ok()
        .and_then(|rows| rows["resources"].as_array());
    for name in children(root, "tmp/resource-downloads", &mut skipped) {
        let relative = format!("tmp/resource-downloads/{name}");
        if declared.is_some_and(|rows| rows.iter().any(|row| row["resource_id"] == name)) {
            collect(
                root,
                &relative,
                "可重新下载的组件暂存",
                &mut retry,
                &mut skipped,
            );
        } else if declared.is_some() && RETIRED_DOWNLOADS.contains(&name.as_str()) {
            collect(
                root,
                &relative,
                "已退役组件的下载暂存",
                &mut entries,
                &mut skipped,
            );
        }
    }
    if manual {
        entries.append(&mut retry);
    } else {
        limit(retry, 8 * GIB, &mut entries);
    }

    for directory in ["cache/aniflive-wheels", "cache/dependencies/wheels"] {
        let mut retry = std::collections::BTreeMap::<String, Vec<Entry>>::new();
        for name in children(root, directory, &mut skipped) {
            let base = name.trim_end_matches(".identity").trim_end_matches(".part");
            if base.ends_with(".whl") {
                collect(
                    root,
                    &format!("{directory}/{name}"),
                    "可重新获取的依赖安装包或旧断点",
                    retry.entry(base.to_string()).or_default(),
                    &mut skipped,
                );
            }
        }
        let mut groups = retry.into_values().collect::<Vec<_>>();
        groups.sort_by_key(|group| std::cmp::Reverse(group.iter().map(|entry| entry.newest).max()));
        let mut retained = 0u64;
        for mut group in groups {
            let size = group
                .iter()
                .fold(0u64, |size, entry| size.saturating_add(entry.size));
            let youngest = group.iter().map(age).min().unwrap_or_default();
            let complete = group.iter().all(|entry| entry.relative.ends_with(".whl"));
            if (manual && complete)
                || youngest >= WEEK
                || (youngest >= DAY && retained.saturating_add(size) > 6 * GIB)
            {
                entries.append(&mut group);
            } else {
                retained = retained.saturating_add(size);
            }
        }
    }
    for (directory, prefixes) in [
        ("tmp/aniflive-tasks", &["m-"][..]),
        ("tmp/aniflive-source", &["s-", "upgrade-"]),
        ("tmp/aniflive-shared", &["nltk-"]),
    ] {
        let mut old = vec![];
        for name in children(root, directory, &mut skipped) {
            if prefixes.iter().any(|prefix| {
                name.strip_prefix(prefix).is_some_and(|suffix| {
                    suffix.len() == 8
                        && suffix
                            .bytes()
                            .all(|ch| ch.is_ascii_lowercase() || ch.is_ascii_digit() || ch == b'_')
                })
            }) {
                collect(
                    root,
                    &format!("{directory}/{name}"),
                    "已结束的本地语音构建暂存",
                    &mut old,
                    &mut skipped,
                );
            }
        }
        if manual {
            entries.extend(old.into_iter().filter(|entry| age(entry) >= DAY));
        } else {
            limit(old, 512 * 1024 * 1024, &mut entries);
        }
    }
    let mut retry = vec![];
    collect(
        root,
        "tmp/voice-environment/stage",
        "已结束的语音环境候选",
        &mut retry,
        &mut skipped,
    );
    collect(
        root,
        "cache/dependencies/python-3.12.10.zip",
        "可重新下载的解释器安装包",
        &mut retry,
        &mut skipped,
    );
    if !context.dev {
        collect(
            root,
            "cache/npm/_cacache",
            "可重新下载的代理依赖缓存",
            &mut retry,
            &mut skipped,
        );
    }
    limit(retry, 1024 * 1024 * 1024, &mut entries);
    let mut old = vec![];
    for name in children(root, "tmp/resource-transactions", &mut skipped) {
        if name
            .bytes()
            .all(|ch| ch.is_ascii_alphanumeric() || b"._-".contains(&ch))
        {
            collect(
                root,
                &format!("tmp/resource-transactions/{name}"),
                "已结束的组件事务暂存",
                &mut old,
                &mut skipped,
            );
        }
    }
    for name in children(root, "tmp/process", &mut skipped) {
        let generated = name
            .strip_prefix("worker-")
            .and_then(|name| name.rsplit_once('.'));
        if generated.is_some_and(|(id, extension)| {
            valid_generated_id(id) && matches!(extension, "json" | "stderr")
        }) {
            collect(
                root,
                &format!("tmp/process/{name}"),
                "已退出的依赖工作进程输出",
                &mut old,
                &mut skipped,
            );
        }
    }
    entries.extend(old.into_iter().filter(|entry| age(entry) >= WEEK));
    if manual {
        for name in children(root, "cache/python", &mut skipped) {
            let mut cached = vec![];
            collect(
                root,
                &format!("cache/python/{name}"),
                "可重新生成的旧解释器字节码",
                &mut cached,
                &mut skipped,
            );
            entries.extend(cached.into_iter().filter(|entry| age(entry) >= WEEK));
        }
        collect_legacy_dictionaries(root, &mut entries, &mut skipped);
        if let Ok(manifest) = workspace::safe_path(root, "models/manifest.json")
            .and_then(|path| resources::load(&path))
        {
            let package_files = manifest["files"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|row| {
                    let path = row["path"]
                        .as_str()?
                        .strip_prefix("aniflive-tts/packages/")?;
                    let (model, tail) = path.split_once('/')?;
                    let (revision, file) = tail.split_once('/')?;
                    Some(((model, revision), file))
                });
            let mut signed = std::collections::BTreeSet::new();
            let mut legacy_receipts = 0;
            for (package, file) in package_files {
                if file.starts_with(".export-stages-") {
                    signed.insert(package);
                }
                if file == "resource-install.json" {
                    legacy_receipts += 1;
                }
            }
            if !signed.is_empty() {
                skipped.push(format!(
                    "{} 个已登记的本地语音模型版本含旧导出暂存，暂存已计入模型完整性清单。",
                    signed.len()
                ));
            }
            if legacy_receipts > 0 {
                skipped.push(format!("{legacy_receipts} 份旧模型通用安装收据仍在模型总清单中，需要按模型完整性流程处理。"));
            }
        }
    }
    match duplicate_update(root, version) {
        Ok(Some(entry)) => entries.push(entry),
        Ok(None) => {}
        Err(error) => skipped.push(format!("更新分发器：{error}")),
    }
    entries.sort_by(|left, right| left.relative.cmp(&right.relative));
    (entries, skipped)
}

fn valid_generated_id(id: &str) -> bool {
    id.split_once('-').is_some_and(|(time, process)| {
        time.len() == 16
            && !process.is_empty()
            && process.len() <= 8
            && time
                .bytes()
                .chain(process.bytes())
                .all(|ch| ch.is_ascii_hexdigit())
    })
}

fn collect_legacy_dictionaries(root: &Path, entries: &mut Vec<Entry>, skipped: &mut Vec<String>) {
    let source = "runtimes/aniflive-tts/source";
    let release = workspace::safe_path(root, "app/release.json")
        .and_then(|path| resources::load(&path))
        .ok();
    let receipt = workspace::safe_path(root, &format!("{source}/windows-source-receipt.json"))
        .and_then(|path| resources::load(&path))
        .ok();
    let matches = |path: &str, directory: &str| {
        let path = path.replace('\\', "/").to_ascii_lowercase();
        path == directory
            || path
                .strip_prefix(directory)
                .is_some_and(|suffix| suffix.starts_with('/'))
    };
    for directory in ["TEMP/ja/open_jtalk_dic", "TEMP/ja/ja_userdic"] {
        let normalized = directory.to_ascii_lowercase();
        if release.as_ref().is_some_and(|release| {
            release["files"].as_array().is_some_and(|files| {
                files.iter().all(|row| {
                    row["path"]
                        .as_str()
                        .is_some_and(|path| !matches(path, &normalized))
                })
            })
        }) {
            collect(
                root,
                &format!("app/{directory}"),
                "旧版写入程序目录的日语词典缓存",
                entries,
                skipped,
            );
        }
        let normalized = format!("minimal_inference/{normalized}");
        if receipt.as_ref().is_some_and(|receipt| {
            receipt["inventory"].as_object().is_some_and(|files| {
                !files.is_empty() && files.keys().all(|path| !matches(path, &normalized))
            })
        }) {
            collect(
                root,
                &format!("{source}/minimal_inference/{directory}"),
                "旧版写入语音源码目录的日语词典缓存",
                entries,
                skipped,
            );
        }
    }
}

pub fn run(
    context: &LaunchContext,
    version: &str,
    confirmation: Option<&str>,
    manual: bool,
) -> Result<Value, String> {
    let _guard = component_guard(&context.install)?;
    if crate::component_reset::pending(context)?.is_some() {
        return Err("工作区有未收尾的组件重置，请完成后再清理。".into());
    }
    let (entries, mut skipped) = scan(context, version, manual);
    let mut hash = Sha256::new();
    hash.update(context.install.to_string_lossy().as_bytes());
    let mut total = 0u64;
    for entry in &entries {
        hash.update(entry.identity);
        total = total.saturating_add(entry.size);
    }
    let token = format!("{:x}", hash.finalize());
    let mut removed_bytes = 0u64;
    let mut removed_count = 0;
    if !manual || confirmation.is_some() {
        if manual && confirmation != Some(token.as_str()) {
            return Err("工作区文件在检查后发生变化，请重新检查。".into());
        }
        for entry in &entries {
            match remove_entry(&context.install, entry) {
                Ok(()) => {
                    removed_bytes = removed_bytes.saturating_add(entry.size);
                    removed_count += 1;
                }
                Err(error) => skipped.push(format!("{}：{error}", entry.relative)),
            }
        }
    }
    Ok(
        json!({"token":token,"total_bytes":total,"entries":entries.iter().map(|row| json!({
        "path":row.relative,"bytes":row.size,"reason":row.reason})).collect::<Vec<_>>(),
        "removed_bytes":removed_bytes,"removed_count":removed_count,"skipped":skipped,
        "fields":{"token":"本次盘点的确认值","total_bytes":"候选文件逻辑字节数","entries":"可回收路径清单",
        "path":"工作区相对路径","bytes":"文件逻辑字节数","reason":"可回收原因","removed_bytes":"已清理文件逻辑字节数",
        "removed_count":"已清理路径数","skipped":"跳过的路径及原因"}}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Fixture(std::path::PathBuf);
    impl Fixture {
        fn new() -> Self {
            let root = crate::canonical_path(&std::env::temp_dir())
                .unwrap()
                .join(format!("voice-cleanup-{}", workspace::new_id()));
            workspace::initialize(&root).unwrap();
            let fixture = Self(root);
            fixture.write(
                "app/config/resources/catalog.json",
                br#"{"resources":[{"resource_id":"model-a"}]}"#,
            );
            fixture.write("app/config/default.yaml", b"{}");
            fixture.write("app/scripts/backend-entry.py", b"# fixture");
            fixture
        }
        fn write(&self, relative: &str, bytes: &[u8]) {
            let path = self.0.join(relative);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, bytes).unwrap();
        }
        fn context(&self) -> LaunchContext {
            LaunchContext::portable(&self.0).unwrap()
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn preview_never_removes_and_changed_scope_requires_new_confirmation() {
        let fixture = Fixture::new();
        let context = fixture.context();
        let cache = "tmp/resource-downloads/model-a/model.bin";
        fixture.write(cache, b"first");
        fixture.write("tmp/resource-downloads/tts-voxcpm2/model.gguf", b"retired");
        fixture.write("tmp/resource-downloads/unknown/project.txt", b"leave alone");
        fixture.write(
            "models/aniflive-tts/packages/demo/revision/.export-stages-fixture/keep.bin",
            b"signed",
        );
        fixture.write("models/manifest.json", br#"{"files":[{"path":"aniflive-tts/packages/demo/revision/.export-stages-fixture/keep.bin"}]}"#);
        let plan = run(&context, "1.0.0", None, true).unwrap();
        assert_eq!(plan["total_bytes"], 12);
        assert_eq!(plan["entries"].as_array().unwrap().len(), 2);
        assert!(plan["skipped"][0].as_str().unwrap().contains("完整性清单"));
        assert!(fixture.0.join(cache).exists());
        fixture.write(cache, b"changed size");
        assert!(run(&context, "1.0.0", plan["token"].as_str(), true).is_err());
        assert!(fixture.0.join(cache).exists());
        let plan = run(&context, "1.0.0", None, true).unwrap();
        let result = run(&context, "1.0.0", plan["token"].as_str(), true).unwrap();
        assert_eq!(result["removed_count"], 2);
        assert_eq!(result["removed_bytes"], 19);
        assert!(fixture
            .0
            .join("tmp/resource-downloads/unknown/project.txt")
            .is_file());
        assert!(fixture
            .0
            .join("models/aniflive-tts/packages/demo/revision/.export-stages-fixture/keep.bin")
            .is_file());
    }

    #[test]
    fn automatic_cleanup_preserves_recent_resume_and_pending_transactions() {
        let fixture = Fixture::new();
        let context = fixture.context();
        let cache = "tmp/resource-downloads/model-a/download.part";
        fixture.write(cache, b"resume");
        let retired = "tmp/resource-downloads/tts-voxcpm1/download.part";
        fixture.write(retired, b"retired");
        let journal = "tmp/native-install.json";
        fixture.write(journal, b"{} ");
        assert!(run(&context, "1.0.0", None, false).is_err());
        assert!(fixture.0.join(retired).is_file());
        fs::remove_file(fixture.0.join(journal)).unwrap();
        let guard = component_guard(&fixture.0).unwrap();
        assert!(run(&context, "1.0.0", None, false).is_err());
        drop(guard);
        assert_eq!(
            run(&context, "1.0.0", None, false).unwrap()["removed_count"],
            1
        );
        assert!(fixture.0.join(cache).is_file());
        assert!(!fixture.0.join(retired).exists());
    }

    #[test]
    fn cleanup_removes_only_the_verified_installer_duplicate_and_unlisted_dictionary() {
        let fixture = Fixture::new();
        let context = fixture.context();
        fixture.write("app/release.json", br#"{"schema":1,"version":"1.0.0","build":"fixture-build","files":[{"path":"TEMP/ja/ja_userdic/seed.csv"}]}"#);
        let duplicate = fixture.0.join(app_update::UPDATE_INSTALLER);
        let verified = vec![42; 1024 * 1024];
        fixture.write(app_update::UPDATE_INSTALLER, &verified);
        app_update::retain_local_installer(&fixture.0, &duplicate, "1.0.0", "fixture-build")
            .unwrap();
        let dictionary = "app/TEMP/ja/open_jtalk_dic/sys.dic";
        let signed = "app/TEMP/ja/ja_userdic/seed.csv";
        fixture.write(dictionary, b"dictionary cache");
        fixture.write(signed, b"signed seed");

        let plan = run(&context, "1.0.0", None, true).unwrap();
        assert_eq!(plan["entries"].as_array().unwrap().len(), 2);
        fixture.write(app_update::UPDATE_INSTALLER, &vec![0; verified.len()]);
        assert!(run(&context, "1.0.0", plan["token"].as_str(), true).is_err());
        assert!(duplicate.exists());
        fixture.write(app_update::UPDATE_INSTALLER, &verified);
        let plan = run(&context, "1.0.0", None, true).unwrap();
        assert_eq!(
            run(&context, "1.0.0", plan["token"].as_str(), true).unwrap()["removed_count"],
            2
        );
        assert!(!duplicate.exists());
        assert!(!fixture.0.join(dictionary).exists());
        assert!(fixture.0.join(signed).is_file());
        assert_eq!(
            fs::read(fixture.0.join(app_update::MAINTENANCE_INSTALLER)).unwrap(),
            verified
        );
    }
}
