"""Viewers — revealing a live session in the yggterm viewport.

**A session must never exist in a vacuum.** The canonical surface is pinned at
the contract geometry and driven from the command line, but it has to be
watchable at any moment, by a person, alongside whatever an agent is doing to
it.  That is co-browse, and it is not an extra: an agent surface nobody can look
at is an agent surface nobody can trust.

The rule that makes it work, and the reason it is easier here than for a
terminal: **every viewer scales, no viewer resizes.** A terminal session's grid
IS its geometry, so two viewers at different sizes fight over the one
authoritative number.  A remote-GUI session's geometry is a contract we pinned
ourselves and the framebuffer is fixed-size by construction, so any number of
viewers can attach at their own window sizes and simply be scaled.  The same
rule that keeps coordinate lore replayable is what makes N-viewer co-browse fall
out for free — do not let a viewer renegotiate the surface, however convenient
it looks.

The path, which needs no changes to yggterm at all:

    canonical surface (headless display, contract geometry)
      └── a VNC server exporting it, shared so viewers do not evict each other
            └── a websocket bridge + a self-contained browser client on loopback
                  └── OSC 7717 web-surface open/heartbeat/close
                        └── the yggterm viewport

⚠ Do not conflate the two VNCs.  Here we SERVE VNC so that viewers can watch.  A
``--vnc`` *target* means we are a VNC CLIENT of some far end.  Opposite
directions, different code paths; a future reader will be tempted to unify them.
"""

from __future__ import annotations

import base64
import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from shutil import which

from . import session as sessions
from .session import Session, SessionError

#: yggterm's web-surface control channel, emitted on our own stdout.  Because the
#: transport is the terminal byte stream, it works identically for a local or a
#: remote session, and a plain terminal ignores it.
OSC_PREFIX = "\033]7717;web-surface;"
OSC_BEL = "\007"

#: The daemon expires a surface that stops speaking, so a killed viewer never
#: leaves a stuck overlay.  Beat comfortably inside that window.
HEARTBEAT_SECONDS = 4.0

NOVNC_ROOTS = ("/usr/share/novnc", "/usr/share/webapps/novnc", "/opt/novnc")


class ViewError(SessionError):
    """A viewer could not be attached."""


@dataclass(slots=True)
class Viewer:
    target: str
    vnc_port: int
    web_port: int
    url: str
    pids: list[int]
    read_only: bool
    started_at: float

    def as_dict(self) -> dict:
        return {
            "target": self.target,
            "vnc_port": self.vnc_port,
            "web_port": self.web_port,
            "url": self.url,
            "pids": self.pids,
            "read_only": self.read_only,
            "started_at": self.started_at,
        }


def _free_port(start: int = 5990, end: int = 6090) -> int:
    for port in range(start, end):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise ViewError(f"no free loopback port between {start} and {end}")


def _novnc_root() -> str:
    for root in NOVNC_ROOTS:
        if Path(root, "vnc.html").is_file():
            return root
    raise ViewError(
        "no noVNC installation found. The reveal serves a browser client from disk; "
        f"install noVNC (looked in {', '.join(NOVNC_ROOTS)})."
    )


def emit(action: str, payload: dict) -> None:
    """Announce a surface to yggterm on our own stdout."""
    blob = base64.b64encode(json.dumps(payload).encode()).decode()
    sys.stdout.write(f"{OSC_PREFIX}{action};{blob}{OSC_BEL}")
    sys.stdout.flush()


def in_yggterm() -> bool:
    """A yggterm-owned PTY exports this; its absence means nobody will listen."""
    return bool(os.environ.get("YGGTERM_SESSION_ID"))


