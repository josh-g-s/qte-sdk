# Warning and error codes

**Version:** 0.4

Every warning and error the SDK raises or logs at WARNING has a code, such as `QTE-TOKEN-MISSING`: about the token, the exchange address, the `.env`, the session, the connection, the history service, replays and updates. This page lists each code with its cause and fix, and the exit codes of the SDK's commands. Errors raised for a wrong argument, such as a `TypeError` for a string where a list belongs, and network errors such as `TimeoutError`, have no code: their message and the line of your code in the traceback say what to change.

## How to read a message

A message has one shape, in one line:

    <CODE>: <what happened>. <why it matters>. <next step>.

For example:

    QTE-TOKEN-MISSING: no token was found in token=, QTE_TOKEN, QTE_TOKEN_FILE or ./.env. The SDK cannot authenticate without your team token. Run python -m qte_sdk.token set in your project folder, or set QTE_TOKEN_FILE to a file that holds it.

The last line of a traceback is the class name and then this message. The code is also in the `code` attribute of every SDK exception and warning, and in the `code` attribute of a log record logged at WARNING. Look a code up below by searching this page for it.

- Catch `qte_sdk.errors.QteError` for any SDK exception that has a code, or filter on `qte_sdk.errors.QteWarning` for any such warning. Each exception keeps the base class it had before codes were added (`MissingToken` is still a `ValueError`, `LivenessTimeout` a `TimeoutError`), so an existing `except` clause still catches it.
- To filter warnings by text, match the code, which is at the start: `warnings.filterwarnings("ignore", message="QTE-DOTENV")`. Filtering by class, such as `DotenvNotIgnored`, works as before.
- No message ever holds your token. Text from the exchange or the history service, such as a rejection's detail, is flattened to one line in the message and kept as sent in the exception's attribute (`detail`, or `message` for a history error), and is withheld if it repeats any part of the token. Text you gave, such as an instrument, a message type, a replay's date or channel, or the path of a `.env` or token file, is withheld from the message if it holds the token, as written, percent-encoded or escaped. Where the SDK has no token to look for, as in the git warning before the `.env` is read or a `Session`'s `repr`, text holding a run of 32 hex digits, the shape of the exchange's tokens, is withheld instead.
- A code never changes once released. A code no longer used is listed under [Retired codes](#retired-codes) and never reused.

## Exit codes

| Command | 0 | 1 | 2 | Other |
|---|---|---|---|---|
| `python -m qte_sdk.token check` | The token and the address are found, the address starts with `ws://` or `wss://`, and nothing needs fixing | Something must be fixed: no token or address (`QTE-TOKEN-*`, `QTE-ADDRESS-*`, `QTE-DOTENV-UNREADABLE`, `QTE-DOTENV-INVALID`), a `.env` git tracks or does not ignore (`QTE-DOTENV-TRACKED`, `QTE-DOTENV-NOT-IGNORED`), or on Windows a file others may read, change or replace (`QTE-TOKEN-SHARED`, `QTE-ADDRESS-SHARED`) | It could not tell: git could not say whether it ignores the `.env` (`QTE-DOTENV-GIT-UNKNOWN`), or Windows would not let it fully check a file (`QTE-TOKEN-UNCHECKED`, `QTE-ADDRESS-UNCHECKED`). Also a usage error | |
| `python -m qte_sdk.token set` | Saved | Refused, with `error: <CODE>: ...` on stderr; nothing was changed | Usage error | 130 when stopped with Ctrl+C or end of input |
| `python -m qte_sdk.update` | The SDK is the latest release | A newer release is out (`QTE-UPDATE-AVAILABLE`) | It cannot tell whether the SDK is current. Also a usage error or a bad `--timeout` | |
| `python examples/smoke_test.py` | No check failed | A check failed, or the SDK is too old to check itself | No token or no usable address, so it could not connect. Also a usage error | 130 on Ctrl+C; 128 plus the signal's number on SIGTERM or SIGHUP |

`token check` exiting 1 is a report, not a refusal: a session still only warns about a tracked `.env` or, on Windows, a shared file, and still uses it. Exit 2 with a `usage:` line on stderr is a mistake on the command line, for every command here. `token check` looks afresh each time it runs, so `token.main(["check"])` gives the same answer when a program calls it twice, or after opening a session; it never prints a path that holds the token. Before this release `token check` exited 0 even when it printed a warning.

## TOKEN

### QTE-TOKEN-MISSING

- Raised as: `qte_sdk.session.MissingToken` (from `open_session`, `resolve_token`, `ReconnectingSession`, `HistoryClient`); `token check` exits 1; `token set` refuses an empty token.
- Cause: No token was passed, `QTE_TOKEN` and `QTE_TOKEN_FILE` are unset or empty, and `./.env` in the working directory sets no `QTE_TOKEN`.
- Fix: Run `python -m qte_sdk.token set` in the folder you run your program from, or set `QTE_TOKEN_FILE` to the path of a file that holds the token. Then run `python -m qte_sdk.token check`.

### QTE-TOKEN-FILE-UNREADABLE

- Raised as: `MissingToken`; `token check` exits 1.
- Cause: `QTE_TOKEN_FILE` names a file that cannot be read, is not UTF-8 text, or is empty. While `QTE_TOKEN_FILE` is set, the SDK does not look in `./.env`. The message never shows the path, since a mistaken setting can make it the token itself.
- Fix: Fix the file, or unset `QTE_TOKEN_FILE` to use `./.env`.

### QTE-TOKEN-SHARED

- Raised as: on macOS and Linux, `MissingToken` (or `MissingURL`, since the whole file is refused) when `./.env` holds `QTE_TOKEN` and other users can read it. On Windows, the warning `qte_sdk.dotenv.TokenFileShared` when a broad group (Everyone, Authenticated Users, Users, INTERACTIVE or Domain Users) may read, change or replace the `.env` or token file, or another account owns it or a folder it is in or reached through. `token check` exits 1; `token set` prints it as `Warning:` after saving.
- Cause: Anyone who can read the file can trade as your team; anyone who can change or replace it can swap the token.
- Fix: On macOS and Linux, `chmod 600 .env`. On Windows, keep the file in a folder under your user profile (`%USERPROFILE%`), which is private by default, and run the `icacls` commands the message gives to see who can open it. On Windows the SDK warns and still uses the file; a later release will refuse such a file.

### QTE-TOKEN-UNCHECKED

- Raised as: on Windows, `TokenFileShared` when the only finding is that the check could not see everything (a link on the way could not be followed, or Windows would not show an access list or showed one in a form it cannot read); and a `token check` line when Windows would not let it read the file's access list at all. `token check` exits 2.
- Cause: The check could not tell who may open the file that holds the token.
- Fix: Keep the file itself, not a link to it, in a folder under your user profile (`%USERPROFILE%`), and run `icacls` on the file and its folder to see who can open them.

### QTE-TOKEN-MALFORMED

- Raised as: `token set` refuses the token; `qte_sdk.history.TokenMalformed`, a `ValueError`, from `HistoryClient` for a token an HTTP header cannot carry.
- Cause: The token typed or pasted has a space, quote or control character, which no token has, or (for `HistoryClient`) a newline, a control character or a letter outside ASCII. It was probably copied wrongly. The message never shows the token.
- Fix: Copy the token again and run `python -m qte_sdk.token set`.

### QTE-TOKEN-NO-TERMINAL

- Raised as: `token set` refuses to run.
- Cause: `set` was run with its input redirected or piped, or it could not turn off echo, so the token could have been shown or recorded. On Windows, input from `NUL` counts as no terminal.
- Fix: Run `python -m qte_sdk.token set` yourself, in an ordinary terminal window. A coding agent should ask you to run it.

### QTE-TOKEN-SET-PATH

- Raised as: `token set` refuses the destination; nothing is changed.
- Cause: The `.env` is a symbolic link, not a regular file, not UTF-8 text, or would grow past the 64 KiB the SDK reads; or the `--file` path is a folder, a symbolic link or a `.gitignore`.
- Fix: Edit the file by hand, or choose another path with `--file PATH`.

### QTE-TOKEN-SET-WRITE

- Raised as: `token set` refuses; the message names the system's reason, such as `Permission denied`.
- Cause: `set` could not read the existing `.env`, create the token file's folder, update `.gitignore`, or write the file. No partial copy of the token is left behind.
- Fix: Check the folder's permissions and free space, then run the command again.

## ADDRESS

### QTE-ADDRESS-MISSING

- Raised as: `qte_sdk.session.MissingURL` (from `open_session`, `resolve_url`, `ReconnectingSession`); `token check` exits 1; `token set` refuses an empty address.
- Cause: No address was passed, `QTE_URL` is unset or empty, and `./.env` sets no `QTE_URL`.
- Fix: Run `python -m qte_sdk.token set` and enter the address the course team gave you, or set `QTE_URL`.

### QTE-ADDRESS-INVALID

- Raised as: a `token check` line (exit 1); `token set` refuses the address; the smoke test's `address` check fails.
- Cause: The address does not start with `ws://` or `wss://`, has spaces or quotes, or (in the smoke test) is a plain `ws://` address for another computer, which would send your token unencrypted.
- Fix: Set `QTE_URL` to the `wss://` address you were given, for example with `python -m qte_sdk.token set`. A test exchange on your own machine may use `ws://127.0.0.1:8080/ws`.

### QTE-ADDRESS-SHARED

- Raised as: on Windows, the warning `qte_sdk.dotenv.AddressFileShared`, for a `.env` that sets `QTE_URL` but holds no token, when a broad group may change or replace it or another account owns it or a folder it is in. `token check` exits 1.
- Cause: Whoever can change the address can point it at a server of their own and capture your token, kept elsewhere, when you next connect.
- Fix: Keep the `.env` in a folder under your user profile (`%USERPROFILE%`) and run the `icacls` commands the message gives. The SDK warns and still uses the file.

### QTE-ADDRESS-UNCHECKED

- Raised as: on Windows, `AddressFileShared` when the only finding is that the check could not see everything. `token check` exits 2.
- Cause: The check could not tell who may change the `.env` that sets `QTE_URL`.
- Fix: Keep the file itself, not a link to it, in a folder under your user profile, and run `icacls` on it.

## DOTENV

### QTE-DOTENV-TRACKED

- Raised as: the warning `qte_sdk.dotenv.DotenvNotIgnored` (logged at WARNING instead if a warnings filter makes it an error); `token check` exits 1; `token set` refuses before asking for the token.
- Cause: git tracks the `.env` (or the token file), so the token in it would be committed. Adding it to `.gitignore` does not stop git committing a file it already tracks.
- Fix: Run `git rm --cached .env` in the folder the message names, add `.env` to `.gitignore`, and commit. If you did not create the file (in a repository you cloned, say), also check the exchange address in it before you use it.

### QTE-DOTENV-NOT-IGNORED

- Raised as: `DotenvNotIgnored`; `token check` exits 1. `token set` offers to fix it instead.
- Cause: The `.env` is in a git working tree and git does not ignore it, so it could be committed with your token.
- Fix: Add `.env` to `.gitignore`. If you did not create the file, check the exchange address in it before you use it.

### QTE-DOTENV-GIT-UNKNOWN

- Raised as: a `token check` line (exit 2); `token set` refuses rather than guess.
- Cause: The `.env` is inside a git repository, but git could not say whether it tracks or ignores it, usually because git is not installed or not on your PATH.
- Fix: Install git and run the command again, or make sure `.env` is in `.gitignore` and not tracked. Sessions say nothing about this.

### QTE-DOTENV-UNREADABLE

- Raised as: `MissingToken` or `MissingURL`, whichever was being read; `token check` exits 1.
- Cause: `./.env` cannot be read, is not a regular file, is larger than 64 KiB, or is not UTF-8 text. Nothing in it is used.
- Fix: Make it a regular UTF-8 text file under 64 KiB that you can read, or recreate it with `python -m qte_sdk.token set`.

### QTE-DOTENV-INVALID

- Raised as: `MissingToken` or `MissingURL`; `token check` exits 1. The message names the line by number only, never its text.
- Cause: The `QTE_TOKEN` or `QTE_URL` line has an opening quote with no closing quote, text after its closing quote, or whitespace or a quote in a value without quotes.
- Fix: Write it as `NAME=value` on one line, unquoted with no spaces, or in single quotes; or recreate the file with `python -m qte_sdk.token set`.

## SESSION

### QTE-SESSION-REJECTED

- Raised as: `qte_sdk.connection.SessionRejected`, with `reason_code`, `reason_name` and `detail`. A `ReconnectingSession` does not retry it.
- Cause: The exchange refused the session with `session_reject`, or with a `reject` of `auth`.
- Fix: Depends on `reason_name`, which the message names:

| `reason_name` | Fix |
|---|---|
| `NOT_AUTHENTICATED` | Run `python -m qte_sdk.token check`. If the token is found and still refused, ask the Head of Technology for a new one |
| `TEAM_DISABLED`, `NO_MARKET_ACCESS` | Ask the Head of Technology about your team's access |
| `EXCHANGE_OUTAGE` | Wait for the exchange's outage to end, then connect again |
| Any other | Ask the course team, quoting the whole message |

### QTE-SESSION-VERSION-MISMATCH

- Raised as: `qte_sdk.connection.ContractVersionMismatch`, a `SessionRejected`, on a `session_reject`, a `reject` or a `ticket_reject` with `VERSION_MISMATCH`, during a session too.
- Cause: The exchange does not serve the contract version this SDK sends, so nothing sent can work.
- Fix: Run `python -m qte_sdk.update` and the command it prints.

### QTE-SESSION-NOT-ACKNOWLEDGED

- Raised as: `qte_sdk.session.SessionNotAcknowledged`, with `close_code`. A `ReconnectingSession` retries it.
- Cause: The connection closed, or `session_ack` could not be read, before the session was acknowledged; the message gives the close code, and the reason when it is one the SDK knows, such as `term change`.
- Fix: Try again in a few seconds. If it keeps happening, run `python examples/smoke_test.py` and send its output to the course team.

### QTE-SESSION-AUTH-NOT-SENT

- Raised as: `qte_sdk.session.AuthNotSent`, a `SessionNotAcknowledged`. Not retried.
- Cause: The `auth` message could not be encoded or sent, such as for a bad `contract_version`. Trying again fails the same way.
- Fix: Check the options passed to `open_session`, and leave `contract_version` unset to use the SDK's own.

### QTE-SESSION-TIMEOUT

- Raised as: `qte_sdk.session.SessionTimeout`, a `TimeoutError`, with `seconds`.
- Cause: No `session_ack` arrived within `ack_timeout` seconds of calling `open_session`.
- Fix: Try again, raise `ack_timeout`, or run `python examples/smoke_test.py` to check the setup.

### QTE-SESSION-RESUME-REJECTED

- Raised as: `qte_sdk.session.ResumeRejected`, a `SessionRejected`, from `Session.resume`.
- Cause: The exchange refused the `resume`. The session goes on, but no report was replayed and no snapshot sent.
- Fix: Treat the resting orders you track (such as a `RestingOrders` view) as unknown until a later resume completes, and check your positions with `send_account_query` before you trade.

### QTE-SESSION-RESUME-NOT-ACKNOWLEDGED

- Raised as: `qte_sdk.session.ResumeNotAcknowledged`, a `SessionNotAcknowledged`, from `Session.resume`.
- Cause: The connection ended, or the `resume_ack` could not be read, before the exchange answered the resume. Reports sent while you were away were not replayed.
- Fix: Open a new session and resume from the same `last_report_seq` (a `ReconnectingSession` does this).

### QTE-SESSION-RESUME-TIMEOUT

- Raised as: `SessionTimeout`, a `TimeoutError`, from `Session.resume`.
- Cause: The exchange did not answer the resume within `timeout` seconds.
- Fix: Open a new session and resume again, or raise `timeout`.

## CONNECT

### QTE-CONNECT-HANDSHAKE-FAILED

- Raised as: `qte_sdk.connection.HandshakeFailed`, a `websockets` `InvalidHandshake`, with `kind` and `status_code`. Only the kind and the HTTP status are kept, since the server's reply could reflect anything.
- Cause: The WebSocket opening handshake failed.
- Fix: Depends on the HTTP status, which the message names:

| Status | Fix |
|---|---|
| 401, 403 | The exchange did not accept the connection: run `python -m qte_sdk.token check`, and ask the Head of Technology if the token is found and still refused |
| 404 | Nothing is served at that address: check that `QTE_URL` is the exchange's `wss://` address, path included |
| 408, 429, 5xx | The exchange is busy or down: try again in a minute (a `ReconnectingSession` does) |
| None or other | Check that `QTE_URL` is the exchange's `wss://` address |

### QTE-CONNECT-LIVENESS-TIMEOUT

- Raised as: `qte_sdk.connection.LivenessTimeout`, a `TimeoutError`, with `timeout`. A `ReconnectingSession` retries it.
- Cause: After the first heartbeat, nothing arrived from the exchange for `liveness_timeout` seconds, so the link was presumed dead and dropped. Orders in flight may or may not have reached the exchange.
- Fix: Reconnect, then check your resting orders and positions.

### QTE-CONNECT-NO-SESSION

- Raised as: `qte_sdk.reconnect.NotConnected`, a `RuntimeError`, from `ReconnectingSession.send`.
- Cause: No session was up when the message was sent, before the first `Connected` event or after a `Disconnected` one. The message names the message type. Nothing was sent, and nothing is queued to send later.
- Fix: Wait for the next `Connected` event, then send it again.

## HISTORY

These are raised by `qte_sdk.history.HistoryClient`, its `fetch`, `fetch_session_state` and `fetch_range`, and by a replay, which raises a download's error as `fetch` does. Every error from a response is a `HistoryError`, with `http_status`, and the service's own `status` word and `message` when the response carried them. The message quotes the service's text, flattened to one line. A network failure, such as `TimeoutError` or `ConnectionRefusedError`, keeps its own type and has no code: check your network and `QTE_HISTORY_URL`.

### QTE-HISTORY-ADDRESS-MISSING

- Raised as: `qte_sdk.history.MissingHistoryURL`, a `ValueError`, from `HistoryClient`.
- Cause: `HistoryClient` was given no `url=`, and `QTE_HISTORY_URL` is unset or empty. It has no default address, and never reads one from `.env`.
- Fix: Pass `url=`, or set `QTE_HISTORY_URL` in the environment to the `https://` address the course team gave you.

### QTE-HISTORY-ADDRESS-INVALID

- Raised as: `qte_sdk.history.HistoryAddressInvalid`, a `ValueError`, from `HistoryClient`. The smoke test's `history` check fails with it.
- Cause: The address is not a well-formed `https://` URL, holds credentials, a query or a fragment, has a port that is not a number, or is a plain `http://` address for another computer (only a test server on your own computer may use `http://`). The client will not send your token there. The message never shows the address, since a mistake could have put the token in it.
- Fix: Set `QTE_HISTORY_URL`, or pass `url=`, to the `https://` address you were given.

### QTE-HISTORY-PENDING

- Raised as: `qte_sdk.history.HistoryPending`, a `HistoryError`, with `retry_after`.
- Cause: The session has closed but its data is not ready yet (HTTP 202), and waiting longer would have passed `max_wait` or `max_retries`. It will be ready.
- Fix: Ask again later, after `retry_after` seconds if it is set, or raise `max_wait`.

### QTE-HISTORY-RATE-LIMITED

- Raised as: `qte_sdk.history.HistoryRateLimited`, a `HistoryError`, with `retry_after`.
- Cause: Too many requests for your team's token (HTTP 429), and waiting longer would have passed `max_wait` or `max_retries`.
- Fix: Wait `retry_after` seconds before the next request, and make fewer, larger requests, such as one `fetch_range` for several days.

### QTE-HISTORY-UNAVAILABLE

- Raised as: `qte_sdk.history.HistoryUnavailable`, a `HistoryError`.
- Cause: The data will never exist (HTTP 404): a date before the service's coverage, a day with no session, or an instrument or channel the service does not know. Asking again does not change that.
- Fix: Check the date against the calendar, and the instrument and channel against the instruments the exchange lists.

### QTE-HISTORY-NOT-CLOSED

- Raised as: `qte_sdk.history.HistoryNotClosed`, a `HistoryError`.
- Cause: The session is running now, or lies in the future (HTTP 409). Nothing is served for a session before its close, so the client does not wait.
- Fix: Ask again after the session's close.

### QTE-HISTORY-NOT-IMPLEMENTED

- Raised as: `qte_sdk.history.HistoryNotImplemented`, a `HistoryError`.
- Cause: The service does not serve this endpoint yet (HTTP 501). Unlike `QTE-HISTORY-UNAVAILABLE`, a later release of the service may.
- Fix: Use another endpoint, or ask again after the service is updated.

### QTE-HISTORY-REQUEST-REJECTED

- Raised as: `qte_sdk.history.HistoryRequestRejected`, a `HistoryError`.
- Cause: The service found the request malformed (HTTP 400), such as a date that does not parse, `to_date` before `from_date`, or a range larger than it allows. The same request fails the same way.
- Fix: Fix the arguments: dates as `YYYY-MM-DD`, `to_date` not before `from_date`, and a range the service allows.

### QTE-HISTORY-UNAUTHENTICATED

- Raised as: `qte_sdk.history.HistoryUnauthenticated`, a `HistoryError`.
- Cause: The service did not recognise the token (HTTP 401).
- Fix: Run `python -m qte_sdk.token check`. If the token is found and still refused, ask the Head of Technology for a new one.

### QTE-HISTORY-FORBIDDEN

- Raised as: `qte_sdk.history.HistoryForbidden`, a `HistoryError`.
- Cause: The token may not read this data (HTTP 403). The service serves only what your team may see.
- Fix: Ask the course team whether this data is open to your team.

### QTE-HISTORY-INTERRUPTED

- Raised as: `qte_sdk.history.HistoryInterrupted`, a `HistoryError`, with `bytes_received`.
- Cause: The connection dropped during a download more often than `max_resumes` allows. The messages already yielded are incomplete, and were not checked against the digest the service stated.
- Fix: Discard them and fetch again, or raise `max_resumes`.

### QTE-HISTORY-CHANGED

- Raised as: `qte_sdk.history.HistoryChanged`, a `HistoryError`.
- Cause: A dropped download could not be resumed where it stopped, because the service now serves different data for the request (for `fetch_range`, typically an entry that was pending has become ready).
- Fix: Discard what was yielded and fetch again from the start.

### QTE-HISTORY-CORRUPT

- Raised as: `qte_sdk.history.HistoryCorrupt`, a `HistoryError`, after the messages were yielded.
- Cause: What arrived does not match the length or the SHA-256 digest the service stated for it, so the messages already yielded may be wrong.
- Fix: Discard them and fetch again. If it happens again, tell the course team.

### QTE-HISTORY-BAD-RESPONSE

- Raised as: `qte_sdk.history.HistoryError`, before any message or when resuming.
- Cause: The response is not one the history service sends: not uncompressed NDJSON, without the identity `ETag`, a range response with no manifest or one that cannot be read, or an answer to a resume that is not the rest of the same data. The client can neither check nor resume it.
- Fix: Check that `QTE_HISTORY_URL` is the history service's address. If it is, tell the course team.

### QTE-HISTORY-UNEXPECTED-STATUS

- Raised as: `qte_sdk.history.HistoryError`, with `http_status`.
- Cause: The service answered with an HTTP status this SDK does not know, such as a 500 from the service or a proxy.
- Fix: Try again later. If it happens again, tell the course team the status.

### QTE-HISTORY-REQUEST-FAILED

- Raised as: `qte_sdk.history.HistoryError`; the message names only the kind of error, with its details withheld.
- Cause: Sending the request or reading the answer failed with an error other than a network error, such as a malformed status line, so no answer the client could use arrived. Its details are withheld, since they can quote the request's headers.
- Fix: Try again. If it happens again, check your network and `QTE_HISTORY_URL`.

## REPLAY

### QTE-REPLAY-OUT-OF-ORDER

- Raised as: `qte_sdk.replay.ReplayOutOfOrder`, a `ValueError`, from `replay`, after the messages before it.
- Cause: A downloaded stream holds a message earlier than the one before it. The replay cannot merge it in order, so it stopped and closed every download. The message names the stream, the date and both times.
- Fix: Replay without that stream, and tell the course team its date and name.

## UPDATE

### QTE-UPDATE-AVAILABLE

- Raised as: the message of `python -m qte_sdk.update` and of `check_for_update()` (`UpdateCheck.code`), and a WARNING from the `qte_sdk.update` logger when the automatic check finds a newer release. That warning is written from the check's own thread and, with the standard stream handlers, never holds up the session or the program's exit (a stderr wrapped by colorama, rich or a tee gets the same check, and can then wait only in a rare race, or when it writes a partial line of the program's own that it held back): on a stderr pipe or terminal that cannot take the whole line at once (a full pipe that nothing reads, say), it is skipped that day, and the next day's check says it again; see "The automatic check" in the quickstart. `python -m qte_sdk.update` exits 1.
- Cause: A newer release of qte-sdk is out; the message says whether it is a recommended update, and why.
- Fix: Run the command the message gives.

## Retired codes

None yet. A retired code is listed as `- QTE-AREA-WHAT: retired in vX.Y.Z; use QTE-...`.
