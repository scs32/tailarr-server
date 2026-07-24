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

# --- Netmap minimality: user-device visibility never widens ---------------
# docs/acl-design.md §12: ANY rule matching a user device on any port in
# either direction makes the peer visible in its netmap, so generated
# grants may connect user selectors to exactly their badges' targets
# (plus cap-only relay grants). Enforced fail-closed in ts_policy_sync.
check(app._grants_minimality_ok(secs["grants"]),
      "generated grants pass the netmap-minimality invariant")
for _dst in ("admin", "member", ""):
    check(app._grants_minimality_ok(
        app._managed_sections(relay_dst=_dst)["grants"]),
        f"relay shape {_dst or 'off'!r} passes netmap minimality")
for _line, _why in [
    ('{"src": ["tag:tailarr-user"], "dst": ["tag:tailarr"], "ip": ["*"]},',
     "a user catch-all src"),
    ('{"src": ["tag:tailarr"], "dst": ["tag:tailarr-user"], "ip": ["443"]},',
     "a user device in dst (reverse visibility)"),
    ('{"src": ["tag:tailarr-can-radarr", "tag:tailarr-user"], '
     '"dst": ["tag:tailarr-svc-radarr"], "ip": ["443"]},',
     "a user tag bundled into a network src"),
    ('{"src": ["tag:tailarr-can-radarr"], "dst": ["tag:tailarr-svc-radarr", '
     '"tag:tailarr-ctrl"], "ip": ["443"]},',
     "a badge grant with more than one dst"),
    ('{"src": ["tag:tailarr"], "dst": ["tag:tailarr-can-radarr"], '
     '"app": {"tailscale.com/cap/relay": []}}, // x',
     "a cap grant targeting user devices"),
    ('this is not a grant line', "an unparseable grant line"),
]:
    check(not app._grants_minimality_ok([_line]),
          f"minimality rejects {_why}")

_real_managed_sections = app._managed_sections
app._managed_sections = lambda relay_dst=None: {
    "grants": ['{"src": ["tag:tailarr-user"], "dst": ["tag:tailarr"], '
               '"ip": ["*"]},'],
    "tagowners": [], "nodeattrs": []}
try:
    _r = app.ts_policy_sync()
    check(_r["ok"] is False and "minimality" in _r["error"],
          "ts_policy_sync fails closed on a visibility-widening grant")
finally:
    app._managed_sections = _real_managed_sections

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

# --- peer relay: platform gate, pre-flight, grant, fallback, verify -------
import types  # noqa: E402

# v0.15.0: the feature is offered on every platform (applicable always
# true) but nothing is granted or recommended before a platform fact —
# auto-emission stays apple-container-only.
code, data = get("/api/relay")
check(code == 200 and data["applicable"] is True
      and data["recommended"] is False
      and data["grant_active"] is False
      and data["mode"] == "global" and data["relays"] == [],
      "relay is offered but inert before a platform fact exists")
code, data = get("/api/info")
check(data["host_platform"] == "unknown" and "relay" in data,
      "/api/info carries host_platform + relay")
secs = app._managed_sections()
check(not any("cap/relay" in ln for ln in secs["grants"]),
      "no relay grant without a platform fact")

# Become an apple/container install.
with open(os.path.join(pods, ".host.json"), "w") as f:
    json.dump({"platform": "apple-container", "pid1": "vminitd",
               "detected_by": "test"}, f)
app._host_platform_cache = None
check(app.host_platform() == "apple-container", "platform fact is read")

FENCED_POLICY = "\n".join([
    "{",
    "  // >>> tailarr-managed:grants",
    "  // <<< tailarr-managed:grants",
    '  "tagOwners": {',
    "    // >>> tailarr-managed:tagowners",
    "    // <<< tailarr-managed:tagowners",
    "  },",
    "  // >>> tailarr-managed:nodeattrs",
    "  // <<< tailarr-managed:nodeattrs",
    "}",
])


def _devices(n_foreign, n_users, n_tagged=2):
    devs = [{"nodeId": f"t{i}", "hostname": f"tailarr-{i}",
             "tags": ["tag:tailarr"], "user": "admin@x"}
            for i in range(n_tagged)]
    devs += [{"nodeId": f"f{i}", "hostname": f"dev-{i}", "tags": [],
              "user": f"user{i % max(n_users, 1)}@x"}
             for i in range(n_foreign)]
    return {"devices": devs}


_real_ts_token5 = app._ts_token
_real_ts_acl5 = app._ts_acl
_real_ts_api5 = app.ts_api
_real_sync5 = app.ts_policy_sync
_real_reconcile5 = app.ts_reconcile_tags
try:
    app._ts_token = lambda: "dummy-test-token"
    app._ts_acl = lambda m, p="", b=None, etag=None: (200, FENCED_POLICY, "e1")
    app.ts_api = lambda m, p, b=None: (200, _devices(3, 1))
    app.ts_reconcile_tags = lambda *a, **k: None

    pf = app.ts_relay_preflight()
    check(pf["eligible"] and pf["reasons"] == []
          and pf["counts"]["foreign_devices"] == 3,
          "pre-flight passes on a small dedicated-looking tailnet")
    app.save_relay({"preflight": pf})
    secs = app._managed_sections()
    check(any("cap/relay" in ln and "autogroup:admin" in ln
              for ln in secs["grants"]),
          "eligible auto mode emits the relay grant (autogroup:admin dst)")
    check(any("tag:tailarr-relay" in ln for ln in secs["tagowners"]),
          "tag:tailarr-relay lands in tagOwners")
    check(app._sections_prefix_ok(secs),
          "the relay grant passes the tag prefix invariant")

    # Splice idempotency with the relay sections present.
    text1 = app._splice_fences(FENCED_POLICY, secs)
    check(app._splice_fences(text1, secs) == text1,
          "relay sections splice idempotently")

    # A busy tailnet flips the verdict and withholds the grant.
    app.ts_api = lambda m, p, b=None: (200, _devices(14, 4))
    pf = app.ts_relay_preflight()
    check(not pf["eligible"] and len(pf["reasons"]) == 2,
          "pre-flight fails on device count AND user count")
    app.save_relay({"preflight": pf})
    check(not any("cap/relay" in ln
                  for ln in app._managed_sections()["grants"]),
          "ineligible auto mode withholds the grant")

    # Explicit user enable overrides the verdict (opt-in banner button).
    app.ts_policy_sync = lambda: {"ok": True, "changed": True, "error": None}
    code, data = post("/api/relay", {"do": "enable"})
    check(code == 200 and data["ok"] and data["relay"]["grant_active"],
          "POST /api/relay enable overrides an ineligible verdict")
    relayfile = os.path.join(pods, ".relay.json")
    check(os.stat(relayfile).st_mode & 0o777 == 0o600,
          "relay state file is 0600")
    saved = app.load_relay()
    check(saved["enabled"] is True and saved["decided_by"] == "user",
          "the explicit decision is recorded as the user's")
    check(any("cap/relay" in ln for ln in app._managed_sections()["grants"]),
          "user enable emits the grant regardless of pre-flight")

    code, data = post("/api/relay", {"do": "bogus"})
    check(code == 400, "unknown relay action is a 400")

    # Validate-rejection ladder: admin dst -> member dst -> grant dropped.
    # The fake control plane rejects any policy containing autogroup:admin
    # in the relay grant, accepts everything else.
    def _acl_reject_admin(method, path_suffix="", body_text=None, etag=None):
        if method == "GET":
            return 200, FENCED_POLICY, "e1"
        if path_suffix == "/validate" and "autogroup:admin\"]" in (
                body_text or "") and "cap/relay" in (body_text or ""):
            return 200, '{"message": "autogroup:admin not allowed here"}', ""
        if path_suffix == "/validate":
            return 200, "{}", ""
        return 200, "", "e2"

    app._ts_acl = _acl_reject_admin
    app.ts_policy_sync = _real_sync5
    r = app.load_relay()
    r["dst_fallback"] = False
    app.save_relay(r)
    sync = app.ts_policy_sync()
    check(sync["ok"] and app.load_relay()["dst_fallback"] is True,
          "validate reject of autogroup:admin falls back to member dst")
    check(any("autogroup:member\"]" in ln
              for ln in app._managed_sections()["grants"] if "cap/relay" in ln),
          "the emitted grant now carries the member dst")

    # ...and when the grant is rejected in ANY form, it is dropped rather
    # than wedging the sync (the relay-free probe validates fine).
    def _acl_reject_relay(method, path_suffix="", body_text=None, etag=None):
        if method == "GET":
            return 200, FENCED_POLICY, "e1"
        if path_suffix == "/validate" and "cap/relay" in (body_text or ""):
            return 200, '{"message": "no cap grants for you"}', ""
        if path_suffix == "/validate":
            return 200, "{}", ""
        return 200, "", "e2"

    app._ts_acl = _acl_reject_relay
    r = app.load_relay()
    r["dst_fallback"] = False
    r["enabled"] = True
    app.save_relay(r)
    sync = app.ts_policy_sync()
    saved = app.load_relay()
    check(sync["ok"] and saved["enabled"] is False
          and saved["decided_by"] == "auto-validate-reject",
          "a fully-rejected relay grant disables itself; sync still lands")
    check(any("rejected the relay grant" in x
              for x in saved["preflight"]["reasons"]),
          "the rejection reason is surfaced for the banner")

    # Disable drops the grant on the next splice.
    app._ts_acl = lambda m, p="", b=None, etag=None: (200, FENCED_POLICY, "e1")
    app.ts_policy_sync = lambda: {"ok": True, "changed": True, "error": None}
    code, data = post("/api/relay", {"do": "disable"})
    check(code == 200 and data["ok"]
          and not data["relay"]["grant_active"],
          "POST /api/relay disable withdraws the grant")
finally:
    app._ts_token = _real_ts_token5
    app._ts_acl = _real_ts_acl5
    app.ts_api = _real_ts_api5
    app.ts_policy_sync = _real_sync5
    app.ts_reconcile_tags = _real_reconcile5

# Sidecar connectivity classification (canned `tailscale status --json`).
_real_podman5 = app.podman
_real_ctrl5 = app._controller_name


