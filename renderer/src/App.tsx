import { useCallback, useEffect, useState } from "react";
import { startSession, isOpenEmailComposer, type HistoryMessage } from "@/api/michelle";
import type { ComposerFields } from "@/components/EmailComposer/EmailComposer";
import { AudioLinesIcon } from "@/components/icons/AudioLinesIcon";
import { MessagesCircleIcon } from "@/components/icons/MessagesCircleIcon";
import { ShellHeader } from "@/components/ShellHeader";
import { useShellCollapse } from "@/hooks/useShellCollapse";
import { setUiMode } from "@/ipc/electron";
import { ChatMode } from "@/modes/ChatMode";
import { CollapsedMode } from "@/modes/CollapsedMode";
import { VoiceMode } from "@/modes/VoiceMode";
import type { UiMode } from "@/types/ui";

const CONVERSATION_STORAGE_KEY = "michelle_conversation_id";
const USER_STORAGE_KEY = "michelle_user_id";

export function App() {
  const [mode, setMode] = useState<UiMode>("chat");
  const [conversationId, setConversationId] = useState<string | null>(
    () => localStorage.getItem(CONVERSATION_STORAGE_KEY)
  );
  const [userId, setUserId] = useState<string | null>(() => localStorage.getItem(USER_STORAGE_KEY));
  const [greeting, setGreeting] = useState<string>("What's up?");
  const [seedMessages, setSeedMessages] = useState<HistoryMessage[] | undefined>();
  const [restoredComposer, setRestoredComposer] = useState<ComposerFields | null>(null);

  const {
    shellRef,
    contentOpacity,
    collapseBtnCircle,
    isAnimating,
    beginCollapse,
    beginExpand,
    crossfadeTo,
    registerCollapseGuard,
  } = useShellCollapse(mode, setMode);

  useEffect(() => {
    void startSession(conversationId, userId).then((data) => {
      if (data.conversation_id) {
        setConversationId(data.conversation_id);
        localStorage.setItem(CONVERSATION_STORAGE_KEY, data.conversation_id);
      }
      if (data.user_id) {
        setUserId(data.user_id);
        localStorage.setItem(USER_STORAGE_KEY, data.user_id);
      }
      if (data.greeting) setGreeting(data.greeting);
      if (isOpenEmailComposer(data)) {
        const resolved = data.resolved_params || {};
        setRestoredComposer({
          recipient: resolved.recipient || "",
          subject: resolved.subject || "",
          body: resolved.body || "",
        });
      }
    });
  }, []);

  useEffect(() => {
    setUiMode(mode);
  }, [mode]);

  useEffect(() => {
    document.body.classList.toggle("collapsed", mode === "collapsed");
    return () => document.body.classList.remove("collapsed");
  }, [mode]);

  const persistConversation = useCallback((id: string) => {
    setConversationId((prev) => (prev === id ? prev : id));
    localStorage.setItem(CONVERSATION_STORAGE_KEY, id);
  }, []);

  const persistUser = useCallback((id: string) => {
    setUserId((prev) => (prev === id ? prev : id));
    localStorage.setItem(USER_STORAGE_KEY, id);
  }, []);

  const openChat = useCallback(
    (transcript?: string, answer?: string) => {
      if (transcript && answer) {
        setSeedMessages([
          { role: "user", content: transcript },
          { role: "assistant", content: answer },
        ]);
      } else {
        setSeedMessages(undefined);
      }
      void crossfadeTo("chat");
    },
    [crossfadeTo]
  );

  const openVoice = useCallback(() => {
    setSeedMessages(undefined);
    void crossfadeTo("voice");
  }, [crossfadeTo]);

  const shellClass =
    mode === "collapsed" ? "app-shell collapsed" : mode === "voice" ? "app-shell voice" : "app-shell";

  return (
    <div ref={shellRef} className={shellClass}>
      <div
        className="flex min-h-0 flex-1 flex-col"
        style={{ display: mode === "collapsed" ? "none" : "flex" }}
      >
        <ShellHeader
          collapseCircle={collapseBtnCircle}
          onCollapse={() => void beginCollapse()}
          hidden={collapseBtnCircle}
          trailing={
            <div className="shell-trailing-fade" style={{ opacity: contentOpacity }}>
              {mode === "voice" ? (
                <button
                  type="button"
                  aria-label="Open chat"
                  onClick={() => openChat()}
                  disabled={isAnimating}
                  className="shell-icon-btn"
                >
                  <MessagesCircleIcon />
                </button>
              ) : (
                <button
                  type="button"
                  aria-label="Open listening mode"
                  onClick={openVoice}
                  disabled={isAnimating}
                  className="shell-icon-btn"
                >
                  <AudioLinesIcon />
                </button>
              )}
            </div>
          }
        />
        <div
          className={`mode-content flex flex-1 flex-col min-h-0 ${
            contentOpacity === 1 ? "mode-content--in" : ""
          }`}
          style={{ opacity: contentOpacity }}
        >
          {mode === "voice" ? (
            <div className="flex flex-1 flex-col min-h-0">
              <VoiceMode
                conversationId={conversationId}
                userId={userId}
                onOpenChat={openChat}
                registerCollapseGuard={registerCollapseGuard}
              />
            </div>
          ) : null}
          <div
            className="flex flex-1 flex-col min-h-0"
            style={{ display: mode === "voice" ? "none" : "flex" }}
          >
            <ChatMode
              conversationId={conversationId}
              userId={userId}
              setConversationId={persistConversation}
              setUserId={persistUser}
              greeting={greeting}
              seedMessages={seedMessages}
              restoredComposer={restoredComposer}
            />
          </div>
        </div>
      </div>
      {mode === "collapsed" ? <CollapsedMode onExpand={() => void beginExpand()} /> : null}
    </div>
  );
}
