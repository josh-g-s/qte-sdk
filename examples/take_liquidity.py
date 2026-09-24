"""Take liquidity with one market order and report how it filled.

Set QTE_URL and QTE_TOKEN first (see docs/quickstart.md), then:

    python examples/take_liquidity.py --instrument AAPL --strat-id <your strategy> --side buy

The example waits for a book that shows the wall on the side it will trade against, sends
one MARKET order of --size shares, prints the `accepted` or `reject`, every fill and any
unfilled remainder the exchange cancels, and stops once the order is finished or after
--seconds, whichever comes first. It sends exactly one order.

Timing. The exchange holds every order message for its order delay before applying it,
currently 150 ms, so the book you decided on is at least that old when your order
reaches it and the price you get can differ from the one you saw. The example prints the
round trip it measures to the `accepted`. The delay, the price collar and the message
budgets are set by the exchange and may change, so nothing here depends on their values.

A fill of a market order carries no order price, so nothing on it says which market
order it belongs to. With one market order in flight per strategy, instrument and side,
as here, every such fill is this order's.
"""

import argparse
import asyncio
import os
import sys
import time

from google.protobuf.message import Message
from websockets.exceptions import ConnectionClosedError, InvalidHandshake

from qte_sdk.connection import Connection, SessionRejected
from qte_sdk.contract.v1.common_pb2 import BUY, MARKET, SELL, Liquidity
from qte_sdk.contract.v1.order_events_pb2 import Accepted, Execution, OrderCancelled, Reject
from qte_sdk.market_data import Book, DecodeFailed, SeqGap, as_market_data, subscribe
from qte_sdk.orders import is_order_event, reason_code_name, request_ref_of, send_new
from qte_sdk.session import MissingToken, SessionNotAcknowledged, open_session
from qte_sdk.units import to_decimal


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Take liquidity with one market order.")
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
    def __init__(self, conn: Connection, args: argparse.Namespace) -> None:
        self.conn = conn
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

    async def on_book(self, book: Book) -> None:
        if self.ref is not None or book.instrument != self.instrument:
            return
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
        self.sent_at = time.monotonic()
        self.ref = await send_new(
            self.conn,
            strat_id=self.strat_id,
            instrument=self.instrument,
            side=self.side,
            order_type=MARKET,  # a market order carries no price
            size=self.size,
        )

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
        match message:
            case Accepted() if request_ref_of(message) == self.ref:
                elapsed_ms = (time.monotonic() - self.sent_at) * 1000
                print(f"accepted after a {elapsed_ms:.0f} ms round trip")
            case Reject() if request_ref_of(message) == self.ref:
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


async def run(url: str, args: argparse.Namespace) -> int:
    # The token comes from the QTE_TOKEN environment variable.
    session = await open_session(url)
    async with session:
        print(f"connected: team {session.info.team}, unscored session: {session.info.unscored}")
        await subscribe(session.connection, [args.instrument])
        taker = Taker(session.connection, args)
        try:
            async with asyncio.timeout(args.seconds):
                async for event in session:
                    item = as_market_data(event)
                    if isinstance(item, Book):
                        await taker.on_book(item)
                    elif isinstance(item, Reject):
                        print(f"subscription refused: {reason_code_name(item.reason_code)}")
                        return 1
                    elif isinstance(item, SeqGap | DecodeFailed):
                        print("warning: a message from the exchange was missed or unreadable")
                        taker.missed = True
                    elif is_order_event(event):
                        taker.on_order_event(event.message)
                    if taker.done:
                        print(taker.summary())
                        return 0
        except TimeoutError:
            sent = "the order's outcome is not known yet" if taker.ref else "no order was sent"
            print(f"stopped after {args.seconds:g} seconds: {sent}; {taker.summary()}")
            return 0
    print("the exchange closed the connection")
    return 1


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
        return fail("the connection dropped; the SDK does not reconnect for you yet, so run again")
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
