import { useEffect, useState } from "react";
import type { Share } from "../types";
import { api } from "../api";
import { parsePairs, pairsToText } from "../lib/pairs";
import { Field, FormSection } from "./Form";
import {
  FolderEditor,
  rowsToVolumes,
  volumesToRows,
  type FolderRow,
} from "./FolderEditor";
import { SharePicker } from "./SharePicker";
import { BackupsPanel } from "./BackupsPanel";
import { Alert } from "./Alert";
import { SpinnerIcon } from "./Icons";

// Edit popup for a deployed pod: prefilled with its saved config, with
// Reload (recreate as-is) and Update (pull latest image + recreate) at the
// bottom. Both save any edits made to the fields first.
export function EditModal({
  name,
  onClose,
  onChanged,
}: {
  name: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [shares, setShares] = useState<Share[]>([]);
  const [controller, setController] = useState(false);

  const [image, setImage] = useState("");
  const [command, setCommand] = useState("");
  const [portsText, setPortsText] = useState("");
  const [envText, setEnvText] = useState("");
  const [folders, setFolders] = useState<FolderRow[]>([]);
  const [memory, setMemory] = useState("");
  const [picked, setPicked] = useState<string[]>([]);

  const [busy, setBusy] = useState<"" | "reload" | "update">("");
  const [output, setOutput] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    Promise.all([api.podConfig(name), api.shares()])
      .then(([res, sh]) => {
        if (!live) return;
        setShares(sh);
        if (!res.ok || !res.config) {
          setErr(res.error ?? "Could not load this pod's config.");
          return;
        }
        const c = res.config;
        setImage(c.image);
        setCommand(c.command);
        setPortsText(pairsToText(c.ports, ":"));
        setEnvText(pairsToText(c.environment, "="));
        setFolders(volumesToRows(c.volumes));
        setMemory(c.memory_limit);
        setPicked(c.shares);
        setController(c.controller);
      })
      .catch((e) => live && setErr(String(e)))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [name]);

  async function apply(pull: boolean) {
    setBusy(pull ? "update" : "reload");
    setOutput(null);
    try {
      const r = await api.reconfigure(name, {
        image: image.trim(),
        command: command.trim(),
        ports: parsePairs(portsText, ":"),
        environment: parsePairs(envText, "="),
        volumes: rowsToVolumes(folders),
        memory_limit: memory.trim(),
        shares: picked,
        pull,
      });
      setOutput(r.output || `${name}: ${r.status}`);
      if (r.ok) onChanged();
    } catch (e) {
      setOutput(String(e));
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="scrim" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal__head">
          <span className="modal__title">Edit {name}</span>
          <div className="spacer" />
          <button className="btn btn--ghost btn--sm" onClick={onClose}>
            Close
          </button>
        </div>

        {loading ? (
          <p className="field__hint" style={{ margin: 0 }}>
            Loading…
          </p>
        ) : err ? (
          <Alert kind="err">{err}</Alert>
        ) : (
          <>
            <FormSection title="Version">
              <Field label="Version">
                <input
                  className="input"
                  value={image}
                  onChange={(e) => setImage(e.target.value)}
                />
              </Field>
              <Field label="Command (optional)">
                <input
                  className="input"
                  value={command}
                  onChange={(e) => setCommand(e.target.value)}
                  placeholder="e.g. sleep infinity"
                />
              </Field>
              <Field label="Ports" hint="one mapping per line (e.g. 8080:8080)">
                <textarea
                  className="textarea"
                  rows={2}
                  value={portsText}
                  onChange={(e) => setPortsText(e.target.value)}
                  placeholder="8080:8080"
                />
              </Field>
              <Field label="Environment" hint="one KEY=value per line">
                <textarea
                  className="textarea"
                  rows={3}
                  value={envText}
                  onChange={(e) => setEnvText(e.target.value)}
                />
              </Field>
              <Field label="Memory limit (optional)" hint="e.g. 512m — blank for none">
                <input
                  className="input"
                  value={memory}
                  onChange={(e) => setMemory(e.target.value)}
                  placeholder=""
                />
              </Field>
            </FormSection>

            <FolderEditor rows={folders} onChange={setFolders} />

            <SharePicker shares={shares} picked={picked} onChange={setPicked} />

            <BackupsPanel name={name} controller={controller} onChanged={onChanged} />

            {controller && (
              <div style={{ marginTop: "var(--sp-4)" }}>
                <Alert kind="info">
                  This is Tailarr itself — it can’t restart itself.
                </Alert>
              </div>
            )}

            {output !== null && (
              <div className="log" style={{ marginTop: "var(--sp-4)" }}>
                <div className="log__body">{output}</div>
              </div>
            )}

            <div className="preview-row" style={{ marginTop: "var(--sp-5)" }}>
              <button
                className={"btn btn--secondary" + (busy === "reload" ? " btn--loading" : "")}
                disabled={!!busy || controller || !image.trim()}
                title="Save edits and restart the service with the current version"
                onClick={() => apply(false)}
              >
                {busy === "reload" && <SpinnerIcon className="btn-icon" />}
                Reload
              </button>
              <button
                className={"btn btn--primary" + (busy === "update" ? " btn--loading" : "")}
                disabled={!!busy || controller || !image.trim()}
                title="Fetch the latest version, save edits, and restart the service"
                onClick={() => apply(true)}
              >
                {busy === "update" && <SpinnerIcon className="btn-icon" />}
                Update
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
