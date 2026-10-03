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

It then cancels the two levels it quoted and waits at most --drain-seconds for the
exchange to confirm. Pressing Ctrl+C does the same, even while those cancels are being
confirmed; press it twice to stop at once. If messages from the exchange are missed,
the example can no longer tell which orders are its own, so it stops sending, lists the
orders it believes it has, and leaves them for you to check.

Run it on prices your team is not otherwise trading. A cancel names a price level, not
an order, and is applied only after the order delay, so it removes whichever of your
team's orders rests at that level by then: if this example's order fills while its
cancel is delayed and a teammate's strategy enters an order at the same price, the
cancel removes the teammate's order.

The exchange decides which prices are valid and where an order may rest: an order it
will not take comes back as a `reject`, and one it will not leave resting as an
`order_cancelled`, each printed with its reason code. This is a teaching example, not a
strategy: it makes no attempt to make money.

Why --inside. Your orders may rest only strictly inside the band between the wall's best
bid and best ask. An order at the wall's own price is not left resting: it comes back as
an `order_cancelled` with reason REMAINDER_OUTSIDE_BAND. So --inside must be at least one
tick, and it must be a whole number of ticks, since the exchange rejects a price that is
not on the tick. The exchange sets each instrument's tick: the default, 0.01, is one tick
only where the tick is $0.01.

Timing. The exchange holds every order message (new, cancel, amend and mass cancel) for
its order delay before applying it, currently 150 ms, so an `accepted` arrives at least
that long after the send; the example prints each round trip it measures. The delay, the
minimum time an order must rest before it may be cancelled or amended, the price collar
and the message budgets are all set by the exchange and may change, so nothing here
depends on their values. Instead the example reacts to what the exchange reports: it only
cancels or amends an order once the exchange has reported it resting. It also paces
itself, sending at most one message per side per --requote-seconds; that is local
pacing, not a promise to stay within your team's budgets, which count every message your
team sends. If a message is rejected (for example MIN_REST_VIOLATION or
MESSAGE_BUDGET_EXCEEDED), the reject is printed and the example tries again later.

