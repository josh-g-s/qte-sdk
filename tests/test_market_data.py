import json
from decimal import Decimal
from typing import Any

import pytest
from fake_exchange import exchange, frame, serve_local
from websockets.asyncio.server import ServerConnection

from qte_sdk import market_data as md
from qte_sdk.connection import (
    Connection,
    DataUncertain,
    DecodeFailed,
    Disconnected,
    Received,
    SeqGap,
    Unknown,
)
from qte_sdk.contract.v1 import market_data_pb2
from qte_sdk.contract.v1.common_pb2 import (
    BUY,
    CLOSED,
    OPEN,
    SELL,
    STUDENT_TO_WALL,
    SUBSCRIBE,
    TRADE_BASED_MATCH,
    UNSUBSCRIBE,
    ReasonCodes,
)
from qte_sdk.contract.v1.order_events_pb2 import Execution, Reject
from qte_sdk.market_data import (
    Book,
    InstrumentCondition,
    Mark,
    OfficialClose,
    SessionState,
    Trades,
    as_market_data,
    market_data,
    subscribe,
    unsubscribe,
)
from qte_sdk.session import Session, SessionInfo
from qte_sdk.units import to_decimal

ABOVE_2_53 = 2**53 + 1  # a float cannot hold this; the wire sends it as a decimal string


def wall(first: int, step: int, count: int) -> list[dict[str, str]]:
    return [{"price": str(first + i * step), "size": str(10 + i)} for i in range(count)]


def book_payload(instrument: str = "AAPL", **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "instrument": instrument,
        "grid_time": "1000",
        "bid_levels": wall(199_990_000, -10_000, 10),
        "ask_levels": wall(200_010_000, 10_000, 10),
        "student_bid_levels": [],
        "student_ask_levels": [],
        "condition": "LIVE",
    }
    payload.update(fields)
    return payload


async def received(frames: list[str]) -> list[md.MarketDataEvent]:
    async with exchange(frames) as url, Connection(url) as conn:
        return [item async for item in market_data(conn)]


# Each type decoded into its typed form, prices exact from decimal strings.


async def test_a_book_decodes_typed_with_exact_prices_and_its_condition():
    payload = book_payload(
        bid_levels=[{"price": str(ABOVE_2_53), "size": "30"}],
        ask_levels=[{"price": str(ABOVE_2_53 + 10_000), "size": "26"}],
        student_bid_levels=[{"price": "199980000", "size": "700"}],
        student_ask_levels=[{"price": str(ABOVE_2_53 + 20_000), "size": str(ABOVE_2_53)}],
        condition="FROZEN",
    )
    [book] = await received([frame("book", payload, 1)])

    assert isinstance(book, Book)
    assert book.instrument == "AAPL"
    assert book.grid_time == 1000
    assert book.bid_levels[0].price == ABOVE_2_53
    assert to_decimal(book.bid_levels[0].price) == Decimal("9007199254.740993")
    assert book.ask_levels[0].price == ABOVE_2_53 + 10_000
    assert book.student_bid_levels[0].price == 199_980_000
    assert book.student_bid_levels[0].size == 700
    assert book.student_ask_levels[0].size == ABOVE_2_53
    assert book.condition == InstrumentCondition.FROZEN


async def test_trades_decode_every_print_individually_with_exact_prices():
    payload = {
        "instrument": "AAPL",
        "grid_time": "2000",
        "prints": [
            {
                "price": str(ABOVE_2_53),
                "size": "40",
                "aggressor_side": "BUY",
                "timestamp": "1990",
                "kind": "STUDENT_TO_WALL",
            },
            {
                "price": "199980000",
                "size": "5",
                "aggressor_side": "SELL",
                "timestamp": "1995",
                "kind": "TRADE_BASED_MATCH",
            },
        ],
    }
    [trades] = await received([frame("trades", payload, 1)])

    assert isinstance(trades, Trades)
    assert (trades.instrument, trades.grid_time) == ("AAPL", 2000)
    first, second = trades.prints
    assert (first.price, first.size, first.aggressor_side, first.timestamp, first.kind) == (
        ABOVE_2_53,
        40,
        BUY,
        1990,
        STUDENT_TO_WALL,
    )
    assert (second.price, second.aggressor_side, second.kind) == (
        199_980_000,
        SELL,
        TRADE_BASED_MATCH,
    )


async def test_a_mark_decodes_its_off_tick_value_exactly_and_its_condition():
    payload = {
        "instrument": "AAPL",
        "sampled_at": "3000",
        "value": "200011000",
        "condition": "LIVE",
    }
    [mark] = await received([frame("mark", payload, 1)])

    assert isinstance(mark, Mark)
    assert (mark.instrument, mark.sampled_at) == ("AAPL", 3000)
    assert mark.value == 200_011_000
    assert to_decimal(mark.value) == Decimal("200.011")
    assert mark.condition == InstrumentCondition.LIVE


