"""R0 regression suite bootstrap.

CRITICAL ordering: memory.py, long_term_memory.py, and retrieve.py read
MICHELLE_DB_PATH / MICHELLE_DOCS_DIR at IMPORT time, and main.py runs
init_db() + index_docs() at import. So all env vars are set here, at module
scope, BEFORE `import main`. pytest imports conftest.py before any test
module, which guarantees the ordering.

Also note the repo's .env sets LLM_PROVIDER=ollama and INTENT_MODE=llm;
load_dotenv() does not override already-set env vars, so the values set
below win as long as they are set before main.py imports.
"""

import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="michelle-r0-"))
DOCS_DIR = _TMP / "docs"
DOCS_DIR.mkdir()

# One doc with a distinctive marker for the RETRIEVE hit case. Written before
# main import because retrieve.index_docs() runs at import time.
REFUND_DOC = DOCS_DIR / "refund_policy.md"
REFUND_MARKER = "QUOKKABERRY-77"
REFUND_DOC.write_text(
    "# Refund Policy\n\n"
    "Our refund policy: purchases can be refunded within 14 days.\n"
    f"The refund confirmation code is {REFUND_MARKER}.\n"
)

os.environ["MICHELLE_DB_PATH"] = str(_TMP / "michelle-r0.db")
os.environ["MICHELLE_DOCS_DIR"] = str(DOCS_DIR)
os.environ["LLM_PROVIDER"] = "mock"
os.environ["INTENT_MODE"] = "rules"

import main  # noqa: E402  (env must be set first — see module docstring)

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def client():
    return TestClient(main.app)


# Orthogonal handler axes for the command-layer stub (same map Ada uses).
_OPEN_VEC = [1.0, 0.0, 0.0, 0.0, 0.0]
_CLOSE_VEC = [0.0, 1.0, 0.0, 0.0, 0.0]
_QUIT_VEC = [0.0, 0.0, 1.0, 0.0, 0.0]
_EMAIL_VEC = [0.0, 0.0, 0.0, 1.0, 0.0]
_CHAT_VEC = [0.0, 0.0, 0.0, 0.0, 1.0]
_TIE_VEC = [0.7071, 0.0, 0.0, 0.7071, 0.0]
_OTHER_VEC = [0.3, 0.3, 0.3, 0.3, 0.3]


def _stub_is_chat_sink(text: str) -> bool:
    t = (text or "").strip().lower()
    if t in {"yo wsp", "yo", "wsp", "hello", "hi", "hey"}:
        return True
    if "how do i open" in t or "how do you open" in t or "how to open" in t:
        return True
    if "how to close" in t or "how do you close" in t or "how do i close" in t:
        return True
    if "steps to open" in t or "steps to close" in t:
        return True
    if "quit my job" in t or "closed the deal" in t:
        return True
    if "email me later" in t:
        return True
    return False


def stub_embed_text(text: str):
    """Deterministic embed stub: command axes + pad 3-d follow-ups. No Ollama."""
    t = (text or "").strip().lower()
    if not t:
        return None
    # Pad-state labels / queries stay 3-d so try-again cosine still binds.
    if t in {"unrelated chat", "hello", "what's for lunch", "whats for lunch"}:
        return [0.0, 0.0, 1.0]
    if any(
        bit in t
        for bit in (
            "try again",
            "wasn't",
            "wasnt",
            "retry quitting",
            "already quit",
            "retry closing",
            "retry opening",
        )
    ):
        return [1.0, 0.0, 0.0]
    if _stub_is_chat_sink(t):
        return list(_CHAT_VEC)
    if "refund" in t:
        return list(_OTHER_VEC)
    if "delete_files" in t or "delete all my files" in t or "book a flight" in t:
        return list(_OTHER_VEC)
    has_open = (not _stub_is_chat_sink(t)) and (
        t in {"open", "launch", "kick off", "pull up", "fire up"}
        or any(
            bit in t
            for bit in (
                "kick off",
                "open notes",
                "open ",
                "launch ",
                "pull up",
                "fire up",
                "hop into",
                "bring up",
            )
        )
    )
    has_email = (not _stub_is_chat_sink(t)) and (
        any(
            bit in t
            for bit in ("send an email", "send email", "email sam", "e-mail")
        )
        or t.startswith("email ")
    )
    if has_open and has_email:
        return list(_TIE_VEC)
    if has_open:
        return list(_OPEN_VEC)
    if has_email or "send an email" in t or "send an ema" in t:
        return list(_EMAIL_VEC)
    if t in {"close", "close notes"} or t.startswith("close ") or t.startswith("shut "):
        return list(_CLOSE_VEC)
    if t in {"quit", "quit notes"} or t.startswith("quit "):
        return list(_QUIT_VEC)
    if any(bit in t for bit in ("open", "kick off", "launch", "pull up")):
        return list(_OPEN_VEC)
    if "email" in t or "gmail" in t:
        return list(_EMAIL_VEC)
    if "close" in t or "clsoe" in t or "shut" in t:
        return list(_CLOSE_VEC)
    if "quit" in t or "quti" in t:
        return list(_QUIT_VEC)
    return list(_OTHER_VEC)


@pytest.fixture(autouse=True)
def deterministic_env(monkeypatch):
    """Default every test to mock LLM + rules intent; tests that need
    INTENT_MODE=mock override it themselves. Both vars are read per-request,
    so no module reload is needed."""
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("INTENT_MODE", "rules")
    import embeddings
    import intent

    intent._PAD_LABEL_VEC_CACHE.clear()
    import actions

    actions._APP_NAME_VEC_CACHE.clear()
    monkeypatch.setattr(embeddings, "embed_text", stub_embed_text)
    try:
        import command_layer

        command_layer.reset_prototypes()
        if hasattr(command_layer, "embeddings"):
            monkeypatch.setattr(command_layer.embeddings, "embed_text", stub_embed_text)
    except ImportError:
        pass


@pytest.fixture
def ids():
    """Fresh conversation/user UUIDs per test — this is the isolation
    mechanism on the shared per-session temp DB."""
    return {"conversation_id": str(uuid4()), "user_id": str(uuid4())}


def chat(client, text, ids):
    resp = client.post(
        "/chat",
        json={
            "text": text,
            "conversation_id": ids["conversation_id"],
            "user_id": ids["user_id"],
        },
    )
    assert resp.status_code == 200
    return resp.json()
