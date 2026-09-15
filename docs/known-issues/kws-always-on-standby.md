# KWS 常驻待机、中文唤醒与交互隔离

## 状态

- 严重度：P1（持续在线语音入口边界）
- 代码状态：已解决
- 自动化验收：已通过
- 运行验收：服务重启后检查 `/health`，真实麦克风、关键词和 30 秒回待机仍需现场确认

## 原问题

原 KWS 只承担会话内门控，无法在麦克风持续运行时与活跃 ASR 交互形成互斥的音频所有权。若直接把
KWS 接到现有帧链，KWS、VAD 和 ASR 会同时消费 PCM，唤醒词也可能被写入用户 turn；静默结束后若
调用完整 `stop_listening()`，还会关闭本应继续驻留的 ASR、LLM、TTS 和 Agent Episode。

## 已实施处置

- KWS 现在只有一个产品开关：`wake.enabled=true` 即持续待机；旧的
  `wake.always_on` / `VOICE_WAKE_ALWAYS_ON` 仅作为兼容字段，不再改变运行语义。
- 产品设置不提供独立的“全天候待机”或 KWS 生效时长；启用 KWS 就在本次语音会话中持续待机，
  只有停止会话才会释放 KWS 输入所有权。30 秒是最终静默检查/回待机事件，不是 KWS 过期时间。
- 中文唤醒词由 sherpa 的 `phone+ppinyin` 路径生成，CJK 字符和中文标点会在生成前规范化，
  不要求词条存在于英文 CMU 词典。当前模型已用中文测试音频和真实 `KwsWorker` 验证可命中。
- UI 灵敏度与 sherpa 阈值采用反向映射：`threshold = (100 - sensitivity) / 100`；默认阈值
  `0.25` 显示为 `75%`。旧版把 `98` 直接作为阈值会导致几乎无法唤醒，修复后 `98` 表示更容易唤醒，
  但可能增加误触发，现场调试建议先使用 `60–80%`。
- 常驻模式使用 `none → kws → asr → kws` 输入所有权；KWS 所有权下在 VAD 前短路，ASR 所有权下
  KWS Worker 保留但不接收 PCM。
- KWS 命中不创建用户 turn，改用 `source_id=wake-<uuid>` 调度既有 `system_check`，并以
  `prompt_kind=kws_wake` 在下一次 400ms heartbeat 请求简短确认语。
- 静默阶梯扩展为 13 个 forced window，并在 30 秒派发 `final_silence`。
- final 请求返回 wait、失败或协议恢复时在请求结算后回 KWS；返回 speak 时等待 TTS 播放、取消或
  失败事务完成。回调同时校验 scheduler generation 和控制器 activation generation。
- 用户在 final 或唤醒确认期间讲话时，旧 system-check speech 和旧回待机任务失效，不抢占新输入。
- KWS 初始化、运行和重新接管失败时 fail-closed；ASR 故障时回到健康 KWS，不开放无保护输入。
- `runtime.snapshot` 与 `/health` 增加可选 `wake_always_on`、`input_owner` 字段，旧 WebUI 协议兼容。
- `/health.kws` 提供 `running`、`error`、`submitted_frames`、`dropped_frames`、
  `detected_count`、`queue_depth`，用于区分“没有收到音频”“队列拥塞”和“模型没有命中”。

## 自动化证据

2026-09-15 本地验证结果：

```text
python -m pytest -q
282 passed, 2 skipped

npm test
16 passed

npm run typecheck
passed

npm run build
passed

git diff --check
passed
```

测试覆盖中文关键词规范化与生成、灵敏度阈值映射、单开关待机语义、默认配置隔离、ASR 不可用时的
受保护启动、KWS 所有权下 VAD/ASR 零消费、无伪造用户输入、13 个 forced window、30 秒 final、
wait/speak exactly-once 结算、新输入 generation 覆盖和旧前端兼容。

## 现场关闭条件

代码问题视为已解决。部署后的运行问题只有在以下现场流程全部通过后关闭：

1. `enabled=false` 点击开始后完整复现原交互基线；
2. `enabled=true` 点击开始后 `/health.input_owner=kws`，ASR 不消费 PCM；
3. 说出关键词后切换为 `input_owner=asr`，LLM/TTS 自然播报确认语并接受正常命令；
4. 30 秒 final 及播报结算后恢复 `input_owner=kws`，可再次唤醒；
5. 点击停止后 `input_owner=none`，任何关键词都不再产生响应。
