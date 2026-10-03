from contextlib import aclosing

import pytest
from fake_exchange import frame, serve_local
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosedError

from qte_sdk.connection import (
    Connection,
    DecodeFailed,
    Disconnected,
    Received,
    SeqGap,
    Unknown,
)
from qte_sdk.contract.v1.common_pb2 import (
    AMEND,
    BUY,
    CANCELLED,
    FILLED,
    LIMIT,
    MAKER,
    MASS_CANCEL,
    NEW,
    RESTING,
    SELL,
    STALE,
    TAKER,
    TEAM,
    ReasonCodes,
)
from qte_sdk.contract.v1.order_entry_pb2 import NewOrder
from qte_sdk.contract.v1.order_events_pb2 import (
    Accepted,
    Execution,
    OrderCancelled,
    OrderState,
    Reject,
)
from qte_sdk.resting import LevelKey, RestingOrder, RestingOrders

PX = 199_970_000
PX2 = 199_960_000
PX3 = 200_020_000


def rested(price=PX, size=100, side=BUY, instrument="AAPL", state=RESTING, **kw) -> OrderState:
    return OrderState(
        strat_id="mm-1",
        instrument=instrument,
        side=side,
        price=price,
        state=state,
        remaining_size=size,
        timestamp=1,
        **kw,
    )


def fill(remaining, size, price=PX, side=BUY, liquidity=MAKER) -> Execution:
    return Execution(
        exec_id="x",
        origin=TEAM,
        strat_id="mm-1",
        instrument="AAPL",
        side=side,
        order_price=price,
        fill_price=price,
        fill_size=size,
        remaining_size=remaining,
        liquidity=liquidity,
    )


def cancelled(reason, price=PX, side=BUY, instrument="AAPL", size=100) -> OrderCancelled:
    return OrderCancelled(
        origin=TEAM,
        strat_id="mm-1",
        instrument=instrument,
        side=side,
        price=price,
        cancelled_size=size,
        reason_code=reason,
    )


def view_of(*events) -> RestingOrders:
    view = RestingOrders()
    for event in events:
        view.apply(event)
    return view


def test_an_order_rests_when_the_exchange_reports_it_resting():
    view = view_of(Accepted(request_ref="r-1", request_type=NEW), rested())
    assert view.get("AAPL", BUY, PX) == RestingOrder(
        LevelKey("AAPL", BUY, PX), "mm-1", 100, RESTING, None
    )
    assert LevelKey("AAPL", BUY, PX) in view
    assert len(view) == 1
    assert not view.incomplete


def test_an_accepted_new_alone_is_not_a_resting_order():
    # accepted carries no level key; only order_state says the order rests.
    view = view_of(Accepted(request_ref="r-1", request_type=NEW))
    assert len(view) == 0


def test_a_partial_fill_then_a_full_fill():
    view = view_of(rested(size=100), fill(remaining=60, size=40))
    assert view.get("AAPL", BUY, PX).remaining_size == 60
    view.apply(fill(remaining=0, size=60))
    assert view.get("AAPL", BUY, PX) is None
    assert len(view) == 0


def test_fills_of_an_incoming_order_before_it_rests_do_not_add_an_entry():
    view = view_of(fill(remaining=60, size=40, liquidity=TAKER))
    assert len(view) == 0
    view.apply(rested(size=60))
    assert view.get("AAPL", BUY, PX).remaining_size == 60


def test_a_market_order_fill_has_no_level_key_and_changes_nothing():
    market = Execution(
        exec_id="x", origin=TEAM, instrument="AAPL", side=BUY, fill_size=5, remaining_size=0
    )
    view = view_of(rested(), market)
    assert view.get("AAPL", BUY, PX).remaining_size == 100


def amended(price, old_price, state=RESTING, size=100, **kw) -> OrderState:
    """The order_state an accepted amend sends: `price` is the amend's new price."""
    return rested(price=price, size=size, state=state, old_price=old_price, **kw)


AMEND_ACCEPTED = Accepted(request_ref="r-2", request_type=AMEND)


def test_a_size_only_amend_sets_the_remaining_size():
    view = view_of(rested(size=100), AMEND_ACCEPTED, amended(PX, old_price=PX, size=80))
    assert view.get("AAPL", BUY, PX).remaining_size == 80
    assert len(view) == 1
    assert not view.incomplete


