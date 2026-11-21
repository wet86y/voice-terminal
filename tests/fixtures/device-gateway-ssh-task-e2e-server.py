"""Isolated Python device gateway（设备网关）for the Rust SSH task-channel integration test."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


class FixtureCapture:
    """FixtureCapture（媒体集成测试中的无硬件采集端口）。"""

    def __init__(self) -> None:
        self.running = False  # running（虚拟采集端口状态）。
        self.diagnostics: dict[str, object] = {}  # diagnostics（虚拟采集诊断快照）。

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    async def read_frame(self):
        await asyncio.Future()

    def pending_frame_count(self) -> int:
        return 0

    def read_pending_frame(self):
        raise asyncio.QueueEmpty

    def preroll(self, _milliseconds: int) -> bytes:
        return b""


class ClockResolutionYield:
    """ClockResolutionYield（测试专用调度间隔）：适配 Windows 粗粒度单调时钟分辨率。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        async def delayed_send(message) -> None:
            await send(message)
            if scope.get("type") == "websocket" and message.get("type") == "websocket.send":
                text = message.get("text")
                if isinstance(text, str):
                    try:
                        body = json.loads(text)
                    except ValueError:
                        return
                    if body.get("type") == "clock.sync.request":
                        await asyncio.sleep(0.02)

        await self.app(scope, receive, delayed_send)


class SessionPollCounter:
    """SessionPollCounter（只统计测试网关收到的任务轮询请求）。"""

    def __init__(self, app, state: dict[str, object]) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") == "http" and scope.get("path") == "/v1/session/poll":
            self.state["session_poll_count"] = int(self.state.get("session_poll_count", 0)) + 1
        await self.app(scope, receive, send)


