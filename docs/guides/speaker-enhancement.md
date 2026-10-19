# 可选声纹增强

声纹是 Voice Terminal（语音终端）的可选身份旁路。主链为 AEC（回声消除）/降噪 → VAD（语音活动检测）/ASR（自动语音识别）。关闭和仅标记模式在声纹异常时仍允许普通请求；主动选择拦截模式后，身份未确认时不向主模型提交输入。开启“主人打断”时，打断授权也必须在异常时关闭。

设置入口为“设置 → 识别与修订”。声纹识别与打断策略位于页面上方，热词修订位于同页下方的“热词替换”区域。

## 运行模式

| 模式 | 行为 |
| --- | --- |
| `off`（关闭） | 不显示说话人标记，不按声纹拦截普通请求。 |
| `observe`（仅标记） | 更新当前声音与可可靠关联的整轮标记，不拦截请求。 |
| `filter`（拦截） | 包含标记；等待本段最终身份，主人确认与有效的连续主人均放行；非主人或服务降级时拦截，并保留诊断原因。 |

另有独立的 `owner_only_barge_in`（主人打断开关），默认关闭；`off`（关闭）与主人打断开启可同时选择，此时声纹仅供打断授权，不显示普通标记或拦截请求。开启要求已注册主人资料及可用声纹工作进程。运行中资料、工作进程或 AEC（回声消除）失效时，保持开关选择但禁止打断，并公开不可用原因。

`filter`（拦截）在流式文本派发前和最终提交时检查。流式转写保留在输入工作区，身份结算前不唤起主 Agent（决策代理）；最终整轮判断仍最多额外等待 150ms（毫秒）。每次判断都为主人或非主人：当前轮次、当前代次的主人确认及有效连续主人可提交；证据不足、超时、工作进程不可用、队列溢出、资料过期、格式错误及服务降级均按非主人处理并拦截，诊断原因继续保留。仅标记模式仍放行普通请求。已收到的可靠非主人证据不会被最终回执超时改成放行。被拦截文本只写入有界的 `AmbientTranscriptStore`（环境文字区），并释放该输入的预热合成资源。

拦截还覆盖待播输出的被动讲话标志：未准入的声音检测继续进入识别与身份分析，但不能暂停有效待播回复或撤销播放许可。

这一规则于 2026-10-10 按实际使用要求调整；此前“未知直通”的规则不再适用于拦截模式。短句、低音量或质量不足的人声也可能被拦截，应先在仅标记模式核对本人注册和分数。身份筛选不改变独立的主人打断开关，也不属于防录音重放认证。修复与验证记录见[语音可靠性修复](../testing/voice-reliability-fixes-20261010.md)。

## 滚动状态与主人打断

音频处理后在内存中保留约 4 秒 PCM（脉冲编码音频）缓存；复用现有 400ms 心跳，每次心跳计划时刻后 300ms 安排一次性声纹分析，不建立第二套周期队列。模型仍分析最近约 2 秒音频；2 秒有效人声证据在当前候选对应的有界缓存中按 VAD（语音活动检测）区间累计。每份结果保留采样边界、完成时间、质量与有效期；不能用上一位说话人的结果判断新候选。声纹分数、身份标签及向量不直接注入模型上下文。

播放期间仅对经 AEC（回声消除）处理、由近端语音门确认的候选累计声纹证据。正常音量播放继续进行；只有候选自身得到主人正向确认且回声保护持续有效，才在停止播放前授权打断，并把候选缓存交给 ASR（自动语音识别）。目标等待约 2 秒、上限约 2.5 秒；非主人、证据不足、超时及保护失效都不打断。播放自然结束后按普通新输入处理。播放标识必须取当前输出事务的真实代次，不能退回已退役输入轮次的不带代次标识。

## 声学特征前端

特征实现位于 `voice_terminal/speaker/features.py`。WeSpeaker（旧声纹模型）的兼容路径中，PCM（脉冲编码音频）→ 80 维特征必须保持与其官方 Kaldi（音频特征工具）前端对齐；该路径不代表当前默认模型：

```text
16 kHz PCM → 25/10 ms Kaldi fbank(80 mel, dither=0)
           → 去直流 → pre-emphasis 0.97 → 加窗 → 512 点 FFT
           → Kaldi mel filterbank → log → CMN
```

