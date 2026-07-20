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

# --- built-in category catalogs: off by default, toggle merges entries ---
code, data = get("/api/sources")
check(code == 200 and any(c["key"] == "observability" for c in data["catalogs"]),
      "sources: built-in catalogs listed")
check(all(not c["enabled"] for c in data["catalogs"]),
      "catalogs: all categories default off")
code, data = get("/api/catalog")
check(not any(c["name"] == "grafana" for c in data["catalog"]),
      "catalog: category entries hidden until enabled")
code, data = post("/api/catalogs", {"key": "observability", "enabled": True})
check(code == 200 and data["ok"], "enable the observability catalog")
code, data = get("/api/catalog")
g = [c for c in data["catalog"] if c["name"] == "grafana"]
check(bool(g) and g[0]["source"] == "Observability",
      "grafana appears, tagged with its category")
check(post("/api/catalogs", {"key": "bogus", "enabled": True})[0] == 400,
      "unknown catalog key rejected")
code, data = post("/api/catalogs", {"key": "observability", "enabled": False})
check(code == 200 and not any(
    c["name"] == "grafana"
    for c in get("/api/catalog")[1]["catalog"]), "disable removes the entries")

# --- /metrics: Prometheus exposition (no podman here -> flags only) ---
with urllib.request.urlopen(BASE + "/metrics") as r:
    text = r.read().decode()
    check(r.status == 200 and 'tailarr_pod_up{pod="apitest"}' in text,
          "/metrics exposes the pod up gauge")
    check("tailarr_pod_public" in text and "tailarr_pod_update_available" in text,
          "/metrics exposes funnel + update flags")

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

# --- deploys leave their log in the service dir (bad-CWD regression) ---
check(os.path.isfile(os.path.join(pods, "apitest", ".deployment.log")),
      "install wrote .deployment.log into the pod dir (absolute LOG_FILE)")

# --- credential wizard: status + validate/save/fences, no credential yet ---
code, data = get("/api/info")
check(code == 200 and data.get("version") and data.get("tsapi") is not None,
      "GET /api/info carries version + tsapi state")
code, data = get("/api/tsapi")
check(code == 200 and data["configured"] is False,
      "GET /api/tsapi: not configured on a fresh install")
code, data = post("/api/tsapi/validate", {})
check(data["ok"] is False and "credential" in (data["error"] or "").lower(),
      "validate with no credential explains itself")
code, data = post("/api/tsapi", {})
check(code == 400 and data["ok"] is False,
      "save with no credential -> 400")
code, data = post("/api/tsapi/fences", {})
check(code == 400 and "no API token" in (data["error"] or ""),
      "fence init without a credential -> 400")

# --- install without a key and without a credential: the wizard trigger ---
code, data = post("/api/install", {
    "custom": True, "service": "nokey", "image": "docker.io/alpine:latest"})
check(code == 400 and data["ok"] is False
      and "auth key is required" in data["error"]
      and "Settings" in data["error"],
      "keyless install without a credential -> 400, points at the wizard")

# --- auto-mint: with a credential + stubbed keys API, zero manual entry ---
_real_ts_token = app._ts_token
_real_ts_api = app.ts_api
_real_policy_sync = app.ts_policy_sync
minted = {}


def _fake_ts_api(method, path, body=None):
    if method == "POST" and path == "/tailnet/-/keys":
        minted["body"] = body
        return 200, {"key": "dummy-test-authkey-minted"}
    return 200, {}