def _fake_status_podman(doc):
    def fake(*args, timeout=60):
        if "status" in args:
            return types.SimpleNamespace(returncode=0,
                                         stdout=json.dumps(doc), stderr="")
        if "version" in args:
            return types.SimpleNamespace(returncode=0,
                                         stdout="1.90.1\n", stderr="")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="nope")
    return fake


try:
    app._controller_name = lambda: "tailarr"
    app.podman = _fake_status_podman(
        {"Peer": {"a": {"Active": True, "PeerRelay": "100.64.0.9:40000",
                        "CurAddr": "", "Relay": "sfo"}}})
    check(app.relay_verify()["state"] == "peer-relay",
          "an active PeerRelay peer classifies as peer-relay")
    app.podman = _fake_status_podman(
        {"Peer": {"a": {"Active": True, "PeerRelay": "",
                        "CurAddr": "1.2.3.4:41641", "Relay": "sfo"}}})
    check(app.relay_verify()["state"] == "direct",
          "a CurAddr peer classifies as direct")
    app.podman = _fake_status_podman(
        {"Peer": {"a": {"Active": True, "PeerRelay": "",
                        "CurAddr": "", "Relay": "sfo"}}})
    v = app.relay_verify()
    check(v["state"] == "derp" and "1.90.1" in v["detail"],
          "a DERP-only peer classifies as derp and surfaces the version")
finally:
    app.podman = _real_podman5
    app._controller_name = _real_ctrl5

# --- relay registry (v0.15.0): picker, modes, per-pod grants, verify ------
_real_sync6 = app.ts_policy_sync
_real_preflight6 = app.ts_relay_preflight
_real_ts_api6 = app.ts_api
_real_host_exec6 = app._host_exec
try:
    app.ts_policy_sync = lambda: {"ok": True, "changed": True, "error": None}
    app.ts_relay_preflight = lambda: {"eligible": False, "reasons": ["x"],
                                      "counts": {}, "fences_present": True,
                                      "checked_at": 0}
    app._host_exec = lambda helper, script, timeout=60: (-1, "no host")

    code, data = post("/api/relay", {"do": "enable"})
    check(code == 200 and data["relay"]["enabled"] is True,
          "feature re-enabled for the registry suite")
    _r = app.load_relay()
    _r.pop("dst_fallback", None)  # learned during the ladder suite; reset
    app.save_relay(_r)

    code, data = post("/api/relay", {"do": "add-relay", "ip": "not-an-ip"})
    check(code == 400, "add-relay rejects a non-IP")

    code, data = post("/api/relay", {"do": "add-relay", "ip": "100.99.0.5",
                                     "name": "office-mac"})
    check(code == 200 and data["command"].startswith("tailscale set "),
          "add-relay returns the local enable command")
    rl = data["relay"]
    # (the registry already holds 100.64.0.9, auto-discovered by the
    # classification suite's relay_verify run above — by design)
    e5 = [e for e in rl["relays"] if e["id"] == "100.99.0.5"][0]
    check(e5["status"] == "pending" and e5["name"] == "office-mac",
          "the relay lands in the registry as pending")
    check(rl["global_relay"] == "100.99.0.5",
          "the first explicitly-added relay in global mode is auto-selected")
    secs = app._managed_sections()
    relay_lines = [ln for ln in secs["grants"] if "cap/relay" in ln]
    check(len(relay_lines) == 1 and '"100.99.0.5/32"' in relay_lines[0]
          and "autogroup:" not in relay_lines[0].split('"dst"')[1].split("app")[0],
          "global grant dst is the specific relay IP, not the autogroup ladder")
    check(not any("tag:tailarr-relay" in ln for ln in secs["tagowners"]),
          "specific-IP dst drops the tag:tailarr-relay owner")
    check(app._sections_prefix_ok(secs),
          "IP dst passes the tag prefix invariant")
    check(app._legacy_relay_dst_in_use() is False,
          "specific-IP dst opts out of the admin->member downgrade rung")

    # v0.15.2: the host-exec special case is REMOVED — capability is
    # always enabled on the device itself; every add lands pending.
    code, data = post("/api/relay", {"do": "add-relay", "ip": "100.99.0.7",
                                     "name": "vm-host"})
    e7 = [e for e in data["relay"]["relays"] if e["id"] == "100.99.0.7"][0]
    check(code == 200 and e7["status"] == "pending",
          "added relays are pending until traffic proves them")
    code, data = post("/api/relay", {"do": "add-relay", "host": True})
    check(code == 400 and "Pick a device" in data["error"],
          "the retired host shortcut is now just a missing-ip 400")
    code, data = post("/api/relay", {"do": "add-relay", "ip": ""})
    check(code == 400 and "Pick a device" in data["error"],
          "an empty add is a clear 400, never a silent no-op")

    # Clearing the global selection restores the legacy autogroup grant.
    code, data = post("/api/relay", {"do": "set-global", "id": ""})
    secs = app._managed_sections()
    relay_lines = [ln for ln in secs["grants"] if "cap/relay" in ln]
    check(code == 200 and len(relay_lines) == 1
          and "autogroup:admin" in relay_lines[0]
          and app._legacy_relay_dst_in_use() is True,
          "clearing the global relay restores the autogroup-dst grant")

    # Per-pod mode: one grant per selection, keyed on the svc tag; the
    # "server" key means the controller.
    code, data = post("/api/relay", {"do": "mode", "mode": "per-pod"})
    check(code == 200 and data["relay"]["mode"] == "per-pod",
          "mode flips to per-pod")
    secs = app._managed_sections()
    check(not any("cap/relay" in ln for ln in secs["grants"]),
          "per-pod mode with no selections emits no relay grants")
    code, data = post("/api/relay", {"do": "set-pod", "pod": "nope",
                                     "id": "100.99.0.5"})
    check(code == 400, "set-pod rejects an unknown service")
    code, data = post("/api/relay", {"do": "set-pod", "pod": "apitest",
                                     "id": "100.99.0.5"})
    check(code == 200 and data["relay"]["pod_relays"]["apitest"]
          == "100.99.0.5", "a pod gets its own relay")
    code, data = post("/api/relay", {"do": "set-pod", "pod": "server",
                                     "id": "100.99.0.7"})
    check(code == 200, "the controller (server) gets its own relay")
    relay_lines = [ln for ln in app._managed_sections()["grants"]
                   if "cap/relay" in ln]
    check(len(relay_lines) == 2, "one grant per per-pod selection")
    check(any("tag:tailarr-svc-apitest" in ln and '"100.99.0.5/32"' in ln
              for ln in relay_lines),
          "the pod grant pairs its svc tag with its chosen relay")
    check(any("tag:tailarr-ctrl" in ln and '"100.99.0.7/32"' in ln
              for ln in relay_lines),
          "the server grant pairs tag:tailarr-ctrl with its relay")
    code, data = post("/api/relay", {"do": "set-pod", "pod": "apitest",
                                     "id": ""})
    check(code == 200 and "apitest" not in data["relay"]["pod_relays"],
          "set-pod with an empty id clears the selection")

    # remove-relay scrubs every reference.
    code, data = post("/api/relay", {"do": "remove-relay",
                                     "id": "100.99.0.7"})
    check(code == 200
          and all(e["id"] != "100.99.0.7" for e in data["relay"]["relays"])
          and data["relay"]["pod_relays"] == {},
          "remove-relay drops the entry and its selections")

    # Candidate picker: fleet-tagged devices are filtered out.
    app.ts_api = lambda m, p, b=None: (200, {"devices": [
        {"hostname": "mac", "name": "mac.tail.ts.net", "os": "macOS",
         "user": "s@x", "addresses": ["100.64.0.1", "fd7a::1"], "tags": []},
        {"hostname": "pod", "name": "pod.tail.ts.net", "os": "linux",
         "user": "s@x", "addresses": ["100.64.0.2"],
         "tags": ["tag:tailarr"]}]})
    code, data = get("/api/relay/devices")
    check(code == 200 and [d["ip"] for d in data["devices"]]
          == ["100.64.0.1"],
          "/api/relay/devices lists candidates, filtering the fleet")

    # relay_verify graduates pending entries seen in PeerRelay and
    # discovers relays it never knew about.
    r = app.load_relay()
    r["relays"]["100.99.0.5"]["status"] = "pending"
    app.save_relay(r)
    app._controller_name = lambda: "tailarr"
    app.podman = _fake_status_podman(
        {"Peer": {"a": {"Active": True,
                        "PeerRelay": "100.99.0.5:40000"},
                  "b": {"Active": True,
                        "PeerRelay": "100.99.0.9:40000"}}})
    check(app.relay_verify()["state"] == "peer-relay",
          "verify classifies peer-relay traffic")
    saved = app.load_relay()["relays"]
    check(saved["100.99.0.5"]["status"] == "active",
          "a pending relay graduates when traffic flows through it")
    check(saved["100.99.0.9"]["status"] == "active"
          and saved["100.99.0.9"].get("discovered"),
          "an unknown active relay is discovered into the registry")
finally:
    app.podman = _real_podman5
    app._controller_name = _real_ctrl5
    app.ts_policy_sync = _real_sync6
    app.ts_relay_preflight = _real_preflight6
    app.ts_api = _real_ts_api6
    app._host_exec = _real_host_exec6

# --- custom pods: the user-authored "custom" catalog source ---------------
code, data = post("/api/custompods", {"do": "save", "name": "Bad Name",
                  "image": "x"})
check(code == 400, "custom pod names are validated")
code, data = post("/api/custompods", {"do": "save", "name": "cpod",
                  "image": ""})
check(code == 400 and "image" in data["error"], "an image is required")
_builtin_name = sorted(app.load_services())[0]
code, data = post("/api/custompods", {"do": "save", "name": _builtin_name,
                  "image": "docker.io/x"})
check(code == 400 and "built-in" in data["error"],
      "built-in catalog names cannot be shadowed")

code, data = post("/api/custompods", {"do": "save", "name": "cpod",
                  "image": "ghcr.io/x/y:latest",
                  "ports": {"9999": "9999"},
                  "environment": {"TZ": "UTC"}})
check(code == 200 and data["ok"], "a valid custom pod saves")
code, data = get("/api/catalog")
entry = [c for c in data["catalog"] if c["name"] == "cpod"]
check(len(entry) == 1 and entry[0]["source"] == "custom"
      and entry[0]["image"] == "ghcr.io/x/y:latest",
      "the custom pod appears in the catalog under the custom source")
