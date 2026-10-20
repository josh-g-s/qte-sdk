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
(`qte_sdk.history`), and merges them into one stream in time order. Name
`option_greeks` in `channels` to add each option contract's published Greeks too. The items are the
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
(`Book`, `Trades`, `OptionGreeks`, `SessionState`), or its `sampled_at` for a `Mark`,
which is published on its own one-second grid. The messages of one grid point (books,
trades, marks, Greeks and the session state, across instruments) come in no promised
order, live or in a replay: treat them as a set, and do not rely on any order within it.
A replay of the same data comes out the same way each time, so a run can be repeated, but
the order it uses within a grid point is not part of this API and may change. Within one
stream, the history service's publication order is kept: an `Unknown` or a `DecodeFailed`
stays where it was in its stream, right after the message before it there.

Memory. Each (instrument, channel) is its own download, plus one for the session state,
and all of them stay open while the replay runs: each is read only as far as the merge
needs, so a whole day never has to fit in memory, but name only the instruments you need.
`replay` opens them one after another, reading the first message of each, before it
yields anything. `HistoryClient.fetch_range` is not used: a range response holds its
entries one after another, so merging them by time would mean holding all but the last
in memory.

Options. An option contract is named by its OCC symbol, such as `SPY261120C00650000` (see
`qte_sdk.options`), and its `book`, `trades` and `mark` are merged as an equity's are.
`option_greeks` is not in the default `channels`, since a session cached before the
history service served Greeks has none and would fail the replay; name it to get them. A
session with options cached before that build (from 15 October 2026) also holds a
contract's object only for a channel on which it published something: a contract that
quoted but never traded has no `trades`, and that channel answers `HistoryUnavailable`,
which fails a default replay. Pass `skip_unavailable=True` to skip such a channel, or
name in `channels` only those the contracts published on. `option_greeks` is fetched only
for the instruments that are option symbols (`qte_sdk.options.is_option_symbol`), since
an equity has no Greeks, so `channels=CHANNELS` over equities alone replays what it did
before; an id that is not a well-formed option symbol is treated as an equity's. Naming
`option_greeks` as the only per-instrument channel with no option symbol among the
instruments is a `ValueError`. The option chain is not part of a replay: fetch it with
`HistoryClient.fetch_option_chain`. The Greeks follow the history contract of
qte-platform #2298, whose server build is not live yet; until it is, and for a session
cached before it that has not been upgraded since, `option_greeks` raises
`HistoryUnavailable`.

Errors. A stream the service holds with no messages, such as an equity with no trades
that day, adds nothing. A per-instrument channel the service answers unavailable when it
is opened raises `HistoryUnavailable`, unless `skip_unavailable=True`, which skips that
one (instrument, channel) and replays the rest; `session_state` raises either way, as
does a channel that fails once it has started. Any other failure of a download is raised
from the replay as `fetch` raises it, after the items before it: `HistoryUnavailable` for
an instrument or channel the service does not know or a day with no session,
`HistoryNotClosed` for a session that has not closed, `HistoryPending` if the data is
still not ready after the client's `max_wait`, and so on (see `qte_sdk.history`). A
stream whose times go back, one message earlier than the one before it, raises
`ReplayOutOfOrder` (a `ValueError`, code `QTE-REPLAY-OUT-OF-ORDER`) naming the stream and
both times, after the items before it, since the replay cannot merge it in order. The
other downloads are closed when any of these is raised. docs/errors.md says what each
code means and how to fix it. An error leaves the replay without the traceback it
gathered inside it, whose frames hold the messages read, as one from `fetch` does.

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

from qte_sdk import errors as _errors
from qte_sdk.contract.v1.market_data_pb2 import Book, Mark, OptionGreeks, SessionState, Trades
from qte_sdk.errors import QteError, token_shaped
from qte_sdk.history import HistoryClient, HistoryItem, HistoryUnavailable, _date_text, _Guarded
from qte_sdk.options import is_option_symbol
from qte_sdk.session import _detached, _text_holds_token

