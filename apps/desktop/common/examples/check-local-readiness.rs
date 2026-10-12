//! 本地验收：复用桌面原生资源校验，仅输出就绪信息，不读取或输出密钥正文。
use std::path::PathBuf;
use voice_desktop_common::{LaunchContext,credentials,resources};
fn main() {
    let root=PathBuf::from(std::env::args().nth(1).expect("需要安装目录"));
    let context=LaunchContext::portable(&root).expect("启动上下文不可用");
    let readiness=resources::startup_readiness(&context).expect("资源检查失败");
    let status=credentials::status(&context.data);
    let value=serde_json::json!({"readiness":readiness,"credentials_ready":status.as_ref().map(credentials::ready).unwrap_or(false),
        "credentials_error":status.err(),"fields":{"readiness":"桌面资源就绪检查","credentials_ready":"接口配置是否齐全","credentials_error":"接口配置读取错误"}});
    println!("{}",value);
}
