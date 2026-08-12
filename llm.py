import os
import re
from typing import Optional

import httpx
from google import genai
from google.genai import types

from long_term_memory import format_facts

## To keep responses nice and concise
SYSTEM_PROMPT = (
    "You are Michelle, a helpful assistant. "
    "Keep replies short — 1 to 5 sentences max. "
    "No bullet lists unless the user asks. Be direct, but nice. "
    "Have a lax personality, you are allowed to swear a little. "
    "If you just asked for their name and they give one, acknowledge it warmly. "
    "You cannot send email, set reminders, or take external actions yet. "
    "If asked, say that plainly — do not pretend you did it. "
    "If a previous document lookup failed, do not keep talking about it "
    "unless the user brings that topic up again."
)


def _build_system_prompt(long_term_facts: Optional[list[dict]] = None) -> str:
    """Regular system prompt + long-term facts that survive the short chat window."""
    prompt = SYSTEM_PROMPT
    facts_block = format_facts(long_term_facts or [])
    if facts_block:
        prompt = (
            f"{prompt}\n\n{facts_block}\n"
            "Use these facts naturally when relevant. "
            "Do not dump the whole list unless asked."
        )
    return prompt


RETRIEVE_MISS_REPLY = (
    "I looked through my docs and couldn't find anything relevant. "
    "Try another question, or add a file under the docs/ folder and restart the backend."
)

RETRIEVE_SYSTEM_EXTRA = (
    "You are answering using the document snippets provided below. "
    "If a snippet is about the same topic as the question, answer from it — "
    "do not claim a miss or apologize for missing info when the snippet has the answer. "
    "Only say you don't have it when the snippets are empty or clearly about something else. "
    "Do not invent policies or facts. Keep replies short (1–5 sentences). "
    "You may briefly mention which doc the answer came from. "
    "Do not keep talking about a failed lookup on later turns unless the user brings it up."
)


_HISTORY_STOPWORDS = {
    "the",
    "and",
    "for",
    "are",
    "was",
    "what",
    "whats",
    "when",
    "where",
    "who",
    "how",
    "why",
    "can",
    "you",
    "our",
    "your",
    "about",
    "with",
    "from",
    "this",
    "that",
    "have",
    "has",
    "does",
    "did",
    "tell",
    "please",
    "something",
    "anything",
    "looking",
    "couldn",
    "find",
    "just",
    "like",
    "want",
    "need",
    "know",
}


def _content_tokens(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]{4,}", (text or "").lower()))
    return tokens - _HISTORY_STOPWORDS


def history_for_reply(history: list[dict], current_text: str) -> list[dict]:
    """Copy of chat history for the LLM.

    Lookup turns stay in the DB (so they can be brought up later) but are
    omitted from the prompt unless the current message looks related.
    A lookup is whatever the router tagged retrieve / retrieve_miss — not a
    phrase list.
    """
    if not history:
        return []

    current_tokens = _content_tokens(current_text)
    skip = set()

    def _is_lookup_turn(turn: dict) -> bool:
        if turn.get("role") != "assistant":
            return False
        kind = turn.get("kind") or ""
        if kind.startswith("retrieve"):
            return True
        # Untagged rows from before kind existed.
        content = (turn.get("content") or "").strip()
        return content.startswith(RETRIEVE_MISS_REPLY[:40])

    for i, turn in enumerate(history):
        if turn.get("role") != "assistant" or not _is_lookup_turn(turn):
            continue
        user_i = i - 1 if i > 0 and history[i - 1].get("role") == "user" else None
        miss_tokens = (
            _content_tokens(history[user_i].get("content", "")) if user_i is not None else set()
        )
        related = bool(current_tokens and miss_tokens and (current_tokens & miss_tokens))
        if not related:
            skip.add(i)
            if user_i is not None:
                skip.add(user_i)

    return [turn for i, turn in enumerate(history) if i not in skip]


def ask_llm(
    prompt: str,
    history: Optional[list[dict]] = None,
    long_term_facts: Optional[list[dict]] = None,
) -> str:
    provider = os.getenv("LLM_PROVIDER", "mock").lower()
    history = history or []
    long_term_facts = long_term_facts or []
    history = history_for_reply(history, prompt)
    if provider == "mock":
        return _ask_mock(prompt, history, long_term_facts)
    if provider == "ollama":
        return _ask_ollama(prompt, history, long_term_facts)
    if provider == "gemini":
        return _ask_gemini(prompt, history, long_term_facts)
    raise ValueError(f"Unknown LLM_PROVIDER: {provider}. Use mock, ollama, or gemini.")


