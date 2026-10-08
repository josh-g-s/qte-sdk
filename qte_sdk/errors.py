"""The codes on the SDK's warnings and errors, and the messages they carry.

An SDK exception or warning with a code, such as `QTE-TOKEN-MISSING`, has it in its
`code` attribute, and its message has one shape:

    <CODE>: <what happened>. <why it matters>. <next step>.

so the last line of a traceback names the code and what to do. A message logged at WARNING
carries its code as the record's `code` too. Codes never change once released; a retired
code is listed in `RETIRED` and never reused. docs/errors.md lists every code, with its
cause and fix, and the exit codes of the SDK's commands.

Catch `QteError` for any SDK exception that has a code, or `QteWarning` for any such
warning. Each exception also keeps the base class it had before codes were added, such as
`ValueError` or `TimeoutError`, so existing `except` clauses keep working.

No message ever holds the token. This module imports nothing from the rest of the SDK, so
every module can import it.
"""

import os
import string
import unicodedata
from collections.abc import Callable
from typing import Any, NamedTuple

__all__ = [
    "CODES",
    "RETIRED",
    "Entry",
    "QteError",
    "QteWarning",
    "render",
]

# TOKEN
TOKEN_MISSING = "QTE-TOKEN-MISSING"
TOKEN_FILE_UNREADABLE = "QTE-TOKEN-FILE-UNREADABLE"
TOKEN_SHARED = "QTE-TOKEN-SHARED"
TOKEN_UNCHECKED = "QTE-TOKEN-UNCHECKED"
TOKEN_MALFORMED = "QTE-TOKEN-MALFORMED"
TOKEN_NO_TERMINAL = "QTE-TOKEN-NO-TERMINAL"
TOKEN_SET_PATH = "QTE-TOKEN-SET-PATH"
TOKEN_SET_WRITE = "QTE-TOKEN-SET-WRITE"
# ADDRESS
ADDRESS_MISSING = "QTE-ADDRESS-MISSING"
ADDRESS_INVALID = "QTE-ADDRESS-INVALID"
ADDRESS_SHARED = "QTE-ADDRESS-SHARED"
ADDRESS_UNCHECKED = "QTE-ADDRESS-UNCHECKED"
# DOTENV
DOTENV_TRACKED = "QTE-DOTENV-TRACKED"
DOTENV_NOT_IGNORED = "QTE-DOTENV-NOT-IGNORED"
DOTENV_GIT_UNKNOWN = "QTE-DOTENV-GIT-UNKNOWN"
DOTENV_UNREADABLE = "QTE-DOTENV-UNREADABLE"
DOTENV_INVALID = "QTE-DOTENV-INVALID"
# SESSION
SESSION_REJECTED = "QTE-SESSION-REJECTED"
SESSION_VERSION_MISMATCH = "QTE-SESSION-VERSION-MISMATCH"
SESSION_NOT_ACKNOWLEDGED = "QTE-SESSION-NOT-ACKNOWLEDGED"
SESSION_AUTH_NOT_SENT = "QTE-SESSION-AUTH-NOT-SENT"
SESSION_TIMEOUT = "QTE-SESSION-TIMEOUT"
SESSION_RESUME_REJECTED = "QTE-SESSION-RESUME-REJECTED"
SESSION_RESUME_NOT_ACKNOWLEDGED = "QTE-SESSION-RESUME-NOT-ACKNOWLEDGED"
SESSION_RESUME_TIMEOUT = "QTE-SESSION-RESUME-TIMEOUT"
# CONNECT
CONNECT_HANDSHAKE_FAILED = "QTE-CONNECT-HANDSHAKE-FAILED"
CONNECT_LIVENESS_TIMEOUT = "QTE-CONNECT-LIVENESS-TIMEOUT"
CONNECT_NO_SESSION = "QTE-CONNECT-NO-SESSION"
# HISTORY
HISTORY_ADDRESS_MISSING = "QTE-HISTORY-ADDRESS-MISSING"
HISTORY_ADDRESS_INVALID = "QTE-HISTORY-ADDRESS-INVALID"
HISTORY_PENDING = "QTE-HISTORY-PENDING"
HISTORY_RATE_LIMITED = "QTE-HISTORY-RATE-LIMITED"
HISTORY_UNAVAILABLE = "QTE-HISTORY-UNAVAILABLE"
HISTORY_NOT_CLOSED = "QTE-HISTORY-NOT-CLOSED"
HISTORY_NOT_IMPLEMENTED = "QTE-HISTORY-NOT-IMPLEMENTED"
HISTORY_REQUEST_REJECTED = "QTE-HISTORY-REQUEST-REJECTED"
HISTORY_UNAUTHENTICATED = "QTE-HISTORY-UNAUTHENTICATED"
HISTORY_FORBIDDEN = "QTE-HISTORY-FORBIDDEN"
HISTORY_INTERRUPTED = "QTE-HISTORY-INTERRUPTED"
HISTORY_CHANGED = "QTE-HISTORY-CHANGED"
HISTORY_CORRUPT = "QTE-HISTORY-CORRUPT"
HISTORY_BAD_RESPONSE = "QTE-HISTORY-BAD-RESPONSE"
HISTORY_UNEXPECTED_STATUS = "QTE-HISTORY-UNEXPECTED-STATUS"
HISTORY_REQUEST_FAILED = "QTE-HISTORY-REQUEST-FAILED"
# REPLAY
REPLAY_OUT_OF_ORDER = "QTE-REPLAY-OUT-OF-ORDER"
# UPDATE: the same value as `qte_sdk.update.UPDATE_AVAILABLE`.
UPDATE_AVAILABLE = "QTE-UPDATE-AVAILABLE"


