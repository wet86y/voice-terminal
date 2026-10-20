"""下载来源回归：使用本机响应，验证内容校验、原子替换及公开源隔离。"""
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from unittest.mock import patch

import pytest

from voice_terminal import download_sources as sources


@pytest.fixture
def server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            requests.append(self.path)
            if self.path == "/missing":
                self.send_error(404)
                return
            if self.path == "/broken":
                self.send_response(200)
                self.send_header("Content-Length", "999")
                self.end_headers()
                self.wfile.write(b"partial")
                return
            body = b"<html>wrong resource</html>" if self.path == "/html" else b"bad" if self.path == "/bad" else b"verified resource"
            self.send_response(200)
            self.send_header("Content-Type", "text/html" if self.path == "/html" else "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{http.server_port}", requests
    http.shutdown()
    thread.join()
    http.server_close()


@pytest.mark.parametrize("route", ["missing", "html", "bad", "broken"])
def test_failure_falls_back_without_promoting_bad_content(tmp_path, server, route):
    base, requests = server
    target = tmp_path / "existing.bin"
    target.write_bytes(b"previous installation")
    expected = hashlib.sha256(b"verified resource").hexdigest()
    with patch.object(sources, "candidates", return_value=[base + "/" + route, base + "/good"]):
        sources.download("https://origin/resource", target, sha256=expected, size=17)
    assert target.read_bytes() == b"verified resource"
    assert requests == ["/" + route, "/good"]
    assert not target.with_name(target.name + ".part").exists()


def test_all_failures_keep_installed_file(tmp_path, server):
    base, _ = server
    target = tmp_path / "existing.bin"
    target.write_bytes(b"previous installation")
    with patch.object(sources, "candidates", return_value=[base + "/bad"]):
        with pytest.raises(OSError, match="所有下载来源"):
            sources.download("https://origin/resource", target, sha256="0" * 64)
    assert target.read_bytes() == b"previous installation"


def test_user_interrupt_never_tries_next_source(tmp_path):
    with patch.object(sources, "candidates", return_value=["https://mirror", "https://origin"]), patch.object(sources.urllib.request, "urlopen", side_effect=KeyboardInterrupt) as opening:
        with pytest.raises(KeyboardInterrupt):
            sources.download("https://origin", tmp_path / "target")
    assert opening.call_count == 1


def test_all_locked_resources_map_without_changing_identity():
    root = sources.Path(__file__).resolve().parents[1]
    for name in ["voice-runtime", "voice-gpu"]:
        lock = json.loads((root / f"config/runtime/{name}.lock.json").read_text("utf-8"))
        for row in lock["wheels"]:
            if row.get("url"):
                assert len(sources.candidates(row["url"])) == 2
                assert sources.candidates(row["url"])[-1] == row["url"]
    catalog = json.loads((root / "config/resources/catalog.json").read_text("utf-8"))
    for spec in catalog["resources"]:
        if spec["source"]["platform"] == "huggingface":
            for row in spec["files"]:
                if row.get("asset"):
                    continue
                origin = f'https://huggingface.co/{spec["source"]["repo_id"]}/resolve/{spec["source"]["revision"]}/{row.get("source_path", row["path"])}'
                if spec["source"].get("mirror_policy") == "origin_only":
                    # 未核验相同摘要的镜像不能替换固定权重或声学类别表。
                    assert spec["resource_id"] in {"emotion2vec-plus-base", "yamnet"}
                    assert spec["source"].get("mirror_policy_zh")
                    assert sources.candidates(origin) == [origin]
                    assert len(spec["source"]["revision"]) == 40
                else:
                    assert sources.candidates(origin)[0].startswith("https://modelscope.cn/")
                assert sources.candidates(origin)[-1] == origin
    manifest = "https://github.com/wet86y/voice-terminal/releases/latest/download/update.json"
    assert sources.candidates(manifest) == [manifest]
    assert sources.candidates("https://private.example/resource") == ["https://private.example/resource"]


def test_private_npm_configuration_is_preserved(tmp_path):
    config = tmp_path / "npmrc"
    config.write_text("registry=https://private.example\n", encoding="utf-8")
    env = {"NPM_CONFIG_USERCONFIG": str(config)}
    assert sources.npm_environment(env) == env


def test_default_user_npm_configuration_and_build_adapter_stay_private(tmp_path, monkeypatch):
    import importlib.util
    (tmp_path / ".npmrc").write_text('registry = "https://private.example/"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    env = {"USERPROFILE": str(tmp_path), "npm_config_disturl": "https://private.example/node"}
    assert sources.npm_registry(env) == "https://private.example"
    assert sources.npm_environment(env) == env
    root = sources.Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("mirror_dsh_build", root / "scripts/build-dsh-runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with patch.object(module, "run", side_effect=RuntimeError("模拟私有源失败")) as run:
        with pytest.raises(RuntimeError):
            module.run_registry("node", "pnpm", "install", cwd=tmp_path, env=env)
    assert run.call_count == 1
    assert run.call_args.kwargs["env"]["npm_config_disturl"] == env["npm_config_disturl"]


def test_project_registry_is_respected_before_changing_build_directory(tmp_path):
    project = tmp_path / "source"
    project.mkdir()
    (project / ".npmrc").write_text("registry=https://private.example\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    env = {"USERPROFILE": str(home)}
    assert sources.npm_environment(env, cwd=project) == env
    env = {"NPM_CONFIG_REGISTRY": "https://private.example", "npm_config_disturl": "https://private.example/node"}
    assert sources.npm_environment(env) == env


def test_pip_uses_sequential_indices_and_stops_on_success():
    with patch.object(sources.subprocess, "run", side_effect=[sources.subprocess.CompletedProcess([], 1), sources.subprocess.CompletedProcess([], 0)]) as run:
        sources.pip_run(["python", "-m", "pip", "wheel", "torch"], profile="cu128", env={})
    assert run.call_count == 2
    assert "https://mirror.sjtu.edu.cn/pytorch-wheels/cu128" in run.call_args_list[0].args[0]
    assert "https://download.pytorch.org/whl/cu128" in run.call_args_list[1].args[0]


def test_private_pip_index_never_falls_through_to_public():
    with patch.object(sources.subprocess, "run", return_value=sources.subprocess.CompletedProcess([], 1)) as run:
        with pytest.raises(sources.subprocess.CalledProcessError):
            sources.pip_run(["python", "-m", "pip", "wheel", "private-package"], env={"PIP_INDEX_URL": "https://private.example/simple"}, profile="cu128")
    assert run.call_count == 1
    assert "--extra-index-url" not in run.call_args.args[0]


def test_tokenizer_uses_bundled_bytes_without_network(tmp_path, monkeypatch):
    from tiktoken.load import read_file_cached
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    record = sources.policy()["tokenizer"]
    with patch("requests.get", side_effect=AssertionError("离线词表不应联网")):
        cache = sources.seed_tokenizer_cache()
        assert hashlib.sha256(read_file_cached(record["url"], expected_hash=record["sha256"])).hexdigest() == record["sha256"]
        cache.write_bytes(b"damaged cache")
        assert sources.seed_tokenizer_cache() == cache
        assert hashlib.sha256(cache.read_bytes()).hexdigest() == record["sha256"]
