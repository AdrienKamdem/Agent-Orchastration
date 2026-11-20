# Markets Research & Portfolio Copilot

A personal markets research and portfolio copilot. It ingests public filings, central bank statements, news and research into a searchable store, answers questions over portfolio data with Text2SQL, and writes a daily brief with citations using multiple agents.

## Status

| Phase | Status | Doc |
|---|---|---|
| 1. Document ingestion (weeks 1–3) | Skeleton implemented, needs live-source validation | [docs/ingestion/PLAN.md](docs/ingestion/PLAN.md) |
| 2. Text2SQL + eval set (weeks 4–6) | Not started | |
| 3. Deployment, tracing, write-up (weeks 7–8) | Not started | |

Free, public data sources only.

## Quickstart (ingestion)

Prerequisites: Python 3.12+, [uv](https://docs.astral.sh/uv/), Docker.

```bash
uv sync --extra embeddings
```

`--extra embeddings` installs sentence-transformers and PyTorch (~1–2 GB). To skip it for
a first try, run `uv sync` and set `EMBEDDING_BACKEND=hashing` in `.env`.

```bash
cp .env.example .env
```

Edit `.env` and set `SEC_USER_AGENT="Your Name your.email@example.com"`. The SEC requires it.

```bash
docker compose up -d
```

```bash
uv run ingest init
```

```bash
uv run ingest run
```

The first run downloads the bge-small model (~130 MB) and the watchlist's filings, which takes a few minutes.

```bash
uv run ingest search "export controls risk" --ticker NVDA
```

```bash
uv run ingest status
```

Other commands: `ingest sources`, `ingest run --source fed_monetary`, `ingest process`,
`ingest retry`, `ingest reindex`, and `ingest --help` for the full list.

To ingest your own PDFs, drop them in `data/inbox/` and run `uv run ingest run --source pdf_inbox`.

**What to ingest** (watchlist, feeds, filing types) lives in [`sources.yaml`](sources.yaml).
**How** (DB, secrets, chunking, embedding model) lives in `.env`.

### Layout

```
src/copilot/ingestion/
  connectors/   rss.py, edgar.py, pdf_inbox.py   talk to the outside world, nothing else
  parsers/      html_article, edgar_filing, pdf  raw bytes -> clean text + sections/pages
  chunking/     recursive, section_aware         text -> chunks with exact char offsets
  embedding/                                     bge-small (local) / hashing (tests)
  pipeline.py                                    acquire + process, status per document
  db.py, migrations/                             Postgres + pgvector, plain SQL
  http.py                                        retries, rate limits, conditional GET
  blobstore.py                                   content-addressed raw store (data/raw)
  cli.py                                         `ingest` command
```

### Development

```bash
uv run pytest
```

That runs the unit tests: offline, mocked HTTP.

```bash
TEST_DATABASE_URL=postgresql://copilot:copilot@localhost:5432/copilot_test uv run pytest -m integration
```

That runs the integration tests. They **drop and recreate the schema** in that database, so create it first with:

```bash
docker compose exec postgres createdb -U copilot copilot_test
```

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src
```

CI (GitHub Actions) runs all of the above, with the integration tests against a pgvector service container.
