/**
 * HarnessWorkbench (P7-5) — Operator-only internal tooling surface.
 *
 * Provides:
 *  - Session browser with kind badges
 *  - Live chat panel with provider/model selector per message
 *  - Fork at any message (split-view comparison)
 *  - Replay a session through a different model
 *  - Side-by-side model comparison for a single prompt
 *  - Cost/latency analytics per message
 *
 * AGENTS.md boundary: this surface is visible only to operator+ roles.
 * No customer traffic is ever routed through the harness.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { apiErrorMessage } from '../../lib/api/client';
import {
  createHarnessSession,
  deleteHarnessSession,
  forkHarnessSession,
  getHarnessSession,
  listHarnessSessions,
  replayHarnessSession,
  runModelComparison,
  sendHarnessMessage,
} from '../../lib/api/endpoints';
import type { HarnessComparisonResult, HarnessMessage, HarnessSession } from '../../lib/api/types';

const KIND_STYLES: Record<string, string> = {
  provider_test:     'bg-blue-100 text-blue-800',
  model_comparison:  'bg-purple-100 text-purple-800',
  prompt_ab:         'bg-amber-100 text-amber-800',
  fork_replay:       'bg-teal-100 text-teal-800',
  adversarial_sweep: 'bg-red-100 text-red-800',
};

const KIND_LABELS: Record<string, string> = {
  provider_test:     'Provider Test',
  model_comparison:  'Model Comparison',
  prompt_ab:         'Prompt A/B',
  fork_replay:       'Fork / Replay',
  adversarial_sweep: 'Adversarial Sweep',
};

const PROVIDER_OPTIONS = [
  { value: 'openai', label: '🟢 OpenAI' },
  { value: 'anthropic', label: '🟣 Anthropic' },
  { value: 'google', label: '🔵 Google' },
  { value: 'groq', label: '⚡ Groq' },
  { value: 'mistral', label: '🔴 Mistral' },
];

export function HarnessWorkbench() {
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState<'sessions' | 'compare'>('sessions');
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const sessions = useQuery({
    queryKey: ['harness-sessions'],
    queryFn: listHarnessSessions,
    refetchInterval: 10_000,
  });

  const sessionDetail = useQuery({
    queryKey: ['harness-session', selectedSessionId],
    queryFn: () => getHarnessSession(selectedSessionId!),
    enabled: !!selectedSessionId,
    refetchInterval: 5_000,
  });

  const deleteMut = useMutation({
    mutationFn: deleteHarnessSession,
    onSuccess: () => {
      setSelectedSessionId(null);
      queryClient.invalidateQueries({ queryKey: ['harness-sessions'] });
      setActionMsg('Session deleted.');
    },
    onError: (err) => setActionError(apiErrorMessage(err)),
  });

  const forkMut = useMutation({
    mutationFn: ({ sessionId, atMessageId }: { sessionId: string; atMessageId?: string }) =>
      forkHarnessSession(sessionId, atMessageId),
    onSuccess: (forked) => {
      queryClient.invalidateQueries({ queryKey: ['harness-sessions'] });
      setSelectedSessionId(forked.id);
      setActionMsg(`Forked → ${forked.name}`);
    },
    onError: (err) => setActionError(apiErrorMessage(err)),
  });

  const replayMut = useMutation({
    mutationFn: ({ sessionId, model, provider }: { sessionId: string; model?: string; provider?: string }) =>
      replayHarnessSession(sessionId, { model, provider }),
    onSuccess: (replayed) => {
      queryClient.invalidateQueries({ queryKey: ['harness-sessions'] });
      setSelectedSessionId(replayed.id);
      setActionMsg(`Replay complete → ${replayed.name}`);
    },
    onError: (err) => setActionError(apiErrorMessage(err)),
  });

  const tabs = [
    { key: 'sessions' as const, label: 'Sessions' },
    { key: 'compare' as const, label: 'Model Comparison' },
  ];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-2">
            ⚗️ Harness Workbench
            <span className="text-xs bg-orange-100 text-orange-700 px-2 py-0.5 rounded-full font-medium border border-orange-200">
              INTERNAL ONLY
            </span>
          </h1>
          <p className="text-sm text-gray-500 mt-1">
            Provider testing, model comparison, session fork/replay. Never routes customer traffic.
          </p>
        </div>
      </div>

      {/* Alerts */}
      {actionMsg && (
        <div className="bg-green-50 border border-green-200 rounded-lg p-3 text-sm text-green-700 flex justify-between">
          {actionMsg} <button onClick={() => setActionMsg(null)} className="text-green-500">×</button>
        </div>
      )}
      {actionError && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-700 flex justify-between">
          {actionError} <button onClick={() => setActionError(null)} className="text-red-500">×</button>
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

      {activeTab === 'sessions' && (
        <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
          {/* Session list */}
          <div className="xl:col-span-1 space-y-4">
            <div className="flex justify-between items-center">
              <h2 className="text-sm font-semibold text-gray-700 uppercase tracking-wider">Sessions</h2>
              <button
                onClick={() => setShowCreateForm(!showCreateForm)}
                className="text-sm text-blue-600 hover:text-blue-800 font-medium"
              >
                {showCreateForm ? 'Cancel' : '+ New'}
              </button>
            </div>

            {showCreateForm && (
              <CreateSessionForm
                onCreated={(session) => {
                  setShowCreateForm(false);
                  setSelectedSessionId(session.id);
                  queryClient.invalidateQueries({ queryKey: ['harness-sessions'] });
                  setActionMsg(`Session "${session.name}" created.`);
                }}
                onError={(err) => setActionError(err)}
              />
            )}

            {sessions.isLoading ? (
              <div className="text-sm text-gray-500 py-4 text-center">Loading sessions…</div>
            ) : (sessions.data ?? []).length === 0 ? (
              <div className="bg-white rounded-lg shadow p-6 text-center text-gray-400 text-sm">
                No sessions yet. Create one above.
              </div>
            ) : (
              <div className="space-y-2">
                {(sessions.data ?? []).map((s) => (
                  <div
                    key={s.id}
                    onClick={() => setSelectedSessionId(s.id)}
                    className={`bg-white rounded-lg shadow p-3 cursor-pointer border-2 transition-colors ${
                      selectedSessionId === s.id
                        ? 'border-blue-400'
                        : 'border-transparent hover:border-gray-200'
                    }`}
                  >
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-sm font-medium text-gray-900 truncate">{s.name}</span>
                      <span className={`text-xs px-1.5 py-0.5 rounded font-medium shrink-0 ml-2 ${KIND_STYLES[s.kind] ?? 'bg-gray-100 text-gray-600'}`}>
                        {KIND_LABELS[s.kind] ?? s.kind}
                      </span>
                    </div>
                    <div className="flex items-center gap-2 text-xs text-gray-400">
                      <span>{s.provider}</span>
                      <span>·</span>
                      <span className="font-mono truncate">{s.model}</span>
                      <span>·</span>
                      <span>{s.messages.length} msgs</span>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Session detail panel */}
          <div className="xl:col-span-2">
            {sessionDetail.data ? (
              <SessionPanel
                session={sessionDetail.data}
                onFork={(atMessageId) => forkMut.mutate({ sessionId: sessionDetail.data!.id, atMessageId })}
                onReplay={(model, provider) => replayMut.mutate({ sessionId: sessionDetail.data!.id, model, provider })}
                onDelete={() => {
                  if (confirm(`Delete session "${sessionDetail.data!.name}"?`)) {
                    deleteMut.mutate(sessionDetail.data!.id);
                  }
                }}
                onMessage={() => queryClient.invalidateQueries({ queryKey: ['harness-session', selectedSessionId] })}
                forking={forkMut.isPending}
                replaying={replayMut.isPending}
              />
            ) : selectedSessionId ? (
              <div className="bg-white rounded-lg shadow p-8 text-center text-gray-400">Loading session…</div>
            ) : (
              <div className="bg-white rounded-lg shadow p-12 text-center">
                <p className="text-4xl mb-3">⚗️</p>
                <p className="text-gray-400">Select a session from the left or create a new one.</p>
              </div>
            )}
          </div>
        </div>
      )}

      {activeTab === 'compare' && <ComparePanel />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Create Session Form
// ---------------------------------------------------------------------------

function CreateSessionForm({
  onCreated,
  onError,
}: {
  onCreated: (session: HarnessSession) => void;
  onError: (err: string) => void;
}) {
  const [name, setName]     = useState('');
  const [kind, setKind]     = useState('provider_test');
  const [provider, setProv] = useState('openai');
  const [model, setModel]   = useState('gpt-4o');

  const createMut = useMutation({
    mutationFn: () => createHarnessSession({ name, kind: kind as any, provider, model }),
    onSuccess: onCreated,
    onError: (err) => onError(apiErrorMessage(err)),
  });

  return (
    <form
      onSubmit={(e) => { e.preventDefault(); createMut.mutate(); }}
      className="bg-white rounded-lg shadow p-4 space-y-3 border border-blue-100"
    >
      <div>
        <label className="text-xs font-medium text-gray-600 block mb-1">Session Name</label>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. GPT-4o vs Claude Sonnet"
          className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
      </div>
      <div>
        <label className="text-xs font-medium text-gray-600 block mb-1">Kind</label>
        <select value={kind} onChange={(e) => setKind(e.target.value)} className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm bg-white focus:outline-none focus:ring-2 focus:ring-blue-500">
          {Object.entries(KIND_LABELS).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
        </select>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <div>
          <label className="text-xs font-medium text-gray-600 block mb-1">Provider</label>
          <select value={provider} onChange={(e) => setProv(e.target.value)} className="w-full px-2 py-2 border border-gray-300 rounded-md text-sm bg-white focus:outline-none focus:ring-2 focus:ring-blue-500">
            {PROVIDER_OPTIONS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
          </select>
        </div>
        <div>
          <label className="text-xs font-medium text-gray-600 block mb-1">Model</label>
          <input value={model} onChange={(e) => setModel(e.target.value)} className="w-full px-2 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" placeholder="model-id" />
        </div>
      </div>
      <button type="submit" disabled={!name.trim() || createMut.isPending} className="w-full btn-primary">
        {createMut.isPending ? 'Creating…' : 'Create Session'}
      </button>
    </form>
  );
}

// ---------------------------------------------------------------------------
// Session Panel
// ---------------------------------------------------------------------------

function SessionPanel({
  session,
  onFork,
  onReplay,
  onDelete,
  onMessage,
  forking,
  replaying,
}: {
  session: HarnessSession;
  onFork: (atMessageId?: string) => void;
  onReplay: (model?: string, provider?: string) => void;
  onDelete: () => void;
  onMessage: () => void;
  forking: boolean;
  replaying: boolean;
}) {
  const [inputText, setInputText]       = useState('');
  const [msgProvider, setMsgProvider]   = useState(session.provider);
  const [msgModel, setMsgModel]         = useState(session.model);
  const [showReplayForm, setShowReplayForm] = useState(false);
  const [replayModel, setReplayModel]   = useState(session.model);
  const [replayProvider, setReplayProvider] = useState(session.provider);
  const [sending, setSending]           = useState(false);
  const queryClient = useQueryClient();

  const sendMut = useMutation({
    mutationFn: () =>
      sendHarnessMessage(session.id, { message: inputText, model: msgModel, provider: msgProvider }),
    onMutate: () => setSending(true),
    onSuccess: () => {
      setInputText('');
      setSending(false);
      queryClient.invalidateQueries({ queryKey: ['harness-session', session.id] });
      onMessage();
    },
    onError: () => setSending(false),
  });

  const totalCost = session.messages
    .filter((m) => m.role === 'assistant')
    .reduce((sum, m) => sum + (m.cost ?? 0), 0);

  const avgLatency = (() => {
    const assistantMsgs = session.messages.filter((m) => m.role === 'assistant' && m.latency_ms);
    if (!assistantMsgs.length) return null;
    return Math.round(assistantMsgs.reduce((sum, m) => sum + (m.latency_ms ?? 0), 0) / assistantMsgs.length);
  })();

  return (
    <div className="bg-white rounded-lg shadow flex flex-col h-[680px]">
      {/* Header */}
      <div className="px-5 py-3 border-b border-gray-200 flex items-center justify-between gap-3">
        <div className="flex-1 min-w-0">
          <h2 className="text-sm font-semibold text-gray-900 truncate">{session.name}</h2>
          <div className="flex items-center gap-3 mt-0.5 text-xs text-gray-400">
            <span className={`px-1.5 py-0.5 rounded font-medium ${KIND_STYLES[session.kind] ?? 'bg-gray-100 text-gray-600'}`}>
              {KIND_LABELS[session.kind] ?? session.kind}
            </span>
            <span>{session.provider} / <span className="font-mono">{session.model}</span></span>
            {totalCost > 0 && <span>💰 ${totalCost.toFixed(4)}</span>}
            {avgLatency && <span>⚡ {avgLatency}ms avg</span>}
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={() => onFork(undefined)}
            disabled={forking}
            className="text-xs text-indigo-600 hover:text-indigo-800 font-medium border border-indigo-200 rounded px-2 py-1"
          >
            {forking ? '…' : 'Fork'}
          </button>
          <button
            onClick={() => setShowReplayForm(!showReplayForm)}
            className="text-xs text-teal-600 hover:text-teal-800 font-medium border border-teal-200 rounded px-2 py-1"
          >
            Replay
          </button>
          <button
            onClick={onDelete}
            className="text-xs text-red-400 hover:text-red-600 border border-red-200 rounded px-2 py-1"
          >
            Delete
          </button>
        </div>
      </div>

      {/* Replay form */}
      {showReplayForm && (
        <div className="px-5 py-3 border-b border-amber-100 bg-amber-50 flex items-end gap-3">
          <div>
            <label className="text-xs text-amber-700 font-medium block mb-1">Replay Provider</label>
            <select value={replayProvider} onChange={(e) => setReplayProvider(e.target.value)} className="px-2 py-1.5 border border-amber-300 rounded text-sm bg-white">
              {PROVIDER_OPTIONS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
            </select>
          </div>
          <div>
            <label className="text-xs text-amber-700 font-medium block mb-1">Replay Model</label>
            <input value={replayModel} onChange={(e) => setReplayModel(e.target.value)} className="px-2 py-1.5 border border-amber-300 rounded text-sm w-36" />
          </div>
          <button
            onClick={() => { onReplay(replayModel, replayProvider); setShowReplayForm(false); }}
            disabled={replaying}
            className="bg-amber-600 text-white px-3 py-1.5 rounded text-sm font-medium hover:bg-amber-700"
          >
            {replaying ? 'Replaying…' : '▶ Run Replay'}
          </button>
          <button onClick={() => setShowReplayForm(false)} className="text-amber-600 text-sm">Cancel</button>
        </div>
      )}

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4 scroll-smooth">
        {session.messages.length === 0 ? (
          <div className="flex items-center justify-center h-full text-gray-300 text-sm">
            Send the first message to start the session.
          </div>
        ) : (
          session.messages.map((msg) => (
            <HarnessMessageBubble
              key={msg.id}
              message={msg}
              onFork={() => onFork(msg.id)}
              forking={forking}
            />
          ))
        )}
      </div>

      {/* Input bar */}
      <div className="px-5 py-3 border-t border-gray-200 space-y-2">
        <div className="flex gap-2 text-xs">
          <select
            value={msgProvider}
            onChange={(e) => setMsgProvider(e.target.value)}
            className="px-2 py-1 border border-gray-300 rounded bg-white focus:outline-none focus:ring-1 focus:ring-blue-500"
          >
            {PROVIDER_OPTIONS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
          </select>
          <input
            value={msgModel}
            onChange={(e) => setMsgModel(e.target.value)}
            className="flex-1 px-2 py-1 border border-gray-300 rounded font-mono focus:outline-none focus:ring-1 focus:ring-blue-500"
            placeholder="model-id"
          />
        </div>
        <div className="flex gap-2">
          <textarea
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                if (inputText.trim() && !sending) sendMut.mutate();
              }
            }}
            placeholder="Type a message… (Enter to send, Shift+Enter for newline)"
            rows={2}
            className="flex-1 px-3 py-2 border border-gray-300 rounded-md text-sm resize-none focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
          <button
            onClick={() => sendMut.mutate()}
            disabled={!inputText.trim() || sending}
            className="btn-primary self-end"
          >
            {sending ? '…' : 'Send'}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Harness Message Bubble
// ---------------------------------------------------------------------------

function HarnessMessageBubble({
  message,
  onFork,
  forking,
}: {
  message: HarnessMessage;
  onFork: () => void;
  forking: boolean;
}) {
  const isUser = message.role === 'user';
  const [showMeta, setShowMeta] = useState(false);

  return (
    <div className={`group flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div className={`max-w-[80%] rounded-xl px-4 py-3 ${
        isUser
          ? 'bg-blue-600 text-white rounded-br-sm'
          : 'bg-gray-100 text-gray-900 rounded-bl-sm'
      }`}>
        <p className="text-sm whitespace-pre-wrap">{message.content}</p>

        {/* Meta bar for assistant messages */}
        {!isUser && (message.latency_ms || message.cost || message.model) && (
          <div className="mt-2 flex items-center gap-3 text-xs opacity-60">
            {message.provider && <span>{message.provider}</span>}
            {message.model && <span className="font-mono">{message.model}</span>}
            {message.latency_ms && <span>⚡{message.latency_ms}ms</span>}
            {message.cost != null && message.cost > 0 && <span>💰${message.cost.toFixed(4)}</span>}
            {message.tokens_used != null && message.tokens_used > 0 && <span>🪙{message.tokens_used.toLocaleString()}</span>}
            <button
              onClick={() => setShowMeta(!showMeta)}
              className="underline cursor-pointer"
            >
              {showMeta ? 'less' : 'meta'}
            </button>
          </div>
        )}

        {showMeta && message.metadata && (
          <pre className="mt-2 text-xs font-mono bg-black/10 rounded p-2 overflow-x-auto max-h-40">
            {JSON.stringify(message.metadata, null, 2)}
          </pre>
        )}

        {/* Fork button on non-user messages */}
        {!isUser && (
          <div className="mt-1.5 flex items-center gap-2">
            <button
              onClick={onFork}
              disabled={forking}
              className="text-xs text-indigo-500 hover:text-indigo-700 font-medium opacity-0 group-hover:opacity-100 transition-opacity"
            >
              ⎇ Fork here
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Compare Panel
// ---------------------------------------------------------------------------

function ComparePanel() {
  const [prompt, setPrompt]             = useState('');
  const [comparisonRows, setCompRows]   = useState<{ provider: string; model: string }[]>([
    { provider: 'openai', model: 'gpt-4o' },
    { provider: 'anthropic', model: 'claude-3-5-sonnet-20241022' },
  ]);
  const [result, setResult]             = useState<HarnessComparisonResult | null>(null);
  const [running, setRunning]           = useState(false);
  const [error, setError]               = useState<string | null>(null);

  const addRow = () => setCompRows([...comparisonRows, { provider: 'openai', model: '' }]);
  const removeRow = (idx: number) => setCompRows(comparisonRows.filter((_, i) => i !== idx));
  const updateRow = (idx: number, field: 'provider' | 'model', value: string) => {
    setCompRows(comparisonRows.map((r, i) => i === idx ? { ...r, [field]: value } : r));
  };

  const handleRun = async () => {
    if (!prompt.trim() || comparisonRows.some((r) => !r.model.trim())) return;
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const res = await runModelComparison({ prompt, models: comparisonRows });
      setResult(res);
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* Configuration */}
      <div className="bg-white rounded-lg shadow p-6 space-y-4">
        <h2 className="text-lg font-semibold text-gray-900">Side-by-Side Model Comparison</h2>
        <p className="text-sm text-gray-500">Run the same prompt against multiple models simultaneously.</p>

        <div>
          <label className="label">Prompt</label>
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            rows={4}
            placeholder="Enter the prompt to compare across models…"
            className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm resize-none focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>

        <div className="space-y-2">
          <div className="flex justify-between items-center">
            <label className="label">Models to compare</label>
            <button onClick={addRow} className="text-sm text-blue-600 hover:text-blue-800 font-medium">+ Add</button>
          </div>
          {comparisonRows.map((row, idx) => (
            <div key={idx} className="flex gap-2 items-center">
              <span className="text-xs font-semibold text-gray-400 w-5">{idx + 1}.</span>
              <select
                value={row.provider}
                onChange={(e) => updateRow(idx, 'provider', e.target.value)}
                className="px-2 py-1.5 border border-gray-300 rounded text-sm bg-white w-36 focus:outline-none focus:ring-2 focus:ring-blue-500"
              >
                {PROVIDER_OPTIONS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
              </select>
              <input
                value={row.model}
                onChange={(e) => updateRow(idx, 'model', e.target.value)}
                placeholder="model-id"
                className="flex-1 px-2 py-1.5 border border-gray-300 rounded text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
              {comparisonRows.length > 1 && (
                <button onClick={() => removeRow(idx)} className="text-red-400 hover:text-red-600 text-lg leading-none">×</button>
              )}
            </div>
          ))}
        </div>

        {error && (
          <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-700">{error}</div>
        )}

        <button
          onClick={handleRun}
          disabled={running || !prompt.trim() || comparisonRows.some((r) => !r.model.trim())}
          className="btn-primary"
        >
          {running ? 'Running comparison…' : '▶ Run Comparison'}
        </button>
      </div>

      {/* Results */}
      {result && (
        <div className="space-y-4">
          <h2 className="text-lg font-semibold text-gray-900">
            Results <span className="text-sm font-normal text-gray-400">— {result.results.length} models</span>
          </h2>

          {/* Summary bar — sorted by latency */}
          <div className="bg-white rounded-lg shadow p-4 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-200">
                  <th className="text-left py-2 px-3 text-xs font-semibold text-gray-500 uppercase">Model</th>
                  <th className="text-right py-2 px-3 text-xs font-semibold text-gray-500 uppercase">Latency</th>
                  <th className="text-right py-2 px-3 text-xs font-semibold text-gray-500 uppercase">In / Out tokens</th>
                  <th className="text-right py-2 px-3 text-xs font-semibold text-gray-500 uppercase">Cost</th>
                  <th className="text-right py-2 px-3 text-xs font-semibold text-gray-500 uppercase">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50">
                {[...result.results]
                  .sort((a, b) => a.latency_ms - b.latency_ms)
                  .map((r, idx) => (
                  <tr key={idx} className={idx === 0 ? 'bg-green-50/40' : ''}>
                    <td className="py-2.5 px-3 font-medium text-gray-900">
                      {idx === 0 && <span className="text-green-600 mr-1">🏆</span>}
                      {r.provider} / <span className="font-mono">{r.model}</span>
                    </td>
                    <td className="py-2.5 px-3 text-right font-mono text-gray-700">{r.latency_ms}ms</td>
                    <td className="py-2.5 px-3 text-right font-mono text-gray-500">
                      {r.input_tokens.toLocaleString()} / {r.output_tokens.toLocaleString()}
                    </td>
                    <td className="py-2.5 px-3 text-right font-mono text-gray-700">${r.cost.toFixed(4)}</td>
                    <td className="py-2.5 px-3 text-right">
                      {r.error
                        ? <span className="badge-error">error</span>
                        : <span className="badge-success">ok</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Response cards */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {result.results.map((r, idx) => (
              <div key={idx} className={`bg-white rounded-lg shadow p-5 border-l-4 ${r.error ? 'border-red-400' : 'border-blue-400'}`}>
                <div className="flex items-center gap-2 mb-3">
                  <h3 className="text-sm font-semibold text-gray-900">{r.provider} / <span className="font-mono">{r.model}</span></h3>
                  <span className="ml-auto text-xs text-gray-400 font-mono">{r.latency_ms}ms · ${r.cost.toFixed(4)}</span>
                </div>
                {r.error ? (
                  <p className="text-sm text-red-600 bg-red-50 rounded p-3">{r.error}</p>
                ) : (
                  <p className="text-sm text-gray-700 whitespace-pre-wrap leading-relaxed">{r.response}</p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
