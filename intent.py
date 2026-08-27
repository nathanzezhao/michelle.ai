import json
import os
import re
from pathlib import Path
from typing import Optional

import httpx
from google import genai

from actions import ACTION_WHITELIST
from long_term_memory import is_valid_name

INTENTS = ("CHAT", "RETRIEVE", "REMEMBER", "ACTION")
PRIORITIES = ("high", "medium", "low")

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


def _intent_llm_on() -> bool:
    mode = os.getenv("INTENT_MODE", "llm").lower()
    return mode not in ("rules", "mock") and _intent_backend() != "rules"


# Shared instruction for every follow-up classifier. Rules still win on
# exact phrases; the model covers typos, slang, and figures of speech.
_SLOPPY_REPLY_HINT = (
    "The user types loosely: typos, slang, and figures of speech. "
    "Read the meaning, not the spelling. yeha=yes, naw=no, nvmind=nevermind, "
    "snd an emial=send an email, bet/for sure=yes, I'm good=abandoning."
)


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


def classify_intent(
    text: str,
    history: Optional[list[dict]] = None,
    existing_facts: Optional[list[dict]] = None,
) -> dict:
    """Classify via a question-first tree, then a typed bucket.

    1. Is this a question? (meaning, not punctuation)
    2. If yes: what kind — REMEMBER (recall), RETRIEVE (docs), or CHAT
    3. If no: CHAT, REMEMBER (store), RETRIEVE (imperative lookup), or ACTION
       (an order to do a task — open an app, send an email).
    Orders ("look this up", "retrieve this") are not questions.
    """
    history = history or []
    existing_facts = existing_facts or []
    mode = os.getenv("INTENT_MODE", "llm").lower()

    # Bare name after "what's your name?" is always chat, never a doc lookup.
    if capture_introduced_name(text, history):
        return {
            "intent": "CHAT",
            "confidence": 0.95,
            "is_question": False,
            "kind": "CHAT",
        }

    if mode == "rules":
        result = _normalize_intent_result(_classify_with_rules(text), text)
    elif mode == "mock":
        result = _normalize_intent_result(_classify_mock(text), text)
    elif _intent_backend() == "rules":
        result = _normalize_intent_result(_classify_with_rules(text), text)
    else:
        try:
            result = _normalize_intent_result(
                _classify_with_llm(text, history, existing_facts), text
            )
        except Exception as e:
            print(f"Intent LLM failed ({e}), falling back to rules")
            result = _normalize_intent_result(_classify_with_rules(text), text)
        else:
            # llama3.2 often labels "open Notes" as CHAT. If the utterance is
            # clearly an order, promote — same idea as maybe_promote_to_remember.
            # CHAT only: a docs question stays RETRIEVE unless it's a draft reopen.
            if result.get("intent") == "CHAT" and _looks_like_action_order(text):
                print("intent_promoted=ACTION (classifier missed an order)")
                result["intent"] = "ACTION"
                result["kind"] = "ACTION"

    if result.get("intent") in ("CHAT", "RETRIEVE", "REMEMBER") and _looks_like_resume_draft(text):
        print("intent_promoted=ACTION (draft resume)")
        result["intent"] = "ACTION"
        result["kind"] = "ACTION"
    return result


def _normalize_intent_result(result: dict, text: str) -> dict:
    """Ensure every classify path exposes the question-first tree fields."""
    out = dict(result)
    out.setdefault("is_question", _looks_like_question(text))
    out.setdefault("kind", out.get("intent") or "CHAT")
    out.setdefault("memory_score", 0.0)
    out.setdefault("docs_score", 0.0)
    out.setdefault("chat_score", 0.0)
    return out


def _confirmation_with_rules(text: str) -> str | None:
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


def classify_memory_confirmation(text: str) -> str | None:
    """Yes/no for memory ask, save-draft ask, or typed reply at Confirm.

    Exact phrases use rules. In INTENT_MODE=llm, anything else goes through
    the model so typos and slang still count.
    """
    ruled = _confirmation_with_rules(text)
    if ruled is not None:
        return ruled
    if not _intent_llm_on():
        return None
    try:
        result = _llm_json(
            f"{_SLOPPY_REPLY_HINT}\n"
            "Michelle asked a yes/no question. Is this message yes, no, or "
            "something else (a new topic, a question, filling a form)?\n"
            f"User message: {text}\n"
            'Reply with ONLY JSON: {"answer": "yes"|"no"|"other"}'
        )
        ans = str((result or {}).get("answer") or "").strip().lower()
        if ans in ("yes", "no"):
            return ans
        return None
    except Exception as e:
        print(f"confirmation LLM failed ({e})")
        return None


_COMPOSER_DISMISS_RE = re.compile(
    r"^\s*(?:(?:please|pls|yeah|nah|ok|okay|um+|uh)\s+)?"
    r"(?:never\s*mind|nvm|forget\s+(?:it|that|this)|scratch\s+that|"
    r"don'?t\s+(?:send|bother)(?:\s+it)?|do\s+not\s+send|"
    r"cancel\s+(?:that|it|the\s+email)|not\s+now|"
    r"stop(?:\s+that)?)"
    r"(?:\s+(?:lol|thanks|thx|then|for\s+now))*[\s.!?]*$",
    re.IGNORECASE,
)


def _looks_like_composer_dismiss(text: str) -> bool:
    """Deterministic nevermind / scratch-that — LLM covers similar phrasing."""
    return bool(_COMPOSER_DISMISS_RE.match((text or "").strip()))


