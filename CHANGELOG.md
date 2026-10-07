# Changelog

**Version:** 0.13

The changes in each release of qte-sdk. A release is a `vX.Y.Z` tag on `main` whose number matches `qte_sdk.__version__`. `python -m qte_sdk.update` says whether yours is the latest, and `pip install "git+https://github.com/josh-g-s/qte-sdk@v1.0.0"` installs a given release.

Changes merged since the last release are listed under "Unreleased" at the top. A release renames that heading to its number, such as `## 1.0.1`, and starts a new empty "Unreleased" section above it. (The `**Version:**` line above is this file's own revision, not the SDK's.)

## Unreleased

- On Windows, the check of a `.env` or the file named by `QTE_TOKEN_FILE` now also reads the access list of the file's folder and the file's owner. It warns, with the same `TokenFileShared` or `AddressFileShared`, when Everyone, Authenticated Users, Users, INTERACTIVE or Domain Users may add or remove files in the folder, since they could replace a private file with one of their own, naming the folder; and when the file is owned by an account other than you, Administrators or SYSTEM. The warning suggests running `icacls` on both the folder and the file. `python -m qte_sdk.token set` and `check` report the same, and say when the folder or the owner could not be checked. It still warns rather than refuses.
- On Windows, when the `.env` or the file named by `QTE_TOKEN_FILE` is reached through symbolic links or junctions, at the file itself, at a folder on its path, or in what a link points to, the check now reads the access list and owner of the file they lead to and the list of that file's folder, and also the list of each folder that holds a link on the way, since whoever may replace any one link may point it elsewhere. The warning names each folder that broad groups may add or remove files in, and the links in it. If a link on the way cannot be followed (a loop, more than 40 links, a link that cannot be read or that points to a volume's own name, or a name on the way that cannot be looked at or is not there), the folders of the links met before it are still checked, and the same warning says the file could not be fully checked, and why, since such a link could be made to hide where it leads. A `KeyboardInterrupt`, or another interruption, raised while the SDK warns about a file that holds the token still stops the program, but no longer carries the frames or chained exceptions that held the file's text.
- On Windows, cancelling a `HistoryClient` fetch, for example with `asyncio.timeout`, now ends it at once, as the `qte_sdk.history` docs say, unless it is still looking up the service's address or opening the connection, which cannot be woken on any system. Before, a fetch cancelled while it waited on the service kept its worker thread and connection until `timeout` ran out (30 seconds by default), which could hold up the program's exit; and one cancelled while it read the data also froze the program's event loop, and so everything else the program was doing, for that long. The client now waits on the service in a way the cancel ends at once on every system. CI runs the history tests on Windows too.
- CI runs the Windows token-file and console checks against the real Windows API, on a `windows-latest` runner. No change to the SDK itself.

## 1.1.0

Update with `python -m qte_sdk.update` and follow what it prints.

