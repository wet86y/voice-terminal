//! DeviceOperation（设备设置任务）：状态属于原生宿主，页面重建后可继续读取，重复请求在副作用前拒绝。
use std::sync::Mutex;

/// GatewayControl（网关交接回执）：迟到结果不能完成另一轮广播操作。
#[cfg(feature = "ssh-device")]
#[derive(Default)]
pub struct GatewayControl {
    sequence: std::sync::atomic::AtomicU64, // sequence（单个桌面进程内的递增请求号）。
    pending: Mutex<std::collections::HashMap<u64, tokio::sync::oneshot::Sender<serde_json::Value>>>, // pending（等待匹配回执的原生调用）。
}

#[cfg(feature = "ssh-device")]
impl GatewayControl {
    pub fn begin(
        &self,
    ) -> Result<(u64, tokio::sync::oneshot::Receiver<serde_json::Value>), String> {
        let id = self
            .sequence
            .fetch_add(1, std::sync::atomic::Ordering::AcqRel)
            + 1;
        let (send, receive) = tokio::sync::oneshot::channel();
        self.pending
            .lock()
            .map_err(|_| "网关交接状态不可用。")?
            .insert(id, send);
        Ok((id, receive))
    }

    pub fn settle(&self, id: u64, result: serde_json::Value) {
        if let Ok(mut pending) = self.pending.lock() {
            if let Some(send) = pending.remove(&id) {
                let _ = send.send(result);
            }
        }
    }

    pub fn forget(&self, id: u64) {
        if let Ok(mut pending) = self.pending.lock() {
            pending.remove(&id);
        }
    }
}

#[derive(Default)]
pub struct DeviceOperation {
    snapshot: Mutex<serde_json::Value>, // snapshot（不含密钥、配对码的任务状态）。
}

pub struct DeviceOperationGuard<'a> {
    owner: &'a DeviceOperation, // owner（拥有任务状态的桌面宿主）。
    finished: bool,             // finished（是否已明确记录终态）。
}

impl DeviceOperation {
    pub fn begin(&self, mode: &str, broadcast: bool) -> Result<DeviceOperationGuard<'_>, String> {
        let mut snapshot = self.snapshot.lock().map_err(|_| "设备任务状态不可用。")?;
        if snapshot["active"] == true {
            return Err("设备设置任务仍在进行，请等待当前任务完成；切换页面不会取消任务。".into());
        }
        let revision = snapshot["revision"].as_u64().unwrap_or(0) + 1;
        let state_revision = snapshot["state_revision"].as_u64().unwrap_or(0) + 1;
        *snapshot = serde_json::json!({"active":true,"revision":revision,"state_revision":state_revision,"phase":"checking",
            "message":"正在检查设备设置切换条件","target_mode":mode,"target_broadcast":broadcast,"error":null,
            "fields":{"active":"任务是否仍在原生宿主执行","revision":"任务序号","state_revision":"进度快照递增号，用于拒绝迟到状态","phase":"执行阶段",
                "message":"阶段中文说明","target_mode":"目标运行模式","target_broadcast":"目标广播开关","error":"终态错误摘要"}});
        Ok(DeviceOperationGuard {
            owner: self,
            finished: false,
        })
    }

    pub fn snapshot(&self) -> serde_json::Value {
        self.snapshot
            .lock()
            .map(|value| value.clone())
            .unwrap_or(serde_json::Value::Null)
    }
}

impl DeviceOperationGuard<'_> {
    pub fn phase(&self, phase: &str, message: &str) {
        if let Ok(mut snapshot) = self.owner.snapshot.lock() {
            snapshot["state_revision"] =
                (snapshot["state_revision"].as_u64().unwrap_or(0) + 1).into();
            snapshot["phase"] = phase.into();
            snapshot["message"] = message.into();
        }
    }

    pub fn finish(mut self, error: Option<String>) {
        if let Ok(mut snapshot) = self.owner.snapshot.lock() {
            snapshot["state_revision"] =
                (snapshot["state_revision"].as_u64().unwrap_or(0) + 1).into();
            snapshot["active"] = false.into();
            snapshot["phase"] = if error.is_some() {
                "failed"
            } else {
                "complete"
            }
            .into();
            snapshot["message"] = if error.is_some() {
                "设备设置任务失败，请查看错误说明"
            } else {
                "设备设置已保存，运行状态将继续更新"
            }
            .into();
            snapshot["error"] = error.into();
        }
        self.finished = true;
    }
}

impl Drop for DeviceOperationGuard<'_> {
    fn drop(&mut self) {
        if !self.finished {
            if let Ok(mut snapshot) = self.owner.snapshot.lock() {
                snapshot["state_revision"] =
                    (snapshot["state_revision"].as_u64().unwrap_or(0) + 1).into();
                snapshot["active"] = false.into();
                snapshot["phase"] = "failed".into();
                snapshot["error"] = "设备设置任务被中断，请刷新实际配置后重试。".into();
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(feature = "ssh-device")]
    #[tokio::test]
    async fn expired_gateway_reply_cannot_complete_a_new_operation() {
        let control = GatewayControl::default();
        let (old_id, old_reply) = control.begin().unwrap();
        control.forget(old_id);
        assert!(old_reply.await.is_err());
        let (new_id, mut new_reply) = control.begin().unwrap();
        control.settle(old_id, serde_json::json!({"ok":true}));
        assert!(matches!(
            new_reply.try_recv(),
            Err(tokio::sync::oneshot::error::TryRecvError::Empty)
        ));
        control.settle(new_id, serde_json::json!({"ok":false,"error":"端口占用"}));
        assert_eq!(new_reply.await.unwrap()["error"], "端口占用");
    }

    #[test]
    fn concurrent_requests_cannot_replace_active_progress_and_failure_is_retained() {
        let tasks = DeviceOperation::default();
        let first = tasks.begin("local", true).unwrap();
        first.phase("preparing_access", "正在准备局域网访问");
        assert_eq!(tasks.snapshot()["state_revision"], 2);
        assert!(tasks.begin("remote", false).is_err());
        assert_eq!(tasks.snapshot()["target_mode"], "local");
        assert_eq!(tasks.snapshot()["message"], "正在准备局域网访问");
        first.finish(Some("用户取消授权".into()));
        assert_eq!(tasks.snapshot()["active"], false);
        assert_eq!(tasks.snapshot()["error"], "用户取消授权");
        assert_eq!(tasks.snapshot()["state_revision"], 3);
        let second = tasks.begin("remote", false).unwrap();
        assert_eq!(tasks.snapshot()["revision"], 2);
        drop(second);
        assert_eq!(tasks.snapshot()["phase"], "failed");
        assert_eq!(tasks.snapshot()["active"], false);
    }
}
