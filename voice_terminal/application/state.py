"""Application snapshot（应用快照）与线程安全状态发布。"""
from __future__ import annotations

import copy
import threading
import time
import uuid

from .ports import validate_endpoint_url
from ..core.operations import OperationRegistry

TERMINAL = {"ready", "disabled", "failed", "blocked", "external", "stopped"}
# LOADING_STATES（加载中组件状态）：瞬态，整体状态按“启动中”呈现而不是启动失败。
LOADING_STATES = {"waiting", "starting"}
LABELS = {
    "host": "应用宿主", "web": "界面服务", "setup": "环境与配置检查",
    "asr": "ASR（语音识别）", "analysis": "分析工作进程",
    "voice": "Voice Server（语音服务）", "speaker": "声纹工作进程",
    "dsh": "DSH（代理执行宿主）",
}


class ApplicationState:
    def __init__(self) -> None:
        self.condition = threading.Condition(threading.RLock())
        self.operations = OperationRegistry()
        self.value = {
            "instance_id": uuid.uuid4().hex,  # instance_id（应用实例标识）。
            "revision": 0,  # revision（状态版本）。
            "state": "starting", "phase": "建立应用入口", "started_at": time.time(),
            "startup_complete": False, "startup_skipped": False, "core_available": False,
            "core_phase": "starting",  # core_phase（核心能力阶段）：ready（就绪）、starting（加载中）或 failed（失败）。
            "unavailable_capabilities": [],  # unavailable_capabilities（能力行）：来自语音服务的分能力状态。
            "stopping": False,
            "voice_api_url": "", "voice_ws_url": "", "capabilities": {},
            "endpoints": {},  # endpoints（端点表）：url（实际地址）、preferred_url（首选地址）、scheme（协议）、host（主机）、port（端口）、path（路径）、generation（代次）、pid（进程编号）、created_at（创建时间）。
            "endpoint_revision": 0,  # endpoint_revision（端点版本）：每次地址或代次变化时递增。
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
            result["runtime_operations"] = self.operations.snapshot()  # runtime_operations（宿主运行操作）。
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
            operation_failed = values.pop("operation_failed", None)  # 能力级恢复可以明确报告终态。
            operation_label = values.pop("operation_label", None)  # 可选的能力中文名称。
            operation_target = values.pop("operation_target", None)  # 可选的能力标识。
            row = next(c for c in self.value["components"] if c["id"] == key)
            was_retrying = row["retrying"]
            if "pid" in values and values["pid"] != row["pid"]:
                row.update(created_at=None, memory_rss_bytes=None, memory_private_commit_bytes=None, gpu_dedicated_bytes=None,
                    cpu_percent=None, uptime_seconds=None, sampled_at=None)
            row.update(values)
            if row["retrying"] and not was_retrying:
                row["operation_id"] = self.operations.begin(operation_target or key, operation_label or row["name"], "restarting")
            elif was_retrying and not row["retrying"] and row.get("operation_id"):
                failed = (row["state"] in {"failed", "blocked", "stopped"}
                          if operation_failed is None else bool(operation_failed))
                self.operations.update(row.pop("operation_id"), state="failed" if failed else "succeeded",
                                       phase="failed" if failed else "ready", detail=row["error"] if failed else "")
            if row["state"] in {"blocked", "disabled", "stopped"} or row["pid"] is None:
                row.update(memory_rss_bytes=None, memory_private_commit_bytes=None, gpu_dedicated_bytes=None,
                    cpu_percent=None, uptime_seconds=None)
            self.value["revision"] += 1
            self.condition.notify_all()

    def set_endpoint(self, component: str, endpoint_url: str | None, *,
                     preferred_url: str | None = None, generation: str | None = None,
                     pid: int | None = None, created_at: float | None = None) -> None:
        """Publish one endpoint row（端点记录）as part of the host snapshot."""
        actual = validate_endpoint_url(component, endpoint_url) if endpoint_url else None
        preferred = validate_endpoint_url(component, preferred_url) if preferred_url else None
        with self.condition:
            endpoints = self.value["endpoints"]
            previous = endpoints.get(component, {})
            row = {
                **previous,
                **(actual or {}),
                "url": actual["url"] if actual else previous.get("url", ""),
                "preferred_url": preferred["url"] if preferred else previous.get("preferred_url", ""),
                "generation": generation if generation is not None else previous.get("generation"),
                "pid": pid if pid is not None else previous.get("pid"),
                "created_at": created_at if created_at is not None else previous.get("created_at"),
            }
            changed = any(previous.get(name) != row.get(name)
                          for name in ("url", "preferred_url", "generation", "pid", "created_at",
                                       "scheme", "host", "port", "path"))
            endpoints[component] = row
            if component == "voice":
                self.value["voice_api_url"] = row["url"]
                self.value["voice_ws_url"] = row["url"].replace("http://", "ws://") + "/ws" if row["url"] else ""
            if changed:
                self.value["endpoint_revision"] += 1
                self.value["revision"] += 1
                self.condition.notify_all()

    def clear_endpoint(self, component: str, *, generation: str | None = None) -> None:
        """Mark a previous listener address unavailable before replacing it."""
        with self.condition:
            previous = self.value["endpoints"].get(component, {})
            if not previous and component != "voice":
                return
            row = dict(previous)
            row.update(url="", generation=generation, pid=None, created_at=None)
            for field in ("scheme", "host", "port", "path"):
                row.pop(field, None)
            changed = (not previous or any(previous.get(name) != row.get(name)
                for name in ("url", "generation", "pid", "created_at", "scheme", "host", "port", "path")))
            self.value["endpoints"][component] = row
            if component == "voice":
                self.value["voice_api_url"] = ""
                self.value["voice_ws_url"] = ""
                self.value["voice_api_ready"] = False
            if changed:
                self.value["endpoint_revision"] += 1
                self.value["revision"] += 1
                self.condition.notify_all()

    def finish(self) -> None:
        snapshot = self.snapshot()
        if snapshot["stopping"]:
            return
        failed_core = any(c["required"] and c["state"] in {"failed", "blocked"} for c in snapshot["components"])
        loading_core = any(c["required"] and c["state"] in LOADING_STATES for c in snapshot["components"])
        failure = any(c["state"] in {"failed", "blocked"} for c in snapshot["components"])
        core = not failed_core and bool(snapshot["core_available"])
        # core_phase（核心能力阶段）把“仍在加载”与“已经失败”分开：加载中是瞬态，不能写成启动失败。
        # probe_failed（探测已判失败）由宿主探测写入，恢复确认前保持，避免加载中被误报为故障。
        probe_failed = snapshot.get("core_phase") == "failed"
        core_phase = ("failed" if failed_core or (probe_failed and not loading_core)
                      else "ready" if core
                      else "starting")
        self.update(startup_complete=True, core_available=core, core_phase=core_phase,
            state=("failed" if core_phase == "failed"
                   else "degraded" if failure
                   else "ready" if core
                   else "starting"),
            phase=("核心能力不可用，请查看诊断" if core_phase == "failed"
                   else "核心能力加载中" if core_phase == "starting"
                   else "部分组件不可用" if failure
                   else "启动完成"))
