"""Opt-in pacing of order messages under your team's message budgets.

    from qte_sdk.pacing import Budget, Pacer

    budget = Budget(
        sustained_per_minute=sustained,  # your team's values, from the course team
        burst_per_second=burst,
        new_orders_per_minute=new_orders,  # only if your arm has a new-order cap
    )
    session = await open_session(pacing=Pacer(budget))
    await send_new(session, ...)  # call sites do not change

The exchange counts every `new`, `cancel` and `amend` your team sends toward rolling
windows, per team across all its connections, whatever the outcome: **rejected messages
count too**. There is a burst window (one second) and a sustained window (one minute), and
for some arms a new-order window (one minute) that counts only `new`. A message is rejected
(reason codes 1500 to 1502) when its window already holds the cap. There is no penalty
timer: the team is clear again only once enough of its messages have aged out of the
window, so a bot that keeps sending at or above its budget stays rejected for as long as it
keeps sending. `mass_cancel` counts toward no window and is never held here; heartbeats,
`resume`, `account_query`, subscriptions and tickets do not count either.

The exchange does not send the budget values, and they change from term to term, so the
SDK has none built in: pass the values your team was given. docs/developing-your-algo.md
has a dated table of the current term's values. Two bots that share a team share its
budget: give each its share (two bots, half each).

A `Pacer` keeps your sends under `headroom` (80% by default) of each cap:

- It mirrors the exchange: a sliding log of send times per window, not a token bucket. Each
  send is stamped when the write returns, and a stamp leaves a window `guard` seconds
  (50 ms) after the exchange's would, to absorb network jitter. The burst window is also
  smoothed: at most a quarter of its limit in any quarter of it, so a burst of sends is
  spread over the second instead of leaving back to back.
- Only `new`, `cancel` and `amend` are paced. Sends are served first come, first served,
  one at a time: a send waits for room with `asyncio.sleep` (never blocking the event
  loop), or, with `on_limit="raise"`, raises `PacingLimit` at once and sends nothing.
- When a wait will be over a second, it logs `QTE-PACING-HOLDING` once, so a bot that seems
  frozen says why.
- On a budget reject (1500, 1501 or 1502) it holds that window's sends until the whole
  window has passed (about a second for the burst cap, a minute for the others), and logs
  `QTE-PACING-REJECTED` once per hold. Messages already sent extend the hold. A reject
  also suggests the cap may be lower than the values passed, so, once the pacer has
  watched a full window, it lowers that window's limit: to `headroom` of what it counted
  there, but never by more than half in one reject, since the count can miss another
  connection's messages. Each sustained window (a minute) with no budget reject on it
  raises the limit again by a tenth, and by at least a fortieth of the limit from the
  values passed, up to that limit; a send held by a lowered limit is let go as soon as a
  raise makes room, and `limits` is up to date whenever it is read. With values
  that are too high the pacer settles near the real cap, rejected now and then rather than
  all session; a reject another connection caused costs a few minutes at a lower limit,
  never the rest of the session. The warning says what it did, and `limits` shows the
  limits in force.
- Each session it paces starts with the burst window held for one second, since it cannot
  see what the team sent just before. With `on_limit="raise"`, every paced send in that
  second, after each connect and reconnect, raises `PacingLimit` (with a `retry_after` of
  at most a second, unless another window, such as one held after a budget reject, holds
  it longer, when that window's error and `retry_after` are given). The minute windows it
  cannot see at all, so after a budget reject wait a full minute before restarting your
  bot.
- With `count_foreign` (the default), the `accepted` reports of messages your team sent on
  other connections, such as another bot or the web Trade page, count too, at their
  receipt time on the exchange. A resume's replayed reports seed the windows the same way,
  so a `ReconnectingSession` counts what the team sent while it was disconnected. Another
  connection's receipt-stage rejects are sent only to it, and are not seen here.

The pacer learns from events as the session reads them, by iterating it or while
`resume` and `wait_for_calendar` read ahead. A bot that stops reading its session stops
learning, and nothing is read from the connection either until it reads again.

One `Pacer` may pace several sessions in one program, since the budget is the team's; on a
`ReconnectingSession` the same pacer carries over every reconnect, as the exchange's windows
do. For a bare `qte_sdk.connection.Connection`, send through `pacer.wrap(connection)` and
pass each event you read to `pacer.observe`.

Without a pacer, a session logs `QTE-BUDGET-REJECTED` at WARNING, at most once a minute,
when it reads a budget reject. Every warning here is written from a thread of its own, never
from the event loop, and is dropped rather than wait on a stderr pipe that is full.
"""

