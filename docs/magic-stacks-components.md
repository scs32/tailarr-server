# Magic Stacks — a component model + an evolving wizard — design

**Status: DESIGN ONLY (2026-07-25). Nothing here is implemented yet.** This is
the agreed direction for turning Magic Stacks from a fixed, hand-wired saga into
a set of composable **components** that a stack maps to, and for making the
wizard render only the steps a given stack actually needs. It lands entirely on
the current Python + React codebase and reuses every existing wiring primitive.

## Motivation

Stephen's instinct: think in **components** (a shared folder, an indexer, a
usenet account…) and map a stack to the components it needs, so the wizard can
*evolve* — if a stack doesn't need a shared folder, it never asks for one (or it
assumes a default). Today's stacks are DATA (`STACKS` in `web/app.py`) but the
wizard's questions and the saga's steps are still hand-coded, and the `inputs`
field already on each stack is dead (nobody reads it). This design makes stacks
declare their components, and derives both the wizard and the saga from that
declaration.

## The core split: input components vs service components

Two kinds of component, and keeping them separate is what lets the wizard shrink:

- **Input components** — they ask the user for something and validate it live
  before anything deploys. Each *may* render a wizard step. Examples: media
  storage, indexer, usenet account, downloader choice.
- **Service components** — deployed and wired by the saga with **no questions**,
  derived entirely from the stack. Examples: the media managers (Arrs), the
  Prowlarr search hub, notifications wiring.

The wizard only ever renders steps for input components. Everything else happens
in the background progress stepper.

## Component catalog (what exists today)

### Input components

| Component | Asks for | Live validation | Provides |
|---|---|---|---|
| **`media`** (media storage) | one host folder | absolute path, exists | the `/data` root + `/data/media/{tv,movies,music}` — **as a real Shared Folder** (see below) |
| **`indexer`** | newznab URL + key (or a saved Accounts entry) | `_validate_newznab` (caps + authenticated probe) | `{url, key}` |
| **`usenet`** | host / port / ssl / user / pass (or a saved Accounts entry) | `_validate_usenet` (raw NNTP dial) | news-server credentials |
| **`downloader`** *(choice)* | pick `nzbget` **or** `sabnzbd` | none (a selection, not a secret) | the download client `{ip, port, creds}` |

### Service components

| Component | Deploys | Consumes | Wiring today |
|---|---|---|---|
| **`managers`** | sonarr / radarr / lidarr | `media`, `downloader`, `indexer`* | `_stack_wire_arr` (root folder + download client + indexer) |
| **`hub`** *(optional)* | prowlarr | `indexer`, `managers` | `_stack_wire_prowlarr` — and flips managers' `add_indexer` **off** |
| **`notifications`** | — | `managers` | `op_ntfy_wire` per Arr (graceful skip when ntfy absent) |

\* Managers wire the indexer *directly* only when there is no `hub`. When a hub
is present the indexer goes into Prowlarr once and syncs out as an Application.

### Future components (backlog stacks 3/4 — not built)

| Component | Deploys | Notes |
|---|---|---|
| **`portal`** | overseerr / jellyseerr | consumes `media_server` + `managers` |
| **`media_server`** | jellyfin | generates its own admin creds via the Startup API (R&D); **Plex excluded** — its claim flow is interactive, nothing is extractable |
| **`monitor`** | uptime-kuma | pre-monitor every pod; `.kuma.json` machinery already exists |

## Dependency graph

Each component declares `provides` and `consumes`; the saga is a topological
walk of the resulting graph. (Today's fixed order in `_stack_services` + the
`add_indexer` flag is a hand-rolled version of exactly this.)

```
media ─────┬─► managers      (root folders)
           ├─► downloader     (completed dir)
           └─► media_server   (libraries)

usenet ───► downloader (seed news server) ──► managers (download client)

indexer ──► hub (if present)  else  each manager

managers ──► hub (applications) ──► [managers skip their own indexer]
         └─► notifications, portal, monitor
```

## A stack becomes a component map

```js
"full-library": {
  name, blurb,
  components: {
    media:      { mode: "assume", default: "/srv/media" },
    indexer:    { mode: "ask" },
    usenet:     { mode: "ask" },
    downloader: { mode: "choice", options: ["nzbget", "sabnzbd"] },
    managers:   { pods: ["sonarr", "radarr", "lidarr"] },  // derived
    hub:        { pod: "prowlarr" },                        // derived
    notifications: {},                                      // derived
  }
}
```

