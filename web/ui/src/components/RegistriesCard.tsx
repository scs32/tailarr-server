import { useCallback, useEffect, useState } from "react";
import type { RegistriesStatus } from "../types";
import { api } from "../api";
import { Alert } from "./Alert";
import { Field } from "./Form";

// Private-registry credentials: needed to pull private images (a custom pod
// from a private ghcr.io package, say). The secret is validated with a real
// registry login server-side, stored 0600, and never sent back to the UI.
export function RegistriesCard() {
  const [status, setStatus] = useState<RegistriesStatus | null>(null);
  const [err, setErr] = useState("");
  const [registry, setRegistry] = useState("ghcr.io");
  const [username, setUsername] = useState("");
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    api
      .registries()
      .then((s) => {
        setStatus(s);
        setErr("");
      })
      .catch((e) => setErr(String(e)));
  }, []);

  useEffect(refresh, [refresh]);

  async function save() {
    if (busy) return;
    setBusy(true);
    try {
      const r = await api.registrySave(registry.trim(), username.trim(), secret.trim());
      if (r.ok) {
        setUsername("");
        setSecret("");
        refresh();
      } else setErr(r.error ?? "Couldn't save the credential.");
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove(host: string) {
    if (
      !window.confirm(
        `Remove the ${host} credential? Pods with private images from it ` +
          "can keep running, but can't pull updates until you add one again.",
      )
    )
      return;
    const r = await api.registryDelete(host);
    if (!r.ok) setErr(r.error ?? "Couldn't remove the credential.");
    refresh();
  }

  return (
    <div className="card" style={{ padding: "var(--sp-4)" }}>
      {err && <Alert kind="err">{err}</Alert>}

      <p className="field__hint" style={{ margin: 0 }}>
        Public images just work. To use a private image — for example one you
        publish to GitHub&rsquo;s ghcr.io — add a login for that registry here.
        For GitHub, use your GitHub username and a personal access token with
        the <code>read:packages</code> scope. Tailarr checks the login against
        the registry before saving, and every pull uses it from then on.
      </p>

      {status && status.registries.length > 0 && (
        <div className="row-list" style={{ marginTop: "var(--sp-3)" }}>
          {status.registries.map((r) => (
            <div key={r.registry} className="row">
              <div>
                <div className="row__title">{r.registry}</div>
                <div className="row__meta">as {r.username}</div>
              </div>
              <div className="spacer" />
              <button
                className="btn btn--ghost btn--sm"
                onClick={() => remove(r.registry)}
              >
                Remove
              </button>
            </div>
          ))}
        </div>
      )}

      <div style={{ marginTop: "var(--sp-3)" }}>
        <Field label="Registry">
          <input
            className="input"
            value={registry}
            placeholder="ghcr.io"
            onChange={(e) => setRegistry(e.target.value)}
          />
        </Field>
        <Field label="Username">
          <input
            className="input"
            value={username}
            placeholder="your GitHub username"
            onChange={(e) => setUsername(e.target.value)}
          />
        </Field>
        <Field label="Token">
          <input
            className="input"
            type="password"
            value={secret}
            placeholder="personal access token (read:packages)"
            onChange={(e) => setSecret(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") save();
            }}
          />
        </Field>
        <div className="preview-row" style={{ marginTop: "var(--sp-2)" }}>
          <button
            className={"btn btn--primary btn--sm" + (busy ? " btn--loading" : "")}
            disabled={busy}
            onClick={save}
          >
            {busy ? "Checking…" : "Add registry"}
          </button>
        </div>
      </div>
    </div>
  );
}
