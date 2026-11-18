//! DeviceTrust（设备信任）：原生层唯一写入 Python（后端）共享的公钥登记格式。
use russh::keys::{HashAlg, PublicKey};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    fs,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

const SCHEMA_VERSION: u32 = 1; // SCHEMA_VERSION（设备信任文件格式版本）。
const MAX_BYTES: u64 = 1024 * 1024; // MAX_BYTES（设备信任文件大小上限，字节）。

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TrustedDevice {
    pub device_id: String,          // device_id（稳定客户端设备编号）。
    pub public_key: String,         // public_key（受信设备 OpenSSH Ed25519 公钥）。
    pub fingerprint: String,        // fingerprint（设备公钥 SHA-256 指纹）。
    pub state: String,              // state（trusted（可信）或 revoked（已撤销））。
    pub trusted_at_ms: u64,         // trusted_at_ms（首次登记的 Unix 毫秒时间）。
    pub revoked_at_ms: Option<u64>, // revoked_at_ms（撤销的 Unix 毫秒时间；可信时为空）。
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct TrustDocument {
    schema_version: u32,         // schema_version（Python 共享信任文件格式版本）。
    devices: Vec<TrustedDevice>, // devices（设备公钥和登记状态）。
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TrustSnapshot {
    pub state: &'static str, // state（valid（有效）、missing（缺失）或 invalid（无效））。
    pub devices: Vec<TrustedDevice>, // devices（公开信任登记，不含私钥）。
    pub error: Option<String>, // error（无法识别时的原因；无效原件会保留）。
}

pub struct DeviceTrustStore {
    data_root: PathBuf,
    path: PathBuf,
    records: Vec<TrustedDevice>,
    digest: Option<[u8; 32]>,
    state: &'static str,
    error: Option<String>,
}

impl DeviceTrustStore {
    pub fn open(data_root: &Path) -> Result<Self, String> {
        let path = crate::workspace::safe_path(data_root, "state/device-trust.json")?;
        let mut store = Self {
            data_root: data_root.to_path_buf(),
            path,
            records: Vec::new(),
            digest: None,
            state: "missing",
            error: None,
        };
        store.reload()?;
        Ok(store)
    }

    pub fn snapshot(&self) -> TrustSnapshot {
        TrustSnapshot {
            state: self.state,
            devices: self.records.clone(),
            error: self.error.clone(),
        }
    }

    pub fn trusted_public_key(&self, device_id: &str) -> Option<&str> {
        self.records
            .iter()
            .find(|record| record.device_id == device_id && record.state == "trusted")
            .map(|record| record.public_key.as_str())
    }

    pub fn approve(&mut self, device_id: &str, public_key: &str) -> Result<TrustedDevice, String> {
        validate_device_id(device_id)?;
        let (canonical_key, fingerprint) = validate_public_key(public_key)?;
        if self.state == "invalid" {
            return Err("设备信任登记无效；原件已保留并拒绝写入。".into());
        }
        self.verify_unchanged()?;
        for record in &self.records {
            if record.public_key == canonical_key && record.device_id != device_id {
                return Err("同一设备公钥不能登记到多个设备编号。".into());
            }
            if record.device_id == device_id
                && record.public_key != canonical_key
                && record.state == "trusted"
            {
                return Err("设备公钥变化；先撤销旧身份，再通过配对流程登记。".into());
            }
        }
        let trusted_at_ms = now_ms()?;
        let record = TrustedDevice {
            device_id: device_id.to_owned(),
            public_key: canonical_key,
            fingerprint,
            state: "trusted".into(),
            trusted_at_ms,
            revoked_at_ms: None,
        };
        let mut records = self
            .records
            .iter()
            .filter(|item| item.device_id != device_id)
            .cloned()
            .collect::<Vec<_>>();
        records.push(record.clone());
        self.persist(records)?;
        Ok(record)
    }

    pub fn revoke(&mut self, device_id: &str) -> Result<bool, String> {
        validate_device_id(device_id)?;
        if self.state == "invalid" {
            return Err("设备信任登记无效；原件已保留并拒绝撤销写入。".into());
        }
        self.verify_unchanged()?;
        let timestamp = now_ms()?;
        let mut found = false;
        let records = self
            .records
            .iter()
            .cloned()
            .map(|mut record| {
                if record.device_id == device_id && record.state == "trusted" {
                    record.state = "revoked".into();
                    record.revoked_at_ms = Some(timestamp);
                    found = true;
                }
                record
            })
            .collect::<Vec<_>>();
        if found {
            self.persist(records)?;
        }
        Ok(found)
    }

    fn reload(&mut self) -> Result<(), String> {
        let metadata = match fs::symlink_metadata(&self.path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                self.records.clear();
                self.digest = None;
                self.state = "missing";
                self.error = None;
                return Ok(());
            }
            Err(error) => return Err(format!("读取设备信任登记失败：{error}")),
        };
        if crate::workspace::metadata_reparse(&metadata)
            || !metadata.is_file()
            || metadata.len() > MAX_BYTES
        {
            self.state = "invalid";
            self.error = Some("设备信任登记路径、类型或大小无效；原件已保留。".into());
            return Ok(());
        }
        let bytes =
            fs::read(&self.path).map_err(|error| format!("读取设备信任登记失败：{error}"))?;
        let result = serde_json::from_slice::<TrustDocument>(&bytes)
            .map_err(|_| "设备信任登记 JSON 或字段无效；原件已保留。".to_string())
            .and_then(validate_document);
        match result {
            Ok(document) => {
                self.records = document.devices;
                self.digest = Some(Sha256::digest(&bytes).into());
                self.state = "valid";
                self.error = None;
            }
            Err(error) => {
                self.records.clear();
                self.digest = Some(Sha256::digest(&bytes).into());
                self.state = "invalid";
                self.error = Some(error);
            }
        }
        Ok(())
    }

    fn verify_unchanged(&self) -> Result<(), String> {
        let metadata = match fs::symlink_metadata(&self.path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound && self.digest.is_none() => {
                return Ok(())
            }
            Err(_) => return Err("设备信任登记在外部发生变化，拒绝覆盖。".into()),
        };
        if crate::workspace::metadata_reparse(&metadata)
            || !metadata.is_file()
            || metadata.len() > MAX_BYTES
        {
            return Err("设备信任登记路径发生变化，拒绝覆盖。".into());
        }
        let bytes =
            fs::read(&self.path).map_err(|_| "无法复核设备信任登记原件，拒绝覆盖。".to_string())?;
        if self.digest != Some(Sha256::digest(&bytes).into()) {
            return Err("设备信任登记在外部发生变化，拒绝覆盖。".into());
        }
        Ok(())
    }

    fn persist(&mut self, mut records: Vec<TrustedDevice>) -> Result<(), String> {
        records.sort_by(|left, right| left.device_id.cmp(&right.device_id));
        let document = TrustDocument {
            schema_version: SCHEMA_VERSION,
            devices: records.clone(),
        };
        let value: Value =
            serde_json::to_value(document).map_err(|_| "设备信任登记编码失败。".to_string())?;
        let path = crate::workspace::safe_path(&self.data_root, "state/device-trust.json")?;
        crate::workspace::atomic_json(&path, &value)?;
        let bytes = fs::read(&path).map_err(|error| format!("回读设备信任登记失败：{error}"))?;
        if bytes.len() as u64 > MAX_BYTES {
            return Err("设备信任登记写入后超过大小限制。".into());
        }
        self.records = records;
        self.digest = Some(Sha256::digest(&bytes).into());
        self.state = "valid";
        self.error = None;
        Ok(())
    }
}

fn validate_document(document: TrustDocument) -> Result<TrustDocument, String> {
    if document.schema_version != SCHEMA_VERSION || document.devices.len() > 1024 {
        return Err("设备信任登记版本或清单无效；原件已保留。".into());
    }
    let mut ids = HashSet::new();
    let mut public_keys = HashSet::new();
    for record in &document.devices {
        validate_device_id(&record.device_id)?;
        let (canonical, fingerprint) = validate_public_key(&record.public_key)?;
        if record.fingerprint != fingerprint
            || record.public_key != canonical
            || !matches!(record.state.as_str(), "trusted" | "revoked")
            || !ids.insert(record.device_id.as_str())
            || !public_keys.insert(canonical)
            || record.state == "trusted" && record.revoked_at_ms.is_some()
            || record.state == "revoked"
                && record
                    .revoked_at_ms
                    .is_none_or(|time| time < record.trusted_at_ms)
        {
            return Err("设备信任登记项重复、非规范或状态矛盾；原件已保留。".into());
        }
    }
    Ok(document)
}

fn validate_device_id(value: &str) -> Result<(), String> {
    if !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
    {
        Ok(())
    } else {
        Err("设备编号格式无效。".into())
    }
}

fn validate_public_key(value: &str) -> Result<(String, String), String> {
    if value.len() > 8192 {
        return Err("设备公钥超过大小限制。".into());
    }
    let key = PublicKey::from_openssh(value)
        .map_err(|_| "设备公钥不是有效 OpenSSH 格式。".to_string())?;
    if key.algorithm() != russh::keys::Algorithm::Ed25519 {
        return Err("仅接受 OpenSSH Ed25519 设备公钥。".into());
    }
    let fingerprint = key.fingerprint(HashAlg::Sha256).to_string();
    let canonical = key
        .to_openssh()
        .map_err(|_| "设备公钥无法规范编码。".to_string())?;
    Ok((canonical, fingerprint))
}

fn now_ms() -> Result<u64, String> {
    let value = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "系统时间无效，无法登记设备信任。".to_string())?
        .as_millis();
    u64::try_from(value).map_err(|_| "系统时间超出设备信任格式范围。".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use russh::keys::{Algorithm, PrivateKey};

    fn root() -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../.local/tmp")
            .join(format!(
                "native-device-trust-{}",
                crate::workspace::new_id()
            ))
    }

    #[test]
    fn native_writer_preserves_python_document_and_rejects_external_edits() {
        let root = root();
        fs::create_dir_all(&root).unwrap();
        let mut store = DeviceTrustStore::open(&root).unwrap();
        let key = PrivateKey::random(&mut rand::rng(), Algorithm::Ed25519)
            .unwrap()
            .public_key()
            .to_openssh()
            .unwrap();
        let trusted = store.approve("device-a", &key).unwrap();
        assert_eq!(
            store.trusted_public_key("device-a"),
            Some(trusted.public_key.as_str())
        );
        let value: Value =
            serde_json::from_slice(&fs::read(root.join("state/device-trust.json")).unwrap())
                .unwrap();
        assert_eq!(value["schema_version"], 1);
        assert_eq!(value["devices"][0]["fingerprint"], trusted.fingerprint);

        fs::write(root.join("state/device-trust.json"), b"{}").unwrap();
        assert!(store
            .revoke("device-a")
            .unwrap_err()
            .contains("外部发生变化"));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn damaged_native_trust_file_is_preserved_and_keeps_authorization_closed() {
        let root = root();
        fs::create_dir_all(root.join("state")).unwrap();
        let path = root.join("state/device-trust.json");
        let original = br#"{"schema_version":99,"devices":[]}"#;
        fs::write(&path, original).unwrap();
        let mut store = DeviceTrustStore::open(&root).unwrap();
        assert_eq!(store.snapshot().state, "invalid");
        assert!(store.approve("device-a", "invalid").is_err());
        assert_eq!(fs::read(&path).unwrap(), original);
        fs::remove_dir_all(root).unwrap();
    }
}
