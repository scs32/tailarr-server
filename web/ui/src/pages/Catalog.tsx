import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import type { CatalogItem } from "../types";
import { api } from "../api";
import { CatalogCard } from "../components/CatalogCard";
import { Alert } from "../components/Alert";

export function Catalog() {
  const [catalog, setCatalog] = useState<CatalogItem[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api.catalog().then(setCatalog).catch((e) => setError(String(e)));
  }, []);

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

      <div className="section-title">Available services</div>
      <div className="grid">
        {catalog?.map((item) => (
          <CatalogCard key={item.name} item={item} />
        ))}
      </div>
    </>
  );
}
