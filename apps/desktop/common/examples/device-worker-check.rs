//! 设备工作进程本机诊断：以自身模拟产品入口，验证受管启动和释放。
#[cfg(windows)]
fn main() {
    if let Some(result) = voice_desktop_common::device_worker::dispatch() {
        if let Err(error) = result {
            eprintln!("{error}");
            std::process::exit(1);
        }
        return;
    }
    let mut args = std::env::args_os().skip(1);
    let mut worker_executable = None; // worker_executable（待验收产品可执行文件）。
    let mut helper_script = None; // helper_script（受管 DSH 桌面辅助脚本）。
    let mut audio_smoke = false; // audio_smoke（是否执行获准的短时录放音检查）。
    let mut expect_helper_unavailable = false; // expect_helper_unavailable（是否验收无交互会话时的快速拒绝）。
    while let Some(argument) = args.next() {
        if argument == "--audio-smoke" {
            audio_smoke = true;
        } else if argument == "--expect-helper-unavailable" {
            expect_helper_unavailable = true;
        } else if argument == "--worker-executable" {
            let Some(path) = args.next() else {
                eprintln!("--worker-executable 后须提供产品可执行文件路径。");
                std::process::exit(64);
            };
            worker_executable = Some(std::path::PathBuf::from(path));
        } else if argument == "--helper-script" {
            let Some(path) = args.next() else {
                eprintln!("--helper-script 后须提供已构建的桌面辅助脚本路径。");
                std::process::exit(64);
            };
            helper_script = Some(std::path::PathBuf::from(path));
        } else {
            eprintln!("用法：device-worker-check [--worker-executable <产品程序路径>] [--helper-script <脚本路径>] [--expect-helper-unavailable] [--audio-smoke]");
            std::process::exit(64);
        }
    }
    if expect_helper_unavailable && helper_script.is_none() {
        eprintln!("--expect-helper-unavailable 必须与 --helper-script 一起使用。");
        std::process::exit(64);
    }
    let worker = match (worker_executable.as_deref(), helper_script.as_deref()) {
        (Some(executable), Some(helper)) => {
            voice_desktop_common::device_worker::ManagedWorker::start_with_executable_and_helper(
                executable,
                Some(helper),
            )
        }
        (Some(executable), None) => {
            voice_desktop_common::device_worker::ManagedWorker::start_with_executable(executable)
        }
        (None, Some(helper)) => {
            voice_desktop_common::device_worker::ManagedWorker::start_with_helper_path(Some(helper))
        }
        (None, None) => voice_desktop_common::device_worker::ManagedWorker::start(),
    };
    let mut worker = worker.expect("设备工作进程启动失败");
    let status = worker.status().expect("设备工作进程状态不可用");
    assert_eq!(status["state"], "ready");
    if helper_script.is_some() {
        let report = worker
            .desktop_helper_health()
            .expect("桌面辅助进程健康请求失败");
        let session_zero_error = report["data"]["error"]
            .as_str()
            .map(|error| error.contains("Session 0"))
            .unwrap_or(false);
        if expect_helper_unavailable
            && report["ok"] == false
            && report["state"] == "unavailable"
            && session_zero_error
        {
            println!("DEVICE_HELPER_UNAVAILABLE={report}");
        } else if report["ok"] == true && report["state"] == "ready" && !expect_helper_unavailable {
            println!("DEVICE_HELPER_HEALTH={report}");
        } else {
            eprintln!("桌面辅助进程健康检查失败：{report}");
            worker.stop();
            std::process::exit(3);
        }
    }
    if audio_smoke {
        let result = audio_smoke_test(&mut worker);
        worker.stop();
        match result {
            Ok(report) => println!("DEVICE_AUDIO_SMOKE={report}"),
            Err(error) => {
                eprintln!("设备音频验收失败：{error}");
                std::process::exit(2);
            }
        }
        return;
    }
    worker.stop();
    assert!(worker.status().is_err());
    println!("设备工作进程认证、状态和释放通过。");
}

