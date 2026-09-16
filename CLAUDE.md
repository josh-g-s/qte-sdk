# qte-sdk

Python SDK and client library for the Queen's Tower Exchange (QTE). Public MIT repo, deliberately separate from the private platform monorepo: everything here ships to ~230 students.

## Authority order

The exchange is specified in qte-platform `SPEC.md`, the build specification derived from the directors' Draft 6.3, and the wire contract is defined in qte-platform `schemas/`. This repo consumes the frozen API; it never defines exchange semantics. Where anything here disagrees with the platform contract, the contract wins; conflicts escalate to the Head of Technology, never resolved silently.

## Design facts that changed recently (do not build against the old ones)

- Market data is ONE conflated 100 ms feed for everyone. There is no raw stream.
- Draft 6.3 says recorded sessions serve backtesting and that counterparty identity never leaves the tape, but how teams get backtest access is not yet decided. Until it is, do not build a local fill-simulator against downloaded recordings.
- Each order message addresses one price level of one instrument, every order carries a registered strategy ID, and per-team message budgets apply. The exact message shapes wait on open director questions; build against the contract's generated types, not hand-written ones.
- In Term 1, teams trade only through the API and this SDK; there is no hosted strategy runtime yet.
- Every student order message carries a symmetric 150 ms delay; examples and docs must not pretend otherwise.
- The mock server (in qte-platform) is the test target for everything here. Examples never point at production.

## Workflow: issue -> branch -> PR

- All work starts from an issue. No issue, no branch.
- Branch from main, named `issue-<n>-<slug>`.
- One issue = one branch = one PR. Keep a PR reviewable in one sitting.
- Direct pushes to main are blocked; everything merges by PR.
- The PR body must contain `Closes #<n>` so the merge closes the issue. Merged branches auto-delete.
- Squash-merge is the default; write the squash title imperative.
- The author never approves their own PR. Agent-authored PRs are reviewed by a human, or by a different person's agent with that human's sign-off.
- CI must be green before review is requested.
- Put work-in-progress up as a draft PR early rather than a large PR late.

## Creating issues (humans use the Task form; agents must mirror it)

An issue created via the API must carry:

- an imperative title;
- a `workstream:<A-K>` label (the map is carried over unchanged under qte-platform DL-02);
- a priority label (P1/P2/T2/T3) only when known - absence means untriaged, do not guess;
- a "What and why" citing the qte-platform `SPEC.md` section it derives from;
- a testable "Done when";
- a duplicate check first (`gh issue list` search); comment on the existing issue instead of filing a twin.

## Public-repo rules

- MIT licence. No internal URLs, keys, credentials, student data, or platform internals in code, tests, docs, or history.
- Document headers carry `**Version:**`; no standalone Date line. No em-dashes in documents.
