"""
resolver.py — turn a natural-language query into (capability, params).

Providers (set AGENT_PROVIDER, or leave blank to default to 'openai'):
  - "openai"    : any OpenAI-compatible server — llama.cpp, Ollama, LM Studio,
                  Groq, Together, etc. Uses only the standard library (urllib).
                  Reliability comes from JSON-schema structured output: the model
                  is constrained to emit {capability, params} and nothing else.
  - "anthropic" : Claude via the anthropic SDK, using native tool-calling.

If a provider call fails, we fall back to the structured-command parser so the
executor is always reachable (e.g. `open_app app_name=Safari`).

Environment:
  AGENT_PROVIDER   openai | anthropic         (default: openai)
  OPENAI_BASE_URL  default http://localhost:8080/v1   (llama.cpp's default)
  OPENAI_API_KEY   default "local"            (llama.cpp ignores the value)
  ANTHROPIC_API_KEY
  AGENT_MODEL      model name; for llama.cpp this can be arbitrary
"""

import json
import os
import re
import urllib.error
import urllib.request

from capabilities import CAPABILITIES, choices_for, parse_command

try:
    import anthropic
except ImportError:
    anthropic = None

PROVIDER = os.environ.get("AGENT_PROVIDER", "").lower().strip()
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://localhost:8080/v1")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "local")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = os.environ.get("AGENT_MODEL")


# ---------------------------------------------------------------------------
# Shared: describe the registry for the model
# ---------------------------------------------------------------------------

def _params_desc(spec, name=None):
    out = []
    for key, desc in spec["parameters"].items():
        param = key.rstrip("?")
        opts = choices_for(name, param) if name else None
        if opts:
            desc = f"{desc} Allowed values: {', '.join(repr(o) for o in opts)}."
        out.append(f"{param}{' (optional)' if key.endswith('?') else ''}: {desc}")
    return "; ".join(out) or "none"


def _capability_spec():
    return "\n".join(
        f"- {name}: {spec['description']} | params: {_params_desc(spec, name)}"
        for name, spec in CAPABILITIES.items()
    )


SYSTEM_PROMPT = (
    "You control a Mac by mapping the user's request to exactly one capability "
    "and its parameters. Choose the single best fit.\n\n"
    "Fill each parameter with the user's OWN WORDS, copied from their request. "
    "Never invent a placeholder: values like <name>, your_playlist_name, or "
    "example@email.com are always wrong. If a required value is genuinely absent "
    'from the request, use capability "none" and say what is missing in '
    '"message". Omit optional parameters you weren\'t given.\n\n'
    "Capabilities:\n{spec}\n\n"
    "Examples:\n"
    "request: play my playlist chill on spotify\n"
    '{{"capability":"spotify_control","params":'
    '{{"action":"play_playlist","playlist":"chill"}}}}\n'
    "request: text Sam that I am late\n"
    '{{"capability":"send_imessage","params":'
    '{{"recipient":"Sam","body":"I am late"}}}}\n'
    "request: open notes\n"
    '{{"capability":"open_app","params":{{"app_name":"Notes"}}}}'
)


# ---------------------------------------------------------------------------
# OpenAI-compatible provider (llama.cpp / Ollama / LM Studio / Groq / ...)
# ---------------------------------------------------------------------------

def _param_schema(capability, param, desc):
    node = {"type": "string", "description": desc}
    opts = choices_for(capability, param)
    if opts:
        # An enum makes an invented placeholder structurally impossible.
        node["enum"] = opts
    return node


def _json_schema():
    """One branch per capability, each pinning its own params.

    A single `{"params": {"type": "object"}}` left the model free to invent
    values, which is how placeholders like "<your_playlist_name>" and dropped
    required parameters got through. Naming the properties per capability and
    marking the required ones lets the grammar enforce what the prose only asked
    for."""
    branches = []
    for name, spec in CAPABILITIES.items():
        props, required = {}, []
        for pname, pdesc in spec["parameters"].items():
            key = pname.rstrip("?")
            props[key] = _param_schema(name, key, pdesc)
            if not pname.endswith("?"):
                required.append(key)
        branches.append({
            "type": "object",
            "properties": {
                "capability": {"const": name},
                "params": {"type": "object", "properties": props,
                           "required": required, "additionalProperties": False},
            },
            "required": ["capability", "params"],
            "additionalProperties": False,
        })
    branches.append({
        "type": "object",
        "properties": {"capability": {"const": "none"},
                       "params": {"type": "object"},
                       "message": {"type": "string"}},
        "required": ["capability", "params"],
        "additionalProperties": False,
    })
    return {"anyOf": branches}


