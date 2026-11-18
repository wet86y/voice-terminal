//! Device task receipts（设备任务回执）：跨 SSH 连接保留已执行但未确认的原任务结果。

use crate::{
    ssh_device::{RemoteDeviceTask, RemoteDeviceTaskResult},
    workspace,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{fs, io::Read, path::PathBuf, sync::Mutex};

const RECEIPT_SCHEMA_VERSION: u32 = 1; // RECEIPT_SCHEMA_VERSION（设备任务回执日志格式版本）。
const MAX_PENDING_RECEIPTS: usize = 64; // MAX_PENDING_RECEIPTS（最多保留的未确认回执数）。
const MAX_RESULT_BYTES: usize = 256 * 1024; // MAX_RESULT_BYTES（单项任务结果上限，字节）。
const MAX_RECEIPT_FILE_BYTES: u64 = 17 * 1024 * 1024; // MAX_RECEIPT_FILE_BYTES（日志上限，含字段开销）。

/// PendingDeviceReceipt（待确认设备回执）：不保存任务参数，只留原任务身份与执行结果。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PendingDeviceReceipt {
    pub device_id: String,          // device_id（原执行设备编号）。
    pub task_id: String,            // task_id（原请求及持久任务编号）。
    pub core_session_id: String,    // core_session_id（原权威核心运行编号）。
    pub connection_generation: u64, // connection_generation（原连接代次）。
    pub executor_generation: u64,   // executor_generation（原执行器代次）。
    pub sequence: u64,              // sequence（原连接内任务序号）。
    pub succeeded: bool,            // succeeded（设备是否明确报告成功）。
    pub result: Value,              // result（原任务的有界 JSON 结果）。
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ReceiptFile {
    schema_version: u32, // schema_version（此日志自己的格式版本，不属于产品设置）。
    receipts: Vec<PendingDeviceReceipt>, // receipts（尚未收到核心结算确认的原任务回执）。
}

/// DeviceTaskReceiptJournal（设备任务回执日志）：原子保存并只删除已确认的回执。
pub struct DeviceTaskReceiptJournal {
    data_root: PathBuf, // data_root（工作区数据根目录；不直接接受任意状态文件路径）。
    io_lock: Mutex<()>, // io_lock（防止同一客户端并发覆盖回执日志）。
}

impl DeviceTaskReceiptJournal {
    /// new（创建日志句柄）：不创建目录、不读取或修改任何文件。
    pub fn new(data_root: PathBuf) -> Self {
        Self {
            data_root,
            io_lock: Mutex::new(()),
        }
    }

    /// ensure_room（检查是否可安全领取下一项任务）：满额时在新副作用前停止派发。
    pub fn ensure_room(&self) -> Result<(), String> {
        let _guard = self.io_lock.lock().map_err(|_| "设备回执日志锁不可用。")?;
        let state = self.load_unlocked()?;
        if state.receipts.len() >= MAX_PENDING_RECEIPTS {
            return Err("未确认设备回执已达到本地上限；先恢复原任务回执后再领取新任务。".into());
        }
        Ok(())
    }

    /// pending_for_device（读取待结算回执）：只返回指定设备的原任务，不创建重派请求。
    pub fn pending_for_device(&self, device_id: &str) -> Result<Vec<PendingDeviceReceipt>, String> {
        if !valid_identifier(device_id) {
            return Err("待查询设备编号无效。".into());
        }
        let _guard = self.io_lock.lock().map_err(|_| "设备回执日志锁不可用。")?;
        Ok(self
            .load_unlocked()?
            .receipts
            .into_iter()
            .filter(|receipt| receipt.device_id == device_id)
            .collect())
    }

    /// record（持久化设备结果）：先落盘再向核心提交，不能因断线丢失已执行结果。
    pub fn record(
        &self,
        task: &RemoteDeviceTask,
        outcome: &RemoteDeviceTaskResult,
    ) -> Result<PendingDeviceReceipt, String> {
        if task.request_id != task.task_id {
            return Err("设备任务请求编号与持久任务编号不一致；结果必须保留为未知。".into());
        }
        let receipt = PendingDeviceReceipt {
            device_id: task.device_id.clone(),
            task_id: task.task_id.clone(),
            core_session_id: task.core_session_id.clone(),
            connection_generation: task.connection_generation,
            executor_generation: task.executor_generation,
            sequence: task.sequence,
            succeeded: outcome.succeeded,
            result: outcome.result.clone(),
        };
        validate_receipt(&receipt)?;
        let encoded_result =
            serde_json::to_vec(&receipt.result).map_err(|_| "设备回执结果无法编码。")?;
        if encoded_result.len() > MAX_RESULT_BYTES {
            return Err("设备回执结果超过 256 KiB 上限，原任务仍须标记为未知。".into());
        }
        let _guard = self.io_lock.lock().map_err(|_| "设备回执日志锁不可用。")?;
        let mut state = self.load_unlocked()?;
        if let Some(previous) = state
            .receipts
            .iter()
            .find(|item| item.device_id == receipt.device_id && item.task_id == receipt.task_id)
        {
            return if previous == &receipt {
                Ok(receipt)
            } else {
                Err("原任务编号已有不同的未确认设备回执。".into())
            };
        }
        if state.receipts.len() >= MAX_PENDING_RECEIPTS {
            return Err("未确认设备回执已达到本地上限；保留原结果且不覆盖旧回执。".into());
        }
        state.receipts.push(receipt.clone());
        self.save_unlocked(&state)?;
        Ok(receipt)
    }

    /// remove（确认后清除）：只移除设备和任务编号均匹配的原回执。
    pub fn remove(&self, device_id: &str, task_id: &str) -> Result<bool, String> {
        if !valid_identifier(device_id) || !valid_identifier(task_id) {
            return Err("设备回执清除目标无效。".into());
        }
        let _guard = self.io_lock.lock().map_err(|_| "设备回执日志锁不可用。")?;
        let mut state = self.load_unlocked()?;
        let old_len = state.receipts.len();
        state
            .receipts
            .retain(|receipt| !(receipt.device_id == device_id && receipt.task_id == task_id));
        if state.receipts.len() == old_len {
            return Ok(false);
        }
        self.save_unlocked(&state)?;
        Ok(true)
    }

    fn path(&self) -> Result<PathBuf, String> {
        workspace::safe_path(&self.data_root, "state/device-task-receipts.json")
    }

    fn load_unlocked(&self) -> Result<ReceiptFile, String> {
        let path = self.path()?;
        let metadata = match fs::symlink_metadata(&path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return Ok(ReceiptFile {
                    schema_version: RECEIPT_SCHEMA_VERSION,
                    receipts: Vec::new(),
                })
            }
            Err(error) => return Err(format!("无法读取设备回执日志：{error}")),
        };
        if workspace::metadata_reparse(&metadata)
            || !metadata.is_file()
            || metadata.len() > MAX_RECEIPT_FILE_BYTES
        {
            return Err("设备回执日志类型、链接或大小无效；原件保留且任务领取已停止。".into());
        }
        let mut bytes = Vec::with_capacity(metadata.len() as usize);
        fs::File::open(&path)
            .map_err(|error| format!("无法打开设备回执日志：{error}"))?
            .take(MAX_RECEIPT_FILE_BYTES + 1)
            .read_to_end(&mut bytes)
            .map_err(|error| format!("无法读取设备回执日志：{error}"))?;
        if bytes.len() as u64 > MAX_RECEIPT_FILE_BYTES {
            return Err("设备回执日志读取时超过大小上限；原件保留。".into());
        }
        let state: ReceiptFile = serde_json::from_slice(&bytes).map_err(|_| {
            "设备回执日志格式损坏或版本不受支持；原件保留且任务领取已停止。".to_string()
        })?;
        if state.schema_version != RECEIPT_SCHEMA_VERSION
            || state.receipts.len() > MAX_PENDING_RECEIPTS
        {
            return Err("设备回执日志版本或条目数量无效；原件保留且任务领取已停止。".into());
        }
        for (index, receipt) in state.receipts.iter().enumerate() {
            validate_receipt(receipt)?;
            if state.receipts[..index].iter().any(|previous| {
                previous.device_id == receipt.device_id && previous.task_id == receipt.task_id
            }) {
                return Err("设备回执日志包含重复原任务编号；原件保留。".into());
            }
            if serde_json::to_vec(&receipt.result)
                .map_or(true, |value| value.len() > MAX_RESULT_BYTES)
            {
                return Err("设备回执日志包含超限结果；原件保留。".into());
            }
        }
        Ok(state)
    }

    fn save_unlocked(&self, state: &ReceiptFile) -> Result<(), String> {
        let path = self.path()?;
        let value = serde_json::to_value(state).map_err(|_| "设备回执日志编码失败。")?;
        if serde_json::to_vec(&value)
            .map_or(true, |bytes| bytes.len() as u64 > MAX_RECEIPT_FILE_BYTES)
        {
            return Err("设备回执日志超过文件大小上限；原回执保持未确认。".into());
        }
        workspace::atomic_json(&path, &value)
            .map_err(|error| format!("原子保存设备回执日志失败：{error}"))
    }
}

