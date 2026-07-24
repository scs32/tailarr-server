# Tailarr footprint containment + reproducible removal — design

**Status: DESIGN ONLY (2026-07-24). Nothing here is implemented.** This is the
agreed plan for shrinking Tailarr's perceived on-host footprint and giving it a
deterministic, near-100% removal path — captured so it can be picked up later.
It lands entirely on the current Python codebase and stays forward-compatible
with a future single-binary (Go) port.

## Motivation

Stephen's instinct: a self-hosted tool should not feel like it "craps up" the
machine, and cleanup should be *"delete this,"* not a scavenger hunt. Today's
footprint (see [`what-tailarr-installs.md`](what-tailarr-installs.md)) is small
but scattered — a handful of files across `/etc`, `/root`, `/run`, `/var/log`,
plus podman's multi-GB image store under `/var/lib/containers`, plus tailnet
cloud objects. Removal is a hand-written checklist that can drift.

We considered folding everything into a single Go binary. Verdict: the
single-binary *end state* is good, but a Python→Go rewrite **now** is bad ROI —
it re-opens months of live-caught edge cases (ACL splicing, upgrade rollback,
identity reconcile) for **zero new user value** while the feature surface is
still moving. The right move is the incremental path below, which captures ~90%
of the "delete this, clean" benefit cheaply and defers the rewrite until the
design stabilizes. (See the single-binary discussion in session notes.)

## Strategy (one sentence)

Funnel every host mutation through one recorded chokepoint, contain everything
Tailarr owns under one root (including podman's image store), and make removal
a data-driven `uninstall` that reverses a written receipt.

## The two mechanisms

1. **The install manifest** — `<root>/.manifest.json`, an append-only receipt
   of every artifact Tailarr creates *outside* its data root: packages (and
   whether *we* installed them vs. found them), system files (created vs.
   one-line-appended), systemd units, and tailnet objects (device IDs, tags,
   fence markers, minted keys). Removal is driven by this data, not a checklist.
2. **The data root** — one directory (`/root/Pods` today) that holds config
   **and** podman's graphroot/runroot, so `rm -rf` reclaims the gigabytes too,
   not just JSON.

---

## Phase 0 — Contain the storage (biggest felt win, cheap)

- Point podman's `graphroot`/`runroot` at `<root>/storage` via a
  Tailarr-written `storage.conf` + `CONTAINERS_STORAGE_CONF` env on every
  podman entrypoint: the `podman system service` line in
  `bootstrap-tailarr.sh` and `start-pods.sh`, the controller's `podman()`
  wrapper in `web/app.py`, and generated `run.sh`. Result: all service
  images/containers live inside the data root.
- Move `/root/start-pods.sh` → `<root>/bin/start-pods.sh`; logs →
  `<root>/logs/`. After this, `/etc` holds only the one systemd unit (plus the
  optional NFS export); everything else is inside the root.
- ⚠️ **Fresh-installs only** at first — relocating storage orphans an existing
  install's images. A migration tool is a later, separate item.

## Phase 1 — The manifest (reproducibility backbone)

- Add one `host_mutation(kind, target, detail)` helper that **every**
  host-touching path calls (MTU line, systemd unit, drop-in, exports, package
  installs, start-pods, sentinel) — it performs the write *and* records it.
- Record enough to reverse precisely: for `containers.conf` we *appended* a
  line → store a marker so uninstall strips just that line, never the file. For
  packages, record only what Tailarr actually installed — never remove what we
  didn't add.
- The bootstrap seeds the first entries; the controller appends the rest.
  Backfill a best-effort manifest on upgrade for existing installs.

## Phase 2 — `tailarr uninstall` (the remove path)

Three delivery surfaces so it works in every state:

- a **subcommand** in the image (`podman exec tailarr tailarr-uninstall …`),
- a **Settings action** (with a dry-run preview), and
- a standalone **`uninstall.sh`** fetched raw from `main` (for when the
  controller is already gone) that reads the manifest.

Ordered, idempotent behavior:

1. *(opt-in, explicit)* tailnet teardown via the stored OAuth client — delete
   Tailarr's devices, remove the fenced ACL regions, revoke minted keys.
2. Stop + remove all pods and sidecars.
3. Reverse each manifest entry (strip the MTU line, remove unit + drop-in +
   start-pods + sentinel, `exportfs -ra` after removing the export).
4. Remove packages *we* installed (guarded: only if present-by-us and nothing
   else depends).
5. Delete the data root (now includes image storage).

Flags: `--dry-run` (prints the exact reversal plan), `--keep-data`,
`--keep-tailnet`. Resilient to a half-installed host.

## Phase 3 — Transparency UI ("Footprint")

- Settings → **"What's on this host"**: renders the manifest — the data root
  and its size, the single systemd unit, packages, tailnet devices — with a
  one-click **Uninstall** that shows the dry-run first. This is what fixes the
  *feeling*: the user sees it's all accounted for and reversible.

## Phase 4 — Docs

- Update [`what-tailarr-installs.md`](what-tailarr-installs.md): replace the
  hand-checklist with "the manifest + `tailarr uninstall` is the supported
  path," and document the containment.

---

## Honest limits (so "near-100%" is truthful)

- **Tailnet cloud objects** need the OAuth client still valid. If the user
  already revoked it, uninstall can't delete devices/fences — so the manifest
  **lists the device IDs and fence markers** for a 30-second manual cleanup.
  That's the gap between "near" and "100%."
- **Package removal** stays conservative — shared deps and other podman
  workloads mean we only remove what we're certain we added.

## Decisions (defaults; revisit if needed)

1. **Data root stays `/root/Pods`** (no rename to `/var/lib/tailarr`) — avoids
   migration churn and muscle-memory breakage; it is already effectively the
   root, we just pull storage into it.
2. **Storage containment is fresh-install-only** initially; migration for
   existing installs is a follow-up.
3. **Cloud teardown is opt-in** in uninstall (off by default) because it is
   destructive and irreversible — dry-run always shows it first.

## Sequencing

Phase 0 + Phase 2 alone deliver the "delete this, clean" outcome and are the
cheap wins to ship first. Phase 1 (manifest) makes Phase 2 *reproducible*
rather than checklist-based — do 1 and 2 together. Phase 3 is the polish that
sells the trust story. None of this requires the Go rewrite; it all lands on
the current codebase and stays forward-compatible with a future single-binary
port.
