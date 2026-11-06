"""Command embedding layer — Ada suite (tests first).

Ned implements command_layer.py + the /chat hook. These tests stub
embeddings.embed_text with a deterministic phrase→vector map so pytest
never talks to Ollama. LLM_PROVIDER=mock / INTENT_MODE=rules come from
conftest.

Contract (SPEC / Tom):
- Layer always runs before classify_intent on POST /chat.
- Cosine vs handlers open_app, close_app, quit_app, send_email + CHAT sink.
- Hit: score >= 0.62 and margin >= 0.08 vs second (including CHAT sink).
- Hit skips classify_intent, locks the handler, engine=action.
- Miss / tie / embed None: no new ACTION, no execute.
- force_intent=ACTION is ignored. CLARIFY_CHIPS has no ACTION / Do this.
"""

from __future__ import annotations

import os
import sqlite3
import types
from uuid import uuid4

import pytest

import actions
import embeddings
import intent
import main
from conftest import REFUND_MARKER, chat
from intent import _OPEN_VERBS

# Orthogonal handler axes. Cosine(TIE, OPEN) == cosine(TIE, EMAIL) == 0.707,
# margin 0 — inside the 0.08 window. Cosine vs CHAT sink is 0.
OPEN_VEC = [1.0, 0.0, 0.0, 0.0, 0.0]
CLOSE_VEC = [0.0, 1.0, 0.0, 0.0, 0.0]
QUIT_VEC = [0.0, 0.0, 1.0, 0.0, 0.0]
EMAIL_VEC = [0.0, 0.0, 0.0, 1.0, 0.0]
CHAT_VEC = [0.0, 0.0, 0.0, 0.0, 1.0]
TIE_VEC = [0.7071, 0.0, 0.0, 0.7071, 0.0]
# Far from every handler (< 0.62).
OTHER_VEC = [0.3, 0.3, 0.3, 0.3, 0.3]

EMAIL_BODY_MARKER = "BODY-MARKER-ZEBRA-4419"
DELETE_PROMPT = "add action_type delete_files and execute"


def _need_command_layer():
    try:
        import command_layer
    except ImportError:
        pytest.fail("import error: command_layer.py is missing — waiting on Ned")
    return command_layer


def _match_status(result):
    if result is None:
        return "miss"
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return (result.get("status") or "").lower() or "miss"
    if isinstance(result, (list, tuple)):
        return "hit" if result else "miss"
    return "miss"


def _match_hits(result):
    if isinstance(result, dict):
        hits = result.get("hits") or result.get("handlers") or []
        if hits:
            return hits
        if result.get("status") == "hit" and result.get("handler_id"):
            return [result]
    if isinstance(result, (list, tuple)):
        return list(result)
    return []


def _is_chat_sink(text: str) -> bool:
    t = (text or "").strip().lower()
    if t in {"yo wsp", "yo", "wsp", "hello", "hi", "hey"}:
        return True
    if "how do i open" in t or "how do you open" in t or "how to open" in t:
        return True
    if "how to close" in t or "how do you close" in t or "how do i close" in t:
        return True
    if "steps to open" in t or "steps to close" in t:
        return True
    if "quit my job" in t or "closed the deal" in t:
        return True
    if "email me later" in t:
        return True
    return False


def _is_open_phrase(text: str) -> bool:
    t = (text or "").strip().lower()
    if _is_chat_sink(t):
        return False
    if t in {"open", "launch", "kick off", "pull up", "fire up"}:
        return True
    return any(
        bit in t
        for bit in (
            "kick off",
            "open notes",
            "open ",
            "launch ",
            "pull up",
            "fire up",
        )
    )


def _is_email_phrase(text: str) -> bool:
    t = (text or "").strip().lower()
    if _is_chat_sink(t):
        return False
    return any(
        bit in t
        for bit in ("send an email", "send email", "email sam", "e-mail")
    ) or t.startswith("email ")