def load_product_api_tests():
    """Load the existing product API fixture without making tests a package."""
    path = ROOT / "tests" / "test_product_api.py"
    spec = importlib.util.spec_from_file_location("voice_terminal_product_api_fixture", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载产品 API 测试夹具。")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ProductApiTests


async def main() -> None:
    device_id = os.environ["VOICE_DEVICE_E2E_ID"]
    public_key = os.environ["VOICE_DEVICE_E2E_PUBLIC_KEY"]
    gateway_port = int(os.environ.get("VOICE_DEVICE_E2E_GATEWAY_PORT", "18765"))
    control_port = int(os.environ.get("VOICE_DEVICE_E2E_CONTROL_PORT", "18766"))
    test_case = load_product_api_tests()("test_device_gateway_status_is_versioned_and_contains_no_private_snapshot")
    test_case.setUp()
    # 该回环测试只使用 ProductApiTests 自建的临时目录；此处模拟桌面原生信任写入，以覆盖旧版网关兼容接口。
    # 正式服务仍以只读 DeviceTrustStore（设备信任存储）视图运行，真实配对由 Rust（原生层）写入。
    assert test_case.runtime.device_trust_store is not None
    test_case.runtime.device_trust_store._read_only = False
    test_case.runtime.device_host_id = os.environ.get("VOICE_TERMINAL_HOST_ID")
    # 复用正式端口门禁，不替换路由鉴权或白名单实现。
    test_case.runtime._device_gateway_port = gateway_port
    control_app = FastAPI()
    state: dict[str, object] = {}
    from voice_terminal.audio.player import PcmPlayer
    from voice_terminal.devices.capture_router import SelectableCapturePort

    try:
        test_case.runtime.device_trust_store.approve(device_id, public_key, trusted_at_ms=1_000)
        decoded_opus_frames: list[bytes] = []

        async def pairing_open() -> JSONResponse:
            """pairing_open（回环测试配对窗口）：撤销初始测试信任，验证未经批准不能执行业务。"""
            test_case.runtime.device_trust_store.revoke(device_id)
            code, _expires = test_case.runtime.device_pairing_manager.open_window(ttl_seconds=120)
            return JSONResponse({"pairing_code": code})

        async def pairing_approve() -> JSONResponse:
            """pairing_approve（测试主机明确批准）：仅批准当前唯一测试申请，不接收替换公钥。"""
            manager = test_case.runtime.device_pairing_manager
            pending = manager.status()["pending_requests"]
            if len(pending) != 1 or pending[0]["device_id"] != device_id:
                raise HTTPException(status_code=409, detail="须有且仅有当前测试设备的申请。")
            manager.approve(pending[0]["request_id"])
            return JSONResponse(manager.status())

        async def pairing_revoke() -> JSONResponse:
            """pairing_revoke（测试主机撤销）：只修改本次隔离夹具的信任记录。"""
            test_case.runtime.device_trust_store.revoke(device_id)
            return JSONResponse(test_case.runtime.device_pairing_manager.status())

        class FixtureAudioMeter:
            def feed(self, direction: str, pcm: bytes, sample_rate: int, channels: int) -> None:
                if direction != "input" or sample_rate != 16_000 or channels != 1:
                    raise AssertionError("Opus 上行应解码为 16 kHz 单声道 PCM 输入")
                decoded_opus_frames.append(pcm)

        test_case.runtime.audio_meter = FixtureAudioMeter()
        capture_route = SelectableCapturePort(FixtureCapture())
        test_case.runtime._capture_router = capture_route
        await capture_route.start()
        test_case.runtime.state.listening = True

        class FixtureController:
            """FixtureController（提供可切换媒体端点要求的采集控制）。"""

            async def stop_listening(self) -> None:
                test_case.runtime.state.listening = False
                await capture_route.stop()

            async def start_listening(self) -> None:
                await capture_route.start()
                test_case.runtime.state.listening = True

        test_case.runtime.controller = FixtureController()
        player = PcmPlayer(sample_rate=16_000, channels=1, output_device=None)
        player.configure_remote_output(
            enqueue=test_case.runtime._enqueue_player_audio,
            is_bound=test_case.runtime.remote_playback_is_bound,
            progress=test_case.runtime.remote_playback_snapshot,
            buffered_ms=test_case.runtime.remote_playback_buffered_ms,
            wait_until_complete=test_case.runtime.wait_remote_playback_output,
            finish_stream=test_case.runtime.finish_remote_playback,
            transactions=test_case.runtime.remote_playback_transactions,
        )
        test_case.runtime.player = player

        async def dispatch_stop() -> JSONResponse:
            if "stop_task" in state:
                raise HTTPException(status_code=409, detail="停止任务已派发。")
            from voice_terminal.devices.playback import RemotePlaybackTransactions

            router = test_case.runtime.device_router
            if router is None or test_case.runtime.device_remote_sessions is None:
                raise HTTPException(status_code=503, detail="远端设备任务路由未就绪。")
            connection = test_case.runtime.device_registry.connection(device_id)
            if connection is None or not connection.connected:
                raise HTTPException(status_code=409, detail="Rust SSH 设备会话尚未注册。")
            state["screen_task"] = asyncio.create_task(router.execute(
                request_id="screen-while-stopping", device_id=device_id, operation="screen.capture_file",
                arguments={}, timeout_seconds=15,
            ))
            state["stop_task"] = asyncio.create_task(
                RemotePlaybackTransactions(router, device_id, timeout_seconds=15)
                .stop_and_snapshot_confirmed("speech-playback-e2e")
            )
            return JSONResponse({"result": "accepted", "fields": {"result": "停止任务已登记"}})

        async def dispatch_replacement() -> JSONResponse:
            if "replacement_task" in state:
                raise HTTPException(status_code=409, detail="测试替换事务已登记。")
            from types import SimpleNamespace
            from voice_terminal.devices.playback import RemotePlaybackTransactions
            from voice_terminal.devices import playback as playback_module
            router = test_case.runtime.device_router
            if router is None or test_case.runtime.device_remote_sessions is None:
                raise HTTPException(status_code=503, detail="远端设备任务路由未就绪。")
            connection = test_case.runtime.device_registry.connection(device_id)
            if connection is None or not connection.connected:
                raise HTTPException(status_code=409, detail="Rust SSH 设备会话尚未注册。")

            request_ids = iter(("replace-a", "bind-a", "wait-a"))
            playback_module.uuid4 = lambda: SimpleNamespace(hex=next(request_ids))

            async def run_replacement() -> dict[str, object]:
                transactions = RemotePlaybackTransactions(router, device_id, timeout_seconds=15)
                ticket = await transactions.prepare_replacement_transaction("speech-old", max_ms=300)
                bound = await transactions.bind_replacement_transaction(ticket, "speech-new")
                result = await transactions.wait_replacement_transaction(ticket)
                return {"ticket": ticket.to_dict(), "bound": bound, "result": result.to_dict()}

            state["replacement_task"] = asyncio.create_task(run_replacement())
            return JSONResponse({"result":"accepted","fields":{"result":"替换事务已登记"}})

        async def dispatch_receipt_reconciliation() -> JSONResponse:
            if "receipt_reconciliation_task" in state:
                raise HTTPException(status_code=409, detail="回执恢复任务已登记。")
            router = test_case.runtime.device_router
            if router is None:
                raise HTTPException(status_code=503, detail="设备任务台账未就绪。")
            request_id = "receipt-reconcile-e2e"
            state["receipt_reconciliation_request_id"] = request_id
            state["receipt_reconciliation_task"] = asyncio.create_task(router.execute(
                request_id=request_id, device_id=device_id,
                operation="playback.pause",
                arguments={"playback_key":"speech-reconcile-e2e"}, timeout_seconds=30,
            ))
            return JSONResponse({"result":"accepted","task_id":request_id,
                "fields":{"result":"测试原任务已登记","task_id":"固定测试任务编号"}})

        async def receipt_reconciliation_status() -> JSONResponse:
            task = state.get("receipt_reconciliation_task")
            request_id = state.get("receipt_reconciliation_request_id")
            router = test_case.runtime.device_router
            if task is None or not isinstance(request_id, str) or router is None:
                raise HTTPException(status_code=404, detail="尚无测试原任务。")
            if not task.done():
                return JSONResponse({"state":"pending","fields":{"state":"原任务台账状态"}}, status_code=202)
            try:
                task.result()
            except Exception:
                pass  # 已断开任务预期先成为 unknown，随后由原回执恢复。
            record = router.journal.get(request_id)
            if record is None:
                raise HTTPException(status_code=404, detail="测试原任务记录不存在。")
            return JSONResponse({"state":record.status,"task_id":record.request_id,
                "result":record.result,"run_id":record.run_id,
                "fields":{"state":"原持久任务状态","task_id":"原持久任务编号",
                          "result":"原任务结果","run_id":"原权威核心运行编号"}})

        async def replacement_status() -> JSONResponse:
            task = state.get("replacement_task")
            if task is None:
                raise HTTPException(status_code=404, detail="尚无测试替换事务。")
            if not task.done():
                return JSONResponse({"state":"pending","fields":{"state":"替换事务状态"}}, status_code=202)
            try:
                value = task.result()
            except Exception as error:
                return JSONResponse({"state":"unknown","error":str(error),
                    "fields":{"state":"替换事务状态","error":"替换事务失败或结果未知原因"}}, status_code=500)
            return JSONResponse({"state":"succeeded","replacement":value,
                "fields":{"state":"替换事务状态","replacement":"原替换编号的票据、绑定与结算回执"}})

        async def stop_status() -> JSONResponse:
            task = state.get("stop_task")
            if task is None:
                raise HTTPException(status_code=404, detail="尚无停止任务。")
            if not task.done():
                return JSONResponse({"state": "pending", "fields": {"state": "停止任务状态"}}, status_code=202)
            try:
                snapshot = task.result()
            except Exception as error:
                return JSONResponse({"state": "failed", "error": str(error),
                                     "fields": {"state": "停止任务状态", "error": "停止任务失败原因"}},
                                    status_code=500)
            return JSONResponse({"state": "succeeded", "snapshot": {
                "playback_key": snapshot.key,
                "enqueued_ms": snapshot.enqueued_ms,
                "played_ms": snapshot.played_ms,
                "buffered_ms": snapshot.buffered_ms,
                "output_estimated_ms": snapshot.output_estimated_ms,
                "started": snapshot.started,
                "device_time_us": snapshot.device_time_us,
                "precision": snapshot.precision,
            }, "fields": {"state": "停止任务状态", "snapshot": "原播放键的设备快照"}})

        async def device_generation() -> JSONResponse:
            return JSONResponse({"generation": test_case.runtime.device_registry.generation,
                                 "fields": {"generation": "主机当前设备清单代次"}})

        async def media_capture_status() -> JSONResponse:
            return JSONResponse({
                "frames": len(decoded_opus_frames),
                "sample_bytes": sum(len(pcm) for pcm in decoded_opus_frames),
                "fields": {"frames": "已解码并送入远端采集端口的 PCM 帧数",
                           "sample_bytes": "交给音频计量回调的 PCM 字节数"},
            })

        async def session_poll_count() -> JSONResponse:
            return JSONResponse({"count": int(state.get("session_poll_count", 0)),
                                 "fields": {"count": "隔离测试服务收到的设备任务轮询请求数"}})

        async def enqueue_tts() -> JSONResponse:
            if "tts_task" in state:
                raise HTTPException(status_code=409, detail="测试 TTS 播放任务已经登记。")
            playback_key = "ssh-media-tts-e2e"
            pcm = b"\x2a\x00" * 320  # pcm（模拟 TTS（语音合成）得到的 20 毫秒 PCM 音频）。
            player.allow(playback_key)

            async def publish() -> None:
                await player.enqueue_async(playback_key, pcm)
                await player.finish_stream_async(playback_key)
                await player.wait_until_output_complete(timeout_seconds=15)

            state["tts_task"] = asyncio.create_task(publish())
            return JSONResponse({"result": "accepted", "playback_key": playback_key,
                                 "fields": {"result": "测试 TTS 播放登记结果",
                                             "playback_key": "固定播放事务编号"}})

        async def tts_status() -> JSONResponse:
            task = state.get("tts_task")
            if task is None:
                raise HTTPException(status_code=404, detail="尚无测试 TTS 播放任务。")
            if not task.done():
                return JSONResponse({"state": "pending", "fields": {"state": "TTS 播放结算状态"}},
                                    status_code=202)
            try:
                task.result()
            except Exception as error:
                return JSONResponse({"state": "unknown", "error": str(error),
                                     "fields": {"state": "TTS 播放结算状态",
                                                 "error": "测试播放等待失败原因"}}, status_code=500)
            return JSONResponse({"state": "succeeded", "progress": player.progress("ssh-media-tts-e2e"),
                                 "fields": {"state": "TTS 播放结算状态",
                                             "progress": "设备最后一次可信播放进度"}})

        async def shutdown() -> JSONResponse:
            server = state.get("server")
            if server is not None:
                server.should_exit = True
            control_server = state.get("control_server")
            if control_server is not None:
                control_server.should_exit = True
            return JSONResponse({"result": "stopping", "fields": {"result": "仅测试服务的退出状态"}})

        control_app.add_api_route("/__test__/pairing-open", pairing_open, methods=["POST"])
        control_app.add_api_route("/__test__/pairing-approve", pairing_approve, methods=["POST"])
        control_app.add_api_route("/__test__/pairing-revoke", pairing_revoke, methods=["POST"])
        control_app.add_api_route("/__test__/dispatch-stop", dispatch_stop, methods=["POST"])
        control_app.add_api_route("/__test__/stop-status", stop_status, methods=["GET"])
        control_app.add_api_route("/__test__/dispatch-replacement", dispatch_replacement, methods=["POST"])
        control_app.add_api_route("/__test__/replacement-status", replacement_status, methods=["GET"])
        control_app.add_api_route("/__test__/dispatch-receipt-reconciliation", dispatch_receipt_reconciliation, methods=["POST"])
        control_app.add_api_route("/__test__/receipt-reconciliation-status", receipt_reconciliation_status, methods=["GET"])
        control_app.add_api_route("/__test__/device-generation", device_generation, methods=["GET"])
        control_app.add_api_route("/__test__/media-capture-status", media_capture_status, methods=["GET"])
        control_app.add_api_route("/__test__/session-poll-count", session_poll_count, methods=["GET"])
        control_app.add_api_route("/__test__/tts-enqueue", enqueue_tts, methods=["POST"])
        control_app.add_api_route("/__test__/tts-status", tts_status, methods=["GET"])
        control_app.add_api_route("/__test__/shutdown", shutdown, methods=["POST"])

        gateway_app = ClockResolutionYield(SessionPollCounter(test_case.app, state))
        server = uvicorn.Server(uvicorn.Config(gateway_app, host="127.0.0.1", port=gateway_port, log_level="error"))
        control_server = uvicorn.Server(uvicorn.Config(control_app, host="127.0.0.1", port=control_port, log_level="error"))
        state["server"] = server
        state["control_server"] = control_server
        await asyncio.gather(server.serve(), control_server.serve())
    finally:
        pending = state.get("stop_task")
        if isinstance(pending, asyncio.Task) and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        pending_tts = state.get("tts_task")
        if isinstance(pending_tts, asyncio.Task) and not pending_tts.done():
            pending_tts.cancel()
            await asyncio.gather(pending_tts, return_exceptions=True)
        pending_replacement = state.get("replacement_task")
        if isinstance(pending_replacement, asyncio.Task) and not pending_replacement.done():
            pending_replacement.cancel()
            await asyncio.gather(pending_replacement, return_exceptions=True)
        pending_reconciliation = state.get("receipt_reconciliation_task")
        if isinstance(pending_reconciliation, asyncio.Task) and not pending_reconciliation.done():
            pending_reconciliation.cancel()
            await asyncio.gather(pending_reconciliation, return_exceptions=True)
        if test_case.runtime.device_remote_sessions is not None:
            await test_case.runtime.device_remote_sessions.close_all()
        await capture_route.stop()
        test_case.tearDown()


if __name__ == "__main__":
    asyncio.run(main())
