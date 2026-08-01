"""``yrdp`` — a general-purpose, agent-first RDP client.

The verb vocabulary mirrors ychrome's on purpose: an agent that can drive a web
surface must be able to drive an RDP surface without learning a second language.
Data verbs print JSON on stdout (an agent parses it) and a sentence on stderr (a
person reads it), so one invocation serves both.

Every verb here is generic.  Nothing in this file knows what runs on the far end
— which application, which operating system, which hypervisor.  That knowledge
belongs to two places that are not this tool: the target file that says where to
connect, and the lore that says what to do once connected.

    yrdp targets / show         what this installation is configured for
    yrdp state                  does the endpoint answer, and is a session live
    yrdp up / down / hook       site-specific mechanisms, run as configured data
    yrdp exec                   run a command on the machine hosting the target
    yrdp open / close / list    sessions, pinned to the geometry contract
    yrdp screenshot / do        read and drive the surface
    yrdp lore                   recall — the thing you cannot forget to do
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import lore, session, substrate, view
from . import config as config_mod
from .config import ConfigError, Target, list_targets, load_target, targets_dir
from .geometry import Geometry, GeometryMismatch

PROG = "yrdp"


def _emit(payload: Any, summary: str = "") -> int:
    json.dump(payload, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    if summary:
        print(f"[{PROG}] {summary}", file=sys.stderr)
    return 0


def _target(args: argparse.Namespace) -> Target:
    return load_target(args.target)


# -- what this installation knows -------------------------------------------


def cmd_targets(args: argparse.Namespace) -> int:
    names = list_targets()
    return _emit(
        {"targets_dir": str(targets_dir()), "targets": names},
        f"{len(names)} target(s) in {targets_dir()}",
    )


def cmd_show(args: argparse.Namespace) -> int:
    t = _target(args)
    conn = t.connection
    return _emit(
        {
            "name": t.name,
            "kind": t.kind,
            "description": t.description,
            "geometry": t.geometry.stamp,
            "protocol": conn.protocol if conn else None,
            "endpoint": f"{conn.host}:{conn.port}" if conn else None,
            "user": conn.user if conn else None,
            "credential_entry": conn.password_vault_entry if conn else None,
            "host_shell": bool(t.host_shell),
            "hooks": sorted(t.hooks),
            "source": str(t.source),
        },
        f"{t.name}: {conn.protocol if conn else 'no'} target pinned at {t.geometry.stamp}",
    )


# -- state and the substrate seam -------------------------------------------


def cmd_state(args: argparse.Namespace) -> int:
    t = _target(args)
    conn = t.connection
    live = session.load(t.name)
    hook_result = None
    if args.with_hook and args.with_hook in t.hooks:
        try:
            hook_result = substrate.run_hook(t, args.with_hook)
        except substrate.HookError as exc:
            hook_result = {"hook": args.with_hook, "error": str(exc)}
    st = substrate.State(
        target=t.name,
        endpoint=f"{conn.host}:{conn.port}" if conn else "",
        state=substrate.REACHABLE if substrate.reachable(t) else substrate.UNREACHABLE,
        protocol=conn.protocol if conn else "",
        geometry=t.geometry.stamp,
        session=session.describe(live) if live else None,
        hook=hook_result,
    )
    return _emit(st.as_dict(), f"{t.name}: {st.state} at {st.endpoint}")


def cmd_hook(args: argparse.Namespace) -> int:
    t = _target(args)
    result = substrate.run_hook(t, args.name, *args.extra, timeout=args.timeout)
    return _emit(result, f"{t.name}: hook {args.name} ok")


def cmd_up(args: argparse.Namespace) -> int:
    """Convention, not magic: run the 'up' hook, then wait for the endpoint."""
    t = _target(args)
    if substrate.reachable(t):
        return _emit(
            {"target": t.name, "state": substrate.REACHABLE, "hook_run": False},
            f"{t.name}: already answering, 'up' hook not run",
        )
    result = substrate.run_hook(t, "up", timeout=args.timeout)
    deadline = args.wait
    import time as _time

    start = _time.monotonic()
    while _time.monotonic() - start < deadline and not substrate.reachable(t):
        _time.sleep(5)
    ok = substrate.reachable(t)
    return _emit(
        {"target": t.name, "state": substrate.REACHABLE if ok else substrate.UNREACHABLE, **result},
        f"{t.name}: {'answering' if ok else 'still not answering'} after the up hook",
    )


def cmd_down(args: argparse.Namespace) -> int:
    t = _target(args)
    result = substrate.run_hook(t, "down", timeout=args.timeout)
    return _emit(result, f"{t.name}: down hook ran")


def cmd_exec(args: argparse.Namespace) -> int:
    t = _target(args)
    result = substrate.host_exec(t, args.command, timeout=args.timeout)
    return _emit(result, f"{t.name}: exit {result['returncode']}")


def _protocol(args: argparse.Namespace) -> str | None:
    """--vnc overrides the target's declaration; there is one tool, not two."""
    return config_mod.PROTOCOL_VNC if getattr(args, "vnc", False) else None


