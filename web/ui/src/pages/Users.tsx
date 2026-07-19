import { useCallback, useEffect, useState } from "react";
import type { UsersStatus } from "../types";
import { api } from "../api";
import { Alert } from "../components/Alert";
import { ChipPicker } from "../components/ChipPicker";
import { FlashView, useFlash } from "../components/Flash";
import { Field } from "../components/Form";
import { SpinnerIcon } from "../components/Icons";
import { TsApiWizard } from "../components/TsApiWizard";

function ago(iso: string): string {
  if (!iso) return "";
  const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 2) return "online";
  if (mins < 60) return `${mins}m ago`;
  if (mins < 48 * 60) return `${Math.floor(mins / 60)}h ago`;
  return `${Math.floor(mins / 1440)}d ago`;
}

// User machines: devices enrolled with a tailarr-user auth key. Each machine
// can reach exactly the services it holds a capability badge for — checkboxes
// here flip tag:tailarr-can-<svc> on the device via the Tailscale API. No
// policy-file changes, effective in seconds. See docs/acl-design.md.
export function Users() {
  const [status, setStatus] = useState<UsersStatus | null>(null);
  const [busyKey, setBusyKey] = useState(""); // "<id>:<svc>" or "<id>:nick"
  const [nickEdit, setNickEdit] = useState<{ id: string; value: string } | null>(null);
  const [minting, setMinting] = useState(false);
  const [mintedKey, setMintedKey] = useState("");
  const [adoptOpen, setAdoptOpen] = useState(false);
  const [adoptId, setAdoptId] = useState("");
  const [adopting, setAdopting] = useState(false);
  const { flash, show, clear } = useFlash();

  async function mintKey() {
    setMinting(true);
    setMintedKey("");
    try {
      const r = await api.userKey();
      if (r.ok && r.key) setMintedKey(r.key);
      else show({ kind: "err", text: r.error ?? "Couldn't mint a key." });
    } catch (e) {
      show({ kind: "err", text: String(e) });
    } finally {
      setMinting(false);
    }
  }

  async function adopt() {
    const id = adoptId.trim();
    if (!id || adopting) return;
    setAdopting(true);
    try {
      const r = await api.userAdopt(id);
      if (r.ok) {
        show({
          kind: "ok",
          text: `${r.hostname || id} is now a user machine — grant it services below.`,
        });
        setAdoptOpen(false);
        setAdoptId("");
        await refresh();
      } else {
        show({ kind: "err", text: r.error ?? "Couldn't adopt the machine." });
      }
    } catch (e) {
      show({ kind: "err", text: String(e) });
    } finally {
      setAdopting(false);
    }
  }

  const refresh = useCallback(async () => {
    try {
      setStatus(await api.users());
    } catch (e) {
      show({ kind: "err", text: String(e) });
    }
  }, [show]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 15000);
    return () => clearInterval(t);
  }, [refresh]);

  async function toggle(id: string, service: string, allow: boolean) {
    // "server" is the controller itself — one plain-words warning.
    if (
      service === "server" &&
      allow &&
      !window.confirm("Adding this gives full admin rights to this device.")
    )
      return;
    setBusyKey(`${id}:${service}`);
    try {
      const r = await api.userAccess(id, service, allow);
      if (!r.ok) show({ kind: "err", text: r.error ?? "Failed to update access." });
      await refresh();
    } catch (e) {
      show({ kind: "err", text: String(e) });
    } finally {
      setBusyKey("");
    }
  }

  async function saveNick(id: string, nickname: string) {
    setNickEdit(null);
    setBusyKey(`${id}:nick`);
    try {
      await api.userNick(id, nickname);
      await refresh();
    } finally {
      setBusyKey("");
    }
  }

  return (
    <>
      <h1 className="page-title">Users</h1>
      <p style={{ color: "var(--muted)", margin: 0 }}>
        Machines enrolled with a Tailarr user key. A machine reaches only the
        services checked here — changes apply in seconds, no restarts.
      </p>

      <FlashView flash={flash} onClose={clear} />

      {status === null ? (
        <p style={{ color: "var(--muted)", marginTop: "var(--sp-5)" }}>Loading…</p>
      ) : !status.configured ? (
        <div style={{ marginTop: "var(--sp-5)", maxWidth: 640 }}>
          <Alert kind="info">
            Managing users needs a Tailscale API credential on the
            controller — set one up below (it also unlocks automatic auth-key
            minting for deploys).
          </Alert>
          <TsApiWizard onDone={refresh} />
        </div>
      ) : status.error ? (
        <div style={{ marginTop: "var(--sp-5)" }}>
          <Alert kind="err">{status.error}</Alert>
        </div>
      ) : (
        <>
          <div className="section-head">
            <div className="section-title">Machines</div>
            <div style={{ display: "flex", gap: "var(--sp-2)" }}>
              <button
                className="btn btn--ghost btn--sm"
                title="Tag an already-enrolled tailnet device as a user machine"
                onClick={() => setAdoptOpen(true)}
              >
                Adopt by ID
              </button>
              <button
                className={"btn btn--primary btn--sm" + (minting ? " btn--loading" : "")}
                disabled={minting}
                title="Mint a single-use enrollment key for a new user machine"
                onClick={mintKey}
              >
                + Add user
              </button>
            </div>
          </div>

          {adoptOpen && (
            <div className="scrim" onClick={adopting ? undefined : () => setAdoptOpen(false)}>
              <div className="modal" onClick={(e) => e.stopPropagation()}>
                <div className="modal__head">
                  <span className="modal__title">Adopt an existing machine</span>
                  <div className="spacer" />
                  <button
                    className="btn btn--ghost btn--sm"
                    disabled={adopting}
                    onClick={() => setAdoptOpen(false)}
                  >
                    Close
                  </button>
                </div>
                <p className="field__hint" style={{ margin: "0 0 var(--sp-3)" }}>
                  For devices already on the tailnet (e.g. an Apple TV that
                  signed in with an Apple ID). Paste its node ID from the
                  Tailscale admin console (machine page URL, or “Copy node ID”).
                  Tagging replaces the device’s login ownership — it becomes a
                  Tailarr user machine with zero access until you grant
                  services.
                </p>
                <Field label="Node ID">
                  <input
                    className="input"
                    autoFocus
                    value={adoptId}
                    placeholder="e.g. npaiBkAAg111CNTRL"
                    onChange={(e) => setAdoptId(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") adopt();
                      if (e.key === "Escape") setAdoptOpen(false);
                    }}
                  />
                </Field>
                <div className="preview-row">
                  <button
                    className={"btn btn--primary" + (adopting ? " btn--loading" : "")}
                    disabled={adopting || !adoptId.trim()}
                    onClick={adopt}
                  >
                    {adopting && <SpinnerIcon className="btn-icon" />}
                    Adopt
                  </button>
                </div>
              </div>
            </div>
          )}

          {mintedKey && (
            <div className="card" style={{ padding: "var(--sp-4)", marginTop: "var(--sp-3)" }}>
              <div className="row__title">Enrollment key (single-use, expires in 24h)</div>
              <div
                className="log__body"
                style={{ margin: "var(--sp-2) 0", userSelect: "all", cursor: "copy" }}
                title="Click, then copy"
              >
                {mintedKey}
              </div>
              <p className="field__hint" style={{ margin: 0 }}>
                On the new machine: install Tailscale and log in with this auth
                key (e.g. <code>tailscale up --auth-key=…</code>). It appears
                here with zero access — grant services with the checkboxes.
                This key is shown once; mint another if you lose it.
              </p>
              <div className="preview-row" style={{ marginTop: "var(--sp-3)" }}>
                <button className="btn btn--ghost btn--sm" onClick={() => setMintedKey("")}>
                  Dismiss
                </button>
              </div>
            </div>
          )}
          {status.users.length === 0 && (
            <div className="empty" style={{ marginTop: "var(--sp-4)" }}>
              <div className="empty__title">No user machines yet</div>
              <p style={{ margin: 0 }}>
                “+ Add user” mints an enrollment key. Devices that log in with
                it appear here with zero access until you grant services.
              </p>
            </div>
          )}
          <div className="row-list">
            {status.users.map((u) => (
              <div key={u.id} className="row card" style={{ flexWrap: "wrap" }}>
                <div style={{ minWidth: 180 }}>
                  {nickEdit?.id === u.id ? (
                    <input
                      className="input"
                      autoFocus
                      value={nickEdit.value}
                      placeholder={u.hostname}
                      onChange={(e) => setNickEdit({ id: u.id, value: e.target.value })}
                      onBlur={() => saveNick(u.id, nickEdit.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") saveNick(u.id, nickEdit.value);
                        if (e.key === "Escape") setNickEdit(null);
                      }}
                    />
                  ) : (
                    <div
                      className="row__title"
                      title="Click to set a nickname"
                      style={{ cursor: "pointer" }}
                      onClick={() => setNickEdit({ id: u.id, value: u.nickname })}
                    >
                      {u.nickname || u.hostname}
                    </div>
                  )}
                  <div className="row__meta">
                    {u.nickname && `${u.hostname} · `}
                    {u.os} · {ago(u.last_seen)}
                    {u.ip && ` · ${u.ip}`}
                  </div>
                </div>
                <div className="spacer" />
                <ChipPicker
                  chips={u.can}
                  options={status.services.map((s) => ({ id: s }))}
                  onAdd={(svc) => toggle(u.id, svc, true)}
                  onRemove={(svc) => toggle(u.id, svc, false)}
                  addLabel="+ Add service"
                  emptyHint="no services deployed"
                  busyId={busyKey.startsWith(`${u.id}:`) ? busyKey.slice(u.id.length + 1) : ""}
                  disabled={!!busyKey}
                />
              </div>
            ))}
          </div>
        </>
      )}
    </>
  );
}
