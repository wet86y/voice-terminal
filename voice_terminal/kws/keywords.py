from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Iterable, Sequence
from pathlib import Path


DEFAULT_WAKE_KEYWORD = "hei claw"
DEFAULT_WAKE_LABEL = "HEI_CLAW"

# A single KWS worker is cheap per keyword (one extra boosting path per entry),
# but every additional phrase widens the match surface, so the product limit is
# deliberately small: enough to cover pronunciation variants of one wake word,
# not enough to turn the spotter into an open-vocabulary listener.
MAX_WAKE_KEYWORDS = 5
MAX_WAKE_KEYWORD_CHARS = 120


def _is_cjk_character(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF  # CJK Unified Ideographs Extension A
        or 0x4E00 <= codepoint <= 0x9FFF  # CJK Unified Ideographs
        or 0xF900 <= codepoint <= 0xFAFF  # CJK Compatibility Ideographs
        or 0x20000 <= codepoint <= 0x2FA1F  # CJK Extensions B-I / compatibility
    )


def _contains_cjk(text: str) -> bool:
    return any(_is_cjk_character(character) for character in text)


def normalize_keyword(keyword: str) -> str:
    # sherpa's phone+ppinyin converter accepts CJK text directly, but raw
    # punctuation (especially Chinese comma/colon) is not a pronunciation
    # token.  Treat Unicode punctuation and whitespace as phrase separators,
    # while keeping the user-visible spelling and allowing Chinese phrases.
    folded = unicodedata.normalize("NFKC", keyword).casefold()
    separated = "".join(
        " " if character.isspace() or unicodedata.category(character).startswith("P") else character
        for character in folded
    )
    normalized = " ".join(separated.strip().split())
    if not normalized:
        raise ValueError("唤醒词不能为空")
    return normalized


def keyword_label(keyword: str) -> str:
    normalized = normalize_keyword(keyword)
    label = "".join(
        character.upper() if character != " " else "_"
        for character in normalized
        if character == " " or character.isalnum() or _is_cjk_character(character)
    )
    label = re.sub(r"_+", "_", label).strip("_")
    if not label:
        raise ValueError(f"无法从唤醒词生成标签：{keyword!r}")
    return label


def normalize_keyword_list(raw: Iterable[str]) -> tuple[str, ...]:
    """Normalize a wake-phrase list, dropping duplicate spellings and labels.

    The product contract is "one wake word, up to :data:`MAX_WAKE_KEYWORDS`
    spellings".  Duplicates are dropped rather than rejected: ``hei claw`` and
    ``HEI_CLAW`` normalize to the same phrase and the same ``@`` label, and a
    user re-typing the same word must not be an error.
    """

    if isinstance(raw, (str, bytes)) or not isinstance(raw, Iterable):
        raise ValueError("唤醒词必须是字符串列表。")
    normalized: list[str] = []
    seen_phrases: set[str] = set()
    seen_labels: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("唤醒词必须是字符串。")
        candidate = item.strip()
        if not candidate:
            continue
        if len(candidate) > MAX_WAKE_KEYWORD_CHARS:
            raise ValueError(f"唤醒词过长（最多 {MAX_WAKE_KEYWORD_CHARS} 个字符）。")
        phrase = normalize_keyword(candidate)
        label = keyword_label(phrase)
        if phrase in seen_phrases or label in seen_labels:
            continue
        seen_phrases.add(phrase)
        seen_labels.add(label)
        normalized.append(phrase)
    if not normalized:
        raise ValueError("请至少填写 1 个唤醒词。")
    if len(normalized) > MAX_WAKE_KEYWORDS:
        raise ValueError(f"最多支持 {MAX_WAKE_KEYWORDS} 个唤醒词，当前 {len(normalized)} 个。")
    return tuple(normalized)


def canonical_labels(keywords: Iterable[str]) -> tuple[str, ...]:
    """The ``@LABEL`` of every phrase, in order, de-duplicated."""

    labels: list[str] = []
    for keyword in keywords:
        label = keyword_label(keyword)
        if label not in labels:
            labels.append(label)
    return tuple(labels)


def expected_label_set(expected: str | Iterable[str]) -> tuple[str, ...]:
    """Normalize the ``@LABEL`` expectation(s) used by file validation."""

    candidates: Sequence[object] = (expected,) if isinstance(expected, str) else tuple(expected)
    labels = tuple(dict.fromkeys(
        str(item).upper() for item in candidates if str(item or "").strip()
    ))
    if not labels:
        raise ValueError("KWS 期望标签不能为空。")
    return labels


