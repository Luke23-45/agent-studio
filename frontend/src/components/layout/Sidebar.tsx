import { Link, useLocation } from '@tanstack/react-router';

const navigation = [
  { name: 'Dashboard', href: '/' },
  { name: 'Tenants', href: '/tenants' },
  { name: 'Policies', href: '/policies' },
  { name: 'Traces', href: '/traces' },
  { name: 'Evaluations', href: '/evaluations' },
  { name: 'Handoffs', href: '/handoffs' },
];

export function Sidebar() {
  const location = useLocation();

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
