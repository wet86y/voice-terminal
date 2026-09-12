from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Any, Iterable, Mapping

from ..audio.player import PlaybackSnapshot


@dataclass(frozen=True)
class SpeechAlignment:
    """A text span and the audio interval that represents it."""

    text: str
    start_ms: int
    end_ms: int | None
    precision: str = "provider"
    source: str = "provider"
    granularity: str = "word"
    valid: bool = True
    text_start: int | None = None
    text_end: int | None = None
    turn_id: str | None = None
    generation_id: int | None = None
    connection_id: int | None = None
    sentence_index: int | None = None


def _number(value: Any) -> int | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return None
    numeric = float(value)
    if not math.isfinite(numeric):
        return None
    return int(round(numeric))


def _normalise_for_match(value: str) -> str:
    """Normalise only what is safe for a prefix comparison.

    TTS providers may return a space-normalised subtitle while the LLM text
    still contains the original whitespace.  Punctuation, digits and CJK
    characters are deliberately retained; removing or rewriting them would
    turn an uncertain mapping into a false claim about what was heard.
    """

    return "".join(value.split()).casefold()


def _covered_prefix_length(generated: str, covered: str) -> int:
    """Return the generated-text prefix represented by ``covered``.

    The return value is measured in the original generated string, so callers
    can display a missing tail without losing punctuation or spacing.  A
    partial match is useful when a provider has only emitted the first few
    subtitle words; a zero result means that no reliable prefix was found.
    """

    if not generated or not covered:
        return 0
    generated_norm = _normalise_for_match(generated)
    covered_norm = _normalise_for_match(covered)
    if not generated_norm or not covered_norm:
        return 0
    common = 0
    for left, right in zip(generated_norm, covered_norm):
        if left != right:
            break
        common += 1
    if common == 0:
        return 0
    # Walk the original string to find the character boundary corresponding
    # to the normalised prefix.  Whitespace belongs to the covered prefix if
    # it occurs before the next spoken character.
    spoken = 0
    for index, char in enumerate(generated):
        if not char.isspace():
            spoken += 1
        if spoken >= common:
            return index + 1
    return len(generated)


def _parse_bidi_words(
    value: Mapping[str, Any],
    *,
    default_precision: str,
    sentence_index: int | None,
) -> list[SpeechAlignment]:
    """Expand one MiniMax Bidi cumulative ``timestamped_words`` snapshot.

    The live Bidi service returns a growing word list and a sentence-local
    clock.  Word indexes provide a stable identity across those cumulative
    snapshots; the resolver later adds the PCM origin captured at
    ``sentence_start``.
    """

    words = value.get("timestamped_words")
    if not isinstance(words, Iterable) or isinstance(words, (str, bytes, bytearray)):
        return []
    parent_text = value.get("text")
    parent_text = parent_text if isinstance(parent_text, str) else ""
    parent_start = _number(value.get("text_begin")) or 0
    result: list[SpeechAlignment] = []
    for item in words:
        if not isinstance(item, Mapping):
            continue
        start = _number(item.get("time_begin"))
        end = _number(item.get("time_end"))
        if start is None or end is None or end < start:
            continue
        word_start = _number(item.get("word_begin"))
        word_end = _number(item.get("word_end"))
        text = item.get("word") or item.get("text") or item.get("content")
        if not isinstance(text, str) and parent_text and word_start is not None and word_end is not None:
            relative_start = max(0, word_start)
            relative_end = max(relative_start, word_end)
            text = parent_text[relative_start:relative_end]
        if not isinstance(text, str) or not text:
            continue
        text_start = parent_start + word_start if word_start is not None else None
        text_end = parent_start + word_end if word_end is not None else None
        result.append(SpeechAlignment(
            text=text,
            start_ms=start,
            end_ms=end,
            precision=default_precision,
            source="minimax_bidi",
            granularity="word",
            valid=True,
            text_start=text_start,
            text_end=text_end,
            sentence_index=sentence_index,
        ))
    return result


