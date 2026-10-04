"""Application lifecycle tests（应用生命周期测试）：真实进程与独立网页接口。"""
from __future__ import annotations

import asyncio
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import threading
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from voice_terminal.application.state import ApplicationState
from voice_terminal.application.supervisor import ApplicationHost
from voice_terminal.application.web import HostWeb
from voice_terminal.application.windows import Job, ManagedProcess, SingleInstance, external_environment


class ApplicationStateTests(unittest.TestCase):
    def test_replaced_pid_clears_previous_identity_and_metrics(self):
        state = ApplicationState()
        state.component("dsh", state="ready", pid=10, created_at=100,
            memory_rss_bytes=500, memory_private_commit_bytes=2000)
        state.component("dsh", state="ready", pid=11)
        row = next(c for c in state.snapshot()["components"] if c["id"] == "dsh")
        self.assertIsNone(row["created_at"])
        self.assertIsNone(row["memory_rss_bytes"])
        self.assertIsNone(row["memory_private_commit_bytes"])

    def test_optional_failure_is_degraded_and_disabled_is_settled(self):
        state = ApplicationState()
        for row in state.snapshot()["components"]:
            state.component(row["id"], state="ready")
        state.component("voice", state="ready", pid=123)
        state.component("dsh", state="failed", error="代理不可用")
        state.component("speaker", state="disabled", enabled=False)
        state.update(core_available=True)
        state.finish()
        snapshot = state.snapshot()
        self.assertEqual(snapshot["state"], "degraded")
        self.assertEqual(snapshot["completed_components"], snapshot["total_components"])
        self.assertTrue(snapshot["core_available"])
        self.assertTrue(next(c for c in snapshot["components"] if c["id"] == "dsh")["retry_allowed"])
        state.update(stopping=True)
        self.assertFalse(any(c["retry_allowed"] for c in state.snapshot()["components"]))

    def test_core_failure_retains_diagnostic_state(self):
        state = ApplicationState()
        state.component("asr", state="failed", pid=42, memory_rss_bytes=1024)
        state.update(core_available=True)
        state.finish()
        self.assertEqual(state.snapshot()["state"], "failed")
        self.assertFalse(state.snapshot()["core_available"])

    def test_retry_completion_cannot_overwrite_exit_state(self):
        state = ApplicationState()
        state.update(state="stopping", stopping=True)
        state.finish()
        self.assertEqual(state.snapshot()["state"], "stopping")


class WebTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = ApplicationHost(Path(self.directory.name))
        self.web = HostWeb(self.app, Path(self.directory.name), port=0)
        self.app.origin = self.web.origin
        self.web.start()
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def tearDown(self):
        self.web.close()
        self.app.outer_job.close()
        self.directory.cleanup()

    def test_feedback_exists_before_any_business_process(self):
        with self.client.open(self.web.origin) as response:
            self.assertIn("启动诊断", response.read().decode())
        with self.client.open(self.web.origin + "/api/v1/application/snapshot") as response:
            data = json.load(response)
        self.assertEqual(data["state"], "starting")
        self.assertEqual(self.app.processes, {})

    def test_occupied_host_port_uses_a_distinct_owned_listener(self):
        second = HostWeb(self.app, Path(self.directory.name), port=self.web.server.server_port)
        second.start()
        try:
            self.assertNotEqual(second.origin, self.web.origin)
            self.assertNotEqual(second.session, self.web.session)
            with urllib.request.urlopen(second.origin) as response:
                self.assertEqual(response.status, 200)
        finally:
            second.close()

    def test_cookie_and_origin_are_required_for_mutations(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.web.origin + "/api/v1/application/snapshot")
        self.assertEqual(error.exception.code, 403)
        self.client.open(self.web.origin).close()
        request = urllib.request.Request(self.web.origin + "/api/v1/application/exit", b"{}",
            {"Content-Type": "application/json", "Origin": "https://other.example"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.client.open(request)
        self.assertEqual(error.exception.code, 403)
        with patch.object(self.app, "request_exit") as exit_app:
            request = urllib.request.Request(self.web.origin + "/api/v1/application/exit", b"{}",
                {"Content-Type": "application/json", "Origin": self.web.origin})
            self.client.open(request).close()
            exit_app.assert_called_once()

    def test_sse_delivers_terminal_snapshot(self):
        self.client.open(self.web.origin).close()
        self.app.state.update(state="stopped", stopping=True)
        with self.client.open(self.web.origin + "/api/v1/application/events", timeout=3) as response:
            line = response.readline().decode()
            self.assertEqual(json.loads(line[6:])["state"], "stopped")

    def test_internal_reports_do_not_accept_browser_session(self):
        self.client.open(self.web.origin).close()
        request = urllib.request.Request(self.web.origin + "/internal/application/report", b"{}", {"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.client.open(request)
        self.assertEqual(error.exception.code, 403)


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_listening_and_streaming_remain_healthy_without_retry(self):
        """实际监听及播报状态不能被宿主误报为核心能力故障。"""
        app = ApplicationHost(Path.cwd())
        try:
            for row in app.state.snapshot()["components"]:
                app.state.component(row["id"], state="ready")
            app.state.component("voice", pid=123)
            app.state.update(startup_complete=True)
            services = dict(audio="READY", asr="READY", llm="READY", tts="READY", kws="READY")
            for stage, changes in (
                ("standby", {}),  # standby（待机）。
                ("listening", dict(audio="RUNNING", kws="RUNNING")),  # listening（监听）。
                ("speaking", dict(asr="RUNNING", llm="STREAMING", tts="STREAMING")),  # speaking（播报）。
                ("stopped", dict(audio="READY", asr="READY", llm="READY", tts="READY", kws="READY")),
            ):
                with self.subTest(stage=stage):
                    services.update(changes)
                    with patch.object(app, "http", AsyncMock(return_value={"wake_enabled": True, "services": dict(services)})):
                        self.assertTrue(await app.probe("voice"))
                    app.state.finish()
                    snapshot = app.state.snapshot()
                    voice = next(c for c in snapshot["components"] if c["id"] == "voice")
                    self.assertEqual(snapshot["state"], "ready")
                    self.assertTrue(snapshot["core_available"])
                    self.assertEqual(voice["state"], "ready")
                    self.assertEqual(voice["error"], "")
                    self.assertFalse(voice["retry_allowed"])
        finally:
            app.outer_job.close()

    async def test_real_capability_failure_names_the_service_and_recovers(self):
        """真实服务失败保留诊断名称，恢复后清除旧错误。"""
        app = ApplicationHost(Path.cwd())
        try:
            app.state.update(startup_complete=True)
            services = dict(audio="RUNNING", asr="READY", llm="READY", tts="READY", kws="RUNNING")
            for service, label in (("kws", "KWS（关键词唤醒）"), ("asr", "ASR（语音识别）")):
                with self.subTest(service=service):
                    with patch.object(app, "http", AsyncMock(return_value={"wake_enabled": True, "services": {**services, service: "ERROR"}})):
                        self.assertTrue(await app.probe("voice"))
                    failed = app.state.snapshot()
                    voice = next(c for c in failed["components"] if c["id"] == "voice")
                    self.assertFalse(failed["core_available"])
                    self.assertEqual(voice["state"], "failed")
                    self.assertEqual(voice["error"], "核心能力未就绪：" + label)
                    with patch.object(app, "http", AsyncMock(return_value={"wake_enabled": True, "services": services})):
                        self.assertTrue(await app.probe("voice"))
                    recovered = app.state.snapshot()
                    self.assertTrue(recovered["core_available"])
                    self.assertEqual(next(c for c in recovered["components"] if c["id"] == "voice")["error"], "")
        finally:
            app.outer_job.close()

    async def test_disabled_wake_does_not_require_keyword_service(self):
        """唤醒关闭时，停用的关键词服务不阻止直接语音识别。"""
        app = ApplicationHost(Path.cwd())
        try:
            app.state.update(startup_complete=True)
            with patch.object(app, "http", AsyncMock(return_value={"wake_enabled": False,
                    "services": dict(audio="RUNNING", asr="RUNNING", llm="READY", tts="READY", kws="DISABLED")})):
                self.assertTrue(await app.probe("voice"))
            self.assertTrue(app.state.snapshot()["core_available"])
        finally:
            app.outer_job.close()

    async def test_failed_capability_with_live_process_retains_real_metrics(self):
        app = ApplicationHost(Path.cwd())
        try:
            app.state.component("voice", state="failed", pid=os.getpid(), phase="能力不可用，进程仍在运行")
            app.sample_metrics()
            row = next(c for c in app.state.snapshot()["components"] if c["id"] == "voice")
            self.assertGreater(row["memory_rss_bytes"], 0)
            if os.name == "nt":
                self.assertGreater(row["memory_private_commit_bytes"], 0)
            app.state.component("voice", pid=None)
            row = next(c for c in app.state.snapshot()["components"] if c["id"] == "voice")
            self.assertIsNone(row["memory_rss_bytes"])
            self.assertIsNone(row["memory_private_commit_bytes"])
        finally:
            app.outer_job.close()

    async def test_exit_cancels_pending_control_without_waiting_for_http_thread(self):
        app = ApplicationHost(Path.cwd())
        blocked = threading.Event()
        started = threading.Event()
        def slow_request(*args, **kwargs):
            started.set()
            blocked.wait(30)
            raise OSError("已取消请求")
        try:
            with patch("urllib.request.urlopen", slow_request):
                task = asyncio.create_task(app.http("http://127.0.0.1:1", action="reinitialize", timeout=190))
                app.background.add(task)
                while not started.is_set():
                    await asyncio.sleep(.01)
                await asyncio.wait_for(app.shutdown(), timeout=1)
                self.assertTrue(task.cancelled())
                self.assertEqual(app.state.snapshot()["state"], "stopped")
                self.assertIsNone(app.state.snapshot()["total_memory_private_commit_bytes"])
        finally:
            blocked.set()
            app.outer_job.close()

    async def test_unowned_worker_is_never_connected(self):
        from voice_terminal.asr.client import AsrWorkerClient, AsrWorkerError
        from voice_terminal.analysis.client import AnalysisClient
        with patch.dict(os.environ, {"VOICE_APP_ASR_AVAILABLE": "0", "VOICE_APP_ANALYSIS_AVAILABLE": "0"}):
            with self.assertRaises(AsrWorkerError):
                await AsrWorkerClient("ws://127.0.0.1:1").connect()
            with self.assertRaisesRegex(RuntimeError, "所有权"):
                await AnalysisClient("ws://127.0.0.1:1").health()

    async def test_shared_process_metrics_are_identical_and_not_double_counted(self):
        import psutil
        app = ApplicationHost(Path.cwd())
        for key in ("host", "web"):
            app.state.component(key, state="ready", pid=os.getpid())
        app.sample_metrics()
        app.sample_metrics()
        snapshot = app.state.snapshot()
        rows = {c["id"]: c for c in snapshot["components"]}
        self.assertEqual(rows["host"]["cpu_percent"], rows["web"]["cpu_percent"])
        self.assertEqual(snapshot["total_memory_rss_bytes"], rows["host"]["memory_rss_bytes"])
        self.assertEqual(rows["host"]["memory_private_commit_bytes"], rows["web"]["memory_private_commit_bytes"])
        self.assertEqual(snapshot["total_memory_private_commit_bytes"], rows["host"]["memory_private_commit_bytes"])
        app.outer_job.close()

    async def test_memory_totals_deduplicate_child_components_and_exclude_external_process(self):
        """共享进程与已列出的子组件只计一次，外部调试进程不计入总量。"""
        app = ApplicationHost(Path.cwd())
        processes = {}
        for pid, rss, commit in ((10, 100, 1000), (20, 200, 2000),
                (30, 300, 3000), (40, 400, 4000), (50, 500, 5000)):
            process = Mock(pid=pid)
            process.create_time.return_value = 100 + pid
            process.memory_info.return_value = SimpleNamespace(rss=rss, private=commit)
            process.cpu_times.return_value = SimpleNamespace(user=1., system=0.)
            process.children.return_value = []
            processes[pid] = process
        processes[20].children.return_value = [processes[30], processes[40]]
        try:
            for key, pid in (("host", 10), ("web", 10), ("voice", 20), ("speaker", 30)):
                app.state.component(key, state="ready", pid=pid)
            app.state.component("asr", state="external", pid=50, external=True)
            with patch("psutil.Process", side_effect=processes.__getitem__):
                app.sample_metrics()
            snapshot = app.state.snapshot()
            rows = {c["id"]: c for c in snapshot["components"]}
            self.assertEqual(snapshot["total_memory_rss_bytes"], 1000)
            self.assertEqual(snapshot["total_memory_private_commit_bytes"], 10000)
            self.assertEqual(rows["asr"]["memory_private_commit_bytes"], 5000)
            self.assertEqual(rows["speaker"]["memory_private_commit_bytes"], 3000)
            for process in processes.values():
                process.memory_info.assert_called_once()
        finally:
            app.outer_job.close()

    async def test_missing_private_counter_does_not_report_virtual_address_space_as_commit(self):
        """缺少私有提交计数时保留空值，不能用虚拟地址空间代替。"""
        app = ApplicationHost(Path.cwd())
        process = Mock(pid=10)
        process.create_time.return_value = 100
        process.memory_info.return_value = SimpleNamespace(rss=100, vms=999999)
        process.cpu_times.return_value = SimpleNamespace(user=0., system=0.)
        try:
            app.state.component("host", state="ready", pid=10)
            with patch("psutil.Process", return_value=process):
                app.sample_metrics()
            snapshot = app.state.snapshot()
            self.assertEqual(snapshot["total_memory_rss_bytes"], 100)
            self.assertIsNone(snapshot["total_memory_private_commit_bytes"])
            self.assertIsNone(snapshot["components"][0]["memory_private_commit_bytes"])
        finally:
            app.outer_job.close()

    async def test_reused_pid_does_not_keep_previous_process_commit(self):
        """进程标识复用后，旧进程的两类内存指标都必须清空。"""
        app = ApplicationHost(Path.cwd())
        process = Mock(pid=10)
        process.create_time.return_value = 200
        try:
            app.state.component("host", state="ready", pid=10, created_at=100,
                memory_rss_bytes=1000, memory_private_commit_bytes=5000)
            with patch("psutil.Process", return_value=process):
                app.sample_metrics()
            snapshot = app.state.snapshot()
            self.assertIsNone(snapshot["components"][0]["memory_rss_bytes"])
            self.assertIsNone(snapshot["components"][0]["memory_private_commit_bytes"])
            self.assertIsNone(snapshot["total_memory_private_commit_bytes"])
            process.memory_info.assert_not_called()
        finally:
            app.outer_job.close()

    async def test_startup_attempts_all_components_after_asr_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            app = ApplicationHost(Path(directory))
            attempts = []
            async def setup():
                app.state.component("setup", state="ready")
            async def start(key):
                attempts.append(key)
                if key == "asr":
                    raise RuntimeError("模型缺失")
                app.state.component(key, state="ready")
            async def delegated():
                app.state.component("dsh", state="failed", error="不可用")
                app.state.component("speaker", state="disabled", enabled=False)
            with patch.object(app, "setup", setup), patch.object(app, "start_service", start), patch.object(app, "start_delegated", delegated):
                await app.startup()
            self.assertEqual(attempts, ["asr", "analysis", "voice"])
            self.assertTrue(app.state.snapshot()["startup_complete"])
            self.assertEqual(app.state.snapshot()["state"], "failed")
            app.outer_job.close()

    async def test_duplicate_retry_is_rejected_and_exit_disables_retry(self):
        app = ApplicationHost(Path.cwd())
        app.state.update(startup_complete=True)
        app.state.component("setup", state="ready")
        app.state.component("voice", state="ready", pid=123)
        app.state.component("dsh", state="failed")
        with patch.object(app, "enqueue", side_effect=lambda coroutine: coroutine.close()):
            self.assertTrue(app.request_retry("dsh"))
            self.assertFalse(app.request_retry("dsh"))
        app.state.update(stopping=True)
        self.assertFalse(app.request_retry("voice"))
        app.outer_job.close()

    async def test_late_generation_report_cannot_overwrite_current_startup(self):
        app = ApplicationHost(Path.cwd())
        app.generations["voice"] = "current"
        app.report({"component": "speaker", "generation": "old", "phase": "过期事件"})
        self.assertNotEqual(next(c for c in app.state.snapshot()["components"] if c["id"] == "speaker")["phase"], "过期事件")
        app.outer_job.close()


@unittest.skipUnless(os.name == "nt", "Windows lifecycle（Windows 生命周期）测试")
class WindowsProcessTests(unittest.TestCase):
    def test_frozen_bundle_paths_are_not_inherited_by_business_environment(self):
        root = Path.cwd()
        bundle = root / ".runtime/fake-bundle"
        env = {"Path": os.pathsep.join((str(bundle), str(bundle / "bin"), str(root / ".venv/Scripts")))}
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "_MEIPASS", str(bundle), create=True):
            clean = external_environment(env)
        self.assertEqual(clean["Path"], str(root / ".venv/Scripts"))
        self.assertNotEqual(clean["Path"], env["Path"])

    def test_single_instance_mutex_releases_on_close(self):
        name = "VoiceTerminal-test-" + str(time.time_ns())
        first, second = SingleInstance(name), SingleInstance(name)
        try:
            self.assertTrue(first.acquired)
            self.assertFalse(second.acquired)
        finally:
            second.close()
            first.close()
        third = SingleInstance(name)
        self.assertTrue(third.acquired)
        third.close()

    def test_named_event_gracefully_stops_hidden_child_in_chinese_path(self):
        with tempfile.TemporaryDirectory(prefix="应用启动测试-") as directory:
            root = Path(directory)
            outer = Job()
            process = ManagedProcess([sys.executable, "-c",
                "import asyncio; from voice_terminal.application.managed import wait_for_shutdown; asyncio.run(wait_for_shutdown()); print('closed')"],
                cwd=Path.cwd(), env=dict(os.environ), output=root / "日志.log", outer_job=outer,
                event_name="Local\\VoiceTerminal-test-" + str(time.time_ns()))
            try:
                process.request_stop()
                deadline = time.monotonic() + 5
                while process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertEqual(process.poll(), 0)
                self.assertIn("closed", (root / "日志.log").read_text())
            finally:
                process.kill()
                process.close()
                outer.close()

    def test_job_close_reclaims_grandchild(self):
        import psutil
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_path = root / "child.txt"
            command = [sys.executable, "-c", "import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)", str(pid_path)]
            outer = Job()
            process = ManagedProcess(command, cwd=root, env=dict(os.environ), output=root / "process.log", outer_job=outer,
                event_name="Local\\VoiceTerminal-test-" + str(time.time_ns()))
            try:
                deadline = time.monotonic() + 5
                while not pid_path.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                child = psutil.Process(int(pid_path.read_text()))
                self.assertTrue(child.is_running())
                outer.close()
                deadline = time.monotonic() + 5
                while child.is_running() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertFalse(child.is_running())
            finally:
                process.kill()
                process.close()
                outer.close()

    def test_abrupt_owner_exit_reclaims_child(self):
        import psutil
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "owner.py"
            pid_path = root / "pid.txt"
            script.write_text("""import os,sys,time
from pathlib import Path
from voice_terminal.application.windows import Job,ManagedProcess
job=Job()
child=ManagedProcess([sys.executable,'-c','import time; time.sleep(60)'],cwd=Path.cwd(),env=dict(os.environ),output=Path(sys.argv[1])/'child.log',outer_job=job,event_name='Local\\\\VoiceTerminal-abrupt-'+str(os.getpid()))
Path(sys.argv[1],'pid.txt').write_text(str(child.pid))
time.sleep(60)
""", encoding="utf-8")
            environment = {**os.environ, "PYTHONPATH": str(Path.cwd())}
            owner = subprocess.Popen([sys.executable, str(script), directory], env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline = time.monotonic() + 5
                while not pid_path.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                child = psutil.Process(int(pid_path.read_text()))
                owner.kill()
                owner.wait(timeout=5)
                deadline = time.monotonic() + 5
                while child.is_running() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertFalse(child.is_running())
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(timeout=5)
