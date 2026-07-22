import { useCallback, useEffect, useRef, useState } from "react";
import type { UpgradeStatus } from "../types";
import { api } from "../api";
import { Alert } from "./Alert";
import { ConfirmDialog } from "./ConfirmDialog";
import { UpgradeOverlay, type UpgradeStage } from "./UpgradeOverlay";

// How long to wait for the new controller before advising a manual look.
// The helper sleeps ~3s, swaps the container, and health-checks for up to
// 60s before rolling back — 150s covers the full cycle plus image start.
const UPGRADE_WAIT_MS = 150_000;
const POLL_MS = 3_000;

// Set before the post-upgrade reload so the fresh page (new SPA bundle)
// can confirm the new version. The fleet rerender happens automatically
// on the new controller's first start (warned up front in the confirm
// dialog) — there is no "Finish upgrade" step. Session-scoped on purpose.
const UPGRADED_FROM_KEY = "tailarr.upgraded_from";

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
  const [stage, setStage] = useState<UpgradeStage>("pull");
  const [error, setError] = useState("");
  const [newVersion, setNewVersion] = useState("");
  const timers = useRef<number[]>([]);
  const polling = useRef(false);

  const refresh = useCallback(() => {
    api
      .upgradeStatus()
      .then((s) => {
        setStatus(s);
        // A reloaded page mid-upgrade should resume waiting, not sit idle
        // — including restarting the poll (the old code set the phase but
        // never resumed polling, leaving the page stuck "upgrading").
        if (s.busy) {
          setPhase("upgrading");
          setStage("wait");
          waitForNewController(s.current);
        }
      })
      .catch(() => setStatus(null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    // The page that loads AFTER a successful swap picks up the handoff
    // and confirms the new version (pods refresh automatically).
    const from = sessionStorage.getItem(UPGRADED_FROM_KEY);
    if (from) {
      sessionStorage.removeItem(UPGRADED_FROM_KEY);
      api
        .info()
        .then((i) => {
          if (i.version !== from) {
            setNewVersion(i.version);
            setPhase("done");
          }
        })
        .catch(() => {});
    }
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
    if (polling.current) return; // refresh() + upgrade() must not double-poll
    polling.current = true;
    const deadline = Date.now() + UPGRADE_WAIT_MS;
    const poll = async () => {
      try {
        const info = await api.info();
        if (info.version !== from) {
          // Success — reload so the page runs the NEW version's SPA bundle
          // (this code is still the old one). The fresh page reads the
          // handoff key and confirms the upgrade.
          sessionStorage.setItem(UPGRADED_FROM_KEY, from);
          window.location.reload();
          return;
        }
        // Same version answering again: either the swap hasn't landed yet,
        // or the helper rolled back — its result file knows which.
        setStage("wait");
        const s = await api.upgradeStatus().catch(() => null);
        if (s && !s.busy && s.last && s.last.rolled_back) {
          setStatus(s);
          setPhase("rolledback");
          polling.current = false;
          return;
        }
      } catch {
        // Connection refused while the controller swaps — keep waiting.
        setStage("swap");
      }
      if (Date.now() > deadline) {
        setPhase("timeout");
        polling.current = false;
        return;
      }
      timers.current.push(window.setTimeout(poll, POLL_MS));
    };
    timers.current.push(window.setTimeout(poll, POLL_MS));
  }

  async function upgrade() {
    if (!status) return;
    setPhase("upgrading");
    // (reached only via the confirm dialog — it owns the restart warning)
    setStage("pull"); // the POST pulls the image before detaching the helper
    setError("");
    const r = await api.upgrade();
    if (!r.ok) {
      setError(r.error || r.output || "Upgrade failed to start.");
      setPhase("idle");
      refresh();
      return;
    }
    setStage("swap");
    waitForNewController(status.current);
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
        <UpgradeOverlay
          stage={stage}
          from={status.current}
          to={status.latest}
        />
      )}

      {phase === "done" && (
        <Alert kind="ok">
          Upgraded to <strong>v{newVersion}</strong>. Your pods are being
          refreshed to the new engine automatically — running pods restart
          briefly (watch the Dashboard); stopped pods stay stopped.
        </Alert>
      )}

      {phase === "confirm" && status.latest && (
        <ConfirmDialog
          title={`Upgrade to v${status.latest}?`}
          confirmLabel="Upgrade"
          onConfirm={upgrade}
          onCancel={() => setPhase("idle")}
        >
          <p>
            The controller replaces itself, and afterwards{" "}
            <strong>every running pod restarts briefly</strong> while its
            scripts are refreshed to the new engine. Stopped pods get fresh
            scripts but stay stopped. Pod images are not changed — use each
            pod’s Update button for that.
          </p>
        </ConfirmDialog>
      )}

      {phase === "rolledback" && (
        <UpgradeOverlay
          stage="wait"
          from={status.current}
          to={status.latest}
          failure={
            <>
              The new controller failed its health check — the upgrade was
              rolled back and v{status.current} is still running. See{" "}
              <code>Pods/.upgrade/upgrade.log</code>.
            </>
          }
          onDismiss={() => setPhase("idle")}
        />
      )}

      {phase === "timeout" && (
        <UpgradeOverlay
          stage="wait"
          from={status.current}
          to={status.latest}
          failure={
            <>
              Still no answer from the controller after the swap. If it stays
              down, check <code>Pods/.upgrade/upgrade.log</code> on the host;
              the helper rolls back automatically on a failed health check.
            </>
          }
          onDismiss={() => setPhase("idle")}
        />
      )}

      {(phase === "idle" || phase === "checking") && (
        <div className="preview-row" style={{ marginTop: "var(--sp-3)" }}>
          {status.available && (
            <button
              className="btn btn--primary btn--sm"
              disabled={status.busy}
              onClick={() => setPhase("confirm")}
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
