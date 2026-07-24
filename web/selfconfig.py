"""Tailarr self-config gateway (the tailarr-gate system pod).

Runs the CONTROLLER'S OWN IMAGE with this entrypoint — a deliberately
tiny surface that user devices are allowed to reach (the only fenced
grant with tag:tailarr-user as a network src; see acl-design.md §12).
The Tailarr app on a user's device calls GET /self/notifications and
gets back ITS OWN notification credentials, no configuration needed;
GET /self/services returns the services its person's badges grant,
ready to drop into the app's modules (v0.23.0).

Identity comes from the wire: this listener binds directly in the
sidecar's network namespace on plain :80, so the TCP peer address IS
the caller's tailnet IP — unforgeable inside a tailnet, and the tailnet
encrypts transport. The gateway holds no secrets and makes no
decisions: it forwards {ip, secret} to the controller over the fleet
intercom, and the controller does the whois (against THIS pod's
sidecar, whose netmap contains the user devices), resolves the person,
and returns their handout. Compromising this pod yields exactly the
ability to ask that question.

Env (set at deploy by the controller): CONTROLLER_URL (the controller's
tailnet address), GATEWAY_SECRET (per-install, proves requests come
from this pod and not an arbitrary fleet container).
"""

import json
import os
import signal
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONTROLLER_URL = os.environ.get("CONTROLLER_URL", "").rstrip("/")
GATEWAY_SECRET = os.environ.get("GATEWAY_SECRET", "")
PORT = int(os.environ.get("GATE_PORT", "80"))


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # Each /self/* route maps to a "want" the controller resolves for
    # the whois'd caller — adding a route here needs a matching branch
    # in op_gateway_resolve (same release: the converge pass moves this
    # pod onto the new controller image at upgrade).
    ROUTES = {"/self/notifications": "notifications",
              "/self/services": "services"}
    # POST routes carry a small body forward (allow-listed fields only —
    # the gateway never blindly proxies caller JSON to the controller).
    POST_ROUTES = {"/self/push-token": ("push-token",
                                        ("token", "sandbox", "do"))}

    def _forward(self, want, extra=None):
        if not (CONTROLLER_URL and GATEWAY_SECRET):
            return self._send(
                {"ok": False, "error": "gateway not configured"}, 500)
        payload = {"ip": self.client_address[0], "want": want,
                   "secret": GATEWAY_SECRET, **(extra or {})}
        req = urllib.request.Request(
            CONTROLLER_URL + "/api/gateway/resolve",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return self._send(json.load(r), r.status)
        except urllib.error.HTTPError as e:
            try:
                return self._send(json.load(e), e.code)
            except ValueError:
                return self._send(
                    {"ok": False, "error": f"controller: HTTP {e.code}"},
                    502)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return self._send(
                {"ok": False, "error": f"controller unreachable: {e}"}, 502)

    def do_GET(self):
        want = self.ROUTES.get(self.path)
        if not want:
            return self._send({"ok": False, "error": "not found"}, 404)
        return self._forward(want)

    def do_POST(self):
        route = self.POST_ROUTES.get(self.path)
        if not route:
            return self._send({"ok": False, "error": "not found"}, 404)
        want, allowed = route
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 4096)
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, OSError):
            return self._send({"ok": False, "error": "bad json"}, 400)
        return self._forward(want,
                             {k: body.get(k) for k in allowed if k in body})

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print(f"Tailarr self-config gateway on :{PORT} -> {CONTROLLER_URL}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
