"""TTS hook（语音后端钩子）参数适配：按声明签名调用一次，内部异常原样传递。"""
from functools import lru_cache
import inspect

# 只兼容旧钩子的可选参数：fresh（独立会话）、turn_id（轮次关联）、speech_id（事务关联）、
# emotion（旧情绪选择）、detach_active（分离旧会话）；音色、控制值及设备播放键必须由后端支持。
_LEGACY_OPTIONAL = frozenset({"fresh", "turn_id", "speech_id", "emotion", "detach_active"})


def _keywords(function):
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return None  # 不可检查的原生调用器接收完整参数，由其正式入口报告错误。
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return None
    return frozenset(parameter.name for parameter in parameters
                     if parameter.kind in {inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                           inspect.Parameter.KEYWORD_ONLY})


@lru_cache(maxsize=128)
def _method_keywords(function):
    # 只缓存类方法实现，不缓存绑定方法或闭包，避免适配缓存延长模型/音频事务寿命。
    return _keywords(function)


def call_tts_hook(function, *args, **options):
    """call_tts_hook（调用后端钩子）：旧适配器只接收其声明支持的可选字段。"""
    implementation = getattr(function, "__func__", None)
    supported = (_method_keywords(implementation)
                 if inspect.isfunction(implementation) and implementation.__closure__ is None
                 else _keywords(function))
    return function(*args, **(options if supported is None else {
        name: value for name, value in options.items() if name in supported or name not in _LEGACY_OPTIONAL
    }))
