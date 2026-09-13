import { useCallback, useEffect, useRef, useState } from "react";
import {
  confirmAction,
  isOpenEmailComposer,
  sendChat,
  submitDraft,
  type ChatResponse,
  type ComposerDraft,
  type HistoryMessage,
} from "@/api/michelle";
import {
  EmailComposer,
  type ComposerFields,
  type EmailComposerHandle,
} from "@/components/EmailComposer/EmailComposer";
import { revealScramble } from "@/lib/scramble";

type ChatModeProps = {
  conversationId: string | null;
  userId: string | null;
  setConversationId: (id: string) => void;
  setUserId: (id: string) => void;
  greeting?: string;
  seedMessages?: HistoryMessage[];
  restoredComposer?: ComposerFields | null;
};

type Bubble = HistoryMessage & { id: string; pending?: boolean };

const GREETING_ID = "greeting-bubble";
const DEFAULT_GREETING = "What's up?";

function bubbleId(prefix: string, index: number): string {
  return `${prefix}-${index}-${Date.now()}`;
}

function greetingBubble(text: string): Bubble {
  return { id: GREETING_ID, role: "assistant", content: text };
}

function fieldsFromResponse(data: ChatResponse): ComposerFields {
  const resolved = data.resolved_params || {};
  return {
    recipient: resolved.recipient || "",
    subject: resolved.subject || "",
    body: resolved.body || "",
  };
}

