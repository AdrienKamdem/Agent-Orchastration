"""Local drop folder for PDFs: research papers, reports, transcripts you have rights to.

Drop a file in ``data/inbox/``. On the next run it's stored in the raw store, then moved to
``data/inbox/processed/``. The external ID is the file's sha256, so the same PDF dropped
twice (even renamed) is only ingested once.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

from ..blobstore import sha256
from ..config import PdfInboxSourceConfig
from ..models import DiscoveredItem, RawDocument
from .base import Cursor, raw_from_inline


class PdfInboxConnector:
    def __init__(self, config: PdfInboxSourceConfig, data_dir: Path):
        self.config = config
        self.source_id = config.id
        path = config.path if config.path.is_absolute() else data_dir / config.path
        self.inbox = path
        self.processed = path / "processed"

    def discover(self, cursor: Cursor | None) -> tuple[list[DiscoveredItem], Cursor]:
        self.inbox.mkdir(parents=True, exist_ok=True)
        items: list[DiscoveredItem] = []
        for f in sorted(self.inbox.iterdir()):
            if not (f.is_file() and f.suffix.lower() == ".pdf"):
                continue
            data = f.read_bytes()
            items.append(
                DiscoveredItem(
                    external_id=sha256(data),
                    url=None,
                    title=f.stem.replace("_", " "),
                    published_at=datetime.fromtimestamp(f.stat().st_mtime, tz=UTC),
                    metadata={"filename": f.name, "inbox_path": str(f)},
                    inline_content=data,
                    inline_mime="application/pdf",
                )
            )
        return items, cursor or {}

    def fetch(self, item: DiscoveredItem) -> RawDocument:
        return raw_from_inline(self.source_id, item)

    def acknowledge(self, item: DiscoveredItem) -> None:
        src = Path(item.metadata["inbox_path"])
        if src.exists():
            self.processed.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), self.processed / src.name)
