"""Subscribe to market data and consume it as typed messages.

    session = await open_session(url)  # see `qte_sdk.session`
    await subscribe(session, ["AAPL", "MSFT"])
    async for item in market_data(session):
        match item:
            case Book():
                best_bid = item.bid_levels[0].price if item.bid_levels else None
            case Trades() | Mark() | SessionState():
                ...
            case OfficialClose():
                last_close = to_decimal(item.value)  # outside a session only
            case SeqGap() | Disconnected() | DecodeFailed():
                ...  # messages were lost: treat what you hold as uncertain
            case Reject():
                ...  # a subscribe or unsubscribe was refused

There is one conflated market-data feed, the same for every participant. Messages are
delivered as they arrive; nothing here waits for, fills in or assumes a grid time.

An instrument's `Book` is published only when it has changed, so a grid point with no
`Book` means that instrument's book is unchanged; `SessionState` is the message sent at
every grid point of a session. A subscribe during a session is answered with the last
book published for each instrument that has one (an instrument with no book yet gets its
first book when it is published), which can be older than the latest `SessionState` and
can arrive again. `qte_sdk.books.LatestBooks` keeps the latest book per instrument.

Outside a session the exchange still answers a subscribe, once: a `SessionState` whose
`state` is `CLOSED`. The contract also provides an `OfficialClose` for each subscribed
instrument that has one, carrying that instrument's last official close: the
time-weighted average of the mark over the final five minutes of its session. The
exchange does not send it yet, so its absence is expected. No `Book`, `Trades` or `Mark`
arrives until a session opens.

That one out-of-hours `SessionState` can also name the next scheduled session:
`next_session_date`, `next_open_time` and `next_close_time` are set together, only on that
reply, and are absent when the term has no later session or the exchange predates them.
`until_next_open(state, session.info.server_time)` reads the wait until the next open from
it. The calendar (`qte_sdk.calendar`) stays the full schedule; these fields are a
convenience on that reply.

Messages are the generated contract classes. Prices are `int` micro-dollars and sizes are
`int` shares, exact at any size; use `qte_sdk.units.to_decimal` for exact `Decimal`
dollars. Each instrument's condition is the `condition` field of `Book` and of `Mark`
(an `InstrumentCondition` value), not a separate message. Timestamps are left as the
`int` the wire carries.
"""

from collections.abc import AsyncIterable, AsyncIterator, Iterable
from typing import Any, cast

from qte_sdk.connection import (
    DataUncertain,
    DecodeFailed,
    Disconnected,
    Event,
    Received,
    ReportGap,
    SeqGap,
)
from qte_sdk.contract.v1.common_pb2 import RequestType
from qte_sdk.contract.v1.market_data_pb2 import (
    Book,
    InstrumentCondition,
    Mark,
    OfficialClose,
    SessionState,
    StudentLevel,
    TapePrint,
    Trades,
    WallLevel,
)
from qte_sdk.contract.v1.order_events_pb2 import Reject
from qte_sdk.contract.v1.session_pb2 import Subscribe, Unsubscribe
from qte_sdk.orders import Sender

__all__ = [
    "MARKET_DATA_TYPES",
    "Book",
    "DecodeFailed",
    "Disconnected",
    "InstrumentCondition",
    "Mark",
    "MarketData",
    "MarketDataEvent",
    "OfficialClose",
    "Reject",
    "SeqGap",
    "SessionState",
    "StudentLevel",
    "TapePrint",
    "Trades",
    "WallLevel",
    "as_market_data",
    "market_data",
    "subscribe",
    "unsubscribe",
    "until_next_open",
]

MarketData = Book | Trades | Mark | SessionState | OfficialClose
"""One market-data message."""

MarketDataEvent = MarketData | Reject | SeqGap | Disconnected | DecodeFailed
"""What `market_data` yields: a message, a refused subscription change, or a sign that
messages were lost (`SeqGap`, `Disconnected` from a reconnecting session, `DecodeFailed`)."""

