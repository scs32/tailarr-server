import { useCallback, useEffect, useState } from "react";
import type { Pod, Share, ShareResult } from "../types";
import { api } from "../api";
import { Field, FormSection } from "../components/Form";
import { Alert } from "../components/Alert";

export function Shares() {
  const [shares, setShares] = useState<Share[]>([]);
  const [pods, setPods] = useState<Pod[]>([]);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  // add form
  const [name, setName] = useState("");
  const [host, setHost] = useState("");
  const [cont, setCont] = useState("");
  const [ro, setRo] = useState(false);

  // attach form
  const [attShare, setAttShare] = useState("");
  const [attPod, setAttPod] = useState("");

  const refresh = useCallback(async () => {
    const [sh, pd] = await Promise.all([api.shares(), api.pods()]);
    setShares(sh);
    setPods(pd);
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

  const attachable = pods.filter((p) => !p.controller).map((p) => p.name);

  return (
    <>
      <h1 className="page-title">Shared folders</h1>
      <p style={{ color: "var(--muted)", margin: 0 }}>
        Media-only mounts — the one thing allowed to pierce the pod barrier.
        Configs and databases stay per-pod.
      </p>

      {msg && (
        <div style={{ marginTop: "var(--sp-5)" }}>
          <Alert kind={msg.kind}>{msg.text}</Alert>
        </div>
      )}

      <div className="section-title">Defined shares</div>
      {shares.length === 0 ? (
        <p style={{ color: "var(--muted)" }}>No shared folders defined yet.</p>
      ) : (
        <div className="grid">
          {shares.map((s) => (
            <div key={s.name} className="pod-card card">
              <div className="pod-card__head">
                <div>
                  <div className="pod-card__title">{s.name}</div>
                  <div className="pod-card__url">
                    {s.host_path} → {s.container_path}
                  </div>
                </div>
                <div className="spacer" />
                <span className={"chip" + (s.ro ? "" : " chip--installed")}>
                  {s.mode}
                </span>
              </div>
              <div className="pod-card__foot">
                <span className="preview-label">
                  {s.used_by.length ? `used by ${s.used_by.join(", ")}` : "unused"}
                  {s.visible ? "" : " · not visible from controller"}
                </span>
                <div className="spacer" />
                <button
                  className="btn btn--danger btn--sm"
                  onClick={async () => report(await api.shareDelete(s.name))}
                >
                  Delete
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      <div style={{ maxWidth: 560 }}>
        {shares.length > 0 && attachable.length > 0 && (
          <FormSection title="Attach to a deployed pod">
            <p className="field__hint" style={{ marginTop: 0 }}>
              Regenerates the pod’s scripts; restart the pod to apply.
            </p>
            <div className="preview-row">
              <select
                className="select"
                value={attShare}
                onChange={(e) => setAttShare(e.target.value)}
                style={{ width: "auto" }}
              >
                <option value="">share…</option>
                {shares.map((s) => (
                  <option key={s.name}>{s.name}</option>
                ))}
              </select>
              <select
                className="select"
                value={attPod}
                onChange={(e) => setAttPod(e.target.value)}
                style={{ width: "auto" }}
              >
                <option value="">pod…</option>
                {attachable.map((p) => (
                  <option key={p}>{p}</option>
                ))}
              </select>
              <button
                className="btn btn--secondary"
                disabled={!attShare || !attPod}
                onClick={async () => report(await api.shareAttach(attPod, attShare))}
              >
                Attach
              </button>
            </div>
          </FormSection>
        )}

        <FormSection title="Add a shared folder">
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
          <label className="toggle" style={{ marginBottom: "var(--sp-4)" }}>
            <input type="checkbox" checked={ro} onChange={(e) => setRo(e.target.checked)} />
            <span className="toggle__track" />
            <span>Read-only</span>
          </label>
          <button
            className="btn btn--primary"
            disabled={!name || !host}
            onClick={add}
          >
            Add
          </button>
        </FormSection>
      </div>
    </>
  );
}
