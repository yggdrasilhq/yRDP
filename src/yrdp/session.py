"""Sessions — one canonical surface per target, pinned to the declared geometry.

A session is a process pair rather than a daemon's bookkeeping: a headless
display created at exactly the contract geometry, and a client living inside it.
The operating system already holds the state a daemon would, so what we persist
is a small record — which means any later invocation, or any other agent or
person on the host, can find, drive, reveal and close a session it did not open.

**The canonical surface is never alone.** It exists whether or not anyone is
watching, and any number of viewers may attach to it and detach again without
disturbing it (see ``view.py``).  Modelling "agent surface" and "human surface"
as exclusive modes was a mistake: a surface nobody can look at is a surface
nobody can trust, and co-browsing one session is the whole point.

The geometry contract is enforced here, at the point of action:

* the display is created at the target's declared size, so nothing downstream
  can renegotiate it;
* the flags that would let the far end resize us are named per protocol in
  ``clients.py`` and locked out by test;
* a coordinate replayed from lore proven at another geometry is refused, and a
  coordinate outside the surface is refused too.

**Viewers scale; they never resize.** That is what makes N-viewer co-browse fall
out for free: the shared object is a fixed-size framebuffer, not a negotiated
one.
"""

from __future__ import annotations

import json
import os
import select
import signal
import socket
import subprocess
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from shutil import which
from typing import Any

from . import clients, rfb
from .config import ConfigError, Target, load_target, state_dir
from .geometry import Geometry, require_match

CONNECT_TIMEOUT = 45.0

#: A direct RFB conversation is opened per verb and closed again, so this bounds
#: a single act, not a session.  Kept short: the endpoint either answers or the
#: caller wants to know that it did not.
RFB_TIMEOUT = 15.0


class SessionError(Exception):
    """A session could not be opened, found, or driven."""


class CredentialUnavailable(SessionError):
    """The credential exists but this process may not have it.

    Named separately because the recovery is the operator's, not ours: unlock
    the vault.  Reaching around it — setting a password over an admin channel we
    happen to hold — would be changing someone's credential for our own
    convenience, and is out of bounds.
    """


class AuthRefused(SessionError):
    """The far end rejected the credential.  Distinct from 'cannot reach it'."""


@dataclass(slots=True)
class Session:
    target: str
    geometry: str
    display: str
    xvfb_pid: int
    client_pid: int
    host: str
    user: str | None
    opened_at: float
    protocol: str = "rdp"
    window_found: bool = False
    lease_until: float | None = None
    last_verb: str = "open"
    #: How this surface is held: a headless display with a client binary in it,
    #: or the protocol spoken directly.  Defaults to x11 so a record written by
    #: an older build still loads and still means what it said.
    backend: str = clients.BACKEND_X11
    #: What the far end declared it actually is, at the last connect.  The
    #: contract lives in ``geometry``; this is the measurement, and the two
    #: disagreeing is a fact worth carrying rather than averaging away.
    server_geometry: str = ""
    #: Attached viewers, appended by view.py.  A session with none is still a
    #: perfectly good session — it is simply unwatched at this moment.
    viewers: list[dict] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    @property
    def geom(self) -> Geometry:
        return Geometry.parse(self.geometry)

    @property
    def endpoint(self) -> tuple[str, int]:
        host, _, port = self.host.rpartition(":")
        return host, int(port)

    def alive(self) -> bool:
        """Alive means something different for each backend, honestly.

        An x11 session is two processes we own, so their existence is the truth.
        A direct RFB session owns no process at all — what makes it usable is
        that the far end still answers, so that is what we ask.  Reporting a
        pid-based 'alive' for a backend with no pids would be a comforting lie.
        """
        if self.backend == clients.BACKEND_RFB:
            host, port = self.endpoint
            try:
                with socket.create_connection((host, port), timeout=1.5):
                    return True
            except (OSError, ValueError):
                return False
        return _alive(self.client_pid) and _alive(self.xvfb_pid)


# -- the record -------------------------------------------------------------


def sessions_dir() -> Path:
    d = state_dir() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(target: str) -> Path:
    return sessions_dir() / f"{target}.json"


def load(target: str) -> Session | None:
    p = _path(target)
    if not p.is_file():
        return None
    try:
        return Session(**json.loads(p.read_text()))
    except (OSError, ValueError, TypeError):
        return None


def save(s: Session) -> None:
    _path(s.target).write_text(json.dumps(asdict(s), indent=2))


def forget(target: str) -> None:
    _path(target).unlink(missing_ok=True)


