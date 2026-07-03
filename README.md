# Podscale

Deploy self-hosted services as Podman pods where **every service becomes
its own device on your Tailscale tailnet** — its own hostname, MagicDNS
name, HTTPS certificate, and ACL identity. No ports exposed anywhere.
One line to install, and everything it produces is a plain script you
can read.

## Quick start — web UI (recommended)

On a Debian/Ubuntu host (a VM or container works great), with a
[Tailscale auth key](https://login.tailscale.com/admin/settings/keys):

```sh
TS_AUTHKEY=tskey-... bash -c "$(curl -fsSL https://raw.githubusercontent.com/scs32/podscale/main/install.sh)"
```

This installs podman, pulls the Podscale controller image, and enrolls it
on your tailnet. Then open **`https://podscale.<your-tailnet>.ts.net`**
and install services from the catalog with a click.

## Quick start — CLI wizard (no resident controller)

```sh
curl -fsSL https://raw.githubusercontent.com/scs32/podscale/main/install.sh -o install.sh
bash install.sh   # interactive menu
```

Same engine, no web UI, nothing left running except the pods themselves.

## Security model — read this

**The web UI has no authentication.** Its security model is that it is
reachable *only over your tailnet* (it binds inside its Tailscale
sidecar's network namespace and publishes nothing). Anyone on your
tailnet can manage your pods. Do NOT port-forward it or expose it any
other way; use [Tailscale ACLs](https://tailscale.com/kb/1018/acls) to
restrict which of your devices can reach it.

Auth keys are stored in mode-600 files and read at runtime; they are
never embedded in generated scripts or saved configs. Single-use keys
are supported (a pod only needs its key once — identity persists in its
state directory).

## The architecture

```
┌────────────────────── shared network namespace ─┐
│  tailscale-sonarr    ←--network container:--    sonarr │
│  (joins your tailnet)                                   │
└────────────────────────────────────────────────────────┘
```

A Tailscale sidecar starts first and joins your tailnet with the
service's name. The service shares its network namespace via
`--network container:`. Optionally, `tailscale serve` terminates HTTPS
on 443 with an automatic `ts.net` certificate.

- **Per-service tailnet identity** — Tailscale ACLs work at the service
  level. Share Jellyfin with family without exposing the rest.
- **Zero exposed ports** — nothing on your LAN, no port conflicts ever.
- **No daemon** — Podman is daemonless; deployments are generated shell
  scripts (`run.sh`, `stop.sh`, `remove.sh`, `diagnose.sh`) in
  `~/Pods/<service>/`. The optional web controller is itself just a pod.

## How this compares

| | CasaOS / Umbrel | Podscale |
|---|---|---|
| Interface | Web dashboard on the LAN | Web UI as a tailnet-only pod, or one-shot CLI |
| Runtime | Docker daemon | Podman (daemonless) |
| Network | Host LAN + published ports | Per-service tailnet devices, HTTPS via ts.net |
| App catalog | Curated store | `homelab.js` — a JSON file you edit |

## Adding a service

Add an entry to `homelab.js`:

```json
{
  "name": "myservice",
  "image": "someone/myservice:latest",
  "network_mode": "bridge",
  "restart_policy": "unless-stopped",
  "environment": { "TZ": "America/Los_Angeles" },
  "volumes": { "/path/to/config": "/config" },
  "ports": { "8080": "8080" }
}
```

Optional fields: `"command"` (appended after the image) and
`"memory_limit"` (podman `-m`).

## Supported platforms

Debian/Ubuntu hosts with podman (auto-installed). Runs happily inside
VMs and container-VMs (tested in apple/container guests — see
`bootstrap-podscale.sh` for the MTU and boot-persistence handling that
nested hosts need). Everything else: PRs welcome.

## Development

```sh
bash tests/smoke.sh   # end-to-end wizard test, podman stubbed, no network
```

CI runs shellcheck + the smoke test on every push; tags build the
multi-arch controller image to `ghcr.io/scs32/podscale`.

## License

MIT — see [LICENSE](LICENSE).
