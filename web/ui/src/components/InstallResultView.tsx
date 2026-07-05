import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import type { InstallResult, NetworkEntry } from "../types";
import { api } from "../api";
import { dnsUrl } from "../lib/urls";
import { Alert } from "./Alert";
import { CheckIcon, SpinnerIcon } from "./Icons";

// After the pod is generated, "Start now" drives a live, staged sequence
// instead of a blind spinner: the pod's real state is polled while the
// start runs, then tailnet enrollment is tracked until the service URL is
// ready to open. No step is marked done until the server says so.

type Phase =
  | "idle" // generated, not started
  | "starting" // start POST in flight; polling pod state
  | "enrolling" // running; waiting for the sidecar's MagicDNS name
  | "ready" // reachable URL known
  | "running" // running, but no tailnet URL applies / enrollment timed out
  | "failed";

const ENROLL_CAP_MS = 90_000;

// Uses the design system's install stepper (.step/.step__dot — the spin
// animation lives on .step--active .step__dot svg).
function Step({
  state,
  children,
}: {
  state: "pending" | "active" | "done";
  children: React.ReactNode;
}) {
  return (
    <div className={`step step--${state}`}>
      <span className="step__dot">
        {state === "done" ? <CheckIcon /> : state === "active" ? <SpinnerIcon /> : null}
      </span>
      <span className="step__label" style={{ alignSelf: "center" }}>
        {children}
      </span>
    </div>
  );
}

export function InstallResultView({
  name,
  result,
  onReset,
}: {
  name: string;
  result: InstallResult;
  onReset: () => void;
}) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [output, setOutput] = useState<string | null>(null);
  const [entry, setEntry] = useState<NetworkEntry | null>(null);
  const live = useRef(true);

  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  async function start() {
    setPhase("starting");
    setOutput(null);

    // Poll the pod's real state while the start POST runs.
    const statePoll = setInterval(async () => {
      try {
        const pods = await api.pods();
        const p = pods.find((x) => x.name === name);
        if (live.current && p?.state === "running") setPhase("enrolling");
      } catch {
        /* transient; keep polling */
      }
    }, 3000);

    let r;
    try {
      r = await api.action(name, "start");
    } finally {
      clearInterval(statePoll);
    }
    if (!live.current) return;
    setOutput(r.output || `start ${name}: ${r.status}`);
    if (!r.ok) {
      setPhase("failed");
      return;
    }

    // Started. Track tailnet enrollment until a reachable URL exists.
    setPhase("enrolling");
    const deadline = Date.now() + ENROLL_CAP_MS;
    while (live.current && Date.now() < deadline) {
      try {
        const net = await api.network();
        const e = net.find((x) => x.name === name);
        if (e?.dns_name) {
          setEntry(e);
          setPhase("ready");
          return;
        }
      } catch {
        /* transient; keep polling */
      }
      await new Promise((res) => setTimeout(res, 5000));
    }
    if (live.current) setPhase("running"); // up, but enrollment is slow — Network tab has it
  }

  if (!result.ok) {
    return (
      <>
        <h1 className="page-title">Install {name}: failed</h1>
        <Alert kind="err">{result.error ?? "create.sh failed — see output below."}</Alert>
        {result.output && (
          <div className="log" style={{ marginTop: "var(--sp-4)", maxWidth: 720 }}>
            <div className="log__body">{result.output}</div>
          </div>
        )}
        <div className="preview-row" style={{ marginTop: "var(--sp-5)" }}>
          <button className="btn btn--secondary" onClick={onReset}>
            Back to form
          </button>
          <Link className="btn btn--ghost" to="/catalog">
            Catalog
          </Link>
        </div>
      </>
    );
  }

  const started = phase !== "idle";
  const settled = phase === "ready" || phase === "running" || phase === "failed";

  return (
    <>
      <h1 className="page-title">Installed {name}</h1>

      {phase === "idle" && (
        <Alert kind="ok">
          Pod generated. Start it now — the first start pulls the image and
          enrolls on the tailnet, and this page will track it live.
        </Alert>
      )}
      {phase === "failed" && (
        <Alert kind="err">Start failed — see the output below.</Alert>
      )}
      {phase === "ready" && entry && (
        <Alert kind="ok">
          {name} is up and reachable at{" "}
          <a href={dnsUrl(entry)} target="_blank" rel="noopener noreferrer">
            {entry.dns_name}
          </a>
          .
        </Alert>
      )}
      {phase === "running" && (
        <Alert kind="ok">
          {name} is running. Tailnet enrollment is still settling — the Network
          tab will show its URL shortly.
        </Alert>
      )}

      {started && phase !== "failed" && (
        <div
          className="card steps"
          style={{ maxWidth: 480, marginTop: "var(--sp-4)", padding: "var(--sp-5) var(--sp-5) var(--sp-2)" }}
        >
          <Step state={phase === "starting" ? "active" : "done"}>
            Pull image &amp; start containers
          </Step>
          <Step
            state={
              phase === "starting"
                ? "pending"
                : phase === "enrolling"
                  ? "active"
                  : "done"
            }
          >
            Enroll on the tailnet
          </Step>
          <Step state={settled ? "done" : "pending"}>
            {phase === "ready" && entry ? (
              <>
                Ready —{" "}
                <a href={dnsUrl(entry)} target="_blank" rel="noopener noreferrer">
                  {dnsUrl(entry)}
                </a>
              </>
            ) : (
              "Service reachable"
            )}
          </Step>
        </div>
      )}

      {output !== null && (
        <div className="log" style={{ marginTop: "var(--sp-4)", maxWidth: 720 }}>
          <div className="log__bar">
            <span className="log__dot" />
            <span className="log__name">start {name}</span>
          </div>
          <div className="log__body">{output}</div>
        </div>
      )}

      <div className="preview-row" style={{ marginTop: "var(--sp-5)" }}>
        {phase === "idle" && (
          <button className="btn btn--primary" onClick={start}>
            Start {name} now
          </button>
        )}
        {phase === "ready" && entry && (
          <a
            className="btn btn--primary"
            href={dnsUrl(entry)}
            target="_blank"
            rel="noopener noreferrer"
          >
            Open {name}
          </a>
        )}
        {phase === "failed" && (
          <button className="btn btn--primary" onClick={start}>
            Retry start
          </button>
        )}
        <Link className="btn btn--ghost" to="/">
          Dashboard
        </Link>
      </div>
    </>
  );
}