class Entry(NamedTuple):
    """One code's message and documentation. `what`, `why` and `next_step` are
    `str.format` templates for the three parts of the message; `cause` and `fix` are the
    one-line summaries docs/errors.md expands."""

    what: str
    why: str
    next_step: str
    cause: str
    fix: str


_TOKEN_CHECK = "python -m qte_sdk.token check"
_TOKEN_SET = "python -m qte_sdk.token set"
_PROFILE = "a folder under your user profile (%USERPROFILE%), which is private by default"

CODES: dict[str, Entry] = {
    TOKEN_MISSING: Entry(
        "no token was found in {where}",
        "The SDK cannot authenticate without your team token",
        f"Run {_TOKEN_SET} in your project folder, or set QTE_TOKEN_FILE to a file that holds it",
        "No token was passed, QTE_TOKEN and QTE_TOKEN_FILE are unset, and ./.env sets no "
        "QTE_TOKEN; or `token set` was given an empty token",
        f"Run {_TOKEN_SET}, or set QTE_TOKEN_FILE",
    ),
    TOKEN_FILE_UNREADABLE: Entry(
        "QTE_TOKEN_FILE names a file that {problem}",
        "While QTE_TOKEN_FILE is set the SDK does not try ./.env, so it has no token",
        f"Fix that file or unset QTE_TOKEN_FILE, then run {_TOKEN_CHECK}",
        "The file QTE_TOKEN_FILE names cannot be read, is not UTF-8, or is empty",
        "Fix the file or unset QTE_TOKEN_FILE",
    ),
    TOKEN_SHARED: Entry(
        "./.env holds QTE_TOKEN but other users of this computer can read it",
        "Anyone who reads it can trade as your team, so the SDK will not use the file",
        f"Run chmod 600 .env, then {_TOKEN_CHECK}",
        "Other users can read the file that holds the token (on Windows: may read, change "
        "or replace it, or another account owns it)",
        f"chmod 600 .env on macOS or Linux; on Windows, keep the file in {_PROFILE}",
    ),
    TOKEN_UNCHECKED: Entry(
        "the access list of {path}, which holds your token, could not be fully checked",
        "Other users of this computer might be able to read or replace it",
        f"Keep the file in {_PROFILE}, and run icacls on it to see who can open it",
        "On Windows, the check could not see who may open the file that holds the token",
        f"Keep the file in {_PROFILE}",
    ),
    TOKEN_MALFORMED: Entry(
        "the token has a character no token has, such as a space, a quote, a control "
        "character or a letter outside ASCII",
        "It was probably copied wrongly, and would be refused",
        f"Copy it again and run {_TOKEN_SET}",
        "The token entered, or the one HistoryClient was given, holds a character no token has",
        "Copy the token again",
    ),
    TOKEN_NO_TERMINAL: Entry(
        f"{_TOKEN_SET} needs a terminal to read the token without showing it",
        "Read from a pipe or a redirected input, the token could be echoed or recorded",
        "Run the command yourself in an ordinary terminal window, without redirecting its input",
        "`token set` was run without a terminal, or echo could not be turned off",
        "Run it in an ordinary terminal window",
    ),
    TOKEN_SET_PATH: Entry(
        "{path} {problem}, so nothing was changed",
        "set writes only to a regular UTF-8 file it can replace in one step",
        "{fix}",
        "`token set` was pointed at a link, a folder, a .gitignore, or a file it cannot rewrite",
        "Edit the file by hand, or choose another path with --file PATH",
    ),
    TOKEN_SET_WRITE: Entry(
        "could not {action} {path} ({strerror})",
        "No partial copy of the token was left behind",
        "Check the folder's permissions and free space, then run the command again",
        "`token set` could not read, create or write a file or folder",
        "Fix the folder's permissions or free space",
    ),
    ADDRESS_MISSING: Entry(
        "no exchange address was found in {where}",
        "The SDK cannot connect without it",
        f"Run {_TOKEN_SET} and enter the address the course team gave you, or set QTE_URL",
        "No address was passed, QTE_URL is unset, and ./.env sets no QTE_URL",
        f"Run {_TOKEN_SET}, or set QTE_URL",
    ),
    ADDRESS_INVALID: Entry(
        "the exchange address from {source} {problem}",
        "The SDK cannot connect to it safely",
        f"Set QTE_URL to the wss:// address the course team gave you, for example with "
        f"{_TOKEN_SET}",
        "The address does not start with ws:// or wss://, or is a plain ws:// address for "
        "another computer",
        "Use the wss:// address you were given",
    ),
    ADDRESS_SHARED: Entry(
        "{path} sets QTE_URL, the exchange address, and other users may change or replace it",
        "They could point it at a server of their own and capture your token when you next connect",
        f"Keep the file in {_PROFILE}, and run icacls on it to see who can change it",
        "On Windows, a broad group may change or replace the .env that sets QTE_URL, or "
        "another account owns it",
        f"Keep the file in {_PROFILE}",
    ),
    ADDRESS_UNCHECKED: Entry(
        "{path} sets QTE_URL, the exchange address, but it could not be fully checked",
        "Other users of this computer might be able to change it",
        f"Keep the file in {_PROFILE}, and run icacls on it to see who can change it",
        "On Windows, the check could not see who may change the .env that sets QTE_URL",
        f"Keep the file in {_PROFILE}",
    ),
    DOTENV_TRACKED: Entry(
        "git tracks {path}, so your token in it would be committed",
        "Adding it to .gitignore does not stop that",
        "Run {command}, add {name} to .gitignore and commit",
        "git tracks the file that holds, or would hold, the token",
        "git rm --cached the file, add it to .gitignore and commit",
    ),
    DOTENV_NOT_IGNORED: Entry(
        "{path} is inside a git working tree and git does not ignore it",
        "It could be committed with your token",
        "Add {name} to .gitignore",
        "The .env is in a git working tree and is not in .gitignore",
        "Add .env to .gitignore",
    ),
    DOTENV_GIT_UNKNOWN: Entry(
        "{path} is inside a git repository, but git could not say whether it tracks or "
        "ignores it (is git installed and on your PATH?)",
        "The token could be committed without a warning",
        "Install git and run the command again, or make sure {name} is in .gitignore and "
        "not tracked",
        "git is missing or failed, so the .env could not be checked",
        "Install git, or make sure .env is in .gitignore",
    ),
    DOTENV_UNREADABLE: Entry(
        "./.env {problem}, so the SDK has no {name}",
        "Nothing in the file is used until it can be read",
        "Make it a regular UTF-8 text file under 64 KiB that you can read, or recreate it "
        f"with {_TOKEN_SET}",
        "The .env cannot be read, is not a regular file, is over 64 KiB or is not UTF-8",
        f"Fix the file, or recreate it with {_TOKEN_SET}",
    ),
    DOTENV_INVALID: Entry(
        "./.env line {line}: {name} {problem}",
        "The SDK does not use a value it cannot parse",
        "Write it as {name}=value on one line, unquoted with no spaces or in single quotes, "
        f"or recreate the file with {_TOKEN_SET}",
        "A QTE_TOKEN or QTE_URL line in the .env cannot be parsed",
        "Fix that line",
    ),
    SESSION_REJECTED: Entry(
        "the exchange refused the session ({reason})",
        "It will not serve this session, and trying again does not help until the cause is fixed",
        "{step}",
        "The exchange sent session_reject, or a reject of auth",
        "Depends on the reason; see the table in docs/errors.md",
    ),
    SESSION_VERSION_MISMATCH: Entry(
        "the exchange does not serve the contract version this SDK sends ({reason})",
        "Nothing sent on this session can work",
        "Run python -m qte_sdk.update and the command it prints",
        "The SDK is older or newer than the contract versions the exchange serves",
        "Update the SDK",
    ),
    SESSION_NOT_ACKNOWLEDGED: Entry(
        "{what}",
        "No session was opened",
        "Try again in a few seconds (a ReconnectingSession does); if it keeps happening, "
        "run python examples/smoke_test.py and send its output to the course team",
        "The connection ended, or session_ack could not be read, before the session was "
        "acknowledged",
        "Try again",
    ),
    SESSION_AUTH_NOT_SENT: Entry(
        "{what}",
        "Trying again fails the same way",
        "Check the options passed to open_session, such as contract_version, and leave "
        "them unset to use the SDK's own",
        "The auth message could not be encoded or sent, such as for a bad contract_version",
        "Fix the options passed to open_session",
    ),
    SESSION_TIMEOUT: Entry(
        "the session was not acknowledged within {seconds} s",
        "The exchange or the network may be slow or down",
        "Try again, raise ack_timeout, or run python examples/smoke_test.py to check the setup",
        "No session_ack arrived within ack_timeout",
        "Try again, or raise ack_timeout",
    ),
    SESSION_RESUME_REJECTED: Entry(
        "the exchange refused the resume ({reason})",
        "The session goes on, but no report was replayed and no snapshot sent",
        "Treat the resting orders you track as unknown until a later resume completes, and "
        "check your positions with send_account_query before you trade",
        "The exchange answered resume with a reject",
        "Check your orders and positions",
    ),
    SESSION_RESUME_NOT_ACKNOWLEDGED: Entry(
        "{what}",
        "Reports sent while you were away were not replayed",
        "Open a new session and resume from the same last_report_seq",
        "The connection ended, or resume_ack could not be read, before the resume was answered",
        "Resume again on a new session",
    ),
    SESSION_RESUME_TIMEOUT: Entry(
        "the exchange did not answer resume within {seconds} s",
        "Reports sent while you were away were not replayed",
        "Open a new session and resume again, or raise timeout",
        "No answer to resume arrived within its timeout",
        "Resume again on a new session",
    ),
    CONNECT_HANDSHAKE_FAILED: Entry(
        "the opening handshake failed ({kind}{status}); details withheld",
        "{why}",
        "{step}",
        "The WebSocket handshake failed, such as for a wrong address or a refused token",
        "Depends on the HTTP status; see docs/errors.md",
    ),
    CONNECT_LIVENESS_TIMEOUT: Entry(
        "no message from the exchange for {seconds} s, so the link was presumed dead and dropped",
        "Orders in flight may or may not have reached the exchange",
        "Reconnect (a ReconnectingSession does this by itself) and check your resting "
        "orders and positions",
        "Nothing arrived for liveness_timeout seconds after the first heartbeat",
        "Reconnect",
    ),
    CONNECT_NO_SESSION: Entry(
        "no session is up, so {type} was not sent",
        "Nothing is queued to send later",
        "Wait for the next Connected event, then send it again",
        "ReconnectingSession.send was called while no session was up",
        "Send it again after the next Connected event",
    ),
    HISTORY_ADDRESS_MISSING: Entry(
        "no history service address was given",
        "HistoryClient has no default address, and does not read one from .env",
        "Pass url=, or set QTE_HISTORY_URL in the environment to the https:// address the "
        "course team gave you",
        "HistoryClient was given no url=, and QTE_HISTORY_URL is unset or empty",
        "Set QTE_HISTORY_URL",
    ),
    HISTORY_ADDRESS_INVALID: Entry(
        "the history service address {problem}",
        "The client will not send your token there",
        "Set QTE_HISTORY_URL, or pass url=, to the https:// address the course team gave you",
        "The address is not an https:// URL (http:// only to this computer), or holds "
        "credentials, a query, a fragment or a port that is not a number",
        "Use the https:// address you were given",
    ),
    HISTORY_PENDING: Entry(
        "{what}",
        "It will be ready, but waiting longer would have passed max_wait or max_retries",
        "Ask again later, or raise max_wait",
        "The session has closed but its data is not ready yet (HTTP 202)",
        "Ask again later",
    ),
    HISTORY_RATE_LIMITED: Entry(
        "{what}",
        "The service is limiting your team's requests",
        "Wait as long as retry_after says before the next request, and fetch more per "
        "request with fetch_range",
        "Too many requests for this token (HTTP 429), past max_wait or max_retries",
        "Wait, then make fewer, larger requests",
    ),
    HISTORY_UNAVAILABLE: Entry(
        "{what}",
        "Asking again will not change that",
        "Check the date, instrument and channel against the calendar and the instruments "
        "the exchange lists",
        "A date before the service's coverage, a day with no session, or an instrument or "
        "channel the service does not know (HTTP 404)",
        "Ask for data that exists",
    ),
    HISTORY_NOT_CLOSED: Entry(
        "{what}",
        "Nothing is served for a session before its close",
        "Ask again after the session's close",
        "The session is running now, or lies in the future (HTTP 409)",
        "Ask again after the close",
    ),
    HISTORY_NOT_IMPLEMENTED: Entry(
        "{what}",
        "A later release of the service may serve it",
        "Use another endpoint, or ask again after the service is updated",
        "The service does not serve this endpoint yet (HTTP 501)",
        "Use another endpoint",
    ),
    HISTORY_REQUEST_REJECTED: Entry(
        "{what}",
        "The same request fails the same way",
        "Fix the arguments: dates as YYYY-MM-DD, to_date not before from_date, and a range "
        "the service allows",
        "The service found the request malformed (HTTP 400)",
        "Fix the arguments",
    ),
    HISTORY_UNAUTHENTICATED: Entry(
        "{what}",
        "The service uses your team token",
        f"Run {_TOKEN_CHECK}; if the token is found and still refused, ask the Head of "
        "Technology for a new one",
        "The service did not recognise the token (HTTP 401)",
        f"Run {_TOKEN_CHECK}",
    ),
    HISTORY_FORBIDDEN: Entry(
        "{what}",
        "The service serves only what your team may see",
        "Ask the course team whether this data is open to your team",
        "The token may not read this data (HTTP 403)",
        "Ask the course team",
    ),
    HISTORY_INTERRUPTED: Entry(
        "{what}",
        "The messages already yielded are incomplete and were not checked against the digest",
        "Discard them and fetch again, or raise max_resumes",
        "The connection dropped during a download more often than max_resumes allows",
        "Fetch again",
    ),
    HISTORY_CHANGED: Entry(
        "{what}",
        "What was already yielded cannot be continued",
        "Discard it and fetch again from the start",
        "A dropped download could not be resumed, since the service now serves different "
        "data for the request",
        "Fetch again from the start",
    ),
    HISTORY_CORRUPT: Entry(
        "{what}",
        "The messages already yielded may be wrong",
        "Discard them and fetch again; if it happens again, tell the course team",
        "What arrived does not match the length or SHA-256 digest the service stated",
        "Fetch again",
    ),
    HISTORY_BAD_RESPONSE: Entry(
        "{what}",
        "The client cannot check or resume such a response, so it stopped",
        "Check that QTE_HISTORY_URL is the history service's address; if it is, tell the "
        "course team",
        "The response is not one the history service sends: not uncompressed NDJSON, no "
        "identity ETag, no readable manifest, or a resume that is not the rest of the data",
        "Check QTE_HISTORY_URL",
    ),
    HISTORY_UNEXPECTED_STATUS: Entry(
        "{what}",
        "The client does not know what this status means",
        "Try again later; if it happens again, tell the course team the status",
        "The service answered with an HTTP status this SDK does not know",
        "Try again later",
    ),
    HISTORY_REQUEST_FAILED: Entry(
        "{what}",
        "The request failed before the service gave an answer the client could use",
        "Try again; if it happens again, check your network and QTE_HISTORY_URL",
        "Sending the request or reading the answer failed with an error other than a network error",
        "Try again",
    ),
    REPLAY_OUT_OF_ORDER: Entry(
        "the {where} goes back in time: {what}",
        "The replay cannot merge it, so it stopped and closed every download",
        "Replay without that stream, and tell the course team its date and name",
        "A downloaded stream holds a message earlier than the one before it",
        "Replay without that stream",
    ),
    UPDATE_AVAILABLE: Entry(
        "qte-sdk {version} is behind {latest}",
        "A newer release is out",
        "Update with {command}",
        "A newer release of qte-sdk is out",
        "Run the command python -m qte_sdk.update prints",
    ),
}

