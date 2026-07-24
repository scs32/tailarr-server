// Typed client for the controller JSON API. Same-origin fetch; the dev server
// proxies /api to a locally-running app.py (see vite.config.ts).

import type {
  AccountsStatus,
  ActionResult,
  BackupEntry,
  BuiltinCatalog,
  CatalogItem,
  FleetAction,
  FleetResult,
  FsListResult,
  FsMkdirResult,
  Info,
  InstallRequest,
  InstallResult,
  MonitorStatus,
  NetworkEntry,
  NtfyStatus,
  Pod,
  PodConfigResult,
  ReconfigureRequest,
  RegistriesStatus,
  RelayAction,
  RelayActionResult,
  RelayDevice,
  RelayStatus,
  SetupStatus,
  Share,
  ShareResult,
  Source,
  StackInputs,
  StackInstallResult,
  StacksStatus,
  StackValidateResult,
  StatsSnapshot,
  TokensStatus,
  TsApiCredential,
  TsApiProbe,
  TsApiStatus,
  UpdatesInfo,
  UpgradeResult,
  UpgradeStatus,
  UsersStatus,
} from "./types";

// When the controller has "require API tokens" on, every /api/* call needs
// a Bearer token. This browser's copy lives in localStorage — paste/mint it
// under Settings → API access.
const TOKEN_KEY = "tailarr.apitoken";

export function getStoredToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setStoredToken(token: string) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    // private mode etc. — the header just won't persist across reloads
  }
}

function authHeaders(): Record<string, string> {
  const t = getStoredToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

async function getJSON<T>(path: string): Promise<T> {
  const r = await fetch(path, { headers: authHeaders() });
  if (r.status === 401)
    throw new Error("This Tailarr requires an API token.");
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json() as Promise<T>;
}

async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });
  // The API returns a structured body on 4xx/5xx too; surface it to the caller.
  return (await r.json()) as T;
}

