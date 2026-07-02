#!/usr/bin/env python3
"""HomePod Creator web UI - concept-validation MVP.

Deliberately basic: Python stdlib only, no styling, no auth (it is reachable
only over the tailnet). A thin front end over the same engine the CLI wizard
uses: it builds the config JSON and pipes it to create.sh, and start/stop
just invoke each pod's generated run.sh/stop.sh.

Expects (provided by the container image / bootstrap script):
  - engine scripts + homelab.js in APP_DIR
  - host ~/Pods mounted at PODS_DIR (same path as on the host!)
  - host podman socket mounted, CONTAINER_HOST pointing at it
"""

import html
import json
import os
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.environ.get("APP_DIR", "/app")
PODS_DIR = os.environ.get("PODS_DIR", "/root/Pods")
PORT = int(os.environ.get("PORT", "8080"))

CONTROLLER_PODS = {"homepod"}  # don't offer stop-self buttons


def load_services():
    with open(os.path.join(APP_DIR, "homelab.js")) as f:
        return {s["name"]: s for s in json.load(f)}


def podman(*args, timeout=60):
    try:
        return subprocess.run(
            ["podman", *args], capture_output=True, text=True, timeout=timeout
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(args, 1, "", f"podman unavailable: {e}")


def running_names():
    out = podman("ps", "--format", "{{.Names}}")
    return set(out.stdout.split()) if out.returncode == 0 else set()


def deployed_services():
    if not os.path.isdir(PODS_DIR):
        return []
    return sorted(
        d
        for d in os.listdir(PODS_DIR)
        if os.path.isfile(os.path.join(PODS_DIR, d, "run.sh"))
    )


def page(title, body):
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title></head>"
        f"<body><h1>{html.escape(title)}</h1>{body}"
        "<hr><p><a href='/'>home</a></p></body></html>"
    ).encode()


def dashboard():
    services = load_services()
    running = running_names()
    deployed = deployed_services()

    rows = []
    for name in deployed:
        state = "running" if name in running else "stopped"
        buttons = (
            f"<form style='display:inline' method='post' action='/action'>"
            f"<input type='hidden' name='service' value='{html.escape(name)}'>"
            f"<button name='do' value='start'>start</button> "
            f"<button name='do' value='stop'>stop</button> "
            f"<button name='do' value='logs'>logs</button></form>"
        )
        rows.append(
            f"<tr><td>{html.escape(name)}</td><td>{state}</td><td>{buttons}</td></tr>"
        )
    deployed_html = (
        "<table border=1><tr><th>service</th><th>state</th><th>actions</th></tr>"
        + "".join(rows)
        + "</table>"
        if rows
        else "<p>No services deployed yet.</p>"
    )

    cat_rows = []
    for name, spec in sorted(services.items()):
        port = next(iter(spec.get("ports", {})), "")
        installed = " (installed)" if name in deployed else ""
        cat_rows.append(
            f"<tr><td>{html.escape(name)}{installed}</td>"
            f"<td>{html.escape(spec['image'])}</td><td>{port}</td>"
            f"<td><a href='/install?service={urllib.parse.quote(name)}'>install</a></td></tr>"
        )
    catalog_html = (
        "<table border=1><tr><th>service</th><th>image</th><th>port</th><th></th></tr>"
        + "".join(cat_rows)
        + "</table>"
    )

    return page(
        "HomePod Creator",
        f"<h2>Deployed</h2>{deployed_html}<h2>Catalog</h2>{catalog_html}",
    )


def install_form(name):
    spec = load_services().get(name)
    if not spec:
        return page("Unknown service", "<p>Not in catalog.</p>")

    env_fields = "".join(
        f"<label>{html.escape(k)} "
        f"<input name='env.{html.escape(k)}' value='{html.escape(v)}'></label><br>"
        for k, v in spec.get("environment", {}).items()
    )
    vol_fields = "".join(
        f"<label>host path for {html.escape(cpath)} "
        f"<input size=50 name='vol.{html.escape(cpath)}' "
        f"value='{html.escape(os.path.join(PODS_DIR, name, cpath.lstrip('/')))}'>"
        f"</label><br>"
        for _, cpath in spec.get("volumes", {}).items()
    )
    key_file = os.path.join(PODS_DIR, name, ".tailscale_authkey")
    key_hint = "existing key file found - leave blank to reuse it" if os.path.isfile(
        key_file
    ) else "paste a fresh single-use, non-ephemeral key"

    body = f"""
<form method='post' action='/install'>
<input type='hidden' name='service' value='{html.escape(name)}'>
<p><label><input type='checkbox' name='tailscale' checked> Tailscale (own tailnet identity)</label></p>
<p><label><input type='checkbox' name='https' checked> HTTPS via tailscale serve
(https://{html.escape(name)}.&lt;tailnet&gt;.ts.net - needs HTTPS Certificates
enabled once in the Tailscale admin console)</label></p>
<p><label><input type='checkbox' name='npm'> Bundle Nginx Proxy Manager</label></p>
<p><label>Tailscale auth key ({key_hint})<br>
<input size=70 name='authkey' autocomplete='off'></label></p>
<h3>Environment</h3>{env_fields or "<p>none</p>"}
<h3>Volumes</h3>{vol_fields or "<p>none</p>"}
<p><button>Install</button></p>
</form>"""
    return page(f"Install {name}", body)


