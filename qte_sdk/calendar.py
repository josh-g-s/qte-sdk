"""When the market next opens and closes, read from the exchange's own calendar.

    calendar = await session.wait_for_calendar()
    if calendar is not None:
        now = session.info.server_time
        opens = next_open(calendar, now)
        if opens is not None:
            print("the market next opens in", opens - now, "exchange timestamp units")

The exchange sends a `calendar` message right after it acknowledges a session, at any hour:
every session of the term with its open and close time, the named holidays inside the term,
and the term's dates. Everything here is computed from that message alone. Nothing about
trading hours, holidays or early closes is built into the SDK, so never hard-code them in
your own code either.

Times are the exchange's timestamps, the integers `session_ack.server_time` and every other
timestamp field carry. `now` must be one of those, for example `session.info.server_time`
or a timestamp from a later message, never your own machine's clock: the answer is then a
simple subtraction, with no time zone to handle.

An exchange that predates the calendar message never sends one, so code that uses these
helpers must also work without a calendar.
"""

from qte_sdk.contract.v1.session_pb2 import Calendar, CalendarSession

__all__ = [
    "Calendar",
    "CalendarSession",
    "next_close",
    "next_open",
    "next_session",
    "session_open_at",
]


def next_session(calendar: Calendar, now: int) -> CalendarSession | None:
    """The first session of the term that opens after `now`, or None if every session of
    the term has already opened."""
    upcoming = [entry for entry in calendar.sessions if entry.open_time > now]
    return min(upcoming, key=lambda entry: entry.open_time, default=None)


def next_open(calendar: Calendar, now: int) -> int | None:
    """The `open_time` of the first session that opens after `now`, or None if every
    session of the term has already opened.

    Days with no session (weekends and holidays) are not in the calendar, so they are
    skipped. While a session is open, this is the open of the next one, not the current.
    """
    entry = next_session(calendar, now)
    return None if entry is None else entry.open_time


def next_close(calendar: Calendar, now: int) -> int | None:
    """The `close_time` of the first session that closes after `now`, or None if every
    session of the term has already closed.

    While a session is open, this is its own close, which is earlier on an early-close
    day. Outside a session it is the close of the next session.
    """
    upcoming = [entry.close_time for entry in calendar.sessions if entry.close_time > now]
    return min(upcoming, default=None)


def session_open_at(calendar: Calendar, now: int) -> CalendarSession | None:
    """The session that is scheduled to be open at `now` (from its open, up to but not
    including its close), or None if the calendar has no session open then.

    This is the schedule. Whether the market is actually open is what the exchange's
    `session_state` messages report; check that before you trade.
    """
    for entry in calendar.sessions:
        if entry.open_time <= now < entry.close_time:
            return entry
    return None
