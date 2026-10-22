//! Runtime recovery（运行期恢复）：页面生命周期独立于可替换的业务进程。
use serde_json::{json, Value};

#[derive(Default)]
pub struct RuntimeRecovery {
    pub started: bool, // started（当前窗口已完成首次启动尝试）。
    pub continuity: Option<Value>, // continuity（仅私有管道交接的网页会话，禁止输出到状态接口）。
    pub result: Option<(u64, Value)>, // result（服务代次对应的真实启动终态）。
    pub require_core: bool, // require_core（更新前核心能力可用，恢复不能退化为不可用）。
    pub current_core: Option<bool>, // current_core（当前宿主通过私有管道上报的核心能力）。
    suspended: bool, // suspended（网页宿主暂时离线，前端暂停连接）。
    revision: u64, // revision（窗口内单调递增状态版本）。
    sequence: u64, // sequence（运行操作编号）。
    operation: Option<Value>, // operation（现有热切换卡使用的公共契约）。
}

impl RuntimeRecovery {
    pub fn preserves_page(&self) -> bool { self.started && self.continuity.is_some() }
    pub fn allows_navigation(&self) -> bool { !self.suspended } // allows_navigation（离线期间禁止页面重新导航到旧端口）。

    pub fn snapshot(&self) -> Value {
        json!({"revision":self.revision,"suspended":self.suspended,"operation":self.operation,
            "fields":{"revision":"窗口内状态修订号","suspended":"网页宿主离线，暂停业务连接","operation":"热切换运行操作"}})
    }

    pub fn suspend(&mut self) {
        if !self.preserves_page() { return; }
        if self.operation.as_ref().is_none_or(|row|row["state"]!="running") {
            self.require_core=self.current_core.unwrap_or_else(||self.result.as_ref().is_some_and(|(_,value)|value["core_available"]==true));
            self.sequence += 1;
            self.operation=Some(json!({"id":format!("desktop-resource-{}",self.sequence),"target":"resources",
                "label":"本地组件","state":"running"}));
        }
        self.suspended=true;
        self.update("running","switching","正在交接组件并暂停业务服务");
    }

    pub fn ready(&mut self) {
        if self.operation.as_ref().is_some_and(|row|row["state"]=="running") {
            self.suspended=false;
            self.update("running","loading","正在恢复组件、加载模型与建立会话");
        }
    }

    pub fn restored(&mut self) {
        if self.operation.as_ref().is_some_and(|row|row["state"]=="running") {
            self.update("running","verifying","业务服务已恢复，正在完成组件事务");
        }
    }

    pub fn finish(&mut self, error: Option<&str>) {
        if self.operation.is_none() { return; }
        if error.is_none() { self.suspended=false; }
        self.update(if error.is_some(){"failed"}else{"succeeded"},if error.is_some(){"failed"}else{"ready"},
            error.unwrap_or("组件与业务服务已恢复"));
    }

    fn update(&mut self,state:&str,phase:&str,detail:&str) {
        self.revision+=1;
        if let Some(row)=self.operation.as_mut(){
            row["revision"]=json!(self.revision);row["state"]=json!(state);row["phase"]=json!(phase);row["detail"]=json!(detail);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn recovery_keeps_one_operation_until_real_completion_and_excludes_credentials() {
        let mut value=RuntimeRecovery::default();
        value.suspend();assert_eq!(value.snapshot()["operation"],Value::Null);
        value.started=true;value.continuity=Some(json!({"session":"private-secret"}));
        value.suspend();let id=value.snapshot()["operation"]["id"].clone();
        assert_eq!(value.snapshot()["suspended"],true);
        value.ready();assert_eq!(value.snapshot()["operation"]["state"],"running");
        assert_eq!(value.snapshot()["suspended"],false);
        value.suspend();assert_eq!(value.snapshot()["operation"]["id"],id);
        value.finish(Some("恢复失败"));assert_eq!(value.snapshot()["suspended"],true);
        assert!(!value.snapshot().to_string().contains("private-secret"));
        value.suspend();assert_ne!(value.snapshot()["operation"]["id"],id);
        value.ready();value.finish(None);assert_eq!(value.snapshot()["operation"]["state"],"succeeded");
    }

    #[test]
    fn recovery_remembers_core_requirement_across_new_startup_and_rollback() {
        let mut value=RuntimeRecovery::default();value.started=true;value.continuity=Some(json!({}));
        value.result=Some((1,json!({"core_available":true})));value.suspend();assert!(value.require_core);
        value.result=None;value.ready();value.suspend();assert!(value.require_core);
        value.finish(Some("恢复失败"));assert_eq!(value.snapshot()["suspended"],true);
        value.result=Some((2,json!({"core_available":false})));value.suspend();assert!(!value.require_core);
        value.finish(None);value.current_core=Some(true);value.suspend();assert!(value.require_core);
    }
}
