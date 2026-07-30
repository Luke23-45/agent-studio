import { createRootRoute, createRoute } from '@tanstack/react-router';
import { App } from './App';

// Root route
const rootRoute = createRootRoute({
  component: App,
});

// Dashboard route
const dashboardRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/',
  component: Dashboard,
});

function Dashboard() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-gray-900">Dashboard</h1>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
        <StatCard title="Active Tenants" value="12" trend="+2" />
        <StatCard title="Total Conversations" value="1,234" trend="+15%" />
        <StatCard title="Escalations" value="8" trend="-3" />
        <StatCard title="Safety Score" value="98.5%" trend="+0.2%" />
      </div>
    </div>
  );
}

function StatCard({ title, value, trend }: { title: string; value: string; trend: string }) {
  const isPositive = trend.startsWith('+');
  return (
    <div className="bg-white rounded-lg shadow p-6">
      <h3 className="text-sm font-medium text-gray-500">{title}</h3>
      <p className="mt-2 text-3xl font-semibold text-gray-900">{value}</p>
      <p className={`mt-1 text-sm ${isPositive ? 'text-green-600' : 'text-red-600'}`}>
        {trend} from last week
      </p>
    </div>
  );
}

// Tenant routes
const tenantsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/tenants',
  component: TenantsList,
});

function TenantsList() {
  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-gray-900">Tenants</h1>
        <button className="bg-blue-600 text-white px-4 py-2 rounded-md hover:bg-blue-700">
          Add Tenant
        </button>
      </div>
      <div className="bg-white shadow rounded-lg overflow-hidden">
        <table className="min-w-full divide-y divide-gray-200">
          <thead className="bg-gray-50">
            <tr>
              <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Name</th>
              <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
              <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Model</th>
              <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Actions</th>
            </tr>
          </thead>
          <tbody className="bg-white divide-y divide-gray-200">
            <tr>
              <td className="px-6 py-4 whitespace-nowrap">Acme Corp</td>
              <td className="px-6 py-4 whitespace-nowrap"><span className="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-green-100 text-green-800">Active</span></td>
              <td className="px-6 py-4 whitespace-nowrap">GPT-4</td>
              <td className="px-6 py-4 whitespace-nowrap text-sm text-blue-600 hover:text-blue-900 cursor-pointer">Edit</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}

// Policy routes
const policiesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/policies',
  component: PoliciesList,
});

function PoliciesList() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-gray-900">Policies</h1>
      <p className="text-gray-600">Configure guardrails, safety rules, and compliance settings.</p>
    </div>
  );
}

// Traces route
const tracesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/traces',
  component: TracesList,
});

function TracesList() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-gray-900">Traces</h1>
      <p className="text-gray-600">View and analyze conversation traces from Langfuse.</p>
    </div>
  );
}

// Evaluations route
const evaluationsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/evaluations',
  component: EvaluationsList,
});

function EvaluationsList() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-gray-900">Evaluations</h1>
      <p className="text-gray-600">Manage eval runs, red-team reports, and regression tests.</p>
    </div>
  );
}

// Handoffs route
const handoffsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/handoffs',
  component: HandoffsList,
});

function HandoffsList() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-gray-900">Human Handoffs</h1>
      <p className="text-gray-600">Review and manage escalated conversations requiring human intervention.</p>
    </div>
  );
}

// Create the route tree
export const routeTree = rootRoute.addChildren([
  dashboardRoute,
  tenantsRoute,
  policiesRoute,
  tracesRoute,
  evaluationsRoute,
  handoffsRoute,
]);
