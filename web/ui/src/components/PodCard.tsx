import { useState } from "react";
import type { Pod } from "../types";
import { api } from "../api";
import { PodGlyph, SpinnerIcon } from "./Icons";

export function PodCard({
  pod,
  onChanged,
  onLogs,
}: {
  pod: Pod;
  onChanged: () => void;
  onLogs: (name: string) => void;
}) {
  const [busy, setBusy] = useState<"" | "start" | "stop" | "update">("");

  async function run(action: "start" | "stop" | "update") {
    setBusy(action);
    try {
      await api.action(pod.name, action);
      onChanged();
    } finally {
      setBusy("");
    }
  }

  const running = pod.state === "running";
  const badge = running ? "badge--running" : "badge--stopped";
  const label = running ? "Running" : "Stopped";

  return (
    <div className="pod-card card">
      <div className="pod-card__head">
        <div className="pod-icon">
          <PodGlyph />
        </div>
        <div>
          <div className="pod-card__title">{pod.name}</div>
          {pod.image && <div className="pod-card__url">{pod.image}</div>}
        </div>
        <div className="spacer" />
        <span className={`badge ${badge}`}>{label}</span>
      </div>

      <div className="pod-card__foot">
        {running ? (
          <button
            className={"btn btn--secondary btn--sm" + (busy ? " btn--loading" : "")}
            disabled={!!busy || pod.controller}
            title={pod.controller ? "The controller can't stop itself" : undefined}
            onClick={() => run("stop")}
          >
            {busy === "stop" && <SpinnerIcon className="btn-icon" />}
            Stop
          </button>
        ) : (
          <button
            className={"btn btn--primary btn--sm" + (busy === "start" ? " btn--loading" : "")}
            disabled={!!busy}
            onClick={() => run("start")}
          >
            {busy === "start" && <SpinnerIcon className="btn-icon" />}
            Start
          </button>
        )}
        <button className="btn btn--ghost btn--sm" onClick={() => onLogs(pod.name)}>
          Logs
        </button>
        <button
          className={"btn btn--ghost btn--sm" + (busy === "update" ? " btn--loading" : "")}
          disabled={!!busy}
          onClick={() => run("update")}
        >
          {busy === "update" && <SpinnerIcon className="btn-icon" />}
          Update
        </button>
        {pod.shares.length > 0 && (
          <>
            <div className="spacer" />
            <span className="preview-label">{pod.shares.join(", ")}</span>
          </>
        )}
      </div>
    </div>
  );
}
