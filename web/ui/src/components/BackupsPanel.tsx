import { useCallback, useEffect, useState } from "react";
import type { BackupEntry } from "../types";
import { api } from "../api";
import { FormSection } from "./Form";
import { ConfirmDialog } from "./ConfirmDialog";
import { SpinnerIcon } from "./Icons";

function fmtSize(bytes: number): string {
  if (bytes >= 1 << 30) return `${(bytes / (1 << 30)).toFixed(1)} GB`;
  if (bytes >= 1 << 20) return `${(bytes / (1 << 20)).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function fmtTs(ts: string): string {
  // YYYYMMDD-HHMMSS -> YYYY-MM-DD HH:MM
  return `${ts.slice(0, 4)}-${ts.slice(4, 6)}-${ts.slice(6, 8)} ${ts.slice(9, 11)}:${ts.slice(11, 13)}`;
}

// Per-pod snapshots: stop -> tar the pod's whole directory -> start. The pod
// is briefly down while the tar runs; an in-place restore brings back the
// same data AND the same tailnet identity.
export function BackupsPanel({
  name,
  controller,
  onChanged,
}: {
  name: string;
  controller: boolean;
  onChanged: () => void;
}) {
  const [entries, setEntries] = useState<BackupEntry[] | null>(null);
  const [busy, setBusy] = useState<"" | "backup" | string>(""); // "restore:<ts>" / "delete:<ts>"
  const [msg, setMsg] = useState("");
  const [confirmRestore, setConfirmRestore] = useState<BackupEntry | null>(null);

  const refresh = useCallback(
    () => api.backups(name).then(setEntries).catch(() => setEntries([])),
    [name],
  );
  useEffect(() => {
    refresh();
  }, [refresh]);

  async function backupNow() {
    setBusy("backup");
    setMsg("");
    try {
      const r = await api.backupCreate(name);
      setMsg(r.ok ? `Snapshot taken (${fmtSize(r.backup?.size ?? 0)}).` : (r.error ?? r.status));
      refresh();
      onChanged();
    } catch (e) {
      setMsg(String(e));
    } finally {
      setBusy("");
    }
  }

  async function restore(entry: BackupEntry) {
    setConfirmRestore(null);
    setBusy(`restore:${entry.ts}`);
    setMsg("");
    try {
      const r = await api.backupRestore(name, entry.ts);
      setMsg(r.ok ? "Restored — pod restarted with the snapshot's data." : (r.error ?? r.status));
      onChanged();
    } catch (e) {
      setMsg(String(e));
    } finally {
      setBusy("");
    }
  }

  async function remove(entry: BackupEntry) {
    setBusy(`delete:${entry.ts}`);
    try {
      await api.backupDelete(name, entry.ts);
      refresh();
    } finally {
      setBusy("");
    }
  }

  if (controller) return null;

  return (
    <FormSection title="Backups">
      <p className="field__hint" style={{ margin: 0 }}>
        Snapshots the pod’s whole directory (config, data, tailnet identity —
        media shares excluded). The pod is stopped for the copy, then started
        again. Keeps 7 daily + 4 weekly.
      </p>

      <div className="preview-row">
        <button
          className={"btn btn--secondary btn--sm" + (busy === "backup" ? " btn--loading" : "")}
          disabled={!!busy}
          onClick={backupNow}
        >
          {busy === "backup" && <SpinnerIcon className="btn-icon" />}
          Back up now
        </button>
        {msg && <span className="field__hint">{msg}</span>}
      </div>

      {entries === null ? (
        <p className="field__hint" style={{ margin: 0 }}>Loading…</p>
      ) : entries.length === 0 ? (
        <p className="field__hint" style={{ margin: 0 }}>No snapshots yet.</p>
      ) : (
        <div>
          {entries.map((e) => (
            <div
              key={e.ts}
              className="preview-row"
              style={{ alignItems: "center", padding: "var(--sp-1) 0" }}
            >
              <span style={{ fontFamily: "monospace" }}>{fmtTs(e.ts)}</span>
              <span className="field__hint">
                {fmtSize(e.size)}
                {e.image && ` · ${e.image.split("/").pop()}`}
                {e.reason && ` · ${e.reason}`}
              </span>
              <div className="spacer" />
              <button
                className={
                  "btn btn--secondary btn--sm" +
                  (busy === `restore:${e.ts}` ? " btn--loading" : "")
                }
                disabled={!!busy}
                onClick={() => setConfirmRestore(e)}
              >
                {busy === `restore:${e.ts}` && <SpinnerIcon className="btn-icon" />}
                Restore
              </button>
              <button
                className="btn btn--ghost btn--sm"
                disabled={!!busy}
                onClick={() => remove(e)}
              >
                Delete
              </button>
            </div>
          ))}
        </div>
      )}

      {confirmRestore && (
        <ConfirmDialog
          title={`Restore ${name} from ${fmtTs(confirmRestore.ts)}?`}
          confirmLabel="Restore"
          onConfirm={() => restore(confirmRestore)}
          onCancel={() => setConfirmRestore(null)}
        >
          The pod’s current data is replaced with this snapshot and the pod is
          restarted. It keeps its tailnet identity and URL. Anything changed
          since the snapshot is lost.
        </ConfirmDialog>
      )}
    </FormSection>
  );
}
