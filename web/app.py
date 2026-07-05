#!/usr/bin/env python3
"""Podscale web controller — JSON API backing the React SPA.

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

APP_DIR = os.environ.get("APP_DIR", "/app")
PODS_DIR = os.environ.get("PODS_DIR", "/root/Pods")
STATIC_DIR = os.environ.get("STATIC_DIR", os.path.join(APP_DIR, "static"))
PORT = int(os.environ.get("PORT", "8080"))

CONTROLLER_PODS = {"podscale", "homepod"}  # don't offer stop-self buttons ("homepod" = pre-rename deploys)

# Shared media folders (the only thing allowed to pierce the pod barrier).
# Each share: {"host_path": "/data", "container_path": "/data", "ro": false}
SHARES_FILE = os.path.join(PODS_DIR, ".shares.json")

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
def load_services():
    with open(os.path.join(APP_DIR, "homelab.js")) as f:
        return {s["name"]: s for s in json.load(f)}


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
        req = urllib.request.Request(url, headers={"User-Agent": "podscale"})
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
    merged = {name: {**spec, "_source": "built-in"}
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
        "auth_key_file": info.get("auth_key_file", ""),
        "base_path": info.get("base_path", PODS_DIR),
        "environment": info.get("environment", {}),
        "volumes": info.get("volumes", {}),
        "command": info.get("command", ""),
        "memory_limit": info.get("memory_limit", ""),
        "shares": info.get("shares", []),
    }


def run_create(config):
    return subprocess.run(
        ["bash", os.path.join(APP_DIR, "create.sh")],
        input=json.dumps(config),
        capture_output=True,
        text=True,
        cwd="/tmp",
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

    # Tailscale is mandatory: resolve (and require) an auth key. A pod that
    # already carries enrolled state in ./tailscale/ can re-run without one.
    auth_key_file = os.path.join(PODS_DIR, name, ".tailscale_authkey")
    pasted = (req.get("authkey") or "").strip()
    if pasted:
        os.makedirs(os.path.dirname(auth_key_file), exist_ok=True)
        with open(auth_key_file, "w") as f:
            f.write(pasted + "\n")
        os.chmod(auth_key_file, 0o600)
    elif not os.path.isfile(auth_key_file):
        return {"ok": False, "name": name,
                "error": "An auth key is required — every pod enrolls as its own Tailscale node.",
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
    }
    result = run_create(config)
    output = result.stdout + result.stderr
    if result.returncode != 0:
        return {"ok": False, "name": name, "error": None, "output": output}
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
    return {"ok": ok, "name": name, "action": action,
            "status": "ok" if ok else f"exit {r.returncode}",
            "error": None, "output": output}


def op_fleet(action):
    """stop / start / restart every deployed pod except the controller.

    Claims all targets up front — so the whole fleet reads as busy in
    /api/pods while this request works through them sequentially — then
    releases each pod as it finishes. Pods already mid-action from another
    request are skipped, not queued. The controller pod is never touched:
    stopping it (and the podhost VM around it) is a host-side operation.
    """
    if action not in ("stop", "start", "restart"):
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


def op_network_set(name, data):
    """Flip a pod's tailscale / HTTPS setting: re-render + restart with the
    rest of its saved config unchanged. data: {tailscale?: bool, https?: bool}."""
    if name not in deployed_services():
        return {"ok": False, "name": name, "action": "network", "status": "error",
                "error": "Unknown service.", "output": ""}
    if name in CONTROLLER_PODS:
        return {"ok": False, "name": name, "action": "network", "status": "refused",
                "error": "Not changing the controller's network from itself.",
                "output": ""}
    info = pod_config(name)
    if info is None:
        return {"ok": False, "name": name, "action": "network", "status": "error",
                "error": "No .config.json for this pod (redeploy once to create it).",
                "output": ""}
    d = reconfig_data_from_info(info)
    if "tailscale" in data:
        d["tailscale"] = bool(data["tailscale"])
    if "https" in data:
        d["https"] = bool(data["https"])
    d["pull"] = False
    return op_reconfigure(name, d)


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
    return {"ok": True, "name": name, "error": None,
            "message": f"Added share '{name}'.", "share": shares[name]}


def op_share_delete(name):
    shares = load_shares()
    if shares.pop(name, None) is None:
        return {"ok": False, "name": name, "error": "Unknown share."}
    save_shares(shares)
    return {"ok": True, "name": name, "error": None,
            "message": f"Deleted share '{name}'. Pods that mount it keep their volume"
                       " until re-rendered."}


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
        return 200, {"pods_dir": PODS_DIR, "controller_pods": sorted(CONTROLLER_PODS)}
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
    if path == "/api/shares":
        return 200, {"shares": status_shares()}
    if path == "/api/sources":
        return 200, {"sources": status_sources()}
    m = re.fullmatch(r"/api/pods/([a-z0-9][a-z0-9-]*)/logs", path)
    if m:
        return 200, op_action(m.group(1), "logs")
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

    m = re.fullmatch(r"/api/pods/([a-z0-9][a-z0-9-]*)/config", path)
    if m:
        result = op_reconfigure(m.group(1), data)
        code = 200 if result["ok"] else (409 if result.get("status") == "busy" else 400)
        return code, result

    if path == "/api/fleet":
        result = op_fleet((data.get("do") or "").strip())
        return (200 if result["ok"] else 400), result

    if path == "/api/updates/refresh":
        return 200, {"ok": True, "status": maybe_check_updates(force=True)}

    m = re.fullmatch(r"/api/network/([a-z0-9][a-z0-9-]*)", path)
    if m:
        result = op_network_set(m.group(1), data)
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
        else:
            return 400, {"ok": False, "error": "Unknown action."}
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
        if url.path.startswith("/api/"):
            code, obj = api_get(url.path)
            return self._send_json(obj, code)
        if self.serve_static(url.path):
            return
        self._send(
            b"Podscale controller: web UI build not found (rebuild the image "
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
        self._send_json(obj, code)

    def log_message(self, fmt, *args):  # quieter default logging
        print("%s - %s" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    # As PID 1 in the container, default signal dispositions don't apply:
    # SIGTERM would be ignored and every `podman stop` waits out its full
    # grace period before SIGKILL. Exit promptly instead — in-flight pod
    # actions are subprocesses of the stop already in progress anyway.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print(f"Podscale web UI on :{PORT} (pods dir: {PODS_DIR})")
    maybe_check_updates()  # kick a first check if the cache is stale
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
