import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import type { InstallResult, Share } from "../types";
import { api } from "../api";
import { Field, FormSection, Toggle } from "../components/Form";
import { SharePicker } from "../components/SharePicker";
import { InstallResultView } from "../components/InstallResultView";

function parsePairs(text: string, sep: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const i = line.indexOf(sep);
    if (i === -1) continue;
    const k = line.slice(0, i).trim();
    const v = line.slice(i + sep.length).trim();
    if (k) out[k] = v;
  }
  return out;
}

function parseVolumes(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [cpath, hpath] of Object.entries(parsePairs(text, "="))) {
    if (cpath.startsWith("/") && hpath.startsWith("/")) out[cpath] = hpath;
  }
  return out;
}

const NAME_RE = /^[a-z0-9][a-z0-9-]*$/;

export function CustomPod() {
  const [shares, setShares] = useState<Share[]>([]);
  const [name, setName] = useState("");
  const [image, setImage] = useState("");
  const [command, setCommand] = useState("");
  const [portsText, setPortsText] = useState("");
  const [envText, setEnvText] = useState("");
  const [volsText, setVolsText] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [tailscale, setTailscale] = useState(true);
  const [https, setHttps] = useState(true);
  const [authkey, setAuthkey] = useState("");

  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<InstallResult | null>(null);

  useEffect(() => {
    api.shares().then(setShares).catch(() => {});
  }, []);

  const nameErr =
    name && !NAME_RE.test(name) ? "Lowercase letters, digits, dashes." : "";
  const canSubmit = NAME_RE.test(name) && image.trim() !== "" && !busy;

  async function submit() {
    setBusy(true);
    setResult(null);
    try {
      setResult(
        await api.install({
          custom: true,
          service: name,
          image: image.trim(),
          command: command.trim(),
          ports: parsePairs(portsText, ":"),
          environment: parsePairs(envText, "="),
          volumes: parseVolumes(volsText),
          shares: picked,
          tailscale,
          https,
          authkey,
        }),
      );
    } finally {
      setBusy(false);
    }
  }

  if (result) {
    return (
      <InstallResultView name={name} result={result} onReset={() => setResult(null)} />
    );
  }

  return (
    <>
      <h1 className="page-title">Custom pod</h1>
      <p style={{ color: "var(--muted)", margin: 0 }}>
        Deploy any OCI image as a pod on your tailnet.
      </p>

      <div style={{ maxWidth: 560, marginTop: "var(--sp-6)" }}>
        <FormSection title="Image">
          <Field label="Name" hint="a–z, 0–9, dashes" error={nameErr}>
            <input
              className="input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="jellyfin"
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
          <Field label="Ports" hint="one host:container per line">
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
          <Field
            label="Volumes"
            hint="one /container/path=/host/path per line · append :ro to a host path for read-only"
          >
            <textarea
              className="textarea"
              rows={3}
              value={volsText}
              onChange={(e) => setVolsText(e.target.value)}
              placeholder="/config=/root/Pods/jellyfin/config"
            />
          </Field>
        </FormSection>

        <FormSection title="Networking">
          <Toggle checked={tailscale} onChange={setTailscale}>
            Own tailnet identity
          </Toggle>
          <Toggle checked={https && tailscale} onChange={setHttps}>
            HTTPS via <code>tailscale serve</code> (first port)
          </Toggle>
          {tailscale && (
            <Field label="Tailscale auth key">
              <input
                className="input"
                autoComplete="off"
                value={authkey}
                onChange={(e) => setAuthkey(e.target.value)}
              />
            </Field>
          )}
        </FormSection>

        <SharePicker shares={shares} picked={picked} onChange={setPicked} />

        <div className="preview-row" style={{ marginTop: "var(--sp-5)" }}>
          <button
            className={"btn btn--primary" + (busy ? " btn--loading" : "")}
            disabled={!canSubmit}
            onClick={submit}
          >
            Install
          </button>
          <Link className="btn btn--ghost" to="/">
            Cancel
          </Link>
        </div>
      </div>
    </>
  );
}
