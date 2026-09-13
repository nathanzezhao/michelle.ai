import { useCallback, useEffect, useImperativeHandle, useRef, useState, forwardRef } from "react";
import { draftBodyFromAudio, syncDraft, type ComposerDraft } from "@/api/michelle";
import { useMicCapture } from "@/hooks/useMicCapture";

type NativeFile = File & { path?: string };

export type ComposerFields = {
  recipient: string;
  subject: string;
  body: string;
};

type EmailComposerProps = {
  initial: ComposerFields;
  conversationId: string | null;
  userId: string | null;
  onSubmit: (draft: ComposerDraft) => void;
};

type GenState =
  | "default"
  | "listening"
  | "transcribing"
  | "drafting"
  | "heard nothing"
  | "downloading"
  | "busy"
  | "whisper missing"
  | "mic blocked";

function genLabel(state: GenState): string {
  if (state === "default") return "tap";
  return state;
}

export type EmailComposerHandle = {
  submit: () => boolean;
};

export const EmailComposer = forwardRef<EmailComposerHandle, EmailComposerProps>(
  function EmailComposer({ initial, conversationId, userId, onSubmit }, ref) {
  const [recipient, setRecipient] = useState(initial.recipient);
  const [subject, setSubject] = useState(initial.subject);
  const [body, setBody] = useState(initial.body);
  const [urgent, setUrgent] = useState(/^\[urgent\]/i.test(initial.subject));
  const [files, setFiles] = useState<NativeFile[]>([]);
  const [heard, setHeard] = useState("");
  const [genState, setGenState] = useState<GenState>("default");
  const [undo, setUndo] = useState<{ body: string; fromTap: boolean } | null>(null);
  const bodyFromTapRef = useRef(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const toRef = useRef<HTMLInputElement>(null);
  const subjectRef = useRef<HTMLInputElement>(null);
  const bodyRef = useRef<HTMLTextAreaElement>(null);
  const syncTimer = useRef<number | null>(null);
  const liveRef = useRef({ recipient, subject, body, listening: false });
  const { startRecording, stopRecording } = useMicCapture();
  const stopRecordingRef = useRef(stopRecording);
  stopRecordingRef.current = stopRecording;

  liveRef.current = {
    recipient,
    subject,
    body,
    listening: genState === "listening",
  };

  useEffect(() => {
    toRef.current?.focus();
  }, []);

  const scheduleSync = useCallback(() => {
    if (syncTimer.current) window.clearTimeout(syncTimer.current);
    syncTimer.current = window.setTimeout(() => {
      const snap = liveRef.current;
      void syncDraft(
        { recipient: snap.recipient.trim(), subject: snap.subject.trim(), body: snap.body.trim() },
        conversationId,
        userId
      ).catch((err) => console.error("Draft sync failed:", err));
    }, 1500);
  }, [conversationId, userId]);

  useEffect(() => {
    return () => {
      if (syncTimer.current) window.clearTimeout(syncTimer.current);
      if (liveRef.current.listening) void stopRecordingRef.current();
    };
  }, []);

  const focusMissing = () => {
    if (!recipient.trim()) toRef.current?.focus();
    else if (!subject.trim()) subjectRef.current?.focus();
    else bodyRef.current?.focus();
  };

  const readDraft = (): ComposerDraft => ({
    recipient: recipient.trim(),
    subject: subject.trim(),
    body: body.trim(),
    attachments: files.map((f) => f.path).filter((p): p is string => Boolean(p)),
  });

  const trySubmit = (): boolean => {
    const draft = readDraft();
    if (!draft.recipient || !draft.subject || !draft.body) {
      focusMissing();
      return true;
    }
    if (syncTimer.current) window.clearTimeout(syncTimer.current);
    onSubmit(draft);
    return true;
  };

  useImperativeHandle(ref, () => ({ submit: trySubmit }));

  const toggleUrgent = () => {
    const next = !urgent;
    setUrgent(next);
    setSubject((prev) => {
      if (next) return /^\[urgent\]/i.test(prev) ? prev : prev ? `[urgent] ${prev}` : "[urgent] ";
      return prev.replace(/^\[urgent\]\s*/i, "");
    });
    scheduleSync();
  };

  const onTap = async () => {
    if (genState === "transcribing" || genState === "drafting") return;
    if (liveRef.current.listening) {
      const blob = await stopRecording();
      if (!blob) {
        setGenState("heard nothing");
        return;
      }
      setGenState("transcribing");
      const snap = liveRef.current;
      const out = await draftBodyFromAudio(
        blob,
        { recipient: snap.recipient, subject: snap.subject, body: snap.body },
        conversationId,
        userId,
        !bodyFromTapRef.current
      );
      if (out.error) {
        if (out.error === "heard_nothing") setGenState("heard nothing");
        else if (out.error === "downloading") setGenState("downloading");
        else if (out.error === "busy") setGenState("busy");
        else if (out.error === "whisper_missing") setGenState("whisper missing");
        else setGenState("default");
        return;
      }
      if (out.body) {
        setUndo({ body: snap.body, fromTap: bodyFromTapRef.current });
        setBody(out.body);
        bodyFromTapRef.current = false;
        setHeard((out.transcript || "").replace(/\s+/g, " ").trim());
        scheduleSync();
      }
      setGenState("default");
      return;
    }

    const started = await startRecording();
    if (!started) {
      setGenState("mic blocked");
      return;
    }
    setGenState("listening");
  };

  const busy = genState === "transcribing" || genState === "drafting";

  return (
    <>
      <div className="composer">
        <div className="composer-head">
          <span className="lbl">to</span>
          <input
            ref={toRef}
            className="composer-to"
            placeholder="name or email"
            value={recipient}
            onChange={(e) => {
              setRecipient(e.target.value);
              scheduleSync();
            }}
          />
          <button type="button" className={`urgent ${urgent ? "on" : ""}`} onClick={toggleUrgent}>
            urgent?
          </button>
          <span className="lbl">subject</span>
          <input
            ref={subjectRef}
            className="composer-subject"
            placeholder="what's this about"
            value={subject}
            onChange={(e) => {
              setSubject(e.target.value);
              setUrgent(/^\[urgent\]/i.test(e.target.value));
              scheduleSync();
            }}
          />
        </div>
        <div className="composer-body">
          <div className="lbl" style={{ marginBottom: 6 }}>
            body
          </div>
          <textarea
            ref={bodyRef}
            className="composer-body-text"
            placeholder="write it here, or tap the mic"
            value={body}
            onChange={(e) => {
              setBody(e.target.value);
              bodyFromTapRef.current = false;
              scheduleSync();
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                trySubmit();
              }
            }}
          />
          {heard ? <div className="composer-heard">{heard}</div> : null}
        </div>
      </div>

      <div id="composer-dock" className="open">
        {files.length > 0 ? (
          <div className="composer-files">
            {files.map((file, i) => (
              <div key={`${file.name}-${i}`} className="file-chip">
                <span>{file.name}</span>
                <button
                  type="button"
                  onClick={() => setFiles((prev) => prev.filter((_, idx) => idx !== i))}
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        ) : null}
        <div className="dock-row">
          <input
            ref={fileInputRef}
            type="file"
            multiple
            hidden
            onChange={(e) => {
              const next = Array.from(e.target.files || []) as NativeFile[];
              setFiles((prev) => [...prev, ...next]);
              e.target.value = "";
            }}
          />
          <button
            type="button"
            className="icon-btn"
            title="attach files"
            aria-label="files"
            onClick={() => fileInputRef.current?.click()}
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <path d="M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z" />
              <path d="M14 2v5a1 1 0 0 0 1 1h5" />
            </svg>
            files
          </button>
          <button
            type="button"
            className="icon-btn"
            title="undo last generate"
            aria-label="undo"
            disabled={!undo}
            onClick={() => {
              if (!undo) return;
              setBody(undo.body);
              bodyFromTapRef.current = undo.fromTap;
              setUndo(null);
              setHeard("");
            }}
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <path d="M3 7v6h6" />
              <path d="M21 17a9 9 0 0 0-9-9 9 9 0 0 0-6.7 2.9L3 13" />
            </svg>
            undo
          </button>
          <button
            type="button"
            className={`gen-btn ${genState === "listening" || genState === "transcribing" || genState === "drafting" ? genState : ""}`}
            title="tap to talk"
            aria-label="tap to talk"
            aria-pressed={genState === "listening"}
            aria-busy={busy}
            disabled={busy}
            onClick={() => void onTap()}
          >
            <span className="gen-pulse" aria-hidden />
            <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <path d="M12 19v3" />
              <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
              <rect x="9" y="2" width="6" height="13" rx="3" />
            </svg>
            <span className="gen-label">{genLabel(genState)}</span>
          </button>
        </div>
        <span className="sr-live" aria-live="polite">
          {genState === "default" ? "" : genLabel(genState)}
        </span>
      </div>
    </>
  );
  }
);
