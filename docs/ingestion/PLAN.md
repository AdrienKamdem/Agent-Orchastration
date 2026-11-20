# Ingestion Pipeline: Implementation Plan

> Scope: **getting documents from external sources into a searchable store.**
> Retrieval agents, Text2SQL and the daily brief all sit downstream of this and are out of scope here.
> Timeline: weeks 1–3 of the flagship plan.

## Implementation status

The skeleton for steps 0–7 is implemented in `src/copilot/ingestion/` and tested offline
(synthetic fixtures, mocked HTTP, real Postgres). Still to do:

- [ ] **Validate against live sources** on your machine. Feed URLs and real EDGAR layouts
      can differ from the fixtures, so save one real response per source into
      `tests/fixtures/` and add a test for each
- [ ] Check section detection on real 10-K **and** 20-F filings (ASML, TSM, TTE)
- [ ] Schedule it (cron / GitHub Actions)
- [ ] Optional: RSS feeds for Fed FEDS notes / BIS papers that link to PDFs

Differences from the plan below, decided during implementation:
- Plain SQL migrations with a small runner (`db.migrate`) instead of Alembic. One less tool
  to learn now; switch if migrations get complex.
- Token counts use a word+punctuation regex instead of `tiktoken`. That avoids a download
  at runtime, and bge has its own tokenizer anyway. The 400-token default leaves headroom
  under bge's 512.
- A `HashingEmbedder` (no dependencies) for tests and smoke runs, next to the real model.
- `lxml` for EDGAR HTML instead of `selectolax` (already pulled in by trafilatura).

---

## 1. What "done" looks like

At the end of this phase you can run:

```bash
ingest run --source all          # fetch new docs from every source, process them, index them
ingest search "FOMC balance sheet runoff" --k 5
```

and get back relevant chunks, each tracing back to a source document (URL, title, publication date, section/page). That provenance is what lets the synthesis agent cite sources later.

Concretely:

- [ ] 5 source types ingest on a schedule: **SEC EDGAR filings, central bank statements, news RSS, research PDFs, earnings material**
- [ ] Re-running ingestion is **idempotent**: no duplicates, unchanged docs are skipped
- [ ] Every raw file is kept, so you can **re-process** with a new parser, chunker or embedding model without re-downloading (your eval work in weeks 4–6 depends on this)
- [ ] Chunks + embeddings + metadata sit in **Postgres + pgvector**
- [ ] Each run logs what happened (fetched / new / skipped / failed, duration)
- [ ] Parsers and connectors have tests that run offline against saved fixtures

---

## 2. Architecture

```
        ┌──────────────┐
        │  Scheduler   │  cron / APScheduler / GitHub Actions (later: Databricks Job)
        └──────┬───────┘
               ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 1. DISCOVER   connector lists what's available (RSS items, EDGAR      │
│               filing index, files in the PDF inbox)                   │
│ 2. FETCH      download what's new → RAW STORE (immutable bytes)       │
│               record a `documents` row, status = fetched              │
├──────────────────────── boundary: network stops here ─────────────────┤
│ 3. PARSE      raw bytes → clean text + structure (sections, pages)    │
│ 4. CHUNK      text → chunks with offsets (strategy is versioned)      │
│ 5. EMBED      chunks → vectors (model is versioned)                   │
│ 6. INDEX      upsert into Postgres/pgvector                           │
└──────────────────────────────────────────────────────────────────────┘
               ▼
     Postgres: sources, documents, chunks(+embedding), ingestion_runs
     Filesystem / object storage: data/raw/...
```

### The design rules worth understanding

