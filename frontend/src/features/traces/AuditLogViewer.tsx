/**
 * AuditLogViewer (P7-4) — Immutable audit log browser.
 *
 * Searchable, filterable table of all recorded audit events with:
 * - Actor type/ID and action filtering
 * - Resource type grouping
 * - Timestamp ordering
 * - Metadata detail drawer
 * - CSV/JSON export
 */

import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { apiErrorMessage } from '../../lib/api/client';
import { listAuditEvents, exportAuditLog } from '../../lib/api/endpoints';
import type { AuditEvent } from '../../lib/api/types';

const ACTION_COLORS: Record<string, string> = {
  create:  'bg-green-100 text-green-800',
  update:  'bg-blue-100 text-blue-800',
  delete:  'bg-red-100 text-red-800',
  publish: 'bg-purple-100 text-purple-800',
  revoke:  'bg-orange-100 text-orange-800',
  login:   'bg-gray-100 text-gray-700',
  logout:  'bg-gray-100 text-gray-700',
};

function actionColor(action: string): string {
  const key = Object.keys(ACTION_COLORS).find((k) => action.toLowerCase().includes(k));
  return key ? ACTION_COLORS[key] : 'bg-gray-100 text-gray-700';
}

export function AuditLogViewer() {
  const [actionFilter, setActionFilter]     = useState('');
  const [resourceFilter, setResourceFilter] = useState('');
  const [tenantFilter, setTenantFilter]     = useState('');
  const [selectedEvent, setSelectedEvent]   = useState<AuditEvent | null>(null);
  const [limit] = useState(100);
  const [offset, setOffset] = useState(0);
  const [exporting, setExporting] = useState(false);

  const activeParams = Object.fromEntries(
    Object.entries({ tenant_id: tenantFilter, action: actionFilter, limit, offset })
      .filter(([, v]) => v !== '' && v !== 0),
  );

  const events = useQuery({
    queryKey: ['audit', activeParams],
    queryFn: () => listAuditEvents(activeParams as any),
  });

  const allEvents = events.data ?? [];

  // Client-side resource type filter (not all backends support it as a query param)
  const filtered = resourceFilter
    ? allEvents.filter((e) => e.resource_type.toLowerCase().includes(resourceFilter.toLowerCase()))
    : allEvents;

  const handleExport = async () => {
    setExporting(true);
    try {
      const blob = await exportAuditLog({
        tenant_id: tenantFilter || undefined,
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `audit-export-${new Date().toISOString().split('T')[0]}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      /* silent — user can retry */
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Audit Log</h1>
          <p className="text-sm text-gray-500 mt-1">
            Immutable record of all operator and system actions.
          </p>
        </div>
        <button
          onClick={handleExport}
          disabled={exporting}
          className="btn-secondary text-sm"
        >
          {exporting ? 'Exporting…' : '↓ Export'}
        </button>
      </div>

      {/* Filters */}
      <div className="bg-white rounded-lg shadow p-4 flex flex-wrap gap-4 items-end">
        <div>
          <label className="text-xs font-medium text-gray-600 block mb-1">Tenant ID</label>
          <input
            value={tenantFilter}
            onChange={(e) => { setTenantFilter(e.target.value); setOffset(0); }}
            placeholder="Filter by tenant"
            className="px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 w-44"
          />
        </div>
        <div>
          <label className="text-xs font-medium text-gray-600 block mb-1">Action</label>
          <input
            value={actionFilter}
            onChange={(e) => { setActionFilter(e.target.value); setOffset(0); }}
            placeholder="e.g. create, delete"
            className="px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 w-40"
          />
        </div>
        <div>
          <label className="text-xs font-medium text-gray-600 block mb-1">Resource Type</label>
          <input
            value={resourceFilter}
            onChange={(e) => setResourceFilter(e.target.value)}
            placeholder="e.g. tenant, policy"
            className="px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 w-40"
          />
        </div>
        <button
          onClick={() => { setTenantFilter(''); setActionFilter(''); setResourceFilter(''); setOffset(0); }}
          className="text-sm text-gray-500 hover:text-gray-700 pb-2"
        >
          Clear
        </button>
      </div>

      {/* Table */}
      <div className="bg-white shadow rounded-lg overflow-hidden">
        {events.isLoading ? (
          <div className="px-6 py-12 text-center text-gray-500">Loading audit events…</div>
        ) : events.error ? (
          <div className="px-6 py-12 text-center text-red-600">{apiErrorMessage(events.error)}</div>
        ) : filtered.length === 0 ? (
          <div className="px-6 py-12 text-center text-gray-400">No audit events match the current filters.</div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-gray-200">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Action</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Actor</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Resource</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Tenant</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Time</th>
                  </tr>
                </thead>
                <tbody className="bg-white divide-y divide-gray-100">
                  {filtered.map((event) => (
                    <tr
                      key={event.id}
                      onClick={() => setSelectedEvent(event)}
                      className="hover:bg-gray-50 cursor-pointer transition-colors"
                    >
                      <td className="px-4 py-3">
                        <span className={`inline-flex text-xs leading-5 font-semibold rounded-full px-2 ${actionColor(event.action)}`}>
                          {event.action}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-sm text-gray-700">
                        <span className="font-medium">{event.actor_type}</span>
                        {event.actor_id && (
                          <span className="text-gray-400 font-mono ml-1">:{event.actor_id.slice(0, 12)}</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-sm text-gray-600">
                        <span className="font-medium">{event.resource_type}</span>
                        {event.resource_id && (
                          <span className="text-gray-400 font-mono ml-1">/{event.resource_id.slice(0, 10)}…</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-sm font-mono text-gray-400 text-xs">
                        {event.tenant_id ? event.tenant_id.slice(0, 12) + '…' : '—'}
                      </td>
                      <td className="px-4 py-3 text-xs text-gray-500 whitespace-nowrap">
                        {new Date(event.created_at).toLocaleString()}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Pagination */}
            <div className="px-6 py-3 border-t border-gray-200 flex items-center justify-between">
              <p className="text-sm text-gray-500">
                Showing {filtered.length} results (page {Math.floor(offset / limit) + 1})
              </p>
              <div className="flex gap-2">
                <button
                  onClick={() => setOffset(Math.max(0, offset - limit))}
                  disabled={offset === 0}
                  className="px-3 py-1.5 text-sm border border-gray-300 rounded-md disabled:opacity-40 hover:bg-gray-50"
                >
                  Previous
                </button>
                <button
                  onClick={() => setOffset(offset + limit)}
                  disabled={allEvents.length < limit}
                  className="px-3 py-1.5 text-sm border border-gray-300 rounded-md disabled:opacity-40 hover:bg-gray-50"
                >
                  Next
                </button>
              </div>
            </div>
          </>
        )}
      </div>

      {/* Detail drawer */}
      {selectedEvent && (
        <div className="fixed inset-0 z-50 flex justify-end" onClick={() => setSelectedEvent(null)}>
          <div className="absolute inset-0 bg-black/20" />
          <div
            className="relative w-full max-w-md bg-white shadow-2xl overflow-y-auto"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="sticky top-0 bg-white border-b px-6 py-4 flex justify-between items-center">
              <h2 className="text-lg font-semibold text-gray-900">Audit Event</h2>
              <button onClick={() => setSelectedEvent(null)} className="text-gray-400 hover:text-gray-700 text-xl">×</button>
            </div>
            <div className="p-6 space-y-3">
              {[
                ['ID', selectedEvent.id],
                ['Action', selectedEvent.action],
                ['Actor Type', selectedEvent.actor_type],
                ['Actor ID', selectedEvent.actor_id ?? '—'],
                ['Resource Type', selectedEvent.resource_type],
                ['Resource ID', selectedEvent.resource_id ?? '—'],
                ['Tenant ID', selectedEvent.tenant_id ?? '—'],
                ['Time', new Date(selectedEvent.created_at).toLocaleString()],
              ].map(([label, value]) => (
                <div key={label} className="flex justify-between py-1.5 border-b border-gray-50">
                  <span className="text-sm text-gray-500">{label}</span>
                  <span className="text-sm text-gray-900 font-mono text-right max-w-[240px] break-all">{value}</span>
                </div>
              ))}
              {Object.keys(selectedEvent.details).length > 0 && (
                <div className="pt-3">
                  <h3 className="text-sm font-semibold text-gray-700 mb-2">Details</h3>
                  <pre className="bg-gray-50 rounded-lg p-4 text-xs font-mono text-gray-700 overflow-x-auto max-h-60">
                    {JSON.stringify(selectedEvent.details, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
