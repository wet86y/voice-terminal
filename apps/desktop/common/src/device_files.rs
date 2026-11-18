//! Device files（设备文件）：安全列举目录并读取有界文件字节，不跨任务通道传输大文件。
use base64::Engine;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Component, Path, PathBuf},
};

const MAX_READ_BYTES: usize = 128 * 1024; // MAX_READ_BYTES（控制任务通道单文件读取上限）。
const MAX_LIST_ENTRIES: usize = 200; // MAX_LIST_ENTRIES（单次目录清单返回上限）。

/// DeviceFileManager（设备文件管理器）：所有操作都须先由 DSH（代理执行宿主）用户授权。
#[derive(Clone, Copy, Default)]
pub struct DeviceFileManager;

impl DeviceFileManager {
    /// execute（执行文件任务）：路径必须绝对且各级现存项不能是链接。
    pub fn execute(
        &self,
        operation: &str,
        arguments: &Value,
        connection_generation: u64,
        executor_generation: u64,
    ) -> Result<Value, String> {
        if connection_generation == 0 || executor_generation == 0 {
            return Err("文件任务缺少有效连接或执行器代次。".into());
        }
        let args = arguments
            .as_object()
            .ok_or("文件任务参数必须是 JSON（结构化数据）对象。")?;
        match operation {
            "file.list" => {
                exact_fields(args, &["path", "limit"])?;
                let path = required_path(args.get("path"))?;
                let limit = args
                    .get("limit")
                    .and_then(Value::as_u64)
                    .ok_or("limit（目录项上限）无效。")? as usize;
                if !(1..=MAX_LIST_ENTRIES).contains(&limit) {
                    return Err("目录项上限必须在 1 至 200 之间。".into());
                }
                self.list(path, limit)
            }
            "file.read" => {
                exact_fields(args, &["path", "max_bytes"])?;
                let path = required_path(args.get("path"))?;
                let max_bytes =
                    args.get("max_bytes")
                        .and_then(Value::as_u64)
                        .ok_or("max_bytes（最大字节数）无效。")? as usize;
                if !(1..=MAX_READ_BYTES).contains(&max_bytes) {
                    return Err("单个控制通道文件读取上限为 128 KiB。".into());
                }
                self.read(path, max_bytes)
            }
            "file.stat" => {
                exact_fields(args, &["path"])?;
                let path = required_path(args.get("path"))?;
                self.stat(path)
            }
            _ => Err("设备文件操作不在当前受限清单中。".into()),
        }
    }

    fn list(&self, path: &Path, limit: usize) -> Result<Value, String> {
        reject_reparse_chain(path)?;
        let metadata = fs::metadata(path).map_err(|_| "无法读取已授权目录。".to_string())?;
        if !metadata.is_dir() {
            return Err("file.list 目标必须是目录。".into());
        }
        let mut entries = Vec::new();
        for entry in fs::read_dir(path).map_err(|_| "已授权目录无法列举。".to_string())? {
            if entries.len() > limit {
                break;
            }
            let entry = entry.map_err(|_| "目录清单包含无法读取的项。".to_string())?;
            let name = entry
                .file_name()
                .to_string_lossy()
                .chars()
                .take(240)
                .collect::<String>();
            let item_path = entry.path();
            let item_metadata =
                fs::symlink_metadata(&item_path).map_err(|_| "目录项状态读取失败。".to_string())?;
            let reparse = crate::workspace::metadata_reparse(&item_metadata);
            entries.push(json!({
                "name": name,
                "kind": if reparse { "link" } else if item_metadata.is_dir() { "directory" } else if item_metadata.is_file() { "file" } else { "other" },
                "bytes": if !reparse && item_metadata.is_file() { Some(item_metadata.len()) } else { None },
                "fields":{"name":"文件或目录名称，已截断","kind":"条目类型；链接不会被跟随","bytes":"普通文件字节数；其他类型为空"}
            }));
        }
        let truncated = entries.len() > limit;
        entries.truncate(limit);
        Ok(
            json!({"path":path.to_string_lossy(),"entries":entries,"truncated":truncated,
            "fields":{"path":"已校验的绝对目录路径","entries":"不跟随链接的有界目录清单","truncated":"达到目录项上限时为 true"}}),
        )
    }

