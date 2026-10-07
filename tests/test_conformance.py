"""The published conformance session, run against a local exchange.

`conformance/CONFORMANCE.md` is vendored byte for byte from the exchange's published
conformance steps. This module runs its WebSocket session (steps 1 to 16, with 1a and 9a
to 9c) through the public SDK API: `open_session`, `subscribe`, the `send_*` functions and
the message classes. Step 15 checks what the SDK hides from its user on purpose (it answers
every ping, absorbs heartbeats, parses payloads and drops a report it has already
delivered), so most of its sub-steps also drive connections frame by frame with the
`websockets` library, still building and reading messages with the SDK's codec and
contract types (see `Wire`). The history service steps (H1 to H19) are not run here.

It is skipped unless these are set, so CI and a plain `pytest` never reach an exchange:

    QTE_CONFORMANCE_URL         the exchange's WebSocket URL, for example ws://127.0.0.1:8080/ws
    QTE_CONFORMANCE_TOKEN       a token the exchange under test recognises
    QTE_CONFORMANCE_INSTRUMENT  the one instrument the script trades

There is no default URL. A URL whose host is not this machine (localhost or a loopback
address) is refused unless QTE_CONFORMANCE_ALLOW_REMOTE=1 is also set, because these steps
send real orders. The token is read from QTE_CONFORMANCE_TOKEN only, never QTE_TOKEN, so a
team token kept for trading is never used by accident.

Optional settings:

    QTE_CONFORMANCE_STRAT_A, QTE_CONFORMANCE_STRAT_B
        the two registered strategies (default strat-a and strat-b, as the steps name them)
    QTE_CONFORMANCE_TICK     the instrument's tick in dollars (default 0.01)
    QTE_CONFORMANCE_SIZE     the size of each resting order, at least 2 (default 10)
    QTE_CONFORMANCE_TIMEOUT  seconds to wait for each expected message (default 10); the
                             wait for a mark, which is published on a slower grid, is three
                             times this

Each numbered step is its own test with its own session, and step 15 is one test per
sub-step. Steps 4 to 14, and the sub-steps of step 15 that rest orders, start by mass
cancelling the team's resting orders, so a step that fails or is skipped leaves nothing
behind for the next. Steps 3 to 14 and those sub-steps need the instrument's session to be
open; if it is not, they are skipped. Step 15's precondition puts its restart before step
14's close and its empty marker after it, and step 16 needs no restart after that close,
so the tests run in this order: steps 1 to 13; step 15's heartbeat, silence, replay,
snapshot, restart and market data sub-steps; step 14; step 15's empty marker, and its
heartbeat and silence sub-steps again, now outside a session; step 16. The empty marker
and step 16 run only after step 14 has closed the session in the same run. Step 15's
heartbeat and silence sub-steps wait out the exchange's own timers, about three minutes
each time.

Every timestamp on the wire is a count of milliseconds since the Unix epoch, UTC, as the
vendored steps state. The steps only compare timestamps with each other, subtract one from
another, or add to a `receipt_time` the order delay learnt as `release_time` minus
`receipt_time`, so all of their timestamp arithmetic is in milliseconds. The waits are in
seconds of this machine's clock and are never compared with a timestamp.

The steps' preconditions are things the exchange under test provides, and some need its
operators. A step whose precondition cannot be met is skipped with the reason. These are
declared by setting a variable:

    QTE_CONFORMANCE_COUNTERPARTY=1
        steps 6 and 7: a scripted counterparty aggresses part of the step's resting buy.
    QTE_CONFORMANCE_RESTING_SELL=1
        step 9c: a counterparty of another team rests a sell of QTE_CONFORMANCE_SIZE shares
        strictly inside the band, above the two lowest buy prices inside it, with nothing
        else resting at or below it on the ask side.
    QTE_CONFORMANCE_WALL_ONLY=1
        step 11: no counterparty trades against the step's market order other than the
        wall, and the instrument's quote holds steady while the order waits out its delay.
        The team's risk limits must allow buying through ten ask levels, and the position
        this leaves is not closed by the test.
    QTE_CONFORMANCE_CLOSE_WITHIN=<seconds>
        step 14: the exchange runs a single configured session and closes it within this
        many seconds of the step resting its order.
    QTE_CONFORMANCE_RESTART_CMD=<command>
        step 15: a shell command that restarts the exchange under test on its own state,
        run once, between two sub-steps, before step 14's close. It returns once the
        exchange it restarts has stopped, and may return before the new one accepts
        connections; the sub-step then waits up to QTE_CONFORMANCE_RESTART_WITHIN
        seconds (default 120) for the command to finish and again for the exchange to
        accept a connection. The instrument's session must still be open after it.
    QTE_CONFORMANCE_RECORDED_SESSION=1
        step 16: the exchange is fed the recorded market session the published step names,
        holding one valid quote of QTEA, a bid of 99.99 and an ask of 100.01 at the session
        open, never replaced, and no quote of QTEB or QTEC. QTE_CONFORMANCE_INSTRUMENT must
        then be QTEA, or step 16 fails. Step 16 then checks that QTEA's `official_close`
        has the value 100000000 and that QTEB and QTEC get none. Without this variable it
        makes its other checks and then skips those. If the exchange rejects QTEB or QTEC
        as unknown, it makes every other check, QTEA's value included, and then skips.

The instrument must be an equity whose buy collar is mark x 1.05, the figure steps 10
and 11 name; an option's wider guard does not fit them.

The others are checked when the step runs: the instrument has a two-sided live quote
(steps 4 to 14), its book shows ten ask levels all within mark x 1.05 (step 11), the
sweep shifts the band so that a rebuilt book is published (step 11: a book is published
only when it changes, so a sweep whose impact is already at its clamp publishes none),
the order is still above mark x 1.05 at the mark in force at its release (step 10), step 6's
partial fill leaves at least two shares (step 7) and its spread leaves room for the
prices a step needs. Step 13's "no budget consumed" is not checked, since no message
reports a team's budget use. Step 12's resting sell for
the second strategy is entered by the step itself. Step 1a's resend of `instruments` after
a listing changes is not checked, since nothing here can change a listing. Step 15's
snapshots are of orders with no fills, so their `remaining_size` is checked only as the
whole order, and its replay is checked only for a cursor inside the window and one above
the newest report, not one older than the window, whose size the steps do not name.
"""

import asyncio
import ipaddress
import json
import logging
import math
import os
import re
from collections.abc import Callable
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlsplit

import pytest
from google.protobuf.message import Message
from websockets.client import ClientProtocol
from websockets.frames import Close, Frame, Opcode
from websockets.http11 import Response
from websockets.protocol import State
from websockets.uri import WebSocketURI, parse_uri

from qte_sdk.books import LatestBooks
from qte_sdk.calendar import next_open
from qte_sdk.connection import (
    DataUncertain,
    DecodeFailed,
    Received,
    ReportGap,
    ResumeComplete,
    SeqGap,
)
from qte_sdk.contract import codec
from qte_sdk.contract.registry import CONTRACT_VERSION, INBOUND
from qte_sdk.contract.v1.common_pb2 import (
    BUY,
    CLOSED,
    FILLED,
    LIMIT,
    MAKER,
    MARKET,
    OPEN,
    RESTING,
    SELL,
    STALE,
    STUDENT_TO_STUDENT,
    STUDENT_TO_WALL,
    TAKER,
    ReasonCodes,
    RequestType,
)
from qte_sdk.contract.v1.market_data_pb2 import (
    LIVE,
    Book,
    Mark,
    OfficialClose,
    SessionState,
    Trades,
)
from qte_sdk.contract.v1.order_events_pb2 import (
    Accepted,
    Execution,
    OrderCancelled,
    OrderState,
    Reject,
)
from qte_sdk.contract.v1.session_pb2 import CALL as RIGHT_CALL
from qte_sdk.contract.v1.session_pb2 import PUT as RIGHT_PUT
from qte_sdk.contract.v1.session_pb2 import Auth, OrderSnapshot, Resume, ResumeAck, Subscribe
from qte_sdk.instruments import EQUITY, OPTION, InstrumentInfo, instrument_info
from qte_sdk.market_data import as_market_data, subscribe
from qte_sdk.options import CALL as CALL_LETTER
from qte_sdk.options import PUT as PUT_LETTER
from qte_sdk.options import option_underlyings, parse_option_symbol, strike_increment
from qte_sdk.orders import (
    reason_code_name,
    request_ref_of,
    send_amend,
    send_cancel,
    send_mass_cancel,
    send_new,
)
from qte_sdk.session import Session, open_session
from qte_sdk.units import to_micros

URL_VAR = "QTE_CONFORMANCE_URL"
TOKEN_VAR = "QTE_CONFORMANCE_TOKEN"
INSTRUMENT_VAR = "QTE_CONFORMANCE_INSTRUMENT"

pytestmark = pytest.mark.skipif(
    not all(os.environ.get(name) for name in (URL_VAR, TOKEN_VAR, INSTRUMENT_VAR)),
    reason=f"conformance session: set {URL_VAR}, {TOKEN_VAR} and {INSTRUMENT_VAR} to run it",
)

# Step 10 names the collar as a buy limit above mark x 1.05. This is the figure the vendored
# steps require of the exchange under test, not a value trading code may rely on: take it
# from CONFORMANCE.md again whenever that file is re-vendored.
STEP_10_MARK_FACTOR = (105, 100)

# Step 16 names the official close its recorded session must give: QTEA's is the midpoint of
# its one quote, 99.99 and 100.01, and QTEB and QTEC, never quoted, get none. These too are
# what the vendored steps require, so take them from CONFORMANCE.md again on re-vendoring.
STEP_16_QUOTED = "QTEA"
STEP_16_OFFICIAL_CLOSE = 100_000_000  # 100.000000 in micro-dollars
STEP_16_UNQUOTED = ("QTEB", "QTEC")


def old_price(state: OrderState) -> int | None:
    """The order's price before the amend this `order_state` reports, or None."""
    return state.old_price if state.HasField("old_price") else None


@dataclass(frozen=True)
class Settings:
    url: str
    instrument: str
    strat_a: str
    strat_b: str
    tick: int
    size: int
    timeout: float


def settings() -> Settings:
    url = os.environ[URL_VAR]
    host = urlsplit(url).hostname or ""
    if os.environ.get("QTE_CONFORMANCE_ALLOW_REMOTE") != "1" and not _is_local(host):
        pytest.skip(
            f"{URL_VAR} does not name this machine; set QTE_CONFORMANCE_ALLOW_REMOTE=1 "
            "to run the conformance session against another exchange"
        )
    size = int(os.environ.get("QTE_CONFORMANCE_SIZE", "10"))
    if size < 2:
        pytest.fail("QTE_CONFORMANCE_SIZE must be at least 2")
    return Settings(
        url=url,
        instrument=os.environ[INSTRUMENT_VAR],
        strat_a=os.environ.get("QTE_CONFORMANCE_STRAT_A", "strat-a"),
        strat_b=os.environ.get("QTE_CONFORMANCE_STRAT_B", "strat-b"),
        tick=to_micros(os.environ.get("QTE_CONFORMANCE_TICK", "0.01")),
        size=size,
        timeout=float(os.environ.get("QTE_CONFORMANCE_TIMEOUT", "10")),
    )


