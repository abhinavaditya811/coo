"""
spotify_sync.py — pull your Spotify playlists into spotify_playlists.json.

Run this occasionally (not per request):

    export SPOTIFY_CLIENT_ID=your-client-id
    python3 spotify_sync.py

The first run opens a browser for one-time authorization; the refresh token is
saved to ~/.coo/spotify_tokens.json (mode 0600) so later runs are silent.

Deliberately separate from capabilities.py: the executor stays offline, stdlib-
only, and fast, and no network call sits in the /run hot path. Uses the
Authorization Code + PKCE flow, so there is no client secret to store.

Setup (one time):
  1. https://developer.spotify.com/dashboard -> Create app
  2. Add redirect URI exactly: http://127.0.0.1:8888/callback
     (Spotify allows http only for loopback IPs -- "localhost" is rejected.)
  3. Copy the Client ID into SPOTIFY_CLIENT_ID. No secret needed with PKCE.
"""

import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from capabilities import PLAYLISTS_PATH

CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID")
REDIRECT_URI = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
SCOPES = "playlist-read-private playlist-read-collaborative"
TOKENS_PATH = os.path.expanduser(
    os.environ.get("SPOTIFY_TOKENS", "~/.coo/spotify_tokens.json"))

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"


class SyncError(Exception):
    """Anything that should stop the sync with a readable message."""


# --- token storage -----------------------------------------------------------

def _load_tokens():
    try:
        with open(TOKENS_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_tokens(tokens):
    os.makedirs(os.path.dirname(TOKENS_PATH), exist_ok=True)
    # Write 0600 from the start; the refresh token is a long-lived credential.
    fd = os.open(TOKENS_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tokens, f, indent=2)


# --- OAuth (Authorization Code + PKCE) ---------------------------------------

def _pkce_pair():
    verifier = base64.urlsafe_b64encode(os.urandom(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def _post_form(fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise SyncError(f"Token request failed ({e.code}): {e.read().decode()[:200]}")
    except urllib.error.URLError as e:
        raise SyncError(f"Could not reach Spotify: {e.reason}")


def _catch_callback(state):
    """Serve exactly one request on the redirect URI and return the auth code."""
    parts = urllib.parse.urlparse(REDIRECT_URI)
    captured = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            captured.update({k: v[0] for k, v in q.items()})
            ok = "code" in captured and captured.get("state") == state
            body = (b"<h2>Authorized. You can close this tab.</h2>" if ok
                    else b"<h2>Authorization failed. Check the terminal.</h2>")
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer((parts.hostname, parts.port or 80), Handler)
    server.timeout = 180
    server.handle_request()
    server.server_close()

    if captured.get("error"):
        raise SyncError(f"Spotify denied authorization: {captured['error']}")
    if not captured.get("code"):
        raise SyncError("No authorization code received (timed out?).")
    if captured.get("state") != state:
        raise SyncError("State mismatch — aborting.")
    return captured["code"]


def _authorize():
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
    })
    print("Opening your browser to authorize Coo with Spotify...")
    print(f"If it doesn't open, visit:\n{url}\n")
    webbrowser.open(url)

    code = _catch_callback(state)
    tokens = _post_form({
        "client_id": CLIENT_ID,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    })
    _save_tokens(tokens)
    return tokens["access_token"]


def _access_token():
    """Refresh silently if we can; otherwise run the browser flow once."""
    tokens = _load_tokens()
    refresh = tokens.get("refresh_token")
    if not refresh:
        return _authorize()
    try:
        fresh = _post_form({
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        })
    except SyncError as e:
        print(f"Refresh failed ({e}); re-authorizing.", file=sys.stderr)
        return _authorize()
    # Spotify may or may not return a new refresh token; keep the old one if not.
    fresh.setdefault("refresh_token", refresh)
    _save_tokens(fresh)
    return fresh["access_token"]


# --- fetching ----------------------------------------------------------------

def _api_get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise SyncError(f"Spotify API {e.code} for {url}: {e.read().decode()[:200]}")
    except urllib.error.URLError as e:
        raise SyncError(f"Could not reach Spotify: {e.reason}")


def fetch_playlists(token):
    """Every playlist you own or follow, as {name: uri}. Follows pagination."""
    found, url = {}, f"{API}/me/playlists?limit=50"
    while url:
        page = _api_get(url, token)
        for item in page.get("items") or []:
            if not item:
                continue  # Spotify occasionally returns nulls in this list
            name, uri = (item.get("name") or "").strip(), item.get("uri")
            if not name or not uri:
                continue
            if name.lower() in {k.lower() for k in found}:
                print(f"  ! duplicate name {name!r} — keeping the first", file=sys.stderr)
                continue
            found[name] = uri
        url = page.get("next")
    return found


def main():
    if not CLIENT_ID:
        sys.exit("SPOTIFY_CLIENT_ID is not set. See the setup notes at the top "
                 "of this file.")

    token = _access_token()
    fetched = fetch_playlists(token)
    print(f"Fetched {len(fetched)} playlists from your profile.")

    try:
        with open(PLAYLISTS_PATH) as f:
            existing = json.load(f)
    except (OSError, json.JSONDecodeError):
        existing = {}

    # Liked Songs isn't a playlist in the API, but the desktop app plays this URI.
    merged = {"Liked Songs": "spotify:collection:tracks"}
    # Hand-added entries survive a sync unless Spotify supplies the same name.
    manual = {k: v for k, v in existing.items()
              if k.lower() not in {n.lower() for n in fetched}
              and not str(v).endswith("REPLACE_ME")
              and k.lower() != "liked songs"}
    merged.update(manual)
    merged.update(fetched)

    with open(PLAYLISTS_PATH, "w") as f:
        json.dump(dict(sorted(merged.items(), key=lambda kv: kv[0].lower())),
                  f, indent=2)
        f.write("\n")

    added = sorted(set(fetched) - set(existing))
    print(f"Wrote {len(merged)} playlists to {PLAYLISTS_PATH}"
          + (f" ({len(manual)} kept from your manual edits)" if manual else ""))
    if added:
        print("New: " + ", ".join(added[:10]) + ("..." if len(added) > 10 else ""))


if __name__ == "__main__":
    try:
        main()
    except SyncError as e:
        sys.exit(f"Sync failed: {e}")
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
