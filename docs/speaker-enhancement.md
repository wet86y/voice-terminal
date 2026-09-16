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
队列溢出和格式错误均使用降级直通。被拦截文本只写入会话级有界 `AmbientTranscriptStore`，
不进入主 `SessionLedger`、主 Agent、TTS 或普通对话历史。

## 窗口和迟滞

Speaker Worker 使用 16 kHz、单声道、int16 PCM，最近 1.5 秒有效人声，每 500 ms 更新一次；
不足 500 ms 的片段只能产生 `insufficient_audio`。状态机的进入阈值默认 0.50，保持阈值默认
0.40。先以普通阈值确认过本人后，短期内才允许保持状态使用较低阈值；这两个固定阈值不会
逐轮改写成动态全局阈值。

状态只在声纹内部按窗口更新；上层只收到状态变化和最终 `SpeakerDecision`。`turn_id` 与
`generation` 同时校验，旧回合结果不能污染恢复后的新回合。

观测里的 `similarity` 保留 WeSpeaker 的原始余弦相似度（范围约为 -1 到 1）；0.50/0.40
是交互路由的测试基线，不是经过平移后的概率，也不是安全认证分数。

## 模板资料

第一版固定一个逻辑用户 `owner`，可保存多组带标签模板。设置页引导正常朗读、自然对话和
自定义标签三段录音；提取后只保留主声纹中心、模板向量、质量摘要和模型版本，原始音频不
长期保存。主中心是首要匹配依据，特殊模板只在中心落入模糊区时辅助判断。

资料文件位于：

```text
config/speaker/registry.json
config/speaker/templates/<template-id>.npy
```

文件由 `SpeakerProfileStore` 原子写入。删除模板会重建主中心；删掉唯一资料后筛选模式自然
降级为直通。第一版明确没有回放检测、合成语音检测、变声防伪、重叠人声分离或多人权限。

## API

产品设置：`PUT /api/v1/product/settings/speaker`；阈值立即用于后续窗口，运行模式从下一语音段生效。

声纹状态与资料：`GET /api/v1/speaker/status`、`GET /api/v1/speaker/profile`。

注册与测试：

- `POST /api/v1/speaker/enrollment/start`
- `POST /api/v1/speaker/enrollment/{id}/segment`
- `POST /api/v1/speaker/enrollment/{id}/commit`
- `DELETE /api/v1/speaker/enrollment/{id}`（取消并立即释放未提交录音）
- `DELETE /api/v1/speaker/templates/{template_id}`
- `PATCH /api/v1/speaker/templates/{template_id}`（设置是否纳入主声纹中心）
- `POST /api/v1/speaker/test`

环境记录：`GET /api/v1/ambient-context`、`DELETE /api/v1/ambient-context`。

客户端只提交短期录音会话 ID、标签和受限 base64 音频，不得提交文件路径、模型路径、用户 ID
或向量。

## 与 KWS 唤醒词的关系

声纹旁路和 KWS 唤醒词是两条独立能力：声纹只负责在 ASR final 的语义提交边界改善路由，KWS
负责决定语音会话是否先进入唤醒词待机。声纹 Worker 正常并不代表 KWS 关键词文件一定可用。

如果产品设置中启用了 KWS，点击“开始语音会话”时会先校验关键词文件。配置中的
`wake.keyword_label` 必须与 `config` 指向的 `keywords.txt` 中的 `@LABEL` 完全一致；例如配置为
“执事”时，文件中必须包含 `@执事`，不能仍然只有旧的 `@HEI_CLAW`。出现
`KWS_WORKER_FAILED` 或“关键词文件未包含”时，Voice Server 会保持在未监听状态，避免在唤醒词
链路不完整时误报已经开始录音。

只测试声纹或希望点击后直接进入 ASR 时，可在“设置 → KWS 唤醒词”关闭“启用 KWS”，保存后下一次
启动会话直接进入语音识别；声纹的 `observe` / `filter` 模式不受影响。需要保留唤醒词时，应在
KWS 设置页重新保存目标唤醒词，让系统重新生成并原子替换关键词文件；重新启动后，按钮进入的
是“等待唤醒词”状态，必须先说出配置的唤醒词才会开始本轮 ASR。

排查入口：

```text
GET http://127.0.0.1:8765/health
```

重点查看 `services.kws`、`startup_errors`、`wake_enabled`、`input_owner` 和 `kws.error`。声纹
问题则查看 `/api/v1/speaker/status`；`speaker` 降级不会阻止主语音链路。
