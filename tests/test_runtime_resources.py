"""Resource contract（资源契约）：独立线程、去重计量和原生完成租约。"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import patch

import pytest

from voice_terminal.resources import ResourcePolicy, ResourceRegistry, aggregate_resource_status, registry
from voice_terminal.analysis.worker import AnalysisLane
from voice_terminal.audio.shared import SharedAudio, SharedAudioReader, capabilities


def test_policy_does_not_change_legacy_threads_and_overrides_only_selected_role():
    original = {"OPENBLAS_NUM_THREADS": "20", "KEEP": "value"}
    assert ResourcePolicy({}).environment("voice", parent=original)["OPENBLAS_NUM_THREADS"] == "20"
    policy = ResourcePolicy({"voice": 4, "speaker": 2})
    assert policy.environment("voice", parent=original)["OMP_NUM_THREADS"] == "4"
    assert policy.environment("speaker", parent=original)["OMP_NUM_THREADS"] == "2"
    assert policy.environment("asr", parent=original)["OPENBLAS_NUM_THREADS"] == "20"
    assert original == {"OPENBLAS_NUM_THREADS": "20", "KEEP": "value"}


@pytest.mark.parametrize("data", [{"native_threads": {"voice": True}}, {"native_threads": {"bad": 4}},
                                  {"shared_audio": "yes"}, {"history_cache_bytes": -1}, {"vad_backend": "unknown"}])
def test_policy_rejects_invalid_budget(tmp_path, data):
    path = tmp_path / "policy.json"; path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        ResourcePolicy.load(path)


def test_leases_count_shared_bytes_once_and_release_idempotently():
    resources = ResourceRegistry(); pcm = bytes(32000)
    first = resources.lease("first", pcm); second = resources.lease("second", pcm)
    assert resources.snapshot()["leased_bytes"] == 32000
    assert resources.snapshot()["active_leases"] == 2
    first.close(); first.close()
    assert resources.snapshot()["leased_bytes"] == 32000
    second.close()
    assert resources.snapshot()["leased_bytes"] == 0


def test_two_analysis_lanes_do_not_double_count_the_process():
    row = {"pid": 12, "created_at": 123.0, "private_commit_bytes": 100,
           "rss_bytes": 50, "thread_count": 4, "managed_cache_bytes": 10, "active_leases": 2,
           "gpu_dedicated_bytes": 200}
    result = aggregate_resource_status([row, dict(row)])
    assert result["private_commit_bytes"] == 100
    assert result["gpu_dedicated_bytes"] == 200
    assert result["active_leases"] == 2
    assert len(result["processes"]) == 1
    partial = aggregate_resource_status([row], missing_roles=["analysis"])
    assert partial["private_commit_bytes"] is None
    assert partial["gpu_dedicated_bytes"] is None
    assert partial["complete"] is False and partial["missing_roles"] == ["analysis"]


def test_native_lease_survives_request_cancellation():
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        def model(pcm):
            entered.set(); release.wait(3)
            assert pcm == b"\0\0" * 160
            return {"ok": True}
        before = registry.snapshot()["active_leases"]
        lane = AnalysisLane(model)
        task = asyncio.create_task(lane.run(b"\0\0" * 160, "cancelled"))
        try:
            while not entered.is_set():
                await asyncio.sleep(.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert registry.snapshot()["active_leases"] == before + 1
            with pytest.raises(RuntimeError, match="busy"):
                await lane.run(b"\0\0", "next")
            release.set()
            while lane.active is not None:
                await asyncio.sleep(.001)
            assert registry.snapshot()["active_leases"] == before
        finally:
            release.set(); lane.close()
    asyncio.run(scenario())


def test_shared_audio_signature_generation_and_native_lifetime(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("共享音频默认只对 Windows（操作系统）管理进程启用")
    path = tmp_path / "policy.json"; path.write_text('{"shared_audio":true}')
    monkeypatch.setenv("VOICE_RESOURCE_POLICY", str(path))
    monkeypatch.setenv("VOICE_APP_INSTANCE_ID", "test-instance")
    monkeypatch.setenv("VOICE_APP_INTERNAL_TOKEN", "test-secret")
    monkeypatch.setenv("VOICE_APP_SERVICE_GENERATION", "test-generation")
    owner = SharedAudio(b"\x01\0" * 160, capabilities(), "request")
    try:
        bad = {**owner.reference, "size": 100}
        with pytest.raises(ValueError, match="签名"):
            SharedAudioReader(bad, "request", 1024)
        with pytest.raises(ValueError, match="请求"):
            SharedAudioReader(owner.reference, "other-request", 1024)
        reader = SharedAudioReader(owner.reference, "request", 1024)
        owner.close()  # 创建方断开；读映射仍拥有真实页面。
        assert bytes(reader.view) == b"\x01\0" * 160
        assert reader.view.readonly
        reader.close(); reader.close()
        with pytest.raises(FileNotFoundError):
            SharedAudioReader(owner.reference, "request", 1024)
    finally:
        owner.close()


def test_thread_policy_is_applied_before_numpy_import(tmp_path):
    path = tmp_path / "policy.json"; path.write_text('{"native_threads":{"voice":4}}')
    code = "from voice_terminal.resources import bootstrap_resources; bootstrap_resources('voice'); import numpy; import os; print(os.environ['OPENBLAS_NUM_THREADS'])"
    result = subprocess.run([sys.executable, "-c", code], env={**os.environ, "VOICE_RESOURCE_POLICY": str(path)},
                            capture_output=True, text=True, check=True, timeout=30)
    assert result.stdout.strip() == "4"


def test_direct_speaker_module_sets_budget_before_parent_package_import(tmp_path):
    path = tmp_path / "policy.json"; path.write_text('{"native_threads":{"speaker":4}}')
    (tmp_path / "sitecustomize.py").write_text("""
