//! lan_update（本体版本同步）：正式设备入口、受限文件处理与现有安装器交接。
use super::*;
use base64::Engine;
use std::{cmp::Ordering as VersionOrder, io::Read};
use voice_desktop_common::{
    app_update::{self, RetainedInstaller},
    lan_update::{self as protocol, Handler, IncomingInstaller, Operation, Request, Response},
};

#[cfg(feature = "ssh-device")]
fn paired_target(state: &Desktop) -> Result<(LaunchContext, String), String> {
    let context = state.context().ok_or("缺少当前工作区。")?;
    match voice_desktop_common::device_config::load_from_root(&context.data) {
        voice_desktop_common::device_config::LoadResult::Valid(config)
            if config.mode == voice_desktop_common::device_config::RunMode::Remote
                && config.reconnect_suspended_reason.is_none() =>
        {
            let host = config.host.ok_or("请先完成局域网配对。")?;
            Ok((
                context,
                host.host_id.ok_or("配对记录缺少服务端编号，请重新配对。")?,
            ))
        }
        _ => Err("当前设备未处于允许连接的远程配对模式。".into()),
    }
}

fn local_installer(context: &LaunchContext) -> Result<RetainedInstaller, String> {
    app_update::retained(
        &context.install,
        env!("CARGO_PKG_VERSION"),
        &integrity_commands::local_build(context)?,
    )
}

/// require_compatible（业务准入）：普通业务只能在版本相同后进入，正式调试调用共用此判断。
#[cfg(feature = "ssh-device")]
pub(super) async fn require_compatible(state: &Desktop) -> Result<(), String> {
    if state.mode != FrontendMode::Product || state.stopping.load(Ordering::Acquire) {
        return Err("当前程序状态不能连接远程业务。".into());
    }
    if state.app_update.busy.load(Ordering::Acquire) {
        return Err("程序版本同步或维护正在进行，请等待完成。".into());
    }
    let (_, host) = paired_target(state)?;
    let response = state
        .ssh_device
        .lan_version_status(&host, env!("CARGO_PKG_VERSION"))
        .await?;
    if protocol::compare_versions(env!("CARGO_PKG_VERSION"), &response.version)?
        != VersionOrder::Equal
    {
        return Err("双端程序版本不同，请在项目弹窗中确认版本同步后再连接业务。".into());
    }
    Ok(())
}

#[tauri::command]
pub async fn device_version_check(
    window: WebviewWindow,
    state: tauri::State<'_, Arc<Desktop>>,
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    #[cfg(feature = "ssh-device")]
    {
        if state.mode != FrontendMode::Product || state.stopping.load(Ordering::Acquire) {
            return Err("当前程序状态不能检查设备版本。".into());
        }
        let (context, host) = paired_target(&state)?;
        let response = state
            .ssh_device
            .lan_version_status(&host, env!("CARGO_PKG_VERSION"))
            .await?;
        let order = protocol::compare_versions(env!("CARGO_PKG_VERSION"), &response.version)?;
        let source = if order == VersionOrder::Greater {
            local_installer(&context)
        } else {
            response.installer.clone().ok_or_else(|| {
                response
                    .error
                    .clone()
                    .unwrap_or_else(|| "服务端没有可用的当前版本分发器。".into())
            })
        };
        let reason = source.as_ref().err().cloned();
        Ok(
            serde_json::json!({"host_id":host,"local_version":env!("CARGO_PKG_VERSION"),"remote_version":response.version,
            "comparison":match order {VersionOrder::Equal=>"same",VersionOrder::Less=>"local_older",VersionOrder::Greater=>"remote_older"},
            "can_sync":order!=VersionOrder::Equal && source.is_ok(),"reason":if order==VersionOrder::Equal {None}else{reason},"remote_update_state":response.update_state,"remote_update_error":response.update_error,
            "fields":{"host_id":"当前已配对服务端编号","local_version":"本机正式版本","remote_version":"服务端正式版本","comparison":"版本比较，仅按正式版本号","can_sync":"高版本是否持有匹配的维护分发器","reason":"暂不能同步的中文原因","remote_update_state":"服务端最近本体同步状态","remote_update_error":"服务端本体更新交接错误"}}),
        )
    }
    #[cfg(not(feature = "ssh-device"))]
    {
        Err("当前构建未包含局域网设备连接。".into())
    }
}

