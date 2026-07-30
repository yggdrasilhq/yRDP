"""RDP sessions — one connection per target, pinned to the declared geometry.

There is no daemon in v0, and that is a decision rather than a shortcut: the
session IS a process pair (a headless X server at exactly the contract geometry,
and an RDP client inside it), so the operating system already keeps the state a
daemon would otherwise hold.  What we persist is a small record of that pair, so
any later invocation — or any other agent on the host — can find, drive and
close a session it did not open.

The geometry contract is enforced here, at the point of action:

* the X server is created at the target's declared size, so nothing downstream
  can renegotiate it;
* ``+dynamic-resolution`` and ``/smart-sizing`` are deliberately NOT passed —
  either one would let the far end resize the surface under our coordinates;
* a coordinate replayed from lore proven at another geometry is refused, and a
  coordinate outside the surface is refused too.
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from shutil import which
from typing import Any

from .config import SURFACE_VIEWPORT, Target, state_dir
from .geometry import Geometry, require_match

CONNECT_TIMEOUT = 45.0


class SessionError(Exception):
    """A session could not be opened, found, or driven."""


class CredentialUnavailable(SessionError):
    """The credential exists but this process may not have it.

    Named separately because the recovery is the operator's, not ours: unlock
    the vault.  Reaching around it — setting a password over an admin channel we
    happen to hold — would be changing the operator's credential to suit our
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
    rdp_pid: int
    host: str
    user: str | None
    opened_at: float
    window_found: bool = False
    lease_until: float | None = None
    last_verb: str = "open"
    events: list[str] = field(default_factory=list)

    @property
    def geom(self) -> Geometry:
        return Geometry.parse(self.geometry)

    def alive(self) -> bool:
        return _alive(self.rdp_pid) and _alive(self.xvfb_pid)


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
    out = []
    for p in sorted(sessions_dir().glob("*.json")):
        if s := load(p.stem):
            out.append(s)
    return out


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
            f"{binary} is not installed on this host; the RDP lane needs Xvfb, "
            f"xfreerdp3 and xdotool"
        )
    return path


def _free_display(start: int = 90, end: int = 120) -> str:
    used = {p.name.lstrip("X") for p in Path("/tmp/.X11-unix").glob("X*")} if Path(
        "/tmp/.X11-unix"
    ).is_dir() else set()
    for n in range(start, end):
        if str(n) not in used and not Path(f"/tmp/.X{n}-lock").exists():
            return f":{n}"
    raise SessionError(f"no free X display between :{start} and :{end}")


# -- credentials ------------------------------------------------------------


def resolve_password(target: Target, entry: str | None = None) -> str:
    """Fetch the RDP password by NAME, never by value from config.

    Order: an explicit environment override (useful for a one-shot proof), then
    the vault entry the target names.  The vault is the single source of truth
    for secrets across this fleet; yRDP does not get a second one.
    """
    if env := os.environ.get("YRDP_RDP_PASSWORD"):
        return env
    name = entry or (target.connection.password_vault_entry if target.connection else None)
    if not name:
        raise CredentialUnavailable(
            f"target {target.name!r} names no RDP credential. Add "
            f"password_vault_entry to [connection], or pass --password-entry, "
            f"or set YRDP_RDP_PASSWORD for a one-shot."
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
            f"route around that."
        )
    return p.stdout.rstrip("\n")


# -- the client command -----------------------------------------------------

#: Flags that would hand the far end the power to resize our surface.  A session
#: that renegotiates its geometry mid-flight invalidates every coordinate in the
#: lore without erroring, which is the exact failure the contract exists to
#: prevent — so they are named here and locked out by test, not merely omitted.
FORBIDDEN_CLIENT_FLAGS = ("dynamic-resolution", "smart-sizing")


