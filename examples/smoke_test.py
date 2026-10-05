"""Check a setup end to end and say plainly what works. Sends no orders unless asked.

Set QTE_URL and QTE_TOKEN first, in ./.env or the environment (see docs/quickstart.md),
then, from a clone of this repository:

    python examples/smoke_test.py --instruments AAPL MSFT

Run it first, before any other program, and again whenever something seems wrong. It runs
these checks in order and prints one line for each, PASS, FAIL or SKIP with a one-line
reason, then a summary:

    sdk-version      whether the installed SDK is the latest release, as
                     `python -m qte_sdk.update` reports it, before anything else and even
                     with no token. A FAIL when a newer release is out, or when the SDK is
                     too old to have the check, with the command that updates; a PASS that
                     notes any newer commits on main; a SKIP when it cannot tell (a local
                     or editable install, say, or GitHub could not be reached).
    token, address   where the SDK finds your token and the exchange address, as
                     `python -m qte_sdk.token check` reports them. Neither is shown.
    dotenv           only when git does not ignore the .env the SDK read: a FAIL, since
                     the token could be committed.
    connect          opens a session and names the team it authenticated as.
    calendar         reads the exchange's calendar: the next open and the last close.
    instruments      the exchange's table of instruments: how many it lists, how many your
                     team may trade now, its option underlyings, and any of --instruments
                     that is not listed or not open to your team. A SKIP from an exchange
                     that does not send the table.
    session-state    subscribes to each instrument and watches for --seconds: the market
                     session's state, OPEN during a session and CLOSED outside one.
    market:<name>    during a session, the instrument's best bid and ask and what arrived.
                     One with no valid quote yet has no book, so the script waits up to
                     --book-wait for its first, and a SKIP says if none came. Outside a
                     session, its official close (a SKIP for an instrument with none yet).
                     An instrument the exchange does not know is a FAIL.
    account          asks for your team's account. No reply, or a refusal as a message
                     type the exchange does not know, is a SKIP: it does not serve the
                     query yet. Any other refusal is a FAIL. No figures are printed.
    test-order       only with --place-test-order (see below).
    feed             whether any message was missed or could not be decoded.
    heartbeat        whether the exchange's heartbeats arrived, and about how far apart;
                     a SKIP if none came, since the interval may be longer than the run.
    history:<name>   the start of the last closed session's books from the history service, when
                     QTE_HISTORY_URL is set (from the environment only, never .env).

It exits with status 0 when no check failed, 1 when one did, and 2 when it found no token
or no usable address, and so could not connect. The output never shows the token or any
account figure; the one exception is a fill of the test order, whose quantity and price
it names so you know the position your team then holds.

The test order. With --place-test-order --strat-id ID --tick DOLLARS, and only while the
market session is OPEN and no exchange outage is in force, it places one limit buy of one
share on the first instrument whose book is two-sided and LIVE, waits until the exchange
reports it resting, then cancels exactly that price level and waits for the exchange to
confirm the cancel. It never sends a mass cancel, which would cancel every order your
team has, other strategies' included. In a scored session it places nothing unless you
also pass --allow-scored: there the order is a real order of your team like any other.

Its price is one tick above the wall's best bid, the lowest at which a buy can rest: your
orders rest only strictly inside the band between the wall's best bid and best ask, and
one at the wall's own price or beyond it is not left resting (`order_cancelled` with
REMAINDER_OUTSIDE_BAND). You give the tick with --tick: the exchange's instruments table
carries each instrument's tick, but this script also runs against an exchange without one.
The price must be at least three ticks below every ask, or the check is a SKIP: try
another instrument.

The order can fill. There is no post-only order, so if the asks fall to its price during
the order delay, or a seller trades with it while it rests, it trades. A fill makes the
check FAIL, and a line on stderr names the position your team then holds. If the wall's
bid rises to the price during the delay, nothing rests and the check is a SKIP: run it
again. A cancel names a price level, not an order, and acts on whichever of your team's
orders rests there when it is applied, so run this on an instrument your team is not
otherwise trading at that price, with a strategy nothing else is using: if anything else
acts on that strategy's buy orders on the instrument during the test (an amend of the test
order, a fill at another price, a cancel of it), the test order can no longer be told
apart from them, and the check fails with a warning. If the script cannot confirm that
the order is gone, the check fails and names the level where it may still rest, so you
can cancel it yourself. If it is interrupted (Ctrl+C) while the order may rest, it first
tries, for a few seconds, to cancel that level.

Timing. The exchange holds every order message for its order delay before applying it. The
delay, the minimum time an order must rest before it may be cancelled, the price collar
and the message budgets are set by the exchange, so nothing here assumes their values: the
script cancels once the exchange reports the order resting, and if a cancel is rejected
MIN_REST_VIOLATION (or for a message budget) it sends it again after the exchange's next
market-data grid point, then after two more, four more and so on, rather than after a
fixed sleep. --seconds bounds each wait for the exchange, except the wait during a session
for an instrument's first book, which --book-wait bounds (never less than --seconds). The
history read is too, except that a slow address lookup, or a history host name that
resolves to several addresses, none reachable, can hold the exit for longer.

Stopping. Ctrl+C, SIGTERM (a `kill`, an editor's stop button, a time limit) and SIGHUP (a
closed terminal) all stop the run the same way: if the test order may rest, the script
first tries for a few seconds to cancel its level, and warns if it cannot confirm that. It
does so even when the terminal is gone and nothing it writes can be seen. A further SIGTERM
or SIGHUP stops the wait once that cancel is out; a second Ctrl+C stops at once, even before
it is, and the warning names the level. A signal that is already ignored when it starts (as
under `nohup`) is left ignored. SIGKILL (`kill -9`)
cannot be caught, so after one, check your team's orders yourself.
"""

import argparse
import asyncio
import contextlib
import ipaddress
import math
import os
import re
import signal
import sys
import time
import warnings
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, NoReturn
from urllib.parse import urlsplit

from google.protobuf.message import Message
from websockets.exceptions import ConnectionClosed

from qte_sdk.account import AccountState, ValuationBasis, is_account_state, send_account_query
from qte_sdk.books import LatestBooks
from qte_sdk.calendar import Calendar, CalendarSession, next_session, session_open_at
from qte_sdk.connection import (
    ContractVersionMismatch,
    Received,
    ReportGap,
    SessionRejected,
    Unknown,
)
from qte_sdk.contract.v1.common_pb2 import (
    BUY,
    LIMIT,
    RESTING,
    STALE,
    MarketSessionPhase,
    ReasonCodes,
)
from qte_sdk.contract.v1.order_events_pb2 import Accepted, Execution, OrderCancelled, OrderState
from qte_sdk.dotenv import DOTENV_NAME, DotenvNotIgnored, dotenv_path
from qte_sdk.history import (
    HISTORY_URL_ENV_VAR,
    HistoryClient,
    HistoryError,
    HistoryNotImplemented,
    HistoryPending,
)
from qte_sdk.instruments import (
    InstrumentInfo,
    Instruments,
    InstrumentStatus,
    can_trade,
    instruments_by_id,
    tradable_instruments,
)
from qte_sdk.market_data import (
    Book,
    DecodeFailed,
    InstrumentCondition,
    Mark,
    OfficialClose,
    Reject,
    SeqGap,
    SessionState,
    Trades,
    as_market_data,
    subscribe,
)
from qte_sdk.orders import (
    ORDER_EVENT_TYPES,
    is_order_event,
    new_request_ref,
    reason_code_name,
    request_ref_of,
    send_cancel,
    send_new,
)
from qte_sdk.session import (
    TOKEN_FILE_ENV_VAR,
    MissingToken,
    MissingURL,
    Session,
    open_session,
    token_source,
    url_source,
)
from qte_sdk.units import to_datetime, to_decimal, to_micros, to_timedelta

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
NAME_WIDTH = 14

# The test order is one share, the smallest order there is.
TEST_ORDER_SIZE = 1

# The least room, in ticks, between the test order's price and the best ask. There is no
# post-only order, so an ask that falls to the price during the order delay fills it; the
# room makes that less likely, never impossible. A local choice.
MIN_ROOM_TICKS = 3