__all__ = ["CHANNELS", "DEFAULT_CHANNELS", "ReplayOutOfOrder", "replay"]

CHANNELS: tuple[str, ...] = ("book", "trades", "mark", "option_greeks", "session_state")
"""The channels a replay can merge."""

DEFAULT_CHANNELS: tuple[str, ...] = ("book", "trades", "mark", "session_state")
"""The channels a replay merges unless you name others: every channel but `option_greeks`."""

_PER_INSTRUMENT = frozenset({"book", "trades", "mark", "option_greeks"})
# Per-instrument channels only an option contract has.
_OPTION_ONLY = frozenset({"option_greeks"})
# Before any timestamp, for an unusable message at the start of its stream.
_EARLIEST = -(2**63)

# Module attributes, so tests can pace a replay without real time passing.
_clock = time.monotonic
_sleep = asyncio.sleep


def replay(
    client: HistoryClient,
    session_date: date | str,
    instruments: Iterable[str],
    channels: Iterable[str] = DEFAULT_CHANNELS,
    *,
    speed: float | None = None,
    skip_unavailable: bool = False,
) -> AsyncGenerator[HistoryItem, None]:
    """Yield one closed session's market data for `instruments`, merged in time order.

    `session_date` is the session's date, a `datetime.date` or `"YYYY-MM-DD"`.
    `instruments` is a list of instrument ids; each is replayed once, whatever the order
    or repeats; an option contract is named by its OCC symbol. `channels` are drawn from
    `CHANNELS`: `book`, `trades` and `mark` for each instrument, `option_greeks` for each
    instrument that is an option symbol, and `session_state` for the session; the default,
    `DEFAULT_CHANNELS`, is all but `option_greeks`. `speed` paces the replay: None (the
    default) for as fast as you read it, or a positive number, 1.0 for the pace the
    session ran at. `skip_unavailable=True` skips a per-instrument channel of an
    instrument that the service answers `HistoryUnavailable` (such as a contract's
    `trades` on a session cached before the options build, if it never traded) instead
    of raising, so an instrument id the service does not know then adds nothing rather
    than failing; `session_state` still raises. Raises `TypeError` or `ValueError` at
    once, before any download, for arguments it cannot use, a date that does not parse
    included. `ReplayOutOfOrder`, also
    a `ValueError`, comes later, while the replay runs, if a stream's times go back.
    Whether the session and the instruments exist only the history service can say, as
    the replay runs. See the module docstring for the order and the errors.
    """
    # The arguments can hold the token, pasted by mistake: an error for them is raised from
    # a frame that no longer holds them, without the frames that checked them.
    failure: Exception
    try:
        day, streams = _checked(
            client, session_date, instruments, channels, speed, skip_unavailable
        )
    except Exception as error:
        failure = _detached(error)
    else:
        # Errors leave without the traceback gathered inside the replay, whose frames hold
        # the messages it read and the instruments asked for.
        return _Guarded(_replay(client, day, streams, speed, skip_unavailable))
    del session_date, instruments, channels, skip_unavailable
    raise failure