def parse_alignment(
    value: Any,
    *,
    default_precision: str = "provider",
    sentence_index: int | None = None,
) -> list[SpeechAlignment]:
    """Parse common MiniMax subtitle shapes without assuming one wire layout.

    ``data.subtitle`` from the current Bidi deployment uses
    ``timestamped_words`` with ``time_begin``/``time_end`` in milliseconds.
    Older generic ``start``/``end`` payloads retain conservative unit
    handling because their units are not self-describing.
    """

    if isinstance(value, Mapping):
        bidi_words = _parse_bidi_words(
            value,
            default_precision=default_precision,
            sentence_index=sentence_index,
        )
        if bidi_words:
            return bidi_words
        text = value.get("text") or value.get("word") or value.get("content")
        start_key = next((key for key in ("start_ms", "time_begin", "start", "start_time") if key in value), None)
        end_key = next((key for key in ("end_ms", "time_end", "end", "end_time") if key in value), None)
        start = _number(value.get(start_key)) if start_key is not None else None
        end = _number(value.get(end_key)) if end_key is not None else None
        if isinstance(text, str) and start is not None:
            declared_unit = str(value.get("time_unit") or value.get("unit") or "").strip().lower()
            unit_is_ms = declared_unit in {"ms", "millisecond", "milliseconds"}
            # MiniMax Bidi's explicit ``time_begin``/``time_end`` fields are
            # milliseconds.  Generic ``start``/``end`` names remain
            # conservative unless an explicit unit is present.
            timestamp_is_reliable = (
                (start_key in {"start_ms", "time_begin"} or unit_is_ms)
                and (end_key in {None, "end_ms", "time_end"} or unit_is_ms)
            )
            if end is None:
                # Some APIs use a duration rather than an end position.
                duration_key = next((key for key in ("duration_ms", "duration") if key in value), None)
                duration = _number(value.get(duration_key)) if duration_key is not None else None
                end = start + duration if duration is not None else None
                if duration_key == "duration" and not unit_is_ms:
                    timestamp_is_reliable = False
            declared_granularity = value.get("granularity")
            if declared_granularity in {"word", "sentence"}:
                granularity = str(declared_granularity)
            elif "word" in value:
                granularity = "word"
            elif "words" in value:
                granularity = "sentence"
            else:
                # A scalar text interval has no reliable indication that it
                # is a word.  Treat it as a sentence/segment boundary so a
                # caller never presents a guessed word edge as exact.
                granularity = "sentence"
            source = str(
                value.get("source")
                or ("minimax_bidi" if start_key == "time_begin" else "provider")
            )
            text_start = _number(value.get("text_begin"))
            text_end = _number(value.get("text_end"))
            return [SpeechAlignment(
                text,
                start,
                end,
                default_precision if timestamp_is_reliable else "uncertain",
                source,
                granularity,
                valid=timestamp_is_reliable,
                text_start=text_start,
                text_end=text_end,
                sentence_index=sentence_index,
            )]
        for key in ("words", "items", "segments", "subtitles", "timestamps", "timestamped_words", "data"):
            if key in value:
                return parse_alignment(
                    value[key],
                    default_precision=default_precision,
                    sentence_index=sentence_index,
                )
        return []
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray)):
        result: list[SpeechAlignment] = []
        for item in value:
            result.extend(parse_alignment(item, default_precision=default_precision, sentence_index=sentence_index))
        return result
    return []