app._ts_token = lambda: "dummy-test-token"
app.ts_api = _fake_ts_api
app.ts_policy_sync = lambda: {"ok": True, "changed": False, "error": None}
try:
    code, data = post("/api/install", {
        "custom": True, "service": "autominted",
        "image": "docker.io/alpine:latest", "command": "sleep infinity"})
    check(code == 200 and data["ok"],
          "keyless install with a credential auto-mints and succeeds")
    keyfile = os.path.join(pods, "autominted", ".tailscale_authkey")
    check(os.path.isfile(keyfile)
          and open(keyfile).read().strip() == "dummy-test-authkey-minted",
          "minted key written to the pod's key file")
    check(os.stat(keyfile).st_mode & 0o777 == 0o600,
          "minted key file is 0600")
    caps = minted["body"]["capabilities"]["devices"]["create"]
    check(caps["tags"] == ["tag:tailarr"] and caps["preauthorized"] is True
          and caps["reusable"] is False and caps["ephemeral"] is False,
          "minted key is single-use, preauthorized, tagged tag:tailarr")
    check(minted["body"]["expirySeconds"] > 0
          and "autominted" in minted["body"]["description"],
          "minted key has a TTL and a descriptive description")
    # pasted keys still override minting
    code, data = post("/api/install", {
        "custom": True, "service": "pastedkey",
        "image": "docker.io/alpine:latest",
        "authkey": "dummy-test-authkey-pasted"})
    pastedfile = os.path.join(pods, "pastedkey", ".tailscale_authkey")
    check(code == 200 and data["ok"]
          and open(pastedfile).read().strip() == "dummy-test-authkey-pasted",
          "a pasted key overrides auto-minting")
    check(os.stat(pastedfile).st_mode & 0o777 == 0o600,
          "pasted key file is 0600")
finally:
    app._ts_token = _real_ts_token
    app.ts_api = _real_ts_api
    app.ts_policy_sync = _real_policy_sync

# --- wizard save: stub the live probe, expect a 0600 whitelisted file ---
_real_validate = app.op_tsapi_validate
app.op_tsapi_validate = lambda data: {
    "ok": True, "mode": "token",
    "checks": {k: {"ok": True, "detail": None}
               for k in ("devices", "auth_keys", "policy_file")},
    "fences": {"present": list(app.FENCE_SECTIONS), "missing": []},
    "error": None}
try:
    code, data = post("/api/tsapi", {"token": "dummy-test-authkey-tsapi",
                                     "junk": "must-not-persist"})
    check(code == 200 and data["ok"] and data["saved"],
          "POST /api/tsapi validates then saves")
    saved = json.load(open(os.path.join(pods, ".tsapi.json")))
    check(saved == {"token": "dummy-test-authkey-tsapi"},
          ".tsapi.json holds exactly the whitelisted credential fields")
    check(os.stat(os.path.join(pods, ".tsapi.json")).st_mode & 0o777 == 0o600,
          ".tsapi.json is 0600")
    code, data = get("/api/tsapi")
    check(code == 200 and data["configured"] and data["mode"] == "token",
          "GET /api/tsapi reports the saved credential")
finally:
    app.op_tsapi_validate = _real_validate
    os.remove(os.path.join(pods, ".tsapi.json"))  # keep later tests offline

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

# --- controller self-upgrade (podman faked in-process) -------------------
# The op must (a) pull the explicit new version tag BEFORE anything is
# removed (GHCR manifest lag), (b) never rm the controller itself — the
# swap belongs to the detached helper script, which must carry a rollback
# path — and (c) refuse cleanly when there is nothing to do.
import subprocess as _sp  # noqa: E402


class FakePodman:
    """Records every call; simulates a live controller + sidecar."""

    def __init__(self):
        self.calls = []
        self.names = ["tailarr", "tailscale-tailarr"]

    def __call__(self, *args, timeout=60):
        self.calls.append(list(args))
        if args[0] == "ps":
            return _sp.CompletedProcess(args, 0, "\n".join(self.names) + "\n", "")
        if args[0] == "inspect":
            return _sp.CompletedProcess(args, 0, "ghcr.io/scs32/tailarr:v0.1.0\n", "")
        return _sp.CompletedProcess(args, 0, "", "")


