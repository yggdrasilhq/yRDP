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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from shutil import which
from typing import Any

from . import clients
from .config import ConfigError, Target, state_dir
from .geometry import Geometry, require_match

CONNECT_TIMEOUT = 45.0


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
    #: Attached viewers, appended by view.py.  A session with none is still a
    #: perfectly good session — it is simply unwatched at this moment.
    viewers: list[dict] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    @property
    def geom(self) -> Geometry:
        return Geometry.parse(self.geometry)

    def alive(self) -> bool:
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


def resolve_password(target: Target, entry: str | None = None) -> str:
    """Fetch the password by NAME, never by value from config.

    Order: an explicit environment override (useful for a one-shot proof), then
    the vault entry the target names.  The vault is the single source of truth
    for secrets; yRDP does not get a second one.
    """
    if env := os.environ.get("YRDP_RDP_PASSWORD"):
        return env
    name = entry or (target.connection.password_vault_entry if target.connection else None)
    if not name:
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
    password = resolve_password(target, password_entry)

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


def click(s: Session, x: int, y: int, *, button: int = 1, proven: str | None = None) -> None:
    check_point(s, x, y, proven)
    _xdo(s, "mousemove", "--sync", str(x), str(y))
    _xdo(s, "click", str(button))
    s.last_verb = f"click {x},{y}"
    save(s)


def type_text(s: Session, text: str, *, delay_ms: int = 12) -> None:
    _xdo(s, "type", "--delay", str(delay_ms), "--", text)
    s.last_verb = "type"
    save(s)


def key(s: Session, chord: str) -> None:
    _xdo(s, "key", "--clearmodifiers", chord)
    s.last_verb = f"key {chord}"
    save(s)


def screenshot(s: Session, out: Path, *, rect: tuple[int, int, int, int] | None = None) -> Path:
    """Capture the pinned surface, crop-first per the ladder when asked."""
    out.parent.mkdir(parents=True, exist_ok=True)
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
    s.last_verb = "screenshot"
    save(s)
    return out


def describe(s: Session) -> dict[str, Any]:
    return {
        **asdict(s),
        "alive": s.alive(),
        "age_s": round(time.time() - s.opened_at, 1),
        "viewer_count": len([v for v in s.viewers if _alive((v.get("pids") or [0])[0])]),
    }
