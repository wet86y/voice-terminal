"""Standalone launcher（独立启动入口）：桌面受管或无界面后端模式。"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import queue
import shutil
import sys
import threading
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


def parse_device_pairing_console_command(line: str) -> dict:
    """parse_device_pairing_console_command（解析主机本地配对命令）：仅接受现有 supervisor 操作。"""
    if not isinstance(line, str) or len(line.encode("utf-8")) > 16_384:
        raise ValueError("设备配对控制命令超过长度上限。")
    try:
        payload = json.loads(line)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("设备配对控制命令不是有效 JSON。") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("operation"), str):
        raise ValueError("设备配对控制命令必须包含 operation（操作类型）。")
    operation = payload["operation"]
    expected = {
        "exit": {"operation"},
        "status": {"operation"},
        "open": {"operation", "ttl_seconds"},
        "close": {"operation"},
        "approve": {"operation", "request_id"},
        "reject": {"operation", "request_id"},
        "revoke": {"operation", "device_id"},
    }.get(operation)
    if expected is None or set(payload) != expected:
        raise ValueError("设备配对命令类型或字段不匹配。")
    if operation == "open" and (type(payload["ttl_seconds"]) is not int or not 30 <= payload["ttl_seconds"] <= 600):
        raise ValueError("配对窗口时长必须为 30 至 600 秒。")
    if operation in {"approve", "reject"}:
        request_id = payload["request_id"]
        if (not isinstance(request_id, str) or len(request_id) != 32
                or any(char not in "0123456789abcdef" for char in request_id)):
            raise ValueError("待处理配对请求编号无效。")
    if operation == "revoke":
        device_id = payload["device_id"]
        if (not isinstance(device_id, str) or not 1 <= len(device_id) <= 128
                or device_id.strip() != device_id
                or any(ord(char) < 32 or ord(char) == 127 for char in device_id)):
            raise ValueError("待撤销设备编号无效。")
    return payload


async def run_device_pairing_console(app: ApplicationHost, *, startup_timeout: float = 180.0) -> None:
    """run_device_pairing_console（无界面主机配对控制台）：仅经 supervisor 访问受管回环端点。"""
    lines: queue.Queue[str] = queue.Queue(maxsize=4)

    def read_input() -> None:
        while True:
            line = sys.stdin.readline(16_385)
            try:
                lines.put(line, timeout=1.0)
            except queue.Full:
                return
            if not line:
                return

    input_thread = threading.Thread(target=read_input, name="device-pairing-console-input", daemon=True)
    input_thread.start()
    try:
        try:
            deadline = time.monotonic() + startup_timeout
            while time.monotonic() < deadline:
                snapshot = app.state.snapshot()
                if app.exit_event.is_set() or snapshot.get("stopping"):
                    raise RuntimeError("主机应用在设备配对控制台就绪前开始退出。")
                if snapshot.get("startup_complete") and snapshot.get("voice_api_url"):
                    if (snapshot.get("device_gateway_enabled") is not True
                            or snapshot.get("device_gateway_ready") is not True):
                        raise RuntimeError("共享设备网关未就绪；配对控制台未启动。")
                    break
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("主机核心未能在时限内就绪；配对控制台未启动。")
        except Exception as exc:
            sys.stdout.write(json.dumps({
                "ok": False, "error": str(exc)[:240],
                "fields": {"ok": "控制台是否就绪", "error": "就绪失败原因"},
            }, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
            raise

        ready = {
            "state": "ready",
            "fields": {
                "state": "无界面主机配对控制台状态",
                "input": "每行一条 JSON（结构化数据）命令；只提供 status/open/close/approve/reject/revoke/exit",
            },
        }
        sys.stdout.write(json.dumps(ready, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()

        while not app.exit_event.is_set():
            try:
                line = lines.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.05)
                continue
            if not line:
                break
            if len(line.encode("utf-8")) > 16_384:
                sys.stdout.write(json.dumps({
                    "ok": False, "error": "设备配对命令超过长度上限。",
                    "fields": {"ok": "命令是否成功", "error": "拒绝超长命令的原因"},
                }, ensure_ascii=False, separators=(",", ":")) + "\n")
                sys.stdout.flush()
                break
            try:
                payload = parse_device_pairing_console_command(line.strip())
                if payload["operation"] == "exit":
                    break
                result = await asyncio.to_thread(app.device_pairing, payload)
                response = {"ok": True, "operation": payload["operation"], "result": result,
                            "fields": {"ok": "本次操作是否获得确认", "operation": "执行的配对管理操作",
                                        "result": "受管 ApplicationSupervisor 返回的主机结果"}}
            except (TypeError, ValueError, RuntimeError) as exc:
                response = {"ok": False, "error": str(exc)[:240],
                            "fields": {"ok": "本次操作是否获得确认", "error": "操作拒绝或失败原因"}}
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    finally:
        app.request_exit()


async def run(args, root):
    # 首帧由原生宿主写入私有管道；连续会话不会进入命令参数、环境、登记文件或日志。
    continuity = None
    if getattr(args, "desktop_managed", False):
        from .continuity import read_start
        continuity = await asyncio.to_thread(read_start, sys.stdin)
    app = ApplicationHost(root, python=args.python, config=args.config,
        no_worker=args.no_worker, no_analysis=args.no_analysis, llm_provider=args.llm_provider,
        share_device_gateway=args.share_device_gateway)
    app.loop = asyncio.get_running_loop()
    app.dev = args.dev
    app.headless = args.headless
    if args.device_pairing_console and not app._shared_host_role_is_saved():
        raise RuntimeError("设备配对控制台要求本机已通过正式配置保存 shared_host（共享主机）角色。")
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
    host = HostWeb(app, frontend, port=host_preferred_port, continuity=continuity)
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
            if continuity:
                from .continuity import loopback_port
                app.frontend_port = loopback_port(continuity["frontend_origin"])
            process = app.spawn("dev", [node, str(helper), "--root", str(root), "--mode", "product"])
            app.frontend_origin = _wait_for_frontend(app, process)
            if continuity and app.frontend_origin != continuity["frontend_origin"]:
                raise RuntimeError("恢复后的开发界面来源与当前窗口不匹配。")
            app.state.set_endpoint("web", app.frontend_origin,
                preferred_url="http://127.0.0.1:5173", generation=app.state.value["instance_id"],
                pid=process.pid, created_at=process.created_at)
            app.state.component("web", state="ready", phase="开发界面已完成本实例握手",
                pid=process.pid, created_at=process.created_at, shared_with=None, error="")
        except Exception as exc:
            app.stop_group("dev")
            if continuity:
                host.close()
                raise RuntimeError("无法恢复当前开发界面来源。") from exc
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
    # startup-result（启动结果）只属于当前活动实例，退出后不留陈旧的 ready（就绪）快照。
    startup_result_path = app.paths.application_state.parent / "startup-result.json"
    if not startup_result_path.is_symlink() and not getattr(startup_result_path, "is_junction", lambda: False)():
        startup_result_path.unlink(missing_ok=True)
    pairing_console_task = None
    try:
        if getattr(args, "desktop_managed", False):
            from .desktop import DesktopControl
            desktop = DesktopControl(app)
            desktop.owned.set()  # 首帧已确认原生宿主接管。
            desktop.start(app.frontend_origin, host.issue_launch_token(), continuity={
                "host_origin": host.origin, "frontend_origin": app.frontend_origin,
                "cookie_name": host.cookie_name, "session": host.session,
            })
            await desktop.owned.wait()
        if not args.diagnostic and not app.exit_event.is_set():
            app.enqueue(app.startup())
            app.enqueue(app.monitor())
        if args.device_pairing_console:
            pairing_console_task = asyncio.create_task(
                run_device_pairing_console(app), name="device-pairing-console")
        reported_startup = False
        reported_runtime = None  # reported_runtime（最近向桌面上报的核心能力与整体状态）。
        reported_endpoints = -1  # reported_endpoints（已发送给桌面的端点版本）。
        while not app.exit_event.is_set():
            status = app.state.snapshot()
            current_runtime = (status["core_available"], status["state"],
                status["device_gateway_enabled"], status["device_gateway_ready"], status["device_gateway_error"])
            if desktop and status["startup_complete"] and current_runtime != reported_runtime:
                desktop.send("runtime_status", core_available=status["core_available"], state=status["state"],
                    device_gateway_enabled=status["device_gateway_enabled"],
                    device_gateway_ready=status["device_gateway_ready"],
                    device_gateway_error=status["device_gateway_error"])
                reported_runtime = current_runtime
            if desktop and status["endpoint_revision"] != reported_endpoints:
                # 端点均由宿主核验过进程归属；私有管道只传不含令牌的来源。
                desktop.send("endpoints", endpoints=status["endpoints"], endpoint_revision=status["endpoint_revision"])
                reported_endpoints = status["endpoint_revision"]
            if status["startup_complete"] and not reported_startup:
                # startup-result（启动快照）字段带同一进程身份，供磁盘状态核对，不作为实时状态来源。
                result = {"schema": 1, "instance_id": status["instance_id"], "state": status["state"],
                    "core_available": status["core_available"], "pid": os.getpid(),
                    "device_gateway_enabled": status["device_gateway_enabled"],
                    "device_gateway_ready": status["device_gateway_ready"],
                    "device_gateway_error": status["device_gateway_error"],
                    "created_at": host_created_at, "url": app.frontend_origin, "checked_at": time.time()}
                # startup-result（启动结果快照）是启动结束时的历史快照，不能视作实时运行状态。
                atomic_json(startup_result_path, result)
                if desktop:
                    desktop.send("startup_result", **result)
                reported_startup = True
            try:
                await asyncio.wait_for(app.exit_event.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
    finally:
        if pairing_console_task is not None:
            if not pairing_console_task.done():
                app.request_exit()
                try:
                    await asyncio.wait_for(pairing_console_task, timeout=2.0)
                except asyncio.TimeoutError:
                    pairing_console_task.cancel()
                    await asyncio.gather(pairing_console_task, return_exceptions=True)
            else:
                try:
                    pairing_console_task.result()
                except Exception as exc:
                    logging.error("设备配对控制台退出：%s", exc)
        await app.shutdown()
        if desktop:
            desktop.send("stopped")
        await asyncio.sleep(.3)  # 让 SSE（服务器推送事件）把已退出状态送达已打开页面。
        host.close()
        registry.unlink(missing_ok=True)
        remove_owned_startup_result(startup_result_path, app.state.value["instance_id"])


def remove_owned_startup_result(path: Path, instance_id: str) -> None:
    """remove_owned_startup_result（清理瞬时快照）：只移除本实例的有界普通文件，保护新实例和异常原件。"""
    try:
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)() or not path.is_file() or path.stat().st_size > 16_384:
            return
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("instance_id") == instance_id and value.get("pid") == os.getpid():
            path.unlink(missing_ok=True)
    except (OSError, UnicodeError, ValueError):
        pass


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
    parser.add_argument("--device-pairing-console", action="store_true",
                        help="无界面主机本地配对管理；要求已保存的共享主机角色并使用 JSON 行命令")
    parser.add_argument("--share-device-gateway", action="store_true",
                        help="为已保存的 shared_host（共享主机）角色启用受保护的回环设备网关")
    parser.add_argument("--no-worker", action="store_true", help="不启动 ASR worker（语音识别工作进程）")
    parser.add_argument("--no-analysis", action="store_true", help="不启动 analysis worker（分析工作进程）")
    parser.add_argument("--dev", action="store_true", help="使用受管 Node.js 开发界面，仅桌面模式可用")
    args = parser.parse_args(argv)
    if args.dev and not args.desktop_managed:
        parser.error("--dev（开发界面）只能与 --desktop-managed（桌面受管）一起使用")
    if args.diagnostic and not args.desktop_managed:
        parser.error("--diagnostic（桌面诊断）只能与 --desktop-managed（桌面受管）一起使用")
    if args.device_pairing_console and (not args.headless or args.diagnostic or args.dev):
        parser.error("--device-pairing-console（主机配对控制台）只能与正式 --headless（无界面）入口一起使用")
    if args.device_pairing_console and not args.share_device_gateway:
        parser.error("--device-pairing-console（主机配对控制台）要求同时启用 --share-device-gateway（共享设备网关）")
    if args.share_device_gateway and not (args.desktop_managed or args.headless):
        parser.error("--share-device-gateway（共享设备网关）必须由正式受管桌面或无界面入口启动")
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
