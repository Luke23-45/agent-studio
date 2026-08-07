/**
 * Sign-in screen.
 *
 * Neryva authenticates with scoped API keys (`X-API-Key` header), so "login"
 * is entering a key. When OIDC SSO is enabled on the deployment, an
 * operator can instead sign in with the identity provider; the flow lands
 * back here with a one-time `?code=`, which is exchanged for an operator
 * session token (never displayed in the URL).
 */

import { useEffect, useState } from 'react';
import { apiErrorMessage } from '../../lib/api/client';
import { exchangeOidcCode, fetchOidcStatus, fetchPrincipal } from '../../lib/api/endpoints';
import { useAuthStore } from '../../lib/auth/session';
import { storeApiKey, storeOperatorToken, storeTenantId } from '../../lib/auth/storage';

export function Login() {
  const setApiKey = useAuthStore((s) => s.setApiKey);
  const setPrincipal = useAuthStore((s) => s.setPrincipal);
  const [value, setValue] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [oidcEnabled, setOidcEnabled] = useState(false);
  const [ssoBusy, setSsoBusy] = useState(false);

  // Detect OIDC availability and a returning SSO callback (?code=).
  useEffect(() => {
    let cancelled = false;
    const params = new URLSearchParams(window.location.search);

    async function run() {
      try {
        const status = await fetchOidcStatus();
        if (cancelled) return;
        setOidcEnabled(status.enabled);
      } catch {
        setOidcEnabled(false);
      }
    }
    run();

    const code = params.get('code');
    const errorParam = params.get('error');
    if (errorParam) {
      setError(`SSO sign-in failed: ${errorParam}`);
    } else if (code) {
      setSsoBusy(true);
      exchangeOidcCode(code)
        .then((result) => {
          if (cancelled) return;
          storeOperatorToken(result.token);
          if (result.principal.tenantId) storeTenantId(result.principal.tenantId);
          setPrincipal({
            keyId: result.principal.keyId,
            name: result.principal.name,
            role: result.principal.role,
            tenantId: result.principal.tenantId,
            scopes: result.principal.scopes,
            authEnabled: result.principal.authEnabled,
          });
          // Strip the one-time code from the URL so a refresh cannot replay it.
          window.history.replaceState({}, document.title, window.location.pathname);
        })
        .catch((err) => {
          if (cancelled) return;
          setError(apiErrorMessage(err));
        })
        .finally(() => {
          if (!cancelled) setSsoBusy(false);
        });
    }
    return () => {
      cancelled = true;
    };
  }, [setPrincipal]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const apiKey = value.trim();
    if (!apiKey) return;

    setSubmitting(true);
    setError(null);
    try {
      // Store first so the client attaches the key to the /auth/me request.
      storeApiKey(apiKey);
      const principal = await fetchPrincipal();
      setApiKey(apiKey);
      setPrincipal({
        keyId: principal.key_id,
        name: principal.name,
        role: principal.role,
        tenantId: principal.tenant_id,
        scopes: principal.scopes,
        authEnabled: principal.auth_enabled,
      });
      if (principal.tenant_id) storeTenantId(principal.tenant_id);
    } catch (err) {
      setError(apiErrorMessage(err));
      setApiKey(null);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="min-h-screen bg-gray-50 flex items-center justify-center">
      <div className="bg-white rounded-lg shadow p-8 w-full max-w-sm">
        <h1 className="text-xl font-bold text-gray-900">Neryva Agent Studio</h1>
        <p className="mt-1 text-sm text-gray-600">
          Enter your API key to sign in.
        </p>
        <form onSubmit={handleSubmit} className="mt-6 space-y-4">
          <input
            type="password"
            autoFocus
            placeholder="nrv_live_…"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
          {error && <p className="text-sm text-red-600">{error}</p>}
          <button
            type="submit"
            disabled={submitting || !value.trim()}
            className="w-full bg-blue-600 text-white px-4 py-2 rounded-md text-sm font-medium hover:bg-blue-700 disabled:opacity-50"
          >
            {submitting ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
        {oidcEnabled && (
          <>
            <div className="my-4 flex items-center gap-2">
              <div className="flex-1 border-t border-gray-200" />
              <span className="text-xs text-gray-400">or</span>
              <div className="flex-1 border-t border-gray-200" />
            </div>
            <a
              href="/api/v1/auth/oidc/authorize"
              className="block w-full text-center bg-white border border-gray-300 text-gray-700 px-4 py-2 rounded-md text-sm font-medium hover:bg-gray-50"
            >
              {ssoBusy ? 'Signing in with SSO…' : 'Sign in with SSO'}
            </a>
          </>
        )}
      </div>
    </div>
  );
}
