"""Opt-in pacing under the message budgets (qte_sdk.pacing), on a fake clock.

Every test but the reconnect one runs on `Sim`, a fake clock whose sleep moves time on and
runs what falls due meanwhile: the exchange receiving a message, or its reject arriving.
`Exchange` models the platform's budget check (engine/sequencer/budget.go at qte-platform
51809de9): per team, a sliding log of receipt stamps per window, the window ending at a
stamp half-open (ts - W, ts], a message rejected when the window already holds the cap
(burst, then sustained, then new-order), and every new, cancel and amend recorded whatever
its outcome.
"""

import asyncio
import heapq
import json
import logging
import os
import random
import threading
from collections import deque
from collections.abc import Callable

import pytest
from fake_exchange import drop_connection, frame, serve_local
from test_session import CONTRACT_VERSION, synthetic_token
from websockets.asyncio.server import ServerConnection

from qte_sdk import errors, pacing
from qte_sdk.connection import Connection, Received, ResumeComplete, SessionInfo
from qte_sdk.contract.v1.common_pb2 import AMEND, CANCEL, NEW, ReasonCodes
from qte_sdk.contract.v1.order_entry_pb2 import CancelOrder, MassCancel, NewOrder
from qte_sdk.contract.v1.order_events_pb2 import Accepted, Reject
from qte_sdk.orders import send_cancel, send_mass_cancel, send_new
from qte_sdk.pacing import Budget, Pacer, PacingDraining, PacingLimit
from qte_sdk.reconnect import Connected, ReconnectingSession
from qte_sdk.session import Session

SUSTAINED = ReasonCodes.MESSAGE_BUDGET_EXCEEDED
BURST = ReasonCodes.BURST_CAP_EXCEEDED
NEW_ORDERS = ReasonCodes.NEW_ORDER_CAP_EXCEEDED
REQUEST_TYPES = {"new": NEW, "cancel": CANCEL, "amend": AMEND}
# Caps for the tests, not any term's values: the SDK has none.
ONE_X = Budget(sustained_per_minute=1200, burst_per_second=200)
WIDE = Budget(sustained_per_minute=1_000_000, burst_per_second=200)
GUARD = pacing.DEFAULT_GUARD


class Sim:
    """A fake clock and scheduler. `sleep` waits until the clock reaches its wake time; the
    clock moves on only when every task is waiting, straight to the next scheduled call (a
    wake, the exchange receiving a message, or its answer arriving), as a real loop would
    with no time wasted. `late` makes each sleep wake up to that much late, and `early` up
    to that much early (never more than half the delay), as a coarse timer may."""

    def __init__(self, *, late: float = 0.0, early: float = 0.0, seed: int = 1) -> None:
        self.now = 0.0
        self.late = late
        self.early = early
        self.rng = random.Random(seed)
        self.sleeps: list[float] = []
        self._due: list[tuple[float, int, Callable[[], None]]] = []
        self._n = 0
        self._driver: asyncio.Task | None = None

    def clock(self) -> float:
        return self.now

    def at(self, when: float, call: Callable[[], None]) -> None:
        self._n += 1
        heapq.heappush(self._due, (when, self._n, call))

    def run_until(self, until: float) -> None:
        while self._due and self._due[0][0] <= until:
            self._next()
        self.now = max(self.now, until)

    def _next(self) -> None:
        when, _, call = heapq.heappop(self._due)
        self.now = max(self.now, when)
        call()

    async def sleep(self, delay: float) -> None:
        delay = max(delay, 0.0)
        self.sleeps.append(delay)
        if self.late:
            delay += self.rng.uniform(0, self.late)
        if self.early:
            delay -= self.rng.uniform(0, min(self.early, delay / 2))
        woken = asyncio.get_running_loop().create_future()
        self.at(self.now + delay, lambda: woken.done() or woken.set_result(None))
        if self._driver is None or self._driver.done():
            self._driver = asyncio.ensure_future(self._drive())
        await woken

    async def _drive(self) -> None:
        # The loop's queue of ready callbacks: empty when every task is waiting.
        loop = asyncio.get_running_loop()
        while self._due:
            await asyncio.sleep(0)
            if not loop._ready:  # type: ignore[attr-defined]
                self._next()


class Exchange:
    """The platform's budget check for one team (see the module docstring)."""

    def __init__(
        self,
        sustained: int,
        burst: int,
        new_orders: int | None = None,
        burst_window: float = 1.0,
        sustained_window: float = 60.0,
    ) -> None:
        self.caps = {"burst": burst, "sustained": sustained, "new-order": new_orders}
        self.lengths = {
            "burst": burst_window,
            "sustained": sustained_window,
            "new-order": sustained_window,
        }
        self.logs: dict[str, deque[float]] = {name: deque() for name in self.caps}
        self.rejects: list[tuple[float, int]] = []

    def receive(self, ts: float, type_: str) -> int | None:
        if type_ not in ("new", "cancel", "amend"):
            return None  # mass_cancel counts toward no window
        for name, log in self.logs.items():
            while log and log[0] <= ts - self.lengths[name]:
                log.popleft()
        reason = None
        if len(self.logs["burst"]) >= self.caps["burst"]:
            reason = BURST
        elif len(self.logs["sustained"]) >= self.caps["sustained"]:
            reason = SUSTAINED
        elif (
            type_ == "new"
            and self.caps["new-order"] is not None
            and len(self.logs["new-order"]) >= self.caps["new-order"]
        ):
            reason = NEW_ORDERS
        self.logs["burst"].append(ts)
        self.logs["sustained"].append(ts)
        if type_ == "new":
            self.logs["new-order"].append(ts)
        if reason is not None:
            self.rejects.append((ts, reason))
        return reason


