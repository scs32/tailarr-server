import { useEffect, useState } from "react";
import type { RelayAction, RelayDevice, RelayStatus } from "../types";
import { api } from "../api";
import { Alert } from "./Alert";
import { Field } from "./Form";
import { SpinnerIcon } from "./Icons";

// Peer relay — the top section of the Network page (v0.15.0, generalized
// from the apple/container-only Settings card). Tailarr AUTHORS the policy
// grant; a device only becomes a usable relay once it locally runs
// `tailscale set --relay-server-port=...` — there is no remote enablement
// (tailscale/tailscale#17791). So the picker lists Tailarr's own registry
// of relays, and "adding" one hands the user the command to run on the
// device and waits for proof of traffic before marking it active.

function VerifiedChip({ status }: { status: RelayStatus }) {
  const v = status.verified.state;
  if (v === "peer-relay")
    return (
      <span className="chip chip--installed" title={status.verified.detail}>
        relaying via your device
      </span>
    );
  if (v === "direct")
    return <span className="chip chip--installed">connections direct</span>;
  if (v === "derp")
    return (
      <span className="chip chip--busy" title={status.verified.detail}>
        using DERP (slow)
      </span>
    );
  return <span className="chip">not checked yet</span>;
}

function AddRelayDialog({
  status,
  busy,
  onAdd,
  onClose,
}: {
  status: RelayStatus;
  busy: boolean;
  onAdd: (a: RelayAction) => Promise<string | null>;
  onClose: () => void;
}) {
  const [devices, setDevices] = useState<RelayDevice[] | null>(null);
  const [devErr, setDevErr] = useState<string | null>(null);
  const [addErr, setAddErr] = useState<string | null>(null);
  const [picked, setPicked] = useState<RelayDevice | null>(null);
  const [manualIp, setManualIp] = useState("");
  const [manualName, setManualName] = useState("");

  useEffect(() => {
    api
      .relayDevices()
      .then((r) => {
        if (r.ok) setDevices(r.devices);
        else {
          setDevices([]);
          setDevErr(r.error);
        }
      })
      .catch((e) => {
        setDevices([]);
        setDevErr(String(e));
      });
  }, []);

  const known = new Set(status.relays.map((r) => r.ip));
  const candidates = (devices ?? []).filter((d) => !known.has(d.ip));
  const ip = picked ? picked.ip : manualIp.trim();
  const name = picked
    ? picked.hostname || picked.name
    : manualName.trim() || manualIp.trim();
  // Identify the relay by device pick or typed IP. Capability is ALWAYS
  // enabled on the device itself (no remote path exists) — the entry stays
  // pending until traffic proves it.
  const ready = !!ip;
  const action: RelayAction = { do: "add-relay", ip, name };

  return (
    <div className="scrim" onClick={busy ? undefined : onClose}>
      <div className="dialog card" onClick={(e) => e.stopPropagation()}>
        <h3 className="dialog__title">Add a relay device</h3>
        <div className="dialog__body">
          <p className="field__hint" style={{ margin: "0 0 var(--sp-3)" }}>
            Pick the device that should carry pod traffic. Tailarr updates
            the tailnet policy for you; the device itself must run one
            command (below) before it can actually relay.
          </p>
          {devices === null ? (
            <p style={{ color: "var(--muted)" }}>Loading tailnet devices…</p>
          ) : (
            <>
              {devErr && <Alert kind="err">{devErr}</Alert>}
              {candidates.length > 0 && (
                <div className="row-list" style={{ marginBottom: "var(--sp-4)" }}>
                  {candidates.map((d) => (
                    <label
                      key={d.ip}
                      className="row card"
                      style={{ cursor: "pointer" }}
                    >
                      <input
                        type="radio"
                        name="relay-device"
                        checked={picked?.ip === d.ip}
                        onChange={() => setPicked(d)}
                      />
                      <div>
                        <div className="row__title">
                          {d.hostname || d.name}
                        </div>
                        <div className="row__meta">
                          {d.ip}
                          {d.os && ` · ${d.os}`}
                          {d.user && ` · ${d.user}`}
                        </div>
                      </div>
                    </label>
                  ))}
                </div>
              )}
              <Field
                label="Or enter a tailnet IP directly"
                hint="The device's 100.x address (Tailscale admin console → Machines)."
              >
                <input
                  className="input"
                  value={manualIp}
                  placeholder="100.64.0.1"
                  onChange={(e) => {
                    setManualIp(e.target.value);
                    setPicked(null);
                  }}
                />
              </Field>
              {!picked && manualIp.trim() && (
                <Field label="Name">
                  <input
                    className="input"
                    value={manualName}
                    placeholder="office-mac"
                    onChange={(e) => setManualName(e.target.value)}
                  />
                </Field>
              )}
              <p className="field__hint" style={{ margin: "var(--sp-2) 0 0" }}>
                A device must run{" "}
                <code>{status.command}</code> once before it can relay. If
                it already has (install-mac.sh does this for your Mac), it
                shows as <strong>ready</strong> right away — otherwise it
                shows the command until it does.
              </p>
            </>
          )}
          {addErr && (
            <div style={{ marginTop: "var(--sp-3)" }}>
              <Alert kind="err">{addErr}</Alert>
            </div>
          )}
        </div>
        <div className="dialog__foot">
          {!ready && !busy && (
            <span className="field__hint" style={{ marginRight: "auto" }}>
              Pick a device or enter an IP.
            </span>
          )}
          <button className="btn btn--ghost" disabled={busy} onClick={onClose}>
            Cancel
          </button>
          <button
            className={"btn btn--primary" + (busy ? " btn--loading" : "")}
            disabled={busy || !ready}
            onClick={async () => {
              setAddErr(null);
              setAddErr(await onAdd(action));
            }}
          >
            {busy && <SpinnerIcon className="btn-icon" />}
            Add relay
          </button>
        </div>
      </div>
    </div>
  );
}

