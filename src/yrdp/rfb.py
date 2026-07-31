"""RFB, spoken directly — the VNC lane with nothing standing in front of it.

The first VNC lane went the same way as the RDP one: a headless X display, a
real VNC viewer living inside it, ``xdotool`` to drive it and ImageMagick to
photograph it.  For RDP that indirection buys something real, because a client
binary is the only practical way to speak that protocol.  For VNC it bought a
whole X server, a GUI viewer, a window manager's worth of assumptions — and it
did not work: the viewer authenticated happily and then painted nothing
headless, while wedging other X clients on the display.

**The reveal path had already proved the fix.**  ``yrdp view`` on a VNC target
bridges the endpoint straight through, no X anywhere, and it works.  This module
applies the same move to the agent lane, and the result deletes code rather than
adding a mode: a framebuffer protocol needs no framebuffer emulator.

What follows is deliberately small.  Raw encoding only, one pixel format, no
compression, no cursor pseudo-encodings — a screenshot of a pinned surface and a
handful of input events do not need more, and every encoding not implemented is
one that cannot arrive malformed.

**The geometry contract, in protocol form.**  A client tells the server which
encodings it understands, and two of those are how a server announces that it
has resized the desktop underneath us.  We never advertise them
(:data:`FORBIDDEN_ENCODINGS`), so the far end has no way to renegotiate our
surface — the same rule ``-RemoteResize=0`` and ``/size:`` express for spawned
clients, enforced here by omission rather than by a flag.  What the server
declares at connect time is recorded as the OBSERVED geometry and compared with
the contract at the point of action, because a coordinate is only meaningful
against the surface it was proven on.

Every connection is short-lived by design: connect, act, disconnect.  A VNC
console is a view onto a framebuffer that exists whether or not anyone is
attached — unlike an RDP logon session, which dies when its client leaves — so
holding a socket open between verbs would be state we do not need and a process
we would have to supervise.  That is why this backend has no client process,
no display, and nothing to leak.
"""

from __future__ import annotations

import socket
import struct
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

from . import vncauth

#: The only encoding this client asks for.  Mandatory in every server.
ENCODING_RAW = 0

#: Pseudo-encodings that hand the FAR END control of our surface size, named so
#: a test can assert we never advertise them.  Their absence IS the geometry
#: contract: a server cannot resize a client that never said it would listen.
FORBIDDEN_ENCODINGS = {
    -223: "DesktopSize",
    -308: "ExtendedDesktopSize",
}

ADVERTISED_ENCODINGS = (ENCODING_RAW,)

SECURITY_NONE = 1
SECURITY_VNC_AUTH = 2

#: Server-to-client message types we may meet.  Anything else is a protocol
#: violation we refuse loudly rather than resynchronising by guesswork.
MSG_FRAMEBUFFER_UPDATE = 0
MSG_SET_COLOUR_MAP = 1
MSG_BELL = 2
MSG_SERVER_CUT_TEXT = 3

BYTES_PER_PIXEL = 4


class RfbError(Exception):
    """The conversation with the far end could not be completed."""


class RfbAuthError(RfbError):
    """The far end refused the credential, or wants one we cannot supply.

    Kept distinct because the recovery is different in kind: a password is the
    operator's to fix, an unreachable endpoint is the machine's.
    """


# -- keys --------------------------------------------------------------------

#: X keysyms, deliberately the SAME vocabulary ``xdotool`` accepts, so that
#: ``yrdp do key Return`` means one thing across both backends.  Two spellings
#: are allowed where a name is genuinely ambiguous in the wild (``enter``,
#: ``pgup``); everything else is the X name.
KEYSYMS = {
    "backspace": 0xFF08, "tab": 0xFF09, "return": 0xFF0D, "enter": 0xFF0D,
    "escape": 0xFF1B, "esc": 0xFF1B, "space": 0x0020, "delete": 0xFFFF,
    "insert": 0xFF63, "home": 0xFF50, "end": 0xFF57,
    "left": 0xFF51, "up": 0xFF52, "right": 0xFF53, "down": 0xFF54,
    "page_up": 0xFF55, "pgup": 0xFF55, "prior": 0xFF55,
    "page_down": 0xFF56, "pgdn": 0xFF56, "next": 0xFF56,
    "kp_enter": 0xFF8D, "kp_add": 0xFFAB, "kp_subtract": 0xFFAD,
    "shift": 0xFFE1, "shift_l": 0xFFE1, "shift_r": 0xFFE2,
    "control": 0xFFE3, "ctrl": 0xFFE3, "control_l": 0xFFE3, "control_r": 0xFFE4,
    "alt": 0xFFE9, "alt_l": 0xFFE9, "alt_r": 0xFFEA,
    "super": 0xFFEB, "super_l": 0xFFEB, "super_r": 0xFFEC, "meta": 0xFFE7,
    "caps_lock": 0xFFE5, "num_lock": 0xFF7F, "scroll_lock": 0xFF14,
    "menu": 0xFF67, "print": 0xFF61, "pause": 0xFF13,
    **{f"f{n}": 0xFFBD + n for n in range(1, 13)},
}