def _checked(
    client: HistoryClient,
    session_date: date | str,
    instruments: Iterable[str],
    channels: Iterable[str],
    speed: float | None,
    skip_unavailable: bool,
) -> tuple[str, list[tuple[int, str]]]:
    """The session date as text and the streams to merge, or the error for arguments the
    replay cannot use. A date or channel it names is withheld if it holds the client's
    token, or is shaped like a token the exchange issues (see `errors.token_shaped`)."""
    secret = getattr(client, "_secret", None)

    def withhold(text: str) -> bool:
        return (secret is not None and _text_holds_token(text, secret)) or token_shaped(text)

    if isinstance(session_date, str):
        try:
            session_date = date.fromisoformat(session_date)
        except ValueError:
            if withhold(session_date):
                raise ValueError(f"Invalid isoformat string: {_errors.WITHHELD}") from None
            raise
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
        shown = [
            _errors.WITHHELD if withhold(c if isinstance(c, str) else repr(c)) else c
            for c in unknown
        ]
        raise ValueError(f"unknown channels {shown}: choose from {list(CHANNELS)}")
    if not wanted:
        raise ValueError(f"name at least one channel from {list(CHANNELS)}")
    if not names and _PER_INSTRUMENT & set(wanted):
        raise ValueError("name at least one instrument, or replay only the session_state")
    options = sorted({name for name in names if is_option_symbol(name)})
    # Only when nothing per instrument would be fetched: `channels=CHANNELS` over equities
    # still replays their books, trades and marks.
    per_instrument = _PER_INSTRUMENT & set(wanted)
    if per_instrument and per_instrument <= _OPTION_ONLY and not options:
        raise ValueError(
            "option_greeks needs at least one option contract, named by its OCC symbol such "
            "as SPY261120C00650000; an equity has no Greeks"
        )
    if speed is not None and not (math.isfinite(speed) and speed > 0):
        raise ValueError("speed must be a positive number, or None for as fast as possible")
    if not isinstance(skip_unavailable, bool):
        raise TypeError("skip_unavailable must be a bool")

    def instruments_of(channel: str) -> list[str]:
        if channel in _OPTION_ONLY:
            return options
        return sorted(set(names)) if channel in _PER_INSTRUMENT else [""]

    streams = [
        (rank, instrument)
        for rank, channel in enumerate(CHANNELS)
        if channel in wanted
        for instrument in instruments_of(channel)
    ]
    # _date_text refuses a datetime, which is also a date.
    return _date_text(session_date), streams


class ReplayOutOfOrder(QteError, ValueError):
    """A downloaded stream went back in time: one of its messages is earlier than the one
    before it. The replay cannot merge such a stream, so it stops and closes every download
    rather than deliver messages out of order. Code `QTE-REPLAY-OUT-OF-ORDER`."""

    code = _errors.REPLAY_OUT_OF_ORDER


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
    skip_unavailable: bool,
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
        elif moment < stream.last:
            raise _out_of_order(client, session_date, stream, moment)
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
            try:
                await advance(_Stream(items, rank, instrument))
            except HistoryUnavailable as error:
                # A per-instrument channel the service does not hold, such as a contract's
                # trades on a session cached before the options build: skipped if asked.
                # One answered unavailable only on a resume, after data came, still raises.
                if not (skip_unavailable and error._at_open and channel in _PER_INSTRUMENT):
                    raise
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


def _out_of_order(
    client: HistoryClient, session_date: str, stream: _Stream, moment: int
) -> ReplayOutOfOrder:
    """The error for a message at `moment` that follows a later one in `stream`. The
    instrument is the caller's text, so it is withheld if it holds the token."""
    channel = CHANNELS[stream.rank]
    where = channel if channel == "session_state" else f"{channel} of {stream.instrument}"
    where = f"{where} on {session_date}"
    what = f"a message at {moment} follows one at {stream.last}"
    secret = getattr(client, "_secret", None)
    fields = _errors.withheld(
        lambda shown: secret is not None and _text_holds_token(shown, secret),
        where=where,
        what=what,
    )
    # The arguments keep the text as given, unless it was withheld.
    if fields["where"] == _errors.WITHHELD:
        where = _errors.WITHHELD
    if fields["what"] == _errors.WITHHELD:
        what = _errors.WITHHELD
    return ReplayOutOfOrder(f"the {where} goes back in time: {what}", fields=fields)


def _time_of(item: HistoryItem) -> int | None:
    """When a message belongs, in milliseconds since the epoch, or None if it has no time."""
    if isinstance(item, Book | Trades | OptionGreeks | SessionState):
        return item.grid_time
    if isinstance(item, Mark):
        return item.sampled_at
    return None
