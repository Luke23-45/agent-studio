/**
 * SSE transport layer (Arch §9.1, P7-1).
 *
 * Wraps a fetch-based SSE consumer with:
 *  - `Last-Event-ID` reconnect on drop
 *  - exponential backoff (capped)
 *  - typed callback hooks (session, delta, redaction, complete, error)
 *  - AbortController integration for cancellation propagation
 *
 * Frame contract (backend `conversations.py` `_sse`):
 *
 *   id: <seq>
 *   event: session | guardrails | compaction | delta | redaction
 *          | retraction | heartbeat | result | error
 *   data: <json>
 *   <blank line>
 *
 * The JSON payload carries NO `type` field — the event name lives in the
 * `event:` line. `result` payloads are snake_case on the wire
 * (`handoff_required`, `thread_id`, …) and are mapped to camelCase here.
 */

export interface SSEStreamOptions {
  /** Full URL of the SSE endpoint (e.g., `/api/v1/conversations/stream`). */
  url: string;
  /** HTTP method — POST for conversations. */
  method: 'GET' | 'POST';
  /** Request headers (Bearer token, Content-Type, Idempotency-Key). */
  headers: Record<string, string>;
  /** JSON-serialisable request body (POST only). */
  body?: unknown;
  /** External abort signal (widget disconnect / cancel). */
  signal?: AbortSignal;

  /** Called for each text delta chunk from the assistant. */
  onDelta: (text: string) => void;
  /** Called once when the stream completes with the final payload. */
  onComplete: (payload: SSECompletePayload) => void;
  /** Called on transport or server error. */
  onError: (error: SSEError) => void;
  /** Called when the connection state changes (for typing indicator). */
  onStateChange?: (state: SSEConnectionState) => void;
  /** Called when the backend opens a session (thread_id/session_id). */
  onSession?: (payload: { sessionId: string; threadId: string | null }) => void;
  /**
   * Called when the backend retracts already-released deltas (output
   * moderation). `text` is the full safe response remaining after the
   * retraction — the UI must replace the displayed content.
   */
  onRedaction?: (text: string) => void;
}

export interface SSECompletePayload {
  response: string;
  threadId: string | null;
  sessionId: string;
  confidence: number;
  handoffRequired: boolean;
  /** The backend does not include usage in stream frames — always null. */
  usage: { inputTokens: number; outputTokens: number } | null;
}

export type SSEConnectionState = 'connecting' | 'streaming' | 'reconnecting' | 'closed';

export class SSEError extends Error {
  status: number;
  retryable: boolean;
  budgetRejected: boolean;

  constructor(
    status: number,
    detail: string,
    retryable = false,
    budgetRejected = false,
  ) {
    super(detail);
    this.name = 'SSEError';
    this.status = status;
    this.retryable = retryable;
    this.budgetRejected = budgetRejected;
  }
}

/** Maximum reconnection attempts before giving up. */
const MAX_RECONNECT_ATTEMPTS = 5;
/** Initial backoff delay in ms. */
const INITIAL_BACKOFF_MS = 500;
/** Maximum backoff delay in ms. */
const MAX_BACKOFF_MS = 16_000;

/**
 * Opens a fetch-based SSE stream (POST bodies are not supported by the
 * native EventSource API).
 *
 * Returns a cleanup function that aborts the connection.
 */
