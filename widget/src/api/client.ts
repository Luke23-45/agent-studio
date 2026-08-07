/**
 * Widget backend client (P7-1, Arch §5, §6.4, §9.1).
 *
 * Talks to the Neryva backend using **session tokens** — NOT API keys.
 * The legacy `api-key` attribute is removed; authentication flows through
 * `POST /session-tokens` (anonymous bootstrap) or a previously minted
 * session token passed via the `token` attribute.
 *
 * Messages use SSE streaming (via the transport layer) by default,
 * with a non-streaming fallback for environments where SSE is blocked.
 */

import {
  openSSEStream,
  SSECompletePayload,
  SSEError,
} from '../transport/sse';

// -----------------------------------------------------------------------
// Session-token bootstrap (Arch §6.4, P1-8)
// -----------------------------------------------------------------------

/** Per-device identity key in localStorage (stable across page loads). */
const DEVICE_ID_KEY = 'neryva:device_id';
/** Cached session token (short-lived, refreshed on expiry). */
const SESSION_TOKEN_KEY = 'neryva:session_token';
const SESSION_END_USER_KEY = 'neryva:end_user_id';
const SESSION_EXPIRY_KEY = 'neryva:token_expires_at';

export interface SessionTokenResult {
  token: string;
  endUserId: string;
  expiresAt: string;
}

/**
 * Returns a stable per-device identity. Generated once and persisted in
 * localStorage so the same anonymous user keeps the same identity across
 * page loads (Arch §6.4: per-browser/per-device anonymous identity).
 */
function getOrCreateDeviceId(): string {
  try {
    const existing = localStorage.getItem(DEVICE_ID_KEY);
    if (existing) return existing;
    const id =
      typeof crypto !== 'undefined' && 'randomUUID' in crypto
        ? crypto.randomUUID()
        : `dev-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    localStorage.setItem(DEVICE_ID_KEY, id);
    return id;
  } catch {
    // localStorage blocked (incognito, CSP) — ephemeral device id
    return `dev-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
  }
}

/** Read a previously cached session token if it has not expired. */
function getCachedToken(): SessionTokenResult | null {
  try {
    const token = localStorage.getItem(SESSION_TOKEN_KEY);
    const endUserId = localStorage.getItem(SESSION_END_USER_KEY);
    const expiresAt = localStorage.getItem(SESSION_EXPIRY_KEY);
    if (!token || !endUserId || !expiresAt) return null;
    // Expire 30 s early to avoid edge-case races.
    if (new Date(expiresAt).getTime() - 30_000 < Date.now()) return null;
    return { token, endUserId, expiresAt };
  } catch {
    return null;
  }
}

function cacheToken(result: SessionTokenResult): void {
  try {
    localStorage.setItem(SESSION_TOKEN_KEY, result.token);
    localStorage.setItem(SESSION_END_USER_KEY, result.endUserId);
    localStorage.setItem(SESSION_EXPIRY_KEY, result.expiresAt);
  } catch {
    // localStorage blocked — token is ephemeral this session
  }
}

/**
 * Bootstrap a session token for this device.
 *
 * Calls `POST /v1/session-tokens` with the per-device identity and the
 * tenant+surface context. The backend mints a short-lived bearer token
 * bound to (tenant, surface, end_user, device). No API key is ever
 * exposed to the customer page (Arch §6.4).
 */
export async function bootstrapSessionToken(options: {
  apiBase: string;
  tenantId: string;
  surfaceId?: string;
}): Promise<SessionTokenResult> {
  // Try the cache first
  const cached = getCachedToken();
  if (cached) return cached;

  const deviceId = getOrCreateDeviceId();
  const previousEndUserId = (() => {
    try {
      return localStorage.getItem(SESSION_END_USER_KEY) ?? undefined;
    } catch {
      return undefined;
    }
  })();

  const response = await fetch(`${options.apiBase}/v1/session-tokens`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      tenant_id: options.tenantId,
      device_id: deviceId,
      surface_id: options.surfaceId ?? null,
      end_user_id: previousEndUserId ?? null,
      scopes: ['conversations:read', 'conversations:write'],
    }),
  });

  if (!response.ok) {
    const detail = await response.text().catch(() => 'Token bootstrap failed');
    throw new WidgetApiError(response.status, detail);
  }

  const data = (await response.json()) as {
    token: string;
    end_user_id: string;
    expires_at: string;
  };

  const result: SessionTokenResult = {
    token: data.token,
    endUserId: data.end_user_id,
    expiresAt: data.expires_at,
  };
  cacheToken(result);
  return result;
}

