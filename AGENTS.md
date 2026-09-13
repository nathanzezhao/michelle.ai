# Michelle.ai — Agent Instructions

Desktop AI assistant: Electron floating UI + FastAPI backend. Not a plain LLM wrapper — every turn routes through intent, memory, retrieve, or whitelisted actions.

## Stack

| Layer | Tech |
|-------|------|
| UI | Electron + Vite/React (`renderer/`, `main.js`) — chat (default), voice stub, collapsed |
| API | FastAPI (`main.py`) |
| LLM | Ollama (default), Gemini, or mock via `LLM_PROVIDER` |
| DB | SQLite (`michelle.db`, gitignored) |
| RAG | Local `docs/` → SQLite FTS5 (`retrieve.py`) |
| Actions | Whitelist only: `open_app`, `send_email` (`actions.py`) |

## Run locally

```bash
# Backend (hot-reloads)
b

# Electron window (restart after index.html / main.js changes)
f
```

Requires `.env` for provider keys. See README for Composio Gmail setup.

## Turn pipeline

```
POST /chat
  → load history + long-term facts (memory.py, long_term_memory.py)
  → classify_intent → CHAT | RETRIEVE | REMEMBER | ACTION
  → engine for that intent
  → save turn (+ facts / action audit row)
```

Composer **Send** and email **tap** (Whisper) are sidecars — they do **not** go through `/chat` or the classifier.

## Critical rules

1. **Do not put specs or runbooks in `docs/`** — that folder is Michelle's RAG knowledge base only. Specs live at repo root (`SPEC-*.md`, `ROADMAP.md`).
2. **ACTION is whitelist-only** — never let the LLM execute arbitrary tools. Confirm/Cancel for high-risk sends.
3. **Immutable patterns** — prefer new objects over in-place mutation in Python.
4. **Tests** — run `pytest` with `LLM_PROVIDER=mock` and `INTENT_MODE=rules` (conftest sets this). Do not require live Ollama/Gemini/Composio in CI.
5. **Secrets** — never commit `.env`, API keys, or `michelle.db`. See `SECURITY.md`.

## Key files

| File | Role |
|------|------|
| `main.py` | `/chat`, `/session/start`, `/action/confirm`, `/action/draft_body` |
| `intent.py` | Intent router, memory assessor, action analyzer |
| `llm.py` | Chat + grounded RETRIEVE replies |
| `actions.py` | Whitelist, `actions_log`, Composio Gmail |
| `session_context.py` | Working pad for follow-ups ("close it"). Cleared on `b` refresh and `/session/start`; chatlog + facts stay. |
| `tests/` | R0/R1 pytest (httpx) |

## Michelle team agents (Cursor)

Project-specific roles under `.cursor/agents/`: Tom (eng lead), Ned (backend), Kit (Electron UI), Oz (devops), Ray (QA lead), Quinn/Ada/Vale (QA). ECC agents are prefixed `ecc-*`.

## Roadmap

Voice UI: three modes, blob default ([SPEC-VOICE-UI.md](SPEC-VOICE-UI.md)). Voice → `POST /chat/voice` → `/chat` ([SPEC-CHAT-VOICE.md](SPEC-CHAT-VOICE.md)). See [ROADMAP.md](ROADMAP.md).

## Reset personal data

```bash
./scripts/reset_michelle.sh
```

Then clear Electron `localStorage` keys `michelle_user_id` and `michelle_conversation_id`.
