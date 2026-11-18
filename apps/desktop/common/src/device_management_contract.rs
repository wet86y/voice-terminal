//! DeviceManagementContract（设备管理契约）：语音核心之外的固定业务与资源管理消息。
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

pub const DEVICE_MANAGEMENT_PROTOCOL_VERSION: u32 = 1; // DEVICE_MANAGEMENT_PROTOCOL_VERSION（独立管理协议版本）。
pub const MAX_DEVICE_MANAGEMENT_ARGUMENT_BYTES: usize = 512 * 1024; // MAX_DEVICE_MANAGEMENT_ARGUMENT_BYTES（单个管理请求参数上限）。

/// DeviceBridgePrelude（受限 SSH 桥接前导帧）：由本机正式 forced-command（强制命令）桥接生成。
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct DeviceBridgePrelude {
    #[serde(rename = "type")]
    pub message_type: String, // type（固定为 bridge.prelude 的桥接消息类型）。
    pub protocol_version: u32,    // protocol_version（桥接前导协议版本）。
    pub workspace_id: String,     // workspace_id（已验证的正式工作区编号）。
    pub owner_sid: String,        // owner_sid（原 Windows 登录账户 SID）。
    pub client_device_id: String, // client_device_id（由 SSH 强制命令绑定的客户端编号）。
    pub build: String,            // build（已验证的正式程序构建编号）。
    pub fields: BTreeMap<String, String>, // fields（字段中文说明）。
}

impl DeviceBridgePrelude {
    /// validate（桥接前导校验）：服务端仍须另行确认当前工作区、账户、构建及设备信任。
    pub fn validate(&self) -> Result<(), String> {
        if self.message_type != "bridge.prelude"
            || self.protocol_version != 1
            || self.workspace_id.len() != 32
            || !self
                .workspace_id
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
            || !valid_sid(&self.owner_sid)
            || !valid_host_id(&self.client_device_id)
            || self.build.is_empty()
            || self.build.len() > 128
            || !self
                .build
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
        {
            return Err("SSH 桥接前导帧的类型、版本、工作区、账户、设备或构建身份无效。".into());
        }
        if self.fields.len() != 6
            || [
                "type",
                "protocol_version",
                "workspace_id",
                "owner_sid",
                "client_device_id",
                "build",
            ]
            .iter()
            .any(|key| !self.fields.contains_key(*key))
            || self
                .fields
                .values()
                .any(|value| value.trim().is_empty() || value.len() > 120)
        {
            return Err("SSH 桥接前导帧缺少逐字段中文说明。".into());
        }
        Ok(())
    }
}

/// RemoteManagementOpenRequest（远程管理会话申请）：通过 SSH 固定桥接认证后申请管理代次。
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RemoteManagementOpenRequest {
    pub protocol_version: u32,    // protocol_version（独立远程管理协议版本）。
    pub request_id: String,       // request_id（本次会话申请的幂等编号）。
    pub target_host_id: String,   // target_host_id（已配对服务端稳定编号）。
    pub client_device_id: String, // client_device_id（当前 SSH 认证客户端设备编号）。
    pub fields: BTreeMap<String, String>, // fields（字段中文说明）。
}

/// RemoteManagementOpenResponse（远程管理会话回执）：代次不依赖 Python 核心或媒体任务租约。
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RemoteManagementOpenResponse {
    pub protocol_version: u32,    // protocol_version（独立远程管理协议版本）。
    pub request_id: String,       // request_id（对应会话申请的幂等编号）。
    pub target_host_id: String,   // target_host_id（本次管理目标服务端编号）。
    pub client_device_id: String, // client_device_id（获准的客户端设备编号）。
    pub session_generation: u64,  // session_generation（管理服务授予的会话代次）。
    pub expires_at_ms: u64,       // expires_at_ms（管理会话到期时刻，Unix 毫秒）。
    pub fields: BTreeMap<String, String>, // fields（字段中文说明）。
}

