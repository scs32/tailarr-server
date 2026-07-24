import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import type {
  BuiltinCatalog,
  CatalogItem,
  MagicStack,
  Source,
  StacksStatus,
} from "../types";
import { api } from "../api";
import { AddCustomPodModal } from "../components/AddCustomPodModal";
import { MagicStackWizard } from "../components/MagicStackWizard";
import { StackRail } from "../components/StackRail";
import { CatalogCard } from "../components/CatalogCard";
import { InstallModal } from "../components/InstallModal";
import { SourcesPanel } from "../components/SourcesPanel";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Alert } from "../components/Alert";
import { FlashView, useFlash } from "../components/Flash";
import {
  PlusIcon,
  RefreshIcon,
  SearchIcon,
  SpinnerIcon,
} from "../components/Icons";

export function Catalog() {
  // /install/<name> deep-links (e.g. from the Monitor page) land here with
  // the install popup already open.
  const { name: installParam } = useParams();
  const navigate = useNavigate();
  const [installing, setInstalling] = useState<string | null>(installParam ?? null);
  const [catalog, setCatalog] = useState<CatalogItem[] | null>(null);
  const [sources, setSources] = useState<Source[]>([]);
  const [catalogs, setCatalogs] = useState<BuiltinCatalog[]>([]);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [showSources, setShowSources] = useState(false);
  const [showAddCustom, setShowAddCustom] = useState(false);
  const [stacks, setStacks] = useState<StacksStatus | null>(null);
  const [wizardStack, setWizardStack] = useState<MagicStack | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [removeBusy, setRemoveBusy] = useState(false);
  const { flash, show, clear } = useFlash();

  const load = useCallback(async () => {
    try {
      const [c, s, st] = await Promise.all([
        api.catalog(),
        api.sources(),
        api.stacks().catch(() => null),
      ]);
      setCatalog(c);
      setSources(s.sources);
      setCatalogs(s.catalogs);
      setStacks(st);
      setError("");
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Refresh = re-fetch sources + catalog AND force a full image-update
  // check, staying busy until the server-side check actually finishes.
  async function refresh() {
    setRefreshing(true);
    try {
      await api.updatesRefresh().catch(() => {});
      const deadline = Date.now() + 120_000;
      let updates = null;
      while (Date.now() < deadline) {
        try {
          updates = await api.updates();
          if (!updates.checking) break;
        } catch {
          /* transient */
        }
        await new Promise((res) => setTimeout(res, 2000));
      }
      await load();
      if (updates) {
        const n = Object.values(updates.images).filter((i) => i.update).length;
        show({
          kind: "ok",
          text:
            n > 0
              ? `Update check complete — ${n} image${n === 1 ? "" : "s"} ha${n === 1 ? "s" : "ve"} updates (see the dashboard).`
              : "Update check complete — everything is up to date.",
        });
      }
    } finally {
      setRefreshing(false);
    }
  }

  async function confirmRemove() {
    if (!removing) return;
    setRemoveBusy(true);
    try {
      const r = await api.action(removing, "remove");
      show(
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
        Install a service — or add any OCI image as a custom pod.
      </p>

      {error && (
        <div style={{ marginTop: "var(--sp-5)" }}>
          <Alert kind="err">{error}</Alert>
        </div>
      )}
      <FlashView flash={flash} onClose={clear} />

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
        <button
          className="btn btn--sm"
          title="Describe any OCI image and add it to the catalog"
          onClick={() => setShowAddCustom(true)}
        >
          <PlusIcon className="btn-icon" />
          Custom pod
        </button>
      </div>

      {stacks && stacks.stacks.length > 0 && (
        <>
          <h2 className="section-title">Magic Stacks</h2>
          <div className="grid" style={{ marginBottom: "var(--sp-6)" }}>
            {stacks.stacks.map((st) => {
              const active =
                stacks.run?.state === "running" && stacks.run.stack === st.key;
              const blocked = !st.eligible && !active;
              return (
                <div key={st.key} className="card stack-card">
                  <span className="stack-card__name">{st.name}</span>
                  <p style={{ color: "var(--muted)", margin: 0 }}>{st.blurb}</p>
                  <StackRail services={st.services} />
                  <div className="preview-row" style={{ marginTop: "auto" }}>
                    <button
                      className="btn btn--primary btn--sm"
                      disabled={blocked}
                      title={
                        blocked && st.blockers.length
                          ? `Magic Stacks can't recreate existing services (${st.blockers.join(", ")})`
                          : blocked
                            ? "A stack setup is already running"
                            : "One short form — Tailarr wires everything else"
                      }
                      onClick={() => setWizardStack(st)}
                    >
                      {active ? "View progress" : "Set up"}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </>
      )}

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
              onInstall={setInstalling}
              onRemove={setRemoving}
              onDeleteCustom={async (name) => {
                const r = await api.customPodDelete(name);
                show(
                  r.ok
                    ? { kind: "ok", text: `Removed ${name} from the catalog.` }
                    : { kind: "err", text: r.error ?? "Delete failed." },
                );
                await load();
              }}
            />
          ))}
        </div>
      )}

      {installing && (
        <InstallModal
          name={installing}
          onClose={() => {
            setInstalling(null);
            if (installParam) navigate("/catalog", { replace: true });
          }}
          onChanged={load}
        />
      )}

      {wizardStack && (
        <MagicStackWizard
          stack={wizardStack}
          initialRun={stacks?.run ?? null}
          onClose={() => {
            setWizardStack(null);
            load();
          }}
          onChanged={load}
        />
      )}

      {showAddCustom && (
        <AddCustomPodModal
          onSaved={() => {
            show({
              kind: "ok",
              text: "Added to the catalog under the custom source — install it from its card.",
            });
            load();
          }}
          onClose={() => setShowAddCustom(false)}
        />
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
            <SourcesPanel sources={sources} catalogs={catalogs} onChanged={load} />
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
