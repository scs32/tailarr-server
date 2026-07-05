# Podscale ACL design — tag management + policy authoring

**Status: DESIGN ONLY (2026-07-05). Nothing here is implemented, and nothing
here has been applied to the live tailnet.** This document consolidates the
architecture decided during the 2026-07-05 backlog session into an executable
spec for a future build. Executing sessions: do NOT call the Tailscale API,
modify the live policy file, or create OAuth clients from this document alone.

---

## 0. Security prerequisite — controller lockdown (NON-NEGOTIABLE, FIRST)

Podscale's standing assumption is *tailnet-only = trusted*: the controller has
no auth of its own, holds the podman socket, and is root-equivalent over the
whole fleet. That assumption **breaks the moment the first family auth key is
handed out** — every family device gets line-of-sight to the controller (and
to Sonarr, and to everything else).

The live policy today makes this worse, not better:

- `grants: [{src: *, dst: *, ip: *}]` — default-ALLOW-all, flat trust.
- A wide-open `tailscale.com/cap/tsidp` cap on that same grant
  (`allow_admin_ui` + `allow_dcr`, users/resources `*`).

So the **first** policy change — before any tag automation, before any family
key exists — is:

1. Replace the wildcard grant with a **default-deny skeleton** (§4).
2. Lock the Podscale controller node to `tag:podscale_admin` sources only.
3. Scope the tsidp cap to admin.

Everything else in this document is gated behind that change. The stakes rise
further once Podscale holds an OAuth client that can write the policy file
(§3): a compromised controller could then rewrite the entire tailnet ACL.
Mitigations in §5, but the primary control is that nothing untrusted can
reach the controller in the first place.

## 1. Division of labor

