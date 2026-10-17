//! DSH adapter（宿主适配）：配置驱动的 npm（包管理器）安装与兼容判断。
use crate::{
    resources::{check_cancel, load, Progress},
    workspace::{atomic_json, file_hash, safe_path},
    LaunchContext,
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::Read,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::atomic::{AtomicBool, Ordering},
    time::{Duration, Instant},
};

pub fn target(program: &Path) -> Result<Value, String> {
    let lock = load(&program.join("config/dsh/runtime.lock.json"))?;
    let mut spec = lock["distribution"].clone();
    if !spec.is_object() || spec["source_kind"] != "npm_package" || spec["adapter"] != "npm_cli_v1"
    {
        return Err("DSH（代理宿主）分发适配器不受支持。".into());
    }
    let version = lock["version"].as_str().ok_or("DSH 目标版本缺失")?;
    semver::Version::parse(version).map_err(|_| "DSH 必须声明精确版本")?;
    spec["version"] = lock["version"].clone();
    spec["package_manager"] = lock["package_manager"].clone();
    for key in ["entry", "modules", "cli_entry", "schedule", "pnpm_entry", "plugin_record"] {
        safe_path(program, spec[key].as_str().ok_or("宿主入口配置缺失")?)?;
    }
    semver::VersionReq::parse(
        &spec["node_version_range"]
            .as_str()
            .ok_or("运行环境范围缺失")?
            .split("||")
            .next()
            .unwrap_or("")
            .trim(),
    )
    .map_err(|_| "运行环境版本范围无效")?;
    Ok(spec)
}
pub fn current(metadata: &Value, spec: &Value) -> bool {
    [
        "source_kind",
        "adapter",
        "package_name",
        "version",
        "bridge_protocol",
        "entry",
        "modules",
        "cli_entry",
        "schedule",
        "pnpm_entry",
        "package_manager",
        "required_files",
    ]
    .iter()
    .all(|key| metadata[*key] == spec[*key])
}
pub fn check_plugin(program: &Path, spec: &Value) -> Result<(), String> {
    let record_path = safe_path(
        program,
        spec["plugin_record"].as_str().ok_or("插件适配记录缺失")?,
    )?;
    let record = load(&record_path)?;
    let package_dir = record_path.parent().ok_or("插件适配目录缺失")?;
    let package = load(&package_dir.join("package.json"))?;
    let target_version = spec["version"].as_str().ok_or("DSH 目标版本缺失")?;
    if record["dsh_version"] != spec["version"]
        || record["bridge_protocol"] != spec["bridge_protocol"]
        || record["adapter"] != spec["adapter"]
        || record["schema"] != 2
        || record["package_name"] != package["name"]
        || record["plugin_version"] != package["version"]
        || package["engines"]["dsh"] != target_version
        || record["peer_dependencies"] != package["peerDependencies"]
    {
        return Err("项目插件适配记录、包版本或 DSH（代理宿主）兼容声明与目标不一致，请更新程序。".into());
    }
    let peer_dependencies = package["peerDependencies"].as_object().ok_or("插件同宿主依赖声明无效")?;
    if peer_dependencies
        .iter()
        .any(|(name, version)| name.starts_with("@deepseek-ai/dsh-") && version.as_str() != Some(target_version))
        || peer_dependencies.get("@deepseek-ai/cordis").and_then(Value::as_str) != Some("~4.0.5")
    {
        return Err("项目插件的宿主依赖版本与 DSH（代理宿主）目标不一致。".into());
    }

    let package_relative = package_dir
        .strip_prefix(program)
        .map_err(|_| "插件适配目录不在程序范围内")?
        .to_string_lossy()
        .replace('\\', "/");
    let exports = package["exports"].as_object().ok_or("插件入口导出声明缺失")?;
    for (subpath, record_key) in [
        (".", "bridge"),
        ("./wake-tasks", "wake_tasks"),
        ("./wake-tasks/locale/en.json", "wake_tasks_locale_en"),
        ("./wake-tasks/locale/zh-CN.json", "wake_tasks_locale_zh_cn"),
    ] {
        let export = exports[subpath].as_str().ok_or("插件入口必须显式导出文件路径")?;
        if record["package_exports"][record_key] != export {
            return Err("项目插件入口与适配记录不一致。".into());
        }
        let target = export.strip_prefix("./").ok_or("插件入口必须位于功能包内")?;
        let relative = format!("{package_relative}/{target}");
        if !safe_path(program, &relative)?.is_file() {
            return Err(format!("项目插件的运行入口缺失：{subpath}"));
        }
    }
    Ok(())
}
pub fn candidate_name(spec:&Value)->Result<String,String>{
    // candidate key（候选键）：版本、适配器及工具变化自动使用不同候选，不混用旧目标。
    let digest=format!("{:x}",Sha256::digest(serde_json::to_vec(spec).map_err(|e|e.to_string())?));
    Ok(format!("candidate-{}",&digest[..24]))
}

