"""
macnode.py — the Mac-side node: resolve intent, execute one capability.

This is the half of Coo that must run on the Mac, because both halves need the
Mac: the local model lives here, and every capability drives a macOS app.

It is deliberately dumb about everything else. No sessions, no history, no
dashboard, no confirmation gate — those belong to the edge, which is the part
that stays up when this machine sleeps. Nothing here imports `plan`, `sessions`
or `history`.

Binds tailnet-only by default (MAC_HOST=tailscale) and authenticates with
MAC_TOKEN, which is a *different* secret from the edge's AGENT_TOKEN: a
compromised edge should not also hand over this machine's credential.

    MAC_TOKEN=... MAC_HOST=tailscale python3 macnode.py
"""

import hashlib
import json
import os
from http.server import ThreadingHTTPServer

import netutil
import resolver
from capabilities import CAPABILITIES, CapabilityError, describe, execute, is_sensitive

PORT = int(os.environ.get("MAC_PORT", "8766"))
HOST = netutil.resolve_host(os.environ.get("MAC_HOST", "127.0.0.1"), "macnode")
TOKEN = os.environ.get("MAC_TOKEN")

# Including the playlist enum would publish the user's music library to whatever
# holds the manifest — and the edge never resolves, so it never needs it.
INCLUDE_CHOICES = os.environ.get("MAC_MANIFEST_CHOICES") == "1"

_manifest_cache = None


def manifest():
    """Capability metadata the edge can hold: names, params, sensitivity.

    `version` lets the edge cheaply notice the registry changed. Deliberately
    excludes the `execute`/`confirm` lambdas (not serialisable) and, by default,
    dynamic `choices`.
    """
    global _manifest_cache
    if _manifest_cache is None:
        caps = {}
        for name, spec in CAPABILITIES.items():
            entry = {
                "description": spec["description"],
                "parameters": dict(spec["parameters"]),
                "sensitive": bool(spec.get("sensitive")),
            }
            if INCLUDE_CHOICES:
                from capabilities import choices_for
                opts = {p.rstrip("?"): choices_for(name, p.rstrip("?"))
                        for p in spec["parameters"]}
                entry["choices"] = {k: v for k, v in opts.items() if v}
            caps[name] = entry
        blob = json.dumps(caps, sort_keys=True, separators=(",", ":"))
        _manifest_cache = {
            "version": "sha256:" + hashlib.sha256(blob.encode()).hexdigest()[:16],
            "capabilities": caps,
        }
    return _manifest_cache


def annotate(steps):
    """Attach `sensitive` and a human `describe` to each step.

    The edge gates on these, and it cannot compute them itself: `describe` is
    built from a `confirm` lambda in the registry, which cannot cross the wire.
    Rendering it here, where the plan and the registry are both in hand, is what
    lets the edge stay free of capability knowledge entirely.
    """
    return [{
        "capability": s["capability"],
        "params": s["params"],
        "sensitive": is_sensitive(s["capability"]),
        "describe": describe(s["capability"], s["params"]),
    } for s in steps]


class Handler(netutil.JSONHandler):
    log_prefix = "[macnode]"

    def _authorized(self):
        if not TOKEN:
            return True  # loopback-only dev mode; serve() refuses this otherwise
        return netutil.token_matches(netutil.bearer_token(self.headers), TOKEN)

    def _body(self):
        try:
            return self.read_json(), None
        except MemoryError as e:
            return None, (413, {"error": str(e)})
        except ValueError:
            return None, (400, {"error": "invalid JSON body"})

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            # Cheap by design: never probe the model here.
            return self._send(200, {"ok": True,
                                    "manifest_version": manifest()["version"]})
        if path == "/manifest":
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            return self._send(200, manifest())
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/resolve", "/execute"):
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})

        body, err = self._body()
        if err:
            return self._send(*err)

        if path == "/resolve":
            query = (body.get("query") or "").strip()
            if not query:
                return self._send(400, {"error": "missing 'query'"})
            try:
                steps, message = resolver.resolve(query)
            except Exception as e:
                return self._send(502, {"error": f"resolver: {e}"})
            return self._send(200, {
                "steps": annotate(steps) if steps else [],
                "message": message,
                "manifest_version": manifest()["version"],
            })

        capability = body.get("capability")
        if capability not in CAPABILITIES:
            return self._send(404, {"error": f"unknown capability: {capability!r}"})
        try:
            result = execute(capability, body.get("params") or {})
        except CapabilityError as e:
            # A capability failing is a normal outcome, not a transport problem.
            # Keeping it at 200 lets the edge treat any non-200 as "Mac gone".
            return self._send(200, {"status": "error", "error": str(e)})
        except Exception as e:
            return self._send(200, {"status": "error", "error": f"unexpected: {e}"})
        return self._send(200, {"status": "ok", "result": result})


def serve():
    if not TOKEN and not netutil.is_loopback(HOST):
        raise SystemExit(
            f"macnode: refusing to listen on {HOST} without MAC_TOKEN.\n"
            "Set MAC_TOKEN, or bind to 127.0.0.1 for local development.")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[macnode] listening on http://{HOST}:{PORT}")
    print(f"[macnode] {len(CAPABILITIES)} capabilities, manifest {manifest()['version']}")
    if not TOKEN:
        print("[macnode] WARNING: no MAC_TOKEN — loopback only.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[macnode] shutting down")
        server.shutdown()


if __name__ == "__main__":
    serve()
