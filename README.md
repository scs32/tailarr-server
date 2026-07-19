# Tailarr

Deploy self-hosted services as Podman pods where **every service becomes
its own device on your Tailscale tailnet** — its own hostname, MagicDNS
name, HTTPS certificate, and ACL identity. No ports exposed anywhere.
One line to install, and everything it produces is a plain script you
can read.

## Quick start — web UI (recommended)

On a Debian/Ubuntu host (a VM or container works great), with a
[Tailscale OAuth client](#the-tailscale-credential) (preferred) or a
plain [auth key](https://login.tailscale.com/admin/settings/keys):

```sh
# OAuth client — everything works from first boot (tagging, ACLs, key minting):
TS_API_CLIENT_ID=... TS_API_CLIENT_SECRET=... bash -c "$(curl -fsSL https://raw.githubusercontent.com/scs32/tailarr-server/main/install.sh)"

# or minimal — plain auth key, configure the API credential later in Settings:
TS_AUTHKEY=tskey-... bash -c "$(curl -fsSL https://raw.githubusercontent.com/scs32/tailarr-server/main/install.sh)"
```

This installs podman, pulls the Tailarr controller image, and enrolls it
on your tailnet. Then open **`https://tailarr.<your-tailnet>.ts.net`**
and install services from the catalog with a click.

### The Tailscale credential

Tailarr needs two things from Tailscale: a way to enroll nodes (auth
keys) and — for its ACL/tagging/sharing features — API access. **One
OAuth client covers both**, because Tailarr mints its own auth keys
through it. Set it up once:

1. Add the controller tag to your
   [tailnet policy](https://login.tailscale.com/admin/acls)'s
   `tagOwners` (Tailscale requires a tag to exist before an OAuth
   client can carry it). Paste it inside Tailarr's fence markers so the
   bootstrap adopts the line instead of duplicating it:
   ```jsonc
   "tagOwners": {
       // >>> tailarr-managed:tagowners
       "tag:tailarr-ctrl": ["autogroup:admin"],
       // <<< tailarr-managed:tagowners
   },
   ```
2. Create an [OAuth client](https://login.tailscale.com/admin/settings/oauth)
   with **write** access to **Auth Keys**, **Devices**, and
   **Policy File**, tagged **`tag:tailarr-ctrl`**.
3. Run the installer with its id + secret (above). The bootstrap
   fills in the tailarr-managed policy sections, mints the controller's
   auth key, and saves the credential for the controller
   (`$PODS_DIR/.tsapi.json`, mode 600) — no Settings wizard needed.

A static API access token (`TS_API_TOKEN=tskey-api-...`) works in place
of the OAuth client, but it is full-access and expires within 90 days;
the plain-auth-key path skips API features entirely until you configure
a credential in Settings.

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

## Running on macOS in a full VM (recommended)

Tailarr needs a Linux host, but you don't need a separate machine — a
Debian VM under [VMware Fusion](https://www.vmware.com/products/desktop-hypervisor.html)
(free for personal use; UTM works too) is the recommended way to run it
on a Mac. Two field-tested rules make it work well:

**Use bridged networking.** A bridged VM gets its own address on your
LAN, so its Tailscale sidecars negotiate *direct* WireGuard paths.
NAT-style networking (the apple/container path below, and Fusion's
"Share with my Mac" mode) often forces Tailscale onto **DERP relays**,
which caps every stream at relay speeds — unusable for media.

**Give the VM its own virtual disk for media — don't pass host folders
through.** Hypervisor shared folders (Fusion's hgfs, virtiofs) garble
paths, permissions, and performance between macOS and the guest.
Instead, add a second virtual disk in the VM's settings, format it ext4,
and mount it at `/data` in the guest:

```sh
mkfs.ext4 /dev/sdb && mkdir -p /data
echo '/dev/sdb /data ext4 defaults 0 2' >> /etc/fstab && mount -a
```

Then install Tailarr in the VM with the one-liner from the top of this
README. On Debian/Ubuntu the bootstrap installs and enables a systemd
unit (`tailarr-pods.service`), so the whole stack self-heals when the VM
reboots. Register `/data` on the web UI's **Shares** page and attach it
to your media pods — the catalog's download/media paths already live
under it.

**Media back out to macOS (native Plex, Finder, etc.).** The clean path
is the reverse of what you might expect: the VM *owns* the media disk
and **NFS-exports it to the Mac**, rather than the Mac pushing a folder
in. On the VM:

```sh
apt install -y nfs-kernel-server
echo '/data 192.168.0.0/16(ro,all_squash,insecure)' >> /etc/exports
exportfs -ra
```

On the Mac: Finder → Go → Connect to Server → `nfs://<vm-ip>/data`
(or `mount -t nfs -o resvport <vm-ip>:/data /Users/you/media`). Point a
native Plex/Jellyfin at that mount for full-speed local playback with
hardware transcoding. Narrow the export CIDR to your LAN, or export to
the VM's Tailscale address and mount over the tailnet.

Or skip the manual steps: as of v0.9.0 the web UI manages exports —
**Shares page → NFS…** on any share (enter the allowed clients, pick
read-only, done). Only `apt install -y nfs-kernel-server` stays manual.

## Running on macOS with apple/container (not recommended for media)

> **⚠️ DERP relay warning.** apple/container puts guests behind a NAT'd
> `vmnet` subnet that Tailscale usually cannot hole-punch. In practice
> the pods' tailnet connections fall back to **DERP relays** far too
> often, throttling transfers to relay speeds even between devices on
> the same LAN. It works, and it's fine for *trying* Tailarr — but for a
> media server, use the full-VM setup above.

Apple's [`container`](https://github.com/apple/container) tool
(macOS 15+, Apple silicon) runs a lightweight Linux guest where podman
and the pods live; macOS just hosts it.

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
# now inside the guest (exec starts at /, not a login shell):
cd /root
apt update && apt install -y curl
TS_AUTHKEY=tskey-... bash -c "$(curl -fsSL https://raw.githubusercontent.com/scs32/tailarr-server/main/install.sh)"
```

The engine scripts land in the directory you run the installer from
(pods themselves always go to `$HOME/Pods`). If you do run it from `/`,
the installer moves itself to `~/tailarr` rather than scattering scripts
across the filesystem root.

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

Debian/Ubuntu hosts with podman (auto-installed) — bare metal or a
bridged VM. On macOS, prefer a full VM (**Running on macOS in a full
VM** above); apple/container works but relays through DERP too often
for media. See `bootstrap-tailarr.sh` for the MTU and boot-persistence
handling that nested hosts need. Everything else: PRs welcome.

## Development

```sh
bash tests/smoke.sh   # engine end-to-end (create.sh → generated scripts), podman stubbed
```

CI runs shellcheck + the smoke test on every push; tags build the
multi-arch controller image to `ghcr.io/scs32/tailarr`.

## License

MIT — see [LICENSE](LICENSE).
