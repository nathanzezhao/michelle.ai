# SPEC-COMMAND-LAYER — Pre-intent cosine catalog

**Author:** Sam (product), from Nathan (locked)
**Status:** Approved for build — current Track 2 slice. Inbox briefing stays PARKED.
**Note:** This file lives at repo root on purpose. Do not move it into `docs/` — that folder is Michelle's RAG knowledge base and this spec would pollute retrieval.

Replaces ACTION *routing* in [SPEC-PIPELINE.md](SPEC-PIPELINE.md) §2.1. Does **not** replace the ACTION state machine, Confirm/Cancel, executors, or `actions_log`. Those still run **after** a handler is locked.

---

## 1. Goal

**User outcome:** When Nathan tells Michelle to open, close, quit, or email, she matches the **handler**, not a lucky keyword. Chat, docs, and memory still go through the classifier. She never treats a chat sentence as an order because it contained the word "open."

**In scope:**
- A cosine command layer on every `POST /chat` **before** `classify_intent`.
- Match to handlers `open_app`, `close_app`, `quit_app`, `send_email`.
- Skip `classify_intent` on a unique hit; lock that handler; run the existing ACTION engine.
- Miss / tie / CHAT-sink → classifier may only emit CHAT | RETRIEVE | REMEMBER.
- Drop **Do this**. Ignore `force_intent=ACTION`.
- `command_vectors` stores handler **prototypes only**.

**Out of scope:** see §14. Inbox briefing stays parked. No new embed env var. No keyword ACTION fallback. No PDF waterfall.

---

## 2. Locked decisions (do not relitigate)

1. **Layer always before `classify_intent` on `POST /chat`.** Typed Send, spoken-then-Send, and Voice blob (`POST /chat/voice` → `/chat`) all hit it. Composer Send and email tap stay sidecars — they still do not go through `/chat`.
2. **This is not the PDF waterfall.** Cosine catalog → (hit? ACTION engine : classify CHAT/RETRIEVE/REMEMBER) is allowed. Action→Retrieval→Memory→Fallback is still rejected.
3. **Match handlers, not user-string whitelists.** Cosine against handler prototypes. `ACTION_WHITELIST` is handler metadata (risk, params, executor) **after** a lock. It is not a filter on the user's text. The LLM does not pick the handler.
4. **Risk lives on the handler object in code.** Never a list of user phrases, never LLM output, never env, never `payload_json`. Same rule as SPEC-PIPELINE §2.4 / §7, restated so nobody "fixes" routing by scanning the utterance against the dict keys.
5. **Hit:** cosine ≥ **0.62** **and** margin (top − second) ≥ **0.08**. Skip `classify_intent`. Lock that handler. Run existing param extraction + state machine + executor.
6. **Miss / tie / CHAT-sink:** no lock. `classify_intent` runs with CHAT | RETRIEVE | REMEMBER only. No ACTION label. No `_looks_like_action_order` promotion. No keyword ACTION fallback in rules/mock either.
7. **Embed `None`:** no task. Honest sentence. No keyword fallback. See §9.
8. **Incomplete unique hit still locks.** Bare "open" / "send an email" with a unique handler hit **asks** for missing params (`AWAITING_INPUT`). It does not fall through to CHAT.
9. **Mixed:** split on `and` / `also` / `then`, embed each clause. See §7.
10. **Pad / PENDING / composer continuation runs before cosine.** Existing AWAITING_INPUT merge, PENDING typed-yes → buttons, composer dismiss/resume, pad "close it" bind — those keep the already-locked handler. Cosine does not steal a continuation turn.
11. **Drop Do this.** Clarifying chips are Look up docs / Remember / Just chat. `force_intent=ACTION` is ignored (treated as unforced). `force_intent` of CHAT | RETRIEVE | REMEMBER still works.
12. **Future skills = new handler object + prototypes.** Not a new classifier label. Not a new env flag.
13. **`command_vectors` = prototypes only.** Never email bodies, never chat transcripts, never `actions_log` payloads.
14. **`/chat` `routed_by=catalog` on hit.** Misses do not claim catalog.
15. **Same embed as the pad.** `EMBED_MODEL` (fallback `RETRIEVE_EMBED_MODEL`, default `nomic-embed-text`) in `embeddings.py`. **No new env var.**