async def test_a_mark_above_2_53_micros_is_exact():
    payload = {"instrument": "AAPL", "sampled_at": "3000", "value": str(ABOVE_2_53)}
    [mark] = await received([frame("mark", {**payload, "condition": "FROZEN"}, 1)])
    assert mark.value == ABOVE_2_53
    assert mark.condition == InstrumentCondition.FROZEN


async def test_a_reference_unavailable_mark_carries_a_real_zero_value():
    payload = {
        "instrument": "QTEZ",
        "sampled_at": "3000",
        "value": "0",
        "condition": "REFERENCE_UNAVAILABLE",
    }
    [mark] = await received([frame("mark", payload, 1)])
    assert mark.value == 0
    assert mark.condition == InstrumentCondition.REFERENCE_UNAVAILABLE


async def test_session_state_decodes_typed():
    payload = {
        "state": "OPEN",
        "session_date": "2026-09-18",
        "open_time": "100",
        "close_time": "200",
        "grid_time": "150",
        "outage_active": True,
    }
    [state] = await received([frame("session_state", payload, 1)])

    assert isinstance(state, SessionState)
    assert state.state == OPEN
    assert state.session_date == "2026-09-18"
    assert (state.open_time, state.close_time, state.grid_time) == (100, 200, 150)
    assert state.outage_active is True


async def test_an_official_close_decodes_its_value_exactly_and_its_frozen_flag():
    payload = {
        "instrument": "AAPL",
        "session_date": "2026-09-18",
        "value": str(ABOVE_2_53),
        "frozen": True,
    }
    [close] = await received([frame("official_close", payload, 1)])

    assert isinstance(close, OfficialClose)
    assert (close.instrument, close.session_date) == ("AAPL", "2026-09-18")
    assert close.value == ABOVE_2_53
    assert to_decimal(close.value) == Decimal("9007199254.740993")
    assert close.frozen is True


async def test_an_official_close_with_no_frozen_mark_leaves_the_flag_unset():
    payload = {"instrument": "AAPL", "session_date": "2026-09-18", "value": "200011000"}
    [close] = await received([frame("official_close", payload, 1)])
    assert to_decimal(close.value) == Decimal("200.011")
    assert close.frozen is False


@pytest.mark.parametrize(
    "condition", ["LIVE", "ONE_SIDED", "EMPTY", "FROZEN", "REFERENCE_UNAVAILABLE", "DISABLED"]
)
async def test_every_book_condition_is_exposed(condition):
    [book] = await received([frame("book", book_payload(condition=condition), 1)])
    assert book.condition == InstrumentCondition.Value(condition)


# Ladder shapes.


async def test_a_full_ladder_keeps_every_level_in_order():
    [book] = await received([frame("book", book_payload(), 1)])
    assert [level.price for level in book.bid_levels] == [
        199_990_000 - i * 10_000 for i in range(10)
    ]
    assert [level.price for level in book.ask_levels] == [
        200_010_000 + i * 10_000 for i in range(10)
    ]
    assert [level.size for level in book.bid_levels] == list(range(10, 20))


async def test_a_ladder_with_fewer_levels_is_not_padded():
    payload = book_payload(bid_levels=wall(199_990_000, -10_000, 3))
    [book] = await received([frame("book", payload, 1)])
    assert [level.price for level in book.bid_levels] == [199_990_000, 199_980_000, 199_970_000]
    assert len(book.ask_levels) == 10


async def test_an_empty_side_decodes_as_an_empty_ladder_with_the_other_side_intact():
    payload = book_payload(
        bid_levels=[],
        student_ask_levels=[{"price": "200000000", "size": "100"}],
        condition="ONE_SIDED",
    )
    [book] = await received([frame("book", payload, 1)])
    assert list(book.bid_levels) == []
    assert len(book.ask_levels) == 10
    assert book.student_ask_levels[0].price == 200_000_000
    assert book.condition == InstrumentCondition.ONE_SIDED


async def test_an_empty_quote_shows_no_wall_but_keeps_student_depth():
    payload = book_payload(
        bid_levels=[],
        ask_levels=[],
        student_bid_levels=[{"price": "199900000", "size": "500"}],
        condition="EMPTY",
    )
    [book] = await received([frame("book", payload, 1)])
    assert list(book.bid_levels) == [] and list(book.ask_levels) == []
    assert book.student_bid_levels[0].price == 199_900_000
    assert book.student_bid_levels[0].size == 500
    assert book.condition == InstrumentCondition.EMPTY


# Subscribe and unsubscribe.


