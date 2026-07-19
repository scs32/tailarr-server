import { useCallback, useEffect, useRef, useState } from "react";
import type { FleetResult, UpgradeStatus } from "../types";
import { api } from "../api";
import { Alert } from "./Alert";

// How long to wait for the new controller before advising a manual look.
// The helper sleeps ~3s, swaps the container, and health-checks for up to
// 60s before rolling back — 150s covers the full cycle plus image start.
const UPGRADE_WAIT_MS = 150_000;
const POLL_MS = 3_000;

type Phase =
  | "idle"
  | "checking"
  | "confirm"
  | "upgrading"
  | "done"
  | "rolledback"
  | "timeout";

export function UpgradeCard() {
  const [status, setStatus] = useState<UpgradeStatus | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState("");
  const [newVersion, setNewVersion] = useState("");
  const [rerenderBusy, setRerenderBusy] = useState(false);
  const [rerender, setRerender] = useState<FleetResult | null>(null);
  const timers = useRef<number[]>([]);

  const refresh = useCallback(() => {
    api
      .upgradeStatus()
      .then((s) => {
        setStatus(s);
        // A reloaded page mid-upgrade should resume waiting, not sit idle.
        if (s.busy) setPhase("upgrading");
      })
      .catch(() => setStatus(null));
  }, []);

  useEffect(refresh, [refresh]);
  useEffect(
    () => () => timers.current.forEach((t) => window.clearTimeout(t)),
    [],
  );

  async function check() {
    setPhase("checking");
    setError("");
    const s = await api.upgradeCheck();
    setStatus(s);
    if (s.ok === false) setError(s.error || "Release check failed.");
    setPhase("idle");
  }

  function waitForNewController(from: string) {
    const deadline = Date.now() + UPGRADE_WAIT_MS;
    const poll = async () => {
      try {
        const info = await api.info();
        if (info.version !== from) {
          setNewVersion(info.version);
          setPhase("done");
          return;
        }
        // Same version answering again: either the swap hasn't landed yet,
        // or the helper rolled back — its result file knows which.
        const s = await api.upgradeStatus().catch(() => null);
        if (s && !s.busy && s.last && s.last.rolled_back) {
          setStatus(s);
          setPhase("rolledback");
          return;
        }
      } catch {
        // Connection refused while the controller swaps — keep waiting.
      }
      if (Date.now() > deadline) {
        setPhase("timeout");
        return;
      }
      timers.current.push(window.setTimeout(poll, POLL_MS));
    };
    timers.current.push(window.setTimeout(poll, POLL_MS));
  }

  async function upgrade() {
    if (!status) return;
    setPhase("upgrading");
    setError("");
    const r = await api.upgrade();
    if (!r.ok) {
      setError(r.error || r.output || "Upgrade failed to start.");
      setPhase("idle");
      refresh();
      return;
    }
    waitForNewController(status.current);
  }

  async function applyEngineUpdates() {
    setRerenderBusy(true);
    try {
      setRerender(await api.fleet("rerender"));
    } finally {
      setRerenderBusy(false);
    }
  }

  if (status === null) {
    return <p style={{ color: "var(--muted)" }}>Loading…</p>;
  }

  return (
    <>
      <p className="field__hint" style={{ marginTop: 0 }}>
        Running <strong>v{status.current}</strong>
        {status.latest
          ? status.available
            ? " — v" + status.latest + " is available."
            : " — up to date (latest release: v" + status.latest + ")."
          : " — no release check yet."}
      </p>

      {status.last && status.last.rolled_back && phase === "idle" && (
        <Alert kind="err">
          The last upgrade to <code>{status.last.to}</code> failed its health
          check and was rolled back ({status.last.finished}). Details:{" "}
          <code>Pods/.upgrade/upgrade.log</code>.
        </Alert>
      )}

      {error && <Alert kind="err">{error}</Alert>}

      {phase === "upgrading" && (
        <Alert kind="info">
          Upgrading… the controller restarts in a few seconds and this page
          reconnects automatically. The Tailscale sidecar (and this pod’s
          HTTPS identity) is untouched.
        </Alert>
      )}

      {phase === "done" && (
        <>
          <Alert kind="ok">
            Upgraded to <strong>v{newVersion}</strong>. One step left: your
            pods are still running with the previous version’s settings —
            finish the upgrade to bring them along (each pod restarts
            briefly).
          </Alert>
          {rerender === null ? (
            <div className="preview-row" style={{ marginTop: "var(--sp-3)" }}>
              <button
                className={
                  "btn btn--primary btn--sm" +
                  (rerenderBusy ? " btn--loading" : "")
                }
                disabled={rerenderBusy}
                onClick={applyEngineUpdates}
              >
                Finish upgrade
              </button>
            </div>
          ) : (
            <Alert kind={rerender.ok ? "ok" : "err"}>
              {rerender.ok
                ? `Updated ${rerender.results.length} pod(s). Upgrade complete.`
                : rerender.error || "Some pods couldn't be updated."}
            </Alert>
          )}
        </>
      )}

      {phase === "rolledback" && (
        <Alert kind="err">
          The new controller failed its health check — the upgrade was rolled
          back and v{status.current} is still running. See{" "}
          <code>Pods/.upgrade/upgrade.log</code>.
        </Alert>
      )}

      {phase === "timeout" && (
        <Alert kind="err">
          Still no answer from the controller after the swap. If it stays
          down, check <code>Pods/.upgrade/upgrade.log</code> on the host; the
          helper rolls back automatically on a failed health check.
        </Alert>
      )}

      {(phase === "idle" || phase === "checking") && (
        <div className="preview-row" style={{ marginTop: "var(--sp-3)" }}>
          {status.available && (
            <button
              className="btn btn--primary btn--sm"
              disabled={status.busy}
              onClick={upgrade}
            >
              Upgrade to v{status.latest}
            </button>
          )}
          <button
            className={
              "btn btn--ghost btn--sm" +
              (phase === "checking" ? " btn--loading" : "")
            }
            disabled={phase === "checking"}
            onClick={check}
          >
            Check for updates
          </button>
        </div>
      )}
    </>
  );
}
