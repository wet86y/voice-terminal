//! App update manifest（应用更新清单）：仅接受当前公开仓库的固定安装器资产。
use std::{collections::BTreeMap, ffi::CString, fs, path::{Path, PathBuf}};
use serde::{Deserialize, Serialize};

pub const LATEST_MANIFEST_URL: &str =
    "https://github.com/wet86y/voice-terminal/releases/latest/download/update.json";
pub const INSTALLER_ASSET: &str = "VoiceTerminalSetup.exe";
const MAX_MANIFEST_BYTES: u64 = 64 * 1024;
pub const MAINTENANCE_INSTALLER: &str = "maintenance/VoiceTerminalSetup.exe"; // MAINTENANCE_INSTALLER（长期保留的维护分发器）。
const MAINTENANCE_RECORD: &str = "maintenance/installer.json"; // MAINTENANCE_RECORD（分发器身份记录）。
pub const UPDATE_INSTALLER: &str = "tmp/app-update/VoiceTerminalSetup.exe"; // UPDATE_INSTALLER（与长期校验器分开的更新暂存分发器）。

#[cfg(windows)]
unsafe extern "C" {
    fn vt_fetch_manifest(url:*const i8,output:*mut u8,output_size:usize,error:*mut i8,error_size:usize)->i32;
}

/// AppUpdateManifest（应用更新清单）：字段与公开发行版的 update.json 保持一致。
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AppUpdateManifest {
    pub version: String, // version（目标程序版本）。
    pub tag: String, // tag（GitHub 发行标签）。
    pub asset: String, // asset（分发安装器文件名）。
    pub size: u64, // size（安装器字节数）。
    pub sha256: String, // sha256（安装器 SHA-256 摘要）。
    #[serde(rename = "releaseNotes")]
    pub release_notes: String, // releaseNotes（中文发行说明）。
    pub fields: BTreeMap<String, String>, // fields（字段中文说明映射）。
}

impl AppUpdateManifest {
    pub fn validate(&self) -> Result<(), String> {
        let version = semver::Version::parse(&self.version).map_err(|_| "更新清单版本无效。")?;
        if !version.pre.is_empty() || !version.build.is_empty() || self.tag != format!("v{}", self.version) {
            return Err("发行版本与标签不匹配，已拒绝该更新。".into());
        }
        if self.asset != INSTALLER_ASSET || self.size < 1024 * 1024 || self.size > 2 * 1024 * 1024 * 1024 {
            return Err("更新安装器名称或大小不符合产品发行约定。".into());
        }
        if self.sha256.len() != 64 || !self.sha256.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err("更新安装器摘要格式无效。".into());
        }
        if self.release_notes.trim().is_empty() || self.release_notes.len() > 32 * 1024 {
            return Err("发行说明为空或超出允许长度。".into());
        }
        for key in ["version", "tag", "asset", "size", "sha256", "releaseNotes"] {
            let description = self.fields.get(key).ok_or("更新清单缺少字段中文说明。")?;
            if description.trim().is_empty() || !description.chars().any(|ch| ('\u{4e00}'..='\u{9fff}').contains(&ch)) {
                return Err(format!("更新清单字段 {key} 缺少中文说明。"));
            }
        }
        Ok(())
    }

    pub fn asset_url(&self) -> String {
        format!("https://github.com/wet86y/voice-terminal/releases/download/{}/{}", self.tag, self.asset)
    }

    pub fn is_newer_than(&self, current: &str) -> Result<bool, String> {
        let target = semver::Version::parse(&self.version).map_err(|_| "更新清单版本无效。")?;
        let installed = semver::Version::parse(current).map_err(|_| "当前程序版本无效。")?;
        Ok(target > installed)
    }
}

/// check_latest（检查最新发行版）：限定 HTTPS（加密传输）和有界清单大小。
fn fetch_json(url:&str)->Result<Vec<u8>,String>{
    #[cfg(windows)] let bytes={
        let url=CString::new(url).map_err(|_|"发行清单地址无效")?;
        let mut output=vec![0u8;MAX_MANIFEST_BYTES as usize+1];let mut error=[0i8;1024];
        let size=unsafe{vt_fetch_manifest(url.as_ptr(),output.as_mut_ptr(),output.len(),error.as_mut_ptr(),error.len())};
        if size<0{let message=error.iter().take_while(|value|**value!=0).map(|value|*value as u8).collect::<Vec<_>>();return Err(String::from_utf8_lossy(&message).to_string());}
        let size=size as usize;if size>MAX_MANIFEST_BYTES as usize{return Err("更新清单超出允许大小。".into());}output.truncate(size);output
    };
    #[cfg(not(windows))] let bytes={let _=url;return Err("应用更新仅支持 Windows（操作系统）。".into());};
    Ok(bytes)
}
fn parse_manifest(bytes:&[u8])->Result<AppUpdateManifest,String>{
    let manifest: AppUpdateManifest = serde_json::from_slice(&bytes)
        .map_err(|error| format!("更新清单格式无效：{error}"))?;
    manifest.validate()?;Ok(manifest)
}
pub fn check_latest(current_version: &str) -> Result<Option<AppUpdateManifest>, String> {
    let manifest=parse_manifest(&fetch_json(LATEST_MANIFEST_URL)?)?;
    if manifest.is_newer_than(current_version)? { Ok(Some(manifest)) } else { Ok(None) }
}

