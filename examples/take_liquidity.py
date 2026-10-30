"""Take liquidity with one market order and report how it filled.

Set QTE_URL and QTE_TOKEN first, in ./.env or the environment (see docs/quickstart.md),
then:

    python examples/take_liquidity.py --instrument AAPL --strat-id <your strategy> --side buy

The example waits for a book that shows the wall on the side it will trade against, sends
one MARKET order of --size shares, prints the `accepted` or `reject`, every fill and any
unfilled remainder the exchange cancels, and stops once the order is finished or after
--seconds, whichever comes first. It sends at most one order, and none if no such book
comes within --seconds. It never retries: a rejected order is printed and the example
stops.

The exchange publishes a book only when it changes, so the example keeps the latest book
it has received with `qte_sdk.books.LatestBooks` and decides from that. Subscribing
during a session brings the last book published at once, so in a quiet market the
example can act on it straight away rather than wait for the book to change.

Timing. The exchange holds every order message for its order delay before applying it,
currently 150 ms, so the book you decided on is at least that old when your order
reaches it and the price you get can differ from the one you saw. The example prints the
round trip it measures to the `accepted`. The delay, the price collar and the message
budgets are set by the exchange and may change, so nothing here depends on their values.

A fill of a market order carries no order price, so nothing on it says which market
order it belongs to. With one market order in flight per strategy, instrument and side,
as here, every such fill is this order's.

On a drop. The example connects with `qte_sdk.reconnect.ReconnectingSession`, so if the
connection drops, or the exchange restarts, it reconnects by itself, waiting longer between
attempts (at most 30 seconds). While it is down it sends nothing. Each new session
subscribes again and resumes the team's order reports, and the example sends nothing until
that resume is complete. Market data sent while it was down is not replayed, so it forgets
the book it held and decides from a book the new session sends. An order in flight at the
drop is never sent again. If the exchange replays the reports missed, the order's
`accepted`, fills and cancelled remainder arrive as if nothing had happened. If it sends a
snapshot of the resting orders instead, or does not resume, the reports missed are not
recovered, and a market order never rests, so nothing says how it filled: the example says
so, tells you to check your fills, and exits with status 1. So it does, too, if a message is
missed or unreadable during the resume (a `SeqGap` or `DecodeFailed`), since the report lost
may be one of its fills; a gap can also end the resume with no `ResumeComplete`. An
`accepted` that comes through the resume is printed as answered after a reconnect, with no
round trip. --seconds counts the time down too. Only the first connection is not retried:
if it fails, the example says why and stops; a later one that fails for good (a revoked
credential, say) says why and, with the order out, that it may have filled.
"""

import argparse
import asyncio
import contextlib
import os
import sys
import time
from collections.abc import AsyncIterator
from contextlib import aclosing

from google.protobuf.message import Message
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, InvalidHandshake

import qte_sdk.logs
from qte_sdk.books import LatestBooks
from qte_sdk.connection import (
    Connected,
    Disconnected,
    ReportGap,
    ResumeComplete,
    SessionRejected,
    describe_close,
)
from qte_sdk.contract.v1.common_pb2 import BUY, MARKET, SELL, Liquidity
from qte_sdk.contract.v1.order_events_pb2 import Accepted, Execution, OrderCancelled, Reject
from qte_sdk.market_data import Book, DecodeFailed, SeqGap, as_market_data
from qte_sdk.orders import (
    Sender,
    is_order_event,
    new_request_ref,
    reason_code_name,
    request_ref_of,
    send_new,
)
from qte_sdk.reconnect import NotConnected, ReconnectingSession, Retrying
from qte_sdk.session import (
    MissingToken,
    MissingURL,
    SessionNotAcknowledged,
    resolve_url,
)
from qte_sdk.units import to_decimal


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Take liquidity with one market order.")
    parser.color = False  # Python 3.14 colours argparse's output; it is read as plain text.
    parser.add_argument(
        "--instrument",
        default=os.environ.get("QTE_INSTRUMENT", "AAPL"),
        help="instrument to trade (default: $QTE_INSTRUMENT, else AAPL)",
    )
    parser.add_argument(
        "--strat-id",
        default=os.environ.get("QTE_STRAT_ID"),
        help="a strategy ID registered for your team (default: $QTE_STRAT_ID)",
    )
    parser.add_argument("--side", choices=["buy", "sell"], default="buy")
    parser.add_argument("--size", type=int, default=1, help="shares (default 1)")
    parser.add_argument(
        "--seconds", type=float, default=20.0, help="stop after this long (default 20)"
    )
    args = parser.parse_args(argv)
    if not args.strat_id:
        parser.error(
            "pass --strat-id or set QTE_STRAT_ID to a strategy ID registered for your team"
        )
    return args


