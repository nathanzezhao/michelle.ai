import json
import os
import re
from typing import Optional

import httpx
from google import genai

# ACTION is next (tools + confirm). Parked for now — do not classify it.
INTENTS = ("CHAT", "RETRIEVE", "REMEMBER")
RESERVED_INTENTS = ("ACTION",)  # planned; map to CHAT until built
PRIORITIES = ("high", "medium", "low")
BLOCKED_NAME_VALUES = {"michelle", "chelle", "michelle.ai"}

# Only HIGH-priority facts with strong confidence get written.
# Default is strict so casual chat does not pollute long-term memory.
MEMORY_SAVE_THRESHOLD = float(os.getenv("MEMORY_SAVE_THRESHOLD", "0.85"))
MEMORY_MIN_PRIORITY = os.getenv("MEMORY_MIN_PRIORITY", "high").lower()

# Explicit "remember this" commands — always force-save (separate from auto-assessor).
REMEMBER_PHRASES = (
    "remember this",
    "remember that",
    "remember for later",
    "remember for the future",
    "remember for future",
    "remember for conversations",
    "remember for future conversations",
    "please remember",
    "don't forget",
    "do not forget",
    "keep in mind",
    "save this",
    "store this",
)


def _intent_backend() -> str:
    """Which model powers INTENT_MODE=llm: follows LLM_PROVIDER (ollama or gemini)."""
    provider = os.getenv("LLM_PROVIDER", "mock").lower()
    if provider == "ollama":
        return "ollama"
    if provider == "gemini":
        return "gemini"
    # mock chat provider → no paid/local chat model for intent; use rules instead
    return "rules"


def _strip_json_fences(raw: str) -> str:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    # Some local models wrap JSON in prose — grab the first {...} block.
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            raw = raw[start : end + 1]
    return raw


def _llm_json(prompt: str) -> dict:
    """Run a JSON-only prompt on Ollama or Gemini (same backend as chat)."""
    backend = _intent_backend()
    if backend == "ollama":
        return _ollama_json(prompt)
    if backend == "gemini":
        return _gemini_json(prompt)
    raise RuntimeError("No LLM backend available for intent (set LLM_PROVIDER=ollama or gemini)")


def _ollama_json(prompt: str) -> dict:
    model = os.getenv("OLLAMA_MODEL", "llama3.2")
    base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    response = httpx.post(
        f"{base_url}/api/chat",
        json={
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You classify intents and memory for Michelle. "
                        "Reply with ONLY valid JSON. No markdown, no prose."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "format": "json",
        },
        timeout=120.0,
    )
    response.raise_for_status()
    content = response.json().get("message", {}).get("content", "")
    return json.loads(_strip_json_fences(content))


def _gemini_json(prompt: str) -> dict:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is missing from .env")
    os.environ["GEMINI_API_KEY"] = api_key
    client = genai.Client()
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    response = client.models.generate_content(model=model, contents=prompt)
    return json.loads(_strip_json_fences(response.text or ""))


def classify_intent(text: str, history: Optional[list[dict]] = None) -> dict:
    """Classify what the user wants: chat, lookup, action, or explicit remember.

    In llm mode the chat provider (Ollama or Gemini) decides REMEMBER from meaning
    (keep in mind, don't forget, etc.) — not only exact "remember this" wording.
    Rules/mock still use phrase heuristics as a fallback.
    """
    history = history or []
    mode = os.getenv("INTENT_MODE", "llm").lower()

    # Bare name after "what's your name?" is always chat, never a doc lookup.
    if capture_introduced_name(text, history):
        return {"intent": "CHAT", "confidence": 0.95}

    if mode == "rules":
        return _classify_with_rules(text)
    if mode == "mock":
        return _classify_mock(text)

    # INTENT_MODE=llm → use Ollama or Gemini (same as LLM_PROVIDER).
    if _intent_backend() == "rules":
        return _classify_with_rules(text)

    try:
        return _classify_with_llm(text, history)
    except Exception as e:
        print(f"Intent LLM failed ({e}), falling back to rules")
        return _classify_with_rules(text)


