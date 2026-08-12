# Michelle.ai — Project Roadmap

Reference doc for what's done, what's next, and what's planned later.

**Build order:** Finish memory → finish post-memory plan → screen capture / vision (see PDF reference below).

**Screen capture source doc:** `/Users/nathan/Downloads/desktop_ai_agent_roadmap_screencapture.pdf`

---

## Current status (July 2026)

### Shipped

- Electron floating UI (collapse/expand, drag, scramble text, thinking animation)
- FastAPI backend + Ollama / Gemini / mock LLM providers
- **Memory v1:** SQLite (`michelle.db`), conversation IDs, `localStorage` persistence
- Last 10 messages sent to LLM per turn (`MAX_HISTORY`, tunable via `.env`)
- **Long-term fact memory:** second assessor + `long_term_facts` table keyed by `user_id`
- Guardrails: blank messages, 4000-char cap, invalid UUID rejected, failed turns not saved
- **Intent router v1:** CHAT / RETRIEVE / REMEMBER (ACTION parked — next)
- **RETRIEVE v1:** local `docs/` folder → SQLite FTS5 → grounded answer (`retrieve.py`)
- Shorter replies via `SYSTEM_PROMPT` in `llm.py`

### How memory works today

```
main.py → memory.py (regular: last 10 turns + long-term facts for user)
       → intent.py (route: CHAT / RETRIEVE / REMEMBER)
       → intent.py (memory assessor: is this worth long-term storage?)
       → retrieve.py (if RETRIEVE: search docs/ via FTS)
       → llm.py (CHAT or grounded RETRIEVE reply)
       → memory.py / long_term_memory.py (save turn + facts)
```

**Two memory layers:**

| Layer | What | Scope | Limit |
|-------|------|--------|--------|
| Regular | Chat turns in `messages` | Per `conversation_id` | Last `MAX_HISTORY` (default 10) sent to LLM |
| Long-term | Stable facts in `long_term_facts` | Per `user_id` (survives new chats) | Always injected into system prompt |

- **DB:** stores every message in the thread + upserted user facts
- **LLM:** sees last 10 messages + all long-term facts each turn
- **Refresh:** same conversation + same user continue via `localStorage`, but chat bubbles don't reload in the UI yet

---

## Track 1: Finish memory (do this first)

- [ ] **Reload chat bubbles on open** — fetch history from DB so the UI matches backend after refresh
- [x] **Long-term fact memory** — second assessor decides importance; durable facts (name, location, etc.) stored per user and injected into the prompt beyond the 10-message window
- [ ] **Tune / document `MAX_HISTORY`** — decide default and document in README + `.env`
- [x] **Update README** — docs/retrieve + reset instructions; Ollama memory note fixed

---

## Track 2: Post-memory plan (do before screen capture)

From the original Michelle architecture (intent router → RAG → agents):

- [ ] **Intent polish** — Ollama-based intent classification (not just rules); use confidence for clarifying questions
- [x] **RETRIEVE v1** — local `docs/` ingest + SQLite FTS5 + grounded answers (sample KB included)
- [ ] **RETRIEVE v2** — query translator + vector/embeddings (optional pgvector later); same `retrieve.search()` API
- [ ] **ACTION for real** — Tool/API calls (tickets, email, etc.) with confirmation before writes
- [ ] **Evaluator loop** — Don't hallucinate when retrieval fails; structured "not found" behavior
- [ ] **Diagnostic agent** — Identify knowledge gaps, ask targeted follow-ups
- [ ] **Escalation agent** — Human handoff when Michelle can't answer
- [ ] **SSE / streaming** — Replace fixed delay with streamed tokens (keep scramble animation)
- [ ] **Production hardening** — Auth, rate limits, audit log, output guardrails

---

## Track 3: Screen capture & vision (after Track 1 + 2)

**Do not start until memory and post-memory items above are in good shape.**

Reference: `desktop_ai_agent_roadmap_screencapture.pdf` (July 2026)

**Vision:** Cross-platform, local-first assistant that floats on the desktop, observes screen context, respects strict privacy guardrails, and can eventually run background tasks via tool integration.

