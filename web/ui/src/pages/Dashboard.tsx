import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import type { FleetAction, Pod } from "../types";
import { api } from "../api";
import { PodCard } from "../components/PodCard";
import { LogsModal } from "../components/LogsModal";
import { EditModal } from "../components/EditModal";
import { Alert } from "../components/Alert";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { FlashView, useFlash } from "../components/Flash";
import { GridIcon, SpinnerIcon } from "../components/Icons";

export function Dashboard() {
  const [pods, setPods] = useState<Pod[] | null>(null);
  const [error, setError] = useState<string>("");
  const [logsFor, setLogsFor] = useState<string | null>(null);
  const [editFor, setEditFor] = useState<string | null>(null);
  const [confirmFleet, setConfirmFleet] = useState<FleetAction | null>(null);
  const [fleetBusy, setFleetBusy] = useState<FleetAction | "">("");
  const { flash, show, clear } = useFlash();

  const refresh = useCallback(async () => {
    try {
      setPods(await api.pods());
      setError("");
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 6000); // keep pod state fresh
    return () => clearInterval(t);
  }, [refresh]);

  async function runFleet(action: FleetAction) {
    setConfirmFleet(null);
    setFleetBusy(action);
    try {
      const r = await api.fleet(action);
      const done = r.results.filter((x) => x.ok).length;
      const skipped = r.skipped.length
        ? `; skipped (busy): ${r.skipped.map((s) => s.name).join(", ")}`
        : "";
      show(
        r.ok
          ? {
              kind: "ok",
              text: `Fleet ${action}: ${done} pod${done === 1 ? "" : "s"}${skipped}.`,
            }
          : { kind: "err", text: r.error ?? `Fleet ${action} failed.` },
      );
    } catch (e) {
      show({ kind: "err", text: String(e) });
    } finally {
      setFleetBusy("");
      refresh();
    }
  }

  const running = pods?.filter((p) => p.state === "running").length ?? 0;
  // Fleet buttons act on everything except the controller pod itself.
  const fleetUp = pods?.some((p) => !p.controller && p.state === "running") ?? false;
  const fleetDown = pods?.some((p) => !p.controller && p.state !== "running") ?? false;

  return (
    <>
      <h1 className="page-title">Dashboard</h1>
      <p style={{ color: "var(--muted)", margin: 0 }}>
        {pods === null
          ? "Loading…"
          : `${pods.length} pod${pods.length === 1 ? "" : "s"} · ${running} running · every service on its own tailnet identity`}
      </p>

      {error && (
        <div style={{ marginTop: "var(--sp-5)" }}>
          <Alert kind="err">Couldn’t reach the controller API: {error}</Alert>
        </div>
      )}

      <FlashView flash={flash} onClose={clear} />

      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "var(--sp-3)",
        }}
      >
        <div className="section-title">Deployed</div>
        {pods && pods.length > 0 && (
          <div className="preview-row" style={{ gap: "var(--sp-2)" }}>
            {fleetDown && (
              <button
                className={
                  "btn btn--secondary btn--sm" +
                  (fleetBusy === "start" ? " btn--loading" : "")
                }
                disabled={!!fleetBusy}
                title="Start every stopped pod (sidecars first)"
                onClick={() => runFleet("start")}
              >
                {fleetBusy === "start" && <SpinnerIcon className="btn-icon" />}
                Start all
              </button>
            )}
            {fleetUp && (
              <button
                className={
                  "btn btn--ghost btn--sm" +
                  (fleetBusy === "restart" ? " btn--loading" : "")
                }
                disabled={!!fleetBusy}
                title="Stop then start every pod"
                onClick={() => setConfirmFleet("restart")}
              >
                {fleetBusy === "restart" && <SpinnerIcon className="btn-icon" />}
                Restart all
              </button>
            )}
            {fleetUp && (
              <button
                className={
                  "btn btn--secondary btn--sm" +
                  (fleetBusy === "stop" ? " btn--loading" : "")
                }
                disabled={!!fleetBusy}
                title="Gracefully stop every pod (the controller stays up)"
                onClick={() => setConfirmFleet("stop")}
              >
                {fleetBusy === "stop" && <SpinnerIcon className="btn-icon" />}
                Stop all
              </button>
            )}
          </div>
        )}
      </div>
      {pods && pods.length === 0 ? (
        <div className="empty">
          <GridIcon className="empty__icon" />
          <div className="empty__title">No pods deployed yet</div>
          <p style={{ margin: "0 0 var(--sp-5)" }}>
            Install a service from the catalog, or spin up any OCI image.
          </p>
          <div className="preview-row" style={{ justifyContent: "center" }}>
            <Link className="btn btn--primary" to="/catalog">
              Browse catalog
            </Link>
            <Link className="btn btn--secondary" to="/custom">
              + Custom pod
            </Link>
          </div>
        </div>
      ) : (
        <div className="grid">
          {pods?.map((pod) => (
            <PodCard
              key={pod.name}
              pod={pod}
              onChanged={refresh}
              onLogs={setLogsFor}
              onEdit={setEditFor}
            />
          ))}
        </div>
      )}

      {confirmFleet && (
        <ConfirmDialog
          title={confirmFleet === "stop" ? "Stop all pods?" : "Restart all pods?"}
          confirmLabel={confirmFleet === "stop" ? "Stop all" : "Restart all"}
          onConfirm={() => runFleet(confirmFleet)}
          onCancel={() => setConfirmFleet(null)}
        >
          {confirmFleet === "stop" ? (
            <>
              Every pod and its Tailscale sidecar gets a graceful stop. The
              podscale controller stays running so you can start everything
              again from here — shutting down the host or VM itself happens
              outside this UI, after the pods are down.
            </>
          ) : (
            <>
              Every pod is stopped, then started again (sidecars first). Brief
              downtime for all services; the controller itself is untouched.
            </>
          )}
        </ConfirmDialog>
      )}

      {logsFor && <LogsModal name={logsFor} onClose={() => setLogsFor(null)} />}
      {editFor && (
        <EditModal
          name={editFor}
          onClose={() => setEditFor(null)}
          onChanged={refresh}
        />
      )}
    </>
  );
}