def do_install(form):
    name = form.get("service", [""])[0]
    spec = load_services().get(name)
    if not spec:
        return page("Error", "<p>Unknown service.</p>")

    tailscale = "yes" if "tailscale" in form else "no"
    npm = "yes" if "npm" in form else "no"
    https = "yes" if ("https" in form and tailscale == "yes") else "no"

    auth_key_file = ""
    if tailscale == "yes":
        auth_key_file = os.path.join(PODS_DIR, name, ".tailscale_authkey")
        pasted = form.get("authkey", [""])[0].strip()
        if pasted:
            os.makedirs(os.path.dirname(auth_key_file), exist_ok=True)
            with open(auth_key_file, "w") as f:
                f.write(pasted + "\n")
            os.chmod(auth_key_file, 0o600)
        elif not os.path.isfile(auth_key_file):
            return page("Error", "<p>Tailscale enabled but no auth key given.</p>")

    env = {
        k[len("env."):]: v[0]
        for k, v in form.items()
        if k.startswith("env.")
    }
    volumes = {
        k[len("vol."):]: v[0]  # container path -> host path (engine order)
        for k, v in form.items()
        if k.startswith("vol.")
    }

    network_mode = (
        f"service:tailscale-{name}" if tailscale == "yes"
        else spec.get("network_mode", "bridge")
    )
    config = {
        "container": name,
        "image": spec["image"],
        "network_mode": network_mode,
        "ports": spec.get("ports", {}),
        "restart_policy": spec.get("restart_policy", "unless-stopped"),
        "include_npm": npm,
        "include_tailscale": tailscale,
        "include_https": https,
        "auth_key_file": auth_key_file,
        "base_path": PODS_DIR,
        "environment": env,
        "volumes": volumes,
    }

    result = subprocess.run(
        ["bash", os.path.join(APP_DIR, "create.sh")],
        input=json.dumps(config),
        capture_output=True,
        text=True,
        cwd="/tmp",
        timeout=300,
    )
    out = html.escape(result.stdout + result.stderr)
    if result.returncode != 0:
        return page(f"Install {name}: FAILED", f"<pre>{out}</pre>")

    start_button = (
        f"<form method='post' action='/action'>"
        f"<input type='hidden' name='service' value='{html.escape(name)}'>"
        f"<button name='do' value='start'>Start {html.escape(name)} now</button>"
        f"</form>"
        "<p>Installing only generated the pod - it is not running until started."
        " Starting pulls the image and enrolls on the tailnet, so it can take"
        " a few minutes.</p>"
    )
    return page(f"Install {name}: installed", start_button + f"<pre>{out}</pre>")


def do_action(form):
    name = form.get("service", [""])[0]
    action = form.get("do", [""])[0]
    if name not in deployed_services():
        return page("Error", "<p>Unknown service.</p>")
    if name in CONTROLLER_PODS and action == "stop":
        return page("Refused", "<p>Not stopping the controller from itself.</p>")

    svc_dir = os.path.join(PODS_DIR, name)
    if action == "start":
        r = subprocess.run(
            ["sh", "./run.sh"], cwd=svc_dir, capture_output=True, text=True,
            timeout=600,
        )
    elif action == "stop":
        r = subprocess.run(
            ["sh", "./stop.sh"], cwd=svc_dir, capture_output=True, text=True,
            timeout=120,
        )
    elif action == "logs":
        r = podman("logs", "--tail", "100", name, timeout=30)
    else:
        return page("Error", "<p>Unknown action.</p>")

    out = html.escape(r.stdout + r.stderr)
    status = "ok" if r.returncode == 0 else f"exit {r.returncode}"
    return page(f"{action} {name}: {status}", f"<pre>{out}</pre>")


class Handler(BaseHTTPRequestHandler):
    def _send(self, content, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/":
            self._send(dashboard())
        elif url.path == "/install":
            q = urllib.parse.parse_qs(url.query)
            self._send(install_form(q.get("service", [""])[0]))
        else:
            self._send(page("Not found", ""), 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        try:
            if self.path == "/install":
                self._send(do_install(form))
            elif self.path == "/action":
                self._send(do_action(form))
            else:
                self._send(page("Not found", ""), 404)
        except subprocess.TimeoutExpired:
            self._send(page("Timeout", "<p>The operation took too long.</p>"), 500)

    def log_message(self, fmt, *args):  # quieter default logging
        print("%s - %s" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    print(f"HomePod web UI on :{PORT} (pods dir: {PODS_DIR})")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
