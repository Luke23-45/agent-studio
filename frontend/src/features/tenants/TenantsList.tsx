/**
 * Tenants list backed by the real `GET /tenants` endpoint, with a create
 * form (super admins only). Tenant-bound keys only ever see their own tenant
 * because the backend scopes the response.
 *
 * Clicking a tenant row opens the TenantConfigEditor.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { apiErrorMessage } from '../../lib/api/client';
import { createTenant, listTenants } from '../../lib/api/endpoints';
import { canManageTenants, useAuthStore } from '../../lib/auth/session';
import { TenantConfigEditor } from './TenantConfigEditor';

export function TenantsList() {
  const queryClient = useQueryClient();
  const principal = useAuthStore((s) => s.principal);
  const [name, setName]         = useState('');
  const [slug, setSlug]         = useState('');
  const [error, setError]       = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);

  const tenants = useQuery({ queryKey: ['tenants'], queryFn: listTenants });
  const create  = useMutation({
    mutationFn: createTenant,
    onSuccess: () => {
      setShowForm(false); setName(''); setSlug('');
      queryClient.invalidateQueries({ queryKey: ['tenants'] });
    },
    onError: (err) => setError(apiErrorMessage(err)),
  });

  const canManage = canManageTenants(principal?.role ?? '');

  // Show the config editor when a tenant row is clicked
  if (editingId) {
    return <TenantConfigEditor tenantId={editingId} onBack={() => setEditingId(null)} />;
  }

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-gray-900">Tenants</h1>
        {canManage && (
          <button
            onClick={() => setShowForm((v) => !v)}
            className="btn-primary"
          >
            {showForm ? 'Cancel' : 'Add Tenant'}
          </button>
        )}
      </div>

      {showForm && canManage && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (!name.trim() || !slug.trim()) return;
            setError(null);
            create.mutate({ name: name.trim(), slug: slug.trim() });
          }}
          className="bg-white rounded-lg shadow p-6 space-y-4"
        >
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <input
              placeholder="Name (e.g. Acme Corp)"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <input
              placeholder="Slug (e.g. acme)"
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              className="px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          {error && <p className="text-sm text-red-600">{error}</p>}
          <button
            type="submit"
            disabled={create.isPending || !name.trim() || !slug.trim()}
            className="btn-primary disabled:opacity-50"
          >
            {create.isPending ? 'Creating…' : 'Create Tenant'}
          </button>
        </form>
      )}

      <div className="bg-white shadow rounded-lg overflow-hidden">
        {tenants.isLoading ? (
          <div className="px-6 py-4 text-sm text-gray-500">Loading…</div>
        ) : tenants.error ? (
          <div className="px-6 py-4 text-sm text-red-600">
            Failed to load tenants: {apiErrorMessage(tenants.error)}
          </div>
        ) : (tenants.data ?? []).length === 0 ? (
          <div className="px-6 py-8 text-center text-sm text-gray-400">No tenants yet.</div>
        ) : (
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Name</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Slug</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Allowed Topics</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Escalation Threshold</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Region</th>
              </tr>
            </thead>
            <tbody className="bg-white divide-y divide-gray-200">
              {(tenants.data ?? []).map((tenant) => (
                <tr
                  key={tenant.id}
                  onClick={() => setEditingId(tenant.id)}
                  className="hover:bg-blue-50/30 cursor-pointer transition-colors"
                >
                  <td className="px-6 py-4 text-sm font-medium text-blue-700 hover:underline">{tenant.name}</td>
                  <td className="px-6 py-4 text-sm text-gray-600 font-mono">{tenant.slug}</td>
                  <td className="px-6 py-4 text-sm text-gray-600">
                    {tenant.allowed_topics.length > 0
                      ? tenant.allowed_topics.slice(0, 3).join(', ') +
                        (tenant.allowed_topics.length > 3 ? ` +${tenant.allowed_topics.length - 3}` : '')
                      : '—'}
                  </td>
                  <td className="px-6 py-4 text-sm text-gray-600 font-mono">{tenant.escalation_threshold}</td>
                  <td className="px-6 py-4 text-sm text-gray-500">{tenant.region ?? 'default'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
