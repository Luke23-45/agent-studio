/**
 * TracesExplorer (P7-4) — Distributed trace viewer and analyzer.
 *
 * Provides a detailed table of all trace spans with:
 * - Filtering by tenant, session, status, provider, model
 * - Latency/cost breakdown per span
 * - Detail drawer with full metadata
 * - Color-coded status badges
 * - Token usage and cost columns
 */

import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { apiErrorMessage } from '../../lib/api/client';
import { listTraces } from '../../lib/api/endpoints';
import type { TraceEntry } from '../../lib/api/types';

const STATUS_COLORS: Record<string, string> = {
  ok: 'bg-green-100 text-green-800',
  error: 'bg-red-100 text-red-800',
  timeout: 'bg-yellow-100 text-yellow-800',
};

export function TracesExplorer() {
  const [filters, setFilters] = useState({
    tenant_id: '',
    session_id: '',
    status: '',
  });
  const [selectedTrace, setSelectedTrace] = useState<TraceEntry | null>(null);
  const [limit] = useState(50);
  const [offset, setOffset] = useState(0);

  const activeFilters = Object.fromEntries(
    Object.entries({ ...filters, limit, offset }).filter(([, v]) => v !== '' && v !== 0),
  );

  const traces = useQuery({
    queryKey: ['traces', activeFilters],
    queryFn: () => listTraces(activeFilters as any),
  });

  const clearFilters = () => {
    setFilters({ tenant_id: '', session_id: '', status: '' });
    setOffset(0);
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Traces</h1>
        <p className="text-sm text-gray-500 mt-1">View and analyze conversation traces from the gateway.</p>
      </div>

      {/* Filter bar */}
      <div className="bg-white rounded-lg shadow p-4 flex flex-wrap items-end gap-4">
        <div>
          <label className="text-xs font-medium text-gray-600 block mb-1">Tenant ID</label>
          <input
            value={filters.tenant_id}
            onChange={(e) => { setFilters({ ...filters, tenant_id: e.target.value }); setOffset(0); }}
            placeholder="Filter by tenant"
            className="px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 w-48"
          />
        </div>
        <div>
          <label className="text-xs font-medium text-gray-600 block mb-1">Session ID</label>
          <input
            value={filters.session_id}
            onChange={(e) => { setFilters({ ...filters, session_id: e.target.value }); setOffset(0); }}
            placeholder="Filter by session"
            className="px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 w-48"
          />
        </div>
        <div>
          <label className="text-xs font-medium text-gray-600 block mb-1">Status</label>
          <select
            value={filters.status}
            onChange={(e) => { setFilters({ ...filters, status: e.target.value }); setOffset(0); }}
            className="px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white w-36"
          >
            <option value="">All</option>
            <option value="ok">OK</option>
            <option value="error">Error</option>
            <option value="timeout">Timeout</option>
          </select>
        </div>
        <button onClick={clearFilters} className="text-sm text-gray-500 hover:text-gray-700 pb-2">
          Clear Filters
        </button>
      </div>

      {/* Results table */}
      <div className="bg-white shadow rounded-lg overflow-hidden">
        {traces.isLoading ? (
          <div className="px-6 py-12 text-center text-gray-500">Loading traces…</div>
        ) : traces.error ? (
          <div className="px-6 py-12 text-center text-red-600">Error: {apiErrorMessage(traces.error)}</div>
        ) : (traces.data ?? []).length === 0 ? (
          <div className="px-6 py-12 text-center text-gray-400">No traces found for the current filters.</div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-gray-200">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Operation</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Status</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Provider / Model</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">Duration</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">Tokens (I/O)</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">Cost</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Time</th>
                  </tr>
                </thead>
                <tbody className="bg-white divide-y divide-gray-100">
                  {(traces.data ?? []).map((trace) => (
                    <tr
                      key={trace.id}
                      onClick={() => setSelectedTrace(trace)}
                      className="hover:bg-gray-50 cursor-pointer transition-colors"
                    >
                      <td className="px-4 py-3 text-sm text-gray-900 font-medium">
                        <div className="flex items-center gap-2">
                          {trace.parent_span_id && <span className="text-gray-300 text-xs">└─</span>}
                          {trace.operation}
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        <span className={`inline-flex text-xs leading-5 font-semibold rounded-full px-2 ${STATUS_COLORS[trace.status] ?? 'bg-gray-100 text-gray-700'}`}>
                          {trace.status}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-sm text-gray-600">
                        <span className="font-medium">{trace.provider}</span>
                        <span className="text-gray-400 mx-1">/</span>
                        <span>{trace.model}</span>
                      </td>
                      <td className="px-4 py-3 text-sm text-right font-mono">
                        <span className={trace.duration_ms > 5000 ? 'text-red-600 font-semibold' : trace.duration_ms > 2000 ? 'text-amber-600' : 'text-gray-700'}>
                          {trace.duration_ms.toLocaleString()}ms
                        </span>
                      </td>
                      <td className="px-4 py-3 text-sm text-right text-gray-600 font-mono">
                        {trace.input_tokens.toLocaleString()} / {trace.output_tokens.toLocaleString()}
                      </td>
                      <td className="px-4 py-3 text-sm text-right text-gray-700 font-mono">
                        ${trace.cost.toFixed(4)}
                      </td>
                      <td className="px-4 py-3 text-xs text-gray-500">
                        {new Date(trace.created_at).toLocaleString()}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Pagination */}
            <div className="px-6 py-3 border-t border-gray-200 flex items-center justify-between">
              <p className="text-sm text-gray-500">
                Showing {offset + 1}–{offset + (traces.data?.length ?? 0)} results
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
                  disabled={(traces.data?.length ?? 0) < limit}
                  className="px-3 py-1.5 text-sm border border-gray-300 rounded-md disabled:opacity-40 hover:bg-gray-50"
                >
                  Next
                </button>
              </div>
            </div>
          </>
        )}
      </div>

      {/* Trace detail drawer */}
      {selectedTrace && (
        <TraceDetailDrawer trace={selectedTrace} onClose={() => setSelectedTrace(null)} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Trace Detail Drawer
// ---------------------------------------------------------------------------

function TraceDetailDrawer({ trace, onClose }: { trace: TraceEntry; onClose: () => void }) {
  const fields = [
    { label: 'Trace ID', value: trace.trace_id },
    { label: 'Span ID', value: trace.span_id },
    { label: 'Parent Span', value: trace.parent_span_id ?? '—' },
    { label: 'Tenant', value: trace.tenant_id },
    { label: 'Session', value: trace.session_id },
    { label: 'Thread', value: trace.thread_id ?? '—' },
    { label: 'Operation', value: trace.operation },
    { label: 'Provider', value: trace.provider },
    { label: 'Model', value: trace.model },
    { label: 'Status', value: trace.status },
    { label: 'Duration', value: `${trace.duration_ms}ms` },
    { label: 'Input Tokens', value: trace.input_tokens.toLocaleString() },
    { label: 'Output Tokens', value: trace.output_tokens.toLocaleString() },
    { label: 'Cost', value: `$${trace.cost.toFixed(4)}` },
    { label: 'Created', value: new Date(trace.created_at).toLocaleString() },
  ];

  return (
    <div className="fixed inset-0 z-50 flex justify-end" onClick={onClose}>
      <div className="absolute inset-0 bg-black/20" />
      <div
        className="relative w-full max-w-lg bg-white shadow-2xl overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sticky top-0 bg-white border-b border-gray-200 px-6 py-4 flex justify-between items-center">
          <h2 className="text-lg font-semibold text-gray-900">Trace Detail</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl">×</button>
        </div>

        <div className="p-6 space-y-4">
          {fields.map((f) => (
            <div key={f.label} className="flex justify-between py-1.5 border-b border-gray-50">
              <span className="text-sm text-gray-500">{f.label}</span>
              <span className="text-sm text-gray-900 font-mono text-right max-w-[260px] truncate">{f.value}</span>
            </div>
          ))}

          {Object.keys(trace.metadata).length > 0 && (
            <div className="pt-4">
              <h3 className="text-sm font-semibold text-gray-700 mb-2">Metadata</h3>
              <pre className="bg-gray-50 rounded-lg p-4 text-xs font-mono text-gray-700 overflow-x-auto max-h-64">
                {JSON.stringify(trace.metadata, null, 2)}
              </pre>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
