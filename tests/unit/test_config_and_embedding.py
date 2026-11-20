from pathlib import Path

import numpy as np
import pytest

from copilot.ingestion.config import EdgarSourceConfig, SourcesConfig
from copilot.ingestion.embedding import HashingEmbedder, make_embedder

REPO = Path(__file__).resolve().parents[2]


def test_repo_sources_yaml_is_valid():
    cfg = SourcesConfig.load(REPO / "sources.yaml")
    tickers = [w.ticker for w in cfg.watchlist]
    assert tickers == ["NVDA", "MRVL", "ASML", "TSM", "AVGO", "IONQ", "QBTS", "TTE"]
    edgar = cfg.get("edgar")
    assert isinstance(edgar, EdgarSourceConfig)
    assert {"20-F", "6-K"} <= set(edgar.forms)  # foreign issuers on the watchlist


def test_duplicate_source_ids_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        SourcesConfig.model_validate(
            {"sources": [{"id": "a", "type": "pdf_inbox"}, {"id": "a", "type": "pdf_inbox"}]}
        )


def test_hashing_embedder_is_normalised_and_lexical():
    e = HashingEmbedder()
    docs = e.embed_documents(["federal funds rate decision", "semiconductor export controls"])
    assert docs.shape == (2, 384)
    assert np.allclose(np.linalg.norm(docs, axis=1), 1.0)
    q = e.embed_query("rate decision")
    assert q @ docs[0] > q @ docs[1]
    assert np.array_equal(e.embed_query("x y"), e.embed_query("x y"))  # deterministic


def test_make_embedder_hashing():
    assert make_embedder("hashing", "ignored").dim == 384
