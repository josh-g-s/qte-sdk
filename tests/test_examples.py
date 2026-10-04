"""Smoke tests for the worked examples in `examples/`.

Every example must compile and import without side effects, and must exit cleanly within
its bounded run against a local fake exchange. Each run is a real subprocess, as a
student would start it, with the exchange URL and a synthetic token in its environment.
"""

import asyncio
import contextlib
import importlib.util
import json
import os
import py_compile
import re
import secrets
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest
from fake_exchange import CONTRACT_VERSION, serve_local
from test_history import FakeHistory, serve_history
from test_history import book as history_book
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

from qte_sdk.connection import DecodeFailed, Received, SeqGap
from qte_sdk.contract.v1.common_pb2 import (
    BUY,
    FILLED,
    LIMIT,
    NEW,
    RESTING,
    MarketSessionPhase,
    OrderLifecycleState,
    ReasonCodes,
)
from qte_sdk.contract.v1.market_data_pb2 import LIVE, StudentLevel, WallLevel
from qte_sdk.contract.v1.market_data_pb2 import Book as BookMessage
from qte_sdk.contract.v1.market_data_pb2 import SessionState as SessionStateMessage
from qte_sdk.contract.v1.order_events_pb2 import (
    Accepted,
    Execution,
    OrderCancelled,
    OrderState,
    Reject,
)
from qte_sdk.orders import reason_code_name, send_amend, send_new
from qte_sdk.resting import RestingOrders
from qte_sdk.session import open_session
from qte_sdk.units import to_decimal, to_timedelta

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
EXAMPLES = sorted(EXAMPLES_DIR.glob("*.py"))
QUICKSTART = EXAMPLES_DIR.parent / "docs" / "quickstart.md"
OUT_OF_HOURS = EXAMPLES_DIR.parent / "docs" / "out-of-hours.md"
SDK_INSTALL_URL = "git+https://github.com/josh-g-s/qte-sdk"
INSTRUMENT = "TEST"
BID, ASK = 99_950_000, 100_050_000
TICK = 10_000

# The longest any run may take, well beyond each example's own bound.
RUN_LIMIT = 30.0