def client_argv(target: Target, *, client: str = "xfreerdp3") -> list[str]:
    """The connection arguments for a target, pinned to its geometry.

    Secret-free by construction.  The password joins this list only inside the
    file descriptor built by :func:`arg_stream`, so it never reaches the process
    argv that ``ps`` shows every user on the host.
    """
    if target.connection is None:
        raise SessionError(f"target {target.name!r} declares no [connection] endpoint")
    geom = target.geometry
    argv = [
        client,
        f"/v:{target.connection.host}:{target.connection.port}",
        f"/size:{geom.width}x{geom.height}",
        "/cert:ignore",
        "/gdi:sw",
        "/log-level:WARN",
        "+auto-reconnect",
    ]
    if target.connection.user:
        argv.insert(2, f"/u:{target.connection.user}")
    if target.connection.domain:
        argv.insert(2, f"/d:{target.connection.domain}")
    if target.connection.security:
        argv.insert(2, f"/sec:{target.connection.security}")
    return argv


def arg_stream(connection_args: list[str], password: str) -> bytes:
    """The argument list FreeRDP reads from an inherited file descriptor.

    ``/from-stdin`` was the obvious way to keep a password off the command line
    and it is the wrong one: the client calls ``tcsetattr`` to stop the terminal
    echoing, that fails on a pipe with "Inappropriate ioctl for device", and the
    connection dies at "NLA begin failed" — a failure that reads like a broken
    credential rather than a broken channel.  Proven on the live host.

    ``/args-from:fd:`` has none of that.  The secret crosses one anonymous pipe
    into the child and exists nowhere else: not in argv, not in the environment,
    not on disk.  One argument per line, and this option may not be combined
    with any other, so the whole connection goes through it.
    """
    return ("\n".join([*connection_args, f"/p:{password}"]) + "\n").encode()


# -- open / close -----------------------------------------------------------


