# Changelog

## v0.12.0 — private registry credentials (2026-07-19)

Pull private OCI images (e.g. a private GHCR package) everywhere Tailarr
pulls:

- **Settings → Private registries**: add a registry host + username +
  token (for GitHub, a PAT with `read:packages`). The credential is
  validated with a real `podman login` before saving, stored `0600` in
  `Pods/.registries.json`, and never returned by the API.
- The store renders to `Pods/.registry-auth.json`, a standard
  containers-auth file. Generated `run.sh` scripts export
  `REGISTRY_AUTH_FILE` when it exists (pre-existing pods pick this up
  after "Finish upgrade" / a fleet rerender), and the controller's own
  `podman pull` (Update button) and `skopeo inspect` (update checks)
  use it too.
- New API: `GET /api/registries`, `POST /api/registries`
  (`do: save|delete`).

## v0.11.0 — OAuth-only install; simpler Settings (2026-07-19)

Direction change from the field: Tailarr's model is a dedicated tailnet
and one OAuth client, so the product now commits to it.

- **Install requires the OAuth client.** `TS_AUTHKEY` and `TS_API_TOKEN`
  are no longer install paths — `install.sh`/`bootstrap-tailarr.sh` take
  `TS_API_CLIENT_ID` + `TS_API_CLIENT_SECRET` (re-runs on an enrolled
  controller still need nothing). README trimmed to the one path.
- **Settings simplified.** The API-tokens card is hidden for now (the
  backend `/api/tokens` gate remains; the UI returns when the token
  story is designed around the app). The credential card just says
  "Configured."
- **Server-grant warning is one line**: "Adding this gives full admin
  rights to this device."

## v0.10.3 — plain-language copy (2026-07-19)

Stephen read the token card and said "what the HECK does this mean."
He was right. UI-only release:

- Tokens card: explains the no-login model in plain words ("like a
  password", "this page locks you out too") instead of "the historical
  model" / "network path" / "authorizes".
- Post-upgrade step 2 is no longer "Apply engine updates to all pods"
  (infrastructure-speak): the alert says your pods still run the
  previous version's settings, the button says "Finish upgrade", the
  result says "Updated N pod(s). Upgrade complete."
- Server-grant confirm and require-token confirm rewritten in the same
  voice; Shares "not visible" → "folder not found on host".

## v0.10.2 — policy sync on controller start (2026-07-19)

Second live-caught gap of the day: releases can ADD managed tags and
grants (v0.10.0's `can-server`), but the policy fences only synced on
mutating actions (installs, share changes, adopt). An upgraded-but-idle
controller kept serving the previous release's policy, so the first
"grant Tailarr Server" click after upgrading failed with `tags API:
requested tags [tag:tailarr-can-server] are invalid or not permitted`.

The controller now runs one policy sync at startup (when a credential
is configured), which also triggers the identity-tag reconcile. Upgrade
→ restart → fences match the running release, no manual action.

## v0.10.1 — OAuth client can self-assign tag:tailarr-ctrl (2026-07-19)

First live run of the one-credential bootstrap on a fresh tailnet
caught a real bug: the managed `tagOwners` gave `tag:tailarr-ctrl` to
`autogroup:admin` only, but the OAuth client *acts as*
`tag:tailarr-ctrl` and may only assign tags that tag owns — and a tag
does not own itself implicitly. Result: everything worked EXCEPT the
controller-start reconcile could never apply `tag:tailarr-ctrl` to the
controller sidecar (`tags API: requested tags [tag:tailarr-ctrl] are
invalid or not permitted`). Full-access static tokens masked the bug by
acting as `autogroup:admin`.

- The fence generator now emits
  `"tag:tailarr-ctrl": ["autogroup:admin", "tag:tailarr-ctrl"]`
  (self-owning); existing installs converge on their next policy sync.
- README: the recommended install is now a **dedicated tailnet** — a
  complete paste-ready Access Controls file (default-deny from day one,
  admin grant outside the fence, self-owning ctrl stub inside), with the
  splice-into-existing-tailnet path kept as the alternative.
- Controller image sets `PYTHONUNBUFFERED=1` — `podman logs tailarr`
  showed nothing until the stdout block buffer flushed.

## v0.10.0 — share the server itself: can-server + API tokens (2026-07-19)

The Tailarr app's server module made the controller a service every app
user needs to reach — but the ACL design hard-refused to share it
("admin device or nothing"), because a network grant to a no-auth API is
full control of the fleet. This lifts that rule as a pair: a tag opens
the pipe, a token authorizes it.

- **"server" pseudo-service on the Users page**: granting it flips
  `tag:tailarr-can-server` on the device — a fenced grant to
  `tag:tailarr-ctrl:443`, same instant tag-flip share/revoke as any
  service. The grant confirm spells out what it means.
- **API bearer tokens** (Settings → API access): mint per-client tokens
  (shown once, stored as sha256 in `Pods/.tokens.json` 0600), then flip
  "require" — every `/api/*` request now needs
  `Authorization: Bearer …`. `/api/info` stays open (self-upgrade health
  gate, the app's pre-auth compat probe). No lockout states: require
  refuses to enable with zero tokens, and deleting the last token
  auto-relaxes it.
- The web UI sends its own token from localStorage (paste or one-click
  "Use in this browser" at mint time).
- Tokens are all-or-nothing for now; scoped/read-only roles are the
  natural next cut. See docs/acl-design.md §9.

## v0.9.9 — one-credential install (2026-07-19)

The installer previously demanded a hand-made Tailscale auth key even
though a configured API credential lets Tailarr mint its own keys — the
auth key was only ever needed for the controller's first enrollment.

- **Bootstrap accepts an OAuth client** (`TS_API_CLIENT_ID` +
  `TS_API_CLIENT_SECRET`) or a static API token (`TS_API_TOKEN`) as an
  alternative to `TS_AUTHKEY`. It seeds the controller's API credential
  (`.tsapi.json`, mode 600), initializes the tailarr-managed policy
  fences (policy-before-mint: a tag must be in `tagOwners` before a key
  for it can be minted), then mints the controller's own single-use
  `tag:tailarr` key — tagging, ACLs, and per-pod key minting work from
  first boot with no Settings wizard.
- The adopt/mint path runs the controller image's own code in a
  one-shot container (the same `op_policy_init_fences` +
  `ts_mint_pod_key` behind the Settings wizard) — no shell
  reimplementation of policy splicing.
- The container-MTU fix now runs before any container does (the
  credential path talks TLS to api.tailscale.com from a container;
  nested guests at MTU <1500 would blackhole it).
- README documents the OAuth client setup: write scopes for Auth Keys /
  Devices / Policy File, tagged `tag:tailarr-ctrl`, with the
  `tagOwners` stub pasted inside the fence markers so adopt takes it
  over instead of duplicating the key.
- The plain `TS_AUTHKEY` path is unchanged.

## v0.9.3 — no more silent svc-tag failures (2026-07-16)

Field report (HIGH): a freshly deployed service could be permanently
unreachable by every user device while looking fully green everywhere.
The per-service grant's dst is `tag:tailarr-svc-<name>` on the sidecar
node — if the one background tagging attempt after pod start failed
(node not enrolled yet, or the tagOwners policy sync hadn't landed so the
tags API rejected it), nothing retried, nothing reported, and the packet
filter silently dropped every user connection. The controller's broad
`tag:tailarr` grant still reached the service, so health stayed clean.

- **Tagging retries with backoff** (~75s window) instead of one silent
  shot, and treats "tags API rejected" — the tagOwners race — as
  retryable. Failures are logged.
- **Reconciliation on every natural event**: controller start, every
  successful policy sync, and a 15-minute maintenance pass re-assert
  identity tags on all running sidecars (idempotent, one devices read; a
  write only when wrong). A missed tag now self-heals instead of waiting
  for that specific pod to restart — which nothing ever did.
- **The failure is visible**: `/api/pods` entries carry
  `identity: ok|missing|unknown`, and the dashboard shows a red
  "identity tag missing" chip explaining that user devices are blocked
  and that reconcile will retry. A service unreachable by all users can
  no longer look fully green.

## v0.9.2 — blank host paths no longer break deploys (2026-07-16)

- **Fix (HIGH, field report):** deploying a catalog app with the shared
  `/data` mount attached and the app's own media path left blank (the
  recommended pattern — e.g. Radarr without filling `/movies`) rendered
  `-v :/movies`, and podman failed at start with "host directory cannot
  be empty". The engine now drops any volume whose host path is blank,
  whitespace, or a bare `:ro` — at parse time, so neither `run.sh` nor
  `.config.json` carries it and re-renders can't resurface it. Applies to
  catalog installs, custom pods, and reconfigures alike.

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
