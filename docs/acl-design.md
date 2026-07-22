# Tailarr ACL design — capability tags + fenced policy authoring

**Status: DESIGN ONLY (2026-07-05, revised same day after design review with
Stephen). Nothing here is implemented, and nothing has been applied to the
live tailnet.** Executing sessions: do NOT call the Tailscale API, modify the
live policy file, or create OAuth clients from this document alone.

**Design principles (locked):**
1. Everything Tailarr does is **tag-based** wherever possible — no reliance
   on autogroups quirks, device ownership, or the shape of the human's
   existing policy. It must work for the majority of tailnets, not one.
2. Tailarr owns **only its own fenced section** of the policy file and only
   its own tag namespace. Blast radius is bounded by construction.
3. **No tiers.** Access is per-service, granted explicitly per machine.
4. Pods mutually trust each other: the fleet is **one trust zone**.
5. **Base posture (locked 2026-07-05):** (a) admin reaches everything;
   (b) a `tag:tailarr-user` machine can never reach anything outside the
   fleet — enforced structurally (§3 invariant), layered on deliberately by
   the human outside the fence if ever needed.

---

## 0. Security prerequisite — controller lockdown (FIRST, non-negotiable)

Tailarr's standing assumption is *tailnet-only = trusted*: the controller
has no auth, holds the podman socket, and is root-equivalent over the fleet.
That breaks the moment the first non-admin auth key is handed out. Nothing
below ships to users before the generated policy makes the controller
(`tag:tailarr-ctrl`) reachable only by the operator. The stakes rise again
once the controller holds an OAuth client that can write the policy file: a
compromised controller could rewrite the tailnet ACL. Mitigations in §5; the
primary control is that non-admin machines can never reach the controller.

## 1. Division of labor

- **Tailarr = authoring.** Compiles intent ("Dave may reach sonarr") into
  tags and grants.
- **Tailscale = enforcement.** The WireGuard packet filter enforces it.
- **Tailarr is NOT in the data path and must never become a reverse proxy**
  — that would destroy the zero-published-ports architecture.
- Header-based per-user authz (`Tailscale-App-Capabilities`, WhoIs) only
  works for capability-aware apps; for the standard catalog the enforcement
  primitive is reachability. A later layer, not the foundation.

## 2. The tag model — identity tags + capability tags

Everything lives under the `tag:tailarr*` prefix. The prefix IS the
ownership rule (§4).

| Tag | Lives on | Meaning |
|---|---|---|
| `tag:tailarr` | every sidecar | fleet membership → intercom grant |
| `tag:tailarr-ctrl` | controller sidecar | shareable only as the `server` pseudo-service + API token (§9) |
| `tag:tailarr-svc-<name>` | that service's sidecar | what this node **is** |
| `tag:tailarr-user` | consumer machines | what this machine **is** (inventory; zero access by itself) |
| `tag:tailarr-can-<name>` | consumer machines | what this machine **may reach** (capability badge) |
| `tag:tailarr-public` | sidecars | funnel nodeAttr marker |
| `tag:tailarr-admin` | dedicated admin machines | full access; optional — see below |

Key decisions embedded here:

- **Admin is dual-form.** The human-owned admin grant accepts
  `["tag:tailarr-admin", "autogroup:admin"]` as src. Solo operators need
  zero setup (their devices are already autogroup:admin); headless/dedicated
  admin boxes get the tag. Deliberately NOT forcing operators to tag their
  personal devices: tagging is one-way, changes key-expiry behavior, and
  breaks user-owned-device features (notably Taildrop).

- **Born tagged.** Tags ride the auth key, so devices are never user-owned
  and the "tagging is one-way / drops the user owner" problem never arises.
  Sidecar keys carry `tag:tailarr` + `tag:tailarr-svc-<name>`; handed-out
  consumer keys carry `tag:tailarr-user`. (Install already requires a key;
  the flow barely changes. With the OAuth client, Tailarr mints a fresh
  single-use tagged key per install / per person — strictly better than
  reusable-key handling.)
