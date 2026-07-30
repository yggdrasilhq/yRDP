"""Locks on the rules that are load-bearing rather than merely convenient.

Each test names the failure it prevents.  A lock that can only pass is worth
nothing, so every one of these was checked by breaking the rule it guards and
watching it go red.
"""

from __future__ import annotations

import base64
import shlex
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from yrdp import config, session, substrate  # noqa: E402
from yrdp.geometry import (  # noqa: E402
    Geometry,
    GeometryError,
    GeometryMismatch,
    require_match,
)

# -- the geometry contract --------------------------------------------------


def test_coordinate_lore_at_another_geometry_is_refused_not_approximated():
    """The whole point: a stale coordinate must raise, never click nearby."""
    with pytest.raises(GeometryMismatch):
        require_match(Geometry(1920, 1080), "1280x800@1.0", what="click at 840,412")


def test_scale_change_alone_is_a_mismatch():
    """Same pixels, different DPI, different hit targets. Must still refuse."""
    with pytest.raises(GeometryMismatch):
        require_match(Geometry(1920, 1080, 1.0), "1920x1080@1.25", what="click")


def test_geometry_free_lore_is_valid_everywhere():
    """The reward for using a cheaper rung: api/uia lore never rots on resize."""
    require_match(Geometry(1920, 1080), "n/a", what="read positions")
    require_match(Geometry(800, 600), None, what="read positions")


def test_matching_geometry_passes():
    require_match(Geometry(1920, 1080), "1920x1080@1.0", what="click")


def test_stamp_is_written_the_way_lore_spells_it():
    """`@1` and `@1.0` compare equal, but only one spelling goes into a file."""
    assert Geometry(1920, 1080).stamp == "1920x1080@1.0"
    assert Geometry.parse("1920x1080").stamp == "1920x1080@1.0"
    assert Geometry.parse("1920x1080@1.25").stamp == "1920x1080@1.25"


def test_a_geometry_cannot_be_degenerate():
    for bad in ("0x1080@1.0", "1920x0@1.0", "1920x1080@0"):
        with pytest.raises(GeometryError):
            Geometry.parse(bad) if "@0" not in bad else Geometry(1920, 1080, 0.0)


# -- target configuration ---------------------------------------------------


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


@pytest.fixture()
def targets(tmp_path, monkeypatch):
    monkeypatch.setenv(config.TARGETS_DIR_ENV, str(tmp_path))
    return tmp_path


def test_geometry_has_no_default_because_a_defaulted_one_rots_silently(targets):
    _write(targets, "t.toml", """
        [target]
        name = "t"
    """)
    with pytest.raises(config.ConfigError, match="geometry"):
        config.load_target("t")









# -- the session's half of the geometry contract ----------------------------


def _session(width=1920, height=1080):
    from yrdp import session

    return session.Session(
        target="t",
        geometry=f"{width}x{height}@1.0",
        display=":99",
        xvfb_pid=1,
        rdp_pid=1,
        host="h:3389",
        user=None,
        opened_at=0.0,
    )


def test_a_coordinate_off_the_pinned_surface_is_refused():
    """Off-surface is a rotted coordinate, not a near miss worth clamping."""
    from yrdp import session

    s = _session()
    session.check_point(s, 1919, 1079, None)
    for bad in ((1920, 500), (500, 1080), (-1, 5)):
        with pytest.raises(session.SessionError):
            session.check_point(s, bad[0], bad[1], None)


def test_lore_from_another_geometry_is_refused_at_the_click(targets):
    from yrdp import session

    with pytest.raises(GeometryMismatch):
        session.check_point(_session(), 840, 412, "1280x800@1.0")


def test_the_client_is_never_given_a_flag_that_lets_the_far_end_resize_us(targets):
    """A resize mid-session invalidates every coordinate in the lore silently."""
    from yrdp import session

    _write(targets, "r.toml", """
        [target]
        name = "r"
        [geometry]
        width = 1920
        height = 1080
        [connection]
        host = "host.example"
        user = "someone"
    """)
    argv = session.client_argv(config.load_target("r"))
    joined = " ".join(argv)
    assert "/size:1920x1080" in joined, "the surface must be pinned to the contract"
    for flag in session.FORBIDDEN_CLIENT_FLAGS:
        assert flag not in joined, f"{flag} hands the far end control of our geometry"