class Wire:
    """A `Sender` on the fake clock: records each write, delivers it to `exchange` after
    `latency` plus `jitter()` seconds, and the exchange's answer to `pacer.observe` after
    `latency` more. `write_time` makes each write take that long, as backpressure does."""

    def __init__(
        self,
        sim: Sim,
        pacer: Pacer | None = None,
        exchange: Exchange | None = None,
        *,
        latency: float = 0.02,
        jitter: Callable[[], float] = lambda: 0.0,
        write_time: float = 0.0,
    ) -> None:
        self.sim = sim
        self.pacer = pacer
        self.exchange = exchange
        self.latency = latency
        self.jitter = jitter
        self.write_time = write_time
        self.sent: list[tuple[float, str, str]] = []
        self.started: list[float] = []

    async def send(self, type_: str, payload) -> None:
        self.started.append(self.sim.now)
        if self.write_time:
            await self.sim.sleep(self.write_time)
        ref = getattr(payload, "request_ref", "")
        self.sent.append((self.sim.now, type_, ref))
        if self.exchange is not None:
            receipt = self.sim.now + self.latency + self.jitter()
            self.sim.at(receipt, lambda: self._receive(receipt, type_, ref))

    def _receive(self, receipt: float, type_: str, ref: str) -> None:
        assert self.exchange is not None
        reason = self.exchange.receive(receipt, type_)
        if self.pacer is None or type_ not in REQUEST_TYPES:
            return
        if reason is None:
            event = accepted(ref, type_, receipt)
        else:
            event = reject(reason, ref, type_, receipt)
        self.sim.at(receipt + self.latency, lambda: self.pacer.observe(event))

    def times(self, type_: str | None = None) -> list[float]:
        return [t for t, kind, _ in self.sent if type_ is None or kind == type_]


def accepted(ref: str, type_: str = "new", receipt: float = 0.0) -> Received:
    message = Accepted(
        request_ref=ref, request_type=REQUEST_TYPES[type_], receipt_time=int(receipt * 1000)
    )
    return Received("accepted", message, None)


def reject(reason: int, ref: str = "r", type_: str = "new", receipt: float = 0.0) -> Received:
    message = Reject(
        request_ref=ref,
        request_type=REQUEST_TYPES[type_],
        reason_code=reason,
        receipt_time=int(receipt * 1000),
    )
    return Received("reject", message, None)


def most_in_window(times: list[float], length: float) -> int:
    """The most of `times` in any half-open window of `length`, give or take the pacer's
    microsecond of rounding."""
    times = sorted(times)
    most, start = 0, 0
    for end, t in enumerate(times):
        while times[start] <= t - length + 2e-6:
            start += 1
        most = max(most, end - start + 1)
    return most