- **Attribution lives in the key's tags.** There is NO device→auth-key
  mapping in the Tailscale API (audit logs only). The tag on the key is how
  Tailarr knows what an enrolling device is. Consequence: **a reusable key
  is an identity, not just a credential** — prefer single-use / short-expiry
  keys minted per person, and surface group membership in the UI so
  unexpected devices are visible.
- **`can-<svc>` is deliberately distinct from `svc-<svc>`.** If a consumer
  machine wore the sidecar's own tag, any `svc-X → svc-X` grant would be
  symmetric and the pod could initiate connections to the consumer's
  machine. `can- → svc-` keeps grants directional.
- **One trust zone.** `tag:tailarr → tag:tailarr` lets any pod reach any
  pod (sonarr→nzbget etc). Rationale: nobody will maintain a pod adjacency
  matrix, and defaults we ship would be wrong for someone. A compromised pod
  reaches other pods — and, by the prefix rule, nothing else on the tailnet.
- **No tiers.** Considered and rejected: per-service capability badges give
  finer granularity with no extra abstraction, and (see §3) sharing no
  longer touches the policy file at all, which was the only argument tiers
  had.

## 3. Grants — static per service; sharing is tag membership

The **admin grant lives outside the fence** (operator sovereignty — Tailarr
never edits it); the fenced block is mechanical and only changes on
install/remove:

```jsonc
// human-owned: admin reaches everything (dual-form, see §2)
{"src": ["tag:tailarr-admin", "autogroup:admin"], "dst": ["*"], "ip": ["*"]},

// >>> TAILARR MANAGED — do not edit; regenerated by tailarr
{"src": ["tag:tailarr"],             "dst": ["tag:tailarr"],             "ip": ["*"]},   // fleet intercom
{"src": ["tag:tailarr-can-sonarr"],  "dst": ["tag:tailarr-svc-sonarr"],  "ip": ["443"]}, // one line per
{"src": ["tag:tailarr-can-jellyfin"],"dst": ["tag:tailarr-svc-jellyfin"],"ip": ["443"]}, // installed service
// <<< TAILARR MANAGED
```

**Invariant (base posture b):** a `tag:tailarr-user` machine only ever
appears as `src` in `can-X → svc-X` lines, whose dst is inside
`tag:tailarr*` by the prefix rule — so user machines are *structurally
incapable* of being granted anything outside the fleet, even by a Tailarr
bug. Anything beyond that is layered on by the human, outside the fence.

- **Install service** → add its `svc-`/`can-` grant line + tagOwners entries
  (policy write). **Remove service** → delete them (policy write).
- **Share / revoke** → add/remove `tag:tailarr-can-<svc>` on the consumer's
  device via `POST /api/v2/device/{id}/tags` — REPLACES the whole tag set,
  so read-modify-write, serialized through the busy registry. **No policy
  write.** Access changes are instant, low-blast-radius tag flips.
- Under default-deny, `svc-sonarr` is reachable by: badge-holders, the
  operator, and other pods. Nothing else. A `tag:tailarr-user` key grants
  access to exactly nothing until a badge is added.
- Consumer grants are port-443-only (`tailscale serve` fronts every service).
- The controller gets NO `can-` tag ever; `tag:tailarr-ctrl` appears in no
  consumer grant (generator hard-refuses). *Superseded by §9: the `server`
  pseudo-service grants `tag:tailarr-can-server → tag:tailarr-ctrl:443`,
  with API bearer tokens as the permission boundary behind it.*
- Whether default-deny is in force is the tailnet owner's choice, outside
  the fence. Our section behaves identically under allow-all (inert labels)
  or default-deny (live policy); Tailarr's docs recommend default-deny and
  the onboarding flow should detect an allow-all wildcard grant and warn.

