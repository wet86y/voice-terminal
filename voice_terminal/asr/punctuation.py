"""Streaming punctuation（实时标点）：只接收识别增量，按轮次持有前文。"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
from pathlib import Path
import time
from typing import Any

from ..compute import device_candidates, loaded_funasr_device
from ..resources import model_thread_budget
from .model_loader import auto_model_class

logger = logging.getLogger(__name__)
REALTIME_PUNC_MODEL = "punc_ct-transformer_zh-cn-common-vad_realtime-vocab272727"  # REALTIME_PUNC_MODEL（固定实时模型标识）。
_PUNCTUATION = "，。？、,!?;；！：:"


@dataclass
class _Turn:
    cache: dict[str, Any] = field(default_factory=dict)  # cache（模型前文状态）。
    terminal: str = ""  # terminal（最近块末字的模型句末预测）。
    tail: str = ""  # tail（已输出的最后一个非空字符）。
    failed: bool = False  # failed（本轮改走原始识别文本）。


class StreamingPunctuation:
    """所有调用由识别专用线程串行执行；不修改已发出的前缀。"""

    def __init__(self, model_dir: Path, device: str = "cpu") -> None:
        self.model_dir = model_dir  # model_dir（实时模型目录）。
        self.requested_device = device  # requested_device（配置设备）。
        self.device = "cpu"  # device（实际推理设备）。
        self.model: Any = None  # model（独立标点模型，复用现有语音框架）。
        self.warmed = False  # warmed（真实短文本预热完成）。
        self.load_error: str | None = None  # load_error（加载失败或设备回退原因）。
        self.last_error: str | None = None  # last_error（最近推理回退原因）。
        self.timings_ms: dict[str, float] = {}  # timings_ms（启动阶段耗时，毫秒）。
        self.rule_finalizations = 0  # rule_finalizations（无模型句末预测时规则补句号次数）。
        self._turns: dict[str, _Turn] = {}  # turns（各识别子轮次状态）。
        self._load_attempted = False  # load_attempted（已尝试加载；失败后重启再试）。

    def load(self) -> None:
        if self._load_attempted:
            return
        self._load_attempted = True
        try:
            for filename in ("config.yaml", "model.pt", "tokens.json"):
                if not (self.model_dir / filename).is_file():
                    raise FileNotFoundError(f"实时标点模型缺少文件：{self.model_dir / filename}")
            import yaml
            config = yaml.safe_load((self.model_dir / "config.yaml").read_text(encoding="utf-8"))
            if config.get("model") != "CTTransformerStreaming":
                raise ValueError("标点必须使用 CTTransformerStreaming（流式标点）配置")
            started = time.perf_counter()
            auto_model = auto_model_class(self.model_dir)
            self.timings_ms["imports"] = (time.perf_counter() - started) * 1000  # imports（依赖导入）。
            errors = []
            for candidate in device_candidates(self.requested_device, "paraformer", "cpu"):
                try:
                    started = time.perf_counter()
                    self.model = auto_model(model=str(self.model_dir), device=candidate,
                                            disable_update=True, disable_pbar=True,
                                            ncpu=model_thread_budget())
                    self.timings_ms["model"] = (time.perf_counter() - started) * 1000  # model（构造与权重加载）。
                    self.device = loaded_funasr_device(self.model, candidate)
                    started = time.perf_counter()
                    # 预热独立缓存；不进入任何用户轮次。
                    cache: dict[str, Any] = {}
                    for text in ("你好今天", "我们测试实时标点"):
                        self.model.generate(input=text, cache=cache, data_type="text")
                    self.timings_ms["warmup"] = (time.perf_counter() - started) * 1000  # warmup（真实推理预热）。
                    cache.clear()
                    self.warmed = True
                    if errors:
                        self.load_error = "；".join(errors)
                    elif candidate == "cuda" and self.device != "cuda":
                        self.load_error = "CUDA（图形处理器）请求实际回退到 CPU（中央处理器）"
                    logger.info("实时标点已就绪：device（实际设备）=%s，startup_ms（启动耗时）=%s",
                                self.device, self.timings_ms)
                    return
                except Exception as exc:
                    errors.append(f"{candidate}（设备）: {type(exc).__name__}: {exc}")
                    self.model = None
            raise RuntimeError("；".join(errors))
        except Exception as exc:
            self.load_error = f"{type(exc).__name__}: {exc}"
            logger.exception("实时标点加载失败，回退到原始识别文字")

    def process(self, turn_id: str, delta: str, *, final: bool) -> str:
        """delta（本窗新增文字）；返回可直接追加的文字，绝不重送累计正文。"""
        if self.model is None:
            return delta
        state = self._turns.setdefault(turn_id, _Turn())
        output = delta
        if delta.strip() and not state.failed:
            try:
                result = self.model.generate(input=delta, cache=state.cache, data_type="text")
                row = result[0]
                output = row["text"]
                if not isinstance(output, str) or not output.strip():
                    raise ValueError("实时标点返回空正文")
                # 实时模型只允许增加标点及调整空格；异常响应不能丢字或重复前文。
                def content(text: str) -> str:
                    return "".join(c for c in text if not c.isspace() and c not in _PUNCTUATION)
                if content(output) != content(delta):
                    raise ValueError("实时标点返回正文与本窗新增文字不一致")
                array = row.get("punc_array")  # punc_array（模型标点类别预测数组）。
                state.terminal = ""
                if array is not None and len(array):
                    # 单字返回二维 [[类别]]，多字通常一维，统一取最后一个元素。
                    flattened = array.reshape(-1) if hasattr(array, "reshape") else array
                    index = int(flattened[-1].item())
                    predicted = self.model.model.punc_list[index]
                    if predicted in {"。", "？"}:
                        state.terminal = predicted
            except Exception as exc:
                # 本轮不重新建立半截缓存；后续增量继续原始输出，保留此前已显示的前缀。
                state.failed = True
                state.cache.clear()
                state.terminal = ""
                self.last_error = f"{type(exc).__name__}: {exc}"
                output = delta
                logger.exception("实时标点推理失败，当前轮次回退：turn_id（轮次）=%s", turn_id)
        if output.strip():
            state.tail = output.rstrip()[-1]
        # FunASR 1.4.14 忽略 is_final（结束标记）并剥掉块尾标点。
        # 空输入不能当作刷新；取最后预测的句末标点，无句末预测时规则补句号。
        if final and not state.failed and state.tail and state.tail not in "。？！.!?":
            output += state.terminal or "。"
            if not state.terminal:
                self.rule_finalizations += 1
                logger.info("实时标点规则收尾：turn_id（轮次）=%s，末尾无模型句末预测，补句号", turn_id)
            state.tail = output[-1]
        return output

    def clear_turn(self, turn_id: str) -> None:
        state = self._turns.pop(turn_id, None)
        if state is not None:
            state.cache.clear()

    def close(self) -> None:
        for turn_id in list(self._turns):
            self.clear_turn(turn_id)
        self.model = None

    def status(self) -> dict[str, Any]:
        return {
            "model": REALTIME_PUNC_MODEL,  # model（固定实时模型名称）。
            "status": "READY" if self.warmed else "DEGRADED",  # status（就绪或原文回退）。
            "model_loaded": self.model is not None,  # model_loaded（权重已加载）。
            "model_warmed": self.warmed,  # model_warmed（已预热）。
            "requested_device": self.requested_device,  # requested_device（请求设备）。
            "device": self.device,  # device（实际设备）。
            "load_error": self.load_error,  # load_error（启动回退原因）。
            "last_error": self.last_error,  # last_error（最近推理回退原因）。
            "rule_finalizations": self.rule_finalizations,  # rule_finalizations（规则补句号次数）。
            "startup_timings_ms": dict(self.timings_ms),  # startup_timings_ms（启动耗时）。
        }
