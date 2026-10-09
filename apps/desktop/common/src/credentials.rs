//! Credentials（接口密钥）：复用 Python（业务后端）的 Windows（系统）凭据库与引用格式。
use std::{collections::BTreeMap, fs, path::Path};
use serde::Deserialize;
use serde_json::{json, Value};

const PROVIDERS: [(&str, &str); 2] = [("deepseek", "DEEPSEEK_API_KEY"), ("minimax", "MINIMAX_API_KEY")]; // PROVIDERS（固定服务及环境密钥名称）。

#[derive(Deserialize, Default)]
#[serde(deny_unknown_fields)]
pub struct CredentialUpdate {
    #[serde(default)] pub deepseek_api_key: String, // deepseek_api_key（语言模型接口密钥）：空白保留原值。
    #[serde(default)] pub minimax_api_key: String, // minimax_api_key（语音合成接口密钥）：空白保留原值。
}

trait Vault { // Vault（系统凭据接口）：测试可替换，正式实现兼容 Python keyring（凭据库）。
    fn get(&self, reference: &str) -> Result<Option<String>, String>;
    fn put(&self, reference: &str, secret: &str) -> Result<(), String>;
    fn delete(&self, reference: &str) -> Result<(), String>;
}

fn registry(path: &Path) -> Result<Value, String> {
    let bytes = match fs::read(path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(json!({"schema_version":2,"fixed_credentials":{}})),
        Err(_) => return Err("无法读取模型接口配置。".into()),
    };
    let mut value: Value = serde_json::from_slice(bytes.strip_prefix(&[0xef,0xbb,0xbf]).unwrap_or(&bytes))
        .map_err(|_| "模型接口配置格式错误，请检查配置文件。")?;
    if !value.is_object() || !matches!(value["schema_version"].as_u64(), Some(1 | 2)) {
        return Err("模型接口配置版本不受支持。".into());
    }
    if value.get("fixed_credentials").is_none() { value["fixed_credentials"] = json!({}); }
    if !value["fixed_credentials"].is_object() { return Err("固定服务凭据引用格式错误。".into()); }
    Ok(value)
}

fn environment_keys(data: &Path, explicit: BTreeMap<String, String>) -> Result<BTreeMap<String, String>, String> {
    let mut values = explicit;
    let content = match fs::read_to_string(data.join(".env.local")) {
        Ok(content) => content,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(values),
        Err(_) => return Err("无法读取当前工作区的环境配置。".into()),
    };
    // 与后端 load_env_file（环境配置读取）保持相同的引号、空值与优先级规则。
    for raw in content.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') { continue; }
        let line = line.strip_prefix("export ").map(str::trim_start).unwrap_or(line);
        let Some((name, value)) = line.split_once('=') else { continue; };
        let name = name.trim();
        if !PROVIDERS.iter().any(|(_, environment)| name == *environment) { continue; }
        let value = value.trim();
        let value = if value.len() >= 2 && ((value.starts_with('"') && value.ends_with('"')) || (value.starts_with('\'') && value.ends_with('\''))) { &value[1..value.len()-1] } else { value };
        values.entry(name.to_string()).or_insert_with(|| value.to_string());
    }
    Ok(values)
}

fn legacy_reference<'a>(value: &'a Value, provider: &str) -> Option<&'a str> {
    let slot = if provider == "deepseek" { "voice_llm_model_id" } else { "tts_profile_id" };
    let mut selected = value["assignments"][slot].as_str()?;
    if provider == "minimax" {
        selected = value["tts_profiles"].as_array()?.iter().find(|row| row["id"].as_str() == Some(selected))?["model_id"].as_str()?;
    }
    let model = value["models"].as_array()?.iter().find(|row| row["id"].as_str() == Some(selected))?;
    let source = value["providers"].as_array()?.iter().find(|row| row["id"] == model["provider_id"] && row["protocol"].as_str() == Some(provider))?;
    source["credential_ref"].as_str()
}

fn snapshot(value: &Value, env: &BTreeMap<String,String>, vault: &impl Vault) -> Result<Value,String> {
    let mut providers = json!({});
    for (provider, environment) in PROVIDERS {
        let configured = if let Some(reference) = value["fixed_credentials"][provider].as_str().filter(|s| !s.is_empty()) {
            vault.get(reference)?.is_some_and(|secret| !secret.is_empty())
        } else if env.get(environment).is_some_and(|secret| !secret.trim().is_empty()) {
            true
        } else if let Some(reference) = legacy_reference(value, provider) {
            vault.get(reference)?.is_some_and(|secret| !secret.is_empty())
        } else { false };
        providers[provider] = json!({"configured":configured});
    }
    Ok(json!({"providers":providers,"vault_available":cfg!(windows),
        "fields":{"providers":"固定服务配置状态","configured":"是否已有可读取的接口密钥","vault_available":"本机系统凭据存储是否可用"}}))
}

