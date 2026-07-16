import json
import os
from typing import Optional

from google import genai

INTENTS = ("CHAT", "RETRIEVE", "ACTION")


def classify_intent(text: str, history: Optional[list[dict]] = None) -> dict:
    """Classify what the user wants: chat, lookup, or action."""
    history = history or []
    mode = os.getenv("INTENT_MODE", "llm").lower()

    if mode == "rules":
        return _classify_with_rules(text)
    if mode == "mock":
        return _classify_mock(text)

    try:
        return _classify_with_llm(text, history)
    except Exception as e:
        print(f"Intent LLM failed ({e}), falling back to rules")
        return _classify_with_rules(text)


def _classify_with_llm(text: str, history: list[dict]) -> dict:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is missing from .env")

    os.environ["GEMINI_API_KEY"] = api_key
    client = genai.Client()
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    recent = history[-2:]
    context = "\n".join(f"{turn['role']}: {turn['content']}" for turn in recent)

    prompt = f"""Classify this user message into exactly one intent.

Intents:
- CHAT: greetings, small talk, opinions, "who are you", thanks, goodbye
- RETRIEVE: questions that need looking up docs, data, policies, or company facts
- ACTION: commands to DO something (create ticket, send email, schedule meeting, remind me)

Recent conversation:
{context or "(none)"}

User message: {text}

Reply with ONLY valid JSON, no markdown:
{{"intent": "CHAT"|"RETRIEVE"|"ACTION", "confidence": 0.0 to 1.0}}"""

    response = client.models.generate_content(model=model, contents=prompt)
    raw = (response.text or "").strip()

    # Gemini sometimes wraps JSON in ``` fences — strip them.
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    result = json.loads(raw)
    intent = result.get("intent", "")
    if intent not in INTENTS:
        raise ValueError(f"Invalid intent: {intent}")

    confidence = float(result.get("confidence", 0.5))
    return {"intent": intent, "confidence": max(0.0, min(1.0, confidence))}


def _classify_with_rules(text: str) -> dict:
    lower = text.strip().lower()

    action_phrases = (
        "create a ticket",
        "open a ticket",
        "schedule a meeting",
        "send an email",
        "book a",
        "remind me to",
    )
    if any(phrase in lower for phrase in action_phrases):
        return {"intent": "ACTION", "confidence": 0.85}

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
    if any(p in lower for p in ("ticket", "schedule", "send an email", "remind me")):
        return {"intent": "ACTION", "confidence": 0.9}
    if "?" in text or any(w in lower for w in ("policy", "look up", "find")):
        return {"intent": "RETRIEVE", "confidence": 0.9}
    return {"intent": "CHAT", "confidence": 0.9}


if __name__ == "__main__":
    samples = [
        "hey!",
        "what's our refund policy?",
        "create a ticket for billing",
        "thanks Michelle",
    ]
    for msg in samples:
        print(msg, "->", classify_intent(msg))