When it acts. The exchange publishes a book only when it changes, so in a quiet market
the next book may be a long time coming. The example keeps the latest book it has
received with `qte_sdk.books.LatestBooks` and decides what to send from that book, not
only when a new book arrives: also after every other event and on its own timer, at
least once per --requote-seconds. So a retry after a reject, or a new order after a fill
or a cancel, does not wait for the market to move. Before it sends anything, it applies
every event it has already received, so a sign that messages were missed is seen first.
If a message that may have been a newer book is missed or cannot be read, the book held
may be out of date, so the example sends nothing from it until a newer book arrives. It
also quotes only while the latest session state says the market is open, so a book kept
from before the close is not quoted after it. It sends one message at a time and applies
what has come in since before sending the next.
"""

import argparse
import asyncio
import contextlib
import os
import signal
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from google.protobuf.message import Message
from websockets.exceptions import ConnectionClosedError, InvalidHandshake

from qte_sdk.books import LatestBooks
from qte_sdk.connection import Event, SessionRejected
from qte_sdk.contract.v1.common_pb2 import (
    BUY,
    LIMIT,
    SELL,
    MarketSessionPhase,
    ReasonCodes,
    RiskNoticeKind,
    Side,
)
from qte_sdk.contract.v1.order_events_pb2 import (
    Accepted,
    Execution,
    OrderCancelled,
    OrderState,
    Reject,
    RiskNotice,
)
from qte_sdk.market_data import DecodeFailed, SeqGap, SessionState, as_market_data, subscribe
from qte_sdk.orders import (
    is_order_event,
    new_request_ref,
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
            "how far inside the wall's best price to quote, in dollars (default 0.01): a "
            "whole number of the instrument's price ticks, and at least one, because an "
            "order at the wall's own price is not left resting (orders rest strictly "
            "inside the wall's best bid and ask); the exchange sets the tick"
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
    if args.requote_seconds <= 0:
        parser.error("--requote-seconds must be more than 0")
    if args.inside <= 0:
        # The tick is not known here, so this only checks for at least something; a
        # price off the tick comes back as a reject with its reason code.
        parser.error("--inside must be at least one price tick, for example 0.01")
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
    # True from an accepted cancel until the exchange reports the order gone.
    cancelling: bool = False


class Quoter:
    def __init__(self, session: Session, view: RestingOrders, args: argparse.Namespace) -> None:
        self.session = session
        self.view = view
        self.instrument: str = args.instrument
        self.strat_id: str = args.strat_id
        self.size: int = args.size
        self.inside: int = args.inside  # micro-dollars
        self.requote_seconds: float = args.requote_seconds
        self.quotes = {BUY: Quote(BUY), SELL: Quote(SELL)}
        self.quoting = True
        # The latest book of each instrument: the exchange sends a book only when it changes.
        self.books = LatestBooks()
        # When to act next even if no event comes, on the time.monotonic() clock.
        self.next_act = float("-inf")
        # Whether the last session state said the market is open; none is known yet.
        self.market_open = False
        # How many order messages it has sent, so a caller can tell whether a step sent one.
        self.messages = 0
        self.last_side: Side = SELL  # so BUY goes first

    def in_turn(self) -> list[Quote]:
        """Both sides, starting with the one that did not send last, so that with one
        message per step neither side can keep the other waiting."""
        first = BUY if self.last_side == SELL else SELL
        return [self.quotes[first], self.quotes[SELL if first == BUY else BUY]]

    # Sending

    def ready(self, quote: Quote) -> bool:
        """Whether this side may send now: nothing in flight, and not sent too recently."""
        waited = time.monotonic() - quote.sent_at
        return quote.pending_ref is None and waited >= self.requote_seconds

    def next_ready(self) -> float:
        """Seconds until a side that is waiting out --requote-seconds may send again, or
        --requote-seconds if none is."""
        now = time.monotonic()
        waits = [
            quote.sent_at + self.requote_seconds - now
            for quote in self.quotes.values()
            if quote.pending_ref is None and quote.sent_at + self.requote_seconds > now
        ]
        return min([self.requote_seconds, *waits])

    def resting(self, quote: Quote) -> RestingOrder | None:
        """This example's order at its price, as the exchange last reported it, or None."""
        if quote.price is None:
            return None
        order = self.view.get(self.instrument, quote.side, quote.price)
        if order is None or order.strat_id != self.strat_id:
            return None
        return order

    def record(self, quote: Quote, kind: str) -> str:
        """Record a message for this side before sending it, and return its request_ref.

        Recording first means a reply is matched even if a Ctrl+C lands part-way through
        the send, and nothing else is sent for this side until that reply comes."""
        ref = new_request_ref()
        quote.pending_ref, quote.pending_kind, quote.sent_at = ref, kind, time.monotonic()
        self.messages += 1
        self.last_side = quote.side
        return ref

    def sent(self, quote: Quote) -> None:
        where = f"{SIDE_NAMES[quote.side]} {self.instrument} @ {price_text(quote.price or 0)}"
        print(f"sent   {quote.pending_kind:<6} {where}")

    async def manage(self, quote: Quote, best: int) -> None:
        """Move one side towards resting --size shares at `best`."""
        if not self.ready(quote) or quote.cancelling:
            return
        if quote.price is None:
            quote.price = best
            await send_new(
                self.session,
                strat_id=self.strat_id,
                instrument=self.instrument,
                side=quote.side,
                order_type=LIMIT,
                price=best,
                size=self.size,
                request_ref=self.record(quote, "new"),
            )
            self.sent(quote)
            return
        order = self.resting(quote)
        if order is None:
            return  # not reported resting yet: wait rather than guess
        if quote.price != best:
            # Cancel and re-enter: the new order goes in once the cancel is confirmed.
            await send_cancel(
                self.session,
                instrument=self.instrument,
                side=quote.side,
                price=quote.price,
                request_ref=self.record(quote, "cancel"),
            )
            self.sent(quote)
        elif order.remaining_size < self.size:
            # new_size is the new total remaining size, not an amount to add.
            await send_amend(
                self.session,
                instrument=self.instrument,
                side=quote.side,
                price=quote.price,
                new_size=self.size,
                request_ref=self.record(quote, "amend"),
            )
            self.sent(quote)

    async def cancel_own(self) -> bool:
        """Cancel this example's orders. Returns True once none are left or in flight."""
        done = True
        for quote in self.in_turn():
            if quote.pending_ref is not None:
                done = False
            elif quote.price is not None:
                done = False
                if self.ready(quote) and not quote.cancelling and self.resting(quote):
                    await send_cancel(
                        self.session,
                        instrument=self.instrument,
                        side=quote.side,
                        price=quote.price,
                        request_ref=self.record(quote, "cancel"),
                    )
                    self.sent(quote)
                    # One message per call: the caller applies any events that came in
                    # meanwhile, which may change what is safe to send, before the next.
                    return False
        # Done once the exchange has also reported each order gone from the book.
        return done and not any(
            order.strat_id == self.strat_id and order.key.instrument == self.instrument
            for order in self.view
        )

    # Receiving

    async def act(self) -> None:
        """Quote from the latest book held, sending at most one message. Safe to call at
        any time: with no book yet, or nothing that may be sent now, it sends nothing. The
        caller applies the events that came in meanwhile before calling it again."""
        try:
            await self.quote_from_book()
        finally:
            # Act again by the time a side may send again, whatever events come or not.
            self.next_act = time.monotonic() + self.next_ready()

    async def quote_from_book(self) -> None:
        book = self.books.get(self.instrument)
        if not self.quoting or not self.market_open or book is None:
            return  # outside a session the exchange takes no orders
        if self.instrument in self.books.stale:
            # A message that may have been a newer book was missed or unreadable, so this
            # book may be out of date. Send nothing until a newer book arrives.
            return
        # Quote --inside dollars inside the wall's best price on each side that has one.
        bid = book.bid_levels[0].price + self.inside if book.bid_levels else None
        ask = book.ask_levels[0].price - self.inside if book.ask_levels else None
        if bid is not None and ask is not None and bid >= ask:
            return  # the wall's spread is too narrow to quote inside
        targets = {BUY: bid, SELL: ask}
        sent = self.messages
        for quote in self.in_turn():
            target = targets[quote.side]
            if target is not None and self.messages == sent:
                await self.manage(quote, target)

    def pending(self, ref: str | None) -> Quote | None:
        for quote in self.quotes.values():
            if ref is not None and quote.pending_ref == ref:
                return quote
        return None

    def own(self, message: Execution | OrderCancelled, price_field: str) -> Quote | None:
        """The side's quote if this fill or cancellation is about this example's order:
        its strategy, instrument, side and price. A teammate's order is not its own."""
        quote = self.quotes.get(message.side)
        if (
            quote is None
            or quote.price is None
            or message.strat_id != self.strat_id
            or message.instrument != self.instrument
            or not message.HasField(price_field)
            or getattr(message, price_field) != quote.price
        ):
            return None
        return quote

    def gone(self, quote: Quote) -> None:
        """This side's order is no longer on the book."""
        quote.price = None
        quote.cancelling = False

    def believed_resting(self) -> str:
        """The orders this example believes it may have, for a message to the user."""
        levels = [
            f"{SIDE_NAMES[q.side]} {self.instrument} @ {price_text(q.price)}"
            for q in self.quotes.values()
            if q.price is not None
        ]
        return ", ".join(levels) or "none"

    def on_order_event(self, message: Message) -> None:
        ref = request_ref_of(message)
        quote = self.pending(ref)
        match message:
            case Accepted() if quote is not None:
                elapsed_ms = (time.monotonic() - quote.sent_at) * 1000
                print(f"accepted {quote.pending_kind} after a {elapsed_ms:.0f} ms round trip")
                if quote.pending_kind == "cancel" and quote.price is not None:
                    quote.cancelling = True  # the order_cancelled that follows clears it
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
                        self.gone(quote)
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
                own = self.own(message, "order_price")
                if own is not None and message.remaining_size == 0:
                    self.gone(own)  # filled completely: enter a new order next time
            case OrderCancelled() if message.instrument == self.instrument:
                side = SIDE_NAMES.get(message.side, "?")
                price = price_text(message.price) if message.HasField("price") else "market"
                reason = reason_code_name(message.reason_code)
                print(f"cancelled {side} {message.cancelled_size} @ {price} ({reason})")
                own = self.own(message, "price")
                if own is not None:
                    self.gone(own)
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
    if timeout <= 0:
        # No time left to wait, even if events are waiting. Yield once all the same, so
        # a Ctrl+C and the reader still get their turn.
        await asyncio.sleep(0)
        return TimeoutError()
    # asyncio.timeout rather than asyncio.wait_for: on Python 3.11, wait_for can lose a
    # Ctrl+C that arrives just as an event does, and the example would not stop.
    try:
        async with asyncio.timeout(timeout):
            return await queue.get()
    except TimeoutError as timeout_error:
        return timeout_error


