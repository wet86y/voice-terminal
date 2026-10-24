//! Process host（项目工作进程）：同一产品可执行文件承载独立解释器，不另起界面或复制运行环境。
use std::{path::{Path, PathBuf}, process::Command};

pub const HOST_ARGUMENT: &str = "--worker-host"; // HOST_ARGUMENT（工作进程专用入口）。
pub const APP_ID: &str = "com.voiceterminal.desktop"; // APP_ID（稳定应用身份）：与桌面程序声明一致。

/// current_host（当前产品宿主）：安装器、根入口和测试程序不冒充桌面工作进程。
pub fn current_host() -> Option<PathBuf> {
    let path = std::env::current_exe().ok()?;
    path.file_name()?.to_str()?.eq_ignore_ascii_case("VoiceTerminalDesktop.exe").then_some(path)
}

/// python_command（解释器启动入口）：只对支持的相邻运行库启用产品宿主，外部环境沿用原入口。
pub fn python_command(python: &Path) -> Command {
    command_with_host(python, current_host().as_deref())
}

fn command_with_host(python: &Path, host: Option<&Path>) -> Command {
    if let Some(host) = host.filter(|_| python.is_absolute() && python.is_file()
        && python.parent().is_some_and(|root| root.join("python312.dll").is_file())) {
        let mut command = Command::new(host);
        command.arg(HOST_ARGUMENT).arg(python);
        command
    } else {
        Command::new(python)
    }
}

#[cfg(windows)]
mod native {
    use super::*;
    use std::{ffi::OsString, os::windows::ffi::OsStrExt, ptr};
    use windows_sys::Win32::{
        Foundation::{CloseHandle, FreeLibrary, HANDLE, WAIT_OBJECT_0},
        System::{LibraryLoader::{GetProcAddress, LoadLibraryExW, LOAD_LIBRARY_SEARCH_DEFAULT_DIRS, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR},
            Threading::{CreateEventW, OpenEventW, SetEvent, WaitForSingleObject, SYNCHRONIZATION_SYNCHRONIZE}},
        UI::Shell::SetCurrentProcessExplicitAppUserModelID,
    };

    pub fn set_identity() {
        let identity: Vec<u16> = APP_ID.encode_utf16().chain(Some(0)).collect();
        // 应用外壳身份失败不阻断现有业务；它不等同于任务管理器的分组承诺。
        unsafe { SetCurrentProcessExplicitAppUserModelID(identity.as_ptr()); }
    }

    pub fn dispatch() -> Option<Result<i32, String>> {
        let mut arguments = std::env::args_os().skip(1);
        if arguments.next().as_deref() != Some(std::ffi::OsStr::new(HOST_ARGUMENT)) { return None; }
        Some(run(arguments.collect()))
    }

    // StartGate（启动门闩）：原生父进程完成作业归属后才允许工作进程加载解释器及创建后代。
    pub(crate) struct StartGate(HANDLE);
    impl StartGate {
        pub(crate) fn prepare(command: &mut Command) -> Result<Option<Self>, String> {
            if command.get_args().next() != Some(std::ffi::OsStr::new(HOST_ARGUMENT)) { return Ok(None); }
            let name = format!("Local\\VoiceTerminal.Worker.Start.{}", crate::workspace::new_id());
            let wide: Vec<u16> = name.encode_utf16().chain(Some(0)).collect();
            let event = unsafe { CreateEventW(ptr::null(), 1, 0, wide.as_ptr()) };
            if event.is_null() { return Err(format!("无法建立进程启动门闩：{}", std::io::Error::last_os_error())); }
            command.env("VOICE_PROCESS_START_GATE", name);
            Ok(Some(Self(event)))
        }
        pub(crate) fn release(&self) -> Result<(), String> {
            if unsafe { SetEvent(self.0) } == 0 {
                return Err(format!("无法释放进程启动门闩：{}", std::io::Error::last_os_error()));
            }
            Ok(())
        }
    }
    impl Drop for StartGate {
        fn drop(&mut self) { unsafe { CloseHandle(self.0); } }
    }

    fn await_start_gate() -> Result<(), String> {
        let gate = std::env::var_os("VOICE_PROCESS_START_GATE");
        // 子孙进程不能继承上一代门闩；自己的创建入口负责设置自己的门闩。
        std::env::remove_var("VOICE_PROCESS_START_GATE");
        let Some(gate) = gate else { return Ok(()); };
        let name: Vec<u16> = gate.encode_wide().chain(Some(0)).collect();
        let event = unsafe { OpenEventW(SYNCHRONIZATION_SYNCHRONIZE, 0, name.as_ptr()) };
        if event.is_null() { return Err("父进程启动门闩已失效。".into()); }
        let status = unsafe { WaitForSingleObject(event, 30_000) };
        unsafe { CloseHandle(event); }
        if status != WAIT_OBJECT_0 { return Err("等待进程所有权建立超时或失败。".into()); }
        Ok(())
    }