spec = app.resolve_service("cpod")
check(spec is not None and spec["_source"] == "custom"
      and spec["network_mode"] == "bridge",
      "install-by-name resolves the custom spec with sane defaults")

code, data = post("/api/custompods", {"do": "delete", "name": "nope"})
check(code == 400, "deleting an unknown custom pod fails")
code, data = post("/api/custompods", {"do": "delete", "name": "cpod"})
check(code == 200 and data["ok"], "custom pods delete")
code, data = get("/api/catalog")
check(not any(c["name"] == "cpod" for c in data["catalog"]),
      "a deleted custom pod leaves the catalog")

# --- relay live-fixes (v0.15.3): cmdline detect, ready state, preflight ---
_real_cmdline = app.CMDLINE_PATH
_real_podman7 = app.podman
_real_ctrl7 = app._controller_name
_real_preflight7 = app.ts_relay_preflight
try:
    # Platform detection from the kernel cmdline — including CORRECTING a
    # wrong verdict left by the v0.13.0 pid1 check (the live 07-21 bug).
    cmdfile = os.path.join(pods, "fake-cmdline")
    with open(cmdfile, "w") as f:
        f.write("console=hvc0 panic=0 init=/sbin/vminitd ro root=/dev/vda\n")
    app.CMDLINE_PATH = cmdfile
    with open(os.path.join(pods, ".host.json"), "w") as f:
        json.dump({"platform": "linux", "pid1": "sleep"}, f)
    app._host_platform_cache = None
    app._detect_host_platform()
    check(app.host_platform() == "apple-container",
          "a wrong 'linux' verdict is corrected from the kernel cmdline")
    with open(os.path.join(pods, ".host.json")) as f:
        hj = json.load(f)
    check(hj["corrected_from"] == "linux"
          and hj["detected_by"] == "controller-cmdline",
          ".host.json records what was corrected and how")
    with open(cmdfile, "w") as f:
        f.write("BOOT_IMAGE=/vmlinuz root=/dev/sda1 ro quiet\n")
    app._detect_host_platform()
    app._host_platform_cache = None
    check(app.host_platform() == "linux",
          "a linux cmdline detects (and corrects back to) linux")
    # ...and restore apple-container for the remaining suites.
    with open(cmdfile, "w") as f:
        f.write("init=/sbin/vminitd ro\n")
    app._detect_host_platform()
    app._host_platform_cache = None

    # First-boot pre-flight: recorded once, never overwritten.
    app.ts_relay_preflight = lambda: {"eligible": True, "reasons": [],
                                      "counts": {}, "fences_present": True,
                                      "checked_at": 1}
    r = app.load_relay()
    r.pop("preflight", None)
    app.save_relay(r)
    app._startup_relay_preflight()
    check(app.load_relay()["preflight"]["checked_at"] == 1,
          "first boot records a pre-flight verdict")
    app.ts_relay_preflight = lambda: {"eligible": False, "reasons": ["x"],
                                      "counts": {}, "fences_present": True,
                                      "checked_at": 2}
    app._startup_relay_preflight()
    check(app.load_relay()["preflight"]["checked_at"] == 1,
          "an existing verdict is never overwritten at startup")

    # Ready state: advertising devices stop nagging about the command.
    def _fake_relay_podman(status_doc, advertising):
        def fake(*args, timeout=60):
            if "peer-relay-servers" in args:
                return types.SimpleNamespace(
                    returncode=0, stdout=json.dumps(advertising), stderr="")
            if "status" in args:
                return types.SimpleNamespace(
                    returncode=0, stdout=json.dumps(status_doc), stderr="")
            if "version" in args:
                return types.SimpleNamespace(returncode=0,
                                             stdout="1.98.9\n", stderr="")
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")
        return fake

    app._controller_name = lambda: "tailarr"
    r = app.load_relay()
    r["relays"] = {"100.99.0.5": {"name": "mac", "ip": "100.99.0.5",
                                  "added_at": 1, "status": "pending"},
                   "100.99.0.6": {"name": "old", "ip": "100.99.0.6",
                                  "added_at": 1, "status": "active"}}
    app.save_relay(r)
    app.podman = _fake_relay_podman({"Peer": {}}, ["100.99.0.5"])
    app.relay_verify()
    saved = app.load_relay()["relays"]
    check(saved["100.99.0.5"]["status"] == "ready",
          "an advertising relay graduates pending -> ready (no command nag)")
    check(saved["100.99.0.6"]["status"] == "active",
          "an active relay is never demoted by the advertising probe")
    app.podman = _fake_relay_podman({"Peer": {}}, [])
    app.relay_verify()
    check(app.load_relay()["relays"]["100.99.0.5"]["status"] == "pending",
          "a relay that stops advertising drops back to pending")
finally:
    app.CMDLINE_PATH = _real_cmdline
    app.podman = _real_podman7
    app._controller_name = _real_ctrl7
    app.ts_relay_preflight = _real_preflight7

# --- stats: /api/stats per-pod live resources + shared collector ---------
_real_stats_podman = app.podman
_real_stats_deployed = app.deployed_services


def _stats_podman(*a, **kw):
    if a and a[0] == "stats":
        rows = [
            {"name": "nginx", "cpu_percent": "12.5%",
             "mem_usage": "128MiB / 512MiB"},
            {"name": "tailscale-nginx", "cpu_percent": "0.4%",
             "mem_usage": "20MiB / 4GiB"},
        ]
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(rows),
                                     stderr="")
    if a and a[0] == "ps":
        rows = [{"Names": ["nginx"], "State": "running", "ExitCode": 0},
                {"Names": ["tailscale-nginx"], "State": "running",
                 "ExitCode": 0}]
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(rows),
                                     stderr="")
    return types.SimpleNamespace(returncode=1, stdout="", stderr="")


try:
    app.podman = _stats_podman
    app.deployed_services = lambda: ["nginx"]
    code, data = get("/api/stats")
    check(code == 200, "GET /api/stats returns 200")
    check(data["totals"]["pods"] == 1 and data["totals"]["running"] == 1,
          "stats totals count the running pod")
    pod = data["pods"][0]
    check(pod["name"] == "nginx", "stats lists the deployed pod")
    check(abs(pod["cpu_percent"] - 12.9) < 1e-6,
          "pod CPU sums app + sidecar (12.5 + 0.4)")
    check(pod["mem_bytes"] == (128 + 20) * (1 << 20),
          "pod memory sums app + sidecar (MiB parsed to bytes)")
    check(pod["mem_limit_bytes"] == 512 * (1 << 20),
          "pod memory limit comes from the app container's cgroup limit")
    # /metrics must reuse the same collector — identical numbers, no drift.
    with urllib.request.urlopen(BASE + "/metrics") as r:
        metrics = r.read().decode()
    check('tailarr_container_cpu_percent{container="nginx"} 12.5' in metrics,
          "/metrics emits per-container CPU from the shared collect_stats()")
    check('tailarr_container_mem_bytes{container="nginx"} %d'
          % (128 * (1 << 20)) in metrics,
          "/metrics emits per-container memory from the shared collector")
finally:
    app.podman = _real_stats_podman
    app.deployed_services = _real_stats_deployed

# --- /api/fs: host folder browser (one-shot /:/host-root container) -------
print("fs browser:")
import subprocess as _sp_fs  # noqa: E402

_fs_calls = []


def _fake_fs_podman(tree):
    """tree: dir path -> list of child dirs (the fake host filesystem)."""
    def fake(*args, **kw):
        _fs_calls.append(args)
        if args[0] == "ps":
            return _sp_fs.CompletedProcess(
                args, 0, "tailarr\ntailscale-tailarr\n", "")
        if args[0] == "inspect":
            return _sp_fs.CompletedProcess(
                args, 0, "ghcr.io/scs32/tailarr:v0.1.0\n", "")
        if args[0] == "run":
            script = args[-1]
            if script.startswith("mkdir -p "):
                return _sp_fs.CompletedProcess(args, 0, "", "")
            # the list script cd's into a (shlex-quoted) /host-root path
            path = script.split("\n")[0].split()[1].strip("'")
            hostpath = path[len("/host-root"):] or "/"
            if hostpath not in tree:
                return _sp_fs.CompletedProcess(args, 3, "", "TAILARR-FS-NODIR\n")
            return _sp_fs.CompletedProcess(
                args, 0, "".join(d + "\n" for d in tree[hostpath]), "")
        return _sp_fs.CompletedProcess(args, 0, "", "")
    return fake


_real_fs_podman = app.podman
app.podman = _fake_fs_podman({
    "/": ["data", "root"],
    "/data": ["movies", "tv"],
    "/data/movies": [],
})
try:
    code, data = post("/api/fs", {"do": "list", "path": "/"})
    check(code == 200 and data["ok"] and data["dirs"] == ["data", "root"],
          "list / returns its child dirs")
    check(data["parent"] is None, "/ has no parent")
    code, data = post("/api/fs", {"do": "list", "path": "/data"})
    check(code == 200 and data["dirs"] == ["movies", "tv"]
          and data["parent"] == "/",
          "list /data returns children + parent")
    code, data = post("/api/fs", {"do": "list", "path": "/data/movies/"})
    check(code == 200 and data["ok"] and data["path"] == "/data/movies"
          and data["dirs"] == [] and data["parent"] == "/data",
          "trailing slash normalized; empty dir lists cleanly")
    check(any(a[0] == "run" and "/:/host-root:ro" in a for a in _fs_calls),
          "listing mounts the host root read-only")
    code, data = post("/api/fs", {"do": "list", "path": "/nope"})
    check(code == 400 and not data["ok"] and "not found" in data["error"],
          "missing folder reports 'not found', not a raw shell error")
    code, data = post("/api/fs", {"do": "list", "path": "relative"})
    check(code == 400 and "absolute" in data["error"],
          "relative paths rejected")
    code, data = post("/api/fs", {"do": "list", "path": "/data/../../etc"})
    check(code == 400 and data["path"] == "/etc"
          and "not found" in data["error"],
          "'..' segments normalize to a plain path (no /host-root escape)")
    code, data = post("/api/fs", {"do": "mkdir", "path": "/data/books"})
    check(code == 200 and data["ok"] and data["path"] == "/data/books",
          "mkdir creates a folder")
    check(any(a[0] == "run" and "/:/host-root" in a
              and "/:/host-root:ro" not in a for a in _fs_calls),
          "mkdir mounts the host root read-write")
    code, data = post("/api/fs", {"do": "mkdir", "path": "/"})
    check(code == 400, "mkdir / rejected")
    code, data = post("/api/fs", {"do": "chmod", "path": "/data"})
    check(code == 400 and "unknown do" in data["error"],
          "unknown do rejected")
