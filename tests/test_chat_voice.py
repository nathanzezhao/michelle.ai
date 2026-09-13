"""Chat voice endpoint — transcribe then normal /chat pipeline."""

import io
import struct
import wave

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
    monkeypatch.setattr(whisper, "is_junk_transcript", lambda _t: False)

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
    monkeypatch.setattr(whisper, "is_junk_transcript", lambda _t: False)

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
