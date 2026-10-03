"""A local view of the team's resting orders, built only from exchange events.

The exchange addresses orders by level key: instrument, side and price. A team holds at
most one resting order per level key, across all of its strategies, so the view keeps one
entry per key. Every order message is delayed and can be rejected, so nothing the client
sends changes the view; only what the exchange reports does:

- `order_state` (RESTING or STALE) adds or replaces the entry at its level key. This is how
  an order that rests, and the result of an amend that leaves it resting, are reported.
- An amend's `order_state` carries `old_price`, the order's price before the amend. The
  entry at `old_price` is removed first, so an amend that moves the price re-keys the
  entry from the old level to the new one, and one that changes only the size (where
  `old_price` equals `price`) replaces it in place. When the amend ended the order, its
  `order_state` reads FILLED or CANCELLED with `remaining_size` 0, and nothing is left at
  either price.
- `execution` with an `order_price` sets the entry's remaining size from
  `remaining_size`, and removes the entry when that reaches 0.
- `order_cancelled` with a `price` removes the entry, whatever the reason: a cancel, a
  mass-cancel (one `order_cancelled` per order), the purge of a STALE order, the close.
- `accepted` and `reject` change nothing. `accepted` carries no level key, and a rejected
  amend leaves the order unchanged.

    view = RestingOrders()
    async for event in view.follow(conn):
        ...
    order = view.get("AAPL", BUY, 199_970_000)

What the view cannot know:

- An order becoming STALE is never reported to its owner, so an entry can read RESTING
  while the exchange holds it STALE. The purge that follows is reported and removes it.
- An older exchange that does not send `old_price` gives no event naming the price an
  amend moved an order away from. The view then keeps the entry at the old price: an
  `order_state` at the new price adds the new entry beside it, and an amend that fills
  in full leaves it alone. `incomplete` is not set. Against such an exchange, cancel and
  re-enter instead of amending the price if you need an accurate view. Size-only amends
  are tracked correctly either way.
- A new view starts empty, which is right only if the team has no resting orders when it
  starts. If orders may already rest (for example on a reconnect), call
  `mark_incomplete()`: nothing yet reports the orders already on the book.
- Events missed on a sequence gap, a frame that could not be decoded, or a disconnect can
  leave the view wrong. It is then marked `incomplete` and stays so, since there is no
  resume yet. Following a `ReconnectingSession`, its `Disconnected` event marks the view
  incomplete too.
"""

from collections.abc import AsyncIterable, AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

from google.protobuf.message import Message

from qte_sdk.connection import DataUncertain, DecodeFailed, Disconnected, Event, Received
from qte_sdk.contract.v1.common_pb2 import RESTING, STALE
from qte_sdk.contract.v1.order_events_pb2 import Execution, OrderCancelled, OrderState

# Message types that can change the view. A frame of one of these, or of no readable
# type, that could not be decoded may have carried a change the view has now missed.
_ORDER_EVENT_TYPES = frozenset(
    {"accepted", "reject", "execution", "order_cancelled", "order_state"}
)


@dataclass(frozen=True)
class LevelKey:
    """Where an order rests: instrument, side (`BUY` or `SELL`) and price in micro-units."""

    instrument: str
    side: int
    price: int


@dataclass(frozen=True)
class RestingOrder:
    """One of the team's resting orders as last reported by the exchange."""

    key: LevelKey
    strat_id: str
    remaining_size: int
    # `RESTING` or `STALE` as last reported; a move to STALE is never reported.
    state: int
    # When the order became STALE, if an `order_state` reported it STALE.
    stale_since: int | None


class RestingOrders:
    """The team's resting orders, keyed by level key, updated only from exchange events.

    An amend that moves an order's price re-keys its entry, using the `old_price` the
    exchange reports on the amend's `order_state`. An older exchange that does not send
    `old_price` leaves the entry at the old price in place; see the module docstring.
    """

    def __init__(self) -> None:
        self._orders: dict[LevelKey, RestingOrder] = {}
        self._incomplete = False

    @property
    def incomplete(self) -> bool:
        """True once events may have been missed; the view may then be wrong."""
        return self._incomplete

    def mark_incomplete(self) -> None:
        """Mark the view incomplete, for example after a disconnect."""
        self._incomplete = True

    def get(self, instrument: str, side: int, price: int) -> RestingOrder | None:
        return self._orders.get(LevelKey(instrument, side, price))

    def __contains__(self, key: object) -> bool:
        return key in self._orders

    def __iter__(self) -> Iterator[RestingOrder]:
        return iter(list(self._orders.values()))

    def __len__(self) -> int:
        return len(self._orders)

    def apply(self, event: Event | Disconnected | Message | object) -> None:
        """Update the view from one connection event or one decoded exchange message.

        A `SeqGap` or `Disconnected` (any `DataUncertain`) marks the view incomplete. Events
        the view has no use for, such as a reconnecting session's `Connected`, are ignored.
        """
        if isinstance(event, DataUncertain):
            self.mark_incomplete()
        elif isinstance(event, DecodeFailed):
            if event.type is None or event.type in _ORDER_EVENT_TYPES:
                self.mark_incomplete()
        elif isinstance(event, Received):
            self.apply(event.message)
        elif isinstance(event, OrderState):
            self._on_order_state(event)
        elif isinstance(event, Execution):
            self._on_execution(event)
        elif isinstance(event, OrderCancelled):
            self._on_order_cancelled(event)
        # Anything else (accepted, reject, market data, unknown types) leaves the view as is.

    async def follow(self, events: AsyncIterable[Any]) -> AsyncIterator[Any]:
        """Apply every event from a connection, or a reconnecting session, and pass it on.

        When the connection closes or drops, the view is marked incomplete, since later
        events will not reach it. A caller that stops iterating early should close the
        iterator, for example with `contextlib.aclosing`, so the view is marked at once.
        """
        try:
            async for event in events:
                self.apply(event)
                yield event
        finally:
            self.mark_incomplete()

    def _on_order_state(self, msg: OrderState) -> None:
        key = LevelKey(msg.instrument, msg.side, msg.price)
        if msg.HasField("old_price"):
            # An amend: whatever it did, the order no longer rests at its old price.
            self._orders.pop(LevelKey(msg.instrument, msg.side, msg.old_price), None)
            if msg.state not in (RESTING, STALE):
                # The amend ended the order (FILLED or CANCELLED): nothing rests at price.
                self._orders.pop(key, None)
                return
        if msg.state not in (RESTING, STALE):
            return  # without old_price, only a resting order is reported
        stale_since = msg.stale_since if msg.HasField("stale_since") else None
        self._orders[key] = RestingOrder(
            key, msg.strat_id, msg.remaining_size, msg.state, stale_since
        )

    def _on_execution(self, msg: Execution) -> None:
        if not msg.HasField("order_price"):
            return  # a market order, which never rests
        key = LevelKey(msg.instrument, msg.side, msg.order_price)
        order = self._orders.get(key)
        if order is None:
            return  # not resting yet: the fill of an incoming order, before any remainder rests
        if msg.remaining_size == 0:
            del self._orders[key]
        else:
            self._orders[key] = RestingOrder(
                key, order.strat_id, msg.remaining_size, order.state, order.stale_since
            )

    def _on_order_cancelled(self, msg: OrderCancelled) -> None:
        if msg.HasField("price"):
            self._orders.pop(LevelKey(msg.instrument, msg.side, msg.price), None)