_real_podman = app.podman
_real_load_release = app.load_release
fake = FakePodman()
app.podman = fake
# The daily update check piggybacks a real GitHub release lookup; pin the
# cache empty so these assertions don't depend on the network or the repo.
app.load_release = lambda: {}
try:
    code, data = get("/api/controller/upgrade")
    check(code == 200 and data["current"] == app.VERSION
          and data["available"] is False and data["busy"] is False,
          "upgrade status: current version, nothing available without a check")

    code, data = post("/api/controller/upgrade", {"version": app.VERSION})
    check(code == 400 and data["status"] == "refused",
          "upgrade to the running version refused")
    code, data = post("/api/controller/upgrade", {"version": "not-a-version"})
    check(code == 400 and "not a release version" in data["error"],
          "malformed version rejected")
    code, data = post("/api/controller/upgrade", {})
    check(code == 400 and "No release known" in data["error"],
          "upgrade without a known release refused (no silent :latest)")

    fake.calls = []
    code, data = post("/api/controller/upgrade", {"version": "9.9.9"})
    check(code == 200 and data["ok"] and data["status"] == "upgrading"
          and data["to"].endswith(":v9.9.9"),
          "upgrade to an explicit version launches")
    check(data["from"] == "ghcr.io/scs32/tailarr:v0.1.0"
          and data["to"] == "ghcr.io/scs32/tailarr:v9.9.9",
          "registry/repo kept from the running image; only the tag moves")
    pulls = [i for i, c in enumerate(fake.calls) if c[0] == "pull"]
    runs = [i for i, c in enumerate(fake.calls) if c[0] == "run"]
    check(pulls and runs and pulls[0] < runs[0]
          and fake.calls[pulls[0]][1] == "ghcr.io/scs32/tailarr:v9.9.9",
          "explicit new tag pulled before the helper starts (manifest-lag safe)")
    check(not any(c[:3] == ["rm", "-f", "tailarr"] for c in fake.calls),
          "the controller never removes itself — the helper script does the swap")
    helper = fake.calls[runs[0]]
    check("--entrypoint" in helper and "/root:/host-root" in " ".join(helper)
          and helper[helper.index("--entrypoint") + 2] == "ghcr.io/scs32/tailarr:v9.9.9",
          "helper runs the NEW image with the host's /root mounted")

    with open(os.path.join(pods, ".upgrade", "redeploy.sh")) as f:
        script = f.read()
    check("podman rm -f tailarr" in script
          and "--network container:tailscale-tailarr" in script,
          "redeploy script swaps the controller onto the existing sidecar")
    check("wget -q -O /dev/null" in script and "tailscale-tailarr" in script,
          "health check probes the API through the sidecar's netns")
    check("ROLLING BACK" in script
          and script.count("ghcr.io/scs32/tailarr:v0.1.0") >= 2,
          "redeploy script carries a rollback to the old image")
    check("start-pods.sh" in script and "/host-root" in script,
          "redeploy script refreshes the host's start-pods.sh from the new image")
    check(script.index("finish true") < script.index("/app/start-pods.sh"),
          "outcome written the moment health passes, before artifact refresh "
          "(result.json must not lag the version flip)")

    fake.names.append(app.UPGRADE_HELPER)
    code, data = post("/api/controller/upgrade", {"version": "9.9.8"})
    check(code == 409 and data["status"] == "busy",
          "second upgrade refused while the helper is running")
    code, data = get("/api/controller/upgrade")
    check(data["busy"] is True, "upgrade status reports busy while helper runs")
    fake.names.remove(app.UPGRADE_HELPER)

    fake.names = ["some-other-pod"]
    code, data = post("/api/controller/upgrade", {"version": "9.9.9"})
    check(code == 400 and "No running controller" in data["error"],
          "upgrade without a visible controller fails cleanly")
finally:
    app.podman = _real_podman
    app.load_release = _real_load_release

