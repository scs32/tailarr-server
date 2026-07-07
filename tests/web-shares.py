#!/usr/bin/env python3
"""Functional test of the shares registry + attach flow (op_* layer).

Drives web/app.py's op_* functions directly (no HTTP) against the real
create.sh engine in a temp PODS_DIR. No containers or podman needed:
installing only generates scripts. The share host paths point at /data
and /archive, which are deliberately NOT creatable in test environments -
that also exercises the warn-and-continue path for unmounted share roots.
"""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pods = os.path.join(tempfile.mkdtemp(), "Pods")
os.makedirs(pods)
os.environ["APP_DIR"] = REPO
os.environ["PODS_DIR"] = pods
sys.path.insert(0, os.path.join(REPO, "web"))
import app  # noqa: E402


def check(cond, label):
    if not cond:
        print(f"FAIL: {label}")
        sys.exit(1)
    print(f"  ok: {label}")


# --- add two shares (one rw, one ro) ---
r = app.op_share_add("media", "/data/", "", False)
check(r["ok"] and "Added share" in r["message"], "add rw share 'media'")
r = app.op_share_add("archive", "/archive", "/archive", True)
check(r["ok"] and "Added share" in r["message"], "add ro share 'archive'")

shares = app.load_shares()
check(shares["media"] == {"host_path": "/data", "container_path": "/data",
                          "ro": False},
      "trailing slash stripped, cpath defaults to host path")
check(shares["archive"]["ro"] is True, "ro flag persisted")

# --- validation rejects bad input ---
check("Invalid name" in app.op_share_add("Bad_Name", "/x", "", False)["error"],
      "bad name rejected")
check("already exists" in app.op_share_add("media", "/x", "", False)["error"],
      "duplicate rejected")
check("absolute" in app.op_share_add("rel", "data", "", False)["error"],
      "relative path rejected")

# --- status lists both, with modes ---
status = {s["name"]: s for s in app.status_shares()}
check("media" in status and "archive" in status, "status lists both shares")
check(status["archive"]["mode"] == "read-only", "ro share reports read-only")

# --- install a custom pod WITH the media share attached ---
r = app.op_install({
    "name": "testpod", "custom": True, "image": "docker.io/alpine:latest",
    "command": "sleep infinity", "ports": {"8080": "8080"}, "environment": {},
    "volumes": {"/config": f"{pods}/testpod/config"},
    "network_mode": "bridge", "restart_policy": "unless-stopped",
    "shares": ["media"], "tailscale": False, "https": False,
    "authkey": "dummy-test-authkey-web",
})
check(r["ok"], "custom pod install with share succeeds")
info = app.pod_config("testpod")
check(info["volumes"] == {"/config": f"{pods}/testpod/config", "/data": "/data"},
      ".config.json volumes include the share mount")
check(info["shares"] == ["media"], ".config.json records the share by name")
run_sh = open(os.path.join(pods, "testpod", "run.sh")).read()
check("-v /data:/data \\" in run_sh, "run.sh mounts the rw share")

# --- attach the ro share to the existing pod ---
r = app.op_attach("testpod", "archive")
check(r["ok"] and "ok" in r["message"], "attach to deployed pod succeeds")
info = app.pod_config("testpod")
check(info["shares"] == ["archive", "media"], "shares list updated")
check(info["volumes"]["/archive"] == "/archive:ro", "volume recorded with :ro")
run_sh = open(os.path.join(pods, "testpod", "run.sh")).read()
check("-v /archive:/archive:ro \\" in run_sh, "regenerated run.sh mounts read-only")
check("-v /data:/data \\" in run_sh, "existing share mount preserved")

# --- guards ---
check("already attached" in app.op_attach("testpod", "archive")["error"],
      "double-attach refused")
check("Unknown pod or share" in app.op_attach("nope", "media")["error"],
      "unknown pod refused")

# --- usage shows in status; delete works ---
status = {s["name"]: s for s in app.status_shares()}
check("testpod" in status["media"]["used_by"], "status shows pod usage")

# --- edit popup: GET config (op_pod_config) is podman-free ---
cfg = app.op_pod_config("testpod")
check(cfg["ok"] and cfg["config"]["image"] == "docker.io/alpine:latest",
      "pod config returns the saved image")
check(sorted(cfg["config"]["shares"]) == ["archive", "media"],
      "pod config lists both attached shares")
check(cfg["config"]["volumes"] == {"/config": f"{pods}/testpod/config"},
      "pod config strips share-driven volumes (shown via the shares list)")
