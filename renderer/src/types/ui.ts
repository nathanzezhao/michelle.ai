export type UiMode = "voice" | "chat" | "collapsed";

export type VoiceRecordingState = "idle" | "recording" | "processing";

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
};