### The Users page (the UX this buys)

Every device wearing `tag:tailarr-user`, with a Tailarr-side nickname
registry (`$PODS_DIR/.users.json`, keyed by stable node ID — deliberately
blurring machine/user for v1), last-seen from the devices API, and a row of
per-service checkboxes. Checking "sonarr" on Dave's row = one tags API call.
Per-person view of the per-service sharing moat.

## 4. Owning our section — fences for mechanics, prefix for safety

Two mechanisms, different jobs; both required:

- **Comment fences = mechanical splice.** HuJSON round-trips comments (the
  POST body is literal text), so Tailarr does **line-level replacement**
  between `// >>> TAILARR MANAGED` … `// <<< TAILARR MANAGED` markers —
  one fenced block per section it manages (`grants`, `tagOwners`,
  `nodeAttrs`). It never parses-and-reserializes the human's file: their
  comments, ordering, and formatting survive byte-for-byte.
- **The prefix = safety invariant.** Generator hard rule: nothing inside a
  fence may reference a name outside `tag:tailarr*`, with exactly two
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
    // >>> TAILARR MANAGED
    "tag:tailarr":              ["autogroup:admin"],
    "tag:tailarr-ctrl":         ["autogroup:admin", "tag:tailarr-ctrl"], // self-own: OAuth client must assign its own tag
    "tag:tailarr-user":         ["autogroup:admin"],
    "tag:tailarr-public":       ["autogroup:admin"],
    "tag:tailarr-svc-sonarr":   ["autogroup:admin"],   // + per installed
    "tag:tailarr-can-sonarr":   ["autogroup:admin"],   //   service pair
    // <<< TAILARR MANAGED
},
"nodeAttrs": [
    // >>> TAILARR MANAGED
    {"target": ["tag:tailarr-public"], "attr": ["funnel"]},
    // <<< TAILARR MANAGED
],
```

(When the OAuth client exists, its identity is added to the managed
tagOwners values so Tailarr may assign the tags it defines.)

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
  every `tag:tailarr-user` device so unexpected enrollments are visible.

## 6. Funnel tie-in

**Two hard-won operational facts (verified live 2026-07-06):**
1. **Funnel ingress traffic is NOT exempt from the packet filter** under
   default-deny (open bug tailscale/tailscale#18181). The managed grants
   block therefore always contains
   `{"src": ["fd7a:115c:a1e0:ab12::/64"], "dst": ["tag:tailarr-public"], "ip": ["*"]}`
   — Tailscale's funnel ingress range → public-tagged pods. Without it the
   node logs `Drop: ... no rules matched` and public requests reset.
2. **Funnel needs the node's tailnet IPv6, and IPv6 refuses to run on links
   with MTU < 1280.** The old sidecar `TS_DEBUG_MTU=1200` silently broke
   Funnel (public requests hang); sidecars run 1280 as of v0.3.4. Sidecars
   created before that need one restart to become publicly reachable.

The shipped Make-public button (v0.3.0) flips `AllowFunnel` in the pod's
serve config, but tailscaled refuses without the **`funnel` nodeAttr** on the
node. The managed nodeAttrs block targets `tag:tailarr-public`; making a pod
public becomes: add `tag:tailarr-public` to the sidecar (tags API) + flip
AllowFunnel (already shipped). Fully tag-based — no hardcoded IPs — and the
same device-tags call as sharing. Until the automation exists, the manual
bridge is adding the sidecar's tailnet IP to a funnel nodeAttr by hand.

## 7. The baseline policy (clean slate — DECIDED 2026-07-05)

All old projects on the tailnet are declared dead, **tsidp is
decommissioned**, and the legacy policy (wildcard grant, tsidp cap, ssh
block, ~20 unused tags) is disposable. The complete replacement:

```jsonc
{
    // ===== human-owned: operator sovereignty, Tailarr never edits =====
    "grants": [
        {"src": ["tag:tailarr-admin", "autogroup:admin"], "dst": ["*"], "ip": ["*"]},

        // >>> TAILARR MANAGED — do not edit; regenerated by tailarr
        {"src": ["tag:tailarr"], "dst": ["tag:tailarr"], "ip": ["*"]},
        // (one can-X → svc-X line per installed service appears here)
        // <<< TAILARR MANAGED
    ],

    "tagOwners": {
        "tag:tailarr-admin": ["autogroup:admin"],
        // >>> TAILARR MANAGED
        "tag:tailarr":        ["autogroup:admin"],
        "tag:tailarr-ctrl":   ["autogroup:admin", "tag:tailarr-ctrl"],
        "tag:tailarr-user":   ["autogroup:admin"],
        "tag:tailarr-public": ["autogroup:admin"],
        // (+ svc-/can- pair per installed service)
        // <<< TAILARR MANAGED
    },

    "nodeAttrs": [
        // >>> TAILARR MANAGED
        {"target": ["tag:tailarr-public"], "attr": ["funnel"]},
        // <<< TAILARR MANAGED
    ],
}
```

### Migration notes (Stephen's tailnet)

- **Sequencing warning:** the moment this policy saves, default-deny is live.
  The operator's own access survives via `autogroup:admin`, but the fleet's
  pod→pod traffic breaks until the sidecars are re-tagged with
  `tag:tailarr` — current sidecars are **user-owned and untagged** (enrolled
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
   Users page, `tag:tailarr-public` from the Make-public button.
4. Fenced-grant generator + ETag read-modify-write apply path.
5. Only then: hand out the first `tag:tailarr-user` key.

## 9. Addendum (2026-07-19): the controller as a grantable service

§2's hard rule — "the controller gets NO `can-` tag ever" — assumed the
only controller client is the operator's browser, and that a network grant
to a no-auth web UI equals full admin of the fleet. The Tailarr app broke
the first assumption: its server module makes the controller the hub every
app user talks to, and "admin device or nothing" left no room between.

The rule is lifted **as a pair** — the tag opens the pipe, a credential
authorizes it:

- **`tag:tailarr-can-server`** (pseudo-service `server` on the Users page)
  grants `tag:tailarr-ctrl:443` like any other capability badge. Same
  flip-a-tag share/revoke, same fenced grant, defined unconditionally in
  the base tagOwners set (the controller always exists).
- **API bearer tokens** (`Pods/.tokens.json`, sha256-only, minted under
  Settings → API access) are the actual permission boundary. With
  `require` on, every `/api/*` request needs `Authorization: Bearer …` —
  401 otherwise. Exempt: `/api/info` (self-upgrade health gate through the
  sidecar netns + the app's pre-auth compatibility probe) and `/metrics`
  (outside `/api/`). Guardrails: `require` cannot be enabled with zero
  tokens, and deleting the last token auto-relaxes it — no lockout state.
- The intended grant flow is one gesture: badge the device with `server`
  AND hand its user a minted token. The Users-page confirm dialog states
  plainly that without required tokens the badge alone is full control.

Still true from the original design: `tag:tailarr-ctrl` itself is never
placed on a consumer device, and the token layer has no roles yet — a
token is all-or-nothing. Scoped/read-only tokens are the obvious next cut
if the app grows a family-facing dashboard.

## 10. Addendum (2026-07-20): the peer-relay grant (apple/container)

apple/container guests sit behind a NAT'd vmnet subnet Tailscale cannot
hole-punch, so every sidecar connection falls back to DERP. Tailscale
peer relays (GA 2026-02, clients 1.86+) fix the throughput: the Mac
hosting the guest runs `tailscale set --relay-server-port=40000`
(install-mac.sh does it), and a fenced grant authorizes devices to use
it:

    {"src": ["tag:tailarr", "tag:tailarr-user", "autogroup:member"],
     "dst": ["tag:tailarr-relay", "autogroup:admin"],
     "app": {"tailscale.com/cap/relay": []}}

Design points, in the spirit of the rest of this doc:

- **The cap grants relaying only — never network access.** The packet
  filter still decides who reaches what; this only lets connections that
  already pass it detour through a better relay than DERP.
- **BOTH ends of a connection need the cap**, hence `autogroup:member`
  in src (the admin's untagged phone/laptop) alongside the fleet tags.
- **dst (who may ACT as a relay) never tags the personal Mac.**
  Tagging a personal device strips its user identity and disables key
  expiry — wrong to do silently. `autogroup:admin` matches the Mac as
  the admin's device; `tag:tailarr-relay` (fenced tagOwners, ctrl
  co-owned) exists for a future dedicated relay box. Only devices
  actually running a relay server advertise, so the wide dst is inert.
- **Auto-emission is pre-flight-gated** (ts_relay_preflight): policy
  adopted by Tailarr + sane ACL size + small foreign-device/user counts
  — i.e. the tailnet matches the dedicated-tailnet product model. Any
  doubt ⇒ nothing is emitted; Settings → Peer relay shows the reasons
  with an explicit enable button (`Pods/.relay.json` records who
  decided).
- **A relay problem may never wedge ts_policy_sync**: if /validate
  rejects a splice that carries the grant AND a relay-free probe
  validates, the grant is the culprit — downgrade one rung per retry
  (`autogroup:admin` dst → `autogroup:member` → grant disabled,
  recorded as `auto-validate-reject`).
- The controller verifies from its own sidecar (`tailscale status
  --json` → peer-relay / direct / derp) in the maintenance loop; the
  Settings banner clears on peer-relay *or* direct.

## 11. Addendum (2026-07-21): relay generalization — registry + per-pod (v0.15.0)

v0.13.0's grant was apple/container-only and tailnet-wide. v0.15.0 makes
peer relay a first-class Networking feature on every platform:

- **The registry** (`Pods/.relay.json` → `relays`): devices Tailarr has
  registered as relays, keyed by tailnet IP. Tailscale has no "list
  relay-capable nodes" API and no remote enablement
  (tailscale/tailscale#17791) — capability is only *proven* when
  `relay_verify()` sees traffic through a device's IP in `PeerRelay`.
  Entries start `pending` and graduate to `active` on proof; relays seen
  carrying traffic that were never registered are auto-discovered in.
- **Grant shapes** (all inside the same fenced grants section, same
  prefix invariant — IP dsts carry no tags at all):
  - *Global, no selection* → the v0.13.0 autogroup-ladder grant,
    verbatim. Upgraded installs change nothing.
  - *Global, relay selected* → dst becomes `"<ip>/32"`; the
    `tag:tailarr-relay` owner line is dropped (nothing references it).
  - *Per-pod* → one cap grant per selection: src
    `["tag:tailarr-svc-<name>", "tag:tailarr-user", "autogroup:member"]`
    (`"server"` selects the controller: `tag:tailarr-ctrl`), dst that
    pod's chosen relay IP. Pods without a selection get no grant.
- **The downgrade ladder narrows**: the admin→member rung only applies
  to the autogroup dst; a rejected specific-IP grant goes straight to
  the disable rung (still confirmed by the relay-free probe splice).
- **No host special case (v0.15.2)**: v0.15.0/0.15.1 briefly offered a
  one-click "enable the machine hosting Tailarr" via host-exec. Removed:
  the machine host-exec reaches is the INNER host (the podman host —
  the guest VM on apple/container, the VM on nested linux), which shares
  the sidecars' NAT position — nominating it as a relay is useless
  exactly when a relay is needed (live-caught 2026-07-21). Capability is
  therefore always device-local: every added relay gets the
  `tailscale set --relay-server-port=<port>` command to run and stays
  pending until `relay_verify()` sees traffic through it. The machine
  worth nominating is the OUTERMOST host (the Mac — install-mac.sh) or
  any well-connected tailnet device.

## 12. Addendum (2026-07-22): netmap minimality — visibility fencing

Motivated by the iOS app's Tailscale Status screen, which renders a
device's netmap and thereby turns *visibility* into product surface (and
into an audit tool). The invariant, stronger than "users can't connect
to ungranted services":

> A device wearing `tag:tailarr-user` must receive a netmap containing
> only what its `can-*` badges grant. Tailscale prunes a peer only when
> the policy allows ZERO traffic between the pair — any rule matching
> them on any port, in either direction, exposes the peer's name and
> tailnet IPs. So there must be **no rule at all** connecting a user
> device to anything outside its grants.

**Why the fences satisfy it** (each generated grant, audited 2026-07-22):

- `tag:tailarr → tag:tailarr ip:*` — user devices never wear
  `tag:tailarr` (`op_user_key` mints `tag:tailarr-user` only;
  `op_user_adopt` refuses fleet-tagged devices). Pods see each other and
  the controller; users see none of it.
- `can-<svc> → svc-<svc>:443` and `can-server → ctrl:443` — the ONLY
  network grants whose src a user device can wear. Single badge, single
  dst: a badge exposes exactly one pod (or the controller, which is
  otherwise invisible — a deliberate per-user opt-in). No user↔user
  rule exists, so enrolled devices never see each other.
- Funnel ingress (`fd7a:…/64 → tag:tailarr-public`) — Tailscale's
  ingress range, not user devices.
- Peer-relay cap grants — cap-only (no `"ip"`), so no network access,
  but a cap grant is still a rule: **the relay dst is visible to every
  consumer** (§11 shapes; the legacy autogroup dst exposes every
  admin/member device, a registry-IP dst exposes one relay). Deliberate,
  documented at `_RELAY_CONSUMERS`; enabling relay is the one opt-in
  that widens a scoped user's peer list.
- Revocation is tag-flip revocation: removing a `can-*` badge unmatches
  its grant and the pod leaves the device's netmap on the next map push.
  Removing a service drops its grant + owner lines at the next sync.

**Outside the fences** the operator-sovereignty rule matters just as
much: with `dst: ["*"]` it pairs every admin-owned device with every
user device, making the admin's machines visible in every user netmap
(reverse-direction rules count — live-confirmed 2026-07-22: a scoped
device's netmap showed the admin's Mac). The README baseline therefore
narrows it to `autogroup:admin → tag:tailarr` plus a separate
`autogroup:member → autogroup:self` rule so the admin's own untagged
devices still reach each other (tagged devices have no user owner, so
`autogroup:self` never matches user enrollments or pods). The self rule
also supplies the network grant Tailscale SSH's member→self rule needs.
Anything else broad outside the fences (a leftover allow-all, ping
rules touching `tag:tailarr-user`) breaks minimality — Tailarr cannot
rewrite the human's policy, so this stays a documented audit item,
checkable from the app's Status screen.

**Enforcement**: `_grants_minimality_ok()` runs before every splice
(including downgrade-ladder retries) and fails the sync closed if a
generated grant puts a user-wearable selector (`tag:tailarr-user`,
`tag:tailarr-can-*`) anywhere but the src of a cap-only grant or the
sole src of a sole-dst badge switch, or in any dst. Future rules that
would widen user visibility must extend that function *and* this
section, consciously.

**Live audit recipe** (doubles as the E2E check): enroll a device with a
minted user key, grant it exactly one service, and its peer list —
Status screen or `tailscale status --json` — must show exactly that
pod, plus the relay device iff relay is enabled, and nothing else
(under the narrowed README baseline; a wide `admin → *` sovereignty
rule additionally surfaces every admin device). Toggle the grant off
and the pod must disappear.