/// Reservation（本体维护准入）：与在线更新、工作区迁移和设备切换互斥，失败自动释放。
struct Reservation {
    state: Arc<Desktop>,   // state（持有准入的当前正式宿主）。
    app: tauri::AppHandle, // app（维护进度和正式交接入口）。
    committed: bool,       // committed（已经成功启动分发器，当前程序将退出）。
    terminal: bool,        // terminal（传输已经正常结算，释放时不生成失败状态）。
}
impl Reservation {
    fn settle(&mut self) {
        self.terminal = true;
    }
    fn acquire(state: Arc<Desktop>, app: tauri::AppHandle) -> Result<Self, String> {
        reserve_maintenance(
            &state.maintenance_gate,
            &state.workspace_busy,
            &state.app_update.busy,
            true,
            Some(&state.device_operation),
        )?;
        state.app_update.cancel.store(false, Ordering::Release);
        let mut initial = maintenance_payload(
            MaintenanceAction::Update,
            "checking",
            None,
            0,
            0,
            Some("正在准备局域网版本同步"),
            None,
            true,
        );
        initial["source"] = serde_json::json!("lan");
        initial["fields"]["source"] = serde_json::json!("局域网版本同步来源");
        emit_lan_update(&state, &app, initial);
        Ok(Self {
            state,
            app,
            committed: false,
            terminal: false,
        })
    }
    fn progress(&self, record: &RetainedInstaller, done: u64, phase: &str, can_cancel: bool) {
        let mut value = maintenance_payload(
            MaintenanceAction::Update,
            "downloading",
            Some(&record.manifest),
            done,
            record.manifest.size,
            Some(phase),
            None,
            can_cancel,
        );
        value["source"] = serde_json::json!("lan");
        value["fields"]["source"] = serde_json::json!("局域网版本同步来源");
        emit_lan_update(&self.state, &self.app, value);
    }
    fn apply(&mut self, candidate: &mut IncomingInstaller) -> Result<(), String> {
        let result = handoff_local_update(
            &self.state,
            &self.app,
            &candidate.path,
            &candidate.installer,
            MaintenanceAction::Update,
            Some("lan"),
        );
        if let Err(mut error) = result {
            if let Some(manager) = self.app.try_state::<Arc<ResourceManager>>() {
                if let Err(recovery) = manager.resume_accepting_jobs() {
                    error = format!("{error}；恢复更新前服务失败：{recovery}");
                }
            }
            return Err(error);
        }
        candidate.handoff()?;
        self.committed = true;
        Ok(())
    }
}
impl Drop for Reservation {
    fn drop(&mut self) {
        if !self.committed {
            if !self.terminal {
                failed(
                    &self.state,
                    &self.app,
                    "版本同步连接结束，更新尚未完成；可重新同步。",
                );
            }
            self.state.app_update.busy.store(false, Ordering::Release);
        }
    }
}

fn failed(state: &Desktop, app: &tauri::AppHandle, error: &str) {
    let cancelled = state.app_update.cancel.load(Ordering::Acquire);
    let mut value = maintenance_payload(
        MaintenanceAction::Update,
        if cancelled { "cancelled" } else { "failed" },
        None,
        0,
        0,
        Some("局域网版本同步未完成"),
        Some(error),
        false,
    );
    value["source"] = serde_json::json!("lan");
    value["fields"]["source"] = serde_json::json!("局域网版本同步来源");
    emit_lan_update(state, app, value);
}