    fn read(&self, path: &Path, max_bytes: usize) -> Result<Value, String> {
        reject_reparse_chain(path)?;
        let before = fs::symlink_metadata(path).map_err(|_| "无法读取已授权文件。".to_string())?;
        if crate::workspace::metadata_reparse(&before) || !before.is_file() {
            return Err("file.read 目标必须是普通文件，不能是目录或链接。".into());
        }
        if before.len() > max_bytes as u64 || before.len() > MAX_READ_BYTES as u64 {
            return Err(
                "文件超过当前控制通道读取上限；大文件须使用独立 SFTP（安全文件传输）任务。".into(),
            );
        }
        let bytes = fs::read(path).map_err(|_| "已授权文件读取失败。".to_string())?;
        let after = fs::symlink_metadata(path).map_err(|_| "读取期间文件状态改变。".to_string())?;
        if crate::workspace::metadata_reparse(&after)
            || !after.is_file()
            || after.len() != bytes.len() as u64
        {
            return Err("文件读取期间路径类型或长度发生变化。".into());
        }
        let digest = Sha256::digest(&bytes)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        let name = path
            .file_name()
            .map(|value| {
                value
                    .to_string_lossy()
                    .chars()
                    .take(240)
                    .collect::<String>()
            })
            .ok_or("文件名无效。")?;
        Ok(
            json!({"path":path.to_string_lossy(),"name":name,"bytes":bytes.len(),"sha256":digest,
            "data_base64":base64::engine::general_purpose::STANDARD.encode(&bytes),
            "fields":{"path":"已授权且复核的绝对路径","name":"文件名，不是路径","bytes":"读取字节数",
                "sha256":"原始字节 SHA-256 摘要","data_base64":"仅用于受限小文件任务的标准 Base64 内容"}}),
        )
    }

    fn stat(&self, path: &Path) -> Result<Value, String> {
        reject_reparse_chain(path)?;
        let metadata =
            fs::symlink_metadata(path).map_err(|_| "无法读取已授权文件状态。".to_string())?;
        if crate::workspace::metadata_reparse(&metadata) || !metadata.is_file() {
            return Err("file.stat 目标必须是普通文件，不能是目录或链接。".into());
        }
        let name = path
            .file_name()
            .map(|value| {
                value
                    .to_string_lossy()
                    .chars()
                    .take(240)
                    .collect::<String>()
            })
            .ok_or("文件名无效。")?;
        Ok(
            json!({"path":path.to_string_lossy(),"name":name,"bytes":metadata.len(),
            "fields":{"path":"已校验的绝对文件路径","name":"文件名，不是路径","bytes":"普通文件字节数"}}),
        )
    }
}

fn exact_fields(args: &serde_json::Map<String, Value>, fields: &[&str]) -> Result<(), String> {
    if args.len() != fields.len() || args.keys().any(|key| !fields.contains(&key.as_str())) {
        return Err("文件任务参数不符合严格字段结构。".into());
    }
    Ok(())
}

fn required_path(value: Option<&Value>) -> Result<&Path, String> {
    let raw = value
        .and_then(Value::as_str)
        .ok_or("path（目标路径）无效。")?;
    if raw.is_empty() || raw.len() > 2048 || raw.chars().any(char::is_control) {
        return Err("path（目标路径）为空、超长或含控制字符。".into());
    }
    let path = Path::new(raw);
    if !path.is_absolute()
        || path
            .components()
            .any(|item| matches!(item, Component::ParentDir | Component::CurDir))
    {
        return Err("文件任务只接受绝对路径，且不能含 . 或 .. 路径段。".into());
    }
    reject_reparse_chain(path)?;
    Ok(path)
}