def classify_memory_confirmation(text: str) -> str | None:
    """If the user is answering Michelle's 'want me to remember that?' ask.

    Returns 'yes', 'no', or None if this message is not a confirmation.
    """
    lower = text.strip().lower().strip(".!?")
    yes = {
        "yes",
        "yeah",
        "yep",
        "yup",
        "sure",
        "ok",
        "okay",
        "please",
        "please do",
        "go ahead",
        "do it",
        "remember it",
        "remember that",
        "yes please",
        "yeah please",
        "of course",
    }
    no = {
        "no",
        "nope",
        "nah",
        "don't",
        "dont",
        "do not",
        "no thanks",
        "no thank you",
        "forget it",
        "never mind",
        "nevermind",
    }
    if lower in yes or lower.startswith("yes ") or lower.startswith("yeah "):
        return "yes"
    if lower in no or lower.startswith("no ") or lower.startswith("don't"):
        return "no"
    return None


def extract_remember_facts(
    text: str,
    history: Optional[list[dict]] = None,
) -> list[dict]:
    """Pull fact(s) out of an explicit REMEMBER message for long-term storage.

    In llm mode this is LLM-only (no string-slicing after the word "remember").
    Rules/mock keep the lightweight extractor for offline testing.
    """
    history = history or []
    mode = os.getenv("INTENT_MODE", "llm").lower()

    if mode in ("rules", "mock") or _intent_backend() == "rules":
        return _extract_remember_with_rules(text, history)

    try:
        return _extract_remember_with_llm(text, history)
    except Exception as e:
        print(f"Remember extract LLM failed ({e}); not saving (no hard-coded fallback)")
        return []


def analyze_remember_request(
    text: str,
    history: Optional[list[dict]] = None,
    existing_facts: Optional[list[dict]] = None,
) -> dict:
    """After intent=REMEMBER: is this a recall question or a store command?

    Returns:
        {
            "is_question": bool,
            "mode": "recall" | "store" | "unclear",
            "matched_facts": [{"key", "value"}, ...],  # already in long-term memory
            "store_facts": [{"key", "value", "priority"}, ...],  # new things to save
        }
    """
    history = history or []
    existing_facts = existing_facts or []
    mode = os.getenv("INTENT_MODE", "llm").lower()

    if mode in ("rules", "mock") or _intent_backend() == "rules":
        return _analyze_remember_with_rules(text, history, existing_facts)

    try:
        return _analyze_remember_with_llm(text, history, existing_facts)
    except Exception as e:
        print(f"Remember analyze LLM failed ({e}), falling back to rules")
        return _analyze_remember_with_rules(text, history, existing_facts)


def _looks_like_remember_command(text: str) -> bool:
    lower = text.strip().lower()
    # Questions about memory / reminders to do a task are not REMEMBER saves.
    if "remind me" in lower:
        return False
    if re.search(
        r"\b(do you remember|did you remember|what do you remember|can you remember)\b",
        lower,
    ):
        return False
    if any(phrase in lower for phrase in REMEMBER_PHRASES):
        return True
    # Imperative: "remember my age is 22" / "remember I'm allergic to peanuts"
    if re.match(r"^(?:please\s+)?remember\b", lower):
        return True
    return False


def assess_memory_worthiness(
    text: str,
    history: Optional[list[dict]] = None,
    existing_facts: Optional[list[dict]] = None,
) -> dict:
    """Second tracker: should this message become long-term memory?

    The LLM scores priority (high|medium|low) and confidence.
    - high + strong confidence → save now (important=True)
    - medium / unsure → ask_user=True (Michelle asks before saving)
    - low → ignore

    Returns:
        {
            "important": bool,
            "ask_user": bool,
            "confidence": float,
            "priority": "high"|"medium"|"low",
            "facts": [{"key": "...", "value": "...", "priority": "..."}, ...],
        }
    """
    history = history or []
    existing_facts = existing_facts or []
    mode = os.getenv("INTENT_MODE", "llm").lower()

    if mode == "rules" or mode == "mock" or _intent_backend() == "rules":
        if mode == "mock":
            return _assess_memory_mock(text, history)
        return _assess_memory_with_rules(text, history)

    try:
        result = _assess_memory_with_llm(text, history, existing_facts)
        # Local models sometimes mark style prefs as low — recover with rules.
        if not result["important"] and not result.get("ask_user"):
            rules = _assess_memory_with_rules(text, history)
            if rules.get("ask_user") and rules.get("facts"):
                return rules
        return result
    except Exception as e:
        print(f"Memory assessor LLM failed ({e}), falling back to rules")
        return _assess_memory_with_rules(text, history)


