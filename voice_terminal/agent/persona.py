"""Episode-scoped Markdown personality document loading.

The documents loaded here are deliberately opaque natural-language input.  The
loader only handles filesystem concerns and records whether each configured
document was available; the Agent remains responsible for placing the snapshot
in the static context lane.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PersonaDocumentStatus:
    """Load status for one configured document, without exposing its content."""

    name: str
    path: str
    loaded: bool
    chars: int = 0
    reason: str = ""


@dataclass(frozen=True)
class PersonaDocuments:
    """Immutable document snapshot owned by one Agent episode."""

    personality: str = ""
    user: str = ""
    statuses: tuple[PersonaDocumentStatus, ...] = ()


class PersonaDocumentLoader:
    """Read configured Markdown files once and return an episode snapshot.

    Missing or unreadable documents are intentionally non-fatal.  A warning is
    emitted for operators, while the remaining documents and the built-in
    Agent protocol continue to work.
    """

    _SPECS = (
        ("personality", "personality_file"),
        ("user", "user_file"),
    )

    def __init__(self, config: Any) -> None:
        self.config = config

    def _path(self, filename_key: str) -> Path:
        directory = Path(getattr(self.config, "directory"))
        filename = str(getattr(self.config, filename_key))
        return directory / filename

    @staticmethod
    def _read(path: Path) -> str:
        # Keep line endings and document text intact.  The loader only checks
        # whether the file is empty after reading; it never rewrites content.
        with path.open("r", encoding="utf-8", newline="") as stream:
            return stream.read()

    def load(self) -> PersonaDocuments:
        values: dict[str, str] = {}
        statuses: list[PersonaDocumentStatus] = []

        for name, filename_key in self._SPECS:
            path = self._path(filename_key)
            try:
                content = self._read(path)
            except FileNotFoundError as exc:
                self._warn(name, path, "missing", exc)
                statuses.append(PersonaDocumentStatus(name=name, path=str(path), loaded=False, reason="missing"))
                continue
            except UnicodeDecodeError as exc:
                self._warn(name, path, "invalid_utf8", exc)
                statuses.append(PersonaDocumentStatus(name=name, path=str(path), loaded=False, reason="invalid_utf8"))
                continue
            except OSError as exc:
                reason = f"read_error:{exc.__class__.__name__}"
                self._warn(name, path, reason, exc)
                statuses.append(PersonaDocumentStatus(name=name, path=str(path), loaded=False, reason=reason))
                continue

            if not content.strip():
                self._warn(name, path, "empty", None)
                statuses.append(PersonaDocumentStatus(name=name, path=str(path), loaded=False, reason="empty"))
                continue

            values[name] = content
            statuses.append(PersonaDocumentStatus(
                name=name,
                path=str(path),
                loaded=True,
                chars=len(content),
            ))

        return PersonaDocuments(
            personality=values.get("personality", ""),
            user=values.get("user", ""),
            statuses=tuple(statuses),
        )

    @staticmethod
    def _warn(name: str, path: Path, reason: str, exc: BaseException | None) -> None:
        detail = f" error={str(exc)[:200]}" if exc is not None else ""
        logger.warning(
            "agent.persona_document_skipped document=%s path=%s reason=%s%s",
            name,
            path,
            reason,
            detail,
        )
