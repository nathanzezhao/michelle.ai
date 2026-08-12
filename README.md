Uses Electron to create a desktop window hosting an AI agent.

Project roadmap and checklist: [ROADMAP.md](ROADMAP.md)

VIDEO DEMOS:

Video demo 07/02/2026:

[https://github.com/user-attachments/assets/36f5f3f5-7986-40b3-92b2-21973c9dfdfb](https://github.com/user-attachments/assets/36f5f3f5-7986-40b3-92b2-21973c9dfdfb)

Video demo 07/17/2026 (memory.py, intent.py, llm.py, michelle.db):

[https://github.com/user-attachments/assets/201dd571-df6e-40d5-bfd9-fc0a9b7425e9](https://github.com/user-attachments/assets/201dd571-df6e-40d5-bfd9-fc0a9b7425e9)

LLMs:

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
- **ACTION** → not live yet (next). Tool requests currently fall through as CHAT.

With `INTENT_MODE=llm`, REMEMBER is meaning-based (“keep in mind”, “don’t forget”, “note that…”, etc.), not only the words “remember this”.

If she’s unsure something should be saved forever, she’ll ask **yes/no** next; yes → long-term memory.

Examples: `keep in mind that I'm 22` · `remember I prefer short replies`

### Intent modes (`.env`)

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

- Run `f` in terminal to start Electron.

Click top left square → Collapse animation → Square into circle  

Click floating circle → Expand animation