def _json_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return False


def _looks_like_question(text: str) -> bool:
    """Syntactic question — not a topic/phrase list."""
    stripped = (text or "").strip()
    if "?" in stripped:
        return True
    return bool(
        re.match(
            r"^(?:please\s+)?(?:what|whats|where|when|who|why|how|"
            r"do|does|did|can|could|is|are|am)\b",
            stripped.lower(),
        )
    )


def _classify_with_llm(text: str, history: list[dict]) -> dict:
    # Prior turns are unused: llama3.2 copies the last label and then
    # misses ordinary lookups and store requests.
    _ = history

    prompt = f"""Answer three independent meaning questions about THIS message only.

wants_recall: true if they are asking you to recall something they already told you,
  or their own name/prefs in memory. That is NOT a document lookup.

wants_store: true if they want future chats to keep a fact or preference they are
  giving you now — including how you should reply, what they like, or who they are —
  however they word that request.
  false if they are only asking whether you already remember something.
  false if they are asking you to do a task (email, remind, tickets).

wants_lookup: true if they want information looked up in docs/knowledge.
  Ordinary policy/product/procedure questions count. Made-up subjects count.
  false for greetings, slang, opinions, tasks, recall questions, and store requests.

User message: {text}

Reply with ONLY valid JSON, no markdown:
{{"wants_recall": true|false, "wants_store": true|false, "wants_lookup": true|false, "confidence": 0.0 to 1.0}}"""

    result = _llm_json(prompt)
    recall = _json_bool(result.get("wants_recall"))
    store = _json_bool(result.get("wants_store"))
    lookup = _json_bool(result.get("wants_lookup"))
    question = _looks_like_question(text)

    # Exclusive resolve in code. Recall beats lookup; statements are not doc misses.
    if recall:
        intent = "CHAT"
    elif store and lookup:
        intent = "RETRIEVE" if question else "REMEMBER"
    elif store:
        intent = "REMEMBER"
    elif lookup and question:
        intent = "RETRIEVE"
    else:
        intent = "CHAT"

    confidence = float(result.get("confidence", 0.5))
    print(
        f"intent_recall={recall} intent_store={store} intent_lookup={lookup} "
        f"question={question} intent={intent}"
    )
    return {"intent": intent, "confidence": max(0.0, min(1.0, confidence))}


def looks_like_question(text: str) -> bool:
    return _looks_like_question(text)


def usable_memory_facts(facts: list[dict] | None) -> list[dict]:
    """Drop blocked/junk facts (e.g. name=Michelle) before save or yes/no."""
    usable = []
    for fact in facts or []:
        key = str(fact.get("key") or "").strip().lower()
        value = str(fact.get("value") or "").strip()
        if not key or not value:
            continue
        if key == "name" and value.lower() in BLOCKED_NAME_VALUES:
            continue
        usable.append(fact)
    return usable


def maybe_promote_to_remember(
    intent: str,
    text: str,
    memory_result: dict,
) -> str:
    """If chat missed an explicit store, promote when the assessor already has a fact.

    llama3.2 often leaves wants_store false on retain-instructions. Do not promote
    questions (those are recall/lookup) and do not wait on a second analyzer call.
    """
    if intent != "CHAT":
        return intent
    if _looks_like_question(text):
        return intent
    if not (memory_result.get("ask_user") or memory_result.get("important")):
        return intent
    if not usable_memory_facts(memory_result.get("facts")):
        return intent
    print("intent_promoted=REMEMBER (assessor facts)")
    return "REMEMBER"


