"""Application lifecycle tests（应用生命周期测试）：真实进程与独立网页接口。"""
from __future__ import annotations

import asyncio
from http.cookiejar import CookieJar
import io
import json
import os
from pathlib import Path
import re
import shutil
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
from voice_terminal.application.launch import _wait_for_frontend, parse_args
from voice_terminal.application.web import FALLBACK_HTML, HostWeb
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
    def test_manual_task_create_checks_cookie_host_and_origin(self):
        route = self.web.origin + '/api/v1/application/wake-tasks'
        payload = {'text': '手动说明', 'daily': {'time': '09:00', 'time_zone': 'Asia/Hong_Kong'}}
        def request(headers=None):
            return urllib.request.Request(route, json.dumps(payload).encode(),
                {'Content-Type': 'application/json', 'Origin': self.web.origin, **(headers or {})})
        with patch.object(self.app, 'create_wake_task', return_value={'task_id': 'manual-test'}) as create:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.client.open(request())
            self.assertEqual(error.exception.code, 403)
            self.client.open(self.web.origin + '/desktop-entry?token=' + self.web.issue_launch_token()).close()
            for headers in ({'Origin': 'https://untrusted.invalid'}, {'Host': 'untrusted.invalid'}):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.client.open(request(headers))
                self.assertEqual(error.exception.code, 403)
            create.assert_not_called()
            with self.client.open(request()) as response:
                self.assertEqual(response.status, 201)
                self.assertEqual(json.load(response), {'task_id': 'manual-test'})
            create.assert_called_once_with(payload)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        (Path(self.directory.name) / "index.html").write_text("<html><head></head><body>product-ui</body></html>", encoding="utf-8")
        (Path(self.directory.name) / "assets.js").write_text("window.product = true", encoding="utf-8")
        self.app = ApplicationHost(Path(self.directory.name))
        self.web = HostWeb(self.app, Path(self.directory.name), port=0)
        self.app.origin = self.web.origin
        self.web.start()
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def tearDown(self):
        self.web.close()
        self.app.outer_job.close()
        self.directory.cleanup()

    def test_capability_retry_route_is_available_to_the_page(self):
        """组件页的能力行通过独立端点恢复，不落到组件级整进程重试。"""
        self.client.open(self.web.origin + "/desktop-entry?token=" + self.web.issue_launch_token()).close()
        with patch.object(self.app, "request_capability_retry", return_value=True) as retry:
            request = urllib.request.Request(self.web.origin + "/api/v1/application/capabilities/tts/retry",
                b"{}", {"Content-Type": "application/json", "Origin": self.web.origin})
            with self.client.open(request) as response:
                self.assertEqual(response.status, 202)
            retry.assert_called_once_with("tts")
        with patch.object(self.app, "request_capability_retry", return_value=False):
            request = urllib.request.Request(self.web.origin + "/api/v1/application/capabilities/tts/retry",
                b"{}", {"Content-Type": "application/json", "Origin": self.web.origin})
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.client.open(request)
            self.assertEqual(error.exception.code, 409)

    def test_unclaimed_root_is_static_information_and_business_resources_are_denied(self):
        with self.client.open(self.web.origin) as response:
            self.assertIn("请通过桌面程序打开", response.read().decode())
            self.assertIsNone(response.headers.get("Set-Cookie"))
        with urllib.request.urlopen(self.web.origin + "/index.html") as response:
            self.assertIn("请通过桌面程序打开", response.read().decode())
        for path in ("/assets.js", "/application-config.js", "/api/v1/application/snapshot"):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(self.web.origin + path)
            self.assertEqual(error.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(self.web.origin + "/assets.js", method="HEAD"))
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(self.web.origin + "/api/v1/application/snapshot", method="OPTIONS"))
        self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.app.processes, {})

    def test_desktop_entry_consumes_single_use_token_and_sets_instance_cookie(self):
        token = self.web.issue_launch_token()
        with self.client.open(self.web.origin + "/desktop-entry?token=" + token) as response:
            body = response.read().decode()
            self.assertEqual(response.status, 200)
            self.assertIn('location.replace("/")', body)
            self.assertNotIn(token, body)
            cookie = response.headers.get("Set-Cookie", "")
            self.assertIn(self.web.cookie_name + "=", cookie)
            self.assertIn(self.web.instance_id, self.web.cookie_name)
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=Strict", cookie)
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertEqual(response.headers.get("Referrer-Policy"), "no-referrer")
        with self.client.open(self.web.origin + "/") as response:
            body = response.read().decode()
            self.assertIn("product-ui", body)
            self.assertNotIn("application-config.js", body)
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.web.origin + "/desktop-entry?token=" + token)
        self.assertEqual(error.exception.code, 403)

    def test_launch_token_is_consumed_atomically(self):
        from concurrent.futures import ThreadPoolExecutor
        token = self.web.issue_launch_token()
        with ThreadPoolExecutor(max_workers=16) as pool:
            accepted = list(pool.map(self.web.consume_launch_token, [token] * 32))
        self.assertEqual(accepted.count(True), 1)

    def test_expired_launch_token_cannot_establish_session(self):
        with patch("voice_terminal.application.web.time.monotonic", side_effect=[100, 161]):
            token = self.web.issue_launch_token()
            self.assertFalse(self.web.consume_launch_token(token))

    def test_config_requires_session_and_does_not_issue_a_cookie(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.web.origin + "/application-config.js")
        self.assertEqual(error.exception.code, 403)
        self.client.open(self.web.origin + "/desktop-entry?token=" + self.web.issue_launch_token()).close()
        with self.client.open(self.web.origin + "/application-config.js") as response:
            self.assertIn("window.__VOICE_APPLICATION__", response.read().decode())
            self.assertIsNone(response.headers.get("Set-Cookie"))

    def test_feedback_exists_before_any_business_process(self):
        self.client.open(self.web.origin + "/desktop-entry?token=" + self.web.issue_launch_token()).close()
        with self.client.open(self.web.origin + "/api/v1/application/snapshot") as response:
            data = json.load(response)
        self.assertEqual(data["state"], "starting")
        self.assertEqual(self.app.processes, {})

    def test_occupied_host_port_uses_a_distinct_owned_listener(self):
        second_app = ApplicationHost(Path(self.directory.name))
        second = HostWeb(second_app, Path(self.directory.name), port=self.web.server.server_port)
        second.start()
        try:
            self.assertNotEqual(second.origin, self.web.origin)
            self.assertNotEqual(second.session, self.web.session)
            self.assertNotEqual(second.cookie_name, self.web.cookie_name)
            with urllib.request.urlopen(second.origin) as response:
                self.assertIn("请通过桌面程序打开", response.read().decode())
        finally:
            second.close()
            second_app.outer_job.close()

    def test_cookie_and_origin_are_required_for_mutations(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.web.origin + "/api/v1/application/snapshot")
        self.assertEqual(error.exception.code, 403)
        self.client.open(self.web.origin + "/desktop-entry?token=" + self.web.issue_launch_token()).close()
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

    def test_repeated_instance_cookie_is_rejected_even_when_values_match(self):
        duplicate = f"{self.web.cookie_name}={self.web.session}; {self.web.cookie_name}={self.web.session}"
        request = urllib.request.Request(self.web.origin + "/api/v1/application/snapshot", headers={"Cookie": duplicate})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        self.assertEqual(error.exception.code, 403)

    def test_dev_proxy_mutations_match_actual_frontend_origin(self):
        self.app.frontend_origin = "http://127.0.0.1:5173"
        self.client.open(self.web.origin + "/desktop-entry?token=" + self.web.issue_launch_token()).close()
        with patch.object(self.app, "request_exit") as exit_app:
            request = urllib.request.Request(self.web.origin + "/api/v1/application/exit", b"{}",
                {"Content-Type": "application/json", "Origin": self.app.frontend_origin})
            self.client.open(request).close()
            exit_app.assert_called_once()

    def test_sse_delivers_terminal_snapshot(self):
        self.client.open(self.web.origin + "/desktop-entry?token=" + self.web.issue_launch_token()).close()
        self.app.state.update(state="stopped", stopping=True)
        with self.client.open(self.web.origin + "/api/v1/application/events", timeout=3) as response:
            line = response.readline().decode()
            self.assertEqual(json.loads(line[6:])["state"], "stopped")

    def test_internal_reports_do_not_accept_browser_session(self):
        self.client.open(self.web.origin + "/desktop-entry?token=" + self.web.issue_launch_token()).close()
        request = urllib.request.Request(self.web.origin + "/internal/application/report", b"{}", {"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.client.open(request)
        self.assertEqual(error.exception.code, 403)
        request = urllib.request.Request(self.web.origin + "/internal/application/report", b"{}",
            {"Content-Type": "application/json", "Authorization": "Bearer " + self.app.internal_token})
        with patch.object(self.app, "report", return_value=True) as report:
            with self.client.open(request) as response:
                self.assertEqual(json.load(response), {"ok": True})
            report.assert_called_once_with({})


class FrontendReadinessTests(unittest.TestCase):
    def setUp(self):
        self.app = SimpleNamespace(frontend_cookie_name="voice_application_instance1",
            frontend_session="private-session", state=SimpleNamespace(value={"instance_id": "instance-1"}))
        self.process = SimpleNamespace(pid=123, created_at=100.0, poll=Mock(return_value=None))

    @staticmethod
    def listener(pid, ip="127.0.0.1", port=5173):
        import psutil
        return SimpleNamespace(status=psutil.CONN_LISTEN,
            laddr=SimpleNamespace(ip=ip, port=port), pid=pid)

    def test_other_listener_fails_before_sending_session_cookie(self):
        owner = Mock()
        owner.create_time.return_value = 100.0
        # 受管进程报告实际监听端口后，系统监听表中的不同所有者必须被拒绝。
        owner.net_connections.return_value = [self.listener(123)]
        with patch("psutil.Process", return_value=owner), \
                patch("psutil.net_connections", return_value=[self.listener(456)]), \
                patch("urllib.request.urlopen") as urlopen:
            with self.assertRaisesRegex(RuntimeError, "端口 5173 已被其他进程占用"):
                _wait_for_frontend(self.app, self.process, timeout=1)
        urlopen.assert_not_called()

    def test_unbound_owned_process_waits_without_http_request(self):
        owner = Mock()
        owner.create_time.return_value = 100.0
        owner.net_connections.return_value = []
        with patch("psutil.Process", return_value=owner), \
                patch("psutil.net_connections", return_value=[]), \
                patch("voice_terminal.application.launch.time.monotonic", side_effect=[0, 0, 2]), \
                patch("voice_terminal.application.launch.time.sleep"), \
                patch("urllib.request.urlopen") as urlopen:
            with self.assertRaisesRegex(RuntimeError, "开发界面未能就绪"):
                _wait_for_frontend(self.app, self.process, timeout=1)
        urlopen.assert_not_called()

    def test_owned_loopback_listener_is_checked_before_readiness_request(self):
        owner = Mock()
        owner.create_time.return_value = 100.0
        owner.net_connections.return_value = [self.listener(123)]
        payload = json.dumps({"pid": 123, "instance_id": "instance-1",
            "origin": "http://127.0.0.1:5173", "frontend_mode": "product"}).encode()
        with patch("psutil.Process", return_value=owner), \
                patch("psutil.net_connections", return_value=[self.listener(123)]), \
                patch("urllib.request.urlopen", return_value=io.BytesIO(payload)) as urlopen:
            result = _wait_for_frontend(self.app, self.process, timeout=1)
        self.assertEqual(result, "http://127.0.0.1:5173")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Cookie"), "voice_application_instance1=private-session")

    def test_reused_pid_with_different_creation_time_never_receives_cookie(self):
        owner = Mock()
        owner.create_time.return_value = 103.0
        owner.net_connections.return_value = [self.listener(123)]
        with patch("psutil.Process", return_value=owner), \
                patch("urllib.request.urlopen") as urlopen:
            with self.assertRaisesRegex(RuntimeError, "created_at（进程创建时间）与本实例不匹配"):
                _wait_for_frontend(self.app, self.process, timeout=1)
        urlopen.assert_not_called()


@unittest.skipUnless(shutil.which("node"), "Node.js（JavaScript 运行环境）测试工具不可用")
class FallbackDiagnosticPageTests(unittest.TestCase):
    def run_script(self, *, bridge: bool, status=None, click_exit=False, click_skip=False, snapshot=None):
        match = re.search(r"<script>(.*?)</script>", FALLBACK_HTML, re.DOTALL)
        self.assertIsNotNone(match)
        harness = r"""
const vm = require('node:vm')
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'))
const calls = [], timers = []
class Element {
  constructor(id) { this.id=id; this.children=[]; this.listeners={}; this.hidden=false; this.textContent=''; this.style={}; this.dataset={}; }
  addEventListener(name, callback) { this.listeners[name]=callback }
  append(child) { this.children.push(child) }
  replaceChildren(...children) { this.children=children }
}
const elements = Object.fromEntries(['title','phase','components','retry','exit','skip','progress','fill'].map(id => [id,new Element(id)]))
const document = { getElementById:id => elements[id], createElement:tag => new Element(tag) }
const window = { setInterval:(callback,delay) => { timers.push(delay); return timers.length } }
if (input.bridge) window.__TAURI_INTERNALS__ = { invoke:async command => {
  calls.push(command)
  if (command === 'desktop_status') return input.status
} }
const fetch = async url => {
  calls.push('fetch:'+url)
  return { ok:true, json:async()=>input.snapshot }
}
vm.runInNewContext(input.script,{window,document,fetch})
setImmediate(async()=>{
  if (input.clickExit) await elements.exit.listeners.click()
  if (input.clickSkip) await elements.skip.listeners.click()
  process.stdout.write(JSON.stringify({calls,timers,phase:elements.phase.textContent,
    title:elements.title.textContent,listHidden:elements.components.hidden,
    progressHidden:elements.progress.hidden,progressWidth:elements.fill.style.width,
    exitHidden:elements.exit.hidden,retryHidden:elements.retry.hidden}))
})
"""
        result = subprocess.run([shutil.which("node"), "-e", harness],
            input=json.dumps({"script": match.group(1), "bridge": bridge, "status": status,
                "clickExit": click_exit, "clickSkip": click_skip,
                "snapshot": snapshot or {"phase": "running", "components": []}}),
            text=True, encoding="utf-8", capture_output=True, timeout=5, check=True)
        return json.loads(result.stdout)

    def test_missing_or_invalid_native_status_never_requests_host(self):
        missing = self.run_script(bridge=False)
        self.assertEqual(missing["calls"], [])
        self.assertIn("请退出并重新启动桌面程序", missing["phase"])
        failed = self.run_script(bridge=True,
            status={"ready": True, "error": None, "frontend_mode": "ui-preview", "development": False})
        self.assertEqual(failed["calls"], ["desktop_status"])
        self.assertIn("请退出并重新启动桌面程序", failed["phase"])
        self.assertEqual(failed["timers"], [])
        invalid_development = self.run_script(bridge=True,
            status={"ready": True, "error": None, "frontend_mode": "product", "development": "false"})
        self.assertEqual(invalid_development["calls"], ["desktop_status"])
        self.assertIn("请退出并重新启动桌面程序", invalid_development["phase"])
        self.assertEqual(invalid_development["timers"], [])

    def test_valid_product_status_gates_host_reads_and_exit_uses_native_command(self):
        ready = self.run_script(bridge=True,
            status={"ready": True, "error": None, "frontend_mode": "product", "development": False},
            )
        self.assertEqual(ready["calls"], ["desktop_status", "fetch:/api/v1/application/snapshot"])
        self.assertEqual(ready["timers"], [2000])
        # 与状态检查分开验证退出按钮只调用原生命令，不发送 Host（宿主）HTTP 请求。
        exited = self.run_script(bridge=True,
            status={"ready": True, "error": None, "frontend_mode": "product", "development": True},
            click_exit=True)
        self.assertEqual(exited["calls"], ["desktop_status", "fetch:/api/v1/application/snapshot", "exit_application"])

    def test_fallback_loading_keeps_details_hidden_until_skip_without_starting_services(self):
        status = {"ready": True, "error": None, "frontend_mode": "product", "development": False}
        snapshot = {"startup_complete": False, "total_components": 8, "completed_components": 4,
            "components": [{"id": "asr", "name": "语音识别", "state": "starting", "phase": "加载模型"}]}
        loading = self.run_script(bridge=True, status=status, snapshot=snapshot)
        self.assertTrue(loading["listHidden"])
        self.assertFalse(loading["progressHidden"])
        self.assertEqual(loading["progressWidth"], "50%")
        inspected = self.run_script(bridge=True, status=status, snapshot=snapshot, click_skip=True)
        self.assertFalse(inspected["listHidden"])
        self.assertTrue(inspected["progressHidden"])
        self.assertEqual(inspected["title"], "本地组件")
        self.assertEqual(inspected["calls"], ["desktop_status", "fetch:/api/v1/application/snapshot"] * 2)


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

    async def test_loading_capability_reports_starting_instead_of_failure(self):
        """加载中的能力是瞬态：组件显示启动中，整体状态不是启动失败。"""
        app = ApplicationHost(Path.cwd())
        try:
            for row in app.state.snapshot()["components"]:
                app.state.component(row["id"], state="ready")
            app.state.component("voice", pid=123)
            app.state.update(startup_complete=True)
            services = dict(audio="READY", asr="READY", llm="READY", tts="STARTING", kws="READY")
            with patch.object(app, "http", AsyncMock(return_value={"wake_enabled": True, "services": dict(services)})):
                self.assertTrue(await app.probe("voice"))
            app.state.finish()
            snapshot = app.state.snapshot()
            voice = next(c for c in snapshot["components"] if c["id"] == "voice")
            self.assertFalse(snapshot["core_available"])
            self.assertEqual(snapshot["core_phase"], "starting")
            self.assertEqual(snapshot["state"], "starting")
            self.assertEqual(voice["state"], "starting")
            self.assertEqual(voice["error"], "")
            self.assertFalse(voice["retry_allowed"])
            self.assertIn("TTS（语音合成）", voice["phase"])
        finally:
            app.outer_job.close()

    async def test_single_probe_failure_does_not_condemn_the_component(self):
        """单次健康检查抖动不能把语音服务判死，连续超过阈值与宽限期才进入故障。"""
        app = ApplicationHost(Path.cwd())
        try:
            for row in app.state.snapshot()["components"]:
                app.state.component(row["id"], state="ready")
            app.state.component("voice", pid=os.getpid())
            app.state.update(startup_complete=True, core_available=True, core_phase="ready")
            app.probe_grace_seconds = 0.0
            self.assertFalse(app._note_probe_failure("voice", OSError("状态查询超时")))
            self.assertFalse(app._note_probe_failure("voice", OSError("状态查询超时")))
            self.assertTrue(app._note_probe_failure("voice", OSError("状态查询超时")))
            row = next(c for c in app.state.snapshot()["components"] if c["id"] == "voice")
            self.assertEqual(row["state"], "starting")
            self.assertIn("健康检查重试中", row["phase"])
            app._reset_probe_failure("voice")
            self.assertIsNone(app._probe_failures.get("voice"))
        finally:
            app.outer_job.close()

    async def test_probe_grace_period_blocks_premature_failure(self):
        """达到次数但未过宽限期仍然不算失败，避免启动或加载瞬间被误判。"""
        app = ApplicationHost(Path.cwd())
        try:
            app.state.update(startup_complete=True)
            app.probe_failure_threshold = 1
            app.probe_grace_seconds = 60.0
            self.assertFalse(app._note_probe_failure("voice", OSError("第一次失败")))
            row = next(c for c in app.state.snapshot()["components"] if c["id"] == "voice")
            self.assertEqual(row["state"], "starting")
        finally:
            app.outer_job.close()

    async def test_capability_retry_does_not_restart_the_voice_process(self):
        """进程存活时优先做能力级恢复，不能重启整个语音服务。"""
        app = ApplicationHost(Path.cwd())
        try:
            alive = Mock()
            alive.poll.return_value = None
            app.processes["voice"] = alive
            app.capability_rows = [
                {"id": "tts", "label": "TTS（语音合成）", "state": "ERROR", "blocking": True,
                 "phase": "语音合成路由与播放器", "error": "模型文件缺失", "retry": "tts"},
            ]
            app.state.update(startup_complete=True)
            app.state.component("voice", state="ready", pid=os.getpid())
            self.assertTrue(app.request_capability_retry("tts"))
            app.state.component("voice", state="ready", pid=os.getpid())
            with patch.object(app, "voice_control", AsyncMock(return_value={"ok": True})) as control, \
                 patch.object(app, "probe", AsyncMock(return_value=True)):
                await app.retry_capability("tts")
            control.assert_awaited_once()
            self.assertEqual(control.await_args.args[0], "capability-retry")
            self.assertEqual(control.await_args.kwargs["payload"], {"capability": "tts"})
            alive.request_stop.assert_not_called()
            alive.kill.assert_not_called()
        finally:
            app.outer_job.close()

    async def test_capability_retry_falls_back_to_process_restart_when_process_died(self):
        """语音进程已经退出时，能力级恢复回落到整进程重建。"""
        app = ApplicationHost(Path.cwd())
        try:
            dead = Mock()
            dead.poll.return_value = 1
            app.processes["voice"] = dead
            app.capability_rows = [
                {"id": "tts", "label": "TTS（语音合成）", "state": "ERROR", "blocking": True,
                 "phase": "语音合成路由与播放器", "error": "", "retry": "tts"},
            ]
            app.state.update(startup_complete=True)
            with patch.object(app, "retry", AsyncMock()) as restart:
                await app.retry_capability("tts")
            restart.assert_awaited_once_with("voice")
        finally:
            app.outer_job.close()

    async def test_capability_retry_rejects_unknown_capability(self):
        app = ApplicationHost(Path.cwd())
        try:
            app.state.update(startup_complete=True)
            self.assertFalse(app.request_capability_retry("nope"))
        finally:
            app.outer_job.close()

    async def test_request_stop_records_the_reason(self):
        """退出与重试必须能从事后日志区分：停止请求要记录组件与原因。"""
        process = ManagedProcess(["cmd", "/c", "exit"], cwd=Path.cwd(), env={},
            output=Path(tempfile.gettempdir()) / "managed-stop-test.log",
            outer_job=Job(), event_name="Local\\VoiceTerminal-Test-Stop", key="voice")
        try:
            with patch("voice_terminal.application.windows.log.info") as logged:
                process.request_stop("component-retry:voice")
            self.assertIn("组件", logged.call_args.args[0])
            self.assertEqual(logged.call_args.args[1], "voice")
            self.assertEqual(logged.call_args.args[2], "component-retry:voice")
        finally:
            process.close()

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

    async def test_initialization_control_sends_live_worker_ownership(self):
        app = ApplicationHost(Path.cwd())
        app.state.component("asr", state="ready")
        app.state.component("analysis", state="external")
        captured = {}

        def response(request, timeout):
            captured.update(json.loads(request.data))
            return io.BytesIO(b"{}")

        try:
            with patch("urllib.request.urlopen", side_effect=response):
                await app.http("http://127.0.0.1:8765/internal/application/control",
                               action="initialize-session")
            self.assertTrue(captured["asr_available"])
            self.assertFalse(captured["analysis_available"])
            self.assertTrue(captured["asr_managed"])
            self.assertFalse(captured["analysis_managed"])
        finally:
            app.outer_job.close()

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

    async def test_dev_server_gets_cookie_handshake_but_not_internal_bearer_token(self):
        app = ApplicationHost(Path.cwd())
        app.origin = "http://127.0.0.1:8764"
        app.frontend_origin = "http://127.0.0.1:5173"
        app.frontend_cookie_name = "voice_application_instance"
        app.frontend_session = "frontend-session-secret"
        app.internal_token = "host-internal-secret"
        captured = {}
        def process(command, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(pid=123, created_at=456)
        try:
            with patch("voice_terminal.application.supervisor.ManagedProcess", side_effect=process):
                app.spawn("dev", ["node", "scripts/frontend-dev.mjs"])
            env = captured["env"]
            self.assertEqual(env["VOICE_APP_ORIGIN"], app.origin)
            self.assertEqual(env["VOICE_WEB_ORIGINS"], app.frontend_origin)
            self.assertEqual(env["VOICE_FRONTEND_COOKIE_NAME"], app.frontend_cookie_name)
            self.assertEqual(env["VOICE_FRONTEND_SESSION"], app.frontend_session)
            self.assertEqual(env["VOICE_APP_INSTANCE_ID"], app.state.value["instance_id"])
            self.assertNotIn("VOICE_APP_INTERNAL_TOKEN", env)
        finally:
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
            with (patch("psutil.Process", side_effect=processes.__getitem__),
                  patch("voice_terminal.gpu_memory.process_vram_snapshot", return_value={pid: pid * 10 for pid in processes})):
                app.sample_metrics()
            snapshot = app.state.snapshot()
            rows = {c["id"]: c for c in snapshot["components"]}
            self.assertEqual(snapshot["total_memory_rss_bytes"], 1000)
            self.assertEqual(snapshot["total_memory_private_commit_bytes"], 10000)
            self.assertEqual(snapshot["total_gpu_dedicated_bytes"], 1000)
            ledger = snapshot["resource_processes"]
            self.assertEqual(len(ledger), 4)
            self.assertEqual(sum(row["memory_rss_bytes"] for row in ledger), snapshot["total_memory_rss_bytes"])
            self.assertEqual(sum(row["memory_private_commit_bytes"] for row in ledger), snapshot["total_memory_private_commit_bytes"])
            self.assertEqual(sum(row["gpu_dedicated_bytes"] for row in ledger), snapshot["total_gpu_dedicated_bytes"])
            self.assertEqual({row["pid"]: row["component_id"] for row in ledger}, {10: "host", 20: "voice", 30: "speaker", 40: "voice"})
            self.assertEqual(rows["host"]["gpu_dedicated_bytes"], rows["web"]["gpu_dedicated_bytes"])
            self.assertEqual(rows["asr"]["gpu_dedicated_bytes"], 500)
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
                memory_rss_bytes=1000, memory_private_commit_bytes=5000, gpu_dedicated_bytes=6000)
            with patch("psutil.Process", return_value=process):
                app.sample_metrics()
            snapshot = app.state.snapshot()
            self.assertIsNone(snapshot["components"][0]["memory_rss_bytes"])
            self.assertIsNone(snapshot["components"][0]["memory_private_commit_bytes"])
            self.assertIsNone(snapshot["total_memory_private_commit_bytes"])
            self.assertIsNone(snapshot["components"][0]["gpu_dedicated_bytes"])
            self.assertIsNone(snapshot["total_gpu_dedicated_bytes"])
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
            async def entry():
                attempts.append("voice-entry")
                app.voice_entry_ready = True
            async def local():
                attempts.append("voice-local")
                app.voice_local_ready = True
            async def session():
                attempts.append("voice-session")
                self.assertEqual(next(c for c in app.state.snapshot()["components"] if c["id"] == "asr")["state"], "failed")
                app.state.component("voice", state="failed", error="语音识别尚未就绪")
            async def delegated():
                app.state.component("dsh", state="failed", error="不可用")
                app.state.component("speaker", state="disabled", enabled=False)
            with patch.object(app, "setup", setup), patch.object(app, "start_service", start), \
                 patch.object(app, "start_voice_entry", entry), patch.object(app, "start_voice_local", local), \
                 patch.object(app, "finish_voice_startup", session), patch.object(app, "start_delegated", delegated), \
                 patch.object(app, "watch_delegated_startup", new=AsyncMock()):
                await app.startup()
            self.assertEqual(attempts, ["voice-entry", "asr", "analysis", "voice-local", "voice-session"])
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

    async def test_duplicate_capability_retry_is_rejected(self):
        """同一能力恢复期间拒绝重复请求，退出后不再接受恢复。"""
        app = ApplicationHost(Path.cwd())
        try:
            alive = Mock()
            alive.poll.return_value = None
            app.processes["voice"] = alive
            app.capability_rows = [
                {"id": "tts", "label": "TTS（语音合成）", "state": "ERROR", "blocking": True,
                 "phase": "语音合成路由与播放器", "error": "模型文件缺失", "retry": "tts"},
            ]
            app.state.update(startup_complete=True)
            app.state.component("voice", state="ready", pid=123)
            with patch.object(app, "enqueue", side_effect=lambda coroutine: coroutine.close()):
                self.assertTrue(app.request_capability_retry("tts"))
                self.assertFalse(app.request_capability_retry("tts"))
            app.state.update(stopping=True)
            self.assertFalse(app.request_capability_retry("tts"))
        finally:
            app.outer_job.close()

    async def test_late_generation_report_cannot_overwrite_current_startup(self):
        app = ApplicationHost(Path.cwd())
        app.generations["voice"] = "current"
        app.report({"component": "speaker", "generation": "old", "phase": "过期事件"})
        self.assertNotEqual(next(c for c in app.state.snapshot()["components"] if c["id"] == "speaker")["phase"], "过期事件")
        app.outer_job.close()


class LaunchModeTests(unittest.TestCase):
    def test_headless_mode_keeps_useful_backend_options(self):
        args = parse_args(["--headless", "--python", "python.exe", "--config", "custom.yaml",
            "--llm-provider", "deepseek", "--no-worker", "--no-analysis", "--data-dir", "data"])
        self.assertTrue(args.headless)
        self.assertFalse(args.desktop_managed)
        self.assertTrue(args.no_worker)
        self.assertTrue(args.no_analysis)
        self.assertEqual(args.llm_provider, "deepseek")
        self.assertEqual(args.data_dir, Path("data"))

    def test_gui_entry_requires_desktop_owner_and_rejects_obsolete_browser_switches(self):
        with self.assertRaises(SystemExit):
            parse_args(["--root", "."])
        with self.assertRaises(SystemExit):
            parse_args(["--headless", "--no-browser"])
        with self.assertRaises(SystemExit):
            parse_args(["--headless", "--dev"])
        args = parse_args(["--desktop-managed", "--dev"])
        self.assertTrue(args.desktop_managed)
        self.assertTrue(args.dev)


@unittest.skipUnless(os.name == "nt", "Windows lifecycle（Windows 生命周期）测试")
class WindowsProcessTests(unittest.TestCase):
    def test_frozen_bundle_paths_are_not_inherited_by_business_environment(self):
        root = Path.cwd()
        bundle = root / ".local/fake-bundle"
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
                "import sys; sys.path.insert(0, " + repr(str(Path(__file__).resolve().parents[1])) + "); import asyncio; from voice_terminal.application.managed import wait_for_shutdown; asyncio.run(wait_for_shutdown()); print('closed')"],
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
            command = [sys.executable, "-c", "import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(60)']); Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)", str(pid_path)]
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
child=ManagedProcess([sys.executable,'-B','-c','import time; time.sleep(60)'],cwd=Path.cwd(),env=dict(os.environ),output=Path(sys.argv[1])/'child.log',outer_job=job,event_name='Local\\\\VoiceTerminal-abrupt-'+str(os.getpid()))
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
