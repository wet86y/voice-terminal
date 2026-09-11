# OpenCode Go 均衡模式诊断（v2）

状态：阻塞于最小 strict Responses schema 预检，未执行大规模 soak。

## 范围与脚本

- 诊断脚本：[probe_balanced_mode_soak_v2.py](D:\项目开发\语音前端\scripts\probe_balanced_mode_soak_v2.py)
- 机器报告：[balanced-mode-agent-actionrunner-soak-v2-20260910.json](D:\项目开发\语音前端\reports\balanced-mode-agent-actionrunner-soak-v2-20260910.json)
- 计划轮次：6 组 × 10 轮 = 60 轮（实际完成：0/60）
- 计划组：wake、ordinary、interrupt、waiting_tools、balanced_no_tools、balanced_with_tools
- 运行路径：当前 `AgentEpisodeRuntime`、Responses strict schema、真实 `ActionRunner`；输入边界为拟真 `USER_INPUT_FINALIZED`（ASR-final 后、TTS 前）；TTS 永不启动。
- 诊断脚本只会写入元数据、哈希、长度、事件/状态/延迟和错误类别；不写入密钥、模型正文、reasoning、完整 prompt、工具正文或 URL。

## 最小预检

预检使用当前 source 构建两种 schema：

1. 无业务工具的 strict envelope；
2. 真实业务工具目录 + 均衡思考控制 action 的 strict envelope。

`request_thinking` wire shape 已固定为：

```json
{"name":"request_thinking","arguments":{"request":true}}
```

两种 schema 均收到真实 provider HTTP 400：

| schema | HTTP | terminal | valid envelope |
|---|---:|---|---|
| no_tools | 400 | 未开始 | 否 |
| tools_and_thinking | 400 | 未开始 | 否 |

因此按诊断要求停止，避免在相同 provider/schema 拒绝上继续发起 50+ 次请求。JSON 报告中的 `rows` 为空，明确表示没有任何 soak 轮次被计入；`tool_probe`、thinking barrier、ActionRunner 真实工具延迟和调度回归指标均未执行。

## 结果、问题与建议方向

结果：本轮不能证明均衡模式的 request_thinking、2 秒 barrier、工具提前/迟到完成、`TOOL_COMPLETED` 回归 400ms 队列、speak/prefix 或用户开口取消 thinking wait 的行为，因为 provider 在 schema 编译/请求阶段即返回 400。

当前可归因问题：strict Responses schema 被 provider 拒绝；本报告没有保留 provider 错误正文。旧的“空 arguments”形状没有被脚本复用，预检记录的控制 action shape 是 `arguments.request=true`。在重新进行 soak 前，应先取得一次脱敏的 HTTP 400 错误类别/字段路径，并由产品侧确认 OpenCode Go 对当前 strict schema（尤其是所有 merged argument properties required、无工具动作数组和 boolean `request` 字段）的支持子集；修复或确认兼容后，再从最小预检开始执行 60 轮。

备注：预检之前曾有一次只读连接失败（TLS connect timeout/error），随后 `/models` 返回 200 且既有单请求 Responses 稳定性探针通过；最终阻塞依据是重试后两种 schema 均为 HTTP 400，而不是网络连通性。
