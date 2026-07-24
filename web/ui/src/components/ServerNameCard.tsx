// The server's display name. Every Tailarr controller shares the MagicDNS
// hostname "tailarr", so a device joining any server would otherwise derive
// the profile name "Tailarr" — ambiguous once you run more than one. The
// admin sets a human name here; it rides /api/info into the invite the app
// embeds, so enrolled profiles are named after the server ("Living Room").
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Alert } from "./Alert";
import { Field } from "./Form";
import { SpinnerIcon } from "./Icons";

export function ServerNameCard() {
  const [name, setName] = useState("");
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const live = useRef(true);
  useEffect(
    () => () => {
      live.current = false;
    },
    [],
  );

  useEffect(() => {
    api
      .info()
      .then((i) => live.current && setName(i.name ?? ""))
      .catch(() => {
        /* /api/info unreachable — the field still works once entered */
      });
  }, []);

  async function save() {
    setBusy(true);
    setError("");
    setSaved(false);
    try {
      const r = await api.serverSave(name.trim());
      setName(r.name);
      setSaved(true);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Field
        label="Server name"
        hint="Shown on devices that join this server (e.g. “Living Room”, “Mom’s House”). Leave blank to use the default name."
      >
        <input
          className="input"
          value={name}
          placeholder="Tailarr"
          maxLength={60}
          onChange={(e) => {
            setName(e.target.value);
            setSaved(false);
          }}
        />
      </Field>
      {error && <Alert kind="err">{error}</Alert>}
      <div className="preview-row" style={{ marginTop: "var(--sp-4)" }}>
        <button className="btn btn--primary btn--sm" disabled={busy} onClick={save}>
          {busy && <SpinnerIcon className="btn-icon" />}
          Save
        </button>
        {saved && !busy && (
          <span style={{ color: "var(--muted)" }}>Saved.</span>
        )}
      </div>
    </>
  );
}
