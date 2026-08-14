# qte-sdk

Python SDK and client library for the Queen's Tower Exchange (QTE). Public MIT repo (DL-10): everything here ships to ~230 students.

## Authority order

The exchange contract is defined in qte-platform (`docs/Draft_5.pdf`, Section 2 and Table 28 row 13). This repo consumes the frozen API; it never defines exchange semantics. Where anything here disagrees with the platform contract, the contract wins; conflicts escalate to the Head of Technology, never resolved silently.

## Design facts that changed recently (do not build against the old ones)

- Market data is ONE conflated 100 ms feed for everyone. There is no raw stream.
- Backtest is hosted replay behind a redaction filter. There is NO recordings download; do not build a local fill-simulator against downloaded recordings.
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
- a `workstream:<A-K>` label (the map is in qte-platform `docs/QTE_Delivery_Plan.md` Section 3);
- a priority label (P1/P2/T2/T3) only when known - absence means untriaged, do not guess;
- a "What and why" citing the Draft 5 section or table it derives from;
- a testable "Done when";
- a duplicate check first (`gh issue list` search); comment on the existing issue instead of filing a twin.

## Public-repo rules

- MIT licence. No internal URLs, keys, credentials, student data, or platform internals in code, tests, docs, or history.
- Document headers carry `**Version:**`; no standalone Date line. No em-dashes in documents.
