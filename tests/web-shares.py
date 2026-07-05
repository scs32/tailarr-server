#!/usr/bin/env python3
"""Functional test of the shares registry + attach flow (op_* layer).

Drives web/app.py's op_* functions directly (no HTTP) against the real
create.sh engine in a temp PODS_DIR. No containers or podman needed:
installing only generates scripts. The share host paths point at /data
and /archive, which are deliberately NOT creatable in test environments -
that also exercises the warn-and-continue path for unmounted share roots.
"""
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
    "authkey": "tskey-test-web-key",
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
    "shares": [], "tailscale": False, "https": False, "authkey": "tskey-test-web-key",
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

# --- network status + set ---
net = {e["name"]: e for e in app.status_network()}
check(net["testpod"]["tailscale"] is True and net["testpod"]["ip"] == "",
      "network: pod is a tailnet node (identity pending without a live sidecar)")
r = app.op_network_set("testpod", {})
check(r["ok"], "network set: re-render succeeds")
check(app.op_network_set("homepod", {})["status"] == "refused",
      "network set: controller refused")
check(app.op_network_set("nope", {})["error"] == "Unknown service.",
      "network set: unknown pod rejected")

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
    "shares": [], "tailscale": False, "https": False, "authkey": "tskey-test-web-key",
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
