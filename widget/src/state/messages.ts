/** Chat message model shared by the widget UI and runtime. */

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  error?: boolean;
}

export interface ConversationTurn {
  role: 'user' | 'assistant';
  content: string;
}

export function createMessage(role: ChatMessage['role'], content: string): ChatMessage {
  return {
    id: typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : `msg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    role,
    content,
  };
}

export function appendMessage(messages: ChatMessage[], message: ChatMessage): ChatMessage[] {
  return [...messages, message];
}

export function createErrorBubble(error: string): ChatMessage {
  return {
    id: typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : `msg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    role: 'assistant',
    content: error,
    error: true,
  };
}

/** Last N turns for the session payload (optional, kept minimal for now). */
export function lastTurns(messages: ChatMessage[], count = 10): ConversationTurn[] {
  return messages.slice(-count).map((m) => ({ role: m.role, content: m.content }));
}
