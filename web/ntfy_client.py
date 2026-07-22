"""ntfy client for the Notifications tab.

ntfy speaks plain HTTP, so unlike kuma_client this needs no third-party
package: publishing is a single authenticated POST. Account/ACL
provisioning is NOT here — it drives the ntfy CLI inside the pod via
podman exec, so it lives in app.py next to the other podman-driving ops.

Two registry files under $PODS_DIR:
  .ntfy.json (0600, SECRET) — accounts, tokens, topics, wiring state.
  .notify-state.json (plain) — last-seen pod/identity states and other
  de-dup bookkeeping for event publishing; kept separate so the secrets
  file is write-rarely.

The ntfy pod's tailnet IP is deliberately never persisted: app.py
resolves it at publish time (the pod's IP can change across restarts).
"""

import json
import os
import urllib.error
import urllib.request

PODS_DIR = os.environ.get("PODS_DIR", "/root/Pods")
NTFY_FILE = os.path.join(PODS_DIR, ".ntfy.json")
STATE_FILE = os.path.join(PODS_DIR, ".notify-state.json")
TIMEOUT = 5

# Deterministic naming: the ACL (auth-default-access: deny-all) is the
# boundary, so predictable names cost nothing and keep provisioning,
# badge-mirroring, and Arr wiring idempotent.
OPS_TOPIC = "tlr-ops"
MEDIA_TOPIC_PREFIX = "tlr-media-"
ADMIN_USER = "tailarr"
PUB_USER = "tailarr-pub"
# The admin phone's read-only account ("Alerts on your phone" card).
ALERTS_USER = "tailarr-alerts"


def load_conf():
    try:
        with open(NTFY_FILE) as f:
            conf = json.load(f)
        return conf if isinstance(conf, dict) and conf.get("publisher") else None
    except (OSError, ValueError):
        return None


def save_conf(conf):
    tmp = NTFY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(conf, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, NTFY_FILE)


def load_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def clear():
    """Remove both registries (ntfy pod removed: a reinstall gets a fresh
    auth.db, so every stored token is dead anyway)."""
    for path in (NTFY_FILE, STATE_FILE):
        try:
            os.remove(path)
        except OSError:
            pass


def publish(conf, base_url, topic, title, message,
            priority="default", tags=None):
    """POST one notification. Returns an error string, or None on success.

    Never raises: callers fire-and-forget from background threads, and a
    down ntfy must never break the operation that triggered the event.
    The returned error is surfaced on the Notifications page instead."""
    token = ((conf or {}).get("publisher") or {}).get("token", "")
    if not (base_url and topic and token):
        return "ntfy is not configured"
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/{topic}",
        data=(message or "").encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Title": title or "Tailarr",
            "Priority": priority,
            **({"Tags": ",".join(tags)} if tags else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            if r.status >= 300:
                return f"ntfy answered HTTP {r.status}"
            return None
    except urllib.error.HTTPError as e:
        return f"ntfy answered HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return f"ntfy unreachable: {e}"
