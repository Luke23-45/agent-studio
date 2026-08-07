/**
 * EvalsExplorer (P7-4) — Evaluation runs dashboard.
 *
 * Lists eval runs with their status, pass/fail ratios, score,
 * and drill-down into individual test cases.
 */

import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { apiErrorMessage } from '../../lib/api/client';
import { listEvalRuns, getEvalCases } from '../../lib/api/endpoints';

const TYPE_COLORS: Record<string, string> = {
  regression: 'bg-blue-100 text-blue-800',
  red_team: 'bg-red-100 text-red-800',
  adversarial: 'bg-purple-100 text-purple-800',
  quality: 'bg-green-100 text-green-800',
  compaction: 'bg-amber-100 text-amber-800',
};

const STATUS_COLORS: Record<string, string> = {
  pending: 'bg-gray-100 text-gray-700',
  running: 'bg-blue-100 text-blue-700',
  completed: 'bg-green-100 text-green-700',
  failed: 'bg-red-100 text-red-700',
};

export function EvalsExplorer() {
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [typeFilter, setTypeFilter] = useState('');

  const runs = useQuery({ queryKey: ['evals', typeFilter], queryFn: () => listEvalRuns(typeFilter ? { type: typeFilter } : undefined) });
  const cases = useQuery({
    queryKey: ['eval-cases', selectedRunId],
    queryFn: () => getEvalCases(selectedRunId!),
    enabled: !!selectedRunId,
  });

  const selectedRun = (runs.data ?? []).find((r) => r.id === selectedRunId);

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Evaluations</h1>
          <p className="text-sm text-gray-500 mt-1">Manage eval runs, red-team reports, and regression tests.</p>
        </div>
        <select
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          className="px-3 py-2 border border-gray-300 rounded-md text-sm bg-white focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          <option value="">All types</option>
          <option value="regression">Regression</option>
          <option value="red_team">Red Team</option>
          <option value="adversarial">Adversarial</option>
          <option value="quality">Quality</option>
          <option value="compaction">Compaction</option>
        </select>
      </div>

      {runs.isLoading ? (
        <div className="text-center text-gray-500 py-12">Loading eval runs…</div>
      ) : runs.error ? (
        <div className="text-center text-red-600 py-12">Error: {apiErrorMessage(runs.error)}</div>
      ) : (runs.data ?? []).length === 0 ? (
        <div className="bg-white rounded-lg shadow p-12 text-center text-gray-400">
          No evaluation runs yet.
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {(runs.data ?? []).map((run) => (
            <div
              key={run.id}
              onClick={() => setSelectedRunId(run.id === selectedRunId ? null : run.id)}
              className={`bg-white rounded-lg shadow p-5 cursor-pointer border-2 transition-colors ${
                selectedRunId === run.id ? 'border-blue-400' : 'border-transparent hover:border-gray-200'
              }`}
            >
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-sm font-semibold text-gray-900 truncate">{run.name}</h3>
                <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${STATUS_COLORS[run.status] ?? ''}`}>
                  {run.status}
                </span>
              </div>

              <div className="flex items-center gap-2 mb-3">
                <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${TYPE_COLORS[run.type] ?? 'bg-gray-100 text-gray-600'}`}>
                  {run.type.replace(/_/g, ' ')}
                </span>
              </div>

              {/* Progress bar */}
              <div className="mb-2">
                <div className="flex justify-between text-xs text-gray-500 mb-1">
                  <span>{run.passed_cases} passed / {run.failed_cases} failed</span>
                  <span>{run.total_cases} total</span>
                </div>
                <div className="w-full bg-gray-200 rounded-full h-2 flex overflow-hidden">
                  {run.total_cases > 0 && (
                    <>
                      <div
                        className="bg-green-500 h-2"
                        style={{ width: `${(run.passed_cases / run.total_cases) * 100}%` }}
                      />
                      <div
                        className="bg-red-500 h-2"
                        style={{ width: `${(run.failed_cases / run.total_cases) * 100}%` }}
                      />
                    </>
                  )}
                </div>
              </div>

              <div className="flex justify-between items-center">
                <span className="text-lg font-bold text-gray-900">
                  {(run.score * 100).toFixed(0)}%
                </span>
                <span className="text-xs text-gray-400">
                  {new Date(run.created_at).toLocaleDateString()}
                </span>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Cases drill-down */}
      {selectedRun && (
        <div className="bg-white shadow rounded-lg overflow-hidden">
          <div className="px-6 py-4 border-b border-gray-200">
            <h2 className="text-lg font-medium text-gray-900">{selectedRun.name} — Test Cases</h2>
          </div>

          {cases.isLoading ? (
            <div className="px-6 py-8 text-center text-gray-500">Loading cases…</div>
          ) : cases.error ? (
            <div className="px-6 py-8 text-center text-red-600">Error: {apiErrorMessage(cases.error)}</div>
          ) : (cases.data ?? []).length === 0 ? (
            <div className="px-6 py-8 text-center text-gray-400">No test cases available.</div>
          ) : (
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Result</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Input</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Expected</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Actual</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Score</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Latency</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {(cases.data ?? []).map((c) => (
                  <tr key={c.id} className={c.passed === false ? 'bg-red-50/30' : ''}>
                    <td className="px-4 py-3">
                      {c.passed == null ? (
                        <span className="badge-warning">pending</span>
                      ) : c.passed ? (
                        <span className="badge-success">pass</span>
                      ) : (
                        <span className="badge-error">fail</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-700 max-w-xs truncate">{c.input}</td>
                    <td className="px-4 py-3 text-sm text-gray-500 max-w-xs truncate">{c.expected}</td>
                    <td className="px-4 py-3 text-sm text-gray-700 max-w-xs truncate">{c.actual ?? '—'}</td>
                    <td className="px-4 py-3 text-sm text-right font-mono text-gray-700">
                      {c.score != null ? `${(c.score * 100).toFixed(0)}%` : '—'}
                    </td>
                    <td className="px-4 py-3 text-sm text-right font-mono text-gray-500">
                      {c.latency_ms != null ? `${c.latency_ms}ms` : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}
