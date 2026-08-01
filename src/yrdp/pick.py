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

from . import config as config_mod

# ⚠ `session` and `view` are imported LAZILY, inside the functions that need
# them. They drag in subprocess, the RFB client and the viewer machinery — about
# half of this program's import cost — and the chooser needs none of it until a
# row is actually clicked. Startup is the whole user-visible cost of a chooser:
# the operator's report was "a slow startup to the libyggterm list (terminal
# starts instantly with yrdp)", and 500ms of that was import time.

#: Chooser state the APP owns. The GUI holds the live draft while the user
#: types and posts it on the search action; this is where it settles.
_STATE = {"query": ""}

#: A TCP connect on a LAN or a loopback tunnel answers in single-digit ms; the
#: only thing a longer timeout buys is a longer stall on a guest that is OFF.
_PROBE_TIMEOUT = 0.4

#: Reachability is re-probed at most this often. The chooser is rebuilt on every
#: schema fetch AND on every liveness ping, so an uncached probe means dialling
#: every guest several times a second.
_PROBE_TTL = 5.0
_PROBE_CACHE: dict[str, tuple[float, str, str]] = {}

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
    hit = _PROBE_CACHE.get(name)
    if hit is not None and (time.monotonic() - hit[0]) < _PROBE_TTL:
        return hit[1], hit[2]
    status, text = _state_of_uncached(name)
    _PROBE_CACHE[name] = (time.monotonic(), status, text)
    return status, text


def _state_of_uncached(name: str) -> tuple[str, str]:
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

    from . import session as sessions

    live = sessions.load(name)
    if live is not None and live.alive():
        # A live session is the strongest possible statement about this target,
        # and it is free to check (a pid probe), so it outranks a port probe.
        return "durable", f"connected · {live.geometry} · {t.connection.protocol.upper()}"

    up = sessions.reachable(t, timeout=_PROBE_TIMEOUT)
    where = f"{t.connection.host}:{t.connection.port}"
    if up:
        return "transient", f"ready · {t.geometry.stamp} · {where}"
    # NOT an error row. A powered-off guest is the normal resting state of a VM
    # and the target's own `up` hook is what fixes it — so the row stays
    # clickable and says what clicking will do.
    return "", f"not running · will start it · {where}"


def _machines() -> list[tuple[str, list]]:
    """Targets grouped into the MACHINES a human is actually choosing between.

    ⛔ THE CATEGORY ERROR THIS FIXES. `tws` and `pl9` are two targets on ONE
    Windows guest — same protocol, same host, same port. Two targets is correct
    for the agent lane (each carries its own hooks, its own lore, its own
    ladder rungs), and it is wrong here: offering a human two rows that open the
    same desktop, described by the APPLICATION that happens to run on it, tells
    them nothing about what they are picking and is inaccurate the moment a
    third app is installed.

    So the chooser groups by `machine_key` — the endpoint, which is what makes
    two targets the same box — and names the group by `machine_label`.
    """
    groups: dict[tuple, list] = {}
    for name in config_mod.list_targets():
        try:
            t = config_mod.load_target(name)
        except config_mod.ConfigError:
            continue
        groups.setdefault(t.machine_key, []).append(t)
    out = []
    for key, targets in groups.items():
        targets.sort(key=lambda t: t.name)
        out.append((targets[0].machine_label, targets))
    out.sort(key=lambda pair: pair[0].lower())
    return out


def _pick_target(targets: list):
    """Which target speaks for the machine when the human clicks it.

    A live session wins — connecting to a box that already has one must attach
    to THAT session rather than force a second at another contract.
    """
    from . import session as sessions

    for t in targets:
        live = sessions.load(t.name)
        if live is not None and live.alive():
            return t
    return targets[0]