import asyncio
import logging
import math
import threading
import time
from bisect import bisect_right, insort
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from google.protobuf.message import Message

from qte_sdk import errors as _errors
from qte_sdk import update as _update
from qte_sdk.connection import Event, Received
from qte_sdk.contract.v1.common_pb2 import AMEND, CANCEL, NEW, ReasonCodes
from qte_sdk.contract.v1.order_events_pb2 import Accepted, Reject
from qte_sdk.errors import QteError

__all__ = [
    "DEFAULT_GUARD",
    "DEFAULT_HEADROOM",
    "PACED_TYPES",
    "Budget",
    "Pacer",
    "PacingDraining",
    "PacingLimit",
]

# The message types the exchange counts toward its budget windows, and the only ones paced.
PACED_TYPES = frozenset({"new", "cancel", "amend"})
DEFAULT_HEADROOM = 0.8
DEFAULT_GUARD = 0.05
# A wait at least this long is logged, once per hold.
HOLDING_WARNING_AFTER = 1.0
# The burst window is smoothed over this many equal parts.
_BURST_PARTS = 4
# After a budget reject lowers a limit, it is raised again by this share of itself for each
# sustained window with no budget reject on it, up to its full value.
_RECOVERY_STEP = 0.1
# And by at least this share of its full value.
_RECOVERY_FLOOR = 0.025
# Waits this short count as none: float rounding must not leave a sleep that never ends.
_EPSILON = 1e-6

BURST = "burst"
SUSTAINED = "sustained"
NEW_ORDER = "new-order"

_BUDGET_REASONS = {
    ReasonCodes.MESSAGE_BUDGET_EXCEEDED: SUSTAINED,
    ReasonCodes.BURST_CAP_EXCEEDED: BURST,
    ReasonCodes.NEW_ORDER_CAP_EXCEEDED: NEW_ORDER,
}
_REQUEST_TYPES = {NEW: "new", CANCEL: "cancel", AMEND: "amend"}

_log = logging.getLogger(__name__)

# A write: it returns False when it did not send the message, for the pacer not to count it.
Write = Callable[[str, Message], Awaitable[object]]