# The longest wait for the cancel made after an interruption. A local choice.
CLEANUP_SECONDS = 5.0

# The longest wait for the connection to close at the end. A local choice, not a value the
# exchange sets.
CLOSE_SECONDS = 5.0

# How long, during a session, to wait for an instrument's first book: a session's first
# grid point publishes a book only for the instruments that have one, so one with no valid
# quote yet has none until it does. Local choices, capped so the run stays bounded.
DEFAULT_BOOK_WAIT = 60.0
MAX_BOOK_WAIT = 600.0

# Rejects of the test order meaning the exchange is not taking orders just now: nothing was
# placed, and nothing is wrong with the setup.
NOT_TAKING_ORDERS = frozenset(
    {ReasonCodes.MARKET_CLOSED, ReasonCodes.RELEASE_AFTER_CLOSE, ReasonCodes.EXCHANGE_OUTAGE}
)

# Rejects of a cancel that may pass if sent again later. Each leaves the order unchanged.
RETRY_CANCEL = frozenset(
    {
        ReasonCodes.MIN_REST_VIOLATION,
        ReasonCodes.MESSAGE_BUDGET_EXCEEDED,
        ReasonCodes.BURST_CAP_EXCEEDED,
    }
)


class Parser(argparse.ArgumentParser):
    """An argument parser whose errors name an argument but never repeat a value typed for
    it, since a mistake could put a token on the command line (`--token ...`)."""

    # The words typed before any "--": only these can be options. Everything after a "--"
    # is a value, however it looks.
    _option_words: frozenset[str] = frozenset()

    def parse_args(self, args: Any = None, namespace: Any = None) -> Any:
        typed = list(sys.argv[1:] if args is None else args)
        self._option_words = frozenset(typed[: typed.index("--")] if "--" in typed else typed)
        return super().parse_args(typed, namespace)

    def error(self, message: str) -> NoReturn:
        super().error(withhold_values(message, self._option_words))


# A long option's name, as argparse reports one it does not know: short enough that a token
# typed after it is not taken for one.
_OPTION_NAME = re.compile(r"--[a-z][a-z-]{0,23}")

# What argparse says about an option's value that never repeats the value.
_SAFE_COMPLAINTS = frozenset(
    {
        "expected one argument",
        "expected at least one argument",
        "expected at most one argument",
    }
)


def withhold_values(message: str, option_words: frozenset[str] = frozenset()) -> str:
    """`message` with every value typed on the command line left out.

    Only text known to hold no typed value is kept: this script's own messages, the names
    of its own options, and an unknown option's name when it was typed before any "--" and
    looks like one (lowercase letters and hyphens, short), as a token does not. Anything
    else is replaced."""
    prefix = "unrecognized arguments: "
    if message.startswith(prefix):
        names = [
            word.split("=", 1)[0]
            for word in message[len(prefix) :].split()
            if word in option_words and _OPTION_NAME.fullmatch(word.split("=", 1)[0])
        ]
        if not names:
            return "unrecognized arguments (not shown)"
        return f"{prefix}{' '.join(dict.fromkeys(names))} (any values not shown)"
    if message.startswith("argument "):
        # "argument --tick: <complaint>", where the names are this script's own options.
        names, _, complaint = message.partition(": ")
        if complaint in _SAFE_COMPLAINTS:
            return message
        return f"{names}: value not accepted (not shown)"
    if message.startswith(("ambiguous option", "invalid")):
        return "an argument was not accepted (not shown)"
    return message


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = Parser(
        description="Check a setup end to end. Sends no orders unless asked.",
        # Spelled out in full only: an abbreviation such as --pl must never place an order.
        allow_abbrev=False,
    )
    default = os.environ.get("QTE_INSTRUMENT")
    parser.add_argument(
        "--instruments",
        nargs="+",
        metavar="INSTRUMENT",
        default=[default] if default else [],
        help=(
            "instruments to subscribe to, for example --instruments AAPL MSFT "
            "(default: $QTE_INSTRUMENT)"
        ),
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=5.0,
        help=(
            "how long to watch market data, and the longest wait for each reply from the "
            "exchange (default 5)"
        ),
    )
    parser.add_argument(
        "--book-wait",
        type=float,
        default=DEFAULT_BOOK_WAIT,
        help=(
            "during a session, how long after subscribing to wait for an instrument's first "
            "book, since one with no valid quote yet has none; never less than --seconds "
            f"(default {DEFAULT_BOOK_WAIT:g}, at most {MAX_BOOK_WAIT:g})"
        ),
    )
    parser.add_argument(
        "--place-test-order",
        action="store_true",
        help=(
            "during a session, place one passive one-share buy, then cancel it and confirm "
            "the cancel; needs --strat-id and --tick. It is a real order and can fill"
        ),
    )
    parser.add_argument(
        "--strat-id",
        default=os.environ.get("QTE_STRAT_ID"),
        help="a strategy ID registered for your team (default: $QTE_STRAT_ID)",
    )
    parser.add_argument(
        "--tick",
        type=to_micros,  # dollars as text, converted exactly to micro-dollars
        default=None,
        help=(
            "the instruments' tick in dollars, for the test order's price, for example "
            "0.01; needed with --place-test-order, since the exchange does not send it"
        ),
    )
    parser.add_argument(
        "--allow-scored",
        action="store_true",
        help=(
            "let --place-test-order place its order in a scored session too, where it "
            "counts like any other order of your team"
        ),
    )
    args = parser.parse_args(argv)
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("--seconds must be a number of seconds more than 0")
    if not 0 <= args.book_wait <= MAX_BOOK_WAIT:
        parser.error(f"--book-wait must be from 0 to {MAX_BOOK_WAIT:g} seconds")
    if args.tick is not None and args.tick <= 0:
        parser.error("--tick must be more than 0, for example 0.01")
    if args.place_test_order and not args.strat_id:
        parser.error(
            "--place-test-order needs --strat-id (or QTE_STRAT_ID): a strategy ID "
            "registered for your team"
        )
    if args.place_test_order and args.tick is None:
        parser.error(
            "--place-test-order needs --tick, the instruments' tick in dollars, for example "
            "0.01: the exchange does not send it, and the order's price is set from it"
        )
    args.instruments = list(dict.fromkeys(args.instruments))
    return args


class Report:
    """Prints one line per check and counts the outcomes."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def add(self, status: str, name: str, reason: str) -> None:
        self.counts[status] += 1
        print(f"{status}  {name:<{NAME_WIDTH}}  {reason}", flush=True)

    def finish(self) -> int:
        """Print the summary and return the exit status: 1 if any check failed, else 0."""
        failed = self.counts[FAIL]
        print(
            f"summary: {self.counts[PASS]} passed, {failed} failed, {self.counts[SKIP]} skipped",
            flush=True,
        )
        return 1 if failed else 0


def name_of(enum: Any, value: int) -> str:
    """An enum value's name, or its number when this SDK does not know it."""
    try:
        return enum.Name(value)
    except ValueError:
        return str(value)


def reject_text(reject: Reject) -> str:
    """The reject's reason code. Its free-text `reason_detail` is left out: it can name a
    risk limit and the team's figures against it, which this output never shows."""
    return reason_code_name(reject.reason_code)


def missed_reports(event: object) -> bool:
    """Whether `event` means a report about the team's orders may have been missed: a gap in
    the messages or in the team's reports, or one that could not be decoded and may have
    been a report."""
    if isinstance(event, SeqGap | ReportGap):
        return True
    return isinstance(event, DecodeFailed) and (
        event.type is None or event.type in ORDER_EVENT_TYPES
    )


def refuses_unknown_type(event: Received) -> bool:
    """Whether `event` is the reject an exchange sends for a message type it does not know:
    MALFORMED_MESSAGE naming no request type at all (the contract names one whenever the
    exchange knows the type), and not one of the team's order reports. It is how
    `qte_sdk.session` recognises a `resume` from an exchange that does not serve it."""
    message = event.message
    payload = event.payload or {}
    return (
        isinstance(message, Reject)
        and message.reason_code == ReasonCodes.MALFORMED_MESSAGE
        and "request_type" not in payload
        and "requestType" not in payload
        and event.report_seq is None
    )


