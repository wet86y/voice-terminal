//! Download sources（下载来源）：与脚本共用配置，保持内容锁与安装收据不变。
use serde_json::Value;
use std::sync::OnceLock;

pub fn policy() -> &'static Value {
    static POLICY: OnceLock<Value> = OnceLock::new();
    POLICY.get_or_init(|| serde_json::from_str(include_str!("../../../../config/download-sources.json")).expect("随包下载来源配置无效"))
}

fn query_path(path: &str) -> String {
    let mut bytes = vec![];
    let mut source = path.as_bytes().iter().copied();
    while let Some(byte) = source.next() {
        if byte == b'%' {
            let pair: Vec<_> = source.by_ref().take(2).collect();
            if let Ok(value) = u8::from_str_radix(&String::from_utf8_lossy(&pair), 16) { bytes.push(value); }
            else { bytes.push(byte); bytes.extend(pair); }
        } else { bytes.push(byte); }
    }
    bytes.iter().map(|byte| if byte.is_ascii_alphanumeric() || b"-._~".contains(byte) {(*byte as char).to_string()} else {format!("%{byte:02X}")}).collect()
}

pub fn candidates(url: &str) -> Vec<String> {
    // update.json（未独立签名的更新清单）只能通过原站建立信任。
    if url.split('?').next().unwrap_or(url).ends_with("/update.json") { return vec![url.into()]; }
    let mut result = vec![];
    for row in policy()["models"].as_array().unwrap() {
        if let Some(path) = url.strip_prefix(row["origin_prefix"].as_str().unwrap()) {
            result.push(format!("https://modelscope.cn/api/v1/models/{}/repo?Revision={}&FilePath={}",row["repo_id"].as_str().unwrap(),row["revision"].as_str().unwrap(),query_path(path)));
        }
    }
    for row in policy()["prefixes"].as_array().unwrap() {
        if let Some(path) = url.strip_prefix(row["origin_prefix"].as_str().unwrap()) {
            result.push(format!("{}{path}",row["mirror_prefix"].as_str().unwrap()));
        }
    }
    result.push(url.into()); result.dedup(); result
}

pub fn host(url: &str) -> &str { url.split("://").nth(1).unwrap_or(url).split('/').next().unwrap_or("").rsplit('@').next().unwrap_or("") }

/// plan_sources（安装计划来源）：只投影域名，不暴露临时签名或认证信息。
pub fn plan_sources(item:&Value)->Vec<String>{
    let mut urls=vec![];
    if item["source"]["transfer"]["kind"]=="npm_package" {
        urls.extend(policy()["npm_registries"].as_array().unwrap().iter().filter_map(Value::as_str).map(str::to_owned));
    } else if item["source"]["platform"]=="huggingface" {
        if let Some(row)=item["files"].as_array().and_then(|rows|rows.iter().find(|row|row["asset"].is_null())){
            urls.push(format!("https://huggingface.co/{}/resolve/{}/{}",item["source"]["repo_id"].as_str().unwrap_or(""),item["source"]["revision"].as_str().unwrap_or(""),row["source_path"].as_str().or(row["path"].as_str()).unwrap_or("")));
        }
    } else if item["source"]["platform"]=="modelscope" {urls.push("https://modelscope.cn".into());}
    else {
        if let Some(url)=item["source"]["transfer"]["artifact"]["url"].as_str(){urls.push(url.into());}
        if let Some(rows)=item["source"]["transfer"]["artifacts"].as_array(){urls.extend(rows.iter().filter_map(|row|row["url"].as_str()).map(str::to_owned));}
    }
    let mut hosts=vec![];
    for url in urls {for candidate in candidates(&url){let domain=host(&candidate).to_owned();if !hosts.contains(&domain){hosts.push(domain);}}}
    hosts
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn public_mirrors_preserve_origin_and_private_sources() {
        let origin="https://download.pytorch.org/whl/cu128/torch.whl";
        assert_eq!(candidates(origin),vec!["https://mirror.sjtu.edu.cn/pytorch-wheels/cu128/torch.whl",origin]);
        let private="https://private.example/pkg.whl"; assert_eq!(candidates(private),vec![private]);
        let manifest="https://github.com/wet86y/voice-terminal/releases/latest/download/update.json";
        assert_eq!(candidates(manifest),vec![manifest]);
    }
    #[test]
    fn model_mirror_keeps_original_revision_and_encodes_path() {
        let origin="https://huggingface.co/audio-cpp/audio.cpp-gguf/resolve/1b5981b2b89a1af578ebba5c8cf34226810e1f55/VoxCPM2-GGUF/a%20b.gguf";
        let rows=candidates(origin); assert_eq!(rows.len(),2); assert_eq!(rows[1],origin);
        assert!(rows[0].contains("Revision=710b2d85fa66ecd0e9cc4eee6413b9595171f502&FilePath=VoxCPM2-GGUF%2Fa%20b.gguf"));
        assert_eq!(candidates(&origin.replace("1b5981b2b89a1af578ebba5c8cf34226810e1f55","different")).len(),1);
    }
}
