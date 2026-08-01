"""Target configuration — data only, and deliberately ignorant.

yRDP is a general-purpose RDP client.  It knows how to reach an RDP endpoint,
how to pin a surface to a declared geometry, how to drive that surface, and how
to recall lore.  It knows **nothing** about what is on the far end: not the
application, not the operating system, not the hypervisor underneath it, not the
site it belongs to.

That ignorance is the design, not an omission.  A tool that has learned one
deployment's shape stops being a tool for anyone else's, and every such fact
baked into this repo is also a fact leaking out of a private one.  So:

* **what to connect to** is a target file (private to whoever owns the machine);
* **what to do once connected** is lore (private in the same way);
* **how to bring a machine up, take it down, or look at it out of band** is a
  named ``[hooks]`` command, because those mechanisms differ per site — a
  hypervisor socket here, a container command there, a cloud API elsewhere —
  and none of them belongs in a client.

Shape of a target file:

    [target]      name, kind, description
    [connection]  protocol, host, port, user, domain, security, password_vault_entry
    [geometry]    width, height, scale        -- THE CONTRACT (see geometry.py)
    [host]        shell = [...]               -- a shell on the hosting machine
    [hooks]       <name> = [...]              -- site-specific commands, as data

There is deliberately no "surface mode".  A session has ONE canonical surface,
pinned at the contract geometry; a viewport is a REVEAL of that same session that
any number of viewers may attach to and detach from without disturbing it.  They
were modelled as exclusive modes once, which was wrong: it left the agent's
surface unwatchable, and a surface nobody can look at is a surface nobody can
trust.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .geometry import Geometry

TARGETS_DIR_ENV = "YRDP_TARGETS_DIR"
STATE_DIR_ENV = "YRDP_STATE_DIR"

#: State (session records, journal, derived caches).  Shares the yggterm
#: namespace because yRDP is a surface of that system, not a separate install.
DEFAULT_STATE_DIR = Path.home() / ".yggterm" / "yrdp"

#: Protocols this client speaks.  One tool, not two: everything above the client
#: adapter — the geometry contract, sessions, viewers, lore, hooks, credentials —
#: is protocol-independent, so a second codebase would only be a second copy that
#: drifts.  ``--vnc`` on the command line overrides the target's declaration.
PROTOCOL_RDP = "rdp"
PROTOCOL_VNC = "vnc"
PROTOCOLS = (PROTOCOL_RDP, PROTOCOL_VNC)

DEFAULT_PORT = {PROTOCOL_RDP: 3389, PROTOCOL_VNC: 5900}


class ConfigError(Exception):
    """A target file is missing, unreadable, or does not describe a target."""


@dataclass(frozen=True, slots=True)
class Connection:
    host: str
    protocol: str = PROTOCOL_RDP
    port: int = 3389
    user: str | None = None
    domain: str | None = None
    security: str | None = None
    #: The NAME of a vault entry, never a secret.
    password_vault_entry: str | None = None


@dataclass(frozen=True, slots=True)
class Target:
    name: str
    geometry: Geometry
    kind: str = "rdp"
    description: str = ""
    #: What MACHINE this target lives on, for humans.  Several targets routinely
    #: share one box — an operator's Windows guest can carry a trading client and
    #: an astrology suite, and each gets its own target because each has its own
    #: hooks and its own lore.  That is right for the agent lane and WRONG for a
    #: chooser: a human picking "which desktop do I want" is picking a MACHINE,
    #: and offering them two rows that open the same guest is a category error.
    #: Empty ⇒ the endpoint is the identity (see `machine_key`).
    machine: str = ""
    connection: Connection | None = None
    #: argv prefix that runs a command on the machine hosting the target.
    #: Whatever gets a shell there — ssh, a container attach, anything.
    host_shell: tuple[str, ...] = ()
    hooks: dict[str, tuple[str, ...]] = field(default_factory=dict)
    source: Path | None = None

    @property
    def machine_key(self) -> tuple[str, str, int]:
        """What makes two targets THE SAME BOX: the endpoint they dial.

        Not the declared name, which is per-application, and not the geometry,
        which two targets on one machine can (wrongly) disagree about.
        """
        c = self.connection
        return (c.protocol, c.host, c.port) if c else ("", self.name, 0)

    @property
    def machine_label(self) -> str:
        """What to call the machine in a chooser."""
        if self.machine.strip():
            return self.machine.strip()
        c = self.connection
        return f"{c.host}:{c.port}" if c else self.name

    def hook(self, name: str) -> tuple[str, ...]:
        if name not in self.hooks:
            known = ", ".join(sorted(self.hooks)) or "none"
            raise ConfigError(
                f"target {self.name!r} declares no {name!r} hook (has: {known}). "
                f"Site-specific mechanisms live in the target file, not in yRDP."
            )
        return self.hooks[name]


def targets_dir() -> Path:
    """Where target files live.  No default that names anybody's directory."""
    raw = os.environ.get(TARGETS_DIR_ENV)
    if not raw:
        raise ConfigError(
            f"{TARGETS_DIR_ENV} is not set. yRDP ships no targets and guesses no "
            f"paths: point it at the directory holding your target files."
        )
    return Path(raw).expanduser()