import builtins, os
original = builtins.__import__
def checked(name, *args, **kwargs):
    if name == 'numpy':
        assert os.environ.get('OPENBLAS_NUM_THREADS') == '4', '数值库导入前线程预算未设置'
    return original(name, *args, **kwargs)
builtins.__import__ = checked
""", encoding="utf-8")
    env = {**os.environ, "VOICE_RESOURCE_POLICY": str(path),
           "PYTHONPATH": os.pathsep.join((str(tmp_path), str(Path.cwd()), os.environ.get("PYTHONPATH", "")))}
    result = subprocess.run([sys.executable, "-m", "voice_terminal.speaker.worker", "--help"],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_voice_hardware_status_never_imports_torch(monkeypatch):
    from voice_terminal import compute
    cached = {"models": {}, "gpu": {}, "backends": {}, "system_memory": {}}
    monkeypatch.setenv("VOICE_RESOURCE_ROLE", "voice")
    monkeypatch.setenv("VOICE_HARDWARE_CAPABILITIES", json.dumps(cached))
    monkeypatch.setattr(compute, "_capabilities", None)
    with patch.object(compute, "_backend_hardware_status", side_effect=AssertionError("不应在主服务加载后端")):
        assert compute.hardware_status()["models"] == {}


def test_conversion_keys_preserve_amplitude_precision_and_sample_range():
    import numpy as np
    from voice_terminal.audio.allocation import AudioAllocation
    pcm = np.array([-32768, -120, 0, 150, 32767], dtype="<i2").tobytes()
    allocation = AudioAllocation(pcm)
    raw = allocation.samples(sample_rate=16000, precision="float64", amplitude="int16")
    unit = allocation.samples(sample_rate=16000, precision="float32", amplitude="normalized")
    assert np.array_equal(raw, np.frombuffer(pcm, "<i2").astype(np.float64))
    assert np.array_equal(unit, np.frombuffer(pcm, "<i2").astype(np.float32) / 32768)
    assert allocation.samples(sample_rate=16000, precision="float64", amplitude="int16") is raw
    part = allocation.samples(sample_rate=16000, precision="float64", amplitude="int16", start=1, end=4)
    assert np.array_equal(part, raw[1:4]) and part is not raw
    with pytest.raises(ValueError, match="重采样"):
        allocation.samples(sample_rate=8000, precision="float64", amplitude="int16")
    allocation.close()
    assert not allocation.converted


def test_audio_range_reuse_remains_bounded_and_invalidates_on_append(tmp_path, monkeypatch):
    from voice_terminal.audio.ring_buffer import PcmRingBuffer
    from voice_terminal.controller.turn_workspace import CurrentTurnAudioBuffer, AudioFrame
    path = tmp_path / "policy.json"; path.write_text('{"shared_audio":true}')
    monkeypatch.setenv("VOICE_RESOURCE_POLICY", str(path))
    ring = PcmRingBuffer(12)
    for chunk in (b"abcd", b"efgh", b"ijkl"):
        ring.append(chunk)
    first = ring.read(5)
    assert first == b"hijkl" and ring.read(5) is first
    ring.append(b"mnop")
    assert ring.read(5) == b"lmnop"
    assert ring.managed_bytes <= ring.max_bytes * 2
    ring.clear(); assert ring.managed_bytes == 0
    turn = CurrentTurnAudioBuffer(max_samples=4)
    turn.append(AudioFrame(b"aabb", 0, 0.0))
    first = turn.tail(); assert turn.tail() is first
    turn.append(AudioFrame(b"ccdd", 2, 0.0))
    assert turn.tail() == b"aabbccdd" and turn.tail() is not first


@pytest.mark.parametrize("crash", [False, True])
def test_shared_mapping_survives_owner_close_until_receiver_finishes_or_dies(tmp_path, monkeypatch, crash):
    if os.name != "nt":
        pytest.skip("Windows（操作系统）共享映射生命周期")
    path = tmp_path / "policy.json"; path.write_text('{"shared_audio":true}')
    for key, value in {"VOICE_RESOURCE_POLICY": str(path), "VOICE_APP_INSTANCE_ID": "test-instance",
                       "VOICE_APP_INTERNAL_TOKEN": "test-secret", "VOICE_APP_SERVICE_GENERATION": "host"}.items():
        monkeypatch.setenv(key, value)
    child_code = """
