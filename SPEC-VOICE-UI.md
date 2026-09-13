# SPEC-VOICE-UI — Voice as an addition on the existing chat window

**Status:** Chat is the original window. Voice is an opt-in stub. Phase 2 wires blob → `/chat/voice`.
**Note:** Repo root only — not in `docs/` (RAG KB).

---

## 1. Goal

Voice is **an extra mode**, not a rework of the chat window. Collapse, bubbles, Send, and expand stay as they were.

| Mode | Default? | Purpose |
|------|----------|---------|
| **Chat** | Yes (launch) | Original window — bubbles, typed input, confirm/cancel |
| **Voice** | No (stub) | AI Blob — tap to listen; reach via audio-lines icon |
| **Collapsed** | No | Small draggable orb, click-through |

Chat ↔ voice via header icons: **messages-circle** (open chat) and **audio-lines** (open listening).

---

## 2. Mode diagram

```
launch → chat (original window)
chat ↔ voice (header icons)
voice → recording (blob glow) → processing → stay voice OR open chat (if reply)
voice|chat → collapsed (header square)
collapsed → previous mode (click orb)
```

---

## 3. Voice mode — blob

- **Size:** diameter = **50% of voice panel width** (`ResizeObserver` on container)
- **Component:** React Bits free **`Orb`** (`@react-bits/Orb-TS-TW`) via `renderer/src/components/Orb/Orb.tsx`, wrapped by `AiBlob`
- **Idle:** subtle animation
- **Recording:** intensified glow; `mic-start` IPC
- **Processing:** distinct pulse; `mic-stop` → `/chat/voice` (Phase 2)
- **Interaction:** tap to start, tap to stop (same as composer tap pattern)
- **Junk/silence:** stay in voice; show "Didn't catch that" — no chat bubble

---

## 4. Chat mode

- Port of legacy [index.html](index.html) chat: bubbles, Send, Confirm/Cancel
- Window starts empty (greeting only). `GET /session/history` is the saved chatlog API; it is not painted on reload. Long-term facts stay in the DB and in the LLM prompt, never as old bubbles.
- **audio-lines** icon returns to voice stub

---

## 5. Collapsed mode

- Port of legacy collapse: click-through, drag orb, expand to **the mode you left** (chat by default)

---

## 6. Renderer architecture

- Vite + React under `renderer/`
- Electron [main.js](main.js) loads `renderer/dist/index.html` (prod) or `ELECTRON_DEV=1` → `http://127.0.0.1:5173`
- Legacy [index.html](index.html) kept for reference; not loaded by Electron

---

## 7. Phase 2 (backend)

See [SPEC-CHAT-VOICE.md](SPEC-CHAT-VOICE.md): blob stop → Whisper → `handle_chat` via `POST /chat/voice` → optional transition to chat with bubbles.

---

## 8. React Bits setup (free)

1. Registry: `@react-bits` in [renderer/components.json](renderer/components.json)
2. Voice orb: `Orb-TS-TW` (dependency: `ogl`) — `ai-blob-tw` is Pro-only
3. MCP: `reactbits` in `.cursor/mcp.json` — optional `GITHUB_TOKEN` for GitHub API limits
4. Wrapper: [renderer/src/components/AiBlob/AiBlob.tsx](renderer/src/components/AiBlob/AiBlob.tsx) maps idle/recording/processing to orb props
