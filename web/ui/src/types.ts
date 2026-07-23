// Shapes returned by the controller JSON API (web/app.py).

export type PodState = "running" | "stopped" | "error";

// In-flight server-side action on a pod (survives SPA reloads).
// "restart" only appears via fleet actions (POST /api/fleet).
export type PodAction =
  | "start"
  | "stop"
  | "restart"
  | "update"
  | "remove"
  | "reconfigure"
  | "backup"
  | "restore"
  | "funnel";

export type FleetAction = "stop" | "start" | "restart" | "rerender";

export interface Pod {
  name: string;
  state: PodState;
  controller: boolean;
  // Infrastructure pod (ntfy): controller-managed, never shareable,
  // invisible to consumer devices. Managed from its own page, not here.
  system: boolean;
  image: string;
  tailscale: boolean;
  https: boolean;
  shares: string[];
  update: boolean; // newer image available (daily digest check)
  busy: PodAction | null;
  // Sidecar identity-tag health. "missing" = the node lacks its
  // tag:tailarr-svc-* — user devices are dropped at the packet filter
  // even though the service is healthy. Self-heals via reconcile passes.
  identity: "ok" | "missing" | "unknown";
}

// GET /api/network — per-pod networking settings + live tailnet identity.
export interface NetworkEntry {
  name: string;
  controller: boolean;
  system: boolean;
  state: PodState;
  tailscale: boolean;
  https: boolean;
  funnel: boolean; // publicly reachable via Tailscale Funnel
  network_mode: string;
  ports: Record<string, string>;
  ip: string; // tailnet IPv4, "" when sidecar not running
  dns_name: string; // MagicDNS name, "" when unknown
  busy: PodAction | null;
}

export interface UpdatesInfo {
  checking: boolean;
  checked: number; // epoch seconds of last completed check
  images: Record<string, { update: boolean; error: string | null }>;
}

// GET /api/monitor — Monitor tab state (Uptime Kuma integration).
export interface MonitorPod {
  name: string;
  state: PodState;
  https: boolean;
  dns_name: string;
  url: string; // what the monitor will probe
  monitored: boolean;
}

export interface MonitorStatus {
  available: boolean; // socket client present in the image
  configured: boolean; // creds saved
  connected: boolean; // creds worked on this load
  error: string | null;
  kuma_pod: string | null; // deployed uptime-kuma pod, if any
  kuma_url: string; // saved or suggested connect URL
  kuma_link: string; // user-facing Kuma UI URL (MagicDNS)
  monitors: { id: number; name: string; url: string; active: boolean }[];
  pods: MonitorPod[];
}

// Editable config for a deployed pod (GET /api/pods/<name>/config).
export interface PodConfig {
  image: string;
  command: string;
  ports: Record<string, string>;
  environment: Record<string, string>;
  volumes: Record<string, string>;
  memory_limit: string;
  shares: string[];
  controller: boolean;
}

export interface PodConfigResult {
  ok: boolean;
  name: string;
  error: string | null;
  config: PodConfig | null;
}

// POST /api/pods/<name>/config body. pull=true pulls the newest image tag
// before recreating ("Update"); pull=false recreates as-is ("Reload").
export interface ReconfigureRequest {
  image: string;
  command: string;
  ports: Record<string, string>;
  environment: Record<string, string>;
  volumes: Record<string, string>;
  memory_limit: string;
  shares: string[];
  pull: boolean;
}

export interface CatalogItem {
  name: string;
  image: string;
  ports: Record<string, string>;
  port: string;
  environment: Record<string, string>;
  volumes: Record<string, string>;
  command: string;
  system: boolean; // installs as a hidden system pod (ntfy)
  installed: boolean;
  state: PodState | ""; // "" when not installed
  source: string; // "built-in" or a source name
}

// GET /api/ntfy — Notifications tab state (ntfy system pod).
export interface NtfyStatus {
  installed: boolean; // an ntfy pod is deployed
  pod: string | null;
  state: PodState | "";
  configured: boolean; // setup has run (accounts + tokens saved)
  funnel_on: boolean; // public HTTPS for phones outside the tailnet
  public_url: string;
  ops_topic: string;
  alerts_issued: boolean; // an admin phone credential exists
  gateway: boolean; // self-config gateway pod deployed (Tailarr app)
  // Media apps and their wiring state ("auto" | "manual" | "").
  arr: { name: string; kind: string; topic: string; wired: string }[];
  publish_error: string | null; // last failed publish (ntfy down?)
  error: string | null;
}

export interface Source {
  name: string;
  url: string;
  service_count: number;
  error: string | null;
}

