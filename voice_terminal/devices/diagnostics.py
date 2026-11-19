"""Device diagnostics（设备诊断）：只记录有界元数据，不记录凭据或音频正文。"""
from __future__ import annotations

import json
import logging

_LABELS = {
    "event": "设备事件", "device_id": "设备编号", "core_session_id": "核心运行编号",
    "connection_generation": "设备连接代次", "executor_generation": "执行器代次",
    "interaction_generation": "交互占用代次", "stream_id": "媒体流编号",
    "capture_stream_id": "输入采集流编号", "reason": "原因", "duration_ms": "持续毫秒",
    "capture_active": "是否采集", "client_tts_enabled": "客户端合成是否启用",
    "sync_id": "时钟测量编号", "host_send": "主机发送秒数", "host_receive": "主机接收秒数",
    "device_receive_us": "设备接收微秒", "device_send_us": "设备发送微秒",
    "uncertainty_us": "时钟映射误差微秒", "resume_intent": "是否希望恢复监听",
    "command": "控制命令", "result": "操作结果", "capability": "恢复能力",
    "trigger": "恢复触发来源", "exit_code": "旧进程退出码", "capabilities": "设备声明能力",
}


def device_event(logger: logging.Logger, name: str, *, level: int = logging.INFO, **details) -> None:
    """device_event（有界事件）：新增字段需在上表提供中文说明，日志失败不改变业务。"""
    record = {"event": name, **{key: value for key, value in details.items()
                               if key in _LABELS and (value is None or isinstance(value, (str, int, float, bool)))}}
    for key, value in tuple(record.items()):
        if isinstance(value, str):
            record[key] = "".join(char for char in value if char.isprintable())[:240 if key == "reason" else 128]
    record["fields"] = {key: _LABELS[key] for key in record}
    encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    for key in sorted((key for key in record if key not in {"event", "fields", "reason", "device_id"}),
                      key=lambda key: len(str(record[key])), reverse=True):
        if len((name + " " + encoded).encode("utf-8")) <= 3800:
            break
        record.pop(key)
        record["fields"].pop(key)
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    logger.log(level, "%s %s", name, encoded)


def lease_context(lease) -> dict:
    """lease_context（租约日志上下文）：只提取设备身份与三个代次。"""
    return {key: getattr(lease, key) for key in (
        "device_id", "connection_generation", "executor_generation", "interaction_generation",
    )}
