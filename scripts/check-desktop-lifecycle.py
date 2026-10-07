"""Desktop lifecycle acceptance（桌面生命周期验收）：磁盘登记与所属进程资源检查。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import psutil


def identity(process):
    """identity（进程身份）：同时用 PID（进程标识）与创建时间抵御 PID 复用。"""
    return {"pid": process.pid, "created_at": process.create_time(), "name": process.name()}


def alive(record):
    try:
        return abs(psutil.Process(record["pid"]).create_time() - record["created_at"]) < 0.01
    except psutil.Error:
        return False


def app_layout(root: Path, data: Path, portable: dict, record: dict):
    """app_layout（本实例程序布局）：校验登记中的管理进程确由本次桌面程序建立。"""
    origin = record["url"]
    from urllib.parse import urlsplit
    address = urlsplit(origin)
    if (address.scheme != "http" or address.hostname != "127.0.0.1" or address.path not in ("", "/")
            or address.username or address.password or address.query or address.fragment
            or not address.port or not 1 <= address.port <= 65535):
        raise ValueError("实例登记不是本地界面地址")
    selected = portable
    program = (root / selected["app"]).resolve(strict=True)
    program.relative_to(root)
    manager = psutil.Process(record["pid"])
    if abs(manager.create_time() - float(record["created_at"])) >= 0.01:
        raise ValueError("登记的管理进程创建时间与当前 PID 不匹配")
    command = manager.cmdline()
    if not any("voice_terminal.application.launch" == value for value in command) or not any(
            str(program).casefold() in value.casefold() for value in command):
        raise ValueError("登记的管理进程不属于指定程序目录")
    native = manager.parent()
    if not native or Path(native.exe()).resolve() != program / "VoiceTerminalDesktop.exe":
        raise ValueError("登记的父进程不是本实例桌面程序")
    return program, manager, native, origin


def append_process(records: list[dict], process) -> None:
    try:
        current = identity(process)
    except psutil.Error:
        return
    if not any(row["pid"] == current["pid"] and row["created_at"] == current["created_at"] for row in records):
        records.append(current)


def owned_processes(native, manager) -> list[dict]:
    records: list[dict] = []
    append_process(records, native)
    append_process(records, manager)
    try:
        for process in native.children(recursive=True):
            append_process(records, process)
    except psutil.Error:
        pass
    return records


def startup_result(data: Path, record: dict):
    """返回同一管理进程写出的历史启动快照；该内容不代表当前实时应用状态。"""
    path = data / "state/startup-result.json"
    if not path.is_file():
        return None, False
    try:
        result = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None, False
    try:
        belongs = (result.get("instance_id") == record.get("instance_id")
            and result.get("pid") == record.get("pid")
            and abs(float(result.get("created_at", 0)) - float(record.get("created_at", -1))) < 0.01)
    except (TypeError, ValueError):
        return result, False
    return result, belongs


def sample_processes(native, manager):
    measurements = {}
    processes = [native, manager]
    try:
        processes.extend(native.children(recursive=True))
    except psutil.Error:
        pass
    for process in processes:
        try:
            key = (process.pid, process.create_time())
            memory = process.memory_info()
            measurements[key] = (memory.rss, getattr(memory, "private", None))
        except psutil.Error:
            continue
    rss = sum(row[0] for row in measurements.values())
    private = sum(row[1] for row in measurements.values()) if all(row[1] is not None for row in measurements.values()) else None
    return measurements, rss, private


def main():
    parser = argparse.ArgumentParser(description="检查本实例启动快照与受管进程资源，不访问或停止其他应用")
    parser.add_argument("--root", type=Path, required=True, help="已组装的应用根目录")
    parser.add_argument("--action", choices=("launch", "status", "exit", "crash", "soak"), default="status",
        help="action（验收动作）：启动、读取登记、等待真实退出、崩溃回收或长时间采样")
    parser.add_argument("--cwd", type=Path, help="启动工作目录，验证入口不依赖当前位置")
    parser.add_argument("--seconds", type=int, default=3600, help="soak（长时间采样）的秒数")
    parser.add_argument("--timeout", type=int, default=300, help="exit（等待用户桌面操作）的最长秒数")
    parser.add_argument("--report", type=Path, required=True, help="不含凭据或用户会话内容的诊断报告")
    parser.add_argument("--restricted-path", action="store_true", help="启动时仅保留 Windows（操作系统）内置程序目录")
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    portable = json.loads((root / "portable.json").read_text(encoding="utf-8-sig"))
    data = Path(portable["data_dir"])
    data = (data if data.is_absolute() else root / data).resolve()
    if args.action == "launch":
        environment = dict(os.environ)
        if args.restricted_path:
            system = Path(os.environ["SystemRoot"])
            environment["PATH"] = os.pathsep.join(map(str, (system / "System32", system, system / "System32/WindowsPowerShell/v1.0")))
        subprocess.Popen([str(root / "VoiceTerminal.exe")], cwd=args.cwd or root, env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    registry = data / "state/application/instance.json"
    deadline = time.monotonic() + (45 if args.action == "launch" else 0)
    while True:
        try:
            record = json.loads(registry.read_text(encoding="utf-8-sig"))
            if not all(key in record for key in ("pid", "created_at", "url", "instance_id")):
                raise ValueError("实例登记缺少 PID、创建时间、地址或实例标识")
            program, manager, native, origin = app_layout(root, data, portable, record)
            break
        except (OSError, psutil.Error, ValueError, KeyError, TypeError) as error:
            if args.action != "launch" or time.monotonic() >= deadline:
                raise RuntimeError("无法确认当前实例归属：" + str(error)) from error
            time.sleep(0.25)

    owned = owned_processes(native, manager)
    snapshot, snapshot_matches = startup_result(data, record)
    result = {
        "schema": 2, "action": args.action, "instance_id": record["instance_id"], "origin": origin,
        "native_pid": native.pid, "manager_pid": manager.pid, "processes": owned, "samples": [],
        "startup_result_is_historical": True,
        "startup_result_matches_instance": snapshot_matches,
        "startup_result_checked_at": snapshot.get("checked_at") if snapshot_matches and snapshot else None,
        "startup_result_state": snapshot.get("state") if snapshot_matches and snapshot else None,
        "fields": {
            "schema": "诊断报告格式版本",
            "action": "本次验收动作",
            "created_at": "进程创建时间（秒，用于识别 PID 复用）",
            "instance_id": "应用实例标识",
            "origin": "本实例登记的本地界面来源",
            "native_pid": "桌面主程序进程标识",
            "manager_pid": "Python 服务管理进程标识",
            "startup_result_is_historical": "启动结果是历史快照，不代表实时状态",
            "startup_result_matches_instance": "启动快照的实例标识、PID 与创建时间是否均匹配",
            "startup_result_checked_at": "启动快照写入时间（秒）",
            "startup_result_state": "启动结束时的状态，不是当前状态",
            "processes": "按进程标识和创建时间确认的本实例进程",
            "pid": "进程标识",
            "name": "进程名称",
            "samples": "仅对本实例受管进程进行的资源采样",
            "rss_bytes": "常驻内存总字节，含共享页重复计入",
            "private_commit_bytes": "私有提交内存总字节",
            "process_count": "本次资源样本中读取成功的进程数",
            "elapsed_seconds": "测量经过秒数",
            "remaining": "退出后仍存在的同一身份进程",
        },
    }
    started = time.monotonic()
    if args.action in ("exit", "crash"):
        if args.action == "exit":
            print("请在桌面窗口或托盘中执行实际退出操作；验收脚本会等待本实例进程回收。", flush=True)
            end = started + max(1, args.timeout)
            while time.monotonic() < end:
                for process in (native, manager):
                    try:
                        for child in process.children(recursive=True):
                            append_process(owned, child)
                    except psutil.Error:
                        pass
                if not any(alive(process) for process in owned):
                    break
                time.sleep(0.2)
        else:
            native.kill()  # 仅结束经路径、父子关系及登记身份确认的本次桌面外壳。
        while any(alive(process) for process in owned) and time.monotonic() - started < 20:
            time.sleep(0.1)
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        result["remaining"] = [process for process in owned if alive(process)]
    else:
        end = started + (max(1, args.seconds) if args.action == "soak" else 0)
        while True:
            measurements, rss, private = sample_processes(native, manager)
            result["samples"].append({
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "rss_bytes": rss,
                "private_commit_bytes": private,
                "process_count": len(measurements),
            })
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            if time.monotonic() >= end:
                break
            time.sleep(min(30, end - time.monotonic()))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"action": args.action, "native_pid": native.pid, "manager_pid": manager.pid,
        "samples": len(result["samples"]), "elapsed_seconds": result.get("elapsed_seconds"),
        "remaining": result.get("remaining"), "report": str(args.report)}, ensure_ascii=False))
    if result.get("remaining"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