def best_text(book: Book) -> str:
    """The best bid and ask across the wall and resting orders, and the book's condition."""
    bids = [levels[0] for levels in (book.bid_levels, book.student_bid_levels) if levels]
    asks = [levels[0] for levels in (book.ask_levels, book.student_ask_levels) if levels]
    bid = max(bids, key=lambda level: level.price, default=None)
    ask = min(asks, key=lambda level: level.price, default=None)
    bid_text = f"bid {to_decimal(bid.price)} x {bid.size}" if bid is not None else "no bid"
    ask_text = f"ask {to_decimal(ask.price)} x {ask.size}" if ask is not None else "no ask"
    return f"{bid_text}, {ask_text} ({name_of(InstrumentCondition, book.condition)})"


# The SDK's version

# The command that updates an SDK too old to say it itself.
OLD_SDK_UPDATE = 'pip install --upgrade "git+https://github.com/josh-g-s/qte-sdk"'


def check_sdk_version(report: Report) -> None:
    """Report whether the installed SDK is the latest release. It reads the SDK's
    repository on GitHub, so it is the one check that needs neither the exchange nor the
    token."""
    try:
        from qte_sdk.update import Status, check_for_update
    except ImportError:
        report.add(
            FAIL,
            "sdk-version",
            f"the installed SDK is older than this script and cannot check itself: update "
            f"it with {OLD_SDK_UPDATE}",
        )
        return
    try:
        result = check_for_update()
    except Exception as error:
        report.add(SKIP, "sdk-version", f"cannot tell ({type(error).__name__})")
        return
    status = {Status.CURRENT: PASS, Status.BEHIND: FAIL}.get(result.status, SKIP)
    report.add(status, "sdk-version", result.message)


# Where the token and the address come from


def describe(source: str) -> str:
    if source == DOTENV_NAME:
        return str(dotenv_path())
    if source == TOKEN_FILE_ENV_VAR:
        return f"the file named by {TOKEN_FILE_ENV_VAR}"
    return f"the {source} environment variable"


def address_problem(url: str) -> str | None:
    """What keeps `url` from being used as the exchange address, or None. Never the address
    itself, which a mistake could have filled with the token.

    A plain ws:// address carries the token unencrypted, so it is refused for any host
    but this machine (a test exchange of your own)."""
    if url.startswith("wss://"):
        return None
    if not url.startswith("ws://"):
        return "it does not start with ws:// or wss:// as an exchange address must"
    try:
        host = urlsplit(url).hostname or ""
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if loopback:
        return None
    return (
        "it is a plain ws:// address for a host other than this machine, which would send "
        "your token unencrypted: use the wss:// address you were given"
    )


def check_setup(report: Report) -> tuple[str | None, list[str]]:
    """Report where the SDK finds the token and the address, showing neither. Returns the
    address and what keeps the script from connecting, if anything."""
    problems: list[str] = []
    url = None
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DotenvNotIgnored)
        try:
            source = token_source()
        except MissingToken as error:
            problems.append(str(error))
            report.add(FAIL, "token", f"none usable: {error}")
        else:
            report.add(PASS, "token", f"found in {describe(source)} (not shown)")
        try:
            source, url = url_source()
        except MissingURL as error:
            problems.append(str(error))
            report.add(FAIL, "address", f"none: {error}")
        else:
            # The address itself is not shown: a mistake could have put the token there.
            where = f"set, from {describe(source)} (not shown)"
            shape = address_problem(url)
            if shape is None:
                report.add(PASS, "address", where)
            else:
                problems.append(f"QTE_URL cannot be used: {shape}")
                report.add(FAIL, "address", f"{where}, but {shape}")
    for warning in caught:
        if issubclass(warning.category, DotenvNotIgnored):
            report.add(FAIL, "dotenv", str(warning.message))
            break
    return url, problems


# The calendar


def last_closed(calendar: Calendar, now: int) -> CalendarSession | None:
    closed = [entry for entry in calendar.sessions if entry.close_time <= now]
    return max(closed, key=lambda entry: entry.close_time, default=None)


def time_text(timestamp: int, now: int) -> str:
    """An exchange timestamp as UTC, to the minute, and how long until it from `now`, the
    exchange's own time (never this machine's clock)."""
    try:
        when = to_datetime(timestamp)
        wait = to_timedelta(timestamp - now)
    except ValueError:  # beyond what a datetime holds
        return f"at exchange time {timestamp}"
    minutes = abs(wait) // timedelta(minutes=1)
    hours, minutes = divmod(minutes, 60)
    span = f"{hours} h {minutes} min" if hours else f"{minutes} min"
    return f"{when:%Y-%m-%d %H:%M} UTC, " + (
        f"in {span}" if wait >= timedelta(0) else f"{span} ago"
    )


def check_instruments(report: Report, table: Instruments | None, requested: list[str]) -> None:
    """The exchange's table of instruments, as the session last received it. Never a FAIL:
    an instrument the exchange does not know already fails its market check."""
    if table is None:
        report.add(SKIP, "instruments", "none received: this exchange does not send the table")
        return
    by_id = instruments_by_id(table)
    parts = [
        f"{len(by_id)} listed, {len(tradable_instruments(table))} your team may trade now",
        f"{len(table.option_underlyings)} option underlying(s)",
    ]
    for name in requested:
        info = by_id.get(name)
        if info is None:
            parts.append(f"{name} is not listed")
        elif not can_trade(info):
            parts.append(f"{name} {why_not_tradable(info)}")
    report.add(PASS, "instruments", "; ".join(parts))


def why_not_tradable(info: InstrumentInfo) -> str:
    if not info.tradable:
        return "is not open to your team"
    return f"is {name_of(InstrumentStatus, info.status)}"


def check_calendar(report: Report, calendar: Calendar | None, now: int, seconds: float) -> None:
    if calendar is None:
        report.add(FAIL, "calendar", f"none received within {seconds:g} s of connecting")
        return
    if not calendar.sessions:
        report.add(FAIL, "calendar", "it lists no sessions")
        return
    parts = []
    current = session_open_at(calendar, now)
    if current is not None:
        parts.append(f"session {current.session_date} is scheduled open now")
    upcoming = next_session(calendar, now)
    if upcoming is None:
        parts.append("no later session this term")
    else:
        opens = time_text(upcoming.open_time, now)
        parts.append(f"next open: session {upcoming.session_date}, {opens}")
    closed = last_closed(calendar, now)
    parts.append(f"last closed: {closed.session_date if closed else 'none yet this term'}")
    report.add(PASS, "calendar", "; ".join(parts))


# The one loop that reads the session, and what it learns


