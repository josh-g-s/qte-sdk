"""Rest a quote on both sides of one instrument and keep it there.

Set QTE_URL and QTE_TOKEN first (see docs/quickstart.md), then:

    python examples/quote_both_sides.py --instrument AAPL --strat-id <your strategy> --seconds 30

The example quotes one limit order of --size shares on each side, --inside dollars inside
the best price the wall shows on that side, and manages the two orders until --seconds
have passed:

- when the wall's best price moves, it cancels its order and enters a new one at the new
  price (cancel and re-enter, rather than amending the price);
- when a fill leaves an order smaller than --size, it amends the order's size back up;
- when an order fills completely or is cancelled, it enters a new one.

It then cancels its own orders and waits at most --drain-seconds for the exchange to
confirm. The exchange decides which prices are valid and where an order may rest: an
order it will not take comes back as a `reject`, and one it will not leave resting as an
`order_cancelled`, each printed with its reason code. This is a teaching example, not a
strategy: it makes no attempt to make money.

Timing. The exchange holds every order message (new, cancel, amend and mass cancel) for
its order delay before applying it, currently 150 ms, so an `accepted` arrives at least
that long after the send; the example prints each round trip it measures. The delay, the
minimum time an order must rest before it may be cancelled or amended, the price collar
and the message budgets are all set by the exchange and may change, so nothing here
depends on their values. Instead the example reacts to what the exchange reports: it only
cancels or amends an order once the exchange has reported it resting, and it sends at
most one message per side per --requote-seconds, which keeps it well inside the budgets.
If a message is rejected anyway (for example MIN_REST_VIOLATION or
MESSAGE_BUDGET_EXCEEDED), the reject is printed and the example tries again later.
"""

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass

from google.protobuf.message import Message
from websockets.exceptions import ConnectionClosedError, InvalidHandshake

from qte_sdk.connection import Event, SessionRejected
from qte_sdk.contract.v1.common_pb2 import BUY, LIMIT, SELL, ReasonCodes, RiskNoticeKind, Side
from qte_sdk.contract.v1.order_events_pb2 import (
    Accepted,
    Execution,
    OrderCancelled,
    OrderState,
    Reject,
    RiskNotice,
)
from qte_sdk.market_data import Book, DecodeFailed, SeqGap, as_market_data, subscribe
from qte_sdk.orders import (
    is_order_event,
    reason_code_name,
    request_ref_of,
    send_amend,
    send_cancel,
    send_new,
)
from qte_sdk.resting import RestingOrder, RestingOrders
from qte_sdk.session import MissingToken, Session, SessionNotAcknowledged, open_session
from qte_sdk.units import to_decimal, to_micros