# Codes retired, with the release that retired them. None yet.
RETIRED: dict[str, str] = {}

# The next step for each `reason_name` of a QTE-SESSION-REJECTED, and the default.
SESSION_REJECTED_STEPS: dict[str, str] = {
    "NOT_AUTHENTICATED": (
        f"Run {_TOKEN_CHECK}; if the token is found and still refused, ask the Head of "
        "Technology for a new one"
    ),
    "TEAM_DISABLED": "Ask the Head of Technology about your team's access",
    "NO_MARKET_ACCESS": "Ask the Head of Technology about your team's access",
    "EXCHANGE_OUTAGE": "Wait for the exchange's outage to end, then connect again",
}
SESSION_REJECTED_DEFAULT_STEP = (
    f"Look up {{reason_name}} under {SESSION_REJECTED} in docs/errors.md"
)

# Why and the next step for a QTE-CONNECT-HANDSHAKE-FAILED, by kind of HTTP status.
HANDSHAKE_STEPS: dict[str, tuple[str, str]] = {
    "refused": (
        "The exchange did not accept the connection",
        f"Run {_TOKEN_CHECK}; if the token is found and still refused, ask the Head of "
        "Technology for a new one",
    ),
    "not-found": (
        "Nothing is served at that address",
        "Check that QTE_URL is the exchange's wss:// address, path included",
    ),
    "busy": (
        "The exchange is busy or down",
        "Try again in a minute (a ReconnectingSession does)",
    ),
    "other": (
        "The address may not be the exchange's",
        "Check that QTE_URL is the exchange's wss:// address",
    ),
}