def open_session(
    target: Target,
    *,
    password_entry: str | None = None,
    connect_timeout: float = CONNECT_TIMEOUT,
    force: bool = False,
) -> Session:
    if target.connection is None:
        raise SessionError(f"target {target.name!r} declares no [connection] endpoint")

    if (existing := load(target.name)) and existing.alive() and not force:
        raise SessionError(
            f"{target.name} already has a live session on {existing.display} "
            f"(pinned {existing.geometry}); close it or pass --force"
        )
    if existing:
        close_session(target.name, quiet=True)

    if target.surface_mode == SURFACE_VIEWPORT:
        raise SessionError(
            f"target {target.name!r} asks for a viewport surface. That lane — the "
            f"session composited into the yggterm viewport as a libyggterm surface — "
            f"is designed but NOT BUILT, and yRDP will not quietly hand you a headless "
            f"shadow instead. Set surface mode to 'shadow' to drive it agentically."
        )
    if not reachable(target):
        raise SessionError(
            f"{target.connection.host}:{target.connection.port} is not answering. yRDP "
            f"does not know why — that is site knowledge. If this target declares an "
            f"'up' hook, `yrdp up --target {target.name}` runs it."
        )

    password = resolve_password(target, password_entry)

    xvfb, client, xdotool = _require("Xvfb"), _require("xfreerdp3"), _require("xdotool")
    geom = target.geometry
    display = _free_display()

    xvfb_proc = subprocess.Popen(
        [xvfb, display, "-screen", "0", f"{geom.width}x{geom.height}x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    _await_display(display, xvfb_proc)

    env = {**os.environ, "DISPLAY": display}
    connection = client_argv(target, client=client)

    read_fd, write_fd = os.pipe()
    os.write(write_fd, arg_stream(connection[1:], password))
    os.close(write_fd)
    try:
        rdp_proc = subprocess.Popen(
            [client, f"/args-from:fd:{read_fd}"],
            env=env,
            pass_fds=(read_fd,),
            # No stdin at all: the client must never be able to sit at a prompt
            # waiting for a credential we have already given it.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    finally:
        os.close(read_fd)

    session = Session(
        target=target.name,
        geometry=geom.stamp,
        display=display,
        xvfb_pid=xvfb_proc.pid,
        rdp_pid=rdp_proc.pid,
        host=f"{target.connection.host}:{target.connection.port}",
        user=target.connection.user,
        opened_at=time.time(),
    )

    try:
        session.window_found = _await_window(display, xdotool, rdp_proc, connect_timeout, session)
    except BaseException:
        # A session that failed to open must not leave a headless X server and a
        # half-connected client behind. They would hold a display number, show up
        # in nobody's session list, and be found later by whoever wonders what is
        # eating the host.
        _reap(rdp_proc, xvfb_proc)
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


#: Every FreeRDP connection failure is reported with one of these prefixes.  We
#: watch for it in the client's stderr AS IT RUNS, because a client that has
#: already been told "no" may still be sitting there alive.
FATAL_MARKER = "ERRCONNECT_"


def _drain(stream, buf: list[str]) -> str:
    """Read whatever the client has said so far without blocking on it.

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
    display: str, xdotool: str, proc: subprocess.Popen, timeout: float, session: Session
) -> bool:
    """Wait for the client to paint, or for it to tell us why it will not."""
    deadline = time.monotonic() + timeout
    buf: list[str] = []
    while time.monotonic() < deadline:
        text = _drain(proc.stderr, buf)
        if (code := proc.poll()) is not None:
            raise _classify(code, text + (proc.stderr.read() or b"").decode(errors="replace"))
        if FATAL_MARKER in text:
            # It answered; do not make the caller wait out the timeout for it.
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.kill()
            raise _classify(proc.returncode or -1, text)
        found = subprocess.run(
            [xdotool, "search", "--onlyvisible", "--class", "."],
            env={**os.environ, "DISPLAY": display},
            capture_output=True,
            text=True,
        )
        if found.returncode == 0 and found.stdout.strip():
            session.events.append(f"window mapped after {time.monotonic() - deadline + timeout:.1f}s")
            return True
        time.sleep(0.5)
    # A live client with no mapped window is not a failure we should invent a
    # cause for: report it and let the caller look with a screenshot.
    session.events.append("client alive but no window mapped before the deadline")
    return False


def _classify(code: int, stderr: str) -> SessionError:
    if "LOGON_FAILURE" in stderr or "ERRCONNECT_LOGON_FAILURE" in stderr:
        return AuthRefused(
            "the guest refused the credential (NLA logon failure). The RDP service is "
            "healthy — this is the password, not the plumbing."
        )
    if "ACCOUNT_DISABLED" in stderr or "ACCOUNT_RESTRICTION" in stderr:
        return AuthRefused(
            "the account is disabled or restricted for network logon. A blank password "
            "is refused for RDP by default policy even when console logon allows it."
        )
    if "ERRCONNECT_CONNECT_FAILED" in stderr or "ERRCONNECT_CONNECT_TRANSPORT_FAILED" in stderr:
        return SessionError(
            "could not reach the RDP service. On this substrate that means the VM is "
            "not running — check `yrdp vm state`."
        )
    tail = "\n".join(line for line in stderr.splitlines() if "ERROR" in line)[-600:]
    return SessionError(f"the RDP client exited with {code}: {tail or stderr[-400:]}")


def close_session(target: str, *, quiet: bool = False) -> bool:
    s = load(target)
    if s is None:
        if quiet:
            return False
        raise SessionError(f"no recorded session for {target}")
    for pid in (s.rdp_pid, s.xvfb_pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (_alive(s.rdp_pid) or _alive(s.xvfb_pid)):
        time.sleep(0.2)
    for pid in (s.rdp_pid, s.xvfb_pid):
        if _alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    forget(target)
    return True


# -- driving ----------------------------------------------------------------


def live_session(target: str) -> Session:
    s = load(target)
    if s is None:
        raise SessionError(f"no session for {target}; `yrdp ctl open --target {target}` first")
    if not s.alive():
        forget(target)
        raise SessionError(
            f"the recorded session for {target} is gone (client or X server exited); "
            f"the record has been cleared, open a new one"
        )
    return s


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
    }
