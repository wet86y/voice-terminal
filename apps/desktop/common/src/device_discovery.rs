//! DeviceDiscovery（设备发现）：只在局域网通过 mDNS 公布服务能力，不公布密钥。
use mdns_sd::{ServiceDaemon, ServiceEvent, ServiceInfo};
use serde::Serialize;
use std::{
    collections::{BTreeMap, BTreeSet},
    net::IpAddr,
    time::{Duration, Instant},
};

pub const SERVICE_TYPE: &str = "_voiceterminal._tcp.local."; // SERVICE_TYPE（Voice Terminal 局域网服务类型）。
pub const DISCOVERY_PROTOCOL_VERSION: u16 = 1; // DISCOVERY_PROTOCOL_VERSION（服务发现协议版本）。

pub const CLIENT_SERVICE_TYPE: &str = "_vtclient._udp.local."; // CLIENT_SERVICE_TYPE（已配对客户端的存在公告，不提供入站业务入口）。

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DiscoveredClient {
    pub device_id: String, // device_id（客户端稳定编号）。
    pub display_name: String, // display_name（客户端显示名称）。
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct InboundClient {
    pub device_id: String, // device_id（本机可信客户端编号）。
    pub display_name: Option<String>, // display_name（当前局域网存在公告名称）。
    pub online: bool, // online（可发现客户端，可由客户端恢复连接；不表示服务端可主动拨入）。
}

/// inbound_clients（可信条目投影）：未登记或已撤销的广播不能增加客户端条目。
pub fn inbound_clients(trust: &crate::device_trust::TrustSnapshot, discovered: &[DiscoveredClient]) -> Result<Vec<InboundClient>, String> {
    if trust.state == "invalid" { return Err(trust.error.clone().unwrap_or_else(|| "设备信任登记无效。".into())); }
    Ok(trust.devices.iter().filter(|device| device.state == "trusted").map(|device| {
        let found = discovered.iter().find(|client| client.device_id == device.device_id);
        InboundClient { device_id: device.device_id.clone(), display_name: found.map(|client| client.display_name.clone()), online: found.is_some() }
    }).collect())
}

/// DiscoveredDevice（发现的服务端）：公开可路由地址仅供原生层连接，不含密钥或配对码。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DiscoveredDevice {
    pub host_id: String,                  // host_id（稳定服务端设备编号）。
    pub display_name: String,             // display_name（服务端显示名称）。
    pub hostname: String,                 // hostname（mDNS 解析出的服务端主机名）。
    pub addresses: Vec<String>,           // addresses（服务发现返回的局域网地址记录）。
    pub pairing_port: u16,                // pairing_port（OPAQUE 配对入口端口）。
    pub ssh_port: u16,                    // ssh_port（微软 OpenSSH 服务端口）。
    pub protocol_version: u16,            // protocol_version（设备发现协议版本）。
    pub compatible: bool,                 // compatible（发现协议是否可兼容）。
    pub state: String,                    // state（服务端准备状态）。
    pub capabilities: Vec<String>,        // capabilities（服务端声明的固定能力编号）。
    pub fields: BTreeMap<String, String>, // fields（字段的中文说明）。
}

/// DiscoveryPublication（发现服务发布租约）：drop（释放）时撤销本次 mDNS 公告。
pub struct DiscoveryPublication {
    daemon: ServiceDaemon,
    fullname: String,
    state: String, // state（公告实际发布的核心准备状态）。
    display_name: String, // display_name（本轮公告使用的服务端显示名称）。
    checked_at: Instant, // checked_at（客户端网络地址最近核对时间，不检查 SSH 系统服务）。
}

impl DiscoveryPublication {
    pub fn state_name(&self) -> &str {
        &self.state
    }

    /// display_name（公告显示名称）：改名后不能复用旧名称的发布租约。
    pub fn display_name(&self) -> &str {
        &self.display_name
    }

    pub fn stop(self) -> Result<(), String> {
        self.daemon
            .unregister(&self.fullname)
            .map_err(|error| format!("撤销局域网服务公告失败：{error}"))?;
        self.daemon
            .shutdown()
            .map_err(|error| format!("停止局域网发现服务失败：{error}"))?;
        Ok(())
    }
}

impl Drop for DiscoveryPublication {
    fn drop(&mut self) {
        let _ = self.daemon.unregister(&self.fullname);
        let _ = self.daemon.shutdown();
    }
}

