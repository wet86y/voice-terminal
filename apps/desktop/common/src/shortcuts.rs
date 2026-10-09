//! shortcuts（桌面快捷方式）：直接指向正式入口，只维护本程序创建且目标匹配的文件。
use std::path::{Path,PathBuf};
#[cfg(windows)] mod platform {
    use super::*;
    use windows::{core::{Interface,PCWSTR},Win32::{System::Com::{CoInitializeEx,CoUninitialize,CoCreateInstance,CoTaskMemFree,COINIT_APARTMENTTHREADED,CLSCTX_INPROC_SERVER,IPersistFile,STGM_READ},UI::Shell::{IShellLinkW,ShellLink,SHGetKnownFolderPath,FOLDERID_Desktop,KF_FLAG_DEFAULT}}};
    const DESCRIPTION:&str="Voice Terminal · 受管工作区正式入口";
    fn wide(text:&str)->Vec<u16>{text.encode_utf16().chain(Some(0)).collect()}
    struct Com;
    impl Com {fn new()->Result<Self,String>{unsafe{CoInitializeEx(None,COINIT_APARTMENTTHREADED).ok().map_err(|e|e.to_string())?;}Ok(Self)}}
    impl Drop for Com{fn drop(&mut self){unsafe{CoUninitialize();}}}
    unsafe fn shell()->Result<IShellLinkW,String>{CoCreateInstance(&ShellLink,None,CLSCTX_INPROC_SERVER).map_err(|e|e.to_string())}
    unsafe fn desktop()->Result<PathBuf,String>{let path=SHGetKnownFolderPath(&FOLDERID_Desktop,KF_FLAG_DEFAULT,None).map_err(|e|e.to_string())?;let result=path.to_string().map_err(|e|e.to_string());CoTaskMemFree(Some(path.0 as _));result.map(PathBuf::from)}
    unsafe fn matches(path:&Path,target:&Path)->bool{
        let result:Result<bool,String>=(||{let link=shell()?;let file:IPersistFile=link.cast().map_err(|e|e.to_string())?;let name=wide(&path.to_string_lossy());file.Load(PCWSTR(name.as_ptr()),STGM_READ).map_err(|e|e.to_string())?;
            let mut actual=vec![0u16;32768];let mut description=vec![0u16;1024];let mut arguments=vec![0u16;1024];
            link.GetPath(&mut actual,std::ptr::null_mut(),0).map_err(|e|e.to_string())?;link.GetDescription(&mut description).map_err(|e|e.to_string())?;link.GetArguments(&mut arguments).map_err(|e|e.to_string())?;
            Ok(String::from_utf16_lossy(&actual).trim_end_matches('\0').eq_ignore_ascii_case(&target.to_string_lossy())&&String::from_utf16_lossy(&description).trim_end_matches('\0')==DESCRIPTION&&arguments[0]==0)
        })();result.unwrap_or(false)
    }
    pub fn create(root:&Path,previous:Option<&Path>)->Result<PathBuf,String>{
        let _com=Com::new()?;
        unsafe{create_in(&desktop()?,root,previous)}
    }
    unsafe fn create_in(desktop:&Path,root:&Path,previous:Option<&Path>)->Result<PathBuf,String>{
        unsafe{let target=root.join("VoiceTerminal.exe");let name=root.file_name().unwrap_or_default().to_string_lossy();
            let id=crate::resources::load(&root.join("portable.json"))?["workspace_id"].as_str().ok_or("工作区编号无效")?.to_string();
            let id=id.chars().filter(|value|value.is_ascii_alphanumeric()||*value=='-'||*value=='_').collect::<String>();
            let candidates=[desktop.join("Voice Terminal.lnk"),desktop.join(format!("Voice Terminal - {name}.lnk")),desktop.join(format!("Voice Terminal - {name} - {id}.lnk"))];
            let shortcut=candidates.into_iter().find(|path|!path.exists()||matches(path,&target)||previous.is_some_and(|source|matches(path,&source.join("VoiceTerminal.exe")))).ok_or("桌面快捷方式名称均已被其他文件使用。")?;
            let link=shell()?;let exe=wide(&target.to_string_lossy());let directory=wide(&root.to_string_lossy());let description=wide(DESCRIPTION);
            link.SetPath(PCWSTR(exe.as_ptr())).map_err(|e|e.to_string())?;link.SetWorkingDirectory(PCWSTR(directory.as_ptr())).map_err(|e|e.to_string())?;link.SetDescription(PCWSTR(description.as_ptr())).map_err(|e|e.to_string())?;link.SetIconLocation(PCWSTR(exe.as_ptr()),0).map_err(|e|e.to_string())?;
            let persist:IPersistFile=link.cast().map_err(|e|e.to_string())?;let filename=wide(&shortcut.to_string_lossy());persist.Save(PCWSTR(filename.as_ptr()),true).map_err(|e|format!("桌面快捷方式创建失败：{e}"))?;
            if let Some(source)=previous {for entry in std::fs::read_dir(&desktop).map_err(|e|e.to_string())?{let path=entry.map_err(|e|e.to_string())?.path();if path!=shortcut&&path.extension().is_some_and(|ext|ext.eq_ignore_ascii_case("lnk"))&&matches(&path,&source.join("VoiceTerminal.exe")){std::fs::remove_file(path).map_err(|e|e.to_string())?;}}}
            Ok(shortcut)
        }
    }
    #[cfg(test)] mod tests{
        use super::*;
        fn fixture()->(PathBuf,PathBuf,PathBuf){
            let base=std::env::temp_dir().join(format!("voice-shortcuts-{}",crate::workspace::new_id()));
            let desktop=base.join("desktop");let root=base.join("中文 工作区");
            std::fs::create_dir_all(&desktop).unwrap();std::fs::create_dir_all(&root).unwrap();
            std::fs::write(root.join("VoiceTerminal.exe"),b"formal entry").unwrap();
            crate::workspace::atomic_json(&root.join("portable.json"),&serde_json::json!({"workspace_id":"test-workspace"})).unwrap();
            (base,desktop,root)
        }
        #[test] fn collision_preserves_unowned_file_and_reinstall_updates_own_link(){
            let _com=Com::new().unwrap();let(base,desktop,root)=fixture();
            let other=desktop.join("Voice Terminal.lnk");std::fs::write(&other,b"unowned shortcut").unwrap();
            unsafe{let link=create_in(&desktop,&root,None).unwrap();assert_eq!(link,desktop.join("Voice Terminal - 中文 工作区.lnk"));assert!(matches(&link,&root.join("VoiceTerminal.exe")));assert_eq!(create_in(&desktop,&root,None).unwrap(),link);}
            assert_eq!(std::fs::read(other).unwrap(),b"unowned shortcut");std::fs::remove_dir_all(base).unwrap();
        }
        #[test] fn migration_retargets_matching_owned_shortcut(){
            let _com=Com::new().unwrap();let(base,desktop,source)=fixture();let target=base.join("搬迁目标");
            std::fs::create_dir_all(&target).unwrap();std::fs::copy(source.join("VoiceTerminal.exe"),target.join("VoiceTerminal.exe")).unwrap();std::fs::copy(source.join("portable.json"),target.join("portable.json")).unwrap();
            unsafe{let old=create_in(&desktop,&source,None).unwrap();let new=create_in(&desktop,&target,Some(&source)).unwrap();assert_eq!(old,new);assert!(matches(&new,&target.join("VoiceTerminal.exe")));assert!(!matches(&new,&source.join("VoiceTerminal.exe")));}
            std::fs::remove_dir_all(base).unwrap();
        }
        #[test] fn shortcut_failure_preserves_installed_program(){
            let _com=Com::new().unwrap();let(base,_,root)=fixture();let invalid=base.join("desktop-file");std::fs::write(&invalid,b"not a directory").unwrap();
            assert!(unsafe{create_in(&invalid,&root,None)}.is_err());assert_eq!(std::fs::read(root.join("VoiceTerminal.exe")).unwrap(),b"formal entry");std::fs::remove_dir_all(base).unwrap();
        }
    }
}
pub fn create(root:&Path,previous:Option<&Path>)->Result<PathBuf,String>{
    #[cfg(windows)]{platform::create(root,previous)}
    #[cfg(not(windows))]{let _=(root,previous);Err("桌面快捷方式仅支持 Windows（操作系统）。".into())}
}
