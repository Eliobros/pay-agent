"""Private storage for refund evidence.

Files are written to a directory that is never mounted as static content. The
database keeps only a relative path; the bytes leave the backend exclusively
through an authenticated owner endpoint. The interface is small (put / read /
delete) so a cloud driver can be added without touching the refund workflow.
"""

import re
import uuid
from pathlib import Path

from . import config

_SAFE = re.compile(r"[^a-zA-Z0-9_-]")


class StorageError(RuntimeError):
    """A stored file is missing or points outside the evidence root."""


class LocalStorage:
    driver = "local"

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _folder(self, store_id: str) -> Path:
        safe = _SAFE.sub("", store_id or "")[:64] or "store"
        return self.root / safe

    def put(self, store_id: str, data: bytes, suffix: str) -> str:
        folder = self._folder(store_id)
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{uuid.uuid4().hex}{suffix}"
        (folder / name).write_bytes(data)
        return f"{folder.name}/{name}"

    def _resolve(self, relative: str) -> Path:
        candidate = (self.root / relative).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise StorageError("invalid evidence path")
        return candidate

    def read(self, relative: str) -> bytes:
        path = self._resolve(relative)
        if not path.is_file():
            raise StorageError("evidence file is missing")
        return path.read_bytes()

    def delete(self, relative: str) -> None:
        try:
            self._resolve(relative).unlink(missing_ok=True)
        except (StorageError, OSError):
            pass


_storage: LocalStorage | None = None


def get_storage() -> LocalStorage:
    global _storage
    if _storage is None:
        _storage = LocalStorage(config.EVIDENCE_DIR)
    return _storage


def reset_storage(root: str | Path | None = None) -> LocalStorage:
    """Point the storage at another root (tests, or a new volume)."""
    global _storage
    _storage = LocalStorage(root or config.EVIDENCE_DIR)
    return _storage
