"""Command-line entry point: ``ingest <command>``.

ingest init                       create/upgrade the schema, register sources
ingest sources                    show what's configured
ingest run [--source ID]          acquire + process (the scheduled job)
ingest process                    parse/chunk/embed anything pending
ingest retry                      reset failed documents and process them again
ingest reindex                    rebuild chunks/embeddings from raw (after config change)
ingest search "query"             semantic search over chunks
ingest status                     recent runs, document counts, failures
"""

from __future__ import annotations

import textwrap
from typing import Annotated

import typer

from . import db
from .blobstore import LocalBlobStore
from .chunking import make_chunker
from .config import Settings, SourcesConfig
from .embedding import Embedder, make_embedder
from .log_setup import configure_logging
from .pipeline import Pipeline, ensure_dirs, make_connector, make_http_client

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)

SourceOpt = Annotated[
    str, typer.Option("--source", "-s", help="Source id from sources.yaml, or 'all'")
]


class _Ctx:
    def __init__(self) -> None:
        self.settings = Settings()
        configure_logging(self.settings.log_level, self.settings.log_json)
        self.sources = SourcesConfig.load(self.settings.sources_file)
        ensure_dirs(self.settings)
        self.conn = db.connect(self.settings.database_url)
        self.store = LocalBlobStore(self.settings.data_dir)
        self.chunker = make_chunker(
            self.settings.chunker,
            self.settings.chunk_max_tokens,
            self.settings.chunk_overlap_tokens,
        )
        self._embedder: Embedder | None = None

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            s = self.settings
            self._embedder = make_embedder(
                s.embedding_backend, s.embedding_model, s.embedding_batch_size
            )
        return self._embedder

    def pipeline(self) -> Pipeline:
        return Pipeline(self.conn, self.store, self.chunker, self.embedder)


@app.command()
def init() -> None:
    """Apply database migrations and register the sources from sources.yaml."""
    ctx = _Ctx()
    applied = db.migrate(ctx.conn)
    for cfg in ctx.sources.sources:
        db.upsert_source(ctx.conn, cfg.id, cfg.kind, cfg.model_dump(mode="json"))
    ctx.conn.commit()
    typer.echo(f"migrations applied: {applied or 'none (up to date)'}")
    typer.echo(f"sources registered: {[s.id for s in ctx.sources.sources]}")


@app.command()
def sources() -> None:
    """List configured sources and the watchlist."""
    s = SourcesConfig.load(Settings().sources_file)
    typer.echo("Watchlist: " + ", ".join(f"{w.ticker} ({w.name})" for w in s.watchlist))
    for cfg in s.sources:
        flag = "on " if cfg.enabled else "off"
        typer.echo(f"  [{flag}] {cfg.id:<22} {cfg.type:<10} {cfg.kind}")


@app.command()
def run(
    source: SourceOpt = "all",
    refresh: Annotated[bool, typer.Option(help="Re-fetch known items to detect changes")] = False,
    process: Annotated[bool, typer.Option(help="Parse/chunk/embed after fetching")] = True,
) -> None:
    """Fetch new documents from sources, then process them. This is the scheduled job."""
    ctx = _Ctx()
    targets = (
        [c for c in ctx.sources.sources if c.enabled]
        if source == "all"
        else [ctx.sources.get(source)]
    )
    any_failed = False
    with make_http_client(ctx.settings) as http:
        pipe = Pipeline(ctx.conn, ctx.store, ctx.chunker, None)
        for cfg in targets:
            try:
                connector = make_connector(cfg, ctx.settings, ctx.sources, http)
            except Exception as e:
                typer.secho(f"{cfg.id}: cannot start connector: {e}", fg="red", err=True)
                any_failed = True
                continue
            st = pipe.acquire(connector, cfg, refresh=refresh)
            any_failed |= st.failed > 0 or any(e.startswith("discover") for e in st.errors)
            typer.echo(
                f"{cfg.id:<22} discovered={st.discovered} new={st.new} "
                f"new_versions={st.new_versions} skipped={st.skipped} failed={st.failed}"
            )
            for err in st.errors[:5]:
                typer.secho(f"    {err}", fg="yellow", err=True)
    if process:
        p = ctx.pipeline().process(None if source == "all" else source)
        typer.echo(f"processed={p.processed} chunks={p.chunks} failed={p.failed}")
        any_failed |= p.failed > 0
    raise typer.Exit(code=1 if any_failed else 0)


