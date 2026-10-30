# Warning and error codes

**Version:** 0.28

Every warning and error the SDK raises or logs at WARNING has a code, such as `QTE-TOKEN-MISSING`: about the token, the exchange address, the `.env`, the session, the connection, the history service, replays, updates, the log format, the message budgets and halted options chains. This page lists each code with its cause and fix, and the exit codes of the SDK's commands. Errors raised for a wrong argument, such as a `TypeError` for a string where a list belongs, and network errors such as `TimeoutError`, have no code: their message and the line of your code in the traceback say what to change.

## How to read a message

A message has one shape, in one line:

    <CODE>: <what happened>. <why it matters>. <next step>.

For example:

    QTE-TOKEN-MISSING: no token was found in token=, QTE_TOKEN, QTE_TOKEN_FILE or ./.env. The SDK cannot authenticate without your team token. Run python -m qte_sdk.token set in your project folder, or set QTE_TOKEN_FILE to a file that holds it.

The last line of a traceback is the class name and then this message. The code is also in the `code` attribute of every SDK exception and warning, and in the `code` attribute of a log record logged at WARNING. Look a code up below by searching this page for it.

- Catch `qte_sdk.errors.QteError` for any SDK exception that has a code, or filter on `qte_sdk.errors.QteWarning` for any such warning. Each exception keeps the base class it had before codes were added (`MissingToken` is still a `ValueError`, `LivenessTimeout` a `TimeoutError`), so an existing `except` clause still catches it.
- To filter warnings by text, match the code, which is at the start: `warnings.filterwarnings("ignore", message="QTE-DOTENV")`. Filtering by class, such as `DotenvNotIgnored`, works as before.
- No message ever holds your token. Text from the exchange or the history service, such as a rejection's detail, is flattened to one line in the message and kept as sent in the exception's attribute (`detail`, or `message` for a history error), and is withheld if it repeats any part of the token. Text you gave, such as an instrument, a message type, a replay's date or channel, or the path of a `.env` or token file, is withheld from the message if it holds the token, as written, percent-encoded or escaped. Where the SDK has no token to look for, as in the git warning before the `.env` is read or a `Session`'s `repr`, text holding a run of 32 hex digits, the shape of the exchange's tokens, is withheld instead.
- The exchange's refusal of an order, a ticket, a break request or an account query is an event your loop reads, not an SDK error, and has no `QTE-` code. Only a refusal of the session itself (its `auth` or a resume) or one with `VERSION_MISMATCH` raises, with a `QTE-SESSION-` code below: a `reject` names its reason with `reason_code_name`, and a `ticket_reject` or `break_request_reject` with `qte_sdk.tickets.ticket_reason_code_name`. Two reasons refuse only a pod's `break_request`: `BREAK_REQUEST_NOT_IN_BREAK` (1721), sent outside an inter-term break (before the close of a term's final session, or from the next term's first session on; in Winter the break runs from the close on Friday 4 December 2026), so send it again only once the break has begun; and `BREAK_REQUEST_EXISTS` (1722), when the pod already holds a pending or approved request in that instrument, so withdraw that one with `send_break_request_withdraw` first if you mean to replace it. The `qte_sdk.tickets` docstring lists the others.
- A code never changes once released. A code no longer used is listed under [Retired codes](#retired-codes) and never reused.

## Order rejects

The exchange's `reject` of an order is an event, with no `QTE-` code, whose `reason_code` says why. For the reasons below, `qte_sdk.errors.order_reject_step(reason)` returns the next step (from `qte_sdk.errors.ORDER_REJECT_STEPS`), given the reason's name or number; for any other it returns `None`.

| `reason_code` | Next step |
|---|---|
| `STRATEGY_NOT_REGISTERED` | This strategy ID has no live registration on this term. Each new term starts with none, and retiring a registration ends it, so your Team Lead must register it again on the console: Strategy registration, Register a new strategy, Submit. After a retirement the new version must be above every version your team has held under this strategy ID, or it is refused VERSION_NOT_NEW. Orders sent after it is accepted no longer get this reject |

## Exit codes

| Command | 0 | 1 | 2 | Other |
|---|---|---|---|---|
| `python -m qte_sdk.token check` | The token and the address are found, the address starts with `ws://` or `wss://`, and nothing needs fixing | Something must be fixed: no token or address (`QTE-TOKEN-*`, `QTE-ADDRESS-*`, `QTE-DOTENV-UNREADABLE`, `QTE-DOTENV-INVALID`), a `.env` git tracks or does not ignore (`QTE-DOTENV-TRACKED`, `QTE-DOTENV-NOT-IGNORED`), on Windows a file others may read, change or replace (`QTE-TOKEN-SHARED`, `QTE-ADDRESS-SHARED`), or on macOS and Linux a `.env` others can change or replace (`QTE-DOTENV-WRITABLE`) | It could not tell: git could not say whether it ignores the `.env` (`QTE-DOTENV-GIT-UNKNOWN`), or Windows would not let it fully check a file (`QTE-TOKEN-UNCHECKED`, `QTE-ADDRESS-UNCHECKED`). Also a usage error | |
| `python -m qte_sdk.token set` | Saved | Refused, with `error: <CODE>: ...` on stderr; nothing was changed | Usage error | 130 when stopped with Ctrl+C or end of input |
| `python -m qte_sdk.update` | The SDK is the latest release | A newer release is out (`QTE-UPDATE-AVAILABLE`) | It cannot tell whether the SDK is current. Also a usage error or a bad `--timeout` | |
| `python examples/smoke_test.py` | No check failed | A check failed, or the SDK is too old to check itself | No token or no usable address, so it could not connect. Also a usage error | 130 on Ctrl+C; 128 plus the signal's number on SIGTERM or SIGHUP |

`token check` exiting 1 is a report, not a refusal: a session still only warns about a tracked `.env` or, on Windows, a shared file, and still uses it. Exit 2 with a `usage:` line on stderr is a mistake on the command line, for every command here. For `python -m qte_sdk.token`, that error never repeats a word of the command line it did not expect, since a token pasted there would be one: it says to run `python -m qte_sdk.token set` and paste the token at the hidden prompt, and to remove the token from the shell history. `token check` looks afresh each time it runs, so `token.main(["check"])` gives the same answer when a program calls it twice, or after opening a session; it never prints a path that holds the token. Before this release `token check` exited 0 even when it printed a warning.

With `--json`, each command exits with the same status, which the document gives as `exit_code` (see [JSON output](#json-output)). Each command also exits 2, with `error: QTE-LOG-FORMAT-INVALID: ...` on stderr and nothing on stdout, when `QTE_LOG_FORMAT` is neither `json` nor `text` (and `--json` is not given).

## JSON output

For coding agents and scripts. Every JSON line and document is one physical line of compact JSON in ASCII alone (any other character is escaped, so it reads the same through a Windows pipe in any code page), with its keys always present and in the order below (`null` when not known). None ever holds your token: a text or path that holds it is given as `(withheld: it holds the token)`. In a log line, that is any text holding the token in `QTE_TOKEN`, or 32 hex digits in a row, the shape of the exchange's tokens, whatever logged it. A message never starts with its code: the code is its own key, and the next step, without its full stop, is too, so the plain-text line is `<code>: <message> <next_step>.`

Log lines. `qte_sdk.logs.configure()` (which the commands and examples call) with `QTE_LOG_FORMAT=json`, or `configure("json")`, writes each SDK log record to stderr as one JSON object per line:

    {"time":"2026-10-08T12:34:56.789Z","level":"WARNING","logger":"qte_sdk.update","code":"QTE-UPDATE-AVAILABLE","message":"qte-sdk 1.1.0 is behind 1.1.1.","next_step":"Update with pip install --upgrade \"git+https://github.com/josh-g-s/qte-sdk\"","fields":{}}

`time` is UTC to the millisecond; `level` the record's level name; `logger` the logger's name; `code` the code or `null`; `message` what happened and why; `next_step` the next step or `null`; `fields` any values the record names, such as a path (`{}` for none). A record logged with an exception also has `exception`, its type name only. With `capture_warnings=True` (the commands set it), SDK warnings are logged the same way, through the `qte_sdk.warnings` logger, with `fields` `{"category": "<warning class>"}`. The automatic update check's line gives the reason for a recommended update in `message`; if the line would be over 512 bytes, it says to run `python -m qte_sdk.update` for the reason instead, so that it can be written to a pipe whole.

`--json` documents. `python -m qte_sdk.token check --json`, `python -m qte_sdk.update --json` and `python examples/smoke_test.py --json` print one JSON document on stdout, and nothing else there. Each starts with `command` (`"token check"`, `"update"` or `"smoke_test"`), `schema` (1) and `exit_code` (the exit status, as in the table above). A usage error is still a plain `usage:` line on stderr, with exit 2 and nothing on stdout.

- `token check`: `result` (`"ok"`, `"fix"` or `"cannot-tell"`, for exit 0, 1 and 2); `token` {`found`, `source` (`"QTE_TOKEN"`, `"QTE_TOKEN_FILE"`, `".env"` or `null`), `path` (the file's path, or `null` for an environment variable), `note` (on Windows, what the access check found)}; `address` {`found`, `source` (`"QTE_URL"`, `".env"` or `null`), `path`, `valid` (whether it starts with `ws://` or `wss://`)}; `findings`, a list of {`code`, `kind` (`"fix"` or `"cannot-tell"`), `message`, `next_step`}.
- `update`: `status` (`"current"`, `"behind"` or `"unknown"`), `code` (`QTE-UPDATE-AVAILABLE` when behind, else `null`), `message`, `next_step`; `installed` {`version`, `description` (as the text's `installed:` line, with a pinned commit cut to 12 hex digits), `commit` (12 hex digits), `revision` (`"main"`, a release tag, a commit cut to 12 hex digits, `"other"` or `null`), `archive` (a release tag, `"main"`, `"other"` or `null`)}; `latest_release`; `main_commit` (12 hex digits); `main_ahead`; `update_command` (the command that updates, or `null`); `recommended`; `why`.
- `smoke_test`: `summary` {`passed`, `failed`, `skipped`}; `checks`, a list of {`name`, `status` (`"pass"`, `"fail"` or `"skip"`), `code` (the first code its message names, or `null`), `message` (as the text line gives it)}; `problems` (why it could not connect, for exit 2); `warnings` (the notes it wrote to stderr: a fill of the test order, a level where it may still rest, an interruption); `stopped_by` (`null`, `"SIGINT"`, `"SIGTERM"` or `"SIGHUP"`). The document comes at the end, on every exit, a stop by Ctrl+C or a signal included. With `--json`, what it writes to stderr is JSON lines too: SDK log records, and its own notes as log lines with `"logger":"smoke_test"`.

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
- Cause: A trading token is 64 characters, 0-9 and a-f, on one line, so this always means the paste picked up something extra or lost part of the token. `token set` first removes spaces and line breaks around the token, then refuses a token with a space (a non-breaking one included, or one after a label such as `Token:`), a quote, a `#`, or an invisible or control character (a zero-width space, say, or the character a Windows terminal can insert for Ctrl+V). `HistoryClient` refuses a token with a control or invisible character, a newline, or a character outside ASCII. The message says how many characters the token has and which of those kinds it found; it never shows the token, any part of it, where the character is, or its codepoint.
- Fix: Copy the token again from plain text, or type it, and paste it only at the hidden prompt of `python -m qte_sdk.token set`; never paste it anywhere else.

### QTE-TOKEN-UNUSUAL

- Raised as: a `warning:` line on stderr from `token set`, after it saves the token. `set` still exits 0.
- Cause: The token entered has none of the characters `QTE-TOKEN-MALFORMED` refuses, but it is not 64 characters of 0-9 and a-f, such as one cut short or one in capitals. The message gives its length and whether it has characters outside 0-9 and a-f, never the token or any part of it. It is only a warning, so a future token format still works.
- Fix: If the exchange refuses the token, copy it again from plain text, or type it, and run `python -m qte_sdk.token set` again.

### QTE-TOKEN-NO-TERMINAL

- Raised as: `token set` refuses to run.
- Cause: `set` was run with its input redirected or piped, or it could not turn off echo, so the token could have been shown or recorded. On Windows, input from `NUL` counts as no terminal.
- Fix: Run `python -m qte_sdk.token set` yourself, in an ordinary terminal window. A coding agent should ask you to run it.

### QTE-TOKEN-SET-PATH

- Raised as: `token set` refuses the destination; nothing is changed.
- Cause: The `.env` is a symbolic link, not a regular file, not UTF-8 text, or would grow past the 64 KiB the SDK reads; or the `--file` path is a folder, a symbolic link or a `.gitignore`, or has a run of 32 hex digits like a token or a control character or line break (not shown, since it may be your token).
- Fix: Edit the file by hand, or choose another path with `--file PATH`.

### QTE-TOKEN-SET-ARGUMENT

- Raised as: `token set` or `token check` refuses to run, as a usage error: exit 2, a `usage:` line and this message on stderr, nothing on stdout (even with `--json`), and nothing changed.
- Cause: The command was given a value it does not take, such as the token itself (`python -m qte_sdk.token set <token>`). The value is not shown, since it may be your token.
- Fix: Run the command again without that value, keeping any options such as `--file PATH`; `set` asks for the token. If you typed your token, clear it from your shell history.

### QTE-TOKEN-SET-WRITE

- Raised as: `token set` refuses; the message names the system's reason, such as `Permission denied`.
- Cause: `set` could not read the existing `.env`, create the token file's folder, update `.gitignore`, or write the file. No partial copy of the token is left behind.
- Fix: Check the folder's permissions and free space, then run the command again.

## ADDRESS

### QTE-ADDRESS-MISSING

- Raised as: `qte_sdk.session.MissingURL` (from `open_session`, `resolve_url`, `ReconnectingSession`); `token check` exits 1; `token set` refuses an empty address.
- Cause: No address was passed, `QTE_URL` is unset or empty, and `./.env` sets no `QTE_URL`.
- Fix: Run `python -m qte_sdk.token set` and enter the exchange's address, `wss://api.trading.queenstowerexchange.com/ws`, or set `QTE_URL`.

### QTE-ADDRESS-INVALID

- Raised as: a `token check` line (exit 1); `token set` refuses the address; the smoke test's `address` check fails.
- Cause: The address does not start with `ws://` or `wss://`, has spaces or quotes, or (in the smoke test) is a plain `ws://` address for another computer, which would send your token unencrypted.
- Fix: Set `QTE_URL` to the exchange's address, `wss://api.trading.queenstowerexchange.com/ws`, for example with `python -m qte_sdk.token set`. A test exchange on your own machine may use `ws://127.0.0.1:8080/ws`.

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

### QTE-DOTENV-WRITABLE

- Raised as: on macOS and Linux, the warning `qte_sdk.dotenv.TokenFileShared` (or `AddressFileShared` for a `.env` that sets `QTE_URL` but holds no token), once per process for each file, logged at WARNING instead if a warnings filter makes it an error. `token check` exits 1; `token set` prints it as `Warning:` after saving.
- Cause: The `.env` holds `QTE_TOKEN` or sets `QTE_URL`, and its group or other users can write to it, or to a folder that holds it or any folder above that, up to `/`, so they could replace it; or another account than you or root owns the file or any of those folders, whatever its mode, since an owner can change the file, or add or remove files in the folder. Each part of the path is looked at as named, so when the `.env` is a symbolic link, or its path leads through a link to a folder, the file it leads to is judged, and each link on the way, to a folder or to the file, the folders that hold them and the folders above those. Root-owned links such as `/tmp`, `/var` and `/etc` on macOS, in folders only root can change, are not reported. A link another account owns is reported when others can write to its folder, sticky bit or not, since its owner could then remove it and make another. A folder with the sticky bit, such as `/tmp` or `/Users/Shared`, is not reported if you or the system own it, since only a file's owner, or the folder's, may remove or rename a file there. Whoever can change the address can point it at a server of their own and capture your token when you next connect. On Linux, a umask of 002 makes new files group-writable; with a private group for each user that is harmless, so this is a warning, not a refusal, and the SDK still uses the file.
- Fix: When you own every path the message names, it gives a `chmod go-w` command: run it, or move the `.env` to a folder only you can change and run `chmod 600` on it. When another account or the system owns one of them, you cannot chmod it, and the message says to move the `.env` to a folder you own that only you can change and run `chmod 600` on it. Then run `python -m qte_sdk.token check`. A path that holds the token, or has a line break, is left out of the command.

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
| `NO_MARKET_ACCESS` with the detail "the ... arm is not open for trading this term (term configuration open_arms)" | Your team's arm does not trade this term (the term configuration's open_arms). Do not retry or ask for a new token: the arms open for trading change only at a later term |
| `TEAM_DISABLED`, any other `NO_MARKET_ACCESS` | Ask the Head of Technology about your team's access |
| `EXCHANGE_OUTAGE` | Wait for the exchange's outage to end, then connect again |
| Any other | Ask the course team, quoting the whole message |

### QTE-SESSION-VERSION-MISMATCH

- Raised as: `qte_sdk.connection.ContractVersionMismatch`, a `SessionRejected`, on a `session_reject`, a `reject`, a `ticket_reject` or a `break_request_reject` with `VERSION_MISMATCH`, during a session too.
- Cause: The exchange does not serve the contract version this SDK sends, so nothing sent can work.
- Fix: Run `python -m qte_sdk.update` and the command it prints.

### QTE-SESSION-NOT-ACKNOWLEDGED

- Raised as: `qte_sdk.session.SessionNotAcknowledged`, with `close_code` and `credential_revoked`. A `ReconnectingSession` retries it, unless `credential_revoked` is True (a close with code 1008 and reason `credential revoked`).
- Cause: The connection closed, or `session_ack` could not be read, before the session was acknowledged; the message gives the close code, and the reason when it is one the SDK knows, such as `term change`.
- Fix: Try again in a few seconds; if `credential_revoked` is True, get a new token instead, since the old one is refused. If it keeps happening, run `python examples/smoke_test.py` and send its output to the course team.

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
- Fix: Open a new session and resume from the same `last_report_seq` (a `ReconnectingSession` does this). If `credential_revoked` is True, get a new token first: the old one is refused, and a `ReconnectingSession` stops.

### QTE-SESSION-RESUME-TIMEOUT

- Raised as: `SessionTimeout`, a `TimeoutError`, from `Session.resume`.
- Cause: The exchange did not answer the resume within `timeout` seconds.
- Fix: Open a new session and resume again, or raise `timeout`.

### QTE-SESSION-LOG-CHANGED

- Raised as: a WARNING from the `qte_sdk.reconnect` logger, with `old_log_id`, `new_log_id` and `cursor`, logged by a `ReconnectingSession` when it forgets its report cursor for this reason.
- Cause: A new session's `session_ack` named a different event log (`log_id`, 16 lowercase hex characters) from the one the report cursor was counted under: the exchange started a new log, for a new term or a fresh start. Report numbers belong to one log, so the old cursor would skip the new log's first reports. The cursor was reset, as at a term change: with a resume the session asks from 0 and gets a snapshot of your resting orders; with `resume=False` it counts the new log's reports from the first it reads. A `log_id` that is absent or malformed is unknown: it resets the cursor too, as the contract asks, but without this warning.
- Fix: Nothing to retry. Treat the resting orders you track as unknown until a snapshot loads them: with a resume, until the next `ResumeComplete`; with `resume=False`, which never gets one, until you call `resume(0)` on a session of your own. Check your positions with `send_account_query` before you trade. With `Session.resume` on your own, pass your saved `last_report_seq` only when `info.log_id` is known and equal to the one it was counted under, and 0 otherwise.

### QTE-SESSION-UNANSWERED

- Raised as: a WARNING from the `qte_sdk.reconnect` logger, with `count` and `refs` (each request's message type and `request_ref`, never its contents), logged by a `ReconnectingSession` when a session drops; the same requests are in that `Disconnected`'s `unanswered`, as (message type, request_ref) pairs. The log shortens a ref of 32 hex digits, such as one `new_request_ref` made, to its first 12 and `...`, since a log withholds anything shaped like a token; `unanswered` has it whole.
- Cause: These requests were sent on the session that dropped and got no answer echoing their `request_ref` (`accepted`, `reject`, `order_cancelled`, `account_state`, `ticket_accepted`, `ticket_reject`, `break_request_state` or `break_request_reject`). They are unconfirmed, not lost: each may or may not have reached the exchange, and a resume that omits one does not prove it is gone, since one still waiting out its order delay can be applied after `ResumeComplete`. A `reject` with no `request_ref` cannot be matched, so its request stays listed. A ref sent again while still listed is listed once, under its first message type, until the first answer echoing it; give each request its own ref.
- Fix: After `ResumeComplete`, settle each against `RestingOrders`, your fills and (after a snapshot) your position from `send_account_query`, then check again after one order delay. Never resend an order only because it is not resting. Send a `cancel` again if its target still rests. Never repeat an `amend` as sent: it sets the remaining size, so work out the size you want from what now rests.

### QTE-SESSION-DISCONNECTED

- Raised as: a WARNING from the `qte_sdk.reconnect` logger, with `close` and `unanswered`, logged by a `ReconnectingSession` once for each `Disconnected` it delivers (not for each retry, which is a `Retrying` event, and not when you call `close`). `close` is the `qte_sdk.connection.describe_close` line, such as `close code 1012` or `close code 1006: the connection dropped with no close frame from the exchange`; when the session ended another way, such as a link presumed dead, it is the failure's type and code, such as `LivenessTimeout (QTE-CONNECT-LIVENESS-TIMEOUT)`, and never its message, followed for a close while connecting or resuming by that close's line, such as `ResumeNotAcknowledged (QTE-SESSION-RESUME-NOT-ACKNOWLEDGED), close code 1008, reason "credential revoked": your token was revoked or rotated; get a new token, and do not connect again with this one`. On a full pipe a line over 512 bytes is written short, with only the close's code and shown reason. `unanswered` is empty, or the number of requests with no answer, such as `; 2 requests were unanswered (QTE-SESSION-UNANSWERED)`: their refs are in that warning, logged for the same drop.
- Cause: The session ended: the exchange closed it, the connection dropped with no close frame, or the link failed. The same drop is delivered as a `Disconnected` event; this line makes it visible in a log too. A session from `open_session` logs nothing: its iteration ends or raises instead.
- Fix: Treat your resting orders as uncertain until the next `ResumeComplete`, and check your positions with `send_account_query` before you trade: a snapshot shows only what rests now, not what filled. Act on the close's cause: for 1008 `credential revoked`, get a new token; for most others, the `ReconnectingSession` reconnects and resumes by itself.

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
- Fix: Pass `url=`, or set `QTE_HISTORY_URL` in the environment to the history service's address, `https://history.trading.queenstowerexchange.com`.

### QTE-HISTORY-ADDRESS-INVALID

- Raised as: `qte_sdk.history.HistoryAddressInvalid`, a `ValueError`, from `HistoryClient`. The smoke test's `history` check fails with it.
- Cause: The address is not a well-formed `https://` URL, holds credentials, a query or a fragment, has a port that is not a number, or is a plain `http://` address for another computer (only a test server on your own computer may use `http://`). The client will not send your token there. The message never shows the address, since a mistake could have put the token in it.
- Fix: Set `QTE_HISTORY_URL`, or pass `url=`, to the history service's address, `https://history.trading.queenstowerexchange.com`.

### QTE-HISTORY-PENDING

- Raised as: `qte_sdk.history.HistoryPending`, a `HistoryError`, with `retry_after`.
- Cause: The session has closed but its data is not ready yet (HTTP 202), and waiting longer would have passed `max_wait` or `max_retries`. It will be ready, except an option contract or option chain asked for before its session is rebuilt, which becomes unavailable instead if the session's chain does not list it.
- Fix: Ask again later, after `retry_after` seconds if it is set, or raise `max_wait`.

### QTE-HISTORY-RATE-LIMITED

- Raised as: `qte_sdk.history.HistoryRateLimited`, a `HistoryError`, with `retry_after`.
- Cause: Too many requests for your team's token (HTTP 429), and waiting longer would have passed `max_wait` or `max_retries`.
- Fix: Wait `retry_after` seconds before the next request, and make fewer, larger requests, such as one `fetch_range` for several days.

### QTE-HISTORY-UNAVAILABLE

- Raised as: `qte_sdk.history.HistoryUnavailable`, a `HistoryError`.
- Cause: The data will never exist (HTTP 404): a date before the service's coverage, a day with no session, an instrument or channel the service does not know, `option_greeks` of an equity, an option contract the session's chain did not list, or the option chain of a session with none. Asking again does not change that. The sessions with options cached before the service's options build (qte-platform #2298, now live), 15 to 21 October 2026, have all been upgraded (an upgrade adds the missing objects and leaves every object already cached unchanged), so they serve their Greeks and chain and their answer is final too. `fetch` raises it for an unavailable channel, and so does a default `replay` of the contract; `replay(..., skip_unavailable=True)` skips that channel instead.
- Fix: Check the date against the calendar, and the instrument and channel against the instruments the exchange lists; for an option contract, check it is in that session's chain (`fetch_option_chain`), and ask for `option_greeks` only of an option contract.

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
- Cause: The token may not read this data (HTTP 403): `fetch_reports` or `fetch_reports_range` named another team, or a session of a term other than the one the token belongs to, or the data is not open to your team. The service serves a team's private reports only to that team, and only for sessions of the token's current term.
- Fix: For reports, pass the team name your session reports (`session.info.team`) exactly, and session dates of the current term only: an earlier term's reports are not served. If both are right and it is still refused, the service's own term setting is wrong: tell the course team. For anything else, ask the course team whether this data is open to your team.

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

## LOG

### QTE-LOG-FORMAT-INVALID

- Raised as: `qte_sdk.logs.LogFormatInvalid`, a `ValueError`, from `qte_sdk.logs.configure()`; the SDK's commands and `examples/smoke_test.py` print it on stderr and exit 2. The value is not shown.
- Cause: `QTE_LOG_FORMAT`, or the format passed to `configure()`, is not `json` or `text` (case and surrounding spaces do not matter; unset or empty means text).
- Fix: Set `QTE_LOG_FORMAT` to `json` or `text`, or unset it.

## PACING and BUDGET

These are about your team's message budgets. The exchange counts every `new`, `cancel` and `amend` your team sends, rejected ones too, toward rolling windows per team across all its connections, and rejects a message (reason codes 1500 to 1502) when its window is full; `mass_cancel` never counts. The `QTE-PACING-` codes come from a `qte_sdk.pacing.Pacer`, which paces those messages under the values you give it; `QTE-BUDGET-REJECTED` is logged by a session without one. The warnings are logged through the `qte_sdk.pacing` logger, from a thread of their own, never the event loop, and are dropped rather than wait on a stderr pipe that is full. A pipe, socket or terminal takes a line of at most 512 bytes whole, so a longer one (a reject's JSON line always is) is written there in a short form: the code, what happened, the limit learned if any, and the next step, with only `kind` and `reason` in `fields`. A log file or other handler gets the whole line.

### QTE-PACING-LIMIT

- Raised as: `qte_sdk.pacing.PacingLimit`, a `RuntimeError`, with `limit` (`burst`, `sustained` or `new-order`), `retry_after` (seconds) and `type`, from a send through a `Pacer`.
- Cause: The pacer has `on_limit="raise"`, or its wait would pass `max_wait`, and the window named by `limit` had no room for the message. `max_wait` bounds the whole time a send waits for room, counted from the call, so time queued behind earlier sends counts toward it, and a send can raise when its own wait for room is shorter than `max_wait`. A send that finds room when its turn comes is sent, however long it queued. Nothing was sent. In raise mode this also happens to every paced send in the first second after each connect or reconnect: a session starts with the burst window held for a second, since the pacer cannot see what your team sent just before (`limit` is `burst` and `retry_after` at most a second, unless another window holds the send longer, as after a budget reject, when that window's `PacingLimit` or `PacingDraining` is raised with its own `retry_after`).
- Fix: Send it again after `retry_after` seconds, or send less often. A pacer with `on_limit="wait"` (the default) waits instead.

### QTE-PACING-DRAINING

- Raised as: `qte_sdk.pacing.PacingDraining`, a `PacingLimit`, with the same fields.
- Cause: The exchange rejected a message for a budget (1500 to 1502), so the pacer holds that window's messages until the whole window has passed (about a second for the burst cap, a minute for the others), and it has `on_limit="raise"`, or a `max_wait` the hold would pass (counting the time the send already waited, queued behind earlier sends included). Rejected messages count toward the window, so sending sooner only keeps the team locked out. Nothing was sent; `send_mass_cancel` is never held.
- Fix: Send it again after `retry_after` seconds. If rejects keep coming, check the `Budget` values against your team's, and split them between bots that share a team.

### QTE-PACING-REJECTED

- Raised as: a WARNING from the `qte_sdk.pacing` logger, once per hold.
- Cause: With a `Pacer`, the exchange rejected a message with code 1500, 1501 or 1502. The pacer's count was wrong: another bot or the web Trade page used the budget, network jitter bunched messages, or the `Budget` values are higher than your team's. It holds that window's messages until the window has passed. When the pacer had watched a whole window, it also lowers that window's limit, to 80% (`headroom`) of what it counted there but never by more than half in one reject, and the message says "your ... budget may be lower than the N you passed (the pacer counted M in the window)". Each minute with no budget reject raises the limit again by a tenth, and by at least a fortieth of the limit from your values, up to that limit (a send waiting for room goes as soon as a raise makes it), so a reject another bot caused costs a few minutes at a lower limit.
- Fix: If it says the pacer counted far fewer than you passed and no other bot shares your team, check the `Budget` values against your team's for this term (the tables in [Developing your algo](developing-your-algo.md#your-message-budgets)), and split them between bots that share a team.

### QTE-PACING-HOLDING

- Raised as: a WARNING from the `qte_sdk.pacing` logger, once per hold.
- Cause: A `Pacer` with `on_limit="wait"` is about to wait over a second for room in a budget window, so nothing paced is sent meanwhile. A hold after a budget reject is logged as `QTE-PACING-REJECTED` instead.
- Fix: Send less often, or check the `Budget` values. To be told instead of waiting, pass `max_wait` or `on_limit="raise"`.

### QTE-BUDGET-REJECTED

- Raised as: a WARNING from the `qte_sdk.pacing` logger, at most once a minute, from a session without a pacer.
- Cause: The exchange rejected a message with code 1500, 1501 or 1502. Rejected messages count toward the window too, so a bot that keeps sending at or above its budget stays rejected for as long as it keeps sending; a restarted bot sends into the same window.
- Fix: Stop sending new, cancel and amend for a full minute, and wait that minute before restarting your bot. Then pace them: `open_session(pacing=Pacer(budget))`, with `Budget` from `qte_sdk.pacing` and your team's values.

## OPTIONS

### QTE-OPTIONS-CHAIN-HALTED

- Raised as: a WARNING from the `qte_sdk.options` logger, logged by a session at most once a minute for each underlying; each session keeps its own minute, and a `ReconnectingSession` one across its connections.
- Cause: A Director has halted an options chain, every contract on one underlying, before the underlying's split ex-date in the Winter term. The exchange rejected your `new` or `amend` in it with the reason `CONTRACT_HALTED` (1305), or cancelled your resting order in it with an `order_cancelled` whose reason is `CHAIN_HALTED` (1818): a halt's start cancels every order resting in the chain. While the halt lasts the contracts' books carry `trading_state` `OPTION_HALTED`: no wall, no matching, no new order or amend accepted, cancels accepted, and the mark and Greeks still publish. The underlying stock is not halted. The halt lasts until the Director ends it, and its end restores no order. The message names the instrument and underlying: from the `order_cancelled` for a cancel, and for a reject, which names no instrument, from the `new` or `amend` the session sent with the reject's `request_ref`. A reject of an order sent some other way, one without a `request_ref`, or one whose `request_ref` was sent again for another contract before every send of it was answered says "an option contract" and "its underlying", and such rejects share one minute. A session cannot tell another connection's order from its own: if another of your team's connections sends the same `request_ref` for a contract on another underlying, the message can name that underlying instead. Use a `request_ref` on one of your team's connections only while its order awaits an answer (the default, a fresh one for each order, does).
- Fix: Do not send a `new` or `amend` in that chain again until `qte_sdk.options.trading_state` of the contract's latest book is no longer `OPTION_HALTED`; cancels still apply. Then place again only what the new state allows: any order at `OPTION_TRADING`, only orders that reduce your position at `OPTION_REDUCING_ONLY`, and none at `OPTION_SUSPENDED`. Retrying sooner is rejected again and counts toward your message budget.

## Retired codes

None yet. A retired code is listed as `- QTE-AREA-WHAT: retired in vX.Y.Z; use QTE-...`.