---

## 3. Order on `POST /chat`

```
load history + long-term facts
  → PENDING typed yes/no (buttons only; unchanged)
  → pad / AWAITING_INPUT / composer continuation (if this turn belongs to an open job)
  → normalize utterance (§6)
  → cosine command layer (§5)
        hit     → skip classify_intent; lock handler; ACTION engine
        miss    → classify_intent ∈ {CHAT, RETRIEVE, REMEMBER} only
  → save turn / facts / action row
```

Continuation **before** cosine means:

| Already open | This turn | Cosine? |
|---|---|---|
| `PENDING` + typed yes/no | Point at buttons. Do not confirm. | No |
| `AWAITING_INPUT` + related params / dismiss / resume | Merge, cancel, or reopen that row | No |
| Composer sidecar (Send / tap) | Not `/chat` | No |
| Pad follow-up that binds "close it" / "quit those" to `session_context` | Keep pad bind | No |
| No open job, or unrelated new order | Cosine | Yes |

If continuation does not claim the turn, cosine always runs. Do not classify first and "maybe promote" later.

---

## 4. Catalog

Executable handlers (v1 lock):

| Handler | Risk (code object) | Required params | Executor |
|---|---|---|---|
| `open_app` | low | `app_names` | native |
| `close_app` | low | `app_names` | native |
| `quit_app` | high | `app_names` | native |
| `send_email` | high | `recipient`, `subject`, `body` | Composio |

Plus a non-executable **CHAT-sink** in the same catalog (prototypes like greetings / small talk). If CHAT-sink wins the cosine, that is a **miss**, not a lock. Never execute. Never write `actions_log`.

Risk is read off the handler object **after** lock. Confirm/Cancel, native vs Composio, and `actions_log` stay SPEC-PIPELINE.

---

## 5. Match rules

For each clause (whole utterance if unsplit):

1. Normalize (§6).
2. Embed with the pad helper (`embeddings.embed_text`). Same model. If `None` → §9.
3. Cosine vs `command_vectors` prototypes. Score a handler as **max** cosine across its prototypes. CHAT-sink is a competitor, not an executable.
4. **Hit** iff `top >= 0.62` and `(top - second) >= 0.08` and top is an executable handler.
5. **Tie:** margin < 0.08 (including executable vs CHAT-sink). Miss.
6. **Miss:** top < 0.62, or CHAT-sink wins, or no vectors. Classifier only.

On hit:

- `classify_intent` is **not called**.
- Handler id is locked. Param extraction may fill/missing-list; it may **not** change handler id.
- `/chat` returns `engine: "action"`, `action_type` = locked handler, `routed_by: "catalog"`.
- Incomplete params → ask (§8). Complete → existing low-risk execute / high-risk PENDING.

On miss:

- No `routed_by: "catalog"`.
- No task fields.
- Classifier cannot emit ACTION. Rules/mock cannot keyword-route ACTION. `force_intent=ACTION` does nothing.

---

## 6. Normalize (before embed)

Apply to every clause, in order:

1. Lowercase.
2. Strip filler tokens as whole words: `um`, `uh`, `lol`.
3. Strip a **leading** `hey` (and leftover whitespace). Do not strip `hey` in the middle of a clause.

Do not stem, do not spell-correct, do not drop app names. Pad pronouns (`it` / `those`) are continuation, not cosine.

---

## 7. Mixed clauses

Split the (already normalized) utterance on `\band\b` / `\balso\b` / `\bthen\b`. Embed each clause.

