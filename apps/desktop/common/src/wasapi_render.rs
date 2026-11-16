//! WASAPI render（WASAPI 播放）：有界的默认输出端点共享模式 PCM 播放流。
use std::{collections::{BTreeMap, HashSet, VecDeque}, time::Instant};

use crate::wasapi_capture::{CaptureFormat, SampleEncoding};

pub const MAX_PLAYBACK_QUEUE_MS: u64 = 2_000; // MAX_PLAYBACK_QUEUE_MS（本地播放储备上限，毫秒）。
pub const MAX_PACKET_MS: u64 = 2_000; // MAX_PACKET_MS（单次播放数据包时长上限，毫秒）。

/// PlaybackSnapshot（播放快照）：只报告音频端点设备时钟及有界本地队列事实。
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
pub struct PlaybackSnapshot {
    pub enqueued_ms: u64,         // enqueued_ms（已受理播放时长，毫秒）。
    pub played_ms: u64,           // played_ms（已提交给 WASAPI 端点的内容时长，毫秒）。
    pub buffered_ms: u64,         // buffered_ms（端点与本地排队时长，毫秒）。
    pub output_estimated_ms: u64, // output_estimated_ms（端点时钟输出估计，毫秒）。
    pub started: bool,            // started（设备时钟是否已开始推进）。
    pub device_time_us: u64,      // device_time_us（设备时钟对应的 QPC 时刻，微秒）。
}

/// PlaybackReplacementProgress（替换状态）：只跨 IPC 传编号、时钟与结算字段。
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct PlaybackReplacementProgress {
    pub replacement_id: String, // replacement_id（替换事务编号）。
    pub old_playback_key: String, // old_playback_key（固定旧播放键）。
    pub successor_playback_key: Option<String>, // successor_playback_key（已绑定的后继播放键）。
    pub reason: Option<String>, // reason（替换结束原因）。
    pub started_device_time_us: u64, // started_device_time_us（受理替换的设备时钟，微秒）。
    pub finished_device_time_us: Option<u64>, // finished_device_time_us（旧音出口结清时钟，微秒）。
    pub finished_output_estimated_ms: Option<u64>, // finished_output_estimated_ms（旧音结清时设备输出估计位置，毫秒）。
    pub events_device_time_us: BTreeMap<String, u64>, // events_device_time_us（替换阶段事件设备时钟，微秒）。
    pub attenuated_range_ms: [u32; 2], // attenuated_range_ms（旧音衰减不确定区间，毫秒）。
    pub done: bool, // done（旧音设备输出是否已结清）。
}

struct RenderPacket {
    playback_key: String, // playback_key（固定播放事务编号）。
    bytes: Vec<u8>, // bytes（按默认混音格式编码的音频帧）。
    head_offset_bytes: usize, // head_offset_bytes（首个尚未提交字节位置）。
}

struct RenderReplacement {
    progress: PlaybackReplacementProgress, // progress（跨进程可序列化的替换事实）。
    sample_rate: u32, // sample_rate（输出端点采样率）。
    block_align: usize, // block_align（输出端点每帧字节数）。
    max_ms: u32, // max_ms（替换硬时限，毫秒）。
    duration_frames: u64, // duration_frames（旧音普通衰减包络采样数）。
    early_frames: u64, // early_frames（后继就绪后的 30 毫秒淡出/淡入采样数）。
    initial_endpoint_frames: u64, // initial_endpoint_frames（受理时已提交给端点的旧音储备）。
    rendered_old_frames: u64, // rendered_old_frames（过渡后提交的旧音采样数）。
    rendered_new_frames: u64, // rendered_new_frames（后继淡入已提交采样数）。
    early_position: Option<u64>, // early_position（后继准备时旧音包络的位置）。
    early_gain: f64, // early_gain（后继提前接入时旧音当前增益）。
    initial_played_ms: u64, // initial_played_ms（受理时的累计端点提交时长）。
    completion_output_estimated_ms: u64, // completion_output_estimated_ms（旧音结清设备时钟目标）。
    started_at: Instant, // started_at（本机单调时钟受理时刻）。
}

impl RenderReplacement {
    fn elapsed_frames(&self) -> u64 {
        let wall_frames = (self.started_at.elapsed().as_nanos()
            .saturating_mul(u128::from(self.sample_rate)) / 1_000_000_000)
            .min(u128::from(u64::MAX)) as u64;
        wall_frames.max(self.initial_endpoint_frames.saturating_add(self.rendered_old_frames))
    }

    fn base_gain(&self, position: u64) -> f64 {
        let fraction = position as f64 / self.duration_frames.max(1) as f64;
        let gain = if fraction < 0.6 {
            1.0 - 0.7 * fraction / 0.6
        } else {
            0.3 * (1.0 - fraction) / 0.4
        };
        gain.clamp(0.0, 1.0)
    }
}

/// Pcm16RenderQueue（PCM 播放队列）：按事务键有界接收，并在设备端处理旧音收尾。
#[derive(Default)]
pub struct Pcm16RenderQueue {
    packets: VecDeque<RenderPacket>,
    queued_frames: u64,
    accepted_frames: u64,
    closed_keys: HashSet<String>, // closed_keys（已停止或开始替换的播放键）。
    closed_key_order: VecDeque<String>, // closed_key_order（有界闭键回收顺序）。
    replacement: Option<RenderReplacement>, // replacement（当前 WASAPI 设备端旧音过渡）。
}

impl Pcm16RenderQueue {
    /// enqueue（加入队列）：拒绝错误长度、非整帧或超过播放储备上限的数据。
    pub fn enqueue(
        &mut self,
        bytes: Vec<u8>,
        frames: usize,
        block_align: usize,
        max_frames: u64,
    ) -> Result<(), String> {
        self.enqueue_for_key(
            "local-playback", bytes, frames, block_align, max_frames,
            0, 0, 0, 0,
        )
    }

    /// enqueue_for_key（按播放事务入队）：开始替换后拒绝旧键迟到音频。
    pub fn enqueue_for_key(
        &mut self,
        playback_key: &str,
        bytes: Vec<u8>,
        frames: usize,
        block_align: usize,
        max_frames: u64,
        sample_rate: u32,
        output_estimated_ms: u64,
        buffered_ms: u64,
        device_time_us: u64,
    ) -> Result<(), String> {
        let expected = frames.checked_mul(block_align).ok_or("播放数据长度溢出")?;
        if playback_key.is_empty() || playback_key.len() > 128 || playback_key.trim() != playback_key
                || frames == 0 || block_align == 0 || bytes.len() != expected {
            return Err("播放数据必须包含完整音频帧".into());
        }
        if self.closed_keys.contains(playback_key) {
            return Err("该播放键已关闭，迟到音频不能入队".into());
        }
        if self.queued_frames.saturating_add(frames as u64) > max_frames {
            return Err("本地播放储备已达上限".into());
        }
        let mut early_handoff = None;
        if let Some(replacement) = self.replacement.as_mut().filter(|item| !item.progress.done) {
            if playback_key == replacement.progress.old_playback_key {
                return Err("旧播放键已进入替换收尾，不能继续追加音频".into());
            }
            let Some(successor) = replacement.progress.successor_playback_key.as_deref() else {
                return Err("替换事务尚未绑定后继播放键".into());
            };
            if playback_key != successor {
                return Err("播放键与当前替换事务后继目标不匹配".into());
            }
            if replacement.early_position.is_none() {
                let position = replacement.elapsed_frames();
                replacement.early_position = Some(position);
                replacement.early_gain = replacement.base_gain(position);
                replacement.progress.events_device_time_us
                    .entry("new_audio_ready".into()).or_insert(device_time_us);
                let old_key = replacement.progress.old_playback_key.clone();
                early_handoff = Some((old_key, replacement.early_frames));
            }
        }
        if let Some((old_key, early_frames)) = early_handoff {
            let queued_before_ms = self.queued_frames.saturating_mul(1_000)
                / u64::from(sample_rate.max(1));
            let endpoint_ms = buffered_ms.saturating_sub(queued_before_ms);
            self.trim_key_frames(&old_key, early_frames, block_align);
            let retained = self.queued_frames_for_key(&old_key, block_align);
            if let Some(replacement) = self.replacement.as_mut() {
                replacement.completion_output_estimated_ms = output_estimated_ms
                    .saturating_add(endpoint_ms)
                    .saturating_add(retained.saturating_mul(1_000) / u64::from(sample_rate.max(1)));
            }
        }
        self.queued_frames += frames as u64;
        self.accepted_frames = self.accepted_frames.saturating_add(frames as u64);
        self.packets.push_back(RenderPacket {
            playback_key: playback_key.to_owned(),
            bytes,
            head_offset_bytes: 0,
        });
        Ok(())
    }