def _classify_with_rules(text: str) -> dict:
    lower = text.strip().lower()

    if _looks_like_remember_command(text):
        return {"intent": "REMEMBER", "confidence": 0.95}

    # Asking what Michelle already knows → chat (uses long-term facts), not docs.
    if re.search(
        r"\b(do you remember|what(?:'s| is) my name|how old am i|what(?:'s| is) my age)\b",
        lower,
    ):
        return {"intent": "CHAT", "confidence": 0.85}

    retrieve_starts = (
        "what is",
        "what's",
        "what are",
        "how do i",
        "how does",
        "find",
        "look up",
        "search for",
    )
    retrieve_words = ("policy", "handbook", "document", "database", "report", "data")
    if lower.startswith(retrieve_starts) or any(word in lower for word in retrieve_words):
        return {"intent": "RETRIEVE", "confidence": 0.75}

    chat_phrases = (
        "hi",
        "hello",
        "hey",
        "how are you",
        "thanks",
        "thank you",
        "bye",
        "good morning",
    )
    if lower in chat_phrases or any(lower.startswith(p) for p in ("hi ", "hey ", "thanks")):
        return {"intent": "CHAT", "confidence": 0.9}

    if "?" in text:
        return {"intent": "RETRIEVE", "confidence": 0.5}

    return {"intent": "CHAT", "confidence": 0.5}


def _classify_mock(text: str) -> dict:
    lower = text.strip().lower()
    if _looks_like_remember_command(text):
        return {"intent": "REMEMBER", "confidence": 0.95}
    if re.search(
        r"\b(do you remember|what(?:'s| is) my name|how old am i|what(?:'s| is) my age)\b",
        lower,
    ):
        return {"intent": "CHAT", "confidence": 0.9}
    if "?" in text or any(w in lower for w in ("policy", "look up", "find")):
        return {"intent": "RETRIEVE", "confidence": 0.9}
    return {"intent": "CHAT", "confidence": 0.9}


def _strip_remember_prefix(text: str) -> str:
    """Remove 'remember this:' style prefixes so we keep the payload."""
    cleaned = text.strip()
    patterns = (
        r"^(?:please\s+)?remember\s+(?:this|that)\s+for\s+(?:future\s+)?(?:conversations?\s+)?(?:in\s+the\s+future\s+)?[:,\-]?\s*",
        r"^(?:please\s+)?remember\s+(?:this|that)\s+for\s+(?:later|the\s+future)\s*[:,\-]?\s*",
        r"^(?:please\s+)?remember\s+(?:this|that)\s*[:,\-]?\s*",
        r"^(?:please\s+)?(?:don't|do not)\s+forget\s*(?:that\s+)?[:,\-]?\s*",
        r"^(?:please\s+)?(?:keep in mind|save this|store this)\s*[:,\-]?\s*",
        r"^(?:please\s+)?remember\s+",
    )
    for pattern in patterns:
        updated = re.sub(pattern, "", cleaned, count=1, flags=re.IGNORECASE).strip()
        if updated != cleaned:
            cleaned = updated
            break
    return cleaned.strip(" \t\"'")


