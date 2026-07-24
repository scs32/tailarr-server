import { useState } from "react";
import type { TsApiProbe } from "../types";
import { api } from "../api";
import { Alert } from "./Alert";
import { Field, FormSection } from "./Form";

const OAUTH_CONSOLE_URL = "https://login.tailscale.com/admin/settings/oauth";
const ACL_CONSOLE_URL = "https://login.tailscale.com/admin/acls/file";

// What to paste into the tailnet policy's tagOwners when tag:tailarr-ctrl
// isn't selectable yet while creating the OAuth client (it must exist in
// tagOwners before the console offers it as a client tag).
const TAGOWNERS_SNIPPET = `"tagOwners": {
	"tag:tailarr-ctrl":   ["autogroup:admin"],
	"tag:tailarr":        ["autogroup:admin", "tag:tailarr-ctrl"],
	"tag:tailarr-user":   ["autogroup:admin", "tag:tailarr-ctrl"],
	"tag:tailarr-public": ["autogroup:admin", "tag:tailarr-ctrl"],
},`;

const CHECKS: { key: "devices" | "auth_keys" | "policy_file"; label: string; why: string }[] = [
  { key: "devices", label: "Devices / Core (write)", why: "adopt user machines, manage service access, and set up networking" },
  { key: "auth_keys", label: "Auth Keys (write)", why: "create enrollment keys — no more manual key pasting" },
  { key: "policy_file", label: "Policy File (write)", why: "keep the tailarr-managed ACL sections in sync" },
];

