# Tailarr Sovereign Mode — embedded headscale control plane

**Status: DESIGN ONLY (2026-07-19, from a design discussion with Stephen).
Nothing here is implemented.** Executing sessions: do NOT install headscale,
change the bootstrap, or restructure the tsapi layer from this document
alone.

**Design principles (locked):**
1. **Never mandatory.** Tailscale-hosted mode stays the default and keeps
   full parity (Funnel, ts.net HTTPS, Tailscale DERP). Sovereign mode is an
   opt-in for people who want zero third-party dependency and accept its
   trade-offs.
2. **Hidden means embedded, not secret.** When enabled, headscale is a
   subsystem of the controller — lifecycle tied to the controller, state in
   the controller's data dir, never listed in the pods API, no separate
   config surface. It is not a catalog service, because a generic service
   gives none of the integration below.
3. **One code path.** The controller talks to *a control plane* through a
   driver interface; the app talks to the same Tailarr users API in both
   modes. No `if headscale` scattered through handlers or screens.
4. Mode choice is effectively **install-time**. Switching modes re-enrolls
   every node and pod — a migration event with tooling, never a toggle.

---

## 1. Why bother

Everything Tailarr does against api.tailscale.com today rides a credential
the user must mint and maintain (tsapi token/OAuth wizard; the mobile app
gates Add User on OAuth mode). With an embedded headscale the control plane
is a localhost call the controller fully owns:

- **Users become first-class.** Mint/expire preauth keys, see enrollments
  the moment they happen, no OAuth client, no 90-day tokens, no gate
  screens. The tsapi wizard ceases to exist in this mode.
- **ACLs by construction.** The controller *generates the entire policy
  file* from its own service-access model and hands it to headscale
  atomically. The fenced-section discipline of [acl-design.md](acl-design.md)
  still applies in hosted mode; in sovereign mode the fence is the whole
  file and the per-service access switches in the app are the literal
  source of truth.
- **The perfect invite.** One link: control URL + preauth key + module
  config (the app's share-configuration payload already reserves room).
  The recipient taps, the app's embedded tsnet enrolls against the user's
  headscale — control URL passed programmatically, invisible to them — and
  services appear. No Tailscale account, no third-party signup.

## 2. Architecture

### 2.1 Control-plane driver interface

```
mint_key(tags, expiry, single_use) -> key
list_nodes()                       -> [{id, hostname, ip, tags, last_seen}]
expire_node(id) / delete_node(id)
set_tags(id, tags)
set_policy(policy_json)
capabilities()                     -> {funnel: bool, ts_cert: bool, ...}
```

Two drivers:
- `tailscale`: the existing tsapi layer (token/OAuth against
  api.tailscale.com), `capabilities() = {funnel: true, ts_cert: true}`.
- `sovereign`: localhost headscale REST/gRPC, operated by the same
  controller, `capabilities() = {funnel: false, ts_cert: false}`.

`/api/info` grows `"control_plane": "tailscale" | "sovereign"` (and/or the
capabilities dict) so the app can hide Funnel and skip the tsapi gate in
sovereign mode without new endpoints.

### 2.2 The headscale subsystem

- Runs inside (or alongside) the controller container, supervised by the
  controller; config + SQLite state under the controller's data dir, backed
  up with it.
- Not present in `/api/pods`, the fleet actions, or the web UI pods list.
  Surface area: a Settings section (mode, public endpoint, cert status,
  DERP status) only.
- Sidecar wiring: generated `run.sh` gains a login-server parameter
  (`TS_EXTRA_ARGS=--login-server=...` / `login_server` in config) rendered
  from the mode. Pods enroll against headscale exactly as they do against
  Tailscale today — preauth key in a file, state on a bind mount
  (`TS_AUTH_ONCE=true` semantics unchanged).

### 2.3 Public endpoint (the entry fee)

Headscale must be reachable by every client from anywhere:

- Requirements: **a domain or DDNS hostname pointing at the user's public
  IP + one forwarded port (443)**. That is the whole entry fee, and it is
  explainable.
- Certificates: once 443 is open, **Let's Encrypt HTTP-01 works with no
  DNS API credentials**. The controller automates issuance + renewal for
  the control-plane hostname itself.
- **CGNAT (T-Mobile Home Internet, Starlink, most LTE) cannot open 443.**
  Detect (STUN / probe-back) and refuse enablement with a clear message
  rather than failing mysteriously. A tunnel-fronted variant (Cloudflare
  Tunnel or similar) is possible later but adds back a third party —
  contrary to the point of the mode.

### 2.4 DERP / relaying

Embed headscale's DERP server on the same public endpoint (TCP 443 mux +
UDP 3478 STUN). Consequences to document honestly in the UI:
- Relayed peer-to-peer traffic hairpins through the user's home upload.
- NAT-traversal quality (fallback latency, success rate) is below
  Tailscale's anycast fleet.