def _extract_remember_with_rules(text: str, history: list[dict]) -> list[dict]:
    payload = _strip_remember_prefix(text)
    if not payload or payload.lower() in {"this", "that", "it"}:
        # "remember this" with no body → use the previous user message.
        for turn in reversed(history):
            if turn["role"] == "user" and turn["content"].strip() != text.strip():
                payload = turn["content"].strip()
                break

    if not payload:
        return []

    # Common structured forms.
    age = re.search(
        r"\b(?:my\s+)?age\s+is\s+(\d{1,3})\b|"
        r"\bi(?:'m| am)\s+(\d{1,3})\s*(?:years?\s+old)?\b",
        payload,
        flags=re.IGNORECASE,
    )
    if age:
        value = age.group(1) or age.group(2)
        return [{"key": "age", "value": value, "priority": "high"}]

    name = re.search(
        r"\b(?:my name is|i'm|i am|call me)\s+([A-Za-z][A-Za-z'\-]{1,30})\b",
        payload,
        flags=re.IGNORECASE,
    )
    if name:
        return [
            {
                "key": "name",
                "value": name.group(1).capitalize(),
                "priority": "high",
            }
        ]

    named = re.search(
        r"\b(?:my\s+)?([A-Za-z][A-Za-z0-9_\s]{1,30}?)\s+(?:is named|is called)\s+(.+)$",
        payload,
        flags=re.IGNORECASE,
    )
    if named:
        key = named.group(1).strip().lower().replace(" ", "_")
        value = named.group(2).strip().strip(".!")
        if key and value:
            return [{"key": key, "value": value, "priority": "high"}]

    keyed = re.search(
        r"^(?:that\s+)?(?:my\s+)?([A-Za-z][A-Za-z0-9_\s]{1,40}?)\s+is\s+(.+)$",
        payload,
        flags=re.IGNORECASE,
    )
    if keyed:
        key = keyed.group(1).strip().lower().replace(" ", "_")
        value = keyed.group(2).strip().strip(".!")
        if key and value and key not in {"it", "this", "that"}:
            return [{"key": key, "value": value, "priority": "high"}]

    # Fallback: store the whole thing under a generic note key.
    note = payload.strip().strip(".!")
    if len(note) > 180:
        note = note[:177] + "..."
    return [{"key": "note", "value": note, "priority": "high"}]


def _extract_remember_with_llm(text: str, history: list[dict]) -> list[dict]:
    analysis = _analyze_remember_with_llm(text, history, [])
    if analysis["mode"] == "store":
        return analysis["store_facts"]
    return []


def _normalize_fact_list(items: list, *, priority: str = "high") -> list[dict]:
    junk_keys = {
        "message",
        "msg",
        "text",
        "question",
        "snake_case",
        "key",
        "value",
        "short",
        "short_value",
    }
    facts = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip().lower().replace(" ", "_")
        value = str(item.get("value", "")).strip()
        if not key or not value or key in junk_keys:
            continue
        if value.lower() in {"short", "short value", "snake_case"}:
            continue
        if "said about" in value.lower():
            continue
        facts.append({"key": key, "value": value, "priority": priority})
    return facts


def _analyze_remember_with_llm(
    text: str,
    history: list[dict],
    existing_facts: list[dict],
) -> dict:
    recent = history[-10:]
    context = "\n".join(f"{turn['role']}: {turn['content']}" for turn in recent)
    known = "\n".join(
        f"- {fact['key']}: {fact['value']}" for fact in existing_facts
    ) or "(none)"

    prompt = f"""Michelle already classified this as related to memory. Dig deeper.
Ignore unrelated document-lookup turns in Recent conversation. Use THIS user message.

Step 1 — is_question: true if the user is asking / checking whether you remember
  something already said or stored. false if they are commanding you to store
  something new, however they word that request.

Step 2 — mode:
- recall: they are asking about something already discussed or already in Known facts.
  Fill matched_facts with the relevant Known facts (and/or facts clearly in Recent conversation
  that match Known facts). Do NOT invent new store_facts.
- store: they want a NEW durable fact saved. Fill store_facts. matched_facts can be empty.
- unclear: not enough info.

Examples:
- "remember what I said about your responses?" → is_question=true, mode=recall
  (matched_facts from Known, e.g. reply_style: shorter replies)
- "keep in mind that I prefer short replies" → is_question=false, mode=store
- "what's my name?" is usually not this path, but if seen → recall on name

Known long-term facts:
{known}

Recent conversation:
{context or "(none)"}

User message: {text}

Reply with ONLY valid JSON, no markdown. Use real keys/values from the message
(e.g. key "favorite_color" value "blue"), never placeholder text like "snake_case".
Example shape:
{{"is_question": false, "mode": "store", "matched_facts": [], "store_facts": [{{"key": "favorite_color", "value": "blue"}}]}}"""

    result = _llm_json(prompt)
    is_question = bool(result.get("is_question", False))
    mode = str(result.get("mode", "unclear")).strip().lower()
    if mode not in ("recall", "store", "unclear"):
        mode = "unclear"

    matched = _normalize_fact_list(result.get("matched_facts") or [])
    # Prefer canonical values from DB when keys match.
    by_key = {f["key"]: f for f in existing_facts}
    resolved_matched = []
    for fact in matched:
        if fact["key"] in by_key:
            resolved_matched.append(
                {
                    "key": fact["key"],
                    "value": by_key[fact["key"]]["value"],
                    "priority": "high",
                }
            )
        else:
            resolved_matched.append(fact)

    store_facts = _normalize_fact_list(result.get("store_facts") or [])

    # If model said recall but forgot to fill matched_facts, try keyword overlap.
    if mode == "recall" and not resolved_matched and existing_facts:
        resolved_matched = _match_facts_by_keywords(text, existing_facts)

    if is_question and mode == "store" and resolved_matched and not store_facts:
        mode = "recall"

    return {
        "is_question": is_question,
        "mode": mode,
        "matched_facts": resolved_matched,
        "store_facts": store_facts if mode == "store" else [],
    }