/// publish（发布服务）：地址由上游物理局域网接口筛选器提供，不自行枚举或开放其他网络。
pub fn publish(
    host_id: &str,
    display_name: &str,
    hostname: &str,
    local_lan_addresses: &[IpAddr],
    pairing_port: u16,
    ssh_port: u16,
    state: &str,
    capabilities: &[&str],
) -> Result<DiscoveryPublication, String> {
    validate_host_id(host_id)?;
    if !valid_display(display_name)
        || !valid_host_name(hostname)
        || pairing_port == 0
        || ssh_port == 0
        || !matches!(state, "ready" | "starting" | "unavailable")
    {
        return Err("mDNS 公告配置无效；未发布服务。".into());
    }
    let addresses = local_lan_addresses
        .iter()
        .filter(|address| is_private_lan_address(**address))
        .map(ToString::to_string)
        .collect::<BTreeSet<_>>();
    if addresses.is_empty() {
        return Err("未找到符合当前物理局域网范围的服务地址；未发布服务。".into());
    }
    let capability_list = capabilities
        .iter()
        .copied()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();
    if capability_list.iter().any(|value| !valid_capability(value)) {
        return Err("mDNS 公告包含无效能力编号；未发布服务。".into());
    }
    let pairing_port_text = pairing_port.to_string();
    let ssh_port_text = ssh_port.to_string();
    let protocol_version_text = DISCOVERY_PROTOCOL_VERSION.to_string();
    let capability_text = capability_list.join(",");
    let properties = [
        ("host_id", host_id),
        ("display_name", display_name),
        ("pairing_port", pairing_port_text.as_str()),
        ("ssh_port", ssh_port_text.as_str()),
        ("protocol_version", protocol_version_text.as_str()),
        ("state", state),
        ("capabilities", capability_text.as_str()),
    ];
    let instance = format!(
        "VoiceTerminal-{}",
        host_id
            .chars()
            .filter(|ch| ch.is_ascii_alphanumeric())
            .take(24)
            .collect::<String>()
    );
    let addresses = addresses.into_iter().collect::<Vec<_>>();
    let service = ServiceInfo::new(
        SERVICE_TYPE,
        &instance,
        hostname,
        addresses.as_slice(),
        pairing_port,
        properties.as_slice(),
    )
    .map_err(|error| format!("建立 mDNS 局域网公告失败：{error}"))?;
    let fullname = service.get_fullname().to_owned();
    let daemon =
        ServiceDaemon::new().map_err(|error| format!("启动 mDNS 局域网公告失败：{error}"))?;
    daemon
        .register(service)
        .map_err(|error| format!("注册 mDNS 局域网公告失败：{error}"))?;
    Ok(DiscoveryPublication {
        daemon,
        fullname,
        state: state.to_owned(),
        display_name: display_name.to_owned(),
        checked_at: Instant::now(),
    })
}

/// refresh_client（客户端存在公告）：只公开已配对身份；端口为零，不增加监听器或连接权限。
pub fn refresh_client(
    mut previous: Option<DiscoveryPublication>, device_id: &str, host_id: &str, display_name: &str,
) -> Result<DiscoveryPublication, String> {
    validate_host_id(device_id)?;
    validate_host_id(host_id)?;
    if !valid_display(display_name) { return Err("客户端显示名称无效。".into()); }
    let prefix = format!("client:{device_id}@{host_id}:");
    if previous.as_ref().is_some_and(|value| value.state.starts_with(&prefix)
        && value.display_name == display_name && value.checked_at.elapsed() < Duration::from_secs(60)) {
        return Ok(previous.take().unwrap());
    }
    let addresses = private_physical_addresses()?;
    let key = format!("{prefix}{}", addresses.iter().map(ToString::to_string).collect::<Vec<_>>().join(","));
    if let Some(value) = previous.as_mut() {
        if value.state == key && value.display_name == display_name {
            value.checked_at = Instant::now();
            return Ok(previous.take().unwrap());
        }
    }
    drop(previous);
    if addresses.is_empty() { return Err("当前没有可发布客户端状态的私有物理局域网。".into()); }
    use sha2::{Digest, Sha256};
    let identity = format!("{:x}", Sha256::digest(device_id.as_bytes()));
    let hostname = format!("vtc-{}.local.", &identity[..48]);
    let properties = [("device_id", device_id), ("host_id", host_id), ("display_name", display_name), ("protocol_version", "1")];
    let service = ServiceInfo::new(CLIENT_SERVICE_TYPE, &format!("VTClient-{}", &identity[..32]),
        &hostname, addresses.as_slice(), 0, properties.as_slice())
        .map_err(|error| format!("建立客户端存在公告失败：{error}"))?;
    let fullname = service.get_fullname().to_owned();
    let daemon = ServiceDaemon::new().map_err(|error| format!("启动客户端存在公告失败：{error}"))?;
    // 限制 mDNS（多播域名系统）收发到已核对的物理私有网络接口。
    daemon.disable_interface(mdns_sd::IfKind::All).map_err(|error| error.to_string())?;
    for address in &addresses { daemon.enable_interface(mdns_sd::IfKind::Addr(*address)).map_err(|error| error.to_string())?; }
    daemon.register(service).map_err(|error| format!("发布客户端存在公告失败：{error}"))?;
    Ok(DiscoveryPublication { daemon, fullname, state: key, display_name: display_name.to_owned(), checked_at: Instant::now() })
}

