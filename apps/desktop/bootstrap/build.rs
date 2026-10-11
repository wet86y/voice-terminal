fn main() {
    // 两个独立启动入口也嵌入相同的多尺寸图标，资源管理器与桌面快捷方式保持一致。
    println!("cargo:rerun-if-changed=../src-tauri/icons/icon.ico");
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "desktop_status", "desktop_cancel_init", "exit_application",
        ]),
    )).expect("迁移辅助进程共享启动页权限构建失败");
}
