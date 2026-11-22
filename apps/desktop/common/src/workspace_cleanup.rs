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
const RETIRED_DOWNLOADS: &[&str] = &["tts-voxcpm1", "tts-voxcpm2", "tts-audiocpp-runtime", "sensevoice-small", "wespeaker-cnceleb-resnet34"];

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

/// component_file_guards（跨进程组件维护锁）：清理和正常卸载共用后端任务的文件锁区间。
pub(crate) fn component_file_guards(root: &Path) -> Result<[fs::File; 2], String> {
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
    Ok([
        lock("tmp/aniflive-task.lock")?,
        lock("tmp/resource-distribution.lock")?,
    ])
}
fn component_guard(root: &Path) -> Result<[fs::File; 2], String> {
    let guards = component_file_guards(root)?;
    for relative in [
        "tmp/update-transaction.json",
        "tmp/native-install.json",
        "tmp/resource-transaction.json",
        "runtimes/aniflive-tts/dependency-transaction.json",
        "runtimes/aniflive-tts/layout-transaction.json",
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

fn scan(context: &LaunchContext, version: &str, manual: bool, extended: bool) -> (Vec<Entry>, Vec<String>) {
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
        } else if extended {
            skipped.push(format!("{relative}：未登记的下载目录，保留原件。"));
        }
    }
    if manual {
        if extended { collect(root, "cache/cuda", "可重新生成的显卡编译缓存", &mut entries, &mut skipped); }
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
        if manual && extended {
            if let Some(pid) = name.strip_prefix("voice-initialization-installer-").and_then(|suffix|suffix.parse::<u32>().ok()) {
                match workspace::process_identity(pid) {
                    Ok(None) => {
                        let mut legacy = vec![];
                        collect(root, &format!("tmp/process/{name}"), "已退出安装器的历史网页缓存", &mut legacy, &mut skipped);
                        entries.extend(legacy.into_iter().filter(|entry| age(entry) >= WEEK));
                    }
                    _ => skipped.push(format!("tmp/process/{name}：进程仍存在或无法确认，保留现场。")),
                }
            }
        }
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
            if !signed.is_empty() && !extended {
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
    if manual && extended && !root.join("tmp/storage-model-cleanup.json").exists() {
        for relative in ["models/analysis/sensevoice", "models/audiocpp/runtime"] {
            let id = if relative.ends_with("sensevoice") {"sensevoice-small"} else {"tts-audiocpp-runtime"};
            if declared.is_some_and(|rows| !rows.iter().any(|row|row["resource_id"] == id)) {
                collect(root, relative, "受管模型维护清理", &mut entries, &mut skipped);
            }
        }
        if let Ok(manifest) = resources::load(&root.join("models/manifest.json")) {
            let mut stages = std::collections::BTreeSet::new();
            for row in manifest["files"].as_array().into_iter().flatten() {
                if let Some(path) = row["path"].as_str() {
                    let parts = path.split('/').collect::<Vec<_>>();
                    if parts.len() >= 6 && parts[0] == "aniflive-tts" && parts[1] == "packages" && parts[4].starts_with(".export-stages-") {
                        stages.insert(format!("models/{}",parts[..5].join("/")));
                    }
                }
            }
            for stage in stages { collect(root, &stage, "受管模型维护清理", &mut entries, &mut skipped); }
        }
    }
    if manual && extended {
        collect(root, "tmp/storage-model-cleanup.json", "受管模型维护清理", &mut entries, &mut skipped);
        if let Some(entry) = entries.iter_mut().find(|row|row.relative == "tmp/storage-model-cleanup.json") {
            entry.size = 0; // 恢复入口展示尚未删除的暂存容量，不把事务元数据算作回收收益。
            match measure(root,"tmp/storage-model-cleanup-old","待收尾模型暂存") {
                Ok(Some(backup)) => {
                    let mut identity = Sha256::new(); identity.update(entry.identity); identity.update(backup.identity);
                    entry.identity = identity.finalize().into();
                    if resources::load(&root.join(&entry.relative)).is_ok_and(|v|v["state"] == "committed") { entry.size = backup.size; }
                },
                Ok(None) => {},
                Err(error) => skipped.push(format!("模型维护收尾暂存：{error}")),
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

/// storage_protected（固定保护区占用）：只读展示，不产生可删除编号。
pub fn storage_protected(c: &LaunchContext) -> Value {
    let mut rows = [("data","用户资料","配置、会话、音色、录音和导入资料保持保留。"),
        ("backups","滚动备份","备份不参与垃圾清理及组件卸载。"),
        ("cache/aniflive-tts/inputs","模型原始输入","保留用于重装和重新转换的原始权重与输入。"),
        ("models/aniflive-tts/shared","本地语音共享资源","保留现用模型共用的文本、语言与音频资源。")]
        .into_iter().map(|(path,label,reason)| json!({"path":path,"label":label,"reason":reason,
            "bytes":measure(&c.install,path,"受保护内容").ok().flatten().map(|r|r.size),
            "fields":{"path":"受保护路径","label":"用途","reason":"保留原因","bytes":"估算逻辑字节数，无法读取时为空"}})).collect::<Vec<_>>();
    if let Ok(specs) = resources::specs(c) {
        for spec in specs.into_iter().filter(|row| row["required"] == true) {
            if let Ok(path) = resources::destination(c, &spec) {
                if let Ok(relative) = path.strip_prefix(&c.install) {
                    let relative = relative.to_string_lossy().replace('\\', "/");
                    rows.push(json!({"path":relative,"label":spec["title"],"reason":"必需组件，保留供基础功能使用。",
                        "bytes":measure(&c.install,&relative,"必需组件").ok().flatten().map(|r|r.size)}));
                }
            }
        }
    }
    let python = measure(&c.install,"runtimes/python","必需语音环境").ok().flatten().map(|r|r.size);
    let gpu = crate::runtime::status(c).ok().is_some_and(|value| value["components"].as_array().into_iter().flatten().any(|r|r["resource_id"] == crate::runtime::GPU_RESOURCE && r["files_installed"] == true));
    let optional = if gpu {measure(&c.install,"runtimes/python/Lib/site-packages/torch","单列显卡依赖").ok().flatten().map(|r|r.size).unwrap_or(0)} else {0};
    rows.push(json!({"path":"runtimes/python","label":"必需语音环境","reason":"保留基础语音依赖，占用已扣除单列的可选显卡依赖。","bytes":python.map(|bytes|bytes.saturating_sub(optional))}));
    json!(rows)
}

/// storage_components（本机可选组件及容量）：归属由正式资源目录产生。
pub fn storage_components(c: &LaunchContext, garbage: &Value) -> Result<Value, String> {
    let mut rows = vec![];
    for item in resources::specs(c)? {
        if item["required"] != false { continue; }
        let id = item["resource_id"].as_str().ok_or("组件编号缺失")?;
        let path = resources::destination(c, &item)?;
        let relative = path.strip_prefix(&c.install).map_err(|_|"组件路径越界")?.to_string_lossy().replace('\\', "/");
        let measured = measure(&c.install, &relative, "可选组件");
        let mut bytes = measured.as_ref().ok().and_then(|v|v.as_ref()).map(|v|v.size).unwrap_or(0);
        let conversion_libraries = measure(&c.install, "runtimes/aniflive-tts/conversion-libs", "转换动态库").ok().flatten().map(|r| r.size).unwrap_or(0);
        if id == "tts-aniflive-converter" { bytes = bytes.saturating_add(conversion_libraries); }
        if id == "tts-aniflive-runtime" {
            let conversion = measure(&c.install, "runtimes/aniflive-tts/export-packages", "转换依赖").ok().flatten().map(|r|r.size).unwrap_or(0);
            bytes = bytes.saturating_sub(conversion).saturating_sub(conversion_libraries);
        }
        // 旧布局尚未显式迁移时，构建器仍在推理目录；盘点按其正式文件收据预归属到转换层。
        if matches!(id, "tts-aniflive-runtime" | "tts-aniflive-converter") {
            let receipt_path = workspace::safe_path(&c.install,"runtimes/aniflive-tts/packages/aniflive-packages.json")?;
            if let Ok(receipt) = resources::load(&receipt_path) {
                let mut legacy = 0u64;
                for row in receipt["files"].as_array().into_iter().flatten() {
                    let Some(name) = row["path"].as_str().and_then(|p|p.strip_prefix("tensorrt_libs/")) else {continue};
                    if !name.contains('/') && name.starts_with("nvinfer_builder_resource_") && name.ends_with("_11.dll") {
                        legacy = legacy.saturating_add(measure(&c.install,&format!("runtimes/aniflive-tts/packages/tensorrt_libs/{name}"),"旧布局转换动态库").ok().flatten().map(|r|r.size).unwrap_or(0));
                    }
                }
                if id == "tts-aniflive-runtime" { bytes = bytes.saturating_sub(legacy); }
                else { bytes = bytes.saturating_add(legacy); }
            }
        }
        for row in garbage.as_array().into_iter().flatten() {
            if row["path"].as_str().is_some_and(|path| path == relative || path.starts_with(&(relative.clone()+"/"))) {
                bytes = bytes.saturating_sub(row["bytes"].as_u64().unwrap_or(0));
            }
        }
        let error = measured.as_ref().err().cloned();
        let plan = crate::component_reset::uninstall_plan(c, id);
        let (label,purpose,impact) = if id == "tts-aniflive-runtime" {
            (json!("本地 TTS（语音合成）推理框架"), "执行已有模型的语音合成。", "同时卸载转换框架，保留已转换模型和用户资料。")
        } else if id == "tts-aniflive-converter" {
            (item["title"].clone(), "导入和转换模型，构建推理引擎。", "保留推理框架和已有模型；下次导入时重新配置。")
        } else if item["preparation"] == "aniflive_local_build" {
            (item["title"].clone(), "已转换的本地语音模型。", "移除该模型全部转换版本，保留编号、别名、音色和原始资料。")
        } else { (item["title"].clone(), "可选增强能力。", "移除组件文件；重新启用时需要安装。") };
        rows.push(json!({"resource_id":id,"label":label,"purpose":purpose,"impact":impact,"bytes":bytes,"installed":path.exists(),
            "blocked_reason":error.or_else(||plan.err()),"fields":{"resource_id":"组件编号","label":"组件名称","bytes":"估算逻辑字节数","installed":"文件已安装","blocked_reason":"卸载阻止原因"}}));
    }
    let gpu = crate::runtime::status(c)?;
    let installed = gpu["components"].as_array().into_iter().flatten().any(|r| r["resource_id"] == crate::runtime::GPU_RESOURCE && r["files_installed"] == true);
    let bytes = measure(&c.install, "runtimes/python/Lib/site-packages/torch", "显卡张量依赖").ok().flatten().map(|r|r.size).unwrap_or(0);
    rows.push(json!({"resource_id":crate::runtime::GPU_RESOURCE,"label":"GPU（图形处理器）运行依赖","purpose":"使用显卡加速语音识别。","impact":"通过增量任务切换到处理器依赖，保留基础语音环境。","installed":installed,"bytes":if installed {bytes}else{0},
        "blocked_reason":crate::component_reset::uninstall_check(c, crate::runtime::GPU_RESOURCE).err()}));
    Ok(json!(rows))
}

pub fn run(
    context: &LaunchContext,
    version: &str,
    confirmation: Option<&str>,
    manual: bool,
) -> Result<Value, String> {
    run_impl(context, version, confirmation, manual, None, false)
}

/// run_selected（按用户勾选的受管条目回收）；空选择不会回收任何内容。
pub fn run_selected(
    context: &LaunchContext, version: &str, confirmation: Option<&str>, manual: bool,
    selected: Option<&[String]>, // selected（仅接受本轮盘点中已有的相对路径编号）。
) -> Result<Value, String> {
    run_impl(context, version, confirmation, manual, selected, true)
}
fn run_impl(context: &LaunchContext, version: &str, confirmation: Option<&str>, manual: bool, selected: Option<&[String]>, extended: bool) -> Result<Value,String> {
    let _guard = component_guard(&context.install)?;
    if crate::component_reset::pending(context)?.is_some() {
        return Err("工作区有未收尾的组件重置，请完成后再清理。".into());
    }
    let (entries, mut skipped) = scan(context, version, manual, extended);
    let mut hash = Sha256::new();
    hash.update(context.install.to_string_lossy().as_bytes());
    let mut total = 0u64;
    for entry in &entries {
        hash.update(entry.identity);
        total = total.saturating_add(entry.size);
    }
    let token = if extended {
        let identities = entries.iter().map(|entry| (entry.relative.clone(), entry.identity.iter().map(|b| format!("{b:02x}")).collect::<String>())).collect::<std::collections::BTreeMap<_,_>>();
        json!({"root":context.install.to_string_lossy(),"identities":identities}).to_string()
    } else { format!("{:x}", hash.finalize()) };
    let mut removed_bytes = 0u64;
    let mut removed_count = 0;
    if !manual || confirmation.is_some() {
        let approved = if extended {
            let value: Value = serde_json::from_str(confirmation.filter(|text|text.len() <= 1024*1024).ok_or("缺少有效盘点确认值")?).map_err(|_|"盘点确认值无效")?;
            if value["root"].as_str() != Some(context.install.to_string_lossy().as_ref()) || !value["identities"].is_object() {
                return Err("盘点不属于本机工作区，请重新扫描。".into());
            }
            Some(value)
        } else {
            if manual && confirmation != Some(token.as_str()) { return Err("工作区文件在检查后发生变化，请重新检查。".into()); }
            None
        };
        if let Some(selected) = selected {
            if selected.iter().any(|path| approved.as_ref().map_or_else(
                || !entries.iter().any(|entry| entry.relative == *path),
                |value| value["identities"].get(path).is_none())) {
                return Err("所选项目不属于本轮盘点，请重新扫描。".into());
            }
            for path in selected.iter().filter(|path| !entries.iter().any(|entry| entry.relative == **path)) {
                skipped.push(format!("{path}：内容已变化、已移除或不再符合回收条件，已跳过。"));
            }
        }
        for entry in &entries {
            if selected.is_some_and(|paths| !paths.contains(&entry.relative)) { continue; }
            if let Some(approved) = &approved {
                let identity = entry.identity.iter().map(|b|format!("{b:02x}")).collect::<String>();
                if approved["identities"][&entry.relative] != identity {
                    skipped.push(format!("{}：文件已变化，请重新扫描后再选中。", entry.relative));
                    continue;
                }
            }
            let removal = if entry.reason == "受管模型维护清理" {
                crate::runtime::worker(context, &context.python, "storage-model-cleanup", &[
                    "--target".into(), context.install.to_string_lossy().into_owned(),
                    "--program".into(), context.installed_program().to_string_lossy().into_owned(),
                    "--model".into(), entry.relative.clone()
                ], &std::sync::atomic::AtomicBool::new(false)).map(|value| (value["removed_bytes"].as_u64().unwrap_or(0), value["error"].as_str().map(str::to_owned)))
            } else { remove_entry(&context.install, entry).map(|_| (entry.size, None)) };
            match removal {
                Ok((bytes, error)) => {
                    removed_bytes = removed_bytes.saturating_add(bytes);
                    if let Some(error) = error { skipped.push(format!("{}：{error}", entry.relative)); }
                    else { removed_count += 1; }
                }
                Err(error) => {
                    if let Ok(Some(remaining)) = measure(&context.install, &entry.relative, entry.reason) {
                        removed_bytes = removed_bytes.saturating_add(entry.size.saturating_sub(remaining.size));
                    }
                    skipped.push(format!("{}：{error}", entry.relative));
                },
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
    fn selected_cleanup_keeps_unselected_and_rejects_foreign_paths() {
        let fixture = Fixture::new();
        fixture.write("tmp/resource-downloads/model-a/keep.bin", b"keep");
        fixture.write("tmp/resource-downloads/sensevoice-small/old.bin", b"old");
        let context = fixture.context();
        let plan = run_selected(&context, "1.0.0", None, true, None).unwrap();
        fixture.write("tmp/resource-downloads/model-a/keep.bin", b"unselected changed");
        assert!(run_selected(&context,"1.0.0",plan["token"].as_str(),true,Some(&["data/user".into()])).is_err());
        let selected = vec!["tmp/resource-downloads/sensevoice-small".to_owned()];
        let result = run_selected(&context,"1.0.0",plan["token"].as_str(),true,Some(&selected)).unwrap();
        assert_eq!(result["removed_bytes"],3);
        assert!(fixture.0.join("tmp/resource-downloads/model-a/keep.bin").exists());
        assert!(!fixture.0.join(&selected[0]).exists());
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