finally:
    app.podman = _real_fs_podman

# no podman at all (dev/CI runs): a clean error, not a stack trace
code, data = post("/api/fs", {"do": "list", "path": "/"})
check(code == 400 and not data["ok"] and data["error"],
      "podman-less environment reports a clean error")

# --- ntfy system pod: hidden from sharing, setup, ops notifications -------
# Install ntfy like any pod (the engine only renders scripts offline).
code, data = post("/api/install", {
    "custom": True, "service": "ntfy",
    "image": "docker.io/binwiederhier/ntfy:v2.26.3",
    "command": "serve",
    "ports": {"80": "80"},
    "volumes": {"/etc/ntfy": f"{pods}/ntfy/etc/ntfy",
                "/var/cache/ntfy": f"{pods}/ntfy/cache"},
    "authkey": "dummy-test-authkey-ntfy",
})
check(code == 200 and data["ok"], "ntfy installs like any pod")
check(app._is_system("ntfy") and not app._is_system("apitest"),
      "_is_system matches the ntfy image only")

code, data = get("/api/pods")
byname = {p["name"]: p for p in data["pods"]}
check(byname["ntfy"]["system"] is True
      and byname["apitest"]["system"] is False,
      "/api/pods flags the system pod")
check("ntfy" not in app._shareable_services()
      and "apitest" in app._shareable_services(),
      "system pod excluded from shareable services")
code, data = get("/api/users")
check("ntfy" not in data["services"] and "server" in data["services"],
      "Users page never offers the system pod")

secs = app._managed_sections()
grants_text = "\n".join(secs["grants"])
owners_text = "\n".join(secs["tagowners"])
check("tag:tailarr-svc-ntfy" in owners_text,
      "system pod keeps its svc- identity tagOwner (tag write must work)")
check("can-ntfy" not in owners_text and "ntfy" not in grants_text,
      "system pod gets no can- badge and NO grant lines (netmap-invisible)")
check(app._grants_minimality_ok(secs["grants"])
      and app._sections_prefix_ok(secs),
      "system-pod sections still pass both policy invariants")

code, data = get("/api/ntfy")
check(code == 200 and data["installed"] and data["configured"] is False,
      "GET /api/ntfy sees the deployed pod, unconfigured")
code, data = post("/api/ntfy/test", {})
check(code == 400 and "not configured" in data["error"],
      "test publish before setup reports unconfigured")

# Setup against a fake podman: the server.yml is pre-written so the
# restart step is skipped (run.sh would need real podman); provisioning
# drives the recorded ntfy CLI.
conf_dir = os.path.join(pods, "ntfy", "etc", "ntfy")
os.makedirs(conf_dir, exist_ok=True)
with open(os.path.join(conf_dir, "server.yml"), "w") as f:
    f.write(app._ntfy_server_yml(""))


class FakeNtfyPodman:
    """Simulates podman exec of the ntfy CLI; records everything."""

    def __init__(self):
        self.calls = []
        self.users = set()

    def __call__(self, *args, timeout=60):
        a = list(args)
        self.calls.append(a)
        if a[0] == "ps":
            return _sp.CompletedProcess(a, 0, "", "")
        if a[0] == "exec" and "tailscale" in a:
            return _sp.CompletedProcess(a, 1, "", "no sidecar")
        if a[0] == "exec" and "ntfy" in a:
            if "list" in a:
                out = "\n".join(f"user {u} (role: x)" for u in self.users)
                return _sp.CompletedProcess(a, 0, "", out)
            if "add" in a and "user" in a:
                self.users.add(a[-1])
                return _sp.CompletedProcess(a, 0, "", "")
            if "token" in a:
                return _sp.CompletedProcess(
                    a, 0, f"token tk_fake{len(self.calls)} created", "")
            return _sp.CompletedProcess(a, 0, "", "")
        return _sp.CompletedProcess(a, 0, "", "")


_real_ntfy_podman = app.podman
nfake = FakeNtfyPodman()
app.podman = nfake
try:
    code, data = post("/api/ntfy/setup", {})
    check(code == 200 and data["ok"],
          "ntfy setup converges against the CLI (no restart needed)")
    check(app.pod_config("ntfy").get("funnel") == "yes"
          and data["status"]["funnel_on"] is True,
          "setup itself opens the public endpoint (funnel is part of the "
          "feature, not a Network-page step)")
    conf = app.ntfy_client.load_conf()
    check(conf is not None
          and conf["publisher"]["token"].startswith("tk_")
          and conf["admin"]["user"] == "tailarr", "registry saved with tokens")
    mode = os.stat(os.path.join(pods, ".ntfy.json")).st_mode & 0o777
    check(mode == 0o600, ".ntfy.json is private (0600)")
    check({"tailarr", "tailarr-pub"} <= nfake.users,
          "both controller accounts created")
    check(any("access" in c and "tlr-*" in c and "write" in c
              for c in nfake.calls),
          "publisher granted write on tlr-* only")
    nfake.calls = []
    code, data = post("/api/ntfy/setup", {})
    check(code == 200 and data["ok"]
          and not any("token" in c for c in nfake.calls),
          "re-running setup keeps existing tokens (idempotent)")
    code, data = post("/api/ntfy/funnel", {"enabled": False})
    check(code == 200 and data["ok"]
          and app.pod_config("ntfy").get("funnel") == "no"
          and data["status"]["funnel_on"] is False,
          "Notifications page owns the funnel toggle (off)")
    code, data = post("/api/ntfy/funnel", {"enabled": True})
    check(code == 200 and data["ok"]
          and app.pod_config("ntfy").get("funnel") == "yes",
          "Notifications page owns the funnel toggle (back on)")

    # "Alerts on your phone": issue / re-show / revoke
    code, data = post("/api/ntfy/alerts", {"do": "issue"})
    check(code == 200 and data["ok"] and data["token"].startswith("tk_")
          and data["topics"] == ["tlr-ops"]
          and data["status"]["alerts_issued"] is True,
          "alerts issue mints a read credential and flags status")
    check(data["user"] == "tailarr-alerts" and len(data["password"]) >= 20,
          "issue returns user+password too (iOS ntfy app lacks token auth)")
    check("tailarr-alerts" in nfake.users
          and any("access" in c and "tailarr-alerts" in c and "read" in c
                  for c in nfake.calls),
          "alerts account created with read-only access")
    tok1 = data["token"]
    nfake.calls = []
    code, data = post("/api/ntfy/alerts", {"do": "issue"})
    check(code == 200 and data["token"] == tok1
          and not any("token" in c for c in nfake.calls),
          "re-issue re-shows the SAME token (idempotent)")
    code, data = post("/api/ntfy/alerts", {"do": "revoke"})
    check(code == 200 and data["ok"]
          and data["status"]["alerts_issued"] is False
          and any("del" in c and "tailarr-alerts" in c for c in nfake.calls)
          and "alerts" not in (app.ntfy_client.load_conf() or {}),
          "revoke deletes the ntfy account and clears the registry")
finally:
    app.podman = _real_ntfy_podman

# --- system pods are invisible everywhere but their feature page ----------
code, data = get("/api/catalog")
_cat_names = {c["name"] for c in data["catalog"]}
check("ntfy" not in _cat_names and "sonarr" in _cat_names,
      "system pods never appear in the catalog")
code, data = post("/api/fleet", {"do": "restart"})
_fleet_names = ({r["name"] for r in data["results"]}
                | {s["name"] for s in data["skipped"]})
check("ntfy" not in _fleet_names and "apitest" in _fleet_names,
      "fleet start/stop/restart leaves system pods alone")

# With no ntfy pod deployed, setup INSTALLS it from the hidden entry.
_inst_calls = []
_real_discover = app._discover_ntfy
_real_op_install = app.op_install
app._discover_ntfy = lambda fresh=False: None
app.op_install = (lambda req: (_inst_calls.append(req)
                               or {"ok": False, "error": "no auth key",
                                   "output": ""}))
try:
    code, data = post("/api/ntfy/setup", {})
    check(code == 400 and data["error"] == "no auth key"
          and _inst_calls and _inst_calls[0]["name"] == "ntfy"
          and _inst_calls[0]["custom"] is False
          and "binwiederhier/ntfy" in _inst_calls[0]["image"],
          "setup auto-installs ntfy from the hidden catalog entry")
finally:
    app._discover_ntfy = _real_discover
    app.op_install = _real_op_install

# --- ops notifications: transition-edge de-dup + health debounce ----------
_notes = []
_real_notify_ops = app.notify_ops
_real_ius = app._image_update_status
_real_check_release = app._check_release
app.notify_ops = (lambda title, message, priority="default", tags=None:
                  _notes.append(title))
app._image_update_status = lambda img: {"update": True, "error": None}
app._check_release = lambda: None
try:
    app._check_updates()
    first = len(_notes)
    app._check_updates()
    check(first == 1 and len(_notes) == first,
          "update-available notifies once (False->True edge only)")

    _notes.clear()
    app.ntfy_client.save_state({"pods": {"apitest": "running"},
                                "pending": {}, "identity": {}})
    app._notify_health_pass()  # apitest reads stopped: pass 1 = debounce
    check(not any("apitest" in n for n in _notes),
          "first bad sighting does not page (debounce)")
    app._notify_health_pass()  # pass 2: alert fires
    check(any(n == "apitest is stopped" for n in _notes),
          "second consecutive bad sighting alerts")
    _notes.clear()
    app._notify_health_pass()  # state now stopped: no re-alert
    check(not _notes, "steady bad state never re-pages")
    _real_pod_state = app.pod_state
    app.pod_state = lambda name, ps: "running"
    try:
        app._notify_health_pass()
    finally:
        app.pod_state = _real_pod_state
    check(any("recovered" in n for n in _notes), "recovery notifies once")
finally:
    app.notify_ops = _real_notify_ops
    app._image_update_status = _real_ius
    app._check_release = _real_check_release

