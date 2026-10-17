//! App update manifest（应用更新清单）：仅接受当前公开仓库的固定安装器资产。
use std::{collections::BTreeMap, ffi::CString};
use serde::{Deserialize, Serialize};

pub const LATEST_MANIFEST_URL: &str =
    "https://github.com/wet86y/voice-terminal/releases/latest/download/update.json";
pub const INSTALLER_ASSET: &str = "VoiceTerminalSetup.exe";
const MAX_MANIFEST_BYTES: u64 = 64 * 1024;

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
pub fn check_latest(current_version: &str) -> Result<Option<AppUpdateManifest>, String> {
    #[cfg(windows)] let bytes={
        let url=CString::new(LATEST_MANIFEST_URL).map_err(|_|"发行清单地址无效")?;
        let mut output=vec![0u8;MAX_MANIFEST_BYTES as usize+1];let mut error=[0i8;1024];
        let size=unsafe{vt_fetch_manifest(url.as_ptr(),output.as_mut_ptr(),output.len(),error.as_mut_ptr(),error.len())};
        if size<0{let message=error.iter().take_while(|value|**value!=0).map(|value|*value as u8).collect::<Vec<_>>();return Err(String::from_utf8_lossy(&message).to_string());}
        let size=size as usize;if size>MAX_MANIFEST_BYTES as usize{return Err("更新清单超出允许大小。".into());}output.truncate(size);output
    };
    #[cfg(not(windows))] let bytes={let _=current_version;return Err("应用更新仅支持 Windows（操作系统）。".into());};
    let manifest: AppUpdateManifest = serde_json::from_slice(&bytes)
        .map_err(|error| format!("更新清单格式无效：{error}"))?;
    manifest.validate()?;
    if manifest.is_newer_than(current_version)? { Ok(Some(manifest)) } else { Ok(None) }
}

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
}