// Built-in category catalogs (opt-in beyond the default media catalog).
export interface BuiltinCatalog {
  key: string;
  name: string;
  description: string;
  enabled: boolean;
  service_count: number;
}

export interface Share {
  name: string;
  host_path: string;
  container_path: string;
  ro: boolean;
  mode: "read-only" | "read-write";
  visible: boolean;
  used_by: string[];
  // Host-kernel NFS export (share media OUT of the VM, e.g. to a native
  // Plex on the machine hosting it). null = not exported.
  nfs: { clients: string; ro: boolean } | null;
}

// One per-pod snapshot (GET /api/pods/<name>/backups).
export interface BackupEntry {
  ts: string; // YYYYMMDD-HHMMSS
  image: string;
  digest: string;
  size: number; // bytes
  sha256: string;
  reason: string;
}

// GET /api/users — machines wearing tag:tailarr-user + their badges.
export interface UserMachine {
  id: string; // stable Tailscale node ID
  hostname: string;
  nickname: string;
  os: string;
  last_seen: string;
  ip: string;
  can: string[]; // services this machine may reach
}

// A first-class user (person): owns devices via tag:tailarr-u-<id>,
// badges apply to all of them, keys reissue against the same identity.
export interface Person {
  id: string;
  name: string;
  badges: string[]; // per-user access switches (services)
  created: number;
  devices: UserMachine[];
}

export interface UsersStatus {
  configured: boolean; // API token present on the controller
  error: string | null;
  ntfy: boolean; // notifications system pod is set up
  people: Person[];
  users: UserMachine[]; // unassigned machines (legacy keys / adopt)
  // Grantable services: deployed non-controller pods plus the "server"
  // pseudo-service (the controller itself, for the app's server module).
  services: string[];
}

// GET /api/tokens — controller API bearer tokens (secrets never returned).
export interface TokenEntry {
  id: string;
  label: string;
  created: string;
}

export interface TokensStatus {
  require: boolean; // when true, every /api/* call needs a Bearer token
  tokens: TokenEntry[];
}

// GET /api/registries — private-registry credentials for image pulls.
// Secrets never leave the server; entries carry host + username only.
export interface RegistryEntry {
  registry: string;
  username: string;
  created: string;
}

export interface RegistriesStatus {
  registries: RegistryEntry[];
}

export interface ActionResult {
  ok: boolean;
  name: string;
  action: string;
  status: string;
  error: string | null;
  output: string;
}

// POST /api/fleet — bulk stop/start/restart of every non-controller pod.
export interface FleetResult {
  ok: boolean;
  action: string;
  status: string;
  error: string | null;
  results: ActionResult[];
  skipped: { name: string; busy: string }[];
}

// GET /api/tsapi — the controller's Tailscale API credential state.
export interface TsApiStatus {
  configured: boolean;
  mode: "oauth" | "token" | null;
  error: string | null;
}

export interface TsApiCheck {
  ok: boolean;
  detail: string | null; // set on failure
}

// POST /api/tsapi/validate and POST /api/tsapi (save) — live credential
// probe, one read-only check per capability the controller needs.
export interface TsApiProbe {
  ok: boolean;
  saved?: boolean; // only on the save endpoint
  mode: "oauth" | "token" | null;
  checks: Partial<Record<"devices" | "auth_keys" | "policy_file", TsApiCheck>>;
  fences: { present: string[]; missing: string[] } | null;
  error: string | null;
}

// Credential shapes accepted by the wizard endpoints (mirrors .tsapi.json).
export interface TsApiCredential {
  token?: string;
  oauth_client_id?: string;
  oauth_client_secret?: string;
}

export interface Info {
  pods_dir: string;
  controller_pods: string[];
  version: string;
  upgrade_available: boolean; // a newer controller release is known
  tsapi: TsApiStatus;
  host_platform: string; // "apple-container" | "linux" | "unknown"
  relay: RelayStatus;
}

// A registry entry: a device Tailarr knows can (or should soon) act as a
// peer relay. `pending` = registered but traffic never seen through it;
// pending = device isn't advertising relay capability (the enable command
// hasn't been run there); ready = advertising, but no relayed traffic seen
// yet; active = relay_verify() watched traffic flow through its IP.
export interface RelayEntry {
  id: string; // == ip
  name: string;
  ip: string;
  status: "pending" | "ready" | "active";
  added_at: number;
  verified_at?: number;
  discovered?: boolean; // seen carrying traffic, never explicitly added
}

