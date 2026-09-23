# qte-sdk

Python SDK and client library for the Queen's Tower Exchange (QTE). Public MIT repo, deliberately separate from the private platform monorepo: everything here ships to ~230 students.

## Authority order

The exchange is specified in qte-platform `SPEC.md`, whose Section 0 sets which source wins, and decisions are recorded in qte-platform `docs/QTE_Decision_Log.md`. Cite those two, never a draft number. The wire contract is the set of `.proto` files in qte-platform `schemas/proto/qte/contract/v1`, which this repo vendors at a pinned qte-platform commit (being set up under #5). The contract is not frozen yet (change control is provisional, H5), so the pin is bumped deliberately. This repo consumes the contract; it never defines exchange semantics. Where anything here disagrees with the platform contract, the contract wins; conflicts escalate to the Head of Technology, never resolved silently.

## Design facts that changed recently (do not build against the old ones)

- Market data (book, trades, instrument conditions) is ONE conflated 100 ms grid for everyone. There is no raw stream. The mark publishing on a 1 s grid is a Technology Arm assumption (`SPEC.md` 4.3).
- Recorded sessions serve backtesting (`SPEC.md` 4.2, 4.4), but how teams get backtest access is open. Until it is decided, do not build a local fill simulator against downloaded recordings.
- New, cancel and amend each address one price level of one instrument, mass-cancel addresses the whole team, and there is no order ID on the wire. A new order carries a registered strategy ID, a team holds at most one resting order per instrument, side and price (DL-22), and per-team message budgets apply. Build against the generated types, not hand-written ones.
- In Term 1, teams trade through the API, directly or through this SDK, from their own machines; there is no hosted strategy runtime yet (DL-05).
- Every student order message, mass-cancel included, carries a symmetric 150 ms delay (`SPEC.md` 8.1). The delay, minimum rest, collar and budgets are values the exchange sets: never compile them in, and examples and docs must not pretend them away.
- The test target is the qte-skeleton walking-skeleton exchange from qte-platform, run locally. Examples never point at production.

## Workflow: issue -> branch -> PR

- All work starts from an issue. No issue, no branch. An issue labelled `ready` is approved to be picked up.
- An assigned issue is claimed (qte-platform DL-11). Agents build only unassigned issues and never pick up, push to or re-scope an assigned one. A developer who wants an agent to take over unassigns themselves.
- Branch from main, named `issue-<n>-<slug>`.
- One issue = one branch = one PR. Keep a PR reviewable in one sitting.
- Direct pushes to main are blocked; everything merges by PR.
- The PR body must contain `Closes #<n>` so the merge closes the issue. Merged branches auto-delete.
- Squash-merge is the default; write the squash title imperative.
- The author never approves their own PR. Agent-authored PRs are labelled `agent-authored`.
- **Until the Head revokes it, every PR is approved and merged automatically.** The Head's decision of 23 September 2026 extends qte-platform DL-07 to this repo, with a review date of 14 October 2026; it is recorded in the qte-platform Decision Log through qte-platform #477. The QTEReviewBot approver account approves and squash-merges a PR once automated gates pass on its latest commit: every required and reported check green; an independent Codex review of that commit with no blocking findings; every review conversation resolved; no `needs-head` label on the PR or its issue; `Closes #<n>` present; and every PR it depends on already merged. Human review, including Jidneya's, is welcome but not required.
- State ordering in the PR body as a dependency declaration: a line that, after trimming surrounding whitespace, is exactly `Depends on #<n>`, one PR per line and nothing else on the line. Nothing else in the body counts.
- If the work hits a spec conflict or an OPEN point, stop, comment on the issue, add the `needs-head` label and escalate to the Head of Technology. `needs-head` also blocks automatic merge.
- CI must be green before review is requested.
- Put work-in-progress up as a draft PR early rather than a large PR late.

## Creating issues (humans use the Task form; agents must mirror it)

An issue created via the API must carry:

- an imperative title;
- a `workstream:<A-K>` label (the map is carried over unchanged under qte-platform DL-02);
- a priority label (P1/P2/T2/T3) only when known - absence means untriaged, do not guess;
- a "What and why" citing the qte-platform `SPEC.md` section or Decision Log entry it derives from;
- a testable "Done when";
- a duplicate check first (`gh issue list` search); comment on the existing issue instead of filing a twin.

## Public-repo rules

- MIT licence. No internal URLs, keys, credentials, student data, or platform internals in code, tests, docs, or history.
- Pushing to any branch, or writing an issue, PR or comment, publishes it. Check before pushing, not before merging.
- The only platform files that may be published here are the Head-approved allowlist: the six v1 `.proto` files (decision recorded on #5). The contract prose in qte-platform `schemas/contract/` stays private; cite its paths, never copy it.
- Document headers carry `**Version:**`; no standalone Date line. No em-dashes in documents.
