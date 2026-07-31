"""Locks on the rules that are load-bearing rather than merely convenient.

Each test names the failure it prevents.  A lock that can only pass is worth
nothing, so every one of these was checked by breaking the rule it guards and
watching it go red.
"""

from __future__ import annotations

import base64
import shlex
import struct
import sys
import textwrap
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from yrdp import clients, config, rfb, session, view  # noqa: E402
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


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """No test may touch the real state directory.

    Learned by doing it: a lock that exercised detach() wrote a session record
    called "t" into the live store, where it then showed up in `yrdp list` as a
    dead session nobody could explain. A test that leaves droppings in the
    thing it tests is worse than no test.
    """
    monkeypatch.setenv(config.STATE_DIR_ENV, str(tmp_path / "state"))
    return tmp_path


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
        client_pid=1,
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
    argv = clients.connection_argv(config.load_target("r"))
    joined = " ".join(argv)
    assert "/size:1920x1080" in joined, "the surface must be pinned to the contract"
    for flag in clients.RDP.forbidden:
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
    argv = clients.connection_argv(config.load_target("s"))
    assert not any(a.startswith("/p:") for a in argv)
    assert not any("hunter2" in a for a in argv)
    # The secret exists only inside the fd stream, which no other process can
    # read: not argv, not the environment, not a file on disk.
    stream = clients.arg_stream(argv[1:], "hunter2").decode()
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
    with pytest.raises(session.CredentialUnavailable, match="names no credential"):
        session.resolve_password(t)


# -- the tool stays a TOOL --------------------------------------------------


def test_no_targets_directory_is_guessed(monkeypatch):
    """yRDP ships no targets and hardcodes nobody's paths."""
    monkeypatch.delenv(config.TARGETS_DIR_ENV, raising=False)
    with pytest.raises(config.ConfigError, match=config.TARGETS_DIR_ENV):
        config.targets_dir()



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
#: Addresses that belong to no site: loopback and the any-address. They are
#: universal facts, not somebody's deployment, and the reveal genuinely needs to
#: name loopback — the surface must never bind off-box. Exempted by NAME rather
#: than by weakening the pattern, so a real address still trips the guard.
UNIVERSAL_ADDRESSES = ("127.0.0.1", "0.0.0.0", "255.255.255.255")

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
                if m.group(0) in UNIVERSAL_ADDRESSES:
                    continue
                line = text[: m.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(root)}:{line}: {m.group(0)!r}")
    assert not offenders, "site-specific detail in a publishable repo:\n" + "\n".join(offenders)


# -- one session, N viewers -------------------------------------------------


def test_a_viewer_scales_and_never_resizes_the_surface():
    """The rule that makes co-browse fall out for free.

    A terminal's grid IS its geometry, so two viewers at different sizes fight
    over one authoritative number. Here the framebuffer is fixed by contract, so
    viewers may differ — as long as every one of them scales rather than asking
    the far end to change size.
    """
    url = "http://127.0.0.1:6100/vnc.html?autoconnect=1&reconnect=1&resize=scale&show_dot=1"
    assert "resize=scale" in url
    assert "resize=remote" not in url


def test_viewers_share_rather_than_evict_each_other():
    """Without -shared, a second viewer kicks the first off. That is not co-browse."""
    import inspect

    src = inspect.getsource(view.attach)
    assert '"-shared"' in src, "viewers must not evict each other"
    assert '"-forever"' in src, "the session must outlive any one viewer"
    assert '"-localhost"' in src, "the surface is loopback-only; tunnelling is yggterm's job"


def test_detaching_a_viewer_leaves_the_session_running():
    """The whole correction: a session must not exist only while someone watches.

    Uses real processes, because the bug this guards against is 'detach reaped
    the wrong pids', which only a real signal can prove.
    """
    import subprocess as sp
    import time

    surface = [sp.Popen(["sleep", "30"]), sp.Popen(["sleep", "30"])]
    viewer_procs = [sp.Popen(["sleep", "30"]), sp.Popen(["sleep", "30"])]
    try:
        s = session.Session(
            target="t", geometry="800x600@1.0", display=":99",
            xvfb_pid=surface[0].pid, client_pid=surface[1].pid,
            host="h:5900", user=None, opened_at=0.0,
        )
        v = view.Viewer(
            target="t", vnc_port=1, web_port=2, url="u",
            pids=[p.pid for p in viewer_procs], read_only=False, started_at=0.0,
        )
        s.viewers.append(v.as_dict())
        view.detach(s, v)
        time.sleep(0.5)
        assert all(p.poll() is not None for p in viewer_procs), "viewers must be gone"
        assert all(p.poll() is None for p in surface), "the SESSION must survive its viewers"
    finally:
        for p in surface + viewer_procs:
            p.kill()