#[derive(Default)]
struct InstallProgress{
    pending:Vec<u8>, // pending（尚未收到完整换行的日志内容）。
    metadata:u64, // metadata（已处理的包元数据请求数）。
    archives:u64, // archives（下载或复用缓存的包归档请求数）。
    phase:String, // phase（当前安装阶段中文说明）。
}
impl InstallProgress{
    fn read(&mut self,file:&mut fs::File)->Result<(),String>{
        file.read_to_end(&mut self.pending).map_err(|e|e.to_string())?;
        let end=self.pending.iter().rposition(|byte|*byte==b'\n').map(|index|index+1).unwrap_or(0);
        for raw in self.pending[..end].split(|byte|*byte==b'\n'){
            let line=String::from_utf8_lossy(raw);
            if let Some(phase)=line.strip_prefix("VOICE_DSH_PHASE "){self.phase=phase.trim().into();}
            else if line.contains("http fetch")||line.contains("http cache"){
                if line.contains(".tgz"){self.archives+=1;self.phase=format!("下载或复用 npm（包管理器）归档缓存（已处理 {} 次包请求）",self.archives);}
                else{self.metadata+=1;self.phase=format!("解析 npm（包管理器）依赖（已处理 {} 次元数据请求）",self.metadata);}
            }else if line.contains("info run "){self.phase="正在执行上游声明的依赖安装脚本".into();}
        }
        self.pending.drain(..end);
        if self.pending.len()>64*1024{self.pending.clear();}
        Ok(())
    }
}
fn node_candidates() -> Vec<PathBuf> {
    let mut paths = vec![];
    for directory in std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()) {
        paths.push(directory.join(if cfg!(windows) { "node.exe" } else { "node" }));
    }
    for key in ["ProgramFiles", "LOCALAPPDATA"] {
        if let Some(base) = std::env::var_os(key) {
            let base = PathBuf::from(base);
            paths.push(base.join("nodejs/node.exe"));
            paths.push(base.join("Programs/nodejs/node.exe"));
        }
    }
    paths
}
fn hidden(command: &mut Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    command
        .env_remove("NODE_OPTIONS")
        .env_remove("ELECTRON_RUN_AS_NODE");
}
fn output(command: &mut Command) -> Result<std::process::Output, String> {
    hidden(command);
    command.stdout(Stdio::piped()).stderr(Stdio::null());
    let mut child = command
        .spawn()
        .map_err(|_| "无法执行 Node.js（运行环境）")?;
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if child.try_wait().map_err(|e| e.to_string())?.is_some() {
            return child.wait_with_output().map_err(|e| e.to_string());
        }
        if Instant::now() > deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err("Node.js（运行环境）检测超时".into());
        }
        std::thread::sleep(Duration::from_millis(30));
    }
}
pub fn discover(spec: &Value, require_npm: bool) -> Result<(PathBuf, Option<PathBuf>), String> {
    let range = spec["node_version_range"]
        .as_str()
        .ok_or("Node.js（运行环境）支持范围缺失")?;
    let ranges = range
        .split("||")
        .map(|v| semver::VersionReq::parse(v.trim()).map_err(|_| "Node.js 支持范围无效"))
        .collect::<Result<Vec<_>, _>>()?;
    for candidate in node_candidates() {
        if !candidate.is_file() {
            continue;
        }
        let response=match output(Command::new(&candidate).args(["-p","JSON.stringify({version:process.versions.node,platform:process.platform,arch:process.arch})"])){Ok(v) if v.status.success()=>v,_=>continue};
        let identity: Value = match serde_json::from_slice(&response.stdout) {
            Ok(v) => v,
            Err(_) => continue,
        };
        let version = match semver::Version::parse(identity["version"].as_str().unwrap_or("")) {
            Ok(v) => v,
            Err(_) => continue,
        };
        if identity["arch"] != "x64"
            || identity["platform"] != "win32"
            || !ranges.iter().any(|r| r.matches(&version))
        {
            continue;
        }
        let npm = candidate
            .parent()
            .unwrap()
            .join("node_modules/npm/bin/npm-cli.js");
        if require_npm {
            if !npm.is_file() {
                continue;
            }
            if !output(Command::new(&candidate).arg(&npm).arg("--version"))
                .is_ok_and(|v| v.status.success())
            {
                continue;
            }
        }
        return Ok((
            crate::canonical_path(&candidate)?,
            npm.is_file().then_some(npm),
        ));
    }
    Err(format!("需要 Windows x64（64 位系统）的 Node.js（运行环境）{range}{}，请安装后重新检测或重启程序。",if require_npm{"及 npm（包管理器）"}else{""}))
}
pub fn guide(reason: &str) -> bool {
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::UI::WindowsAndMessaging::*;
        let wide = |s: &str| s.encode_utf16().chain(Some(0)).collect::<Vec<_>>();
        let body=format!("{reason}\n\n是：打开 Node.js（运行环境）官方安装页面\n否：重新检测\n取消：返回\n\n安装完成后选择重新检测。");
        match MessageBoxW(
            std::ptr::null_mut(),
            wide(&body).as_ptr(),
            wide("脚本运行环境尚不可用").as_ptr(),
            MB_YESNOCANCEL | MB_ICONINFORMATION,
        ) {
            IDYES => {
                let _ = crate::windows::open_url("https://nodejs.org/en/download");
                true
            }
            IDNO => true,
            _ => false,
        }
    }
    #[cfg(not(windows))]
    {
        let _ = reason;
        false
    }
}
pub fn inspect(program: &Path, root: &Path) -> Result<Value, String> {
    let metadata = load(&root.join("runtime.json"))?;
    if metadata["schema"] != 3
        || metadata["source_kind"] != "npm_package"
        || metadata["adapter"] != "npm_cli_v1"
    {
        return Err("npm（包管理器）宿主运行记录无效".into());
    }
    let name = metadata["package_name"].as_str().ok_or("宿主包名缺失")?;
    let modules = metadata["modules"].as_str().ok_or("宿主依赖目录缺失")?;
    let package = load(&safe_path(root, &format!("{modules}/{name}/package.json"))?)?;
    if package["name"] != metadata["package_name"] || package["version"] != metadata["version"] {
        return Err("DSH（代理宿主）实际包名或版本与安装记录不一致".into());
    }
    let tool=load(&safe_path(root,&format!("{modules}/pnpm/package.json"))?)?;
    if tool["name"]!="pnpm"||metadata["package_manager"].as_str()!=Some(format!("pnpm@{}",tool["version"].as_str().unwrap_or("")).as_str()){
        return Err("私有 pnpm（插件包管理器）实际版本与记录不一致".into());
    }
    if !root.join("package.json").is_file(){return Err("DSH（代理宿主）根包信息缺失".into());}
    for key in ["entry", "cli_entry", "schedule", "pnpm_entry"] {
        if !safe_path(root, metadata[key].as_str().ok_or("运行入口缺失")?)?.is_file() {
            return Err("DSH（代理宿主）必要入口缺失".into());
        }
    }
    for row in metadata["required_files"]
        .as_array()
        .ok_or("必要资源声明缺失")?
    {
        if !safe_path(root, row.as_str().ok_or("必要资源路径无效")?)?.is_file() {
            return Err("DSH（代理宿主）网页或包管理资源缺失".into());
        }
    }
    let spec = target(program)?;
    if current(&metadata, &spec) {
        check_plugin(program, &spec)?;
    }
    Ok(metadata)
}
pub fn install(
    context: &LaunchContext,
    root: &Path,
    cancel: &AtomicBool,
    progress: &mut Progress,
) -> Result<(), String> {
    let program = context.installed_program();
    let spec = target(&program)?;
    check_plugin(&program, &spec)?;
    let (node, npm) = discover(&spec, true)?;
    check_cancel(cancel)?;
    let proof=root.with_extension("verified.json");
    if root.join("manifest.json").is_file()&&inspect(&program,root).is_ok_and(|metadata|current(&metadata,&spec)){
        progress("检查上次中断后保留的完整候选组件",0,0);
        let reusable=crate::workspace::verify_cached_tree_with_progress(root,"manifest.json",&proof,cancel,progress);
        check_cancel(cancel)?;
        if reusable.is_ok(){progress("复用已完成并验证的 DSH（代理宿主）候选，无需重新下载或解压",1,1);return Ok(());}
    }
    // partial candidate（残缺候选）：只清理暂存解压内容，npm（包管理器）归档缓存始终保留。
    if root.exists(){crate::workspace::remove_owned(root,&context.install)?;}
    if proof.exists(){crate::workspace::remove_owned(&proof,&context.install)?;}
    fs::create_dir_all(root).map_err(|e| e.to_string())?;
    let log = root.with_extension("npm.log");
    let file = fs::File::create(&log).map_err(|e| e.to_string())?;
    let mut reader=fs::File::open(&log).map_err(|e|e.to_string())?;
    let mut status_progress=InstallProgress::default();let mut last_report=Instant::now();let mut published_phase=String::new();
    let mut command = Command::new(&node);
    hidden(&mut command);
    context.configure_command(&mut command);
    command
        .env_remove("NODE_OPTIONS")
        .env_remove("ELECTRON_RUN_AS_NODE");
    command
        .arg(program.join("scripts/dsh-npm.mjs"))
        .arg("install")
        .arg(&program)
        .arg(root)
        .arg(npm.ok_or("npm 入口缺失")?)
        .arg(&context.install)
        .stdin(Stdio::null())
        .stdout(Stdio::from(file.try_clone().map_err(|e| e.to_string())?))
        .stderr(Stdio::from(file));
    progress("正在通过 npm（包管理器）安装目标 DSH 与插件管理工具", 0, 0);
    let mut child = command.spawn().map_err(|e| e.to_string())?;
    #[cfg(windows)]
    let job = crate::windows::Job::new()?;
    #[cfg(windows)]
    {
        use std::os::windows::io::AsRawHandle;
        if let Err(error) = job.assign(child.as_raw_handle()) {
            let _ = child.kill();
            let _ = child.wait();
            return Err(error);
        }
    }
    loop {
        if cancel.load(Ordering::Acquire) {
            #[cfg(windows)]
            job.terminate();
            let _ = child.kill();
            let _ = child.wait();
            return Err("安装已取消；npm（包管理器）归档缓存及候选内容保留，重试会复用已完成内容。".into());
        }
        if last_report.elapsed()>=Duration::from_millis(350){
            status_progress.read(&mut reader)?;
            if !status_progress.phase.is_empty()&&status_progress.phase!=published_phase{
                progress(&status_progress.phase,0,0);published_phase=status_progress.phase.clone();
            }
            last_report=Instant::now();
        }
        if let Some(status) = child.try_wait().map_err(|e| e.to_string())? {
            if !status.success() {
                return Err(format!(
                    "DSH（代理宿主）安装失败，请检查网络或运行环境后重试；日志：{}",
                    log.display()
                ));
            }
            break;
        }
        std::thread::sleep(Duration::from_millis(80));
    }
    inspect(&program, root)?;
    check_cancel(cancel)?;
    progress("校验新 DSH（代理宿主）组件文件", 0, 0);
    crate::workspace::manifest_with_verified_cache(
        root,
        "dsh",
        &crate::workspace::new_id(),
        &proof,
        cancel,
        progress,
    )
}
pub fn bind(context: &LaunchContext, root: &Path) -> Result<(), String> {
    let metadata = load(&root.join("runtime.json"))?;
    atomic_json(
        &context.data.join("state/native-dsh-binding.json"),
        &json!({"source_kind":metadata["source_kind"],"adapter":metadata["adapter"],"version":metadata["version"],"manifest_sha256":file_hash(&root.join("manifest.json"))?,"fields":{"source_kind":"组件来源","adapter":"运行适配器","version":"安装版本","manifest_sha256":"已验证的运行清单摘要"}}),
    )?;
    let cache = context.install.join("cache/component-validation/dsh.json");
    if cache.is_file() {
        fs::remove_file(cache).map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn resume_candidates_are_isolated_by_the_entire_target_contract(){
        let lock:Value=serde_json::from_str(include_str!("../../../../config/dsh/runtime.lock.json")).unwrap();
        let mut spec=lock["distribution"].clone();spec["version"]=lock["version"].clone();spec["package_manager"]=lock["package_manager"].clone();
        let original=candidate_name(&spec).unwrap();assert_eq!(original,candidate_name(&spec).unwrap());
        for (key,value) in [("version",json!("9.0.0")),("package_manager",json!("pnpm@12.0.0")),("adapter",json!("future_adapter")),("registry_url",json!("https://example.com"))]{
            let mut changed=spec.clone();changed[key]=value;assert_ne!(original,candidate_name(&changed).unwrap());
        }
    }
    #[test]
    fn npm_progress_preserves_partial_lines_and_distinguishes_phases(){
        use std::io::Write;
        let path=std::env::temp_dir().join(format!("voice-npm-progress-{}",crate::workspace::new_id()));
        let mut writer=fs::File::create(&path).unwrap();let mut reader=fs::File::open(&path).unwrap();let mut state=InstallProgress::default();
        writer.write_all(b"npm http fetch GET 200 https://example.com/package 80ms\nnpm http fetch GET 200 https://example.com/a.tg").unwrap();
        state.read(&mut reader).unwrap();assert_eq!(state.metadata,1);assert_eq!(state.archives,0);
        writer.write_all(b"z 100ms\nVOICE_DSH_PHASE ").unwrap();state.read(&mut reader).unwrap();assert_eq!(state.archives,1);
        writer.write_all("核对入口\n".as_bytes()).unwrap();state.read(&mut reader).unwrap();assert_eq!(state.phase,"核对入口");
        drop(reader);drop(writer);fs::remove_file(path).unwrap();
    }
    #[test]
    fn shared_backend_and_installer_compatibility_contract() {
        let lock: Value=serde_json::from_str(include_str!("../../../../config/dsh/runtime.lock.json")).unwrap();
        let mut base=lock["distribution"].clone();base["version"]=lock["version"].clone();base["package_manager"]=lock["package_manager"].clone();
        let cases:Value=serde_json::from_str(include_str!("../../../../tests/fixtures/dsh-adapter-cases.json")).unwrap();
        for case in cases["cases"].as_array().unwrap(){
            let mut installed=base.clone();let mut spec=base.clone();
            for(key,value)in case["installed"].as_object().unwrap(){installed[key]=value.clone();}
            for(key,value)in case["target"].as_object().unwrap(){spec[key]=value.clone();}
            assert_eq!(current(&installed,&spec),case["matches"].as_bool().unwrap(),"{}",case["name"]);
        }
    }
    #[test]
    fn configured_versions_and_adapters_require_exact_compatibility() {
        let lock: Value =
            serde_json::from_str(include_str!("../../../../config/dsh/runtime.lock.json")).unwrap();
        let mut installed = lock["distribution"].clone();
        installed["version"] = lock["version"].clone();
        installed["package_manager"] = lock["package_manager"].clone();
        assert!(current(&installed, &installed));
        for version in ["0.1.0", "1.0.0", "0.2.1-alpha.1"] {
            let mut target = installed.clone();
            target["version"] = json!(version);
            assert!(!current(&installed, &target));
            assert!(current(&target, &target));
        }
        for (key, value) in [
            ("source_kind", json!("official_desktop")),
            ("adapter", json!("future_adapter")),
            ("package_manager", json!("pnpm@12.0.0")),
            ("required_files", json!([])),
        ] {
            let mut changed = installed.clone();
            changed[key] = value;
            assert!(!current(&changed, &installed));
        }
    }
    #[test]
    fn node_ranges_reject_unsupported_versions() {
        let lock: Value =
            serde_json::from_str(include_str!("../../../../config/dsh/runtime.lock.json")).unwrap();
        let ranges = lock["distribution"]["node_version_range"]
            .as_str()
            .unwrap()
            .split("||")
            .map(|value| semver::VersionReq::parse(value.trim()).unwrap())
            .collect::<Vec<_>>();
        for (version, accepted) in [
            ("22.18.0", false),
            ("22.19.0", true),
            ("23.0.0", false),
            ("24.0.0", true),
            ("26.0.0", true),
        ] {
            assert_eq!(
                ranges
                    .iter()
                    .any(|range| range.matches(&semver::Version::parse(version).unwrap())),
                accepted
            );
        }
    }
    #[test]
    fn plugin_record_must_match_host_and_protocol() {
        let root =
            std::env::temp_dir().join(format!("voice-dsh-contract-{}", crate::workspace::new_id()));
        let lock: Value =
            serde_json::from_str(include_str!("../../../../config/dsh/runtime.lock.json")).unwrap();
        atomic_json(&root.join("config/dsh/runtime.lock.json"), &lock).unwrap();
        let spec = target(&root).unwrap();
        let record = safe_path(&root, spec["plugin_record"].as_str().unwrap()).unwrap();
        let package_dir=record.parent().unwrap();
        let package_exports=json!({
            "bridge":"./lib/bridge/plugin.js",
            "wake_tasks":"./lib/wake-tasks/plugin.js",
            "wake_tasks_locale_en":"./lib/wake-tasks/locale/en.json",
            "wake_tasks_locale_zh_cn":"./lib/wake-tasks/locale/zh-CN.json"
        });
        let peer_dependencies=json!({
            "@deepseek-ai/cordis":"~4.0.5",
            "@deepseek-ai/dsh-tools":spec["version"],
            "@deepseek-ai/dsh-schedule":spec["version"]
        });
        let package_manifest=json!({
            "name":"@voice-terminal/dsh-plugin",
            "version":"0.2.1",
            "engines":{"dsh":spec["version"]},
            "peerDependencies":peer_dependencies,
            "exports":{
                ".":package_exports["bridge"],
                "./wake-tasks":package_exports["wake_tasks"],
                "./wake-tasks/locale/en.json":package_exports["wake_tasks_locale_en"],
                "./wake-tasks/locale/zh-CN.json":package_exports["wake_tasks_locale_zh_cn"]
            }
        });
        fs::create_dir_all(package_dir.join("lib/bridge")).unwrap();
        fs::create_dir_all(package_dir.join("lib/wake-tasks/locale")).unwrap();
        for relative in ["lib/bridge/plugin.js","lib/wake-tasks/plugin.js","lib/wake-tasks/locale/en.json","lib/wake-tasks/locale/zh-CN.json"] {
            fs::write(package_dir.join(relative),b"fixture").unwrap();
        }
        atomic_json(&package_dir.join("package.json"),&package_manifest).unwrap();
        atomic_json(&record,&json!({"schema":1,"dsh_version":"unadapted","bridge_protocol":spec["bridge_protocol"],"adapter":spec["adapter"]})).unwrap();
        assert!(check_plugin(&root, &spec).is_err());
        atomic_json(&record,&json!({
            "schema":2,
            "dsh_version":spec["version"],
            "bridge_protocol":spec["bridge_protocol"],
            "adapter":spec["adapter"],
            "package_name":package_manifest["name"],
            "plugin_version":package_manifest["version"],
            "package_exports":package_exports,
            "peer_dependencies":peer_dependencies
        })).unwrap();
        assert!(check_plugin(&root, &spec).is_ok());
        let mut broken=package_manifest.clone();broken["exports"]["./wake-tasks"]=json!("./lib/wake-tasks/missing.js");
        atomic_json(&package_dir.join("package.json"),&broken).unwrap();
        assert!(check_plugin(&root, &spec).is_err());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    #[ignore = "手动验收：仅在明确指定的独立测试工作区安装目标 DSH（代理宿主）"]
    fn install_test_workspace() {
        let root =
            PathBuf::from(std::env::var_os("VOICE_DSH_TEST_ROOT").expect("需要独立测试工作区"));
        let root = crate::canonical_path(&root).unwrap();
        assert!(root.starts_with(Path::new(r"D:\项目开发\测试")));
        assert!(root
            .file_name()
            .unwrap()
            .to_string_lossy()
            .starts_with("dsh-npm-"));
        let context = LaunchContext::portable(&root).unwrap();
        let result = crate::resources::download_resource(
            &context,
            "dsh",
            std::sync::Arc::new(AtomicBool::new(false)),
            &mut |phase, _, _| println!("{phase}"),
        );
        assert_eq!(result.unwrap(), vec!["dsh"]);
    }
    #[test]
    #[ignore = "手动验收：在独立工作区取消 npm（包管理器）安装进程，确认现用组件不变"]
    fn cancel_test_workspace() {
        let root=PathBuf::from(std::env::var_os("VOICE_DSH_TEST_ROOT").expect("需要独立测试工作区"));
        let root=crate::canonical_path(&root).unwrap();
        assert!(root.starts_with(Path::new(r"D:\项目开发\测试"))&&root.file_name().unwrap().to_string_lossy().starts_with("dsh-npm-"));
        let context=LaunchContext::portable(&root).unwrap();
        let installed=file_hash(&root.join("runtimes/dsh/manifest.json")).unwrap();
        let candidate=root.join("tmp/npm-cancellation-test");
        let cancel=AtomicBool::new(false);
        let result=install(&context,&candidate,&cancel,&mut|phase,_,_|{if phase.starts_with("正在通过 npm"){cancel.store(true,Ordering::Release);}});
        assert!(result.unwrap_err().contains("取消"));
        assert_eq!(file_hash(&root.join("runtimes/dsh/manifest.json")).unwrap(),installed);
        assert!(root.join("cache/npm").is_dir());
        crate::workspace::remove_owned(&candidate,&root).unwrap();
    }
    #[test]
    #[ignore="手动验收：独立工作区验证离线缓存取消重试、完整候选复用、损坏修复和启用缓存交接"]
    fn retry_interrupted_install_workspace(){
        let root=crate::canonical_path(&PathBuf::from(std::env::var_os("VOICE_DSH_TEST_ROOT").expect("需要独立测试工作区"))).unwrap();
        assert!(root.starts_with(Path::new(r"D:\项目开发\测试"))&&root.file_name().unwrap().to_string_lossy().starts_with("dsh-npm-retry-"));
        let context=LaunchContext::portable(&root).unwrap();let spec=target(&context.installed_program()).unwrap();
        let candidate=root.join("tmp/resource-downloads/dsh").join(candidate_name(&spec).unwrap());
        let old_hash=file_hash(&root.join("runtimes/dsh/manifest.json")).unwrap();
        let cancel=AtomicBool::new(false);let started=Instant::now();
        let result=install(&context,&candidate,&cancel,&mut|phase,_,_|{
            if phase.contains("归档缓存（已处理"){println!("取消时阶段：{phase}");cancel.store(true,Ordering::Release);}
        });
        assert!(result.unwrap_err().contains("取消"));let cancel_seconds=started.elapsed().as_secs_f64();
        assert!(candidate.exists());assert!(root.join("cache/npm/_cacache").is_dir());assert_eq!(file_hash(&root.join("runtimes/dsh/manifest.json")).unwrap(),old_hash);
        let started=Instant::now();let cancel=AtomicBool::new(false);let mut last_print=Instant::now();
        install(&context,&candidate,&cancel,&mut|phase,_,_|{if last_print.elapsed().as_secs()>=2{println!("{phase}");last_print=Instant::now();}}).unwrap();
        let retry_seconds=started.elapsed().as_secs_f64();let log=fs::read_to_string(candidate.with_extension("npm.log")).unwrap();assert!(!log.contains("(cache miss)"));
        let before_log=file_hash(&candidate.with_extension("npm.log")).unwrap();let before_manifest=file_hash(&candidate.join("manifest.json")).unwrap();let started=Instant::now();
        let mut reused=false;install(&context,&candidate,&cancel,&mut|phase,_,_|reused|=phase.starts_with("复用已完成")).unwrap();
        let candidate_reuse_seconds=started.elapsed().as_secs_f64();assert!(reused);assert_eq!(file_hash(&candidate.with_extension("npm.log")).unwrap(),before_log);assert_eq!(file_hash(&candidate.join("manifest.json")).unwrap(),before_manifest);
        let damaged=safe_path(&candidate,spec["required_files"][0].as_str().unwrap()).unwrap();fs::write(&damaged,b"damaged").unwrap();let started=Instant::now();
        install(&context,&candidate,&cancel,&mut|_,_,_|{}).unwrap();let repair_seconds=started.elapsed().as_secs_f64();assert_ne!(fs::read(damaged).unwrap(),b"damaged");
        crate::resources::activate(&context,&candidate,"runtimes/dsh").unwrap();
        let installed=root.join("runtimes/dsh");let cache=root.join("cache/component-validation/dsh.json");let started=Instant::now();let mut startup_hashed=false;
        crate::workspace::verify_cached_tree_with_progress(&installed,"manifest.json",&cache,&cancel,&mut|phase,_,_|startup_hashed|=phase=="校验组件文件内容").unwrap();
        let startup_seconds=started.elapsed().as_secs_f64();assert!(!startup_hashed);
        atomic_json(&root.join("retry-acceptance.json"),&json!({"ok":true,"workspace":root,"version":spec["version"],"cancel_seconds":cancel_seconds,"retry_seconds":retry_seconds,"candidate_reuse_seconds":candidate_reuse_seconds,"repair_seconds":repair_seconds,"startup_seconds":startup_seconds,"offline_retry":true,"candidate_reused":reused,"startup_rehashed":startup_hashed,
            "fields":{"ok":"实际验收通过","workspace":"独立测试工作区","version":"目标宿主版本","cancel_seconds":"中断前执行秒数","retry_seconds":"离线缓存重试及完整校验秒数","candidate_reuse_seconds":"复用完整候选秒数","repair_seconds":"损坏候选重新从缓存解压及校验秒数","startup_seconds":"启用后启动校验秒数","offline_retry":"禁止联网时完成缓存重试","candidate_reused":"完整候选未重新运行包安装","startup_rehashed":"启动时是否再次完整读取组件内容"}})).unwrap();
    }
}
