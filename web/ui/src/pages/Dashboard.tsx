import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import type { Pod } from "../types";
import { api } from "../api";
import { PodCard } from "../components/PodCard";
import { LogsModal } from "../components/LogsModal";
import { EditModal } from "../components/EditModal";
import { Alert } from "../components/Alert";
import { GridIcon } from "../components/Icons";

export function Dashboard() {
  const [pods, setPods] = useState<Pod[] | null>(null);
  const [error, setError] = useState<string>("");
  const [logsFor, setLogsFor] = useState<string | null>(null);
  const [editFor, setEditFor] = useState<string | null>(null);

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

  const running = pods?.filter((p) => p.state === "running").length ?? 0;

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

      <div className="section-title">Deployed</div>
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
