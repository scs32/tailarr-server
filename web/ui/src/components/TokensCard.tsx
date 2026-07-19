import { useCallback, useEffect, useState } from "react";
import type { TokensStatus } from "../types";
import { api, getStoredToken, setStoredToken } from "../api";
import { Alert } from "./Alert";
import { Field } from "./Form";

function ago(iso: string): string {
  if (!iso) return "";
  const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 60) return `${mins}m ago`;
  if (mins < 48 * 60) return `${Math.floor(mins / 60)}h ago`;
  return `${Math.floor(mins / 1440)}d ago`;
}

// API bearer tokens: the permission boundary behind granting "server" access
// to user machines. Minting is free; flipping "require" is the switch that
// closes the API — including for this browser, which is why the card keeps
// its own token field and renders even when the token list can't load.
export function TokensCard() {
  const [status, setStatus] = useState<TokensStatus | null>(null);
  const [err, setErr] = useState("");
  const [label, setLabel] = useState("");
  const [minted, setMinted] = useState("");
  const [busy, setBusy] = useState(false);
  const [browserTok, setBrowserTok] = useState(getStoredToken());

  const refresh = useCallback(() => {
    api
      .tokens()
      .then((s) => {
        setStatus(s);
        setErr("");
      })
      .catch((e) => setErr(String(e)));
  }, []);

  useEffect(refresh, [refresh]);

  async function create() {
    if (busy) return;
    setBusy(true);
    try {
      const r = await api.tokenCreate(label);
      if (r.ok && r.token) {
        setMinted(r.token);
        setLabel("");
        refresh();
      } else setErr(r.error ?? "Couldn't create a token.");
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string, tlabel: string) {
    if (!window.confirm(`Delete token “${tlabel || id}”? Clients using it lose API access immediately.`)) return;
    const r = await api.tokenDelete(id);
    if (!r.ok) setErr(r.error ?? "Couldn't delete the token.");
    refresh();
  }

  async function toggleRequire(enabled: boolean) {
    if (
      enabled &&
      !window.confirm(
        "Require a token to use Tailarr? Every app and browser will need " +
          "one — including this browser (set its token below first) and " +
          "the Tailarr app on your phone.",
      )
    )
      return;
    const r = await api.tokenRequire(enabled);
    if (!r.ok) setErr(r.error ?? "Couldn't change the setting.");
    refresh();
  }

  function saveBrowserTok(value: string) {
    setBrowserTok(value);
    setStoredToken(value.trim());
  }

  return (
    <div className="card" style={{ padding: "var(--sp-4)" }}>
      {err && <Alert kind="err">{err}</Alert>}

      {status && (
        <label
          style={{ display: "flex", gap: "var(--sp-2)", alignItems: "center", cursor: "pointer" }}
        >
          <input
            type="checkbox"
            checked={status.require}
            disabled={status.tokens.length === 0 && !status.require}
            onChange={(e) => toggleRequire(e.target.checked)}
          />
          <span>
            Require a token to use Tailarr
            {status.tokens.length === 0 && !status.require && " (create a token first)"}
          </span>
        </label>
      )}
      <p className="field__hint" style={{ margin: "var(--sp-2) 0 0" }}>
        Tailarr has no login: anyone who can open this page can manage your
        pods. That’s safe while only your own devices can reach it. Turn
        this on and every app and browser must present a token instead —
        like a password. Create one below and keep a copy in this browser
        first, or this page locks you out too. Sharing “Tailarr Server”
        with someone on the Users page only lets their device connect;
        their token is what lets them in.
      </p>

      {status && status.tokens.length > 0 && (
        <div className="row-list" style={{ marginTop: "var(--sp-3)" }}>
          {status.tokens.map((t) => (
            <div key={t.id} className="row">
              <div>
                <div className="row__title">{t.label || t.id}</div>
                <div className="row__meta">created {ago(t.created)}</div>
              </div>
              <div className="spacer" />
              <button
                className="btn btn--ghost btn--sm"
                onClick={() => remove(t.id, t.label)}
              >
                Delete
              </button>
            </div>
          ))}
        </div>
      )}

      <div style={{ display: "flex", gap: "var(--sp-2)", marginTop: "var(--sp-3)" }}>
        <input
          className="input"
          value={label}
          placeholder="Label (e.g. Stephen's iPhone)"
          onChange={(e) => setLabel(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") create();
          }}
        />
        <button
          className={"btn btn--primary btn--sm" + (busy ? " btn--loading" : "")}
          disabled={busy}
          onClick={create}
        >
          + New token
        </button>
      </div>

      {minted && (
        <div style={{ marginTop: "var(--sp-3)" }}>
          <div className="row__title">Token (shown once — copy it now)</div>
          <div
            className="log__body"
            style={{ margin: "var(--sp-2) 0", userSelect: "all", cursor: "copy" }}
            title="Click, then copy"
          >
            {minted}
          </div>
          <div className="preview-row">
            <button
              className="btn btn--ghost btn--sm"
              onClick={() => {
                saveBrowserTok(minted);
                setMinted("");
              }}
            >
              Use in this browser
            </button>
            <button className="btn btn--ghost btn--sm" onClick={() => setMinted("")}>
              Dismiss
            </button>
          </div>
        </div>
      )}

      <div style={{ marginTop: "var(--sp-4)" }}>
        <Field label="This browser's token">
          <input
            className="input"
            type="password"
            value={browserTok}
            placeholder="paste a token to keep using this UI when tokens are required"
            onChange={(e) => saveBrowserTok(e.target.value)}
          />
        </Field>
      </div>
    </div>
  );
}