# -- sessions ---------------------------------------------------------------


def cmd_open(args: argparse.Namespace) -> int:
    t = _target(args)
    s = session.open_session(
        t,
        password_entry=args.password_entry,
        protocol=_protocol(args),
        connect_timeout=args.timeout,
        force=args.force,
    )
    # Recall is not a flag and not optional: a skill an agent must remember to
    # load is a skill an agent forgets, so opening a session prints the lore.
    lore.recall(t.name)
    where = f"on {s.display}" if s.display else f"direct to {s.host}"
    drift = (
        "" if not s.server_geometry or s.server_geometry == s.geometry
        else f" ⚠ the far end is actually {s.server_geometry}"
    )
    return _emit(
        session.describe(s),
        f"{t.name}: {s.protocol} surface {where} pinned at {s.geometry}"
        + ("" if s.window_found else " (client alive, nothing mapped yet — screenshot it)")
        + drift,
    )


def cmd_list(args: argparse.Namespace) -> int:
    rows = [session.describe(s) for s in session.all_sessions()]
    return _emit(rows, f"{sum(1 for r in rows if r['alive'])} live of {len(rows)} recorded")


def cmd_close(args: argparse.Namespace) -> int:
    return _emit(
        {"target": args.target, "closed": session.close_session(args.target)},
        f"{args.target}: closed",
    )


def cmd_repin(args: argparse.Namespace) -> int:
    """Re-pin a live session's contract geometry, on the record.

    A contract that can change needs ONE door, and this is it.  The far end
    really does change size sometimes — a guest's display settings, a launcher
    edited, a viewer that adopted the surface — and when it does, the honest
    move is to say so rather than to let a stale contract quietly refuse every
    click with a message about the wrong thing.

    Re-pinning INVALIDATES every coordinate read before it.  That is the whole
    point, and it is why ``--by`` is required: a refusal that can name who moved
    the surface, and when, ends an investigation in one sentence instead of
    sending the next agent to debug an input path that was never broken.
    """
    s = session.live_session(args.target)
    previous, previous_epoch = s.geometry, s.geometry_epoch
    changed = session.repin(s, args.geometry, by=args.by)
    return _emit(
        {
            "target": args.target,
            "changed": changed,
            "geometry": s.geometry,
            "previous": previous,
            "epoch": s.geometry_epoch,
            "previous_epoch": previous_epoch,
            "resized_by": s.resized_by,
        },
        f"{args.target}: {previous} → {s.geometry} (epoch {s.geometry_epoch}), "
        f"every coordinate read before now is refused until you re-observe"
        if changed
        else f"{args.target}: already {s.geometry}; nothing re-pinned, nothing invalidated",
    )


def cmd_screenshot(args: argparse.Namespace) -> int:
    s = session.live_session(args.target)
    rect = None
    if args.rect:
        parts = [int(v) for v in args.rect.split(",")]
        if len(parts) != 4:
            raise SystemExit(f"[{PROG}] --rect wants x,y,w,h")
        rect = tuple(parts)  # type: ignore[assignment]
    shot = session.screenshot(s, Path(args.out).expanduser(), rect=rect)
    drift = "" if shot["observed"] == shot["geometry"] else f" ⚠ far end is {shot['observed']}"
    partial = "" if shot["complete"] else " ⚠ PARTIAL frame — not every rectangle arrived"
    return _emit(
        {**shot, "rect": args.rect},
        f"{args.target}: {'crop' if rect else 'full surface'} -> {shot['path']}{drift}{partial}",
    )