def raw_keyword_line(keyword: str, label: str | None = None) -> str:
    normalized = normalize_keyword(keyword)
    canonical = label or keyword_label(normalized)
    if any(character.isspace() for character in canonical):
        raise ValueError("KWS 规范标签不能包含空格")
    return f"{normalized.upper()} @{canonical.upper()}"


def write_raw_keywords(path: Path, keyword: str, label: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw_keyword_line(keyword, label) + "\n", encoding="utf-8")


def write_raw_keywords_multi(path: Path, entries: Sequence[tuple[str, str]]) -> None:
    """Write one raw line per phrase; ``text2token`` consumes the whole file."""

    if not entries:
        raise ValueError("KWS 关键词条目不能为空")
    lines = [raw_keyword_line(keyword, label) for keyword, label in entries]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_keywords_file(path: Path, expected_labels: str | Iterable[str] = DEFAULT_WAKE_LABEL) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"KWS 关键词文件不存在：{path}")
    # ``utf-8-sig`` tolerates a Notepad-saved BOM: without it sherpa fails later
    # with ``Cannot find ID for token ﻿q`` and the session never starts.
    text = path.read_text(encoding="utf-8-sig")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError(f"KWS 关键词文件为空：{path}")
    # ``@HEI_CLAW_EXTRA`` is not the same keyword as ``@HEI_CLAW``.  The
    # old substring check accepted it and subsequently let a differently
    # named keyword pass the wake gate.  sherpa's generated file puts the
    # original keyword label in the final whitespace-separated field; the
    # leading ``@`` is the file marker, not part of the label the spotter
    # returns.
    labels = {line.split()[-1].upper().lstrip("@") for line in lines if line.split()}
    missing = [label for label in expected_label_set(expected_labels) if label not in labels]
    if missing:
        pretty = "、".join(f"@{label}" for label in missing)
        raise ValueError(
            f"KWS 关键词文件缺少标签 {pretty}：{path}；"
            "请在设置页重新保存唤醒词以重新生成 keywords.txt。"
        )


def strip_wake_prefix(text: str, keywords: str | Iterable[str] = DEFAULT_WAKE_KEYWORD) -> str:
    """Remove only a configured wake phrase at the beginning of ASR text."""

    candidates: Sequence[object] = (keywords,) if isinstance(keywords, str) else tuple(keywords)
    normalized: list[str] = []
    for item in candidates:
        if not isinstance(item, str):
            continue
        try:
            normalized.append(normalize_keyword(item))
        except ValueError:
            continue
    if not normalized:
        return text.strip()
    # Longest phrase first: with ``七三`` and ``七三同学`` registered together,
    # "七三同学你好" must lose "七三同学", not "七三" (which would leave "同学你好").
    for keyword in sorted(set(normalized), key=len, reverse=True):
        separator = r"[\s_-]*" if _contains_cjk(keyword) else r"[\s_-]+"
        escaped = re.escape(keyword).replace(r"\ ", separator)
        # Chinese ASR often concatenates the wake phrase and the command without
        # an ASCII word boundary (for example ``嘿小爪打开浏览器``).  An ASCII
        # ``\b`` would reject that valid prefix, so CJK phrases use the explicit
        # phrase length as the boundary while English keeps the stricter check.
        boundary = "" if _contains_cjk(keyword) else r"(?:\b|$)"
        pattern = re.compile(rf"^\s*{escaped}{boundary}[\s,，:：\-—]*", re.IGNORECASE)
        stripped = pattern.sub("", text, count=1).strip()
        if stripped != text.strip():
            return stripped
    return text.strip()