// Guided first-run setup for the controller's Tailscale API credential:
// explains the scopes, validates live with read-only calls (per-capability
// pass/fail), saves .tsapi.json (0600 server-side), and offers to
// initialize the tailarr-managed policy fences. Embedded wherever an
// API-requiring action first fails (Users, install forms) and on Settings.
export function TsApiWizard({ onDone }: { onDone?: () => void }) {
  const [mode, setMode] = useState<"oauth" | "token">("oauth");
  const [cid, setCid] = useState("");
  const [secret, setSecret] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState<"" | "validate" | "save" | "fences">("");
  const [probe, setProbe] = useState<TsApiProbe | null>(null);
  const [saved, setSaved] = useState(false);
  const [fencesDone, setFencesDone] = useState(false);
  const [fenceErr, setFenceErr] = useState("");
  const [showSnippet, setShowSnippet] = useState(false);

  const cred =
    mode === "oauth"
      ? { oauth_client_id: cid.trim(), oauth_client_secret: secret.trim() }
      : { token: token.trim() };
  const filled =
    mode === "oauth" ? !!(cid.trim() && secret.trim()) : !!token.trim();

  async function run(kind: "validate" | "save") {
    setBusy(kind);
    setFenceErr("");
    try {
      const r =
        kind === "validate"
          ? await api.tsapiValidate(cred)
          : await api.tsapiSave(cred);
      setProbe(r);
      if (kind === "save" && r.ok && r.saved) {
        setSaved(true);
        if (!r.fences || r.fences.missing.length === 0) {
          setFencesDone(true);
          onDone?.();
        }
      }
    } catch (e) {
      setProbe({ ok: false, mode: null, checks: {}, fences: null, error: String(e) });
    } finally {
      setBusy("");
    }
  }

  async function initFences() {
    setBusy("fences");
    setFenceErr("");
    try {
      const r = await api.tsapiFences();
      if (r.ok) {
        setFencesDone(true);
        onDone?.();
      } else {
        setFenceErr(r.error ?? "Couldn't initialize the policy fences.");
      }
    } catch (e) {
      setFenceErr(String(e));
    } finally {
      setBusy("");
    }
  }

  const missingFences = probe?.fences?.missing ?? [];

  return (
    <div className="card" style={{ padding: "var(--sp-5)", marginTop: "var(--sp-4)" }}>
      <FormSection title="1 · Why Tailarr needs an API credential">
        <p className="field__hint" style={{ margin: 0 }}>
          Tailarr manages your tailnet for you: it generates per-service and per-user
          enrollment keys (no more pasting), manages service access, and keeps the
          Tailarr-managed sections of your network policy in
          sync. All of that goes through the Tailscale API, so the controller
          needs its own credential — stored only on this machine, at{" "}
          <code>Pods/.tsapi.json</code> (mode 0600), never logged.
        </p>
      </FormSection>

      <FormSection title="2 · Create the credential">
        <p className="field__hint" style={{ marginTop: 0 }}>
          Preferred: an <strong>OAuth client</strong> from{" "}
          <a href={OAUTH_CONSOLE_URL} target="_blank" rel="noreferrer">
            admin console → Settings → OAuth clients
          </a>
          . Grant exactly these <strong>write</strong> scopes and assign it
          the Tailarr server tag (<code>tag:tailarr-ctrl</code>):
        </p>
        <ul className="field__hint" style={{ margin: "0 0 var(--sp-2)", paddingLeft: "1.2em" }}>
          {CHECKS.map((c) => (
            <li key={c.key}>
              <strong>{c.label}</strong> — {c.why}
            </li>
          ))}
        </ul>
        <p className="field__hint" style={{ margin: 0 }}>
          <button
            className="btn btn--ghost btn--sm"
            onClick={() => setShowSnippet((v) => !v)}
          >
            tag:tailarr-ctrl isn’t selectable?
          </button>
        </p>
        {showSnippet && (
          <div style={{ marginTop: "var(--sp-2)" }}>
            <p className="field__hint" style={{ marginTop: 0 }}>
              The console only offers tags that already exist in the policy’s{" "}
              <code>tagOwners</code>. Two ways out: (a) paste this into your{" "}
              <a href={ACL_CONSOLE_URL} target="_blank" rel="noreferrer">
                policy file
              </a>{" "}
              first, or (b) use a static <strong>API access token</strong>{" "}
              (Settings → Keys — needs no tags) below, let Tailarr initialize
              the managed sections in step 3, then come back and swap in a
              scoped OAuth client.
            </p>
            <pre className="log__body" style={{ margin: 0, userSelect: "all" }}>
              {TAGOWNERS_SNIPPET}
            </pre>
          </div>
        )}

        <div style={{ display: "flex", gap: "var(--sp-4)", margin: "var(--sp-4) 0 var(--sp-3)" }}>
          <label style={{ cursor: "pointer" }}>
            <input
              type="radio"
              checked={mode === "oauth"}
              onChange={() => setMode("oauth")}
            />{" "}
            OAuth client (recommended)
          </label>
          <label style={{ cursor: "pointer" }}>
            <input
              type="radio"
              checked={mode === "token"}
              onChange={() => setMode("token")}
            />{" "}
            API access token
          </label>
        </div>

        {mode === "oauth" ? (
          <>
            <Field label="Client ID">
              <input
                className="input"
                autoComplete="off"
                value={cid}
                onChange={(e) => setCid(e.target.value)}
              />
            </Field>
            <Field label="Client secret" hint="tskey-client-…">
              <input
                className="input"
                type="password"
                autoComplete="off"
                value={secret}
                onChange={(e) => setSecret(e.target.value)}
              />
            </Field>
          </>
        ) : (
          <Field
            label="API access token"
            hint="tskey-api-… — full access, expires after 90 days; prefer a scoped OAuth client long-term"
          >
            <input
              className="input"
              type="password"
              autoComplete="off"
              value={token}
              onChange={(e) => setToken(e.target.value)}
            />
          </Field>
        )}

        <div className="preview-row">
          <button
            className={"btn btn--ghost" + (busy === "validate" ? " btn--loading" : "")}
            disabled={!filled || !!busy}
            onClick={() => run("validate")}
          >
            Test
          </button>
          <button
            className={"btn btn--primary" + (busy === "save" ? " btn--loading" : "")}
            disabled={!filled || !!busy}
            onClick={() => run("save")}
          >
            Validate &amp; save
          </button>
        </div>

        {probe && (
          <div style={{ marginTop: "var(--sp-3)" }}>
            {CHECKS.map((c) => {
              const r = probe.checks[c.key];
              if (!r) return null;
              return (
                <div key={c.key} className="field__hint" style={{ margin: "2px 0" }}>
                  {r.ok ? "✓" : "✗"} {c.label}
                  {r.detail && <> — {r.detail}</>}
                </div>
              );
            })}
            {probe.error && (
              <div style={{ marginTop: "var(--sp-2)" }}>
                <Alert kind="err">{probe.error}</Alert>
              </div>
            )}
            {probe.ok && !saved && (
              <div style={{ marginTop: "var(--sp-2)" }}>
                <Alert kind="ok">
                  All capability checks passed — “Validate &amp; save” stores
                  the credential.
                </Alert>
              </div>
            )}
            {saved && (
              <div style={{ marginTop: "var(--sp-2)" }}>
                <Alert kind="ok">Credential saved to Pods/.tsapi.json (0600).</Alert>
              </div>
            )}
          </div>
        )}
      </FormSection>

      {saved && (
        <FormSection title="3 · Policy fences">
          {fencesDone || missingFences.length === 0 ? (
            <Alert kind="ok">
              The tailnet policy carries all three tailarr-managed sections.
              You’re done.
            </Alert>
          ) : (
            <>
              <p className="field__hint" style={{ marginTop: 0 }}>
                Your policy is missing the managed section
                {missingFences.length > 1 ? "s" : ""}{" "}
                <code>{missingFences.join(", ")}</code>. Without them, policy
                sync fails closed. Tailarr can add the
                fenced markers now (everything you wrote in the policy is
                left byte-for-byte untouched) and fill them from the deployed
                service list.
              </p>
              <button
                className={"btn btn--primary" + (busy === "fences" ? " btn--loading" : "")}
                disabled={!!busy}
                onClick={initFences}
              >
                Initialize policy fences
              </button>
              {fenceErr && (
                <div style={{ marginTop: "var(--sp-2)" }}>
                  <Alert kind="err">{fenceErr}</Alert>
                </div>
              )}
            </>
          )}
        </FormSection>
      )}
    </div>
  );
}