def classify_composer_dismiss(text: str) -> bool:
    """True when the user is abandoning the open email composer.

    Obvious phrases use rules. Anything vaguer goes to the same LLM as intent
    when INTENT_MODE=llm — no draft is stored either way.
    """
    stripped = (text or "").strip()
    if not stripped:
        return False
    if _looks_like_composer_dismiss(stripped):
        return True
    if _extract_email_params(stripped):
        return False
    if not _intent_llm_on():
        return False
    try:
        result = _llm_json(
            f"{_SLOPPY_REPLY_HINT}\n"
            "The user has an email composer open. Is this message them "
            "abandoning the email (nevermind, forget it, don't send, not now, "
            "scratch that, I'm good, don't bother) rather than filling to / "
            "subject / body or talking about something else?\n"
            f"Message: {stripped}\n"
            'Reply with ONLY JSON: {"dismiss": true|false}'
        )
        return bool(isinstance(result, dict) and result.get("dismiss"))
    except Exception as e:
        print(f"composer dismiss LLM failed ({e})")
        return False


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


# --- ACTION param extraction (SPEC-PIPELINE §3.2) ---------------------------

_EMAIL_ADDR_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Leading chatter is allowed; app names are NEVER a closed list — whatever
# they said after the verb is the app (including slang / gibberish names).
_LEADING_FILLER = (
    r"(?:(?:yo|hey|hi|sup|bruh|pls|please|like|ok|okay|uhm|uh)\s+)*"
    r"(?:(?:can|could|would|will)\s+(?:you|u|ya)\s+)?"
    r"(?:(?:please|pls|gonna|wanna|go\s+ahead\s+and)\s+)?"
)
_OPEN_VERBS = (
    r"(?:open|launch|start|run|pull\s+up|fire\s+up|hop\s+(?:in(?:to)?|on)|"
    r"bring\s+up|boot\s+up|pop\s+open|crank\s+up)"
)
# First/last letter pinned like _EMAIL_WORD. close/clsoe/cloes; quit/quti/qiut.
# "quite" is ordinary English — do not treat it as quit.
_CLOSE_WORD = r"(?:c[a-z]{3}e|cloes|closed|shut)"
_QUIT_WORD = r"(?:q[a-z]{2}t|quti|qiut|exit)"
_CLOSE_TAIL = rf"{_CLOSE_WORD}(?:\s+(?:the\s+)?windows?(?:\s+(?:of|for))?)?"
_TRAILING_FILLER = r"(?:\s+(?:rq|pls|please|thx|thanks|real\s+quick|for\s+me))*[.!?]*"

# "email" with first and last letters pinned (e … l). Middle can be a typo:
# emial, emauil, emaill, emal. Not "el"/"eel" (too short).
_EMAIL_WORD = r"e-?[a-z]{2,6}l"