def test_a_price_moving_amend_that_rests_re_keys_the_entry():
    view = view_of(rested(price=PX), AMEND_ACCEPTED, amended(PX2, old_price=PX, size=70))
    assert view.get("AAPL", BUY, PX) is None
    assert view.get("AAPL", BUY, PX2) == RestingOrder(
        LevelKey("AAPL", BUY, PX2), "mm-1", 70, RESTING, None
    )
    assert len(view) == 1
    assert not view.incomplete


def test_a_marketable_amend_that_fills_completely_removes_the_old_entry():
    # The amend's execution at its new price comes first, then its order_state, FILLED.
    view = view_of(
        rested(price=PX, size=100),
        AMEND_ACCEPTED,
        fill(remaining=0, size=100, price=PX3, liquidity=TAKER),
        amended(PX3, old_price=PX, state=FILLED, size=0),
    )
    assert len(view) == 0
    assert not view.incomplete


def test_a_marketable_amend_that_fills_in_part_rests_its_remainder_at_the_new_price():
    view = view_of(
        rested(price=PX, size=100),
        AMEND_ACCEPTED,
        fill(remaining=30, size=70, price=PX3, liquidity=TAKER),
        amended(PX3, old_price=PX, size=30),
    )
    assert view.get("AAPL", BUY, PX) is None
    assert view.get("AAPL", BUY, PX3).remaining_size == 30
    assert len(view) == 1


def test_an_amend_cut_to_nothing_reported_cancelled_removes_the_entry():
    view = view_of(
        rested(price=PX),
        AMEND_ACCEPTED,
        cancelled(ReasonCodes.AMEND_CUT, price=PX),
        amended(PX, old_price=PX, state=CANCELLED, size=0),
    )
    assert len(view) == 0


def test_a_price_moving_amend_cancelled_at_its_new_price_leaves_nothing_at_either_price():
    # For example a remainder that could not rest: the order_state alone clears both levels,
    # whichever order the messages come in.
    view = view_of(
        rested(price=PX),
        AMEND_ACCEPTED,
        amended(PX3, old_price=PX, state=CANCELLED, size=0),
        cancelled(ReasonCodes.REMAINDER_OUTSIDE_BAND, price=PX3),
    )
    assert len(view) == 0


def test_an_amend_keeps_a_stale_order_stale_at_its_new_price():
    view = view_of(
        rested(price=PX),
        amended(PX2, old_price=PX, state=STALE, size=50, stale_since=1234),
    )
    order = view.get("AAPL", BUY, PX2)
    assert (order.state, order.stale_since, order.remaining_size) == (STALE, 1234, 50)
    assert view.get("AAPL", BUY, PX) is None


def test_an_amend_leaves_other_levels_alone():
    view = view_of(
        rested(price=PX),
        rested(price=PX, side=SELL),
        rested(price=PX, instrument="MSFT"),
        amended(PX2, old_price=PX),
    )
    assert view.get("AAPL", BUY, PX) is None
    assert view.get("AAPL", SELL, PX) is not None
    assert view.get("MSFT", BUY, PX) is not None
    assert view.get("AAPL", BUY, PX2) is not None


def test_an_older_exchange_without_old_price_leaves_the_old_entry():
    # Without old_price no event names the price an amend moved an order away from, so the
    # old entry stays; the view is not marked incomplete.
    view = view_of(rested(price=PX), AMEND_ACCEPTED, rested(price=PX2, size=70))
    assert view.get("AAPL", BUY, PX2).remaining_size == 70
    assert view.get("AAPL", BUY, PX) is not None
    assert not view.incomplete


def test_an_ended_order_state_without_old_price_changes_nothing():
    ended = OrderState(
        strat_id="mm-1", instrument="AAPL", side=BUY, price=PX, state=FILLED, remaining_size=0
    )
    view = view_of(rested(price=PX), ended)
    assert view.get("AAPL", BUY, PX).remaining_size == 100


