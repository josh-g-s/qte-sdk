"""Read an option chain and its Greeks, rest one limit order on a contract and cancel it.

Set QTE_URL and QTE_TOKEN first, in ./.env or the environment (see docs/quickstart.md),
then:

    python examples/options_bot.py --strat-id <your strategy> --underlying SPY --seconds 30

The example reads the exchange's instruments table for the option contracts listed on
--underlying (by default the first option underlying the table names) and subscribes to
all of them. That brings the day's chain (`OptionChain`), each contract's latest Greeks
(`OptionGreeks`) and its book. It keeps the latest Greeks of each contract with
`qte_sdk.options.LatestGreeks` and the latest books with `qte_sdk.books.LatestBooks`, since
the exchange publishes each only when it is calculated or changes.

It then picks one contract: the nearest expiry of the underlying the chain lists that is
not reducing-only, and in it the active or obligated contract whose strike is nearest the
forward that the latest valid Greeks give. It passes over a contract whose book's
`trading_state` is not OPTION_TRADING, one your team cannot trade, and one whose wall
leaves no room for a resting buy, and tries the next nearest strike.

On that contract it sends one LIMIT buy of one lot (one contract, never 100 shares), naming
the contract by its OCC symbol. It waits for the exchange to report the order resting,
cancels that price level, waits for the cancel to be confirmed and stops. One contract is
also the Options Market Making minimum per obligated side, but this example rests one side
for a moment and does not meet an obligation: a desk quotes both sides and keeps them. It never
retries an order: a reject is printed with its reason's name and the example stops. A
DUPLICATE_ORDER_AT_LEVEL reject means your team already has an order at that level, often
one an earlier run left: the example names the level, so you can cancel that order
yourself, and exits with status 1.

It never means to leave the order resting. When --seconds are up, or on Ctrl+C, it stops
choosing and cancels the order's level, waiting at most --drain-seconds for the exchange
to confirm. If it cannot confirm, it names the level where the order may still rest, so
you can cancel it yourself, and exits with status 1. A Ctrl+C during that wait does not
start --drain-seconds again. Press Ctrl+C twice to stop at once;
if an order was sent, it then names the level where it may still rest and exits with
status 1. A Ctrl+C before any order is sent stops the example with status 0.

It never sends a cancel for its order without its own `accepted` for that order. A cancel
names a price level, not an order, so before that `accepted` it could take another order
of your team resting there: one an earlier run left, or another strategy's still in its
order delay when this one was sent, which then has this one refused as a duplicate. So at
time-up or Ctrl+C with no `accepted`, it waits out --drain-seconds for the reply. If the
`accepted` comes, it cancels as usual; if none comes, or a resume lost it, it cancels
nothing, warns that its order may still rest at the level it names (check with
order_snapshot or the console, and cancel it yourself if it is this one) and exits with
status 1. There is no exception: it never infers which order is its own (see On a drop).
Its own `accepted` is the one whose request_ref is that of the order it sent: the protocol
has no order id, and a cancel names a contract, side and price. It trades one contract,
so only orders of its strategy on that contract are looked at.

The price. Your orders rest only strictly inside the wall's best bid and ask: one at the
wall's own price or beyond it is not left resting (`order_cancelled` with
REMAINDER_OUTSIDE_BAND). So the price is one tick above the wall's best bid, the lowest at
which a buy can rest, and the farthest from the offer. It must be at least three ticks
below every ask, or the contract is passed over. The tick and the lot size come from the
instruments table, never from this file. A price inside the wall's quote is close to the
contract's mark, so it should be inside the price collar, which the exchange sets and may
change; a PRICE_COLLAR reject is printed like any other.

The order can fill. There is no post-only order, so if the asks fall to its price during
the order delay, or a seller trades with it while it rests, it trades. Fills are printed,
and any remainder is still cancelled. Run it on a contract your team is not otherwise
trading at that price: a cancel names a price level, not an order, so it removes whichever
of your team's orders rests there when it is applied.

Timing. The exchange holds every order message for its order delay before applying it, and
an order must rest for a minimum time before it may be cancelled. Both are set by the
exchange, so nothing here assumes their values: a cancel rejected MIN_REST_VIOLATION is
sent again after the exchange's next market-data grid point, then after two more, four
more and so on. One rejected for a message budget is sent again only after a minute, since
rejected messages count toward the budget too. That is longer than the default
--drain-seconds, so a budget reject at the end names itself: wait a minute and cancel the
level yourself. To have the run wait out such a reject itself before it exits, start it
with --drain-seconds above 60.

What it cannot tell apart. It follows its order by strategy, contract, side and price
level, and follows an amend's move to a new level. If anything else acts on that
strategy's buy orders in the contract while it runs (another program, or an amend that
fills at once), its reports may be misread, so use a strategy nothing else is using. If
messages from the exchange are missed after its order is accepted, it still cancels, but
then exits with status 1 and a warning to check your orders and fills; missed before its
`accepted`, it still sends no cancel without that `accepted`, as above.

On a drop. The example connects with `qte_sdk.reconnect.ReconnectingSession`, so if the
connection drops, or the exchange restarts, it reconnects by itself, waiting longer between
attempts (at most 30 seconds). While it is down it sends nothing and keeps what it knows of
its order. Each new session subscribes to the contracts again and resumes the team's order
reports: the exchange either replays every report missed, or sends a snapshot of the
orders resting now. Once that is complete, the example looks at its view of resting
orders (`qte_sdk.resting.RestingOrders`). If its `accepted` came, it knows its order: if
the order rests at its level, it cancels it as usual; if nothing of its strategy rests on
the contract, nothing of it rests and the example stops; if orders of its strategy rest at
other prices there (one perhaps its order, moved), it names them, cancels nothing and exits
with status 1. If no `accepted` came, it never infers which order is its own: any order of
its strategy seen on the contract after the drop, at any price, in the resume (each
snapshot entry as it comes, so a snapshot cut short or damaged still counts) or in a live
order report before the `accepted`, may or may not be its order, so it cancels nothing,
warns naming each order seen and how to check (order_snapshot or the console) and exits
with status 1 at once. With none seen, the order may still be in its order delay, so it
keeps waiting for the reply as above, cancels as usual once the `accepted` comes, and
otherwise cancels nothing, warns that it may still rest and exits with status 1. After a
snapshot, fills made while it was down are not reported, and the summary says so. An
order or a cancel in flight at the drop is never sent again: the replay or the snapshot says what
became of it, and a cancel is sent afresh only while the order rests. Market data sent
while it was down is not replayed, so before it chooses a contract again it waits for a
session state, an option chain, Greeks and a book received since it reconnected. If the
order reports cannot be recovered (the exchange did not resume, or messages were missed
meanwhile) after its order was sent, it treats them as missed, as above, and sends no
order from then on; a cancel in flight at the drop is then sent afresh, and a reply not
yet come counts as lost until a later resume completes. A resume cut short by a message
missed or unreadable (a `SeqGap`, or a `DecodeFailed` of no known type) ends with no
`ResumeComplete`: the example takes it as one that did not recover the reports, so the
session is up again and an accepted order is still cancelled. Before an order is sent,
a resume that does not recover the reports leaves nothing of its own to miss, so it
chooses and sends as usual, but a message missed means it sends no order and says why.
If the session never comes up (its first resume never completes), it says so, sends
nothing and exits with status 1. A new whose send finds the connection down
(`NotConnected`) was not sent: no order of it can rest, and it chooses again once the
connection is back. An
`accepted` that comes through the resume is printed as answered after a reconnect, with
no round trip. If a cancel, one sent before a drop included, takes an order that is not
its own at a level its order has left, it cancels again where its order rests and, at
the end, warns of it and exits with status 1. If it takes another strategy's order at
its own level, this one does not rest there (the team has one order per level) and
where it rests is not known: it sends no further cancel, warns that it may still rest
and how to check, and exits with status 1. When one warning already names the orders
that may be this one, no generic warning of missed messages follows it.
--seconds and --drain-seconds count the time down too.
Only the first connection is not retried: if it fails, the example says why and stops.
A reconnect that fails for good (a refused session, or a 1008 `credential revoked`)
while its order may still rest names the order and how to check it (order_snapshot or
the console) and exits with status 1; a watch-only run still ends on its `watch-only:`
line.
--seconds also bounds the first subscribe: if the connection stops taking data before it
is sent, the example says so and exits with status 1, with no order sent.
The replay or the snapshot covers only what the exchange had applied when it answered: an
order still waiting out its order delay at the drop can be applied just after that, which
is why, with no `accepted`, an empty resume is not taken as the order gone.

Watching only. With --watch-only (or QTE_WATCH_ONLY=1) it reads the table, subscribes, keeps
the chain, Greeks and books, and chooses a contract exactly as above, printing the reason for
each contract it passes over. It then prints the order it would send, as one line starting
`WOULD SEND (watch-only, not sent):` with the contract's OCC symbol, side, size and price,
and sends nothing at all: no order and no cancel. It keeps reading until --seconds are up or
Ctrl+C, then exits with status 0 and a last line starting `watch-only: chose`, or, if no
contract could be chosen, with status 1 and a last line starting `watch-only: NO CONTRACT
CHOSEN:` that names why. If the connection drops, it prints that and reconnects, as above.
--drain-seconds has no use then, since there is nothing to cancel. --strat-id is still
required, as for any run of this example.
QTE_WATCH_ONLY, read without case or surrounding spaces, turns it on with 1, true, yes or on,
and leaves it off when unset, empty, 0, false, no or off; any other value stops the example
with status 2 before it connects, so a mistyped value never sends an order.
"""

