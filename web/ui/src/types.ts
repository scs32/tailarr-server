// Shapes returned by the controller JSON API (web/app.py).

export type PodState = "running" | "stopped" | "error";

export interface Pod {
  name: string;
  state: PodState;
  controller: boolean;
  image: string;
  tailscale: boolean;
  https: boolean;
  shares: string[];
  update: boolean; // newer image available (daily digest check)
}

// GET /api/network — per-pod networking settings + live tailnet identity.
export interface NetworkEntry {
  name: string;
  controller: boolean;
  state: PodState;
  tailscale: boolean;
  https: boolean;
  network_mode: string;
  ports: Record<string, string>;
  ip: string; // tailnet IPv4, "" when sidecar not running
  dns_name: string; // MagicDNS name, "" when unknown
}

export interface UpdatesInfo {
  checking: boolean;
  checked: number; // epoch seconds of last completed check
  images: Record<string, { update: boolean; error: string | null }>;
}

// Editable config for a deployed pod (GET /api/pods/<name>/config).
export interface PodConfig {
  image: string;
  command: string;
  ports: Record<string, string>;
  environment: Record<string, string>;
  volumes: Record<string, string>;
  memory_limit: string;
  tailscale: boolean;
  https: boolean;
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
  tailscale: boolean;
  https: boolean;
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
  installed: boolean;
  state: PodState | ""; // "" when not installed
  source: string; // "built-in" or a source name
}

export interface Source {
  name: string;
  url: string;
  service_count: number;
  error: string | null;
}

export interface Share {
  name: string;
  host_path: string;
  container_path: string;
  ro: boolean;
  mode: "read-only" | "read-write";
  visible: boolean;
  used_by: string[];
}

export interface ActionResult {
  ok: boolean;
  name: string;
  action: string;
  status: string;
  error: string | null;
  output: string;
}

export interface Info {
  pods_dir: string;
  controller_pods: string[];
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
  tailscale?: boolean;
  https?: boolean;
  authkey?: string;
}