def all_sessions() -> list[Session]:
    return [s for p in sorted(sessions_dir().glob("*.json")) if (s := load(p.stem))]


def live_session(target: str) -> Session:
    s = load(target)
    if s is None:
        raise SessionError(f"no session for {target}; `yrdp open --target {target}` first")
    if not s.alive():
        forget(target)
        raise SessionError(
            f"the recorded session for {target} is gone (client or display exited); "
            f"the record has been cleared, open a new one"
        )
    return s


def _alive(pid: int) -> bool:
    # pid 0 means "this process group" to kill(2), so a record with no process —
    # every rfb session — must never reach it. Guarding here rather than at each
    # call site: there is one right answer and this is where it belongs.
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, TypeError):
        return False


def _require(binary: str) -> str:
    path = which(binary)
    if path is None:
        raise SessionError(
            f"{binary} is not installed on this host. yRDP needs Xvfb, xdotool, "
            f"ImageMagick and a client for the protocol in use."
        )
    return path


def _free_display(start: int = 90, end: int = 120) -> str:
    x11 = Path("/tmp/.X11-unix")
    used = {p.name.lstrip("X") for p in x11.glob("X*")} if x11.is_dir() else set()
    for n in range(start, end):
        if str(n) not in used and not Path(f"/tmp/.X{n}-lock").exists():
            return f":{n}"
    raise SessionError(f"no free X display between :{start} and :{end}")


def reachable(target: Target, timeout: float = 3.0) -> bool:
    if target.connection is None:
        raise ConfigError(f"target {target.name!r} declares no [connection] endpoint")
    try:
        with socket.create_connection(
            (target.connection.host, target.connection.port), timeout=timeout
        ):
            return True
    except OSError:
        return False


# -- credentials ------------------------------------------------------------