    /// prepare_replacement（准备设备端替换）：固定旧播放键、保留有界淡出尾音。
    pub fn prepare_replacement(
        &mut self,
        replacement_id: &str,
        old_playback_key: &str,
        max_ms: u32,
        sample_rate: u32,
        block_align: usize,
        buffered_ms: u64,
        played_ms: u64,
        output_estimated_ms: u64,
        device_time_us: u64,
    ) -> Result<PlaybackReplacementProgress, String> {
        if replacement_id.is_empty() || replacement_id.len() > 128
                || replacement_id.trim() != replacement_id
                || old_playback_key.is_empty() || old_playback_key.len() > 128
                || old_playback_key.trim() != old_playback_key
                || !(1..=600).contains(&max_ms) || sample_rate == 0 || block_align == 0 {
            return Err("替换编号、播放键、时长或输出格式无效".into());
        }
        if let Some(existing) = self.replacement.as_ref() {
            if existing.progress.replacement_id == replacement_id
                && existing.progress.old_playback_key == old_playback_key
            {
                return Ok(existing.progress.clone());
            }
            if !existing.progress.done {
                return Err("当前设备已有未结算的替换事务".into());
            }
        }
        if self.closed_keys.contains(old_playback_key) {
            return Err("替换旧播放键已经关闭".into());
        }
        let duration_ms = u64::from(max_ms.min(500));
        let duration_frames = (duration_ms * u64::from(sample_rate) / 1_000).max(1);
        let software_queued_ms = self.queued_frames.saturating_mul(1_000) / u64::from(sample_rate);
        let endpoint_pending_ms = buffered_ms.saturating_sub(software_queued_ms);
        let endpoint_pending_frames = endpoint_pending_ms.saturating_mul(u64::from(sample_rate)) / 1_000;
        let retained_limit = duration_frames.saturating_sub(endpoint_pending_frames);
        self.trim_key_frames(old_playback_key, retained_limit, block_align);
        self.remember_closed_key(old_playback_key);
        let retained_frames = self.queued_frames_for_key(old_playback_key, block_align);
        let endpoint_pending_ms = endpoint_pending_frames.saturating_mul(1_000) / u64::from(sample_rate);
        let retained_ms = retained_frames.saturating_mul(1_000) / u64::from(sample_rate);
        let mut events = BTreeMap::new();
        events.insert("cancel_accepted".into(), device_time_us);
        let done = endpoint_pending_frames == 0 && retained_frames == 0;
        let reason = done.then(|| "no_remaining_audio".to_owned());
        if done { events.insert("old_audio_silenced".into(), device_time_us); }
        let progress = PlaybackReplacementProgress {
            replacement_id: replacement_id.to_owned(),
            old_playback_key: old_playback_key.to_owned(),
            successor_playback_key: None,
            reason,
            started_device_time_us: device_time_us,
            finished_device_time_us: done.then_some(device_time_us),
            finished_output_estimated_ms: done.then_some(output_estimated_ms),
            events_device_time_us: events,
            attenuated_range_ms: [played_ms.min(u64::from(u32::MAX)) as u32,
                played_ms.saturating_add(retained_ms).min(u64::from(u32::MAX)) as u32],
            done,
        };
        self.replacement = Some(RenderReplacement {
            progress: progress.clone(),
            sample_rate,
            block_align,
            max_ms,
            duration_frames,
            early_frames: (u64::from(sample_rate) * 30 / 1_000).max(1),
            initial_endpoint_frames: endpoint_pending_frames,
            rendered_old_frames: 0,
            rendered_new_frames: 0,
            early_position: None,
            early_gain: 0.0,
            initial_played_ms: played_ms,
            completion_output_estimated_ms: output_estimated_ms
                .saturating_add(endpoint_pending_ms).saturating_add(retained_ms),
            started_at: Instant::now(),
        });
        Ok(progress)
    }

    /// bind_replacement（绑定后继）：普通播放许可不会继承替换票据。
    pub fn bind_replacement(
        &mut self,
        replacement_id: &str,
        successor_playback_key: &str,
    ) -> Result<bool, String> {
        if successor_playback_key.is_empty() || successor_playback_key.len() > 128
                || successor_playback_key.trim() != successor_playback_key {
            return Err("替换后继播放键无效".into());
        }
        let Some(replacement) = self.replacement.as_mut() else { return Ok(false) };
        if replacement.progress.replacement_id != replacement_id {
            return Ok(false);
        }
        if successor_playback_key == replacement.progress.old_playback_key {
            return Err("替换后继必须使用新的播放键".into());
        }
        if let Some(bound) = replacement.progress.successor_playback_key.as_deref() {
            return Ok(bound == successor_playback_key);
        }
        replacement.progress.successor_playback_key = Some(successor_playback_key.to_owned());
        Ok(true)
    }

    /// abort_replacement（撤销设备端替换）：丢弃旧键淡出尾音并返回可核对状态。
    pub fn abort_replacement(
        &mut self,
        replacement_id: &str,
        reason: &str,
        device_time_us: u64,
        output_estimated_ms: u64,
        block_align: usize,
    ) -> Result<Option<PlaybackReplacementProgress>, String> {
        let Some(replacement) = self.replacement.as_ref() else { return Ok(None) };
        if replacement.progress.replacement_id != replacement_id { return Ok(None); }
        let old_key = replacement.progress.old_playback_key.clone();
        let was_done = replacement.progress.done;
        if !was_done {
            self.remove_key_packets(&old_key, block_align);
            let replacement = self.replacement.as_mut().expect("匹配的替换状态仍存在");
            replacement.progress.reason = Some(reason.to_owned());
            replacement.progress.finished_device_time_us = Some(device_time_us);
            replacement.progress.finished_output_estimated_ms = Some(output_estimated_ms);
            replacement.progress.done = true;
            replacement.progress.events_device_time_us.insert("old_audio_silenced".into(), device_time_us);
        }
        Ok(self.replacement.as_ref().map(|item| item.progress.clone()))
    }

    /// replacement_status（读取替换状态）：只返回当前稳定编号及设备时钟事实。
    pub fn replacement_status(&self, replacement_id: &str) -> Option<PlaybackReplacementProgress> {
        self.replacement.as_ref()
            .filter(|item| item.progress.replacement_id == replacement_id)
            .map(|item| item.progress.clone())
    }

    /// update_output_progress（更新设备输出进度）：仅在旧键队列已空且设备时钟越过目标时结算。
    pub fn update_output_progress(&mut self, output_estimated_ms: u64, device_time_us: u64) {
        let Some(pending) = self.replacement.as_ref() else { return };
        if pending.progress.done { return; }
        let old_key = pending.progress.old_playback_key.clone();
        let block_align = pending.block_align;
        let target = pending.completion_output_estimated_ms;
        if self.queued_frames_for_key(&old_key, block_align) != 0
                || output_estimated_ms < target {
            return;
        }
        let Some(replacement) = self.replacement.as_mut() else { return };
        let reason = if replacement.early_position.is_some() {
            "early_handoff"
        } else if replacement.started_at.elapsed().as_millis() >= u128::from(replacement.max_ms) {
            "deadline"
        } else {
            "envelope_complete"
        };
        replacement.progress.reason = Some(reason.into());
        replacement.progress.finished_device_time_us = Some(device_time_us);
        replacement.progress.finished_output_estimated_ms = Some(output_estimated_ms);
        replacement.progress.events_device_time_us.insert("old_audio_silenced".into(), device_time_us);
        replacement.progress.done = true;
    }

    /// fill（填充端点缓冲）：不阻塞等待生产者；保留兼容的无键诊断入口。
    pub fn fill(
        &mut self,
        destination: &mut [u8],
        frames: usize,
        block_align: usize,
        silence_byte: u8,
    ) -> Result<usize, String> {
        self.fill_inner(destination, frames, block_align, silence_byte, None, 0)
    }