_OPEN_APP_EXTRACT_RE = re.compile(
    rf"^{_LEADING_FILLER}{_OPEN_VERBS}(?:\s+up)?"
    rf"\s+(?:the\s+)?(.+?){_TRAILING_FILLER}$",
    re.IGNORECASE,
)
_CLOSE_APP_EXTRACT_RE = re.compile(
    rf"^{_LEADING_FILLER}{_CLOSE_TAIL}"
    rf"\s+(?:the\s+)?(.+?){_TRAILING_FILLER}$",
    re.IGNORECASE,
)
_QUIT_APP_EXTRACT_RE = re.compile(
    rf"^{_LEADING_FILLER}{_QUIT_WORD}"
    rf"\s+(?:the\s+)?(.+?){_TRAILING_FILLER}$",
    re.IGNORECASE,
)
_EMAIL_CMD_RE = re.compile(
    r"^(?:please\s+)?(?:can you\s+|could you\s+)?"
    rf"(?:send\s+(?:an?\s+)?{_EMAIL_WORD}\b|{_EMAIL_WORD})\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)
_SUBJECT_RE = re.compile(
    r"\bsubject\s*[:=]?\s*(.+?)(?=[,;]?\s+(?:and\s+)?body\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_BODY_RE = re.compile(r"\bbody\s*[:=]?\s*(.+)$", re.IGNORECASE | re.DOTALL)
_RECIPIENT_NAME_RE = re.compile(r"\bto\s+([A-Za-z][\w.'-]*)", re.IGNORECASE)

# Placeholder-ish app names that mean the user never said which app.
_GENERIC_APP_WORDS = {"app", "an app", "the app", "it", "that", "something", "a new app"}


def analyze_action_request(
    text: str,
    history: Optional[list[dict]] = None,
    task_context: Optional[dict] = None,
) -> dict:
    """After intent=ACTION (or while an action is AWAITING_INPUT): what task,
    with what params? Runs ONLY on those turns — no other turn pays for it.

    task_context is the open actions_log row ({action_type, resolved_params,
    missing_params}) when continuing a paused action; the analyzer then also
    decides related (supplying the missing info) vs. unrelated (topic change).

    Returns:
        {
            "action_type": whitelist key | "unsupported",
            "resolved_params": {...},   # grounded in THIS message (+ open task)
            "missing_params": [...],    # recomputed in code from the whitelist
            "confidence": float,
            "related": bool,            # meaningful when task_context given
        }
    """
    history = history or []
    mode = os.getenv("INTENT_MODE", "llm").lower()

    if mode in ("rules", "mock") or _intent_backend() == "rules":
        raw = _analyze_action_with_rules(text, task_context)
    else:
        try:
            raw = _analyze_action_with_llm(text, history, task_context)
        except Exception as e:
            print(f"Action analyze LLM failed ({e}), falling back to rules")
            raw = _analyze_action_with_rules(text, task_context)
        else:
            # llama often returns open_app with empty resolved_params, or
            # related=false on a bare app name. Fill gaps from the rules
            # extractor — never invent values, only recover what the user typed.
            raw = _fill_action_from_rules(
                raw, _analyze_action_with_rules(text, task_context)
            )

    return _finalize_action_analysis(raw, text, history, task_context)


def _params_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _fill_action_from_rules(raw: dict, rules: dict) -> dict:
    """Merge deterministic extracts into a flaky LLM analyzer result."""
    out = dict(raw) if isinstance(raw, dict) else {}
    rules = rules if isinstance(rules, dict) else {}
    out["resolved_params"] = _params_dict(out.get("resolved_params"))
    raw_type = str(out.get("action_type") or "").strip().lower()
    rules_type = str(rules.get("action_type") or "").strip().lower()
    if rules_type in ACTION_WHITELIST and (
        raw_type not in ACTION_WHITELIST or rules_type != raw_type
    ):
        # llama often labels "send an email …" as open_app. If rules matched a
        # real verb, that type wins.
        out["action_type"] = rules_type
        raw_type = rules_type
        allowed = set(ACTION_WHITELIST[rules_type]["required_params"])
        resolved = {
            k: v
            for k, v in _params_dict(out.get("resolved_params")).items()
            if k in allowed
        }
        out["resolved_params"] = resolved

    resolved = dict(_params_dict(out.get("resolved_params")))
    if not str(resolved.get("app_name") or "").strip() and _param_empty(
        resolved.get("app_names")
    ):
        for alias in ("app", "application", "name", "appName"):
            if str(resolved.get(alias) or "").strip():
                resolved["app_name"] = str(resolved[alias]).strip()
                break
    for key, value in _params_dict(rules.get("resolved_params")).items():
        if _param_empty(resolved.get(key)) and not _param_empty(value):
            resolved[key] = value
    out["resolved_params"] = _coerce_app_params(raw_type, resolved)
    if rules.get("related"):
        out["related"] = True
        if raw_type not in ACTION_WHITELIST and rules_type in ACTION_WHITELIST:
            out["action_type"] = rules_type
    if rules.get("dismiss"):
        out["dismiss"] = True
        out["related"] = False
    rules_new_send = rules_type == "send_email" and not rules.get("resume")
    if rules.get("resume") or (out.get("resume") and not rules_new_send):
        out["resume"] = True
        out["action_type"] = "send_email"
        query = str(out.get("resume_query") or rules.get("resume_query") or "").strip()
        out["resume_query"] = query
        out["resolved_params"] = {}
    elif rules_new_send:
        out["resume"] = False
        out["resume_query"] = ""
    return out


def _finalize_action_analysis(
    raw: dict,
    text: str,
    history: list[dict],
    task_context: Optional[dict],
) -> dict:
    """Code-side validation (§3.2): the LLM never gets to invent an executable
    action type, and params must be grounded in the user's own words."""
    action_type = str(raw.get("action_type") or "").strip().lower()
    related = bool(raw.get("related", False))
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.5) or 0.5)))
    except (TypeError, ValueError):
        confidence = 0.5

    prior_params: dict = {}
    if task_context and related:
        # Continuations keep the stored task's type; prior params were already
        # grounded when they were first saved.
        if action_type in ("", "unsupported", "none", "null"):
            action_type = str(task_context.get("action_type") or "")
        if action_type == task_context.get("action_type"):
            prior_params = dict(task_context.get("resolved_params") or {})
        else:
            prior_params = {}

    # Whitelist validation happens HERE, in code, after the model call.
    if action_type not in ACTION_WHITELIST:
        return {
            "action_type": "unsupported",
            "resolved_params": {},
            "missing_params": [],
            "confidence": confidence,
            "related": False,
        }

    # This utterance only. Chat history is how the last sent email leaked
    # back in on reload ("send an email" → Confirm the old one again).
    incoming = _coerce_app_params(
        action_type, _params_dict(raw.get("resolved_params"))
    )
    grounded = _ground_action_params(incoming, text)
    prior_params = _coerce_app_params(action_type, prior_params)
    resolved = {**prior_params, **grounded}
    required = ACTION_WHITELIST[action_type]["required_params"]
    resolved = {k: v for k, v in resolved.items() if k in required}
    missing = [p for p in required if _param_empty(resolved.get(p))]

    dismiss = False
    if task_context and task_context.get("action_type") == "send_email":
        dismiss = bool(raw.get("dismiss")) or _looks_like_composer_dismiss(text)
        if dismiss and _extract_email_params(text) and not _looks_like_composer_dismiss(text):
            dismiss = False
        if dismiss:
            related = False

    resume = bool(raw.get("resume"))
    resume_query = str(raw.get("resume_query") or "").strip()
    if resume:
        resolved = {}
        missing = list(required)

    return {
        "action_type": action_type,
        "resolved_params": resolved,
        "missing_params": missing,
        "confidence": confidence,
        "related": related if task_context else True,
        "dismiss": dismiss,
        "resume": resume,
        "resume_query": resume_query or (text if resume else ""),
    }