// GET /api/relay — peer-relay state. Offered on every platform since
// v0.15.0 (`recommended` marks the apple/container near-certain-DERP
// case); the grant is auto-emitted only when the pre-flight says the
// tailnet looks dedicated; `reasons` explains a negative verdict in
// customer language.
export interface RelayStatus {
  platform: string;
  applicable: boolean; // always true since v0.15.0 (kept for app compat)
  recommended: boolean;
  enabled: boolean | null; // null = auto (follow the pre-flight)
  eligible: boolean;
  reasons: string[];
  counts: Record<string, number>;
  grant_active: boolean;
  dst_fallback: boolean;
  mode: "global" | "per-pod";
  relays: RelayEntry[];
  global_relay: string; // relay id, "" = automatic (any admin device)
  pod_relays: Record<string, string>; // svc (or "server") -> relay id
  port: number;
  command: string; // the local enable command for pending relays
  verified: {
    state: "unknown" | "derp" | "direct" | "peer-relay";
    at: number;
    detail: string;
  };
}

// POST /api/relay body — do + action-specific fields.
export interface RelayAction {
  do:
    | "enable"
    | "disable"
    | "recheck"
    | "mode"
    | "add-relay"
    | "remove-relay"
    | "set-global"
    | "set-pod";
  mode?: "global" | "per-pod";
  ip?: string;
  name?: string;
  id?: string;
  pod?: string;
}

export interface RelayActionResult {
  ok: boolean;
  error?: string | null;
  relay: RelayStatus;
  command?: string;
  sync?: { ok: boolean; changed: boolean; error: string | null };
}

// GET /api/relay/devices — candidates for the add-relay picker.
export interface RelayDevice {
  hostname: string;
  name: string;
  ip: string;
  os: string;
  user: string;
}

// GET /api/controller/upgrade — Settings upgrade card state.
export interface UpgradeStatus {
  current: string;
  latest: string; // "" until a release check has succeeded
  available: boolean;
  checked: number; // epoch seconds of the last successful release check
  busy: boolean; // an upgrade helper container is running right now
  last: {
    ok: boolean;
    from: string;
    to: string;
    rolled_back: boolean;
    finished: string;
  } | null;
  // POST /api/controller/upgrade/check additionally sets these:
  ok?: boolean;
  error?: string;
}

// POST /api/controller/upgrade — the swap is handed to a detached helper;
// the controller restarts a few seconds after an ok response.
export interface UpgradeResult {
  ok: boolean;
  action: string;
  status: string;
  error: string | null;
  from?: string;
  to?: string;
  output: string;
}

export interface InstallResult {
  ok: boolean;
  name: string;
  error: string | null;
  output: string;
}

export interface ShareResult {
  ok: boolean;
  name?: string;
  error: string | null;
  message?: string;
  output?: string;
}

// POST /api/install body. `service` names a catalog entry; `custom` marks an
// arbitrary OCI image. Omitted volumes default to per-pod paths server-side.
export interface InstallRequest {
  service?: string;
  custom?: boolean;
  image?: string;
  command?: string;
  ports?: Record<string, string>;
  environment?: Record<string, string>;
  volumes?: Record<string, string>;
  shares?: string[];
  authkey?: string;
}

// GET /api/stats — one LIVE snapshot of per-pod resources (app container +
// tailscale sidecar) from a single podman stats pass. `at` is an epoch; the
// endpoint is history-ready (a future `series` field), so this shape won't
// change when trends land. See CLAUDE.md backlog item 9.
export interface ContainerStat {
  cpu_percent: number;
  mem_bytes: number;
  mem_limit_bytes: number;
}

export interface PodStat {
  name: string;
  state: PodState;
  cpu_percent: number; // app + sidecar
  mem_bytes: number; // app + sidecar
  mem_limit_bytes: number; // app container's cgroup limit, 0 if unbounded
  app: ContainerStat | null; // null when the container isn't running
  sidecar: ContainerStat | null;
}

export interface StatsSnapshot {
  at: number;
  pods: PodStat[];
  totals: {
    cpu_percent: number;
    mem_bytes: number;
    pods: number;
    running: number;
  };
}

// POST /api/fs — browse the PODMAN HOST's directories (what pods actually
// bind-mount). Backs the FolderEditor "Browse" popover; listing runs in a
// one-shot helper container, so a call takes a moment.
export interface FsListResult {
  ok: boolean;
  path: string;
  parent: string | null; // null at "/"
  dirs: string[]; // child directory names (dotdirs hidden), sorted
  error: string | null;
}

export interface FsMkdirResult {
  ok: boolean;
  path: string;
  error: string | null;
}
