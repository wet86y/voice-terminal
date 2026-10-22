//! VerificationSession（单次维护校验会话）：仅在工作区独占维护中复用，退出任务即销毁。
use std::{cell::RefCell,collections::HashMap,path::{Path,PathBuf},sync::atomic::AtomicBool};
use serde_json::{Value,json};
use sha2::{Digest,Sha256};
thread_local! { static CURRENT:RefCell<Option<HashMap<(PathBuf,String),Receipt>>>=const{RefCell::new(None)}; }
struct Receipt{result:Result<(),String>,watch:Watch} // result（本轮结果）、watch（内容变化监视，避免复用过期结果）。
/// Watch（目录变化监视）：只读取一个目录身份，不为命中缓存再次遍历文件树；不可监视时禁用复用。
pub(crate) struct Watch{
    #[cfg(windows)] handle:isize, // handle（系统目录变化通知句柄）。
    root:PathBuf, // root（被监视的组件目录）。
    created:std::time::SystemTime, // created（目录创建身份，检测目录被整体替换）。
}
#[cfg(windows)]
unsafe extern "system"{
    fn FindFirstChangeNotificationW(path:*const u16,subtree:i32,filter:u32)->isize;
    fn FindCloseChangeNotification(handle:isize)->i32;
    fn WaitForSingleObject(handle:isize,milliseconds:u32)->u32;
}
pub(crate) fn watch(root:&Path)->Option<Watch>{
    if !CURRENT.with(|current|current.borrow().is_some()){return None;}
    #[cfg(windows)]{
        use std::os::windows::ffi::OsStrExt;
        let metadata=std::fs::symlink_metadata(root).ok()?;
        if !metadata.is_dir()||crate::workspace::metadata_reparse(&metadata){return None;}
        let path=root.as_os_str().encode_wide().chain(Some(0)).collect::<Vec<_>>();
        let created=metadata.created().ok()?;
        let handle=unsafe{FindFirstChangeNotificationW(path.as_ptr(),1,0x5f)};
        if handle==-1||handle==0{return None;}
        Some(Watch{handle,root:root.to_path_buf(),created})
    }
    #[cfg(not(windows))]{let _=root;None}
}
impl Watch{fn unchanged(&self)->bool{
    #[cfg(windows)]{(unsafe{WaitForSingleObject(self.handle,0)==258})&&std::fs::symlink_metadata(&self.root).is_ok_and(|info|info.is_dir()&&!crate::workspace::metadata_reparse(&info)&&info.created().ok()==Some(self.created))}
    #[cfg(not(windows))]{false}
}}
impl Drop for Watch{fn drop(&mut self){#[cfg(windows)]unsafe{FindCloseChangeNotification(self.handle);}}}
pub struct VerificationSession { owner:bool } // owner（仅最外层维护任务销毁本次结果）。
impl VerificationSession {
    pub fn begin()->Self{Self{owner:CURRENT.with(|current|{let mut current=current.borrow_mut();if current.is_some(){false}else{*current=Some(HashMap::new());true}})}}
}
impl Drop for VerificationSession{fn drop(&mut self){if self.owner{CURRENT.with(|current|*current.borrow_mut()=None);}}}
pub(crate) fn key(records:&[Value],exact:bool,python:bool)->String{
    let mut rows=records.iter().map(|row|json!([row["path"],row["size"],row["sha256"].as_str().unwrap_or("").to_ascii_lowercase()])).collect::<Vec<_>>();
    rows.sort_by_key(Value::to_string);format!("{:x}",Sha256::digest(serde_json::to_vec(&json!([exact,python,rows])).unwrap()))
}
pub(crate) fn reuse(root:&Path,key:&str)->Option<Result<(),String>>{CURRENT.with(|current|{let mut current=current.borrow_mut();let map=current.as_mut()?;let identity=(root.to_path_buf(),key.to_owned());
    if map.get(&identity).is_some_and(|receipt|receipt.watch.unchanged()){map.get(&identity).map(|receipt|receipt.result.clone())}else{map.remove(&identity);None}
})}
pub(crate) fn remember(root:&Path,key:String,result:Result<(),String>,watch:Option<Watch>)->Result<(),String>{
    let result=if result.is_ok()&&watch.as_ref().is_some_and(|watch|!watch.unchanged()){Err("校验期间组件发生变化，请重新执行完整性维护。".into())}else{result};
    CURRENT.with(|current|{if let (Some(current),Some(watch))=(current.borrow_mut().as_mut(),watch){current.insert((root.to_path_buf(),key),Receipt{result:result.clone(),watch});}});result
}
/// invalidate（写入前撤销）：成功和失败结果都不能跨组件替换沿用。
pub(crate) fn invalidate(root:&Path){CURRENT.with(|current|{if let Some(current)=current.borrow_mut().as_mut(){current.retain(|(path,_),_|!path.starts_with(root)&&!root.starts_with(path));}})}
pub(crate) fn ensure_unchanged()->Result<(),String>{CURRENT.with(|current|{
    if current.borrow().as_ref().is_some_and(|map|map.values().any(|receipt|receipt.result.is_ok()&&!receipt.watch.unchanged())){Err("维护期间受管组件发生外部变更，请重试完整性校验。".into())}else{Ok(())}
})}
/// Batch（统一检查计划）：所有现有组件共用一个有界并行文件池。
pub(crate) struct Batch {
    pub root:PathBuf, // root（组件路径）。
    pub records:Vec<Value>, // records（锁定文件）。
    pub exact:bool, // exact（是否要求精确文件集合）。
    pub python:bool, // python（是否容许登记源码的标准字节码）。
}
pub(crate) fn check(batches:Vec<Batch>,cancel:&AtomicBool,progress:&mut crate::resources::Progress)->Result<(),String>{crate::workspace::verify_batches(batches,cancel,progress)}

#[cfg(test)] mod tests{
    use super::*;
    use std::fs;
    fn sample(root:&Path,bytes:&[u8])->Vec<Value>{
        fs::create_dir_all(root).unwrap();fs::write(root.join("model.bin"),bytes).unwrap();
        vec![json!({"path":"model.bin","size":bytes.len(),"sha256":format!("{:x}",Sha256::digest(bytes))})]
    }
    #[test]
    fn one_batch_reuses_results_until_write_and_never_across_tasks(){
        let owner=crate::canonical_path(&std::env::temp_dir()).unwrap();let root=owner.join(format!("verification-{}",crate::workspace::new_id()));
        let a=root.join("a");let b=root.join("b");let ra=sample(&a,b"good");let rb=sample(&b,b"also-good");let cancel=AtomicBool::new(false);
        {
            let _session=VerificationSession::begin();let mut observed=0;
            check(vec![Batch{root:a.clone(),records:ra.clone(),exact:false,python:false},Batch{root:b.clone(),records:rb.clone(),exact:false,python:false}],&cancel,&mut|phase,done,_|if phase=="批量校验组件文件内容"{observed=observed.max(done)}).unwrap();
            assert_eq!(observed,13);
            let mut read_again=false;
            crate::workspace::verify_records_with_progress(&a,&ra,false,&cancel,&mut|phase,_,_|read_again|=phase=="校验组件文件内容").unwrap();
            assert!(!read_again);
            invalidate(&a);fs::write(a.join("model.bin"),b"evil").unwrap();
            assert!(crate::workspace::verify_records_with_progress(&a,&ra,false,&cancel,&mut|_,_,_|{}).is_err());
            assert!(crate::workspace::verify_records_with_progress(&b,&rb,false,&cancel,&mut|_,_,_|{}).is_ok());
        }
        fs::write(b.join("model.bin"),b"corrupted").unwrap();
        assert!(crate::workspace::verify_records_with_progress(&b,&rb,false,&cancel,&mut|_,_,_|{}).is_err());
        crate::workspace::remove_owned(&root,&owner).unwrap();
    }
    #[test]
    fn batch_records_corruption_without_losing_other_components_or_cancellation(){
        let owner=crate::canonical_path(&std::env::temp_dir()).unwrap();let root=owner.join(format!("verification-{}",crate::workspace::new_id()));
        let a=root.join("a");let b=root.join("b");let ra=sample(&a,b"good");let rb=sample(&b,b"good");fs::write(a.join("model.bin"),b"evil").unwrap();
        let _session=VerificationSession::begin();let cancel=AtomicBool::new(false);
        check(vec![Batch{root:a.clone(),records:ra.clone(),exact:false,python:false},Batch{root:b.clone(),records:rb.clone(),exact:false,python:false}],&cancel,&mut|_,_,_|{}).unwrap();
        assert!(reuse(&a,&key(&ra,false,false)).unwrap().is_err());assert!(reuse(&b,&key(&rb,false,false)).unwrap().is_ok());
        let cancelled=AtomicBool::new(true);assert!(check(vec![],&cancelled,&mut|_,_,_|{}).is_err());
        crate::workspace::remove_owned(&root,&owner).unwrap();
    }
    #[test]
    fn external_writes_revoke_receipts_without_a_second_directory_walk(){
        let owner=crate::canonical_path(&std::env::temp_dir()).unwrap();let root=owner.join(format!("verification-{}",crate::workspace::new_id()));
        let records=sample(&root,b"good");let _session=VerificationSession::begin();let cancel=AtomicBool::new(false);
        crate::workspace::verify_records_with_progress(&root,&records,false,&cancel,&mut|_,_,_|{}).unwrap();
        fs::write(root.join("model.bin"),b"evil").unwrap();
        let deadline=std::time::Instant::now()+std::time::Duration::from_secs(2);
        while reuse(&root,&key(&records,false,false)).is_some(){assert!(std::time::Instant::now()<deadline);std::thread::sleep(std::time::Duration::from_millis(10));}
        assert!(crate::workspace::verify_records_with_progress(&root,&records,false,&cancel,&mut|_,_,_|{}).is_err());
        crate::workspace::remove_owned(&root,&owner).unwrap();
    }
}
