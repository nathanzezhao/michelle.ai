const BASE = "http://127.0.0.1:8000";

export type SessionStartResponse = {
  conversation_id?: string;
  user_id?: string;
  greeting?: string;
  ask_name?: boolean;
  name?: string | null;
  action_type?: string;
  task_status?: string;
  resolved_params?: Record<string, string>;
  confirm_required?: boolean;
  task_id?: string;
};

export type ChatResponse = {
  answer: string;
  conversation_id?: string;
  user_id?: string;
  engine?: string;
  intent?: string;
  confirm_required?: boolean;
  task_id?: string;
  task_status?: string | null;
  action_type?: string | null;
  asked_to_save_draft?: boolean;
  resolved_params?: Record<string, string>;
  missing_params?: string[];
  error?: string;
  transcript?: string;
};

export type ComposerDraft = {
  recipient: string;
  subject: string;
  body: string;
  attachments?: string[];
};

export type HistoryMessage = {
  role: "user" | "assistant";
  content: string;
};

export async function startSession(
  conversationId: string | null,
  userId: string | null
): Promise<SessionStartResponse> {
  const r = await fetch(`${BASE}/session/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ conversation_id: conversationId, user_id: userId }),
  });
  return r.json();
}

export async function fetchHistory(
  conversationId: string,
  userId: string
): Promise<HistoryMessage[]> {
  const params = new URLSearchParams({ conversation_id: conversationId, user_id: userId });
  const r = await fetch(`${BASE}/session/history?${params.toString()}`);
  if (!r.ok) return [];
  const data = await r.json();
  return Array.isArray(data.messages) ? data.messages : [];
}

export async function sendChat(
  text: string,
  conversationId: string | null,
  userId: string | null
): Promise<ChatResponse> {
  const r = await fetch(`${BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, conversation_id: conversationId, user_id: userId }),
  });
  return r.json();
}

export async function sendChatVoice(
  audio: Blob,
  conversationId: string | null,
  userId: string | null
): Promise<ChatResponse> {
  const form = new FormData();
  form.append("audio", audio, "voice.wav");
  if (conversationId) form.append("conversation_id", conversationId);
  if (userId) form.append("user_id", userId);
  const r = await fetch(`${BASE}/chat/voice`, { method: "POST", body: form });
  return r.json();
}

export function isOpenEmailComposer(data: ChatResponse | SessionStartResponse): boolean {
  return data.action_type === "send_email" && data.task_status === "AWAITING_INPUT";
}

export async function syncDraft(
  draft: ComposerDraft,
  conversationId: string | null,
  userId: string | null
): Promise<ChatResponse> {
  const r = await fetch(`${BASE}/action/sync_draft`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      recipient: draft.recipient,
      subject: draft.subject,
      body: draft.body,
      conversation_id: conversationId,
      user_id: userId,
    }),
  });
  return r.json();
}

export async function submitDraft(
  draft: ComposerDraft,
  conversationId: string | null,
  userId: string | null
): Promise<ChatResponse> {
  const r = await fetch(`${BASE}/action/submit_draft`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      recipient: draft.recipient,
      subject: draft.subject,
      body: draft.body,
      conversation_id: conversationId,
      user_id: userId,
      attachments: draft.attachments ?? [],
    }),
  });
  return r.json();
}

export async function draftBodyFromAudio(
  audio: Blob,
  draft: ComposerDraft,
  conversationId: string | null,
  userId: string | null,
  bodyFromUser: boolean
): Promise<{ body?: string; transcript?: string; error?: string }> {
  const form = new FormData();
  form.append("recipient", draft.recipient);
  form.append("subject", draft.subject);
  form.append("body", draft.body);
  form.append("body_from_user", bodyFromUser ? "true" : "false");
  form.append("conversation_id", conversationId || "");
  form.append("user_id", userId || "");
  form.append("audio", audio, "hold.wav");
  const r = await fetch(`${BASE}/action/draft_body`, { method: "POST", body: form });
  return r.json();
}

export async function confirmAction(
  taskId: string,
  decision: "confirm" | "cancel",
  conversationId: string | null,
  userId: string | null
): Promise<ChatResponse> {
  const r = await fetch(`${BASE}/action/confirm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      task_id: taskId,
      decision,
      conversation_id: conversationId,
      user_id: userId,
    }),
  });
  return r.json();
}
