# Coo

**Text your Mac home.** Say it once; Coo carries it home.

A tiny kernel that runs on your Mac, takes a natural-language request, maps it to
one **pre-defined capability**, runs the AppleScript/shell for it, and returns the
result over HTTP. The LLM never writes code — it only picks a capability and fills
its parameters, so there's no script-injection surface.

```
iPhone / curl / SMS webhook  ──HTTP──▶  kernel.py
                                          ├─ auth (shared token)
                                          ├─ resolve  (Claude → capability + params)
                                          ├─ execute  (osascript / shell)
                                          └─ JSON result
```

## Files
- `capabilities.py` — the registry: each capability + its injection-safe executor.
- `resolver.py` — natural language → (capability, params) via Claude tool-calling,
  with a no-LLM structured-command fallback.
- `kernel.py` — the HTTP server (auth, resolve, execute, respond).

## Setup

The intent step is provider-agnostic. Pick one:

### A) Local model via llama.cpp (default — no Python deps)
```bash
# Terminal 1: serve your model (llama.cpp exposes an OpenAI-compatible API)
llama-server -m /path/to/qwen2.5-7b-instruct-q4_k_m.gguf -c 8192 --port 8080

# Terminal 2: run the agent (defaults already point at llama.cpp)
export AGENT_TOKEN="pick-a-long-random-secret"
export AGENT_PROVIDER=openai
export OPENAI_BASE_URL=http://localhost:8080/v1   # this is the default
export OPENAI_API_KEY=local                        # llama.cpp ignores the value
export AGENT_MODEL=qwen                             # arbitrary for llama.cpp
python3 kernel.py
```
Reliability comes from JSON-schema structured output, so even a 7B Qwen is
constrained to return a valid `{capability, params}` — nothing to parse-and-pray.

### B) Local model via Ollama
```bash
export AGENT_PROVIDER=openai
export OPENAI_BASE_URL=http://localhost:11434/v1
export OPENAI_API_KEY=ollama
export AGENT_MODEL=qwen3:8b
```

### C) Claude (hosted, native tool-calling)
```bash
pip install -r requirements.txt          # needs the anthropic SDK
export AGENT_PROVIDER=anthropic
export ANTHROPIC_API_KEY="sk-ant-..."
export AGENT_MODEL=claude-haiku-4-5       # optional
```

Then in all cases:
```bash
export AGENT_TOKEN="pick-a-long-random-secret"
python3 kernel.py     # -> listening on http://127.0.0.1:8765
```

If the chosen provider is unreachable, the resolver falls back to the
structured-command syntax (e.g. `open_app app_name=Safari`) so the executor is
never blocked.

## macOS permissions (the #1 gotcha)
The first time a capability drives another app, macOS will prompt for permission.
Grant them under **System Settings → Privacy & Security**:
- **Automation** — allow your terminal (or Python) to control Music, Messages,
  System Events, etc.
- **Accessibility** — needed for some System Events queries.
If a capability silently fails, an ungranted permission is almost always why.
`send_imessage` in particular requires Automation access to Messages, and Messages
scripting varies by macOS version — adjust the AppleScript in `capabilities.py` if
your version rejects it.

## Try it

Natural language (needs the API key):
```bash
curl -s localhost:8765/run \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -d '{"query":"open safari"}'
```

Structured command (works with no key):
```bash
curl -s localhost:8765/run \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -d '{"query":"system_status"}'

curl -s localhost:8765/run \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -d '{"query":"send_imessage recipient=+15551234567 body=\"on my way\""}'
```

List what it can do:
```bash
curl -s localhost:8765/capabilities
```

## Capabilities included
`open_app`, `open_url`, `get_clipboard`, `set_clipboard`, `send_imessage`,
`speak`, `music_control`, `spotify_control`, `get_directions`, `system_status`,
`run_shortcut`.

Add one by writing an executor function and a registry entry in `capabilities.py`.

### Spotify playlists
Spotify's AppleScript dictionary can only play a **URI**, not search by name, so
playlist names are mapped to URIs in `spotify_playlists.json` (override the path
with `SPOTIFY_PLAYLISTS`):

```json
{
  "all": "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M",
  "liked songs": "spotify:collection:tracks"
}
```

Lookup is case-insensitive and accepts an unambiguous partial name, so "workout"
matches "Workout Mix".

