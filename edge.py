"""
edge.py — the always-on front door.

Everything that must survive the Mac being asleep lives here: authentication,
pending confirmations, request history, and the dashboard. It owns no
capabilities and no model; it asks the Mac node for both over the tailnet.

Run it on a VPS (AGENT_ROLE=edge) or alongside the node on one Mac
(AGENT_ROLE=all, the default) — the difference is configuration, not code
paths. Even in `all` mode it talks to the node over real HTTP on loopback, so
the deployed path is the one being exercised.
"""

import json
import os
import re
import time
import urllib.parse
from http.server import ThreadingHTTPServer

import history
import macclient
import netutil
import plan
import sessions

PORT = int(os.environ.get("AGENT_PORT", "8765"))
TOKEN = os.environ.get("AGENT_TOKEN")
HOST = netutil.resolve_host(os.environ.get("AGENT_HOST", "127.0.0.1"), "edge")

# Pending confirmations for sensitive capabilities.
PENDING = sessions.PendingStore()
CONFIRM_RE = re.compile(r"^\s*confirm\s+([A-Za-z0-9]{%d})\s*$" % sessions.CODE_LENGTH,
                        re.IGNORECASE)


def _authorized(handler, body):
    if not TOKEN:
        return True  # loopback-only dev mode; serve() refuses this otherwise
    if netutil.token_matches(netutil.bearer_token(handler.headers), TOKEN):
        return True
    return netutil.token_matches(body.get("token"), TOKEN)


def degraded(what="do that"):
    """The honest answer when the Mac isn't there.

    Says explicitly that nothing ran: over SMS the user has no other way to
    tell whether the action happened.
    """
    seen = macclient.last_seen()
    when = ""
    if seen:
        mins = int((time.time() - seen) // 60)
        when = f" (last seen {mins} minute{'s' if mins != 1 else ''} ago)"
    return {
        "status": "unavailable",
        "error": "mac_unreachable",
        "message": (f"Your Mac isn't reachable right now{when}, so I can't "
                    f"{what}. Nothing was run — try again when it's back."),
        "mac": {"reachable": False, "last_seen": seen},
    }


def as_text(payload):
    """Flatten a response to one speakable line for Shortcuts / Siri."""
    if payload.get("status") == "ok":
        return str(payload.get("result", "Done."))
    if payload.get("message"):
        return str(payload["message"])
    return "Error: " + str(payload.get("error", "something went wrong."))


class Handler(netutil.JSONHandler):
    log_prefix = "[edge]"

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

    def _authorized_get(self, params):
        """Browsers can't set headers on a plain navigation, so ?token= is
        accepted here. Read-only, and still refused without the token."""
        if not TOKEN:
            return True
        if netutil.token_matches(netutil.bearer_token(self.headers), TOKEN):
            return True
        return netutil.token_matches(params.get("token", [None])[0], TOKEN)

    def do_GET(self):
        self.text_mode = False  # reset: one instance serves a keep-alive connection
        parsed = urllib.parse.urlparse(self.path)
        self.path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if self.path == "/health":
            payload = {"ok": True}
            if self._authorized_get(params):
                # Uptime detail only for authenticated callers: otherwise this
                # publishes "their Mac has been offline for hours" to anyone.
                payload["mac"] = {"reachable": macclient.reachable(),
                                  "last_seen": macclient.last_seen()}
            return self._send(200, payload)

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
                                    "capabilities": history.seen_capabilities()})
        if self.path == "/capabilities":
            m = macclient.manifest() or {}
            return self._send(200, {"capabilities": m.get("capabilities", {}),
                                    "mac_reachable": macclient.reachable()})
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
            # Check reachability BEFORE claiming: claim() is destructive and
            # single-use, so claiming a code we then can't act on would burn it
            # and force the user to re-issue the whole request.
            if not macclient.reachable():
                return self._log(degraded("run that"), 503)
            claimed = PENDING.claim(sender, match.group(1))
            if not claimed:
                return self._log({"status": "no_action", "message":
                    "That code isn't valid — it may have expired, been used "
                    "already, or belong to a different sender."})
            return self._run_plan(claimed)

        try:
            steps, message = macclient.resolve(query)
        except macclient.MacUnreachable:
            return self._log(degraded("work out what you meant"), 503)
        except Exception as e:  # model failure, bad response, bad token
            return self._log({"status": "error", "error": f"resolver: {e}"}, 502)

        if not steps:
            # Nothing to run; relay the model's message (e.g. clarification).
            return self._log({"status": "no_action", "message": message})

        # If any step is sensitive the whole plan waits: you see every action
        # before any of it happens, and one code releases all of them.
        if any(macclient.is_sensitive(s) for s in steps):
            code = PENDING.stash(sender, steps)
            lines = "; ".join(f"{i}. {s.get('describe') or s['capability']}"
                              for i, s in enumerate(steps, start=1))
            what = lines if len(steps) > 1 else (steps[0].get("describe")
                                                 or steps[0]["capability"])
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
        """Execute a plan on the Mac node and record every step."""
        # Strip the annotations before sending; the node only needs the call.
        bare = [{"capability": s["capability"], "params": s.get("params") or {}}
                for s in steps]
        outcomes = plan.run(bare, macclient.execute)

        for o in outcomes:
            history.record(query=getattr(self, "_query", ""),
                           sender=getattr(self, "_sender", None),
                           status=o["status"], capability=o["capability"],
                           params=o["params"], result=o.get("result"),
                           error=o.get("error"),
                           duration_ms=int((time.monotonic() -
                                            getattr(self, "_started", time.monotonic())) * 1000))

        payload = plan.summarize(outcomes)
        if all(o["status"] == "skipped" for o in outcomes):
            payload = degraded("run that")
            return self._send(503, payload)
        return self._send(200, payload)


def serve():
    if not TOKEN and not netutil.is_loopback(HOST):
        raise SystemExit(
            f"edge: refusing to listen on {HOST} without AGENT_TOKEN.\n"
            "Set AGENT_TOKEN, or bind to 127.0.0.1 for local development.")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[edge] listening on http://{HOST}:{PORT}")
    print(f"[edge] mac node at {macclient.MAC_URL}")
    if not TOKEN:
        print("[edge] WARNING: no AGENT_TOKEN — loopback only.")
    elif HOST not in ("127.0.0.1", "localhost"):
        print(f"[edge] client endpoint: http://{HOST}:{PORT}/run?format=text")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[edge] shutting down")
        server.shutdown()


if __name__ == "__main__":
    serve()
