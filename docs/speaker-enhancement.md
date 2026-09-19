# 可选声纹增强组件

声纹旁路是 Voice Terminal 的可选交互增强，不是现有音频链路的前置条件。音频经过现有
AEC/降噪后，同时送入 Paraformer 和本地 Speaker Worker；声纹异常、模型缺失或 Worker 断开
均只影响诊断/路由，不影响 VAD、ASR 和普通 Agent。

## 运行模式

| 模式 | 实时推理 | 正式语义路由 |
| --- | --- | --- |
| `off` | 不启动 | 原有链路直通 |
| `observe` | 启动并记录状态 | 所有 ASR final 直通，附带声纹诊断 |
| `filter` | 启动并记录状态 | 只拦截 `non_owner_confirmed`，其余全部直通 |

`filter` 的最终等待从 ASR final 可提交时开始，预算为 150 ms。超时、未知状态、服务不可用、
队列溢出、资料过期和格式错误均使用降级直通。被拦截文本只写入会话级有界
`AmbientTranscriptStore`，不进入主 `SessionLedger`、主 Agent、TTS 或普通对话历史。

## 特征前端（唯一权威）

`voice_terminal/speaker/features.py` 是 PCM → 80 维特征的唯一实现，逐值对齐 WeSpeaker 官方
`compute_fbank`：

```text
waveform × 2^15 → Kaldi fbank(80 mel, 25/10 ms, hamming, dither=0, use_energy=False)
                → 逐帧去直流 → 预加重 0.97 → 加窗 → 补零到 512 → 功率谱
                → Kaldi mel 滤波器组（mel 域三角、峰值 1、n_fft/2 个 bin 再补 1 列 0）
                → ln(max(·, float32 eps)) → CMN（逐 mel 维减时间均值，不做 CVN）
```

三点必须保持不变，否则分数尺度会整体改变：

1. **CMN 不可省略。** 它把录音增益这个常数整体减掉。省掉之后一次纯电平变化就能让本人分数
   漂移 0.2 以上，阈值随即失效。
2. **mel 滤波器组用 `n_fft/2` 个 bin 再补一列 0**（Kaldi 约定），不是 `n_fft+1`（librosa 约定）；
   三角形定义在 mel 域，不做面积归一化。
3. **分数是原始余弦**（与官方 `wespeakerruntime.compute_cosine_score` 一致，范围约 −1…1）。
   不要与 `(cos+1)/2` 这类映射尺度混用，也不要与 AS-Norm 分数混用。

模型侧契约（本机 ONNX 实测）：输入 `feats [B, T, 80]`，输出 `embs [B, 256]`，时间轴动态，
图内不含前端，输出未做 L2 归一化；**时间轴少于 9 帧（音频 < 105 ms）会静默返回 NaN**，
因此前端对过短音频直接抛 `InsufficientAudioError`。

`tests/test_speaker_features.py` 会与 `torchaudio.compliance.kaldi.fbank` 逐值比对，
并在有模型时比对 embedding 余弦（要求 > 0.9999）。改动前端后该测试必须仍然通过。

## 资料指纹（为什么换了前端就必须重录）

`config/speaker/registry.json` 记录 `feature_fingerprint`（前端版本 + 模型 ID），
`config/speaker/probe.npy` 记录一段确定性合成信号的 embedding。启动时会把当前进程的
probe embedding 与资料里的比对：

| 情况 | 结果 |
| --- | --- |
| 全部一致 | 正常使用 |
| 前端版本不同 | `stale`，原因 `feature_frontend_changed` |
| 模型 ID 不同 | `stale`，原因 `speaker_model_changed` |
| 版本相同但 probe 余弦 < 0.9999 | `stale`，原因 `feature_numeric_mismatch` |
| 旧 schema / 缺 probe | `stale`，原因 `profile_schema_upgraded` / `profile_missing_probe` |

`stale` 时 `available` 为 false，路由全部降级直通，设置页显示"需要重新注册声纹"。
**这是为了让"前端改了"这种静默故障不可能再发生**：旧模板仍在错误的坐标系里时，
症状表现为"所有人都被拒绝"，从分数上无法与"阈值太高"区分。

## 注册协议

三段**内容不同**的连续语句，每段 5–10 秒。为什么不是"念一句固定的话"：

