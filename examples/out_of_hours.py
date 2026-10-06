"""Connect outside a session and see the closed market. Sends no orders.

Set QTE_URL and QTE_TOKEN first, in ./.env or the environment (see docs/quickstart.md),
then:

    python examples/out_of_hours.py --instrument AAPL

The example is the runnable part of docs/out-of-hours.md. It opens a session, prints the
exchange's time, waits for the exchange's calendar and prints the last session that has
closed and when the next one opens, then subscribes to one instrument and prints the
market session state the exchange answers with and, when that state names it, the wait
until the next session opens. It stops after --seconds.

Times are printed in UTC, and in New York time too where Python has time zone data. On
Windows that comes from the tzdata package, which installing the SDK brings in there;
without it (an SDK installed before 1.0.1, say) the example prints UTC only. New York time
is only for reading: the trading hours come from the calendar, and "now" is the exchange's
clock, never this computer's.

Outside a session the state is CLOSED, and the reply carries each instrument's official
close if it has one. The example prints the ones that arrive, and says so when none does,
which is expected for an instrument with no official close yet. During a session the
state is OPEN and the example simply stops, since this is not a way to watch a live
market (use print_book.py for that).
"""

import argparse
import asyncio
import contextlib
import os
import sys
from collections.abc import AsyncIterator
from contextlib import aclosing
from datetime import timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from websockets.exceptions import ConnectionClosedError, InvalidHandshake

from qte_sdk.calendar import Calendar, next_session
from qte_sdk.connection import SessionRejected
from qte_sdk.contract.v1.common_pb2 import MarketSessionPhase
from qte_sdk.market_data import (
    OfficialClose,
    Reject,
    SessionState,
    market_data,
    subscribe,
    until_next_open,
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
from qte_sdk.units import to_datetime, to_decimal, to_timedelta

# The longest wait for the calendar, and for the connection to close at the end. Local
# choices, not values the exchange sets.
CALENDAR_SECONDS = 5.0
CLOSE_SECONDS = 5.0


def load_new_york() -> tzinfo | None:
    """New York's time zone, to show times in, or None where Python has no time zone data
    (on Windows without the tzdata package, which the SDK installs there)."""
    try:
        return ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:
        return None


NEW_YORK = load_new_york()


def show_time(timestamp: int, zone: tzinfo | None = NEW_YORK) -> str:
    """An exchange timestamp as UTC and, given a zone, New York time, to the minute."""
    utc = to_datetime(timestamp)
    shown = f"{utc:%Y-%m-%d %H:%M} UTC"
    if zone is None:
        return shown
    local = utc.astimezone(zone)
    # The date only if New York is on a different day from UTC.
    local_text = f"{local:%H:%M}" if local.date() == utc.date() else f"{local:%Y-%m-%d %H:%M}"
    return f"{shown} ({local_text} New York)"


def show_wait(wait: timedelta) -> str:
    """A wait as hours and whole minutes, rounded down: "in 40 h 13 min", or "... ago"
    when the time has already passed."""
    minutes = abs(wait) // timedelta(minutes=1)
    hours, minutes = divmod(minutes, 60)
    if hours:
        text = f"{hours} h {minutes} min"
    elif minutes:
        text = f"{minutes} min"
    else:
        text = "less than a minute"
    return f"in {text}" if wait >= timedelta(0) else f"{text} ago"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="See the closed market outside a session.")
    parser.add_argument(
        "--instrument",
        default=os.environ.get("QTE_INSTRUMENT", "AAPL"),
        help="instrument to subscribe to (default: $QTE_INSTRUMENT, else AAPL)",
    )
    parser.add_argument(
        "--seconds", type=float, default=10.0, help="stop after this long (default 10)"
    )
    return parser.parse_args(argv)


def show_calendar(calendar: Calendar | None, now: int) -> None:
    """Print the last session that has closed and when the next one opens.

    `now` is the exchange's current time, `session.info.server_time`."""
    if calendar is None:
        print("calendar: none received")
        return
    closed = [entry for entry in calendar.sessions if entry.close_time <= now]
    upcoming = next_session(calendar, now)
    # The session date is what the history service asks for.
    print("last closed session:", closed[-1].session_date if closed else "none this term")
    if upcoming is None:
        print("next session: none left this term")
    else:
        wait = to_timedelta(upcoming.open_time - now)
        opens = f"opens {show_time(upcoming.open_time)}, {show_wait(wait)}"
        print(f"next session: {upcoming.session_date}, {opens}")


def show_next_open(state: SessionState, now: int) -> None:
    """Print when the next session opens, if the closed-market reply names one.

    `now` is the exchange's current time, never the reply's `grid_time`: outside a session
    that equals `close_time`, which can be in the future."""
    wait = until_next_open(state, now)  # in milliseconds
    if wait is None:
        print("next open: not given in the session state (the calendar has the schedule)")
    else:
        print(f"next open: {show_time(state.next_open_time)}, {show_wait(to_timedelta(wait))}")


@contextlib.asynccontextmanager
async def closing(session: Session) -> AsyncIterator[Session]:
    """Like `async with session:`, but waits at most CLOSE_SECONDS for the close."""
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
    async with closing(session):
        print(f"connected: team {session.info.team}, unscored session: {session.info.unscored}")
        # The exchange's clock when it acknowledged the session: "now" from here on.
        print(f"exchange time: {show_time(session.info.server_time)}")
        seen_closed = False
        closes = 0
        try:
            async with asyncio.timeout(args.seconds):
                calendar = await session.wait_for_calendar(
                    timeout=min(CALENDAR_SECONDS, args.seconds / 2)
                )
                show_calendar(calendar, session.info.server_time)
                await subscribe(session, [args.instrument])
                async with aclosing(market_data(session)) as items:
                    async for item in items:
                        if isinstance(item, SessionState):
                            phase = MarketSessionPhase.Name(item.state)
                            print(f"market session {item.session_date}: {phase}")
                            if item.state != MarketSessionPhase.CLOSED:
                                print("a session is under way: run this outside one")
                                return 0
                            seen_closed = True
                            show_next_open(item, session.info.server_time)
                        elif isinstance(item, OfficialClose):
                            closes += 1
                            close = to_decimal(item.value)
                            print(f"official close {item.instrument}: {close}")
                        elif isinstance(item, Reject):
                            reason = reason_code_name(item.reason_code)
                            print(f"subscription refused: {reason}")
                            return 1
        except TimeoutError:
            if not seen_closed:
                print("no market session state arrived")
            elif closes == 0:
                print("no official close for this instrument")
            print(f"stopped after {args.seconds:g} seconds")
            return 0
    print("the exchange closed the connection")
    return 0


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        url = resolve_url()  # QTE_URL from the environment, or from ./.env
    except MissingURL as error:
        fail(f"{error}; for example ws://127.0.0.1:8080/ws (see docs/quickstart.md)")
        return 2
    try:
        return asyncio.run(run(url, args))
    except MissingToken as error:
        # The message names the source and the problem, never the token.
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
