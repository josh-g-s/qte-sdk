"""Order entry and order events.

Four messages enter orders: `new`, `cancel`, `amend` and `mass_cancel`. There is no order
ID on the wire. A team's orders are addressed by instrument, side and price, so `cancel`
and `amend` act on every order the team has at that price level.

    ref = await send_new(conn, strat_id="mm-1", instrument="AAPL", side=BUY,
                         order_type=LIMIT, price=199_970_000, size=100)
    async for event in conn:
        if is_order_event(event) and request_ref_of(event.message) == ref:
            ...

Prices are integers in micro-dollars (1 dollar is 1_000_000) and sizes are whole shares.

Every send returns the `request_ref` it put on the message, so a caller can match the
`accepted` or `reject` that answers it, and the `order_cancelled` events a `cancel`,
`amend` or `mass_cancel` causes. Fills (`execution`), `order_state` and `risk_notice`
carry no `request_ref`. A fill of a limit order names the order's price level through its
strategy, instrument, side and `order_price`; a fill of a market order has no
`order_price`, so nothing on it ties it to one particular `new` when several market
orders for the same strategy, instrument and side are in flight.

Every order message is held by the exchange for its order delay before it is applied, so
an `accepted` arrives no sooner than that delay after the send. The delay, the minimum
time an order must rest before it may be cancelled or amended, the price collar and the
message budgets are all set by the exchange. This module assumes none of their values.
"""

import uuid
from typing import Protocol, TypeGuard

from google.protobuf.message import Message

from qte_sdk.connection import Event, Received
from qte_sdk.contract.v1.common_pb2 import (
    BUY,
    LIMIT,
    MARKET,
    SELL,
    OrderType,
    ReasonCodes,
    Side,
)
from qte_sdk.contract.v1.order_entry_pb2 import AmendOrder, CancelOrder, MassCancel, NewOrder
from qte_sdk.contract.v1.order_events_pb2 import (
    Accepted,
    Execution,
    OrderCancelled,
    OrderState,
    Reject,
    RiskNotice,
)

__all__ = [
    "ORDER_EVENT_TYPES",
    "OrderEvent",
    "Sender",
    "is_order_event",
    "new_request_ref",
    "reason_code_name",
    "request_ref_of",
    "send_amend",
    "send_cancel",
    "send_mass_cancel",
    "send_new",
]

# The envelope `type` tokens of the events the exchange sends about a team's own orders.
ORDER_EVENT_TYPES = frozenset(
    {"accepted", "reject", "execution", "order_cancelled", "order_state", "risk_notice"}
)

OrderEvent = Accepted | Reject | Execution | OrderCancelled | OrderState | RiskNotice


class Sender(Protocol):
    """Anything that sends one message by its envelope type, such as a `Connection`."""

    async def send(self, type_: str, payload: Message) -> None: ...


def new_request_ref() -> str:
    """A fresh client correlation reference: 32 lowercase hex characters, unique per call."""
    return uuid.uuid4().hex


def _side(side: Side) -> Side:
    if side not in (BUY, SELL):
        raise ValueError(f"side must be BUY or SELL, got {side!r}")
    return side


async def send_new(
    conn: Sender,
    *,
    strat_id: str,
    instrument: str,
    side: Side,
    order_type: OrderType,
    size: int,
    price: int | None = None,
    request_ref: str | None = None,
) -> str:
    """Send `new`: one order for one strategy at one price level. Returns its `request_ref`.

    A LIMIT order needs `price`; a MARKET order must not have one. To change the size of an
    order already resting, use `send_amend` rather than a second `new` at the same level.
    """
    if order_type == LIMIT:
        if price is None:
            raise ValueError("a LIMIT order needs a price")
    elif order_type == MARKET:
        if price is not None:
            raise ValueError("a MARKET order carries no price")
    else:
        raise ValueError(f"order_type must be LIMIT or MARKET, got {order_type!r}")
    ref = new_request_ref() if request_ref is None else request_ref
    msg = NewOrder(
        request_ref=ref,
        strat_id=strat_id,
        instrument=instrument,
        side=_side(side),
        order_type=order_type,
        size=size,
    )
    if price is not None:
        msg.price = price
    await conn.send("new", msg)
    return ref


async def send_cancel(
    conn: Sender,
    *,
    instrument: str,
    side: Side,
    price: int,
    request_ref: str | None = None,
) -> str:
    """Send `cancel`: clear every order the team has at one price level. Returns its
    `request_ref`, which each resulting `order_cancelled` echoes."""
    ref = new_request_ref() if request_ref is None else request_ref
    msg = CancelOrder(request_ref=ref, instrument=instrument, side=_side(side), price=price)
    await conn.send("cancel", msg)
    return ref


async def send_amend(
    conn: Sender,
    *,
    instrument: str,
    side: Side,
    price: int,
    new_size: int,
    new_price: int | None = None,
    request_ref: str | None = None,
) -> str:
    """Send `amend`: change the team's orders at one price level. Returns its `request_ref`.

    `price` names the level to change. `new_size` is the new total remaining size of the
    team's orders there, not a size to add. Leave `new_price` out, or pass `price`, for a
    size-only amend. An order the amend cuts to nothing is reported as `order_cancelled`
    with reason `AMEND_CUT`, echoing this `request_ref`.
    """
    ref = new_request_ref() if request_ref is None else request_ref
    msg = AmendOrder(
        request_ref=ref,
        instrument=instrument,
        side=_side(side),
        price=price,
        new_price=price if new_price is None else new_price,
        new_size=new_size,
    )
    await conn.send("amend", msg)
    return ref


async def send_mass_cancel(conn: Sender, *, request_ref: str | None = None) -> str:
    """Send `mass_cancel`: cancel every order the team has on the exchange. Returns its
    `request_ref`, which each resulting `order_cancelled` echoes."""
    ref = new_request_ref() if request_ref is None else request_ref
    await conn.send("mass_cancel", MassCancel(request_ref=ref))
    return ref


def is_order_event(event: Event) -> TypeGuard[Received]:
    """Whether `event` is a decoded order event, one of `ORDER_EVENT_TYPES`.

    Its `message` is then one of `OrderEvent`. Every rejected message shares the one
    `reject` shape, so a rejected subscription is included too; its `request_type` says
    which kind of message was rejected.
    """
    return isinstance(event, Received) and event.type in ORDER_EVENT_TYPES


def request_ref_of(message: Message) -> str | None:
    """The `request_ref` an order event echoes, or None when it carries none.

    `accepted` always echoes one. A `reject` echoes one unless the exchange could not read
    it from the message it rejects. An `order_cancelled` echoes one only when the team's
    own `cancel`, `amend` or `mass_cancel` caused it. `execution`, `order_state` and
    `risk_notice` never carry one.
    """
    if isinstance(message, Accepted):
        return message.request_ref
    if isinstance(message, Reject | OrderCancelled):
        return message.request_ref if message.HasField("request_ref") else None
    return None


def reason_code_name(code: int) -> str:
    """The name of a reason code, or its number as text when this SDK does not know it.

    An unknown code number is kept on the decoded message and named here by its number,
    so it can be logged and compared without crashing. A reason token this SDK does not
    recognise decodes as `REASON_CODE_UNSPECIFIED` (0).
    """
    try:
        return ReasonCodes.ReasonCode.Name(code)
    except ValueError:
        return str(code)