async def test_subscribe_and_unsubscribe_send_the_instrument_list():
    inbox: list[str] = []

    async def handler(ws: ServerConnection) -> None:
        inbox.append(await ws.recv())
        inbox.append(await ws.recv())
        await ws.close()

    async with serve_local(handler) as url, Connection(url) as conn:
        await subscribe(conn, ["AAPL", "MSFT"])
        await unsubscribe(conn, ("MSFT",))
        assert [e async for e in conn] == []

    sent = [json.loads(m) for m in inbox]
    assert [(m["type"], m["payload"]) for m in sent] == [
        ("subscribe", {"instruments": ["AAPL", "MSFT"]}),
        ("unsubscribe", {"instruments": ["MSFT"]}),
    ]


def session_on(conn: Connection, early: list[Any] | None = None) -> Session:
    """A session on `conn` as `open_session` returns it, without the auth exchange."""
    return Session(conn, SessionInfo("s-1", "team-a", 1, "0.x", True), early or [])


async def test_subscribe_and_unsubscribe_send_on_a_session():
    inbox: list[str] = []

    async def handler(ws: ServerConnection) -> None:
        inbox.append(await ws.recv())
        inbox.append(await ws.recv())
        await ws.close()

    async with serve_local(handler) as url, Connection(url) as conn:
        session = session_on(conn)
        await subscribe(session, ["AAPL"])
        await unsubscribe(session, ["AAPL"])
        assert [e async for e in session] == []

    sent = [json.loads(m) for m in inbox]
    assert [(m["type"], m["payload"]) for m in sent] == [
        ("subscribe", {"instruments": ["AAPL"]}),
        ("unsubscribe", {"instruments": ["AAPL"]}),
    ]


async def test_market_data_reads_a_session_from_before_its_acknowledgement():
    # Held back by `open_session` while it waited for the acknowledgement.
    early = Received("book", market_data_pb2.Book(instrument="AAPL"), None)
    frames = [frame("book", book_payload("MSFT"), 1)]
    async with exchange(frames) as url, Connection(url) as conn:
        items = [item async for item in market_data(session_on(conn, [early]))]
    assert [(type(m), m.instrument) for m in items] == [(Book, "AAPL"), (Book, "MSFT")]


async def test_a_single_string_is_refused_rather_than_split_into_letters():
    async with exchange([]) as url, Connection(url) as conn:
        with pytest.raises(TypeError):
            await subscribe(conn, "AAPL")
        with pytest.raises(TypeError):
            await unsubscribe(conn, "AAPL")


async def test_unsubscribing_stops_delivery_for_that_instrument():
    unsubscribed: list[dict[str, Any]] = []

    async def handler(ws: ServerConnection) -> None:
        # A scripted exchange: it stops sending an instrument once asked to.
        assert json.loads(await ws.recv())["type"] == "subscribe"
        await ws.send(frame("book", book_payload("AAPL"), 1))
        await ws.send(frame("book", book_payload("MSFT"), 2))
        unsubscribed.append(json.loads(await ws.recv()))
        await ws.send(frame("book", book_payload("MSFT"), 3))
        mark = {"instrument": "MSFT", "sampled_at": "1", "value": "1", "condition": "LIVE"}
        await ws.send(frame("mark", mark, 4))
        await ws.close()

    async with serve_local(handler) as url, Connection(url) as conn:
        await subscribe(conn, ["AAPL", "MSFT"])
        feed = market_data(conn)
        before = [await anext(feed), await anext(feed)]
        await unsubscribe(conn, ["AAPL"])
        after = [item async for item in feed]

    assert [m.instrument for m in before] == ["AAPL", "MSFT"]
    assert unsubscribed[0]["type"] == "unsubscribe"
    assert unsubscribed[0]["payload"] == {"instruments": ["AAPL"]}
    assert [(type(m), m.instrument) for m in after] == [(Book, "MSFT"), (Mark, "MSFT")]


def closed_market(closes: dict[str, str]) -> Any:
    """A scripted exchange outside a session. It answers a subscribe once, with the closed
    state and, for each named instrument in `closes`, its last official close (micro-dollars
    as the wire's decimal string), and sends nothing else: no book, trades or mark."""

    async def handler(ws: ServerConnection) -> None:
        subscribed = json.loads(await ws.recv())
        assert subscribed["type"] == "subscribe"
        state = {
            "state": "CLOSED",
            "session_date": "2026-09-18",
            "open_time": "100",
            "close_time": "200",
            "grid_time": "200",
            "outage_active": False,
        }
        seq = 1
        await ws.send(frame("session_state", state, seq))
        for instrument in subscribed["payload"]["instruments"]:
            if instrument in closes:
                seq += 1
                close = {
                    "instrument": instrument,
                    "session_date": "2026-09-18",
                    "value": closes[instrument],
                    "frozen": False,
                }
                await ws.send(frame("official_close", close, seq))
        await ws.close()

    return handler