#: Characters a US layout puts behind Shift.  Sent with an explicit Shift press
#: rather than trusting every server to infer it from the keysym alone.
_SHIFTED = set('~!@#$%^&*()_+{}|:"<>?')


def keysym(name: str) -> int:
    """One key, by X name or by the single character it types."""
    if len(name) == 1:
        return ord(name)
    sym = KEYSYMS.get(name.lower())
    if sym is None:
        raise RfbError(
            f"unknown key {name!r}. Use an X keysym name (Return, Escape, F5, Left) "
            f"or a single character."
        )
    return sym


def parse_chord(chord: str) -> tuple[list[int], int]:
    """``ctrl+alt+Delete`` -> the modifiers to hold, and the key to strike."""
    if not chord:
        raise RfbError("an empty chord acts on nothing")
    parts = [p for p in chord.split("+") if p != ""]
    if chord.endswith("+"):  # a literal '+' is the key being struck
        parts.append("+")
    if not parts:
        raise RfbError(f"nothing to strike in {chord!r}")
    *mods, key = parts
    return [keysym(m) for m in mods], keysym(key)


# -- frames ------------------------------------------------------------------


@dataclass(slots=True)
class Frame:
    """A captured framebuffer, in the one pixel format this client requests."""

    width: int
    height: int
    #: 4 bytes per pixel, little-endian 0x00RRGGBB, so: blue, green, red, pad.
    pixels: bytearray
    #: False when the server had not painted every rectangle before the
    #: deadline.  Reported rather than hidden: a partly-filled frame is a
    #: perfectly good diagnostic and a terrible thing to mistake for a whole one.
    complete: bool = True

    def crop(self, x: int, y: int, w: int, h: int) -> "Frame":
        if x < 0 or y < 0 or x + w > self.width or y + h > self.height:
            raise RfbError(
                f"refusing crop {x},{y} {w}x{h}: not inside the {self.width}x{self.height} "
                f"framebuffer the far end actually has"
            )
        out = bytearray(w * h * BYTES_PER_PIXEL)
        for row in range(h):
            src = ((y + row) * self.width + x) * BYTES_PER_PIXEL
            dst = row * w * BYTES_PER_PIXEL
            out[dst:dst + w * BYTES_PER_PIXEL] = self.pixels[src:src + w * BYTES_PER_PIXEL]
        return Frame(w, h, out, self.complete)

    def png(self) -> bytes:
        """A PNG, written by hand — the stdlib has zlib, and that is all it takes."""
        # BGRX -> RGB by slicing, not by looping over a million pixels in Python:
        # drop the pad byte, then swap the blue and red planes in place.
        rgb = bytearray(self.pixels)
        del rgb[3::4]
        rgb[0::3], rgb[2::3] = rgb[2::3], rgb[0::3]

        stride = self.width * 3
        raw = bytearray()
        for row in range(self.height):
            raw.append(0)  # filter: none
            raw += rgb[row * stride:(row + 1) * stride]

        def chunk(kind: bytes, data: bytes) -> bytes:
            body = kind + data
            return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

        header = struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b"")
        )

    def write_png(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.png())
        return path


# -- the client --------------------------------------------------------------


