# 可选声纹增强

声纹是 Voice Terminal 的可选旁路，不是音频主链前置条件。主链为 AEC/降噪 → VAD/ASR；声纹异常、模型缺失或 Worker 不可用时，主语音链仍按降级策略运行。

## 运行模式

| 模式 | 行为 |
| --- | --- |
| `off` | 不启动声纹旁路，ASR/Agent 原样直通。 |
| `observe` | 执行识别并记录诊断，所有 ASR final 直通。 |
| `filter` | 只拦截明确判定为 `non_owner_confirmed` 的文本，其余状态直通。 |

`filter` 从 ASR final 可提交时开始等待，预算为 150ms。超时、unknown、Worker 不可用、队列溢出、资料过期和格式错误都必须安全直通。被拦截文本只写入有界的 `AmbientTranscriptStore`，不进入主 Agent、TTS 或普通对话历史。

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

每轮只产生一个权威分数：

```text
窗口 2000ms，步进 500ms；窗口内有效语音 ≥ 700ms 且占比 ≥ 0.40 才提取 embedding
turn_emb = normalize(Σ speech_msᵢ · normalize(embᵢ))
turn_score = cos(turn_emb, centroid)
```

默认阈值：

- `turn_score >= 0.60` 且本轮有效语音 ≥ 2000ms：本人；
- `turn_score < 0.50`：明确非本人；
- 中间区间：`unknown`，直通；
- 最近一次确认过本人时，短暂模糊结果可使用 continuity window 保持状态，但明确非本人立即清除。

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
python -m pytest tests/test_speaker.py tests/test_speaker_features.py -q
```

现场验收：

1. `off` 下清理旧资料并重新注册三段不同语音；
2. `observe` 跑至少 10 轮本人语音，检查分数、unknown 和降级直通；
3. 检查异人或回放样本；
4. 确认 `filter` 只拦截明确非本人；
5. 分别验证 KWS standby、ASR active、停止会话时的注册/测试冲突行为。

健康检查：

```text
GET http://127.0.0.1:8765/health
GET http://127.0.0.1:8765/api/v1/speaker/status
```

重点查看 `services.kws`、`startup_errors`、`input_owner`、`kws.error`、`speaker.available` 和 `speaker.stale_reason`。
