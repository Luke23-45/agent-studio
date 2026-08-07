/**
 * Usage & Billing page (P7-4): spend-ledger aggregations (Arch 10, P3-5)
 * and durable USD quota windows. Super admins see the platform view;
 * tenant-bound principals see their own tenant.
 */

import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { apiErrorMessage } from '../../lib/api/client';
import {
  getUsageEvents,
  getUsageQuota,
  getUsageSummary,
  getUsageTenantDetail,
} from '../../lib/api/endpoints';
import { useAuthStore } from '../../lib/auth/session';

function usd(v: number): string {
  return `$${v.toFixed(4)}`;
}

function tokens(v: number): string {
  if (v >= 1_000_000_000) return `${(v / 1_000_000_000).toFixed(1)}B`;
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}K`;
  return String(v);
}

function StatCard({ label, value, accent }: { label: string; value: string; accent?: string }) {
  return (
    <div className="bg-white rounded-lg border p-4">
      <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">{label}</p>
      <p className={`mt-1 text-xl font-bold ${accent ?? 'text-gray-900'}`}>{value}</p>
    </div>
  );
}

function BreakdownTable({ title, rows }: { title: string; rows: { key: string; calls: number; usd: number; inputTokens: number; outputTokens: number }[] }) {
  return (
    <div className="bg-white rounded-lg border p-4">
      <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
      {rows.length === 0 ? (
        <p className="mt-2 text-sm text-gray-400">No spend recorded yet.</p>
      ) : (
        <table className="mt-2 w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-gray-400 uppercase">
              <th className="pb-1">Key</th>
              <th className="pb-1 text-right">Calls</th>
              <th className="pb-1 text-right">USD</th>
              <th className="pb-1 text-right">Input</th>
              <th className="pb-1 text-right">Output</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key} className="border-t border-gray-50">
                <td className="py-1 pr-2 font-mono text-xs">{r.key}</td>
                <td className="py-1 text-right">{r.calls}</td>
                <td className="py-1 text-right">{usd(r.usd)}</td>
                <td className="py-1 text-right">{tokens(r.inputTokens)}</td>
                <td className="py-1 text-right">{tokens(r.outputTokens)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export function UsageBilling() {
  const principal = useAuthStore((s) => s.principal);
  const isGlobal = !principal?.tenantId;
  const [selectedTenant, setSelectedTenant] = useState<string | null>(
    principal?.tenantId ?? null,
  );
  const tenantId = selectedTenant ?? principal?.tenantId ?? null;

  const summary = useQuery({
    queryKey: ['usage', 'summary'],
    queryFn: () => getUsageSummary(),
  });

  const detail = useQuery({
    queryKey: ['usage', 'tenant', tenantId],
    queryFn: () => (tenantId ? getUsageTenantDetail(tenantId) : Promise.resolve(null)),
    enabled: !!tenantId,
  });

  const quota = useQuery({
    queryKey: ['usage', 'quota', tenantId ?? 'all'],
    queryFn: () => getUsageQuota(tenantId ? { tenant_id: tenantId } : undefined),
  });

  const events = useQuery({
    queryKey: ['usage', 'events', tenantId ?? 'all'],
    queryFn: () => getUsageEvents(tenantId ? { tenant_id: tenantId, limit: 20 } : { limit: 20 }),
  });

  const queryError = summary.error ?? detail.error ?? quota.error ?? events.error;

  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setError(queryError ? apiErrorMessage(queryError) : null);
  }, [queryError]);

  const chartData = useMemo(
    () =>
      (detail.data?.perDay ?? []).map((d) => ({
        day: d.key,
        usd: Number(d.usd.toFixed(4)),
        calls: d.calls,
      })),
    [detail.data],
  );

  const t = summary.data?.totals;
  const perTenant = summary.data?.perTenant ?? [];
  const dayDetail = detail.data;

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Usage & Billing</h1>
          <p className="mt-1 text-sm text-gray-600">
            Spend-ledger aggregations over all completed generations (P3-5).
          </p>
        </div>
        {isGlobal && (
          <select
            value={selectedTenant ?? ''}
            onChange={(e) => setSelectedTenant(e.target.value || null)}
            className="px-3 py-2 border border-gray-300 rounded-md text-sm bg-white"
          >
            <option value="">All tenants</option>
            {perTenant.map((p) => (
              <option key={p.tenantId} value={p.tenantId}>
                {p.tenantId.slice(0, 8)}…
              </option>
            ))}
          </select>
        )}
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-700">
          {error}
        </div>
      )}

      {summary.isLoading ? (
        <p className="text-sm text-gray-400">Loading…</p>
      ) : (
        t && (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <StatCard label="Calls" value={String(t.calls)} />
            <StatCard label="Spend" value={usd(t.usd)} accent="text-green-700" />
            <StatCard label="Input tokens" value={tokens(t.inputTokens)} />
            <StatCard label="Output tokens" value={tokens(t.outputTokens)} />
          </div>
        )
      )}

      {!tenantId && (
        <BreakdownTable
          title="Per tenant"
          rows={perTenant.map((p) => ({ key: p.tenantId, ...p }))}
        />
      )}

      {tenantId && dayDetail && (
        <>
          <div className="bg-white rounded-lg border p-4">
            <h3 className="text-sm font-semibold text-gray-900">Spend per day (last 90)</h3>
            {chartData.length === 0 ? (
              <p className="mt-2 text-sm text-gray-400">No spend recorded in this window.</p>
            ) : (
              <div className="mt-2 h-48">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={chartData}>
                    <XAxis dataKey="day" tick={{ fontSize: 10 }} />
                    <YAxis tick={{ fontSize: 10 }} />
                    <Tooltip />
                    <Bar dataKey="usd" fill="#3b82f6" name="USD" />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}
          </div>
          <div className="grid md:grid-cols-2 gap-4">
            <BreakdownTable
              title="Per model"
              rows={dayDetail.perModel}
            />
            <BreakdownTable
              title="Per surface"
              rows={dayDetail.perSurface}
            />
          </div>
        </>
      )}

      <div className="bg-white rounded-lg border p-4">
        <h3 className="text-sm font-semibold text-gray-900">Quota windows</h3>
        {quota.data?.windows.length === 0 ? (
          <p className="mt-2 text-sm text-gray-400">No quota windows recorded.</p>
        ) : (
          <table className="mt-2 w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-gray-400 uppercase">
                <th className="pb-1">Scope</th>
                <th className="pb-1">Tenant</th>
                <th className="pb-1 text-right">Limit</th>
                <th className="pb-1 text-right">Spent</th>
                <th className="pb-1 text-right">Reserved</th>
                <th className="pb-1">Window</th>
              </tr>
            </thead>
            <tbody>
              {(quota.data?.windows ?? []).map((w, i) => (
                <tr key={i} className="border-t border-gray-50">
                  <td className="py-1">{w.scopeType}</td>
                  <td className="py-1 font-mono text-xs">{w.tenantId?.slice(0, 8) ?? '—'}</td>
                  <td className="py-1 text-right">
                    {w.limitUsd !== null ? usd(w.limitUsd) : '∞'}
                  </td>
                  <td className="py-1 text-right">{usd(w.spentUsd)}</td>
                  <td className="py-1 text-right">{usd(w.reservedUsd)}</td>
                  <td className="py-1 text-xs text-gray-500">
                    {new Date(w.windowStartedAt).toLocaleDateString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="bg-white rounded-lg border p-4">
        <h3 className="text-sm font-semibold text-gray-900">Recent spend events</h3>
        {events.data?.events.length === 0 ? (
          <p className="mt-2 text-sm text-gray-400">No spend events recorded.</p>
        ) : (
          <table className="mt-2 w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-gray-400 uppercase">
                <th className="pb-1">When</th>
                <th className="pb-1">Tenant</th>
                <th className="pb-1">Model</th>
                <th className="pb-1 text-right">Input</th>
                <th className="pb-1 text-right">Output</th>
                <th className="pb-1 text-right">USD</th>
              </tr>
            </thead>
            <tbody>
              {(events.data?.events ?? []).map((e) => (
                <tr key={e.id} className="border-t border-gray-50">
                  <td className="py-1 text-xs text-gray-500">
                    {e.createdAt ? new Date(e.createdAt).toLocaleString() : '—'}
                  </td>
                  <td className="py-1 font-mono text-xs">{e.tenantId.slice(0, 8)}</td>
                  <td className="py-1 font-mono text-xs">
                    {e.model ? `${e.provider ?? ''}:${e.model}` : '—'}
                  </td>
                  <td className="py-1 text-right">{tokens(e.inputTokens)}</td>
                  <td className="py-1 text-right">{tokens(e.outputTokens)}</td>
                  <td className="py-1 text-right">{usd(e.usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
