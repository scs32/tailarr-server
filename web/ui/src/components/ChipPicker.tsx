import { useEffect, useRef, useState } from "react";
import { SpinnerIcon } from "./Icons";

export interface PickerOption {
  id: string;
  label?: string;
  hint?: string;
}

// The shared picking affordance (Users / Monitor / Shares): what's assigned
// shows as removable chips; "+ Add" opens a searchable popover of the rest.
// Scales past checkbox rows — only granted items take space, search handles
// long option lists, and a chip can later carry state (expiry, pending…).
export function ChipPicker({
  chips,
  options,
  onAdd,
  onRemove,
  addLabel = "+ Add",
  emptyHint,
  busyId = "",
  disabled = false,
}: {
  chips: string[]; // assigned option ids, shown as removable chips
  options: PickerOption[]; // the full universe (assigned ones are marked)
  onAdd: (id: string) => void;
  onRemove: (id: string) => void;
  addLabel?: string;
  emptyHint?: string; // shown when there are no options at all
  busyId?: string; // option currently being applied (spinner + lock)
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const rootRef = useRef<HTMLDivElement | null>(null);

  // close on outside click / Escape
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const byId = new Map(options.map((o) => [o.id, o]));
  const q = query.trim().toLowerCase();
  const listed = options.filter(
    (o) => !q || o.id.toLowerCase().includes(q) || (o.label ?? "").toLowerCase().includes(q),
  );

  return (
    <div className="chip-picker" ref={rootRef}>
      {chips.map((id) => (
        <span key={id} className="chip chip--installed chip--removable">
          {busyId === id && <SpinnerIcon className="chip__spin" />}
          {byId.get(id)?.label ?? id}
          <button
            className="chip__x"
            title="Remove"
            disabled={disabled || !!busyId}
            onClick={() => onRemove(id)}
          >
            ×
          </button>
        </span>
      ))}

      {options.length === 0 ? (
        emptyHint && <span className="field__hint">{emptyHint}</span>
      ) : (
        <button
          className="chip chip--add"
          disabled={disabled || !!busyId}
          onClick={() => {
            setOpen((v) => !v);
            setQuery("");
          }}
        >
          {busyId && !chips.includes(busyId) && <SpinnerIcon className="chip__spin" />}
          {addLabel}
        </button>
      )}

      {open && (
        <div className="picker-pop card">
          {options.length > 6 && (
            <input
              className="input picker-pop__search"
              placeholder="Search…"
              autoFocus
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          )}
          <div className="picker-pop__list">
            {listed.length === 0 ? (
              <span className="field__hint" style={{ padding: "var(--sp-2)" }}>
                No matches.
              </span>
            ) : (
              listed.map((o) => {
                const on = chips.includes(o.id);
                return (
                  <button
                    key={o.id}
                    className={"picker-item" + (on ? " picker-item--on" : "")}
                    disabled={disabled || !!busyId}
                    onClick={() => (on ? onRemove(o.id) : onAdd(o.id))}
                  >
                    <span className="picker-item__mark">{on ? "✓" : ""}</span>
                    <span>{o.label ?? o.id}</span>
                    {o.hint && <span className="field__hint">{o.hint}</span>}
                  </button>
                );
              })
            )}
          </div>
        </div>
      )}
    </div>
  );
}