def resolve_password(
    target: Target, entry: str | None = None, *, required: bool = True
) -> str | None:
    """Fetch the password by NAME, never by value from config.

    Order: an explicit environment override (useful for a one-shot proof), then
    the vault entry the target names.  The vault is the single source of truth
    for secrets; yRDP does not get a second one.
    """
    if env := os.environ.get("YRDP_RDP_PASSWORD"):
        return env
    name = entry or (target.connection.password_vault_entry if target.connection else None)
    if not name:
        if not required:
            # Naming no credential is a legitimate answer for an endpoint that
            # asks for none. Refusing here would invent a requirement the far
            # end does not have; if it does want one, its own refusal says so
            # clearly and this returns AuthRefused instead.
            return None
        raise CredentialUnavailable(
            f"target {target.name!r} names no credential. Add password_vault_entry to "
            f"[connection], or pass --password-entry, or set YRDP_RDP_PASSWORD once."
        )
    vault = which("ychrome-vault")
    if vault is None:
        raise CredentialUnavailable("ychrome-vault is not on PATH, so no secret can be resolved")
    p = subprocess.run([vault, "get", name], capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        detail = (p.stderr or p.stdout).strip()[:300]
        raise CredentialUnavailable(
            f"the vault would not give up {name!r}: {detail}. If it reads locked, the "
            f"operator has to unlock it — agents cannot, by design, and yRDP will not "
            f"route around that. If it says the entry is unknown, the local agent may "
            f"simply be behind: `ychrome-vault sync` re-pulls without a password."
        )
    return p.stdout.rstrip("\n")


# -- open / close -----------------------------------------------------------


def open_session(
    target: Target,
    *,
    password_entry: str | None = None,
    protocol: str | None = None,
    connect_timeout: float = CONNECT_TIMEOUT,
    force: bool = False,
) -> Session:
    if target.connection is None:
        raise SessionError(f"target {target.name!r} declares no [connection] endpoint")

    if (existing := load(target.name)) and existing.alive() and not force:
        raise SessionError(
            f"{target.name} already has a live session on {existing.display} "
            f"(pinned {existing.geometry}); reveal it with `yrdp view`, or pass --force"
        )
    if existing:
        close_session(target.name, quiet=True)

    if not reachable(target):
        raise SessionError(
            f"{target.connection.host}:{target.connection.port} is not answering. yRDP "
            f"does not know why — that is site knowledge. If this target declares an "
            f"'up' hook, `yrdp up --target {target.name}` runs it."
        )

    proto = protocol or target.connection.protocol
    adapter = clients.ADAPTERS[proto]
    password = resolve_password(target, password_entry, required=adapter.credential_required)

    if adapter.backend == clients.BACKEND_RFB:
        return _open_direct(target, proto, password)

    xvfb, client, xdotool = _require("Xvfb"), _require(adapter.binary), _require("xdotool")
    geom = target.geometry
    display = _free_display()

    xvfb_proc = subprocess.Popen(
        [xvfb, display, "-screen", "0", f"{geom.width}x{geom.height}x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    _await_display(display, xvfb_proc)

    env = {**os.environ, "DISPLAY": display}
    spawned = clients.spawn(target, password, env, protocol=proto, binary=client)
    spawned.release()
    client_proc = spawned.proc

    session = Session(
        target=target.name,
        geometry=geom.stamp,
        display=display,
        xvfb_pid=xvfb_proc.pid,
        client_pid=client_proc.pid,
        host=f"{target.connection.host}:{target.connection.port}",
        user=target.connection.user,
        opened_at=time.time(),
        protocol=proto,
    )

    try:
        session.window_found = _await_window(
            display, xdotool, client_proc, connect_timeout, session, adapter
        )
    except BaseException:
        # A session that failed to open must not leave a headless display and a
        # half-connected client behind: they would hold a display number, appear
        # in nobody's session list, and be found later by whoever wonders what is
        # eating the host.
        _reap(client_proc, xvfb_proc)
        raise
    save(session)
    return session


def _open_direct(target: Target, proto: str, password: str | None) -> Session:
    """Open a session for a protocol we speak ourselves.

    There is no display to create and no client to supervise, so 'opening' is:
    prove the endpoint really talks the protocol, measure what it actually is,
    and record the contract we will hold it to.  Everything the record exists
    for — being findable by another agent, carrying viewers, being closed by
    someone who did not open it — works exactly as it does for the x11 backend.
    """
    conn = target.connection
    assert conn is not None  # open_session checked; this keeps the type honest
    with _rfb_errors("open"):
        client = rfb.RfbClient.connect(
            conn.host, conn.port, password=password, timeout=RFB_TIMEOUT
        )
        observed = client.geometry
        name, version = client.name, client.version
        client.close()

    session = Session(
        target=target.name,
        geometry=target.geometry.stamp,
        display="",
        xvfb_pid=0,
        client_pid=0,
        host=f"{conn.host}:{conn.port}",
        user=conn.user,
        opened_at=time.time(),
        protocol=proto,
        backend=clients.BACKEND_RFB,
        server_geometry=observed,
        window_found=True,
    )
    session.events.append(f"RFB {version} · {name!r} · framebuffer {observed}")
    if observed != session.geometry:
        # Not a failure: looking is still allowed and still useful. But every
        # coordinate is now meaningless, and saying so once here beats a click
        # that lands somewhere plausible and wrong.
        session.events.append(
            f"⚠ the far end is {observed}, the contract says {session.geometry}; "
            f"coordinate verbs will refuse until they agree"
        )
    save(session)
    return session


@contextmanager
def _rfb_errors(what: str):
    """One place where the protocol's failures become the tool's named outcomes.

    The x11 backend earns these by matching strings in a client's stderr; here
    they arrive as types. Same two outcomes either way, because the caller's
    recovery depends on the outcome and must never depend on the protocol.
    """
    try:
        yield
    except rfb.RfbAuthError as exc:
        raise AuthRefused(str(exc)) from exc
    except rfb.RfbError as exc:
        raise SessionError(f"{what}: {exc}") from exc


def _reap(*procs: subprocess.Popen) -> None:
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
    for proc in procs:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


def _await_display(display: str, proc: subprocess.Popen, timeout: float = 15.0) -> None:
    sock = Path("/tmp/.X11-unix") / ("X" + display.lstrip(":"))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sock.exists():
            return
        if proc.poll() is not None:
            err = (proc.stderr.read() or b"").decode(errors="replace").strip()[:300]
            raise SessionError(f"Xvfb died before {display} came up: {err}")
        time.sleep(0.2)
    raise SessionError(f"Xvfb never created {display}")


def _drain(stream, buf: list[str]) -> str:
    """Read what the client has said so far without blocking on it.

    Draining also keeps the pipe from filling: a client we never read from can
    block on its own stderr and then look like a hang we caused.
    """
    if stream is None:
        return "".join(buf)
    while select.select([stream], [], [], 0)[0]:
        chunk = os.read(stream.fileno(), 65536)
        if not chunk:
            break
        buf.append(chunk.decode(errors="replace"))
    return "".join(buf)


def _await_window(
    display: str,
    xdotool: str,
    proc: subprocess.Popen,
    timeout: float,
    session: Session,
    adapter: clients.Adapter,
) -> bool:
    """Wait for the client to paint, or for it to tell us why it will not."""
    deadline = time.monotonic() + timeout
    buf: list[str] = []
    while time.monotonic() < deadline:
        text = _drain(proc.stderr, buf)
        if (code := proc.poll()) is not None:
            raise _classify(code, text + (proc.stderr.read() or b"").decode(errors="replace"), adapter)
        if adapter.fatal_marker and adapter.fatal_marker in text:
            # It answered. Do not make the caller wait out the timeout for a
            # verdict already given.
            _reap(proc)
            raise _classify(proc.returncode or -1, text, adapter)
        found = subprocess.run(
            [xdotool, "search", "--onlyvisible", "--class", "."],
            env={**os.environ, "DISPLAY": display},
            capture_output=True,
            text=True,
        )
        if found.returncode == 0 and found.stdout.strip():
            return True
        time.sleep(0.5)
    # A live client with no mapped window is not a failure we should invent a
    # cause for: report it and let the caller look with a screenshot.
    session.events.append("client alive but no window mapped before the deadline")
    return False


def _classify(code: int, stderr: str, adapter: clients.Adapter) -> SessionError:
    """Same named outcomes for every protocol — the caller's recovery differs."""
    if any(m in stderr for m in adapter.auth_markers):
        return AuthRefused(
            "the far end refused the credential. The service is healthy — this is the "
            "password, not the plumbing."
        )
    if any(m in stderr for m in adapter.unreachable_markers):
        return SessionError(
            "could not reach the service. yRDP does not guess why; if this target has an "
            "'up' hook, the machine may simply not be running."
        )
    tail = "\n".join(line for line in stderr.splitlines() if "ERROR" in line)[-600:]
    return SessionError(f"the client exited with {code}: {tail or stderr[-400:]}")


def close_session(target: str, *, quiet: bool = False) -> bool:
    s = load(target)
    if s is None:
        if quiet:
            return False
        raise SessionError(f"no recorded session for {target}")
    # Viewers first: a viewer outliving its session would keep serving a frozen
    # frame, which is worse than no picture at all.
    for viewer in s.viewers:
        for pid in viewer.get("pids", []):
            _kill(pid)
    for pid in (s.client_pid, s.xvfb_pid):
        _kill(pid)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (_alive(s.client_pid) or _alive(s.xvfb_pid)):
        time.sleep(0.2)
    for pid in (s.client_pid, s.xvfb_pid):
        _kill(pid, signal.SIGKILL)
    forget(target)
    return True


def _kill(pid: int, sig: int = signal.SIGTERM) -> None:
    if not pid or pid <= 0:  # never signal our own process group by accident
        return
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError, TypeError):
        pass


# -- driving ----------------------------------------------------------------


def _xdo(s: Session, *args: str, timeout: float = 20.0) -> str:
    p = subprocess.run(
        [_require("xdotool"), *args],
        env={**os.environ, "DISPLAY": s.display},
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if p.returncode != 0:
        raise SessionError(f"xdotool {' '.join(args)} failed: {p.stderr.strip()[:300]}")
    return p.stdout


def check_point(s: Session, x: int, y: int, proven: str | None) -> None:
    """Both halves of the contract, at the point of action."""
    require_match(s.geom, proven, what=f"click at {x},{y}")
    g = s.geom
    if not (0 <= x < g.width and 0 <= y < g.height):
        raise SessionError(
            f"refusing {x},{y}: outside the pinned surface {g.stamp}. A coordinate off "
            f"the surface is a rotted one, not a near miss."
        )


def _direct(s: Session) -> bool:
    return s.backend == clients.BACKEND_RFB


def _rfb_client(s: Session, *, what: str, require_contract: bool) -> rfb.RfbClient:
    """Connect for one act, and hold the far end to the contract when it matters.

    ``require_contract`` is the honest split between looking and acting.  A
    screenshot of a surface that has changed size is still true and still worth
    having; a CLICK on one is a coordinate replayed against a surface it was
    never proven on, which is precisely what the contract exists to refuse.
    """
    target = load_target(s.target)
    password = resolve_password(target, required=False)
    host, port = s.endpoint
    with _rfb_errors(what):
        client = rfb.RfbClient.connect(host, port, password=password, timeout=RFB_TIMEOUT)
    observed = client.geometry
    if observed != s.server_geometry:
        s.server_geometry = observed
        save(s)
    if require_contract and observed != s.geometry:
        client.close()
        raise SessionError(
            f"refusing {what}: this session is pinned at {s.geometry} but the far end is "
            f"now {observed}. A coordinate proven on one surface means nothing on "
            f"another — re-measure, then update the target's [geometry] if the change "
            f"is the new truth. Screenshots still work at the size it really is."
        )
    return client


def click(s: Session, x: int, y: int, *, button: int = 1, proven: str | None = None) -> None:
    check_point(s, x, y, proven)
    if _direct(s):
        with _rfb_client(s, what=f"click at {x},{y}", require_contract=True) as c:
            with _rfb_errors("click"):
                c.click(x, y, button=button)
    else:
        _xdo(s, "mousemove", "--sync", str(x), str(y))
        _xdo(s, "click", str(button))
    s.last_verb = f"click {x},{y}"
    save(s)


def type_text(s: Session, text: str, *, delay_ms: int = 12) -> None:
    if _direct(s):
        # Typing is geometry-free — it goes to whatever has focus — so it does
        # not need the contract to hold.
        with _rfb_client(s, what="type", require_contract=False) as c:
            with _rfb_errors("type"):
                c.type_text(text, delay_ms=delay_ms)
    else:
        _xdo(s, "type", "--delay", str(delay_ms), "--", text)
    s.last_verb = "type"
    save(s)


def key(s: Session, chord: str, *, hold_ms: int = 0) -> None:
    """Strike a chord, optionally holding it down.

    The hold is not decoration.  Firmware, boot pickers and BIOS-era menus poll
    the keyboard on a slow loop and can miss a press released within a frame,
    which looks exactly like "keys never arrive" — the most misleading symptom
    there is, because it sends the next person to debug the input path that was
    working all along.
    """
    if _direct(s):
        with _rfb_client(s, what=f"key {chord}", require_contract=False) as c:
            with _rfb_errors("key"):
                c.press(chord, hold_ms=hold_ms)
    elif hold_ms > 0:
        mods, _, key_name = chord.rpartition("+")
        for mod in [m for m in mods.split("+") if m]:
            _xdo(s, "keydown", mod)
        _xdo(s, "keydown", key_name)
        time.sleep(hold_ms / 1000.0)
        _xdo(s, "keyup", key_name)
        for mod in reversed([m for m in mods.split("+") if m]):
            _xdo(s, "keyup", mod)
    else:
        _xdo(s, "key", "--clearmodifiers", chord)
    s.last_verb = f"key {chord}"
    save(s)


def screenshot(
    s: Session, out: Path, *, rect: tuple[int, int, int, int] | None = None
) -> dict[str, Any]:
    """Capture the surface, crop-first per the ladder when asked.

    Returns what was actually captured rather than only where it was written:
    the size the far end really is, and whether every rectangle had arrived.  A
    partly-painted frame is a good diagnostic and a terrible thing to mistake
    for a whole one.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    if _direct(s):
        with _rfb_client(s, what="screenshot", require_contract=False) as c:
            with _rfb_errors("screenshot"):
                frame = c.capture()
        if rect:
            frame = frame.crop(*rect)
        frame.write_png(out)
        result = {
            "path": str(out),
            "geometry": s.geometry,
            "observed": s.server_geometry,
            "complete": frame.complete,
        }
    else:
        env = {**os.environ, "DISPLAY": s.display}
        grabber = which("import") or which("magick")
        if grabber is None:
            raise SessionError("no ImageMagick on this host, so the pixel rung cannot capture")
        argv = [grabber] + ([] if grabber.endswith("import") else ["import"])
        argv += ["-window", "root"]
        if rect:
            x, y, w, h = rect
            g = s.geom
            if x < 0 or y < 0 or x + w > g.width or y + h > g.height:
                raise SessionError(f"refusing crop {rect}: not inside the pinned surface {g.stamp}")
            argv += ["-crop", f"{w}x{h}+{x}+{y}"]
        argv.append(str(out))
        p = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            raise SessionError(f"capture failed: {p.stderr.strip()[:300]}")
        result = {
            "path": str(out),
            "geometry": s.geometry,
            "observed": s.geometry,
            "complete": True,
        }
    s.last_verb = "screenshot"
    save(s)
    return result


def describe(s: Session) -> dict[str, Any]:
    return {
        **asdict(s),
        "alive": s.alive(),
        "age_s": round(time.time() - s.opened_at, 1),
        "viewer_count": len([v for v in s.viewers if _alive((v.get("pids") or [0])[0])]),
    }
