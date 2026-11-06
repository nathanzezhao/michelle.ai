"""Pre-intent command embedding layer.

Cosine-match the utterance to coded handlers before classify_intent.
A unique handler hit is the only way a new ACTION starts. Prototypes are
paraphrases, not a user-text allowlist. Never persist email bodies.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

import embeddings
from embeddings import cosine

DB_PATH = Path(os.getenv("MICHELLE_DB_PATH", "michelle.db"))

MIN_SCORE = 0.62
MARGIN = 0.08
CHAT_SINK_ID = "__chat__"

EMBED_DOWN_REPLY = (
    "I can't match commands right now — embeddings are down. Please try again in a moment."
)

# Skill cards + paraphrases. Not a filter of what the user may type.
# Window handlers share the same objects (the app / notes / it) so an app
# name cannot pull every utterance toward open_app.
_WINDOW_OBJECTS = ("the app", "notes", "it")

def _window_phrases(verbs: tuple[str, ...]) -> tuple[str, ...]:
    cards = []
    for verb in verbs:
        cards.append(verb)
        for obj in _WINDOW_OBJECTS:
            cards.append(f"{verb} {obj}")
    return tuple(cards)


HANDLER_PROTOTYPES = {
    "open_app": (
        "open or launch an application",
        "start an app running",
        *_window_phrases(
            ("open", "opening", "launch", "kick off", "pull up", "fire up", "bring up", "hop into")
        ),
        "can you open the app",
        "can you open notes",
        "can you opening notes",
        "could you open notes",
        "please open notes",
        "please open the app",
        "would you open it",
    ),
    "close_app": (
        "close the windows of an application without quitting",
        "hide the app windows",
        *_window_phrases(("close", "shut")),
    ),
    "quit_app": (
        "quit or force quit an application so it exits",
        "exit an app completely",
        *_window_phrases(("quit", "force quit", "exit")),
    ),
    "send_email": (
        "send an email message",
        "compose an email to someone",
        "send an email",
        "send email",
        "email",
        "email someone",
        "send an email to",
        "e-mail",
    ),
}

# Instructional / life-event paraphrases. Short "how do I open Notes?" and
# "can you tell me how to open notes" sit inside the polite-open cluster
# (nomic ~0.93). Short "I quit my job" sits next to "quit Chrome".
CHAT_SINK_PROTOTYPES = (
    "hello",
    "yo wsp",
    "how do you open an app?",
    "what are the steps to open notes",
    "explain how to open notes",
    "teach me how to open the notes app",
    "explain how to close notes",
    "what are the steps to close notes",
    "I quit my job last week",
    "I left my job",
    "I closed the deal",
    "email me later",
    "what's the refund policy?",
)

_FILLER_RE = re.compile(r"\b(?:um|uh|lol|lmao)\b", re.IGNORECASE)
_LEADING_HEY_RE = re.compile(r"^hey[\s,]+", re.IGNORECASE)
# English coordinators only. Do not split on ";" — that is a shell metachar,
# not "and" (open Notes; rm -rf ~ must stay one argv).
_SPLIT_RE = re.compile(
    r"\s*(?:,|\band also\b|\band then\b|\balso\b|\bthen\b|\band\b)\s*",
    re.IGNORECASE,
)
_BARE_VERB_RE = re.compile(
    r"^(?:close|shut|quit|exit|open|launch|kick off|pull up|fire up|"
    r"bring up|hop into)$",
    re.IGNORECASE,
)
_VERB_LEAD_RE = re.compile(
    r"^(?:close|shut|quit|exit|open|launch|kick off|pull up|fire up|"
    r"bring up|hop into)\s+",
    re.IGNORECASE,
)

_prototypes_ready = False


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS command_vectors (
                handler_id TEXT NOT NULL,
                prototype TEXT NOT NULL,
                embedding TEXT
            )
            """
        )