import argparse
import asyncio
import contextlib
import dataclasses
import os
import signal
import sys
import time
from collections.abc import AsyncIterator

from google.protobuf.message import Message
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, InvalidHandshake

import qte_sdk.logs
from qte_sdk.books import LatestBooks
from qte_sdk.connection import (
    Connected,
    Disconnected,
    Received,
    ReportGap,
    ResumeComplete,
    SessionRejected,
    describe_close,
)
from qte_sdk.contract.v1.common_pb2 import (
    BUY,
    LIMIT,
    RESTING,
    STALE,
    MarketSessionPhase,
    ReasonCodes,
)
from qte_sdk.contract.v1.market_data_pb2 import LIVE
from qte_sdk.contract.v1.order_events_pb2 import (
    Accepted,
    Execution,
    OrderCancelled,
    OrderState,
    Reject,
)
from qte_sdk.contract.v1.session_pb2 import Instruments, OrderSnapshot
from qte_sdk.instruments import can_trade, instrument_info
from qte_sdk.market_data import (
    Book,
    DecodeFailed,
    SeqGap,
    SessionState,
    as_market_data,
    subscribe,
)
from qte_sdk.options import (
    OPTION_GREEKS_VALID,
    OPTION_ROLE_ACTIVE,
    OPTION_ROLE_OBLIGATED,
    OPTION_TRADING,
    LatestGreeks,
    OptionChain,
    OptionGreeks,
    OptionTradingState,
    chain_contracts,
    expiry_date,
    greek_to_decimal,
    listed_contracts,
    option_underlyings,
    parse_option_symbol,
    trading_state,
    vol_to_decimal,
)
from qte_sdk.orders import (
    is_order_event,
    new_request_ref,
    reason_code_name,
    request_ref_of,
    send_cancel,
    send_new,
)
from qte_sdk.reconnect import NotConnected, ReconnectingSession, Retrying
from qte_sdk.resting import RestingOrders
from qte_sdk.session import (
    MissingToken,
    MissingURL,
    SessionNotAcknowledged,
    resolve_url,
)
from qte_sdk.units import to_decimal

# The least room, in ticks, between the order's price and the best ask. There is no
# post-only order, so an ask that falls to the price during the order delay fills it; the
# room makes that less likely, never impossible. A local choice.
MIN_ROOM_TICKS = 3

# The longest wait for the instruments table after the session opens. A local choice.
TABLE_SECONDS = 10.0

# Rejects of a cancel for a message budget. Each leaves the order unchanged, and rejected
# messages count toward the budget too, so a retry waits a full window, as
# docs/developing-your-algo.md asks after a budget reject.
BUDGET_REJECTS = frozenset({ReasonCodes.MESSAGE_BUDGET_EXCEEDED, ReasonCodes.BURST_CAP_EXCEEDED})
BUDGET_WAIT_SECONDS = 60.0

# Why a contract cannot be chosen yet, though it may be once more data arrives.
WAIT = "wait"


# The values of QTE_WATCH_ONLY, after stripping and lower-casing; any other is an error.
WATCH_OFF = ("", "0", "false", "no", "off")
WATCH_ON = ("1", "true", "yes", "on")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rest one limit order on an option contract and cancel it, or with "
        "--watch-only print the order it would send and send nothing."
    )
    parser.color = False  # Python 3.14 colours argparse's output; it is read as plain text.
    parser.add_argument(
        "--underlying",
        default=os.environ.get("QTE_UNDERLYING"),
        help="option underlying, such as SPY (default: $QTE_UNDERLYING, else the first "
        "option underlying in the exchange's instruments table)",
    )
    parser.add_argument(
        "--strat-id",
        default=os.environ.get("QTE_STRAT_ID"),
        help="a strategy ID registered for your team (default: $QTE_STRAT_ID)",
    )
    parser.add_argument(
        "--seconds", type=float, default=30.0, help="stop after this long (default 30)"
    )
    parser.add_argument(
        "--drain-seconds",
        type=float,
        default=10.0,
        help="the longest wait, at the end, for the cancel to be confirmed (default 10; "
        "a cancel rejected for a message budget is sent again only after 60)",
    )
    # A value not recognised is an error, never read as off: off sends a real order.
    watch_env = os.environ.get("QTE_WATCH_ONLY", "").strip().lower()
    if watch_env not in WATCH_OFF + WATCH_ON:
        parser.error(
            "QTE_WATCH_ONLY must be unset, empty, or one of 0, false, no, off (off) or "
            "1, true, yes, on (on)"
        )
    parser.add_argument(
        "--watch-only",
        action="store_true",
        default=watch_env in WATCH_ON,
        help="read and choose a contract as usual, print the order it would send, and send "
        "nothing: no order, no cancel. Exits 0 if a contract was chosen, else 1 with the "
        "reason (default: on if $QTE_WATCH_ONLY is 1, true, yes or on; off if it is unset, "
        "empty, 0, false, no or off; any other value is refused)",
    )
    args = parser.parse_args(argv)
    if not args.strat_id:
        parser.error(
            "pass --strat-id or set QTE_STRAT_ID to a strategy ID registered for your team"
        )
    return args


