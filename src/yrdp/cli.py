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
import json
import sys
from pathlib import Path
from typing import Any

from . import lore, session, substrate
from .config import ConfigError, Target, list_targets, load_target, targets_dir
from .geometry import GeometryMismatch

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
            "surface_mode": t.surface_mode,
            "endpoint": f"{conn.host}:{conn.port}" if conn else None,
            "user": conn.user if conn else None,
            "credential_entry": conn.password_vault_entry if conn else None,
            "host_shell": bool(t.host_shell),
            "hooks": sorted(t.hooks),
            "source": str(t.source),
        },
        f"{t.name}: {t.surface_mode} surface pinned at {t.geometry.stamp}",
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
        surface_mode=t.surface_mode,
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


# -- sessions ---------------------------------------------------------------


def cmd_open(args: argparse.Namespace) -> int:
    t = _target(args)
    s = session.open_session(
        t, password_entry=args.password_entry, connect_timeout=args.timeout, force=args.force
    )
    # Recall is not a flag and not optional: a skill an agent must remember to
    # load is a skill an agent forgets, so opening a session prints the lore.
    lore.recall(t.name)
    return _emit(
        session.describe(s),
        f"{t.name}: {t.surface_mode} surface on {s.display} pinned at {s.geometry}"
        + ("" if s.window_found else " (client alive, nothing mapped yet — screenshot it)"),
    )


def cmd_list(args: argparse.Namespace) -> int:
    rows = [session.describe(s) for s in session.all_sessions()]
    return _emit(rows, f"{sum(1 for r in rows if r['alive'])} live of {len(rows)} recorded")


def cmd_close(args: argparse.Namespace) -> int:
    return _emit(
        {"target": args.target, "closed": session.close_session(args.target)},
        f"{args.target}: closed",
    )


def cmd_screenshot(args: argparse.Namespace) -> int:
    s = session.live_session(args.target)
    rect = None
    if args.rect:
        parts = [int(v) for v in args.rect.split(",")]
        if len(parts) != 4:
            raise SystemExit(f"[{PROG}] --rect wants x,y,w,h")
        rect = tuple(parts)  # type: ignore[assignment]
    out = session.screenshot(s, Path(args.out).expanduser(), rect=rect)
    return _emit(
        {"path": str(out), "geometry": s.geometry, "rect": args.rect},
        f"{args.target}: {'crop' if rect else 'full surface'} -> {out}",
    )


def cmd_do(args: argparse.Namespace) -> int:
    s = session.live_session(args.target)
    if args.action == "click":
        if not args.at:
            raise SystemExit(f"[{PROG}] click needs --at X,Y")
        x, y = (int(v) for v in args.at.split(","))
        session.click(s, x, y, button=args.button, proven=args.proven)
        did = f"click {x},{y}"
    elif args.action == "type":
        session.type_text(s, args.text or "")
        did = "type"
    else:
        session.key(s, args.text or "")
        did = f"key {args.text}"
    return _emit({"target": args.target, "did": did, "geometry": s.geometry}, f"{args.target}: {did}")


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
    op.set_defaults(func=cmd_open)
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
        "--proven",
        help="the geometry this coordinate was proven at (from lore). A mismatch is "
        "REFUSED, never approximated; omit it only for a coordinate you just read "
        "off this session's own screenshot.",
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
