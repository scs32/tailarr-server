import { Link } from "react-router-dom";
import type { Share } from "../types";
import { FormSection } from "./Form";
import { ChipPicker } from "./ChipPicker";

// Attach shared folders to a pod. Same picking affordance as Users/Monitor
// (chips + searchable add), but batched: changes land in the form state and
// apply on Reload/Update, matching the rest of the edit popup.
export function SharePicker({
  shares,
  picked,
  onChange,
}: {
  shares: Share[];
  picked: string[];
  onChange: (names: string[]) => void;
}) {
  return (
    <FormSection title="Shared folders">
      {shares.length === 0 ? (
        <p className="field__hint" style={{ margin: 0 }}>
          None defined — <Link to="/shares">add shared folders</Link>.
        </p>
      ) : (
        <>
          <ChipPicker
            chips={picked}
            options={shares.map((s) => ({
              id: s.name,
              hint: `${s.host_path}${s.ro ? " :ro" : ""} → ${s.container_path}`,
            }))}
            onAdd={(name) => onChange([...picked, name])}
            onRemove={(name) => onChange(picked.filter((n) => n !== name))}
            addLabel="+ Attach share"
          />
          <p className="field__hint" style={{ margin: "var(--sp-2) 0 0" }}>
            Applied when you Reload or Update the pod.
          </p>
        </>
      )}
    </FormSection>
  );
}
