# Podscale ACL design — capability tags + fenced policy authoring

**Status: DESIGN ONLY (2026-07-05, revised same day after design review with
Stephen). Nothing here is implemented, and nothing has been applied to the
live tailnet.** Executing sessions: do NOT call the Tailscale API, modify the
live policy file, or create OAuth clients from this document alone.

**Design principles (locked):**
1. Everything Podscale does is **tag-based** wherever possible — no reliance
   on autogroups quirks, device ownership, or the shape of the human's
   existing policy. It must work for the majority of tailnets, not one.
2. Podscale owns **only its own fenced section** of the policy file and only
   its own tag namespace. Blast radius is bounded by construction.
3. **No tiers.** Access is per-service, granted explicitly per machine.
4. Pods mutually trust each other: the fleet is **one trust zone**.
5. **Base posture (locked 2026-07-05):** (a) admin reaches everything;
   (b) a `tag:podscale-user` machine can never reach anything outside the
   fleet — enforced structurally (§3 invariant), layered on deliberately by
   the human outside the fence if ever needed.

---

## 0. Security prerequisite — controller lockdown (FIRST, non-negotiable)

Podscale's standing assumption is *tailnet-only = trusted*: the controller
has no auth, holds the podman socket, and is root-equivalent over the fleet.
That breaks the moment the first non-admin auth key is handed out. Nothing
below ships to users before the generated policy makes the controller
(`tag:podscale-ctrl`) reachable only by the operator. The stakes rise again
once the controller holds an OAuth client that can write the policy file: a
compromised controller could rewrite the tailnet ACL. Mitigations in §5; the
primary control is that non-admin machines can never reach the controller.

## 1. Division of labor

- **Podscale = authoring.** Compiles intent ("Dave may reach sonarr") into
  tags and grants.
- **Tailscale = enforcement.** The WireGuard packet filter enforces it.
- **Podscale is NOT in the data path and must never become a reverse proxy**
  — that would destroy the zero-published-ports architecture.
- Header-based per-user authz (`Tailscale-App-Capabilities`, WhoIs) only
  works for capability-aware apps; for the standard catalog the enforcement
  primitive is reachability. A later layer, not the foundation.

## 2. The tag model — identity tags + capability tags

Everything lives under the `tag:podscale*` prefix. The prefix IS the
ownership rule (§4).

| Tag | Lives on | Meaning |
|---|---|---|
| `tag:podscale` | every sidecar | fleet membership → intercom grant |
| `tag:podscale-ctrl` | controller sidecar | never shareable (hard generator rule) |
| `tag:podscale-svc-<name>` | that service's sidecar | what this node **is** |
| `tag:podscale-user` | consumer machines | what this machine **is** (inventory; zero access by itself) |
| `tag:podscale-can-<name>` | consumer machines | what this machine **may reach** (capability badge) |
| `tag:podscale-public` | sidecars | funnel nodeAttr marker |
| `tag:podscale-admin` | dedicated admin machines | full access; optional — see below |

Key decisions embedded here:

- **Admin is dual-form.** The human-owned admin grant accepts
  `["tag:podscale-admin", "autogroup:admin"]` as src. Solo operators need
  zero setup (their devices are already autogroup:admin); headless/dedicated
  admin boxes get the tag. Deliberately NOT forcing operators to tag their
  personal devices: tagging is one-way, changes key-expiry behavior, and
  breaks user-owned-device features (notably Taildrop).

- **Born tagged.** Tags ride the auth key, so devices are never user-owned
  and the "tagging is one-way / drops the user owner" problem never arises.
  Sidecar keys carry `tag:podscale` + `tag:podscale-svc-<name>`; handed-out
  consumer keys carry `tag:podscale-user`. (Install already requires a key;
  the flow barely changes. With the OAuth client, Podscale mints a fresh
  single-use tagged key per install / per person — strictly better than
  reusable-key handling.)
- **Attribution lives in the key's tags.** There is NO device→auth-key
  mapping in the Tailscale API (audit logs only). The tag on the key is how
  Podscale knows what an enrolling device is. Consequence: **a reusable key
  is an identity, not just a credential** — prefer single-use / short-expiry
  keys minted per person, and surface group membership in the UI so
  unexpected devices are visible.
- **`can-<svc>` is deliberately distinct from `svc-<svc>`.** If a consumer
  machine wore the sidecar's own tag, any `svc-X → svc-X` grant would be
  symmetric and the pod could initiate connections to the consumer's
  machine. `can- → svc-` keeps grants directional.
- **One trust zone.** `tag:podscale → tag:podscale` lets any pod reach any
  pod (sonarr→nzbget etc). Rationale: nobody will maintain a pod adjacency
  matrix, and defaults we ship would be wrong for someone. A compromised pod
  reaches other pods — and, by the prefix rule, nothing else on the tailnet.
- **No tiers.** Considered and rejected: per-service capability badges give
  finer granularity with no extra abstraction, and (see §3) sharing no
  longer touches the policy file at all, which was the only argument tiers
  had.

