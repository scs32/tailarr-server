"""Uptime Kuma client for the Monitor tab.

Kuma has no REST API for CRUD -- its UI speaks Socket.io -- so this wraps
the uptime-kuma-api package (installed in the controller image). Each call
opens a fresh connection, does its work, and disconnects; Kuma logins are
cheap (<1s) and this keeps the stdlib HTTP server free of long-lived
socket state.

Credentials live in $PODS_DIR/.kuma.json (mode 600). The connect flow
handles factory-fresh instances: if the server still needs setup, the
credentials given become the new admin account.

Import of uptime_kuma_api is deferred so the controller (and CI) work
without the package -- the API then reports the feature as unavailable.
"""

import json
import os

PODS_DIR = os.environ.get("PODS_DIR", "/root/Pods")
KUMA_FILE = os.path.join(PODS_DIR, ".kuma.json")
TIMEOUT = 20

# Reachable = up. Auth-walled services (sonarr & co) answer 401 on "/";
# treating any non-5xx response as healthy avoids per-app path knowledge.
# Kuma only accepts its predefined range strings, not arbitrary ranges.
ACCEPT_CODES = ["200-299", "300-399", "400-499"]


def _lib():
    from uptime_kuma_api import MonitorType, UptimeKumaApi
    return UptimeKumaApi, MonitorType


def available():
    try:
        _lib()
        return True
    except ImportError:
        return False


def load_conf():
    try:
        with open(KUMA_FILE) as f:
            conf = json.load(f)
        return conf if isinstance(conf, dict) and conf.get("url") else None
    except (OSError, ValueError):
        return None


def save_conf(conf):
    tmp = KUMA_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(conf, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, KUMA_FILE)


def _connect(conf):
    UptimeKumaApi, _ = _lib()
    api = UptimeKumaApi(conf["url"], timeout=TIMEOUT)
    try:
        api.login(conf["username"], conf["password"])
    except Exception:
        api.disconnect()
        raise
    return api


def setup(url, username, password):
    """Connect to Kuma, creating the admin account on a fresh instance.
    Validates the credentials either way, then persists them."""
    UptimeKumaApi, _ = _lib()
    api = UptimeKumaApi(url, timeout=TIMEOUT)
    try:
        fresh = False
        try:
            fresh = api.need_setup()
        except Exception:
            pass
        if fresh:
            api.setup(username, password)
        api.login(username, password)
    finally:
        api.disconnect()
    save_conf({"url": url, "username": username, "password": password})
    return {"ok": True, "fresh": fresh}


def get_monitors():
    """[{id, name, url, active}] from the configured Kuma."""
    conf = load_conf()
    if not conf:
        return None
    api = _connect(conf)
    try:
        return [
            {"id": m["id"], "name": m["name"], "url": m.get("url", ""),
             "active": bool(m.get("active", True))}
            for m in api.get_monitors()
        ]
    finally:
        api.disconnect()


def add_monitor(name, url):
    """Create an HTTP monitor (named after the pod) for a service URL."""
    conf = load_conf()
    if not conf:
        raise RuntimeError("Kuma is not configured.")
    UptimeKumaApi, MonitorType = _lib()
    api = _connect(conf)
    try:
        return api.add_monitor(
            type=MonitorType.HTTP,
            name=name,
            url=url,
            accepted_statuscodes=ACCEPT_CODES,
        )
    finally:
        api.disconnect()


def remove_monitor(name):
    """Delete the monitor whose name matches the pod. Returns True if found."""
    conf = load_conf()
    if not conf:
        raise RuntimeError("Kuma is not configured.")
    api = _connect(conf)
    try:
        for m in api.get_monitors():
            if m["name"] == name:
                api.delete_monitor(m["id"])
                return True
        return False
    finally:
        api.disconnect()