* 在受控实验里（同一 TTS 音色、同一渲染器、同一通道，只有文本不同），用同一句注册时
  该句得分 +0.9554、其他文本 +0.8552，**同文本凭空高出约 0.10 余弦**。这是内容捷径，
  不是更强的说话人证据；而运行时要判的永远是自发语音，不可能与注册语句一致。
* 三段读同一句还会让三段嵌入高度相关，质心的有效样本量≈1，本仓库的自一致性门禁同时失效。

因此界面为每段显示一句**不同**的提示句，并明确禁止：反复念同一句、拖长音的持续元音、
耳语、刻意夹嗓。持续元音属于非连续语音，在本模型上明显更差（本机 `sustained-20s.wav`
在两条独立声纹管线上都与其余录音相距 0.7 以上余弦）。

**采集走服务端**（`POST /api/v1/speaker/enrollment/{id}/capture`），使用与运行期完全相同的
设备（`audio.input_device`）、16 kHz/单声道/int16 与同一个 `AudioPreprocessor` 实例。
此前注册走浏览器 `getUserMedia`：默认设备、WebRTC AEC/NS/AGC 全开、48 kHz→16 kHz 无抗混叠
线性抽取，与运行期不是同一条链路。浏览器上传接口保留为兜底（无采集设备时），并已改为
请求 16 kHz `AudioContext` 且关闭浏览器 DSP。

提交时的门禁（任一不过则拒绝并说明原因）：

| 检查 | 阈值 |
| --- | --- |
| 每段有效人声 | ≥ 3 s |
| 三段有效人声合计 | ≥ 15 s |
| 每段 VAD 语音占比（VAD 可用时） | ≥ 50% |
| 削波率 | ≤ 0.5% |
| 三段两两余弦最小值 | ≥ 0.70 |

通过后写入：逐段 L2 归一化后的 embedding、每段质量（`speech_ms`、`voiced_ratio`、`rms_dbfs`、
`cosine_to_centroid`）、质心与资料指纹。原始录音不落盘。

## 判定口径

每一轮只产生**一个**权威分数：

```text
窗口 2000 ms / 步进 500 ms，仅当窗口内 VAD 语音 ≥ min_speech_ms(700 ms)
  且语音占比 ≥ min_window_speech_ratio(0.40) 时才提取 embedding
→ turn_emb = normalize(Σ speech_msᵢ · normalize(embᵢ))
→ turn_score = cos(turn_emb, 质心)          ← 唯一权威分数
```

* `turn_score ≥ confirm_threshold(0.60)` 且本轮有效语音 ≥ `min_turn_speech_ms(2000)` → 本人
* `turn_score < continuity_threshold(0.50)` 且语音足够 → 明确非本人
* 其余 → `unknown`，直通

此外：上一轮刚确认过本人时，本轮的模糊分数（落在两个阈值之间）可在
`continuity_window_ms`(5 s) 内维持 `owner_continuity`（最长 `continuity_max_ms` 10 s）；
一旦出现明确非本人，信任立即清除。

> 注：`config/product-settings.json` 里所有旧阈值都是在前端更换之前的尺度上标定的
> （包括为了对抗误接受手动抬高的值）。schema v4 迁移会把它们重置为 0.60/0.50 并在
> `warnings` 里说明；`filter` 模式同时回落为 `observe`。

**设置页的"现场测试"与运行期使用同一公式与同一阈值**，因此两个数字可以直接对比。
（旧实现的现场测试取质心与全部模板的最大值，比运行时口径宽松，会系统性高估。）

## 窗口与最短语音

| 窗口 | 同人 p05 | 异人 p95 | 建议工作点 |
| --- | --- | --- | --- |
| 1.0 s | +0.627 | +0.174 | 0.40 |
| 1.5 s | +0.742 | +0.179 | 0.46 |
| **2.0 s** | **+0.856** | **+0.186** | **0.52** |
| 3.0 s | +0.925 | +0.210 | 0.57 |

2 s 在延迟与裕度之间最合适，因此默认 `window_ms: 2000`、`min_turn_speech_ms: 2000`。
文本无关验证类服务的公开最低时长通常为 4 s，把 `min_turn_speech_ms` 继续调高只会让更多
短回合走直通（安全方向），可以按实际会话分布调整。

## 资料文件

```text
config/speaker/registry.json      # schema 2：用户、模板、质心、特征指纹
config/speaker/templates/*.npy    # 每个模板一个 L2 归一化后的 256 维向量
config/speaker/probe.npy          # 特征指纹用的确定性探测 embedding
```

