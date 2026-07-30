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
    [connection]  host, port, user, domain, security, password_vault_entry
    [geometry]    width, height, scale        -- THE CONTRACT (see geometry.py)
    [surface]     mode = "shadow" | "viewport"
    [host]        ssh = [...]                 -- a shell on the hosting machine
    [hooks]       <name> = [...]              -- site-specific commands, as data
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

#: A fixed-dimension surface the agent drives, with no window on any screen.
#: The dimensions are the contract, which is what makes lore replayable.
SURFACE_SHADOW = "shadow"
#: A libyggterm surface in the yggterm viewport — the human lane.
SURFACE_VIEWPORT = "viewport"
SURFACE_MODES = (SURFACE_SHADOW, SURFACE_VIEWPORT)


class ConfigError(Exception):
    """A target file is missing, unreadable, or does not describe a target."""


@dataclass(frozen=True, slots=True)
class Connection:
    host: str
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
    connection: Connection | None = None
    surface_mode: str = SURFACE_SHADOW
    #: argv prefix that runs a command on the machine hosting the target.
    #: Whatever gets a shell there — ssh, a container attach, anything.
    host_shell: tuple[str, ...] = ()
    hooks: dict[str, tuple[str, ...]] = field(default_factory=dict)
    source: Path | None = None

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
        try:
            connection = Connection(
                host=str(c["host"]),
                port=int(c.get("port", 3389)),
                user=c.get("user"),
                domain=c.get("domain"),
                security=c.get("security"),
                password_vault_entry=c.get("password_vault_entry"),
            )
        except KeyError as exc:
            raise ConfigError(f"{path or name}: [connection] needs a host") from exc

    surface = str((raw.get("surface") or {}).get("mode", SURFACE_SHADOW))
    if surface not in SURFACE_MODES:
        raise ConfigError(
            f"{path or name}: surface mode {surface!r} is not one of {SURFACE_MODES}"
        )

    hooks = {
        str(k): tuple(str(x) for x in v)
        for k, v in (raw.get("hooks") or {}).items()
        if isinstance(v, list)
    }

    return Target(
        name=str(t.get("name", name)),
        kind=str(t.get("kind", "rdp")),
        description=str(t.get("description", "")),
        geometry=geometry,
        connection=connection,
        surface_mode=surface,
        host_shell=tuple(str(x) for x in (raw.get("host") or {}).get("shell", ())),
        hooks=hooks,
        source=path,
    )
