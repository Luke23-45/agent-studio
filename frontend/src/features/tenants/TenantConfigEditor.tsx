/**
 * TenantConfigEditor (P7-4) — Full tenant configuration management.
 *
 * Provides a complete editor for tenant settings including:
 * - Basic info (name, slug, region)
 * - Topic allow/block lists
 * - Escalation threshold with slider
 * - Data retention settings
 * - Deployment shape configuration
 * - Onboarding progress tracker
 * - Metrics overview
 * - GDPR / compliance panel
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { apiErrorMessage } from '../../lib/api/client';
import {
  getTenant,
  getTenantMetrics,
  getTenantOnboarding,
  getTenantCompliance,
  updateTenant,
  exportEndUserData,
  eraseEndUserData,
} from '../../lib/api/endpoints';
import type { Tenant, TenantMetrics, TenantOnboarding } from '../../lib/api/types';

interface Props {
  tenantId: string;
  onBack: () => void;
}

export function TenantConfigEditor({ tenantId, onBack }: Props) {
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState<'config' | 'metrics' | 'compliance' | 'onboarding'>('config');
  const [successMsg, setSuccessMsg] = useState<string | null>(null);

  const tenant = useQuery({ queryKey: ['tenant', tenantId], queryFn: () => getTenant(tenantId) });
  const metrics = useQuery({ queryKey: ['tenant-metrics', tenantId], queryFn: () => getTenantMetrics(tenantId), enabled: activeTab === 'metrics' });
  const onboarding = useQuery({ queryKey: ['tenant-onboarding', tenantId], queryFn: () => getTenantOnboarding(tenantId), enabled: activeTab === 'onboarding' });
  const compliance = useQuery({ queryKey: ['tenant-compliance', tenantId], queryFn: () => getTenantCompliance(tenantId), enabled: activeTab === 'compliance' });

  if (tenant.isLoading) return <div className="p-6 text-gray-500">Loading tenant…</div>;
  if (tenant.error) return <div className="p-6 text-red-600">Error: {apiErrorMessage(tenant.error)}</div>;
  if (!tenant.data) return <div className="p-6 text-gray-500">Tenant not found.</div>;

  const tabs = [
    { key: 'config' as const, label: 'Configuration' },
    { key: 'metrics' as const, label: 'Metrics' },
    { key: 'compliance' as const, label: 'Compliance & GDPR' },
    { key: 'onboarding' as const, label: 'Onboarding' },
  ];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-4">
        <button onClick={onBack} className="text-blue-600 hover:text-blue-800 text-sm font-medium">
          ← Back to Tenants
        </button>
        <div>
          <h1 className="text-2xl font-bold text-gray-900">{tenant.data.name}</h1>
          <p className="text-sm text-gray-500">Tenant slug: <code className="bg-gray-100 px-1.5 py-0.5 rounded text-xs">{tenant.data.slug}</code></p>
        </div>
      </div>

      {successMsg && (
        <div className="bg-green-50 border border-green-200 rounded-lg p-3 text-sm text-green-700 flex items-center gap-2">
          <span>✓</span> {successMsg}
          <button onClick={() => setSuccessMsg(null)} className="ml-auto text-green-500 hover:text-green-700">×</button>
        </div>
      )}

      {/* Tab bar */}
      <div className="border-b border-gray-200">
        <nav className="flex gap-6">
          {tabs.map((tab) => (
            <button
              key={tab.key}
              onClick={() => setActiveTab(tab.key)}
              className={`pb-3 px-1 text-sm font-medium border-b-2 transition-colors ${
                activeTab === tab.key
                  ? 'border-blue-600 text-blue-600'
                  : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </div>

      {/* Tab content */}
      {activeTab === 'config' && <ConfigTab tenant={tenant.data} onSuccess={(msg) => { setSuccessMsg(msg); queryClient.invalidateQueries({ queryKey: ['tenant', tenantId] }); }} />}
      {activeTab === 'metrics' && <MetricsTab metrics={metrics.data} loading={metrics.isLoading} error={metrics.error} />}
      {activeTab === 'compliance' && <ComplianceTab tenantId={tenantId} data={compliance.data} loading={compliance.isLoading} />}
      {activeTab === 'onboarding' && <OnboardingTab data={onboarding.data} loading={onboarding.isLoading} />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Config Tab
// ---------------------------------------------------------------------------

function ConfigTab({ tenant, onSuccess }: { tenant: Tenant; onSuccess: (msg: string) => void }) {
  const [name, setName] = useState(tenant.name);
  const [allowedTopics, setAllowedTopics] = useState(tenant.allowed_topics.join(', '));
  const [blockedTopics, setBlockedTopics] = useState(tenant.blocked_topics.join(', '));
  const [escalationThreshold, setEscalationThreshold] = useState(tenant.escalation_threshold);
  const [retentionDays, setRetentionDays] = useState(tenant.retention_days ?? 90);
  const [region, setRegion] = useState(tenant.region ?? '');
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: () =>
      updateTenant(tenant.id, {
        name,
        allowed_topics: allowedTopics.split(',').map((t) => t.trim()).filter(Boolean),
        blocked_topics: blockedTopics.split(',').map((t) => t.trim()).filter(Boolean),
        escalation_threshold: escalationThreshold,
        retention_days: retentionDays,
        region: region || null,
      }),
    onSuccess: () => {
      setError(null);
      onSuccess('Configuration saved successfully.');
    },
    onError: (err) => setError(apiErrorMessage(err)),
  });

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
      {/* General settings */}
      <div className="bg-white rounded-lg shadow p-6 space-y-5">
        <h3 className="text-lg font-semibold text-gray-900 border-b pb-3">General Settings</h3>

        <div>
          <label className="label">Tenant Name</label>
          <input value={name} onChange={(e) => setName(e.target.value)} className="input w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
        </div>

        <div>
          <label className="label">Region</label>
          <select value={region} onChange={(e) => setRegion(e.target.value)} className="input w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white">
            <option value="">Platform default</option>
            <option value="eu-west-1">EU West (Ireland)</option>
            <option value="eu-central-1">EU Central (Frankfurt)</option>
            <option value="us-east-1">US East (Virginia)</option>
            <option value="us-west-2">US West (Oregon)</option>
            <option value="ap-southeast-1">Asia Pacific (Singapore)</option>
          </select>
        </div>

        <div>
          <label className="label">Data Retention (days)</label>
          <input
            type="number"
            min={1}
            max={3650}
            value={retentionDays}
            onChange={(e) => setRetentionDays(parseInt(e.target.value, 10) || 90)}
            className="input w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
          <p className="mt-1 text-xs text-gray-400">Conversations and threads older than this are purged.</p>
        </div>
      </div>

      {/* Topics & guardrails */}
      <div className="bg-white rounded-lg shadow p-6 space-y-5">
        <h3 className="text-lg font-semibold text-gray-900 border-b pb-3">Topics & Guardrails</h3>

        <div>
          <label className="label">Allowed Topics</label>
          <textarea
            value={allowedTopics}
            onChange={(e) => setAllowedTopics(e.target.value)}
            rows={3}
            className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
            placeholder="Comma-separated topics, e.g.: billing, support, returns"
          />
        </div>

        <div>
          <label className="label">Blocked Topics</label>
          <textarea
            value={blockedTopics}
            onChange={(e) => setBlockedTopics(e.target.value)}
            rows={3}
            className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
            placeholder="Comma-separated topics to block"
          />
        </div>

        <div>
          <label className="label">Escalation Threshold</label>
          <div className="flex items-center gap-4">
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={escalationThreshold}
              onChange={(e) => setEscalationThreshold(parseFloat(e.target.value))}
              className="flex-1 h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-blue-600"
            />
            <span className="text-sm font-mono text-gray-700 w-12 text-right">
              {escalationThreshold.toFixed(2)}
            </span>
          </div>
          <p className="mt-1 text-xs text-gray-400">Confidence below this triggers human handoff.</p>
        </div>
      </div>

      {/* Save bar */}
      <div className="lg:col-span-2">
        {error && (
          <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-700 mb-4">
            {error}
          </div>
        )}
        <button
          onClick={() => save.mutate()}
          disabled={save.isPending}
          className="btn-primary"
        >
          {save.isPending ? 'Saving…' : 'Save Configuration'}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Metrics Tab
// ---------------------------------------------------------------------------

function MetricsTab({ metrics, loading, error }: { metrics?: TenantMetrics; loading: boolean; error: unknown }) {
  if (loading) return <div className="text-gray-500 py-8 text-center">Loading metrics…</div>;
  if (error) return <div className="text-red-600 py-8">Error loading metrics: {apiErrorMessage(error)}</div>;
  if (!metrics) return <div className="text-gray-500 py-8 text-center">No metrics available yet.</div>;

  const cards = [
    { label: 'Total Conversations', value: metrics.total_conversations.toLocaleString(), color: 'bg-blue-50 text-blue-700' },
    { label: 'Total Tokens', value: metrics.total_tokens.toLocaleString(), color: 'bg-indigo-50 text-indigo-700' },
    { label: 'Total Cost', value: `$${metrics.total_cost.toFixed(2)}`, color: 'bg-green-50 text-green-700' },
    { label: 'Avg Confidence', value: `${(metrics.avg_confidence * 100).toFixed(1)}%`, color: 'bg-amber-50 text-amber-700' },
    { label: 'Escalation Rate', value: `${(metrics.escalation_rate * 100).toFixed(1)}%`, color: 'bg-red-50 text-red-700' },
    { label: 'Guardrail Hit Rate', value: `${(metrics.guardrail_hit_rate * 100).toFixed(1)}%`, color: 'bg-orange-50 text-orange-700' },
    { label: 'Avg TTFT', value: `${metrics.avg_ttft_ms.toFixed(0)}ms`, color: 'bg-purple-50 text-purple-700' },
    { label: 'Active End Users', value: metrics.active_end_users.toLocaleString(), color: 'bg-teal-50 text-teal-700' },
  ];

  return (
    <div className="space-y-4">
      <p className="text-sm text-gray-500">
        Period: {new Date(metrics.period_start).toLocaleDateString()} — {new Date(metrics.period_end).toLocaleDateString()}
      </p>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {cards.map((c) => (
          <div key={c.label} className={`rounded-lg p-4 ${c.color}`}>
            <p className="text-xs font-medium opacity-75">{c.label}</p>
            <p className="text-xl font-bold mt-1">{c.value}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Compliance Tab
// ---------------------------------------------------------------------------

function ComplianceTab({ tenantId, data, loading }: { tenantId: string; data?: Record<string, unknown>; loading: boolean }) {
  const [endUserId, setEndUserId] = useState('');
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const handleExport = async () => {
    if (!endUserId.trim()) return;
    try {
      setActionError(null);
      const blob = await exportEndUserData(tenantId, endUserId.trim());
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `export-${endUserId}.json`;
      a.click();
      URL.revokeObjectURL(url);
      setActionMsg('Export downloaded.');
    } catch (err) {
      setActionError(apiErrorMessage(err));
    }
  };

  const handleErase = async () => {
    if (!endUserId.trim()) return;
    if (!confirm(`This will permanently erase ALL data for end user "${endUserId}". Continue?`)) return;
    try {
      setActionError(null);
      await eraseEndUserData(tenantId, endUserId.trim());
      setActionMsg('End user data erased successfully.');
      setEndUserId('');
    } catch (err) {
      setActionError(apiErrorMessage(err));
    }
  };

  return (
    <div className="space-y-6">
      {/* Compliance status */}
      <div className="bg-white rounded-lg shadow p-6">
        <h3 className="text-lg font-semibold text-gray-900 mb-4">Compliance Status</h3>
        {loading ? (
          <p className="text-gray-500 text-sm">Loading…</p>
        ) : data ? (
          <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
            {Object.entries(data).map(([key, value]) => (
              <div key={key} className="border rounded-lg p-3">
                <p className="text-xs text-gray-500 font-medium uppercase tracking-wider">{key.replace(/_/g, ' ')}</p>
                <p className="text-sm font-semibold text-gray-900 mt-1">
                  {typeof value === 'boolean' ? (value ? '✓ Compliant' : '✗ Non-compliant') : String(value)}
                </p>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-gray-500 text-sm">No compliance data available.</p>
        )}
      </div>

      {/* GDPR DSR */}
      <div className="bg-white rounded-lg shadow p-6">
        <h3 className="text-lg font-semibold text-gray-900 mb-2">Data Subject Requests (GDPR)</h3>
        <p className="text-sm text-gray-500 mb-4">Export or erase a specific end user's data (threads, memory, metadata).</p>

        {actionMsg && (
          <div className="bg-green-50 border border-green-200 rounded-lg p-3 text-sm text-green-700 mb-4">
            {actionMsg}
          </div>
        )}
        {actionError && (
          <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-700 mb-4">
            {actionError}
          </div>
        )}

        <div className="flex items-end gap-3">
          <div className="flex-1">
            <label className="label">End User ID</label>
            <input
              value={endUserId}
              onChange={(e) => setEndUserId(e.target.value)}
              placeholder="e.g. eu_abc123"
              className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <button onClick={handleExport} disabled={!endUserId.trim()} className="btn-primary">
            Export Data
          </button>
          <button
            onClick={handleErase}
            disabled={!endUserId.trim()}
            className="bg-red-600 text-white px-4 py-2 rounded-md hover:bg-red-700 transition-colors font-medium disabled:opacity-50 text-sm"
          >
            Erase Data
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Onboarding Tab
// ---------------------------------------------------------------------------

function OnboardingTab({ data, loading }: { data?: TenantOnboarding; loading: boolean }) {
  if (loading) return <div className="text-gray-500 py-8 text-center">Loading onboarding progress…</div>;
  if (!data) return <div className="text-gray-500 py-8 text-center">No onboarding data available.</div>;

  const totalSteps = data.steps_completed.length + data.steps_remaining.length;
  const pct = totalSteps > 0 ? Math.round((data.steps_completed.length / totalSteps) * 100) : 0;

  return (
    <div className="bg-white rounded-lg shadow p-6 space-y-6">
      <div>
        <h3 className="text-lg font-semibold text-gray-900">Onboarding Progress</h3>
        <p className="text-sm text-gray-500 mt-1">{data.steps_completed.length} of {totalSteps} steps completed</p>
      </div>

      {/* Progress bar */}
      <div className="w-full bg-gray-200 rounded-full h-3">
        <div className="bg-blue-600 h-3 rounded-full transition-all duration-500" style={{ width: `${pct}%` }} />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div>
          <h4 className="text-sm font-semibold text-green-700 mb-2">✓ Completed</h4>
          <ul className="space-y-1">
            {data.steps_completed.map((step) => (
              <li key={step} className="text-sm text-gray-700 flex items-center gap-2">
                <span className="w-5 h-5 rounded-full bg-green-100 text-green-600 flex items-center justify-center text-xs">✓</span>
                {step.replace(/_/g, ' ')}
              </li>
            ))}
          </ul>
        </div>
        <div>
          <h4 className="text-sm font-semibold text-gray-500 mb-2">○ Remaining</h4>
          <ul className="space-y-1">
            {data.steps_remaining.map((step) => (
              <li key={step} className="text-sm text-gray-500 flex items-center gap-2">
                <span className="w-5 h-5 rounded-full bg-gray-100 text-gray-400 flex items-center justify-center text-xs">○</span>
                {step.replace(/_/g, ' ')}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}