def _ground_action_params(params, text: str, history: Optional[list[dict]] = None) -> dict:
    """Same spirit as _fact_supported_by_text: the extractor must not invent
    an email address, subject, or body the user never typed in THIS message.

    history is ignored. Prior emails live in the thread after reload; using
    them as a grounding blob lets "send another" re-Confirm the last send.
    An open task keeps its own fields via task_context, not via chat history.
    """
    if not isinstance(params, dict):
        return {}
    del history  # accepted so older call sites don't break
    blob = (text or "").lower()
    grounded = {}
    # Full addresses actually present in the text — substring matching is not
    # enough for emails ("notalex@example.com" contains "alex@example.com",
    # a different mailbox).
    known_addresses = set(_EMAIL_ADDR_RE.findall(blob))
    for key, raw_value in params.items():
        if isinstance(raw_value, list):
            items = []
            for item in raw_value:
                kept = _ground_one_param(str(item or "").strip(), blob, known_addresses)
                if kept:
                    items.append(kept)
            if items:
                grounded[str(key)] = items
            continue
        value = str(raw_value or "").strip()
        kept = _ground_one_param(value, blob, known_addresses)
        if kept:
            grounded[str(key)] = kept
    return grounded


def _ground_one_param(value: str, blob: str, known_addresses: set) -> str:
    if not value:
        return ""
    if _EMAIL_ADDR_RE.fullmatch(value):
        return value if value.lower() in known_addresses else ""
    if value.lower() in blob:
        return value
    if "@" in value:
        # Email addresses must appear verbatim — never guessed.
        return ""
    tokens = [t for t in re.split(r"[^a-z0-9]+", value.lower()) if len(t) >= 3]
    if tokens and sum(1 for t in tokens if t in blob) * 2 >= len(tokens):
        return value
    return ""


def _extract_email_params(text: str) -> dict:
    params: dict = {}
    addr = _EMAIL_ADDR_RE.search(text)
    subject = _SUBJECT_RE.search(text)
    body = _BODY_RE.search(text)
    if addr:
        params["recipient"] = addr.group(0)
    else:
        named = _RECIPIENT_NAME_RE.search(text)
        if named and named.group(1).lower() not in {"the", "a", "an", "my"}:
            params["recipient"] = named.group(1)
    if subject:
        value = subject.group(1).strip().strip("\"'").strip()
        if value:
            params["subject"] = value
    if body:
        value = body.group(1).strip().strip("\"'").strip()
        if value:
            params["body"] = value
    return params


def _param_empty(value) -> bool:
    if isinstance(value, list):
        return not any(str(v or "").strip() for v in value)
    return not str(value or "").strip()


def _clean_app_name(raw: str) -> str:
    app = (raw or "").strip().strip("\"'").strip()
    app = re.sub(
        r"\s+(?:rq|pls|please|thx|thanks|real\s+quick|for\s+me)+$",
        "",
        app,
        flags=re.IGNORECASE,
    ).strip()
    app = re.sub(r"\s+app$", "", app, flags=re.IGNORECASE).strip()
    if app.lower() in _GENERIC_APP_WORDS:
        return ""
    return app


def _split_app_names(raw: str) -> list[str]:
    blob = _clean_app_name(raw)
    if not blob:
        return []
    parts = re.split(r"\s*(?:,|&|\band\b)\s*", blob, flags=re.IGNORECASE)
    names = []
    for part in parts:
        name = _clean_app_name(re.sub(r"^(?:the\s+)", "", part, flags=re.IGNORECASE))
        if name and name.lower() not in {"and", "also", "then", "plus"}:
            if name not in names:
                names.append(name)
    return names


def _coerce_app_params(action_type: str, resolved: dict) -> dict:
    out = dict(resolved or {})
    if action_type not in ("close_app", "quit_app"):
        return out
    names = out.get("app_names")
    if isinstance(names, str) and names.strip():
        names = _split_app_names(names)
    elif isinstance(names, list):
        collected = []
        for item in names:
            collected.extend(_split_app_names(str(item or "")))
        names = collected
    else:
        names = []
    if not names:
        one = str(out.get("app_name") or "").strip()
        if one:
            names = _split_app_names(one)
    out.pop("app_name", None)
    if names:
        out["app_names"] = names
    else:
        out.pop("app_names", None)
    return out


