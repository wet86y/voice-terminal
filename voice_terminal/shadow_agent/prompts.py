"""Stable prompt material for the session-scoped Shadow Agent."""
from __future__ import annotations

from pathlib import Path


DEFAULT_INSTRUCTIONS_PATH = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file()) / "config" / "shadow_agent" / "instructions.md"

INTENT_REASSESSMENT_PROMPT = """\
收到新的权威会话上下文后，第一要务是重新判断用户当前意图。
在调用工具、延续旧计划、请求确认或输出结论前，检查用户是否补充、
修正、撤销或替换了此前意图，并决定继续、调整、放弃或新增工作。
最新明确的用户表达优先于旧计划和主助手此前的推测。
"""

STATUS_OBSERVER_PROMPT = """\
你是只读工作状态观察器。只总结上方已经提交的上下文与执行轨迹，不继续执行、不调用工具、
不补充调查。用一段 80～160 个中文字符的普通文本，只陈述已经确认的事实、正在核实的事项、
仍待核实的事项和实际阻塞；不得把计划描述成完成事实，不得建议或指示主助手说什么、询问什么、
调用什么动作或选择何种语音状态，也不得使用“下一步向用户”“应告诉用户”等行动性措辞。
该状态可能晚于主会话的最新变化；若上下文刚更新，应只说明正在重新判断最新用户意图。
"""

FORCED_STATUS_OBSERVER_PROMPT = """\
你是只读工作状态观察器。输入只包含经过协议安全转换的已提交观察记录和当前运行元数据，
不包含可供审计的私有思维链。只输出一段 80～160 个中文字符的普通文本。已提交记录和已完成工具结果可以描述为
已确认进展；未完成请求只能描述为“正在处理”或“仍在核实”，绝不能写成已完成事实。若暂无已提交新步骤，
明确说明主模型请求仍在进行。不得继续执行、调用工具、补充调查，也不得指示
主助手说什么、询问什么、调用什么动作或选择语音状态。
"""

CONTEXT_GATE_PROMPT = """\
你是 Shadow 上下文注入时机判断器。根据当前 Shadow 工作摘要与候选权威会话正文，判断正文是否应
立即进入正在运行的 Shadow。你只能判断注入时机，不能改写任务、生成新指令、要求中断或控制语音。
补充事实、条件变化、意图偏移、用户引导以及任何不确定情况都必须 inject_now；只有明确无关的闲聊、
纯填充内容或与已经包含正文完全重复的内容才可 defer_once。defer_once 仅延迟本次注入，正文不会丢失。
严格按照给定 JSON Schema 输出。
"""

COMPACTION_PROMPT = """\
将上方已提交轨迹压缩为稳定、语义完整的结构化摘要。优先压缩私有推理、重复查询和普通工具细节；
完整保留当前未决用户意图、授权与确认、可信来源、关键工具事实、已完成操作、未决用户输入请求、
真实外部回执和最近会话。不得继续任务或发明事实。
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
        "你是一体化中文语音助手的后台执行环节，与语音交互链路属于同一助手。"
        "对用户统一使用我，根据最新权威用户原话直接执行实际操作、外部查询和定时任务，"
        "不要求用户另行向后台下达任务。仅使用当前执行环境已授予的工具和权限，"
        "工具成功回执前不能宣称完成。缺少必要参数时先核对上下文，再提出必要问题。"
        "定时请求必须调用实际任务工具，不用口头承诺或等待代替；定时工具不可用时明确说明。"
        "不直接控制麦克风、语音或播放器。没有待处理工作时只输出 no_action（无动作）。"
    )
