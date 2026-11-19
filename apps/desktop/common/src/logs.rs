//! Log archive（日志归档）：同步并打包仍由运行进程打开的日志文件。
use std::{
    fs::{self, File, OpenOptions},
    io::{self, Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

/// record_client_event（客户端边界事件）：只记录登记的标量元数据，4 KiB（单行字节）上限，4 MiB × 3（滚动文件）。
pub fn record_client_event(install_root: &Path, event: &str, details: &serde_json::Value) -> Result<(), String> {
    static LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
    let _guard = LOCK.lock().map_err(|_| "客户端日志写入锁不可用。")?;
    let directory = crate::workspace::safe_path(install_root, "cache/logs")?;
    fs::create_dir_all(&directory).map_err(|error| error.to_string())?;
    let path = crate::workspace::safe_path(install_root, "cache/logs/client.log")?;
    if path.metadata().is_ok_and(|value| value.len() >= 4 * 1024 * 1024) {
        let first = crate::workspace::safe_path(install_root, "cache/logs/client.log.1")?;
        let second = crate::workspace::safe_path(install_root, "cache/logs/client.log.2")?;
        if second.is_file() { fs::remove_file(&second).map_err(|error| error.to_string())?; }
        if first.is_file() { fs::rename(&first, &second).map_err(|error| error.to_string())?; }
        fs::rename(&path, &first).map_err(|error| error.to_string())?;
    }
    let mut record = serde_json::json!({
        "event":event.chars().take(96).collect::<String>(),
        "timestamp_ms":SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis() as u64,
        "role":"client", "fields":{"event":"客户端事件","timestamp_ms":"UTC（协调世界时）毫秒时间","role":"client（客户端）"}
    });
    for (key, label) in [
        ("device_id","设备编号"),("core_session_id","核心运行编号"),("build","程序构建"),
        ("version","程序版本"),("connection_generation","设备连接代次"),
        ("executor_generation","执行器代次"),("interaction_generation","交互占用代次"),
        ("stream_id","媒体连接编号"),("capture_stream_id","本轮输入流编号"),
        ("state","通道状态"),("reason","结束或失败原因"),("duration_ms","持续毫秒"),
        ("playback_key","播放事务编号"),("request_id","合成请求编号"),
        ("client_online_tts_local","客户端在线合成播放开关"),("audio_compression","音频压缩开关"),
        ("previous_client_online_tts_local","保存前的客户端在线合成开关"),
        ("previous_audio_compression","保存前的音频压缩开关"),("trigger","操作来源"),
        ("mode","运行模式"),("result","操作结果"),
    ] {
        let Some(value) = details.get(key) else { continue };
        if !value.is_string() && !value.is_number() && !value.is_boolean() && !value.is_null() { continue }
        record[key] = if let Some(value) = value.as_str() {
            serde_json::json!(value.chars().filter(|value| !value.is_control()).take(240).collect::<String>())
        } else { value.clone() };
        record["fields"][key] = serde_json::json!(label);
        if serde_json::to_vec(&record).map_err(|error| error.to_string())?.len() > 4095 {
            record.as_object_mut().unwrap().remove(key);
            record["fields"].as_object_mut().unwrap().remove(key);
        }
    }
    let mut bytes = serde_json::to_vec(&record).map_err(|error| error.to_string())?;
    bytes.push(b'\n');
    OpenOptions::new().create(true).append(true).open(path)
        .and_then(|mut file| file.write_all(&bytes)).map_err(|error| error.to_string())
}

/// record_device_channel_error（设备通道诊断）：固定日志名，最多保留两份约 1 MiB（兆二进制字节）日志。
pub fn record_device_channel_error(
    install_root: &Path,
    channel: &str,
    error: &str,
) -> Result<(), String> {
    static LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
    if !matches!(channel, "management" | "file") {
        return Err("设备日志通道无效。".into());
    }
    let _guard = LOCK.lock().map_err(|_| "设备日志写入锁不可用。")?;
    let directory = crate::workspace::safe_path(install_root, "cache/logs")?;
    fs::create_dir_all(directory).map_err(|error| error.to_string())?;
    let path = crate::workspace::safe_path(install_root, "cache/logs/device-channels.log")?;
    let backup = crate::workspace::safe_path(install_root, "cache/logs/device-channels.log.1")?;
    if path
        .metadata()
        .is_ok_and(|metadata| metadata.len() >= 1024 * 1024)
    {
        if backup.is_file() {
            fs::remove_file(&backup).map_err(|error| error.to_string())?;
        }
        fs::rename(&path, &backup).map_err(|error| error.to_string())?;
    }
    let reason = error
        .chars()
        .filter(|character| !character.is_control())
        .take(240)
        .collect::<String>();
    let record = serde_json::json!({
        "timestamp_ms":SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis() as u64,
        "channel":channel,"reason":reason,
        "fields":{"timestamp_ms":"UTC（协调世界时）毫秒时间","channel":"management（管理）或 file（文件）通道","reason":"连接结束原因，不含协议正文或凭据"}
    });
    let mut bytes = serde_json::to_vec(&record).map_err(|error| error.to_string())?;
    bytes.push(b'\n');
    OpenOptions::new()
        .append(true)
        .create(true)
        .open(path)
        .and_then(|mut file| file.write_all(&bytes))
        .map_err(|error| error.to_string())
}

/// archive_logs（日志打包）：读取活动日志的共享快照并保存为 ZIP（压缩包）。
pub fn archive_logs(log_directory: &Path, destination: &Path) -> Result<PathBuf, String> {
    let root = log_directory
        .canonicalize()
        .map_err(|error| format!("无法定位日志目录：{error}"))?;
    if !root.is_dir() {
        return Err("日志目录不可用。".into());
    }
    if !destination.is_dir() {
        return Err("所选保存位置不是文件夹。".into());
    }

    // entries（归档条目）：只收集本目录内的常规日志文件，不跟随链接或其他目录。
    let mut entries = Vec::new();
    for entry in fs::read_dir(&root).map_err(|error| format!("无法读取日志目录：{error}"))?
    {
        let entry = entry.map_err(|error| format!("无法读取日志目录内容：{error}"))?;
        if !entry
            .file_type()
            .map_err(|error| error.to_string())?
            .is_file()
        {
            continue;
        }
        let name = entry.file_name();
        if !is_log_file(&name.to_string_lossy()) {
            continue;
        }
        let path = entry.path();
        if path
            .canonicalize()
            .map_err(|error| error.to_string())?
            .starts_with(&root)
        {
            entries.push((name, path));
        }
    }
    entries.sort_by(|left, right| left.0.cmp(&right.0));

    if entries.is_empty() {
        return Err("当前没有可打包的日志文件。".into());
    }

    // file_name（压缩包文件名）：使用 UTC（协调世界时）时间，冲突时递增编号。
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let (archive_path, output) = create_archive_file(destination, timestamp)?;
    let result = write_archive(output, entries);
    if let Err(error) = result {
        let _ = fs::remove_file(&archive_path);
        return Err(error);
    }
    Ok(archive_path)
}

#[cfg(test)]
mod channel_tests {
    use super::*;

    #[test]
    fn client_diagnostics_exclude_payloads_bound_lines_and_rotate_into_the_archive() {
        let owner = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../.local/tmp");
        let root = owner.join(format!("client-log-{}", crate::workspace::new_id()));
        let details = serde_json::json!({"device_id":"device-log","reason":"原因\r\n".repeat(300),
            "credential":"must-never-appear","text":"private-transcript","body":{"credential":"secret"}});
        record_client_event(&root, "client.start", &details).unwrap();
        let path = root.join("cache/logs/client.log");
        let bytes = fs::read(&path).unwrap();
        assert!(bytes.len() <= 4096);
        let text = String::from_utf8(bytes).unwrap();
        assert!(!text.contains("must-never-appear") && !text.contains("private-transcript"));
        let record: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(record["fields"]["device_id"], "设备编号");
        assert!(!record["reason"].as_str().unwrap().chars().any(char::is_control));
        fs::write(&path, vec![b'x'; 4 * 1024 * 1024]).unwrap();
        record_client_event(&root, "client.media_closed", &serde_json::json!({"state":"stopped"})).unwrap();
        assert!(root.join("cache/logs/client.log.1").is_file());
        let archive = archive_logs(&root.join("cache/logs"), &root).unwrap();
        let mut zip = zip::ZipArchive::new(File::open(archive).unwrap()).unwrap();
        assert!(zip.by_name("client.log").is_ok());
        assert!(zip.by_name("client.log.1").is_ok());
        drop(zip);
        assert!(root.canonicalize().unwrap().starts_with(owner.canonicalize().unwrap()));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn channel_diagnostics_rotate_and_bound_control_free_reasons() {
        let owner = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../.local/tmp");
        let root = owner.join(format!("channel-log-{}", crate::workspace::new_id()));
        record_device_channel_error(&root, "management", "握手失败").unwrap();
        let path = root.join("cache/logs/device-channels.log");
        fs::write(&path, vec![b'x'; 1024 * 1024]).unwrap();
        record_device_channel_error(&root, "file", &format!("错误\r\n{}", "长".repeat(300)))
            .unwrap();
        let record: serde_json::Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        let reason = record["reason"].as_str().unwrap();
        assert_eq!(reason.chars().count(), 240);
        assert!(!reason.chars().any(char::is_control));
        assert_eq!(record["channel"], "file");
        assert_eq!(
            root.join("cache/logs/device-channels.log.1")
                .metadata()
                .unwrap()
                .len(),
            1024 * 1024
        );
        assert!(record_device_channel_error(&root, "../other", "拒绝外部通道").is_err());
        crate::workspace::remove_owned(&root, &owner).unwrap();
    }
}

fn is_log_file(name: &str) -> bool {
    let name = name.to_ascii_lowercase();
    name.contains(".log") || name.contains(".jsonl")
}

fn create_archive_file(destination: &Path, timestamp: u64) -> Result<(PathBuf, File), String> {
    for suffix in 0..1000u32 {
        let file_name = if suffix == 0 {
            format!("VoiceTerminal-logs-{timestamp}.zip")
        } else {
            format!("VoiceTerminal-logs-{timestamp}-{suffix}.zip")
        };
        let path = destination.join(file_name);
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(file) => return Ok((path, file)),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(format!("无法创建日志压缩包：{error}")),
        }
    }
    Err("无法生成不重复的日志压缩包文件名。".into())
}

fn write_archive(output: File, entries: Vec<(std::ffi::OsString, PathBuf)>) -> Result<(), String> {
    let mut archive = zip::ZipWriter::new(output);
    let options = zip::write::SimpleFileOptions::default()
        .compression_method(zip::CompressionMethod::Deflated);
    let mut archived = 0usize;
    for (name, path) in entries {
        // snapshot_length（快照长度）：限定为同步后的文件大小，不等待持续写入的日志结束。
        let (mut file, can_sync) = match OpenOptions::new().read(true).write(true).open(&path) {
            Ok(file) => (file, true),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(_) => match File::open(&path) {
                Ok(file) => (file, false),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                Err(error) => {
                    return Err(format!(
                        "无法打开日志文件 {}：{error}",
                        name.to_string_lossy()
                    ))
                }
            },
        };
        if can_sync {
            // file_sync（文件同步）：若活动写入句柄阻止刷新，仍读取由日志处理器已刷新过的内容。
            let _ = file.sync_all();
        }
        let length = file.metadata().map_err(|error| error.to_string())?.len();
        file.seek(SeekFrom::Start(0))
            .map_err(|error| error.to_string())?;
        let name = name.to_string_lossy().into_owned();
        archive
            .start_file(name, options)
            .map_err(|error| format!("无法写入日志压缩包：{error}"))?;
        let mut snapshot = file.take(length);
        io::copy(&mut snapshot, &mut archive)
            .map_err(|error| format!("无法写入日志压缩包：{error}"))?;
        archived += 1;
    }
    if archived == 0 {
        return Err("当前没有可打包的日志文件。".into());
    }
    archive
        .finish()
        .map_err(|error| format!("无法完成日志压缩包：{error}"))?
        .sync_all()
        .map_err(|error| format!("无法刷新日志压缩包：{error}"))
}