def handle(quoter: Quoter, view: RestingOrders, event: Event) -> bool:
    """Apply one event to what the example knows. It sends nothing: the example acts
    only once every event already received is applied. Returns False when there is no
    point quoting on."""
    # Update the view of resting orders first, from this event and in stream order.
    view.apply(event)
    item = as_market_data(event)
    # Keeps a newer book; after a message that may have been a book was missed or could
    # not be read, LatestBooks lists the book held as stale.
    quoter.books.update(item)
    if isinstance(item, SessionState):
        quoter.market_open = item.state == MarketSessionPhase.OPEN
    elif isinstance(item, Reject):
        # The exchange refused the subscription, for example an unknown instrument.
        print(f"subscription refused: {reason_code_name(item.reason_code)}")
        return False
    elif isinstance(item, SeqGap | DecodeFailed):
        print("warning: a message from the exchange was missed or could not be read")
        if isinstance(item, DecodeFailed) and item.type in (None, "session_state"):
            # It may have said the market closed: quote again only after the next OPEN.
            quoter.market_open = False
    elif is_order_event(event):
        quoter.on_order_event(event.message)
    return True


def apply_received(quoter: Quoter, view: RestingOrders, item: object) -> str | None:
    """Apply one item from `pump`. Returns why quoting must stop, or None to go on."""
    if item is None:
        return "closed"
    if isinstance(item, Exception):
        raise item  # the connection dropped
    if not handle(quoter, view, item):
        return "refused"
    if view.incomplete:
        return "unreliable"
    return None


