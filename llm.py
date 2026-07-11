import os
from typing import Optional

import httpx
from google import genai


def ask_llm(prompt: str, history: Optional[list[dict]] = None) -> str:
    provider = os.getenv("LLM_PROVIDER", "mock").lower()
    if provider == "mock":
        return _ask_mock(prompt)
    if provider == "ollama":
        return _ask_ollama(prompt, history or [])
    if provider == "gemini":
        return _ask_gemini(prompt, history or [])
    raise ValueError(f"Unknown LLM_PROVIDER: {provider}. Use mock, ollama, or gemini.")


def _ask_mock(prompt: str) -> str:
    text = prompt.strip()
    lower = text.lower()

    if lower in {"hi", "hello", "hey"}:
        return "Hi! I'm Michelle in mock mode. No API credits are being used."
    if "your name" in lower or "who are you" in lower:
        return "I'm Michelle.ai — running in mock mode for free local testing."
    if lower.startswith("echo "):
        return text[5:].strip() or "(empty echo)"

    return (
        f"[mock mode] I heard: \"{text}\". "
        "Set LLM_PROVIDER=ollama or gemini in .env when you want a real AI response."
    )


def _ask_ollama(prompt: str, history: list[dict]) -> str:
    model = os.getenv("OLLAMA_MODEL", "llama3.2")
    base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")

    messages = [{"role": turn["role"], "content": turn["content"]} for turn in history]
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


def _ask_gemini(prompt: str, history: list[dict]) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is missing from .env")

    os.environ["GEMINI_API_KEY"] = api_key
    client = genai.Client()
    model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

    contents = []
    for turn in history:
        contents.append({"role": turn["role"], "parts": [{"text": turn["content"]}]})
    contents.append({"role": "user", "parts": [{"text": prompt}]})

    response = client.models.generate_content(model=model, contents=contents)
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty response.")
    return text
