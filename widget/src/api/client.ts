/**
 * Widget backend client.
 *
 * Talks to the Neryva conversations API with the tenant-bound API key.
 * The key is supplied by the host page via the `api-key` attribute; embedding
 * a key in customer HTML is acceptable for private deployments and is a
 * documented limitation (a public-token/turn-based flow is the production
 * path for untrusted pages).
 */

export interface SendMessageResult {
  response: string;
  confidence: number;
  handoffRequired: boolean;
  sessionId: string;
}

export interface WidgetApiError {
  status: number;
  detail: string;
}

export async function sendMessage(options: {
  apiBase: string;
  tenantSlug: string;
  apiKey: string;
  message: string;
  sessionId: string;
  signal?: AbortSignal;
}): Promise<SendMessageResult> {
  const { apiBase, tenantSlug, apiKey, message, sessionId, signal } = options;

  let response: Response;
  try {
    response = await fetch(`${apiBase}/v1/conversations`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-API-Key': apiKey,
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
    detail?: string;
  } | null;

  if (!response.ok) {
    const detail =
      (payload && typeof payload.detail === 'string' ? payload.detail : undefined) ??
      `Request failed with status ${response.status}`;
    throw new WidgetApiError(response.status, detail);
  }

  return {
    response: payload?.response ?? '',
    confidence: payload?.confidence ?? 0,
    handoffRequired: payload?.handoff_required ?? false,
    sessionId: payload?.session_id ?? sessionId,
  };
}

export class WidgetApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = 'WidgetApiError';
    this.status = status;
    this.detail = detail;
  }
}