# -- one tool, two protocols ------------------------------------------------


def test_vnc_is_spoken_directly_and_has_no_client_argv(targets):
    """The VNC lane must never grow a spawned-viewer path again.

    It had one: a headless X display with a real viewer inside it.  The viewer
    authenticated and then painted nothing, while wedging other X clients on the
    display.  If someone re-adds an argv here, this goes red — the point is not
    that argv is bad, it is that two ways to hold one surface will diverge.
    """
    _write(targets, "v9.toml", """
        [target]
        name = "v9"
        [geometry]
        width = 1440
        height = 900
        [connection]
        protocol = "vnc"
        host = "host.example"
    """)
    t = config.load_target("v9")
    assert t.connection.port == 5900, "the default port must follow the protocol"
    assert clients.VNC.backend == clients.BACKEND_RFB
    assert clients.VNC.binary == "", "a directly-spoken protocol spawns nothing"
    with pytest.raises(ValueError, match="spoken directly"):
        clients.connection_argv(t)


def test_the_direct_client_never_advertises_a_resize_pseudo_encoding():
    """The geometry contract, in protocol form.

    A server can only resize a client that said it would listen.  Advertising
    DesktopSize is the exact analogue of passing RDP's ``dynamic-resolution``,
    and it would rot every coordinate in the lore without raising anything.
    """
    assert rfb.FORBIDDEN_ENCODINGS, "the forbidden set must name what it forbids"
    for code in rfb.ADVERTISED_ENCODINGS:
        assert code not in rfb.FORBIDDEN_ENCODINGS, (
            f"encoding {code} ({rfb.FORBIDDEN_ENCODINGS.get(code)}) hands the far end "
            f"control of our surface size"
        )
    assert rfb.ENCODING_RAW in rfb.ADVERTISED_ENCODINGS


def test_both_protocols_classify_into_the_same_named_outcomes():
    """The caller's recovery depends on the outcome, never on the protocol.

    The x11 backend earns the two outcomes by matching a client's stderr; the
    direct backend raises them as types.  Both must arrive at the SAME pair, or
    a caller has to ask which protocol it is before it can recover.
    """
    assert clients.RDP.auth_markers, "a spawned client must be able to say 'wrong password'"
    assert clients.RDP.unreachable_markers, "a spawned client must be able to say 'no answer'"
    assert clients.RDP.forbidden, "a spawned client must name its resize-granting flags"

    with pytest.raises(session.AuthRefused):
        with session._rfb_errors("open"):
            raise rfb.RfbAuthError("refused")
    with pytest.raises(session.SessionError):
        with session._rfb_errors("open"):
            raise rfb.RfbError("nobody home")


def test_a_session_with_no_processes_never_signals_the_process_group():
    """``kill(0, sig)`` means 'my whole process group' — including the tool.

    Every rfb session records pid 0 twice, so a single unguarded ``os.kill``
    here would have taken down the caller's shell.
    """
    assert session._alive(0) is False
    killed = []
    original = session.os.kill
    session.os.kill = lambda pid, sig: killed.append(pid)
    try:
        session._kill(0)
        session._kill(-1)
    finally:
        session.os.kill = original
    assert killed == [], f"refused pids leaked to kill(): {killed}"


def test_a_chord_keeps_one_vocabulary_across_both_backends():
    """``yrdp do key ctrl+alt+Delete`` must mean one thing, not two."""
    mods, key = rfb.parse_chord("ctrl+alt+Delete")
    assert [hex(m) for m in mods] == ["0xffe3", "0xffe9"]
    assert key == 0xFFFF
    assert rfb.parse_chord("Return") == ([], 0xFF0D)
    assert rfb.parse_chord("a") == ([], ord("a"))
    assert rfb.parse_chord("ctrl++") == ([0xFFE3], ord("+"))
    with pytest.raises(rfb.RfbError, match="unknown key"):
        rfb.parse_chord("Warp")


