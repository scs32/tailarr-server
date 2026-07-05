// Typed client for the controller JSON API. Same-origin fetch; the dev server
// proxies /api to a locally-running app.py (see vite.config.ts).

import type {
  ActionResult,
  BackupEntry,
  CatalogItem,
  FleetAction,
  FleetResult,
  Info,
  InstallRequest,
  InstallResult,
  MonitorStatus,
  NetworkEntry,
  Pod,
  PodConfigResult,
  ReconfigureRequest,
  Share,
  ShareResult,
  Source,
  UpdatesInfo,
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

  exec: (name: string, cmd: string) =>
    postJSON<ActionResult>(`/api/pods/${name}/exec`, { cmd }),

  backups: (name: string) =>
    getJSON<{ name: string; backups: BackupEntry[] }>(
      `/api/pods/${name}/backups`,
    ).then((d) => d.backups),

  backupCreate: (name: string, reason = "") =>
    postJSON<ActionResult & { backup?: BackupEntry }>(
      `/api/pods/${name}/backups`,
      { reason },
    ),

  backupRestore: (name: string, ts: string) =>
    postJSON<ActionResult>(`/api/pods/${name}/backups/restore`, { ts }),

  backupDelete: (name: string, ts: string) =>
    postJSON<ActionResult>(`/api/pods/${name}/backups/delete`, { ts }),

  action: (name: string, action: "start" | "stop" | "update" | "remove") =>
    postJSON<ActionResult>(`/api/pods/${name}/action`, { do: action }),

  fleet: (action: FleetAction) =>
    postJSON<FleetResult>("/api/fleet", { do: action }),

  podConfig: (name: string) =>
    getJSON<PodConfigResult>(`/api/pods/${name}/config`),

  reconfigure: (name: string, body: ReconfigureRequest) =>
    postJSON<ActionResult>(`/api/pods/${name}/config`, body),

  updates: () => getJSON<UpdatesInfo>("/api/updates"),

  updatesRefresh: () =>
    postJSON<{ ok: boolean; status: string }>("/api/updates/refresh", {}),

  network: () =>
    getJSON<{ network: NetworkEntry[] }>("/api/network").then((d) => d.network),

  networkSet: (pod: string, body: { tailscale?: boolean; https?: boolean }) =>
    postJSON<ActionResult>(`/api/network/${pod}`, body),

  monitor: () => getJSON<MonitorStatus>("/api/monitor"),

  monitorSetup: (body: { url: string; username: string; password: string }) =>
    postJSON<{ ok: boolean; error: string | null; fresh?: boolean }>(
      "/api/monitor/setup",
      body,
    ),

  monitorPod: (name: string, action: "add" | "remove") =>
    postJSON<{ ok: boolean; name: string; error: string | null }>(
      `/api/monitor/pods/${name}`,
      { do: action },
    ),

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