class DeliveryResolver:
    """Resolve what a listener could have received at an interruption point."""

    def __init__(self) -> None:
        self._provider_spans: list[SpeechAlignment] = []
        self._local_spans: list[SpeechAlignment] = []
        self._pending_text: list[dict[str, Any]] = []
        self._last_boundary_ms = 0
        # Some providers reset subtitle timestamps at each sentence.  Once a
        # backwards jump is observed, retaining the raw values as if they
        # shared one clock would falsely classify a later sentence as already
        # heard.  Keep those spans in arrival order and mark the affected
        # timestamp segment untrusted; the resolver can still expose the text as
        # uncertain without inventing an offset.
        self._provider_last_raw_start_ms: int | None = None
        self._provider_timebase_reset = False

    @property
    def spans(self) -> tuple[SpeechAlignment, ...]:
        return tuple(self._provider_spans or self._local_spans)

    @property
    def pending_count(self) -> int:
        return sum(1 for item in self._pending_text if item["end_ms"] is None)

    @property
    def diagnostics(self) -> dict[str, int | None]:
        """Return non-content timing diagnostics for delivery reconciliation."""

        spans = list(self._provider_spans)
        starts = [int(item.start_ms) for item in spans]
        ends = [int(item.end_ms) for item in spans if item.end_ms is not None]
        values = starts + ends
        return {
            "provider_span_count": len(spans),
            "provider_min_ms": min(values) if values else None,
            "provider_max_ms": max(values) if values else None,
        }

    def complete_pending(self, end_ms: int) -> None:
        """Close all locally tracked spans once the provider is fully drained."""

        while self.pending_count:
            self.mark_boundary(end_ms)

    @staticmethod
    def _span_identity(span: SpeechAlignment) -> tuple[Any, ...]:
        """Return a stable identity for cumulative provider snapshots."""

        if span.sentence_index is not None and span.text_start is not None and span.text_end is not None:
            return ("sentence", span.sentence_index, span.text_start, span.text_end)
        if span.sentence_index is not None:
            return ("sentence-value", span.sentence_index, span.text, span.start_ms)
        return ("value", span.text, span.start_ms, span.end_ms)

    def add_provider_alignment(self, value: Any) -> None:
        """Add provider spans and normalize Bidi sentence-local timestamps.

        The MiniMax adapter wraps raw subtitle data with the PCM origin
        captured at ``sentence_start``.  This method accepts that wrapper so
        controllers never need to understand provider-specific field names.
        """

        metadata: Mapping[str, Any] = {}
        raw_value = value
        if isinstance(value, Mapping) and "value" in value and (
            "audio_base_ms" in value or "sentence_index" in value or "connection_id" in value
        ):
            metadata = value
            raw_value = value.get("value")
        sentence_index = _number(metadata.get("sentence_index")) if metadata else None
        base_value = metadata.get("audio_base_ms") if metadata else None
        audio_base_ms = _number(base_value) if base_value is not None else 0
        spans = parse_alignment(raw_value, sentence_index=sentence_index)
        # A sentence index without its PCM origin can identify duplicate
        # snapshots but cannot place them on the task clock.  The first
        # sentence is safe at origin zero; later unanchored segments remain
        # explicitly uncertain.
        anchored = base_value is not None or sentence_index in (None, 0)
        for span in spans:
            if metadata:
                end_ms = span.end_ms + audio_base_ms if span.end_ms is not None else None
                span = replace(
                    span,
                    start_ms=span.start_ms + audio_base_ms,
                    end_ms=end_ms,
                    sentence_index=sentence_index,
                )
            if not anchored:
                span = replace(span, valid=False, precision="uncertain")
            elif span.granularity == "sentence" and span.sentence_index is not None and any(
                item.sentence_index == span.sentence_index and item.granularity == "word"
                for item in self._provider_spans
            ):
                # A late sentence envelope must not reintroduce a duplicate
                # whole-sentence span after word timing has already arrived.
                continue
            elif span.granularity == "word" and span.sentence_index is not None:
                # Some deployments emit the sentence text interval before
                # the first word snapshot.  Once word timing arrives, the
                # scalar interval is only a provisional envelope and must
                # not be counted alongside the word spans.
                self._provider_spans = [
                    item
                    for item in self._provider_spans
                    if not (
                        item.sentence_index == span.sentence_index
                        and item.granularity == "sentence"
                    )
                ]
            identity = self._span_identity(span)
            existing_index = next(
                (
                    index
                    for index, item in enumerate(self._provider_spans)
                    if self._span_identity(item) == identity
                ),
                None,
            )
            if anchored and sentence_index is None and existing_index is None:
                # Preserve the conservative behavior for legacy providers
                # whose raw timestamp clock may reset without a segment marker.
                if (
                    self._provider_last_raw_start_ms is not None
                    and span.start_ms < self._provider_last_raw_start_ms
                ):
                    self._provider_timebase_reset = True
                if self._provider_timebase_reset:
                    span = replace(span, valid=False, precision="uncertain")
                self._provider_last_raw_start_ms = span.start_ms
            if existing_index is None:
                self._provider_spans.append(span)
            else:
                # A later cumulative snapshot may refine a word's end time;
                # replace the old version instead of duplicating it.
                self._provider_spans[existing_index] = span
        self._provider_spans.sort(
            key=lambda item: (
                item.start_ms,
                item.end_ms or item.start_ms,
                item.sentence_index if item.sentence_index is not None else -1,
                item.text_start if item.text_start is not None else -1,
            )
        )

    def add_text(self, text: str, start_ms: int) -> None:
        if text.strip():
            self._pending_text.append({"text": text.strip(), "start_ms": max(0, int(start_ms)), "end_ms": None})

    def mark_boundary(self, end_ms: int) -> None:
        end = max(self._last_boundary_ms, int(end_ms))
        self._last_boundary_ms = end
        for item in self._pending_text:
            if item["end_ms"] is None:
                item["end_ms"] = end
                self._local_spans.append(SpeechAlignment(
                    item["text"], item["start_ms"], end, "sentence", "local", "sentence"
                ))
                break

    @staticmethod
    def _render_spans(
        spans: list[SpeechAlignment],
        classifications: list[str],
        generated: str,
    ) -> dict[str, list[str]]:
        """Render provider spans with the original generated spacing.

        Word subtitle payloads often omit the whitespace surrounding a word.
        Concatenating those payloads would silently change the assistant
        history (for example ``"hello"`` + ``"world"`` would become
        ``"helloworld"``).  Consume the matching prefix in the generated
        source when possible and fall back to the provider text if its shape
        cannot be validated.
        """

        rendered: dict[str, list[str]] = {"heard": [], "uncertain": [], "unheard": []}
        cursor = 0
        for span, kind in zip(spans, classifications):
            value = span.text
            if generated and cursor < len(generated):
                length = _covered_prefix_length(generated[cursor:], span.text)
                if length:
                    value = generated[cursor : cursor + length]
                    cursor += length
            rendered.setdefault(kind, []).append(value)
        return rendered

    def resolve(
        self,
        snapshot: PlaybackSnapshot | Mapping[str, Any],
        generated_text: str = "",
        *,
        overlap_start_ms: int | None = None,
        overlap_stop_ms: int | None = None,
    ) -> dict[str, Any]:
        if isinstance(snapshot, PlaybackSnapshot):
            played_ms = snapshot.output_estimated_ms
            started = snapshot.started
        else:
            played_ms = int(snapshot.get("output_estimated_ms", snapshot.get("played_ms", 0)) or 0)
            started = bool(snapshot.get("started", False))
        spans = list(self._provider_spans or self._local_spans)
        if not spans:
            if played_ms <= 0:
                return {
                    "status": "none",
                    "heard_text": "",
                    "uncertain_text": "",
                    "unheard_text": generated_text.strip(),
                    "precision": "unknown",
                    "played_ms": played_ms,
                }
            return {
                "status": "unknown" if started else "none",
                "heard_text": "",
                "uncertain_text": generated_text.strip() if started else "",
                "unheard_text": generated_text.strip() if not started else "",
                "precision": "unknown",
                "played_ms": played_ms,
            }

        classifications: list[str] = []
        for span in spans:
            if not span.valid:
                classifications.append("uncertain")
            elif span.end_ms is not None and span.end_ms <= played_ms:
                classifications.append("heard")
            elif span.start_ms <= played_ms or (span.start_ms == 0 and started and played_ms > 0):
                classifications.append("uncertain")
            else:
                classifications.append("unheard")
        if overlap_start_ms is not None:
            overlap_end = overlap_stop_ms if overlap_stop_ms is not None else played_ms
            if overlap_end < overlap_start_ms:
                overlap_end = overlap_start_ms
            for index, span in enumerate(spans):
                # Treat an open-ended span as overlapping until we have a
                # trustworthy provider boundary.  A word which straddles the
                # user's onset or the software stop is never reported as
                # definitely heard.
                span_end = span.end_ms if span.end_ms is not None else overlap_end
                if span.start_ms < overlap_end and span_end > overlap_start_ms:
                    classifications[index] = "uncertain"
        rendered = self._render_spans(spans, classifications, generated_text)
        heard = rendered["heard"]
        uncertain = rendered["uncertain"]
        unheard = rendered["unheard"]
        precision = "provider" if self._provider_spans else "sentence"
        generated = generated_text.strip()
        covered_text = "".join(span.text for span in spans)
        covered_prefix = _covered_prefix_length(generated, covered_text)
        if generated and covered_prefix < len(generated):
            # The provider has not supplied a trustworthy boundary for the
            # remainder.  Keep the known prefix, but classify the tail as
            # uncertain rather than claiming it was heard by proportion of
            # characters.  If no output sample has started, however, the
            # whole generated tail is safely classified as unheard.
            missing = generated[covered_prefix:].strip()
            if missing:
                if played_ms <= 0 or not started:
                    unheard.append(missing)
                else:
                    uncertain.append(missing)
                    precision = "uncertain"
        if uncertain:
            precision = "uncertain"
        status = "full" if not unheard and not uncertain else ("partial" if heard else ("unknown" if uncertain else "none"))
        return {
            "status": status,
            "heard_text": "".join(heard).strip(),
            "uncertain_text": "".join(uncertain).strip(),
            "unheard_text": "".join(unheard).strip(),
            "precision": precision,
            "played_ms": played_ms,
        }