def cmd_do(args: argparse.Namespace) -> int:
    s = session.live_session(args.target)
    if args.action == "click":
        if not args.at:
            raise SystemExit(f"[{PROG}] click needs --at X,Y")
        x, y = (int(v) for v in args.at.split(","))
        session.click(s, x, y, button=args.button, proven=args.proven, from_epoch=args.from_epoch)
        did = f"click {x},{y}"
    elif args.action == "type":
        session.type_text(s, args.text or "")
        did = "type"
    else:
        session.key(s, args.text or "", hold_ms=args.hold_ms)
        did = f"key {args.text}" + (f" held {args.hold_ms}ms" if args.hold_ms else "")
    return _emit({"target": args.target, "did": did, "geometry": s.geometry}, f"{args.target}: {did}")


def cmd_view(args: argparse.Namespace) -> int:
    """Reveal a live session in the yggterm viewport, and hold it there.

    The session is NOT created for the view and NOT destroyed with it: a viewer
    attaches to whatever is already running, and detaching leaves it running.
    Several viewers may watch at once — that is co-browse, and it is the point.
    """
    return reveal_target(
        args.target,
        read_only=args.read_only,
        title=args.title,
        once=args.once,
        no_open=args.no_open,
        password_entry=args.password_entry,
        protocol=_protocol(args),
        quality=getattr(args, "quality", 8),
        compression=getattr(args, "compression", 2),
    )