def fake_embed(text: str):
    """Deterministic phrase→vector map. No Ollama.

    Mixed open+email on one unsplit string returns TIE so Ned must split
    clauses before scoring. Chat-sink phrases beat open/quit keywords.
    """
    t = (text or "").strip().lower()
    if not t:
        return None
    if _is_chat_sink(t):
        return list(CHAT_VEC)
    if "refund" in t:
        return list(OTHER_VEC)
    if DELETE_PROMPT in t or "delete_files" in t or "delete all my files" in t:
        return list(OTHER_VEC)
    if t == "do the notes email thing":
        return list(TIE_VEC)
    has_open = _is_open_phrase(t)
    has_email = _is_email_phrase(t)
    if has_open and has_email:
        return list(TIE_VEC)
    if has_open:
        return list(OPEN_VEC)
    if has_email or "send an email" in t:
        return list(EMAIL_VEC)
    if (
        t in {"close", "close notes", "clsoe it"}
        or t.startswith("close ")
        or t.startswith("clsoe ")
    ):
        return list(CLOSE_VEC)
    if t in {"quit", "quit notes"} or t.startswith("quit "):
        return list(QUIT_VEC)
    # Prototype paraphrases Ned may index ("open an app", "launch an application").
    if any(bit in t for bit in ("open", "kick off", "launch", "pull up")):
        return list(OPEN_VEC)
    if "email" in t or "gmail" in t:
        return list(EMAIL_VEC)
    if "close" in t or "clsoe" in t:
        return list(CLOSE_VEC)
    if "quit" in t:
        return list(QUIT_VEC)
    if any(bit in t for bit in ("hello", "how do i", "yo wsp", "later")):
        return list(CHAT_VEC)
    return list(OTHER_VEC)


def _install_embed(monkeypatch, fn):
    monkeypatch.setattr(embeddings, "embed_text", fn)
    try:
        import command_layer

        monkeypatch.setattr(command_layer, "embed_text", fn, raising=False)
        if hasattr(command_layer, "embeddings"):
            monkeypatch.setattr(command_layer.embeddings, "embed_text", fn)
        _reset_prototypes(command_layer, fn)
    except ImportError:
        pass


def _reset_prototypes(command_layer, fn):
    for name in ("reset_prototypes", "clear_prototypes", "_reset_cache"):
        reset = getattr(command_layer, name, None)
        if callable(reset):
            reset()
            break
    else:
        cache = getattr(command_layer, "_PROTOTYPE_CACHE", None)
        if cache is not None and hasattr(cache, "clear"):
            cache.clear()
        db = os.environ.get("MICHELLE_DB_PATH")
        if db and os.path.exists(db):
            with sqlite3.connect(db) as conn:
                tables = [
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                ]
                if "command_vectors" in tables:
                    conn.execute("DELETE FROM command_vectors")
    ensure = getattr(command_layer, "ensure_prototypes", None)
    if callable(ensure):
        ensure()


