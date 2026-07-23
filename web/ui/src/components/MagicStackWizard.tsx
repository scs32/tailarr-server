// Magic Stack wizard: collect the stack's few real inputs, live-validate
// them against the outside world, then run the deploy+wire saga with a
// polled step list. Greenfield-only in v1 — ineligible stacks never get
// this far (the card is disabled with a tooltip).
import { useEffect, useRef, useState } from "react";
import type { MagicStack, StackCheck, StackRun } from "../types";
import { api } from "../api";
import { Alert } from "./Alert";
import { Field, FormSection, Toggle } from "./Form";
import { FolderBrowser } from "./FolderBrowser";
import { CheckIcon, SpinnerIcon } from "./Icons";

type Checks = { media: StackCheck; indexer: StackCheck; usenet: StackCheck };

export function MagicStackWizard({
  stack,
  initialRun,
  onClose,
  onChanged,
}: {
  stack: MagicStack;
  initialRun: StackRun | null;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [media, setMedia] = useState("");
  const [idxUrl, setIdxUrl] = useState("");
  const [idxKey, setIdxKey] = useState("");
  const [nHost, setNHost] = useState("");
  const [nPort, setNPort] = useState("563");
  const [nSsl, setNSsl] = useState(true);
  const [nUser, setNUser] = useState("");
  const [nPass, setNPass] = useState("");
  const [busy, setBusy] = useState(false);
  const [checks, setChecks] = useState<Checks | null>(null);
  const [error, setError] = useState("");
  const [run, setRun] = useState<StackRun | null>(
    initialRun && initialRun.stack === stack.key ? initialRun : null,
  );
  const live = useRef(true);
  useEffect(
    () => () => {
      live.current = false;
    },
    [],
  );

  // While a run is active, poll the step list; on any terminal state let
  // the page behind refresh (new pods exist now).
  const running = run?.state === "running";
  useEffect(() => {
    if (!running) return;
    const t = setInterval(async () => {
      try {
        const s = await api.stacks();
        if (!live.current) return;
        setRun(s.run);
        if (s.run && s.run.state !== "running") onChanged();
      } catch {
        /* transient */
      }
    }, 3000);
    return () => clearInterval(t);
  }, [running, onChanged]);

  const body = () => ({
    stack: stack.key,
    media,
    indexer: { url: idxUrl, key: idxKey },
    usenet: {
      host: nHost,
      port: nPort,
      ssl: nSsl,
      user: nUser,
      password: nPass,
    },
  });

  // Any edit invalidates a previous green state — install only ever runs
  // straight off a validated form.
  const touch = <T,>(set: (v: T) => void) => (v: T) => {
    set(v);
    setChecks(null);
  };

  async function validate() {
    setBusy(true);
    setError("");
    try {
      const r = await api.stackValidate(body());
      setChecks(r.checks);
      if (!r.ok) setError(r.error ?? "Fix the failing checks.");
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function install() {
    setBusy(true);
    setError("");
    try {
      const r = await api.stackInstall(body());
      if (!r.ok) {
        if (r.checks) setChecks(r.checks as Checks);
        setError(r.error ?? "Setup could not start.");
        return;
      }
      const s = await api.stacks();
      setRun(
        s.run ?? {
          stack: stack.key,
          state: "running",
          started: 0,
          finished: null,
          error: null,
          steps: [],
        },
      );
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  const filled =
    media && idxUrl && idxKey && nHost && nPort && nUser && nPass;
  const allGreen =
    !!checks && checks.media.ok && checks.indexer.ok && checks.usenet.ok;

  return (
    <div className="scrim" onClick={busy || running ? undefined : onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal__head">
          <span className="modal__title">{stack.name}</span>
          <div className="spacer" />
          <button className="btn btn--ghost btn--sm" onClick={onClose}>
            Close
          </button>
        </div>

        {run ? (
          <>
            <p style={{ color: "var(--muted)", margin: "0 0 var(--sp-4)" }}>
              {run.state === "running"
                ? "Setting everything up — this takes a few minutes."
                : run.state === "done"
                  ? "All wired up. Search for something and watch it land."
                  : "Setup stopped — nothing after the failed step ran."}
            </p>
            <div className="steps">
              {run.steps.map((s) => (
                <div
                  key={s.key}
                  className={
                    "step step--" +
                    (s.state === "ok"
                      ? "done"
                      : s.state === "running"
                        ? "active"
                        : s.state === "failed"
                          ? "failed"
                          : "pending")
                  }
                >
                  <span className="step__dot">
                    {s.state === "ok" ? (
                      <CheckIcon />
                    ) : s.state === "running" ? (
                      <SpinnerIcon />
                    ) : s.state === "failed" ? (
                      <>✕</>
                    ) : null}
                  </span>
                  <span className="step__label">
                    {s.label}
                    {s.detail && <span className="step__sub"> — {s.detail}</span>}
                  </span>
                </div>
              ))}
            </div>
            {run.state === "failed" && (
              <div style={{ marginTop: "var(--sp-4)" }}>
                <Alert kind="err">{run.error ?? "Setup failed."}</Alert>
              </div>
            )}
            {run.state !== "running" && (
              <div className="preview-row" style={{ marginTop: "var(--sp-5)" }}>
                <button className="btn btn--primary" onClick={onClose}>
                  Done
                </button>
              </div>
            )}
          </>
        ) : (
          <>
            <p style={{ color: "var(--muted)", margin: "0 0 var(--sp-2)" }}>
              {stack.blurb} Tailarr wires everything together — these are
              the only things it can't know.
            </p>

            <FormSection title="Where media lives">
              <Field
                label="Media folder on this server"
                hint="Downloads, TV and movies all live under this folder."
                error={checks?.media.error ?? undefined}
              >
                <div className="folder-row">
                  <input
                    className="input"
                    value={media}
                    placeholder="browse for a host folder…"
                    aria-label="Media folder"
                    readOnly
                    title="Use the folder button to browse the host"
                  />
                  <FolderBrowser value={media} onPick={touch(setMedia)} />
                </div>
              </Field>
            </FormSection>

            <FormSection title="Your indexer">
              <Field
                label="Indexer URL"
                hint="The newznab site you search with (e.g. https://api.nzbgeek.info)."
                error={checks?.indexer.error ?? undefined}
              >
                <input
                  className="input"
                  value={idxUrl}
                  placeholder="https://…"
                  onChange={(e) => touch(setIdxUrl)(e.target.value)}
                />
              </Field>
              <Field label="Indexer API key">
                <input
                  className="input"
                  value={idxKey}
                  onChange={(e) => touch(setIdxKey)(e.target.value)}
                />
              </Field>
            </FormSection>

            <FormSection title="Your usenet account">
              <Field
                label="News server"
                hint="From your usenet provider (e.g. news.eweka.nl)."
                error={checks?.usenet.error ?? undefined}
              >
                <input
                  className="input"
                  value={nHost}
                  onChange={(e) => touch(setNHost)(e.target.value)}
                />
              </Field>
              <div className="folder-row">
                <Field label="Port">
                  <input
                    className="input"
                    style={{ width: 90 }}
                    value={nPort}
                    onChange={(e) => touch(setNPort)(e.target.value)}
                  />
                </Field>
                <Toggle
                  checked={nSsl}
                  onChange={(v) => {
                    touch(setNSsl)(v);
                    setNPort(v ? "563" : "119");
                  }}
                >
                  SSL
                </Toggle>
              </div>
              <Field label="Username">
                <input
                  className="input"
                  value={nUser}
                  onChange={(e) => touch(setNUser)(e.target.value)}
                />
              </Field>
              <Field label="Password">
                <input
                  className="input"
                  type="password"
                  value={nPass}
                  onChange={(e) => touch(setNPass)(e.target.value)}
                />
              </Field>
            </FormSection>

            {checks && (
              <div className="stack-checks">
                {(
                  [
                    ["media", "Media folder"],
                    ["indexer", "Indexer"],
                    ["usenet", "Usenet account"],
                  ] as const
                ).map(([k, label]) => (
                  <div
                    key={k}
                    className={
                      "check-row " +
                      (checks[k].ok ? "check-row--ok" : "check-row--err")
                    }
                  >
                    {checks[k].ok ? "✓" : "✕"} {label}
                    {!checks[k].ok && checks[k].error
                      ? ` — ${checks[k].error}`
                      : ""}
                  </div>
                ))}
              </div>
            )}
            {error && (
              <div style={{ marginTop: "var(--sp-3)" }}>
                <Alert kind="err">{error}</Alert>
              </div>
            )}

            <div className="preview-row" style={{ marginTop: "var(--sp-5)" }}>
              {allGreen ? (
                <button
                  className="btn btn--primary"
                  disabled={busy}
                  onClick={install}
                >
                  {busy && <SpinnerIcon className="btn-icon" />}
                  Set up the stack
                </button>
              ) : (
                <button
                  className="btn btn--primary"
                  disabled={!filled || busy}
                  title={
                    filled
                      ? "Check the indexer and usenet account before deploying"
                      : "Fill in every field first"
                  }
                  onClick={validate}
                >
                  {busy && <SpinnerIcon className="btn-icon" />}
                  Validate
                </button>
              )}
              <button
                className="btn btn--ghost"
                disabled={busy}
                onClick={onClose}
              >
                Cancel
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
