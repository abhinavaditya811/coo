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

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from capabilities import CAPABILITIES, execute, CapabilityError
from resolver import resolve

HOST = os.environ.get("AGENT_HOST", "127.0.0.1")
PORT = int(os.environ.get("AGENT_PORT", "8765"))
TOKEN = os.environ.get("AGENT_TOKEN")


def _authorized(handler, body):
    if not TOKEN:
        return True  # no token set -> local-only, unauthenticated (dev mode)
    header = handler.headers.get("Authorization", "")
    if header.startswith("Bearer ") and header[7:] == TOKEN:
        return True
    return body.get("token") == TOKEN


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        print("[kernel]", self.address_string(), fmt % args)

    def do_GET(self):
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
        if self.path != "/run":
            return self._send(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or "{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "invalid JSON body"})

        if not _authorized(self, body):
            return self._send(401, {"error": "unauthorized"})

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
    print(f"[kernel] {len(CAPABILITIES)} capabilities loaded: "
          f"{', '.join(CAPABILITIES)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[kernel] shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()