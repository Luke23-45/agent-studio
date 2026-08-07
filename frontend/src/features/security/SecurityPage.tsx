/**
 * Security page (P7-4 MFA): per-key TOTP enrollment, proof generation and
 * disabling. Operator sessions minted by OIDC SSO are MFA-managed by the
 * identity provider and shown as such.
 */

import { useCallback, useEffect, useState } from 'react';
import { apiErrorMessage } from '../../lib/api/client';
import { confirmMfa, disableMfa, fetchMfaStatus, requestMfaProof, setupMfa } from '../../lib/api/endpoints';
import { MfaProofResult, MfaSetupResult, MfaStatus } from '../../lib/api/types';

export function SecurityPage() {
  const [status, setStatus] = useState<MfaStatus | null>(null);
  const [setup, setSetup] = useState<MfaSetupResult | null>(null);
  const [code, setCode] = useState('');
  const [proof, setProof] = useState<MfaProofResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setStatus(await fetchMfaStatus());
    } catch (err) {
      setError(apiErrorMessage(err));
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const startSetup = async () => {
    setBusy(true);
    setError(null);
    try {
      setSetup(await setupMfa());
      setCode('');
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const confirm = async () => {
    if (!code.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await confirmMfa(code.trim());
      setSetup(null);
      setNotice('MFA is now enabled for this key.');
      await refresh();
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const generateProof = async () => {
    if (!code.trim()) return;
    setBusy(true);
    setError(null);
    try {
      setProof(await requestMfaProof(code.trim()));
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const disable = async () => {
    if (!proof) return;
    setBusy(true);
    setError(null);
    try {
      await disableMfa(proof.proof);
      setProof(null);
      setNotice('MFA disabled for this key.');
      await refresh();
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const statusLine = status ? (
    status.enabled ? (
      <span className="text-green-700 font-medium">Enabled</span>
    ) : (
      <span className="text-gray-500 font-medium">Not enabled</span>
    )
  ) : (
    <span className="text-gray-400">…</span>
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Security</h1>
        <p className="mt-1 text-sm text-gray-600">
          Second-factor authentication for privileged console actions.
        </p>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm text-red-700">
          {error}
        </div>
      )}
      {notice && (
        <div className="bg-green-50 border border-green-200 rounded-lg p-3 text-sm text-green-700">
          {notice}
        </div>
      )}

      <div className="bg-white rounded-lg border p-6 max-w-2xl space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">TOTP authenticator</h2>
            <p className="text-sm text-gray-600">
              Status: {statusLine}
            </p>
          </div>
          {status && !status.enabled && !status.sessionBased && !setup && (
            <button
              type="button"
              onClick={startSetup}
              disabled={busy}
              className="px-4 py-2 rounded-md text-sm font-medium bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
            >
              Set up
            </button>
          )}
        </div>

        {status?.sessionBased && (
          <p className="text-sm text-gray-600 bg-gray-50 border rounded-lg p-3">
            This session was minted by your identity provider (OIDC SSO) — MFA is managed
            there and no local enrollment is needed.
          </p>
        )}

        {setup && (
          <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 space-y-3">
            <p className="text-sm text-blue-800">
              Scan the QR with your authenticator app, or enter the secret manually, then
              confirm with a code. MFA activates only after confirmation.
            </p>
            <div className="text-center">
              <p className="text-[11px] uppercase tracking-wide text-gray-500">otpauth URI</p>
              <a
                href={setup.otpauthUri}
                className="text-blue-700 text-sm break-all underline"
                title="Open in authenticator app"
              >
                {setup.otpauthUri}
              </a>
              <p className="mt-2 text-[11px] uppercase tracking-wide text-gray-500">Manual secret</p>
              <code className="text-sm bg-white border border-gray-200 rounded px-2 py-1 inline-block">
                {setup.secret}
              </code>
            </div>
            <div className="flex gap-2">
              <input
                type="text"
                inputMode="numeric"
                placeholder="6-digit code"
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                className="flex-1 px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
              <button
                type="button"
                onClick={confirm}
                disabled={busy || !code.trim()}
                className="px-4 py-2 rounded-md text-sm font-medium bg-green-600 text-white hover:bg-green-700 disabled:opacity-50"
              >
                {busy ? 'Confirming…' : 'Confirm'}
              </button>
            </div>
          </div>
        )}

        {status?.enabled && (
          <div className="space-y-4 border-t border-gray-100 pt-4">
            <div>
              <h3 className="text-sm font-semibold text-gray-900">Generate a proof</h3>
              <p className="text-sm text-gray-600">
                Proofs unlock privileged actions (publish, reset, key management) for 60
                seconds. Generate one before performing such an action.
              </p>
            </div>
            <div className="flex gap-2">
              <input
                type="text"
                inputMode="numeric"
                placeholder="6-digit code"
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                className="flex-1 px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
              <button
                type="button"
                onClick={generateProof}
                disabled={busy || !code.trim()}
                className="px-4 py-2 rounded-md text-sm font-medium bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
              >
                {busy ? 'Generating…' : 'Generate proof'}
              </button>
            </div>
            {proof && (
              <div className="bg-gray-50 border border-gray-200 rounded-lg p-3 text-sm">
                <p className="text-xs text-gray-500">
                  Valid until {new Date(proof.expiresAt).toLocaleTimeString()} — do not share.
                </p>
                <code className="break-all">{proof.proof}</code>
                <div className="mt-2 flex gap-2">
                  <button
                    type="button"
                    onClick={() => navigator.clipboard?.writeText(proof.proof)}
                    className="px-3 py-1 rounded text-xs font-medium bg-gray-200 text-gray-700 hover:bg-gray-300"
                  >
                    Copy
                  </button>
                  <button
                    type="button"
                    onClick={disable}
                    disabled={busy}
                    className="px-3 py-1 rounded text-xs font-medium bg-red-50 text-red-700 border border-red-200 hover:bg-red-100"
                  >
                    Disable MFA
                  </button>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
