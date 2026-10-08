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
  also proves the cap is at most what the pacer counted in that window, so, once the pacer
  has watched a full window, it lowers that window's limit to `headroom` of the count for
  the rest of its life, and the warning says so: with wrong values the pacer settles under
  the real cap after a drain or two instead of being rejected all session. `limits` shows
  the limits in force.
- Each session it paces starts with the burst window held for one second, since it cannot
  see what the team sent just before. The minute windows it cannot see at all, so after a
  budget reject wait a full minute before restarting your bot.
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
from bisect import insort
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

Write = Callable[[str, Message], Awaitable[None]]


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

    A stamp counts until `length + guard` seconds after it. `limit` None counts stamps but
    checks none (a new-order window the budget gives no cap). `held_until` holds every send
    the window applies to until then, after a budget reject or at a session's start."""

    __slots__ = (
        "cap",
        "guard",
        "held_by_reject",
        "held_until",
        "length",
        "limit",
        "name",
        "stamps",
    )

    def __init__(self, name: str, length: float, guard: float, cap: int | None, limit: int | None):
        self.name = name
        self.length = length
        self.guard = guard
        self.cap = cap
        self.limit = limit
        self.stamps: deque[float] = deque()
        self.held_until = -math.inf
        self.held_by_reject = False

    def prune(self, now: float) -> None:
        stamps = self.stamps
        span = self.length + self.guard
        while stamps and stamps[0] + span <= now + _EPSILON:
            stamps.popleft()

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

    def wait(self, now: float) -> tuple[float, bool]:
        """Seconds until this window has room for one more stamp, and whether a hold after
        a budget reject is what decides it."""
        self.prune(now)
        wait, by_reject = 0.0, False
        if self.held_until > now:
            wait, by_reject = self.held_until - now, self.held_by_reject
        if self.limit is not None and len(self.stamps) >= self.limit:
            # Room comes when the stamp that leaves `limit - 1` after it expires.
            frees = self.stamps[len(self.stamps) - self.limit] + self.length + self.guard - now
            if frees > wait:
                wait, by_reject = frees, False
        return wait, by_reject

    def count_within(self, now: float) -> int:
        """The stamps in the exchange's window ending at `now`, (now - length, now]."""
        start = now - self.length
        return sum(1 for stamp in self.stamps if start < stamp <= now)


