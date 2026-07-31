# yRDP — a general-purpose, agent-first remote-desktop client

## 1. What it is

yRDP connects to any **RDP or VNC** host and presents that remote desktop as **one session
with any number of viewers**:

- the **canonical surface** — the session at **fixed dimensions**, pinned by the geometry
  contract, driven entirely from the command line. It exists whether or not anyone is
  watching. This is the agent lane.
- a **reveal** — that same session shown in the yggterm viewport, attached and detached at
  will, by as many viewers as care to look. This is the human lane, and it is also
  co-browse.

**A session must never exist in a vacuum.** These were once modelled as exclusive modes,
which was exactly backwards: it left the surface an agent drives unwatchable, and an agent
surface nobody can look at is an agent surface nobody can trust.

Everything else in this document exists to serve one idea:

> **A surface pinned to fixed dimensions turns a GUI into an addressable one, and lore
> turns repeat traversal into roughly O(1).** You pay once, expensively, to work out where
> things are and how a flow goes; every run after that is a cheap replay.

That is the same trick a browser automation surface plays on the DOM, which is why the
verbs, the lore mechanism and the ladder are deliberate mirrors of it rather than new
inventions. An agent that can drive one must be able to drive the other without learning a
second vocabulary.

## 2. What yRDP does not know, on purpose

yRDP knows how to reach an RDP endpoint, how to pin a surface, how to drive it, and how to
recall lore. It knows **nothing** about what is on the far end — not the application, not
the operating system, not the machine underneath it, not the site it belongs to.

That ignorance is the product, not an omission. Two things follow from it:

1. **A tool that has learned one deployment's shape stops being a tool for anyone else's.**
   The moment a client contains a particular application's window names or a particular
   hypervisor's command lines, it is that installation's script wearing a tool's name.
2. **Every such fact baked in here is also a fact leaked out of somewhere private.**

So the knowledge lives in two places that are not this repo:

| knowledge | where it lives |
|---|---|
| where to connect, at what geometry, with which credential NAME | a target file, private to whoever owns the machine |
| what to do once connected — the flows, the traps, the coordinates | **lore**, private in the same way |
| how to bring a machine up, take it down, or look at it out of band | a named `[hooks]` command in the target file, run as data |

A lore store holds entries like *"for this trading terminal, connect and go here, then
there"* or *"for this game under Wine, connect to that Linux desktop and do this"*. yRDP
prints them; it never contains them.

## 3. The geometry contract (the load-bearing rule)

Coordinate automation rots **silently**: when the display size changes, the same lore that
worked yesterday clicks empty canvas today and nothing errors. So geometry is a contract
with a **refusal**:

> The agent lane negotiates ONE canonical geometry, declared per target. Every lore entry
> stamps the geometry it was proven at. **yRDP refuses to replay coordinate lore at a
> different geometry** rather than clicking approximately.

Enforced at the point of action, in three places:

- the shadow surface is created at exactly the declared size, so nothing downstream can
  renegotiate it;
