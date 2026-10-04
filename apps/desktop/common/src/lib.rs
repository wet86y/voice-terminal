//! LaunchContext（启动上下文）：根入口和桌面程序共享同一份路径解析。
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    fs,
    path::{Component, Path, PathBuf},
};

#[derive(Debug, Deserialize, Serialize, Clone)]
pub struct Selection {
    pub schema: u32,                      // schema（本地记录格式）。
    pub app: String,                      // app（程序包的相对目录）。
    pub python: String,                   // python（解释器的相对路径）。
    pub models: String,                   // models（模型包的相对目录）。
    pub webview2: Option<String>,         // webview2（固定网页运行时的相对目录）。
    pub dsh: Option<String>,              // dsh（代理宿主发行目录）。
    pub node: Option<String>,             // node（宿主使用的独立运行环境）。
    pub external_python: Option<PathBuf>, // external_python（手动指定外部解释器的退路）。
    pub external_models: Option<PathBuf>, // external_models（手动指定外部模型的退路）。
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
    pub install: PathBuf,          // install（应用解压根目录）。
    pub program: PathBuf,          // program（当前程序包／源码目录）。
    pub data: PathBuf,             // data（独立用户数据目录）。
    pub python: PathBuf,           // python（明确的后端解释器）。
    pub models: Option<PathBuf>,   // models（只读模型目录）。
    pub webview2: Option<PathBuf>, // webview2（固定网页渲染引擎）。
    pub dsh: Option<PathBuf>,      // dsh（代理宿主的完整运行产物）。
    pub node: Option<PathBuf>,     // node（可选的独立宿主解释器）。
    pub dev: bool,                 // dev（前端开发服务器模式）。
    pub diagnostic: bool,          // diagnostic（不启动业务的入口诊断模式）。
}