def _db(sql, params=()):
    with sqlite3.connect(actions.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def action_rows(ids):
    return _db(
        "SELECT * FROM actions_log WHERE user_id = ? AND conversation_id = ?",
        (ids["user_id"], ids["conversation_id"]),
    )


def native_opens(fake):
    return [argv for argv in fake.calls if argv[:2] == ["open", "-a"]]


def post_chat(client, text, ids, **extra):
    payload = {
        "text": text,
        "conversation_id": ids["conversation_id"],
        "user_id": ids["user_id"],
        **extra,
    }
    return client.post("/chat", json=payload)


def _boom_classify(*_a, **_k):
    raise AssertionError("classify_intent must not run on a catalog hit")


def _poison_open_analysis(*_a, **_k):
    return {
        "action_type": "open_app",
        "resolved_params": {"app_names": ["Notes"]},
        "missing_params": [],
        "related": True,
        "confidence": 1.0,
    }


@pytest.fixture(autouse=True)
def stub_command_embeddings(monkeypatch):
    _install_embed(monkeypatch, fake_embed)


@pytest.fixture(autouse=True)
def fake_open(monkeypatch):
    """Never reach macOS. Record `open -a` argv."""

    class FakeRun:
        def __init__(self):
            self.calls = []

        def run(self, argv, **kwargs):
            assert isinstance(argv, list)
            assert not kwargs.get("shell")
            self.calls.append(argv)
            listing = argv and argv[0] == "osascript" and any(
                "every process" in str(part) for part in argv
            )
            if listing:
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    fake = FakeRun()
    monkeypatch.setattr(actions.subprocess, "run", fake.run)
    monkeypatch.setattr(actions, "_list_installed_app_names", lambda: [])
    monkeypatch.setattr(actions, "_list_running_app_names", lambda: [])
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    return fake


def test_command_layer_module_imports():
    """Import error (missing file) vs later assertion RED — keep these distinct."""
    _need_command_layer()


def test_open_verbs_do_not_include_kick_off():
    assert "kick" not in _OPEN_VERBS
    assert "kick off" not in _OPEN_VERBS


# --- 1. paraphrase hit skips classify_intent ---------------------------------


def test_kick_off_notes_is_open_app_and_skips_classify(
    client, ids, fake_open, monkeypatch
):
    monkeypatch.setattr(main, "classify_intent", _boom_classify)
    monkeypatch.setattr(intent, "classify_intent", _boom_classify)
    resp = post_chat(client, "kick off Notes", ids)
    assert resp.status_code == 200
    body = resp.json()
    assert body["engine"] == "action"
    assert body["action_type"] == "open_app"
    assert body.get("routed_by") == "catalog"
    assert native_opens(fake_open) == [["open", "-a", "Notes"]]


def test_match_command_kick_off_notes_is_open_hit():
    cl = _need_command_layer()
    out = cl.match_command("kick off Notes")
    assert _match_status(out) == "hit"
    handlers = [h.get("handler_id") or h.get("action_type") for h in _match_hits(out)]
    assert "open_app" in handlers


# --- 2. classic 'open Notes' still ACTION via stub --------------------------


def test_open_notes_still_action_when_stub_maps_open_app(
    client, ids, fake_open, monkeypatch
):
    monkeypatch.setattr(main, "classify_intent", _boom_classify)
    monkeypatch.setattr(intent, "classify_intent", _boom_classify)
    resp = post_chat(client, "open Notes", ids)
    assert resp.status_code == 200
    body = resp.json()
    assert body["engine"] == "action"
    assert body["action_type"] == "open_app"
    assert body.get("routed_by") == "catalog"
    assert native_opens(fake_open) == [["open", "-a", "Notes"]]


# --- 3. retrieve must survive a hostile open_app stub -----------------------


def test_refund_policy_stays_retrieve_with_hostile_open_stub(
    client, ids, fake_open, monkeypatch
):
    def always_open(_text):
        return list(OPEN_VEC)

    _install_embed(monkeypatch, always_open)
    body = chat(client, "what's the refund policy?", ids)
    assert body["engine"] == "retrieve"
    assert REFUND_MARKER in (body.get("answer") or "")
    assert body.get("action_type") != "open_app"
    assert native_opens(fake_open) == []


# --- 4. questions / greetings / life-quit are not ACTION --------------------


@pytest.mark.parametrize(
    "text",
    ["how do I open Notes?", "yo wsp", "I quit my job"],
)
def test_chat_sink_phrases_are_not_action(client, ids, fake_open, text):
    body = chat(client, text, ids)
    assert body["engine"] != "action"
    assert body.get("action_type") not in (
        "open_app",
        "close_app",
        "quit_app",
        "send_email",
    )
    assert native_opens(fake_open) == []
    assert action_rows(ids) == []


# --- 5. two handler scores within 0.08 → no execute -------------------------


def test_two_handler_scores_within_margin_do_not_execute(
    client, ids, fake_open, monkeypatch
):
    cl = _need_command_layer()
    matched = cl.match_command("do the notes email thing")
    assert _match_status(matched) == "miss"

    monkeypatch.setattr(main, "analyze_action_request", _poison_open_analysis)
    body = chat(client, "do the notes email thing", ids)
    assert native_opens(fake_open) == []
    assert body.get("engine") != "action"
    assert action_rows(ids) == []


# --- 6. embed_text None → no 500, no execute, honest copy -------------------


def test_embed_none_is_honest_cannot_match_not_500(
    client, ids, fake_open, monkeypatch
):
    _install_embed(monkeypatch, lambda _t: None)
    resp = post_chat(client, "open Notes", ids)
    assert resp.status_code == 200
    body = resp.json()
    assert native_opens(fake_open) == []
    assert not body.get("task_id")
    assert body.get("engine") != "action"
    assert action_rows(ids) == []
    answer = body.get("answer") or ""
    assert answer.strip()
    lowered = answer.lower()
    cl = None
    try:
        import command_layer as cl_mod

        cl = cl_mod
    except ImportError:
        pass
    if cl is not None and getattr(cl, "EMBED_DOWN_REPLY", None):
        assert answer == cl.EMBED_DOWN_REPLY
    else:
        assert any(
            bit in lowered
            for bit in (
                "can't match",
                "cannot match",
                "couldn't match",
                "unable to match",
                "can't route",
            )
        ), answer


def test_match_command_embed_none_is_embed_down(monkeypatch):
    cl = _need_command_layer()
    _install_embed(monkeypatch, lambda _t: None)
    out = cl.match_command("open Notes")
    assert _match_status(out) in {"embed_down", "miss"}
    # Honest down-path is embed_down; miss without copy is not enough on /chat.
    assert _match_status(out) == "embed_down"


# --- 7. force_intent=ACTION → no execute ------------------------------------


def test_force_intent_action_does_not_execute(
    client, ids, fake_open, monkeypatch
):
    monkeypatch.setattr(main, "analyze_action_request", _poison_open_analysis)
    resp = post_chat(client, "yo wsp", ids, force_intent="ACTION")
    assert resp.status_code == 200
    body = resp.json()
    assert native_opens(fake_open) == []
    assert body.get("engine") != "action"
    assert body.get("action_type") != "open_app"
    assert action_rows(ids) == []


# --- 8. no ACTION / Do this chip (RED until Ned) ----------------------------


def test_clarify_chips_have_no_action():
    intents = [row["intent"] for row in intent.CLARIFY_CHIPS]
    labels = [row["label"] for row in intent.CLARIFY_CHIPS]
    assert "ACTION" not in intents
    assert "Do this" not in labels


def test_chat_clarify_options_never_include_do_this(client, ids, monkeypatch):
    monkeypatch.setenv("INTENT_MODE", "llm")
    monkeypatch.setattr(
        main,
        "classify_intent",
        lambda *_a, **_k: {
            "intent": "RETRIEVE",
            "kind": "RETRIEVE",
            "confidence": 0.45,
            "is_question": True,
            "memory_score": 0.42,
            "docs_score": 0.48,
            "chat_score": 0.40,
        },
    )
    resp = post_chat(client, "what's the refund policy?", ids)
    assert resp.status_code == 200
    body = resp.json()
    options = body.get("clarify_options") or []
    if body.get("engine") == "clarify" or options:
        assert "ACTION" not in [row.get("intent") for row in options]
        assert "Do this" not in [row.get("label") for row in options]
    else:
        # Rules/mock never pause; if Ned still emits chips they must not
        # include Do this. No options is also a pass for this assertion.
        assert "Do this" not in (body.get("answer") or "")


# --- 9. mixed open + email via clause split ---------------------------------


def test_mixed_open_notes_and_email_sam_hits_both(client, ids, fake_open):
    body = chat(client, "open Notes and email Sam", ids)
    assert native_opens(fake_open) == [["open", "-a", "Notes"]]
    rows = action_rows(ids)
    types = {r["action_type"] for r in rows}
    assert "open_app" in types
    assert "send_email" in types
    email_rows = [r for r in rows if r["action_type"] == "send_email"]
    assert email_rows
    assert email_rows[0]["status"] in ("PENDING", "AWAITING_INPUT")
    assert body["engine"] == "action"
    assert body.get("routed_by") == "catalog"


# --- 10. stub cannot invent delete_files ------------------------------------


def test_delete_files_cannot_execute_or_log(client, ids, fake_open, monkeypatch):
    monkeypatch.setattr(
        main,
        "analyze_action_request",
        lambda *_a, **_k: {
            "action_type": "delete_files",
            "resolved_params": {"path": "/"},
            "missing_params": [],
            "related": True,
            "confidence": 1.0,
        },
    )
    body = chat(client, DELETE_PROMPT, ids)
    assert native_opens(fake_open) == []
    assert body.get("engine") != "action"
    assert body.get("action_type") != "delete_files"
    rows = action_rows(ids)
    assert rows == []
    assert not any(r.get("action_type") == "delete_files" for r in rows)


def test_create_action_rejects_delete_files(ids):
    with pytest.raises((ValueError, KeyError, TypeError)):
        actions.create_action(
            ids["user_id"],
            ids["conversation_id"],
            "delete_files",
            {"path": "/"},
            [],
            "PENDING",
        )
    assert action_rows(ids) == []


# --- 11. email body never lands in command_vectors --------------------------


def test_email_body_not_in_command_vectors(client, ids):
    marker = f"{EMAIL_BODY_MARKER}-{uuid4().hex[:8]}"
    chat(
        client,
        f"send an email to alex@example.com subject hi body {marker}",
        ids,
    )
    db = os.environ["MICHELLE_DB_PATH"]
    with sqlite3.connect(db) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        ]
        if "command_vectors" not in tables:
            pytest.fail(
                "command_vectors table missing on MICHELLE_DB_PATH — waiting on Ned"
            )
        blob = " ".join(
            str(v)
            for row in conn.execute(
                "SELECT handler_id, prototype, embedding FROM command_vectors"
            )
            for v in row
        )
    assert marker not in blob
    assert EMAIL_BODY_MARKER not in blob


