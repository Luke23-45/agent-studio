import { useEffect } from 'react';
import { Outlet } from '@tanstack/react-router';
import { Header } from '../components/layout/Header';
import { Sidebar } from '../components/layout/Sidebar';
import { Login } from '../features/auth/Login';
import { apiErrorMessage } from '../lib/api/client';
import { fetchPrincipal } from '../lib/api/endpoints';
import { useAuthStore } from '../lib/auth/session';
import { clearStoredApiKey } from '../lib/auth/storage';

export function App() {
  const apiKey = useAuthStore((s) => s.apiKey);
  const principal = useAuthStore((s) => s.principal);
  const setPrincipal = useAuthStore((s) => s.setPrincipal);
  const setApiKey = useAuthStore((s) => s.setApiKey);
  const clear = useAuthStore((s) => s.clear);

  // Resolve the principal once when a key exists but no principal is known yet.
  useEffect(() => {
    if (!apiKey || principal) return;
    let cancelled = false;
    fetchPrincipal()
      .then((p) => {
        if (cancelled) return;
        setPrincipal({
          keyId: p.key_id,
          name: p.name,
          role: p.role,
          tenantId: p.tenant_id,
          scopes: p.scopes,
          authEnabled: p.auth_enabled,
        });
      })
      .catch((err) => {
        if (cancelled) return;
        clearStoredApiKey();
        setApiKey(null);
        setPrincipal(null);
        console.error('principal resolution failed:', apiErrorMessage(err));
      });
    return () => {
      cancelled = true;
    };
  }, [apiKey, principal, setApiKey, setPrincipal]);

  // Any 401/403 from the API layer drops the session.
  useEffect(() => {
    const onAuthError = () => clear();
    window.addEventListener('neryva:auth-error', onAuthError);
    return () => window.removeEventListener('neryva:auth-error', onAuthError);
  }, [clear]);

  if (!apiKey) {
    return <Login />;
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <Header />
      <div className="flex">
        <Sidebar />
        <main className="flex-1 p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
