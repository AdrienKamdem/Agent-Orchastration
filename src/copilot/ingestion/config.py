"""Configuration.

Two layers, on purpose:

* ``Settings``: environment and infrastructure (DB URL, data dir, secrets, SEC contact).
  Read from environment variables or ``.env``. Never committed.
* ``SourcesConfig``: *what* to ingest (watchlist, feeds, filing types).
  Read from ``sources.yaml``. Committed, reviewed like code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DocumentKind = Literal["filing", "cb_statement", "news", "research", "earnings"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://copilot:copilot@localhost:5433/copilot"
    data_dir: Path = Path("data")
    sources_file: Path = Path("sources.yaml")

    # Generic UA for RSS/news. The SEC needs its own, with a real contact address.
    http_user_agent: str = "markets-copilot/0.1 (personal research project)"
    # Required by SEC fair-access policy, e.g. "Jane Doe jane@example.com".
    sec_user_agent: str | None = None

    # "sentence-transformers" = real model (needs `uv sync --extra embeddings`).
    # "hashing" = dependency-free bag-of-words embedder for tests / quick smoke runs.
    embedding_backend: Literal["sentence-transformers", "hashing"] = "sentence-transformers"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_batch_size: int = 32

    chunker: Literal["section_aware", "recursive"] = "section_aware"
    chunk_max_tokens: int = 400
    chunk_overlap_tokens: int = 50

    log_level: str = "INFO"
    log_json: bool = False


# --------------------------------------------------------------------------- sources.yaml


class WatchlistEntry(BaseModel):
    ticker: str
    name: str

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class _SourceBase(BaseModel):
    id: str
    kind: DocumentKind
    enabled: bool = True


class RssSourceConfig(_SourceBase):
    type: Literal["rss"] = "rss"
    url: str
    # If true, `url` is a template expanded once per watchlist entry, using
    # {ticker} and {name} (URL-encoded), e.g. a news search feed per company.
    per_watchlist: bool = False
    # true: download the linked page (central bank statements).
    # false: store only the feed's title + summary (news; respects copyright).
    fetch_full_page: bool = True
    max_items: int = 50
    lookback_days: int = 120


class EdgarSourceConfig(_SourceBase):
    type: Literal["edgar"] = "edgar"
    kind: DocumentKind = "filing"
    # US issuers file 10-K / 10-Q / 8-K. Foreign private issuers (ASML, TSM,
    # TotalEnergies) file 20-F (annual) and 6-K (current reports) instead.
    forms: list[str] = Field(default_factory=lambda: ["10-K", "10-Q", "8-K", "20-F", "6-K"])
    # For these forms the interesting content is usually in exhibits
    # (8-K EX-99.1 = earnings press release), so fetch those too.
    exhibit_forms: list[str] = Field(default_factory=lambda: ["8-K", "6-K"])
    exhibit_types: list[str] = Field(default_factory=lambda: ["EX-99.1", "EX-99.2"])
    lookback_days: int = 365
    max_filings_per_ticker: int = 8


class PdfInboxSourceConfig(_SourceBase):
    type: Literal["pdf_inbox"] = "pdf_inbox"
    kind: DocumentKind = "research"
    # Relative paths are resolved against Settings.data_dir.
    path: Path = Path("inbox")


SourceConfig = Annotated[
    RssSourceConfig | EdgarSourceConfig | PdfInboxSourceConfig, Field(discriminator="type")
]


class SourcesConfig(BaseModel):
    watchlist: list[WatchlistEntry] = Field(default_factory=list)
    sources: list[SourceConfig] = Field(default_factory=list)

    @field_validator("sources")
    @classmethod
    def _unique_ids(cls, v: list[SourceConfig]) -> list[SourceConfig]:
        ids = [s.id for s in v]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate source ids: {sorted(dupes)}")
        return v

    def get(self, source_id: str) -> SourceConfig:
        for s in self.sources:
            if s.id == source_id:
                return s
        raise KeyError(f"unknown source {source_id!r}; known: {[s.id for s in self.sources]}")

    @classmethod
    def load(cls, path: Path) -> SourcesConfig:
        with path.open() as f:
            return cls.model_validate(yaml.safe_load(f) or {})
