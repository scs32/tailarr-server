# Tailarr

Deploy self-hosted services as Podman pods where **every service becomes
its own device on your Tailscale tailnet** — its own hostname, MagicDNS
name, HTTPS certificate, and ACL identity. No ports exposed anywhere.
One line to install, and everything it produces is a plain script you
can read.

## Quick start — web UI (recommended)

On a Debian/Ubuntu host (a VM or container works great), with a
[Tailscale OAuth client](#the-tailscale-credential):

```sh
TS_API_CLIENT_ID=... TS_API_CLIENT_SECRET=... bash -c "$(curl -fsSL https://raw.githubusercontent.com/scs32/tailarr-server/main/install.sh)"
```

This installs podman, pulls the Tailarr controller image, and enrolls it
on your tailnet. Then open **`https://tailarr.<your-tailnet>.ts.net`**
and install services from the catalog with a click.

### The Tailscale credential

Tailarr needs two things from Tailscale: a way to enroll nodes (auth
keys) and — for its ACL/tagging/sharing features — API access. **One
OAuth client covers both**, because Tailarr mints its own auth keys
through it. It is the install credential. Set it up once:

1. **Give Tailarr its own tailnet.** Create a fresh (free) Tailscale
   account for your media empire, log your own devices into it, and
   replace its ENTIRE
   [Access Controls file](https://login.tailscale.com/admin/acls) with
   the policy below. Because nothing else lives on the tailnet,
   default-deny is safe from the first save: your admin-owned devices
   reach the whole fleet, and every other device reaches — and even
   *sees* — exactly the services you grant it on the Users page.
   Tailscale only hides a peer from a device when no rule connects
   them at all, which is why the admin rule below deliberately avoids
   `"dst": ["*"]`: that shape would put your personal machines in
   every guest's device list.

   ```jsonc
   // Tailarr tailnet policy — the entire Access Controls file of a
   // dedicated tailnet. Edit anything outside the fenced regions;
   // never edit inside them (Tailarr regenerates their contents).
   {
       "grants": [
           // Operator sovereignty: your devices reach the whole fleet.
           // These lines are yours — Tailarr never touches them. dst is
           // deliberately NOT "*": a rule allowing admin -> guest-device
           // traffic would make every admin machine visible (name + IPs)
           // in every guest's netmap, even with all ports closed.
           {"src": ["autogroup:admin"], "dst": ["tag:tailarr"], "ip": ["*"]},
           // Your own (untagged) devices still reach each other; tagged
           // devices have no user owner, so this never touches guests.
           {"src": ["autogroup:member"], "dst": ["autogroup:self"], "ip": ["*"]},

           // >>> tailarr-managed:grants
           // <<< tailarr-managed:grants
       ],

       "tagOwners": {
           // >>> tailarr-managed:tagowners
           // Self-ownership is REQUIRED: the OAuth client acts as
           // tag:tailarr-ctrl and may only assign tags this tag owns —
           // a tag does not own itself implicitly.
           "tag:tailarr-ctrl": ["autogroup:admin", "tag:tailarr-ctrl"],
           // tag:tailarr must be declared too: the sovereignty grant
           // above references it, and Tailscale rejects a policy whose
           // rules name an undeclared tag ("tag not found").
           "tag:tailarr": ["autogroup:admin", "tag:tailarr-ctrl"],
           // <<< tailarr-managed:tagowners
       },

       "nodeAttrs": [
           // >>> tailarr-managed:nodeattrs
           // <<< tailarr-managed:nodeattrs
       ],
   }
   ```

   *Sharing an existing tailnet instead?* Paste just the fenced
   `tagOwners` block above into your policy's `tagOwners` section —
   the bootstrap adopts the fence and adds the missing `grants` /
   `nodeAttrs` fences itself. Your existing rules are untouched; note
   that Tailarr's grants only ever *allow* `tag:tailarr*` traffic, so
   under an allow-all policy they are inert labels until you move to
   default-deny.

   Either way, also enable **HTTPS Certificates** (DNS tab) — pod HTTPS
   needs it, and it's the one thing the bootstrap can't switch on for
   you.

2. Create an [OAuth client](https://login.tailscale.com/admin/settings/oauth)
   with **write** access to **Auth Keys**, **Devices**, and
   **Policy File**, tagged **`tag:tailarr-ctrl`**.
3. Run the installer with its id + secret (above). The bootstrap
   fills in the tailarr-managed policy sections, mints the controller's
   auth key, and saves the credential for the controller
   (`$PODS_DIR/.tsapi.json`, mode 600) — no Settings wizard needed.

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
HTTPS, enrolled with a key Tailarr mints through your OAuth client.

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

## Beyond deploys

Installing pods is the start; the web UI manages the fleet from there:

- **People, not devices.** Add a user on the Users page and hand them
  one enrollment key — every device they enroll with it is theirs
  automatically. Flip a service on for a person and all their devices
  can reach it (and *see* nothing else on the tailnet); flip it off and
  access is gone everywhere. Sharing is Tailscale ACLs under the hood,
  authored for you.
- **Notifications.** One click on the Notifications page sets up a
  self-hosted notification service ([ntfy](https://ntfy.sh)) as a
  hidden system pod: server alerts (pod down, upgrade results, updates
  available) to your phone, Sonarr/Radarr/Lidarr/Readarr wired
  automatically so downloads and upgrades notify, and a per-person feed
  that mirrors exactly the services each user can access.
- **A companion app that configures itself.** With the
  [Tailarr iOS app](https://github.com/scs32/tailarr), a user's phone
  asks a small gateway pod "what's mine?" — identity comes from the
  tailnet wire, so there is nothing to type and no credentials to send
  around.
- **Self-upgrades and monitoring.** The controller updates itself from
  GitHub releases and re-renders the fleet's scripts automatically; a
  Stats page shows live per-pod CPU/memory; Uptime Kuma monitors are a
  drag away on the Monitor page. Settings has themes, because why not.

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

### Private images

Public images just work. To deploy a **private** image — say one you
publish to GitHub's `ghcr.io` — add a login under **Settings → Private
registries** first: the registry host, your username, and a token (for
GitHub, a personal access token with the `read:packages` scope). Tailarr
verifies the login against the registry before saving, stores it
privately on the server (`0600`, never shown again), and every image
pull — installs, the per-pod Update button, and update checks — uses it
from then on.

## Running on macOS in a full VM (recommended)

Tailarr needs a Linux host, but you don't need a separate machine — a
Debian VM under [VMware Fusion](https://www.vmware.com/products/desktop-hypervisor.html)
(free for personal use; UTM works too) is the recommended way to run it
on a Mac. Two field-tested rules make it work well:

**Use bridged networking.** A bridged VM gets its own address on your
LAN, so its Tailscale sidecars negotiate *direct* WireGuard paths.
NAT-style networking (Fusion's "Share with my Mac" mode) often forces
Tailscale onto **DERP relays**, which caps every stream at relay speeds
— unusable for media. (The apple/container path below is also NAT'd,
but its installer compensates with a peer relay on the Mac.)

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

## Running on macOS with apple/container

Apple's [`container`](https://github.com/apple/container) tool
(macOS 15+, Apple silicon) runs a lightweight Linux guest where podman
and the pods live; macOS just hosts it. One command does everything —
creates the guest, installs Tailarr inside it, and configures the fix
for apple/container's big catch (see the note below):

```sh
TS_API_CLIENT_ID=... TS_API_CLIENT_SECRET=... bash -c "$(curl -fsSL https://raw.githubusercontent.com/scs32/tailarr-server/main/install-mac.sh)"
```

Prerequisites: `brew install container`, plus the
[Tailscale app](https://tailscale.com/download) (1.86+) logged in to the
same tailnet the OAuth client belongs to. `GUEST_NAME`, `MEDIA_DIR`
(default `~/poddata`, mapped to `/data` in the guest) and `RELAY_PORT`
are overridable via env.

> **The DERP problem, and the peer-relay fix.** apple/container puts
> guests behind a NAT'd `vmnet` subnet that Tailscale cannot hole-punch,
> so every pod connection falls back to **DERP relays** — throttled to
> relay speeds even between devices on the same LAN. The installer fixes
> this by making your Mac a [Tailscale peer
> relay](https://tailscale.com/docs/features/peer-relay)
> (`tailscale set --relay-server-port=40000`): pods still relay, but
> through the Mac they run on, at local speeds. The matching policy
> grant is authored by the controller — automatically only when the
> tailnet passes a pre-flight check (policy adopted by Tailarr, small
> device/user counts — i.e. it looks like the dedicated tailnet Tailarr
> assumes). Otherwise nothing is touched and the **Network page's Peer
> relay section** shows exactly what was found, with an explicit enable
> button. The
> grant carries only the relay *capability* (`tailscale.com/cap/relay`)
> — it never opens network access, and your Mac is matched via
> `autogroup:admin`, never by tagging your personal device.

### Manual setup (what the installer does)

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
[Tailscale OAuth client](#the-tailscale-credential):

```sh
container exec -ti podhost bash
# now inside the guest (exec starts at /, not a login shell):
cd /root
apt update && apt install -y curl
TS_API_CLIENT_ID=... TS_API_CLIENT_SECRET=... bash -c "$(curl -fsSL https://raw.githubusercontent.com/scs32/tailarr-server/main/install.sh)"
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

**Peer relay by hand.** If you skipped `install-mac.sh`, enable the
relay on the Mac yourself:

```sh
/Applications/Tailscale.app/Contents/MacOS/Tailscale set --relay-server-port=40000
```

then check the **Network page's Peer relay section** in the web UI — it
verifies from
the controller's own sidecar and keeps nudging until traffic actually
leaves DERP. Pods deployed before v0.13.0 also need their sidecar image
updated (peer relays need Tailscale 1.86+ in the *client* too).

## Supported platforms

Debian/Ubuntu hosts with podman (auto-installed) — bare metal or a
bridged VM. On macOS: apple/container via `install-mac.sh` (the peer
relay keeps pod traffic off DERP), or a full VM (**Running on macOS in
a full VM** above). See `bootstrap-tailarr.sh` for the MTU and
boot-persistence handling that nested hosts need. Everything else: PRs
welcome.

## Development

```sh
bash tests/smoke.sh   # engine end-to-end (create.sh → generated scripts), podman stubbed
```

CI runs shellcheck + the smoke test on every push; tags build the
multi-arch controller image to `ghcr.io/scs32/tailarr`.

## License

MIT — see [LICENSE](LICENSE).