@dataclass
class ProbeOrder:
    """The test order, as the exchange has reported it.

    Reports are tied to it by its level (instrument, BUY, price) and strategy, once its
    `new` is accepted: the team holds at most one order at a level, so from then until it
    leaves, the level holds the test order. That holds only while nothing else acts on this
    strategy's buy orders on the instrument. A sign that something does (an amend of the
    test order, a fill of the strategy at another price, a cancel of the test order by
    someone else's request) is interference: from then on nothing can be tied to the test
    order for certain, so it is reported as possibly resting. The first report that it left
    the book is final; later reports at the level are about some other order.
    """

    instrument: str
    strat_id: str
    price: int
    new_ref: str = field(default_factory=new_request_ref)
    sent: bool = False
    sent_at: float = 0.0
    accepted_ms: float | None = None
    rejected: Reject | None = None
    resting: bool = False
    filled: int = 0
    fill_prices: list[int] = field(default_factory=list)
    fully_filled: bool = False
    # The order_cancelled that took it off the book, whoever's cancel caused it.
    cancelled: OrderCancelled | None = None
    # Set when a cancel is rejected NO_ORDER_AT_LEVEL: nothing of the team's rests there.
    absent: bool = False
    # The price another of the team's programs amended the order to, if one did.
    moved_to: int | None = None
    # What showed that something else acted on this strategy's buy orders here, if anything.
    interference: list[str] = field(default_factory=list)
    # Fills of this strategy's buy orders here that could not be tied to the test order.
    other_fills: list[Execution] = field(default_factory=list)
    # Set when messages that may have been reports about it were missed after it was sent,
    # such as an amend that moved it: what the script saw of it may then be incomplete.
    reports_missed: bool = False
    cancel_refs: list[str] = field(default_factory=list)
    replies: dict[str, Message] = field(default_factory=dict)
    retried: list[str] = field(default_factory=list)

    @property
    def where(self) -> str:
        return f"BUY {TEST_ORDER_SIZE} {self.instrument} @ {to_decimal(self.price)}"

    @property
    def level_price(self) -> int:
        """The price the order rests at as far as this script knows: its own, or the price
        another program last moved it to."""
        return self.price if self.moved_to is None else self.moved_to

    @property
    def where_now(self) -> str:
        """Where the order may rest now: its own level, or the one it was moved to."""
        if self.moved_to is None:
            return self.where
        return f"BUY {self.instrument} @ {to_decimal(self.moved_to)}"

    @property
    def gone(self) -> bool:
        return self.fully_filled or self.cancelled is not None

    @property
    def interfered(self) -> bool:
        return bool(self.interference)

    @property
    def confirmed(self) -> bool:
        """Whether one of this script's own cancels took the order off the book."""
        return self.cancelled is not None and request_ref_of(self.cancelled) in self.cancel_refs

    @property
    def may_rest(self) -> bool:
        if not self.sent or self.rejected is not None:
            return False
        if self.accepted_ms is None:
            # Never seen accepted, so nothing seen at its level can be tied to it.
            return True
        if self.reports_missed or self.interfered:
            # What was seen of it may not be all, or may not be about it at all.
            return True
        return not self.gone and not self.absent

    def warning(self) -> str:
        if self.accepted_ms is None:
            return (
                f"WARNING: no reply to the test order ({self.where}) was seen, so it may "
                "still be resting there, or wherever an amend moved it. Check your team's "
                "orders and cancel it yourself."
            )
        if self.interfered:
            return (
                f"WARNING: something else acted on this strategy's buy orders on "
                f"{self.instrument} during the test, so the test order may still be resting "
                f"at {self.where_now}, or elsewhere. Check your team's orders and positions."
            )
        if self.reports_missed:
            return (
                f"WARNING: reports about the test order ({self.where}) were missed, so it "
                "may still rest, there or elsewhere if it was moved. Check your team's "
                "orders and cancel it yourself."
            )
        return (
            f"WARNING: the test order may still be resting at {self.where_now}. Check your "
            "team's orders and cancel that level yourself."
        )

    def fill_warning(self) -> str:
        prices = ", ".join(str(to_decimal(price)) for price in self.fill_prices)
        doubt = (
            " (reports were missed, so it may have been another order of this strategy there)"
            if self.reports_missed
            else ""
        )
        return (
            f"WARNING: the test order filled{doubt}: your team bought {self.filled} "
            f"{self.instrument} at {prices}, and now holds that position."
        )

    def other_fill_warning(self) -> str:
        fills = ", ".join(
            f"{fill.fill_size} at {to_decimal(fill.fill_price)}" for fill in self.other_fills
        )
        return (
            f"WARNING: fills of strategy {self.strat_id}'s buy orders on {self.instrument} "
            f"were seen that cannot be told apart from the test order: {fills}. Your team "
            "may now hold that position."
        )

    def answered(self) -> bool:
        """Whether the exchange has said what became of the new order."""
        if self.rejected is not None or self.gone or self.interfered:
            return True
        return self.accepted_ms is not None and self.resting

    def interfere(self, sign: str) -> None:
        if sign not in self.interference:
            self.interference.append(sign)

    def apply(self, message: Message) -> None:
        ref = request_ref_of(message)
        match message:
            case Accepted():
                if ref == self.new_ref and self.accepted_ms is None:
                    self.accepted_ms = (time.monotonic() - self.sent_at) * 1000
                elif ref in self.cancel_refs:
                    self.replies[ref] = message
                return
            case Reject():
                if ref == self.new_ref:
                    self.rejected = message
                elif ref in self.cancel_refs:
                    self.replies[ref] = message
                return
            case OrderCancelled() if ref in self.cancel_refs and not self.gone:
                # This script's own cancel, which names the test order's first level.
                if message.strat_id != self.strat_id:
                    # It found another strategy's order there: the test order left unseen.
                    self.interfere(
                        f"this script's cancel removed strategy {message.strat_id}'s order"
                    )
                    self.absent = True
                elif self.moved_to is None and not self.interfered:
                    self.cancelled = message
                return
            case OrderState() | Execution() | OrderCancelled():
                pass
            case _:
                return
        if (
            self.accepted_ms is None  # about an order that held the level before
            or self.gone  # the order left the book: later reports are about another
            or message.strat_id != self.strat_id
            or (message.instrument, message.side) != (self.instrument, BUY)
        ):
            return
        match message:
            case OrderState():
                if message.HasField("old_price"):
                    # An amend, which this script never sends, of its price or only its size.
                    if message.old_price == self.level_price:
                        if message.price != message.old_price:
                            self.moved_to = message.price
                        self.interfere("an amend of the test order")
                elif message.price == self.level_price and message.state in (RESTING, STALE):
                    self.resting = True
            case Execution():
                if not message.HasField("order_price"):
                    return
                if message.order_price == self.level_price and not self.interfered:
                    self.filled += message.fill_size
                    self.fill_prices.append(message.fill_price)
                    self.fully_filled = message.remaining_size == 0
                else:
                    self.other_fills.append(message)
                    self.interfere("a fill of this strategy at another price")
            case OrderCancelled():
                if not message.HasField("price") or message.price != self.level_price:
                    return
                if self.interfered:
                    # Cannot be tied to the test order: it ends nothing, so later fills are
                    # still collected and named.
                    return
                if ref is not None and ref not in self.cancel_refs:
                    self.interfere("a cancel of the test order sent by something else")
                self.cancelled = message


