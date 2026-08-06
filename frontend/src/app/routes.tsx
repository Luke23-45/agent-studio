/* eslint-disable react-refresh/only-export-components -- route tree must export route objects + components together */
import { createRootRoute, createRoute } from '@tanstack/react-router';
import { App } from './App';
import { Dashboard } from '../features/dashboard/Dashboard';
import { TenantsList } from '../features/tenants/TenantsList';

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

// Tenant routes
const tenantsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/tenants',
  component: TenantsList,
});

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