def generate_keywords_for(
    *,
    model_dir: Path,
    raw_path: Path,
    output_path: Path,
    entries: Sequence[tuple[str, str]],
    tokens_type: str = "phone+ppinyin",
) -> None:
    """Generate sherpa-onnx tokens for one or more phrases using text2token.

    ``text2token`` accepts a multi-line input file and emits one output line per
    keyword — that is exactly how the upstream model ships its own
    ``test_wavs/keywords.txt`` (ten phrases, ten distinct labels), so several
    wake phrases cost one CLI invocation and stay a single atomic artifact.
    """

    if not entries:
        raise ValueError("KWS 关键词条目不能为空")

    tokens = model_dir / "tokens.txt"
    lexicon_candidates = [model_dir / "en.phone", model_dir / "lexicon.txt"]
    lexicon = next((path for path in lexicon_candidates if path.is_file()), None)
    if not tokens.is_file():
        raise FileNotFoundError(f"KWS tokens.txt 不存在：{tokens}")
    if lexicon is None:
        raise FileNotFoundError(f"KWS 词典不存在，已检查：{', '.join(str(p) for p in lexicon_candidates)}")

    write_raw_keywords_multi(raw_path, entries)
    cli = shutil.which("sherpa-onnx-cli")
    if cli is None:
        scripts_dir = Path(sys.executable).parent / "Scripts"
        for candidate in (scripts_dir / "sherpa-onnx-cli.exe", scripts_dir / "sherpa-onnx-cli.cmd"):
            if candidate.is_file():
                cli = str(candidate)
                break
    if cli is None:
        raise RuntimeError("找不到 sherpa-onnx-cli，请确认 sherpa-onnx 已安装且 Python Scripts 在 PATH。")

    # The bundled CMU lexicon has ``HEY`` but not the user's transliterated
    # spelling ``HEI``.  Keep the raw/configured wake phrase as ``hei claw``
    # and derive the missing pronunciation from the existing lexicon entry;
    # the final keyword file is still produced by sherpa-onnx's text2token.
    lexicon_for_cli = lexicon
    temporary_lexicon: tempfile.TemporaryDirectory[str] | None = None
    lexicon_lines = lexicon.read_text(encoding="utf-8", errors="strict").splitlines()
    lexicon_words = {
        line.split(maxsplit=1)[0].upper()
        for line in lexicon_lines
        if line.split(maxsplit=1)
    }
    aliases = {"HEI": "HEY"}
    # CJK words are converted by sherpa's ``ppinyin`` path and therefore do
    # not appear in the bundled English CMU lexicon.  Only require lexicon
    # entries for non-CJK words; otherwise every Chinese wake word is rejected
    # before the official text2token command gets a chance to handle it.
    words: list[str] = []
    for keyword, _label in entries:
        for word in normalize_keyword(keyword).split():
            if not _contains_cjk(word) and word.upper() not in words:
                words.append(word.upper())
    missing_words = [word for word in words if word not in lexicon_words]
    unresolved = [word for word in missing_words if word not in aliases or aliases[word] not in lexicon_words]
    if unresolved:
        raise RuntimeError(
            "KWS 词典不包含唤醒词片段："
            + ", ".join(unresolved)
            + "；请提供该模型支持的英文词条后重试。"
        )
    if missing_words:
        temporary_lexicon = tempfile.TemporaryDirectory(prefix=".voice-terminal-lexicon-", dir=str(model_dir))
        augmented_lines = list(lexicon_lines)
        for word in missing_words:
            source = aliases[word]
            source_line = next(
                line for line in lexicon_lines
                if line.split(maxsplit=1)[0].upper() == source
            )
            augmented_lines.append(f"{word} {source_line.split(maxsplit=1)[1]}")
        lexicon_for_cli = Path(temporary_lexicon.name) / lexicon.name
        lexicon_for_cli.write_text("\n".join(augmented_lines) + "\n", encoding="utf-8")

    command = [
        cli,
        "text2token",
        "--tokens",
        str(tokens),
        "--tokens-type",
        tokens_type,
        "--lexicon",
        str(lexicon_for_cli),
        str(raw_path),
        str(output_path),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(f"KWS text2token 失败：{details}") from exc
    finally:
        if temporary_lexicon is not None:
            temporary_lexicon.cleanup()
    validate_keywords_file(output_path, tuple(label for _keyword, label in entries))


def generate_keywords(
    *,
    model_dir: Path,
    raw_path: Path,
    output_path: Path,
    keyword: str,
    label: str,
    tokens_type: str = "phone+ppinyin",
) -> None:
    """Single-phrase compatibility wrapper around :func:`generate_keywords_for`."""

    generate_keywords_for(
        model_dir=model_dir,
        raw_path=raw_path,
        output_path=output_path,
        entries=((keyword, label),),
        tokens_type=tokens_type,
    )