export function ChatMode({
  conversationId,
  userId,
  setConversationId,
  setUserId,
  greeting,
  seedMessages,
  restoredComposer,
}: ChatModeProps) {
  const greetingText = greeting || DEFAULT_GREETING;
  const [messages, setMessages] = useState<Bubble[]>(() => [greetingBubble(greetingText)]);
  const [input, setInput] = useState("");
  const [pendingAction, setPendingAction] = useState<{ taskId: string; afterId: string } | null>(
    null
  );
  const [composer, setComposer] = useState<ComposerFields | null>(restoredComposer ?? null);
  const [composerKey, setComposerKey] = useState(0);
  const contentRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<EmailComposerHandle | null>(null);
  const scrambledFor = useRef<string | null>(null);
  const scrambleTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const stickToBottom = useRef(false);
  const seededRef = useRef<HistoryMessage[] | undefined>(undefined);

  useEffect(() => {
    if (restoredComposer) {
      setComposer(restoredComposer);
      setComposerKey((n) => n + 1);
    }
  }, [restoredComposer]);

  useEffect(() => {
    setMessages((prev) => {
      if (!prev[0] || prev[0].id !== GREETING_ID) return prev;
      if (prev[0].content === greetingText) return prev;
      return [greetingBubble(greetingText), ...prev.slice(1)];
    });
    scrambledFor.current = null;
  }, [greetingText]);

  useEffect(() => {
    if (!seedMessages || seedMessages.length === 0) return;
    if (seededRef.current === seedMessages) return;
    seededRef.current = seedMessages;
    setMessages((prev) => {
      const greetingRow =
        prev[0]?.id === GREETING_ID ? prev[0] : greetingBubble(greetingText);
      const seeded = seedMessages.map((m, i) => ({ ...m, id: bubbleId("seed", i) }));
      return [greetingRow, ...seeded];
    });
    stickToBottom.current = true;
  }, [greetingText, seedMessages]);

  useEffect(() => {
    if (seedMessages && seedMessages.length > 0) return;
    if (scrambledFor.current === greetingText) return;
    const node = document.getElementById(GREETING_ID);
    if (!node) return;
    if (scrambleTimerRef.current !== null) clearInterval(scrambleTimerRef.current);
    scrambledFor.current = greetingText;
    node.innerText = "…";
    scrambleTimerRef.current = revealScramble(node, greetingText);
    return () => {
      if (scrambleTimerRef.current !== null) {
        clearInterval(scrambleTimerRef.current);
        scrambleTimerRef.current = null;
      }
    };
  }, [greetingText, seedMessages]);

  useEffect(() => {
    if (!stickToBottom.current) return;
    const el = contentRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, composer, pendingAction]);

  const applyChatResponse = useCallback(
    (data: ChatResponse, botId: string) => {
      if (data.conversation_id) setConversationId(data.conversation_id);
      if (data.user_id) setUserId(data.user_id);
      window.setTimeout(() => {
        setMessages((prev) =>
          prev.map((m) => (m.id === botId ? { ...m, content: data.answer, pending: false } : m))
        );
        const node = document.getElementById(botId);
        if (node) revealScramble(node, data.answer || "");
        if (data.confirm_required && data.task_id) {
          setComposer(null);
          setPendingAction({ taskId: data.task_id, afterId: botId });
        } else if (isOpenEmailComposer(data)) {
          setPendingAction(null);
          setComposer(fieldsFromResponse(data));
          setComposerKey((n) => n + 1);
        } else if (data.asked_to_save_draft || data.task_status !== "AWAITING_INPUT") {
          setComposer(null);
          setPendingAction(null);
        }
      }, 400);
    },
    [setConversationId, setUserId]
  );

  const handleSend = useCallback(async () => {
    const text = input.trim();
    if (!text) {
      if (composer && composerRef.current) {
        composerRef.current.submit();
      }
      return;
    }
    setInput("");
    setPendingAction(null);
    stickToBottom.current = true;
    const userBubble: Bubble = { id: bubbleId("user", messages.length), role: "user", content: text };
    const botBubble: Bubble = {
      id: bubbleId("bot", messages.length + 1),
      role: "assistant",
      content: "",
      pending: true,
    };
    setMessages((prev) => [...prev, userBubble, botBubble]);
    try {
      const data = await sendChat(text, conversationId, userId);
      applyChatResponse(data, botBubble.id);
    } catch {
      applyChatResponse({ answer: "System Error: Connection to backend lost." }, botBubble.id);
    }
  }, [applyChatResponse, composer, conversationId, input, messages.length, userId]);

  const handleComposerSubmit = useCallback(
    async (draft: ComposerDraft) => {
      setComposer(null);
      setPendingAction(null);
      stickToBottom.current = true;
      const botBubble: Bubble = {
        id: bubbleId("draft", messages.length),
        role: "assistant",
        content: "",
        pending: true,
      };
      setMessages((prev) => [...prev, botBubble]);
      try {
        const data = await submitDraft(draft, conversationId, userId);
        applyChatResponse(data, botBubble.id);
      } catch {
        applyChatResponse({ answer: "Something went wrong." }, botBubble.id);
      }
    },
    [applyChatResponse, conversationId, messages.length, userId]
  );

  const handleConfirm = async (decision: "confirm" | "cancel") => {
    if (!pendingAction) return;
    stickToBottom.current = true;
    const botBubble: Bubble = {
      id: bubbleId("confirm", messages.length),
      role: "assistant",
      content: "",
      pending: true,
    };
    setMessages((prev) => [...prev, botBubble]);
    setPendingAction(null);
    try {
      const data = await confirmAction(pendingAction.taskId, decision, conversationId, userId);
      applyChatResponse(data, botBubble.id);
    } catch {
      applyChatResponse({ answer: "Something went wrong." }, botBubble.id);
    }
  };

  return (
    <div className="flex flex-1 flex-col min-h-0">
      <div ref={contentRef} className="chat-content">
        {messages.map((m) => (
          <div
            key={m.id}
            id={m.id}
            className={`bubble ${m.role === "user" ? "bubble-user" : ""} ${
              m.pending ? "bubble-thinking" : ""
            }`}
          >
            {m.pending ? <span className="loading-dot" /> : m.content}
          </div>
        ))}
        {pendingAction ? (
          <div className="action-row">
            <button type="button" onClick={() => void handleConfirm("confirm")} className="action-btn">
              Confirm
            </button>
            <button
              type="button"
              onClick={() => void handleConfirm("cancel")}
              className="action-btn action-btn-cancel"
            >
              Cancel
            </button>
          </div>
        ) : null}
      </div>

      {composer ? (
        <EmailComposer
          key={composerKey}
          ref={composerRef}
          initial={composer}
          conversationId={conversationId}
          userId={userId}
          onSubmit={(draft) => void handleComposerSubmit(draft)}
        />
      ) : null}

      <div className="chat-input-area">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void handleSend();
          }}
          placeholder="Ask me anything..."
          className="chat-input"
        />
        <button type="button" onClick={() => void handleSend()} className="chat-send-btn">
          Send
        </button>
      </div>
    </div>
  );
}
