/** Chat message model shared by the widget UI and runtime (P7-1). */

export type MessageRole = 'user' | 'assistant' | 'system';

export interface ChatMessage {
  id: string;
  role: MessageRole;
  content: string;
  /** True when the message is still being streamed from the backend. */
  streaming?: boolean;
  error?: boolean;
  budget?: boolean;
  /** Feedback state — null means no feedback submitted yet. */
  feedback?: 'up' | 'down' | null;
  /** Thread-level message seq (set from the backend response). */
  seq?: number;
}

export interface ConversationTurn {
  role: 'user' | 'assistant';
  content: string;
}

let _idCounter = 0;

function makeId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID();
  }
  return `msg-${Date.now()}-${(++_idCounter).toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

export function createMessage(
  role: MessageRole,
  content: string,
  streaming = false,
): ChatMessage {
  return { id: makeId(), role, content, streaming, feedback: null };
}

export function appendMessage(messages: ChatMessage[], message: ChatMessage): ChatMessage[] {
  return [...messages, message];
}

/** Update the last message in-place (used during SSE streaming). */
export function updateLastMessage(
  messages: ChatMessage[],
  updater: (msg: ChatMessage) => ChatMessage,
): ChatMessage[] {
  if (messages.length === 0) return messages;
  const copy = [...messages];
  copy[copy.length - 1] = updater(copy[copy.length - 1]);
  return copy;
}

export function createErrorBubble(error: string): ChatMessage {
  return { id: makeId(), role: 'assistant', content: error, error: true, feedback: null };
}

/** Distinct surface-level budget rejection bubble (P5-7): the request was
 * refused by the budget gate, not failed by an outage. */
export function createBudgetErrorBubble(error: string): ChatMessage {
  return {
    id: makeId(),
    role: 'assistant',
    content: error,
    error: true,
    budget: true,
    feedback: null,
  };
}

/** Last N turns for the session payload (optional, kept minimal for now). */
export function lastTurns(messages: ChatMessage[], count = 10): ConversationTurn[] {
  return messages
    .filter((m): m is ChatMessage & { role: 'user' | 'assistant' } =>
      m.role === 'user' || m.role === 'assistant',
    )
    .slice(-count)
    .map((m) => ({ role: m.role, content: m.content }));
}
