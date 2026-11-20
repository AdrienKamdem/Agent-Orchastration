"""Storage for raw and parsed files.

The raw store is the "bronze layer": exact bytes as downloaded, never modified. Keys are
content-addressed (``sha256``), so writing the same bytes twice is a no-op, and the rest
of the pipeline can always be rebuilt from here without touching the network.

Local disk for now. On cloud deployment, add an ``S3BlobStore`` with the same three
methods; nothing else changes.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

_EXT_OVERRIDES = {"text/html": "html", "application/pdf": "pdf", "text/plain": "txt"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extension_for(mime_type: str) -> str:
    base = mime_type.split(";")[0].strip().lower()
    if base in _EXT_OVERRIDES:
        return _EXT_OVERRIDES[base]
    guessed = mimetypes.guess_extension(base)
    return guessed.lstrip(".") if guessed else "bin"


def raw_key(source_id: str, when: datetime, digest: str, mime_type: str) -> str:
    safe_source = source_id.replace(":", "_").replace("/", "_")
    return f"raw/{safe_source}/{when:%Y}/{when:%m}/{digest}.{extension_for(mime_type)}"


def parsed_key(document_id: int) -> str:
    return f"parsed/{document_id}.json"


class BlobStore(Protocol):
    def put(self, key: str, data: bytes) -> str: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...


class LocalBlobStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if not p.is_relative_to(self.root.resolve()):
            raise ValueError(f"key escapes blob root: {key!r}")
        return p

    def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file then rename: a crash never leaves a half-written blob.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return key

    def put_json(self, key: str, obj: Any) -> str:
        return self.put(key, json.dumps(obj, indent=2, default=str).encode())

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()
