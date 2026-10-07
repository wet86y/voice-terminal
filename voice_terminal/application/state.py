"""Application snapshot（应用快照）与线程安全状态发布。"""
from __future__ import annotations

import copy
import threading
import time
import uuid

TERMINAL = {"ready", "disabled", "failed", "blocked", "external", "stopped"}
LABELS = {
    "host": "应用宿主", "web": "界面服务", "setup": "环境与配置检查",
    "asr": "ASR（语音识别）", "analysis": "分析工作进程",
    "voice": "Voice Server（语音服务）", "speaker": "声纹工作进程",
    "dsh": "DSH（代理执行宿主）",
}


class ApplicationState:
    def __init__(self) -> None:
        self.condition = threading.Condition(threading.RLock())
        self.value = {
            "instance_id": uuid.uuid4().hex,  # instance_id（应用实例标识）。
            "revision": 0,  # revision（状态版本）。
            "state": "starting", "phase": "建立应用入口", "started_at": time.time(),
            "startup_complete": False, "core_available": False, "stopping": False,
            "voice_api_url": "", "voice_ws_url": "", "capabilities": {},
            "voice_api_ready": False,  # voice_api_ready（轻量业务接口已监听，独立于模型与会话就绪）。
            "sampled_at": None, "total_memory_rss_bytes": None,
            "total_memory_private_commit_bytes": None,  # 去重后的受管进程私有提交内存（字节）。
            "total_gpu_dedicated_bytes": None,  # total_gpu_dedicated_bytes（受管进程专用显存字节总量）。
            "resource_processes": [],  # resource_processes（本次采样的去重进程明细，供界面分组）。
            "components": [self.new_component(key) for key in LABELS],
        }

    @staticmethod
    def new_component(key: str) -> dict:
        return {
            "id": key, "name": LABELS[key], "state": "waiting", "phase": "等待启动",
            "error": "", "enabled": True, "required": key in {"host", "web", "setup", "asr", "voice"},
            "pid": None, "created_at": None, "uptime_seconds": None,
            "memory_rss_bytes": None, "cpu_percent": None, "sampled_at": None,
            "memory_private_commit_bytes": None,  # 进程私有提交内存（字节），不等于物理占用。
            "gpu_dedicated_bytes": None,  # gpu_dedicated_bytes（进程专用显存字节）。
            "shared_with": "host" if key == "web" else None,
            "retry_allowed": False, "retrying": False, "external": False,
        }

    def snapshot(self) -> dict:
        with self.condition:
            result = copy.deepcopy(self.value)
            scheduled = [c for c in result["components"] if c["enabled"]]
            result["completed_components"] = sum(c["state"] in TERMINAL for c in scheduled)
            result["total_components"] = len(scheduled)
            setup_ready = next(c for c in result["components"] if c["id"] == "setup")["state"] == "ready"
            voice_running = next(c for c in result["components"] if c["id"] == "voice")["pid"] is not None
            for c in result["components"]:
                c["retry_allowed"] = (c["state"] in {"failed", "blocked"} and not c["retrying"]
                    and c["id"] != "host" and not result["stopping"] and result["startup_complete"]
                    and (c["id"] in {"setup", "web"} or setup_ready)
                    and (c["id"] not in {"speaker", "dsh"} or voice_running))
            return result

    def update(self, **values) -> None:
        with self.condition:
            self.value.update(values)
            self.value["revision"] += 1
            self.condition.notify_all()

    def component(self, key: str, **values) -> None:
        with self.condition:
            row = next(c for c in self.value["components"] if c["id"] == key)
            if "pid" in values and values["pid"] != row["pid"]:
                row.update(created_at=None, memory_rss_bytes=None, memory_private_commit_bytes=None, gpu_dedicated_bytes=None,
                    cpu_percent=None, uptime_seconds=None, sampled_at=None)
            row.update(values)
            if row["state"] in {"blocked", "disabled", "stopped"} or row["pid"] is None:
                row.update(memory_rss_bytes=None, memory_private_commit_bytes=None, gpu_dedicated_bytes=None,
                    cpu_percent=None, uptime_seconds=None)
            self.value["revision"] += 1
            self.condition.notify_all()

    def finish(self) -> None:
        snapshot = self.snapshot()
        if snapshot["stopping"]:
            return
        failed_core = any(c["required"] and c["state"] in {"failed", "blocked"} for c in snapshot["components"])
        failure = any(c["state"] in {"failed", "blocked"} for c in snapshot["components"])
        core = not failed_core and bool(snapshot["core_available"])
        self.update(startup_complete=True, core_available=core,
            state="failed" if not core else "degraded" if failure else "ready",
            phase="核心能力不可用，请查看诊断" if not core else "部分组件不可用" if failure else "启动完成")