def reveal_target(
    name: str,
    *,
    read_only: bool = False,
    title: str | None = None,
    once: bool = False,
    no_open: bool = False,
    password_entry: str | None = None,
    protocol: str | None = None,
    quality: int = 8,
    compression: int = 2,
) -> int:
    """The body of `view`, callable without an argparse Namespace.

    Extracted so the CHOOSER can hand off to it (`yrdp pick` becomes the viewer
    for whatever was picked, in the same process and therefore the same row).
    A second copy of this logic in `pick` would be a second place for the
    VNC-direct rule below to be got wrong.
    """
    t = config_mod.load_target(name)
    conn = t.connection

    # Start the guest if it is not answering. The chooser's row promises "not
    # running · will start it", and a promise the connect path cannot keep is
    # worse than no promise: the operator clicks, nothing happens, and the
    # reason is in a hook they never ran.
    if "up" in t.hooks and not substrate.reachable(t):
        print(f"[{PROG}] {name}: not answering — running its 'up' hook first", file=sys.stderr)
        substrate.run_hook(t, "up", timeout=600.0)
        deadline = time.monotonic() + 600.0
        while time.monotonic() < deadline and not substrate.reachable(t):
            time.sleep(5)

    direct = None
    s = session.load(name)
    if s is not None and not s.alive():
        s = None
    if s is None and conn is not None and conn.protocol == config_mod.PROTOCOL_VNC:
        # A VNC endpoint is already a framebuffer protocol, so a reveal needs
        # nothing standing in front of it — no session, no X export, no viewer.
        # Bridging it straight through is fewer moving parts, not a shortcut.
        direct = (conn.host, conn.port)
        if not session.reachable(t):
            raise SystemExit(
                f"[{PROG}] {conn.host}:{conn.port} is not answering; "
                f"`yrdp up --target {t.name}` runs this target's own 'up' hook"
            )
        lore.recall(t.name)
    elif s is None:
        if no_open:
            raise SystemExit(f"[{PROG}] no live session for {name} and --no-open was given")
        s = session.open_session(
            t, password_entry=password_entry, protocol=protocol, force=True
        )
        lore.recall(t.name)
    if not view.in_yggterm():
        print(
            f"[{PROG}] warning: no YGGTERM_SESSION_ID in this environment, so nothing "
            f"will consume the surface announcement. The URL below still works in any "
            f"browser that can reach this host's loopback.",
            file=sys.stderr,
        )
    viewer = view.attach(
        s,
        read_only=read_only,
        title=title,
        endpoint=direct,
        label=t.name,
        quality=quality,
        compression=compression,
    )
    where = f"on {s.display}" if s else f"straight from {direct[0]}:{direct[1]}"
    print(
        f"[{PROG}] {name}: revealed at {t.geometry.stamp} {where} "
        f"({'read-only' if read_only else 'interactive'}) — {viewer.url}",
        file=sys.stderr,
    )
    if once:
        return _emit(viewer.as_dict(), f"{name}: surface announced once, not held")
    print(f"[{PROG}] holding the surface; Ctrl-C detaches (the session keeps running)",
          file=sys.stderr)
    view.hold(s, viewer)
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    """`attach` — the named half of the dual-viewport contract.

    Two modes, and the default is the one that protects the agent:

    * ``--scaled`` (default) — the agent's pinned contract stays authoritative
      and the human sees it scaled into whatever viewport they have.  Their view
      may be letterboxed when the aspects differ; the agent's coordinates stay
      valid.  This is what ``view`` has always done.
    * ``--adopt`` — the HUMAN's viewport wins.  The surface is re-pinned to the
      size they name, so they get a pixel-exact full-bleed view and **every
      coordinate the agent holds is invalidated, loudly**: the epoch moves, the
      surface is dirty until re-observed, and a click quoting a stale epoch is
      refused with a message naming who resized it and when.

    ``--adopt`` is deliberately not the default and deliberately not implicit.
    It is the answer to "I hid my cwd tree and the picture no longer fits",
    because rescaling cannot fix an ASPECT change — only re-pinning can.
    """
    if not args.adopt:
        return reveal_target(
            args.target,
            read_only=args.read_only,
            title=args.title,
            once=args.once,
            no_open=args.no_open,
            password_entry=args.password_entry,
            protocol=_protocol(args),
            quality=args.quality,
            compression=args.compression,
        )

    t = _target(args)
    # A VNC target's framebuffer belongs to the GUEST, not to us: we are a
    # CLIENT of a console someone else sized. Refusing is the honest answer —
    # silently scaling while calling it "adopt" would be a lie of success, and
    # the macOS console in particular is pinned by its VM's video device.
    if t.connection is not None and t.connection.protocol == config_mod.PROTOCOL_VNC:
        raise SystemExit(
            f"[{PROG}] {t.name} speaks VNC, so its framebuffer is the guest's and yRDP "
            f"cannot re-pin it — only an RDP session negotiates its own size. Attach "
            f"it --scaled, or change the size at the guest and `yrdp repin` to record it."
        )
    if not args.geometry:
        raise SystemExit(
            f"[{PROG}] --adopt needs --geometry WxH: the viewport size is the human's, "
            f"and yRDP cannot see it from here. Read it off `server app state`'s window "
            f"inner_size, or from a `web screenshot --session` of the surface."
        )

    adopted = Geometry.parse(args.geometry)
    previous = session.load(args.target)
    # Carry the epoch ACROSS the reopen. A fresh session would start at 0, and an
    # epoch that goes backwards is worse than none: a coordinate stamped at the
    # old surface's epoch 1 would compare equal to the new surface's epoch 1 and
    # the refusal that should have fired would not.
    carried = (previous.geometry_epoch if previous else 0) + 1
    was = previous.geometry if previous else t.geometry.stamp

    reopened = dataclasses.replace(t, geometry=adopted)
    s = session.open_session(
        reopened, password_entry=args.password_entry, protocol=_protocol(args), force=True
    )
    s.geometry_epoch = carried
    s.resized_by = args.by or "human viewport (attach --adopt)"
    s.resized_at = time.time()
    s.events.append(f"adopted {was} → {s.geometry} (epoch {carried})")
    session.save(s)
    print(
        f"[{PROG}] {t.name}: adopted {was} → {s.geometry} (epoch {carried}) — "
        f"every coordinate read before now is refused until you re-observe",
        file=sys.stderr,
    )
    return reveal_target(
        args.target,
        read_only=args.read_only,
        title=args.title,
        once=args.once,
        no_open=True,  # the session we just opened IS the one to reveal
        quality=args.quality,
        compression=args.compression,
    )