    /// fill_for_device（按事务键并应用淡出/淡入包络填充 WASAPI）。
    pub fn fill_for_device(
        &mut self,
        destination: &mut [u8],
        frames: usize,
        format: CaptureFormat,
        device_time_us: u64,
    ) -> Result<usize, String> {
        self.fill_inner(destination, frames, format.block_align as usize,
            silence_byte(format.encoding), Some(format), device_time_us)
    }

    fn fill_inner(
        &mut self,
        destination: &mut [u8],
        frames: usize,
        block_align: usize,
        silence: u8,
        format: Option<CaptureFormat>,
        device_time_us: u64,
    ) -> Result<usize, String> {
        let requested_bytes = frames.checked_mul(block_align).ok_or("WASAPI 输出缓冲长度溢出")?;
        if block_align == 0 || destination.len() != requested_bytes {
            return Err("WASAPI 输出缓冲与音频帧布局不一致".into());
        }
        destination.fill(silence);
        let mut filled_frames = 0;
        while filled_frames < frames {
            let Some(packet) = self.packets.front() else { break };
            let key = packet.playback_key.clone();
            let source_start = packet.head_offset_bytes;
            let available_bytes = packet.bytes.len().saturating_sub(source_start);
            let available_frames = available_bytes / block_align;
            let count = available_frames.min(frames - filled_frames);
            if count == 0 { return Err("播放队列中存在未对齐音频数据".into()); }
            let source_end = source_start + count * block_align;
            let destination_start = filled_frames * block_align;
            let destination_end = destination_start + count * block_align;
            destination[destination_start..destination_end]
                .copy_from_slice(&packet.bytes[source_start..source_end]);
            if let Some(format) = format {
                for frame_index in 0..count {
                    let gain = self.replacement_gain(&key, device_time_us);
                    if gain < 0.999_999 {
                        let frame_start = destination_start + frame_index * block_align;
                        scale_encoded_frame(&mut destination[frame_start..frame_start + block_align], format, gain);
                    }
                }
            }
            filled_frames += count;
            self.queued_frames = self.queued_frames.saturating_sub(count as u64);
            let packet = self.packets.front_mut().expect("读取过的播放队列头仍存在");
            packet.head_offset_bytes = source_end;
            if packet.head_offset_bytes == packet.bytes.len() { self.packets.pop_front(); }
        }
        if let Some(replacement) = self.replacement.as_mut() {
            let now_us = device_time_us;
            if replacement.progress.events_device_time_us.contains_key("new_audio_ready")
                    && !replacement.progress.events_device_time_us.contains_key("new_audio_first_output")
                    && replacement.rendered_new_frames > 0 {
                replacement.progress.events_device_time_us.insert("new_audio_first_output".into(), now_us);
            }
        }
        Ok(filled_frames)
    }

    fn replacement_gain(&mut self, playback_key: &str, device_time_us: u64) -> f64 {
        let Some(replacement) = self.replacement.as_mut().filter(|item| !item.progress.done) else {
            return 1.0;
        };
        if playback_key == replacement.progress.old_playback_key {
            let position = replacement.elapsed_frames();
            replacement.progress.events_device_time_us
                .entry("first_attenuated_audio".into()).or_insert(device_time_us);
            let gain = if let Some(early_position) = replacement.early_position {
                let elapsed = position.saturating_sub(early_position);
                replacement.early_gain * (1.0 - elapsed as f64 / replacement.early_frames as f64).max(0.0)
            } else {
                replacement.base_gain(position)
            };
            replacement.rendered_old_frames = replacement.rendered_old_frames.saturating_add(1);
            let attenuated_ms = replacement.rendered_old_frames.saturating_mul(1_000)
                / u64::from(replacement.sample_rate);
            replacement.progress.attenuated_range_ms[1] = replacement.initial_played_ms
                .saturating_add(attenuated_ms).min(u64::from(u32::MAX)) as u32;
            gain
        } else if replacement.progress.successor_playback_key.as_deref() == Some(playback_key) {
            replacement.progress.events_device_time_us
                .entry("new_audio_first_output".into()).or_insert(device_time_us);
            let gain = if replacement.early_position.is_some() {
                (replacement.rendered_new_frames as f64 / replacement.early_frames as f64).min(1.0)
            } else { 1.0 };
            replacement.rendered_new_frames = replacement.rendered_new_frames.saturating_add(1);
            gain
        } else {
            1.0
        }
    }

    fn trim_key_frames(&mut self, key: &str, keep_frames: u64, block_align: usize) {
        let mut remaining = keep_frames;
        let mut queued = 0_u64;
        let mut retained = VecDeque::with_capacity(self.packets.len());
        for mut packet in self.packets.drain(..) {
            let available = packet.bytes.len().saturating_sub(packet.head_offset_bytes) / block_align;
            if packet.playback_key == key {
                let take = (available as u64).min(remaining) as usize;
                remaining = remaining.saturating_sub(take as u64);
                if take == 0 { continue; }
                packet.bytes.truncate(packet.head_offset_bytes + take * block_align);
                queued = queued.saturating_add(take as u64);
            } else {
                queued = queued.saturating_add(available as u64);
            }
            retained.push_back(packet);
        }
        self.packets = retained;
        self.queued_frames = queued;
    }

    fn remove_key_packets(&mut self, key: &str, block_align: usize) {
        self.trim_key_frames(key, 0, block_align);
    }

    fn queued_frames_for_key(&self, key: &str, block_align: usize) -> u64 {
        self.packets.iter().filter(|packet| packet.playback_key == key)
            .map(|packet| packet.bytes.len().saturating_sub(packet.head_offset_bytes) / block_align)
            .map(|frames| frames as u64)
            .sum()
    }

    fn remember_closed_key(&mut self, key: &str) {
        if self.closed_keys.insert(key.to_owned()) {
            self.closed_key_order.push_back(key.to_owned());
            while self.closed_key_order.len() > 256 {
                if let Some(old) = self.closed_key_order.pop_front() { self.closed_keys.remove(&old); }
            }
        }
    }

    pub fn queued_frames(&self) -> u64 {
        self.queued_frames
    }

    pub fn accepted_frames(&self) -> u64 {
        self.accepted_frames
    }
}

/// Pcm16RenderConverter（PCM 播放转换器）：连续维护输入位置和重采样相位。
#[derive(Clone)]
pub struct Pcm16RenderConverter {
    output: CaptureFormat,         // output（Windows 默认混音格式）。
    source_rate: Option<u32>,      // source_rate（当前输入流采样率）。
    source_frames: u64,            // source_frames（连续输入样本总位置）。
    next_output_position: f64,     // next_output_position（下一输出采样对应输入位置）。
    output_step: f64,              // output_step（相邻输出采样的输入位置间隔）。
    previous: Option<f32>,         // previous（上一滤波输入采样）。
    filter: Option<RenderLowPass>, // filter（连续低通状态）。
}

impl Pcm16RenderConverter {
    pub fn new(output: CaptureFormat) -> Result<Self, String> {
        Ok(Self {
            output: output.validate()?,
            source_rate: None,
            source_frames: 0,
            next_output_position: 0.0,
            output_step: 1.0,
            previous: None,
            filter: None,
        })
    }

    /// convert_chunk（转换数据块）：跨调用维持重采样相位，速率切换前结清旧尾帧。
    pub fn convert_chunk(&mut self, source_rate: u32, samples: &[i16]) -> Result<Vec<u8>, String> {
        self.validate_chunk(source_rate, samples)?;
        let mut bytes = Vec::new();
        if self.source_rate != Some(source_rate) {
            if self.source_rate.is_some() {
                bytes.extend(self.finish()?);
            }
            self.reset_for_rate(source_rate);
        }
        let estimated_frames = (samples.len() as u64)
            .saturating_mul(self.output.sample_rate as u64)
            .checked_div(source_rate as u64)
            .unwrap_or(0)
            .saturating_add(2) as usize;
        let capacity = estimated_frames
            .checked_mul(self.output.block_align as usize)
            .ok_or("PCM 播放输出长度溢出")?;
        bytes
            .try_reserve_exact(capacity)
            .map_err(|_| "PCM 播放输出缓冲分配失败")?;
        let channels = self.output.channels as usize;
        let bytes_per_sample = self.output.block_align as usize / channels;
        for source in samples {
            let absolute_position = self.source_frames as f64;
            let sample = self
                .filter
                .as_mut()
                .map(|filter| filter.process(*source as f32 / 32768.0))
                .unwrap_or(*source as f32 / 32768.0);
            if let Some(previous) = self.previous {
                let lower = (absolute_position - 1.0).max(0.0);
                while self.next_output_position <= absolute_position + 1e-9 {
                    let fraction = (self.next_output_position - lower).clamp(0.0, 1.0) as f32;
                    let output = previous + (sample - previous) * fraction;
                    self.append_output(output, &mut bytes, bytes_per_sample);
                    self.next_output_position += self.output_step;
                }
            } else {
                self.next_output_position = absolute_position;
                self.append_output(sample, &mut bytes, bytes_per_sample);
                self.next_output_position += self.output_step;
            }
            self.previous = Some(sample);
            self.source_frames = self
                .source_frames
                .checked_add(1)
                .ok_or("PCM 播放输入采样位置溢出")?;
        }
        Ok(bytes)
    }

