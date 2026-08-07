import { Link, useLocation } from '@tanstack/react-router';
import {
  canManageBilling,
  canViewAudit,
  canViewEscalations,
  isOperatorOrAbove,
  useAuthStore,
  ROLE_SUPER_ADMIN,
} from '../../lib/auth/session';

interface NavItem {
  name: string;
  href: string;
  icon: string;
  visible: boolean;
  section?: string;
  badge?: string;
}

export function Sidebar() {
  const location  = useLocation();
  const principal = useAuthStore((s) => s.principal);
  const role      = principal?.role ?? '';

  const isOperator    = isOperatorOrAbove(role);
  const isSuperAdmin  = role === ROLE_SUPER_ADMIN;

  const navigation: (NavItem | 'divider')[] = [
    // Core
    { name: 'Dashboard', href: '/', icon: '◈', visible: true },

    'divider',

    // Tenant & config
    { name: 'Tenants', href: '/tenants', icon: '🏢', visible: true, section: 'Configuration' },
    { name: 'Policies', href: '/policies', icon: '🛡', visible: true },
    { name: 'Model Catalog', href: '/models', icon: '🤖', visible: isSuperAdmin },

    'divider',

    // Observability
    { name: 'Traces', href: '/traces', icon: '🔍', visible: isOperator, section: 'Observability' },
    { name: 'Audit Log', href: '/audit', icon: '📋', visible: canViewAudit(role) },
    { name: 'Evaluations', href: '/evaluations', icon: '📊', visible: isOperator },
    { name: 'Usage & Billing', href: '/usage', icon: '💳', visible: canManageBilling(role) },

    'divider',

    // Operations
    { name: 'Escalation Queue', href: '/handoffs', icon: '🚨', visible: canViewEscalations(role), section: 'Operations' },
    { name: 'Security (MFA)', href: '/security', icon: '🔐', visible: isOperator },

    'divider',

    // Internal tooling (operator+ only, AGENTS.md boundary)
    {
      name: 'Harness',
      href: '/harness',
      icon: '⚗️',
      visible: isOperator,
      section: 'Internal',
      badge: 'INTERNAL',
    },
  ];

  let currentSection: string | undefined;

  return (
    <aside className="w-64 bg-white border-r min-h-screen flex flex-col">
      <nav className="mt-4 px-3 flex-1 space-y-0.5">
        {navigation.map((item, idx) => {
          if (item === 'divider') {
            return <div key={`div-${idx}`} className="my-2 border-t border-gray-100" />;
          }

          if (!item.visible) return null;

          const isActive = location.pathname === item.href;
          const showSection = item.section && item.section !== currentSection;
          if (showSection) currentSection = item.section;

          return (
            <div key={item.href}>
              {showSection && (
                <p className="px-3 pt-3 pb-1 text-xs font-semibold text-gray-400 uppercase tracking-wider">
                  {item.section}
                </p>
              )}
              <Link
                to={item.href as any}
                className={`flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                  isActive
                    ? 'bg-blue-50 text-blue-700'
                    : 'text-gray-600 hover:bg-gray-50 hover:text-gray-900'
                }`}
              >
                <span className="text-base leading-none w-5 text-center">{item.icon}</span>
                <span className="flex-1">{item.name}</span>
                {item.badge && (
                  <span className="text-[10px] font-bold bg-orange-100 text-orange-700 px-1.5 py-0.5 rounded border border-orange-200">
                    {item.badge}
                  </span>
                )}
              </Link>
            </div>
          );
        })}
      </nav>

      {/* Footer */}
      <div className="px-4 py-3 border-t border-gray-100">
        <p className="text-xs text-gray-400">Neryva Agent Studio</p>
        {principal && (
          <p className="text-xs text-gray-400 mt-0.5">
            {principal.role} · {principal.tenantId ? 'tenant-scoped' : 'global'}
          </p>
        )}
      </div>
    </aside>
  );
}
