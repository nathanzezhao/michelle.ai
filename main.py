import os
from typing import Optional
from uuid import UUID, uuid4

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from intent import (
    analyze_remember_request,
    assess_memory_worthiness,
    capture_introduced_name,
    classify_intent,
    classify_memory_confirmation,
    looks_like_question,
    maybe_promote_to_remember,
    usable_memory_facts,
)
from llm import ask_llm, ask_llm_with_context, history_for_reply
import long_term_memory
from memory import get_history, init_db, save_message
import retrieve

load_dotenv()
init_db()
long_term_memory.init_db()
retrieve.init_db()
retrieve.index_docs()

# Guardrail: cap message size so a giant paste can't blow up the prompt or API bill.
MAX_MESSAGE_CHARS = int(os.getenv("MAX_MESSAGE_CHARS", "4000"))

NAME_PROMPT = "Hey, I'm Michelle. What's your name?"

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
    user_id: Optional[str] = None


class SessionStart(BaseModel):
    conversation_id: Optional[str] = None
    user_id: Optional[str] = None


def _valid_uuid(value: Optional[str]) -> str:
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


def _fact_bits(facts: list[dict]) -> str:
    return "; ".join(f"{f['key']}: {f['value']}" for f in facts)


@app.post("/session/start")
def start_session(incoming_data: SessionStart):
    """Called when the Electron UI first opens (not on backend restart).

    If Michelle does not yet know this user's name, she asks once and stores
    it forever in long-term memory when they reply.
    """
    conversation_id = _valid_uuid(incoming_data.conversation_id)
    user_id = _valid_uuid(incoming_data.user_id)
    name = long_term_memory.get_fact(user_id, "name")

    if name:
        greeting = f"Hey {name}, what's up?"
        ask_name = False
    else:
        greeting = NAME_PROMPT
        ask_name = True
        # Persist the name ask into regular chat history so the memory
        # assessor can treat a bare reply like "Nathan" as their name.
        # Returning users only get a UI greeting — not a new DB row each launch.
        history = get_history(conversation_id, limit=1)
        already_asked = (
            history
            and history[-1]["role"] == "assistant"
            and history[-1]["content"] == NAME_PROMPT
        )
        if not already_asked:
            save_message(conversation_id, "assistant", greeting)

    return {
        "conversation_id": conversation_id,
        "user_id": user_id,
        "greeting": greeting,
        "ask_name": ask_name,
        "name": name,
    }