def reset_prototypes() -> None:
    """Drop cached vectors so the next match re-embeds with the current stub."""
    global _prototypes_ready
    _prototypes_ready = False
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS command_vectors (
                handler_id TEXT NOT NULL,
                prototype TEXT NOT NULL,
                embedding TEXT
            )
            """
        )
        conn.execute("DELETE FROM command_vectors")


def _handler_ids():
    import actions

    return set(actions.ACTION_HANDLERS)


def normalize(text: str) -> str:
    blob = (text or "").strip().lower()
    blob = _LEADING_HEY_RE.sub("", blob)
    blob = _FILLER_RE.sub(" ", blob)
    blob = re.sub(r"\s+", " ", blob).strip(" \t,;.!?").strip()
    return blob


def split_clauses(text: str) -> list[str]:
    stripped = (text or "").strip()
    if not stripped:
        return []
    parts = [p.strip(" \t,;.") for p in _SPLIT_RE.split(stripped)]
    return _inherit_bare_objects([p for p in parts if p])


def _clause_object(part: str) -> str:
    blob = (part or "").strip()
    if not blob or _BARE_VERB_RE.match(blob):
        return ""
    leftover = _VERB_LEAD_RE.sub("", blob).strip(" \t,;.")
    return leftover


def _inherit_bare_objects(parts: list[str]) -> list[str]:
    """close and quit notes → close notes + quit notes. Cosine is the matcher."""
    inherited = ""
    out = list(parts)
    for i in range(len(out) - 1, -1, -1):
        obj = _clause_object(out[i])
        if obj:
            inherited = obj
        elif inherited and _BARE_VERB_RE.match(out[i]):
            out[i] = f"{out[i]} {inherited}"
    return out


def ensure_prototypes() -> None:
    """Index handler + CHAT-sink paraphrases. Lazy — never at import."""
    global _prototypes_ready
    if _prototypes_ready:
        return
    init_db()
    rows: list[tuple[str, str, str]] = []
    for handler_id, phrases in HANDLER_PROTOTYPES.items():
        for phrase in phrases:
            vec = embeddings.embed_text(normalize(phrase) or phrase)
            if not vec:
                continue
            rows.append((handler_id, phrase, json.dumps(vec)))
    for phrase in CHAT_SINK_PROTOTYPES:
        vec = embeddings.embed_text(normalize(phrase) or phrase)
        if not vec:
            continue
        rows.append((CHAT_SINK_ID, phrase, json.dumps(vec)))
    with _connect() as conn:
        conn.execute("DELETE FROM command_vectors")
        conn.executemany(
            "INSERT INTO command_vectors (handler_id, prototype, embedding) "
            "VALUES (?, ?, ?)",
            rows,
        )
    _prototypes_ready = True


def _load_vectors() -> list[tuple[str, str, list[float]]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT handler_id, prototype, embedding FROM command_vectors"
        ).fetchall()
    out = []
    for row in rows:
        try:
            vec = json.loads(row["embedding"] or "[]")
        except json.JSONDecodeError:
            continue
        if isinstance(vec, list) and vec:
            out.append((row["handler_id"], row["prototype"], [float(x) for x in vec]))
    return out


def _best_by_handler(vec: list[float], catalog: list[tuple[str, str, list[float]]]):
    best: dict[str, float] = {}
    for handler_id, _prototype, proto_vec in catalog:
        score = cosine(vec, proto_vec)
        prev = best.get(handler_id)
        if prev is None or score > prev:
            best[handler_id] = score
    return best


def _unique_handler_hit(vec: list[float], catalog: list[tuple[str, str, list[float]]]):
    """Return (handler_id, score) or None. CHAT sink is never executable."""
    known = _handler_ids()
    ranked = sorted(
        _best_by_handler(vec, catalog).items(),
        key=lambda item: item[1],
        reverse=True,
    )
    if not ranked:
        return None
    top_id, top_score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    if top_id not in known:
        return None
    if top_score < MIN_SCORE:
        return None
    if top_score - second < MARGIN:
        return None
    return top_id, top_score


def _skill_match_texts(clause: str) -> list[str]:
    """Handler match may use the command head; params still see the full clause.

    ';' and '|' are payload, not extra skills. Do not execute a second action.
    """
    texts = [clause]
    head = re.split(r"\s*[;|]\s*", (clause or "").strip(), maxsplit=1)[0].strip()
    if head and head.casefold() != (clause or "").strip().casefold():
        texts.append(head)
    return texts


def _score_clause(clause: str, catalog: list[tuple[str, str, list[float]]]):
    saw_vec = False
    best = None
    for blob in _skill_match_texts(clause):
        vec = embeddings.embed_text(normalize(blob) or blob)
        if vec is None:
            continue
        saw_vec = True
        hit = _unique_handler_hit(vec, catalog)
        if hit is None:
            continue
        handler_id, score = hit
        if best is None or score > best["score"]:
            best = {"handler_id": handler_id, "clause": clause, "score": score}
    if best:
        return "hit", best
    if not saw_vec:
        return "embed_down", None
    return "miss", None


def _join_clauses(left: str, right: str) -> str:
    return f"{left} and {right}".strip()


def match_command(text: str) -> dict:
    """Score the utterance against handler prototypes + CHAT sink.

    Returns:
        {"status": "hit"|"miss"|"embed_down", "hits": [...]}
        hits are {handler_id, clause, score} for executable handlers only.
    """
    ensure_prototypes()
    stripped = (text or "").strip()
    if not stripped:
        return {"status": "miss", "hits": []}

    catalog = _load_vectors()
    probe = embeddings.embed_text(normalize(stripped) or stripped)
    if probe is None:
        return {"status": "embed_down", "hits": []}
    if not catalog:
        return {"status": "miss", "hits": []}

    parts = split_clauses(stripped) or [stripped]
    scored = []
    for part in parts:
        status, hit = _score_clause(part, catalog)
        if status == "embed_down":
            return {"status": "embed_down", "hits": []}
        scored.append((part, status, hit))

    merged: list[dict] = []
    pending_miss = ""
    for part, status, hit in scored:
        if status == "hit" and hit:
            if pending_miss:
                combined = _join_clauses(pending_miss, hit["clause"])
                comb_status, comb_hit = _score_clause(combined, catalog)
                if comb_status == "embed_down":
                    return {"status": "embed_down", "hits": []}
                if comb_status == "hit" and comb_hit:
                    merged.append(comb_hit)
                else:
                    merged.append(hit)
                pending_miss = ""
            else:
                merged.append(hit)
            continue
        if merged:
            combined = _join_clauses(merged[-1]["clause"], part)
            comb_status, comb_hit = _score_clause(combined, catalog)
            if comb_status == "embed_down":
                return {"status": "embed_down", "hits": []}
            if comb_status == "hit" and comb_hit:
                merged[-1] = comb_hit
            continue
        pending_miss = _join_clauses(pending_miss, part) if pending_miss else part

    if not merged:
        status, hit = _score_clause(stripped, catalog)
        if status == "embed_down":
            return {"status": "embed_down", "hits": []}
        if status == "hit" and hit:
            return {"status": "hit", "hits": [hit]}
        return {"status": "miss", "hits": []}

    return {"status": "hit", "hits": merged}