def _analyze_action_with_rules(text: str, task_context: Optional[dict]) -> dict:
    """Regex extractor for whitelist actions — offline QA parity (§3.2)."""
    stripped = (text or "").strip()

    if (
        task_context
        and task_context.get("action_type") == "send_email"
        and _looks_like_composer_dismiss(stripped)
    ):
        return {
            "action_type": "send_email",
            "resolved_params": {},
            "confidence": 0.95,
            "related": False,
            "dismiss": True,
        }

    if _looks_like_resume_draft(stripped):
        return {
            "action_type": "send_email",
            "resolved_params": {},
            "confidence": 0.85,
            "related": False,
            "resume": True,
            "resume_query": stripped,
        }

    close_match = _CLOSE_APP_EXTRACT_RE.match(stripped)
    if close_match and _ACTION_CLOSE_RE.match(stripped):
        names = _split_app_names(close_match.group(1))
        return {
            "action_type": "close_app",
            "resolved_params": {"app_names": names} if names else {},
            "confidence": 0.9,
            "related": bool(
                task_context and task_context.get("action_type") == "close_app"
            ),
        }

    quit_match = _QUIT_APP_EXTRACT_RE.match(stripped)
    if quit_match and _ACTION_QUIT_RE.match(stripped):
        names = _split_app_names(quit_match.group(1))
        return {
            "action_type": "quit_app",
            "resolved_params": {"app_names": names} if names else {},
            "confidence": 0.9,
            "related": bool(
                task_context and task_context.get("action_type") == "quit_app"
            ),
        }

    open_match = _OPEN_APP_EXTRACT_RE.match(stripped)
    if open_match and _ACTION_OPEN_RE.match(stripped):
        app = _clean_app_name(open_match.group(1))
        return {
            "action_type": "open_app",
            "resolved_params": {"app_name": app} if app else {},
            "confidence": 0.9,
            "related": bool(
                task_context and task_context.get("action_type") == "open_app"
            ),
        }

    if _ACTION_EMAIL_RE.match(stripped):
        return {
            "action_type": "send_email",
            "resolved_params": _extract_email_params(stripped),
            "confidence": 0.9,
            "related": bool(
                task_context and task_context.get("action_type") == "send_email"
            ),
        }

    if task_context:
        # Paused action: does this message supply the missing params?
        action_type = task_context.get("action_type")
        missing = task_context.get("missing_params") or []
        supplied: dict = {}
        if action_type == "send_email":
            supplied = {
                k: v
                for k, v in _extract_email_params(stripped).items()
                if k in missing
            }
        elif action_type == "open_app" and "app_name" in missing:
            bare = re.fullmatch(r"[A-Za-z][\w .'+-]{0,40}", stripped)
            if bare and not _looks_like_question(stripped):
                app = _clean_app_name(stripped)
                if app:
                    supplied = {"app_name": app}
        elif action_type in ("close_app", "quit_app") and "app_names" in missing:
            if not _looks_like_question(stripped):
                names = _split_app_names(stripped)
                if names and re.fullmatch(
                    r"[A-Za-z][\w .'+-]*(?:\s*(?:,|&|and)\s*[A-Za-z][\w .'+-]*)*",
                    stripped.strip().strip("\"'"),
                    re.IGNORECASE,
                ):
                    supplied = {"app_names": names}
        if supplied:
            return {
                "action_type": action_type,
                "resolved_params": supplied,
                "confidence": 0.85,
                "related": True,
            }
        return {
            "action_type": "unsupported",
            "resolved_params": {},
            "confidence": 0.7,
            "related": False,
        }

    return {
        "action_type": "unsupported",
        "resolved_params": {},
        "confidence": 0.6,
        "related": False,
    }


def _analyze_action_with_llm(
    text: str,
    history: list[dict],
    task_context: Optional[dict],
) -> dict:
    recent = history[-10:]
    context = "\n".join(f"{turn['role']}: {turn['content']}" for turn in recent)
    catalog = "\n".join(
        f'- "{name}": requires {", ".join(entry["required_params"])}'
        for name, entry in ACTION_WHITELIST.items()
    )
    task_block = ""
    if task_context:
        task_block = (
            "\nThe user was in the middle of this paused task:\n"
            f"- action_type: {task_context.get('action_type')}\n"
            f"- already provided: {json.dumps(task_context.get('resolved_params') or {})}\n"
            f"- still missing: {json.dumps(task_context.get('missing_params') or [])}\n"
            'Set "related": true ONLY if this message supplies missing info for that '
            "task or clearly continues it. If they changed topic, related is false.\n"
            'If they are abandoning the email (nevermind, nvm, forget it, don\'t send, '
            "scratch that, not now, I'm good — including typos), set \"dismiss\": true "
            "and related false.\n"
        )

    prompt = f"""The user gave Michelle an order to do a task. Extract the task.

{_SLOPPY_REPLY_HINT}

Supported action types (anything else → "unsupported"):
{catalog}
{task_block}
Rules:
- resolved_params may ONLY contain values the user stated in THIS User
  message. Do not copy to/subject/body from an earlier email in the
  conversation. "send an email" / "send another email" with no details →
  empty resolved_params. NEVER invent an email address, subject, body, or
  app name. If a required value was not given, list it in missing_params.
- A brand-new send-mail request (even with no details, even with typos) is
  send_email with resume=false. Do not copy an old letter's fields.
- If they want to reopen a letter they already stashed — that draft, finish
  that email, the one about X, "show me the draft about X" — set resume=true
  and resume_query to the describing words (e.g. "math tutor application").
  Still send_email. Do not invent to/subject/body.
- Opening/launching a program is open_app. Closing windows (app stays
  running) is close_app. Quitting/exiting a program is quit_app. Sending
  or finishing mail is never open_app, close_app, or quit_app.
- close_app and quit_app take resolved_params.app_names as an array of
  the app names the user said (one or more). "close Notes and Safari" →
  close_app with app_names ["Notes","Safari"]. Do not invent names.
- "close the draft" / "close that email" is resume send_email, not close_app.
- "unsupported" for any task that is not exactly one of the supported types
  (booking, deleting files, reminders, browsing, etc.).

Recent conversation:
{context or "(none)"}

User message: {text}

Reply with ONLY valid JSON, no markdown:
{{"action_type": "open_app"|"close_app"|"quit_app"|"send_email"|"unsupported", "resume": false, "resume_query": "", "related": true|false, "dismiss": false, "resolved_params": {{}}, "missing_params": [], "confidence": 0.0 to 1.0}}"""

    result = _llm_json(prompt)
    if not isinstance(result, dict):
        raise ValueError("action analyzer returned non-object JSON")
    return result


