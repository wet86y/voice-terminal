# 可选声纹增强

声纹是 Voice Terminal（语音终端）的可选旁路，不是音频主链前置条件。主链为 AEC（回声消除）/降噪 → VAD（语音活动检测）/ASR（自动语音识别）。普通请求在声纹异常时仍直通；开启“主人打断”时，打断授权必须在异常时关闭，不能退回普通打断。

## 运行模式

| 模式 | 行为 |
| --- | --- |
| `off`（关闭） | 不显示说话人标记，不按声纹拦截普通请求。 |
| `observe`（仅标记） | 更新当前声音与可可靠关联的整轮标记，不拦截请求。 |
| `filter`（拦截） | 包含标记；只否决与当前音频候选匹配、明确判为非主人的转写，未知仍直通。 |

另有独立的 `owner_only_barge_in`（主人打断开关），默认关闭；`off`（关闭）与主人打断开启可同时选择，此时声纹仅供打断授权，不显示普通标记或拦截请求。开启要求已注册主人资料及可用声纹工作进程。运行中资料、工作进程或 AEC（回声消除）失效时，保持开关选择但禁止打断，并公开不可用原因。

`filter`（拦截）在流式文本派发前和最终提交时检查。已有最终整轮判断仍最多等待 150ms；超时、`unknown`（未知）、工作进程不可用、队列溢出、资料过期和格式错误都直通。被拦截文本只写入有界的 `AmbientTranscriptStore`（环境文字区），不进入主 Agent（决策代理）、TTS（语音合成）或普通对话历史。已经派发的请求无法回撤，因此拦截是保守否决，不是严格身份认证。

## 滚动状态与主人打断

音频处理后在内存中保留约 4 秒 PCM（脉冲编码音频）缓存；复用现有 400ms 心跳，每次心跳计划时刻后 300ms 安排一次性声纹分析，不建立第二套周期队列。模型仍分析最近约 2 秒音频；2 秒有效人声证据在当前候选对应的有界缓存中按 VAD（语音活动检测）区间累计。每份结果保留采样边界、完成时间、质量与有效期；不能用上一位说话人的结果判断新候选。声纹分数、身份标签及向量不直接注入模型上下文。

播放期间仅对经 AEC（回声消除）处理、由近端语音门确认的候选累计声纹证据。正常音量播放继续进行；只有候选自身得到主人正向确认且回声保护持续有效，才在停止播放前授权打断，并把候选缓存交给 ASR（自动语音识别）。目标等待约 2 秒、上限约 2.5 秒；非主人、未知、超时及保护失效都不打断。播放自然结束后按普通新输入处理。播放标识必须取当前输出事务的真实代次，不能退回已退役输入轮次的不带代次标识。

## 唯一特征前端

唯一实现位于 `voice_terminal/speaker/features.py`，PCM → 80 维特征必须保持与 WeSpeaker/Kaldi 对齐：

```text
16 kHz PCM → 25/10 ms Kaldi fbank(80 mel, dither=0)
           → 去直流 → pre-emphasis 0.97 → 加窗 → 512 点 FFT
           → Kaldi mel filterbank → log → CMN
```

禁止随意替换为 librosa 默认参数、删除 CMN、改变 mel filterbank 约定或将原始余弦映射为 `(cos+1)/2`。模型输入为 `[B,T,80]`，输出为 `[B,256]`；少于 9 帧的音频必须拒绝，不得把 NaN 送入模型。

`tests/test_speaker_features.py` 负责与 Kaldi fbank 逐值比对；有模型时还要验证 embedding 余弦相似度。修改特征前端必须同步更新指纹和回归测试。

## 资料指纹

资料文件包含 `feature_fingerprint` 和确定性 `probe.npy`：

```text
config/speaker/registry.json
config/speaker/templates/*.npy
config/speaker/probe.npy
```

特征前端、模型 ID、schema 或数值实现发生变化时，旧模板必须标记为 `stale`，停止参与判定并提示重新注册。不能让旧模板在新坐标系中继续工作，否则“前端变化”和“阈值太高”无法区分。

## 注册协议

注册由三段内容不同的连续语音组成，每段 5–10 秒。禁止重复朗读同一句、持续元音、耳语和刻意夹嗓；相同文本会产生内容捷径，抬高分数但不能增加说话人证据。

首选服务端采集，确保与运行期使用相同的 `audio.input_device`、16kHz、单声道、int16 和处理链；没有服务端设备时才使用浏览器上传作为兜底。原始注册录音不落盘。

