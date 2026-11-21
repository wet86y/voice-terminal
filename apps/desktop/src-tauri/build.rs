include!("src/commands.rs");

// command_manifest_from_registry（从正式注册表生成权限）：调试目录不维护第二份命令名。
macro_rules! command_manifest_from_registry {
    ($($command:path),* $(,)?) => {{
        let commands = vec![$(stringify!($command).rsplit("::").next().unwrap().trim()),*];
        // 构建进程内保留静态切片，供正式权限生成器读取。
        command_manifest(Box::leak(commands.into_boxed_slice()))
    }};
}

fn main() {
    println!("cargo:rerun-if-changed=src/commands.rs");
    // 应用图标资源改变后重新编译，确保标题栏与任务栏同时更新。
    println!("cargo:rerun-if-changed=icons/icon.ico");
    println!("cargo:rerun-if-changed=icons/icon.png");
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(desktop_command_registry!(command_manifest_from_registry)),
    )
    .expect("桌面权限清单构建失败");
}

fn command_manifest(commands: &'static [&'static str]) -> tauri_build::AppManifest {
    // 代理调试目录与正式权限生成使用同一个原生命令声明。
    std::fs::write(
        std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap()).join("desktop-commands.txt"),
        commands.join("\n"),
    )
    .expect("无法写入正式命令目录");
    tauri_build::AppManifest::new().commands(commands)
}