    /// finish（结清尾帧）：流结束时按最后可信采样封住小于一帧的输出尾部。
    pub fn finish(&mut self) -> Result<Vec<u8>, String> {
        let mut bytes = Vec::new();
        let Some(last) = self.previous else {
            return Ok(bytes);
        };
        let output = self.output;
        let channels = output.channels as usize;
        let bytes_per_sample = output.block_align as usize / channels;
        while self.next_output_position < self.source_frames as f64 - 1e-9 {
            self.append_output(last, &mut bytes, bytes_per_sample);
            self.next_output_position += self.output_step;
        }
        Ok(bytes)
    }

    fn validate_chunk(&self, source_rate: u32, samples: &[i16]) -> Result<(), String> {
        if !matches!(source_rate, 16_000 | 24_000 | 32_000 | 48_000)
            || samples.is_empty()
            || samples.len() as u64 > source_rate as u64 * MAX_PACKET_MS / 1_000
        {
            return Err("PCM 播放采样率或数据包时长无效".into());
        }
        Ok(())
    }

    fn reset_for_rate(&mut self, source_rate: u32) {
        self.source_rate = Some(source_rate);
        self.source_frames = 0;
        self.next_output_position = 0.0;
        self.output_step = source_rate as f64 / self.output.sample_rate as f64;
        self.previous = None;
        self.filter = RenderLowPass::new(source_rate, self.output.sample_rate);
    }

    fn append_output(&self, sample: f32, bytes: &mut Vec<u8>, width: usize) {
        for _ in 0..self.output.channels {
            encode_sample(sample, self.output.encoding, bytes, width);
        }
    }
}

#[derive(Clone)]
struct RenderLowPass {
    b0: f64,
    b1: f64,
    b2: f64,
    a1: f64,
    a2: f64,
    x1: f64,
    x2: f64,
    y1: f64,
    y2: f64,
}

impl RenderLowPass {
    fn new(source_rate: u32, output_rate: u32) -> Option<Self> {
        if source_rate <= output_rate {
            return None;
        }
        let cutoff = (output_rate as f64 * 0.45).min(source_rate as f64 * 0.45);
        let omega = 2.0 * std::f64::consts::PI * cutoff / source_rate as f64;
        let cosine = omega.cos();
        let alpha = omega.sin() / 2.0_f64.sqrt();
        let a0 = 1.0 + alpha;
        let b0 = ((1.0 - cosine) / 2.0) / a0;
        Some(Self {
            b0,
            b1: (1.0 - cosine) / a0,
            b2: b0,
            a1: (-2.0 * cosine) / a0,
            a2: (1.0 - alpha) / a0,
            x1: 0.0,
            x2: 0.0,
            y1: 0.0,
            y2: 0.0,
        })
    }

    fn process(&mut self, sample: f32) -> f32 {
        let x = sample as f64;
        let y = self.b0 * x + self.b1 * self.x1 + self.b2 * self.x2
            - self.a1 * self.y1
            - self.a2 * self.y2;
        self.x2 = self.x1;
        self.x1 = x;
        self.y2 = self.y1;
        self.y1 = y;
        y as f32
    }
}

/// convert_pcm16_mono（重采样并编码）：短独立样本序列的便利转换入口。
pub fn convert_pcm16_mono(
    samples: &[i16],
    source_rate: u32,
    output: CaptureFormat,
) -> Result<Vec<u8>, String> {
    let mut converter = Pcm16RenderConverter::new(output)?;
    let mut bytes = converter.convert_chunk(source_rate, samples)?;
    bytes.extend(converter.finish()?);
    Ok(bytes)
}

fn encode_sample(sample: f32, encoding: SampleEncoding, bytes: &mut Vec<u8>, width: usize) {
    let sample = sample.clamp(-1.0, 1.0);
    match encoding {
        SampleEncoding::PcmUnsigned8 => {
            let value = (sample * 128.0 + 128.0).round().clamp(0.0, 255.0) as u8;
            bytes.push(value);
        }
        SampleEncoding::PcmSigned16 => {
            let value = (sample * 32768.0).round().clamp(-32768.0, 32767.0) as i16;
            bytes.extend_from_slice(&value.to_le_bytes());
        }
        SampleEncoding::PcmSigned24 => {
            let value = ((sample * 8_388_608.0)
                .round()
                .clamp(-8_388_608.0, 8_388_607.0) as i32)
                .to_le_bytes();
            bytes.extend_from_slice(&value[..3]);
        }
        SampleEncoding::PcmSigned32 => {
            let value = (sample as f64 * 2_147_483_648.0)
                .round()
                .clamp(-2_147_483_648.0, 2_147_483_647.0) as i32;
            bytes.extend_from_slice(&value.to_le_bytes());
        }
        SampleEncoding::Float32 => bytes.extend_from_slice(&sample.to_le_bytes()),
    }
    debug_assert_eq!(bytes.len() % width, 0);
}

fn scale_encoded_frame(frame: &mut [u8], format: CaptureFormat, gain: f64) {
    if gain >= 1.0 { return; }
    let channels = format.channels as usize;
    let width = format.block_align as usize / channels;
    let gain = gain.clamp(0.0, 1.0);
    for sample in frame.chunks_exact_mut(width) {
        match format.encoding {
            SampleEncoding::PcmUnsigned8 => {
                let centered = sample[0] as f64 - 128.0;
                sample[0] = (centered.mul_add(gain, 128.0).round().clamp(0.0, 255.0)) as u8;
            }
            SampleEncoding::PcmSigned16 => {
                let value = i16::from_le_bytes([sample[0], sample[1]]) as f64;
                let scaled = value.mul_add(gain, 0.0).round().clamp(-32768.0, 32767.0) as i16;
                sample.copy_from_slice(&scaled.to_le_bytes());
            }
            SampleEncoding::PcmSigned24 => {
                let raw = i32::from(sample[0]) | (i32::from(sample[1]) << 8) | (i32::from(sample[2]) << 16);
                let signed = if raw & 0x80_0000 != 0 { raw | !0xff_ffff } else { raw };
                let scaled = (signed as f64 * gain).round().clamp(-8_388_608.0, 8_388_607.0) as i32;
                let bytes = scaled.to_le_bytes();
                sample.copy_from_slice(&bytes[..3]);
            }
            SampleEncoding::PcmSigned32 => {
                let value = i32::from_le_bytes(sample.try_into().expect("validated 32-bit sample")) as f64;
                let scaled = (value * gain).round().clamp(-2_147_483_648.0, 2_147_483_647.0) as i32;
                sample.copy_from_slice(&scaled.to_le_bytes());
            }
            SampleEncoding::Float32 => {
                let value = f32::from_le_bytes(sample.try_into().expect("validated float sample"));
                sample.copy_from_slice(&(value * gain as f32).to_le_bytes());
            }
        }
    }
}

fn silence_byte(encoding: SampleEncoding) -> u8 {
    match encoding {
        SampleEncoding::PcmUnsigned8 => 128,
        _ => 0,
    }
}

