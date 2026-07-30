import { Outlet } from '@tanstack/react-router';
import { Header } from '../components/layout/Header';
import { Sidebar } from '../components/layout/Sidebar';

export function App() {
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
