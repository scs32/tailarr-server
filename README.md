# Tailarr

Deploy self-hosted services as Podman pods where **every service becomes
its own device on your Tailscale tailnet** — its own hostname, MagicDNS
name, HTTPS certificate, and ACL identity. No ports exposed anywhere.
One line to install, and everything it produces is a plain script you
can read.

## Quick start — web UI (recommended)

On a Debian/Ubuntu host (a VM or container works great), with a
[Tailscale auth key](https://login.tailscale.com/admin/settings/keys):

```sh
TS_AUTHKEY=tskey-... bash -c "$(curl -fsSL https://raw.githubusercontent.com/scs32/tailarr-server/main/install.sh)"
```

This installs podman, pulls the Tailarr controller image, and enrolls it
on your tailnet. Then open **`https://tailarr.<your-tailnet>.ts.net`**
and install services from the catalog with a click.

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
`--network container:`. `tailscale serve` terminates HTTPS on 443 with
an automatic `ts.net` certificate. This is the whole product — there is
no plain-HTTP or no-Tailscale mode: every pod is a tailnet device with
HTTPS, so an auth key is required to install one.

- **Per-service tailnet identity** — Tailscale ACLs work at the service
  level. Share Jellyfin with family without exposing the rest.
- **Zero exposed ports** — nothing on your LAN, no port conflicts ever.
- **No daemon** — Podman is daemonless; deployments are generated shell
  scripts (`run.sh`, `stop.sh`, `remove.sh`, `diagnose.sh`) in
  `~/Pods/<service>/`. The optional web controller is itself just a pod.

## How this compares

| | CasaOS / Umbrel | Tailarr |
|---|---|---|
| Interface | Web dashboard on the LAN | Web UI as a tailnet-only pod |
| Runtime | Docker daemon | Podman (daemonless) |
| Network | Host LAN + published ports | Per-service tailnet devices, HTTPS via ts.net |
| App catalog | Curated store | `homelab.js` — a JSON file you edit |

## Adding a service

Add an entry to `homelab.js`:

```json
{
  "name": "myservice",
  "image": "someone/myservice:latest",
  "restart_policy": "unless-stopped",
  "environment": { "TZ": "America/Los_Angeles" },
  "volumes": { "/path/to/config": "/config" },
  "ports": { "8080": "8080" }
}
```

Optional fields: `"command"` (appended after the image) and
`"memory_limit"` (podman `-m`).

## Running on macOS with apple/container

Tailarr needs a Linux host, but you don't need a separate machine — run
it inside a lightweight Linux guest with Apple's
[`container`](https://github.com/apple/container) tool (macOS 15+, Apple
silicon). The guest is where podman and the pods live; macOS just hosts it.

**1. Install and start apple/container:**

```sh
brew install container
container system start
```

**2. Create a long-lived Debian guest.** Bind-mount a host folder for
media *now* — apple/container fixes mounts at creation time, so adding one
later means destroying and recreating the guest:

```sh
mkdir -p "$HOME/poddata"
container run -d --name podhost \
  --cpus 4 --memory 4g \
  --volume "$HOME/poddata:/data" \
  docker.io/library/debian:bookworm sleep infinity
```

**3. Install Tailarr inside the guest.** Shell in, add `curl`, then run
the normal one-liner with your
[Tailscale auth key](https://login.tailscale.com/admin/settings/keys):

```sh
container exec -ti podhost bash
# now inside the guest:
apt update && apt install -y curl
TS_AUTHKEY=tskey-... bash -c "$(curl -fsSL https://raw.githubusercontent.com/scs32/tailarr-server/main/install.sh)"
```

The bootstrap detects the guest's reduced MTU and pins podman's network
MTU to match (nested guests run at MTU 1280; larger MTUs silently
blackhole TLS). When it finishes, open
**`https://tailarr.<your-tailnet>.ts.net`** from any tailnet device.

**Media into pods.** Everything under the guest's `/data` maps back to
`$HOME/poddata` on your Mac. Attach it to pods from the web UI's Shares
page so only media crosses the pod boundary — configs and Tailscale
identities stay per-pod inside the guest.

**Surviving reboots.** apple/container has no `--restart` flag and the
guest stops when your Mac shuts down. The bootstrap installs
`/root/start-pods.sh`, which wipes podman's stale runroot once per boot
(the guest keeps `/run` on disk, so podman can't detect reboots) and
starts sidecars before services. Bring the fleet back after a reboot with:

```sh
container start podhost
container exec podhost /root/start-pods.sh
```

To automate it, wire those two commands into a macOS **LaunchAgent** (or
login item) so the guest and pods come up without you.

## Supported platforms

Debian/Ubuntu hosts with podman (auto-installed). Runs happily inside
VMs and container-VMs — see **Running on macOS with apple/container**
above for the fully worked nested-guest path, and
`bootstrap-tailarr.sh` for the MTU and boot-persistence handling that
nested hosts need. Everything else: PRs welcome.

## Development

```sh
bash tests/smoke.sh   # engine end-to-end (create.sh → generated scripts), podman stubbed
```

CI runs shellcheck + the smoke test on every push; tags build the
multi-arch controller image to `ghcr.io/scs32/tailarr`.

## License

MIT — see [LICENSE](LICENSE).
