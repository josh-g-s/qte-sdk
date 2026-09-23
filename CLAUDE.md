# qte-sdk

Python SDK and client library for the Queen's Tower Exchange (QTE). This is a public MIT repo: everything in it, including this file, issues, PRs and comments, is readable by the students who use the SDK.

## Internal rules

Team members and their agents also follow the private qte-sdk agent rules in the qte-platform repo (`docs/agents/qte-sdk.md`). They hold the internal detail this public file leaves out: the specification sources, the review and merge process, and assignment rules. Read them before starting work. If you cannot read them, stop and ask rather than guess.

## Authority

The exchange is defined by the QTE platform specification and its decision log. This repo consumes the wire contract at a pinned version and never defines exchange semantics. Where anything here disagrees with the platform specification, the specification wins; conflicts are escalated to the Head of Technology, never resolved silently.

## Design facts (do not build against older assumptions)

- Market data (book, trades, instrument conditions) is one conflated 100 ms grid for everyone. There is no raw stream.
- How teams get backtest access is not decided yet. Do not build a local fill simulator against downloaded recordings.
- New, cancel and amend each address one price level of one instrument, mass-cancel addresses the whole team, and there is no order ID on the wire. A new order carries a registered strategy ID, a team holds at most one resting order per instrument, side and price, and per-team message budgets apply. Build against the generated contract types, not hand-written ones.
- Teams trade through the API, directly or through this SDK, from their own machines.
- Every order message, mass-cancel included, carries a symmetric 150 ms delay. The delay, minimum rest, collar and budgets are values the exchange sets: never compile them in, and examples and docs must not pretend them away.
- Tests and examples run against a local exchange. Examples never point at production.

## Workflow: issue -> branch -> PR

- All work starts from an issue. No issue, no branch. An issue labelled `ready` is approved to be picked up.
- Branch from main, named `issue-<n>-<slug>`.
- One issue = one branch = one PR. Keep a PR reviewable in one sitting.
- Direct pushes to main are blocked; everything merges by PR.
- The PR body must contain `Closes #<n>` so the merge closes the issue. Merged branches auto-delete.
- State ordering in the PR body as a line that is exactly `Depends on #<n>`, one PR per line and nothing else on the line.
- Squash-merge is the default; write the squash title imperative.
- The author never approves their own PR. PRs written by an agent are labelled `agent-authored`.
- CI must be green and an independent review must pass before a PR merges.
- If the work hits a spec conflict or an open question, stop, comment on the issue and add the `needs-head` label. `needs-head` blocks merging.
- Put work-in-progress up as a draft PR early rather than a large PR late.

## Creating issues (humans use the Task form; agents must mirror it)

An issue created via the API must carry:

- an imperative title;
- a `workstream:<A-K>` label;
- a priority label (P1/P2/T2/T3) only when known - absence means untriaged, do not guess;
- a "What and why" that explains the need without quoting private platform material (the internal rules say how to cite internal sources);
- a testable "Done when";
- a duplicate check first (`gh issue list` search); comment on the existing issue instead of filing a twin.

## Public-repo rules

- MIT licence. No internal URLs, keys, credentials, student data, or platform internals in code, tests, docs, issues, PRs, comments, or history.
- Pushing to any branch, or writing an issue, PR or comment, publishes it. Check before pushing, not before merging.
- The only platform files published here are the vendored wire-contract `.proto` files. Never copy other platform documents or their content into this repo.
- Document headers carry `**Version:**`; no standalone Date line. No em-dashes in documents.
