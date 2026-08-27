"""
kernel.py — the Mac executor kernel.

An HTTP server that:
  1. authenticates the caller (shared token),
  2. resolves the natural-language query to one capability + params,
  3. executes the capability (osascript / shell), and
  4. returns a JSON result.

Anything can drive it: curl, an iPhone Shortcut ("Get contents of URL"), or the
SMS-gateway webhook later. Start it with:

    AGENT_TOKEN=your-secret python3 kernel.py

Endpoints:
    GET  /health        -> {"ok": true}
    GET  /capabilities  -> list of capabilities and parameters
    POST /run           -> {"query": "...", "token": "..."}  (token also accepted
                           as an "Authorization: Bearer ..." header)
"""

import ipaddress
import json
import os
import re
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from capabilities import CAPABILITIES, execute, CapabilityError
from resolver import resolve

PORT = int(os.environ.get("AGENT_PORT", "8765"))
TOKEN = os.environ.get("AGENT_TOKEN")

# Tailscale hands out addresses from the CGNAT range 100.64.0.0/10.
_TAILNET = ipaddress.ip_network("100.64.0.0/10")


def tailnet_ip():
    """This Mac's Tailscale address, or None if it isn't on a tailnet."""
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for addr in re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out):
        try:
            if ipaddress.ip_address(addr) in _TAILNET:
                return addr
        except ValueError:
            continue
    return None


def resolve_host(value):
    """AGENT_HOST=tailscale binds only to the tailnet address, so joining an
    untrusted Wi-Fi never exposes the kernel the way 0.0.0.0 would."""
    if value.lower() in ("tailscale", "tailnet"):
        ip = tailnet_ip()
        if not ip:
            raise SystemExit(
                "AGENT_HOST=tailscale but this Mac has no Tailscale address.\n"
                "Start Tailscale.app and sign in, then try again.")
        return ip
    return value


HOST = resolve_host(os.environ.get("AGENT_HOST", "127.0.0.1"))


def _authorized(handler, body):
    if not TOKEN:
        return True  # no token set -> local-only, unauthenticated (dev mode)
    header = handler.headers.get("Authorization", "")
    if header.startswith("Bearer ") and header[7:] == TOKEN:
        return True
    return body.get("token") == TOKEN


def as_text(payload):
    """Flatten a response to one speakable line for Shortcuts / Siri."""
    status = payload.get("status")
    if status == "ok":
        return str(payload.get("result", "Done."))
    if status == "no_action":
        return str(payload.get("message", "Nothing to do."))
    return "Error: " + str(payload.get("error", "something went wrong."))


class Handler(BaseHTTPRequestHandler):
    # Set per-request; when true, responses are plain text instead of JSON.
    text_mode = False

    def _send(self, code, payload):
        if self.text_mode:
            data = as_text(payload).encode()
            ctype = "text/plain; charset=utf-8"
        else:
            data = json.dumps(payload).encode()
            ctype = "application/json"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        print("[kernel]", self.address_string(), fmt % args)

    def do_GET(self):
        self.text_mode = False  # reset: one instance serves a keep-alive connection
        self.path = urllib.parse.urlparse(self.path).path
        if self.path == "/health":
            return self._send(200, {"ok": True})
        if self.path == "/capabilities":
            listing = {
                name: {"description": spec["description"],
                       "parameters": list(spec["parameters"].keys())}
                for name, spec in CAPABILITIES.items()
            }
            return self._send(200, {"capabilities": listing})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/run":
            return self._send(404, {"error": "not found"})

        query = urllib.parse.parse_qs(parsed.query)
        self.text_mode = (
            query.get("format", [""])[0].lower() == "text"
            or "text/plain" in self.headers.get("Accept", "")
        )
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or "{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "invalid JSON body"})

        if not _authorized(self, body):
            return self._send(401, {"error": "unauthorized"})

        if str(body.get("format", "")).lower() == "text":
            self.text_mode = True

        query = (body.get("query") or "").strip()
        if not query:
            return self._send(400, {"error": "missing 'query'"})

        try:
            capability, params, message = resolve(query)
        except Exception as e:  # LLM / network failure
            return self._send(502, {"status": "error", "error": f"resolver: {e}"})

        if capability is None:
            # Nothing to run; relay the model's message (e.g. clarification).
            return self._send(200, {"status": "no_action", "message": message})

        try:
            result = execute(capability, params)
        except CapabilityError as e:
            return self._send(200, {"status": "error", "capability": capability,
                                    "params": params, "error": str(e)})
        except Exception as e:
            return self._send(500, {"status": "error", "capability": capability,
                                    "error": f"unexpected: {e}"})

        return self._send(200, {"status": "ok", "capability": capability,
                                "params": params, "result": result})


def main():
    if not TOKEN:
        print("WARNING: AGENT_TOKEN not set — running unauthenticated. "
              "Only safe on 127.0.0.1.")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[kernel] listening on http://{HOST}:{PORT}")
    if HOST == "0.0.0.0":
        tip = tailnet_ip()
        print("[kernel] WARNING: bound to every interface, including untrusted "
              "Wi-Fi. Prefer AGENT_HOST=tailscale"
              + (f" (this Mac is {tip})" if tip else "."))
    elif HOST not in ("127.0.0.1", "localhost"):
        print(f"[kernel] iPhone endpoint: http://{HOST}:{PORT}/run?format=text")
    print(f"[kernel] {len(CAPABILITIES)} capabilities loaded: "
          f"{', '.join(CAPABILITIES)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[kernel] shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()