from __future__ import annotations

import os
import hashlib
from pathlib import Path

TEXT_SUFFIXES = {".txt", ".md", ".json", ".yaml", ".yml", ".csv", ".tsv", ".py", ".ts", ".js", ".vue", ".css", ".html", ".toml", ".rst"}


class FileTools:
    def __init__(self, roots: tuple[Path, ...]) -> None:
        self.roots = tuple(p.resolve() for p in roots)

    def resolve(self, value: str) -> Path:
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise ValueError("invalid path")
        candidate = Path(value)
        if not candidate.is_absolute():
            if not self.roots:
                raise PermissionError("no allowed directories")
            candidate = self.roots[0] / candidate
        path = candidate.resolve(strict=True)
        if not any(path.is_relative_to(root) for root in self.roots):
            raise PermissionError("path outside allowed directories")
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            raise ValueError("only ordinary text files are supported")
        return path

    def normalize(self, name: str, arguments: dict) -> tuple[str, dict]:
        name = {"files.search": "files_search", "files.read_text": "files_read_text"}.get(name, name)
        if name == "files_search":
            if set(arguments) != {"query"} or not isinstance(arguments["query"], str) or not 1 <= len(arguments["query"].strip()) <= 256:
                raise ValueError("search requires a nonempty filename query")
            return name, {"query": arguments["query"].strip().casefold()}
        if name == "files_read_text" and set(arguments) == {"path"}:
            return name, {"path": os.path.normcase(str(self.resolve(arguments["path"])))}
        raise ValueError("unknown or invalid read-only tool")

    @staticmethod
    def version(path: Path) -> str:
        stat = path.stat()
        return f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}"

    def search_version(self) -> str:
        digest = hashlib.sha256()
        count = 0
        for root in self.roots:
            for folder, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not (Path(folder) / d).is_symlink() and not (Path(folder) / d).is_junction())
                for filename in sorted(files):
                    path = Path(folder) / filename
                    if path.suffix.lower() not in TEXT_SUFFIXES:
                        continue
                    count += 1
                    if count > 10000:
                        raise ValueError("directory too large to validate cached search")
                    digest.update((str(path) + self.version(path)).encode("utf-8"))
        return digest.hexdigest()

    def execute(self, name: str, arguments: dict) -> dict:
        name, arguments = self.normalize(name, arguments)
        if name == "files_read_text":
            path = self.resolve(arguments["path"])
            before = self.version(path)
            with path.open("rb") as handle:
                raw = handle.read(16385)
                # Verify opened object, as well as the resolved path, against replacement.
                opened = os.fstat(handle.fileno())
                current = path.stat()
                if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                    raise RuntimeError("file changed during read")
            if self.resolve(str(path)) != path or self.version(path) != before:
                raise RuntimeError("file changed during read")
            if b"\x00" in raw:
                raise ValueError("binary file")
            return {"path": str(path), "text": raw[:16384].decode("utf-8-sig", errors="replace"),
                    "truncated": len(raw) > 16384, "version": before}
        found = []
        scanned = 0
        for root in self.roots:
            if not root.is_dir():
                continue
            for folder, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not (Path(folder) / d).is_symlink()
                                 and not (Path(folder) / d).is_junction()
                                 and (Path(folder) / d).resolve().is_relative_to(root))
                for filename in sorted(files):
                    scanned += 1
                    if scanned > 10000:
                        return {"files": found, "truncated": True}
                    if arguments["query"] not in filename.casefold():
                        continue
                    try:
                        path = self.resolve(str(Path(folder) / filename))
                        found.append({"path": str(path), "version": self.version(path)})
                    except (OSError, ValueError, PermissionError):
                        continue
                    if len(found) >= 20:
                        return {"files": found, "truncated": True, "version": self.search_version()}
        return {"files": found, "truncated": False, "version": self.search_version()}

    def fresh(self, name: str, result: dict) -> bool:
        # Search results can change as directories change; rerun at final selection.
        if name != "files_read_text":
            try:
                return self.search_version() == result.get("version")
            except (OSError, ValueError):
                return False
        try:
            return self.version(self.resolve(result["path"])) == result["version"]
        except (OSError, ValueError, KeyError):
            return False

    def schemas(self) -> list[dict]:
        roots = ", ".join(map(str, self.roots))
        return [{"type": "function", "function": {"name": name, "description": description,
                 "parameters": {"type": "object", "properties": {key: {"type": "string"}},
                                "required": [key], "additionalProperties": False}}}
                for name, key, description in (
                    ("files_search", "query", f"Search filenames containing query in allowed roots: {roots}. Read only."),
                    ("files_read_text", "path", "Read up to 16 KB of UTF-8 text from an allowed path. Read only."))]