/// private_physical_addresses（轻量私有物理网络查询）：不读取 SSH、签名、系统可选功能或防火墙规则。
fn private_physical_addresses() -> Result<Vec<IpAddr>, String> {
    let script = r#"
$ErrorActionPreference = 'Stop'
$physical = @(Get-NetAdapter -Physical | Where-Object { $_.Status -eq 'Up' } | Select-Object -ExpandProperty ifIndex)
$private = @(Get-NetConnectionProfile | Where-Object { [string]$_.NetworkCategory -in @('Private','DomainAuthenticated') } | Select-Object -ExpandProperty InterfaceIndex)
$addresses = @(Get-NetIPAddress -AddressFamily IPv4,IPv6 -Type Unicast | Where-Object { $physical -contains $_.InterfaceIndex -and $private -contains $_.InterfaceIndex } | Select-Object -ExpandProperty IPAddress)
ConvertTo-Json -InputObject $addresses -Compress
"#;
    let host = crate::powershell::select(crate::powershell::Purpose::System)?;
    let (code, output, _) = crate::powershell::execute(&host, script, Duration::from_secs(15), &mut |_| {})?;
    if code != 0 { return Err("无法读取私有物理局域网地址。".into()); }
    let values: Vec<String> = serde_json::from_str(output.trim()).map_err(|_| "私有物理网络地址回执无效。")?;
    Ok(values.iter().filter_map(|value| value.parse::<IpAddr>().ok()).filter(|value| is_private_lan_address(*value))
        .collect::<BTreeSet<_>>().into_iter().collect())
}

fn parse_client(service: &mdns_sd::ResolvedService, host_id: &str) -> Option<DiscoveredClient> {
    if service.ty_domain != CLIENT_SERVICE_TYPE || !service.is_valid() || service.port != 0
        || !service.addresses.iter().any(|address| is_private_lan_address(address.to_ip_addr())) { return None; }
    let properties = &service.txt_properties;
    if properties.get_property_val_str("host_id")? != host_id || properties.get_property_val_str("protocol_version")? != "1" { return None; }
    let device_id = properties.get_property_val_str("device_id")?;
    let display_name = properties.get_property_val_str("display_name")?;
    validate_host_id(device_id).ok()?;
    if !valid_display(display_name) { return None; }
    Some(DiscoveredClient { device_id: device_id.to_owned(), display_name: display_name.to_owned() })
}

/// search_clients（发现本机已配对客户端）：公告仅用于可达状态，不授予设备认证或业务权限。
pub fn search_clients(host_id: &str, timeout: Duration) -> Result<Vec<DiscoveredClient>, String> {
    validate_host_id(host_id)?;
    if timeout.is_zero() || timeout > Duration::from_secs(10) { return Err("客户端发现等待时长无效。".into()); }
    let daemon = ServiceDaemon::new().map_err(|error| error.to_string())?;
    let receiver = daemon.browse(CLIENT_SERVICE_TYPE).map_err(|error| error.to_string())?;
    let deadline = Instant::now() + timeout;
    let mut found = BTreeMap::new();
    while Instant::now() < deadline {
        if let Ok(event) = receiver.recv_timeout(deadline.saturating_duration_since(Instant::now()).min(Duration::from_millis(250))) {
            match event {
                ServiceEvent::ServiceResolved(service) => { if let Some(client) = parse_client(&service, host_id) { found.insert(service.fullname.clone(), client); } }
                ServiceEvent::ServiceRemoved(_, fullname) => { found.remove(&fullname); }
                _ => {}
            }
        }
    }
    let _ = daemon.stop_browse(CLIENT_SERVICE_TYPE);
    let _ = daemon.shutdown();
    Ok(found.into_values().collect())
}

