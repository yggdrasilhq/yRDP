"""The durable half — a per-host yRDP daemon.

**The emacsclient model, which libyggterm already prescribes:** *"the view client
is disposable; the daemon is durable."*  A serious libyggterm app splits into a
per-host DAEMON that owns state and the control endpoint, and a thin VIEW CLIENT
that anchors the surface in a session and forwards OSC.

Why this exists, in one measurement.  The chooser used to pay a whole process
start — interpreter, imports, a fresh control endpoint on a fresh port — every
time the operator opened it, and then paid a cold reachability probe of every
guest because nothing outlived the invocation.  None of that is work; it is
setup, repeated.  A daemon does it once:

    per invocation, before        per invocation, after
    ------------------------      ---------------------
    interpreter + imports          one TCP connect
    bind a new control port        the port is already there and already
                                   forwarded by the GUI's ssh -L
    probe every guest cold         the cache is warm, and refreshed in the
                                   background between invocations
    attach on click (~400 ms)      the bridge can already be up

The last line is the one the operator will feel: the daemon can hold a session's
viewer bridge ready, so "connect" becomes an OSC emit rather than a build.

⚠ THE DAEMON DOES NOT OWN THE PTY, AND CANNOT.  An OSC surface announcement is
carried by the terminal byte stream of the session it belongs to, so only a
process running *in that PTY* can emit one.  That is exactly why the client
survives as a thin forwarder rather than disappearing entirely: the daemon
decides, the client speaks.  Anything that tries to make the daemon emit OSC
directly is announcing into a stream nobody is reading.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config as config_mod

#: Where the daemon publishes how to reach it.  A file rather than a fixed port:
#: a fixed port is a collision waiting for a second user on a shared host, and
#: the GUI never sees this — it only ever gets the URL the client declares.
def state_path() -> Path:
    return config_mod.state_dir() / "daemon.json"


#: Reachability re-probe cadence.  The daemon refreshes in the BACKGROUND, so a
#: chooser fetch never waits on a socket: it reads whatever the last sweep saw.
PROBE_TTL = 5.0
PROBE_TIMEOUT = 0.4

#: How long a daemon with nobody talking to it stays up.  Long enough that a
#: session of work never pays a cold start twice, short enough that an idle host
#: is not holding an RDP client open forever.
IDLE_EXIT_SECONDS = 3600.0

#: How long a client's `/events` poll vouches for that client.  `pick` polls on
#: a 4 s cadence, so three missed polls is a client that has GONE rather than a
#: client that was slow.  This is what makes the routing below exact instead of
#: hopeful: a mailbox is addressed to a session we heard from seconds ago.
CLIENT_TTL = 12.0


class _Hub:
    """Everything that is worth not rebuilding: probe state and the OSC queue."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.probes: dict[str, tuple[float, str, str]] = {}
        self.query = ""
        # OSC the daemon wants emitted, per client session. The client polls
        # this and writes to its own stdout — see the module docstring for why
        # the daemon cannot do it itself.
        self.events: dict[str, list[dict]] = {}
        # Which client sessions are actually listening, by last `/events` poll.
        # A mailbox addressed to anything else is a message into the void.
        self.clients: dict[str, float] = {}
        # The last connect failure per target, so a failure is something the
        # operator READS rather than something the pane hides.
        self.errors: dict[str, str] = {}
        self.last_seen = time.monotonic()

    def touch(self) -> None:
        self.last_seen = time.monotonic()

    def seen(self, session: str) -> None:
        """Record that a client is listening. Every poll is its own liveness."""
        if session:
            with self.lock:
                self.clients[session] = time.monotonic()

    def live_clients(self) -> list[str]:
        now = time.monotonic()
        with self.lock:
            return sorted(s for s, at in self.clients.items() if now - at <= CLIENT_TTL)

    def push(self, session: str, verb: str, action: str, payload: dict) -> None:
        with self.lock:
            self.events.setdefault(session, []).append(
                {"verb": verb, "action": action, "payload": payload}
            )

    def drain(self, session: str) -> list[dict]:
        with self.lock:
            return self.events.pop(session, [])

    def forget_dead_mailboxes(self) -> None:
        """Drop mail for clients that will never collect it.

        Without this a client that dies mid-connect leaves its viewer URL queued
        for the lifetime of the daemon — invisible, undeliverable, and growing.
        """
        now = time.monotonic()
        with self.lock:
            for sess in [
                s for s in self.events
                if now - self.clients.get(s, 0.0) > CLIENT_TTL
            ]:
                self.events.pop(sess, None)