/// RemoteManagementRequest（远程管理请求）：固定操作枚举名和类型化参数，不接收任意命令或网址。
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RemoteManagementRequest {
    pub protocol_version: u32,   // protocol_version（独立远程管理协议版本）。
    pub request_id: String,      // request_id（一次业务请求的稳定编号；重试沿用原编号）。
    pub target_host_id: String,  // target_host_id（已配对服务端稳定编号）。
    pub session_generation: u64, // session_generation（管理服务授予的会话代次）。
    pub operation: String,       // operation（服务端固定操作名称）。
    pub arguments: Value,        // arguments（与固定操作对应的 JSON 对象参数）。
    pub fields: BTreeMap<String, String>, // fields（字段中文说明）。
}

/// RemoteManagementResponse（远程管理结果）：保留服务端操作原状态和结果，另带代次绑定。
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RemoteManagementResponse {
    pub protocol_version: u32,   // protocol_version（独立远程管理协议版本）。
    pub request_id: String,      // request_id（服务端回传的原操作编号）。
    pub target_host_id: String,  // target_host_id（本次操作所属服务端编号）。
    pub session_generation: u64, // session_generation（本次已认证管理会话代次）。
    pub operation: String,       // operation（服务端确认的固定操作名称）。
    pub status: u16,             // status（原业务处理器 HTTP 状态码）。
    pub body: Value,             // body（既有业务处理器返回值或原始错误）。
    pub fields: BTreeMap<String, String>, // fields（字段中文说明）。
}

impl RemoteManagementRequest {
    /// validate（管理请求校验）：调用方仍须另行验证当前 SSH 信任身份及会话归属。
    pub fn validate(&self) -> Result<(), String> {
        if self.protocol_version != DEVICE_MANAGEMENT_PROTOCOL_VERSION
            || !valid_request_id(&self.request_id)
            || !valid_host_id(&self.target_host_id)
            || self.session_generation == 0
            || !valid_operation(&self.operation)
            || !self.arguments.is_object()
        {
            return Err("远程管理请求的版本、编号、代次、操作或参数形状无效。".into());
        }
        if serde_json::to_vec(&self.arguments).map_or(true, |bytes| {
            bytes.len() > MAX_DEVICE_MANAGEMENT_ARGUMENT_BYTES
        }) {
            return Err("远程管理请求参数超过 512 KiB 上限。".into());
        }
        if self.fields.len() != 6
            || [
                "protocol_version",
                "request_id",
                "target_host_id",
                "session_generation",
                "operation",
                "arguments",
            ]
            .iter()
            .any(|key| !self.fields.contains_key(*key))
            || self
                .fields
                .values()
                .any(|value| value.trim().is_empty() || value.len() > 120)
        {
            return Err("远程管理请求缺少逐字段中文说明。".into());
        }
        Ok(())
    }
}

pub fn valid_request_id(value: &str) -> bool {
    value.len() == 32
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

pub fn valid_host_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.trim() == value
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}

pub fn valid_operation(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 96
        && value.is_ascii()
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'.' | b'_' | b'-')
        })
}

fn valid_sid(value: &str) -> bool {
    let parts = value.split('-').collect::<Vec<_>>();
    parts.len() >= 4
        && parts[0] == "S"
        && parts
            .iter()
            .skip(1)
            .all(|part| !part.is_empty() && part.bytes().all(|byte| byte.is_ascii_digit()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn management_request_binds_fixed_ids_fields_and_bounded_object_arguments() {
        let request = RemoteManagementRequest {
            protocol_version: 1,
            request_id: "a".repeat(32),
            target_host_id: "server-a".into(),
            session_generation: 4,
            operation: "resource.jobs".into(),
            arguments: serde_json::json!({}),
            fields: BTreeMap::from([
                ("protocol_version".into(), "独立远程管理协议版本".into()),
                ("request_id".into(), "一次业务请求的稳定编号".into()),
                ("target_host_id".into(), "已配对服务端稳定编号".into()),
                ("session_generation".into(), "管理服务授予的会话代次".into()),
                ("operation".into(), "服务端固定操作名称".into()),
                ("arguments".into(), "与固定操作对应的 JSON 对象参数".into()),
            ]),
        };
        assert!(request.validate().is_ok());
        let mut stale = request.clone();
        stale.session_generation = 0;
        assert!(stale.validate().is_err());
        let mut arbitrary = request.clone();
        arbitrary.operation = "../../command".into();
        assert!(arbitrary.validate().is_err());
        let mut unlabeled = request;
        unlabeled.fields.remove("target_host_id");
        assert!(unlabeled.validate().is_err());
    }
}