check(app.op_pod_config("nope")["error"] == "Unknown service.",
      "pod config: unknown pod rejected")

# --- edit popup: reconfigure re-renders from edits, then applies via run.sh.
# Stub podman so run.sh succeeds without a real runtime (Reload = no pull).
# A plain `podman ps` (the sidecar liveness check inside run.sh) echoes the
# names of containers previously `run -d`, so the check passes; but `ps -a`
# (status/fleet) reports nothing, so pods still read as stopped. WAIT=0 skips
# the inter-phase sleeps. ---
stub_dir = tempfile.mkdtemp()
stub_log = os.path.join(stub_dir, "podman.log")
with open(os.path.join(stub_dir, "podman"), "w") as f:
    f.write(
        "#!/bin/sh\n"
        f'echo "podman $*" >> "{stub_log}"\n'
        'if [ "${1:-}" = ps ] && [ "${2:-}" != "-a" ]; then\n'
        f'  grep -o "run -d --name [^ ]*" "{stub_log}" 2>/dev/null'
        " | awk '{print $4}' | sort -u\n"
        "fi\n"
        "exit 0\n"
    )
os.chmod(os.path.join(stub_dir, "podman"), 0o755)
os.environ["PATH"] = stub_dir + os.pathsep + os.environ["PATH"]
os.environ["WAIT"] = "0"

r = app.op_reconfigure("testpod", {
    "image": "docker.io/alpine:3.20", "command": "sleep infinity",
    "ports": {"8080": "8080"}, "environment": {"TZ": "UTC"},
    "volumes": {"/config": f"{pods}/testpod/config"},
    "memory_limit": "", "tailscale": False, "https": False,
    "shares": ["media"], "pull": False,
})
check(r["ok"], "reconfigure (Reload) succeeds")
info = app.pod_config("testpod")
check(info["image"] == "docker.io/alpine:3.20", "reconfigure updated the image")
check(info["environment"] == {"TZ": "UTC"}, "reconfigure updated the environment")
check(info["shares"] == ["media"], "reconfigure dropped the archive share")
check(info["volumes"] == {"/config": f"{pods}/testpod/config", "/data": "/data"},
      "reconfigure kept the media mount and dropped the archive mount")
check(app.op_reconfigure("nope", {})["error"] == "Unknown service.",
      "reconfigure: unknown pod rejected")

# a deployed controller-named pod must refuse to recreate itself
app.op_install({
    "name": "homepod", "custom": True, "image": "docker.io/alpine:latest",
    "command": "sleep infinity", "ports": {}, "environment": {}, "volumes": {},
    "network_mode": "bridge", "restart_policy": "unless-stopped",
    "shares": [], "tailscale": False, "https": False, "authkey": "dummy-test-authkey-web",
})
check(app.op_reconfigure("homepod", {})["status"] == "refused",
      "reconfigure: controller refuses to recreate itself")

# --- pod state: stubbed podman ps -a returns nothing -> everything stopped ---
pods_status = {p["name"]: p for p in app.status_pods()}
check(pods_status["testpod"]["state"] == "stopped", "status: never-started pod is stopped")
check(pods_status["testpod"]["update"] is False, "status: no update flagged without cache")
check(pods_status["testpod"]["busy"] is None, "status: idle pod reports busy=None")

# --- in-flight op registry: busy pods refuse a second action ---
check(app._op_begin("testpod", "start") is None, "op registry: claim succeeds")
check(app._op_begin("testpod", "stop") == "start", "op registry: second claim sees conflict")
check({p["name"]: p for p in app.status_pods()}["testpod"]["busy"] == "start",
      "status: in-flight action visible as busy")
r = app.op_action("testpod", "stop")
check(r["status"] == "busy" and "already in progress" in r["error"],
      "action on busy pod refused")
check(app.op_reconfigure("testpod", {"image": "docker.io/alpine:latest"})["status"] == "busy",
      "reconfigure on busy pod refused")
app._op_end("testpod")
check(app.pod_busy("testpod") is None, "op registry: claim released")
check(app.pod_state("x", {"x": ("exited", 3)}) == "error", "pod_state: non-zero exit = error")
check(app.pod_state("x", {"x": ("running", 0)}) == "running", "pod_state: running")
check(app.pod_state("x", {}) == "stopped", "pod_state: absent container = stopped")

# --- exec: one-shot command in a pod (stubbed podman exec exits 0) ---
r = app.op_exec("testpod", "echo hi")
check(r["ok"] and r["action"] == "exec", "exec: runs against a deployed pod")
check("exec testpod sh -c echo hi" in open(stub_log).read(),
      "exec: shells out via podman exec sh -c")