def state_dir() -> Path:
    d = Path(os.environ.get(STATE_DIR_ENV) or DEFAULT_STATE_DIR).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_targets() -> list[str]:
    d = targets_dir()
    if not d.is_dir():
        raise ConfigError(f"{TARGETS_DIR_ENV} points at {d}, which is not a directory")
    return sorted(p.stem for p in d.glob("*.toml") if not p.name.startswith("_"))


def load_target(name: str) -> Target:
    path = targets_dir() / f"{name}.toml"
    if not path.is_file():
        known = ", ".join(list_targets()) or "none"
        raise ConfigError(f"no target {name!r} in {targets_dir()} (known: {known})")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read target {name!r}: {exc}") from exc
    return from_toml(name, raw, path)


def from_toml(name: str, raw: dict, path: Path | None = None) -> Target:
    t = raw.get("target") or {}
    if not t:
        raise ConfigError(f"{path or name} has no [target] table")

    geom_raw = raw.get("geometry") or {}
    try:
        geometry = Geometry(
            width=int(geom_raw["width"]),
            height=int(geom_raw["height"]),
            scale=float(geom_raw.get("scale", 1.0)),
        )
    except KeyError as exc:
        raise ConfigError(
            f"{path or name} must declare [geometry] width/height. There is no default, "
            f"because a defaulted geometry is exactly the silent rot the contract exists "
            f"to stop."
        ) from exc

    connection = None
    if c := (raw.get("connection") or {}):
        protocol = str(c.get("protocol", PROTOCOL_RDP)).lower()
        if protocol not in PROTOCOLS:
            raise ConfigError(
                f"{path or name}: protocol {protocol!r} is not one of {PROTOCOLS}"
            )
        try:
            connection = Connection(
                host=str(c["host"]),
                protocol=protocol,
                port=int(c.get("port", DEFAULT_PORT[protocol])),
                user=c.get("user"),
                domain=c.get("domain"),
                security=c.get("security"),
                password_vault_entry=c.get("password_vault_entry"),
            )
        except KeyError as exc:
            raise ConfigError(f"{path or name}: [connection] needs a host") from exc

    hooks = {
        str(k): tuple(str(x) for x in v)
        for k, v in (raw.get("hooks") or {}).items()
        if isinstance(v, list)
    }

    return Target(
        name=str(t.get("name", name)),
        kind=str(t.get("kind", "rdp")),
        description=str(t.get("description", "")),
        machine=str(t.get("machine", "")),
        geometry=geometry,
        connection=connection,
        host_shell=tuple(str(x) for x in (raw.get("host") or {}).get("shell", ())),
        hooks=hooks,
        source=path,
    )