注册门禁：

| 检查 | 要求 |
| --- | ---: |
| 每段有效人声 | ≥ 3s |
| 三段合计有效人声 | ≥ 15s |
| 每段 VAD 语音占比 | ≥ 50% |
| 削波率 | ≤ 0.5% |
| 三段两两余弦最小值 | ≥ 0.70 |

通过后保存每段 embedding、质量摘要、质心和资料指纹；保存内容不包括原始录音。

## 判定口径

原有整轮声纹链只产生一个最终聚合分数；它仍用于整轮结束时的补充判定和现场测试。生产旁路另按心跳产生滚动快照，不能把旧整轮结果误认为当前打断候选的授权证据：

```text
窗口 2000ms，步进 500ms；窗口内有效语音 ≥ 700ms 且占比 ≥ 0.40 才提取 embedding
turn_speech_ms = 截止当前窗口末尾的 VAD 语音区间并集时长（重叠窗口不重复累计）
turn_emb = normalize(Σ window_speech_msᵢ · normalize(embᵢ))
turn_score = cos(turn_emb, centroid)
```

`window_speech_ms`（单窗有效人声时长）必须同时达到 700ms 和窗口占比门槛；对完整 2 秒窗口而言，实际至少需要 800ms。`turn_speech_ms`（整轮不重复人声时长）按 VAD 区间并集统计，不能把相邻重叠窗的时长直接相加。

现场测试与实时 Worker（实时声纹工作进程）共用基础音频质量检查、单窗人声门槛和窗口占比门槛。现场测试回报字段说明：

| 字段 | 中文说明 |
| --- | --- |
| `measured_speech_ms` | 整段测试由 VAD 测得的人声时长 |
| `turn_speech_ms` | Worker（工作进程）随每个观察窗报告的整轮不重复人声时长 |
| `candidate_window_count` | 按滑窗规则产生的候选窗口数 |
| `eligible_window_count` | 通过单窗质量和人声门槛的窗口数 |
| `window_gate_rejected_count` | 被单窗质量或人声门槛拒绝的窗口数 |
| `window_quality_counts` | 按质量标签汇总的窗口计数 |
| `window_diagnostics` | 每个窗口的人声时长、占比和质量标签 |
| `minimum_window_speech_ms` | 单窗最低人声时长要求 |
| `minimum_window_speech_ratio` | 单窗最低人声占比要求 |
| `minimum_turn_speech_ms` | 整轮最低不重复人声时长要求 |

`window_diagnostics`（逐窗诊断）的子字段说明：`index`（窗口序号）、`speech_ms`（窗口人声时长）、`speech_ratio`（窗口人声占比）、`quality`（质量标签：`ready` 就绪、`not_speech` 非语音占比不足、`insufficient_audio` 人声时长不足、`too_quiet` 电平过低、`clipped` 削波过多）。

默认阈值：

- `turn_score >= 0.60` 且本轮有效语音 ≥ 2000ms：本人；
- `turn_score < 0.50`：明确非本人；
- 中间区间：`unknown`，直通；
- 最近一次确认过本人时，短暂模糊结果可使用 continuity window 保持状态，但明确非本人立即清除。

滚动快照使用 `confirm_threshold`（主人确认阈值）作为主人下界，`continuity_threshold`（非主人确认界限；旧整轮链仍用作连续保持阈值）作为非主人上界；两者之间为未知。滚动快照不继承上一位说话人的连续状态，且必须与当前候选的采样区间、轮次／播放标识和有效期匹配。

设置页现场测试必须使用同一公式和阈值，不能用“所有模板最大值”替代运行时质心分数。schema v4 迁移会将旧阈值重置为 `0.60/0.50`，并把 `filter` 降为 `observe`。

## API

设置和状态：

```text
PUT   /api/v1/product/settings/speaker
GET   /api/v1/speaker/status
GET   /api/v1/speaker/profile
```

注册和测试：

```text
POST   /api/v1/speaker/enrollment/start
POST   /api/v1/speaker/enrollment/{id}/capture   # 首选：服务端录音
POST   /api/v1/speaker/enrollment/{id}/segment   # 兜底：上传受限 base64 音频
POST   /api/v1/speaker/enrollment/{id}/commit
DELETE /api/v1/speaker/enrollment/{id}
DELETE /api/v1/speaker/templates/{template_id}
PATCH  /api/v1/speaker/templates/{template_id}
POST   /api/v1/speaker/test/capture               # 首选：服务端测试录音
POST   /api/v1/speaker/test
```

