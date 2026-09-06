"""Incremental, strict decoding of the sole speak argument (never content)."""
from __future__ import annotations


class SpeechArgumentStream:
    """Decode ``{"text": "..."}`` without rescanning the growing prefix."""

    _HEX = set("0123456789abcdefABCDEF")
    _ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f",
                "n": "\n", "r": "\r", "t": "\t"}

    def __init__(self, limit: int = 16384):
        self.raw = ""
        self.emitted = ""
        self.limit = limit
        self._cursor = 0
        self._phase = "start"
        self._string_kind: str | None = None
        self._string_buffer: list[str] = []
        self._escape = False
        self._unicode_digits = ""
        self._pending_high: int | None = None

    @staticmethod
    def _space(char: str) -> bool:
        return char in " \r\n\t"

    def _append_string_char(self, char: str) -> None:
        if self._string_kind == "value":
            self.emitted += char
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
            if "".join(self._string_buffer) != "text":
                raise ValueError("speak accepts only text")
            self._phase = "after_key"
        else:
            self._phase = "after_value"
        self._string_kind = None
        self._string_buffer = []

    def _consume_string(self, char: str) -> None:
        phase = self._phase
        kind = self._string_kind
        if kind is None:
            raise ValueError("invalid speak argument state")
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
                self._append_string_char(decoded)
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
            self._append_string_char(char)
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
            self._append_string_char(chr(code))
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
            code = 0x10000 + ((high - 0xD800) << 10) + low - 0xDC00
            self._append_string_char(chr(code))
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
                    self._cursor += 1
                    continue
                if char != "{":
                    raise ValueError("invalid speak argument structure")
                self._phase = "before_key"
                self._cursor += 1
                continue
            if phase == "before_key":
                if self._space(char):
                    self._cursor += 1
                    continue
                if char != '"':
                    raise ValueError("invalid speak argument structure")
                self._open_string("key")
                self._cursor += 1
                continue
            if phase.startswith(("key_string", "key_unicode", "key_low_")):
                self._consume_string(char)
                self._cursor += 1
                continue
            if phase == "after_key":
                if self._space(char):
                    self._cursor += 1
                    continue
                if char != ":":
                    raise ValueError("invalid speak argument structure")
                self._phase = "before_value"
                self._cursor += 1
                continue
            if phase == "before_value":
                if self._space(char):
                    self._cursor += 1
                    continue
                if char != '"':
                    raise ValueError("text must be a string")
                self._open_string("value")
                self._cursor += 1
                continue
            if phase.startswith(("value_string", "value_unicode", "value_low_")):
                self._consume_string(char)
                self._cursor += 1
                continue
            if phase == "after_value":
                if self._space(char):
                    self._cursor += 1
                    continue
                if char != "}":
                    raise ValueError("trailing speak arguments")
                self._phase = "done"
                self._cursor += 1
                continue
            if phase == "done":
                if not self._space(char):
                    raise ValueError("trailing speak arguments")
                self._cursor += 1
                continue
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
        if self._phase != "done" or not self.emitted.strip():
            raise ValueError("incomplete or empty speak arguments")
        return self.emitted
