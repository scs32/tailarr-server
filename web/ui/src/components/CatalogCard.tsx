import { Link } from "react-router-dom";
import type { CatalogItem } from "../types";
import { PodGlyph } from "./Icons";

// Installed state is conveyed by the card tint (green running / amber
// stopped / red crashed) — see .catalog-card--* in the stylesheet.
export function CatalogCard({
  item,
  onRemove,
}: {
  item: CatalogItem;
  onRemove: (name: string) => void;
}) {
  const stateClass = item.installed && item.state ? ` catalog-card--${item.state}` : "";
  return (
    <div className={`catalog-card card${stateClass}`}>
      <div className="catalog-card__head">
        <div className="pod-icon">
          <PodGlyph />
        </div>
        <div>
          <div className="pod-card__title" style={{ fontSize: "var(--fs-base)" }}>
            {item.name}
          </div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 2 }}>
            {!item.installed && item.port && (
              <span className="chip">port {item.port}</span>
            )}
            {item.source !== "built-in" && (
              <span className="chip" title={`from source: ${item.source}`}>
                {item.source}
              </span>
            )}
          </div>
        </div>
        <div className="spacer" />
        {item.installed ? (
          <button
            className="btn btn--danger btn--sm"
            onClick={() => onRemove(item.name)}
          >
            Remove
          </button>
        ) : (
          <Link className="btn btn--primary btn--sm" to={`/install/${item.name}`}>
            Install
          </Link>
        )}
      </div>
      <div className="catalog-card__meta">{item.image}</div>
    </div>
  );
}
