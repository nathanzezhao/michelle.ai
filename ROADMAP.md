# Michelle.ai — Project Roadmap

Reference doc for what's done, what's next, and what's planned later.

**Build order:** Memory + ACTION v1 are in. **Voice UI (three modes, blob default)** is in via Vite/React renderer ([SPEC-VOICE-UI.md](SPEC-VOICE-UI.md)). **Chat voice backend** (`POST /chat/voice`) is wired. **Track 2 remainder (Nathan 2026-09-30):** chat-bar mic → clarifying chips → inbox briefing (LLM intent, not a chip) → evaluator miss → one diagnostic follow-up → more actions. Park SSE, escalation, and auth/hardening. Then screen capture / vision. Do not call inbox briefing “scraping.”

**Screen capture source doc:** `/Users/nathan/Downloads/desktop_ai_agent_roadmap_screencapture.pdf`

**Slice 1 spec:** [SPEC-PIPELINE.md](SPEC-PIPELINE.md) (ACTION engine).
**Next-slice spec:** [SPEC-CHAT-VOICE.md](SPEC-CHAT-VOICE.md) (main-chat microphone). Do not put specs in `docs/` — that folder is Michelle's RAG KB.

---

## Current status (August 2026)

### Shipped

- Electron floating UI (collapse/expand, drag, scramble text, thinking animation)
- FastAPI backend + Ollama / Gemini / mock LLM providers
- **Memory v1:** SQLite (`michelle.db`), conversation IDs, `localStorage` persistence
- Last 10 messages sent to LLM per turn (`MAX_HISTORY`, tunable via `.env`)
- **Long-term fact memory:** second assessor + `long_term_facts` table keyed by `user_id`
- Guardrails: blank messages, 4000-char cap, invalid UUID rejected, failed turns not saved
- **Intent router:** CHAT / RETRIEVE / REMEMBER / **ACTION** (live). `INTENT_MODE=llm` uses the same model as `LLM_PROVIDER`
- **RETRIEVE v1:** local `docs/` folder → SQLite FTS5 → grounded answer (`retrieve.py`)
- **ACTION v1:** whitelist only — `open_app` (native `open -a`) and `send_email` (Composio Gmail)
- **Email composer** in the window: to / subject / body, urgent, attach, generate body; files + generate pinned above Send; Confirm / Cancel before send
- **Composio Platform** Gmail: real sends as the connected inbox (`COMPOSIO_API_KEY` project key `ak_…`, user `default`). For You `ck_…` keys are rejected
- `actions_log` audit + no replay after backend restart
- R0/R1 pytest suites (`tests/`)
- Shorter replies via `SYSTEM_PROMPT` in `llm.py`

### Voice UI + chat voice (in progress)

- [x] **Three-mode shell** — chat (original window, default) + voice stub + collapsed ([SPEC-VOICE-UI.md](SPEC-VOICE-UI.md))
- [x] **AI Blob voice mode** — tap record, glow, process via `/chat/voice`
- [x] **Saved chatlog API** — `GET /session/history` (window still starts empty)
- [x] **`POST /chat/voice`** — Whisper → normal `/chat` ([SPEC-CHAT-VOICE.md](SPEC-CHAT-VOICE.md))
- [x] **React Bits free orb** — `Orb` WebGL blob + `reactbits` MCP (no Pro license)
- [x] **Full composer port** — email tap/composer in React chat mode

### How a turn works today

```
main.py → memory.py (last N turns + long-term facts)
       → intent.py (CHAT | RETRIEVE | REMEMBER | ACTION)
       → ACTION: analyze params (+ session_context pad) → actions_log → native or Composio
       → REMEMBER: existing store/recall
       → RETRIEVE: docs/ FTS
       → CHAT: reply + synchronous memory assessor
       → save turn / facts / action row
```

**Two memory layers:**

| Layer | What | Scope | Limit |
|-------|------|--------|--------|
| Regular | Chat turns in `messages` | Per `conversation_id` | Last `MAX_HISTORY` (default 10) sent to LLM |
| Long-term | Stable facts in `long_term_facts` | Per `user_id` (survives new chats) | Always injected into system prompt |

