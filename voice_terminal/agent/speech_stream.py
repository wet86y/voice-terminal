"""Incremental, strict decoding of native ``speak`` arguments."""
from __future__ import annotations


class SpeechArgumentStream:
    """Decode ``text`` and optional ``emotion`` without rescanning input.

    New calls should put ``emotion`` before ``text`` so the Bidi session gets
    its task-level voice setting before the first text frame.  A legacy call
    without an emotion field streams with the confirmed ``calm`` default.
    """

    _HEX = set("0123456789abcdefABCDEF")
    _ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f",
                "n": "\n", "r": "\r", "t": "\t"}
    _EMOTIONS = {"calm", "happy", "sad", "angry", "fearful", "disgusted", "surprised"}

    def __init__(self, limit: int = 16384):
        self.raw = ""
        self.emitted = ""
        self.limit = limit
        # A legacy ``speak({"text": ...})`` must keep its low-latency stream.
        # New model prompts emit emotion first; a missing field is therefore
        # already a confirmed calm default rather than a reason to buffer old
        # tool calls until the closing brace.
        self.emotion: str | None = "calm"
        self._cursor = 0
        self._phase = "start"
        self._string_kind: str | None = None
        self._string_buffer: list[str] = []
        self._key: str | None = None
        self._seen: set[str] = set()
        self._escape = False
        self._unicode_digits = ""
        self._pending_high: int | None = None
        self._pending_text = ""

    @staticmethod
    def _space(char: str) -> bool:
        return char in " \r\n\t"

    @property
    def emotion_ready(self) -> bool:
        return self.emotion is not None

    def _append_value_char(self, char: str) -> None:
        if self._key == "text":
            if self.emotion_ready:
                self.emitted += char
            else:
                self._pending_text += char
        else:
            self._string_buffer.append(char)

    def _open_string(self, kind: str) -> None:
        self._string_kind = kind
        self._string_buffer = []
        self._escape = False
        self._unicode_digits = ""
        self._pending_high = None
        self._phase = f"{kind}_string"

    def _close_string(self) -> None:
        if self._string_kind == "key":
            key = "".join(self._string_buffer)
            if key not in {"text", "emotion"} or key in self._seen:
                raise ValueError("speak accepts only text and emotion once")
            self._key = key
            self._phase = "after_key"
        else:
            if self._key == "emotion":
                value = "".join(self._string_buffer)
                if value not in self._EMOTIONS:
                    raise ValueError("invalid speak emotion")
                if self._pending_text or (self.emitted and value != "calm"):
                    raise ValueError("emotion must precede streamed text")
                self.emotion = value
                if self._pending_text:
                    self.emitted += self._pending_text
                    self._pending_text = ""
            self._seen.add(self._key or "")
            self._phase = "after_value"
        self._string_kind = None
        self._string_buffer = []

    def _consume_string(self, char: str) -> None:
        kind = self._string_kind
        if kind is None:
            raise ValueError("invalid speak argument state")
        phase = self._phase
        if phase == f"{kind}_string":
            if self._escape:
                self._escape = False
                if char == "u":
                    self._unicode_digits = ""
                    self._phase = f"{kind}_unicode"
                    return
                decoded = self._ESCAPES.get(char)
                if decoded is None:
                    raise ValueError("invalid JSON escape")
                self._append_value_char(decoded)
                return
            if char == '"':
                self._close_string()
                return
            code = ord(char)
            if code < 32 or 0xD800 <= code <= 0xDFFF:
                raise ValueError("invalid character in speak text")
            if char == "\\":
                self._escape = True
                return
            self._append_value_char(char)
            return
        if phase == f"{kind}_unicode":
            if char not in self._HEX:
                raise ValueError("invalid unicode escape")
            self._unicode_digits += char
            if len(self._unicode_digits) < 4:
                return
            code = int(self._unicode_digits, 16)
            self._unicode_digits = ""
            if 0xDC00 <= code <= 0xDFFF:
                raise ValueError("unpaired low surrogate")
            if 0xD800 <= code <= 0xDBFF:
                self._pending_high = code
                self._phase = f"{kind}_low_slash"
                return
            self._append_value_char(chr(code))
            self._phase = f"{kind}_string"
            return
        if phase == f"{kind}_low_slash":
            if char != "\\":
                raise ValueError("missing low surrogate")
            self._phase = f"{kind}_low_u"
            return
        if phase == f"{kind}_low_u":
            if char != "u":
                raise ValueError("missing low surrogate")
            self._unicode_digits = ""
            self._phase = f"{kind}_low_unicode"
            return
        if phase == f"{kind}_low_unicode":
            if char not in self._HEX:
                raise ValueError("invalid unicode escape")
            self._unicode_digits += char
            if len(self._unicode_digits) < 4:
                return
            low = int(self._unicode_digits, 16)
            if not 0xDC00 <= low <= 0xDFFF:
                raise ValueError("invalid low surrogate")
            high = self._pending_high
            assert high is not None
            self._append_value_char(chr(0x10000 + ((high - 0xD800) << 10) + low - 0xDC00))
            self._pending_high = None
            self._unicode_digits = ""
            self._phase = f"{kind}_string"
            return
        raise ValueError("invalid speak argument state")

    def _consume(self) -> None:
        while self._cursor < len(self.raw):
            char = self.raw[self._cursor]
            phase = self._phase
            if phase == "start":
                if self._space(char):
                    self._cursor += 1; continue
                if char != "{":
                    raise ValueError("invalid speak argument structure")
                self._phase = "before_key"; self._cursor += 1; continue
            if phase == "before_key":
                if self._space(char):
                    self._cursor += 1; continue
                if char != '"':
                    raise ValueError("invalid speak argument structure")
                self._open_string("key"); self._cursor += 1; continue
            if phase.startswith(("key_string", "key_unicode", "key_low_", "value_string", "value_unicode", "value_low_")):
                self._consume_string(char); self._cursor += 1; continue
            if phase == "after_key":
                if self._space(char):
                    self._cursor += 1; continue
                if char != ":":
                    raise ValueError("invalid speak argument structure")
                self._phase = "before_value"; self._cursor += 1; continue
            if phase == "before_value":
                if self._space(char):
                    self._cursor += 1; continue
                if char != '"':
                    raise ValueError("speak values must be strings")
                self._open_string("value"); self._cursor += 1; continue
            if phase == "after_value":
                if self._space(char):
                    self._cursor += 1; continue
                if char == ",":
                    self._phase = "before_key"; self._cursor += 1; continue
                if char == "}":
                    self._phase = "done"; self._cursor += 1; continue
                raise ValueError("trailing speak arguments")
            if phase == "done":
                if not self._space(char):
                    raise ValueError("trailing speak arguments")
                self._cursor += 1; continue
            raise ValueError("invalid speak argument state")

    def feed(self, delta: str) -> str:
        if not isinstance(delta, str):
            raise ValueError("invalid speech argument delta")
        previous = len(self.emitted)
        self.raw += delta
        if len(self.raw) > self.limit:
            raise ValueError("speech arguments too large")
        self._consume()
        return self.emitted[previous:]

    def finish(self) -> str:
        self._consume()
        if self._phase != "done" or "text" not in self._seen:
            raise ValueError("incomplete or empty speak arguments")
        if not self.emitted.strip():
            raise ValueError("incomplete or empty speak arguments")
        return self.emitted