HUB = _Hub()


def _probe(name: str) -> tuple[str, str]:
    """Reachability + session state for one target, from cache when fresh."""
    hit = HUB.probes.get(name)
    if hit is not None and (time.monotonic() - hit[0]) < PROBE_TTL:
        return hit[1], hit[2]

    from . import session as sessions

    try:
        t = config_mod.load_target(name)
    except config_mod.ConfigError as exc:
        out = ("", f"misconfigured — {exc}")
        HUB.probes[name] = (time.monotonic(), *out)
        return out

    live = sessions.load(name)
    if live is not None and live.alive():
        out = ("durable", f"connected · {live.geometry} · {t.connection.protocol.upper()}")
    else:
        where = f"{t.connection.host}:{t.connection.port}" if t.connection else "?"
        if sessions.reachable(t, timeout=PROBE_TIMEOUT):
            out = ("transient", f"ready · {t.geometry.stamp} · {where}")
        else:
            # A powered-off guest is the normal resting state of a VM, not an
            # error: the row stays clickable and says what clicking will do.
            out = ("", f"not running · will start it · {where}")
    HUB.probes[name] = (time.monotonic(), *out)
    return out


def _sweep() -> None:
    """Keep the cache warm between invocations — the whole point of a daemon.

    A chooser fetch should never wait on a socket. This runs on its own thread
    and refreshes every target just before the TTL expires, so the answer is
    already sitting there when the GUI asks.
    """
    while True:
        try:
            for name in config_mod.list_targets():
                _probe(name)
            HUB.forget_dead_mailboxes()
        except Exception:
            pass  # a bad target file must not take the daemon down
        if time.monotonic() - HUB.last_seen > IDLE_EXIT_SECONDS:
            os._exit(0)
        time.sleep(PROBE_TTL * 0.8)


# -- the chooser's content ---------------------------------------------------


def machines() -> list[tuple[str, list]]:
    """Targets grouped into the MACHINES a human chooses between.

    Several targets routinely share one box — an operator's Windows guest can
    carry a trading client and an astrology suite, and each gets its own target
    because each has its own hooks and its own lore. Right for the agent lane,
    wrong for a chooser: two rows that open the same desktop, named after
    whichever application happens to be installed, describe nothing.
    """
    groups: dict[tuple, list] = {}
    for name in config_mod.list_targets():
        try:
            t = config_mod.load_target(name)
        except config_mod.ConfigError:
            continue
        groups.setdefault(t.machine_key, []).append(t)
    out = []
    for targets in groups.values():
        targets.sort(key=lambda t: t.name)
        out.append((targets[0].machine_label, targets))
    out.sort(key=lambda pair: pair[0].lower())
    return out


def _representative(targets: list):
    """A live session wins: connecting to a box that already has one must attach
    to THAT session rather than force a second at another contract."""
    from . import session as sessions

    for t in targets:
        live = sessions.load(t.name)
        if live is not None and live.alive():
            return t
    return targets[0]


def schema() -> dict:
    query = HUB.query.strip().lower()
    rows: list[dict] = []
    shown = 0
    groups = machines()
    for label, targets in groups:
        chosen = _representative(targets)
        apps = ", ".join(t.name for t in targets)
        if query and query not in f"{label} {apps}".lower():
            continue
        shown += 1
        status, state_text = _probe(chosen.name)
        # A failed attempt outranks the probe: a cheerful "ready · …" over the
        # top of a connect that just threw tells the operator to click the same
        # button again and expect a different answer.
        failed = HUB.errors.get(chosen.name)
        if failed:
            status, state_text = "", f"last attempt failed — {failed}"
        rows.append(
            {
                "kind": "list-row",
                "id": chosen.name,
                "title": label,
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
            "kind": "label", "muted": True,
            "text": (f"No machine matches {HUB.query!r}." if query else
                     "No targets configured. yRDP ships none and guesses no paths — "
                     "point YRDP_TARGETS_DIR at a store of *.toml target files."),
        })
    return {
        "title": "yRDP",
        "widgets": [
            {"kind": "markdown", "id": "hdr", "source": "# Remote desktops"},
            {"kind": "search-box", "id": "q", "action": "search",
             "placeholder": "Search machines…", "value": HUB.query},
            *rows,
        ],
        "footer": [{
            "kind": "label", "muted": True,
            "text": (f"{shown} of {len(groups)} machine(s)" if query
                     else f"{len(groups)} machine(s)") + f"  ·  {config_mod.targets_dir()}",
        }],
    }


