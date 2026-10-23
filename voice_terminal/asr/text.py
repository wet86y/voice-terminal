"""识别文本的保真空白规范化；英文短语及路径不按空格拆词。"""
from __future__ import annotations

import re

_HAN = r"\u3400-\u9fff\uf900-\ufaff"


def normalize_asr_text(text: str) -> str:
    """仅去掉汉字间和中文标点附近的模型空白，保留增量首尾边界。"""
    text = re.sub(rf"(?<=[{_HAN}])\s+(?=[{_HAN}])", "", text)
    text = re.sub(r"\s+([，。？！；：、])", r"\1", text)
    return re.sub(rf"([，。？！；：、])\s+(?=[{_HAN}])", r"\1", text)


def join_asr_segments(parts: list[str]) -> str:
    """闭段按顺序连接；两个英文或数字词之间补一个边界空格。"""
    result = ""
    for part in parts:
        part = normalize_asr_text(part).strip()
        if not part:
            continue
        if result and result[-1].isascii() and result[-1].isalnum() and part[0].isascii() and part[0].isalnum():
            result += " "
        result += part
    return normalize_asr_text(result)