def _looks_like_remember_command(text: str) -> bool:
    lower = text.strip().lower()
    # Questions about memory / reminders to do a task are not REMEMBER saves.
    if "remind me" in lower:
        return False
    # "hey rmbr my name?" is recall, not a store command.
    if _looks_like_question(text) and re.search(r"\b(remember|rmbr|recall)\b", lower):
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
    is_question: bool | None = None,
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
    question = (
        is_question if is_question is not None else _looks_like_question(text)
    )
    mode = os.getenv("INTENT_MODE", "llm").lower()

    if mode == "rules" or mode == "mock" or _intent_backend() == "rules":
        if mode == "mock":
            result = _assess_memory_mock(text, history)
        else:
            result = _assess_memory_with_rules(text, history)
        if question:
            return {
                "important": False,
                "ask_user": False,
                "confidence": result.get("confidence", 0.0),
                "priority": "low",
                "facts": [],
            }
        return result

    try:
        result = _assess_memory_with_llm(text, history, existing_facts)
        # Local models sometimes mark style prefs as low — recover with rules.
        if not result["important"] and not result.get("ask_user"):
            rules = _assess_memory_with_rules(text, history)
            if rules.get("ask_user") and rules.get("facts"):
                result = rules
    except Exception as e:
        print(f"Memory assessor LLM failed ({e}), falling back to rules")
        result = _assess_memory_with_rules(text, history)

    if question:
        return {
            "important": False,
            "ask_user": False,
            "confidence": result.get("confidence", 0.0),
            "priority": "low",
            "facts": [],
        }
    return result


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


def _memory_shelf(existing_facts: list[dict]) -> str:
    if not existing_facts:
        return "(empty — nothing saved about this user yet)"
    return "\n".join(
        f"- {fact['key']}: {fact['value']}" for fact in existing_facts
    )


def _docs_shelf() -> str:
    folder = Path(os.getenv("MICHELLE_DOCS_DIR", "docs"))
    if not folder.is_dir():
        return "(empty)"
    names = sorted(
        path.name
        for path in folder.iterdir()
        if path.suffix.lower() in {".md", ".txt", ".markdown"}
    )
    if not names:
        return "(empty)"
    return "\n".join(f"- {name}" for name in names)


def _classify_with_llm(
    text: str,
    history: list[dict],
    existing_facts: Optional[list[dict]] = None,
) -> dict:
    # Prior turns unused: llama3.2 copies the last label and misses this turn.
    _ = history
    existing_facts = existing_facts or []
    memory_shelf = _memory_shelf(existing_facts)
    docs_shelf = _docs_shelf()

    prompt = f"""You are routing one message through Michelle's library.

{_SLOPPY_REPLY_HINT}

Step 1 — is_question (meaning, not punctuation or wording):
true = they want an answer.
false = telling, greeting, acknowledging, or an order/command.
Orders are not questions even with a "?".
Reopening a mailbox draft is ACTION even if phrased as a question
("show me the draft about X", "can you open that email", "get me the draft").
That is not RETRIEVE and not a docs lookup.

Step 2 — if is_question is true, SCORE the two libraries plus chat.
Do not jump to docs because the wording looks like "what is …", or because
one shelf is empty. An empty shelf is not a reason to pick the other one.
Weigh which shelf the ANSWER belongs on.

Memory library (personal facts about this user):
{memory_shelf}
Score memory_score 0-1: the answer is about this person — identity, prefs,
things they told her. Stay high even if that card is not filed yet.

Docs library (shared knowledge files; she may write into these later):
{docs_shelf}
Score docs_score 0-1: the answer lives in those files (policy, product,
handbook, company knowledge). Stay low if the question is about the user.

Score chat_score 0-1: social / opinion / neither library.

Pick kind as the highest of those three: REMEMBER, RETRIEVE, or CHAT.
Exception: showing/opening/finding a mailbox draft or email draft is ACTION,
not RETRIEVE, even when the message is a question.

Step 2b — if is_question is false, set kind without the scores:
  REMEMBER = keep a personal fact/pref
  RETRIEVE = order a doc lookup
  CHAT = greetings, small talk, acknowledgements — NEVER an order to open an app
  ACTION = they want a task done on the computer or an external service.
    Opening/launching ANY app is ACTION, including slang and made-up names:
    "open Notes", "yo pull up chrome", "fire up vs code rq", "hop into discord",
    "pls launch xyzzyqorp". Sending email is ACTION. Not a memory instruction,
    not a doc lookup. App names are whatever they said — there is no fixed list.
Set unused scores to 0.

User message: {text}

Reply with ONLY valid JSON, no markdown:
{{"is_question": true|false, "memory_score": 0.0, "docs_score": 0.0, "chat_score": 0.0, "kind": "CHAT"|"RETRIEVE"|"REMEMBER"|"ACTION", "confidence": 0.0 to 1.0}}"""

    result = _llm_json(prompt)
    is_question = _json_bool(result.get("is_question"))
    kind = str(result.get("kind") or "").strip().upper()

    def _score(key: str) -> float:
        try:
            return max(0.0, min(1.0, float(result.get(key, 0) or 0)))
        except (TypeError, ValueError):
            return 0.0

    memory_score = _score("memory_score")
    docs_score = _score("docs_score")
    chat_score = _score("chat_score")

    # Question path: pick the winning shelf. Do not keep a raw RETRIEVE
    # just because the model also filled kind.
    if is_question:
        scores = {
            "REMEMBER": memory_score,
            "RETRIEVE": docs_score,
            "CHAT": chat_score,
        }
        best_val = max(scores.values())
        winners = [name for name, value in scores.items() if value == best_val]
        if best_val > 0:
            if len(winners) == 1:
                kind = winners[0]
            elif kind not in winners:
                kind = winners[0]
        elif kind not in ("CHAT", "RETRIEVE", "REMEMBER"):
            kind = "CHAT"
    elif kind not in ("CHAT", "RETRIEVE", "REMEMBER", "ACTION"):
        kind = "CHAT"

    # ACTION is live (SPEC-PIPELINE §3.1): no demotion to CHAT anymore.
    intent = kind

    confidence = float(result.get("confidence", 0.5))
    print(
        f"intent_is_question={is_question} "
        f"memory={memory_score:.2f} docs={docs_score:.2f} chat={chat_score:.2f} "
        f"intent_kind={kind} intent={intent}"
    )
    return {
        "intent": intent,
        "confidence": max(0.0, min(1.0, confidence)),
        "is_question": is_question,
        "kind": kind,
        "memory_score": memory_score,
        "docs_score": docs_score,
        "chat_score": chat_score,
    }