def document_version() -> str:
    """A stamp over CONTENT, never a clock.

    ⛔ It was a clock once and the chooser never painted: the GUI refetches the
    viewport pane only when this MOVES, so a value changing every second means
    "the document changed" on every declare and every liveness ping. It
    refetched forever and never settled, with no error anywhere.
    """
    import hashlib

    return hashlib.sha256(json.dumps(schema(), sort_keys=True).encode()).hexdigest()[:16]


def route(declared: str, live: list[str]) -> tuple[str, str]:
    """Which client session a connect belongs to — or why it cannot be known.

    ⛔ THE BUG THIS EXISTS TO END.  yggterm's action POST carries `pane`,
    `action`, `values` and `value_keys` — and **no session**, even though the
    document channel it arrives on is session-scoped on the GUI's own side.  So
    `body["session"]` was empty, the outcome was filed under `""`, and the
    client — polling `/events?session=<its own id>` — collected nothing, for
    ever.  The operator watched "Connecting" while the guest was up, the RDP
    session was live, and the viewer URL sat finished in a mailbox with no
    reader.  A silent success is worse than a failure: nothing was wrong to
    find, because nothing was wrong.

    The fix does not guess.  Every client announces itself on the 4 s poll it
    was already making, so the daemon KNOWS who is listening:

    * a declared session wins outright — this is forward-compatible, and the
      day the platform names the session on the wire this whole fallback stops
      being reached;
    * exactly one live client is not a guess, it is the answer;
    * anything else is refused BY NAME.  Two choosers and a session-less action
      is genuinely unknowable, and picking the newest would open a stranger's
      desktop in the wrong viewport — the one failure worse than hanging.

    Returns `(session, refusal)`; exactly one of the two is non-empty.
    """
    if declared:
        return declared, ""
    if len(live) == 1:
        return live[0], ""
    if not live:
        return "", (
            "No chooser is listening any more, so there is nowhere to put this "
            "desktop. Run yrdp pick in a yggterm session and connect again."
        )
    return "", (
        f"{len(live)} choosers are open and the click did not say which one "
        f"asked, so this desktop has no address. Close the others, or open one "
        f"directly with yrdp view --target <name>."
    )


def _connect(session_id: str, target: str, quality: int, compression: int) -> None:
    """Do the work, then hand the client an OSC to speak.

    Runs on its own thread: a cold guest runs its `up` hook and a cold RDP
    negotiation takes ten seconds, and an HTTP handler that blocks that long
    holds the GUI's fetch thread and reads as a hung chooser.
    """
    from . import cli

    try:
        viewer = cli.attach_viewer(target, quality=quality, compression=compression)
    except Exception as exc:  # a failed connect must not kill the daemon
        # Both lines matter. The message is what the operator READS on the row;
        # dropping the probe is what makes them SEE it — the GUI refetches a
        # pane only when the content stamp moves, so a failure that changed no
        # cached state would leave "Connecting" on screen with the error
        # reachable nowhere. That is the same forever-hang wearing a
        # different hat.
        HUB.errors[target] = str(exc)
        HUB.probes.pop(target, None)
        HUB.push(session_id, "toast", "error", {"text": str(exc)})
        return
    HUB.push(session_id, "web-surface", "open", {
        "session": session_id,
        "url": viewer.url,
        "title": viewer.target,
    })
    HUB.probes.pop(target, None)  # the state just changed; re-probe on next read


# -- the control endpoint ----------------------------------------------------