class Pacer:
    """Paces `new`, `cancel` and `amend` under `budget` (see the module).

    `headroom` is the share of each cap to keep under, 0.5 to 0.95 (0.8 by default).
    `on_limit="wait"` (the default) waits for room; `on_limit="raise"` raises
    `PacingLimit` at once, or `PacingDraining` after a budget reject, and sends nothing, for
    a bot that would rather requote than send late. `max_wait`, if not None, is the longest
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
        # One send at a time, first come, first served. A `new` first waits its turn among
        # news for the new-order window alone, so news held by their own cap never hold up
        # a cancel or an amend.
        self._lock = asyncio.Lock()
        self._new_lock = asyncio.Lock()
        # request_refs this pacer sent, and (request_ref, type) of foreign reports counted,
        # with when, so a report is never counted twice.
        self._known: OrderedDict[object, float] = OrderedDict()
        # Since when this pacer has seen everything this program sent: a reject teaches the
        # cap only once it has watched a whole window.
        self._watching_since = clock()
        self._holding_until = -math.inf

    def __repr__(self) -> str:
        return f"Pacer({self.budget!r}, headroom={self.headroom}, on_limit={self.on_limit!r})"

    def _limit(self, cap: int) -> int:
        return max(1, math.floor(cap * self.headroom))

    @staticmethod
    def _part(burst_limit: int) -> int:
        return max(1, burst_limit // _BURST_PARTS)

    @property
    def limits(self) -> dict[str, int | None]:
        """The most each window lets this pacer send: `headroom` of each cap, or less once
        a budget reject has shown the cap is lower. "new-order" is None without a cap."""
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

    async def _send(self, type_: str, payload: Message, write: Write) -> None:
        if type_ not in PACED_TYPES:
            await write(type_, payload)
            return
        ref = getattr(payload, "request_ref", None) or None
        start = self._clock()
        if type_ == "new":
            async with self._new_lock:
                await self._wait_for_room((self._new_order,), type_, start)
                await self._send_in_turn(type_, payload, write, ref, start)
        else:
            await self._send_in_turn(type_, payload, write, ref, start)

    async def _send_in_turn(
        self, type_: str, payload: Message, write: Write, ref: str | None, start: float
    ) -> None:
        async with self._lock:
            await self._wait_for_room(self._windows(type_), type_, start)
            if ref is not None:
                self._remember(ref, self._clock())
            try:
                await write(type_, payload)
            finally:
                # Stamped when the write returns, or fails: it may have been sent.
                stamp = self._clock()
                self._record(type_, stamp)
                if ref is not None:
                    self._remember(ref, stamp)

    def _windows(self, type_: str) -> tuple[_Window, ...]:
        if type_ == "new":
            return (self._burst, self._smooth, self._sustained, self._new_order)
        return (self._burst, self._smooth, self._sustained)

    @staticmethod
    def _blocked(windows: tuple[_Window, ...], now: float) -> tuple[float, _Window, bool]:
        longest, which, by_reject = 0.0, windows[0], False
        for window in windows:
            wait, rejected = window.wait(now)
            if wait > longest:
                longest, which, by_reject = wait, window, rejected
        return longest, which, by_reject

    async def _wait_for_room(self, windows: tuple[_Window, ...], type_: str, start: float) -> None:
        while True:
            # Checked again after every sleep: a timer may wake early or late, and a reject
            # or another connection's report may have arrived meanwhile.
            now = self._clock()
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

    def _remember(self, key: object, now: float) -> None:
        known = self._known
        known[key] = now
        known.move_to_end(key)
        horizon = 2 * self.budget.sustained_window + self.guard
        while known:
            first = next(iter(known))
            if known[first] + horizon >= now:
                break
            del known[first]

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
        if reason is not None:
            window = self._by_name[reason]
            # A reject older than its window (a replay, say) says nothing about now.
            if stamp + window.length + window.guard > now:
                self._on_budget_reject(window, message.reason_code, now, ref or None)
        kind = _REQUEST_TYPES.get(request_type) if request_type is not None else None
        if kind is None or not self.count_foreign:
            return
        ref = ref or None
        key = None if ref is None else (ref, event.type)
        if ref is not None and (ref in self._known or key in self._known):
            return  # this pacer's own message, or a report already counted
        if stamp + self._sustained.length + self.guard <= now:
            return  # older than every window
        if key is not None:
            self._remember(key, now)
        for window in self._windows(kind):
            if window is not self._smooth and stamp + window.length + window.guard > now:
                window.add(stamp)

    def _on_budget_reject(
        self, window: _Window, reason_code: int, now: float, ref: str | None
    ) -> None:
        fresh = not (window.held_by_reject and window.held_until > now)
        learned = None
        # The exchange held at least the cap in its window when the rejected message
        # arrived: counted up to that message's own stamp when it is this pacer's, and up
        # to now otherwise (later sends, still in flight, would count too).
        sent = self._known.get(ref) if ref is not None else None
        end, itself = (sent, 1) if sent is not None else (now, 0)
        if end - self._watching_since >= window.length:
            count = window.count_within(end) - itself
            inferred = self._limit(count) if count > 0 else None
            if inferred is not None and (window.limit is None or inferred < window.limit):
                window.limit = inferred
                if window is self._burst:
                    self._smooth.limit = self._part(inferred)
                learned = (count, inferred)
        hold = window.length + window.guard
        window.hold(now + hold, by_reject=True)
        if not (fresh or learned):
            return
        if learned is not None:
            count, inferred = learned
            passed = "none" if window.cap is None else str(window.cap)
            why = (
                f"Your {window.name} budget looks like at most {count} per "
                f"{_per(window.length)}, not the {passed} you passed, so the pacer now keeps "
                f"under {inferred}"
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
        await self.pacer._send(type_, payload, self.sender.send)


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
    record."""
    if _log.disabled or not _log.isEnabledFor(logging.WARNING):
        return
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