// -----------------------------------------------------------------------
// Streaming conversation client
// -----------------------------------------------------------------------

export interface StreamMessageOptions {
  apiBase: string;
  tenantSlug: string;
  token: string;
  message: string;
  sessionId: string;
  surfaceId?: string;
  signal?: AbortSignal;

  /** Called for each text delta chunk. */
  onDelta: (text: string) => void;
  /** Called once when the stream completes. */
  onComplete: (result: SendMessageResult) => void;
  /** Called on error. */
  onError: (error: WidgetApiError) => void;
  /** Called when the backend opens the session (thread_id/session_id). */
  onSession?: (payload: { sessionId: string; threadId: string | null }) => void;
  /** Called when released deltas are retracted (output moderation). */
  onRedaction?: (text: string) => void;
}

export interface SendMessageResult {
  response: string;
  confidence: number;
  handoffRequired: boolean;
  sessionId: string;
  threadId: string | null;
}

/**
 * Send a message and stream the response via SSE (Arch §9.1).
 *
 * Returns a cleanup function that aborts the connection.
 */
export function streamMessage(options: StreamMessageOptions): () => void {
  const idempotencyKey =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : `req-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;

  return openSSEStream({
    url: `${options.apiBase}/v1/conversations/stream`,
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${options.token}`,
      'Idempotency-Key': idempotencyKey,
    },
    body: {
      tenant_slug: options.tenantSlug,
      message: options.message,
      session_id: options.sessionId,
      surface_id: options.surfaceId ?? null,
    },
    signal: options.signal,
    onDelta: options.onDelta,
    onSession: options.onSession,
    onRedaction: options.onRedaction,
    onComplete: (payload: SSECompletePayload) => {
      options.onComplete({
        response: payload.response,
        confidence: payload.confidence,
        handoffRequired: payload.handoffRequired,
        sessionId: payload.sessionId || options.sessionId,
        threadId: payload.threadId,
      });
    },
    onError: (err: SSEError) => {
      options.onError(
        new WidgetApiError(err.status, err.message, err.budgetRejected),
      );
    },
  });
}

/**
 * Non-streaming fallback: sends a message via the regular POST endpoint.
 * Used when SSE is unavailable (e.g., some corporate proxies strip SSE).
 */
export async function sendMessage(options: {
  apiBase: string;
  tenantSlug: string;
  token: string;
  message: string;
  sessionId: string;
  signal?: AbortSignal;
}): Promise<SendMessageResult> {
  const { apiBase, tenantSlug, token, message, sessionId, signal } = options;

  let response: Response;
  try {
    response = await fetch(`${apiBase}/v1/conversations`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        tenant_slug: tenantSlug,
        message,
        session_id: sessionId,
      }),
      signal,
    });
  } catch (err) {
    throw new WidgetApiError(
      0,
      err instanceof Error ? err.message : 'Network request failed',
    );
  }

  const payload = (await response.json().catch(() => null)) as {
    response?: string;
    confidence?: number;
    handoff_required?: boolean;
    session_id?: string;
    thread_id?: string;
    detail?: string;
  } | null;

  if (!response.ok) {
    const detail =
      (payload && typeof payload.detail === 'string' ? payload.detail : undefined) ??
      `Request failed with status ${response.status}`;
    throw new WidgetApiError(response.status, detail, response.status === 402);
  }

  return {
    response: payload?.response ?? '',
    confidence: payload?.confidence ?? 0,
    handoffRequired: payload?.handoff_required ?? false,
    sessionId: payload?.session_id ?? sessionId,
    threadId: payload?.thread_id ?? null,
  };
}

// -----------------------------------------------------------------------
// Feedback capture (P7-1)
// -----------------------------------------------------------------------

export async function submitFeedback(options: {
  apiBase: string;
  token: string;
  threadId: string;
  messageId: string;
  rating: 'up' | 'down';
  comment?: string;
}): Promise<void> {
  await fetch(`${options.apiBase}/v1/threads/${options.threadId}/feedback`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${options.token}`,
    },
    body: JSON.stringify({
      message_id: options.messageId,
      rating: options.rating,
      comment: options.comment ?? null,
    }),
  });
}

// -----------------------------------------------------------------------
// Error class
// -----------------------------------------------------------------------

export class WidgetApiError extends Error {
  status: number;
  detail: string;
  budgetRejected: boolean;

  constructor(status: number, detail: string, budgetRejected = false) {
    super(detail);
    this.name = 'WidgetApiError';
    this.status = status;
    this.detail = detail;
    this.budgetRejected = budgetRejected;
  }
}
