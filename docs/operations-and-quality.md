# 运行与质量门槛

默认 `config/default.yaml` 即为唯一生产运行线。`smart_turn`、`agent` 与 `tools` 是能力配置，不是版本开关。文件工具默认仅限 `docs`；不包含 Shell、写文件、外部账号操作、RAG 或长期记忆。

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