def test_a_frame_refuses_a_crop_that_is_not_inside_it():
    """A crop off the surface is a rotted coordinate, not a near miss."""
    frame = rfb.Frame(4, 2, bytearray(4 * 2 * 4))
    assert frame.crop(1, 0, 2, 2).width == 2
    with pytest.raises(rfb.RfbError, match="refusing crop"):
        frame.crop(3, 0, 2, 2)


def test_a_frame_writes_the_pixels_it_was_given():
    """BGRX in, RGB out — a channel swap here would mis-report every colour."""
    pixels = bytearray([0, 0, 255, 0, 255, 0, 0, 0])  # red, then blue
    png = rfb.Frame(2, 1, pixels).png()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    body = zlib.decompress(png[png.index(b"IDAT") + 4:][: -12])
    assert bytes(body) == bytes([0, 255, 0, 0, 0, 0, 255])  # filter, RGB, RGB


def test_an_unknown_protocol_is_refused_not_defaulted(targets):
    _write(targets, "p.toml", """
        [target]
        name = "p"
        [geometry]
        width = 800
        height = 600
        [connection]
        protocol = "telepathy"
        host = "host.example"
    """)
    with pytest.raises(config.ConfigError, match="telepathy"):
        config.load_target("p")


class _ScriptedSocket:
    """A socket that replays a server's bytes and records what we sent."""

    def __init__(self, script: bytes):
        self._script = script
        self._at = 0
        self.sent = bytearray()

    def recv(self, count: int) -> bytes:
        chunk = self._script[self._at:self._at + count]
        self._at += len(chunk)
        return chunk

    def sendall(self, data: bytes) -> None:
        self.sent += data


def test_an_apple_endpoint_is_spoken_to_with_the_ordinary_password_type():
    """macOS Screen Sharing offers Apple's own types AND plain VNC auth.

    This client only ever needed the ordinary one.  The endpoint announces
    ``RFB 003.889`` — a minor version far past 8 — and security types
    ``30, 33, 35, 36, 2``.  Two things have to hold or the connection dies on a
    live macOS guest: the version has to be CLAMPED to 3.8 rather than echoed
    back, and type 2 has to be chosen out of a list whose first four entries are
    Apple's.  Both were proven against the real endpoint on 2026-07-31, which
    also falsified the old error text claiming an Apple adapter was required.
    """
    apple_offer = bytes([5, 30, 33, 36, 2, 35])
    sock = _ScriptedSocket(b"RFB 003.889\n" + apple_offer + struct.pack(">I", 0))

    version = rfb._handshake_version(sock)
    assert version == "3.8", "889 must clamp to the highest version we speak, not echo back"
    assert bytes(sock.sent) == b"RFB 003.008\n"

    sock.sent.clear()
    # The DES response follows the challenge, so give it one.
    sock._script = sock._script[:0] + apple_offer + b"\x00" * 16 + struct.pack(">I", 0)
    sock._at = 0
    rfb._handshake_security(sock, version, "hunter2")
    assert sock.sent[0] == rfb.SECURITY_VNC_AUTH, (
        f"chose security type {sock.sent[0]} out of {list(apple_offer[1:])}; "
        f"type {rfb.SECURITY_VNC_AUTH} is the one this client implements"
    )


def test_a_type_this_client_cannot_speak_says_so_without_inventing_a_reason():
    """The refusal names the offer and stops.

    It used to assert that a macOS Screen Sharing endpoint needs an Apple
    adapter.  That was wrong — macOS offers plain VNC auth as well — and a
    confidently wrong error sends the next reader to build something that was
    never needed.
    """
    only_apple = bytes([2, 30, 33])
    sock = _ScriptedSocket(only_apple)
    with pytest.raises(rfb.RfbAuthError) as caught:
        rfb._handshake_security(sock, "3.8", "hunter2")
    message = str(caught.value)
    assert "30" in message and "33" in message, "the refusal must name what was offered"
    assert "adapter" not in message.lower(), "do not prescribe a fix that was never needed"


# -- the geometry epoch: a resize must not be able to rot a coordinate quietly --


