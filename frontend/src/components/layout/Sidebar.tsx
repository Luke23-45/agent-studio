import { Link, useLocation } from '@tanstack/react-router';
import { canViewEscalations, useAuthStore } from '../../lib/auth/session';

export function Sidebar() {
  const location = useLocation();
  const principal = useAuthStore((s) => s.principal);
  const role = principal?.role ?? '';

  const navigation = [
    { name: 'Dashboard', href: '/', visible: true },
    { name: 'Tenants', href: '/tenants', visible: true },
    { name: 'Policies', href: '/policies', visible: principal?.tenantId == null },
    { name: 'Traces', href: '/traces', visible: principal?.tenantId == null },
    { name: 'Evaluations', href: '/evaluations', visible: principal?.tenantId == null },
    { name: 'Handoffs', href: '/handoffs', visible: canViewEscalations(role) },
  ].filter((item) => item.visible);

  return (
    <aside className="w-64 bg-white border-r min-h-screen">
      <nav className="mt-6 px-4 space-y-2">
        {navigation.map((item) => {
          const isActive = location.pathname === item.href;
          return (
            <Link
              key={item.name}
              to={item.href as any}
              className={`block px-4 py-2 rounded-md text-sm font-medium transition-colors ${
                isActive
                  ? 'bg-blue-50 text-blue-700'
                  : 'text-gray-600 hover:bg-gray-50 hover:text-gray-900'
              }`}
            >
              {item.name}
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}