def _post(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {OPENAI_API_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def _extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _resolve_openai(query):
    url = OPENAI_BASE_URL.rstrip("/") + "/chat/completions"
    system = SYSTEM_PROMPT.format(spec=_capability_spec())
    base = {
        "model": MODEL or "local-model",
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": query}],
        "temperature": 0,
        "max_tokens": 512,
    }
    # Prefer strict JSON-schema; fall back to json_object for older servers.
    try:
        payload = _post(url, dict(base, response_format={
            "type": "json_schema",
            "json_schema": {"name": "capability_call",
                            "schema": _json_schema(), "strict": True},
        }))
    except urllib.error.HTTPError:
        payload = _post(url, dict(base, response_format={"type": "json_object"}))

    content = payload["choices"][0]["message"]["content"]
    obj = _extract_json(content)

    cap = obj.get("capability")
    params = obj.get("params") or {}
    if cap in (None, "", "none"):
        return None, None, obj.get("message") or "I couldn't map that to anything I can do."
    if cap not in CAPABILITIES:
        return None, None, f"Model chose an unknown capability: {cap!r}."
    return cap, params, None


# ---------------------------------------------------------------------------
# Anthropic provider (native tool-calling)
# ---------------------------------------------------------------------------

def build_tools():
    tools = []
    for name, spec in CAPABILITIES.items():
        props, required = {}, []
        for pname, pdesc in spec["parameters"].items():
            key = pname.rstrip("?")
            props[key] = _param_schema(name, key, pdesc)
            if not pname.endswith("?"):
                required.append(key)
        tools.append({
            "name": name,
            "description": spec["description"],
            "input_schema": {"type": "object", "properties": props, "required": required},
        })
    return tools


def _resolve_anthropic(query):
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=MODEL or "claude-haiku-4-5",
        max_tokens=1024,
        system=SYSTEM_PROMPT.format(spec=_capability_spec()),
        tools=build_tools(),
        messages=[{"role": "user", "content": query}],
    )
    for block in msg.content:
        if block.type == "tool_use":
            return block.name, dict(block.input), None
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    return None, None, (text or "I couldn't map that to anything I can do.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# Values a model emits when it is echoing the schema instead of reading the
# request. Executing one of these is always wrong — better to say so.
_PLACEHOLDER_RE = re.compile(
    r"^<.*>$"                                  # <name>, <your_playlist_name>
    r"|^\{\{?.*\}\}?$"                         # {name}, {{name}}
    r"|your[_ ](name|number|playlist|email|phone|app|text|message)"
    r"|(playlist|app|user|file|recipient)[_ ]name$"
    r"|^(placeholder|example|foo|bar|xxx+|todo|tbd|n/a|none|null)$"
    r"|example\.(com|org)$",
    re.IGNORECASE)


def _placeholders(params):
    return [k for k, v in (params or {}).items()
            if isinstance(v, str) and _PLACEHOLDER_RE.search(v.strip())]


def _validated(capability, params, message):
    """Reject a resolution that filled parameters with schema echoes."""
    if capability is None:
        return capability, params, message
    bad = _placeholders(params)
    if bad:
        names = ", ".join(bad)
        return None, None, (
            f"I couldn't tell what you meant for: {names}. "
            "Please say the exact value.")
    return capability, params, message


def _fallback(query, why):
    name, params = parse_command(query)
    if name:
        return name, params, None
    return None, None, (
        f"{why} You can use a structured command like `open_app app_name=Safari`."
    )


def resolve(query):
    """Return (capability, params, message). If capability is None, show message."""
    provider = PROVIDER or "openai"

    if provider == "anthropic":
        if anthropic is None or not ANTHROPIC_API_KEY:
            return _fallback(query, "Anthropic provider not configured.")
        try:
            return _validated(*_resolve_anthropic(query))
        except Exception as e:
            return _fallback(query, f"Anthropic call failed: {e}.")

    if provider == "openai":
        try:
            return _validated(*_resolve_openai(query))
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            return _fallback(
                query,
                f"Local model call failed ({e}). Is a server running at "
                f"{OPENAI_BASE_URL}?",
            )
        except Exception as e:
            return _fallback(query, f"Resolver error: {e}.")

    return _fallback(query, f"Unknown provider {provider!r}.")