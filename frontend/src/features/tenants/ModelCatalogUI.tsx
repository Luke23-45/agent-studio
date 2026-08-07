/**
 * ModelCatalogUI (P7-4) — Model catalog management and circuit-breaker view.
 *
 * Displays all models from the catalog with provider/capability info,
 * pricing, context windows, tier assignments, and circuit-breaker status.
 * Super admins can enable/disable models and reset circuit breakers.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { apiErrorMessage } from '../../lib/api/client';
import {
  listModels,
  updateModel,
  listCircuitBreakers,
  resetCircuitBreaker,
} from '../../lib/api/endpoints';
import { MfaCancelled, useMfaProof } from '../security/mfa';
import type { ModelCatalogEntry } from '../../lib/api/types';

const STATUS_STYLES: Record<string, string> = {
  active:     'bg-green-100 text-green-800',
  deprecated: 'bg-yellow-100 text-yellow-800',
  disabled:   'bg-red-100 text-red-800',
};

const CB_STYLES: Record<string, string> = {
  closed:    'bg-green-100 text-green-800',
  open:      'bg-red-100 text-red-800',
  half_open: 'bg-yellow-100 text-yellow-800',
};

const PROVIDER_LOGOS: Record<string, string> = {
  openai:    '🟢',
  anthropic: '🟣',
  google:    '🔵',
  cohere:    '🟡',
  mistral:   '🔴',
  groq:      '⚡',
};

export function ModelCatalogUI() {
  const queryClient = useQueryClient();
  const { getProof } = useMfaProof();
  const [activeTab, setActiveTab] = useState<'models' | 'circuit-breakers'>('models');
  const [providerFilter, setProviderFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [successMsg, setSuccessMsg] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const models = useQuery({ queryKey: ['models'], queryFn: listModels });
  const circuitBreakers = useQuery({
    queryKey: ['circuit-breakers'],
    queryFn: listCircuitBreakers,
    enabled: activeTab === 'circuit-breakers',
    refetchInterval: 15_000,
  });

  const updateMut = useMutation({
    mutationFn: async ({ id, input }: { id: string; input: Partial<ModelCatalogEntry> }) => {
      let proof: string | null;
      try {
        proof = await getProof();
      } catch (err) {
        if (err instanceof MfaCancelled) return null;
        throw err;
      }
      return updateModel(id, input, { proof });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['models'] });
      setSuccessMsg('Model updated.');
      setErrorMsg(null);
    },
    onError: (err) => setErrorMsg(apiErrorMessage(err)),
  });

  const resetCbMut = useMutation({
    mutationFn: async ({ provider, model }: { provider: string; model: string }) => {
      let proof: string | null;
      try {
        proof = await getProof();
      } catch (err) {
        if (err instanceof MfaCancelled) return null;
        throw err;
      }
      return resetCircuitBreaker(provider, model, { proof });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['circuit-breakers'] });
      setSuccessMsg('Circuit breaker reset.');
      setErrorMsg(null);
    },
    onError: (err) => setErrorMsg(apiErrorMessage(err)),
  });

  const filteredModels = (models.data ?? []).filter((m) => {
    if (providerFilter && !m.provider.toLowerCase().includes(providerFilter.toLowerCase())) return false;
    if (statusFilter && m.status !== statusFilter) return false;
    return true;
  });

  const groupedByProvider: Record<string, ModelCatalogEntry[]> = {};
  for (const model of filteredModels) {
    if (!groupedByProvider[model.provider]) groupedByProvider[model.provider] = [];
    groupedByProvider[model.provider].push(model);
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Model Catalog</h1>
        <p className="text-sm text-gray-500 mt-1">Manage available models, tiers, pricing, and gateway circuit breakers.</p>
      </div>

      {/* Alerts */}
      {successMsg && (
        <div className="bg-green-50 border border-green-200 rounded-lg p-3 text-sm text-green-700 flex justify-between">
          {successMsg} <button onClick={() => setSuccessMsg(null)} className="text-green-500">×</button>
        </div>
      )}
      {errorMsg && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-700 flex justify-between">
          {errorMsg} <button onClick={() => setErrorMsg(null)} className="text-red-500">×</button>
        </div>
      )}

      {/* Tabs */}
      <div className="border-b border-gray-200">
        <nav className="flex gap-6">
          {[
            { key: 'models' as const, label: 'Model Catalog' },
            { key: 'circuit-breakers' as const, label: 'Circuit Breakers' },
          ].map((tab) => (
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

      {activeTab === 'models' && (
        <>
          {/* Filters */}
          <div className="flex flex-wrap gap-3">
            <input
              value={providerFilter}
              onChange={(e) => setProviderFilter(e.target.value)}
              placeholder="Filter by provider…"
              className="px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 w-44"
            />
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="px-3 py-2 border border-gray-300 rounded-md text-sm bg-white focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="">All statuses</option>
              <option value="active">Active</option>
              <option value="deprecated">Deprecated</option>
              <option value="disabled">Disabled</option>
            </select>
          </div>

          {models.isLoading ? (
            <div className="text-center text-gray-500 py-12">Loading models…</div>
          ) : models.error ? (
            <div className="text-center text-red-600 py-12">{apiErrorMessage(models.error)}</div>
          ) : Object.keys(groupedByProvider).length === 0 ? (
            <div className="bg-white rounded-lg shadow p-12 text-center text-gray-400">No models found.</div>
          ) : (
            <div className="space-y-8">
              {Object.entries(groupedByProvider).map(([provider, providerModels]) => (
                <div key={provider}>
                  <div className="flex items-center gap-2 mb-3">
                    <span className="text-xl">{PROVIDER_LOGOS[provider] ?? '🤖'}</span>
                    <h2 className="text-lg font-semibold text-gray-900 capitalize">{provider}</h2>
                    <span className="text-sm text-gray-400">({providerModels.length} models)</span>
                  </div>
                  <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                    {providerModels.map((model) => (
                      <ModelCard
                        key={model.id}
                        model={model}
                        onToggle={(m) =>
                          updateMut.mutate({
                            id: m.id,
                            input: { status: m.status === 'active' ? 'disabled' : 'active' },
                          })
                        }
                      />
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      )}

      {activeTab === 'circuit-breakers' && (
        <div className="bg-white shadow rounded-lg overflow-hidden">
          {circuitBreakers.isLoading ? (
            <div className="px-6 py-12 text-center text-gray-500">Loading circuit breaker states…</div>
          ) : circuitBreakers.error ? (
            <div className="px-6 py-12 text-center text-red-600">{apiErrorMessage(circuitBreakers.error)}</div>
          ) : (circuitBreakers.data ?? []).length === 0 ? (
            <div className="px-6 py-12 text-center text-gray-400">No circuit breakers have tripped.</div>
          ) : (
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-5 py-3 text-left text-xs font-medium text-gray-500 uppercase">Provider / Model</th>
                  <th className="px-5 py-3 text-left text-xs font-medium text-gray-500 uppercase">State</th>
                  <th className="px-5 py-3 text-right text-xs font-medium text-gray-500 uppercase">Failures</th>
                  <th className="px-5 py-3 text-right text-xs font-medium text-gray-500 uppercase">Failure Rate</th>
                  <th className="px-5 py-3 text-left text-xs font-medium text-gray-500 uppercase">Cooldown Until</th>
                  <th className="px-5 py-3 text-left text-xs font-medium text-gray-500 uppercase">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 bg-white">
                {(circuitBreakers.data ?? []).map((cb) => (
                  <tr key={`${cb.provider}/${cb.model}`} className={cb.state === 'open' ? 'bg-red-50/30' : ''}>
                    <td className="px-5 py-4 text-sm font-medium text-gray-900">
                      {PROVIDER_LOGOS[cb.provider] ?? '🤖'} {cb.provider}
                      <span className="text-gray-400 mx-1">/</span>
                      <span className="font-mono text-xs">{cb.model}</span>
                    </td>
                    <td className="px-5 py-4">
                      <span className={`inline-flex text-xs leading-5 font-semibold rounded-full px-2.5 py-0.5 ${CB_STYLES[cb.state] ?? ''}`}>
                        {cb.state.replace('_', ' ')}
                      </span>
                    </td>
                    <td className="px-5 py-4 text-sm text-right font-mono text-gray-700">{cb.failure_count}</td>
                    <td className="px-5 py-4 text-sm text-right font-mono text-gray-700">
                      {(cb.failure_rate * 100).toFixed(1)}%
                    </td>
                    <td className="px-5 py-4 text-xs text-gray-500">
                      {cb.cooldown_until ? new Date(cb.cooldown_until).toLocaleTimeString() : '—'}
                    </td>
                    <td className="px-5 py-4">
                      {cb.state !== 'closed' && (
                        <button
                          onClick={() => resetCbMut.mutate({ provider: cb.provider, model: cb.model })}
                          disabled={resetCbMut.isPending}
                          className="text-xs text-blue-600 hover:text-blue-800 font-medium"
                        >
                          Reset
                        </button>
                      )}
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

// ---------------------------------------------------------------------------
// Model Card
// ---------------------------------------------------------------------------

function ModelCard({
  model,
  onToggle,
}: {
  model: ModelCatalogEntry;
  onToggle: (model: ModelCatalogEntry) => void;
}) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className={`bg-white border rounded-lg p-4 transition-shadow ${model.status === 'disabled' ? 'opacity-60' : 'shadow-sm hover:shadow-md'}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <h3 className="text-sm font-semibold text-gray-900 truncate">{model.display_name}</h3>
            <span className={`text-xs px-2 py-0.5 rounded-full font-medium shrink-0 ${STATUS_STYLES[model.status] ?? ''}`}>
              {model.status}
            </span>
          </div>
          <p className="text-xs font-mono text-gray-400">{model.model_id}</p>
        </div>
        <button
          onClick={() => onToggle(model)}
          className={`w-10 h-5 rounded-full relative transition-colors shrink-0 ${model.status === 'active' ? 'bg-blue-600' : 'bg-gray-300'}`}
          aria-label={model.status === 'active' ? 'Disable model' : 'Enable model'}
        >
          <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-transform ${model.status === 'active' ? 'left-5' : 'left-0.5'}`} />
        </button>
      </div>

      {/* Capabilities */}
      <div className="flex flex-wrap gap-1 mt-2 mb-3">
        {model.capabilities.map((cap) => (
          <span key={cap} className="text-xs bg-gray-100 text-gray-600 px-1.5 py-0.5 rounded">
            {cap}
          </span>
        ))}
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-3 gap-2 text-center">
        <div className="bg-gray-50 rounded p-1.5">
          <p className="text-xs text-gray-500">Context</p>
          <p className="text-xs font-semibold text-gray-900">{(model.context_window / 1000).toFixed(0)}K</p>
        </div>
        <div className="bg-gray-50 rounded p-1.5">
          <p className="text-xs text-gray-500">In $/1K</p>
          <p className="text-xs font-semibold text-gray-900">${model.input_price_per_1k.toFixed(3)}</p>
        </div>
        <div className="bg-gray-50 rounded p-1.5">
          <p className="text-xs text-gray-500">Out $/1K</p>
          <p className="text-xs font-semibold text-gray-900">${model.output_price_per_1k.toFixed(3)}</p>
        </div>
      </div>

      {/* Expand */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="mt-2 text-xs text-blue-600 hover:text-blue-800"
      >
        {expanded ? 'Less ↑' : 'More ↓'}
      </button>

      {expanded && (
        <div className="mt-3 space-y-1 text-xs text-gray-600">
          <div className="flex justify-between"><span>Tier</span><span className="font-semibold">{model.tier}</span></div>
          <div className="flex justify-between"><span>Max output</span><span className="font-mono">{model.max_output_tokens.toLocaleString()} tokens</span></div>
          <div className="flex justify-between"><span>Regions</span><span>{model.region_availability.join(', ') || 'Global'}</span></div>
          <div className="flex justify-between"><span>Updated</span><span>{new Date(model.updated_at).toLocaleDateString()}</span></div>
        </div>
      )}
    </div>
  );
}