def test_the_examples_directory_has_the_three_worked_examples():
    names = {path.name for path in EXAMPLES}
    assert {"print_book.py", "quote_both_sides.py", "take_liquidity.py"} <= names


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_each_example_compiles_and_imports_without_running(path: Path, tmp_path: Path):
    py_compile.compile(str(path), cfile=str(tmp_path / "compiled.pyc"), doraise=True)
    spec = importlib.util.spec_from_file_location(f"example_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.main)
    assert module.__doc__, "each example opens with a docstring saying what it does"


@pytest.mark.parametrize("path", [*EXAMPLES, QUICKSTART, OUT_OF_HOURS], ids=lambda p: p.name)
def test_no_example_or_quickstart_names_any_exchange_but_a_local_one(path: Path):
    for url in re.findall(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s`'\")]+", path.read_text()):
        if url == SDK_INSTALL_URL:
            continue  # where pip installs the SDK from, not an exchange
        parts = urlsplit(url)
        assert parts.scheme == "ws", url
        assert parts.hostname == "127.0.0.1", url
        assert parts.username is None and parts.password is None, url


@pytest.mark.parametrize("path", [*EXAMPLES, QUICKSTART, OUT_OF_HOURS], ids=lambda p: p.name)
def test_no_example_or_quickstart_reaches_past_the_session_to_its_connection(path: Path):
    # A session sends and is iterated itself; its connection is an internal layer.
    assert re.search(r"\bsession\.connection\b", path.read_text()) is None


class FakeExchange:
    """A scripted exchange: acknowledges the session, publishes a book on every tick once
    subscribed, and answers order messages the way the contract describes.

    With `book_once` it publishes one book after the subscribe and then only the session
    state on each tick, as the exchange does for a book that does not change."""

    def __init__(
        self,
        *,
        reject_new: str | None = None,
        partial_fill: bool = False,
        move_bid_after: int | None = None,
        teammate_fill_first: bool | str = False,
        gap_after_resting: int | None = None,
        reject_first_cancel_then_gap: bool = False,
        stray_reject: bool = False,
        confirm_cancels: asyncio.Event | None = None,
        closed: bool = False,
        book_once: bool = False,
        fill_first_buy: bool = False,
        official_close: bool = True,
        calendar: dict[str, Any] | None = None,
        closed_state: dict[str, Any] | None = None,
        server_time: int = 1,
        known_instruments: frozenset[str] | None = None,
        account_state: dict[str, Any] | None = None,
        account_reject: str | None = None,
        cancel_rejects: list[str] | None = None,
        reject_detail: str | None = None,
        move_resting_to: tuple[int, ...] = (),
        withhold_accepted: bool = False,
        account_unknown: str | None = None,
        first_book_after: int = 0,
        unscored: bool = True,
        cross_new_at: int | None = None,
        outside_band: bool = False,
        hide_moves: bool = False,
        outage: bool = False,
        session_reject: dict[str, Any] | None = None,
        refill_level: bool = False,
        heartbeat_every: float | None = None,
    ) -> None:
        # With `heartbeat_every`, a heartbeat is sent that often, in seconds, from the ack
        # on, each carrying a send time that many seconds after the ack's server_time.
        self.heartbeat_every = heartbeat_every
        # The exchange's clock in the session_ack, in milliseconds since the epoch.
        self.server_time = server_time
        # Whether the session_ack says the session is unscored.
        self.unscored = unscored
        # With `cross_new_at`, a limit order fills in full on release, as a taker, at that
        # price: the asks fell to it while it was delayed. Nothing rests.
        self.cross_new_at = cross_new_at
        # With `outside_band`, a limit order is accepted but cancelled at once with
        # REMAINDER_OUTSIDE_BAND, as when the wall's bid rose to it during the delay.
        self.outside_band = outside_band
        # With `hide_moves`, each amend `move_resting_to` makes is a message the client
        # never receives: a gap in the sequence instead of its order_state.
        self.hide_moves = hide_moves
        # With `outage`, the session state says an exchange outage is in force.
        self.outage = outage
        # With `session_reject`, the session is refused with it instead of acknowledged.
        self.session_reject = session_reject
        # With `refill_level`, once `move_resting_to` has moved the order away, another
        # order of the same strategy takes the level it left.
        self.refill_level = refill_level
        # With `account_unknown`, an account_query is refused as a message type the
        # exchange does not know, as an older build does: MALFORMED_MESSAGE naming no
        # request type. "echo" keeps its request_ref on that reject; "bare" does not.
        self.account_unknown = account_unknown
        # The books start only after this many intervals of the session, as for an
        # instrument with no valid quote yet; the session state comes from the first.
        self.first_book_after = first_book_after
        # With `withhold_accepted`, a limit order's `accepted` is never sent, as if lost.
        self.withhold_accepted = withhold_accepted
        # With `known_instruments`, a subscribe naming any other instrument is answered with
        # an UNKNOWN_INSTRUMENT reject for it, and only the known ones are served.
        self.known_instruments = known_instruments
        # With `account_state`, an account_query is answered with it, echoing request_ref,
        # and with `account_reject` it is rejected with that reason; with neither, it is
        # never answered, as by an exchange that does not serve it.
        self.account_state = account_state
        self.account_reject = account_reject
        # Each cancel takes the next reason here, if any, and is rejected with it.
        self.cancel_rejects = list(cancel_rejects or [])
        # With `reject_detail`, the reject that `reject_new` asks for carries it.
        self.reject_detail = reject_detail
        # With `move_resting_to`, the first limit order to rest is at once amended, as by
        # another program of the same team, to rest at each of those prices in turn.
        self.move_resting_to = move_resting_to
        # Every message sent and received, in order, by type.
        self.log: list[tuple[str, str]] = []
        # With `closed_state`, that is the session_state answering a subscribe when closed.
        self.closed_state = closed_state
        self.book_once = book_once
        # With `fill_first_buy`, the first buy order to rest is at once filled completely.
        self.fill_first_buy = fill_first_buy
        self.books_sent = 0
        # With `closed`, the exchange is outside a session: it answers a subscribe once,
        # with the closed state and, unless `official_close` is False (as on the exchange
        # today), each instrument's official close, and publishes nothing.
        self.closed = closed
        self.official_close = official_close
        # With `calendar`, that calendar follows the session_ack.
        self.calendar = calendar
        # With `confirm_cancels`, a cancel is accepted at once but its order_cancelled is
        # sent only once the test sets that event (never, if it does not).
        self.confirm_cancels = confirm_cancels
        self._confirming: set[asyncio.Task] = set()
        self.stray_reject = stray_reject
        self.reject_first_cancel_then_gap = reject_first_cancel_then_gap
        self.reject_new = reject_new
        self.partial_fill = partial_fill
        self.move_bid_after = move_bid_after
        self.teammate_fill_first = teammate_fill_first
        self.gap_after_resting = gap_after_resting
        self.resting_reports = 0
        self.received: list[dict[str, Any]] = []
        self.resting: dict[tuple[str, str, int], tuple[str, int]] = {}
        self._seq = 0
        self._lock = asyncio.Lock()

    async def send(
        self, ws: ServerConnection, type_: str, payload: dict[str, Any], **envelope: Any
    ) -> None:
        async with self._lock:  # seq numbers must reach the client in order
            self._seq += 1
            env = {"version": CONTRACT_VERSION, "type": type_, "payload": payload, "seq": self._seq}
            env.update(envelope)
            self.log.append(("sent", type_))
            await ws.send(json.dumps(env))

    async def beat(self, ws: ServerConnection) -> None:
        assert self.heartbeat_every is not None
        sent = 0
        while True:
            await asyncio.sleep(self.heartbeat_every)
            sent += 1
            sent_at = self.server_time + round(sent * self.heartbeat_every * 1000)
            with contextlib.suppress(ConnectionClosed):
                await self.send(ws, "heartbeat", {}, sent_at=str(sent_at))

    async def answer_account_query(self, ws: ServerConnection, ref: str) -> None:
        if self.account_unknown is not None:
            unknown: dict[str, Any] = {"reason_code": "MALFORMED_MESSAGE", "receipt_time": "1"}
            if self.account_unknown == "echo":
                unknown["request_ref"] = ref
            await self.send(ws, "reject", unknown)
        elif self.account_reject is not None:
            reject = {
                "request_ref": ref,
                "request_type": "ACCOUNT_QUERY",
                "reason_code": self.account_reject,
                "receipt_time": "1",
            }
            await self.send(ws, "reject", reject)
        elif self.account_state is not None:
            await self.send(ws, "account_state", {**self.account_state, "request_ref": ref})

    async def __call__(self, ws: ServerConnection) -> None:
        self.received.append(json.loads(await ws.recv()))
        self.log.append(("received", self.received[-1]["type"]))
        if self.session_reject is not None:
            await self.send(ws, "session_reject", self.session_reject)
            await ws.close()
            return
        ack = {
            "session_id": "s-1",
            "team": "team-a",
            "server_time": str(self.server_time),
            "contract_version": CONTRACT_VERSION,
            "unscored": self.unscored,
        }
        await self.send(ws, "session_ack", ack)
        if self.calendar is not None:
            await self.send(ws, "calendar", self.calendar)
        ticker: asyncio.Task | None = None
        beats = None if self.heartbeat_every is None else asyncio.create_task(self.beat(ws))
        try:
            async for raw in ws:
                message = json.loads(raw)
                self.received.append(message)
                self.log.append(("received", message["type"]))
                if message["type"] == "subscribe":
                    instruments = message["payload"]["instruments"]
                    if self.known_instruments is not None:
                        instruments = await self.refuse_unknown(ws, instruments)
                        if not instruments:
                            continue
                if message["type"] == "account_query":
                    await self.answer_account_query(ws, message["payload"]["request_ref"])
                elif message["type"] == "subscribe" and self.closed:
                    await self.answer_closed(ws, instruments)
                elif message["type"] == "subscribe" and ticker is None:
                    if self.stray_reject:
                        # A reject of something the exchange could not read: no request_ref.
                        stray = {"reason_code": "MALFORMED_MESSAGE", "receipt_time": "1"}
                        await self.send(ws, "reject", stray)
                    ticker = asyncio.create_task(self.publish(ws))
                else:
                    await self.on_order(ws, message["type"], message["payload"])
        except ConnectionClosed:
            pass
        finally:
            if ticker is not None:
                ticker.cancel()
            if beats is not None:
                beats.cancel()
            for task in self._confirming:
                task.cancel()

    async def publish(self, ws: ServerConnection) -> None:
        book = {
            "instrument": INSTRUMENT,
            "grid_time": "1",
            "bid_levels": [{"price": str(BID), "size": "300"}],
            "ask_levels": [{"price": str(ASK), "size": "200"}],
            "condition": "LIVE",
        }
        state = {"state": "OPEN", "session_date": "2026-01-05", "grid_time": "1"}
        if self.outage:
            state["outage_active"] = True
        published = 0
        while True:
            if published == self.move_bid_after:
                # A changed book is published at a later grid time.
                book["bid_levels"] = [{"price": str(BID - TICK), "size": "300"}]
                book["grid_time"] = "2"
            # As on the exchange, the session state comes on every interval, unchanged.
            await self.send(ws, "session_state", state)
            if published < self.first_book_after:
                pass  # no valid quote yet, so no book
            elif not self.book_once or self.books_sent == 0:
                await self.send(ws, "book", book)
                self.books_sent += 1
            published += 1
            await asyncio.sleep(0.05)

    async def refuse_unknown(self, ws: ServerConnection, instruments: list[str]) -> list[str]:
        """Reject each instrument this exchange does not know; return the ones it does."""
        assert self.known_instruments is not None
        for instrument in instruments:
            if instrument not in self.known_instruments:
                reject = {
                    "request_type": "SUBSCRIBE",
                    "reason_code": "UNKNOWN_INSTRUMENT",
                    "receipt_time": "1",
                    "instrument": instrument,
                }
                await self.send(ws, "reject", reject)
        return [name for name in instruments if name in self.known_instruments]

    async def answer_closed(self, ws: ServerConnection, instruments: list[str]) -> None:
        state = {"state": "CLOSED", "session_date": "2026-01-05", "close_time": "2"}
        # As the out-of-hours reply does, it names the next scheduled session.
        state["next_session_date"] = "2026-01-06"
        state["next_open_time"], state["next_close_time"] = "100", "200"
        if self.closed_state is not None:
            await self.send(ws, "session_state", self.closed_state)
        else:
            await self.send(ws, "session_state", {**state, "grid_time": "2"})
        if not self.official_close:
            return
        for instrument in instruments:
            close = {"instrument": instrument, "session_date": "2026-01-05", "value": "100011000"}
            await self.send(ws, "official_close", close)

    def types(self) -> list[str]:
        return [message["type"] for message in self.received]

    async def on_order(self, ws: ServerConnection, type_: str, p: dict[str, Any]) -> None:
        ref = p.get("request_ref")
        accepted = {"request_ref": ref, "request_type": type_.upper(), "receipt_time": "1"}
        if type_ == "new":
            level = (p["instrument"], p["side"], int(p.get("price", 0)))
            if p["order_type"] == "LIMIT" and level in self.resting:
                reject = {
                    "request_ref": ref,
                    "request_type": "NEW",
                    "reason_code": "DUPLICATE_ORDER_AT_LEVEL",
                    "receipt_time": "1",
                }
                await self.send(ws, "reject", reject)
                return
            if self.reject_new is not None:
                reject = {
                    "request_ref": ref,
                    "request_type": "NEW",
                    "reason_code": self.reject_new,
                    "receipt_time": "1",
                }
                if self.reject_detail is not None:
                    reject["reason_detail"] = self.reject_detail
                await self.send(ws, "reject", reject)
                return
            if self.teammate_fill_first:
                # While this order is delayed, a teammate's order at the same level fills
                # completely and leaves it, which frees the level for this one. A string
                # names that order's strategy, which may be this order's own.
                earlier = self.teammate_fill_first
                self.teammate_fill_first = False
                teammate = {
                    "exec_id": "e-0",
                    "origin": "TEAM",
                    "strat_id": earlier if isinstance(earlier, str) else "teammate",
                    "instrument": p["instrument"],
                    "side": p["side"],
                    "order_price": p["price"],
                    "fill_price": p["price"],
                    "fill_size": "4",
                    "remaining_size": "0",
                    "fill_kind": "STUDENT_TO_STUDENT",
                    "liquidity": "MAKER",
                    "fee": "5",
                }
                await self.send(ws, "execution", teammate)
            if not (self.withhold_accepted and p["order_type"] == "LIMIT"):
                await self.send(ws, "accepted", accepted)
            size = int(p["size"])
            if p["order_type"] == "LIMIT" and self.cross_new_at is not None:
                fill = {
                    "exec_id": "e-5",
                    "origin": "TEAM",
                    "strat_id": p["strat_id"],
                    "instrument": p["instrument"],
                    "side": p["side"],
                    "order_price": p["price"],
                    "fill_price": str(self.cross_new_at),
                    "fill_size": str(size),
                    "remaining_size": "0",
                    "fill_kind": "STUDENT_TO_WALL",
                    "liquidity": "TAKER",
                    "fee": "-10",
                }
                await self.send(ws, "execution", fill)
                return
            if p["order_type"] == "LIMIT" and self.outside_band:
                cancelled = {
                    "origin": "TEAM",
                    "strat_id": p["strat_id"],
                    "instrument": p["instrument"],
                    "side": p["side"],
                    "price": p["price"],
                    "cancelled_size": str(size),
                    "reason_code": "REMAINDER_OUTSIDE_BAND",
                    "timestamp": "1",
                }
                await self.send(ws, "order_cancelled", cancelled)
                return
            if p["order_type"] == "MARKET":
                fill = {
                    "exec_id": "e-1",
                    "origin": "TEAM",
                    "strat_id": p["strat_id"],
                    "instrument": p["instrument"],
                    "side": p["side"],
                    "fill_price": str(ASK if p["side"] == "BUY" else BID),
                    "fill_size": str(size),
                    "remaining_size": "0",
                    "fill_kind": "STUDENT_TO_WALL",
                    "liquidity": "TAKER",
                    "fee": "-10",
                }
                await self.send(ws, "execution", fill)
                return
            key = (p["instrument"], p["side"], int(p["price"]))
            self.resting[key] = (p["strat_id"], size)
            await self.send(ws, "order_state", self.order_state(key))
            moves, self.move_resting_to = self.move_resting_to, ()
            for price in moves:
                moved = (key[0], key[1], price)
                self.resting[moved] = self.resting.pop(key)
                state = {**self.order_state(moved), "old_price": str(key[2])}
                if self.hide_moves:
                    self._seq += 1  # the order_state the client never receives
                else:
                    await self.send(ws, "order_state", state)
                key = moved
            if moves and self.refill_level:
                level = (p["instrument"], p["side"], int(p["price"]))
                self.resting[level] = (p["strat_id"], size)
            self.resting_reports += 1
            if self.resting_reports == self.gap_after_resting:
                self._seq += 1  # one message the client never receives
            if self.fill_first_buy and p["side"] == "BUY":
                self.fill_first_buy = False
                del self.resting[key]
                fill = {
                    "exec_id": "e-3",
                    "origin": "TEAM",
                    "strat_id": p["strat_id"],
                    "instrument": p["instrument"],
                    "side": p["side"],
                    "order_price": p["price"],
                    "fill_price": p["price"],
                    "fill_size": str(size),
                    "remaining_size": "0",
                    "fill_kind": "STUDENT_TO_STUDENT",
                    "liquidity": "MAKER",
                    "fee": "5",
                }
                await self.send(ws, "execution", fill)
            if self.partial_fill and size > 1:
                self.partial_fill = False
                self.resting[key] = (p["strat_id"], size - 1)
                fill = {
                    "exec_id": "e-2",
                    "origin": "TEAM",
                    "strat_id": p["strat_id"],
                    "instrument": p["instrument"],
                    "side": p["side"],
                    "order_price": p["price"],
                    "fill_price": p["price"],
                    "fill_size": "1",
                    "remaining_size": str(size - 1),
                    "fill_kind": "STUDENT_TO_STUDENT",
                    "liquidity": "MAKER",
                    "fee": "5",
                }
                await self.send(ws, "execution", fill)
        elif type_ == "amend":
            await self.on_amend(ws, p, accepted)
        elif type_ == "cancel":
            key = (p["instrument"], p["side"], int(p["price"]))
            if self.cancel_rejects:
                reject = {
                    "request_ref": ref,
                    "request_type": "CANCEL",
                    "reason_code": self.cancel_rejects.pop(0),
                    "receipt_time": "1",
                }
                await self.send(ws, "reject", reject)
                return
            if self.reject_first_cancel_then_gap:
                self.reject_first_cancel_then_gap = False
                reject = {
                    "request_ref": ref,
                    "request_type": "CANCEL",
                    "reason_code": "MIN_REST_VIOLATION",
                    "receipt_time": "1",
                }
                await self.send(ws, "reject", reject)
                self._seq += 1  # one message the client never receives
                return
            if key not in self.resting:
                reject = {
                    "request_ref": ref,
                    "request_type": "CANCEL",
                    "reason_code": "NO_ORDER_AT_LEVEL",
                    "receipt_time": "1",
                }
                await self.send(ws, "reject", reject)
                return
            await self.send(ws, "accepted", accepted)
            if self.confirm_cancels is not None:
                self._confirming.add(asyncio.create_task(self.confirm_later(ws, key, ref)))
                return
            await self.send(ws, "order_cancelled", self.cancelled(key, ref, "CANCEL_REQUEST"))
        elif type_ == "mass_cancel":
            await self.send(ws, "accepted", accepted)
            for key in list(self.resting):
                await self.send(ws, "order_cancelled", self.cancelled(key, ref, "MASS_CANCEL"))

    async def on_amend(
        self, ws: ServerConnection, p: dict[str, Any], accepted: dict[str, Any]
    ) -> None:
        """Every accepted amend sends one order_state naming the price the order had before
        it as `old_price`: RESTING where the order still rests, CANCELLED when the amend cut
        it to nothing, FILLED when a marketable new price filled it against the wall."""
        old = (p["instrument"], p["side"], int(p["price"]))
        key = (p["instrument"], p["side"], int(p["new_price"]))
        reason = None
        if old not in self.resting:
            reason = "NO_ORDER_AT_LEVEL"
        elif key != old and key in self.resting:
            reason = "DUPLICATE_ORDER_AT_LEVEL"
        elif key != old and ((key[2] <= BID) if p["side"] == "BUY" else (key[2] >= ASK)):
            # Price tests run only when the amend moves the price: a new price at or beyond
            # its own side's wall. A size-only amend at the wall is not price-checked.
            reason = "AMEND_PRICE_AT_OR_BEYOND_WALL"
        if reason is not None:
            reject = {
                "request_ref": p.get("request_ref"),
                "request_type": "AMEND",
                "reason_code": reason,
                "receipt_time": "1",
            }
            await self.send(ws, "reject", reject)
            return
        await self.send(ws, "accepted", accepted)
        size = int(p["new_size"])
        moved = {"old_price": str(old[2])}
        if size == 0:
            strat_id = self.resting[old][0]
            ref = p.get("request_ref", "")
            await self.send(ws, "order_cancelled", self.cancelled(old, ref, "AMEND_CUT"))
            await self.send(ws, "order_state", self.ended(key, strat_id, "CANCELLED", moved))
            return
        strat_id, _ = self.resting.pop(old)
        wall = ASK if p["side"] == "BUY" else BID
        if (key[2] >= wall) if p["side"] == "BUY" else (key[2] <= wall):
            fill = {
                "exec_id": "e-4",
                "origin": "TEAM",
                "strat_id": strat_id,
                "instrument": p["instrument"],
                "side": p["side"],
                "order_price": str(key[2]),
                "fill_price": str(wall),
                "fill_size": str(size),
                "remaining_size": "0",
                "fill_kind": "STUDENT_TO_WALL",
                "liquidity": "TAKER",
                "fee": "-10",
            }
            await self.send(ws, "execution", fill)
            await self.send(ws, "order_state", self.ended(key, strat_id, "FILLED", moved))
            return
        self.resting[key] = (strat_id, size)
        await self.send(ws, "order_state", {**self.order_state(key), **moved})

    def ended(
        self, key: tuple[str, str, int], strat_id: str, state: str, extra: dict[str, str]
    ) -> dict[str, Any]:
        instrument, side, price = key
        return {
            "strat_id": strat_id,
            "instrument": instrument,
            "side": side,
            "price": str(price),
            "state": state,
            "remaining_size": "0",
            "timestamp": "1",
            **extra,
        }

    async def confirm_later(
        self, ws: ServerConnection, key: tuple[str, str, int], ref: str
    ) -> None:
        assert self.confirm_cancels is not None
        await self.confirm_cancels.wait()
        if key not in self.resting:
            return  # an earlier cancel of the same level already took it
        with contextlib.suppress(ConnectionClosed):
            await self.send(ws, "order_cancelled", self.cancelled(key, ref, "CANCEL_REQUEST"))

    def order_state(self, key: tuple[str, str, int]) -> dict[str, Any]:
        strat_id, size = self.resting[key]
        instrument, side, price = key
        return {
            "strat_id": strat_id,
            "instrument": instrument,
            "side": side,
            "price": str(price),
            "state": "RESTING",
            "remaining_size": str(size),
            "timestamp": "1",
        }

    def cancelled(self, key: tuple[str, str, int], ref: str, reason: str) -> dict[str, Any]:
        strat_id, size = self.resting.pop(key)
        instrument, side, price = key
        return {
            "origin": "TEAM",
            "strat_id": strat_id,
            "instrument": instrument,
            "side": side,
            "price": str(price),
            "cancelled_size": str(size),
            "reason_code": reason,
            "request_ref": ref,
            "timestamp": "1",
        }


async def run_example(
    name: str, url: str | None, token: str | None, *args: str, **extra_env: str
) -> tuple[int, str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("QTE_")}
    env.update(extra_env)
    if url is not None:
        env["QTE_URL"] = url
    if token is not None:
        env["QTE_TOKEN"] = token
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(EXAMPLES_DIR / name),
        *args,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), RUN_LIMIT)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    assert process.returncode is not None
    return process.returncode, out.decode(), err.decode()


def synthetic_token() -> str:
    return secrets.token_urlsafe(32)


async def test_print_book_stops_after_its_message_count():
    exchange = FakeExchange()
    token = synthetic_token()
    async with serve_local(exchange) as url:
        code, out, err = await run_example(
            "print_book.py", url, token, "--instrument", INSTRUMENT, "--max-messages", "5"
        )
    assert code == 0, err
    assert "stopped after 5 messages" in out
    assert "book   TEST  99.950000 x 300  |  100.050000 x 200" in out
    # The unchanged session state counts as a message but is printed only once.
    assert out.count("market session 2026-01-05: OPEN") == 1
    # The fake sends the same book again at the same grid time: it is printed once.
    assert out.count("book   TEST") == 1
    assert exchange.received[0] == {
        "version": CONTRACT_VERSION,
        "type": "auth",
        "payload": {"token": token},
    }
    assert exchange.types() == ["auth", "subscribe"]
    assert token not in out + err


async def test_an_example_reads_the_address_and_token_from_dotenv_in_its_working_directory():
    exchange = FakeExchange()
    token = synthetic_token()
    async with serve_local(exchange) as url:
        dotenv = Path.cwd() / ".env"  # the conftest makes the working directory a fresh one
        dotenv.write_text(f"QTE_URL={url}\nQTE_TOKEN='{token}'\n")
        dotenv.chmod(0o600)
        code, out, err = await run_example(
            "print_book.py", None, None, "--instrument", INSTRUMENT, "--max-messages", "2"
        )
    assert code == 0, err
    assert exchange.received[0]["payload"] == {"token": token}
    assert token not in out + err


async def test_print_book_stops_after_its_duration():
    async with serve_local(FakeExchange()) as url:
        code, out, err = await run_example(
            "print_book.py", url, synthetic_token(), "--instrument", INSTRUMENT, "--seconds", "0.5"
        )
    assert code == 0, err
    assert "stopped after 0.5 seconds" in out


async def test_print_book_prints_the_official_close_outside_a_session():
    async with serve_local(FakeExchange(closed=True)) as url:
        code, out, err = await run_example(
            "print_book.py", url, synthetic_token(), "--instrument", INSTRUMENT, "--seconds", "0.5"
        )
    assert code == 0, err
    assert "market session 2026-01-05: CLOSED" in out
    assert "close  TEST  100.011000 on 2026-01-05" in out
    assert "book " not in out
    assert "stopped after 0.5 seconds (2 messages)" in out


# Exchange timestamps are milliseconds since the Unix epoch, UTC.
LAST_OPEN = 1_790_947_800_000  # 2026-10-02 13:30 UTC, 09:30 in New York
LAST_CLOSE = 1_790_971_200_000  # 2026-10-02 20:00 UTC
NEXT_OPEN = 1_791_207_000_000  # 2026-10-05 13:30 UTC, 09:30 in New York
NEXT_CLOSE = 1_791_230_400_000  # 2026-10-05 20:00 UTC
SERVER_TIME = 1_791_062_220_000  # 2026-10-03 21:17 UTC, 40 h 13 min before NEXT_OPEN

CALENDAR = {
    "term_first_session": "2026-10-02",
    "term_last_session": "2026-10-05",
    "sessions": [
        {"session_date": "2026-10-02", "open_time": str(LAST_OPEN), "close_time": str(LAST_CLOSE)},
        {"session_date": "2026-10-05", "open_time": str(NEXT_OPEN), "close_time": str(NEXT_CLOSE)},
    ],
}


def new_york_loads() -> bool:
    """Whether Python finds New York's time zone here, as the example's process will."""
    try:
        ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:
        return False
    return True


# How the example shows NEXT_OPEN, with New York time where the time zone data is present.
NEXT_OPEN_SHOWN = "2026-10-05 13:30 UTC" + (" (09:30 New York)" if new_york_loads() else "")


async def test_out_of_hours_shows_the_calendar_and_the_closed_market_with_no_close():
    # As on the exchange today: the closed state, and no official close after it. The state
    # names the last closed session and the next one, as the calendar does.
    state = {
        "state": "CLOSED",
        "session_date": "2026-10-02",
        "open_time": str(LAST_OPEN),
        "close_time": str(LAST_CLOSE),
        "grid_time": str(LAST_CLOSE),
        "next_session_date": "2026-10-05",
        "next_open_time": str(NEXT_OPEN),
        "next_close_time": str(NEXT_CLOSE),
    }
    exchange = FakeExchange(
        closed=True,
        official_close=False,
        calendar=CALENDAR,
        closed_state=state,
        server_time=SERVER_TIME,
    )
    async with serve_local(exchange) as url:
        code, out, err = await run_example(
            "out_of_hours.py", url, synthetic_token(), "--instrument", INSTRUMENT, "--seconds", "1"
        )
    assert code == 0, err
    new_york = " (17:17 New York)" if new_york_loads() else ""
    assert f"exchange time: 2026-10-03 21:17 UTC{new_york}" in out
    assert "last closed session: 2026-10-02" in out
    assert f"next session: 2026-10-05, opens {NEXT_OPEN_SHOWN}, in 40 h 13 min" in out
    assert "market session 2026-10-02: CLOSED" in out
    assert f"next open: {NEXT_OPEN_SHOWN}, in 40 h 13 min" in out
    assert "no official close: the exchange does not send it yet, as expected" in out
    assert exchange.types() == ["auth", "subscribe"]
    # Real times only: no raw count of milliseconds is printed.
    assert str(NEXT_OPEN) not in out and str(SERVER_TIME) not in out
    assert "time units" not in out


async def test_out_of_hours_measures_the_next_open_from_server_time_not_a_future_grid_time():
    # Before the exchange has closed any session, the reply names the next scheduled one,
    # and its grid_time equals that session's close_time, in the future.
    state = {
        "state": "CLOSED",
        "session_date": "2026-10-05",
        "open_time": str(NEXT_OPEN),
        "close_time": str(NEXT_CLOSE),
        "grid_time": str(NEXT_CLOSE),
        "next_session_date": "2026-10-05",
        "next_open_time": str(NEXT_OPEN),
        "next_close_time": str(NEXT_CLOSE),
    }
    exchange = FakeExchange(
        closed=True, official_close=False, closed_state=state, server_time=SERVER_TIME
    )
    async with serve_local(exchange) as url:
        code, out, err = await run_example(
            "out_of_hours.py", url, synthetic_token(), "--instrument", INSTRUMENT, "--seconds", "1"
        )
    assert code == 0, err
    # Measured from server_time, not from grid_time (which would give "6 h 30 min ago").
    assert f"next open: {NEXT_OPEN_SHOWN}, in 40 h 13 min" in out


@pytest.mark.skipif(
    importlib.util.find_spec("tzdata") is not None,
    reason="the tzdata package gives zoneinfo New York's time zone whatever PYTHONTZPATH says",
)
async def test_out_of_hours_shows_utc_only_where_python_has_no_time_zone_data():
    # An empty time zone search path, as on Windows without the tzdata package.
    exchange = FakeExchange(
        closed=True, official_close=False, calendar=CALENDAR, server_time=SERVER_TIME
    )
    async with serve_local(exchange) as url:
        code, out, err = await run_example(
            "out_of_hours.py",
            url,
            synthetic_token(),
            "--instrument",
            INSTRUMENT,
            "--seconds",
            "1",
            PYTHONTZPATH="",
        )
    assert code == 0, err
    assert "exchange time: 2026-10-03 21:17 UTC\n" in out
    assert "next session: 2026-10-05, opens 2026-10-05 13:30 UTC, in 40 h 13 min" in out
    assert "New York" not in out


def test_out_of_hours_falls_back_to_utc_when_new_york_cannot_be_loaded(
    monkeypatch: pytest.MonkeyPatch,
):
    example = load_example("out_of_hours.py")

    def no_zone(key: str) -> None:
        raise ZoneInfoNotFoundError(f"No time zone found with key {key}")

    monkeypatch.setattr(example, "ZoneInfo", no_zone)
    assert example.load_new_york() is None
    assert example.show_time(NEXT_OPEN, None) == "2026-10-05 13:30 UTC"


def test_out_of_hours_shows_new_york_time_and_its_date_when_it_differs():
    example = load_example("out_of_hours.py")
    if example.NEW_YORK is None:
        pytest.skip("no time zone data here (on Windows, install the tzdata package)")
    assert example.show_time(NEXT_OPEN) == "2026-10-05 13:30 UTC (09:30 New York)"
    # 02:00 UTC on 6 October is still the evening of 5 October in New York.
    late = NEXT_OPEN + (12 * 60 + 30) * 60_000
    assert example.show_time(late) == "2026-10-06 02:00 UTC (2026-10-05 22:00 New York)"
    # In January New York is five hours behind UTC, not four.
    january = 1_767_623_400_000  # 2026-01-05 14:30 UTC
    assert example.show_time(january) == "2026-01-05 14:30 UTC (09:30 New York)"


@pytest.mark.parametrize(
    ("milliseconds", "shown"),
    [
        (NEXT_OPEN - SERVER_TIME, "in 40 h 13 min"),
        (40 * 60_000 + 59_999, "in 40 min"),
        (59_999, "in less than a minute"),
        (0, "in less than a minute"),
        (-1, "less than a minute ago"),
        (-(2 * 60 + 5) * 60_000, "2 h 5 min ago"),
    ],
)
def test_out_of_hours_shows_a_wait_as_hours_and_whole_minutes(milliseconds, shown):
    example = load_example("out_of_hours.py")
    assert example.show_wait(to_timedelta(milliseconds)) == shown


async def test_out_of_hours_works_when_the_state_names_no_next_session():
    state = {"state": "CLOSED", "session_date": "2026-01-05", "close_time": "2", "grid_time": "2"}
    exchange = FakeExchange(closed=True, official_close=False, closed_state=state)
    async with serve_local(exchange) as url:
        code, out, err = await run_example(
            "out_of_hours.py", url, synthetic_token(), "--instrument", INSTRUMENT, "--seconds", "1"
        )
    assert code == 0, err
    assert "next open: not given in the session state" in out


async def test_out_of_hours_prints_an_official_close_and_works_without_a_calendar():
    async with serve_local(FakeExchange(closed=True)) as url:
        code, out, err = await run_example(
            "out_of_hours.py", url, synthetic_token(), "--instrument", INSTRUMENT, "--seconds", "1"
        )
    assert code == 0, err
    assert "calendar: none received" in out
    assert "official close TEST: 100.011000" in out
    assert "no official close" not in out


async def test_out_of_hours_stops_at_once_during_a_session():
    async with serve_local(FakeExchange(calendar=CALENDAR)) as url:
        code, out, err = await run_example(
            "out_of_hours.py", url, synthetic_token(), "--instrument", INSTRUMENT, "--seconds", "5"
        )
    assert code == 0, err
    assert "market session 2026-01-05: OPEN" in out
    assert "a session is under way: run this outside one" in out
    assert "stopped after" not in out


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("quote_both_sides.py", ("--strat-id", "closed-test", "--seconds", "0.5")),
        ("take_liquidity.py", ("--strat-id", "closed-test", "--side", "buy", "--seconds", "0.5")),
    ],
)
async def test_the_trading_examples_send_no_orders_outside_a_session(name: str, args: tuple):
    # With no book to act on, each waits out its time and sends nothing.
    exchange = FakeExchange(closed=True)
    async with serve_local(exchange) as url:
        code, out, err = await run_example(
            name, url, synthetic_token(), "--instrument", INSTRUMENT, *args
        )
    assert code == 0, out + err
    assert exchange.types() == ["auth", "subscribe"]


async def test_quote_both_sides_rests_amends_and_cancels_its_own_orders():
    exchange = FakeExchange(partial_fill=True)
    token = synthetic_token()
    async with serve_local(exchange) as url:
        code, out, err = await run_example(
            "quote_both_sides.py",
            url,
            token,
            *("--instrument", INSTRUMENT, "--strat-id", "quote-test", "--size", "2"),
            *("--seconds", "1.5", "--requote-seconds", "0.1", "--drain-seconds", "5"),
        )
    assert code == 0, out + err
    types = exchange.types()
    assert types.count("new") == 2  # one per side
    assert "amend" in types  # the partly filled side was topped back up
    assert types.count("cancel") == 2  # each side cancelled at the end
    assert "mass_cancel" not in types  # it only cancels its own orders
    assert exchange.resting == {}
    assert "all of this example's orders are cancelled" in out
    assert token not in out + err


async def test_quote_both_sides_cancels_and_re_enters_when_the_wall_moves():
    exchange = FakeExchange(move_bid_after=10)
    async with serve_local(exchange) as url:
        code, out, err = await run_example(
            "quote_both_sides.py",
            url,
            synthetic_token(),
            *("--instrument", INSTRUMENT, "--strat-id", "quote-test"),
            *("--seconds", "1.5", "--requote-seconds", "0.1", "--drain-seconds", "5"),
        )
    assert code == 0, out + err
    buys = [
        (m["type"], int(m["payload"]["price"]))
        for m in exchange.received
        if m["type"] in ("new", "cancel") and m["payload"]["side"] == "BUY"
    ]
    first, moved = BID + TICK, BID  # one tick inside the first and the moved best bid
    # Quoted one tick inside the wall, then cancelled and re-entered when the bid moved.
    assert buys == [("new", first), ("cancel", first), ("new", moved), ("cancel", moved)]
    assert "amend" not in exchange.types()  # a price move never amends
    assert exchange.resting == {}


async def test_quote_both_sides_prints_rejects_and_still_exits_cleanly():
    exchange = FakeExchange(reject_new="MARKET_CLOSED")
    async with serve_local(exchange) as url:
        code, out, err = await run_example(
            "quote_both_sides.py",
            url,
            synthetic_token(),
            *("--instrument", INSTRUMENT, "--strat-id", "quote-test"),
            *("--seconds", "1", "--requote-seconds", "0.2", "--drain-seconds", "1"),
        )
    assert code == 0, out + err
    assert "REJECTED new: MARKET_CLOSED" in out
    # Rejected sides are retried no faster than --requote-seconds allows.
    assert 2 <= exchange.types().count("new") <= 12


async def test_quote_both_sides_retries_a_reject_with_no_further_book():
    # One book and then none: the exchange publishes a book only when it changes.
    exchange = FakeExchange(reject_new="MESSAGE_BUDGET_EXCEEDED", book_once=True)
    async with serve_local(exchange) as url:
        code, out, err = await run_example(
            "quote_both_sides.py",
            url,
            synthetic_token(),
            *("--instrument", INSTRUMENT, "--strat-id", "quote-test"),
            *("--seconds", "1.5", "--requote-seconds", "0.2", "--drain-seconds", "1"),
        )
    assert code == 0, out + err
    assert exchange.books_sent == 1
    assert "REJECTED new: MESSAGE_BUDGET_EXCEEDED" in out
    sides = [m["payload"]["side"] for m in exchange.received if m["type"] == "new"]
    # Each side retried at least twice after its first reject, and no faster than
    # --requote-seconds allows: at most one new per side per 0.2 s of the 1.5 s.
    for side in ("BUY", "SELL"):
        assert 3 <= sides.count(side) <= 8, sides
    assert "all of this example's orders are cancelled" in out


async def test_quote_both_sides_re_enters_after_a_full_fill_with_no_further_book():
    exchange = FakeExchange(fill_first_buy=True, book_once=True)
    async with serve_local(exchange) as url:
        code, out, err = await run_example(
            "quote_both_sides.py",
            url,
            synthetic_token(),
            *("--instrument", INSTRUMENT, "--strat-id", "quote-test"),
            *("--seconds", "1.5", "--requote-seconds", "0.1", "--drain-seconds", "5"),
        )
    assert code == 0, out + err
    assert exchange.books_sent == 1
    buys = [
        (m["type"], int(m["payload"]["price"]))
        for m in exchange.received
        if m["type"] in ("new", "cancel") and m["payload"]["side"] == "BUY"
    ]
    # Filled completely, entered again from the book held, then cancelled at the end.
    assert buys == [("new", BID + TICK), ("new", BID + TICK), ("cancel", BID + TICK)]
    assert exchange.resting == {}
    assert "all of this example's orders are cancelled" in out


async def test_quote_both_sides_ignores_a_teammates_fill_at_its_level():
    exchange = FakeExchange(teammate_fill_first=True)
    # A teammate's order at another level, which the example must leave alone.
    exchange.resting[(INSTRUMENT, "BUY", BID)] = ("teammate", 7)
    async with serve_local(exchange) as url:
        code, out, err = await run_example(
            "quote_both_sides.py",
            url,
            synthetic_token(),
            *("--instrument", INSTRUMENT, "--strat-id", "quote-test"),
            *("--seconds", "1", "--requote-seconds", "0.1", "--drain-seconds", "5"),
        )
    assert code == 0, out + err
    cancels = [m["payload"] for m in exchange.received if m["type"] == "cancel"]
    # It still cancels both of its own orders at the end, and nothing else.
    assert sorted((c["side"], int(c["price"])) for c in cancels) == [
        ("BUY", BID + TICK),
        ("SELL", ASK - TICK),
    ]
    assert exchange.resting == {(INSTRUMENT, "BUY", BID): ("teammate", 7)}


async def test_quote_both_sides_stops_sending_when_messages_are_missed():
    exchange = FakeExchange(gap_after_resting=2)
    async with serve_local(exchange) as url:
        code, out, err = await run_example(
            "quote_both_sides.py",
            url,
            synthetic_token(),
            *("--instrument", INSTRUMENT, "--strat-id", "quote-test"),
            *("--seconds", "5", "--requote-seconds", "0.1"),
        )
    assert code == 1, out + err
    assert "can no longer tell which orders are its own" in out
    assert f"BUY {INSTRUMENT} @ 99.960000" in out
    assert exchange.types().count("new") == 2
    assert "cancel" not in exchange.types()  # it sends nothing more


async def test_quote_both_sides_sends_no_retry_after_missing_messages_while_cancelling():
    exchange = FakeExchange(reject_first_cancel_then_gap=True)
    async with serve_local(exchange) as url:
        # --requote-seconds is well above the book interval, so the gap is seen before a
        # retry is due even on a slow machine; a retry after it would still be caught.
        code, out, err = await run_example(
            "quote_both_sides.py",
            url,
            synthetic_token(),
            *("--instrument", INSTRUMENT, "--strat-id", "quote-test"),
            *("--seconds", "1", "--requote-seconds", "0.5", "--drain-seconds", "5"),
        )
    assert code == 1, out + err
    assert "REJECTED cancel: MIN_REST_VIOLATION" in out
    assert "can no longer tell which orders are its own" in out
    # The two cancels sent before the gap, and no retry of the rejected one after it.
    assert exchange.types().count("cancel") == 2


async def test_the_fake_rejects_a_duplicate_without_accepting_it():
    exchange = FakeExchange()
    exchange.resting[(INSTRUMENT, "BUY", BID + TICK)] = ("teammate", 7)
    async with serve_local(exchange) as url:
        code, out, err = await run_example(
            "quote_both_sides.py",
            url,
            synthetic_token(),
            *("--instrument", INSTRUMENT, "--strat-id", "quote-test"),
            *("--seconds", "0.5", "--requote-seconds", "1", "--drain-seconds", "5"),
        )
    assert code == 0, out + err
    assert "REJECTED new: DUPLICATE_ORDER_AT_LEVEL" in out
    assert out.count("accepted new") == 1  # only the sell side was accepted
    # The teammate's order at that level is untouched.
    assert exchange.resting == {(INSTRUMENT, "BUY", BID + TICK): ("teammate", 7)}


async def test_the_fakes_amends_keep_a_resting_order_view_right():
    exchange = FakeExchange()
    view = RestingOrders()
    # Bids rest strictly inside the wall's best bid and ask.
    first, second, third = BID + TICK, BID + 2 * TICK, BID + 3 * TICK
    async with serve_local(exchange) as url:
        session = await open_session(url, token=synthetic_token())
        async with session:
            events = aiter(session)

            async def answer() -> str:
                # Apply events until the one that settles the last message sent.
                while True:
                    event = await anext(events)
                    view.apply(event)
                    if isinstance(event, Received) and event.type == "order_state":
                        return OrderLifecycleState.Name(event.message.state)
                    if isinstance(event, Received) and event.type == "reject":
                        return reason_code_name(event.message.reason_code)

            for price in (first, second):
                await send_new(
                    session,
                    strat_id="s",
                    instrument=INSTRUMENT,
                    side=BUY,
                    order_type=LIMIT,
                    price=price,
                    size=5,
                )
                assert await answer() == "RESTING"
            amend = {"instrument": INSTRUMENT, "side": BUY, "new_size": 5}
            await send_amend(session, price=first, new_price=second, **amend)
            assert await answer() == "DUPLICATE_ORDER_AT_LEVEL"
            await send_amend(session, price=first, new_price=BID, **amend)
            assert await answer() == "AMEND_PRICE_AT_OR_BEYOND_WALL"
            assert {o.key.price for o in view} == {first, second}

            await send_amend(session, price=first, new_price=third, **amend)
            assert await answer() == "RESTING"
            assert {o.key.price for o in view} == {second, third}

            await send_amend(session, price=third, new_price=ASK, **amend)
            assert await answer() == "FILLED"  # against the wall
            assert {o.key.price for o in view} == {second}

            await send_amend(session, price=second, new_price=second, **{**amend, "new_size": 0})
            assert await answer() == "CANCELLED"  # cut to nothing
    assert len(view) == 0
    assert exchange.resting == {}


async def test_the_fake_accepts_a_cut_to_nothing_at_the_wall_without_a_price_test():
    # A bid resting at the wall's own price, as one can after the wall moves onto it. An
    # amend that keeps the price is not price-tested, so cutting it to nothing is accepted.
    exchange = FakeExchange()
    exchange.resting[(INSTRUMENT, "BUY", BID)] = ("s", 5)
    received: list[Received] = []
    async with asyncio.timeout(RUN_LIMIT), serve_local(exchange) as url:
        session = await open_session(url, token=synthetic_token())
        async with session:
            await send_amend(
                session, instrument=INSTRUMENT, side=BUY, price=BID, new_price=BID, new_size=0
            )
            async for event in session:
                if isinstance(event, Received) and event.type != "session_ack":
                    received.append(event)
                    if event.type in ("order_state", "reject"):
                        break
    assert [event.type for event in received] == ["accepted", "order_cancelled", "order_state"]
    cancelled, state = received[1].message, received[2].message
    assert reason_code_name(cancelled.reason_code) == "AMEND_CUT"
    assert cancelled.price == BID
    assert OrderLifecycleState.Name(state.state) == "CANCELLED"
    assert (state.price, state.old_price, state.remaining_size) == (BID, BID, 0)
    assert state.HasField("old_price")
    assert exchange.resting == {}


async def start_quoter(url: str, *args: str) -> asyncio.subprocess.Process:
    env = {k: v for k, v in os.environ.items() if not k.startswith("QTE_")}
    env.update(QTE_URL=url, QTE_TOKEN=synthetic_token(), PYTHONUNBUFFERED="1")
    return await asyncio.create_subprocess_exec(
        sys.executable,
        str(EXAMPLES_DIR / "quote_both_sides.py"),
        *("--instrument", INSTRUMENT, "--strat-id", "quote-test", *args),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def read_until(process: asyncio.subprocess.Process, text: bytes, seen: bytearray) -> None:
    """Read the example's output into `seen` until it contains `text`."""
    assert process.stdout is not None
    while text not in seen:
        line = await process.stdout.readline()
        assert line, seen.decode()
        seen += line


async def test_quote_both_sides_cancels_its_orders_on_ctrl_c():
    exchange = FakeExchange()
    async with serve_local(exchange) as url:
        process = await start_quoter(url, "--seconds", "60")
        seen = bytearray()
        try:
            async with asyncio.timeout(RUN_LIMIT):
                assert process.stdout is not None
                while seen.count(b"resting ") < 2:
                    line = await process.stdout.readline()
                    assert line, seen.decode()
                    seen += line
                process.send_signal(signal.SIGINT)
                out, err = await process.communicate()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    assert process.returncode == 0, (seen + out + err).decode()
    assert "all of this example's orders are cancelled" in out.decode()
    assert exchange.types().count("cancel") == 2
    assert exchange.resting == {}


async def until(condition: Callable[[], bool]) -> None:
    while not condition():
        await asyncio.sleep(0.01)


async def test_quote_both_sides_finishes_cancelling_on_ctrl_c_during_cleanup():
    # The cancels are confirmed only when the test says so, so the Ctrl+C lands while the
    # example waits for those confirmations.
    confirm = asyncio.Event()
    exchange = FakeExchange(confirm_cancels=confirm)
    async with serve_local(exchange) as url:
        process = await start_quoter(
            url, *("--seconds", "1", "--requote-seconds", "0.1", "--drain-seconds", "20")
        )
        seen = bytearray()
        try:
            async with asyncio.timeout(RUN_LIMIT):
                await read_until(process, b"cancelling this example's orders", seen)
                await until(lambda: exchange.types().count("cancel") == 2)
                process.send_signal(signal.SIGINT)
                await read_until(process, b"still cancelling", seen)
                confirm.set()
                out, err = await process.communicate()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    output = (seen + out).decode()
    assert process.returncode == 0, output + err.decode()
    assert "interrupted: still cancelling this example's orders" in output
    assert "all of this example's orders are cancelled" in output
    cancels = [m["payload"] for m in exchange.received if m["type"] == "cancel"]
    assert sorted((c["side"], int(c["price"])) for c in cancels) == [
        ("BUY", BID + TICK),
        ("SELL", ASK - TICK),
    ]
    assert exchange.resting == {}


async def test_quote_both_sides_stops_at_once_on_a_second_ctrl_c_during_cleanup():
    # Cancels are never confirmed, so without a second Ctrl+C cleanup would run for the
    # whole of --drain-seconds.
    exchange = FakeExchange(confirm_cancels=asyncio.Event())
    loop = asyncio.get_running_loop()
    async with serve_local(exchange) as url:
        process = await start_quoter(
            url, *("--seconds", "1", "--requote-seconds", "0.1", "--drain-seconds", "20")
        )
        seen = bytearray()
        try:
            async with asyncio.timeout(RUN_LIMIT):
                await read_until(process, b"cancelling this example's orders", seen)
                await until(lambda: exchange.types().count("cancel") == 2)
                process.send_signal(signal.SIGINT)
                await read_until(process, b"still cancelling", seen)
                process.send_signal(signal.SIGINT)
                stopping = loop.time()
                out, err = await process.communicate()
                stopped_after = loop.time() - stopping
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    assert process.returncode == 1, (seen + out + err).decode()
    assert "interrupted: this example's orders may still rest" in err.decode()
    assert stopped_after < 5, stopped_after  # well inside the 20 s drain
    assert "all of this example's orders are cancelled" not in (seen + out).decode()
    assert exchange.types().count("cancel") == 2


async def test_take_liquidity_sends_one_market_order_and_reports_the_fill():
    exchange = FakeExchange()
    async with serve_local(exchange) as url:
        code, out, err = await run_example(
            "take_liquidity.py",
            url,
            synthetic_token(),
            *("--instrument", INSTRUMENT, "--strat-id", "take-test", "--size", "3"),
            *("--seconds", "10"),
        )
    assert code == 0, out + err
    news = [m["payload"] for m in exchange.received if m["type"] == "new"]
    assert len(news) == 1
    assert news[0]["order_type"] == "MARKET" and "price" not in news[0]
    assert "FILL 3 @ 100.050000" in out
    assert "filled 3 of 3" in out


async def test_take_liquidity_acts_on_the_one_book_it_receives():
    exchange = FakeExchange(book_once=True)
    async with serve_local(exchange) as url:
        code, out, err = await run_example(
            "take_liquidity.py",
            url,
            synthetic_token(),
            *("--instrument", INSTRUMENT, "--strat-id", "take-test", "--seconds", "10"),
        )
    assert code == 0, out + err
    assert exchange.books_sent == 1
    assert exchange.types().count("new") == 1
    assert "filled 1 of 1" in out


async def test_take_liquidity_ignores_a_reject_that_is_not_its_own():
    exchange = FakeExchange(stray_reject=True)
    async with serve_local(exchange) as url:
        code, out, err = await run_example(
            "take_liquidity.py",
            url,
            synthetic_token(),
            *("--instrument", INSTRUMENT, "--strat-id", "take-test", "--seconds", "10"),
        )
    assert code == 0, out + err
    assert "REJECTED" not in out
    assert exchange.types().count("new") == 1
    assert "filled 1 of 1" in out


async def test_take_liquidity_prints_a_reject_and_exits_cleanly():
    exchange = FakeExchange(reject_new="NO_WALL_ON_SIDE")
    async with serve_local(exchange) as url:
        code, out, err = await run_example(
            "take_liquidity.py",
            url,
            synthetic_token(),
            *("--instrument", INSTRUMENT, "--strat-id", "take-test", "--seconds", "10"),
        )
    assert code == 0, out + err
    assert "REJECTED: NO_WALL_ON_SIDE" in out
    assert exchange.types().count("new") == 1


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
async def test_each_example_refuses_to_start_without_an_exchange_url(path: Path):
    code, out, err = await run_example(path.name, None, synthetic_token(), QTE_STRAT_ID="x")
    assert code == 2
    assert "QTE_URL" in err


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
async def test_each_example_refuses_to_start_without_a_token(path: Path):
    async with serve_local(FakeExchange()) as url:
        code, out, err = await run_example(path.name, url, None, QTE_STRAT_ID="x")
    assert code == 2
    assert "QTE_TOKEN" in err


@pytest.mark.parametrize("inside", ["0", "-0.01"])
async def test_quote_both_sides_refuses_an_inside_of_less_than_a_tick(inside: str):
    code, out, err = await run_example(
        "quote_both_sides.py", None, synthetic_token(), "--strat-id", "x", f"--inside={inside}"
    )
    assert code == 2
    assert "--inside must be at least one price tick" in err


def load_example(name: str) -> Any:
    """Import an example as a module, to test its parts in this process."""
    spec = importlib.util.spec_from_file_location(f"example_{Path(name).stem}", EXAMPLES_DIR / name)
    assert spec is not None and spec.loader is not None
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    return example


class StalledConnection:
    """A session whose every send writes its message and then never returns, as on a
    connection that has stopped taking data. Records each message's request_ref."""

    def __init__(self, sent: list[str]) -> None:
        self.sent = sent

    async def send(self, type_: str, payload: Any) -> None:
        self.sent.append(payload.request_ref)
        await asyncio.Event().wait()


def book_event() -> Received:
    book = BookMessage(
        instrument=INSTRUMENT,
        bid_levels=[WallLevel(price=BID, size=300)],
        ask_levels=[WallLevel(price=ASK, size=200)],
    )
    return Received("book", book, 1)


def session_state_event(phase: int = MarketSessionPhase.OPEN) -> Received:
    return Received("session_state", SessionStateMessage(state=phase), 1)


async def test_quote_both_sides_does_not_resend_a_cancel_that_ctrl_c_cut_short():
    # A send can stall after its message is written, for example on a full send buffer. A
    # Ctrl+C then must not make the example forget that message and send it again.
    example = load_example("quote_both_sides.py")
    sent: list[str] = []
    args = example.parse_args(["--instrument", INSTRUMENT, "--strat-id", "quote-test"])
    quoter = example.Quoter(StalledConnection(sent), None, args)
    quoter.resting = lambda quote: quote.price is not None  # reported resting
    buy = quoter.quotes[example.BUY]
    buy.price = BID + TICK

    cleanup = asyncio.create_task(quoter.cancel_own())
    try:
        async with asyncio.timeout(RUN_LIMIT):
            await until(lambda: len(sent) == 1)
            cleanup.cancel()  # Ctrl+C during the send
            with pytest.raises(asyncio.CancelledError):
                await cleanup

            assert buy.pending_ref == sent[0]  # recorded before it was sent
            buy.sent_at = float("-inf")  # however long cleanup waits, no second send
            quoter.view = []  # no order is reported gone yet
            assert await quoter.cancel_own() is False
    finally:
        cleanup.cancel()
    assert len(sent) == 1
    # The exchange's reply to that cancel is still matched to it.
    quoter.on_order_event(Accepted(request_ref=sent[0]))
    assert buy.pending_ref is None and buy.cancelling


async def test_quote_both_sides_keeps_to_drain_seconds_when_a_cancel_send_stalls():
    example = load_example("quote_both_sides.py")
    sent: list[str] = []
    args = example.parse_args(["--instrument", INSTRUMENT, "--strat-id", "quote-test"])
    view = RestingOrders()
    quoter = example.Quoter(StalledConnection(sent), view, args)
    quoter.resting = lambda quote: quote.price is not None  # reported resting
    quoter.quotes[example.BUY].price = BID + TICK
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with asyncio.timeout(RUN_LIMIT):
        outcome = await example.cancel_own_orders(quoter, view, asyncio.Queue(), started + 0.3)
    assert outcome == "unconfirmed"
    assert loop.time() - started < 2  # the drain deadline, not the stalled send, ended it
    assert len(sent) == 1


async def test_quote_both_sides_keeps_to_seconds_when_a_send_stalls_while_quoting():
    example = load_example("quote_both_sides.py")
    sent: list[str] = []
    args = example.parse_args(["--instrument", INSTRUMENT, "--strat-id", "quote-test"])
    view = RestingOrders()
    quoter = example.Quoter(StalledConnection(sent), view, args)
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait(session_state_event())
    queue.put_nowait(book_event())
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with asyncio.timeout(RUN_LIMIT):
        why = await example.quote_until(quoter, view, queue, 0.3)
    assert why == "time"
    assert loop.time() - started < 2  # --seconds, not the stalled send, ended it
    assert len(sent) == 1  # the first new order, which stalled
    assert quoter.quotes[example.BUY].pending_ref == sent[0]  # recorded all the same


class RecordingSession:
    """A session whose sends return at once. Records each message's type."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, type_: str, payload: Any) -> None:
        self.sent.append(type_)


async def test_quote_both_sides_sends_nothing_from_a_book_that_may_be_stale():
    example = load_example("quote_both_sides.py")
    session = RecordingSession()
    args = example.parse_args(["--instrument", INSTRUMENT, "--strat-id", "quote-test"])
    quoter = example.Quoter(session, RestingOrders(), args)
    quoter.market_open = True
    quoter.books.update(book_event().message)
    # A frame that may have been a newer book could not be read.
    example.handle(quoter, quoter.view, DecodeFailed("book", ValueError("unreadable")))
    await quoter.act()
    assert session.sent == []
    # A newer book clears it, and the example quotes again.
    newer = book_event().message
    newer.grid_time = 5
    example.handle(quoter, quoter.view, Received("book", newer, 2))
    await quoter.act()
    await quoter.act()  # one message per call
    assert session.sent == ["new", "new"]


@pytest.mark.parametrize(
    ("missed", "why"),
    [
        (SeqGap(expected=2, received=3), "unreliable"),
        (DecodeFailed("book", ValueError("unreadable")), "time"),
    ],
    ids=["seq-gap", "unreadable-book"],
)
async def test_quote_both_sides_applies_queued_events_before_it_sends(missed: object, why: str):
    # A book is held and both sides are free to send, but the reader has already queued
    # a sign that messages were missed. Nothing may be sent before that is applied: after
    # a gap the example sends nothing more, so an order sent first would be left resting.
    example = load_example("quote_both_sides.py")
    session = RecordingSession()
    args = example.parse_args(["--instrument", INSTRUMENT, "--strat-id", "quote-test"])
    view = RestingOrders()
    quoter = example.Quoter(session, view, args)
    quoter.market_open = True
    quoter.books.update(book_event().message)
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait(missed)
    async with asyncio.timeout(RUN_LIMIT):
        assert await example.quote_until(quoter, view, queue, 0.2) == why
    assert session.sent == []


class YieldingSession:
    """A session whose first send lets other tasks run before it returns, and during
    which the reader queues `arrives`. Records each message's side."""

    def __init__(self, queue: asyncio.Queue, arrives: object) -> None:
        self.queue = queue
        self.arrives = arrives
        self.sent: list[int] = []

    async def send(self, type_: str, payload: Any) -> None:
        self.sent.append(payload.side)
        if len(self.sent) == 1:
            self.queue.put_nowait(self.arrives)
        await asyncio.sleep(0)


@pytest.mark.parametrize(
    ("arrives", "why"),
    [
        (SeqGap(expected=2, received=3), "unreliable"),
        (DecodeFailed("book", ValueError("unreadable")), "time"),
        (session_state_event(MarketSessionPhase.CLOSED), "time"),
    ],
    ids=["seq-gap", "unreadable-book", "market-closed"],
)
async def test_quote_both_sides_applies_events_that_arrive_between_two_sends(
    arrives: object, why: str
):
    # While the first new order is being sent, a sign that it is no longer safe to quote
    # arrives. The second side must not be sent from what was known before.
    example = load_example("quote_both_sides.py")
    queue: asyncio.Queue = asyncio.Queue()
    session = YieldingSession(queue, arrives)
    args = example.parse_args(["--instrument", INSTRUMENT, "--strat-id", "quote-test"])
    view = RestingOrders()
    quoter = example.Quoter(session, view, args)
    queue.put_nowait(session_state_event())
    queue.put_nowait(book_event())
    async with asyncio.timeout(RUN_LIMIT):
        assert await example.quote_until(quoter, view, queue, 0.2) == why
    assert session.sent == [example.BUY]


async def test_quote_both_sides_sends_no_second_cancel_after_a_gap_during_the_first():
    example = load_example("quote_both_sides.py")
    queue: asyncio.Queue = asyncio.Queue()
    session = YieldingSession(queue, SeqGap(expected=2, received=3))
    args = example.parse_args(["--instrument", INSTRUMENT, "--strat-id", "quote-test"])
    view = RestingOrders()
    quoter = example.Quoter(session, view, args)
    quoter.resting = lambda quote: quote.price is not None  # reported resting
    quoter.quotes[example.BUY].price = BID + TICK
    quoter.quotes[example.SELL].price = ASK - TICK
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(RUN_LIMIT):
        outcome = await example.cancel_own_orders(quoter, view, queue, loop.time() + 1)
    assert outcome == "unreliable"
    assert session.sent == [example.BUY]


async def test_quote_both_sides_takes_the_two_sides_in_turn():
    # One message per step: if the side that just sent were always tried first, a side
    # whose replies come back quickly could keep the other from ever sending.
    example = load_example("quote_both_sides.py")
    session = RecordingSession()
    args = example.parse_args(["--instrument", INSTRUMENT, "--strat-id", "quote-test"])
    quoter = example.Quoter(session, RestingOrders(), args)
    quoter.resting = lambda quote: quote.price is not None  # reported resting
    buy = quoter.quotes[example.BUY]

    def rejected_and_ready_again() -> None:
        buy.pending_ref, buy.price, buy.sent_at = None, None, float("-inf")

    example.handle(quoter, quoter.view, session_state_event())
    example.handle(quoter, quoter.view, book_event())
    await quoter.act()
    rejected_and_ready_again()
    await quoter.act()
    assert session.sent == ["new", "new"]
    assert quoter.quotes[example.SELL].pending_ref is not None  # the second was SELL's

    # The same while cancelling at the end.
    session.sent.clear()
    quoter.quoting = False
    for quote in quoter.quotes.values():
        quote.pending_ref, quote.price, quote.sent_at = None, 1, float("-inf")
    quoter.last_side = example.SELL
    await quoter.cancel_own()
    buy.pending_ref, buy.sent_at = None, float("-inf")  # rejected, and ready again
    await quoter.cancel_own()
    assert session.sent == ["cancel", "cancel"]
    assert quoter.quotes[example.SELL].pending_ref is not None


async def test_quote_both_sides_stops_quoting_after_an_unreadable_session_state():
    example = load_example("quote_both_sides.py")
    session = RecordingSession()
    args = example.parse_args(["--instrument", INSTRUMENT, "--strat-id", "quote-test"])
    quoter = example.Quoter(session, RestingOrders(), args)
    example.handle(quoter, quoter.view, session_state_event())
    example.handle(quoter, quoter.view, book_event())
    # It may have said the market closed.
    unreadable = DecodeFailed("session_state", ValueError("unreadable"))
    example.handle(quoter, quoter.view, unreadable)
    await quoter.act()
    assert session.sent == []
    example.handle(quoter, quoter.view, session_state_event())  # open again
    await quoter.act()
    assert session.sent == ["new"]


def book_at(grid_time: int) -> Received:
    event = book_event()
    event.message.grid_time = grid_time
    return event


def state_at(open_time: int, phase: int = MarketSessionPhase.OPEN) -> Received:
    state = SessionStateMessage(state=phase, open_time=open_time)
    return Received("session_state", state, 1)


CLOSED = MarketSessionPhase.CLOSED
UNREADABLE_STATE = DecodeFailed("session_state", ValueError("unreadable"))
# Events across two sessions, the second opening at time 100, and whether a quote may be
# sent from the book held at the end. In the second session, only a book published at or
# after 100 may be quoted.
SESSIONS = {
    # As on subscribing during a session: the last book first, then the session state.
    "subscribed-mid-session": ([book_at(50), state_at(10)], True),
    "reopened-before-new-book": (
        [book_at(50), state_at(10), state_at(10, CLOSED), state_at(100)],
        False,
    ),
    "close-missed": ([book_at(50), state_at(10), state_at(100)], False),
    "close-unreadable": ([book_at(50), UNREADABLE_STATE, state_at(100)], False),
    "new-book-before-open-state": (
        [state_at(10), state_at(10, CLOSED), book_at(150), state_at(100)],
        True,
    ),
}


@pytest.mark.parametrize("name", SESSIONS)
async def test_quote_both_sides_quotes_only_a_book_of_the_session_in_progress(name: str):
    example = load_example("quote_both_sides.py")
    session = RecordingSession()
    args = example.parse_args(["--instrument", INSTRUMENT, "--strat-id", "quote-test"])
    quoter = example.Quoter(session, RestingOrders(), args)
    events, quotes = SESSIONS[name]
    for event in events:
        example.handle(quoter, quoter.view, event)
    await quoter.act()
    assert session.sent == (["new"] if quotes else [])


async def test_quote_both_sides_sends_nothing_while_the_market_is_closed():
    example = load_example("quote_both_sides.py")
    session = RecordingSession()
    args = example.parse_args(["--instrument", INSTRUMENT, "--strat-id", "quote-test"])
    view = RestingOrders()
    quoter = example.Quoter(session, view, args)
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait(session_state_event(MarketSessionPhase.CLOSED))
    queue.put_nowait(book_event())  # a book held from before the close
    async with asyncio.timeout(RUN_LIMIT):
        assert await example.quote_until(quoter, view, queue, 0.2) == "time"
    assert session.sent == []


async def test_quote_both_sides_applies_an_event_that_arrives_as_time_runs_out():
    # An `accepted` taken from the queue just after --seconds ran out must still be
    # applied, or the cleanup would not know the order it answers.
    example = load_example("quote_both_sides.py")
    session = RecordingSession()
    args = example.parse_args(["--instrument", INSTRUMENT, "--strat-id", "quote-test"])
    view = RestingOrders()
    quoter = example.Quoter(session, view, args)
    buy = quoter.quotes[example.BUY]
    buy.price = BID + TICK
    ref = quoter.record(buy, "new")
    accepted = Received("accepted", Accepted(request_ref=ref), 1)

    async def late_event(queue: asyncio.Queue, timeout: float) -> Any:
        await asyncio.sleep(max(timeout, 0) + 0.05)  # past the deadline
        return accepted

    example.next_event = late_event
    async with asyncio.timeout(RUN_LIMIT):
        why = await example.quote_until(quoter, view, asyncio.Queue(), 0.1)
    assert why == "time"
    assert buy.pending_ref is None  # the accepted was applied
    assert session.sent == []  # and nothing was sent once time was up


async def test_quote_both_sides_sends_nothing_once_time_is_up():
    example = load_example("quote_both_sides.py")
    session = RecordingSession()
    args = example.parse_args(["--instrument", INSTRUMENT, "--strat-id", "quote-test"])
    view = RestingOrders()
    quoter = example.Quoter(session, view, args)
    quoter.market_open = True
    quoter.books.update(book_event().message)  # a book held, and both sides free to send
    async with asyncio.timeout(RUN_LIMIT):
        why = await example.quote_until(quoter, view, asyncio.Queue(), 0)
    assert why == "time"
    assert session.sent == []


async def test_quote_both_sides_reads_events_with_a_very_short_requote_interval():
    example = load_example("quote_both_sides.py")
    session = RecordingSession()
    argv = ["--instrument", INSTRUMENT, "--strat-id", "quote-test", "--requote-seconds", "1e-6"]
    view = RestingOrders()
    quoter = example.Quoter(session, view, example.parse_args(argv))
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait(session_state_event())
    queue.put_nowait(book_event())
    async with asyncio.timeout(RUN_LIMIT):
        why = await example.quote_until(quoter, view, queue, 0.2)
    assert why == "time"
    assert queue.empty()
    assert session.sent == ["new", "new"]  # quoted from the book it read


async def test_take_liquidity_knows_it_may_have_sent_an_order_whose_send_stalled():
    example = load_example("take_liquidity.py")
    sent: list[str] = []
    args = example.parse_args(["--instrument", INSTRUMENT, "--strat-id", "take-test"])
    taker = example.Taker(StalledConnection(sent), args)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.3):  # as --seconds running out mid-send
            await taker.on_book(book_event().message)
    assert len(sent) == 1
    assert taker.ref == sent[0]  # so the example reports the outcome as not known yet
    # A reply to the order still counts as this example's.
    taker.on_order_event(Accepted(request_ref=sent[0]))
    fill = Execution(
        strat_id="take-test",
        instrument=INSTRUMENT,
        side=example.BUY,
        fill_price=ASK,
        fill_size=1,
        remaining_size=0,
    )
    taker.on_order_event(fill)
    assert taker.done and taker.filled == 1


class StalledSession:
    """A session whose close never finishes and on which no event ever arrives, as on a
    connection that has stopped taking data. With `stall_sends`, sends never return
    either. It stands in for `open_session`'s session, to run an example's own code."""

    def __init__(self, *, stall_sends: bool) -> None:
        self.info = SimpleNamespace(team="team-a", unscored=True)
        self.stall_sends = stall_sends
        self.sent: list[str] = []
        self.close_started = False

    async def send(self, type_: str, payload: Any) -> None:
        self.sent.append(type_)
        if self.stall_sends:
            await asyncio.Event().wait()

    async def close(self) -> None:
        self.close_started = True
        await asyncio.Event().wait()

    def __aiter__(self) -> Any:
        return self.events()

    async def events(self) -> Any:
        await asyncio.Event().wait()
        yield


@pytest.mark.parametrize("stall_sends", [True, False], ids=["send-and-close", "close"])
@pytest.mark.parametrize("name", ["print_book.py", "take_liquidity.py", "quote_both_sides.py"])
async def test_each_example_stops_in_time_on_a_connection_that_stalls(
    name: str, stall_sends: bool, capsys: pytest.CaptureFixture[str]
):
    example = load_example(name)
    example.CLOSE_SECONDS = 0.2
    session = StalledSession(stall_sends=stall_sends)
    argv = ["--instrument", INSTRUMENT, "--seconds", "0.3"]
    if name != "print_book.py":
        argv += ["--strat-id", "x"]
    args = example.parse_args(argv)
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with asyncio.timeout(RUN_LIMIT):
        if name == "quote_both_sides.py":
            # Its own part, so this test process does not take over Ctrl+C.
            await example.quote_and_clean_up(session, args, asyncio.current_task())
        else:

            async def stalled_open_session(url: str) -> StalledSession:
                return session

            example.open_session = stalled_open_session
            await example.run("ws://127.0.0.1:1/ws", args)
    assert loop.time() - started < 3  # --seconds plus the close bound, not forever
    assert session.sent[0] == "subscribe"
    assert session.close_started
    assert "did not close in time" in capsys.readouterr().out


@pytest.mark.parametrize("presses", [1, 2], ids=["interrupt", "interrupt-during-close"])
async def test_the_bounded_close_lets_an_interrupt_through(presses: int):
    # In quote_both_sides a Ctrl+C that stops the run cancels its task, and the close then
    # runs while that cancellation is on its way out. The close must not swallow it, and a
    # further Ctrl+C during the close must end it at once.
    example = load_example("quote_both_sides.py")
    example.CLOSE_SECONDS = 0.2 if presses == 1 else 20.0
    session = StalledSession(stall_sends=False)

    async def body() -> None:
        async with example.closing(session):
            await asyncio.Event().wait()

    loop = asyncio.get_running_loop()
    task = asyncio.create_task(body())
    started = loop.time()
    async with asyncio.timeout(RUN_LIMIT):
        await asyncio.sleep(0.05)
        task.cancel()
        await until(lambda: session.close_started)
        if presses == 2:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert loop.time() - started < 3


# The smoke test


SMOKE_TEST = "smoke_test.py"
CHECK_LINE = re.compile(r"(PASS|FAIL|SKIP)  (\S+) +(.*)")
TEST_ORDER = ("--place-test-order", "--strat-id", "smoke", "--tick", "0.01")


def checks(out: str) -> dict[str, tuple[str, str]]:
    """The smoke test's checks by name: each one's status and reason."""
    found: dict[str, tuple[str, str]] = {}
    for line in out.splitlines():
        match = CHECK_LINE.fullmatch(line)
        if match is not None:
            assert match[2] not in found, f"{match[2]} reported twice"
            found[match[2]] = (match[1], match[3])
    return found


async def run_smoke_test(
    exchange: FakeExchange, *args: str, token: str | None = None, **extra_env: str
) -> tuple[int, str, str, dict[str, tuple[str, str]]]:
    token = token or synthetic_token()
    async with serve_local(exchange) as url:
        code, out, err = await run_example(
            SMOKE_TEST, url, token, "--seconds", "1", *args, **extra_env
        )
    # Neither the token nor the address is ever shown.
    assert token not in out + err
    assert url not in out + err
    return code, out, err, checks(out)


async def test_the_smoke_test_checks_a_setup_during_a_session_and_sends_no_order():
    exchange = FakeExchange(calendar=CALENDAR, server_time=SERVER_TIME)
    code, out, err, found = await run_smoke_test(exchange, "--instruments", INSTRUMENT)
    assert code == 0, out + err
    assert found["token"] == ("PASS", "found in the QTE_TOKEN environment variable (not shown)")
    assert found["address"] == ("PASS", "set, from the QTE_URL environment variable (not shown)")
    assert found["connect"] == ("PASS", "authenticated as team team-a (unscored, contract 0.x)")
    # The fake acknowledges at SERVER_TIME, between the calendar's two sessions.
    assert found["calendar"] == (
        "PASS",
        "next open: session 2026-10-05, 2026-10-05 13:30 UTC, in 40 h 13 min; "
        "last closed: 2026-10-02",
    )
    assert found["session-state"] == ("PASS", "OPEN, session 2026-01-05")
    status, reason = found["market:TEST"]
    assert status == "PASS"
    assert reason.startswith("bid 99.950000 x 300, ask 100.050000 x 200 (LIVE); ")
    assert found["account"] == ("SKIP", "not answered by this exchange within 1 s")
    assert found["test-order"][0] == "SKIP"
    assert found["feed"][0] == "PASS"
    # The fake sends no heartbeats: never a FAIL, since the interval may outlast the run.
    status, reason = found["heartbeat"]
    assert status == "SKIP"
    assert reason.startswith("no heartbeat in ")
    assert reason.endswith(
        "s of reading; the exchange's interval may be longer than this run, try a larger --seconds"
    )
    status, reason = found["history"]
    assert status == "SKIP"
    assert "QTE_HISTORY_URL is not set" in reason
    assert out.splitlines()[-1] == "summary: 7 passed, 0 failed, 4 skipped"
    assert exchange.types() == ["auth", "subscribe", "account_query"]


async def test_the_smoke_test_reports_the_heartbeats_and_about_how_far_apart_they_came():
    # Heartbeats every 0.2 s, each stamped 0.2 s after the last on the exchange's clock.
    exchange = FakeExchange(calendar=CALENDAR, server_time=SERVER_TIME, heartbeat_every=0.2)
    code, out, err, found = await run_smoke_test(exchange, "--instruments", INSTRUMENT)
    assert code == 0, out + err
    status, reason = found["heartbeat"]
    assert status == "PASS"
    assert re.fullmatch(r"\d+ heartbeat\(s\), about 0\.2 s apart", reason), reason


@pytest.mark.parametrize(
    ("sent_at", "arrived_after", "expected"),
    [
        (None, 3.0, "3 heartbeat(s), about 1 s apart"),  # no send time: from arrival
        (1_000, 3.0, "3 heartbeat(s), about 1 s apart"),  # a send time before the ack
        (None, 0.0, "3 heartbeat(s)"),  # no usable span: no spacing named
    ],
    ids=["no-send-time", "send-time-unusable", "no-span"],
)
def test_the_smoke_tests_heartbeat_spacing_falls_back_to_arrival_times(
    sent_at: int | None, arrived_after: float, expected: str, capsys: pytest.CaptureFixture[str]
):
    smoke = load_example(SMOKE_TEST)
    opened_at = 100.0
    session = SimpleNamespace(
        heartbeats_received=3,
        last_heartbeat_sent_at=sent_at,
        last_heartbeat_at=opened_at + arrived_after,
        info=SimpleNamespace(server_time=5_000),
    )
    report = smoke.Report()
    smoke.check_heartbeat(report, session, opened_at)
    assert checks(capsys.readouterr().out)["heartbeat"] == ("PASS", expected)


async def test_the_smoke_tests_order_rests_inside_the_band_and_is_cancelled():
    # The wall shows 99.95 and 100.05: with a 0.01 tick, the order goes at 99.96.
    price = 99_960_000
    exchange = FakeExchange(calendar=CALENDAR, server_time=SERVER_TIME)
    code, out, err, found = await run_smoke_test(exchange, "--instruments", INSTRUMENT, *TEST_ORDER)
    assert code == 0, out + err
    kinds = ("new", "cancel", "amend", "mass_cancel")
    orders = [m for m in exchange.received if m["type"] in kinds]
    assert [m["type"] for m in orders] == ["new", "cancel"]
    new, cancel = (m["payload"] for m in orders)
    assert (new["strat_id"], new["instrument"], new["side"], new["order_type"]) == (
        "smoke",
        INSTRUMENT,
        "BUY",
        "LIMIT",
    )
    assert (int(new["size"]), int(new["price"])) == (1, price)
    # Exactly that level is cancelled.
    level = (cancel["instrument"], cancel["side"], int(cancel["price"]))
    assert level == (INSTRUMENT, "BUY", price)
    assert exchange.resting == {}
    status, reason = found["test-order"]
    assert status == "PASS"
    assert reason.startswith(f"BUY 1 TEST @ {to_decimal(price)} rested (accepted after ")
    assert reason.endswith("then was cancelled and the cancel confirmed")


async def test_the_smoke_test_sends_a_cancel_again_after_later_grid_points():
    exchange = FakeExchange(
        calendar=CALENDAR, server_time=SERVER_TIME, cancel_rejects=["MIN_REST_VIOLATION"] * 2
    )
    code, out, err, found = await run_smoke_test(exchange, "--instruments", INSTRUMENT, *TEST_ORDER)
    assert code == 0, out + err
    types = exchange.types()
    assert types.count("new") == 1
    assert types.count("cancel") == 3
    assert "mass_cancel" not in types
    assert exchange.resting == {}
    # Each rejected cancel is sent again only once the exchange has published more grid
    # points since the reject: one after the first, two after the second.
    log = exchange.log
    rejects = [i for i, entry in enumerate(log) if entry == ("sent", "reject")]
    cancels = [i for i, entry in enumerate(log) if entry == ("received", "cancel")]
    assert len(rejects) == 2
    for reject, again, grid_points in zip(rejects, cancels[1:], (1, 2), strict=True):
        assert log[reject:again].count(("sent", "session_state")) >= grid_points
    status, reason = found["test-order"]
    assert status == "PASS"
    assert "2 rejected cancel(s) (MIN_REST_VIOLATION, MIN_REST_VIOLATION) sent again" in reason


@pytest.mark.parametrize(
    "moves", [(100_010_000,), (100_010_000, 100_020_000)], ids=["once", "twice"]
)
async def test_the_smoke_test_fails_loudly_when_the_order_is_moved_away_from_its_level(
    moves: tuple[int, ...],
):
    # Another program of the team amends the resting test order to a new price, so a
    # cancel of the test order's own level would not touch it.
    exchange = FakeExchange(calendar=CALENDAR, server_time=SERVER_TIME, move_resting_to=moves)
    code, out, err, found = await run_smoke_test(exchange, "--instruments", INSTRUMENT, *TEST_ORDER)
    assert code == 1, out + err
    status, reason = found["test-order"]
    assert status == "FAIL"
    where = f"BUY TEST @ {to_decimal(moves[-1])}"
    assert f"may still be resting at {where}" in reason
    assert f"so the test order may still be resting at {where}, or elsewhere" in err
    assert exchange.resting == {(INSTRUMENT, "BUY", moves[-1]): ("smoke", 1)}
    # A cancel, if one went before the move was seen, names only the test order's level.
    cancels = [m["payload"] for m in exchange.received if m["type"] == "cancel"]
    assert all(int(cancel["price"]) == 99_960_000 for cancel in cancels)
    assert "mass_cancel" not in exchange.types()


async def test_the_smoke_test_still_warns_when_it_never_sees_the_order_accepted():
    # With no accepted, nothing at the level can be tied to the test order: the script
    # cancels the level all the same, and warns that the order may be anywhere.
    exchange = FakeExchange(calendar=CALENDAR, server_time=SERVER_TIME, withhold_accepted=True)
    code, out, err, found = await run_smoke_test(exchange, "--instruments", INSTRUMENT, *TEST_ORDER)
    assert code == 1, out + err
    status, reason = found["test-order"]
    assert status == "FAIL"
    assert "no reply to the order within 1 s" in reason
    assert "cannot be tied to the test order" in reason
    assert "WARNING: no reply to the test order (BUY 1 TEST @ 99.960000) was seen" in err
    assert exchange.types().count("cancel") == 1
    assert exchange.resting == {}


async def test_the_smoke_test_ignores_an_earlier_orders_fill_at_its_level():
    # While the test order is delayed, an earlier order of the same strategy at that level
    # fills and leaves it. That fill is not the test order's, which still rests and must be
    # cancelled.
    exchange = FakeExchange(calendar=CALENDAR, server_time=SERVER_TIME, teammate_fill_first="smoke")
    code, out, err, found = await run_smoke_test(exchange, "--instruments", INSTRUMENT, *TEST_ORDER)
    assert code == 0, out + err
    assert exchange.types().count("cancel") == 1
    assert exchange.resting == {}
    status, reason = found["test-order"]
    assert status == "PASS"
    assert "filled" not in reason


async def test_the_smoke_test_fails_loudly_when_it_cannot_confirm_the_cancel():
    # The cancel is accepted, but no order_cancelled ever follows.
    exchange = FakeExchange(
        calendar=CALENDAR, server_time=SERVER_TIME, confirm_cancels=asyncio.Event()
    )
    code, out, err, found = await run_smoke_test(exchange, "--instruments", INSTRUMENT, *TEST_ORDER)
    assert code == 1, out + err
    status, reason = found["test-order"]
    assert status == "FAIL"
    assert "the order may still be resting at BUY 1 TEST @ 99.960000" in reason
    assert "WARNING: the test order may still be resting" in err
    assert "mass_cancel" not in exchange.types()


@pytest.mark.parametrize(
    ("reason", "status", "code"),
    [("STRATEGY_NOT_REGISTERED", "FAIL", 1), ("MARKET_CLOSED", "SKIP", 0)],
)
async def test_the_smoke_test_reports_a_rejected_test_order_by_its_reason(
    reason: str, status: str, code: int
):
    exchange = FakeExchange(calendar=CALENDAR, server_time=SERVER_TIME, reject_new=reason)
    returned, out, err, found = await run_smoke_test(
        exchange, "--instruments", INSTRUMENT, *TEST_ORDER
    )
    assert returned == code, out + err
    assert found["test-order"][0] == status
    assert reason in found["test-order"][1]
    assert "cancel" not in exchange.types()


@pytest.mark.parametrize("order", [(), TEST_ORDER], ids=["read-only", "test-order-asked"])
@pytest.mark.parametrize(
    ("official_close", "expected"),
    [
        (False, ("SKIP", "no official close: not sent by this exchange")),
        (True, ("PASS", "official close 100.011000 for session 2026-01-05")),
    ],
    ids=["no-close", "close"],
)
async def test_the_smoke_test_outside_a_session_sees_the_closed_market_and_places_no_order(
    official_close: bool, expected: tuple[str, str], order: tuple[str, ...]
):
    exchange = FakeExchange(
        closed=True, official_close=official_close, calendar=CALENDAR, server_time=SERVER_TIME
    )
    code, out, err, found = await run_smoke_test(exchange, "--instruments", INSTRUMENT, *order)
    assert code == 0, out + err
    assert found["session-state"] == ("PASS", "CLOSED, session 2026-01-05")
    assert found["market:TEST"] == expected
    status, reason = found["test-order"]
    assert status == "SKIP"
    if order:
        assert reason == "the market session is CLOSED: it is placed only while OPEN"
    else:
        assert reason.startswith("not asked for")
    assert exchange.types() == ["auth", "subscribe", "account_query"]


async def test_the_smoke_test_skips_an_instrument_with_no_book_yet_during_a_session():
    # The fake publishes a book for TEST only, while its session state comes every interval:
    # OTHER is like an instrument with no valid quote yet, which has no book.
    exchange = FakeExchange(calendar=CALENDAR, server_time=SERVER_TIME)
    code, out, err, found = await run_smoke_test(
        exchange, "--instruments", INSTRUMENT, "OTHER", "--book-wait", "1.5"
    )
    assert code == 0, out + err
    assert found["market:TEST"][0] == "PASS"
    assert found["market:OTHER"] == (
        "SKIP",
        "no book published within 1.5 s (the instrument may have no valid quote yet)",
    )


async def test_the_smoke_test_waits_for_an_instruments_first_book_during_a_session():
    # The first book comes about 1.5 s into the session, after the --seconds window.
    exchange = FakeExchange(calendar=CALENDAR, server_time=SERVER_TIME, first_book_after=30)
    loop = asyncio.get_running_loop()
    started = loop.time()
    code, out, err, found = await run_smoke_test(
        exchange, "--instruments", INSTRUMENT, "--book-wait", "20"
    )
    assert code == 0, out + err
    status, reason = found["market:TEST"]
    assert status == "PASS"
    assert reason.startswith("bid 99.950000 x 300, ask 100.050000 x 200 (LIVE); ")
    # It stops waiting once the book is there, well before --book-wait is up.
    assert loop.time() - started < 15


@pytest.mark.parametrize("book_wait", ["-1", "601", "inf", "nan"])
async def test_the_smoke_test_keeps_its_book_wait_bounded(book_wait: str):
    code, out, err = await run_example(
        SMOKE_TEST, None, synthetic_token(), f"--book-wait={book_wait}"
    )
    assert code == 2
    assert "--book-wait must be from 0 to 600 seconds" in err


@pytest.mark.parametrize("echo", ["echo", "bare"], ids=["ref-echoed", "no-ref"])
async def test_the_smoke_test_skips_an_account_query_refused_as_an_unknown_type(echo: str):
    # An older exchange refuses a message type it does not know: MALFORMED_MESSAGE naming no
    # request type, with or without the request_ref it could read.
    exchange = FakeExchange(calendar=CALENDAR, server_time=SERVER_TIME, account_unknown=echo)
    code, out, err, found = await run_smoke_test(exchange, "--instruments", INSTRUMENT)
    assert code == 0, out + err
    assert found["account"] == (
        "SKIP",
        "refused as a message type this exchange does not know (MALFORMED_MESSAGE): it does "
        "not serve the query yet",
    )


async def test_the_smoke_test_fails_a_refused_account_query():
    exchange = FakeExchange(
        calendar=CALENDAR, server_time=SERVER_TIME, account_reject="MALFORMED_MESSAGE"
    )
    code, out, err, found = await run_smoke_test(exchange, "--instruments", INSTRUMENT)
    assert code == 1, out + err
    assert found["account"] == ("FAIL", "refused: MALFORMED_MESSAGE")


async def test_the_smoke_test_never_shows_a_rejects_free_text():
    # A reject's reason_detail is the exchange's own text, which can name the team's
    # figures against a risk limit: only the reason code is shown.
    detail = "gross 123456789 of 100000000"
    exchange = FakeExchange(
        calendar=CALENDAR,
        server_time=SERVER_TIME,
        reject_new="RISK_LIMIT_BREACH",
        reject_detail=detail,
    )
    code, out, err, found = await run_smoke_test(exchange, "--instruments", INSTRUMENT, *TEST_ORDER)
    assert code == 1, out + err
    assert found["test-order"] == ("FAIL", "BUY 1 TEST @ 99.960000 rejected: RISK_LIMIT_BREACH")
    assert "123456789" not in out + err


async def test_the_smoke_test_fails_an_instrument_the_exchange_does_not_know():
    exchange = FakeExchange(
        calendar=CALENDAR, server_time=SERVER_TIME, known_instruments=frozenset({INSTRUMENT})
    )
    code, out, err, found = await run_smoke_test(exchange, "--instruments", INSTRUMENT, "NOPE")
    assert code == 1, out + err
    assert found["market:NOPE"] == ("FAIL", "the exchange does not know it: UNKNOWN_INSTRUMENT")
    assert found["market:TEST"][0] == "PASS"
    # One subscribe each, so the unknown one cannot keep the other from being served.
    assert exchange.types() == ["auth", "subscribe", "subscribe", "account_query"]


async def test_the_smoke_test_says_the_account_query_was_answered_without_its_figures():
    state = {
        "valuation_basis": "LIVE_MARK",
        "cash": "123456789",
        "positions": [{"instrument": INSTRUMENT, "quantity": "7", "price": "100000000"}],
    }
    exchange = FakeExchange(calendar=CALENDAR, server_time=SERVER_TIME, account_state=state)
    code, out, err, found = await run_smoke_test(exchange, "--instruments", INSTRUMENT)
    assert code == 0, out + err
    assert found["account"] == (
        "PASS",
        "answered, valued at LIVE_MARK, without a summary (figures not shown)",
    )
    assert "123.456789" not in out
    assert "123456789" not in out


async def test_the_smoke_test_reads_the_last_closed_sessions_books_from_history():
    token = synthetic_token()
    books = history_book(1) + history_book(2, bid="99960000")
    fake = FakeHistory(token=token, objects={("2026-10-02", INSTRUMENT, "book"): books})
    with serve_history(fake) as history_url:
        code, out, err, found = await run_smoke_test(
            FakeExchange(calendar=CALENDAR, server_time=SERVER_TIME),
            "--instruments",
            INSTRUMENT,
            token=token,
            QTE_HISTORY_URL=history_url,
        )
    assert code == 0, out + err
    assert found["history:TEST"] == (
        "PASS",
        "session 2026-10-02: first book bid 99.950000 x 300, ask 100.050000 x 200 (LIVE); "
        "only its start was read, not checked against its digest",
    )
    assert [path for path, _ in fake.requests] == ["/v1/history/2026-10-02/TEST/book"]


async def test_the_smoke_test_never_repeats_an_unusable_history_address():
    # A value pasted into the wrong place could be a secret: the address is not shown.
    secret = synthetic_token()
    code, out, err, found = await run_smoke_test(
        FakeExchange(calendar=CALENDAR, server_time=SERVER_TIME),
        "--instruments",
        INSTRUMENT,
        QTE_HISTORY_URL=f"https://127.0.0.1:{secret}/",
    )
    assert code == 1, out + err
    status, reason = found["history"]
    assert status == "FAIL"
    assert reason.startswith("cannot use QTE_HISTORY_URL (ValueError)")
    assert secret not in out + err


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on the PATH")
async def test_the_smoke_test_fails_a_dotenv_that_git_does_not_ignore():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "QTE_"))}
    # The conftest makes the working directory a fresh one: make it a git repository.
    subprocess.run(["git", "init", "-q", "."], check=True, capture_output=True, env=env)
    exchange = FakeExchange(calendar=CALENDAR, server_time=SERVER_TIME)
    token = synthetic_token()
    async with serve_local(exchange) as url:
        dotenv = Path.cwd() / ".env"
        dotenv.write_text(f"QTE_URL={url}\nQTE_TOKEN='{token}'\n")
        dotenv.chmod(0o600)
        code, out, err = await run_example(
            SMOKE_TEST, None, None, "--instruments", INSTRUMENT, "--seconds", "1"
        )
    assert code == 1, out + err
    found = checks(out)
    assert found["token"] == ("PASS", f"found in {dotenv} (not shown)")
    assert found["address"] == ("PASS", f"set, from {dotenv} (not shown)")
    status, reason = found["dotenv"]
    assert status == "FAIL"
    assert ".gitignore" in reason
    assert found["connect"][0] == "PASS"
    assert token not in out + err
    assert url not in out + err


async def test_the_smoke_test_needs_a_strategy_for_the_test_order():
    code, out, err = await run_example(SMOKE_TEST, None, synthetic_token(), "--place-test-order")
    assert code == 2
    assert "--place-test-order needs --strat-id" in err


def smoke_book(bid: int, ask: int, student_asks: tuple[int, ...] = ()) -> BookMessage:
    return BookMessage(
        instrument=INSTRUMENT,
        bid_levels=[WallLevel(price=bid, size=1)],
        ask_levels=[WallLevel(price=ask, size=1)],
        student_ask_levels=[StudentLevel(price=price, size=1) for price in student_asks],
        condition=LIVE,
    )


def test_the_smoke_tests_order_price_is_a_tick_above_the_wall_bid_and_three_below_the_asks():
    smoke = load_example(SMOKE_TEST)
    assert smoke.probe_price(smoke_book(99_950_000, 100_050_000), TICK) == (99_960_000, "")
    # Exactly three ticks of room below the ask is enough; two is not.
    assert smoke.probe_price(smoke_book(99_950_000, 99_990_000), TICK) == (99_960_000, "")
    price, why = smoke.probe_price(smoke_book(99_950_000, 99_980_000), TICK)
    assert price is None
    assert "fewer than 3 ticks" in why
    # A one-tick spread leaves no room strictly inside the band at all.
    price, _ = smoke.probe_price(smoke_book(100_000_000, 100_010_000), TICK)
    assert price is None
    # A resting sell counts as an ask too.
    price, _ = smoke.probe_price(smoke_book(99_950_000, 100_050_000, (99_980_000,)), TICK)
    assert price is None
    # A one-sided book has no band to rest inside.
    one_sided = smoke_book(99_950_000, 100_050_000)
    del one_sided.ask_levels[:]
    price, why = smoke.probe_price(one_sided, TICK)
    assert price is None
    assert "not two-sided" in why


async def test_the_smoke_test_fails_and_names_the_position_when_its_order_fills():
    # The asks fall to the order's price while it is delayed, so it trades on release.
    exchange = FakeExchange(calendar=CALENDAR, server_time=SERVER_TIME, cross_new_at=99_960_000)
    code, out, err, found = await run_smoke_test(exchange, "--instruments", INSTRUMENT, *TEST_ORDER)
    assert code == 1, out + err
    status, reason = found["test-order"]
    assert status == "FAIL"
    assert "filled 1 share(s): a real trade, so your team now holds that position" in reason
    assert "WARNING: the test order filled: your team bought 1 TEST at 99.960000" in err
    assert "cancel" not in exchange.types()


async def test_the_smoke_test_skips_when_the_wall_moves_onto_its_order():
    # The wall's bid rises to the order's price during the delay: nothing rests.
    exchange = FakeExchange(calendar=CALENDAR, server_time=SERVER_TIME, outside_band=True)
    code, out, err, found = await run_smoke_test(exchange, "--instruments", INSTRUMENT, *TEST_ORDER)
    assert code == 0, out + err
    status, reason = found["test-order"]
    assert status == "SKIP"
    assert "the wall moved during the order delay, so nothing rested; run it again" in reason
    assert "WARNING" not in err


async def test_the_smoke_test_keeps_warning_when_missed_reports_may_hide_a_move():
    # Another program moves the order, but the client never sees that order_state (a gap):
    # the cancel at the order's first level finds nothing there.
    exchange = FakeExchange(
        calendar=CALENDAR, server_time=SERVER_TIME, move_resting_to=(100_010_000,), hide_moves=True
    )
    code, out, err, found = await run_smoke_test(exchange, "--instruments", INSTRUMENT, *TEST_ORDER)
    assert code == 1, out + err
    status, reason = found["test-order"]
    assert status == "FAIL"
    assert "so it may rest elsewhere; check your team's orders" in reason
    assert "WARNING: reports about the test order (BUY 1 TEST @ 99.960000) were missed" in err
    assert exchange.resting == {(INSTRUMENT, "BUY", 100_010_000): ("smoke", 1)}


@pytest.mark.parametrize("allow", [False, True], ids=["refused", "allowed"])
async def test_the_smoke_test_places_its_order_in_a_scored_session_only_when_allowed(allow: bool):
    exchange = FakeExchange(calendar=CALENDAR, server_time=SERVER_TIME, unscored=False)
    extra = ("--allow-scored",) if allow else ()
    code, out, err, found = await run_smoke_test(
        exchange, "--instruments", INSTRUMENT, *TEST_ORDER, *extra
    )
    assert code == 0, out + err
    assert found["connect"][1] == "authenticated as team team-a (scored, contract 0.x)"
    status, reason = found["test-order"]
    if allow:
        assert status == "PASS"
        assert exchange.types().count("new") == 1
    else:
        assert status == "SKIP"
        assert reason.startswith("this is a scored session")
        assert "new" not in exchange.types()


async def test_the_smoke_test_places_no_order_during_an_outage():
    exchange = FakeExchange(calendar=CALENDAR, server_time=SERVER_TIME, outage=True)
    code, out, err, found = await run_smoke_test(exchange, "--instruments", INSTRUMENT, *TEST_ORDER)
    assert code == 0, out + err
    assert found["session-state"] == (
        "PASS",
        "OPEN, session 2026-01-05; an exchange outage is in force",
    )
    assert found["test-order"][0] == "SKIP"
    assert "outage" in found["test-order"][1]
    assert "new" not in exchange.types()


@pytest.mark.parametrize(
    ("args", "shown"),
    [
        (("--token", "{secret}"), "unrecognized arguments: --token (any values not shown)"),
        (("--token={secret}",), "unrecognized arguments: --token (any values not shown)"),
        (("--token", "--{secret}"), "unrecognized arguments: --token (any values not shown)"),
        (("{secret}",), "unrecognized arguments (not shown)"),
        (("--tick", "{secret}"), "argument --tick: value not accepted (not shown)"),
        (("--tick", "-{secret}"), "argument --tick: expected one argument"),
        (("--seconds", "{secret}"), "argument --seconds: value not accepted (not shown)"),
        (
            ("--place-test-order={secret}",),
            "argument --place-test-order: value not accepted (not shown)",
        ),
    ],
    ids=[
        "option-and-value",
        "option-equals-value",
        "value-like-an-option",
        "bare-value",
        "tick",
        "value-with-a-hyphen",
        "seconds",
        "value-for-a-flag",
    ],
)
async def test_the_smoke_tests_argument_errors_never_repeat_what_was_typed(
    args: tuple[str, ...], shown: str
):
    # A token typed on the command line by mistake must not come back in the error. Mixed
    # case and digits, as a token has, and no leading hyphen unless a case adds one.
    secret = f"SeCrEt{secrets.token_hex(16)}"
    typed = [arg.replace("{secret}", secret) for arg in args]
    code, out, err = await run_example(SMOKE_TEST, None, synthetic_token(), *typed)
    assert code == 2
    assert shown in err
    assert secret not in out + err


async def test_the_smoke_test_treats_everything_after_a_double_dash_as_a_value():
    # After "--", a word shaped like an option is a value all the same, so it is not named.
    code, out, err = await run_example(SMOKE_TEST, None, synthetic_token(), "--", "--private-value")
    assert code == 2
    assert "unrecognized arguments (not shown)" in err
    assert "private-value" not in out + err


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--place-test-order", "--strat-id", "x"), "--place-test-order needs --tick"),
        (("--pl", "--strat-id", "x", "--tick", "0.01"), "unrecognized arguments: --pl"),
    ],
    ids=["no-tick", "abbreviated"],
)
async def test_the_smoke_test_places_an_order_only_when_asked_in_full(
    args: tuple[str, ...], message: str
):
    code, out, err = await run_example(SMOKE_TEST, None, synthetic_token(), *args)
    assert code == 2
    assert message in err


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("TEAM_DISABLED", "the exchange refused the session: TEAM_DISABLED"),
        (
            "VERSION_MISMATCH",
            "the exchange does not serve this SDK's contract version (VERSION_MISMATCH): "
            "update the SDK",
        ),
    ],
)
async def test_the_smoke_test_shows_a_refused_session_by_its_reason_name_only(
    reason: str, expected: str
):
    detail = synthetic_token()
    refusal = {"reason_code": reason, "reason_detail": detail}
    code, out, err, found = await run_smoke_test(FakeExchange(session_reject=refusal))
    assert code == 1, out + err
    assert found["connect"] == ("FAIL", expected)
    assert detail not in out + err


@pytest.mark.parametrize("kind", ["credentials-in-address", "refused-port"])
async def test_the_smoke_test_shows_a_connect_failure_by_its_kind_only(kind: str):
    secrets_in_it = [synthetic_token() for _ in range(3)]
    user, port, query = secrets_in_it
    if kind == "credentials-in-address":
        url = f"ws://user:{user}@127.0.0.1:{port}/ws?token={query}"
    else:
        url, secrets_in_it = "ws://127.0.0.1:1/ws", ["127.0.0.1", "ws://"]
    code, out, err = await run_example(SMOKE_TEST, url, synthetic_token(), "--seconds", "1")
    assert code == 1, out + err
    status, reason = checks(out)["connect"]
    assert status == "FAIL"
    assert reason.startswith("could not connect (")
    for text in secrets_in_it:
        assert text not in out + err


async def test_the_smoke_test_refuses_a_plain_address_for_another_host():
    # A plain ws:// address would send the token unencrypted to that host.
    code, out, err = await run_example(
        SMOKE_TEST, "ws://exchange.example:8080/ws", synthetic_token(), "--seconds", "1"
    )
    assert code == 2
    status, reason = checks(out)["address"]
    assert status == "FAIL"
    assert "would send your token unencrypted" in reason
    assert "exchange.example" not in out + err


async def test_the_smoke_test_shows_a_history_error_by_kind_and_status_only():
    token = synthetic_token()
    words = synthetic_token()
    fake = FakeHistory(token=token, error_message=words)  # it serves nothing: 404 unavailable
    with serve_history(fake) as history_url:
        code, out, err, found = await run_smoke_test(
            FakeExchange(calendar=CALENDAR, server_time=SERVER_TIME),
            "--instruments",
            INSTRUMENT,
            token=token,
            QTE_HISTORY_URL=history_url,
        )
    assert code == 1, out + err
    assert found["history:TEST"] == (
        "FAIL",
        "session 2026-10-02: refused (HistoryUnavailable, HTTP 404)",
    )
    assert words not in out + err


def default_signals() -> None:
    """In the child, before it starts: the stop signals as a terminal gives them. A shell
    that runs the tests in the background, or under nohup, can start them with SIGINT or
    SIGHUP ignored, and a child would inherit that."""
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        signal.signal(getattr(signal, name), signal.SIG_DFL)


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="POSIX signals")
@pytest.mark.parametrize(
    ("signal_name", "code"), [("SIGHUP", 128 + 1), ("SIGTERM", 128 + 15)], ids=["sighup", "sigterm"]
)
async def test_the_smoke_test_cancels_its_orders_level_even_when_its_terminal_is_gone(
    signal_name: str, code: int
):
    # A closed terminal: writes to stdout and stderr fail, and their buffers cannot be
    # flushed at exit. The cleanup cancel must be sent all the same, and the exit status
    # must still be the signal's.
    confirm = asyncio.Event()
    exchange = FakeExchange(calendar=CALENDAR, server_time=SERVER_TIME, confirm_cancels=confirm)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("QTE_", "PYTHONUNBUFFERED"))}
    out_reader, out_writer = os.pipe()
    err_reader, err_writer = os.pipe()
    readers = [out_reader, err_reader]
    try:
        async with serve_local(exchange) as url:
            env.update(QTE_URL=url, QTE_TOKEN=synthetic_token())
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(EXAMPLES_DIR / SMOKE_TEST),
                *("--instruments", INSTRUMENT, "--seconds", "3", *TEST_ORDER),
                env=env,
                stdout=out_writer,
                stderr=err_writer,
                preexec_fn=default_signals,
            )
            os.close(out_writer)
            os.close(err_writer)
            try:
                async with asyncio.timeout(RUN_LIMIT):
                    await until(lambda: exchange.types().count("cancel") == 1)
                    for fd in readers:  # nothing reads its output any more
                        os.close(fd)
                    readers = []
                    process.send_signal(getattr(signal, signal_name))
                    await until(lambda: exchange.types().count("cancel") == 2)
                    confirm.set()
                    await process.wait()
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
    finally:
        for fd in readers:
            os.close(fd)
    assert process.returncode == code
    cancels = [m["payload"] for m in exchange.received if m["type"] == "cancel"]
    assert [(c["side"], int(c["price"])) for c in cancels] == [("BUY", 99_960_000)] * 2
    assert "mass_cancel" not in exchange.types()
    assert exchange.resting == {}


def ignored_sighup() -> None:
    default_signals()
    signal.signal(signal.SIGHUP, signal.SIG_IGN)


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="POSIX signals")
async def test_the_smoke_test_leaves_an_ignored_sighup_ignored():
    # Started under nohup, SIGHUP is ignored: the run goes on to its end.
    exchange = FakeExchange(calendar=CALENDAR, server_time=SERVER_TIME)
    env = {k: v for k, v in os.environ.items() if not k.startswith("QTE_")}
    async with serve_local(exchange) as url:
        env.update(QTE_URL=url, QTE_TOKEN=synthetic_token(), PYTHONUNBUFFERED="1")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(EXAMPLES_DIR / SMOKE_TEST),
            *("--instruments", INSTRUMENT, "--seconds", "1"),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=ignored_sighup,
        )
        try:
            async with asyncio.timeout(RUN_LIMIT):
                await until(lambda: "subscribe" in exchange.types())
                process.send_signal(signal.SIGHUP)
                out, err = await process.communicate()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    assert process.returncode == 0, out.decode() + err.decode()
    assert "summary: " in out.decode()


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="POSIX signals")
@pytest.mark.parametrize(
    ("signal_name", "code"),
    [("SIGINT", 130), ("SIGTERM", 128 + 15), ("SIGHUP", 128 + 1)],
    ids=["ctrl-c", "sigterm", "sighup"],
)
async def test_the_smoke_test_cancels_its_orders_level_when_interrupted(
    signal_name: str, code: int
):
    # The cancel is accepted but confirmed only when the test says so, so the signal lands
    # while the order may still rest. Ctrl+C, a kill and a closed terminal all stop the run
    # the same way.
    confirm = asyncio.Event()
    exchange = FakeExchange(calendar=CALENDAR, server_time=SERVER_TIME, confirm_cancels=confirm)
    env = {k: v for k, v in os.environ.items() if not k.startswith("QTE_")}
    async with serve_local(exchange) as url:
        env.update(QTE_URL=url, QTE_TOKEN=synthetic_token(), PYTHONUNBUFFERED="1")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(EXAMPLES_DIR / SMOKE_TEST),
            *("--instruments", INSTRUMENT, "--seconds", "3", *TEST_ORDER),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=default_signals,
        )
        try:
            async with asyncio.timeout(RUN_LIMIT):
                await until(lambda: exchange.types().count("cancel") == 1)
                process.send_signal(getattr(signal, signal_name))
                # It cancels the same level again before stopping; then the exchange confirms.
                await until(lambda: exchange.types().count("cancel") == 2)
                confirm.set()
                out, err = await process.communicate()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    stderr = err.decode()
    assert process.returncode == code, out.decode() + stderr
    assert "interrupted: cancelling the test order's level (BUY 1 TEST @ 99.960000)" in stderr
    assert "may still" not in stderr
    cancels = [m["payload"] for m in exchange.received if m["type"] == "cancel"]
    assert [(c["side"], int(c["price"])) for c in cancels] == [("BUY", 99_960_000)] * 2
    assert "mass_cancel" not in exchange.types()
    assert exchange.resting == {}


async def test_the_smoke_tests_cleanup_after_an_interruption_is_bounded():
    # The cancels are never confirmed: the cleanup gives up after its bound and warns.
    exchange = FakeExchange(
        calendar=CALENDAR, server_time=SERVER_TIME, confirm_cancels=asyncio.Event()
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("QTE_")}
    loop = asyncio.get_running_loop()
    async with serve_local(exchange) as url:
        env.update(QTE_URL=url, QTE_TOKEN=synthetic_token(), PYTHONUNBUFFERED="1")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(EXAMPLES_DIR / SMOKE_TEST),
            *("--instruments", INSTRUMENT, "--seconds", "2", *TEST_ORDER),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=default_signals,
        )
        try:
            async with asyncio.timeout(RUN_LIMIT):
                await until(lambda: exchange.types().count("cancel") == 1)
                process.send_signal(signal.SIGINT)
                interrupted = loop.time()
                out, err = await process.communicate()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    stderr = err.decode()
    assert process.returncode == 130, out.decode() + stderr
    # The cleanup is bounded by --seconds (2 s here); closing the connection adds little.
    assert loop.time() - interrupted < 8
    assert exchange.types().count("cancel") == 2
    assert "WARNING: the test order may still be resting at BUY 1 TEST @ 99.960000" in stderr


async def test_the_smoke_tests_cleanup_sends_its_cancel_through_repeated_stops():
    # Several more SIGTERMs arrive while a slow send of the cleanup's cancel is under way:
    # none of them cuts the short grace for that send.
    smoke = load_example(SMOKE_TEST)
    sent: list[str] = []

    class SlowSession:
        async def send(self, type_: str, payload: Any) -> None:
            await asyncio.sleep(0.1)
            sent.append(type_)

    watcher = smoke.Watcher(SlowSession(), 0.5)
    order = smoke.ProbeOrder(INSTRUMENT, "smoke", 99_960_000)
    order.sent = True
    watcher.order = order
    cleaning = asyncio.create_task(smoke.clean_up(watcher, order, 0.5))
    async with asyncio.timeout(RUN_LIMIT):
        await asyncio.sleep(0)
        for _ in range(3):
            cleaning.cancel()
            await asyncio.sleep(0.02)
        with pytest.raises(asyncio.CancelledError):
            await cleaning
        assert sent == ["cancel"]
        await asyncio.sleep(0.6)  # the shielded cancel ends within its own 0.5 s bound


async def test_the_smoke_tests_cleanup_sends_its_cancel_before_a_further_stop():
    # A further SIGTERM lands before the cleanup's cancel task has run at all: the stop
    # still waits for that cancel to go out before it goes on.
    smoke = load_example(SMOKE_TEST)
    sent: list[str] = []

    class Session:
        async def send(self, type_: str, payload: Any) -> None:
            sent.append(type_)

    watcher = smoke.Watcher(Session(), 0.5)
    order = smoke.ProbeOrder(INSTRUMENT, "smoke", 99_960_000)
    order.sent = True
    watcher.order = order
    cleaning = asyncio.create_task(smoke.clean_up(watcher, order, 0.5))
    async with asyncio.timeout(RUN_LIMIT):
        await asyncio.sleep(0)  # the cleanup has made its task and waits on it
        assert sent == []
        cleaning.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cleaning
        assert sent == ["cancel"]
        await asyncio.sleep(0.6)  # the shielded cancel ends within its own 0.5 s bound


async def test_the_smoke_tests_cleanup_lets_a_further_stop_through():
    # A SIGTERM or second Ctrl+C during the cleanup cancel stops the wait and goes on as a
    # cancellation, even when the cleanup began after an ordinary error, so the run exits
    # as the signal asks.
    smoke = load_example(SMOKE_TEST)
    sent: list[str] = []

    class Session:
        async def send(self, type_: str, payload: Any) -> None:
            sent.append(type_)

    watcher = smoke.Watcher(Session(), 0.5)
    order = smoke.ProbeOrder(INSTRUMENT, "smoke", 99_960_000)
    order.sent = True
    watcher.order = order
    cleaning = asyncio.create_task(smoke.clean_up(watcher, order, 0.5))
    async with asyncio.timeout(RUN_LIMIT):
        await until(lambda: sent == ["cancel"])
        cleaning.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cleaning
        await asyncio.sleep(0.6)  # the shielded cancel ends within its own 0.5 s bound


async def test_the_smoke_tests_cleanup_sends_nothing_once_the_order_is_rejected():
    # An interruption lands before the reject of the test order was applied. The reject
    # (here DUPLICATE_ORDER_AT_LEVEL) shows nothing of the test order is at the level, so
    # a cancel there could only hit the order already resting at it.
    smoke = load_example(SMOKE_TEST)
    sent: list[str] = []

    class Session:
        async def send(self, type_: str, payload: Any) -> None:
            sent.append(type_)

    watcher = smoke.Watcher(Session(), 1.0)
    order = smoke.ProbeOrder(INSTRUMENT, "smoke", 99_960_000)
    order.sent = True
    watcher.order = order
    reason = ReasonCodes.DUPLICATE_ORDER_AT_LEVEL
    reject = Reject(request_ref=order.new_ref, request_type=NEW, reason_code=reason)
    watcher.queue.put_nowait(Received("reject", reject, 7))
    assert await smoke.cancel_level(watcher, order, 1.0) is None
    assert sent == []
    assert not order.may_rest


def probe(smoke: Any) -> tuple[Any, dict[str, Any]]:
    """A test order at 99.96 that the exchange has accepted and reported resting."""
    order = smoke.ProbeOrder(INSTRUMENT, "smoke", 99_960_000)
    order.sent = True
    level = {"strat_id": "smoke", "instrument": INSTRUMENT, "side": BUY}
    order.apply(Accepted(request_ref=order.new_ref))
    order.apply(OrderState(**level, price=99_960_000, state=RESTING, remaining_size=1))
    return order, level


def test_the_smoke_test_cannot_tie_a_fill_after_another_programs_amend_to_its_order():
    # Another program amends the resting test order to a marketable price, where it fills:
    # the execution comes before the order_state with old_price. A fill of the strategy at
    # another price is a sign that something else is acting, so nothing after it is tied
    # to the test order, and the warnings name the fill and the order's possible level.
    smoke = load_example(SMOKE_TEST)
    order, level = probe(smoke)
    order.cancel_refs.append("cleanup")
    fill = Execution(
        **level, order_price=100_050_000, fill_price=100_050_000, fill_size=1, remaining_size=0
    )
    order.apply(fill)
    moved = OrderState(
        **level, price=100_050_000, old_price=99_960_000, state=FILLED, remaining_size=0
    )
    order.apply(moved)
    assert order.interfered
    assert order.filled == 0
    assert order.may_rest
    # This script's cancel at the first level, of an order that took it since, is not
    # taken for the test order's.
    order.apply(OrderCancelled(**level, price=99_960_000, request_ref="cleanup"))
    assert order.cancelled is None
    assert "1 at 100.050000" in order.other_fill_warning()
    assert "may still be resting at BUY TEST @ 100.050000" in order.warning()


def test_the_smoke_test_keeps_the_first_report_that_its_order_left():
    # Another program cancels the test order and puts an order of the same strategy at the
    # level; this script's cancel then removes that one. The first cancel is the test
    # order's, and it came from something else, so the order is not reported as cleanly gone.
    smoke = load_example(SMOKE_TEST)
    order, level = probe(smoke)
    order.cancel_refs.append("mine")
    order.apply(OrderCancelled(**level, price=99_960_000, request_ref="theirs"))
    order.apply(OrderCancelled(**level, price=99_960_000, request_ref="mine"))
    assert order.cancelled is not None
    assert order.cancelled.request_ref == "theirs"
    assert not order.confirmed
    assert order.interfered
    assert order.may_rest


def test_the_smoke_test_takes_a_size_only_amend_as_something_else_acting():
    smoke = load_example(SMOKE_TEST)
    order, level = probe(smoke)
    amended = OrderState(
        **level, price=99_960_000, old_price=99_960_000, state=RESTING, remaining_size=5
    )
    order.apply(amended)
    assert order.interfered
    assert order.moved_to is None
    assert order.may_rest


def test_the_smoke_test_still_names_fills_after_a_cancel_it_cannot_tie_to_its_order():
    # After a sign that something else acts, a cancel at the level ends nothing, so a fill
    # that follows is still collected and named on stderr.
    smoke = load_example(SMOKE_TEST)
    order, level = probe(smoke)
    elsewhere = Execution(
        **level, order_price=99_900_000, fill_price=99_900_000, fill_size=2, remaining_size=0
    )
    order.apply(elsewhere)
    assert order.interfered
    order.apply(OrderCancelled(**level, price=99_960_000, request_ref="theirs"))
    assert order.cancelled is None
    at_level = Execution(
        **level, order_price=99_960_000, fill_price=99_960_000, fill_size=1, remaining_size=0
    )
    order.apply(at_level)
    assert order.filled == 0
    assert [fill.fill_size for fill in order.other_fills] == [2, 1]
    assert "2 at 99.900000, 1 at 99.960000" in order.other_fill_warning()


def test_the_smoke_test_does_not_clear_its_warning_on_a_fill_after_missed_reports():
    # After missed reports, a fill at the level may be another order's that took it.
    smoke = load_example(SMOKE_TEST)
    order, level = probe(smoke)
    order.reports_missed = True
    fill = Execution(
        **level, order_price=99_960_000, fill_price=99_960_000, fill_size=1, remaining_size=0
    )
    order.apply(fill)
    assert order.gone
    assert order.may_rest
    assert "may have been another order of this strategy there" in order.fill_warning()


async def test_the_smoke_test_does_not_trust_a_confirmed_cancel_after_missed_reports():
    # Another program moves the test order, unseen (a gap), and an order of the same
    # strategy then takes the level it left: the cancel there removes that one instead.
    exchange = FakeExchange(
        calendar=CALENDAR,
        server_time=SERVER_TIME,
        move_resting_to=(100_010_000,),
        hide_moves=True,
        refill_level=True,
    )
    code, out, err, found = await run_smoke_test(exchange, "--instruments", INSTRUMENT, *TEST_ORDER)
    assert code == 1, out + err
    status, reason = found["test-order"]
    assert status == "FAIL"
    assert "reports were missed, so it may have been another that took the level" in reason
    assert "WARNING: reports about the test order (BUY 1 TEST @ 99.960000) were missed" in err
    assert exchange.resting == {(INSTRUMENT, "BUY", 100_010_000): ("smoke", 1)}
