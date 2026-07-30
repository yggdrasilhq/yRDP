"""Lore recall — the part that must not be optional.

A skill an agent must remember to load is a skill an agent forgets.  So recall
does not live only in a skill file: opening a session **prints that target's
lore to stderr**, whether or not the agent thought to look.  When there is no
lore yet, the store prints the exact command to record the first entry.

The store itself lives in the operator's PRIVATE lore repo and is the single
source of truth; this module only locates it and passes its output through.  It
deliberately does not parse or re-render entries — a second renderer here would
be a second encoding of a format that already has an owner.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

LORE_DIR_ENV = "YRDP_LORE_DIR"
LORE_PY_ENV = "YRDP_LORE_PY"

#: Where a lore store keeps its CLI, relative to the store root.  yRDP ships no
#: store and hardcodes nobody's path: a store is private to whoever wrote it.
_SKILL_REL = Path(".claude/skills/yrdp-app-lore/lore.py")


def find_lore_py() -> Path | None:
    """Locate the store's CLI, honouring the same env vars the skill documents."""
    if explicit := os.environ.get(LORE_PY_ENV):
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    d = os.environ.get(LORE_DIR_ENV)
    if not d:
        return None
    root = Path(d).expanduser()
    # The variable may point at the store root or at the skill directory itself.
    for root in (root, root.parent.parent.parent.parent):
        candidate = root / _SKILL_REL
        if candidate.is_file():
            return candidate
        if (root / "lore.py").is_file():
            return root / "lore.py"
    return None


def recall(target: str, *, stream=sys.stderr) -> bool:
    """Print everything the fleet knows about ``target``.  True if any exists."""
    lore_py = find_lore_py()
    if lore_py is None:
        print(
            f"[yrdp] no lore store found (set {LORE_DIR_ENV} or {LORE_PY_ENV}); "
            f"driving this target blind",
            file=stream,
        )
        return False
    python = shutil.which("python3") or sys.executable
    proc = subprocess.run(
        [python, str(lore_py), "get", target], capture_output=True, text=True
    )
    body = (proc.stdout or "") + (proc.stderr or "")
    print(f"[yrdp] app-lore for {target} ({lore_py}):", file=stream)
    print(body.rstrip() or "(the store said nothing)", file=stream)
    return proc.returncode == 0