    fn run(arguments: Vec<OsString>) -> Result<i32, String> {
        await_start_gate()?;
        let python = arguments.first().map(PathBuf::from).ok_or("工作进程缺少解释器路径。")?;
        if !python.is_absolute() || !python.is_file() {
            return Err("工作进程必须指定现有解释器的绝对路径。".into());
        }
        let library = python.parent().unwrap().join("python312.dll");
        if !library.is_file() { return Err("工作进程缺少 Python 3.12（解释器）动态运行库。".into()); }
        let library_path: Vec<u16> = library.as_os_str().encode_wide().chain(Some(0)).collect();
        let mut wide_arguments: Vec<Vec<u16>> = arguments.iter()
            .map(|value| value.encode_wide().chain(Some(0)).collect()).collect();
        let mut pointers: Vec<*mut u16> = wide_arguments.iter_mut().map(|value| value.as_mut_ptr()).collect();
        set_identity();
        unsafe {
            // 仅从明确选择的运行库和系统安全目录解析依赖，不修改全局 DLL（动态链接库）搜索路径。
            let module = LoadLibraryExW(library_path.as_ptr(), ptr::null_mut(),
                LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
            if module.is_null() { return Err(format!("无法加载解释器运行库：{}", std::io::Error::last_os_error())); }
            let Some(entry) = GetProcAddress(module, c"Py_Main".as_ptr().cast()) else {
                FreeLibrary(module);
                return Err("解释器运行库没有 Py_Main（标准执行入口）。".into());
            };
            let main: unsafe extern "C" fn(i32, *mut *mut u16) -> i32 = std::mem::transmute(entry);
            // argv[0]（解释器路径）保持原值，保留 sys.executable（解释器身份）、隔离路径和设备环境选择。
            // 操作系统看到的真实可执行文件仍是产品宿主，不产生中转解释器进程。
            let audio = audio_identity::Monitor::start();
            let code = main(pointers.len() as i32, pointers.as_mut_ptr());
            drop(audio);
            // 解释器的原生依赖可能仍持有退出钩子；让操作系统在进程退出时统一释放运行库。
            Ok(code)
        }
    }

    mod audio_identity {
        use std::{collections::HashSet, ptr, sync::mpsc, thread, time::Duration};
        use windows::{core::{GUID, Interface, PCWSTR}, Win32::{
            Media::Audio::{IMMDeviceEnumerator, IAudioSessionControl2, IAudioSessionManager2, MMDeviceEnumerator, DEVICE_STATE_ACTIVE, eAll},
            System::Com::{CoCreateInstance, CoInitializeEx, CoTaskMemFree, CoUninitialize, CLSCTX_ALL, COINIT_MULTITHREADED},
        }};

        // GROUP（音频会话分组）：同一项目的会话使用一致的音量分组，不改变采样、设备或音量。
        const GROUP: GUID = GUID::from_u128(0x6f0c6411_e33d_4e4d_b606_077f92c33cc9);

        pub struct Monitor {
            stop: mpsc::Sender<()>, // stop（进程退出通知）。
            thread: Option<thread::JoinHandle<()>>, // thread（独立音频身份维护线程）。
        }
        impl Monitor {
            pub fn start() -> Option<Self> {
                // 仅音频服务维护自身会话；模型推理和组件安装不创建轮询线程。
                if std::env::var("VOICE_RESOURCE_ROLE").ok().as_deref() != Some("voice") { return None; }
                let icon: Vec<u16> = std::env::current_exe().ok()?.as_os_str()
                    .to_string_lossy().encode_utf16().chain(Some(0)).collect();
                let (stop, receiver) = mpsc::channel();
                let thread = thread::Builder::new().name("audio-identity".into()).spawn(move || unsafe {
                    if CoInitializeEx(None, COINIT_MULTITHREADED).is_err() { return; }
                    let name: Vec<u16> = "Voice Terminal · 语音终端".encode_utf16().chain(Some(0)).collect();
                    let mut applied = HashSet::new();
                    loop {
                        // 设备切换和音频会话重建后重新设置。只读取会话身份，不读取或改变音频内容。
                        let _ = synchronize(&name, &icon, &mut applied);
                        if receiver.recv_timeout(Duration::from_secs(1)) != Err(mpsc::RecvTimeoutError::Timeout) { break; }
                    }
                    CoUninitialize();
                }).ok()?;
                Some(Self { stop, thread: Some(thread) })
            }
        }
        impl Drop for Monitor {
            fn drop(&mut self) {
                let _ = self.stop.send(());
                if let Some(thread) = self.thread.take() { let _ = thread.join(); }
            }
        }

        unsafe fn synchronize(name: &[u16], icon: &[u16], applied: &mut HashSet<String>) -> windows::core::Result<()> {
            let enumerator: IMMDeviceEnumerator = unsafe { CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL)? };
            let devices = unsafe { enumerator.EnumAudioEndpoints(eAll, DEVICE_STATE_ACTIVE)? };
            let mut present = HashSet::new();
            for index in 0..unsafe { devices.GetCount()? } {
                let Ok(device) = (unsafe { devices.Item(index) }) else { continue; };
                let Ok(manager) = (unsafe { device.Activate::<IAudioSessionManager2>(CLSCTX_ALL, None) }) else { continue; };
                let Ok(sessions) = (unsafe { manager.GetSessionEnumerator() }) else { continue; };
                let Ok(count) = (unsafe { sessions.GetCount() }) else { continue; };
                for index in 0..count {
                    let Ok(session) = (unsafe { sessions.GetSession(index) }) else { continue; };
                    let Ok(control) = session.cast::<IAudioSessionControl2>() else { continue; };
                    if unsafe { control.GetProcessId() }.ok() != Some(std::process::id()) { continue; }
                    let Ok(identifier) = (unsafe { control.GetSessionInstanceIdentifier() }) else { continue; };
                    let key = unsafe { identifier.to_string() };
                    unsafe { CoTaskMemFree(Some(identifier.as_ptr().cast())); }
                    let Ok(key) = key else { continue; };
                    present.insert(key.clone());
                    if applied.contains(&key) { continue; }
                    if unsafe { session.SetDisplayName(PCWSTR(name.as_ptr()), ptr::null()) }.is_ok()
                        && unsafe { session.SetIconPath(PCWSTR(icon.as_ptr()), ptr::null()) }.is_ok()
                        && unsafe { session.SetGroupingParam(&GROUP, ptr::null()) }.is_ok() {
                        applied.insert(key);
                    }
                }
            }
            applied.retain(|key| present.contains(key));
            Ok(())
        }
    }
}