pub(crate) fn reject_reparse_chain(path: &Path) -> Result<(), String> {
    if !path.is_absolute() {
        return Err("文件路径必须是绝对路径。".into());
    }
    let mut current = PathBuf::new();
    for component in path.components() {
        match component {
            Component::Prefix(_) | Component::RootDir => current.push(component.as_os_str()),
            Component::Normal(_) => {
                current.push(component.as_os_str());
                if let Ok(metadata) = fs::symlink_metadata(&current) {
                    if crate::workspace::metadata_reparse(&metadata) {
                        return Err("文件路径经过链接或重解析点，已拒绝。".into());
                    }
                }
            }
            Component::CurDir | Component::ParentDir => {
                return Err("文件路径不能包含 . 或 .. 路径段。".into())
            }
        }
    }
    Ok(())
}

/// validate_transfer_source（校验 SFTP 源文件）：只接受普通文件且不超过登记上限。
pub(crate) fn validate_transfer_source(path: &Path, maximum_bytes: u64) -> Result<u64, String> {
    reject_reparse_chain(path)?;
    let metadata = fs::symlink_metadata(path).map_err(|_| "文件传输源无法读取。".to_string())?;
    if crate::workspace::metadata_reparse(&metadata) || !metadata.is_file() {
        return Err("文件传输源必须是普通文件，不能是目录或链接。".into());
    }
    if metadata.len() > maximum_bytes {
        return Err("文件传输源超过已授权的传输上限。".into());
    }
    Ok(metadata.len())
}

/// validate_transfer_target_parent（校验 SFTP 下载目标）：父目录必须已存在且不能经过链接。
pub(crate) fn validate_transfer_target_parent(path: &Path) -> Result<(), String> {
    if !path.is_absolute() || path.file_name().is_none() {
        return Err("文件传输目标必须是绝对文件路径。".into());
    }
    let parent = path.parent().ok_or("文件传输目标没有父目录。")?;
    reject_reparse_chain(parent)?;
    let metadata = fs::metadata(parent).map_err(|_| "文件传输目标父目录无法读取。".to_string())?;
    if !metadata.is_dir() {
        return Err("文件传输目标父路径不是目录。".into());
    }
    if let Ok(existing) = fs::symlink_metadata(path) {
        if crate::workspace::metadata_reparse(&existing) || !existing.is_file() {
            return Err("文件传输目标已存在且不是可替换普通文件。".into());
        }
        return Err("文件传输目标已存在；默认禁止覆盖。".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn file_operations_require_absolute_paths_and_reject_large_reads() {
        let manager = DeviceFileManager;
        assert!(manager
            .execute(
                "file.read",
                &json!({"path":"relative.txt","max_bytes":128}),
                1,
                1
            )
            .is_err());
        assert!(manager
            .execute(
                "file.read",
                &json!({"path":"C:\\temp\\file.txt","max_bytes":MAX_READ_BYTES + 1}),
                1,
                1
            )
            .is_err());
        assert!(manager
            .execute("file.write", &json!({"path":"C:\\temp\\file.txt"}), 1, 1)
            .is_err());
    }

    #[test]
    fn file_read_and_list_return_bounded_hash_checked_data() {
        let root = std::env::temp_dir().join(format!(
            "voice-device-files-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir(&root).unwrap();
        let path = root.join("sample.bin");
        let bytes = [0_u8, 255, 7, 19];
        fs::write(&path, bytes).unwrap();
        let manager = DeviceFileManager;
        let read = manager
            .execute(
                "file.read",
                &json!({"path":path.to_string_lossy(),"max_bytes":128}),
                1,
                1,
            )
            .unwrap();
        assert_eq!(read["bytes"], bytes.len());
        let expected_hash = Sha256::digest(bytes)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        assert_eq!(read["sha256"], expected_hash);
        assert_eq!(
            read["data_base64"],
            base64::engine::general_purpose::STANDARD.encode(bytes)
        );
        let listing = manager
            .execute(
                "file.list",
                &json!({"path":root.to_string_lossy(),"limit":10}),
                1,
                1,
            )
            .unwrap();
        assert_eq!(listing["entries"][0]["name"], "sample.bin");
        assert_eq!(listing["truncated"], false);
        fs::remove_dir_all(root).unwrap();
    }
}
