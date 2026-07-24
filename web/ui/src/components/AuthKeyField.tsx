import { useState } from "react";
import { Alert } from "./Alert";
import { Field } from "./Form";
import { TsApiWizard } from "./TsApiWizard";

// The install forms' auth-key affordance. With an API credential on the
// controller, keys are minted automatically and the field collapses into an
// advanced override; without one, the field is front-and-center and the
// credential wizard is one click away (Feature A's deploy-time trigger).
export function AuthKeyField({
  configured,
  value,
  onChange,
  onConfigured,
}: {
  configured: boolean | null; // null = not known yet
  value: string;
  onChange: (v: string) => void;
  onConfigured: () => void;
}) {
  const [wizardOpen, setWizardOpen] = useState(false);

  if (configured) {
    return (
      <>
        <p className="field__hint" style={{ margin: 0 }}>
          ✓ An enrollment key is generated automatically for this service —
          nothing to paste.
        </p>
        <details style={{ marginTop: "var(--sp-2)" }}>
          <summary className="field__hint" style={{ cursor: "pointer" }}>
            Advanced: use a specific auth key
          </summary>
          <Field
            label="Tailscale auth key (override)"
            hint="Leave blank to generate one automatically."
          >
            <input
              className="input"
              autoComplete="off"
              value={value}
              onChange={(e) => onChange(e.target.value)}
            />
          </Field>
        </details>
      </>
    );
  }

  return (
    <>
      <Field
        label="Tailscale auth key"
        hint="Fresh single-use, non-ephemeral key. Leave blank only if this service already has enrolled Tailscale state."
      >
        <input
          className="input"
          autoComplete="off"
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      </Field>
      {configured === false && (
        <div style={{ marginTop: "var(--sp-2)" }}>
          <Alert kind="info">
            Tired of pasting keys? Configure the Tailscale API once and
            Tailarr mints them automatically for every deploy.{" "}
            <button
              className="btn btn--ghost btn--sm"
              onClick={() => setWizardOpen((v) => !v)}
            >
              {wizardOpen ? "Hide setup" : "Set up now"}
            </button>
          </Alert>
          {wizardOpen && (
            <TsApiWizard
              onDone={() => {
                setWizardOpen(false);
                onConfigured();
              }}
            />
          )}
        </div>
      )}
    </>
  );
}
