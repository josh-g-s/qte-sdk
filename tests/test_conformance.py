"""The published conformance session, run against a local exchange.

`conformance/CONFORMANCE.md` is vendored byte for byte from the exchange's published
conformance steps. This module runs its WebSocket session (steps 1 to 16, with 9a to 9c)
through the public SDK API only: `open_session`, `subscribe`, the `send_*` functions and the message
classes. The history service steps (H1 to H19) are not run here.

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

Each numbered step is its own test with its own session. Steps 4 to 14 start by mass
cancelling the team's resting orders, so a step that fails or is skipped leaves nothing
behind for the next. Steps 3 to 14 need the instrument's session to be open; if it is
not, they are skipped. Step 16 runs only after step 14 has closed the session in the same
run.

Step 16 also checks the official close the published step expects of the recorded market
session in its precondition: one valid quote of QTEA, a bid of 99.99 and an ask of 100.01
from the open, never replaced, and no quote of QTEB or QTEC. QTEA's `official_close` must
then have the value 100000000, and QTEB and QTEC must get none. That part runs only when
QTE_CONFORMANCE_INSTRUMENT is QTEA, the one instrument that recorded session quotes, and the
exchange under test must then be fed that recorded session. With any other instrument, or
if the exchange rejects QTEB or QTEC as unknown, step 16 makes its other checks and is
then skipped with the reason.

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
the second strategy is entered by the step itself. Step 15 has no checks until it is
specified (see issue #12) and is reported as an expected failure.
"""

import asyncio
import ipaddress
import os
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import pytest
from google.protobuf.message import Message

from qte_sdk.books import LatestBooks
from qte_sdk.calendar import next_open
from qte_sdk.connection import DecodeFailed, Received, SeqGap
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
from qte_sdk.market_data import as_market_data, subscribe
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
    """

    def __init__(self, session: Session, config: Settings) -> None:
        self.session = session
        self.config = config
        self.seen: list[Message] = []
        self.books = LatestBooks()
        self.state: SessionState | None = None
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self._failure: BaseException | None = None
        self._ended = False
        self._reader = asyncio.create_task(self._read())

    @classmethod
    async def connect(cls, config: Settings) -> "Client":
        session = await open_session(config.url, os.environ[TOKEN_VAR])
        # Read before the reader starts: the calendar follows the acknowledgement.
        await session.wait_for_calendar(config.timeout)
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
        if isinstance(event, SeqGap | DecodeFailed):
            raise AssertionError(f"messages were missed or unreadable: {event!r}")
        if not isinstance(event, Received):
            return True
        item = as_market_data(event)
        self.books.update(item)
        if isinstance(item, SessionState):
            self.state = item
        self.seen.append(event.message)
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
    """The wall ladder: ten ask levels and up to ten bid levels, best first, every level at
    least one share, and both sides spaced uniformly by the asks' own step.

    The bid ladder of a low-priced instrument stops at its last level with a positive
    price, so fewer than ten bids are accepted only when one more step below the last bid
    would not be positive.
    """
    asks = [level.price for level in book.ask_levels]
    bids = [level.price for level in book.bid_levels]
    assert len(asks) == 10, f"{len(asks)} ask levels, not ten"
    assert 1 <= len(bids) <= 10, f"{len(bids)} bid levels"
    step = asks[1] - asks[0]
    assert step > 0, "asks not best first"
    assert all(b - a == step for a, b in zip(asks[:-1], asks[1:], strict=True)), (
        "asks not evenly spaced"
    )
    assert all(a - b == step for a, b in zip(bids[:-1], bids[1:], strict=True)), (
        "bids not evenly spaced"
    )
    assert bids[0] < asks[0], "the bid ladder crosses the ask ladder"
    assert bids[-1] > 0, "a bid level without a positive price"
    if len(bids) < 10:
        assert bids[-1] - step <= 0, f"{len(bids)} bid levels, though the next would be positive"
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


async def test_step_02_subscribe(client: Client):
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
    # Read on to the first grid point after the release, so every book and mark published
    # up to it is in hand, then settle the preconditions before checking what the order did.
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
    # Two grid points on, any print for it would have been published.
    after = c.after(cancelled)
    state = await c.next_session_state(after)
    await c.next_session_state(c.after(state))
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


# Steps 14 to 16: the close.


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


@pytest.mark.xfail(
    reason="step 15 (heartbeat and resume) is not yet specified; see issue #12", run=False
)
async def test_step_15_heartbeat_and_resume():
    raise NotImplementedError


def unknown_among(reject: Reject, names: tuple[str, ...]) -> bool:
    """Whether `reject` refuses a subscription because the exchange does not know one of
    `names`."""
    return (
        reject.reason_code == ReasonCodes.UNKNOWN_INSTRUMENT
        and reject.HasField("instrument")
        and reject.instrument in names
    )


async def test_step_16_subscribe_outside_a_session(client: Client):
    if not _CLOSED_BY_STEP_14:
        pytest.skip("precondition: step 14 closed the instrument's session in this run")
    closed = _CLOSED_BY_STEP_14[0]
    c = client
    instrument = c.config.instrument
    # The step's expected value is for the one instrument its recorded session quotes, and
    # the subscribe then also names the two it never quotes, which must get no official close.
    expected_value = instrument == STEP_16_QUOTED
    unquoted = STEP_16_UNQUOTED if expected_value else ()
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
    if expected_value:
        assert not [m for m in closes if m.instrument in STEP_16_UNQUOTED], (
            "an official_close for QTEB or QTEC, which have no valid mark in the close window"
        )
    assert [m.instrument for m in closes] == [instrument], (
        "not exactly one official_close, for the instrument"
    )
    if not expected_value:
        pytest.skip(
            f"precondition: the instrument is {STEP_16_QUOTED}, the one instrument the "
            "recorded session of step 16 quotes; the official close value was not checked"
        )
    assert official.value == STEP_16_OFFICIAL_CLOSE, (
        f"{STEP_16_QUOTED}'s official close is {official.value}, not {STEP_16_OFFICIAL_CLOSE}"
    )
    if unknown:
        pytest.skip(
            "precondition: the exchange knows QTEB and QTEC, which step 16's recorded session "
            f"never quotes; rejected as unknown: {', '.join(unknown)}"
        )
