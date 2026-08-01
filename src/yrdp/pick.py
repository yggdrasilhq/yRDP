"""The chooser — `yrdp` with no target, as a libyggterm document surface.

**This module invents nothing.**  Every mechanism it uses already ships in
yggterm and is documented in `.agents/skills/libyggterm-surfaces/SKILL.md`; the
whole point is that a libyggterm app introduces ITSELF to the GUI rather than
the GUI growing a special case for it.  Concretely:

* the **app registry** (`~/.yggterm/apps/yrdp.json`) puts "New yRDP" in the
  right-click menu, the titlebar `+` and the start page — one derivation
  (`app_launcher_entries`), so those three can never disagree;
* the **document surface** (`OSC 7717 ; sidebar ; declare` with a pane whose
  `placement` is `"viewport"`) renders this chooser as ordinary shell DOM in the
  viewport.  The skill is explicit that this is where a picker belongs:
  *"markdown, dashboards, forms, pickers all belong here."*
* the **control endpoint** serves the schema and takes the click back.

The alternative — an HTML page in a webview, or teaching yggterm what an "RDP
target" is — would have been two more moving parts and a violation of the one
rule the platform actually enforces: *yggterm provides the surface INTERFACE,
the app OWNS the surface content.*

⚠ Do NOT reach for yggterm's OSC action `pick` here.  That one is the *profile*
chooser, and its list is enumerated GUI-side from `~/.yggterm/web-profiles/` —
it can only ever offer ychrome profiles.  A document surface is the general
mechanism; `pick` is a special case that predates it.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config as config_mod
from . import session as sessions
from . import view

#: The viewport pane id — the chooser proper.
PANE = "targets"

#: The same list offered as a rail panel (see the declare for why both).
RAIL_PANE = "targets-rail"

#: Re-declare cadence.  The GUI expires a contribution that stops speaking, the
#: same way it expires a surface, so this is liveness rather than politeness.
DECLARE_SECONDS = 4.0


def _osc(verb: str, action: str, payload: dict) -> None:
    """Announce on our own stdout — the PTY is the transport, for local and
    remote sessions alike, which is why this needs no discovery and no socket."""
    import base64

    blob = base64.b64encode(json.dumps(payload).encode()).decode()
    sys.stdout.write(f"\033]7717;{verb};{action};{blob}\007")
    sys.stdout.flush()


@dataclass
class _Choice:
    """What the user picked, handed from the HTTP thread to the main thread."""

    target: str


def _state_of(name: str) -> tuple[str, str]:
    """(status dot class, human sublabel) for one target, cheaply.

    Deliberately shallow: this runs on every schema fetch, and a chooser that
    takes two seconds to paint because it probed three hypervisors is a worse
    chooser.  `reachable()` is a TCP connect with a short timeout — enough to
    tell "the VM is up" from "the VM is not running", which is the distinction
    that decides whether clicking will work.  Anything deeper belongs behind
    `yrdp state`.
    """
    try:
        t = config_mod.load_target(name)
    except config_mod.ConfigError as exc:
        return "", f"misconfigured — {exc}"

    live = sessions.load(name)
    if live is not None and live.alive():
        # A live session is the strongest possible statement about this target,
        # and it is free to check (a pid probe), so it outranks a port probe.
        return "durable", f"connected · {live.geometry} · {t.connection.protocol.upper()}"

    up = sessions.reachable(t, timeout=1.0)
    where = f"{t.connection.host}:{t.connection.port}"
    if up:
        return "transient", f"ready · {t.geometry.stamp} · {where}"
    # NOT an error row. A powered-off guest is the normal resting state of a VM
    # and the target's own `up` hook is what fixes it — so the row stays
    # clickable and says what clicking will do.
    return "", f"not running · will start it · {where}"


def _schema() -> dict:
    """The chooser, in the pane vocabulary yggterm already renders.

    `row_action` (not a trailing button) is what makes the whole row the target:
    the operator said "I click one, and go to the session", and a row you must
    hit a small button on is not that.
    """
    rows: list[dict] = []
    for name in config_mod.list_targets():
        try:
            t = config_mod.load_target(name)
            title = t.description or name
            icon = "🖥"
        except config_mod.ConfigError:
            title, icon = name, "⚠"
        status, subtitle = _state_of(name)
        rows.append(
            {
                "kind": "list-row",
                "id": name,
                "title": title,
                "subtitle": subtitle,
                "icon": icon,
                "status": status,
                # Clicking anywhere on the row connects. The explicit action
                # button stays for discoverability and for a pointer that has
                # not learned the row is live.
                "row_action": f"connect:{name}",
                "actions": [
                    {"action": f"connect:{name}", "label": "Connect", "title": f"Attach {name}"}
                ],
            }
        )

    if not rows:
        rows.append(
            {
                "kind": "label",
                "text": (
                    "No targets configured. yRDP ships none and guesses no paths — "
                    "point YRDP_TARGETS_DIR at a store of *.toml target files."
                ),
                "muted": True,
            }
        )

    return {
        "title": "yRDP",
        "widgets": [{"kind": "section", "text": "Remote desktops"}, *rows],
        "footer": [
            {
                "kind": "label",
                "text": f"{len(config_mod.list_targets())} target(s) · {config_mod.targets_dir()}",
                "muted": True,
            }
        ],
    }


def _document_version() -> str:
    """A stamp over the CONTENT of the chooser, never a clock.

    ⛔ This was a clock for one debugging session and the chooser never painted.
    The contract is explicit — the GUI refetches the viewport pane *only when
    this moves* — so a value that moves every second means "the document changed"
    on every 4s declare and every 2.5s liveness ping. The GUI dutifully refetched
    forever and the surface never settled into a painted state, with no error
    anywhere: transport fine, schema valid, endpoint reachable, nothing drawn.

    Hashing the schema makes the stamp mean what the GUI thinks it means, and it
    is also what makes a genuine change (a guest that just came up) propagate
    promptly instead of on a timer.
    """
    return hashlib.sha256(json.dumps(_schema(), sort_keys=True).encode()).hexdigest()[:16]


class _Handler(BaseHTTPRequestHandler):
    """The control endpoint. Three routes, and the GUI drives all of them."""

    chosen: _Choice | None = None
    lock = threading.Lock()

    def log_message(self, *_args) -> None:  # noqa: D102 - silence stderr access logs
        return

    def _reply(self, body: dict, code: int = 200) -> None:
        blob = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        path = self.path.split("?", 1)[0]
        if path == "/ping":
            # Endpoint-ping liveness: answering IS the liveness signal, and the
            # stamp tells the GUI whether to refetch the pane. It moves every
            # tick on purpose — a target's reachability is exactly the kind of
            # thing that changes without anyone touching this process, and a
            # chooser showing a stale "not running" is worse than a refetch.
            self._reply({"app_name": "yRDP", "document_version": _document_version()})
        elif path in (f"/pane/{PANE}", f"/pane/{RAIL_PANE}"):
            # One list, two placements — the schema does not depend on which.
            self._reply(_schema())
        else:
            self._reply({"error": "no such route"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/action":
            self._reply({"error": "no such route"}, 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._reply({"error": "unparseable action"}, 400)
            return

        action = str(body.get("action") or "")
        if not action.startswith("connect:"):
            self._reply({"toast": f"yRDP does not know the action {action!r}"})
            return

        name = action.split(":", 1)[1]
        with _Handler.lock:
            _Handler.chosen = _Choice(target=name)
        # Answer immediately with a schema that says what is happening. The
        # connect itself can take a minute (a cold guest runs its `up` hook),
        # and an HTTP handler that blocks that long would hold the GUI's fetch
        # thread and read as a hung chooser.
        self._reply(
            {
                "toast": f"yRDP: connecting to {name}…",
                "schema": {
                    "title": "yRDP",
                    "widgets": [
                        {"kind": "section", "text": "Connecting"},
                        {"kind": "label", "text": f"Attaching {name}…", "muted": False},
                        {
                            "kind": "label",
                            "text": "A guest that is not running is started first; that can take a minute.",
                            "muted": True,
                        },
                    ],
                },
            }
        )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(*, quality: int, compression: int) -> int:
    """Show the chooser, then become the viewer for whatever was chosen.

    One process, two phases.  The chooser retires itself the moment a target is
    picked (`sidebar ; close`), and the same process goes on to hold the
    surface — so the row the operator launched from is the row the desktop
    appears in, which is what "I click one, and go to the session" means.
    """
    if not view.in_yggterm():
        print(
            "[yrdp] pick is a yggterm surface and there is no YGGTERM_SESSION_ID here. "
            "Name a target instead: `yrdp view --target <name>`.",
            file=sys.stderr,
        )
        return 2

    port = _free_port()
    control = f"http://127.0.0.1:{port}"
    # THREADING, not the plain HTTPServer. The GUI pings liveness every ~2.5s
    # AND fetches the pane schema; a single-threaded server serialises them
    # behind whichever connection the client holds open, which presents as a
    # chooser that declares itself and then hangs.
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    session_id = os.environ.get("YGGTERM_SESSION_ID", "")
    declare = {
        "session": session_id,
        "control": control,
        "app_name": "yRDP",
        # The GUI refetches the viewport pane only when this moves.
        "document_version": _document_version(),
        "panes": [
            # TWO placements on purpose. The viewport pane is the chooser the
            # operator asked for; the rail pane is the same list as a right-hand
            # panel, and it is ALSO the bisect that tells the two failure modes
            # apart when nothing paints: a rail button that appears proves the
            # OSC parsed, the contribution applied and the schema fetched, so a
            # blank viewport is specifically about viewport placement rather
            # than about this app's declare.
            {"id": PANE, "icon": "🖥", "title": "yRDP", "placement": "viewport"},
            {"id": RAIL_PANE, "icon": "🖥", "title": "yRDP"},
        ],
    }
    _osc("sidebar", "declare", declare)
    print(f"[yrdp] choose a target in the viewport (control {control})", file=sys.stderr)

    chosen: _Choice | None = None
    try:
        while chosen is None:
            time.sleep(DECLARE_SECONDS)
            declare["document_version"] = _document_version()
            _osc("sidebar", "declare", declare)
            with _Handler.lock:
                chosen = _Handler.chosen
    except KeyboardInterrupt:
        _osc("sidebar", "close", {"session": session_id})
        httpd.shutdown()
        return 130

    # Retire the chooser BEFORE the surface opens. Leaving it declared would
    # leave a viewport pane competing with the desktop we are about to reveal.
    _osc("sidebar", "close", {"session": session_id})
    httpd.shutdown()

    from . import cli  # local: avoids a circular import at module load

    return cli.reveal_target(chosen.target, quality=quality, compression=compression)
