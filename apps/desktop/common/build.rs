use std::{env, fs, path::PathBuf};

fn main() {
    let repo = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap()).join("../../..");
    let output = PathBuf::from(env::var("OUT_DIR").unwrap());
    if env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("windows") { return; }
    let kit = repo.join(".local/build/update-kit/native");
    let transport_path = kit.join("src/UpdateKit.cpp");
    let source = fs::read_to_string(&transport_path)
        .expect("先运行 scripts/prepare-update-kit.ps1（下载组件准备）");
    // 仅允许测试发布源的 IPv4（网络地址）回环 HTTP（本地明文传输）；正式资源仍为 HTTPS（加密传输）。
    let source = source.replace("parts.nScheme != INTERNET_SCHEME_HTTPS)",
        "(parts.nScheme != INTERNET_SCHEME_HTTPS && !(parts.nScheme == INTERNET_SCHEME_HTTP && std::wstring(parts.lpszHostName, parts.dwHostNameLength) == L\"127.0.0.1\")))")
        .replace("WINHTTP_DEFAULT_ACCEPT_TYPES, WINHTTP_FLAG_SECURE)",
                 "WINHTTP_DEFAULT_ACCEPT_TYPES, parts.nScheme == INTERNET_SCHEME_HTTPS ? WINHTTP_FLAG_SECURE : 0)")
        .replace("std::min<std::size_t>(buffer.size(), available)",
                 "std::min<std::size_t>(std::min<std::size_t>(buffer.size(), available), input.max_body_bytes ? static_cast<std::size_t>(*input.max_body_bytes - delivered) : buffer.size())");
    let patched = output.join("UpdateKitTransport.cpp");
    fs::write(&patched, source).expect("生成固定下载组件适配失败");
    println!("cargo:rerun-if-changed={}", transport_path.display());
    println!("cargo:rerun-if-changed={}", repo.join("integrations/downloader/resource_download.cpp").display());
    let static_crt = env::var("CARGO_CFG_TARGET_FEATURE").unwrap_or_default().contains("crt-static");
    cc::Build::new().cpp(true).std("c++20").static_crt(static_crt)
        .define("UNICODE", None).define("_UNICODE", None).define("NOMINMAX", None)
        .define("WIN32_LEAN_AND_MEAN", None).flag_if_supported("/utf-8")
        .include(kit.join("include")).include(kit.join("src"))
        .file(patched).file(kit.join("src/Json.cpp"))
        .file(repo.join("integrations/downloader/resource_download.cpp"))
        .compile("voice_resource_download");
    println!("cargo:rustc-link-lib=bcrypt");
    println!("cargo:rustc-link-lib=winhttp");
}
