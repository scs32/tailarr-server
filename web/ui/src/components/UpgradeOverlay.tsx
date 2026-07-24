import type { ReactNode } from "react";
import { Alert } from "./Alert";
import { CheckIcon, SpinnerIcon, TailarrMark } from "./Icons";

// Full-screen blocking status for a controller self-upgrade. The controller
// container is replaced underneath this very page, so ordinary UI can't be
// trusted mid-swap — this overlay owns the screen from the moment the
// upgrade starts until the new version answers (then the page reloads to
// pick up the new SPA bundle) or the failure is surfaced.

export type UpgradeStage = "pull" | "swap" | "wait";

const STEPS: { key: UpgradeStage; label: string }[] = [
  { key: "pull", label: "Downloading the new release" },
  { key: "swap", label: "Updating Tailarr" },
  { key: "wait", label: "Health check + reconnect" },
];

function StepDot({ state }: { state: "done" | "active" | "todo" }) {
  if (state === "done")
    return (
      <span className="upgrade-step__dot">
        <CheckIcon style={{ width: 16, height: 16 }} />
      </span>
    );
  if (state === "active")
    return (
      <span className="upgrade-step__dot">
        <SpinnerIcon className="btn-icon" style={{ width: 16, height: 16 }} />
      </span>
    );
  return (
    <span className="upgrade-step__dot">
      <span className="upgrade-step__idle" />
    </span>
  );
}

export function UpgradeOverlay({
  stage,
  from,
  to,
  failure,
  onDismiss,
}: {
  stage: UpgradeStage;
  from: string;
  to: string;
  failure?: ReactNode; // set = the upgrade ended badly; overlay unblocks
  onDismiss?: () => void;
}) {
  const activeIdx = STEPS.findIndex((s) => s.key === stage);
  return (
    <div
      className={"upgrade-overlay" + (failure ? "" : " upgrade-overlay--live")}
    >
      <div className="upgrade-overlay__box">
        <TailarrMark className="upgrade-overlay__mark" />
        <h2 className="upgrade-overlay__title">
          {failure
            ? "Upgrade failed"
            : `Upgrading v${from} → v${to || "latest"}`}
        </h2>
        {failure ? (
          <>
            <div style={{ marginTop: "var(--sp-4)", textAlign: "left" }}>
              <Alert kind="err">{failure}</Alert>
            </div>
            <button
              className="btn btn--ghost btn--sm"
              style={{ marginTop: "var(--sp-4)" }}
              onClick={onDismiss}
            >
              Close
            </button>
          </>
        ) : (
          <>
            <p className="upgrade-overlay__sub">
              Hang tight — the controller restarts underneath this page. Your
              pods and their Tailscale identities are untouched.
            </p>
            <ul className="upgrade-steps">
              {STEPS.map((s, i) => {
                const state =
                  i < activeIdx ? "done" : i === activeIdx ? "active" : "todo";
                return (
                  <li key={s.key} className={`upgrade-step upgrade-step--${state}`}>
                    <StepDot state={state} />
                    {s.label}
                  </li>
                );
              })}
            </ul>
          </>
        )}
      </div>
    </div>
  );
}
