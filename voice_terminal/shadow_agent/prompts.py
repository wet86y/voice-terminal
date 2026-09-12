"""Prompt loading and fixed prompts for the shadow task agent."""
from __future__ import annotations

from pathlib import Path


DEFAULT_INSTRUCTIONS_PATH = Path(__file__).resolve().parents[2] / "config" / "shadow_agent" / "instructions.md"

CHECKPOINT_PROMPT = """\
你现在处于影子任务的强制进度汇报点。不要调用工具。只基于已经提交的任务历史和工具结果，
输出一个严格 JSON 对象：{"completed": boolean, "text": string}。
completed=true 仅表示任务已经可以给出最终结果；text 应简短说明已完成结论或当前进度。
"""

TIMEOUT_PROMPT = """\
影子任务的工作时间已经耗尽。不要调用工具，也不要继续调查。输出一个严格 JSON 对象：
{"completed": boolean, "text": string}。text 只总结已经完成的工作、可用结论和未完成部分。
"""


def load_shadow_instructions(path: Path | None = None) -> str:
    selected = path or DEFAULT_INSTRUCTIONS_PATH
    try:
        text = selected.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if text:
        return text
    return (
        "你是本地影子任务执行 agent。完成当前任务，使用提供的工具，"
        "不要扮演语音助手，不要进行寒暄。"
    )


def modification_message(text: str) -> str:
    return (
        "【任务修改】\n当前任务没有取消，也不是新任务。主协调模型要求按以下方向修改：\n"
        f"{text.strip()}\n"
        "立即放弃冲突工作，保留仍有效的完整工具结果和事实；后续结论以此修改为准。"
    )