# --- fleet rerender (applies engine updates to existing pods) ------------
# Needs a (stubbed) podman on PATH: rerender re-renders each pod's scripts
# from its saved .config.json and then executes run.sh. Keep this last —
# earlier tests rely on podman being absent.
stub_bin = os.path.join(pods, "..", "stub-bin")
os.makedirs(stub_bin, exist_ok=True)
with open(os.path.join(stub_bin, "podman"), "w") as f:
    # Same shape as the smoke-test stub: log every call; `ps` replays the
    # names started so far, so run.sh's sidecar liveness check passes.
    f.write(
        '#!/bin/sh\n'
        'LOG="${PODMAN_STUB_LOG:?}"\n'
        'echo "podman $*" >> "$LOG"\n'
        'case "${1:-}" in\n'
        '  ps) grep -o "run -d --name [^ ]*" "$LOG" 2>/dev/null'
        ' | awk \'{print $4}\' | sort -u ;;\n'
        'esac\n'
        'exit 0\n'
    )
os.chmod(os.path.join(stub_bin, "podman"), 0o755)
os.environ["PODMAN_STUB_LOG"] = os.path.join(stub_bin, "podman.log")
os.environ["PATH"] = stub_bin + os.pathsep + os.environ["PATH"]
os.environ["WAIT"] = "0"  # run.sh startup pauses off, for test speed

runsh = os.path.join(pods, "apitest", "run.sh")
before = os.path.getmtime(runsh)
code, data = post("/api/fleet", {"do": "rerender"})
check(code == 200 and data["ok"] and data["results"]
      and all(r["action"] == "rerender" and r["ok"] for r in data["results"]),
      "fleet rerender re-renders and restarts every non-controller pod")
check(os.path.getmtime(runsh) >= before, "rerender rewrote the pod's run.sh")

# --- NFS exports (podman faked in-process) --------------------------------
# The controller renders Pods/.exports and applies it on the HOST through a
# privileged nsenter helper. Assert the rendered exports syntax, the helper
# invocation shape, registry round-trips, and the friendly no-nfsd error.


class FakeNfsPodman:
    def __init__(self):
        self.calls = []
        self.mode = "ok"

    def __call__(self, *args, timeout=60):
        self.calls.append(list(args))
        if args[0] == "ps":
            return _sp.CompletedProcess(args, 0, "tailarr\ntailscale-tailarr\n", "")
        if args[0] == "inspect":
            return _sp.CompletedProcess(args, 0, "ghcr.io/scs32/tailarr:v9.9.9\n", "")
        if args[0] == "run":
            if self.mode == "missing":
                return _sp.CompletedProcess(args, 9, "NFS-SERVER-MISSING\n", "")
            return _sp.CompletedProcess(
                args, 0, "EXPORTS:\n/data 192.168.1.0/24\nHOSTIP: 192.168.64.7\n", "")
        return _sp.CompletedProcess(args, 0, "", "")


