fn main() {
    // 两个独立启动入口也嵌入相同的多尺寸图标，资源管理器与桌面快捷方式保持一致。
    println!("cargo:rerun-if-changed=app.rc");
    println!("cargo:rerun-if-changed=../src-tauri/icons/icon.ico");
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        embed_resource::compile("app.rc", embed_resource::NONE)
            .manifest_required()
            .expect("独立启动入口图标资源构建失败");
    }
}