# --- auto rerender after upgrade: once per outcome, stopped stay stopped --
_rr_calls = []
_real_run_rerender = app._run_rerender
_real_running_names = app.running_names
_real_notify_ops2 = app.notify_ops
app._run_rerender = (lambda name, start=True:
                     (_rr_calls.append((name, start))
                      or {"ok": True, "name": name, "action": "rerender",
                          "status": "ok", "error": None, "output": ""}))
app.running_names = lambda: {"apitest"}  # only apitest "running"
app.notify_ops = lambda *a, **k: None
try:
    os.makedirs(app.UPGRADE_DIR, exist_ok=True)
    with open(os.path.join(app.UPGRADE_DIR, "result.json"), "w") as f:
        json.dump({"ok": True, "rolled_back": False,
                   "from": "x:v1", "to": "x:v2",
                   "finished": "2026-07-22T20:00:00Z"}, f)
    app._auto_rerender_after_upgrade()
    names = {n for n, _ in _rr_calls}
    check("apitest" in names and "ntfy" in names
          and not names & app.CONTROLLER_PODS,
          "auto rerender covers every non-controller pod")
    check(dict(_rr_calls)["apitest"] is True
          and dict(_rr_calls)["ntfy"] is False,
          "running pods restart; stopped pods rendered but left stopped")
    _rr_calls.clear()
    app._auto_rerender_after_upgrade()
    check(not _rr_calls, "marker makes the pass one-shot per upgrade")
    with open(os.path.join(app.UPGRADE_DIR, "result.json"), "w") as f:
        json.dump({"ok": False, "rolled_back": True,
                   "from": "x:v1", "to": "x:v2",
                   "finished": "2026-07-22T21:00:00Z"}, f)
    app._auto_rerender_after_upgrade()
    check(not _rr_calls, "a rolled-back upgrade triggers no rerender")
finally:
    app._run_rerender = _real_run_rerender
    app.running_names = _real_running_names
    app.notify_ops = _real_notify_ops2

# start=False renders without starting (the real function)
_started = []
_real_run_action2 = app._run_action
app._run_action = (lambda name, action:
                   (_started.append((name, action))
                    or {"ok": True, "name": name, "action": action,
                        "status": "ok", "error": None, "output": ""}))
try:
    r = app._run_rerender("apitest", start=False)
    check(r["ok"] and "left stopped" in r["status"] and not _started,
          "_run_rerender(start=False) renders but never calls start")
finally:
    app._run_action = _real_run_action2

# --- people: first-class users (identity-carrying keys) -------------------
_real_ppl_tok = app._ts_token
_real_ppl_api = app.ts_api
_real_ppl_sync = app.ts_policy_sync
_keys_minted = []
_fake_devices = {"devices": []}


def _fake_people_api(method, path, body=None):
    if method == "POST" and path == "/tailnet/-/keys":
        _keys_minted.append(body)
        return 200, {"key": "dummy-test-authkey-person"}
    if method == "GET" and path == "/tailnet/-/devices":
        return 200, _fake_devices
    if method == "GET" and path.startswith("/device/"):
        nid = path.split("/")[2]
        for d in _fake_devices["devices"]:
            if d["nodeId"] == nid:
                return 200, d
        return 404, "no such device"
    if method == "POST" and path.endswith("/tags"):
        nid = path.split("/")[2]
        for d in _fake_devices["devices"]:
            if d["nodeId"] == nid:
                d["tags"] = body["tags"]
                return 200, {}
        return 404, "no such device"
    return 200, {}


app._ts_token = lambda: "dummy-test-token"
app.ts_api = _fake_people_api
app.ts_policy_sync = lambda: {"ok": True, "changed": False, "error": None}
# The fire-and-forget badge->topic mirror would race the recorders below
# (its thread resolves app.podman at call time); the mirror behavior is
# tested synchronously through /api/people/<uid>/notifications instead.
_real_sync_bg = app._ntfy_person_sync_bg
app._ntfy_person_sync_bg = lambda uid: None
try:
    code, data = post("/api/people", {"do": "add", "name": "Dave"})
    check(code == 200 and data["ok"]
          and data["key"] == "dummy-test-authkey-person", "add user mints a key")
    uid = data["id"]
    tags = _keys_minted[-1]["capabilities"]["devices"]["create"]["tags"]
    check(sorted(tags) == sorted(["tag:tailarr-user", f"tag:tailarr-u-{uid}"]),
          "fresh user's key carries identity tags and no badges")
    secs = app._managed_sections()
    check(any(f"tag:tailarr-u-{uid}" in ln for ln in secs["tagowners"]),
          "person tag enters the fenced tagOwners")
    check(app._grants_minimality_ok(secs["grants"])
          and app._sections_prefix_ok(secs),
          "person tags keep both policy invariants green")
    check(not app._grants_minimality_ok(
        [f'{{"src": ["tag:tailarr-u-{uid}"], "dst": ["tag:tailarr"], '
         '"ip": ["*"]}},']),
        "a grant referencing a person tag is rejected (identity-only)")

    # a device enrolls with the key -> owned; badge flip fans out
    _fake_devices["devices"].append(
        {"nodeId": "pdev1", "hostname": "daves-phone", "os": "iOS",
         "tags": ["tag:tailarr-user", f"tag:tailarr-u-{uid}"]})
    code, data = post(f"/api/people/{uid}/access",
                      {"service": "apitest", "allow": True})
    check(code == 200 and data["ok"]
          and "tag:tailarr-can-apitest" in _fake_devices["devices"][0]["tags"],
          "per-user badge flip reaches every owned device")
    code, data = post("/api/people", {"do": "reissue", "id": uid})
    tags = _keys_minted[-1]["capabilities"]["devices"]["create"]["tags"]
    check(code == 200 and "tag:tailarr-can-apitest" in tags,
          "reissued key carries the user's current badges")

    # grouping: owned devices sit under the person, not in unassigned
    _fake_devices["devices"].append(
        {"nodeId": "udev2", "hostname": "orphan", "os": "tvOS",
         "tags": ["tag:tailarr-user"]})
    code, data = get("/api/users")
    person = next(p for p in data["people"] if p["id"] == uid)
    check([d["id"] for d in person["devices"]] == ["pdev1"]
          and [u["id"] for u in data["users"]] == ["udev2"],
          "devices group under their person; anonymous ones stay unassigned")

    code, data = post("/api/people", {"do": "assign", "id": uid,
                                      "node": "udev2"})
    d2 = _fake_devices["devices"][1]
    check(code == 200 and f"tag:tailarr-u-{uid}" in d2["tags"]
          and "tag:tailarr-can-apitest" in d2["tags"],
          "assigning a machine adds the identity tag and the user's badges")

    # reconcile self-heals a device that drifted from its person's badges
    d2["tags"] = ["tag:tailarr-user", f"tag:tailarr-u-{uid}"]
    app.ts_reconcile_people()
    check("tag:tailarr-can-apitest" in d2["tags"],
          "reconcile re-applies the person's badges to drifted devices")

    # --- users release 2: notifications mirror onto the person ---
    app.ntfy_client.save_conf({
        "version": 1, "pod": "ntfy",
        "public_url": "https://ntfy.test.ts.net",
        "admin": {"user": "tailarr", "password": "x", "token": "tk_a"},
        "publisher": {"user": "tailarr-pub", "token": "tk_p"},
        "topics": {}, "users": {}, "arr": {}})
    _real_ppl_podman = app.podman
    n2 = FakeNtfyPodman()
    app.podman = n2
    code, data = post(f"/api/people/{uid}/notifications", {})
    check(code == 200 and data["ok"] and data["user"] == f"u-{uid}"
          and data["token"].startswith("tk_") and len(data["password"]) >= 20
          and data["topics"] == ["tlr-media-apitest"],
          "person handout mints an account with badge-mirrored topics")
    check(any("access" in c and f"u-{uid}" in c
              and "tlr-media-apitest" in c and "read" in c
              for c in n2.calls),
          "read grant issued for the badge's media topic")
    _ptok = data["token"]
    n2.calls = []
    code, data = post(f"/api/people/{uid}/notifications", {})
    check(code == 200 and data["token"] == _ptok
          and not any("token" in c for c in n2.calls),
          "person handout is idempotent (same token, no churn)")
    # the server badge (admin-ish) additionally opens the ops topic
    _ppl = app.load_people()
    _ppl[uid]["badges"] = ["apitest", "server"]
    app.save_people(_ppl)
    code, data = post(f"/api/people/{uid}/notifications", {})
    check(code == 200
          and data["topics"] == ["tlr-media-apitest", "tlr-ops"],
          "server badge mirrors into ops-topic read access")

    code, data = post("/api/people", {"do": "delete", "id": uid})
    check(code == 200 and data["ok"] and uid not in app.load_people()
          and not any(t.startswith("tag:tailarr-u-")
                      or t.startswith("tag:tailarr-can-")
                      for d in _fake_devices["devices"] for t in d["tags"]),
          "delete strips identity + badges from every owned device")
    check(uid not in ((app.ntfy_client.load_conf() or {}).get("users") or {})
          and any("del" in c and f"u-{uid}" in c for c in n2.calls),
          "delete drops the person's ntfy account too")
    app.podman = _real_ppl_podman
finally:
    app._ts_token = _real_ppl_tok
    app.ts_api = _real_ppl_api
    app.ts_policy_sync = _real_ppl_sync
    app._ntfy_person_sync_bg = _real_sync_bg

# --- self-config gateway: the one deliberate visibility exception ---------
# Simulate the deployed gateway pod (controller image, matched by name).
_gate_dir = os.path.join(pods, "tailarr-gate")
os.makedirs(_gate_dir, exist_ok=True)
with open(os.path.join(_gate_dir, ".config.json"), "w") as f:
    json.dump({"image": "ghcr.io/scs32/tailarr:v0.0.0",
               "ports": {"80": "80"}}, f)
with open(os.path.join(_gate_dir, "run.sh"), "w") as f:
    f.write("#!/bin/sh\nexit 0\n")

check(app._is_system("tailarr-gate") and not app._is_system("apitest"),
      "the gateway is a system pod by name (controller image stays clean)")
secs = app._managed_sections()
gate_lines = [ln for ln in secs["grants"] if "tailarr-gate" in ln]
check(len(gate_lines) == 1 and '"tag:tailarr-user"' in gate_lines[0]
      and '"80"' in gate_lines[0],
      "deployed gateway emits exactly one user->gateway grant")
