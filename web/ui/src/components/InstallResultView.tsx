import { useState } from "react";
import { Link } from "react-router-dom";
import type { InstallResult } from "../types";
import { api } from "../api";
import { Alert } from "./Alert";
import { SpinnerIcon } from "./Icons";

export function InstallResultView({
  name,
  result,
  onReset,
}: {
  name: string;
  result: InstallResult;
  onReset: () => void;
}) {
  const [starting, setStarting] = useState(false);
  const [startOut, setStartOut] = useState<string | null>(null);

  async function start() {
    setStarting(true);
    try {
      const r = await api.action(name, "start");
      setStartOut(r.output || `start ${name}: ${r.status}`);
    } finally {
      setStarting(false);
    }
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

  return (
    <>
      <h1 className="page-title">Installed {name}</h1>
      <Alert kind="ok">
        Pod generated. It isn’t running until started — starting pulls the image and
        enrolls on the tailnet, which can take a few minutes.
      </Alert>

      {result.output && (
        <div className="log" style={{ marginTop: "var(--sp-4)", maxWidth: 720 }}>
          <div className="log__body">{result.output}</div>
        </div>
      )}

      {startOut !== null && (
        <div className="log" style={{ marginTop: "var(--sp-4)", maxWidth: 720 }}>
          <div className="log__bar">
            <span className="log__dot" />
            <span className="log__name">start {name}</span>
          </div>
          <div className="log__body">{startOut}</div>
        </div>
      )}

      <div className="preview-row" style={{ marginTop: "var(--sp-5)" }}>
        <button
          className={"btn btn--primary" + (starting ? " btn--loading" : "")}
          disabled={starting || startOut !== null}
          onClick={start}
        >
          {starting && <SpinnerIcon className="btn-icon" />}
          Start {name} now
        </button>
        <Link className="btn btn--ghost" to="/">
          Dashboard
        </Link>
      </div>
    </>
  );
}
