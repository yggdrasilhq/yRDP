#!/usr/bin/env node
/**
 * yrdp launcher — the ynpm `git` package kind.
 *
 * yRDP is deliberately a git-checkout tool ("no install step on purpose: the
 * tool is a git checkout that `git pull` updates, exactly like the lore it
 * reads" — the repo's own bin/yrdp). This launcher preserves that design
 * while making it ynpm-installable: the checkout lives at
 * ~/.local/share/ynpm/yRDP, is cloned on first use and `git pull`ed when the
 * user asks for freshness, and every argument is forwarded verbatim.
 *
 * The repo is PRIVATE (yggdrasilhq/yRDP): the clone/pull uses whatever auth
 * the invoking user already has for GitHub (gh credential helper, ssh keys).
 */
const fs = require("fs");
const { execFileSync, spawnSync } = require("child_process");
const path = require("path");
const os = require("os");

const REPO = "https://github.com/yggdrasilhq/yRDP.git";
const CHECKOUT = path.join(os.homedir(), ".local", "share", "ynpm", "yRDP");

function run(cmd, args, opts) {
  return spawnSync(cmd, args, { stdio: "inherit", ...(opts || {}) });
}

if (!fsExists(CHECKOUT)) {
  fsMkdir(path.dirname(CHECKOUT));
  const result = run("git", ["clone", REPO, CHECKOUT]);
  if (result.status !== 0) {
    console.error(
      "yrdp: cloning yggdrasilhq/yRDP failed — the repo is private, so you " +
        "need GitHub auth for it (gh auth login, or an ssh key). " +
        "The checkout was expected at " + CHECKOUT
    );
    process.exit(result.status ?? 1);
  }
} else if (process.argv.includes("--update")) {
  run("git", ["-C", CHECKOUT, "pull", "--ff-only"]);
}

const entry = path.join(CHECKOUT, "bin", "yrdp");
try {
  execFileSync(entry, process.argv.slice(2), { stdio: "inherit" });
} catch (error) {
  process.exit(error.status ?? 1);
}

function fsExists(p) {
  try { fs.accessSync(p); return true; } catch { return false; }
}
function fsMkdir(p) {
  fs.mkdirSync(p, { recursive: true });
}
