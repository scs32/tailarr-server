#!/usr/bin/env python3
"""Test the bootstrap's embedded adopt/mint script against the real app.py.

bootstrap-tailarr.sh's API-credential path runs a python heredoc inside a
one-shot controller container (policy fence adopt + controller key mint —
the same code behind the Settings wizard). This extracts that heredoc from
the shell script and runs it in-process with app's Tailscale calls
monkeypatched, so a rename/reshape of op_policy_init_fences or
ts_mint_pod_key (or a broken heredoc) fails CI instead of a fresh install.
"""
import contextlib
import io
import json
import os
import re
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["APP_DIR"] = REPO
os.environ["PODS_DIR"] = os.path.join(tempfile.mkdtemp(), "Pods")
sys.path.insert(0, os.path.join(REPO, "web"))
import app  # noqa: E402

with open(os.path.join(REPO, "bootstrap-tailarr.sh")) as f:
    m = re.search(r"python3 - <<'PYEOF'\n(.*?)\nPYEOF\n", f.read(), re.S)
assert m, "could not find the PYEOF heredoc in bootstrap-tailarr.sh"
SRC = compile(m.group(1), "bootstrap-heredoc.py", "exec")


def run(mint):
    os.environ["MINT_KEY"] = "mint" if mint else ""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            exec(SRC, {"__name__": "__main__"})
        except SystemExit:
            pass
    out = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert set(out) == {"ok", "error", "key"}, out  # the bash side parses these
    return out


def ok(msg):
    print(f"  ok: {msg}")


print("=== bootstrap adopt/mint heredoc ===")

app._ts_token = lambda: ""
r = run(mint=True)
assert r["ok"] is False and "access token" in r["error"], r
ok("rejected credential reports a clear token error")

app._ts_token = lambda: "tok"
app.op_policy_init_fences = lambda: {"ok": False, "added": [],
                                     "error": "acl GET: boom"}
r = run(mint=True)
assert r["ok"] is False and r["error"] == "policy adopt: acl GET: boom", r
ok("adopt failure surfaces before any mint")

app.op_policy_init_fences = lambda: {"ok": True, "added": ["grants"],
                                     "error": None}
app.ts_mint_pod_key = lambda name: {"ok": True, "error": None,
                                    "key": f"dummy-test-authkey-{name}"}
r = run(mint=True)
assert r == {"ok": True, "error": None,
             "key": "dummy-test-authkey-tailarr"}, r
ok("adopt + mint returns the controller key")

app.ts_mint_pod_key = lambda name: {"ok": False, "error": "keys API: 403",
                                    "key": ""}
r = run(mint=True)
assert r["ok"] is False and r["error"] == "key mint: keys API: 403", r
ok("mint failure is reported, not an empty key")

def _no_mint(name):
    raise AssertionError("mint called on the adopt-only path")

app.ts_mint_pod_key = _no_mint
r = run(mint=False)
assert r == {"ok": True, "error": None, "key": ""}, r
ok("adopt-only run never mints")

print("BOOTSTRAP MINT TEST PASSED")
