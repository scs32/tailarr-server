import { useCallback, useEffect, useState } from "react";
import type { UserMachine, UsersStatus } from "../types";
import { api } from "../api";
import { Alert } from "../components/Alert";
import { ChipPicker } from "../components/ChipPicker";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { FlashView, useFlash } from "../components/Flash";
import { Field } from "../components/Form";
import { SpinnerIcon } from "../components/Icons";
import { TsApiWizard } from "../components/TsApiWizard";
import { QRCodeSVG } from "qrcode.react";
import { inviteLink } from "../lib/invite";

function ago(iso: string): string {
  if (!iso) return "";
  const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 2) return "online";
  if (mins < 60) return `${mins}m ago`;
  if (mins < 48 * 60) return `${Math.floor(mins / 60)}h ago`;
  return `${Math.floor(mins / 1440)}d ago`;
}

// One row of the "Manage devices" list: rename (a local nickname stored
// on the controller) or revoke (delete the device from the tailnet — the
// lost/stolen action). Local input state seeds from the saved nickname.
function ManagedDevice({
  device,
  meta,
  busy,
  onSave,
  onRevoke,
}: {
  device: UserMachine;
  meta: string;
  busy: boolean;
  onSave: (node: string, nickname: string) => void;
  onRevoke: (node: string, label: string) => void;
}) {
  const [nick, setNick] = useState(device.nickname);
  const dirty = nick.trim() !== device.nickname;
  return (
    <div className="row card" style={{ flexWrap: "wrap", gap: "var(--sp-2)" }}>
      <div style={{ minWidth: 160 }}>
        <div className="row__title">{device.hostname}</div>
        <div className="row__meta">{meta}</div>
      </div>
      <div className="spacer" />
      <input
        className="input"
        style={{ width: 150 }}
        value={nick}
        placeholder="nickname"
        aria-label={`Nickname for ${device.hostname}`}
        disabled={busy}
        onChange={(e) => setNick(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && dirty) onSave(device.id, nick.trim());
        }}
      />
      <button
        className="btn btn--ghost btn--sm"
        disabled={busy || !dirty}
        onClick={() => onSave(device.id, nick.trim())}
      >
        Save name
      </button>
      <button
        className="btn btn--ghost btn--sm"
        disabled={busy}
        onClick={() => onRevoke(device.id, device.nickname || device.hostname)}
      >
        Revoke
      </button>
    </div>
  );
}

