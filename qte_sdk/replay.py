"""Replay a closed session's market data through your strategy's loop.

    client = HistoryClient()  # address from QTE_HISTORY_URL, token from QTE_TOKEN(_FILE)
    async with aclosing(replay(client, "2026-01-05", ["AAPL", "MSFT"])) as items:
        async for item in items:
            match item:
                case Book() | Trades() | Mark() | SessionState():
                    ...  # the same handling code as for market_data(session)
                case Unknown() | DecodeFailed():
                    ...  # a message this SDK cannot use: report it, do not ignore it

`replay` fetches one closed session's `book`, `trades` and `mark` streams for each
instrument you name, and the session's `session_state` stream, from the history service
(`qte_sdk.history`), and merges them into one stream in time order. The items are the
same generated classes that `qte_sdk.market_data.market_data` yields, so a loop that
handles market data from a live session runs on a replay unchanged. The one addition is
`Unknown`, a message of a type this SDK does not know, which `fetch` reports and the live
`market_data` does not deliver.

Market data only. A replay sends no orders and takes none, fills nothing, and keeps no
positions, cash or profit and loss. It shows what the market published to everyone that
day, with whatever was really traded then, your own team's orders and trades included if
it traded. It cannot add an order that was not there, or work out what one would have
done.

Order. Grid points come in order: every message of one grid point comes before any
message of the next, as on a live connection. A message's grid point is its `grid_time`
(`Book`, `Trades`, `SessionState`), or its `sampled_at` for a `Mark`, which is published
on its own one-second grid. The messages of one grid point (books, trades, marks and the
session state, across instruments) come in no promised order, live or in a replay: treat
them as a set, and do not rely on any order within it. A replay of the same data comes
out the same way each time, so a run can be repeated, but the order it uses within a grid
point is not part of this API and may change. Within one stream, the history service's
publication order is kept: an `Unknown` or a `DecodeFailed` stays where it was in its
stream, right after the message before it there.

Memory. Each (instrument, channel) is its own download, plus one for the session state,
and all of them stay open while the replay runs: each is read only as far as the merge
needs, so a whole day never has to fit in memory, but name only the instruments you need.
`replay` opens them one after another, reading the first message of each, before it
yields anything. `HistoryClient.fetch_range` is not used: a range response holds its
entries one after another, so merging them by time would mean holding all but the last
in memory.

Errors. A stream with no messages, such as an instrument with no trades that day, adds
nothing. Any other failure of a download is raised from the replay as `fetch` raises it,
after the items before it: `HistoryUnavailable` for an instrument or channel the service
does not know or a day with no session, `HistoryNotClosed` for a session that has not
closed, `HistoryPending` if the data is still not ready after the client's `max_wait`,
and so on (see `qte_sdk.history`). The other downloads are closed when it is raised.

Stopping. Use the replay inside `aclosing`, as above: leaving the block, by `break`, an
error or the end of the session, closes every download.

Pace. By default (`speed=None`) the replay runs as fast as your loop takes the items.
With `speed`, it waits before each message until its time, counted from the first
message, divided by `speed`, has passed on this machine's monotonic clock: `speed=1.0`
replays at the pace the session ran, `speed=10.0` ten times as fast. If your loop falls
behind, messages come without waiting until it has caught up; none is skipped.
"""

import asyncio
import heapq
import itertools
import math
import time
from collections.abc import AsyncGenerator, AsyncIterator, Iterable
from contextlib import AsyncExitStack, aclosing
from datetime import date

from qte_sdk.contract.v1.market_data_pb2 import Book, Mark, SessionState, Trades
from qte_sdk.history import HistoryClient, HistoryItem, _date_text

__all__ = ["CHANNELS", "replay"]

CHANNELS: tuple[str, ...] = ("book", "trades", "mark", "session_state")
"""The channels a replay can merge."""

_PER_INSTRUMENT = frozenset({"book", "trades", "mark"})
# Before any timestamp, for an unusable message at the start of its stream.
_EARLIEST = -(2**63)

# Module attributes, so tests can pace a replay without real time passing.
_clock = time.monotonic
_sleep = asyncio.sleep


