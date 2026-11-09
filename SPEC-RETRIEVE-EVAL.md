# SPEC-RETRIEVE-EVAL — Code evaluator + one diagnostic

**Author:** Sam (product), from Nathan (locked)
**Status:** Approved for build — current Track 2 slice. Inbox briefing stays PARKED.
**Note:** This file lives at repo root on purpose. Do not move it into `docs/` — that folder is Michelle's RAG knowledge base and this spec would pollute retrieval.

Amends retrieval miss behavior in [SPEC-PIPELINE.md](SPEC-PIPELINE.md) §2.9 / R0 #5. Command routing stays [SPEC-COMMAND-LAYER.md](SPEC-COMMAND-LAYER.md).

---

## 1. Goal

**User outcome:** When docs lookup cannot actually answer, Michelle **does not invent**. Hopeless misses stay the canned sentence. Close misses add **one** clarifying question; the next non-bail reply is searched **once**. Then she stops. `hey` is still a greeting. `open Notes` still opens Notes.

**In scope:**
- Code evaluator before `ask_llm_with_context`. Empty or weak chunks skip the LLM.
- Close-miss: miss stem + one template question + pad.
- Hopeless / second miss: verbatim `RETRIEVE_MISS_REPLY`, no question, no pad.
- One merge retry. Cosine still runs first on the follow-up turn.

**Out of scope:** inbox briefing, chips, LLM-as-judge, extra env, diagnostic loop, SSE, escalation, auth, Track 3, extra actions. Do not put this spec in `docs/`.

---

## 2. Locked decisions (do not relitigate)

1. **Evaluator is code**, not an LLM-as-judge. No extra model, no extra env.
2. Empty context already skips the LLM. The bug is **weak chunks that still reach Ollama**.
3. **Hopeless:** `search()` returned `[]` → verbatim `RETRIEVE_MISS_REPLY`, no question, no pad.
4. **Close-miss:** `search()` returned ≥1 chunk and the evaluator kept 0 → `RETRIEVE_MISS_REPLY` + one template question + pad. `"what is the teleportation policy"` is a close-miss on this KB (FTS hits `"policy"`, relevance drops `teleportation`).
5. **Exactly one diagnostic per gap.** Second miss after the merged search → verbatim miss copy, clear pad, stop. A later new RETRIEVE may ask again.
6. **Sentence in `answer`.** No chips. No `engine: "clarify"`. Kit idle. Voice / chat-bar inherit `POST /chat`.
7. **Cosine still runs first** on the follow-up turn. Catalog hit cancels the diagnostic. Do **not** add retrieve-gap to `skip_command_layer`.
8. **Pad only** (`last_retrieve_gap`). Never `long_term_facts`. Never `format_for_prompt`. `get()` must round-trip the key.
9. **Template question**, not an LLM-written ask. Stem stays `RETRIEVE_MISS_REPLY`. DNE never user-facing.
10. **Next turn claims the diagnostic unless it is a bail.** Do not require token overlap with the original query. Bails: greeting (`_is_greeting_only`), nvm/nevermind, catalog unique hit, `force_intent` CHAT/REMEMBER/RETRIEVE, ACTION continuation. `"try again"` with no new content still claims once, then stops if still miss.
11. Empty `query_tokens` → miss, not keep-all.
12. Distinctive query tokens (length ≥ 6, not generic like `policy`) must all appear in a kept chunk. Generic-only overlap is a close-miss.

---

## 3. Order on `POST /chat`

```
PENDING / pad "close it" / composer continuation (clears retrieve gap)
  → cosine catalog
        hit     → clear gap; lock handler
        miss    → if gap and not bail: skip classify; merge original + this text;
                  search once; keep-eval against the follow-up; clear gap
                  else classify CHAT | RETRIEVE | REMEMBER
  → RETRIEVE: evaluator → hit LLM | close-miss ask | hopeless stop
```

Retry grounded call uses history **without** `retrieve*` rows (empty history is allowed).

---

## 4. Copy

Hopeless / second miss (frozen):

> I looked through my docs and couldn't find anything relevant. Try another question, or add a file under the docs/ folder and restart the backend.

Close-miss: that stem + `Which topic or document did you mean?`

---

## 5. Acceptance (QA bar — Ray's team)

Ada: `LLM_PROVIDER=mock` `INTENT_MODE=rules`. Quinn live after Ada is green.

1. Hopeless (`search()` `[]`): `answer == RETRIEVE_MISS_REPLY`, no `?`, no pad, LLM not called.
2. Close-miss teleportation policy: stem + `?`, pad set, no refund marker, LLM not called.
3. Refund hit unchanged (`refund_policy.md` + marker).
4. Next-turn merge can hit (`whats the return thing` then `refunds dude`).
5. Second miss: verbatim stop, pad cleared, no second `?`.
6. `hey` / nvm after close-miss → CHAT, pad gone.
7. `open Notes` after close-miss → `engine: "action"`, `routed_by: "catalog"`, gap cleared.
8. No DNE string. No facts from miss. No email on the gap.

---

## 6. Who (for Tom)

- **Ned:** evaluator, pad field, catalog-then-diag, one merge retry.
- **Kit:** idle.
- **Oz:** no new env.
- **Ray:** Ada then Quinn. Vale only if a model sneaks in.