`usenet-starter` is the same map minus `hub`, minus the music manager, with
`downloader` fixed to a single option (so no choice step renders).

## The evolving-wizard rule

The wizard renders **one step per input component**, gated by its `mode`:

- **`ask`** → always a step (indexer, usenet).
- **`choice`** → a step **only if** `options.length > 1` (the downloader picker
  already behaves this way).
- **`assume`** → **no step**; use `default`. Optionally surfaced on a final
  *Review* step so it's overridable without being in the user's face.
- **absent** (component not in the map) → never asked, never assumed; it simply
  doesn't exist for that stack.

This is the whole of "if you don't need a shared folder, it doesn't ask or it
assumes": set `media` to `assume` (default path, no step) or omit it entirely
for a stack that reuses existing storage.

## The `media` component IS a Shared Folder (decided)

Today the stack bind-mounts the user's chosen folder straight into each pod at
`/data` (`_stack_install_req`), bypassing the Shared Folders registry
(`.shares.json`). That creates two parallel worlds: stack pods mount media one
way, everything else via Shares. **Decision: the `media` component provisions a
real Shared Folder** named `media` (host path defaulting to **`/srv/media`**),
and the managers/downloader attach it by share name.

Why `/srv/media`: FHS-correct (`/srv` = "data served by this system"), an
obvious mount point for a dedicated disk, and it still maps to `/data` inside the
pods. It must be a single tree (media + downloads together on one filesystem) so
Sonarr/Radarr import by hardlink/atomic-move rather than slow copies.

Consequences:

- The wizard's media step reuses `FolderBrowser`, pre-filled with `/srv/media`,
  and its help text says *this becomes a Shared Folder* and links to the Shares
  page.
- On run, the component creates the share (`op_share_add`-equivalent) if absent
  — seed-once: an existing `media` share is the user's, left untouched — and the
  managers attach it via their `shares: ["media"]` list instead of a raw volume.
- The folder is now managed in one place, visible on the Shares page, reusable
  by non-stack pods, and NFS-exportable — the same first-class object everything
  else already uses.

## What this unlocks

- **Evolving/short wizard** — the immediate goal: only the needed steps render.
- **Upgrade my stack** — adding a component to a deployed stack = re-walk the
  graph for gaps only (the converge pattern already in `.stacks.json`).
- **Composable stacks** — overlapping component maps merge cleanly (own-what-you-
  name means shared objects like the indexer aren't duplicated).
- **Brownfield adopt** (future) — a component can *adopt* an existing pod as its
  member instead of deploying a new one.

## Migration path (small, incremental)

The engine primitives (`_stack_arr_ensure`, `_validate_newznab`,
`_validate_usenet`, `_stack_seed_downloader`, `_stack_wire_arr`,
`_stack_wire_prowlarr`) all stay — they just get **owned by named components**.

1. Define a `COMPONENTS` descriptor (provides/consumes, validate fn, deploy fn,
   wire fn, wizard step spec) and enrich each stack's dead `inputs` into a
   `components` map.
2. Make the saga derive its step list + order from the graph instead of the
   fixed sequence in `_stack_worker`.
3. Make the wizard render steps from `components` (mode-gated) instead of the
   hardcoded `<FormSection>`s.
4. Switch the `media` component to provision a Shared Folder (default
   `/srv/media`) and attach by share name.

None of these require a new OCI image or CI change; they're refactors of
existing data + control flow, covered by the existing stack test suite plus new
per-component cases.

## Guardrails carried over (unchanged intent)

- **Greenfield v1** — a stack is disabled when any of its service components
  collide with a deployed pod (kind-matched, not name-matched). Adopt-mode is
  future work.
- **Seed-once** — existing config (an occupied `Server1.Host`, an existing
  `media` share) is the user's; report "already configured", never overwrite.
- **Own-what-you-name** — Tailarr creates/updates only objects it named
  (`Tailarr nzbget`, `Tailarr indexer`, the `media` share); user objects are
  never touched.
- **Validate-first** — every input component's live check must pass before any
  deploy step runs.
