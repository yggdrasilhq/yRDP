"""The geometry contract — the load-bearing rule of coordinate automation.

Coordinate-based automation rots *silently*: when the display size changes, the
same lore that worked yesterday clicks empty canvas today and nothing errors.
So geometry here is a contract with a **refusal**, not a convention:

  * a target declares exactly ONE canonical geometry for the agent lane;
  * a session pins that geometry at open time and never renegotiates it;
  * every piece of coordinate lore stamps the geometry it was proven at;
  * replaying coordinate lore at a different geometry **refuses** rather than
    clicking approximately.

A human viewer attaching to watch may not renegotiate the agent lane's geometry
as a side effect of resizing a window.  The viewer gets a scaled view of the
canonical surface, never a resize of it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: The stamp a lore entry carries when its method genuinely uses no coordinates
#: (an API call, a UIA element name, a config-file edit).  Such lore is valid at
#: every geometry, which is exactly why the cheaper rungs are preferred.
GEOMETRY_FREE = "n/a"

_STAMP = re.compile(r"^\s*(\d+)\s*x\s*(\d+)\s*(?:@\s*([0-9]*\.?[0-9]+))?\s*$")


class GeometryError(ValueError):
    """A geometry stamp could not be understood."""


class GeometryMismatch(Exception):
    """Refusal: coordinate work was requested at a geometry it was not proven at.

    This is not a warning and must never be downgraded to one.  Approximating
    the click is the failure mode the whole contract exists to prevent.
    """

    def __init__(self, *, proven: str, live: str, what: str) -> None:
        super().__init__(
            f"refusing {what}: proven at {proven}, session is pinned at {live}. "
            f"Re-prove the method at {live} (or use a geometry-free rung: api/script/uia)."
        )
        self.proven = proven
        self.live = live
        self.what = what


@dataclass(frozen=True, slots=True)
class Geometry:
    """A canonical surface size.  Immutable: a session pins one and keeps it."""

    width: int
    height: int
    scale: float = 1.0

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise GeometryError(f"geometry must be positive, got {self.width}x{self.height}")
        if self.scale <= 0:
            raise GeometryError(f"scale must be positive, got {self.scale}")

    @classmethod
    def parse(cls, stamp: str) -> "Geometry":
        """Parse ``1920x1080@1.0`` (the ``@scale`` half defaults to 1.0)."""
        m = _STAMP.match(stamp or "")
        if not m:
            raise GeometryError(f"not a geometry stamp: {stamp!r} (want WIDTHxHEIGHT[@SCALE])")
        return cls(int(m.group(1)), int(m.group(2)), float(m.group(3) or 1.0))

    @property
    def stamp(self) -> str:
        """The canonical string form, as written into lore entries.

        Always at least one decimal on the scale (``@1.0``, never ``@1``) so
        that a stamp a human reads in a lore file and a stamp this tool prints
        are the same characters.  Comparison is numeric either way, but a
        wobbling spelling makes a reviewer doubt a match that is really there.
        """
        scale = f"{self.scale:.10g}"
        if "." not in scale and "e" not in scale:
            scale += ".0"
        return f"{self.width}x{self.height}@{scale}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.stamp


def is_geometry_free(stamp: str | None) -> bool:
    """True when a lore stamp declares the method uses no coordinates at all."""
    return stamp is None or stamp.strip().lower() in {GEOMETRY_FREE, "", "none"}


def require_match(live: Geometry, proven: str | None, *, what: str) -> None:
    """Enforce the contract, or refuse.

    ``proven`` is the stamp carried by the lore entry (or ``n/a``).  Geometry-free
    lore passes at any geometry — that is the reward for using a cheaper rung.
    Anything else must match the session's pinned geometry exactly.
    """
    if is_geometry_free(proven):
        return
    if Geometry.parse(proven) != live:
        raise GeometryMismatch(proven=proven.strip(), live=live.stamp, what=what)