class Watcher:
    """Reads the session in one task, and applies every event, in the order received, to
    what the checks need. Every check waits on it rather than reading the session."""

    def __init__(self, session: Session, seconds: float) -> None:
        self.session = session
        self.seconds = seconds
        self.queue: asyncio.Queue[object] = asyncio.Queue()
        self.books = LatestBooks()
        self.state: SessionState | None = None
        self.grid_points = 0  # session_state messages received
        self.messages = 0
        self.counts: defaultdict[str, Counter[str]] = defaultdict(Counter)
        self.closes: dict[str, OfficialClose] = {}
        self.unknown_instruments: dict[str, str] = {}
        self.refusals: list[str] = []  # subscription rejects that name no instrument
        self.gaps = 0
        self.report_gaps = 0  # gaps in the team's private order reports
        self.undecodable = 0
        self.unknown_types: set[str] = set()
        self.account_ref: str | None = None
        self.account_reply: AccountState | Reject | None = None
        # Whether the reply refuses the query as a message type the exchange does not know.
        self.account_unknown = False
        self.order: ProbeOrder | None = None
        self.closed = False
        self.failure: Exception | None = None

    async def read(self) -> None:
        """The one loop that reads the session. It queues every event, then None when the
        exchange closes the connection, or the exception if it drops."""
        try:
            async for event in self.session:
                await self.queue.put(event)
        except Exception as error:
            await self.queue.put(error)
        else:
            await self.queue.put(None)

    async def sent(self, sending: Awaitable[object], deadline: float | None = None) -> bool:
        """Await one send. False if the connection had closed, or if the send did not
        finish within --seconds or by `deadline` (on the event loop's clock), as on a
        connection that has stopped taking data."""
        limit = asyncio.get_running_loop().time() + self.seconds
        try:
            async with asyncio.timeout_at(limit if deadline is None else min(limit, deadline)):
                await sending
        except (ConnectionClosed, TimeoutError):
            return False
        return True

    async def until(self, done: Callable[[], bool], deadline: float) -> bool:
        """Apply events as they arrive until `done()` is true, the connection ends, or
        `deadline` (on the event loop's clock) passes. Returns `done()`."""
        loop = asyncio.get_running_loop()
        while not done():
            remaining = deadline - loop.time()
            if self.closed or remaining <= 0:
                return False
            try:
                async with asyncio.timeout(remaining):
                    item = await self.queue.get()
            except TimeoutError:
                return False
            self.take(item)
        return True

    def drain(self) -> None:
        """Apply every event already received, without waiting."""
        while not self.closed and not self.queue.empty():
            self.take(self.queue.get_nowait())

    def take(self, item: object) -> None:
        if item is None:
            self.closed = True
        elif isinstance(item, Exception):
            self.closed, self.failure = True, item
        else:
            self.apply(item)

    def has_session_book(self, instrument: str) -> bool:
        """Whether a book of `instrument` from the session in progress has arrived."""
        book = self.books.get(instrument)
        return (
            book is not None and self.state is not None and book.grid_time >= self.state.open_time
        )

    def waiting_for_books(self, instruments: list[str]) -> bool:
        """Whether the market is open and an instrument the exchange knows has no book from
        this session yet."""
        if self.state is None or self.state.state != MarketSessionPhase.OPEN:
            return False
        return any(
            not self.has_session_book(name)
            for name in instruments
            if name not in self.unknown_instruments
        )

    def apply(self, event: Any) -> None:
        self.messages += 1
        if isinstance(event, SeqGap):
            self.gaps += 1
        elif isinstance(event, ReportGap):
            self.report_gaps += 1
        elif isinstance(event, DecodeFailed):
            self.undecodable += 1
        elif isinstance(event, Unknown):
            self.unknown_types.add(event.type)
        if self.order is not None and self.order.sent and missed_reports(event):
            self.order.reports_missed = True
        item = as_market_data(event)
        self.books.update(item)  # lists a book as stale after a gap
        if isinstance(item, SessionState):
            self.state = item
            self.grid_points += 1
        elif isinstance(item, Book):
            self.counts[item.instrument]["books"] += 1
        elif isinstance(item, Trades):
            self.counts[item.instrument]["trade prints"] += len(item.prints)
        elif isinstance(item, Mark):
            self.counts[item.instrument]["marks"] += 1
        elif isinstance(item, OfficialClose):
            self.closes[item.instrument] = item
        elif isinstance(item, Reject):
            # A refused subscription. It names the instrument when the exchange does not
            # know it.
            if item.HasField("instrument"):
                self.unknown_instruments[item.instrument] = reject_text(item)
            else:
                self.refusals.append(reject_text(item))
        elif is_account_state(event):
            if event.message.request_ref == self.account_ref:
                self.account_reply = event.message
        elif is_order_event(event):
            message = event.message
            ref = request_ref_of(message)
            ours = ref is not None and ref == self.account_ref
            if isinstance(message, Reject) and ours:
                self.account_reply = message
                self.account_unknown = refuses_unknown_type(event)
            elif (
                self.account_ref is not None
                and self.account_reply is None
                and ref is None
                and refuses_unknown_type(event)
            ):
                # Nothing else this script sends before the test order is a type an
                # exchange could fail to know, so this refuses the account query.
                self.account_reply = message
                self.account_unknown = True
            elif self.order is not None:
                self.order.apply(message)


# Market data, the account and the feed


def check_market(
    report: Report, watcher: Watcher, instruments: list[str], book_limit: float, watched: float
) -> None:
    """Report the session state and each instrument. `book_limit` is how long a book was
    waited for during a session, and `watched` how long market data was watched."""
    if not instruments:
        hint = "no instruments given: pass --instruments, for example --instruments AAPL MSFT"
        report.add(SKIP, "session-state", hint)
        report.add(SKIP, "market", hint)
        return
    state = watcher.state
    if state is None:
        report.add(FAIL, "session-state", f"none received within {watched:.0f} s of subscribing")
    else:
        outage = "; an exchange outage is in force" if state.outage_active else ""
        phase = name_of(MarketSessionPhase, state.state)
        report.add(PASS, "session-state", f"{phase}, session {state.session_date}{outage}")
    for reason in watcher.refusals:
        report.add(FAIL, "subscribe", f"a subscription was refused: {reason}")
    is_open = state is not None and state.state == MarketSessionPhase.OPEN
    is_closed = state is not None and state.state == MarketSessionPhase.CLOSED
    for instrument in instruments:
        name = f"market:{instrument}"
        book = watcher.books.get(instrument)
        if instrument in watcher.unknown_instruments:
            reason = watcher.unknown_instruments[instrument]
            report.add(FAIL, name, f"the exchange does not know it: {reason}")
        elif is_open:
            if watcher.has_session_book(instrument):
                assert book is not None
                counts = watcher.counts[instrument]
                arrived = ", ".join(
                    f"{counts[kind]} {kind}" for kind in ("books", "trade prints", "marks")
                )
                report.add(PASS, name, f"{best_text(book)}; {arrived} in {watched:.0f} s")
            else:
                # A session publishes a book only for an instrument that has one, so one
                # with no valid quote yet has none: not a fault in the setup.
                why = "the instrument may have no valid quote yet"
                if watcher.closed and watched < book_limit:
                    why = "the connection ended first"
                    report.add(SKIP, name, f"no book published within {watched:.1f} s ({why})")
                else:
                    report.add(SKIP, name, f"no book published within {book_limit:g} s ({why})")
        elif is_closed:
            close = watcher.closes.get(instrument)
            if close is None:
                report.add(SKIP, name, "no official close for this instrument")
            else:
                frozen = " (frozen)" if close.frozen else ""
                text = f"official close {to_decimal(close.value)}{frozen}"
                report.add(PASS, name, f"{text} for session {close.session_date}")
        else:
            report.add(SKIP, name, "no session state, so nothing to expect")


def check_account(report: Report, watcher: Watcher, seconds: float) -> None:
    reply = watcher.account_reply
    if reply is None:
        report.add(SKIP, "account", f"not answered by this exchange within {seconds:g} s")
    elif isinstance(reply, Reject) and watcher.account_unknown:
        reason = f"refused as a message type this exchange does not know ({reject_text(reply)})"
        report.add(SKIP, "account", f"{reason}: it does not serve the query yet")
    elif isinstance(reply, Reject):
        report.add(FAIL, "account", f"refused: {reject_text(reply)}")
    else:
        basis = name_of(ValuationBasis, reply.valuation_basis)
        summary = "with" if reply.HasField("summary") else "without"
        text = f"answered, valued at {basis}, {summary} a summary (figures not shown)"
        report.add(PASS, "account", text)


def check_feed(report: Report, watcher: Watcher) -> None:
    problems = []
    if watcher.gaps:
        problems.append(f"{watcher.gaps} sequence gap(s): messages were missed")
    if watcher.report_gaps:
        problems.append(f"{watcher.report_gaps} gap(s) in your team's order reports")
    if watcher.undecodable:
        problems.append(f"{watcher.undecodable} message(s) could not be decoded")
    if watcher.failure is not None:
        problems.append(f"the connection dropped ({type(watcher.failure).__name__})")
    elif watcher.closed:
        problems.append("the exchange closed the connection")
    if problems:
        report.add(FAIL, "feed", "; ".join(problems))
        return
    note = ""
    if watcher.unknown_types:
        names = ", ".join(sorted(watcher.unknown_types))
        note = f"; message types this SDK does not know: {names} (a newer SDK may read them)"
    report.add(PASS, "feed", f"{watcher.messages} messages, none missed or unreadable{note}")


def check_heartbeat(report: Report, session: Session, opened_at: float) -> None:
    """Whether the exchange's heartbeats arrived while the session was read, and about how
    far apart. Never a FAIL: the interval is the exchange's to set, and may be longer than
    this run, so none arriving is a SKIP. No interval is assumed here."""
    count = session.heartbeats_received
    read = time.monotonic() - opened_at
    if count == 0:
        report.add(
            SKIP,
            "heartbeat",
            f"no heartbeat in {read:.0f} s of reading; the exchange's interval may be longer "
            "than this run, try a larger --seconds",
        )
        return
    # From the exchange's own clock when the heartbeat carries its send time, else from
    # when they arrived here.
    apart = None
    sent_at = session.last_heartbeat_sent_at
    if sent_at is not None:
        apart = (sent_at - session.info.server_time) / count / 1000
    if apart is None or apart <= 0:
        arrived = session.last_heartbeat_at
        apart = None if arrived is None else (arrived - opened_at) / count
    spacing = "" if apart is None or apart <= 0 else f", about {apart:.3g} s apart"
    report.add(PASS, "heartbeat", f"{count} heartbeat(s){spacing}")


