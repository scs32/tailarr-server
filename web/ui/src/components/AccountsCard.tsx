import { useCallback, useEffect, useState } from "react";
import type { AccountsStatus } from "../types";
import { api } from "../api";
import { Alert } from "./Alert";
import { Field, Toggle } from "./Form";
import { SpinnerIcon } from "./Icons";

// Saved provider accounts: the logins for outside services (indexers,
// usenet providers) that Tailarr can't derive — the things a Magic Stack
// wizard would otherwise ask for every time. Each save is validated live
// against the provider first; secrets are stored server-side (0600) and
// never sent back to the UI.
export function AccountsCard() {
  const [status, setStatus] = useState<AccountsStatus | null>(null);
  const [err, setErr] = useState("");
  const [ok, setOk] = useState("");
  const [kind, setKind] = useState<"newznab" | "usenet">("newznab");
  const [label, setLabel] = useState("");
  const [idxUrl, setIdxUrl] = useState("");
  const [idxKey, setIdxKey] = useState("");
  const [nHost, setNHost] = useState("");
  const [nPort, setNPort] = useState("563");
  const [nSsl, setNSsl] = useState(true);
  const [nUser, setNUser] = useState("");
  const [nPass, setNPass] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    api
      .accounts()
      .then((s) => setStatus(s))
      .catch((e) => setErr(String(e)));
  }, []);

  useEffect(refresh, [refresh]);

  const filled =
    kind === "newznab" ? idxUrl && idxKey : nHost && nUser && nPass;

  async function save() {
    if (busy || !filled) return;
    setBusy(true);
    setErr("");
    setOk("");
    try {
      const body =
        kind === "newznab"
          ? { kind, label, url: idxUrl, key: idxKey }
          : {
              kind,
              label,
              host: nHost,
              port: nPort,
              ssl: nSsl,
              user: nUser,
              password: nPass,
            };
      const r = await api.accountSave(body);
      if (r.ok) {
        setOk("Account checked and saved.");
        setLabel("");
        setIdxUrl("");
        setIdxKey("");
        setNHost("");
        setNUser("");
        setNPass("");
        if (r.status) setStatus(r.status);
        else refresh();
      } else {
        setErr(r.error ?? "Couldn't save the account.");
      }
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string, label: string) {
    if (
      !window.confirm(
        `Remove ${label}? Services already set up with it keep working — ` +
          "it just stops being offered in setup forms.",
      )
    )
      return;
    const r = await api.accountDelete(id);
    if (!r.ok) setErr(r.error ?? "Couldn't remove the account.");
    if (r.status) setStatus(r.status);
    else refresh();
  }

  return (
    <div className="card" style={{ padding: "var(--sp-4)" }}>
      {err && <Alert kind="err">{err}</Alert>}
      {ok && <Alert kind="ok">{ok}</Alert>}

      <p className="field__hint" style={{ margin: 0 }}>
        The logins for services you pay for — indexers and usenet
        providers. Save them once and setup forms (like Magic Stacks)
        offer them instead of asking again. Every account is checked
        against the provider before it saves; keys and passwords are
        never shown back.
      </p>

      {status && status.accounts.length > 0 && (
        <div className="row-list" style={{ marginTop: "var(--sp-3)" }}>
          {status.accounts.map((a) => (
            <div key={a.id} className="row">
              <div>
                <div className="row__title">
                  {a.label}{" "}
                  <span className="chip">
                    {a.kind === "newznab" ? "indexer" : "usenet"}
                  </span>
                </div>
                <div className="row__meta">
                  <code>{a.detail}</code>
                </div>
              </div>
              <div className="spacer" />
              <button
                className="btn btn--ghost btn--sm"
                onClick={() => remove(a.id, a.label)}
              >
                Remove
              </button>
            </div>
          ))}
        </div>
      )}

      <div style={{ marginTop: "var(--sp-3)" }}>
        <Field label="Type">
          <select
            className="input"
            value={kind}
            onChange={(e) => setKind(e.target.value as "newznab" | "usenet")}
          >
            <option value="newznab">Indexer (newznab)</option>
            <option value="usenet">Usenet provider</option>
          </select>
        </Field>
        <Field label="Name" hint="How it appears in lists (optional).">
          <input
            className="input"
            value={label}
            placeholder={kind === "newznab" ? "NZBgeek" : "Eweka"}
            onChange={(e) => setLabel(e.target.value)}
          />
        </Field>
        {kind === "newznab" ? (
          <>
            <Field label="Indexer URL">
              <input
                className="input"
                value={idxUrl}
                placeholder="https://api.nzbgeek.info"
                onChange={(e) => setIdxUrl(e.target.value)}
              />
            </Field>
            <Field label="API key">
              <input
                className="input"
                type="password"
                value={idxKey}
                onChange={(e) => setIdxKey(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") save();
                }}
              />
            </Field>
          </>
        ) : (
          <>
            <Field label="News server">
              <input
                className="input"
                value={nHost}
                placeholder="news.eweka.nl"
                onChange={(e) => setNHost(e.target.value)}
              />
            </Field>
            <div className="folder-row">
              <Field label="Port">
                <input
                  className="input"
                  style={{ width: 90 }}
                  value={nPort}
                  onChange={(e) => setNPort(e.target.value)}
                />
              </Field>
              <Toggle
                checked={nSsl}
                onChange={(v) => {
                  setNSsl(v);
                  setNPort(v ? "563" : "119");
                }}
              >
                SSL
              </Toggle>
            </div>
            <Field label="Username">
              <input
                className="input"
                value={nUser}
                onChange={(e) => setNUser(e.target.value)}
              />
            </Field>
            <Field label="Password">
              <input
                className="input"
                type="password"
                value={nPass}
                onChange={(e) => setNPass(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") save();
                }}
              />
            </Field>
          </>
        )}
        <div className="preview-row" style={{ marginTop: "var(--sp-2)" }}>
          <button
            className="btn btn--primary btn--sm"
            disabled={busy || !filled}
            onClick={save}
          >
            {busy && <SpinnerIcon className="btn-icon" />}
            {busy ? "Checking with the provider…" : "Check and save"}
          </button>
        </div>
      </div>
    </div>
  );
}