def cmd_detach(args: argparse.Namespace) -> int:
    """`detach` — stop looking. NEVER stops the session.

    The invariant the whole split rests on: detach is about *looking*, never
    about *living*. The surface, its processes and its pinned geometry all
    survive, which is what makes an agent surface safe to hand to a human and
    take back.
    """
    s = session.load(args.target)
    closed = view.detach(s)
    return _emit(
        {"target": args.target, "viewers_closed": closed, "session_alive": bool(s and s.alive())},
        f"{args.target}: {closed} viewer(s) closed; the session keeps running",
    )


def cmd_pick(args: argparse.Namespace) -> int:
    from . import pick

    return pick.run(quality=args.quality, compression=args.compression)


def _quality_flags(parser: argparse.ArgumentParser) -> None:
    """Picture-quality knobs, shared by every verb that reveals a surface.

    The defaults are chosen for THIS link, which is a loopback socket carried
    over an ssh tunnel on a LAN — not the internet the VNC defaults assume.

    ``--quality 9`` is the important one: below 9 the Tight encoding sends
    photographic regions as JPEG, and a desktop is not photographic.  JPEG on
    antialiased text produces ringing around every glyph, which reads exactly
    like "the remote desktop is blurry" and is the single biggest quality
    complaint on this path.  ``--compression 0`` then trades bandwidth we have
    for CPU we would rather not spend on both ends.
    """
    parser.add_argument(
        "--quality",
        type=int,
        default=9,
        choices=range(0, 10),
        metavar="0-9",
        help="picture quality; 9 keeps text lossless (default: 9)",
    )
    parser.add_argument(
        "--compression",
        type=int,
        default=0,
        choices=range(0, 10),
        metavar="0-9",
        help="wire compression; 0 spends bandwidth to save latency (default: 0)",
    )


def cmd_lore(args: argparse.Namespace) -> int:
    return 0 if lore.recall(args.target, stream=sys.stdout) else 1