#[cfg(windows)]
mod windows_render {
    use super::*;
    use std::{
        ffi::c_void,
        ptr,
        sync::{
            atomic::{AtomicBool, AtomicU64, Ordering},
            mpsc, Arc, Mutex,
        },
        thread::JoinHandle,
        time::Duration,
    };
    use windows::Win32::{
        Foundation::HANDLE as ComHandle,
        Media::Audio::{
            eConsole, eRender, IAudioClient, IAudioClock, IAudioRenderClient, IMMDeviceEnumerator,
            MMDeviceEnumerator, AUDCLNT_BUFFERFLAGS_SILENT, AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
        },
        System::Com::{
            CoCreateInstance, CoInitializeEx, CoTaskMemFree, CoUninitialize, CLSCTX_ALL,
            COINIT_MULTITHREADED,
        },
    };
    use windows_sys::Win32::{
        Foundation::{CloseHandle, HANDLE, WAIT_OBJECT_0, WAIT_TIMEOUT},
        System::Threading::{CreateEventW, WaitForSingleObject},
    };

    struct ComApartment;
    impl Drop for ComApartment {
        fn drop(&mut self) {
            unsafe { CoUninitialize() };
        }
    }

    struct AudioEvent(HANDLE);
    impl Drop for AudioEvent {
        fn drop(&mut self) {
            unsafe { CloseHandle(self.0) };
        }
    }

    pub struct WasapiRenderStream {
        stop: Arc<AtomicBool>,
        control_sender: mpsc::SyncSender<RenderControl>, // control_sender（同线程执行的暂停/恢复请求）。
        paused: Arc<AtomicBool>, // paused（设备线程确认的暂停状态）。
        control_unknown: AtomicBool, // control_unknown（控制回执超时后的未知状态保护）。
        queue: Arc<Mutex<Pcm16RenderQueue>>,
        output_format: CaptureFormat,
        converter: Mutex<Pcm16RenderConverter>,
        snapshot: Arc<Mutex<PlaybackSnapshot>>,
        total_enqueued_frames: Arc<AtomicU64>,
        terminal_error: Arc<Mutex<Option<String>>>,
        thread: Option<JoinHandle<()>>,
    }

    enum RenderControl {
        Pause(mpsc::SyncSender<Result<(), String>>),
        Resume(mpsc::SyncSender<Result<(), String>>),
    }

    impl WasapiRenderStream {
        /// start_default（启动默认播放）：显式调用时打开系统默认扬声器。
        pub fn start_default() -> Result<Self, String> {
            let stop = Arc::new(AtomicBool::new(false));
            let thread_stop = stop.clone();
            let queue = Arc::new(Mutex::new(Pcm16RenderQueue::default()));
            let thread_queue = queue.clone();
            let snapshot = Arc::new(Mutex::new(PlaybackSnapshot {
                enqueued_ms: 0,
                played_ms: 0,
                buffered_ms: 0,
                output_estimated_ms: 0,
                started: false,
                device_time_us: 0,
            }));
            let thread_snapshot = snapshot.clone();
            let total_enqueued_frames = Arc::new(AtomicU64::new(0));
            let thread_total_enqueued_frames = total_enqueued_frames.clone();
            let total_submitted_frames = Arc::new(AtomicU64::new(0));
            let thread_total_submitted_frames = total_submitted_frames.clone();
            let terminal_error = Arc::new(Mutex::new(None));
            let thread_error = terminal_error.clone();
            let (control_sender, control_receiver) = mpsc::sync_channel(1);
            let paused = Arc::new(AtomicBool::new(false));
            let thread_paused = paused.clone();
            let control_unknown = AtomicBool::new(false);
            let (ready_tx, ready_rx) = mpsc::sync_channel(1);
            let thread = std::thread::Builder::new()
                .name("wasapi-render".into())
                .spawn(move || {
                    let result = run_render(
                        thread_stop,
                        thread_queue,
                        thread_snapshot,
                        thread_total_enqueued_frames,
                        thread_total_submitted_frames,
                        control_receiver,
                        thread_paused,
                        &ready_tx,
                    );
                    if let Err(error) = result {
                        if let Ok(mut terminal) = thread_error.lock() {
                            *terminal = Some(error.clone());
                        }
                        let _ = ready_tx.send(Err(error));
                    }
                })
                .map_err(|error| format!("无法启动 WASAPI 播放线程：{error}"))?;
            match ready_rx.recv_timeout(Duration::from_secs(10)) {
                Ok(Ok(output_format)) => {
                    let converter = match Pcm16RenderConverter::new(output_format) {
                        Ok(converter) => converter,
                        Err(error) => {
                            stop.store(true, Ordering::Release);
                            let _ = thread.join();
                            return Err(error);
                        }
                    };
                    Ok(Self {
                        stop,
                        control_sender,
                        paused,
                        control_unknown,
                        queue,
                        output_format,
                        converter: Mutex::new(converter),
                        snapshot,
                        total_enqueued_frames,
                        terminal_error,
                        thread: Some(thread),
                    })
                }
                Ok(Err(error)) => {
                    let _ = thread.join();
                    Err(error)
                }
                Err(error) => {
                    stop.store(true, Ordering::Release);
                    drop(thread);
                    Err(format!("WASAPI 默认播放启动超时：{error}"))
                }
            }
        }

        /// enqueue_pcm16（入队 PCM）：接受单声道帧并转换为系统混音格式。
        pub fn enqueue_pcm16(&self, sample_rate: u32, samples: &[i16]) -> Result<u64, String> {
            self.enqueue_pcm16_for_key("local-playback", sample_rate, samples)
        }

        /// enqueue_pcm16_for_key（按播放事务键入队）：远端替换淡出淡入依靠稳定键分界。
        pub fn enqueue_pcm16_for_key(
            &self,
            playback_key: &str,
            sample_rate: u32,
            samples: &[i16],
        ) -> Result<u64, String> {
            if self.stop.load(Ordering::Acquire) {
                return Err("WASAPI 播放流正在停止".into());
            }
            self.check_error()?;
            let mut converter = self.converter.lock().map_err(|_| "播放转换器状态不可用")?;
            let previous = converter.clone();
            let encoded = converter.convert_chunk(sample_rate, samples)?;
            if encoded.is_empty() {
                return Ok(self
                    .total_enqueued_frames
                    .load(Ordering::Acquire)
                    .saturating_mul(1_000)
                    / self.output_format.sample_rate as u64);
            }
            let frames = encoded.len() / self.output_format.block_align as usize;
            let maximum = self.output_format.sample_rate as u64 * MAX_PLAYBACK_QUEUE_MS / 1_000;
            let snapshot = self.snapshot()?;
            let mut queue = match self.queue.lock() {
                Ok(queue) => queue,
                Err(_) => {
                    *converter = previous;
                    return Err("播放队列状态不可用".into());
                }
            };
            if let Err(error) = queue.enqueue_for_key(
                playback_key,
                encoded,
                frames,
                self.output_format.block_align as usize,
                maximum,
                self.output_format.sample_rate,
                snapshot.output_estimated_ms,
                snapshot.buffered_ms,
                snapshot.device_time_us,
            ) {
                *converter = previous;
                return Err(error);
            }
            let total = self
                .total_enqueued_frames
                .fetch_add(frames as u64, Ordering::AcqRel)
                .saturating_add(frames as u64);
            Ok(total.saturating_mul(1_000) / self.output_format.sample_rate as u64)
        }

        /// snapshot（读取播放快照）：仅读取线程缓存，不同步查询 COM 或设备。
        pub fn snapshot(&self) -> Result<PlaybackSnapshot, String> {
            self.check_terminal_error()?;
            self.snapshot
                .lock()
                .map(|snapshot| *snapshot)
                .map_err(|_| "播放快照状态不可用".into())
        }

        /// pause（暂停）：在 WASAPI 所属线程停止端点时钟并等待确认。
        pub fn pause(&self) -> Result<(), String> {
            self.set_paused(true)
        }

        /// resume（恢复）：在 WASAPI 所属线程从冻结的端点位置恢复播放。
        pub fn resume(&self) -> Result<(), String> {
            self.set_paused(false)
        }

        /// prepare_replacement（受理替换）：冻结旧键追加并保留有界设备端淡出尾音。
        pub fn prepare_replacement(
            &self,
            replacement_id: &str,
            old_playback_key: &str,
            max_ms: u32,
        ) -> Result<PlaybackReplacementProgress, String> {
            self.check_error()?;
            let snapshot = self.snapshot()?;
            let mut queue = self.queue.lock().map_err(|_| "播放队列状态不可用")?;
            queue.prepare_replacement(
                replacement_id, old_playback_key, max_ms,
                self.output_format.sample_rate, self.output_format.block_align as usize,
                snapshot.buffered_ms, snapshot.played_ms,
                snapshot.output_estimated_ms, snapshot.device_time_us,
            )
        }

