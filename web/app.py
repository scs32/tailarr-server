#!/usr/bin/env python3
"""Tailarr web controller — JSON API backing the React SPA.

Architecture:

  * op_*()      -- pure logic. Take plain data, talk to the create.sh engine
                   and podman, and return structured result dicts. No HTML.
  * JSON API    -- /api/* endpoints. Thin adapters that (de)serialize JSON
                   around the op_* functions. This is what the SPA consumes.
  * Static SPA  -- the built React app under STATIC_DIR is served at the web
                   root, with an index.html fallback for client-side routing.
                   Baked into the image by the multi-stage Containerfile.

Still stdlib-only and no-auth (reachable only over the tailnet by design).

Expects (provided by the container image / bootstrap script):
  - engine scripts + homelab.js in APP_DIR
  - the built SPA in STATIC_DIR (default APP_DIR/static)
  - host ~/Pods mounted at PODS_DIR (same path as on the host!)
  - host podman socket mounted, CONTAINER_HOST pointing at it
"""

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ntfy_client  # local module beside app.py; stdlib-only

VERSION = "0.29.0"

APP_DIR = os.environ.get("APP_DIR", "/app")
PODS_DIR = os.environ.get("PODS_DIR", "/root/Pods")
STATIC_DIR = os.environ.get("STATIC_DIR", os.path.join(APP_DIR, "static"))
PORT = int(os.environ.get("PORT", "8080"))

CONTROLLER_PODS = {"tailarr", "podscale", "homepod"}  # older names = pre-rename deploys

# System pods: infrastructure services the controller manages on the
# operator's behalf (ntfy). Hidden from sharing entirely — no can- badge,
# no grant lines, no Users-page row — so they never appear in any consumer
# device's netmap (the §12 minimality invariant, docs/acl-design.md).
# Unlike CONTROLLER_PODS they still wear their tag:tailarr-svc-* identity
# tag and MAY be funneled (ntfy's user delivery path). Matched on image
# substring, like _discover_kuma — the flag must survive engine re-renders
# and hand-rolled installs, and .config.json carries no custom fields.
SYSTEM_IMAGES = ("binwiederhier/ntfy",)

# The self-config gateway (acl-design §12 addendum): the ONE node user
# devices may reach besides their granted pods, so the Tailarr app can
# fetch its own notification config with zero setup. Runs the
# controller's OWN image with the selfconfig.py entrypoint;
# auto-deployed at ntfy setup; matched by NAME (its image is the
# controller image, which must never itself read as a system pod).
# Holds no secrets — it asks the controller via /api/gateway/resolve,
# authenticated by a per-install shared secret in .gateway.json (0600).
GATEWAY_POD = "tailarr-gate"
GATEWAY_PORT = "80"
GATEWAY_FILE = os.path.join(PODS_DIR, ".gateway.json")

# Function-first display names for the infrastructure pods, used where
# they legitimately appear to the admin (resource stats) — never leak
# the implementation name. Everywhere else they're hidden outright.
POD_DISPLAY_NAMES = {"tailarr-gate": "Tailarr app setup"}


def _display_name(name):
    if any(s in (pod_config(name) or {}).get("image", "")
           for s in ("binwiederhier/ntfy",)):
        return "Notifications"
    return POD_DISPLAY_NAMES.get(name, name)

# Host platform fact: written by the bootstrap (apple/container guests run
# vminitd as PID 1 — that single fact drives the peer-relay offer and skips
# host helpers that need systemd). Backfilled by the controller on installs
# bootstrapped before this file existed. Peer-relay state lives beside it.
HOST_FILE = os.path.join(PODS_DIR, ".host.json")
RELAY_FILE = os.path.join(PODS_DIR, ".relay.json")
RELAY_PREFLIGHT_TTL = 24 * 3600
# "Does this look like a dedicated Tailarr tailnet?" thresholds — the relay
# grant is only auto-emitted when the tailnet matches the product model (a
# 1:1 tailnet per install); anything bigger needs the explicit Settings
# opt-in. Foreign = devices without any tag:tailarr* tag.
RELAY_MAX_ACL_LINES = 200
RELAY_MAX_FOREIGN_DEVICES = 10
RELAY_MAX_FOREIGN_USERS = 2

# Shared media folders (the only thing allowed to pierce the pod barrier).
# Each share: {"host_path": "/data", "container_path": "/data", "ro": false}
# plus an optional "nfs": {"clients": "...", "ro": true} when the share is
# exported to machines OUTSIDE the pod world (e.g. a native Plex on the
# macOS machine hosting this VM) via the host kernel's NFS server.
SHARES_FILE = os.path.join(PODS_DIR, ".shares.json")
EXPORTS_FRAGMENT = os.path.join(PODS_DIR, ".exports")
EXPORTS_HOST_FILE = "/etc/exports.d/tailarr.exports"
NFS_HELPER = "tailarr-nfs"  # one-shot helper that applies exports on the host
MOUNTS_HELPER = "tailarr-mounts"  # one-shot helper for the systemd drop-in
UNIT_DROPIN_DIR = "/etc/systemd/system/tailarr-pods.service.d"
# One export-client token: IP, CIDR, hostname, or wildcard. No parens,
# quotes, or whitespace — those would change the /etc/exports syntax.
NFS_CLIENT_RE = re.compile(r"[A-Za-z0-9.\-*/:_]+")

# External catalog sources: a registry of URLs to extra catalogs
# (homelab.js JSON schema) whose services are merged into the catalog.
# Each source: {"url": "https://..."}. Trusted single-user, tailnet-only.
SOURCES_FILE = os.path.join(PODS_DIR, ".sources.json")
CATALOG_TTL = 60  # seconds to cache a fetched source catalog
_catalog_cache = {}  # url -> (expires_at, services, error)

# User-authored catalog entries (the "custom" source): the Add-custom-pod
# dialog on the Catalog page SAVES a definition here instead of installing
# directly — the entry then installs/reinstalls like any catalog service.
# Each entry: the same spec shape a catalog file carries (image, command,
# ports, environment, volumes).
CUSTOMPODS_FILE = os.path.join(PODS_DIR, ".custompods.json")

# Image update checks: remote digests (via skopeo) vs local RepoDigests,
# cached here. Refreshed daily (piggybacked on /api/pods) or on demand.
UPDATES_FILE = os.path.join(PODS_DIR, ".updates.json")
UPDATES_TTL = 24 * 3600

# Controller self-upgrade: released versions come from the repo's git tags
# (the release workflow builds ghcr images on tag push; there are no GitHub
# Releases). The check is best-effort and cached; every /api/info read is
# cache-only so the UI never blocks on GitHub.
RELEASE_REPO = "scs32/tailarr-server"
RELEASE_FILE = os.path.join(PODS_DIR, ".release.json")
UPGRADE_DIR = os.path.join(PODS_DIR, ".upgrade")
UPGRADE_HELPER = "tailarr-upgrade"  # detached container that swaps the controller

# Per-pod app-data snapshots: stop -> tar the pod dir -> start. The pod dir
# is the pod's entire mutable state (only media pierces the barrier), so one
# tar is an application-consistent backup — including the Tailscale identity,
# which an in-place restore deliberately brings back. Dot-prefixed names keep
# both invisible to deployed_services() (it keys on <dir>/run.sh).
BACKUPS_FILE = os.path.join(PODS_DIR, ".backups.json")
BACKUPS_DIR = os.path.join(PODS_DIR, ".backups")
BACKUP_KEEP_DAILY = 7   # newest N always kept
BACKUP_KEEP_WEEKLY = 4  # plus newest-per-ISO-week for N older weeks
BACKUP_TS_RE = re.compile(r"[0-9]{8}-[0-9]{6}")
_updates_lock = threading.Lock()
_updates_running = False

# In-flight pod actions (start/stop/update/reconfigure/remove). Actions run
# synchronously inside their request thread, so this registry is accurate;
# it lets every view (and a reloaded SPA) see that a pod is mid-transition,
# and lets us refuse a second, racing action on the same pod.
_pod_ops = {}  # name -> action
_pod_ops_lock = threading.Lock()


def _op_begin(name, action):
    """Claim a pod for an action. Returns the conflicting action, or None."""
    with _pod_ops_lock:
        current = _pod_ops.get(name)
        if current:
            return current
        _pod_ops[name] = action
        return None


def _op_end(name):
    with _pod_ops_lock:
        _pod_ops.pop(name, None)


def pod_busy(name):
    with _pod_ops_lock:
        return _pod_ops.get(name)

NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*")


# =========================================================================
# Data helpers (filesystem + podman + engine)
# =========================================================================
# Built-in category catalogs beyond the default media-empire homelab.js.
# Each is a homelab.js-schema file baked into the image; users opt in per
# category from the /sources panel. Enabled set persists like the other
# registries. The default catalog is always on and wins name collisions.
CATALOGS_DIR = os.path.join(APP_DIR, "catalogs")
CATALOGS_FILE = os.path.join(PODS_DIR, ".catalogs.json")
BUILTIN_CATALOGS = [
    {"key": "observability", "name": "Observability",
     "description": "Grafana + Prometheus — scrape the controller's /metrics"},
    {"key": "home-network", "name": "Home & network",
     "description": "Home Assistant, Pi-hole, UniFi, WireGuard, Portainer"},
    {"key": "apps", "name": "Apps",
     "description": "Nextcloud, Vaultwarden, BookStack, Gitea"},
]


def load_enabled_catalogs():
    try:
        with open(CATALOGS_FILE) as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def save_enabled_catalogs(keys):
    tmp = CATALOGS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sorted(keys), f)
    os.replace(tmp, CATALOGS_FILE)


