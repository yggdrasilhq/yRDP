"""Client adapters — the one protocol-shaped seam in the whole tool.

Everything above this module is protocol-independent: the geometry contract and
its refusal, session records, viewers, lore, hooks, credential resolution, the
verb set.  Only *which binary is spawned into the pinned display, with which
flags, and how the secret reaches it* differs between RDP and VNC.

That is why there is no second tool.  A separate VNC client would be a second
copy of all the machinery above, and two copies of one idea drift — usually
within a month, and always in the direction that costs a debugging session.

Every adapter owes four things:

1. an argv that pins the surface to the contract geometry;
2. a named list of flags that would let the FAR END resize us, so they can be
   locked out by test rather than merely left off — a mid-session resize
   invalidates every coordinate in the lore and raises nothing;
3. delivery of the secret that keeps it out of argv, where ``ps`` shows it to
   every user on the host;
4. failure classification into the SAME named outcomes, because the caller's
   recovery differs by outcome and must not depend on which protocol it is.
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


@dataclass(frozen=True, slots=True)
class Adapter:
    binary: str
    #: Flags that hand the far end control of our geometry.  Named so a test can
    #: assert their absence; see tests/test_contracts.py.
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
    forbidden=("dynamic-resolution", "smart-sizing"),
    auth_markers=("LOGON_FAILURE", "ACCOUNT_DISABLED", "ACCOUNT_RESTRICTION"),
    unreachable_markers=("ERRCONNECT_CONNECT_FAILED", "ERRCONNECT_CONNECT_TRANSPORT_FAILED"),
    fatal_marker="ERRCONNECT_",
)

VNC = Adapter(
    binary="xtigervncviewer",
    # RemoteResize=1 is TigerVNC's exact analogue of RDP's dynamic-resolution:
    # it resizes the REMOTE desktop to match the local window. That is precisely
    # the silent rot the geometry contract exists to prevent.
    forbidden=("RemoteResize=1", "Scaling="),
    auth_markers=("Authentication failure", "authentication failed", "Too many auth"),
    unreachable_markers=("unable to connect", "Connection refused", "No route to host"),
    fatal_marker="",
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
    exe = binary or adapter.binary
    geom: Geometry = target.geometry

    if proto == PROTOCOL_RDP:
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

    # VNC. RemoteResize=0 is not optional: it is the contract in flag form.
    return [
        exe,
        f"{conn.host}::{conn.port}" if conn.port != 5900 else conn.host,
        "-RemoteResize=0",
        f"-geometry={geom.width}x{geom.height}+0+0",
        "-Shared=1",
        "-AlertOnFatalError=0",
        "-ReconnectOnError=0",
    ]


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
    """Start the client, giving it the secret by a channel ``ps`` cannot read."""
    conn = target.connection
    proto = protocol or (conn.protocol if conn else PROTOCOL_RDP)
    argv = connection_argv(target, protocol=proto, binary=binary)
    exe = binary or ADAPTERS[proto].binary

    if proto == PROTOCOL_RDP:
        if password is None:
            raise ValueError("RDP needs a credential; none was resolved")
        # The whole invocation crosses one anonymous pipe as an inherited fd, so
        # the secret exists nowhere else: not argv, not the environment, not disk.
        read_fd, write_fd = os.pipe()
        os.write(write_fd, arg_stream(argv[1:], password))
        os.close(write_fd)
        proc = subprocess.Popen(
            [exe, f"/args-from:fd:{read_fd}"],
            env=env,
            pass_fds=(read_fd,),
            # No stdin at all: the client must never sit at a prompt waiting for
            # a credential it has already been given.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return Spawned(proc, (read_fd,))

    # TigerVNC's -autopass reads one password line from stdin, which is the same
    # guarantee by a different door. Only ask for it when we actually have one:
    # a server offering no authentication would be handed a password it never
    # requested, and the handshake fails in a way that reads like a bad secret.
    if password is not None:
        argv.append("-autopass")
    proc = subprocess.Popen(
        argv,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        if password is not None:
            proc.stdin.write((password + "\n").encode())
            proc.stdin.flush()
    except (BrokenPipeError, OSError):  # pragma: no cover
        pass
    finally:
        try:
            proc.stdin.close()
        except OSError:  # pragma: no cover
            pass
    return Spawned(proc)