export const api = {
  info: () => getJSON<Info>("/api/info"),
  serverSave: (name: string) =>
    postJSON<{ ok: boolean; error: string | null; name: string }>(
      "/api/server",
      { name },
    ),
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

  relay: () => getJSON<RelayStatus>("/api/relay"),

  relayAction: (action: RelayAction) =>
    postJSON<RelayActionResult>("/api/relay", action),

  relayDevices: () =>
    getJSON<{ ok: boolean; error: string | null; devices: RelayDevice[] }>(
      "/api/relay/devices",
    ),

  upgradeStatus: () => getJSON<UpgradeStatus>("/api/controller/upgrade"),

  upgradeCheck: () =>
    postJSON<UpgradeStatus>("/api/controller/upgrade/check", {}),

  upgrade: (version?: string) =>
    postJSON<UpgradeResult>(
      "/api/controller/upgrade",
      version ? { version } : {},
    ),

  podConfig: (name: string) =>
    getJSON<PodConfigResult>(`/api/pods/${name}/config`),

  reconfigure: (name: string, body: ReconfigureRequest) =>
    postJSON<ActionResult>(`/api/pods/${name}/config`, body),

  updates: () => getJSON<UpdatesInfo>("/api/updates"),

  updatesRefresh: () =>
    postJSON<{ ok: boolean; status: string }>("/api/updates/refresh", {}),

  network: () =>
    getJSON<{ network: NetworkEntry[] }>("/api/network").then((d) => d.network),

  networkSet: (pod: string, body: { funnel: boolean }) =>
    postJSON<ActionResult>(`/api/network/${pod}`, body),

  monitor: () => getJSON<MonitorStatus>("/api/monitor"),

  ntfy: () => getJSON<NtfyStatus>("/api/ntfy"),

  stacks: () => getJSON<StacksStatus>("/api/stacks"),
  stackValidate: (body: StackInputs) =>
    postJSON<StackValidateResult>("/api/stacks", { ...body, do: "validate" }),
  stackInstall: (body: StackInputs) =>
    postJSON<StackInstallResult>("/api/stacks", { ...body, do: "install" }),

  ntfySetup: () =>
    postJSON<{
      ok: boolean;
      error: string | null;
      test_error?: string | null;
      funnel_error?: string | null;
      gateway_error?: string | null;
      status?: NtfyStatus;
    }>("/api/ntfy/setup", {}),

  ntfyFunnel: (enabled: boolean) =>
    postJSON<{ ok: boolean; error: string | null; status?: NtfyStatus }>(
      "/api/ntfy/funnel",
      { enabled },
    ),

  ntfyTest: () =>
    postJSON<{ ok: boolean; error: string | null }>("/api/ntfy/test", {}),

  ntfyWire: (pod: string) =>
    postJSON<{
      ok: boolean;
      error: string | null;
      topic?: string;
      recipe?: {
        server: string;
        username: string;
        password: string;
        topic: string;
      };
    }>(`/api/ntfy/wire/${pod}`, {}),

  stats: () => getJSON<StatsSnapshot>("/api/stats"),

  users: () => getJSON<UsersStatus>("/api/users"),

  userNick: (id: string, nickname: string) =>
    postJSON<{ ok: boolean; error: string | null }>(`/api/users/${id}`, {
      nickname,
    }),

  userRevoke: (id: string) =>
    postJSON<{ ok: boolean; error: string | null }>(
      `/api/users/${id}/revoke`,
      {},
    ),

  userKey: () =>
    postJSON<{ ok: boolean; error: string | null; key: string }>(
      "/api/users/keys",
      {},
    ),

  registries: () => getJSON<RegistriesStatus>("/api/registries"),

  registrySave: (registry: string, username: string, secret: string) =>
    postJSON<{ ok: boolean; error: string | null }>("/api/registries", {
      do: "save",
      registry,
      username,
      secret,
    }),

  registryDelete: (registry: string) =>
    postJSON<{ ok: boolean; error: string | null }>("/api/registries", {
      do: "delete",
      registry,
    }),

  accounts: () => getJSON<AccountsStatus>("/api/accounts"),

  accountSave: (body: Record<string, unknown>) =>
    postJSON<{
      ok: boolean;
      error: string | null;
      id?: string;
      status?: AccountsStatus;
    }>("/api/accounts", { do: "save", ...body }),

  accountDelete: (id: string) =>
    postJSON<{ ok: boolean; error: string | null; status?: AccountsStatus }>(
      "/api/accounts",
      { do: "delete", id },
    ),

  tokens: () => getJSON<TokensStatus>("/api/tokens"),

  tokenCreate: (label: string) =>
    postJSON<{ ok: boolean; error: string | null; id: string; token: string }>(
      "/api/tokens",
      { do: "create", label },
    ),

  tokenDelete: (id: string) =>
    postJSON<{ ok: boolean; error: string | null }>("/api/tokens", {
      do: "delete",
      id,
    }),

  tokenRequire: (enabled: boolean) =>
    postJSON<{ ok: boolean; error: string | null }>("/api/tokens", {
      do: "require",
      enabled,
    }),

  tsapi: () => getJSON<TsApiStatus>("/api/tsapi"),

  tsapiValidate: (cred: TsApiCredential) =>
    postJSON<TsApiProbe>("/api/tsapi/validate", cred),

  tsapiSave: (cred: TsApiCredential) =>
    postJSON<TsApiProbe>("/api/tsapi", cred),

  tsapiFences: () =>
    postJSON<{ ok: boolean; added: string[]; error: string | null }>(
      "/api/tsapi/fences",
      {},
    ),

  userAdopt: (id: string) =>
    postJSON<{ ok: boolean; error: string | null; hostname: string }>(
      "/api/users/adopt",
      { id },
    ),

  userAccess: (id: string, service: string, allow: boolean) =>
    postJSON<{ ok: boolean; error: string | null }>(
      `/api/users/${id}/access`,
      { service, allow },
    ),

  person: (body: {
    do: "add" | "rename" | "reissue" | "delete" | "assign" | "basic";
    id?: string;
    name?: string;
    node?: string;
    basic?: boolean;
  }) =>
    postJSON<{ ok: boolean; error: string | null; id?: string; key?: string }>(
      "/api/people",
      body,
    ),

  personAccess: (id: string, service: string, allow: boolean) =>
    postJSON<{ ok: boolean; error: string | null }>(
      `/api/people/${id}/access`,
      { service, allow },
    ),

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

  shareNfs: (name: string, enabled: boolean, clients = "", ro = true) =>
    postJSON<ShareResult>("/api/shares", {
      do: "nfs",
      name,
      enabled,
      clients,
      ro,
    }),

  sources: () =>
    getJSON<{ sources: Source[]; catalogs: BuiltinCatalog[] }>("/api/sources"),

  customPodSave: (spec: {
    name: string;
    image: string;
    command?: string;
    ports?: Record<string, string>;
    environment?: Record<string, string>;
    volumes?: Record<string, string>;
  }) =>
    postJSON<{ ok: boolean; name: string; error: string | null }>(
      "/api/custompods",
      { do: "save", ...spec },
    ),

  customPodDelete: (name: string) =>
    postJSON<{ ok: boolean; name: string; error: string | null }>(
      "/api/custompods",
      { do: "delete", name },
    ),

  catalogSet: (key: string, enabled: boolean) =>
    postJSON<{ ok: boolean; error: string | null }>("/api/catalogs", {
      key,
      enabled,
    }),

  fsList: (path: string) =>
    postJSON<FsListResult>("/api/fs", { do: "list", path }),

  fsMkdir: (path: string) =>
    postJSON<FsMkdirResult>("/api/fs", { do: "mkdir", path }),

  sourceAdd: (name: string, url: string) =>
    postJSON<ShareResult>("/api/sources", { do: "add", name, url }),

  sourceDelete: (name: string) =>
    postJSON<ShareResult>("/api/sources", { do: "delete", name }),

  setup: () => getJSON<SetupStatus>("/api/setup"),

  setupAction: (action: "retry" | "skip") =>
    postJSON<{ ok: boolean; error: string | null }>("/api/setup", {
      do: action,
    }),
};
