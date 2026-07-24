# What Tailarr installs and uses on a server

A complete inventory of everything Tailarr puts on a host, where it lives, and
what it touches on your tailnet — so you can audit the footprint before you run
the installer and reverse it cleanly afterwards.

**The one-line model:** Tailarr installs *podman + jq*, drops a handful of
boot/socket/systemd files under `/root`, `/etc`, `/run`, and `/var/log`, and
keeps **all** of its actual state in a single `/root/Pods` tree — one
subdirectory per pod plus a set of dot-file registries. Every pod becomes its
own Tailscale device, managed through one OAuth client. It never modifies your
media directories except through bind-mounts you explicitly choose.

This document tracks the code in `install.sh`, `bootstrap-tailarr.sh`, and the
controller (`web/app.py`). If you change those, update this.

---

## 1. Host packages (apt, Debian-family, only if missing)

- **`podman`** — the container engine everything runs on.
- **`jq`** — JSON parsing in the install scripts.

That is the entire host-package footprint. If podman already exists the OS
check is skipped, and non-Debian hosts must pre-install podman themselves (the
installer refuses to auto-install on anything but Debian-family).

## 2. Container images pulled

| Image | From | Role |
|---|---|---|
| `ghcr.io/scs32/tailarr:v<version>` | GHCR | the controller — also reused for the upgrade helper, the gateway pod, and one-shot host/fs helpers |
| `docker.io/tailscale/tailscale:stable` | Docker Hub | every pod's Tailscale sidecar |
| `linuxserver/*` and friends | per catalog | each service you install (sonarr, radarr, nzbget, …) |
| `binwiederhier/ntfy` (pinned) | Docker Hub | the hidden notifications system pod (only if you set up notifications) |

The controller image is pinned to an explicit version tag (never `:latest`) so
a fresh install right after a release cannot catch a stale GHCR manifest.

## 3. Containers that run

- **`tailscale-tailarr`** + **`tailarr`** — the controller sidecar and the
  controller, sharing one network namespace.
- **Per service**: `tailscale-<svc>` (sidecar) + `<svc>` (the app). Each
  sidecar is its own tailnet device.
- **System pods** (hidden from the normal UI): `ntfy` and `tailarr-gate` (the
  self-config gateway) — deployed on demand.
- **Ephemeral one-shots**: `tailarr-upgrade` during a self-upgrade, plus
  short-lived privileged / `nsenter` containers for host tasks (NFS exports,
  the systemd drop-in) and a folder-browse helper that mounts the host `/` at
  `/host-root` (read-only for listing, read-write only for `mkdir`).

## 4. Files written OUTSIDE the Pods dir

This is the real host footprint beyond containers and packages.

| Path | What | When |
|---|---|---|
| `/etc/containers/containers.conf` | appends one `network_cmd_options mtu=…` line | only if host MTU < 1500 (nested VMs) |
| `/run/podman/podman.sock` | the podman API socket the controller drives | always |
| `/var/log/podman-api.log` | podman service log | always |
| `/root/start-pods.sh` | boot-recovery script (wipes stale runroot, starts sidecars then services) | always |
| `/etc/systemd/system/tailarr-pods.service` | boot unit that starts the fleet | systemd hosts only |
| `/etc/systemd/system/tailarr-pods.service.d/50-tailarr-mounts.conf` | drop-in: `RequiresMountsFor` for every share, so the fleet waits for media disks (nofail mounts) instead of bind-mounting an empty dir | when shares exist |
| `/etc/exports.d/tailarr.exports` (+ runs `exportfs -ra`) | kernel NFS exports | only if you enable NFS on a share |
| `/dev/shm/pods-booted` | per-boot sentinel (gates the runroot wipe) | at boot |
| `/run/libpod/rootless-netns/…` | IPAM staging mkdirs (podman bridge-bug workaround) | at container start |

Tailarr writes under `/root`, `/etc`, `/run`, and `/var/log`. It does **not**
touch your media directories except through the bind-mounts you choose per
service or per share.

## 5. The Pods dir — all persistent state

Default `/root/Pods` (it runs as root, so `$HOME/Pods` = `/root/Pods`).
Overridable with the `PODS_DIR` environment variable. This one directory holds
everything Tailarr owns.

### Controller identity

- `Pods/tailarr/tailscale/` — the controller's tailnet state
  (`tailscaled.state`).