pub fn ready(status: &Value) -> bool {
    PROVIDERS.iter().all(|(provider, _)| status["providers"][provider]["configured"] == true)
}

fn native_environment(data: &Path) -> Result<BTreeMap<String,String>,String> {
    environment_keys(data, PROVIDERS.iter().filter_map(|(_, name)| std::env::var(name).ok().map(|value| (name.to_string(),value))).collect())
}

pub fn status(data: &Path) -> Result<Value,String> {
    snapshot(&registry(&data.join("config/model-registry.json"))?, &native_environment(data)?, &SystemVault)
}

fn contains_reference(value: &Value, reference: &str) -> bool {
    match value {
        Value::String(text) => text == reference,
        Value::Array(rows) => rows.iter().any(|row| contains_reference(row, reference)),
        Value::Object(rows) => rows.values().any(|row| contains_reference(row, reference)),
        _ => false,
    }
}

fn save_with(path: &Path, request: CredentialUpdate, vault: &impl Vault) -> Result<(),String> {
    let mut value = registry(path)?;
    let previous = value["fixed_credentials"].clone();
    let updates = [("deepseek", request.deepseek_api_key.trim()), ("minimax", request.minimax_api_key.trim())];
    if updates.iter().all(|(_, secret)| secret.is_empty()) { return Err("请至少填写一个接口密钥。".into()); }
    if updates.iter().any(|(_, secret)| secret.chars().count() > 4096 || secret.contains('\0')) { return Err("API Key（接口密钥）过长或包含无效字符。".into()); }
    let mut staged = BTreeMap::new();
    let result = (|| {
        for (provider, secret) in updates.into_iter().filter(|(_, secret)| !secret.is_empty()) {
            let reference = format!("credential-{}", crate::workspace::new_id());
            staged.insert(provider, reference.clone());
            vault.put(&reference, secret)?;
            value["fixed_credentials"][provider] = json!(reference);
        }
        crate::workspace::atomic_json(path, &value).map_err(|_| "接口密钥引用保存失败，原配置已保留。".to_string())
    })();
    if result.is_err() {
        for reference in staged.values() { let _ = vault.delete(reference); }
    } else {
        for provider in staged.keys() {
            if let Some(reference) = previous[provider].as_str() {
                if !contains_reference(&value, reference) { let _ = vault.delete(reference); }
            }
        }
    }
    result
}

pub fn save(data: &Path, request: CredentialUpdate) -> Result<Value,String> {
    save_with(&data.join("config/model-registry.json"), request, &SystemVault)?;
    let mut result = status(data)?;
    result["message"] = json!("接口密钥已保存，启动程序时生效。留空的服务保留已有密钥。");
    result["fields"]["message"] = json!("保存结果说明");
    Ok(result)
}

struct SystemVault; // SystemVault（系统凭据存储）：仅密钥引用写入配置文件。

