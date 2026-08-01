# Contributing

Contributions are welcome, with one condition stated up front rather than buried:
**a signed CLA is required before anything is merged.** See `CLA.md`.

## Why there is a CLA

This project is licensed GPL-3.0-or-later to the public, and the maintainer,
Avikalpa Kundu, also intends to license it commercially under different terms.
Both halves only work while one person holds the right to license the whole
work. The moment a contribution lands whose copyright sits with someone who
never granted that right, the project can no longer be relicensed as a whole —
not for a paid edition, not for a store submission, not even to move to a
different open licence later. There is no way to undo that after the fact, which
is why the CLA comes first rather than when it is eventually needed.

You keep your copyright. You are granting a licence, not signing your work away,
and everything you contribute stays public under GPL-3.0-or-later regardless.
`CLA.md` says exactly what is granted, in about a page, without legalese.

## How to sign

In the same pull request as your first contribution:

1. Add one line to the end of `CLA-signatures.md`:

       Full Name <you@example.com> — YYYY-MM-DD — @your-github-username

2. Sign off every commit with `git commit -s`, which adds a `Signed-off-by:`
   trailer certifying the contribution's origin.

That is the whole process, and it is once per contributor, not once per pull
request. Later contributions need only the `Signed-off-by:` trailer.

## Sending a change

1. Open an issue describing the proposed change before writing much of it, so
   the design can be settled while it is still cheap to move.
2. Keep pull requests focused and reviewable. One concern per branch.
3. Include tests, or written validation notes, for any behavioural change.
4. Confirm no private infrastructure details are introduced — hostnames, IP
   addresses, internal paths, credentials, or personal data belong nowhere in
   this repository, including in commit messages and test fixtures.
5. Explain *why* in the commit message, not only *what*. The diff already says
   what changed.

## Licensing of what you send

Code you contribute is licensed GPL-3.0-or-later. Documentation you contribute
is licensed CC-BY-SA-4.0. Names and logos are not licensed by either — see
`TRADEMARKS.md` where present. By opening a pull request you confirm you have
signed the CLA and that the terms above apply to your contribution.
