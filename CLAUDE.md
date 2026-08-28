# CLAUDE.md

Guidance for AI assistants (and humans) working on **Coo**. For the *why* behind
all of this, read `DESIGN.md`.

## What this is
`Coo` runs on a Mac, takes a natural-language request, maps it to **one
pre-defined capability**, runs the AppleScript/shell for it, and returns the result
over HTTP. The iPhone (and later an SMS gateway) is just a client that POSTs a query;
the Mac is where things actually happen.

## Repo layout
- `kernel.py` — HTTP server and the main loop: auth → resolve → execute → respond. Entry point.
- `capabilities.py` — the capability registry plus each injection-safe executor.
- `resolver.py` — natural language → `(capability, params)` via Claude tool-calling, with a no-LLM structured-command fallback.
- `spotify_sync.py` — one-off helper: OAuth (PKCE) to the Spotify Web API to
  populate `spotify_playlists.json`. Never imported by the kernel; keeps the
  network out of the request path.
- `install_launchagent.sh` — installs the LaunchAgent that keeps the kernel
  running across reboots (needed for the iPhone/SMS channels to be reachable).
- `requirements.txt` — only `anthropic`, and only for natural-language mode.
- `DESIGN.md` — architecture, decisions, diagrams.

## Running
The intent step is provider-pluggable and **defaults to a local model** via any
OpenAI-compatible server (llama.cpp at `http://localhost:8080/v1` by default).
```bash
export AGENT_TOKEN="a-long-random-secret"
export AGENT_PROVIDER=openai             # default; points at llama.cpp / Ollama / etc.
# export AGENT_PROVIDER=anthropic        # to use Claude instead (needs the SDK + key)
python3 kernel.py                        # listens on 127.0.0.1:8765
```
See `README.md` for the full llama.cpp / Ollama / Claude configs.

## Testing
This code targets macOS (`osascript` / `shortcuts` only exist there), but the
pure-Python paths are testable anywhere:
```bash
python3 -m py_compile *.py
python3 -c "import capabilities as c; print(c.parse_command('open_app app_name=Safari'))"
```
If the configured provider is unreachable, `resolver.resolve()` falls back to the
structured-command syntax, so the executor and registry can be exercised without a
running model.

## Invariants — do not break these
1. **The LLM never writes or runs code.** It only selects a capability name and
   fills parameters. All execution flows through `capabilities.CAPABILITIES`. Never
   add a path that runs arbitrary model-generated osascript or shell.
2. **Injection safety.** User/model values go to osascript as `argv` items (scripts
   use `on run argv`) or to `subprocess` as an argv list. Never string-interpolate
   values into a script body and never use `shell=True`.
3. **Auth.** Every `/run` request is checked against `AGENT_TOKEN`. Don't add
   unauthenticated mutating endpoints.
4. **Exposure.** Bind to `127.0.0.1` by default; reach it remotely only over a
   Tailscale tailnet. `AGENT_HOST=tailscale` binds solely to the `100.x` address
   and fails closed if the tailnet is down — prefer it to `0.0.0.0`. Never add or
   document a public internet port-forward.

## Adding a capability
1. Write an executor in `capabilities.py` that takes explicit args and returns a
   result string. Use `_osa(...)` for AppleScript (argv-passed) or `_sh(...)` for
   shell (argv list). Raise `CapabilityError` for expected failures.
2. Register it in `CAPABILITIES` with a clear `description` (the LLM reads this to
   choose it), a `parameters` dict (mark optional params with a trailing `?`), and an
   `execute` lambda mapping the params dict to your function.
3. It automatically becomes both an LLM tool and a structured command — no other
   wiring needed.

For anything already possible in the macOS Shortcuts app, prefer `run_shortcut`
over new Python.

Player capabilities are per-app and must stay that way: `music_control` drives
Apple Music, `spotify_control` drives Spotify. Their descriptions say so
explicitly because the resolver picks by description alone — a vague one sends
"play my Spotify playlist" to Apple Music.

## Conventions
- Standard library only in `kernel.py` and `capabilities.py`; `anthropic` is
  confined to `resolver.py`.
- Keep executors small and single-purpose; keep human-facing result strings short
  (they may be delivered over SMS one day, ~160 chars per segment).
- Sensitive or destructive capabilities must gain a confirmation step before they
  execute (see `DESIGN.md` → "Confirmation flow for sensitive actions"). The
  `sensitive` flag is **designed but not yet implemented**; until it is,
  `send_imessage` sends with no confirmation.

## Status
- **Built:** kernel loop + Mac executor (11 capabilities) + pluggable intent
  provider (local llama.cpp/Qwen by default, Claude as fallback).
- **Built:** iPhone Shortcut → HTTP over Tailscale (tailnet-only binding,
  `?format=text` replies, LaunchAgent autostart).
- **Next:** confirmation flow for sensitive actions. NOTE: the `sensitive` flag
  is described in DESIGN.md but is **not implemented** — `send_imessage`
  currently executes immediately.
- **Later:** confirmation flow (designed in DESIGN.md), SMS gateway (offline
  channel), iPhone-side execution.