export function RelaySection({
  status,
  busy,
  onAct,
}: {
  status: RelayStatus | null;
  busy: boolean;
  onAct: (a: RelayAction) => Promise<string | null>; // error text, null = ok
}) {
  const [showAdd, setShowAdd] = useState(false);

  if (status === null) return null;
  const on = status.grant_active;

  return (
    <>
      <div className="section-title">Peer relay</div>
      <div className="card panel" style={{ marginBottom: "var(--sp-8)" }}>
        <div className="preview-row">
          <p style={{ color: "var(--muted)", margin: 0, flex: 1, minWidth: 260 }}>
            When pods can't connect directly, traffic falls back to
            Tailscale's shared DERP servers — slow. A peer relay routes that
            traffic through one of <em>your</em> devices instead. This speeds
            up access from your tailnet devices only — pods made public
            always serve internet visitors through Tailscale Funnel's own
            infrastructure, unaffected by relays.
          </p>
          <VerifiedChip status={status} />
          <button
            className="btn btn--ghost btn--sm"
            disabled={busy}
            onClick={() => onAct({ do: "recheck" })}
          >
            Re-check
          </button>
          {on ? (
            <button
              className="btn btn--ghost btn--sm"
              disabled={busy}
              onClick={() => onAct({ do: "disable" })}
            >
              Turn off
            </button>
          ) : (
            <button
              className="btn btn--sm"
              disabled={busy}
              onClick={() => onAct({ do: "enable" })}
            >
              Turn on
            </button>
          )}
        </div>

        {!on && status.recommended && (
          <div style={{ marginTop: "var(--sp-4)" }}>
            <Alert kind="info">
              This install runs behind apple/container's NAT, so pod traffic
              almost certainly uses DERP — a peer relay on your Mac fixes
              that.
              {status.reasons.length > 0 && (
                <ul style={{ margin: "var(--sp-2) 0 0", paddingLeft: "1.2em" }}>
                  {status.reasons.map((r) => (
                    <li key={r}>{r}</li>
                  ))}
                </ul>
              )}
            </Alert>
          </div>
        )}

        {on && (
          <div style={{ marginTop: "var(--sp-6)" }}>
            <div className="preview-row" style={{ marginBottom: "var(--sp-4)" }}>
              <span className="preview-label">Relay devices</span>
              <div className="spacer" />
              <button
                className={
                  "btn btn--sm " +
                  (status.mode === "global" ? "btn--secondary" : "btn--ghost")
                }
                disabled={busy}
                title="Every service relays through the same device"
                onClick={() => onAct({ do: "mode", mode: "global" })}
              >
                One relay for everything
              </button>
              <button
                className={
                  "btn btn--sm " +
                  (status.mode === "per-pod" ? "btn--secondary" : "btn--ghost")
                }
                disabled={busy}
                title="Pick a relay (or none) per service, in the list below"
                onClick={() => onAct({ do: "mode", mode: "per-pod" })}
              >
                Choose per pod
              </button>
            </div>

            {status.relays.length === 0 ? (
              <p className="field__hint" style={{ margin: "0 0 var(--sp-3)" }}>
                No relay devices yet. Without one, "any admin device" on the
                tailnet may relay once it runs the enable command.
              </p>
            ) : (
              <div className="row-list" style={{ marginBottom: "var(--sp-4)" }}>
                {status.relays.map((r) => (
                  <div key={r.id} className="row card">
                    <div style={{ minWidth: 140 }}>
                      <div className="row__title">{r.name}</div>
                      <div className="row__meta">{r.ip}</div>
                    </div>
                    <div className="spacer" />
                    {r.status === "active" ? (
                      <span
                        className="chip chip--installed"
                        title={
                          r.discovered
                            ? "Seen carrying relay traffic"
                            : "Verified"
                        }
                      >
                        active
                      </span>
                    ) : r.status === "ready" ? (
                      <span
                        className="chip chip--installed"
                        title="This device advertises relay capability — it engages automatically when a connection needs it"
                      >
                        ready
                      </span>
                    ) : (
                      <span
                        className="chip chip--busy"
                        title="This device isn't relay-capable yet — run the command on it once"
                      >
                        not enabled — run: {status.command}
                      </span>
                    )}
                    <button
                      className="btn btn--ghost btn--sm"
                      disabled={busy}
                      onClick={() => onAct({ do: "remove-relay", id: r.id })}
                    >
                      Remove
                    </button>
                  </div>
                ))}
              </div>
            )}

            <div className="preview-row">
              <button
                className="btn btn--ghost btn--sm"
                disabled={busy}
                onClick={() => setShowAdd(true)}
              >
                + Add relay device…
              </button>
              {status.mode === "global" && (
                <>
                  <span className="preview-label">Relay through</span>
                  <select
                    className="select"
                    style={{ width: "auto" }}
                    disabled={busy}
                    value={status.global_relay}
                    onChange={(e) =>
                      onAct({ do: "set-global", id: e.target.value })
                    }
                  >
                    <option value="">Automatic — any admin device</option>
                    {status.relays.map((r) => (
                      <option key={r.id} value={r.id}>
                        {r.name} ({r.ip})
                      </option>
                    ))}
                  </select>
                </>
              )}
              {status.mode === "per-pod" && (
                <span className="field__hint">
                  Pick each pod's relay in the list below.
                </span>
              )}
            </div>
          </div>
        )}
      </div>

      {showAdd && (
        <AddRelayDialog
          status={status}
          busy={busy}
          onAdd={async (a) => {
            const err = await onAct(a);
            if (!err) setShowAdd(false);
            return err;
          }}
          onClose={() => setShowAdd(false)}
        />
      )}
    </>
  );
}