# -- parser -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG, description="a general-purpose, agent-first RDP client"
    )
    sub = p.add_subparsers(dest="verb", required=True)

    def wt(sp):
        sp.add_argument("--target", "-t", required=True, help="target name (see `yrdp targets`)")
        return sp

    sub.add_parser("targets", help="list configured targets").set_defaults(func=cmd_targets)
    wt(sub.add_parser("show", help="the resolved target, secrets excluded")).set_defaults(
        func=cmd_show
    )
    st = wt(sub.add_parser("state", help="does the endpoint answer; is a session live"))
    st.add_argument("--with-hook", help="also run this hook and include its output")
    st.set_defaults(func=cmd_state)

    hk = wt(sub.add_parser("hook", help="run a site-specific hook declared by the target"))
    hk.add_argument("name")
    hk.add_argument("extra", nargs="*", help="extra arguments appended to the hook")
    hk.add_argument("--timeout", type=float, default=600.0)
    hk.set_defaults(func=cmd_hook)

    up = wt(sub.add_parser("up", help="run the 'up' hook, then wait for the endpoint"))
    up.add_argument("--timeout", type=float, default=600.0)
    up.add_argument("--wait", type=float, default=420.0)
    up.set_defaults(func=cmd_up)
    dn = wt(sub.add_parser("down", help="run the 'down' hook"))
    dn.add_argument("--timeout", type=float, default=600.0)
    dn.set_defaults(func=cmd_down)

    ex = wt(sub.add_parser("exec", help="run a command on the machine hosting the target"))
    ex.add_argument("command")
    ex.add_argument("--timeout", type=float, default=300.0)
    ex.set_defaults(func=cmd_exec)

    op = wt(sub.add_parser("open", help="connect, pin the geometry, print the lore"))
    op.add_argument("--password-entry", help="vault entry NAME holding the password")
    op.add_argument("--timeout", type=float, default=session.CONNECT_TIMEOUT)
    op.add_argument("--force", action="store_true")
    op.add_argument("--vnc", action="store_true", help="speak VNC instead of the target's protocol")
    op.set_defaults(func=cmd_open)

    rp = wt(sub.add_parser("repin", help="change a live session's contract geometry, on the record"))
    rp.add_argument("--geometry", required=True, help="the new contract, e.g. 1600x900@1.0")
    rp.add_argument(
        "--by",
        required=True,
        help="WHO is changing it (e.g. 'viewer adopt', 'guest display settings'). Required "
        "because the refusal this causes has to be able to name a cause — 'stale' sends "
        "the reader hunting, 'a viewer adopted this 4m ago' ends it.",
    )
    rp.set_defaults(func=cmd_repin)

    vw = wt(sub.add_parser("view", help="reveal a live session in the yggterm viewport"))
    vw.add_argument("--read-only", action="store_true", help="watch without being able to act")
    vw.add_argument("--title", help="surface title shown by yggterm")
    vw.add_argument("--once", action="store_true", help="announce and exit instead of holding")
    vw.add_argument("--no-open", action="store_true", help="fail if no session is already live")
    vw.add_argument("--password-entry", help="vault entry NAME, if a session must be opened")
    vw.add_argument("--vnc", action="store_true")
    _quality_flags(vw)
    vw.set_defaults(func=cmd_view)

    at = wt(sub.add_parser("attach", help="put this surface in a human viewport"))
    at.add_argument("--scaled", action="store_true",
                    help="the agent's contract wins; the human sees it scaled (default)")
    at.add_argument("--adopt", action="store_true",
                    help="the human's viewport wins; RE-PINS the surface and invalidates coordinates")
    at.add_argument("--geometry", help="WxH[@scale] to adopt (required with --adopt)")
    at.add_argument("--by", help="who is adopting, recorded on the epoch bump")
    at.add_argument("--read-only", action="store_true")
    at.add_argument("--title")
    at.add_argument("--once", action="store_true")
    at.add_argument("--no-open", action="store_true")
    at.add_argument("--password-entry")
    at.add_argument("--vnc", action="store_true")
    _quality_flags(at)
    at.set_defaults(func=cmd_attach)

    wt(sub.add_parser("detach", help="take a surface out of the viewport; it keeps running")
       ).set_defaults(func=cmd_detach)

    pk = sub.add_parser(
        "pick",
        help="choose a target in the viewport, then become its viewer",
    )
    _quality_flags(pk)
    pk.set_defaults(func=cmd_pick)
    sub.add_parser("list", help="every recorded session").set_defaults(func=cmd_list)
    wt(sub.add_parser("close", help="end a session")).set_defaults(func=cmd_close)

    ss = wt(sub.add_parser("screenshot", help="capture the pinned surface"))
    ss.add_argument("--out", "-o", default="surface.png")
    ss.add_argument("--rect", help="x,y,w,h — crop first, per the ladder")
    ss.set_defaults(func=cmd_screenshot)

    do = wt(sub.add_parser("do", help="act on the surface"))
    do.add_argument("action", choices=["click", "type", "key"])
    do.add_argument("text", nargs="?", help="text to type, or a chord like ctrl+shift+o")
    do.add_argument("--at", help="X,Y on the pinned surface")
    do.add_argument("--button", type=int, default=1)
    do.add_argument(
        "--hold-ms",
        type=int,
        default=0,
        help="hold a key down this long before releasing it. Firmware and boot "
        "pickers poll slowly and can miss a press released within a frame — which "
        "looks exactly like keys not arriving at all.",
    )
    do.add_argument(
        "--proven",
        help="the geometry this coordinate was proven at (from lore). A mismatch is "
        "REFUSED, never approximated; omit it only for a coordinate you just read "
        "off this session's own screenshot.",
    )
    do.add_argument(
        "--from-epoch",
        type=int,
        help="the geometry epoch this coordinate was read at — the `epoch` field a "
        "screenshot returns. Quoting it lets the refusal be exact. Without it, a "
        "coordinate is refused outright while the surface has been re-pinned since "
        "the last observation, because that is the case where every other check "
        "passes and the click still lands in the wrong place.",
    )
    do.set_defaults(func=cmd_do)

    wt(sub.add_parser("lore", help="print everything the store knows")).set_defaults(func=cmd_lore)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (
        ConfigError,
        substrate.HookError,
        session.SessionError,
        view.ViewError,
        GeometryMismatch,
    ) as exc:
        # The tool's own refusals and diagnoses, not tracebacks: they are meant
        # to be read and acted on, so they print as sentences.
        print(f"[{PROG}] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
