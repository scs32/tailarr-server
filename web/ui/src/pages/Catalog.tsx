import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import type { CatalogItem, Source } from "../types";
import { api } from "../api";
import { CatalogCard } from "../components/CatalogCard";
import { SourcesPanel } from "../components/SourcesPanel";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Alert } from "../components/Alert";
import { RefreshIcon, SearchIcon, SpinnerIcon } from "../components/Icons";

export function Catalog() {
  const [catalog, setCatalog] = useState<CatalogItem[] | null>(null);
  const [sources, setSources] = useState<Source[]>([]);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [showSources, setShowSources] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [removeBusy, setRemoveBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const load = useCallback(async () => {
    try {
      const [c, s] = await Promise.all([api.catalog(), api.sources()]);
      setCatalog(c);
      setSources(s);
      setError("");
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Refresh = re-fetch sources + catalog AND force a full image-update check.
  async function refresh() {
    setRefreshing(true);
    try {
      await Promise.all([load(), api.updatesRefresh().catch(() => {})]);
    } finally {
      setRefreshing(false);
    }
  }

  async function confirmRemove() {
    if (!removing) return;
    setRemoveBusy(true);
    try {
      const r = await api.action(removing, "remove");
      setMsg(
        r.ok
          ? { kind: "ok", text: `Removed ${removing}.` }
          : { kind: "err", text: r.error ?? r.output ?? "Remove failed." },
      );
      setRemoving(null);
      await load();
    } finally {
      setRemoveBusy(false);
    }
  }

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return catalog ?? [];
    return (catalog ?? []).filter(
      (i) =>
        i.name.toLowerCase().includes(q) ||
        i.image.toLowerCase().includes(q) ||
        i.source.toLowerCase().includes(q),
    );
  }, [catalog, query]);

  return (
    <>
      <h1 className="page-title">Catalog</h1>
      <p style={{ color: "var(--muted)", margin: 0 }}>
        Install a service, or{" "}
        <Link to="/custom">deploy any OCI image as a custom pod</Link>.
      </p>

      {error && (
        <div style={{ marginTop: "var(--sp-5)" }}>
          <Alert kind="err">{error}</Alert>
        </div>
      )}
      {msg && (
        <div style={{ marginTop: "var(--sp-5)" }}>
          <Alert kind={msg.kind}>{msg.text}</Alert>
        </div>
      )}

      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "var(--sp-3)",
          margin: "var(--sp-6) 0 var(--sp-4)",
        }}
      >
        <div style={{ position: "relative", flex: 1, maxWidth: 340 }}>
          <SearchIcon
            style={{
              position: "absolute",
              left: 11,
              top: "50%",
              transform: "translateY(-50%)",
              width: 16,
              height: 16,
              color: "var(--faint)",
              pointerEvents: "none",
            }}
          />
          <input
            className="input"
            style={{ paddingLeft: 34 }}
            placeholder="Search services…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            autoFocus
            aria-label="Search the catalog"
          />
        </div>
        {catalog && (
          <span className="preview-label">
            {query.trim()
              ? `${filtered.length} of ${catalog.length}`
              : `${catalog.length} services`}
          </span>
        )}
        <div className="spacer" />
        <button
          className={"btn btn--ghost btn--sm" + (refreshing ? " btn--loading" : "")}
          disabled={refreshing}
          title="Reload the catalog and check all images for updates"
          onClick={refresh}
        >
          {refreshing ? (
            <SpinnerIcon className="btn-icon" />
          ) : (
            <RefreshIcon className="btn-icon" />
          )}
          Refresh
        </button>
        <button
          className="btn btn--ghost btn--sm"
          onClick={() => setShowSources(true)}
        >
          Sources{sources.length ? ` (${sources.length})` : ""}
        </button>
      </div>

      {catalog && filtered.length === 0 ? (
        <p style={{ color: "var(--muted)" }}>
          No services match “{query.trim()}”.
        </p>
      ) : (
        <div className="grid">
          {filtered.map((item) => (
            <CatalogCard
              key={`${item.source}/${item.name}`}
              item={item}
              onRemove={setRemoving}
            />
          ))}
        </div>
      )}

      {showSources && (
        <div className="scrim" onClick={() => setShowSources(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal__head">
              <span className="modal__title">Catalog sources</span>
              <div className="spacer" />
              <button
                className="btn btn--ghost btn--sm"
                onClick={() => setShowSources(false)}
              >
                Close
              </button>
            </div>
            <SourcesPanel sources={sources} onChanged={load} />
          </div>
        </div>
      )}

      {removing && (
        <ConfirmDialog
          title={`Remove ${removing}?`}
          confirmLabel="Remove"
          busy={removeBusy}
          onConfirm={confirmRemove}
          onCancel={() => setRemoving(null)}
        >
          This stops the pod and permanently deletes its directory — config,
          data stored under the pod, and its Tailscale identity. Media in
          shared folders is not touched.
        </ConfirmDialog>
      )}
    </>
  );
}
