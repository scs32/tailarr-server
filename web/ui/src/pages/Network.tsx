import { useCallback, useEffect, useState } from "react";
import type { NetworkEntry } from "../types";
import { api } from "../api";
import { Alert } from "../components/Alert";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { SpinnerIcon } from "../components/Icons";

// Best-guess launch URLs for a pod. The MagicDNS name gets HTTPS on 443
// when tailscale serve terminates TLS, else plain http on the service's
// first port. The IP link goes straight at the service port (no cert
// warnings — the ts.net certificate only matches the DNS name).
function dnsUrl(e: NetworkEntry): string {
  const port = Object.values(e.ports)[0];
  if (e.https) return `https://${e.dns_name}`;
  return port ? `http://${e.dns_name}:${port}` : `http://${e.dns_name}`;
}

function ipUrl(e: NetworkEntry): string {
  const port = Object.values(e.ports)[0];
  if (port) return `http://${e.ip}:${port}`;
  return e.https ? `https://${e.ip}` : `http://${e.ip}`;
}

// Per-pod networking: tailnet identity (IP + MagicDNS name) and the
// tailscale / HTTPS-serve switches. Flipping a switch re-renders the pod's
// scripts and restarts it.
export function Network() {
  const [entries, setEntries] = useState<NetworkEntry[] | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [busy, setBusy] = useState<string>(""); // "<pod>:<what>"
  const [confirmTs, setConfirmTs] = useState<string | null>(null); // pod pending TS disable

  const refresh = useCallback(async () => {
    try {
      setEntries(await api.network());
    } catch (e) {
      setMsg({ kind: "err", text: String(e) });
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function apply(pod: string, what: string, body: { tailscale?: boolean; https?: boolean }) {
    setBusy(`${pod}:${what}`);
    try {
      const r = await api.networkSet(pod, body);
      setMsg(
        r.ok
          ? { kind: "ok", text: `${pod}: network updated.` }
          : { kind: "err", text: r.error ?? r.output ?? "Failed." },
      );
      await refresh();
    } finally {
      setBusy("");
    }
  }

  return (
    <>
      <h1 className="page-title">Network</h1>
      <p style={{ color: "var(--muted)", margin: 0 }}>
        Each pod's tailnet identity and how it's exposed. Changes re-render the
        pod's scripts and restart it.
      </p>

      {msg && (
        <div style={{ marginTop: "var(--sp-5)" }}>
          <Alert kind={msg.kind}>{msg.text}</Alert>
        </div>
      )}

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
                  {e.tailscale ? (
                    <>
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
                    </>
                  ) : Object.keys(e.ports).length ? (
                    `published ports: ${Object.entries(e.ports)
                      .map(([h, c]) => `${h}→${c}`)
                      .join(", ")}`
                  ) : (
                    "no tailnet identity, no published ports"
                  )}
                </div>
              </div>
              <div className="spacer" />
              {e.tailscale && (
                <span className="chip chip--installed">tailscale</span>
              )}
              {e.https ? (
                <span className="chip chip--installed">https</span>
              ) : (
                <span className="chip">http</span>
              )}
              {e.controller ? (
                <span className="preview-label">controller — managed by bootstrap</span>
              ) : (
                <>
                  {e.tailscale ? (
                    <>
                      <button
                        className={
                          "btn btn--ghost btn--sm" +
                          (busy === `${e.name}:https` ? " btn--loading" : "")
                        }
                        disabled={!!busy}
                        title={
                          e.https
                            ? "Stop terminating HTTPS via tailscale serve"
                            : "Terminate HTTPS on 443 via tailscale serve"
                        }
                        onClick={() => apply(e.name, "https", { https: !e.https })}
                      >
                        {busy === `${e.name}:https` && <SpinnerIcon className="btn-icon" />}
                        {e.https ? "Disable HTTPS" : "Enable HTTPS"}
                      </button>
                      <button
                        className={
                          "btn btn--danger btn--sm" +
                          (busy === `${e.name}:ts` ? " btn--loading" : "")
                        }
                        disabled={!!busy}
                        title="Remove this pod's tailnet identity and publish its ports locally instead"
                        onClick={() => setConfirmTs(e.name)}
                      >
                        {busy === `${e.name}:ts` && <SpinnerIcon className="btn-icon" />}
                        Remove TS
                      </button>
                    </>
                  ) : (
                    <button
                      className={
                        "btn btn--secondary btn--sm" +
                        (busy === `${e.name}:ts` ? " btn--loading" : "")
                      }
                      disabled={!!busy}
                      title="Give this pod its own tailnet identity (uses its existing Tailscale state or key file)"
                      onClick={() => apply(e.name, "ts", { tailscale: true, https: true })}
                    >
                      {busy === `${e.name}:ts` && <SpinnerIcon className="btn-icon" />}
                      Enable TS
                    </button>
                  )}
                </>
              )}
            </div>
          ))}
        </div>
      )}

      {confirmTs && (
        <ConfirmDialog
          title={`Remove ${confirmTs}'s tailnet identity?`}
          confirmLabel="Remove TS"
          busy={busy === `${confirmTs}:ts`}
          onConfirm={async () => {
            const pod = confirmTs;
            await apply(pod, "ts", { tailscale: false, https: false });
            setConfirmTs(null);
          }}
          onCancel={() => setConfirmTs(null)}
        >
          The pod stops being a device on your tailnet — its MagicDNS name and
          HTTPS certificate stop working, and its ports get published on the
          host instead. Its Tailscale state is kept, so re-enabling restores
          the same identity.
        </ConfirmDialog>
      )}
    </>
  );
}