#[cfg(windows)]
fn audio_smoke_test(
    worker: &mut voice_desktop_common::device_worker::ManagedWorker,
) -> Result<serde_json::Value, String> {
    let endpoints = worker.audio_devices()?;
    worker.capture_start()?;
    let run: Result<serde_json::Value, String> = (|| {
        worker.playback_start()?;
        let mut previous_sequence: Option<u64> = None;
        let mut previous_sample: Option<u64> = None;
        let mut gaps = 0_u64;
        let mut ready_frames = 0_u64;
        let mut protected_frames = 0_u64;
        let mut first_frame: Option<serde_json::Value> = None;
        let mut last_frame: Option<serde_json::Value> = None;
        for packet in 0..10_u32 {
            let samples: Vec<i16> = (0..960)
                .map(|sample| {
                    let absolute = (packet as usize * 960 + sample) as f32;
                    (absolute * 440.0 * 2.0 * std::f32::consts::PI / 48_000.0)
                        .sin()
                        .mul_add(900.0, 0.0) as i16
                })
                .collect();
            let bytes: Vec<u8> = samples
                .iter()
                .flat_map(|sample| sample.to_le_bytes())
                .collect();
            worker.playback_enqueue(48_000, &bytes)?;
            let frame = worker
                .capture_next(100)?
                .ok_or_else(|| "100 毫秒内没有收到默认麦克风采集帧".to_owned())?;
            let sequence = frame["sequence"].as_u64().ok_or("采集帧缺少 sequence")?;
            let start_sample = frame["start_sample"]
                .as_u64()
                .ok_or("采集帧缺少 start_sample")?;
            if let (Some(previous), Some(previous_start)) = (previous_sequence, previous_sample) {
                if sequence != previous + 1 || start_sample != previous_start + 320 {
                    gaps = gaps.saturating_add(1);
                }
            }
            previous_sequence = Some(sequence);
            previous_sample = Some(start_sample);
            if frame["audio_processing"]["state"] == "ready" {
                ready_frames = ready_frames.saturating_add(1);
            }
            if frame["audio_processing"]["protected_interruption_available"] == true {
                protected_frames = protected_frames.saturating_add(1);
            }
            if first_frame.is_none() {
                first_frame = Some(frame.clone());
            }
            last_frame = Some(frame);
        }
        let playback = worker.playback_snapshot()?;
        Ok(serde_json::json!({
            "endpoints":endpoints,
            "capture_frames":10,
            "capture_gaps":gaps,
            "ready_processing_frames":ready_frames,
            "protected_interruption_frames":protected_frames,
            "first_frame_processing":first_frame.unwrap_or(serde_json::Value::Null)["audio_processing"],
            "last_frame_processing":last_frame.unwrap_or(serde_json::Value::Null)["audio_processing"],
            "playback_snapshot":playback,
            "tone":{"sample_rate":48_000,"duration_ms":200,"frequency_hz":440,"amplitude_i16":900},
            "fields":{"endpoints":"默认采集与播放端点信息","capture_frames":"读取的 20 毫秒麦克风帧数量","capture_gaps":"连续序号或采样位置缺口计数","ready_processing_frames":"AEC、降噪与 VAD 健康帧数量","protected_interruption_frames":"具有新鲜参考及有效延迟证据的帧数量","first_frame_processing":"首帧前处理状态","last_frame_processing":"末帧前处理状态","playback_snapshot":"WASAPI 播放时钟和缓冲快照","tone":"短时低音量验收音调参数；不写入音频文件"}
        }))
    })();
    let playback_stop = worker.playback_stop();
    let capture_stop = worker.capture_stop();
    let report = run?;
    playback_stop?;
    capture_stop?;
    Ok(report)
}

#[cfg(not(windows))]
fn main() {
    eprintln!("设备工作进程受管启动诊断需要 Windows（操作系统）。");
}
