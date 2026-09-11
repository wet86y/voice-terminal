# DeepSeek thinking / 空白输出对照实验报告

日期：2026-09-10（Asia/Hong_Kong）  
工作区：`D:\项目开发\语音前端`  
模型：`deepseek-v4-flash`  
传输：DeepSeek Chat Completions，SSE，`response_format=json_object`

## 结论

本次真实实验完成了 **150 次串行请求**，5 个实验臂各 30 次；每个请求均为一次独立尝试，失败不自动重试，间隔 1 秒。A/B/C/D 所有请求均 HTTP 200、`finish_reason=stop`，本地 JSON envelope 均有效，最终没有出现“仅空白内容”的响应。

观察到稳定的流式首帧差异：

- enabled + `reasoning_effort=low`：30/30 次检测到 reasoning；首个 content delta 为非空内容 30/30 次。
- disabled：0/60 次检测到 reasoning；首个 content delta 为空字符串 60/60 次（短上下文和长上下文各 30 次），随后均产生非空 JSON。

因此，本实验支持“禁用 thinking 会产生空首 content delta”这一较窄的流式行为观察；**不支持**“禁用 thinking 导致最终空白流”或“禁用 thinking 提高最终空白流比例”。最终空白-only 在所有真实组中均为 0/30。由于样本中没有一次最终空白事件，不能把该结论外推为低概率空白故障不存在。

## 实验设计

| 组 | 输入/上下文 | thinking 设置 | 请求数 |
| --- | --- | --- | ---: |
| A | 短上下文，2 messages | enabled，`reasoning_effort=low` | 30 |
| B | 与 A 完全相同的短上下文，2 messages | disabled | 30 |
| C-enabled | 非敏感合成长上下文，42 messages | enabled，`reasoning_effort=low` | 30 |
| C-disabled | 与 C-enabled 完全相同的 42 messages | disabled | 30 |
| D | 非敏感合成的“未回复/确认已听见”风险输入，18 messages | enabled，`reasoning_effort=low` | 30 |

C 的 42 messages 由 system message、20 组 user/assistant 合成历史对、当前 user message 组成；没有使用真实用户正文。D 也只使用合成消息，目的是在不人为注入非法响应的前提下探测类似“未回复投诉”上下文是否能自然触发空白终止。

## 各组结果

延迟单位为 ms；`first non-whitespace` 是首个非空白 content 的到达时间，`terminal` 是终止标记到达时间；p95 为样本 p95 的描述性统计。

| 组 | HTTP 200 | stop | reasoning | 首个 content 为空 | 首个非空白中位数 | terminal 中位数 | envelope 有效 | 最终空白-only | 最终非空白字符中位数 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A short enabled | 30/30 | 30/30 | 30/30 | 0/30 | 1133.0 | 1209.3 | 30/30 | 0/30 | 82 |
| B short disabled | 30/30 | 30/30 | 0/30 | 30/30 | 241.9 | 402.4 | 30/30 | 0/30 | 79 |
| C long enabled | 30/30 | 30/30 | 30/30 | 0/30 | 979.9 | 1054.9 | 30/30 | 0/30 | 82 |
| C long disabled | 30/30 | 30/30 | 0/30 | 30/30 | 328.1 | 491.5 | 30/30 | 0/30 | 82 |
| D blank-like enabled | 30/30 | 30/30 | 30/30 | 0/30 | 928.1 | 1023.5 | 30/30 | 0/30 | 74 |

完整的逐轮 JSON 仅保存状态、计数、延迟、结构形状和首个 delta 的短哈希，不保存模型正文、reasoning 正文、用户正文或 provider 错误正文：

[deepseek-thinking-whitespace-matrix-20260910.json](D:\项目开发\语音前端\reports\deepseek-thinking-whitespace-matrix-20260910.json)

## 关键对照观察

### 短上下文：A vs B

- A 和 B 使用完全相同的 2-message 输入。
- A 的首个非空白 content 中位数为 1133.0 ms，B 为 241.9 ms。
- A 的 terminal 中位数为 1209.3 ms，B 为 402.4 ms。
- B 的首个 content delta 在 30/30 次为空字符串；这不是最终空响应，因为随后 30/30 次都完成了有效 JSON envelope。
- A/B 最终空白-only 均为 0/30。

### 长上下文：C-enabled vs C-disabled

- 两组使用完全相同的 42-message 非敏感合成上下文。
- C-enabled 的首个非空白 content 中位数为 979.9 ms，C-disabled 为 328.1 ms。
- C-enabled 的 terminal 中位数为 1054.9 ms，C-disabled 为 491.5 ms。
- C-disabled 首个 content delta 为空字符串 30/30；C-enabled 为 0/30。
- 两组最终空白-only 均为 0/30，envelope 均为 30/30 有效。

### reasoning 观测

- enabled low：A、C-enabled、D 均为 reasoning 30/30。
- disabled：B、C-disabled 均为 reasoning 0/30。
- 这说明本探针的 payload 设置确实生效；它不能单独证明 provider 内部 reasoning 计算成本与计时之间的因果关系。

## D 组与恢复策略

D 的 30 次真实生产 API 风险探测全部正常：HTTP 200、stop、envelope 有效 30/30，最终空白-only 0/30。因此没有真实空白终止可供安全触发恢复，也没有把合成成功当作生产恢复成功。

现有运行时可控回归测试 `tests/test_whitespace_recovery_reproduction.py` 仍提供立即同态恢复链的定量证据：

- 1 次普通请求 + 3 次连续空白终止 + 第 4 次有效恢复请求，共 4 次 provider 调用。
- `SPEAK_ENVELOPE_INVALID`：3 次。
- `LLM_RECOVERY_QUEUED`：3 次。
- `LLM_RECOVERY_STARTED`：3 次。

该测试说明当前运行时在合成空白终止下会立即进入同态恢复，并能形成连续恢复链；它没有实现或验证生产中的受限恢复策略。若把“最多 1 次恢复”作为纯计数上的受限策略，其上限将是普通请求加 1 次恢复，即 2 次调用；这只是反事实容量说明，不是本次真实 API 测量。

既有 `runtime-v05.log` 的脱敏事件计数为 `deepseek.response_status=41`，但 `SPEAK_ENVELOPE_INVALID`、`LLM_RECOVERY_QUEUED`、`LLM_RECOVERY_STARTED` 均为 0；日志没有为 D 提供真实空白终止证据。

## 统计限制

每个真实实验臂 n=30，适合发现本次测试条件下的明显、稳定差异，不足以证明低概率故障不存在。对 n=30 中 0 次事件，按常用“3/n”近似，单组真实空白-only 比例的单侧 95% 上界约为 10%；精确的 zero-event 单侧上界约为 9.5%。因此“0/30”应读作“本次条件下未观察到”，而不是“发生率为零”。

此外：

- A/B 与 C-enabled/C-disabled 的时间差是描述性对照，不做 provider 内部因果归因。
- D 的输入是非敏感合成的类似上下文，不是历史会话正文的逐字重放。
- 流式首帧为空字符串与最终空白-only 是不同事件；本次只观察到前者，未观察到后者。
- 没有进行自动重试，150 次均为独立一次尝试；没有启动 TTS/PCM，也没有重启服务。

## 验证

- `python -m py_compile scripts/probe_deepseek_thinking_whitespace_matrix.py`：通过。
- `python -m pytest -q tests/test_whitespace_recovery_reproduction.py tests/test_v06_contracts.py tests/test_conversation_runtime.py`：**27 passed**。
- 探针没有修改生产 Python 模块、`config/local.yaml` 或 `.env.local`。