check(app._grants_minimality_ok(secs["grants"])
      and app._sections_prefix_ok(secs),
      "the gateway grant passes the minimality carve-out")
for bad, why in [
    ('{"src": ["tag:tailarr-user"], "dst": ["tag:tailarr-svc-tailarr-gate"], '
     '"ip": ["443"]},', "wrong port"),
    ('{"src": ["tag:tailarr-user"], "dst": ["tag:tailarr-svc-sonarr"], '
     '"ip": ["80"]},', "wrong destination"),
    ('{"src": ["tag:tailarr-user", "tag:tailarr"], '
     '"dst": ["tag:tailarr-svc-tailarr-gate"], "ip": ["80"]},',
     "bundled src"),
]:
    check(not app._grants_minimality_ok([bad]),
          f"carve-out is exact: {why} rejected")

# resolve: whois against the gateway's sidecar -> the person's handout
app._ts_token = lambda: "dummy-test-token"
app.ts_api = _fake_people_api
app.ts_policy_sync = lambda: {"ok": True, "changed": False, "error": None}
app._ntfy_person_sync_bg = lambda uid: None
code, data = post("/api/people", {"do": "add", "name": "Eve"})
_gate_uid = data["id"]
_ppl = app.load_people()
_ppl[_gate_uid]["badges"] = ["apitest"]
app.save_people(_ppl)


class GateFake(FakeNtfyPodman):
    def __call__(self, *args, timeout=60):
        a = list(args)
        if a[0] == "exec" and "whois" in a:
            self.calls.append(a)
            return _sp.CompletedProcess(a, 0, json.dumps(
                {"Node": {"Tags": ["tag:tailarr-user",
                                   f"tag:tailarr-u-{_gate_uid}"]}}), "")
        return super().__call__(*args, timeout=timeout)


gfake = GateFake()
_real_gate_podman = app.podman
app.podman = gfake
try:
    app._write_secret(app.GATEWAY_FILE,
                      json.dumps({"secret": "dummy-test-gwsecret"}))
    code, data = post("/api/gateway/resolve",
                      {"ip": "100.64.0.9", "secret": "wrong"})
    check(code == 400 and "secret" in data["error"],
          "resolve refuses a bad gateway secret")
    code, data = post("/api/gateway/resolve",
                      {"ip": "100.64.0.9", "secret": "dummy-test-gwsecret"})
    check(code == 200 and data["ok"] and data["user"] == f"u-{_gate_uid}"
          and data["topics"] == ["tlr-media-apitest"]
          and data["token"].startswith("tk_"),
          "resolve whoises the caller and returns THEIR handout")
    check(any("whois" in c and f"tailscale-{app.GATEWAY_POD}" in c
              for c in gfake.calls),
          "whois runs against the gateway's sidecar (its peers, not ours)")
    plain = FakeNtfyPodman()  # its exec/tailscale branch fails -> rc 1
    app.podman = plain
    code, data = post("/api/gateway/resolve",
                      {"ip": "100.64.0.9", "secret": "dummy-test-gwsecret"})
    check(code == 400 and "whois failed" in data["error"],
          "unresolvable caller is a clean refusal")
finally:
    app.podman = _real_gate_podman
    app._ts_token = _real_ppl_tok
    app.ts_api = _real_ppl_api
    app.ts_policy_sync = _real_ppl_sync
    app._ntfy_person_sync_bg = _real_sync_bg

# --- media wiring: automated Arr ntfy Connect (phase 2) -------------------
_arr_dir = os.path.join(pods, "sonarr")
os.makedirs(_arr_dir, exist_ok=True)
with open(os.path.join(_arr_dir, ".config.json"), "w") as f:
    json.dump({"image": "linuxserver/sonarr:latest",
               "ports": {"8989": "8989"}}, f)
with open(os.path.join(_arr_dir, "run.sh"), "w") as f:
    f.write("#!/bin/sh\nexit 0\n")

_arr_calls = []
_arr_notifications = []
_ARR_SCHEMA = [{"implementation": "Ntfy", "configContract": "NtfySettings",
                "fields": [{"name": "serverUrl"}, {"name": "accessToken"},
                           {"name": "userName"}, {"name": "password"},
                           {"name": "topics"},
                           {"name": "priority", "value": 4}]}]


def _fake_arr_req(base, key, method, path, body=None):
    _arr_calls.append((method, path, body))
    if method == "GET" and path == "/notification/schema":
        return 200, _ARR_SCHEMA
    if method == "GET" and path == "/notification":
        return 200, list(_arr_notifications)
    if method == "POST" and path == "/notification":
        _arr_notifications.append({"name": body["name"], "id": 7})
        return 201, {}
    if method == "PUT" and path == "/notification/7":
        return 202, {}
    return 404, None


_real_arr_req = app._arr_req
_real_arr_key = app._arr_api_key
_real_net_entry = app.network_entry
app._arr_req = _fake_arr_req
app._arr_api_key = lambda pod: "arr-key-123"
app.network_entry = (lambda name, ps:
                     {"name": name, "ip": "100.64.0.5",
                      "ports": {"8989": "8989"}, "state": "running",
                      "dns_name": "", "https": False, "controller": False,
                      "system": False, "tailscale": True, "funnel": False,
                      "network_mode": "bridge", "busy": None})
try:
    code, data = post("/api/ntfy/wire/sonarr", {})
    check(code == 200 and data["ok"] and data["topic"] == "tlr-media-sonarr",
          "wire configures the Arr's native ntfy connection")
    _method, _path, body = next(c for c in _arr_calls if c[0] == "POST")
    fields = {f["name"]: f.get("value") for f in body["fields"]}
    check(body["name"] == "Tailarr ntfy"
          and fields["topics"] == ["tlr-media-sonarr"]
          and fields["accessToken"] == (app.ntfy_client.load_conf()
                                        ["publisher"]["token"])
          and body["onDownload"] is True and body["onGrab"] is False,
          "connection fields map from the Arr's own schema")
    _arr_status_rows = app.status_ntfy()["arr"]
    check(any(r["name"] == "sonarr" and r["wired"] == "auto"
              for r in _arr_status_rows),
          "wiring state recorded and surfaced")
    _arr_calls.clear()
    code, data = post("/api/ntfy/wire/sonarr", {})
    check(code == 200 and any(c[0] == "PUT" for c in _arr_calls)
          and not any(c[0] == "POST" for c in _arr_calls),
          "re-wiring updates the existing connection in place")
    app._arr_req = (lambda base, key, method, path, body=None:
                    (200, []))
    code, data = post("/api/ntfy/wire/sonarr", {})
    check(code == 400 and data["recipe"]["topic"] == "tlr-media-sonarr",
          "schema surprises degrade to the manual recipe")
    code, data = post("/api/ntfy/wire/apitest", {})
    check(code == 400 and "supported" in data["error"],
          "non-Arr pods are refused")
finally:
    app._arr_req = _real_arr_req
    app._arr_api_key = _real_arr_key
    app.network_entry = _real_net_entry

# --- gateway deploy on a bootstrap-created controller (no .config.json) ---
# Live-caught (app session, 2026-07-22): network_entry gates its sidecar
# query on .config.json, which bootstrap controllers don't have —
# _controller_ip must ask the sidecar directly.


class CtrlIpFake:
    def __call__(self, *args, timeout=60):
        a = list(args)
        if a[0] == "ps":
            return _sp.CompletedProcess(
                a, 0, "tailarr\ntailscale-tailarr\n", "")
        if a[0] == "exec" and "status" in a:
            return _sp.CompletedProcess(a, 0, json.dumps(
                {"Self": {"TailscaleIPs": ["100.64.0.7", "fd7a::7"]}}), "")
        return _sp.CompletedProcess(a, 1, "", "nope")


_real_ip_podman = app.podman
app.podman = CtrlIpFake()
try:
    check(not os.path.exists(os.path.join(pods, "tailarr", ".config.json")),
          "precondition: the controller pod has no .config.json")
    check(app._controller_ip() == "100.64.0.7",
          "controller IP comes straight from its sidecar (config-less OK)")
finally:
    app.podman = _real_ip_podman

# The gateway is name-matched, not image-matched: /api/pods and
# /api/network must flag it system so the Dashboard/Network hide it
# (regression: they used SYSTEM_IMAGES image-substring only, which
# missed the gateway — it leaked onto the Dashboard, caught 2026-07-22).
code, data = get("/api/pods")
_bn = {p["name"]: p for p in data["pods"]}
check(_bn["tailarr-gate"]["system"] is True
      and _bn["apitest"]["system"] is False,
      "/api/pods flags the name-matched gateway as system")
check(app._display_name("ntfy") == "Notifications"
      and app._display_name("tailarr-gate") == "Tailarr app setup"
      and app._display_name("apitest") == "apitest",
      "system pods get function-first display names (Stats)")

# --- services handout: /self/services via the gateway (v0.23.0) ----------
# The same whois'd person asks for their services instead of their
# notification config: badged services come back ready for the app's
# modules — native kinds (sonarr/radarr/lidarr) with the Arr's own API
# key, everything else as an external (URL-only) bookmark entry.
_ppl = app.load_people()
_ppl[_gate_uid]["badges"] = ["apitest", "sonarr", "server", "ghostsvc"]
app.save_people(_ppl)
_real_svc_key = app._arr_api_key
_real_svc_net = app.network_entry
_real_svc_dns = app._controller_dns
_real_svc_podman = app.podman
app._arr_api_key = lambda pod: "arr-key-123" if pod == "sonarr" else None
app.network_entry = (lambda name, ps: {
    "name": name, "ip": "100.64.0.5", "ports": {"80": "80"},
    "state": "running", "dns_name": f"{name}.test.ts.net", "https": True,
    "controller": False, "system": False, "tailscale": True,
    "funnel": False, "network_mode": "bridge", "busy": None})
