import { useCallback, useEffect, useState } from "react";
import type { NetworkEntry, RelayAction, RelayStatus } from "../types";
import { api } from "../api";
import { FlashView, useFlash } from "../components/Flash";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { RelaySection } from "../components/RelaySection";
import { SpinnerIcon } from "../components/Icons";

import { dnsUrl, ipUrl } from "../lib/urls";

// Per-pod networking: the peer-relay section (see RelaySection), then each
// pod's tailnet identity (IP + MagicDNS name) and how it's exposed. Every
// pod is its own tailnet node with HTTPS via `tailscale serve` — the row
// controls are public/private (Tailscale Funnel, a live serve-config flip
// with no pod restart) and, in per-pod relay mode, the pod's relay. Polls
// while mounted so enrolling sidecars and busy pods settle into their real
// state without a reload.
export function Network() {
  const [entries, setEntries] = useState<NetworkEntry[] | null>(null);
  const [relay, setRelay] = useState<RelayStatus | null>(null);
  const [busyPod, setBusyPod] = useState("");
  const [relayBusy, setRelayBusy] = useState(false);
  const [confirmPublic, setConfirmPublic] = useState<NetworkEntry | null>(null);
  const { flash, show, clear } = useFlash();

  const refresh = useCallback(async () => {
    try {
      // System pods (ntfy) are configured from their own page — their
      // networking (incl. funnel) is feature-managed, not per-pod knobs.
      setEntries((await api.network()).filter((e) => !e.system));
    } catch (e) {
      show({ kind: "err", text: String(e) });
    }
    api.relay().then(setRelay).catch(() => {});
  }, [show]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 10000); // settle enrolling/busy pods
    return () => clearInterval(t);
  }, [refresh]);

  // Returns error text (null = success) so the add dialog can show
  // failures INSIDE itself — the page FlashView sits behind the scrim.
  const relayAct = useCallback(
    async (a: RelayAction): Promise<string | null> => {
      setRelayBusy(true);
      try {
        const r = await api.relayAction(a);
        setRelay(r.relay);
        const err = !r.ok || r.error ? (r.error ?? "Request failed.") : null;
        if (err) show({ kind: "err", text: err });
        else if (a.do === "add-relay")
          show({
            kind: "ok",
            text: "Relay added. It activates once traffic flows through it.",
          });
        return err;
      } catch (e) {
        show({ kind: "err", text: String(e) });
        return String(e);
      } finally {
        setRelayBusy(false);
      }
    },
    [show],
  );

  // Per-pod relay selection — the controller row maps to the "server" key.
  const perPod = !!relay?.grant_active && relay.mode === "per-pod";

  async function setFunnel(e: NetworkEntry, funnel: boolean) {
    setConfirmPublic(null);
    setBusyPod(e.name);
    try {
      const r = await api.networkSet(e.name, { funnel });
      show(
        r.ok
          ? {
              kind: "ok",
              text: funnel
                ? `${e.name} is now PUBLIC at https://${e.dns_name || e.name} — needs the public-access setting in your network policy to actually serve.`
                : `${e.name} is private again (tailnet-only).`,
            }
          : { kind: "err", text: r.error ?? r.status },
      );
      refresh();
    } catch (err) {
      show({ kind: "err", text: String(err) });
    } finally {
      setBusyPod("");
    }
  }

  return (
    <>
      <h1 className="page-title">Network</h1>
      <p style={{ color: "var(--muted)", margin: 0 }}>
        Each service gets its own private HTTPS address, reachable from your
        devices — no ports exposed. “Make public” exposes a service to the whole
        internet through Tailscale Funnel.
      </p>

      <FlashView flash={flash} onClose={clear} />

      <RelaySection status={relay} busy={relayBusy} onAct={relayAct} />

      <div className="section-title">Services</div>
      {entries === null ? (
        <p style={{ color: "var(--muted)", margin: 0 }}>Loading…</p>
      ) : entries.length === 0 ? (
        <p style={{ color: "var(--muted)", margin: 0 }}>No services deployed.</p>
      ) : (
        <div className="row-list">
          {entries.map((e) => (
            <div key={e.name} className="row card">
              <span className={`state-dot state-dot--${e.state}`} title={e.state} />
              <div style={{ minWidth: 120 }}>
                <div className="row__title">{e.name}</div>
                <div className="row__meta">
                  {e.dns_name ? (
                    <a
                      href={dnsUrl(e)}
                      target="_blank"
                      rel="noopener noreferrer"
                      title={`Open ${dnsUrl(e)}`}
                    >
                      {e.dns_name}
                    </a>
                  ) : (
                    "(enrolling…)"
                  )}
                  {e.ip && (
                    <>
                      {" · "}
                      <a
                        href={ipUrl(e)}
                        target="_blank"
                        rel="noopener noreferrer"
                        title={`Open ${ipUrl(e)}`}
                      >
                        {e.ip}
                      </a>
                    </>
                  )}
                </div>
              </div>
              <div className="spacer" />
              {perPod && relay && (
                <select
                  className="select"
                  style={{ width: "auto" }}
                  disabled={relayBusy}
                  title="Which of your devices relays this service's traffic when direct connections fail"
                  value={
                    relay.pod_relays[e.controller ? "server" : e.name] ?? ""
                  }
                  onChange={(ev) =>
                    relayAct({
                      do: "set-pod",
                      pod: e.controller ? "server" : e.name,
                      id: ev.target.value,
                    })
                  }
                >
                  <option value="">No relay</option>
                  {relay.relays.map((r) => (
                    <option key={r.id} value={r.id}>
                      via {r.name}
                    </option>
                  ))}
                </select>
              )}
              {e.busy && <span className="chip chip--busy">{e.busy}…</span>}
              <span className="chip chip--installed">tailscale</span>
              {e.https ? (
                <span className="chip chip--installed">https</span>
              ) : (
                <span className="chip">no port</span>
              )}
              {e.funnel && (
                <span className="chip chip--busy" title="Reachable from the public internet via Tailscale Funnel">
                  public
                </span>
              )}
              {e.controller ? (
                <span className="preview-label">Tailarr — managed automatically</span>
              ) : (
                e.https && (
                  <button
                    className={
                      "btn btn--sm " +
                      (e.funnel ? "btn--secondary" : "btn--ghost") +
                      (busyPod === e.name ? " btn--loading" : "")
                    }
                    disabled={!!busyPod || !!e.busy}
                    title={
                      e.funnel
                        ? "Back to private access"
                        : "Expose this service to the public internet (live, no restart)"
                    }
                    onClick={() =>
                      e.funnel ? setFunnel(e, false) : setConfirmPublic(e)
                    }
                  >
                    {busyPod === e.name && <SpinnerIcon className="btn-icon" />}
                    {e.funnel ? "Make private" : "Make public"}
                  </button>
                )
              )}
            </div>
          ))}
        </div>
      )}

      {confirmPublic && (
        <ConfirmDialog
          title={`Make ${confirmPublic.name} public?`}
          confirmLabel="Make public"
          onConfirm={() => setFunnel(confirmPublic, true)}
          onCancel={() => setConfirmPublic(null)}
        >
          This exposes {confirmPublic.name} to the <strong>entire public
          internet</strong> at https://{confirmPublic.dns_name || "its MagicDNS name"}{" "}
          via Tailscale Funnel — anyone with the URL can reach it, no tailnet
          required. The flip is live (no restart) and requires the{" "}
          public-access setting in your network policy.
        </ConfirmDialog>
      )}
    </>
  );
}