def drain(queue: asyncio.Queue) -> list:
    """Take every item already queued, in order, without waiting."""
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


def apply_waiting(quoter: Quoter, view: RestingOrders, queue: asyncio.Queue) -> str | None:
    """Apply every item `pump` has already queued, in the order received. Returns why
    quoting must stop, or None to go on. Run before every decision to send: a queued
    event may show that messages were missed or that the book held is out of date."""
    while not queue.empty():
        why = apply_received(quoter, view, queue.get_nowait())
        if why is not None:
            return why
    return None


async def quote_until(
    quoter: Quoter, view: RestingOrders, queue: asyncio.Queue, seconds: float
) -> str:
    """Quote until `seconds` have passed. Returns why it stopped: "time", "refused",
    "unreliable" (events were missed) or "closed" (the exchange closed the connection)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        # Apply every event already received before deciding anything, even once time is
        # up: an `accepted` left unapplied would leave an order unknown to the cleanup.
        why = apply_waiting(quoter, view, queue)
        if why is not None:
            return why
        if loop.time() >= deadline:
            return "time"  # checked before acting: a send may complete without waiting
        sent = quoter.messages
        try:
            # A send can stall (for example on a connection that stopped taking data), so
            # it too is bounded by the time left. Its request_ref is already recorded.
            async with asyncio.timeout(max(deadline - loop.time(), 0)):
                # Act on the latest book held after every event, and on the timer even if
                # no event has come: the exchange sends a book only when it changes.
                await quoter.act()
        except TimeoutError:
            return "time"
        if quoter.messages != sent:
            continue  # apply what came in during that send before sending another
        # Wait for the next event, but only until it is time to act again.
        wake = min(deadline - loop.time(), quoter.next_act - time.monotonic())
        item = await next_event(queue, wake)
        if not isinstance(item, TimeoutError):
            why = apply_received(quoter, view, item)
            if why is not None:
                return why


async def cancel_own_orders(
    quoter: Quoter, view: RestingOrders, queue: asyncio.Queue, deadline: float
) -> str:
    """Cancel this example's orders. Returns "done" once the exchange has confirmed each
    is gone, "unreliable" if events were missed (it then sends nothing more), or
    "unconfirmed" if neither happens by `deadline`, a time on the event loop's clock.
    Safe to call again after an interruption: it resends nothing already in flight."""
    quoter.quoting = False
    loop = asyncio.get_running_loop()
    while True:
        # Apply every event already received first, so a missed message is seen before
        # anything is sent. After missed events, ownership is no longer known.
        for item in drain(queue):
            if item is None:
                return "unconfirmed"
            if isinstance(item, Exception):
                raise item
            handle(quoter, view, item)
        if view.incomplete:
            return "unreliable"
        sent = quoter.messages
        try:
            # Bounded by the time left, so a stalled send cannot outlast --drain-seconds.
            async with asyncio.timeout(max(deadline - loop.time(), 0)):
                done = await quoter.cancel_own()
        except TimeoutError:
            return "unconfirmed"
        if done:
            return "done"
        if loop.time() >= deadline:
            return "unconfirmed"
        if quoter.messages != sent:
            continue  # apply what came in during that send before sending another
        # Wake at least every requote interval, to retry a rejected cancel.
        item = await next_event(queue, min(deadline - loop.time(), quoter.requote_seconds))
        if item is None:
            return "unconfirmed"
        if isinstance(item, TimeoutError):
            continue
        if isinstance(item, Exception):
            raise item
        handle(quoter, view, item)


def unreliable(quoter: Quoter) -> int:
    print(
        "messages were missed, so this example can no longer tell which orders are its "
        "own and sends nothing more. Check your team's orders; it believes it may have: "
        f"{quoter.believed_resting()}"
    )
    return 1


# The longest wait for the connection to close at the end. A local choice, not a value
# the exchange sets. If the close does not finish, the connection is dropped anyway when
# the example exits.
CLOSE_SECONDS = 5.0


@contextlib.asynccontextmanager
async def closing(session: Session) -> AsyncIterator[Session]:
    """Like `async with session:`, but waits at most CLOSE_SECONDS for the close, so a
    connection that has stopped taking data cannot keep the example from exiting."""
    try:
        yield session
    finally:
        try:
            async with asyncio.timeout(CLOSE_SECONDS):
                await session.close()
        except TimeoutError:
            print("the connection did not close in time; it is dropped as the example exits")


async def run(url: str, args: argparse.Namespace) -> int:
    # The token comes from the QTE_TOKEN environment variable.
    session = await open_session(url)
    # Each Ctrl+C from here on cancels this task. The first cancellation is absorbed,
    # whether it lands while quoting or while cancelling, so the run still cancels its
    # orders; a second is let through and stops the run, and a third also stops it
    # waiting for the connection to close. The event loop handles Ctrl+C itself where it
    # can (not on Windows), so a press never interrupts asyncio's own code part-way.
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGINT, task.cancel)
    try:
        return await quote_and_clean_up(session, args, task)
    finally:
        with contextlib.suppress(NotImplementedError):
            loop.remove_signal_handler(signal.SIGINT)


async def quote_and_clean_up(session: Session, args: argparse.Namespace, task: asyncio.Task) -> int:
    loop = asyncio.get_running_loop()
    async with closing(session):
        print(f"connected: team {session.info.team}, unscored session: {session.info.unscored}")
        try:
            async with asyncio.timeout(args.seconds):
                await subscribe(session, [args.instrument])
        except TimeoutError:
            print("could not subscribe within --seconds; no orders were sent")
            return 1

        view = RestingOrders()
        quoter = Quoter(session, view, args)
        queue: asyncio.Queue = asyncio.Queue()
        reader = asyncio.create_task(pump(session, queue))
        interrupted = False

        def absorb_first_interrupt() -> bool:
            nonlocal interrupted
            if interrupted:
                return False
            interrupted = True
            # This cancellation is handled. If another is still pending, Ctrl+C was
            # pressed twice before the first reached this point: stop at once.
            return task.uncancel() == 0

        try:
            try:
                why = await quote_until(quoter, view, queue, args.seconds)
            except asyncio.CancelledError:
                if not absorb_first_interrupt():
                    raise
                why = "interrupted"

            if why == "closed":
                print("the exchange closed the connection")
                print(f"orders this example may still have: {quoter.believed_resting()}")
                return 1
            if why == "unreliable":
                return unreliable(quoter)

            reason = {"time": "time is up", "refused": "nothing to quote"}.get(why, why)
            print(f"{reason}: cancelling this example's orders")
            deadline = loop.time() + args.drain_seconds
            while True:
                try:
                    outcome = await cancel_own_orders(quoter, view, queue, deadline)
                    break
                except asyncio.CancelledError:
                    if not absorb_first_interrupt():
                        raise
                    print(
                        "interrupted: still cancelling this example's orders; "
                        "press Ctrl+C again to stop at once"
                    )
            if outcome == "done":
                print("all of this example's orders are cancelled")
                return 0
            if outcome == "unreliable":
                return unreliable(quoter)
            print(
                "WARNING: not confirmed cancelled, so these may still rest: "
                f"{quoter.believed_resting()}; check and cancel them yourself"
            )
            return 1
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
            "the connection dropped, and this example does not reconnect. Your orders may "
            "still rest: reconnect and cancel them"
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        # A second Ctrl+C, before the example's orders were confirmed cancelled.
        return fail("interrupted: this example's orders may still rest; check and cancel them")


if __name__ == "__main__":
    sys.exit(main())