def looks_like_question(text: str) -> bool:
    return _looks_like_question(text)


def usable_memory_facts(
    facts: list[dict] | None,
    text: str | None = None,
) -> list[dict]:
    """Drop blocked/junk facts (e.g. name=Michelle) before save or yes/no.

    If text is given, also drop facts the current message never mentioned
    (stops the assessor from copying old indigo/Nate onto "all good").
    """
    usable = []
    for fact in facts or []:
        key = str(fact.get("key") or "").strip().lower()
        value = str(fact.get("value") or "").strip()
        if not key or not value:
            continue
        if key == "name" and not is_valid_name(value):
            continue
        if text is not None and not _fact_supported_by_text(fact, text):
            continue
        usable.append(fact)
    return usable


def _fact_supported_by_text(fact: dict, text: str) -> bool:
    """Assessor/analyzer facts must come from THIS message, not prior chat."""
    value = str(fact.get("value") or "").strip().lower()
    blob = (text or "").strip().lower()
    if not value or not blob:
        return False
    if value in blob:
        return True
    tokens = [t for t in re.split(r"[^a-z0-9]+", value) if len(t) >= 4]
    return bool(tokens) and any(token in blob for token in tokens)


def maybe_promote_to_remember(
    intent: str,
    text: str,
    memory_result: dict,
    is_question: bool | None = None,
) -> str:
    """If chat missed an explicit store, promote when the assessor already has a fact.

    llama3.2 often leaves wants_store false on retain-instructions. Do not promote
    questions (those are recall/lookup) and do not wait on a second analyzer call.
    Facts must be grounded in THIS message so "all good" cannot become a store.
    """
    if intent != "CHAT":
        return intent
    if is_question if is_question is not None else _looks_like_question(text):
        return intent
    if not (memory_result.get("ask_user") or memory_result.get("important")):
        return intent
    if not usable_memory_facts(memory_result.get("facts"), text):
        return intent
    print("intent_promoted=REMEMBER (assessor facts)")
    return "REMEMBER"


# Deterministic ACTION detection for rules/mock modes (SPEC-PIPELINE §3.1).
# Verbs/slang only — app names are not a closed list.
_ACTION_OPEN_RE = re.compile(
    rf"^{_LEADING_FILLER}{_OPEN_VERBS}(?:\s+up)?\s+\S",
    re.IGNORECASE,
)
_ACTION_CLOSE_RE = re.compile(
    rf"^{_LEADING_FILLER}{_CLOSE_TAIL}\s+\S",
    re.IGNORECASE,
)
_ACTION_QUIT_RE = re.compile(
    rf"^{_LEADING_FILLER}{_QUIT_WORD}\s+\S",
    re.IGNORECASE,
)
# send email / send an email / send another email / send a new email /
# or "email alex@…" — including e…l typos (emauil, emial). Not a lone
# "email" inside a body ("email feature") because that lacks send / an @.
_EMAIL_VERB = (
    rf"(?:send\s+(?:(?:an?|another|a\s+new)\s+)?{_EMAIL_WORD}\b|"
    rf"{_EMAIL_WORD}\s+(?:to\s+)?\S+@\S+)"
)
_ACTION_EMAIL_RE = re.compile(
    rf"^{_LEADING_FILLER}{_EMAIL_VERB}",
    re.IGNORECASE,
)
# Must say draft or email (except "the one about …"). "show me the handbook"
# is not resume. "email draft" is one opener so we do not also split on
# "draft about" in "pull up the email draft about the moon".
_RESUME_DRAFT_INNER = (
    r"(?:send|finish|continue|resume)\s+(?:that|the|this)\s+(?:\S+\s+){0,4}(?:draft|e-?mail)"
    r"|(?:show|open|pull\s+up|find|get)(?:\s+me)?\s+(?:the\s+)?(?:(?:e-?mail\s+)?draft|e-?mail)"
    r"|(?:close|shut|clsoe|cloes|closed)\s+(?:the\s+|that\s+|this\s+)?(?:(?:e-?mail\s+)?draft|e-?mail|letter)"
    r"|(?:draft|e-?mail)\s+about"
    r"|(?:that|the)\s+one\s+about"
)
_RESUME_DRAFT_RE = re.compile(_RESUME_DRAFT_INNER, re.IGNORECASE)
_NEW_EMAIL_SEND_RE = re.compile(
    r"\bsend\s+(?:an?|another|a\s+new)\s+e-?mail\b",
    re.IGNORECASE,
)