- **DB:** stores every message in the thread + upserted user facts + `actions_log`
- **LLM:** sees last 10 messages + all long-term facts each turn
- **Refresh:** same conversation + same user continue via `localStorage`. The window starts empty (greeting only). Chatlog stays in `messages`; long-term facts stay in `long_term_facts` (invisible in the UI). The session_context working pad clears on `b` refresh and on `/session/start`.

---

## Track 1: Finish memory

- [x] **Saved chatlog API** — `GET /session/history` exists; React chat mode does not dump old bubbles on open
- [x] **Long-term fact memory** — second assessor decides importance; durable facts (name, location, etc.) stored per user and injected into the prompt beyond the 10-message window
- [x] **Document `MAX_HISTORY`** — default 10; set in `.env`; described in README
- [x] **Update README** — docs/retrieve, reset, ACTION, Composio, composer UI

---

## Track 2: Post-memory plan (before screen capture)

From the original Michelle architecture (intent router → RAG → agents):

- [x] **Chat-bar microphone** — see [SPEC-CHAT-VOICE.md](SPEC-CHAT-VOICE.md). Tap/tap. `[input] [mic] [Send]`. Hide mic while email composer is open. Header Voice blob stays. Transcript fills the input; user hits Send (does **not** auto-run `/chat`). Email composer tap stays isolated.
- [x] **Intent includes ACTION** — fourth live label; `INTENT_MODE=llm` uses Ollama/Gemini (rules fallback)
- [ ] **Intent clarifying questions** — if `confidence < 0.6` and top two labels are close: chips (Look up docs / Remember / Do this / Just chat). Do not run ACTION/RETRIEVE until tap. Rules/mock never ask. `force_intent` on `/chat` re-runs the same utterance. Close = shelf-score gap ≤ 0.15.
- [x] **RETRIEVE v1** — local `docs/` ingest + SQLite FTS5 + grounded answers (sample KB included)
- [x] **RETRIEVE v2 (baseline)** — query translator + optional Ollama embeddings hybrid behind `retrieve.search()` (`RETRIEVE_V2=1`)
- [x] **ACTION v1** — `open_app` + `send_email` (Composio), Confirm/Cancel, composer UI, `actions_log`
- [ ] **Inbox briefing (after clarifying)** — consented Gmail **read**. Trigger is **LLM intent** (same classifier/ACTION analyzer as typing or spoken-then-Send), not a greeting chip and not auto on launch. Cap: last 24h, max 15, one page. See below.
- [ ] **More actions** — quit apps, calendar, Slack, etc. Still whitelist + risk tiers in code, never LLM-judged. `quit_app` / `close_app` already exist in the whitelist — do not rebuild them as “new.” Inbox briefing is the first **new** extra ACTION.

### Inbox briefing (Track 2 ACTION — not started)

**User outcome:** After Gmail is connected, the user can get a short briefing of recent inbox mail (from / subject + a few bullets). Copy: “inbox briefing” / “catch me up.” Never “scraping.”

**Team call (2026-09-28):** Sam option 2 — put it on Track 2 now, **build after chat-bar mic**. Tom: Ned (whitelist + Composio list) + Kit (greeting UI) + Oz (read scopes). Ray: **block silent auto-read on every `/session/start`.**

**Why a new ACTION:** Composio Gmail today is send + drafts only (`GMAIL_SEND_EMAIL`, draft CRUD, `GMAIL_LIST_DRAFTS`). Drafts are outbound. Inbox list/read is a new whitelist type (e.g. `brief_inbox` / `summarize_inbox`), new tool, likely new Gmail readonly scopes (reconnect). `ComposioExecutor.execute` currently runs `send_email` only.

**MVP (done-when):**
- **LLM intent** — `classify_intent` / ACTION analyzer maps meaning to the briefing ACTION. No greeting chip. No auto-fetch on launch. Chat-bar does not auto-Send; they must press Send after dictation.
- **Cap:** last 24h, max 15, one page. From + subject + one-liner each, then a short summary. Truncate; no MIME dump, no attachments.
- `/session/start` still greets / asks name; window still starts empty of old bubbles. Briefing must not replace the name ask.
- Honest fail if Gmail isn’t connected (`composio_not_connected` + Connect Link), empty inbox, or list error — never a fake briefing.
- `actions_log` every fetch. Memory assessor must **not** write inbox into `long_term_facts`. Do not store raw bodies in the DB.
- Confirm/Cancel stays for **send**. Summary must not send, open composer, or confirm a send.

