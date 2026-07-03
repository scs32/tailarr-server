import { useEffect, useState } from "react";
import { api } from "../api";

export function LogsModal({ name, onClose }: { name: string; onClose: () => void }) {
  const [text, setText] = useState("Loading…");

  useEffect(() => {
    let live = true;
    api
      .logs(name)
      .then((r) => live && setText(r.output || "(no output)"))
      .catch((e) => live && setText(String(e)));
    return () => {
      live = false;
    };
  }, [name]);

  return (
    <div className="scrim" onClick={onClose}>
      <div
        className="log"
        style={{ width: "min(720px, 100%)" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="log__bar">
          <span className="log__dot" />
          <span className="log__name">{name} · last 100 lines</span>
          <div className="spacer" />
          <button className="btn btn--ghost btn--sm" onClick={onClose}>
            Close
          </button>
        </div>
        <div className="log__body">{text}</div>
      </div>
    </div>
  );
}