SIDE_NAMES = {BUY: "BUY", SELL: "SELL"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rest a quote on both sides and manage it.")
    parser.add_argument(
        "--instrument",
        default=os.environ.get("QTE_INSTRUMENT", "AAPL"),
        help="instrument to quote (default: $QTE_INSTRUMENT, else AAPL)",
    )
    parser.add_argument(
        "--strat-id",
        default=os.environ.get("QTE_STRAT_ID"),
        help="a strategy ID registered for your team (default: $QTE_STRAT_ID)",
    )
    parser.add_argument("--size", type=int, default=1, help="shares per side (default 1)")
    parser.add_argument(
        "--inside",
        type=to_micros,  # dollars as text, converted exactly to micro-dollars
        default="0.01",
        help=(
            "how far inside the wall's best price to quote, in dollars (default 0.01); "
            "the exchange sets the price tick and where an order may rest"
        ),
    )
    parser.add_argument(
        "--seconds", type=float, default=30.0, help="quote for this long (default 30)"
    )
    parser.add_argument(
        "--requote-seconds",
        type=float,
        default=1.0,
        help="least time between two order messages on one side (default 1)",
    )
    parser.add_argument(
        "--drain-seconds",
        type=float,
        default=5.0,
        help="longest wait for the final cancels to be confirmed (default 5)",
    )
    args = parser.parse_args(argv)
    if not args.strat_id:
        parser.error(
            "pass --strat-id or set QTE_STRAT_ID to a strategy ID registered for your team"
        )
    return args


def price_text(price: int) -> str:
    return str(to_decimal(price))


@dataclass
class Quote:
    """What this example believes about its order on one side."""

    side: Side
    # The price level its order is at, or is being sent to; None when it has no order.
    price: int | None = None
    # The request_ref of the message awaiting `accepted` or `reject`, and its kind.
    pending_ref: str | None = None
    pending_kind: str = ""
    sent_at: float = float("-inf")


class Quoter:
    def __init__(self, session: Session, view: RestingOrders, args: argparse.Namespace) -> None:
        self.conn = session.connection
        self.view = view
        self.instrument: str = args.instrument
        self.strat_id: str = args.strat_id
        self.size: int = args.size
        self.inside: int = args.inside  # micro-dollars
        self.requote_seconds: float = args.requote_seconds
        self.quotes = {BUY: Quote(BUY), SELL: Quote(SELL)}
        self.quoting = True

    # Sending

    def ready(self, quote: Quote) -> bool:
        """Whether this side may send now: nothing in flight, and not sent too recently."""
        waited = time.monotonic() - quote.sent_at
        return quote.pending_ref is None and waited >= self.requote_seconds

    def resting(self, quote: Quote) -> RestingOrder | None:
        """This example's order at its price, as the exchange last reported it, or None."""
        if quote.price is None:
            return None
        order = self.view.get(self.instrument, quote.side, quote.price)
        if order is None or order.strat_id != self.strat_id:
            return None
        return order

    def sent(self, quote: Quote, ref: str, kind: str) -> None:
        quote.pending_ref, quote.pending_kind, quote.sent_at = ref, kind, time.monotonic()
        where = f"{SIDE_NAMES[quote.side]} {self.instrument} @ {price_text(quote.price or 0)}"
        print(f"sent   {kind:<6} {where}")

    async def manage(self, quote: Quote, best: int) -> None:
        """Move one side towards resting --size shares at `best`."""
        if not self.ready(quote):
            return
        if quote.price is None:
            quote.price = best
            ref = await send_new(
                self.conn,
                strat_id=self.strat_id,
                instrument=self.instrument,
                side=quote.side,
                order_type=LIMIT,
                price=best,
                size=self.size,
            )
            self.sent(quote, ref, "new")
            return
        order = self.resting(quote)
        if order is None:
            return  # not reported resting yet: wait rather than guess
        if quote.price != best:
            # Cancel and re-enter: the new order goes in once the cancel is confirmed.
            ref = await send_cancel(
                self.conn, instrument=self.instrument, side=quote.side, price=quote.price
            )
            self.sent(quote, ref, "cancel")
        elif order.remaining_size < self.size:
            # new_size is the new total remaining size, not an amount to add.
            ref = await send_amend(
                self.conn,
                instrument=self.instrument,
                side=quote.side,
                price=quote.price,
                new_size=self.size,
            )
            self.sent(quote, ref, "amend")

    async def cancel_own(self) -> bool:
        """Cancel this example's orders. Returns True once none are left or in flight."""
        done = True
        for quote in self.quotes.values():
            if quote.pending_ref is not None:
                done = False
            elif quote.price is not None:
                done = False
                if self.ready(quote) and self.resting(quote) is not None:
                    ref = await send_cancel(
                        self.conn, instrument=self.instrument, side=quote.side, price=quote.price
                    )
                    self.sent(quote, ref, "cancel")
        # Done once the exchange has also reported each order gone from the book.
        return done and not any(
            order.strat_id == self.strat_id and order.key.instrument == self.instrument
            for order in self.view
        )

    # Receiving

    async def on_book(self, book: Book) -> None:
        if not self.quoting or book.instrument != self.instrument:
            return
        # Quote --inside dollars inside the wall's best price on each side that has one.
        bid = book.bid_levels[0].price + self.inside if book.bid_levels else None
        ask = book.ask_levels[0].price - self.inside if book.ask_levels else None
        if bid is not None and ask is not None and bid >= ask:
            return  # the wall's spread is too narrow to quote inside
        if bid is not None:
            await self.manage(self.quotes[BUY], bid)
        if ask is not None:
            await self.manage(self.quotes[SELL], ask)

    def pending(self, ref: str | None) -> Quote | None:
        for quote in self.quotes.values():
            if ref is not None and quote.pending_ref == ref:
                return quote
        return None

    def on_order_event(self, message: Message) -> None:
        ref = request_ref_of(message)
        quote = self.pending(ref)
        match message:
            case Accepted() if quote is not None:
                elapsed_ms = (time.monotonic() - quote.sent_at) * 1000
                print(f"accepted {quote.pending_kind} after a {elapsed_ms:.0f} ms round trip")
                if quote.pending_kind == "cancel":
                    quote.price = None
                quote.pending_ref = None
            case Reject():
                detail = f" ({message.reason_detail})" if message.HasField("reason_detail") else ""
                reason = reason_code_name(message.reason_code)
                kind = quote.pending_kind if quote is not None else "message"
                print(f"REJECTED {kind}: {reason}{detail}")
                if quote is not None:
                    if quote.pending_kind == "new" or (
                        quote.pending_kind == "cancel"
                        and message.reason_code == ReasonCodes.NO_ORDER_AT_LEVEL
                    ):
                        quote.price = None
                    quote.pending_ref = None
            case OrderState() if message.instrument == self.instrument:
                side = SIDE_NAMES.get(message.side, "?")
                print(
                    f"resting {side} {message.remaining_size} @ {price_text(message.price)} "
                    f"(strategy {message.strat_id})"
                )
            case Execution() if message.instrument == self.instrument:
                side = SIDE_NAMES.get(message.side, "?")
                print(
                    f"FILL   {side} {message.fill_size} @ {price_text(message.fill_price)}, "
                    f"{message.remaining_size} left, fee {to_decimal(message.fee)}"
                )
                own = self.quotes.get(message.side)
                if (
                    own is not None
                    and message.HasField("order_price")
                    and message.order_price == own.price
                    and message.remaining_size == 0
                ):
                    own.price = None  # filled completely: enter a new order next time
            case OrderCancelled() if message.instrument == self.instrument:
                side = SIDE_NAMES.get(message.side, "?")
                price = price_text(message.price) if message.HasField("price") else "market"
                reason = reason_code_name(message.reason_code)
                print(f"cancelled {side} {message.cancelled_size} @ {price} ({reason})")
                own = self.quotes.get(message.side)
                if own is not None and message.HasField("price") and message.price == own.price:
                    own.price = None
            case RiskNotice():
                print(f"risk notice: {RiskNoticeKind.Name(message.kind)}")


async def pump(session: Session, queue: asyncio.Queue) -> None:
    """Read every event into `queue`, then None when the exchange closes the connection, or
    the exception if it drops. Only this task reads the session. A separate reader lets
    the main loop wait for the next event with a timeout without disturbing the stream."""
    try:
        async for event in session:
            await queue.put(event)
    except Exception as error:
        await queue.put(error)
    else:
        await queue.put(None)


async def next_event(queue: asyncio.Queue, timeout: float):
    """The next item from `pump`, or TimeoutError if none arrives within `timeout`."""
    try:
        return await asyncio.wait_for(queue.get(), max(timeout, 0))
    except TimeoutError as timeout_error:
        return timeout_error


async def handle(quoter: Quoter, view: RestingOrders, event: Event) -> bool:
    """Act on one event. Returns False when there is no point quoting on."""
    # Update the view of resting orders first, from this event and in stream order.
    view.apply(event)
    item = as_market_data(event)
    if isinstance(item, Book):
        await quoter.on_book(item)
    elif isinstance(item, Reject):
        # The exchange refused the subscription, for example an unknown instrument.
        print(f"subscription refused: {reason_code_name(item.reason_code)}")
        return False
    elif isinstance(item, SeqGap | DecodeFailed):
        # The view may now be wrong, and stays marked incomplete.
        print("warning: messages were missed, so the resting view may be wrong")
    elif is_order_event(event):
        quoter.on_order_event(event.message)
    return True


async def run(url: str, args: argparse.Namespace) -> int:
    # The token comes from the QTE_TOKEN environment variable.
    session = await open_session(url)
    async with session:
        print(f"connected: team {session.info.team}, unscored session: {session.info.unscored}")
        await subscribe(session.connection, [args.instrument])

        view = RestingOrders()
        quoter = Quoter(session, view, args)
        queue: asyncio.Queue = asyncio.Queue()
        reader = asyncio.create_task(pump(session, queue))
        loop = asyncio.get_running_loop()
        try:
            # Quote until the time is up.
            deadline = loop.time() + args.seconds
            while True:
                item = await next_event(queue, deadline - loop.time())
                if item is None:
                    print("the exchange closed the connection")
                    return 1
                if isinstance(item, TimeoutError):
                    break
                if isinstance(item, Exception):
                    raise item  # the connection dropped
                if not await handle(quoter, view, item):
                    break

            # Then cancel this example's own orders and wait, briefly, for confirmation.
            print("time is up: cancelling")
            quoter.quoting = False
            deadline = loop.time() + args.drain_seconds
            while not await quoter.cancel_own():
                if loop.time() >= deadline:
                    left = [
                        f"{SIDE_NAMES[q.side]} @ {price_text(q.price)}"
                        for q in quoter.quotes.values()
                        if q.price is not None
                    ]
                    print(f"WARNING: may still be resting: {', '.join(left)}; cancel it yourself")
                    return 1
                # Wake at least every requote interval to retry a rejected cancel.
                wait = min(deadline - loop.time(), args.requote_seconds)
                item = await next_event(queue, wait)
                if item is None:
                    print("the exchange closed the connection")
                    return 1
                if isinstance(item, TimeoutError):
                    continue
                if isinstance(item, Exception):
                    raise item
                await handle(quoter, view, item)
            print("all of this example's orders are cancelled")
            return 0
        finally:
            reader.cancel()


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    url = os.environ.get("QTE_URL")
    if not url:
        fail("set QTE_URL to the exchange address, for example ws://127.0.0.1:8080/ws")
        return 2
    try:
        return asyncio.run(run(url, args))
    except MissingToken:
        fail("set QTE_TOKEN to your practice token (see docs/quickstart.md)")
        return 2
    except ValueError as error:
        return fail(f"not sent: {error}")
    except SessionRejected as error:
        return fail(f"the exchange refused the session: {error}")
    except (OSError, InvalidHandshake, SessionNotAcknowledged) as error:
        return fail(f"could not connect to QTE_URL: {error}")
    except ConnectionClosedError:
        return fail(
            "the connection dropped; the SDK does not reconnect for you yet. Your orders may "
            "still rest: reconnect and cancel them"
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