_real_podman = app.podman
nfs_fake = FakeNfsPodman()
app.podman = nfs_fake
try:
    code, data = post("/api/shares", {"do": "nfs", "name": "media",
                                      "enabled": True})
    check(code == 400 and "clients required" in data["error"],
          "nfs enable without a client list refused")
    code, data = post("/api/shares", {"do": "nfs", "name": "media",
                                      "enabled": True,
                                      "clients": "10.0.0.1;rm -rf /"})
    check(code == 400 and "Invalid client" in data["error"],
          "shell/exports metacharacters in clients rejected")
    code, data = post("/api/shares", {"do": "nfs", "name": "nope",
                                      "enabled": True, "clients": "10.0.0.1"})
    check(code == 400 and "Unknown share" in data["error"],
          "nfs on an unknown share rejected")

    code, data = post("/api/shares", {"do": "nfs", "name": "media",
                                      "enabled": True,
                                      "clients": "192.168.1.0/24"})
    check(code == 200 and data["ok"]
          and "nfs://192.168.64.7/data" in data["message"],
          "nfs enable succeeds; message carries the exact mount URL")
    with open(os.path.join(pods, ".exports")) as f:
        frag = f.read()
    check("/data 192.168.1.0/24(ro,all_squash,insecure)" in frag,
          "exports fragment: read-only, squashed, mac-mountable")
    helper = next(c for c in nfs_fake.calls if c[0] == "run")
    check("--privileged" in helper and "--pid=host" in helper
          and "nsenter" in helper and "ghcr.io/scs32/tailarr:v9.9.9" in helper,
          "helper nsenter's into the host from a privileged one-shot container")
    code, data = get("/api/shares")
    media = next(s for s in data["shares"] if s["name"] == "media")
    check(media["nfs"] == {"clients": "192.168.1.0/24", "ro": True},
          "export persisted in the shares registry")

    code, data = post("/api/shares", {"do": "nfs", "name": "media",
                                      "enabled": True,
                                      "clients": "192.168.1.5 mac.local",
                                      "ro": False})
    check(code == 200 and data["ok"], "read-write export with two clients")
    with open(os.path.join(pods, ".exports")) as f:
        frag = f.read()
    check("192.168.1.5(rw,all_squash,anonuid=1000,anongid=1000,insecure)" in frag
          and "mac.local(rw," in frag,
          "rw export maps writes to PUID 1000; every client gets the options")

    nfs_fake.mode = "missing"
    code, data = post("/api/shares", {"do": "nfs", "name": "media",
                                      "enabled": True, "clients": "10.0.0.1"})
    check(code == 400 and "nfs-kernel-server" in data["error"],
          "host without nfsd gets the one-line install hint")
    nfs_fake.mode = "ok"

    code, data = post("/api/shares", {"do": "nfs", "name": "media",
                                      "enabled": False})
    check(code == 200 and data["ok"], "nfs disable succeeds")
    with open(os.path.join(pods, ".exports")) as f:
        frag = f.read()
    check("/data " not in frag, "disable drops the share from the fragment")
    code, data = get("/api/shares")
    media = next(s for s in data["shares"] if s["name"] == "media")
    check(media["nfs"] is None, "registry cleared on disable")
    code, data = post("/api/shares", {"do": "nfs", "name": "media",
                                      "enabled": False})
    check(code == 400 and "no NFS export" in data["error"],
          "disabling a non-exported share refused")

    # --- systemd mounts drop-in: adding a share must order the boot unit
    # after its backing mount (nofail media disks raced the fleet at boot
    # and pods bind-mounted an empty /data — field report).
    nfs_fake.calls = []
    code, data = post("/api/shares", {"do": "add", "name": "disk2",
                                      "host_path": "/data2"})
    check(code == 200 and data["ok"], "share add succeeds with podman present")
    dropin = [c for c in nfs_fake.calls if c[0] == "run"
              and "RequiresMountsFor" in " ".join(c)]
    check(bool(dropin), "share add syncs the RequiresMountsFor drop-in")
    joined = " ".join(dropin[0])
    check("/data2" in joined and pods in joined
          and "daemon-reload" in joined
          and "tailarr-pods.service.d" in joined,
          "drop-in covers PODS_DIR + the new share and reloads systemd")
    nfs_fake.calls = []
    code, data = post("/api/shares", {"do": "delete", "name": "disk2"})
    check(code == 200 and any(
        c[0] == "run" and "RequiresMountsFor" in " ".join(c)
        for c in nfs_fake.calls),
        "share delete re-syncs the drop-in")
finally:
    app.podman = _real_podman

# --- sidecar identity tags: retry, surface, reconcile ---------------------
# Field report (HIGH): radarr's sidecar never got tag:tailarr-svc-radarr
# (one silent background attempt, no retry, no visibility) so every user
# device was dropped at the packet filter while the service looked green.


class FakeTsApi:
    """Devices list with a mis-tagged pod; POST /tags controllable."""

    def __init__(self):
        self.mode = "reject"
        self.tag_posts = []

    def __call__(self, method, path, body=None):
        if method == "GET" and path == "/tailnet/-/devices":
            return 200, {"devices": [
                {"hostname": "apitest", "nodeId": "n1",
                 "tags": ["tag:tailarr", "tag:tailarr-public"],
                 "lastSeen": "2026-07-16T00:00:00Z"},
            ]}
        if method == "POST" and path.startswith("/device/"):
            self.tag_posts.append(body)
            if self.mode == "reject":
                return 400, {"message": "tag not permitted (tagOwners)"}
            return 200, {}
        return 200, {}


