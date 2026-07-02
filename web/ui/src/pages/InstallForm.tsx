import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import type { CatalogItem, InstallResult, Share } from "../types";
import { api } from "../api";
import { Field, FormSection, Toggle } from "../components/Form";
import { Alert } from "../components/Alert";
import { SharePicker } from "../components/SharePicker";
import { InstallResultView } from "../components/InstallResultView";

export function InstallForm() {
  const { name = "" } = useParams();
  const [item, setItem] = useState<CatalogItem | null>(null);
  const [shares, setShares] = useState<Share[]>([]);
  const [loadErr, setLoadErr] = useState("");

  const [env, setEnv] = useState<Record<string, string>>({});
  const [vols, setVols] = useState<Record<string, string>>({});
  const [picked, setPicked] = useState<string[]>([]);
  const [tailscale, setTailscale] = useState(true);
  const [https, setHttps] = useState(true);
  const [npm, setNpm] = useState(false);
  const [authkey, setAuthkey] = useState("");

  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<InstallResult | null>(null);

  useEffect(() => {
    Promise.all([api.catalog(), api.info(), api.shares()])
      .then(([catalog, info, sh]) => {
        const found = catalog.find((c) => c.name === name) ?? null;
        setItem(found);
        setShares(sh);
        if (found) {
          setEnv({ ...found.environment });
          const v: Record<string, string> = {};
          for (const cpath of Object.values(found.volumes)) {
            v[cpath] = `${info.pods_dir}/${name}/${cpath.replace(/^\//, "")}`;
          }
          setVols(v);
        }
      })
      .catch((e) => setLoadErr(String(e)));
  }, [name]);

  async function submit() {
    setBusy(true);
    setResult(null);
    try {
      setResult(
        await api.install({
          service: name,
          environment: env,
          volumes: vols,
          shares: picked,
          tailscale,
          https,
          npm,
          authkey,
        }),
      );
    } finally {
      setBusy(false);
    }
  }

  if (loadErr) return <Alert kind="err">{loadErr}</Alert>;
  if (!item) {
    return (
      <>
        <h1 className="page-title">Install</h1>
        <Alert kind="err">
          “{name}” is not in the catalog. <Link to="/catalog">Back to catalog</Link>.
        </Alert>
      </>
    );
  }

  if (result) {
    return (
      <InstallResultView
        name={name}
        result={result}
        onReset={() => setResult(null)}
      />
    );
  }

  return (
    <>
      <h1 className="page-title">Install {item.name}</h1>
      <p className="catalog-card__meta" style={{ margin: 0 }}>{item.image}</p>

      <div style={{ maxWidth: 560, marginTop: "var(--sp-6)" }}>
        <FormSection title="Networking">
          <Toggle checked={tailscale} onChange={setTailscale}>
            Own tailnet identity
          </Toggle>
          <Toggle
            checked={https && tailscale}
            onChange={setHttps}
          >
            HTTPS via <code>tailscale serve</code> — https://{item.name}.&lt;tailnet&gt;.ts.net
          </Toggle>
          <Toggle checked={npm} onChange={setNpm}>
            Bundle Nginx Proxy Manager
          </Toggle>
          {tailscale && (
            <Field
              label="Tailscale auth key"
              hint="Fresh single-use, non-ephemeral key. Leave blank to reuse an existing key file."
            >
              <input
                className="input"
                autoComplete="off"
                value={authkey}
                onChange={(e) => setAuthkey(e.target.value)}
              />
            </Field>
          )}
        </FormSection>

        {Object.keys(env).length > 0 && (
          <FormSection title="Environment">
            {Object.entries(env).map(([k, v]) => (
              <Field key={k} label={k}>
                <input
                  className="input"
                  value={v}
                  onChange={(e) => setEnv({ ...env, [k]: e.target.value })}
                />
              </Field>
            ))}
          </FormSection>
        )}

        {Object.keys(vols).length > 0 && (
          <FormSection title="Volumes">
            {Object.entries(vols).map(([cpath, host]) => (
              <Field key={cpath} label={`host path for ${cpath}`}>
                <input
                  className="input"
                  value={host}
                  onChange={(e) => setVols({ ...vols, [cpath]: e.target.value })}
                />
              </Field>
            ))}
          </FormSection>
        )}

        <SharePicker shares={shares} picked={picked} onChange={setPicked} />

        <div className="preview-row" style={{ marginTop: "var(--sp-5)" }}>
          <button
            className={"btn btn--primary" + (busy ? " btn--loading" : "")}
            disabled={busy}
            onClick={submit}
          >
            Install
          </button>
          <Link className="btn btn--ghost" to="/catalog">
            Cancel
          </Link>
        </div>
      </div>
    </>
  );
}
