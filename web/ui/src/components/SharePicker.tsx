import { Link } from "react-router-dom";
import type { Share } from "../types";
import { FormSection } from "./Form";

export function SharePicker({
  shares,
  picked,
  onChange,
}: {
  shares: Share[];
  picked: string[];
  onChange: (names: string[]) => void;
}) {
  function toggle(name: string, on: boolean) {
    onChange(on ? [...picked, name] : picked.filter((n) => n !== name));
  }

  return (
    <FormSection title="Shared folders">
      {shares.length === 0 ? (
        <p className="field__hint" style={{ margin: 0 }}>
          None defined — <Link to="/shares">add shared folders</Link>.
        </p>
      ) : (
        shares.map((s) => (
          <label
            key={s.name}
            className="toggle"
            // one share per line (.toggle is inline-flex by default)
            style={{ display: "flex", marginBottom: "var(--sp-3)", alignItems: "flex-start" }}
          >
            <input
              type="checkbox"
              checked={picked.includes(s.name)}
              onChange={(e) => toggle(s.name, e.target.checked)}
            />
            <span className="toggle__track" />
            <span>
              {s.name}{" "}
              <span className="field__hint">
                ({s.host_path}
                {s.ro ? " :ro" : ""} → {s.container_path})
              </span>
            </span>
          </label>
        ))
      )}
    </FormSection>
  );
}