/// search（搜索服务端）：限定最长十秒，不按主机名/IP 排除同机的独立工作区。
pub fn search(timeout: Duration) -> Result<Vec<DiscoveredDevice>, String> {
    if timeout.is_zero() || timeout > Duration::from_secs(10) {
        return Err("设备搜索时间必须为 1 至 10 秒。".into());
    }
    let daemon =
        ServiceDaemon::new().map_err(|error| format!("启动 mDNS 设备搜索失败：{error}"))?;
    let receiver = daemon
        .browse(SERVICE_TYPE)
        .map_err(|error| format!("开始 mDNS 设备搜索失败：{error}"))?;
    let deadline = Instant::now() + timeout;
    let mut found = BTreeMap::<String, DiscoveredDevice>::new();
    while Instant::now() < deadline {
        let remaining = deadline.saturating_duration_since(Instant::now());
        match receiver.recv_timeout(remaining.min(Duration::from_millis(250))) {
            Ok(ServiceEvent::ServiceResolved(service)) => {
                if let Some(device) = parse_resolved_service(&service) {
                    found.insert(device.host_id.clone(), device);
                }
            }
            Ok(ServiceEvent::ServiceRemoved(_, fullname)) => {
                found.retain(|_, device| device.hostname != fullname);
            }
            Ok(_) => {}
            Err(_) => {}
        }
    }
    let _ = daemon.stop_browse(SERVICE_TYPE);
    let _ = daemon.shutdown();
    Ok(found.into_values().collect())
}

fn parse_resolved_service(service: &mdns_sd::ResolvedService) -> Option<DiscoveredDevice> {
    if service.ty_domain != SERVICE_TYPE || !service.is_valid() {
        return None;
    }
    let properties = &service.txt_properties;
    let host_id = properties.get_property_val_str("host_id")?.to_owned();
    let display_name = properties.get_property_val_str("display_name")?.to_owned();
    let pairing_port = parse_port(properties.get_property_val_str("pairing_port")?)?;
    let ssh_port = parse_port(properties.get_property_val_str("ssh_port")?)?;
    let protocol_version = properties
        .get_property_val_str("protocol_version")?
        .parse::<u16>()
        .ok()?;
    let state = properties.get_property_val_str("state")?.to_owned();
    let capabilities = properties
        .get_property_val_str("capabilities")?
        .split(',')
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
        .collect::<Vec<_>>();
    let addresses = service
        .addresses
        .iter()
        .map(|address| address.to_ip_addr())
        .filter(|address| is_private_lan_address(*address))
        .take(16)
        .map(|address| address.to_string())
        .collect::<Vec<_>>();
    validate_host_id(&host_id).ok()?;
    if addresses.is_empty()
        || !valid_display(&display_name)
        || !matches!(state.as_str(), "ready" | "starting" | "unavailable")
        || capabilities.iter().any(|value| !valid_capability(value))
    {
        return None;
    }
    Some(DiscoveredDevice {
        host_id,
        display_name,
        hostname: service.host.clone(),
        addresses,
        pairing_port,
        ssh_port,
        protocol_version,
        compatible: protocol_version == DISCOVERY_PROTOCOL_VERSION,
        state,
        capabilities,
        fields: [
            ("host_id".into(), "稳定服务端设备编号".into()),
            ("display_name".into(), "服务端显示名称".into()),
            ("hostname".into(), "mDNS 解析得到的服务端名称".into()),
            (
                "addresses".into(),
                "局域网服务地址记录；由原生层连接，不提供手工编辑".into(),
            ),
            ("pairing_port".into(), "OPAQUE 六位码配对入口端口".into()),
            ("ssh_port".into(), "微软 OpenSSH 服务端口".into()),
            ("protocol_version".into(), "局域网设备发现协议版本".into()),
            ("compatible".into(), "双方发现协议是否兼容".into()),
            ("state".into(), "服务端实际准备状态".into()),
            ("capabilities".into(), "服务端声明的固定能力编号".into()),
        ]
        .into_iter()
        .collect(),
    })
}

fn parse_port(value: &str) -> Option<u16> {
    value.parse::<u16>().ok().filter(|port| *port > 0)
}

fn validate_host_id(value: &str) -> Result<(), String> {
    if !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
    {
        Ok(())
    } else {
        Err("服务端设备编号无效。".into())
    }
}

fn valid_display(value: &str) -> bool {
    !value.trim().is_empty() && value.len() <= 128 && !value.chars().any(char::is_control)
}

fn valid_host_name(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 253
        && value.ends_with(".local.")
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-'))
}

