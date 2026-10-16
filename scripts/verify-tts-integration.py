"""真实 TTS（语音合成）验证：隔离本地端口、静音缓存、取消交接与在线回执。"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voice_terminal.providers.audio_cpp_tts import AudioCppTtsClient, AudioCppTtsServer
from voice_terminal.providers.minimax_tts import MiniMaxTtsClient
from voice_terminal.tts.voices import ModelVoiceLibraries
from voice_terminal.config import load_config


async def playback_gate_case(client):
    """真实适配器经过公共管线；回调写入测试缓冲，始终不打开音频设备。"""
    from voice_terminal.audio.player import PcmPlayer
    from voice_terminal.controller.episode_output import EpisodeSpeechOutput, SpeechHandle
    async def noop(*args, **kwargs):
        pass
    player = PcmPlayer(sample_rate=client.sample_rate, channels=1, output_device=None)
    controller = SimpleNamespace(tts=client, player=player,
        options=SimpleNamespace(tts_pacing=False, tts_lead_ms=2500, tts_local_buffer_ms=3000),
        bridge_options={}, _turn_timings={}, _track=lambda task: task)
    output = EpisodeSpeechOutput(controller)
    output.runtime = SimpleNamespace(gate=lambda identity: None, emit=noop, log=noop,
                                    user_speaking=True, closed=False, request_started_at=None)
    output.loop = asyncio.get_running_loop()
    output._playback_hold = True
    output._playback_release.clear()
    handle = SpeechHandle(output, ("verification", "gate"), "gate-turn", 0, "calm")
    handle.tts_client = client
    handle.text = "第一段正文必须提前生成，但在播放许可关闭时保持静音。第二段保留正常预取窗口。最后一段文字仍应保持可改。"
    output.handle = handle
    output._handles_by_token[handle.workspace.token] = handle
    async def deltas():
        yield handle.text
        yield None
    task = asyncio.create_task(output._play_stream(handle.workspace, handle.workspace.token, deltas(),
                                                   handle=handle, speech_id=handle.speech_id))
    handle.task = task
    try:
        async with asyncio.timeout(90):
            while handle.tts_session is None or not handle.tts_session.received_bytes:
                if task.done():
                    await task
                    raise AssertionError("公共管线没有提前合成音频")
                await asyncio.sleep(.01)
        buffer = bytearray(3200)
        player._callback(buffer, 1600, None, None)
        assert not any(buffer) and player.buffered_ms == 0, "关闭播放许可时设备缓冲存在声音"
        cached_bytes = handle.tts_session.received_bytes
        locked_chars = handle.sent_chars
        if client.effective_scheduling == "sentence_window":
            assert locked_chars < len(handle.text), "正常锁定窗口扩大到整个可改尾巴"
            assert len(handle.admission.submitted_clauses) <= 2, "超过当前块加一个预取块"
        output.runtime.user_speaking = False
        output.release_held()
        heard = False
        async with asyncio.timeout(120):
            while not task.done():
                frames = max(1, client.sample_rate // 50)
                buffer = bytearray(frames * 2)
                player._callback(buffer, frames, None, None)
                heard = heard or any(buffer)
                await asyncio.sleep(.005)
        await task
        assert heard, "授权后回调没有读到音频"
        return {"held_pcm_bytes": cached_bytes, "locked_chars_before_release": locked_chars,
                "silent_before_release": True, "audio_after_release": True}
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if handle.tts_session is not None:
            await handle.tts_session.close()


async def cancellation_case(client, *, before_audio):
    """取消前不读取音频；cancelled（已取消）的读流必须为空。"""
    old = client.session(fresh=True)
    await old.start()
    arrived = asyncio.Event()
    old.first_audio_callback = arrived.set
    long_text = "这是一段用于检验提前合成取消交接的播报正文，所有音频暂存在事务内部，不进入播放器。" * 6
    sending = asyncio.create_task(old.send_text(long_text))
    if before_audio:
        if isinstance(client, AudioCppTtsClient):
            async with asyncio.timeout(30):
                while old._active_response is None:
                    assert not sending.done(), "请求在原生响应建立前异常结束"
                    await asyncio.sleep(.001)
        await asyncio.sleep(.03)
        if isinstance(client, AudioCppTtsClient):
            assert old.received_bytes == 0, "首音频前取消测试已越过首音频"
    else:
        async with asyncio.timeout(120):
            while not int(getattr(old, "received_bytes", 0)):
                await asyncio.sleep(.01)
    cached = int(getattr(old, "received_bytes", 0))
    started = time.perf_counter()
    confirmed = await old.cancel(timeout_seconds=2)
    await asyncio.gather(sending, return_exceptions=True)
    cancel_ms = round((time.perf_counter() - started) * 1000, 2)
    await old.close()
    release_ms = round((time.perf_counter() - started) * 1000, 2)
    native_release = getattr(old, "context_release_confirmed", None)
    if isinstance(client, AudioCppTtsClient):
        assert native_release is True, "原生执行锁与上下文释放未取得确认"
    assert [chunk async for chunk in old.audio_chunks()] == [], "旧事务取消后仍返回音频"
    assert int(getattr(old, "_buffered_bytes", 0)) == 0, "旧事务缓存未释放"
    fresh = client.session(fresh=True)
    first_time = []
    fresh.first_audio_callback = lambda: first_time.append(time.perf_counter()) if not first_time else None
    await fresh.start()
    await fresh.send_text("新的请求应完整生成，不携带上一事务的音频。")
    if getattr(fresh, "bidi", False):
        await fresh.flush()
    else:
        await fresh.finish()
    chunks = []
    async with asyncio.timeout(120):
        async for chunk in fresh.audio_chunks():
            if not first_time:
                first_time.append(time.perf_counter())
            chunks.append(chunk)
    count = sum(map(len, chunks))
    assert count > 0, "取消后的新请求没有完整音频"
    receipts = int(getattr(fresh, "completed_segments", 0))
    await fresh.close()
    return {"before_audio": before_audio, "cached_bytes": cached, "cancel_confirmed": confirmed,
            "cancel_ms": cancel_ms, "old_session_close_ms": release_ms, "native_release_confirmed": native_release,
            "next_first_audio_ms": round((first_time[0] - started) * 1000, 2),
            "next_pcm_bytes": count,
            "next_completed_segments": receipts if not getattr(fresh, "bidi", False) else None,
            "next_server_boundary_events": receipts if getattr(fresh, "bidi", False) else None}


async def repeated_generation_case(client):
    """固定种子下重复相同正文，捕获预填充图复用造成的短输出与噪声化。"""
    import hashlib
    import numpy as np

    if client.local_model != "voxcpm2" or client.sample_rate != 48000:
        raise AssertionError("重复合成回归必须使用二代模型的 48 kHz（千赫兹）原生音频。")
    rows = []
    for steps in (10, 15):
        for text in ("我是七三，你的语音助手。", "好，那这事就这么定了。"):
            baseline = None
            for repeat in range(3):
                session = client.session(fresh=True, inference_steps=steps)
                session._request_options["seed"] = 1234  # seed（随机种子）：固定其他生成条件。
                try:
                    await session.start()
                    await session.send_text(text)
                    await session.finish()
                    pcm = b"".join([chunk async for chunk in session.audio_chunks()])
                    assert pcm and session.completed_segments == 1, "重复生成没有完整正文回执"
                finally:
                    await session.close()
                samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
                if baseline is None:
                    baseline = samples
                assert samples.size == baseline.size, "同种子、同正文复用计算图后音频长度改变"
                reference_rms = float(np.sqrt(np.mean(baseline**2)))
                assert reference_rms > 0, "首次生成是全静音，无法建立有效的音频对照"
                relative_error = float(np.sqrt(np.mean((samples - baseline)**2))) / reference_rms
                # 允许显卡预热带来的微小浮点差异；1% 波形均方根误差远小于本次失真。
                assert relative_error < .01, "复用计算图后波形与首次生成严重不一致"
                rows.append({"text": text, "inference_steps": steps, "repeat": repeat + 1,
                             "audio_ms": samples.size * 1000 / client.sample_rate,
                             "relative_rms_error": relative_error,
                             "pcm_sha256": hashlib.sha256(pcm).hexdigest()})
    return {"requests": rows, "sample_rate": client.sample_rate,
            "fields": {"requests": "相同正文连续三次合成的逐请求记录", "text": "合成正文",
                       "inference_steps": "固定推理步数", "repeat": "重复次数序号",
                       "audio_ms": "原生音频毫秒数", "relative_rms_error": "相对首次音频的均方根误差比例",
                       "pcm_sha256": "原生音频样本校验摘要", "sample_rate": "实际下行采样率"}}


async def voice_design_case(client):
    """二代纯设计与混合设计走正式候选管线，保存目标样音并验证正式克隆快照。"""
    from voice_terminal.server.tts_voice_runtime import TtsVoiceOperationManager, DESIGN_SAMPLE_TEXT
    from voice_terminal.audio.player import PcmPlayer
    root = ROOT / ".local/cache/reports/tts-integration/design" / uuid4().hex
    libraries = ModelVoiceLibraries(root)
    library = libraries.for_model("voxcpm2")
    seed = ROOT / ".local/models/audiocpp/voice-seed"
    transcript = json.loads((seed / "default.json").read_text(encoding="utf-8"))["reference_text"]
    source = library.register_default((seed / "default.wav").read_bytes(), transcript)
    original = client.voice_library
    client.voice_library = libraries
    # 播放器采样率跟随客户端的下行率：VoxCPM2 原生 48 kHz，写死 16 kHz 会让
    # 每回调帧数与真实字节数不匹配，播放推进量会算错。
    player = PcmPlayer(sample_rate=client.sample_rate, channels=1, output_device=None)
    runtime = SimpleNamespace(voice_library=libraries, tts=SimpleNamespace(local=client), player=player,
                              config=SimpleNamespace(tts=SimpleNamespace(backend="voxcpm1", local_model="voxcpm2")))
    manager = TtsVoiceOperationManager(runtime)
    results = []
    requests = []  # requests（实际发给原生服务的合成入口）。
    request_bodies = []  # request_bodies（实际请求参数，用于核对正式与样音步数隔离）。
    async def record_request(request):
        if request.method == "POST" and request.url.path in {"/v1/tasks/run", "/v1/audio/speech"}:
            requests.append(request.url.path)
            request_bodies.append(json.loads(request.content))
    http = client._request_client()
    http.event_hooks["request"].append(record_request)
    try:
        for mixed in (False, True):
            draft = {"name": "混合样音" if mixed else "设计样音", "design_description": "温柔自然的年轻女声，吐字清晰，语速平稳。"}
            if mixed:
                draft.update(source_reference_asset_id=source["reference_asset_id"], source_reference_text=transcript)
            request_offset = len(requests)
            operation = manager.start_preview("voxcpm2", draft, DESIGN_SAMPLE_TEXT)
            task = manager._items[operation["operation_id"]].task
            frame = max(1, player.sample_rate // 50)
            async with asyncio.timeout(120):
                while not task.done():
                    player._callback(bytearray(frame * 2), frame, None, None)
                    await asyncio.sleep(.005)
            await task
            completed = manager.get(operation["operation_id"])
            assert completed["state"] == "succeeded", completed["error"]
            preview_endpoint = requests[request_offset]
            assert preview_endpoint == "/v1/tasks/run", "样音没有使用完整任务接口"
            preview_body = request_bodies[request_offset]["request"]
            assert preview_body["options"]["num_inference_steps"] == "30"
            assert preview_body["options"]["voxcpm2.full_decode"] == "true"
            assert "audio_base64" not in preview_body and "reference_text" not in preview_body
            candidate = completed["result"]["voice"]
            assert candidate["inference_steps"] == 30
            assert candidate["generation_recipe"] == "voxcpm2-full-decode-v2"
            saved = library.create_voice(candidate)
            formal = library.resolve("voxcpm2", {"voice_id": saved["id"], "revision": 1})
            assert formal.reference_path == library.asset_path(candidate["reference_asset_id"])
            assert formal.style_description == "" and not formal.is_preview
            if mixed:
                assert candidate["reference_asset_id"] != source["reference_asset_id"]
                assert saved["source_reference_text"] == transcript
            clone = client.session(voice_snapshot=formal)
            try:
                await clone.start()
                await clone.send_text("正式播报使用已经保存的目标样音进行音色克隆。")
                await clone.finish()
                formal_bytes = sum([len(chunk) async for chunk in clone.audio_chunks()])
                assert formal_bytes > 0 and not clone._control_instructions()
                formal_body = request_bodies[-1]
                assert requests[-1] == "/v1/audio/speech"
                assert formal_body["options"]["num_inference_steps"] == client.inference_steps_by_model["voxcpm2"]
                assert formal_body["prompt_audio"] == formal_body["voice_ref"] == str(formal.reference_path.resolve())
                assert formal_body["reference_text"] == candidate["reference_text"]
            finally:
                await clone.close()
            # 高保真旁路：原生率样音另存一份，主素材仍是 16 kHz 供编码器使用。
            asset_id = candidate["reference_asset_id"]
            hifi = library.high_fidelity_path(asset_id)
            asset_meta = library._index["assets"][asset_id]
            assert asset_meta["sample_rate"] == 16000
            assert hifi is not None and hifi.is_file(), (
                f"高保真副本缺失 mixed={mixed} client_rate={client.sample_rate} "
                f"native={client.native_sample_rate} meta={asset_meta}")
            assert asset_meta["high_fidelity"]["sample_rate"] == client.native_sample_rate
            assert hifi.stat().st_size > library.asset_path(asset_id).stat().st_size
            results.append({"mixed": mixed, "duration_ms": completed["result"]["duration_ms"],
                            "preview_endpoint": preview_endpoint,
                            "target_asset_id": asset_id, "formal_uses_target_sample": True,
                            "formal_pcm_bytes": formal_bytes,
                            "sample_steps": candidate["inference_steps"],
                            "formal_steps": formal_body["options"]["num_inference_steps"],
                            "high_fidelity_rate": asset_meta["high_fidelity"]["sample_rate"],
                            "high_fidelity_bytes": hifi.stat().st_size})
            results[-1]["sample_path"] = str(hifi)
        return results
    finally:
        http.event_hooks["request"].remove(record_request)
        await manager.close()
        client.voice_library = original


async def sample_cancellation_case(client):
    """完整样音取消后丢弃迟到音频，确认原生执行锁释放后再交接。"""
    from voice_terminal.server.tts_voice_runtime import DESIGN_SAMPLE_TEXT
    library = client.voice_library.for_model("voxcpm2")
    old = client.session(voice_snapshot=library.resolve_design("自然温和的声音", is_preview=True),
                         preview=True, inference_steps=30)
    await old.start()
    sender = asyncio.create_task(old.send_text(DESIGN_SAMPLE_TEXT))
    try:
        async with asyncio.timeout(30):
            while not old._native_request_submitted:
                assert not sender.done()
                await asyncio.sleep(.001)
        await asyncio.sleep(.1)
        assert old.received_bytes == 0
        started = time.perf_counter()
        await old.cancel()
        cancel_ms = (time.perf_counter() - started) * 1000
        await old.close()
        release_ms = (time.perf_counter() - started) * 1000
        assert old.context_release_confirmed is True
        assert [pcm async for pcm in old.audio_chunks()] == []
        following = client.session()
        try:
            await following.start()
            await following.send_text("取消后的正式播报正常。")
            await following.finish()
            pcm_bytes = sum([len(chunk) async for chunk in following.audio_chunks()])
            assert pcm_bytes > 0 and following.completed_segments == 1
        finally:
            await following.close()
        return {"cancel_ms": round(cancel_ms, 2), "native_release_confirmed": True,
                "old_session_close_ms": round(release_ms, 2),
                "next_pcm_bytes": pcm_bytes, "late_audio_discarded": True}
    finally:
        await asyncio.gather(sender, return_exceptions=True)
        await old.close()
        client._warmed = False


async def local(report, *, design_only=False):
    runtime = ROOT / ".local"
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 18883)) == 0:
            raise RuntimeError("隔离端口 18883 已被占用，不复用其他服务。")
    config_path = runtime / "cache/reports/tts-integration/server-18883.json"
    AudioCppTtsServer.write_runtime_config(runtime, config_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.update(host="127.0.0.1", port=18883)
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    libraries = ModelVoiceLibraries(runtime / "data/tts")
    server = AudioCppTtsServer(runtime, config_path, local_model="voxcpm1")
    client = AudioCppTtsClient(server, voice_library=libraries,
                              active_voice_provider=lambda model: libraries.for_model(model).active_selection,
                              inference_steps_by_model={"voxcpm1": 10, "voxcpm2": 10})
    try:
        for model in (("voxcpm2",) if design_only else ("voxcpm1", "voxcpm2")):
            await client.switch_model(model)
            await client.ensure_ready()
            result = []
            if not design_only:
                for before_audio in (True, False):
                    result.append(await cancellation_case(client, before_audio=before_audio))
                report[model] = {"cancellation": result, "playback_gate": await playback_gate_case(client)}
            if model == "voxcpm2":
                if not design_only:
                    report[model]["repeated_generation"] = await repeated_generation_case(client)
                report.setdefault(model, {})["voice_design"] = await voice_design_case(client)
                report[model]["sample_cancellation"] = await sample_cancellation_case(client)
            print(json.dumps({model: report[model]}, ensure_ascii=False), flush=True)
    finally:
        await client.close()


async def online(report):
    config = load_config(ROOT / "config/default.yaml", ROOT / ".local/config/local.yaml")
    key = os.environ.get(config.tts.api_key_env, "").strip()
    if not key:
        raise RuntimeError("未发现在线语音凭据。")
    for transport in ("websocket", "websocket_bidi"):
        client = MiniMaxTtsClient(base_url=config.tts.base_url, websocket_path=config.tts.websocket_path,
                                 model=config.tts.model, api_key=key, sample_rate=config.tts.sample_rate,
                                 voice_id=config.tts.voice_id, transport=transport,
                                 scheduling="sentence_window" if transport == "websocket" else "playback_window",
                                 emotion_mode="off", timeout_ms=30000)
        try:
            result = await cancellation_case(client, before_audio=False)
            if transport == "websocket":
                assert result["next_completed_segments"] > 0, "标准传输缺少可信分段回执"
            report[transport] = result
            report[transport]["playback_gate"] = await playback_gate_case(client)
            print(json.dumps({transport: result}, ensure_ascii=False), flush=True)
        finally:
            await client.close()


async def main(args):
    saved_path = ROOT / ".local/cache/reports/tts-integration/results.json"
    report = json.loads(saved_path.read_text(encoding="utf-8")) if args.design and saved_path.is_file() else {}
    report["fields"] = {"before_audio": "首音频前取消", "cached_bytes": "取消前已生成字节",
              "cancel_confirmed": "应用层取消确认", "cancel_ms": "应用层取消毫秒",
              "old_session_close_ms": "旧会话关闭毫秒", "native_release_confirmed": "原生执行锁与上下文释放确认，在线为无此机制",
              "next_first_audio_ms": "取消至下一请求首音频毫秒",
              "next_pcm_bytes": "新请求完整音频字节", "next_completed_segments": "客户端正文块完成回执数，仅标准传输与本地",
              "next_server_boundary_events": "双向流式的服务端边界事件数，不表示客户端文本块回执"}
    report["fields"].update(held_pcm_bytes="播放许可关闭时缓存字节", locked_chars_before_release="授权前锁定字数",
                            silent_before_release="授权前回调静音", audio_after_release="授权后回调顺序读取音频")
    report["fields"].update(mixed="是否混合设计", duration_ms="样音时长毫秒", target_asset_id="生成目标样音标识",
                            sample_cancellation="完整样音取消与后续正式请求交接", late_audio_discarded="迟到样音未进入播放队列",
                            sample_steps="样音实际制作步数", formal_steps="正式播报实际设置步数",
                            sample_path="用于本机内容核对的原生样音路径，仅诊断报告包含",
                            preview_endpoint="试音实际调用的原生接口",
                            formal_uses_target_sample="正式克隆使用已保存目标样音", formal_pcm_bytes="正式目标样音克隆返回的完整音频字节",
                            high_fidelity_rate="高保真旁路副本的原生采样率", high_fidelity_bytes="高保真旁路副本字节数")
    try:
        if args.local:
            await local(report)
        elif args.design:
            await local(report, design_only=True)
        if args.online:
            await online(report)
    finally:
        path = ROOT / ".local/cache/reports/tts-integration/results.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true", help="local（本地模型真实验证）")
    parser.add_argument("--online", action="store_true", help="online（在线真实验证，会产生少量合成用量）")
    parser.add_argument("--design", action="store_true", help="design（仅验证二代纯设计与混合样音，保留其他验证记录）")
    asyncio.run(main(parser.parse_args()))
