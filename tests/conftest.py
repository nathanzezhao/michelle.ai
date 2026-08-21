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


@pytest.fixture(autouse=True)
def deterministic_env(monkeypatch):
    """Default every test to mock LLM + rules intent; tests that need
    INTENT_MODE=mock override it themselves. Both vars are read per-request,
    so no module reload is needed."""
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("INTENT_MODE", "rules")


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
