"""
kernel.py — entry point. Starts whichever half (or both) this machine runs.

Coo is two processes:
  - edge.py    the always-on front door: auth, confirmations, history, dashboard
  - macnode.py the Mac: the local model and the capability executor

AGENT_ROLE picks what starts here:
  all   (default) both, on one machine — the edge still reaches the node over
        real HTTP on loopback, so this exercises the deployed path rather than
        a shortcut that only exists in development
  edge  the VPS
  mac   the Mac executor

Keeping this file as the entry point means the installed LaunchAgent keeps
working across the split; only its environment changes.

    AGENT_TOKEN=... MAC_TOKEN=... python3 kernel.py
"""

import os
import sys
import threading


def main():
    role = os.environ.get("AGENT_ROLE", "all").lower().strip()

    if role == "mac":
        import macnode
        return macnode.serve()

    if role == "edge":
        import edge
        return edge.serve()

    if role != "all":
        sys.exit(f"AGENT_ROLE must be one of: all, edge, mac (got {role!r})")

    import edge
    import macnode

    # The node runs in a daemon thread so Ctrl-C on the edge takes both down.
    threading.Thread(target=macnode.serve, daemon=True).start()
    edge.serve()


if __name__ == "__main__":
    main()