class RfbClient:
    """One short conversation with a VNC server: connect, act, disconnect."""

    def __init__(self, sock: socket.socket, width: int, height: int, name: str, version: str):
        self.sock = sock
        self.width = width
        self.height = height
        self.name = name
        self.version = version

    # -- lifecycle

    @classmethod
    def connect(
        cls,
        host: str,
        port: int,
        *,
        password: str | None = None,
        timeout: float = 15.0,
        shared: bool = True,
    ) -> "RfbClient":
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise RfbError(f"could not reach {host}:{port}: {exc}") from exc
        sock.settimeout(timeout)
        try:
            version = _handshake_version(sock)
            _handshake_security(sock, version, password)
            sock.sendall(bytes((1 if shared else 0,)))
            width, height, name = _server_init(sock)
            client = cls(sock, width, height, name, version)
            client._set_pixel_format()
            client._set_encodings()
            return client
        except BaseException:
            sock.close()
            raise

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:  # pragma: no cover
            pass

    def __enter__(self) -> "RfbClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def geometry(self) -> str:
        """What the far end actually is, in the same stamp form as the contract."""
        return f"{self.width}x{self.height}@1.0"

    # -- setup

    def _set_pixel_format(self) -> None:
        # 32bpp, depth 24, little-endian, true colour, R<<16 G<<8 B. One format,
        # always: a client that negotiates several has several decoders to keep
        # honest, and this one needs none of them.
        fmt = struct.pack(
            ">BBBBHHHBBBxxx",
            32, 24, 0, 1,
            255, 255, 255,
            16, 8, 0,
        )
        self.sock.sendall(struct.pack(">Bxxx", 0) + fmt)

    def _set_encodings(self) -> None:
        for code in ADVERTISED_ENCODINGS:
            if code in FORBIDDEN_ENCODINGS:  # pragma: no cover - guarded by test
                raise RfbError(
                    f"refusing to advertise {FORBIDDEN_ENCODINGS[code]}: it would let the "
                    f"far end resize the pinned surface, which is the one thing the "
                    f"geometry contract exists to prevent"
                )
        body = struct.pack(">BxH", 2, len(ADVERTISED_ENCODINGS))
        body += b"".join(struct.pack(">i", code) for code in ADVERTISED_ENCODINGS)
        self.sock.sendall(body)

    # -- reading

    def capture(self, *, timeout: float = 20.0) -> Frame:
        """Ask for the whole surface and read until it has been painted."""
        pixels = bytearray(self.width * self.height * BYTES_PER_PIXEL)
        wanted = self.width * self.height
        painted = 0
        deadline = time.monotonic() + timeout
        rounds = 0
        while painted < wanted and time.monotonic() < deadline and rounds < 8:
            self._request(incremental=False)
            painted += self._read_update(pixels)
            rounds += 1
        return Frame(self.width, self.height, pixels, complete=painted >= wanted)

    def _request(self, *, incremental: bool, rect: tuple[int, int, int, int] | None = None) -> None:
        x, y, w, h = rect or (0, 0, self.width, self.height)
        self.sock.sendall(struct.pack(">BBHHHH", 3, 1 if incremental else 0, x, y, w, h))

    def _read_update(self, pixels: bytearray) -> int:
        """Apply one FramebufferUpdate, returning how many pixels it painted."""
        while True:
            kind = _recv(self.sock, 1)[0]
            if kind == MSG_FRAMEBUFFER_UPDATE:
                break
            if kind == MSG_BELL:
                continue
            if kind == MSG_SET_COLOUR_MAP:
                _, _, count = struct.unpack(">BHH", _recv(self.sock, 5))
                _recv(self.sock, count * 6)
                continue
            if kind == MSG_SERVER_CUT_TEXT:
                length = struct.unpack(">xxxI", _recv(self.sock, 7))[0]
                _recv(self.sock, length)
                continue
            raise RfbError(f"the server sent message type {kind}, which this client never asked for")

        count = struct.unpack(">xH", _recv(self.sock, 3))[0]
        painted = 0
        for _ in range(count):
            x, y, w, h, encoding = struct.unpack(">HHHHi", _recv(self.sock, 12))
            if encoding != ENCODING_RAW:
                raise RfbError(
                    f"the server used encoding {encoding} after being told this client "
                    f"understands only Raw"
                )
            data = _recv(self.sock, w * h * BYTES_PER_PIXEL)
            row_bytes = w * BYTES_PER_PIXEL
            for row in range(h):
                dst = ((y + row) * self.width + x) * BYTES_PER_PIXEL
                pixels[dst:dst + row_bytes] = data[row * row_bytes:(row + 1) * row_bytes]
            painted += w * h
        return painted

    # -- driving

    def key_event(self, sym: int, down: bool) -> None:
        self.sock.sendall(struct.pack(">BBxxI", 4, 1 if down else 0, sym))

    def press(self, chord: str, *, hold_ms: int = 0) -> None:
        """Strike a chord, optionally HOLDING it.

        The hold is not a nicety.  Firmware and boot pickers routinely poll the
        keyboard on a slow loop and miss a press that is released within a
        frame — a real key held by a real finger is down for ~100 ms, and a
        client that always releases immediately can look, wrongly, like a client
        whose keys never arrive at all.
        """
        mods, sym = parse_chord(chord)
        for mod in mods:
            self.key_event(mod, True)
        self.key_event(sym, True)
        if hold_ms > 0:
            time.sleep(hold_ms / 1000.0)
        self.key_event(sym, False)
        for mod in reversed(mods):
            self.key_event(mod, False)

    def type_text(self, text: str, *, delay_ms: int = 12) -> None:
        shift = KEYSYMS["shift_l"]
        for char in text:
            needs_shift = char.isupper() or char in _SHIFTED
            if needs_shift:
                self.key_event(shift, True)
            self.key_event(ord(char), True)
            self.key_event(ord(char), False)
            if needs_shift:
                self.key_event(shift, False)
            if delay_ms:
                time.sleep(delay_ms / 1000.0)

    def pointer_event(self, x: int, y: int, mask: int = 0) -> None:
        self.sock.sendall(struct.pack(">BBHH", 5, mask, x, y))

    def click(self, x: int, y: int, *, button: int = 1, hold_ms: int = 40) -> None:
        mask = 1 << (button - 1)
        self.pointer_event(x, y, 0)  # move first: some far ends ignore a click that teleports
        time.sleep(0.03)
        self.pointer_event(x, y, mask)
        time.sleep(max(hold_ms, 0) / 1000.0)
        self.pointer_event(x, y, 0)


