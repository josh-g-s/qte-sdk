"""Check a setup end to end and say plainly what works. Sends no orders unless asked.

Set QTE_URL and QTE_TOKEN first, in ./.env or the environment (see docs/quickstart.md),
then, from a clone of this repository:

    python examples/smoke_test.py --instruments AAPL MSFT

Run it first, before any other program, and again whenever something seems wrong. It runs
these checks in order and prints one line for each, PASS, FAIL or SKIP with a one-line
reason, then a summary:

    token, address   where the SDK finds your token and the exchange address, as
                     `python -m qte_sdk.token check` reports them. Neither is shown.
    dotenv           only when git does not ignore the .env the SDK read: a FAIL, since
                     the token could be committed.
    connect          opens a session and names the team it authenticated as.
    calendar         reads the exchange's calendar: the next open and the last close.
    session-state    subscribes to each instrument and watches for --seconds: the market
                     session's state, OPEN during a session and CLOSED outside one.
    market:<name>    during a session, the instrument's best bid and ask and what arrived;
                     outside one, its official close, which the exchange may not send yet
                     (a SKIP). An instrument the exchange does not know is a FAIL.
    account          asks for your team's account. An exchange that does not answer the
                     query yet is a SKIP. No figures are printed.
    test-order       only with --place-test-order (see below).
    feed             whether any message was missed or could not be decoded.
    history:<name>   the last closed session's books from the history service, when
                     QTE_HISTORY_URL is set (from the environment only, never .env).

It exits with status 0 when no check failed, 1 when one did, and 2 when it found no token
or no usable address, and so could not connect. The output never shows the token or any
account figure.

The test order. With --place-test-order --strat-id ID, and only while the market session is
OPEN, it places one passive limit buy of one share on the first instrument whose book is
two-sided and LIVE, waits until the exchange reports it resting, then cancels exactly that
price level and waits for the exchange to confirm the cancel. It never sends a mass cancel,
which would cancel every order your team has, other strategies' included.

Its price is the lowest at which a buy can rest. Your orders rest only strictly inside the
band between the wall's best bid and best ask: an order at the wall's own price or beyond
it is not left resting (`order_cancelled` with REMAINDER_OUTSIDE_BAND). So the price is the
first price on the tick above the wall's best bid, as far below the asks as an order can
rest, and it must be below every ask. The exchange does not send the tick: give it with
--tick, or the script uses the largest step that every price in the book is a multiple of.
Every price in a book is on the tick, so that step is a whole number of ticks and a price
on it is on the tick too. If the spread leaves no room, the check is a SKIP: try another
instrument, or give --tick.

A resting buy can still be filled. If it is, the check says so: your team then holds that
position. A cancel names a price level, not an order, and acts on whichever of your team's
orders rests there when it is applied, so run this on an instrument your team is not
otherwise trading at that price. If the script cannot confirm that the order is gone, the
check fails and names the level where it may still rest, so you can cancel it yourself.

Timing. The exchange holds every order message for its order delay before applying it. The
delay, the minimum time an order must rest before it may be cancelled, the price collar
and the message budgets are set by the exchange, so nothing here assumes their values: the
script cancels once the exchange reports the order resting, and if a cancel is rejected
MIN_REST_VIOLATION (or for a message budget) it sends it again after the exchange's next
market-data grid point, then after two more, four more and so on, rather than after a
fixed sleep. --seconds bounds each wait for the exchange.
"""

import argparse
import asyncio
import contextlib
import math
import os
import sys
import time
import warnings
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from google.protobuf.message import Message
from websockets.exceptions import ConnectionClosed

from qte_sdk.account import AccountState, ValuationBasis, is_account_state, send_account_query
from qte_sdk.books import LatestBooks
from qte_sdk.calendar import Calendar, CalendarSession, next_session, session_open_at
from qte_sdk.connection import ContractVersionMismatch, SessionRejected, Unknown
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
from qte_sdk.units import to_decimal, to_micros

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
NAME_WIDTH = 14

