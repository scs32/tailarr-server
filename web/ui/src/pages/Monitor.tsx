import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import type { MonitorStatus } from "../types";
import { api } from "../api";
import { Alert } from "../components/Alert";
import { ChipPicker } from "../components/ChipPicker";
import { FlashView, useFlash } from "../components/Flash";
import { Field } from "../components/Form";
import { PodGlyph, PulseIcon, SpinnerIcon } from "../components/Icons";

// Drag a pod card onto the Kuma card to wire up monitoring: an animated
// beam draws the connection while the monitor is created over Kuma's
// socket API. Monitored pods show a linked state with an Unlink action.

interface Beam {
  from: { x: number; y: number };
  to: { x: number; y: number };
  pod: string;
}

export function Monitor() {
  const [status, setStatus] = useState<MonitorStatus | null>(null);
  const { flash, show, clear } = useFlash();
  const [busy, setBusy] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const [beam, setBeam] = useState<Beam | null>(null);
  const [pulse, setPulse] = useState(false);

  // connect form
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [url, setUrl] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [showExternal, setShowExternal] = useState(false); // connect form w/o a kuma pod

  const podRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const kumaRef = useRef<HTMLDivElement | null>(null);

  const refresh = useCallback(async () => {
    try {
      const s = await api.monitor();
      setStatus(s);
      if (!s.configured && s.kuma_url) setUrl((u) => u || s.kuma_url);
    } catch (e) {
      show({ kind: "err", text: String(e) });
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 15000); // keep monitored flags + URLs current
    return () => clearInterval(t);
  }, [refresh]);

  async function connect() {
    setConnecting(true);
    try {
      const r = await api.monitorSetup({ url: url.trim(), username, password });
      show(
        r.ok
          ? {
              kind: "ok",
              text: r.fresh
                ? "Kuma initialized — the admin account was created with these credentials."
                : "Connected to Uptime Kuma.",
            }
          : { kind: "err", text: r.error ?? "Connection failed." },
      );
      if (r.ok) {
        setPassword("");
        await refresh();
      }
    } finally {
      setConnecting(false);
    }
  }

  async function addMonitor(pod: string) {
    // beam from the pod card to the kuma card
    const from = podRefs.current[pod]?.getBoundingClientRect();
    const to = kumaRef.current?.getBoundingClientRect();
    if (from && to) {
      setBeam({
        from: { x: from.right - 10, y: from.top + from.height / 2 },
        to: { x: to.left + to.width / 2, y: to.top + to.height / 2 },
        pod,
      });
      setPulse(true);
    }
    setBusy(pod);
    try {
      const r = await api.monitorPod(pod, "add");
      // let the animation land before resolving it
      await new Promise((res) => setTimeout(res, 1100));
      show(
        r.ok
          ? { kind: "ok", text: `${pod} is now monitored by Kuma.` }
          : { kind: "err", text: r.error ?? "Failed to add the monitor." },
      );
      await refresh();
    } finally {
      setBusy("");
      setBeam(null);
      setTimeout(() => setPulse(false), 600);
    }
  }

  async function removeMonitor(pod: string) {
    setBusy(pod);
    try {
      const r = await api.monitorPod(pod, "remove");
      show(
        r.ok
          ? { kind: "ok", text: `Removed the ${pod} monitor.` }
          : { kind: "err", text: r.error ?? "Failed to remove the monitor." },
      );
      await refresh();
    } finally {
      setBusy("");
    }
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragOver(false);
    const pod = e.dataTransfer.getData("text/plain");
    if (pod && status?.connected) addMonitor(pod);
  }

  const ready = status?.available && status?.connected;

  return (
    <>
      <h1 className="page-title">Monitor</h1>
      <p style={{ color: "var(--muted)", margin: 0 }}>
        Drag a pod onto the Kuma card to start monitoring it.
      </p>

      <FlashView flash={flash} onClose={clear} />
      {status?.error && (
        <div style={{ marginTop: "var(--sp-5)" }}>
          <Alert kind="err">{status.error}</Alert>
        </div>
      )}

      {status === null ? (
        <p style={{ color: "var(--muted)", marginTop: "var(--sp-6)" }}>Loading…</p>
      ) : (
        <div className="monitor-layout">
          <div>
            <div className="section-title">Tailnet pods</div>
            {status.pods.length === 0 ? (
              <p style={{ color: "var(--muted)", margin: 0 }}>
                No tailscale-enabled pods deployed.
              </p>
            ) : (
              <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))" }}>
                {status.pods.map((p) => (
                  <div
                    key={p.name}
                    ref={(el) => {
                      podRefs.current[p.name] = el;
                    }}
                    className={
                      "pod-card card monitor-pod" +
                      (p.monitored ? " monitor-pod--linked" : "") +
                      (busy === p.name ? " monitor-pod--busy" : "")
                    }
                    draggable={ready && !p.monitored && !busy}
                    onDragStart={(e) => {
                      e.dataTransfer.setData("text/plain", p.name);
                      e.dataTransfer.effectAllowed = "link";
                    }}
                    title={
                      p.monitored
                        ? `Monitored: ${p.url}`
                        : ready
                          ? "Drag onto the Kuma card to monitor"
                          : undefined
                    }
                  >
                    <div className="pod-card__head">
                      <div className="pod-icon">
                        <PodGlyph />
                      </div>
                      <div className="pod-card__info">
                        <div className="pod-card__title">{p.name}</div>
                        <div className="pod-card__url" title={p.url}>
                          {p.dns_name || p.url}
                        </div>
                      </div>
                      <span className={`state-dot state-dot--${p.state}`} title={p.state} />
                    </div>
                    <div className="pod-card__foot">
                      {p.monitored ? (
                        <>
                          <span className="chip chip--installed">
                            <PulseIcon style={{ width: 11, height: 11, marginRight: 4, verticalAlign: -1 }} />
                            monitored
                          </span>
                          <div className="spacer" />
                          <button
                            className={"btn btn--ghost btn--sm" + (busy === p.name ? " btn--loading" : "")}
                            disabled={!!busy}
                            onClick={() => removeMonitor(p.name)}
                          >
                            {busy === p.name && <SpinnerIcon className="btn-icon" />}
                            Unlink
                          </button>
                        </>
                      ) : (
                        <>
                          <span className="preview-label">not monitored</span>
                          <div className="spacer" />
                          <button
                            className={"btn btn--ghost btn--sm" + (busy === p.name ? " btn--loading" : "")}
                            disabled={!ready || !!busy}
                            title={ready ? "Or click to add without dragging" : "Connect Kuma first"}
                            onClick={() => addMonitor(p.name)}
                          >
                            {busy === p.name && <SpinnerIcon className="btn-icon" />}
                            + Monitor
                          </button>
                        </>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="monitor-layout__side">
            <div className="section-title">Monitoring</div>
            <div
              ref={kumaRef}
              className={
                "card kuma-card" +
                (dragOver ? " kuma-card--over" : "") +
                (pulse ? " kuma-card--pulse" : "")
              }
              onDragOver={(e) => {
                if (!ready) return;
                e.preventDefault();
                e.dataTransfer.dropEffect = "link";
                setDragOver(true);
              }}
              onDragLeave={() => setDragOver(false)}
              onDrop={onDrop}
            >
              <div className="kuma-card__glyph">
                <PulseIcon />
              </div>
              <div className="pod-card__title">Uptime Kuma</div>

              {!status.available ? (
                <p className="field__hint" style={{ textAlign: "center", margin: 0 }}>
                  {status.error ?? "Monitoring client not available in this image."}
                </p>
              ) : status.connected ? (
                <>
                  <span className="chip chip--installed">connected</span>
                  <p className="field__hint" style={{ margin: "var(--sp-2) 0 0", textAlign: "center" }}>
                    {status.monitors.length} monitor
                    {status.monitors.length === 1 ? "" : "s"}
                    {status.kuma_link && (
                      <>
                        {" · "}
                        <a href={status.kuma_link} target="_blank" rel="noopener noreferrer">
                          open Kuma
                        </a>
                      </>
                    )}
                  </p>
                  <div style={{ marginTop: "var(--sp-3)", display: "flex", justifyContent: "center" }}>
                    <ChipPicker
                      chips={status.pods.filter((p) => p.monitored).map((p) => p.name)}
                      options={status.pods.map((p) => ({ id: p.name, hint: p.dns_name }))}
                      onAdd={addMonitor}
                      onRemove={removeMonitor}
                      addLabel="+ Add pod"
                      busyId={busy}
                    />
                  </div>
                  <p className="field__hint" style={{ margin: "var(--sp-3) 0 0", textAlign: "center" }}>
                    …or drag a pod card here
                  </p>
                </>
              ) : !status.kuma_pod && !status.configured && !showExternal ? (
                <>
                  <p className="field__hint" style={{ textAlign: "center", margin: "0 0 var(--sp-4)" }}>
                    Uptime Kuma isn't installed on this tailnet yet.
                  </p>
                  <Link
                    className="btn btn--primary"
                    to="/install/uptime-kuma"
                    style={{ width: "100%", justifyContent: "center" }}
                  >
                    Install Uptime Kuma
                  </Link>
                  <button
                    className="btn btn--ghost btn--sm"
                    style={{ marginTop: "var(--sp-2)" }}
                    onClick={() => setShowExternal(true)}
                  >
                    Use an external Kuma…
                  </button>
                </>
              ) : (
                <div style={{ width: "100%" }}>
                  <p className="field__hint" style={{ margin: "0 0 var(--sp-3)", textAlign: "center" }}>
                    {status.configured
                      ? "Saved credentials stopped working — reconnect."
                      : "Connect to Kuma. On a fresh instance these credentials create the admin account."}
                  </p>
                  <Field label="Kuma URL">
                    <input className="input" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="http://100.x.y.z:3001" />
                  </Field>
                  <Field label="Username">
                    <input className="input" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="off" />
                  </Field>
                  <Field label="Password">
                    <input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
                  </Field>
                  <button
                    className={"btn btn--primary" + (connecting ? " btn--loading" : "")}
                    disabled={connecting || !url.trim() || !username || !password}
                    style={{ width: "100%" }}
                    onClick={connect}
                  >
                    {connecting && <SpinnerIcon className="btn-icon" />}
                    Connect
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {beam && (
        <svg className="beam-overlay">
          <path
            className="beam-path"
            d={`M ${beam.from.x} ${beam.from.y} Q ${(beam.from.x + beam.to.x) / 2} ${
              Math.min(beam.from.y, beam.to.y) - 60
            } ${beam.to.x} ${beam.to.y}`}
          />
          <circle className="beam-dot" r="5">
            <animateMotion
              dur="1.1s"
              repeatCount="1"
              fill="freeze"
              path={`M ${beam.from.x} ${beam.from.y} Q ${(beam.from.x + beam.to.x) / 2} ${
                Math.min(beam.from.y, beam.to.y) - 60
              } ${beam.to.x} ${beam.to.y}`}
            />
          </circle>
        </svg>
      )}
    </>
  );
}
