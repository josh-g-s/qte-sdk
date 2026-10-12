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
cancels that price level, waits for the cancel to be confirmed and stops. It never
retries an order: a reject is printed with its reason's name and the example stops.

It never means to leave the order resting. When --seconds are up, or on Ctrl+C, it stops
choosing and cancels the order's level, waiting at most --drain-seconds for the exchange
to confirm. If it cannot confirm, it names the level where the order may still rest, so
you can cancel it yourself, and exits with status 1. Press Ctrl+C twice to stop at once.

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
exchange, so nothing here assumes their values: a cancel rejected MIN_REST_VIOLATION, or
for a message budget, is sent again after the exchange's next market-data grid point, then
after two more, four more and so on.
"""

import argparse
import asyncio
import contextlib
import os
import signal
import sys
import time
from collections.abc import AsyncIterator

from google.protobuf.message import Message
from websockets.exceptions import ConnectionClosed, InvalidHandshake

import qte_sdk.logs
from qte_sdk.books import LatestBooks
from qte_sdk.connection import SessionRejected
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
from qte_sdk.contract.v1.session_pb2 import Instruments
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
from qte_sdk.session import (
    MissingToken,
    MissingURL,
    Session,
    SessionNotAcknowledged,
    open_session,
    resolve_url,
)
from qte_sdk.units import to_decimal

# The least room, in ticks, between the order's price and the best ask. There is no
# post-only order, so an ask that falls to the price during the order delay fills it; the
# room makes that less likely, never impossible. A local choice.
MIN_ROOM_TICKS = 3

# The longest wait for the instruments table after the session opens. A local choice.
TABLE_SECONDS = 10.0

# Rejects of a cancel that may pass if sent again later. Each leaves the order unchanged.
RETRY_CANCEL = frozenset(
    {
        ReasonCodes.MIN_REST_VIOLATION,
        ReasonCodes.MESSAGE_BUDGET_EXCEEDED,
        ReasonCodes.BURST_CAP_EXCEEDED,
    }
)

# Why a contract cannot be chosen yet, though it may be once more data arrives.
WAIT = "wait"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rest one limit order on an option contract and cancel it."
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
        help="the longest wait, at the end, for the cancel to be confirmed (default 10)",
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
        self, session: Session, table: Instruments, underlying: str, strat_id: str
    ) -> None:
        self.session = session
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
        self.accepted = False  # until then, no report at its level is taken as its own
        self.resting = False  # the exchange has reported it resting
        self.gone = False  # nothing of it rests: cancelled, filled or never left resting
        self.rejected = False
        self.filled = 0
        # The cancel: its request_ref and price while one is in flight, and the grid point
        # to wait for before sending it again after a reject that may pass later.
        self.cancel_ref: str | None = None
        self.cancel_price = 0
        self.grid_points = 0
        self.retry_at: int | None = None
        self.backoff = 1
        self.cancel_failed: str | None = None
        self.stopping = False  # no new order; cancel the one sent, if any
        self.missed = False  # whether any message from the exchange was missed

    @property
    def finished(self) -> bool:
        if self.rejected or self.gone or self.cancel_failed is not None:
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
        if self.chain is None or not self.market_open():
            return None
        entries = [
            entry
            for entry in self.chain.expiries
            if entry.underlying == self.underlying and not entry.reducing_only
        ]
        if not entries:
            return None
        nearest = min(expiry_date(entry) for entry in entries)
        contracts = [
            contract.instrument
            for contract in chain_contracts(self.chain, underlying=self.underlying, expiry=nearest)
            if contract.role in (OPTION_ROLE_ACTIVE, OPTION_ROLE_OBLIGATED)
        ]
        found = self.forward(contracts)
        if found is None:
            return None  # no valid Greeks yet for this expiry
        forward = found[1].forward
        # Nearest strike first; the chain's order breaks a tie.
        contracts.sort(key=lambda c: abs(parse_option_symbol(c).strike - forward))
        for contract in contracts:
            outcome = self.order_price(contract)
            if outcome == WAIT:
                return None
            if isinstance(outcome, str):
                self.pass_over(contract, outcome)
                continue
            price, size = outcome
            return contract, price, size, found[1]
        return None

    # Sending

    async def act(self) -> None:
        if self.finished:
            return
        if self.ref is None:
            if not self.stopping and not self.missed:
                await self.send_order()
            return
        if (self.resting or self.stopping) and self.cancel_ref is None:
            if self.retry_at is None or self.grid_points >= self.retry_at:
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
        print(f"sending a LIMIT buy of {size} contract(s): {self.where}")
        self.ref = new_request_ref()
        self.sent_at = time.monotonic()
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

    async def cancel(self) -> None:
        assert self.contract is not None
        self.cancel_ref = new_request_ref()  # recorded first, so its reply is matched
        self.cancel_price = self.price
        print(f"cancelling {self.where}")
        await send_cancel(
            self.session,
            instrument=self.contract,
            side=BUY,
            price=self.price,
            request_ref=self.cancel_ref,
        )

    # Receiving

    async def on_event(self, event: object) -> None:
        item = as_market_data(event)
        if isinstance(item, OptionChain):
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
        elif isinstance(item, SeqGap | DecodeFailed):
            print("warning: a message from the exchange was missed or unreadable")
            self.missed = True  # no order is sent after this; one sent is still cancelled
            self.books.update(item)
            self.greeks.update(item)
        elif is_order_event(event):
            self.on_order_event(event.message)  # type: ignore[attr-defined]
        await self.act()

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
        match message:
            case Accepted() if ref is not None and ref == self.ref:
                elapsed_ms = (time.monotonic() - self.sent_at) * 1000
                print(f"accepted after a {elapsed_ms:.0f} ms round trip")
                self.accepted = True
            case Accepted() if ref is not None and ref == self.cancel_ref:
                print("cancel accepted")
            case Reject() if ref is not None and ref == self.ref:
                # A reject stops the example: it never retries. Halts are being named in
                # issue #216; until then CONTRACT_SUSPENDED is what a halted contract gives.
                detail = f" ({message.reason_detail})" if message.HasField("reason_detail") else ""
                print(f"REJECTED: {reason_code_name(message.reason_code)}{detail}")
                self.rejected = True
            case Reject() if ref is not None and ref == self.cancel_ref:
                self.on_cancel_reject(message)
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
                ref is not None and ref == self.cancel_ref
            ):
                reason = reason_code_name(message.reason_code)
                rested = "cancelled" if self.resting else "not left resting"
                print(f"{rested}: {message.cancelled_size} at {self.where} ({reason})")
                self.gone = True

    def on_cancel_reject(self, message: Reject) -> None:
        reason = reason_code_name(message.reason_code)
        self.cancel_ref = None
        if message.reason_code == ReasonCodes.NO_ORDER_AT_LEVEL:
            if self.cancel_price != self.price:
                return  # the order moved after this cancel was sent: cancel it again
            print(f"nothing rests at {self.where} ({reason})")
            self.gone = True
        elif message.reason_code in RETRY_CANCEL:
            # The exchange decides how long an order must rest and how fast you may send,
            # so its own clock paces the retry: after the next grid point, then after two
            # more, four more and so on.
            print(
                f"cancel rejected ({reason}); sending it again after {self.backoff} grid point(s)"
            )
            self.retry_at = self.grid_points + self.backoff
            self.backoff *= 2
        else:
            self.cancel_failed = reason

    def summary(self) -> str:
        if self.ref is None:
            return "no order was sent"
        if self.rejected:
            return "the order was rejected, so nothing rests"
        text = f"nothing of the order rests; {self.filled} contract(s) filled"
        if self.missed:
            text += " (messages were missed, so this may be incomplete: check your fills)"
        return text


# The longest wait for the connection to close at the end. A local choice, not a value
# the exchange sets. If the close does not finish, the connection is dropped anyway when
# the example exits.
CLOSE_SECONDS = 5.0


@contextlib.asynccontextmanager
async def closing(session: Session) -> AsyncIterator[Session]:
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


async def drive(bot: OptionsBot, session: Session, seconds: float) -> str:
    """Apply events until the bot is finished ("done"), `seconds` pass ("time") or the
    exchange closes the connection ("closed")."""
    try:
        async with asyncio.timeout(seconds):
            await bot.act()
            if bot.finished:
                return "done"
            async for event in session:
                await bot.on_event(event)
                if bot.finished:
                    return "done"
    except TimeoutError:
        return "time"
    return "closed"


async def run(url: str, args: argparse.Namespace) -> int:
    # The token comes from QTE_TOKEN, or from the file named by QTE_TOKEN_FILE.
    session = await open_session(url)
    # Each Ctrl+C from here on cancels this task. The first is absorbed, so the run still
    # cancels its order; a second is let through and stops the run. The event loop handles
    # Ctrl+C itself where it can (not on Windows).
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGINT, task.cancel)
    try:
        return await trade_and_clean_up(session, args, task)
    finally:
        with contextlib.suppress(NotImplementedError):
            loop.remove_signal_handler(signal.SIGINT)


async def trade_and_clean_up(session: Session, args: argparse.Namespace, task: asyncio.Task) -> int:
    async with closing(session):
        print(f"connected: team {session.info.team}, unscored session: {session.info.unscored}")
        table = await session.wait_for_instrument_table(timeout=TABLE_SECONDS)
        if table is None:
            print("no instruments table from this exchange, so no listed option contract to use")
            return 1
        underlyings = option_underlyings(table)
        underlying = args.underlying or (underlyings[0] if underlyings else None)
        if underlying is None:
            print("the instruments table names no option underlying")
            return 1
        contracts = listed_contracts(table, underlying)
        if not contracts:
            print(f"no option contract is listed on {underlying}; no order was sent")
            return 0
        print(f"{len(contracts)} contract(s) listed on {underlying}; subscribing to them")
        await subscribe(session, contracts)

        bot = OptionsBot(session, table, underlying, args.strat_id)
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
            why = await drive(bot, session, args.seconds)
        except asyncio.CancelledError:
            if not absorb_first_interrupt():
                raise
            why = "interrupted"
        if why == "closed":
            print("the exchange closed the connection")
            if bot.ref is not None and not bot.finished:
                print(f"WARNING: the order may still rest at {bot.where}; check and cancel it")
            return 1
        if not bot.finished and bot.ref is not None:
            reason = {"time": "time is up", "interrupted": "interrupted"}[why]
            print(f"{reason}: cancelling {bot.where}")
            bot.stopping = True
            while True:
                try:
                    why = await drive(bot, session, args.drain_seconds)
                    break
                except asyncio.CancelledError:
                    if not absorb_first_interrupt():
                        raise
                    print("interrupted: still cancelling; press Ctrl+C again to stop at once")
        if bot.cancel_failed is not None:
            print(
                f"WARNING: the cancel was rejected ({bot.cancel_failed}), so the order may "
                f"still rest at {bot.where}; check and cancel it yourself"
            )
            return 1
        if bot.ref is not None and not bot.finished:
            print(
                f"WARNING: the cancel was not confirmed, so the order may still rest at "
                f"{bot.where}; check and cancel it yourself"
            )
            return 1
        if why == "time" and bot.ref is None:
            print(f"stopped after {args.seconds:g} seconds: no contract could take the order")
        print(bot.summary())
        return 0


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # With QTE_LOG_FORMAT=json, the SDK's log records are JSON lines on stderr.
    qte_sdk.logs.configure()
    try:
        url = resolve_url()  # QTE_URL from the environment, or from ./.env
    except MissingURL as error:
        fail(f"{error}; for example ws://127.0.0.1:8080/ws (see docs/quickstart.md)")
        return 2
    try:
        return asyncio.run(run(url, args))
    except MissingToken as error:
        # The message names the variable and the problem, never the token.
        fail(f"{error} (see docs/quickstart.md)")
        return 2
    except ValueError as error:
        return fail(f"not sent: {error}")
    except SessionRejected as error:
        return fail(f"the exchange refused the session: {error}")
    except (OSError, InvalidHandshake, SessionNotAcknowledged) as error:
        return fail(f"could not connect to QTE_URL: {error}")
    except ConnectionClosed:
        return fail(
            "the connection dropped, and this example does not reconnect. Your order may "
            "still rest: reconnect and cancel it"
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        # A second Ctrl+C, before the order was confirmed cancelled.
        return fail("interrupted: this example's order may still rest; check and cancel it")


if __name__ == "__main__":
    sys.exit(main())