def test_a_rejected_amend_leaves_the_entry_unchanged():
    view = view_of(rested(size=100))
    before = view.get("AAPL", BUY, PX)
    view.apply(
        Reject(
            request_ref="r-2",
            request_type=AMEND,
            reason_code=ReasonCodes.AMEND_PRICE_AT_OR_BEYOND_WALL,
        )
    )
    assert view.get("AAPL", BUY, PX) == before
    assert len(view) == 1


def test_a_cancel_removes_the_entry():
    view = view_of(rested(), rested(price=PX2), cancelled(ReasonCodes.CANCEL_REQUEST))
    assert view.get("AAPL", BUY, PX) is None
    assert view.get("AAPL", BUY, PX2) is not None


def test_an_amend_cut_to_nothing_removes_the_entry():
    view = view_of(rested(), cancelled(ReasonCodes.AMEND_CUT))
    assert len(view) == 0


def test_a_mass_cancel_clears_every_entry():
    levels = [("AAPL", BUY, PX), ("AAPL", SELL, 200_100_000), ("MSFT", BUY, 400_000_000)]
    view = view_of(*(rested(price=p, side=s, instrument=i) for i, s, p in levels))
    assert len(view) == 3
    view.apply(Accepted(request_ref="r-9", request_type=MASS_CANCEL))
    for i, s, p in levels:
        view.apply(cancelled(ReasonCodes.MASS_CANCEL, price=p, side=s, instrument=i))
    assert len(view) == 0
    assert list(view) == []


def test_the_purge_of_a_stale_order_removes_it():
    # The move to STALE is never reported, so the entry still reads RESTING until the purge.
    view = view_of(rested())
    assert view.get("AAPL", BUY, PX).state == RESTING
    view.apply(cancelled(ReasonCodes.PURGE_STALE))
    assert len(view) == 0


def test_an_order_reported_stale_keeps_its_stale_since_and_is_purged():
    view = view_of(rested())
    view.apply(
        OrderState(
            strat_id="mm-1",
            instrument="AAPL",
            side=BUY,
            price=PX,
            state=STALE,
            remaining_size=50,
            stale_since=1234,
        )
    )
    order = view.get("AAPL", BUY, PX)
    assert (order.state, order.stale_since, order.remaining_size) == (STALE, 1234, 50)
    view.apply(fill(remaining=20, size=30))
    assert view.get("AAPL", BUY, PX).state == STALE
    view.apply(cancelled(ReasonCodes.PURGE_STALE, size=20))
    assert len(view) == 0


def test_a_cancellation_without_a_price_changes_nothing():
    remainder = OrderCancelled(
        origin=TEAM,
        instrument="AAPL",
        side=BUY,
        cancelled_size=10,
        reason_code=ReasonCodes.MARKET_REMAINDER,
    )
    view = view_of(rested(), remainder)
    assert len(view) == 1


def test_events_arrive_as_connection_events_too():
    view = view_of(Received("order_state", rested(), 1), Unknown("new_kind", {}, 2))
    assert len(view) == 1
    assert not view.incomplete


@pytest.mark.parametrize(
    "event",
    [
        SeqGap(expected=3, received=5),
        DecodeFailed("execution", ValueError("bad")),
        DecodeFailed(None, ValueError("not json")),
    ],
)
def test_a_missed_or_unreadable_event_marks_the_view_incomplete(event):
    view = view_of(rested(), event)
    assert view.incomplete
    assert len(view) == 1


def test_an_unreadable_market_data_frame_does_not_mark_the_view_incomplete():
    view = view_of(DecodeFailed("book", ValueError("bad")))
    assert not view.incomplete


def test_a_caller_can_mark_the_view_incomplete():
    view = RestingOrders()
    view.mark_incomplete()
    assert view.incomplete


