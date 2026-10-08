//! Native setup UI（原生初始化界面）：无需 Python（后端环境）或 WebView2（网页运行时）。
use std::{path::PathBuf,sync::{Arc,Mutex,atomic::{AtomicBool,Ordering}}};

#[derive(Clone)]
pub struct Progress {
    pub cancel:Arc<AtomicBool>, // cancel（任务取消信号）。
    message:Arc<Mutex<String>>, // message（窗口当前阶段和字节进度）。
    can_cancel:Arc<AtomicBool>, // can_cancel（原子切换及清理阶段禁止取消）。
}
impl Progress {
    pub fn update(&self,phase:&str,current:u64,total:u64){
        self.can_cancel.store(!phase.starts_with("启动目标")&&!phase.starts_with("目标已启动")&&!phase.starts_with("切换定位")&&!phase.starts_with("启用基础组件"),Ordering::Release);
        let text=if total>0 {format!("{phase}\n{:.1}%  ·  {:.2} / {:.2} MiB（兆二进制字节）", current as f64*100.0/total as f64,current as f64/1048576.0,total as f64/1048576.0)}else{phase.into()};
        *self.message.lock().unwrap()=text;
    }
}
#[cfg(windows)] fn wide(value:&str)->Vec<u16>{value.encode_utf16().chain(Some(0)).collect()}

pub fn choose_folder(title:&str)->Result<Option<PathBuf>,String>{
    #[cfg(windows)] unsafe {
        use windows_sys::Win32::{System::Com::{CoInitializeEx,CoUninitialize,CoTaskMemFree,COINIT_APARTMENTTHREADED},UI::Shell::{SHBrowseForFolderW,SHGetPathFromIDListW,BROWSEINFOW,BIF_RETURNONLYFSDIRS,BIF_NEWDIALOGSTYLE,BIF_EDITBOX}};
        let initialized=CoInitializeEx(std::ptr::null(),COINIT_APARTMENTTHREADED as u32)>=0;
        let label=wide(title);let mut name=[0u16;260];let mut info:BROWSEINFOW=std::mem::zeroed();
        info.lpszTitle=label.as_ptr();info.pszDisplayName=name.as_mut_ptr();info.ulFlags=BIF_RETURNONLYFSDIRS|BIF_NEWDIALOGSTYLE|BIF_EDITBOX;
        let item=SHBrowseForFolderW(&info);
        let result=if item.is_null(){Ok(None)}else{
            let mut path=[0u16;260];let valid=SHGetPathFromIDListW(item,path.as_mut_ptr());CoTaskMemFree(item as _);
            if valid==0 {Err("所选位置不是本地文件夹。".into())}else{Ok(Some(PathBuf::from(String::from_utf16_lossy(&path[..path.iter().position(|v|*v==0).unwrap_or(path.len())]))))}
        };if initialized{CoUninitialize();}result
    }
    #[cfg(not(windows))] {let _=title;Err("原生文件夹选择仅支持 Windows（操作系统）。".into())}
}
pub fn choose_files()->Result<Vec<PathBuf>,String>{
    #[cfg(windows)] unsafe {
        use windows_sys::Win32::UI::Controls::Dialogs::{GetOpenFileNameW,OPENFILENAMEW,OFN_EXPLORER,OFN_FILEMUSTEXIST,OFN_ALLOWMULTISELECT,OFN_NOCHANGEDIR};
        let mut names=vec![0u16;32768];let filter:Vec<u16>="资源包或官方安装器\0*.zip;*.exe;*.7z\0所有文件\0*.*\0\0".encode_utf16().collect();let title=wide("选择离线资源包或官方 DSH（代理宿主）安装器");
        let mut info:OPENFILENAMEW=std::mem::zeroed();info.lStructSize=std::mem::size_of::<OPENFILENAMEW>() as u32;
        info.lpstrFile=names.as_mut_ptr();info.nMaxFile=names.len() as u32;info.lpstrFilter=filter.as_ptr();info.lpstrTitle=title.as_ptr();
        info.Flags=OFN_EXPLORER|OFN_FILEMUSTEXIST|OFN_ALLOWMULTISELECT|OFN_NOCHANGEDIR;
        if GetOpenFileNameW(&mut info)==0{return Ok(vec![]);}
        let mut values=vec![];let mut offset=0;
        while offset<names.len()&&names[offset]!=0{let end=names[offset..].iter().position(|v|*v==0).unwrap()+offset;values.push(String::from_utf16_lossy(&names[offset..end]));offset=end+1;}
        if values.len()==1{return Ok(vec![PathBuf::from(&values[0])]);}
        Ok(values[1..].iter().map(|v|PathBuf::from(&values[0]).join(v)).collect())
    }
    #[cfg(not(windows))] {Err("文件选择仅支持 Windows（操作系统）。".into())}
}
/// choose_resource（按组件选择导入来源）：批量选择压缩包，单模型选择文件夹。
pub fn choose_resource(resource_id:Option<&str>)->Result<Vec<PathBuf>,String>{
    match resource_id {
        None => return choose_files(),
        Some("dsh") => {},
        Some(_) => return Ok(choose_folder("选择已下载的单模型文件夹")?.into_iter().collect()),
    }
    #[cfg(windows)] unsafe {
        use windows_sys::Win32::UI::WindowsAndMessaging::{MessageBoxW,MB_YESNOCANCEL,MB_ICONQUESTION,IDYES,IDNO};
        let choice=MessageBoxW(std::ptr::null_mut(),wide("选择 DSH（代理执行宿主）导入来源：\n\n是：选择官方安装器文件\n否：选择已提取的可信运行文件夹\n取消：返回").as_ptr(),wide("导入 DSH").as_ptr(),MB_YESNOCANCEL|MB_ICONQUESTION);
        match choice {IDYES=>choose_files(),IDNO=>Ok(choose_folder("选择已提取的可信 DSH 运行文件夹")?.into_iter().collect()),_=>Ok(vec![])}
    }
    #[cfg(not(windows))] {Err("资源选择仅支持 Windows（操作系统）。".into())}
}

