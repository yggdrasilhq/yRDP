# yRDP

**A general-purpose, agent-first RDP client.** It connects to any RDP host and presents
that desktop as two surfaces: a **viewport surface** in the yggterm viewport for a person
to use, and a **shadow surface** at fixed dimensions for an agent to drive from the command
line.

The idea it is built around: *a surface pinned to fixed dimensions turns a GUI into an
addressable one, and lore turns repeat traversal into roughly O(1).* You pay once,
expensively, to work out where things are; every run after that is a cheap replay.

**yRDP knows nothing about what is on the far end** — not the application, not the
operating system, not the machine underneath it. That is the point. Where to connect lives
in a target file, what to do once connected lives in lore, and how to start or stop the
machine is a hook the target declares. All three are private to whoever owns them; none of
them belongs in a client.

## Using it

No install step: it is a git checkout that `git pull` updates, like the lore it reads.
Point it at your own target directory and lore store.

```sh
export YRDP_TARGETS_DIR=/path/to/your/targets
export YRDP_LORE_DIR=/path/to/your/lore-store

bin/yrdp targets                       # what this installation is configured for
bin/yrdp show   --target X             # the resolved target, secrets excluded
bin/yrdp state  --target X             # does the endpoint answer; is a session live
bin/yrdp up     --target X             # run the site's own 'up' hook, wait for it
bin/yrdp hook   --target X <name>      # any other site mechanism, same seam
bin/yrdp exec   --target X "<command>" # run something on the machine hosting the target

bin/yrdp open   --target X             # connect, pin the geometry, PRINT THE LORE
bin/yrdp screenshot --target X --rect 100,100,400,200    # crop first, per the ladder
bin/yrdp do click --target X --at 840,412 --proven 1920x1080@1.0
bin/yrdp do type "hello" ; bin/yrdp do key ctrl+shift+o
bin/yrdp close  --target X
```

Data verbs print JSON on stdout and a sentence on stderr, so one invocation serves both a
script and a person reading the transcript. Every refusal names its own recovery.

## A target file

```toml
[target]
name = "example"
description = "whatever this is"

[geometry]              # THE CONTRACT. No default: a defaulted geometry is the silent
width = 1920            # rot the contract exists to stop.
height = 1080
scale = 1.0

[surface]
mode = "shadow"         # shadow (agent lane, built) | viewport (human lane, designed)

[connection]
host = "host.example"
port = 3389
user = "someone"
password_vault_entry = "the NAME of a vault entry — never the secret itself"

[host]                  # optional: an argv prefix that gets a shell on the hosting machine
shell = ["ssh", "-o", "BatchMode=yes", "someone@host.example"]

[hooks]                 # optional: site mechanisms, as data. yRDP runs them without
up   = ["..."]          # understanding them. 'up' and 'down' are conventions, not
down = ["..."]          # requirements — declare whatever your site actually has.
```

## Rules worth not re-litigating

- **Geometry is a contract with a refusal.** Coordinate lore is valid only at the geometry
  it was proven at, and yRDP refuses to replay it elsewhere rather than clicking
  approximately. `+dynamic-resolution` and `/smart-sizing` are locked out by test.
- **Secrets are named, never carried.** The password is resolved from a vault at connect
  time and reaches the client down an inherited file descriptor — never argv, never the
  environment, never disk.
- **Take the cheapest rung that works**: API, then host scripting, then the accessibility
  tree, then a cropped OCR, then a full screenshot. Lore records which rung worked.
- **Recall is not optional.** Opening a session prints that target's lore, because a skill
  an agent must remember to load is a skill an agent forgets.

Requires `xfreerdp3`, `Xvfb`, `xdotool` and ImageMagick on the host that runs it.
Read `docs/architecture.md` for the design and the reasoning behind each rule.
