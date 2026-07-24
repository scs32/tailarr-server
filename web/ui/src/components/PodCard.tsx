import { useState } from "react";
import type { Pod } from "../types";
import { api } from "../api";
import { PodGlyph, SpinnerIcon } from "./Icons";

const BUSY_LABEL: Record<string, string> = {
  start: "starting…",
  stop: "stopping…",
  restart: "restarting…",
  update: "updating…",
  remove: "removing…",
  reconfigure: "applying config…",
  backup: "backing up…",
  restore: "restoring…",
  funnel: "toggling public access…",
};

// State is conveyed by the card tint (green running / amber stopped / red
// crashed). `pod.busy` is the server's in-flight action registry, so a
// transition started elsewhere (another view, another tab, before a reload)
// still locks this card and shows what's happening.
export function PodCard({
  pod,
  onChanged,
  onLogs,
  onExec,
  onEdit,
}: {
  pod: Pod;
  onChanged: () => void;
  onLogs: (name: string) => void;
  onExec: (name: string) => void;
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

  const serverBusy = pod.busy; // in-flight on the server (any client)
  const locked = !!busy || !!serverBusy;
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
        {pod.system && (
          <span
            className="chip"
            title="System service: Tailarr manages this itself. It is never shareable and never appears to user devices; manage it from the Notifications page."
          >
            system
          </span>
        )}
        {pod.identity === "missing" && (
          <span
            className="chip chip--danger"
            title="This service's network identity is missing, so users can't reach it even though it's healthy. Tailarr keeps retrying automatically; if it persists, re-check your Tailscale policy and API credential in Settings."
          >
            identity tag missing
          </span>
        )}
        {serverBusy && !busy && (
          <span className="chip chip--busy">{BUSY_LABEL[serverBusy] ?? serverBusy}</span>
        )}
      </div>

      <div className="pod-card__foot">
        {running ? (
          <button
            className={"btn btn--secondary btn--sm" + (busy ? " btn--loading" : "")}
            disabled={locked || pod.controller}
            title={pod.controller ? "Tailarr can't stop itself" : undefined}
            onClick={() => run("stop")}
          >
            {busy === "stop" && <SpinnerIcon className="btn-icon" />}
            Stop
          </button>
        ) : (
          <button
            className={"btn btn--primary btn--sm" + (busy === "start" ? " btn--loading" : "")}
            disabled={locked}
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
          disabled={!running}
          title={running ? "Run a one-shot command in this service" : "Service isn't running"}
          onClick={() => onExec(pod.name)}
        >
          Shell
        </button>
        <button
          className="btn btn--ghost btn--sm"
          disabled={locked || pod.controller || pod.system}
          title={
            pod.controller
              ? "Tailarr can't reconfigure itself"
              : pod.system
                ? "Tailarr manages this service — configure it from the Notifications page"
                : "Edit config, then reload or update"
          }
          onClick={() => onEdit(pod.name)}
        >
          Edit
        </button>
        {pod.update && (
          <button
            className={"btn btn--secondary btn--sm" + (busy === "update" ? " btn--loading" : "")}
            disabled={locked}
            title="A newer version is available — update to it"
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