- The SDK reads the exchange's contract 11.
- `qte_sdk.tickets`, for Fundamentals tickets: a pod sends its Execution desk a ticket with `send_ticket`, cancels it with `send_ticket_cancel` and raises a cure ticket's urgency with `send_ticket_urgency`. Pods and desks pick out the exchange's answers (`ticket_accepted`, `ticket_reject`, `ticket_state`) with `is_ticket_event`, and `LatestTickets` keeps each ticket's newest state, forgetting them all on a `Disconnected` so that the states resent on the next connection rebuild it. `stopped_time_of` and `stopped_mark_of` read when a ticket stopped and the mark then, and `parent_ticket_of` names the ticket a desk's fill works.
- A desk's child order cancelled because the pod cancelled its ticket, or the mark moved beyond the ticket's limit, carries the reason `PARENT_STOPPED`. At the term's final close the children are cancelled `SESSION_CLOSE`, a child still resting when the exchange cancels a ticket itself would be cancelled with a reason from the 1800 band (today none rests then), and a Directors' handover of a working ticket cancels its outstanding children `PARENT_STOPPED` while the ticket keeps working.
- The order helpers never set the contract's new `house_team` field, which only the exchange's own sessions may send.
- The reasons `TRADING_CUTOFF` (an order message received at or after the term-end trading cutoff) and `TERM_CUTOFF` (a resting order cancelled at the cutoff) are named, instead of decoding as `REASON_CODE_UNSPECIFIED`.
- The reasons for option contracts are named: a `new` or `amend` is rejected `CONTRACT_NOT_LISTED`, `CONTRACT_SUSPENDED` or `CONTRACT_REDUCING_ONLY`, and a resting order in a reducing-only contract that would grow the position or cross zero is cancelled `CONTRACT_REDUCING_RECHECK_FAILED`. The `qte_sdk.options` docstring and the quickstart say when each applies.
- The docs say that a `reject` can carry `CURE_WINDOW`, for a new still in its order delay when a cure window opens.
- `InstrumentInfo.sector_limit` is the sector the Fundamentals sector limit counts an instrument against, and `qte_sdk.instruments.sector_of` reads it, as None when the exchange gives none.
- A `TapePrint` in live `trades` carries `feed_only`, present and true only on an option contract's residual print taken at the live print's premium because the contract had no wall on that side. Test it with `HasField("feed_only")`.
- `Origin` names `HOUSE`, the origin of the exchange's own house orders. A team's connection is never sent a report with it.
- `qte_sdk.options.is_feed_only` says whether a live tape print is an option contract's feed-only residual print; a print without the flag is an ordinary one.
- The smoke test counts the ticket states a pod or desk is sent, instead of naming `ticket_state` as a type the SDK does not know.
- The conformance session (`tests/test_conformance.py`, run only against an exchange you name) follows the published steps at v1.7: step 1a checks the `instruments` message that comes right after the calendar, step 15 checks heartbeats, the 45-second silence close, resume replay and snapshots, and a resume across an exchange restart, and step 3 accepts up to ten ladder levels a side.

## 1.0.2

Update with `python -m qte_sdk.update` and follow what it prints. If you are still on 1.0.0 from a zip, upgrade once by hand: `pip install https://github.com/josh-g-s/qte-sdk/archive/refs/tags/v1.0.2.zip`.

- On Windows, the SDK reads the access list of a `.env` that holds the token, and of the file named by `QTE_TOKEN_FILE`, and gives a `TokenFileShared` warning if Everyone, Authenticated Users, Users, INTERACTIVE or Domain Users may read or change it, naming the groups and how to fix it. It gives an `AddressFileShared` warning if one of them may change a `.env` that sets only `QTE_URL`, since a changed address could capture your token. Both are kinds of `FileShared`. `python -m qte_sdk.token set` and `check` report the same. A folder on a second drive, such as `D:\`, is usually readable, and often changeable, by every local user; keep your project under your user profile. On macOS and Linux nothing changes: a `.env` that holds the token and that other users can read is still refused, and nothing else is checked there.
- A later release will refuse, on Windows, a token or `.env` file that other users can read or change, rather than warn. (macOS and Linux refuse only a `.env` holding the token that other users can read.) If you keep your project on a shared drive, move it under your user profile now.
- On Windows, `python -m qte_sdk.token` gives PowerShell and cmd commands instead of `unset` and `export`.
- On Windows, `python -m qte_sdk.token set` refuses to run without a console, as it does without a terminal elsewhere, instead of waiting for ever with its input redirected from `NUL` (in a CI job, a scheduled task or an IDE without a console, say).

## 1.0.1

**If you installed 1.0.0 from a zip, upgrade once by hand:** `pip install https://github.com/josh-g-s/qte-sdk/archive/refs/tags/v1.0.1.zip`. The update check in 1.0.0 cannot check a zip install, so it never tells you about this release; from 1.0.1 on it does. If you installed with git, run `python -m qte_sdk.update` and follow what it prints.

- The README and quickstart give the Windows steps (`py`, activating a virtual environment in PowerShell or cmd, the execution policy) and an install with no git, from a release's zip such as `pip install https://github.com/josh-g-s/qte-sdk/archive/refs/tags/v1.0.1.zip`, with the examples from the same zip.
- `python -m qte_sdk.update` checks an install from a release zip (or a zip of a branch) of this repository: current, or behind with the command that installs the latest release's zip. An install it cannot trace to the repository, such as an editable one, is now reported as behind when its version is lower than the latest release, though still never as current.
- Installing the SDK on Windows brings in the `tzdata` package, so `zoneinfo` finds time zones such as New York's there.
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
