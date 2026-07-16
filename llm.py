import os
from typing import Optional
from google.genai import types
import httpx
from google import genai

## To keep responses nice and concise
SYSTEM_PROMPT = (
    "You are Michelle, a helpful assistant. "
    "Keep replies short — 1 to 5 sentences max. "
    "No bullet lists unless the user asks. Be direct, but nice. "
    "Have a lax personality, you are allowed to use gen z lingo and swear a little. "
)

def ask_llm(prompt: str, history: Optional[list[dict]] = None) -> str:
    provider = os.getenv("LLM_PROVIDER", "mock").lower()
    if provider == "mock":
        return _ask_mock(prompt, history or [])
    if provider == "ollama":
        return _ask_ollama(prompt, history or [])
    if provider == "gemini":
        return _ask_gemini(prompt, history or [])
    raise ValueError(f"Unknown LLM_PROVIDER: {provider}. Use mock, ollama, or gemini.")


def _ask_mock(prompt: str, history: list[dict]) -> str:
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
        for turn in history:
            if turn["role"] != "user":
                continue
            content = turn["content"].lower()
            if "my name is" in content:
                name = content.split("my name is", 1)[1].strip().strip(".!?")
                if name:
                    return name.capitalize()
        return "I don't know your name yet. Tell me with something like \"My name is Nathan\"."

    return (
        f"[mock mode] I heard: \"{text}\". "
        "Set LLM_PROVIDER=ollama or gemini in .env when you want a real AI response."
    )


def _ask_ollama(prompt: str, history: list[dict]) -> str:
    model = os.getenv("OLLAMA_MODEL", "llama3.2")
    base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
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


def _ask_gemini(prompt: str, history: list[dict]) -> str:
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
    config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
    )
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty response.")
    return text



