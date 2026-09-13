import { useCallback, useRef, useState } from "react";
import { askMicrophone, micStart, micStatus, micStop } from "@/ipc/electron";

export function useMicCapture() {
  const [recording, setRecording] = useState(false);
  const micGrantedRef = useRef(false);

  const ensureMic = useCallback(async (): Promise<boolean> => {
    if (micGrantedRef.current) return true;
    const status = micStatus();
    if (status.granted) {
      micGrantedRef.current = true;
      return true;
    }
    if (status.status === "not-determined") {
      const perm = await askMicrophone();
      micGrantedRef.current = !!perm.granted;
      return micGrantedRef.current;
    }
    return false;
  }, []);

  const startRecording = useCallback(async (): Promise<boolean> => {
    const ok = await ensureMic();
    if (!ok) return false;
    const out = await micStart();
    if (!out.ok) return false;
    setRecording(true);
    return true;
  }, [ensureMic]);

  const stopRecording = useCallback(async (): Promise<Blob | null> => {
    setRecording(false);
    return micStop();
  }, []);

  return { recording, startRecording, stopRecording, ensureMic };
}
