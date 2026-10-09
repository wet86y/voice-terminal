use std::{env,fs,path::PathBuf};
fn main(){
    println!("cargo:rerun-if-env-changed=VOICE_PROGRAM_PACKAGE");
    let path=env::var_os("VOICE_PROGRAM_PACKAGE").map(PathBuf::from);
    let output=PathBuf::from(env::var_os("OUT_DIR").unwrap()).join("program.zip");
    if let Some(path)=path {println!("cargo:rerun-if-changed={}",path.display());fs::copy(path,output).expect("复制统一正式程序包失败");}else{fs::write(output,[]).unwrap();}
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(tauri_build::AppManifest::new().commands(&["desktop_status","desktop_initialize","desktop_cancel_init","exit_application"]))).expect("安装器命令权限构建失败");
}