/// version_manifest_url（指定版本发行清单）：完整性维护绝不回退到 latest（最新发行入口）。
pub fn version_manifest_url(version:&str)->Result<String,String>{
    let parsed=semver::Version::parse(version).map_err(|_|"当前程序版本无效。")?;
    if !parsed.pre.is_empty()||!parsed.build.is_empty(){return Err("当前程序没有对应的正式校验程序。".into());}
    Ok(format!("https://github.com/wet86y/voice-terminal/releases/download/v{version}/update.json"))
}
pub fn check_version(version:&str)->Result<AppUpdateManifest,String>{
    let manifest=parse_manifest(&fetch_json(&version_manifest_url(version)?)?)?;
    if manifest.version!=version{return Err("指定版本的发行清单不匹配；不会改为下载最新版。".into());}Ok(manifest)
}

#[derive(Clone,Debug,Deserialize,Serialize)]
pub struct RetainedInstaller {
    pub schema:u32, // schema（长期分发器记录格式）。
    pub manifest:AppUpdateManifest, // manifest（固定发行清单，含大小及摘要）。
    pub build:String, // build（内嵌程序构建身份）。
    pub fields:BTreeMap<String,String>, // fields（记录字段中文说明）。
}
impl RetainedInstaller {
    pub fn matches(&self,version:&str,build:&str)->bool{self.schema==1&&self.manifest.validate().is_ok()&&self.manifest.version==version&&self.build==build}
}
/// delivery（发行身份）：相同版本的不同构建也不能互相冒充完整性维修来源。
pub fn delivery(manifest:&AppUpdateManifest)->Result<RetainedInstaller,String>{
    manifest.validate()?;
    let url=format!("https://github.com/wet86y/voice-terminal/releases/download/{}/delivery.json",manifest.tag);
    let record:serde_json::Value=serde_json::from_slice(&fetch_json(&url)?).map_err(|_|"交付记录格式无效。")?;
    if record["version"]!=manifest.version||record["entry"]!=INSTALLER_ASSET||record["size"].as_u64()!=Some(manifest.size)
        ||record["sha256"].as_str().is_none_or(|hash|!hash.eq_ignore_ascii_case(&manifest.sha256)){
        return Err("交付记录与指定版本分发器不匹配。".into());
    }
    let build=record["build"].as_str().filter(|value|!value.is_empty()).ok_or("交付记录缺少构建身份。")?.to_owned();
    Ok(RetainedInstaller{schema:1,manifest:manifest.clone(),build,fields:BTreeMap::from([
        ("schema".into(),"长期分发器记录格式".into()),("manifest".into(),"已验证的固定发行清单".into()),
        ("build".into(),"内嵌程序构建编号".into()),("fields".into(),"字段中文说明".into())])})
}
pub fn installer_path(root:&Path)->Result<PathBuf,String>{crate::workspace::safe_path(root,MAINTENANCE_INSTALLER)}
pub fn update_installer_path(root:&Path)->Result<PathBuf,String>{crate::workspace::safe_path(root,UPDATE_INSTALLER)}
/// installed_build（独立安装身份）：本体清单损坏时，仍能从上次提交身份或兼容记录启动修复。
pub fn installed_build(root:&Path,version:&str)->Result<String,String>{
    for relative in ["data/state/product-identity.json","app/release.json"]{
        if let Ok(record)=crate::resources::load(&crate::workspace::safe_path(root,relative)?){
            if record["schema"]!=1||record["version"]!=version{continue;}
            if let Some(build)=record["build"].as_str().filter(|v|!v.is_empty()){return Ok(build.to_owned());}
        }
    }
    // 旧工作区可能没有独立身份；兼容记录只能在工作区编号一致时作为回退。
    let portable=crate::resources::load(&crate::workspace::safe_path(root,"portable.json")?).ok();
    let compatibility=crate::resources::load(&crate::workspace::safe_path(root,"data/state/compatibility.json")?).ok();
    if let (Some(portable),Some(record))=(portable,compatibility){
        if portable["schema"]==2&&record["schema"]==1&&portable["workspace_id"].as_str().is_some_and(|id|!id.is_empty()&&record["workspace_id"]==id){
            if let Some(build)=record["target_build"].as_str().filter(|v|!v.is_empty()){return Ok(build.to_owned());}
        }
    }
    Err("缺少当前构建身份，请使用原始分发器修复程序本体。".into())
}
pub fn retained(root:&Path,version:&str,build:&str)->Result<RetainedInstaller,String>{
    let value=crate::resources::load(&crate::workspace::safe_path(root,MAINTENANCE_RECORD)?)?;
    let record:RetainedInstaller=serde_json::from_value(value).map_err(|_|"本机校验程序记录无效。")?;
    let path=installer_path(root)?;
    if !record.matches(version,build)||std::fs::metadata(path).map_err(|_|"尚未下载当前版本的校验程序。")?.len()!=record.manifest.size{
        return Err("校验程序缺失或与本地构建不一致，请下载当前版本的校验程序。".into());
    }Ok(record)
}
pub fn save_retained(root:&Path,record:&RetainedInstaller)->Result<(),String>{
    crate::workspace::atomic_json(&crate::workspace::safe_path(root,MAINTENANCE_RECORD)?,&serde_json::to_value(record).map_err(|e|e.to_string())?)
}

