"""Download sources（下载来源）：独立传输策略，不改写资源版本、收据或内容锁。"""
from __future__ import annotations

import hashlib
from http.client import HTTPException
import json
import os
import shutil
from pathlib import Path
import subprocess
import urllib.request
from urllib.parse import quote, unquote, urlsplit


def policy() -> dict:
    """policy（统一配置）：从当前源码或发行程序目录读取。"""
    return json.loads((Path(__file__).resolve().parents[1] / "config/download-sources.json").read_text("utf-8-sig"))


def seed_tokenizer_cache() -> Path:
    """seed_tokenizer_cache（离线词表缓存）：先验证随包文件，损坏时拒绝隐式联网。"""
    root = Path(__file__).resolve().parents[1]
    record = policy()["tokenizer"]
    asset = root / record["asset"]
    if hashlib.sha256(asset.read_bytes()).hexdigest() != record["sha256"]:
        raise ValueError("随包分词词表摘要不符")
    from .paths import AppPaths
    cache = Path(os.environ.get("TIKTOKEN_CACHE_DIR") or AppPaths.resolve().install_root / "cache/tiktoken")
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / hashlib.sha1(record["url"].encode()).hexdigest()
    if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != record["sha256"]:
        import tempfile
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as output:
            temporary = Path(output.name)
            try:
                with asset.open("rb") as incoming:
                    shutil.copyfileobj(incoming, output)
            except BaseException:
                output.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    os.environ["TIKTOKEN_CACHE_DIR"] = str(cache)
    return target


def candidates(url: str) -> list[str]:
    """candidates（候选地址）：只改已声明的公开源，更新清单始终直连。"""
    if urlsplit(url).path.endswith("/update.json"):
        return [url]
    config = policy()
    result = []
    for row in config["models"]:
        if url.startswith(row["origin_prefix"]):
            path = quote(unquote(url[len(row["origin_prefix"]):]), safe="")
            result.append(f'https://modelscope.cn/api/v1/models/{row["repo_id"]}/repo?Revision={row["revision"]}&FilePath={path}')
    for row in config["prefixes"]:
        if url.startswith(row["origin_prefix"]):
            result.append(row["mirror_prefix"] + url[len(row["origin_prefix"]):])
    return list(dict.fromkeys([*result, url]))


def fetch(url: str, *, head: bool = False, decode_json: bool = False) -> bytes | dict:
    """fetch（有界元数据读取）：网络或解析错误由调用方按需要处理。"""
    errors = []
    for source in candidates(url):
        host = urlsplit(source).hostname or "未知来源"
        try:
            print(f"下载来源：{host}", flush=True)
            with urllib.request.urlopen(urllib.request.Request(source, method="HEAD" if head else "GET"), timeout=20) as response:
                if not head and "text/html" in response.headers.get("Content-Type", "").lower():
                    raise ValueError("来源返回网页，未返回资源文件")
                if head:
                    return dict(response.headers)
                body = response.read()
                return json.loads(body) if decode_json else body
        except (OSError, ValueError, HTTPException) as error:
            errors.append(f"{host}：{type(error).__name__}")
            print(f"来源失败，尝试备用来源：{host}（{type(error).__name__}）", flush=True)
    raise OSError("所有下载来源均失败：" + "；".join(errors))


def download(url: str, destination: Path, *, sha256: str | None = None,
             size: int | None = None, sha512: str | None = None) -> str:
    """download（原子下载）：候选文件逐源重下，校验失败不启用；不吞掉用户中断。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(destination.name + ".part")
    errors = []
    for source in candidates(url):
        host = urlsplit(source).hostname or "未知来源"
        try:
            print(f"下载来源：{host}", flush=True)
            digest = hashlib.sha512() if sha512 else hashlib.sha256()
            count = 0
            with urllib.request.urlopen(source, timeout=20) as response, part.open("wb") as output:
                if "text/html" in response.headers.get("Content-Type", "").lower():
                    raise ValueError("来源返回网页，未返回资源文件")
                while block := response.read(1024 * 1024):
                    count += len(block)
                    if size is not None and count > size:
                        raise ValueError("下载内容超出锁定大小")
                    output.write(block)
                    digest.update(block)
            expected = sha512 or sha256
            if size is not None and count != size or expected and digest.hexdigest() != expected:
                raise ValueError("下载文件大小或摘要不符")
            part.replace(destination)
            return digest.hexdigest()
        except (OSError, ValueError, HTTPException) as error:
            part.unlink(missing_ok=True)
            errors.append(f"{host}：{type(error).__name__}")
            print(f"来源失败，尝试备用来源：{host}（{type(error).__name__}）", flush=True)
    raise OSError("所有下载来源均失败：" + "；".join(errors))


def npm_registry(env: dict[str, str], cwd: Path | None = None) -> str:
    """npm_registry（选定注册表）：环境、项目、用户、全局配置依次查找，不输出认证内容。"""
    lower = {key.lower(): value for key, value in env.items()}
    if lower.get("npm_config_registry"):
        return lower["npm_config_registry"].rstrip("/")
    home = Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())
    configs = [(cwd or Path.cwd()) / ".npmrc", Path(lower.get("npm_config_userconfig") or home / ".npmrc")]
    if lower.get("npm_config_globalconfig"):
        configs.append(Path(lower["npm_config_globalconfig"]))
    for config in configs:
        if config.is_file():
            registry = ""
            for line in config.read_text("utf-8-sig").splitlines():
                key, separator, value = line.partition("=")
                if separator and key.strip().lower() == "registry":
                    registry = value.strip().strip('"\'').rstrip("/")
            if registry:
                return registry
    return policy()["npm_registries"][0]


def npm_environment(env: dict[str, str], cwd: Path | None = None) -> dict[str, str]:
    """npm_environment（包工具子进程环境）：公开源可镜像；私有源保持独立。"""
    result = dict(env)
    registry = npm_registry(env, cwd)
    public = policy()["npm_registries"]
    if registry and registry.rstrip("/") not in public:
        return result
    for key in list(result):
        if key.lower() in {"npm_config_registry", "npm_config_disturl"}:
            del result[key]
    result.update(npm_config_registry=public[0], npm_config_disturl=policy()["node_dist"])
    return result


def pip_run(command: list, *, env: dict | None = None, profile: str | None = None) -> None:
    """pip_run（构建依赖安装）：按候选索引重试，不同时混用主副索引。"""
    config = policy()
    environment = os.environ if env is None else env
    requested = next((value.rstrip("/") for key, value in environment.items() if key.lower() == "pip_index_url"), "")
    private = bool(requested and requested not in config["pypi_indexes"])
    indices = [requested] if private else config["pypi_indexes"]
    for index, source in enumerate(indices):
        args = [str(value) for value in command] + ["--index-url", source]
        if profile and not private:
            args += ["--extra-index-url", config["pytorch_indexes"][index] + "/" + profile]
        print(f"依赖索引来源：{urlsplit(source).hostname}", flush=True)
        result = subprocess.run(args, env=env)
        if result.returncode == 0:
            return
        if result.returncode < 0 or result.returncode in {130, 3221225786}:
            raise subprocess.CalledProcessError(result.returncode, args)
    raise subprocess.CalledProcessError(result.returncode, args)
