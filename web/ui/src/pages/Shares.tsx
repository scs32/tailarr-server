import { useCallback, useEffect, useState } from "react";
import type { Share, ShareResult } from "../types";
import { api } from "../api";
import { Field, Toggle } from "../components/Form";
import { FlashView, useFlash } from "../components/Flash";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { SpinnerIcon } from "../components/Icons";

// Defining shared folders only. Attaching a share to a pod happens in the
// pod's Edit popup on the dashboard.
export function Shares() {
  const [shares, setShares] = useState<Share[]>([]);
  const { flash, show, clear } = useFlash();

  // Add-share popup
  const [addOpen, setAddOpen] = useState(false);
  const [addBusy, setAddBusy] = useState(false);
  const [name, setName] = useState("");
  const [host, setHost] = useState("");
  const [cont, setCont] = useState("");
  const [ro, setRo] = useState(false);

  // NFS-export popup (one share at a time)
  const [nfsFor, setNfsFor] = useState<Share | null>(null);
  const [nfsClients, setNfsClients] = useState("");
  const [nfsRo, setNfsRo] = useState(true);
  const [nfsBusy, setNfsBusy] = useState(false);

  const [deleting, setDeleting] = useState<Share | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  const refresh = useCallback(async () => {
    setShares(await api.shares());
  }, []);

  useEffect(() => {
    refresh().catch((e) => show({ kind: "err", text: String(e) }));
  }, [refresh]);

  function report(r: ShareResult) {
    show(
      r.ok
        ? { kind: "ok", text: r.message ?? "Done." }
        : { kind: "err", text: r.error ?? "Failed." },
    );
    refresh();
  }

  async function add() {
    setAddBusy(true);
    try {
      const r = await api.shareAdd(name.trim(), host.trim(), cont.trim(), ro);
      report(r);
      if (r.ok) {
        setAddOpen(false);
        setName("");
        setHost("");
        setCont("");
        setRo(false);
      }
    } finally {
      setAddBusy(false);
    }
  }

  async function confirmDelete() {
    if (!deleting) return;
    setDeleteBusy(true);
    try {
      report(await api.shareDelete(deleting.name));
      setDeleting(null);
    } finally {
      setDeleteBusy(false);
    }
  }

  function openNfs(s: Share) {
    setNfsFor(s);
    setNfsClients(s.nfs?.clients ?? "");
    setNfsRo(s.nfs?.ro ?? true);
  }

  async function applyNfs(enabled: boolean) {
    if (!nfsFor) return;
    setNfsBusy(true);
    try {
      report(await api.shareNfs(nfsFor.name, enabled, nfsClients, nfsRo));
      setNfsFor(null);
    } finally {
      setNfsBusy(false);
    }
  }

  return (
    <>
      <h1 className="page-title">Shared folders</h1>
      <p style={{ color: "var(--muted)", margin: 0 }}>
        Media-only mounts — the one thing allowed to pierce the pod barrier.
        Attach them to a pod via its <strong>Edit</strong> button on the
        dashboard.
      </p>

      <FlashView flash={flash} onClose={clear} />

      <div className="section-head">
        <div className="section-title">Defined shares</div>
        <button className="btn btn--primary btn--sm" onClick={() => setAddOpen(true)}>
          + Add share
        </button>
      </div>
      {shares.length === 0 ? (
        <p style={{ color: "var(--muted)", margin: 0 }}>
          No shared folders defined yet.
        </p>
      ) : (
        <div className="row-list">
          {shares.map((s) => (
            <div key={s.name} className="row card">
              <div style={{ minWidth: 0 }}>
                <div className="row__title">{s.name}</div>
                <div className="row__meta">
                  {s.host_path} → {s.container_path}
                </div>
              </div>
              <div className="spacer" />
              {s.nfs && <span className="chip chip--installed">NFS</span>}
              <span className={"chip" + (s.ro ? "" : " chip--installed")}>
                {s.mode}
              </span>
              <span className="preview-label">
                {s.used_by.length ? `used by ${s.used_by.join(", ")}` : "unused"}
                {s.visible ? "" : " · folder not found on host"}
              </span>
              <button className="btn btn--ghost btn--sm" onClick={() => openNfs(s)}>
                NFS…
              </button>
              <button
                className="btn btn--danger btn--sm"
                onClick={() => setDeleting(s)}
              >
                Delete
              </button>
            </div>
          ))}
        </div>
      )}

      {addOpen && (
        <div className="scrim" onClick={addBusy ? undefined : () => setAddOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal__head">
              <span className="modal__title">Add a shared folder</span>
              <div className="spacer" />
              <button
                className="btn btn--ghost btn--sm"
                disabled={addBusy}
                onClick={() => setAddOpen(false)}
              >
                Close
              </button>
            </div>
            <Field label="Name" hint="a–z, 0–9, dashes">
              <input
                className="input"
                autoFocus
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="media"
              />
            </Field>
            <Field label="Host path">
              <input
                className="input"
                value={host}
                onChange={(e) => setHost(e.target.value)}
                placeholder="/data"
              />
            </Field>
            <Field label="Container path" hint="blank = same as host path">
              <input
                className="input"
                value={cont}
                onChange={(e) => setCont(e.target.value)}
                placeholder="/data"
              />
            </Field>
            <Toggle checked={ro} onChange={setRo}>
              Read-only
            </Toggle>
            <div className="preview-row">
              <button
                className={"btn btn--primary" + (addBusy ? " btn--loading" : "")}
                disabled={addBusy || !name.trim() || !host.trim()}
                onClick={add}
              >
                {addBusy && <SpinnerIcon className="btn-icon" />}
                Add share
              </button>
            </div>
          </div>
        </div>
      )}

      {nfsFor && (
        <div className="scrim" onClick={nfsBusy ? undefined : () => setNfsFor(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal__head">
              <span className="modal__title">NFS export — {nfsFor.name}</span>
              <div className="spacer" />
              <button
                className="btn btn--ghost btn--sm"
                disabled={nfsBusy}
                onClick={() => setNfsFor(null)}
              >
                Close
              </button>
            </div>
            <p className="field__hint" style={{ marginTop: 0 }}>
              Export <code>{nfsFor.host_path}</code> from this VM's kernel NFS
              server — e.g. to a native Plex on the machine hosting it. Mount it
              there as <code>nfs://&lt;vm-ip&gt;{nfsFor.host_path}</code>.
            </p>
            <Field
              label="Allowed clients"
              hint="IP, CIDR (192.168.1.0/24), or hostname — space-separated for several"
            >
              <input
                className="input"
                autoFocus
                value={nfsClients}
                onChange={(e) => setNfsClients(e.target.value)}
                placeholder="192.168.1.0/24"
              />
            </Field>
            <Toggle checked={nfsRo} onChange={setNfsRo}>
              Read-only export (recommended for media players)
            </Toggle>
            <div className="preview-row">
              <button
                className={"btn btn--primary" + (nfsBusy ? " btn--loading" : "")}
                disabled={nfsBusy || !nfsClients.trim()}
                onClick={() => applyNfs(true)}
              >
                {nfsBusy && <SpinnerIcon className="btn-icon" />}
                {nfsFor.nfs ? "Update export" : "Enable export"}
              </button>
              {nfsFor.nfs && (
                <button
                  className="btn btn--danger"
                  disabled={nfsBusy}
                  onClick={() => applyNfs(false)}
                >
                  Disable
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      {deleting && (
        <ConfirmDialog
          title={`Delete the ${deleting.name} share?`}
          confirmLabel="Delete"
          busy={deleteBusy}
          onConfirm={confirmDelete}
          onCancel={() => setDeleting(null)}
        >
          This removes the shared-folder definition
          {deleting.used_by.length > 0 && (
            <>
              {" "}
              — currently attached to{" "}
              <strong>{deleting.used_by.join(", ")}</strong>
            </>
          )}
          . The folder and its media on disk are not touched.
        </ConfirmDialog>
      )}
    </>
  );
}