def _looks_like_resume_draft(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    if _NEW_EMAIL_SEND_RE.search(stripped):
        return False
    return bool(_RESUME_DRAFT_RE.search(stripped))


def _looks_like_action_order(text: str) -> bool:
    return bool(parse_mixed_utterance(text)["actions"])


def _clause_is_action(clause: str) -> bool:
    stripped = (clause or "").strip()
    return bool(
        _ACTION_OPEN_RE.match(stripped)
        or _ACTION_CLOSE_RE.match(stripped)
        or _ACTION_QUIT_RE.match(stripped)
        or _ACTION_EMAIL_RE.match(stripped)
        or _looks_like_resume_draft(stripped)
    )


_ACTION_START_RE = re.compile(
    rf"(?:{_LEADING_FILLER})(?:{_RESUME_DRAFT_INNER}|{_EMAIL_VERB}|"
    rf"{_CLOSE_TAIL}\s+\S|{_QUIT_WORD}\s+\S|"
    rf"{_OPEN_VERBS}(?:\s+up)?\s+\S)",
    re.IGNORECASE,
)
_TRAILING_CONNECTOR_RE = re.compile(
    r"[\s,;]*(?:\b(?:and then|and|also|then|plus|btw)\b[\s,;]*)+$",
    re.IGNORECASE,
)


def parse_mixed_utterance(text: str) -> dict:
    """Split chat filler from one or more action clauses.

    Commas are NOT a blanket split (email params use them). An action starts
    wherever an open/email verb appears, so 'hey, open Notes' still routes.
    """
    stripped = (text or "").strip()
    if not stripped:
        return {"chat": "", "actions": []}
    starts = [m.start() for m in _ACTION_START_RE.finditer(stripped)]
    if not starts:
        return {"chat": stripped if not _clause_is_action(stripped) else "",
                "actions": [stripped] if _clause_is_action(stripped) else []}
    chat = stripped[: starts[0]].strip()
    chat = _TRAILING_CONNECTOR_RE.sub("", chat).strip(" \t,;.")
    actions: list[str] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(stripped)
        chunk = stripped[start:end]
        chunk = _TRAILING_CONNECTOR_RE.sub("", chunk).strip(" \t,;.")
        if chunk:
            actions.append(chunk)
    return {"chat": chat, "actions": actions}


def _classify_with_rules(text: str) -> dict:
    lower = text.strip().lower()

    if _looks_like_remember_command(text):
        return {"intent": "REMEMBER", "confidence": 0.95}

    # Asking what Michelle already knows → REMEMBER recall, not docs.
    if re.search(
        r"\b(do you remember|rmbr|what(?:'s| is) my name|how old am i|what(?:'s| is) my age)\b",
        lower,
    ):
        return {"intent": "REMEMBER", "confidence": 0.85}

    # Orders to do a task (open an app, send an email) — even with a "?".
    if _looks_like_action_order(text):
        return {"intent": "ACTION", "confidence": 0.9}

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
        # Complaints about Michelle's last reply are chat, not a doc lookup.
        if re.match(r"^(?:hey\s+)?(?:why|whyd|how'd|howd|how come)\b", lower):
            return {"intent": "CHAT", "confidence": 0.8}
        return {"intent": "RETRIEVE", "confidence": 0.5}

    return {"intent": "CHAT", "confidence": 0.5}


def _classify_mock(text: str) -> dict:
    lower = text.strip().lower()
    if _looks_like_remember_command(text):
        return {"intent": "REMEMBER", "confidence": 0.95}
    if re.search(
        r"\b(do you remember|rmbr|what(?:'s| is) my name|how old am i|what(?:'s| is) my age)\b",
        lower,
    ):
        return {"intent": "REMEMBER", "confidence": 0.9}
    if _looks_like_action_order(text):
        return {"intent": "ACTION", "confidence": 0.9}
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
        raw_value = item.get("value")
        if raw_value is None:
            continue
        value = str(raw_value).strip()
        if key == "name" and not is_valid_name(value):
            continue
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
    ) or "(no facts saved)"

    prompt = f"""Michelle already classified this as related to memory. Dig deeper.
{_SLOPPY_REPLY_HINT}
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
- "what's my name?" / "hey rmbr my name?" → is_question=true, mode=recall
  If no name is in Known facts, do not invent one and do not store None/null.

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
        raw_value = item.get("value")
        if raw_value is None:
            continue
        value = str(raw_value).strip()
        item_priority = str(item.get("priority", overall_priority)).strip().lower()
        if item_priority not in PRIORITIES:
            item_priority = overall_priority
        if key == "name" and not is_valid_name(value):
            continue
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
    ) or "(no facts saved)"

    prompt = f"""You score whether this user message should enter LONG-TERM memory.
{_SLOPPY_REPLY_HINT}
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
- never invent a name. If you do not know their name, omit the name fact.
  Do not use None, null, unknown, or n/a as a name.
- questions and acknowledgements ("all good", "ok", "hey rmbr my name?") —
  important=false, ask_user=false, empty facts. Do not copy Known facts into facts.

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

    if not candidate and _intent_llm_on():
        try:
            result = _llm_json(
                f"{_SLOPPY_REPLY_HINT}\n"
                "Michelle just asked for the user's name. Extract the name "
                "they are giving. If they are not giving a name, use null.\n"
                f"User message: {stripped}\n"
                'Reply with ONLY JSON: {"name": "Nathan"|null}'
            )
            raw = (result or {}).get("name")
            if raw:
                candidate = str(raw).strip().split()[0]
        except Exception as e:
            print(f"name capture LLM failed ({e})")

    if not candidate:
        return None
    if not is_valid_name(candidate):
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
        if is_valid_name(value):
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
        if bare and is_valid_name(bare.group(1)):
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
