//! Component recovery（共用组件恢复）：事件分类、局部重建与全面校验导流。
use serde_json::{Value,json};

#[derive(Clone,Copy,Debug,PartialEq,Eq)]
pub enum Event {
    Transfer, // Transfer（网络传输或下载失败）。
    FileBusy, // FileBusy（文件占用）。
    Space, // Space（空间不足）。
    Permission, // Permission（目录权限）。
    Content, // Content（已安装内容或候选加载异常，可尝试局部重建）。
    Consistency, // Consistency（登记、路径或事务证据失效，不能安全继续）。
    Unknown, // Unknown（未分类操作错误，不据此推断组件损坏）。
}
pub fn classify(message:&str)->Event{
    let lower=message.to_lowercase();let contains=|words:&[&str]|words.iter().any(|word|lower.contains(word));
    if contains(&["空间不足","磁盘空间","no space left","disk full","os error 112","winerror 112"]){return Event::Space;}
    if contains(&["文件仍被占用","文件被占用","文件正在使用","另一个程序","sharing violation","another process","being used","os error 32","os error 33","winerror 32","winerror 33","系统错误码：32"]){return Event::FileBusy;}
    if contains(&["目录权限","拒绝访问","permission denied","access denied","access is denied","os error 5","winerror 5","系统错误码：5"]){return Event::Permission;}
    if contains(&["保留事务恢复记录","恢复旧资源失败","恢复原组件失败","资源回滚留待","恢复旧依赖失败","有效备份均缺失","路径越界","不能包含外部链接","缺少原件身份","发行元数据","缺少安装文件记录","缺少完整安装记录","内容身份","未锁定","清单无效","清单缺失","清单路径无效","需要完整性校验","局部修复规划失败","显卡核心推理库","解释器工作进程无法启动"]){return Event::Consistency;}
    if contains(&["增量依赖实际加载失败","现用运行环境完整性检查失败","现用显卡运行环境完整性校验失败","完整摘要复核失败"]){return Event::Content;}
    if contains(&["下载","http ","http:","https:","http/","httperror","httpstatus","网络","连接","timeout","timed out","tls","ssl","代理服务器","connection","socket"]){return Event::Transfer;}
    if contains(&["实际加载失败","加载检查失败","完整性检查失败","完整性校验失败","完整摘要","内容校验失败","文件校验失败","摘要不符","摘要错误","大小不符","固定版本","安装记录","动态库","归档不完整","补丁损坏"]){return Event::Content;}
    Event::Unknown
}
pub fn automatic(event:Event,attempt:u32)->bool{
    attempt<3&&matches!(event,Event::Transfer|Event::FileBusy|Event::Content|Event::Unknown)
}
pub fn local_rebuild(event:Event)->bool{matches!(event,Event::Content|Event::Unknown)}
pub fn guidance(event:Event,repairing:bool)->Value{
    let (kind,state,action,message)=if repairing{match event{
        Event::Transfer=>("transfer","retrying","none","正在重试传输，继续复用已校验文件和下载进度。"),
        Event::FileBusy=>("file_busy","retrying","none","等待文件占用释放后重试，原组件保持保护。"),
        _=>("content","repairing","none","正在核验本机材料并局部重建受影响组件，保留其它组件。"),
    }}else{match event{
        Event::Transfer=>("transfer","retry_available","retry","下载暂未完成。请检查网络后重试，已校验缓存与可续传进度会保留。"),
        Event::FileBusy=>("file_busy","action_required","retry","文件仍被占用。请关闭占用该组件的程序后重试。"),
        Event::Space=>("space","action_required","retry","请释放安装盘空间后重试，现有组件和下载缓存会保留。"),
        Event::Permission=>("permission","action_required","retry","请恢复安装目录的读写权限后重试。"),
        Event::Content|Event::Consistency=>("integrity","integrity_required","check_integrity","局部恢复未完成或无法安全继续。请执行全面完整性校验，由校验程序确定修复对象及范围。"),
        Event::Unknown=>("unknown","retry_available","retry","操作未完成。可重试或查看具体错误；当前错误不能证明组件内容损坏。"),
    }};
    json!({"event":kind,"state":state,"next_action":action,"message":message,"scope":"component",
        "fields":{"event":"故障事件类别","state":"恢复状态","next_action":"下一步操作；完整性校验始终检查全部组件","message":"用户可见处理说明","scope":"自动恢复只作用于当前组件"}})
}
pub fn interrupted()->Value{
    json!({"event":"interrupted","state":"retry_available","next_action":"retry","message":"上次任务意外中断。可继续原任务，复用已校验缓存和可续传进度。","fields":{"event":"任务意外中断","state":"等待继续","next_action":"重试原任务","message":"恢复说明"}})
}

#[cfg(test)]mod tests{
    use super::*;
    #[test]fn routine_errors_never_escalate_to_integrity_even_after_retries(){
        for(message,event)in[("下载失败：HTTP 503",Event::Transfer),("文件仍被占用：os error 32",Event::FileBusy),("磁盘空间不足",Event::Space),("拒绝访问：os error 5",Event::Permission)]{
            assert_eq!(classify(message),event);assert_eq!(guidance(event,false)["next_action"],"retry");assert!(!automatic(event,3));
        }
        assert_eq!(classify("局部修复规划失败：[WinError 32] The file is being used by another process"),Event::FileBusy);
        assert_eq!(classify("局部修复规划失败：[WinError 5] Access is denied"),Event::Permission);
    }
    #[test]fn only_content_and_consistency_failures_request_a_full_scan(){
        assert!(automatic(classify("增量依赖实际加载失败"),1));
        assert_eq!(guidance(classify("增量依赖实际加载失败"),false)["next_action"],"check_integrity");
        assert!(!automatic(classify("旧依赖备份损坏，保留事务恢复记录"),1));
        assert_eq!(guidance(Event::Unknown,false)["next_action"],"retry");
    }
}
