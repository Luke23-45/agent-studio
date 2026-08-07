/* eslint-disable react-refresh/only-export-components */
import { createRootRoute, createRoute } from '@tanstack/react-router';
import { App } from './App';
import { Dashboard } from '../features/dashboard/Dashboard';
import { TenantsList } from '../features/tenants/TenantsList';
import { PolicyEditor } from '../features/policies/PolicyEditor';
import { TracesExplorer } from '../features/traces/TracesExplorer';
import { AuditLogViewer } from '../features/traces/AuditLogViewer';
import { EvalsExplorer } from '../features/evaluations/EvalsExplorer';
import { EscalationQueue } from '../features/handoffs/EscalationQueue';
import { ModelCatalogUI } from '../features/tenants/ModelCatalogUI';
import { HarnessWorkbench } from '../features/harness/HarnessWorkbench';
import { SecurityPage } from '../features/security/SecurityPage';
import { UsageBilling } from '../features/usage/UsageBilling';
import { useAuthStore } from '../lib/auth/session';

// Root route
const rootRoute = createRootRoute({ component: App });

// Dashboard
const dashboardRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/',
  component: Dashboard,
});

// Tenants
const tenantsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/tenants',
  component: TenantsList,
});

// Policies — now uses the real editor
const policiesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/policies',
  component: PoliciesPage,
});

function PoliciesPage() {
  const principal = useAuthStore((s) => s.principal);
  const tenantId = principal?.tenantId ?? '';
  if (!tenantId) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-bold text-gray-900">Policies</h1>
        <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-4 text-sm text-yellow-800">
          Select a tenant context to manage its policies, or use a tenant-bound API key.
        </div>
      </div>
    );
  }
  return <PolicyEditor tenantId={tenantId} />;
}

// Traces
const tracesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/traces',
  component: TracesExplorer,
});

// Audit Log
const auditRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/audit',
  component: AuditLogViewer,
});

// Evaluations
const evaluationsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/evaluations',
  component: EvalsExplorer,
});

// Escalations (Handoffs)
const handoffsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/handoffs',
  component: EscalationQueue,
});

// Model Catalog
const modelsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/models',
  component: ModelCatalogUI,
});

// Harness Workbench (operator only)
const harnessRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/harness',
  component: HarnessWorkbench,
});

// Usage & Billing (billing:read)
const usageRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/usage',
  component: UsageBilling,
});

// Security / MFA (operator+)
const securityRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/security',
  component: SecurityPage,
});

export const routeTree = rootRoute.addChildren([
  dashboardRoute,
  tenantsRoute,
  policiesRoute,
  tracesRoute,
  auditRoute,
  evaluationsRoute,
  handoffsRoute,
  modelsRoute,
  harnessRoute,
  usageRoute,
  securityRoute,
]);