fn canonical_path(path: &Path) -> Result<PathBuf, String> {
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
        let root_text = install.to_string_lossy().to_ascii_lowercase();
        if root_text.starts_with(r"\\?\unc\")
            || (root_text.starts_with(r"\\") && !root_text.starts_with(r"\\?\"))
        {
            return Err("请先将应用解压到本地磁盘；不支持网络共享路径。".into());
        }
        let portable: Portable = read_json(&install.join("portable.json"))?;
        let data = if portable.data_dir.is_absolute() {
            portable.data_dir
        } else {
            install.join(portable.data_dir)
        };
        fs::create_dir_all(&data).map_err(|e| format!("用户数据目录不可用：{e}"))?;
        let data = canonical_path(&data)?;
        let active = data_state(&data)?.join("active.json");
        reject_reparse(&active)?;
        let selection: Selection = if active.is_file() {
            read_json(&active)?
        } else {
            portable.selection
        };
        if selection.schema != 1 {
            return Err("不支持的本地启用记录格式。".into());
        }
        let initial_selection = selection.clone();
        let program = component(&install, &selection.app)?;
        if data.starts_with(&program) || program.starts_with(&data) {
            return Err("用户数据目录不能与程序包重叠。".into());
        }
        let python = match selection.external_python {
            Some(p) if p.is_absolute() && p.is_file() => canonical_path(&p)?,
            Some(_) => return Err("外部解释器必须指定存在的绝对路径。".into()),
            None => component(&install, &selection.python)?,
        };
        let models = Some(match selection.external_models {
            Some(p) if p.is_absolute() && p.is_dir() => canonical_path(&p)?,
            Some(_) => return Err("外部模型必须指定存在的绝对目录。".into()),
            None => component(&install, &selection.models)?,
        });
        let optional = |value: Option<String>| value.map(|p| component(&install, &p)).transpose();
        let webview2 = optional(selection.webview2)?;
        let dsh = optional(selection.dsh)?;
        let node = optional(selection.node)?;
        for component_path in [
            &Some(program.clone()),
            &Some(python.clone()),
            &models,
            &webview2,
            &dsh,
            &node,
        ]
        .into_iter()
        .flatten()
        {
            if data.starts_with(component_path) || component_path.starts_with(&data) {
                return Err("用户数据不能与程序、运行环境或模型目录重叠。".into());
            }
        }
        let context = Self {
            install,
            program,
            data,
            python,
            models,
            webview2,
            dsh,
            node,
            dev: false,
            diagnostic: false,
        };
        context.validate()?;
        if !active.is_file() {
            fs::create_dir_all(active.parent().unwrap()).map_err(|e| e.to_string())?;
            let temporary = active.with_extension(format!("new-{}", std::process::id()));
            let mut file = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&temporary)
                .map_err(|e| e.to_string())?;
            use std::io::Write;
            file.write_all(
                &serde_json::to_vec_pretty(&initial_selection).map_err(|e| e.to_string())?,
            )
            .map_err(|e| e.to_string())?;
            file.sync_all().map_err(|e| e.to_string())?;
            drop(file);
            match fs::rename(&temporary, &active) {
                Ok(()) => {}
                Err(_) if active.is_file() => {
                    let _ = fs::remove_file(temporary);
                }
                Err(error) => return Err(format!("无法写入本地启用记录：{error}")),
            }
        }
        Ok(context)
    }

    pub fn from_args() -> Result<Self, String> {
        let args: Vec<String> = std::env::args().skip(1).collect();
        let value = |key: &str| {
            args.iter()
                .position(|a| a == key)
                .and_then(|i| args.get(i + 1))
                .cloned()
        };
        if let Some(root) = value("--root") {
            let program = canonical_path(Path::new(&root))?;
            let data = value("--data-dir")
                .map(|p| program.join(p))
                .unwrap_or_else(|| program.join(".runtime/desktop-data"));
            fs::create_dir_all(&data).map_err(|e| e.to_string())?;
            let python = if let Some(p) = value("--python") {
                program.join(p)
            } else {
                let activation: serde_json::Value = serde_json::from_slice(
                    &fs::read(program.join(".runtime/application/runtime.json")).map_err(|e| {
                        format!("请先准备业务环境或用 --python（解释器路径）明确指定：{e}")
                    })?,
                )
                .map_err(|e| e.to_string())?;
                program.join(
                    activation["python"]
                        .as_str()
                        .ok_or("业务环境记录缺少解释器路径")?,
                )
            };
            let models = value("--models")
                .map(|p| canonical_path(&program.join(p)))
                .transpose()?;
            let webview2 = value("--webview2")
                .map(|p| canonical_path(&program.join(p)))
                .transpose()?;
            let context = Self {
                install: program.clone(),
                program,
                data: canonical_path(&data)?,
                python: canonical_path(&python)?,
                models,
                webview2,
                dsh: None,
                node: None,
                dev: args.iter().any(|a| a == "--dev"),
                diagnostic: args.iter().any(|a| a == "--diagnostic"),
            };
            context.validate()?;
            data_state(&context.data)?;
            return Ok(context);
        }
        let exe = std::env::current_exe().map_err(|e| e.to_string())?;
        let mut root = exe.parent().ok_or("无法定位启动程序")?;
        while !root.join("portable.json").is_file() {
            root = root.parent().ok_or("缺少 portable.json（便携配置）")?;
        }
        let mut context = Self::portable(root)?;
        context.diagnostic = args.iter().any(|a| a == "--diagnostic");
        Ok(context)
    }

    fn validate(&self) -> Result<(), String> {
        if self.data == self.program || self.program.starts_with(&self.data) {
            return Err("用户数据位置不能包含程序包。".into());
        }
        for path in [
            &self.python,
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
        values.insert(
            "PYTHONPYCACHEPREFIX".into(),
            self.data
                .join("cache/python")
                .to_string_lossy()
                .into_owned(),
        );
        values
    }

    #[cfg(windows)]
    pub fn prepare_webview_access(&self) -> Result<(), String> {
        let Some(engine) = &self.webview2 else {
            return Ok(());
        };
        let marker = data_state(&self.data)?.join("webview2-access.json");
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
            serde_json::to_vec(&serde_json::json!({"schema":1,"path":engine}))
                .map_err(|e| e.to_string())?,
        )
        .map_err(|e| e.to_string())?;
        Ok(())
    }
}

#[cfg(windows)]
pub mod windows {
    use std::{ffi::OsStr, os::windows::ffi::OsStrExt, path::Path, ptr};
    use windows_sys::Win32::{
        Foundation::{CloseHandle, HANDLE},
        System::JobObjects::*,
        UI::{
            Shell::ShellExecuteW,
            WindowsAndMessaging::{MessageBoxW, MB_ICONERROR, MB_OK, SW_SHOWNORMAL},
        },
    };
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
        let result = unsafe {
            ShellExecuteW(
                ptr::null_mut(),
                wide("open").as_ptr(),
                wide(path).as_ptr(),
                ptr::null(),
                ptr::null(),
                SW_SHOWNORMAL,
            )
        } as isize;
        if result <= 32 {
            Err(format!("无法打开目录，系统错误码：{result}"))
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
}

#[cfg(test)]
mod tests {
    use super::*;
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