**Out of scope (10-star / later):** thread graphs, auto-replies, labels/search, calendar/Slack, full bodies in UI, reply-from-briefing, every-launch auto-fetch, Gmail HTML / DOM scrape, Track 3 vision.

**Ask Nathan (locked 2026-09-30):**
- **Trigger:** LLM intent — `classify_intent` / ACTION analyzer maps meaning to `brief_inbox` (or the whitelist name we ship). No greeting chip. No silent `/session/start` auto-read. Spoken catch-up works only after the user Sends the transcript (chat-bar does not auto-submit).
- **Cap:** last 24 hours, max 15 messages, one page. From + subject + one-liner, then 3–5 bullets.
- **(A)/(B)/(C) chips:** not used. (B) auto-brief remains parked.

**Also locked (same day):**
- Mic: tap/tap; `[input] [mic] [Send]`; keep header blob; hide chat-row mic when composer is open.
- Transcript: fill the input and wait for Send (overrides SPEC §4 bubble-then-auto-`/chat` for the **chat bar only**; Voice blob still uses `POST /chat/voice`).
- Chat/blob junk: empty + too-short **audio** + “thank you” hallucinations. Email tap keeps the 4-word / 12-char gate.
- Clarifying: chips at confidence below 0.6 when top two are close.
- SSE and production auth/rate-limits: **parked** this track. Keep scramble.
- Whisper: same `base` / `int8` as email tap. No cloud STT.

- [ ] **Evaluator loop** — Don't hallucinate when retrieval fails; structured "not found" behavior
- [ ] **Diagnostic agent** — Identify knowledge gaps, ask targeted follow-ups
- [ ] **Escalation agent** — Human handoff when Michelle can't answer
- [ ] **SSE / streaming** — Replace fixed delay with streamed tokens (keep scramble animation)
- [ ] **Production hardening** — Auth, rate limits, audit log, output guardrails

---

## Track 3: Screen capture & vision (after Track 1 + 2)

**Do not start until leftover memory UI and RETRIEVE v2 are in good shape.** ACTION v1 / Composio Gmail does not unblock this track.

Reference: `desktop_ai_agent_roadmap_screencapture.pdf` (July 2026)

**Vision:** Cross-platform, local-first assistant that floats on the desktop, observes screen context (pixels **or** DOM text when the target is a page), respects strict privacy guardrails, and can eventually run background tasks via tool integration.

- [ ] **DOM / text access script shell** — read `textContent` / `innerText` / `innerHTML` on a selected element when the page is reachable; pixels + OCR/VLM remain the fallback. Read-only. Not wired.

### Phase 1 — Screen capture & OS integration (The Eyes)

- **Framework note (PDF):** Tauri recommended over Electron for native webview + Rust backend; efficient low-level capture hooks per OS
- **Current stack:** Electron today — migration to Tauri is a future decision, not required to prototype capture concepts
- **Capture strategy:** Event-driven or low-Hz polling (1–2 fps) to limit CPU/GPU; downsample frames before inference
- **OS APIs:** Windows Graphics Capture, macOS ScreenCaptureKit, Linux equivalent
- **When to skip pixels:** If the target is a browser or webview Michelle can reach, prefer Phase 1b (DOM text) over OCR on a screenshot

**Edge cases (from PDF):**

- Multi-monitor: follow cursor to capture the correct display
- DRM / protected content: black frame → fail gracefully, don't hallucinate
- Transient UI: tooltips vanish on focus loss → "capture snapshot" shortcut before agent window takes focus

### Phase 1b — DOM / text access (The Script)

Pixel capture is the general eye. For a page Michelle can already see as a document, **read the DOM first** — cheaper, faster, and more accurate than OCR. Fall back to Phase 1 frames when the UI is native, canvas/WebGL, or the origin is not reachable.

**Script shell (read-only prototype — not wired, not an ACTION):**

```javascript
// Selecting the element
const element = document.getElementById("myElement");

// 1. Get or set the text content (Fastest, gets hidden text too)
let text = element.textContent;

// 2. Get or set the visible text (Respects CSS styling like display: none)
let visibleText = element.innerText;

// 3. Get or set the full HTML structure inside the element
let htmlContent = element.innerHTML;
```

**Which field:**

