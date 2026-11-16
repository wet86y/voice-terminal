use std::{env, fs, path::PathBuf};

fn collect_files(directory: &std::path::Path, files: &mut Vec<PathBuf>) {
    for entry in fs::read_dir(directory).unwrap_or_else(|error| {
        panic!(
            "无法读取固定原生 VAD 源码目录 {}：{error}",
            directory.display()
        )
    }) {
        let entry = entry.expect("无法读取原生 VAD 源码目录项");
        let path = entry.path();
        if path.is_dir() {
            collect_files(&path, files);
        } else if path.is_file() {
            files.push(path);
        }
    }
}

fn main() {
    let repo = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap()).join("../../..");
    let output = PathBuf::from(env::var("OUT_DIR").unwrap());
    if env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("windows") {
        return;
    }
    let static_crt = env::var("CARGO_CFG_TARGET_FEATURE")
        .unwrap_or_default()
        .contains("crt-static");
    let common = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap());
    let vad_root = common.join("native/webrtcvad/cbits");
    let mut vad_files = Vec::new();
    collect_files(&vad_root, &mut vad_files);
    vad_files.sort();
    let mut vad_build = cc::Build::new();
    vad_build
        .static_crt(
            env::var("CARGO_CFG_TARGET_FEATURE")
                .unwrap_or_default()
                .contains("crt-static"),
        )
        .include(&vad_root)
        .define("_WIN32", None);
    for file in &vad_files {
        println!("cargo:rerun-if-changed={}", file.display());
        if file.extension().is_some_and(|extension| extension == "c") {
            vad_build.file(file);
        }
    }
    vad_build.compile("voice_webrtcvad");

    if env::var_os("CARGO_FEATURE_NATIVE_AUDIO").is_some() {
        if !static_crt {
            panic!("native-audio（原生音频）需与桌面产品的 +crt-static（静态 C/C++ 运行库）配置一起构建");
        }
        let aec_install = repo.join(".local/build/audio/aec-native-static-install");
        let aec_source = repo.join(
            ".local/build/audio/aec-audio-processing-1.0.1/source/aec_audio_processing-1.0.1/webrtc-audio-processing",
        );
        let aec_library = aec_install.join("lib/libwebrtc-audio-processing-2.a");
        if !aec_library.is_file() || !aec_source.join("meson.build").is_file() {
            panic!("固定 AEC 源码或静态库缺失；先运行 scripts/prepare-native-audio.ps1（原生音频准备）");
        }
        let wrapper = common.join("native/webrtc_aec/aec_ffi.cpp");
        println!("cargo:rerun-if-changed={}", wrapper.display());
        println!("cargo:rerun-if-changed={}", aec_library.display());
        println!("cargo:rerun-if-changed={}", aec_source.display());
        cc::Build::new()
            .cpp(true)
            .std("c++20")
            .static_crt(static_crt)
            .define("UNICODE", None)
            .define("_UNICODE", None)
            .define("NOMINMAX", None)
            .define("WIN32_LEAN_AND_MEAN", None)
            .define("WEBRTC_WIN", None)
            .define("WEBRTC_AUDIO_PROCESSING_ONLY", None)
            .flag_if_supported("/utf-8")
            .flag_if_supported("/EHsc")
            .include(common.join("native/webrtc_aec"))
            .include(aec_source.join("webrtc"))
            .include(&aec_source)
            .include(aec_source.join("subprojects/abseil-cpp-20240722.0"))
            .include(aec_install.join("include/webrtc-audio-processing-2"))
            .file(&wrapper)
            .compile("voice_native_aec");
        println!(
            "cargo:rustc-link-search=native={}",
            aec_install.join("lib").display()
        );
        println!("cargo:rustc-link-lib=static:+verbatim=libwebrtc-audio-processing-2.a");
        println!("cargo:rustc-link-lib=winmm");

        let opus_install = repo.join(".local/build/audio/opus-static-install");
        let opus_library = opus_install.join("lib/opus.lib");
        let opus_runtime = repo.join(".local/build/audio/opus-shared-install/bin/opus.dll");
        if !opus_library.is_file() || !opus_runtime.is_file() {
            panic!("固定 libopus 1.6.1 静态库或 Python 服务端共享库缺失；先运行 scripts/prepare-libopus.ps1（固定 Opus 准备）");
        }
        println!("cargo:rerun-if-changed={}", opus_library.display());
        println!("cargo:rerun-if-changed={}", opus_runtime.display());
        println!(
            "cargo:rustc-link-search=native={}",
            opus_install.join("lib").display()
        );
        let opus_wrapper = common.join("native/opus/opus_ffi.c");
        println!("cargo:rerun-if-changed={}", opus_wrapper.display());
        cc::Build::new()
            .static_crt(static_crt)
            .flag_if_supported("/utf-8")
            .include(opus_install.join("include"))
            .file(&opus_wrapper)
            .compile("voice_opus_ffi");
        println!("cargo:rustc-link-lib=static=opus");
    }

    let kit = repo.join(".local/build/update-kit/native");
    let transport_path = kit.join("src/UpdateKit.cpp");
    let source = fs::read_to_string(&transport_path)
        .expect("先运行 scripts/prepare-update-kit.ps1（下载组件准备）");
    // 仅允许测试发布源的 IPv4（网络地址）回环 HTTP（本地明文传输）；正式资源仍为 HTTPS（加密传输）。
    let mut source = source.replace("parts.nScheme != INTERNET_SCHEME_HTTPS)",
        "(parts.nScheme != INTERNET_SCHEME_HTTPS && !(parts.nScheme == INTERNET_SCHEME_HTTP && std::wstring(parts.lpszHostName, parts.dwHostNameLength) == L\"127.0.0.1\")))")
        .replace("WINHTTP_DEFAULT_ACCEPT_TYPES, WINHTTP_FLAG_SECURE)",
                 "WINHTTP_DEFAULT_ACCEPT_TYPES, parts.nScheme == INTERNET_SCHEME_HTTPS ? WINHTTP_FLAG_SECURE : 0)")
        .replace("std::min<std::size_t>(buffer.size(), available)",
                 "std::min<std::size_t>(std::min<std::size_t>(buffer.size(), available), input.max_body_bytes ? static_cast<std::size_t>(*input.max_body_bytes - delivered) : buffer.size())");
    // locked_kit_transfer（锁定源码桥接）：同编译单元复用上游已有四路算法，锁定源码保持不变。
    source.push_str("\n#include \"locked_kit_transfer.inl\"\n");
    source.push_str("\n#include \"locked_kit_nodes.inl\"\n");
    let patched = output.join("UpdateKitTransport.cpp");
    fs::write(&patched, source).expect("生成固定下载组件适配失败");
    println!("cargo:rerun-if-changed={}", transport_path.display());
    println!(
        "cargo:rerun-if-changed={}",
        repo.join("integrations/downloader/resource_download.cpp")
            .display()
    );
    for path in [
        "integrations/downloader/locked_kit_transfer.h",
        "integrations/downloader/locked_kit_transfer.inl",
        "integrations/downloader/locked_kit_nodes.inl",
        "config/update-kit.lock.json",
    ] {
        println!("cargo:rerun-if-changed={}", repo.join(path).display());
    }
    let mut build = cc::Build::new();
    if env::var_os("CARGO_FEATURE_QUALIFICATION").is_some() {
        build.define("VOICE_DOWNLOAD_QUALIFICATION", None);
    }
    build
        .cpp(true)
        .std("c++20")
        .static_crt(static_crt)
        .define("UNICODE", None)
        .define("_UNICODE", None)
        .define("NOMINMAX", None)
        .define("WIN32_LEAN_AND_MEAN", None)
        .flag_if_supported("/utf-8")
        // /EHsc（标准异常展开）：下载失败或取消时释放文件和网络句柄，允许立即重试。
        .flag_if_supported("/EHsc")
        .include(kit.join("include"))
        .include(kit.join("src"))
        .include(repo.join("integrations/downloader"))
        .file(patched)
        .file(kit.join("src/Json.cpp"))
        .file(repo.join("integrations/downloader/resource_download.cpp"))
        .compile("voice_resource_download");
    println!("cargo:rustc-link-lib=bcrypt");
    println!("cargo:rustc-link-lib=winhttp");
}