app._controller_dns = lambda: "tailarr.test.ts.net"
app.podman = gfake  # whois resolves the caller to the person
try:
    code, data = post("/api/gateway/resolve",
                      {"ip": "100.64.0.9", "secret": "dummy-test-gwsecret",
                       "want": "services"})
    rows = {s["name"]: s for s in data.get("services") or []}
    check(code == 200 and data["ok"] and data["kind"] == "services"
          and rows["sonarr"]["type"] == "sonarr"
          and rows["sonarr"]["auth"] == {"api_key": "arr-key-123"}
          and rows["sonarr"]["url"] == "https://sonarr.test.ts.net",
          "services handout: native module entry carries the Arr's key")
    check(rows["apitest"]["type"] == "external"
          and rows["apitest"]["auth"] is None
          and rows["apitest"]["url"] == "https://apitest.test.ts.net",
          "non-module badges appear as external (URL-only) entries")
    check(rows["server"]["type"] == "tailarr"
          and rows["server"]["url"] == "https://tailarr.test.ts.net"
          and rows["server"]["auth"] is None,
          "the server badge configures the app's own server module")
    check("ghostsvc" not in rows, "stale badges are skipped, not errors")
    code, data = post("/api/gateway/resolve",
                      {"ip": "100.64.0.9", "secret": "dummy-test-gwsecret",
                       "want": "bogus"})
    check(code == 400 and not data["ok"], "unknown want is refused")
    code, data = post("/api/gateway/resolve",
                      {"ip": "100.64.0.9", "secret": "dummy-test-gwsecret"})
    check(code == 200 and "token" in data and "services" not in data,
          "want defaults to notifications (pre-0.23.0 gateway compat)")
finally:
    app._arr_api_key = _real_svc_key
    app.network_entry = _real_svc_net
    app._controller_dns = _real_svc_dns
    app.podman = _real_svc_podman

# --- services handout release 2: the non-Arr credential extractors --------
# nzbget (control user+password), sabnzbd + Tautulli (ini api_key),
# overseerr/jellyseerr (settings.json main.apiKey; jellyseerr hands out
# as type "overseerr" — its API is Overseerr-compatible — and keeps its
# config under /app/config, exercising the path fallback).
for _n, _img in [("nzbget", "linuxserver/nzbget:latest"),
                 ("sabnzbd", "linuxserver/sabnzbd:latest"),
                 ("tautulli", "linuxserver/tautulli:latest"),
                 ("overseerr", "linuxserver/overseerr:latest"),
                 ("jellyseerr", "fallenbagel/jellyseerr:latest")]:
    _d = os.path.join(pods, _n)
    os.makedirs(_d, exist_ok=True)
    with open(os.path.join(_d, ".config.json"), "w") as f:
        json.dump({"image": _img, "ports": {"80": "80"}}, f)
    with open(os.path.join(_d, "run.sh"), "w") as f:
        f.write("#!/bin/sh\nexit 0\n")

_CONF_FILES = {
    ("nzbget", "/config/nzbget.conf"):
        "MainDir=/config\nControlUsername=nzbget\n"
        "ControlPassword=dummy-test-pw\n",
    ("sabnzbd", "/config/sabnzbd.ini"): "[misc]\napi_key = sab-key-1\n",
    ("tautulli", "/config/config.ini"): "[General]\napi_key = taut-key-1\n",
    ("overseerr", "/config/settings.json"):
        json.dumps({"main": {"apiKey": "ovr-key-1"}}),
    ("jellyseerr", "/app/config/settings.json"):
        json.dumps({"main": {"apiKey": "jelly-key-1"}}),
}


class ConfFake:
    def __call__(self, *args, timeout=60):
        a = list(args)
        if a[0] == "exec" and len(a) == 4 and a[2] == "cat":
            body = _CONF_FILES.get((a[1], a[3]))
            return _sp.CompletedProcess(a, 0 if body is not None else 1,
                                        body or "", "")
        return _sp.CompletedProcess(a, 1, "", "nope")


_real_conf_podman = app.podman
app.podman = ConfFake()
try:
    check(app._service_kind("nzbget") == "nzbget"
          and app._service_kind("jellyseerr") == "overseerr"
          and app._service_kind("apitest") is None,
          "kind detection: native kinds, jellyseerr->overseerr, else None")
    check(app._service_auth("nzbget", "nzbget")
          == {"user": "nzbget", "password": "dummy-test-pw"},
          "nzbget hands out its control user + password")
    check(app._service_auth("sabnzbd", "sabnzbd")
          == {"api_key": "sab-key-1"}
          and app._service_auth("tautulli", "tautulli")
          == {"api_key": "taut-key-1"},
          "sabnzbd + Tautulli hand out their ini api_key")
    check(app._service_auth("overseerr", "overseerr")
          == {"api_key": "ovr-key-1"}
          and app._service_auth("jellyseerr", "overseerr")
          == {"api_key": "jelly-key-1"},
          "overseerr reads settings.json; jellyseerr via /app/config")
    check(app._service_auth("apitest", "sabnzbd") is None,
          "unreadable config degrades to auth null, not an error")
finally:
    app.podman = _real_conf_podman

# --- gateway converge: upgrades move the pod onto the new image -----------
# The gateway runs the controller's image; after an upgrade a stale copy
# wouldn't know new /self/* routes. Converge must repoint the saved
# config and re-render IN PLACE (never remove+reinstall — that wipes the
# Tailscale identity and invites a hostname collision).
_rr_calls = []
_real_cvg_img = app._controller_image
_real_cvg_rr = app._run_rerender
app._controller_image = lambda: "ghcr.io/scs32/tailarr:v9.9.9"
app._run_rerender = lambda name, start=True: (
    _rr_calls.append(name) or {"ok": True, "name": name,
                               "action": "rerender", "status": "ok",
                               "error": None, "output": ""})
try:
    app._converge_notifications()
    check(_rr_calls == ["tailarr-gate"]
          and app.pod_config("tailarr-gate")["image"]
          == "ghcr.io/scs32/tailarr:v9.9.9",
          "converge repoints + re-renders a stale gateway image")
    _rr_calls.clear()
    app._converge_notifications()
    check(_rr_calls == [],
          "converge is a no-op when the gateway image matches")
finally:
    app._controller_image = _real_cvg_img
    app._run_rerender = _real_cvg_rr

# --- Magic Stacks (v0.25.0): guardrail, validators, seeding, wiring ------
# The pods dir already contains sonarr + nzbget from earlier suites, so
# the greenfield guardrail must trip for usenet-starter here.
code, data = get("/api/stacks")
_st = next(s for s in data["stacks"] if s["key"] == "usenet-starter")
check(code == 200 and not _st["eligible"]
      and "sonarr" in _st["blockers"] and "nzbget" in _st["blockers"]
      and "radarr" not in _st["blockers"],
      "greenfield guardrail: existing kinds block the stack, absent don't")
code, data = post("/api/stacks", {"do": "install",
                                  "stack": "usenet-starter"})
check(code == 400 and "recreate existing services" in data["error"],
      "install refuses when the guardrail is tripped")
code, data = post("/api/stacks", {"do": "nonsense"})
check(code == 400, "unknown stack action is refused")

# Validators, against fakes (no network).
import urllib.request as _urlreq  # noqa: E402


class _CapsResp:
    def __init__(self, body):
        self.body = body.encode()
        self.status = 200

    def read(self, n=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_caps_agents = []


def _fake_urlopen(req, timeout=10):
    # v0.25.1: the probe sends Request objects with a browser-shaped UA
    # (Cloudflare 403s python-urllib's default — live-caught 07-23).
    # caps is served unauthenticated (as on real indexers); only the
    # t=search probe judges the key.
    _caps_agents.append(req.get_header("User-agent", ""))
    url = req.full_url
    if "t=caps" in url:
        return _CapsResp("<caps><server/></caps>")
    return _CapsResp(
        "<rss/>" if "goodkey" in url
        else '<error code="101" description="Invalid API Key"/>')


_real_urlopen = _urlreq.urlopen
_urlreq.urlopen = _fake_urlopen
try:
    check(app._validate_newznab("https://indexer.test", "goodkey") is None,
          "newznab probe passes a good indexer + key")
    check(_caps_agents and all(a.startswith("Mozilla/5.0")
                               and "Tailarr" in a for a in _caps_agents),
          "probes send a browser-shaped UA (Cloudflare 1010 fix)")
    err = app._validate_newznab("https://indexer.test", "badkey")
    check(err is not None and "Invalid API Key" in err,
          "open caps + bad key is caught by the authenticated search")
    check(app._validate_newznab("indexer.test", "goodkey") is None,
          "schemeless indexer URL is forgiven (https assumed)")
    check(app._validate_newznab("", "k") is not None
          and app._validate_newznab("https://nodots", "k") is not None,
          "empty or hostname-less indexer input still refused")
finally:
    _urlreq.urlopen = _real_urlopen

# Paste-shape forgiveness (v0.25.1, live-caught: known-good accounts
# failed validation on raw input shapes).
check(app._indexer_base("api.nzbgeek.info/api?t=caps&apikey=zz")
      == "https://api.nzbgeek.info/api"
      and app._indexer_pasted_key(
          "https://api.nzbgeek.info/api?t=caps&apikey=zz") == "zz",
      "full pasted API URLs reduce to base; embedded apikey recovered")
check(app._usenet_host("ssl://news.eweka.nl:563/") == ("news.eweka.nl", 563)
      and app._usenet_host("news.eweka.nl") == ("news.eweka.nl", None),
      "news-server paste shapes normalize (scheme/port/path stripped)")
_ninp, _nerrs = app._stack_inputs(
    {"media": "/srv/media",
     "indexer": {"url": "https://idx.test/api?apikey=fromurl", "key": ""},
     "usenet": {"host": "nntps://news.test:8563", "port": 563,
                "ssl": True, "user": "u", "password": "p"}})
check(_ninp["indexer"]["key"] == "fromurl"
      and _ninp["indexer"]["url"] == "https://idx.test/api"
      and _ninp["usenet"]["host"] == "news.test"
      and _ninp["usenet"]["port"] == 8563,
      "inputs normalize: URL-borne key honored, embedded port wins")


class _FakeNNTP:
    """Scripted NNTP server: greeting, then AUTHINFO USER/PASS."""

    def __init__(self, good):
        self.good = good
        self.lines = [b"200 news.test ready\r\n"]

    def makefile(self, mode):
        return self

    def readline(self):
        return self.lines.pop(0) if self.lines else b""

    def write(self, b):
        if b.startswith(b"AUTHINFO USER"):
            self.lines.append(b"381 password required\r\n")
        elif b.startswith(b"AUTHINFO PASS"):
            self.lines.append(b"281 welcome\r\n" if self.good
                              else b"481 bad credentials\r\n")

    def flush(self):
        pass

    def close(self):
        pass


_real_conn = app.socket.create_connection
app.socket.create_connection = (lambda addr, timeout=10:
                                _FakeNNTP(addr[0] == "good.news.test"))
try:
    check(app._validate_usenet("good.news.test", 119, False, "u", "p")
          is None, "NNTP validator signs in against a good account")
    err = app._validate_usenet("bad.news.test", 119, False, "u", "p")
    check(err is not None and "Sign-in failed" in err,
          "NNTP validator surfaces a refused sign-in")
finally:
    app.socket.create_connection = _real_conn

# nzbget seeding: seed-once guardrail + the actual write.
_nzconf_dir = os.path.join(pods, "nzbget", "config")
os.makedirs(_nzconf_dir, exist_ok=True)
_nzconf = os.path.join(_nzconf_dir, "nzbget.conf")
with open(_nzconf, "w") as f:
    f.write("MainDir=/config\nServer1.Active=no\nServer1.Host=\n"
            "Server1.Port=119\nServer1.Username=\nServer1.Password=\n")


class _RestartFake:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, timeout=60):
        self.calls.append(list(args))
        return _sp.CompletedProcess(args, 0, "", "")