/// retain_local_installer（保存本地当前版本校验器）：将刚完成安装的分发器固定到维护目录。
pub fn retain_local_installer(root:&Path,source:&Path,version:&str,build:&str)->Result<(),String>{
    let parsed=semver::Version::parse(version).map_err(|_|"当前程序版本无效。")?;
    if !parsed.pre.is_empty()||!parsed.build.is_empty()||build.trim().is_empty(){return Err("当前安装器缺少正式版本或构建身份。".into());}
    let source=fs::canonicalize(source).map_err(|error|format!("无法定位当前分发器：{error}"))?;
    let source_metadata=fs::metadata(&source).map_err(|error|error.to_string())?;
    let size=source_metadata.len();
    if !(1024*1024..=2*1024*1024*1024).contains(&size){return Err("当前分发器大小不符合维护器要求。".into());}
    let sha256=crate::workspace::file_hash(&source)?;
    let destination=installer_path(root)?;
    let parent=destination.parent().ok_or("维护器目录无效")?;
    fs::create_dir_all(parent).map_err(|error|format!("无法创建校验维护目录：{error}"))?;
    let destination=installer_path(root)?;
    let same_file=destination.exists()&&fs::canonicalize(&destination).map_err(|error|error.to_string())?==source;
    if !same_file{
        let temporary_relative=format!("maintenance/.VoiceTerminalSetup-{}.tmp",crate::workspace::new_id());
        let temporary=crate::workspace::safe_path(root,&temporary_relative)?;
        let copy_result=(||{
            fs::copy(&source,&temporary).map_err(|error|format!("无法复制当前校验分发器：{error}"))?;
            if fs::metadata(&temporary).map_err(|error|error.to_string())?.len()!=size||crate::workspace::file_hash(&temporary)?!=sha256{
                return Err("当前校验分发器复制后摘要不匹配。".into());
            }
            replace_file(&temporary,&destination)
        })();
        if let Err(error)=copy_result{let _=fs::remove_file(&temporary);return Err(error);}
    }
    if fs::metadata(&destination).map_err(|error|error.to_string())?.len()!=size||crate::workspace::file_hash(&destination)?!=sha256{
        return Err("已保存的当前校验分发器摘要不匹配。".into());
    }
    let manifest=AppUpdateManifest{
        version:version.to_owned(),tag:format!("v{version}"),asset:INSTALLER_ASSET.to_owned(),size,sha256,
        release_notes:"当前分发器用于校验本地版本的受管组件。".into(),
        fields:BTreeMap::from([
            ("version".into(),"产品版本".into()),("tag".into(),"发行标签".into()),
            ("asset".into(),"分发器文件名".into()),("size".into(),"分发器字节数".into()),
            ("sha256".into(),"分发器 SHA-256 摘要".into()),("releaseNotes".into(),"维护用途说明".into()),
        ]),
    };
    manifest.validate()?;
    let record=RetainedInstaller{schema:1,manifest,build:build.to_owned(),fields:BTreeMap::from([
        ("schema".into(),"长期分发器记录格式".into()),("manifest".into(),"当前分发器文件身份".into()),
        ("build".into(),"内嵌程序构建编号".into()),("fields".into(),"字段中文说明".into()),
    ])};
    save_retained(root,&record)
}