#[cfg(windows)] pub use native::{dispatch, set_identity};
#[cfg(windows)] pub(crate) use native::StartGate;

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unrelated_entry_keeps_original_interpreter() {
        let python = Path::new("external-python.exe");
        let command = command_with_host(python, None);
        assert_eq!(command.get_program(), python);
        assert_eq!(command.get_args().count(), 0);
    }
    #[test]
    fn incomplete_runtime_does_not_enter_embedded_host() {
        let command = command_with_host(Path::new("missing/python.exe"), Some(Path::new("VoiceTerminalDesktop.exe")));
        assert_eq!(command.get_program(), "missing/python.exe");
    }
    #[test]
    #[cfg(windows)]
    #[ignore = "原生集成验收：明确提供本轮宿主和私有解释器路径后执行"]
    fn native_host_is_in_job_before_python_runs() {
        let host = PathBuf::from(std::env::var_os("VOICE_TEST_PROCESS_HOST").expect("缺少本轮宿主路径"));
        let python = PathBuf::from(std::env::var_os("VOICE_TEST_PROCESS_PYTHON").expect("缺少测试解释器路径"));
        let job = crate::windows::Job::new().unwrap();
        for _ in 0..5 {
            for candidate in [Some(host.as_path()), None] {
            let mut command = command_with_host(&python, candidate);
            command.args(["-I", "-B", "-X", "utf8", "-c",
                "import ctypes;v=ctypes.c_int();k=ctypes.windll.kernel32;k.IsProcessInJob.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.POINTER(ctypes.c_int)];assert k.IsProcessInJob(ctypes.c_void_p(-1),None,ctypes.byref(v));assert v.value;print('owned')"])
                .stdout(std::process::Stdio::piped()).stderr(std::process::Stdio::piped());
            let child = job.spawn(&mut command).unwrap();
            let output = child.wait_with_output().unwrap();
            assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
            assert_eq!(String::from_utf8_lossy(&output.stdout).trim(), "owned");
            }
        }
    }
}
