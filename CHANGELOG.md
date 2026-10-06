# Changelog

**Version:** 0.1

The changes in each release of qte-sdk. A release is a `vX.Y.Z` tag on `main` whose number matches `qte_sdk.__version__`. `python -m qte_sdk.update` says whether yours is the latest, and `pip install "git+https://github.com/josh-g-s/qte-sdk@v1.0.0"` installs a given release.

Changes merged since the last release are listed under "Unreleased" at the top. A release renames that heading to its number, such as `## 1.0.1`, and starts a new empty "Unreleased" section above it. (The `**Version:**` line above is this file's own revision, not the SDK's.)

## Unreleased

- On Windows, the SDK reads the access list of a `.env` that holds the token, and of the file named by `QTE_TOKEN_FILE`, and gives a `TokenFileShared` warning if Everyone, Authenticated Users, Users, INTERACTIVE or Domain Users may read or change it, naming the groups and how to fix it. It also warns if one of them may change a `.env` that sets only `QTE_URL`, since a changed address could capture your token. `python -m qte_sdk.token set` and `check` report the same. A folder on a second drive, such as `D:\`, is usually readable, and often changeable, by every local user; keep your project under your user profile. On macOS and Linux nothing changes: a `.env` others can read is still refused.
- A later release will refuse, as macOS and Linux already do, a token or `.env` file that other users can read or change on Windows. If you keep your project on a shared drive, move it under your user profile now.
- On Windows, `python -m qte_sdk.token` gives PowerShell and cmd commands instead of `unset` and `export`.
- On Windows, `python -m qte_sdk.token set` refuses to run without a console, as it does without a terminal elsewhere, instead of waiting for ever with its input redirected from `NUL` (in a CI job, a scheduled task or an IDE without a console, say).
- `ReconnectingSession.calendar` no longer goes back to the previous session's calendar for a moment after a reconnect.
- The docs say that `tradable` is an entitlement only (use `can_trade`), to show `instrument` when `display_name` is absent, that `session_id` is unique only within one run of the exchange, what carries over into a new term for each arm, and that positions and cash start afresh at the start of the 7 October 2026 session, a one-off within the term.

## 1.0.0

The first numbered release. It provides:

- Sessions: `open_session` authenticates with your team token, read from the environment, a token file or a private `.env`, and `ReconnectingSession` reconnects and resumes after a dropped connection.
- The exchange's calendar and instruments table: trading days and hours, each instrument's tick and lot size, and what your team may trade.
- Market data: subscribe to books, trades, marks and the market's session state, and keep the latest book of each instrument with `LatestBooks`.
- Orders: send, cancel, amend and mass cancel, read order events, and track your team's resting orders with `RestingOrders`.
- Your team's account: query positions, cash and limit use, and count fills against replies with `AccountReports`.
- Options: build and read option symbols, the day's option chain, published Greeks and each contract's trading state.
- Past market data: `HistoryClient` reads closed sessions, and `replay` runs them through your loop.
- Exact units: prices in micro-dollars and exchange timestamps, converted without `float`.
- Tools: `python -m qte_sdk.token` sets up your token, `python -m qte_sdk.update` checks for a newer release, and `examples/smoke_test.py` checks a setup end to end.