# The test order is one share, the smallest order there is.
TEST_ORDER_SIZE = 1

# The longest wait for the connection to close at the end. A local choice, not a value the
# exchange sets.
CLOSE_SECONDS = 5.0

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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check a setup end to end. Sends no orders unless asked."
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
        "--place-test-order",
        action="store_true",
        help=(
            "during a session, place one passive one-share buy, then cancel it and confirm "
            "the cancel; needs --strat-id"
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
            "the instrument's tick in dollars, for the test order's price, for example 0.01 "
            "(default: worked out from the prices in the book)"
        ),
    )
    args = parser.parse_args(argv)
    if args.seconds <= 0:
        parser.error("--seconds must be more than 0")
    if args.tick is not None and args.tick <= 0:
        parser.error("--tick must be more than 0, for example 0.01")
    if args.place_test_order and not args.strat_id:
        parser.error(
            "--place-test-order needs --strat-id (or QTE_STRAT_ID): a strategy ID "
            "registered for your team"
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
    detail = f" ({reject.reason_detail})" if reject.HasField("reason_detail") else ""
    return f"{reason_code_name(reject.reason_code)}{detail}"


def best_text(book: Book) -> str:
    """The best bid and ask across the wall and resting orders, and the book's condition."""
    bids = [levels[0] for levels in (book.bid_levels, book.student_bid_levels) if levels]
    asks = [levels[0] for levels in (book.ask_levels, book.student_ask_levels) if levels]
    bid = max(bids, key=lambda level: level.price, default=None)
    ask = min(asks, key=lambda level: level.price, default=None)
    bid_text = f"bid {to_decimal(bid.price)} x {bid.size}" if bid is not None else "no bid"
    ask_text = f"ask {to_decimal(ask.price)} x {ask.size}" if ask is not None else "no ask"
    return f"{bid_text}, {ask_text} ({name_of(InstrumentCondition, book.condition)})"


# Where the token and the address come from


def describe(source: str) -> str:
    if source == DOTENV_NAME:
        return str(dotenv_path())
    if source == TOKEN_FILE_ENV_VAR:
        return f"the file named by {TOKEN_FILE_ENV_VAR}"
    return f"the {source} environment variable"


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
            if url.startswith(("ws://", "wss://")):
                report.add(PASS, "address", where)
            else:
                shape = "it does not start with ws:// or wss:// as an exchange address must"
                problems.append(f"QTE_URL is not a WebSocket address: {shape}")
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
        # In the exchange's time units, whose resolution the contract has not fixed.
        wait = upcoming.open_time - now
        parts.append(f"next open: session {upcoming.session_date}, in {wait} exchange time units")
    closed = last_closed(calendar, now)
    parts.append(f"last closed: {closed.session_date if closed else 'none yet this term'}")
    report.add(PASS, "calendar", "; ".join(parts))


# The one loop that reads the session, and what it learns


@dataclass
class ProbeOrder:
    """The test order, as the exchange has reported it."""

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
    fully_filled: bool = False
    # The order_cancelled that took it off the book, whoever's cancel caused it.
    cancelled: OrderCancelled | None = None
    # Set when a cancel is rejected NO_ORDER_AT_LEVEL: nothing of the team's rests there.
    absent: bool = False
    cancel_refs: list[str] = field(default_factory=list)
    replies: dict[str, Message] = field(default_factory=dict)
    retried: list[str] = field(default_factory=list)

    @property
    def where(self) -> str:
        return f"BUY {TEST_ORDER_SIZE} {self.instrument} @ {to_decimal(self.price)}"

    @property
    def gone(self) -> bool:
        return self.fully_filled or self.cancelled is not None

    @property
    def confirmed(self) -> bool:
        """Whether one of this script's own cancels took the order off the book."""
        return self.cancelled is not None and request_ref_of(self.cancelled) in self.cancel_refs

    @property
    def may_rest(self) -> bool:
        return self.sent and self.rejected is None and not self.gone and not self.absent

    def answered(self) -> bool:
        """Whether the exchange has said what became of the new order."""
        if self.rejected is not None or self.gone:
            return True
        return self.accepted_ms is not None and self.resting

    def at(self, instrument: str, side: int, price: int) -> bool:
        return (instrument, side, price) == (self.instrument, BUY, self.price)

    def apply(self, message: Message) -> None:
        ref = request_ref_of(message)
        match message:
            case Accepted():
                if ref == self.new_ref and self.accepted_ms is None:
                    self.accepted_ms = (time.monotonic() - self.sent_at) * 1000
                elif ref in self.cancel_refs:
                    self.replies[ref] = message
            case Reject():
                if ref == self.new_ref:
                    self.rejected = message
                elif ref in self.cancel_refs:
                    self.replies[ref] = message
            case OrderState():
                if (
                    message.strat_id == self.strat_id
                    and self.at(message.instrument, message.side, message.price)
                    and message.state in (RESTING, STALE)
                ):
                    self.resting = True
            case Execution():
                if (
                    message.strat_id == self.strat_id
                    and message.HasField("order_price")
                    and self.at(message.instrument, message.side, message.order_price)
                ):
                    self.filled += message.fill_size
                    self.fully_filled = message.remaining_size == 0
            case OrderCancelled():
                if (
                    message.HasField("price")
                    and self.at(message.instrument, message.side, message.price)
                    and (ref in self.cancel_refs or message.strat_id == self.strat_id)
                ):
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
        self.undecodable = 0
        self.unknown_types: set[str] = set()
        self.account_ref: str | None = None
        self.account_reply: AccountState | Reject | None = None
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

    async def sent(self, sending: Awaitable[object]) -> bool:
        """Await one send. False if the connection had closed, or if the send did not
        finish within --seconds, as on a connection that has stopped taking data."""
        try:
            async with asyncio.timeout(self.seconds):
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

    def apply(self, event: Any) -> None:
        self.messages += 1
        if isinstance(event, SeqGap):
            self.gaps += 1
        elif isinstance(event, DecodeFailed):
            self.undecodable += 1
        elif isinstance(event, Unknown):
            self.unknown_types.add(event.type)
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
            if isinstance(message, Reject) and ref is not None and ref == self.account_ref:
                self.account_reply = message
            elif self.order is not None:
                self.order.apply(message)


# Market data, the account and the feed


def check_market(report: Report, watcher: Watcher, instruments: list[str], seconds: float) -> None:
    if not instruments:
        hint = "no instruments given: pass --instruments, for example --instruments AAPL MSFT"
        report.add(SKIP, "session-state", hint)
        report.add(SKIP, "market", hint)
        return
    state = watcher.state
    if state is None:
        report.add(FAIL, "session-state", f"none received within {seconds:g} s of subscribing")
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
        elif book is not None:
            counts = watcher.counts[instrument]
            arrived = ", ".join(
                f"{counts[kind]} {kind}" for kind in ("books", "trade prints", "marks")
            )
            report.add(PASS, name, f"{best_text(book)}; {arrived} in {seconds:g} s")
        elif is_open:
            report.add(SKIP, name, f"no book within {seconds:g} s: none published for it yet")
        elif is_closed:
            close = watcher.closes.get(instrument)
            if close is None:
                report.add(SKIP, name, "no official close: not sent by this exchange")
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
    elif isinstance(reply, Reject):
        text = reject_text(reply)
        if reply.reason_code in (
            ReasonCodes.MALFORMED_MESSAGE,
            ReasonCodes.REASON_CODE_UNSPECIFIED,
        ):
            # The query this SDK sends is well formed, so this is how an exchange that does
            # not serve it yet would refuse it.
            reason = f"refused as {text}: this exchange may not serve the query yet"
            report.add(SKIP, "account", reason)
        else:
            report.add(FAIL, "account", f"refused: {text}")
    else:
        basis = name_of(ValuationBasis, reply.valuation_basis)
        summary = "with" if reply.HasField("summary") else "without"
        text = f"answered, valued at {basis}, {summary} a summary (figures not shown)"
        report.add(PASS, "account", text)


def check_feed(report: Report, watcher: Watcher) -> None:
    problems = []
    if watcher.gaps:
        problems.append(f"{watcher.gaps} sequence gap(s): messages were missed")
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


# The test order


def price_step(book: Book) -> int:
    """The largest step that every price in the book is a multiple of. Every price in a
    book is on the instrument's tick, so this is a whole number of ticks, and a multiple
    of it is on the tick too, even where it is coarser than the tick itself."""
    sides = (book.bid_levels, book.ask_levels, book.student_bid_levels, book.student_ask_levels)
    return math.gcd(*(level.price for levels in sides for level in levels))


def probe_price(book: Book, tick: int | None) -> tuple[int | None, str]:
    """The test buy's price and "", or None and why there is none.

    The price is the first one on the tick above the wall's best bid: the lowest at which a
    buy can rest, since an order rests only strictly inside the wall's best bid and ask. It
    must also be below every ask, the wall's and resting orders', so it cannot trade on
    arrival."""
    if book.condition != InstrumentCondition.LIVE or not book.bid_levels or not book.ask_levels:
        condition = name_of(InstrumentCondition, book.condition)
        return None, f"its quote is not two-sided and LIVE ({condition})"
    step = tick if tick is not None else price_step(book)
    if step <= 0:
        return None, "its book shows no positive price"
    wall_bid = book.bid_levels[0].price
    best_ask = min(
        levels[0].price for levels in (book.ask_levels, book.student_ask_levels) if levels
    )
    price = (wall_bid // step + 1) * step
    if price >= best_ask:
        return None, f"no room inside the band at a step of {to_decimal(step)}"
    return price, ""


def choose(
    watcher: Watcher, instruments: list[str], state: SessionState, tick: int | None
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
    if order.fully_filled:
        return PASS, (
            f"{order.where} was filled ({order.filled} share) before it could be cancelled: "
            "nothing is left resting, but your team now holds that position"
        )
    assert order.cancelled is not None
    code = order.cancelled.reason_code
    reason = reason_code_name(code)
    if code == ReasonCodes.SESSION_CLOSE:
        return SKIP, f"{order.where}: the session closed, which cancelled it ({reason})"
    rested = "rested, then was cancelled" if order.resting else "was not left resting"
    return FAIL, f"{order.where} {rested} by the exchange: {reason}; nothing is left resting"


async def cancel_level(watcher: Watcher, order: ProbeOrder, seconds: float) -> str | None:
    """Cancel the test order's level until the order leaves the book or the exchange reports
    nothing there. Returns None then, else why it could not confirm either."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    grid_points = 1
    while not order.gone:
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
            )
        ):
            return "the cancel could not be sent: the connection closed or stalled"
        answered = await watcher.until(
            lambda ref=ref: order.gone or isinstance(order.replies.get(ref), Reject), deadline
        )
        if order.gone:
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
            lambda target=target: order.gone or watcher.grid_points >= target, deadline
        )
    return None


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
        if not await watcher.sent(sending):
            return FAIL, (
                f"{order.where} could not be sent (the connection closed or stalled): it may be "
                "resting; check your team's orders and cancel that level yourself"
            )
    except ValueError as error:  # an identifier the contract does not allow; nothing sent
        order.sent = False
        return FAIL, f"not sent: {error}"
    await watcher.until(order.answered, loop.time() + seconds)
    if order.rejected is not None:
        return rejected_new(order)
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
    if order.confirmed:
        assert order.cancelled is not None
        if order.cancelled.strat_id != order.strat_id:
            return FAIL, (
                f"the cancel at {order.where} removed strategy {order.cancelled.strat_id}'s "
                "order there, not the test order: tell whoever runs that strategy"
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
        if order.filled:
            text += f"; {order.filled} share(s) filled first: your team now holds them"
        return PASS, text
    if order.gone:
        return left_alone(order)
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
        hint = "not asked for: --place-test-order --strat-id ID places and cancels one order"
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
    choice = choose(watcher, args.instruments, state, args.tick)
    if isinstance(choice, str):
        report.add(SKIP, name, f"nowhere to place it: {choice}")
        return
    instrument, price = choice
    order = ProbeOrder(instrument, args.strat_id, price)
    watcher.order = order
    reported = False
    try:
        status, reason = await place_and_cancel(watcher, order, args.seconds)
        report.add(status, name, reason)
        reported = True
    finally:
        if order.may_rest:
            where = "" if reported else f": {order.where}"
            print(
                f"WARNING: the test order may still be resting{where}. Check your team's "
                "orders and cancel that level yourself.",
                file=sys.stderr,
                flush=True,
            )


# History


async def first_past_book(
    client: HistoryClient, day: str, instrument: str, seconds: float
) -> tuple[str, str]:
    try:
        async with asyncio.timeout(seconds):
            async with aclosing(client.fetch(day, instrument, "book")) as items:
                async for item in items:
                    if isinstance(item, Book):
                        return PASS, f"session {day} served; its first book: {best_text(item)}"
                    if isinstance(item, Unknown | DecodeFailed):
                        kind = type(item).__name__
                        return FAIL, f"session {day}: a line could not be used ({kind})"
        return FAIL, f"session {day}: no books served"
    except HistoryPending:
        return SKIP, f"session {day} is not ready yet: try again later"
    except HistoryNotImplemented:
        return SKIP, f"session {day}: the service does not serve books yet"
    except HistoryError as error:
        return FAIL, f"session {day}: {type(error).__name__}: {error}"
    except TimeoutError:
        return FAIL, f"session {day}: no answer within {seconds:g} s"
    except Exception as error:  # a network failure; the SDK keeps the token out of it
        return FAIL, f"session {day}: {type(error).__name__}: {error}"


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
        # Never wait for data that is not ready yet: report it instead.
        client = HistoryClient(timeout=args.seconds, max_retries=0)
    except (ValueError, MissingToken) as error:
        report.add(FAIL, "history", f"{HISTORY_URL_ENV_VAR} cannot be used: {error}")
        return
    for instrument in instruments:
        status, reason = await first_past_book(
            client, closed.session_date, instrument, args.seconds
        )
        report.add(status, f"history:{instrument}", reason)


# Running it


def skip_after_connect(report: Report, reason: str) -> None:
    for name in ("calendar", "market", "account", "test-order", "history"):
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
            print("the connection did not close in time; it is dropped as the script exits")


async def session_checks(report: Report, watcher: Watcher, args: argparse.Namespace) -> None:
    loop = asyncio.get_running_loop()
    session = watcher.session
    # One subscribe per instrument, so one the exchange does not know cannot keep the
    # others from being served.
    for instrument in args.instruments:
        await watcher.sent(subscribe(session, [instrument]))
    watcher.account_ref = new_request_ref()
    await watcher.sent(send_account_query(session, request_ref=watcher.account_ref))
    # Watch for --seconds, applying every event as it arrives.
    await watcher.until(lambda: False, loop.time() + args.seconds)
    check_market(report, watcher, args.instruments, args.seconds)
    check_account(report, watcher, args.seconds)
    await check_test_order(report, watcher, args)
    watcher.drain()
    check_feed(report, watcher)


async def run(url: str, args: argparse.Namespace, report: Report) -> None:
    failure = None
    try:
        session = await open_session(url)
    except ContractVersionMismatch as error:
        failure = f"the exchange does not serve this SDK's contract version: {error}; update it"
    except SessionRejected as error:
        failure = f"the exchange refused the session: {error}"
    except Exception as error:  # the SDK keeps the token out of every error it raises
        failure = f"could not connect: {type(error).__name__}: {error}"
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
            await session_checks(report, watcher, args)
        finally:
            reader.cancel()
        known = [name for name in args.instruments if name not in watcher.unknown_instruments]
    await check_history(report, args, calendar, info.server_time, known)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = Report()
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
        print("interrupted", file=sys.stderr)
        return 130
    return report.finish()


if __name__ == "__main__":
    sys.exit(main())
