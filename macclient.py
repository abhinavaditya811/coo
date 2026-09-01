"""
macclient.py — the edge's view of the Mac node.

Wraps the four macnode endpoints and, more importantly, owns the question the
edge has to answer constantly: *is the Mac there right now?*

Two failure kinds, kept strictly apart:
  - MacUnreachable — transport: asleep, off the tailnet, refusing connections.
    Subclasses plan.ExecutorUnavailable so a plan aborts its remaining steps
    instead of paying one timeout per step.
  - MacError — the Mac answered, but with an HTTP error (bad token, bad request).
    That is a bug or a misconfiguration, not an outage, and is not retried.

A capability that *fails* is neither: macnode returns HTTP 200 with
status="error" for that, precisely so it can't be mistaken for an outage.
"""

import json
import os
import time
import urllib.error
import urllib.request

import plan

MAC_URL = os.environ.get("MAC_URL", "http://127.0.0.1:8766").rstrip("/")
MAC_TOKEN = os.environ.get("MAC_TOKEN", "")

HEALTH_TIMEOUT = 2       # must stay cheap: called before other work
MANIFEST_TIMEOUT = 5
RESOLVE_TIMEOUT = 75     # must exceed resolver's own 60s model budget, or a
                         # *thinking* Mac gets reported as an *unreachable* one
EXECUTE_TIMEOUT = 150    # run_shortcut alone allows 120s internally
DOWN_COOLDOWN = 5        # skip connecting while the Mac is known-down

MANIFEST_PATH = os.path.expanduser(
    os.environ.get("COO_MANIFEST", "~/.coo/manifest.json"))
MANIFEST_TTL = 300


class MacUnreachable(plan.ExecutorUnavailable):
    """Transport-level failure: the Mac is not answering."""


class MacError(Exception):
    """The Mac answered with an HTTP error."""


_last_ok = None
_down_until = 0.0
_manifest = None
_manifest_at = 0.0


def last_seen():
    return _last_ok


def _mark_ok():
    global _last_ok, _down_until
    _last_ok, _down_until = time.time(), 0.0


def _mark_down():
    global _down_until
    _down_until = time.time() + DOWN_COOLDOWN


def _call(method, path, payload=None, timeout=30):
    global _manifest, _manifest_at
    if time.time() < _down_until:
        # Known down: fail instantly rather than making every step of a plan
        # wait out its own connect timeout.
        raise MacUnreachable("Mac unreachable")

    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if MAC_TOKEN:
        headers["Authorization"] = f"Bearer {MAC_TOKEN}"
    req = urllib.request.Request(f"{MAC_URL}{path}", data=data,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        _mark_ok()  # it answered, so it is up; the request was just wrong
        raise MacError(f"{path} -> {e.code}: {e.read().decode()[:200]}")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        _mark_down()
        raise MacUnreachable(f"Mac unreachable ({getattr(e, 'reason', e)})")
    except json.JSONDecodeError as e:
        _mark_ok()
        raise MacError(f"{path} returned invalid JSON: {e}")

    _mark_ok()
    # Every response carries the manifest version, so the cache is refreshed
    # exactly when it goes stale — no background poller needed.
    version = body.get("manifest_version")
    if version and _manifest and _manifest.get("version") != version:
        _manifest, _manifest_at = None, 0.0
    return body


def health():
    return _call("GET", "/health", timeout=HEALTH_TIMEOUT)


def reachable(max_age=10):
    """True if the Mac answered recently, else probe it."""
    if _last_ok and (time.time() - _last_ok) < max_age:
        return True
    if time.time() < _down_until:
        return False
    try:
        health()
        return True
    except (MacUnreachable, MacError):
        return False


def resolve(query):
    """Return (annotated_steps, message)."""
    body = _call("POST", "/resolve", {"query": query}, timeout=RESOLVE_TIMEOUT)
    return body.get("steps") or [], body.get("message")


def execute(capability, params):
    """Run one step. Returns its result string; raises on failure.

    Signature matches capabilities.execute so it can be passed to plan.run.
    """
    body = _call("POST", "/execute",
                 {"capability": capability, "params": params},
                 timeout=EXECUTE_TIMEOUT)
    if body.get("status") == "ok":
        return body.get("result", "")
    raise RuntimeError(body.get("error") or "capability failed")


def manifest(force=False):
    """Capability metadata, cached in memory and on disk.

    The disk copy is what keeps /dashboard useful while the Mac is down.
    Returns None if we have never successfully fetched one.
    """
    global _manifest, _manifest_at
    fresh = _manifest and (time.time() - _manifest_at) < MANIFEST_TTL
    if fresh and not force:
        return _manifest
    try:
        _manifest, _manifest_at = _call("GET", "/manifest",
                                        timeout=MANIFEST_TIMEOUT), time.time()
        try:
            os.makedirs(os.path.dirname(MANIFEST_PATH), exist_ok=True)
            with open(MANIFEST_PATH, "w") as f:
                json.dump(_manifest, f)
        except OSError:
            pass
        return _manifest
    except (MacUnreachable, MacError):
        if _manifest:
            return _manifest
        try:
            with open(MANIFEST_PATH) as f:
                _manifest = json.load(f)
                return _manifest
        except (OSError, json.JSONDecodeError):
            return None


def is_sensitive(step):
    """Fail closed. An unannotated step, an unknown capability, or no manifest
    at all all count as sensitive — the cost of a needless confirmation is one
    extra message; the cost of skipping a needed one is a sent text."""
    if step.get("sensitive"):
        return True
    m = manifest()
    if not m:
        return True
    entry = m.get("capabilities", {}).get(step.get("capability"))
    if entry is None:
        return True
    return bool(entry.get("sensitive"))
