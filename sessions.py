"""
sessions.py — short-lived state the kernel keeps between messages.

Right now that means one thing: pending confirmations for sensitive
capabilities. A sensitive action is never executed by the message that asked
for it; the kernel stashes it here under a single-use code and only runs it
when a follow-up message quotes that code back.

Properties that matter (see DESIGN.md -> "Confirmation flow for sensitive
actions"):
  - single-use   — a claimed code is gone, so a duplicated SMS can't double-send
  - time-boxed   — codes expire, so a stale one can't be replayed later
  - sender-bound — a code is only claimable by the sender it was issued to

Standard library only, in-memory: pending actions are meant to die with the
process. A restart cancelling an unconfirmed send is the safe failure.
"""

import copy
import secrets
import threading
import time

TTL_SECONDS = 120
CODE_LENGTH = 4
# No 0/O/1/I — these get read aloud, typed on a phone, and dictated to Siri.
_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
MAX_PENDING = 64


class PendingStore:
    """Thread-safe: ThreadingHTTPServer handles requests concurrently."""

    def __init__(self, ttl=TTL_SECONDS, clock=time.monotonic):
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._pending = {}  # code -> (sender, plan, expires_at)

    def _purge(self, now):
        for code in [c for c, (_, _, exp) in self._pending.items() if exp <= now]:
            del self._pending[code]

    def _new_code(self):
        while True:
            code = "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH))
            if code not in self._pending:
                return code

    def stash(self, sender, plan):
        """Hold a plan (a list of steps) and return the code that releases it."""
        with self._lock:
            now = self._clock()
            self._purge(now)
            # A sender asking again supersedes their own earlier request, so an
            # abandoned code can't be confirmed by accident later.
            for code in [c for c, (s, _, _) in self._pending.items() if s == sender]:
                del self._pending[code]
            if len(self._pending) >= MAX_PENDING:
                oldest = min(self._pending, key=lambda c: self._pending[c][2])
                del self._pending[oldest]
            code = self._new_code()
            # Deep copy: a pending plan must not change after you were shown it.
            self._pending[code] = (sender, copy.deepcopy(list(plan)), now + self._ttl)
            return code

    def claim(self, sender, code):
        """Consume a code. Returns the stashed plan, or None if the code is
        unknown, expired, already used, or belongs to a different sender."""
        with self._lock:
            now = self._clock()
            self._purge(now)
            entry = self._pending.get(code.upper())
            if not entry:
                return None
            owner, plan, _ = entry
            if owner != sender:
                return None
            del self._pending[code.upper()]
            return plan

    def __len__(self):
        with self._lock:
            self._purge(self._clock())
            return len(self._pending)
