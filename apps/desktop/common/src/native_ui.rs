//! Native dialogs（系统选择与提示对话框）：加载进度统一由共享启动页显示。
use std::path::PathBuf;

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