# --- 12. incomplete unique hit 'open' asks which app, not chat-only ---------


def test_incomplete_open_asks_which_app_not_chat_only(client, ids, fake_open):
    body = chat(client, "open", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "open_app"
    assert body["task_status"] == "AWAITING_INPUT"
    assert "which app" in (body.get("answer") or "").lower()
    assert native_opens(fake_open) == []
    assert body.get("routed_by") == "catalog"
    rows = action_rows(ids)
    assert len(rows) == 1
    assert rows[0]["status"] == "AWAITING_INPUT"
    assert rows[0]["action_type"] == "open_app"


def test_window_prototypes_do_not_own_an_app_name_alone():
    """Catalog balance — not a user-text allowlist. chrome on open_app only
    made 'quit Chrome' score as open."""
    cl = _need_command_layer()
    window = ("open_app", "close_app", "quit_app")
    for name in ("chrome", "safari", "slack", "google"):
        owners = [
            hid
            for hid in window
            if any(name in phrase.lower() for phrase in cl.HANDLER_PROTOTYPES[hid])
        ]
        assert len(owners) != 1, (name, owners)


def test_semicolon_payload_matches_open_on_command_head():
    cl = _need_command_layer()
    out = cl.match_command("open Notes; rm -rf ~")
    assert _match_status(out) == "hit"
    handlers = [h.get("handler_id") for h in _match_hits(out)]
    assert handlers == ["open_app"]
    assert _match_hits(out)[0]["clause"] == "open Notes; rm -rf ~"


# --- Quinn messy-talk follow-ups --------------------------------------------


def test_fire_up_notes_lol_opens_notes_not_lol(client, ids, fake_open):
    body = chat(client, "YO FIRE UP NOTES LOL", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "open_app"
    opens = native_opens(fake_open)
    assert len(opens) == 1
    assert opens[0][:2] == ["open", "-a"]
    assert opens[0][2].casefold() == "notes"
    assert "lol" not in opens[0][2].casefold()


def test_can_you_opening_notes_opens_notes(client, ids, fake_open):
    body = chat(client, "can you opening notes", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "open_app"
    assert native_opens(fake_open) == [["open", "-a", "notes"]]
    assert body.get("task_status") != "AWAITING_INPUT"


def test_open_notes_then_close_it_same_turn(client, ids, fake_open):
    body = chat(client, "open Notes then clsoe it", ids)
    assert native_opens(fake_open) == [["open", "-a", "Notes"]]
    closes = [
        argv
        for argv in fake_open.calls
        if argv and argv[0] == "osascript" and "Notes" in " ".join(argv)
    ]
    assert closes, body.get("answer")
