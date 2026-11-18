//! RemoteImportContract（远程导入契约）：只传服务端签发的暂存编号和规范化相对清单。
use serde::{Deserialize, Serialize};
use std::{collections::HashSet, path::PathBuf};

pub const REMOTE_IMPORT_MAX_FILES: usize = 4096; // REMOTE_IMPORT_MAX_FILES（一次目录导入的最大文件数）。
pub const REMOTE_IMPORT_MAX_BYTES: u64 = 4 * 1024 * 1024 * 1024; // REMOTE_IMPORT_MAX_BYTES（一次远程导入的最大原始字节数）。

/// RemoteImportSourceTicket（服务端暂存票据）：仅在原生进程间传递，绝不序列化给远端。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RemoteImportSourceTicket {
    pub source_id: String,           // source_id（服务端签发的暂存来源编号）。
    pub resource_id: Option<String>, // resource_id（要配置的目标资源；空值表示批量导入）。
    pub staging_dir: PathBuf,        // staging_dir（服务端受管暂存目录的原生路径）。
}

/// RemoteImportFileEntry（远程导入文件）：路径相对于服务端签发的暂存目录。
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RemoteImportFileEntry {
    pub relative_path: String, // relative_path（规范化相对路径；不接受绝对路径、盘符或目录穿越）。
    pub byte_length: u64,      // byte_length（该文件的预期字节数）。
    pub sha256: String,        // sha256（该文件内容的小写 SHA-256 摘要）。
}

/// RemoteImportManifest（远程导入清单）：服务端复核每个文件后才启动现有导入事务。
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RemoteImportManifest {
    pub source_id: String, // source_id（必须匹配服务端签发的暂存票据）。
    pub total_bytes: u64,  // total_bytes（所有文件的预期总字节数）。
    pub files: Vec<RemoteImportFileEntry>, // files（完整文件清单，路径均相对于暂存目录）。
}

impl RemoteImportManifest {
    /// validate（验证远程清单）：跨平台拒绝路径越界、碰撞、特殊名称和不一致长度。
    pub fn validate(&self) -> Result<(), String> {
        if !valid_source_id(&self.source_id) {
            return Err("远程导入暂存来源编号无效。".into());
        }
        if self.files.is_empty() || self.files.len() > REMOTE_IMPORT_MAX_FILES {
            return Err("远程导入文件数量必须在 1 至 4096 之间。".into());
        }
        if self.total_bytes == 0 || self.total_bytes > REMOTE_IMPORT_MAX_BYTES {
            return Err("远程导入总长度超出 1 字节至 4 GiB 的限制。".into());
        }

        let mut total = 0_u64;
        let mut paths = HashSet::with_capacity(self.files.len());
        for file in &self.files {
            validate_relative_path(&file.relative_path)?;
            if file.byte_length == 0 || file.byte_length > REMOTE_IMPORT_MAX_BYTES {
                return Err("远程导入文件长度无效或超过 4 GiB。".into());
            }
            if !valid_sha256(&file.sha256) {
                return Err("远程导入文件摘要必须是 64 位小写 SHA-256 十六进制值。".into());
            }
            let normalized = file.relative_path.to_lowercase();
            if !paths.insert(normalized.clone()) {
                return Err("远程导入清单包含重复或 Windows 大小写冲突路径。".into());
            }
            total = total
                .checked_add(file.byte_length)
                .ok_or_else(|| "远程导入总长度发生整数溢出。".to_owned())?;
            if total > REMOTE_IMPORT_MAX_BYTES {
                return Err("远程导入总长度超过 4 GiB。".into());
            }
        }
        for path in paths.iter() {
            let mut parent = path.as_str();
            while let Some((prefix, _)) = parent.rsplit_once('/') {
                if paths.contains(prefix) {
                    return Err("远程导入清单中的文件与目录路径冲突。".into());
                }
                parent = prefix;
            }
        }
        if total != self.total_bytes {
            return Err("远程导入清单总长度与文件长度之和不一致。".into());
        }
        Ok(())
    }
}

pub fn valid_source_id(value: &str) -> bool {
    value.len() == 32
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn validate_relative_path(value: &str) -> Result<(), String> {
    if value.is_empty()
        || value.len() > 1024
        || value.starts_with('/')
        || value.contains(['\\', ':', '\0'])
    {
        return Err("远程导入路径必须是 1 至 1024 字节的正斜线相对路径。".into());
    }
    for component in value.split('/') {
        if component.is_empty()
            || component == "."
            || component == ".."
            || component.ends_with(['.', ' '])
            || component
                .chars()
                .any(|character| character < ' ' || "<>\"|?*".contains(character))
        {
            return Err("远程导入路径含空段、目录穿越或 Windows 非法文件名。".into());
        }
        let stem = component
            .split('.')
            .next()
            .unwrap_or("")
            .to_ascii_uppercase();
        if matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
            || (stem.len() == 4
                && (stem.starts_with("COM") || stem.starts_with("LPT"))
                && stem.as_bytes()[3].is_ascii_digit()
                && (b'1'..=b'9').contains(&stem.as_bytes()[3]))
        {
            return Err("远程导入路径包含 Windows 保留设备名称。".into());
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn manifest(paths: &[(&str, u64)]) -> RemoteImportManifest {
        let files = paths
            .iter()
            .map(|(path, bytes)| RemoteImportFileEntry {
                relative_path: (*path).into(),
                byte_length: *bytes,
                sha256: "a".repeat(64),
            })
            .collect::<Vec<_>>();
        RemoteImportManifest {
            source_id: "0123456789abcdef0123456789abcdef".into(),
            total_bytes: files.iter().map(|file| file.byte_length).sum(),
            files,
        }
    }

    #[test]
    fn remote_import_manifest_accepts_normalized_relative_tree() {
        assert!(manifest(&[("models/vad.onnx", 10), ("README.txt", 3)])
            .validate()
            .is_ok());
    }

    #[test]
    fn remote_import_manifest_rejects_traversal_collisions_and_bad_totals() {
        for path in [
            "../escape",
            "C:/escape",
            "folder\\escape",
            "/root",
            "CON.txt",
            "folder/.",
        ] {
            assert!(manifest(&[(path, 1)]).validate().is_err(), "{path}");
        }
        assert!(manifest(&[("A/file", 1), ("a/FILE", 1)])
            .validate()
            .is_err());
        assert!(manifest(&[("a", 1), ("a/b", 1)]).validate().is_err());
        let mut wrong_total = manifest(&[("file.bin", 1)]);
        wrong_total.total_bytes = 2;
        assert!(wrong_total.validate().is_err());
    }
}
