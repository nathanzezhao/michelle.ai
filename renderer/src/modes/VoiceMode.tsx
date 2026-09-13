import { useCallback, useEffect, useState } from "react";
import { sendChatVoice } from "@/api/michelle";
import { AiBlob } from "@/components/AiBlob/AiBlob";
import { useMicCapture } from "@/hooks/useMicCapture";
import type { VoiceRecordingState } from "@/types/ui";

type VoiceModeProps = {
  conversationId: string | null;
  userId: string | null;
  onOpenChat: (lastTranscript?: string, lastAnswer?: string) => void;
  registerCollapseGuard: (guard: (() => Promise<void> | void) | null) => void;
};

export function VoiceMode({
  conversationId,
  userId,
  onOpenChat,
  registerCollapseGuard,
}: VoiceModeProps) {
  const [voiceState, setVoiceState] = useState<VoiceRecordingState>("idle");
  const [statusText, setStatusText] = useState<string | undefined>();
  const { recording, startRecording, stopRecording, ensureMic } = useMicCapture();

  useEffect(() => {
    registerCollapseGuard(async () => {
      if (!recording && voiceState !== "recording") return;
      await stopRecording();
      setVoiceState("idle");
      setStatusText(undefined);
    });
    return () => registerCollapseGuard(null);
  }, [recording, registerCollapseGuard, stopRecording, voiceState]);

  const handleBlobClick = useCallback(async () => {
    if (voiceState === "processing") return;

    if (recording || voiceState === "recording") {
      setVoiceState("processing");
      setStatusText("Processing…");
      const blob = await stopRecording();
      if (!blob) {
        setVoiceState("idle");
        setStatusText("Didn't catch that");
        window.setTimeout(() => setStatusText(undefined), 2000);
        return;
      }

      try {
        const data = await sendChatVoice(blob, conversationId, userId);
        if (data.error === "heard_nothing") {
          setVoiceState("idle");
          setStatusText("Didn't catch that");
          window.setTimeout(() => setStatusText(undefined), 2000);
          return;
        }
        setVoiceState("idle");
        setStatusText(undefined);
        onOpenChat(data.transcript, data.answer);
      } catch {
        setVoiceState("idle");
        setStatusText("Connection error");
        window.setTimeout(() => setStatusText(undefined), 2000);
      }
      return;
    }

    const ok = await ensureMic();
    if (!ok) {
      setStatusText("Microphone blocked");
      window.setTimeout(() => setStatusText(undefined), 2000);
      return;
    }
    const started = await startRecording();
    if (!started) {
      setStatusText("Mic unavailable");
      window.setTimeout(() => setStatusText(undefined), 2000);
      return;
    }
    setVoiceState("recording");
    setStatusText("Listening…");
  }, [
    conversationId,
    ensureMic,
    onOpenChat,
    recording,
    startRecording,
    stopRecording,
    userId,
    voiceState,
  ]);

  return (
    <div className="flex flex-1 items-center justify-center min-h-0">
      <AiBlob state={voiceState} onClick={handleBlobClick} statusText={statusText} />
    </div>
  );
}