# The only field names a template may use. None names the token.
FIELDS = frozenset(
    {
        "action",
        "command",
        "fix",
        "kind",
        "latest",
        "line",
        "name",
        "path",
        "problem",
        "reason",
        "reason_name",
        "seconds",
        "source",
        "status",
        "step",
        "strerror",
        "type",
        "version",
        "what",
        "where",
        "why",
    }
)

_MAX_FIELD = 300


def plain(text: object) -> str:
    """`text` as it may be shown in one line of a message: no line breaks, no control or
    formatting characters, no full stop at the end, and at most 300 characters. Used for
    text from the exchange or the system, such as a rejection's detail."""
    flat = flatten(text).rstrip(". ")
    if len(flat) > _MAX_FIELD:
        flat = flat[: _MAX_FIELD - 3].rstrip() + "..."
    return flat


def flatten(text: object) -> str:
    """`text` with each run of whitespace made one space and every control or formatting
    character removed: how `plain` shows it, before it is cut short."""
    flat = " ".join(str(text).split())
    flat = "".join(c for c in flat if not unicodedata.category(c).startswith("C"))
    return " ".join(flat.split())


# Control characters, and the line and paragraph separators: each would break a line.
_BREAKS = frozenset({"Cc", "Zl", "Zp"})


def one_line(text: str) -> str:
    """`text` with each line break, tab or other control character, and each line or
    paragraph separator, made a space: how a message shows a field such as a path."""
    return "".join(" " if unicodedata.category(c) in _BREAKS else c for c in text)


