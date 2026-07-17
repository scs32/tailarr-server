import { useEffect, useState } from "react";
import type { CatalogItem, InstallResult, Share } from "../types";
import { api } from "../api";
import { Field, FormSection } from "./Form";
import { Alert } from "./Alert";
import {
  FolderEditor,
  rowsToVolumes,
  volumesToRows,
  type FolderRow,
} from "./FolderEditor";
import { SharePicker } from "./SharePicker";
import { InstallResultView } from "./InstallResultView";
import { AuthKeyField } from "./AuthKeyField";
import { SpinnerIcon } from "./Icons";

// Install popup for a catalog service — the same affordance as the pod Edit
// popup. After the pod is generated it swaps to the live install stepper
// (InstallResultView in embedded mode).
export function InstallModal({
  name,
  onClose,
  onChanged,
}: {
  name: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [item, setItem] = useState<CatalogItem | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadErr, setLoadErr] = useState("");
  const [shares, setShares] = useState<Share[]>([]);

  const [env, setEnv] = useState<Record<string, string>>({});
  const [folders, setFolders] = useState<FolderRow[]>([]);
  const [picked, setPicked] = useState<string[]>([]);
  const [authkey, setAuthkey] = useState("");
  const [tsapiOk, setTsapiOk] = useState<boolean | null>(null);

  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<InstallResult | null>(null);

  useEffect(() => {
    let live = true;
    Promise.all([api.catalog(), api.info(), api.shares()])
      .then(([catalog, info, sh]) => {
        if (!live) return;
        const found = catalog.find((c) => c.name === name) ?? null;
        setItem(found);
        setShares(sh);
        setTsapiOk(info.tsapi.configured);
        if (found) {
          setEnv({ ...found.environment });
          const v: Record<string, string> = {};
          for (const cpath of Object.values(found.volumes)) {
            v[cpath] = `${info.pods_dir}/${name}/${cpath.replace(/^\//, "")}`;
          }
          setFolders(volumesToRows(v));
        }
      })
      .catch((e) => live && setLoadErr(String(e)))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [name]);

  async function submit() {
    setBusy(true);
    try {
      const r = await api.install({
        service: name,
        environment: env,
        volumes: rowsToVolumes(folders),
        shares: picked,
        authkey,
      });
      setResult(r);
      if (r.ok) onChanged();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="scrim" onClick={busy ? undefined : onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal__head">
          <span className="modal__title">
            {result?.ok ? `Installed ${name}` : `Install ${name}`}
          </span>
          <div className="spacer" />
          <button className="btn btn--ghost btn--sm" disabled={busy} onClick={onClose}>
            Close
          </button>
        </div>

        {loading ? (
          <p className="field__hint" style={{ margin: 0 }}>
            Loading…
          </p>
        ) : loadErr ? (
          <Alert kind="err">{loadErr}</Alert>
        ) : !item ? (
          <Alert kind="err">“{name}” is not in the catalog.</Alert>
        ) : result ? (
          <InstallResultView
            name={name}
            result={result}
            onReset={() => setResult(null)}
            embedded
          />
        ) : (
          <>
            <p className="catalog-card__meta" style={{ margin: "0 0 var(--sp-4)" }}>
              {item.image}
            </p>

            <FormSection title="Networking">
              <p className="field__hint" style={{ margin: 0 }}>
                Every pod gets its own tailnet identity with HTTPS via{" "}
                <code>tailscale serve</code> — https://{item.name}
                .&lt;tailnet&gt;.ts.net.
              </p>
              <AuthKeyField
                configured={tsapiOk}
                value={authkey}
                onChange={setAuthkey}
                onConfigured={() => setTsapiOk(true)}
              />
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

            <FolderEditor rows={folders} onChange={setFolders} />

            <SharePicker shares={shares} picked={picked} onChange={setPicked} />

            <div className="preview-row" style={{ marginTop: "var(--sp-5)" }}>
              <button
                className={"btn btn--primary" + (busy ? " btn--loading" : "")}
                disabled={busy}
                onClick={submit}
              >
                {busy && <SpinnerIcon className="btn-icon" />}
                Install
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