// Users are PEOPLE: adding one mints an enrollment key carrying their
// identity tag (tag:tailarr-u-<id>) plus their current badges, so any
// device that logs in with it belongs to them — and inherits their
// access — automatically. Reissue = same person, new device. Badges are
// per-user: a switch applies to all their devices (a reconcile pass
// covers late-enrolling ones). Machines enrolled with old anonymous keys
// appear under "Unassigned" until attached to a person.
export function Users() {
  const [status, setStatus] = useState<UsersStatus | null>(null);
  const [busyKey, setBusyKey] = useState("");
  const [addOpen, setAddOpen] = useState(false);
  const [addName, setAddName] = useState("");
  const [addBusy, setAddBusy] = useState(false);
  const [key, setKey] = useState<{ who: string; key: string } | null>(null);
  const [renaming, setRenaming] = useState<{ id: string; value: string } | null>(null);
  const [deleting, setDeleting] = useState<{ id: string; name: string } | null>(null);
  const [adoptOpen, setAdoptOpen] = useState(false);
  const [adoptId, setAdoptId] = useState("");
  const [adopting, setAdopting] = useState(false);
  const [managing, setManaging] = useState(""); // person id whose devices are open
  const { flash, show, clear } = useFlash();

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

  async function addPerson() {
    const name = addName.trim();
    if (!name || addBusy) return;
    setAddBusy(true);
    try {
      const r = await api.person({ do: "add", name });
      if (!r.ok) {
        show({ kind: "err", text: r.error ?? "Couldn't add the user." });
      } else {
        setAddOpen(false);
        setAddName("");
        if (r.key) setKey({ who: name, key: r.key });
        else if (r.error) show({ kind: "err", text: r.error });
        await refresh();
      }
    } finally {
      setAddBusy(false);
    }
  }

  async function reissue(id: string, name: string) {
    setBusyKey(`${id}:reissue`);
    try {
      const r = await api.person({ do: "reissue", id });
      if (r.ok && r.key) setKey({ who: name, key: r.key });
      else show({ kind: "err", text: r.error ?? "Couldn't create a key." });
    } finally {
      setBusyKey("");
    }
  }

  async function rename(id: string, name: string) {
    setRenaming(null);
    if (!name.trim()) return;
    await api.person({ do: "rename", id, name: name.trim() });
    await refresh();
  }

  async function removePerson() {
    if (!deleting) return;
    const { id, name } = deleting;
    setDeleting(null);
    setBusyKey(`${id}:delete`);
    try {
      const r = await api.person({ do: "delete", id });
      show(
        r.ok
          ? {
              kind: "ok",
              text: `Removed ${name}. Their devices stay connected but lost all access.`,
            }
          : { kind: "err", text: r.error ?? "Delete failed." },
      );
      await refresh();
    } finally {
      setBusyKey("");
    }
  }

  async function togglePerson(id: string, service: string, allow: boolean) {
    if (
      service === "server" &&
      allow &&
      !window.confirm(
        "Adding this gives full admin rights to ALL of this user's devices.",
      )
    )
      return;
    setBusyKey(`${id}:${service}`);
    try {
      const r = await api.personAccess(id, service, allow);
      if (!r.ok) show({ kind: "err", text: r.error ?? "Failed to update access." });
      else if (r.error) show({ kind: "err", text: `Some devices lagged: ${r.error}` });
      await refresh();
    } finally {
      setBusyKey("");
    }
  }

  async function setBasic(id: string, basic: boolean) {
    setBusyKey(`${id}:basic`);
    try {
      const r = await api.person({ do: "basic", id, basic });
      if (!r.ok) show({ kind: "err", text: r.error ?? "Failed to update." });
      await refresh();
    } finally {
      setBusyKey("");
    }
  }

  async function toggleDevice(id: string, service: string, allow: boolean) {
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
    } finally {
      setBusyKey("");
    }
  }

  async function assign(node: string, uid: string) {
    setBusyKey(`${node}:assign`);
    try {
      const r = await api.person({ do: "assign", id: uid, node });
      if (!r.ok) show({ kind: "err", text: r.error ?? "Assign failed." });
      await refresh();
    } finally {
      setBusyKey("");
    }
  }

  async function saveNick(node: string, nickname: string) {
    setBusyKey(`${node}:nick`);
    try {
      const r = await api.userNick(node, nickname);
      if (!r.ok) show({ kind: "err", text: r.error ?? "Couldn't save the name." });
      else show({ kind: "ok", text: nickname ? "Name saved." : "Name cleared." });
      await refresh();
    } finally {
      setBusyKey("");
    }
  }

  async function revokeDevice(node: string, label: string) {
    if (
      !window.confirm(
        `Revoke ${label}? It's disconnected and loses all access ` +
          "immediately. It can only rejoin with a new enrollment key.",
      )
    )
      return;
    setBusyKey(`${node}:revoke`);
    try {
      const r = await api.userRevoke(node);
      if (!r.ok) show({ kind: "err", text: r.error ?? "Couldn't revoke the device." });
      else show({ kind: "ok", text: `Revoked ${label}.` });
      await refresh();
    } finally {
      setBusyKey("");
    }
  }

  async function copyText(text: string, label: string) {
    try {
      await navigator.clipboard.writeText(text);
      show({ kind: "ok", text: `${label} copied.` });
    } catch {
      show({ kind: "err", text: "Couldn't copy — select the text and copy it manually." });
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
          text: `${r.hostname || id} is now a user machine — assign it to a user below.`,
        });
        setAdoptOpen(false);
        setAdoptId("");
        await refresh();
      } else {
        show({ kind: "err", text: r.error ?? "Couldn't adopt the machine." });
      }
    } finally {
      setAdopting(false);
    }
  }

  const deviceMeta = (u: UserMachine) =>
    [u.os, ago(u.last_seen), u.ip].filter(Boolean).join(" · ");

  return (
    <>
      <h1 className="page-title">Users</h1>
      <p style={{ color: "var(--muted)", margin: 0 }}>
        Each user’s devices enroll with their personal key and inherit the
        services granted here — changes apply in seconds, no restarts.
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
      ) : (
        <>
          {status.error && <Alert kind="err">{status.error}</Alert>}
          <div className="section-head">
            <div className="section-title">Users</div>
            <div style={{ display: "flex", gap: "var(--sp-2)" }}>
              <button
                className="btn btn--ghost btn--sm"
                title="Add a device that's already on your network as a user's device"
                onClick={() => setAdoptOpen(true)}
              >
                Adopt by ID
              </button>
              <button
                className="btn btn--primary btn--sm"
                onClick={() => setAddOpen(true)}
              >
                + Add user
              </button>
            </div>
          </div>

          {status.people.length === 0 && (
            <div className="empty" style={{ marginTop: "var(--sp-4)" }}>
              <div className="empty__title">No users yet</div>
              <p style={{ margin: 0 }}>
                “+ Add user” creates a person and creates their enrollment key.
                Every device that logs in with it is theirs automatically.
              </p>
            </div>
          )}

          <div className="row-list">
            {status.people.map((p) => (
              <div key={p.id} className="card" style={{ padding: "var(--sp-4)" }}>
                <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: "var(--sp-2)" }}>
                  {renaming?.id === p.id ? (
                    <input
                      className="input"
                      autoFocus
                      value={renaming.value}
                      onChange={(e) => setRenaming({ id: p.id, value: e.target.value })}
                      onBlur={() => rename(p.id, renaming.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") rename(p.id, renaming.value);
                        if (e.key === "Escape") setRenaming(null);
                      }}
                    />
                  ) : (
                    <div
                      className="row__title"
                      title="Click to rename"
                      style={{ cursor: "pointer", fontSize: "var(--fs-md)" }}
                      onClick={() => setRenaming({ id: p.id, value: p.name })}
                    >
                      {p.name}
                    </div>
                  )}
                  <span className="chip">
                    {p.devices.length} device{p.devices.length === 1 ? "" : "s"}
                  </span>
                  <div className="spacer" />
                  <button
                    className={
                      "btn btn--sm " +
                      (p.basic ? "btn--primary" : "btn--ghost") +
                      (busyKey === `${p.id}:basic` ? " btn--loading" : "")
                    }
                    disabled={!!busyKey}
                    title={
                      p.basic
                        ? "Basic mode is on: this user's app hides settings and opens straight into their main app. Click for the full experience."
                        : "Basic mode: give this user a stripped, single-purpose app (no settings, no drawer) that opens into their main app."
                    }
                    onClick={() => setBasic(p.id, !p.basic)}
                  >
                    {busyKey === `${p.id}:basic` && (
                      <SpinnerIcon className="btn-icon" />
                    )}
                    {p.basic ? "Basic ✓" : "Basic"}
                  </button>
                  <button
                    className={
                      "btn btn--ghost btn--sm" +
                      (busyKey === `${p.id}:reissue` ? " btn--loading" : "")
                    }
                    disabled={!!busyKey}
                    title="Create a fresh enrollment key for this user's next device"
                    onClick={() => reissue(p.id, p.name)}
                  >
                    {busyKey === `${p.id}:reissue` && <SpinnerIcon className="btn-icon" />}
                    Reissue key
                  </button>
                  {p.devices.length > 0 && (
                    <button
                      className="btn btn--ghost btn--sm"
                      disabled={!!busyKey}
                      title="Rename or revoke this user's devices"
                      onClick={() =>
                        setManaging(managing === p.id ? "" : p.id)
                      }
                    >
                      {managing === p.id ? "Done" : "Manage devices"}
                    </button>
                  )}
                  <button
                    className="btn btn--ghost btn--sm"
                    disabled={!!busyKey}
                    onClick={() => setDeleting({ id: p.id, name: p.name })}
                  >
                    Remove
                  </button>
                </div>
                <div style={{ marginTop: "var(--sp-3)" }}>
                  <ChipPicker
                    chips={p.badges}
                    options={status.services.map((s) => ({ id: s }))}
                    onAdd={(svc) => togglePerson(p.id, svc, true)}
                    onRemove={(svc) => togglePerson(p.id, svc, false)}
                    addLabel="+ Grant service"
                    emptyHint="no services deployed"
                    busyId={busyKey.startsWith(`${p.id}:`) ? busyKey.slice(p.id.length + 1) : ""}
                    disabled={!!busyKey}
                  />
                </div>
                {p.devices.length > 0 &&
                  (managing === p.id ? (
                    <div className="row-list" style={{ marginTop: "var(--sp-3)" }}>
                      {p.devices.map((u) => (
                        <ManagedDevice
                          key={u.id}
                          device={u}
                          meta={deviceMeta(u)}
                          busy={!!busyKey}
                          onSave={saveNick}
                          onRevoke={revokeDevice}
                        />
                      ))}
                    </div>
                  ) : (
                    <div style={{ marginTop: "var(--sp-3)" }}>
                      {p.devices.map((u) => (
                        <div key={u.id} className="row__meta">
                          <strong>{u.nickname || u.hostname}</strong>
                          {deviceMeta(u) && ` · ${deviceMeta(u)}`}
                        </div>
                      ))}
                    </div>
                  ))}
              </div>
            ))}
          </div>

          {status.users.length > 0 && (
            <>
              <div className="section-title" style={{ marginTop: "var(--sp-6)" }}>
                Unassigned machines
              </div>
              <p className="field__hint" style={{ marginTop: 0 }}>
                Enrolled with an anonymous key or adopted by ID. Attach each
                to a user (it inherits their access) — or keep granting
                per-device below.
              </p>
              <div className="row-list">
                {status.users.map((u) => (
                  <div key={u.id} className="row card" style={{ flexWrap: "wrap" }}>
                    <div style={{ minWidth: 180 }}>
                      <div className="row__title">{u.nickname || u.hostname}</div>
                      <div className="row__meta">{deviceMeta(u)}</div>
                    </div>
                    <div className="spacer" />
                    {status.people.length > 0 && (
                      <select
                        className="input"
                        style={{ width: "auto" }}
                        disabled={!!busyKey}
                        value=""
                        onChange={(e) => e.target.value && assign(u.id, e.target.value)}
                      >
                        <option value="">Assign to user…</option>
                        {status.people.map((p) => (
                          <option key={p.id} value={p.id}>
                            {p.name}
                          </option>
                        ))}
                      </select>
                    )}
                    <ChipPicker
                      chips={u.can}
                      options={status.services.map((s) => ({ id: s }))}
                      onAdd={(svc) => toggleDevice(u.id, svc, true)}
                      onRemove={(svc) => toggleDevice(u.id, svc, false)}
                      addLabel="+ Add service"
                      emptyHint="no services deployed"
                      busyId={busyKey.startsWith(`${u.id}:`) ? busyKey.slice(u.id.length + 1) : ""}
                      disabled={!!busyKey}
                    />
                    <button
                      className="btn btn--ghost btn--sm"
                      disabled={!!busyKey}
                      title="Disconnect this device"
                      onClick={() =>
                        revokeDevice(u.id, u.nickname || u.hostname)
                      }
                    >
                      Revoke
                    </button>
                  </div>
                ))}
              </div>
            </>
          )}

          {key && (
            <div className="scrim" onClick={() => setKey(null)}>
              <div
                className="modal modal--narrow"
                onClick={(e) => e.stopPropagation()}
              >
                <div className="modal__head">
                  <span className="modal__title">Invite {key.who}</span>
                  <div className="spacer" />
                  <button
                    className="btn btn--ghost btn--sm"
                    onClick={() => setKey(null)}
                  >
                    Close
                  </button>
                </div>
                <p
                  className="field__hint"
                  style={{ margin: "0 0 var(--sp-3)", textAlign: "center" }}
                >
                  Scan with the <strong>Tailarr app</strong> to join as{" "}
                  {key.who} and set up their services automatically.
                  <br />
                  Single-use, expires in 24h — shown once.
                </p>
                <div className="invite-qr invite-qr--modal">
                  <QRCodeSVG
                    value={inviteLink(window.location.origin, key.key)}
                    size={200}
                    marginSize={2}
                    aria-label={`Enrollment QR code for ${key.who}`}
                  />
                </div>
                <div
                  className="preview-row"
                  style={{ marginTop: "var(--sp-4)", justifyContent: "center" }}
                >
                  <button
                    className="btn btn--sm"
                    title="A link you can text or email — opens the Tailarr app and sets everything up"
                    onClick={() =>
                      copyText(
                        inviteLink(window.location.origin, key.key),
                        "Invite link",
                      )
                    }
                  >
                    Copy invite link
                  </button>
                  <button
                    className="btn btn--sm"
                    title="The raw enrollment key, for `tailscale up --auth-key=…` on a device without the app"
                    onClick={() => copyText(key.key, "Auth key")}
                  >
                    Copy auth key
                  </button>
                </div>
              </div>
            </div>
          )}

          {addOpen && (
            <div className="scrim" onClick={addBusy ? undefined : () => setAddOpen(false)}>
              <div className="modal" onClick={(e) => e.stopPropagation()}>
                <div className="modal__head">
                  <span className="modal__title">Add a user</span>
                  <div className="spacer" />
                  <button
                    className="btn btn--ghost btn--sm"
                    disabled={addBusy}
                    onClick={() => setAddOpen(false)}
                  >
                    Close
                  </button>
                </div>
                <p className="field__hint" style={{ margin: "0 0 var(--sp-3)" }}>
                  Creates the user and creates their enrollment key in one step.
                  Devices that log in with the key are theirs automatically
                  and inherit whatever services you grant them — now or later.
                </p>
                <Field label="Name">
                  <input
                    className="input"
                    autoFocus
                    value={addName}
                    placeholder="e.g. Dave"
                    onChange={(e) => setAddName(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") addPerson();
                      if (e.key === "Escape") setAddOpen(false);
                    }}
                  />
                </Field>
                <div className="preview-row">
                  <button
                    className={"btn btn--primary" + (addBusy ? " btn--loading" : "")}
                    disabled={addBusy || !addName.trim()}
                    onClick={addPerson}
                  >
                    {addBusy && <SpinnerIcon className="btn-icon" />}
                    Create + get key
                  </button>
                </div>
              </div>
            </div>
          )}

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
                  For devices already on your network (e.g. an Apple TV that
                  signed in with an Apple ID). Paste its device ID from the
                  Tailscale admin console. Tagging replaces the device’s login
                  ownership — it appears under Unassigned with zero access.
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

          {deleting && (
            <ConfirmDialog
              title={`Remove ${deleting.name}?`}
              confirmLabel="Remove user"
              onConfirm={removePerson}
              onCancel={() => setDeleting(null)}
            >
              <p>
                Their devices stay connected but lose all service access and
                the ownership link — they’ll show under Unassigned.
                Any unused enrollment keys keep working until they expire
                (24h), but new devices would arrive ownerless.
              </p>
            </ConfirmDialog>
          )}
        </>
      )}
    </>
  );
}
