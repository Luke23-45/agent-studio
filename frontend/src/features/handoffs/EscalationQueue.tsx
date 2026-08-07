/**
 * EscalationQueue (P7-4) — Human handoff review and management.
 *
 * Full queue with assign/resolve workflow, severity badges, category
 * tags, and a conversation summary drawer. Operators can claim an
 * escalation and write resolution notes before closing it.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { apiErrorMessage } from '../../lib/api/client';
import { listEscalations, assignEscalation, resolveEscalation } from '../../lib/api/endpoints';
import type { Escalation } from '../../lib/api/types';

const SEVERITY_STYLES: Record<string, string> = {
  low:      'bg-gray-100 text-gray-700',
  medium:   'bg-yellow-100 text-yellow-800',
  high:     'bg-orange-100 text-orange-800',
  critical: 'bg-red-100 text-red-800',
};

const STATUS_STYLES: Record<string, string> = {
  open:        'bg-blue-100 text-blue-800',
  assigned:    'bg-indigo-100 text-indigo-800',
  resolved:    'bg-green-100 text-green-800',
  closed:      'bg-gray-100 text-gray-500',
};

const CATEGORY_STYLES: Record<string, string> = {
  safety:      'bg-red-50 text-red-700 border border-red-200',
  compliance:  'bg-purple-50 text-purple-700 border border-purple-200',
  hallucination: 'bg-amber-50 text-amber-700 border border-amber-200',
  off_topic:   'bg-gray-50 text-gray-600 border border-gray-200',
  abuse:       'bg-red-50 text-red-600 border border-red-200',
};

export function EscalationQueue() {
  const queryClient = useQueryClient();
  const [statusFilter, setStatusFilter] = useState('open');
  const [selected, setSelected]         = useState<Escalation | null>(null);
  const [resolutionNotes, setResolutionNotes] = useState('');
  const [assigneeId, setAssigneeId]     = useState('');
  const [actionError, setActionError]   = useState<string | null>(null);
  const [successMsg, setSuccessMsg]     = useState<string | null>(null);

  const escalations = useQuery({
    queryKey: ['escalations', statusFilter],
    queryFn: () => listEscalations(statusFilter ? { status: statusFilter } : undefined),
    refetchInterval: 30_000, // auto-refresh every 30 s
  });

  const assignMut = useMutation({
    mutationFn: ({ id, assignee }: { id: string; assignee: string }) =>
      assignEscalation(id, assignee),
    onSuccess: (updated) => {
      setSelected(updated);
      setAssigneeId('');
      setActionError(null);
      setSuccessMsg('Escalation assigned.');
      queryClient.invalidateQueries({ queryKey: ['escalations'] });
    },
    onError: (err) => setActionError(apiErrorMessage(err)),
  });

  const resolveMut = useMutation({
    mutationFn: ({ id, notes }: { id: string; notes: string }) =>
      resolveEscalation(id, { resolution_notes: notes }),
    onSuccess: (updated) => {
      setSelected(updated);
      setResolutionNotes('');
      setActionError(null);
      setSuccessMsg('Escalation resolved.');
      queryClient.invalidateQueries({ queryKey: ['escalations'] });
    },
    onError: (err) => setActionError(apiErrorMessage(err)),
  });

  const openCount   = (escalations.data ?? []).filter((e) => e.status === 'open').length;
  const criticalCount = (escalations.data ?? []).filter((e) => e.severity === 'critical').length;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Escalation Queue</h1>
          <p className="text-sm text-gray-500 mt-1">
            Review and resolve conversations escalated for human intervention.
          </p>
        </div>
        <div className="flex gap-3">
          <div className="bg-blue-50 border border-blue-200 rounded-lg px-4 py-2 text-center">
            <p className="text-xl font-bold text-blue-700">{openCount}</p>
            <p className="text-xs text-blue-600 font-medium">Open</p>
          </div>
          <div className="bg-red-50 border border-red-200 rounded-lg px-4 py-2 text-center">
            <p className="text-xl font-bold text-red-700">{criticalCount}</p>
            <p className="text-xs text-red-600 font-medium">Critical</p>
          </div>
        </div>
      </div>

      {/* Alerts */}
      {successMsg && (
        <div className="bg-green-50 border border-green-200 rounded-lg p-3 text-sm text-green-700 flex justify-between">
          {successMsg}
          <button onClick={() => setSuccessMsg(null)} className="text-green-500">×</button>
        </div>
      )}
      {actionError && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-700 flex justify-between">
          {actionError}
          <button onClick={() => setActionError(null)} className="text-red-500">×</button>
        </div>
      )}

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
        {/* Left: list */}
        <div className="xl:col-span-2 space-y-4">
          {/* Status filter tabs */}
          <div className="flex gap-1 bg-gray-100 p-1 rounded-lg w-fit">
            {['open', 'assigned', 'resolved', ''].map((s) => (
              <button
                key={s}
                onClick={() => setStatusFilter(s)}
                className={`px-4 py-1.5 rounded-md text-sm font-medium transition-colors ${
                  statusFilter === s
                    ? 'bg-white text-gray-900 shadow-sm'
                    : 'text-gray-500 hover:text-gray-700'
                }`}
              >
                {s === '' ? 'All' : s.charAt(0).toUpperCase() + s.slice(1)}
              </button>
            ))}
          </div>

          {escalations.isLoading ? (
            <div className="bg-white rounded-lg shadow p-8 text-center text-gray-500">Loading escalations…</div>
          ) : escalations.error ? (
            <div className="bg-white rounded-lg shadow p-8 text-center text-red-600">
              {apiErrorMessage(escalations.error)}
            </div>
          ) : (escalations.data ?? []).length === 0 ? (
            <div className="bg-white rounded-lg shadow p-12 text-center">
              <p className="text-4xl mb-3">✓</p>
              <p className="text-gray-500 font-medium">No {statusFilter || ''} escalations.</p>
            </div>
          ) : (
            <div className="space-y-3">
              {(escalations.data ?? []).map((esc) => (
                <div
                  key={esc.id}
                  onClick={() => { setSelected(esc); setResolutionNotes(''); setAssigneeId(''); }}
                  className={`bg-white rounded-lg shadow p-5 cursor-pointer border-l-4 hover:shadow-md transition-all ${
                    selected?.id === esc.id
                      ? 'border-l-blue-500 ring-2 ring-blue-100'
                      : esc.severity === 'critical'
                      ? 'border-l-red-500'
                      : esc.severity === 'high'
                      ? 'border-l-orange-400'
                      : 'border-l-gray-300'
                  }`}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap mb-2">
                        <span className={`text-xs px-2 py-0.5 rounded-full font-semibold ${SEVERITY_STYLES[esc.severity] ?? ''}`}>
                          {esc.severity}
                        </span>
                        <span className={`text-xs px-2 py-0.5 rounded font-medium ${CATEGORY_STYLES[esc.category] ?? 'bg-gray-100 text-gray-600'}`}>
                          {esc.category.replace(/_/g, ' ')}
                        </span>
                        <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${STATUS_STYLES[esc.status] ?? ''}`}>
                          {esc.status}
                        </span>
                      </div>
                      <p className="text-sm font-medium text-gray-900 truncate">{esc.reason}</p>
                      <p className="text-sm text-gray-500 mt-1 line-clamp-2">{esc.summary}</p>
                    </div>
                    <div className="text-right shrink-0">
                      <p className="text-xs text-gray-400">{new Date(esc.created_at).toLocaleString()}</p>
                      {esc.session_id && (
                        <p className="text-xs font-mono text-gray-400 mt-1 truncate max-w-[120px]">{esc.session_id}</p>
                      )}
                    </div>
                  </div>

                  {/* Channel badge */}
                  <div className="mt-2 flex items-center gap-2">
                    <span className="text-xs text-gray-400">via {esc.channel}</span>
                    {esc.external_ref && (
                      <span className="text-xs font-mono text-gray-400 truncate max-w-[200px]">
                        ref: {esc.external_ref}
                      </span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Right: detail + actions */}
        <div className="xl:col-span-1">
          {selected ? (
            <div className="bg-white rounded-lg shadow p-6 space-y-5 sticky top-6">
              <h2 className="text-base font-semibold text-gray-900">Escalation Detail</h2>

              <div className="space-y-2 text-sm">
                {[
                  ['ID', selected.id.slice(0, 16) + '…'],
                  ['Tenant', selected.tenant_id],
                  ['Session', selected.session_id ?? '—'],
                  ['Category', selected.category],
                  ['Severity', selected.severity],
                  ['Status', selected.status],
                  ['Channel', selected.channel],
                  ['Created', new Date(selected.created_at).toLocaleString()],
                ].map(([label, value]) => (
                  <div key={label} className="flex justify-between">
                    <span className="text-gray-500">{label}</span>
                    <span className="text-gray-900 font-medium text-right ml-4 break-all">{value}</span>
                  </div>
                ))}
              </div>

              <div>
                <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-1">Summary</p>
                <p className="text-sm text-gray-700 bg-gray-50 rounded-lg p-3">{selected.summary}</p>
              </div>

              {/* Assign */}
              {(selected.status === 'open') && (
                <div className="border-t pt-4 space-y-2">
                  <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider">Assign</p>
                  <div className="flex gap-2">
                    <input
                      value={assigneeId}
                      onChange={(e) => setAssigneeId(e.target.value)}
                      placeholder="Assignee ID or email"
                      className="flex-1 px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                    />
                    <button
                      onClick={() => assignMut.mutate({ id: selected.id, assignee: assigneeId })}
                      disabled={!assigneeId.trim() || assignMut.isPending}
                      className="btn-primary text-sm"
                    >
                      {assignMut.isPending ? '…' : 'Assign'}
                    </button>
                  </div>
                </div>
              )}

              {/* Resolve */}
              {(selected.status === 'open' || selected.status === 'assigned') && (
                <div className="border-t pt-4 space-y-2">
                  <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider">Resolve</p>
                  <textarea
                    value={resolutionNotes}
                    onChange={(e) => setResolutionNotes(e.target.value)}
                    placeholder="Resolution notes…"
                    rows={3}
                    className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
                  />
                  <button
                    onClick={() => resolveMut.mutate({ id: selected.id, notes: resolutionNotes })}
                    disabled={!resolutionNotes.trim() || resolveMut.isPending}
                    className="w-full bg-green-600 text-white py-2 rounded-md hover:bg-green-700 text-sm font-medium disabled:opacity-50 transition-colors"
                  >
                    {resolveMut.isPending ? 'Resolving…' : 'Mark Resolved'}
                  </button>
                </div>
              )}

              {/* Metadata */}
              {Object.keys(selected.details).length > 0 && (
                <div className="border-t pt-4">
                  <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Details</p>
                  <pre className="bg-gray-50 rounded-lg p-3 text-xs font-mono text-gray-700 overflow-x-auto max-h-48">
                    {JSON.stringify(selected.details, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          ) : (
            <div className="bg-white rounded-lg shadow p-8 text-center text-gray-400">
              <p className="text-4xl mb-3">📋</p>
              <p className="text-sm">Select an escalation to view details and take action.</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