# What a field is shown as when `withhold` says it holds the token.
WITHHELD = "(withheld: it holds the token)"


def template_fields(code: str) -> set[str]:
    """The field names `code`'s templates use."""
    entry = CODES[code]
    return {
        name
        for part in (entry.what, entry.why, entry.next_step)
        for _, name, _, _ in string.Formatter().parse(part)
        if name
    }


def summary(code: str, /, *, withhold: Callable[[str], bool] | None = None, **fields: Any) -> str:
    """`code`'s message without the code: `<what>. <why>. <next step>.` Never raises: if a
    field is missing, or the code is unknown, the code's cause, or a fixed sentence, is
    given instead, since a `__str__` that raises prints only `<exception str() failed>`.

    `withhold`, if given, is asked about each text field, as given and as the message would
    show it; a field it says yes to (one that holds the token, say) is shown as `WITHHELD`."""
    try:
        entry = CODES[code]
        fields = {name: _shown(value, withhold) for name, value in fields.items()}
        parts = [part.format(**fields) for part in (entry.what, entry.why, entry.next_step)]
        parts = [part.strip().rstrip(".") for part in parts]
        return " ".join(f"{part}." for part in parts if part)
    except Exception:
        entry = CODES.get(code)
        cause = entry.cause if entry is not None else "an error with no registered message"
        return f"{cause.rstrip('.')}. See {code} in docs/errors.md."