def _is_local(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# What one step learns that a later step checks: the order delay every `accepted` shows,
# and the session step 14 closed.
_ORDER_DELAY: list[int] = []
_CLOSED_BY_STEP_14: list[SessionState] = []

_END = object()


class NoMessage(AssertionError):
    """An expected message did not arrive in time."""


class Client:
    """One session, read by one task, with everything it has received kept in order.

    Each step notes `mark()` before it sends, then waits for messages from that point, so a
    message that arrives before the step starts waiting for it is still found.
    `report_seqs` holds each message's envelope `report_seq`, None where it has none.
    """

    def __init__(self, session: Session, config: Settings) -> None:
        self.session = session
        self.config = config
        self.seen: list[Message] = []
        self.report_seqs: list[int | None] = []
        self.books = LatestBooks()
        self.state: SessionState | None = None
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self._failure: BaseException | None = None
        self._ended = False
        self._reader = asyncio.create_task(self._read())

    @classmethod
    async def connect(cls, config: Settings) -> "Client":
        session = await open_session(config.url, os.environ[TOKEN_VAR])
        # Read before the reader starts: the calendar and then the instruments table
        # follow the acknowledgement.
        await session.wait_for_calendar(config.timeout)
        await session.wait_for_instrument_table(config.timeout)
        return cls(session, config)

    async def _read(self) -> None:
        try:
            async for event in self.session:
                await self._queue.put(event)
        except Exception as error:
            self._failure = error
        finally:
            await self._queue.put(_END)

    async def close(self) -> None:
        self._reader.cancel()
        try:
            await self._reader
        except asyncio.CancelledError:
            pass
        await self.session.close()

    def mark(self) -> int:
        return len(self.seen)

    async def _pull(self, timeout: float) -> bool:
        """Take one event off the session into `seen`. False if none came in time."""
        if self._ended:
            failure = f" ({type(self._failure).__name__})" if self._failure else ""
            raise AssertionError(f"the connection ended{failure}")
        try:
            async with asyncio.timeout(timeout):
                event = await self._queue.get()
        except TimeoutError:
            return False
        if event is _END:
            self._ended = True
            return await self._pull(0)
        if isinstance(event, SeqGap | ReportGap | DecodeFailed):
            raise AssertionError(f"messages were missed or unreadable: {event!r}")
        if not isinstance(event, Received):
            return True
        item = as_market_data(event)
        self.books.update(item)
        if isinstance(item, SessionState):
            self.state = item
        self.seen.append(event.message)
        self.report_seqs.append(event.report_seq)
        return True

    async def wait_for(
        self,
        match: Callable[[Message], bool],
        what: str,
        since: int,
        timeout: float | None = None,
    ) -> Message:
        """The first message from index `since` on that `match` accepts."""
        timeout = self.config.timeout if timeout is None else timeout
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        index = since
        while True:
            while index < len(self.seen):
                message = self.seen[index]
                index += 1
                if match(message):
                    return message
            remaining = deadline - loop.time()
            if remaining <= 0 or not await self._pull(remaining):
                raise NoMessage(f"no {what} within {timeout} s")

    async def until(
        self, done: Callable[[], bool], what: str, timeout: float | None = None
    ) -> None:
        """Read until `done()` is true, with one deadline for the whole wait."""
        timeout = self.config.timeout if timeout is None else timeout
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not done():
            remaining = deadline - loop.time()
            if remaining <= 0 or not await self._pull(remaining):
                raise NoMessage(f"no {what} within {timeout} s")

    async def drain(self, seconds: float) -> None:
        """Keep reading for `seconds`, so later checks see everything sent meanwhile."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while (remaining := deadline - loop.time()) > 0:
            await self._pull(remaining)

    def after(self, message: Message) -> int:
        """The index just past `message` in `seen`."""
        return next(i for i, m in enumerate(self.seen) if m is message) + 1

    def report_seq_of(self, message: Message) -> int | None:
        """The envelope `report_seq` that `message` arrived with, or None."""
        return self.report_seqs[self.after(message) - 1]

    def newest_report_seq(self) -> int | None:
        """The highest `report_seq` this session has received, or None."""
        return max((n for n in self.report_seqs if n is not None), default=None)

    def since(self, index: int, kind: type[Message]) -> list:
        return [m for m in self.seen[index:] if isinstance(m, kind)]

    async def next_session_state(self, since: int) -> SessionState:
        return await self.wait_for(lambda m: isinstance(m, SessionState), "session_state", since)

    async def answer(self, ref: str, since: int) -> Accepted:
        """The `accepted` for `ref`; fails, naming the reason, if it is rejected instead."""
        reply = await self.wait_for(
            lambda m: isinstance(m, Accepted | Reject) and request_ref_of(m) == ref,
            "accepted or reject for the request",
            since,
        )
        if isinstance(reply, Reject):
            pytest.fail(f"request rejected: {reason_code_name(reply.reason_code)}")
        return reply

    async def rejection(self, ref: str, since: int) -> Reject:
        """The `reject` for `ref`; fails if it is accepted instead."""
        reply = await self.wait_for(
            lambda m: isinstance(m, Accepted | Reject) and request_ref_of(m) == ref,
            "accepted or reject for the request",
            since,
        )
        assert isinstance(reply, Reject), "the request was accepted, not rejected"
        return reply

    async def order_state(self, strat: str, side: int, price: int, since: int) -> OrderState:
        return await self.wait_for(
            lambda m: (
                isinstance(m, OrderState)
                and m.strat_id == strat
                and m.instrument == self.config.instrument
                and m.side == side
                and m.price == price
            ),
            f"order_state for {strat} at the level",
            since,
        )

    async def rest(self, strat: str, side: int, price: int, size: int | None = None) -> OrderState:
        """Enter a limit order and wait until it is reported resting."""
        size = self.config.size if size is None else size
        start = self.mark()
        ref = await send_new(
            self.session,
            strat_id=strat,
            instrument=self.config.instrument,
            side=side,
            order_type=LIMIT,
            price=price,
            size=size,
        )
        await self.answer(ref, start)
        state = await self.order_state(strat, side, price, start)
        assert state.state == RESTING
        return state


def check_release_time(accepted: Accepted) -> None:
    """`release_time` is `receipt_time` plus the order delay, the same on every `accepted`
    the steps check (4, 5 and 13).

    The delay is the exchange's setting, so it is learnt from the first `accepted` checked
    rather than assumed. Like both timestamps, it is in milliseconds.
    """
    delay = accepted.release_time - accepted.receipt_time
    assert delay > 0, "release_time is not after receipt_time"
    if not _ORDER_DELAY:
        _ORDER_DELAY.append(delay)
    assert delay == _ORDER_DELAY[0], "release_time minus receipt_time differs between orders"


def assert_ladder(book: Book) -> None:
    """The wall ladder: up to ten levels a side, best first, every level at least one
    share, and both sides spaced uniformly by the asks' own step.

    How many levels a side shows is the exchange's setting, one number for both sides, so
    the ask ladder shows that many. The bid ladder of a low-priced instrument stops at its
    last level with a positive price, so fewer bids than asks are accepted only when one
    more step below the last bid would not be positive.
    """
    asks = [level.price for level in book.ask_levels]
    bids = [level.price for level in book.bid_levels]
    assert 1 <= len(asks) <= 10, f"{len(asks)} ask levels"
    assert 1 <= len(bids) <= len(asks), f"{len(bids)} bid levels and {len(asks)} ask levels"
    assert bids[0] < asks[0], "the bid ladder crosses the ask ladder"
    assert bids[-1] > 0, "a bid level without a positive price"
    if len(asks) > 1:
        step = asks[1] - asks[0]
        assert step > 0, "asks not best first"
        assert all(b - a == step for a, b in zip(asks[:-1], asks[1:], strict=True)), (
            "asks not evenly spaced"
        )
        assert all(a - b == step for a, b in zip(bids[:-1], bids[1:], strict=True)), (
            "bids not evenly spaced"
        )
        if len(bids) < len(asks):
            assert bids[-1] - step <= 0, (
                f"{len(bids)} bid levels and {len(asks)} ask levels, though the next bid "
                "would be positive"
            )
    sizes = [level.size for level in [*book.bid_levels, *book.ask_levels]]
    assert all(size >= 1 for size in sizes), "a level shows less than one share"


def inside_prices(book: Book, tick: int, count: int) -> list[int]:
    """`count` buy prices strictly inside the band, from the best bid up, and below every
    resting sell, so none is marketable. Skips the step if the spread is too narrow."""
    best_bid = book.bid_levels[0].price
    best_ask = min([book.ask_levels[0].price] + [level.price for level in book.student_ask_levels])
    first = (best_bid // tick + 1) * tick
    prices = [first + k * tick for k in range(count)]
    if prices[-1] >= best_ask:
        pytest.skip(f"precondition: the spread leaves no room for {count} prices inside the band")
    return prices


@pytest.fixture
async def client():
    c = await Client.connect(settings())
    try:
        yield c
    finally:
        await c.close()


async def subscribed(c: Client) -> SessionState:
    """Subscribe to the instrument and return the first `session_state` that follows."""
    start = c.mark()
    await subscribe(c.session, [c.config.instrument])
    return await c.next_session_state(start)


async def open_book(c: Client) -> Book:
    """Subscribe and wait for the instrument's book, skipping the step unless the session
    is open and the instrument has a two-sided live quote."""
    start = c.mark()
    state = await subscribed(c)
    if state.state != OPEN:
        pytest.skip("precondition: the instrument's session is open")
    try:
        await c.wait_for(
            lambda m: isinstance(m, Book) and m.instrument == c.config.instrument,
            "book",
            start,
        )
    except NoMessage:
        pytest.skip("precondition: the instrument has a book")
    book = c.books.get(c.config.instrument)
    assert book is not None
    if book.condition != LIVE or not book.bid_levels or not book.ask_levels:
        pytest.skip("precondition: the instrument has a two-sided live quote")
    return book


@pytest.fixture
async def market(client: Client):
    """An open session on the instrument with none of the team's orders resting."""
    await open_book(client)
    await mass_cancel_all(client)
    yield client
    # Best effort: the step's own result stands whether or not this clean-up succeeds.
    if client.state is not None and client.state.state == OPEN and not client._ended:
        try:
            await mass_cancel_all(client)
        except (Exception, pytest.fail.Exception):
            pass


async def mass_cancel_all(c: Client) -> None:
    """Mass cancel and read on to the next grid point, so its cancellations are seen."""
    start = c.mark()
    ref = await send_mass_cancel(c.session)
    accepted = await c.answer(ref, start)
    after = c.after(accepted)
    await c.until(
        lambda: any(s.grid_time >= accepted.release_time for s in c.since(after, SessionState)),
        "session_state after the mass cancel's release",
    )


# Steps 1 to 3: the session layer.


async def test_step_01_connect_and_authenticate(client: Client):
    assert client.session.info.team, "session_ack names no team"
    calendar = client.session.calendar
    assert calendar is not None, "no calendar after session_ack"
    assert calendar.sessions, "the calendar lists no sessions"


# Shares of the underlying per option contract, as the vendored contract's `OptionTerms`
# states. Like the figures the steps name, take it from there again on re-vendoring.
OPTION_MULTIPLIER = 100
# An `OptionTerms.right` and the letter of an OCC option symbol that names it.
_SYMBOL_RIGHTS = {RIGHT_CALL: CALL_LETTER, RIGHT_PUT: PUT_LETTER}
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def assert_option_entry(info: InstrumentInfo) -> None:
    """Step 1a's `OPTION` entry: an id in Alpaca's unpadded OCC form and `option` terms
    with every field set, which agree with the id. The id's root is the underlying, its
    YYMMDD the expiry, its C or P the right, and its eight digits the strike in thousandths
    of a dollar, which `parse_option_symbol` gives in micro-dollars like `strike`."""
    name = info.instrument
    try:
        symbol = parse_option_symbol(name)
    except ValueError:
        raise AssertionError(f"{name} is not an unpadded OCC option symbol") from None
    assert info.HasField("option"), f"option {name} carries no option terms"
    terms = info.option
    assert terms.underlying, f"option {name} has no underlying"
    assert terms.expiry, f"option {name} has no expiry"
    assert terms.right in _SYMBOL_RIGHTS, f"option {name} has no right, CALL or PUT"
    assert terms.strike > 0, f"option {name} has no positive strike"
    assert terms.multiplier == OPTION_MULTIPLIER, (
        f"option {name} has a multiplier of {terms.multiplier}, not {OPTION_MULTIPLIER}"
    )
    assert terms.underlying == symbol.underlying, f"{name}'s underlying is {terms.underlying}"
    assert _ISO_DATE.fullmatch(terms.expiry), f"{name}'s expiry is not an ISO 8601 date"
    try:
        expiry = date.fromisoformat(terms.expiry)
    except ValueError:
        raise AssertionError(f"{name}'s expiry is not a real date") from None
    assert expiry == symbol.expiry, f"{name}'s expiry is {terms.expiry}"
    assert _SYMBOL_RIGHTS[terms.right] == symbol.right, f"{name}'s right does not match"
    assert terms.strike == symbol.strike, f"{name}'s strike is {terms.strike} micro-dollars"


# The fields step 1a names on each instrument, which must be on the wire even at their
# zero value: `tradable` "present, `false` included".
STEP_01A_FIELDS = ("kind", "tick_size", "lot_size", "status", "tradable")
# Step 1a names the option underlyings every exchange lists. Like the figures of steps 10
# and 16, take them from CONFORMANCE.md again whenever that file is re-vendored.
STEP_01A_UNDERLYINGS = ("SPY", "GOOGL")


async def test_step_01a_instruments():
    config = settings()
    session = await open_session(config.url, os.environ[TOKEN_VAR])
    events: list = []
    try:
        # The session is read directly, rather than through `Client`, to see each
        # message's `seq` and the payload as received.
        async with asyncio.timeout(config.timeout):
            async for event in session:
                events.append(event)
                if isinstance(event, Received) and event.type == "instruments":
                    break
    except TimeoutError:
        pytest.fail(f"no instruments within {config.timeout} s of session_ack")
    finally:
        await session.close()
    calendars = [
        i for i, e in enumerate(events) if isinstance(e, Received) and e.type == "calendar"
    ]
    assert calendars, "no calendar before instruments"
    *_, table_event = events
    between = events[calendars[-1] + 1 : -1]
    assert not between, f"messages between calendar and instruments: {between!r}"
    calendar_event = events[calendars[-1]]
    # A heartbeat between them is absorbed by the SDK but still takes a `seq`.
    assert table_event.seq == calendar_event.seq + 1, "instruments is not the next seq"
    table = table_event.message
    assert table is session.instrument_table
    ids = [info.instrument for info in table.instruments]
    assert ids, "the instruments table lists no instrument"
    assert all(a.encode() < b.encode() for a, b in zip(ids[:-1], ids[1:], strict=True)), (
        "instruments not sorted by the byte order of instrument, or one listed twice"
    )
    for info, entry in zip(table.instruments, table_event.payload["instruments"], strict=True):
        missing = [name for name in STEP_01A_FIELDS if name not in entry]
        assert not missing, f"{info.instrument} has no {', '.join(missing)}"
        if info.kind == OPTION:
            assert_option_entry(info)
        else:
            assert info.kind == EQUITY, f"{info.instrument} is neither EQUITY nor OPTION"
            assert not info.HasField("option"), f"equity {info.instrument} carries option terms"
    raw_underlyings = {
        entry.get("underlying"): entry
        for entry in table_event.payload.get("option_underlyings", [])
    }
    for underlying in STEP_01A_UNDERLYINGS:
        assert underlying in option_underlyings(table), (
            f"option_underlyings does not name {underlying}"
        )
        entry = raw_underlyings[underlying]
        assert "strike_increment" in entry, f"{underlying} has no strike_increment"
        assert "contracts" in entry, f"{underlying} has no contracts"
        assert strike_increment(table, underlying) > 0, f"{underlying}'s strike_increment"
    # Not checked: that a client replaces its table with each `instruments` the exchange
    # resends, since a resend needs a listing to change.
    if instrument_info(table, config.instrument) is None:
        pytest.skip(f"precondition: the exchange lists the instrument {config.instrument}")


async def test_step_02_subscribe(client: Client):
    # Step 1a: a client subscribes to an instrument from the exchange's table.
    table = client.session.instrument_table
    assert table is not None, "no instruments after the calendar"
    if instrument_info(table, client.config.instrument) is None:
        pytest.skip(f"precondition: the exchange lists the instrument {client.config.instrument}")
    start = client.mark()
    await subscribe(client.session, [client.config.instrument])
    first = await client.wait_for(
        lambda m: isinstance(m, SessionState | Book | Reject), "reply to subscribe", start
    )
    assert not isinstance(first, Reject), (
        f"subscribe rejected: {reason_code_name(first.reason_code)}"
    )


async def test_step_03_first_book(client: Client):
    book = await open_book(client)
    assert book.grid_time > 0
    assert_ladder(book)


# Steps 4 to 13: order entry during the session. Step 14 uses the same set-up.


async def test_step_04_new_limit_order(market: Client):
    c = market
    (price,) = inside_prices(c.books.get(c.config.instrument), c.config.tick, 1)
    start = c.mark()
    ref = await send_new(
        c.session,
        strat_id=c.config.strat_a,
        instrument=c.config.instrument,
        side=BUY,
        order_type=LIMIT,
        price=price,
        size=c.config.size,
    )
    accepted = await c.answer(ref, start)
    assert accepted.request_type == RequestType.NEW
    check_release_time(accepted)
    state = await c.order_state(c.config.strat_a, BUY, price, start)
    assert state.state == RESTING
    assert state.remaining_size == c.config.size


async def test_step_05_early_cancel(market: Client):
    c = market
    (price,) = inside_prices(c.books.get(c.config.instrument), c.config.tick, 1)
    start = c.mark()
    new_ref = await send_new(
        c.session,
        strat_id=c.config.strat_a,
        instrument=c.config.instrument,
        side=BUY,
        order_type=LIMIT,
        price=price,
        size=c.config.size,
    )
    # Sent at once, without reading anything, so it is applied inside the minimum rest.
    cancel_ref = await send_cancel(c.session, instrument=c.config.instrument, side=BUY, price=price)
    reject = await c.rejection(cancel_ref, start)
    assert reject.reason_code == ReasonCodes.MIN_REST_VIOLATION, reason_code_name(
        reject.reason_code
    )
    assert reject.request_type == RequestType.CANCEL
    # Step 4's confirmations, checked only now.
    check_release_time(await c.answer(new_ref, start))
    state = await c.order_state(c.config.strat_a, BUY, price, start)
    assert state.state == RESTING


NEEDS_COUNTERPARTY = pytest.mark.skipif(
    os.environ.get("QTE_CONFORMANCE_COUNTERPARTY") != "1",
    reason="precondition: a scripted counterparty (QTE_CONFORMANCE_COUNTERPARTY=1)",
)


async def partial_fill(c: Client, price: int) -> Execution:
    """Rest a buy at `price` and wait for the scripted counterparty's first fill of it."""
    start = c.mark()
    await c.rest(c.config.strat_a, BUY, price)
    return await c.wait_for(
        lambda m: (
            isinstance(m, Execution)
            and m.strat_id == c.config.strat_a
            and m.instrument == c.config.instrument
            and m.side == BUY
            and m.HasField("order_price")
            and m.order_price == price
        ),
        "execution against the resting order",
        start,
    )


@NEEDS_COUNTERPARTY
async def test_step_06_partial_fill(market: Client):
    c = market
    (price,) = inside_prices(c.books.get(c.config.instrument), c.config.tick, 1)
    fill = await partial_fill(c, price)
    assert fill.fill_kind == STUDENT_TO_STUDENT
    assert fill.liquidity == MAKER
    assert fill.fee > 0, "the maker's fee is not a rebate"
    assert fill.remaining_size > 0, "the fill was not partial"
    # The print belongs on the first grid point at or after the fill. Its `trades` and
    # `session_state` may arrive in either order, so both are looked for from the start.
    start = c.after(fill) - 1
    boundary = await c.wait_for(
        lambda m: isinstance(m, SessionState) and m.grid_time >= fill.timestamp,
        "session_state at the grid point after the fill",
        0,
    )
    await c.wait_for(
        lambda m: (
            isinstance(m, Trades)
            and m.instrument == c.config.instrument
            and m.grid_time == boundary.grid_time
            and any(
                p.kind == STUDENT_TO_STUDENT
                and p.price == fill.fill_price
                and p.size == fill.fill_size
                for p in m.prints
            )
        ),
        "trades print for the fill at the next grid point",
        start,
    )


@NEEDS_COUNTERPARTY
async def test_step_07_amend_down(market: Client):
    # After step 6's partial fill, so that `new_size` read as the new total size and as the
    # new remaining size give different answers, and only the remaining size passes.
    c = market
    (price,) = inside_prices(c.books.get(c.config.instrument), c.config.tick, 1)
    fill = await partial_fill(c, price)
    if fill.remaining_size < 2:
        pytest.skip("precondition: step 6's partial fill leaves at least 2 shares to amend down")
    new_size = fill.remaining_size // 2
    start = c.mark()
    ref = await send_amend(
        c.session,
        instrument=c.config.instrument,
        side=BUY,
        price=price,
        new_price=price,
        new_size=new_size,
    )
    accepted = await c.answer(ref, start)
    assert accepted.request_type == RequestType.AMEND
    state = await c.order_state(c.config.strat_a, BUY, price, c.after(accepted))
    assert state.remaining_size == new_size
    assert old_price(state) == price


async def test_step_08_second_strategy_at_the_same_price(market: Client):
    c = market
    (price,) = inside_prices(c.books.get(c.config.instrument), c.config.tick, 1)
    rested = c.mark()
    await c.rest(c.config.strat_a, BUY, price)
    start = c.mark()
    ref = await send_new(
        c.session,
        strat_id=c.config.strat_b,
        instrument=c.config.instrument,
        side=BUY,
        order_type=LIMIT,
        price=price,
        size=c.config.size,
    )
    reject = await c.rejection(ref, start)
    assert reject.reason_code == ReasonCodes.DUPLICATE_ORDER_AT_LEVEL, reason_code_name(
        reject.reason_code
    )
    await c.next_session_state(c.after(reject))
    assert not [m for m in c.since(start, Accepted) if m.request_ref == ref]
    assert not [
        m
        for m in c.since(start, OrderState)
        if m.strat_id == c.config.strat_b and m.side == BUY and m.price == price
    ], "an order rests for the second strategy"
    assert not [
        m
        for m in c.since(rested, OrderCancelled)
        if m.strat_id == c.config.strat_a and m.price == price
    ], "the first strategy's order was cancelled"
    assert not [
        m
        for m in c.since(rested, Execution)
        if m.strat_id == c.config.strat_a and m.order_price == price
    ], "the first strategy's order was filled"
    assert all(
        m.state == RESTING and m.remaining_size == c.config.size
        for m in c.since(rested, OrderState)
        if m.strat_id == c.config.strat_a and m.side == BUY and m.price == price
    ), "the first strategy's order changed"


async def test_step_09_cancel_the_level(market: Client):
    c = market
    (price,) = inside_prices(c.books.get(c.config.instrument), c.config.tick, 1)
    await c.rest(c.config.strat_a, BUY, price)
    start = c.mark()
    amend = await send_amend(
        c.session,
        instrument=c.config.instrument,
        side=BUY,
        price=price,
        new_size=c.config.size // 2,
    )
    accepted = await c.answer(amend, start)
    state = await c.order_state(c.config.strat_a, BUY, price, c.after(accepted))
    start = c.mark()
    ref = await send_cancel(c.session, instrument=c.config.instrument, side=BUY, price=price)
    cancelled = await c.wait_for(
        lambda m: isinstance(m, OrderCancelled) and request_ref_of(m) == ref,
        "order_cancelled for the cancel",
        start,
    )
    assert cancelled.strat_id == c.config.strat_a
    assert cancelled.reason_code == ReasonCodes.CANCEL_REQUEST
    assert cancelled.cancelled_size == state.remaining_size
    await c.next_session_state(c.after(cancelled))
    assert len([m for m in c.since(start, OrderCancelled) if request_ref_of(m) == ref]) == 1


class OwnOrders:
    """The client's own view of its resting buys, kept by step 9's rule from outbound
    messages alone: remove the order at `old_price`, then add it at `price` if it is
    `RESTING` or `STALE`."""

    def __init__(self) -> None:
        self.prices: set[int] = set()

    def apply(self, state: OrderState) -> None:
        previous = old_price(state)
        if previous is not None:
            self.prices.discard(previous)
        if state.state in (RESTING, STALE):
            self.prices.add(state.price)
        else:
            self.prices.discard(state.price)


async def amend_response(c: Client, ref: str, start: int) -> tuple[list[OrderState], list]:
    """Everything the amend `ref` caused, read through the first grid point after the
    `accepted`: the strategy's `order_state` messages and its executions, in order."""
    accepted = await c.answer(ref, start)
    assert accepted.request_type == RequestType.AMEND
    await c.until(
        lambda: any(
            m.grid_time > accepted.release_time for m in c.since(c.after(accepted), SessionState)
        ),
        "session_state after the amend's release",
    )
    states = [
        m
        for m in c.since(start, OrderState)
        if m.strat_id == c.config.strat_a and m.instrument == c.config.instrument
    ]
    events = [
        m
        for m in c.seen[start:]
        if isinstance(m, OrderState | Execution)
        and m.strat_id == c.config.strat_a
        and m.instrument == c.config.instrument
    ]
    return states, events


async def rest_and_move(c: Client, low: int, high: int) -> tuple[OrderState, OrderState]:
    """Steps 9a and 9b: rest a buy at `low`, then amend it to `high`. Returns the
    `order_state` of each, leaving `old_price` for the caller to check last."""
    rested = await c.rest(c.config.strat_a, BUY, low)
    start = c.mark()
    ref = await send_amend(
        c.session,
        instrument=c.config.instrument,
        side=BUY,
        price=low,
        new_price=high,
        new_size=c.config.size,
    )
    states, events = await amend_response(c, ref, start)
    assert len(states) == 1, f"the amend sent {len(states)} order_state messages, not one"
    (moved,) = states
    assert not [m for m in events if isinstance(m, Execution)], "the amend executed"
    assert moved.price == high
    assert moved.side == BUY
    assert moved.state == RESTING
    assert moved.remaining_size == c.config.size
    return rested, moved


async def test_step_09a_rest_for_the_amend(market: Client):
    c = market
    (low,) = inside_prices(c.books.get(c.config.instrument), c.config.tick, 1)
    state = await c.rest(c.config.strat_a, BUY, low)
    assert old_price(state) is None, "order_state for a new order carries old_price"


async def test_step_09b_price_moving_amend_resting(market: Client):
    c = market
    low, high = inside_prices(c.books.get(c.config.instrument), c.config.tick, 2)
    rested, moved = await rest_and_move(c, low, high)
    assert old_price(rested) is None, "order_state for a new order carries old_price"
    assert old_price(moved) == low
    own = OwnOrders()
    own.apply(rested)
    own.apply(moved)
    assert own.prices == {high}


@pytest.mark.skipif(
    os.environ.get("QTE_CONFORMANCE_RESTING_SELL") != "1",
    reason="precondition: another team rests a sell for the amend to meet "
    "(QTE_CONFORMANCE_RESTING_SELL=1)",
)
async def test_step_09c_price_moving_amend_filling_completely(market: Client):
    c = market
    low, high = inside_prices(c.books.get(c.config.instrument), c.config.tick, 2)
    rested, moved = await rest_and_move(c, low, high)

    def counterparty_sell() -> int | None:
        book = c.books.get(c.config.instrument)
        if book is None or not book.student_ask_levels:
            return None
        lowest = min(book.student_ask_levels, key=lambda level: level.price)
        if not high < lowest.price < book.ask_levels[0].price:
            return None
        if lowest.size != c.config.size:
            return None
        return lowest.price

    try:
        await c.until(lambda: counterparty_sell() is not None, "counterparty sell in the book")
    except NoMessage:
        pytest.skip(
            "precondition: another team rests a sell of QTE_CONFORMANCE_SIZE shares inside "
            "the band above the step's prices, with nothing else resting at or below it"
        )
    target = counterparty_sell()
    assert target is not None
    start = c.mark()
    ref = await send_amend(
        c.session,
        instrument=c.config.instrument,
        side=BUY,
        price=high,
        new_price=target,
        new_size=c.config.size,
    )
    states, events = await amend_response(c, ref, start)
    assert len(states) == 1, f"the amend sent {len(states)} order_state messages, not one"
    (filled,) = states
    fills = [m for m in events if isinstance(m, Execution)]
    assert fills, "the amend did not execute"
    assert events[-1] is filled, "an execution came after the amend's order_state"
    assert all(f.side == BUY and f.liquidity == TAKER for f in fills)
    assert fills[-1].remaining_size == 0
    assert filled.side == BUY
    assert filled.price == target
    assert filled.state == FILLED
    assert filled.remaining_size == 0
    assert old_price(rested) is None
    assert old_price(moved) == low
    assert old_price(filled) == high
    own = OwnOrders()
    for state in (rested, moved, filled):
        own.apply(state)
    assert not own.prices, "an order of the team rests at the old or new price"


async def test_step_10_collar(market: Client):
    c = market
    await c.wait_for(
        lambda m: isinstance(m, Mark) and m.instrument == c.config.instrument,
        "mark",
        0,
        timeout=3 * c.config.timeout,
    )

    def marks() -> list[Mark]:
        return [m for m in c.seen if isinstance(m, Mark) and m.instrument == c.config.instrument]

    numerator, denominator = STEP_10_MARK_FACTOR
    tick = c.config.tick
    # Priced from the latest mark; the collar applies at the mark in force at release.
    price = (marks()[-1].value * numerator // (denominator * tick) + 1) * tick
    start = c.mark()
    ref = await send_new(
        c.session,
        strat_id=c.config.strat_a,
        instrument=c.config.instrument,
        side=BUY,
        order_type=LIMIT,
        price=price,
        size=c.config.size,
    )
    reply = await c.wait_for(
        lambda m: isinstance(m, Accepted | Reject) and request_ref_of(m) == ref,
        "accepted or reject for the request",
        start,
    )
    # A reject carries no release time: it is its receipt time plus the order delay, which
    # the set-up's mass cancel `accepted` already showed. All three are timestamps, so the
    # delay added is in milliseconds, as the vendored steps require.
    if isinstance(reply, Accepted):
        release = reply.release_time
    else:
        earlier = [m for m in c.seen[:start] if isinstance(m, Accepted)]
        assert earlier, "no accepted to take the order delay from"
        release = reply.receipt_time + (earlier[-1].release_time - earlier[-1].receipt_time)
    await c.until(
        lambda: any(m.grid_time > release for m in c.since(start, SessionState)),
        "session_state after the order's release",
    )
    in_force = [m for m in marks() if m.sampled_at <= release]
    if in_force and price * denominator <= in_force[-1].value * numerator:
        pytest.skip("precondition: the order is still above mark x 1.05 at the mark in force")
    assert isinstance(reply, Reject), "the order above mark x 1.05 was accepted"
    assert reply.reason_code == ReasonCodes.PRICE_COLLAR, reason_code_name(reply.reason_code)


@pytest.mark.skipif(
    os.environ.get("QTE_CONFORMANCE_WALL_ONLY") != "1",
    reason="precondition: only the wall trades against the order (QTE_CONFORMANCE_WALL_ONLY=1)",
)
async def test_step_11_wall_sweep_with_a_market_order(market: Client):
    c = market
    if len(c.books.get(c.config.instrument).ask_levels) != 10:
        pytest.skip("precondition: the instrument's book shows ten ask levels")
    await c.wait_for(
        lambda m: isinstance(m, Mark) and m.instrument == c.config.instrument,
        "mark",
        0,
        timeout=3 * c.config.timeout,
    )
    numerator, denominator = STEP_10_MARK_FACTOR

    def marks() -> list[Mark]:
        return [m for m in c.seen if isinstance(m, Mark) and m.instrument == c.config.instrument]

    def within_guard(book: Book, mark: Mark) -> bool:
        return book.ask_levels[-1].price * denominator <= mark.value * numerator

    shown = c.books.get(c.config.instrument)
    if not within_guard(shown, marks()[-1]):
        pytest.skip("precondition: all ten ask levels lie within mark x 1.05, the market guard")
    size = sum(level.size for level in shown.ask_levels) + 1
    start = c.mark()
    ref = await send_new(
        c.session,
        strat_id=c.config.strat_a,
        instrument=c.config.instrument,
        side=BUY,
        order_type=MARKET,
        size=size,
    )
    accepted = await c.answer(ref, start)
    # Read on to the first grid point after the release, so every book and mark of an
    # earlier grid point is in hand (one grid point's own messages come in no fixed order),
    # then settle the preconditions before checking what the order did.
    await c.until(
        lambda: any(
            m.grid_time > accepted.release_time for m in c.since(c.after(accepted), SessionState)
        ),
        "session_state after the market order's release",
    )
    # Only books received before the `accepted`: a book after it may already show the
    # sweep's own rebuilt ladder, which is checked below, not taken as a moved quote.
    books = [
        m
        for m in c.seen[: c.after(accepted)]
        if isinstance(m, Book) and m.instrument == c.config.instrument
    ]
    at_receipt = [m for m in books if m.grid_time <= accepted.receipt_time][-1]
    meanwhile = [m for m in books if accepted.receipt_time < m.grid_time <= accepted.release_time]
    if any(list(m.ask_levels) != list(at_receipt.ask_levels) for m in meanwhile):
        pytest.skip("precondition: the quote holds steady while the market order is delayed")
    met = meanwhile[-1] if meanwhile else at_receipt
    if size <= sum(level.size for level in met.ask_levels):
        pytest.skip("precondition: the market order is larger than the ten ask levels it met")
    released = [m for m in marks() if m.sampled_at <= accepted.release_time]
    if not within_guard(met, released[-1] if released else marks()[-1]):
        pytest.skip("precondition: all ten ask levels lie within mark x 1.05, the market guard")
    remainder = await c.wait_for(
        lambda m: (
            isinstance(m, OrderCancelled)
            and m.strat_id == c.config.strat_a
            and m.instrument == c.config.instrument
            and m.side == BUY
            and m.reason_code == ReasonCodes.MARKET_REMAINDER
        ),
        "order_cancelled for the market remainder",
        start,
    )
    fills = [
        m
        for m in c.since(start, Execution)
        if m.strat_id == c.config.strat_a
        and m.instrument == c.config.instrument
        and m.side == BUY
        and not m.HasField("order_price")
    ]
    before_remainder = c.seen[start : c.after(remainder)]
    assert all(any(f is m for m in before_remainder) for f in fills), (
        "an execution came after the market remainder's cancellation"
    )
    assert [f.fill_price for f in fills] == [level.price for level in met.ask_levels]
    assert [f.fill_size for f in fills] == [level.size for level in met.ask_levels]
    for fill in fills:
        assert fill.fill_kind == STUDENT_TO_WALL
        assert fill.liquidity == TAKER
        assert fill.fee < 0
    assert remainder.cancelled_size == size - sum(f.fill_size for f in fills)
    last_fill = max(f.timestamp for f in fills)

    def wall_prints() -> list[tuple[int, int]]:
        return [
            (p.price, p.size)
            for t in c.since(start, Trades)
            if t.instrument == c.config.instrument
            for p in t.prints
            if p.kind == STUDENT_TO_WALL
        ]

    await c.until(lambda: len(wall_prints()) >= len(fills), "trades prints for the wall fills")
    expected_prints = sorted((f.fill_price, f.fill_size) for f in fills)
    assert sorted(wall_prints()) == expected_prints

    # A book is published only when it changes. If the sweep's impact was already at its
    # clamp, the rebuilt ladder can equal the one before and no new book is published; the
    # step's "shifted band" then cannot be observed, so it is a precondition.
    try:
        rebuilt = await c.wait_for(
            lambda m: (
                isinstance(m, Book)
                and m.instrument == c.config.instrument
                and m.grid_time >= last_fill
            ),
            "book after the sweep",
            start,
        )
    except NoMessage:
        # Everything read while waiting still counts: no further wall prints may appear.
        assert sorted(wall_prints()) == expected_prints, "extra wall prints after the sweep"
        pytest.skip("precondition: the sweep shifts the band, so a rebuilt book is published")
    assert sorted(wall_prints()) == expected_prints, "extra wall prints after the sweep"
    assert_ladder(rebuilt)
    assert rebuilt.ask_levels[0].price > met.ask_levels[0].price, "the band did not shift"


async def test_step_12_self_trade_prevention(market: Client):
    c = market
    (price,) = inside_prices(c.books.get(c.config.instrument), c.config.tick, 1)
    start = c.mark()
    await c.rest(c.config.strat_b, SELL, price)
    buy = c.mark()
    ref = await send_new(
        c.session,
        strat_id=c.config.strat_a,
        instrument=c.config.instrument,
        side=BUY,
        order_type=LIMIT,
        price=price,
        size=c.config.size,
    )
    await c.answer(ref, buy)
    cancelled = await c.wait_for(
        lambda m: (
            isinstance(m, OrderCancelled)
            and m.strat_id == c.config.strat_b
            and m.side == SELL
            and m.price == price
        ),
        "order_cancelled for the resting sell",
        buy,
    )
    assert cancelled.reason_code == ReasonCodes.SELF_TRADE, reason_code_name(cancelled.reason_code)
    # A print for it would belong to the first grid point at or after the cancellation.
    # Messages of one grid point come in no fixed order, so read on to a `session_state`
    # of a later grid point than that one: every message of an earlier grid point is sent
    # before it.
    boundary = await c.wait_for(
        lambda m: isinstance(m, SessionState) and m.grid_time >= cancelled.timestamp,
        "session_state at the grid point after the cancellation",
        0,
    )
    await c.wait_for(
        lambda m: isinstance(m, SessionState) and m.grid_time > boundary.grid_time,
        "session_state at a later grid point",
        0,
    )
    assert not [
        p
        for t in c.since(start, Trades)
        if t.instrument == c.config.instrument
        for p in t.prints
        if p.kind == STUDENT_TO_STUDENT and p.price == price
    ], "the self-trade was printed"
    assert not [m for m in c.since(start, Execution) if m.strat_id == c.config.strat_b], (
        "the resting sell was filled"
    )


async def test_step_13_mass_cancel(market: Client):
    c = market
    low, high = inside_prices(c.books.get(c.config.instrument), c.config.tick, 2)
    await c.rest(c.config.strat_a, BUY, low)
    await c.rest(c.config.strat_b, BUY, high)
    start = c.mark()
    ref = await send_mass_cancel(c.session)
    accepted = await c.answer(ref, start)
    assert accepted.request_type == RequestType.MASS_CANCEL
    check_release_time(accepted)
    first = await c.wait_for(
        lambda m: isinstance(m, OrderCancelled) and request_ref_of(m) == ref,
        "order_cancelled for the mass cancel",
        start,
    )
    await c.next_session_state(c.after(first))
    cancelled = [m for m in c.since(start, OrderCancelled) if request_ref_of(m) == ref]
    assert {(m.strat_id, m.price) for m in cancelled} == {
        (c.config.strat_a, low),
        (c.config.strat_b, high),
    }
    assert len(cancelled) == 2
    for m in cancelled:
        assert m.reason_code == ReasonCodes.MASS_CANCEL
        assert m.timestamp == accepted.release_time, "not cancelled at the mass cancel's release"
    # "No budget consumed" is not checked: no message reports a team's budget use.


# Steps 14 to 16: the close, and step 15, heartbeat and resume.
#
# Step 15's precondition puts a restart between two of its sub-steps and step 14's close
# before its empty marker, and step 16 needs the exchange not to have restarted since
# that close. So the sub-steps that need the session open, the restart among them, are
# defined here, before step 14, and run before it; the empty marker runs after it. Its
# heartbeat sub-steps run at whatever hour the run reaches them, and again after step
# 14's close, so that one run sees them inside a session and outside one.

# Step 15 names the heartbeat interval, the silence after which the exchange closes a
# connection, that close's code and reason, and the 44 s between the frames of its second
# sub-step. Like the figures of steps 10 and 16, these are what the vendored steps require
# of the exchange under test, not values trading code may rely on: take them from
# CONFORMANCE.md again whenever that file is re-vendored.
STEP_15_HEARTBEAT_EVERY = 15.0  # seconds
STEP_15_SILENCE = 45.0  # seconds
STEP_15_CLOSE_CODE = 4000
STEP_15_CLOSE_REASON = "heartbeat timeout"
STEP_15_FRAME_EVERY = 44.0  # seconds

# How far, in seconds of this machine's clock, a ping, a heartbeat or the close may come
# before or after the instant step 15 names. A connection is taken to open when this end
# has finished its handshake, a little after the exchange starts timing it, each frame
# arrives a little after it is sent, and the exchange checks its timers on a tick of its
# own.
STEP_15_EARLY = 0.5
STEP_15_LATE = 2.5

# The six private messages that carry a `report_seq`, as step 15 names them.
REPORT_TYPES = frozenset(
    {"accepted", "reject", "execution", "order_cancelled", "order_state", "risk_notice"}
)
# The fields step 15 names on each `order_snapshot`.
SNAPSHOT_FIELDS = ("strat_id", "instrument", "side", "price", "remaining_size", "timestamp")

RESTART_VAR = "QTE_CONFORMANCE_RESTART_CMD"
RESTART_WITHIN_VAR = "QTE_CONFORMANCE_RESTART_WITHIN"

# What step 14 leaves for step 15's empty marker: the close's cancellation of the step's
# order, its `report_seq`, and the team's newest `report_seq` after the close.
_STEP_14_REPORTS: list[tuple[OrderCancelled, int | None, int]] = []


class NotAccepted(Exception):
    """The exchange did not complete the WebSocket handshake, as while it catches up after
    a restart."""


# The protocol's own logger, kept off: its debug lines show every frame, `auth` and its
# token included, and `Wire` has none of the SDK's guards against that.
_WIRE_LOGGER = logging.getLogger("test_conformance.wire")
_WIRE_LOGGER.disabled = True


class _Protocol(ClientProtocol):
    """The `websockets` client protocol, answering a ping with a pong only if `pong`."""

    def __init__(self, uri: WebSocketURI, *, pong: bool) -> None:
        super().__init__(uri, max_size=None, logger=_WIRE_LOGGER)
        self.pong = pong

    def recv_frame(self, frame: Frame) -> None:
        if frame.opcode is Opcode.PING and not self.pong:
            self.events.append(frame)  # kept, and not answered
            return
        super().recv_frame(frame)


@dataclass(frozen=True)
class WireMessage:
    """One message as a `Wire` received it. `at` is when, in seconds after the connection
    opened on this machine's clock, and `payload_text` is the envelope's `payload` member
    exactly as it was sent."""

    at: float
    type: str
    seq: int | None
    sent_at: int | None
    report_seq: int | None
    payload: dict
    payload_text: str | None
    message: Message | None


_JSON = json.JSONDecoder()
_SPACE = re.compile(r"[ \t\n\r]*")


def member_text(text: str, name: str) -> str | None:
    """The text of the member `name` of the JSON object `text`, exactly as it stands there,
    or None if there is none. `text` must already be known to be valid JSON."""
    index = _SPACE.match(text, 0).end()
    if text[index : index + 1] != "{":
        raise ValueError("not a JSON object")
    index = _SPACE.match(text, index + 1).end()
    while text[index : index + 1] not in ("}", ""):
        key, index = _JSON.raw_decode(text, index)
        index = _SPACE.match(text, index).end() + 1  # past the colon
        start = _SPACE.match(text, index).end()
        _, end = _JSON.raw_decode(text, start)
        if key == name:
            return text[start:end]
        index = _SPACE.match(text, end).end()
        if text[index : index + 1] == ",":
            index = _SPACE.match(text, index + 1).end()
    return None


class Wire:
    """One WebSocket connection to the exchange, driven frame by frame, for step 15.

    Step 15 checks things the SDK does for its user on purpose and so hides: its connection
    answers every ping with a pong and sends pings of its own, so it is never silent; it
    absorbs heartbeats; and a session parses each payload, drops a report it has already
    delivered and holds live reports until a resume is complete. So this drives the
    `websockets` library's Sans-I/O client protocol over a socket of its own. It sends
    exactly the frames a sub-step names, answers pings only if told to, and keeps every
    message as it was received. Messages are still built and read with the SDK's codec and
    contract types.
    """

    def __init__(
        self, protocol: _Protocol, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._protocol = protocol
        self._reader = reader
        self._writer = writer
        self._loop = asyncio.get_running_loop()
        self._opened = self._loop.time()
        self.messages: list[WireMessage] = []
        self.pings: list[float] = []
        self.unreadable: list[str] = []
        self._fragments: list[bytes] | None = None
        self.closed_at: float | None = None
        self.last_sent = 0.0
        self.ended = False
        self._changed = asyncio.Event()
        self._task: asyncio.Task | None = None

    @classmethod
    async def open(cls, url: str, *, pong: bool, timeout: float = 10.0) -> "Wire":
        uri = parse_uri(url)
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(
                uri.host, uri.port, ssl=True if uri.secure else None
            )
            try:
                protocol = _Protocol(uri, pong=pong)
                protocol.send_request(protocol.connect())
                writer.write(b"".join(protocol.data_to_send()))
                events: list = []
                while not any(isinstance(event, Response) for event in events):
                    data = await reader.read(65536)
                    if not data:
                        break
                    protocol.receive_data(data)
                    events += protocol.events_received()
            except BaseException:
                writer.close()
                raise
        if protocol.state is not State.OPEN:
            writer.close()
            status = [event.status_code for event in events if isinstance(event, Response)]
            raise NotAccepted(f"the handshake was answered with {status or 'nothing'}")
        wire = cls(protocol, reader, writer)
        wire._take(events)
        wire._task = asyncio.create_task(wire._read())
        return wire

    async def __aenter__(self) -> "Wire":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def now(self) -> float:
        """Seconds since the connection opened."""
        return self._loop.time() - self._opened

    @property
    def close_frame(self) -> Close | None:
        """The close frame the exchange sent, once it has."""
        return self._protocol.close_rcvd

    def _take(self, events: list) -> None:
        for event in events:
            if not isinstance(event, Frame):
                continue
            at = self.now()
            if event.opcode is Opcode.PING:
                self.pings.append(at)
            elif event.opcode is Opcode.CLOSE:
                self.closed_at = at
            elif event.opcode is Opcode.TEXT or (
                event.opcode is Opcode.CONT and self._fragments is not None
            ):
                # A message may come in fragments: it is read once its last has arrived.
                self._fragments = [*(self._fragments or []), bytes(event.data)]
                if event.fin:
                    data, self._fragments = b"".join(self._fragments), None
                    try:
                        text = data.decode()
                    except UnicodeDecodeError:
                        self.unreadable.append("a text message that is not UTF-8")
                        continue
                    self._take_text(at, text)
            elif event.opcode is not Opcode.PONG:
                self.unreadable.append(f"a {event.opcode.name} frame")
        self._changed.set()

    def _take_text(self, at: float, text: str) -> None:
        try:
            decoded = codec.decode(text)
            envelope = decoded.envelope
            kind = INBOUND.get(envelope.type)
            message = None if kind is None else codec.unpack(decoded.payload, kind)
            payload_text = member_text(text, "payload")
        except Exception as error:
            self.unreadable.append(f"a frame that could not be decoded ({type(error).__name__})")
            return
        self.messages.append(
            WireMessage(
                at=at,
                type=envelope.type,
                seq=envelope.seq if envelope.HasField("seq") else None,
                sent_at=envelope.sent_at if envelope.HasField("sent_at") else None,
                report_seq=envelope.report_seq if envelope.HasField("report_seq") else None,
                payload=decoded.payload,
                payload_text=payload_text,
                message=message,
            )
        )

    def _flush(self) -> None:
        # What the protocol queued itself: a pong if `pong`, and the reply to a close.
        for data in self._protocol.data_to_send():
            if data:
                self._writer.write(data)

    async def _read(self) -> None:
        try:
            while True:
                data = await self._reader.read(65536)
                if data:
                    self._protocol.receive_data(data)
                else:
                    self._protocol.receive_eof()
                self._take(self._protocol.events_received())
                self._flush()
                if not data:
                    break
        except OSError:
            pass
        finally:
            self.ended = True
            self._changed.set()

    async def send(self, type_: str, payload: Message) -> None:
        self._protocol.send_text(codec.encode(CONTRACT_VERSION, type_, payload).encode())
        self._flush()
        self.last_sent = self.now()
        await self._writer.drain()

    async def login(self, timeout: float) -> int:
        """Send `auth` with the conformance token and read through the `session_ack`,
        `calendar` and `instruments` that answer it. Returns the index just past them."""
        await self.send("auth", Auth(token=os.environ[TOKEN_VAR]))
        reply = await self.wait_for(
            lambda m: m.type in ("instruments", "session_reject"),
            "session_ack, calendar and instruments",
            0,
            timeout,
        )
        assert reply.type == "instruments", "the exchange rejected the session"
        return self.after(reply)

    def after(self, message: WireMessage) -> int:
        """The index just past `message` in `messages`."""
        return next(i for i, m in enumerate(self.messages) if m is message) + 1

    async def until(self, done: Callable[[], bool], what: str, timeout: float) -> None:
        """Read until `done()` is true or the connection ends, with one deadline."""
        deadline = self._loop.time() + timeout
        while True:
            self._changed.clear()
            if self.unreadable:
                raise AssertionError(f"the exchange sent {self.unreadable[0]}")
            if done():
                return
            if self.ended:
                raise AssertionError(f"the connection ended before {what}")
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                raise NoMessage(f"no {what} within {timeout} s")
            try:
                async with asyncio.timeout(remaining):
                    await self._changed.wait()
            except TimeoutError:
                pass

    async def wait_for(
        self, match: Callable[[WireMessage], bool], what: str, since: int, timeout: float
    ) -> WireMessage:
        """The first message from index `since` on that `match` accepts."""
        found: list[WireMessage] = []

        def done() -> bool:
            found[:] = [m for m in self.messages[since:] if match(m)][:1]
            return bool(found)

        await self.until(done, what, timeout)
        return found[0]

    async def wait_closed(self, timeout: float) -> None:
        """Wait until the exchange has closed the connection."""
        deadline = self._loop.time() + timeout
        while not self.ended:
            self._changed.clear()
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                raise AssertionError(f"the connection was still open after {self.now():.1f} s")
            try:
                async with asyncio.timeout(remaining):
                    await self._changed.wait()
            except TimeoutError:
                pass

    async def sleep_until(self, at: float) -> None:
        """Sleep until `at` seconds after the connection opened."""
        await asyncio.sleep(max(0.0, at - self.now()))

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        with suppress(Exception):
            if self._protocol.state is State.OPEN:
                self._protocol.send_close()
                self._flush()
        self._writer.close()
        with suppress(Exception):
            async with asyncio.timeout(2):
                await self._writer.wait_closed()


def reports_of(w: Wire, since: int) -> list[WireMessage]:
    """The private reports `w` received from index `since` on."""
    return [m for m in w.messages[since:] if m.report_seq is not None]


def assert_seq_increasing(w: Wire) -> None:
    seqs = [m.seq for m in w.messages]
    assert None not in seqs, "a message without a seq"
    assert all(a < b for a, b in zip(seqs[:-1], seqs[1:], strict=True)), (
        "seq does not increase from one message of the connection to the next"
    )


def assert_numbered(reports: list[WireMessage], first: int) -> None:
    numbers = [m.report_seq for m in reports]
    assert numbers == list(range(first, first + len(reports))), (
        f"report_seq {numbers}, not a run from {first} with no gap or duplicate"
    )


def assert_same_reports(got: list[WireMessage], sent: list[WireMessage]) -> None:
    """`got` are the reports `sent`, each with the type, `report_seq` and payload bytes it
    was first sent with."""
    assert [(m.type, m.report_seq) for m in got] == [(m.type, m.report_seq) for m in sent]
    for again, first in zip(got, sent, strict=True):
        assert again.payload_text == first.payload_text, (
            f"report {again.report_seq} was not sent with the payload bytes it was first sent with"
        )


def assert_nothing_but(messages: list[WireMessage], what: str, *also: str) -> None:
    """Nothing in `messages` but private reports and the types named in `also`. Heartbeats
    are the connection's own, sent every 15 s whatever else it carries, so they are not
    counted."""
    for m in messages:
        if m.type == "heartbeat" or m.type in also:
            continue
        assert m.type in REPORT_TYPES and m.report_seq is not None, f"a {m.type} {what}"


def assert_snapshot_first(answer: list[WireMessage], count: int) -> None:
    """`answer`, what followed a `resume_ack` other than heartbeats, opens with exactly
    `count` `order_snapshot` and has none after them."""
    kinds = [m.type for m in answer]
    assert kinds[:count] == ["order_snapshot"] * count, "fewer order_snapshot than snapshot_count"
    assert "order_snapshot" not in kinds[count:], "more order_snapshot than snapshot_count"


async def answer_to_resume(r: Wire, since: int, timeout: float) -> WireMessage:
    """The first message after `since` other than a heartbeat, which must be `resume_ack`."""
    answer = await r.wait_for(lambda m: m.type != "heartbeat", "resume_ack", since, timeout)
    assert answer.type == "resume_ack", f"{answer.type} answered resume, not resume_ack"
    return answer


async def resting_reports(
    c: Client, witness: Wire, orders: list[tuple[str, int, int]]
) -> list[WireMessage]:
    """Rest each (strategy, side, price) order and return the reports `witness`, another
    connection of the team, received for them as they were first sent."""
    start = len(witness.messages)
    for strat, side, price in orders:
        await c.rest(strat, side, price)
        await witness.wait_for(
            lambda m, side=side, price=price: (
                isinstance(m.message, OrderState)
                and m.message.instrument == c.config.instrument
                and m.message.side == side
                and m.message.price == price
            ),
            "order_state on a second connection of the team",
            start,
            c.config.timeout,
        )
    sent = reports_of(witness, start)
    assert_numbered(sent, sent[0].report_seq)
    return sent


def check_snapshots(
    snapshots: list[WireMessage], orders: list[tuple[str, int, int]], c: Client
) -> None:
    """`snapshots` describe `orders`, each resting whole, in level-key order: instrument
    ascending, then BUY before SELL, then price ascending."""
    expected = sorted(orders, key=lambda o: (o[1] != BUY, o[2]))  # one instrument
    assert len(snapshots) == len(expected), f"{len(snapshots)} order_snapshot, not {len(expected)}"
    for m, (strat, side, price) in zip(snapshots, expected, strict=True):
        assert m.report_seq is None, "an order_snapshot carries a report_seq"
        missing = [name for name in SNAPSHOT_FIELDS if name not in m.payload]
        assert not missing, f"an order_snapshot has no {', '.join(missing)}"
        s = m.message
        assert (s.strat_id, s.instrument, s.side, s.price) == (
            strat,
            c.config.instrument,
            side,
            price,
        ), "order_snapshot not in level-key order, or not the team's resting order"
        assert s.remaining_size == c.config.size
        assert s.timestamp > 0


async def sdk_resume(config: Settings, last: int) -> tuple[ResumeAck, list[Received]]:
    """Resume through the SDK's own `Session.resume`, as a client would, and return the
    `resume_ack` and every message the session delivered before its `ResumeComplete`."""
    session = await open_session(config.url, os.environ[TOKEN_VAR])
    delivered: list[Received] = []
    try:
        await session.wait_for_calendar(config.timeout)
        ack = await session.resume(last, timeout=config.timeout)
        async with asyncio.timeout(config.timeout):
            async for event in session:
                assert not isinstance(event, DataUncertain), f"{event!r} during the resume"
                if isinstance(event, ResumeComplete):
                    break
                if isinstance(event, Received) and event.type not in (
                    "calendar",
                    "instruments",
                    "resume_ack",
                ):
                    delivered.append(event)
    finally:
        await session.close()
    return ack, delivered


async def silent_heartbeats(config: Settings) -> None:
    """Step 15's first sub-step on a connection that never sends `auth` and one that sends
    only `auth`, neither sending anything else, not even a pong."""
    async with (
        await Wire.open(config.url, pong=False) as unauthenticated,
        await Wire.open(config.url, pong=False) as authenticated,
    ):
        await authenticated.login(config.timeout)
        wait = STEP_15_SILENCE + STEP_15_LATE + config.timeout
        for w, quiet_since in ((unauthenticated, 0.0), (authenticated, authenticated.last_sent)):
            await w.wait_closed(wait)
            assert w.closed_at is not None, "the connection ended without a close frame"
            # It stays connected for as long as the exchange's own timer allows.
            assert w.closed_at >= quiet_since + STEP_15_SILENCE - STEP_15_EARLY, (
                f"closed {w.closed_at:.1f} s after opening, before 45 s of silence"
            )
            assert_heartbeats(w)


def assert_heartbeats(w: Wire) -> None:
    """A WebSocket ping and a `heartbeat` 15 s after the connection opened and every 15 s
    after that, until the exchange closed it, each heartbeat an envelope with `seq` and
    `sent_at`, no payload fields and no `report_seq`."""
    assert w.closed_at is not None
    beats = [m for m in w.messages if m.type == "heartbeat"]
    for m in beats:
        assert m.seq is not None and m.sent_at is not None, "a heartbeat without seq or sent_at"
        assert m.payload == {}, "a heartbeat with payload fields"
        assert m.report_seq is None, "a heartbeat with a report_seq"
    assert_seq_increasing(w)
    every = STEP_15_HEARTBEAT_EVERY
    for kind, times in (("heartbeat", [m.at for m in beats]), ("ping", w.pings)):
        for at in times:
            due = max(1, round(at / every)) * every
            assert due - STEP_15_EARLY <= at <= due + STEP_15_LATE, (
                f"a {kind} {at:.1f} s after the connection opened, off its 15 s beat"
            )
        for k in range(1, int((w.closed_at + STEP_15_EARLY) // every) + 1):
            due = k * every
            count = sum(due - STEP_15_EARLY <= at <= due + STEP_15_LATE for at in times)
            # One may or may not come before a close that falls on a beat.
            if due + STEP_15_LATE < w.closed_at:
                assert count == 1, f"{count} {kind}s {due:.0f} s after opening, not one"
            else:
                assert count <= 1, f"{count} {kind}s {due:.0f} s after opening"
    assert beats, "no heartbeat"


def assert_closed_for_silence(w: Wire, quiet_since: float) -> None:
    """The exchange closed `w` with 4000 "heartbeat timeout" 45 s after `quiet_since`."""
    close = w.close_frame
    assert close is not None and w.closed_at is not None, "ended without a close frame"
    assert (close.code, close.reason) == (STEP_15_CLOSE_CODE, STEP_15_CLOSE_REASON), (
        f"closed with {close.code} {close.reason!r}"
    )
    due = quiet_since + STEP_15_SILENCE
    assert due - STEP_15_EARLY <= w.closed_at <= due + STEP_15_LATE, (
        f"closed {w.closed_at:.1f} s after opening, not {due:.1f} s"
    )


async def silence_closes(config: Settings) -> None:
    """Step 15's second sub-step, on three connections side by side."""
    wait = STEP_15_SILENCE + STEP_15_LATE + config.timeout

    async def authenticated_and_silent() -> None:
        async with await Wire.open(config.url, pong=False) as w:
            await w.login(config.timeout)
            await w.wait_closed(wait)
            assert_closed_for_silence(w, w.last_sent)

    async def never_authenticated_but_answers_pings() -> None:
        async with await Wire.open(config.url, pong=True) as w:
            await w.wait_closed(wait)
            assert len(w.pings) >= 2, "fewer than two pings to answer before the close"
            assert_closed_for_silence(w, 0.0)

    async def a_frame_at_44_s_and_another_44_s_later() -> None:
        async with await Wire.open(config.url, pong=False) as w:
            await w.login(config.timeout)
            for k in (1, 2):
                await w.sleep_until(k * STEP_15_FRAME_EVERY)
                assert not w.ended, (
                    f"closed {w.closed_at} s after opening, before its frame at "
                    f"{k * STEP_15_FRAME_EVERY:.0f} s"
                )
                # Any frame will do; a subscribe is one the exchange answers harmlessly.
                await w.send("subscribe", Subscribe(instruments=[config.instrument]))
            await w.wait_closed(wait)
            assert_closed_for_silence(w, w.last_sent)

    async with asyncio.TaskGroup() as group:
        group.create_task(authenticated_and_silent())
        group.create_task(never_authenticated_but_answers_pings())
        group.create_task(a_frame_at_44_s_and_another_44_s_later())


async def test_step_15a_heartbeat_with_the_client_silent():
    await silent_heartbeats(settings())


async def test_step_15b_silence_closes_with_4000():
    await silence_closes(settings())


async def test_step_15c_resume_inside_the_window_replays(market: Client):
    c = market
    config = c.config
    low, high = inside_prices(c.books.get(config.instrument), config.tick, 2)
    async with await Wire.open(config.url, pong=True) as witness:
        await witness.login(config.timeout)
        sent = await resting_reports(
            c, witness, [(config.strat_a, BUY, low), (config.strat_b, BUY, high)]
        )
        # L is just below the reports the witness saw first sent, so each replayed one can
        # be compared with its first sending; at least 1, as the step requires.
        last = max(1, sent[0].report_seq - 1)
        newest = sent[-1].report_seq
        replayed = [m for m in sent if m.report_seq > last]
        async with await Wire.open(config.url, pong=True) as r:
            start = await r.login(config.timeout)
            await r.send("resume", Resume(last_report_seq=last))
            ack = await answer_to_resume(r, start, config.timeout)
            assert (
                ack.message.replayed,
                ack.message.as_of_report_seq,
                ack.message.snapshot_count,
            ) == (True, newest, 0)
            answered = r.after(ack)
            await r.until(
                lambda: len(reports_of(r, answered)) >= len(replayed),
                "the replayed reports",
                config.timeout,
            )
            # Then live reports from H + 1: the cancel's.
            live_start = len(witness.messages)
            await send_cancel(c.session, instrument=config.instrument, side=BUY, price=low)
            await witness.wait_for(
                lambda m: isinstance(m.message, OrderCancelled) and m.message.price == low,
                "order_cancelled on a second connection of the team",
                live_start,
                config.timeout,
            )
            live = reports_of(witness, live_start)
            assert_numbered(live, newest + 1)
            await r.until(
                lambda: len(reports_of(r, answered)) >= len(replayed) + len(live),
                "the live reports after the replay",
                config.timeout,
            )
            await asyncio.sleep(1.0)  # so anything sent after them is read too
            assert_nothing_but(r.messages[answered:], "after resume_ack")
            assert_same_reports(reports_of(r, answered), replayed + live)
            assert_seq_increasing(r)
    # The SDK's own resume, as a client makes it, gets the same replay.
    ack, delivered = await sdk_resume(config, last)
    expected = replayed + live
    assert (ack.replayed, ack.as_of_report_seq) == (True, expected[-1].report_seq)
    assert [(e.type, e.report_seq, e.payload) for e in delivered] == [
        (m.type, m.report_seq, m.payload) for m in expected
    ]


async def test_step_15d_resume_with_no_cursor_gets_a_snapshot(market: Client):
    c = market
    config = c.config
    low, middle, high = inside_prices(c.books.get(config.instrument), config.tick, 3)
    # Rested out of level-key order, so a snapshot in the order the orders were accepted
    # fails.
    orders = [
        (config.strat_a, SELL, high),
        (config.strat_b, BUY, middle),
        (config.strat_a, BUY, low),
    ]
    async with await Wire.open(config.url, pong=True) as witness, AsyncExitStack() as stack:
        await witness.login(config.timeout)
        sent = await resting_reports(c, witness, orders)
        newest = sent[-1].report_seq
        # No cursor, and one above the newest report, which a replay cannot serve either.
        resumed: list[tuple[Wire, int]] = []
        for last in (0, newest + 1000):
            r = await stack.enter_async_context(await Wire.open(config.url, pong=True))
            start = await r.login(config.timeout)
            await r.send("resume", Resume(last_report_seq=last))
            ack = await answer_to_resume(r, start, config.timeout)
            assert (
                ack.message.replayed,
                ack.message.as_of_report_seq,
                ack.message.snapshot_count,
            ) == (False, newest, len(orders)), f"resume_ack for last_report_seq {last}"
            answered = r.after(ack)
            await r.until(
                lambda r=r, answered=answered: (
                    sum(m.type == "order_snapshot" for m in r.messages[answered:]) >= len(orders)
                ),
                "order_snapshot for each resting order",
                config.timeout,
            )
            resumed.append((r, answered))
        # Then reports from N + 1: the cancel's.
        live_start = len(witness.messages)
        await send_cancel(c.session, instrument=config.instrument, side=SELL, price=high)
        await witness.wait_for(
            lambda m: isinstance(m.message, OrderCancelled) and m.message.side == SELL,
            "order_cancelled on a second connection of the team",
            live_start,
            config.timeout,
        )
        live = reports_of(witness, live_start)
        assert_numbered(live, newest + 1)
        for r, answered in resumed:
            await r.until(
                lambda r=r, answered=answered: len(reports_of(r, answered)) >= len(live),
                "the reports after the snapshot",
                config.timeout,
            )
        await asyncio.sleep(1.0)  # so anything sent after them is read too
        for r, answered in resumed:
            answer = [m for m in r.messages[answered:] if m.type != "heartbeat"]
            assert_nothing_but(answer, "after resume_ack", "order_snapshot")
            assert_snapshot_first(answer, len(orders))
            snapshots = answer[: len(orders)]
            check_snapshots(snapshots, orders, c)
            assert_same_reports(reports_of(r, answered), live)
            assert_seq_increasing(r)
    # The SDK's own resume, as a client makes it, gets the snapshot of the two buys left.
    ack, delivered = await sdk_resume(config, 0)
    assert (ack.replayed, ack.as_of_report_seq, ack.snapshot_count) == (
        False,
        live[-1].report_seq,
        len(orders) - 1,
    )
    # Compared order by order, not by timestamp: each answer may stamp its own.
    assert all(isinstance(e.message, OrderSnapshot) for e in delivered)
    as_received = [
        WireMessage(
            at=0.0,
            type=e.type,
            seq=e.seq,
            sent_at=None,
            report_seq=e.report_seq,
            payload=e.payload,
            payload_text=None,
            message=e.message,
        )
        for e in delivered
    ]
    check_snapshots(as_received, [o for o in orders if o[1] == BUY], c)


def restart_within() -> float:
    """The seconds QTE_CONFORMANCE_RESTART_WITHIN allows, 120 if it is not set."""
    text = os.environ.get(RESTART_WITHIN_VAR, "120")
    try:
        within = float(text)
    except ValueError:
        within = math.nan
    if not (math.isfinite(within) and within > 0):
        pytest.fail(f"{RESTART_WITHIN_VAR} must be a positive number of seconds")
    return within


async def restart_exchange() -> None:
    """Run the operator's restart command, as given, and wait for it to finish."""
    within = restart_within()
    process = await asyncio.create_subprocess_shell(os.environ[RESTART_VAR])
    try:
        async with asyncio.timeout(within):
            status = await process.wait()
    except TimeoutError:
        process.kill()
        pytest.fail(f"{RESTART_VAR} did not finish within {within} s")
    assert status == 0, f"{RESTART_VAR} exited with status {status}"


async def reopened(config: Settings) -> Wire:
    """A connection to the restarted exchange, once it accepts one: until it has caught up
    with its log, it refuses the handshake."""
    within = restart_within()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while True:
        try:
            return await Wire.open(config.url, pong=True)
        except (OSError, NotAccepted, TimeoutError):
            if loop.time() >= deadline:
                pytest.fail(f"the exchange accepted no connection within {within} s of its restart")
            await asyncio.sleep(0.5)


@pytest.mark.skipif(
    not os.environ.get(RESTART_VAR),
    reason=f"precondition: the exchange can be restarted between sub-steps ({RESTART_VAR})",
)
async def test_step_15e_resume_across_a_restart(market: Client):
    c = market
    config = c.config
    low, high = inside_prices(c.books.get(config.instrument), config.tick, 2)
    # Rested out of level-key order, so a snapshot in the order the orders were accepted
    # fails.
    orders = [(config.strat_b, BUY, high), (config.strat_a, BUY, low)]
    async with await Wire.open(config.url, pong=True) as witness:
        await witness.login(config.timeout)
        sent = await resting_reports(c, witness, orders)
    newest = sent[-1].report_seq
    last = newest - 1  # inside the window before the restart
    await c.close()
    await restart_exchange()
    async with await reopened(config) as r:
        start = await r.login(config.timeout)
        await r.send("resume", Resume(last_report_seq=last))
        ack = await answer_to_resume(r, start, config.timeout)
        assert (
            ack.message.replayed,
            ack.message.as_of_report_seq,
            ack.message.snapshot_count,
        ) == (False, newest, len(orders))
        answered = r.after(ack)
        await r.until(
            lambda: sum(m.type == "order_snapshot" for m in r.messages[answered:]) >= len(orders),
            "order_snapshot for each resting order",
            config.timeout,
        )
        # Then reports from N + 1, numbered as an uninterrupted run would have numbered them.
        again = await Client.connect(config)
        try:
            mark = again.mark()
            ref = await send_cancel(
                again.session, instrument=config.instrument, side=BUY, price=low
            )
            await again.wait_for(
                lambda m: isinstance(m, OrderCancelled) and request_ref_of(m) == ref,
                "order_cancelled for the cancel",
                mark,
            )
            own = [n for n in again.report_seqs[mark:] if n is not None]
        finally:
            await again.close()
        await r.until(
            lambda: len(reports_of(r, answered)) >= len(own),
            "the reports after the snapshot",
            config.timeout,
        )
        await asyncio.sleep(1.0)  # so anything sent after them is read too
        answer = [m for m in r.messages[answered:] if m.type != "heartbeat"]
        assert_nothing_but(answer, "after resume_ack", "order_snapshot")
        assert_snapshot_first(answer, len(orders))
        check_snapshots(answer[: len(orders)], orders, c)
        reports = reports_of(r, answered)
        assert_numbered(reports, newest + 1)
        assert [m.report_seq for m in reports] == own
        assert_seq_increasing(r)


async def test_step_15g_market_data_is_not_replayed(market: Client):
    c = market
    config = c.config
    (price,) = inside_prices(c.books.get(config.instrument), config.tick, 1)
    await c.rest(config.strat_a, BUY, price)
    newest = c.newest_report_seq()
    assert newest is not None and newest >= 2
    for last in (newest - 1, 0):  # a replay, then a snapshot
        async with await Wire.open(config.url, pong=True) as r:
            start = await r.login(config.timeout)
            await r.send("resume", Resume(last_report_seq=last))
            ack = await answer_to_resume(r, start, config.timeout)
            assert ack.message.replayed == (last != 0)
            answered = r.after(ack)
            # Market data goes on being published meanwhile, as the subscribed client sees.
            published = c.mark()
            await c.until(
                lambda published=published: len(c.since(published, SessionState)) >= 3,
                "session_state on the subscribed connection",
            )
            await asyncio.sleep(0.5)
            answer = [m for m in r.messages[answered:] if m.type != "heartbeat"]
            assert_nothing_but(answer, "after resume_ack", "order_snapshot")
            assert_snapshot_first(answer, ack.message.snapshot_count)
            # A client closes the gap by subscribing again, which is answered with the latest
            # book.
            latest = c.books.get(config.instrument)
            asked = len(r.messages)
            await r.send("subscribe", Subscribe(instruments=[config.instrument]))
            book = await r.wait_for(
                lambda m: m.type == "book" and m.message.instrument == config.instrument,
                "book for the subscribe",
                asked,
                config.timeout,
            )
            await c.drain(0.5)
            assert book.message.grid_time >= latest.grid_time, "older than the latest book"
            assert any(book.message == m for m in c.seen if isinstance(m, Book)), (
                "not a book the exchange published"
            )


@pytest.mark.skipif(
    not os.environ.get("QTE_CONFORMANCE_CLOSE_WITHIN"),
    reason="precondition: the exchange closes its session (QTE_CONFORMANCE_CLOSE_WITHIN=<seconds>)",
)
async def test_step_14_close(market: Client):
    within = os.environ["QTE_CONFORMANCE_CLOSE_WITHIN"]
    c = market
    (price,) = inside_prices(c.books.get(c.config.instrument), c.config.tick, 1)
    start = c.mark()
    await c.rest(c.config.strat_a, BUY, price)
    closed = await c.wait_for(
        lambda m: isinstance(m, SessionState) and m.state == CLOSED,
        "session_state CLOSED",
        start,
        timeout=float(within),
    )
    _CLOSED_BY_STEP_14.append(closed)
    cancelled = await c.wait_for(
        lambda m: (
            isinstance(m, OrderCancelled)
            and m.strat_id == c.config.strat_a
            and m.side == BUY
            and m.price == price
        ),
        "order_cancelled for the order resting into the close",
        start,
    )
    assert cancelled.reason_code == ReasonCodes.SESSION_CLOSE, reason_code_name(
        cancelled.reason_code
    )
    later = c.mark()
    ref = await send_new(
        c.session,
        strat_id=c.config.strat_a,
        instrument=c.config.instrument,
        side=BUY,
        order_type=LIMIT,
        price=price,
        size=c.config.size,
    )
    reject = await c.rejection(ref, later)
    assert reject.reason_code == ReasonCodes.RELEASE_AFTER_CLOSE, reason_code_name(
        reject.reason_code
    )
    # What step 15's empty marker is checked against, once anything else is read.
    await c.drain(1.0)
    newest = c.newest_report_seq()
    assert newest is not None
    _STEP_14_REPORTS.append((cancelled, c.report_seq_of(cancelled), newest))


async def test_step_15f_resume_after_the_close_gets_the_empty_marker():
    config = settings()
    if not _STEP_14_REPORTS:
        pytest.skip("precondition: step 14's close has cancelled every order, in this run")
    cancelled, cancelled_seq, newest = _STEP_14_REPORTS[0]
    async with await Wire.open(config.url, pong=True) as r:
        start = await r.login(config.timeout)
        await r.send("resume", Resume(last_report_seq=0))
        ack = await answer_to_resume(r, start, config.timeout)
        assert (
            ack.message.replayed,
            ack.message.as_of_report_seq,
            ack.message.snapshot_count,
        ) == (False, newest, 0)
        answered = r.after(ack)
        await asyncio.sleep(2.0)  # so anything sent after it is read too
        rest = [m.type for m in r.messages[answered:] if m.type != "heartbeat"]
        assert not rest, f"{rest[0]} after the empty marker"
    # A last_report_seq the exchange can still replay gets the replay, the close's
    # SESSION_CLOSE cancellation included.
    assert cancelled_seq is not None, "step 14's SESSION_CLOSE order_cancelled has no report_seq"
    async with await Wire.open(config.url, pong=True) as r:
        start = await r.login(config.timeout)
        await r.send("resume", Resume(last_report_seq=cancelled_seq - 1))
        ack = await answer_to_resume(r, start, config.timeout)
        assert (
            ack.message.replayed,
            ack.message.as_of_report_seq,
            ack.message.snapshot_count,
        ) == (True, newest, 0)
        answered = r.after(ack)
        await r.until(
            lambda: len(reports_of(r, answered)) >= newest - cancelled_seq + 1,
            "the replayed reports",
            config.timeout,
        )
        await asyncio.sleep(1.0)  # so anything sent after them is read too
        assert_nothing_but(r.messages[answered:], "after resume_ack")
        replayed = reports_of(r, answered)
        assert_numbered(replayed, cancelled_seq)
        assert replayed[0].message == cancelled, (
            "the replay does not start with the close's cancellation"
        )


async def test_step_15a_heartbeat_after_the_close():
    config = settings()
    if not _CLOSED_BY_STEP_14:
        pytest.skip("precondition: step 14 closed the instrument's session in this run")
    await silent_heartbeats(config)


async def test_step_15b_silence_closes_with_4000_after_the_close():
    config = settings()
    if not _CLOSED_BY_STEP_14:
        pytest.skip("precondition: step 14 closed the instrument's session in this run")
    await silence_closes(config)


def unknown_among(reject: Reject, names: tuple[str, ...]) -> bool:
    """Whether `reject` refuses a subscription because the exchange does not know one of
    `names`."""
    return (
        reject.reason_code == ReasonCodes.UNKNOWN_INSTRUMENT
        and reject.HasField("instrument")
        and reject.instrument in names
    )


async def test_step_16_subscribe_outside_a_session(client: Client):
    c = client
    instrument = c.config.instrument
    # The step's expected value needs the recorded session it names. That session quotes
    # only QTEA, so QTEA must be the instrument, and the subscribe then also names the two
    # it never quotes, which must get no official close.
    recorded = os.environ.get("QTE_CONFORMANCE_RECORDED_SESSION") == "1"
    if recorded and instrument != STEP_16_QUOTED:
        pytest.fail(
            "QTE_CONFORMANCE_RECORDED_SESSION=1 needs "
            f"QTE_CONFORMANCE_INSTRUMENT={STEP_16_QUOTED}, the one instrument that recorded "
            f"session quotes, not {instrument}"
        )
    if not _CLOSED_BY_STEP_14:
        pytest.skip("precondition: step 14 closed the instrument's session in this run")
    closed = _CLOSED_BY_STEP_14[0]
    unquoted = STEP_16_UNQUOTED if recorded else ()
    unknown: list[str] = []
    start = c.mark()
    await subscribe(c.session, [instrument, *unquoted])
    reply = await c.wait_for(
        lambda m: isinstance(m, SessionState | Reject), "session_state for the subscribe", start
    )
    if isinstance(reply, Reject) and unknown_among(reply, unquoted):
        # A rejected subscribe is not applied at all, so the exchange, which does not know
        # QTEB or QTEC, is asked again for the instrument alone, for the step's other checks.
        unknown.append(reply.instrument)
        start = c.mark()
        await subscribe(c.session, [instrument])
        reply = await c.wait_for(
            lambda m: isinstance(m, SessionState | Reject), "session_state for the subscribe", start
        )
    assert not isinstance(reply, Reject), (
        f"subscribe rejected: {reason_code_name(reply.reason_code)}"
    )
    state = reply
    assert state.state == CLOSED, "the session is open again since step 14 closed it"
    assert (state.session_date, state.open_time, state.close_time) == (
        closed.session_date,
        closed.open_time,
        closed.close_time,
    ), "session_state does not name the session step 14 closed"
    assert state.grid_time == state.close_time
    calendar = c.session.calendar
    assert calendar is not None
    expected_next = next_open(calendar, state.close_time)
    if expected_next is None:
        assert not calendar.HasField("next_open")
    else:
        assert calendar.next_open == expected_next
    official = await c.wait_for(
        lambda m: isinstance(m, OfficialClose) and m.instrument == instrument,
        "official_close",
        start,
    )
    assert official.session_date == closed.session_date
    await c.drain(min(2.0, c.config.timeout))
    after = c.seen[start:]
    unknown += [m.instrument for m in c.since(start, Reject) if unknown_among(m, unquoted)]
    rejected = [m for m in c.since(start, Reject) if not unknown_among(m, unquoted)]
    assert not rejected, f"subscribe rejected: {reason_code_name(rejected[0].reason_code)}"
    assert len([m for m in after if isinstance(m, SessionState)]) == 1
    assert not [m for m in after if isinstance(m, Book | Trades | Mark)]
    closes = [m for m in after if isinstance(m, OfficialClose)]
    if recorded:
        assert not [m for m in closes if m.instrument in STEP_16_UNQUOTED], (
            "an official_close for QTEB or QTEC, which have no valid mark in the close window"
        )
    assert [m.instrument for m in closes] == [instrument], (
        "not exactly one official_close, for the instrument"
    )
    if not recorded:
        pytest.skip(
            "precondition: the exchange is fed the recorded session step 16 names "
            "(QTE_CONFORMANCE_RECORDED_SESSION=1); QTEA's official close value and that QTEB "
            "and QTEC get none were not checked"
        )
    assert official.value == STEP_16_OFFICIAL_CLOSE, (
        f"{STEP_16_QUOTED}'s official close is {official.value}, not {STEP_16_OFFICIAL_CLOSE}"
    )
    if unknown:
        pytest.skip(
            "precondition: the exchange knows QTEB and QTEC, which step 16's recorded session "
            f"never quotes; rejected as unknown: {', '.join(unknown)}"
        )
