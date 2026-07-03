// Shapes returned by the controller JSON API (web/app.py).

export type PodState = "running" | "stopped";

export interface Pod {
  name: string;
  state: PodState;
  controller: boolean;
  image: string;
  tailscale: boolean;
  https: boolean;
  shares: string[];
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