def test_a_secret_is_never_placed_on_the_command_line(targets):
    """ps is world-readable; the password goes down stdin, never in argv."""
    from yrdp import session

    _write(targets, "s.toml", """
        [target]
        name = "s"
        [geometry]
        width = 800
        height = 600
        [connection]
        host = "host.example"
        user = "someone"
    """)
    argv = session.client_argv(config.load_target("s"))
    assert not any(a.startswith("/p:") for a in argv)
    assert not any("hunter2" in a for a in argv)
    # The secret exists only inside the fd stream, which no other process can
    # read: not argv, not the environment, not a file on disk.
    stream = session.arg_stream(argv[1:], "hunter2").decode()
    assert "/p:hunter2" in stream.splitlines()
    assert stream.endswith("\n"), "FreeRDP wants one argument per line"


def test_the_credential_is_named_not_carried(targets):
    """A target may name a vault entry; it may never hold the secret itself."""
    from yrdp import session

    _write(targets, "v.toml", """
        [target]
        name = "v"
        [geometry]
        width = 800
        height = 600
        [connection]
        host = "host.example"
    """)
    t = config.load_target("v")
    assert t.connection.password_vault_entry is None
    with pytest.raises(session.CredentialUnavailable, match="names no RDP credential"):
        session.resolve_password(t)


# -- the tool stays a TOOL --------------------------------------------------


def test_no_targets_directory_is_guessed(monkeypatch):
    """yRDP ships no targets and hardcodes nobody's paths."""
    monkeypatch.delenv(config.TARGETS_DIR_ENV, raising=False)
    with pytest.raises(config.ConfigError, match=config.TARGETS_DIR_ENV):
        config.targets_dir()


def test_a_viewport_target_refuses_rather_than_quietly_going_headless(targets):
    """Silently substituting a different surface would be a lie about the product."""
    _write(targets, "vp.toml", """
        [target]
        name = "vp"
        [geometry]
        width = 1920
        height = 1080
        [surface]
        mode = "viewport"
        [connection]
        host = "host.example"
    """)
    t = config.load_target("vp")
    assert t.surface_mode == config.SURFACE_VIEWPORT
    with pytest.raises(session.SessionError, match="NOT BUILT"):
        session.open_session(t)


def test_an_unknown_hook_says_what_the_target_does_declare(targets):
    """Site mechanisms are data; a missing one is a config answer, not a crash."""
    _write(targets, "h.toml", """
        [target]
        name = "h"
        [geometry]
        width = 800
        height = 600
        [hooks]
        up = ["true"]
    """)
    t = config.load_target("h")
    assert t.hook("up") == ("true",)
    with pytest.raises(config.ConfigError, match="up"):
        t.hook("console")


#: Shapes — not names — that betray a particular deployment.  Names would leak
#: in the very act of listing them, so the lock matches structure instead: an
#: address literal, somebody's home directory, a hypervisor image, a container
#: command. A general-purpose client has no business containing any of them.
SITE_SPECIFIC_SHAPES = (
    r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
    r"/home/[a-z]",
    r"~/(git|gh)/",
    r"\.qcow2",
    r"lxc-attach|lxc-start|qemu-system|virsh",
    r"\bhostfwd\b",
)


def test_the_repo_carries_no_site_specific_shapes():
    """The leak guard, permanent. This repo is meant to be publishable.

    It scans itself, so it skips its own source: the patterns above ARE the
    shapes, and a scanner that flagged its own pattern list would be useless.
    """
    import re

    root = Path(__file__).resolve().parents[1]
    me = Path(__file__).resolve()
    offenders = []
    for path in root.rglob("*"):
        if not path.is_file() or path == me:
            continue
        if any(part in (".git", "__pycache__") for part in path.parts):
            continue
        if path.suffix not in (".py", ".md", ".toml", ".ps1", "") :
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in SITE_SPECIFIC_SHAPES:
            for m in re.finditer(pattern, text):
                line = text[: m.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(root)}:{line}: {m.group(0)!r}")
    assert not offenders, "site-specific detail in a publishable repo:\n" + "\n".join(offenders)
