import os
from typing import Optional, List
from uuid import UUID, uuid4

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import actions
from intent import (
    analyze_action_request,
    analyze_remember_request,
    assess_memory_worthiness,
    capture_introduced_name,
    classify_composer_dismiss,
    classify_intent,
    classify_memory_confirmation,
    maybe_promote_to_remember,
    parse_mixed_utterance,
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
actions.init_db()
# No replay after restart: stale open actions are closed out (SPEC-PIPELINE §4).
actions.startup_sweep()

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
    attachments: Optional[List[str]] = None


class DraftBodyRequest(BaseModel):
    recipient: str = ""
    subject: str = ""
    conversation_id: Optional[str] = None
    user_id: Optional[str] = None


class SessionStart(BaseModel):
    conversation_id: Optional[str] = None
    user_id: Optional[str] = None


class ActionDecision(BaseModel):
    """Confirm/Cancel button press (SPEC-PIPELINE §9.2) — never goes through
    the classifier: a button press is not natural language."""

    task_id: str
    decision: str
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


# --- ACTION engine helpers (SPEC-PIPELINE §4, §8–§10) ------------------------

# engine is lowercase and present on EVERY /chat turn so QA can assert routing.
ENGINE_BY_INTENT = {
    "CHAT": "chat",
    "RETRIEVE": "retrieve",
    "REMEMBER": "remember",
    "ACTION": "action",
}

COMPOSIO_NOT_CONNECTED_REPLY = (
    "I can't actually send email yet — Composio isn't connected. "
    "Once there's an API key I can."
)
UNSUPPORTED_ACTION_REPLY = (
    "I can't do that yet — right now I can open apps and send email, "
    "that's about it."
)
USE_BUTTONS_REPLY = "Use the Confirm or Cancel buttons below to send or cancel it."
SAVE_DRAFT_ASK = "Want to save that draft?"
SAVE_DRAFT_YES_REPLY = "Can't stash drafts yet — I'll just drop it."
SAVE_DRAFT_NO_REPLY = "All good — it's gone."

# In-memory only. No draft body is stored — yes just acknowledges the ask.
_pending_draft_asks: set[tuple[str, str]] = set()

_MISSING_PARAM_WORDS = {
    "app_name": "which app to open",
    "recipient": "who it's going to",
    "subject": "the subject",
    "body": "the body",
}


def _action_desc(action_type: str) -> str:
    return "email draft" if action_type == "send_email" else "open-app request"


def _task_fields(action: dict) -> dict:
    return {
        "task_id": action["action_id"],
        "task_status": action["status"],
        "action_type": action["action_type"],
        "risk": action["risk"],
        "confirm_required": action["status"] == "PENDING",
        "missing_params": action["missing_params"],
        "resolved_params": action.get("resolved_params") or {},
    }


def _missing_ask(action_type: str, missing: list) -> str:
    wanted = [_MISSING_PARAM_WORDS.get(p, p) for p in missing]
    if len(wanted) > 1:
        listed = ", ".join(wanted[:-1]) + " and " + wanted[-1]
    else:
        listed = wanted[0]
    if action_type == "send_email":
        return f"I can send that email — I just need {listed}."
    return f"Sure — I just need {listed}."


def _pending_summary(params: dict) -> str:
    return (
        f"Ready to send: to {params.get('recipient')}, "
        f"subject \"{params.get('subject')}\", body \"{params.get('body')}\". "
        "Send it?"
    )


def _exec_reply(action_type: str, params: dict, status: str, exec_result: dict) -> str:
    if status == "SUCCESS":
        if action_type == "open_app":
            return f"Opened {params.get('app_name')}."
        return f"Sent the email to {params.get('recipient')}."
    if exec_result.get("error") == "composio_not_connected":
        link = exec_result.get("connect_link")
        if link:
            return (
                "I can't send email until Gmail is connected. "
                f"Open this link to connect it: {link}"
            )
        return COMPOSIO_NOT_CONNECTED_REPLY
    if action_type == "open_app":
        return (
            f"I couldn't find an app called {params.get('app_name')} — "
            "nothing was opened."
        )
    return f"That didn't work — {exec_result.get('detail') or 'the action failed'}."


def _existing_files(paths) -> list[str]:
    files = []
    for path in paths or []:
        if isinstance(path, str) and os.path.isfile(path):
            files.append(path)
    return files


def _merge_attachments(action: dict | None, paths) -> dict | None:
    if action is None:
        return None
    files = _existing_files(paths)
    if not files:
        return action
    resolved = dict(action.get("resolved_params") or {})
    resolved["attachments"] = files
    return actions.update_action(action["action_id"], resolved_params=resolved)


def _settle_action(action: dict, note: str) -> tuple[dict, str]:
    """Move a non-terminal row to the state its params dictate (§4) and build
    the reply. Low-risk + complete executes NOW, same request."""
    action_type = action["action_type"]
    risk = action["risk"]
    resolved = action["resolved_params"]
    missing = action["missing_params"]
    if missing:
        action = actions.update_action(action["action_id"], status="AWAITING_INPUT")
        answer = note + _missing_ask(action_type, missing)
    elif risk == "high":
        action = actions.update_action(action["action_id"], status="PENDING")
        answer = note + _pending_summary(resolved)
    else:
        status, exec_result = actions.confirm_and_execute(action["action_id"])
        action = actions.get_action(action["action_id"])
        answer = note + _exec_reply(action_type, resolved, status, exec_result)
    return action, answer


def _queue_item(analysis: dict) -> dict:
    return {
        "action_type": analysis["action_type"],
        "resolved_params": analysis["resolved_params"],
        "missing_params": analysis["missing_params"],
    }


def _run_one_action(user_id, conversation_id, analysis, note="", queue=None):
    action = actions.create_action(
        user_id,
        conversation_id,
        analysis["action_type"],
        analysis["resolved_params"],
        analysis["missing_params"],
        "AWAITING_INPUT",
        queue=queue,
    )
    return _settle_action(action, note)


def _handle_action_intents(
    user_text: str,
    reply_history: list,
    history: list,
    long_term_facts: list,
    user_id: str,
    conversation_id: str,
    open_action: dict | None,
) -> tuple[dict, str]:
    """Run every action clause in the message. Chat filler gets a reply too.

    Low-risk complete actions execute now. High-risk / incomplete ones go
    PENDING or AWAITING_INPUT; extras of those are queued and started after
    Confirm on the current one.
    """
    parsed = parse_mixed_utterance(user_text)
    clauses = parsed["actions"] or [user_text]
    analyses = []
    for clause in clauses:
        analysis = analyze_action_request(clause, reply_history)
        print(
            f"action_clause={clause!r} type={analysis['action_type']} "
            f"resolved={analysis['resolved_params']} "
            f"missing={analysis['missing_params']}"
        )
        if analysis["action_type"] in actions.ACTION_WHITELIST:
            analyses.append(analysis)

    drop_note = ""
    if open_action:
        actions.cancel_action(open_action["action_id"])
        drop_note = f"Dropped the {_action_desc(open_action['action_type'])} — "

    if not analyses:
        return None, drop_note

    answer_parts: list[str] = []
    if drop_note:
        answer_parts.append(drop_note.rstrip(" —"))
    chat = parsed.get("chat") or ""
    if chat:
        try:
            answer_parts.append(ask_llm(chat, history, long_term_facts))
        except Exception as e:
            print(f"mixed-chat reply failed ({e})")

    immediate = []
    deferred = []
    for analysis in analyses:
        risk = actions.ACTION_WHITELIST[analysis["action_type"]]["risk"]
        if analysis["missing_params"] or risk == "high":
            deferred.append(analysis)
        else:
            immediate.append(analysis)

    last_action = None
    for analysis in immediate:
        last_action, piece = _run_one_action(
            user_id, conversation_id, analysis, note=""
        )
        answer_parts.append(piece)

    if deferred:
        first, rest = deferred[0], deferred[1:]
        queue = [_queue_item(item) for item in rest] or None
        last_action, piece = _run_one_action(
            user_id, conversation_id, first, note="", queue=queue
        )
        answer_parts.append(piece)

    answer = "\n\n".join(p for p in answer_parts if p)
    return last_action, answer
    """Move a non-terminal row to the state its params dictate (§4) and build
    the reply. Low-risk + complete executes NOW, same request."""
    action_type = action["action_type"]
    risk = action["risk"]
    resolved = action["resolved_params"]
    missing = action["missing_params"]
    if missing:
        action = actions.update_action(action["action_id"], status="AWAITING_INPUT")
        answer = note + _missing_ask(action_type, missing)
    elif risk == "high":
        action = actions.update_action(action["action_id"], status="PENDING")
        answer = note + _pending_summary(resolved)
    else:
        status, exec_result = actions.confirm_and_execute(action["action_id"])
        action = actions.get_action(action["action_id"])
        answer = note + _exec_reply(action_type, resolved, status, exec_result)
    return action, answer


def _action_turn_payload(
    answer: str,
    conversation_id: str,
    user_id: str,
    action: dict,
    intent_result: dict,
    attachments=None,
) -> dict:
    action = _merge_attachments(action, attachments) or action
    return {
        "answer": answer,
        "conversation_id": conversation_id,
        "user_id": user_id,
        "intent": "ACTION",
        "is_question": intent_result.get("is_question"),
        "kind": intent_result.get("kind"),
        "memory_score": intent_result.get("memory_score"),
        "docs_score": intent_result.get("docs_score"),
        "chat_score": intent_result.get("chat_score"),
        "remembered": [],
        "asked_to_remember": False,
        "engine": "action",
        **_task_fields(action),
    }


@app.post("/session/start")
def start_session(incoming_data: SessionStart):
    """Called when the Electron UI first opens (not on backend restart).

    If Michelle does not yet know this user's name, she asks once and stores
    it forever in long-term memory when they reply.
    """
    conversation_id = _valid_uuid(incoming_data.conversation_id)
    user_id = _valid_uuid(incoming_data.user_id)
    name = long_term_memory.get_fact(user_id, "name")
    if name and not long_term_memory.is_valid_name(name):
        long_term_memory.delete_fact(user_id, "name")
        name = None

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
            "engine": "chat",
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
            "engine": "chat",
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
                replace=True,
            )
            long_term_facts = long_term_memory.get_facts(user_id)

        draft_key = (user_id, conversation_id)
        if draft_key in _pending_draft_asks:
            confirmation = classify_memory_confirmation(user_text)
            if confirmation == "yes":
                _pending_draft_asks.discard(draft_key)
                save_message(conversation_id, "user", user_text)
                save_message(conversation_id, "assistant", SAVE_DRAFT_YES_REPLY)
                return {
                    "answer": SAVE_DRAFT_YES_REPLY,
                    "conversation_id": conversation_id,
                    "user_id": user_id,
                    "intent": "CHAT",
                    "remembered": [],
                    "asked_to_save_draft": False,
                    "engine": "chat",
                }
            if confirmation == "no":
                _pending_draft_asks.discard(draft_key)
                save_message(conversation_id, "user", user_text)
                save_message(conversation_id, "assistant", SAVE_DRAFT_NO_REPLY)
                return {
                    "answer": SAVE_DRAFT_NO_REPLY,
                    "conversation_id": conversation_id,
                    "user_id": user_id,
                    "intent": "CHAT",
                    "remembered": [],
                    "asked_to_save_draft": False,
                    "engine": "chat",
                }
            _pending_draft_asks.discard(draft_key)

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
                        replace=fact.get("key") == "name",
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
                        "engine": "chat",
                    }
                answer = (
                    "Cool — saved for future conversations ("
                    + _fact_bits(saved)
                    + ")."
                )
                save_message(conversation_id, "user", user_text)
                save_message(conversation_id, "assistant", answer)
                print(f"[{provider}] [{conversation_id[:8]}] confirmed remember")
                # A memory "yes" is NOT a topic change for a pending action —
                # any open action stays untouched (SPEC-PIPELINE §10-A).
                return {
                    "answer": answer,
                    "conversation_id": conversation_id,
                    "user_id": user_id,
                    "intent": "REMEMBER",
                    "remembered": saved,
                    "engine": "remember",
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
                    "engine": "chat",
                }
            # Unrelated follow-up → drop the pending ask and continue normally.
            long_term_memory.clear_pending_memory(user_id, conversation_id)

        # --- ACTION seams (SPEC-PIPELINE §10) --------------------------------
        open_action = actions.get_open_action(user_id, conversation_id)

        # Typed yes/no NEVER confirms or cancels an action — buttons are the
        # only confirm channel (§10-A). The action stays PENDING.
        if (
            open_action
            and open_action["status"] == "PENDING"
            and classify_memory_confirmation(user_text) is not None
        ):
            answer = USE_BUTTONS_REPLY
            save_message(conversation_id, "user", user_text, kind="action")
            save_message(conversation_id, "assistant", answer, kind="action")
            print(f"[{provider}] [{conversation_id[:8]}] typed yes/no at buttons")
            return _action_turn_payload(
                answer,
                conversation_id,
                user_id,
                open_action,
                {
                    "is_question": False,
                    "kind": "ACTION",
                    "memory_score": 0.0,
                    "docs_score": 0.0,
                    "chat_score": 0.0,
                },
            )

        # Classify on the same filtered history the reply model sees, so a
        # failed lookup does not bias the next intent toward that topic.
        reply_history = history_for_reply(history, user_text)
        intent_result = classify_intent(
            user_text, reply_history, long_term_facts
        )
        intent = intent_result["intent"]
        confidence = intent_result["confidence"]

        print(
            f"[{provider}] [{conversation_id[:8]}] "
            f"is_question={intent_result.get('is_question')} "
            f"kind={intent_result.get('kind')} "
            f"memory={intent_result.get('memory_score')} "
            f"docs={intent_result.get('docs_score')} "
            f"chat={intent_result.get('chat_score')} "
            f"intent={intent} ({confidence:.2f})"
        )

        # --- ACTION engine (SPEC-PIPELINE §3.2, §4, §10) ---------------------
        # One extra analyzer call, made ONLY on action turns — exactly like
        # analyze_remember_request on REMEMBER turns.
        action_note = ""
        if intent == "ACTION":
            action, answer = _handle_action_intents(
                user_text,
                reply_history,
                history,
                long_term_facts,
                user_id,
                conversation_id,
                open_action,
            )
            if action is not None:
                save_message(conversation_id, "user", user_text, kind="action")
                save_message(conversation_id, "assistant", answer, kind="action")
                print(
                    f"[{provider}] [{conversation_id[:8]}] "
                    f"action {action['action_id'][:8]} → {action['status']}"
                )
                return _action_turn_payload(
                    answer, conversation_id, user_id, action, intent_result,
                    attachments=incoming_data.attachments,
                )
            # Non-whitelisted order: no actions_log row, no execution path,
            # engine reports "chat" (§3.2, §8).
            action_note = ""
            if answer:
                action_note = f"\n\n({answer.rstrip('.')})"
            answer = UNSUPPORTED_ACTION_REPLY + action_note
            save_message(conversation_id, "user", user_text)
            save_message(conversation_id, "assistant", answer)
            return {
                "answer": answer,
                "conversation_id": conversation_id,
                "user_id": user_id,
                "intent": "ACTION",
                "is_question": intent_result.get("is_question"),
                "kind": intent_result.get("kind"),
                "memory_score": intent_result.get("memory_score"),
                "docs_score": intent_result.get("docs_score"),
                "chat_score": intent_result.get("chat_score"),
                "remembered": [],
                "asked_to_remember": False,
                "engine": "chat",
            }
        if open_action:
            if open_action["status"] == "AWAITING_INPUT":
                # Paused action: the analyzer (given the task context) decides
                # related vs. unrelated (§10-B).
                analysis = analyze_action_request(
                    user_text, reply_history, task_context=open_action
                )
                dismissed = bool(analysis.get("dismiss"))
                if (
                    not dismissed
                    and open_action["action_type"] == "send_email"
                    and not analysis.get("related")
                    and classify_composer_dismiss(user_text)
                ):
                    dismissed = True
                if (
                    dismissed
                    and open_action["action_type"] == "send_email"
                ):
                    actions.cancel_action(open_action["action_id"])
                    _pending_draft_asks.add((user_id, conversation_id))
                    save_message(conversation_id, "user", user_text, kind="action")
                    save_message(
                        conversation_id, "assistant", SAVE_DRAFT_ASK, kind="action"
                    )
                    print(
                        f"[{provider}] [{conversation_id[:8]}] "
                        f"action {open_action['action_id'][:8]} dismissed → "
                        "save-draft ask"
                    )
                    return {
                        "answer": SAVE_DRAFT_ASK,
                        "conversation_id": conversation_id,
                        "user_id": user_id,
                        "intent": "CHAT",
                        "is_question": False,
                        "kind": "CHAT",
                        "remembered": [],
                        "asked_to_remember": False,
                        "asked_to_save_draft": True,
                        "engine": "chat",
                    }
                if (
                    analysis.get("related")
                    and analysis["action_type"] == open_action["action_type"]
                ):
                    action = actions.update_action(
                        open_action["action_id"],
                        resolved_params=analysis["resolved_params"],
                        missing_params=analysis["missing_params"],
                    )
                    action, answer = _settle_action(action, "")
                    save_message(conversation_id, "user", user_text, kind="action")
                    save_message(conversation_id, "assistant", answer, kind="action")
                    print(
                        f"[{provider}] [{conversation_id[:8]}] "
                        f"action {action['action_id'][:8]} continued → "
                        f"{action['status']}"
                    )
                    return _action_turn_payload(
                        answer, conversation_id, user_id, action, intent_result,
                        attachments=incoming_data.attachments,
                    )
            # Unrelated turn: Michelle's open question dies quietly — cancel
            # the action, carry a one-line drop note, handle the new message
            # normally, never nag about it later (§10-B).
            actions.cancel_action(open_action["action_id"])
            action_note = (
                f"\n\n(dropped the {_action_desc(open_action['action_type'])})"
            )
            print(
                f"[{provider}] [{conversation_id[:8]}] "
                f"dropped action {open_action['action_id'][:8]} (topic change)"
            )

        memory_result = assess_memory_worthiness(
            user_text,
            reply_history,
            long_term_facts,
            is_question=intent_result.get("is_question"),
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
            is_question=intent_result.get("is_question"),
        )
        if intent == "REMEMBER":
            intent_result["kind"] = "REMEMBER"
        # Replace an existing name only when they just introduced it, or
        # this turn is a REMEMBER store (meaning-based, not phrase-based).
        replace_name = bool(introduced) or intent == "REMEMBER"

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

            assessor_facts = usable_memory_facts(
                memory_result.get("facts"), user_text
            )
            store_facts = usable_memory_facts(
                analysis.get("store_facts"), user_text
            )

            # Question + no new fact in THIS message → recall, even if the
            # analyzer guessed "store" (llama does that on "rmbr my name?").
            storing_now = analysis["mode"] == "store" and (
                store_facts or assessor_facts
            )
            if intent_result.get("is_question") and not storing_now:
                # Only facts they asked about. Merging the whole CRM is why
                # "what's my name?" volunteered indigo.
                recall_facts = analysis["matched_facts"] or long_term_facts
                answer = ask_llm(
                    user_text,
                    history,
                    recall_facts,
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
                            replace=replace_name,
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
            chat_facts = usable_memory_facts(
                memory_result.get("facts"), user_text
            )
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
                        replace=replace_name,
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

        # A turn that dropped a paused action carries the note in passing;
        # the drop itself is only otherwise visible in actions_log (§10-B).
        if action_note:
            answer = f"{answer}{action_note}"

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
            "is_question": intent_result.get("is_question"),
            "kind": intent_result.get("kind"),
            "memory_score": intent_result.get("memory_score"),
            "docs_score": intent_result.get("docs_score"),
            "chat_score": intent_result.get("chat_score"),
            "remembered": remembered,
            "asked_to_remember": asked_to_remember,
            "engine": ENGINE_BY_INTENT.get(intent, "chat"),
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
            "engine": "chat",
        }


