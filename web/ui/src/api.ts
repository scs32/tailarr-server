// Typed client for the controller JSON API. Same-origin fetch; the dev server
// proxies /api to a locally-running app.py (see vite.config.ts).

import type {
  ActionResult,
  CatalogItem,
  Info,
  InstallRequest,
  InstallResult,
  Pod,
  Share,
  ShareResult,
  Source,
} from "./types";

async function getJSON<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json() as Promise<T>;
}

async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  // The API returns a structured body on 4xx/5xx too; surface it to the caller.
  return (await r.json()) as T;
}

export const api = {
  info: () => getJSON<Info>("/api/info"),
  pods: () => getJSON<{ pods: Pod[] }>("/api/pods").then((d) => d.pods),
  catalog: () =>
    getJSON<{ catalog: CatalogItem[] }>("/api/catalog").then((d) => d.catalog),
  shares: () => getJSON<{ shares: Share[] }>("/api/shares").then((d) => d.shares),

  logs: (name: string) => getJSON<ActionResult>(`/api/pods/${name}/logs`),

  action: (name: string, action: "start" | "stop" | "update") =>
    postJSON<ActionResult>(`/api/pods/${name}/action`, { do: action }),

  install: (req: InstallRequest) =>
    postJSON<InstallResult>("/api/install", req),

  shareAdd: (name: string, host_path: string, container_path: string, ro: boolean) =>
    postJSON<ShareResult>("/api/shares", {
      do: "add",
      name,
      host_path,
      container_path,
      ro,
    }),

  shareDelete: (name: string) =>
    postJSON<ShareResult>("/api/shares", { do: "delete", name }),

  shareAttach: (pod: string, share: string) =>
    postJSON<ShareResult>("/api/shares", { do: "attach", pod, share }),

  sources: () => getJSON<{ sources: Source[] }>("/api/sources").then((d) => d.sources),

  sourceAdd: (name: string, url: string) =>
    postJSON<ShareResult>("/api/sources", { do: "add", name, url }),

  sourceDelete: (name: string) =>
    postJSON<ShareResult>("/api/sources", { do: "delete", name }),
};
