fn main() {
    // 应用图标资源改变后重新编译，确保标题栏与任务栏同时更新。
    println!("cargo:rerun-if-changed=icons/icon.ico");
    println!("cargo:rerun-if-changed=icons/icon.png");
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "desktop_status",
            "open_logs",
            "open_console",
            "exit_application",
            "focus_main",
        ]),
    ))
    .expect("桌面权限清单构建失败");
}
