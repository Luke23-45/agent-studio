/**
 * API key entry screen.
 *
 * Neryva authenticates with scoped API keys (`X-API-Key` header), so "login"
 * is entering a key. On success the principal from `GET /auth/me` is stored
 * and the admin console unlocks.
 */

import { useState } from 'react';
import { apiErrorMessage } from '../../lib/api/client';
import { fetchPrincipal } from '../../lib/api/endpoints';
import { useAuthStore } from '../../lib/auth/session';
import { storeApiKey, storeTenantId } from '../../lib/auth/storage';

export function Login() {
  const setApiKey = useAuthStore((s) => s.setApiKey);
  const setPrincipal = useAuthStore((s) => s.setPrincipal);
  const [value, setValue] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

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
      </div>
    </div>
  );
}
