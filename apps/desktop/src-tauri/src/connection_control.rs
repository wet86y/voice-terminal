//! ConnectionControl（入站连接控制）：通知只经已认证管理通道传递，局域网发现不参与授权。
use std::{collections::HashMap, sync::Mutex, time::{Duration, Instant}};
use voice_desktop_common::device_management_contract::{ConnectionAction, ConnectionNotice};

#[derive(Default)]
pub struct ConnectionControl {
    state: Mutex<ControlState>, // state（活动连接和待确认通知）。
}

#[derive(Default)]
struct ControlState {
    active: HashMap<String, usize>, // active（各设备实际打开的已认证管理连接数）。
    pending: HashMap<String, PendingControl>, // pending（等待客户端持久保存暂停状态的通知）。
}

struct PendingControl {
    notice: ConnectionNotice, // notice（固定设备和服务端的控制通知）。
    acknowledged: bool, // acknowledged（客户端已持久保存暂停意图并确认通知）。
    created: Instant, // created（有界通知的创建时刻）。
    session_generation: Option<u64>, // session_generation（首次发送通知的管理连接代次）。
}

pub struct ActiveConnection<'a> {
    owner: &'a ConnectionControl, // owner（拥有此连接计数的管理服务）。
    device_id: String, // device_id（此连接已认证的客户端编号）。
}

impl ConnectionControl {
    pub fn enter(&self, device_id: &str) -> Result<ActiveConnection<'_>, String> {
        let mut state = self.state.lock().map_err(|_| "入站连接状态不可用。")?;
        *state.active.entry(device_id.to_owned()).or_default() += 1;
        Ok(ActiveConnection { owner: self, device_id: device_id.into() })
    }

    pub fn connected(&self, device_id: &str) -> bool {
        self.state.lock().map(|state| state.active.get(device_id).is_some_and(|count| *count > 0)).unwrap_or(true)
    }

    pub fn begin(&self, host_id: &str, device_id: &str, action: ConnectionAction) -> Result<ConnectionNotice, String> {
        let mut state = self.state.lock().map_err(|_| "入站连接状态不可用。")?;
        state.pending.retain(|_, pending| !pending.acknowledged && pending.created.elapsed() < Duration::from_secs(24 * 3600));
        if state.pending.contains_key(device_id) { return Err("该客户端已有连接控制任务，请等待其确认。".into()); }
        if state.pending.len() >= 1024 { return Err("入站连接通知数量已达上限。".into()); }
        let notice = ConnectionNotice { control_id: rand::random::<[u8; 16]>().iter().map(|byte| format!("{byte:02x}")).collect(),
            host_id: host_id.into(), client_device_id: device_id.into(), action };
        state.pending.insert(device_id.into(), PendingControl { notice: notice.clone(), acknowledged: false, created: Instant::now(), session_generation: None });
        Ok(notice)
    }

    pub fn pending(&self, device_id: &str, session_generation: u64) -> Option<ConnectionNotice> {
        let mut state = self.state.lock().ok()?;
        if state.pending.get(device_id).is_some_and(|pending| pending.created.elapsed() >= Duration::from_secs(24 * 3600)) {
            state.pending.remove(device_id); return None;
        }
        let pending = state.pending.get_mut(device_id).filter(|pending| !pending.acknowledged)?;
        pending.session_generation.get_or_insert(session_generation);
        Some(pending.notice.clone())
    }

    pub fn acknowledge(&self, device_id: &str, notice: &ConnectionNotice, session_generation: u64) -> Result<(), String> {
        let mut state = self.state.lock().map_err(|_| "入站连接状态不可用。")?;
        if state.pending.get(device_id).is_some_and(|pending| pending.created.elapsed() >= Duration::from_secs(24 * 3600)) {
            state.pending.remove(device_id); return Err("连接通知已过期。".into());
        }
        let pending = state.pending.get_mut(device_id).ok_or("连接通知已失效。")?;
        if &pending.notice != notice || pending.session_generation != Some(session_generation) { return Err("连接确认与原通知或管理代次不匹配。".into()); }
        pending.acknowledged = true;
        Ok(())
    }

    pub fn acknowledged(&self, notice: &ConnectionNotice) -> bool {
        self.state.lock().ok().and_then(|state| state.pending.get(&notice.client_device_id)
            .map(|pending| pending.notice == *notice && pending.acknowledged)).unwrap_or(false)
    }

    pub fn finish(&self, notice: &ConnectionNotice) {
        if let Ok(mut state) = self.state.lock() {
            if state.pending.get(&notice.client_device_id).is_some_and(|pending| pending.notice == *notice) {
                state.pending.remove(&notice.client_device_id);
            }
        }
    }
}

impl Drop for ActiveConnection<'_> {
    fn drop(&mut self) {
        if let Ok(mut state) = self.owner.state.lock() {
            if let Some(count) = state.active.get_mut(&self.device_id) {
                *count -= 1;
                if *count == 0 {
                    state.active.remove(&self.device_id);
                    if let Some(pending) = state.pending.get_mut(&self.device_id) { pending.session_generation = None; }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn connection_lifetime_and_notice_ack_are_bound_to_exact_device_and_action() {
        let controls = ConnectionControl::default();
        assert!(!controls.connected("client-a"));
        let connection = controls.enter("client-a").unwrap();
        assert!(controls.connected("client-a"));
        let notice = controls.begin("host-a", "client-a", ConnectionAction::Disconnect).unwrap();
        notice.validate("host-a", "client-a").unwrap();
        assert!(controls.begin("host-a", "client-a", ConnectionAction::Remove).is_err());
        assert!(controls.acknowledge("client-a", &notice, 7).is_err());
        assert_eq!(controls.pending("client-a", 7), Some(notice.clone()));
        assert!(controls.acknowledge("client-b", &notice, 7).is_err());
        let mut changed = notice.clone(); changed.action = ConnectionAction::Remove;
        assert!(controls.acknowledge("client-a", &changed, 7).is_err());
        assert!(controls.acknowledge("client-a", &notice, 8).is_err());
        assert!(!controls.acknowledged(&notice));
        controls.acknowledge("client-a", &notice, 7).unwrap();
        assert!(controls.acknowledged(&notice));
        drop(connection);
        assert!(!controls.connected("client-a"));
        controls.finish(&notice);
        assert!(controls.pending("client-a", 7).is_none());
    }

    #[test]
    fn expired_connection_notices_cannot_pause_a_later_session_or_be_acknowledged() {
        let controls = ConnectionControl::default();
        let notice = controls.begin("host-a", "client-a", ConnectionAction::Disconnect).unwrap();
        controls.pending("client-a", 7);
        controls.state.lock().unwrap().pending.get_mut("client-a").unwrap().created = Instant::now() - Duration::from_secs(24 * 3600 + 1);
        assert!(controls.acknowledge("client-a", &notice, 7).is_err());
        assert!(controls.pending("client-a", 8).is_none());
        controls.begin("host-a", "client-a", ConnectionAction::Disconnect).unwrap();
        controls.state.lock().unwrap().pending.get_mut("client-a").unwrap().created = Instant::now() - Duration::from_secs(24 * 3600 + 1);
        assert!(controls.pending("client-a", 8).is_none());
    }
}
