// Magic Stack wizard: a step-by-step flow that collects the stack's few real
// inputs, live-validates each one as the user leaves its step, then runs the
// deploy+wire saga with a polled step list. Greenfield-only in v1 — ineligible
// stacks never get this far (the card is disabled with a tooltip).
import { useEffect, useRef, useState } from "react";
import type {
  MagicStack,
  ProviderAccount,
  StackCheck,
  StackRun,
} from "../types";
import { api } from "../api";
import { Alert } from "./Alert";
import { Field, Toggle } from "./Form";
import { FolderBrowser } from "./FolderBrowser";
import { CheckIcon, SpinnerIcon } from "./Icons";

type Checks = { media: StackCheck; indexer: StackCheck; usenet: StackCheck };
type StepId = "media" | "downloader" | "indexer" | "usenet" | "review";

// Display names for the downloader choice (keys are the pod/service ids).
const DL_LABELS: Record<string, string> = {
  nzbget: "NZBGet",
  sabnzbd: "SABnzbd",
};

const STEP_LABELS: Record<StepId, string> = {
  media: "Media",
  downloader: "Downloader",
  indexer: "Indexer",
  usenet: "Usenet",
  review: "Review",
};

// Steps that run a live check when the user clicks Next.
const VALIDATABLE = new Set<StepId>(["media", "indexer", "usenet"]);

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
  // The downloader is a choice only when the stack offers more than one.
  const [downloader, setDownloader] = useState(stack.downloaders[0] ?? "nzbget");
  const [idxUrl, setIdxUrl] = useState("");
  const [idxKey, setIdxKey] = useState("");
  const [idxSave, setIdxSave] = useState(true);
  const [nHost, setNHost] = useState("");
  const [nPort, setNPort] = useState("563");
  const [nSsl, setNSsl] = useState(true);
  const [nUser, setNUser] = useState("");
  const [nPass, setNPass] = useState("");
  const [nSave, setNSave] = useState(true);
  // Saved accounts (Settings → Accounts) fill a slot instead of asking
  // again: "" = enter details, otherwise the saved account's id. The
  // secret never reaches the browser — the server resolves the id.
  const [accounts, setAccounts] = useState<ProviderAccount[]>([]);
  const [idxSel, setIdxSel] = useState("");
  const [useSel, setUseSel] = useState("");
  const [busy, setBusy] = useState(false);
  // Per-component check results, filled as each step is validated on Next.
  const [checks, setChecks] = useState<Partial<Checks>>({});
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

  // The ordered steps for THIS stack: the downloader step only exists when
  // there's a choice to make. (Media/indexer/usenet are always asked in v1.)
  const steps: StepId[] = [
    "media",
    ...(stack.downloaders.length > 1 ? (["downloader"] as StepId[]) : []),
    "indexer",
    "usenet",
    "review",
  ];
  const [stepIdx, setStepIdx] = useState(0);
  const stepId = steps[Math.min(stepIdx, steps.length - 1)];

  // Preselect the first saved account of each kind — the point of the
  // vault is that a re-run asks for nothing.
  useEffect(() => {
    api
      .accounts()
      .then((s) => {
        if (!live.current) return;
        setAccounts(s.accounts);
        const idx = s.accounts.find((a) => a.kind === "newznab");
        const use = s.accounts.find((a) => a.kind === "usenet");
        if (idx) setIdxSel(idx.id);
        if (use) setUseSel(use.id);
      })
      .catch(() => {
        /* vault empty or unreachable — the form still works */
      });
  }, []);

  const idxAccounts = accounts.filter((a) => a.kind === "newznab");
  const useAccounts = accounts.filter((a) => a.kind === "usenet");

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
    downloader,
    media,
    indexer: idxSel
      ? { account: idxSel }
      : { url: idxUrl, key: idxKey, save: idxSave },
    usenet: useSel
      ? { account: useSel }
      : {
          host: nHost,
          port: nPort,
          ssl: nSsl,
          user: nUser,
          password: nPass,
          save: nSave,
        },
  });

  // Any edit invalidates a previous green state — a run only ever starts
  // from freshly validated inputs.
  const touch =
    <T,>(set: (v: T) => void) =>
    (v: T) => {
      set(v);
      setChecks({});
      setError("");
    };

  // Whether the current step has enough filled in to advance.
  const filledFor = (id: StepId): boolean => {
    switch (id) {
      case "media":
        return !!media;
      case "downloader":
        return true;
      case "indexer":
        return !!(idxSel || (idxUrl && idxKey));
      case "usenet":
        return !!(useSel || (nHost && nPort && nUser && nPass));
      case "review":
        return true;
    }
  };

  function goBack() {
    setError("");
    setStepIdx((i) => Math.max(i - 1, 0));
  }

  async function goNext() {
    if (stepId === "review") return install();
    if (VALIDATABLE.has(stepId) && !checks[stepId as keyof Checks]?.ok) {
      setBusy(true);
      setError("");
      try {
        const r = await api.stackValidate(body(), stepId as keyof Checks);
        const c = r.checks[stepId as keyof Checks];
        if (!live.current) return;
        setChecks((prev) => ({ ...prev, [stepId]: c }));
        if (!c.ok) {
          setError(c.error ?? "That didn't check out — fix it and try again.");
          return;
        }
      } catch (e) {
        setError(String(e));
        return;
      } finally {
        setBusy(false);
      }
    }
    setStepIdx((i) => Math.min(i + 1, steps.length - 1));
    setError("");
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

  const idxLabel = idxSel
    ? (idxAccounts.find((a) => a.id === idxSel)?.label ?? "saved account")
    : idxUrl || "—";
  const useLabel = useSel
    ? (useAccounts.find((a) => a.id === useSel)?.label ?? "saved account")
    : nHost || "—";

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
            {/* Step rail */}
            <div className="wiz-rail">
              {steps.map((s, i) => (
                <div
                  key={s}
                  className={
                    "wiz-rail__step" +
                    (i === stepIdx ? " wiz-rail__step--active" : "") +
                    (i < stepIdx ? " wiz-rail__step--done" : "")
                  }
                >
                  <span className="wiz-rail__num">
                    {i < stepIdx ? (
                      <CheckIcon style={{ width: 12, height: 12 }} />
                    ) : (
                      i + 1
                    )}
                  </span>
                  {STEP_LABELS[s]}
                </div>
              ))}
            </div>

            {stepId === "media" && (
              <>
                <h3 className="wiz-step__head">Where media lives</h3>
                <p className="wiz-step__hint">
                  Downloads, TV and movies all live under this one folder so
                  the apps can import in place.
                </p>
                <Field
                  label="Media folder on this server"
                  error={checks.media?.error ?? undefined}
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
              </>
            )}

            {stepId === "downloader" && (
              <>
                <h3 className="wiz-step__head">Your downloader</h3>
                <p className="wiz-step__hint">
                  Both do the same job — pick the one you know, or NZBGet if
                  you're unsure.
                </p>
                <div className="preview-row">
                  {stack.downloaders.map((d) => (
                    <button
                      key={d}
                      type="button"
                      className={
                        "btn btn--sm " +
                        (downloader === d ? "btn--primary" : "btn--ghost")
                      }
                      onClick={() => touch(setDownloader)(d)}
                    >
                      {DL_LABELS[d] ?? d}
                    </button>
                  ))}
                </div>
              </>
            )}

            {stepId === "indexer" && (
              <>
                <h3 className="wiz-step__head">Your indexer</h3>
                <p className="wiz-step__hint">
                  The newznab site you search with.
                </p>
                {idxAccounts.length > 0 && (
                  <Field
                    label="Indexer"
                    hint="Saved under Settings → Accounts."
                    error={idxSel ? (checks.indexer?.error ?? undefined) : undefined}
                  >
                    <select
                      className="input"
                      value={idxSel}
                      onChange={(e) => touch(setIdxSel)(e.target.value)}
                    >
                      {idxAccounts.map((a) => (
                        <option key={a.id} value={a.id}>
                          {a.label} — {a.detail}
                        </option>
                      ))}
                      <option value="">Enter details…</option>
                    </select>
                  </Field>
                )}
                {!idxSel && (
                  <>
                    <Field
                      label="Indexer URL"
                      hint="e.g. https://api.nzbgeek.info"
                      error={checks.indexer?.error ?? undefined}
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
                    <Toggle checked={idxSave} onChange={setIdxSave}>
                      Save to Accounts for next time
                    </Toggle>
                  </>
                )}
              </>
            )}

            {stepId === "usenet" && (
              <>
                <h3 className="wiz-step__head">Your usenet account</h3>
                <p className="wiz-step__hint">
                  The news server your downloader connects to.
                </p>
                {useAccounts.length > 0 && (
                  <Field
                    label="Usenet account"
                    hint="Saved under Settings → Accounts."
                    error={useSel ? (checks.usenet?.error ?? undefined) : undefined}
                  >
                    <select
                      className="input"
                      value={useSel}
                      onChange={(e) => touch(setUseSel)(e.target.value)}
                    >
                      {useAccounts.map((a) => (
                        <option key={a.id} value={a.id}>
                          {a.label} — {a.detail}
                        </option>
                      ))}
                      <option value="">Enter details…</option>
                    </select>
                  </Field>
                )}
                {!useSel && (
                  <>
                    <Field
                      label="News server"
                      hint="From your usenet provider (e.g. news.eweka.nl)."
                      error={checks.usenet?.error ?? undefined}
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
                    <Toggle checked={nSave} onChange={setNSave}>
                      Save to Accounts for next time
                    </Toggle>
                  </>
                )}
              </>
            )}

            {stepId === "review" && (
              <>
                <h3 className="wiz-step__head">Review</h3>
                <p className="wiz-step__hint">
                  {stack.blurb} Everything below checked out — deploy when
                  you're ready.
                </p>
                <div className="wiz-review">
                  <div className="wiz-review__row">
                    <span className="wiz-review__k">Media folder</span>
                    <span className="wiz-review__v">{media}</span>
                  </div>
                  {stack.downloaders.length > 1 && (
                    <div className="wiz-review__row">
                      <span className="wiz-review__k">Downloader</span>
                      <span className="wiz-review__v">
                        {DL_LABELS[downloader] ?? downloader}
                      </span>
                    </div>
                  )}
                  <div className="wiz-review__row">
                    <span className="wiz-review__k">Indexer</span>
                    <span className="wiz-review__v">{idxLabel}</span>
                  </div>
                  <div className="wiz-review__row">
                    <span className="wiz-review__k">Usenet</span>
                    <span className="wiz-review__v">{useLabel}</span>
                  </div>
                </div>
              </>
            )}

            {error && (
              <div style={{ marginTop: "var(--sp-4)" }}>
                <Alert kind="err">{error}</Alert>
              </div>
            )}

            <div
              className="preview-row"
              style={{
                marginTop: "var(--sp-6)",
                justifyContent: "space-between",
              }}
            >
              <button
                className="btn btn--ghost"
                disabled={busy || stepIdx === 0}
                onClick={goBack}
              >
                Back
              </button>
              <button
                className="btn btn--primary"
                disabled={busy || !filledFor(stepId)}
                onClick={goNext}
                title={
                  filledFor(stepId) ? undefined : "Fill in this step first"
                }
              >
                {busy && <SpinnerIcon className="btn-icon" />}
                {stepId === "review" ? "Set up the stack" : "Next"}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
