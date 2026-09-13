"""RETRIEVE v2 helpers — query translation and hybrid merge."""

import retrieve


def test_translate_query_normalizes_whats():
    out = retrieve._translate_query("what's the refund policy?")
    assert "what is" in out


def test_merge_hybrid_combines_sources():
    fts = [{"source": "a.md", "content": "alpha", "score": 0.1}]
    vec = [{"source": "b.md", "content": "beta", "score": -0.2}]
    merged = retrieve._merge_hybrid(fts, vec, limit=2)
    sources = {hit["source"] for hit in merged}
    assert sources == {"a.md", "b.md"}
