"""The substrate seam — everything yRDP deliberately refuses to know.

An RDP endpoint sits on *something*: a virtual machine, a container, a physical
desk, a cloud instance.  Bringing that something up, taking it down, or looking
at it when it will not talk are all real operations an agent needs — and every
one of them is site-specific.  Encoding any of them in a client would make the
client useless to the next site and would leak the current one's shape.

So the seam is data.  A target declares named ``[hooks]`` (argv lists), and yRDP
runs them without understanding them.  ``up`` and ``down`` are conventions, not
requirements; a site is free to declare ``snapshot``, ``console``, ``uia``, or
anything else, and reach it with ``yrdp hook``.

The two things yRDP *does* know are genuinely generic: whether a TCP endpoint
answers, and how to run a command on the machine hosting the target.
"""

from __future__ import annotations

import shlex
import socket
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from .config import ConfigError, Target

#: The endpoint answers a TCP connect.
REACHABLE = "reachable"
#: It does not.  yRDP does not guess why: on one substrate that means the
#: machine is off, on another it means a firewall, on a third it means a tunnel
#: is down.  Naming a cause we cannot see would be inventing a diagnosis.
UNREACHABLE = "unreachable"


class HookError(Exception):
    """A configured hook failed."""


def port_open(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def reachable(target: Target, timeout: float = 3.0) -> bool:
    if target.connection is None:
        raise ConfigError(f"target {target.name!r} declares no [connection] endpoint")
    return port_open(target.connection.host, target.connection.port, timeout)


@dataclass(slots=True)
class State:
    """A reading, with every field saying how it was measured."""

    target: str
    endpoint: str
    state: str
    protocol: str
    geometry: str
    session: dict[str, Any] | None = None
    hook: dict[str, Any] | None = None
    checked_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "endpoint": self.endpoint,
            "state": self.state,
            "protocol": self.protocol,
            "geometry": self.geometry,
            "session": self.session,
            "hook": self.hook,
            "checked_at": self.checked_at,
        }


def run_hook(target: Target, name: str, *extra: str, timeout: float = 600.0) -> dict[str, Any]:
    """Run a named hook.  Its meaning is the site's business, not ours."""
    argv = [*target.hook(name), *extra]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise HookError(f"hook {name!r} names a command that does not exist: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise HookError(f"hook {name!r} timed out after {timeout}s") from exc
    result = {
        "hook": name,
        "argv": shlex.join(argv),
        "returncode": p.returncode,
        "stdout": p.stdout,
        "stderr": p.stderr,
    }
    if p.returncode != 0:
        raise HookError(f"hook {name!r} failed ({p.returncode}): {(p.stderr or p.stdout)[:400]}")
    return result


def host_exec(target: Target, command: str, *, timeout: float = 300.0) -> dict[str, Any]:
    """Run a command on the machine hosting the target.

    ``[host] shell`` is an argv prefix that gets a shell there — ssh for one
    site, a container attach for another.  yRDP appends the command and reads
    the result; what the command means is lore's business.
    """
    if not target.host_shell:
        raise ConfigError(
            f"target {target.name!r} declares no [host] shell prefix, so there is no "
            f"way to run a command on the machine hosting it"
        )
    argv = [*target.host_shell, command]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise HookError(f"host command timed out after {timeout}s") from exc
    return {
        "argv": shlex.join(argv),
        "returncode": p.returncode,
        "stdout": p.stdout,
        "stderr": p.stderr,
    }