@app.post("/action/draft_body")
def draft_body(incoming_data: DraftBodyRequest):
    """Fill the composer body without touching actions_log or /chat."""
    user_id = _valid_uuid(incoming_data.user_id)
    recipient = (incoming_data.recipient or "").strip()
    subject = (incoming_data.subject or "").strip()
    prompt = (
        "Write only the body of a short email. No subject line, no markdown, "
        "no 'Subject:' prefix. 2 to 6 sentences.\n"
        f"To: {recipient or '(not given)'}\n"
        f"Subject: {subject or '(not given)'}"
    )
    try:
        body = ask_llm(
            prompt,
            history=[],
            long_term_facts=long_term_memory.get_facts(user_id),
        )
    except Exception as e:
        print(f"draft_body failed ({e})")
        return {"body": ""}
    return {"body": (body or "").strip()}


@app.post("/action/confirm")
def confirm_action(incoming_data: ActionDecision):
    """Confirm/Cancel button press for a PENDING action (SPEC-PIPELINE §9.2).

    Unknown task, mismatched user, non-PENDING status, or a bad decision →
    graceful reply, no state change, HTTP 200.
    """
    provider = os.getenv("LLM_PROVIDER", "mock")
    user_id = _valid_uuid(incoming_data.user_id)
    conversation_id = _valid_uuid(incoming_data.conversation_id)
    decision = (incoming_data.decision or "").strip().lower()
    action = actions.get_action(incoming_data.task_id)
    if action is not None and action["user_id"] != user_id:
        # A mismatched user must be indistinguishable from an unknown task_id:
        # leaking status/action_type/risk would confirm the task exists (§9.2).
        action = None

    def _confirm_payload(answer: str, action: dict | None) -> dict:
        payload = {
            "answer": answer,
            "conversation_id": conversation_id,
            "user_id": user_id,
            "intent": "ACTION",
            "engine": "action",
            "task_id": action["action_id"] if action else incoming_data.task_id,
            "task_status": action["status"] if action else "UNKNOWN",
            "action_type": action["action_type"] if action else None,
            "risk": action["risk"] if action else None,
            "confirm_required": bool(action and action["status"] == "PENDING"),
            "missing_params": (action.get("missing_params") or []) if action else [],
            "resolved_params": (action.get("resolved_params") or {}) if action else {},
        }
        return payload

    if (
        action is None
        or action["status"] != "PENDING"
        or decision not in ("confirm", "cancel")
    ):
        print(
            f"[{provider}] [{conversation_id[:8]}] stale/invalid confirm "
            f"task={incoming_data.task_id[:8]} decision={decision}"
        )
        return _confirm_payload(
            "That one's already done or cancelled — nothing to confirm.", action
        )

    # Valid press: history rows for the press and the outcome keep the
    # transcript coherent (§9.2). Use the action's own conversation.
    conversation_id = action["conversation_id"]
    save_message(conversation_id, "user", decision.capitalize(), kind="action")

    if decision == "cancel":
        action = actions.cancel_action(action["action_id"])
        answer = "Cancelled — I won't send it."
    else:
        queued = list(action.get("queue") or [])
        status, exec_result = actions.confirm_and_execute(action["action_id"])
        done = actions.get_action(action["action_id"])
        answer = _exec_reply(
            done["action_type"], done["resolved_params"], status, exec_result
        )
        if status == "SUCCESS" and queued:
            nxt = queued[0]
            rest = queued[1:]
            action, more = _run_one_action(
                done["user_id"],
                done["conversation_id"],
                {
                    "action_type": nxt["action_type"],
                    "resolved_params": nxt.get("resolved_params") or {},
                    "missing_params": nxt.get("missing_params") or [],
                },
                note="",
                queue=rest or None,
            )
            answer = f"{answer}\n\n{more}"
        else:
            action = done

    save_message(conversation_id, "assistant", answer, kind="action")
    print(
        f"[{provider}] [{conversation_id[:8]}] confirm {decision} → "
        f"{action['status']}: {answer}"
    )
    return _confirm_payload(answer, action)