def ask_llm_with_context(
    prompt: str,
    context: str,
    history: Optional[list[dict]] = None,
    long_term_facts: Optional[list[dict]] = None,
) -> str:
    """Answer a RETRIEVE question grounded in retrieved doc snippets."""
    if not context.strip():
        return RETRIEVE_MISS_REPLY

    provider = os.getenv("LLM_PROVIDER", "mock").lower()
    history = history_for_reply(history or [], prompt)
    long_term_facts = long_term_facts or []

    if provider == "mock":
        return _ask_mock_with_context(prompt, context)

    grounded_prompt = (
        f"Document snippets:\n{context}\n\n"
        f"User question: {prompt}\n\n"
        "Answer using only those snippets."
    )
    system = (
        f"{_build_system_prompt(long_term_facts)}\n\n{RETRIEVE_SYSTEM_EXTRA}"
    )

    if provider == "ollama":
        return _ask_ollama(grounded_prompt, history, long_term_facts, system_prompt=system)
    if provider == "gemini":
        return _ask_gemini(grounded_prompt, history, long_term_facts, system_prompt=system)
    raise ValueError(f"Unknown LLM_PROVIDER: {provider}. Use mock, ollama, or gemini.")


def _ask_mock_with_context(prompt: str, context: str) -> str:
    """Cheap local answer: pull a short excerpt from the best matching snippet."""
    lower = prompt.lower()
    blocks = [
        b.strip()
        for b in re.split(r"\n(?=\[\d+\] Source:)", context)
        if b.strip()
    ]
    if not blocks:
        return "I don't have that in my docs (mock mode)."

    chosen = blocks[0]
    for block in blocks:
        head = block.lower()
        if "refund" in lower and "refund" in head:
            chosen = block
            break
        if any(
            w in lower
            for w in ("vacation", "remote", "handbook", "time off", "learning")
        ) and (
            "handbook" in head
            or "vacation" in head
            or "remote" in head
            or "learning" in head
        ):
            chosen = block
            break

    source = "docs"
    body = chosen
    if "Source:" in chosen:
        first_line, _, rest = chosen.partition("\n")
        source = first_line.split("Source:", 1)[-1].strip() or source
        body = rest.strip()

    snippet = " ".join(body.split())
    if len(snippet) > 280:
        snippet = snippet[:277].rstrip() + "..."
    return f"From {source}: {snippet}"


def _ask_mock(
    prompt: str,
    history: list[dict],
    long_term_facts: list[dict],
) -> str:
    text = prompt.strip()
    lower = text.lower()

    if lower in {"hi", "hello", "hey"}:
        return "Hi! I'm Michelle in mock mode. No API credits are being used."
    if "your name" in lower or "who are you" in lower:
        return "I'm Michelle.ai — running in mock mode for free local testing."
    if lower.startswith("echo "):
        return text[5:].strip() or "(empty echo)"

    # Memory-aware replies so conversation memory can be tested for free.
    if "what did i just ask" in lower or "what did i say" in lower:
        for turn in reversed(history):
            if turn["role"] == "user":
                return f'You just asked: "{turn["content"]}"'
        return "You haven't asked me anything yet in this conversation."

    if "what's my name" in lower or "what is my name" in lower:
        for fact in long_term_facts:
            if fact["key"] == "name":
                return fact["value"]
        for turn in history:
            if turn["role"] != "user":
                continue
            content = turn["content"].lower()
            if "my name is" in content:
                name = content.split("my name is", 1)[1].strip().strip(".!?")
                if name:
                    return name.capitalize()
        return "I don't know your name yet. Tell me with something like \"My name is Nathan\"."

    if "where do i live" in lower or "where's my location" in lower:
        for fact in long_term_facts:
            if fact["key"] == "location":
                return fact["value"]
        return "I don't have your location saved yet."

    if "how old am i" in lower or "what's my age" in lower or "what is my age" in lower:
        for fact in long_term_facts:
            if fact["key"] == "age":
                return fact["value"]
        return "I don't have your age saved yet. Say \"remember this: my age is 22\"."

    return (
        f"[mock mode] I heard: \"{text}\". "
        "Set LLM_PROVIDER=ollama or gemini in .env when you want a real AI response."
    )


def _ask_ollama(
    prompt: str,
    history: list[dict],
    long_term_facts: list[dict],
    system_prompt: Optional[str] = None,
) -> str:
    model = os.getenv("OLLAMA_MODEL", "llama3.2")
    base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")

    messages = [
        {
            "role": "system",
            "content": system_prompt or _build_system_prompt(long_term_facts),
        }
    ]
    messages += [{"role": turn["role"], "content": turn["content"]} for turn in history]
    messages.append({"role": "user", "content": prompt})

    response = httpx.post(
        f"{base_url}/api/chat",
        json={"model": model, "messages": messages, "stream": False},
        timeout=300.0,
    )
    response.raise_for_status()
    content = response.json().get("message", {}).get("content", "").strip()
    if not content:
        raise RuntimeError("Ollama returned an empty response.")
    return content


def _ask_gemini(
    prompt: str,
    history: list[dict],
    long_term_facts: list[dict],
    system_prompt: Optional[str] = None,
) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is missing from .env")

    os.environ["GEMINI_API_KEY"] = api_key
    client = genai.Client()
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    contents = []
    for turn in history:
        # Gemini uses "model" for assistant turns, not "assistant".
        role = "model" if turn["role"] == "assistant" else turn["role"]
        contents.append({"role": role, "parts": [{"text": turn["content"]}]})
    contents.append({"role": "user", "parts": [{"text": prompt}]})

    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt or _build_system_prompt(long_term_facts),
        ),
    )
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty response.")
    return text
