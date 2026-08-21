# SPEC-PIPELINE — Non-Question Pipeline v1 (ACTION Engine + contract hardening)

**Author:** Sam (product)
**Status:** Approved for build — Slice 1. Code must not start until Ada's R0 regression suite exists (Nathan's decision 10).
**Source:** `rag_agent_pipeline_spec.pdf` (SPEC-RAG-AGENT-V1), scoped to the non-question half only (`Is Question? -> False`), amended by Nathan's locked decisions from the pipeline redesign review.
**Note:** This file lives at repo root on purpose. Do not move it into `docs/` — that folder is Michelle's RAG knowledge base and this spec would pollute retrieval.

---

## 1. Goal & scope

**User outcome:** When Nathan tells Michelle to *do* something (not ask something), she either does it, asks to confirm it, or asks for what's missing — and never silently does anything risky, never pretends she did something she didn't, and never loses track of what she was asked.

**In scope (Slice 1):**
- ACTION becomes a live fourth intent (currently parked → falls through to CHAT).
- Two whitelisted actions: `open_app` (low risk, native macOS) and `send_email` (high risk, Composio).
- Task state machine persisted in a new `actions_log` table.
- Confirm/Cancel buttons in the Electron UI for high-risk actions.
- `/chat` response contract additions so QA can assert routing.
- Graceful degradation when `COMPOSIO_API_KEY` is absent.
- Startup cancellation of stale pending actions.

**Out of scope:** see §12. Notably: document vectors (Slice 2), any schema adoption from the PDF beyond `actions_log`, async memory scoring, auto-resume of actions after restart.

**Non-question pipeline after this slice:**

```
User input (is_question = false)
  → single classifier call (intent.py): CHAT | RETRIEVE | REMEMBER | ACTION
      ACTION   → param extraction → state machine → executor (native | Composio)
      REMEMBER → existing store/recall flow (unchanged)
      RETRIEVE → existing docs FTS flow (unchanged)
      CHAT     → existing reply + synchronous memory assessor (unchanged)
```

---

## 2. Locked decisions (do not relitigate)