- `Pods/tailarr/.tailscale_authkey` — the minted single-use key (mode 0600).
- `Pods/tailarr/tailscale-serve.json` — the HTTPS-on-443 `tailscale serve`
  config.

### Per service — `Pods/<svc>/`

- Generated `run.sh` / `stop.sh` / `remove.sh` / `diagnose.sh`.
- `.config.json` (the pod's spec) and the `.config-seeded` sentinel.
- The service's own config volume(s), e.g. `Pods/<svc>/config`.
- `Pods/<svc>/tailscale/` — that service's own sidecar identity.

### Registries and state files

Dot-files at the Pods root. Files marked 🔒 hold secrets and are written mode
0600.

| File | Holds | Secret |
|---|---|:--:|
| `.tsapi.json` | the Tailscale OAuth client | 🔒 |
| `.host.json` | platform fact (linux / apple-container) | |
| `.server.json` | the admin-chosen server display name | |
| `.shares.json` / `.exports` | media shares + NFS config | |
| `.sources.json` / `.catalogs.json` / `.custompods.json` | catalog sources | |
| `.users.json` / `.people.json` | devices, people, and per-user badges | |
| `.gateway.json` | per-install gateway secret | 🔒 |
| `.tokens.json` | API bearer tokens (sha256) | 🔒 |
| `.registries.json` / `.registry-auth.json` | private image credentials | 🔒 |
| `.accounts.json` | saved indexer / usenet logins | 🔒 |
| `.relay.json` | peer-relay registry + verdicts | |
| `.kuma.json` | Uptime-Kuma wiring | |
| `.ntfy.json` / `.notify-state.json` | notifications config | 🔒 |
| `.push.json` | push-token registry | 🔒 |
| `.stacks.json` | Magic Stack run state | |
| `.updates.json` / `.release.json` | update-check caches | |
| `.acl-last-good.hujson` | last-known-good ACL backup | |
| `.backups.json` + `.backups/` | pod-config backups | |
| `.upgrade/` | self-upgrade scripts + result | |
| `ntfy/`, `nzbget/`, … | system / service config trees | |

## 6. What it uses on the Tailscale side

Not host files, but part of "the server":

- **One tailnet device per pod** (the sidecars), all tagged `tag:tailarr*`.
- **Fenced ACL regions** in your tailnet policy
  (`// >>> tailarr-managed:grants|tagowners|nodeattrs`), spliced and maintained
  by the controller. Everything outside the fences is left byte-for-byte
  untouched. See [`acl-design.md`](acl-design.md).
- **Auth keys** minted per pod, **MagicDNS** hostnames, **HTTPS certificates**
  on 443 per device, and **Funnel** for pods you make public.
- **Zero published host ports** — all access is over the tailnet.

## 7. Outbound network dependencies

- **Install time:** `raw.githubusercontent.com`, `ghcr.io`,
  `api.tailscale.com`, `login.tailscale.com` (the installer preflight checks
  each by name).
- **Runtime:** Tailscale coordination / DERP, GHCR for image pulls, and
  **optionally** `push.tailarr.com` (the push relay — outbound only, and
  content-free: it carries a device wake token, never message contents).

## 8. Uninstalling

There is no dedicated uninstaller yet; the footprint above is small and
reversible by hand:

1. **Stop and remove the containers.**

   ```sh
   for c in $(podman ps -a --format '{{.Names}}'); do podman rm -f "$c"; done
   ```

   (Or remove just the `tailarr`, `tailscale-*`, and per-service containers if
   the host runs other podman workloads.)

2. **Remove the boot wiring.**

   ```sh
   systemctl disable --now tailarr-pods.service 2>/dev/null || true
   rm -f /etc/systemd/system/tailarr-pods.service
   rm -rf /etc/systemd/system/tailarr-pods.service.d
   systemctl daemon-reload 2>/dev/null || true
   rm -f /root/start-pods.sh
   ```

3. **Remove NFS exports** (only if you enabled NFS):

   ```sh
   rm -f /etc/exports.d/tailarr.exports && exportfs -ra
   ```

4. **Delete the state tree.** This removes all controller and service config,
   including secrets:

   ```sh
   rm -rf /root/Pods
   ```

5. **Optional:** revert the MTU line in `/etc/containers/containers.conf` (if
   it was added), remove the `podman` / `jq` packages if nothing else needs
   them, and delete the Tailarr devices, minted keys, and fenced ACL regions
   from your Tailscale admin console.

Removing `/root/Pods` erases the controller's tailnet identity; a later
reinstall enrolls fresh devices.
