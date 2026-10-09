"""Connect to the exchange and print the book of one instrument.

Set QTE_URL and QTE_TOKEN first, in ./.env or the environment (see docs/quickstart.md),
then:

    python examples/print_book.py --instrument AAPL --seconds 10

The run stops after --seconds or after --max-messages market-data messages, whichever
comes first. It sends no orders. The market session's state is printed when it changes.
Outside a session there is no book: the exchange sends the closed state and, if the
instrument has one, its last official close instead.

Every participant receives the same market data: one conflated feed, published on a
100 ms grid. A `book` is a snapshot of one instrument at the end of an interval, not a
stream of individual changes, so you see the book as everyone else sees it. It is
published only when it has changed: a grid point with no book means the book is the same
as the last one printed. Subscribing during a session brings the last book published at
once, with the grid time it was published at, and that same book can then arrive again.
The example keeps the latest book with `qte_sdk.books.LatestBooks` and prints a book only
when it is newer than the one held.
"""

import argparse
import asyncio
import contextlib
import os
import sys
from collections.abc import AsyncIterator
from contextlib import aclosing

from websockets.exceptions import ConnectionClosedError, InvalidHandshake

import qte_sdk.logs
from qte_sdk.books import LatestBooks
from qte_sdk.connection import SessionRejected
from qte_sdk.contract.v1.common_pb2 import MarketSessionPhase
from qte_sdk.market_data import (
    Book,
    DecodeFailed,
    InstrumentCondition,
    Mark,
    MarketDataEvent,
    OfficialClose,
    Reject,
    SeqGap,
    SessionState,
    Trades,
    market_data,
    subscribe,
)
from qte_sdk.orders import reason_code_name
from qte_sdk.session import (
    MissingToken,
    MissingURL,
    Session,
    SessionNotAcknowledged,
    open_session,
    resolve_url,
)
from qte_sdk.units import to_decimal


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Print the book of one instrument.")
    parser.color = False  # Python 3.14 colours argparse's output; it is read as plain text.
    parser.add_argument(
        "--instrument",
        default=os.environ.get("QTE_INSTRUMENT", "AAPL"),
        help="instrument to subscribe to (default: $QTE_INSTRUMENT, else AAPL)",
    )
    parser.add_argument(
        "--seconds", type=float, default=30.0, help="stop after this long (default 30)"
    )
    parser.add_argument(
        "--max-messages",
        type=int,
        default=100,
        help="stop after this many market-data messages (default 100)",
    )
    return parser.parse_args(argv)


def top(levels, empty: str) -> str:
    """The first level of one side of the wall as "price x size", in dollars."""
    if not levels:
        return empty
    return f"{to_decimal(levels[0].price)} x {levels[0].size}"


def show(item: MarketDataEvent) -> bool:
    """Print one market-data event. Returns False when there is no point carrying on."""
    match item:
        case Book():
            # Level 1 of each side of the wall, then how many prices hold student orders.
            bid = top(item.bid_levels, "no wall bid")
            ask = top(item.ask_levels, "no wall offer")
            students = f"{len(item.student_bid_levels)}/{len(item.student_ask_levels)}"
            condition = InstrumentCondition.Name(item.condition)
            print(f"book   {item.instrument}  {bid}  |  {ask}  students {students}  {condition}")
        case Trades():
            for tape_print in item.prints:
                print(
                    f"trade  {item.instrument}  {tape_print.size} @ {to_decimal(tape_print.price)}"
                )
        case Mark():
            condition = InstrumentCondition.Name(item.condition)
            if item.condition == InstrumentCondition.REFERENCE_UNAVAILABLE:
                print(f"mark   {item.instrument}  none yet ({condition})")
            else:
                print(f"mark   {item.instrument}  {to_decimal(item.value)} ({condition})")
        case SessionState():
            phase = MarketSessionPhase.Name(item.state)
            outage = ", outage in force" if item.outage_active else ""
            print(f"market session {item.session_date}: {phase}{outage}")
        case OfficialClose():
            # Sent only outside a session, in place of a book.
            frozen = " (frozen)" if item.frozen else ""
            close = to_decimal(item.value)
            print(f"close  {item.instrument}  {close} on {item.session_date}{frozen}")
        case Reject():
            # The exchange refused the subscription, for example an unknown instrument.
            detail = f" ({item.reason_detail})" if item.HasField("reason_detail") else ""
            print(f"subscription refused: {reason_code_name(item.reason_code)}{detail}")
            return False
        case SeqGap():
            print(f"warning: messages missed (expected seq {item.expected}, got {item.received})")
        case DecodeFailed():
            print(f"warning: a message could not be decoded: {item.error}")
    return True


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
    # The token comes from QTE_TOKEN, or from the file named by QTE_TOKEN_FILE.
    session = await open_session(url)
    async with closing(session):
        info = session.info
        print(f"connected: team {info.team}, unscored session: {info.unscored}")
        received = 0
        last_state = None
        books = LatestBooks()
        try:
            async with asyncio.timeout(args.seconds):
                # Send on the session and read from it.
                await subscribe(session, [args.instrument])
                async with aclosing(market_data(session)) as items:
                    async for item in items:
                        received += 1
                        # The session state is published on every interval; print it only
                        # when it changes. A book repeated at the grid time already held,
                        # or older, is not printed either. Every message counts towards
                        # --max-messages.
                        newer_book = books.update(item)
                        repeated = False
                        if isinstance(item, SessionState):
                            repeated = (item.state, item.outage_active) == last_state
                            last_state = (item.state, item.outage_active)
                        elif isinstance(item, Book):
                            repeated = not newer_book
                        if not repeated and not show(item):
                            return 1
                        if received >= args.max_messages:
                            print(f"stopped after {received} messages")
                            return 0
        except TimeoutError:
            print(f"stopped after {args.seconds:g} seconds ({received} messages)")
            return 0
    print("the exchange closed the connection")
    return 0


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
    except SessionRejected as error:
        return fail(f"the exchange refused the session: {error}")
    except (OSError, InvalidHandshake, SessionNotAcknowledged) as error:
        return fail(f"could not connect to QTE_URL: {error}")
    except ConnectionClosedError:
        return fail("the connection dropped, and this example does not reconnect: run it again")
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
