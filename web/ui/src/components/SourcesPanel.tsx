import { useState } from "react";
import type { ShareResult, Source } from "../types";
import { api } from "../api";
import { Field } from "./Form";
import { Alert } from "./Alert";

export function SourcesPanel({
  sources,
  onChanged,
}: {
  sources: Source[];
  onChanged: () => void;
}) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  function report(r: ShareResult) {
    setMsg(
      r.ok
        ? { kind: "ok", text: r.message ?? "Done." }
        : { kind: "err", text: r.error ?? "Failed." },
    );
    onChanged();
  }

  async function add() {
    setBusy(true);
    try {
      const r = await api.sourceAdd(name.trim(), url.trim());
      report(r);
      if (r.ok) {
        setName("");
        setUrl("");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="form-section" style={{ maxWidth: 640 }}>
      <h3>Catalog sources</h3>
      <p className="field__hint" style={{ marginTop: 0 }}>
        Add a URL to an external catalog (homelab.js JSON schema). Its services
        appear in the catalog alongside the built-in ones.
      </p>

      {msg && (
        <div style={{ marginBottom: "var(--sp-3)" }}>
          <Alert kind={msg.kind}>{msg.text}</Alert>
        </div>
      )}

      {sources.length > 0 && (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: "var(--sp-2)",
            marginBottom: "var(--sp-4)",
          }}
        >
          {sources.map((s) => (
            <div
              key={s.name}
              className="card"
              style={{
                padding: "var(--sp-3)",
                display: "flex",
                alignItems: "center",
                gap: "var(--sp-3)",
              }}
            >
              <div style={{ minWidth: 0 }}>
                <div style={{ fontWeight: "var(--fw-medium)" }}>{s.name}</div>
                <div className="catalog-card__meta" style={{ margin: 0 }}>
                  {s.url}
                </div>
              </div>
              <div className="spacer" />
              {s.error ? (
                <span className="badge badge--error" title={s.error}>
                  error
                </span>
              ) : (
                <span className="preview-label">{s.service_count} services</span>
              )}
              <button
                className="btn btn--danger btn--sm"
                onClick={async () => report(await api.sourceDelete(s.name))}
              >
                Delete
              </button>
            </div>
          ))}
        </div>
      )}

      <Field label="Name" hint="a–z, 0–9, dashes">
        <input
          className="input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="community"
        />
      </Field>
      <Field label="Catalog URL">
        <input
          className="input"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://example.com/catalog.json"
        />
      </Field>
      <button
        className={"btn btn--primary" + (busy ? " btn--loading" : "")}
        disabled={busy || !name.trim() || !url.trim()}
        onClick={add}
      >
        Add source
      </button>
    </div>
  );
}