fn valid_capability(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

pub fn is_private_lan_address(address: IpAddr) -> bool {
    match address {
        IpAddr::V4(ip) => {
            ip.is_private() && !ip.is_loopback() && !ip.is_link_local() && !ip.is_multicast()
        }
        IpAddr::V6(ip) => {
            let segments = ip.segments();
            (segments[0] & 0xfe00) == 0xfc00 && !ip.is_loopback() && !ip.is_multicast()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn client_service(host_id: &str, device_id: &str) -> mdns_sd::ResolvedService {
        let properties = [("host_id", host_id), ("device_id", device_id), ("display_name", "中文客户端"), ("protocol_version", "1")];
        ServiceInfo::new(CLIENT_SERVICE_TYPE, "Client", "client.local.", "192.168.1.20", 0, properties.as_slice())
            .unwrap().as_resolved_service()
    }

    #[test]
    fn client_presence_requires_exact_paired_host_private_address_and_protocol() {
        let service = client_service("host-a", "client-a");
        assert_eq!(parse_client(&service, "host-a").unwrap().display_name, "中文客户端");
        assert!(parse_client(&service, "host-b").is_none());
        let mut invalid = service.clone();
        invalid.port = 22;
        assert!(parse_client(&invalid, "host-a").is_none());
        invalid = service.clone();
        invalid.addresses = [mdns_sd::ScopedIp::from("8.8.8.8".parse::<IpAddr>().unwrap())].into_iter().collect();
        assert!(parse_client(&invalid, "host-a").is_none());
        assert!(parse_client(&client_service("host-a", "非法编号"), "host-a").is_none());
        assert!(parse_resolved_service(&service).is_none(), "客户端公告不能进入服务端配对搜索");
    }

    #[test]
    fn inbound_projection_keeps_trusted_offline_devices_and_excludes_untrusted_broadcasts() {
        let record = |id: &str, state: &str| crate::device_trust::TrustedDevice {
            device_id: id.into(), public_key: "private-test-value".into(), fingerprint: "not-for-ui".into(),
            state: state.into(), trusted_at_ms: 1, revoked_at_ms: None,
        };
        let mut trust = crate::device_trust::TrustSnapshot { state: "valid", error: None,
            devices: vec![record("online", "trusted"), record("offline", "trusted"), record("revoked", "revoked")] };
        let discovered = ["online", "revoked", "unknown"].map(|id| DiscoveredClient { device_id: id.into(), display_name: "同名设备".into() });
        let rows = inbound_clients(&trust, &discovered).unwrap();
        assert_eq!(rows.len(), 2);
        assert!(rows[0].online);
        assert!(!rows[1].online);
        let encoded = serde_json::to_string(&rows).unwrap();
        assert!(!encoded.contains("private-test-value") && !encoded.contains("not-for-ui"));
        assert!(!encoded.contains("revoked") && !encoded.contains("unknown"));
        assert!(inbound_clients(&trust, &[]).unwrap().iter().all(|device| !device.online));
        trust.state = "invalid";
        assert!(inbound_clients(&trust, &discovered).is_err());
    }

    #[test]
    #[ignore = "需要真实物理私有局域网，按需执行存在公告验收"]
    fn client_presence_live_publication_discovery_and_withdrawal() {
        let device_id = format!("presence-client-{}", std::process::id());
        let host_id = format!("presence-host-{}", std::process::id());
        let publication = refresh_client(None, &device_id, &host_id, "客户端存在验收").unwrap();
        let clients = search_clients(&host_id, Duration::from_secs(5)).unwrap();
        assert_eq!(clients.iter().find(|client| client.device_id == device_id).unwrap().display_name, "客户端存在验收");
        assert!(search_clients("different-host", Duration::from_secs(1)).unwrap().is_empty());
        publication.stop().unwrap();
        assert!(!search_clients(&host_id, Duration::from_secs(3)).unwrap().iter().any(|client| client.device_id == device_id));
    }

    #[test]
    fn accepts_only_private_physical_network_ranges() {
        assert!(is_private_lan_address("192.168.1.7".parse().unwrap()));
        assert!(is_private_lan_address("10.24.0.4".parse().unwrap()));
        assert!(is_private_lan_address("fd00::1".parse().unwrap()));
        assert!(!is_private_lan_address("8.8.8.8".parse().unwrap()));
        assert!(!is_private_lan_address("169.254.1.2".parse().unwrap()));
        assert!(!is_private_lan_address("fe80::1".parse().unwrap()));
    }

    #[test]
    fn validates_fixed_host_names_ids_capabilities_and_ports() {
        assert!(validate_host_id("device-甲").is_err());
        assert!(valid_host_name("voice-terminal.local."));
        assert!(!valid_host_name("voice-terminal.example.com"));
        assert!(valid_capability("voice.control"));
        assert!(!valid_capability("../command"));
        assert_eq!(parse_port("22"), Some(22));
        assert_eq!(parse_port("0"), None);
    }
}