# The test order


def probe_price(book: Book, tick: int) -> tuple[int | None, str]:
    """The test buy's price and "", or None and why there is none.

    The price is one tick above the wall's best bid, the lowest at which a buy can rest,
    since an order rests only strictly inside the wall's best bid and ask. It must also be
    at least MIN_ROOM_TICKS ticks below every ask, the wall's and resting orders': there is
    no post-only order, so if the asks fall to it during the order delay, it trades."""
    if book.condition != InstrumentCondition.LIVE or not book.bid_levels or not book.ask_levels:
        condition = name_of(InstrumentCondition, book.condition)
        return None, f"its quote is not two-sided and LIVE ({condition})"
    wall_bid = book.bid_levels[0].price
    best_ask = min(
        levels[0].price for levels in (book.ask_levels, book.student_ask_levels) if levels
    )
    price = (wall_bid // tick + 1) * tick
    if best_ask - price < MIN_ROOM_TICKS * tick:
        return None, f"fewer than {MIN_ROOM_TICKS} ticks between the band's floor and the asks"
    return price, ""


def choose(
    watcher: Watcher, instruments: list[str], state: SessionState, tick: int
) -> tuple[str, int] | str:
    """The first instrument the test order can rest on and its price, or why there is none."""
    why = []
    for instrument in instruments:
        if instrument in watcher.unknown_instruments:
            continue
        book = watcher.books.get(instrument)
        if book is None:
            problem = "no book"
        elif book.grid_time < state.open_time:
            problem = "no book from this session"
        elif instrument in watcher.books.stale:
            problem = "its book may be out of date, since messages were missed"
        else:
            price, problem = probe_price(book, tick)
            if price is not None:
                return instrument, price
        why.append(f"{instrument}: {problem}")
    return "; ".join(why) or "the exchange knows none of the instruments"


def rejected_new(order: ProbeOrder) -> tuple[str, str]:
    assert order.rejected is not None
    text = reject_text(order.rejected)
    code = order.rejected.reason_code
    if code in NOT_TAKING_ORDERS:
        return SKIP, f"{order.where} not placed: the exchange is not taking orders now: {text}"
    if code == ReasonCodes.DUPLICATE_ORDER_AT_LEVEL:
        return SKIP, f"{order.where} not placed: your team already has an order there ({text})"
    hint = {
        ReasonCodes.STRATEGY_NOT_REGISTERED: "; ask the Head of Technology to register it",
        ReasonCodes.NO_MARKET_ACCESS: "; this team's account does not send orders",
        ReasonCodes.TICK_VIOLATION: "; give the instrument's tick with --tick",
    }.get(code, "")
    return FAIL, f"{order.where} rejected: {text}{hint}"


def left_alone(order: ProbeOrder) -> tuple[str, str]:
    """The verdict when the order left the book without one of this script's cancels."""
    if order.filled:
        rest = "" if order.fully_filled else ", and the exchange cancelled the rest"
        return FAIL, (
            f"{order.where} filled {order.filled} share(s){rest}: a real trade, so your team "
            "now holds that position"
        )
    assert order.cancelled is not None
    code = order.cancelled.reason_code
    reason = reason_code_name(code)
    if code == ReasonCodes.SESSION_CLOSE:
        return SKIP, f"{order.where}: the session closed, which cancelled it ({reason})"
    if code == ReasonCodes.REMAINDER_OUTSIDE_BAND and not order.resting:
        # The wall's bid rose to the price during the order delay: nothing rested.
        return SKIP, (
            f"{order.where} was not left resting ({reason}): the wall moved during the "
            "order delay, so nothing rested; run it again"
        )
    rested = "rested, then was cancelled" if order.resting else "was not left resting"
    return FAIL, f"{order.where} {rested} by the exchange: {reason}; nothing is left resting"


async def cancel_level(
    watcher: Watcher, order: ProbeOrder, seconds: float, on_sent: asyncio.Event | None = None
) -> str | None:
    """Cancel the test order's level until the order leaves the book or the exchange reports
    nothing there. Returns None then, else why it could not confirm either. `on_sent`, if
    given, is set once a cancel has been sent."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    grid_points = 1
    while True:
        watcher.drain()  # apply what has arrived before deciding to send
        if order.gone or order.interfered or order.rejected is not None:
            # Nothing of the test order is at its level: a cancel there could only hit
            # another order, such as the one a DUPLICATE_ORDER_AT_LEVEL reject names.
            return None
        if watcher.closed:
            return "the connection ended"
        if loop.time() >= deadline:
            return f"not confirmed within {seconds:g} s"
        ref = new_request_ref()
        order.cancel_refs.append(ref)  # recorded first, so its reply is matched
        if not await watcher.sent(
            send_cancel(
                watcher.session,
                instrument=order.instrument,
                side=BUY,
                price=order.price,
                request_ref=ref,
            ),
            deadline,
        ):
            return "the cancel could not be sent: the connection closed or stalled"
        if on_sent is not None:
            on_sent.set()
        answered = await watcher.until(
            lambda ref=ref: (
                order.gone or order.interfered or isinstance(order.replies.get(ref), Reject)
            ),
            deadline,
        )
        if order.gone or order.interfered:
            return None
        reply = order.replies.get(ref)
        if not answered:
            if isinstance(reply, Accepted):
                return f"the cancel was accepted, but no order_cancelled followed in {seconds:g} s"
            return "the connection ended" if watcher.closed else f"no reply within {seconds:g} s"
        assert isinstance(reply, Reject)
        if reply.reason_code == ReasonCodes.NO_ORDER_AT_LEVEL:
            order.absent = True
            return None
        if reply.reason_code not in RETRY_CANCEL:
            return f"the cancel was rejected: {reject_text(reply)}"
        order.retried.append(reason_code_name(reply.reason_code))
        # Send it again after the exchange's next grid points, twice as many each time: the
        # exchange decides how long an order must rest, so its own clock paces the retry.
        target = watcher.grid_points + grid_points
        grid_points *= 2
        await watcher.until(
            lambda target=target: order.gone or order.interfered or watcher.grid_points >= target,
            deadline,
        )


async def interfered(watcher: Watcher, order: ProbeOrder, seconds: float) -> tuple[str, str]:
    """The verdict when something else acted on this strategy's buy orders here.

    It first waits for the exchange's next grid point: every report sent before it arrives
    before it on the one connection, so a further move made by then is seen and the level
    named is where the order rested then."""
    loop = asyncio.get_running_loop()
    seen = watcher.grid_points
    await watcher.until(lambda: watcher.grid_points > seen, loop.time() + seconds)
    watcher.drain()
    signs = "; ".join(order.interference)
    return FAIL, (
        f"{order.where}: something else acted on strategy {order.strat_id}'s buy orders on "
        f"{order.instrument} during the test ({signs}), so the test order cannot be told "
        f"apart from them; it may still be resting at {order.where_now}; check your team's "
        "orders and positions"
    )


async def place_and_cancel(watcher: Watcher, order: ProbeOrder, seconds: float) -> tuple[str, str]:
    loop = asyncio.get_running_loop()
    order.sent, order.sent_at = True, time.monotonic()
    sending = send_new(
        watcher.session,
        strat_id=order.strat_id,
        instrument=order.instrument,
        side=BUY,
        order_type=LIMIT,
        price=order.price,
        size=TEST_ORDER_SIZE,
        request_ref=order.new_ref,
    )
    try:
        if not await watcher.sent(sending, loop.time() + seconds):
            return FAIL, (
                f"{order.where} could not be sent (the connection closed or stalled): it may be "
                "resting; check your team's orders and cancel that level yourself"
            )
    except ValueError as error:  # an identifier the contract does not allow; nothing sent
        order.sent = False
        return FAIL, f"not sent: {error}"
    await watcher.until(order.answered, loop.time() + seconds)
    watcher.drain()  # so a later report, such as a second move, is seen before deciding
    if order.rejected is not None:
        return rejected_new(order)
    if order.interfered:
        return await interfered(watcher, order, seconds)
    if order.gone:
        return left_alone(order)
    # It rests, or what became of it is unknown. Cancel the level either way: a cancel is
    # applied after the new, so it also removes an order that rests after this wait.
    unsure = None
    if order.accepted_ms is None:
        unsure = f"no reply to the order within {seconds:g} s"
    elif not order.resting:
        unsure = f"accepted, but not reported resting within {seconds:g} s"
    why_not = await cancel_level(watcher, order, seconds)
    watcher.drain()  # a move or a fill may have arrived just behind the last reply
    if order.interfered:
        return await interfered(watcher, order, seconds)
    if order.confirmed:
        if order.accepted_ms is None:
            return FAIL, (
                f"{order.where}: {unsure}; a cancel then removed an order of this strategy "
                "at that level, which cannot be tied to the test order"
            )
        if order.reports_missed:
            return FAIL, (
                f"{order.where}: the cancel removed an order of this strategy at that level, "
                "but reports were missed, so it may have been another that took the level "
                "after the test order moved; it may rest elsewhere, so check your team's orders"
            )
        if order.filled:
            return FAIL, (
                f"{order.where} filled {order.filled} share(s) before the cancel removed the "
                "rest: a real trade, so your team now holds that position"
            )
        if unsure is not None:
            return FAIL, f"{order.where}: {unsure}; the cancel then removed it"
        text = (
            f"{order.where} rested (accepted after {order.accepted_ms:.0f} ms), "
            "then was cancelled and the cancel confirmed"
        )
        if order.retried:
            text += (
                f", after {len(order.retried)} rejected cancel(s) "
                f"({', '.join(order.retried)}) sent again on later grid points"
            )
        return PASS, text
    if order.gone:
        return left_alone(order)
    if order.absent and order.accepted_ms is None:
        return FAIL, (
            f"{order.where}: {unsure}; the exchange then reported no order of your team at "
            "that level, so it is not there, but this script cannot tell where it went"
        )
    if order.absent and order.reports_missed:
        return FAIL, (
            f"{order.where}: the exchange reported no order at that level, but reports "
            "about it were missed, so it may rest elsewhere; check your team's orders"
        )
    if order.absent:
        return FAIL, (
            f"{order.where}: {unsure or 'it rested'}, then the exchange reported no order at "
            "that level, without this script seeing it leave; nothing rests there now"
        )
    return FAIL, (
        f"could not confirm the cancel ({why_not}): the order may still be resting at "
        f"{order.where}; check your team's orders and cancel that level yourself"
    )


async def check_test_order(report: Report, watcher: Watcher, args: argparse.Namespace) -> None:
    name = "test-order"
    if not args.place_test_order:
        hint = (
            "not asked for: --place-test-order --strat-id ID --tick DOLLARS places and "
            "cancels one order"
        )
        report.add(SKIP, name, hint)
        return
    watcher.drain()  # decide on everything received so far
    state = watcher.state
    if watcher.closed:
        report.add(SKIP, name, "the connection has ended")
        return
    if not args.instruments:
        report.add(SKIP, name, "no instruments given")
        return
    if state is None or state.state != MarketSessionPhase.OPEN:
        phase = "unknown" if state is None else name_of(MarketSessionPhase, state.state)
        report.add(SKIP, name, f"the market session is {phase}: it is placed only while OPEN")
        return
    if state.outage_active:
        report.add(SKIP, name, "an exchange outage is in force: no order is placed during one")
        return
    if not watcher.session.info.unscored and not args.allow_scored:
        report.add(
            SKIP,
            name,
            "this is a scored session, where the test order is a real order of your team "
            "and can fill: add --allow-scored to place it all the same",
        )
        return
    assert args.tick is not None  # required with --place-test-order
    choice = choose(watcher, args.instruments, state, args.tick)
    if isinstance(choice, str):
        report.add(SKIP, name, f"nowhere to place it: {choice}")
        return
    instrument, price = choice
    order = ProbeOrder(instrument, args.strat_id, price)
    watcher.order = order
    try:
        status, reason = await place_and_cancel(watcher, order, args.seconds)
    except BaseException:
        # Interrupted (Ctrl+C) or failed part-way while the order may rest: try once more
        # to cancel exactly its level before the error goes on.
        if order.may_rest and not order.interfered:
            await clean_up(watcher, order, args.seconds)
        raise
    else:
        report.add(status, name, reason)
    finally:
        # Also on Ctrl+C or a failure part-way, so the level is always named.
        if order.filled:
            say(order.fill_warning())
        if order.other_fills:
            say(order.other_fill_warning())
        if order.may_rest:
            say(order.warning())


def say(text: str) -> None:
    """Print a line to stderr, if it can still be written. On a stop path the terminal or
    pipe may be gone (a closed terminal is what SIGHUP means), and a failed write must not
    stop what follows, above all the cleanup cancel."""
    with contextlib.suppress(OSError, ValueError):
        print(text, file=sys.stderr, flush=True)


async def clean_up(watcher: Watcher, order: ProbeOrder, seconds: float) -> None:
    """A bounded, best-effort cancel of exactly the test order's level after an interruption.

    It runs in a task of its own behind `asyncio.shield`, so the interruption that started it
    does not stop it. The task is made before anything is said, so nothing can keep the
    cancel from being sent. A further SIGTERM or SIGHUP stops the wait for the confirmation,
    but not before the cancel itself has gone out (a second at most). A second Ctrl+C is
    Python's own "stop now", which ends every task at once."""
    sent = asyncio.Event()
    cleanup_seconds = min(seconds, CLEANUP_SECONDS)
    cancelling = asyncio.ensure_future(cancel_level(watcher, order, cleanup_seconds, sent))
    say(f"interrupted: cancelling the test order's level ({order.where}) before stopping")
    try:
        await asyncio.shield(cancelling)
    except (asyncio.CancelledError, KeyboardInterrupt):
        # Interrupted again (or stopped by a signal while cleaning up after an error): stop
        # waiting for the confirmation, and stop as that asks, once the cancel is out. The
        # warning that follows says what may rest.
        # Further stops within that grace do not cut it short: its end is fixed.
        loop = asyncio.get_running_loop()
        grace_ends = loop.time() + min(cleanup_seconds, 1.0)
        while not sent.is_set() and not cancelling.done():
            left = grace_ends - loop.time()
            if left <= 0:
                break
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                async with asyncio.timeout(left):
                    await asyncio.shield(sent.wait())
        raise
    except Exception:
        pass  # the cancel failed: the warning that follows says so


# History


async def first_past_book(
    client: HistoryClient, day: str, instrument: str, seconds: float
) -> tuple[str, str]:
    """Read the start of one closed session's books, up to the first book. A whole session
    is too much to download here, so the rest, and the digest the service states for the
    whole, are not checked."""
    try:
        async with asyncio.timeout(seconds):
            async with aclosing(client.fetch(day, instrument, "book")) as items:
                async for item in items:
                    if isinstance(item, Book):
                        sample = "only its start was read, not checked against its digest"
                        return PASS, f"session {day}: first book {best_text(item)}; {sample}"
                    if isinstance(item, Unknown | DecodeFailed):
                        kind = type(item).__name__
                        return FAIL, f"session {day}: a line could not be used ({kind})"
        return FAIL, f"session {day}: no books served"
    except HistoryPending:
        return SKIP, f"session {day} is not ready yet: try again later"
    except HistoryNotImplemented:
        return SKIP, f"session {day}: the service does not serve books yet"
    except HistoryError as error:
        # Its kind and HTTP status only: its text carries the service's own words.
        status = f", HTTP {error.http_status}" if error.http_status is not None else ""
        return FAIL, f"session {day}: refused ({type(error).__name__}{status})"
    except TimeoutError:
        return FAIL, f"session {day}: no answer within {seconds:g} s"
    except Exception as error:
        # A network failure. Its text can repeat the address, so only its kind is shown.
        return FAIL, f"session {day}: could not reach the service ({type(error).__name__})"


async def check_history(
    report: Report,
    args: argparse.Namespace,
    calendar: Calendar | None,
    now: int,
    instruments: list[str],
) -> None:
    if not os.environ.get(HISTORY_URL_ENV_VAR):
        hint = (
            f"{HISTORY_URL_ENV_VAR} is not set: set it in the environment (it is never read "
            "from .env) to the history service's address to check past data"
        )
        report.add(SKIP, "history", hint)
        return
    if not instruments:
        report.add(SKIP, "history", "no instrument the exchange knows to ask about")
        return
    closed = None if calendar is None else last_closed(calendar, now)
    if closed is None:
        why = "no calendar" if calendar is None else "no session has closed yet this term"
        report.add(SKIP, "history", f"{why}, so no closed session to ask about")
        return
    try:
        # Never wait for data that is not ready yet: report it instead. A timeout here
        # wakes the client's worker thread at once, except while it looks up the address,
        # which only the system resolver bounds, or opens a TCP connection, which the
        # client's timeout (half of --seconds) bounds for each address it tries. So a slow
        # lookup, or several unreachable addresses, can still hold the exit for longer.
        client = HistoryClient(timeout=args.seconds / 2, max_retries=0)
    except (ValueError, MissingToken) as error:
        # Not the error's text, which can repeat part of the address.
        why = (
            "it must be the service's https address (http only on this machine), with no "
            "credentials, query or fragment, and the token printable ASCII"
        )
        report.add(
            FAIL, "history", f"cannot use {HISTORY_URL_ENV_VAR} ({type(error).__name__}): {why}"
        )
        return
    for instrument in instruments:
        status, reason = await first_past_book(
            client, closed.session_date, instrument, args.seconds
        )
        report.add(status, f"history:{instrument}", reason)


# Running it


def skip_after_connect(report: Report, reason: str) -> None:
    names = ("calendar", "instruments", "market", "account", "test-order", "heartbeat", "history")
    for name in names:
        report.add(SKIP, name, reason)


@contextlib.asynccontextmanager
async def closing(session: Session) -> AsyncIterator[Session]:
    """Like `async with session:`, but waits at most CLOSE_SECONDS for the close."""
    try:
        yield session
    finally:
        try:
            async with asyncio.timeout(CLOSE_SECONDS):
                await session.close()
        except TimeoutError:
            say("the connection did not close in time; it is dropped as the script exits")


async def session_checks(
    report: Report, watcher: Watcher, args: argparse.Namespace, opened_at: float
) -> None:
    """The checks made on the open session. `opened_at` is when the script began to open
    it, on the `time.monotonic()` clock."""
    loop = asyncio.get_running_loop()
    session = watcher.session
    subscribed = loop.time()  # the waits below count from here, before any send
    # One subscribe per instrument, so one the exchange does not know cannot keep the
    # others from being served.
    for instrument in args.instruments:
        await watcher.sent(subscribe(session, [instrument]))
    watcher.account_ref = new_request_ref()
    await watcher.sent(send_account_query(session, request_ref=watcher.account_ref))
    # Watch for --seconds, applying every event as it arrives.
    await watcher.until(lambda: False, subscribed + args.seconds)
    # During a session, give an instrument with no book yet up to --book-wait for its first.
    book_limit = max(args.seconds, args.book_wait)
    await watcher.until(
        lambda: not watcher.waiting_for_books(args.instruments), subscribed + book_limit
    )
    check_market(report, watcher, args.instruments, book_limit, loop.time() - subscribed)
    check_account(report, watcher, args.seconds)
    await check_test_order(report, watcher, args)
    watcher.drain()
    check_feed(report, watcher)
    check_heartbeat(report, session, opened_at)


async def run(url: str, args: argparse.Namespace, report: Report) -> None:
    """Run the checks, stopping as on Ctrl+C if the process is asked to stop."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    # SIGTERM (a `kill`, a stop button, a time limit) and SIGHUP (a closed terminal) cancel
    # this task, as Ctrl+C does, so a test order that may rest is cancelled first and any
    # warning printed. Off Unix there are no such signals to handle. SIGKILL cannot be
    # caught: after one, check your team's orders yourself.
    handled = []
    for name in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is None or signal.getsignal(signum) == signal.SIG_IGN:
            continue  # absent here, or ignored on purpose (as under nohup): leave it so
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signum, stop, task, signum)
            handled.append(signum)
    try:
        await run_checks(url, args, report)
    finally:
        for signum in handled:
            loop.remove_signal_handler(signum)


def stop(task: asyncio.Task[Any], signum: int) -> None:
    """Stop the run on a signal: cancel its task, as Ctrl+C does, noting which signal."""
    STOPPED_BY.append(signum)
    task.cancel()


# The signals that stopped the run, if any.
STOPPED_BY: list[int] = []


async def run_checks(url: str, args: argparse.Namespace, report: Report) -> None:
    failure = None
    try:
        opened_at = time.monotonic()
        session = await open_session(url, ack_timeout=args.seconds)
    # Only the error's kind, or the exchange's reason name, is shown: an error's text can
    # repeat the address (with anything a mistake put in it), and a refusal's free-text
    # detail is the exchange's own words.
    except ContractVersionMismatch as error:
        failure = (
            f"the exchange does not serve this SDK's contract version ({error.reason_name}): "
            "update the SDK"
        )
    except SessionRejected as error:
        failure = f"the exchange refused the session: {error.reason_name}"
    except Exception as error:
        failure = f"could not connect ({type(error).__name__})"
    if failure is not None:
        report.add(FAIL, "connect", failure)
        skip_after_connect(report, "not connected")
        return
    info = session.info
    async with closing(session):
        scoring = "unscored" if info.unscored else "scored"
        contract = info.contract_version
        report.add(
            PASS, "connect", f"authenticated as team {info.team} ({scoring}, contract {contract})"
        )
        # Read before the reader starts: the calendar follows the acknowledgement.
        calendar = await session.wait_for_calendar(timeout=args.seconds)
        check_calendar(report, calendar, info.server_time, args.seconds)
        watcher = Watcher(session, args.seconds)
        reader = asyncio.create_task(watcher.read())
        try:
            await session_checks(report, watcher, args, opened_at)
        finally:
            reader.cancel()
        # Read at the end, not waited for: an exchange that does not send it would hold up
        # every check after it, and the watcher has read the session meanwhile.
        check_instruments(report, session.instrument_table, args.instruments)
        known = [name for name in args.instruments if name not in watcher.unknown_instruments]
    await check_history(report, args, calendar, info.server_time, known)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = Report()
    check_sdk_version(report)
    url, problems = check_setup(report)
    if problems or url is None:
        reason = "no token or no usable address"
        report.add(SKIP, "connect", reason)
        skip_after_connect(report, "not connected")
        report.finish()
        for problem in problems:
            print(f"cannot connect: {problem} (see docs/quickstart.md)", file=sys.stderr)
        return 2
    try:
        asyncio.run(run(url, args, report))
    except KeyboardInterrupt:
        say("interrupted")
        return stopped(130)
    except asyncio.CancelledError:
        # SIGTERM or SIGHUP: see run(). The exit status is 128 plus the signal's number.
        signum = STOPPED_BY[0] if STOPPED_BY else signal.SIGTERM
        say(f"stopped by {signal.Signals(signum).name}")
        return stopped(128 + signum)
    return report.finish()


def stopped(status: int) -> int:
    """`status`, once anything still buffered for stdout or stderr is written or, if their
    terminal or pipe is gone, thrown away: Python would otherwise fail to flush them as it
    exits and end with another status (120)."""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:  # started with that descriptor closed (`>&-` or `2>&-`)
            continue
        try:
            stream.flush()
        except (OSError, ValueError):
            with contextlib.suppress(OSError, ValueError):
                fd = stream.fileno()
                devnull = os.open(os.devnull, os.O_WRONLY)
                try:
                    os.dup2(devnull, fd)
                finally:
                    if devnull != fd:
                        os.close(devnull)
    return status


if __name__ == "__main__":
    sys.exit(main())
