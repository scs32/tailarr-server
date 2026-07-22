"""Tailarr self-config gateway (the tailarr-gate system pod).

Runs the CONTROLLER'S OWN IMAGE with this entrypoint — a deliberately
tiny surface that user devices are allowed to reach (the only fenced
grant with tag:tailarr-user as a network src; see acl-design.md §12).
The Tailarr app on a user's device calls GET /self/notifications and
gets back ITS OWN notification credentials, no configuration needed.

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

    def do_GET(self):
        if self.path != "/self/notifications":
            return self._send({"ok": False, "error": "not found"}, 404)
        if not (CONTROLLER_URL and GATEWAY_SECRET):
            return self._send(
                {"ok": False, "error": "gateway not configured"}, 500)
        req = urllib.request.Request(
            CONTROLLER_URL + "/api/gateway/resolve",
            data=json.dumps({"ip": self.client_address[0],
                             "secret": GATEWAY_SECRET}).encode(),
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

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print(f"Tailarr self-config gateway on :{PORT} -> {CONTROLLER_URL}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