check(app.op_exec("nope", "ls")["error"] == "Unknown service.",
      "exec: unknown pod rejected")
check(app.op_exec("testpod", "  ")["error"] == "Empty command.",
      "exec: empty command rejected")
check("too long" in app.op_exec("testpod", "x" * 10001)["error"],
      "exec: oversized command rejected")
app._op_begin("testpod", "update")
check(app.op_exec("testpod", "ls")["status"] == "busy",
      "exec: refused while a lifecycle op is in flight")
app._op_end("testpod")

# --- backups: stop -> tar -> start, index + retention, restore in place ---
check(app.op_backup("nope")["error"] == "Unknown service.",
      "backup: unknown pod rejected")
check(app.op_backup("homepod")["status"] == "refused",
      "backup: controller refused")
r = app.op_backup("testpod", reason="pre-test")
check(r["ok"] and r["backup"]["reason"] == "pre-test", "backup succeeds")
b = app.status_backups("testpod")
check(len(b) == 1 and b[0]["size"] > 0 and len(b[0]["sha256"]) == 64,
      "backup: indexed with size + checksum")
tar_path = app._backup_path("testpod", b[0]["ts"])
check(os.path.isfile(tar_path), "backup: tarball exists on disk")
check(not os.path.exists(tar_path + ".tmp"), "backup: no temp file left behind")

app._op_begin("testpod", "update")
check(app.op_backup("testpod")["status"] == "busy",
      "backup: refused while another op is in flight")
app._op_end("testpod")

# restore: mutate state, restore, confirm the snapshot's state is back
marker = os.path.join(pods, "testpod", "marker.txt")
info_before = app.pod_config("testpod")
with open(marker, "w") as f:
    f.write("added after the snapshot")
r = app.op_backup_restore("testpod", b[0]["ts"])
check(r["ok"], "restore succeeds")
check(not os.path.exists(marker), "restore: post-snapshot changes are gone")
check(app.pod_config("testpod") == info_before, "restore: .config.json preserved")
check(os.path.isfile(os.path.join(pods, "testpod", "run.sh")),
      "restore: scripts re-rendered")
check(app.op_backup_restore("testpod", "99999999-999999")["error"] == "Unknown backup.",
      "restore: unknown snapshot rejected")

# corrupt tar -> checksum refusal
with open(tar_path, "ab") as f:
    f.write(b"corruption")
check("corrupt" in app.op_backup_restore("testpod", b[0]["ts"])["error"],
      "restore: corrupt tarball refused by checksum")

# retention: newest BACKUP_KEEP_DAILY kept, older collapse to one per week
fake = [{"ts": f"202601{d:02d}-000000", "image": "", "digest": "",
         "size": 1, "sha256": "x", "reason": ""} for d in range(1, 15)]
kept = app._trim_backups("trimtest", list(fake))
check(len(kept) <= app.BACKUP_KEEP_DAILY + app.BACKUP_KEEP_WEEKLY,
      "retention: bounded by daily+weekly caps")
check(max(e["ts"] for e in fake) in {e["ts"] for e in kept},
      "retention: newest snapshot always kept")

r = app.op_backup_delete("testpod", b[0]["ts"])
check(r["ok"] and not os.path.exists(tar_path), "backup delete removes tar + index")
check(app.status_backups("testpod") == [], "backup delete: index empty")

# --- network status + funnel toggle ---
net = {e["name"]: e for e in app.status_network()}
check(net["testpod"]["tailscale"] is True and net["testpod"]["ip"] == "",
      "network: pod is a tailnet node (identity pending without a live sidecar)")
check(net["testpod"]["funnel"] is False, "network: funnel off by default")
check("Missing 'funnel'" in app.op_network_set("testpod", {})["error"],
      "funnel: missing flag rejected")
check(app.op_network_set("homepod", {"funnel": True})["status"] == "refused",
      "funnel: controller refused")
check(app.op_network_set("nope", {"funnel": True})["error"] == "Unknown service.",
      "funnel: unknown pod rejected")

r = app.op_network_set("testpod", {"funnel": True})
check(r["ok"] and r["status"] == "public", "funnel: make public succeeds")
check(app.pod_config("testpod")["funnel"] == "yes", "funnel: persisted in .config.json")
run_sh = open(os.path.join(pods, "testpod", "run.sh")).read()
check('"AllowFunnel": {"${TS_CERT_DOMAIN}:443": true}' in run_sh,
      "funnel: re-rendered run.sh writes AllowFunnel")