def attach(
    s: Session | None,
    *,
    read_only: bool = False,
    title: str | None = None,
    endpoint: tuple[str, int] | None = None,
    label: str = "",
) -> Viewer:
    """Reveal a surface and announce it, without disturbing what is behind it.

    Two shapes, and the difference is worth stating because it removes work
    rather than adding it:

    * an **RDP session** has no framebuffer anyone else can read, so we export
      the pinned display over VNC and bridge that;
    * a **VNC endpoint is already a framebuffer protocol**, so there is nothing
      to export — the bridge points straight at it. No X server, no viewer, no
      window manager in the path, and one less thing to go wrong.
    """
    websockify = which("websockify")
    if not websockify:
        raise ViewError(
            "the reveal needs websockify on this host; install it, or use "
            "`yrdp screenshot` for a still frame"
        )
    root = _novnc_root()
    web_port = _free_port(6100, 6200)
    pids: list[int] = []

    if endpoint is not None:
        vnc_host, vnc_port = endpoint
    else:
        if s is None:
            raise ViewError("a reveal needs either a session or an endpoint")
        x11vnc = which("x11vnc")
        if not x11vnc:
            raise ViewError("exporting a session's display needs x11vnc on this host")
        vnc_host, vnc_port = "127.0.0.1", _free_port()

        argv = [
            x11vnc,
            "-display", s.display,
            "-rfbport", str(vnc_port),
            "-localhost",      # never off-box; the tunnel is yggterm's job
            "-nopw",           # loopback-only, and the surface is already gated
            "-shared",         # THE co-browse flag: viewers do not evict each other
            "-forever",        # the session outlives any one viewer
            "-noxdamage",
        ]
        if read_only:
            argv.append("-viewonly")
        vnc = subprocess.Popen(
            argv, env=_clean_env(s.display), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        _await_port(vnc_port, vnc, "x11vnc")
        pids.append(vnc.pid)

    bridge = subprocess.Popen(
        [websockify, "--web", root, f"127.0.0.1:{web_port}", f"{vnc_host}:{vnc_port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    _await_port(web_port, bridge, "websockify")
    pids.append(bridge.pid)

    # resize=scale IS the contract in URL form: the viewer scales to its window
    # and never asks the far end to change size.
    url = (
        f"http://127.0.0.1:{web_port}/vnc.html"
        f"?autoconnect=1&reconnect=1&resize=scale&show_dot=1"
        + ("&view_only=1" if read_only else "")
    )
    viewer = Viewer(
        target=s.target if s else label,
        vnc_port=vnc_port,
        web_port=web_port,
        url=url,
        pids=pids,
        read_only=read_only,
        started_at=time.time(),
    )
    if s is not None:
        s.viewers.append(viewer.as_dict())
        sessions.save(s)

    emit("open", {
        "session": os.environ.get("YGGTERM_SESSION_ID", ""),
        "url": url,
        "title": title or (f"{s.target} ({s.geometry})" if s else label),
    })
    return viewer


#: Desktop-session variables that must not reach a headless exporter.  Both were
#: learned the hard way on a real workstation:
#:
#: * ``WAYLAND_DISPLAY`` — x11vnc sees it and refuses outright ("Wayland sessions
#:   are as of now only supported…"), even though we are pointing it at our own
#:   Xvfb. The inherited variable describes the OPERATOR'S session, which has
#:   nothing to do with the surface we are exporting.
#: * ``XAUTHORITY`` — points at that session's cookie, which our private display
#:   will refuse; the headless display has no auth file of its own to offer.
#:
#: This is the same class of bug as a daemon's frozen environment poisoning every
#: process it spawns: an inherited variable that describes a different world.
INHERITED_DESKTOP_VARS = ("WAYLAND_DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE")


def _clean_env(display: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in INHERITED_DESKTOP_VARS}
    env["DISPLAY"] = display
    return env


def _await_port(port: int, proc: subprocess.Popen, what: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            err = (proc.stderr.read() or b"").decode(errors="replace").strip()[:300]
            raise ViewError(f"{what} exited before it listened on {port}: {err}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise ViewError(f"{what} never listened on {port}")


def detach(s: Session | None, viewer: Viewer | None = None) -> int:
    """Close viewers and tell yggterm.  The SESSION IS UNTOUCHED — that is the point."""
    doomed = [viewer.as_dict()] if viewer else list(s.viewers if s else [])
    for v in doomed:
        for pid in v.get("pids", []):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, TypeError):
                pass
    if s is not None:
        s.viewers = [] if viewer is None else [v for v in s.viewers if v != viewer.as_dict()]
        sessions.save(s)
    emit("close", {"session": os.environ.get("YGGTERM_SESSION_ID", "")})
    return len(doomed)


def hold(s: Session | None, viewer: Viewer, *, interval: float = HEARTBEAT_SECONDS) -> None:
    """Keep the surface alive until interrupted.

    The heartbeat is the liveness truth rather than a courtesy: a surface that
    stops speaking is reaped, which is exactly why a SIGKILLed viewer cannot
    leave a stuck overlay behind.  It also carries the full payload, so a
    terminal remount replays it and the surface heals itself.
    """
    payload = {
        "session": os.environ.get("YGGTERM_SESSION_ID", ""),
        "url": viewer.url,
        "title": f"{s.target} ({s.geometry})" if s else viewer.target,
    }

    def _stop(signum, frame):  # noqa: ARG001
        raise KeyboardInterrupt

    # SIGTERM must unwind through the same path as Ctrl-C, or a `timeout`, a
    # logout or a supervisor restart leaves the exporter and the bridge running
    # with nobody announcing them — orphans holding ports that the next reveal
    # then has to route around.
    previous = signal.signal(signal.SIGTERM, _stop)
    try:
        while True:
            if s is not None and not s.alive():
                print(
                    f"[yrdp] the session behind this view exited; closing the surface",
                    file=sys.stderr,
                )
                break
            emit("heartbeat", payload)
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous)
        detach(s, viewer)