def _match_facts_by_keywords(text: str, existing_facts: list[dict]) -> list[dict]:
    lower = text.lower()
    hits = []
    for fact in existing_facts:
        key = fact["key"].replace("_", " ")
        value = str(fact["value"]).lower()
        if key in lower or any(tok in lower for tok in value.split() if len(tok) > 3):
            hits.append(
                {"key": fact["key"], "value": fact["value"], "priority": "high"}
            )
        elif fact["key"] in ("reply_style", "preference") and any(
            w in lower for w in ("reply", "replies", "response", "responses", "short")
        ):
            hits.append(
                {"key": fact["key"], "value": fact["value"], "priority": "high"}
            )
    return hits


def _analyze_remember_with_rules(
    text: str,
    history: list[dict],
    existing_facts: list[dict],
) -> dict:
    lower = text.strip().lower()
    is_question = "?" in text or bool(
        re.search(r"\b(what i said|do you remember|you remember|remember what)\b", lower)
    )
    if is_question:
        matched = _match_facts_by_keywords(text, existing_facts)
        return {
            "is_question": True,
            "mode": "recall" if matched else "unclear",
            "matched_facts": matched,
            "store_facts": [],
        }
    store_facts = _extract_remember_with_rules(text, history)
    return {
        "is_question": False,
        "mode": "store" if store_facts else "unclear",
        "matched_facts": [],
        "store_facts": store_facts,
    }


def _priority_rank(priority: str) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get(priority, 0)


def _normalize_memory_result(result: dict) -> dict:
    confidence = float(result.get("confidence", 0.0))
    confidence = max(0.0, min(1.0, confidence))

    overall_priority = str(result.get("priority", "low")).strip().lower()
    if overall_priority not in PRIORITIES:
        overall_priority = "low"

    facts = []
    for item in result.get("facts") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip().lower().replace(" ", "_")
        value = str(item.get("value", "")).strip()
        item_priority = str(item.get("priority", overall_priority)).strip().lower()
        if item_priority not in PRIORITIES:
            item_priority = overall_priority
        if key and value:
            facts.append({"key": key, "value": value, "priority": item_priority})

    # Auto-save only high + strong confidence.
    meets_priority = _priority_rank(overall_priority) >= _priority_rank(MEMORY_MIN_PRIORITY)
    high_facts = [
        fact
        for fact in facts
        if _priority_rank(fact["priority"]) >= _priority_rank(MEMORY_MIN_PRIORITY)
    ]
    important = (
        bool(result.get("important", False))
        and confidence >= MEMORY_SAVE_THRESHOLD
        and meets_priority
        and bool(high_facts)
    )

    # Unsure / medium → ask the user instead of silently saving or dropping.
    llm_ask = bool(result.get("ask_user", False))
    borderline_high = (
        overall_priority == "high"
        and bool(facts)
        and not important
        and confidence >= 0.55
    )
    medium_candidate = overall_priority == "medium" and bool(facts) and confidence >= 0.55
    ask_user = (not important) and (llm_ask or borderline_high or medium_candidate)

    if important:
        out_facts = high_facts
    elif ask_user:
        out_facts = facts
    else:
        out_facts = []

    return {
        "important": important,
        "ask_user": ask_user,
        "confidence": confidence,
        "priority": overall_priority,
        "facts": out_facts,
    }