@app.post("/chat")
def handle_chat(incoming_data: UserMessage):
    provider = os.getenv("LLM_PROVIDER", "mock")
    conversation_id = _valid_uuid(incoming_data.conversation_id)
    user_id = _valid_uuid(incoming_data.user_id)
    user_text = incoming_data.text.strip()

    # Guardrail: blank sends are rejected before touching the DB or the LLM.
    if not user_text:
        return {
            "answer": "Please type a message first.",
            "conversation_id": conversation_id,
            "user_id": user_id,
        }

    # Guardrail: oversized messages are rejected, not truncated silently.
    if len(user_text) > MAX_MESSAGE_CHARS:
        return {
            "answer": (
                f"That message is too long ({len(user_text)} characters). "
                f"Please keep it under {MAX_MESSAGE_CHARS} characters."
            ),
            "conversation_id": conversation_id,
            "user_id": user_id,
        }

    print(f"[{provider}] [{conversation_id[:8]}] User said: {user_text}")

    try:
        # Regular memory = recent chat turns. Long-term = durable user facts.
        history = get_history(conversation_id)
        long_term_facts = long_term_memory.get_facts(user_id)
        pending = long_term_memory.get_pending_memory(user_id, conversation_id)

        introduced = capture_introduced_name(user_text, history)
        if introduced:
            long_term_memory.upsert_fact(
                user_id=user_id,
                fact_key="name",
                fact_value=introduced,
                confidence=1.0,
                priority="high",
                conversation_id=conversation_id,
            )
            long_term_facts = long_term_memory.get_facts(user_id)

        # If Michelle asked "want me to remember that?", handle yes/no first.
        if pending:
            confirmation = classify_memory_confirmation(user_text)
            if confirmation == "yes":
                saved = usable_memory_facts(pending)
                for fact in saved:
                    long_term_memory.upsert_fact(
                        user_id=user_id,
                        fact_key=fact["key"],
                        fact_value=fact["value"],
                        confidence=1.0,
                        priority="high",
                        conversation_id=conversation_id,
                    )
                long_term_memory.clear_pending_memory(user_id, conversation_id)
                if not saved:
                    answer = "No worries — nothing to save there."
                    save_message(conversation_id, "user", user_text)
                    save_message(conversation_id, "assistant", answer)
                    return {
                        "answer": answer,
                        "conversation_id": conversation_id,
                        "user_id": user_id,
                        "intent": "CHAT",
                        "remembered": [],
                    }
                answer = (
                    "Cool — saved for future conversations ("
                    + _fact_bits(saved)
                    + ")."
                )
                save_message(conversation_id, "user", user_text)
                save_message(conversation_id, "assistant", answer)
                print(f"[{provider}] [{conversation_id[:8]}] confirmed remember")
                return {
                    "answer": answer,
                    "conversation_id": conversation_id,
                    "user_id": user_id,
                    "intent": "REMEMBER",
                    "remembered": saved,
                }
            if confirmation == "no":
                long_term_memory.clear_pending_memory(user_id, conversation_id)
                answer = "No worries — I won't save that."
                save_message(conversation_id, "user", user_text)
                save_message(conversation_id, "assistant", answer)
                print(f"[{provider}] [{conversation_id[:8]}] declined remember")
                return {
                    "answer": answer,
                    "conversation_id": conversation_id,
                    "user_id": user_id,
                    "intent": "CHAT",
                    "remembered": [],
                }
            # Unrelated follow-up → drop the pending ask and continue normally.
            long_term_memory.clear_pending_memory(user_id, conversation_id)

        # Classify on the same filtered history the reply model sees, so a
        # failed lookup does not bias the next intent toward that topic.
        reply_history = history_for_reply(history, user_text)
        intent_result = classify_intent(user_text, reply_history)
        intent = intent_result["intent"]
        confidence = intent_result["confidence"]

        print(
            f"[{provider}] [{conversation_id[:8]}] "
            f"intent={intent} ({confidence:.2f})"
        )

        memory_result = assess_memory_worthiness(
            user_text,
            reply_history,
            long_term_facts,
        )
        print(
            f"[{provider}] [{conversation_id[:8]}] "
            f"memory_important={memory_result['important']} "
            f"ask_user={memory_result.get('ask_user')} "
            f"priority={memory_result.get('priority')} "
            f"({memory_result['confidence']:.2f}) "
            f"facts={memory_result['facts']}"
        )

        intent = maybe_promote_to_remember(
            intent,
            user_text,
            memory_result,
        )

        sources: list[str] = []
        remembered = []
        asked_to_remember = False

        if intent == "REMEMBER":
            # Question about past memory → recall naturally.
            # Non-question retain → save assessor/analyzer facts (analyzer often
            # mis-labels "don't forget …" as recall and then writes nothing).
            analysis = analyze_remember_request(
                user_text,
                reply_history,
                long_term_facts,
            )
            print(
                f"[{provider}] [{conversation_id[:8]}] "
                f"remember_mode={analysis['mode']} "
                f"is_question={analysis['is_question']} "
                f"matched={analysis['matched_facts']} "
                f"store={analysis['store_facts']}"
            )

            assessor_facts = usable_memory_facts(memory_result.get("facts"))
            store_facts = usable_memory_facts(analysis.get("store_facts"))

            if looks_like_question(user_text) and (
                analysis["mode"] == "recall"
                or analysis["matched_facts"]
                or analysis["is_question"]
            ):
                recall_facts = analysis["matched_facts"] or long_term_facts
                merged = {f["key"]: f for f in long_term_facts}
                for fact in recall_facts:
                    merged[fact["key"]] = {
                        "key": fact["key"],
                        "value": fact["value"],
                        "confidence": 1.0,
                        "priority": "high",
                    }
                answer = ask_llm(
                    user_text,
                    history,
                    list(merged.values()),
                )
            else:
                facts_to_store = store_facts or assessor_facts
                if facts_to_store:
                    for fact in facts_to_store:
                        long_term_memory.upsert_fact(
                            user_id=user_id,
                            fact_key=fact["key"],
                            fact_value=fact["value"],
                            confidence=1.0,
                            priority="high",
                            conversation_id=conversation_id,
                        )
                        remembered.append(fact)
                    updated_facts = long_term_memory.get_facts(user_id)
                    answer = ask_llm(
                        (
                            f"{user_text}\n\n"
                            "[System: You just saved these long-term facts: "
                            f"{_fact_bits(remembered)}. "
                            "Acknowledge briefly and naturally — do not use a rigid "
                            "'Got it — I'll remember that for future conversations' template, "
                            "do not dump the raw key:value list unless asked, "
                            "and do not bring up unrelated earlier topics.]"
                        ),
                        history,
                        updated_facts,
                    )
                else:
                    # "remind me to email Sam" etc. — not a durable fact.
                    intent = "CHAT"
                    answer = ask_llm(user_text, history, long_term_facts)
        elif intent == "RETRIEVE":
            context, chunks = retrieve.answer_from_docs(user_text)
            sources = sorted({chunk["source"] for chunk in chunks})
            print(
                f"[{provider}] [{conversation_id[:8]}] "
                f"retrieve_hits={len(chunks)} sources={sources}"
            )
            answer = ask_llm_with_context(
                user_text,
                context,
                history,
                long_term_facts,
            )
        else:
            answer = ask_llm(user_text, history, long_term_facts)

        # Auto-assessor: CHAT only. Don't ask to remember a doc lookup.
        # Still allowed to save user facts (name, prefs) when the assessor is sure,
        # including facts that showed up in earlier turns of this chat.
        if intent == "CHAT":
            chat_facts = usable_memory_facts(memory_result.get("facts"))
            if memory_result["important"] and chat_facts:
                for fact in chat_facts:
                    long_term_memory.upsert_fact(
                        user_id=user_id,
                        fact_key=fact["key"],
                        fact_value=fact["value"],
                        confidence=memory_result["confidence"],
                        priority=fact.get(
                            "priority", memory_result.get("priority", "high")
                        ),
                        conversation_id=conversation_id,
                    )
                    remembered.append(fact)
            elif memory_result.get("ask_user") and chat_facts:
                long_term_memory.set_pending_memory(
                    user_id,
                    conversation_id,
                    chat_facts,
                )
                asked_to_remember = True
                answer = (
                    f"{answer}\n\nWant me to remember that for future chats "
                    f"({_fact_bits(chat_facts)})? "
                    f"Say yes or no."
                )

        # Save only after a successful answer: a failed turn leaves no trace,
        # so Michelle never "remembers" her own error messages.
        # Tag by routing, not by matching the miss sentence.
        turn_kind = None
        if intent == "RETRIEVE":
            turn_kind = "retrieve_miss" if not sources else "retrieve"
        save_message(conversation_id, "user", user_text, kind=turn_kind)
        save_message(conversation_id, "assistant", answer, kind=turn_kind)

        print(f"[{provider}] [{conversation_id[:8]}] Michelle responded: {answer}")
        payload = {
            "answer": answer,
            "conversation_id": conversation_id,
            "user_id": user_id,
            "intent": intent,
            "remembered": remembered,
            "asked_to_remember": asked_to_remember,
        }
        if intent == "RETRIEVE":
            payload["sources"] = sources
        return payload
    except Exception as e:
        print(f"Error: {e}")
        return {
            "answer": "Sorry, I am having some trouble with this right now. Please try again later.",
            "conversation_id": conversation_id,
            "user_id": user_id,
        }