@app.command("process")
def process_cmd(source: SourceOpt = "all") -> None:
    """Parse, chunk and embed documents that are fetched but not yet embedded."""
    ctx = _Ctx()
    p = ctx.pipeline().process(None if source == "all" else source)
    typer.echo(f"processed={p.processed} chunks={p.chunks} failed={p.failed}")


@app.command()
def retry(source: SourceOpt = "all") -> None:
    """Reset failed documents to 'fetched' and process them again."""
    ctx = _Ctx()
    n = db.reset_failed(ctx.conn, None if source == "all" else source)
    typer.echo(f"reset {n} failed document(s)")
    p = ctx.pipeline().process(None if source == "all" else source)
    typer.echo(f"processed={p.processed} chunks={p.chunks} failed={p.failed}")


@app.command()
def reindex(source: SourceOpt = "all") -> None:
    """Rebuild chunks + embeddings from the raw store with the current settings.

    Example, comparing chunkers: CHUNKER=recursive CHUNK_MAX_TOKENS=256 ingest reindex
    """
    ctx = _Ctx()
    typer.echo(f"reindexing with chunker={ctx.chunker.name} embedder={ctx.embedder.name}")
    p = ctx.pipeline().reindex(None if source == "all" else source)
    typer.echo(f"processed={p.processed} chunks={p.chunks} failed={p.failed}")


@app.command()
def search(
    query: str,
    k: Annotated[int, typer.Option("-k")] = 5,
    ticker: Annotated[str | None, typer.Option(help="Filter on ticker, e.g. NVDA")] = None,
    source: Annotated[str | None, typer.Option("--source", "-s")] = None,
) -> None:
    """Semantic search over indexed chunks (sanity check for ingestion quality)."""
    ctx = _Ctx()
    qv = ctx.embedder.embed_query(query)
    hits = db.search(
        ctx.conn,
        qv,
        model=ctx.embedder.name,
        chunker=ctx.chunker.name,
        k=k,
        source_id=source,
        ticker=ticker,
    )
    if not hits:
        typer.echo("no results (has anything been embedded with the current model/chunker?)")
    for i, h in enumerate(hits, 1):
        date = h.published_at.date().isoformat() if h.published_at else "?"
        where = " · ".join(x for x in (h.section, f"p.{h.page}" if h.page else None) if x)
        typer.secho(f"\n[{i}] {h.score:.3f}  {h.title}  ({h.source_id}, {date})", bold=True)
        if where:
            typer.echo(f"    {where}")
        if h.url:
            typer.echo(f"    {h.url}")
        typer.echo(textwrap.indent(textwrap.shorten(h.text, 500), "    "))


@app.command()
def status() -> None:
    """Show recent runs, document counts per status, and recent failures."""
    ctx = _Ctx()
    typer.secho("Recent runs", bold=True)
    for r in db.recent_runs(ctx.conn, 15):
        s = r["stats"] or {}
        typer.echo(
            f"  #{r['id']:<5} {r['started_at']:%Y-%m-%d %H:%M} {r['source_id']:<22} "
            f"{r['status']:<8} new={s.get('new', 0)} skipped={s.get('skipped', 0)} "
            f"failed={s.get('failed', 0)}"
        )
    typer.secho("\nDocuments", bold=True)
    for r in db.status_counts(ctx.conn):
        typer.echo(f"  {r['source_id']:<22} {r['status']:<9} {r['n']}")
    failed = db.failed_documents(ctx.conn)
    if failed:
        typer.secho("\nRecent failures", bold=True, fg="red")
        for f in failed:
            typer.echo(f"  doc {f['id']} [{f['source_id']}] {f['title']}: {f['error']}")


if __name__ == "__main__":
    app()