def _assess_memory_with_llm(
    text: str,
    history: list[dict],
    existing_facts: list[dict],
) -> dict:
    recent = history[-4:]
    context = "\n".join(f"{turn['role']}: {turn['content']}" for turn in recent)
    known = "\n".join(
        f"- {fact['key']}: {fact['value']}" for fact in existing_facts
    ) or "(none)"

    prompt = f"""You score whether this user message should enter LONG-TERM memory.
Default to NOT saving. Most messages are temporary chat and must stay out.

Priority levels (pick one overall priority for the message):
- high: durable identity the assistant must never forget
  (name, preferred name, pronouns, hometown/city, job/company, age if clearly stated)
- medium: lasting preferences about how Michelle should behave — ALWAYS set ask_user=true
  Examples of medium (ask first, do NOT mark low):
  "keep responses shorter", "I'd prefer shorter replies", "be more casual",
  "don't use bullet points", "talk like a friend", hobbies they clearly state as lasting
- low: temporary / chat fluff — never save, ask_user=false
  (mood, jokes, one-off "sorry", "lol", weather)

Set important=true ONLY when priority is "high" AND you are very sure (auto-save).
Set ask_user=true when priority is medium OR when there is a candidate fact but you are unsure.
If Michelle just asked for their name and they reply with a name, important=true, priority=high.
CRITICAL: communication-style preferences (short replies, tone, formality) are medium + ask_user=true,
never low.

DO NOT SAVE and ask_user=false for:
- greetings, small talk, thanks, goodbye
- one-off questions, commands, jokes
- temporary mood / weather / "I'm busy right now"
- secrets (passwords, API keys, credit cards, SSN)
- anything already in Known facts unless the user is clearly correcting it

Known facts already saved:
{known}

Recent conversation:
{context or "(none)"}

User message: {text}

Reply with ONLY valid JSON, no markdown:
{{"important": true|false, "ask_user": true|false, "priority": "high"|"medium"|"low", "confidence": 0.0 to 1.0, "facts": [{{"key": "snake_case", "value": "short value", "priority": "high"|"medium"|"low"}}]}}
If nothing to store, empty facts, priority "low", ask_user false."""

    return _normalize_memory_result(_llm_json(prompt))


def _assistant_just_asked_for_name(history: list[dict]) -> bool:
    for turn in reversed(history):
        if turn["role"] != "assistant":
            continue
        lower = turn["content"].lower()
        return "name" in lower and ("what" in lower or "what's" in lower or "whats" in lower)
    return False


def capture_introduced_name(text: str, history: Optional[list[dict]] = None) -> str | None:
    """If Michelle just asked for a name, return the user's name (never 'Michelle')."""
    history = history or []
    if not _assistant_just_asked_for_name(history):
        return None

    stripped = text.strip()
    named = re.search(
        r"\b(?:my name is|i'm|i am|call me)\s+([A-Za-z][A-Za-z'\-]{1,30})\b",
        stripped,
        flags=re.IGNORECASE,
    )
    if named:
        candidate = named.group(1)
    else:
        bare = re.fullmatch(r"([A-Za-z][A-Za-z'\-]{1,30})", stripped)
        candidate = bare.group(1) if bare else None

    if not candidate:
        return None
    if candidate.lower() in BLOCKED_NAME_VALUES:
        return None
    return " ".join(part.capitalize() for part in candidate.split())


