"""The spotter accepts every configured wake spelling and nothing else.

``KwsDetector`` normally loads a native sherpa-onnx model.  These tests build the
object without ``__init__`` and drive it with a fake spotter/stream so the label
policy (one wake word, several accepted spellings) is pinned down without a
model, a microphone, or a 12 MB ONNX file.
"""

from __future__ import annotations

from types import SimpleNamespace

from voice_terminal.kws.detector import KwsDetector


class _Stream:
    def __init__(self) -> None:
        self.fed = 0

    def accept_waveform(self, _sample_rate, samples) -> None:
        self.fed += len(samples)


class _Spotter:
    def __init__(self, results: list[str]) -> None:
        self._results = list(results)
        self.reset_count = 0
        self.decoded = 0

    def is_ready(self, _stream) -> bool:
        return bool(self._results)

    def decode_stream(self, _stream) -> None:
        self.decoded += 1

    def get_result(self, _stream) -> str:
        return self._results.pop(0) if self._results else ""

    def reset_stream(self, _stream) -> None:
        self.reset_count += 1


def _detector(labels, results: list[str]) -> KwsDetector:
    detector = object.__new__(KwsDetector)
    detector.sample_rate = 16000
    detector.keyword_labels = tuple(dict.fromkeys(label.upper() for label in labels))
    detector.keyword_label = detector.keyword_labels[0]
    detector._label_index = {label.casefold(): label for label in detector.keyword_labels}
    detector._spotter = _Spotter(results)
    detector._stream = _Stream()
    detector._np = __import__("numpy")
    return detector


def test_every_configured_spelling_is_a_hit():
    labels = ["七三", "你好七三", "七三同学"]
    for label in labels:
        detector = _detector(labels, [label])
        assert detector.accept_pcm(b"\x00\x00" * 320) == label


def test_labels_are_matched_case_insensitively():
    detector = _detector(["HEI_CLAW", "你好七三"], ["hei_claw"])
    assert detector.accept_pcm(b"\x00\x00" * 320) == "HEI_CLAW"


def test_unconfigured_label_is_never_a_wake_signal():
    # A keywords file may carry diagnostic entries; only the configured wake
    # word may open the microphone.
    detector = _detector(["七三"], ["法国"])
    assert detector.accept_pcm(b"\x00\x00" * 320) is None


def test_no_result_yields_no_detection():
    detector = _detector(["七三"], [])
    assert detector.accept_pcm(b"\x00\x00" * 320) is None


def test_primary_label_is_the_first_configured_spelling():
    detector = _detector(["七三", "你好七三"], [])
    assert detector.keyword_label == "七三"
    assert detector.keyword_labels == ("七三", "你好七三")


def test_discover_model_paths_validates_every_label(tmp_path):
    from voice_terminal.kws.detector import discover_model_paths

    model_dir = tmp_path / "kws"
    model_dir.mkdir()
    (model_dir / "tokens.txt").write_text("<blk> 0\n", encoding="utf-8")
    for name in ("encoder-epoch-13.onnx", "decoder-epoch-13.onnx", "joiner-epoch-13.onnx"):
        (model_dir / name).write_bytes(b"stub")
    keywords = model_dir / "keywords.txt"
    keywords.write_text("q ī s ān @七三\nn ǐ h ǎo q ī s ān @你好七三\n", encoding="utf-8")

    paths = discover_model_paths(model_dir, keywords, ["七三", "你好七三"])
    assert paths.keywords == keywords

    try:
        discover_model_paths(model_dir, keywords, ["七三", "七三同学"])
    except ValueError as exc:
        assert "@七三同学" in str(exc)
    else:  # pragma: no cover - the assertion above is the contract
        raise AssertionError("a missing label must fail the startup check")


def test_detector_rejects_a_file_missing_one_configured_label(tmp_path):
    from voice_terminal.kws.detector import KwsError

    model_dir = tmp_path / "kws"
    model_dir.mkdir()
    (model_dir / "tokens.txt").write_text("<blk> 0\n", encoding="utf-8")
    for name in ("encoder-epoch-13.onnx", "decoder-epoch-13.onnx", "joiner-epoch-13.onnx"):
        (model_dir / name).write_bytes(b"stub")
    keywords = model_dir / "keywords.txt"
    keywords.write_text("q ī s ān @七三\n", encoding="utf-8")

    try:
        KwsDetector(
            model_dir=model_dir,
            keywords_file=keywords,
            keyword_label=["七三", "你好七三"],
        )
    except (KwsError, ValueError) as exc:
        assert "@你好七三" in str(exc)
    else:  # pragma: no cover - the assertion above is the contract
        raise AssertionError("KwsDetector must fail closed on a partial label set")


def test_max_active_paths_is_forwarded_to_the_spotter(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeSherpa:
        class KeywordSpotter:  # noqa: N801 - mirrors the sherpa name
            def __init__(self, **kwargs) -> None:
                captured.update(kwargs)

            def create_stream(self):
                return SimpleNamespace()

    monkeypatch.setitem(__import__("sys").modules, "sherpa_onnx", _FakeSherpa)
    model_dir = _prepare_model(monkeypatch)

    detector = KwsDetector(
        model_dir=model_dir,
        keywords_file=model_dir / "keywords.txt",
        keyword_label=["七三"],
        max_active_paths=8,
    )
    assert captured["max_active_paths"] == 8
    assert captured["keywords_score"] == 1.0
    assert detector.keyword_labels == ("七三",)


def _prepare_model(monkeypatch):
    import tempfile
    from pathlib import Path

    directory = Path(tempfile.mkdtemp())
    (directory / "tokens.txt").write_text("<blk> 0\n", encoding="utf-8")
    for name in ("encoder-epoch-13.onnx", "decoder-epoch-13.onnx", "joiner-epoch-13.onnx"):
        (directory / name).write_bytes(b"stub")
    (directory / "keywords.txt").write_text("q ī s ān @七三\n", encoding="utf-8")
    # Keep numpy available (the real module) but never call into it here.
    return directory
