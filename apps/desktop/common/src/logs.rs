//! Log archive（日志归档）：同步并打包仍由运行进程打开的日志文件。
use std::{
    fs::{self, File, OpenOptions},
    io::{self, Read, Seek, SeekFrom},
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

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
    for entry in fs::read_dir(&root).map_err(|error| format!("无法读取日志目录：{error}"))? {
        let entry = entry.map_err(|error| format!("无法读取日志目录内容：{error}"))?;
        if !entry.file_type().map_err(|error| error.to_string())?.is_file() {
            continue;
        }
        let name = entry.file_name();
        if !is_log_file(&name.to_string_lossy()) {
            continue;
        }
        let path = entry.path();
        if path.canonicalize().map_err(|error| error.to_string())?.starts_with(&root) {
            entries.push((name, path));
        }
    }
    entries.sort_by(|left, right| left.0.cmp(&right.0));

    if entries.is_empty() {
        return Err("当前没有可打包的日志文件。".into());
    }

    // file_name（压缩包文件名）：使用 UTC（协调世界时）时间，冲突时递增编号。
    let timestamp = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
    let (archive_path, output) = create_archive_file(destination, timestamp)?;
    let result = write_archive(output, entries);
    if let Err(error) = result {
        let _ = fs::remove_file(&archive_path);
        return Err(error);
    }
    Ok(archive_path)
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
                Err(error) => return Err(format!("无法打开日志文件 {}：{error}", name.to_string_lossy())),
            },
        };
        if can_sync {
            // file_sync（文件同步）：若活动写入句柄阻止刷新，仍读取由日志处理器已刷新过的内容。
            let _ = file.sync_all();
        }
        let length = file.metadata().map_err(|error| error.to_string())?.len();
        file.seek(SeekFrom::Start(0)).map_err(|error| error.to_string())?;
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