def _extract_rule_facts(text: str, history: Optional[list[dict]] = None) -> list[dict]:
    """Lightweight pattern extractors used by rules + mock modes."""
    history = history or []
    stripped = text.strip()
    facts: list[dict] = []

    name_match = re.search(
        r"\b(?:my name is|i'm|i am|call me)\s+([A-Za-z][A-Za-z'\-]{1,30})\b",
        stripped,
        flags=re.IGNORECASE,
    )
    if name_match:
        value = name_match.group(1).capitalize()
        if value.lower() not in BLOCKED_NAME_VALUES:
            facts.append(
                {
                    "key": "name",
                    "value": value,
                    "priority": "high",
                }
            )
    elif _assistant_just_asked_for_name(history):
        # Bare reply like "Nathan" after Michelle asked for a name.
        bare = re.fullmatch(r"([A-Za-z][A-Za-z'\-]{1,30})", stripped)
        if bare and bare.group(1).lower() not in BLOCKED_NAME_VALUES:
            facts.append(
                {
                    "key": "name",
                    "value": bare.group(1).capitalize(),
                    "priority": "high",
                }
            )

    live_match = re.search(
        r"\b(?:i live in|i'm based in|i am based in|i'm from|i am from)\s+([^,.!?]+)",
        stripped,
        flags=re.IGNORECASE,
    )
    if live_match:
        facts.append(
            {
                "key": "location",
                "value": live_match.group(1).strip().title(),
                "priority": "high",
            }
        )

    work_match = re.search(
        r"\b(?:i work (?:at|for)|i'm a|i am a)\s+([^,.!?]+)",
        stripped,
        flags=re.IGNORECASE,
    )
    if work_match:
        facts.append(
            {
                "key": "work",
                "value": work_match.group(1).strip(),
                "priority": "high",
            }
        )

    return facts


def _assess_memory_with_rules(text: str, history: Optional[list[dict]] = None) -> dict:
    facts = _extract_rule_facts(text, history)
    if facts:
        return _normalize_memory_result(
            {
                "important": True,
                "ask_user": False,
                "priority": "high",
                "confidence": 0.9,
                "facts": facts,
            }
        )
    # Soft preferences / reply-style asks → ask before saving.
    lower = text.strip().lower()
    style = re.search(
        r"\b(?:prefer|like|want|keep)\b.*\b(?:short(?:er)?|brief|concise|long(?:er)?|"
        r"casual|formal|bullet)\b",
        lower,
    )
    if style or re.search(r"\bresponses?\b.*\bshort", lower) or re.search(
        r"\bshort(?:er)?\b.*\bresponses?\b", lower
    ):
        return _normalize_memory_result(
            {
                "important": False,
                "ask_user": True,
                "priority": "medium",
                "confidence": 0.75,
                "facts": [
                    {
                        "key": "reply_style",
                        "value": "shorter replies"
                        if "short" in lower or "brief" in lower or "concise" in lower
                        else text.strip()[:80],
                        "priority": "medium",
                    }
                ],
            }
        )

    prefer = re.search(
        r"\bi(?:'d| would)?(?:\s+\w+){0,3}\s+(?:prefer|like|love)\s+([^,.!?]+)",
        text.strip(),
        flags=re.IGNORECASE,
    )
    if prefer:
        return _normalize_memory_result(
            {
                "important": False,
                "ask_user": True,
                "priority": "medium",
                "confidence": 0.7,
                "facts": [
                    {
                        "key": "preference",
                        "value": prefer.group(1).strip(),
                        "priority": "medium",
                    }
                ],
            }
        )
    return {
        "important": False,
        "ask_user": False,
        "confidence": 0.2,
        "priority": "low",
        "facts": [],
    }


def _assess_memory_mock(text: str, history: Optional[list[dict]] = None) -> dict:
    return _assess_memory_with_rules(text, history)


if __name__ == "__main__":
    samples = [
        "hey!",
        "what's our refund policy?",
        "create a ticket for billing",
        "thanks Michelle",
        "My name is Nathan and I live in Seattle",
        "Nathan",
        "remember this: my age is 22",
        "remember that I prefer short replies",
        "do you remember my name?",
    ]
    asked = [{"role": "assistant", "content": "Hey — I'm Michelle. What's your name?"}]
    for msg in samples:
        hist = asked if msg == "Nathan" else []
        intent = classify_intent(msg)
        print(msg, "->", intent)
        if intent["intent"] == "REMEMBER":
            print("   facts:", extract_remember_facts(msg, hist))
        else:
            print("   auto:", assess_memory_worthiness(msg, hist))