_real_ts_token2 = app._ts_token
_real_ts_api2 = app.ts_api
fake_ts = FakeTsApi()
app._ts_token = lambda: "dummy-test-token"
app.ts_api = fake_ts
try:
    state = app.ts_tag_sidecar("apitest", attempts=2)
    check(state == "missing" and len(fake_ts.tag_posts) == 2,
          "rejected tag write retries with backoff, then records 'missing'")
    code, data = get("/api/pods")
    pod = next(p for p in data["pods"] if p["name"] == "apitest")
    check(pod["identity"] == "missing",
          "mis-tagged pod surfaces identity=missing in /api/pods")

    fake_ts.mode = "ok"
    fake_ts.tag_posts = []
    state = app.ts_tag_sidecar("apitest", attempts=1)
    check(state == "ok", "tag applied once the tags API accepts")
    check(fake_ts.tag_posts
          and "tag:tailarr-svc-apitest" in fake_ts.tag_posts[0]["tags"]
          and "tag:tailarr-public" in fake_ts.tag_posts[0]["tags"],
          "svc tag applied; tag:tailarr-public preserved")
    code, data = get("/api/pods")
    pod = next(p for p in data["pods"] if p["name"] == "apitest")
    check(pod["identity"] == "ok", "identity flips to ok after the fix")

    # Reconcile pass: running sidecars re-tagged, stopped pods left alone.
    tagged = []
    _real_tag = app.ts_tag_sidecar
    app.ts_tag_sidecar = lambda n, attempts=6: tagged.append(n)
    _real_podman2 = app.podman
    app.podman = lambda *a, **kw: _sp.CompletedProcess(
        a, 0, "apitest\ntailscale-apitest\n", "")
    try:
        app.ts_reconcile_tags()
    finally:
        app.ts_tag_sidecar = _real_tag
        app.podman = _real_podman2
    check(tagged == ["apitest"],
          "reconcile re-tags running sidecars only")
    check(app._tag_state.get("extpod") == "unknown",
          "stopped pod reads identity=unknown, not a false 'missing'")
finally:
    app._ts_token = _real_ts_token2
    app.ts_api = _real_ts_api2

# --- API errors are always JSON (never a dropped connection) --------------
# An unexpected exception in a handler used to close the socket with no
# HTTP response at all — scripted callers saw a bare RemoteDisconnected.


def _boom(*a, **kw):
    raise RuntimeError("kaboom")


_real_upgrade_op = app.op_controller_upgrade
app.op_controller_upgrade = _boom
try:
    code, data = post("/api/controller/upgrade", {})
    check(code == 500 and data["ok"] is False and "kaboom" in data["error"],
          "handler crash returns JSON 500, not a dropped connection")
finally:
    app.op_controller_upgrade = _real_upgrade_op

# --- "server" pseudo-service: grant the controller itself -----------------
# tag:tailarr-can-server opens the network path to tag:tailarr-ctrl:443;
# the API bearer tokens (next suite) are the permission boundary behind it.
secs = app._managed_sections()
check(any("tag:tailarr-can-server" in ln and "tag:tailarr-ctrl" in ln
          for ln in secs["grants"]),
      "managed grants include can-server -> tailarr-ctrl:443")
check(any(ln.startswith('"tag:tailarr-can-server"')
          for ln in secs["tagowners"]),
      "managed tagOwners define tag:tailarr-can-server")
check(any(ln.startswith('"tag:tailarr-ctrl"') and '"tag:tailarr-ctrl"' in
          ln[len('"tag:tailarr-ctrl"'):] for ln in secs["tagowners"]),
      "tag:tailarr-ctrl owns itself (OAuth client can self-assign it)")
check(app._sections_prefix_ok(secs),
      "can-server content passes the tag prefix invariant")

code, data = get("/api/users")
check(code == 200 and "server" in data["services"],
      "'server' is offered as a grantable service on the Users page")

_real_ts_token3 = app._ts_token
_real_ts_api3 = app.ts_api
_tagwrite = {}