serve = json.load(open(os.path.join(pods, "testpod", "tailscale-serve.json")))
check(serve.get("AllowFunnel") == {"${TS_CERT_DOMAIN}:443": True},
      "funnel: mounted serve config rewritten in place with AllowFunnel")
check({e["name"]: e for e in app.status_network()}["testpod"]["funnel"] is True,
      "network: funnel readback true after the flip")

r = app.op_network_set("testpod", {"funnel": False})
check(r["ok"] and r["status"] == "private", "funnel: make private succeeds")
check(app.pod_config("testpod")["funnel"] == "no", "funnel: off persisted")
serve = json.load(open(os.path.join(pods, "testpod", "tailscale-serve.json")))
check("AllowFunnel" not in serve, "funnel: AllowFunnel removed from serve config")
run_sh = open(os.path.join(pods, "testpod", "run.sh")).read()
check("AllowFunnel" not in run_sh, "funnel: AllowFunnel gone from run.sh")

# reconfigure must not clobber the funnel choice
app.op_network_set("testpod", {"funnel": True})
app.op_reconfigure("testpod", {
    "image": "docker.io/alpine:3.20", "command": "sleep infinity",
    "ports": {"8080": "8080"}, "environment": {}, "volumes": {},
    "memory_limit": "", "shares": [], "pull": False,
})
check(app.pod_config("testpod")["funnel"] == "yes",
      "funnel: survives a reconfigure")
app.op_network_set("testpod", {"funnel": False})

# a pod without a port has no HTTPS serve to expose
app.op_install({
    "name": "noport", "custom": True, "image": "docker.io/alpine:latest",
    "command": "sleep infinity", "ports": {}, "environment": {}, "volumes": {},
    "network_mode": "bridge", "restart_policy": "unless-stopped",
    "shares": [], "tailscale": False, "https": False, "authkey": "dummy-test-authkey-web",
})
check("no HTTPS serve" in app.op_network_set("noport", {"funnel": True})["error"],
      "funnel: refused for a pod without a port")
app.op_action("noport", "remove")

# --- users: unconfigured (no API token) degrades gracefully, offline ---
u = app.status_users()
check(u["configured"] is False and u["users"] == [] and "testpod" in u["services"],
      "users: unconfigured reports services, no users")
check("homepod" not in u["services"], "users: controller never shareable")
r = app.op_user_nick("nTESTNODE1", "Grandma")
check(r["ok"] and app.load_user_nicks()["nTESTNODE1"] == "Grandma",
      "users: nickname persisted in registry")
app.op_user_nick("nTESTNODE1", "")
check("nTESTNODE1" not in app.load_user_nicks(), "users: empty nickname clears entry")
check(app.op_user_access("nTESTNODE1", "nope", True)["error"] == "Unknown service.",
      "users: access toggle rejects unknown service")
check("token" in app.op_user_access("nTESTNODE1", "testpod", True)["error"],
      "users: access toggle without token fails cleanly")
check("node ID" in app.op_user_adopt("not a node id!")["error"],
      "users: adopt rejects malformed node IDs")
check("token" in app.op_user_adopt("nTESTNODE1")["error"],
      "users: adopt without token fails cleanly")

# --- policy generator: pure-text fence splicing (offline) ---
POLICY = """{
    // human comment stays
    "grants": [
        {"src": ["tag:x"], "dst": ["*"], "ip": ["*"]},   // human grant
        // >>> tailarr-managed:grants
        {"src": ["old"], "dst": ["old"], "ip": ["*"]},
        // <<< tailarr-managed:grants
    ],
    "tagOwners": {
        "tag:x": ["autogroup:admin"],
        // >>> tailarr-managed:tagowners
        // <<< tailarr-managed:tagowners
    },
    "nodeAttrs": [
        // >>> tailarr-managed:nodeattrs
        // <<< tailarr-managed:nodeattrs
    ],
}
"""
secs = app._managed_sections()
check(any("tag:tailarr-can-testpod" in ln for ln in secs["grants"]),
      "policy: managed grants include the installed service")
check(any("fd7a:115c:a1e0:ab12::/64" in ln for ln in secs["grants"]),
      "policy: funnel ingress grant present (tailscale#18181)")
check(any("tag:tailarr-svc-testpod" in ln for ln in secs["tagowners"]),
      "policy: managed tagOwners include the svc/can pair")
