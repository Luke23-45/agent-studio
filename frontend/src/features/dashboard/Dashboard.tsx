/**
 * Dashboard with real backend data (no hardcoded metrics).
 *
 * Stats are derived from actual endpoints: tenant count, open escalations,
 * and recent audit activity. Loading/error states are explicit.
 */

import { useQuery } from '@tanstack/react-query';
import { apiErrorMessage } from '../../lib/api/client';
import { listAuditEvents, listEscalations, listTenants } from '../../lib/api/endpoints';

function StatCard({
  title,
  value,
  detail,
  loading,
}: {
  title: string;
  value: string;
  detail: string;
  loading: boolean;
}) {
  return (
    <div className="bg-white rounded-lg shadow p-6">
      <h3 className="text-sm font-medium text-gray-500">{title}</h3>
      <p className="mt-2 text-3xl font-semibold text-gray-900">
        {loading ? '—' : value}
      </p>
      <p className="mt-1 text-sm text-gray-500">{detail}</p>
    </div>
  );
}

export function Dashboard() {
  const tenants = useQuery({ queryKey: ['tenants'], queryFn: listTenants });
  const escalations = useQuery({ queryKey: ['escalations'], queryFn: listEscalations });
  const audit = useQuery({ queryKey: ['audit'], queryFn: listAuditEvents });

  const error = tenants.error ?? escalations.error ?? audit.error;
  const loading = tenants.isLoading || escalations.isLoading || audit.isLoading;

  const openEscalations = (escalations.data ?? []).filter((e) => e.status === 'open').length;
  const recentAudit = (audit.data ?? []).slice(0, 8);

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-gray-900">Dashboard</h1>

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-4 text-sm text-red-700">
          Failed to load dashboard: {apiErrorMessage(error)}
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        <StatCard
          title="Active Tenants"
          value={String(tenants.data?.length ?? 0)}
          detail={loading ? 'Loading…' : 'From the tenant registry'}
          loading={loading}
        />
        <StatCard
          title="Open Escalations"
          value={String(openEscalations)}
          detail={loading ? 'Loading…' : 'Awaiting human review'}
          loading={loading}
        />
        <StatCard
          title="Audit Events"
          value={String(audit.data?.length ?? 0)}
          detail={loading ? 'Loading…' : 'Recorded actions'}
          loading={loading}
        />
      </div>

      <div className="bg-white shadow rounded-lg overflow-hidden">
        <div className="px-6 py-4 border-b border-gray-200">
          <h2 className="text-lg font-medium text-gray-900">Recent Audit Events</h2>
        </div>
        {loading ? (
          <div className="px-6 py-4 text-sm text-gray-500">Loading…</div>
        ) : recentAudit.length === 0 ? (
          <div className="px-6 py-4 text-sm text-gray-500">No audit events yet.</div>
        ) : (
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Action</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Actor</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Resource</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Time</th>
              </tr>
            </thead>
            <tbody className="bg-white divide-y divide-gray-200">
              {recentAudit.map((event) => (
                <tr key={event.id}>
                  <td className="px-6 py-3 text-sm text-gray-900">{event.action}</td>
                  <td className="px-6 py-3 text-sm text-gray-600">{event.actor_type}:{event.actor_id ?? 'system'}</td>
                  <td className="px-6 py-3 text-sm text-gray-600">{event.resource_type}</td>
                  <td className="px-6 py-3 text-sm text-gray-500">
                    {new Date(event.created_at).toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