| Clauses | Product |
|---|---|
| One unique executable hit, rest miss | Lock that handler. Missed clauses are not tasks. |
| Several unique hits, **same** handler | Merge params (e.g. app names). One row. |
| Several unique hits, **different** handlers | Existing single-slot + `queue`: first unique hit this turn, rest queued after SUCCESS. Same as today's two-email queue. |
| Any clause ties | That clause is not a lock. Other unique hits still lock. |
| Trailing name after the same handler ("open notes and safari") | Second clause with no verb attaches to the previous unique same-handler hit if it is an app-name leftover — one `open_app`, `app_names: [Notes, Safari]`. Do not CHAT-sink "safari" into a miss that drops Safari. |

Do not execute two non-terminal rows at once (SPEC-PIPELINE §10-A still holds).

---

## 8. Incomplete unique hit

A unique hit with missing required params **asks**. Examples:

- "open" / "open an app" → `open_app`, `AWAITING_INPUT`, ask which app.
- "close" / "quit" with no name and no pad bind (pad already ran) → ask which app.
- "send an email" / "email" with no to/subject/body → `send_email`, composer / `AWAITING_INPUT`, ask.

Do not re-classify. Do not reply as CHAT. Do not invent params (existing grounding: no made-up addresses or bodies).

---

## 9. Embed `None`

Ollama embed down, empty vector, or helper returns `None`:

- **No task.** No `actions_log` row. No native open. No Gmail.
- **No keyword fallback.**
- Classifier still runs for CHAT | RETRIEVE | REMEMBER (docs/memory must not die because embed is down).
- If that classifier result is CHAT, the user-visible `answer` is an **honest sentence** that task matching is unavailable — not "Opened Notes.", not a hallucinated send, not silence.
- RETRIEVE / REMEMBER engines still run when classified. Do not fail a refund-policy lookup because the catalog is down.

---

## 10. `command_vectors`

Same `michelle.db`. Prototypes only.

- Columns: handler id (`open_app` | `close_app` | `quit_app` | `send_email` | `CHAT`), prototype phrase, embedding.
- Seed short command phrases per handler, plus CHAT-sink small talk. Ned picks the seed list; product bar is "orders match, chat does not."
- **Never** write email bodies, subjects, recipients, chat turns, or `actions_log` payloads into this table.
- Rebuild on backend start from the seed list (like docs ingest). Do not learn from live mail.

Adding a skill later: new handler object (risk + params + executor) **and** new prototypes here. That is the whole extension surface.

---

## 11. `/chat` contract

Existing SPEC-PIPELINE fields stay. Add:

| Field | When | Value |
|---|---|---|
| `routed_by` | catalog **hit** only | `"catalog"` |

Miss / classify path: omit `routed_by` or do not set `"catalog"`. Unsupported / CHAT-sink: `engine: "chat"`, no task fields, no catalog claim.

Clarifying (`engine: "clarify"`) may still fire on a **classifier** miss when confidence < 0.6 and top two shelves are close. Chips: RETRIEVE / REMEMBER / CHAT only. No ACTION chip.

---

## 12. Clarifying chips + `force_intent`

Shipped chips minus **Do this**.

- Backend `CLARIFY_CHIPS`: Look up docs, Remember, Just chat.
- Kit renders whatever the API returns; do not hardcode Do this.
- `force_intent=ACTION` → ignore (unforced). Cosine then classify as a normal turn.
- `force_intent` CHAT | RETRIEVE | REMEMBER still re-runs the same utterance without pausing.

There is no chip that forces a handler. If cosine missed, Nathan chats or says the order again.

---

## 13. Environment

| Var | Role |
|---|---|
| `EMBED_MODEL` | Already used by the pad (`embeddings.py`). Command layer uses the same call. |
| `RETRIEVE_EMBED_MODEL` | Fallback if `EMBED_MODEL` unset. Default `nomic-embed-text`. |