fn valid_identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.trim() == value
        && !value.chars().any(char::is_control)
}

fn validate_receipt(receipt: &PendingDeviceReceipt) -> Result<(), String> {
    if !valid_identifier(&receipt.device_id)
        || receipt.device_id == "local"
        || !valid_identifier(&receipt.task_id)
        || !valid_identifier(&receipt.core_session_id)
        || receipt.connection_generation == 0
        || receipt.executor_generation == 0
        || receipt.sequence == 0
    {
        return Err("设备回执身份或代次无效。".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ssh_device::RemoteDeviceTask;

    fn task(id: &str) -> RemoteDeviceTask {
        RemoteDeviceTask {
            request_id: id.into(),
            device_id: "device-a".into(),
            core_session_id: "core-old".into(),
            task_id: id.into(),
            connection_generation: 7,
            executor_generation: 3,
            sequence: 11,
            operation: "playback.stop_and_snapshot".into(),
            arguments: serde_json::json!({"playback_key":"speech-a"}),
        }
    }

    fn root() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../../.local/tmp")
            .join(format!("device-receipts-{}", workspace::new_id()))
    }

    #[test]
    fn receipt_is_durable_until_the_matching_core_acknowledgement() {
        let root = root();
        let journal = DeviceTaskReceiptJournal::new(root.clone());
        let outcome = RemoteDeviceTaskResult {
            succeeded: true,
            result: serde_json::json!({"played_ms":120}),
        };
        journal.record(&task("task-a"), &outcome).unwrap();
        let reopened = DeviceTaskReceiptJournal::new(root.clone());
        let receipts = reopened.pending_for_device("device-a").unwrap();
        assert_eq!(receipts.len(), 1);
        assert_eq!(receipts[0].task_id, "task-a");
        assert_eq!(receipts[0].core_session_id, "core-old");
        assert_eq!(receipts[0].result["played_ms"], 120);
        assert!(reopened.ensure_room().is_ok());
        assert!(reopened.remove("device-a", "task-a").unwrap());
        assert!(reopened.pending_for_device("device-a").unwrap().is_empty());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn duplicate_task_ids_cannot_overwrite_a_different_result() {
        let root = root();
        let journal = DeviceTaskReceiptJournal::new(root.clone());
        journal
            .record(
                &task("task-a"),
                &RemoteDeviceTaskResult {
                    succeeded: true,
                    result: serde_json::json!({"played_ms":120}),
                },
            )
            .unwrap();
        assert!(journal
            .record(
                &task("task-a"),
                &RemoteDeviceTaskResult {
                    succeeded: true,
                    result: serde_json::json!({"played_ms":121}),
                }
            )
            .is_err());
        assert_eq!(
            journal.pending_for_device("device-a").unwrap()[0].result["played_ms"],
            120
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn malformed_receipt_file_is_never_overwritten() {
        let root = root();
        fs::create_dir_all(root.join("state")).unwrap();
        let path = root.join("state/device-task-receipts.json");
        fs::write(&path, b"{broken").unwrap();
        let journal = DeviceTaskReceiptJournal::new(root.clone());
        assert!(journal.ensure_room().is_err());
        assert!(journal
            .record(
                &task("task-a"),
                &RemoteDeviceTaskResult {
                    succeeded: true,
                    result: serde_json::json!({"played_ms":120}),
                }
            )
            .is_err());
        assert_eq!(fs::read(path).unwrap(), b"{broken");
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn full_receipt_journal_stops_admission_without_evicting_unconfirmed_results() {
        let root = root();
        let journal = DeviceTaskReceiptJournal::new(root.clone());
        let outcome = RemoteDeviceTaskResult {
            succeeded: true,
            result: serde_json::json!({"ok":true}),
        };
        for index in 0..MAX_PENDING_RECEIPTS {
            let id = format!("task-{index}");
            journal.record(&task(&id), &outcome).unwrap();
        }
        assert!(journal.ensure_room().is_err());
        assert_eq!(
            journal.pending_for_device("device-a").unwrap().len(),
            MAX_PENDING_RECEIPTS
        );
        assert!(journal.remove("device-a", "task-0").unwrap());
        assert!(journal.ensure_room().is_ok());
        assert_eq!(
            journal.pending_for_device("device-a").unwrap().len(),
            MAX_PENDING_RECEIPTS - 1
        );
        let _ = fs::remove_dir_all(root);
    }
}
