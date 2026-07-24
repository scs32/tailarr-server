import { useCallback, useEffect, useState } from "react";
import type { PodStat, StatsSnapshot } from "../types";
import { api } from "../api";
import { Alert } from "../components/Alert";
import { PodGlyph } from "../components/Icons";

// Live resource stats for the fleet — one podman-stats snapshot per poll,
// per pod (app container + tailscale sidecar). LIVE ONLY today; the data
// shape carries an `at` timestamp so historical trends can slot in later
// without reworking this page (see CLAUDE.md backlog item 9).

const POLL_MS = 3000;

function fmtBytes(n: number): string {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(i > 0 && v < 10 ? 1 : 0)} ${units[i]}`;
}

function meterClass(pct: number): string {
  if (pct >= 90) return " meter__fill--danger";
  if (pct >= 70) return " meter__fill--warn";
  return "";
}

function Meter({ pct }: { pct: number }) {
  const clamped = Math.max(0, Math.min(100, pct));
  return (
    <div className="meter">
      <div
        className={"meter__fill" + meterClass(pct)}
        style={{ width: `${clamped}%` }}
      />
    </div>
  );
}

function PodCard({ pod }: { pod: PodStat }) {
  const running = pod.state === "running";
  const memPct = pod.mem_limit_bytes
    ? (pod.mem_bytes / pod.mem_limit_bytes) * 100
    : 0;
  return (
    <div className="pod-card card">
      <div className="pod-card__head">
        <div className="pod-icon">
          <PodGlyph />
        </div>
        <div className="pod-card__info">
          <div className="pod-card__title">{pod.label || pod.name}</div>
          <div className="pod-card__url">
            {running ? "running" : pod.state}
          </div>
        </div>
        <span
          className={`state-dot state-dot--${pod.state}`}
          title={pod.state}
        />
      </div>

      {running ? (
        <>
          <div className="stat-metric">
            <div className="stat-metric__row">
              <span>CPU</span>
              <span className="stat-metric__val">
                {pod.cpu_percent.toFixed(1)}%
              </span>
            </div>
            <Meter pct={pod.cpu_percent} />
          </div>

          <div className="stat-metric">
            <div className="stat-metric__row">
              <span>Memory</span>
              <span className="stat-metric__val">
                {fmtBytes(pod.mem_bytes)}
                {pod.mem_limit_bytes
                  ? ` / ${fmtBytes(pod.mem_limit_bytes)}`
                  : ""}
              </span>
            </div>
            {pod.mem_limit_bytes ? (
              <Meter pct={memPct} />
            ) : (
              <div className="stat-metric__row" style={{ marginTop: -1 }}>
                <span className="stat-updated">no memory limit set</span>
              </div>
            )}
          </div>
        </>
      ) : (
        <p
          className="field__hint"
          style={{ margin: "var(--sp-3) 0 0" }}
        >
          Pod isn't running — no live resource data.
        </p>
      )}
    </div>
  );
}

export function Stats() {
  const [snap, setSnap] = useState<StatsSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setSnap(await api.stats());
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, POLL_MS);
    return () => clearInterval(t);
  }, [refresh]);

  return (
    <>
      <h1 className="page-title">Stats</h1>
      <p style={{ color: "var(--muted)", margin: 0 }}>
        Live CPU and memory for every service, refreshed every{" "}
        {POLL_MS / 1000}s.
      </p>

      {error && (
        <div style={{ marginTop: "var(--sp-5)" }}>
          <Alert kind="err">{error}</Alert>
        </div>
      )}

      {snap === null ? (
        <p style={{ color: "var(--muted)", marginTop: "var(--sp-6)" }}>
          Loading…
        </p>
      ) : (
        <div style={{ marginTop: "var(--sp-5)" }}>
          <div className="stat-tiles">
            <div className="card stat-tile">
              <div className="stat-tile__big">
                {snap.totals.running}
                <span style={{ color: "var(--faint)", fontSize: "var(--fs-lg)" }}>
                  /{snap.totals.pods}
                </span>
              </div>
              <div className="stat-tile__label">Services running</div>
            </div>
            <div className="card stat-tile">
              <div className="stat-tile__big">
                {snap.totals.cpu_percent.toFixed(1)}%
              </div>
              <div className="stat-tile__label">Total CPU</div>
            </div>
            <div className="card stat-tile">
              <div className="stat-tile__big">
                {fmtBytes(snap.totals.mem_bytes)}
              </div>
              <div className="stat-tile__label">Total memory</div>
            </div>
          </div>

          {snap.pods.length === 0 ? (
            <p style={{ color: "var(--muted)" }}>No services deployed yet.</p>
          ) : (
            <div
              className="grid"
              style={{
                gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))",
              }}
            >
              {snap.pods.map((p) => (
                <PodCard key={p.name} pod={p} />
              ))}
            </div>
          )}

          <p className="stat-updated" style={{ marginTop: "var(--sp-4)" }}>
            updated {new Date(snap.at * 1000).toLocaleTimeString()}
          </p>
        </div>
      )}
    </>
  );
}