**No new env var.** Not a cosine threshold knob. Not a catalog on/off flag. Thresholds 0.62 / 0.08 are code constants.

---

## 14. Out of scope

- Inbox briefing / Gmail read (PARKED).
- New handlers beyond the four + CHAT-sink.
- PDF sequential engines, embeddings of documents as the ACTION router, pgvector.
- Keyword / regex ACTION fallback (including rules/mock "open\|launch\|email").
- Using `ACTION_WHITELIST` keys as a user-text filter.
- Storing mail or chat in `command_vectors`.
- Auto-submit from chat-bar mic (already locked: fill input, wait for Send).
- Track 3 vision / capture.
- Changing Confirm/Cancel, Composio send, or `actions_log` semantics.

---

## 15. Acceptance (QA bar — Ray's team)

Ada stubs `embeddings.embed_text` / cosine. Do not require live Ollama embed in CI. R0 stays green. Quinn does live utterances after.

**Routing**
1. "open Notes" → skip classify (spy: not called), `engine: "action"`, `action_type: "open_app"`, `routed_by: "catalog"`, `risk: "low"` from the handler object.
2. "quit Safari" → `quit_app`, `risk: "high"`, Confirm if params complete.
3. "send an email" (no fields) → unique hit, `send_email`, `AWAITING_INPUT` or composer ask, **not** CHAT.
4. "hey um open Notes lol" → same as "open Notes" after normalize.
5. Small talk / "what's the refund policy?" / "remember I prefer short replies" → no catalog hit, no `routed_by: "catalog"`, classifier RETRIEVE/REMEMBER/CHAT as today.
6. Tie (two handlers within 0.08) → no lock, no task, classifier only.
7. CHAT-sink wins → miss, no task.

**Forbidden paths**
8. `ACTION_WHITELIST` is never consulted as a user-string match. Code inspection + a phrasing that is not a dict key still hits via prototypes.
9. No keyword ACTION fallback: with catalog miss, "open Notes" stubbed as miss does **not** become ACTION via rules/regex/`_looks_like_action_order`.
10. `force_intent=ACTION` does not lock a handler and does not skip cosine.
11. `clarify_options` never includes ACTION / Do this.
12. Embed `None` → no `actions_log` row, no executor, honest CHAT sentence (or RETRIEVE/REMEMBER if classified). Never a pretend open/send.

**Continuations**
13. Pad "close it" after a successful open still binds from `session_context` **without** cosine stealing the turn.
14. PENDING typed "yes" still does not send; buttons only.
15. Composer Send / email tap still do not hit `/chat` or this layer.

**Mixed**
16. "open Notes and Safari" → one `open_app`, both names.
17. "open Notes then send an email" → `open_app` now; `send_email` queued (or asked) after, never two non-terminal rows at once.

**Audit / risk**
18. Risk on the response and in behavior matches the handler object, not the user's words, not the LLM, not the DB payload.
19. `command_vectors` rows are prototypes only — Ada asserts no body-like / `@` mail payload in the table after an email turn.

---

## 16. Who (for Tom)

- **Ned:** cosine layer before `classify_intent`; `command_vectors`; lock handler; skip classify on hit; strip ACTION from classifier + rules/mock; ignore `force_intent=ACTION`; drop Do this from `CLARIFY_CHIPS`; `routed_by`; embed-None honest path. Reuse `embeddings.py`. No new env.
- **Kit:** chips from API only; Do this gone. No new UI for catalog.
- **Oz:** no new env. Same Ollama embed as pad (`EMBED_MODEL` / `nomic-embed-text`). Document in README (already this slice).
- **Ray:** Ada §15; Quinn live open/close/quit/email vs chat; Vale embed-down and whitelist-as-filter abuse.

Have Tom assign Ned (and Kit for the chip). Have Ray assign Ada/Quinn/Vale. Sam does not implement.
