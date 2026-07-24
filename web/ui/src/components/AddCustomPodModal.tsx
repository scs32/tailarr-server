import { useState } from "react";
import { api } from "../api";
import { Alert } from "./Alert";
import { Field } from "./Form";
import {
  FolderEditor,
  rowsToVolumes,
  type FolderRow,
} from "./FolderEditor";
import { SpinnerIcon } from "./Icons";
import { parsePairs } from "../lib/pairs";

const NAME_RE = /^[a-z0-9][a-z0-9-]*$/;

// Authors an entry in the "custom" catalog source — it does NOT install
// anything. The saved definition shows up as a catalog card and installs
// (and reinstalls) through the same flow as any other service; shares and
// auth-key concerns live there, not here.
export function AddCustomPodModal({
  onSaved,
  onClose,
}: {
  onSaved: () => void;
  onClose: () => void;
}) {
  const [name, setName] = useState("");
  const [image, setImage] = useState("");
  const [command, setCommand] = useState("");
  const [portsText, setPortsText] = useState("");
  const [envText, setEnvText] = useState("");
  const [folders, setFolders] = useState<FolderRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const nameErr =
    name && !NAME_RE.test(name) ? "Lowercase letters, digits, dashes." : "";
  const canSave = NAME_RE.test(name) && image.trim() !== "" && !busy;

  async function save() {
    setBusy(true);
    setError(null);
    try {
      const r = await api.customPodSave({
        name,
        image: image.trim(),
        command: command.trim(),
        ports: parsePairs(portsText, ":"),
        environment: parsePairs(envText, "="),
        volumes: rowsToVolumes(folders),
      });
      if (r.ok) {
        onSaved();
        onClose();
      } else {
        setError(r.error ?? "Save failed.");
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="scrim" onClick={busy ? undefined : onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal__head">
          <span className="modal__title">Add a custom service</span>
          <div className="spacer" />
          <button className="btn btn--ghost btn--sm" onClick={onClose}>
            Close
          </button>
        </div>

        <p className="field__hint" style={{ margin: "0 0 var(--sp-4)" }}>
          Describe any OCI image once — it joins your catalog under the{" "}
          <strong>custom</strong> source and installs like any other service.
        </p>

        <Field label="Name" hint="a–z, 0–9, dashes" error={nameErr}>
          <input
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="jellyfin"
            autoFocus
          />
        </Field>
        <Field label="Image">
          <input
            className="input"
            value={image}
            onChange={(e) => setImage(e.target.value)}
            placeholder="ghcr.io/someone/thing:latest"
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

        <FolderEditor rows={folders} onChange={setFolders} />

        {error && (
          <div style={{ marginTop: "var(--sp-3)" }}>
            <Alert kind="err">{error}</Alert>
          </div>
        )}

        <div className="preview-row" style={{ marginTop: "var(--sp-5)" }}>
          <button
            className={"btn btn--primary" + (busy ? " btn--loading" : "")}
            disabled={!canSave}
            onClick={save}
          >
            {busy && <SpinnerIcon className="btn-icon" />}
            Add to catalog
          </button>
          <button className="btn btn--ghost" disabled={busy} onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
