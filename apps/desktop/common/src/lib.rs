//! LaunchContext（启动上下文）：根入口和桌面程序共享同一份路径解析。
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    fs,
    path::{Component, Path, PathBuf},
};

pub mod download;
pub mod native_ui;
pub mod workspace;
pub mod embedded; // embedded（内嵌本体）：首次展开不需要网络或 Python。
pub mod runtime; // runtime（私有语音环境）：安装计划、校验及事务启用。
pub mod resources; // resources（原生资源管理）：不依赖业务后端。
pub mod webview; // webview（系统网页引擎）：Evergreen（自动更新版）检测与引导。

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum LaunchChannel {
    Development, // Development（开发）：仅使用项目内资源和数据。
    Distribution, // Distribution（分发）：通过用户工作区定位记录启动。
}

impl LaunchChannel {
    pub fn current() -> Self {
        if cfg!(feature = "distribution") { Self::Distribution } else { Self::Development }
    }
    pub fn label(self) -> &'static str {
        match self { Self::Development => "development", Self::Distribution => "distribution" }
    }
}

#[derive(Debug, Deserialize, Serialize, Clone)]
pub struct Selection {
    pub schema: u32,              // schema（本地记录格式）。
    pub app: String,              // app（程序包的相对目录）。
    pub python: String,           // python（解释器的相对路径）。
    pub models: String,           // models（模型包的相对目录）。
    pub webview2: Option<String>, // webview2（固定网页运行时的相对目录）。
    pub dsh: Option<String>,      // dsh（代理宿主发行目录）。
}

#[derive(Deserialize)]
struct Portable {
    data_dir: PathBuf, // data_dir（用户数据位置，允许相对路径或明确绝对路径）。
    #[serde(flatten)]
    selection: Selection,
}

fn read_json<T: serde::de::DeserializeOwned>(path: &Path) -> Result<T, String> {
    let bytes = fs::read(path).map_err(|error| format!("无法读取 {}：{error}", path.display()))?;
    let payload = bytes.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(&bytes);
    serde_json::from_slice(payload).map_err(|error| format!("{} 格式错误：{error}", path.display()))
}

#[derive(Debug, Clone)]
pub struct LaunchContext {
    pub channel: LaunchChannel, // channel（启动渠道）：与调试／优化构建级别分离。
    pub workspace_id: String, // workspace_id（工作区稳定编号）。
    pub install: PathBuf,          // install（应用解压根目录）。
    pub program: PathBuf,          // program（当前程序包／源码目录）。
    pub data: PathBuf,             // data（独立用户数据目录）。
    pub python: PathBuf,           // python（明确的后端解释器）。
    pub models: Option<PathBuf>,   // models（只读模型目录）。
    pub webview2: Option<PathBuf>, // webview2（固定网页渲染引擎）。
    pub dsh: Option<PathBuf>,      // dsh（代理宿主的完整运行产物）。
    pub node: Option<PathBuf>,     // node（宿主清单内的脚本解释器）。
    pub dev: bool,                 // dev（前端开发服务器模式）。
    pub diagnostic: bool,          // diagnostic（不启动业务的入口诊断模式）。
}

pub fn canonical_path(path: &Path) -> Result<PathBuf, String> {
    let resolved = path
        .canonicalize()
        .map_err(|e| format!("路径不可用：{}（{e}）", path.display()))?;
    // canonicalize（规范化）在 Windows（操作系统）返回扩展路径前缀；
    // Node.js（宿主解释器）和部分模型库无法处理这种拼写。保留 Unicode（统一字符编码）绝对路径。
    #[cfg(windows)]
    {
        let text = resolved.to_str().ok_or("路径包含不能表示的字符")?;
        if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
            return Ok(PathBuf::from(format!(r"\\{rest}")));
        }
        if let Some(rest) = text.strip_prefix(r"\\?\") {
            return Ok(PathBuf::from(rest));
        }
    }
    Ok(resolved)
}

pub fn component(root: &Path, relative: &str) -> Result<PathBuf, String> {
    let path = Path::new(relative);
    if relative.is_empty()
        || path
            .components()
            .any(|c| !matches!(c, Component::Normal(_)))
    {
        return Err(format!("组件路径必须是应用内的相对路径：{relative}"));
    }
    let resolved = canonical_path(&root.join(path))?;
    if !resolved.starts_with(root) {
        return Err(format!("组件联接指向应用目录之外：{relative}"));
    }
    Ok(resolved)
}

fn reject_reparse(path: &Path) -> Result<(), String> {
    if let Ok(metadata) = fs::symlink_metadata(path) {
        #[cfg(windows)]
        {
            use std::os::windows::fs::MetadataExt;
            if metadata.file_attributes() & 0x400 != 0 {
                return Err(format!(
                    "状态路径不能使用目录联接或符号链接：{}",
                    path.display()
                ));
            }
        }
        if metadata.file_type().is_symlink() {
            return Err(format!("状态路径不能使用符号链接：{}", path.display()));
        }
    }
    Ok(())
}

fn data_state(data: &Path) -> Result<PathBuf, String> {
    let state = data.join("state");
    reject_reparse(&state)?;
    fs::create_dir_all(&state).map_err(|e| format!("用户状态目录不可用：{e}"))?;
    let resolved = canonical_path(&state)?;
    if !resolved.starts_with(data) {
        return Err("用户状态目录不能指向数据根目录之外。".into());
    }
    Ok(resolved)
}