        /// bind_replacement（绑定固定后继）：后继 PCM 到来后旧音按 30 毫秒提前收尾。
        pub fn bind_replacement(&self, replacement_id: &str, playback_key: &str) -> Result<bool, String> {
            self.check_error()?;
            self.queue.lock().map_err(|_| "播放队列状态不可用")?
                .bind_replacement(replacement_id, playback_key)
        }

        /// abort_replacement（撤销替换）：丢弃旧淡出尾音，保留设备时钟结算状态。
        pub fn abort_replacement(
            &self, replacement_id: &str, reason: &str,
        ) -> Result<Option<PlaybackReplacementProgress>, String> {
            self.check_terminal_error()?;
            let snapshot = self.snapshot()?;
            self.queue.lock().map_err(|_| "播放队列状态不可用")?
                .abort_replacement(replacement_id, reason, snapshot.device_time_us,
                    snapshot.output_estimated_ms, self.output_format.block_align as usize)
        }

        pub fn replacement_status(&self, replacement_id: &str) -> Result<Option<PlaybackReplacementProgress>, String> {
            self.check_terminal_error()?;
            Ok(self.queue.lock().map_err(|_| "播放队列状态不可用")?
                .replacement_status(replacement_id))
        }

        pub fn is_paused(&self) -> bool {
            self.paused.load(Ordering::Acquire)
        }

        /// ensure_healthy（检查端点健康）：默认设备失效后禁止沿用旧播放事务。
        pub fn ensure_healthy(&self) -> Result<(), String> {
            self.check_terminal_error()?;
            if self.control_unknown.load(Ordering::Acquire) {
                return Err("WASAPI 暂停/恢复结果未知；旧播放事务不能继续".into());
            }
            if self.thread.as_ref().is_some_and(|thread| !thread.is_finished()) {
                Ok(())
            } else {
                Err("WASAPI 播放线程已结束；旧播放结果保持未知".into())
            }
        }

        fn set_paused(&self, requested: bool) -> Result<(), String> {
            self.check_error()?;
            if self.paused.load(Ordering::Acquire) == requested {
                return Ok(());
            }
            let (reply_sender, reply_receiver) = mpsc::sync_channel(1);
            let command = if requested {
                RenderControl::Pause(reply_sender)
            } else {
                RenderControl::Resume(reply_sender)
            };
            if self.control_sender.send(command).is_err() {
                self.control_unknown.store(true, Ordering::Release);
                return Err("WASAPI 播放控制线程已退出；暂停/恢复结果未知".into());
            }
            match reply_receiver.recv_timeout(Duration::from_secs(2)) {
                Ok(Ok(())) => {
                    self.paused.store(requested, Ordering::Release);
                    Ok(())
                }
                Ok(Err(error)) => {
                    self.control_unknown.store(true, Ordering::Release);
                    Err(error)
                }
                Err(error) => {
                    self.control_unknown.store(true, Ordering::Release);
                    Err(format!("WASAPI 暂停/恢复回执未知：{error}"))
                }
            }
        }

        fn check_error(&self) -> Result<(), String> {
            if self.control_unknown.load(Ordering::Acquire) {
                return Err("WASAPI 暂停/恢复结果未知，播放状态已隔离".into());
            }
            self.check_terminal_error()
        }

        fn check_terminal_error(&self) -> Result<(), String> {
            match self.terminal_error.lock() {
                Ok(error) => match error.as_ref() {
                    Some(error) => Err(error.clone()),
                    None => Ok(()),
                },
                Err(_) => Err("WASAPI 播放错误状态不可用".into()),
            }
        }

        /// stop（停止播放）：停止端点时钟并释放默认输出端点。
        pub fn stop(&mut self) -> Result<(), String> {
            self.stop.store(true, Ordering::Release);
            if let Some(thread) = self.thread.take() {
                if thread.join().is_err() {
                    return Err("WASAPI 播放线程异常退出".into());
                }
            }
            self.check_terminal_error()
        }
    }

    impl Drop for WasapiRenderStream {
        fn drop(&mut self) {
            let _ = self.stop();
        }
    }

    struct Session {
        enumerator: IMMDeviceEnumerator, // enumerator（用于检查系统默认播放端点的枚举器）。
        client: IAudioClient,
        renderer: IAudioRenderClient,
        clock: IAudioClock,
        event: AudioEvent,
        format: CaptureFormat,
        buffer_frames: u32,
        clock_frequency: u64,
        endpoint_id: String, // endpoint_id（当前播放流绑定的 Windows 端点编号）。
    }

