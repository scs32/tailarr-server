import type { ReactNode } from "react";
import { SpinnerIcon } from "./Icons";

export function ConfirmDialog({
  title,
  children,
  confirmLabel = "Confirm",
  busy = false,
  onConfirm,
  onCancel,
}: {
  title: string;
  children: ReactNode;
  confirmLabel?: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="scrim" onClick={busy ? undefined : onCancel}>
      <div className="dialog card" onClick={(e) => e.stopPropagation()}>
        <h3 className="dialog__title">{title}</h3>
        <div className="dialog__body">{children}</div>
        <div className="dialog__foot">
          <button className="btn btn--ghost" disabled={busy} onClick={onCancel}>
            Cancel
          </button>
          <button
            className={"btn btn--danger" + (busy ? " btn--loading" : "")}
            disabled={busy}
            onClick={onConfirm}
          >
            {busy && <SpinnerIcon className="btn-icon" />}
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
