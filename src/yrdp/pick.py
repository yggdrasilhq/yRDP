"""The disposable half — a thin view client for the chooser.

**libyggterm's two-tier doctrine, applied:** *"the view client is disposable;
the daemon is durable."*  Everything expensive to build — the control endpoint,
the reachability cache, the session machinery — lives in [`daemon`] and outlives
any one invocation.  What is left here is the one job a daemon structurally
cannot do.

**Why a client survives at all.**  An OSC surface announcement travels the
terminal byte stream of the session it belongs to, so only a process running
*inside that PTY* can emit one.  The daemon decides; the client speaks.  That is
the whole division of labour, and it is why this file imports neither `session`
nor `view`: a chooser nobody has clicked yet has no business loading an RDP
client.

The startup cost this removes was measured, not assumed:

    interpreter + imports      ->  one TCP connect to a daemon already up
    bind a fresh control port  ->  the port is already there, and already
                                   forwarded by the GUI's `ssh -L`
    probe every guest cold     ->  a cache kept warm in the background
"""

from __future__ import annotations

import base64
import json
import os
import socket
import sys
import time
from urllib.parse import urlparse

from . import daemon

#: The viewport pane — the chooser proper — and the same list offered as a rail
#: panel.  Both are declared: the rail is useful on its own, and a rail that
#: renders while the viewport does not is the bisect that separates "my declare
#: failed" from "viewport placement failed".
PANE = "targets"
RAIL_PANE = "targets-rail"

#: Re-declare cadence.  The GUI expires a contribution that stops speaking, the
#: same way it expires a surface, so this is liveness rather than politeness.
DECLARE_SECONDS = 4.0


def _osc(verb: str, action: str, payload: dict) -> None:
    blob = base64.b64encode(json.dumps(payload).encode()).decode()
    sys.stdout.write(f"\033]7717;{verb};{action};{blob}\007")
    sys.stdout.flush()


def in_yggterm() -> bool:
    """A yggterm-owned PTY exports this; its absence means nobody will listen."""
    return bool(os.environ.get("YGGTERM_SESSION_ID"))


def _get(control: str, path: str, timeout: float = 5.0) -> dict:
    """One GET against the daemon.

    Hand-rolled for the same reason the daemon's server is: `http.server` cost
    ~240 ms to import on this interpreter, measured — more than everything else
    combined — and `urllib` is not much better. This is a socket and a split.
    """
    u = urlparse(control)
    buf = b""
    try:
        with socket.create_connection(
            (u.hostname or "127.0.0.1", u.port or 80), timeout=timeout
        ) as s:
            s.sendall(
                f"GET {path} HTTP/1.1\r\nHost: {u.hostname}\r\n"
                f"Connection: close\r\n\r\n".encode()
            )
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except OSError:
        return {}
    _, _, body = buf.partition(b"\r\n\r\n")
    try:
        return json.loads(body or b"{}")
    except ValueError:
        return {}


def run(*, quality: int, compression: int) -> int:
    """Show the chooser, then stay as the mouth of whatever gets picked.

    This blocks in the foreground for as long as the surface should live,
    because the heartbeat that keeps a surface from being reaped has to come
    from this PTY. It is doing almost nothing while it does so — one poll and
    one write every four seconds — which is exactly the point of the split.
    """
    if not in_yggterm():
        print(
            "[yrdp] pick is a yggterm surface and there is no YGGTERM_SESSION_ID here. "
            "Name a target instead: `yrdp view --target <name>`.",
            file=sys.stderr,
        )
        return 2

    try:
        control = daemon.ensure()
    except RuntimeError as exc:
        print(f"[yrdp] {exc}", file=sys.stderr)
        return 1

    session_id = os.environ.get("YGGTERM_SESSION_ID", "")
    declare = {
        "session": session_id,
        "control": control,
        "app_name": "yRDP",
        # A placeholder, NOT a stamp. Computing the real one means building the
        # schema, and the FIRST declare is the only thing the operator is
        # waiting on — the GUI fetches the pane straight afterwards anyway.
        "document_version": "boot",
        "panes": [
            {"id": PANE, "icon": "🖥", "title": "yRDP", "placement": "viewport"},
            {"id": RAIL_PANE, "icon": "🖥", "title": "yRDP"},
        ],
    }
    _osc("sidebar", "declare", declare)
    print(f"[yrdp] choose a target in the viewport (daemon {control})", file=sys.stderr)

    #: Set once the daemon reports a surface. From then on this client's job is
    #: the surface's heartbeat rather than the contribution's.
    surface: dict | None = None
    stamp = "boot"

    try:
        while True:
            time.sleep(DECLARE_SECONDS)

            for event in _get(control, f"/events?session={session_id}").get("events", []):
                verb, action = event.get("verb", ""), event.get("action", "")
                payload = event.get("payload") or {}
                if verb == "web-surface" and action == "open":
                    # The choice landed. Retire the chooser BEFORE announcing the
                    # desktop: leaving it declared would leave a viewport pane
                    # competing with the surface about to replace it.
                    _osc("sidebar", "close", {"session": session_id})
                    payload["session"] = session_id
                    surface = payload
                    _osc("web-surface", "open", surface)
                elif verb == "toast":
                    print(f"[yrdp] {payload.get('text', '')}", file=sys.stderr)

            if surface is not None:
                _osc("web-surface", "heartbeat", surface)
                continue

            # Still choosing. Re-declaring IS the liveness signal; carry the
            # daemon's content stamp so a guest that just came up shows up.
            stamp = _get(control, "/ping").get("document_version") or stamp
            declare["document_version"] = stamp
            _osc("sidebar", "declare", declare)
    except KeyboardInterrupt:
        if surface is not None:
            _osc("web-surface", "close", {"session": session_id})
        else:
            _osc("sidebar", "close", {"session": session_id})
        return 130