#[tauri::command]
pub async fn device_version_sync(
    window: WebviewWindow,
    app: tauri::AppHandle,
    state: tauri::State<'_, Arc<Desktop>>,
    expected_host_id: String, // expected_host_id（弹窗确认的配对目标）。
    expected_remote_version: String, // expected_remote_version（弹窗展示的服务端版本）。
    confirmed: bool,          // confirmed（用户已确认目标设备退出并升级）。
) -> Result<serde_json::Value, String> {
    trusted(&window, &state)?;
    #[cfg(feature = "ssh-device")]
    {
        if !confirmed
            || state.mode != FrontendMode::Product
            || state.stopping.load(Ordering::Acquire)
        {
            return Err("请先在项目弹窗中确认版本同步。".into());
        }
        let (_, host) = paired_target(&state)?;
        if host != expected_host_id {
            return Err("配对目标已变化，请重新检查版本。".into());
        }
        protocol::compare_versions(&expected_remote_version, env!("CARGO_PKG_VERSION"))?;
        let reservation = Reservation::acquire(state.inner().clone(), app.clone())?;
        let desktop = state.inner().clone();
        let mut initial = maintenance_payload(
            MaintenanceAction::Update,
            "checking",
            None,
            0,
            0,
            Some("正在复核双方版本与局域网分发器"),
            None,
            true,
        );
        initial["source"] = serde_json::json!("lan");
        initial["fields"]["source"] = serde_json::json!("局域网版本同步来源");
        emit_lan_update(&desktop, &app, initial.clone());
        tauri::async_runtime::spawn(async move {
            let result =
                sync_client(&desktop, &app, &host, &expected_remote_version, reservation).await;
            if let Err(error) = result {
                failed(&desktop, &app, &error);
            }
        });
        Ok(initial)
    }
    #[cfg(not(feature = "ssh-device"))]
    {
        let _ = (app, expected_host_id, expected_remote_version, confirmed);
        Err("当前构建未包含局域网设备连接。".into())
    }
}

#[cfg(feature = "ssh-device")]
async fn sync_client(
    state: &Arc<Desktop>,
    app: &tauri::AppHandle,
    host: &str,
    expected: &str,
    mut reservation: Reservation,
) -> Result<(), String> {
    let (context, current) = paired_target(state)?;
    if current != host {
        return Err("配对目标已变化。".into());
    }
    let mut channel = state.ssh_device.open_lan_update(host).await?;
    let result = async {
        let status = channel
            .exchange(
                Operation::Status {
                    version: env!("CARGO_PKG_VERSION").into(),
                },
                &state.app_update.cancel,
            )
            .await?;
        if status.version != expected {
            return Err("服务端版本在确认期间发生变化，请重新确认。".into());
        }
        match protocol::compare_versions(env!("CARGO_PKG_VERSION"), &status.version)? {
            VersionOrder::Equal => return Err("双端版本已经一致，无需同步。".into()),
            VersionOrder::Less => {
                let ready = channel
                    .exchange(Operation::Download, &state.app_update.cancel)
                    .await?;
                if ready.state != "ready" {
                    return Err("服务端没有准备更新分发器。".into());
                }
                let record = ready.installer.ok_or("服务端未提供分发器身份。")?;
                protocol::validate_installer(&record, expected)?;
                let mut candidate = IncomingInstaller::create(&context.install, record)?;
                while candidate.offset < candidate.installer.manifest.size {
                    let offset = candidate.offset;
                    let chunk = channel
                        .exchange(Operation::Read { offset }, &state.app_update.cancel)
                        .await?;
                    if chunk.state != "chunk" || chunk.offset != offset {
                        return Err("更新下载分块回执偏移无效。".into());
                    }
                    candidate.append(
                        offset,
                        chunk.data_base64.as_deref().ok_or("缺少更新分块。")?,
                        chunk.sha256.as_deref().ok_or("缺少分块摘要。")?,
                    )?;
                    reservation.progress(
                        &candidate.installer,
                        candidate.offset,
                        "正在从局域网服务端接收分发器",
                        true,
                    );
                }
                let complete = channel
                    .exchange(Operation::Finish, &state.app_update.cancel)
                    .await?;
                if complete.state != "verified" {
                    return Err("服务端尚未确认分发器传输完整。".into());
                }
                let app = app.clone();
                let desktop = state.clone();
                tauri::async_runtime::spawn_blocking(move || {
                    let result = candidate
                        .finish_cancellable(&reservation.state.app_update.cancel)
                        .and_then(|()| reservation.apply(&mut candidate));
                    if let Err(error) = &result {
                        failed(&desktop, &app, error);
                    }
                    result
                })
                .await
                .map_err(|e| e.to_string())??;
            }
            VersionOrder::Greater => {
                let record = local_installer(&context)?;
                let path = app_update::installer_path(&context.install)?;
                let verify_path = path.clone();
                let verify_record = record.clone();
                let cancel = state.app_update.cancel.clone();
                tauri::async_runtime::spawn_blocking(move || {
                    protocol::verify_installer_cancellable(&verify_path, &verify_record, &cancel)
                })
                .await
                .map_err(|e| e.to_string())??;
                let mut file = File::open(&path).map_err(|e| e.to_string())?;
                let ready = channel
                    .exchange(
                        Operation::Upload {
                            installer: record.clone(),
                        },
                        &state.app_update.cancel,
                    )
                    .await?;
                if ready.state != "ready" {
                    return Err("服务端未接受本体更新。".into());
                }
                let mut offset = 0;
                let mut buffer = vec![0; protocol::CHUNK_BYTES];
                loop {
                    let count = file.read(&mut buffer).map_err(|e| e.to_string())?;
                    if count == 0 {
                        break;
                    }
                    let bytes = &buffer[..count];
                    let response = channel
                        .exchange(
                            Operation::Write {
                                offset,
                                data_base64: base64::engine::general_purpose::STANDARD
                                    .encode(bytes),
                                sha256: protocol::chunk_hash(bytes),
                            },
                            &state.app_update.cancel,
                        )
                        .await?;
                    offset += count as u64;
                    if response.state != "ready" || response.offset != offset {
                        return Err("服务端更新分块确认无效。".into());
                    }
                    reservation.progress(&record, offset, "正在向局域网服务端传送分发器", true);
                }
                let complete = channel
                    .exchange(Operation::Finish, &state.app_update.cancel)
                    .await?;
                if complete.state != "verified" {
                    return Err("服务端尚未完成分发器校验。".into());
                }
                // 取消与更新交接使用同一门闩；接受取消后不会再向目标提交应用请求。
                state.app_update.begin_handoff(maintenance_payload(
                    MaintenanceAction::Update,
                    "handing_off",
                    Some(&record.manifest),
                    offset,
                    offset,
                    Some("正在请求服务端更新"),
                    None,
                    false,
                ))?;
                let applied = channel
                    .exchange(Operation::Apply, &AtomicBool::new(false))
                    .await?;
                if applied.state != "handing_off" {
                    return Err("服务端未进入更新交接。".into());
                }
                emit_lan_update(
                    state,
                    app,
                    maintenance_payload(
                        MaintenanceAction::Update,
                        "current",
                        Some(&record.manifest),
                        offset,
                        offset,
                        Some("分发器已完整送达，等待服务端更新重启并重新检查版本"),
                        None,
                        false,
                    ),
                );
                reservation.settle();
            }
        }
        Ok(())
    }
    .await;
    channel.close().await;
    state.ssh_device.invalidate_lan_version().await;
    result
}