impl LaunchContext {
    pub fn portable(root: &Path) -> Result<Self, String> {
        let install = canonical_path(root)?;
        if install.join("tmp/update-transaction.json").exists() {
            return Err("存在未完成更新；先执行 npm run desktop:publish（覆盖发布）恢复或收尾，再启动应用。".into());
        }
        let root_text = install.to_string_lossy().to_ascii_lowercase();
        if root_text.starts_with(r"\\?\unc\")
            || (root_text.starts_with(r"\\") && !root_text.starts_with(r"\\?\"))
        {
            return Err("请先将应用解压到本地磁盘；不支持网络共享路径。".into());
        }
        let portable: Portable = read_json(&install.join("portable.json"))?;
        let selection = portable.selection;
        if selection.schema != 2
            || portable.data_dir != PathBuf::from("data")
            || selection.app != "app"
            || selection.python != "runtimes/python/python.exe"
            || selection.models != "models"
            || selection.dsh.as_deref() != Some("runtimes/dsh")
            || !matches!(selection.webview2.as_deref(), None | Some("runtimes/webview2"))
        {
            return Err("需要 schema=2（第二版）固定便携配置，请先执行布局迁移。".into());
        }
        let data = install.join("data");
        reject_reparse(&data)?;
        fs::create_dir_all(&data).map_err(|e| e.to_string())?;
        let data = canonical_path(&data)?;
        data_state(&data)?;
        let program = component(&install, &selection.app)?;
        let python = workspace::safe_path(&install, &selection.python)?;
        let models = Some(install.join(&selection.models));
        let webview2 = if LaunchChannel::current() == LaunchChannel::Development {
            selection.webview2.as_deref().and_then(|path| component(&install, path).ok())
        } else { None }; // 分发始终使用系统 Evergreen（自动更新网页引擎）。
        let runtime = install.join(selection.dsh.as_ref().unwrap());
        let node = if runtime.join("runtime.json").is_file() {
            let metadata: serde_json::Value = read_json(&runtime.join("runtime.json"))?;
            metadata["node"].as_str().or(metadata["executable"].as_str()).and_then(|relative| component(&runtime, relative).ok())
        } else { None };
        let identity: serde_json::Value = read_json(&install.join("portable.json"))?;
        let context = Self {
            channel: LaunchChannel::current(),
            workspace_id: identity["workspace_id"].as_str().unwrap_or("development").to_string(),
            install,
            program,
            data,
            python,
            models,
            webview2,
            dsh: Some(runtime),
            node,
            dev: false,
            diagnostic: false,
        };
        context.validate()?;
        Ok(context)
    }

    pub fn from_args() -> Result<Self, String> {
        let args: Vec<String> = std::env::args().skip(1).collect();
        Self::from_argument_list(&args)
    }

    // from_argument_list（解析启动参数）：开发固定使用项目资源，分发只使用定位记录。
    fn from_argument_list(args: &[String]) -> Result<Self, String> {
        let value = |key: &str| {
            args.iter()
                .position(|a| a == key)
                .and_then(|i| args.get(i + 1))
                .cloned()
        };
        if LaunchChannel::current() == LaunchChannel::Distribution {
            if args.iter().any(|arg| ["--root", "--install", "--data-dir", "--python", "--models", "--webview2", "--dev", "--diagnostic", "--ui-preview"].contains(&arg.as_str())) {
                return Err("分发版本不接受开发路径或调试启动参数。".into());
            }
            let locator = workspace::Locator::from_args(args)?;
            let root = if let Some(journal)=value("--workspace-handoff"){locator.handoff_root(Path::new(&journal))?}else{locator.resolve()?};
            let mut context = Self::portable(&root)?;
            context.channel = LaunchChannel::Distribution;
            return Ok(context);
        }
        if let Some(root) = value("--root") {
            let program = canonical_path(Path::new(&root))?;
            if !program.join("pyproject.toml").is_file() || !program.join("apps/desktop/Cargo.toml").is_file() {
                return Err("开发入口必须明确传入当前项目根目录。".into());
            }
            if value("--install").is_some() || value("--data-dir").is_some() {
                return Err("开发渠道的数据与组件固定保存在项目 .local（本机资源），不接受外部路径。".into());
            }
            let install = program.join(".local");
            let installed = Self::portable(&install)?;
            if value("--python").is_some()
                || value("--models").is_some()
                || value("--webview2").is_some()
            {
                return Err("业务组件路径统一由 portable.json（便携配置）管理。".into());
            }
            let data = installed.data.clone();
            fs::create_dir_all(&data).map_err(|e| e.to_string())?;
            let python = installed.python.clone();
            let models = installed.models.clone();
            let webview2 = installed.webview2.clone();
            let context = Self {
                channel: LaunchChannel::Development,
                workspace_id: installed.workspace_id.clone(),
                install: installed.install.clone(),
                program,
                data: canonical_path(&data)?,
                python,
                models,
                webview2,
                dsh: installed.dsh.clone(),
                node: installed.node.clone(),
                dev: args.iter().any(|a| a == "--dev"),
                diagnostic: args.iter().any(|a| a == "--diagnostic"),
            };
            context.validate()?;
            data_state(&context.data)?;
            return Ok(context);
        }
        let exe = std::env::current_exe().map_err(|e| e.to_string())?;
        let mut root = exe.parent().ok_or("无法定位启动程序")?;
        while !(root.join("pyproject.toml").is_file() && root.join("apps/desktop/Cargo.toml").is_file()) {
            root = root.parent().ok_or("开发版本无法定位项目根目录，请从项目开发入口启动。")?;
        }
        let mut explicit=args.to_vec();explicit.push("--root".into());explicit.push(root.to_string_lossy().into_owned());
        Self::from_argument_list(&explicit)
    }

    fn validate(&self) -> Result<(), String> {
        if self.data == self.program || self.program.starts_with(&self.data) {
            return Err("用户数据位置不能包含程序包。".into());
        }
        for path in [
            &self.program.join("config/default.yaml"),
            &self.program.join("scripts/backend-entry.py"),
        ] {
            if !path.is_file() {
                return Err(format!("启动必需文件缺失：{}", path.display()));
            }
        }
        if let Some(engine) = &self.webview2 {
            if !engine.join("msedgewebview2.exe").is_file() {
                return Err("固定网页运行时缺少 msedgewebview2.exe。".into());
            }
        }
        let check = self
            .data
            .join(format!(".write-check-{}", std::process::id()));
        let owned_check = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&check)
            .map_err(|e| format!("用户数据目录不可写：{e}"))?;
        drop(owned_check);
        fs::remove_file(check).map_err(|e| e.to_string())?;
        Ok(())
    }

    pub fn environment(&self) -> BTreeMap<String, String> {
        let mut values = BTreeMap::new();
        values.insert("VOICE_LAUNCH_CHANNEL".into(), self.channel.label().into());
        values.insert("VOICE_WORKSPACE_ID".into(), self.workspace_id.clone());
        for (name, path) in [
            ("VOICE_INSTALL_ROOT", &self.install),
            ("VOICE_PROGRAM_ROOT", &self.program),
            ("VOICE_DATA_DIR", &self.data),
        ] {
            values.insert(name.into(), path.to_string_lossy().into_owned());
        }
        for (name, path) in [
            ("VOICE_MODEL_DIR", &self.models),
            ("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", &self.webview2),
            ("VOICE_DSH_RUNTIME", &self.dsh),
            ("VOICE_NODE_EXECUTABLE", &self.node),
        ] {
            if let Some(path) = path {
                values.insert(name.into(), path.to_string_lossy().into_owned());
            }
        }
        values.insert("PYTHONUTF8".into(), "1".into());
        values.insert("PYTHONNOUSERSITE".into(), "1".into());
        values.insert("VOICE_RESOURCE_POLICY".into(),self.data.join("config/runtime-resources.json").to_string_lossy().into_owned());
        for (name, folder) in [("TEMP", "tmp/process"), ("TMP", "tmp/process"),
            ("HF_HOME", "cache/huggingface"), ("MODELSCOPE_CACHE", "cache/modelscope"),
            ("TORCH_HOME", "cache/torch"), ("PIP_CACHE_DIR", "cache/pip"),
            ("CUDA_CACHE_PATH", "cache/cuda"), ("TORCH_EXTENSIONS_DIR", "cache/torch/extensions"),
            ("MPLCONFIGDIR", "cache/matplotlib"), ("XDG_CACHE_HOME", "cache"),
            ("PIP_CONFIG_FILE", "data/config/pip.ini"),
            ("npm_config_cache", "cache/npm"), ("npm_config_store_dir", "cache/pnpm/store"),
            ("npm_config_userconfig", "data/config/npmrc")] {
            values.insert(name.into(), self.install.join(folder).to_string_lossy().into_owned());
        }
        values.insert(
            "PYTHONPYCACHEPREFIX".into(),
            self.install
                .join("cache/python")
                .to_string_lossy()
                .into_owned(),
        );
        values
    }

    /// configure_command（子进程环境）：消除开发终端遗留路径，再应用已解析的上下文。
    pub fn configure_command(&self, command: &mut std::process::Command) {
        for (name, _) in std::env::vars_os() {
            let key = name.to_string_lossy().to_ascii_uppercase();
            if key.starts_with("VOICE_") || ["PYTHONPATH", "PYTHONHOME", "WEBVIEW2_USER_DATA_FOLDER",
                "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", "PYTHONPYCACHEPREFIX"].contains(&key.as_str()) {
                command.env_remove(name);
            }
        }
        command.envs(self.environment());
    }

    #[cfg(windows)]
    pub fn prepare_webview_access(&self) -> Result<(), String> {
        prepare_webview_access_paths(&self.data, self.webview2.as_deref())
    }
}

#[cfg(windows)]
pub fn prepare_webview_access_paths(data: &Path, engine: Option<&Path>) -> Result<(), String> {
    let Some(engine) = engine else {
        return Ok(());
    };
    let marker = data_state(data)?.join("webview2-access.json");
    reject_reparse(&marker)?;
    if let Ok(bytes) = fs::read(&marker) {
        if let Ok(record) = serde_json::from_slice::<serde_json::Value>(&bytes) {
            if record["path"].as_str() == engine.to_str() {
                return Ok(());
            }
        }
    }
    use std::os::windows::process::CommandExt;
    let system = std::env::var_os("SystemRoot").ok_or("无法定位系统权限工具")?;
    let tool = PathBuf::from(system).join("System32/icacls.exe");
    for identity in ["*S-1-15-2-1:(OI)(CI)(RX)", "*S-1-15-2-2:(OI)(CI)(RX)"] {
        let result = std::process::Command::new(&tool)
            .arg(engine)
            .args(["/grant", identity, "/T", "/Q"])
            .creation_flags(0x08000000)
            .output()
            .map_err(|e| format!("固定网页运行时的读取／执行权限准备失败：{e}"))?;
        if !result.status.success() {
            return Err("固定网页运行时权限准备失败。请将应用移到当前账户可写的本地目录，或在停机后将便携配置及本地启用记录的 webview2（固定引擎）设为空并使用系统运行时。".into());
        }
    }
    fs::create_dir_all(marker.parent().unwrap()).map_err(|e| e.to_string())?;
    fs::write(
        marker,
        serde_json::to_vec(&serde_json::json!({"schema":2,"path":engine}))
            .map_err(|e| e.to_string())?,
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

#[cfg(windows)]
pub mod windows {
    use std::{
        env,
        ffi::{c_void, OsStr, OsString},
        io::{Read, Write},
        os::windows::ffi::{OsStrExt, OsStringExt},
        path::{Path, PathBuf},
        process::{Command, Stdio},
        ptr,
    };
    use windows_sys::Win32::{
        Foundation::{CloseHandle, HANDLE},
        System::{Com::CoTaskMemFree, JobObjects::*},
        UI::{
            Shell::{
                ShellExecuteW, SHGetKnownFolderPath, FOLDERID_LocalAppData, KF_FLAG_DEFAULT,
            },
            WindowsAndMessaging::{MessageBoxW, MB_ICONERROR, MB_OK, SW_SHOWNORMAL},
        },
    };

    const URL_HELPER_SWITCH: &str = "--voice-external-url-helper"; // URL helper（网址辅助进程）内部开关；网址内容只通过标准输入传递。
    const MAX_HELPER_URL_UTF16_UNITS: usize = 32_767;
    const MAX_HELPER_URL_BYTES: usize = 128 * 1024;

    struct ExternalUrlLaunchPlan {
        executable: PathBuf, // executable（辅助程序路径）：当前桌面程序自身。
        arguments: Vec<OsString>, // arguments（命令行参数）：只包含固定内部开关，不包含网址或令牌。
        payload: Vec<u8>, // payload（标准输入内容）：UTF-8 编码的网址。
        environment: Vec<(OsString, OsString)>, // environment（子进程环境）：过滤工作区私有变量后的环境块。
        current_dir: PathBuf, // current_dir（当前目录）：Windows 用户临时目录，不属于工作区。
    }

    pub fn wide(value: impl AsRef<OsStr>) -> Vec<u16> {
        value.as_ref().encode_wide().chain(Some(0)).collect()
    }
    pub fn error(message: &str) {
        unsafe {
            MessageBoxW(
                ptr::null_mut(),
                wide(message).as_ptr(),
                wide("Voice Terminal（语音终端）").as_ptr(),
                MB_OK | MB_ICONERROR,
            );
        }
    }
    pub fn open(path: &Path) -> Result<(), String> {
        shell_open(path.as_os_str()).map_err(|code| format!("无法打开目录，系统错误码：{code}"))
    }
    // open_url（打开网页）：先以隔离环境启动同一桌面程序的短寿命辅助进程，再交给 Windows 默认浏览器。
    pub fn open_url(url: &str) -> Result<(), String> {
        let environment: Vec<_> = env::vars_os().collect();
        let executable = env::current_exe().map_err(|error| format!("无法定位网址辅助进程：{error}"))?;
        let current_dir = safe_external_temp_directory(&environment)?;
        let plan = external_url_launch_plan(url, executable, current_dir, &environment)?;

        use std::os::windows::process::CommandExt;
        let mut command = Command::new(&plan.executable);
        command
            .args(&plan.arguments)
            .env_clear()
            .envs(plan.environment.iter().cloned())
            .current_dir(&plan.current_dir)
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .creation_flags(0x08000000); // CREATE_NO_WINDOW（不创建控制台窗口）。
        let mut child = command
            .spawn()
            .map_err(|error| format!("无法启动隔离的网址辅助进程：{error}"))?;
        let write_result = match child.stdin.take() {
            Some(mut input) => input.write_all(&plan.payload),
            None => Err(std::io::Error::new(
                std::io::ErrorKind::Other,
                "网址辅助进程标准输入不可用",
            )),
        };
        if let Err(error) = write_result {
            let _ = child.kill();
            let _ = child.wait();
            return Err(format!("无法传递网址给系统默认浏览器：{error}"));
        }
        let status = child
            .wait()
            .map_err(|error| format!("无法等待网址辅助进程：{error}"))?;
        if status.success() {
            Ok(())
        } else {
            Err("无法打开系统默认浏览器，请检查默认浏览器设置。".into())
        }
    }

    // run_url_helper_if_requested（辅助进程入口）：在桌面初始化前读取网址并调用 Windows URL handler（网址处理程序）。
    pub fn run_url_helper_if_requested() -> Option<Result<(), String>> {
        let mut arguments = env::args_os();
        let _executable = arguments.next();
        if arguments.next().as_deref() != Some(OsStr::new(URL_HELPER_SWITCH)) {
            return None;
        }
        if arguments.next().is_some() {
            return Some(Err("网址辅助进程不接受额外命令行参数。".into()));
        }
        Some(dispatch_helper_url(&mut std::io::stdin().lock(), |url| {
            shell_open(OsStr::new(url)).map_err(|code| format!("系统 URL handler（网址处理程序）错误码：{code}"))
        }))
    }

    fn dispatch_helper_url<R, F>(reader: &mut R, open: F) -> Result<(), String>
    where
        R: Read,
        F: FnOnce(&str) -> Result<(), String>,
    {
        let mut bytes = Vec::new();
        reader
            .take((MAX_HELPER_URL_BYTES + 1) as u64)
            .read_to_end(&mut bytes)
            .map_err(|error| format!("无法读取网址辅助进程输入：{error}"))?;
        if bytes.len() > MAX_HELPER_URL_BYTES {
            return Err("网址长度超出限制。".into());
        }
        let url = String::from_utf8(bytes).map_err(|_| "网址不是有效 UTF-8 文本。".to_string())?;
        validate_web_url(&url)?;
        open(&url)
    }

    fn external_url_launch_plan(
        url: &str,
        executable: PathBuf,
        current_dir: PathBuf,
        inherited: &[(OsString, OsString)],
    ) -> Result<ExternalUrlLaunchPlan, String> {
        validate_web_url(url)?;
        Ok(ExternalUrlLaunchPlan {
            arguments: vec![OsString::from(URL_HELPER_SWITCH)],
            payload: url.as_bytes().to_vec(),
            environment: external_browser_environment(inherited, &current_dir),
            current_dir,
            executable,
        })
    }

    fn validate_web_url(url: &str) -> Result<(), String> {
        let is_web = url
            .get(..7)
            .is_some_and(|scheme| scheme.eq_ignore_ascii_case("http://"))
            || url
                .get(..8)
                .is_some_and(|scheme| scheme.eq_ignore_ascii_case("https://"));
        if !is_web || url.chars().any(char::is_control) {
            return Err("仅允许通过系统浏览器打开 HTTP 或 HTTPS 网页。".into());
        }
        if url.encode_utf16().count() > MAX_HELPER_URL_UTF16_UNITS {
            return Err("网址长度超出 Windows URL handler（网址处理程序）限制。".into());
        }
        Ok(())
    }

    fn external_browser_environment(
        inherited: &[(OsString, OsString)],
        safe_temp: &Path,
    ) -> Vec<(OsString, OsString)> {
        let workspace_roots = workspace_environment_roots(inherited);
        let mut result = Vec::with_capacity(inherited.len() + 2);
        for (name, value) in inherited {
            let key = name.to_string_lossy().to_ascii_uppercase();
            if is_private_browser_environment_key(&key) || matches!(key.as_str(), "TEMP" | "TMP") {
                continue;
            }
            if key == "PATH" {
                let retained: Vec<PathBuf> = env::split_paths(value)
                    .filter(|entry| {
                        !entry.as_os_str().is_empty()
                            && !workspace_roots.iter().any(|root| path_is_within(entry, root))
                    })
                    .collect();
                if let Ok(path) = env::join_paths(retained) {
                    result.push((name.clone(), path));
                }
            } else {
                result.push((name.clone(), value.clone()));
            }
        }
        let temp = safe_temp.as_os_str().to_owned();
        result.push((OsString::from("TEMP"), temp.clone()));
        result.push((OsString::from("TMP"), temp));
        result
    }

    fn is_private_browser_environment_key(key: &str) -> bool {
        key.starts_with("VOICE_")
            || key.starts_with("PYTHON")
            || key.starts_with("NODE_")
            || key.starts_with("NPM_CONFIG_")
            || key.starts_with("PNPM_")
            || key.starts_with("WEBVIEW2_")
            || key.starts_with("PIP_")
            || key.starts_with("HF_")
            || key.starts_with("TORCH_")
            || key.starts_with("CUDA_")
            || key.starts_with("CONDA_")
            || matches!(
                key,
                "VIRTUAL_ENV"
                    | "MODELSCOPE_CACHE"
                    | "HUGGINGFACE_HUB_CACHE"
                    | "TRANSFORMERS_CACHE"
                    | "TRITON_CACHE_DIR"
                    | "MPLCONFIGDIR"
                    | "XDG_CACHE_HOME"
                    | "TORCHINDUCTOR_CACHE_DIR"
            )
    }

    fn workspace_environment_roots(inherited: &[(OsString, OsString)]) -> Vec<PathBuf> {
        let mut roots = Vec::new();
        for (name, value) in inherited {
            let key = name.to_string_lossy().to_ascii_uppercase();
            let private_path = key.starts_with("VOICE_")
                || key.starts_with("PYTHON")
                || key.starts_with("NODE_")
                || key.starts_with("NPM_CONFIG_")
                || key.starts_with("PNPM_")
                || key.starts_with("WEBVIEW2_")
                || key.starts_with("PIP_")
                || key.starts_with("HF_")
                || key.starts_with("TORCH_")
                || key.starts_with("CUDA_")
                || matches!(key.as_str(), "TEMP" | "TMP" | "MODELSCOPE_CACHE" | "MPLCONFIGDIR" | "XDG_CACHE_HOME");
            if !private_path {
                continue;
            }
            let path = PathBuf::from(value);
            if !path.is_absolute() {
                continue;
            }
            roots.push(path.clone());
            if let Ok(resolved) = path.canonicalize() {
                roots.push(resolved);
            }
            if key.ends_with("EXECUTABLE") || path.extension().is_some() {
                if let Some(parent) = path.parent() {
                    roots.push(parent.to_path_buf());
                    if let Ok(resolved) = parent.canonicalize() {
                        roots.push(resolved);
                    }
                }
            }
        }
        roots
    }

    fn path_is_within(path: &Path, root: &Path) -> bool {
        let path = normalize_windows_path(path.as_os_str());
        let root = normalize_windows_path(root.as_os_str());
        path == root
            || path
                .strip_prefix(&root)
                .is_some_and(|suffix| suffix.starts_with('\\'))
    }

    fn normalize_windows_path(path: &OsStr) -> String {
        let mut value = path.to_string_lossy().replace('/', "\\");
        if let Some(rest) = value.strip_prefix(r"\\?\") {
            value = rest.to_string();
        }
        while value.ends_with('\\') && value.len() > 3 {
            value.pop();
        }
        value.to_ascii_uppercase()
    }

    fn safe_external_temp_directory(inherited: &[(OsString, OsString)]) -> Result<PathBuf, String> {
        let local_app_data = std::thread::spawn(known_folder_local_app_data)
            .join()
            .map_err(|_| "无法查询 Windows 用户 LocalAppData（本地应用数据目录）。".to_string())??;
        let temp = local_app_data.join("Temp");
        if !temp.is_dir() {
            return Err("Windows 用户 LocalAppData（本地应用数据目录）缺少 Temp（临时目录）。".into());
        }
        let resolved_temp = temp
            .canonicalize()
            .map_err(|error| format!("无法核对 Windows 用户临时目录：{error}"))?;
        if workspace_identity_roots(inherited)
            .iter()
            .any(|root| path_is_within(&temp, root) || path_is_within(&resolved_temp, root))
        {
            return Err("Windows 用户临时目录落在应用工作区内，无法安全打开外部网页。".into());
        }
        Ok(temp)
    }

    fn workspace_identity_roots(inherited: &[(OsString, OsString)]) -> Vec<PathBuf> {
        let mut roots = Vec::new();
        for (name, value) in inherited {
            let key = name.to_string_lossy().to_ascii_uppercase();
            if !matches!(key.as_str(), "VOICE_INSTALL_ROOT" | "VOICE_PROGRAM_ROOT" | "VOICE_DATA_DIR") {
                continue;
            }
            let path = PathBuf::from(value);
            if !path.is_absolute() {
                continue;
            }
            roots.push(path.clone());
            if let Ok(resolved) = path.canonicalize() {
                roots.push(resolved);
            }
        }
        roots
    }

    fn known_folder_local_app_data() -> Result<PathBuf, String> {
        // CoInitializeEx（COM 组件对象模型初始化）在专用线程运行，避免与桌面 UI 线程的 COM 模式冲突。
        let initialized = unsafe { windows_sys::Win32::System::Com::CoInitializeEx(ptr::null(), windows_sys::Win32::System::Com::COINIT_APARTMENTTHREADED as u32) };
        if initialized < 0 {
            return Err(format!("无法初始化 Windows known-folder（已知目录）查询：{initialized}"));
        }
        let mut path = ptr::null_mut();
        let result = unsafe {
            SHGetKnownFolderPath(
                &FOLDERID_LocalAppData,
                KF_FLAG_DEFAULT as u32,
                ptr::null_mut(),
                &mut path,
            )
        };
        let resolved = if result < 0 || path.is_null() {
            Err(format!("无法定位 Windows 用户 LocalAppData（本地应用数据目录）：{result}"))
        } else {
            let mut length = 0;
            unsafe {
                while *path.add(length) != 0 {
                    length += 1;
                }
            }
            let value = unsafe {
                let wide = std::slice::from_raw_parts(path, length);
                PathBuf::from(OsString::from_wide(wide))
            };
            if !value.is_absolute() {
                Err("Windows 返回的 LocalAppData（本地应用数据目录）不是绝对路径。".into())
            } else {
                Ok(value)
            }
        };
        if !path.is_null() {
            unsafe { CoTaskMemFree(path.cast::<c_void>()) };
        }
        unsafe { windows_sys::Win32::System::Com::CoUninitialize() };
        resolved
    }

    fn shell_open(target: &OsStr) -> Result<(), isize> {
        let result = unsafe {
            ShellExecuteW(
                ptr::null_mut(),
                wide("open").as_ptr(),
                wide(target).as_ptr(),
                ptr::null(),
                ptr::null(),
                SW_SHOWNORMAL,
            )
        } as isize;
        if result <= 32 {
            Err(result)
        } else {
            Ok(())
        }
    }
    pub struct Job(HANDLE);
    unsafe impl Send for Job {}
    unsafe impl Sync for Job {}
    impl Job {
        pub fn new() -> Result<Self, String> {
            unsafe {
                let handle = CreateJobObjectW(ptr::null(), ptr::null());
                if handle.is_null() {
                    return Err(std::io::Error::last_os_error().to_string());
                }
                let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
                limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                if SetInformationJobObject(
                    handle,
                    JobObjectExtendedLimitInformation,
                    &limits as *const _ as *const _,
                    std::mem::size_of_val(&limits) as u32,
                ) == 0
                {
                    CloseHandle(handle);
                    return Err(std::io::Error::last_os_error().to_string());
                }
                Ok(Self(handle))
            }
        }
        pub fn assign(&self, process: HANDLE) -> Result<(), String> {
            if unsafe { AssignProcessToJobObject(self.0, process) } == 0 {
                Err(std::io::Error::last_os_error().to_string())
            } else {
                Ok(())
            }
        }
        pub fn terminate(&self) {
            unsafe {
                TerminateJobObject(self.0, 1);
            }
        }
    }
    impl Drop for Job {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.0);
            }
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use std::collections::BTreeMap;
        use std::io::Cursor;

        #[test]
        fn helper_payload_keeps_unicode_spaces_and_ampersands_without_shell_parsing() {
            let url = "https://example.test/模型 文件?phrase=hello world&token=sensitive-value";
            let mut input = Cursor::new(url.as_bytes());
            let mut opened = None;
            dispatch_helper_url(&mut input, |received| {
                opened = Some(received.to_string());
                Ok(())
            })
            .unwrap();
            assert_eq!(opened.as_deref(), Some(url));
        }

        #[test]
        fn sensitive_url_is_payload_only_and_never_a_helper_argument() {
            let url = "https://127.0.0.1:43127/?session_token=do-not-put-on-command-line";
            let inherited = vec![
                (OsString::from("VOICE_INSTALL_ROOT"), OsString::from(r"D:\QA Workspace")),
                (OsString::from("PATH"), OsString::from(r"D:\QA Workspace\runtimes\python;C:\Windows\System32")),
                (OsString::from("APPDATA"), OsString::from(r"C:\Users\qa\AppData\Roaming")),
            ];
            let plan = external_url_launch_plan(
                url,
                PathBuf::from(r"D:\QA Workspace\app\VoiceTerminalDesktop.exe"),
                PathBuf::from(r"C:\Users\qa\AppData\Local\Temp"),
                &inherited,
            )
            .unwrap();
            assert_eq!(plan.arguments, [OsString::from(URL_HELPER_SWITCH)]);
            assert!(!plan.arguments.iter().any(|argument| argument.to_string_lossy().contains("session_token")));
            assert_eq!(String::from_utf8(plan.payload).unwrap(), url);
        }

        #[test]
        fn browser_environment_removes_private_paths_and_uses_known_safe_temp() {
            let safe_temp = PathBuf::from(r"C:\Users\qa\AppData\Local\Temp");
            let inherited = vec![
                (OsString::from("VOICE_LAUNCH_CHANNEL"), OsString::from("development")),
                (OsString::from("VOICE_INSTALL_ROOT"), OsString::from(r"D:\QA Workspace")),
                (OsString::from("PYTHONUTF8"), OsString::from("1")),
                (OsString::from("PYTHONHOME"), OsString::from(r"D:\QA Workspace\runtimes\python")),
                (OsString::from("NODE_OPTIONS"), OsString::from("--require private.js")),
                (OsString::from("npm_config_cache"), OsString::from(r"D:\QA Workspace\cache\npm")),
                (OsString::from("WEBVIEW2_USER_DATA_FOLDER"), OsString::from(r"D:\QA Workspace\data\webview2")),
                (OsString::from("HF_HOME"), OsString::from(r"D:\QA Workspace\cache\hf")),
                (OsString::from("TEMP"), OsString::from(r"D:\QA Workspace\tmp\process")),
                (OsString::from("TMP"), OsString::from(r"D:\QA Workspace\tmp\process")),
                (OsString::from("PATH"), OsString::from(r"D:\QA Workspace\runtimes\python;C:\Windows\System32")),
                (OsString::from("SystemRoot"), OsString::from(r"C:\Windows")),
                (OsString::from("APPDATA"), OsString::from(r"C:\Users\qa\AppData\Roaming")),
            ];
            let filtered = external_browser_environment(&inherited, &safe_temp);
            let as_map: BTreeMap<_, _> = filtered
                .iter()
                .map(|(key, value)| (key.to_string_lossy().to_ascii_uppercase(), value.to_string_lossy().into_owned()))
                .collect();
            for key in [
                "VOICE_LAUNCH_CHANNEL", "VOICE_INSTALL_ROOT", "PYTHONUTF8", "PYTHONHOME",
                "NODE_OPTIONS", "NPM_CONFIG_CACHE", "WEBVIEW2_USER_DATA_FOLDER", "HF_HOME",
            ] {
                assert!(!as_map.contains_key(key), "private key leaked: {key}");
            }
            assert_eq!(as_map.get("TEMP").map(String::as_str), Some(r"C:\Users\qa\AppData\Local\Temp"));
            assert_eq!(as_map.get("TMP").map(String::as_str), Some(r"C:\Users\qa\AppData\Local\Temp"));
            assert_eq!(as_map.get("SYSTEMROOT").map(String::as_str), Some(r"C:\Windows"));
            assert_eq!(as_map.get("APPDATA").map(String::as_str), Some(r"C:\Users\qa\AppData\Roaming"));
            let path = as_map.get("PATH").unwrap();
            assert!(!path.to_ascii_uppercase().contains("QA WORKSPACE"));
            assert!(path.contains(r"C:\Windows\System32"));
        }

        #[test]
        fn helper_rejects_non_web_urls_before_shell_dispatch() {
            let mut input = Cursor::new(b"file:///C:/sensitive.txt".as_slice());
            let mut invoked = false;
            assert!(dispatch_helper_url(&mut input, |_| {
                invoked = true;
                Ok(())
            })
            .is_err());
            assert!(!invoked);
        }

        #[test]
        fn browser_helper_uses_real_local_app_data_temp_not_workspace_temp() {
            let inherited = vec![
                (OsString::from("VOICE_INSTALL_ROOT"), OsString::from(r"D:\QA Workspace")),
                (OsString::from("TEMP"), OsString::from(r"D:\QA Workspace\tmp\process")),
                (OsString::from("TMP"), OsString::from(r"D:\QA Workspace\tmp\process")),
            ];
            let temp = safe_external_temp_directory(&inherited).unwrap();
            assert_eq!(temp.file_name(), Some(OsStr::new("Temp")));
            assert!(!path_is_within(&temp, Path::new(r"D:\QA Workspace")));
            let resolved = temp.canonicalize().unwrap();
            assert!(!path_is_within(&resolved, Path::new(r"D:\QA Workspace")));
        }

        #[test]
        fn normal_system_temp_is_not_mistaken_for_workspace_identity() {
            let local_app_data = known_folder_local_app_data().unwrap();
            let temp = local_app_data.join("Temp");
            let inherited = vec![
                (OsString::from("TEMP"), temp.as_os_str().to_owned()),
                (OsString::from("TMP"), temp.as_os_str().to_owned()),
                (OsString::from("PYTHONPYCACHEPREFIX"), temp.join("python-cache").into_os_string()),
            ];
            assert_eq!(safe_external_temp_directory(&inherited).unwrap(), temp);
        }

        #[test]
        fn explicit_workspace_root_cannot_point_to_system_temp() {
            let temp = known_folder_local_app_data().unwrap().join("Temp");
            let inherited = vec![(
                OsString::from("VOICE_INSTALL_ROOT"),
                temp.as_os_str().to_owned(),
            )];
            assert!(safe_external_temp_directory(&inherited).is_err());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pending_task_skips_terminal_marker_and_finds_new_active_migration() {
        let root = std::env::temp_dir().join(format!("voice-pending-migration-{}", workspace::new_id()));
        fs::create_dir(&root).unwrap();
        let old_target = root.join("old-target");
        let new_target = root.join("new-target");
        for path in [root.join("tmp"), root.join("data/state"), old_target.join("tmp"), new_target.join("tmp")] {
            fs::create_dir_all(path).unwrap();
        }
        workspace::atomic_json(&root.join("tmp/workspace-task.json"), &serde_json::json!({
            "target_root": old_target,
            "fields": {"target_root": "旧迁移目标"}
        })).unwrap();
        workspace::atomic_json(&old_target.join("tmp/workspace-task.json"), &serde_json::json!({
            "state": "completed",
            "helper_pid": 0,
            "fields": {"state": "事务阶段"}
        })).unwrap();
        workspace::atomic_json(&root.join("data/state/workspace-task.json"), &serde_json::json!({
            "target_root": new_target,
            "fields": {"target_root": "当前迁移目标"}
        })).unwrap();
        let new_journal = new_target.join("tmp/workspace-task.json");
        workspace::atomic_json(&new_journal, &serde_json::json!({
            "state": "copying",
            "helper_pid": 0,
            "fields": {"state": "事务阶段"}
        })).unwrap();

        assert_eq!(workspace::pending_task(&root).unwrap(), Some(new_journal));
        fs::remove_dir_all(&root).unwrap();
    }

    #[cfg(not(feature = "distribution"))] // development（开发渠道）：允许项目根目录参数。
    #[test]
    fn development_uses_project_data_and_rejects_external_override() {
        let root = std::env::temp_dir().join(format!("voice-source-data-{}", std::process::id()));
        let install = root.join(".local");
        for path in [
            root.join("config"),
            root.join("scripts"),
            root.join("apps/desktop"),
            install.join("app/config"),
            install.join("app/scripts"),
            install.join("runtimes/python"),
            install.join("models"),
            install.join("runtimes/dsh"),
            install.join("runtimes/webview2"),
            install.join("data/config"),
        ] {
            fs::create_dir_all(path).unwrap();
        }
        for path in [
            root.join("config/default.yaml"),
            root.join("scripts/backend-entry.py"),
            root.join("pyproject.toml"),
            root.join("apps/desktop/Cargo.toml"),
            install.join("app/config/default.yaml"),
            install.join("app/scripts/backend-entry.py"),
            install.join("runtimes/python/python.exe"),
            install.join("runtimes/webview2/msedgewebview2.exe"),
            install.join("runtimes/dsh/node.exe"),
        ] {
            fs::write(path, b"fixture").unwrap();
        }
        fs::write(
            install.join("data/config/preserved.txt"),
            b"existing-settings",
        )
        .unwrap();
        fs::write(
            install.join("runtimes/dsh/runtime.json"),
            b"{\"node\":\"node.exe\"}",
        )
        .unwrap();
        fs::write(install.join("portable.json"), serde_json::to_vec(&serde_json::json!({
            "data_dir":"data", "schema":2, "app":"app", "python":"runtimes/python/python.exe",
            "models":"models", "dsh":"runtimes/dsh", "webview2":"runtimes/webview2", "node":null,
            "external_python":null, "external_models":null
        })).unwrap()).unwrap();
        let root = canonical_path(&root).unwrap();
        let args = vec![
            "--root".into(),
            root.to_str().unwrap().into(),
            "--dev".into(),
        ];
        let context = LaunchContext::from_argument_list(&args).unwrap();
        assert_eq!(context.data, root.join(".local/data"));
        assert_eq!(context.program, root);
        assert_eq!(context.python, install.join("runtimes/python/python.exe"));
        assert_eq!(context.dsh, Some(install.join("runtimes/dsh")));
        assert_eq!(context.models, Some(install.join("models")));
        assert!(context.dev);
        assert!(!root.join(".local/isolated-data").exists());
        let mut explicit = args;
        explicit.extend(["--data-dir".into(), "explicit-data".into()]);
        assert!(LaunchContext::from_argument_list(&explicit).unwrap_err().contains("不接受外部路径"));
        assert!(!root.join("explicit-data").exists());
        assert_eq!(
            fs::read(install.join("data/config/preserved.txt")).unwrap(),
            b"existing-settings"
        );
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn pending_update_never_initializes_business_data() {
        let root =
            std::env::temp_dir().join(format!("voice-pending-update-{}", std::process::id()));
        fs::create_dir_all(root.join("tmp")).unwrap();
        fs::write(root.join("tmp/update-transaction.json"), b"{}").unwrap();
        let error = LaunchContext::portable(&root).unwrap_err();
        assert!(error.contains("未完成更新"));
        assert!(!root.join("data").exists());
        fs::remove_dir_all(&root).unwrap();
    }

    #[cfg(not(feature = "distribution"))] // development（开发渠道）：检查项目安装定位。
    #[test]
    fn missing_formal_installation_never_creates_an_isolated_product() {
        let root = std::env::temp_dir().join(format!("voice-no-install-{}", std::process::id()));
        fs::create_dir_all(&root).unwrap();
        let error =
            LaunchContext::from_argument_list(&["--root".into(), root.to_str().unwrap().into()])
                .unwrap_err();
        assert!(error.contains("项目根目录"));
        assert!(!root.join(".local").exists());
        fs::remove_dir_all(&root).unwrap();
    }

    #[cfg(feature = "distribution")] // distribution（分发渠道）：拒绝开发路径与调试参数。
    #[test]
    fn distribution_rejects_development_paths_before_workspace_resolution() {
        for argument in ["--root", "--install", "--data-dir", "--python", "--models", "--webview2", "--dev", "--diagnostic", "--ui-preview"] {
            let error = LaunchContext::from_argument_list(&[argument.into()]).unwrap_err();
            assert!(error.contains("分发版本不接受开发路径或调试启动参数"), "{argument}: {error}");
        }
    }
    #[test]
    fn rejects_escape_and_absolute_components() {
        for value in ["../other", "a/../../b", "/outside", "C:\\outside", ""] {
            assert!(component(Path::new("."), value).is_err());
        }
    }
    #[test]
    fn canonical_paths_are_usable_by_business_runtimes() {
        let directory = canonical_path(Path::new(".")).unwrap();
        assert!(directory.is_absolute());
        assert!(!directory.to_string_lossy().starts_with(r"\\?\"));
    }
    #[test]
    fn windows_text_editor_bom_and_invalid_state_are_handled() {
        let temporary = std::env::temp_dir();
        let fixture = temporary.join(format!("voice-desktop-common-{}", std::process::id()));
        fs::create_dir_all(&fixture).unwrap();
        let fixture = canonical_path(&fixture).unwrap();
        assert!(fixture.starts_with(canonical_path(&temporary).unwrap()));
        let json = fixture.join("portable.json");
        fs::write(&json, b"\xef\xbb\xbf{\"schema\":1}").unwrap();
        let value: serde_json::Value = read_json(&json).unwrap();
        assert_eq!(value["schema"], 1);
        fs::write(fixture.join("state"), b"not a directory").unwrap();
        assert!(data_state(&fixture).is_err());
        fs::remove_dir_all(&fixture).unwrap();
    }
}