export function openSSEStream(options: SSEStreamOptions): () => void {
  const controller = new AbortController();
  const combinedSignal = options.signal
    ? combineSignals(options.signal, controller.signal)
    : controller.signal;

  let lastEventId = '';
  let reconnectAttempts = 0;
  let fullResponse = '';
  let sessionId = '';
  let threadId: string | null = null;
  let closed = false;
  /** Deltas keyed by their `id:` line, for retraction on redaction. */
  let releasedDeltas: Array<{ id: string; text: string }> = [];

  const connect = async (): Promise<void> => {
    if (closed || combinedSignal.aborted) return;

    options.onStateChange?.(reconnectAttempts > 0 ? 'reconnecting' : 'connecting');

    const headers: Record<string, string> = {
      Accept: 'text/event-stream',
      'Cache-Control': 'no-cache',
      ...options.headers,
    };
    if (lastEventId) {
      headers['Last-Event-ID'] = lastEventId;
    }

    let response: Response;
    try {
      response = await fetch(options.url, {
        method: options.method,
        headers,
        body: options.body != null ? JSON.stringify(options.body) : undefined,
        signal: combinedSignal,
      });
    } catch (err) {
      if (combinedSignal.aborted) return;
      return handleReconnect(
        new SSEError(0, err instanceof Error ? err.message : 'Network error', true),
      );
    }

    if (!response.ok) {
      const text = await response.text().catch(() => '');
      let detail = `Request failed with status ${response.status}`;
      try {
        const json = JSON.parse(text) as { detail?: string };
        if (json.detail) detail = json.detail;
      } catch {
        // non-JSON error body
      }
      const budgetRejected = response.status === 402;
      const retryable = response.status >= 500 || response.status === 429;
      if (retryable) {
        return handleReconnect(new SSEError(response.status, detail, true, budgetRejected));
      }
      options.onError(new SSEError(response.status, detail, false, budgetRejected));
      closed = true;
      return;
    }

    if (!response.body) {
      options.onError(new SSEError(0, 'Response body is null', false));
      closed = true;
      return;
    }

    // Successful connection resets reconnect counter
    reconnectAttempts = 0;
    options.onStateChange?.('streaming');

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (combinedSignal.aborted) break;

        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split(/\r?\n\r?\n/);
        buffer = frames.pop() ?? '';

        for (const frame of frames) {
          handleFrame(frame);
        }
      }
      // Flush any trailing frame without a closing blank line
      if (buffer.trim()) handleFrame(buffer);
    } catch (err) {
      if (combinedSignal.aborted) return;
      return handleReconnect(
        new SSEError(0, err instanceof Error ? err.message : 'Stream read error', true),
      );
    }

    // Clean stream end
    if (!closed) {
      closed = true;
      options.onStateChange?.('closed');
      // If we haven't received a [result] event, synthesise one from the
      // accumulated deltas (e.g., a stream that ended after redaction).
      options.onComplete({
        response: fullResponse,
        threadId,
        sessionId,
        confidence: 0,
        handoffRequired: false,
        usage: null,
      });
    }
  };

  /**
   * Parses one SSE frame (everything between blank lines) and dispatches
   * it. A frame may span multiple `data:` lines which are joined.
   */
  const handleFrame = (frame: string): void => {
    let frameEvent = '';
    let frameId = '';
    const dataLines: string[] = [];

    for (const rawLine of frame.split(/\r?\n/)) {
      const line = rawLine.trim();
      if (line.startsWith('id:')) {
        frameId = line.slice(3).trim();
        lastEventId = frameId;
      } else if (line.startsWith('event:')) {
        frameEvent = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        dataLines.push(line.slice(5).trimStart());
      }
      // Comments, retry:, and unknown lines are ignored
    }

    if (dataLines.length === 0) return;
    const data = dataLines.join('\n');
    if (data === '[DONE]') return;

    let payload: unknown;
    try {
      payload = JSON.parse(data);
    } catch {
      // Non-JSON data line — treat as raw text delta
      if (data) {
        fullResponse += data;
        options.onDelta(data);
      }
      return;
    }

    switch (frameEvent) {
      case 'session': {
        const p = payload as { session_id?: string; thread_id?: string };
        sessionId = p.session_id ?? '';
        threadId = p.thread_id ?? null;
        options.onSession?.({ sessionId, threadId });
        break;
      }
      case 'delta': {
        const p = payload as { content?: string };
        if (p.content) {
          fullResponse += p.content;
          if (frameId) releasedDeltas.push({ id: frameId, text: p.content });
          options.onDelta(p.content);
        }
        break;
      }
      case 'retraction': {
        // Output moderation: drop every released delta from
        // retract_from_event_id (null → everything) and resync the UI.
        const p = payload as { retract_from_event_id?: number | null };
        const threshold = p.retract_from_event_id ?? -1;
        releasedDeltas = releasedDeltas.filter((d) => {
          const id = parseInt(d.id, 10);
          return !(Number.isFinite(id) && id >= threshold);
        });
        fullResponse = releasedDeltas.map((d) => d.text).join('');
        options.onRedaction?.(fullResponse);
        break;
      }
      case 'result': {
        const p = payload as {
          response?: string;
          confidence?: number;
          handoff_required?: boolean;
          thread_id?: string;
        };
        closed = true;
        options.onStateChange?.('closed');
        options.onComplete({
          response: p.response ?? fullResponse,
          threadId: p.thread_id ?? threadId,
          sessionId,
          confidence: p.confidence ?? 0,
          handoffRequired: p.handoff_required ?? false,
          usage: null,
        });
        break;
      }
      case 'error': {
        const p = payload as {
          error?: string;
          status?: number;
          level?: string | null;
        };
        const status = p.status ?? 500;
        closed = true;
        options.onStateChange?.('closed');
        options.onError(
          new SSEError(
            status,
            p.error ?? 'Server error during streaming',
            status >= 500 || status === 429,
            status === 402 || p.level != null,
          ),
        );
        break;
      }
      case 'redaction':
      case 'guardrails':
      case 'compaction':
      case 'heartbeat':
      default:
        // Informational frames — no widget-visible action required.
        break;
    }
  };

  const handleReconnect = async (error: SSEError): Promise<void> => {
    if (closed || combinedSignal.aborted) return;

    reconnectAttempts++;
    if (reconnectAttempts > MAX_RECONNECT_ATTEMPTS) {
      options.onError(
        new SSEError(
          error.status,
          `Connection failed after ${MAX_RECONNECT_ATTEMPTS} attempts: ${error.message}`,
          false,
        ),
      );
      closed = true;
      options.onStateChange?.('closed');
      return;
    }

    const delay = Math.min(
      INITIAL_BACKOFF_MS * Math.pow(2, reconnectAttempts - 1),
      MAX_BACKOFF_MS,
    );
    options.onStateChange?.('reconnecting');
    await sleep(delay, combinedSignal);

    if (!combinedSignal.aborted) {
      return connect();
    }
  };

  // Start the initial connection
  void connect();

  // Return cleanup function
  return () => {
    closed = true;
    controller.abort();
  };
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener('abort', () => {
      clearTimeout(timer);
      resolve();
    }, { once: true });
  });
}

/**
 * Combine two AbortSignals: the returned signal fires when *either* fires.
 */
function combineSignals(a: AbortSignal, b: AbortSignal): AbortSignal {
  const controller = new AbortController();
  const onAbort = () => controller.abort();
  a.addEventListener('abort', onAbort, { once: true });
  b.addEventListener('abort', onAbort, { once: true });
  if (a.aborted || b.aborted) controller.abort();
  return controller.signal;
}
