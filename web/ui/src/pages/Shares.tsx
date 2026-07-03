import { useCallback, useEffect, useState } from "react";
import type { Share, ShareResult } from "../types";
import { api } from "../api";
import { Field } from "../components/Form";
import { Alert } from "../components/Alert";

// Defining shared folders only. Attaching a share to a pod happens in the
// pod's Edit popup on the dashboard.
export function Shares() {
  const [shares, setShares] = useState<Share[]>([]);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const [name, setName] = useState("");
  const [host, setHost] = useState("");
  const [cont, setCont] = useState("");
  const [ro, setRo] = useState(false);

  const refresh = useCallback(async () => {
    setShares(await api.shares());
  }, []);

  useEffect(() => {
    refresh().catch((e) => setMsg({ kind: "err", text: String(e) }));
  }, [refresh]);

  function report(r: ShareResult) {
    setMsg(
      r.ok
        ? { kind: "ok", text: r.message ?? "Done." }
        : { kind: "err", text: r.error ?? "Failed." },
    );
    refresh();
  }

  async function add() {
    report(await api.shareAdd(name, host, cont, ro));
    setName("");
    setHost("");
    setCont("");
    setRo(false);
  }

  return (
    <>
      <h1 className="page-title">Shared folders</h1>
      <p style={{ color: "var(--muted)", margin: 0 }}>
        Media-only mounts — the one thing allowed to pierce the pod barrier.
        Attach them to a pod via its <strong>Edit</strong> button on the
        dashboard.
      </p>

      {msg && (
        <div style={{ marginTop: "var(--sp-5)" }}>
          <Alert kind={msg.kind}>{msg.text}</Alert>
        </div>
      )}

      <div className="section-title">Defined shares</div>
      {shares.length === 0 ? (
        <p style={{ color: "var(--muted)", margin: 0 }}>
          No shared folders defined yet.
        </p>
      ) : (
        <div className="row-list" style={{ maxWidth: 640 }}>
          {shares.map((s) => (
            <div key={s.name} className="row card">
              <div style={{ minWidth: 0 }}>
                <div className="row__title">{s.name}</div>
                <div className="row__meta">
                  {s.host_path} → {s.container_path}
                </div>
              </div>
              <div className="spacer" />
              <span className={"chip" + (s.ro ? "" : " chip--installed")}>
                {s.mode}
              </span>
              <span className="preview-label">
                {s.used_by.length ? `used by ${s.used_by.join(", ")}` : "unused"}
                {s.visible ? "" : " · not visible"}
              </span>
              <button
                className="btn btn--danger btn--sm"
                onClick={async () => report(await api.shareDelete(s.name))}
              >
                Delete
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="section-title">Add a shared folder</div>
      <div style={{ maxWidth: 440 }}>
        <Field label="Name" hint="a–z, 0–9, dashes">
          <input
            className="input"
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
        <label className="toggle" style={{ margin: "var(--sp-2) 0 var(--sp-4)" }}>
          <input type="checkbox" checked={ro} onChange={(e) => setRo(e.target.checked)} />
          <span className="toggle__track" />
          <span>Read-only</span>
        </label>
        <div>
          <button className="btn btn--primary" disabled={!name || !host} onClick={add}>
            Add
          </button>
        </div>
      </div>
    </>
  );
}
