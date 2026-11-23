"""图文展示决策：与语音播报并列，只有正式桌面处于悬浮模式时受理。"""
from dataclasses import dataclass


DISPLAY_SCHEMA = {
    "type": "array",
    "maxItems": 1,
    "items": {"anyOf": [
        {"type": "object", "additionalProperties": False,
         "required": ["content", "duration_seconds"], "properties": {
             "content": {"type": "string", "minLength": 1, "maxLength": 4096,
                         "description": "固定排版的图文正文。"},
             "duration_seconds": {"type": "integer", "enum": list(range(5, 61)),
                                  "description": "初始展示秒数。"},
         }},
        {"type": "object", "additionalProperties": False, "required": ["cancel"],
         "properties": {"cancel": {"type": "boolean", "enum": [True], "description": "清除当前展示。"}}},
    ]},
}

DISPLAY_RULES = (
    "当前支持 display（悬浮图文），可补充当前对话内容，或按用户明确要求展示或清除。"
    "此字段必填，保持原展示用 []（无操作），清除用 [{\"cancel\":true}]（取消展示），"
    "展示用 [{\"content\":\"简短正文\",\"duration_seconds\":12}]（图文正文、展示秒数）。"
    "不需要同时开口，可与 speak（语音决策）独立使用；只更改本地展示时无需请求后台执行。"
    "正文使用固定 Markdown（轻量排版）子集：可有一个开头的 # 标题、段落、- 短列表、**强调**。"
    "图片最多一张，独占一行，使用 ![图注](加密图片直链)，只用已有的有效 HTTPS（加密连接）图片直链。"
    "无图最多六个排版行，有图最多三个文字排版行（含图注），折行也计入。"
    "正文和图注最多五百字符，具体内容还须能在紧凑窗口内完整摆放。"
    "展示计时 5 至 60 秒。新图文通过验证后替换旧图文，无需先清除。"
)

DISPLAY_INACTIVE = "本次 display（悬浮图文）不可用，该字段必填并使用 []（无操作）。"


@dataclass(frozen=True)
class DisplayDecision:
    content: str | None = None  # content（图文正文），空值代表取消。
    duration_seconds: int | None = None  # duration_seconds（展示秒数）。
    error: str | None = None  # error（图文校验结果），不影响语音回复。


def parse_display(value: object) -> DisplayDecision | None:
    if value == []:
        return None
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        return DisplayDecision(error="展示字段须由零或一个合法的展示操作组成。")
    item = value[0]
    if set(item) == {"cancel"} and item["cancel"] is True:
        return DisplayDecision()
    content, seconds = item.get("content"), item.get("duration_seconds")
    if (set(item) == {"content", "duration_seconds"} and isinstance(content, str) and content.strip()
            and type(seconds) is int and 5 <= seconds <= 60):
        try:
            if len(content.encode("utf-16-le")) <= 8192:
                return DisplayDecision(content=content, duration_seconds=seconds)
        except UnicodeEncodeError:
            pass
    return DisplayDecision(error="请使用有效的图文正文和 5–60 秒展示时长。")
