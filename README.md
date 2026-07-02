# HomePod Creator

Deploy self-hosted homelab services as Podman "pods" where **every service
becomes its own device on your Tailscale tailnet** — with its own hostname,
MagicDNS name, and ACL identity. One line to try it, and everything it
produces is a plain shell script you can read.

```sh
curl -fsSL https://raw.githubusercontent.com/scs32/homelabpodcreator/main/install.sh | bash
```

The installer fetches the wizard scripts into your current directory,
installs `podman` and `jq` if missing (Debian/Ubuntu), and starts an
interactive menu. Pick a service, answer a few prompts, and you get a
self-contained folder under `~/Pods/<service>/` with four scripts:

| Script | Purpose |
|--------|---------|
| `run.sh` | Start the service (and its Tailscale/NPM sidecars) |
| `stop.sh` | Stop all of the service's containers |
| `remove.sh` | Remove all of the service's containers |
| `diagnose.sh` | Troubleshoot status, logs, bindings, connectivity |

## The architecture

Each deployment is a sidecar pod, hand-built from Podman primitives:

```
┌────────────────────────── shared network namespace ─┐
│  tailscale-sonarr        sonarr         npm-sonarr  │
│  (joins your tailnet) ← --network container: ← ...   │
└──────────────────────────────────────────────────────┘
```

A Tailscale container starts first and joins your tailnet with the
service's name as its hostname. The service (and optionally Nginx Proxy
Manager) then share that container's network namespace via
`--network container:tailscale-<service>`. The result:

- **Per-service tailnet identity.** `sonarr` and `jellyfin` are separate
  tailnet devices with separate MagicDNS names — so Tailscale ACLs work at
  the *service* level. Share Jellyfin with family without exposing the
  rest of your stack.
- **Zero exposed ports.** With Tailscale enabled, nothing is published on
  your LAN and port conflicts are impossible — two services can both use
  port 8080 without caring.
- **No daemon, no management plane.** Podman is daemonless and the
  deployment artifact is a shell script. There is nothing resident to
  update, secure, or break.

Decline Tailscale and the service instead publishes its ports locally
with `-p`, like a conventional container.

## Requirements

- A Linux host (Debian/Ubuntu for automatic dependency install) —
  including a container or VM: running the installer inside a fresh
  Debian container works and keeps the whole homelab in one disposable
  guest.
- A [Tailscale auth key](https://login.tailscale.com/admin/settings/keys)
  if you enable Tailscale (generate it with **Ephemeral: OFF** — ephemeral
  nodes are auto-deleted when they go offline).

### Auth key handling — a first-run choice

The first Tailscale deployment asks how keys should be handled:

1. **Shared reusable key** (convenient): one reusable key is stored in
   `~/Pods/.tailscale_authkey` (mode 600) and every new service enrolls
   with it automatically. Mitigate the standing-credential risk by
   generating the key with a tag (e.g. `tag:pod`) and an ACL that lets
   tagged devices accept connections but initiate none.
2. **Fresh single-use key per service** (most secure): each deployment
   prompts for a new one-off key, stored at
   `~/Pods/<service>/.tailscale_authkey`. Keys are only needed for a
   pod's FIRST enrollment - afterwards its identity persists in
   `~/Pods/<service>/tailscale/` and the spent key file can be deleted.

Generated scripts read the key from its file at runtime and never embed
it. The choice is saved in `~/Pods/.tailscale_keymode`; delete that file
to be asked again.

## How this compares

The one-line install is inspired by curl-installed homelab layers like
CasaOS and Umbrel, but the design goals are inverted:

| | CasaOS / Umbrel | HomePod Creator |
|---|---|---|
| Interface | Web dashboard (resident service) | One-shot wizard → shell scripts |
| Runtime | Docker | Podman (daemonless) |
| Network | Host LAN + published ports | Per-service tailnet devices, no LAN exposure |
| App catalog | Curated store | `homelab.js` — a JSON file you edit |

It optimizes for the person who wants to *own and understand* every
layer: the installer is a script that fetches scripts that generate
scripts, and you can `cat` all of them.

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

## Development

Run the smoke test (no real containers or network access — podman is
stubbed):

```sh
bash tests/smoke.sh
```

It drives the wizard end-to-end for a Tailscale and a non-Tailscale
deployment and asserts on the generated scripts.

## License

MIT — see [LICENSE](LICENSE).