def replay(
    client: HistoryClient,
    session_date: date | str,
    instruments: Iterable[str],
    channels: Iterable[str] = CHANNELS,
    *,
    speed: float | None = None,
) -> AsyncGenerator[HistoryItem, None]:
    """Yield one closed session's market data for `instruments`, merged in time order.

    `session_date` is the session's date, a `datetime.date` or `"YYYY-MM-DD"`.
    `instruments` is a list of instrument ids; each is replayed once, whatever the order
    or repeats. `channels` are drawn from `CHANNELS`: `book`, `trades` and `mark` for each
    instrument, and `session_state` for the session. `speed` paces the replay: None (the
    default) for as fast as you read it, or a positive number, 1.0 for the pace the
    session ran at. Raises `TypeError` or `ValueError` at once, before any download, for
    arguments it cannot use, a date that does not parse included. Whether the session
    and the instruments exist only the history service can say, as the replay runs. See
    the module docstring for the order and the errors.
    """
    if isinstance(session_date, str):
        session_date = date.fromisoformat(session_date)  # ValueError if it does not parse
    elif not isinstance(session_date, date):
        raise TypeError("pass a session date as a datetime.date or 'YYYY-MM-DD'")
    if isinstance(instruments, str):
        raise TypeError('pass instruments as a list, such as ["AAPL"], not a single string')
    if isinstance(channels, str):
        raise TypeError('pass channels as a list, such as ["book"], not a single string')
    names = list(instruments)
    if any(not isinstance(name, str) or not name for name in names):
        raise ValueError("instruments must be non-empty instrument ids")
    wanted = list(channels)
    unknown = sorted(set(wanted) - set(CHANNELS))
    if unknown:
        raise ValueError(f"unknown channels {unknown}: choose from {list(CHANNELS)}")
    if not wanted:
        raise ValueError(f"name at least one channel from {list(CHANNELS)}")
    if not names and _PER_INSTRUMENT & set(wanted):
        raise ValueError("name at least one instrument, or replay only the session_state")
    if speed is not None and not (math.isfinite(speed) and speed > 0):
        raise ValueError("speed must be a positive number, or None for as fast as possible")
    streams = [
        (rank, instrument)
        for rank, channel in enumerate(CHANNELS)
        if channel in wanted
        for instrument in (sorted(set(names)) if channel in _PER_INSTRUMENT else [""])
    ]
    # _date_text refuses a datetime, which is also a date.
    return _replay(client, _date_text(session_date), streams, speed)


class _Stream:
    """One download being merged: its items, its place among messages at the same time,
    and the time of its latest message that had one."""

    __slots__ = ("items", "rank", "instrument", "last")

    def __init__(self, items: AsyncIterator[HistoryItem], rank: int, instrument: str) -> None:
        self.items = items
        self.rank = rank
        self.instrument = instrument
        self.last = _EARLIEST


# (time, channel rank, instrument, a counter that keeps each stream's own order, the
# message, its stream). The counter is unique, so the message is never compared. The
# channel rank and instrument only make the order within one grid point repeatable; that
# order is not promised to callers.
_Entry = tuple[int, int, str, int, HistoryItem, _Stream]


async def _replay(
    client: HistoryClient,
    session_date: str,
    streams: list[tuple[int, str]],
    speed: float | None,
) -> AsyncGenerator[HistoryItem, None]:
    heap: list[_Entry] = []
    counter = itertools.count()

    async def advance(stream: _Stream) -> None:
        """Put the stream's next message, if any, on the heap."""
        try:
            item = await anext(stream.items)
        except StopAsyncIteration:
            return
        moment = _time_of(item)
        if moment is None:
            moment = stream.last  # kept just after the message before it in its stream
        else:
            stream.last = moment
        heapq.heappush(heap, (moment, stream.rank, stream.instrument, next(counter), item, stream))

    async with AsyncExitStack() as downloads:
        for rank, instrument in streams:
            channel = CHANNELS[rank]
            if channel == "session_state":
                fetched = client.fetch_session_state(session_date)
            else:
                fetched = client.fetch(session_date, instrument, channel)
            items = await downloads.enter_async_context(aclosing(fetched))
            await advance(_Stream(items, rank, instrument))
        start: tuple[float, int] | None = None  # (clock, message time) at the first message
        while heap:
            moment, _, _, _, item, stream = heapq.heappop(heap)
            if speed is not None and _time_of(item) is not None:
                if start is None:
                    start = (_clock(), moment)
                else:
                    wait = start[0] + (moment - start[1]) / (1000 * speed) - _clock()
                    if wait > 0:
                        await _sleep(wait)
            yield item
            await advance(stream)


def _time_of(item: HistoryItem) -> int | None:
    """When a message belongs, in milliseconds since the epoch, or None if it has no time."""
    if isinstance(item, Book | Trades | SessionState):
        return item.grid_time
    if isinstance(item, Mark):
        return item.sampled_at
    return None