import json, sys
from voice_terminal.audio.shared import SharedAudioReader, capabilities
print(json.dumps(capabilities()), flush=True)
reader = SharedAudioReader(json.loads(sys.stdin.readline()), 'request', 1024)
print('opened', flush=True)
sys.stdin.readline()
assert bytes(reader.view) == b'\\x01\\0' * 160
reader.close()
print('finished', flush=True)
"""
    async def scenario():
        import psutil
        from multiprocessing.shared_memory import SharedMemory
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", child_code,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "VOICE_APP_SERVICE_GENERATION": "receiver"})
        owner = None
        try:
            # Windows（操作系统）的虚拟环境启动器可能另起业务进程；必须用工作进程报告的身份。
            peer = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
            owner = SharedAudio(b"\x01\0" * 160, peer, "request")
            process.stdin.write((json.dumps(owner.reference) + "\n").encode()); await process.stdin.drain()
            assert (await asyncio.wait_for(process.stdout.readline(), 10)).strip() == b"opened"
            name = owner.memory.name; owner.close()
            probe = SharedMemory(name=name); probe.close()  # 原生接收进程仍持有映射。
            if crash:
                psutil.Process(peer["pid"]).kill()
            else:
                process.stdin.write(b"inspect\n"); await process.stdin.drain()
                assert (await asyncio.wait_for(process.stdout.readline(), 5)).strip() == b"finished"
            await asyncio.wait_for(process.wait(), 5)
            if not crash:
                assert process.returncode == 0, (await process.stderr.read()).decode()
            with pytest.raises(FileNotFoundError):
                SharedMemory(name=name)
        finally:
            if owner:
                owner.close()
            if process.returncode is None:
                process.kill(); await process.wait()
    asyncio.run(scenario())
