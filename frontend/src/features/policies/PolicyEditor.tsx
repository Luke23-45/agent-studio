/**
 * PolicyEditor (P7-4) — Guardrail policy management with lifecycle.
 *
 * Supports the full policy lifecycle: draft → review → publish.
 * Operators can add/edit/remove individual rules, see diffs between
 * versions, simulate rules against sample inputs, and publish with
 * one-click approval.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { apiErrorMessage } from '../../lib/api/client';
import {
  listPolicies,
  createPolicy,
  updatePolicy,
  publishPolicy,
} from '../../lib/api/endpoints';
import type { PolicyRule } from '../../lib/api/types';

interface Props {
  tenantId: string;
}

const RULE_KINDS = ['input', 'output', 'tool', 'topic', 'safety'] as const;
const RULE_ACTIONS = ['block', 'flag', 'redact', 'escalate'] as const;
const RULE_SEVERITIES = ['low', 'medium', 'high', 'critical'] as const;

const SEVERITY_COLORS: Record<string, string> = {
  low: 'bg-gray-100 text-gray-700',
  medium: 'bg-yellow-100 text-yellow-800',
  high: 'bg-orange-100 text-orange-800',
  critical: 'bg-red-100 text-red-800',
};

const STATUS_COLORS: Record<string, string> = {
  draft: 'bg-gray-100 text-gray-700',
  review: 'bg-blue-100 text-blue-700',
  published: 'bg-green-100 text-green-700',
  archived: 'bg-gray-100 text-gray-500',
};

export function PolicyEditor({ tenantId }: Props) {
  const queryClient = useQueryClient();
  const [selectedPolicyId, setSelectedPolicyId] = useState<string | null>(null);
  const [showNewRuleForm, setShowNewRuleForm] = useState(false);
  const [editingRules, setEditingRules] = useState<PolicyRule[] | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  // Simulation panel
  const [simulationInput, setSimulationInput] = useState('');
  const [simulationResults, setSimulationResults] = useState<{ rule: string; action: string; matched: boolean }[] | null>(null);

  const policies = useQuery({
    queryKey: ['policies', tenantId],
    queryFn: () => listPolicies(tenantId),
  });

  const selectedPolicy = (policies.data ?? []).find((p) => p.id === selectedPolicyId) ?? null;
  const currentRules = editingRules ?? selectedPolicy?.rules ?? [];

  // Mutations
  const createMut = useMutation({
    mutationFn: () => createPolicy(tenantId, { rules: [] }),
    onSuccess: (data) => {
      setSelectedPolicyId(data.id);
      setEditingRules([]);
      queryClient.invalidateQueries({ queryKey: ['policies', tenantId] });
      setSuccessMsg('New policy draft created.');
    },
    onError: (err) => setErrorMsg(apiErrorMessage(err)),
  });

  const saveMut = useMutation({
    mutationFn: () => {
      if (!selectedPolicyId || !editingRules) return Promise.resolve(null as any);
      return updatePolicy(tenantId, selectedPolicyId, { rules: editingRules });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['policies', tenantId] });
      setSuccessMsg('Policy saved.');
      setEditingRules(null);
    },
    onError: (err) => setErrorMsg(apiErrorMessage(err)),
  });

  const submitForReview = useMutation({
    mutationFn: () => {
      if (!selectedPolicyId) return Promise.resolve(null as any);
      return updatePolicy(tenantId, selectedPolicyId, { rules: currentRules, status: 'review' });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['policies', tenantId] });
      setSuccessMsg('Policy submitted for review.');
    },
    onError: (err) => setErrorMsg(apiErrorMessage(err)),
  });

  const publishMut = useMutation({
    mutationFn: () => {
      if (!selectedPolicyId) return Promise.resolve(null as any);
      return publishPolicy(tenantId, selectedPolicyId);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['policies', tenantId] });
      setSuccessMsg('Policy published and active.');
    },
    onError: (err) => setErrorMsg(apiErrorMessage(err)),
  });

  // Add new rule
  const addRule = (rule: PolicyRule) => {
    setEditingRules([...(editingRules ?? selectedPolicy?.rules ?? []), rule]);
    setShowNewRuleForm(false);
  };

  // Remove a rule
  const removeRule = (ruleId: string) => {
    setEditingRules((editingRules ?? selectedPolicy?.rules ?? []).filter((r) => r.id !== ruleId));
  };

  // Toggle rule enabled
  const toggleRule = (ruleId: string) => {
    setEditingRules(
      (editingRules ?? selectedPolicy?.rules ?? []).map((r) =>
        r.id === ruleId ? { ...r, enabled: !r.enabled } : r,
      ),
    );
  };

  // Simulate rules against sample input
  const runSimulation = () => {
    if (!simulationInput.trim()) return;
    const results = currentRules.map((rule) => {
      let matched = false;
      if (rule.enabled && rule.pattern) {
        try {
          matched = new RegExp(rule.pattern, 'i').test(simulationInput);
        } catch {
          matched = simulationInput.toLowerCase().includes(rule.pattern.toLowerCase());
        }
      }
      return { rule: rule.name, action: rule.action, matched };
    });
    setSimulationResults(results);
  };

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Policies</h1>
          <p className="text-sm text-gray-500 mt-1">Configure guardrails, safety rules, and compliance settings.</p>
        </div>
        <button onClick={() => createMut.mutate()} disabled={createMut.isPending} className="btn-primary">
          {createMut.isPending ? 'Creating…' : '+ New Policy Draft'}
        </button>
      </div>

      {/* Alerts */}
      {successMsg && (
        <div className="bg-green-50 border border-green-200 rounded-lg p-3 text-sm text-green-700">
          {successMsg} <button onClick={() => setSuccessMsg(null)} className="ml-2 text-green-500">×</button>
        </div>
      )}
      {errorMsg && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-700">
          {errorMsg} <button onClick={() => setErrorMsg(null)} className="ml-2 text-red-500">×</button>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
        {/* Policy versions sidebar */}
        <div className="lg:col-span-1 space-y-2">
          <h3 className="text-sm font-semibold text-gray-700 uppercase tracking-wider mb-3">Versions</h3>
          {policies.isLoading ? (
            <p className="text-sm text-gray-500">Loading…</p>
          ) : (policies.data ?? []).length === 0 ? (
            <p className="text-sm text-gray-400">No policies yet.</p>
          ) : (
            (policies.data ?? []).map((p) => (
              <button
                key={p.id}
                onClick={() => { setSelectedPolicyId(p.id); setEditingRules(null); setSimulationResults(null); }}
                className={`w-full text-left rounded-lg p-3 border transition-colors ${
                  selectedPolicyId === p.id
                    ? 'border-blue-400 bg-blue-50'
                    : 'border-gray-200 hover:border-gray-300 bg-white'
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium text-gray-900">v{p.version}</span>
                  <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${STATUS_COLORS[p.status] ?? ''}`}>
                    {p.status}
                  </span>
                </div>
                <p className="text-xs text-gray-500 mt-1">{p.rules.length} rules · {new Date(p.updated_at).toLocaleDateString()}</p>
              </button>
            ))
          )}
        </div>

        {/* Policy detail */}
        <div className="lg:col-span-3 space-y-6">
          {selectedPolicy ? (
            <>
              {/* Action bar */}
              <div className="flex items-center gap-3 flex-wrap">
                <span className={`text-xs px-2.5 py-1 rounded-full font-semibold ${STATUS_COLORS[selectedPolicy.status] ?? ''}`}>
                  {selectedPolicy.status.toUpperCase()}
                </span>
                {selectedPolicy.status === 'draft' && (
                  <>
                    <button onClick={() => saveMut.mutate()} disabled={!editingRules || saveMut.isPending} className="btn-primary text-sm">
                      {saveMut.isPending ? 'Saving…' : 'Save Draft'}
                    </button>
                    <button onClick={() => submitForReview.mutate()} disabled={submitForReview.isPending} className="btn-secondary text-sm">
                      Submit for Review
                    </button>
                  </>
                )}
                {selectedPolicy.status === 'review' && (
                  <button onClick={() => publishMut.mutate()} disabled={publishMut.isPending} className="bg-green-600 text-white px-4 py-2 rounded-md hover:bg-green-700 text-sm font-medium">
                    {publishMut.isPending ? 'Publishing…' : '✓ Publish'}
                  </button>
                )}
                {selectedPolicy.published_at && (
                  <span className="text-xs text-gray-500">Published {new Date(selectedPolicy.published_at).toLocaleString()} by {selectedPolicy.published_by ?? 'system'}</span>
                )}
              </div>

              {/* Rules list */}
              <div className="bg-white rounded-lg shadow overflow-hidden">
                <div className="px-6 py-4 border-b border-gray-200 flex justify-between items-center">
                  <h2 className="text-lg font-medium text-gray-900">Rules ({currentRules.length})</h2>
                  {(selectedPolicy.status === 'draft' || selectedPolicy.status === 'review') && (
                    <button onClick={() => setShowNewRuleForm(!showNewRuleForm)} className="text-sm text-blue-600 hover:text-blue-800 font-medium">
                      {showNewRuleForm ? 'Cancel' : '+ Add Rule'}
                    </button>
                  )}
                </div>

                {showNewRuleForm && <NewRuleForm onAdd={addRule} onCancel={() => setShowNewRuleForm(false)} />}

                {currentRules.length === 0 ? (
                  <div className="px-6 py-8 text-center text-gray-400 text-sm">No rules defined yet. Add your first rule above.</div>
                ) : (
                  <div className="divide-y divide-gray-100">
                    {currentRules.map((rule) => (
                      <div key={rule.id} className={`px-6 py-4 flex items-center gap-4 ${!rule.enabled ? 'opacity-50' : ''}`}>
                        <button
                          onClick={() => toggleRule(rule.id)}
                          className={`w-10 h-5 rounded-full relative transition-colors ${rule.enabled ? 'bg-blue-600' : 'bg-gray-300'}`}
                          aria-label={rule.enabled ? 'Disable rule' : 'Enable rule'}
                        >
                          <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-transform ${rule.enabled ? 'left-5' : 'left-0.5'}`} />
                        </button>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2">
                            <span className="text-sm font-medium text-gray-900">{rule.name}</span>
                            <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${SEVERITY_COLORS[rule.severity] ?? ''}`}>{rule.severity}</span>
                            <span className="text-xs bg-gray-100 text-gray-600 px-1.5 py-0.5 rounded">{rule.kind}</span>
                            <span className="text-xs bg-gray-100 text-gray-600 px-1.5 py-0.5 rounded">{rule.action}</span>
                          </div>
                          <p className="text-xs text-gray-500 mt-0.5 truncate">{rule.description}</p>
                          {rule.pattern && <code className="text-xs text-gray-400 font-mono mt-0.5 block truncate">/{rule.pattern}/</code>}
                        </div>
                        {selectedPolicy.status === 'draft' && (
                          <button onClick={() => removeRule(rule.id)} className="text-red-400 hover:text-red-600 text-sm">Remove</button>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* Simulation panel */}
              <div className="bg-white rounded-lg shadow p-6">
                <h3 className="text-lg font-medium text-gray-900 mb-3">Rule Simulation</h3>
                <p className="text-sm text-gray-500 mb-4">Test how the current rules would evaluate against sample input.</p>
                <div className="flex gap-3">
                  <input
                    value={simulationInput}
                    onChange={(e) => setSimulationInput(e.target.value)}
                    placeholder="Enter sample user message…"
                    className="flex-1 px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                  />
                  <button onClick={runSimulation} disabled={!simulationInput.trim()} className="btn-primary text-sm">
                    Simulate
                  </button>
                </div>
                {simulationResults && (
                  <div className="mt-4 divide-y divide-gray-100">
                    {simulationResults.map((r, i) => (
                      <div key={i} className="py-2 flex items-center gap-3">
                        <span className={`w-2 h-2 rounded-full ${r.matched ? 'bg-red-500' : 'bg-green-500'}`} />
                        <span className="text-sm text-gray-900">{r.rule}</span>
                        <span className="text-xs text-gray-500">→ {r.action}</span>
                        <span className={`ml-auto text-xs font-medium ${r.matched ? 'text-red-600' : 'text-green-600'}`}>
                          {r.matched ? 'TRIGGERED' : 'pass'}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </>
          ) : (
            <div className="bg-white rounded-lg shadow p-12 text-center">
              <p className="text-gray-400 text-lg">Select a policy version from the sidebar or create a new draft.</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// New Rule Form
// ---------------------------------------------------------------------------

function NewRuleForm({ onAdd, onCancel }: { onAdd: (rule: PolicyRule) => void; onCancel: () => void }) {
  const [name, setName] = useState('');
  const [kind, setKind] = useState<PolicyRule['kind']>('input');
  const [action, setAction] = useState<PolicyRule['action']>('block');
  const [severity, setSeverity] = useState<PolicyRule['severity']>('medium');
  const [description, setDescription] = useState('');
  const [pattern, setPattern] = useState('');

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    onAdd({
      id: `rule-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
      name: name.trim(),
      kind,
      action,
      severity,
      description: description.trim(),
      pattern: pattern.trim() || undefined,
      enabled: true,
    });
  };

  return (
    <form onSubmit={handleSubmit} className="px-6 py-4 border-b border-gray-200 bg-gray-50 space-y-3">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div>
          <label className="text-xs font-medium text-gray-600 block mb-1">Rule Name</label>
          <input value={name} onChange={(e) => setName(e.target.value)} className="w-full px-2 py-1.5 border border-gray-300 rounded text-sm" placeholder="e.g. PII Detector" />
        </div>
        <div>
          <label className="text-xs font-medium text-gray-600 block mb-1">Kind</label>
          <select value={kind} onChange={(e) => setKind(e.target.value as any)} className="w-full px-2 py-1.5 border border-gray-300 rounded text-sm bg-white">
            {RULE_KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
        </div>
        <div>
          <label className="text-xs font-medium text-gray-600 block mb-1">Action</label>
          <select value={action} onChange={(e) => setAction(e.target.value as any)} className="w-full px-2 py-1.5 border border-gray-300 rounded text-sm bg-white">
            {RULE_ACTIONS.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
        </div>
        <div>
          <label className="text-xs font-medium text-gray-600 block mb-1">Severity</label>
          <select value={severity} onChange={(e) => setSeverity(e.target.value as any)} className="w-full px-2 py-1.5 border border-gray-300 rounded text-sm bg-white">
            {RULE_SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div>
          <label className="text-xs font-medium text-gray-600 block mb-1">Description</label>
          <input value={description} onChange={(e) => setDescription(e.target.value)} className="w-full px-2 py-1.5 border border-gray-300 rounded text-sm" placeholder="What this rule does" />
        </div>
        <div>
          <label className="text-xs font-medium text-gray-600 block mb-1">Pattern (regex, optional)</label>
          <input value={pattern} onChange={(e) => setPattern(e.target.value)} className="w-full px-2 py-1.5 border border-gray-300 rounded text-sm font-mono" placeholder="e.g. \b(SSN|social\s*security)\b" />
        </div>
      </div>
      <div className="flex gap-2">
        <button type="submit" disabled={!name.trim()} className="btn-primary text-sm">Add Rule</button>
        <button type="button" onClick={onCancel} className="btn-secondary text-sm">Cancel</button>
      </div>
    </form>
  );
}