Pointing at Tailscale's public DERP map technically works today but is
freeloading on infrastructure the user isn't paying for — not something to
build on.

### 2.5 In-tailnet HTTPS for pods (the open sore)

`tailscale serve` + `TS_CERT_DOMAIN` magic exists only because Tailscale
owns ts.net. Sovereign v1 answer: **pods serve plain HTTP inside the
tailnet** — defensible (WireGuard already encrypts and authenticates every
peer), and the app treats sovereign-mode hosts as trusted transport (its
non-ts.net host warning must learn the mode). Later opt-in for the padlock
crowd: wildcard cert under the user's domain via DNS-01, which requires DNS
API credentials and stays optional. A private CA is rejected: installing
trust roots on iOS is user-hostile.

## 3. What sovereign mode gives up (state plainly in UI + docs)

- **Funnel** — public exposure of pods is Tailscale-infrastructure ingress.
- **ts.net HTTPS + MagicDNS under ts.net** — names come from the user's
  domain; pods are HTTP-in-tailnet by default (§2.5).
- **Tailscale's DERP fleet** — self-relaying (§2.4).
- **Admin console** — Tailarr's own UI must cover node visibility fully,
  because there is no console behind it. (Headscale third-party UIs exist;
  not our surface.)
- Node sharing between tailnets, Mullvad exit nodes, tailnet lock, and
  similar hosted-only features.
- **Control-plane custody.** The headscale endpoint is the most
  security-critical thing the user runs: whoever owns it owns the tailnet.
  Controller-lockdown obligations from acl-design §0 apply doubly. Uptime
  matters: nodes cannot (re)join while it is down.
- Official Tailscale clients CAN join (custom coordination server), but the
  flow is buried on iOS — sovereign invites should steer recipients to the
  Tailarr app, where the control URL is programmatic and invisible.

## 4. Open questions (decide before implementation)

1. Headscale in-process in the controller container vs. a hidden sibling
   container. (Sibling is cleaner for upgrades; "hidden" then means
   filtered from every pods surface, including diagnose.)
2. OIDC: expose headscale's OIDC hooks for browser-based enrollment of
   plain Tailscale clients, or keep preauth-keys-only (matches the invite
   flow; less surface)?
3. Migration tooling scope: assisted re-enroll of pods is scriptable
   (regenerate keys, restart sidecars); user devices are inherently manual.
4. Does the app's `tailscale_embed` plugin need API surface for a custom
   control URL end-to-end (tsnet supports it; plumb through TailscaleConfig)?
5. Backup story: headscale SQLite must join the controller backup set with
   crash-consistent snapshots.

## 5. Relationship to existing work

- The mobile app's share-configuration payload (`{v, module, ...}`) is
  versioned; the invite adds `enroll: {control_url, key}` without breaking
  v1 parsers.
- The Add User OAuth gate (app, 2026-07-19) is correct for hosted mode and
  simply never fires in sovereign mode (`control_plane` says so).
- `TS_AUTH_ONCE` + socket-liveness bootstrap fixes (2026-07-19) apply to
  both modes unchanged.
