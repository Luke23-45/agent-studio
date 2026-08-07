/* eslint-disable react-refresh/only-export-components */
/**
 * MFA proof plumbing (P7-4).
 *
 * Privileged actions (policy publish, model enable/disable, circuit-breaker
 * reset, key management) require a short-lived `X-MFA-Proof` header when the
 * calling API key has MFA enabled. `useMfaProof` inspects the key's MFA
 * status: keys without MFA proceed untouched; keys with MFA prompt for a
 * TOTP code via a modal, mint the proof, and attach it.
 */

import { useState } from 'react';
import { create } from 'zustand';
import { apiErrorMessage } from '../../lib/api/client';
import { fetchMfaStatus, requestMfaProof } from '../../lib/api/endpoints';

export class MfaCancelled extends Error {}

interface MfaUiState {
  open: boolean;
  busy: boolean;
  error: string | null;
  resolve: ((proof: string | null) => void) | null;
}

export const useMfaUi = create<MfaUiState>(() => ({
  open: false,
  busy: false,
  error: null,
  resolve: null,
}));

/**
 * Return a fresh MFA proof, prompting for a TOTP code when the key has MFA
 * enabled. Returns `null` when MFA is not required (no header needed);
 * throws `MfaCancelled` when the operator dismisses the prompt.
 */
export function useMfaProof() {
  const getProof = async (): Promise<string | null> => {
    const status = await fetchMfaStatus();
    if (!status.enabled) return null;
    return new Promise<string | null>((resolve) => {
      useMfaUi.setState({ open: true, busy: false, error: null, resolve });
    });
  };
  return { getProof };
}

export function MfaModalHost() {
  const { open, error, resolve } = useMfaUi();
  const [code, setCode] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  if (!open) return null;

  const close = (proof: string | null) => {
    useMfaUi.setState({ open: false, busy: false, error: null, resolve: null });
    setCode('');
    setLocalError(null);
    resolve?.(proof);
  };

  const submit = async () => {
    if (!code.trim() || submitting) return;
    setSubmitting(true);
    setLocalError(null);
    try {
      const result = await requestMfaProof(code.trim());
      close(result.proof);
    } catch (err) {
      setLocalError(apiErrorMessage(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-gray-900/40 flex items-center justify-center z-50">
      <div className="bg-white rounded-lg shadow-xl p-6 w-full max-w-sm">
        <h2 className="text-lg font-bold text-gray-900">Two-factor authentication</h2>
        <p className="mt-1 text-sm text-gray-600">
          This key has MFA enabled. Enter the current code from your authenticator app to
          authorize this action.
        </p>
        <input
          type="text"
          autoFocus
          inputMode="numeric"
          placeholder="6-digit code"
          value={code}
          onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
          onKeyDown={(e) => {
            if (e.key === 'Enter') submit();
          }}
          className="mt-4 w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
        {(error || localError) && <p className="mt-2 text-sm text-red-600">{error ?? localError}</p>}
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={() => close(null)}
            className="px-4 py-2 rounded-md text-sm font-medium text-gray-600 hover:bg-gray-100"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={submitting || !code.trim()}
            onClick={submit}
            className="px-4 py-2 rounded-md text-sm font-medium bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
          >
            {submitting ? 'Verifying…' : 'Authorize'}
          </button>
        </div>
      </div>
    </div>
  );
}
