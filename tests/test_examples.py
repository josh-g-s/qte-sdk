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
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest
from fake_exchange import CONTRACT_VERSION, serve_local
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

from qte_sdk.connection import DecodeFailed, Received, SeqGap
from qte_sdk.contract.v1.common_pb2 import MarketSessionPhase
from qte_sdk.contract.v1.market_data_pb2 import Book as BookMessage
from qte_sdk.contract.v1.market_data_pb2 import SessionState as SessionStateMessage
from qte_sdk.contract.v1.market_data_pb2 import WallLevel
from qte_sdk.contract.v1.order_events_pb2 import Accepted, Execution
from qte_sdk.resting import RestingOrders

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
        teammate_fill_first: bool = False,
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
    ) -> None:
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

    async def send(self, ws: ServerConnection, type_: str, payload: dict[str, Any]) -> None:
        async with self._lock:  # seq numbers must reach the client in order
            self._seq += 1
            env = {"version": CONTRACT_VERSION, "type": type_, "payload": payload, "seq": self._seq}
            await ws.send(json.dumps(env))

    async def __call__(self, ws: ServerConnection) -> None:
        self.received.append(json.loads(await ws.recv()))
        ack = {
            "session_id": "s-1",
            "team": "team-a",
            "server_time": "1",
            "contract_version": CONTRACT_VERSION,
            "unscored": True,
        }
        await self.send(ws, "session_ack", ack)
        if self.calendar is not None:
            await self.send(ws, "calendar", self.calendar)
        ticker: asyncio.Task | None = None
        try:
            async for raw in ws:
                message = json.loads(raw)
                self.received.append(message)
                if message["type"] == "subscribe" and self.closed:
                    await self.answer_closed(ws, message["payload"]["instruments"])
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
        published = 0
        while True:
            if published == self.move_bid_after:
                # A changed book is published at a later grid time.
                book["bid_levels"] = [{"price": str(BID - TICK), "size": "300"}]
                book["grid_time"] = "2"
            # As on the exchange, the session state comes on every interval, unchanged.
            await self.send(ws, "session_state", state)
            if not self.book_once or self.books_sent == 0:
                await self.send(ws, "book", book)
                self.books_sent += 1
            published += 1
            await asyncio.sleep(0.05)

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
                await self.send(ws, "reject", reject)
                return
            if self.teammate_fill_first:
                # While this order is delayed, a teammate's order at the same level fills
                # completely and leaves it, which frees the level for this one.
                self.teammate_fill_first = False
                teammate = {
                    "exec_id": "e-0",
                    "origin": "TEAM",
                    "strat_id": "teammate",
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
            await self.send(ws, "accepted", accepted)
            size = int(p["size"])
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
            key = (p["instrument"], p["side"], int(p["price"]))
            await self.send(ws, "accepted", accepted)
            self.resting[key] = (self.resting[key][0], int(p["new_size"]))
            await self.send(ws, "order_state", self.order_state(key))
        elif type_ == "cancel":
            key = (p["instrument"], p["side"], int(p["price"]))
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

    async def confirm_later(
        self, ws: ServerConnection, key: tuple[str, str, int], ref: str
    ) -> None:
        assert self.confirm_cancels is not None
        await self.confirm_cancels.wait()
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


CALENDAR = {
    "term_first_session": "2026-01-02",
    "term_last_session": "2026-01-05",
    "sessions": [
        {"session_date": "2026-01-02", "open_time": "-20", "close_time": "0"},
        {"session_date": "2026-01-05", "open_time": "100", "close_time": "200"},
    ],
}


async def test_out_of_hours_shows_the_calendar_and_the_closed_market_with_no_close():
    # As on the exchange today: the closed state, and no official close after it.
    exchange = FakeExchange(closed=True, official_close=False, calendar=CALENDAR)
    async with serve_local(exchange) as url:
        code, out, err = await run_example(
            "out_of_hours.py", url, synthetic_token(), "--instrument", INSTRUMENT, "--seconds", "1"
        )
    assert code == 0, err
    assert "last closed session: 2026-01-02" in out
    assert "next session: 2026-01-05" in out
    assert "market session 2026-01-05: CLOSED" in out
    # The fake acknowledges at server_time 1 and names a next open at 100.
    assert "next open: session 2026-01-06, in 99 exchange time units" in out
    assert "no official close: the exchange does not send it yet, as expected" in out
    assert exchange.types() == ["auth", "subscribe"]


async def test_out_of_hours_measures_the_next_open_from_server_time_not_a_future_grid_time():
    # Before the exchange has closed any session, the reply names the next scheduled one,
    # and its grid_time equals that session's close_time, in the future.
    state = {
        "state": "CLOSED",
        "session_date": "2026-01-05",
        "open_time": "100",
        "close_time": "200",
        "grid_time": "200",
        "next_session_date": "2026-01-05",
        "next_open_time": "100",
        "next_close_time": "200",
    }
    exchange = FakeExchange(closed=True, official_close=False, closed_state=state)
    async with serve_local(exchange) as url:
        code, out, err = await run_example(
            "out_of_hours.py", url, synthetic_token(), "--instrument", INSTRUMENT, "--seconds", "1"
        )
    assert code == 0, err
    # Measured from server_time 1, not from grid_time 200 (which would give -100).
    assert "next open: session 2026-01-05, in 99 exchange time units" in out


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
