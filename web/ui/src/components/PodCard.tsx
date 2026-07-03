import { useState } from "react";
import type { Pod } from "../types";
import { api } from "../api";
import { PodGlyph, SpinnerIcon } from "./Icons";

// State is conveyed by the card tint (green running / amber stopped / red
// crashed) — see .pod-card--* in the stylesheet. No badge.
export function PodCard({
  pod,
  onChanged,
  onLogs,
  onEdit,
}: {
  pod: Pod;
  onChanged: () => void;
  onLogs: (name: string) => void;
  onEdit: (name: string) => void;
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

  return (
    <div className={`pod-card card pod-card--${pod.state}`}>
      <div className="pod-card__head">
        <div className="pod-icon">
          <PodGlyph />
        </div>
        <div className="pod-card__info">
          <div className="pod-card__title">{pod.name}</div>
          {pod.image && (
            <div className="pod-card__url" title={pod.image}>
              {pod.image}
            </div>
          )}
        </div>
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
          className="btn btn--ghost btn--sm"
          disabled={!!busy || pod.controller}
          title={
            pod.controller
              ? "The controller can't reconfigure itself"
              : "Edit config, then reload or update"
          }
          onClick={() => onEdit(pod.name)}
        >
          Edit
        </button>
        {pod.update && (
          <button
            className={"btn btn--secondary btn--sm" + (busy === "update" ? " btn--loading" : "")}
            disabled={!!busy}
            title="A newer image is available — pull it and recreate the pod"
            onClick={() => run("update")}
          >
            {busy === "update" && <SpinnerIcon className="btn-icon" />}
            Update
          </button>
        )}
      </div>
    </div>
  );
}