### Phase 1 — Screen capture & OS integration (The Eyes)

- **Framework note (PDF):** Tauri recommended over Electron for native webview + Rust backend; efficient low-level capture hooks per OS
- **Current stack:** Electron today — migration to Tauri is a future decision, not required to prototype capture concepts
- **Capture strategy:** Event-driven or low-Hz polling (1–2 fps) to limit CPU/GPU; downsample frames before inference
- **OS APIs:** Windows Graphics Capture, macOS ScreenCaptureKit, Linux equivalent

**Edge cases (from PDF):**

- Multi-monitor: follow cursor to capture the correct display
- DRM / protected content: black frame → fail gracefully, don't hallucinate
- Transient UI: tooltips vanish on focus loss → "capture snapshot" shortcut before agent window takes focus

### Phase 2 — Local AI processing (The Brain)

- **Local VLM:** Ollama or llama.cpp; multimodal models (e.g. LLaVA or smaller quantized VLMs)
- **OCR pre-pass:** Tesseract (or similar) before VLM — hard text + downsampled image improves small UI text accuracy
- **Privacy:** All vision processing local by default

### Phase 3 — Guardrails (The Brakes)

- **Local privacy masking:** OpenCV blur password fields, credit cards, SSN-like patterns before VLM sees the frame
- **Contextual denylists:** Read active window title; pause capture ("go blind") on banking apps, password managers, incognito windows
- **UI indicator:** Clear visual state when capturing vs resting (e.g. glowing ring or eye icon)

### Phase 4 — UI & floating overlay (The Face)

- Frameless, transparent, always-on-top (already partially done in Electron)
- Must show capture-on vs capture-off state explicitly

### Phase 5 — Background task execution (The Hands)

- **Tool integration (PDF suggests Composio):** Standardized APIs for Gmail, Slack, Notion, etc.
- **Flow:** LLM decides action → function call to tool API → runs in background without hijacking mouse/keyboard → reports back to floating agent
- Ties into Track 2 ACTION agent work

---

## Quick reference: intent modes

| `INTENT_MODE` | Behavior |
|---------------|----------|
| `llm` | Uses `LLM_PROVIDER` model (Ollama or Gemini); falls back to rules on failure |
| `rules` | Keyword matching only |
| `mock` | Simplest keywords; terminal testing only |

| Intent | Today |
|--------|-------|
| CHAT | Ollama/Gemini/mock reply |
| RETRIEVE | Search `docs/` (FTS5) + grounded answer |
| REMEMBER | Store or recall long-term facts |
| ACTION | Parked — next (tools + confirm). Not classified. |

---

## Files map

| File | Role |
|------|------|
| `memory.py` | SQLite regular chat history (last-N window) |
| `long_term_memory.py` | Durable per-user facts table |
| `retrieve.py` | Index/search `docs/` via SQLite FTS5 |
| `docs/` | Sample + user knowledge base (`.md` / `.txt`) |
| `scripts/reset_michelle.sh` | Wipe local DB so another person starts clean |
| `intent.py` | Intent router + memory-worthiness assessor (priority gate) |
| `llm.py` | Chat replies + grounded RETRIEVE answers |
| `main.py` | `/chat`, `/session/start`, routing, memory write |
| `index.html` | Electron UI (`conversation_id` + `user_id` in localStorage) |
| `main.js` | Window collapse/expand, drag |
| `michelle.db` | Local chat archive + facts + doc index (gitignored) |

## Quick reference: memory assessor

Uses the same `INTENT_MODE` as the intent router (`llm` / `rules` / `mock`).

LLM returns `priority` (`high` / `medium` / `low`) + `confidence`.
Only writes when `important`, `priority` meets `MEMORY_MIN_PRIORITY` (default `high`),
and `confidence >= MEMORY_SAVE_THRESHOLD` (default `0.85`).

On Electron launch (`POST /session/start`): if no `name` fact yet, Michelle asks once;
after they answer it stays in long-term memory forever. Backend restart is not a new session.

Examples that should save: "My name is Nathan", "I live in Seattle".
Examples that should not: "hey", "what's the weather", temporary mood, medium/low prefs.