async def closed_market_reply(closes: dict[str, str], instruments: list[str]) -> list[Any]:
    async with serve_local(closed_market(closes)) as url, Connection(url) as conn:
        session = session_on(conn)
        await subscribe(session, instruments)
        return [item async for item in market_data(session)]


async def test_outside_a_session_a_subscribe_gets_the_closed_state_and_official_closes():
    closes = {"AAPL": "200011000", "MSFT": "415250000"}
    items = await closed_market_reply(closes, ["AAPL", "MSFT"])

    assert [type(item) for item in items] == [SessionState, OfficialClose, OfficialClose]
    assert not any(isinstance(item, Book | Trades | Mark) for item in items)
    state, *official = items
    assert state.state == CLOSED
    assert state.grid_time == state.close_time
    assert [(c.instrument, c.session_date) for c in official] == [
        ("AAPL", "2026-09-18"),
        ("MSFT", "2026-09-18"),
    ]
    assert [to_decimal(c.value) for c in official] == [Decimal("200.011"), Decimal("415.25")]


async def test_outside_a_session_an_instrument_with_no_official_close_yet_gets_none():
    items = await closed_market_reply({"AAPL": "200011000"}, ["AAPL", "QTEZ"])
    assert [type(item) for item in items] == [SessionState, OfficialClose]
    assert items[1].instrument == "AAPL"


# What the market-data view passes on and what it leaves out.


async def test_order_events_and_unknown_types_are_left_out_of_the_feed():
    frames = [
        frame("execution", {"exec_id": "e-1", "instrument": "AAPL"}, 1),
        frame("a_newer_message", {"x": 1}, 2),
        frame("book", book_payload(), 3),
        frame("reject", {"request_type": "NEW", "reason_code": "TICK_VIOLATION"}, 4),
    ]
    items = await received(frames)
    assert [type(item) for item in items] == [Book]


async def test_a_refused_subscription_is_passed_on():
    reject = {
        "request_type": "SUBSCRIBE",
        "reason_code": "UNKNOWN_INSTRUMENT",
        "instrument": "APPL",
        "receipt_time": "5",
    }
    [item] = await received([frame("reject", reject, 1)])
    assert isinstance(item, Reject)
    assert item.request_type == SUBSCRIBE
    assert item.reason_code == ReasonCodes.UNKNOWN_INSTRUMENT
    assert item.instrument == "APPL"


async def test_gaps_and_undecodable_frames_are_passed_on_not_hidden():
    bad_book = frame("book", {"instrument": "AAPL", "grid_time": "not a number"}, 1)
    # A reject that cannot be decoded may have been a refused subscription.
    bad_reject = frame("reject", {"request_type": "SUBSCRIBE", "reason_code": ["x"]}, 2)
    frames = [bad_book, bad_reject, "not json", frame("book", book_payload(), 5)]
    items = await received(frames)

    assert [type(item) for item in items] == [
        DecodeFailed,
        DecodeFailed,
        DecodeFailed,
        SeqGap,
        Book,
    ]
    assert [item.type for item in items[:3]] == ["book", "reject", None]
    assert items[3] == SeqGap(expected=3, received=5)


def test_as_market_data_classifies_single_events():
    book = Book(instrument="AAPL")
    assert as_market_data(Received("book", book, 1)) is book
    assert as_market_data(Received("execution", Execution(), 1)) is None
    unsub_reject = Reject(request_type=UNSUBSCRIBE)
    assert as_market_data(Received("reject", unsub_reject, 1)) is unsub_reject
    assert as_market_data(Received("reject", Reject(), 1)) is None
    assert as_market_data(Unknown("book_v2", {}, 1)) is None
    close = OfficialClose(instrument="AAPL", value=200_011_000)
    assert as_market_data(Received("official_close", close, 1)) is close
    gap = SeqGap(1, 3)
    assert as_market_data(gap) is gap
    failed = DecodeFailed("execution", ValueError())
    assert as_market_data(failed) is failed


def test_no_option_chain_types_are_exposed():
    public = set(md.__all__) | {n for n in dir(md) if not n.startswith("_")}
    assert not [name for name in public if "option" in name.lower()]
    messages = market_data_pb2.DESCRIPTOR.message_types_by_name
    assert not [name for name in messages if "option" in name.lower()]
    assert md.MARKET_DATA_TYPES == {"book", "trades", "mark", "session_state", "official_close"}


def test_a_disconnect_is_passed_on_as_a_sign_that_messages_were_lost():
    disconnected = Disconnected(None)
    assert md.as_market_data(disconnected) is disconnected
    assert isinstance(disconnected, DataUncertain) and isinstance(SeqGap(1, 3), DataUncertain)


def test_events_that_are_neither_market_data_nor_uncertainty_are_skipped():
    assert md.as_market_data(object()) is None