def _shown(value: object, withhold: Callable[[str], bool] | None) -> object:
    """A field as the message shows it: text in one line, whatever it holds (a path may
    hold a line break), or `WITHHELD` if `withhold` says so of it in either form."""
    if not isinstance(value, str | os.PathLike):
        return value
    text = str(value)
    shown = one_line(text)
    if withhold is not None and (withhold(text) or withhold(shown)):
        return WITHHELD
    return shown


def withheld(withhold: Callable[[str], bool], /, **fields: Any) -> dict[str, Any]:
    """`fields` as a message shows them, each one `withhold` says yes to, as given or in
    one line, replaced by `WITHHELD`. For an exception's `fields`, which its message renders
    later with nothing to ask: the caller holds the token now, and the exception must not."""
    return {name: _shown(value, withhold) for name, value in fields.items()}


def render(code: str, /, *, withhold: Callable[[str], bool] | None = None, **fields: Any) -> str:
    """The message for `code` with `fields` filled in: `<CODE>: <what>. <why>. <next>.`
    `withhold` is as for `summary`."""
    return f"{code}: {summary(code, withhold=withhold, **fields)}"


class Problem(str):
    """What is wrong, as text, carrying the code and fields of the message to raise for
    it. A `str`, so code that treats a problem as text keeps working."""

    code: str
    fields: dict[str, Any]

    def __new__(cls, text: str, code: str, **fields: Any) -> "Problem":
        problem = super().__new__(cls, text)
        problem.code = code
        problem.fields = fields
        return problem