def _count(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int")
    if value < 1:
        raise ValueError(f"{name} must be at least 1, got {value}")
    return value


def _seconds(name: str, value: object, *, least: float = 0.0, zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a number of seconds")
    value = float(value)
    if not math.isfinite(value) or value < least or (value == least and not zero):
        bound = f"at least {least}" if zero else f"more than {least}"
        raise ValueError(f"{name} must be {bound} seconds, got {value}")
    return value


@dataclass(frozen=True)
class Budget:
    """Your team's message budgets, as the course team gave them. The exchange sends none,
    so there are no defaults: see docs/developing-your-algo.md for the current term's.

    `sustained_per_minute` is the cap on new, cancel and amend together in any
    `sustained_window` (60 s); `burst_per_second` the cap in any `burst_window` (1 s);
    `new_orders_per_minute`, for an arm that has one, the cap on `new` alone in any
    `sustained_window`. Leave it None otherwise. The windows are term settings too, and
    change only if the course team says so.
    """

    sustained_per_minute: int
    burst_per_second: int
    new_orders_per_minute: int | None = None
    burst_window: float = 1.0
    sustained_window: float = 60.0

    def __post_init__(self) -> None:
        _count("sustained_per_minute", self.sustained_per_minute)
        _count("burst_per_second", self.burst_per_second)
        if self.new_orders_per_minute is not None:
            _count("new_orders_per_minute", self.new_orders_per_minute)
        object.__setattr__(self, "burst_window", _seconds("burst_window", self.burst_window))
        object.__setattr__(
            self, "sustained_window", _seconds("sustained_window", self.sustained_window)
        )


class PacingLimit(QteError, RuntimeError):
    """A paced send found no room and was not sent: with `on_limit="raise"`, or when its
    wait would pass `max_wait`. Code `QTE-PACING-LIMIT`. `limit` is the window that is full
    ("burst", "sustained" or "new-order"), `retry_after` the seconds until it has room, if
    nothing else is sent meanwhile, and `type` the message type."""

    code = _errors.PACING_LIMIT

    def __init__(self, type_: str, limit: str, retry_after: float) -> None:
        self.type = type_
        self.limit = limit
        self.retry_after = retry_after
        super().__init__(
            f"the {limit} budget has no room for this {type_} message for {retry_after:.3f} s",
            fields={"kind": limit, "type": type_, "seconds": round(retry_after, 3)},
        )

    def __reduce__(self) -> Any:
        return (type(self), (self.type, self.limit, self.retry_after))


class PacingDraining(PacingLimit):
    """A `PacingLimit` raised while the pacer holds a window after a budget reject, until
    the whole window has passed. Code `QTE-PACING-DRAINING`."""

    code = _errors.PACING_DRAINING


class _Window:
    """One budget window: a sliding log of stamps, in time order, and its limit.

    A stamp counts until `length + guard` seconds after it, and is kept a whole window
    longer, so a reject read late can still be matched against the window that held its
    message; `forgotten_before` says how far back the log is complete. `limit` None counts
    stamps but checks none (a new-order window the budget gives no cap). `held_until` holds
    every send the window applies to until then, after a budget reject or at a session's
    start."""

    __slots__ = (
        "cap",
        "forgotten_before",
        "full",
        "guard",
        "held_by_reject",
        "held_until",
        "length",
        "limit",
        "lowered_at",
        "name",
        "stamps",
    )

    def __init__(self, name: str, length: float, guard: float, cap: int | None, limit: int | None):
        self.name = name
        self.length = length
        self.guard = guard
        self.cap = cap
        self.limit = limit
        # The limit from the values passed, and when the limit was last lowered or raised
        # again after a budget reject.
        self.full = limit
        self.lowered_at = -math.inf
        self.stamps: deque[float] = deque()
        self.held_until = -math.inf
        self.held_by_reject = False
        self.forgotten_before = -math.inf

    def prune(self, now: float) -> None:
        cutoff = now - 2 * self.length - self.guard
        stamps = self.stamps
        while stamps and stamps[0] <= cutoff:
            stamps.popleft()
        self.forgotten_before = max(self.forgotten_before, cutoff)

    def add(self, stamp: float) -> None:
        stamps = self.stamps
        if not stamps or stamp >= stamps[-1]:
            stamps.append(stamp)
        else:
            insort(stamps, stamp)

    def hold(self, until: float, *, by_reject: bool) -> None:
        if until > self.held_until:
            self.held_until = until
            self.held_by_reject = by_reject

    def wait(self, now: float, recovers_at: float = math.inf) -> tuple[float, bool]:
        """Seconds until this window has room for one more stamp, or until `recovers_at`,
        when a lowered limit is raised again, if that comes first; and whether a hold after
        a budget reject is what decides it. The caller checks again then."""
        self.prune(now)
        wait, by_reject = 0.0, False
        if self.held_until > now:
            wait, by_reject = self.held_until - now, self.held_by_reject
        stamps = self.stamps
        # The stamps that still count: those less than `length + guard` old.
        counting = len(stamps) - bisect_right(stamps, now - self.length - self.guard + _EPSILON)
        if self.limit is not None and counting >= self.limit:
            # Room comes when the stamp that leaves `limit - 1` after it expires.
            frees = stamps[len(stamps) - self.limit] + self.length + self.guard - now
            frees = min(frees, recovers_at - now)
            if frees > wait:
                wait, by_reject = frees, False
        return wait, by_reject

    def count_within(self, end: float) -> int | None:
        """The stamps in the exchange's window ending at `end`, (end - length, end], or None
        when the log no longer holds all of that window."""
        if end - self.length < self.forgotten_before:
            return None
        stamps = self.stamps
        return bisect_right(stamps, end) - bisect_right(stamps, end - self.length)


class _Send:
    """One send of a pacer's, until a report answers it: its stamp, its type, and whether
    the stamp is in the windows yet (it is once the write returns)."""

    __slots__ = ("recorded", "stamp", "type")

    def __init__(self, stamp: float, type_: str) -> None:
        self.stamp = stamp
        self.type = type_
        self.recorded = False


class Pacer:
    """Paces `new`, `cancel` and `amend` under `budget` (see the module).

    `headroom` is the share of each cap to keep under, 0.5 to 0.95 (0.8 by default).
    `on_limit="wait"` (the default) waits for room; `on_limit="raise"` raises
    `PacingLimit` at once, or `PacingDraining` after a budget reject, and sends nothing, for
    a bot that would rather requote than send late. In raise mode, the second after each
    connect or reconnect, while the burst window is held (see the module), every paced send
    raises `PacingLimit`. `max_wait`, if not None, is the longest
    one send waits for room once its turn comes; a longer wait raises as "raise" does.
    Sends are served first come, first served, so a send waits behind those before it too.
    `guard` is how long after the exchange's window a stamp still counts (50 ms).
    `count_foreign` counts the team's messages from other connections (see the module).
    `clock` and `sleep` tell the time and wait; replace them in tests.

    Pass it to `open_session(pacing=...)` or `ReconnectingSession(pacing=...)`, or wrap a
    `Connection` with `wrap`.
    """

    def __init__(
        self,
        budget: Budget,
        *,
        headroom: float = DEFAULT_HEADROOM,
        on_limit: Literal["wait", "raise"] = "wait",
        max_wait: float | None = None,
        guard: float = DEFAULT_GUARD,
        count_foreign: bool = True,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        if not isinstance(budget, Budget):
            raise TypeError("budget must be a qte_sdk.pacing.Budget")
        if isinstance(headroom, bool) or not isinstance(headroom, int | float):
            raise TypeError("headroom must be a number")
        if not 0.5 <= headroom <= 0.95:
            raise ValueError(f"headroom must be from 0.5 to 0.95, got {headroom}")
        if on_limit not in ("wait", "raise"):
            raise ValueError(f'on_limit must be "wait" or "raise", got {on_limit!r}')
        if max_wait is not None:
            max_wait = _seconds("max_wait", max_wait, zero=True)
        guard = _seconds("guard", guard, zero=True)
        if not isinstance(count_foreign, bool):
            raise TypeError("count_foreign must be True or False")
        self.budget = budget
        self.headroom = float(headroom)
        self.on_limit = on_limit
        self.max_wait = max_wait
        self.guard = guard
        self.count_foreign = count_foreign
        self._clock = clock
        self._sleep = sleep
        burst = self._limit(budget.burst_per_second)
        self._burst = _Window(BURST, budget.burst_window, guard, budget.burst_per_second, burst)
        # The smoothing part has no guard: it shapes this program's sends, and mirrors no
        # window of the exchange's.
        self._smooth = _Window(
            BURST, budget.burst_window / _BURST_PARTS, 0.0, None, self._part(burst)
        )
        self._sustained = _Window(
            SUSTAINED,
            budget.sustained_window,
            guard,
            budget.sustained_per_minute,
            self._limit(budget.sustained_per_minute),
        )
        new_orders = budget.new_orders_per_minute
        self._new_order = _Window(
            NEW_ORDER,
            budget.sustained_window,
            guard,
            new_orders,
            None if new_orders is None else self._limit(new_orders),
        )
        self._by_name = {BURST: self._burst, SUSTAINED: self._sustained, NEW_ORDER: self._new_order}
        # The windows every paced type waits for; a `new` waits for the new-order window
        # first, on its own.
        self._shared = (self._burst, self._smooth, self._sustained)
        # One send at a time, first come, first served. A `new` first waits its turn among
        # news for the new-order window alone, so news held by their own cap never hold up
        # a cancel or an amend.
        self._lock = asyncio.Lock()
        self._new_lock = asyncio.Lock()
        # This pacer's sends that no report has answered yet, by request_ref, oldest first;
        # and all of them in the order sent, to forget those never answered.
        self._own: dict[str, deque[_Send]] = {}
        self._own_order: deque[tuple[_Send, str]] = deque()
        # The reports already seen (see `_observe`), with when.
        self._seen: OrderedDict[tuple[object, ...], float] = OrderedDict()
        self._last_stamp = -math.inf
        # Since when this pacer has seen everything this program sent: a reject teaches the
        # cap only once it has watched a whole window.
        self._watching_since = clock()
        self._holding_until = -math.inf

    def __repr__(self) -> str:
        return f"Pacer({self.budget!r}, headroom={self.headroom}, on_limit={self.on_limit!r})"

    def _recover(self, now: float) -> None:
        """Raise each lowered limit again by one step (see `_step`) for each whole sustained
        window with no budget reject on it, up to its full value."""
        clean = self.budget.sustained_window
        for window in (self._burst, self._sustained, self._new_order):
            full, limit = window.full, window.limit
            if full is None or limit is None or limit >= full:
                continue
            # Due a hair early, as the waits are, so a wait that ends at the deadline
            # finds the limit raised.
            steps = math.floor((now - window.lowered_at + _EPSILON) / clean)
            for _ in range(min(steps, 100)):
                limit = min(full, limit + self._step(limit, full))
            if steps > 0:
                window.limit = limit
                window.lowered_at += steps * clean
        self._smooth.limit = self._part(self._burst.limit or 1)

    @staticmethod
    def _step(limit: int, full: int) -> int:
        """One clean window's raise: a tenth of the limit, and at least a fortieth of the
        full limit, so a deep cut does not take long to recover."""
        return max(1, math.ceil(limit * _RECOVERY_STEP), math.ceil(full * _RECOVERY_FLOOR))

    def _recovers_at(self, window: _Window) -> float:
        """When `window`'s lowered limit is next raised, or never (infinity)."""
        source = self._burst if window is self._smooth else window
        if source.full is None or source.limit is None or source.limit >= source.full:
            return math.inf
        return source.lowered_at + self.budget.sustained_window

    def _limit(self, cap: int) -> int:
        return max(1, math.floor(cap * self.headroom))

    @staticmethod
    def _part(burst_limit: int) -> int:
        return max(1, burst_limit // _BURST_PARTS)

    @property
    def limits(self) -> dict[str, int | None]:
        """The most each window lets this pacer send: `headroom` of each cap, or less once
        a budget reject has shown the cap is lower, raised again as clean minutes pass.
        "new-order" is None without a cap."""
        self._recover(self._clock())
        return {name: window.limit for name, window in self._by_name.items()}

    def wrap(self, sender: Any) -> "PacedSender":
        """A `qte_sdk.orders.Sender` that paces each send through this pacer, then sends it
        on `sender`, such as a `Connection`. Pass every event read from it to `observe`."""
        return PacedSender(self, sender)

    def observe(self, event: Event) -> None:
        """Learn from one event read from the exchange: a budget reject holds and may lower
        a window, and another connection's report counts. A session with this pacer calls
        it for every event it reads; call it yourself only for a wrapped `Connection`. A
        report is counted at the moment it is observed."""
        self._observe(event, None)

    # Used by Session and ReconnectingSession.

    def _session_started(self) -> None:
        """A session opened: what the team sent just before is unseen, so the burst window
        is held for one window, and the cap is not learned until a whole window is seen."""
        now = self._clock()
        self._watching_since = now
        self._burst.hold(now + self._burst.length, by_reject=False)

    async def _send(self, type_: str, payload: Message, write: Write) -> bool | None:
        """Pace and send one message through `write`. Returns False when `write` did not
        send it (see `Write`)."""
        if type_ not in PACED_TYPES:
            # Not paced, and perhaps `auth`, which holds the token: no copy of it stays in
            # this frame when the write fails.
            failure: BaseException
            try:
                await write(type_, payload)
            except BaseException as error:
                failure = error
            else:
                return True
            del payload
            raise failure
        ref = getattr(payload, "request_ref", None) or None
        start = self._clock()
        if type_ != "new":
            return await self._send_in_turn(type_, payload, write, ref, start, None)
        async with self._new_lock:
            while True:
                await self._wait_for_room((self._new_order,), type_, start)
                sent = await self._send_in_turn(type_, payload, write, ref, start, self._new_order)
                if sent is not None:
                    return sent

    async def _send_in_turn(
        self,
        type_: str,
        payload: Message,
        write: Write,
        ref: str | None,
        start: float,
        again: _Window | None,
    ) -> bool | None:
        """Wait for the shared windows, then write. Returns whether the message was
        written (False when `write` returned False, as for a session that dropped), or None
        when `again`, the new-order window, filled meanwhile: it is waited for without
        holding up every cancel and amend behind this `new`."""
        async with self._lock:
            await self._wait_for_room(self._shared, type_, start)
            if again is not None and again.wait(self._clock())[0] > _EPSILON:
                return None
            # Before the write, so a report read while it is in progress is known as ours.
            # No two sends share a stamp, so a reject can be placed among them.
            entry = None if ref is None else self._sent(ref, type_, self._next_stamp())
            written = True
            try:
                written = await write(type_, payload) is not False
            finally:
                # Stamped when the write returns, or fails: it may have been sent.
                if written:
                    stamp = self._next_stamp()
                    self._record(type_, stamp)
                    if entry is not None:
                        entry.stamp, entry.recorded = stamp, True
                elif entry is not None and ref is not None:
                    self._unsend(ref, entry)
            return written

    def _next_stamp(self) -> float:
        stamp = max(self._clock(), math.nextafter(self._last_stamp, math.inf))
        self._last_stamp = stamp
        return stamp

    def _windows(self, type_: str) -> tuple[_Window, ...]:
        if type_ == "new":
            return (self._burst, self._smooth, self._sustained, self._new_order)
        return (self._burst, self._smooth, self._sustained)

    def _blocked(self, windows: tuple[_Window, ...], now: float) -> tuple[float, _Window, bool]:
        longest, which, by_reject = 0.0, windows[0], False
        for window in windows:
            wait, rejected = window.wait(now, self._recovers_at(window))
            if wait > longest:
                longest, which, by_reject = wait, window, rejected
        return longest, which, by_reject

    async def _wait_for_room(self, windows: tuple[_Window, ...], type_: str, start: float) -> None:
        while True:
            # Checked again after every sleep: a timer may wake early or late, and a reject
            # or another connection's report may have arrived meanwhile.
            now = self._clock()
            self._recover(now)
            wait, window, by_reject = self._blocked(windows, now)
            if wait <= _EPSILON:
                return
            if self.on_limit == "raise" or (
                self.max_wait is not None and now - start + wait > self.max_wait
            ):
                error = PacingDraining if by_reject else PacingLimit
                raise error(type_, window.name, wait)
            # A hold after a reject was logged with the reject.
            if not by_reject and wait >= HOLDING_WARNING_AFTER and now >= self._holding_until:
                self._holding_until = now + wait
                _warn(
                    _errors.PACING_HOLDING,
                    type=_held_types(window.name),
                    seconds=round(wait, 1),
                    kind=window.name,
                )
            await self._sleep(wait)

    def _record(self, type_: str, stamp: float) -> None:
        for window in self._windows(type_):
            window.add(stamp)

    def _sent(self, ref: str, type_: str, stamp: float) -> "_Send":
        """Note a send of this pacer's, before its write, so a report that answers it is not
        counted again. Returns its entry, which the write's end stamps."""
        entry = _Send(stamp, type_)
        self._own.setdefault(ref, deque()).append(entry)
        self._own_order.append((entry, ref))
        self._forget(stamp)
        return entry

    def _unsend(self, ref: str, entry: "_Send") -> None:
        """Forget a send that was never written."""
        waiting = self._own.get(ref)
        if waiting and entry in waiting:
            waiting.remove(entry)
            if not waiting:
                del self._own[ref]

    def _claim(self, ref: str, kind: str | None) -> "_Send | None":
        """The oldest send of this pacer's with `ref`, of type `kind` (any, if None), that
        no report has answered, now answered; or None if there is none."""
        waiting = self._own.get(ref)
        if not waiting:
            return None
        entry = next((e for e in waiting if kind is None or e.type == kind), None)
        if entry is None:
            return None
        waiting.remove(entry)
        if not waiting:
            del self._own[ref]
        return entry

    def _forget(self, now: float) -> None:
        """Forget sends and reports older than two sustained windows: a report that comes
        later is counted as another connection's, which only makes the pacer more careful."""
        horizon = now - 2 * self.budget.sustained_window - self.guard
        order = self._own_order
        while order and order[0][0].stamp < horizon:
            entry, ref = order.popleft()
            self._unsend(ref, entry)
        seen = self._seen
        while seen:
            first = next(iter(seen))
            if seen[first] >= horizon:
                break
            del seen[first]

    def _observe(self, event: Event, anchor: tuple[float, int] | None) -> None:
        """`observe`, with the session's anchor: the local time it was acknowledged and the
        exchange's `server_time` then, to place a report at its receipt time."""
        if not isinstance(event, Received):
            return
        message = event.message
        if isinstance(message, Reject):
            reason = _BUDGET_REASONS.get(message.reason_code)
            request_type = message.request_type if message.HasField("request_type") else None
            ref = message.request_ref if message.HasField("request_ref") else None
        elif isinstance(message, Accepted):
            reason, request_type, ref = None, message.request_type, message.request_ref
        else:
            return
        now = self._clock()
        stamp = _exchange_stamp(message.receipt_time, anchor, now)
        kind = _REQUEST_TYPES.get(request_type) if request_type is not None else None
        # The same report reaches every connection of the team, and a resume can replay
        # it: a report's number names it, with its receipt time, since numbers start again
        # each term; one without (a reject sent at receipt, to one connection only) is named
        # by what it says.
        if event.report_seq is not None:
            key: tuple[object, ...] = ("report", event.report_seq, message.receipt_time)
        else:
            reason_code = message.reason_code if isinstance(message, Reject) else None
            key = (event.type, ref, request_type, reason_code, message.receipt_time)
        self._forget(now)
        if key in self._seen:
            return
        self._seen[key] = now
        mine = None
        if ref and (kind is not None or request_type is None):
            mine = self._claim(ref, kind)
        if reason is not None:
            window = self._by_name[reason]
            # A reject older than its window (a replay, say) says nothing about now.
            if stamp + window.length + window.guard > now:
                self._on_budget_reject(window, message.reason_code, now, mine)
        if mine is not None:
            return  # this pacer's own message, counted when it was sent
        if kind is None or not self.count_foreign:
            return
        if stamp + self._sustained.length + self.guard <= now:
            return  # older than every window
        for window in self._windows(kind):
            if window is not self._smooth and stamp + window.length + window.guard > now:
                window.add(stamp)

    def _on_budget_reject(
        self, window: _Window, reason_code: int, now: float, mine: "_Send | None"
    ) -> None:
        fresh = not (window.held_by_reject and window.held_until > now)
        learned = None
        # The exchange held at least the cap in its window when the rejected message
        # arrived: counted up to that message's own stamp when it is this pacer's, and up
        # to now otherwise (later sends, still in flight, would count too).
        if mine is None:
            end, itself = now, 0
        else:
            # A reject read while its write was still in progress: not yet in the log.
            end, itself = mine.stamp, (1 if mine.recorded else 0)
        within = window.count_within(end)
        self._recover(now)
        if (
            within is not None
            and window.full is not None
            and window.limit is not None
            and end - self._watching_since >= window.length
        ):
            # The count can miss another connection's messages still in flight, its rejects
            # and its late reports, so one reject never lowers a limit by more than half;
            # but never below what the count shows, either. It is raised again after each
            # clean sustained window (see `_recover`).
            count = within - itself
            lowered = max(math.ceil(window.limit / 2), self._limit(count) if count > 0 else 1)
            if lowered < window.limit:
                window.limit = lowered
                if window is self._burst:
                    self._smooth.limit = self._part(lowered)
                learned = (count, lowered)
        if window.full is not None and window.limit is not None and window.limit < window.full:
            # Any budget reject on a lowered window starts its clean minute again, whether or
            # not this one could be counted.
            window.lowered_at = now
        hold = window.length + window.guard
        window.hold(now + hold, by_reject=True)
        if not (fresh or learned):
            return
        if learned is not None:
            count, inferred = learned
            passed = str(window.cap)
            why = (
                f"Your {window.name} budget may be lower than the {passed} you passed (the "
                f"pacer counted {count} in the window), so it now keeps under {inferred} per "
                f"{_per(window.length)}, and raises that again by "
                f"a tenth (at least {self._step(1, window.full or 1)}) after each "
                f"{_per(self.budget.sustained_window)} with no budget reject"
            )
        else:
            why = (
                "Rejected messages count toward the window too, so the pacer waits for the "
                "whole window to pass before it sends them again"
            )
        _warn(
            _errors.PACING_REJECTED,
            kind=window.name,
            reason=_reason_name(reason_code),
            type=_held_types(window.name),
            seconds=round(hold, 2),
            why=why,
        )


def _checked(pacing: object) -> "Pacer | None":
    """`pacing` if it is a `Pacer` or None; a `TypeError` otherwise, before any connection."""
    if pacing is not None and not isinstance(pacing, Pacer):
        raise TypeError("pacing must be a qte_sdk.pacing.Pacer or None")
    return pacing


class PacedSender:
    """A `qte_sdk.orders.Sender` that paces each send through a `Pacer` (see `Pacer.wrap`)."""

    def __init__(self, pacer: Pacer, sender: Any) -> None:
        self.pacer = pacer
        self.sender = sender

    async def send(self, type_: str, payload: Message) -> None:
        failure: BaseException
        try:
            await self.pacer._send(type_, payload, self.sender.send)
        except BaseException as error:
            failure = error
        else:
            return
        # The message may be `auth`, which holds the token: no copy stays in this frame.
        del payload
        raise failure


def _exchange_stamp(receipt_time: int, anchor: tuple[float, int] | None, now: float) -> float:
    """A report's receipt time on this program's clock: the session's local anchor plus how
    long after the anchor's `server_time` the exchange received it (both in exchange
    milliseconds, never compared with local wall time). Never later than now, and now when
    either time is missing. The anchor is read a little after `server_time`, so a report is
    placed a little late, which only makes the pacer more careful."""
    if anchor is None or receipt_time <= 0 or anchor[1] <= 0:
        return now
    local, server_time = anchor
    return min(local + (receipt_time - server_time) / 1000.0, now)


def _held_types(window: str) -> str:
    return "new orders" if window == NEW_ORDER else "new, cancel and amend messages"


def _per(length: float) -> str:
    if length == 1.0:
        return "second"
    if length == 60.0:
        return "minute"
    return f"{length:g} s"


def _reason_name(code: int) -> str:
    try:
        return ReasonCodes.ReasonCode.Name(code)
    except ValueError:
        return str(code)


# The warning a session without a pacer logs on a budget reject, at most once a minute in a
# program.
BUDGET_WARNING_INTERVAL = 60.0
_budget_warning_lock = threading.Lock()
# The clock of that warning, and of a session's anchor without a pacer; replaced in tests.
_monotonic = time.monotonic
_budget_warned_at: float | None = None


def _budget_rejected(event: Event, anchor: tuple[float, int] | None) -> None:
    """For a session without a pacer: log `QTE-BUDGET-REJECTED` for a budget reject, unless
    one was logged in the last minute or the reject is older than a minute."""
    global _budget_warned_at
    if not isinstance(event, Received) or not isinstance(event.message, Reject):
        return
    message = event.message
    if message.reason_code not in _BUDGET_REASONS:
        return
    now = _monotonic()
    if _exchange_stamp(message.receipt_time, anchor, now) + BUDGET_WARNING_INTERVAL <= now:
        return
    with _budget_warning_lock:
        if _budget_warned_at is not None and now - _budget_warned_at < BUDGET_WARNING_INTERVAL:
            return
        _budget_warned_at = now
    _warn(_errors.BUDGET_REJECTED, reason=_reason_name(message.reason_code))


def _warn(code: str, **fields: Any) -> None:
    """Log `code`'s message at WARNING through this module's logger, with `code` on the
    record. Whether the logger is enabled is asked in the warning's thread too: asking can
    wait for logging's lock."""
    _emit(_errors.render(code, **fields), code)


def _emit(message: str, code: str) -> None:
    """Write the warning from a thread of its own, never the event loop's: a write to a
    full stderr pipe would stop the loop, and with it market data and orders. It is
    dropped there rather than wait (see `qte_sdk.update._log_without_waiting`)."""
    threading.Thread(
        target=_write, args=(message, code), name="qte-sdk pacing warning", daemon=True
    ).start()


def _write(message: str, code: str) -> None:
    try:
        _update._log_without_waiting(logging.WARNING, message, code, _log)
    except BaseException:
        # Never into the program: a thread's uncaught error would be printed.
        return