def _respond(conn: socket.socket, body: dict, code: int = 200) -> None:
    blob = json.dumps(body).encode()
    head = (
        f"HTTP/1.1 {code} {'OK' if code == 200 else 'Not Found'}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(blob)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()
    try:
        conn.sendall(head + blob)
    except OSError:
        pass


def _handle(conn: socket.socket) -> None:
    """One request per connection.

    Hand-rolled rather than `http.server`: importing that cost ~240 ms on this
    interpreter, measured — more than every other import combined — to serve a
    handful of routes of tiny JSON. yggterm's own side of this channel is
    hand-rolled over a raw socket for the same reason.
    """
    try:
        conn.settimeout(15)
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
        raw_path = target.split(" ")[0]
        path, _, qs = raw_path.partition("?")
        params = dict(
            (kv.split("=", 1) + [""])[:2] for kv in qs.split("&") if kv
        ) if qs else {}
        HUB.touch()

        if method == "GET" and path == "/ping":
            _respond(conn, {"app_name": "yRDP", "document_version": document_version()})
            return
        if method == "GET" and path.startswith("/pane/"):
            _respond(conn, schema())
            return
        if method == "GET" and path == "/events":
            # The client's mailbox. It polls this and writes what it finds to
            # its own PTY, because only a process in that PTY can emit an OSC.
            # The poll doubles as the client's registration: this is the ONLY
            # place the daemon ever learns a session id, since the GUI's action
            # POST does not carry one. See `route`.
            who = params.get("session", "")
            HUB.seen(who)
            _respond(conn, {"events": HUB.drain(who)})
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
        session_id = str(body.get("session") or params.get("session") or "")

        if action == "search":
            HUB.query = str(values.get("q") or "")
            _respond(conn, {"schema": schema()})
            return
        if not action.startswith("connect:"):
            _respond(conn, {"toast": f"yRDP does not know the action {action!r}"})
            return

        name = action.split(":", 1)[1]
        session_id, refusal = route(session_id, HUB.live_clients())
        if refusal:
            # Land the operator back on a working chooser with the reason on it,
            # never on a dead end. The reply's `toast` is a real GUI
            # notification, unlike one pushed through the client's mailbox —
            # that one only reaches the PTY's stderr, which nobody is reading
            # while they look at the document pane.
            warned = schema()
            warned["widgets"].insert(1, {"kind": "label", "text": f"⚠ {refusal}"})
            # NO "yRDP:" in the text. The GUI titles the card with the pane's
            # own title — which is "yRDP" — so a prefixed body reads "yRDP ·
            # yRDP: no chooser…" on screen, and the operator photographed
            # exactly that. The app names itself once, in the place the
            # platform asks it to.
            _respond(conn, {"toast": refusal, "schema": warned})
            return
        HUB.errors.pop(name, None)  # a retry clears the last failure's message
        threading.Thread(
            target=_connect, args=(session_id, name, 9, 0), daemon=True
        ).start()
        # NO TOAST ON THE HAPPY PATH. The reply's schema puts "Connecting" in
        # the pane the operator is already looking at, so a notification on top
        # of it says nothing they cannot see — and it does not fade: it lands in
        # the notification centre for them to clear afterwards. A click that
        # worked is not news. Refusals and failures still speak, because those
        # are the ones the pane may lose.
        _respond(conn, {
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


def serve() -> int:
    """Run the daemon in the foreground. `yrdp daemon` calls this."""
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(32)
    port = sock.getsockname()[1]

    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"port": port, "pid": os.getpid(), "started": time.time()}))
    print(f"[yrdp] daemon on 127.0.0.1:{port}", file=sys.stderr)

    threading.Thread(target=_sweep, daemon=True).start()
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return 0
        threading.Thread(target=_handle, args=(conn,), daemon=True).start()


# -- what a client uses ------------------------------------------------------


def probe(port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.sendall(b"GET /ping HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            return b"200" in s.recv(64)
    except OSError:
        return False


def ensure(timeout: float = 8.0) -> str:
    """The daemon's control URL, starting it if nobody has yet.

    This is the whole client-side cost of the two-tier split: a file read and a
    TCP connect on the warm path, versus a process start on the cold one. The
    cold path happens once per host per session of work.
    """
    path = state_path()
    if path.is_file():
        try:
            port = int(json.loads(path.read_text())["port"])
            if probe(port):
                return f"http://127.0.0.1:{port}"
        except (ValueError, KeyError, OSError):
            pass

    # Detached on purpose: the daemon must outlive the client that started it,
    # which is the entire point of the split.
    subprocess.Popen(
        [sys.executable, "-m", "yrdp.cli", "daemon"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                port = int(json.loads(path.read_text())["port"])
                if probe(port):
                    return f"http://127.0.0.1:{port}"
            except (ValueError, KeyError, OSError):
                pass
        time.sleep(0.05)
    raise RuntimeError("the yRDP daemon did not come up; run `yrdp daemon` to see why")