1. **Separate fetching from processing.** Steps 1–2 talk to the internet; steps 3–6 only read the raw store. External sources are slow, rate-limited and flaky, and sometimes delete content. Once you have the bytes, you never need the network again to rebuild your index. This is a "landing zone" or "bronze layer", the same idea as the Databricks medallion architecture (bronze = raw, silver = parsed, gold = chunks + embeddings).
2. **Idempotency through stable identifiers.** Each document gets a natural key (`source + external_id`, e.g. an EDGAR accession number or an RSS `guid`) and a `content_hash` (SHA-256 of the raw bytes). Same key and same hash means skip. Same key with a new hash means a new version.
3. **Incremental fetching.** Don't re-download everything each run. Use a per-source **cursor** (last seen date or ID) plus HTTP conditional requests (`ETag` / `If-None-Match`, `Last-Modified` / `If-Modified-Since`). A `304 Not Modified` costs almost nothing.
4. **Be a polite client.** Rate-limit per host, retry with exponential backoff on 429/5xx, send an honest `User-Agent`. The SEC requires this and will block you otherwise.
5. **Provenance on every chunk.** Store `document_id`, `char_start/char_end`, `page`, `section`. A citation is only as good as this metadata.
6. **Version every processing step.** Store `parser_version`, `chunker` (name + params) and `embedding_model` on each chunk. Comparing chunking strategies later means having several versions side by side.
7. **Status per document, not per run.** `fetched → parsed → chunked → embedded`, or `failed` with an error message. One bad PDF must not kill the run, and `ingest retry --failed` picks up where things stopped.

---

## 3. Sources: what, where, and the catch

> Free and public sources only. **No Bloomberg data, terminals, internal code or internal docs.** Check URLs when you implement; sites move their feeds around.

| Source | How to get it | Format | Catch |
|---|---|---|---|
| **SEC EDGAR** (10-K, 10-Q, 8-K) | `https://www.sec.gov/files/company_tickers.json` maps ticker → CIK. `https://data.sec.gov/submissions/CIK##########.json` (CIK zero-padded to 10 digits) lists recent filings. Documents live under `https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/{primary_doc}` | HTML / inline XBRL | **Max 10 requests/s**, and a `User-Agent` with a name and contact email is **required** (read it from config, never hardcode it). Filings are large: start with 10-K items 1A (Risk Factors) and 7 (MD&A) |
| **Central banks** | Fed: press release RSS (`federalreserve.gov/feeds/press_monetary.xml`). ECB: `ecb.europa.eu/rss/press.html`. BoE: `bankofengland.co.uk/rss/news` | RSS → HTML page | The feed only links to the page; you fetch and extract the statement text. Keep every version, since "what changed vs the last statement" is a diff between two documents |
| **News RSS** | Publisher RSS feeds, configured in `sources.yaml` | RSS / Atom | **Copyright.** Store title, summary, link and date from the feed. Don't scrape full articles behind paywalls. Dedupe on `guid` or a normalized URL |
| **Research PDFs** | (a) a local `data/inbox/` folder you drop files into; (b) public research: Fed FEDS notes, BIS working papers, IMF WPs, often with their own RSS | PDF | Parsing PDFs is the hardest part: multi-column layouts, tables, headers and footers. Keep page numbers |
| **Earnings material** | **Free transcripts are mostly copyrighted**, so don't scrape them. Use the 8-K **Exhibit 99.1** (earnings press release) from EDGAR, and drop transcripts you have legitimate access to into the inbox | HTML / PDF | Start with the 8-K press releases. That's clean and legal |

Suggested order: **Fed RSS first** (small, simple, quick win), then EDGAR (where the real engineering is), then news RSS, then the PDF inbox, then 8-K exhibits.

---

## 4. Tech choices (and why)

| Concern | Choice | Why |
|---|---|---|
| Language / packaging | Python 3.12, `uv`, `ruff`, `mypy`, `pytest` | Fast, modern, standard |
| HTTP | `httpx` + `tenacity` (retries) + a simple token-bucket rate limiter | Timeouts and HTTP/2, testable with `respx` |
| RSS | `feedparser` | Handles malformed feeds and both RSS and Atom |
| HTML → text | `trafilatura` (articles, CB statements), `selectolax`/`BeautifulSoup` (EDGAR) | trafilatura strips boilerplate. EDGAR needs custom section logic |
| PDF → text | `pymupdf` (aka `fitz`) to start. Try `unstructured` or `docling` later as an **eval comparison** | Fast, gives page numbers, no heavy dependencies |
| Chunking | **Write it yourself** (token-based recursive + section-aware) using `tiktoken` for counts | It's about 100 lines, and you'll understand exactly what you're evaluating |
| Embeddings | Interface with two implementations: local `sentence-transformers` (`BAAI/bge-small-en-v1.5`, 384-d) and an API model | Free, runs on CPU. The API version gives you a comparison point for the eval |
| Store | **Postgres 16 + pgvector** (HNSW index), via Docker | One DB for metadata, vectors **and** full-text (`tsvector`) for hybrid search later, and the same DB the Text2SQL agent will query. Qdrant is a fine alternative, but it adds a second system |
| DB access | `SQLAlchemy 2.0` + `alembic` migrations, or `psycopg` + raw SQL | Migrations matter once the schema starts changing |
| Config | `pydantic-settings` (`.env`) + `sources.yaml` | Secrets in env vars, watchlist and feeds in YAML |
| CLI | `typer` | `ingest run / retry / reindex / search / status` |
| Logging | `structlog` (JSON) | Machine-readable, and easy to ship to a tracing tool later |
| Scheduling | Start with **cron or a GitHub Actions schedule** calling the CLI. Add APScheduler or Prefect only if you need it | The simplest thing that works. The CLI stays the single entrypoint |

