import { useCallback, useEffect, useState } from "react";
import type { TsApiStatus } from "../types";
import { api } from "../api";
import { Alert } from "../components/Alert";
import { AccountsCard } from "../components/AccountsCard";
import { RegistriesCard } from "../components/RegistriesCard";
import { ThemesCard } from "../components/ThemesCard";
import { TsApiWizard } from "../components/TsApiWizard";
import { UpgradeCard } from "../components/UpgradeCard";

// Each section = a title + one raised panel. Sections breathe via the
// section-title's own top rhythm (--sp-12) — don't override it tighter.
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

      <div style={{ maxWidth: 720 }}>
        <div className="section-title" style={{ marginTop: "var(--sp-6)" }}>
          Controller
        </div>
        <div className="card panel">
          <UpgradeCard />
        </div>

        <div className="section-title">Tailscale API credential</div>
        <div className="card panel">
          {status === null ? (
            <p style={{ color: "var(--muted)", margin: 0 }}>Loading…</p>
          ) : status.configured ? (
            <>
              <Alert kind="ok">Configured.</Alert>
              {!replacing && (
                <div className="preview-row" style={{ marginTop: "var(--sp-4)" }}>
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
                No API credential yet. Without one, Tailarr can’t mint auth
                keys, manage user machines, or sync the ACL policy — the
                wizard below sets it up in a couple of minutes.
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
        </div>

        <div className="section-title">Themes</div>
        <div className="card panel">
          <ThemesCard />
        </div>

        <div className="section-title">Accounts</div>
        <div className="card panel">
          <AccountsCard />
        </div>

        <div className="section-title">Private registries</div>
        <div className="card panel">
          <RegistriesCard />
        </div>
      </div>
    </>
  );
}