# -- handshake ---------------------------------------------------------------


def _recv(sock: socket.socket, count: int) -> bytes:
    buf = bytearray()
    while len(buf) < count:
        try:
            chunk = sock.recv(count - len(buf))
        except socket.timeout as exc:
            raise RfbError(f"the server went quiet after {len(buf)} of {count} bytes") from exc
        if not chunk:
            raise RfbError("the server closed the connection mid-message")
        buf += chunk
    return bytes(buf)


def _handshake_version(sock: socket.socket) -> str:
    banner = _recv(sock, 12)
    if not banner.startswith(b"RFB ") or not banner.endswith(b"\n"):
        raise RfbError(f"not an RFB endpoint: it opened with {banner!r}")
    try:
        major, minor = (int(part) for part in banner[4:11].split(b"."))
    except ValueError as exc:
        raise RfbError(f"cannot read the RFB version out of {banner!r}") from exc
    if major != 3:
        raise RfbError(f"RFB major version {major} is not one this client speaks")
    spoken = min(minor, 8) if minor >= 7 else 3
    sock.sendall(b"RFB 003.%03d\n" % spoken)
    return f"3.{spoken}"


def _handshake_security(sock: socket.socket, version: str, password: str | None) -> None:
    if version == "3.3":
        chosen = struct.unpack(">I", _recv(sock, 4))[0]
        if chosen == 0:
            raise RfbAuthError(f"the server refused the connection: {_read_reason(sock)}")
    else:
        count = _recv(sock, 1)[0]
        if count == 0:
            raise RfbAuthError(f"the server refused the connection: {_read_reason(sock)}")
        offered = set(_recv(sock, count))
        if password is not None and SECURITY_VNC_AUTH in offered:
            chosen = SECURITY_VNC_AUTH
        elif SECURITY_NONE in offered:
            chosen = SECURITY_NONE
        elif SECURITY_VNC_AUTH in offered:
            raise RfbAuthError(
                "this endpoint wants a VNC password and none was resolved. Name a vault "
                "entry in the target's [connection], or set YRDP_RDP_PASSWORD once."
            )
        else:
            raise RfbAuthError(
                f"the server offers only security types {sorted(offered)}; this client "
                f"speaks None (1) and VNC password authentication (2). Nothing here is "
                f"one of those, so the endpoint needs a type this client does not "
                f"implement."
            )
        sock.sendall(bytes((chosen,)))

    if chosen == SECURITY_VNC_AUTH:
        challenge = _recv(sock, 16)
        sock.sendall(vncauth.respond(challenge, password or ""))

    # 3.8 always reports the result; older versions only after an actual attempt.
    if version == "3.8" or chosen == SECURITY_VNC_AUTH:
        result = struct.unpack(">I", _recv(sock, 4))[0]
        if result != 0:
            detail = _read_reason(sock) if version == "3.8" else "no reason given"
            raise RfbAuthError(f"the far end refused the credential: {detail}")


def _read_reason(sock: socket.socket) -> str:
    try:
        length = struct.unpack(">I", _recv(sock, 4))[0]
        return _recv(sock, length).decode("utf-8", errors="replace")
    except RfbError:  # pragma: no cover - a server that says nothing at all
        return "no reason given"


def _server_init(sock: socket.socket) -> tuple[int, int, str]:
    width, height = struct.unpack(">HH", _recv(sock, 4))
    _recv(sock, 16)  # the server's own pixel format; we replace it immediately
    length = struct.unpack(">I", _recv(sock, 4))[0]
    name = _recv(sock, length).decode("utf-8", errors="replace") if length else ""
    if width <= 0 or height <= 0:
        raise RfbError(f"the server announced a {width}x{height} framebuffer")
    return width, height, name