async def test_a_sent_but_unacknowledged_order_is_absent_and_a_disconnect_marks_incomplete():
    inbox: list[str] = []

    async def drops_after_receiving(ws: ServerConnection) -> None:
        # One resting order is reported, then the client's new order is received but never
        # acknowledged before the connection drops.
        await ws.send(frame("order_state", _json_state(PX2), 1))
        inbox.append(await ws.recv())
        ws.transport.abort()

    view = RestingOrders()
    new = NewOrder(
        request_ref="r-1",
        strat_id="mm-1",
        instrument="AAPL",
        side=BUY,
        order_type=LIMIT,
        price=PX,
        size=100,
    )
    async with serve_local(drops_after_receiving) as url:
        async with Connection(url) as conn:
            with pytest.raises(ConnectionClosedError):
                async for event in view.follow(conn):
                    if isinstance(event, Received) and event.type == "order_state":
                        await conn.send("new", new)

    assert inbox and '"new"' in inbox[0]
    assert view.get("AAPL", BUY, PX) is None
    assert view.get("AAPL", BUY, PX2).remaining_size == 100
    assert view.incomplete


async def test_events_decoded_from_the_wire_update_the_view():
    frames = [
        frame("accepted", {"request_ref": "r-1", "request_type": "NEW"}, 1),
        frame("order_state", _json_state(PX), 2),
        frame("execution", _json_fill(remaining="60"), 3),
    ]

    async def handler(ws: ServerConnection) -> None:
        for f in frames:
            await ws.send(f)
        await ws.close()

    view = RestingOrders()
    async with serve_local(handler) as url:
        async with Connection(url) as conn:
            events = [event async for event in view.follow(conn)]
    assert len(events) == 3
    assert view.get("AAPL", BUY, PX).remaining_size == 60
    # The connection has ended, so later events can no longer reach the view.
    assert view.incomplete


async def test_amends_decoded_from_the_wire_re_key_and_then_close_the_entry():
    moved = {**_json_state(PX2), "old_price": str(PX)}
    filled = {**_json_state(PX3), "state": "FILLED", "remaining_size": "0", "old_price": str(PX2)}
    frames = [
        frame("order_state", _json_state(PX), 1),
        frame("accepted", {"request_ref": "r-2", "request_type": "AMEND"}, 2),
        frame("order_state", moved, 3),
        frame("accepted", {"request_ref": "r-3", "request_type": "AMEND"}, 4),
        frame("order_state", filled, 5),
    ]
    sizes: list[tuple[int | None, int | None]] = []

    def size_at(price: int) -> int | None:
        order = view.get("AAPL", BUY, price)
        return None if order is None else order.remaining_size

    async def handler(ws: ServerConnection) -> None:
        for f in frames:
            await ws.send(f)
        await ws.close()

    view = RestingOrders()
    async with serve_local(handler) as url:
        async with Connection(url) as conn:
            async for event in view.follow(conn):
                if isinstance(event, Received) and event.seq == 3:
                    sizes.append((size_at(PX), size_at(PX2)))
    assert sizes == [(None, 100)]
    assert len(view) == 0


async def test_stopping_early_marks_the_view_incomplete_when_the_iterator_is_closed():
    async def handler(ws: ServerConnection) -> None:
        await ws.send(frame("order_state", _json_state(PX), 1))
        await ws.wait_closed()

    view = RestingOrders()
    async with serve_local(handler) as url:
        async with Connection(url) as conn:
            async with aclosing(view.follow(conn)) as events:
                async for _ in events:
                    break
            assert view.incomplete
    assert view.get("AAPL", BUY, PX).remaining_size == 100


def _json_state(price: int) -> dict:
    return {
        "strat_id": "mm-1",
        "instrument": "AAPL",
        "side": "BUY",
        "price": str(price),
        "state": "RESTING",
        "remaining_size": "100",
        "timestamp": "1",
    }


def _json_fill(remaining: str) -> dict:
    return {
        "exec_id": "x-1",
        "origin": "TEAM",
        "strat_id": "mm-1",
        "instrument": "AAPL",
        "side": "BUY",
        "order_price": str(PX),
        "fill_price": str(PX),
        "fill_size": "40",
        "remaining_size": remaining,
        "fill_kind": "STUDENT_TO_STUDENT",
        "liquidity": "MAKER",
        "fee": "1",
        "timestamp": "2",
    }


def test_a_disconnect_marks_the_view_incomplete_and_keeps_its_entries():
    view = view_of(rested())
    view.apply(Disconnected(None))
    assert view.incomplete
    assert view.get("AAPL", BUY, PX) is not None


def test_other_objects_leave_the_view_as_is():
    view = view_of(rested())
    view.apply(object())
    assert not view.incomplete and len(view) == 1
