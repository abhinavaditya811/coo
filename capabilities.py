"""
capabilities.py — the capability registry.

Each capability is a pre-defined, parameterized action. The LLM never writes
AppleScript; it only chooses a capability name and fills in parameters. All user/
model-supplied values are passed to osascript as `argv` items (never string-
interpolated into the script text), so there is no script-injection surface.

To add a capability: write an execute function and register it in CAPABILITIES.
"""

import json
import shlex
import subprocess
import tempfile
import os
from urllib.parse import quote


class CapabilityError(Exception):
    """Raised when an executor fails in an expected way."""


# ---------------------------------------------------------------------------
# Low-level runners
# ---------------------------------------------------------------------------

def _osa(script, *args, timeout=30):
    """Run an AppleScript. If the script uses `on run argv`, values are passed
    as argv items — safe from injection. Returns stdout (stripped)."""
    try:
        proc = subprocess.run(
            ["osascript", "-e", script, *[str(a) for a in args]],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise CapabilityError("Script timed out.")
    if proc.returncode != 0:
        raise CapabilityError(proc.stderr.strip() or "osascript failed.")
    return proc.stdout.strip()


def _sh(args, timeout=30):
    """Run a shell command as an argv list (no shell=True, no injection)."""
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CapabilityError("Command timed out.")
    if proc.returncode != 0:
        raise CapabilityError(proc.stderr.strip() or f"{args[0]} failed.")
    return proc.stdout.strip()


# ---------------------------------------------------------------------------
# Executors
# ---------------------------------------------------------------------------

def open_app(app_name):
    _osa('on run argv\ntell application (item 1 of argv) to activate\nend run', app_name)
    return f"Opened {app_name}."


def open_url(url):
    _osa('on run argv\nopen location (item 1 of argv)\nend run', url)
    return f"Opened {url}."


def get_clipboard():
    text = _osa('the clipboard')
    return text if text else "(clipboard is empty)"


def set_clipboard(text):
    _osa('on run argv\nset the clipboard to (item 1 of argv)\nend run', text)
    return "Clipboard updated."


def send_imessage(recipient, body):
    script = (
        'on run argv\n'
        '  set targetBuddy to item 1 of argv\n'
        '  set targetMessage to item 2 of argv\n'
        '  tell application "Messages"\n'
        '    set targetService to 1st account whose service type = iMessage\n'
        '    send targetMessage to participant targetBuddy of targetService\n'
        '  end tell\n'
        'end run'
    )
    _osa(script, recipient, body, timeout=20)
    return f"Message sent to {recipient}."


def speak(text):
    _sh(["say", text], timeout=60)
    return "Spoke the text aloud."


def music_control(action):
    action = (action or "").lower().strip()
    if action in ("play", "pause", "playpause", "toggle"):
        _osa('tell application "Music" to playpause')
        return "Toggled playback."
    if action in ("next", "skip"):
        _osa('tell application "Music" to next track')
        return "Skipped to next track."
    if action in ("previous", "prev", "back"):
        _osa('tell application "Music" to previous track')
        return "Went to previous track."
    if action in ("now_playing", "current", "what"):
        return _osa(
            'tell application "Music"\n'
            '  if player state is playing then\n'
            '    return (name of current track) & " — " & (artist of current track)\n'
            '  else\n'
            '    return "Nothing is playing."\n'
            '  end if\n'
            'end tell'
        )
    raise CapabilityError(f"Unknown music action: {action!r}")


# --- Spotify -----------------------------------------------------------------
# Spotify's AppleScript dictionary can only play a *URI*, not search by name, so
# friendly playlist names are mapped to URIs in a JSON file. Point
# SPOTIFY_PLAYLISTS at your own, or edit spotify_playlists.json next to this file.
#   {"all": "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M", ...}
# Get a URI from the Spotify app: right-click a playlist -> Share -> Copy Spotify URI.

PLAYLISTS_PATH = os.environ.get(
    "SPOTIFY_PLAYLISTS",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "spotify_playlists.json"),
)