def price_text(price: int) -> str:
    return str(to_decimal(price))


class OptionsBot:
    def __init__(
        self,
        session: ReconnectingSession,
        table: Instruments,
        underlying: str,
        strat_id: str,
        watch_only: bool = False,
        view: RestingOrders | None = None,
    ) -> None:
        self.session = session
        # The team's resting orders, from the session's order reports, applied as each event
        # is handled; read after a resume.
        self.view = view
        self.table = table
        self.underlying = underlying
        self.strat_id = strat_id
        self.chain: OptionChain | None = None
        # The latest of each: the exchange sends Greeks at each calculation and a book only
        # when it changes.
        self.greeks = LatestGreeks()
        self.books = LatestBooks()
        self.market: SessionState | None = None
        self.passed_over: set[tuple[str, str]] = set()  # (contract, why), printed once
        # The order, once chosen. `ref` is recorded before the send, so a send cut short
        # still counts as an order that may have reached the exchange.
        self.contract: str | None = None
        self.price = 0
        self.ref: str | None = None
        self.sent_at = 0.0
        self.dropped_since_send = False  # the connection dropped after the order was sent
        self.accepted = False  # until then, no report at its level is taken as its own
        self.resting = False  # the exchange has reported it resting
        self.gone = False  # nothing of it rests: cancelled, filled or never left resting
        self.rejected = False
        self.duplicate = False  # rejected because the team already rests at the level
        self.filled = 0
        # The cancel: its request_ref and price while one is in flight, and the grid point
        # to wait for before sending it again after a reject that may pass later.
        self.cancel_ref: str | None = None
        # Cancels sent before a drop and forgotten at the resume: a late reply to one is
        # still this example's cancel, so it is matched by these.
        self.earlier_cancel_refs: set[str] = set()
        self.cancel_price = 0
        self.grid_points = 0
        self.retry_at: int | None = None
        self.backoff = 1
        self.retry_after = 0.0  # time.monotonic() before which no cancel is sent
        self.budget_reject: str | None = None  # why the cancel waits that long, if it does
        self.cancel_failed: str | None = None
        self.stopping = False  # no new order; cancel the one sent, if any
        self.missed = False  # whether any message from the exchange was missed
        # Whether the last resume failed to recover the order reports, so a reply not yet
        # come never comes; a later resume that completes clears it.
        self.reply_lost = False
        self.gave_up = False  # stopping with the reply lost: nothing more to wait for
        # Whether a warning already names the orders that may be this one and how to
        # check them: the generic "messages were missed" warning would add nothing.
        self.unsure = False
        self.warnings: list[str] = []  # printed at the end, which then exits with status 1
        # --watch-only: no message is ever sent, so `ref` stays None. `would_send` is the
        # order it would have sent, once chosen; `why_none` why no contract is chosen yet.
        self.watch_only = watch_only
        self.would_send: str | None = None
        self.why_none = "no contract was chosen yet"
        # Whether a session is up and its resume complete; nothing is sent until it is.
        self.up = True
        self.ever_up = False  # whether any session's resume has completed
        # Whether a session announced a resume still to complete, and whether its
        # `resume_ack` has been delivered: see `on_missed`.
        self.resuming = False
        self.resume_answered = False
        self.unreported_fills = False  # resumed from a snapshot after the order was sent
        self.subscribe_to: list[str] = []  # contracts to subscribe to once a session is up

    @property
    def finished(self) -> bool:
        if self.rejected or self.gone or self.gave_up or self.cancel_failed is not None:
            return True
        return self.stopping and self.ref is None

    @property
    def where(self) -> str:
        return f"{self.contract} BUY @ {price_text(self.price)}"

    # Choosing a contract

    def market_open(self) -> bool:
        state = self.market
        return (
            state is not None and state.state == MarketSessionPhase.OPEN and not state.outage_active
        )

    def forward(self, contracts: list[str]) -> tuple[str, OptionGreeks] | None:
        """The contract whose Greeks are the latest valid ones among `contracts`, with
        those Greeks. Rely on them only when their status is OPTION_GREEKS_VALID."""
        best: tuple[str, OptionGreeks] | None = None
        for contract in contracts:
            greeks = self.greeks.get(contract)
            if (
                greeks is None
                or contract in self.greeks.stale
                or greeks.status != OPTION_GREEKS_VALID
                or not greeks.HasField("forward")
            ):
                continue
            if best is None or greeks.grid_time > best[1].grid_time:
                best = (contract, greeks)
        return best

    def pass_over(self, contract: str, why: str) -> None:
        if (contract, why) not in self.passed_over:
            self.passed_over.add((contract, why))
            print(f"passing over {contract}: {why}")

    def order_price(self, contract: str) -> tuple[int, int] | str:
        """The price and size of a buy that can rest on `contract` now, or why there is
        none: WAIT if its book may still come or change, else a reason to pass it over."""
        # The latest table: the exchange sends it again, whole, when a listing changes.
        info = instrument_info(self.session.instrument_table or self.table, contract)
        if info is None or not can_trade(info):
            return "your team cannot trade it now"
        if info.tick_size <= 0 or info.lot_size <= 0:
            return "the instruments table gives no usable tick or lot size"
        book = self.books.get(contract)
        assert self.market is not None
        if book is None or contract in self.books.stale or book.grid_time < self.market.open_time:
            return WAIT  # no book of this session yet, or one that may be out of date
        state = trading_state(book)
        if state != OPTION_TRADING:
            # A suspended or reducing-only contract takes no new position. Halts are being
            # named in issue #216: once they are, the halt's reason can be printed here.
            return f"its trading state is {OptionTradingState.Name(state)}"
        if book.condition != LIVE or not book.bid_levels or not book.ask_levels:
            return "its quote is not two-sided and LIVE"
        tick = info.tick_size
        price = (book.bid_levels[0].price // tick + 1) * tick
        best_ask = min(
            levels[0].price for levels in (book.ask_levels, book.student_ask_levels) if levels
        )
        if best_ask - price < MIN_ROOM_TICKS * tick:
            return f"fewer than {MIN_ROOM_TICKS} ticks between the wall's bid and the asks"
        return price, info.lot_size

    def choose(self) -> tuple[str, int, int, OptionGreeks] | None:
        """The contract to order, its price and size, and the Greeks its forward came
        from; None while there is none."""
        if self.chain is None:
            self.why_none = "no option chain was received"
            return None
        if not self.market_open():
            self.why_none = "the market is not open"
            return None
        entries = [
            entry
            for entry in self.chain.expiries
            if entry.underlying == self.underlying and not entry.reducing_only
        ]
        if not entries:
            self.why_none = f"the chain lists no expiry of {self.underlying} open to new positions"
            return None
        nearest = min(expiry_date(entry) for entry in entries)
        contracts = [
            contract.instrument
            for contract in chain_contracts(self.chain, underlying=self.underlying, expiry=nearest)
            if contract.role in (OPTION_ROLE_ACTIVE, OPTION_ROLE_OBLIGATED)
        ]
        found = self.forward(contracts)
        if found is None:
            self.why_none = "no valid Greeks with a forward for the nearest expiry"
            return None  # no valid Greeks yet for this expiry
        forward = found[1].forward
        # Nearest strike first; the chain's order breaks a tie.
        contracts.sort(key=lambda c: abs(parse_option_symbol(c).strike - forward))
        for contract in contracts:
            outcome = self.order_price(contract)
            if outcome == WAIT:
                self.why_none = f"no current book of {contract} yet"
                return None
            if isinstance(outcome, str):
                self.pass_over(contract, outcome)
                continue
            price, size = outcome
            return contract, price, size, found[1]
        self.why_none = "every contract in the nearest expiry was passed over"
        return None

    # Sending

    async def act(self) -> None:
        if self.finished or not self.up:
            return  # nothing while the connection is down or resuming
        try:
            await self.act_now()
        except (NotConnected, ConnectionClosed) as error:
            # The connection dropped before the example saw it. The message stays recorded
            # as in flight, and is never sent again: the resume says what became of it.
            print(f"not sent, the connection is down: {error}")

    async def act_now(self) -> None:
        if self.ref is None:
            if self.missed and self.would_send is None:
                self.why_none = "a message from the exchange was missed before a choice"
            if not self.stopping and not self.missed and self.would_send is None:
                await self.send_order()
            return
        if not self.accepted:
            # A cancel names a level, not an order: until the exchange says it took this
            # order, whatever rests at the level may be another order of the team (one
            # still in its order delay when this one was sent, say, which had this one
            # refused as a duplicate). Never cancel there without this order's own
            # `accepted`. If the last resume lost the reply, none can come: at the end,
            # stop and warn; until then, wait for it.
            if self.stopping and self.reply_lost:
                self.gave_up = True
            return
        if (self.resting or self.stopping) and self.cancel_ref is None:
            if self.retry_at is not None and self.grid_points < self.retry_at:
                return
            if time.monotonic() >= self.retry_after:
                await self.cancel()

    async def send_order(self) -> None:
        choice = self.choose()
        if choice is None:
            return
        contract, price, size, greeks = choice
        symbol = parse_option_symbol(contract)
        print(
            f"chose {contract}: expiry {symbol.expiry}, strike {price_text(symbol.strike)}, "
            f"nearest the forward {price_text(greeks.forward)} from the Greeks of "
            f"{greeks.instrument}"
        )
        own = self.greeks.get(contract)
        if own is not None and own.status == OPTION_GREEKS_VALID and own.HasField("delta"):
            # Fixed-point integers on the wire: read them exactly, never as float.
            delta = greek_to_decimal(own.delta)
            vol = vol_to_decimal(own.implied_vol) if own.HasField("implied_vol") else "none"
            print(f"its Greeks: delta {delta}, implied vol {vol} points")
        self.contract, self.price = contract, price
        if self.watch_only:
            # Nothing is sent: `ref` stays None, so no cancel can follow either.
            self.would_send = f"LIMIT BUY {size} contract(s) of {contract} @ {price_text(price)}"
            print(f"WOULD SEND (watch-only, not sent): {self.would_send}")
            return
        print(f"sending a LIMIT buy of {size} contract(s): {self.where}")
        self.ref = new_request_ref()
        self.sent_at = time.monotonic()
        try:
            await send_new(
                self.session,
                strat_id=self.strat_id,
                instrument=contract,  # the contract's OCC symbol
                side=BUY,
                order_type=LIMIT,
                size=size,  # contracts, never shares
                price=price,
                request_ref=self.ref,
            )
        except NotConnected:
            # Nothing was sent: no order of this example can rest. Choose again once the
            # connection is back. (A ConnectionClosed may have sent it, so it stays sent.)
            self.ref = None
            print("not sent, the connection is down: choosing again once it is back")

    async def cancel(self) -> None:
        assert self.contract is not None
        self.cancel_ref = new_request_ref()  # recorded first, so its reply is matched
        self.cancel_price = self.price
        self.budget_reject = None
        print(f"cancelling {self.where}")
        await send_cancel(
            self.session,
            instrument=self.contract,
            side=BUY,
            price=self.price,
            request_ref=self.cancel_ref,
        )

    # The connection

    async def on_connected(self, connected: Connected) -> None:
        self.up = False  # until its resume is complete
        self.resuming = connected.resume is not None
        self.resume_answered = False
        if connected.reconnected:
            print("reconnected: sending nothing until the resume is complete")
        if connected.resume is None:
            self.resumed(replayed=False)  # the exchange did not resume this session
        if self.subscribe_to:
            await self.subscribe()

    async def subscribe(self) -> None:
        """Subscribe to the contracts. The session subscribes to them again on every
        reconnect; if it is down now, this is done once the next session is up."""
        try:
            await subscribe(self.session, self.subscribe_to)
            self.subscribe_to = []
        except NotConnected:
            print("not subscribed yet, the connection is down: subscribing once it is back")
        except ConnectionClosed:
            # The session recorded the contracts before the send failed, so the next
            # session subscribes to them by itself: subscribing again would do it twice.
            self.subscribe_to = []
            print("not subscribed yet, the connection is down: the next session subscribes")

    def on_disconnected(self, disconnected: Disconnected) -> None:
        self.up = False
        # Wait for a session state, an option chain, Greeks and a book received after the
        # reconnect: the chain held may be of another day or event log.
        self.market = None
        self.chain = None
        self.books.update(disconnected)
        self.greeks.update(disconnected)
        why = disconnected.error or "the exchange closed it"
        print(f"the connection dropped ({why}); sending nothing until it is back")
        if self.ref is not None and not self.finished:
            print(f"the order sent may still rest at {self.where}")
            # Its accepted, if it comes, comes through the resume: no round trip.
            self.dropped_since_send = True

    def resumed(self, replayed: bool) -> None:
        """A session's resume is complete, or the exchange did not resume it: take this
        example's order from the view of resting orders, and send again."""
        self.up = True
        self.ever_up = True
        self.resuming = False
        if self.view is None:
            return
        if self.view.incomplete:
            if self.ref is None:
                return  # no order was sent, so none of its reports can have been missed
            # The reports missed while down are not recovered: treat them as missed, so
            # no order is sent from now on. A reply not yet come is never sent again. A
            # cancel in flight at the drop may never have reached the exchange, and its
            # answer is never sent again: forget it, so the order is cancelled afresh.
            print("warning: the order reports missed while disconnected were not recovered")
            self.missed = True
            self.reply_lost = True
            self.forget_cancel()
            return
        # This resume recovered the reports: a reply not yet come may still come, even if
        # an earlier resume failed.
        self.reply_lost = False
        if self.ref is None or self.finished:
            return  # no order was sent, so there is nothing to take from the view
        # An order or a cancel in flight at the drop is never sent again: the view, from
        # the replay or the snapshot, says what became of it.
        self.forget_cancel()
        if not replayed:
            self.unreported_fills = True
        assert self.contract is not None
        seen = self.strategy_orders()
        if not self.accepted:
            # Never infer which order is this one: with no `accepted`, any order of this
            # strategy on the contract ends the run (see `owner_unknown_if_seen`). With
            # none, the order may still be in its order delay, since the resume covers
            # only what the exchange had applied: wait for its reply as before.
            if self.owner_unknown_if_seen(seen):
                return
            print(
                f"nothing of this strategy rests on {self.contract} (from the resume), but "
                "no accepted came for the order: it may still be in its order delay, so "
                "waiting for its reply"
            )
            return
        order = self.view.get(self.contract, BUY, self.price)
        if order is not None and order.strat_id == self.strat_id:
            # Accepted, and this strategy's order rests at its level: this one.
            if not self.resting:
                print(f"resting: {order.remaining_size} at {self.where} (from the resume)")
            self.resting = True
        elif seen:
            # Not at its level, but orders of this strategy rest on the contract: perhaps
            # this one, moved by an amend while disconnected. Cancel none of them.
            listed = "; ".join(seen)
            print(
                f"nothing rests at {self.where}, but this strategy has orders on the "
                f"contract: {listed}. One may be this one, moved: cancelling nothing"
            )
            self.warnings.append(
                f"the order sent to {self.where} was not there after the resume, but this "
                f"strategy has orders on the contract ({listed}), one perhaps this one, "
                "moved: check with order_snapshot or the console, and cancel yourself any "
                "that is this one"
            )
            self.missed = True
            self.unsure = True
            self.gone = True
        else:
            print(f"nothing of the order rests at {self.where} (from the resume)")
            self.gone = True

    def forget_cancel(self) -> None:
        """Forget the cancel in flight, so the order is cancelled afresh, but keep its
        request_ref: a reply to it may still come."""
        if self.cancel_ref is not None:
            self.earlier_cancel_refs.add(self.cancel_ref)
        self.cancel_ref = None

    def own_cancel(self, ref: str | None) -> bool:
        """Whether `ref` is a cancel this example sent, the one in flight or an earlier."""
        return ref is not None and (ref == self.cancel_ref or ref in self.earlier_cancel_refs)

    def level_text(self, side: int, price: int) -> str:
        return f"{self.contract} {'BUY' if side == BUY else 'SELL'} @ {price_text(price)}"

    def strategy_orders(self) -> list[str]:
        """The orders of this strategy that the view shows on the contract, at any price."""
        if self.view is None or self.contract is None:
            return []
        return sorted(
            f"{self.level_text(order.key.side, order.key.price)} ({order.remaining_size} resting)"
            for order in self.view
            if order.strat_id == self.strat_id and order.key.instrument == self.contract
        )

    def owner_unknown_if_seen(self, seen: list[str]) -> bool:
        """The one check, on a resume and on every live order report alike: an order of
        this strategy on the contract, at any price, seen after a drop and before this
        order's own `accepted`, may or may not be this one. This example never infers
        which: it cancels nothing, warns naming each order seen, and the run ends with
        status 1. Returns whether it did so."""
        if self.ref is None or self.accepted or not self.dropped_since_send or self.finished:
            return False
        if not seen:
            return False
        listed = "; ".join(seen)
        print(
            f"before an accepted came for the order sent to {self.where}, this strategy's "
            f"orders were seen on the contract after the drop: {listed}. Whose they are is "
            "unknown, so cancelling nothing"
        )
        self.warnings.append(
            f"no accepted came for the order sent to {self.where}, and after the "
            f"connection dropped this strategy's orders were seen on the contract: "
            f"{listed}. Any of them may be this example's order, and it takes none as its "
            "own without its accepted, so nothing was cancelled: check with order_snapshot "
            "or the console, and cancel yourself any that is this one"
        )
        self.missed = True
        self.unsure = True
        self.gone = True
        return True

    # Receiving

    async def on_event(self, event: object, act: bool = True) -> None:
        """Apply one event, then act unless `act` is False: a caller with more events
        already queued applies them all first, so no order goes out on what an event still
        queued (a drop, say) would have changed."""
        if self.view is not None:
            self.view.apply(event)  # in the order the bot handles events
        if isinstance(event, Received) and isinstance(event.message, OrderSnapshot):
            # Each frame as it comes, so a snapshot cut short or damaged still counts.
            frame = event.message
            if frame.strat_id == self.strat_id and frame.instrument == self.contract:
                level = self.level_text(frame.side, frame.price)
                self.owner_unknown_if_seen([f"{level} ({frame.remaining_size} resting)"])
        if isinstance(event, Retrying):
            print(f"reconnecting in {event.delay:.1f} s (attempt {event.attempt})")
            return
        if isinstance(event, Connected):
            await self.on_connected(event)
        elif isinstance(event, ResumeComplete):
            self.resumed(event.replayed)
        elif isinstance(event, Received) and event.type == "resume_ack":
            self.resume_answered = True
        item = as_market_data(event)
        if isinstance(item, Disconnected):
            self.on_disconnected(item)
        elif isinstance(item, OptionChain):
            self.chain = item
        elif isinstance(item, OptionGreeks):
            self.greeks.update(item)
        elif isinstance(item, Book):
            self.books.update(item)
        elif isinstance(item, SessionState):
            self.market = item
            self.grid_points += 1  # the session state comes on every grid point
        elif isinstance(item, Reject):
            detail = f" ({item.instrument})" if item.HasField("instrument") else ""
            print(f"subscription refused: {reason_code_name(item.reason_code)}{detail}")
        elif isinstance(item, SeqGap | DecodeFailed) or isinstance(event, ReportGap):
            print("warning: a message from the exchange was missed or unreadable")
            self.missed = True  # no order is sent after this; one sent is still cancelled
            if item is not None:
                self.books.update(item)
                self.greeks.update(item)
            self.on_missed(
                isinstance(item, SeqGap) or (isinstance(item, DecodeFailed) and item.type is None)
            )
        elif is_order_event(event):
            self.on_order_event(event.message)  # type: ignore[attr-defined]
        if act:
            await self.act()

    def on_missed(self, ends_resume: bool) -> None:
        """A `SeqGap`, or a `DecodeFailed` whose type is unknown, ends a resume with no
        `ResumeComplete` (the session stops waiting for it): take it as a resume that did
        not recover the reports, so the session is up and a cancel can still be sent."""
        if not self.resuming or not ends_resume:
            return
        if self.resume_answered:
            print("the resume was cut short by a message missed or unreadable")
        else:
            # Delivered before the resume_ack, from what the session read ahead: the
            # session's resume goes on, but the example errs safe and ends it here.
            print(
                "a message was missed or unreadable before the resume was answered: "
                "taking the resume as cut short"
            )
        if self.view is not None:
            self.view.mark_incomplete()
        self.resumed(replayed=False)

    def ours(self, message: OrderState | OrderCancelled | Execution) -> bool:
        """Whether a report is about this example's order: same strategy, contract, side
        and price level, once the order is accepted. Before that, a report at the level
        can only be about an earlier order. An amend's `order_state` names the level the
        order left in `old_price`."""
        if isinstance(message, Execution):
            price = message.order_price
        elif isinstance(message, OrderState) and message.HasField("old_price"):
            price = message.old_price
        else:
            price = message.price
        return (
            self.accepted
            and message.strat_id == self.strat_id
            and message.instrument == self.contract
            and message.side == BUY
            and price == self.price
        )

    def on_order_event(self, message: Message) -> None:
        # A reject the exchange could not tie to a message carries no request_ref.
        ref = request_ref_of(message)
        if (
            isinstance(message, OrderState | Execution | OrderCancelled)
            and message.strat_id == self.strat_id
            and message.instrument == self.contract
        ):
            price = message.order_price if isinstance(message, Execution) else message.price
            report = type(message).__name__
            seen = [f"{self.level_text(message.side, price)} (an {report} report)"]
            if self.owner_unknown_if_seen(seen):
                return
        match message:
            case Accepted() if ref is not None and ref == self.ref:
                if self.dropped_since_send:
                    print("accepted (answered after a reconnect)")
                else:
                    elapsed_ms = (time.monotonic() - self.sent_at) * 1000
                    print(f"accepted after a {elapsed_ms:.0f} ms round trip")
                self.accepted = True
            case Accepted() if self.own_cancel(ref):
                print("cancel accepted")
            case Reject() if ref is not None and ref == self.ref:
                # A reject stops the example: it never retries. Halts are being named in
                # issue #216; until then CONTRACT_SUSPENDED is what a halted contract gives.
                detail = f" ({message.reason_detail})" if message.HasField("reason_detail") else ""
                print(f"REJECTED: {reason_code_name(message.reason_code)}{detail}")
                self.rejected = True
                self.duplicate = message.reason_code == ReasonCodes.DUPLICATE_ORDER_AT_LEVEL
            case Reject() if ref is not None and ref == self.cancel_ref:
                self.on_cancel_reject(message)
            case Reject() if self.own_cancel(ref):
                pass  # an earlier cancel's: the order was cancelled afresh since
            case OrderState() if self.ours(message):
                if message.price != self.price:
                    # Moved by an amend from elsewhere: follow it, so the cancel names
                    # the level where it rests now.
                    print(f"moved by an amend to {price_text(message.price)}")
                    self.price = message.price
                if message.state in (RESTING, STALE):
                    if not self.resting:
                        print(f"resting: {message.remaining_size} at {self.where}")
                    self.resting = True
                else:
                    self.gone = True  # ended by something else, such as an amend
            case Execution() if self.ours(message):
                print(
                    f"FILL {message.fill_size} @ {price_text(message.fill_price)}, "
                    f"{message.remaining_size} left"
                )
                self.filled += message.fill_size
                if message.remaining_size == 0:
                    self.gone = True
            case OrderCancelled() if self.ours(message) or (
                # This example's own cancel, at the level where its order rests, and of
                # an order of its strategy: a cancel clears the team's order at the
                # level, whichever strategy's it is.
                self.own_cancel(ref)
                and message.price == self.price
                and message.strat_id == self.strat_id
            ):
                reason = reason_code_name(message.reason_code)
                rested = "cancelled" if self.resting else "not left resting"
                print(f"{rested}: {message.cancelled_size} at {self.where} ({reason})")
                self.gone = True
            case OrderCancelled() if self.own_cancel(ref) and message.price == self.price:
                # The cancel, this one or one sent before a drop, took another strategy's
                # order at this example's level. The team has one order per level, so
                # this one does not rest there, and where it rests is not known: send no
                # further cancel there, and stop.
                print(
                    f"the cancel took another strategy's order at {self.where}, not this "
                    "one: cancelling nothing more"
                )
                self.warnings.append(
                    f"a cancel at {self.where} took an order of your team there that was "
                    "not this example's, so this example's order is not there and may "
                    "still rest elsewhere: check with order_snapshot or the console, and "
                    "cancel it yourself"
                )
                if ref == self.cancel_ref:
                    self.cancel_ref = None
                self.unsure = True
                self.gone = True
            case OrderCancelled() if self.own_cancel(ref):
                # The cancel, this one or one sent before a drop, took an order at a level
                # this example's order has left. Cancel again where the order rests now;
                # a NO_ORDER_AT_LEVEL then says none does.
                print(f"the cancel took an order at {price_text(message.price)}, not this one")
                self.warnings.append(
                    f"a cancel took an order of your team at {price_text(message.price)} "
                    "that was not this example's: check your orders"
                )
                if ref == self.cancel_ref:
                    self.cancel_ref = None

    def on_cancel_reject(self, message: Reject) -> None:
        reason = reason_code_name(message.reason_code)
        self.cancel_ref = None
        if message.reason_code == ReasonCodes.NO_ORDER_AT_LEVEL:
            if self.cancel_price != self.price:
                return  # the order moved after this cancel was sent: cancel it again
            print(f"nothing rests at {self.where} ({reason})")
            self.gone = True
        elif message.reason_code == ReasonCodes.MIN_REST_VIOLATION:
            # The exchange decides how long an order must rest, so its own clock paces the
            # retry: after the next grid point, then after two more, four more and so on.
            print(
                f"cancel rejected ({reason}); sending it again after {self.backoff} grid point(s)"
            )
            self.retry_at = self.grid_points + self.backoff
            self.backoff *= 2
        elif message.reason_code in BUDGET_REJECTS:
            print(f"cancel rejected ({reason}); sending it again in {BUDGET_WAIT_SECONDS:g} s")
            self.retry_after = time.monotonic() + BUDGET_WAIT_SECONDS
            self.budget_reject = reason
        else:
            self.cancel_failed = reason

    def summary(self) -> str:
        if self.ref is None:
            return "no order was sent"
        if self.rejected:
            return "the order was rejected, so nothing rests"
        if self.unsure:
            return f"the order sent to {self.where} may still rest: see the warnings above"
        text = f"nothing of the order rests; {self.filled} contract(s) filled"
        if self.unreported_fills and not self.missed:
            text += (
                " (resumed from a snapshot, so fills while disconnected are not reported: "
                "check your fills)"
            )
        if self.missed:
            text += " (messages were missed, so this may be incomplete: check your fills)"
        return text


# The longest wait for the connection to close at the end. A local choice, not a value
# the exchange sets. If the close does not finish, the connection is dropped anyway when
# the example exits.
CLOSE_SECONDS = 5.0


@contextlib.asynccontextmanager
async def closing(session: ReconnectingSession) -> AsyncIterator[ReconnectingSession]:
    """Like `async with session:`, but waits at most CLOSE_SECONDS for the close, so a
    connection that has stopped taking data cannot keep the example from exiting."""
    try:
        yield session
    finally:
        try:
            async with asyncio.timeout(CLOSE_SECONDS):
                await session.close()
        except TimeoutError:
            print("the connection did not close in time; it is dropped as the example exits")
        except asyncio.CancelledError:
            # Ctrl+C while closing: what the run printed and its exit status stand.
            print("interrupted while closing; the connection is dropped as the example exits")


async def pump(session: ReconnectingSession, queue: asyncio.Queue) -> None:
    """Read every event into `queue`, then None when the session is closed, or the exception
    if it fails for good. Only this task reads the session, so a timeout that ends one wait
    for events never ends the session's stream."""
    try:
        async for event in session:
            await queue.put(event)
    except Exception as error:
        await queue.put(error)
    else:
        await queue.put(None)


async def next_item(queue: asyncio.Queue) -> object:
    """The next event from `pump`. Raises the exception that ended the session, if any."""
    item = await queue.get()
    if isinstance(item, Exception):
        raise item
    return item


async def drive(bot: OptionsBot, queue: asyncio.Queue, seconds: float) -> str:
    """Apply events until the bot is finished ("done"), `seconds` pass ("time") or the
    session is closed ("closed")."""
    try:
        async with asyncio.timeout(seconds):
            if queue.empty():
                await bot.act()
            if bot.finished:
                return "done"
            while (event := await next_item(queue)) is not None:
                # Act only once every event already read is applied.
                await bot.on_event(event, act=queue.empty())
                if bot.finished:
                    return "done"
    except TimeoutError:
        return "time"
    return "closed"


async def wait_for_table(
    session: ReconnectingSession, queue: asyncio.Queue, early: list
) -> Instruments | None:
    """Wait for the first session, then at most TABLE_SECONDS for the instruments table.
    The events read meanwhile are kept in `early`, for the bot to apply in order."""
    loop = asyncio.get_running_loop()
    connected = False
    try:
        async with asyncio.timeout(None) as within:
            while True:
                event = await next_item(queue)
                if event is None:
                    return None  # the session was closed
                if isinstance(event, Retrying) and not connected and event.error is not None:
                    raise event.error  # the first connection failed: stop
                early.append(event)
                if isinstance(event, Connected) and not connected:
                    connected = True
                    info = event.info
                    print(f"connected: team {info.team}, unscored session: {info.unscored}")
                    within.reschedule(loop.time() + TABLE_SECONDS)
                if connected and session.instrument_table is not None:
                    return session.instrument_table
    except TimeoutError:
        if not within.expired():
            raise
        return None


@dataclasses.dataclass
class Progress:
    """How far a run got, so that a run stopped at once by Ctrl+C can say whether an
    order may still rest."""

    bot: OptionsBot | None = None
    watch_only: bool = False
    status: int | None = None  # the exit status, once the run has printed its outcome


def stopped_at_once(progress: Progress) -> int:
    if progress.status is not None:
        # Ctrl+C while the connection closed: the outcome printed before it stands.
        print("interrupted while closing; the connection is dropped as the example exits")
        return progress.status
    bot = progress.bot
    if bot is not None and bot.watch_only:
        return watch_only_outcome(bot)
    if progress.watch_only:
        print("watch-only: NO CONTRACT CHOSEN: interrupted before the chain was read")
        return 1
    if bot is None or bot.ref is None:
        print("interrupted: no order was sent")
        return 0
    return fail(
        f"interrupted: this example's order may still rest at {bot.where}; check and cancel it"
    )


def watch_only_outcome(bot: OptionsBot) -> int:
    """The last line and exit status of a --watch-only run: 0 once a contract was chosen,
    else 1 with the reason none was."""
    if bot.would_send is not None:
        print(f"watch-only: chose {bot.contract}; would send {bot.would_send}; nothing was sent")
        return 0
    print(f"watch-only: NO CONTRACT CHOSEN: {bot.why_none}; nothing was sent")
    return 1


async def run(url: str, args: argparse.Namespace, progress: Progress) -> int:
    # The token comes from QTE_TOKEN, or from the file named by QTE_TOKEN_FILE. It is read
    # once, here, and kept to authenticate again on each reconnect. Every new session
    # subscribes to the contracts again and resumes the team's order reports. The bot
    # applies them to its own view of resting orders as it handles each event, not as the
    # reader queues it, so the view never runs ahead of what the bot has handled.
    # `close_timeout` bounds the close of each connection, since a ReconnectingSession
    # finishes closing even when the wait for it is cut short.
    session = ReconnectingSession(url, close_timeout=CLOSE_SECONDS)
    # Each Ctrl+C from here on cancels this task. The first is absorbed, so the run still
    # cancels its order; a second is let through and stops the run. The event loop handles
    # Ctrl+C itself where it can (not on Windows).
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGINT, task.cancel)
    try:
        return await trade_and_clean_up(session, args, task, progress)
    finally:
        with contextlib.suppress(NotImplementedError):
            loop.remove_signal_handler(signal.SIGINT)