#[cfg(windows)]
mod windows_vault {
    use super::*;
    use windows_sys::Win32::Security::Credentials::*;
    const SERVICE: &str = "Voice Terminal"; // SERVICE（凭据服务名称）：与 Python（业务后端）保持一致。
    fn wide(text: &str) -> Vec<u16> { text.encode_utf16().chain(Some(0)).collect() }
    struct Credential(*mut CREDENTIALW); // Credential（系统读取缓冲区）：自动释放系统分配的内存。
    impl Drop for Credential { fn drop(&mut self) { unsafe { CredFree(self.0.cast()); } } }
    fn read(target: &str, reference: &str) -> Result<Option<String>, String> {
        let mut pointer = std::ptr::null_mut();
        if unsafe { CredReadW(wide(target).as_ptr(), CRED_TYPE_GENERIC, 0, &mut pointer) } == 0 {
            if std::io::Error::last_os_error().raw_os_error() == Some(1168) { return Ok(None); }
            return Err("无法读取 Windows（系统）凭据，请检查当前用户的凭据存储。".into());
        }
        let owner = Credential(pointer);
        let row = unsafe { &*owner.0 };
        if row.UserName.is_null() { return Ok(None); }
        let mut size = 0;
        unsafe { while *row.UserName.add(size) != 0 { size += 1; } }
        if String::from_utf16_lossy(unsafe { std::slice::from_raw_parts(row.UserName, size) }) != reference { return Ok(None); }
        if row.CredentialBlobSize == 0 { return Ok(Some(String::new())); }
        if row.CredentialBlob.is_null() { return Err("系统凭据内容无效，请重新填写接口密钥。".into()); }
        let bytes = unsafe { std::slice::from_raw_parts(row.CredentialBlob, row.CredentialBlobSize as usize) };
        let decoded = if bytes.len() % 2 == 0 {
            String::from_utf16(&bytes.chunks_exact(2).map(|pair| u16::from_le_bytes([pair[0],pair[1]])).collect::<Vec<_>>()).ok()
        } else { None };
        decoded.or_else(|| String::from_utf8(bytes.to_vec()).ok()).map(Some).ok_or_else(|| "系统凭据编码无效，请重新填写接口密钥。".into())
    }
    impl Vault for SystemVault {
        fn get(&self, reference: &str) -> Result<Option<String>,String> {
            // Python keyring（凭据库）先读服务名，再读 reference@service（引用与服务组合名称）。
            match read(SERVICE, reference)? { Some(secret) => Ok(Some(secret)), None => read(&format!("{reference}@{SERVICE}"),reference) }
        }
        fn put(&self, reference: &str, secret: &str) -> Result<(),String> {
            let mut target = wide(&format!("{reference}@{SERVICE}"));
            let mut username = wide(reference);
            let mut blob: Vec<u8> = secret.encode_utf16().flat_map(u16::to_le_bytes).collect();
            let row = CREDENTIALW { Type:CRED_TYPE_GENERIC, TargetName:target.as_mut_ptr(), UserName:username.as_mut_ptr(),
                CredentialBlobSize:blob.len() as u32, CredentialBlob:blob.as_mut_ptr(), Persist:CRED_PERSIST_ENTERPRISE, ..Default::default() };
            let result = unsafe { CredWriteW(&row,0) };
            blob.fill(0);
            if result == 0 { return Err("Windows（系统）凭据保存失败，请检查接口密钥长度及当前用户的凭据存储。".into()); }
            Ok(())
        }
        fn delete(&self, reference: &str) -> Result<(),String> {
            for target in [SERVICE.to_string(), format!("{reference}@{SERVICE}")] {
                if read(&target, reference)?.is_some() && unsafe { CredDeleteW(wide(&target).as_ptr(),CRED_TYPE_GENERIC,0) } == 0 {
                    return Err("旧系统凭据清理失败。".into());
                }
            }
            Ok(())
        }
    }
}

