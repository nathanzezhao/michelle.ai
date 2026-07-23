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
- Guardrails: blank messages, 4000-char cap, invalid UUID rejected, failed turns not saved
- **Intent router v1:** CHAT / RETRIEVE / ACTION (RETRIEVE + ACTION are stub replies for now)
- Shorter replies via `SYSTEM_PROMPT` in `llm.py`

### How memory works today

```
main.py → memory.py (load last 10 from DB)
       → intent.py (classify message)
       → llm.py (generate reply for CHAT)
       → memory.py (save user + assistant turn)
```

- **DB:** stores every message in the thread
- **LLM:** only sees the last 10 messages each turn
- **Refresh:** same conversation continues via `localStorage`, but chat bubbles don't reload in the UI

---

## Track 1: Finish memory (do this first)

- [ ] **Reload chat bubbles on open** — fetch history from DB so the UI matches backend after refresh
- [ ] **Long-term fact memory** — extract stable facts (e.g. user's name) and always inject into prompt so they survive beyond the 10-message window
- [ ] **Tune / document `MAX_HISTORY`** — decide default and document in README + `.env`
- [ ] **Update README** — fix outdated "Ollama has no memory" note; point to this roadmap

---

## Track 2: Post-memory plan (do before screen capture)

From the original Michelle architecture (intent router → RAG → agents):

- [ ] **Intent polish** — Ollama-based intent classification (not just rules); use confidence for clarifying questions
- [ ] **RETRIEVE for real** — Query translator + vector search (pgvector / doc ingestion)
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
| `llm` | Gemini classifies (default); falls back to rules on failure |
| `rules` | Keyword matching; good for Ollama-only setups |
| `mock` | Simplest keywords; terminal testing only |

| Intent | Today |
|--------|-------|
| CHAT | Ollama/Gemini reply |
| RETRIEVE | Stub — search not built |
| ACTION | Stub — tools not built |

---

## Files map

| File | Role |
|------|------|
| `memory.py` | SQLite read/write |
| `intent.py` | Intent classification |
| `llm.py` | Chat replies + `SYSTEM_PROMPT` |
| `main.py` | `/chat` endpoint, routing |
| `index.html` | Electron UI |
| `main.js` | Window collapse/expand, drag |
| `michelle.db` | Local chat archive (gitignored) |
