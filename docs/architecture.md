# yRDP — a general-purpose, agent-first RDP client

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
- `+dynamic-resolution` and `/smart-sizing` are never passed to the client — either would
  hand the far end power to resize the surface under our coordinates. They are named in
  `FORBIDDEN_CLIENT_FLAGS` and locked out by test, not merely left off;
- a coordinate replayed from another geometry is refused, and a coordinate off the surface
  is refused too, because off-surface is a rotted coordinate rather than a near miss.

A human watching must not be able to renegotiate the agent lane's geometry by resizing a
window. The viewport surface is a **viewer**; if it needs a different size it gets a scaled
view of the canonical surface, never a resize of it.

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
agent (any host)                         human
   │ yrdp <verb> --target X                 │ yggterm viewport
   ▼                                        ▼
shadow surface                          viewport surface  (libyggterm)
   fixed dimensions, no window             composited like a web surface
   └── RDP client on a headless display    └── the same session, as a viewer
          capture: screenshot / crop
          input:   click / type / key
```

- **The agent lane never needs a display of its own**, so unattended operation costs the
  desktop machine nothing.
- **The viewport lane adds no new substrate**: it is a libyggterm surface alongside the
  existing ones, not a second window system.
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
yrdp open       --target X         connect, pin the geometry, PRINT THE LORE
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

`clients.py` is the single protocol-shaped seam, and an adapter owes four things: pin the
surface to the contract geometry; **name** the flags that would let the far end resize us so
they can be locked out by test (TigerVNC's `RemoteResize=1` is the exact analogue of RDP's
`dynamic-resolution`); deliver the secret off argv; and classify failures into the **same
named outcomes**, because the caller's recovery depends on the outcome and must never
depend on the protocol.

## 9. Status

**Built and proven on real hardware:** the shadow surface end to end — reachability, the
client connecting at a pinned geometry, a real authentication verdict in about two seconds,
a captured desktop at exactly the contract size, and nothing leaked when an open fails.
Plus the substrate seam, `exec`, and lore recall on open.

**Designed, not built:** the **viewport surface**. A target that asks for it is refused with
a message saying so, because silently handing back a headless shadow instead would be a lie
about which product you are using.

**Next:** the viewport surface as a first-class libyggterm surface; a click-grid bootstrap
for rung 5; and a session lease so long agent flows are not reaped mid-way.
