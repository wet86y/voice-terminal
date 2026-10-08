"""Qualification（分发验收）：本地发布源、限流模型探测与原生续传实验。"""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import unquote, urlsplit, urlencode

SANDBOX = Path("D:/项目开发/测试").resolve()
REPOSITORY = Path(__file__).resolve().parents[1]


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def bounded(path: Path) -> Path:
    path = path.resolve()
    if path == SANDBOX or not path.is_relative_to(SANDBOX):
        raise ValueError("验收操作只能使用指定测试目录的独立子目录。")
    for parent in (path, *path.parents):
        if parent == SANDBOX.parent:
            break
        if parent.is_symlink() or parent.is_junction():
            raise ValueError("验收目录不能经过目录联接。")
    return path


class TestSource:
    """TestSource（本地测试发布源）：HTTP 范围、重定向、故障与忽略范围模拟。"""
    def __init__(self, directory: Path):
        self.directory = bounded(directory)
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def handle(self):
                # 范围拒绝、取消与限额探测会主动关闭连接；下一次 keep-alive（复用连接）读取也可能收到重置。
                try:
                    super().handle()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

            def do_GET(self):
                route = unquote(urlsplit(self.path).path).lstrip("/")
                if route.startswith("redirect/"):
                    self.send_response(302)
                    self.send_header("Location", "/" + route.removeprefix("redirect/"))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                ignore_range = route.startswith("no-range/")
                if ignore_range:
                    route = route.removeprefix("no-range/")
                fail = route.startswith("wrong-range/")
                if fail:
                    route = route.removeprefix("wrong-range/")
                path = (owner.directory / route).resolve()
                if not path.is_relative_to(owner.directory) or not path.is_file():
                    self.send_error(404)
                    return
                size = path.stat().st_size
                start, end, status = 0, size - 1, 200
                header = self.headers.get("Range", "")
                if header and not ignore_range:
                    match = re.fullmatch(r"bytes=(\d+)-(\d+)", header)
                    if not match:
                        self.send_error(416)
                        return
                    start, end = map(int, match.groups())
                    end = min(end, size - 1)
                    if not 0 <= start <= end:
                        self.send_error(416)
                        return
                    status = 206
                self.send_response(status)
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Content-Type", "application/octet-stream")
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start + int(fail)}-{end}/{size}")
                self.end_headers()
                sent = 0
                try:
                    with path.open("rb") as incoming:
                        incoming.seek(start)
                        remaining = end - start + 1
                        while remaining:
                            block = incoming.read(min(65536, remaining))
                            if not block:
                                break
                            self.wfile.write(block)
                            sent += len(block)
                            remaining -= len(block)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                with owner.lock:
                    owner.requests.append({"path": route, "range": header, "status": status, "sent_bytes": sent})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.worker.start()
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def prepare(root: Path, release: Path) -> tuple[Path, Path, Path]:
    root = bounded(root)
    root.mkdir(parents=True, exist_ok=True)
    stamp = json.loads((release / "build-stamp.json").read_text(encoding="utf-8-sig"))
    if stamp.get("launch_channel") != "distribution" or not stamp.get("qualification"):
        raise ValueError("验收只接受独立分发渠道验收构建。")
    catalog = json.loads((release / "catalog.json").read_text(encoding="utf-8-sig"))
    source = root / "发布源"
    source.mkdir(exist_ok=True)
    for package in catalog["packages"]:
        records = package.get("parts") or [package["archive"]]
        for record in records:
            name = record["url"]
            if Path(name).name != name:
                raise ValueError("验收清单必须使用固定附件名。")
            origin, target = release / name, source / name
            if not target.is_file() or target.stat().st_size != origin.stat().st_size:
                shutil.copyfile(origin, target)
    executable = root / "入口 A" / "VoiceTerminal.exe"
    executable.parent.mkdir(exist_ok=True)
    shutil.copyfile(release / "VoiceTerminal.exe", executable)
    write_json(root / "prepare-result.json", {"channel": "distribution", "root": str(root), "ready": True,
        "fields": {"channel": "分发渠道", "root": "验收专用目录", "ready": "本地发布样本已准备"}})
    return root, source, executable


