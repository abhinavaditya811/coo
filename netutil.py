"""
netutil.py — transport helpers shared by the edge and the Mac node.

Extracted from kernel.py so both servers bind, authenticate, and read request
bodies the same way. No capability, resolver, or session knowledge lives here.
"""

import hmac
import ipaddress
import json
import re
import subprocess
from http.server import BaseHTTPRequestHandler

# Tailscale hands out addresses from the CGNAT range 100.64.0.0/10.
_TAILNET = ipaddress.ip_network("100.64.0.0/10")

# A request body is a short natural-language query; anything larger is a mistake
# or an attempt to exhaust memory. Content-Length is attacker-controlled, so it
# is never trusted as a read size on its own.
MAX_BODY_BYTES = 64 * 1024


def tailnet_ip():
    """This machine's Tailscale address, or None if it isn't on a tailnet."""
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


def resolve_host(value, what="server"):
    """AGENT_HOST/MAC_HOST=tailscale binds only to the tailnet address, so
    joining an untrusted Wi-Fi never exposes the service the way 0.0.0.0 would.
    Fails closed rather than falling back to something broader."""
    if value.lower() in ("tailscale", "tailnet"):
        ip = tailnet_ip()
        if not ip:
            raise SystemExit(
                f"{what}: host is set to 'tailscale' but this machine has no "
                "Tailscale address.\nStart Tailscale and sign in, then retry.")
        return ip
    return value


def is_loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost",)


def token_matches(candidate, token):
    """Constant-time compare, so a token can't be recovered by timing."""
    if not candidate or not token:
        return False
    return hmac.compare_digest(str(candidate), str(token))


def bearer_token(headers):
    value = headers.get("Authorization", "")
    return value[7:] if value.startswith("Bearer ") else None


class JSONHandler(BaseHTTPRequestHandler):
    """Shared plumbing: JSON replies, a size-capped body reader, quiet logs."""

    log_prefix = "[coo]"

    def _send(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        print(self.log_prefix, self.address_string(), fmt % args)

    def read_json(self):
        """Return the parsed body, or raise ValueError. Refuses oversized bodies
        before allocating, so a bogus Content-Length can't exhaust memory."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("bad Content-Length")
        if length > MAX_BODY_BYTES:
            raise MemoryError(f"body larger than {MAX_BODY_BYTES} bytes")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        return body