文件由 `SpeakerProfileStore` 原子写入。主中心是首要匹配依据；`include_in_centroid` 为 false
的模板不参与判定，只保留用于排查。纳入中心的模板少于 2 个时 `snapshot()` 会给出提示。
第一版明确没有回放检测、合成语音检测、变声防伪、重叠人声分离或多人权限
（`anti_spoofing: "unavailable"`）。

## API

产品设置：`PUT /api/v1/product/settings/speaker`；阈值立即用于后续窗口，运行模式从下一语音段生效。

声纹状态与资料：`GET /api/v1/speaker/status`、`GET /api/v1/speaker/profile`
（后者的 `stale` / `stale_reason` / `needs_reenrollment` / `centroid_template_count` 是界面提示的依据）。

注册与测试：

- `POST /api/v1/speaker/enrollment/start` → 返回 `prompts`（三段提示句）、`guidance`、
  `required_segments` 与 `capture_endpoint`
- `POST /api/v1/speaker/enrollment/{id}/capture`（**首选**：服务端按运行期链路录制）
- `POST /api/v1/speaker/enrollment/{id}/segment`（兜底：浏览器/API 上传 base64 音频）
- `POST /api/v1/speaker/enrollment/{id}/commit` → 自一致性门禁，失败返回
  `SPEAKER_ENROLLMENT_INCONSISTENT` 与两两余弦矩阵
- `DELETE /api/v1/speaker/enrollment/{id}`（取消并立即释放未提交录音）
- `DELETE /api/v1/speaker/templates/{template_id}`
- `PATCH /api/v1/speaker/templates/{template_id}`（设置是否纳入主声纹中心）
- `POST /api/v1/speaker/test/capture`（**首选**：服务端录制并返回与运行时同口径的分数）
- `POST /api/v1/speaker/test`（兜底：上传 base64 音频）

录制前要求当前没有进行中的语音会话，否则返回 `SPEAKER_INPUT_BUSY`；没有可用的服务端采集
设备时返回 `SPEAKER_CAPTURE_UNAVAILABLE`，界面自动回退到浏览器录制。
客户端只提交短期录音会话 ID、标签和受限 base64 音频，不得提交文件路径、模型路径、用户 ID
或向量。

环境记录：`GET /api/v1/ambient-context`、`DELETE /api/v1/ambient-context`。

## 验收与回归

```powershell
python -m pytest tests/test_speaker.py tests/test_speaker_features.py -q
python scripts\probe_speaker_verification.py
```

基准脚本产出 `logs/evaluation/speaker-verification-latest.md`，包含同人/异人分数分布、分离度、
EER、配置阈值下的 FAR/FRR，以及电平鲁棒性。**该基准使用的 `logs/evaluation/audio/` 是
Microsoft Huihui 合成语音，其中 8 个文件是同一次 60 秒录音的截断**，因此它的 EER 只能用于
回归对比，不能当作真实场景指标；真实验收必须用本人录音走第 8 章流程。

真实用户验收（必须由本人完成）：

1. `mode=off` 下删除旧模板 → 重新注册三段；
2. 用 `observe` 模式跑 ≥10 轮真实会话，确认本人分数集中在 0.85 以上、噪声与他人低于 0.2、
   没有回合落在 0.4–0.7 之间；
3. 需要时用"另一人朗读"或回放做异人检查；
4. 分布确认后再切 `filter`。

## 与 KWS 唤醒词的关系

声纹旁路和 KWS 唤醒词是两条独立能力：声纹只负责在 ASR final 的语义提交边界改善路由，KWS
负责决定语音会话是否先进入唤醒词待机。声纹 Worker 正常并不代表 KWS 关键词文件一定可用。

如果产品设置中启用了 KWS，点击"开始语音会话"时会先校验关键词文件。出现
`KWS_WORKER_FAILED` 或"关键词文件未包含"时，Voice Server 会保持在未监听状态，避免在唤醒词
链路不完整时误报已经开始录音。

只测试声纹或希望点击后直接进入 ASR 时，可在"设置 → KWS 唤醒词"关闭"启用 KWS"，保存后下一次
启动会话直接进入语音识别；声纹的 `observe` / `filter` 模式不受影响。

排查入口：

```text
GET http://127.0.0.1:8765/health
```

重点查看 `services.kws`、`startup_errors`、`wake_enabled`、`input_owner` 和 `kws.error`。声纹
问题则查看 `/api/v1/speaker/status`；`speaker` 降级不会阻止主语音链路。
