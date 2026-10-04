fn main() {
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
