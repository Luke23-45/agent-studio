import { Link } from '@tanstack/react-router';
import { useAuthStore } from '../../lib/auth/session';
import { clearStoredApiKey } from '../../lib/auth/storage';

export function Header() {
  const principal = useAuthStore((s) => s.principal);
  const clear = useAuthStore((s) => s.clear);

  function handleLogout() {
    clearStoredApiKey();
    clear();
  }

  const displayName = principal?.name ?? 'Session';
  const roleLabel = principal?.role ?? 'unknown';
  const initial = displayName.charAt(0).toUpperCase() || 'A';

  return (
    <header className="bg-white shadow-sm border-b">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex justify-between h-16">
          <div className="flex items-center">
            <Link to="/" className="flex-shrink-0 flex items-center">
              <span className="text-xl font-bold text-blue-600">Neryva</span>
              <span className="ml-2 text-sm text-gray-500">Agent Studio</span>
            </Link>
          </div>
          <div className="flex items-center space-x-4">
            <div className="flex items-center space-x-2">
              <div className="w-8 h-8 bg-blue-600 rounded-full flex items-center justify-center text-white text-sm font-medium">
                {initial}
              </div>
              <div className="text-left">
                <div className="text-sm font-medium text-gray-900">{displayName}</div>
                <div className="text-xs text-gray-500">{roleLabel}</div>
              </div>
            </div>
            <button
              onClick={handleLogout}
              className="text-sm text-gray-500 hover:text-gray-900"
            >
              Sign out
            </button>
          </div>
        </div>
      </div>
    </header>
  );
}
