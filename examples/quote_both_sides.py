"""Rest a quote on both sides of one instrument and keep it there.

Set QTE_URL and QTE_TOKEN first, in ./.env or the environment (see docs/quickstart.md),
then:

    python examples/quote_both_sides.py --instrument AAPL --strat-id <your strategy> \\
        --size 100 --seconds 30

--size is the shares to quote on each side. A Market Making desk must display at least one
round lot of its name at its best bid and best offer: read the round lot from the console's
obligations panel and pass at least that (100 above is an example). Without --size the
example quotes 1 share and warns at start; a later release will require --size.

The example quotes one limit order of --size shares on each side, --inside dollars inside
the best price the wall shows on that side, and manages the two orders until --seconds
have passed:

- when the wall's best price moves, it cancels its order and enters a new one at the new
  price (cancel and re-enter, rather than amending the price);
- when a fill leaves an order smaller than --size, it amends the order's size back up;
- when an order fills completely or is cancelled, it enters a new one.

It then cancels its own orders and waits at most --drain-seconds for the exchange to
confirm. Pressing Ctrl+C does the same, even while those cancels are being confirmed;
press it twice to stop at once. An order is its own only once the exchange has answered
its new with `accepted`: it never cancels an order of its strategy it cannot tell is its
own, nor sends a cancel where its view shows another strategy's order. If messages from
the exchange are missed, it stops quoting, still sends one cancel to each of its own
orders, and exits 1, since what filled may not all be known. It exits 0 only when every
message it sent was answered, each of its own orders was seen to end, and no order of its
strategy rests on the instrument; otherwise its last lines name each order, message and
fill it is unsure of.

Run it on prices your team is not otherwise trading. A cancel names a price level, not
an order, and is applied only after the order delay, so it removes whichever of your
team's orders rests at that level by then: if this example's order fills while its
cancel is delayed and a teammate's strategy enters an order at the same price, the
cancel removes the teammate's order. When a cancel's reply shows it took an order that
was not this run's, the example names it and exits 1.

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
MESSAGE_BUDGET_EXCEEDED), the reject is printed and the example tries again later; a
cancel refused for a message budget goes again about a minute later.

When it acts. The exchange publishes a book only when it changes, so in a quiet market
the next book may be a long time coming. The example keeps the latest book it has
received with `qte_sdk.books.LatestBooks` and decides what to send from that book, not
only when a new book arrives: also after every other event and on its own timer, at
least once per --requote-seconds. So a retry after a reject, or a new order after a fill
or a cancel, does not wait for the market to move. Before it sends anything, it applies
every event it has already received, so a sign that messages were missed is seen first.
If a message that may have been a newer book is missed or cannot be read, the book held
may be out of date, so the example sends nothing from it until a newer book arrives. It
also quotes only while the latest session state says the market is open, and only from
a book published since that session opened, never one kept from an earlier session. It
sends one message at a time, and applies what has come in since before sending the next.

On a drop. The example connects with `qte_sdk.reconnect.ReconnectingSession`, so if the
connection drops, or the exchange restarts, it reconnects by itself, waiting longer between
attempts (at most 30 seconds). While it is down it sends nothing, keeps the orders it
believes it has, and prints them. Each new session subscribes again and resumes the
team's order reports: the exchange either replays every report missed, or sends a
snapshot of the orders resting now. A message in flight at the drop is never sent again,
and stays unanswered until its own reply comes: neither a replay nor a snapshot settles
it. A late `accepted` for a new makes that order its own, so it is cancelled at the end.
Once the resume is complete, the example checks its own orders against its view of
resting orders (`qte_sdk.resting.RestingOrders`): one the view no longer shows ended while
it was down, filled or cancelled, so it says to check your fills. Market data sent while
it was down is not replayed, so it quotes again only from a book received since it
reconnected, once the session state says the market is open. If the resume is cut short
or refused, or messages were missed meanwhile, or the view shows an order of its strategy
it cannot tell is its own (as when a new was in flight at the drop), it stops quoting,
cancels its own orders as above, and exits 1. --seconds counts the time down too; if the
connection is down when it ends, the example waits at most --drain-seconds for it to come
back to cancel its orders, and otherwise names them for you to check. Only the first
connection is not retried: if it fails, the example says why and stops, before sending
anything. If a later connection ends for good (the exchange refused the token, say), it
cannot clean up: it names everything it is unsure of and exits 1.

What the resume cannot show. The replay or the snapshot covers only what the exchange had
applied when it answered. A new still waiting out its order delay at the drop can be
applied just after that, so the resume does not show it. The example sends no second new
for that side while the first is unanswered, and names the order by the request_ref of
its new if no reply comes, so you can find it in your reports and cancel it.
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
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, InvalidHandshake

import qte_sdk.logs
from qte_sdk.books import LatestBooks
from qte_sdk.connection import (
    Connected,
    Disconnected,
    Event,
    Received,
    ResumeComplete,
    SessionRejected,
    describe_close,
)
from qte_sdk.contract.v1.common_pb2 import (
    BUY,
    LIMIT,
    RESTING,
    SELL,
    STALE,
    TEAM,
    MarketSessionPhase,
    OrderLifecycleState,
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
from qte_sdk.errors import order_reject_step
from qte_sdk.market_data import DecodeFailed, SeqGap, SessionState, as_market_data
from qte_sdk.orders import (
    is_order_event,
    new_request_ref,
    reason_code_name,
    request_ref_of,
    send_amend,
    send_cancel,
    send_new,
)
from qte_sdk.reconnect import NotConnected, ReconnectingSession, Retrying, is_retryable
from qte_sdk.resting import RestingOrder, RestingOrders
from qte_sdk.session import (
    MissingToken,
    MissingURL,
    SessionNotAcknowledged,
    resolve_url,
)
from qte_sdk.units import to_decimal, to_micros

SIDE_NAMES = {BUY: "BUY", SELL: "SELL"}

# Cancel rejects that wait out a message budget, and how long a final cancel waits after
# one before it is sent again: about a minute, a local choice.
BUDGET_REJECTS = frozenset({ReasonCodes.MESSAGE_BUDGET_EXCEEDED, ReasonCodes.BURST_CAP_EXCEEDED})
BUDGET_WAIT_SECONDS = 60.0


SIZE_WARNING = (
    "warning: --size not set, so this example quotes 1 share per side. The market maker "
    "minimum is one round lot per side: set --size to your instrument's round lot, shown in "
    "the console's obligations panel. A later release will require --size."
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rest a quote on both sides and manage it.")
    parser.color = False  # Python 3.14 colours argparse's output; it is read as plain text.
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
    parser.add_argument(
        "--size",
        type=int,
        default=None,
        help=(
            "shares per side; a Market Making desk passes at least its name's round lot, "
            "shown in the console's obligations panel (default 1, with a warning; a later "
            "release will require it)"
        ),
    )
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
    if args.size is None:
        args.size = 1
        print(SIZE_WARNING, file=sys.stderr)
    return args


def price_text(price: int) -> str:
    return str(to_decimal(price))


@dataclass
class Sent:
    """A message this example sent that its own reply has not yet answered."""

    kind: str  # "new", "cancel" or "amend"
    side: Side
    price: int
    sent_at: float
    # Whether the connection dropped after it was sent: a reply may still come, late.
    dropped: bool = False


@dataclass
class Quote:
    """What this example believes about its order on one side."""

    side: Side
    # The price level its order is at, or is being sent to; None when it has no order.
    price: int | None = None
    # The request_ref of the last message sent for this side, its kind and when. Nothing
    # else is sent for the side while that message is unanswered.
    pending_ref: str | None = None
    pending_kind: str = ""
    sent_at: float = float("-inf")
    # True from an accepted cancel until the exchange reports the order gone.
    cancelling: bool = False
    # The request_ref of the new that placed its order, so a message can name the order.
    order_ref: str | None = None
    # True from the `accepted` of its new, and from nothing else: only then is the order
    # this example's own, and only then does it cancel it or follow reports about it.
    own: bool = False
    # Whether fills of the order may have gone unreported: messages were missed, or a
    # snapshot answered a resume.
    fills_unknown: bool = False
    # Its remaining size at a drop, as the view showed it, to compare with a snapshot.
    remaining: int | None = None
    # One more cancel may go while one is unanswered: a complete resume still showed the
    # order resting.
    again: bool = False
    # No cancel is sent again before this time, on the time.monotonic() clock.
    retry_after: float = float("-inf")
    # The reason the last cancel of this order was rejected, to name at the end.
    last_reject: str | None = None
    # Whether a final cancel was held back because the view shows another strategy's
    # order at the level.
    skipped: bool = False


class Quoter:
    def __init__(
        self, session: ReconnectingSession, view: RestingOrders, args: argparse.Namespace
    ) -> None:
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
        self.open_time = 0  # when the session in progress opened, from its session state
        # How many order messages it has sent, so a caller can tell whether a step sent one.
        self.messages = 0
        self.last_side: Side = SELL  # so BUY goes first
        # Whether a session is up and its resume has ended; nothing is sent until it is.
        self.up = True
        self.sessions = 0  # how many sessions have been connected
        # Whether the session's resume is under way, and whether its resume_ack has come.
        self.resuming = False
        self.resume_answered = False
        # The reject reasons whose next step this run has printed: once each, not every retry.
        self.steps_shown: set[str] = set()
        # The levels where an order of its strategy rested when the first resume completed,
        # before this run sent anything: those orders are an earlier run's. None until then.
        self.earlier: set[tuple[int, int]] | None = None
        # Every message sent and not yet answered by its own reply, by request_ref. Nothing
        # else settles one: not a resume, a replay, a snapshot or time.
        self.open: dict[str, Sent] = {}
        # Every cancel sent, by request_ref, so a late order_cancelled is still matched.
        self.cancels: dict[str, tuple[int, int]] = {}
        self.last_ref: str | None = None  # the message being sent, if a send fails
        # Whether messages from the exchange were missed or unreadable, or a resume did not
        # put the view right: what filled may then not all be known.
        self.missed = False
        # Why quoting stopped early, for the cleanup's first line.
        self.stop_reason = "messages were missed"
        # Orders of its own that ended with their fills not all reported, for the end.
        self.unreported: list[str] = []
        # Whether an order of its own ended other than by this example's cancel.
        self.ended_otherwise = False
        # What went wrong that the last lines must name.
        self.warnings: list[str] = []

    def where(self, side: int, price: int) -> str:
        return f"{SIDE_NAMES.get(side, '?')} {self.instrument} @ {price_text(price)}"

    def on_session_state(self, state: SessionState) -> None:
        self.market_open = state.state == MarketSessionPhase.OPEN
        # A book published before this session opened is from an earlier session.
        self.open_time = state.open_time

    # The connection

    def on_retrying(self, retrying: Retrying) -> None:
        if self.sessions == 0 and retrying.error is not None:
            raise retrying.error  # the first connection failed: stop, before any order
        print(f"reconnecting in {retrying.delay:.1f} s (attempt {retrying.attempt})")

    def on_connected(self, connected: Connected) -> None:
        self.sessions += 1
        self.up = False  # until its resume has ended
        self.resuming = connected.resume is not None
        self.resume_answered = False
        info = connected.info
        if connected.reconnected:
            print(f"reconnected: team {info.team}; sending nothing until the resume is complete")
        else:
            print(f"connected: team {info.team}, unscored session: {info.unscored}")

    def on_disconnected(self, disconnected: Disconnected) -> None:
        self.up = False
        self.resuming = False
        # The market may have closed meanwhile: quote again only after the next OPEN.
        self.market_open = False
        for sent in self.open.values():
            sent.dropped = True
        for quote in self.quotes.values():
            order = self.resting(quote) if quote.own else None
            quote.remaining = order.remaining_size if order is not None else None
        error = disconnected.error
        why = error or "the exchange closed it"
        if error is not None and not is_retryable(error):
            then = "it will not reconnect, so it cannot cancel them"
        else:
            then = "sending nothing until it is back"
        print(
            f"the connection dropped ({why}); {then}. Orders this example believes it has: "
            f"{self.believed_resting(refs=True)}"
        )

    def on_missed(self, ends_resume: bool) -> None:
        """A message was missed or unreadable. During a resume, a `SeqGap`, or a
        `DecodeFailed` whose type is unknown, after the resume_ack ends the resume with no
        `ResumeComplete` (the session stops waiting for it): take it as a resume that did
        not recover the reports, so the session is up and a cancel can still be sent.
        Before the resume_ack, the session's resume goes on: wait for its
        `ResumeComplete`, sending nothing. While up, the view marked incomplete says it."""
        if not self.resuming:
            return
        self.lost()
        if not ends_resume:
            return
        if not self.resume_answered:
            print(
                "a message was missed or unreadable before the resume was answered: "
                "still waiting for the resume to complete"
            )
            return
        print("the resume was cut short by a message missed or unreadable")
        self.view.mark_incomplete()
        self.resumed(replayed=False)

    def lost(self) -> None:
        """Messages were missed: fills of any order may not all be reported, and quoting
        stops. Each order of its own is still cancelled."""
        self.missed = True
        for quote in self.quotes.values():
            if quote.price is not None:
                quote.fills_unknown = True

    def resume_refused(self) -> bool:
        """A session connected with no resume. Returns False when the view cannot be put
        right: a reconnect the exchange did not resume."""
        if not self.view.incomplete:
            return self.resumed(replayed=True)  # nothing to recover
        print("the exchange did not resume the session, so reports missed while down are lost")
        self.lost()
        return self.resumed(replayed=False)

    def resumed(self, replayed: bool) -> bool:
        """A session's resume has ended: complete, or cut short. Take this example's
        orders from the view where it is complete, and send again. Returns False when
        quoting must stop: messages were missed, or the view shows an order of its strategy
        that it cannot tell is its own."""
        self.up = True
        self.resuming = False
        if self.view.incomplete:
            self.lost()  # the view was not put right
        if self.earlier is None:
            # The first resume to end: nothing is sent before it, so every order of its
            # strategy in the view now is an earlier run's.
            self.earlier = self.strategy_levels()
            return not self.missed
        complete = not self.view.incomplete
        for quote in self.quotes.values():
            if not quote.own or quote.price is None:
                continue  # a new still unanswered stays so: only its own reply settles it
            order = self.resting(quote)
            if not replayed and (order is None or order.remaining_size != quote.remaining):
                # A snapshot reports no fills: any while disconnected are not known.
                quote.fills_unknown = True
            if not complete:
                continue
            if order is None:
                print(
                    f"{self.where(quote.side, quote.price)} (request_ref {quote.order_ref}) "
                    "ended while disconnected, filled or cancelled"
                )
                quote.fills_unknown = True
                self.end(quote, by_this_run=False)
            elif self.cancel_open(quote):
                quote.again = True  # still resting, its cancel unanswered: one more may go
        unknown = self.unattributed() if complete else []
        for order in unknown:
            # Not this run's own order, nor an earlier run's: perhaps the order of a new
            # whose accepted the resume did not show, or one of its own moved while it was
            # down. Whose it is cannot be told, so it is never cancelled.
            print(
                f"an order of this strategy rests at {self.where(order.key.side, order.key.price)}"
                ", which this example cannot tell is its own: it does not cancel it"
            )
            self.stop_reason = "an order of this strategy cannot be told apart"
        print(f"resume ended: the orders this example may have now: {self.believed_resting()}")
        return not (self.missed or unknown)

    def strategy_levels(self) -> set[tuple[int, int]]:
        """The levels where the view shows an order of this strategy on the instrument."""
        return {(order.key.side, order.key.price) for order in self.strategy_orders()}

    def strategy_orders(self) -> list[RestingOrder]:
        return [
            order
            for order in self.view
            if order.strat_id == self.strat_id and order.key.instrument == self.instrument
        ]

    def unattributed(self) -> list[RestingOrder]:
        """Orders of this strategy in the view that are not its own, nor an earlier run's."""
        own = {(q.side, q.price) for q in self.quotes.values() if q.own}
        return [
            order
            for order in self.strategy_orders()
            if (order.key.side, order.key.price) not in own | (self.earlier or set())
        ]

    def believes_any(self) -> bool:
        """Whether this example may have an order resting or a message unanswered."""
        return bool(self.open) or any(q.price is not None for q in self.quotes.values())

    def in_turn(self) -> list[Quote]:
        """Both sides, starting with the one that did not send last, so that with one
        message per step neither side can keep the other waiting."""
        first = BUY if self.last_side == SELL else SELL
        return [self.quotes[first], self.quotes[SELL if first == BUY else BUY]]

    # Sending

    def ready(self, quote: Quote) -> bool:
        """Whether this side may send now: nothing unanswered, and not sent too recently."""
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
        """An order of its strategy at the side's price, as the exchange last reported it."""
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
        assert quote.price is not None
        self.open[ref] = Sent(kind, quote.side, quote.price, quote.sent_at)
        if kind == "cancel":
            self.cancels[ref] = (quote.side, quote.price)
        self.last_ref = ref
        self.messages += 1
        self.last_side = quote.side
        return ref

    def not_sent(self, error: Exception) -> None:
        """The send raised `NotConnected`: the message never left, so it is not unanswered."""
        ref = self.last_ref
        sent = self.open.pop(ref, None) if ref is not None else None
        if sent is None:
            print(f"not sent, the connection is down: {error}")
            return
        print(
            f"not sent, the connection is down: {error}; the {sent.kind} to "
            f"{self.where(sent.side, sent.price)} (request_ref {ref}) was never sent"
        )
        self.cancels.pop(ref or "", None)
        quote = self.quotes[sent.side]
        if quote.pending_ref == ref:
            quote.pending_ref = None
        if sent.kind == "new" and quote.order_ref == ref:
            self.gone(quote)

    def sent(self, quote: Quote) -> None:
        print(f"sent   {quote.pending_kind:<6} {self.where(quote.side, quote.price or 0)}")

    async def manage(self, quote: Quote, best: int) -> None:
        """Move one side towards resting --size shares at `best`."""
        if not self.ready(quote) or quote.cancelling:
            return
        if quote.price is None:
            quote.price = best
            quote.order_ref = self.record(quote, "new")
            await send_new(
                self.session,
                strat_id=self.strat_id,
                instrument=self.instrument,
                side=quote.side,
                order_type=LIMIT,
                price=best,
                size=self.size,
                request_ref=quote.order_ref,
            )
            self.sent(quote)
            return
        order = self.resting(quote)
        if not quote.own or order is None:
            return  # not accepted, or not reported resting yet: wait rather than guess
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

    def cancel_open(self, quote: Quote) -> bool:
        """Whether a cancel sent to the side's level is still unanswered."""
        return any(
            sent.kind == "cancel" and sent.side == quote.side and sent.price == quote.price
            for sent in self.open.values()
        )

    async def cancel_own(self) -> None:
        """Send at most one cancel, to the level of an order of its own that may still
        rest. Only one cancel per level is unanswered at a time, unless a complete resume
        still showed the order resting; a rejected one goes again, paced. No cancel goes
        where the view shows another strategy's order: with one order per level for the
        team, this example's is not there, and the cancel would take the other."""
        now = time.monotonic()
        for quote in self.in_turn():
            if not quote.own or quote.price is None:
                continue  # never cancel an order it cannot tell is its own
            if self.cancel_open(quote) and not quote.again:
                continue
            if now < quote.retry_after or now - quote.sent_at < self.requote_seconds:
                continue
            order = self.view.get(self.instrument, quote.side, quote.price)
            if order is not None and order.strat_id != self.strat_id:
                if not quote.skipped:
                    print(
                        f"not cancelling at {self.where(quote.side, quote.price)}: an order of "
                        f"strategy {order.strat_id} rests there"
                    )
                quote.skipped = True
                continue
            quote.skipped = quote.again = False
            await send_cancel(
                self.session,
                instrument=self.instrument,
                side=quote.side,
                price=quote.price,
                request_ref=self.record(quote, "cancel"),
            )
            self.sent(quote)
            # One message per call: the caller applies any events that came in meanwhile,
            # which may change what is safe to send, before the next.
            return

    def settled(self) -> bool:
        """Whether every message is answered and each order of its own has ended, or has
        a cancel held back by another strategy's order at its level."""
        return not self.open and all(
            not q.own or q.price is None or q.skipped for q in self.quotes.values()
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
        if not self.quoting or not self.up or not self.market_open or book is None:
            # Nothing while the connection is down or resuming. Outside a session the
            # exchange takes no orders.
            return
        if book.grid_time < self.open_time:
            return  # kept from an earlier session: wait for this session's first book
        if self.instrument in self.books.stale:
            # A message that may have been a newer book was missed or unreadable, or the
            # connection dropped, so this book may be out of date. Send nothing until a
            # newer book arrives, on the new connection after a drop.
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

    def ours(self, message: OrderState | Execution | OrderCancelled) -> Quote | None:
        """The side's quote if this report is about its own order: its strategy,
        instrument, side and current level, once accepted. An amend's `order_state` names
        the level the order left in `old_price`. A fill is its own only with origin TEAM."""
        if isinstance(message, Execution):
            if message.origin != TEAM or not message.HasField("order_price"):
                return None
            price = message.order_price
        elif isinstance(message, OrderState):
            price = message.old_price if message.HasField("old_price") else message.price
        elif message.HasField("price"):
            price = message.price
        else:
            return None  # a market order's cancel names no level
        quote = self.quotes.get(message.side)
        if (
            quote is None
            or not quote.own
            or quote.price != price
            or message.strat_id != self.strat_id
            or message.instrument != self.instrument
        ):
            return None
        return quote

    def end(self, quote: Quote, by_this_run: bool) -> None:
        """This side's own order is no longer on the book."""
        if not by_this_run:
            self.ended_otherwise = True
        if quote.fills_unknown and quote.price is not None:
            self.unreported.append(
                f"{self.where(quote.side, quote.price)} (request_ref {quote.order_ref})"
            )
        self.gone(quote)

    def gone(self, quote: Quote) -> None:
        """This side has no order now: it may enter a new one."""
        quote.price = None
        quote.cancelling = quote.own = quote.fills_unknown = quote.again = False
        quote.skipped = False
        quote.order_ref = quote.remaining = quote.last_reject = None
        quote.retry_after = float("-inf")

    def believed_resting(self, refs: bool = False) -> str:
        """The orders this example believes it may have, for a message to the user. With
        `refs`, each names the request_ref of its new, and the kind of a message in flight."""
        levels = []
        for q in self.quotes.values():
            if q.price is None:
                continue
            level = self.where(q.side, q.price)
            if refs:
                flight = f", {q.pending_kind} in flight" if q.pending_ref in self.open else ""
                level += f" (request_ref {q.order_ref}{flight})"
            levels.append(level)
        return ", ".join(levels) or "none"

    def outstanding(self) -> list[str]:
        """What this example cannot say is settled, one line each, for its last lines."""
        lines = []
        for ref, sent in self.open.items():
            where = self.where(sent.side, sent.price)
            line = f"no reply came for the {sent.kind} sent to {where} (request_ref {ref})"
            if sent.kind == "new":
                line += ": the order may rest there"
            elif sent.kind == "cancel":
                line += (
                    ": applied late, a cancel takes whatever order of your team rests at its "
                    "level by then"
                )
            lines.append(line)
        for q in self.quotes.values():
            if not q.own or q.price is None:
                continue
            where = self.where(q.side, q.price)
            line = f"this example's order {where} (request_ref {q.order_ref}) may still rest"
            if q.skipped:
                line = (
                    f"no cancel was sent to {where}, since the view shows another strategy's "
                    f"order there: this run's order (request_ref {q.order_ref}) may still rest "
                    "there"
                )
            if q.last_reject is not None:
                line += f"; its last cancel was rejected ({q.last_reject})"
            if q.fills_unknown:
                line += "; its fills may not all be reported"
            lines.append(line)
        for order in self.unreported:
            lines.append(
                f"{order} ended with its fills not all reported: check your fills and "
                "position (send_account_query)"
            )
        own = {(q.side, q.price) for q in self.quotes.values() if q.own}
        for order in self.strategy_orders():
            if (order.key.side, order.key.price) not in own:
                lines.append(
                    f"an order of this strategy rests at "
                    f"{self.where(order.key.side, order.key.price)} "
                    f"({order.remaining_size} resting): not cancelled: whose it is cannot be told"
                )
        lines.extend(self.warnings)
        return lines

    def finish(self) -> int:
        """Print the last lines and return the exit status: 0 only if nothing is unsure."""
        lines = self.outstanding()
        if self.missed:
            lines.append(
                "messages were missed, so this example may not know every fill or order: "
                "check your fills and position (send_account_query)"
            )
        elif self.messages and self.view.incomplete:
            lines.append(
                "the view of resting orders is not complete, so an order may rest unseen: "
                "check with order_snapshot or the console"
            )
        if not lines:
            if self.ended_otherwise:
                print("all of this example's orders ended (filled or cancelled)")
            else:
                print("all of this example's orders are cancelled")
            return 0
        for line in lines:
            print(f"WARNING: {line}")
        print("check and cancel them yourself")
        return 1

    def on_order_event(self, message: Message) -> None:
        ref = request_ref_of(message)
        sent = self.open.get(ref) if ref is not None else None
        match message:
            case Accepted() if sent is not None and ref is not None:
                self.settle(ref)
                quote = self.quotes[sent.side]
                if sent.dropped:
                    print(f"accepted {sent.kind} (answered after a reconnect)")
                else:
                    elapsed_ms = (time.monotonic() - sent.sent_at) * 1000
                    print(f"accepted {sent.kind} after a {elapsed_ms:.0f} ms round trip")
                if sent.kind == "new" and quote.order_ref == ref:
                    quote.own = True  # applied, so the team's one order at its level
                elif sent.kind == "cancel" and quote.own and quote.price == sent.price:
                    quote.cancelling = True  # the order_cancelled that follows clears it
            case Reject():
                detail = f" ({message.reason_detail})" if message.HasField("reason_detail") else ""
                reason = reason_code_name(message.reason_code)
                kind = sent.kind if sent is not None else "message"
                print(f"REJECTED {kind}: {reason}{detail}")
                step = order_reject_step(reason)
                if step is not None and reason not in self.steps_shown:
                    self.steps_shown.add(reason)
                    print(f"next step for {reason}: {step}")
                if sent is not None and ref is not None:
                    self.settle(ref)
                    quote = self.quotes[sent.side]
                    if sent.kind == "new" and quote.order_ref == ref:
                        self.gone(quote)
                    elif sent.kind == "cancel" and quote.own and quote.price == sent.price:
                        self.on_cancel_reject(quote, message)
            case OrderState() if message.instrument == self.instrument:
                side = SIDE_NAMES.get(message.side, "?")
                if message.state in (RESTING, STALE):
                    status = f"resting {side} {message.remaining_size}"
                else:  # an amend ended the order: FILLED or CANCELLED
                    status = f"{OrderLifecycleState.Name(message.state)} {side}"
                print(f"{status} @ {price_text(message.price)} (strategy {message.strat_id})")
                quote = self.ours(message)
                if quote is not None:
                    if message.price != quote.price:
                        # Moved by an amend from elsewhere: follow it, so the cancel names
                        # the level where it rests now.
                        print(f"moved by an amend to {price_text(message.price)}")
                        quote.price = message.price
                    if message.state not in (RESTING, STALE):
                        self.end(quote, by_this_run=False)
            case Execution() if message.instrument == self.instrument:
                side = SIDE_NAMES.get(message.side, "?")
                print(
                    f"FILL   {side} {message.fill_size} @ {price_text(message.fill_price)}, "
                    f"{message.remaining_size} left, fee {to_decimal(message.fee)}"
                )
                quote = self.ours(message)
                if quote is not None and message.remaining_size == 0:
                    self.end(quote, by_this_run=False)  # filled: enter a new order next time
            case OrderCancelled() if message.instrument == self.instrument:
                side = SIDE_NAMES.get(message.side, "?")
                price = price_text(message.price) if message.HasField("price") else "market"
                reason = reason_code_name(message.reason_code)
                print(f"cancelled {side} {message.cancelled_size} @ {price} ({reason})")
                self.on_cancelled(message, ref)
            case RiskNotice():
                print(f"risk notice: {RiskNoticeKind.Name(message.kind)}")

    def settle(self, ref: str) -> None:
        """A message's own reply came: it is answered."""
        sent = self.open.pop(ref, None)
        if sent is not None and self.quotes[sent.side].pending_ref == ref:
            self.quotes[sent.side].pending_ref = None

    def on_cancel_reject(self, quote: Quote, message: Reject) -> None:
        reason = reason_code_name(message.reason_code)
        assert quote.price is not None
        if message.reason_code == ReasonCodes.NO_ORDER_AT_LEVEL:
            # Nothing rests there, and no report said the order ended: its end went
            # unreported, so what filled is not known.
            print(f"nothing rests at {self.where(quote.side, quote.price)} ({reason})")
            quote.fills_unknown = True
            self.end(quote, by_this_run=False)
            return
        quote.last_reject = reason
        if message.reason_code in BUDGET_REJECTS:
            print(f"cancel rejected ({reason}); sending it again in {BUDGET_WAIT_SECONDS:g} s")
            quote.retry_after = time.monotonic() + BUDGET_WAIT_SECONDS

    def on_cancelled(self, message: OrderCancelled, ref: str | None) -> None:
        if ref is not None:
            self.settle(ref)  # a cancel's own reply, if its accepted was not seen
        by_this_run = ref is not None and ref in self.cancels
        own = self.ours(message)
        if own is not None:
            self.end(own, by_this_run)
            return
        if not by_this_run or not message.HasField("price"):
            return
        level = self.where(message.side, message.price)
        quote = self.quotes.get(message.side)
        if quote is not None and quote.own and quote.price == message.price:
            # This example's cancel took another strategy's order at its order's level.
            # The team has one order per level, so this one is not there, and where it
            # rests is not known.
            print(f"the cancel took another strategy's order at {level}, not this one")
            self.warnings.append(
                f"a cancel at {level} took an order of your team there that was not this "
                f"example's, so this example's order (request_ref {quote.order_ref}) is not "
                "there and may still rest elsewhere: check with order_snapshot or the console"
            )
            quote.fills_unknown = True
            self.end(quote, by_this_run=False)
            return
        # The cancel was applied after this example's order at the level had ended or
        # moved, so it took another order of the team there.
        print(f"the cancel cancelled an order at {level} that was not this run's")
        self.warnings.append(
            f"a cancel at {level} cancelled an order that was not this run's: check your "
            "orders, and place that one again if it should rest"
        )


async def pump(session: ReconnectingSession, queue: asyncio.Queue) -> None:
    """Read every event into `queue`, then None when the session is closed, or the exception
    if it fails for good. Only this task reads the session. A separate reader lets the main
    loop wait for the next event with a timeout without disturbing the stream."""
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
        quoter.on_session_state(item)
    elif isinstance(item, Reject):
        # The exchange refused the subscription, for example an unknown instrument.
        print(f"subscription refused: {reason_code_name(item.reason_code)}")
        return False
    elif isinstance(item, Disconnected):
        quoter.on_disconnected(item)
    elif isinstance(item, SeqGap | DecodeFailed):
        print("warning: a message from the exchange was missed or could not be read")
        if isinstance(item, DecodeFailed) and item.type in (None, "session_state"):
            # It may have said the market closed: quote again only after the next OPEN.
            quoter.market_open = False
        quoter.on_missed(
            isinstance(item, SeqGap) or (isinstance(item, DecodeFailed) and item.type is None)
        )
    elif isinstance(event, Received) and event.type == "resume_ack":
        quoter.resume_answered = True
    elif isinstance(event, Connected):
        quoter.on_connected(event)
    elif isinstance(event, Retrying):
        quoter.on_retrying(event)
    elif is_order_event(event):
        quoter.on_order_event(event.message)
    return True


def apply_received(quoter: Quoter, view: RestingOrders, item: object) -> str | None:
    """Apply one item from `pump`. Returns why quoting must stop, or None to go on."""
    if item is None:
        return "closed"
    if isinstance(item, Exception):
        raise item  # the connection failed for good
    if not handle(quoter, view, item):
        return "refused"
    if isinstance(item, ResumeComplete):
        if not quoter.resumed(item.replayed):
            return "unreliable"
    elif isinstance(item, Connected) and item.resume is None:
        # The exchange did not resume the session: only a view put right by the replay or
        # the snapshot says what became of this example's orders.
        if not quoter.resume_refused():
            return "unreliable"
    elif quoter.up and view.incomplete:
        quoter.lost()  # messages were missed while connected
    if quoter.missed:
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
    "unreliable" (events were missed, or an order cannot be told apart) or "closed" (the
    exchange closed the connection)."""
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
        except NotConnected as error:
            quoter.not_sent(error)
        except ConnectionClosed as error:
            # The connection dropped part-way through the send. The message stays
            # unanswered, and is never sent again: only its own reply settles it.
            print(f"perhaps not sent, the connection dropped: {error}")
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
    """Cancel this example's own orders. Returns "done" once every message it sent is
    answered and each order of its own has ended, "closed" if the exchange closes the
    connection, or "unconfirmed" if neither happens by `deadline`, a time on the event
    loop's clock. Missed messages do not stop it: it still cancels each of its own orders.
    Safe to call again after an interruption: it resends nothing already in flight."""
    quoter.quoting = False
    loop = asyncio.get_running_loop()
    while True:
        # Apply every event already received first, so what is safe to send is known.
        for item in drain(queue):
            if apply_received(quoter, view, item) == "closed":
                return "closed"
        if quoter.settled():
            return "done"
        sent = quoter.messages
        if quoter.up:
            # While the connection is down or resuming, wait for it instead.
            try:
                # Bounded by the time left, so a stalled send cannot outlast --drain-seconds.
                async with asyncio.timeout(max(deadline - loop.time(), 0)):
                    await quoter.cancel_own()
            except TimeoutError:
                return "unconfirmed"
            except NotConnected as error:
                quoter.not_sent(error)
            except ConnectionClosed as error:
                print(f"perhaps not sent, the connection dropped: {error}")
        if loop.time() >= deadline:
            return "unconfirmed"
        if quoter.messages != sent:
            continue  # apply what came in during that send before sending another
        # Wake at least every requote interval, to retry a rejected cancel.
        item = await next_event(queue, min(deadline - loop.time(), quoter.requote_seconds))
        if isinstance(item, TimeoutError):
            continue
        if apply_received(quoter, view, item) == "closed":
            return "closed"


# The longest wait for the connection to close at the end. A local choice, not a value
# the exchange sets. If the close does not finish, the connection is dropped anyway when
# the example exits.
CLOSE_SECONDS = 5.0


@contextlib.asynccontextmanager
async def closing(session: ReconnectingSession) -> AsyncIterator[ReconnectingSession]:
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


class Progress:
    """How far a run got, so that a run stopped at once, or by a connection that ended for
    good, can name the orders it may have left."""

    quoter: Quoter | None = None


async def run(url: str, args: argparse.Namespace, progress: Progress | None = None) -> int:
    # The token comes from QTE_TOKEN, or from the file named by QTE_TOKEN_FILE. It is read
    # once, here, and kept to authenticate again on each reconnect. Every new session
    # subscribes to the instrument again and resumes the team's order reports. The view of
    # resting orders is not passed as `resting=`: the example applies each event to it
    # itself, in the order it handles them, while the reader may already have read ahead.
    # `close_timeout` bounds the close of each connection, since a ReconnectingSession
    # finishes closing even when the wait for it is cut short.
    session = ReconnectingSession(url, instruments=[args.instrument], close_timeout=CLOSE_SECONDS)
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
        return await quote_and_clean_up(session, args, task, progress)
    finally:
        with contextlib.suppress(NotImplementedError):
            loop.remove_signal_handler(signal.SIGINT)


async def quote_and_clean_up(
    session: ReconnectingSession,
    args: argparse.Namespace,
    task: asyncio.Task,
    progress: Progress | None = None,
) -> int:
    loop = asyncio.get_running_loop()
    async with closing(session):
        view = RestingOrders()
        quoter = Quoter(session, view, args)
        if progress is not None:
            progress.quoter = quoter
        quoter.up = False  # until the first session is connected and resumed
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
                print("the exchange closed the connection, so this example cannot cancel")
                quoter.finish()
                return 1
            reason = {
                "time": "time is up",
                "refused": "nothing to quote",
                "unreliable": quoter.stop_reason,
            }.get(why, why)
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
            if outcome == "closed":
                print("the exchange closed the connection before the cancels were confirmed")
                quoter.finish()
                return 1
            if outcome == "unconfirmed":
                print(f"not all confirmed within --drain-seconds ({args.drain_seconds:g} s)")
            return quoter.finish()
        finally:
            reader.cancel()


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def name_what_is_left(progress: Progress, why: str) -> int:
    """The run's end when it could not clean up: stopped at once by a second Ctrl+C, or a
    connection that ended for good. Names each order and message it is unsure of."""
    quoter = progress.quoter
    if quoter is not None:
        for line in quoter.outstanding():
            print(f"WARNING: {line}", file=sys.stderr)
    return fail(why)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # With QTE_LOG_FORMAT=json, the SDK's log records are JSON lines on stderr.
    qte_sdk.logs.configure()
    try:
        url = resolve_url()  # QTE_URL from the environment, or from ./.env
    except MissingURL as error:
        fail(f"{error}; for example ws://127.0.0.1:8080/ws (see docs/quickstart.md)")
        return 2
    progress = Progress()
    try:
        return asyncio.run(run(url, args, progress))
    except MissingToken as error:
        # The message names the variable and the problem, never the token.
        fail(f"{error} (see docs/quickstart.md)")
        return 2
    except ValueError as error:
        return fail(f"not sent: {error}")
    except (SessionRejected, OSError, InvalidHandshake, SessionNotAcknowledged) as error:
        if isinstance(error, SessionRejected):
            why = f"the exchange refused the session: {error}"
        else:
            why = f"could not connect to QTE_URL: {error}"
        if progress.quoter is None or progress.quoter.sessions == 0:
            return fail(why)
        return name_what_is_left(
            progress, f"{why}; it will not reconnect, so cleanup was not possible"
        )
    except ConnectionClosed as error:
        if progress.quoter is None or progress.quoter.sessions == 0:
            return fail("the connection dropped before the session was up: run it again")
        close = describe_close(error) if isinstance(error, ConnectionClosedError) else "a close"
        return name_what_is_left(
            progress,
            f"the connection ended for good with {close}; it will not reconnect, so cleanup "
            "was not possible: check and cancel this example's orders yourself",
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        # A second Ctrl+C, before the example's orders were confirmed cancelled.
        return name_what_is_left(
            progress, "interrupted: this example's orders may still rest; check and cancel them"
        )


if __name__ == "__main__":
    sys.exit(main())