def run_native(executable: Path, config: Path, extra: list[str] | None = None, timeout=1200):
    result = subprocess.run([str(executable), "--qualification-config", str(config), *(extra or [])],
        cwd=executable.parent, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        creationflags=0x08000000)
    return result


def native_transfer_experiments(root: Path, executable: Path, config: Path, source: TestSource):
    data = bytes(range(256)) * (6 * 1024 * 1024 // 256)
    payload = source.directory / "mock.bin"
    payload.write_bytes(data)
    expected = hashlib.sha256(data).hexdigest()
    fixture = root / "mock-download.json"
    spec = {"url": f"{source.url}/redirect/mock.bin", "size": len(data), "sha256": expected,
            "sha512": "", "path": "mock.bin"}
    write_json(fixture, spec)
    workspace = Path(json.loads(config.read_text(encoding="utf-8"))["workspace_root"])
    target = workspace / "tmp/download-fixture.bin"
    for path in (target, Path(str(target) + ".part"), Path(str(target) + ".part.identity")):
        path.unlink(missing_ok=True)
    cancelled = run_native(executable, config, ["--download-fixture", str(fixture), "--cancel-after-bytes", str(4 * 1024 * 1024 + 1)])
    part = Path(str(target) + ".part")
    assert cancelled.returncode != 0 and part.stat().st_size == 4 * 1024 * 1024
    resumed = run_native(executable, config, ["--download-fixture", str(fixture)])
    assert resumed.returncode == 0 and hashlib.sha256(target.read_bytes()).hexdigest() == expected
    for route in ("no-range", "wrong-range"):
        target.unlink()
        spec["url"] = f"{source.url}/{route}/mock.bin"
        write_json(fixture, spec)
        response = run_native(executable, config, ["--download-fixture", str(fixture)])
        assert (response.returncode == 0) == (route == "no-range")
    target.unlink(missing_ok=True)
    spec["url"] = f"{source.url}/mock.bin"
    spec["sha256"] = "0" * 64
    write_json(fixture, spec)
    rejected = run_native(executable, config, ["--download-fixture", str(fixture)])
    assert rejected.returncode != 0 and not target.exists()
    return {"resume_after_restart": True, "redirect": True, "range_ignored_fallback": True,
            "wrong_range_rejected": True, "bad_digest_rejected": True}


def model_probes(root: Path, executable: Path, config: Path):
    catalog = json.loads((REPOSITORY / "config/resources/catalog.json").read_text(encoding="utf-8"))
    urls = []
    for resource in catalog["resources"]:
        source = resource["source"]
        if source.get("platform") == "huggingface":
            candidates = [item for item in resource["files"] if not item.get("asset")]
            file = next((item for item in candidates if item["path"].endswith((".json", ".yaml"))), candidates[0])
            urls.append((resource["resource_id"], f"https://huggingface.co/{source['repo_id']}/resolve/{source['revision']}/{file.get('source_path',file['path'])}"))
        elif source.get("platform") == "modelscope":
            file = next((item for item in resource["files"] if item["path"] == "configuration.json"), resource["files"][0])
            query = urlencode({"Revision": source["revision"], "FilePath": file.get("source_path", file["path"])})
            urls.append((resource["resource_id"], f"https://modelscope.cn/api/v1/models/{source['repo_id']}/repo?{query}"))
        elif source.get("transfer", {}).get("artifact", {}).get("url"):
            urls.append((resource["resource_id"], source["transfer"]["artifact"]["url"]))
    results = []
    total_reserved = 0
    for resource_id, url in urls:
        if total_reserved + 65536 > 1024 * 1024:
            break
        total_reserved += 65536  # 包括失败请求在内，按单次最大正文预留预算。
        try:
            result = run_native(executable, config, ["--probe-url", url], timeout=70)
        except subprocess.TimeoutExpired:
            results.append({"resource_id": resource_id, "ok": False, "read_bytes": None, "error": "探测超时，受测进程已停止。"})
            continue
        actual = None
        record = Path(json.loads(config.read_text(encoding="utf-8"))["workspace_root"]) / "data/state/last-probe.json"
        if result.returncode == 0:
            actual = json.loads(record.read_text(encoding="utf-8"))["bytes"]
            assert 0 <= actual <= 65536
        results.append({"resource_id": resource_id, "ok": result.returncode == 0, "read_bytes": actual,
                        "error": (result.stderr.strip()[-500:] or "原生探测失败") if result.returncode else None})
    return {"results": results, "reserved_body_bytes": total_reserved, "limit_body_bytes": 1024 * 1024,
            "actual_body_bytes": sum(row["read_bytes"] or 0 for row in results),
            "fields": {"results": "逐资源连接探测结果", "resource_id": "固定资源编号", "ok": "探测是否成功",
                       "read_bytes": "实际读取正文的字节数", "error": "安全错误说明", "reserved_body_bytes": "包括失败请求的最大正文预留",
                       "limit_body_bytes": "本轮模型探测总正文上限", "actual_body_bytes": "成功请求实际读取正文合计"}}


def main():
    parser = argparse.ArgumentParser(description="分发安装验收与受限下载实验")
    parser.add_argument("--root", type=Path, default=SANDBOX / "分发验收 0.8.0")
    parser.add_argument("--release", type=Path, default=REPOSITORY / ".local/releases/qualification")
    parser.add_argument("--serve", action="store_true", help="供原生界面验收持续运行本地发布源")
    parser.add_argument("--probe-models", action="store_true", help="单次 64 KiB、总计 1 MiB 的模型连接实验")
    parser.add_argument("--transfers", action="store_true", help="本地文件续传与错误恢复实验")
    args = parser.parse_args()
    root, directory, executable = prepare(args.root, args.release)
    source = TestSource(directory).start()
    workspace = bounded(root / "工作区 中文和空格")
    config = root / "qualification.json"
    settings = {"registry": str(root / "定位记录/workspace.json"), "instance_tag": "qualification-0.8.0",
                "source_base": source.url, "headless": not args.serve}
    if not args.serve:
        settings["workspace_root"] = str(workspace)
    else:
        workspace.mkdir(exist_ok=True)
    write_json(config, settings)
    print(json.dumps({"source_base": source.url, "config": str(config), "entry": str(executable), "workspace": str(workspace)}, ensure_ascii=False), flush=True)
    try:
        if args.serve:
            # stop-local-source（本地发布源停止标记）只属于当前测试目录，便于正常写出请求记录。
            stop_file = root / "stop-local-source"
            stop_file.unlink(missing_ok=True)
            while not stop_file.exists():
                time.sleep(.2)
        else:
            initialized = run_native(executable, config)
            (root / "initialization.log").write_text(initialized.stdout + initialized.stderr, encoding="utf-8")
            assert initialized.returncode == 0, initialized.stderr
            results = {"initialization": True}
            if args.transfers:
                results["native_transfer"] = native_transfer_experiments(root, executable, config, source)
            if args.probe_models:
                results["model_probe"] = model_probes(root, executable, config)
            moved = root / "入口 B/移动后的语音终端.exe"
            moved.parent.mkdir(exist_ok=True)
            shutil.copyfile(executable, moved)
            assert run_native(moved, config).returncode == 0
            results["entry_moved"] = True
            write_json(root / "distribution-qualification.json", {"results": results,
                "fields": {"results": "验收结果", "initialization": "原生初始化", "native_transfer": "本地下载恢复", "model_probe": "受限模型连接", "entry_moved": "入口移动后定位"}})
            print(json.dumps(results, ensure_ascii=False), flush=True)
    finally:
        source.close()
        write_json(root / "local-source-requests.json", {"requests": source.requests,
            "fields": {"requests": "本地发布源请求", "path": "附件名", "range": "范围请求", "status": "响应状态", "sent_bytes": "本地发送正文"}})


if __name__ == "__main__":
    main()