- **Podscale = the authoring layer.** It compiles intent ("family can reach
  media apps") into Tailscale grants and tag assignments.
- **Tailscale = the enforcement layer.** The WireGuard packet filter enforces
  the compiled policy at the network level.
- **Podscale is NOT in the data path and must never become one.** Making
  Podscale the enforcement point would mean becoming a reverse proxy, which
  destroys the zero-published-ports / no-proxy architecture. Do not go there.

For the standard catalog (Plex, Sonarr, …) the enforcement primitive is
network reachability: *can this device reach that sidecar at all*. True
per-user, header-based authz (`Tailscale-App-Capabilities` + WhoIs,
`--accept-app-caps`, stable v1.92+) only works for capability-aware apps —
a later layer, not the foundation.

## 2. Tag model

| Tag | On | Meaning |
|---|---|---|
| `tag:podscale_admin` | Stephen's devices | may reach the controller + everything |
| `tag:app-<svc>` | each pod's sidecar | per-service identity (fine-grained tier) |
| `tag:tier-media` (etc.) | sidecars, grouped | access tier — the churn escape hatch |
| `tag:family` / `tag:family-<person>` | family devices | who may reach which tier |

Key facts that shaped this:

- **Grants match tags EXACTLY** — no globs (`tag:app-*` is invalid), no
  relational matching. Per-service isolation therefore means one grant line
  per service, i.e. the grants block changes every time a service is added
  or removed. That is automatable (§3) but churny.
- **Tiers are the escape hatch:** `tag:tier-media` covering
  plex/jellyfin/sonarr/radarr means adding a service = one device-tag call
  into an existing tier, ZERO grant edits. Recommended default granularity;
  per-service `tag:app-<svc>` grants only where a service genuinely needs
  its own audience.
- **Family devices are tagged devices, not users.** Auth-key-enrolled devices
  have no Tailscale user identity in ACL terms (they're owned by their tags).
  Per-person granularity = per-person tag buckets (`tag:family-grandma`).
  Real per-user identity requires Tailscale user sharing/invites (a TS login
  per person) — build the tag model first.
- **Tagging is one-way:** tagging a device drops its user owner, and clearing
  tags does not restore one. Acceptable for this model (family devices live
  as tagged devices) — but document it wherever a tag is applied.

## 3. Mechanics

### Device tagging (membership)
- `POST /api/v2/device/{id}/tags` — **REPLACES the whole tag set**, so every
  write is read-modify-write. Serialize through the controller's existing
  busy registry to avoid lost updates.
- `tagOwners` must pre-authorize Podscale's OAuth identity for every tag it
  will assign (one-time entries in the human-owned section of the policy).

### Policy file (grants)
- `GET/POST /api/v2/tailnet/{tailnet}/acl` — HuJSON, **whole-file replace**
  (no patch op), with `ETag` / `If-Match` optimistic concurrency and an
  `/acl/validate` dry-run endpoint.
- Podscale edits ONLY inside fenced markers:

  ```
  // >>> podscale-managed grants — do not edit by hand
  ...generated one-grant-per-service / per-tier lines...
  // <<< podscale-managed grants
  ```

  The POST body is literal HuJSON, so comments round-trip and the
  human-owned sections survive regeneration.
- Write cycle: `GET` (capture ETag) → regenerate the fenced block from the
  deployed-pod list → `POST /acl/validate` → `POST` with `If-Match` → on
  `412` refetch and retry. Serialize via the busy registry.
- Add-service flow: create sidecar → tag it (`tag:app-<svc>` or into a tier)
  via the device API → regenerate fenced grants → validate → POST. Reverse
  on remove. ACLs recompute tailnet-wide in seconds.

### Credentials
- An **OAuth client** (auto-refreshing) scoped to exactly `devices:write` +
  `acl:write`, stored in the controller. This is what makes §0 non-negotiable.

## 4. Initial policy skeleton (drafted 2026-07-05, NOT applied)

Shape only — exact HuJSON to be written at execution time against the then-
current policy:

- **default-deny** (remove `{src:*, dst:*, ip:*}`)
- `tag:podscale_admin` → `*` (admin reaches everything)
- controller node reachable from `tag:podscale_admin` ONLY
- one worked tier example: `tag:family` → `tag:tier-media` on 443
- tsidp cap scoped to admin (drop `allow_dcr`/`*` users+resources)
- `tagOwners` for every tag in §2, including authorizing Podscale's OAuth
  identity for the tags it manages
- the empty fenced `podscale-managed grants` region, ready for takeover
- **funnel nodeAttr** (§6) granted to the funnel-capable tag

Existing `tagOwners` in the live policy already provisions many service tags
(plex/jellyfin/sonarr/… → autogroup:admin) — today they have ZERO access
effect (labels only, because the grant is `*→*`). The skeleton makes them
meaningful; reconcile/rename toward the §2 scheme rather than keeping two
naming systems.

## 5. Safety rails (all mandatory in the implementation)

- `POST /acl/validate` before every apply; refuse to POST on validation error.
- Keep the last-known-good policy as a local backup before each write;
  provide a one-call rollback.
- Never generate outside the fence; refuse to apply if the fence markers are
  missing or ambiguous (fail closed — tell the human).
- `If-Match`/ETag on every POST; on 412 refetch + regenerate + retry, never
  blind-overwrite.
- OAuth client scoped minimally (§3); rotate if the controller is rebuilt.

## 6. Funnel tie-in

The shipped Funnel toggle (Network tab, `POST /api/network/<n>`) flips
`AllowFunnel` in the pod's serve config — but tailscaled refuses unless the
node carries the **`funnel` nodeAttr**. In the live policy only `tag:funnel`
and two hardcoded IPs have it, so freshly-enrolled sidecars can't actually
serve publicly yet (the toggle surfaces the refusal in its output).

The skeleton must therefore attach the funnel nodeAttr to a tag Podscale can
assign (e.g. `tag:funnel` on the sidecar, added/removed by the same device-
tagging flow when a pod is made public/private). That closes the loop: the
toggle becomes fully self-service.

## 7. Build order (when this is executed)

1. §0 skeleton by hand in the admin console (human-authored, one time),
   including the empty fenced region + tagOwners.
2. OAuth client creation + storage in the controller.
3. Device-tagging flow (tier membership on install/remove; funnel tag on
   toggle).
4. Fenced-grant generator + ETag read-modify-write apply path.
5. Only then: hand out the first family auth key.