**On LangChain or DSPy for ingestion:** don't use either here. LangChain has document loaders and splitters, but they hide the parts you want to learn (offsets, versioning, idempotency), and you'll want full control during the eval. Bring in a framework at the agent layer, where it earns its place, and say why in the README. That's the "used deliberately" point from the project plan.

---

## 5. Data model

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE sources (
    id           TEXT PRIMARY KEY,          -- 'edgar', 'fed_rss', 'ecb_rss', 'news:ft', 'pdf_inbox'
    kind         TEXT NOT NULL,             -- 'filing' | 'cb_statement' | 'news' | 'research' | 'earnings'
    config       JSONB NOT NULL DEFAULT '{}',
    cursor       JSONB,                     -- incremental state: last date/ID, ETag, Last-Modified
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE documents (
    id             BIGSERIAL PRIMARY KEY,
    source_id      TEXT NOT NULL REFERENCES sources(id),
    external_id    TEXT NOT NULL,           -- accession no., RSS guid, file hash...
    version        INT  NOT NULL DEFAULT 1,
    url            TEXT,
    title          TEXT,
    published_at   TIMESTAMPTZ,
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_hash   TEXT NOT NULL,           -- sha256 of raw bytes
    mime_type      TEXT,
    raw_path       TEXT NOT NULL,           -- data/raw/...
    status         TEXT NOT NULL,           -- fetched | parsed | chunked | embedded | failed
    error          TEXT,
    parser_version TEXT,
    metadata       JSONB NOT NULL DEFAULT '{}',  -- ticker, cik, form_type, bank, author...
    UNIQUE (source_id, external_id, version)
);

CREATE TABLE chunks (
    id              BIGSERIAL PRIMARY KEY,
    document_id     BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal         INT NOT NULL,
    text            TEXT NOT NULL,
    token_count     INT NOT NULL,
    section         TEXT,                   -- 'Item 1A. Risk Factors'
    page            INT,
    char_start      INT NOT NULL,
    char_end        INT NOT NULL,
    chunker         TEXT NOT NULL,          -- 'recursive_v1:512/64'
    embedding_model TEXT,
    embedding       vector(384),            -- dimension depends on the model
    tsv             tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    UNIQUE (document_id, chunker, ordinal)
);
CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON chunks USING gin (tsv);

CREATE TABLE ingestion_runs (
    id           BIGSERIAL PRIMARY KEY,
    source_id    TEXT,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    stats        JSONB,                     -- {"discovered":40,"new":3,"skipped":37,"failed":0}
    status       TEXT                       -- running | ok | partial | failed
);
```

Notes:
- The parsed text goes in `data/parsed/{doc_id}.json` (text plus section/page spans) rather than in a table. It's large, and rebuilding it from raw is cheap. Chunk offsets point into it.
- Comparing embedding models with different dimensions needs a column per model or a table per model (`chunk_embeddings_{model}`). Start with one model and solve this in week 4.

**Raw store layout:** `data/raw/{source_id}/{yyyy}/{mm}/{sha256}.{ext}`, plus a sidecar `.meta.json` with response headers and fetch time. This is local disk for now. On cloud deployment it becomes S3/GCS, so put it behind a small `BlobStore` interface (`put/get/exists`) from day one.

---

## 6. Code layout

```
src/copilot/ingestion/
    config.py            # pydantic settings + sources.yaml loader
    models.py            # dataclasses: DiscoveredItem, RawDocument, ParsedDocument, Chunk
    http.py              # shared httpx client: retries, rate limit, UA, conditional GET
    blobstore.py         # BlobStore interface + LocalBlobStore
    db.py                # repository functions: upsert_document, set_status, save_chunks...
    connectors/
        base.py          # class Connector(Protocol): discover(cursor) -> items; fetch(item) -> RawDocument
        rss.py           # generic RSS connector (Fed, ECB, BoE, news)
        edgar.py
        pdf_inbox.py
    parsers/
        base.py          # parse(raw) -> ParsedDocument (text + sections + pages)
        html_article.py
        edgar_filing.py
        pdf.py
    chunking/
        recursive.py
        section_aware.py
    embedding/
        base.py          # embed(texts) -> list[vector]; .name, .dim
        local_st.py
    pipeline.py          # orchestrates steps per doc, handles status + errors
    cli.py               # typer app
tests/
    fixtures/            # saved RSS xml, EDGAR json/html, sample PDFs
    unit/ ...
    integration/         # runs against docker-compose Postgres
migrations/              # alembic
docker-compose.yml       # postgres+pgvector
sources.yaml
```

The two interfaces that matter most:

```python
class Connector(Protocol):
    source_id: str

    def discover(self, cursor: dict | None) -> tuple[list[DiscoveredItem], dict]: ...
    def fetch(self, item: DiscoveredItem) -> RawDocument: ...  # bytes + headers + metadata


class Parser(Protocol):
    version: str

    def can_parse(self, raw: RawDocument) -> bool: ...
    def parse(self, raw: RawDocument) -> ParsedDocument: ...
```

Adding a new source then means writing one connector, and maybe a parser. The pipeline doesn't change.

---

## 7. Step-by-step tasks

Each step ends with something you can run.

### Step 0: Scaffolding (½ day)
- `uv init`, `src/` layout, ruff/mypy/pytest config, pre-commit
- `docker-compose.yml` with `pgvector/pgvector:pg16`
- `.env.example` (`DATABASE_URL`, `SEC_USER_AGENT`, `DATA_DIR`)
- GitHub Actions: lint + unit tests on every push
- **Done when:** `docker compose up -d && uv run pytest` passes in CI and locally

### Step 1: Storage foundations (1 day)
- Alembic migration with the schema from §5
- `LocalBlobStore` (content-addressed by sha256)
- `db.py` repository functions plus an integration test
- **Done when:** a test writes a fake document and chunk and reads them back, including a vector similarity query

### Step 2: HTTP client + first connector: Fed RSS (1–2 days)
- `http.py`: timeouts, `tenacity` retries on 429/5xx with backoff and jitter, per-host rate limiter, configurable UA, conditional GET using the stored ETag/Last-Modified
- `connectors/rss.py`: generic over any feed URL. `discover` parses the feed and filters items newer than the cursor. `fetch` downloads the linked HTML page
- Tests with `respx`, using a saved feed XML and HTML page in `tests/fixtures/`
- **Done when:** `ingest run --source fed_rss` puts raw files on disk and `documents` rows in status `fetched`, and a second run reports `new=0`

### Step 3: Parse → chunk → embed → index (2–3 days)
- `parsers/html_article.py` with trafilatura. Keep the title and date
- `chunking/recursive.py`: split on paragraphs, then sentences, then tokens, with a 512-token target, 64 overlap and **char offsets kept**
- `embedding/local_st.py`: batch the input, normalize vectors, skip chunks already embedded with the same model
- `pipeline.py`: moves each document through the statuses and catches per-document exceptions into `status=failed, error=...`
- `ingest search "<query>"`: embed the query, cosine top-k, print chunk + title + URL
- **Done when:** you can search the Fed statements and the results make sense. **This is your first end-to-end slice.** Everything after this is more sources and better quality

### Step 4: ECB / BoE / news RSS (1 day, mostly config)
- Add the feeds to `sources.yaml`. Same connector, different config
- News: store feed summary only (`fetch_full_page: false`), normalize URLs (strip `utm_*`), dedupe on guid/URL
- **Done when:** several feeds ingest in one `ingest run --source all`

### Step 5: SEC EDGAR (3–4 days, the hardest connector)
- Watchlist tickers from `sources.yaml`, mapped to CIKs (cache `company_tickers.json` locally)
- `discover`: the submissions JSON filtered by `form in {10-K, 10-Q, 8-K}` and `filingDate > cursor`
- `fetch`: the primary document, plus for 8-K, Exhibit 99.1 from the filing index
- Rate limiter set to **≤ 10 req/s** for sec.gov hosts. Fail fast if `SEC_USER_AGENT` is unset
- `parsers/edgar_filing.py`: strip iXBRL tags and hidden elements, convert tables to readable text, detect `Item 1A`, `Item 7` and similar with regex over headings, store section spans
- `chunking/section_aware.py`: never cross a section boundary, and put the section name in chunk metadata
- Metadata: `ticker, cik, form_type, period_of_report, accession_no`
- **Done when:** "What are Apple's main risk factors?" returns chunks from Item 1A with section metadata

### Step 6: PDF inbox + public research (2 days)
- `connectors/pdf_inbox.py`: scan `data/inbox/`, `external_id = sha256`, move processed files to `data/inbox/processed/`
- `parsers/pdf.py` (PyMuPDF): per-page text, strip repeated headers/footers (lines that repeat across pages), fix hyphenation across line breaks
- Optional: an RSS connector for FEDS notes or BIS papers, which link to PDFs, reusing the same parser
- **Done when:** dropping a PDF in the inbox makes it searchable, with page numbers in the results

### Step 7: Scheduling, observability, operations (1–2 days)
- `ingestion_runs` rows with stats per source. `ingest status` prints the last runs and failures
- `ingest retry --failed` and `ingest reindex --chunker X --embedding Y` (rebuild from raw)
- `structlog` JSON logs with `run_id`, `source_id`, `document_id` bound into the context
- Schedule: cron (`0 6 * * 1-5 ingest run --source all`) or a GitHub Actions workflow on a schedule
- **Done when:** it runs unattended each morning before your brief, and you can see from `ingest status` what happened

**Total: about 2.5–3 weeks part-time**, which fits the weeks 1–3 slot.

---

## 8. Testing strategy

- **Unit tests for parsers** using real saved files in `tests/fixtures/` (one Fed statement, one 10-K excerpt, one 8-K exhibit, one multi-column PDF). These catch most regressions
- **Connector tests** with `respx`: mock HTTP and assert on cursor handling, conditional GET, 429 retry, and 304 skip
- **Chunker property tests:** offsets slice back to the exact chunk text, there are no empty chunks, token counts stay ≤ the max, and the section boundary is respected
- **Idempotency test:** run the pipeline twice on the same fixtures and assert the row counts don't change
- **Integration test** in CI with a Postgres service container
- No live network in CI. Live smoke tests are opt-in only (`pytest -m live`)

---

## 9. Where this links to the rest of the project

- **Eval (weeks 4–6):** because raw is kept and steps are versioned, `ingest reindex --chunker section_aware_v1 --embedding bge-base` gives you a second index to compare against the labelled question set. Design for this now
- **Databricks:** the bronze/silver/gold split maps directly onto Delta tables. Later you can run steps 3–5 as a Spark job over the raw store, with the same code inside `mapInPandas`
- **Cloud:** `BlobStore` becomes S3/GCS, Postgres becomes RDS or Cloud SQL, and the cron job becomes a scheduled container job
- **Citations:** `chunks.document_id + char_start/char_end + page + url` is exactly what the synthesis agent needs

---

## 10. Compliance checklist (it's a personal project, keep it clean)

- [ ] Only public, free sources. No Bloomberg data or code, nothing from work machines
- [ ] Respect each site's ToS and `robots.txt`. Respect SEC fair-access limits
- [ ] Don't redistribute copyrighted content: news is stored as headline, summary and link. Keep the raw store out of git (`data/` in `.gitignore`)
- [ ] Secrets and your contact UA only in `.env`, never committed
- [ ] Check your employer's outside-activity policy before publishing the repo

---

## 11. Concepts to learn along the way

- HTTP caching semantics (ETag, Last-Modified, 304), backoff with jitter, token buckets
- Idempotent pipelines and natural keys versus surrogate keys
- Medallion architecture (bronze/silver/gold)
- How tokenizers count tokens, and why chunk size interacts with the embedding model's max sequence length (bge-small: 512 tokens)
- ANN indexes: HNSW versus IVFFlat in pgvector, and the recall/latency trade-off
- Inline XBRL and the EDGAR filing structure (filing index → primary doc + exhibits)
