import { useCallback, useEffect, useState } from "react";
import type { TsApiStatus } from "../types";
import { api } from "../api";
import { Alert } from "../components/Alert";
import { RegistriesCard } from "../components/RegistriesCard";
import { TsApiWizard } from "../components/TsApiWizard";
import { UpgradeCard } from "../components/UpgradeCard";

export function Settings() {
  const [status, setStatus] = useState<TsApiStatus | null>(null);
  const [replacing, setReplacing] = useState(false);

  const refresh = useCallback(() => {
    api.tsapi().then(setStatus).catch(() => setStatus(null));
  }, []);

  useEffect(refresh, [refresh]);

  return (
    <>
      <h1 className="page-title">Settings</h1>

      <div style={{ maxWidth: 640 }}>
        <div className="section-title" style={{ marginTop: "var(--sp-5)" }}>
          Controller
        </div>
        <UpgradeCard />

        <div className="section-title" style={{ marginTop: "var(--sp-6)" }}>
          Tailscale API credential
        </div>

        {status === null ? (
          <p style={{ color: "var(--muted)" }}>Loading…</p>
        ) : status.configured ? (
          <>
            <Alert kind="ok">Configured.</Alert>
            {!replacing && (
              <div className="preview-row" style={{ marginTop: "var(--sp-3)" }}>
                <button
                  className="btn btn--ghost btn--sm"
                  onClick={() => setReplacing(true)}
                >
                  Replace credential
                </button>
              </div>
            )}
          </>
        ) : (
          <>
            {status.error && <Alert kind="err">{status.error}</Alert>}
            <Alert kind="info">
              No API credential yet. Without one, Tailarr can’t mint auth keys,
              manage user machines, or sync the ACL policy — the wizard below
              sets it up in a couple of minutes.
            </Alert>
          </>
        )}

        {(status !== null && (!status.configured || replacing)) && (
          <TsApiWizard
            onDone={() => {
              setReplacing(false);
              refresh();
            }}
          />
        )}

        <div className="section-title" style={{ marginTop: "var(--sp-6)" }}>
          Private registries
        </div>
        <RegistriesCard />
      </div>
    </>
  );
}
