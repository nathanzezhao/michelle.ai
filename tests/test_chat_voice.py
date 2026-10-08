"""Chat voice endpoint — transcribe then normal /chat pipeline."""

import io
import struct
import wave
from uuid import uuid4

import pytest

import whisper


def _silent_wav(seconds: float = 0.05) -> bytes:
    buf = io.BytesIO()
    rate = 16000
    frames = int(rate * seconds)
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(struct.pack("<" + "h" * frames, *([0] * frames)))
    return buf.getvalue()


def test_chat_voice_heard_nothing_short_audio(client, ids):
    files = {"audio": ("voice.wav", _silent_wav(0.05), "audio/wav")}
    data = {"conversation_id": ids["conversation_id"], "user_id": ids["user_id"]}
    r = client.post("/chat/voice", files=files, data=data)
    assert r.status_code == 200
    body = r.json()
    assert body.get("error") == "heard_nothing"


def test_chat_voice_routes_like_typed(client, ids, monkeypatch):
    spoken = "open Notes"

    def fake_transcribe(_audio: bytes) -> str:
        return spoken

    monkeypatch.setattr(whisper, "transcribe_wav", fake_transcribe)
    monkeypatch.setattr(whisper, "wav_duration_seconds", lambda _b: 1.0)
    monkeypatch.setattr(whisper, "is_junk_transcript", lambda _t, **_k: False)

    files = {"audio": ("voice.wav", _silent_wav(1.0), "audio/wav")}
    data = {"conversation_id": ids["conversation_id"], "user_id": ids["user_id"]}
    r = client.post("/chat/voice", files=files, data=data)
    assert r.status_code == 200
    body = r.json()
    assert body.get("transcript") == spoken
    assert body.get("engine") == "action"
    assert body.get("action_type") == "open_app"


def test_chat_voice_does_not_hit_draft_body(client, ids, monkeypatch):
    monkeypatch.setattr(whisper, "transcribe_wav", lambda _b: "hey there")
    monkeypatch.setattr(whisper, "wav_duration_seconds", lambda _b: 1.0)
    monkeypatch.setattr(whisper, "is_junk_transcript", lambda _t, **_k: False)

    seen = {"draft": False}

    def boom(*_a, **_k):
        seen["draft"] = True
        raise AssertionError("draft_body should not run")

    import main

    monkeypatch.setattr(main, "draft_body", boom)

    files = {"audio": ("voice.wav", _silent_wav(1.0), "audio/wav")}
    data = {"conversation_id": ids["conversation_id"], "user_id": ids["user_id"]}
    r = client.post("/chat/voice", files=files, data=data)
    assert r.status_code == 200
    assert seen["draft"] is False


def test_chat_transcribe_fills_text_without_chat(client, ids, monkeypatch):
    spoken = "open Notes"
    monkeypatch.setattr(whisper, "transcribe_wav", lambda _b: spoken)
    monkeypatch.setattr(whisper, "wav_duration_seconds", lambda _b: 1.0)

    files = {"audio": ("voice.wav", _silent_wav(1.0), "audio/wav")}
    data = {"conversation_id": ids["conversation_id"], "user_id": ids["user_id"]}
    r = client.post("/chat/transcribe", files=files, data=data)
    assert r.status_code == 200
    body = r.json()
    assert body.get("transcript") == spoken
    assert body.get("error") is None
    assert body.get("engine") == "chat"


def test_chat_voice_send_email_returns_composer_fields(client, ids, monkeypatch):
    """Blob /chat/voice must carry the same send_email payload as typed /chat
    so the window can open the composer (not only the missing-fields sentence)."""
    spoken = "send an email"
    monkeypatch.setattr(whisper, "transcribe_wav", lambda _b: spoken)
    monkeypatch.setattr(whisper, "wav_duration_seconds", lambda _b: 1.0)
    monkeypatch.setattr(whisper, "is_junk_transcript", lambda _t, **_k: False)

    files = {"audio": ("voice.wav", _silent_wav(1.0), "audio/wav")}
    data = {"conversation_id": ids["conversation_id"], "user_id": ids["user_id"]}
    voice = client.post("/chat/voice", files=files, data=data).json()
    typed = client.post(
        "/chat",
        json={
            "text": spoken,
            "conversation_id": str(uuid4()),
            "user_id": str(uuid4()),
        },
    ).json()

    assert voice.get("transcript") == spoken
    assert voice["engine"] == "action"
    assert voice["action_type"] == "send_email"
    assert voice["task_status"] == "AWAITING_INPUT"
    assert voice["task_id"]
    assert "subject" in (voice.get("answer") or "").lower()
    assert typed["action_type"] == "send_email"
    assert typed["task_status"] == "AWAITING_INPUT"


def test_chat_transcribe_junk_does_not_return_usable_text(client, ids, monkeypatch):
    monkeypatch.setattr(whisper, "transcribe_wav", lambda _b: "thank you")
    monkeypatch.setattr(whisper, "wav_duration_seconds", lambda _b: 1.0)
    files = {"audio": ("voice.wav", _silent_wav(1.0), "audio/wav")}
    data = {"conversation_id": ids["conversation_id"], "user_id": ids["user_id"]}
    r = client.post("/chat/transcribe", files=files, data=data)
    assert r.status_code == 200
    body = r.json()
    assert body.get("error") == "heard_nothing"