def _playlists():
    """Load the name -> URI map. Missing file is not an error (no playlists)."""
    try:
        with open(PLAYLISTS_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        raise CapabilityError(f"Could not read {PLAYLISTS_PATH}: {e}")
    if not isinstance(data, dict):
        raise CapabilityError(f"{PLAYLISTS_PATH} must be a JSON object of name -> URI.")
    return {str(k).lower().strip(): str(v) for k, v in data.items()}


def _resolve_playlist(name):
    """Match a spoken playlist name to a URI: exact, then unique substring."""
    playlists = _playlists()
    if not playlists:
        raise CapabilityError(
            f"No playlists configured. Add them to {PLAYLISTS_PATH} as "
            '{"name": "spotify:playlist:..."}.'
        )
    key = (name or "").lower().strip()
    if key in playlists:
        return playlists[key]
    hits = [k for k in playlists if key and key in k]
    if len(hits) == 1:
        return playlists[hits[0]]
    known = ", ".join(sorted(playlists))
    if len(hits) > 1:
        raise CapabilityError(f"{name!r} matches several playlists: {', '.join(sorted(hits))}.")
    raise CapabilityError(f"No playlist named {name!r}. Known: {known}.")


def playlist_names():
    """Known playlist names, for constraining the resolver's choices. A model
    that can only pick from real names cannot invent a placeholder."""
    try:
        return sorted(_playlists())
    except CapabilityError:
        return []


def spotify_control(action, playlist=None):
    action = (action or "").lower().strip()

    if action in ("play_playlist", "playlist"):
        if not playlist:
            raise CapabilityError("Which playlist? Pass playlist=<name>.")
        uri = _resolve_playlist(playlist)
        # `play track <uri>` is how Spotify's dictionary starts a playlist too.
        _osa('on run argv\ntell application "Spotify" to play track (item 1 of argv)\nend run', uri)
        return f"Playing {playlist} on Spotify."
    if action in ("play", "pause", "playpause", "toggle"):
        _osa('tell application "Spotify" to playpause')
        return "Toggled Spotify playback."
    if action in ("next", "skip"):
        _osa('tell application "Spotify" to next track')
        return "Skipped to next track on Spotify."
    if action in ("previous", "prev", "back"):
        _osa('tell application "Spotify" to previous track')
        return "Went to previous track on Spotify."
    if action in ("now_playing", "current", "what"):
        return _osa(
            'tell application "Spotify"\n'
            '  if player state is playing then\n'
            '    return (name of current track) & " \u2014 " & (artist of current track)\n'
            '  else\n'
            '    return "Nothing is playing on Spotify."\n'
            '  end if\n'
            'end tell'
        )
    raise CapabilityError(f"Unknown Spotify action: {action!r}")


def get_directions(destination, origin=None):
    daddr = quote(destination)
    maps_url = f"maps://?daddr={daddr}"
    if origin:
        maps_url += f"&saddr={quote(origin)}"
    _osa('on run argv\nopen location (item 1 of argv)\nend run', maps_url)
    web = f"https://www.google.com/maps/dir/?api=1&destination={daddr}"
    if origin:
        web += f"&origin={quote(origin)}"
    return f"Opened Maps to {destination}. Shareable link: {web}"


def system_status():
    parts = []
    try:
        parts.append("Front app: " + _osa(
            'tell application "System Events" to get name of first process whose frontmost is true'))
    except CapabilityError:
        pass
    try:
        parts.append("Volume: " + _osa('output volume of (get volume settings)') + "%")
    except CapabilityError:
        pass
    try:
        batt = _sh(["pmset", "-g", "batt"])
        line = next((l for l in batt.splitlines() if "%" in l), "")
        if line:
            pct = line.split(";")[0].split("\t")[-1].strip()
            state = "charging" if "AC Power" in batt else "on battery"
            parts.append(f"Battery: {pct} ({state})")
    except (CapabilityError, StopIteration, IndexError):
        pass
    try:
        wifi = _sh(["networksetup", "-getairportnetwork", "en0"])
        if ":" in wifi:
            parts.append("Wi-Fi: " + wifi.split(":", 1)[1].strip())
    except CapabilityError:
        pass
    return "\n".join(parts) if parts else "No status available."


def run_shortcut(name, input=None):
    with tempfile.NamedTemporaryFile("w", suffix=".out", delete=False) as out:
        out_path = out.name
    cmd = ["shortcuts", "run", name, "--output-path", out_path]
    in_path = None
    try:
        if input:
            with tempfile.NamedTemporaryFile("w", suffix=".in", delete=False) as inf:
                inf.write(input)
                in_path = inf.name
            cmd += ["--input-path", in_path]
        _sh(cmd, timeout=120)
        try:
            with open(out_path) as f:
                result = f.read().strip()
        except OSError:
            result = ""
        return result or f"Ran shortcut '{name}'."
    finally:
        for p in (out_path, in_path):
            if p and os.path.exists(p):
                os.unlink(p)


# ---------------------------------------------------------------------------
# Registry — this is what the kernel and the LLM see.
# Parameter keys ending in "?" are optional.
# ---------------------------------------------------------------------------

CAPABILITIES = {
    "open_app": {
        "description": "Open or activate a macOS application by name.",
        "parameters": {"app_name": "Application name, e.g. 'Safari' or 'Notes'."},
        "execute": lambda p: open_app(p["app_name"]),
    },
    "open_url": {
        "description": "Open a web URL in the default browser.",
        "parameters": {"url": "A full URL including https://."},
        "execute": lambda p: open_url(p["url"]),
    },
    "get_clipboard": {
        "description": "Read the current contents of the Mac clipboard and return the text.",
        "parameters": {},
        "execute": lambda p: get_clipboard(),
    },
    "set_clipboard": {
        "description": "Replace the Mac clipboard contents with the given text.",
        "parameters": {"text": "The text to copy to the clipboard."},
        "execute": lambda p: set_clipboard(p["text"]),
    },
    "send_imessage": {
        "description": "Send an iMessage to a phone number or Apple ID email.",
        "parameters": {
            "recipient": "Phone number (with country code) or Apple ID email.",
            "body": "The message text to send.",
        },
        "execute": lambda p: send_imessage(p["recipient"], p["body"]),
        # Contacts another person as you — never fires from a single message.
        "sensitive": True,
        "confirm": lambda p: f'iMessage {p.get("recipient")}: "{p.get("body")}"',
    },
    "speak": {
        "description": "Speak the given text aloud on the Mac using text-to-speech.",
        "parameters": {"text": "The text to speak."},
        "execute": lambda p: speak(p["text"]),
    },
    "music_control": {
        "description": "Control the Apple Music app (Apple's Music.app, NOT Spotify): play/pause, next, previous, or report the now-playing track.",
        "parameters": {"action": "One of: playpause, next, previous, now_playing."},
        "choices": {"action": ["playpause", "next", "previous", "now_playing"]},
        "execute": lambda p: music_control(p["action"]),
    },
    "spotify_control": {
        "description": (
            "Control the Spotify app: start one of the user's saved playlists by name, "
            "play/pause, next, previous, or report the now-playing track. Use this for "
            "any request that mentions Spotify."
        ),
        "parameters": {
            "action": "One of: play_playlist, playpause, next, previous, now_playing.",
            "playlist?": "Playlist name; required when action is play_playlist.",
        },
        # Constrains the model to real values instead of invented placeholders.
        "choices": {
            "action": ["play_playlist", "playpause", "next", "previous", "now_playing"],
            "playlist": playlist_names,
        },
        "execute": lambda p: spotify_control(p["action"], p.get("playlist")),
    },
    "get_directions": {
        "description": "Open Maps with directions to a destination and return a shareable maps link.",
        "parameters": {
            "destination": "Where to go (address or place name).",
            "origin?": "Optional starting point; defaults to current location.",
        },
        "execute": lambda p: get_directions(p["destination"], p.get("origin")),
    },
    "system_status": {
        "description": "Report the Mac's status: front app, volume, battery, and Wi-Fi network.",
        "parameters": {},
        "execute": lambda p: system_status(),
    },
    "run_shortcut": {
        "description": "Run a macOS Shortcut by name, optionally passing text input, and return its output.",
        "parameters": {
            "name": "The exact name of the Shortcut.",
            "input?": "Optional text input to pass to the shortcut.",
        },
        "execute": lambda p: run_shortcut(p["name"], p.get("input")),
    },
}


def execute(capability, params):
    """Run a capability by name with a params dict. Returns a result string."""
    spec = CAPABILITIES.get(capability)
    if not spec:
        raise CapabilityError(f"Unknown capability: {capability!r}")
    return spec["execute"](params or {})


def choices_for(capability, param):
    """Allowed values for a parameter, or None if it is free-form. A value may
    be a callable so the list can reflect current state (e.g. playlists)."""
    opts = (CAPABILITIES.get(capability) or {}).get("choices", {}).get(param)
    if callable(opts):
        opts = opts()
    return list(opts) if opts else None


def is_sensitive(capability):
    """Sensitive capabilities need a confirmation step before they execute."""
    return bool(CAPABILITIES.get(capability, {}).get("sensitive"))


def describe(capability, params, limit=90):
    """One short line echoing exactly what is about to happen, so the user sees
    the real parameters before approving. Kept short for SMS."""
    spec = CAPABILITIES.get(capability) or {}
    params = params or {}
    try:
        text = spec["confirm"](params)
    except (KeyError, TypeError):
        args = ", ".join(f"{k}={v}" for k, v in params.items())
        text = f"{capability}" + (f" ({args})" if args else "")
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "\u2026"


# ---------------------------------------------------------------------------
# Fallback parser (used when no LLM key is configured) so you can test the
# executor directly, e.g.:  open_app app_name=Safari
#                           send_imessage recipient=+15551234567 body="on my way"
# ---------------------------------------------------------------------------

def parse_command(text):
    """Parse `capability key=value key="two words"` into (name, params). Returns
    (None, None) if the first token isn't a known capability."""
    try:
        tokens = shlex.split(text)
    except ValueError:
        return None, None
    if not tokens or tokens[0] not in CAPABILITIES:
        return None, None
    name, params = tokens[0], {}
    for tok in tokens[1:]:
        if "=" in tok:
            k, v = tok.split("=", 1)
            params[k] = v
    return name, params