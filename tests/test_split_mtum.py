"""MTUM's 4-for-1 split at the 5 November 2026 open, from a student program's side.

Each fixture is shaped on the platform contract at qte-platform c66eb2e4 (the vendored
protos are byte-identical to it), and its comment names the contract file and section.
The platform's rules are SPEC.md 12.2 ("SOFR and corporate actions") and Decision Log
DL-103. What the exchange does at the split, and what each test pins on the SDK side:

1. Positions. The split posts with no report (DL-103 item 5, `event.SplitPosting`, which
   the gateway maps to no outbound message), so only a fresh `account_state` shows the
   scaled position, with `as_of_report_seq` unmoved. `AccountReports` must keep the
   pre-split state until that reply, take it when it comes, and cut the reports around it
   by `as_of_report_seq` as usual.
2. Tickets and orders. Resting orders went at the 4 November close (`SESSION_CLOSE`);
   ordinary working tickets stop `TICKET_SPLIT_CANCELLED` at the open with `stopped_mark`
   0; a ticket sent before the first live quote is refused `REFERENCE_UNAVAILABLE`.
3. Valuation, books and history. Until MTUM's first live quote the position is valued at
   the scaled close, `round_half_even(P x b / a)`; history and a held book are as
   published, never split-adjusted.
4. A surviving cure ticket carries mixed units: `shares` is its pre-split fills plus its
   unfilled shares scaled, while `filled_shares` and `decision_price` stay pre-split.
"""

import json
from typing import Any

from fake_exchange import frame
from test_history import FakeHistory, collect, line, serve_history, synthetic_token
from test_orders import Recorder, received

from qte_sdk.account import AccountReports, covers, is_account_state
from qte_sdk.books import LatestBooks
from qte_sdk.connection import Received
from qte_sdk.contract.v1.common_pb2 import (
    LIVE_MARK,
    TICKET_COMPLETE,
    TICKET_SPLIT_CANCELLED,
    TICKET_WORKING,
    VWAP,
    ReasonCodes,
    TicketReasonCodes,
    TicketStatus,
)
from qte_sdk.contract.v1.common_pb2 import SERVICE as EXECUTOR_SERVICE
from qte_sdk.contract.v1.market_data_pb2 import LIVE, REFERENCE_UNAVAILABLE, Book, Mark
from qte_sdk.contract.v1.order_events_pb2 import AccountState, Execution, TicketState
from qte_sdk.history import HistoryClient
from qte_sdk.resting import RestingOrders
from qte_sdk.tickets import (
    LatestTickets,
    stopped_mark_of,
    stopped_time_of,
    ticket_reason_code_name,
)

# The split: `a` new shares for `b` old (SPEC.md 12.2; DL-103).
A, B = 4, 1

# Times, UTC epoch milliseconds. 4 November is EST, so the 16:00 ET close is 21:00 UTC
# and the 5 November 09:30 ET open is 14:30 UTC.
NOV4_OPEN = 1_793_802_600_000
NOV4_CLOSE = 1_793_826_000_000
NOV5_OPEN = 1_793_889_000_000
NOV5_QUERY = NOV5_OPEN + 5_000  # before MTUM's first live quote
NOV5_FIRST_QUOTE = NOV5_OPEN + 60_000

# MTUM's official close of 4 November in micro-dollars: a TWAP of marks, so off the tick.
# P / 4 ends in .5, so the half-even rounding of the scaled close is exercised.
CLOSE_NOV4 = 240_123_458

CASH = 5_000_000_000
NOV4_AFTER = NOV4_CLOSE + 60_000  # a query after the 4 November close
OUTSIDE = "LAST_OFFICIAL_CLOSE"  # the valuation basis outside a session


def round_half_even_div(n: int, d: int) -> int:
    """`n / d` rounded half to even, for positive integers."""
    q, r = divmod(n, d)
    if 2 * r > d or (2 * r == d and q % 2 == 1):
        q += 1
    return q