MARKET_DATA_TYPES: frozenset[str] = frozenset(
    {"book", "trades", "mark", "session_state", "official_close"}
)
"""The envelope `type` tokens of market-data messages."""

_SUBSCRIPTION_REQUESTS = frozenset({RequestType.SUBSCRIBE, RequestType.UNSUBSCRIBE})


async def subscribe(conn: Sender, instruments: Iterable[str]) -> None:
    """Ask the exchange to start sending market data for `instruments`.

    `conn` is a session (`Session` or `ReconnectingSession`) or a `Connection` that has
    been authenticated and acknowledged.

    Returns once the request is sent, which does not mean the exchange has accepted it:
    no acknowledgement message is defined. An instrument the exchange does not know comes
    back as a `Reject`, which `market_data` passes on. The subscription messages are not
    final yet and may change in a later contract version.
    """
    await conn.send("subscribe", Subscribe(instruments=_instrument_list(instruments)))


async def unsubscribe(conn: Sender, instruments: Iterable[str]) -> None:
    """Ask the exchange to stop sending market data for `instruments`.

    Returns once the request is sent, which does not mean the exchange has acted on it.
    This SDK keeps no subscription state and filters nothing locally: whatever the
    exchange sends is delivered.
    """
    await conn.send("unsubscribe", Unsubscribe(instruments=_instrument_list(instruments)))


def as_market_data(event: Event | Disconnected | object) -> MarketDataEvent | None:
    """The market-data meaning of one connection event, or None if it has none.

    Use this in your own loop over a connection when you also handle order events there.
    Returns the message for `book`, `trades`, `mark`, `session_state` and
    `official_close`; a `Reject` of a `subscribe` or `unsubscribe`; every `SeqGap` and
    `Disconnected` (any `DataUncertain` but `ReportGap`, which concerns only the team's
    private order reports), since messages may have been missed; and every
    `DecodeFailed`, since a message that could not be decoded may have been market data
    or a refused subscription, and sequence tracking has already counted it, so no later
    gap will report it.
    """
    if isinstance(event, Received):
        if event.type in MARKET_DATA_TYPES:
            return cast(MarketData, event.message)
        if (
            isinstance(event.message, Reject)
            and event.message.HasField("request_type")
            and event.message.request_type in _SUBSCRIPTION_REQUESTS
        ):
            return event.message
        return None
    if isinstance(event, SeqGap | Disconnected | DecodeFailed):
        return event
    if isinstance(event, ReportGap):
        return None  # private order reports only: no market data was missed
    if isinstance(event, DataUncertain):
        return cast(MarketDataEvent, event)  # a kind added later, passed on all the same
    return None


async def market_data(events: AsyncIterable[Any]) -> AsyncIterator[MarketDataEvent]:
    """Iterate over the market-data events in `events`: a `Session`, a
    `ReconnectingSession`, whose `Disconnected` events are passed on, or a `Connection`.

    This consumes the session: events that are not market data, such as order events,
    are skipped. To handle both on one session, loop over the session yourself and call
    `as_market_data` on each event.
    """
    async for event in events:
        item = as_market_data(event)
        if item is not None:
            yield item


def until_next_open(state: SessionState, now: int) -> int | None:
    """The time from `now` until the next session opens, as `state` names it, or None
    when `state` names no next session.

    Only the `SessionState` that answers a subscribe outside a session carries the next
    session; it is absent there too when the term has no later session. `now` must be an
    exchange timestamp, such as `session.info.server_time` (the time the session was
    acknowledged) or a later one, never your machine's clock:
    the result is `state.next_open_time - now`, in the exchange's time units, whose
    resolution the contract has not fixed. It is negative if `now` is already past that
    open.
    """
    if not state.HasField("next_open_time"):
        return None
    return state.next_open_time - now


def _instrument_list(instruments: Iterable[str]) -> list[str]:
    # A bare string is iterable too, and would otherwise subscribe to each of its letters.
    if isinstance(instruments, str):
        raise TypeError('pass a list of instrument ids, such as ["AAPL"], not a single string')
    return list(instruments)