禁止随意替换为 librosa 默认参数、删除 CMN、改变 mel filterbank 约定或将原始余弦映射为 `(cos+1)/2`。模型输入为 `[B,T,80]`，输出为 `[B,256]`；少于 9 帧的音频必须拒绝，不得把 NaN 送入模型。

`tests/test_speaker_features.py` 负责核对旧 WeSpeaker（旧声纹模型）兼容特征路径；当前默认 CAM++（上下文感知掩码声纹模型）的前端、向量维度与模型指纹须使用对应回归覆盖。修改任何特征前端必须同步更新指纹和回归测试。

CAM++（上下文感知掩码声纹模型）是当前默认声纹模型，使用独立的官方声学前端与 192 维向量。WeSpeaker（旧声纹模型）只作为兼容背景保留，其 256 维特征、注册向量、指纹和分数均不能与 CAM++ 混用；WeSpeaker 资料不能直接用于 CAM++ 身份判定。模型分发与升级证据见[CAM++ 分发与升级验收](../releases/campplus-distribution-upgrade-20261012.md)，流式测试见[CAM++ 流式验收](../testing/campplus-streaming-acceptance-20261012.md)。

## 资料指纹

资料文件包含 `feature_fingerprint`（特征指纹）和确定性 `probe.npy`（特征检查样本），统一保存于当前用户数据目录：

```text
.local/data/speaker/registry.json
.local/data/speaker/templates/*.npy
.local/data/speaker/probe.npy
```

特征前端、模型 ID、schema 或数值实现发生变化时，旧模板必须标记为 `stale`，停止参与判定并提示重新注册。不能让旧模板在新坐标系中继续工作，否则“前端变化”和“阈值太高”无法区分。

## 注册协议

注册由三段内容不同的连续语音组成，每段 5–10 秒。禁止重复朗读同一句、持续元音、耳语和刻意夹嗓；相同文本会产生内容捷径，抬高分数但不能增加说话人证据。

首选服务端采集，确保与运行期使用同一系统默认麦克风、16 kHz（采样率）、单声道、int16（16 位整数采样格式）和处理链；没有服务端设备时才使用浏览器上传作为兜底。`audio.input_device`（旧麦克风选择字段）的名称或编号不再生效。原始注册录音不落盘。

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

`window_speech_ms`（单窗有效人声时长）必须同时达到 700ms 和窗口占比门槛；对完整 2 秒窗口而言，实际至少需要 800ms。不足 2 秒的收尾窗口按真实样本时长计算占比，禁止固定除以 2 秒。现场测试与实时结束处理都包含未对齐步进的最后完整窗。`turn_speech_ms`（整轮不重复人声时长）按 VAD 区间并集统计，不能把相邻重叠窗的时长直接相加。

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

- 有足够人声且 `turn_score`（整轮分数）达到主人确认阈值（默认 0.60，包含相等）时判主人，低于阈值判非主人；证据不足、噪声及模型异常也判非主人，诊断原因继续保留。
- `continuity_relaxation`（连续主人放宽量）默认 0.10，范围 0–0.20；连续阈值为主人阈值减去放宽量，最低为 0。页面两条滑块只修改草稿，点击“保存声纹阈值”后持久化。
- 最近一次有效主人确认后，5 秒内可进入连续主人状态，进入后最多保留到该确认后的 10 秒；整轮与实时滚动判断均使用这一放宽规则。重复查看结果或重复分析同一窗口不续期，新的合格主人音频才刷新确认时间。

拦截模式在最终结算前保留待提交正文，临时非主人结果不能让心跳调度提前删除输入。最终依据当前轮次及代次放行主人及有效连续主人；非主人或服务降级被拦截。实时工作进程在新轮次与恢复代次时补送 VAD（语音活动检测）回溯起点后的有界前滚录音，避免漏掉已录到的语音开头。

滚动快照使用 `confirm_threshold`（主人确认阈值），存在有效连续主人证据时使用派生的 `continuity_threshold`（连续主人阈值）。结果为主人或非主人，快照仍须匹配当前候选的采样区间、轮次／播放标识和有效期。

设置页现场测试必须使用同一公式和阈值，不能用“所有模板最大值”替代运行时质心分数。产品设置 schema v10（设置格式第 10 版）加入 `continuity_relaxation`（连续主人放宽量），从旧 `confirm_threshold`（主人确认阈值）及 `continuity_threshold`（旧连续阈值）推导并限制在 0–0.20；保留已保存的主人阈值与运行模式。

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