环境记录：

```text
GET    /api/v1/ambient-context
DELETE /api/v1/ambient-context
```

客户端只能提交短期录音会话 ID、标签和受限音频内容，不能提交文件路径、模型路径、用户 ID 或向量。后端结构化错误码必须原样保留，例如 `SPEAKER_CAPTURE_UNAVAILABLE`、`SPEAKER_INPUT_BUSY` 和 `SPEAKER_ENROLLMENT_INCONSISTENT`。

## 音频并发边界

注册和测试不能与实时采集并发使用同一设备或共享 `AudioPreprocessor`。尤其在 KWS standby 下，虽然 workspace 可能为空，实时输入仍然可能持有设备和处理器。

实现必须满足其一：

1. 只要 session 处于 listening/KWS standby，就拒绝服务端注册和测试；或
2. 使用统一音频锁串行化实时处理、注册和测试；或
3. 为注册/测试创建完全隔离的设备和处理器。

在该边界完成前，声纹注册功能不应被描述为支持“实时会话期间安全并发操作”。

## 验收

自动化：

```powershell
python -m pytest tests/test_speaker.py tests/test_speaker_features.py tests/test_speaker_rolling.py tests/test_speaker_production.py -q
```

现场验收：

1. `off` 下清理旧资料并重新注册三段不同语音；
2. `observe` 跑至少 10 轮本人语音，检查分数、unknown 和降级直通；
3. 检查异人或回放样本；
4. 确认 `filter` 只拦截明确非本人；
5. 分别验证 KWS（关键词唤醒）待机、ASR（自动语音识别）活动、停止会话时的注册／测试冲突行为；
6. 验证三种模式与主人打断开／关的六种组合，包括关闭模式下独立主人打断；
7. 用真实麦克风和扬声器双工测试纯扬声器声、主人或非主人叠加播放声、AEC（回声消除）失效、候选超时及播放先结束。上线顺序为仅标记观察 → 拦截 → 单独启用主人打断。

健康检查：

```text
GET http://127.0.0.1:8765/health
GET http://127.0.0.1:8765/api/v1/speaker/status
```

重点查看 `services.kws`、`startup_errors`、`input_owner`、`kws.error`、`speaker.available` 和 `speaker.stale_reason`。

## 本机验证记录（2026-09-30）

本轮在 Windows 本机执行了 ONNX（开放神经网络交换格式）模型回归、临时 Worker（工作进程）请求和音频设备格式检查：

| 项目 | 结果 |
|---|---|
| 模型文件与运行依赖 | `cnceleb_resnet34` 模型文件存在（26,534,127 字节）；`onnxruntime`、`torch`、`torchaudio`、`sounddevice` 可导入 |
| ONNX 回归脚本 | 327 个合成语音窗口用时 12.51 秒；同人均分 0.9143、异人均分 0.0439，增益测试最大分数漂移 0.0024 |
| Worker（工作进程）请求 | 1.277 秒内就绪；2 秒合成 PCM（脉冲编码调制）请求返回 `quality=ready`（质量可用）、`speech_ms=1038`（人声时长）、`embedding_dim=256`（向量维度）、`embedding_norm=1.0`（向量范数） |
| 输入设备格式 | Intel 麦克风阵列解析至 PortAudio index 1，16 kHz、单声道、`int16` 格式检查通过；未打开采集流 |
| 自动化测试 | `test_speaker.py`、`test_speaker_features.py`、`test_speaker_rolling.py`、`test_speaker_production.py` 共 60 项通过 |

回归命令如下；报告输出到 Windows 临时目录，不写入仓库日志：

```powershell
$probeReport = Join-Path $env:TEMP 'speaker-verification-latest.md'
python -X utf8 scripts\probe_speaker_verification.py --out $probeReport
python -X utf8 -m pytest tests\test_speaker.py tests\test_speaker_features.py tests\test_speaker_rolling.py tests\test_speaker_production.py -q
```

以上模型分数来自合成语音语料，只能说明本机模型及推理链运行正常，不能代表真人识别率。设备只通过格式检查，没有采集声音；真人主人／非主人区分、播放期间的说话识别、AEC（回声消除）失效和双工打断仍须按现场验收第 7 项测试。Worker 验证使用本机临时进程，验证完成后已结束；本次没有修改主人声纹模板或持久化设置。