#[cfg(not(windows))]
impl Vault for SystemVault {
    fn get(&self, _: &str) -> Result<Option<String>,String> { Ok(None) }
    fn put(&self, _: &str, _: &str) -> Result<(),String> { Err("当前系统不支持 Windows（系统）凭据存储。".into()) }
    fn delete(&self, _: &str) -> Result<(),String> { Ok(()) }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    #[derive(Default)] struct MemoryVault(RefCell<BTreeMap<String,String>>); // MemoryVault（内存凭据库）：验证事务，不触碰用户密钥。
    impl Vault for MemoryVault {
        fn get(&self, id:&str)->Result<Option<String>,String>{Ok(self.0.borrow().get(id).cloned())}
        fn put(&self,id:&str,secret:&str)->Result<(),String>{self.0.borrow_mut().insert(id.into(),secret.into());Ok(())}
        fn delete(&self,id:&str)->Result<(),String>{self.0.borrow_mut().remove(id);Ok(())}
    }
    #[test] fn saves_references_preserves_other_settings_and_blank_key() {
        let root=std::env::temp_dir().join(format!("voice-credentials-{}",crate::workspace::new_id()));
        let path=root.join("config/model-registry.json"); let vault=MemoryVault::default();
        crate::workspace::atomic_json(&path,&json!({"schema_version":2,"fixed_credentials":{},"user_setting":{"keep":true}})).unwrap();
        save_with(&path,CredentialUpdate{deepseek_api_key:" test-language-secret ".into(),minimax_api_key:"test-speech-secret".into()},&vault).unwrap();
        let first=registry(&path).unwrap(); let old=first["fixed_credentials"]["deepseek"].as_str().unwrap();
        assert_eq!(first["user_setting"]["keep"],true);assert!(!fs::read_to_string(&path).unwrap().contains("test-language-secret"));
        assert!(ready(&snapshot(&first,&BTreeMap::new(),&vault).unwrap()));
        save_with(&path,CredentialUpdate{deepseek_api_key:"updated-secret".into(),minimax_api_key:"  ".into()},&vault).unwrap();
        let second=registry(&path).unwrap();assert_eq!(first["fixed_credentials"]["minimax"],second["fixed_credentials"]["minimax"]);
        assert!(vault.get(old).unwrap().is_none()); assert!(!snapshot(&second,&BTreeMap::new(),&vault).unwrap().to_string().contains("updated-secret"));
        fs::remove_dir_all(root).unwrap();
    }
    #[test] fn rolls_back_new_vault_entries_if_reference_write_fails() {
        let root=std::env::temp_dir().join(format!("voice-credentials-{}",crate::workspace::new_id()));
        let path=root.join("model-registry.json"); fs::create_dir_all(&root).unwrap();
        fs::create_dir(path.with_extension(format!("{}.tmp",std::process::id()))).unwrap();
        let vault=MemoryVault::default();assert!(save_with(&path,CredentialUpdate{deepseek_api_key:"test-secret".into(),..Default::default()},&vault).is_err());
        assert!(vault.0.borrow().is_empty());assert!(!path.exists());fs::remove_dir_all(root).unwrap();
    }
    #[test] fn environment_keys_match_backend_priority_and_missing_reference_blocks_start() {
        let root=std::env::temp_dir().join(format!("voice-credentials-{}",crate::workspace::new_id()));fs::create_dir_all(&root).unwrap();
        fs::write(root.join(".env.local"),"export DEEPSEEK_API_KEY='file-language'\nMINIMAX_API_KEY=\"file-speech\"\n").unwrap();
        let env=environment_keys(&root,BTreeMap::from([("DEEPSEEK_API_KEY".into(),"process-language".into())])).unwrap();assert_eq!(env["DEEPSEEK_API_KEY"],"process-language");
        let vault=MemoryVault::default();assert!(ready(&snapshot(&json!({}),&env,&vault).unwrap()));
        assert!(!ready(&snapshot(&json!({"fixed_credentials":{"deepseek":"missing-reference"}}),&env,&vault).unwrap()));
        let empty=environment_keys(&root,BTreeMap::from([("DEEPSEEK_API_KEY".into(),"".into())])).unwrap();assert!(!ready(&snapshot(&json!({}),&empty,&vault).unwrap()));
        fs::remove_dir_all(root).unwrap();
    }
    #[test] fn resolves_existing_managed_provider_credentials() {
        let value=json!({"assignments":{"voice_llm_model_id":"language","tts_profile_id":"speech-profile"},"tts_profiles":[{"id":"speech-profile","model_id":"speech"}],
            "models":[{"id":"language","provider_id":"language-provider"},{"id":"speech","provider_id":"speech-provider"}],
            "providers":[{"id":"language-provider","protocol":"deepseek","credential_ref":"language-reference"},{"id":"speech-provider","protocol":"minimax","credential_ref":"speech-reference"}]});
        let vault=MemoryVault::default();vault.put("language-reference","test-language").unwrap();vault.put("speech-reference","test-speech").unwrap();
        assert!(ready(&snapshot(&value,&BTreeMap::new(),&vault).unwrap()));
    }
    #[test]
    #[cfg(windows)]
    #[ignore="手动验收：使用指定项目解释器验证原生与 Python（业务后端）凭据互通，随后删除临时凭据"]
    fn native_credentials_interoperate_with_python_keyring() {
        let python=std::env::var_os("VOICE_CREDENTIAL_TEST_PYTHON").expect("请提供 VOICE_CREDENTIAL_TEST_PYTHON（测试解释器路径）");
        let reference=format!("credential-test-{}",crate::workspace::new_id());
        SystemVault.put(&reference,"临时互通测试-secret").unwrap();
        let result=std::process::Command::new(python).args(["-B","-X","utf8","-c",
            "import sys; from keyring.backends.Windows import WinVaultKeyring; v=WinVaultKeyring(); r=sys.argv[1]; assert v.get_password('Voice Terminal',r)=='临时互通测试-secret'; v._set_password(r+'@Voice Terminal',r,'业务后端更新-secret')",&reference]).output();
        let read=SystemVault.get(&reference);let cleanup=SystemVault.delete(&reference);
        let output=result.unwrap();assert!(output.status.success(),"凭据互通验证失败，退出状态：{}",output.status);
        assert_eq!(read.unwrap().as_deref(),Some("业务后端更新-secret"));cleanup.unwrap();
        assert!(SystemVault.get(&reference).unwrap().is_none());
    }
}
