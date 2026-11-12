"""Live MiniLM score-gate. Skips unless LIVE_EMBED=1.

conftest autouse-stubs embeddings.embed_text. Call _embed_minilm directly
so this never false-greens on the 5-d stub.
"""

from __future__ import annotations

import os

import pytest

import embeddings

pytestmark = pytest.mark.skipif(
    os.getenv("LIVE_EMBED") != "1",
    reason="set LIVE_EMBED=1 to load MiniLM (downloads weights once)",
)


def test_minilm_empty_is_none():
    assert embeddings._embed_minilm("") is None


def test_minilm_dim_is_384():
    vec = embeddings._embed_minilm("quit notes")
    assert vec is not None
    assert len(vec) == 384


def test_minilm_quit_job_is_farther_than_quit_app():
    quit_notes = embeddings._embed_minilm("quit notes")
    quit_app = embeddings._embed_minilm("quit the app")
    quit_job = embeddings._embed_minilm("I quit my job")
    how_open = embeddings._embed_minilm("how do I open notes")
    open_notes = embeddings._embed_minilm("open notes")
    assert quit_notes and quit_app and quit_job and how_open and open_notes
    assert embeddings.cosine(quit_notes, quit_app) > embeddings.cosine(
        quit_notes, quit_job
    )


def test_minilm_catalog_gold_pairs(monkeypatch):
    import command_layer

    monkeypatch.setattr(embeddings, "embed_text", embeddings._embed_minilm)
    monkeypatch.setattr(command_layer.embeddings, "embed_text", embeddings._embed_minilm)
    command_layer.reset_prototypes()
    quit_hit = command_layer.match_command("quit Notes")
    assert quit_hit.get("status") == "hit"
    assert [h["handler_id"] for h in quit_hit.get("hits") or []] == ["quit_app"]
    job = command_layer.match_command("I quit my job")
    assert job.get("status") == "miss"
    typo_job = command_layer.match_command("I quitm y job")
    assert typo_job.get("status") == "miss"
    how = command_layer.match_command("how do I open notes")
    assert how.get("status") == "miss"
    mixed = command_layer.match_command("quit notes and calendar")
    assert mixed.get("status") == "hit"
    assert [h["handler_id"] for h in mixed.get("hits") or []] == ["quit_app"]
