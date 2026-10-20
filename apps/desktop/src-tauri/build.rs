fn main() {
    // 应用图标资源改变后重新编译，确保标题栏与任务栏同时更新。
    println!("cargo:rerun-if-changed=icons/icon.ico");
    println!("cargo:rerun-if-changed=icons/icon.png");
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "desktop_initialize", "desktop_manage", "desktop_start_service", "desktop_cancel_init", "voice_environment_status", "voice_environment_plan", "resource_install_plan", "voice_environment_configure", "voice_environment_import", "gpu_environment_register", "gpu_environment_probe", "environment_guide",
            "desktop_status", "desktop_page_ready",
            // model_credentials（模型接口凭据）：业务服务与组件未就绪时也要生成读写权限。
            "model_credentials_status", "model_credentials_save",
            "tts_selection_status", "tts_selection_save", "audio_enhancements_settings", "resource_download_required",
            "skip_startup",
            "export_logs",
            "open_console",
            "exit_application",
            "focus_main",
            "publish_floating_model", "get_floating_model", "return_to_workbench", "start_floating_drag", "control_floating_session",
            "workspace_info", "workspace_choose", "workspace_link", "workspace_migrate",
            "app_update_check", "app_update_download", "app_update_cancel", "project_open_repository", "open_minimax_voice_library",
            "resource_inventory", "resource_jobs", "resource_download", "resource_import", "resource_import_confirm", "resource_open_source", "resource_cancel",
        ]),
    ))
    .expect("桌面权限清单构建失败");
}