    fn run_render(
        stop: Arc<AtomicBool>,
        queue: Arc<Mutex<Pcm16RenderQueue>>,
        snapshot: Arc<Mutex<PlaybackSnapshot>>,
        total_enqueued_frames: Arc<AtomicU64>,
        total_submitted_frames: Arc<AtomicU64>,
        control_receiver: mpsc::Receiver<RenderControl>,
        paused: Arc<AtomicBool>,
        ready: &mpsc::SyncSender<Result<CaptureFormat, String>>,
    ) -> Result<(), String> {
        unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) }
            .ok()
            .map_err(|error| format!("WASAPI COM 初始化失败：{error}"))?;
        let _apartment = ComApartment;
        let mut session = match Session::open(&stop) {
            Ok(session) => session,
            Err(error) => {
                let _ = ready.send(Err(error.clone()));
                return Err(error);
            }
        };
        if ready.send(Ok(session.format)).is_err() {
            let _ = unsafe { session.client.Stop() };
            return Err("播放启动等待方已退出".into());
        }
        let mut result = Ok(());
        let mut paused_on_audio_thread = false;
        let mut control_faulted = false;
        let mut next_endpoint_check = std::time::Instant::now() + Duration::from_millis(500);
        while !stop.load(Ordering::Acquire) {
            loop {
                let command = match control_receiver.try_recv() {
                    Ok(command) => command,
                    Err(mpsc::TryRecvError::Empty | mpsc::TryRecvError::Disconnected) => break,
                };
                let (requested, reply) = match command {
                    RenderControl::Pause(reply) => (true, reply),
                    RenderControl::Resume(reply) => (false, reply),
                };
                let transition = if paused_on_audio_thread == requested {
                    Ok(())
                } else if requested {
                    unsafe { session.client.Stop() }
                        .map_err(|error| format!("暂停 WASAPI 播放失败：{error}"))
                } else {
                    unsafe { session.client.Start() }
                        .map_err(|error| format!("恢复 WASAPI 播放失败：{error}"))
                };
                if transition.is_ok() {
                    paused_on_audio_thread = requested;
                    paused.store(requested, Ordering::Release);
                }
                if let Err(error) = transition {
                    if unsafe { session.client.Stop() }.is_ok() {
                        paused_on_audio_thread = true;
                        paused.store(true, Ordering::Release);
                    }
                    let _ = reply.send(Err(error));
                    control_faulted = true;
                    break;
                }
                let _ = reply.send(Ok(()));
            }
            if control_faulted {
                std::thread::sleep(Duration::from_millis(20));
                continue;
            }
            if std::time::Instant::now() >= next_endpoint_check {
                next_endpoint_check = std::time::Instant::now() + Duration::from_millis(500);
                match session.default_endpoint_id() {
                    Ok(endpoint_id) if endpoint_id == session.endpoint_id => {}
                    Ok(_) => {
                        result = Err("默认扬声器已更换；当前播放结果未知且不会重放".into());
                        break;
                    }
                    Err(error) => {
                        result = Err(format!("默认扬声器状态不可确认；当前播放结果未知：{error}"));
                        break;
                    }
                }
            }
            if result.is_err() {
                break;
            }
            if paused_on_audio_thread {
                std::thread::sleep(Duration::from_millis(20));
                continue;
            }
            let wait = unsafe { WaitForSingleObject(session.event.0, 50) };
            if wait == WAIT_TIMEOUT {
                continue;
            }
            if wait != WAIT_OBJECT_0 {
                result = Err("等待 WASAPI 播放事件失败".into());
                break;
            }
            if let Err(error) = render_available(
                &mut session,
                &queue,
                &snapshot,
                &total_enqueued_frames,
                &total_submitted_frames,
            ) {
                result = Err(error);
                break;
            }
        }
        let stopped = unsafe { session.client.Stop() }
            .map_err(|error| crate::device_audio::format_wasapi_error("停止 WASAPI 播放流失败", error));
        result?;
        stopped?;
        Ok(())
    }

    impl Session {
        fn open(stop: &AtomicBool) -> Result<Self, String> {
            let enumerator: IMMDeviceEnumerator =
                unsafe { CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL) }
                    .map_err(|error| format!("WASAPI 设备枚举器不可用：{error}"))?;
            let device = unsafe { enumerator.GetDefaultAudioEndpoint(eRender, eConsole) }
                .map_err(|error| format!("系统默认扬声器不可用：{error}"))?;
            let endpoint_id = crate::device_audio::endpoint_id_from_device(&device)?;
            let client: IAudioClient = unsafe { device.Activate(CLSCTX_ALL, None) }
                .map_err(|error| format!("打开默认扬声器失败：{error}"))?;
            let format_ptr = unsafe { client.GetMixFormat() }
                .map_err(|error| format!("读取默认扬声器混音格式失败：{error}"))?;
            if format_ptr.is_null() {
                return Err("WASAPI 返回空扬声器格式".into());
            }
            let format = match crate::wasapi_capture::parse_mix_format(format_ptr) {
                Ok(format) => format,
                Err(error) => {
                    unsafe { CoTaskMemFree(Some(format_ptr.cast::<c_void>())) };
                    return Err(error);
                }
            };
            if stop.load(Ordering::Acquire) {
                unsafe { CoTaskMemFree(Some(format_ptr.cast::<c_void>())) };
                return Err("WASAPI 播放启动已取消".into());
            }
            let initialized = unsafe {
                client.Initialize(
                    AUDCLNT_SHAREMODE_SHARED,
                    AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
                    0,
                    0,
                    format_ptr,
                    None,
                )
            };
            unsafe { CoTaskMemFree(Some(format_ptr.cast::<c_void>())) };
            initialized.map_err(|error| format!("初始化共享模式播放流失败：{error}"))?;
            let event = unsafe { CreateEventW(ptr::null(), 0, 0, ptr::null()) };
            if event.is_null() {
                return Err("无法创建 WASAPI 播放事件".into());
            }
            let event = AudioEvent(event);
            unsafe { client.SetEventHandle(ComHandle(event.0)) }
                .map_err(|error| format!("注册 WASAPI 播放事件失败：{error}"))?;
            let renderer: IAudioRenderClient = unsafe { client.GetService() }
                .map_err(|error| format!("获取 WASAPI 输出缓冲失败：{error}"))?;
            let clock: IAudioClock = unsafe { client.GetService() }
                .map_err(|error| format!("获取 WASAPI 设备时钟失败：{error}"))?;
            let buffer_frames = unsafe { client.GetBufferSize() }
                .map_err(|error| format!("读取 WASAPI 输出缓冲容量失败：{error}"))?;
            let clock_frequency = unsafe { clock.GetFrequency() }
                .map_err(|error| format!("读取 WASAPI 设备时钟频率失败：{error}"))?;
            if clock_frequency == 0 || buffer_frames == 0 {
                return Err("WASAPI 输出缓冲或设备时钟频率无效".into());
            }
            if stop.load(Ordering::Acquire) {
                return Err("WASAPI 播放启动已取消".into());
            }
            let mut session = Self {
                enumerator,
                client,
                renderer,
                clock,
                event,
                format,
                buffer_frames,
                clock_frequency,
                endpoint_id,
            };
            prime_silence(&mut session)?;
            unsafe { session.client.Start() }
                .map_err(|error| crate::device_audio::format_wasapi_error("启动 WASAPI 播放流失败", error))?;
            Ok(session)
        }

        fn default_endpoint_id(&self) -> Result<String, String> {
            let device = unsafe {
                self.enumerator.GetDefaultAudioEndpoint(eRender, eConsole)
            }.map_err(|error| format!("读取当前默认扬声器失败：{error}"))?;
            crate::device_audio::endpoint_id_from_device(&device)
        }
    }

    fn prime_silence(session: &mut Session) -> Result<(), String> {
        let data = unsafe { session.renderer.GetBuffer(session.buffer_frames) }
            .map_err(|error| format!("预备 WASAPI 输出缓冲失败：{error}"))?;
        if data.is_null() {
            let _ = unsafe {
                session
                    .renderer
                    .ReleaseBuffer(session.buffer_frames, AUDCLNT_BUFFERFLAGS_SILENT.0 as u32)
            };
            return Err("WASAPI 输出缓冲指针为空".into());
        }
        let length = session.buffer_frames as usize * session.format.block_align as usize;
        let bytes = unsafe { std::slice::from_raw_parts_mut(data, length) };
        bytes.fill(silence_byte(session.format.encoding));
        unsafe { session.renderer.ReleaseBuffer(session.buffer_frames, 0) }
            .map_err(|error| format!("提交 WASAPI 初始静音缓冲失败：{error}"))
    }

    fn render_available(
        session: &mut Session,
        queue: &Mutex<Pcm16RenderQueue>,
        snapshot: &Mutex<PlaybackSnapshot>,
        total_enqueued_frames: &AtomicU64,
        total_submitted_frames: &AtomicU64,
    ) -> Result<(), String> {
        let padding = unsafe { session.client.GetCurrentPadding() }
            .map_err(|error| crate::device_audio::format_wasapi_error("读取 WASAPI 输出储备失败", error))?;
        let available = session.buffer_frames.saturating_sub(padding);
        if available > 0 {
            let data = unsafe { session.renderer.GetBuffer(available) }
                .map_err(|error| crate::device_audio::format_wasapi_error("取得 WASAPI 输出缓冲失败", error))?;
            if data.is_null() {
                let _ = unsafe {
                    session
                        .renderer
                        .ReleaseBuffer(available, AUDCLNT_BUFFERFLAGS_SILENT.0 as u32)
                };
                return Err("WASAPI 输出缓冲指针为空".into());
            }
            let length = available as usize * session.format.block_align as usize;
            let output = unsafe { std::slice::from_raw_parts_mut(data, length) };
            output.fill(silence_byte(session.format.encoding));
            let device_time_us = snapshot.lock().map(|value| value.device_time_us).unwrap_or(0);
            let fill_result = queue
                .lock()
                .map_err(|_| "播放队列状态不可用".to_owned())
                .and_then(|mut queue| {
                    queue.fill_for_device(output, available as usize, session.format, device_time_us)
                });
            let released = unsafe { session.renderer.ReleaseBuffer(available, 0) }
                .map_err(|error| crate::device_audio::format_wasapi_error("提交 WASAPI 输出缓冲失败", error));
            let filled_frames = fill_result?;
            released?;
            total_submitted_frames.fetch_add(filled_frames as u64, Ordering::AcqRel);
        }
        let mut device_position = 0_u64;
        let mut qpc_position = 0_u64;
        unsafe {
            session
                .clock
                .GetPosition(&mut device_position, Some(&mut qpc_position))
        }
        .map_err(|error| crate::device_audio::format_wasapi_error("读取 WASAPI 设备时钟失败", error))?;
        let output_rate = session.format.sample_rate as u64;
        let played_ms = total_submitted_frames.load(Ordering::Acquire) * 1_000 / output_rate;
        let output_estimated_ms = (device_position as u128 * 1_000
            / session.clock_frequency as u128)
            .min(u64::MAX as u128) as u64;
        let queued_frames = {
            let mut queue = queue.lock().map_err(|_| "播放队列状态不可用")?;
            queue.update_output_progress(output_estimated_ms, qpc_position / 10);
            queue.queued_frames()
        };
        let buffered_ms = (padding as u64 + queued_frames) * 1_000 / output_rate;
        let enqueued_ms = total_enqueued_frames.load(Ordering::Acquire) * 1_000 / output_rate;
        let next = PlaybackSnapshot {
            enqueued_ms,
            played_ms,
            buffered_ms,
            output_estimated_ms,
            started: output_estimated_ms > 0,
            device_time_us: qpc_position / 10,
        };
        *snapshot.lock().map_err(|_| "播放快照状态不可用")? = next;
        Ok(())
    }

}

#[cfg(windows)]
pub use windows_render::WasapiRenderStream;

#[cfg(test)]
mod tests {
    use super::*;