async def trade_and_clean_up(
    session: ReconnectingSession, args: argparse.Namespace, task: asyncio.Task, progress: Progress
) -> int:
    async with closing(session):
        progress.status = await trade(session, args, task, progress)
    return progress.status


async def trade(
    session: ReconnectingSession, args: argparse.Namespace, task: asyncio.Task, progress: Progress
) -> int:
    queue: asyncio.Queue = asyncio.Queue()
    reader = asyncio.create_task(pump(session, queue))
    try:
        return await trade_from(session, queue, args, task, progress)
    finally:
        reader.cancel()


async def trade_from(
    session: ReconnectingSession,
    queue: asyncio.Queue,
    args: argparse.Namespace,
    task: asyncio.Task,
    progress: Progress,
) -> int:
    early: list = []
    table = await wait_for_table(session, queue, early)
    if table is None:
        if args.watch_only:
            print("watch-only: NO CONTRACT CHOSEN: no instruments table from this exchange")
            return 1
        print("no instruments table from this exchange, so no listed option contract to use")
        return 1
    underlyings = option_underlyings(table)
    underlying = args.underlying or (underlyings[0] if underlyings else None)
    if underlying is None:
        if args.watch_only:
            print(
                "watch-only: NO CONTRACT CHOSEN: the instruments table names no option underlying"
            )
            return 1
        print("the instruments table names no option underlying")
        return 1
    contracts = listed_contracts(table, underlying)
    if not contracts:
        if args.watch_only:
            print(f"watch-only: NO CONTRACT CHOSEN: no option contract is listed on {underlying}")
            return 1
        print(f"no option contract is listed on {underlying}; no order was sent")
        return 0
    print(f"{len(contracts)} contract(s) listed on {underlying}; subscribing to them")
    # Incomplete until the first resume's snapshot: if the exchange does not resume the
    # first session, orders of the team may rest that the view never sees.
    view = RestingOrders()
    view.mark_incomplete()
    bot = OptionsBot(session, table, underlying, args.strat_id, args.watch_only, view)
    progress.bot = bot
    for event in early:
        await bot.on_event(event)  # the first Connected among them; nothing is sent yet
    bot.subscribe_to = contracts
    # --seconds bounds the first subscribe too: a connection that has stopped taking
    # data must not keep the run from ending.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + args.seconds
    try:
        async with asyncio.timeout_at(deadline) as within:
            await bot.subscribe()
    except TimeoutError:
        if not within.expired():
            raise
        stalled = (
            "the subscribe was not sent within --seconds: the connection may have stopped "
            "taking data"
        )
        if bot.watch_only:
            bot.why_none = stalled
            return watch_only_outcome(bot)
        print(f"{stalled}; no order was sent")
        return 1
    interrupted = False

    def absorb_first_interrupt() -> bool:
        nonlocal interrupted
        if interrupted:
            return False
        interrupted = True
        # This cancellation is handled. If another is still pending, Ctrl+C was
        # pressed twice before the first reached this point: stop at once.
        return task.uncancel() == 0

    try:
        why = await drive(bot, queue, max(0.0, deadline - loop.time()))
    except asyncio.CancelledError:
        if not absorb_first_interrupt():
            raise
        why = "interrupted"
    if why == "closed" and bot.watch_only:
        print("the exchange closed the connection")
        return watch_only_outcome(bot)
    if why == "closed":
        print("the exchange closed the connection")
        if bot.ref is not None and not bot.finished:
            print(f"WARNING: the order may still rest at {bot.where}; check and cancel it")
        return 1
    if not bot.finished and bot.ref is not None:
        reason = {"time": "time is up", "interrupted": "interrupted"}[why]
        if not bot.accepted:
            print(f"{reason}: waiting for the reply to the order sent to {bot.where}")
        else:
            print(f"{reason}: cancelling {bot.where}")
        bot.stopping = True
        # One deadline for the drain: a Ctrl+C during it does not start it again.
        drain_deadline = loop.time() + args.drain_seconds
        while True:
            try:
                why = await drive(bot, queue, max(0.0, drain_deadline - loop.time()))
                break
            except asyncio.CancelledError:
                if not absorb_first_interrupt():
                    raise
                print("interrupted: still cancelling; press Ctrl+C again to stop at once")
    if bot.watch_only:
        if not bot.ever_up and bot.would_send is None:
            bot.why_none = "the session never came up (its resume never completed)"
        return watch_only_outcome(bot)
    for warning in bot.warnings:
        print(f"WARNING: {warning}")
    if bot.cancel_failed is not None:
        print(
            f"WARNING: the cancel was rejected ({bot.cancel_failed}), so the order may "
            f"still rest at {bot.where}; check and cancel it yourself"
        )
        return 1
    if bot.duplicate:
        print(
            f"WARNING: your team already has an order resting at {bot.where}, perhaps "
            "one an earlier run left, so this one was rejected. That order still rests: "
            "cancel it yourself with a cancel at that contract, side and price"
        )
        return 1
    if bot.ref is not None and not bot.accepted and not bot.rejected and not bot.gone:
        print(
            f"WARNING: no accepted came for the order sent to {bot.where}, so nothing was "
            "cancelled there: a cancel names the level, and could have removed another "
            "order of your team. This example's order may still rest there: check with "
            "order_snapshot or the console, and cancel it yourself if it is this one"
        )
        return 1
    if bot.ref is not None and bot.missed and not bot.rejected and not bot.unsure:
        print(
            "WARNING: messages from the exchange were missed, so the order may have "
            f"moved, filled or still rest (last seen at {bot.where}); check your orders "
            "and fills"
        )
        return 1
    if bot.ref is not None and not bot.finished and bot.budget_reject is not None:
        print(
            f"WARNING: the cancel was rejected ({bot.budget_reject}) and --drain-seconds "
            f"ended before it could be sent again {BUDGET_WAIT_SECONDS:g} s later, so the "
            f"order may still rest at {bot.where}. Wait a minute, then cancel it "
            "yourself. Next time, a --drain-seconds above 60 lets the run wait out such a "
            "reject itself before it exits"
        )
        return 1
    if bot.ref is not None and not bot.finished:
        print(
            f"WARNING: the cancel was not confirmed, so the order may still rest at "
            f"{bot.where}; check and cancel it yourself"
        )
        return 1
    if bot.ref is None and not bot.ever_up:
        print(
            "the session never came up (its resume never completed), so no order was sent; "
            "check the exchange and run it again"
        )
        return 1
    if why == "time" and bot.ref is None and bot.missed:
        print(f"stopped after {args.seconds:g} seconds: no order was sent: {bot.why_none}")
    elif why == "time" and bot.ref is None:
        print(f"stopped after {args.seconds:g} seconds: no contract could take the order")
    print(bot.summary())
    return 1 if bot.warnings else 0


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def ended_for_good(progress: Progress, why: str) -> int:
    """The run's end when the connection fails for good: on the first connection, or on
    a reconnect that is not retried (a refused session, a revoked credential). An order
    sent may still rest, so it is named with how to check it; a watch-only run ends on
    its watch-only line."""
    bot = progress.bot
    if progress.watch_only:
        print(why)
        if bot is not None:
            return watch_only_outcome(bot)
        print("watch-only: NO CONTRACT CHOSEN: the connection ended before the chain was read")
        return 1
    if bot is None or bot.ref is None or bot.rejected or (bot.gone and not bot.missed):
        return fail(why)
    for warning in bot.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    return fail(
        f"{why}. WARNING: this example's order may still rest at {bot.where}: check with "
        "order_snapshot or the console, and cancel it yourself if it is this one, before "
        "running this again"
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # With QTE_LOG_FORMAT=json, the SDK's log records are JSON lines on stderr.
    qte_sdk.logs.configure()
    try:
        url = resolve_url()  # QTE_URL from the environment, or from ./.env
    except MissingURL as error:
        fail(f"{error}; for example ws://127.0.0.1:8080/ws (see docs/quickstart.md)")
        return 2
    progress = Progress(watch_only=args.watch_only)
    try:
        return asyncio.run(run(url, args, progress))
    except MissingToken as error:
        # The message names the variable and the problem, never the token.
        fail(f"{error} (see docs/quickstart.md)")
        return 2
    except ValueError as error:
        return fail(f"not sent: {error}")
    except SessionRejected as error:
        return ended_for_good(progress, f"the exchange refused the session: {error}")
    except (OSError, InvalidHandshake, SessionNotAcknowledged) as error:
        return ended_for_good(progress, f"could not connect to QTE_URL: {error}")
    except ConnectionClosed as error:
        if progress.bot is None:
            if progress.watch_only:
                print("the connection dropped before the session was up")
                print(
                    "watch-only: NO CONTRACT CHOSEN: the connection dropped before the chain "
                    "was read"
                )
                return 1
            return fail("the connection dropped before the session was up: run it again")
        close = describe_close(error) if isinstance(error, ConnectionClosedError) else "a close"
        return ended_for_good(progress, f"the connection ended for good with {close}")
    except (KeyboardInterrupt, asyncio.CancelledError):
        # A Ctrl+C before the run could take it over, or a second one while it cancels.
        return stopped_at_once(progress)


if __name__ == "__main__":
    sys.exit(main())