| Field | Use when |
|-------|----------|
| `textContent` | Fast dump of all text nodes, including `display: none` and off-screen copy |
| `innerText` | What a sighted user would see (layout + CSS) |
| `innerHTML` | Structure / semantics, not the visible string |

**Guardrails (same brakes as Phase 3):**

- Shell is **read-only** until a later whitelist. Do not set `innerHTML` from model output (XSS). If a write ever ships, prefer `textContent`.
- `textContent` can leak hidden fields (passwords, visually hidden PII) — mask before the VLM/LLM sees it.
- Same contextual denylists as capture (banking, password managers, incognito).
- Electron later: inject via a consented `webContents.executeJavaScript`; never scrape arbitrary origins without user permission.
- Does **not** click, type, or hijack mouse/keyboard.

### Phase 2 — Local AI processing (The Brain)

- **Local VLM:** Ollama or llama.cpp; multimodal models (e.g. LLaVA or smaller quantized VLMs)
- **OCR pre-pass:** Tesseract (or similar) before VLM — hard text + downsampled image improves small UI text accuracy. Skip when Phase 1b already returned `innerText` / `textContent`
- **Privacy:** All vision processing local by default

### Phase 3 — Guardrails (The Brakes)

- **Local privacy masking:** OpenCV blur password fields, credit cards, SSN-like patterns before VLM sees the frame
- **Contextual denylists:** Read active window title; pause capture ("go blind") on banking apps, password managers, incognito windows
- **UI indicator:** Clear visual state when capturing vs resting (e.g. glowing ring or eye icon)

### Phase 4 — UI & floating overlay (The Face)

- Frameless, transparent, always-on-top (already partially done in Electron)
- Must show capture-on vs capture-off state explicitly

### Phase 5 — Background task execution (The Hands)

- **Tool integration:** Composio. Gmail send is live in ACTION v1 (`actions.py` + Platform project key).
- **Still later:** more apps (Slack, Notion, calendar), same confirm-before-write pattern. Inbox briefing is Track 2 (Gmail **read**), not this phase.
- **Flow:** LLM decides action → whitelist + executor → Confirm if high-risk → reports back in the floating window
- Does **not** hijack mouse/keyboard

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
| ACTION | `open_app` now; `send_email` composer + Confirm → Composio Gmail. Inbox briefing (read) is planned, not live. |

---

## Files map

| File | Role |
|------|------|
| `memory.py` | SQLite regular chat history (last-N window) |
| `long_term_memory.py` | Durable per-user facts table |
| `retrieve.py` | Index/search `docs/` via SQLite FTS5 |
| `docs/` | Sample + user knowledge base (`.md` / `.txt`) |
| `scripts/reset_michelle.sh` | Wipe local DB so another person starts clean |
| `intent.py` | Intent router + memory assessor + action analyzer |
| `actions.py` | ACTION whitelist, `actions_log`, native + Composio executors |
| `llm.py` | Chat replies + grounded RETRIEVE answers |
| `main.py` | `/chat`, `/session/start`, `/action/confirm`, `/action/draft_body` |
| `index.html` | Electron UI (chat, composer, Confirm/Cancel) |
| `main.js` | Window collapse/expand, drag |
| `tests/` | R0/R1 pytest (httpx); `COMPOSIO_API_KEY` unset in suite |
| `SPEC-PIPELINE.md` | Slice 1 ACTION spec (not a RAG doc) |
| `SPEC-CHAT-VOICE.md` | Next: main-chat mic → Whisper → `/chat` (not email tap) |
| `michelle.db` | Local chat archive + facts + doc index + actions (gitignored) |

## Quick reference: memory assessor

Uses the same `INTENT_MODE` as the intent router (`llm` / `rules` / `mock`).

LLM returns `priority` (`high` / `medium` / `low`) + `confidence`.
Only writes when `important`, `priority` meets `MEMORY_MIN_PRIORITY` (default `high`),
and `confidence >= MEMORY_SAVE_THRESHOLD` (default `0.85`).

On Electron launch (`POST /session/start`): if no `name` fact yet, Michelle asks once;
after they answer it stays in long-term memory forever. Backend restart is not a new session.

Examples that should save: "My name is Nathan", "I live in Seattle".
Examples that should not: "hey", "what's the weather", temporary mood, medium/low prefs.
