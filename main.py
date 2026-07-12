import os
from typing import Optional
from uuid import UUID, uuid4

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from llm import ask_llm
from memory import get_history, init_db, save_message

load_dotenv()
init_db()

# Guardrail: cap message size so a giant paste can't blow up the prompt or API bill.
MAX_MESSAGE_CHARS = int(os.getenv("MAX_MESSAGE_CHARS", "4000"))

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class UserMessage(BaseModel):
    text: str
    conversation_id: Optional[str] = None


def _valid_conversation_id(value: Optional[str]) -> str:
    """Return the client's ID only if it's a real UUID; otherwise start fresh.

    Guardrail: a malformed or garbage ID never gets written into the database.
    """
    if value:
        try:
            UUID(value)
            return value
        except ValueError:
            pass
    return str(uuid4())


@app.post("/chat")
def handle_chat(incoming_data: UserMessage):
    provider = os.getenv("LLM_PROVIDER", "mock")
    conversation_id = _valid_conversation_id(incoming_data.conversation_id)
    user_text = incoming_data.text.strip()

    # Guardrail: blank sends are rejected before touching the DB or the LLM.
    if not user_text:
        return {
            "answer": "Please type a message first.",
            "conversation_id": conversation_id,
        }

    # Guardrail: oversized messages are rejected, not truncated silently.
    if len(user_text) > MAX_MESSAGE_CHARS:
        return {
            "answer": (
                f"That message is too long ({len(user_text)} characters). "
                f"Please keep it under {MAX_MESSAGE_CHARS} characters."
            ),
            "conversation_id": conversation_id,
        }

    print(f"[{provider}] [{conversation_id[:8]}] User said: {user_text}")

    try:
        history = get_history(conversation_id)
        answer = ask_llm(user_text, history)

        # Save only after a successful answer: a failed turn leaves no trace,
        # so Michelle never "remembers" her own error messages.
        save_message(conversation_id, "user", user_text)
        save_message(conversation_id, "assistant", answer)

        print(f"[{provider}] [{conversation_id[:8]}] Michelle responded: {answer}")
        return {"answer": answer, "conversation_id": conversation_id}
    except Exception as e:
        print(f"Error: {e}")
        return {
            "answer": "Sorry, I am having some trouble with this right now. Please try again later.",
            "conversation_id": conversation_id,
        }
