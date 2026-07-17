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

import hashlib
import json
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.9.6"

APP_DIR = os.environ.get("APP_DIR", "/app")
PODS_DIR = os.environ.get("PODS_DIR", "/root/Pods")
STATIC_DIR = os.environ.get("STATIC_DIR", os.path.join(APP_DIR, "static"))
PORT = int(os.environ.get("PORT", "8080"))

CONTROLLER_PODS = {"tailarr", "podscale", "homepod"}  # older names = pre-rename deploys

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


def all_services():
    """Merged catalog: built-in homelab.js + enabled sources.

    Built-in and earlier sources win on name collision. Each spec is tagged
    with `_source` ("built-in" or the source name). Returns (dict, errors).
    """
    merged = {name: {**spec, "_source": spec.get("_source", "built-in")}
              for name, spec in load_services().items()}
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
            ["podman", *args], capture_output=True, text=True, timeout=timeout
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
        results = {img: _image_update_status(img) for img in images}
        save_updates({"checked": time.time(), "images": results})
        _check_release()  # piggyback the controller-release check (cached)
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


def _shareable_services():
    return [s for s in deployed_services() if s not in CONTROLLER_PODS]


def status_users():
    """User machines (tag:tailarr-user) + their capability badges."""
    services = _shareable_services()
    if not _ts_token():
        return {"configured": False, "error": None, "users": [],
                "services": services}
    code, data = ts_api("GET", "/tailnet/-/devices")
    if code != 200:
        return {"configured": True, "error": f"devices API: {data}",
                "users": [], "services": services}
    nicks = load_user_nicks()
    users = []
    for d in data.get("devices", []):
        tags = d.get("tags") or []
        if TS_USER_TAG not in tags:
            continue
        users.append({
            "id": d.get("nodeId", ""),
            "hostname": d.get("hostname", ""),
            "nickname": nicks.get(d.get("nodeId", ""), ""),
            "os": d.get("os", ""),
            "last_seen": d.get("lastSeen", ""),
            "ip": next((a for a in d.get("addresses", []) if "." in a), ""),
            "can": sorted(t[len(TS_CAN_PREFIX):] for t in tags
                          if t.startswith(TS_CAN_PREFIX)),
        })
    users.sort(key=lambda u: (u["nickname"] or u["hostname"]).lower())
    return {"configured": True, "error": None, "users": users,
            "services": services}


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
    """
    if service not in _shareable_services():
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
# Tailnet policy sync — the fenced-grant generator (docs/acl-design.md §4)
#
# Tailarr owns three labeled fenced regions of the tailnet policy file
# (grants / tagOwners / nodeAttrs) and regenerates them from the deployed
# service list on install/remove. Line-level splicing only: the human's
# HuJSON outside the fences survives byte-for-byte. Fail closed on any
# fence anomaly; nothing inside a fence may reference a name outside
# tag:tailarr* (the prefix invariant).
# =========================================================================
_policy_lock = threading.Lock()
ACL_BACKUP_FILE = os.path.join(PODS_DIR, ".acl-last-good.hujson")
FENCE_BEGIN = "// >>> tailarr-managed:"
FENCE_END = "// <<< tailarr-managed:"


def _managed_sections():
    """Desired fence contents, derived from the deployed service list."""
    svcs = _shareable_services()
    grants = [
        '{"src": ["tag:tailarr"], "dst": ["tag:tailarr"], "ip": ["*"]},',
        # Funnel ingress traffic is NOT exempt from the packet filter under
        # default-deny (tailscale/tailscale#18181) — admit Tailscale's
        # ingress range to public-tagged pods or Funnel silently drops.
        '{"src": ["fd7a:115c:a1e0:ab12::/64"], '
        '"dst": ["tag:tailarr-public"], "ip": ["*"]}, // funnel ingress',
    ]
    for s in svcs:
        grants.append(f'{{"src": ["tag:tailarr-can-{s}"], '
                      f'"dst": ["tag:tailarr-svc-{s}"], "ip": ["443"]}},')
    # tag:tailarr-ctrl co-owns every other tag so an OAuth client tagged
    # tag:tailarr-ctrl may assign them (device tagging + key minting).
    OWN = '["autogroup:admin", "tag:tailarr-ctrl"]'
    owners = ['"tag:tailarr-ctrl": ["autogroup:admin"],']
    owners += [f'"{t}": {OWN},'
               for t in ("tag:tailarr", "tag:tailarr-user",
                         "tag:tailarr-public")]
    for s in svcs:
        owners.append(f'"tag:tailarr-svc-{s}": {OWN},')
        owners.append(f'"tag:tailarr-can-{s}": {OWN},')
    attrs = ['{"target": ["tag:tailarr-public"], "attr": ["funnel"]},']
    return {"grants": grants, "tagowners": owners, "nodeattrs": attrs}


def _sections_prefix_ok(sections):
    """The safety invariant: fences may only reference tag:tailarr* names."""
    for lines in sections.values():
        for ln in lines:
            for t in re.findall(r'"(tag:[a-z0-9-]+)"', ln):
                if not t.startswith("tag:tailarr"):
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
    with _policy_lock:
        for _attempt in range(2):
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
        installed = name in deployed
        out.append({
            "name": name,
            "image": spec.get("image", ""),
            "ports": spec.get("ports", {}),
            "port": next(iter(spec.get("ports", {})), ""),
            "environment": spec.get("environment", {}),
            "volumes": spec.get("volumes", {}),
            "command": spec.get("command", ""),
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
    r = podman("stats", "--no-stream", "--format", "json", timeout=30)
    if r.returncode == 0:
        try:
            rows = json.loads(r.stdout or "[]")
        except ValueError:
            rows = []
        if rows:
            lines += [
                "# HELP tailarr_container_cpu_percent live CPU percent.",
                "# TYPE tailarr_container_cpu_percent gauge",
                "# HELP tailarr_container_mem_bytes live memory usage.",
                "# TYPE tailarr_container_mem_bytes gauge",
            ]
        for row in rows:
            cname = row.get("name") or row.get("Name") or ""
            if not cname:
                continue
            cpu = str(row.get("cpu_percent") or row.get("CPUPerc")
                      or "0").rstrip("%") or "0"
            mem = row.get("mem_usage") or row.get("MemUsage") or 0
            if isinstance(mem, str):  # e.g. "123.4MB / 4GB"
                mem = mem.split("/")[0].strip()
                units = {"kB": 1e3, "KiB": 1024, "MB": 1e6, "MiB": 1 << 20,
                         "GB": 1e9, "GiB": 1 << 30, "B": 1}
                for u, mult in units.items():
                    if mem.endswith(u):
                        try:
                            mem = float(mem[: -len(u)]) * mult
                        except ValueError:
                            mem = 0
                        break
                else:
                    mem = 0
            try:
                lines.append(
                    f'tailarr_container_cpu_percent{{container="{cname}"}} '
                    f'{float(cpu)}')
                lines.append(
                    f'tailarr_container_mem_bytes{{container="{cname}"}} '
                    f'{int(float(mem))}')
            except (TypeError, ValueError):
                continue
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


def _run_rerender(name):
    """Re-render one pod's scripts from its saved .config.json, then re-run
    it. This is how engine updates (new run.sh templates) reach existing
    pods — typically right after a controller upgrade. The pod's image,
    volumes, environment and Tailscale identity are all unchanged; run.sh
    recreates the containers, so expect a brief restart."""
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
    r = _run_action(name, "start")
    r["action"] = "rerender"
    r["output"] = result.stdout + result.stderr + r.get("output", "")
    return r


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
        return 200, {"pods_dir": PODS_DIR,
                     "controller_pods": sorted(CONTROLLER_PODS),
                     "version": VERSION,
                     "upgrade_available": bool(latest)
                     and _ver_key(latest) > _ver_key(VERSION),
                     "tsapi": status_tsapi()}
    if path == "/api/controller/upgrade":
        return 200, upgrade_status()
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
    if path == "/api/users":
        return 200, status_users()
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


def _maintenance_loop():
    """Periodic self-heal: controller serve config + sidecar identity tags.

    Both failure modes are silent by nature (serve: HTTPS just absent;
    tags: users filtered while everything reads healthy), so they are
    re-asserted on a timer as well as on their natural trigger events."""
    while True:
        try:
            ensure_controller_serve()
        except Exception as e:  # never let the self-heal thread die
            print(f"serve self-heal error: {e}")
        try:
            ts_reconcile_tags()
        except Exception as e:
            print(f"tag reconcile error: {e}")
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
    maybe_check_updates()  # kick a first check if the cache is stale
    threading.Thread(target=_maintenance_loop, daemon=True).start()
    # Existing installs get the mounts drop-in on first start after an
    # upgrade, not only when shares next change.
    threading.Thread(target=_sync_mounts_dropin, daemon=True).start()
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
