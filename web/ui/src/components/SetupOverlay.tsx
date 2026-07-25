import { useEffect, useState } from "react";
import { api } from "../api";
import type { SetupStatus, SetupStep } from "../types";
import { Alert } from "./Alert";
import { CheckIcon, SpinnerIcon, TailarrMark } from "./Icons";

// Full-screen blocking status for the first-run setup saga. On a fresh
// install the controller brings up the Configurator, notifications, and the
// peer relay — pods deploy and sidecars enroll, which takes long enough that
// a blank dashboard reads as "broken". This overlay owns the screen until the
// run finishes (or fails, where the admin can retry / skip). Existing installs
// report `done`/`unknown` and never see it.

const POLL_MS = 2000;

function StepDot({ state }: { state: SetupStep["state"] }) {
  if (state === "ok")
    return (
      <span className="upgrade-step__dot" style={{ color: "var(--ok)" }}>
        <CheckIcon style={{ width: 16, height: 16 }} />
      </span>
    );
  if (state === "running")
    return (
      <span className="upgrade-step__dot">
        <SpinnerIcon className="btn-icon" style={{ width: 16, height: 16 }} />
      </span>
    );
  if (state === "warn" || state === "failed")
    return (
      <span
        className="upgrade-step__dot"
        style={{
          color: state === "warn" ? "var(--warn, #f5a623)" : "var(--err)",
          fontWeight: 700,
        }}
      >
        !
      </span>
    );
  return (
    <span className="upgrade-step__dot">
      <span className="upgrade-step__idle" />
    </span>
  );
}

export function SetupOverlay() {
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [dismissed, setDismissed] = useState(false);
  const [busy, setBusy] = useState(false);
  // Bumped by "Try again" to restart polling after a re-run kicks off.
  const [pollKey, setPollKey] = useState(0);

  useEffect(() => {
    let alive = true;
    let id: number | undefined;
    const stop = () => {
      if (id) window.clearInterval(id);
      id = undefined;
    };
    const poll = async () => {
      try {
        const s = await api.setup();
        if (!alive) return;
        setStatus(s);
        // Terminal states don't change on their own — stop the timer so an
        // established install (always "done") isn't polled forever. "unknown"
        // is the brief pre-marker window on a fresh boot, so keep polling.
        if (s.state === "done" || s.state === "failed") stop();
      } catch {
        // controller unreachable (e.g. mid-restart) — keep the last state
      }
    };
    poll();
    id = window.setInterval(poll, POLL_MS);
    return () => {
      alive = false;
      stop();
    };
  }, [pollKey]);

  if (!status || dismissed) return null;

  const { state, steps } = status;
  const warns = steps.filter((s) => s.state === "warn");
  const running = state === "pending" || state === "running";
  const failed = state === "failed";
  // Done with warnings: keep the panel up so the admin sees the relay note
  // (and its host command) once, then dismisses. Clean done => nothing.
  const doneWithWarn = state === "done" && warns.length > 0;
  if (!running && !failed && !doneWithWarn) return null;

  const retry = async () => {
    setBusy(true);
    try {
      await api.setupAction("retry");
      setStatus(await api.setup());
      setPollKey((k) => k + 1); // resume polling the re-run to done
    } finally {
      setBusy(false);
    }
  };
  const skip = async () => {
    setBusy(true);
    try {
      await api.setupAction("skip");
    } finally {
      setBusy(false);
      setDismissed(true);
    }
  };

  return (
    <div className={"upgrade-overlay" + (running ? " upgrade-overlay--live" : "")}>
      <div className="upgrade-overlay__box">
        <TailarrMark className="upgrade-overlay__mark" />
        <h2 className="upgrade-overlay__title">
          {failed
            ? "Setup needs attention"
            : doneWithWarn
              ? "Almost there"
              : "Finishing installation"}
        </h2>
        <p className="upgrade-overlay__sub">
          {failed
            ? "One step didn't complete. You can try again or skip it for now."
            : doneWithWarn
              ? "Your services are up. One optional item needs a hand:"
              : "Setting up notifications and networking. This runs once — hang tight."}
        </p>

        <ul className="upgrade-steps">
          {steps.map((s) => (
            <li
              key={s.key}
              className={
                "upgrade-step" +
                (s.state === "running" ? " upgrade-step--active" : "") +
                (s.state === "ok" ? " upgrade-step--done" : "")
              }
            >
              <StepDot state={s.state} />
              <span>
                {s.label}
                {(s.state === "warn" || s.state === "failed") && s.detail ? (
                  <span className="setup-step__detail">{s.detail}</span>
                ) : null}
              </span>
            </li>
          ))}
        </ul>

        {failed && status.error ? (
          <div style={{ marginTop: "var(--sp-4)", textAlign: "left" }}>
            <Alert kind="err">{status.error}</Alert>
          </div>
        ) : null}

        {(failed || doneWithWarn) && (
          <div className="setup-actions">
            {failed && (
              <button className="btn btn--sm" disabled={busy} onClick={retry}>
                Try again
              </button>
            )}
            <button
              className={"btn btn--sm" + (failed ? " btn--ghost" : "")}
              disabled={busy}
              onClick={skip}
            >
              {failed ? "Skip for now" : "Get started"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