@pytest.fixture
def warned(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """The pacing warnings logged, as (code, message), caught before their thread."""
    caught: list[tuple[str, str]] = []
    monkeypatch.setattr(pacing, "_emit", lambda message, code: caught.append((code, message)))
    monkeypatch.setattr(pacing, "_budget_warned_at", None)
    return caught


def codes(warned: list[tuple[str, str]]) -> list[str]:
    return [code for code, _ in warned]


async def blast(sender, count: int, type_: str = "new", prefix: str = "r") -> None:
    for n in range(count):
        await send_one(sender, type_, f"{prefix}{n}")


async def send_one(sender, type_: str, ref: str) -> None:
    if type_ == "new":
        await sender.send("new", NewOrder(request_ref=ref))
    elif type_ == "cancel":
        await sender.send("cancel", CancelOrder(request_ref=ref))
    else:
        await sender.send(type_, MassCancel(request_ref=ref))


# The budget and the pacer's arguments


def test_budget_values_are_checked():
    with pytest.raises(TypeError):
        Budget(sustained_per_minute=1.5, burst_per_second=10)
    with pytest.raises(TypeError):
        Budget(sustained_per_minute=True, burst_per_second=10)
    with pytest.raises(ValueError):
        Budget(sustained_per_minute=0, burst_per_second=10)
    with pytest.raises(ValueError):
        Budget(sustained_per_minute=10, burst_per_second=10, new_orders_per_minute=-1)
    with pytest.raises(ValueError):
        Budget(sustained_per_minute=10, burst_per_second=10, burst_window=0)
    with pytest.raises(TypeError):
        Budget(sustained_per_minute=10, burst_per_second=10, sustained_window="60")


def test_pacer_arguments_are_checked():
    with pytest.raises(ValueError):
        Pacer(ONE_X, headroom=0.96)
    with pytest.raises(ValueError):
        Pacer(ONE_X, headroom=0.49)
    with pytest.raises(ValueError):
        Pacer(ONE_X, on_limit="drop")
    with pytest.raises(ValueError):
        Pacer(ONE_X, max_wait=-1)
    with pytest.raises(TypeError):
        Pacer({"burst": 200})


def test_limits_keep_headroom_under_each_cap():
    budget = Budget(sustained_per_minute=1200, burst_per_second=200, new_orders_per_minute=120)
    assert Pacer(budget).limits == {"burst": 160, "sustained": 960, "new-order": 96}
    assert Pacer(ONE_X, headroom=0.9).limits == {"burst": 180, "sustained": 1080, "new-order": None}
    assert Pacer(Budget(sustained_per_minute=1, burst_per_second=1)).limits["burst"] == 1


async def test_a_session_rejects_a_pacing_that_is_not_a_pacer():
    from qte_sdk.session import open_session

    with pytest.raises(TypeError):
        await open_session("ws://127.0.0.1:9", synthetic_token(), pacing=object())
    with pytest.raises(TypeError):
        ReconnectingSession("ws://127.0.0.1:9", synthetic_token(), pacing=0.8)


# 1. Pacing at the margin


async def test_pacing_at_the_margin_keeps_every_window_under_its_limit():
    sim = Sim()
    pacer = Pacer(ONE_X, clock=sim.clock, sleep=sim.sleep)
    wire = Wire(sim)
    await blast(pacer.wrap(wire), 10_000)
    times = wire.times()
    assert most_in_window(times, 1.0) <= 160
    assert most_in_window(times, 60.0) <= 960
    # Throughput: each sustained window of 60 s plus the guard is filled to the limit.
    periods = len(times) // 960
    assert times[-1] <= periods * (60.0 + GUARD) + 10
    assert most_in_window(times, 60.0 + GUARD) == 960


async def test_burst_pacing_reaches_its_margin():
    sim = Sim()
    pacer = Pacer(WIDE, clock=sim.clock, sleep=sim.sleep)
    wire = Wire(sim)
    await blast(pacer.wrap(wire), 2000)
    times = wire.times()
    assert most_in_window(times, 1.0) <= 160
    # At best 160 in each 1 s plus the guard.
    best = 160 / (1.0 + GUARD)
    rate = (len(times) - 1) / (times[-1] - times[0])
    assert rate >= 0.97 * best, rate


# 2. The exchange's own check never rejects a paced stream


@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_the_exchange_never_rejects_paced_sends_with_jitter_up_to_300_ms(seed):
    sim = Sim(seed=seed)
    rng = random.Random(seed)
    exchange = Exchange(1200, 200)
    pacer = Pacer(ONE_X, clock=sim.clock, sleep=sim.sleep)
    wire = Wire(sim, pacer, exchange, jitter=lambda: rng.uniform(0.0, 0.3))
    await blast(pacer.wrap(wire), 4000)
    sim.run_until(sim.now + 5)
    assert exchange.rejects == []


async def test_bursts_of_160_every_1_05_s_never_trip_the_burst_cap_with_jitter():
    # Bursty: 160 sends at once every 1.05 s. Without the smoothing they would leave back
    # to back, and two bursts whose latency differs by more than the guard could land in
    # one receipt second.
    sim = Sim()
    rng = random.Random(7)
    exchange = Exchange(1_000_000, 200)
    pacer = Pacer(WIDE, clock=sim.clock, sleep=sim.sleep)
    wire = Wire(sim, pacer, exchange, jitter=lambda: rng.uniform(0.0, 0.3))
    sender = pacer.wrap(wire)
    for cycle in range(40):
        start = cycle * 1.05
        if sim.now < start:
            await sim.sleep(start - sim.now)
        await asyncio.gather(*(send_one(sender, "new", f"c{cycle}-{n}") for n in range(160)))
    sim.run_until(sim.now + 5)
    assert exchange.rejects == []
    assert most_in_window(wire.times(), 0.25) <= 40
    assert most_in_window(wire.times(), 1.0) <= 160


async def test_each_send_is_stamped_when_its_write_returns_and_writes_never_overlap():
    sim = Sim()
    pacer = Pacer(WIDE, clock=sim.clock, sleep=sim.sleep)
    wire = Wire(sim, write_time=0.1)
    sender = pacer.wrap(wire)
    await asyncio.gather(*(send_one(sender, "new", f"w{n}") for n in range(3)))
    assert wire.started == pytest.approx([0.0, 0.1, 0.2])
    assert list(pacer._burst.stamps) == pytest.approx([0.1, 0.2, 0.3])


async def test_a_send_whose_write_fails_is_still_counted():
    sim = Sim()
    pacer = Pacer(WIDE, clock=sim.clock, sleep=sim.sleep)

    class Failing:
        async def send(self, type_, payload):
            raise OSError("broken pipe")

    with pytest.raises(OSError):
        await send_one(pacer.wrap(Failing()), "new", "x")
    assert len(pacer._burst.stamps) == 1


# 3 and 4. A budget reject drains the window


async def test_recovery_after_a_burst_of_sustained_rejects(warned):
    sim = Sim()
    pacer = Pacer(ONE_X, clock=sim.clock, sleep=sim.sleep)
    wire = Wire(sim)
    sender = pacer.wrap(wire)
    await blast(sender, 10, prefix="a")
    sim.run_until(5.0)
    for n in range(5):
        pacer.observe(reject(SUSTAINED, f"a{n}"))
    assert codes(warned) == [errors.PACING_REJECTED]
    assert "sustained budget (MESSAGE_BUDGET_EXCEEDED)" in warned[0][1]
    # mass_cancel is never held, and is not counted.
    await send_one(sender, "mass_cancel", "m")
    assert wire.sent[-1] == (5.0, "mass_cancel", "m")
    # Nothing paced goes until the whole window and the guard have passed.
    await send_one(sender, "cancel", "b0")
    assert wire.sent[-1][0] == pytest.approx(5.0 + 60.0 + GUARD)
    # Then pacing resumes at the margin.
    await blast(sender, 159, prefix="c")
    assert wire.times()[-1] - wire.times()[-160] <= 1.0 + GUARD
    # The hold was logged once, with the reject, not again as a long wait.
    assert codes(warned) == [errors.PACING_REJECTED]


async def test_a_burst_reject_drains_only_about_a_second(warned):
    sim = Sim()
    pacer = Pacer(ONE_X, clock=sim.clock, sleep=sim.sleep)
    wire = Wire(sim)
    sender = pacer.wrap(wire)
    await blast(sender, 10, prefix="a")
    sim.run_until(3.0)
    pacer.observe(reject(BURST, "a9"))
    await send_one(sender, "new", "b")
    assert wire.sent[-1][0] == pytest.approx(3.0 + 1.0 + GUARD)
    assert pacer._sustained.held_until < 0  # the sustained window is not held
    assert codes(warned) == [errors.PACING_REJECTED]


async def test_a_reject_older_than_its_window_does_not_drain(warned):
    sim = Sim()
    sim.now = 100.0
    pacer = Pacer(ONE_X, clock=sim.clock, sleep=sim.sleep)
    server = 1_760_000_000_000
    old = reject(SUSTAINED, receipt=0)
    old.message.receipt_time = server - 70_000  # 70 s before the session's ack
    pacer._observe(old, (100.0, server))
    assert warned == []
    wire = Wire(sim)
    await send_one(pacer.wrap(wire), "new", "x")
    assert wire.sent[-1][0] == 100.0


# 5. The new-order cap


async def test_the_new_order_cap_holds_only_new_orders():
    budget = Budget(sustained_per_minute=1200, burst_per_second=200, new_orders_per_minute=120)
    sim = Sim()
    pacer = Pacer(budget, clock=sim.clock, sleep=sim.sleep)
    wire = Wire(sim)
    sender = pacer.wrap(wire)

    async def news():
        await blast(sender, 100, "new", "n")

    async def cancels():
        await blast(sender, 300, "cancel", "c")

    await asyncio.gather(news(), cancels())
    new_times, cancel_times = wire.times("new"), wire.times("cancel")
    assert most_in_window(new_times, 60.0) <= 96
    # 96 news went at once, the last 4 a minute later; every cancel went meanwhile.
    assert new_times[95] < 5 and new_times[96] >= 60.0
    assert cancel_times[-1] < 5


async def test_a_new_order_reject_holds_news_but_not_cancels(warned):
    sim = Sim()
    pacer = Pacer(ONE_X, clock=sim.clock, sleep=sim.sleep)
    wire = Wire(sim)
    sender = pacer.wrap(wire)
    pacer.observe(reject(NEW_ORDERS))
    await send_one(sender, "cancel", "c")
    assert wire.sent[-1][0] == 0.0
    await send_one(sender, "new", "n")
    assert wire.sent[-1][0] == pytest.approx(60.0 + GUARD)
    assert "pacer holds new orders" in warned[0][1]


# 6. on_limit="raise" and max_wait


async def test_raise_mode_raises_with_the_window_and_retry_after_and_sends_nothing():
    sim = Sim()
    pacer = Pacer(ONE_X, on_limit="raise", clock=sim.clock, sleep=sim.sleep)
    wire = Wire(sim)
    sender = pacer.wrap(wire)
    await blast(sender, 40)
    with pytest.raises(PacingLimit) as caught:
        await send_one(sender, "new", "x")
    error = caught.value
    assert type(error) is PacingLimit and isinstance(error, RuntimeError)
    assert error.code == errors.PACING_LIMIT and error.limit == "burst"
    assert error.type == "new" and error.retry_after == pytest.approx(0.25)
    assert str(error).startswith("QTE-PACING-LIMIT: the burst budget has no room for this new ")
    assert len(wire.sent) == 40
    assert sim.sleeps == []


async def test_raise_mode_after_a_reject_raises_draining():
    sim = Sim()
    pacer = Pacer(ONE_X, on_limit="raise", clock=sim.clock, sleep=sim.sleep)
    pacer.observe(reject(SUSTAINED))
    with pytest.raises(PacingDraining) as caught:
        await send_one(pacer.wrap(Wire(sim)), "amend", "x")
    error = caught.value
    assert isinstance(error, PacingLimit) and error.code == errors.PACING_DRAINING
    assert error.limit == "sustained" and error.retry_after == pytest.approx(60.0 + GUARD)
    assert str(error).startswith("QTE-PACING-DRAINING: paced messages are held for 60.05 s")


async def test_max_wait_waits_up_to_it_and_raises_past_it():
    sim = Sim()
    pacer = Pacer(
        Budget(sustained_per_minute=10, burst_per_second=200),
        max_wait=2.0,
        clock=sim.clock,
        sleep=sim.sleep,
    )
    wire = Wire(sim)
    sender = pacer.wrap(wire)
    await blast(sender, 8)  # the sustained limit; the smoothing lets them go at once
    with pytest.raises(PacingLimit) as caught:
        await send_one(sender, "new", "late")
    assert caught.value.limit == "sustained" and caught.value.retry_after > 59
    assert len(wire.sent) == 8 and sim.sleeps == []
    # A short wait is waited out.
    burst = Pacer(WIDE, max_wait=2.0, clock=sim.clock, sleep=sim.sleep)
    await blast(burst.wrap(wire), 41, prefix="b")
    assert len(wire.sent) == 49


# Waits over a second are logged once


async def test_a_long_wait_logs_holding_once_per_hold(warned):
    sim = Sim()
    pacer = Pacer(
        Budget(sustained_per_minute=10, burst_per_second=200), clock=sim.clock, sleep=sim.sleep
    )
    sender = pacer.wrap(Wire(sim))
    await blast(sender, 8)
    assert warned == []

    async def later(ref: str) -> None:
        await send_one(sender, "new", ref)

    await asyncio.gather(later("x"), later("y"))
    assert codes(warned) == [errors.PACING_HOLDING]
    assert warned[0][1].startswith(
        "QTE-PACING-HOLDING: the pacer is holding new, cancel and amend messages for 60.0 s "
        "on the sustained budget."
    )


# 7. Other connections' reports


async def test_a_foreign_accepted_counts_and_our_own_does_not():
    sim = Sim()
    pacer = Pacer(ONE_X, clock=sim.clock, sleep=sim.sleep)
    await send_one(pacer.wrap(Wire(sim)), "new", "ours")
    assert len(pacer._sustained.stamps) == 1
    pacer.observe(accepted("ours"))
    assert len(pacer._sustained.stamps) == 1
    pacer.observe(accepted("theirs", "cancel"))
    assert len(pacer._sustained.stamps) == 2
    assert len(pacer._new_order.stamps) == 1  # a cancel is not a new order
    pacer.observe(accepted("theirs-new"))
    assert len(pacer._new_order.stamps) == 2


async def test_a_foreign_report_seen_on_two_sessions_counts_once():
    sim = Sim()
    pacer = Pacer(ONE_X, clock=sim.clock, sleep=sim.sleep)
    first = Session(Connection("ws://127.0.0.1:9"), info(), [], pacer)
    second = Session(Connection("ws://127.0.0.1:9"), info(), [], pacer)
    event = accepted("theirs", "amend", receipt=0)
    first._keep(event)
    second._keep(event)
    assert len(pacer._sustained.stamps) == 1


async def test_mass_cancel_reports_and_count_foreign_false_count_nothing():
    sim = Sim()
    pacer = Pacer(ONE_X, count_foreign=False, clock=sim.clock, sleep=sim.sleep)
    pacer.observe(accepted("theirs"))
    assert len(pacer._sustained.stamps) == 0
    counting = Pacer(ONE_X, clock=sim.clock, sleep=sim.sleep)
    mass = Accepted(request_ref="m", request_type=4)  # MASS_CANCEL
    counting.observe(Received("accepted", mass, None))
    assert len(counting._sustained.stamps) == 0


async def test_replayed_reports_seed_the_windows_at_their_receipt_time():
    sim = Sim()
    sim.now = 100.0
    budget = Budget(sustained_per_minute=10, burst_per_second=200)
    pacer = Pacer(budget, clock=sim.clock, sleep=sim.sleep)
    server = 1_760_000_000_000
    anchor = (100.0, server)
    for n in range(8):
        event = accepted(f"t{n}")
        event.message.receipt_time = server - 30_000  # 30 s before the ack
        pacer._observe(event, anchor)
    old = accepted("old")
    old.message.receipt_time = server - 70_000  # out of every window
    pacer._observe(old, anchor)
    assert list(pacer._sustained.stamps) == pytest.approx([70.0] * 8)
    wire = Wire(sim)
    await send_one(pacer.wrap(wire), "new", "x")
    # The window frees once the stamps 30 s old have aged out, plus the guard.
    assert wire.sent[-1][0] == pytest.approx(130.0 + GUARD)


async def test_a_report_with_no_exchange_time_counts_now_and_never_later():
    sim = Sim()
    sim.now = 50.0
    pacer = Pacer(ONE_X, clock=sim.clock, sleep=sim.sleep)
    pacer._observe(accepted("a", receipt=0), (50.0, 1_760_000_000_000))
    future = accepted("b")
    future.message.receipt_time = 1_760_000_000_000 + 9_000_000
    pacer._observe(future, (50.0, 1_760_000_000_000))
    pacer._observe(accepted("c", receipt=1), (50.0, 0))
    assert list(pacer._sustained.stamps) == [50.0, 50.0, 50.0]


# Learning the cap from a reject (wrong values) and the cold start


async def test_wrong_values_four_times_the_real_caps_converge_within_two_drains(warned):
    sim = Sim()
    exchange = Exchange(1200, 200)
    pacer = Pacer(
        Budget(sustained_per_minute=4800, burst_per_second=800), clock=sim.clock, sleep=sim.sleep
    )
    pacer._session_started()
    # Every write returns at once, so many sends share a clock reading, as on a coarse clock.
    wire = Wire(sim, pacer, exchange)
    sender = pacer.wrap(wire)
    n = 0
    while sim.now < 300:
        await send_one(sender, "new", f"r{n}")
        n += 1
    sim.run_until(sim.now + 5)
    drains = {"burst": 0, "sustained": 0}
    for code, message in warned:
        if code == errors.PACING_REJECTED:
            drains["burst" if "burst budget" in message else "sustained"] += 1
    assert 1 <= drains["burst"] <= 2 and 1 <= drains["sustained"] <= 2, drains
    assert pacer.limits["burst"] < 200 and pacer.limits["sustained"] < 1200
    last = max(ts for ts, _ in exchange.rejects)
    assert last < 120, last  # none after the second drain
    learned = [m for c, m in warned if "looks like at most" in m]
    assert learned and "not the 800 you passed" in learned[0]


async def test_a_reject_soon_after_a_session_starts_drains_without_learning(warned):
    # A restarted bot can send into a window the last run filled: its own count proves
    # nothing about the cap then.
    sim = Sim()
    pacer = Pacer(ONE_X, clock=sim.clock, sleep=sim.sleep)
    pacer._session_started()
    await blast(pacer.wrap(Wire(sim)), 5)
    pacer.observe(reject(SUSTAINED, "r4"))
    assert pacer.limits == {"burst": 160, "sustained": 960, "new-order": None}
    assert "looks like" not in warned[0][1]


async def test_a_session_start_holds_the_burst_window_for_a_second():
    sim = Sim()
    sim.now = 10.0
    pacer = Pacer(ONE_X, clock=sim.clock, sleep=sim.sleep)
    Session(Connection("ws://127.0.0.1:9"), info(), [], pacer)
    wire = Wire(sim)
    await send_one(pacer.wrap(wire), "new", "x")
    assert wire.sent[-1][0] == pytest.approx(11.0)


async def test_a_reject_read_ahead_holds_the_next_send_before_it_is_delivered(warned):
    sim = Sim()
    pacer = Pacer(ONE_X, clock=sim.clock, sleep=sim.sleep)
    session = Session(Connection("ws://127.0.0.1:9"), info(), [reject(SUSTAINED)], pacer)
    assert session.pacing is pacer
    assert codes(warned) == [errors.PACING_REJECTED]
    assert pacer._sustained.held_until == pytest.approx(60.0 + GUARD)


# 8. Reconnect


def info() -> SessionInfo:
    return SessionInfo("s-1", "team-a", 1_760_000_000_000, CONTRACT_VERSION, False)


SERVER_TIME = 1_760_000_000_000


def ack_at(server_time: int) -> str:
    payload = {
        "session_id": "s-1",
        "team": "team-a",
        "server_time": str(server_time),
        "contract_version": CONTRACT_VERSION,
        "unscored": False,
    }
    return frame("session_ack", payload, 1)


def resume_ack_frame(replayed: bool, as_of: int) -> str:
    payload = {"replayed": replayed, "as_of_report_seq": str(as_of), "snapshot_count": 0}
    return frame("resume_ack", payload)


def accepted_frame(report_seq: int, ref: str, receipt_time: int) -> str:
    payload = {
        "request_ref": ref,
        "request_type": "NEW",
        "receipt_time": str(receipt_time),
        "release_time": str(receipt_time + 150),
    }
    return frame("accepted", payload, report_seq=str(report_seq))


async def test_the_pacer_survives_a_reconnect_and_a_long_replay_does_not_delay_sends():
    sim = Sim()
    pacer = Pacer(ONE_X, clock=sim.clock, sleep=sim.sleep)
    connections = 0
    orders_seen = asyncio.Event()

    async def handler(ws: ServerConnection) -> None:
        nonlocal connections
        connections += 1
        await ws.recv()  # auth
        await ws.send(ack_at(SERVER_TIME))
        await ws.recv()  # resume
        if connections == 1:
            await ws.send(resume_ack_frame(False, 0))
            for _ in range(5):
                assert json.loads(await ws.recv())["type"] == "new"
            orders_seen.set()
            await drop_connection(ws)
            return
        # 1,000 reports older than any window, then 3 the team sent on another
        # connection 10 s before this ack.
        await ws.send(resume_ack_frame(True, 1003))
        for n in range(1, 1001):
            await ws.send(accepted_frame(n, f"old{n}", SERVER_TIME - 600_000))
        for n in range(1001, 1004):
            await ws.send(accepted_frame(n, f"gap{n}", SERVER_TIME - 10_000))
        await ws.wait_closed()

    async def no_wait(delay: float) -> None:
        await asyncio.sleep(0)

    async with serve_local(handler) as url:
        rs = ReconnectingSession(url, synthetic_token(), pacing=pacer, sleep=no_wait)
        assert rs.pacing is pacer
        async with rs:
            sessions = 0
            async with asyncio.timeout(10):
                async for event in rs:
                    if isinstance(event, Connected):
                        sessions += 1
                        if sessions == 1:
                            for n in range(5):
                                await send_new(
                                    rs,
                                    strat_id="mm-1",
                                    instrument="AAPL",
                                    side=1,
                                    order_type=1,
                                    price=1,
                                    size=1,
                                    request_ref=f"own{n}",
                                )
                    if isinstance(event, ResumeComplete) and event.replayed:
                        assert orders_seen.is_set()
                        # The 5 own sends carried over, and only the 3 recent foreign
                        # reports count.
                        assert len(pacer._sustained.stamps) == 8
                        assert len(pacer._new_order.stamps) == 8
                        started = sim.now
                        await send_cancel(rs, instrument="AAPL", side=1, price=1)
                        # Only the second session's one-second hold on the burst window
                        # delays it.
                        assert sim.now - started <= 1.0 + 1e-9
                        await send_mass_cancel(rs)
                        break
            assert sessions == 2


# 9. Concurrency


async def test_concurrent_sends_go_in_call_order_and_never_over_the_limit():
    sim = Sim()
    pacer = Pacer(
        Budget(sustained_per_minute=1000, burst_per_second=20), clock=sim.clock, sleep=sim.sleep
    )
    wire = Wire(sim)
    sender = pacer.wrap(wire)
    await asyncio.gather(*(send_one(sender, "new", f"t{n}") for n in range(50)))
    assert [ref for _, _, ref in wire.sent] == [f"t{n}" for n in range(50)]
    assert most_in_window(wire.times(), 1.0) <= 16
    assert most_in_window(wire.times(), 0.25) <= 4


async def test_a_cancelled_waiting_send_frees_its_turn():
    sim = Sim()
    budget = Budget(sustained_per_minute=10, burst_per_second=200)
    pacer = Pacer(budget, clock=sim.clock, sleep=sim.sleep)
    wire = Wire(sim)
    sender = pacer.wrap(wire)
    await blast(sender, 8)
    gate = asyncio.Event()

    async def stuck_sleep(delay: float) -> None:
        await gate.wait()

    pacer._sleep = stuck_sleep
    waiting = asyncio.ensure_future(send_one(sender, "new", "w"))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert not pacer._lock.locked()
    await send_one(sender, "mass_cancel", "m")
    assert wire.sent[-1][1] == "mass_cancel"


# 10. Timers that wake early or late


@pytest.mark.parametrize("sim", [Sim(late=0.016, seed=3), Sim(early=0.001, seed=4)])
async def test_caps_hold_when_timers_wake_early_or_late(sim):
    exchange = Exchange(1200, 200)
    pacer = Pacer(ONE_X, clock=sim.clock, sleep=sim.sleep)
    wire = Wire(sim, pacer, exchange)
    await blast(pacer.wrap(wire), 2500)
    sim.run_until(sim.now + 5)
    times = wire.times()
    assert most_in_window(times, 1.0 + GUARD) <= 160
    assert most_in_window(times, 60.0 + GUARD) <= 960
    assert most_in_window(times, 0.25) <= 40
    assert exchange.rejects == []
    if sim.early:
        # A sleep that woke early was followed by another, not by a send.
        assert len(sim.sleeps) > len({round(t, 9) for t in times})


# 11. Without a pacer


async def test_without_a_pacer_a_budget_reject_warns_once_a_minute(warned, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(pacing, "_monotonic", lambda: now[0])
    session = Session(Connection("ws://127.0.0.1:9"), info(), [])
    assert session.pacing is None
    live = reject(SUSTAINED)

    def keep() -> None:
        # Received now, by the exchange's clock.
        live.message.receipt_time = SERVER_TIME + int((now[0] - 1000.0) * 1000)
        session._keep(live)

    for _ in range(3):
        keep()
    now[0] += 30
    keep()
    assert codes(warned) == [errors.BUDGET_REJECTED]
    assert warned[0][1] == (
        "QTE-BUDGET-REJECTED: the exchange rejected a message for your team's message budget "
        "(MESSAGE_BUDGET_EXCEEDED). Rejected messages count toward the window too, so each "
        "message sent now keeps your team locked out for longer. Stop sending new, cancel and "
        "amend for a full minute, and wait that minute before restarting your bot too; then "
        "pace them with open_session(pacing=Pacer(budget)) from qte_sdk.pacing."
    )
    now[0] += 31
    keep()
    assert codes(warned) == [errors.BUDGET_REJECTED] * 2
    # A reject older than a minute (a replay, say) says nothing.
    monkeypatch.setattr(pacing, "_budget_warned_at", None)
    live.message.receipt_time = SERVER_TIME
    session._keep(live)
    assert len(warned) == 2
    # Other rejects, and accepted reports, say nothing.
    session._keep(reject(ReasonCodes.MARKET_CLOSED))
    session._keep(accepted("x"))
    assert len(warned) == 2


def test_a_warning_reaches_the_log_from_its_own_thread_with_its_code(caplog, monkeypatch):
    monkeypatch.setattr(pacing, "_budget_warned_at", None)
    before = set(threading.enumerate())
    with caplog.at_level(logging.WARNING, logger="qte_sdk.pacing"):
        pacing._budget_rejected(reject(BURST), None)
        for thread in set(threading.enumerate()) - before:
            if thread.name == "qte-sdk pacing warning":
                thread.join(5)
    records = [r for r in caplog.records if r.name == "qte_sdk.pacing"]
    assert len(records) == 1
    assert records[0].code == errors.BUDGET_REJECTED
    assert records[0].getMessage().startswith("QTE-BUDGET-REJECTED: ")
    assert records[0].threadName == "qte-sdk pacing warning"


def test_warnings_never_stop_the_event_loop_on_a_full_stderr_pipe(monkeypatch):
    # A real pipe that nothing reads, filled first: a write to it would block whichever
    # thread made it. The loop runs in a thread of its own, so a regression that wrote
    # from the loop fails here rather than hanging the suite.
    from test_update import fill, read_all

    monkeypatch.setattr(pacing, "_budget_warned_at", None)
    read, write = os.pipe()
    stream = open(write, "w", encoding="utf-8")  # noqa: SIM115
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("qte_sdk.pacing")
    monkeypatch.setattr(logger, "propagate", False)
    logger.addHandler(handler)
    ticks: list[int] = []
    before = set(threading.enumerate())

    async def trading() -> None:
        sim = Sim()
        pacer = Pacer(ONE_X, clock=sim.clock, sleep=sim.sleep)
        for n in range(20):
            pacing._budget_rejected(reject(BURST), None)
            pacer.observe(reject(SUSTAINED, f"r{n}"))
            sim.now += 61.0  # each reject a new hold, so each is logged
            await asyncio.sleep(0)
            ticks.append(n)

    try:
        fill(write)
        loop = threading.Thread(target=asyncio.run, args=(trading(),), daemon=True)
        loop.start()
        loop.join(30)
        assert not loop.is_alive(), "the event loop stopped on a full stderr pipe"
        assert ticks == list(range(20))
        for thread in set(threading.enumerate()) - before:
            if thread.name == "qte-sdk pacing warning":
                thread.join(5)
                assert not thread.is_alive()
        assert b"QTE-" not in read_all(read)
    finally:
        logger.removeHandler(handler)
        stream.close()
        os.close(read)


def test_a_pacing_limit_survives_pickling():
    import pickle

    error = pickle.loads(pickle.dumps(PacingDraining("cancel", "sustained", 12.5)))
    assert type(error) is PacingDraining
    assert (error.type, error.limit, error.retry_after) == ("cancel", "sustained", 12.5)
    assert str(error).startswith("QTE-PACING-DRAINING: ")


# Found in review


async def test_a_new_held_after_its_turn_never_holds_up_a_cancel():
    budget = Budget(sustained_per_minute=1000, burst_per_second=200, new_orders_per_minute=120)
    sim = Sim()
    pacer = Pacer(budget, clock=sim.clock, sleep=sim.sleep)
    wire = Wire(sim)
    sender = pacer.wrap(wire)
    await blast(sender, 40, "cancel", "c")  # the burst window's first quarter is full
    # The next new passes the new-order window, then waits for the burst window, while
    # other connections' news fill the new-order window.
    sim.at(0.1, lambda: [pacer.observe(accepted(f"f{n}")) for n in range(96)])
    await asyncio.gather(send_one(sender, "new", "n"), send_one(sender, "cancel", "late"))
    assert [ref for _, _, ref in wire.sent[-2:]] == ["n", "late"]
    assert wire.sent[-1][0] < 1


async def test_an_order_queued_before_a_disconnect_is_not_sent_on_the_next_session():
    from types import SimpleNamespace

    from qte_sdk.reconnect import NotConnected

    sim = Sim()
    pacer = Pacer(ONE_X, clock=sim.clock, sleep=sim.sleep)
    old, replacement = Wire(sim), Wire(sim)
    rs = ReconnectingSession("ws://127.0.0.1:9", synthetic_token(), pacing=pacer)
    rs._session, rs._up = SimpleNamespace(connection=old), True
    pacer.observe(reject(BURST))

    def down() -> None:
        rs._session, rs._up = None, False

    def up() -> None:
        rs._session, rs._up = SimpleNamespace(connection=replacement), True

    sim.at(0.25, down)
    sim.at(0.5, up)
    with pytest.raises(NotConnected):
        await send_one(rs, "new", "stale")
    assert old.sent == [] and replacement.sent == []


def frames_hold(error: BaseException, secret: str) -> list[str]:
    """The SDK frames in `error`'s traceback whose locals show `secret`."""
    import traceback

    found = []
    summary = traceback.TracebackException.from_exception(error, capture_locals=True)
    for frame_ in summary.stack:
        if "qte_sdk" in frame_.filename and secret in repr(frame_.locals):
            found.append(f"{frame_.name}")
    return found


async def test_an_unpaced_message_that_fails_leaves_no_copy_in_the_pacer_frames():
    from qte_sdk.contract.v1.session_pb2 import Auth

    token = synthetic_token()
    session = Session(Connection("ws://127.0.0.1:9"), info(), [], Pacer(ONE_X))
    with pytest.raises(Exception) as caught:
        await session.send("auth", Auth(token=token))
    assert frames_hold(caught.value, token) == []

    class Failing:
        async def send(self, type_, payload):
            raise OSError("broken pipe")

    with pytest.raises(OSError) as caught:
        await Pacer(ONE_X).wrap(Failing()).send("auth", Auth(token=token))
    assert frames_hold(caught.value, token) == []


async def test_a_wrong_pacing_argument_leaves_no_token_in_the_traceback():
    from qte_sdk.session import open_session

    token = synthetic_token()
    with pytest.raises(TypeError) as caught:
        await open_session("ws://127.0.0.1:9", token, pacing=0.8)
    assert frames_hold(caught.value, token) == []
    with pytest.raises(TypeError) as caught:
        ReconnectingSession("ws://127.0.0.1:9", token, pacing=0.8)
    assert frames_hold(caught.value, token) == []


async def test_a_reject_read_late_is_matched_against_the_window_that_held_its_message():
    sim = Sim()
    pacer = Pacer(
        Budget(sustained_per_minute=10_000, burst_per_second=100), clock=sim.clock, sleep=sim.sleep
    )
    sender = pacer.wrap(Wire(sim))
    sim.now = 1.0
    await blast(sender, 8, prefix="a")
    sim.now = 1.5
    await send_one(sender, "new", "b")
    sim.now = 1.95
    await send_one(sender, "new", "rejected")
    sim.now = 2.06
    await send_one(sender, "new", "after")  # the 8 sent at 1.0 no longer count here
    sim.now = 2.1
    pacer.observe(reject(BURST, "rejected"))
    # Nine were in the exchange's window ahead of it, so the cap is at most nine.
    assert pacer.limits["burst"] == 7


async def test_foreign_reports_that_reuse_a_request_ref_each_count():
    sim = Sim()
    pacer = Pacer(ONE_X, clock=sim.clock, sleep=sim.sleep)
    pacer.observe(accepted("r1", receipt=1.0))
    pacer.observe(accepted("r1", receipt=2.0))
    pacer.observe(accepted("r1", receipt=2.0))  # the same report, again
    assert len(pacer._sustained.stamps) == 2


def test_a_warning_never_asks_the_logger_on_the_event_loop_thread(monkeypatch):
    monkeypatch.setattr(pacing, "_budget_warned_at", None)
    asked: list[str] = []
    logger = logging.getLogger("qte_sdk.pacing")
    real = logger.isEnabledFor

    def is_enabled_for(level: int) -> bool:
        asked.append(threading.current_thread().name)
        return real(level)

    monkeypatch.setattr(logger, "isEnabledFor", is_enabled_for)
    before = set(threading.enumerate())
    pacing._budget_rejected(reject(BURST), None)
    for thread in set(threading.enumerate()) - before:
        thread.join(5)
    assert asked and set(asked) == {"qte-sdk pacing warning"}