    fn format(rate: u32, channels: u16, encoding: SampleEncoding) -> CaptureFormat {
        let bytes = match encoding {
            SampleEncoding::PcmUnsigned8 => 1,
            SampleEncoding::PcmSigned16 => 2,
            SampleEncoding::PcmSigned24 => 3,
            SampleEncoding::PcmSigned32 | SampleEncoding::Float32 => 4,
        };
        CaptureFormat {
            sample_rate: rate,
            channels,
            block_align: (bytes * channels as usize) as u16,
            encoding,
        }
    }

    #[test]
    fn converts_twenty_millisecond_pcm_to_mix_rate_and_duplicates_mono_safely() {
        let samples = vec![16_000; 320];
        let stereo_float =
            convert_pcm16_mono(&samples, 16_000, format(44_100, 2, SampleEncoding::Float32))
                .unwrap();
        assert_eq!(stereo_float.len(), 882 * 2 * 4);
        let left = f32::from_le_bytes(stereo_float[0..4].try_into().unwrap());
        let right = f32::from_le_bytes(stereo_float[4..8].try_into().unwrap());
        assert!((left - 16_000.0 / 32_768.0).abs() < 0.001);
        assert_eq!(left, right);
        let output_format = format(44_100, 2, SampleEncoding::Float32);
        let mut whole = Pcm16RenderConverter::new(output_format).unwrap();
        let mut expected = whole.convert_chunk(16_000, &samples).unwrap();
        expected.extend(whole.finish().unwrap());
        let mut split = Pcm16RenderConverter::new(output_format).unwrap();
        let mut actual = split.convert_chunk(16_000, &samples[..137]).unwrap();
        actual.extend(split.convert_chunk(16_000, &samples[137..]).unwrap());
        actual.extend(split.finish().unwrap());
        assert_eq!(actual, expected);
        assert_eq!(actual.len(), 882 * 2 * 4);
    }

    #[test]
    fn render_queue_is_bounded_and_fills_underrun_with_format_silence() {
        let mut queue = Pcm16RenderQueue::default();
        queue.enqueue(vec![1, 2, 3, 4], 2, 2, 4).unwrap();
        assert_eq!(queue.queued_frames(), 2);
        assert!(queue.enqueue(vec![5, 6, 7, 8, 9, 10], 3, 2, 4).is_err());
        let mut output = [0_u8; 8];
        assert_eq!(queue.fill(&mut output, 4, 2, 128).unwrap(), 2);
        assert_eq!(output, [1, 2, 3, 4, 128, 128, 128, 128]);
        assert_eq!(queue.queued_frames(), 0);
        assert_eq!(queue.accepted_frames(), 2);
    }

    #[test]
    fn replacement_queue_closes_old_key_fades_early_tail_and_fades_in_successor() {
        let format = format(1_000, 1, SampleEncoding::PcmSigned16);
        let mut queue = Pcm16RenderQueue::default();
        queue.enqueue_for_key(
            "speech-old", 10_000_i16.to_le_bytes().repeat(100),
            100, 2, 500, 1_000, 100, 100, 1_000_000,
        ).unwrap();
        let prepared = queue.prepare_replacement(
            "replacement-1", "speech-old", 500, 1_000, 2,
            100, 80, 100, 1_000_000,
        ).unwrap();
        assert_eq!(prepared.old_playback_key, "speech-old");
        assert!(!prepared.done);
        assert_eq!(queue.queued_frames_for_key("speech-old", 2), 100);
        assert!(queue.enqueue_for_key(
            "speech-old", vec![0; 2], 1, 2, 500, 1_000, 100, 100, 1_000_001,
        ).is_err());
        assert!(queue.bind_replacement("replacement-1", "speech-new").unwrap());
        queue.enqueue_for_key(
            "speech-new", 10_000_i16.to_le_bytes().repeat(60),
            60, 2, 500, 1_000, 100, 100, 1_001_000,
        ).unwrap();

        let mut output = vec![0_u8; 35 * 2];
        queue.fill_for_device(&mut output, 35, format, 1_002_000).unwrap();
        let samples: Vec<i16> = output.chunks_exact(2)
            .map(|chunk| i16::from_le_bytes([chunk[0], chunk[1]])).collect();
        assert!(samples[0] > 9_000);
        assert!(samples[29] < 1_000);
        assert_eq!(samples[30], 0);
        assert!(samples[34] > 0 && samples[34] < 2_000);
        assert_eq!(queue.queued_frames_for_key("speech-old", 2), 0);

        queue.update_output_progress(135, 1_035_000);
        let result = queue.replacement_status("replacement-1").unwrap();
        assert!(result.done);
        assert_eq!(result.reason.as_deref(), Some("early_handoff"));
        assert_eq!(result.finished_device_time_us, Some(1_035_000));
        assert_eq!(result.attenuated_range_ms, [80, 110]);
        assert!(result.events_device_time_us.contains_key("first_attenuated_audio"));
        assert!(result.events_device_time_us.contains_key("new_audio_first_output"));
    }

    #[test]
    fn aborting_replacement_discards_only_old_tail_and_keeps_successor_queue() {
        let mut queue = Pcm16RenderQueue::default();
        queue.enqueue_for_key("speech-old", vec![1; 200], 100, 2, 300, 1_000, 100, 100, 1_000)
            .unwrap();
        queue.prepare_replacement("replacement-2", "speech-old", 400, 1_000, 2,
            100, 50, 100, 1_000).unwrap();
        queue.bind_replacement("replacement-2", "speech-new").unwrap();
        queue.enqueue_for_key("speech-new", vec![2; 40], 20, 2, 300, 1_000, 100, 100, 1_010)
            .unwrap();
        let result = queue.abort_replacement("replacement-2", "stopped", 1_020, 110, 2).unwrap().unwrap();
        assert_eq!(result.finished_output_estimated_ms, Some(110));
        assert!(result.done);
        assert_eq!(result.reason.as_deref(), Some("stopped"));
        assert_eq!(queue.queued_frames_for_key("speech-old", 2), 0);
        assert_eq!(queue.queued_frames_for_key("speech-new", 2), 20);
    }

    #[test]
    fn retrying_the_same_replacement_id_returns_the_existing_ticket_without_mutation() {
        let mut queue = Pcm16RenderQueue::default();
        queue.enqueue_for_key("speech-old", vec![1; 200], 100, 2, 500, 1_000, 0, 100, 10_000).unwrap();
        let first = queue.prepare_replacement("replacement-repeat", "speech-old", 300, 1_000, 2,
            100, 20, 0, 10_100).unwrap();
        let queued_after_first = queue.queued_frames_for_key("speech-old", 2);
        let repeated = queue.prepare_replacement("replacement-repeat", "speech-old", 300, 1_000, 2,
            100, 20, 0, 10_200).unwrap();
        assert_eq!(repeated, first);
        assert_eq!(queue.queued_frames_for_key("speech-old", 2), queued_after_first);
        assert!(queue.enqueue_for_key("speech-old", vec![2; 2], 1, 2, 500, 1_000, 0, 100, 10_300).is_err());
    }

    #[test]
    fn completed_replacement_accepts_only_an_idempotent_successor_binding() {
        let mut queue = Pcm16RenderQueue::default();
        let progress = queue.prepare_replacement("replacement-completed", "speech-old", 300,
            1_000, 2, 0, 0, 0, 10_000).unwrap();
        assert!(progress.done);
        assert!(queue.bind_replacement("replacement-completed", "speech-new").unwrap());
        assert!(queue.bind_replacement("replacement-completed", "speech-new").unwrap());
        assert!(!queue.bind_replacement("replacement-completed", "speech-other").unwrap());
        queue.enqueue_for_key("speech-new", vec![2; 40], 20, 2, 500, 1_000, 0, 0, 10_100).unwrap();
        assert_eq!(queue.queued_frames_for_key("speech-new", 2), 20);
    }

    #[test]
    fn converter_rejects_unbounded_or_malformed_audio() {
        assert!(
            convert_pcm16_mono(&[], 16_000, format(48_000, 2, SampleEncoding::PcmSigned16))
                .is_err()
        );
        assert!(convert_pcm16_mono(
            &vec![0; 16_000 * 3],
            16_000,
            format(48_000, 2, SampleEncoding::PcmSigned16)
        )
        .is_err());
        assert!(convert_pcm16_mono(
            &vec![0; 320],
            22_050,
            format(48_000, 2, SampleEncoding::PcmSigned16)
        )
        .is_err());
    }
}