`spotify_playlists.json` is git-ignored (it ends up listing your real library);
start from the template with `cp spotify_playlists.example.json spotify_playlists.json`.

**Fill the map automatically** with `spotify_sync.py`, which pulls every playlist
you own or follow from your profile via the Spotify Web API:

```bash
export SPOTIFY_CLIENT_ID=your-client-id
python3 spotify_sync.py
```

One-time setup: create an app at
[developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) and add
the redirect URI `http://127.0.0.1:8888/callback` exactly. Spotify permits plain
`http` only for loopback IPs — `localhost` is rejected. The flow uses PKCE, so
there is no client secret to store; the refresh token lands in
`~/.coo/spotify_tokens.json` (mode 0600) and later syncs run without a browser.

The sync keeps hand-added entries and refreshes any name Spotify also returns, so
re-running it is safe. To skip all of this for a one-off playlist, right-click it
in the Spotify app -> Share -> Copy Spotify URI and paste it into the JSON.

Either way, the map is read at request time, so `spotify_control` itself never
touches the network. Then:

```bash
curl -s localhost:8765/run \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -d '{"query":"play my spotify playlist all"}'
```
`run_shortcut` is the escape hatch: anything you can build in the Shortcuts app
becomes callable without new Python.

## Driving it from your iPhone (later)
This is the "use my Mac through my iPhone" path, no companion app required:
### 1. Join both devices to a tailnet
Install [Tailscale](https://tailscale.com) on the Mac and from the App Store on the
iPhone, and sign both into the **same account**. Find the Mac's address:

```bash
tailscale ip -4        # e.g. 100.101.102.103
```

### 2. Run the kernel on the tailnet
```bash
export AGENT_TOKEN=$(openssl rand -hex 32)
export AGENT_HOST=tailscale       # binds ONLY to the Tailscale address
python3 kernel.py
```

`AGENT_HOST=tailscale` resolves this Mac's `100.x` address itself and binds just
that interface. Prefer it to `0.0.0.0`, which would also expose the kernel to every
coffee-shop Wi-Fi you join. If Tailscale isn't connected the kernel refuses to
start rather than falling back to something broader.

Keep it running across reboots:

```bash
AGENT_TOKEN=$AGENT_TOKEN ./install_launchagent.sh
```

That installs a LaunchAgent (`~/Library/LaunchAgents/com.coo.kernel.plist`, mode
600 since it holds your token) which starts at login, restarts on crash, and logs
to `~/Library/Logs/coo-kernel.log`.

### 3. Build the iPhone Shortcut
In **Shortcuts** → **+** → add these actions in order:

| # | Action | Configuration |
|---|--------|---------------|
| 1 | **Dictate Text** | Language: English. (Or **Ask for Input** to type instead.) |
| 2 | **Get Contents of URL** | URL: `http://100.101.102.103:8765/run?format=text` |
| | | Method: **POST** |
| | | Headers: `Authorization` = `Bearer <your AGENT_TOKEN>` |
| | | Request Body: **JSON**, one field `query` (Text) = the **Dictated Text** variable |
| 3 | **Speak Text** | Input: **Contents of URL** |

Name it something Siri-friendly like **"Ask My Mac"**, then say *"Hey Siri, Ask My
Mac"* and speak your request.

The `?format=text` is what makes this simple: the kernel replies with the bare
result string instead of JSON, so step 3 speaks it directly with no parsing. Drop
the parameter and you get the full JSON (`status`, `capability`, `params`,
`result`) if you'd rather branch on it. `Accept: text/plain` or `"format":"text"`
in the body do the same thing.

### Checks
```bash
# from the Mac
curl -s "http://$(tailscale ip -4):8765/run?format=text" \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -d '{"query":"what is playing on spotify"}'
```
From the iPhone, open `http://<mac-tailscale-ip>:8765/health` in Safari — it should
show `{"ok": true}`. If it doesn't, the tailnet is the problem, not the kernel.

The same endpoint is what the SMS gateway webhook will POST to when you add the
offline channel.

## Security notes
- Binds to `127.0.0.1` by default. Only expose it via Tailscale, never a
  port-forward to the public internet.
- Always set `AGENT_TOKEN` before exposing it beyond localhost.
- The LLM cannot execute arbitrary code — only the capabilities in the registry.
- Consider requiring a confirmation step before adding destructive capabilities.