class _Coded:
    """What `QteError` and `QteWarning` share: `code`, the class's default unless an
    instance sets its own, and `fields`, which fill the code's templates. An instance the
    SDK makes has `fields` (perhaps empty) and shows the full coded message; one a program
    makes itself, with no `fields`, shows the code and its own text."""

    code: str | None = None
    fields: dict[str, Any] | None = None

    def __str__(self) -> str:
        text = super().__str__()  # type: ignore[misc]
        code = self.code
        if code is None:
            return text
        if self.fields is not None:
            return render(code, **self.fields)
        return f"{code}: {text}"


class QteError(_Coded, Exception):
    """The base of every SDK exception that has a code. Each also keeps its own older
    base, such as `ValueError`, so an `except` clause written for that still catches it.
    `code`, if given, replaces the class's default; `fields` fill its templates."""

    def __init__(
        self, *args: object, code: str | None = None, fields: dict[str, Any] | None = None
    ) -> None:
        super().__init__(*args)
        if code is not None:
            self.code = code
        if fields is not None:
            self.fields = fields


class QteWarning(_Coded, UserWarning):
    """The base of every SDK warning that has a code. Its arguments are as for any warning,
    the first being the text without the code; `code`, if given, replaces the class's
    default."""

    def __init__(
        self,
        *args: object,
        code: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(*args)
        if code is not None:
            self.code = code
        if fields is not None:
            self.fields = fields