/// retry（原生重试提示）：失败后继续使用当前工作区中保留的下载断点。
pub fn retry(message:&str)->bool{
    #[cfg(windows)] unsafe{
        use windows_sys::Win32::UI::WindowsAndMessaging::{MessageBoxW,MB_RETRYCANCEL,MB_ICONERROR,IDRETRY};
        MessageBoxW(std::ptr::null_mut(),wide(&format!("{message}\n\n重试将保留已选工作区并恢复下载；取消后可再次打开启动程序。")).as_ptr(),wide("工作区初始化未完成").as_ptr(),MB_RETRYCANCEL|MB_ICONERROR)==IDRETRY
    }
    #[cfg(not(windows))]{let _=message;false}
}

pub fn run<T:Send+'static,F:FnOnce(Progress)->Result<T,String>+Send+'static>(title:&str,task:F)->Result<T,String>{
    let progress=Progress {cancel:Arc::new(AtomicBool::new(false)),message:Arc::new(Mutex::new("正在准备工作区…".into())),can_cancel:Arc::new(AtomicBool::new(true))};
    let result=Arc::new(Mutex::new(None));
    #[cfg(windows)] unsafe {
        use windows_sys::Win32::{System::LibraryLoader::GetModuleHandleW,UI::WindowsAndMessaging::*};
        unsafe extern "system" fn procedure(window:windows_sys::Win32::Foundation::HWND,message:u32,wparam:usize,lparam:isize)->isize{
            if message==WM_COMMAND||message==WM_CLOSE {
                let state=unsafe{GetWindowLongPtrW(window,GWLP_USERDATA)} as *const Progress;
                if !state.is_null()&&unsafe{(*state).can_cancel.load(Ordering::Acquire)}{unsafe{(*state).cancel.store(true,Ordering::Release)};}return 0;
            }unsafe{DefWindowProcW(window,message,wparam,lparam)}
        }
        let class=wide("VoiceTerminalSetupProgress");let instance=GetModuleHandleW(std::ptr::null());
        let mut registration:WNDCLASSW=std::mem::zeroed();registration.lpfnWndProc=Some(procedure);registration.hInstance=instance;registration.lpszClassName=class.as_ptr();
        RegisterClassW(&registration);
        let window=CreateWindowExW(0,class.as_ptr(),wide(title).as_ptr(),WS_CAPTION|WS_SYSMENU|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,560,205,std::ptr::null_mut(),std::ptr::null_mut(),instance,std::ptr::null());
        if window.is_null(){return Err("无法建立原生初始化窗口。".into());}
        SetWindowLongPtrW(window,GWLP_USERDATA,&progress as *const Progress as isize);
        let text=CreateWindowExW(0,wide("STATIC").as_ptr(),wide("正在准备工作区…").as_ptr(),WS_CHILD|WS_VISIBLE,22,22,510,80,window,std::ptr::null_mut(),instance,std::ptr::null());
        let button=CreateWindowExW(0,wide("BUTTON").as_ptr(),wide("取消").as_ptr(),WS_CHILD|WS_VISIBLE,426,112,100,32,window,1usize as _,instance,std::ptr::null());
        let worker_progress=progress.clone();let worker_result=result.clone();
        std::thread::spawn(move||{let value=std::panic::catch_unwind(std::panic::AssertUnwindSafe(||task(worker_progress))).unwrap_or_else(|_|Err("初始化任务异常退出。".into()));*worker_result.lock().unwrap()=Some(value);});
        let mut message:MSG=std::mem::zeroed();
        while result.lock().unwrap().is_none(){
            while PeekMessageW(&mut message,std::ptr::null_mut(),0,0,PM_REMOVE)!=0{TranslateMessage(&message);DispatchMessageW(&message);}
            #[link(name="user32")]
            unsafe extern "system"{fn EnableWindow(window:windows_sys::Win32::Foundation::HWND,enable:i32)->i32;}
            EnableWindow(button,if progress.can_cancel.load(Ordering::Acquire){1}else{0});
            SetWindowTextW(text,wide(&progress.message.lock().unwrap()).as_ptr());std::thread::sleep(std::time::Duration::from_millis(80));
        }
        DestroyWindow(window);
    }
    #[cfg(not(windows))] {let _=title;*result.lock().unwrap()=Some(task(progress));}
    let value=result.lock().unwrap().take().ok_or("初始化任务未返回结果")?;value
}
