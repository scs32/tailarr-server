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
    "shares": ["media"], "tailscale": False, "https": False, "npm": False,
    "authkey": "",
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
r = app.op_share_delete("archive")
check(r["ok"] and "Deleted share" in r["message"]
      and "archive" not in app.load_shares(), "delete works")

print("WEB SHARES TEST PASSED")