- whatever would let the far end resize us is never offered, and is NAMED so a test can
  assert its absence rather than trusting that it was left off. For a spawned client that
  is a flag list (RDP's `dynamic-resolution`, `smart-sizing`) in `clients.py`; for a
  protocol we speak ourselves it is the pseudo-encodings we never advertise
  (`rfb.FORBIDDEN_ENCODINGS`: `DesktopSize`, `ExtendedDesktopSize`) — a server can only
  resize a client that said it would listen;
- a coordinate replayed from another geometry is refused, and a coordinate off the surface
  is refused too, because off-surface is a rotted coordinate rather than a near miss.

A human watching must not be able to renegotiate the agent lane's geometry by resizing a
window. The viewport surface is a **viewer**; if it needs a different size it gets a scaled
view of the canonical surface, never a resize of it. That is `attach --scaled`, and it is
the default because the agent's contract is expensive to rebuild while the human's
discomfort is recoverable by leaning in.

### The geometry epoch — because a contract that CAN change needs a way to say when

Sometimes the surface really does change size: a guest's display settings, a viewer that
adopts the surface so the human gets a pixel-exact view, an operator editing a launcher.
Re-pinning is legitimate. Re-pinning **silently** is not, and it fails in a specific and
nasty shape the user named before it happened:

> an agent screenshots at 1920x1080 · a human adopts the surface at 1600x900 · the agent
> clicks a coordinate it read off *its own screenshot*

Every existing check passes. There is no `--proven` stamp to compare, because the rule
correctly says to omit it for a coordinate you just read off this session's own screenshot.
The point is inside the new surface. The far end now agrees with the new contract. So the
click lands somewhere meaningless, the agent cannot see why, **and it keeps trying** — the
expensive rung silently stops paying back.

A warning in a document cannot prevent that, because the agent that is failing has already
read the document and still does not know that *this* surface moved. So the invalidation is
carried in the data:

- `Session.geometry_epoch` is bumped by `session.repin()`, the **one** door through which
  the contract may change. `repin` also records `resized_by` and `resized_at`, and is
  idempotent — attaching a viewer at the contract geometry changes nothing and therefore
  invalidates nothing, or co-browse would cost the agent its coordinates every time a human
  glanced at the surface.
- **Observations stamp the epoch.** `screenshot` returns `epoch` and sets
  `observed_at_epoch`, because a picture is the only thing that actually re-synchronises an
  agent with a surface somebody moved.
- **Actions check it.** `do click --from-epoch N` is refused when N is not current; and a
  click with *no* epoch is refused outright while `observed_at_epoch != geometry_epoch`.
  That unstamped case is the hole above, and closing it is the point of the whole
  mechanism.
- **`state` reports `geometry_stale` and `resized_ago`**, because the first command an
  agent runs when something stops working is `state`. If the answer to "why did my clicks
  stop landing?" is not in the first command, the agent looks in the wrong place.

A session is born *observed* (`observed_at_epoch == geometry_epoch == 0`), so replaying
lore proven at the contract geometry never requires taking a picture first — making the
pixel rung a prerequisite for the cheap rungs would be exactly backwards.

**Not built yet:** `attach --adopt` as a single verb. For yRDP the surface size is the *far
end's* size, so adopting means re-opening the session at the viewer's geometry — real for
RDP (which negotiates `/size:`), impossible for a hypervisor console that is 1280x800
because the guest says so. Until that lands, an adopt is expressed honestly as
`yrdp repin --geometry WxH --by "viewer adopt"` after the far end really is that size;
`--by` is required so the refusal it causes can name a cause.

## 4. The ladder (path of least resistance)

Each rung is roughly an order of magnitude dearer and more fragile than the one above.
**Lore records which rung actually worked**, so the next run starts at the right one.

1. **API** — whatever the application itself exposes. No pixels at all.
2. **Scripting and files on the host** — its own config, export and print-to-file paths.
   `yrdp exec` runs a command on the machine hosting the target for exactly this.
3. **A structured accessibility tree over the wire** — element names and roles are
   *semantic*, so they survive geometry changes. **This is the rung that actually delivers
   "GUI as a CLI"**, and it deserves to be built early rather than treated as an
   optimisation. The reader is a per-platform helper the site supplies as a hook, because
   what reads that tree differs by operating system.
4. **Template match or cropped OCR** on a small known rect — cheap precisely because the
   rect is small and known, which is what lore stores.
5. **Full screenshot plus a click grid** — the bootstrap path for a target with no lore.
   Every target starts here exactly once. A matured flow still needing rung 5 means the
   lore is wrong.

## 5. Architecture

```
agent (any host)                          human (any number of them)
   │ yrdp <verb> --target X                  │ yrdp view --target X
   ▼                                         ▼
        ONE canonical surface — fixed dimensions, pinned by contract
        ├── rdp: a client binary on a headless display we created   [backend: x11]
        │        capture: ImageMagick   input: xdotool
        └── vnc: the protocol spoken directly, nothing in between   [backend: rfb]
                 capture: framebuffer   input: RFB key/pointer events
                                                 reveal: shared VNC export
                                                       + websocket bridge
                                                       + OSC 7717 web-surface
```

**Two backends, one seam** (`clients.Adapter.backend`). Everything above it — the geometry
contract, session records, viewers, lore, hooks, credential resolution, the verb set — is
shared, which is why this is still one tool rather than two.

VNC took the x11 route first and it did not work: the viewer authenticated, painted nothing
headless, and wedged other X clients on the display. The reveal path had already shown the
fix, because a `yrdp view` of a VNC target bridges the endpoint straight through with no X
in the path. Applying the same move to the agent lane **deleted** the spawned-viewer path
rather than adding a mode beside it — a framebuffer protocol needs no framebuffer emulator,
and a broken second way to hold one surface is worse than none.

That difference in shape follows a difference in the far end, not a preference:

- an **RDP session is stateful on the far end** — a logon session that ends when its client
  leaves — so a client process must stay alive for as long as the session does;
- a **VNC console is a view onto a framebuffer that exists regardless**, so each verb opens
  its own short connection and closes it. No display, no client process, nothing to
  supervise and nothing to leak. `alive()` therefore asks the endpoint rather than a pid,
  because reporting pid-liveness for a backend with no pids would be a comforting lie.

- **The agent lane never needs a display of its own**, so unattended operation costs the
  desktop machine nothing.
- **The reveal adds no new substrate and needs no changes to yggterm**: it rides the
  existing web-surface channel. A native libyggterm surface — the framebuffer composited
  directly, with no browser and no VNC hop — is the better endgame and is yggterm-side work.
- **Viewers never evict each other**, and detaching one never touches the session.
- **Secrets are named, never carried.** A target stores a vault entry NAME; the secret is
  resolved at connect time and handed to the client down an inherited file descriptor, so
  it never appears in argv, the environment, or on disk.

## 6. Verbs

```
yrdp targets                       what this installation is configured for
yrdp show       --target X         the resolved target, secrets excluded
yrdp state      --target X         does the endpoint answer; is a session live
yrdp up / down  --target X         run the site's own mechanism, declared as a hook
yrdp hook       --target X <name>  any other site mechanism, same seam
yrdp exec       --target X "<cmd>" run a command on the machine hosting the target
yrdp open       --target X [--vnc] connect, pin the geometry, PRINT THE LORE
yrdp view       --target X         reveal that session in the yggterm viewport
yrdp list / close
yrdp screenshot --target X [--rect x,y,w,h]        crop first, per the ladder
yrdp do click   --target X --at 840,412 --proven 1920x1080@1.0
yrdp do type "text" | do key ctrl+shift+o
yrdp lore       --target X
```

Data verbs print JSON on stdout and a sentence on stderr, so one invocation serves both a
script and a person reading the transcript. Every refusal names its own recovery.

## 7. The substrate seam

An RDP endpoint sits on *something*: a virtual machine, a container, a physical desk, a
cloud instance. Starting it, stopping it, or looking at it when it will not talk are real
operations — and every one of them is site-specific. Encoding any of them in a client
would make the client useless to the next site.

So the seam is data. A target declares named `[hooks]` (argv lists) and yRDP runs them
without understanding them. `up` and `down` are conventions, not requirements; a site may
declare `console`, `snapshot`, `tree`, anything, and reach it with `yrdp hook`.

The only two substrate facts yRDP claims for itself are genuinely generic: whether a TCP
endpoint answers, and how to run a command on the machine hosting the target. It does not
guess *why* an endpoint is silent — on one substrate that means the machine is off, on
another a firewall, on a third a tunnel. Naming a cause it cannot see would be inventing a
diagnosis.

## 8. Lore

- **Source of truth: one Markdown file per target**, committed to a git repo so a fleet
  shares it with a plain `git pull`.
- **Retrieval: a derived index**, rebuildable and gitignored, never the source of truth —
  files sync newest-wins, and a committed binary index cannot be diffed or reviewed.
- **Entry header** carries `geometry:` (required for coordinate lore, `n/a` when the method
  uses none) and `rung:` (which ladder rung worked).
- **Recall at launch is not optional.** `yrdp open` prints that target's lore to stderr the
  moment the session opens. *A skill an agent must remember to load is a skill an agent
  forgets.*
- `YRDP_LORE_DIR` points at the store. There is no default: yRDP ships no lore and guesses
  no paths.

## 8b. One tool, two protocols

`protocol = "rdp" | "vnc"` in the target, `--vnc` on the command line. There is no second
codebase, because everything that matters — the geometry contract, sessions, viewers, lore,
hooks, credentials, the verb set — is protocol-independent. Two copies of one idea drift,
always in the direction that costs a debugging session.

`clients.py` is the single protocol-shaped seam, and a protocol owes four things in
whichever form its backend expresses them: pin the surface to the contract geometry;
**name** whatever would let the far end resize us so it can be locked out by test (RDP's
`dynamic-resolution` flag; the `DesktopSize` pseudo-encoding we never advertise); deliver
the secret by a channel `ps` cannot read; and classify failures into the **same named
outcomes**, because the caller's recovery depends on the outcome and must never depend on
the protocol. The x11 backend earns those outcomes by matching a client's stderr; the rfb
backend raises them as `RfbAuthError` / `RfbError` — the same contract with fewer spellings
to get wrong.

`rfb.py` is deliberately small: Raw encoding only, one pixel format, no compression, no
cursor pseudo-encodings. A screenshot of a pinned surface and a handful of input events
need no more, and an encoding that is never implemented is one that cannot arrive
malformed. VNC's original password security type needs DES, which is why `vncauth.py`
exists and why nothing else may import it.

## 9. Status

**Proven on real hardware, by running it rather than by reading it.**

- **RDP, x11 backend** — reachability, a client connecting at a pinned geometry, a real
  authentication verdict in about two seconds, a captured desktop at exactly the contract
  size, a coordinate stamped at another geometry refused against a live session, and
  nothing left behind when an open fails. Plus the substrate seam, `exec`, and lore recall.
- **The reveal** — one session, several viewers, none evicting another; the session still
  alive and painting after every viewer detached. For a VNC target the bridge points
  straight at the endpoint, with no export in the middle.
- **VNC, rfb backend** — the handshake (3.8, and 3.3/3.7 by version fallback), security type
  None and the DES password type, a framebuffer decoded and written as a PNG, and key and
  pointer events landing in the far end. Proven twice over: against a local x11vnc that
  really did refuse a wrong password and really did accept the right one, and against a
  hypervisor console where the picture read back matched what was painted and arrow keys
  visibly moved the selection.

**Not built:**

- **A native libyggterm surface** for the reveal — the framebuffer composited directly, no
  browser and no VNC hop. Better endgame, yggterm-side work.
- **A session lease**, so a long agent flow is not reaped mid-way.

**Answered, 2026-07-31 — two items left this list without any code being written:**

- **The accessibility rung (3)** exists for Windows, and it is not in this repo. It is a
  UIAutomation helper the target runs as a site `[hooks]` entry, because "how do I get a
  semantic tree out of THAT machine" is site knowledge and yRDP's identity is its
  generality. Proven against Excel (rich tree: named buttons, `msotcidPlaceOpen`,
  invoke/gettext/focus all working). Two of the four consumers publish **no** tree at all —
  TWS is Java Swing, PL9 is Qt5 — which is the ladder working rather than a gap: those
  two have better rungs (the IB API under IBC; PL9's own export paths).
- **Apple's own VNC security type** turned out never to be needed. A macOS Screen Sharing
  endpoint announces `RFB 003.889` and offers types `30, 33, 35, 36, 2` — Apple's four
  **and** ordinary VNC password auth, which this client already speaks. Connecting to a
  live macOS 26.4.1 guest works today: version clamped 889 → 3.8, type 2 chosen,
  1280x800 framebuffer, desktop name `iMac`. Both halves are locked by test with the
  mutations proven red. (That endpoint then paints an all-black framebuffer on that
  particular guest, for reasons that belong to the guest and not to this client — it has
  no GPU kext. The hypervisor console is the surface there.)
