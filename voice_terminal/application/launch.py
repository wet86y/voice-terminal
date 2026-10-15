"""Standalone launcher（独立启动入口）：桌面受管或无界面后端模式。"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import time
import urllib.error
import urllib.request

from .supervisor import ApplicationHost
from .web import HostWeb
from ..log_budget import (
    BudgetedRotatingFileHandler,
    enforce_log_budget,
    protect_log_files,
    start_log_budget_watchdog,
)
from ..paths import AppPaths, atomic_json


def find_root(explicit: str = "") -> Path:
    if explicit:
        root = Path(explicit).resolve()
        if not (root / "config/default.yaml").is_file():
            raise RuntimeError("指定目录不是 Voice Terminal 项目")
        return root
    source = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
    for root in source.parents:
        if (root / "config/default.yaml").is_file() and (root / "package.json").is_file():
            return root
    raise RuntimeError("未找到项目根目录，请使用 --root 指定项目")


def _process_created_at() -> float:
    """读取宿主进程真实创建时间；created_at（进程创建时间）用于排除旧 PID 复用。"""
    try:
        import psutil
        return float(psutil.Process(os.getpid()).create_time())
    except Exception:
        return time.time()


def _frontend_listener_port(process) -> int | None:
    """Return the unique loopback listener port owned by this Node process."""
    try:
        import psutil
    except ImportError as exc:
        raise RuntimeError("缺少 psutil（进程与端口检查库），无法安全核验开发界面端口") from exc

    expected_pid = getattr(process, "pid", None)  # pid（进程标识）。
    expected_created_at = getattr(process, "created_at", None)  # created_at（进程创建时间）。
    if not isinstance(expected_pid, int) or expected_pid <= 0 or expected_created_at is None:
        raise RuntimeError("开发界面受管进程缺少 PID（进程标识）或 created_at（进程创建时间）")

    try:
        owner = psutil.Process(expected_pid)
        actual_created_at = float(owner.create_time())
        owned_connections = owner.net_connections(kind="tcp")
    except psutil.Error as exc:
        raise RuntimeError(f"无法核验开发界面受管进程 PID {expected_pid}：{exc}") from exc

    # ManagedProcess（受管进程）在创建前记录 created_at，容许调度延迟，但拒绝复用 PID。
    if abs(actual_created_at - float(expected_created_at)) > 2:
        raise RuntimeError(f"开发界面 PID {expected_pid} 的 created_at（进程创建时间）与本实例不匹配")

    def address(connection):
        local = connection.laddr
        if not local:
            return "", 0
        try:
            return local.ip, local.port
        except AttributeError:
            return local[0], local[1]

    own_listeners = [connection for connection in owned_connections
                     if connection.status == psutil.CONN_LISTEN and address(connection)[1] > 0]
    if any(address(connection)[0] != "127.0.0.1" for connection in own_listeners):
        raise RuntimeError("开发界面受管进程未按要求监听本机回环地址。")
    own_ports = {address(connection)[1] for connection in own_listeners}
    if not own_ports:
        return None
    if len(own_ports) != 1:
        raise RuntimeError("开发界面受管进程存在多个监听端口，无法验证唯一入口。")
    actual_port = next(iter(own_ports))

    # 检查系统监听表，确保端口没有第二个所有者或地址错误的监听器。
    try:
        all_connections = psutil.net_connections(kind="tcp")
    except psutil.Error as exc:
        raise RuntimeError(f"无法核验开发界面端口 {actual_port} 的监听归属：{exc}") from exc
    for connection in all_connections:
        if (connection.status != psutil.CONN_LISTEN
                or address(connection)[1] != actual_port):
            continue
        address_ip, _ = address(connection)
        if connection.pid != expected_pid:
            pid_text = str(connection.pid) if connection.pid is not None else "未知"
            raise RuntimeError(f"开发界面端口 {actual_port} 已被其他进程占用（PID {pid_text}）")
        if address_ip != "127.0.0.1":
            raise RuntimeError("开发界面受管进程未按要求监听本机回环地址。")
    return actual_port


def _wait_for_frontend(app: ApplicationHost, process, *, timeout: float = 20) -> str:
    """只接受带本实例 Cookie 的 Node 就绪握手，不以端口可连接代替服务就绪。"""
    deadline = time.monotonic() + timeout
    last_error = "开发界面尚未报告就绪"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"开发界面提前退出，退出码 {process.poll()}；请查看 application-dev.log（开发界面日志）。")
        # 先从受管 PID 的唯一监听 socket（套接字）取得端口，避免向未知服务发送 Cookie。
        port = _frontend_listener_port(process)
        if port is None:
            time.sleep(.15)
            continue
        expected_origin = f"http://127.0.0.1:{port}"
        request = urllib.request.Request(expected_origin + "/__desktop_ready", headers={
            "Cookie": f"{app.frontend_cookie_name}={app.frontend_session}",
            "Cache-Control": "no-store",
        })
        try:
            with urllib.request.urlopen(request, timeout=1) as response:
                ready = json.load(response)
            # pid（进程标识）、instance_id（实例标识）、origin（界面地址）和 frontend_mode（界面模式）共同绑定本次就绪结果。
            if (not isinstance(ready, dict) or ready.get("pid") != process.pid
                    or ready.get("instance_id") != app.state.value["instance_id"]
                    or ready.get("origin") != expected_origin
                    or ready.get("frontend_mode") != "product"):
                raise RuntimeError("开发界面就绪信息与本实例不匹配")
            return expected_origin
        except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
            last_error = str(exc)
        time.sleep(.15)
    raise RuntimeError(f"开发界面未能就绪：{last_error}；请查看 application-dev.log（开发界面日志）。")


async def run(args, root):
    app = ApplicationHost(root, python=args.python, config=args.config,
        no_worker=args.no_worker, no_analysis=args.no_analysis, llm_provider=args.llm_provider)
    app.loop = asyncio.get_running_loop()
    app.dev = args.dev
    app.headless = args.headless
    if args.headless and os.environ.get("VOICE_APP_SHUTDOWN_EVENT"):
        async def stop_headless():
            # Shutdown event（正常退出事件）：无界面宿主复用受管服务的退出约定。
            from .managed import wait_for_shutdown
            await wait_for_shutdown()
            app.request_exit()
        app.enqueue(stop_headless())
    desktop = None
    frontend = root / ".local/build/frontend" if (root / "apps/desktop/Cargo.toml").is_file() else root / "apps/frontend/dist"
    app.frontend_directory = frontend  # frontend_directory（界面路径）：服务、状态和重试共用。
    host_preferred_port = 8764 if args.dev else 5173
    host = HostWeb(app, frontend, port=host_preferred_port)
    app.origin = host.origin  # origin（宿主内部地址）：业务组件报告与内部 Bearer 校验使用此地址。
    app.frontend_origin = host.origin  # frontend_origin（实际界面地址）：启动后更新为 Node 开发界面地址。
    host_created_at = _process_created_at()
    app.state.set_endpoint("host", host.origin, preferred_url=f"http://127.0.0.1:{host_preferred_port}",
        generation=app.state.value["instance_id"], pid=os.getpid(), created_at=host_created_at)
    app.state.set_endpoint("web", None,
        preferred_url=f"http://127.0.0.1:{5173 if args.dev else host_preferred_port}",
        generation=app.state.value["instance_id"], pid=os.getpid(), created_at=host_created_at)
    host.start()
    for key in ("host", "web"):
        app.state.component(key, state="ready", phase="入口已就绪", pid=os.getpid(), created_at=host_created_at)
    if args.headless:
        app.state.component("web", state="ready", required=False, phase="无界面模式，仅保留宿主内部报告", error="")
    elif not args.dev and not (frontend / "index.html").is_file():
        missing_ui = "正式程序界面文件不完整，请修复程序本体。"
        app.frontend_error = missing_ui
        app.state.component("web", state="failed", phase="使用内置诊断页", error=missing_ui)

    if args.dev:
        try:
            node = os.environ.get("VOICE_NODE_EXECUTABLE")
            helper = root / "scripts/frontend-dev.mjs"
            if not node:
                raise RuntimeError("开发模式缺少 Node.js（JavaScript 运行环境）")
            if not helper.is_file():
                raise RuntimeError("缺少受管开发界面脚本 scripts/frontend-dev.mjs")
            app.frontend_cookie_name = host.cookie_name
            app.frontend_session = host.session
            process = app.spawn("dev", [node, str(helper), "--root", str(root), "--mode", "product"])
            app.frontend_origin = _wait_for_frontend(app, process)
            app.state.set_endpoint("web", app.frontend_origin,
                preferred_url="http://127.0.0.1:5173", generation=app.state.value["instance_id"],
                pid=process.pid, created_at=process.created_at)
            app.state.component("web", state="ready", phase="开发界面已完成本实例握手",
                pid=process.pid, created_at=process.created_at, shared_with=None, error="")
        except Exception as exc:
            app.stop_group("dev")
            app.frontend_error = str(exc)
            # 开发界面失败时保持本实例 Host（宿主）的会话受控诊断页。
            app.frontend_origin = host.origin
            app.state.set_endpoint("web", host.origin,
                preferred_url=f"http://127.0.0.1:{host_preferred_port}", generation=app.state.value["instance_id"],
                pid=os.getpid(), created_at=host_created_at)
            app.state.component("web", state="failed", phase="开发界面启动失败", error=str(exc), pid=os.getpid())

    if not args.dev:
        app.state.set_endpoint("web", host.origin,
            preferred_url=f"http://127.0.0.1:{host_preferred_port}", generation=app.state.value["instance_id"],
            pid=os.getpid(), created_at=host_created_at)

    registry = app.paths.application_state / "instance.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    # registry（实例登记）字段：pid（进程标识）、created_at（进程创建时间）、url（受管界面地址）、instance_id（实例标识）。
    atomic_json(registry, {"pid": os.getpid(), "created_at": host_created_at,
        "url": app.frontend_origin, "instance_id": app.state.value["instance_id"]})
    try:
        if getattr(args, "desktop_managed", False):
            from .desktop import DesktopControl
            desktop = DesktopControl(app)
            desktop.start(app.frontend_origin, host.issue_launch_token())
            await desktop.owned.wait()
        if not args.diagnostic and not app.exit_event.is_set():
            app.enqueue(app.startup())
            app.enqueue(app.monitor())
        reported_startup = False
        reported_endpoints = -1  # reported_endpoints（已发送给桌面的端点版本）。
        while not app.exit_event.is_set():
            status = app.state.snapshot()
            if desktop and status["endpoint_revision"] != reported_endpoints:
                # 端点均由宿主核验过进程归属；私有管道只传不含令牌的来源。
                desktop.send("endpoints", endpoints=status["endpoints"], endpoint_revision=status["endpoint_revision"])
                reported_endpoints = status["endpoint_revision"]
            if status["startup_complete"] and not reported_startup:
                # startup-result（启动快照）字段带同一进程身份，供磁盘状态核对，不作为实时状态来源。
                result = {"schema": 1, "instance_id": status["instance_id"], "state": status["state"],
                    "core_available": status["core_available"], "pid": os.getpid(),
                    "created_at": host_created_at, "url": app.frontend_origin, "checked_at": time.time()}
                # startup-result（启动结果快照）是启动结束时的历史快照，不能视作实时运行状态。
                atomic_json(app.paths.application_state.parent / "startup-result.json", result)
                if desktop:
                    desktop.send("startup_result", **result)
                reported_startup = True
            try:
                await asyncio.wait_for(app.exit_event.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
    finally:
        await app.shutdown()
        if desktop:
            desktop.send("stopped")
        await asyncio.sleep(.3)  # 让 SSE（服务器推送事件）把已退出状态送达已打开页面。
        host.close()
        registry.unlink(missing_ok=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Voice Terminal 独立应用启动程序")
    parser.add_argument("--root", default="", help="项目根目录")
    parser.add_argument("--python", default="", help="业务 Python（程序运行环境）路径")
    parser.add_argument("--config", default="config/local.yaml", help="本机配置路径")
    parser.add_argument("--llm-provider", choices=("deepseek", "opencode_go"), default="", help="LLM provider（大语言模型服务商）")
    parser.add_argument("--data-dir", type=Path, help="data directory（独立用户数据目录）")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--desktop-managed", action="store_true", help="由桌面宿主建立窗口并管理本实例")
    mode.add_argument("--headless", action="store_true", help="仅启动后端，不创建桌面窗口、托盘或浏览器")
    parser.add_argument("--diagnostic", action="store_true", help="仅验证窗口与服务入口，不启动音频和模型")
    parser.add_argument("--no-worker", action="store_true", help="不启动 ASR worker（语音识别工作进程）")
    parser.add_argument("--no-analysis", action="store_true", help="不启动 analysis worker（分析工作进程）")
    parser.add_argument("--dev", action="store_true", help="使用受管 Node.js 开发界面，仅桌面模式可用")
    args = parser.parse_args(argv)
    if args.dev and not args.desktop_managed:
        parser.error("--dev（开发界面）只能与 --desktop-managed（桌面受管）一起使用")
    if args.diagnostic and not args.desktop_managed:
        parser.error("--diagnostic（桌面诊断）只能与 --desktop-managed（桌面受管）一起使用")
    return args


def main():
    args = parse_args()
    root = find_root(args.root)
    paths = AppPaths.resolve(root, data_dir=args.data_dir)
    paths.prepare()
    os.environ.update(paths.environment())
    if paths.separated and args.config == "config/local.yaml":
        args.config = str(paths.config / "local.yaml")
    logdir = paths.logs
    logdir.mkdir(parents=True, exist_ok=True)
    handler = BudgetedRotatingFileHandler(logdir / "application.log", maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s %(message)s", force=True)
    protect_log_files(handler.baseFilename)
    enforce_log_budget(logdir, protect=[handler.baseFilename])
    start_log_budget_watchdog(logdir, protect=[handler.baseFilename])
    try:
        asyncio.run(run(args, root))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
