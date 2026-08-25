Uses Electron to create a desktop window hosting an AI agent.

Project roadmap and checklist: [ROADMAP.md](ROADMAP.md)

## LLMs:

- Ollama
  - Local backend AI **with** short-term chat memory + long-term facts + doc retrieve
  - Start backend by running `b` in terminal
  - Should be default AI in use; if not, set `LLM_PROVIDER=ollama` in `.env` and run `ollama pull llama3.2`
  - Refresh backend and Electron by pressing control + c, then restart
- Mock
  - Very basic local mock AI for simple responses (also answers RETRIEVE from doc snippets)
  - Set `LLM_PROVIDER=mock` to use
- Gemini
  - Paid with tokens
  - Set `LLM_PROVIDER=gemini` in `.env`

## Memory

- **Regular:** last `MAX_HISTORY` messages (default 10) from SQLite per conversation
- **Long-term:** durable facts (e.g. name) per `user_id`, injected every turn
- Flow: `main.py` → `memory.py` / `long_term_memory.py` → `intent.py` → `llm.py` / `retrieve.py`

## Intents

- **CHAT** → normal reply from Ollama/Gemini/mock
- **RETRIEVE** → search files in `docs/` (SQLite FTS), then answer from snippets
- **REMEMBER** → keep something / recall something already saved
- **ACTION** → do a task. v1 whitelist:
  - `open_app` — runs now (`open -a` on macOS)
  - `send_email` — glass composer (to / subject / body, urgent, attach, generate body) then **Confirm / Cancel**. Sent via Composio Gmail. Everything else is refused. Every attempt is audited in `actions_log`.

Say *send an email* (even without the details). Michelle asks for what’s missing and the composer drops under her message. Fill it, press **Send** with the chat box empty (or Enter in the body), then Confirm. Send posts the fields as an email draft — it does not run the body through chat, intent, or memory. Files and generate sit in the same bottom bar as Send.

**Tap — microphone.** Tap **tap** to record the body; tap again to stop. Whisper transcribes what you said, then the LLM only fixes grammar and fillers — it does not chat as Michelle, does not read long-term memory, and does not rewrite the letter. Voice is body-only: it never sends mail. **Confirm** is still required.

On first hold, macOS may ask for the microphone. That press does not send (and does not tap-draft). Allow, then hold again.

Local Whisper `base` runs in the same venv as `b`. First download is a few hundred MB; later holds reuse the cache. Set `WHISPER_MODEL` in `.env` (default `base`). On CPU it uses `int8` (`WHISPER_COMPUTE_TYPE`) so you should not see a float16 warning. Install `faster-whisper` into that venv (`pip install -r requirements.txt` after `source venv/bin/activate`). Restart `f` after `index.html` or `main.js` changes (Electron does not hot-reload).

Do not put specs or runbooks in [`docs/`](docs/) — that folder is Michelle’s RAG KB.

With `INTENT_MODE=llm`, REMEMBER is meaning-based (“keep in mind”, “don’t forget”, “note that…”, etc.), not only the words “remember this”.

If she’s unsure something should be saved forever, she’ll ask **yes/no** next; yes → long-term memory.

Examples: `keep in mind that I'm 22` · `remember I prefer short replies`

### `COMPOSIO_API_KEY` (`.env`, optional)

Auth for the Composio email executor. Without it the backend still boots and the whole email flow works up to Confirm, which then replies that email isn't connected yet (no crash, no pretend success).

To enable real sends (Gmail), Michelle needs **Composio Platform**, not Composio For You:

1. Open [dashboard.composio.dev](https://dashboard.composio.dev) → **Platform** → your project → Getting Started / Settings → API keys. Copy a **project** API key (`ak_...`). A For You consumer key (`ck_...`) from Sessions & API Key will 401 and is not interchangeable.
2. In that **same** Platform project, connect Gmail (one OAuth). Michelle sends as that inbox.
3. Paste the project key into `.env` as `COMPOSIO_API_KEY=...` (leave `COMPOSIO_USER_ID=default` unless Composio gave you a different user id).
4. Restart the backend (`b`). Electron (`f`) can stay open.

Then: *send an email* → composer → Confirm → it actually sends as the connected Gmail inbox. Don't paste the key into chat.

This app loads `.env`, not `.env.local`. For You consumer keys (`ck_...`) are ignored.

First-time CLI alternative: `composio login` then `composio dev init` in this repo. That writes a project key to `.env.local`; copy `COMPOSIO_API_KEY` into `.env`.

## Intent modes (`.env`)

```
INTENT_MODE=llm      ← default; uses the SAME model as LLM_PROVIDER (Ollama or Gemini)
INTENT_MODE=rules    ← keyword matching only (no model call)
INTENT_MODE=mock     ← simplest keywords, for terminal testing
```

So with `LLM_PROVIDER=ollama` + `INTENT_MODE=llm`, Ollama does chat **and** intent/remember detection first (rules only if Ollama fails).

## Docs / RETRIEVE (how to “link” your own files)

1. Put `.md` or `.txt` files in the [`docs/`](docs/) folder (samples are already there).
2. Restart the backend (`b`) so Michelle re-indexes.
3. Ask things like “What’s the refund policy?” or “How much vacation do we get?”

No Postgres. Search is local SQLite full-text over that folder. Later you can swap in embeddings/pgvector behind the same retrieve API.

Try these demo questions against the sample KB:

- What’s the refund policy?
- How many remote days per week?
- What’s the learning budget?

## Reset (so someone else doesn’t get your chats/name)

Personal data lives in `michelle.db` (gitignored) and Electron `localStorage`. Sample files in `docs/` stay.

```bash
# stop backend first (Ctrl+C), then:
./scripts/reset_michelle.sh
```

Then clear localStorage in Electron DevTools:

```js
localStorage.removeItem('michelle_user_id')
localStorage.removeItem('michelle_conversation_id')
location.reload()
```

A fresh git clone has no `michelle.db`, so it’s already clean.

## UI

- Run `f` in terminal to start Electron. Restart `f` after `index.html` or `main.js` changes (the backend `b` hot-reloads; Electron does not).

Click top left square → Collapse animation → Square into circle  

Click floating circle → Expand animation

Email composer: to / subject / body in the chat card; **files**, **undo**, and **tap** (mic) stay pinned above Send. Tap the mic to record the body, tap again to stop. Chat history in that pane scrolls. Confirm / Cancel still appear once the draft is complete.