#[cfg(feature = "ssh-device")]
pub(super) struct ServerHandler {
    app: tauri::AppHandle,  // app（当前正式宿主）。
    state: Arc<Desktop>,    // state（正式维护准入与更新交接）。
    context: LaunchContext, // context（绑定当前工作区）。
    peer: voice_desktop_common::device_file_channel::FileChannelPeer, // peer（强制命令已认证身份）。
    host_id: String,                                                  // host_id（本机服务端编号）。
    client_version: Option<String>, // client_version（本条连接已交换的客户端版本）。
    outgoing: Option<(File, RetainedInstaller, u64)>, // outgoing（本机分发器的顺序读取状态）。
    incoming: Option<IncomingInstaller>, // incoming（当前接收的较新版本候选）。
    reservation: Option<Reservation>, // reservation（传输及应用期间的维护锁）。
    pending_apply: Option<std::sync::mpsc::Sender<()>>, // pending_apply（工作线程已创建，回执完整写出后才开始安装）。
}
#[cfg(feature = "ssh-device")]
impl ServerHandler {
    pub(super) fn new(
        app: tauri::AppHandle,
        peer: voice_desktop_common::device_file_channel::FileChannelPeer,
    ) -> Result<Box<dyn Handler>, String> {
        let state = app
            .try_state::<Arc<Desktop>>()
            .ok_or("正式宿主尚未就绪。")?
            .inner()
            .clone();
        let context = state.context().ok_or("缺少当前工作区。")?;
        let config = match voice_desktop_common::device_config::load_from_root(&context.data) {
            voice_desktop_common::device_config::LoadResult::Valid(config) => config,
            _ => return Err("设备广播配置不可用。".into()),
        };
        Ok(Box::new(Self {
            app,
            state,
            context,
            peer,
            host_id: config.device_id,
            client_version: None,
            outgoing: None,
            incoming: None,
            reservation: None,
            pending_apply: None,
        }))
    }
    fn validate_peer(&self) -> Result<(), String> {
        if self.state.stopping.load(Ordering::Acquire)
            || self.peer.workspace_id != self.context.workspace_id
            || self.peer.owner_sid != voice_desktop_common::system_feature::current_user_sid()?
            || self.peer.build != integrity_commands::local_build(&self.context)?
        {
            return Err("本体同步的桥接身份已失效。".into());
        }
        let trust = voice_desktop_common::device_trust::DeviceTrustStore::open(&self.context.data)?;
        if trust.snapshot().state != "valid"
            || trust.trusted_public_key(&self.peer.device_id).is_none()
        {
            return Err("设备信任已被撤销。".into());
        }
        match voice_desktop_common::device_config::load_from_root(&self.context.data) {
            voice_desktop_common::device_config::LoadResult::Valid(config)
                if config.mode == voice_desktop_common::device_config::RunMode::Local
                    && config.broadcast_enabled
                    && config.device_id == self.host_id =>
            {
                Ok(())
            }
            _ => Err("服务端已关闭广播或切换设备模式。".into()),
        }
    }
}
#[cfg(feature = "ssh-device")]
impl Handler for ServerHandler {
    fn request(&mut self, request: Request) -> Result<Response, String> {
        self.validate_peer()?;
        let mut response = Response::new(&self.host_id, env!("CARGO_PKG_VERSION"), "ready");
        if self.reservation.is_some() && self.state.app_update.cancel.load(Ordering::Acquire) {
            return Err("本机已取消版本同步。".into());
        }
        match request.operation {
            Operation::Status { version } => {
                if self.client_version.is_some() {
                    return Err("同步连接已绑定版本，不能重复更换身份。".into());
                }
                protocol::compare_versions(&version, env!("CARGO_PKG_VERSION"))?;
                self.client_version = Some(version);
                response.state = "status".into();
                if let Ok(status) = self.state.app_update.status.lock() {
                    if status["source"] == "lan" {
                        response.update_state = status["state"].as_str().map(str::to_owned);
                        response.update_error = status["error"].as_str().map(str::to_owned);
                    }
                }
                match local_installer(&self.context) {
                    Ok(record) => response.installer = Some(record),
                    Err(error) => response.error = Some(error),
                }
            }
            Operation::Download => {
                if self.reservation.is_some()
                    || protocol::compare_versions(
                        self.client_version.as_deref().ok_or("请先交换产品版本。")?,
                        env!("CARGO_PKG_VERSION"),
                    )? != VersionOrder::Less
                {
                    return Err("仅允许向较低版本提供本机当前分发器。".into());
                }
                self.reservation =
                    Some(Reservation::acquire(self.state.clone(), self.app.clone())?);
                let record = local_installer(&self.context)?;
                let path = app_update::installer_path(&self.context.install)?;
                protocol::verify_installer_cancellable(
                    &path,
                    &record,
                    &self.state.app_update.cancel,
                )?;
                self.outgoing = Some((
                    File::open(path).map_err(|e| e.to_string())?,
                    record.clone(),
                    0,
                ));
                response.installer = Some(record);
            }
            Operation::Upload { installer } => {
                if self.reservation.is_some() {
                    return Err("本次同步已绑定分发器。".into());
                }
                let version = self.client_version.as_deref().ok_or("请先交换产品版本。")?;
                protocol::validate_installer(&installer, version)?;
                if protocol::compare_versions(version, env!("CARGO_PKG_VERSION"))?
                    != VersionOrder::Greater
                {
                    return Err("只接受来自较高产品版本的更新。".into());
                }
                self.reservation =
                    Some(Reservation::acquire(self.state.clone(), self.app.clone())?);
                self.incoming = Some(IncomingInstaller::create(&self.context.install, installer)?);
            }
            Operation::Read { offset } => {
                let (file, record, next) = self.outgoing.as_mut().ok_or("没有可读取的分发器。")?;
                if offset != *next || offset >= record.manifest.size {
                    return Err("分发器读取偏移无效。".into());
                }
                let mut bytes =
                    vec![0; protocol::CHUNK_BYTES.min((record.manifest.size - offset) as usize)];
                file.read_exact(&mut bytes).map_err(|e| e.to_string())?;
                *next += bytes.len() as u64;
                response.state = "chunk".into();
                response.offset = offset;
                response.sha256 = Some(protocol::chunk_hash(&bytes));
                response.data_base64 =
                    Some(base64::engine::general_purpose::STANDARD.encode(bytes));
            }
            Operation::Write {
                offset,
                data_base64,
                sha256,
            } => {
                let candidate = self.incoming.as_mut().ok_or("没有已接受的更新候选。")?;
                candidate.append(offset, &data_base64, &sha256)?;
                response.offset = candidate.offset;
                self.reservation.as_ref().ok_or("缺少维护准入。")?.progress(
                    &candidate.installer,
                    candidate.offset,
                    "正在接收局域网客户端的版本分发器",
                    true,
                );
            }
            Operation::Finish => {
                if let Some(candidate) = self.incoming.as_mut() {
                    candidate.finish_cancellable(&self.state.app_update.cancel)?;
                } else if self
                    .outgoing
                    .as_ref()
                    .is_none_or(|(_, record, offset)| *offset != record.manifest.size)
                {
                    return Err("分发器尚未完整传输。".into());
                }
                if self.outgoing.is_some() {
                    if let Some(reservation) = self.reservation.as_mut() {
                        reservation.terminal = true;
                    }
                    emit_lan_update(
                        &self.state,
                        &self.app,
                        maintenance_payload(
                            MaintenanceAction::Update,
                            "current",
                            None,
                            0,
                            0,
                            Some("已向配对设备提供完整分发器"),
                            None,
                            false,
                        ),
                    );
                }
                response.state = "verified".into();
            }
            Operation::Apply => {
                // handoff()（候选交接）只在完整摘要验证后成功；实际保留等安装器成功启动后确认。
                let candidate = self.incoming.as_mut().ok_or("没有已接收的更新候选。")?;
                candidate.finish_cancellable(&self.state.app_update.cancel)?;
                self.state.app_update.begin_handoff(maintenance_payload(
                    MaintenanceAction::Update,
                    "handing_off",
                    Some(&candidate.installer.manifest),
                    candidate.offset,
                    candidate.offset,
                    Some("正在交接局域网分发器"),
                    None,
                    false,
                ))?;
                let mut candidate = self.incoming.take().ok_or("缺少更新候选。")?;
                let mut reservation = self.reservation.take().ok_or("缺少维护准入。")?;
                let app = self.app.clone();
                let state = self.state.clone();
                let (ready, start) = std::sync::mpsc::channel();
                std::thread::Builder::new()
                    .name("lan-app-update".into())
                    .spawn(move || {
                        if start.recv().is_ok() {
                            if let Err(error) = reservation.apply(&mut candidate) {
                                reservation.settle();
                                failed(&state, &app, &error);
                            }
                        }
                    })
                    .map_err(|e| e.to_string())?;
                self.pending_apply = Some(ready);
                response.state = "handing_off".into();
            }
            Operation::Cancel => {
                if self.reservation.is_some() {
                    self.state.app_update.cancel.store(true, Ordering::Release);
                }
                self.incoming.take();
                self.outgoing.take();
                self.reservation.take();
                response.state = "cancelled".into();
            }
        }
        Ok(response)
    }
    fn flushed(&mut self, response: &Response) -> Result<(), String> {
        if response.state == "handing_off" {
            self.pending_apply
                .take()
                .ok_or("更新工作线程尚未准备。")?
                .send(())
                .map_err(|_| "更新工作线程提前结束。")?;
        }
        Ok(())
    }
}

// emit_lan_update（局域网进度）：避免其他应用更新事件混入版本同步弹窗。
fn emit_lan_update(state: &Desktop, app: &tauri::AppHandle, mut payload: serde_json::Value) {
    payload["source"] = serde_json::json!("lan");
    payload["fields"]["source"] = serde_json::json!("局域网版本同步来源");
    super::emit_app_update(state, app, payload);
}