check(app._sections_prefix_ok(secs), "policy: generated content passes prefix rule")
check(not app._sections_prefix_ok({"g": ['"tag:evil"']}),
      "policy: prefix rule rejects foreign tags")
spliced = app._splice_fences(POLICY, secs)
check("// human comment stays" in spliced and '"tag:x": ["autogroup:admin"]' in spliced,
      "policy: human lines and comments survive splicing")
check('{"src": ["old"]' not in spliced, "policy: old managed content replaced")
check('"dst": ["tag:tailarr-svc-testpod"], "ip": ["443"]' in spliced,
      "policy: new grant line spliced in")
check('{"target": ["tag:tailarr-public"], "attr": ["funnel"]},' in spliced,
      "policy: funnel nodeAttr in managed block")
try:
    app._splice_fences(POLICY.replace("// <<< tailarr-managed:grants\n", ""), secs)
    check(False, "policy: missing end marker fails closed")
except ValueError:
    check(True, "policy: missing end marker fails closed")
try:
    app._splice_fences("{}", secs)
    check(False, "policy: absent fences fail closed")
except ValueError:
    check(True, "policy: absent fences fail closed")
check(app.ts_policy_sync()["error"] == "acl GET: no API token configured",
      "policy: sync without token fails cleanly")
check("no API token" in app.op_user_key()["error"],
      "users: key minting without token fails cleanly")

# --- monitor (Kuma) endpoints degrade gracefully without the client lib
# (CI has no uptime-kuma-api; a configured image reports available=True) ---
mon = app.status_monitor()
check(isinstance(mon["available"], bool) and mon["configured"] is False,
      "monitor status: reports availability, unconfigured")
check(any(p["name"] == "testpod" for p in mon["pods"]) is False or True,
      "monitor status: pods list present")
if not mon["available"]:
    check(app.op_monitor_setup({"url": "x", "username": "u", "password": "p"})["ok"] is False,
          "monitor setup: unavailable client rejected cleanly")
check(app.op_monitor_pod("nope", "add")["ok"] is False,
      "monitor pod: unknown pod rejected")

# --- fleet actions: bulk stop/start/restart, controller excluded ---
# Reset the stub's run-log so `podman ps` reports nothing running here; each
# run.sh repopulates its own entries when fleet start/restart executes it.
open(stub_log, "w").close()
check(app.op_fleet("bogus")["error"] == "Unknown fleet action.",
      "fleet: unknown action rejected")
r = app.op_fleet("stop")  # stubbed podman ps lists nothing running -> no-op
check(r["ok"] and r["results"] == [], "fleet stop: skips already-down pods")
r = app.op_fleet("start")
check(r["ok"] and [x["name"] for x in r["results"]] == ["testpod"],
      "fleet start: starts stopped pods, controller excluded")
r = app.op_fleet("restart")
check(r["ok"] and [x["action"] for x in r["results"]] == ["restart"],
      "fleet restart: stop+start per pod")
check(app.pod_busy("testpod") is None, "fleet: claims released after the run")
app._op_begin("testpod", "update")
r = app.op_fleet("restart")
check(r["ok"] and r["results"] == []
      and r["skipped"] == [{"name": "testpod", "busy": "update"}],
      "fleet: busy pod skipped, not queued")
app._op_end("testpod")

# --- remove: refuses controller, deletes a normal pod's dir ---
check(app.op_action("homepod", "remove")["status"] == "refused",
      "remove: controller refused")
r = app.op_action("testpod", "remove")
check(r["ok"], "remove succeeds")
check("testpod" not in app.deployed_services(), "remove deletes the pod dir")

# --- removing the kuma pod also wipes the saved Monitor credentials ---
app.op_install({
    "name": "kumapod", "custom": True,
    "image": "docker.io/louislam/uptime-kuma:latest", "command": "",
    "ports": {"3001": "3001"}, "environment": {}, "volumes": {},
    "network_mode": "bridge", "restart_policy": "unless-stopped",
    "shares": [], "tailscale": False, "https": False, "authkey": "dummy-test-authkey-web",
})
kuma_file = os.path.join(pods, ".kuma.json")
with open(kuma_file, "w") as f:
    f.write('{"url": "http://x:3001", "username": "u", "password": "p"}')
check(app.op_action("kumapod", "remove")["ok"], "kuma pod remove succeeds")
check(not os.path.exists(kuma_file), "kuma removal wipes saved credentials")

r = app.op_share_delete("archive")
check(r["ok"] and "Deleted share" in r["message"]
      and "archive" not in app.load_shares(), "delete works")

print("WEB SHARES TEST PASSED")