## 3. Grants — static per service; sharing is tag membership

The **admin grant lives outside the fence** (operator sovereignty — Podscale
never edits it); the fenced block is mechanical and only changes on
install/remove:

```jsonc
// human-owned: admin reaches everything (dual-form, see §2)
{"src": ["tag:podscale-admin", "autogroup:admin"], "dst": ["*"], "ip": ["*"]},

// >>> PODSCALE MANAGED — do not edit; regenerated by podscale
{"src": ["tag:podscale"],             "dst": ["tag:podscale"],             "ip": ["*"]},   // fleet intercom
{"src": ["tag:podscale-can-sonarr"],  "dst": ["tag:podscale-svc-sonarr"],  "ip": ["443"]}, // one line per
{"src": ["tag:podscale-can-jellyfin"],"dst": ["tag:podscale-svc-jellyfin"],"ip": ["443"]}, // installed service
// <<< PODSCALE MANAGED
```

**Invariant (base posture b):** a `tag:podscale-user` machine only ever
appears as `src` in `can-X → svc-X` lines, whose dst is inside
`tag:podscale*` by the prefix rule — so user machines are *structurally
incapable* of being granted anything outside the fleet, even by a Podscale
bug. Anything beyond that is layered on by the human, outside the fence.

- **Install service** → add its `svc-`/`can-` grant line + tagOwners entries
  (policy write). **Remove service** → delete them (policy write).
- **Share / revoke** → add/remove `tag:podscale-can-<svc>` on the consumer's
  device via `POST /api/v2/device/{id}/tags` — REPLACES the whole tag set,
  so read-modify-write, serialized through the busy registry. **No policy
  write.** Access changes are instant, low-blast-radius tag flips.
- Under default-deny, `svc-sonarr` is reachable by: badge-holders, the
  operator, and other pods. Nothing else. A `tag:podscale-user` key grants
  access to exactly nothing until a badge is added.
- Consumer grants are port-443-only (`tailscale serve` fronts every service).
- The controller gets NO `can-` tag ever; `tag:podscale-ctrl` appears in no
  consumer grant (generator hard-refuses).
- Whether default-deny is in force is the tailnet owner's choice, outside
  the fence. Our section behaves identically under allow-all (inert labels)
  or default-deny (live policy); Podscale's docs recommend default-deny and
  the onboarding flow should detect an allow-all wildcard grant and warn.

### The Users page (the UX this buys)

Every device wearing `tag:podscale-user`, with a Podscale-side nickname
registry (`$PODS_DIR/.users.json`, keyed by stable node ID — deliberately
blurring machine/user for v1), last-seen from the devices API, and a row of
per-service checkboxes. Checking "sonarr" on Dave's row = one tags API call.
Per-person view of the per-service sharing moat.

## 4. Owning our section — fences for mechanics, prefix for safety

Two mechanisms, different jobs; both required:

- **Comment fences = mechanical splice.** HuJSON round-trips comments (the
  POST body is literal text), so Podscale does **line-level replacement**
  between `// >>> PODSCALE MANAGED` … `// <<< PODSCALE MANAGED` markers —
  one fenced block per section it manages (`grants`, `tagOwners`,
  `nodeAttrs`). It never parses-and-reserializes the human's file: their
  comments, ordering, and formatting survive byte-for-byte.
- **The prefix = safety invariant.** Generator hard rule: nothing inside a
  fence may reference a name outside `tag:podscale*`, with exactly two
  whitelisted exceptions: `autogroup:admin` (operator src / tagOwners
  values) and nothing else. Even a generator bug cannot grant access to the
  human's other machines.
- **Fail closed.** Missing fence pair, duplicate/nested markers, or fence
  content violating the prefix rule → refuse to write and tell the human to
  re-run **adopt** (the one-time step that appends fresh fenced blocks to
  each section). Never guess; never touch anything outside the fences.

Managed tagOwners + nodeAttrs blocks:

```jsonc
"tagOwners": {
    // ...human-owned tags untouched...
    // >>> PODSCALE MANAGED
    "tag:podscale":              ["autogroup:admin"],
    "tag:podscale-ctrl":         ["autogroup:admin"],
    "tag:podscale-user":         ["autogroup:admin"],
    "tag:podscale-public":       ["autogroup:admin"],
    "tag:podscale-svc-sonarr":   ["autogroup:admin"],   // + per installed
    "tag:podscale-can-sonarr":   ["autogroup:admin"],   //   service pair
    // <<< PODSCALE MANAGED
},
"nodeAttrs": [
    // >>> PODSCALE MANAGED
    {"target": ["tag:podscale-public"], "attr": ["funnel"]},
    // <<< PODSCALE MANAGED
],
```

(When the OAuth client exists, its identity is added to the managed
tagOwners values so Podscale may assign the tags it defines.)

