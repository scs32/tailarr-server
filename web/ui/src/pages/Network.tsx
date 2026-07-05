import { useCallback, useEffect, useState } from "react";
import type { NetworkEntry } from "../types";
import { api } from "../api";
import { FlashView, useFlash } from "../components/Flash";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { SpinnerIcon } from "../components/Icons";

import { dnsUrl, ipUrl } from "../lib/urls";

// Per-pod networking: each pod's tailnet identity (IP + MagicDNS name) and how
// it's exposed. Every pod is its own tailnet node with HTTPS via `tailscale
// serve` — the only control here is public/private (Tailscale Funnel), a live
// serve-config flip with no pod restart. Polls while mounted so enrolling
// sidecars and busy pods settle into their real state without a reload.
export function Network() {
  const [entries, setEntries] = useState<NetworkEntry[] | null>(null);
  const [busyPod, setBusyPod] = useState("");
  const [confirmPublic, setConfirmPublic] = useState<NetworkEntry | null>(null);
  const { flash, show, clear } = useFlash();

  const refresh = useCallback(async () => {
    try {
      setEntries(await api.network());
    } catch (e) {
      show({ kind: "err", text: String(e) });
    }
  }, [show]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 10000); // settle enrolling/busy pods
    return () => clearInterval(t);
  }, [refresh]);

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
                ? `${e.name} is now PUBLIC at https://${e.dns_name || e.name} — needs the funnel nodeAttr in your tailnet policy to actually serve.`
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
        Each pod is its own device on your tailnet, reachable over HTTPS at its
        MagicDNS name via <code>tailscale serve</code>. “Make public” exposes a
        pod to the whole internet through Tailscale Funnel.
      </p>

      <FlashView flash={flash} onClose={clear} />

      <div className="section-title">Pods</div>
      {entries === null ? (
        <p style={{ color: "var(--muted)", margin: 0 }}>Loading…</p>
      ) : entries.length === 0 ? (
        <p style={{ color: "var(--muted)", margin: 0 }}>No pods deployed.</p>
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
                <span className="preview-label">controller — managed by bootstrap</span>
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
                        ? "Back to tailnet-only access"
                        : "Expose this pod to the public internet via Tailscale Funnel (live, no restart)"
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
          <code>funnel</code> nodeAttr in your tailnet policy.
        </ConfirmDialog>
      )}
    </>
  );
}
