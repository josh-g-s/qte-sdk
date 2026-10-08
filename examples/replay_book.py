"""Replay a past session's book of one instrument and print the best bid and ask.

Set QTE_HISTORY_URL (the history service's address, which the course team gives you) and
QTE_TOKEN first. QTE_TOKEN can also come from ./.env; QTE_HISTORY_URL is read from the
environment only. Then:

    python examples/replay_book.py --date 2026-01-05 --instrument AAPL

It fetches that session's books and market session state from the history service,
merged in time order by `qte_sdk.replay`, and prints the best bid and ask across the
wall and participants' resting orders each time the book changes, with its grid time in
UTC. It needs no exchange session and works at any hour, but only for a session that has
closed. It stops after --max-books books, after --seconds, or at the end of the session's
data, whichever comes first. By default it runs as fast as it can print; --speed 1 replays
at the pace the session ran, --speed 10 ten times as fast.

This is market data only: the replay sends no orders and fills nothing. It shows what the
market published that day, with whatever was really traded then, and cannot add an order
that was not there or work out what one would have done.
"""

import argparse
import asyncio
import math
import os
import sys
from contextlib import aclosing
from datetime import date

from qte_sdk.connection import DecodeFailed, Unknown
from qte_sdk.contract.v1.common_pb2 import MarketSessionPhase
from qte_sdk.history import (
    HistoryClient,
    HistoryError,
    HistoryNotClosed,
    HistoryPending,
    HistoryUnavailable,
    MissingHistoryURL,
)
from qte_sdk.market_data import Book, SessionState
from qte_sdk.replay import ReplayOutOfOrder, replay
from qte_sdk.session import MissingToken
from qte_sdk.units import to_datetime, to_decimal


def positive_number(text: str) -> float:
    value = float(text)
    if not (math.isfinite(value) and value > 0):
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive number")
    return value


def positive_count(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive whole number")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay a past session's book of one instrument.")
    parser.color = False  # Python 3.14 colours argparse's output; it is read as plain text.
    parser.add_argument(
        "--date",
        required=True,
        type=date.fromisoformat,
        help="the session date to replay, as YYYY-MM-DD; the session must have closed",
    )
    parser.add_argument(
        "--instrument",
        default=os.environ.get("QTE_INSTRUMENT", "AAPL"),
        help="instrument to replay (default: $QTE_INSTRUMENT, else AAPL)",
    )
    parser.add_argument(
        "--speed",
        type=positive_number,
        default=None,
        help="replay this many times as fast as the session ran (default: as fast as possible)",
    )
    parser.add_argument(
        "--max-books",
        type=positive_count,
        default=50,
        help="stop after this many books (default 50)",
    )
    parser.add_argument(
        "--seconds", type=positive_number, default=60.0, help="stop after this long (default 60)"
    )
    return parser.parse_args(argv)


def best(book: Book) -> tuple[int | None, int | None]:
    """The best bid and ask across the wall and participants' resting orders."""
    bids = [levels[0].price for levels in (book.bid_levels, book.student_bid_levels) if levels]
    asks = [levels[0].price for levels in (book.ask_levels, book.student_ask_levels) if levels]
    return (max(bids) if bids else None, min(asks) if asks else None)


def price(micros: int | None) -> str:
    return "none" if micros is None else str(to_decimal(micros))


def clock(timestamp: int) -> str:
    """An exchange timestamp as a UTC time of day, to the millisecond."""
    return to_datetime(timestamp).strftime("%H:%M:%S.%f")[:-3]


async def run(client: HistoryClient, args: argparse.Namespace) -> int:
    print(f"replaying {args.instrument} on {args.date} (times in UTC)")
    books = 0
    last_state = None
    items = replay(
        client, args.date, [args.instrument], ["book", "session_state"], speed=args.speed
    )
    deadline = asyncio.timeout(args.seconds)
    try:
        async with deadline, aclosing(items) as stream:
            async for item in stream:
                match item:
                    case Book():
                        # The history service, like the live feed, sends a book only when
                        # it has changed, so every book here is a new one.
                        books += 1
                        bid, ask = best(item)
                        print(
                            f"{clock(item.grid_time)}  {item.instrument}  "
                            f"bid {price(bid)}  ask {price(ask)}"
                        )
                        if books >= args.max_books:
                            print(f"stopped after {books} books")
                            return 0
                    case SessionState():
                        # Sent at every grid point: print it only when it changes.
                        state = (item.state, item.outage_active)
                        if state != last_state:
                            phase = MarketSessionPhase.Name(item.state)
                            outage = ", outage in force" if item.outage_active else ""
                            print(f"{clock(item.grid_time)}  market session {phase}{outage}")
                            last_state = state
                    case Unknown() | DecodeFailed():
                        # A line this SDK cannot use: a book may be missing after it.
                        print(f"warning: a message could not be used: {type(item).__name__}")
    except TimeoutError:
        if not deadline.expired():
            raise  # a network step timed out, not this run's own limit
        print(f"stopped after {args.seconds:g} seconds ({books} books)")
        return 0
    print(f"end of the session's data ({books} books)")
    return 0


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        # The address from QTE_HISTORY_URL, the token from QTE_TOKEN, QTE_TOKEN_FILE or .env.
        client = HistoryClient()
    except (MissingToken, MissingHistoryURL) as error:
        # The message names the variable and the problem, never the token.
        print(f"{error} (see docs/quickstart.md)", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run(client, args))
    except ReplayOutOfOrder as error:
        # Names the stream and both times; no server text or token.
        return fail(f"the history went back in time, so the replay stopped: {error}")
    except HistoryUnavailable:
        return fail("no such data: not a session day, or an instrument the service does not know")
    except HistoryNotClosed:
        return fail("that session has not closed yet: try again after its close")
    except HistoryPending as error:
        wait = f" in {error.retry_after:g} seconds" if error.retry_after is not None else " later"
        return fail(f"that session's data is not ready yet: try again{wait}")
    except HistoryError as error:
        # Only the kind of error and its HTTP status: the service's own text stays out.
        status = f" (HTTP {error.http_status})" if error.http_status is not None else ""
        return fail(f"the history request failed: {type(error).__name__}{status}")
    except OSError as error:
        return fail(f"could not reach the history service: {type(error).__name__}")
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