### Write cycle (policy file)
`GET /api/v2/tailnet/{t}/acl` (capture `ETag`) → line-splice the fenced
regions → `POST /acl/validate` (refuse on error) → `POST` with `If-Match` →
on 412 refetch + regenerate + retry. Keep last-known-good policy as a local
backup with one-call rollback.

### Credentials
OAuth client scoped to exactly `devices:write` + `acl:write` (+ `auth_keys`
for minting tagged keys), stored in the controller. This is what makes §0
non-negotiable.

## 5. Safety rails (all mandatory)

- Validate before every apply; fail closed on fence/prefix violations.
- Last-known-good backup + rollback before each policy write.
- ETag concurrency on every POST; never blind-overwrite.
- Serialize all tag + policy writes through the busy registry.
- Generator refuses: controller shares, non-prefix names in fences, consumer
  grants on ports other than 443.
- Minted keys: single-use / short-expiry by default; Users page surfaces
  every `tag:podscale-user` device so unexpected enrollments are visible.

## 6. Funnel tie-in

**Two hard-won operational facts (verified live 2026-07-06):**
1. **Funnel ingress traffic is NOT exempt from the packet filter** under
   default-deny (open bug tailscale/tailscale#18181). The managed grants
   block therefore always contains
   `{"src": ["fd7a:115c:a1e0:ab12::/64"], "dst": ["tag:podscale-public"], "ip": ["*"]}`
   — Tailscale's funnel ingress range → public-tagged pods. Without it the
   node logs `Drop: ... no rules matched` and public requests reset.
2. **Funnel needs the node's tailnet IPv6, and IPv6 refuses to run on links
   with MTU < 1280.** The old sidecar `TS_DEBUG_MTU=1200` silently broke
   Funnel (public requests hang); sidecars run 1280 as of v0.3.4. Sidecars
   created before that need one restart to become publicly reachable.

The shipped Make-public button (v0.3.0) flips `AllowFunnel` in the pod's
serve config, but tailscaled refuses without the **`funnel` nodeAttr** on the
node. The managed nodeAttrs block targets `tag:podscale-public`; making a pod
public becomes: add `tag:podscale-public` to the sidecar (tags API) + flip
AllowFunnel (already shipped). Fully tag-based — no hardcoded IPs — and the
same device-tags call as sharing. Until the automation exists, the manual
bridge is adding the sidecar's tailnet IP to a funnel nodeAttr by hand.

## 7. The baseline policy (clean slate — DECIDED 2026-07-05)

All old projects on the tailnet are declared dead, **tsidp is
decommissioned**, and the legacy policy (wildcard grant, tsidp cap, ssh
block, ~20 unused tags) is disposable. The complete replacement:

```jsonc
{
    // ===== human-owned: operator sovereignty, Podscale never edits =====
    "grants": [
        {"src": ["tag:podscale-admin", "autogroup:admin"], "dst": ["*"], "ip": ["*"]},

        // >>> PODSCALE MANAGED — do not edit; regenerated by podscale
        {"src": ["tag:podscale"], "dst": ["tag:podscale"], "ip": ["*"]},
        // (one can-X → svc-X line per installed service appears here)
        // <<< PODSCALE MANAGED
    ],

    "tagOwners": {
        "tag:podscale-admin": ["autogroup:admin"],
        // >>> PODSCALE MANAGED
        "tag:podscale":        ["autogroup:admin"],
        "tag:podscale-ctrl":   ["autogroup:admin"],
        "tag:podscale-user":   ["autogroup:admin"],
        "tag:podscale-public": ["autogroup:admin"],
        // (+ svc-/can- pair per installed service)
        // <<< PODSCALE MANAGED
    },

    "nodeAttrs": [
        // >>> PODSCALE MANAGED
        {"target": ["tag:podscale-public"], "attr": ["funnel"]},
        // <<< PODSCALE MANAGED
    ],
}
```

### Migration notes (Stephen's tailnet)

- **Sequencing warning:** the moment this policy saves, default-deny is live.
  The operator's own access survives via `autogroup:admin`, but the fleet's
  pod→pod traffic breaks until the sidecars are re-tagged with
  `tag:podscale` — current sidecars are **user-owned and untagged** (enrolled
  with a plain reusable key). Apply the policy and re-tag the fleet in the
  same sitting, or stay on the old policy until the tagging step of the
  build.
- Re-tag via the devices API (sidecars become tag-owned — now the intended
  state) or re-enroll with a new tagged key; future installs use tagged keys.
- Delete the decommissioned tsidp node and other dead-project machines from
  the admin console while at it.

## 8. Build order (when executed)

1. §0 + adopt: human applies the initial fenced skeleton (or clean minimal
   policy) in the admin console; default-deny; controller locked down.
2. OAuth client created + stored; tagged-key minting for installs.
3. Device-tagging flow: sidecar svc tags on install, `can-` badges from the
   Users page, `tag:podscale-public` from the Make-public button.
4. Fenced-grant generator + ETag read-modify-write apply path.
5. Only then: hand out the first `tag:podscale-user` key.
