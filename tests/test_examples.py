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

from qte_sdk.connection import Received
from qte_sdk.contract.v1.market_data_pb2 import Book as BookMessage
from qte_sdk.contract.v1.market_data_pb2 import WallLevel
from qte_sdk.contract.v1.order_events_pb2 import Accepted, Execution
from qte_sdk.resting import RestingOrders

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
EXAMPLES = sorted(EXAMPLES_DIR.glob("*.py"))
QUICKSTART = EXAMPLES_DIR.parent / "docs" / "quickstart.md"
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


@pytest.mark.parametrize("path", [*EXAMPLES, QUICKSTART], ids=lambda p: p.name)
def test_no_example_or_quickstart_names_any_exchange_but_a_local_one(path: Path):
    for url in re.findall(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s`'\")]+", path.read_text()):
        parts = urlsplit(url)
        assert parts.scheme == "ws", url
        assert parts.hostname == "127.0.0.1", url
        assert parts.username is None and parts.password is None, url


class FakeExchange:
    """A scripted exchange: acknowledges the session, publishes a book on every tick once
    subscribed, and answers order messages the way the contract describes."""

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
    ) -> None:
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
        ticker: asyncio.Task | None = None
        try:
            async for raw in ws:
                message = json.loads(raw)
                self.received.append(message)
                if message["type"] == "subscribe" and ticker is None:
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
                book["bid_levels"] = [{"price": str(BID - TICK), "size": "300"}]
            # As on the exchange, the session state comes on every interval, unchanged.
            await self.send(ws, "session_state", state)
            await self.send(ws, "book", book)
            published += 1
            await asyncio.sleep(0.05)

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
    """A connection whose every send writes its message and then never returns, as on a
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


async def test_quote_both_sides_does_not_resend_a_cancel_that_ctrl_c_cut_short():
    # A send can stall after its message is written, for example on a full send buffer. A
    # Ctrl+C then must not make the example forget that message and send it again.
    example = load_example("quote_both_sides.py")
    sent: list[str] = []
    args = example.parse_args(["--instrument", INSTRUMENT, "--strat-id", "quote-test"])
    quoter = example.Quoter(SimpleNamespace(connection=StalledConnection(sent)), None, args)
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
    quoter = example.Quoter(SimpleNamespace(connection=StalledConnection(sent)), view, args)
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
    quoter = example.Quoter(SimpleNamespace(connection=StalledConnection(sent)), view, args)
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait(book_event())
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with asyncio.timeout(RUN_LIMIT):
        why = await example.quote_until(quoter, view, queue, 0.3)
    assert why == "time"
    assert loop.time() - started < 2  # --seconds, not the stalled send, ended it
    assert len(sent) == 1  # the first new order, which stalled
    assert quoter.quotes[example.BUY].pending_ref == sent[0]  # recorded all the same


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