_rf = _RestartFake()
_real_st_podman = app.podman
app.podman = _rf
try:
    _use = {"host": "news.test", "port": 563, "ssl": True,
            "user": "u1", "password": "pw1"}
    detail = app._stack_seed_nzbget(_use)
    text = open(_nzconf).read()
    check("Server1.Host=news.test" in text
          and "Server1.Password=pw1" in text
          and "Server1.Encryption=yes" in text
          and "Server1.Active=yes" in text
          and any(c[:2] == ["restart", "nzbget"] for c in _rf.calls),
          "usenet account seeds into nzbget.conf and the pod restarts")
    _rf.calls.clear()
    detail = app._stack_seed_nzbget({**_use, "host": "other.news"})
    check("left untouched" in detail and not _rf.calls
          and "Server1.Host=news.test" in open(_nzconf).read(),
          "seed-once guardrail: an occupied Server1 is never overwritten")
finally:
    app.podman = _real_st_podman

# Arr wiring: schema-driven create-or-update with category + baseUrl
# mapping.
_wire_calls = []
_DL_SCHEMA = [{"implementation": "Nzbget",
               "configContract": "NzbgetSettings",
               "fields": [{"name": "host"}, {"name": "port"},
                          {"name": "useSsl"}, {"name": "username"},
                          {"name": "password"}, {"name": "tvCategory"}]}]
_IDX_SCHEMA = [{"implementation": "Newznab",
                "fields": [{"name": "baseUrl"}, {"name": "apiKey"},
                           {"name": "categories", "value": [5030]}]}]


def _fake_wire_req(base, key, method, path, body=None):
    _wire_calls.append((method, path, body))
    if method == "GET" and path == "/downloadclient/schema":
        return 200, _DL_SCHEMA
    if method == "GET" and path == "/indexer/schema":
        return 200, _IDX_SCHEMA
    if method == "GET" and path in ("/downloadclient", "/indexer",
                                    "/rootfolder"):
        return 200, []
    if method == "POST":
        return 201, {}
    return 404, None


_real_wire_req = app._arr_req
_real_wait_arr = app._stack_wait_arr
app._arr_req = _fake_wire_req
app._stack_wait_arr = lambda pod: ("http://100.64.0.5:8989/api/v3",
                                   "arr-key")
app.podman = _RestartFake()  # exec mkdir succeeds
try:
    _inp = {"media": "/srv/media",
            "indexer": {"url": "https://indexer.test/api", "key": "ik"},
            "usenet": _use}
    detail = app._stack_wire_arr("sonarr", _inp, "100.64.0.9",
                                 {"user": "u1", "password": "pw1"})
    dl = next(b for m, p, b in _wire_calls
              if m == "POST" and p == "/downloadclient")
    dlf = {f["name"]: f.get("value") for f in dl["fields"]}
    check(dl["name"] == "Tailarr nzbget" and dlf["host"] == "100.64.0.9"
          and dlf["tvCategory"] == "tv" and dlf["password"] == "pw1",
          "download client wired from schema; category matched by suffix")
    idx = next(b for m, p, b in _wire_calls
               if m == "POST" and p == "/indexer")
    idf = {f["name"]: f.get("value") for f in idx["fields"]}
    check(idx["name"] == "Tailarr indexer"
          and idf["baseUrl"] == "https://indexer.test"
          and idf["apiKey"] == "ik" and idx["enableRss"] is True,
          "indexer wired; /api suffix stripped from baseUrl")
    rfb = next(b for m, p, b in _wire_calls
               if m == "POST" and p == "/rootfolder")
    check(rfb == {"path": "/data/media/tv"},
          "root folder created under the shared /data mount")
finally:
    app._arr_req = _real_wire_req
    app._stack_wait_arr = _real_wait_arr
    app.podman = _real_st_podman

# Worker end-to-end: all primitives faked, saga must land every step.
_real_w_install = app.op_install
_real_w_action = app.op_action
_real_w_seed = app._stack_seed_nzbget
_real_w_wire = app._stack_wire_arr
_real_w_net = app.network_entry
_real_w_auth = app._service_auth
app.op_install = lambda req: {"ok": True, "name": req["name"],
                              "error": None, "output": ""}
app.op_action = lambda name, action: {"ok": True, "status": "ok",
                                      "error": None}
app._stack_seed_nzbget = lambda usenet: "news server configured"
app._stack_wire_arr = lambda pod, i, ip, auth: "wired"
app.network_entry = (lambda name, ps: {"ip": "100.64.0.9", "ports":
                     {"6789": "6789"}, "dns_name": "", "https": False})
app._service_auth = lambda pod, kind: {"user": "u", "password": "p"}
try:
    _spec = app.STACKS["usenet-starter"]
    app._save_stack_run({"stack": "usenet-starter", "state": "running",
                         "started": 1, "finished": None, "error": None,
                         "steps": app._stack_steps_for(_spec)})
    app._stack_worker("usenet-starter", _inp)
    _run = app.load_stack_run()
    check(_run["state"] == "done"
          and all(s["state"] == "ok" for s in _run["steps"]),
          "worker saga completes every step and finishes done")
    app._save_stack_run({"stack": "usenet-starter", "state": "running",
                         "started": 1, "finished": None, "error": None,
                         "steps": app._stack_steps_for(_spec)})
    app._stack_wire_arr = (lambda pod, i, ip, auth:
                           (_ for _ in ()).throw(
                               app._StackAbort("schema probe failed")))
    app._stack_worker("usenet-starter", _inp)
    _run = app.load_stack_run()
    check(_run["state"] == "failed"
          and "schema probe failed" in (_run["error"] or "")
          and any(s["state"] == "failed" for s in _run["steps"]),
          "a failing step stops the run with the step's message")
finally:
    app.op_install = _real_w_install
    app.op_action = _real_w_action
    app._stack_seed_nzbget = _real_w_seed
    app._stack_wire_arr = _real_w_wire
    app.network_entry = _real_w_net
    app._service_auth = _real_w_auth


# --- push wakes (v0.26.0): gateway registration + fan-out + prune --------
import io as _io  # noqa: E402
import urllib.error as _urlerr  # noqa: E402

app.podman = gfake  # whois resolves to the person again
try:
    code, data = post("/api/gateway/resolve",
                      {"ip": "100.64.0.9", "secret": "dummy-test-gwsecret",
                       "want": "push-token", "token": "AB" * 32,
                       "sandbox": True})
    check(code == 200 and data["ok"] and data["registered"]
          and data["count"] == 1,
          "push token registers via the whois'd gateway route")
    code, data = post("/api/gateway/resolve",
                      {"ip": "100.64.0.9", "secret": "dummy-test-gwsecret",
                       "want": "push-token", "token": "not-a-token"})
    check(code == 400, "malformed device token refused")
finally:
    app.podman = _real_gate_podman

r = app.op_person_push(_gate_uid, {"token": "AB" * 32})
check(r["ok"] and r["count"] == 1,
      "re-registering the same token is an idempotent refresh")
for i in range(12):
    app.op_person_push(_gate_uid, {"token": f"{i:02x}" * 32})
check(len(app.load_push()["tokens"][_gate_uid]) == 10,
      "per-person token cap holds at 10")
r = app.op_person_push(_gate_uid, {"do": "unregister",
                                   "token": "0b" * 32})
check(r["ok"] and not r["registered"] and r["count"] == 9,
      "unregister removes exactly that token")

# Fan-out: the person's badges (apitest/sonarr/server from earlier
# suites) map topics -> their tokens; a relay "gone" answer prunes.
app.save_push({"tokens": {_gate_uid: [
    {"token": "aa" * 32, "sandbox": False},
    {"token": "bb" * 32, "sandbox": False}]}})
app._push_recent.clear()
_woken = []


def _fake_relay_urlopen(req, timeout=10):
    body = json.loads(req.data)
    _woken.append(body["token"])
    if body["token"].startswith("bb"):
        raise _urlerr.HTTPError(req.full_url, 400, "bad", {},
                                _io.BytesIO(b'{"ok":false,"gone":true,'
                                            b'"error":"BadDeviceToken"}'))
    return _CapsResp('{"ok":true}')


_real_push_urlopen = _urlreq.urlopen
_urlreq.urlopen = _fake_relay_urlopen
try:
    app._push_handle_topic("tlr-media-sonarr")
    check(sorted(_woken) == ["aa" * 32, "bb" * 32],
          "a topic message wakes every device of every reader")
    left = [t["token"] for t in app.load_push()["tokens"][_gate_uid]]
    check(left == ["aa" * 32],
          "relay 'gone' prunes the dead token, keeps the live one")
    _woken.clear()
    app._push_handle_topic("tlr-media-sonarr")
    check(_woken == [],
          "burst coalescing: no re-wake within the window")
    _woken.clear()
    app._push_recent.clear()
    app._push_handle_topic("tlr-media-radarr")
    check(_woken == [],
          "topics outside the person's badges wake nobody")
finally:
    _urlreq.urlopen = _real_push_urlopen

catsrv.shutdown()
srv.shutdown()
print("WEB API TEST PASSED")
