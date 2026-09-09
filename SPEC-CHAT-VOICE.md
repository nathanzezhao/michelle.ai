# SPEC-CHAT-VOICE — Main-chat microphone (next slice)

**Author:** Sam (product), from Nathan
**Status:** Locked for next build. Do not start until this spec is the ticket.
**Contrast:** Email **tap** stays isolated. This mic is a **normal chat turn**.
**Note:** This file lives at repo root on purpose. Do not move it into `docs/` — that folder is Michelle's RAG knowledge base.

---

## 1. Goal

**User outcome:** Nathan can talk to Michelle from the main chat bar (not the email composer). What he said becomes the user message for that turn — same as if he typed it.

**In scope:**
- A microphone on the main chat input (`#chat-input` / Send row).
- Local Whisper transcribes first.
- The transcript then runs the **existing** `/chat` pipeline: session pad, classifier, engines, save.
- Reuse the existing capture stack (`main.js` + `mic-capture.html`) where it already works.

**Out of scope:**
- Changing email tap / `/action/draft_body` / `polish_email_body` (those stay body-only).
- Streaming tokens, screen capture, a second Whisper model, cloud STT.
- Reordering today's classify-then-action-pad sequence for **typed** turns.

---

## 2. Locked decisions (do not relitigate)

1. **This is next.** Ahead of chat-history reload and RETRIEVE v2.
2. **Not the email path.** Email tap: Whisper → grammar polish only → composer body. No Michelle persona, no long-term facts, no `session_context`, no `classify_intent`, never `/chat`. Chat voice must not reuse that sidecar.
3. **Transcribe, then a normal turn.** Order after stop-record:

```
audio
  → Whisper (text only; no polish_email_body)
  → that text is user_text
  → same handle_chat as typing:
       history + long_term_facts
       pending memory / draft-pick / action seams
       classify_intent  (CHAT | RETRIEVE | REMEMBER | ACTION)
       ACTION analysis may read session_context.get(user_id, conversation_id)
         (so "close it" / "quit those" work from speech the same as from type)
       save_message as a normal user turn
```

4. **Session context is in-pipeline, not skipped.** The conversation pad (`session_context`) must be available on this turn the same way it is for typed `/chat`. Voice must not get a thinner analyzer.
5. **Classifier is required.** Spoken "what's the refund policy?", "remember I prefer short replies", "open Notes" must route like the typed versions. No voice-only intent, no "always CHAT because it came from the mic."
6. **One pipeline, two inputs.** Typing and chat-voice share `handle_chat`. Prefer the server transcribes then calls that function in-process (multipart `/chat` or `POST /chat/voice` that delegates). Do **not** send chat audio to `/action/draft_body`.
7. **Junk / silence does not become a turn.** Same spirit as `whisper.is_junk_transcript` / short audio: no user bubble, no classify, no session mutation, no saved message. Honest UI ("didn't catch that"), not a fake "thank you" chat.
8. **Email tap does not change.** Composer **tap** remains body-only. Confirm still required to send. Chat mic never sends mail by itself.

---

## 3. Why email tap must stay separate

| | Email tap (today, keep) | Chat mic (this slice) |
|--|-------------------------|------------------------|
| Endpoint | `/action/draft_body` | `/chat` after transcribe |
| LLM | `polish_email_body` only | full Michelle reply / retrieve / remember / action |
| Memory | must not read `long_term_facts` | must read them like typing |
| Session pad | must not | must (`analyze_action_request(..., session_context=pad)`) |
| Classifier | must not | must (`classify_intent`) |
| Saved as | composer body field | `messages` user row |
| Can send mail | no (Confirm still) | only if the turn classifies ACTION `send_email` and the existing composer/Confirm flow runs |

---

## 4. UI (Kit)

- Mic control on the **main input row**, visible when the email composer is not the active job (or always visible for chat; composer **tap** stays on the composer dock).
- Tap to record, tap to stop (same pattern as composer tap). First macOS permission prompt does not submit a turn.
- After a good transcript: show it as a **user chat bubble**, then the usual thinking/scramble while `/chat` runs.
- Do not dump the transcript into the email body.
- Recording state must not collapse/hide the overlay (existing `ensureOverlayVisible` behavior).

---

## 5. Backend (Ned)

- Whisper stays local (`whisper.py`, same model/env as today).
- After transcribe, call the same code path as `handle_chat` with `text = transcript`. Do not invent a parallel router.
- Do not call `polish_email_body` / `EMAIL_BODY_POLISH_SYSTEM` on this path.
- `engine` / `intent` / action fields stay whatever `/chat` already returns — QA asserts routing the same as typed fixtures, with audio or a stubbed `transcribe_wav`.

---

## 6. Acceptance (Ray / Ada)

All of these vs a typed control with the same words (stub Whisper to return that string):

1. Spoken "open Notes" → `engine: "action"`, `action_type: "open_app"`, session pad `last_opened` includes Notes when success.
2. After that, spoken "close it" / "quit those" fills from `session_context` the same as typed (not "unsupported" / not a naked CHAT miss).
3. Spoken "what's the refund policy?" → `engine: "retrieve"` (sample KB).
4. Spoken "remember I prefer short replies" → REMEMBER path, not composer body.
5. `/action/draft_body` is **not** hit. `polish_email_body` is **not** called.
6. Junk/short audio: HTTP 200 or UI-only miss, **no** new `messages` row, pad unchanged.
7. Email composer tap still isolated (existing R1 draft_body tests stay green).
8. R0 `/chat` typed tests stay green.

---

## 7. Who (for Tom)

- **Kit:** chat-bar mic, user bubble from transcript, wire to the chat-voice request (not draft_body).
- **Ned:** transcribe-then-`handle_chat`; keep draft_body untouched.
- **Oz:** no new env unless Whisper path needs a note; same venv as `b`.
- **Ray:** Ada fixtures above; Quinn: talk vs type vs email tap; Vale: junk/silence must not write facts or fire actions.