def _catalog_file_services(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def load_services():
    services = {}
    # enabled categories first — the default catalog wins name collisions
    enabled = load_enabled_catalogs()
    for cat in BUILTIN_CATALOGS:
        if cat["key"] not in enabled:
            continue
        for s in _catalog_file_services(
                os.path.join(CATALOGS_DIR, cat["key"] + ".js")):
            s = dict(s)
            s["_source"] = cat["name"]
            services[s["name"]] = s
    with open(os.path.join(APP_DIR, "homelab.js")) as f:
        for s in json.load(f):
            services[s["name"]] = s
    return services


def load_sources():
    try:
        with open(SOURCES_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_sources(sources):
    tmp = SOURCES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sources, f, indent=2)
    os.replace(tmp, SOURCES_FILE)


def _valid_service(s):
    return (isinstance(s, dict) and isinstance(s.get("name"), str) and s["name"]
            and isinstance(s.get("image"), str) and s["image"])


def fetch_catalog(url, force=False):
    """Fetch + parse a remote catalog (homelab.js JSON schema).

    Cached per URL with a short TTL so /api/catalog and installs stay fast and
    a down source doesn't re-block every call. Returns (services, error).
    """
    now = time.time()
    if not force:
        cached = _catalog_cache.get(url)
        if cached and cached[0] > now:
            return cached[1], cached[2]
    services, error = [], None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tailarr"})
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read(2_000_000)  # cap at ~2 MB
        data = json.loads(raw.decode())
        if not isinstance(data, list):
            error = "catalog must be a JSON array of services"
        else:
            services = [s for s in data if _valid_service(s)]
            if not services:
                error = "no valid services (each needs a name and image)"
    except (urllib.error.URLError, ValueError, OSError) as e:
        error = f"fetch failed: {e}"
    _catalog_cache[url] = (now + CATALOG_TTL, services, error)
    return services, error


def load_custompods():
    try:
        with open(CUSTOMPODS_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_custompods(pods_):
    tmp = CUSTOMPODS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(pods_, f, indent=2)
    os.replace(tmp, CUSTOMPODS_FILE)


def all_services():
    """Merged catalog: built-in homelab.js + user custom pods + sources.

    Precedence on name collision: built-in, then the user's own custom
    entries, then external sources. Each spec is tagged with `_source`
    ("built-in", "custom", or the source name). Returns (dict, errors).
    """
    merged = {name: {**spec, "_source": spec.get("_source", "built-in")}
              for name, spec in load_services().items()}
    for name, spec in sorted(load_custompods().items()):
        if name not in merged:
            merged[name] = {**spec, "_source": "custom"}
    errors = {}
    for sname, s in sorted(load_sources().items()):
        services, err = fetch_catalog(s["url"])
        if err:
            errors[sname] = err
        for svc in services:
            if svc["name"] not in merged:
                merged[svc["name"]] = {**svc, "_source": sname}
    return merged, errors


def resolve_service(name):
    """Look up a catalog service by name across built-in + sources."""
    return all_services()[0].get(name)


def load_shares():
    try:
        with open(SHARES_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_shares(shares):
    tmp = SHARES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(shares, f, indent=2)
    os.replace(tmp, SHARES_FILE)


def share_volume(share):
    """Volume entry (container_path, host_path[:ro]) for a share."""
    host = share["host_path"] + (":ro" if share.get("ro") else "")
    return share["container_path"], host


def pod_config(name):
    try:
        with open(os.path.join(PODS_DIR, name, ".config.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def podman(*args, timeout=60):
    try:
        return subprocess.run(
            ["podman", *args], capture_output=True, text=True, timeout=timeout,
            env=registry_env(),  # private-registry pulls (None = inherit)
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(args, 1, "", f"podman unavailable: {e}")


def running_names():
    out = podman("ps", "--format", "{{.Names}}")
    return set(out.stdout.split()) if out.returncode == 0 else set()


def ps_all():
    """name -> (state, exit_code) for every container, running or not."""
    out = podman("ps", "-a", "--format", "json")
    if out.returncode != 0:
        return {}
    try:
        rows = json.loads(out.stdout or "[]")
    except ValueError:
        return {}
    info = {}
    for r in rows:
        for n in r.get("Names") or []:
            info[n] = (r.get("State") or "", r.get("ExitCode") or 0)
    return info


def pod_state(name, ps):
    """running / stopped / error for a deployed pod.

    error = the main container exists but last exited non-zero (a crash);
    a cleanly stopped or never-started pod is just stopped."""
    state, code = ps.get(name, ("", 0))
    if state == "running":
        return "running"
    if name in ps and code != 0:
        return "error"
    return "stopped"


def deployed_services():
    if not os.path.isdir(PODS_DIR):
        return []
    return sorted(
        d
        for d in os.listdir(PODS_DIR)
        if os.path.isfile(os.path.join(PODS_DIR, d, "run.sh"))
    )


def config_from_info(info):
    """Rebuild a create.sh input config from a pod's saved .config.json."""
    return {
        "container": info["service"],
        "image": info["image"],
        "network_mode": info.get("network_mode", "bridge"),
        "ports": info.get("ports", {}),
        "restart_policy": info.get("restart_policy", "unless-stopped"),
        "include_tailscale": info.get("include_tailscale", "no"),
        "include_https": info.get("include_https", "no"),
        "funnel": info.get("funnel", "no"),
        "auth_key_file": info.get("auth_key_file", ""),
        "base_path": info.get("base_path", PODS_DIR),
        "environment": info.get("environment", {}),
        "volumes": info.get("volumes", {}),
        "command": info.get("command", ""),
        "memory_limit": info.get("memory_limit", ""),
        "shares": info.get("shares", []),
        "config_file": info.get("config_file", ""),
        "config_set": info.get("config_set", {}),
    }


def run_create(config):
    """Render a pod through create.sh.

    The engine must never depend on an ambient CWD or a relative log path:
    a momentarily-invalid /tmp once killed deploys at the very first log
    `touch`. Run it from the pod's own directory (created here, and
    guaranteed to exist for the engine anyway) and pin absolute log paths
    via the environment."""
    svc_dir = os.path.join(config.get("base_path") or PODS_DIR,
                           config.get("container") or "")
    try:
        os.makedirs(svc_dir, exist_ok=True)
    except OSError:
        svc_dir = PODS_DIR  # always exists (host mount)
    env = {**os.environ,
           "LOG_FILE": os.path.join(svc_dir, ".deployment.log"),
           "ERROR_LOG_FILE": os.path.join(svc_dir, ".error.log")}
    return subprocess.run(
        ["bash", os.path.join(APP_DIR, "create.sh")],
        input=json.dumps(config),
        capture_output=True,
        text=True,
        cwd=svc_dir,
        env=env,
        timeout=300,
    )


# =========================================================================
# Image update checks
# =========================================================================
def load_updates():
    try:
        with open(UPDATES_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"checked": 0, "images": {}}
    except (OSError, ValueError):
        return {"checked": 0, "images": {}}


def save_updates(data):
    tmp = UPDATES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, UPDATES_FILE)


def _image_update_status(image):
    """Compare the registry's digest for an image tag against local pulls."""
    try:
        r = subprocess.run(
            ["skopeo", "inspect", "--format", "{{.Digest}}", f"docker://{image}"],
            capture_output=True, text=True, timeout=60,
            env=registry_env(),  # digest checks work for private images too
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return {"update": False, "error": f"skopeo unavailable: {e}"}
    if r.returncode != 0:
        return {"update": False, "error": (r.stderr or "inspect failed").strip()[-200:]}
    remote = r.stdout.strip()
    local = podman("image", "inspect", image, "--format",
                   "{{range .RepoDigests}}{{.}}\n{{end}}", timeout=30)
    if local.returncode != 0:
        # not pulled locally yet -> updating would pull it; don't nag
        return {"update": False, "error": None}
    digests = [d.strip() for d in local.stdout.splitlines() if d.strip()]
    return {"update": not any(d.endswith(remote) for d in digests), "error": None}


def _check_updates():
    global _updates_running
    try:
        images = {}
        for name in deployed_services():
            img = (pod_config(name) or {}).get("image", "")
            if img:
                images[img] = None
        prev = load_updates().get("images", {})
        results = {img: _image_update_status(img) for img in images}
        save_updates({"checked": time.time(), "images": results})
        # Notify on the False->True transition only — the flag then stays
        # set until the pod updates, and re-checks must not re-page.
        fresh = sorted(img for img, r in results.items()
                       if r.get("update")
                       and not (prev.get(img) or {}).get("update"))
        if fresh:
            notify_ops("Pod updates available",
                       "New image versions: " + ", ".join(fresh),
                       tags=["arrow_up"])
        prev_latest = load_release().get("latest", "")
        latest = _check_release()  # piggybacked controller-release check
        if latest and latest != prev_latest \
                and _ver_key(latest) > _ver_key(VERSION):
            notify_ops("Tailarr update available",
                       f"v{latest} is out (this controller runs "
                       f"v{VERSION}).", tags=["arrow_up"])
    finally:
        with _updates_lock:
            _updates_running = False


def maybe_check_updates(force=False):
    """Kick off a background update check if stale (daily) or forced."""
    global _updates_running
    with _updates_lock:
        if _updates_running:
            return "running"
        if not force and time.time() - load_updates().get("checked", 0) < UPDATES_TTL:
            return "fresh"
        _updates_running = True
    threading.Thread(target=_check_updates, daemon=True).start()
    return "started"


def mark_image_fresh(image):
    """After a successful pull+recreate, clear the update flag immediately."""
    data = load_updates()
    if image in data.get("images", {}):
        data["images"][image] = {"update": False, "error": None}
        save_updates(data)


# =========================================================================
# Controller self-upgrade
# =========================================================================
def _ver_key(v):
    """Sortable key for a vX.Y.Z-ish version string ("" sorts lowest)."""
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:3]) + (0,) * (3 - min(len(nums), 3))


def load_release():
    try:
        with open(RELEASE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _check_release():
    """Refresh the newest released version from the repo's git tags.

    Best-effort: failures (offline, GitHub rate limit) leave the cache
    untouched — the upgrade hint just doesn't appear."""
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{RELEASE_REPO}/tags?per_page=100",
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "tailarr-controller"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            tags = json.load(r)
        versions = [t["name"].lstrip("v") for t in tags
                    if isinstance(t, dict)
                    and re.fullmatch(r"v\d+\.\d+\.\d+", t.get("name") or "")]
        if not versions:
            return None
        latest = max(versions, key=_ver_key)
        data = {"checked": time.time(), "latest": latest}
        tmp = RELEASE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, RELEASE_FILE)
        return latest
    except Exception as e:  # noqa: BLE001 — never let a version check bite
        print(f"release check failed: {e}")
        return None


def _controller_name():
    """The live controller container's name (its sidecar must exist too:
    the replacement joins that sidecar's netns and health-checks through
    it). None when podman is unavailable or no controller is visible."""
    out = podman("ps", "--format", "{{.Names}}")
    if out.returncode != 0:
        return None
    names = set(out.stdout.split())
    for n in sorted(CONTROLLER_PODS):
        if n in names and f"tailscale-{n}" in names:
            return n
    return None


def _upgrade_last_result():
    try:
        with open(os.path.join(UPGRADE_DIR, "result.json")) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def upgrade_status():
    """Everything the Settings upgrade card needs, from caches + one ps."""
    latest = load_release().get("latest", "")
    return {
        "current": VERSION,
        "latest": latest,
        "available": bool(latest) and _ver_key(latest) > _ver_key(VERSION),
        "checked": load_release().get("checked", 0),
        "busy": UPGRADE_HELPER in running_names(),
        "last": _upgrade_last_result(),
    }


def _render_redeploy_script(name, old_image, new_image, socket):
    """The script the detached helper runs to swap the controller.

    The controller cannot replace itself (`podman rm -f <self>` kills the
    process issuing it), so this runs in a separate container. Ordering is
    load-bearing: the new image is already pulled by the explicit version
    tag BEFORE this script exists (GHCR's :latest manifest can lag a
    release), and the old controller is only removed after that. On a
    failed health check it rolls back to the old image. The sidecar is
    never touched — tailnet identity and HTTPS survive the swap."""
    pods_dir = PODS_DIR
    log = f"{UPGRADE_DIR}/upgrade.log"
    result = f"{UPGRADE_DIR}/result.json"
    run_flags = (f"--network container:tailscale-{name} "
                 f"-v {pods_dir}:{pods_dir} "
                 f"-v {socket}:{socket} "
                 f"-v /run/libpod:/run/libpod "
                 f"-e CONTAINER_HOST=unix://{socket} "
                 f"-e PODS_DIR={pods_dir} "
                 f"--restart unless-stopped")
    return f"""#!/bin/sh
# Rendered by the Tailarr controller (self-upgrade). Runs detached in the
# {UPGRADE_HELPER} container; the controller that wrote this dies mid-script.
exec >> "{log}" 2>&1

finish() {{
    printf '{{"ok": %s, "from": "%s", "to": "%s", "rolled_back": %s, "finished": "%s"}}\\n' \\
        "$1" "{old_image}" "{new_image}" "$2" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "{result}"
}}

healthy() {{
    i=0
    while [ "$i" -lt 30 ]; do
        if podman exec tailscale-{name} \\
                wget -q -O /dev/null -T 3 http://127.0.0.1:{PORT}/api/info 2>/dev/null; then
            return 0
        fi
        sleep 2
        i=$((i + 1))
    done
    return 1
}}

echo "=== upgrade $(date -u +%Y-%m-%dT%H:%M:%SZ): {old_image} -> {new_image} ==="
sleep 3  # let the controller flush its API response before it dies

podman rm -f {name}
podman run -d --name {name} {run_flags} {new_image}

if healthy; then
    # Write the outcome FIRST: the new controller is already answering, so
    # a poller reading result.json before this line would see the previous
    # upgrade's outcome (observed in the field as a cosmetic lag).
    echo "upgrade OK"
    finish true false
    # Refresh host-side boot artifacts shipped in the new image (the
    # controller image carries start-pods.sh; /host-root is the host's
    # /root). The systemd unit only ever points at this script, so
    # refreshing the script is enough — no daemon-reload needed.
    if [ -f /app/start-pods.sh ] && [ -d /host-root ]; then
        cp /app/start-pods.sh /host-root/start-pods.sh \\
            && chmod +x /host-root/start-pods.sh \\
            && echo "refreshed /root/start-pods.sh from the new image"
    fi
    exit 0
fi

echo "new controller failed its health check - ROLLING BACK to {old_image}"
podman rm -f {name}
podman run -d --name {name} {run_flags} {old_image}
finish false true
exit 1
"""


def op_controller_upgrade(data):
    """Swap the controller for a newer (or explicitly chosen) release.

    Pull the explicit version tag first; hand the actual swap to a
    detached helper container (this process dies with the old controller).
    Returns immediately with status "upgrading" — the UI polls /api/info
    until the version changes."""
    name = _controller_name()
    if not name:
        return {"ok": False, "action": "upgrade", "status": "error",
                "error": "No running controller (with its Tailscale sidecar) "
                         "is visible via podman — is the socket mounted?",
                "output": ""}
    if UPGRADE_HELPER in running_names():
        return {"ok": False, "action": "upgrade", "status": "busy",
                "error": "An upgrade is already in progress.", "output": ""}

    target = (data.get("version") or "").strip().lstrip("v")
    if not target:
        target = load_release().get("latest", "")
    if not target:
        return {"ok": False, "action": "upgrade", "status": "error",
                "error": "No release known yet — use 'Check for updates' "
                         "first, or pass an explicit version.", "output": ""}
    if not re.fullmatch(r"\d+\.\d+\.\d+", target):
        return {"ok": False, "action": "upgrade", "status": "error",
                "error": f"'{target}' is not a release version (X.Y.Z).",
                "output": ""}
    if _ver_key(target) == _ver_key(VERSION):
        return {"ok": False, "action": "upgrade", "status": "refused",
                "error": f"Already running v{VERSION}.", "output": ""}

    ins = podman("inspect", name, "--format", "{{.ImageName}}", timeout=30)
    old_image = ins.stdout.strip() if ins.returncode == 0 else ""
    if not old_image:
        old_image = f"ghcr.io/scs32/tailarr:v{VERSION}"
    # Keep the deployment's registry/repo (HOMEPOD_IMAGE overrides exist);
    # only the tag moves. rsplit on ":" is safe: tags can't contain "/".
    repo, _, tag = old_image.rpartition(":")
    if not repo or "/" in tag:
        repo = old_image
    new_image = f"{repo}:v{target}"

    # Pull by the explicit version tag BEFORE anything is removed (GHCR
    # manifest lag: :latest — and briefly a brand-new tag — can serve stale
    # right after a release; an explicit tag either exists or fails here,
    # with the controller still intact).
    pull = podman("pull", new_image, timeout=600)
    if pull.returncode != 0:
        return {"ok": False, "action": "upgrade", "status": "pull failed",
                "error": f"Could not pull {new_image} — is v{target} released?",
                "output": pull.stdout + pull.stderr}

    socket = "/run/podman/podman.sock"
    host = os.environ.get("CONTAINER_HOST", "")
    if host.startswith("unix://"):
        socket = host[len("unix://"):]

    os.makedirs(UPGRADE_DIR, exist_ok=True)
    script = os.path.join(UPGRADE_DIR, "redeploy.sh")
    with open(script, "w") as f:
        f.write(_render_redeploy_script(name, old_image, new_image, socket))
    os.chmod(script, 0o755)

    podman("rm", "-f", UPGRADE_HELPER, timeout=30)  # stale helper, if any
    run = podman(
        "run", "-d", "--rm", "--name", UPGRADE_HELPER,
        "-v", f"{socket}:{socket}",
        "-v", f"{PODS_DIR}:{PODS_DIR}",
        "-v", "/root:/host-root",
        "-e", f"CONTAINER_HOST=unix://{socket}",
        "--entrypoint", "sh",
        new_image, script, timeout=120,
    )
    if run.returncode != 0:
        return {"ok": False, "action": "upgrade", "status": "error",
                "error": "Could not launch the upgrade helper.",
                "output": run.stdout + run.stderr}
    return {"ok": True, "action": "upgrade", "status": "upgrading",
            "error": None, "from": old_image, "to": new_image,
            "output": f"Upgrade helper started: {old_image} -> {new_image}. "
                      "The controller restarts in a few seconds; this page "
                      "will reconnect."}


# =========================================================================
# Per-pod backups (stop -> tar pod dir -> start)
# =========================================================================
def load_backups():
    try:
        with open(BACKUPS_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_backups(data):
    tmp = BACKUPS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, BACKUPS_FILE)


def _local_digest(image):
    r = podman("image", "inspect", image, "--format",
               "{{range .RepoDigests}}{{.}}\n{{end}}", timeout=30)
    if r.returncode != 0:
        return ""
    digests = [d.strip() for d in r.stdout.splitlines() if d.strip()]
    return digests[0] if digests else ""


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _backup_path(name, ts):
    return os.path.join(BACKUPS_DIR, name, f"{ts}.tar")


def _trim_backups(name, entries):
    """Retention: keep the newest BACKUP_KEEP_DAILY, plus the newest snapshot
    per ISO week for BACKUP_KEEP_WEEKLY older weeks. Deletes trimmed tars."""
    entries.sort(key=lambda e: e["ts"], reverse=True)
    keep = list(entries[:BACKUP_KEEP_DAILY])
    weeks = []
    for e in entries[BACKUP_KEEP_DAILY:]:
        week = time.strftime(
            "%G-%V", time.strptime(e["ts"], "%Y%m%d-%H%M%S"))
        if week not in weeks:  # entries are newest-first: first hit per week wins
            if len(weeks) < BACKUP_KEEP_WEEKLY:
                weeks.append(week)
                keep.append(e)
    for e in entries:
        if e not in keep:
            try:
                os.remove(_backup_path(name, e["ts"]))
            except OSError:
                pass
    return keep


def status_backups(name):
    entries = load_backups().get(name, [])
    return sorted(entries, key=lambda e: e["ts"], reverse=True)


def op_backup(name, reason=""):
    """Application-consistent snapshot: stop -> tar the pod dir -> start.

    One busy claim covers the whole run so the card reads "backup" fleet-wide
    and racing lifecycle ops get a 409. The pod is ALWAYS restarted if it was
    running, even when the tar fails.
    """
    if name not in deployed_services():
        return {"ok": False, "name": name, "action": "backup", "status": "error",
                "error": "Unknown service.", "output": ""}
    if name in CONTROLLER_PODS:
        return {"ok": False, "name": name, "action": "backup", "status": "refused",
                "error": "The controller can't stop itself to snapshot its own "
                         "directory.", "output": ""}
    conflict = _op_begin(name, "backup")
    if conflict:
        return {"ok": False, "name": name, "action": "backup", "status": "busy",
                "error": f"{conflict} is already in progress for {name}.", "output": ""}
    ts = time.strftime("%Y%m%d-%H%M%S")
    tar_dir = os.path.join(BACKUPS_DIR, name)
    tmp_path = os.path.join(tar_dir, f".tmp-{ts}.tar")
    output = ""
    was_running = False
    try:
        running = running_names()
        was_running = name in running or f"tailscale-{name}" in running
        if was_running:
            r = _run_action(name, "stop")
            output += r["output"]
            if not r["ok"]:
                return {"ok": False, "name": name, "action": "backup",
                        "status": "stop failed", "error": "Couldn't stop the pod "
                        "for a consistent snapshot.", "output": output}
        os.makedirs(tar_dir, exist_ok=True)
        try:
            tar = subprocess.run(["tar", "-cf", tmp_path, "-C", PODS_DIR, name],
                                 capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            tar = subprocess.CompletedProcess([], 1, "", "tar timed out")
        output += tar.stdout + tar.stderr
        if tar.returncode != 0:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return {"ok": False, "name": name, "action": "backup",
                    "status": f"exit {tar.returncode}", "error": "tar failed",
                    "output": output}
        entry = {
            "ts": ts,
            "image": (pod_config(name) or {}).get("image", ""),
            "digest": _local_digest((pod_config(name) or {}).get("image", "")),
            "size": os.path.getsize(tmp_path),
            "sha256": _sha256_file(tmp_path),
            "reason": (reason or "").strip()[:200],
        }
        os.replace(tmp_path, _backup_path(name, ts))
        backups = load_backups()
        backups[name] = _trim_backups(name, backups.get(name, []) + [entry])
        save_backups(backups)
        return {"ok": True, "name": name, "action": "backup", "status": "ok",
                "error": None, "output": output, "backup": entry}
    finally:
        if was_running:
            _run_action(name, "start")
        _op_end(name)


def op_backup_restore(name, ts):
    """In-place restore: stop -> wipe the pod dir -> untar -> re-render -> start.

    The tar carries ./tailscale/ so the pod comes back with the SAME tailnet
    identity — that's the point of in-place restore. (Restore-as-clone would
    have to wipe tailscale/ and re-enroll; not offered here.)
    """
    if name not in deployed_services():
        return {"ok": False, "name": name, "action": "restore", "status": "error",
                "error": "Unknown service.", "output": ""}
    if name in CONTROLLER_PODS:
        return {"ok": False, "name": name, "action": "restore", "status": "refused",
                "error": "Not restoring the controller from itself.", "output": ""}
    entry = next((e for e in load_backups().get(name, [])
                  if e["ts"] == ts), None)
    if not entry or not BACKUP_TS_RE.fullmatch(ts or ""):
        return {"ok": False, "name": name, "action": "restore", "status": "error",
                "error": "Unknown backup.", "output": ""}
    tar_path = _backup_path(name, ts)
    if not os.path.isfile(tar_path):
        return {"ok": False, "name": name, "action": "restore", "status": "error",
                "error": "Backup file is missing.", "output": ""}
    if _sha256_file(tar_path) != entry.get("sha256"):
        return {"ok": False, "name": name, "action": "restore", "status": "error",
                "error": "Backup file is corrupt (checksum mismatch).", "output": ""}
    conflict = _op_begin(name, "restore")
    if conflict:
        return {"ok": False, "name": name, "action": "restore", "status": "busy",
                "error": f"{conflict} is already in progress for {name}.", "output": ""}
    output = ""
    try:
        r = _run_action(name, "stop")
        output += r["output"]
        svc_dir = os.path.join(PODS_DIR, name)
        shutil.rmtree(svc_dir, ignore_errors=True)
        try:
            tar = subprocess.run(["tar", "-xf", tar_path, "-C", PODS_DIR],
                                 capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            tar = subprocess.CompletedProcess([], 1, "", "tar timed out")
        output += tar.stdout + tar.stderr
        if tar.returncode != 0:
            return {"ok": False, "name": name, "action": "restore",
                    "status": f"exit {tar.returncode}", "error": "untar failed",
                    "output": output}
        # Re-render run.sh/stop.sh/... from the restored .config.json so the
        # scripts match the current engine (handles engine upgrades since the
        # snapshot was taken).
        info = pod_config(name)
        if info:
            rc = run_create(config_from_info(info))
            output += rc.stdout + rc.stderr
            if rc.returncode != 0:
                return {"ok": False, "name": name, "action": "restore",
                        "status": "render failed",
                        "error": "Restored data, but re-rendering scripts failed.",
                        "output": output}
        r = _run_action(name, "start")
        output += r["output"]
        return {"ok": r["ok"], "name": name, "action": "restore",
                "status": r["status"], "error": None, "output": output}
    finally:
        _op_end(name)


def op_backup_delete(name, ts):
    backups = load_backups()
    entries = backups.get(name, [])
    entry = next((e for e in entries if e["ts"] == ts), None)
    if not entry or not BACKUP_TS_RE.fullmatch(ts or ""):
        return {"ok": False, "name": name, "action": "backup-delete",
                "status": "error", "error": "Unknown backup.", "output": ""}
    backups[name] = [e for e in entries if e["ts"] != ts]
    if not backups[name]:
        del backups[name]
    save_backups(backups)
    try:
        os.remove(_backup_path(name, ts))
    except OSError:
        pass
    return {"ok": True, "name": name, "action": "backup-delete", "status": "ok",
            "error": None, "output": ""}


# =========================================================================
# Tailnet user machines (the Users page)
#
# A "user" here is a MACHINE wearing tag:tailarr-user (enrolled with a
# handed-out auth key). It can reach nothing until it also wears a
# tag:tailarr-can-<service> capability badge — flipping badges is one
# device-tags API call and never touches the policy file. Nicknames are a
# Tailarr-side registry (.users.json, keyed by stable node ID).
# See docs/acl-design.md.
# =========================================================================
TSAPI_FILE = os.path.join(PODS_DIR, ".tsapi.json")
USERS_FILE = os.path.join(PODS_DIR, ".users.json")
TS_USER_TAG = "tag:tailarr-user"
TS_CAN_PREFIX = "tag:tailarr-can-"
# First-class PEOPLE (v0.19.0): a person is an entity in .people.json
# whose enrollment keys carry tag:tailarr-u-<uid> — attribution rides
# the key's tags (acl-design §2), so devices are born owned and a
# reissued key ties the new device back automatically. u- tags are
# IDENTITY-ONLY: they enter the fenced tagOwners but may NEVER appear
# in a grant (netmap minimality treats them as user-wearable).
PEOPLE_FILE = os.path.join(PODS_DIR, ".people.json")
TS_PERSON_PREFIX = "tag:tailarr-u-"
# Pseudo-service granting the controller itself (the app's server module).
# tag:tailarr-can-server's grant dst is tag:tailarr-ctrl, not a svc- tag;
# the API's bearer-token auth is the real permission boundary behind it.
SERVER_SERVICE = "server"
# Pseudo-service granting the app's Search module the saved newznab
# indexers (Accounts vault). Unlike every other badge it maps to NO
# tailnet tag — indexers are public internet services the phone reaches
# directly, not tailnet devices — so _person_badge_tags deliberately
# never resolves it. It exists only to gate the indexer handout, so a
# paid indexer key reaches only people the admin grants Search to.
SEARCH_SERVICE = "search"


_oauth_lock = threading.Lock()
_oauth_cache = {"token": "", "exp": 0.0}


def _oauth_exchange(cid, secret):
    """Exchange an OAuth client for a short-lived access token.
    Returns (token, error) — the error string carries no secrets."""
    req = urllib.request.Request(
        "https://api.tailscale.com/api/v2/oauth/token",
        data=urllib.parse.urlencode(
            {"client_id": cid, "client_secret": secret}).encode(),
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return "", (f"OAuth token exchange rejected (HTTP {e.code}) — "
                    "check the client id and secret")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return "", f"OAuth token exchange failed: {e}"
    token = data.get("access_token", "")
    if not token:
        return "", "OAuth token exchange returned no access token"
    return token, None


def _cred_token(cfg):
    """Resolve a credential dict to a bearer token, uncached.
    Returns (token, mode, error); mode is 'token' | 'oauth' | None."""
    static = (cfg.get("token") or "").strip()
    if static:
        return static, "token", None
    cid = (cfg.get("oauth_client_id") or "").strip()
    secret = (cfg.get("oauth_client_secret") or "").strip()
    if cid and secret:
        token, err = _oauth_exchange(cid, secret)
        return token, "oauth", err
    return "", None, ("no credential: provide an OAuth client id+secret "
                      "or an API access token")


def _ts_token():
    """The Tailscale API credential.

    .tsapi.json holds either a static {"token": "tskey-api-..."} (simple,
    expires in 90 days, full access) or an OAuth client
    {"oauth_client_id": "...", "oauth_client_secret": "..."} — preferred:
    scope it to devices/auth_keys/policy_file writes only, tag it
    tag:tailarr-ctrl (the managed tagOwners name that tag as co-owner of
    every tailarr tag, so the client may assign them). Access tokens are
    exchanged on demand and cached until near expiry.
    """
    try:
        with open(TSAPI_FILE) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return ""
    static = (cfg.get("token") or "").strip()
    if static:
        return static
    if not ((cfg.get("oauth_client_id") or "").strip()
            and (cfg.get("oauth_client_secret") or "").strip()):
        return ""
    now = time.time()
    with _oauth_lock:
        if _oauth_cache["exp"] - 60 > now:
            return _oauth_cache["token"]
        token, _mode, err = _cred_token(cfg)
        if err:
            return ""
        _oauth_cache["token"] = token
        _oauth_cache["exp"] = now + 3600  # Tailscale access tokens live 1h
        return token


def _ts_api_with(token, method, path, body=None):
    """Tailscale API call with an explicit bearer token.
    Returns (status_code, parsed_or_text)."""
    req = urllib.request.Request(
        "https://api.tailscale.com/api/v2" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode()
            try:
                return r.status, json.loads(text) if text.strip() else {}
            except ValueError:
                return r.status, text
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:500]
    except (urllib.error.URLError, TimeoutError) as e:
        return 0, f"tailscale API unreachable: {e}"


def ts_api(method, path, body=None):
    """Minimal Tailscale API client. Returns (status_code, parsed_or_text)."""
    token = _ts_token()
    if not token:
        return 0, "no API token configured"
    return _ts_api_with(token, method, path, body)


def load_user_nicks():
    try:
        with open(USERS_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_user_nicks(data):
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, USERS_FILE)


def _is_system(name):
    """Infrastructure pods (SYSTEM_IMAGES + the gateway): controller-
    managed, never shareable, hidden from the admin UI's pod lists.
    The gateway matches by NAME — it runs the controller's image."""
    if name == GATEWAY_POD:
        return True
    img = (pod_config(name) or {}).get("image", "")
    return any(s in img for s in SYSTEM_IMAGES)


def _shareable_services():
    return [s for s in deployed_services()
            if s not in CONTROLLER_PODS and not _is_system(s)]


def _vault_indexers():
    """Saved newznab accounts from the vault, sorted for a stable
    handout. Empty when the vault has none."""
    return sorted(
        ((aid, e) for aid, e in load_accounts().items()
         if e.get("kind") == "newznab"),
        key=lambda kv: (kv[1].get("label", "").lower(), kv[0]))


def status_users():
    """People (first-class users, each with their devices) plus
    unassigned user machines (enrolled without a person tag)."""
    services = _shareable_services() + [SERVER_SERVICE]
    # "search" is grantable only once there are indexers to hand out —
    # no point offering an empty capability.
    if _vault_indexers():
        services.append(SEARCH_SERVICE)
    people = load_people()
    people_out = [{"id": uid, "name": p.get("name", uid),
                   "badges": sorted(p.get("badges") or []),
                   "created": p.get("created", 0), "devices": []}
                  for uid, p in people.items()]
    people_out.sort(key=lambda p: p["name"].lower())
    by_uid = {p["id"]: p for p in people_out}
    if not _ts_token():
        return {"configured": False, "error": None, "users": [],
                "people": people_out, "services": services}
    code, data = ts_api("GET", "/tailnet/-/devices")
    if code != 200:
        return {"configured": True, "error": f"devices API: {data}",
                "users": [], "people": people_out, "services": services}
    nicks = load_user_nicks()
    users = []
    for d in data.get("devices", []):
        tags = d.get("tags") or []
        if TS_USER_TAG not in tags:
            continue
        entry = {
            "id": d.get("nodeId", ""),
            "hostname": d.get("hostname", ""),
            "nickname": nicks.get(d.get("nodeId", ""), ""),
            "os": d.get("os", ""),
            "last_seen": d.get("lastSeen", ""),
            "ip": next((a for a in d.get("addresses", []) if "." in a), ""),
            "can": sorted(t[len(TS_CAN_PREFIX):] for t in tags
                          if t.startswith(TS_CAN_PREFIX)),
        }
        uid = next((t[len(TS_PERSON_PREFIX):] for t in tags
                    if t.startswith(TS_PERSON_PREFIX)), None)
        if uid in by_uid:
            by_uid[uid]["devices"].append(entry)
        else:
            users.append(entry)  # unassigned (or orphaned person tag)
    users.sort(key=lambda u: (u["nickname"] or u["hostname"]).lower())
    for p in people_out:
        p["devices"].sort(key=lambda u: u["hostname"].lower())
    return {"configured": True, "error": None, "users": users,
            "people": people_out, "services": services}


def op_user_nick(node_id, nickname):
    nickname = (nickname or "").strip()[:40]
    nicks = load_user_nicks()
    if nickname:
        nicks[node_id] = nickname
    else:
        nicks.pop(node_id, None)
    save_user_nicks(nicks)
    return {"ok": True, "id": node_id, "nickname": nickname, "error": None}


def op_user_access(node_id, service, allow):
    """Grant/revoke a service to a user machine: flip its can-<svc> badge.

    Tag membership only — the policy file is never touched here. The
    device must already wear tag:tailarr-user (we manage user machines,
    nothing else), and the can- tag must exist in the tailnet policy's
    tagOwners (it does for every installed service once the fenced
    generator has run; errors from Tailscale are surfaced verbatim).

    "server" is the controller itself (the app's server module): its badge
    tag:tailarr-can-server grants network reach to tag:tailarr-ctrl:443.
    Grant it together with an API token — the token is the permission
    boundary; the tag only opens the pipe.
    """
    if service not in _shareable_services() + [SERVER_SERVICE]:
        return {"ok": False, "id": node_id, "error": "Unknown service."}
    code, dev = ts_api("GET", f"/device/{node_id}")
    if code != 200:
        return {"ok": False, "id": node_id, "error": f"device API: {dev}"}
    tags = dev.get("tags") or []
    if TS_USER_TAG not in tags:
        return {"ok": False, "id": node_id,
                "error": "Not a tailarr user machine."}
    badge = TS_CAN_PREFIX + service
    new_tags = [t for t in tags if t != badge]
    if allow:
        new_tags.append(badge)
    if sorted(new_tags) == sorted(tags):
        return {"ok": True, "id": node_id, "error": None}  # no-op
    code, resp = ts_api("POST", f"/device/{node_id}/tags",
                        {"tags": sorted(new_tags)})
    if code != 200:
        return {"ok": False, "id": node_id, "error": f"tags API: {resp}"}
    return {"ok": True, "id": node_id, "error": None}


def op_user_adopt(node_id):
    """Adopt an already-enrolled tailnet device as a user machine: add
    tag:tailarr-user via the device-tags API.

    For devices that joined by logging in (an Apple TV signed in with an
    Apple ID, say) instead of enrolling with a minted key. Tagging a
    login-owned device converts it to a tagged device — Tailscale drops
    the user ownership and key expiry. It appears on the Users page with
    zero badges.
    """
    node_id = (node_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]+", node_id):
        return {"ok": False, "id": node_id, "hostname": "",
                "error": "That doesn't look like a node ID."}
    code, dev = ts_api("GET", f"/device/{node_id}")
    if code != 200:
        return {"ok": False, "id": node_id, "hostname": "",
                "error": f"device API: {dev}"}
    hostname = dev.get("hostname", "")
    tags = dev.get("tags") or []
    if TS_USER_TAG in tags:
        return {"ok": True, "id": node_id, "hostname": hostname,
                "error": None}  # already a user machine
    if any(t.startswith("tag:tailarr") for t in tags):
        return {"ok": False, "id": node_id, "hostname": hostname,
                "error": f"'{hostname}' is part of the Tailarr fleet, "
                         "not a consumer device."}
    code, resp = ts_api("POST", f"/device/{node_id}/tags",
                        {"tags": sorted(tags + [TS_USER_TAG])})
    if code != 200:
        return {"ok": False, "id": node_id, "hostname": hostname,
                "error": f"tags API: {resp}"}
    return {"ok": True, "id": node_id, "hostname": hostname, "error": None}


# =========================================================================
# People — first-class users (v0.19.0). See the PEOPLE_FILE note above.
# =========================================================================
def load_people():
    try:
        with open(PEOPLE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_people(data):
    tmp = PEOPLE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, PEOPLE_FILE)


def _person_tag(uid):
    return TS_PERSON_PREFIX + uid


def _person_badge_tags(person):
    """The can- tags a person's badges resolve to RIGHT NOW — only ones
    that exist in the policy's tagOwners (deployed services + server),
    so a stale badge can never wedge a key mint or a tag write."""
    valid = set(_shareable_services()) | {SERVER_SERVICE}
    return [TS_CAN_PREFIX + s for s in sorted(person.get("badges") or [])
            if s in valid]


def ts_mint_person_key(uid, person):
    """Mint a single-use, preauthorized enrollment key for a person.

    Tags = tag:tailarr-user + tag:tailarr-u-<uid> + their current can-
    badges (DECIDED: keys carry badges, so a new device has its access
    the moment it joins). The u- tag must already be in the fenced
    tagOwners — op_person syncs the policy before minting (the same
    policy-before-mint ordering the bootstrap uses)."""
    if not _ts_token():
        return {"ok": False, "error": "no API token configured", "key": ""}
    tags = [TS_USER_TAG, _person_tag(uid)] + _person_badge_tags(person)
    code, resp = ts_api("POST", "/tailnet/-/keys", {
        "capabilities": {"devices": {"create": {
            "reusable": False, "ephemeral": False, "preauthorized": True,
            "tags": tags}}},
        "expirySeconds": 86400,
        "description": f"tailarr user {person.get('name', uid)}",
    })
    if code != 200:
        return {"ok": False, "error": f"keys API: {resp}", "key": ""}
    return {"ok": True, "error": None, "key": resp.get("key", "")}


def _person_device_tags(device_tags, person):
    """The tag set a person's device SHOULD wear: keep every non-badge
    tag it already has (user tag, u- tag, public, anything foreign),
    replace the can- set with exactly the person's badges."""
    keep = [t for t in device_tags if not t.startswith(TS_CAN_PREFIX)]
    return sorted(set(keep) | set(_person_badge_tags(person)))


def op_person(data):
    """POST /api/people {do: add|rename|reissue|delete|assign, ...}.

    add: create the person, splice their u- tag into the fenced
    tagOwners (policy-before-mint), mint their enrollment key.
    reissue: a fresh key with the same u- tag + current badges — the
    new device ties back to the person automatically.
    assign: attach an already-enrolled user machine to a person (adds
    the u- tag and aligns badges).
    delete: remove the person; their devices lose the u- tag and all
    badges (they fall back to plain unassigned user machines)."""
    do = (data.get("do") or "").strip()
    people = load_people()
    if do == "add":
        name = (data.get("name") or "").strip()[:60]
        if not name:
            return {"ok": False, "error": "A name is required."}
        uid = secrets.token_hex(4)
        people[uid] = {"name": name, "created": int(time.time()),
                       "badges": []}
        save_people(people)
        if not _ts_token():
            return {"ok": True, "id": uid, "key": "",
                    "error": "Created — but no API credential, so no key "
                             "was minted. Configure Settings and reissue."}
        sync = ts_policy_sync()  # u- tagOwner must land before the mint
        if not sync["ok"]:
            return {"ok": True, "id": uid, "key": "",
                    "error": f"Created, but the policy sync failed "
                             f"({sync['error']}) — reissue once fixed."}
        mint = ts_mint_person_key(uid, people[uid])
        _ntfy_person_sync_bg(uid)  # notifications are part of the person
        return {"ok": True, "id": uid, "key": mint.get("key", ""),
                "error": mint.get("error")}
    uid = (data.get("id") or "").strip()
    if uid not in people:
        return {"ok": False, "error": "Unknown user."}
    if do == "rename":
        name = (data.get("name") or "").strip()[:60]
        if not name:
            return {"ok": False, "error": "A name is required."}
        people[uid]["name"] = name
        save_people(people)
        return {"ok": True, "id": uid, "error": None}
    if do == "reissue":
        mint = ts_mint_person_key(uid, people[uid])
        return {"ok": mint["ok"], "id": uid, "key": mint.get("key", ""),
                "error": mint.get("error")}
    if do == "assign":
        node = (data.get("node") or "").strip()
        code, dev = ts_api("GET", f"/device/{node}")
        if code != 200:
            return {"ok": False, "error": f"device API: {dev}"}
        tags = dev.get("tags") or []
        if TS_USER_TAG not in tags:
            return {"ok": False, "error": "Not a tailarr user machine."}
        if any(t.startswith(TS_PERSON_PREFIX) for t in tags):
            return {"ok": False, "error": "Already assigned to a user."}
        new = _person_device_tags(tags + [_person_tag(uid)], people[uid])
        code, resp = ts_api("POST", f"/device/{node}/tags", {"tags": new})
        if code != 200:
            return {"ok": False, "error": f"tags API: {resp}"}
        return {"ok": True, "id": uid, "error": None}
    if do == "delete":
        person = people.pop(uid)
        save_people(people)
        errors = []
        code, data_ = ts_api("GET", "/tailnet/-/devices")
        if code == 200:
            for d in data_.get("devices", []):
                tags = d.get("tags") or []
                if _person_tag(uid) not in tags:
                    continue
                new = sorted(t for t in tags
                             if t != _person_tag(uid)
                             and not t.startswith(TS_CAN_PREFIX))
                code2, resp = ts_api(
                    "POST", f"/device/{d['nodeId']}/tags", {"tags": new})
                if code2 != 200:
                    errors.append(f"{d.get('hostname', '?')}: {resp}")
        if _ts_token():
            ts_policy_sync()  # drops the u- tagOwner line
        try:
            _ntfy_person_drop(uid)  # their notification account too
        except Exception as e:  # noqa: BLE001 — best-effort cleanup
            errors.append(f"ntfy: {e}")
        return {"ok": True, "id": uid,
                "error": ("; ".join(errors)[:300] or None),
                "name": person.get("name", "")}
    return {"ok": False, "error": f"unknown do '{do}'"}


def op_person_access(uid, service, allow):
    """Flip a service badge for a PERSON: update the registry, then fan
    the can- tag out to every device they own (the per-user model —
    devices inherit the person's access). The reconcile pass covers
    devices that enroll later."""
    people = load_people()
    if uid not in people:
        return {"ok": False, "id": uid, "error": "Unknown user."}
    if service not in _shareable_services() + [SERVER_SERVICE, SEARCH_SERVICE]:
        return {"ok": False, "id": uid, "error": "Unknown service."}
    badges = set(people[uid].get("badges") or [])
    (badges.add if allow else badges.discard)(service)
    people[uid]["badges"] = sorted(badges)
    save_people(people)
    errors = []
    code, data = ts_api("GET", "/tailnet/-/devices")
    if code != 200:
        return {"ok": True, "id": uid,
                "error": f"saved, but devices API failed: {data}"}
    for d in data.get("devices", []):
        tags = d.get("tags") or []
        if _person_tag(uid) not in tags:
            continue
        new = _person_device_tags(tags, people[uid])
        if new == sorted(tags):
            continue
        code2, resp = ts_api("POST", f"/device/{d['nodeId']}/tags",
                             {"tags": new})
        if code2 != 200:
            errors.append(f"{d.get('hostname', '?')}: {resp}")
    _ntfy_person_sync_bg(uid)  # mirror the badge into topic grants
    return {"ok": True, "id": uid,
            "error": ("; ".join(errors)[:300] or None)}


def ts_reconcile_people():
    """Assert every person's devices wear exactly their badges (plus
    whatever non-badge tags they already carry). Covers devices that
    enrolled with an older key, missed flips, and hand-edited tags.
    Quiet best-effort, like ts_reconcile_tags."""
    people = load_people()
    if not people or not _ts_token():
        return
    code, data = ts_api("GET", "/tailnet/-/devices")
    if code != 200:
        return
    for d in data.get("devices", []):
        tags = d.get("tags") or []
        uid = next((t[len(TS_PERSON_PREFIX):] for t in tags
                    if t.startswith(TS_PERSON_PREFIX)), None)
        if uid is None or uid not in people:
            continue
        new = _person_device_tags(tags, people[uid])
        if new != sorted(tags):
            ts_api("POST", f"/device/{d['nodeId']}/tags", {"tags": new})


def _ntfy_person_topics(person):
    """The ntfy topics a person may read, mirrored from their badges:
    tlr-media-<svc> per service; the server badge (admin-ish) also
    opens the ops topic."""
    badges = person.get("badges") or []
    topics = [ntfy_client.MEDIA_TOPIC_PREFIX + s for s in badges
              if s != SERVER_SERVICE]
    if SERVER_SERVICE in badges:
        topics.append(ntfy_client.OPS_TOPIC)
    return sorted(topics)


def _ntfy_person_sync(uid, person, pod=None):
    """Ensure a person's ntfy account (u-<uid>) exists with read grants
    mirroring their badges. Check-then-act like every other ntfy
    provisioning path; grants are reset+re-issued only when the badge
    set drifted from what was last synced. Returns (entry, error);
    entry is the credential dict stored under .ntfy.json users."""
    conf = ntfy_client.load_conf()
    if not conf:
        return None, "ntfy is not configured"
    if pod is None:
        net = _discover_ntfy(fresh=True)
        if not net:
            return None, "No ntfy pod found."
        pod = net["name"]
    user = f"u-{uid}"
    saved = (conf.get("users") or {}).get(uid) or {}
    listed = _ntfy_cli(pod, "user", "list")
    if listed.returncode != 0:
        return None, ("ntfy CLI unavailable: "
                      + (listed.stdout + listed.stderr).strip()[-200:])
    exists = f"user {user}" in (listed.stdout + listed.stderr)
    password = (saved.get("password") if exists else None) \
        or secrets.token_urlsafe(24)
    if not exists:
        r = _ntfy_cli(pod, "user", "add", "--role=user", user,
                      env={"NTFY_PASSWORD": password})
        if r.returncode != 0 and "exists" not in (r.stdout + r.stderr):
            return None, ("could not create the account: "
                          + (r.stdout + r.stderr).strip()[-200:])
    elif not saved.get("password"):
        r = _ntfy_cli(pod, "user", "change-pass", user,
                      env={"NTFY_PASSWORD": password})
        if r.returncode != 0:
            return None, ("could not reset the password: "
                          + (r.stdout + r.stderr).strip()[-200:])
    token = saved.get("token", "") if exists else ""
    if not token:
        r = _ntfy_cli(pod, "token", "add", user)
        m = re.search(r"tk_[A-Za-z0-9_]+", r.stdout + r.stderr)
        if r.returncode != 0 or not m:
            return None, ("could not mint a token: "
                          + (r.stdout + r.stderr).strip()[-200:])
        token = m.group(0)
    badges = sorted(person.get("badges") or [])
    if not exists or sorted(saved.get("services") or []) != badges:
        _ntfy_cli(pod, "access", "--reset", user)
        for t in _ntfy_person_topics(person):
            r = _ntfy_cli(pod, "access", user, t, "read")
            if r.returncode != 0:
                return None, (f"could not grant {t}: "
                              + (r.stdout + r.stderr).strip()[-200:])
    entry = {"user": user, "password": password, "token": token,
             "services": badges}
    conf.setdefault("users", {})[uid] = entry
    ntfy_client.save_conf(conf)
    return entry, None


def _ntfy_person_sync_bg(uid):
    """Fire-and-forget badge->topic mirror (person add / badge flips):
    ntfy trouble must never fail the tailnet operation that worked."""
    if not ntfy_client.load_conf():
        return
    def run():
        people = load_people()
        if uid in people:
            _ntfy_person_sync(uid, people[uid])
    threading.Thread(target=run, daemon=True).start()


def _ntfy_person_drop(uid):
    """Delete a person's ntfy account (tokens + grants die with it)."""
    conf = ntfy_client.load_conf()
    if not conf or uid not in (conf.get("users") or {}):
        return
    net = _discover_ntfy(fresh=True)
    if net:
        _ntfy_cli(net["name"], "user", "del", f"u-{uid}")
    conf["users"].pop(uid, None)
    ntfy_client.save_conf(conf)


def _ntfy_people_pass():
    """Maintenance-loop mirror: converge every person's ntfy account to
    their badges (covers missed flips and ntfy-set-up-after-users), and
    drop accounts for people who no longer exist."""
    conf = ntfy_client.load_conf()
    if not conf:
        return
    people = load_people()
    saved = conf.get("users") or {}
    for uid, p in people.items():
        s = saved.get(uid)
        if not s or sorted(s.get("services") or []) != \
                sorted(p.get("badges") or []):
            _ntfy_person_sync(uid, p)
    for uid in [u for u in saved if u not in people]:
        _ntfy_person_drop(uid)


def op_person_notify(uid):
    """The person-card handout: idempotent issue of their notification
    credentials (auto-created at Add User; this converges + re-shows).
    Like the admin alerts card, the token/password ARE the handout —
    returning them is the point."""
    people = load_people()
    if uid not in people:
        return {"ok": False, "error": "Unknown user."}
    entry, err = _ntfy_person_sync(uid, people[uid])
    if err:
        return {"ok": False, "error": err}
    conf = ntfy_client.load_conf() or {}
    return {"ok": True, "error": None,
            "url": conf.get("public_url", ""),
            "user": entry["user"], "password": entry["password"],
            "token": entry["token"],
            "topics": _ntfy_person_topics(people[uid])}


# =========================================================================
# Tailnet policy sync — the fenced-grant generator (docs/acl-design.md §4)
#
# Tailarr owns three labeled fenced regions of the tailnet policy file
# (grants / tagOwners / nodeAttrs) and regenerates them from the deployed
# service list on install/remove. Line-level splicing only: the human's
# HuJSON outside the fences survives byte-for-byte. Fail closed on any
# fence anomaly; nothing inside a fence may reference a name outside
# tag:tailarr* (the prefix invariant).
#
# NETMAP MINIMALITY (visibility invariant — docs/acl-design.md §12):
# Tailscale prunes a peer from a node's netmap only when the policy allows
# ZERO traffic between them in either direction; ANY rule matching the
# pair — any port, any direction, even ping — makes the peer's name and
# tailnet IPs visible. So "users can't connect to ungranted services" is
# not enough: a tag:tailarr-user device must have NO rule at all
# connecting it to anything beyond its can-* badges. Concretely, a fence
# grant whose src can match a user device (tag:tailarr-user itself or a
# can-* badge) must be either a single-badge -> single-service network
# grant, or an app-capability-only grant (peer relay: no "ip" key, no
# network access — but note the relay DEVICE does become visible to every
# consumer, a deliberate trade); and user selectors may never appear in
# any dst (reverse-direction rules create visibility too). Enforced fail-
# closed by _grants_minimality_ok before every splice. Revocation needs
# no extra work: flipping a can-* badge off unmatches the grant and the
# pod drops out of the device's netmap on the next map push.
# =========================================================================
_policy_lock = threading.Lock()
ACL_BACKUP_FILE = os.path.join(PODS_DIR, ".acl-last-good.hujson")
FENCE_BEGIN = "// >>> tailarr-managed:"
FENCE_END = "// <<< tailarr-managed:"


def _managed_sections(relay_dst=None):
    """Desired fence contents, derived from the deployed service list.

    relay_dst: None derives the peer-relay grant from saved state
    (_relay_grant_wanted); "" force-excludes it (used to probe whether a
    validate rejection was the relay grant's fault); "admin"/"member"
    force-include it with that autogroup in dst."""
    svcs = _shareable_services()
    grants = [
        '{"src": ["tag:tailarr"], "dst": ["tag:tailarr"], "ip": ["*"]},',
        # Funnel ingress traffic is NOT exempt from the packet filter under
        # default-deny (tailscale/tailscale#18181) — admit Tailscale's
        # ingress range to public-tagged pods or Funnel silently drops.
        '{"src": ["fd7a:115c:a1e0:ab12::/64"], '
        '"dst": ["tag:tailarr-public"], "ip": ["*"]}, // funnel ingress',
        # The controller as a grantable "service" (the Tailarr app's server
        # module). Network reach only — the API's bearer-token auth is the
        # actual permission boundary; see docs/acl-design.md addendum.
        '{"src": ["tag:tailarr-can-server"], '
        '"dst": ["tag:tailarr-ctrl"], "ip": ["443"]},',
    ]
    for s in svcs:
        grants.append(f'{{"src": ["tag:tailarr-can-{s}"], '
                      f'"dst": ["tag:tailarr-svc-{s}"], "ip": ["443"]}},')
    # The self-config gateway: the ONE deliberate exception to "user
    # devices reach only their badges" (§12 addendum) — every user
    # device may ask the gateway for its own notification config. The
    # minimality checker's carve-out matches exactly this line.
    if GATEWAY_POD in deployed_services():
        grants.append(f'{{"src": ["{TS_USER_TAG}"], '
                      f'"dst": ["tag:tailarr-svc-{GATEWAY_POD}"], '
                      f'"ip": ["{GATEWAY_PORT}"]}}, // self-config gateway')
    # tag:tailarr-ctrl co-owns every other tag so an OAuth client tagged
    # tag:tailarr-ctrl may assign them (device tagging + key minting).
    # It must also own ITSELF: the client acts as tag:tailarr-ctrl, and a
    # tag does not implicitly own itself — without the self-entry the
    # controller-start reconcile can never apply tag:tailarr-ctrl to the
    # controller sidecar (live-caught on a fresh tailnet, 2026-07-19; a
    # full-access static token masks it by acting as autogroup:admin).
    OWN = '["autogroup:admin", "tag:tailarr-ctrl"]'
    owners = [f'"tag:tailarr-ctrl": {OWN},']
    owners += [f'"{t}": {OWN},'
               for t in ("tag:tailarr", "tag:tailarr-user",
                         "tag:tailarr-public", "tag:tailarr-can-server")]
    # Identity tags exist for EVERY non-controller pod — ts_tag_sidecar
    # applies tag:tailarr-svc-* to system pods too, so their tagOwners
    # must be here or the tags API rejects the write and the pod reads
    # identity "missing" forever. can- badges stay shareable-only: a
    # system pod gets no badge, no grant line, and therefore no presence
    # in any consumer netmap (§12).
    for s in [x for x in deployed_services() if x not in CONTROLLER_PODS]:
        owners.append(f'"tag:tailarr-svc-{s}": {OWN},')
    for s in svcs:
        owners.append(f'"tag:tailarr-can-{s}": {OWN},')
    # Person identity tags: tagOwners ONLY, never a grant — they mark
    # which person owns a device (netmap minimality treats them as
    # user-wearable, so a grant referencing one fails the sync).
    for uid in sorted(load_people()):
        if re.fullmatch(r"[a-f0-9]+", uid):
            owners.append(f'"{TS_PERSON_PREFIX}{uid}": {OWN},')
    # Peer relay: see _relay_sections. The cap grants RELAYING only, never
    # network access; BOTH ends of a connection need it.
    relay_grants, relay_owners = _relay_sections(relay_dst)
    grants += relay_grants
    owners += relay_owners
    attrs = ['{"target": ["tag:tailarr-public"], "attr": ["funnel"]},']
    return {"grants": grants, "tagowners": owners, "nodeattrs": attrs}


def _relay_cap(src, dst, note):
    """One peer-relay cap grant line. src = who may USE the relay (the pod
    sidecars plus the consumer devices — both ends of a connection need the
    cap, so untagged member devices are always included); dst = who may ACT
    as the relay."""
    return ('{"src": [' + src + '], "dst": [' + dst + '], '
            '"app": {"tailscale.com/cap/relay": []}}, // ' + note)


# Devices allowed to USE a relay alongside the pod sidecars: enrolled user
# devices and the admin's untagged phone/laptop. Visibility trade
# (deliberate — acl-design.md §12): a cap grant is still a rule, so the
# relay dst becomes VISIBLE (name + IPs, no access) in every consumer's
# netmap — with the legacy autogroup dst that is every admin/member
# device, with a registry-IP dst just the one relay. Enabling relay is
# therefore the one opt-in that widens a scoped user's peer list.
_RELAY_CONSUMERS = '"tag:tailarr-user", "autogroup:member"'


def _relay_sections(relay_dst=None):
    """Peer-relay grant + tagOwner lines for the managed fences.

    v0.13.0 shipped ONE tailnet-wide grant whose dst was the autogroup
    ladder (admin -> member on validate-reject): "any admin device may act
    as a relay". v0.15.0 generalizes: the registry in .relay.json holds
    VERIFIED relay devices, and the grant dst can name a specific relay's
    tailnet IP — globally (all pods share it) or per pod (a grant per
    tag:tailarr-svc-<name>, "server" meaning the controller). With no
    registry selection the legacy autogroup grant is emitted unchanged, so
    upgraded installs keep exactly their v0.13.0 behavior.

    relay_dst: None derives everything from saved state; "" force-excludes
    the grant (the relay-free validate probe); "admin"/"member" force the
    legacy autogroup dst (downgrade-ladder retries)."""
    OWN = '["autogroup:admin", "tag:tailarr-ctrl"]'
    legacy_owner = [f'"tag:tailarr-relay": {OWN},']
    if relay_dst == "":
        return [], []
    if relay_dst in ("admin", "member"):
        return ([_relay_cap('"tag:tailarr", ' + _RELAY_CONSUMERS,
                            f'"tag:tailarr-relay", "autogroup:{relay_dst}"',
                            "peer relay")], legacy_owner)
    if not _relay_grant_wanted():
        return [], []
    r = load_relay()
    relays = r.get("relays") or {}
    if (r.get("mode") or "global") == "global":
        ip = (relays.get(r.get("global_relay") or "") or {}).get("ip")
        if ip:
            return ([_relay_cap('"tag:tailarr", ' + _RELAY_CONSUMERS,
                                f'"{ip}/32"', "peer relay (global)")], [])
        dst = "member" if r.get("dst_fallback") else "admin"
        return ([_relay_cap('"tag:tailarr", ' + _RELAY_CONSUMERS,
                            f'"tag:tailarr-relay", "autogroup:{dst}"',
                            "peer relay")], legacy_owner)
    grants = []
    deployed = set(deployed_services())
    for key, rid in sorted((r.get("pod_relays") or {}).items()):
        ip = (relays.get(rid) or {}).get("ip")
        if not ip:
            continue
        if key == "server":
            src = '"tag:tailarr-ctrl", ' + _RELAY_CONSUMERS
        elif key in deployed:
            src = f'"tag:tailarr-svc-{key}", ' + _RELAY_CONSUMERS
        else:
            continue  # pod since removed; stale selection stays inert
        grants.append(_relay_cap(src, f'"{ip}/32"', f"peer relay ({key})"))
    return grants, []


def _legacy_relay_dst_in_use():
    """True when the emitted grant (if any) uses the autogroup-dst ladder —
    the only shape the admin->member downgrade rung applies to. Specific-IP
    dsts skip straight to the disable rung on validate-reject."""
    r = load_relay()
    if (r.get("mode") or "global") != "global":
        return False
    return not ((r.get("relays") or {})
                .get(r.get("global_relay") or "") or {}).get("ip")


def _sections_prefix_ok(sections):
    """The safety invariant: fences may only reference tag:tailarr* names."""
    for lines in sections.values():
        for ln in lines:
            for t in re.findall(r'"(tag:[a-z0-9-]+)"', ln):
                if not t.startswith("tag:tailarr"):
                    return False
    return True


def _grant_obj(line):
    """Parse one generated grant line (single-line JSON object, trailing
    comma, optional // comment) back into a dict. Raises on anything that
    doesn't parse — a generated line we can't read is a line we can't
    audit, so the caller fails closed."""
    return json.loads(line[:line.rindex("}") + 1])


def _grants_minimality_ok(grant_lines):
    """The netmap-minimality invariant (see the block comment above and
    docs/acl-design.md §12): no grant may widen what a tag:tailarr-user
    device can see. A selector a user device can wear (tag:tailarr-user
    or any tag:tailarr-can-*) may appear only

      - in the src of an app-capability-only grant (no "ip" key — the
        peer-relay cap; no network access is conferred), or
      - as the SOLE src of a network grant with a SOLE dst (the per-badge
        access switch: can-<svc> -> svc-<svc>, can-server -> ctrl), or
      - as EXACTLY the self-config gateway grant (tag:tailarr-user ->
        tag:tailarr-svc-tailarr-gate on the gateway port only) — the one
        deliberate visibility exception, decided 2026-07-22: user
        netmaps gain the gateway node so the app can self-configure;
        the controller stays invisible,

    and never in any dst (reverse-direction rules create visibility too).
    Anything else — user tags bundled into a broad src, a catch-all dst,
    a rule targeting user devices — fails the whole sync."""
    def wearable(name):
        return (name == TS_USER_TAG or name.startswith(TS_CAN_PREFIX)
                or name.startswith(TS_PERSON_PREFIX))
    try:
        for ln in grant_lines:
            g = _grant_obj(ln)
            src, dst = g.get("src", []), g.get("dst", [])
            if any(wearable(d) for d in dst):
                return False
            if not any(wearable(s) for s in src):
                continue
            if "ip" not in g and "app" in g:
                continue  # cap-only (peer relay): relaying, not access
            if len(src) == 1 and src[0].startswith(TS_CAN_PREFIX) \
                    and len(dst) == 1:
                continue  # the per-badge access switch
            if src == [TS_USER_TAG] \
                    and dst == [f"tag:tailarr-svc-{GATEWAY_POD}"] \
                    and g.get("ip") == [GATEWAY_PORT]:
                continue  # the self-config gateway (sole exception)
            return False
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _splice_fences(text, sections):
    """Replace each labeled fenced region's content; leave everything else
    untouched. Raises ValueError (fail closed) on any marker anomaly."""
    lines = text.splitlines()
    out, seen, i, n = [], set(), 0, len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith(FENCE_END):
            raise ValueError(f"stray end marker at line {i + 1}")
        if not stripped.startswith(FENCE_BEGIN):
            out.append(line)
            i += 1
            continue
        sec = stripped[len(FENCE_BEGIN):].strip()
        if sec not in sections:
            raise ValueError(f"unknown managed section '{sec}'")
        if sec in seen:
            raise ValueError(f"duplicate managed section '{sec}'")
        seen.add(sec)
        indent = line[:len(line) - len(line.lstrip())]
        out.append(line)
        j = i + 1
        while j < n and not lines[j].strip().startswith(FENCE_END):
            if lines[j].strip().startswith(FENCE_BEGIN):
                raise ValueError(f"nested managed markers in '{sec}'")
            j += 1
        if j >= n:
            raise ValueError(f"missing end marker for '{sec}'")
        end_sec = lines[j].strip()[len(FENCE_END):].strip()
        if end_sec != sec:
            raise ValueError(f"mismatched fence labels: '{sec}' vs '{end_sec}'")
        out.extend(indent + c for c in sections[sec])
        out.append(lines[j])
        i = j + 1
    missing = sorted(set(sections) - seen)
    if missing:
        raise ValueError(f"managed sections missing from policy: {missing} "
                         "(re-run adopt: add the fenced markers)")
    return "\n".join(out) + "\n"


def _ts_acl(method, path_suffix="", body_text=None, etag=None):
    """Raw-HuJSON ACL endpoint client. Returns (status, text, etag)."""
    token = _ts_token()
    if not token:
        return 0, "no API token configured", ""
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/hujson"}
    if body_text is not None:
        headers["Content-Type"] = "application/hujson"
    if etag:
        headers["If-Match"] = f'"{etag}"'
    req = urllib.request.Request(
        "https://api.tailscale.com/api/v2/tailnet/-/acl" + path_suffix,
        data=body_text.encode() if body_text is not None else None,
        headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode(), \
                (r.headers.get("ETag") or "").strip('"')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:500], ""
    except (urllib.error.URLError, TimeoutError) as e:
        return 0, f"tailscale API unreachable: {e}", ""


def ts_policy_sync():
    """Regenerate the tailarr-managed regions of the tailnet policy.
    Returns {ok, changed, error}."""
    sections = _managed_sections()
    if not _sections_prefix_ok(sections):
        return {"ok": False, "changed": False,
                "error": "generated content violates the tag prefix rule"}
    if not _grants_minimality_ok(sections["grants"]):
        return {"ok": False, "changed": False,
                "error": "generated grants violate netmap minimality "
                         "(user-device visibility would widen)"}
    with _policy_lock:
        # 2 attempts covers a 412 retry; the extra headroom is for the
        # peer-relay downgrade ladder (admin dst -> member dst -> no grant).
        for _attempt in range(4):
            code, raw, etag = _ts_acl("GET")
            if code != 200:
                return {"ok": False, "changed": False, "error": f"acl GET: {raw}"}
            try:
                new_text = _splice_fences(raw, sections)
            except ValueError as e:
                return {"ok": False, "changed": False, "error": f"policy fences: {e}"}
            if new_text == raw:
                # Policy already right — but the field showed a node can
                # still be missing its svc tag (tag write raced/failed).
                # Every sync doubles as a tag reconcile trigger.
                threading.Thread(target=ts_reconcile_tags,
                                 daemon=True).start()
                return {"ok": True, "changed": False, "error": None}
            code, resp, _ = _ts_acl("POST", "/validate", new_text)
            if code != 200 or '"message"' in resp:
                # A relay problem must never wedge policy sync. If the
                # rejected policy carried the relay grant, probe a
                # relay-free splice: only when THAT validates is the grant
                # the culprit — then downgrade (member dst, then give up)
                # and retry. Otherwise surface the original error.
                downgraded = _relay_downgrade_after_reject(raw, resp)
                if downgraded is not None and \
                        _grants_minimality_ok(downgraded["grants"]):
                    sections = downgraded
                    continue
                return {"ok": False, "changed": False, "error": f"acl validate: {resp[:300]}"}
            # keep the last-known-good policy for one-call rollback
            try:
                with open(ACL_BACKUP_FILE + ".tmp", "w") as f:
                    f.write(raw)
                os.replace(ACL_BACKUP_FILE + ".tmp", ACL_BACKUP_FILE)
            except OSError:
                pass
            code, resp, _ = _ts_acl("POST", "", new_text, etag=etag)
            if code == 200:
                # tagOwners just (re)landed: nodes whose tag write was
                # rejected before this sync can be fixed now.
                threading.Thread(target=ts_reconcile_tags,
                                 daemon=True).start()
                return {"ok": True, "changed": True, "error": None}
            if code != 412:
                return {"ok": False, "changed": False, "error": f"acl POST: {resp[:300]}"}
            # 412: someone else edited the policy — refetch and retry once
        return {"ok": False, "changed": False,
                "error": "acl POST: concurrent edits kept winning (412)"}


def _ts_find_device(hostname):
    code, data = ts_api("GET", "/tailnet/-/devices")
    if code != 200:
        return None
    matches = [d for d in data.get("devices", [])
               if d.get("hostname") == hostname]
    matches.sort(key=lambda d: d.get("lastSeen", ""), reverse=True)
    return matches[0] if matches else None


# Live identity-tag state per pod, fed by every ts_tag_sidecar run (post-
# start hooks, reconcile passes). Surfaced as `identity` in /api/pods so a
# mis-tagged service can never look fully green.
_tag_state = {}  # name -> "ok" | "missing" | "unknown"
_tag_state_lock = threading.Lock()


def _record_tag_state(name, state):
    with _tag_state_lock:
        _tag_state[name] = state
    return state


def ts_tag_sidecar(name, attempts=6):
    """Ensure a pod's sidecar wears its identity tags.

    tag:tailarr-svc-<name> is the *dst* of the per-service access grant: a
    node missing it passes every health check (the controller's broad
    tag:tailarr grant still reaches it) while EVERY user device is dropped
    at the packet filter — seen in the field as "LunaSea reaches sonarr
    but not radarr" with nothing to go on. So this is no longer a silent
    one-shot: failures (sidecar not enrolled yet, tags API rejecting
    because the tagOwners policy sync hasn't landed) retry with backoff,
    the outcome is recorded for the UI badge, and reconcile passes re-run
    it on controller start, after policy syncs, and periodically.
    Idempotent; preserves tag:tailarr-public. Returns the recorded state:
    "ok" | "missing" | "unknown"."""
    if not _ts_token():
        return _record_tag_state(name, "unknown")
    want = {"tag:tailarr",
            "tag:tailarr-ctrl" if name in CONTROLLER_PODS
            else f"tag:tailarr-svc-{name}"}
    delay = 3
    for attempt in range(attempts):
        if attempt:
            time.sleep(delay)
            delay = min(delay * 2, 30)  # 3+6+12+24+30 ≈ 75s worst case
        d = _ts_find_device(name)
        if not d:
            continue  # enrollment may lag the container start by seconds
        tags = set(d.get("tags") or [])
        if want <= tags:
            return _record_tag_state(name, "ok")
        keep = {t for t in tags if t == "tag:tailarr-public"}
        code, resp = ts_api("POST", f"/device/{d['nodeId']}/tags",
                            {"tags": sorted(want | keep)})
        if code == 200:
            return _record_tag_state(name, "ok")
        # Typical retryable rejection: tagOwners doesn't carry the svc tag
        # yet because the policy sync raced this hook. Back off and retry.
        print(f"tagging {name} (attempt {attempt + 1}/{attempts}): "
              f"tags API {code}: {resp}")
    print(f"identity tag NOT applied to {name} — user devices cannot reach "
          "it until a reconcile pass succeeds (see the pod's identity badge)")
    return _record_tag_state(name, "missing")


def ts_reconcile_tags():
    """Re-assert identity tags on every running sidecar (+ controller).

    A single missed ts_tag_sidecar used to be unrecoverable in practice:
    "the next start retries" only helps if that specific pod restarts, and
    nothing does. Runs on controller start, after successful policy syncs,
    and from the periodic maintenance loop, so a missed tag self-heals on
    the next natural event. Cheap: one devices read per running sidecar, a
    tags write only when something is wrong."""
    if not _ts_token():
        return
    running = running_names()
    names = sorted(set(deployed_services()) | (CONTROLLER_PODS & running))
    for name in names:
        if f"tailscale-{name}" in running:
            ts_tag_sidecar(name, attempts=2)
        else:
            # No live sidecar: nothing to tag, and "missing" would be
            # noise on a deliberately stopped pod.
            _record_tag_state(name, "unknown")


def ts_set_public(name, public):
    """Add/remove tag:tailarr-public on a pod's sidecar so the funnel
    nodeAttr applies. Returns an error string or None."""
    if not _ts_token():
        return ("no Tailscale API token on the controller — the funnel "
                "nodeAttr was not updated; public exposure will be refused "
                "by tailscaled")
    d = _ts_find_device(name)
    if not d:
        return f"sidecar '{name}' not found in the tailnet"
    tags = set(d.get("tags") or [])
    new = (tags | {"tag:tailarr-public"}) if public \
        else (tags - {"tag:tailarr-public"})
    if new == tags:
        return None
    code, resp = ts_api("POST", f"/device/{d['nodeId']}/tags",
                        {"tags": sorted(new)})
    if code != 200:
        return f"tags API: {resp}"
    return None


def op_user_key():
    """Mint a single-use, preauthorized tag:tailarr-user auth key (24h TTL).
    Devices enrolling with it appear on the Users page with zero access."""
    if not _ts_token():
        return {"ok": False, "error": "no API token configured", "key": ""}
    code, resp = ts_api("POST", "/tailnet/-/keys", {
        "capabilities": {"devices": {"create": {
            "reusable": False, "ephemeral": False, "preauthorized": True,
            "tags": [TS_USER_TAG]}}},
        "expirySeconds": 86400,
        "description": "tailarr user enrollment",
    })
    if code != 200:
        return {"ok": False, "error": f"keys API: {resp}", "key": ""}
    return {"ok": True, "error": None, "key": resp.get("key", "")}


def ts_mint_pod_key(name):
    """Mint a preauthorized, single-use auth key for a pod's sidecar.

    Tagged tag:tailarr only: the base tag is in the policy's tagOwners from
    day one, while tag:tailarr-svc-<name> enters tagOwners only during the
    post-install policy sync — minting with the svc tag would be a
    chicken-and-egg failure on first deploy. The sidecar gains its svc tag
    right after start via ts_tag_sidecar() (devices API). Non-ephemeral so
    the node survives restarts; 7-day TTL covers installs that aren't
    started immediately (once enrolled, ./tailscale/ state replaces it)."""
    if not _ts_token():
        return {"ok": False, "error": "no API token configured", "key": ""}
    code, resp = ts_api("POST", "/tailnet/-/keys", {
        "capabilities": {"devices": {"create": {
            "reusable": False, "ephemeral": False, "preauthorized": True,
            "tags": ["tag:tailarr"]}}},
        "expirySeconds": 7 * 86400,
        "description": f"tailarr pod {name}",
    })
    if code != 200:
        return {"ok": False, "error": f"keys API: {resp}", "key": ""}
    return {"ok": True, "error": None, "key": resp.get("key", "")}


def _write_secret(path, content):
    """Write a secret file created private (0600) from the first byte."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.chmod(path, 0o600)  # a pre-existing file keeps 0600 too


# =========================================================================
# API bearer tokens — the permission boundary behind tag:tailarr-can-server.
#
# The web UI's historical security model is pure network reachability
# (tailnet-only). Granting the controller to app users (the "server"
# pseudo-service) opens that pipe to non-admin devices, so the API gains an
# opt-in token gate: mint tokens on the Users/Settings page, then flip
# "require" — from then on every /api/* request needs "Authorization:
# Bearer <token>". Exempt: /api/info (self-upgrade health gate + the app's
# pre-auth compatibility probe; it leaks nothing sensitive) and /metrics
# (not under /api/, prometheus scrape). Secrets are stored as sha256
# hashes; the plaintext is shown exactly once at mint time.
# =========================================================================
TOKENS_FILE = os.path.join(PODS_DIR, ".tokens.json")


def load_tokens():
    try:
        with open(TOKENS_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"require": False, "tokens": []}
    if not isinstance(data, dict):
        return {"require": False, "tokens": []}
    return {"require": bool(data.get("require")),
            "tokens": [t for t in data.get("tokens") or []
                       if isinstance(t, dict)]}


def save_tokens(reg):
    _write_secret(TOKENS_FILE, json.dumps(reg, indent=2) + "\n")


def status_tokens():
    """Token list for the UI — ids/labels/timestamps only, never hashes."""
    reg = load_tokens()
    return {"require": reg["require"],
            "tokens": [{"id": t.get("id", ""), "label": t.get("label", ""),
                        "created": t.get("created", "")}
                       for t in reg["tokens"]]}


def op_token_create(label):
    """Mint an API token. The plaintext is returned ONCE and never stored."""
    label = (label or "").strip()[:60]
    plain = "tailarr-tok-" + secrets.token_hex(24)
    reg = load_tokens()
    tid = secrets.token_hex(4)
    reg["tokens"].append({
        "id": tid, "label": label,
        "sha256": hashlib.sha256(plain.encode()).hexdigest(),
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    save_tokens(reg)
    return {"ok": True, "error": None, "id": tid, "token": plain}


def op_token_delete(tid):
    reg = load_tokens()
    keep = [t for t in reg["tokens"] if t.get("id") != tid]
    if len(keep) == len(reg["tokens"]):
        return {"ok": False, "error": "No such token."}
    reg["tokens"] = keep
    if not keep and reg["require"]:
        reg["require"] = False  # never leave the API requiring what nobody has
    save_tokens(reg)
    return {"ok": True, "error": None}


def op_token_require(enabled):
    reg = load_tokens()
    if enabled and not reg["tokens"]:
        return {"ok": False, "error": "Create a token first — requiring "
                "auth with zero tokens would lock every client out, "
                "including this UI."}
    reg["require"] = bool(enabled)
    save_tokens(reg)
    return {"ok": True, "error": None}


def token_auth_ok(header):
    """Gate for /api/* requests: open until the operator flips require on,
    then a valid Bearer token is mandatory."""
    reg = load_tokens()
    if not reg["require"]:
        return True
    if not header or not header.startswith("Bearer "):
        return False
    digest = hashlib.sha256(header[7:].strip().encode()).hexdigest()
    return any(hmac.compare_digest(digest, t.get("sha256", ""))
               for t in reg["tokens"])


# =========================================================================
# Private registry credentials — pulls of private OCI images (e.g. GHCR).
#
# .registries.json (0600) is the source of truth: registry host -> username
# + secret (a GitHub PAT with read:packages for ghcr.io). Every change
# re-renders .registry-auth.json, a standard containers-auth file that
# podman AND skopeo read via the REGISTRY_AUTH_FILE env var — exported by
# every generated run.sh (pull-on-run), by the controller's podman()
# wrapper (the Update button's explicit pull), and by the skopeo
# update-digest check. Credentials are validated with a live `podman
# login` before saving; the API only ever returns host + username, never
# the secret.
# =========================================================================
REGISTRIES_FILE = os.path.join(PODS_DIR, ".registries.json")
REGISTRY_AUTH_FILE = os.path.join(PODS_DIR, ".registry-auth.json")

# Registry hosts are DNS names with an optional :port (ghcr.io,
# registry.example.com:5000). Anything else is a typo or an injection.
REGISTRY_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?$")


def load_registries():
    try:
        with open(REGISTRIES_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {h: e for h, e in data.items()
            if isinstance(e, dict) and e.get("username") and e.get("secret")}


def save_registries(reg):
    """Persist the credential store and re-render the podman authfile."""
    _write_secret(REGISTRIES_FILE, json.dumps(reg, indent=2) + "\n")
    render_registry_auth(reg)


def render_registry_auth(reg):
    """Render .registries.json into containers-auth format (the docker
    config.json "auths" shape podman/skopeo consume). No credentials ->
    no authfile, so run.sh scripts skip the export entirely."""
    if not reg:
        try:
            os.remove(REGISTRY_AUTH_FILE)
        except OSError:
            pass
        return
    auths = {}
    for host, entry in reg.items():
        raw = f"{entry['username']}:{entry['secret']}".encode()
        auths[host] = {"auth": base64.b64encode(raw).decode()}
    _write_secret(REGISTRY_AUTH_FILE, json.dumps({"auths": auths}, indent=2) + "\n")


def registry_env():
    """subprocess env for podman/skopeo calls: point them at the rendered
    authfile when one exists (both tools honor REGISTRY_AUTH_FILE)."""
    if os.path.exists(REGISTRY_AUTH_FILE):
        return {**os.environ, "REGISTRY_AUTH_FILE": REGISTRY_AUTH_FILE}
    return None


def status_registries():
    """Registry list for the UI — hosts and usernames only, never secrets."""
    return {"registries": [
        {"registry": h, "username": e.get("username", ""),
         "created": e.get("created", "")}
        for h, e in sorted(load_registries().items())]}


def _registry_login_probe(host, username, secret):
    """Validate a credential with a real `podman login` against the
    registry, into a throwaway authfile. Returns (ok, error)."""
    tmp = REGISTRY_AUTH_FILE + ".probe"
    try:
        r = subprocess.run(
            ["podman", "login", "--authfile", tmp,
             "--username", username, "--password-stdin", host],
            input=secret, capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, f"podman unavailable: {e}"
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if r.returncode != 0:
        return False, (r.stderr or "login failed").strip()[-300:]
    return True, None


def op_registry_save(data):
    """Validate, then add or replace one registry's credential."""
    host = (data.get("registry") or "").strip().lower()
    username = (data.get("username") or "").strip()
    secret = (data.get("secret") or "").strip()
    if not REGISTRY_HOST_RE.fullmatch(host):
        return {"ok": False, "error": "Registry must be a hostname like "
                "ghcr.io (no scheme, no path)."}
    if not username or not secret:
        return {"ok": False, "error": "Both a username and a token are needed."}
    ok, err = _registry_login_probe(host, username, secret)
    if not ok:
        return {"ok": False, "error": f"The registry rejected this "
                f"credential: {err}"}
    reg = load_registries()
    reg[host] = {"username": username, "secret": secret,
                 "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    save_registries(reg)
    return {"ok": True, "error": None}


def op_registry_delete(host):
    reg = load_registries()
    if host not in reg:
        return {"ok": False, "error": "No such registry."}
    del reg[host]
    save_registries(reg)
    return {"ok": True, "error": None}


# =========================================================================
# Accounts vault (Settings → Accounts) — saved provider accounts.
#
# The crisp boundary: ONLY accounts with OUTSIDE services that Tailarr
# cannot derive or extract — exactly the set of things a Magic Stack
# wizard would otherwise have to ask for (newznab indexers, usenet
# providers). Anything extractable from a pod (Arr keys, nzbget logins)
# stays extracted-live-never-stored. Secrets are write-only through the
# API: GET /api/accounts returns labels and public detail, never keys or
# passwords — the controller uses them server-side (stack wizards resolve
# {"account": id} references in _stack_inputs).
ACCOUNTS_FILE = os.path.join(PODS_DIR, ".accounts.json")


def load_accounts():
    try:
        with open(ACCOUNTS_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_accounts(acc):
    _write_secret(ACCOUNTS_FILE, json.dumps(acc, indent=2) + "\n")


def _account_detail(e):
    """The public half shown in lists — never the key/password."""
    if e.get("kind") == "usenet":
        return (("ssl://" if e.get("ssl", True) else "") + str(e.get("host", ""))
                + ":" + str(e.get("port", "")) + " · " + str(e.get("user", "")))
    return e.get("url", "")


def status_accounts():
    return {"accounts": [
        {"id": aid, "kind": e.get("kind", ""),
         "label": e.get("label", ""), "detail": _account_detail(e)}
        for aid, e in sorted(load_accounts().items(),
                             key=lambda kv: (kv[1].get("kind", ""),
                                             kv[1].get("label", "").lower()))]}


def _account_upsert(kind, label, fields):
    """Create or update by natural identity — an indexer IS its URL, a
    usenet account is host+user — so re-saving (from the card or a
    wizard's save-through) never piles up duplicates."""
    acc = load_accounts()

    def ident(e):
        # An indexer IS its host (paste shapes differ in path — /api or
        # bare — but it's the same indexer); a usenet account is
        # host+user (providers sell multiple accounts).
        return _account_host_label(e.get("url")) if kind == "newznab" \
            else (e.get("host"), e.get("user"))

    new = dict(fields)
    for aid, e in acc.items():
        if e.get("kind") == kind and ident(e) == ident(new):
            e.update(new)
            e["label"] = label or e.get("label", "")
            save_accounts(acc)
            return aid
    aid = secrets.token_hex(4)
    acc[aid] = {"kind": kind, "label": label, **new,
                "created": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime())}
    save_accounts(acc)
    return aid


def _account_host_label(url_or_host):
    return re.sub(r"^https?://", "", str(url_or_host or "")).split("/")[0]


def op_account_save(data):
    """Validate LIVE, then save — the private-registries contract: prove
    the account works before storing it, so the vault only ever holds
    known-good entries."""
    kind = (data.get("kind") or "").strip()
    label = _clean(data.get("label"), 60)
    if kind == "newznab":
        url = _indexer_base(data.get("url"))
        key = _clean(data.get("key")) or _indexer_pasted_key(data.get("url"))
        err = _validate_newznab(url, key)
        if err:
            return {"ok": False, "error": err}
        aid = _account_upsert(kind, label or _account_host_label(url),
                              {"url": url, "key": key})
        return {"ok": True, "error": None, "id": aid,
                "status": status_accounts()}
    if kind == "usenet":
        host, embedded_port = _usenet_host(data.get("host"))
        ssl_on = bool(data.get("ssl", True))
        port = embedded_port or data.get("port") or (563 if ssl_on else 119)
        user = _clean(data.get("user"), 120)
        password = _clean(data.get("password"), 120)
        err = _validate_usenet(host, port, ssl_on, user, password)
        if err:
            return {"ok": False, "error": err}
        aid = _account_upsert(kind, label or host,
                              {"host": host, "port": port, "ssl": ssl_on,
                               "user": user, "password": password})
        return {"ok": True, "error": None, "id": aid,
                "status": status_accounts()}
    return {"ok": False, "error": "Unknown account type."}


def op_account_delete(data):
    acc = load_accounts()
    aid = (data.get("id") or "").strip()
    if aid not in acc:
        return {"ok": False, "error": "No such account."}
    del acc[aid]
    save_accounts(acc)
    return {"ok": True, "error": None, "status": status_accounts()}


def _account_resolved(section, kind):
    """A wizard slot may reference a saved account ({"account": id})
    instead of carrying raw fields — resolve it so validate and install
    both see plain inputs (and the secret never round-trips through the
    browser). Returns (fields, error)."""
    section = section or {}
    ref = (section.get("account") or "").strip() \
        if isinstance(section.get("account"), str) else ""
    if not ref:
        return section, None
    e = load_accounts().get(ref)
    if not e or e.get("kind") != kind:
        return {}, ("That saved account is gone — pick another or enter "
                    "the details.")
    return e, None


# =========================================================================
# API-credential wizard (Settings) — validate, save, and adopt the policy.
#
# The first API-requiring action (user adoption, key minting, service
# deploy without a pasted key) on a fresh install has nowhere to get its
# credential from; these ops back the guided in-UI wizard that creates it.
# Secrets are validated live (read-only calls), written 0600, and never
# logged or echoed back.
# =========================================================================
FENCE_SECTIONS = ("grants", "tagowners", "nodeattrs")


def _tsapi_cfg_from(data):
    """Whitelist a credential dict from a request body (nothing else is
    ever persisted). Returns {} when no usable credential is present."""
    token = (data.get("token") or "").strip()
    if token:
        return {"token": token}
    cid = (data.get("oauth_client_id") or "").strip()
    secret = (data.get("oauth_client_secret") or "").strip()
    if cid and secret:
        return {"oauth_client_id": cid, "oauth_client_secret": secret}
    return {}


def status_tsapi():
    """Credential presence/shape only — no network calls (cheap to poll)."""
    try:
        with open(TSAPI_FILE) as f:
            cfg = json.load(f)
    except OSError:
        return {"configured": False, "mode": None, "error": None}
    except ValueError:
        return {"configured": False, "mode": None,
                "error": ".tsapi.json exists but is not valid JSON"}
    if (cfg.get("token") or "").strip():
        return {"configured": True, "mode": "token", "error": None}
    if ((cfg.get("oauth_client_id") or "").strip()
            and (cfg.get("oauth_client_secret") or "").strip()):
        return {"configured": True, "mode": "oauth", "error": None}
    return {"configured": False, "mode": None,
            "error": ".tsapi.json exists but holds neither a token nor an "
                     "OAuth client"}


def _fence_sections_present(text):
    """The managed sections whose begin marker appears in a policy text."""
    found = set()
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith(FENCE_BEGIN):
            found.add(s[len(FENCE_BEGIN):].strip())
    return found & set(FENCE_SECTIONS)


def _snip(data):
    return str(data)[:200]


def op_tsapi_validate(data):
    """Live-validate a credential (from the request, or the saved one when
    the request carries none) with READ-ONLY calls, reporting pass/fail per
    capability the controller needs — devices, auth_keys, policy_file —
    plus whether the policy already has the tailarr-managed fence markers.
    """
    cfg = _tsapi_cfg_from(data)
    if not cfg:
        try:
            with open(TSAPI_FILE) as f:
                cfg = _tsapi_cfg_from(json.load(f))
        except (OSError, ValueError):
            cfg = {}
    if not cfg:
        return {"ok": False, "mode": None, "checks": {}, "fences": None,
                "error": "No credential: provide an OAuth client id+secret "
                         "or an API access token."}
    token, mode, err = _cred_token(cfg)
    if err:
        return {"ok": False, "mode": mode, "checks": {}, "fences": None,
                "error": err}

    checks = {}
    code, resp = _ts_api_with(token, "GET", "/tailnet/-/devices")
    checks["devices"] = {
        "ok": code == 200,
        "detail": None if code == 200 else f"GET /devices -> {code}: "
        + _snip(resp)}
    code, resp = _ts_api_with(token, "GET", "/tailnet/-/keys")
    checks["auth_keys"] = {
        "ok": code == 200,
        "detail": None if code == 200 else f"GET /keys -> {code}: "
        + _snip(resp)}
    code, resp = _ts_api_with(token, "GET", "/tailnet/-/acl")
    checks["policy_file"] = {
        "ok": code == 200,
        "detail": None if code == 200 else f"GET /acl -> {code}: "
        + _snip(resp)}
    fences = None
    if checks["policy_file"]["ok"]:
        text = resp if isinstance(resp, str) else json.dumps(resp)
        found = _fence_sections_present(text)
        fences = {"present": sorted(found),
                  "missing": sorted(set(FENCE_SECTIONS) - found)}
    failed = sorted(k for k, c in checks.items() if not c["ok"])
    return {"ok": not failed, "mode": mode, "checks": checks,
            "fences": fences,
            "error": None if not failed else
            "The credential is missing write scope(s) or was rejected for: "
            + ", ".join(failed) + ". Recreate the OAuth client with "
            "Devices/Core, Auth Keys and Policy File write scopes."}


def op_tsapi_save(data):
    """Validate, then persist the credential to .tsapi.json (0600).
    Rejects credentials that fail any capability check."""
    cfg = _tsapi_cfg_from(data)
    if not cfg:
        return {"ok": False, "saved": False, "mode": None, "checks": {},
                "fences": None,
                "error": "Provide an OAuth client id+secret or an API "
                         "access token."}
    probe = op_tsapi_validate(cfg)
    if not probe["ok"]:
        probe["saved"] = False
        return probe
    _write_secret(TSAPI_FILE, json.dumps(cfg, indent=2) + "\n")
    with _oauth_lock:  # a fresh credential invalidates any cached token
        _oauth_cache["token"], _oauth_cache["exp"] = "", 0.0
    probe["saved"] = True
    return probe


def _insert_fence_markers(text, missing):
    """Insert empty marker pairs for missing managed sections into a policy.

    Same line-splicing spirit as _splice_fences: the marker pair lands just
    inside the section's container ('"grants": [' / '"tagOwners": {' /
    '"nodeAttrs": ['); an absent container is created before the policy's
    final closing brace (trailing commas are legal HuJSON). Everything the
    human wrote survives byte-for-byte. Raises ValueError (fail closed) on
    any layout this cannot handle safely."""
    keymap = {"grants": ("grants", "[", "],"),
              "tagowners": ("tagOwners", "{", "},"),
              "nodeattrs": ("nodeAttrs", "[", "],")}
    lines = text.splitlines()
    for sec in missing:
        key, opener, closer = keymap[sec]
        pat = re.compile(r'^\s*"' + key + r'"\s*:\s*' + re.escape(opener))
        idx = next((i for i, ln in enumerate(lines) if pat.match(ln)), None)
        if idx is not None:
            indent = lines[idx][:len(lines[idx]) - len(lines[idx].lstrip())]
            lines[idx + 1:idx + 1] = [indent + "\t" + FENCE_BEGIN + sec,
                                      indent + "\t" + FENCE_END + sec]
            continue
        if any(re.search(r'"' + key + r'"\s*:', ln) for ln in lines):
            raise ValueError(
                f'"{key}" exists but not as \'"{key}": {opener}\' on one '
                "line — add the fence markers to it by hand")
        close = max((i for i, ln in enumerate(lines) if ln.strip() == "}"),
                    default=None)
        if close is None:
            raise ValueError("could not find the policy's closing brace")
        lines[close:close] = [f'\t"{key}": {opener}',
                              "\t\t" + FENCE_BEGIN + sec,
                              "\t\t" + FENCE_END + sec,
                              "\t" + closer]
    return "\n".join(lines) + "\n"


def op_policy_init_fences():
    """The adopt path: ensure the tailnet policy carries all three
    tailarr-managed fenced regions, then sync their contents. Without this,
    _splice_fences fails closed ('managed sections missing') on tailnets
    that never ran the manual adopt. Returns {ok, added, error}."""
    added = []
    with _policy_lock:
        for _attempt in range(2):
            code, raw, etag = _ts_acl("GET")
            if code != 200:
                return {"ok": False, "added": [], "error": f"acl GET: {raw}"}
            missing = sorted(set(FENCE_SECTIONS)
                             - _fence_sections_present(raw))
            if not missing:
                break
            try:
                new_text = _insert_fence_markers(raw, missing)
            except ValueError as e:
                return {"ok": False, "added": [],
                        "error": f"policy fences: {e}"}
            code, resp, _ = _ts_acl("POST", "/validate", new_text)
            if code != 200 or '"message"' in resp:
                return {"ok": False, "added": [],
                        "error": f"acl validate: {resp[:300]}"}
            try:  # keep the pre-adopt policy for one-call rollback
                with open(ACL_BACKUP_FILE + ".tmp", "w") as f:
                    f.write(raw)
                os.replace(ACL_BACKUP_FILE + ".tmp", ACL_BACKUP_FILE)
            except OSError:
                pass
            code, resp, _ = _ts_acl("POST", "", new_text, etag=etag)
            if code == 200:
                added = missing
                break
            if code != 412:
                return {"ok": False, "added": [],
                        "error": f"acl POST: {resp[:300]}"}
            # 412: someone else edited the policy — refetch and retry once
        else:
            return {"ok": False, "added": [],
                    "error": "acl POST: concurrent edits kept winning (412)"}
    # Fill the (possibly empty) fences from the deployed service list.
    sync = ts_policy_sync()
    return {"ok": sync["ok"], "added": added, "error": sync["error"]}


# =========================================================================
# Peer relay (apple/container installs) -- see docs/acl-design.md and the
# grant comment in _managed_sections. State machine: bootstrap records the
# platform (.host.json); a pre-flight decides whether the tailnet looks
# like the dedicated 1:1 tailnet the product assumes; only then is the
# relay grant auto-emitted (Settings has an explicit override either way).
# The Mac-side `tailscale set --relay-server-port` step lives in
# install-mac.sh — the controller can't reach macOS, so it verifies from
# its own sidecar and keeps nudging until traffic leaves DERP.
# =========================================================================
def load_relay():
    try:
        with open(RELAY_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_relay(data):
    tmp = RELAY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, RELAY_FILE)
    os.chmod(RELAY_FILE, 0o600)


def _relay_grant_wanted():
    """Pure disk read — _managed_sections() must stay deterministic and
    offline. enabled True/False is the user's explicit call; null (auto)
    follows the platform + pre-flight verdict."""
    r = load_relay()
    if r.get("enabled") is True:
        return True
    if r.get("enabled") is False:
        return False
    return (host_platform() == "apple-container"
            and bool((r.get("preflight") or {}).get("eligible")))


def ts_relay_preflight():
    """Read-only 'does this tailnet look dedicated to Tailarr?' checks.

    All must pass for auto-emission; any API failure is itself a reason
    (fail closed). The reasons are shown verbatim in the Settings banner,
    so they are written for the customer, not the log."""
    reasons, counts = [], {}
    fences_present = False
    if not _ts_token():
        reasons.append("No Tailscale API credential configured.")
        return {"eligible": False, "reasons": reasons, "counts": counts,
                "fences_present": False, "checked_at": int(time.time())}
    code, raw, _etag = _ts_acl("GET")
    if code != 200:
        reasons.append(f"Could not read the tailnet policy: {raw}")
    else:
        fences_present = (_fence_sections_present(raw)
                          == set(FENCE_SECTIONS))
        if not fences_present:
            reasons.append("The tailnet policy has not been adopted by "
                           "Tailarr (fence markers missing).")
        acl_lines = len([ln for ln in raw.splitlines() if ln.strip()])
        counts["acl_lines"] = acl_lines
        if acl_lines > RELAY_MAX_ACL_LINES:
            reasons.append("The tailnet policy is unusually large "
                           f"({acl_lines} lines) for a dedicated Tailarr "
                           "tailnet.")
    code, data = ts_api("GET", "/tailnet/-/devices")
    if code != 200:
        reasons.append(f"Could not list tailnet devices: {_snip(data)}")
    else:
        devs = data.get("devices", []) or []
        foreign = [d for d in devs
                   if not any(str(t).startswith("tag:tailarr")
                              for t in d.get("tags") or [])]
        users = {d.get("user") or "" for d in foreign}
        counts.update({"devices": len(devs),
                       "foreign_devices": len(foreign),
                       "foreign_users": len(users)})
        if len(foreign) > RELAY_MAX_FOREIGN_DEVICES:
            reasons.append(f"Found {len(foreign)} devices that are not "
                           "part of the Tailarr fleet (expected a "
                           "dedicated tailnet).")
        if len(users) > RELAY_MAX_FOREIGN_USERS:
            reasons.append(f"Devices belong to {len(users)} different "
                           "users — this doesn't look like a "
                           "single-admin tailnet.")
    return {"eligible": not reasons, "reasons": reasons, "counts": counts,
            "fences_present": fences_present,
            "checked_at": int(time.time())}


def _startup_relay_preflight():
    """Record a first pre-flight verdict on apple-container installs that
    have none — the platform where DERP fallback is a near-certainty and
    the auto-grant is designed to engage without a human. Never overwrites
    an existing verdict (recheck/enable own refreshes)."""
    if host_platform() != "apple-container":
        return
    r = load_relay()
    if r.get("preflight"):
        return
    r["preflight"] = ts_relay_preflight()
    save_relay(r)
    pf = r["preflight"]
    print("relay pre-flight (first boot): "
          + ("eligible" if pf.get("eligible")
             else "not eligible: " + "; ".join(pf.get("reasons") or [])))


def _relay_downgrade_after_reject(raw, resp):
    """Called from ts_policy_sync when /validate rejected a splice. If the
    relay grant was present AND a relay-free splice of the same policy
    validates cleanly, the grant is the culprit: downgrade one rung
    (autogroup:admin dst -> autogroup:member dst -> grant disabled) and
    return fresh sections for a retry. Returns None when the rejection is
    not the relay grant's fault (including: grant wasn't there)."""
    if not _relay_grant_wanted():
        return None
    try:
        probe = _splice_fences(raw, _managed_sections(relay_dst=""))
    except ValueError:
        return None
    code, presp, _ = _ts_acl("POST", "/validate", probe)
    if code != 200 or '"message"' in presp:
        return None  # broken with or without the grant — not ours
    r = load_relay()
    if not r.get("dst_fallback") and _legacy_relay_dst_in_use():
        r["dst_fallback"] = True
        print("peer relay: validate rejected autogroup:admin dst — "
              "retrying with autogroup:member")
    else:
        r["enabled"] = False
        r["decided_by"] = "auto-validate-reject"
        pf = r.setdefault("preflight", {})
        pf.setdefault("reasons", []).append(
            "The tailnet policy rejected the relay grant: " + resp[:200])
        pf["eligible"] = False
        print(f"peer relay: grant disabled after validate reject: {resp[:200]}")
    save_relay(r)
    return _managed_sections()


def _relay_ips_in_status(raw):
    """Tailnet IPs of relays CURRENTLY carrying traffic, from a `tailscale
    status --json` document (PeerRelay is "ip:port" or "[v6]:port")."""
    try:
        st = json.loads(raw)
    except ValueError:
        return set()
    ips = set()
    for p in (st.get("Peer") or {}).values():
        pr = p.get("PeerRelay") or ""
        if p.get("Active") and pr:
            ips.add(pr.rsplit(":", 1)[0].strip("[]"))
    return ips


def relay_verify():
    """Best-effort connectivity classification from the controller's own
    sidecar: are active peers reached via peer relay, directly, or DERP?
    Cached into .relay.json; the relay banner clears on peer-relay or
    direct (direct means the problem this feature exists for is absent).

    Doubles as the registry's verification pass. Registry states:
    - pending: registered, but the device isn't advertising relay
      capability — the enable command genuinely hasn't been run there.
    - ready: the device ADVERTISES as a relay server (it appears in the
      sidecar's `tailscale debug peer-relay-servers`) but no relayed
      traffic has been seen yet. No command nag — it's already enabled.
    - active: traffic was seen flowing through it (PeerRelay match).
    Relays seen carrying traffic that were never registered are
    auto-discovered in (e.g. the Mac set up by install-mac.sh)."""
    sidecar = f"tailscale-{_controller_name() or 'tailarr'}"
    r = podman("exec", sidecar, "tailscale", "status", "--json", timeout=20)
    seen = set()
    if r.returncode != 0:
        state, detail = "unknown", (r.stdout + r.stderr).strip()[:200]
    else:
        state, detail = _classify_ts_status(r.stdout)
        seen = _relay_ips_in_status(r.stdout)
        if state == "derp":
            # Old sidecar images (<1.86) can't use peer relays at all —
            # surface the version so "stuck on derp" is diagnosable.
            v = podman("exec", sidecar, "tailscale", "version", timeout=15)
            if v.returncode == 0 and v.stdout.split():
                detail = f"sidecar tailscale {v.stdout.split()[0]}"
    # Who's advertising relay capability right now (debug output — parse
    # defensively; on failure just don't move anyone between pending and
    # ready this pass).
    advertising = None
    a = podman("exec", sidecar, "tailscale", "debug", "peer-relay-servers",
               timeout=15)
    if a.returncode == 0:
        try:
            parsed = json.loads(a.stdout or "[]")
            if isinstance(parsed, list):
                advertising = {str(x) for x in parsed}
        except ValueError:
            pass
    rec = load_relay()
    now = int(time.time())
    relays = rec.setdefault("relays", {})
    for ip in seen:
        entry = relays.setdefault(
            ip, {"name": ip, "ip": ip, "added_at": now, "discovered": True})
        entry["status"] = "active"
        entry["verified_at"] = now
    if advertising is not None:
        for ip, entry in relays.items():
            if entry.get("status") == "active":
                continue  # traffic-proven — never demoted by a probe
            entry["status"] = "ready" if ip in advertising else "pending"
    rec["verified"] = {"state": state, "at": now, "detail": detail}
    save_relay(rec)
    return rec["verified"]


def _classify_ts_status(raw):
    """Map a `tailscale status --json` document to a connectivity state."""
    try:
        st = json.loads(raw)
    except ValueError:
        return "unknown", "unparseable tailscale status"
    states = set()
    for p in (st.get("Peer") or {}).values():
        if not p.get("Active"):
            continue
        if p.get("PeerRelay"):
            states.add("peer-relay")
        elif p.get("CurAddr"):
            states.add("direct")
        elif p.get("Relay"):
            states.add("derp")
    for s in ("peer-relay", "direct", "derp"):
        if s in states:
            return s, ""
    return "unknown", "no active peer connections to classify"


RELAY_DEFAULT_PORT = 40000


def _relay_command(port):
    """The one command a device must run LOCALLY to become relay-capable.
    There is no remote/API enablement (tailscale/tailscale#17791) — the
    controller authors the grant; the device owner runs this."""
    return f"tailscale set --relay-server-port={port}"


def status_relay():
    """Disk-only (cheap to poll); preflight/verify writes refresh it."""
    platform = host_platform()
    r = load_relay()
    pf = r.get("preflight") or {}
    port = int(r.get("port") or RELAY_DEFAULT_PORT)
    relays = [dict(e, id=rid)
              for rid, e in sorted((r.get("relays") or {}).items())]
    return {"platform": platform,
            # v0.15.0: the feature is offered on EVERY platform (DERP
            # fallback isn't apple/container-specific); `recommended`
            # keeps the strong nudge for the vmnet-NAT case.
            "applicable": True,
            "recommended": platform == "apple-container",
            "enabled": r.get("enabled"),
            "eligible": bool(pf.get("eligible")),
            "reasons": pf.get("reasons") or [],
            "counts": pf.get("counts") or {},
            "grant_active": _relay_grant_wanted(),
            "dst_fallback": bool(r.get("dst_fallback")),
            "mode": r.get("mode") or "global",
            "relays": relays,
            "global_relay": r.get("global_relay") or "",
            "pod_relays": r.get("pod_relays") or {},
            "port": port,
            "command": _relay_command(port),
            "verified": r.get("verified")
            or {"state": "unknown", "at": 0, "detail": ""}}


def status_relay_devices():
    """GET /api/relay/devices — relay CANDIDATES for the add-relay picker:
    every tailnet device that isn't part of the Tailarr fleet (a sidecar
    can't relay for itself). Tailscale has no relay-capability listing, so
    candidacy is just membership; capability is proven later by
    relay_verify() seeing traffic."""
    code, data = ts_api("GET", "/tailnet/-/devices")
    if code != 200:
        return {"ok": False, "error": _snip(data), "devices": []}
    out = []
    for d in data.get("devices", []) or []:
        if any(str(t).startswith("tag:tailarr")
               for t in d.get("tags") or []):
            continue
        ip4 = next((a for a in d.get("addresses") or [] if "." in a), "")
        if not ip4:
            continue
        out.append({"hostname": d.get("hostname") or "",
                    "name": (d.get("name") or "").split(".")[0],
                    "ip": ip4,
                    "os": d.get("os") or "",
                    "user": d.get("user") or ""})
    return {"ok": True, "error": None, "devices": out}


def _relay_result(ok, sync, error=None, **extra):
    out = {"ok": ok, "error": error or (sync or {}).get("error"),
           "relay": status_relay()}
    if sync is not None:
        out["sync"] = sync
    out.update(extra)
    return out


def _op_relay_add(data):
    """Register a relay device. The grant is authored by the next policy
    sync (if selected). CAPABILITY is always enabled on the device itself —
    `tailscale set --relay-server-port` has no remote/API form
    (tailscale/tailscale#17791) — so the command is returned for the user
    to run there; entries start pending and graduate to active only when
    relay_verify() sees traffic through them. (v0.15.2 removed the
    host-exec special case that ran the command on the podman host: the
    inner host shares the sidecars' NAT position on nested installs, so
    it was the wrong machine to nominate.)"""
    ip = (data.get("ip") or "").strip()
    name = (data.get("name") or "").strip() or ip
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
        return {"ok": False, "relay": status_relay(),
                "error": "Pick a device from the list or enter its tailnet "
                         "IPv4 address (100.x.y.z)."}
    r = load_relay()
    port = int(r.get("port") or RELAY_DEFAULT_PORT)
    relays = r.setdefault("relays", {})
    entry = relays.setdefault(ip, {"ip": ip, "added_at": int(time.time())})
    entry["name"] = name
    entry.setdefault("status", "pending")
    # First relay in global mode becomes THE relay — the obvious intent.
    if (r.get("mode") or "global") == "global" and not r.get("global_relay"):
        r["global_relay"] = ip
    save_relay(r)
    return _relay_result(True, ts_policy_sync(),
                         command=_relay_command(port))


def _op_relay_remove(data):
    rid = (data.get("id") or "").strip()
    r = load_relay()
    if rid not in (r.get("relays") or {}):
        return {"ok": False, "relay": status_relay(),
                "error": "No such relay."}
    del r["relays"][rid]
    if r.get("global_relay") == rid:
        r["global_relay"] = ""
    r["pod_relays"] = {k: v for k, v in (r.get("pod_relays") or {}).items()
                       if v != rid}
    save_relay(r)
    return _relay_result(True, ts_policy_sync())


def _op_relay_select(data):
    """set-global {id} / set-pod {pod, id} — id "" clears the selection
    (global falls back to the legacy autogroup grant; a pod simply has no
    relay). Selections drive which cap grants the splice emits."""
    r = load_relay()
    rid = (data.get("id") or "").strip()
    if rid and rid not in (r.get("relays") or {}):
        return {"ok": False, "relay": status_relay(),
                "error": "No such relay."}
    if data.get("do") == "set-global":
        r["global_relay"] = rid
    else:
        pod = (data.get("pod") or "").strip()
        if pod != "server" and pod not in deployed_services():
            return {"ok": False, "relay": status_relay(),
                    "error": "Unknown service."}
        pr = r.setdefault("pod_relays", {})
        if rid:
            pr[pod] = rid
        else:
            pr.pop(pod, None)
    save_relay(r)
    return _relay_result(True, ts_policy_sync())


def op_relay(do, data=None):
    """POST /api/relay — enable/disable are the user's explicit override
    (recorded as such); recheck reruns pre-flight + sidecar verification;
    the rest manage the relay registry and selections (v0.15.0)."""
    data = data or {}
    if do == "enable":
        r = load_relay()
        r["preflight"] = ts_relay_preflight()  # record, but don't gate on it
        r["enabled"] = True
        r["decided_by"] = "user"
        save_relay(r)
        sync = ts_policy_sync()
        return _relay_result(sync["ok"], sync)
    if do == "disable":
        r = load_relay()
        r["enabled"] = False
        r["decided_by"] = "user"
        save_relay(r)
        sync = ts_policy_sync()  # splice drops the grant
        return _relay_result(sync["ok"], sync)
    if do == "recheck":
        r = load_relay()
        r["preflight"] = ts_relay_preflight()
        save_relay(r)
        relay_verify()
        sync = ts_policy_sync()  # a newly-eligible auto grant lands now
        return _relay_result(True, sync)
    if do == "mode":
        mode = (data.get("mode") or "").strip()
        if mode not in ("global", "per-pod"):
            return {"ok": False, "relay": status_relay(),
                    "error": "mode must be 'global' or 'per-pod'"}
        r = load_relay()
        r["mode"] = mode
        save_relay(r)
        return _relay_result(True, ts_policy_sync())
    if do == "add-relay":
        return _op_relay_add(data)
    if do == "remove-relay":
        return _op_relay_remove(data)
    if do in ("set-global", "set-pod"):
        return _op_relay_select(data)
    return {"ok": False, "error": f"unknown do '{do}'"}


# =========================================================================
# Core operations -- pure logic returning result dicts (no HTML)
# =========================================================================
def status_pods():
    """List deployed pods with their runtime state and saved metadata."""
    ps = ps_all()
    updates = load_updates().get("images", {})
    out = []
    for name in deployed_services():
        info = pod_config(name) or {}
        image = info.get("image", "")
        out.append({
            "name": name,
            "state": pod_state(name, ps),
            "controller": name in CONTROLLER_PODS,
            "system": _is_system(name),
            "image": image,
            "tailscale": info.get("include_tailscale") == "yes",
            "https": info.get("include_https") == "yes",
            "shares": info.get("shares", []),
            "update": bool(updates.get(image, {}).get("update")),
            "busy": pod_busy(name),
            # Identity-tag health (see ts_tag_sidecar): "missing" means the
            # sidecar lacks its tag:tailarr-svc-* — every user device is
            # being dropped at the packet filter even though the service
            # itself is healthy. Never let that look fully green.
            "identity": _tag_state.get(name, "unknown"),
        })
    return out


def status_catalog():
    """The installable service catalog (built-in + sources), flagged with
    what's deployed and where each entry came from."""
    deployed = set(deployed_services())
    merged, _ = all_services()
    ps = ps_all() if deployed else {}
    out = []
    for name, spec in sorted(merged.items()):
        # System-pod entries (ntfy) never appear in the catalog: their
        # feature page installs and manages them (single control
        # surface). resolve_service still finds them for that path.
        if spec.get("system"):
            continue
        installed = name in deployed
        out.append({
            "name": name,
            "image": spec.get("image", ""),
            "ports": spec.get("ports", {}),
            "port": next(iter(spec.get("ports", {})), ""),
            "environment": spec.get("environment", {}),
            "volumes": spec.get("volumes", {}),
            "command": spec.get("command", ""),
            "system": bool(spec.get("system")),
            "installed": installed,
            "state": pod_state(name, ps) if installed else "",
            "source": spec.get("_source", "built-in"),
        })
    return out


def status_sources():
    """Registered catalog sources, each with its service count / fetch error."""
    out = []
    for name, s in sorted(load_sources().items()):
        services, err = fetch_catalog(s["url"])
        out.append({
            "name": name, "url": s["url"],
            "service_count": len(services), "error": err,
        })
    return out


def status_catalogs():
    """Built-in category catalogs with their enabled state + entry counts."""
    enabled = load_enabled_catalogs()
    out = []
    for cat in BUILTIN_CATALOGS:
        count = len(_catalog_file_services(
            os.path.join(CATALOGS_DIR, cat["key"] + ".js")))
        out.append({**cat, "enabled": cat["key"] in enabled,
                    "service_count": count})
    return out


def op_custompods(data):
    """POST /api/custompods {do: save|delete, name, image, command, ports,
    environment, volumes} — author/remove entries in the "custom" catalog
    source. Deleting an entry never touches a deployed pod (remove that
    from the Dashboard/Catalog like any other)."""
    do = (data.get("do") or "save").strip()
    name = (data.get("name") or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name or ""):
        return {"ok": False, "name": name,
                "error": "Name must be lowercase letters, digits, dashes."}
    pods_ = load_custompods()
    if do == "delete":
        if name not in pods_:
            return {"ok": False, "name": name,
                    "error": "No such custom pod."}
        del pods_[name]
        save_custompods(pods_)
        return {"ok": True, "name": name, "error": None}
    if do != "save":
        return {"ok": False, "name": name, "error": f"unknown do '{do}'"}
    image = (data.get("image") or "").strip()
    if not image:
        return {"ok": False, "name": name, "error": "An image is required."}
    if name in load_services():
        return {"ok": False, "name": name,
                "error": "That name belongs to a built-in catalog service."}
    pods_[name] = {
        "name": name,
        "image": image,
        "command": (data.get("command") or "").strip(),
        "ports": data.get("ports") or {},
        "environment": data.get("environment") or {},
        "volumes": data.get("volumes") or {},
        "network_mode": "bridge",
        "restart_policy": "unless-stopped",
        "created": pods_.get(name, {}).get("created") or int(time.time()),
    }
    save_custompods(pods_)
    return {"ok": True, "name": name, "error": None}


def op_catalog_set(key, enabled):
    if key not in {c["key"] for c in BUILTIN_CATALOGS}:
        return {"ok": False, "key": key, "error": "Unknown catalog."}
    keys = load_enabled_catalogs()
    if enabled:
        keys.add(key)
    else:
        keys.discard(key)
    save_enabled_catalogs(keys)
    return {"ok": True, "key": key, "error": None}


def _parse_mem(val):
    """A podman-stats memory field -> bytes. Accepts a number, a bare size
    ("4GB"), or a usage/limit pair ("123.4MB / 4GB") — takes the left side."""
    if isinstance(val, (int, float)):
        return int(val)
    if not isinstance(val, str):
        return 0
    s = val.split("/")[0].strip()
    units = {"kB": 1e3, "KiB": 1024, "MB": 1e6, "MiB": 1 << 20,
             "GB": 1e9, "GiB": 1 << 30, "B": 1}
    for u, mult in units.items():
        if s.endswith(u):
            try:
                return int(float(s[: -len(u)]) * mult)
            except ValueError:
                return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def collect_stats():
    """One structured, side-effect-free snapshot of live per-container
    resources from a single `podman stats` pass. The SINGLE source for both
    /metrics (Prometheus text) and /api/stats (SPA JSON) — kept pure so a
    future maintenance-loop sampler can call it on a timer and append to a
    ring buffer without duplicating this parsing (CLAUDE.md backlog item 9).

    Returns {"at": epoch, "containers": {name: {"cpu_percent": float,
    "mem_bytes": int, "mem_limit_bytes": int}}}."""
    r = podman("stats", "--no-stream", "--format", "json", timeout=30)
    containers = {}
    if r.returncode == 0:
        try:
            rows = json.loads(r.stdout or "[]")
        except ValueError:
            rows = []
        for row in rows:
            cname = row.get("name") or row.get("Name") or ""
            if not cname:
                continue
            cpu = str(row.get("cpu_percent") or row.get("CPUPerc")
                      or "0").rstrip("%") or "0"
            try:
                cpu = float(cpu)
            except ValueError:
                cpu = 0.0
            raw_mem = row.get("mem_usage") or row.get("MemUsage") or 0
            # the "/ 4GB" side of MemUsage, when present, is the cgroup limit
            limit = (_parse_mem(raw_mem.split("/", 1)[1])
                     if isinstance(raw_mem, str) and "/" in raw_mem else 0)
            containers[cname] = {"cpu_percent": cpu,
                                 "mem_bytes": _parse_mem(raw_mem),
                                 "mem_limit_bytes": limit}
    return {"at": int(time.time()), "containers": containers}


def status_stats():
    """Per-pod live resource view for the Stats page: CPU% and memory for
    each pod's app container + its tailscale-<svc> sidecar, from one podman
    stats pass, plus fleet totals.

    LIVE ONLY today (no stored history). The snapshot shape (an `at`
    timestamp + per-pod gauges) is deliberately history-ready: a future
    sampler can persist collect_stats() into a ring buffer and this endpoint
    can grow a `series` field with no client rewrite (CLAUDE.md item 9)."""
    snap = collect_stats()
    cs = snap["containers"]
    ps = ps_all()
    pods, tot_cpu, tot_mem = [], 0.0, 0
    for name in deployed_services():
        app_c = cs.get(name)
        side = cs.get("tailscale-" + name)
        cpu = round((app_c or {}).get("cpu_percent", 0.0)
                    + (side or {}).get("cpu_percent", 0.0), 2)
        mem = (app_c or {}).get("mem_bytes", 0) + (side or {}).get("mem_bytes", 0)
        tot_cpu, tot_mem = tot_cpu + cpu, tot_mem + mem
        pods.append({
            "name": name,
            "label": _display_name(name),
            "state": pod_state(name, ps),
            "cpu_percent": cpu,
            "mem_bytes": mem,
            "mem_limit_bytes": (app_c or {}).get("mem_limit_bytes", 0),
            "app": app_c,
            "sidecar": side,
        })
    return {"at": snap["at"],
            "pods": pods,
            "totals": {"cpu_percent": round(tot_cpu, 2),
                       "mem_bytes": tot_mem,
                       "pods": len(pods),
                       "running": sum(1 for p in pods
                                      if p["state"] == "running")}}


def render_metrics():
    """Prometheus exposition for the fleet: state, flags, backups, and live
    CPU/mem from one `podman stats` pass (app container + sidecar per pod).
    Scraped by the observability catalog's Prometheus at /metrics."""
    lines = [
        "# HELP tailarr_pod_up 1 when the pod's main container is running.",
        "# TYPE tailarr_pod_up gauge",
        "# HELP tailarr_pod_update_available 1 when a newer image was seen.",
        "# TYPE tailarr_pod_update_available gauge",
        "# HELP tailarr_pod_public 1 when exposed via Tailscale Funnel.",
        "# TYPE tailarr_pod_public gauge",
        "# HELP tailarr_pod_backup_age_seconds seconds since newest snapshot.",
        "# TYPE tailarr_pod_backup_age_seconds gauge",
    ]
    ps = ps_all()
    updates = load_updates().get("images", {})
    backups = load_backups()
    now = time.time()
    for name in deployed_services():
        info = pod_config(name) or {}
        up = 1 if pod_state(name, ps) == "running" else 0
        lines.append(f'tailarr_pod_up{{pod="{name}"}} {up}')
        upd = 1 if updates.get(info.get("image", ""), {}).get("update") else 0
        lines.append(f'tailarr_pod_update_available{{pod="{name}"}} {upd}')
        pub = 1 if info.get("funnel") == "yes" else 0
        lines.append(f'tailarr_pod_public{{pod="{name}"}} {pub}')
        snaps = backups.get(name, [])
        if snaps:
            newest = max(s["ts"] for s in snaps)
            age = now - time.mktime(time.strptime(newest, "%Y%m%d-%H%M%S"))
            lines.append(
                f'tailarr_pod_backup_age_seconds{{pod="{name}"}} {int(age)}')
    # live resources: one stats pass covers app containers and sidecars
    containers = collect_stats()["containers"]
    if containers:
        lines += [
            "# HELP tailarr_container_cpu_percent live CPU percent.",
            "# TYPE tailarr_container_cpu_percent gauge",
            "# HELP tailarr_container_mem_bytes live memory usage.",
            "# TYPE tailarr_container_mem_bytes gauge",
        ]
    for cname, c in containers.items():
        lines.append(
            f'tailarr_container_cpu_percent{{container="{cname}"}} '
            f'{c["cpu_percent"]}')
        lines.append(
            f'tailarr_container_mem_bytes{{container="{cname}"}} '
            f'{c["mem_bytes"]}')
    return "\n".join(lines) + "\n"


def status_shares():
    """Defined shares, each with mode/visibility and the pods using it."""
    shares = load_shares()
    usage = {}
    for pod in deployed_services():
        for sname in (pod_config(pod) or {}).get("shares", []):
            usage.setdefault(sname, []).append(pod)
    out = []
    for name, s in sorted(shares.items()):
        out.append({
            "name": name,
            "host_path": s["host_path"],
            "container_path": s["container_path"],
            "ro": bool(s.get("ro")),
            "mode": "read-only" if s.get("ro") else "read-write",
            "visible": os.path.isdir(s["host_path"]),
            "used_by": usage.get(name, []),
            "nfs": s.get("nfs") or None,
        })
    return out


def op_install(req):
    """Generate a pod from an install request.

    req: name, custom(bool), image, command, ports, environment, volumes,
         shares(list of names), authkey, restart_policy.
    Every pod enrolls as its own Tailscale node (HTTPS via serve when it has a
    port), so an auth key is always required unless the pod already has
    Tailscale state. Returns {ok, name, error, output}. error set => rejected
    before the engine; ok False with output set => create.sh failed.
    """
    name = (req.get("name") or "").strip()
    custom = bool(req.get("custom"))
    image = (req.get("image") or "").strip()

    if custom:
        if not NAME_RE.fullmatch(name):
            return {"ok": False, "name": name, "error": "Invalid name (a-z, 0-9, dashes).", "output": ""}
        if not image:
            return {"ok": False, "name": name, "error": "An image is required.", "output": ""}

    # Tailscale is mandatory: resolve an auth key. Priority: pasted key
    # (manual override) > existing key file > auto-mint via the keys API
    # when a credential is configured. A pod that already carries enrolled
    # state in ./tailscale/ tolerates a spent key file.
    auth_key_file = os.path.join(PODS_DIR, name, ".tailscale_authkey")
    pasted = (req.get("authkey") or "").strip()
    if pasted:
        _write_secret(auth_key_file, pasted + "\n")
    elif not os.path.isfile(auth_key_file):
        if _ts_token():
            mint = ts_mint_pod_key(name)
            if not mint["ok"]:
                return {"ok": False, "name": name,
                        "error": "Couldn't mint an auth key automatically "
                                 f"({mint['error']}). Paste a key manually, or "
                                 "fix the API credential under Settings.",
                        "output": ""}
            _write_secret(auth_key_file, mint["key"] + "\n")
        else:
            return {"ok": False, "name": name,
                    "error": "An auth key is required — every pod enrolls as "
                             "its own Tailscale node. Paste one, or configure "
                             "the Tailscale API credential (Settings) so "
                             "Tailarr mints keys automatically.",
                    "output": ""}

    volumes = dict(req.get("volumes") or {})
    reg = load_shares()
    attached = []
    for sname in req.get("shares") or []:
        share = reg.get(sname)
        if share:
            cpath, host = share_volume(share)
            volumes[cpath] = host
            attached.append(sname)

    # The engine (parse-service-config.sh) forces Tailscale on, derives HTTPS
    # from the presence of a port, and sets the sidecar network mode itself;
    # these fields are here for a coherent saved config only.
    config = {
        "container": name,
        "image": image,
        "network_mode": f"service:tailscale-{name}",
        "ports": req.get("ports") or {},
        "restart_policy": req.get("restart_policy", "unless-stopped"),
        "include_tailscale": "yes",
        "include_https": "yes" if (req.get("ports") or {}) else "no",
        "auth_key_file": auth_key_file,
        "base_path": PODS_DIR,
        "environment": req.get("environment") or {},
        "volumes": volumes,
        "command": req.get("command", ""),
        "shares": sorted(attached),
        # Catalog-defined app-config seeding (applied once, after the
        # first start — see generate-run-template.sh).
        "config_file": req.get("config_file", ""),
        "config_set": req.get("config_set") or {},
    }
    result = run_create(config)
    output = result.stdout + result.stderr
    if result.returncode != 0:
        return {"ok": False, "name": name, "error": None, "output": output}
    # New service => its svc-/can- tags and grant line enter the policy's
    # managed regions (no-op without an API token; the install still works,
    # the pod just isn't shareable/taggable until the policy catches up).
    if _ts_token():
        sync = ts_policy_sync()
        if not sync["ok"]:
            output += f"\n[policy] {sync['error']}"
    return {"ok": True, "name": name, "error": None, "output": output}


def op_action(name, action):
    """start / stop / logs / update / remove a deployed pod. Returns a result dict."""
    if name not in deployed_services():
        return {"ok": False, "name": name, "action": action, "status": "error",
                "error": "Unknown service.", "output": ""}
    if name in CONTROLLER_PODS and action in ("stop", "remove"):
        return {"ok": False, "name": name, "action": action, "status": "refused",
                "error": f"Not {action.replace('stop', 'stopping').replace('remove', 'removing')}"
                         " the controller from itself.", "output": ""}

    if action == "logs":  # read-only: no need to claim the pod
        return _run_action(name, action)
    conflict = _op_begin(name, action)
    if conflict:
        return {"ok": False, "name": name, "action": action, "status": "busy",
                "error": f"{conflict} is already in progress for {name}.", "output": ""}
    try:
        return _run_action(name, action)
    finally:
        _op_end(name)


def _run_action(name, action):
    svc_dir = os.path.join(PODS_DIR, name)
    if action == "start":
        r = subprocess.run(["sh", "./run.sh"], cwd=svc_dir, capture_output=True,
                           text=True, timeout=600)
    elif action == "stop":
        r = subprocess.run(["sh", "./stop.sh"], cwd=svc_dir, capture_output=True,
                           text=True, timeout=120)
    elif action == "logs":
        r = podman("logs", "--tail", "100", name, timeout=30)
    elif action == "update":
        # Pull the current image tag, then recreate the pod from run.sh.
        info = pod_config(name)
        if not info or "image" not in info:
            return {"ok": False, "name": name, "action": action, "status": "error",
                    "error": "No .config.json for this pod (redeploy once to create it).",
                    "output": ""}
        pull = podman("pull", info["image"], timeout=600)
        if pull.returncode != 0:
            return {"ok": False, "name": name, "action": action, "status": "pull failed",
                    "error": "pull failed", "output": pull.stdout + pull.stderr}
        r = subprocess.run(["sh", "./run.sh"], cwd=svc_dir, capture_output=True,
                           text=True, timeout=600)
        if r.returncode == 0:
            mark_image_fresh(info["image"])
            notify_ops(f"{name} updated",
                       f"Pulled {info['image']} and restarted the pod.",
                       tags=["white_check_mark"])
        else:
            notify_ops(f"{name} update failed",
                       "The new image pulled but the pod did not restart "
                       "cleanly — check its logs.", priority="high",
                       tags=["rotating_light"])
    elif action == "remove":
        # Uninstall: stop + remove the containers, then delete the pod's
        # directory (config, data dirs, and Tailscale identity included).
        info_rm = pod_config(name) or {}
        r = subprocess.run(["sh", "./remove.sh"], cwd=svc_dir, capture_output=True,
                           text=True, timeout=300)
        if r.returncode == 0:
            shutil.rmtree(svc_dir, ignore_errors=True)
            # Removing Uptime Kuma orphans the Monitor tab's saved
            # credentials (a reinstall starts with a fresh admin account) —
            # drop them so the connect flow starts clean.
            if "uptime-kuma" in info_rm.get("image", ""):
                try:
                    os.remove(os.path.join(PODS_DIR, ".kuma.json"))
                except OSError:
                    pass
            # Removing ntfy invalidates every stored token (a reinstall
            # starts a fresh auth.db) — drop the registries so the
            # Notifications tab honestly reads "not configured".
            if NTFY_IMAGE in info_rm.get("image", ""):
                ntfy_client.clear()
    else:
        return {"ok": False, "name": name, "action": action, "status": "error",
                "error": "Unknown action.", "output": ""}

    output = r.stdout + r.stderr
    ok = r.returncode == 0
    if ok and action == "start":
        # Sidecar just (re)enrolled — make sure it wears its identity tags.
        threading.Thread(target=ts_tag_sidecar, args=(name,), daemon=True).start()
    if ok and action == "remove" and _ts_token():
        # Drop the service's grant line + tagOwners from the managed regions.
        # The tailnet NODE is deliberately not deleted: backups carry its
        # identity, and a reinstall under the same name reuses it.
        sync = ts_policy_sync()
        if not sync["ok"]:
            output += f"\n[policy] {sync['error']}"
    return {"ok": ok, "name": name, "action": action,
            "status": "ok" if ok else f"exit {r.returncode}",
            "error": None, "output": output}


def op_exec(name, cmd):
    """Run a one-shot shell command inside a pod's main container.

    Like logs, this is not claimed in the busy registry (it doesn't
    conflict with lifecycle ops) — but it refuses to run while one is
    mid-flight, since the container may be getting stopped or replaced.
    The command string is a single argv element to `sh -c` inside the
    container: arbitrary shell *inside* the pod is the feature; there is
    no host-side shell to inject into.
    """
    if name not in deployed_services():
        return {"ok": False, "name": name, "action": "exec", "status": "error",
                "error": "Unknown service.", "output": ""}
    if not isinstance(cmd, str) or not cmd.strip():
        return {"ok": False, "name": name, "action": "exec", "status": "error",
                "error": "Empty command.", "output": ""}
    if len(cmd) > 10000:
        return {"ok": False, "name": name, "action": "exec", "status": "error",
                "error": "Command too long.", "output": ""}
    busy = pod_busy(name)
    if busy:
        return {"ok": False, "name": name, "action": "exec", "status": "busy",
                "error": f"{busy} is already in progress for {name}.", "output": ""}
    r = podman("exec", name, "sh", "-c", cmd, timeout=30)
    output = r.stdout + r.stderr
    ok = r.returncode == 0
    return {"ok": ok, "name": name, "action": "exec",
            "status": "ok" if ok else f"exit {r.returncode}",
            "error": None, "output": output}


def _run_rerender(name, start=True):
    """Re-render one pod's scripts from its saved .config.json, then re-run
    it. This is how engine updates (new run.sh templates) reach existing
    pods — typically right after a controller upgrade. The pod's image,
    volumes, environment and Tailscale identity are all unchanged; run.sh
    recreates the containers, so expect a brief restart. start=False
    renders WITHOUT starting — the auto post-upgrade pass uses it so a
    deliberately-stopped pod gets fresh scripts but stays stopped."""
    info = pod_config(name)
    if not info:
        return {"ok": False, "name": name, "action": "rerender",
                "status": "error", "output": "",
                "error": "No .config.json for this pod."}
    result = run_create(config_from_info(info))
    if result.returncode != 0:
        return {"ok": False, "name": name, "action": "rerender",
                "status": "render failed", "error": "create.sh failed",
                "output": result.stdout + result.stderr}
    if not start:
        return {"ok": True, "name": name, "action": "rerender",
                "status": "rendered (left stopped)", "error": None,
                "output": result.stdout + result.stderr}
    r = _run_action(name, "start")
    r["action"] = "rerender"
    r["output"] = result.stdout + result.stderr + r.get("output", "")
    return r


RERENDER_MARKER = os.path.join(UPGRADE_DIR, "rerendered.json")


def _converge_notifications():
    """Self-heal the notification stack after a controller upgrade or
    restart, so the operator never has to re-run setup by hand: when
    notifications are configured, ensure the app self-config gateway is
    deployed — and running the CONTROLLER'S CURRENT image. The gateway
    runs our image with the selfconfig entrypoint; after an upgrade a
    stale copy wouldn't know new /self/* routes, so on image mismatch
    the saved config is repointed and the pod re-rendered in place
    (never remove+reinstall: that wipes its Tailscale identity and
    invites a hostname collision).

    Also retires the legacy "Alerts on your phone" credential (card
    removed in v0.27.0 — the Tailarr app gets alerts natively via the
    gateway, and person handouts cover the official ntfy app): the
    read-only tailarr-alerts account would otherwise linger with no UI
    left to revoke it. Kept on failure so a later start retries."""
    conf = ntfy_client.load_conf()
    if not conf:
        return
    if conf.get("alerts"):
        entry = _discover_ntfy(fresh=True)
        if entry:
            r = _ntfy_cli(entry["name"], "user", "del",
                          ntfy_client.ALERTS_USER)
            out = r.stdout + r.stderr
            if r.returncode == 0 or "exist" in out or "found" in out:
                conf.pop("alerts", None)
                ntfy_client.save_conf(conf)
                print("alerts converge: retired the legacy phone "
                      "credential (superseded by the Tailarr app)")
    if GATEWAY_POD not in deployed_services():
        err = _ensure_gateway()
        if err:
            print(f"gateway converge: {err}")
        else:
            print("gateway converge: redeployed the app self-config gateway")
        return
    info = pod_config(GATEWAY_POD) or {}
    image = _controller_image()
    if not image or not info.get("image") or info["image"] == image:
        return
    info["image"] = image
    try:
        with open(os.path.join(PODS_DIR, GATEWAY_POD, ".config.json"),
                  "w") as f:
            json.dump(info, f, indent=2)
    except OSError as e:
        print(f"gateway converge: {e}")
        return
    r = _run_rerender(GATEWAY_POD)
    print("gateway converge: moved the gateway to the controller image "
          f"({'ok' if r['ok'] else r.get('error') or r['status']})")


def _auto_rerender_after_upgrade():
    """One-shot fleet rerender after a successful controller upgrade.

    The upgrade flow warns UP FRONT that upgrading restarts running pods
    — then this does it automatically on the new controller's first
    start, so nobody has to find a "Finish upgrade" button (and headless
    API upgrades get engine updates too). Keyed on result.json's
    finished stamp: exactly once per upgrade outcome. Running pods
    restart on the new templates; stopped pods get fresh scripts but
    are NOT started; busy pods are skipped (the manual fleet rerender
    remains for stragglers)."""
    res = _upgrade_last_result()
    if not res or not res.get("ok") or res.get("rolled_back") \
            or not res.get("finished"):
        return
    try:
        with open(RERENDER_MARKER) as f:
            if (json.load(f) or {}).get("for") == res["finished"]:
                return
    except (OSError, ValueError):
        pass
    running = running_names()
    done, failed = [], []
    for name in deployed_services():
        if name in CONTROLLER_PODS:
            continue
        if _op_begin(name, "rerender"):
            continue  # mid-action from another request; skip, don't queue
        try:
            r = _run_rerender(name, start=name in running)
            (done if r["ok"] else failed).append(name)
        except Exception:  # noqa: BLE001 — one bad pod must not end the pass
            failed.append(name)
        finally:
            _op_end(name)
    try:
        with open(RERENDER_MARKER + ".tmp", "w") as f:
            json.dump({"for": res["finished"], "at": time.time(),
                       "ok": sorted(done), "failed": sorted(failed)}, f)
        os.replace(RERENDER_MARKER + ".tmp", RERENDER_MARKER)
    except OSError:
        pass
    print(f"post-upgrade rerender: {len(done)} ok, {len(failed)} failed")
    if failed:
        notify_ops("Post-upgrade refresh incomplete",
                   f"Refreshed {len(done)} pod(s); failed: "
                   + ", ".join(sorted(failed)), priority="high",
                   tags=["warning"])
    elif done:
        notify_ops("Upgrade complete",
                   f"Now running v{VERSION}; refreshed {len(done)} pod(s) "
                   "to the new engine.", tags=["white_check_mark"])


def op_fleet(action):
    """stop / start / restart / rerender every deployed pod except the
    controller.

    Claims all targets up front — so the whole fleet reads as busy in
    /api/pods while this request works through them sequentially — then
    releases each pod as it finishes. Pods already mid-action from another
    request are skipped, not queued. The controller pod is never touched:
    stopping it (and the podhost VM around it) is a host-side operation.
    """
    if action not in ("stop", "start", "restart", "rerender"):
        return {"ok": False, "action": action, "status": "error",
                "error": "Unknown fleet action.", "results": [], "skipped": []}
    running = running_names()
    targets, skipped = [], []
    for name in deployed_services():
        if name in CONTROLLER_PODS:
            continue
        # System pods are infrastructure like the controller: fleet
        # stop/start/restart leaves them alone (notifications must
        # survive "Stop all"). Rerender still includes them — they need
        # engine updates like everything else.
        if action != "rerender" and _is_system(name):
            continue
        # Skip no-ops so cards don't flash busy for nothing: a fully-down
        # pod has nothing to stop (stop.sh covers the sidecar too), and a
        # running pod needs no start.
        if action == "stop" and name not in running \
                and f"tailscale-{name}" not in running:
            continue
        if action == "start" and name in running:
            continue
        conflict = _op_begin(name, action)
        if conflict:
            skipped.append({"name": name, "busy": conflict})
        else:
            targets.append(name)
    results = []
    try:
        for name in targets:
            try:
                if action == "restart":
                    r = _run_action(name, "stop")
                    if r["ok"]:
                        r = _run_action(name, "start")
                    r["action"] = "restart"
                elif action == "rerender":
                    r = _run_rerender(name)
                else:
                    r = _run_action(name, action)
            except subprocess.TimeoutExpired as e:
                r = {"ok": False, "name": name, "action": action,
                     "status": "timeout", "error": str(e), "output": ""}
            results.append(r)
            _op_end(name)
    finally:
        for name in targets:  # release anything left claimed if a pod blew up
            _op_end(name)
    failed = [r["name"] for r in results if not r["ok"]]
    return {"ok": not failed, "action": action,
            "status": "ok" if not failed else "partial failure",
            "error": None if not failed
            else f"{action} failed for: {', '.join(failed)}",
            "results": results, "skipped": skipped}


def reconfig_data_from_info(info):
    """Editable config fields from a saved .config.json, in the shape
    op_reconfigure consumes. Share-derived volumes are stripped from
    `volumes` (they're driven by the `shares` list instead)."""
    reg = load_shares()
    share_cpaths = {
        reg[s]["container_path"] for s in info.get("shares", []) if s in reg
    }
    volumes = {
        c: h for c, h in (info.get("volumes") or {}).items() if c not in share_cpaths
    }
    return {
        "image": info.get("image", ""),
        "command": info.get("command", ""),
        "ports": info.get("ports", {}),
        "environment": info.get("environment", {}),
        "volumes": volumes,
        "memory_limit": info.get("memory_limit", ""),
        "tailscale": info.get("include_tailscale") == "yes",
        "https": info.get("include_https") == "yes",
        "shares": info.get("shares", []),
    }


def op_pod_config(name):
    """The editable config for a deployed pod, prefilling the edit popup."""
    if name not in deployed_services():
        return {"ok": False, "name": name, "config": None, "error": "Unknown service."}
    info = pod_config(name)
    if info is None:
        return {"ok": False, "name": name, "config": None,
                "error": "No .config.json for this pod (redeploy once to create it)."}
    config = reconfig_data_from_info(info)
    config["controller"] = name in CONTROLLER_PODS
    return {"ok": True, "name": name, "error": None, "config": config}


def op_reconfigure(name, data):
    """Re-render a deployed pod from edited config, then apply it via run.sh.

    data: image, command, ports, environment, volumes, memory_limit,
          shares(list of names), pull(bool).
    pull True  => fetch the latest image tag first ("Update").
    pull False => recreate with the current image ("Reload").
    Both save the edits. Refuses the controller (can't recreate itself)."""
    if name not in deployed_services():
        return {"ok": False, "name": name, "action": "reconfigure", "status": "error",
                "error": "Unknown service.", "output": ""}
    if name in CONTROLLER_PODS:
        return {"ok": False, "name": name, "action": "reconfigure", "status": "refused",
                "error": "Not reconfiguring the controller from itself.", "output": ""}
    info = pod_config(name)
    if info is None:
        return {"ok": False, "name": name, "action": "reconfigure", "status": "error",
                "error": "No .config.json for this pod (redeploy once to create it).",
                "output": ""}

    image = (data.get("image") or info.get("image") or "").strip()
    if not image:
        return {"ok": False, "name": name, "action": "reconfigure", "status": "error",
                "error": "An image is required.", "output": ""}

    conflict = _op_begin(name, "reconfigure")
    if conflict:
        return {"ok": False, "name": name, "action": "reconfigure", "status": "busy",
                "error": f"{conflict} is already in progress for {name}.", "output": ""}
    try:
        return _run_reconfigure(name, data, info, image)
    finally:
        _op_end(name)


def _run_reconfigure(name, data, info, image):
    # Merge registered shares into volumes, exactly as install does.
    volumes = dict(data.get("volumes") or {})
    reg = load_shares()
    attached = []
    for sname in data.get("shares") or []:
        share = reg.get(sname)
        if share:
            cpath, host = share_volume(share)
            volumes[cpath] = host
            attached.append(sname)

    # Tailscale is mandatory; the engine derives HTTPS/network mode itself.
    config = {
        "container": name,
        "image": image,
        "network_mode": f"service:tailscale-{name}",
        "ports": data.get("ports") or {},
        "restart_policy": info.get("restart_policy", "unless-stopped"),
        "include_tailscale": "yes",
        "include_https": "yes" if (data.get("ports") or {}) else "no",
        # Public exposure survives reconfigures; only the Funnel toggle
        # (op_network_set) changes it.
        "funnel": info.get("funnel", "no"),
        # Existing pods keep their Tailscale identity in ./tailscale/, so no
        # auth key is needed to re-enroll; carry the saved path through.
        "auth_key_file": info.get("auth_key_file",
                                  os.path.join(PODS_DIR, name, ".tailscale_authkey")),
        "base_path": PODS_DIR,
        "environment": data.get("environment") or {},
        "volumes": volumes,
        "command": data.get("command", ""),
        "memory_limit": (data.get("memory_limit") or "").strip(),
        "shares": sorted(attached),
        # Config seeding is one-time (sentinel-gated in run.sh) and not
        # user-editable — carry it through so a re-render keeps it.
        "config_file": info.get("config_file", ""),
        "config_set": info.get("config_set", {}),
    }

    pull_out = ""
    if data.get("pull"):
        pull = podman("pull", image, timeout=600)
        pull_out = pull.stdout + pull.stderr
        if pull.returncode != 0:
            return {"ok": False, "name": name, "action": "reconfigure",
                    "status": "pull failed", "error": "pull failed", "output": pull_out}

    result = run_create(config)
    if result.returncode != 0:
        return {"ok": False, "name": name, "action": "reconfigure", "status": "render failed",
                "error": "create.sh failed",
                "output": pull_out + result.stdout + result.stderr}

    r = subprocess.run(["sh", "./run.sh"], cwd=os.path.join(PODS_DIR, name),
                       capture_output=True, text=True, timeout=600)
    output = pull_out + result.stdout + result.stderr + r.stdout + r.stderr
    ok = r.returncode == 0
    return {"ok": ok, "name": name, "action": "reconfigure",
            "status": "ok" if ok else f"exit {r.returncode}",
            "error": None, "output": output}


def network_entry(name, ps):
    """One pod's networking facts: flags from .config.json plus the live
    tailnet identity (IP + MagicDNS name) read from its running sidecar."""
    info = pod_config(name) or {}
    ts = info.get("include_tailscale") == "yes"
    entry = {
        "name": name,
        "controller": name in CONTROLLER_PODS,
        "system": _is_system(name),
        "state": pod_state(name, ps),
        "tailscale": ts,
        "https": info.get("include_https") == "yes",
        "funnel": info.get("funnel") == "yes",
        "network_mode": info.get("network_mode", "bridge"),
        "ports": info.get("ports", {}),
        "ip": "",
        "dns_name": "",
        "busy": pod_busy(name),
    }
    if ts and ps.get(f"tailscale-{name}", ("", 0))[0] == "running":
        r = podman("exec", f"tailscale-{name}", "tailscale", "status",
                   "--json", "--peers=false", timeout=15)
        if r.returncode == 0:
            try:
                me = (json.loads(r.stdout) or {}).get("Self") or {}
                ips = me.get("TailscaleIPs") or []
                entry["ip"] = next((i for i in ips if "." in i), ips[0] if ips else "")
                entry["dns_name"] = (me.get("DNSName") or "").rstrip(".")
            except ValueError:
                pass
    return entry


def service_url(entry):
    """Best launch/probe URL for a pod: HTTPS on the MagicDNS name when
    tailscale serve terminates TLS, else plain http on the first port."""
    port = next(iter(entry["ports"].values()), "")
    host = entry["dns_name"] or entry["ip"]
    if not host:
        return ""
    if entry["https"] and entry["dns_name"]:
        return f"https://{entry['dns_name']}"
    return f"http://{host}:{port}" if port else f"http://{host}"


def status_network():
    """Per-pod networking for every deployed pod."""
    ps = ps_all()
    return [network_entry(name, ps) for name in deployed_services()]


def _serve_config(primary_port, funnel):
    """The tailscale-serve.json content for a pod: TLS on 443 proxying to the
    service, with public (Funnel) exposure opt-in. ${TS_CERT_DOMAIN} is a
    literal placeholder — the sidecar's containerboot interpolates it."""
    cfg = {"TCP": {"443": {"HTTPS": True}}}
    if funnel:
        cfg["AllowFunnel"] = {"${TS_CERT_DOMAIN}:443": True}
    cfg["Web"] = {"${TS_CERT_DOMAIN}:443": {
        "Handlers": {"/": {"Proxy": f"http://127.0.0.1:{primary_port}"}}}}
    return cfg


def op_network_set(name, data):
    """Make a pod public (Tailscale Funnel) or private again. data: {funnel: bool}.

    Live flip, no pod restart: the pod's tailscale-serve.json is rewritten
    IN PLACE — it's a single-file bind mount into the sidecar (same inode or
    the mount goes stale), whose containerboot watches TS_SERVE_CONFIG and
    re-applies serve config on change. The choice is also persisted through
    create.sh (.config.json + run.sh) so recreates and reboots keep it.

    Public exposure additionally requires the `funnel` nodeAttr in the
    tailnet policy — without it tailscaled refuses, which shows up in the
    sidecar's logs and in the serve-status readback appended to the output.
    """
    if name not in deployed_services():
        return {"ok": False, "name": name, "action": "funnel", "status": "error",
                "error": "Unknown service.", "output": ""}
    if name in CONTROLLER_PODS:
        return {"ok": False, "name": name, "action": "funnel", "status": "refused",
                "error": "Not exposing the controller to the public internet.",
                "output": ""}
    info = pod_config(name)
    if info is None:
        return {"ok": False, "name": name, "action": "funnel", "status": "error",
                "error": "No .config.json for this pod (redeploy once to create it).",
                "output": ""}
    if "funnel" not in data:
        return {"ok": False, "name": name, "action": "funnel", "status": "error",
                "error": "Missing 'funnel' flag.", "output": ""}
    if info.get("include_https") != "yes" or not info.get("primary_port"):
        return {"ok": False, "name": name, "action": "funnel", "status": "error",
                "error": "This pod has no HTTPS serve to expose (no port).",
                "output": ""}
    funnel = bool(data["funnel"])
    conflict = _op_begin(name, "funnel")
    if conflict:
        return {"ok": False, "name": name, "action": "funnel", "status": "busy",
                "error": f"{conflict} is already in progress for {name}.", "output": ""}
    try:
        # 1. Persist: re-render .config.json + scripts with the new flag.
        #    create.sh only generates files — the running pod is untouched.
        config = config_from_info(info)
        config["funnel"] = "yes" if funnel else "no"
        result = run_create(config)
        output = result.stdout + result.stderr
        if result.returncode != 0:
            return {"ok": False, "name": name, "action": "funnel",
                    "status": "render failed", "error": "create.sh failed",
                    "output": output}

        # 2. Persist for the NEXT sidecar start: rewrite the mounted serve
        #    config in place (containerboot applies it at start only — its
        #    file watcher doesn't see cross-namespace writes, verified live).
        serve_path = os.path.join(PODS_DIR, name, "tailscale-serve.json")
        with open(serve_path, "w") as f:
            json.dump(_serve_config(info["primary_port"], funnel), f, indent=2)

        # 3. Funnel needs the `funnel` nodeAttr, which the managed policy
        #    grants via tag:tailarr-public — flip that tag on the sidecar.
        attr_err = ts_set_public(name, funnel)
        if attr_err:
            output += f"\n[funnel nodeAttr] {attr_err}"

        # 4. Live apply via the CLI in the running sidecar (no restart).
        #    ACL refusals (missing nodeAttr) surface right here.
        port = info["primary_port"]
        if ps_all().get(f"tailscale-{name}", ("", 0))[0] == "running":
            if attr_err is None and funnel:
                time.sleep(2)  # let the nodeAttr land before asking for funnel
            if funnel:
                r = podman("exec", f"tailscale-{name}", "tailscale",
                           "funnel", "--bg", str(port), timeout=30)
                output += r.stdout + r.stderr
                if r.returncode != 0:
                    return {"ok": False, "name": name, "action": "funnel",
                            "status": "funnel refused",
                            "error": "tailscale refused funnel (see output; "
                                     "is the funnel nodeAttr granted?)",
                            "output": output}
            else:
                r = podman("exec", f"tailscale-{name}", "tailscale",
                           "funnel", "--https=443", "off", timeout=30)
                output += r.stdout + r.stderr
                # restore tailnet-only serve for 443
                r = podman("exec", f"tailscale-{name}", "tailscale",
                           "serve", "--bg", str(port), timeout=30)
                output += r.stdout + r.stderr
            r = podman("exec", f"tailscale-{name}", "tailscale", "funnel",
                       "status", timeout=15)
            output += r.stdout + r.stderr
        else:
            output += "\n(sidecar not running — applies at next start)"
        state = "public" if funnel else "private"
        return {"ok": True, "name": name, "action": "funnel", "status": state,
                "error": None, "output": output}
    finally:
        _op_end(name)


# =========================================================================
# Uptime Kuma monitoring (Monitor tab)
# =========================================================================
def _kuma():
    """The kuma_client module, or (None, reason) when its socket-client
    dependency isn't installed (e.g. in CI)."""
    try:
        import kuma_client
        if not kuma_client.available():
            return None, "uptime-kuma-api is not installed in this image."
        return kuma_client, None
    except ImportError as e:
        return None, f"monitoring client unavailable: {e}"


def _discover_kuma(entries):
    """The deployed Uptime Kuma pod (by image), with a connect URL the
    controller can reach: plain http on its tailnet IP + service port
    (socket.io needs no TLS here; the tailnet already encrypts)."""
    for e in entries:
        info = pod_config(e["name"]) or {}
        if "uptime-kuma" in info.get("image", ""):
            port = next(iter(e["ports"].values()), "3001")
            url = f"http://{e['ip']}:{port}" if e["ip"] else ""
            return e["name"], url, service_url(e)
    return None, "", ""


def status_monitor():
    """Everything the Monitor tab needs: Kuma connection state, the
    tailscale-enabled pods, and which of them already have monitors."""
    kuma, err = _kuma()
    ps = ps_all()
    entries = [network_entry(n, ps) for n in deployed_services()]
    kuma_pod, suggested_url, kuma_link = _discover_kuma(entries)
    pods = [
        {"name": e["name"], "state": e["state"], "https": e["https"],
         "dns_name": e["dns_name"], "url": service_url(e), "monitored": False}
        for e in entries
        if e["tailscale"] and e["name"] != kuma_pod and not e["controller"]
        and not e["system"]  # system pods: ops health alerts cover them
    ]
    out = {
        "available": kuma is not None,
        "configured": False,
        "connected": False,
        "error": err,
        "kuma_pod": kuma_pod,
        "kuma_url": suggested_url,
        "kuma_link": kuma_link,
        "monitors": [],
        "pods": pods,
    }
    if kuma is None:
        return out
    conf = kuma.load_conf()
    out["configured"] = conf is not None
    if not conf:
        return out
    out["kuma_url"] = conf["url"]
    try:
        monitors = kuma.get_monitors() or []
        out["connected"] = True
        out["monitors"] = monitors
        by_name = {m["name"] for m in monitors}
        for p in pods:
            p["monitored"] = p["name"] in by_name
    except Exception as e:
        out["error"] = f"Kuma connection failed: {e}"
    return out


def op_monitor_setup(data):
    """Connect (and on a fresh instance, initialize) Kuma; save creds."""
    kuma, err = _kuma()
    if kuma is None:
        return {"ok": False, "error": err}
    url = (data.get("url") or "").strip()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not url:
        ps = ps_all()
        entries = [network_entry(n, ps) for n in deployed_services()]
        _, url, _ = _discover_kuma(entries)
    if not url:
        return {"ok": False, "error": "No Kuma URL given and no uptime-kuma pod found."}
    if not username or not password:
        return {"ok": False, "error": "Username and password are required."}
    try:
        r = kuma.setup(url, username, password)
        return {"ok": True, "error": None, "fresh": r.get("fresh", False), "url": url}
    except Exception as e:
        return {"ok": False, "error": f"Could not connect: {e}"}


def op_monitor_pod(name, action):
    """Add or remove the Kuma monitor for a pod."""
    kuma, err = _kuma()
    if kuma is None:
        return {"ok": False, "name": name, "error": err}
    if name not in deployed_services():
        return {"ok": False, "name": name, "error": "Unknown service."}
    try:
        if action == "add":
            entry = network_entry(name, ps_all())
            url = service_url(entry)
            if not url:
                return {"ok": False, "name": name,
                        "error": "Pod has no reachable URL yet (sidecar still enrolling?)."}
            kuma.add_monitor(name, url)
            return {"ok": True, "name": name, "error": None, "url": url}
        if action == "remove":
            found = kuma.remove_monitor(name)
            return {"ok": found, "name": name,
                    "error": None if found else "No monitor with this name."}
        return {"ok": False, "name": name, "error": "Unknown action."}
    except Exception as e:
        return {"ok": False, "name": name, "error": f"Kuma call failed: {e}"}


# =========================================================================
# ntfy notifications (system pod) — registry + publish in ntfy_client.py,
# podman-driving provisioning here beside the other exec ops. ntfy is the
# first SYSTEM_IMAGES pod: hidden from sharing (no can- badge, no grant
# lines, no consumer-netmap presence), reachable by pods over the fleet
# intercom grant, funnel-able for phones outside the tailnet.
# =========================================================================
NTFY_IMAGE = "binwiederhier/ntfy"

_ntfy_cache = {"at": 0.0, "entry": None}


def _discover_ntfy(fresh=False):
    """The deployed ntfy pod's network entry (image match, like Kuma).
    Cached ~60s — publishes fire from background threads on every event
    and must not each cost a podman exec."""
    if not fresh and time.time() - _ntfy_cache["at"] < 60:
        return _ntfy_cache["entry"]
    entry = None
    ps = ps_all()
    for name in deployed_services():
        if NTFY_IMAGE in (pod_config(name) or {}).get("image", ""):
            entry = network_entry(name, ps)
            break
    _ntfy_cache["at"] = time.time()
    _ntfy_cache["entry"] = entry
    return entry


def _ntfy_internal_url(entry):
    """Plain http on the tailnet IP + service port: reachable by the
    controller and every pod via the fleet intercom grant, no TLS needed
    (the tailnet already encrypts)."""
    if not entry or not entry.get("ip"):
        return ""
    port = next(iter(entry["ports"].values()), "80")
    return f"http://{entry['ip']}:{port}"


def _ntfy_server_yml(dns_name):
    """The server.yml the controller owns. deny-all is the security model:
    every topic needs an explicit account grant, so the funnel can be
    opened without exposing anything. upstream-base-url relays a
    content-free poll hint through ntfy.sh so iOS gets APNs push —
    payloads never leave this server."""
    lines = [
        "# Managed by Tailarr (Notifications setup) — regenerated on every",
        "# setup run; do not edit.",
        "behind-proxy: true",
        "auth-file: /etc/ntfy/auth.db",
        "auth-default-access: deny-all",
        "cache-file: /var/cache/ntfy/cache.db",
        "attachment-cache-dir: /var/cache/ntfy/attachments",
        'upstream-base-url: "https://ntfy.sh"',
    ]
    if dns_name:
        lines.insert(2, f'base-url: "https://{dns_name}"')
    return "\n".join(lines) + "\n"


def _ntfy_cli(pod, *args, env=None):
    """Run the ntfy CLI inside the pod. env pairs ride -e so secrets
    (NTFY_PASSWORD) stay off the podman argv."""
    pre = []
    for k, v in (env or {}).items():
        pre += ["-e", f"{k}={v}"]
    return podman("exec", *pre, pod, "ntfy", *args, timeout=30)


def _ntfy_provision(pod):
    """Ensure the controller's two ntfy accounts + tokens exist.

    Check-then-act throughout, so a crashed or repeated setup converges:
    existing accounts keep their saved tokens; accounts missing from the
    server (fresh install, or a wiped auth.db after reinstall) are
    recreated WITH new tokens — saved tokens can't survive a new auth.db.
    Returns (admin, publisher, error)."""
    conf = ntfy_client.load_conf() or {}
    listed = _ntfy_cli(pod, "user", "list")
    if listed.returncode != 0:
        return None, None, ("ntfy CLI unavailable: "
                            + (listed.stdout + listed.stderr).strip()[-200:])
    users_out = listed.stdout + listed.stderr  # the CLI lists on stderr
    out = {}
    for user, role, key in ((ntfy_client.ADMIN_USER, "admin", "admin"),
                            (ntfy_client.PUB_USER, "user", "publisher")):
        saved = conf.get(key) or {}
        exists = f"user {user}" in users_out
        password = (saved.get("password") if exists else None) \
            or secrets.token_urlsafe(24)
        if not exists:
            r = _ntfy_cli(pod, "user", "add", f"--role={role}", user,
                          env={"NTFY_PASSWORD": password})
            if r.returncode != 0 and "exists" not in (r.stdout + r.stderr):
                return None, None, (f"could not create {user}: "
                                    + (r.stdout + r.stderr).strip()[-200:])
        token = saved.get("token", "") if exists else ""
        if not token:
            r = _ntfy_cli(pod, "token", "add", user)
            m = re.search(r"tk_[A-Za-z0-9_]+", r.stdout + r.stderr)
            if r.returncode != 0 or not m:
                return None, None, (f"could not mint a token for {user}: "
                                    + (r.stdout + r.stderr).strip()[-200:])
            token = m.group(0)
        out[key] = {"user": user, "password": password, "token": token}
    # The publisher may write every tailarr topic; per-user READ grants
    # come later (badge mirroring) — deny-all covers everything else.
    r = _ntfy_cli(pod, "access", ntfy_client.PUB_USER, "tlr-*", "write")
    if r.returncode != 0:
        return None, None, ("could not grant publish access: "
                            + (r.stdout + r.stderr).strip()[-200:])
    return out["admin"], out["publisher"], None


def status_ntfy():
    """Everything the Notifications tab needs."""
    entry = _discover_ntfy(fresh=True)
    conf = ntfy_client.load_conf()
    state = ntfy_client.load_state()
    info = pod_config(entry["name"]) if entry else None
    return {
        "installed": entry is not None,
        "pod": entry["name"] if entry else None,
        "state": entry["state"] if entry else "",
        "configured": conf is not None,
        "funnel_on": bool(info and info.get("funnel") == "yes"),
        "public_url": (f"https://{entry['dns_name']}"
                       if entry and entry.get("dns_name") else ""),
        "ops_topic": ntfy_client.OPS_TOPIC,
        "gateway": GATEWAY_POD in deployed_services(),
        "arr": _arr_status(),
        "publish_error": state.get("last_publish_error") or None,
        "error": None,
    }


def op_ntfy_setup(_data):
    """Configure the deployed ntfy pod end-to-end. Idempotent: every step
    checks before acting, so re-running after any failure converges.

    write server.yml (into the host side of the /etc/ntfy volume — the
    default install puts it under PODS_DIR, which the controller mounts)
    -> restart the pod if the file changed -> provision accounts/tokens
    via the ntfy CLI -> save the registry -> ENABLE FUNNEL -> test-publish.

    Funnel is part of setup, not a separate Network-page step: phone
    delivery IS the feature, and the deny-all ACL + per-account tokens
    are what make the public endpoint safe. The SPA warns about the
    internet exposure before calling this; the Notifications page is the
    single control surface for everything ntfy."""
    entry = _discover_ntfy(fresh=True)
    if not entry:
        # ntfy is hidden from the catalog (single control surface): the
        # setup button IS the install path. Defaults mirror the catalog
        # entry exactly like a catalog install would.
        spec = resolve_service("ntfy")
        if not spec:
            return {"ok": False,
                    "error": "The ntfy catalog entry is missing."}
        inst = op_install({
            "name": "ntfy", "custom": False,
            "image": spec["image"], "command": spec.get("command", ""),
            "ports": spec.get("ports", {}),
            "environment": spec.get("environment", {}),
            "volumes": {cpath: os.path.join(PODS_DIR, "ntfy",
                                            cpath.lstrip("/"))
                        for _, cpath in spec.get("volumes", {}).items()},
            "restart_policy": spec.get("restart_policy", "unless-stopped"),
            "shares": [], "authkey": "",
            "config_file": "", "config_set": {},
        })
        if not inst["ok"]:
            return {"ok": False,
                    "error": inst.get("error")
                    or "ntfy install failed — see output.",
                    "output": inst.get("output", "")}
        entry = _discover_ntfy(fresh=True)
        if not entry:
            return {"ok": False,
                    "error": "ntfy installed but was not found afterwards."}
    name = entry["name"]
    info = pod_config(name) or {}
    conf_dir = (info.get("volumes") or {}).get("/etc/ntfy", "")
    if not conf_dir:
        return {"ok": False,
                "error": "The ntfy pod has no /etc/ntfy volume — reinstall "
                         "it from the current catalog entry."}
    yml = _ntfy_server_yml(entry.get("dns_name", ""))
    yml_path = os.path.join(conf_dir, "server.yml")
    try:
        with open(yml_path) as f:
            changed = f.read() != yml
    except OSError:
        changed = True
    if changed:
        try:
            os.makedirs(conf_dir, exist_ok=True)
            with open(yml_path + ".tmp", "w") as f:
                f.write(yml)
            os.replace(yml_path + ".tmp", yml_path)
        except OSError as e:
            return {"ok": False, "error": f"could not write server.yml: {e}"}
        r = op_action(name, "start")  # run.sh recreates -> config applies
        if not r["ok"]:
            return {"ok": False, "error": "ntfy restart failed",
                    "output": r.get("output", "")}
    admin = publisher = None
    err = "ntfy pod is not running"
    for _attempt in range(5):  # the recreated container needs a beat
        admin, publisher, err = _ntfy_provision(name)
        if not err or "CLI unavailable" not in err:
            break
        time.sleep(2)
    if err:
        return {"ok": False, "error": err}
    conf = ntfy_client.load_conf() or {}
    conf.update({
        "version": 1,
        "pod": name,
        "public_url": (f"https://{entry['dns_name']}"
                       if entry.get("dns_name") else ""),
        "admin": admin,
        "publisher": publisher,
        "topics": {"ops": ntfy_client.OPS_TOPIC,
                   "media_prefix": ntfy_client.MEDIA_TOPIC_PREFIX},
        "users": conf.get("users") or {},
        "arr": conf.get("arr") or {},
    })
    ntfy_client.save_conf(conf)
    # Funnel is part of the feature (see docstring). Non-fatal: a refused
    # funnel (nodeAttr not granted yet, sidecar down) leaves everything
    # else working on the tailnet; re-running setup retries it.
    funnel_error = None
    if (pod_config(name) or {}).get("funnel") != "yes":
        fr = op_network_set(name, {"funnel": True})
        if not fr["ok"]:
            funnel_error = fr.get("error") or "funnel enable failed"
    entry = _discover_ntfy(fresh=True)  # IP may differ after the restart
    # Late base-url converge: on a first-ever setup the sidecar hadn't
    # enrolled when server.yml was written, so base-url (iOS push +
    # attachments need it) was missing — once the DNS name is known,
    # rewrite and bounce the pod one more time. No-op on re-runs.
    if entry and entry.get("dns_name"):
        yml2 = _ntfy_server_yml(entry["dns_name"])
        try:
            with open(yml_path) as f:
                stale = f.read() != yml2
        except OSError:
            stale = True
        if stale:
            try:
                with open(yml_path + ".tmp", "w") as f:
                    f.write(yml2)
                os.replace(yml_path + ".tmp", yml_path)
                op_action(name, "start")
                entry = _discover_ntfy(fresh=True)
            except OSError:
                pass  # next setup run converges it
    test_err = ntfy_client.publish(
        conf, _ntfy_internal_url(entry), ntfy_client.OPS_TOPIC,
        "Tailarr", "Notifications are set up.")
    # The self-config gateway rides along: with notifications live, the
    # Tailarr app on user devices can now fetch its own config.
    try:
        gateway_error = _ensure_gateway()
    except Exception as e:  # noqa: BLE001 — never fail setup on this
        gateway_error = str(e)
    # And converge media wiring for any Arr not wired yet (best-effort;
    # per-pod failures surface as rows with a manual recipe in the UI).
    wire_errors = {}
    for arr in _arr_status():
        if arr["wired"]:
            continue
        try:
            wr = op_ntfy_wire(arr["name"])
            if not wr["ok"]:
                wire_errors[arr["name"]] = wr.get("error")
        except Exception as e:  # noqa: BLE001
            wire_errors[arr["name"]] = str(e)
    return {"ok": True, "error": None, "test_error": test_err,
            "funnel_error": funnel_error, "gateway_error": gateway_error,
            "wire_errors": wire_errors or None,
            "status": status_ntfy()}


def op_ntfy_funnel(data):
    """Toggle ntfy's public endpoint from the Notifications page — the
    single control surface for ntfy (the Network page hides system pods)."""
    entry = _discover_ntfy(fresh=True)
    if not entry:
        return {"ok": False, "error": "No ntfy pod found."}
    r = op_network_set(entry["name"], {"funnel": bool(data.get("enabled"))})
    return {"ok": r["ok"], "error": r.get("error"),
            "status": status_ntfy()}


def op_ntfy_test(_data):
    """Synchronous test publish for the Notifications page button."""
    conf = ntfy_client.load_conf()
    if not conf:
        return {"ok": False, "error": "ntfy is not configured — run setup."}
    err = ntfy_client.publish(
        conf, _ntfy_internal_url(_discover_ntfy(fresh=True)),
        ntfy_client.OPS_TOPIC, "Tailarr", "Test notification.")
    return {"ok": err is None, "error": err}


# =========================================================================
# Self-config gateway (tailarr-gate) — see the GATEWAY_POD note and
# web/selfconfig.py. Deployed as part of ntfy setup; the app on a user's
# device asks the gateway, the gateway asks us, we whois THE GATEWAY'S
# sidecar (user devices are its peers, never ours) and answer with that
# person's notification handout.
# =========================================================================
def load_gateway():
    try:
        with open(GATEWAY_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) and data.get("secret") \
            else None
    except (OSError, ValueError):
        return None


def _controller_image():
    name = _controller_name()
    if not name:
        return ""
    r = podman("inspect", name, "--format", "{{.ImageName}}")
    return r.stdout.strip() if r.returncode == 0 else ""


def _controller_ip():
    """The controller's own tailnet IPv4, straight from its sidecar.

    Deliberately NOT network_entry(): that helper gates its sidecar
    query on the pod's .config.json, and BOOTSTRAP-created controllers
    (i.e. every install) have none — live-caught by the app session
    2026-07-22 when gateway deploy failed with "controller tailnet IP
    unknown" on an upgraded install."""
    ctrl = _controller_name()
    if not ctrl:
        return ""
    r = podman("exec", f"tailscale-{ctrl}", "tailscale", "status",
               "--json", "--peers=false", timeout=15)
    if r.returncode != 0:
        return ""
    try:
        ips = ((json.loads(r.stdout) or {}).get("Self") or {}) \
            .get("TailscaleIPs") or []
        return next((i for i in ips if "." in i), ips[0] if ips else "")
    except ValueError:
        return ""


def _controller_dns():
    """The controller's MagicDNS name, straight from its sidecar (same
    direct-ask rationale as _controller_ip: bootstrap-created
    controllers have no .config.json for network_entry to gate on)."""
    ctrl = _controller_name()
    if not ctrl:
        return ""
    r = podman("exec", f"tailscale-{ctrl}", "tailscale", "status",
               "--json", "--peers=false", timeout=15)
    if r.returncode != 0:
        return ""
    try:
        return (((json.loads(r.stdout) or {}).get("Self") or {})
                .get("DNSName") or "").rstrip(".")
    except ValueError:
        return ""


def _ensure_gateway():
    """Deploy the gateway pod once (idempotent; runs during ntfy setup).
    Uses the controller's own image with the selfconfig entrypoint. The
    post-install policy sync emits the user->gateway grant. Returns an
    error string or None."""
    if GATEWAY_POD in deployed_services():
        return None
    gw = load_gateway()
    if not gw:
        gw = {"secret": secrets.token_urlsafe(32),
              "created": int(time.time())}
        _write_secret(GATEWAY_FILE, json.dumps(gw, indent=2))
    image = _controller_image()
    if not image:
        return "could not determine the controller image"
    ip = _controller_ip()
    if not ip:
        return "controller tailnet IP unknown (sidecar not running?)"
    inst = op_install({
        "name": GATEWAY_POD, "custom": True,
        "image": image,
        "command": "python3 /app/web/selfconfig.py",
        "ports": {GATEWAY_PORT: GATEWAY_PORT},
        "environment": {"CONTROLLER_URL": f"http://{ip}:{PORT}",
                        "GATEWAY_SECRET": gw["secret"]},
        "volumes": {}, "restart_policy": "unless-stopped",
        "shares": [], "authkey": "",
        "config_file": "", "config_set": {},
    })
    if not inst["ok"]:
        return inst.get("error") or "gateway install failed"
    r = op_action(GATEWAY_POD, "start")
    if not r["ok"]:
        return "gateway start failed: " + (r.get("error") or r["status"])
    return None


def op_gateway_resolve(data):
    """The gateway's only question: whose connection is this, and what
    is their notification config? Authenticated by the per-install
    shared secret (the gateway's env), NOT the bearer gate — this path
    is exempt there and useless without the secret. Tailnet source
    addresses are unforgeable, so the whois answer is authoritative."""
    gw = load_gateway()
    if not gw or not hmac.compare_digest(
            str(data.get("secret") or ""), gw["secret"]):
        return {"ok": False, "error": "bad gateway secret"}
    ip = (data.get("ip") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F.:]+", ip):
        return {"ok": False, "error": "bad address"}
    r = podman("exec", f"tailscale-{GATEWAY_POD}", "tailscale",
               "whois", "--json", ip, timeout=15)
    if r.returncode != 0:
        return {"ok": False, "error": ("whois failed: "
                + (r.stdout + r.stderr).strip()[-200:])}
    try:
        node = (json.loads(r.stdout) or {}).get("Node") or {}
        tags = node.get("Tags") or []
    except ValueError:
        return {"ok": False, "error": "whois returned no node"}
    uid = next((t[len(TS_PERSON_PREFIX):] for t in tags
                if t.startswith(TS_PERSON_PREFIX)), None)
    if not uid:
        return {"ok": False,
                "error": "this device is not assigned to a user"}
    want = (data.get("want") or "notifications").strip()
    if want == "services":
        return op_person_services(uid)
    if want == "push-token":
        return op_person_push(uid, data)
    if want != "notifications":
        return {"ok": False, "error": "unknown request"}
    return op_person_notify(uid)


# Native app-module kinds the services handout fully configures: each has
# a matching connection module in the Tailarr app AND an extractable
# credential, read live from the pod's own config the same way the ntfy
# Arr wiring reads config.xml. Every other badged service still appears —
# as an "external" entry (URL only) the app renders as a web bookmark, so
# nothing a badge grants is ever invisible. Detection is by image
# substring; jellyseerr's API is Overseerr-compatible, so it hands out as
# type "overseerr" for the app's (dormant, contract-ready) module.
SERVICE_MODULE_KINDS = ("sonarr", "radarr", "lidarr", "nzbget",
                        "sabnzbd", "tautulli", "jellyseerr", "overseerr")


def _service_kind(name):
    """The handout type for a pod, or None (=> external entry)."""
    img = (pod_config(name) or {}).get("image", "")
    for k in SERVICE_MODULE_KINDS:
        if k in img:
            return "overseerr" if k == "jellyseerr" else k
    return None


def _conf_read(pod, path):
    """One config file out of a running pod, or None."""
    r = podman("exec", pod, "cat", path, timeout=15)
    return r.stdout if r.returncode == 0 else None


def _ini_value(text, key):
    """First `key = value` (or key=value) line — the sabnzbd.ini /
    Tautulli config.ini / nzbget.conf shape."""
    m = re.search(rf"^\s*{re.escape(key)}\s*=\s*(\S+)\s*$", text,
                  re.MULTILINE)
    return m.group(1) if m else None


def _service_auth(pod, kind):
    """The credential handout for a native-kind pod, or None when it
    can't be read (the app keeps the module and prompts). Same trust
    math as the Arr key: every one of these is visible in the service's
    own UI to anyone the badge already lets in."""
    if kind in ("sonarr", "radarr", "lidarr"):
        key = _arr_api_key(pod)
        return {"api_key": key} if key else None
    if kind == "nzbget":
        text = _conf_read(pod, "/config/nzbget.conf") or ""
        pw = _ini_value(text, "ControlPassword")
        return {"user": _ini_value(text, "ControlUsername") or "",
                "password": pw} if pw else None
    if kind == "sabnzbd":
        key = _ini_value(_conf_read(pod, "/config/sabnzbd.ini") or "",
                         "api_key")
        return {"api_key": key} if key else None
    if kind == "tautulli":
        key = _ini_value(_conf_read(pod, "/config/config.ini") or "",
                         "api_key")
        return {"api_key": key} if key else None
    if kind == "overseerr":
        # linuxserver/overseerr keeps config in /config;
        # fallenbagel/jellyseerr in /app/config. Same settings.json.
        for path in ("/config/settings.json",
                     "/app/config/settings.json"):
            text = _conf_read(pod, path)
            if text is None:
                continue
            try:
                key = ((json.loads(text) or {}).get("main") or {}) \
                    .get("apiKey")
            except ValueError:
                key = None
            return {"api_key": key} if key else None
    return None


def op_person_services(uid):
    """The app's services handout (GET /self/services via the gateway):
    every service this person's badges grant, ready to drop into the
    app's modules.

    Credentials are read live from the pod, never stored. No privilege
    is widened: the badge already grants network reach to the service,
    and the Arr's own Settings page shows the same API key to anyone who
    can load it — this only removes the scavenger hunt. Contract notes
    for the app: "url" may be "" while a service is stopped (its sidecar
    holds the MagicDNS name) — keep the previous value rather than
    deconfigure; "auth" is null when the credential couldn't be read —
    create the module and prompt for the missing piece."""
    people = load_people()
    if uid not in people:
        return {"ok": False, "error": "Unknown user."}
    valid = set(_shareable_services())
    ps = ps_all()
    services = []
    for svc in sorted(set(people[uid].get("badges") or [])):
        if svc == SERVER_SERVICE:
            dns = _controller_dns()
            services.append({
                "type": "tailarr", "name": SERVER_SERVICE,
                "url": f"https://{dns}" if dns else "", "auth": None})
            continue
        if svc not in valid:
            continue  # stale badge: the service is gone
        kind = _service_kind(svc)
        auth = _service_auth(svc, kind) if kind else None
        services.append({
            "type": kind or "external", "name": svc,
            "url": service_url(network_entry(svc, ps)), "auth": auth})
    # Search: the saved newznab indexers, gated on the "search" badge.
    # These are NOT tailnet pods — url is the indexer's own public
    # address and the app's Search module reaches it directly. Multiple
    # entries are expected (a person can have several indexers), so the
    # app must ACCUMULATE type "indexer", not dedup it like native
    # single-slot modules.
    if SEARCH_SERVICE in (people[uid].get("badges") or []):
        for _aid, e in _vault_indexers():
            services.append({
                "type": "indexer",
                "name": e.get("label") or _account_host_label(e.get("url")),
                "url": e.get("url", ""),
                "auth": {"api_key": e.get("key", "")}})
    return {"ok": True, "error": None, "kind": "services",
            "services": services}


# =========================================================================
# Media events — automated Arr wiring (ntfy phase 2). The controller
# configures each Arr's NATIVE ntfy Connect notification for it: API key
# read from the pod's own config.xml, the connection created/updated via
# the Arr's API over the tailnet, pointed at ntfy's tailnet URL with the
# publisher credentials and the pod's tlr-media-<name> topic (the same
# topic the person badge mirror grants read access to). Field names are
# mapped from the Arr's OWN /notification/schema — versions drift — and
# any mismatch falls back to a copy-paste manual recipe in the UI.
# =========================================================================
ARR_KINDS = {"sonarr": "v3", "radarr": "v3",
             "lidarr": "v1", "readarr": "v1"}


def _arr_kind(name):
    """(kind, api version) when a deployed pod is a supported Arr."""
    img = (pod_config(name) or {}).get("image", "")
    for k, ver in ARR_KINDS.items():
        if k in img:
            return k, ver
    return None, None


def _arr_api_key(pod):
    """The Arr's own API key, from its config.xml inside the pod."""
    r = podman("exec", pod, "cat", "/config/config.xml", timeout=15)
    if r.returncode != 0:
        return None
    m = re.search(r"<ApiKey>([^<]+)</ApiKey>", r.stdout)
    return m.group(1).strip() if m else None


def _arr_req(base, key, method, path, body=None):
    """One authenticated Arr API call. Returns (status, parsed-json)."""
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Api-Key": key, "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except ValueError:
            return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return 0, str(e)


def _arr_recipe(pod):
    """The copy-paste fallback when automation can't map the fields."""
    conf = ntfy_client.load_conf() or {}
    pub = conf.get("publisher") or {}
    return {"server": _ntfy_internal_url(_discover_ntfy()),
            "username": pub.get("user", ""),
            "password": pub.get("password", ""),
            "topic": ntfy_client.MEDIA_TOPIC_PREFIX + pod}


def op_ntfy_wire(pod):
    """Wire one Arr's native ntfy Connect notification automatically.
    Idempotent: an existing "Tailarr ntfy" connection is updated in
    place. Any schema surprise returns ok:False WITH the manual recipe
    so the UI degrades to copy-paste instead of a dead end."""
    conf = ntfy_client.load_conf()
    if not conf:
        return {"ok": False, "error": "ntfy is not configured — run setup."}
    kind, ver = _arr_kind(pod)
    if not kind:
        return {"ok": False, "error": "Not a supported media app."}
    entry = network_entry(pod, ps_all())
    port = next(iter(entry["ports"].values()), "")
    if not entry.get("ip") or not port:
        return {"ok": False, "recipe": _arr_recipe(pod),
                "error": f"{pod} has no reachable tailnet address yet."}
    key = _arr_api_key(pod)
    if not key:
        return {"ok": False, "recipe": _arr_recipe(pod),
                "error": f"could not read {pod}'s API key (pod running?)."}
    base = f"http://{entry['ip']}:{port}/api/{ver}"
    ntfy_url = _ntfy_internal_url(_discover_ntfy())
    topic = ntfy_client.MEDIA_TOPIC_PREFIX + pod
    pub = conf.get("publisher") or {}
    code, schemas = _arr_req(base, key, "GET", "/notification/schema")
    if code != 200 or not isinstance(schemas, list):
        return {"ok": False, "recipe": _arr_recipe(pod),
                "error": f"schema probe failed (HTTP {code})."}
    item = next((s for s in schemas
                 if (s.get("implementation") or "").lower() == "ntfy"), None)
    if not item:
        return {"ok": False, "recipe": _arr_recipe(pod),
                "error": f"{pod} has no ntfy notification type."}
    names = {f.get("name") for f in item.get("fields") or []}
    if "serverUrl" not in names or "topics" not in names:
        return {"ok": False, "recipe": _arr_recipe(pod),
                "error": "unrecognized ntfy field layout — wire manually."}
    fields = []
    for f in item.get("fields") or []:
        f = dict(f)
        n = f.get("name")
        if n == "serverUrl":
            f["value"] = ntfy_url
        elif n == "topics":
            f["value"] = [topic]
        elif n == "accessToken" and pub.get("token"):
            f["value"] = pub["token"]
        elif n == "userName" and not ("accessToken" in names
                                      and pub.get("token")):
            f["value"] = pub.get("user", "")
        elif n == "password" and not ("accessToken" in names
                                      and pub.get("token")):
            f["value"] = pub.get("password", "")
        fields.append(f)
    payload = dict(item)
    payload.update({"name": "Tailarr ntfy", "fields": fields,
                    "onGrab": False, "onDownload": True,
                    "onUpgrade": True, "onRename": False,
                    "onHealthIssue": False})
    code, existing = _arr_req(base, key, "GET", "/notification")
    if code != 200 or not isinstance(existing, list):
        return {"ok": False, "recipe": _arr_recipe(pod),
                "error": f"could not list notifications (HTTP {code})."}
    prev = next((n for n in existing
                 if n.get("name") == "Tailarr ntfy"), None)
    if prev:
        payload["id"] = prev["id"]
        code, resp = _arr_req(base, key, "PUT",
                              f"/notification/{prev['id']}", payload)
    else:
        code, resp = _arr_req(base, key, "POST", "/notification", payload)
    if code not in (200, 201, 202):
        detail = ""
        if isinstance(resp, list) and resp and isinstance(resp[0], dict):
            detail = ": " + str(resp[0].get("errorMessage", ""))[:120]
        return {"ok": False, "recipe": _arr_recipe(pod),
                "error": f"{pod} rejected the connection "
                         f"(HTTP {code}){detail}"}
    conf = ntfy_client.load_conf() or {}
    conf.setdefault("arr", {})[pod] = {"wired": "auto", "topic": topic}
    ntfy_client.save_conf(conf)
    return {"ok": True, "error": None, "name": pod, "topic": topic}


def _arr_status():
    """Per-Arr wiring rows for the Notifications page."""
    conf = ntfy_client.load_conf() or {}
    wired = conf.get("arr") or {}
    out = []
    for name in deployed_services():
        kind, _ = _arr_kind(name)
        if not kind:
            continue
        out.append({"name": name, "kind": kind,
                    "topic": ntfy_client.MEDIA_TOPIC_PREFIX + name,
                    "wired": (wired.get(name) or {}).get("wired") or ""})
    return out


# =========================================================================
# Magic Stacks (v0.25.0) — curated bundles the wizard deploys AND fully
# wires. A stack qualifies as "magic" only when every internal connection
# is auto-wireable and every collected input is a genuine external secret
# (indexer key, usenet account) — never derivable busywork. v1 guardrail
# (DECIDED): greenfield-only — if any pod of a bundled kind already
# exists, the stack is ineligible ("Magic Stacks can't recreate existing
# services"); the adopt-don't-replace model is future work. Inputs are
# validated BEFORE anything deploys (newznab caps probe + a raw NNTP
# sign-in) and are used in-flight only — never persisted. Run state lives
# in .stacks.json (steps only, no secrets); the saga runs in a background
# thread the UI polls via GET /api/stacks.
# =========================================================================
STACKS_FILE = os.path.join(PODS_DIR, ".stacks.json")

STACKS = {
    "usenet-starter": {
        "name": "Usenet Starter",
        "blurb": "TV and movies over usenet — search, download and "
                 "import, fully connected out of the box.",
        "services": ["sonarr", "radarr", "nzbget"],
        "inputs": ["media", "indexer", "usenet"],
    },
}

# Per-Arr wiring facts: root folder under the shared /data mount + the
# download category the Arr tells nzbget to file things under.
STACK_ARR_ROOTS = {"sonarr": ("/data/media/tv", "tv"),
                   "radarr": ("/data/media/movies", "movies")}


def load_stack_run():
    try:
        with open(STACKS_FILE) as f:
            data = json.load(f)
        return data.get("run") if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _save_stack_run(run):
    tmp = STACKS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"run": run}, f, indent=2)
    os.replace(tmp, STACKS_FILE)


_stack_lock = threading.Lock()


def _stack_step_set(key, state, detail=""):
    with _stack_lock:
        run = load_stack_run()
        if not run:
            return
        for s in run["steps"]:
            if s["key"] == key:
                s["state"] = state
                s["detail"] = detail
        _save_stack_run(run)


def _stack_finish(state, error=None):
    with _stack_lock:
        run = load_stack_run()
        if not run:
            return
        run["state"] = state
        run["error"] = error
        run["finished"] = int(time.time())
        _save_stack_run(run)


def stack_blockers(spec):
    """Deployed pods that collide with a stack's services — the v1
    greenfield guardrail. Kind-matched (a pod named `tv` running the
    sonarr image still blocks), not just name-matched."""
    kinds = set(spec["services"])
    out = set()
    for name in deployed_services():
        if name in kinds or (_service_kind(name) or "") in kinds \
                or (_arr_kind(name)[0] or "") in kinds:
            out.add(name)
    return sorted(out)


def status_stacks():
    run = load_stack_run()
    stacks = []
    for key, spec in STACKS.items():
        blockers = stack_blockers(spec)
        stacks.append({
            "key": key, "name": spec["name"], "blurb": spec["blurb"],
            "services": spec["services"], "blockers": blockers,
            "eligible": not blockers
            and not (run and run["state"] == "running"),
        })
    return {"stacks": stacks, "run": run}


def _clean(s, n=200):
    """Inputs travel into config files and wire protocols — strip
    control characters so nothing can smuggle an extra line."""
    return re.sub(r"[\r\n\t\0]", "", str(s or "")).strip()[:n]


# Nearly every indexer sits behind Cloudflare, whose default rules 403
# Python's stock User-Agent (error 1010) — live-caught 2026-07-23 when
# validation failed against known-good accounts on NZBgeek/NZBFinder/
# DrunkenSlug. A browser-shaped product UA passes all three.
_NEWZNAB_UA = f"Mozilla/5.0 (compatible; Tailarr/{VERSION})"


def _indexer_base(url):
    """Forgive real-world paste shapes: scheme optional (https assumed),
    a full API URL (query string and all) reduces to its base, trailing
    slashes dropped."""
    u = _clean(url, 300)
    if u and not re.match(r"^https?://", u):
        u = "https://" + u
    return u.split("?", 1)[0].rstrip("/")


def _indexer_pasted_key(url):
    """The apikey buried in a pasted full API URL, if any — so leaving
    the key field empty still works when the URL already carries it."""
    q = urllib.parse.urlparse(_clean(url, 300)).query
    return (urllib.parse.parse_qs(q).get("apikey") or [""])[0]


def _newznab_get(url):
    """One indexer request. Returns (status, body) — the body is read
    even on HTTP errors, because indexers put their real answer in
    error XML behind 401s."""
    req = urllib.request.Request(url, headers={"User-Agent": _NEWZNAB_UA})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(65536).decode("utf-8", "replace")
        except OSError:
            return e.code, ""
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, str(getattr(e, "reason", e))[:80]


def _newznab_error(body):
    m = re.search(r'<error[^>]*description="([^"]+)"', body or "")
    return m.group(1)[:120] if m else None


def _validate_newznab(url, key):
    """Two live probes against the indexer: caps (is this a newznab
    API?), then an authenticated search (is the KEY good? — caps is
    served WITHOUT auth on major indexers, live-caught 07-23, so caps
    alone would bless a wrong key). None = good, else human-readable."""
    url = _indexer_base(url)
    if not url or "." not in urllib.parse.urlparse(url).netloc:
        return "Enter the indexer's address (e.g. https://api.nzbgeek.info)."
    probes = [url] if url.endswith("/api") else [url + "/api", url]
    qkey = urllib.parse.quote(_clean(key))
    last = "no response"
    for probe in probes:
        status, body = _newznab_get(f"{probe}?t=caps&apikey={qkey}")
        if status == 0:
            last = body or "no response"
            continue
        err = _newznab_error(body)
        if err:
            return f"The indexer says: {err}"
        if "<caps" not in body:
            last = (f"HTTP {status}" if status != 200
                    else "unexpected response (not a newznab API?)")
            continue
        # caps proved the endpoint; now prove the key on an
        # authenticated call. Transport hiccups here don't fail the
        # check — reachability is already established.
        status, body = _newznab_get(
            f"{probe}?t=search&q=test&limit=1&apikey={qkey}")
        err = _newznab_error(body)
        if err:
            return f"The indexer says: {err}"
        return None
    return f"Could not verify the indexer ({last})."


def _validate_usenet(host, port, use_ssl, user, password):
    """A real NNTP sign-in against the provider — greeting, AUTHINFO
    USER/PASS, expect 281. None = good, else a human-readable failure."""
    host = _clean(host, 120)
    if not host:
        return "Enter the news server hostname."
    try:
        port = int(port)
    except (TypeError, ValueError):
        return "Port must be a number (563 for SSL, 119 for plain)."
    try:
        raw = socket.create_connection((host, port), timeout=10)
    except (OSError, TimeoutError) as e:
        return f"Could not connect: {str(e)[:100]}"
    try:
        if use_ssl:
            raw = ssl.create_default_context().wrap_socket(
                raw, server_hostname=host)
        f = raw.makefile("rwb")

        def talk(line=None):
            if line is not None:
                f.write(line.encode() + b"\r\n")
                f.flush()
            return (f.readline() or b"").decode("utf-8", "replace").strip()

        greet = talk()
        if not greet.startswith(("200", "201")):
            return ("The server refused the connection: "
                    f"{greet[:80] or 'no greeting'}")
        resp = talk(f"AUTHINFO USER {_clean(user, 120)}")
        if resp.startswith("381"):
            resp = talk(f"AUTHINFO PASS {_clean(password, 120)}")
        if not resp.startswith("281"):
            return f"Sign-in failed: {resp[:80] or 'no response'}"
        try:
            f.write(b"QUIT\r\n")
            f.flush()
        except OSError:
            pass
        return None
    except ssl.SSLError as e:
        return f"TLS handshake failed: {str(e)[:100]}"
    except (OSError, TimeoutError) as e:
        return f"Connection dropped: {str(e)[:100]}"
    finally:
        try:
            raw.close()
        except OSError:
            pass


def _usenet_host(raw):
    """Forgive real-world paste shapes for the news server: scheme
    prefixes (ssl://, nntps://…) and trailing paths are dropped; an
    embedded :port is split out and returned (None when absent)."""
    h = re.sub(r"^[A-Za-z+]+://", "", _clean(raw, 200)).split("/", 1)[0]
    port = None
    if ":" in h:
        h, _, p = h.partition(":")
        if p.isdigit():
            port = int(p)
    return h.strip(), port


def _stack_inputs(data):
    """Normalize + sanity-check the wizard's inputs — forgivingly: fix
    the paste shapes people actually produce rather than bouncing them.
    Returns (inputs, errors-by-field)."""
    idx, idx_err = _account_resolved(data.get("indexer"), "newznab")
    use, use_err = _account_resolved(data.get("usenet"), "usenet")
    host, embedded_port = _usenet_host(use.get("host"))
    inputs = {
        "media": _clean(data.get("media"), 300),
        "indexer": {"url": _indexer_base(idx.get("url")),
                    # A full pasted API URL often carries the key —
                    # honor it when the key field was left empty.
                    "key": _clean(idx.get("key"))
                    or _indexer_pasted_key(idx.get("url"))},
        "usenet": {"host": host,
                   "port": embedded_port or use.get("port")
                   or (563 if use.get("ssl", True) else 119),
                   "ssl": bool(use.get("ssl", True)),
                   "user": _clean(use.get("user"), 120),
                   "password": _clean(use.get("password"), 120)},
    }
    errors = {}
    if idx_err:
        errors["indexer"] = idx_err
    if use_err:
        errors["usenet"] = use_err
    m = inputs["media"]
    if not m.startswith("/") or ".." in m.split("/"):
        errors["media"] = "Pick an absolute folder on the host."
    return inputs, errors


def op_stack_validate(data):
    """Live-check the wizard's inputs BEFORE anything deploys — the
    green-checks step that makes a failed run near-impossible."""
    inputs, errors = _stack_inputs(data)
    checks = {"media": {"ok": "media" not in errors,
                        "error": errors.get("media")}}
    err = errors.get("indexer") or _validate_newznab(
        inputs["indexer"]["url"], inputs["indexer"]["key"])
    checks["indexer"] = {"ok": not err, "error": err}
    u = inputs["usenet"]
    err = errors.get("usenet") or _validate_usenet(
        u["host"], u["port"], u["ssl"], u["user"], u["password"])
    checks["usenet"] = {"ok": not err, "error": err}
    ok = all(c["ok"] for c in checks.values())
    return {"ok": ok, "error": None if ok
            else "Fix the failing checks and validate again.",
            "checks": checks}


def _stack_save_accounts(data, inputs):
    """The wizard's "Save to Accounts" checkboxes: keep the entered
    details in the vault for next time. Called from op_stack_install
    AFTER validation on purpose — the vault only ever holds accounts
    that just proved they work."""
    if (data.get("indexer") or {}).get("save"):
        i = inputs["indexer"]
        _account_upsert("newznab", _account_host_label(i["url"]),
                        {"url": i["url"], "key": i["key"]})
    if (data.get("usenet") or {}).get("save"):
        u = inputs["usenet"]
        _account_upsert("usenet", u["host"],
                        {k: u[k] for k in
                         ("host", "port", "ssl", "user", "password")})


class _StackAbort(Exception):
    pass


def _stack_install_req(svc, media):
    """The op_install request for one stack member: catalog spec with
    default (pod-dir) volumes, except the shared /data mount which
    points at the user's media folder."""
    spec = resolve_service(svc)
    if not spec:
        raise _StackAbort(f"{svc} is not in the catalog")
    volumes = {
        cpath: (media if cpath == "/data"
                else os.path.join(PODS_DIR, svc, cpath.lstrip("/")))
        for _h, cpath in (spec.get("volumes") or {}).items()
    }
    return {
        "name": svc, "custom": False,
        "image": spec["image"], "command": spec.get("command", ""),
        "ports": spec.get("ports", {}),
        "environment": spec.get("environment", {}),
        "volumes": volumes,
        "restart_policy": spec.get("restart_policy", "unless-stopped"),
        "shares": [], "authkey": "",
        "config_file": spec.get("config_file", ""),
        "config_set": spec.get("config_set") or {},
    }


def _stack_seed_nzbget(usenet):
    """Write the news-server into nzbget's own config. The pod's
    /config is the default pod-dir volume, so this is plain file IO on
    the controller. Seed-once guardrail: an existing non-empty
    Server1.Host is the user's provider — never overwritten."""
    path = os.path.join(PODS_DIR, "nzbget", "config", "nzbget.conf")
    deadline = time.time() + 120
    while not os.path.isfile(path):
        if time.time() > deadline:
            raise _StackAbort("nzbget never wrote its config file")
        time.sleep(3)
    with open(path) as f:
        text = f.read()
    m = re.search(r"^Server1\.Host=(.+)$", text, re.MULTILINE)
    if m and m.group(1).strip():
        return "already configured — left untouched"
    wanted = {
        "Server1.Active": "yes",
        "Server1.Host": usenet["host"],
        "Server1.Port": str(usenet["port"]),
        "Server1.Username": usenet["user"],
        "Server1.Password": usenet["password"],
        "Server1.Encryption": "yes" if usenet["ssl"] else "no",
        "Server1.Connections": "8",
    }
    for k, v in wanted.items():
        line = f"{k}={v}"
        if re.search(rf"^{re.escape(k)}=.*$", text, re.MULTILINE):
            text = re.sub(rf"^{re.escape(k)}=.*$", line.replace("\\", r"\\"),
                          text, count=1, flags=re.MULTILINE)
        else:
            text += "\n" + line
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)
    r = podman("restart", "nzbget", timeout=120)
    if r.returncode != 0:
        raise _StackAbort("nzbget did not restart cleanly")
    return "news server configured"


def _stack_wait_arr(pod):
    """Block until the Arr's API answers: sidecar up, config.xml
    written, /system/status 200. Returns (base, key)."""
    _kind, ver = _arr_kind(pod)
    deadline = time.time() + 240
    while time.time() < deadline:
        entry = network_entry(pod, ps_all())
        port = next(iter(entry["ports"].values()), "")
        key = _arr_api_key(pod)
        if entry.get("ip") and port and key:
            base = f"http://{entry['ip']}:{port}/api/{ver}"
            code, _resp = _arr_req(base, key, "GET", "/system/status")
            if code == 200:
                return base, key
        time.sleep(5)
    raise _StackAbort(f"{pod}'s API never came up")


def _arr_err_detail(resp):
    if isinstance(resp, list) and resp and isinstance(resp[0], dict):
        return ": " + str(resp[0].get("errorMessage", ""))[:120]
    if isinstance(resp, dict) and resp.get("message"):
        return ": " + str(resp["message"])[:120]
    return ""


def _stack_arr_ensure(base, key, path, implementation, name, values,
                      extra=None):
    """Create-or-update one named object (download client / indexer) in
    an Arr from its own schema — the "Tailarr ntfy" convention: we own
    what we name, and never touch anything else. `values` maps schema
    field names to values; a callable value receives the field name
    (used for the *Category fields, whose exact name varies by Arr)."""
    code, schemas = _arr_req(base, key, "GET", path + "/schema")
    if code != 200 or not isinstance(schemas, list):
        raise _StackAbort(f"schema probe failed (HTTP {code})")
    item = next((s for s in schemas
                 if (s.get("implementation") or "").lower()
                 == implementation), None)
    if not item:
        raise _StackAbort(f"no {implementation} type in {path} schema")
    fields = []
    for f in item.get("fields") or []:
        f = dict(f)
        n = f.get("name") or ""
        if n in values:
            f["value"] = values[n]
        else:
            for k, v in values.items():
                if callable(v) and v(n):
                    f["value"] = v(n)
                    break
        fields.append(f)
    payload = dict(item)
    payload.update({"name": name, "enable": True, "fields": fields})
    payload.update(extra or {})
    code, existing = _arr_req(base, key, "GET", path)
    if code != 200 or not isinstance(existing, list):
        raise _StackAbort(f"could not list {path} (HTTP {code})")
    prev = next((x for x in existing if x.get("name") == name), None)
    if prev:
        payload["id"] = prev["id"]
        code, resp = _arr_req(base, key, "PUT",
                              f"{path}/{prev['id']}", payload)
    else:
        code, resp = _arr_req(base, key, "POST", path, payload)
    if code not in (200, 201, 202):
        raise _StackAbort(
            f"rejected (HTTP {code}){_arr_err_detail(resp)}")


def _stack_wire_arr(pod, inputs, nz_ip, nz_auth):
    """Everything one Arr needs: media folders, the download client,
    the indexer. The Arr live-tests each object on save, so success
    here is proof the whole chain connects."""
    root, category = STACK_ARR_ROOTS[pod]
    base, key = _stack_wait_arr(pod)
    r = podman("exec", pod, "sh", "-c",
               f"mkdir -p {root} && "
               f"chown 1000:1000 /data/media {root} 2>/dev/null || true",
               timeout=30)
    if r.returncode != 0:
        raise _StackAbort("could not create the media folder in /data")
    code, roots = _arr_req(base, key, "GET", "/rootfolder")
    if not any(x.get("path") == root for x in (roots or [])
               if isinstance(x, dict)):
        code, resp = _arr_req(base, key, "POST", "/rootfolder",
                              {"path": root})
        if code not in (200, 201):
            raise _StackAbort(
                f"root folder (HTTP {code}){_arr_err_detail(resp)}")
    _stack_arr_ensure(
        base, key, "/downloadclient", "nzbget", "Tailarr nzbget",
        {"host": nz_ip, "port": 6789, "useSsl": False,
         "username": nz_auth.get("user", ""),
         "password": nz_auth.get("password", ""),
         # The category field's exact name varies by Arr (tvCategory /
         # movieCategory) — match by suffix.
         "*category": lambda n: (category
                                 if n.lower().endswith("category")
                                 else None)},
        extra={"protocol": "usenet"})
    idx_url = inputs["indexer"]["url"]
    if idx_url.endswith("/api"):
        idx_url = idx_url[:-4]
    _stack_arr_ensure(
        base, key, "/indexer", "newznab", "Tailarr indexer",
        {"baseUrl": idx_url, "apiKey": inputs["indexer"]["key"]},
        extra={"enableRss": True, "enableAutomaticSearch": True,
               "enableInteractiveSearch": True, "protocol": "usenet"})
    return "folders, download client and indexer connected"


def _stack_worker(key, inputs):
    """The saga: deploy -> start -> seed nzbget -> wire each Arr ->
    optional notifications. Steps stream into .stacks.json for the UI;
    the first failure stops the run with the step's message."""
    spec = STACKS[key]

    def step(skey, fn):
        _stack_step_set(skey, "running")
        try:
            detail = fn()
        except _StackAbort as e:
            _stack_step_set(skey, "failed", str(e))
            raise
        except Exception as e:  # pragma: no cover - belt+braces
            _stack_step_set(skey, "failed", f"unexpected: {e}"[:200])
            raise _StackAbort(str(e))
        _stack_step_set(skey, "ok", detail if isinstance(detail, str)
                        else "")

    try:
        for svc in spec["services"]:
            def _install(svc=svc):
                res = op_install(_stack_install_req(svc, inputs["media"]))
                if not res["ok"]:
                    raise _StackAbort(res.get("error")
                                      or res.get("output", "")[-200:]
                                      or "install failed")
            step(f"install:{svc}", _install)
        for svc in spec["services"]:
            def _start(svc=svc):
                res = op_action(svc, "start")
                if not res["ok"]:
                    raise _StackAbort(res.get("error") or res["status"])
            step(f"start:{svc}", _start)
        step("usenet", lambda: _stack_seed_nzbget(inputs["usenet"]))
        nz_entry = network_entry("nzbget", ps_all())
        nz_auth = _service_auth("nzbget", "nzbget") or {}
        if not nz_entry.get("ip"):
            raise _StackAbort("nzbget has no tailnet address yet")
        for arr in [s for s in spec["services"] if s in STACK_ARR_ROOTS]:
            step(f"wire:{arr}",
                 lambda arr=arr: _stack_wire_arr(
                     arr, inputs, nz_entry["ip"], nz_auth))

        def _notify():
            if not ntfy_client.load_conf():
                return "notifications not set up — skipped"
            wired = []
            for arr in [s for s in spec["services"]
                        if s in STACK_ARR_ROOTS]:
                if op_ntfy_wire(arr).get("ok"):
                    wired.append(arr)
            return ("media alerts on: " + ", ".join(wired)) if wired \
                else "could not wire media alerts (see Notifications)"
        step("notify", _notify)
        _stack_finish("done")
        notify_ops("Magic Stack ready",
                   f"{spec['name']} is deployed and fully wired.",
                   tags=["sparkles"])
    except _StackAbort as e:
        _stack_finish("failed", str(e)[:300])
        notify_ops("Magic Stack failed",
                   f"{spec['name']}: {str(e)[:200]}",
                   priority="high", tags=["rotating_light"])


def _stack_steps_for(spec):
    steps = []
    for svc in spec["services"]:
        steps.append({"key": f"install:{svc}", "label": f"Create {svc}",
                      "state": "pending", "detail": ""})
    for svc in spec["services"]:
        steps.append({"key": f"start:{svc}", "label": f"Start {svc}",
                      "state": "pending", "detail": ""})
    steps.append({"key": "usenet", "label": "Connect your usenet account",
                  "state": "pending", "detail": ""})
    for arr in [s for s in spec["services"] if s in STACK_ARR_ROOTS]:
        steps.append({"key": f"wire:{arr}",
                      "label": f"Wire {arr} (folders, downloader, indexer)",
                      "state": "pending", "detail": ""})
    steps.append({"key": "notify", "label": "Media notifications",
                  "state": "pending", "detail": ""})
    return steps


def op_stack_install(data):
    """Kick off a stack run. Refuses when a run is active, when the
    greenfield guardrail is tripped, or when validation fails — the
    saga only ever starts from a fully green state."""
    key = (data.get("stack") or "").strip()
    spec = STACKS.get(key)
    if not spec:
        return {"ok": False, "error": "Unknown stack."}
    run = load_stack_run()
    if run and run["state"] == "running":
        return {"ok": False, "error": "A stack setup is already running."}
    blockers = stack_blockers(spec)
    if blockers:
        return {"ok": False, "error":
                "Magic Stacks can't recreate existing services ("
                + ", ".join(blockers) + ")."}
    v = op_stack_validate(data)
    if not v["ok"]:
        return {"ok": False, "error": v["error"], "checks": v["checks"]}
    inputs, _errs = _stack_inputs(data)
    _stack_save_accounts(data, inputs)
    with _stack_lock:
        _save_stack_run({"stack": key, "state": "running",
                         "started": int(time.time()), "finished": None,
                         "error": None, "steps": _stack_steps_for(spec)})
    threading.Thread(target=_stack_worker, args=(key, inputs),
                     daemon=True).start()
    return {"ok": True, "error": None}


# =========================================================================
# Push wakes (v0.26.0) — real background push for the Tailarr app via the
# vendor relay (push.tailarr.com, repo scs32/tailarr-relay). The relay is
# content-free: the controller tells it only "wake device token X", the
# device's Notification Service Extension then fetches the composed
# message from THIS server over the tailnet — no payloads ever transit
# third-party infrastructure. Registration is whois-authenticated via the
# self-config gateway (POST /self/push-token); registering a token IS the
# consent for wakes. Fan-out mirrors the ntfy stream: every message on a
# topic wakes each reader's registered devices, so anything that already
# notifies (Arr media events, ops alerts) gains push with no new hooks.
# =========================================================================
PUSH_FILE = os.path.join(PODS_DIR, ".push.json")
DEFAULT_PUSH_RELAY = "https://push.tailarr.com"
PUSH_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{32,200}$")
_push_lock = threading.Lock()
_push_recent = {}  # token -> last wake unix ts (burst coalescing)


def load_push():
    try:
        with open(PUSH_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_push(data):
    _write_secret(PUSH_FILE, json.dumps(data, indent=2))


def op_person_push(uid, data):
    """Register/unregister one device's raw APNs token for a person —
    the gateway forwards POST /self/push-token here after whois. Capped
    per person; same-token re-register is an idempotent refresh."""
    token = str(data.get("token") or "").strip().lower()
    if not PUSH_TOKEN_RE.match(token):
        return {"ok": False, "error": "bad device token"}
    do = (data.get("do") or "register").strip()
    if do not in ("register", "unregister"):
        return {"ok": False, "error": "unknown action"}
    with _push_lock:
        reg = load_push()
        tokens = reg.setdefault("tokens", {})
        toks = [t for t in (tokens.get(uid) or [])
                if t.get("token") != token]
        if do == "register":
            toks.append({"token": token,
                         "sandbox": bool(data.get("sandbox")),
                         "added": int(time.time())})
            toks = toks[-10:]
        if toks:
            tokens[uid] = toks
        else:
            tokens.pop(uid, None)
        save_push(reg)
    return {"ok": True, "error": None,
            "registered": do == "register", "count": len(toks)}


def _push_relay_url():
    return (load_push().get("relay") or DEFAULT_PUSH_RELAY).rstrip("/")


def _push_wake_token(entry):
    """One content-free wake via the public relay. Returns None, "gone"
    (the device unregistered — prune it), or an error string."""
    body = json.dumps({"token": entry["token"],
                       "sandbox": bool(entry.get("sandbox")),
                       "kind": "alert"}).encode()
    req = urllib.request.Request(
        _push_relay_url() + "/wake", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10):
            return None
    except urllib.error.HTTPError as e:
        try:
            if (json.load(e) or {}).get("gone"):
                return "gone"
        except ValueError:
            pass
        return f"relay HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return f"relay unreachable: {e}"


def _push_handle_topic(topic):
    """Wake every registered device of every person whose badges read
    this topic. Bursts coalesce (one wake per token per 10s — the app
    fetches everything new anyway); dead tokens are pruned."""
    reg = load_push()
    tokens = reg.get("tokens") or {}
    if not tokens:
        return
    people = load_people()
    now = time.time()
    gone = set()
    for uid, person in people.items():
        if topic not in _ntfy_person_topics(person):
            continue
        for entry in tokens.get(uid) or []:
            tok = entry["token"]
            if now - _push_recent.get(tok, 0) < 10:
                continue
            _push_recent[tok] = now
            err = _push_wake_token(entry)
            if err == "gone":
                gone.add(tok)
            elif err:
                print(f"push wake: {err}")
    if gone:
        with _push_lock:
            reg = load_push()
            toks = reg.get("tokens") or {}
            for uid in list(toks):
                toks[uid] = [t for t in toks[uid]
                             if t.get("token") not in gone]
                if not toks[uid]:
                    toks.pop(uid)
            save_push(reg)


def _push_waker_loop():
    """Mirror the ntfy stream into wakes. Subscribes to every tailarr
    topic with the admin account (ntfy has no topic wildcards, so the
    set is enumerated and refreshed by reconnecting every 15 minutes);
    each message event wakes that topic's readers. Idle when push has
    no registrations or notifications aren't set up."""
    while True:
        try:
            conf = ntfy_client.load_conf()
            if not conf or not (load_push().get("tokens") or {}):
                time.sleep(30)
                continue
            base = _ntfy_internal_url(_discover_ntfy())
            if not base:
                time.sleep(30)
                continue
            topics = [ntfy_client.OPS_TOPIC] + \
                [ntfy_client.MEDIA_TOPIC_PREFIX + s
                 for s in _shareable_services()]
            admin = conf.get("admin") or {}
            if admin.get("token"):
                auth = f"Bearer {admin['token']}"
            else:
                auth = "Basic " + base64.b64encode(
                    f"{admin.get('user', '')}:{admin.get('password', '')}"
                    .encode()).decode()
            req = urllib.request.Request(
                f"{base.rstrip('/')}/{','.join(topics)}/json",
                headers={"Authorization": auth})
            deadline = time.time() + 900
            # 60s read timeout > ntfy's 45s keepalive cadence: a healthy
            # stream never times out; a wedged one reconnects.
            with urllib.request.urlopen(req, timeout=60) as r:
                for line in r:
                    if time.time() > deadline:
                        break
                    try:
                        ev = json.loads(line.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    if ev.get("event") == "message" and ev.get("topic"):
                        _push_handle_topic(ev["topic"])
        except Exception as e:  # the waker must never die
            print(f"push waker: {e}")
        time.sleep(10)


_notify_state_lock = threading.Lock()


def _notify_send(title, message, priority, tags):
    conf = ntfy_client.load_conf()
    if not conf:
        return
    err = ntfy_client.publish(conf, _ntfy_internal_url(_discover_ntfy()),
                              ntfy_client.OPS_TOPIC, title, message,
                              priority=priority, tags=tags)
    with _notify_state_lock:
        state = ntfy_client.load_state()
        state["last_publish_error"] = err
        state["last_publish_at"] = time.time()
        ntfy_client.save_state(state)


def notify_ops(title, message, priority="default", tags=None):
    """Fire-and-forget an admin (ops-topic) notification.

    A daemon thread + a never-raising publish: a down or unconfigured
    ntfy must never break the operation that triggered the event. The
    last failure is recorded for the Notifications page banner (an alert
    about ntfy itself being down is undeliverable by definition)."""
    if not ntfy_client.load_conf():
        return
    threading.Thread(target=_notify_send,
                     args=(title, message, priority, tags),
                     daemon=True).start()


def _notify_health_pass():
    """Publish pod-state / identity-tag transitions (maintenance loop).

    De-dup lives in .notify-state.json: alerts fire on transitions only,
    and a pod must look bad for TWO consecutive passes before its down
    alert (a restart window must not page). Recoveries notify once."""
    if not ntfy_client.load_conf():
        return
    ps = ps_all()
    with _notify_state_lock:
        state = ntfy_client.load_state()
    seen = state.get("pods") or {}
    pending = state.get("pending") or {}
    idents = state.get("identity") or {}
    new_seen, new_pending, new_idents = {}, {}, {}
    for name in deployed_services():
        cur = pod_state(name, ps)
        known = seen.get(name)
        if known is None:
            new_seen[name] = cur  # first sighting: baseline, no alert
        elif cur == known:
            new_seen[name] = known
        elif cur != "running":
            n = pending.get(name, 0) + 1
            if n >= 2:
                notify_ops(f"{name} is {cur}",
                           f"The {name} pod has been {cur} for two checks.",
                           priority="high", tags=["rotating_light"])
                new_seen[name] = cur
            else:
                new_pending[name] = n
                new_seen[name] = known
        else:
            notify_ops(f"{name} recovered",
                       f"The {name} pod is running again.",
                       tags=["white_check_mark"])
            new_seen[name] = cur
        ident = _tag_state.get(name, "unknown")
        new_idents[name] = ident
        if ident == "missing" and idents.get(name) not in (None, "missing"):
            notify_ops(f"{name} identity tag missing",
                       f"{name}'s sidecar lost tag:tailarr-svc-{name} — "
                       "user devices are being filtered.",
                       priority="high", tags=["warning"])
    res = _upgrade_last_result()
    if res and res.get("finished") and \
            res.get("finished") != state.get("upgrade_seen"):
        if res.get("rolled_back"):
            notify_ops("Controller upgrade rolled back",
                       f"Upgrade to {res.get('to', '?')} failed its health "
                       "check and rolled back.", priority="high",
                       tags=["rotating_light"])
        elif res.get("ok"):
            notify_ops("Controller upgraded",
                       f"Now running {res.get('to', '?')}.",
                       tags=["white_check_mark"])
        state["upgrade_seen"] = res.get("finished")
    state.update({"pods": new_seen, "pending": new_pending,
                  "identity": new_idents})
    with _notify_state_lock:
        merged = ntfy_client.load_state()
        merged.update(state)
        ntfy_client.save_state(merged)


def op_share_add(name, host_path, container_path, ro):
    """Add a share to the registry. Returns a result dict."""
    shares = load_shares()
    name = (name or "").strip()
    raw_host = (host_path or "").strip()
    cont = (container_path or "").strip() or raw_host
    host = raw_host.rstrip("/") or "/"
    cont = cont.rstrip("/") or "/"

    if not NAME_RE.fullmatch(name):
        return {"ok": False, "name": name, "error": "Invalid name (a-z, 0-9, dashes)."}
    if name in shares:
        return {"ok": False, "name": name, "error": f"Share '{name}' already exists."}
    if not host.startswith("/") or host.endswith(":ro"):
        return {"ok": False, "name": name,
                "error": "Host path must be absolute (use the checkbox for read-only)."}
    if not cont.startswith("/"):
        return {"ok": False, "name": name, "error": "Container path must be absolute."}

    shares[name] = {"host_path": host, "container_path": cont, "ro": bool(ro)}
    save_shares(shares)
    _sync_mounts_dropin()  # boot unit waits for this share's backing mount
    return {"ok": True, "name": name, "error": None,
            "message": f"Added share '{name}'.", "share": shares[name]}


def op_share_delete(name):
    shares = load_shares()
    gone = shares.pop(name, None)
    if gone is None:
        return {"ok": False, "name": name, "error": "Unknown share."}
    save_shares(shares)
    _sync_mounts_dropin()
    extra = ""
    if gone.get("nfs"):
        ok, out, _ = _nfs_apply()  # registry already saved: drops its export
        extra = (" Its NFS export was removed." if ok
                 else f" NFS export cleanup failed: {out}")
    return {"ok": True, "name": name, "error": None,
            "message": f"Deleted share '{name}'. Pods that mount it keep their volume"
                       f" until re-rendered.{extra}"}


# =========================================================================
# NFS exports (share media OUT of the VM, e.g. to a native Plex on the Mac
# hosting it — the recommended macOS layout keeps media on a VM-local disk)
# =========================================================================
def _render_exports(shares):
    """The /etc/exports.d fragment for every NFS-enabled share.

    Read-only exports squash everyone to nobody; read-write maps to the
    catalog's PUID/PGID 1000 so pod-written files stay editable. `insecure`
    lets macOS mount without forcing a reserved source port (Finder's
    Connect to Server doesn't use one)."""
    lines = ["# Managed by Tailarr (Shares page) - do not edit; changes are",
             "# overwritten on every apply. Source: " + EXPORTS_FRAGMENT]
    for name, s in sorted(shares.items()):
        nfs = s.get("nfs")
        if not nfs:
            continue
        if nfs.get("ro", True):
            opts = "ro,all_squash,insecure"
        else:
            opts = "rw,all_squash,anonuid=1000,anongid=1000,insecure"
        specs = " ".join(f"{c}({opts})" for c in nfs["clients"].split())
        lines.append(f"{s['host_path']} {specs}")
    return "\n".join(lines) + "\n"


def _host_exec(helper, script, timeout=60):
    """Run a shell script on the HOST via a one-shot privileged helper.

    The controller is containerized, but some state it manages lives on
    the actual host (kernel NFS exports, systemd drop-ins). The helper
    runs the controller's own image with --pid=host and nsenter's into
    PID 1's namespaces, so `script` executes on the host proper. Returns
    (rc, output); rc -1 = podman/controller unavailable (dev/CI runs)."""
    name = _controller_name()
    if not name:
        return -1, ("podman (or the controller container) is not "
                    "reachable - cannot touch the host.")
    ins = podman("inspect", name, "--format", "{{.ImageName}}", timeout=30)
    image = ins.stdout.strip()
    if ins.returncode != 0 or not image:
        return -1, "Could not determine the controller image."
    r = podman("run", "--rm", "--name", helper,
               "--privileged", "--pid=host", image,
               "nsenter", "-t", "1", "-m", "-n", "-u", "--",
               "sh", "-c", script, timeout=timeout)
    return r.returncode, r.stdout + r.stderr


# =========================================================================
# Host folder browser (/api/fs) — backs the FolderEditor "Browse" popover
# =========================================================================
# The controller container only mounts PODS_DIR + the podman socket, so it
# cannot see arbitrary podman-host paths (the ones pods actually bind-mount).
# Browse via a one-shot container with the host root at /host-root instead of
# _host_exec's nsenter, which is EPERM on apple/container.
FS_ROOT = "/host-root"


def _fs_exec(script, rw=False, timeout=30):
    """Run `script` with the podman host's / at /host-root (read-only unless
    rw). Returns (CompletedProcess, error) — error set means podman/the
    controller image is unreachable (dev/CI runs)."""
    name = _controller_name()
    if not name:
        return None, ("podman (or the controller container) is not "
                      "reachable - cannot browse host folders.")
    ins = podman("inspect", name, "--format", "{{.ImageName}}", timeout=30)
    image = ins.stdout.strip()
    if ins.returncode != 0 or not image:
        return None, "Could not determine the controller image."
    r = podman("run", "--rm", "-v", f"/:{FS_ROOT}" + ("" if rw else ":ro"),
               image, "sh", "-c", script, timeout=timeout)
    return r, None


def _fs_path(raw):
    """Validate/normalize a browse path. Returns (path, error)."""
    p = (raw or "").strip()
    if not p.startswith("/"):
        return None, "Path must be absolute."
    p = os.path.normpath(p)
    while p.startswith("//"):  # normpath keeps a leading double slash
        p = p[1:]
    if ".." in p.split("/"):
        return None, "Path may not contain '..'."
    return p, None


def op_fs_list(raw):
    """Child directories of a podman-host path (dirs only, dotdirs hidden)."""
    path, err = _fs_path(raw)
    if err:
        return {"ok": False, "path": raw, "parent": None, "dirs": [], "error": err}
    q = shlex.quote(FS_ROOT + ("" if path == "/" else path))
    script = (f'cd {q} 2>/dev/null || {{ echo TAILARR-FS-NODIR >&2; exit 3; }}\n'
              'for d in */; do [ -d "$d" ] && printf \'%s\\n\' "${d%/}"; done\n'
              'exit 0\n')
    r, err = _fs_exec(script)
    if err:
        return {"ok": False, "path": path, "parent": None, "dirs": [], "error": err}
    if "TAILARR-FS-NODIR" in r.stderr:
        return {"ok": False, "path": path, "parent": None, "dirs": [],
                "error": "Folder not found on host."}
    if r.returncode != 0:
        return {"ok": False, "path": path, "parent": None, "dirs": [],
                "error": (r.stderr or r.stdout or "listing failed").strip()[-300:]}
    dirs = sorted(line for line in r.stdout.splitlines() if line)
    parent = None if path == "/" else (os.path.dirname(path) or "/")
    return {"ok": True, "path": path, "parent": parent, "dirs": dirs, "error": None}


def op_fs_mkdir(raw):
    """Create a folder on the podman host (mkdir -p)."""
    path, err = _fs_path(raw)
    if err:
        return {"ok": False, "path": raw, "error": err}
    if path == "/":
        return {"ok": False, "path": path, "error": "Path must name a folder."}
    r, err = _fs_exec(f"mkdir -p {shlex.quote(FS_ROOT + path)}", rw=True)
    if err:
        return {"ok": False, "path": path, "error": err}
    if r.returncode != 0:
        return {"ok": False, "path": path,
                "error": (r.stderr or r.stdout or "mkdir failed").strip()[-300:]}
    return {"ok": True, "path": path, "error": None}


_host_platform_cache = None


def host_platform():
    """Platform fact from .host.json: apple-container | linux | unknown.
    Absence is NOT cached — the controller backfill may land later."""
    global _host_platform_cache
    if _host_platform_cache is None:
        try:
            with open(HOST_FILE) as f:
                _host_platform_cache = json.load(f).get("platform") or "unknown"
        except (OSError, ValueError):
            return "unknown"
    return _host_platform_cache


# The kernel command line is VM-global and NOT namespaced — readable from
# inside any container on the VM's kernel. apple/container VMs boot with
# init=/sbin/vminitd on it. This is THE reliable in-container signal: the
# original /proc/1/comm check could never work (containers get their own
# PID namespace, so PID 1 is the container's own command — live-caught
# 2026-07-21 when a fresh install detected "linux" with pid1=sleep).
CMDLINE_PATH = "/proc/cmdline"


def _cmdline_platform():
    """apple-container | linux | "" (unreadable) from the kernel cmdline."""
    try:
        with open(CMDLINE_PATH) as f:
            cmdline = f.read()
    except OSError:
        return ""
    return ("apple-container" if "init=/sbin/vminitd" in cmdline
            else "linux")


def _detect_host_platform():
    """Write (or CORRECT) .host.json from the kernel command line.

    Runs at every controller start, in-process — no helper container.
    Correction matters: every v0.13.0–v0.15.2 install has a wrong
    "linux" verdict on apple/container (the pid1 check saw the
    container's own init), which silently disabled the whole peer-relay
    offer there."""
    detected = _cmdline_platform()
    if not detected:
        return  # cmdline unreadable (odd CI sandbox) — keep whatever exists
    existing = None
    try:
        with open(HOST_FILE) as f:
            existing = json.load(f).get("platform")
    except (OSError, ValueError):
        pass
    if existing == detected:
        return
    global _host_platform_cache
    _host_platform_cache = detected
    tmp = HOST_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"platform": detected,
                   "detected_at": int(time.time()),
                   "detected_by": "controller-cmdline",
                   "corrected_from": existing}, f, indent=2)
    os.replace(tmp, HOST_FILE)
    os.chmod(HOST_FILE, 0o600)
    print(f"host platform {'corrected' if existing else 'detected'}: "
          f"{detected}" + (f" (was {existing})" if existing else ""))


def _sync_mounts_dropin():
    """Order the boot unit after every share's backing mount (drop-in).

    Media commonly lives on its own disk mounted `nofail`: systemd can
    reach network-online (and start the fleet) before that mount lands,
    and podman happily bind-mounts the EMPTY mountpoint directory — pods
    come up with no media until a manual restart (reproduced in the
    field). RequiresMountsFor makes tailarr-pods.service pull in and wait
    for the mount units backing every registered share; for paths that
    aren't separate mounts it's satisfied trivially. Written as a drop-in
    so it survives bootstrap re-runs and never clobbers user overrides.
    Best-effort: silently skipped when the host has no systemd unit."""
    if host_platform() == "apple-container":
        return  # vminitd init: no systemd, ever — skip the privileged helper
    paths = [PODS_DIR] + sorted(s["host_path"]
                                for s in load_shares().values())
    # Rendered into a shell script and a space-separated unit setting:
    # skip anything that can't survive either encoding.
    paths = [p for p in dict.fromkeys(paths)
             if p and not re.search(r"[\s'\"]", p)]
    content = ("# Managed by Tailarr - regenerated whenever shares change.\n"
               "[Unit]\n"
               f"RequiresMountsFor={' '.join(paths)}\n")
    script = (
        "[ -f /etc/systemd/system/tailarr-pods.service ] || exit 0; "
        f"mkdir -p {UNIT_DROPIN_DIR} && "
        f"printf '%s' '{content}' > {UNIT_DROPIN_DIR}/50-tailarr-mounts.conf "
        "&& systemctl daemon-reload"
    )
    rc, out = _host_exec(MOUNTS_HELPER, script)
    if rc > 0:
        print(f"mounts drop-in sync failed: {out.strip()}")


def _nfs_apply():
    """Install the exports fragment on the HOST and (re)load kernel nfsd.

    NFS is served by the VM's kernel, not by any container. The controller
    writes the fragment under PODS_DIR (a host path, mounted 1:1); the
    host-side helper copies it into /etc/exports.d and runs exportfs.
    Returns (ok, output, host_ip)."""
    with open(EXPORTS_FRAGMENT, "w") as f:
        f.write(_render_exports(load_shares()))

    host_script = (
        "set -e; "
        "command -v exportfs >/dev/null 2>&1 || "
        "{ echo NFS-SERVER-MISSING; exit 9; }; "
        f"mkdir -p /etc/exports.d && cp {EXPORTS_FRAGMENT} {EXPORTS_HOST_FILE}; "
        "systemctl enable --now nfs-server >/dev/null 2>&1 || true; "
        "exportfs -ra; "
        "echo EXPORTS:; exportfs; "
        "echo HOSTIP: $(hostname -I 2>/dev/null)"
    )
    rc, out = _host_exec(NFS_HELPER, host_script)
    if "NFS-SERVER-MISSING" in out or rc == 9:
        return False, ("The host has no NFS server. On the VM, run:\n"
                       "  apt install -y nfs-kernel-server\n"
                       "then toggle the export again."), ""
    if rc != 0:
        return False, out, ""
    ip = ""
    m = re.search(r"HOSTIP:\s*(\S+)", out)
    if m:
        ip = m.group(1)
    return True, out, ip


def op_share_nfs(name, enabled, clients, ro):
    """Enable/disable an NFS export for a share. Returns a result dict."""
    shares = load_shares()
    share = shares.get(name or "")
    if not share:
        return {"ok": False, "name": name, "error": "Unknown share."}

    if enabled:
        tokens = (clients or "").split()
        if not tokens:
            return {"ok": False, "name": name,
                    "error": "Allowed clients required - an IP, a CIDR like "
                             "192.168.1.0/24, or a hostname (space-separated "
                             "for several)."}
        bad = [t for t in tokens if not NFS_CLIENT_RE.fullmatch(t)]
        if bad:
            return {"ok": False, "name": name,
                    "error": f"Invalid client spec: {' '.join(bad)}"}
        share["nfs"] = {"clients": " ".join(tokens), "ro": bool(ro)}
    else:
        if share.pop("nfs", None) is None:
            return {"ok": False, "name": name,
                    "error": "This share has no NFS export."}
    save_shares(shares)

    ok, out, ip = _nfs_apply()
    if not ok:
        return {"ok": False, "name": name, "error": out,
                "message": None, "output": out}
    if enabled:
        where = ip or "<vm-ip>"
        msg = (f"NFS export live. Mount from an allowed client: "
               f"nfs://{where}{share['host_path']}  (Finder: Go > Connect "
               f"to Server, or: mount -t nfs -o resvport "
               f"{where}:{share['host_path']} <mountpoint>)")
    else:
        msg = f"NFS export removed for '{name}'."
    return {"ok": True, "name": name, "error": None,
            "message": msg, "output": out}


def op_source_add(name, url):
    """Register an external catalog source. Fetches it first to validate."""
    sources = load_sources()
    name = (name or "").strip()
    url = (url or "").strip()
    if not NAME_RE.fullmatch(name):
        return {"ok": False, "name": name, "error": "Invalid name (a-z, 0-9, dashes)."}
    if name in sources:
        return {"ok": False, "name": name, "error": f"Source '{name}' already exists."}
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"ok": False, "name": name, "error": "URL must start with http:// or https://."}
    services, err = fetch_catalog(url, force=True)
    if err:
        return {"ok": False, "name": name, "error": f"Could not load catalog: {err}"}
    sources[name] = {"url": url}
    save_sources(sources)
    return {"ok": True, "name": name, "error": None,
            "message": f"Added source '{name}' ({len(services)} services)."}


def op_source_delete(name):
    sources = load_sources()
    s = sources.pop(name, None)
    if s is None:
        return {"ok": False, "name": name, "error": "Unknown source."}
    save_sources(sources)
    _catalog_cache.pop(s["url"], None)
    return {"ok": True, "name": name, "error": None,
            "message": f"Deleted source '{name}'."}


def op_attach(pod, sname):
    """Attach a share to an already-deployed pod. Returns a result dict."""
    shares = load_shares()
    share = shares.get(sname)
    if not share or pod not in deployed_services() or pod in CONTROLLER_PODS:
        return {"ok": False, "pod": pod, "share": sname, "error": "Unknown pod or share.",
                "output": ""}
    info = pod_config(pod)
    if not info:
        return {"ok": False, "pod": pod, "share": sname, "output": "",
                "error": f"No readable .config.json for {pod} (redeploy once to create it)."}
    if sname in info.get("shares", []):
        return {"ok": False, "pod": pod, "share": sname, "output": "",
                "error": f"'{sname}' is already attached to {pod}."}
    cpath, host = share_volume(share)
    if cpath in info.get("volumes", {}):
        return {"ok": False, "pod": pod, "share": sname, "output": "",
                "error": f"{pod} already mounts something at {cpath}."}

    config = config_from_info(info)
    config["volumes"][cpath] = host
    config["shares"] = sorted(config["shares"] + [sname])
    result = run_create(config)
    output = result.stdout + result.stderr
    if result.returncode != 0:
        return {"ok": False, "pod": pod, "share": sname, "output": output,
                "error": f"attach {sname} to {pod}: FAILED"}
    return {"ok": True, "pod": pod, "share": sname, "output": output, "error": None,
            "message": f"attach {sname} to {pod}: ok"}


# =========================================================================
# JSON API
# =========================================================================
def api_get(path):
    if path == "/api/info":
        latest = load_release().get("latest", "")
        return 200, {"api_version": 1,
                     "pods_dir": PODS_DIR,
                     "controller_pods": sorted(CONTROLLER_PODS),
                     "version": VERSION,
                     "upgrade_available": bool(latest)
                     and _ver_key(latest) > _ver_key(VERSION),
                     "tsapi": status_tsapi(),
                     "host_platform": host_platform(),
                     "relay": status_relay()}
    if path == "/api/controller/upgrade":
        return 200, upgrade_status()
    if path == "/api/relay":
        return 200, status_relay()
    if path == "/api/relay/devices":
        return 200, status_relay_devices()
    if path == "/api/tsapi":
        return 200, status_tsapi()
    if path == "/api/pods":
        maybe_check_updates()  # daily background refresh, piggybacked here
        return 200, {"pods": status_pods()}
    if path == "/api/catalog":
        return 200, {"catalog": status_catalog()}
    if path == "/api/updates":
        data = load_updates()
        return 200, {"checking": _updates_running,
                     "checked": data.get("checked", 0),
                     "images": data.get("images", {})}
    if path == "/api/network":
        return 200, {"network": status_network()}
    if path == "/api/monitor":
        return 200, status_monitor()
    if path == "/api/ntfy":
        return 200, status_ntfy()
    if path == "/api/stacks":
        return 200, status_stacks()
    if path == "/api/stats":
        return 200, status_stats()
    if path == "/api/users":
        return 200, status_users()
    if path == "/api/tokens":
        return 200, status_tokens()
    if path == "/api/registries":
        return 200, status_registries()

    if path == "/api/accounts":
        return 200, status_accounts()
    if path == "/api/shares":
        return 200, {"shares": status_shares()}
    if path == "/api/sources":
        return 200, {"sources": status_sources(),
                     "catalogs": status_catalogs()}
    m = re.fullmatch(r"/api/pods/([a-z0-9][a-z0-9-]*)/logs", path)
    if m:
        return 200, op_action(m.group(1), "logs")
    m = re.fullmatch(r"/api/pods/([a-z0-9][a-z0-9-]*)/backups", path)
    if m:
        return 200, {"name": m.group(1), "backups": status_backups(m.group(1))}
    m = re.fullmatch(r"/api/pods/([a-z0-9][a-z0-9-]*)/config", path)
    if m:
        result = op_pod_config(m.group(1))
        return (200 if result["ok"] else 404), result
    return 404, {"error": "not found"}


def _install_req_from_json(data):
    """Build an op_install request from a JSON API payload."""
    name = (data.get("service") or data.get("name") or "").strip()
    if data.get("custom"):
        return {
            "name": name, "custom": True,
            "image": data.get("image", ""), "command": data.get("command", ""),
            "ports": data.get("ports", {}), "environment": data.get("environment", {}),
            "volumes": data.get("volumes", {}),
            "restart_policy": "unless-stopped",
            "shares": data.get("shares", []),
            "authkey": data.get("authkey", ""),
            "config_file": data.get("config_file", ""),
            "config_set": data.get("config_set", {}),
        }, None
    spec = resolve_service(name)
    if not spec:
        return None, "Unknown service."
    volumes = data.get("volumes")
    if volumes is None:
        volumes = {
            cpath: os.path.join(PODS_DIR, name, cpath.lstrip("/"))
            for _, cpath in spec.get("volumes", {}).items()
        }
    return {
        "name": name, "custom": False,
        "image": spec["image"], "command": spec.get("command", ""),
        "ports": data.get("ports", spec.get("ports", {})),
        "environment": {**spec.get("environment", {}), **data.get("environment", {})},
        "volumes": volumes,
        "restart_policy": spec.get("restart_policy", "unless-stopped"),
        "shares": data.get("shares", []),
        "authkey": data.get("authkey", ""),
        "config_file": spec.get("config_file", ""),
        "config_set": spec.get("config_set") or {},
    }, None


def api_post(path, data):
    if path == "/api/install":
        req, err = _install_req_from_json(data)
        if err:
            return 400, {"ok": False, "error": err}
        result = op_install(req)
        code = 200 if result["ok"] else (400 if result.get("error") else 500)
        return code, result

    m = re.fullmatch(r"/api/pods/([a-z0-9][a-z0-9-]*)/action", path)
    if m:
        result = op_action(m.group(1), (data.get("do") or "").strip())
        code = 200 if result["ok"] else (409 if result.get("status") == "busy" else 400)
        return code, result

    m = re.fullmatch(r"/api/pods/([a-z0-9][a-z0-9-]*)/exec", path)
    if m:
        result = op_exec(m.group(1), data.get("cmd") or "")
        code = 200 if result["ok"] else (409 if result.get("status") == "busy" else 400)
        return code, result

    m = re.fullmatch(r"/api/pods/([a-z0-9][a-z0-9-]*)/backups", path)
    if m:
        result = op_backup(m.group(1), data.get("reason") or "")
        code = 200 if result["ok"] else (409 if result.get("status") == "busy" else 400)
        return code, result

    m = re.fullmatch(r"/api/pods/([a-z0-9][a-z0-9-]*)/backups/restore", path)
    if m:
        result = op_backup_restore(m.group(1), (data.get("ts") or "").strip())
        code = 200 if result["ok"] else (409 if result.get("status") == "busy" else 400)
        return code, result

    m = re.fullmatch(r"/api/pods/([a-z0-9][a-z0-9-]*)/backups/delete", path)
    if m:
        result = op_backup_delete(m.group(1), (data.get("ts") or "").strip())
        return (200 if result["ok"] else 400), result

    m = re.fullmatch(r"/api/pods/([a-z0-9][a-z0-9-]*)/config", path)
    if m:
        result = op_reconfigure(m.group(1), data)
        code = 200 if result["ok"] else (409 if result.get("status") == "busy" else 400)
        return code, result

    if path == "/api/fleet":
        result = op_fleet((data.get("do") or "").strip())
        return (200 if result["ok"] else 400), result

    if path == "/api/controller/upgrade":
        result = op_controller_upgrade(data)
        code = 200 if result["ok"] else (409 if result.get("status") == "busy" else 400)
        return code, result

    if path == "/api/controller/upgrade/check":
        latest = _check_release()  # synchronous, ~1 network call, 10s cap
        status = upgrade_status()
        status["ok"] = latest is not None
        if latest is None:
            status["error"] = ("Could not reach the release list "
                               "(offline, or GitHub rate limit) — try later.")
        return (200 if status["ok"] else 502), status

    if path == "/api/updates/refresh":
        return 200, {"ok": True, "status": maybe_check_updates(force=True)}

    m = re.fullmatch(r"/api/network/([a-z0-9][a-z0-9-]*)", path)
    if m:
        result = op_network_set(m.group(1), data)
        code = 200 if result["ok"] else (409 if result.get("status") == "busy" else 400)
        return code, result

    if path == "/api/users/keys":
        result = op_user_key()
        return (200 if result["ok"] else 400), result

    if path == "/api/people":
        result = op_person(data)
        return (200 if result["ok"] else 400), result

    m = re.fullmatch(r"/api/people/([a-f0-9]+)/access", path)
    if m:
        result = op_person_access(m.group(1),
                                  (data.get("service") or "").strip(),
                                  bool(data.get("allow")))
        return (200 if result["ok"] else 400), result

    m = re.fullmatch(r"/api/people/([a-f0-9]+)/notifications", path)
    if m:
        result = op_person_notify(m.group(1))
        return (200 if result["ok"] else 400), result

    if path == "/api/tokens":
        do = data.get("do") or ""
        if do == "create":
            result = op_token_create(data.get("label") or "")
        elif do == "delete":
            result = op_token_delete(data.get("id") or "")
        elif do == "require":
            result = op_token_require(bool(data.get("enabled")))
        else:
            result = {"ok": False, "error": f"unknown do '{do}'"}
        return (200 if result["ok"] else 400), result

    if path == "/api/registries":
        do = data.get("do") or ""
        if do == "save":
            result = op_registry_save(data)
        elif do == "delete":
            result = op_registry_delete((data.get("registry") or "").strip().lower())
        else:
            result = {"ok": False, "error": f"unknown do '{do}'"}
        return (200 if result["ok"] else 400), result

    if path == "/api/accounts":
        do = (data.get("do") or "").strip()
        if do == "save":
            result = op_account_save(data)
        elif do == "delete":
            result = op_account_delete(data)
        else:
            result = {"ok": False, "error": f"unknown do '{do}'"}
        return (200 if result["ok"] else 400), result

    if path == "/api/relay":
        result = op_relay((data.get("do") or "").strip(), data)
        return (200 if result["ok"] else 400), result

    # --- credential wizard (Settings) ---
    if path == "/api/tsapi/validate":
        return 200, op_tsapi_validate(data)  # per-capability result body

    if path == "/api/tsapi/fences":
        result = op_policy_init_fences()
        return (200 if result["ok"] else 400), result

    if path == "/api/tsapi":
        result = op_tsapi_save(data)
        return (200 if result["ok"] else 400), result

    # Must precede the /api/users/<id> match — "adopt" is a valid node-ID
    # shape.
    if path == "/api/users/adopt":
        result = op_user_adopt(data.get("id") or "")
        return (200 if result["ok"] else 400), result

    m = re.fullmatch(r"/api/users/([A-Za-z0-9]+)/access", path)
    if m:
        result = op_user_access(m.group(1), (data.get("service") or "").strip(),
                                bool(data.get("allow")))
        return (200 if result["ok"] else 400), result

    m = re.fullmatch(r"/api/users/([A-Za-z0-9]+)", path)
    if m:
        result = op_user_nick(m.group(1), data.get("nickname"))
        return (200 if result["ok"] else 400), result

    if path == "/api/monitor/setup":
        result = op_monitor_setup(data)
        return (200 if result["ok"] else 400), result

    if path == "/api/stacks":
        do = (data.get("do") or "").strip()
        if do == "validate":
            result = op_stack_validate(data)
        elif do == "install":
            result = op_stack_install(data)
        else:
            return 400, {"ok": False, "error": "Unknown action."}
        return (200 if result["ok"] else 400), result

    if path == "/api/ntfy/setup":
        result = op_ntfy_setup(data)
        return (200 if result["ok"] else 400), result

    if path == "/api/ntfy/test":
        result = op_ntfy_test(data)
        return (200 if result["ok"] else 400), result

    if path == "/api/ntfy/funnel":
        result = op_ntfy_funnel(data)
        return (200 if result["ok"] else 400), result

    if path == "/api/gateway/resolve":
        result = op_gateway_resolve(data)
        return (200 if result["ok"] else 400), result

    m = re.fullmatch(r"/api/ntfy/wire/([a-z0-9][a-z0-9-]*)", path)
    if m:
        result = op_ntfy_wire(m.group(1))
        return (200 if result["ok"] else 400), result

    m = re.fullmatch(r"/api/monitor/pods/([a-z0-9][a-z0-9-]*)", path)
    if m:
        result = op_monitor_pod(m.group(1), (data.get("do") or "").strip())
        return (200 if result["ok"] else 400), result

    if path == "/api/shares":
        action = (data.get("do") or "").strip()
        if action == "add":
            result = op_share_add(data.get("name"), data.get("host_path"),
                                  data.get("container_path"), data.get("ro"))
        elif action == "delete":
            result = op_share_delete(data.get("name"))
        elif action == "attach":
            result = op_attach(data.get("pod"), data.get("share"))
        elif action == "nfs":
            result = op_share_nfs(data.get("name"), bool(data.get("enabled")),
                                  data.get("clients"), bool(data.get("ro", True)))
        else:
            return 400, {"ok": False, "error": "Unknown action."}
        return (200 if result["ok"] else 400), result

    if path == "/api/catalogs":
        result = op_catalog_set((data.get("key") or "").strip(),
                                bool(data.get("enabled")))
        return (200 if result["ok"] else 400), result

    if path == "/api/custompods":
        result = op_custompods(data)
        return (200 if result["ok"] else 400), result

    if path == "/api/fs":
        do = (data.get("do") or "list").strip()
        if do == "list":
            result = op_fs_list(data.get("path") or "/")
        elif do == "mkdir":
            result = op_fs_mkdir(data.get("path") or "")
        else:
            return 400, {"ok": False, "error": f"unknown do '{do}'"}
        return (200 if result["ok"] else 400), result

    if path == "/api/sources":
        action = (data.get("do") or "").strip()
        if action == "add":
            result = op_source_add(data.get("name"), data.get("url"))
        elif action == "delete":
            result = op_source_delete(data.get("name"))
        else:
            return 400, {"ok": False, "error": "Unknown action."}
        return (200 if result["ok"] else 400), result

    return 404, {"error": "not found"}


# =========================================================================
# Controller HTTPS serve self-heal
# =========================================================================
def ensure_controller_serve():
    """Re-assert `tailscale serve` on the controller's OWN sidecar.

    Service pods re-render their sidecar on every run.sh, so their
    declarative TS_SERVE_CONFIG is re-applied whenever they start. The
    controller's sidecar, however, is created once by bootstrap-tailarr.sh:
    if HTTPS certificates are enabled on the tailnet AFTER that first
    bootstrap, a plain restart of the sidecar can come up with "No serve
    config" and stay that way (containerboot does not re-resolve the cert
    domain). Verify on controller start and periodically, applying the same
    proxy the bootstrap serve.json declares. Best-effort and quiet when
    podman or the sidecar is unavailable (dev/CI runs, first bootstrap)."""
    r = podman("exec", "tailscale-tailarr", "tailscale", "serve", "status",
               timeout=15)
    if r.returncode != 0:
        return  # no podman / no sidecar / tailscale not up — nothing to do
    out = (r.stdout or "") + (r.stderr or "")
    if out.strip() and "No serve config" not in out:
        return  # serve already configured
    a = podman("exec", "tailscale-tailarr", "tailscale", "serve", "--bg",
               str(PORT), timeout=30)
    if a.returncode == 0:
        print(f"controller serve re-applied (https:443 -> 127.0.0.1:{PORT})")
    else:
        print("controller serve re-apply failed (are HTTPS certificates "
              "enabled on the tailnet?): "
              + (a.stdout + a.stderr).strip().replace("\n", " "))


def _startup_policy_sync():
    """One policy sync at controller start (credential permitting).

    A release can ADD managed tags/grants (v0.10.0's can-server did), and
    fences otherwise sync only on mutating actions — an upgraded-but-idle
    controller kept serving a stale policy, so the first server grant
    after upgrading failed with "requested tags invalid or not permitted"
    (live-hit twice on 2026-07-19). The sync also fires the tag
    reconcile as its natural follow-on."""
    if not _ts_token():
        return
    # Refresh the peer-relay pre-flight first (auto mode only, daily) so
    # the sync below picks up a newly-eligible grant in the same pass.
    try:
        if host_platform() == "apple-container":
            r = load_relay()
            pf = r.get("preflight") or {}
            if r.get("enabled") is None and \
                    time.time() - pf.get("checked_at", 0) > RELAY_PREFLIGHT_TTL:
                r["preflight"] = ts_relay_preflight()
                save_relay(r)
    except Exception as e:
        print(f"relay preflight error: {e}")
    try:
        sync = ts_policy_sync()
        if not sync["ok"]:
            print(f"startup policy sync: {sync['error']}")
        elif sync["changed"]:
            print("startup policy sync: fences updated to this release")
    except Exception as e:  # never block startup on the tailscale API
        print(f"startup policy sync error: {e}")


def _maintenance_loop():
    """Periodic self-heal: controller serve config + sidecar identity tags.

    Both failure modes are silent by nature (serve: HTTPS just absent;
    tags: users filtered while everything reads healthy), so they are
    re-asserted on a timer as well as on their natural trigger events."""
    try:
        # Re-render the registry authfile from the credential store so a
        # restored backup (or an authfile-format change in a new release)
        # never leaves pulls running on a stale or missing file.
        render_registry_auth(load_registries())
    except Exception as e:
        print(f"registry authfile render error: {e}")
    try:
        # First-boot relay pre-flight (apple-container only): without it,
        # "auto-emit when eligible" could never fire on a fresh install —
        # nothing else runs the pre-flight until a user clicks Re-check
        # (live-caught 2026-07-21: install-mac.sh read an empty verdict).
        # Runs BEFORE the policy sync so an eligible grant lands in it.
        _startup_relay_preflight()
    except Exception as e:
        print(f"relay pre-flight error: {e}")
    _startup_policy_sync()
    # After a successful self-upgrade, push the new engine templates to
    # the fleet automatically (one-shot, marker-keyed) — the upgrade UI
    # warned about the restarts up front, so no "Finish upgrade" step.
    threading.Thread(target=_auto_rerender_after_upgrade,
                     daemon=True).start()
    # Self-heal notifications so an upgrade/restart never needs a manual
    # "Re-run setup": redeploy the app self-config gateway if it went
    # missing. Cheap no-op when it's already up.
    threading.Thread(target=_converge_notifications, daemon=True).start()
    # Mirror ntfy messages into content-free push wakes (idle unless
    # devices have registered tokens via the gateway).
    threading.Thread(target=_push_waker_loop, daemon=True).start()
    while True:
        try:
            ensure_controller_serve()
        except Exception as e:  # never let the self-heal thread die
            print(f"serve self-heal error: {e}")
        try:
            # Health/identity/upgrade-outcome notifications (transition-
            # edge de-dup in .notify-state.json; no-op unless configured).
            _notify_health_pass()
        except Exception as e:
            print(f"notify pass error: {e}")
        try:
            ts_reconcile_tags()
        except Exception as e:
            print(f"tag reconcile error: {e}")
        try:
            # Devices inherit their person's badges — assert it (covers
            # devices enrolled with an older key + missed flips).
            ts_reconcile_people()
        except Exception as e:
            print(f"people reconcile error: {e}")
        try:
            # And the same for their ntfy topic grants (+ orphan cleanup).
            _ntfy_people_pass()
        except Exception as e:
            print(f"ntfy people pass error: {e}")
        try:
            # Relay-in-use is invisible from health checks (DERP still
            # "works", just slowly) — reclassify periodically so the
            # Settings banner reflects reality.
            if _relay_grant_wanted():
                relay_verify()
        except Exception as e:
            print(f"relay verify error: {e}")
        time.sleep(900)


# =========================================================================
# HTTP server
# =========================================================================
class Handler(BaseHTTPRequestHandler):
    def _send(self, content, code=200, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, obj, code=200):
        self._send(json.dumps(obj).encode(), code, "application/json")

    def serve_static(self, path):
        """Serve an SPA build from STATIC_DIR, with index.html routing fallback."""
        if not os.path.isdir(STATIC_DIR):
            return False
        base = os.path.realpath(STATIC_DIR)
        rel = urllib.parse.unquote(path).lstrip("/") or "index.html"
        full = os.path.realpath(os.path.join(base, rel))
        if full != base and not full.startswith(base + os.sep):
            return False  # path traversal attempt
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            # client-side route (no file extension) -> hand back index.html
            index = os.path.join(base, "index.html")
            if "." in os.path.basename(rel) or not os.path.isfile(index):
                return False
            full = index
        with open(full, "rb") as f:
            body = f.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        self._send(body, 200, ctype)
        return True

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/metrics":
            return self._send(render_metrics().encode(), 200,
                              "text/plain; version=0.0.4; charset=utf-8")
        if url.path.startswith("/api/"):
            # /api/info stays open: the self-upgrade health gate and the
            # app's pre-auth compatibility probe both depend on it.
            if url.path != "/api/info" and \
                    not token_auth_ok(self.headers.get("Authorization")):
                return self._send_json(
                    {"ok": False, "error": "authentication required"}, 401)
            try:
                code, obj = api_get(url.path)
            except Exception as e:  # noqa: BLE001 — same contract as do_POST
                code, obj = 500, {"ok": False,
                                  "error": f"{type(e).__name__}: {e}"}
            return self._send_json(obj, code)
        if self.serve_static(url.path):
            return
        self._send(
            b"Tailarr controller: web UI build not found (rebuild the image "
            b"or point STATIC_DIR at an SPA build). The JSON API is at /api/.",
            404, "text/plain; charset=utf-8",
        )

    def do_POST(self):
        if not self.path.startswith("/api/"):
            return self._send_json({"error": "not found"}, 404)
        # The gateway authenticates with its per-install shared secret
        # (op_gateway_resolve), not a bearer token it has no way to hold.
        if self.path != "/api/gateway/resolve" and \
                not token_auth_ok(self.headers.get("Authorization")):
            return self._send_json(
                {"ok": False, "error": "authentication required"}, 401)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            return self._send_json({"error": "invalid JSON body"}, 400)
        try:
            code, obj = api_post(self.path, data)
        except subprocess.TimeoutExpired:
            return self._send_json({"error": "operation timed out"}, 504)
        except Exception as e:  # noqa: BLE001 — scripted callers need a JSON
            # error, not a dropped connection (single-user tailnet API, so
            # surfacing the exception text is help, not a leak).
            return self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        self._send_json(obj, code)

    def log_message(self, fmt, *args):  # quieter default logging
        print("%s - %s" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    # As PID 1 in the container, default signal dispositions don't apply:
    # SIGTERM would be ignored and every `podman stop` waits out its full
    # grace period before SIGKILL. Exit promptly instead — in-flight pod
    # actions are subprocesses of the stop already in progress anyway.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print(f"Tailarr web UI on :{PORT} (pods dir: {PODS_DIR})")
    # Detect/correct the host-platform fact BEFORE anything reads it (it
    # gates the relay pre-flight below) — a cheap in-process file read
    # since the cmdline rework, no helper container.
    _detect_host_platform()
    maybe_check_updates()  # kick a first check if the cache is stale
    threading.Thread(target=_maintenance_loop, daemon=True).start()
    # Existing installs get the mounts drop-in on first start after an
    # upgrade, not only when shares next change.
    threading.Thread(target=_sync_mounts_dropin, daemon=True).start()
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