def test_a_fresh_session_is_not_stale_and_replays_lore_without_a_screenshot(monkeypatch):
    """Being born must not count as having been moved.

    The epoch guard has to bite on a RE-PIN and on nothing else.  If a session
    started out 'unobserved', every agent would be forced to take a screenshot
    before replaying a coordinate the lore already proved — which would make the
    pixel rung mandatory to use the cheap one, exactly backwards.
    """
    from yrdp import session

    s = _session()
    assert s.geometry_epoch == s.observed_at_epoch
    session.check_point(s, 100, 100, None)  # no screenshot taken, and that is fine
    session.check_point(s, 100, 100, "1920x1080@1.0")


def test_an_unstamped_coordinate_is_refused_after_someone_repins(monkeypatch, tmp_path):
    """THE case this machinery exists for, and the one nothing else catches.

    An agent screenshots at 1920x1080; a human attaches in adopt mode and the
    surface becomes 1600x900; the agent clicks a coordinate it read off its own
    screenshot.  ``--proven`` was omitted (correctly, by the old rule), the point
    is inside the new surface, and the far end now agrees with the new contract —
    so every other check PASSES and the click lands somewhere meaningless.
    """
    from yrdp import session

    monkeypatch.setattr(session, "sessions_dir", lambda: tmp_path)
    s = _session()
    session.check_point(s, 800, 400, None)  # fine before anyone moves anything

    assert session.repin(s, "1600x900@1.0", by="viewer 'seat' adopt") is True
    assert s.geometry_epoch == 1 and s.observed_at_epoch == 0

    with pytest.raises(session.SessionError) as caught:
        session.check_point(s, 800, 400, None)
    message = str(caught.value)
    assert "1600x900@1.0" in message, "the refusal must name what the surface became"
    assert "viewer 'seat' adopt" in message, "and WHO moved it — 'stale' sends the reader hunting"

    # Re-observing is what clears it, because a fresh picture is the only thing
    # that actually re-synchronises the agent with the surface.
    s.observed_at_epoch = s.geometry_epoch
    session.check_point(s, 800, 400, None)


def test_a_quoted_epoch_is_checked_exactly(monkeypatch, tmp_path):
    """An explicit stamp beats an inferred one, in both directions."""
    from yrdp import session

    monkeypatch.setattr(session, "sessions_dir", lambda: tmp_path)
    s = _session()
    session.repin(s, "1600x900@1.0", by="guest display settings")
    s.observed_at_epoch = s.geometry_epoch  # observed AFTER the change

    session.check_point(s, 10, 10, None, from_epoch=1)  # current: allowed
    with pytest.raises(session.SessionError) as caught:
        session.check_point(s, 10, 10, None, from_epoch=0)  # read before it moved
    assert "epoch 0" in str(caught.value)


def test_repinning_to_the_same_size_invalidates_nothing(monkeypatch, tmp_path):
    """A viewer that attaches at the contract geometry must cost nothing.

    If any attach bumped the epoch, the default (scaled) attach would invalidate
    the agent's coordinates every time a human glanced at the surface — which
    would make co-browse expensive and teach agents to avoid it.
    """
    from yrdp import session

    monkeypatch.setattr(session, "sessions_dir", lambda: tmp_path)
    s = _session()
    assert session.repin(s, "1920x1080@1.0", by="viewer 'seat' scaled") is False
    assert s.geometry_epoch == 0
    assert s.resized_by == ""
    session.check_point(s, 800, 400, None)


def test_a_screenshot_stamps_the_epoch_it_was_taken_at(monkeypatch, tmp_path):
    """The returned pixels have to say WHICH surface state they describe."""
    from yrdp import session

    monkeypatch.setattr(session, "sessions_dir", lambda: tmp_path)
    s = _session()
    session.repin(s, "1600x900@1.0", by="guest display settings")
    assert session.describe(s)["geometry_stale"] is True

    monkeypatch.setattr(session, "_direct", lambda _s: True)
    monkeypatch.setattr(session, "_rfb_client", lambda *a, **k: _FakeRfb())
    out = tmp_path / "shot.png"
    result = session.screenshot(s, out)
    assert result["epoch"] == s.geometry_epoch == 1
    assert session.describe(s)["geometry_stale"] is False


class _FakeRfb:
    """Just enough of an RFB client for the capture path."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def capture(self):
        from yrdp.rfb import Frame

        return Frame(2, 2, bytearray(2 * 2 * 4), complete=True)
