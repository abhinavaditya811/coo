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

from capabilities import CAPABILITIES, parse_command

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

def _params_desc(spec):
    out = []
    for key, desc in spec["parameters"].items():
        optional = key.endswith("?")
        out.append(f"{key.rstrip('?')}{' (optional)' if optional else ''}: {desc}")
    return "; ".join(out) or "none"


def _capability_spec():
    return "\n".join(
        f"- {name}: {spec['description']} | params: {_params_desc(spec)}"
        for name, spec in CAPABILITIES.items()
    )


SYSTEM_PROMPT = (
    "You control a Mac by mapping the user's request to exactly one capability "
    "and its parameters. Choose the single best fit. If nothing fits, use "
    'capability "none" and explain briefly in "message". Only include parameters '
    "that were actually provided; omit optional ones you don't have.\n\n"
    "Capabilities:\n{spec}"
)


# ---------------------------------------------------------------------------
# OpenAI-compatible provider (llama.cpp / Ollama / LM Studio / Groq / ...)
# ---------------------------------------------------------------------------

def _json_schema():
    return {
        "type": "object",
        "properties": {
            "capability": {
                "type": "string",
                "enum": list(CAPABILITIES.keys()) + ["none"],
            },
            "params": {"type": "object"},
            "message": {"type": "string"},
        },
        "required": ["capability", "params"],
        "additionalProperties": False,
    }


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
            props[key] = {"type": "string", "description": pdesc}
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
            return _resolve_anthropic(query)
        except Exception as e:
            return _fallback(query, f"Anthropic call failed: {e}.")

    if provider == "openai":
        try:
            return _resolve_openai(query)
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            return _fallback(
                query,
                f"Local model call failed ({e}). Is a server running at "
                f"{OPENAI_BASE_URL}?",
            )
        except Exception as e:
            return _fallback(query, f"Resolver error: {e}.")

    return _fallback(query, f"Unknown provider {provider!r}.")