def _fake_ts_access(method, path, body=None):
    if method == "GET" and path.startswith("/device/"):
        return 200, {"nodeId": "node1", "tags": ["tag:tailarr-user"]}
    if method == "POST" and path.endswith("/tags"):
        _tagwrite["tags"] = body["tags"]
        return 200, {}
    return 200, {"devices": []}


app._ts_token = lambda: "dummy-test-token"
app.ts_api = _fake_ts_access
try:
    code, data = post("/api/users/node1/access",
                      {"service": "server", "allow": True})
    check(code == 200 and data["ok"]
          and "tag:tailarr-can-server" in _tagwrite["tags"],
          "granting 'server' flips tag:tailarr-can-server on the device")
    code, data = post("/api/users/node1/access",
                      {"service": "no-such-svc", "allow": True})
    check(code == 400 and not data["ok"],
          "unknown services are still rejected")
finally:
    app._ts_token = _real_ts_token3
    app.ts_api = _real_ts_api3

# --- startup policy sync: upgrades that add managed tags self-apply -------
# (v0.10.0 added can-server; an upgraded-but-idle controller never synced,
# so the first server grant failed. _maintenance_loop now syncs once at
# start — assert the extracted helper syncs when a credential exists and
# stays quiet when none does.)
_real_ts_token4 = app._ts_token
_real_sync4 = app.ts_policy_sync
_sync_calls = []
app.ts_policy_sync = lambda: (_sync_calls.append(1),
                              {"ok": True, "changed": True,
                               "error": None})[1]
try:
    app._ts_token = lambda: ""
    app._startup_policy_sync()
    check(not _sync_calls, "startup sync is a no-op without a credential")
    app._ts_token = lambda: "dummy-test-token"
    app._startup_policy_sync()
    check(len(_sync_calls) == 1, "startup sync runs once a credential exists")

    def _sync_boom():
        raise RuntimeError("api down")
    app.ts_policy_sync = _sync_boom
    app._startup_policy_sync()  # must not raise — startup can't be blocked
    check(True, "startup sync swallows API failures (startup never blocks)")
finally:
    app._ts_token = _real_ts_token4
    app.ts_policy_sync = _real_sync4

# --- API bearer tokens: mint, require, gate, auto-relax -------------------


