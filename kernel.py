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
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import history
import resolver
import sessions
from capabilities import (CAPABILITIES, execute, CapabilityError, describe,
                          is_sensitive)
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

# Pending confirmations for sensitive capabilities.
PENDING = sessions.PendingStore()
CONFIRM_RE = re.compile(r"^\s*confirm\s+([A-Za-z0-9]{%d})\s*$" % sessions.CODE_LENGTH,
                        re.IGNORECASE)


def _authorized(handler, body):
    if not TOKEN:
        return True  # no token set -> local-only, unauthenticated (dev mode)
    header = handler.headers.get("Authorization", "")
    if header.startswith("Bearer ") and header[7:] == TOKEN:
        return True
    return body.get("token") == TOKEN


def as_text(payload):
    """Flatten a response to one speakable line for Shortcuts / Siri."""
    if payload.get("status") == "ok":
        return str(payload.get("result", "Done."))
    if payload.get("message"):
        return str(payload["message"])
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

    def _authorized_get(self, params):
        """Browsers can't set headers on a plain navigation, so ?token= is
        accepted here. Read-only, and still refused without the token."""
        if not TOKEN:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer ") and header[7:] == TOKEN:
            return True
        return params.get("token", [None])[0] == TOKEN

    def do_GET(self):
        self.text_mode = False  # reset: one instance serves a keep-alive connection
        parsed = urllib.parse.urlparse(self.path)
        self.path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if self.path == "/health":
            return self._send(200, {"ok": True})

        # History is sensitive — it is every request you have ever made.
        if self.path in ("/dashboard", "/api/history"):
            if not self._authorized_get(params):
                return self._send(401, {"error": "unauthorized"})

            if self.path == "/dashboard":
                try:
                    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           "dashboard.html")) as f:
                        page = f.read().encode()
                except OSError:
                    return self._send(500, {"error": "dashboard.html missing"})
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                return self.wfile.write(page)

            def one(name, cast=str, default=None):
                raw = params.get(name, [None])[0]
                return cast(raw) if raw else default

            rows, total = history.recent(
                limit=min(one("limit", int, 100), 500),
                offset=one("offset", int, 0),
                status=one("status"), capability=one("capability"),
                search=one("search"))
            return self._send(200, {"rows": rows, "total": total,
                                    "stats": history.stats(),
                                    "capabilities": sorted(CAPABILITIES)})
        if self.path == "/capabilities":
            listing = {
                name: {"description": spec["description"],
                       "parameters": list(spec["parameters"].keys())}
                for name, spec in CAPABILITIES.items()
            }
            return self._send(200, {"capabilities": listing})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        self._started = time.monotonic()
        self._query, self._sender = "", None
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

        # Who is asking. The SMS gateway will pass the sender's number; over
        # HTTP everyone holding the token is the same principal.
        sender = str(body.get("sender") or "token")
        self._query, self._sender = query, sender

        # "confirm 7QF2" releases a previously stashed sensitive action. Checked
        # before the resolver: it is a protocol word, not a capability.
        match = CONFIRM_RE.match(query)
        if match:
            claimed = PENDING.claim(sender, match.group(1))
            if not claimed:
                return self._log({"status": "no_action", "message":
                    "That code isn't valid — it may have expired, been used "
                    "already, or belong to a different sender."})
            return self._run_plan(claimed)

        try:
            steps, message = resolve(query)
        except Exception as e:  # LLM / network failure
            return self._log({"status": "error", "error": f"resolver: {e}"}, 502)

        if not steps:
            # Nothing to run; relay the model's message (e.g. clarification).
            return self._log({"status": "no_action", "message": message})

        # If any step is sensitive the whole plan waits: you see every action
        # before any of it happens, and one code releases all of them.
        if any(is_sensitive(s["capability"]) for s in steps):
            code = PENDING.stash(sender, steps)
            lines = "; ".join(f"{i}. {describe(s['capability'], s['params'])}"
                              for i, s in enumerate(steps, start=1))
            what = lines if len(steps) > 1 else describe(steps[0]["capability"],
                                                         steps[0]["params"])
            return self._log({
                "status": "confirmation_required",
                "steps": steps,
                "code": code,
                "message": f'About to {what}. Reply "confirm {code}" to run.',
            })

        return self._run_plan(steps)

    def _log(self, payload, code=200):
        """Record the outcome, then send it. Bookkeeping never blocks a reply."""
        history.record(
            query=getattr(self, "_query", ""),
            sender=getattr(self, "_sender", None),
            status=payload.get("status", "error"),
            capability=payload.get("capability"),
            params=payload.get("params"),
            result=payload.get("result") or payload.get("message"),
            error=payload.get("error"),
            duration_ms=int((time.monotonic() - getattr(self, "_started", time.monotonic())) * 1000),
        )
        return self._send(code, payload)

    def _run_plan(self, steps):
        """Run steps in order. A step's {{stepN}} is replaced by step N's
        result. Steps are independent unless they reference each other, so a
        failure doesn't stop the rest — except where a later step needed the
        result the failed step never produced."""
        outcomes, results = [], {}

        for i, step in enumerate(steps, start=1):
            capability, params = step["capability"], dict(step["params"])

            missing = sorted({int(n) for v in params.values()
                              if isinstance(v, str)
                              for n in resolver.STEP_REF_RE.findall(v)} - set(results))
            if missing:
                outcomes.append({
                    "step": i, "capability": capability, "params": params,
                    "status": "skipped",
                    "error": f"needed the result of step {missing[0]}, which failed",
                })
                continue

            for key, value in params.items():
                if isinstance(value, str):
                    params[key] = resolver.STEP_REF_RE.sub(
                        lambda m: results[int(m.group(1))], value)

            try:
                result = execute(capability, params)
                results[i] = result
                outcomes.append({"step": i, "capability": capability,
                                 "params": params, "status": "ok", "result": result})
            except CapabilityError as e:
                outcomes.append({"step": i, "capability": capability,
                                 "params": params, "status": "error", "error": str(e)})
            except Exception as e:
                outcomes.append({"step": i, "capability": capability,
                                 "params": params, "status": "error",
                                 "error": f"unexpected: {e}"})

        for o in outcomes:
            history.record(query=getattr(self, "_query", ""),
                           sender=getattr(self, "_sender", None),
                           status=o["status"], capability=o["capability"],
                           params=o["params"], result=o.get("result"),
                           error=o.get("error"),
                           duration_ms=int((time.monotonic() -
                                            getattr(self, "_started", time.monotonic())) * 1000))

        summary = " ".join(
            o["result"] if o["status"] == "ok" else "Error: " + o["error"]
            for o in outcomes)
        failed = [o for o in outcomes if o["status"] != "ok"]
        payload = {
            "status": "ok" if not failed else ("error" if len(failed) == len(outcomes)
                                               else "partial"),
            "steps": outcomes,
            "result": summary,
        }
        if len(outcomes) == 1:
            # Keep the single-step response shape stable for existing clients.
            o = outcomes[0]
            payload["capability"], payload["params"] = o["capability"], o["params"]
            if o["status"] == "ok":
                payload["result"] = o["result"]
            else:
                payload["error"] = o["error"]
        return self._send(200, payload)


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