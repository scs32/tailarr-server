import { useCallback, useEffect, useState } from "react";
import type { RelayStatus } from "../types";
import { api } from "../api";
import { Alert } from "./Alert";

// Peer relay (apple/container installs). The host Mac relays pod traffic
// that would otherwise fall back to DERP; the controller only authors the
// policy grant. Renders nothing on hosts where it doesn't apply.
export function RelayCard() {
  const [status, setStatus] = useState<RelayStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    api.relay().then(setStatus).catch(() => setStatus(null));
  }, []);

  useEffect(refresh, [refresh]);

  const act = (action: "enable" | "disable" | "recheck") => {
    setBusy(true);
    setError(null);
    api
      .relayAction(action)
      .then((r) => {
        setStatus(r.relay);
        if (!r.ok) setError(r.error || r.sync?.error || "Request failed.");
      })
      .catch((e) => setError(String(e)))
      .finally(() => setBusy(false));
  };

  if (status === null || !status.applicable) return null;

  const verified = status.verified.state;
  const macStep = (
    <>
      <p style={{ margin: "var(--sp-2) 0 0" }}>
        On the Mac hosting this install, run once:
      </p>
      <pre
        className="log__body"
        style={{ margin: "var(--sp-2) 0 0", userSelect: "all" }}
      >
        /Applications/Tailscale.app/Contents/MacOS/Tailscale set
        --relay-server-port=40000
      </pre>
      <p style={{ margin: "var(--sp-2) 0 0", color: "var(--muted)" }}>
        (install-mac.sh does this automatically.)
      </p>
    </>
  );

  return (
    <>
      <div className="section-title" style={{ marginTop: "var(--sp-6)" }}>
        Peer relay
      </div>
      {status.grant_active ? (
        verified === "peer-relay" ? (
          <Alert kind="ok">
            Peer relay active — pod traffic goes through your Mac instead of
            Tailscale’s DERP relays.
          </Alert>
        ) : verified === "direct" ? (
          <Alert kind="ok">
            Connections are already direct — no relay needed right now.
          </Alert>
        ) : (
          <Alert kind="info">
            The relay grant is in place, but pod traffic is still using
            Tailscale’s DERP relays (slow).
            {status.verified.detail && (
              <> Last check: {status.verified.detail}.</>
            )}
            {macStep}
          </Alert>
        )
      ) : status.enabled === false ? (
        <Alert kind="info">Peer relay is switched off.</Alert>
      ) : (
        <Alert kind="info">
          This install runs behind apple/container’s NAT, so pod traffic
          falls back to slow DERP relays. Tailarr can fix that with a peer
          relay on your Mac, but it didn’t enable the policy grant
          automatically:
          <ul style={{ margin: "var(--sp-2) 0 0", paddingLeft: "1.2em" }}>
            {(status.reasons.length
              ? status.reasons
              : ["The pre-flight check has not run yet."]
            ).map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        </Alert>
      )}

      {error && <Alert kind="err">{error}</Alert>}

      <div className="preview-row" style={{ marginTop: "var(--sp-3)" }}>
        {status.grant_active ? (
          <button
            className="btn btn--ghost btn--sm"
            disabled={busy}
            onClick={() => act("disable")}
          >
            Disable peer relay
          </button>
        ) : (
          <button
            className="btn btn--sm"
            disabled={busy}
            onClick={() => act("enable")}
          >
            {status.enabled === false
              ? "Enable peer relay"
              : "Enable peer relay anyway"}
          </button>
        )}
        <button
          className="btn btn--ghost btn--sm"
          disabled={busy}
          onClick={() => act("recheck")}
        >
          Re-check
        </button>
      </div>
    </>
  );
}
