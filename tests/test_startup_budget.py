"""Startup admission（启动放行）：资源预留、低资源串行及并行分支的生命周期。"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from voice_terminal.application.startup_budget import GIB, StartupResources, parallel_admission
from voice_terminal.application.supervisor import ApplicationHost

AMPLE = StartupResources(16 * GIB, 32 * GIB, 20, 10, 8 * GIB)
LOW = StartupResources(2 * GIB, 32 * GIB, 20, 10, 8 * GIB)


@pytest.fixture(autouse=True)
def clear_startup_override(monkeypatch):
    monkeypatch.delenv("VOICE_STARTUP_PARALLEL", raising=False)


@pytest.mark.parametrize("resources, expected", [
    (AMPLE, True), (LOW, False), (StartupResources(None, None, 20, 10, None), False),
    (StartupResources(16 * GIB, 32 * GIB, 4, 10, 8 * GIB), False),
    (StartupResources(16 * GIB, 32 * GIB, 20, 90, 8 * GIB), False),
    (StartupResources(16 * GIB, 4 * GIB, 20, 10, 8 * GIB), False),
    (StartupResources(16 * GIB, 32 * GIB, 20, 10, 4 * GIB), False),
    (StartupResources(16 * GIB, 32 * GIB, 20, 10, None), False),
])
def test_parallel_budget_reserves_both_loading_peaks(resources, expected):
    allowed, _ = parallel_admission(resources, "analysis", {"asr": (0, 0)}, gpu_roles={"asr", "analysis"}, threads={})
    assert allowed is expected


def test_cpu_models_do_not_need_gpu_sampling_and_used_memory_is_not_reserved_twice():
    resources = StartupResources(5 * GIB, 12 * GIB, 20, 10)
    allowed, _ = parallel_admission(resources, "analysis", {"asr": (3 * GIB, 7 * GIB)}, gpu_roles=set(), threads={})
    assert allowed
    assert not parallel_admission(resources, "analysis", {"asr": (0, 0)}, gpu_roles=set(), threads={})[0]
    assert parallel_admission(LOW, "asr", {}, gpu_roles=set(), threads={})[0]


def test_explicit_thread_budget_can_force_serial_even_on_many_cores():
    assert not parallel_admission(AMPLE, "analysis", {"asr": (0, 0)},
                                  gpu_roles=set(), threads={"asr": 16, "analysis": 8})[0]


def test_early_dsh_and_three_model_lanes_join_before_session_and_full_readiness(tmp_path):
    async def run():
        host = ApplicationHost(tmp_path)
        host.manifest["startup_gpu_roles"] = ["asr", "analysis", "voice"]
        entered = {key: asyncio.Event() for key in ("asr", "analysis", "voice", "dsh", "session")}
        release = {key: asyncio.Event() for key in ("asr", "analysis", "voice", "dsh")}
        completed = set()
        peak, loading = 0, 0
        async def setup():
            host.state.component("setup", state="ready")
        async def entry():
            host.voice_entry_ready = True
            entered["dsh"].set()
        async def start(key):
            nonlocal peak, loading
            assert entered["dsh"].is_set()
            loading += 1
            peak = max(peak, loading)
            entered[key].set()
            await release[key].wait()
            loading -= 1
            completed.add(key)
            host.state.component(key, state="ready")
        async def local():
            await start("voice")
            host.voice_local_ready = True
        async def session():
            assert completed == {"asr", "analysis", "voice"}
            entered["session"].set()
            host.state.update(core_available=True)
        async def delegated():
            await release["dsh"].wait()
        try:
            with patch.object(host, "setup", setup), patch.object(host, "start_service", start), \
                 patch.object(host, "start_voice_entry", entry), patch.object(host, "start_voice_local", local), \
                 patch.object(host, "finish_voice_startup", session), patch.object(host, "start_delegated", delegated), \
                 patch.object(host, "watch_delegated_startup", new=AsyncMock()), \
                 patch("voice_terminal.application.startup_budget.sample_startup_resources", return_value=AMPLE):
                task = asyncio.create_task(host.startup())
                await asyncio.wait_for(entered["voice"].wait(), 3)
                assert peak == 3 and all(entered[key].is_set() for key in ("asr", "analysis", "dsh"))
                assert not entered["session"].is_set()
                release["asr"].set()
                await asyncio.sleep(0)
                assert not entered["session"].is_set()
                release["analysis"].set()
                await asyncio.sleep(.1)
                assert not entered["session"].is_set()
                release["voice"].set()
                await asyncio.wait_for(entered["session"].wait(), 2)
                assert not host.state.snapshot()["startup_complete"]
                release["dsh"].set()
                await asyncio.wait_for(task, 2)
                assert host.state.snapshot()["startup_complete"]
        finally:
            host.outer_job.close()
    asyncio.run(run())


def test_three_lanes_are_admitted_with_budget_and_fourth_is_always_blocked():
    active = {"asr": (0, 0), "analysis": (0, 0)}
    assert parallel_admission(AMPLE, "voice", active, gpu_roles={"asr", "analysis", "voice"}, threads={})[0]
    assert not parallel_admission(LOW, "voice", active, gpu_roles=set(), threads={})[0]
    active["voice"] = (0, 0)
    allowed, reason = parallel_admission(AMPLE, "asr", active, gpu_roles=set(), threads={})
    assert not allowed and "通道" in reason


def test_low_memory_queues_analysis_until_asr_finishes(tmp_path):
    async def run():
        host = ApplicationHost(tmp_path)
        release = asyncio.Event()
        entered = []
        async def start(key):
            entered.append(key)
            if key == "asr":
                await release.wait()
        try:
            with patch.object(host, "start_service", start), \
                 patch("voice_terminal.application.startup_budget.sample_startup_resources", return_value=LOW):
                task = asyncio.create_task(host.start_model_services())
                async def queued():
                    while "可用内存不足" not in next(c for c in host.state.snapshot()["components"] if c["id"] == "analysis")["phase"]:
                        await asyncio.sleep(.01)
                await asyncio.wait_for(queued(), 2)
                assert entered == ["asr"]
                row = next(c for c in host.state.snapshot()["components"] if c["id"] == "analysis")
                assert row["state"] == "waiting" and row["pid"] is None and "等待资源" in row["phase"]
                release.set()
                await asyncio.wait_for(task, 2)
                assert entered == ["asr", "analysis"]
        finally:
            host.outer_job.close()
    asyncio.run(run())


def test_exit_cancels_loading_and_never_spawns_queued_component(tmp_path):
    async def run():
        host = ApplicationHost(tmp_path)
        entered, cancelled = [], asyncio.Event()
        async def start(key):
            entered.append(key)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        try:
            with patch.object(host, "start_service", start), \
                 patch("voice_terminal.application.startup_budget.sample_startup_resources", return_value=LOW):
                task = asyncio.create_task(host.start_model_services())
                await asyncio.sleep(.1)
                host.state.update(stopping=True)
                await asyncio.wait_for(task, 2)
                assert cancelled.is_set() and entered == ["asr"]
                assert not host.state.snapshot()["startup_complete"]
        finally:
            host.outer_job.close()
    asyncio.run(run())


def test_one_parallel_failure_does_not_cancel_other_model(tmp_path):
    async def run():
        host = ApplicationHost(tmp_path)
        analysis_entered, release_asr, release_analysis = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def start(key):
            if key == "asr":
                await release_asr.wait()
                raise RuntimeError("模拟模型不可用")
            analysis_entered.set()
            await release_analysis.wait()
            host.state.component(key, state="ready")
        try:
            with patch.object(host, "start_service", start), \
                 patch("voice_terminal.application.startup_budget.sample_startup_resources", return_value=AMPLE):
                task = asyncio.create_task(host.start_model_services())
                await asyncio.wait_for(analysis_entered.wait(), 2)
                release_asr.set()
                await asyncio.sleep(.1)
                assert not task.done()
                release_analysis.set()
                await asyncio.wait_for(task, 2)
                rows = {c["id"]: c for c in host.state.snapshot()["components"]}
                assert rows["asr"]["state"] == "failed" and rows["analysis"]["state"] == "ready"
        finally:
            host.outer_job.close()
    asyncio.run(run())


def test_resources_released_during_loading_can_admit_second_model(tmp_path):
    async def run():
        host = ApplicationHost(tmp_path)
        release = asyncio.Event()
        analysis_entered = asyncio.Event()
        async def start(key):
            if key == "analysis":
                analysis_entered.set()
            await release.wait()
        try:
            with patch.object(host, "start_service", start), \
                 patch("voice_terminal.application.startup_budget.sample_startup_resources", side_effect=[LOW, AMPLE]):
                task = asyncio.create_task(host.start_model_services())
                await asyncio.wait_for(analysis_entered.wait(), 3)
                assert not task.done()
                release.set()
                await asyncio.wait_for(task, 2)
        finally:
            host.outer_job.close()
    asyncio.run(run())
