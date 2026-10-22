"""Desktop continuity tests（桌面连续会话测试）：真实端口、网页凭据与私有首帧。"""
import io
import json
import os
from pathlib import Path
import secrets
import tempfile
import subprocess
import sys
import threading
import queue
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.request

from voice_terminal.application.continuity import read_start
from voice_terminal.application.state import ApplicationState
from voice_terminal.application.web import HostWeb


class ContinuityTests(unittest.TestCase):
    def test_real_desktop_control_rebuilds_host_without_a_second_page_handshake(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "app"
            (root / "config").mkdir(parents=True)
            (root / "config/default.yaml").write_text("{}", encoding="utf-8")
            (root / "package.json").write_text("{}", encoding="utf-8")
            frontend = root / "apps/frontend/dist"
            frontend.mkdir(parents=True)
            (frontend / "index.html").write_text("<html><head></head><body>连续会话</body></html>", encoding="utf-8")
            source = Path(__file__).resolve().parents[1]
            code = f"import sys;sys.path.insert(0,{str(source)!r});from voice_terminal.application.launch import main;main()"
            environment = {key: value for key, value in os.environ.items() if not key.startswith("VOICE_")}
            def launch(continuity=None):
                process = subprocess.Popen([sys.executable, "-B", "-X", "utf8", "-c", code,
                    "--root", str(root), "--python", sys.executable, "--data-dir", str(Path(directory) / "data"),
                    "--desktop-managed", "--diagnostic"], cwd=root, env=environment,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
                messages = queue.Queue()
                def read():
                    for line in process.stdout:
                        message = json.loads(line)
                        if message.get("kind") == "ready":
                            messages.put(message)
                    messages.put(None)
                threading.Thread(target=read, daemon=True).start()
                process.stdin.write(json.dumps({"protocol": 1, "kind": "start", "continuity": continuity}) + "\n")
                process.stdin.flush()
                try:
                    message = messages.get(timeout=15)
                    if message is None: raise queue.Empty
                    return process, message
                except queue.Empty:
                    if process.poll() is None: process.kill()
                    process.wait()
                    error = process.stderr.read()
                    process.stdin.close();process.stdout.close();process.stderr.close()
                    self.fail("真实桌面管道未报告入口就绪：" + error[-1800:])
            def stop(process):
                try:
                    process.stdin.write('{"protocol":1,"kind":"shutdown","budget_ms":0}\n')
                    process.stdin.flush()
                    self.assertEqual(process.wait(timeout=8), 0)
                finally:
                    if process.poll() is None: process.kill();process.wait()
                    process.stdin.close();process.stdout.close();process.stderr.close()
            first, ready = launch()
            try:
                entry = ready["origin"] + "/desktop-entry?token=" + ready["launch_token"]
                with urllib.request.urlopen(entry) as response:
                    cookie = response.headers["Set-Cookie"].split(";", 1)[0]
                continuity = ready["continuity"]
            finally:
                stop(first)
            second, restored = launch(continuity)
            try:
                self.assertEqual(restored["origin"], ready["origin"])
                self.assertNotEqual(restored["instance_id"], ready["instance_id"])
                request = urllib.request.Request(restored["origin"] + "/api/v1/application/snapshot", headers={"Cookie": cookie})
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(json.load(response)["instance_id"], restored["instance_id"])
            finally:
                stop(second)

    def test_private_start_frame_rejects_invalid_or_foreign_origins(self):
        self.assertIsNone(read_start(io.StringIO('{"protocol":1,"kind":"start"}\n')))
        record = {"host_origin": "http://127.0.0.1:12345", "frontend_origin": "http://127.0.0.1:23456",
                  "cookie_name": "voice_application_test", "session": secrets.token_urlsafe(32)}
        def frame(value):
            return io.StringIO(json.dumps({"protocol": 1, "kind": "start", "continuity": value}) + "\n")
        self.assertEqual(read_start(frame(record)), record)
        for origin in ("https://example.com", "http://localhost:12345", "http://127.0.0.1:12345/path",
                       "http://127.0.0.1:12345?secret=value", "http://user@127.0.0.1:12345", "http://127.0.0.1:0"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                read_start(frame({**record, "host_origin": origin}))
        for value in ([], {**record, "cookie_name": "bad; cookie=value"}, {**record, "session": "short"}):
            with self.assertRaises(ValueError):
                read_start(frame(value))
        for line in ("", '{"protocol":1,"kind":"shutdown"}\n', "x" * 16385):
            with self.assertRaises((RuntimeError, ValueError)):
                read_start(io.StringIO(line))

    def test_replacement_keeps_same_page_session_and_rejects_unrelated_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.html").write_text("<html><head></head><body>产品页</body></html>", encoding="utf-8")
            def application():
                return SimpleNamespace(state=ApplicationState(), frontend_origin="", internal_token="internal")
            first = HostWeb(application(), root, port=0)
            first.start()
            continuity = {"host_origin": first.origin, "frontend_origin": first.origin,
                          "cookie_name": first.cookie_name, "session": first.session}
            headers = {"Cookie": f"{first.cookie_name}={first.session}"}
            try:
                # 热恢复不能静默改用其它端口，避免旧页面连接错误来源。
                with self.assertRaises(OSError):
                    HostWeb(application(), root, continuity=continuity)
            finally:
                first.close()
            second_app = application()
            second = HostWeb(second_app, root, continuity=continuity)
            second.start()
            try:
                self.assertEqual(second.origin, continuity["host_origin"])
                request = urllib.request.Request(second.origin + "/api/v1/application/snapshot", headers=headers)
                with urllib.request.urlopen(request) as response:
                    snapshot = json.load(response)
                self.assertEqual(snapshot["instance_id"], second_app.state.value["instance_id"])
                self.assertNotEqual(snapshot["instance_id"], first.instance_id)
                self.assertNotIn(continuity["session"], json.dumps(snapshot))
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(second.origin + "/api/v1/application/snapshot")
                self.assertEqual(error.exception.code, 403)
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(second.origin + "/desktop-entry?token=" + first.issue_launch_token())
            finally:
                second.close()
