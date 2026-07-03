import { Link } from "react-router-dom";
import type { CatalogItem } from "../types";
import { PodGlyph } from "./Icons";

export function CatalogCard({ item }: { item: CatalogItem }) {
  return (
    <div className="catalog-card card">
      <div className="catalog-card__head">
        <div className="pod-icon">
          <PodGlyph />
        </div>
        <div>
          <div className="pod-card__title" style={{ fontSize: "var(--fs-base)" }}>
            {item.name}
          </div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 2 }}>
            {item.installed ? (
              <span className="chip chip--installed">Installed</span>
            ) : (
              item.port && <span className="chip">port {item.port}</span>
            )}
            {item.source !== "built-in" && (
              <span className="chip" title={`from source: ${item.source}`}>
                {item.source}
              </span>
            )}
          </div>
        </div>
        <div className="spacer" />
        <Link className="btn btn--primary btn--sm" to={`/install/${item.name}`}>
          {item.installed ? "Reinstall" : "Install"}
        </Link>
      </div>
      <div className="catalog-card__meta">{item.image}</div>
    </div>
  );
}
