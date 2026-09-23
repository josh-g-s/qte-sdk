"""Subscribe to market data and consume it as typed messages.

    # `conn` must be authenticated first: the exchange refuses a subscribe on a
    # connection whose session it has not acknowledged.
    await subscribe(conn, ["AAPL", "MSFT"])
    async for item in market_data(conn):
        match item:
            case Book():
                best_bid = item.bid_levels[0].price if item.bid_levels else None
            case Trades() | Mark() | SessionState():
                ...
            case SeqGap() | DecodeFailed():
                ...  # messages were lost: treat what you hold as uncertain
            case Reject():
                ...  # a subscribe or unsubscribe was refused

There is one conflated market-data feed, the same for every participant. Messages are
delivered as they arrive; nothing here waits for, fills in or assumes a grid time.

Messages are the generated contract classes. Prices are `int` micro-dollars and sizes are
`int` shares, exact at any size; use `qte_sdk.units.to_decimal` for exact `Decimal`
dollars. Each instrument's condition is the `condition` field of `Book` and of `Mark`
(an `InstrumentCondition` value), not a separate message. Timestamps are left as the
`int` the wire carries.
"""

from collections.abc import AsyncIterable, AsyncIterator, Iterable
from typing import cast

from qte_sdk.connection import Connection, DecodeFailed, Event, Received, SeqGap
from qte_sdk.contract.v1.common_pb2 import RequestType
from qte_sdk.contract.v1.market_data_pb2 import (
    Book,
    InstrumentCondition,
    Mark,
    SessionState,
    StudentLevel,
    TapePrint,
    Trades,
    WallLevel,
)
from qte_sdk.contract.v1.order_events_pb2 import Reject
from qte_sdk.contract.v1.session_pb2 import Subscribe, Unsubscribe

__all__ = [
    "MARKET_DATA_TYPES",
    "Book",
    "DecodeFailed",
    "InstrumentCondition",
    "Mark",
    "MarketData",
    "MarketDataEvent",
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
]

MarketData = Book | Trades | Mark | SessionState
"""One market-data message."""

MarketDataEvent = MarketData | Reject | SeqGap | DecodeFailed
"""What `market_data` yields: a message, a refused subscription change, or a sign that
messages were lost."""

MARKET_DATA_TYPES: frozenset[str] = frozenset({"book", "trades", "mark", "session_state"})
"""The envelope `type` tokens of market-data messages."""

_SUBSCRIPTION_REQUESTS = frozenset({RequestType.SUBSCRIBE, RequestType.UNSUBSCRIBE})


async def subscribe(conn: Connection, instruments: Iterable[str]) -> None:
    """Ask the exchange to start sending market data for `instruments`.

    Returns once the request is sent, which does not mean the exchange has accepted it:
    no acknowledgement message is defined. An instrument the exchange does not know comes
    back as a `Reject`, which `market_data` passes on. The subscription messages are not
    final yet and may change in a later contract version.
    """
    await conn.send("subscribe", Subscribe(instruments=_instrument_list(instruments)))


async def unsubscribe(conn: Connection, instruments: Iterable[str]) -> None:
    """Ask the exchange to stop sending market data for `instruments`.

    Returns once the request is sent, which does not mean the exchange has acted on it.
    This SDK keeps no subscription state and filters nothing locally: whatever the
    exchange sends is delivered.
    """
    await conn.send("unsubscribe", Unsubscribe(instruments=_instrument_list(instruments)))


def as_market_data(event: Event) -> MarketDataEvent | None:
    """The market-data meaning of one connection event, or None if it has none.

    Use this in your own loop over a connection when you also handle order events there.
    Returns the message for `book`, `trades`, `mark` and `session_state`; a `Reject` of a
    `subscribe` or `unsubscribe`; and every `SeqGap` and `DecodeFailed`, since a message
    that could not be decoded may have been market data or a refused subscription, and
    sequence tracking has already counted it, so no later gap will report it.
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
    if isinstance(event, SeqGap | DecodeFailed):
        return event
    return None


async def market_data(events: AsyncIterable[Event]) -> AsyncIterator[MarketDataEvent]:
    """Iterate over the market-data events in `events`, usually a `Connection`.

    This consumes the connection: events that are not market data, such as order events,
    are skipped. To handle both on one connection, loop over the connection yourself and
    call `as_market_data` on each event.
    """
    async for event in events:
        item = as_market_data(event)
        if item is not None:
            yield item


def _instrument_list(instruments: Iterable[str]) -> list[str]:
    # A bare string is iterable too, and would otherwise subscribe to each of its letters.
    if isinstance(instruments, str):
        raise TypeError('pass a list of instrument ids, such as ["AAPL"], not a single string')
    return list(instruments)
