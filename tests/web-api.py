#!/usr/bin/env python3
"""End-to-end test of the web controller JSON API.

Boots the real ThreadingHTTPServer against the create.sh engine in a temp
PODS_DIR and drives it over HTTP. No podman/containers needed: installing only
generates scripts, and pod-state reads degrade gracefully when podman is absent
(podman() catches FileNotFoundError). Tailscale is mandatory, so installs
carry a dummy auth key (only stored in a key file, never used offline).
"""
import json
import os
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pods = os.path.join(tempfile.mkdtemp(), "Pods")
os.makedirs(pods)
os.environ["APP_DIR"] = REPO
os.environ["PODS_DIR"] = pods
# Point STATIC_DIR at a definitely-absent dir so the JSON API + legacy HTML win.
os.environ["STATIC_DIR"] = os.path.join(pods, "no-such-static")
sys.path.insert(0, os.path.join(REPO, "web"))
import app  # noqa: E402

srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"

# A tiny local server that serves an external catalog (homelab.js schema),
# so the catalog-sources test exercises the real fetch/merge path offline.
CATALOG_JSON = json.dumps([
    {"name": "extpod", "image": "docker.io/alpine:latest",
     "command": "sleep infinity", "ports": {}, "environment": {}, "volumes": {},
     "network_mode": "bridge", "restart_policy": "unless-stopped"},
]).encode()


class CatalogHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(CATALOG_JSON)))
        self.end_headers()
        self.wfile.write(CATALOG_JSON)

    def log_message(self, *a):  # keep test output quiet
        pass


catsrv = ThreadingHTTPServer(("127.0.0.1", 0), CatalogHandler)
threading.Thread(target=catsrv.serve_forever, daemon=True).start()
CAT_URL = f"http://127.0.0.1:{catsrv.server_address[1]}/catalog.json"


def check(cond, label):
    if not cond:
        print(f"FAIL: {label}")
        srv.shutdown()
        sys.exit(1)
    print(f"  ok: {label}")


def get(path):
    with urllib.request.urlopen(BASE + path) as r:
        return r.status, json.load(r)


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


# --- catalog ---
code, data = get("/api/catalog")
check(code == 200 and isinstance(data.get("catalog"), list) and data["catalog"],
      "GET /api/catalog returns entries")
check(all("name" in c and "image" in c and "installed" in c for c in data["catalog"]),
      "catalog entries carry name/image/installed")

# --- install a custom pod over the API ---
code, data = post("/api/install", {
    "custom": True, "service": "apitest", "image": "docker.io/alpine:latest",
    "command": "sleep infinity",
    "volumes": {"/config": f"{pods}/apitest/config"},
    "authkey": "dummy-test-authkey-api",
})
check(code == 200 and data["ok"] and data["name"] == "apitest",
      "POST /api/install (custom) succeeds")
check(os.path.isfile(os.path.join(pods, "apitest", "run.sh")),
      "install generated run.sh")
check(app.pod_config("apitest")["image"] == "docker.io/alpine:latest",
      ".config.json written")

# --- it shows up in /api/pods ---
code, data = get("/api/pods")
names = {p["name"]: p for p in data["pods"]}
check(code == 200 and "apitest" in names, "GET /api/pods lists the new pod")
check(names["apitest"]["state"] == "stopped" and names["apitest"]["controller"] is False,
      "pod reported stopped / non-controller")

# --- shares: add then attach via the API ---
code, data = post("/api/shares", {"do": "add", "name": "media", "host_path": "/data"})
check(code == 200 and data["ok"], "POST /api/shares add")
code, data = get("/api/shares")
check(any(s["name"] == "media" and s["mode"] == "read-write" for s in data["shares"]),
      "GET /api/shares lists it")
code, data = post("/api/shares", {"do": "attach", "pod": "apitest", "share": "media"})
check(code == 200 and data["ok"], "POST /api/shares attach")
check(app.pod_config("apitest")["shares"] == ["media"], "share recorded on the pod")

# --- validation / error paths ---
code, data = post("/api/install", {"service": "definitely-not-real"})
check(code == 400 and data["ok"] is False and "Unknown service" in data["error"],
      "unknown catalog service -> 400")
code, data = post("/api/pods/nope/action", {"do": "start"})
check(code == 400 and data["ok"] is False, "action on unknown pod -> 400")
code, data = post("/api/shares", {"do": "bogus"})
check(code == 400 and "Unknown action" in data["error"], "bad share action -> 400")
code, data = post("/api/fleet", {"do": "bogus"})
check(code == 400 and "Unknown fleet action" in data["error"], "bad fleet action -> 400")
# No podman here, so nothing reads as running: fleet stop is a clean no-op.
code, data = post("/api/fleet", {"do": "stop"})
check(code == 200 and data["ok"] and data["results"] == [],
      "fleet stop with nothing running -> 200 no-op")

try:
    get("/api/nope")
    check(False, "unknown API path -> 404")
except urllib.error.HTTPError as e:
    check(e.code == 404, "unknown API path -> 404")

# --- catalog sources: add a URL source, merge, install from it, delete ---
code, data = post("/api/sources", {"do": "add", "name": "community", "url": CAT_URL})
check(code == 200 and data["ok"] and "1 services" in (data.get("message") or ""),
      "POST /api/sources add fetches + validates the catalog")
code, data = get("/api/sources")
check(any(s["name"] == "community" and s["service_count"] == 1 and not s["error"]
          for s in data["sources"]),
      "GET /api/sources lists it with a service count")
code, data = get("/api/catalog")
ext = [c for c in data["catalog"] if c["name"] == "extpod"]
check(bool(ext) and ext[0]["source"] == "community",
      "source service merged into the catalog, tagged with its source")
code, data = post("/api/install", {"service": "extpod", "volumes": {},
                                    "authkey": "dummy-test-authkey-api"})
check(code == 200 and data["ok"], "install a service that came from a source")
check(app.pod_config("extpod")["image"] == "docker.io/alpine:latest",
      "source service resolved from the merged catalog and installed")
code, data = post("/api/sources", {"do": "add", "name": "bad", "url": "ftp://nope"})
check(code == 400 and "http" in data["error"], "reject a non-http(s) source URL")
code, data = post("/api/sources", {"do": "delete", "name": "community"})
check(code == 200 and data["ok"], "delete source")
code, data = get("/api/catalog")
check(not any(c["name"] == "extpod" for c in data["catalog"]),
      "source's services leave the catalog after the source is deleted")

catsrv.shutdown()
srv.shutdown()
print("WEB API TEST PASSED")