def get_h(path, token=None):
    hdrs = {"Authorization": "Bearer " + token} if token else {}
    req = urllib.request.Request(BASE + path, headers=hdrs)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def post_h(path, body, token=None):
    hdrs = {"Content-Type": "application/json"}
    if token:
        hdrs["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


code, data = get("/api/tokens")
check(code == 200 and data["require"] is False and data["tokens"] == [],
      "tokens start empty with require off (API open, historical model)")

code, data = post("/api/tokens", {"do": "require", "enabled": True})
check(code == 400 and "Create a token first" in data["error"],
      "require with zero tokens is refused (would lock everyone out)")

code, data = post("/api/tokens", {"do": "create", "label": "phone"})
check(code == 200 and data["ok"] and data["token"].startswith("tailarr-tok-"),
      "minting returns a tailarr-tok- secret")
tok1 = data["token"]
tokfile = os.path.join(pods, ".tokens.json")
check(os.stat(tokfile).st_mode & 0o777 == 0o600,
      "token registry is 0600")
check(tok1 not in open(tokfile).read(),
      "the plaintext secret is never stored (hash only)")

code, data = get("/api/tokens")
check(code == 200 and len(data["tokens"]) == 1
      and data["tokens"][0]["label"] == "phone"
      and "sha256" not in data["tokens"][0]
      and "token" not in data["tokens"][0],
      "token list carries label/id only, never secrets")

code, data = post("/api/tokens", {"do": "require", "enabled": True})
check(code == 200 and data["ok"], "require flips on once a token exists")

code, data = get_h("/api/pods")
check(code == 401, "tokenless GET is rejected once require is on")
code, data = post_h("/api/tokens", {"do": "create", "label": "x"})
check(code == 401, "tokenless POST is rejected once require is on")
code, data = get_h("/api/pods", token="tailarr-tok-wrong")
check(code == 401, "a wrong token is rejected")
code, data = get_h("/api/pods", token=tok1)
check(code == 200, "the minted token opens the API")
code, data = get_h("/api/info")
check(code == 200 and "version" in data,
      "/api/info stays open (upgrade health gate + app compat probe)")

code, data = post_h("/api/tokens", {"do": "create", "label": "second"},
                    token=tok1)
check(code == 200 and data["ok"], "an authed client can mint more tokens")
tid2 = data["id"]
code, data = post_h("/api/tokens", {"do": "delete", "id": tid2}, token=tok1)
check(code == 200 and data["ok"], "token delete works")
tid1 = get_h("/api/tokens", token=tok1)[1]["tokens"][0]["id"]
code, data = post_h("/api/tokens", {"do": "delete", "id": tid1}, token=tok1)
check(code == 200 and data["ok"], "the last token can delete itself")
code, data = get("/api/tokens")
check(code == 200 and data["require"] is False and data["tokens"] == [],
      "deleting the last token auto-relaxes require (no lockout state)")

# --- private registries: validate, store 0600, render authfile ------------
import base64  # noqa: E402

code, data = get("/api/registries")
check(code == 200 and data["registries"] == [], "registries start empty")

code, data = post("/api/registries", {"do": "save",
                  "registry": "https://ghcr.io",
                  "username": "u", "secret": "s"})
check(code == 400 and "hostname" in data["error"],
      "registry hosts with a scheme are rejected")

code, data = post("/api/registries", {"do": "save", "registry": "ghcr.io",
                  "username": "", "secret": "s"})
check(code == 400, "a missing username is rejected")

_real_probe = app._registry_login_probe
try:
    app._registry_login_probe = lambda h, u, s: (False, "401 unauthorized")
    code, data = post("/api/registries", {"do": "save", "registry": "ghcr.io",
                      "username": "scs32",
                      "secret": "dummy-test-registry-secret"})
    check(code == 400 and "rejected" in data["error"],
          "a failing registry login blocks the save")

    app._registry_login_probe = lambda h, u, s: (True, None)
    code, data = post("/api/registries", {"do": "save", "registry": "ghcr.io",
                      "username": "scs32",
                      "secret": "dummy-test-registry-secret"})
    check(code == 200 and data["ok"], "a validated credential saves")
finally:
    app._registry_login_probe = _real_probe

regfile = os.path.join(pods, ".registries.json")
authfile = os.path.join(pods, ".registry-auth.json")
check(os.stat(regfile).st_mode & 0o777 == 0o600, "credential store is 0600")
check(os.stat(authfile).st_mode & 0o777 == 0o600, "rendered authfile is 0600")
with open(authfile) as f:
    auths = json.load(f)["auths"]
check(auths["ghcr.io"]["auth"]
      == base64.b64encode(b"scs32:dummy-test-registry-secret").decode(),
      "authfile carries containers-auth base64 for podman/skopeo")

code, data = get("/api/registries")
check(code == 200 and data["registries"] == [
      {"registry": "ghcr.io", "username": "scs32",
       "created": data["registries"][0]["created"]}],
      "registry list carries host + username only, never the secret")

with open(os.path.join(pods, "apitest", "run.sh")) as f:
    check("REGISTRY_AUTH_FILE" in f.read(),
          "generated run.sh exports the authfile to podman when present")

check(app.registry_env()["REGISTRY_AUTH_FILE"] == authfile,
      "controller podman/skopeo calls point at the authfile")

code, data = post("/api/registries", {"do": "delete", "registry": "nope.io"})
check(code == 400, "deleting an unknown registry fails")
code, data = post("/api/registries", {"do": "delete", "registry": "ghcr.io"})
check(code == 200 and data["ok"] and not os.path.exists(authfile),
      "deleting the last registry removes the authfile")
check(app.registry_env() is None,
      "without credentials the environment is untouched")

catsrv.shutdown()
srv.shutdown()
print("WEB API TEST PASSED")