#[cfg(windows)]
fn replace_file(source:&Path,destination:&Path)->Result<(),String>{
    use std::os::windows::ffi::OsStrExt;
    unsafe extern "system" {fn MoveFileExW(from:*const u16,to:*const u16,flags:u32)->i32;}
    let from:Vec<_>=source.as_os_str().encode_wide().chain(Some(0)).collect();
    let to:Vec<_>=destination.as_os_str().encode_wide().chain(Some(0)).collect();
    if unsafe{MoveFileExW(from.as_ptr(),to.as_ptr(),1|8)}==0{Err(std::io::Error::last_os_error().to_string())}else{Ok(())}
}
#[cfg(not(windows))]
fn replace_file(source:&Path,destination:&Path)->Result<(),String>{fs::rename(source,destination).map_err(|error|error.to_string())}

#[cfg(test)]
mod tests {
    use super::*;

    fn manifest() -> AppUpdateManifest {
        AppUpdateManifest {
            version: "0.10.0".into(), tag: "v0.10.0".into(), asset: INSTALLER_ASSET.into(),
            size: 4 * 1024 * 1024, sha256: "a".repeat(64), release_notes: "修复更新流程".into(),
            fields: BTreeMap::from([
                ("version".into(), "产品版本".into()), ("tag".into(), "发行标签".into()),
                ("asset".into(), "安装器名称".into()), ("size".into(), "安装器字节数".into()),
                ("sha256".into(), "安装器文件摘要".into()), ("releaseNotes".into(), "中文发行说明".into()),
            ]),
        }
    }

    #[test]
    fn validates_fixed_asset_and_chinese_field_descriptions() {
        let value = manifest();
        assert!(value.validate().is_ok());
        assert_eq!(value.asset_url(), "https://github.com/wet86y/voice-terminal/releases/download/v0.10.0/VoiceTerminalSetup.exe");
        assert!(value.is_newer_than("0.9.0").unwrap());
        assert!(!value.is_newer_than("0.10.0").unwrap());
    }

    #[test]
    fn rejects_version_asset_digest_and_field_mismatches() {
        let mut value = manifest(); value.tag = "v0.9.0".into(); assert!(value.validate().is_err());
        let mut value = manifest(); value.asset = "other.exe".into(); assert!(value.validate().is_err());
        let mut value = manifest(); value.sha256 = "not-a-digest".into(); assert!(value.validate().is_err());
        let mut value = manifest(); value.fields.insert("size".into(), "asset bytes".into()); assert!(value.validate().is_err());
    }
    #[test]
    fn integrity_uses_exact_release_and_requires_the_same_build(){
        assert_eq!(version_manifest_url("0.9.6").unwrap(),"https://github.com/wet86y/voice-terminal/releases/download/v0.9.6/update.json");
        assert!(version_manifest_url("../../latest").is_err());
        assert!(version_manifest_url("0.9.6-preview").is_err());
        let record=RetainedInstaller{schema:1,manifest:manifest(),build:"build-a".into(),fields:BTreeMap::new()};
        assert!(record.matches("0.10.0","build-a"));
        assert!(!record.matches("0.10.0","build-b"));
        assert!(!record.matches("0.9.6","build-a"));
    }
    #[test]
    fn repair_identity_survives_a_damaged_release_manifest(){
        let owner=crate::canonical_path(&std::env::temp_dir()).unwrap();let root=owner.join(format!("identity-{}",crate::workspace::new_id()));
        crate::workspace::atomic_json(&root.join("data/state/product-identity.json"),&serde_json::json!({"schema":1,"version":"0.9.6","build":"original-build"})).unwrap();
        crate::workspace::atomic_json(&root.join("data/state/compatibility.json"),&serde_json::json!({"schema":1,"target_build":"stale-build"})).unwrap();
        std::fs::create_dir_all(root.join("app")).unwrap();std::fs::write(root.join("app/release.json"),b"broken").unwrap();
        assert_eq!(installed_build(&root,"0.9.6").unwrap(),"original-build");
        assert!(installed_build(&root,"0.10.0").is_err());
        crate::workspace::remove_owned(&root,&owner).unwrap();
    }
    #[test]
    fn legacy_identity_fallback_requires_the_same_workspace(){
        let owner=crate::canonical_path(&std::env::temp_dir()).unwrap();let root=owner.join(format!("identity-{}",crate::workspace::new_id()));
        crate::workspace::atomic_json(&root.join("portable.json"),&serde_json::json!({"schema":2,"workspace_id":"current"})).unwrap();
        let path=root.join("data/state/compatibility.json");
        crate::workspace::atomic_json(&path,&serde_json::json!({"schema":1,"workspace_id":"other","target_build":"wrong"})).unwrap();
        assert!(installed_build(&root,"0.9.6").is_err());
        crate::workspace::atomic_json(&path,&serde_json::json!({"schema":1,"workspace_id":"current","target_build":"legacy"})).unwrap();
        assert_eq!(installed_build(&root,"0.9.6").unwrap(),"legacy");
        crate::workspace::remove_owned(&root,&owner).unwrap();
    }
}
