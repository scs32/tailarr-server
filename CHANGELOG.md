# Changelog

## v0.9.1 — polish from the first live self-upgrade (2026-07-16)

The v0.8.0 → v0.9.0 upgrade ran end to end in the field (swap, health
gate, mount-guard drop-in). Two paper cuts it reported:

- **API errors are always JSON now.** An unexpected exception in a
  handler used to close the connection with no HTTP response at all —
  scripted callers saw a bare connection drop. Both API verbs now return
  `500 {"ok": false, "error": ...}` instead.
- **`result.json` no longer lags the version flip.** The upgrade helper
  wrote its outcome after refreshing host boot artifacts, so a poller
  could see the new controller answering while `result.json` still held
  the previous upgrade's outcome. The outcome is now written the moment
  the health check passes.

## v0.9.0 — NFS exports on the Shares page (2026-07-16)

The recommended macOS layout (see the README's new full-VM section) keeps
media on a VM-local virtual disk — so the machine *hosting* the VM needs a
way back in for a native Plex with hardware transcoding. That's now a
toggle.

### Features

- **NFS export per share.** Shares page → "NFS…" on any share: enter the
  allowed clients (IP / CIDR / hostname, space-separated), choose
  read-only (default; read-write maps writes to PUID/PGID 1000), enable.
  Tailarr renders `/etc/exports.d/tailarr.exports` and reloads the **host
  kernel's** NFS server through a one-shot privileged helper that
  `nsenter`s into the host (the controller itself stays in its container).
  The success message includes the exact `nfs://<vm-ip>/...` mount URL for
  the Mac. Deleting a share cleans up its export; a host without
  `nfs-kernel-server` gets a friendly one-line install hint instead of a
  stack trace. Exports use `all_squash` and are limited to the client list
  you give — no wildcards unless you type one.
  New API: `POST /api/shares {do: "nfs", name, enabled, clients, ro}`;
  share objects gain an `nfs` field.
- README: apple/container demoted with a DERP-relay warning; new
  recommended macOS path — VMware Fusion Debian VM, bridged networking,
  media on a VM-local second disk at `/data`, NFS back out to the Mac.

### Fixes (field report from the v0.6→0.7→0.8 upgrade run)

- **Boot no longer races nofail media mounts.** With `/data` on its own
  disk mounted `nofail`, systemd could start the fleet before the mount
  landed and podman bind-mounted the EMPTY mountpoint — pods came up with
  no media until a manual restart. The controller now maintains a
  `RequiresMountsFor` drop-in (`tailarr-pods.service.d/50-tailarr-mounts.conf`)
  covering `PODS_DIR` and every registered share, refreshed on share
  add/delete and on controller start (so existing installs pick it up on
  their first post-upgrade boot). The bootstrap unit also gains
  `RequiresMountsFor=$PODS_DIR`.
- **The boot unit's overwrite behavior is now documented in the unit
  itself**: re-running the bootstrap overwrites
  `/etc/systemd/system/tailarr-pods.service`; customizations belong in a
  drop-in (`systemctl edit tailarr-pods`), which survives re-runs — as
  does the controller-managed mounts drop-in.
- **nzbget: `MainDir` pinned to `/config`.** Seeding only DestDir/InterDir
  left MainDir free to scatter queue/tmp/scripts/nzb/logs into the media
  root on some image vintages. Working dirs now stay in the config volume;
  only completed/intermediate downloads live under `/data`.

### Notes

- The controller image now includes `util-linux` (nsenter) for the export
  and drop-in helpers — these features need the v0.9.0+ image, not just
  the code.

## v0.8.0 — controller self-upgrade (2026-07-16)

The controller can finally update itself — no more SSHing in for the
pull → `rm -f` → long `podman run` dance (and no more getting bitten by
GHCR manifest lag while doing it).

### Features

- **Upgrade from the UI.** Settings → Controller shows the running and
  latest released versions (release list from the repo's git tags, checked
  daily alongside the image-update check, cache-only on every page load)
  with an "update available" hint in the sidebar. One click upgrades:
  the controller pulls the **explicit new version tag first** (manifest-lag
  safe — nothing is removed until the pull succeeds), then hands the swap
  to a detached helper container, since a container cannot `podman rm -f`
  itself. The helper replaces the controller on its existing Tailscale
  sidecar (identity and HTTPS untouched, a few seconds of outage),
  health-checks the new controller through the sidecar's netns, and
  **rolls back to the old image automatically** if it doesn't come up.
  Everything is logged to `Pods/.upgrade/upgrade.log`, and the outcome
  (including rollbacks) is reported back on the Settings card.
  New endpoints: `GET/POST /api/controller/upgrade`,
  `POST /api/controller/upgrade/check`; `/api/info` gains
  `upgrade_available`.
- **Fleet re-render** (`POST /api/fleet {do: "rerender"}`). Engine fixes
  only reach existing pods when their scripts are re-rendered — after an
  upgrade the Settings card offers "Apply engine updates to all pods",
  which re-renders every non-controller pod from its saved `.config.json`
  and re-runs it (brief per-pod restart; images, volumes, environment and
  Tailscale identities unchanged).
- **Host boot artifacts stay current.** The controller image now ships
  `start-pods.sh` (extracted at image build from the bootstrap heredoc —
  single source of truth), and the upgrade helper refreshes the host's
  `/root/start-pods.sh` from the new image. The systemd unit points at
  that script, so no daemon-reload is needed.

### Notes

- Explicit downgrades work too: pass a version to
  `POST /api/controller/upgrade` — same pull-first/rollback safety net.
- The upgrade never touches service pods or the controller's sidecar.

## v0.7.0 — fixes from a real Debian VM deployment (2026-07-16)

Bugs surfaced by a production install on Debian 13 (podman, per-service
Tailscale sidecars, shared `/data` media mount).

### Fixes

- **nzbget downloads land under the shared `/data` mount out of the box.**
  The linuxserver base image bakes `DestDir=/downloads/completed` /
  `InterDir=/downloads/intermediate` into `nzbget.conf` — paths mounted
  nowhere under the shared-data layout, so completed downloads fell onto
  the container's ephemeral overlay and the *arr apps could never import
  them without a remote-path-mapping band-aid. The catalog now mounts the
  shared data path at `/data` and seeds `DestDir=/data/downloads/completed`
  and `InterDir=/data/downloads/intermediate` into the pod's own config —
  once, after the first start (a `.config-seeded` sentinel keeps re-renders
  from stomping later user edits). Fresh nzbget + Sonarr deploys import
  with zero remote path mapping, per the TRaSH-guides single-shared-mount
  convention. General mechanism: catalog entries (and custom installs) may
  declare `config_file` + `config_set` key=value seeds.
- **The download pipeline shares one mount.** sonarr/radarr/lidarr,
  qbittorrent and sabnzbd catalog entries now mount `/path/to/data` at
  `/data` instead of per-service `/downloads` silos, so every pipeline
  container sees identical paths (attach the same share, or fill the same
  host path). Existing pods keep their saved volumes — this changes fresh
  installs only. (qbittorrent/sabnzbd still need their save paths pointed
  at `/data/downloads/...` in-app; config seeding for their formats is a
  candidate follow-up.)
- **Controller HTTPS self-heals.** The controller sidecar gets its serve
  config declaratively at bootstrap only; if HTTPS certificates were
  enabled on the tailnet after that, a plain sidecar restart came up with
  "No serve config" until someone applied `tailscale serve` by hand.
  The controller now verifies serve on startup and every 15 minutes,
  re-applying the bootstrap proxy whenever it is missing — HTTPS comes up
  as soon as certs become available. Service pods were already fine (their
  run.sh re-renders the sidecar declaratively on every start).

### Features

- **Boot persistence on Debian/Ubuntu.** `bootstrap-tailarr.sh` now
  installs and enables a `tailarr-pods.service` systemd oneshot
  (`After=network-online.target`) that runs `start-pods.sh` — sidecars,
  then services — so the stack self-heals on reboot with no manual wiring.
  Non-systemd hosts (e.g. apple/container guests) keep the documented
  manual hookup.

### Previously unreleased

- Fresh installs pin the controller image to the release the scripts ship
  with (`ghcr.io/scs32/tailarr:v<VERSION>`) instead of `:latest`, closing
  the GHCR manifest-lag window right after a release. `HOMEPOD_IMAGE`
  still overrides. CI now fails if the bootstrap pin and the controller
  `VERSION` disagree.
- `install.sh` no longer scatters engine scripts across `/` when executed
  inside a container.

## v0.6.0 — onboarding: credential wizard + auto-minted keys (2026-07-15)

The release that removes both manual steps from a fresh install: no more
hand-crafting `.tsapi.json`, no more pasting auth keys per service.

### Features

- **First-run API-credential wizard** (Settings page, and embedded wherever
  an API-requiring action first hits a missing credential — the Users page
  and both install forms). Explains the required Tailscale OAuth scopes
  (Devices/Core, Auth Keys, Policy File — all write) and the
  `tag:tailarr-ctrl` tagging, deep-links to the admin console, and handles
  the tag-not-selectable-yet case (paste-in `tagOwners` snippet, or
  bootstrap via a static API access token). Accepts an OAuth client id +
  secret or a static `{"token": "tskey-api-…"}`; validates live with
  read-only calls and reports per-capability pass/fail before saving
  `Pods/.tsapi.json` with 0600 perms. Can also initialize the three
  `tailarr-managed` policy fences (the adopt path) so policy sync never
  fails closed with "managed sections missing" on a fresh tailnet.
  New endpoints: `GET /api/tsapi`, `POST /api/tsapi/validate`,
  `POST /api/tsapi`, `POST /api/tsapi/fences`.
- **Auto-minted auth keys on deploy.** With a credential configured,
  installing a service mints its own single-use, preauthorized,
  non-ephemeral `tag:tailarr` key (7-day TTL) via the keys API and writes it
  to the pod's key file (0600) — zero manual key entry. Pasting a key still
  works as an override; without a credential the old paste-or-error flow is
  unchanged (and now points at the wizard). The install forms collapse the
  auth-key field into an "Advanced" override once a credential exists.
- Version surfaced: `VERSION` constant in the controller, shown in the
  sidebar footer and on `GET /api/info`.

### Fixes

- **Deploys can no longer die on log initialization** (seen in production
  after a controller restart: `touch: ./.deployment.log: No such file or
  directory` before any other output). `LOG_FILE`/`ERROR_LOG_FILE` now
  resolve to absolute paths (the service dir, with a tmp fallback), logging
  no longer initializes at source time, every log-file write is best-effort
  (WARN and continue — never abort a deploy over a log file), and the
  controller runs `create.sh` from the pod's own directory with pinned
  absolute log paths instead of depending on an ambient CWD.
- **Boot-recovery wipe is no longer destructive on a healthy stack.**
  `start-pods.sh` (installed by `bootstrap-tailarr.sh`) only wipes podman's
  runroot when the API socket is genuinely unreachable AND no containers
  are Up — running it by hand on a live fleet no longer drops every
  container to `Created`.

### Compatibility

- Existing installs keep working unchanged: a hand-written `.tsapi.json`
  is picked up as before, pasted auth keys still win over minting, and the
  policy-sync fail-closed and `tag:tailarr*` prefix invariants are intact.

## v0.5.1 and earlier

See the git tag history (`git log v0.4.0..v0.5.1 --oneline`) — releases
predate this changelog.
