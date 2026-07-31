"""Client adapters — the one protocol-shaped seam in the whole tool.

Everything above this module is protocol-independent: the geometry contract and
its refusal, session records, viewers, lore, hooks, credential resolution, the
verb set.  What differs per protocol is only *how a surface is held and driven*,
and there are exactly two answers:

* **the x11 backend** — a headless display pinned at the contract geometry with
  a real client binary living inside it, driven by ``xdotool`` and photographed
  by ImageMagick.  RDP needs this, because speaking RDP means running an RDP
  client;
* **the rfb backend** — no display, no binary, no window manager: we speak the
  protocol ourselves (``rfb.py``).  VNC gets this, because VNC *is* a
  framebuffer protocol and putting a framebuffer emulator in front of one buys
  nothing.  It was tried the other way first; the viewer authenticated and then
  painted nothing headless, while wedging other X clients on the display.

That is still one tool, not two.  The backend seam is a dozen lines wide and
sits underneath everything that actually carries risk.

Every protocol owes four things, in whichever form its backend expresses them:

1. a surface pinned to the contract geometry;
2. a named, testable refusal of whatever would let the FAR END resize us — for a
   spawned client that is a flag list, for our own client it is the pseudo-
   encodings we never advertise (``rfb.FORBIDDEN_ENCODINGS``).  A mid-session
   resize invalidates every coordinate in the lore and raises nothing;
3. delivery of the secret by a channel ``ps`` cannot read;
4. failure classification into the SAME named outcomes, because the caller's
   recovery differs by outcome and must not depend on which protocol it is.
   The x11 backend reads its client's stderr for those; the rfb backend raises
   them as distinct exception types, which is the same contract with fewer
   spellings to get wrong.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

from .config import PROTOCOL_RDP, PROTOCOL_VNC, Target
from .geometry import Geometry


@dataclass(slots=True)
class Spawned:
    """A running client, plus whatever must be closed after it starts."""

    proc: subprocess.Popen
    fds_to_close: tuple[int, ...] = ()

    def release(self) -> None:
        for fd in self.fds_to_close:
            try:
                os.close(fd)
            except OSError:  # pragma: no cover
                pass


#: A client binary living in a headless X display we created.
BACKEND_X11 = "x11"
#: The protocol spoken directly, by us, with nothing in front of it.
BACKEND_RFB = "rfb"


@dataclass(frozen=True, slots=True)
class Adapter:
    binary: str
    #: How a surface for this protocol is held and driven.
    backend: str = BACKEND_X11
    #: Flags that hand the far end control of our geometry.  Named so a test can
    #: assert their absence; see tests/test_contracts.py.  A direct backend has
    #: no argv to keep clean — its equivalent lock is the pseudo-encodings it
    #: never advertises, in ``rfb.FORBIDDEN_ENCODINGS``.
    forbidden: tuple[str, ...] = ()
    #: Markers in the client's own stderr that mean "the credential was refused"
    #: as opposed to "I could not reach it".
    auth_markers: tuple[str, ...] = ()
    #: Markers that mean the endpoint could not be reached at all.
    unreachable_markers: tuple[str, ...] = ()
    #: A marker whose appearance means the attempt is over, so a caller need not
    #: wait out its whole connect timeout for an answer already given.
    fatal_marker: str = ""
    #: Whether a credential is required to connect at all.  RDP effectively
    #: always needs one, so a target that names none is a configuration mistake
    #: worth refusing early.  A VNC endpoint may legitimately offer no
    #: authentication — a hypervisor console on loopback is the common case —
    #: and refusing to connect to one would be inventing a requirement.
    credential_required: bool = True


RDP = Adapter(
    binary="xfreerdp3",
    backend=BACKEND_X11,
    forbidden=("dynamic-resolution", "smart-sizing"),
    auth_markers=("LOGON_FAILURE", "ACCOUNT_DISABLED", "ACCOUNT_RESTRICTION"),
    unreachable_markers=("ERRCONNECT_CONNECT_FAILED", "ERRCONNECT_CONNECT_TRANSPORT_FAILED"),
    fatal_marker="ERRCONNECT_",
)

#: VNC has no client binary at all any more.  There is nothing to spawn, nothing
#: to keep out of ``ps``, and no stderr to pattern-match: the outcomes arrive as
#: ``rfb.RfbAuthError`` and ``rfb.RfbError``, which cannot be misspelled the way
#: a marker string can.  The empty marker tuples are not an omission — they are
#: the honest statement that this protocol classifies by type, not by text.
VNC = Adapter(
    binary="",
    backend=BACKEND_RFB,
    credential_required=False,
)

ADAPTERS = {PROTOCOL_RDP: RDP, PROTOCOL_VNC: VNC}


def adapter_for(target: Target, override: str | None = None) -> Adapter:
    protocol = override or (target.connection.protocol if target.connection else PROTOCOL_RDP)
    return ADAPTERS[protocol]


def connection_argv(target: Target, *, protocol: str | None = None, binary: str | None = None) -> list[str]:
    """The connection arguments, pinned to the contract geometry, secret-free.

    The password never appears here.  It reaches the client by a channel the
    adapter chooses in :func:`spawn`, so that this list can be logged, tested and
    shown to a user without leaking anything.
    """
    conn = target.connection
    if conn is None:
        raise ValueError(f"target {target.name!r} declares no [connection] endpoint")
    proto = protocol or conn.protocol
    adapter = ADAPTERS[proto]
    if adapter.backend != BACKEND_X11:
        raise ValueError(
            f"{proto} is spoken directly by this tool, so it has no client argv. "
            f"Its geometry lock is rfb.FORBIDDEN_ENCODINGS, not a flag list."
        )
    exe = binary or adapter.binary
    geom: Geometry = target.geometry

    argv = [
        exe,
        f"/v:{conn.host}:{conn.port}",
        f"/size:{geom.width}x{geom.height}",
        "/cert:ignore",
        "/gdi:sw",
        "/log-level:WARN",
        "+auto-reconnect",
    ]
    if conn.user:
        argv.insert(2, f"/u:{conn.user}")
    if conn.domain:
        argv.insert(2, f"/d:{conn.domain}")
    if conn.security:
        argv.insert(2, f"/sec:{conn.security}")
    return argv


def arg_stream(connection_args: list[str], password: str) -> bytes:
    """FreeRDP's ``/args-from:fd:`` payload — one argument per line.

    ``/from-stdin`` was the obvious way to keep a password off the command line
    and it is the wrong one: the client calls ``tcsetattr`` to stop the terminal
    echoing, that fails on a pipe with "Inappropriate ioctl for device", and the
    connection dies at "NLA begin failed" — a failure that reads like a rejected
    credential when the credential never arrived.  Proven on the live host.
    """
    return ("\n".join([*connection_args, f"/p:{password}"]) + "\n").encode()


def spawn(
    target: Target,
    password: str | None,
    env: dict[str, str],
    *,
    protocol: str | None = None,
    binary: str | None = None,
) -> Spawned:
    """Start the client, giving it the secret by a channel ``ps`` cannot read.

    Only the x11 backend spawns anything; a directly-spoken protocol has no
    process to start, which is why ``session.py`` never reaches this for one.
    """
    conn = target.connection
    proto = protocol or (conn.protocol if conn else PROTOCOL_RDP)
    argv = connection_argv(target, protocol=proto, binary=binary)
    exe = binary or ADAPTERS[proto].binary

    if password is None:
        raise ValueError(f"{proto} needs a credential; none was resolved")
    # The whole invocation crosses one anonymous pipe as an inherited fd, so the
    # secret exists nowhere else: not argv, not the environment, not disk.
    read_fd, write_fd = os.pipe()
    os.write(write_fd, arg_stream(argv[1:], password))
    os.close(write_fd)
    proc = subprocess.Popen(
        [exe, f"/args-from:fd:{read_fd}"],
        env=env,
        pass_fds=(read_fd,),
        # No stdin at all: the client must never sit at a prompt waiting for a
        # credential it has already been given.
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return Spawned(proc, (read_fd,))