def split_quantity(q: int) -> int:
    """SPEC.md 12.2: `q' = sgn(q) x floor(|q| x a / b)`."""
    sign = -1 if q < 0 else 1
    return sign * (abs(q) * A // B)


def split_cash(q: int, close: int) -> int:
    """SPEC.md 12.2: `r = (|q| x a) mod b`, `cash = sgn(q) x floor(r x P / a)`."""
    sign = -1 if q < 0 else 1
    r = (abs(q) * A) % B
    return sign * (r * close // A)


# DL-103 item 1: the carried close becomes round_half_even(P x b / a).
SCALED_CLOSE = round_half_even_div(CLOSE_NOV4 * B, A)


def test_the_reference_arithmetic_matches_spec_12_2():
    assert SCALED_CLOSE == 60_030_864  # 60_030_864.5 rounds to the even neighbour
    assert split_quantity(137) == 548
    assert split_quantity(-50) == -200
    # A 4-for-1 always divides, so MTUM pays no cash in lieu.
    assert split_cash(137, CLOSE_NOV4) == 0 == split_cash(-50, CLOSE_NOV4)


# Fixtures.


def execution(report_seq: int, size: int, price: int, timestamp: int) -> str:
    """03-outbound-order-events.md, `execution`: one fill of a team order in MTUM, with
    `report_seq` on the envelope (01-envelope-and-session.md, "Sequence numbers")."""
    payload = {
        "exec_id": f"x-{report_seq}",
        "origin": "TEAM",
        "strat_id": "mt-1",
        "instrument": "MTUM",
        "side": "BUY",
        "order_price": str(price),
        "fill_price": str(price),
        "fill_size": str(size),
        "remaining_size": "0",
        "fill_kind": "STUDENT_TO_WALL",
        "liquidity": "TAKER",
        "fee": "-1000",
        "timestamp": str(timestamp),
    }
    return frame("execution", payload, report_seq=str(report_seq))


def account_state(
    request_ref: str,
    as_of_report_seq: int,
    mtum: int,
    mtum_price: int,
    cash: int,
    session_date: str,
    as_of: int,
    previous_close_equity: int,
    basis: str = "LIVE_MARK",
) -> str:
    """03-outbound-order-events.md, `account_state` and `PositionValue`, with `summary`
    an `account_summary` (same file, `account_summary`) embedded whole. Inside a session
    `valuation_basis` is `LIVE_MARK` (outside one, `LAST_OFFICIAL_CLOSE`), and a position
    with no valid mark yet is valued at the last official close ("The valuation basis",
    "Before a valid mark in a session").
    The reply carries no `report_seq` of its own ("Ordering")."""
    equity = cash + mtum * mtum_price
    summary = {
        "equity": str(equity),
        "cash": str(cash),
        "previous_close_equity": str(previous_close_equity),
        "daily_pnl": str(equity - previous_close_equity),
        "loss_level": "LOSS_LEVEL_NONE",
        "limits": [],
        "in_cure": False,
        "timestamp": str(as_of),
    }
    payload = {
        "request_ref": request_ref,
        "summary": summary,
        "cash": str(cash),
        "positions": [{"instrument": "MTUM", "quantity": str(mtum), "price": str(mtum_price)}],
        "valuation_basis": basis,
        "session_date": session_date,
        "as_of": str(as_of),
        "as_of_report_seq": str(as_of_report_seq),
    }
    return frame("account_state", payload)


def progress(accepted: int, due: int, filled: int) -> list[dict[str, str]]:
    """11-ticket-messages.md, "Progress (issue #2346)": a point at every 30-minute tick of
    the 4 November session at or after `accepted_time`, through its close. Due and filled
    climb evenly to the totals at the close (illustrative values)."""
    ticks = [t for t in range(NOV4_OPEN, NOV4_CLOSE + 1, 1_800_000) if t >= accepted]
    n = len(ticks)
    return [
        {
            "time": str(t),
            "due_shares": str(due * (i + 1) // n),
            "filled_shares": str(filled * (i + 1) // n),
        }
        for i, t in enumerate(ticks)
    ]


def ticket_state(update_time: int, **fields: Any) -> dict[str, Any]:
    """11-ticket-messages.md, `ticket_state`: a pod's `SERVICE` ticket in MTUM (the
    Winter term routes every pod ticket to the service, "The automatic execution service"),
    so `desk` is the empty string and `notes` is absent. `progress` follows "Progress
    (issue #2346)". Ticket messages carry no `report_seq` ("What applies to every ticket
    message", Resume)."""
    payload: dict[str, Any] = {
        "ticket_id": "48300",
        "pod": "pod-3",
        "desk": "",
        "instrument": "MTUM",
        "side": "BUY",
        "shares": "2000",
        "limit_price": "241000000",
        "urgency": "URGENCY_MEDIUM",
        "decision_price": "240100000",
        "accepted_time": str(NOV4_OPEN + 600_000),
        "planned_completion_time": str(NOV5_OPEN + 3_600_000),
        "due_shares": "1000",
        "filled_shares": "600",
        "remaining_shares": "1400",
        "average_fill_price": "240200000",
        "net_fee": "-3000",
        "status": "TICKET_WORKING",
        "thesis_id": "TH-2026-031",
        "thesis_version": 1,
        "cure": False,
        "update_time": str(update_time),
        "executor": "SERVICE",
        "algorithm": "VWAP",
        "activity": "MARKET_CLOSED",
        "behind_schedule": True,
        "config_version": 1,
        "last_assessment_time": str(NOV4_CLOSE),
        "filled_notional": str(600 * 240_200_000),
        "progress": progress(NOV4_OPEN + 600_000, 1000, 600),
    }
    payload.update(fields)
    return payload


def book(grid_time: int, bid: list[str], ask: list[str], condition: str) -> dict[str, Any]:
    """05-outbound-market-data.md, `book` and `WallLevel`. An empty live quote has both
    wall arrays empty ("One-sided live quote ... Empty live quote")."""
    return {
        "instrument": "MTUM",
        "grid_time": str(grid_time),
        "bid_levels": [{"price": p, "size": "300"} for p in bid],
        "ask_levels": [{"price": p, "size": "300"} for p in ask],
        "student_bid_levels": [],
        "student_ask_levels": [],
        "condition": condition,
    }


def numbered(*frames: str) -> list[str]:
    """Give each frame the connection's next `seq`, 1 on (01-envelope-and-session.md)."""
    out = []
    for seq, text in enumerate(frames, 1):
        env = json.loads(text)
        env["seq"] = seq
        out.append(json.dumps(env))
    return out


# Path 1: positions x4 and the close /4, seen only through a fresh account_state.


async def test_the_split_posts_no_report_so_account_reports_keeps_the_pre_split_account():
    # 4 November: the team holds +137 MTUM; its last report is 40.
    reports = AccountReports()
    rec = Recorder()
    ref = await reports.query(rec)
    events = await received(
        *numbered(
            account_state(ref, 40, 137, CLOSE_NOV4, CASH, "2026-11-04", NOV4_AFTER, 0, OUTSIDE),
            # 5 November open: the split stops the ordinary ticket (11-ticket-messages.md,
            # choice 17). Nothing else reaches the team: SplitPosting has no wire message.
            frame(
                "ticket_state",
                ticket_state(
                    NOV5_OPEN,
                    status="TICKET_SPLIT_CANCELLED",
                    stopped_time=str(NOV5_OPEN),
                    stopped_mark="0",
                    progress=progress(NOV4_OPEN + 600_000, 1000, 600)
                    + [{"time": str(NOV5_OPEN), "due_shares": "1000", "filled_shares": "600"}],
                ),
            ),
        )
    )
    [reply, stop] = events
    assert reports.update(reply) == [reply]
    assert reports.update(stop) == []
    # No report came, so the account held is still the pre-split one: the program must
    # query again (account.py, AccountReports docstring, "A split posts with no report").
    [held] = reports.state.positions
    assert (held.instrument, held.quantity, held.price) == ("MTUM", 137, CLOSE_NOV4)


async def test_the_query_after_the_open_brings_the_scaled_position_and_close():
    reports = AccountReports()
    rec = Recorder()
    first = await reports.query(rec)
    [before] = await received(
        account_state(first, 40, 137, CLOSE_NOV4, CASH, "2026-11-04", NOV4_AFTER, 0, OUTSIDE)
    )
    reports.update(before)

    second = await reports.query(rec)
    assert [type_ for type_, _ in rec.sent] == ["account_query", "account_query"]
    cash = 5_000_000_000 + split_cash(137, CLOSE_NOV4)
    previous_close_equity = 5_000_000_000 + 137 * CLOSE_NOV4
    [after] = await received(
        account_state(
            second,
            40,  # the split adds no report, so as_of_report_seq does not move
            split_quantity(137),
            SCALED_CLOSE,
            cash,
            "2026-11-05",
            NOV5_QUERY,
            previous_close_equity,
        )
    )
    assert is_account_state(after)
    assert reports.update(after) == [after]
    state = reports.state
    assert isinstance(state, AccountState)
    assert state.valuation_basis == LIVE_MARK
    assert state.session_date == "2026-11-05"
    assert state.as_of_report_seq == 40
    [mtum] = state.positions
    assert (mtum.instrument, mtum.quantity, mtum.price) == ("MTUM", 548, 60_030_864)
    assert state.cash == 5_000_000_000  # 4-for-1: no fraction, no cash in lieu
    # Equity is continuous up to the half-even rounding of the close (DL-103 item 1).
    assert state.summary.equity == 5_000_000_000 + 548 * 60_030_864
    assert state.summary.daily_pnl == 548 * 60_030_864 - 137 * CLOSE_NOV4 == -274


async def test_a_short_is_scaled_toward_zero_like_a_long():
    reports = AccountReports()
    ref = await reports.query(Recorder())
    [reply] = await received(
        account_state(
            ref,
            12,
            split_quantity(-50),
            SCALED_CLOSE,
            7_000_000_000,
            "2026-11-05",
            NOV5_QUERY,
            7_000_000_000 - 50 * CLOSE_NOV4,
        )
    )
    assert reports.update(reply) == [reply]
    [mtum] = reports.state.positions
    assert mtum.quantity == -200


async def test_reports_around_the_post_split_reply_are_cut_by_as_of_report_seq():
    """A pre-split fill (report 40) is in the reply; a post-split fill (41) is not. The
    split itself moved no number, so the usual cut still holds across it."""
    reports = AccountReports()
    ref = await reports.query(Recorder())
    late_pre_split, reply, post_split = await received(
        *numbered(
            execution(40, 10, 240_150_000, NOV4_CLOSE - 60_000),
            account_state(
                ref,
                40,
                split_quantity(137),
                SCALED_CLOSE,
                5_000_000_000,
                "2026-11-05",
                NOV5_QUERY,
                5_000_000_000 + 137 * CLOSE_NOV4,
            ),
            execution(41, 40, 60_040_000, NOV5_FIRST_QUOTE + 1_000),
        )
    )
    # Report 40 arrived while the query was out, so it is held and then dropped as covered.
    assert reports.update(late_pre_split) == [late_pre_split]
    assert reports.update(reply) == [reply]
    assert covers(reports.state, late_pre_split)
    # The post-split fill, in post-split shares and price, is applied on the new account.
    assert reports.update(post_split) == [post_split]
    assert not covers(reports.state, post_split)
    fill = post_split.message
    assert isinstance(fill, Execution)
    assert (fill.fill_size, fill.fill_price) == (40, 60_040_000)


async def test_a_post_split_fill_before_the_reply_is_held_and_returned_after_it():
    """A post-split fill (report 41) that arrives while the query is out is held and given
    back after the reply, which reads the account at report 40 (03-outbound-order-events.md,
    `account_state`, "Ordering")."""
    reports = AccountReports()
    ref = await reports.query(Recorder())
    early_fill, reply = await received(
        *numbered(
            execution(41, 40, 60_040_000, NOV5_FIRST_QUOTE + 1_000),
            account_state(
                ref,
                40,
                split_quantity(137),
                SCALED_CLOSE,
                CASH,
                "2026-11-05",
                NOV5_QUERY,
                CASH + 137 * CLOSE_NOV4,
            ),
        )
    )
    assert reports.update(early_fill) == [early_fill]
    assert reports.update(reply) == [reply, early_fill]
    [mtum] = reports.state.positions
    # The program adds the fill to the reply's post-split position: 548 + 40, never 137 + 40.
    assert mtum.quantity + early_fill.message.fill_size == 588


# Path 2: resting orders, ordinary tickets and the first live quote.


async def test_mtum_orders_resting_on_4_november_are_cancelled_at_that_close():
    """03-outbound-order-events.md, `order_state` and `order_cancelled`; 06-reason-codes.md
    band 1800, `SESSION_CLOSE`. SPEC.md 12.2: resting orders in the instrument are
    cancelled at the preceding close (every close cancels every resting order)."""
    resting_payload = {
        "strat_id": "mt-1",
        "instrument": "MTUM",
        "side": "SELL",
        "price": "240500000",
        "state": "RESTING",
        "remaining_size": "100",
        "timestamp": str(NOV4_CLOSE - 120_000),
    }
    cancel_payload = {
        "origin": "TEAM",
        "strat_id": "mt-1",
        "instrument": "MTUM",
        "side": "SELL",
        "price": "240500000",
        "cancelled_size": "100",
        "reason_code": "SESSION_CLOSE",
        "timestamp": str(NOV4_CLOSE),
    }
    rests, cancelled = await received(
        *numbered(
            frame("order_state", resting_payload, report_seq="38"),
            frame("order_cancelled", cancel_payload, report_seq="39"),
        )
    )
    orders = RestingOrders()
    orders.apply(rests)
    assert len(orders) == 1
    orders.apply(cancelled)
    assert len(orders) == 0
    assert cancelled.message.reason_code == ReasonCodes.SESSION_CLOSE
    assert cancelled.unknown_enum_names() == {}


async def test_a_working_vwap_ticket_stops_split_cancelled_at_the_open_with_fills_standing():
    """11-ticket-messages.md, choice 17: at the ex-date open every working ordinary ticket
    in the instrument stops `TICKET_SPLIT_CANCELLED`, committed fills standing,
    `stopped_mark` 0, no child resting. The stop is the progress end point."""
    working = ticket_state(NOV4_CLOSE)
    stopped_progress = working["progress"] + [
        {"time": str(NOV5_OPEN), "due_shares": "1000", "filled_shares": "600"}
    ]
    stopped = ticket_state(
        NOV5_OPEN,
        status="TICKET_SPLIT_CANCELLED",
        stopped_time=str(NOV5_OPEN),
        stopped_mark="0",
        progress=stopped_progress,
    )
    before, after = await received(
        *numbered(frame("ticket_state", working), frame("ticket_state", stopped))
    )
    assert isinstance(after, Received)
    assert after.unknown_enum_names() == {}
    state = after.message
    assert isinstance(state, TicketState)
    assert state.status == TICKET_SPLIT_CANCELLED
    assert TicketStatus.Name(state.status) == "TICKET_SPLIT_CANCELLED"
    assert (state.executor, state.algorithm) == (EXECUTOR_SERVICE, VWAP)
    # Fills stand, in pre-split shares and prices.
    assert (state.shares, state.filled_shares, state.remaining_shares) == (2000, 600, 1400)
    assert state.average_fill_price == 240_200_000
    assert stopped_time_of(state) == NOV5_OPEN
    assert stopped_mark_of(state) is None  # no valid mark at the open: sent as 0
    assert [p.time for p in state.progress][-1] == NOV5_OPEN

    tickets = LatestTickets()
    assert tickets.update(before)
    assert [t.ticket_id for t in tickets.working()] == ["48300"]
    assert tickets.update(after)
    assert tickets.working() == []
    assert tickets.get("48300").status == TICKET_SPLIT_CANCELLED


async def test_a_ticket_sent_before_the_first_live_quote_is_refused_reference_unavailable():
    """11-ticket-messages.md, `ticket` ("the mark, ... `REFERENCE_UNAVAILABLE` before its
    first valid mark") and `ticket_reject`; DL-103 item 5: the open empties MTUM's quote,
    so there is no valid mark until its first live quote."""
    payload = {
        "request_ref": "t-9",
        "request_kind": "TICKET_REQUEST_SUBMIT",
        "reason_code": "REFERENCE_UNAVAILABLE",
        "receipt_time": str(NOV5_OPEN + 2_000),
    }
    [reject] = await received(*numbered(frame("ticket_reject", payload)))
    assert reject.unknown_enum_names() == {}
    code = reject.message.reason_code
    assert code == TicketReasonCodes.REFERENCE_UNAVAILABLE
    assert ticket_reason_code_name(code) == "REFERENCE_UNAVAILABLE"


async def test_mtum_has_no_valid_mark_at_the_open_until_its_first_live_quote():
    """05-outbound-market-data.md, `mark` and "Non-normative example: a
    reference-unavailable mark": `value` 0 with `REFERENCE_UNAVAILABLE`."""
    unavailable = {
        "instrument": "MTUM",
        "sampled_at": str(NOV5_OPEN + 1_000),
        "value": "0",
        "condition": "REFERENCE_UNAVAILABLE",
    }
    live = {
        "instrument": "MTUM",
        "sampled_at": str(NOV5_FIRST_QUOTE),
        "value": "60035000",
        "condition": "LIVE",
    }
    first, second = await received(*numbered(frame("mark", unavailable), frame("mark", live)))
    assert isinstance(first.message, Mark)
    assert (first.message.value, first.message.condition) == (0, REFERENCE_UNAVAILABLE)
    assert (second.message.value, second.message.condition) == (60_035_000, LIVE)


# Path 3: books and history are as published, never split-adjusted.


async def test_a_held_4_november_book_stays_pre_split_until_a_5_november_book_replaces_it():
    """books.py keeps a book until a later one and never clears at a close. 05, "When a
    `book` is published", rule 2: the first grid publication of the 5 November session
    carries MTUM's book if it has one; with its quote emptied at the open (DL-103 item 5)
    that book has no wall and condition `REFERENCE_UNAVAILABLE` (qte-core confirmed on 28 Oct
    2026: the publisher clears its last-book set at the session open)."""
    pre = book(NOV4_CLOSE, ["240110000"], ["240140000"], "LIVE")
    empty = book(NOV5_OPEN + 100, [], [], "REFERENCE_UNAVAILABLE")
    quoted = book(NOV5_FIRST_QUOTE, ["60030000"], ["60040000"], "LIVE")
    events = await received(
        *numbered(frame("book", pre), frame("book", empty), frame("book", quoted))
    )
    books = LatestBooks()
    assert books.update(events[0].message)
    held = books.get("MTUM")
    assert isinstance(held, Book)
    # Until a 5 November book arrives, the held book is 4 November's, in pre-split prices:
    # only its grid_time tells the program so.
    assert held.grid_time < NOV5_OPEN
    assert held.bid_levels[0].price == 240_110_000
    assert books.update(events[1].message)
    held = books.get("MTUM")
    assert (list(held.bid_levels), list(held.ask_levels)) == ([], [])
    assert held.condition == REFERENCE_UNAVAILABLE
    assert books.update(events[2].message)
    assert books.get("MTUM").bid_levels[0].price == 60_030_000


async def test_history_serves_both_sides_of_the_split_as_published():
    """09-history-api.md: history is the published feed, byte-identical to what teams
    received, so prices on either side of the ex-date are not adjusted. Lines are
    05-outbound-market-data.md `mark` messages."""
    token = synthetic_token()
    nov4 = line(
        "mark",
        1,
        {
            "instrument": "MTUM",
            "sampled_at": str(NOV4_CLOSE - 1_000),
            "value": "240123500",
            "condition": "LIVE",
        },
    )
    nov5 = line(
        "mark",
        1,
        {
            "instrument": "MTUM",
            "sampled_at": str(NOV5_FIRST_QUOTE),
            "value": "60035000",
            "condition": "LIVE",
        },
    )
    objects = {("2026-11-04", "MTUM", "mark"): nov4, ("2026-11-05", "MTUM", "mark"): nov5}
    # The fake's universe is TEST, AAA and BBB; MTUM is served by stating both ready.
    fake = FakeHistory(token, objects, status_override=dict.fromkeys(objects, "ready"))
    with serve_history(fake) as url:
        client = HistoryClient(url, token)
        [before] = await collect(client.fetch("2026-11-04", "MTUM", "mark"))
        [after] = await collect(client.fetch("2026-11-05", "MTUM", "mark"))
    assert isinstance(before, Mark) and isinstance(after, Mark)
    assert before.value == 240_123_500  # as published, pre-split
    assert after.value == 60_035_000
    # The SDK's documented adjustment (history.py docstring): multiply earlier prices by b/a.
    assert round_half_even_div(before.value * B, A) == 60_030_875


# Path 4: a surviving cure ticket's mixed units.


async def test_a_surviving_cure_ticket_reports_mixed_units_and_keeps_working():
    """11-ticket-messages.md, choice 17: a cure is not cancelled; its unfilled shares `u`
    become floor(u x a / b) and it is reported with `shares` = committed fills plus the
    scaled unfilled shares, `TICKET_WORKING`. `filled_shares` and `decision_price` stay
    pre-split; `progress` is emptied from the split on ("Progress", last paragraph). A cure
    has no `limit_price` and carries `cure_deadline_date` (`ticket_state` table)."""
    cure_before = ticket_state(
        NOV4_CLOSE,
        ticket_id="48310",
        side="SELL",
        shares="900",
        filled_shares="300",
        remaining_shares="600",
        due_shares="400",
        average_fill_price="240300000",
        filled_notional=str(300 * 240_300_000),
        algorithm="TWAP",
        cure=True,
        cure_deadline_date="2026-11-05",
        accepted_time=str(NOV4_CLOSE - 3_600_000),
        progress=progress(NOV4_CLOSE - 3_600_000, 400, 300),
        thesis_id=None,
        thesis_version=None,
    )
    for key in ("limit_price", "thesis_id", "thesis_version"):
        del cure_before[key]
    scaled_unfilled = 600 * A // B
    cure_after = dict(
        cure_before,
        shares=str(300 + scaled_unfilled),
        remaining_shares=str(scaled_unfilled),
        # choice 17 (issue #2235): the due part the fills cover stays, the rest scales.
        due_shares=str(300 + (400 - 300) * A // B),
        update_time=str(NOV5_OPEN),
        progress=[],
    )
    before, after = await received(
        *numbered(frame("ticket_state", cure_before), frame("ticket_state", cure_after))
    )
    assert after.unknown_enum_names() == {}
    state = after.message
    assert state.cure and not state.HasField("limit_price")
    assert state.status == TICKET_WORKING
    assert (state.shares, state.filled_shares, state.remaining_shares) == (2700, 300, 2400)
    assert state.due_shares == 700
    assert state.decision_price == 240_100_000  # pre-split P0
    assert state.average_fill_price == 240_300_000  # pre-split fills
    assert list(state.progress) == []
    tickets = LatestTickets()
    tickets.update(before)
    assert tickets.update(after)
    assert [t.ticket_id for t in tickets.working()] == ["48310"]
    # A 4-for-1 never empties a cure (u >= 1 gives 4u >= 4), so the Complete and
    # split-cancelled cure outcomes of choice 17 are unreachable for MTUM.
    assert all(u * A // B >= 1 for u in range(1, 10_000))
    assert state.status != TICKET_COMPLETE
