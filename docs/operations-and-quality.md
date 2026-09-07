# 运行与质量门槛

默认 `config/default.yaml` 即为唯一生产运行线。`smart_turn`、`agent` 与 `tools` 是能力配置，不是版本开关。文件工具默认仅限 `docs`；不包含 Shell、写文件、外部账号操作、RAG 或长期记忆。

默认 `audio.output_device: null`，由 Windows 选择当前活跃默认输出设备。这样可避免固定的 MME 端点在 PortAudio 实际打开 32 kHz PCM 流时失败，也能自然适应耳机和外接音箱切换。只有部署环境需要强制固定设备时，才在被忽略的 `config/local.yaml` 中设置 `audio.output_device`；修改后应先以真实输出流验证，而不只依赖设备能力查询。

运行检查：

```powershell
python -m pytest -q
npm run typecheck --workspace @voice-terminal/web
npm run test --workspace @voice-terminal/web -- --run
npm run build --workspace @voice-terminal/web
python -m compileall -q voice_terminal apps tests
git diff --check
```

软件通过后仍必须在真实设备完成：至少 20 条人工核对自然语音、耳机和扬声器下的打断、连续多轮、停止/重置、真实 ASR/Analysis/LLM/TTS 链路，以及结束至实际首音的 p50/p95。内存播放器结果不能替代这些证据。
