# Michelle — project memory

Durable notes for agents working on this repo. Update when design decisions change.

## Architecture decisions

- **Intent router is one LLM call** — CHAT | RETRIEVE | REMEMBER | ACTION. No sequential engine waterfall (see SPEC-PIPELINE.md).
- **`docs/` is RAG only** — never store specs, runbooks, or agent instructions there.
- **Email composer is isolated** — Send posts opaque fields; tap uses `/action/draft_body` (grammar only). Neither uses `/chat`.
- **Session context pad** — `session_context.py` supplies follow-up context ("close it", resume draft) on ACTION turns.

## Known pitfalls

- Electron does **not** hot-reload `index.html` / `main.js` — restart `f` after UI changes.
- Backend `b` hot-reloads Python; DB modules read env at import — tests must set env before `import main` (see `tests/conftest.py`).
- `load_dotenv()` does not override already-set env vars — conftest wins over repo `.env` in pytest.
- Composio needs **Platform** project key (`ak_...`), not For You consumer key (`ck_...`).

## Test conventions

- CI and local gate: `LLM_PROVIDER=mock`, `INTENT_MODE=rules`.
- Temp DB + docs dir per session via conftest — never hit production `michelle.db` in tests.

## Next slice

Chat-bar microphone → Whisper → normal `/chat` turn ([SPEC-CHAT-VOICE.md](../SPEC-CHAT-VOICE.md)). Do not reuse email tap path.