1. **Router:** the existing single classifier call in `classify_intent()` is extended with ACTION as a fourth live label. No sequential engine waterfall (the PDF's Action→Retrieval→Memory→Fallback chain is rejected).
2. **Memory scoring:** keep high/medium/low priority + confidence bands mapping to commit/confirm/discard. No 0–10 scale. Scoring stays **synchronous** — the "want me to remember?" ask rides in the same reply, exactly as today.
3. **Action Engine v1:** external actions (email first) included from day one, gated behind confirmation. Low-risk local actions execute immediately with visible-but-subtle feedback — never fully silent, always audited in `actions_log`.
4. **Risk tiers are code-enforced** via a whitelist constant in code. Never LLM-judged, never env-configurable.
5. **Confirmation UX:** Confirm/Cancel **buttons** for high-risk actions. Memory asks stay **typed yes/no**. Buttons = action, typed = memory — this is the disambiguation mechanism (see §10-A).
6. **Composio** is the external executor (Nathan's directive). The executor is an interface: native macOS for local actions, Composio SDK for external. Missing `COMPOSIO_API_KEY` → classify + confirm still work, then a graceful "email isn't connected yet" reply.
7. **Schema:** keep `long_term_facts` (with `user_id`). Do **not** adopt the PDF's `entities`/`memories` tables. Add `actions_log` (§6). **No auto-resume** after backend restart — on startup, stale `PENDING`/`AWAITING_INPUT` rows are marked `CANCELLED` (security review decision; the PDF's "resume when AWAITING_INPUT" directive is rejected).
8. **/chat contract** gains `engine` plus action fields (§8) so QA can assert routing. Memory decisions already exposed via `remembered`/`asked_to_remember`.
9. **DNE stays internal.** User-facing copy for retrieval misses stays conversational (current `RETRIEVE_MISS_REPLY`). No "Does Not Exist (DNE)" string ever reaches the user.
10. **Tests first.** Ada's R0 regression suite over current behavior is a prerequisite. This spec + R0 gate Slice 1 code.
11. **Document vectors** (sqlite-vec + nomic-embed-text) are Slice 2, after Oz's venv fix. Mentioned here only as the next slice.

---

## 3. Extended classifier (intent.py)

### 3.1 Routing

`classify_intent()` keeps its exact shape — one LLM call, question-first tree. Changes:

- `INTENTS` becomes `("CHAT", "RETRIEVE", "REMEMBER", "ACTION")`; `RESERVED_INTENTS` is removed.
- In `_classify_with_llm()`, the `kind == "ACTION" → intent = "CHAT"` demotion is deleted: on the non-question path, `kind = ACTION` now yields `intent = ACTION`. The prompt's Step 2b already labels ACTION ("external task (email, remind me to…)"); update the wording from "Parked — still label ACTION" to a live definition: *ACTION = an order to perform a task on the computer or an external service (open an app, send an email). Not a memory instruction, not a doc lookup.*
- The **question path is untouched**: questions still route only to REMEMBER/RETRIEVE/CHAT by shelf scores. "Can you send an email to Alex?" is phrased as a question but is an order — the existing prompt rule "orders are not questions even with a ?" already handles this; add it to the R0/R1 test set rather than adding code.
- Rules and mock modes get a deterministic ACTION branch so Ada can test offline (e.g. leading verb `open|launch|start <app>`, `send an email|email <person>` → ACTION). Same fallback ladder as today: LLM failure → rules.

### 3.2 Param extraction — `analyze_action_request()`

One extra LLM call, made **only when intent = ACTION** — exactly analogous to `analyze_remember_request()` running only when intent = REMEMBER. No other turn pays for it.

Input: user text, filtered history (`history_for_reply`), and — when continuing a paused action — the stored task context. Output:

```json
{
  "action_type": "send_email",            // must be a whitelist key, else "unsupported"
  "resolved_params": {"recipient": "alex@example.com"},
  "missing_params": ["subject", "body"],
  "confidence": 0.9
}
```

Rules:
- `action_type` is validated against the whitelist **in code** after the LLM call. Anything not whitelisted → treated as `unsupported`: Michelle replies conversationally that she can't do that yet, `engine` reports `"chat"`, **no `actions_log` row** is written. The LLM never gets to invent an executable action type.
- Param values must be grounded in the user's message/history (same spirit as `_fact_supported_by_text`); the extractor must not invent an email address or body.
- Rules/mock modes implement a regex extractor for the two v1 actions (offline QA).
- `llm.py`'s `SYSTEM_PROMPT` line "You cannot send email, set reminders, or take external actions yet" must be updated — she can now open apps and send email (when connected), and still cannot do anything else.

---

## 4. Task state machine

One row in `actions_log` per action attempt. States (decision 7):

| Status | Meaning |
|---|---|
| `AWAITING_INPUT` | Required params missing; Michelle asked for them in the reply. |
| `PENDING` | Params complete, high-risk, Confirm/Cancel buttons shown. |
| `CONFIRMED` | User pressed Confirm; executor is running (transient, within one request). |
| `SUCCESS` | Executor completed. Terminal. |
| `FAILED` | Executor errored (incl. no Composio key). Terminal. `payload_json.error` says why. |
| `CANCELLED` | User pressed Cancel, action was replaced/dropped (§10), or startup sweep. Terminal. |

### Transitions

```
create (high-risk, params missing)   → AWAITING_INPUT
create (high-risk, params complete)  → PENDING
create (low-risk, params complete)   → CONFIRMED → SUCCESS | FAILED   (same request)
create (low-risk, params missing)    → AWAITING_INPUT                  (e.g. "open an app" with no name)

AWAITING_INPUT + related user turn supplying params:
    still missing some → AWAITING_INPUT (params merged)
    all resolved, high-risk → PENDING (buttons shown now)
    all resolved, low-risk  → CONFIRMED → SUCCESS | FAILED

AWAITING_INPUT | PENDING + unrelated user turn → CANCELLED (seam B, §10)
AWAITING_INPUT | PENDING + new ACTION intent   → old CANCELLED, new row created (seam A, §10)
PENDING + Confirm button → CONFIRMED → SUCCESS | FAILED
PENDING + Cancel button  → CANCELLED

backend startup: AWAITING_INPUT | PENDING → CANCELLED (no replay, decision 7)
backend startup: CONFIRMED (crashed mid-execution) → FAILED with error "interrupted_by_restart"
```

That last line is my call, not in Nathan's list: a row stuck in `CONFIRMED` means we crashed while executing and cannot know if the side effect happened. Marking it `CANCELLED` would claim we stopped it; `FAILED` + reason is honest for the audit trail. Nothing is re-executed either way.

Invariant: **at most one non-terminal action row per (user_id, conversation_id)** at any time (§10-A).

State context lives in the `actions_log` row (`payload_json`), not in raw chat history — the PDF's one good idea we keep. Terminal states are never mutated (except the startup CONFIRMED→FAILED sweep above).

---

## 5. Executor interface

```python
class ActionExecutor(Protocol):
    def execute(self, action_type: str, params: dict) -> ExecResult
        # ExecResult: {ok: bool, detail: str, error: str | None}
```

Two implementations, selected per whitelist entry (not per request):

- **NativeExecutor** (local, macOS): `open_app` via `open -a "<App Name>"` (subprocess, no shell string interpolation of user input — pass args as a list). App name goes through as given; a non-existent app returns non-zero → `FAILED`, honest reply ("couldn't find an app called X").
- **ComposioExecutor** (external): `send_email` via the Composio SDK (Gmail toolkit first). Constructed lazily; if `COMPOSIO_API_KEY` is unset it reports not-connected instead of raising at startup.

**Graceful no-key path (must-have, decision 6):** with no `COMPOSIO_API_KEY`, an email request still classifies as ACTION, still extracts params, still asks for missing ones, still shows Confirm/Cancel. On Confirm, the executor returns not-connected → row `FAILED` (`error: "composio_not_connected"`), reply is conversational: *"I can't actually send email yet — Composio isn't connected. Once there's an API key I can."* Never the generic error message, never a pretend success.

---

## 6. `actions_log` schema

```sql
CREATE TABLE IF NOT EXISTS actions_log (
    action_id       TEXT PRIMARY KEY,          -- uuid4
    user_id         TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    action_type     TEXT NOT NULL,             -- whitelist key
    payload_json    TEXT NOT NULL,             -- {resolved_params, missing_params, risk, error?}
    status          TEXT NOT NULL,             -- PENDING|CONFIRMED|SUCCESS|FAILED|CANCELLED|AWAITING_INPUT
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_actions_open
    ON actions_log (user_id, conversation_id, status);
```

Same `michelle.db`, new module `actions.py` (mirrors `long_term_memory.py` conventions: `init_db()`, WAL, additive migration style). Do **not** adopt the PDF's `entities`, `memories`, `vec_*`, or `document_chunks` tables. Every action attempt — including low-risk auto-executed ones — writes a row. That is the audit requirement behind "never fully silent."

---

## 7. Whitelist v1 (code constant, security-critical)

```python
ACTION_WHITELIST = {
    "open_app": {
        "risk": "low",
        "required_params": ["app_name"],
        "executor": "native",
    },
    "send_email": {
        "risk": "high",
        "required_params": ["recipient", "subject", "body"],
        "executor": "composio",
    },
}
```

- Risk is read from this dict only. There is no code path where an LLM output, env var, or request field sets or overrides risk (decision 4).
- Unknown `action_type` → unsupported flow (§3.2), never executed, never logged as an action.
- Adding an action = adding an entry here + executor support. That's the whole extension surface.

---

## 8. `/chat` response contract additions

Existing fields are unchanged (R0 protects them). New:

| Field | Type | When | Values |
|---|---|---|---|
| `engine` | string | every turn | `"chat"` \| `"retrieve"` \| `"remember"` \| `"action"` |
| `task_id` | string | action turns | `actions_log.action_id` |
| `task_status` | string | action turns | the status after this turn's processing |
| `action_type` | string | action turns | whitelist key |
| `risk` | string | action turns | `"low"` \| `"high"` |
| `confirm_required` | bool | action turns | `true` iff `task_status == "PENDING"` |
| `missing_params` | list | action turns | empty when complete |

Notes:
- `engine` is lowercase and present on **every** turn (QA asserts routing on it). The existing `intent` field stays as-is for back-compat.
- Unsupported action → `engine: "chat"`, no task fields.
- A turn that cancels/drops a prior action while doing something else reports the new turn's `engine`; the drop is visible in the `answer` text (§10-B) and in `actions_log`.
- Memory fields (`remembered`, `asked_to_remember`) are unchanged.

---

## 9. UI contract (Electron, Kit)

### 9.1 Rendering

When a `/chat` (or `/action/confirm`) response has `confirm_required: true`:

1. Render `answer` as a normal assistant bubble (it contains the human-readable summary, e.g. *"Ready to send: to alex@example.com, subject 'Standup', body '…'. Send it?"*).
2. Render a **Confirm** and a **Cancel** button attached to that bubble.
3. Store `task_id` on the buttons.
4. Disable/remove both buttons after either is clicked, or when any newer message is sent (the backend will have cancelled the action anyway — §10).

No buttons for anything else. Memory asks remain plain text ("Say yes or no").

### 9.2 Confirm endpoint

New endpoint (buttons must not go through the classifier — a button press is not natural language):

```
POST /action/confirm
{ "task_id": "...", "decision": "confirm" | "cancel",
  "conversation_id": "...", "user_id": "..." }
```

Response: same shape as `/chat` (`answer`, `conversation_id`, `user_id`, `engine: "action"`, `task_id`, `task_status`, `action_type`, `risk`, `confirm_required: false`, `missing_params: []`). The UI renders `answer` as a new assistant bubble.

Validation: unknown `task_id`, mismatched `user_id`, or a task not in `PENDING` → graceful reply ("that one's already done/cancelled"), no state change, HTTP 200. Both the button press and the outcome are saved to chat history (`save_message`) so the transcript reads coherently.

### 9.3 Low-risk feedback

Low-risk actions show their result as a normal reply bubble (*"Opened Notes."*). That's the "visible but subtle" bar: no buttons, no modal, but always a user-visible sentence plus the `actions_log` row. Never zero-feedback.

---

## 10. Seam resolutions (PM calls — resolved, not open)

### A. Bare "yes" with both an action and a memory ask pending

**Resolution: adopt the proposal.** Typed yes/no **only ever** answers the memory ask. Action confirmation happens **only** via buttons; a typed "yes", "do it", "send it", etc. never confirms an action. Handler order: pending-memory yes/no check runs first (as today), and a memory answer does **not** count as a topic change for a pending action — the action stays pending with its buttons live.

Why: the collision only exists if two input channels can mean "confirm." Splitting the channels (buttons = action, typing = memory) removes the ambiguity structurally instead of asking a classifier to guess, and it's trivially testable. The cost — a user typing "yes" at an email confirm gets the buttons pointed out (*"use the buttons above to send or cancel"*) rather than a send — is the safe direction to fail for a high-risk action.

**One pending action at a time.** A new ACTION request while one is non-terminal cancels the old row (`CANCELLED`) and starts the new one; the reply states the swap in passing (*"Dropped the email draft — opening Notes."*). No queue, no stack. Matches the single-slot `pending_memories` pattern and keeps the state machine, the UI, and QA assertions single-threaded.

### B. Paused action (`AWAITING_INPUT`) when the user changes topic

**Resolution: adopt the proposal — mirror pending-memory behavior.** When an action is `AWAITING_INPUT` and the next message is not supplying the missing params (the action analyzer, given the task context, decides related vs. unrelated), the action is marked `CANCELLED`, the new message flows through the normal pipeline, and the reply carries a one-line note (*"(dropped the email draft)"*). Michelle never nags about it later; the draft is recoverable only by asking again.

Why: this is exactly how the pending-memory ask behaves today (`main.py` clears it on an unrelated follow-up), so the product has one consistent rule: *Michelle's open questions die quietly when you move on.* Holding a paused email in the background invites the worst failure mode — a half-remembered draft firing params into an unrelated conversation days later.

---

## 11. Environment variables

| Var | Default | Meaning |
|---|---|---|
| `COMPOSIO_API_KEY` | unset | Composio auth. Absent → graceful not-connected path (§5). Backend must boot without it. |
| `ACTION_ANALYZER_TIMEOUT` | inherit LLM timeouts | No new knob unless Ned needs one; extraction uses the same `_llm_json` backend as intent. |

Explicitly **not** env vars: the whitelist, risk tiers, confirmation requirements (decision 4). Existing vars (`LLM_PROVIDER`, `INTENT_MODE`, `MEMORY_SAVE_THRESHOLD`, `MEMORY_MIN_PRIORITY`, `MAX_HISTORY`, `MICHELLE_DB_PATH`, etc.) are unchanged.

`.env.example` / README gain `COMPOSIO_API_KEY` documentation.

---

## 12. Out of scope (Slice 1)

- **Document vectors** — sqlite-vec + nomic-embed-text is Slice 2, blocked on Oz's venv fix. `retrieve.py` FTS is untouched this slice.
- The PDF's `entities` / `memories` / `vec_memories` / `document_chunks` schemas, CRUD memory manager, similarity-based conflict resolution, timestamp weighting, reranker, parallel hybrid retrieval.
- The PDF's sequential engine waterfall and 0–10 async memory scoring.
- Auto-resume/replay of actions after restart (explicitly rejected — startup cancels).
- More than one pending action; action queues; scheduled/deferred actions ("remind me at 5").
- Any action beyond `open_app` and `send_email`; any Composio toolkit beyond email.
- User-facing DNE wording changes; retrieval behavior changes of any kind.
- Streaming/SSE, UI history reload, auth/rate limiting (tracked in ROADMAP).
- Changes to memory thresholds, priority bands, or the yes/no memory flow.

---

## 13. Acceptance criteria (QA bar — Ray's team)

All assertable via `/chat` / `/action/confirm` JSON plus direct `actions_log`/`long_term_facts` reads. R0 (current behavior) must stay green throughout; R1 below is new. Run in `INTENT_MODE=llm` for the bar, with rules/mock parity where marked (M).

**Routing & contract**
1. Every `/chat` response contains `engine` ∈ {chat, retrieve, remember, action}; non-action turns carry no task fields. (M)
2. "open Notes" → `engine: "action"`, `action_type: "open_app"`, `risk: "low"`, `confirm_required: false`, `task_status: "SUCCESS"` (or `FAILED` with an honest answer if the app doesn't exist), and an `actions_log` row exists. (M)
3. "send an email to alex@example.com subject hi body hello" → `engine: "action"`, `action_type: "send_email"`, `risk: "high"`, `confirm_required: true`, `task_status: "PENDING"`, `missing_params: []`.
4. A non-whitelisted order ("delete all my files", "book a flight") → `engine: "chat"`, no task fields, no `actions_log` row, reply says she can't do that. **Zero** execution paths for non-whitelist types (code inspection + test).
5. Existing R0 behaviors unchanged: greeting/name flow, RETRIEVE hit + miss copy (`RETRIEVE_MISS_REPLY` verbatim), REMEMBER store/recall, memory auto-save only at high priority + confidence ≥ `MEMORY_SAVE_THRESHOLD`, medium → `asked_to_remember: true`, low → silent discard.

**State machine**
6. "send an email to alex" (no subject/body) → `task_status: "AWAITING_INPUT"`, `missing_params` lists `subject`,`body`; reply asks for them. Follow-up supplying both → same `task_id`, `task_status: "PENDING"`, buttons payload present.
7. `POST /action/confirm` with `decision: "confirm"` on a PENDING task → `SUCCESS` (key present) and the reply reports the send; `decision: "cancel"` → `CANCELLED`, nothing executed.
8. Confirm on an already-terminal or unknown `task_id` → HTTP 200, graceful answer, status unchanged.
9. Terminal rows (`SUCCESS`/`FAILED`/`CANCELLED`) are never mutated by later turns.

**Seams**
10. With an email `PENDING`, typed "yes" does **not** execute it: no status change, reply points at the buttons. With a memory ask also pending, typed "yes" saves the memory (`remembered` populated) and the action stays `PENDING`.
11. With an action `AWAITING_INPUT` or `PENDING`, an unrelated message → old row `CANCELLED`, reply contains a brief drop note, new message handled normally; no mention of the dropped action on subsequent turns.
12. With an action non-terminal, a new action order → old row `CANCELLED`, new row created, reply mentions the swap. At no point do two non-terminal rows exist for one (user, conversation).

**Graceful no-key**
13. With `COMPOSIO_API_KEY` unset: email flow reaches `PENDING` with buttons; Confirm → `task_status: "FAILED"`, `payload_json.error = "composio_not_connected"`, reply says email isn't connected yet — not the generic error string, and never a claimed success. Backend boots cleanly without the key. (M)

**Restart safety**
14. Seed rows in `PENDING` and `AWAITING_INPUT`, restart backend → both `CANCELLED`; a seeded `CONFIRMED` row → `FAILED` (`interrupted_by_restart`). Nothing executes at startup. `SUCCESS`/`FAILED`/`CANCELLED` rows untouched. (M)

**Audit**
15. Every action attempt — including low-risk auto-executes and unsupported-type near-misses that were rejected pre-log — is either visible in `actions_log` (whitelisted) or provably never executed (non-whitelisted). Low-risk executions always produce a user-visible sentence in `answer`.

---

## 14. Who builds what (for Tom)

- **Ned:** classifier extension + `analyze_action_request`, `actions.py` (schema, state machine, startup sweep), executor interface + native/Composio implementations, `/chat` contract fields, `/action/confirm`, system-prompt update.
- **Kit:** Confirm/Cancel buttons per §9 (render, disable rules, `/action/confirm` wiring).
- **Oz:** `COMPOSIO_API_KEY` env plumbing/runbook; venv fix stays the Slice 2 gate.
- **Ray:** R1 suite from §13; R0 must exist first and stay green (Ada), manual seam passes (Quinn), whitelist/no-key/restart abuse cases (Vale).