def _schema() -> dict:
    """The chooser: a heading, a filter, and one row per MACHINE."""
    query = _STATE["query"].strip().lower()
    rows: list[dict] = []
    shown = 0
    for label, targets in _machines():
        chosen = _pick_target(targets)
        apps = ", ".join(t.name for t in targets)
        haystack = f"{label} {apps}".lower()
        if query and query not in haystack:
            continue
        shown += 1
        status, state_text = _state_of(chosen.name)
        rows.append(
            {
                "kind": "list-row",
                "id": chosen.name,
                "title": label,
                # The apps ON the machine belong in the subtitle as context, not
                # in the title as identity.
                "subtitle": f"{state_text}  ·  {apps}",
                "icon": "🖥",
                "status": status,
                "row_action": f"connect:{chosen.name}",
                "actions": [
                    {"action": f"connect:{chosen.name}", "label": "Connect",
                     "title": f"Attach {label}"}
                ],
            }
        )

    if not rows:
        rows.append({
            "kind": "label",
            "muted": True,
            "text": (f"No machine matches {_STATE['query']!r}." if query else
                     "No targets configured. yRDP ships none and guesses no paths — "
                     "point YRDP_TARGETS_DIR at a store of *.toml target files."),
        })

    total = len(_machines())
    return {
        "title": "yRDP",
        "widgets": [
            # A markdown heading rather than a `section`: `section` is a small
            # all-caps group label sized for a 300px rail, and this is a document.
            {"kind": "markdown", "id": "hdr", "source": "# Remote desktops"},
            {"kind": "search-box", "id": "q", "action": "search",
             "placeholder": "Search machines…", "value": _STATE["query"]},
            *rows,
        ],
        "footer": [
            {"kind": "label", "muted": True,
             "text": (f"{shown} of {total} machine(s)" if query else f"{total} machine(s)")
                     + f"  ·  {config_mod.targets_dir()}"},
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


def _declare_now(payload: dict) -> None:
    """Emit a declare without stamping it first.

    The stamp costs a whole schema build, and the FIRST declare is the one the
    operator is waiting on — it is what makes the list appear at all. Stamping
    before announcing put the schema build on the critical path for no benefit:
    the GUI fetches the pane straight after, and that fetch is what needs to be
    current, not the announcement.
    """
    _osc("sidebar", "declare", payload)


#: Routes the GUI drives. Three of them, all tiny JSON — which is exactly why
#: this is hand-rolled rather than `http.server`.
#:
#: ⛔ `import http.server` COSTS ~240ms on this interpreter, measured, and it was
#: the single largest component of the chooser's startup — larger than every
#: other import combined and larger than the interpreter itself. The operator's
#: report was "a slow startup to the libyggterm list (terminal starts instantly
#: with yrdp)", and a stdlib import for three routes was most of it. yggterm's
#: own side of this channel is hand-rolled over a raw socket for the same reason
#: ("no HTTP client dependency for one GET"); this matches it.
_CHOSEN: _Choice | None = None
_LOCK = threading.Lock()


def _respond(conn: socket.socket, body: dict, code: int = 200) -> None:
    blob = json.dumps(body).encode()
    head = (
        f"HTTP/1.1 {code} {'OK' if code == 200 else 'Not Found'}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(blob)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()
    try:
        conn.sendall(head + blob)
    except OSError:
        pass  # the GUI hung up; nothing here is worth a traceback


def _handle(conn: socket.socket) -> None:
    """One request, one connection. `Connection: close` keeps this honest —
    no keep-alive means no half-read request wedging a worker."""
    global _CHOSEN
    try:
        conn.settimeout(10)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
            if len(buf) > 1 << 20:
                return
        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        method, _, target = lines[0].partition(" ")
        path = target.split(" ")[0].split("?", 1)[0]

        if method == "GET" and path == "/ping":
            # Answering IS the liveness signal. The stamp tells the GUI whether
            # to refetch the pane, so it must be over CONTENT, never a clock.
            _respond(conn, {"app_name": "yRDP", "document_version": _document_version()})
            return
        if method == "GET" and path in (f"/pane/{PANE}", f"/pane/{RAIL_PANE}"):
            _respond(conn, _schema())
            return
        if method != "POST" or path != "/action":
            _respond(conn, {"error": "no such route"}, 404)
            return

        length = 0
        for line in lines[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "content-length":
                length = int(value.strip() or 0)
        body_bytes = rest
        while len(body_bytes) < length:
            chunk = conn.recv(min(65536, length - len(body_bytes)))
            if not chunk:
                break
            body_bytes += chunk
        try:
            body = json.loads(body_bytes or b"{}")
        except ValueError:
            _respond(conn, {"error": "unparseable action"}, 400)
            return

        action = str(body.get("action") or "")
        values = body.get("values") or {}

        if action == "search":
            _STATE["query"] = str(values.get("q") or "")
            _respond(conn, {"schema": _schema()})
            return

        if not action.startswith("connect:"):
            _respond(conn, {"toast": f"yRDP does not know the action {action!r}"})
            return

        name = action.split(":", 1)[1]
        with _LOCK:
            _CHOSEN = _Choice(target=name)
        # Answer at once. A cold guest runs its `up` hook, which can take a
        # minute, and an HTTP handler that blocks that long holds the GUI's
        # fetch thread and reads as a hung chooser.
        _respond(conn, {
            "toast": f"yRDP: connecting to {name}…",
            "schema": {
                "title": "yRDP",
                "widgets": [
                    {"kind": "markdown", "id": "hdr", "source": "# Connecting"},
                    {"kind": "label", "text": f"Attaching {name}…"},
                    {"kind": "label", "muted": True,
                     "text": "A guest that is not running is started first; that can take a minute."},
                ],
            },
        })
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _serve(sock: socket.socket) -> None:
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        threading.Thread(target=_handle, args=(conn,), daemon=True).start()


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
    from . import view

    if not view.in_yggterm():
        print(
            "[yrdp] pick is a yggterm surface and there is no YGGTERM_SESSION_ID here. "
            "Name a target instead: `yrdp view --target <name>`.",
            file=sys.stderr,
        )
        return 2

    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = sock.getsockname()[1]
    control = f"http://127.0.0.1:{port}"
    threading.Thread(target=_serve, args=(sock,), daemon=True).start()

    session_id = os.environ.get("YGGTERM_SESSION_ID", "")
    declare = {
        "session": session_id,
        "control": control,
        "app_name": "yRDP",
        # A cheap placeholder, NOT a real stamp. The real one is computed on the
        # first loop pass; putting a schema build ahead of the first announcement
        # delays the only thing the operator is waiting for.
        "document_version": "boot",
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
    _declare_now(declare)
    print(f"[yrdp] choose a target in the viewport (control {control})", file=sys.stderr)

    chosen: _Choice | None = None
    try:
        while chosen is None:
            time.sleep(DECLARE_SECONDS)
            declare["document_version"] = _document_version()
            _osc("sidebar", "declare", declare)
            with _LOCK:
                chosen = _CHOSEN
    except KeyboardInterrupt:
        _osc("sidebar", "close", {"session": session_id})
        sock.close()
        return 130

    # Retire the chooser BEFORE the surface opens. Leaving it declared would
    # leave a viewport pane competing with the desktop we are about to reveal.
    _osc("sidebar", "close", {"session": session_id})
    sock.close()

    from . import cli  # local: avoids a circular import at module load

    return cli.reveal_target(chosen.target, quality=quality, compression=compression)