class Taker:
    def __init__(self, session: Sender, args: argparse.Namespace) -> None:
        self.session = session
        self.instrument: str = args.instrument
        self.strat_id: str = args.strat_id
        self.side = BUY if args.side == "buy" else SELL
        self.size: int = args.size
        self.ref: str | None = None
        self.sent_at = 0.0
        self.waiting_for_wall = False
        self.filled = 0
        self.cost = 0  # micro-dollars
        self.done = False
        self.missed = False  # whether any message from the exchange was missed
        # The latest book of each instrument: the exchange sends a book only when it changes.
        self.books = LatestBooks()
        # Whether a session is up and its resume complete; nothing is sent until it is.
        self.up = True
        # Whether the order was in flight at a drop, until a resume says what became of it.
        self.in_doubt = False
        self.lost = False  # its reports while disconnected were not recovered
        # Whether a resume announced by a session is still to complete, and whether a
        # message was missed or unreadable during it, so its replay may lack a report.
        self.resuming = False
        self.resume_damaged = False
        # Whether the connection dropped after the order was sent: its `accepted` then
        # comes through a resume, and the time to it is no round trip.
        self.dropped_since_send = False

    async def on_book(self, book: Book) -> None:
        """Keep `book` if it is newer than the one held, and act on it if so."""
        if self.books.update(book):
            await self.act()

    async def act(self) -> None:
        """Send the order from the latest book held, unless one is already sent, no session
        is up and resumed, the book may be out of date (held from before a drop, or a
        gap, with no newer one since), or that book shows no wall on the side to trade
        against."""
        book = self.books.get(self.instrument)
        if self.ref is not None or book is None or not self.up:
            return
        if self.instrument in self.books.stale:
            return  # held from before a drop or a gap: wait for a book sent since
        # A buy trades against the offer, a sell against the bid.
        levels = book.ask_levels if self.side == BUY else book.bid_levels
        if not levels:
            if not self.waiting_for_wall:
                print("no wall on the side to trade against yet; waiting")
                self.waiting_for_wall = True
            return
        verb = "buy" if self.side == BUY else "sell"
        print(
            f"book shows {levels[0].size} @ {to_decimal(levels[0].price)}; "
            f"sending a MARKET order to {verb} {self.size} {self.instrument}"
        )
        # Record the request_ref before sending: if the send is cut short after the order
        # reached the exchange, the example still knows it may have sent one, and can
        # still match the exchange's reply to it.
        self.ref = new_request_ref()
        self.sent_at = time.monotonic()
        try:
            await send_new(
                self.session,
                strat_id=self.strat_id,
                instrument=self.instrument,
                side=self.side,
                order_type=MARKET,  # a market order carries no price
                size=self.size,
                request_ref=self.ref,
            )
        except NotConnected:
            # Nothing was sent: the order goes out from a book of the next session.
            print("not sent, the connection is down: sending once it is back")
            self.ref = None
        except ConnectionClosed as error:
            # It may have reached the exchange: it stays recorded as sent, and is never
            # sent again. The resume says what became of it.
            print(f"the connection dropped as the order was sent: {error}")

    def on_connected(self, connected: Connected) -> None:
        self.up = False  # until its resume is complete
        self.resuming = connected.resume is not None
        self.resume_damaged = False
        if connected.reconnected:
            print("reconnected: sending nothing until the resume is complete")
        if connected.resume is None:
            self.resumed(replayed=False)  # the exchange did not resume this session

    def on_missed(self, ends_resume: bool) -> None:
        """A message was missed or unreadable. During a resume, a report the replay was to
        bring may be the one lost. A `SeqGap`, or a `DecodeFailed` whose type is unknown,
        also ends the resume with no `ResumeComplete` (the session stops waiting for it),
        so it counts as a resume that did not recover the reports missed."""
        self.missed = True
        if not self.resuming:
            return
        self.resume_damaged = True
        if ends_resume:
            print("the resume was cut short by a message missed or unreadable")
            self.resumed(replayed=False)
        elif self.in_doubt:
            # The replay goes on, but its last fill may end the order before the
            # ResumeComplete: decide now, so the run cannot end as if nothing were lost.
            self.fills_unknown()

    def on_disconnected(self, disconnected: Disconnected) -> None:
        self.up = False
        # Market data sent while down is not replayed: decide from a book sent after it.
        self.books.update(disconnected)
        why = disconnected.error or "the exchange closed it"
        print(f"the connection dropped ({why}); sending nothing until it is back")
        self.resuming = False
        if self.ref is not None and not self.done:
            self.in_doubt = self.dropped_since_send = True
            print("the order sent may have filled meanwhile: the resume says what became of it")

    def resumed(self, replayed: bool) -> None:
        """A session's resume is complete, or the exchange did not resume it."""
        self.up = True
        self.resuming = False
        if not self.in_doubt:
            return
        if not replayed or self.resume_damaged:
            # A snapshot lists only resting orders, and a market order never rests: its
            # fills while disconnected are not recovered. Nor are they after a replay a
            # message was missed from. It is never sent again.
            self.fills_unknown()
        self.in_doubt = False

    def fills_unknown(self) -> None:
        """The order's reports while disconnected were not all recovered: its outcome is
        not known, and the run ends with status 1."""
        self.in_doubt = False
        print(
            "the order's reports while disconnected were not recovered (no complete "
            "replay), so how it filled is not known: check your fills before running "
            "this again"
        )
        self.missed = self.lost = self.done = True

    def order_out(self) -> bool:
        """Whether an order may have reached the exchange without its outcome known."""
        return self.ref is not None and not self.done

    def ours(self, message: Execution | OrderCancelled) -> bool:
        """Whether a fill or cancellation belongs to this example's market order: same
        strategy, instrument and side, and no order price, which only a market order lacks."""
        price_field = "order_price" if isinstance(message, Execution) else "price"
        return (
            self.ref is not None
            and message.strat_id == self.strat_id
            and message.instrument == self.instrument
            and message.side == self.side
            and not message.HasField(price_field)
        )

    def on_order_event(self, message: Message) -> None:
        # A reject the exchange could not tie to a message carries no request_ref.
        ref = request_ref_of(message)
        answers_ours = ref is not None and ref == self.ref
        match message:
            case Accepted() if answers_ours:
                if self.dropped_since_send:
                    print("accepted (answered after a reconnect)")
                else:
                    elapsed_ms = (time.monotonic() - self.sent_at) * 1000
                    print(f"accepted after a {elapsed_ms:.0f} ms round trip")
            case Reject() if answers_ours:
                detail = f" ({message.reason_detail})" if message.HasField("reason_detail") else ""
                print(f"REJECTED: {reason_code_name(message.reason_code)}{detail}")
                self.done = True
            case Execution() if self.ours(message):
                liquidity = Liquidity.Name(message.liquidity)
                print(
                    f"FILL {message.fill_size} @ {to_decimal(message.fill_price)} ({liquidity}, "
                    f"fee {to_decimal(message.fee)}), {message.remaining_size} left"
                )
                self.filled += message.fill_size
                self.cost += message.fill_size * message.fill_price
                if message.remaining_size == 0:
                    self.done = True
            case OrderCancelled() if self.ours(message):
                reason = reason_code_name(message.reason_code)
                print(f"the unfilled {message.cancelled_size} was cancelled ({reason})")
                self.done = True

    def summary(self) -> str:
        if self.filled == 0:
            text = "nothing filled"
        else:
            average = to_decimal(self.cost // self.filled)
            text = f"filled {self.filled} of {self.size} at an average of about {average}"
        if self.missed:
            text += " (messages were missed, so this may be incomplete: check your fills)"
        return text


# Said when the session ends for good while the order sent may have reached the exchange.
ORDER_OUT_WARNING = (
    "WARNING: the order sent may have reached the exchange and filled, and its outcome is "
    "not known. Check your fills before running this again"
)

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


async def run(url: str, args: argparse.Namespace) -> int:
    # The token comes from QTE_TOKEN, or from the file named by QTE_TOKEN_FILE. It is read
    # once, here, and kept to authenticate again on each reconnect. Every new session
    # subscribes to the instrument again and resumes the team's order reports, so the
    # order's fills sent while it was down can be replayed.
    # `close_timeout` bounds the close of each connection, since a ReconnectingSession
    # finishes closing even when the wait for it is cut short.
    session = ReconnectingSession(url, instruments=[args.instrument], close_timeout=CLOSE_SECONDS)
    async with closing(session):
        taker = Taker(session, args)
        taker.up = False  # until the first session is connected and resumed
        connected = False
        deadline = asyncio.timeout(args.seconds)
        try:
            async with deadline, aclosing(session.events()) as events:
                async for event in events:
                    if isinstance(event, Retrying):
                        if not connected and event.error is not None:
                            raise event.error  # the first connection failed: stop
                        print(f"reconnecting in {event.delay:.1f} s (attempt {event.attempt})")
                        continue
                    item = as_market_data(event)
                    if isinstance(event, Connected):
                        if not connected:
                            info = event.info
                            print(f"connected: team {info.team}, unscored session: {info.unscored}")
                        connected = True
                        taker.on_connected(event)
                    elif isinstance(event, ResumeComplete):
                        taker.resumed(event.replayed)
                    elif isinstance(item, Disconnected):
                        taker.on_disconnected(item)
                    elif isinstance(item, Book):
                        await taker.on_book(item)
                    elif isinstance(item, Reject):
                        print(f"subscription refused: {reason_code_name(item.reason_code)}")
                        return 1
                    elif isinstance(item, SeqGap | DecodeFailed) or isinstance(event, ReportGap):
                        print("warning: a message from the exchange was missed or unreadable")
                        if item is not None:
                            taker.books.update(item)  # it may have been a book: now stale
                        # As the session does: these end a resume with no ResumeComplete.
                        taker.on_missed(
                            isinstance(item, SeqGap)
                            or (isinstance(item, DecodeFailed) and item.type is None)
                        )
                    elif is_order_event(event):
                        taker.on_order_event(event.message)
                    if taker.done:
                        print(taker.summary())
                        return 1 if taker.lost else 0
                    await taker.act()  # a book held may be acted on once a resume completes
        except TimeoutError:
            if not deadline.expired():
                raise  # the first connection timed out
            # Once a request_ref is recorded the order may have reached the exchange, even
            # if its send did not finish.
            sent = "the order's outcome is not known yet" if taker.ref else "no order was sent"
            print(f"stopped after {args.seconds:g} seconds: {sent}; {taker.summary()}")
            return 0
        # After TimeoutError, an OSError too, so a deadline is not taken for one.
        except (SessionRejected, OSError, InvalidHandshake, SessionNotAcknowledged):
            if taker.order_out():
                # Not retried, such as a credential revoked while reconnecting.
                print(ORDER_OUT_WARNING, file=sys.stderr)
            raise
    print("the exchange closed the connection")
    return 1


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # With QTE_LOG_FORMAT=json, the SDK's log records are JSON lines on stderr.
    qte_sdk.logs.configure()
    try:
        url = resolve_url()  # QTE_URL from the environment, or from ./.env
    except MissingURL as error:
        fail(f"{error}; for example ws://127.0.0.1:8080/ws (see docs/quickstart.md)")
        return 2
    try:
        return asyncio.run(run(url, args))
    except MissingToken as error:
        # The message names the variable and the problem, never the token.
        fail(f"{error} (see docs/quickstart.md)")
        return 2
    except ValueError as error:
        return fail(f"not sent: {error}")
    except SessionRejected as error:
        return fail(f"the exchange refused the session: {error}")
    except (OSError, InvalidHandshake, SessionNotAcknowledged) as error:
        return fail(f"could not connect to QTE_URL: {error}")
    except ConnectionClosedError as error:
        # Not retried, such as a 1008 "credential revoked": the token no longer works.
        return fail(
            f"the connection dropped with {describe_close(error)}. The order's outcome may "
            "not be known. Check your fills before running this again"
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
