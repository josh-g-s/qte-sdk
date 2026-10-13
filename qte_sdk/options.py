"""Option contracts: their symbols, the day's chain, published Greeks and trading state.

The exchange names an option contract by its OCC option symbol, in the unpadded form
Alpaca uses: the underlying's root, the expiry as YYMMDD, `C` for a call or `P` for a
put, and the strike in thousandths of a dollar as eight digits. Its length varies with
the root, and it holds no spaces:

    >>> from qte_sdk.units import to_micros
    >>> option_symbol("SPY", date(2024, 1, 19), CALL, to_micros("470"))
    'SPY240119C00470000'
    >>> parse_option_symbol("SPY240119C00470000")
    OptionSymbol(underlying='SPY', expiry=datetime.date(2024, 1, 19), right='C', strike=470000000)

The strike is in micro-dollars, like every price in this SDK, so it can be compared with
prices from the exchange without conversion; a symbol can carry only whole thousandths of
a dollar, so a strike that is not a multiple of 1000 micro-dollars has no symbol.

`is_option_symbol` tells an option contract's id from an equity's, for example in an
account's positions, where both appear in `PositionValue.instrument`.

An option contract is an instrument like any other. Subscribe to it by its symbol, as
to a stock; its `Book`, `Trades`, `Mark` and `OfficialClose` are the usual messages, and
`qte_sdk.books.LatestBooks` keeps its book. Order it with `qte_sdk.orders.send_new` and
the rest, naming the contract in `instrument`. Every size is a whole number of
contracts, never shares: the 100-share multiplier is never applied on the wire. A
`Mark`'s `value` and every price stay in micro-dollars per share.

The day's chain. `OptionChain` lists the contracts that accept orders in one session: for
each underlying and expiry, a call and a put at each active strike, and any contract kept
listed because a position is open in it. Each contract has a role: `OPTION_ROLE_ACTIVE`,
`OPTION_ROLE_OBLIGATED` (the one a market making desk must quote at its strike) or
`OPTION_ROLE_RETAINED` (reducing-only). The chain is fixed for its session. It reaches a
connection once that connection holds a subscription to at least one option contract:
in the answer to the `subscribe` that gives it its first one, and again when the next
session's chain is published, between the close and the next open. There is no request
for the chain, so the first option subscribe must name a listed contract; a subscribe
naming a contract that is not listed is rejected `UNKNOWN_INSTRUMENT`. Take that first
contract from the exchange's `instruments` message (`Session.instrument_table`, see
`qte_sdk.instruments`), which names each option underlying with its strike increment and
the ids of its listed contracts: `option_underlyings` lists the underlyings,
`listed_contracts` an underlying's contracts and `strike_increment` its increment. An
underlying with no contract listed yet has an empty list. An exchange that predates the
`instruments` message sends none; then you work a contract out from the symbol rules.

    >>> from qte_sdk.contract.v1.session_pb2 import Instruments
    >>> table = Instruments()
    >>> _ = table.option_underlyings.add(
    ...     underlying="SPY", strike_increment=5_000_000,
    ...     contracts=["SPY261120C00665000", "SPY261120P00665000"])
    >>> listed_contracts(table, "SPY")
    ('SPY261120C00665000', 'SPY261120P00665000')
    >>> strike_increment(table, "SPY")
    5000000

`chain_contracts` filters a chain, and `expiry_date` and `limit_scope` read an expiry:

    >>> from qte_sdk.contract.v1.market_data_pb2 import OptionChain
    >>> chain = OptionChain(session_date="2026-10-26")
    >>> spy = chain.expiries.add(underlying="SPY", expiry="2026-11-20")
    >>> _ = spy.contracts.add(instrument="SPY261120C00665000", role=OPTION_ROLE_OBLIGATED)
    >>> _ = spy.contracts.add(instrument="SPY261120P00665000", role=OPTION_ROLE_ACTIVE)
    >>> [c.instrument for c in chain_contracts(chain, role=OPTION_ROLE_OBLIGATED)]
    ['SPY261120C00665000']
    >>> limit_scope("SPY", expiry_date(spy))
    'SPY 2026-11-20'

Greeks. `OptionGreeks` carries one contract's published delta, gamma, vega and theta,
with the forward and implied volatility they were calculated from, all per share. They
come at each calculation for every listed contract of the expiry, changed or not: at
the expiry's open calculation, every five minutes after it, at the close, and when a
contract's mark first becomes valid. The first grid point of a session carries the held
values. A subscribe during a session is answered with the last Greeks published for the
contract. `LatestGreeks` keeps the latest per contract, as `LatestBooks` does for books.
Their values are fixed-point integers. Read them exactly as `Decimal` with
`greek_to_decimal` (delta and gamma, in units of 10^-12) and `vol_to_decimal`
(implied volatility, in millionths of a volatility point). Vega, theta and the forward
are micro-dollars, for `qte_sdk.units.to_decimal`:

    >>> greek_to_decimal(500_000_000_000)  # a delta of 0.5
    Decimal('0.500000000000')
    >>> vol_to_decimal(18_000_000)  # 18 volatility points: 18 percent
    Decimal('18.000000')

`status` says whether to rely on them. With `OPTION_GREEKS_VALID` the values come from
the latest calculation. With `OPTION_GREEKS_UNAVAILABLE` they are the last published,
and the contract is reducing-only. With `OPTION_GREEKS_NONE` there are no values at
all (check `HasField` before reading one), and the contract is suspended. A status
added in a later contract decodes as `OPTION_GREEKS_STATUS_UNSPECIFIED`: rely on the
values only when `status` is `OPTION_GREEKS_VALID`. Likewise a role added later decodes
as `OPTION_CONTRACT_ROLE_UNSPECIFIED`, which no role filter matches.

Trading state. An option contract's `Book` carries `trading_state`:

- `OPTION_TRADING`: ordinary trading.
- `OPTION_REDUCING_ONLY`: only orders that reduce your position without crossing zero.
- `OPTION_SUSPENDED`: no wall and no matching; new orders are refused, cancels are
  accepted.
- `OPTION_HALTED`: a Director has halted the contract's whole chain, every contract on
  its underlying, before the underlying's split ex-date in the Winter term. No wall and no
  matching; new orders and amends are refused (`CONTRACT_HALTED`), cancels are accepted.
  The orders resting in the chain when the halt started were cancelled (`CHAIN_HALTED`).
  The mark and Greeks still publish. It lasts until the Director ends the halt, and it
  wins over every other state. The end restores no order: once the contract's book shows
  another state, place again only what that state allows. The underlying stock is not
  halted.

The book is republished when the state changes, so the latest book holds the state in
force. The list of states may grow. Read it with `trading_state`, which treats a state
this SDK does not know as `OPTION_SUSPENDED` (no new orders, cancels accepted) rather
than the field itself. Send a `new` or `amend` only while it is `OPTION_TRADING`, or
`OPTION_REDUCING_ONLY` for an order that reduces your position. An option contract's wall
is one level per side, or none at all while it is suspended or halted or its quote is not
two-sided.

Reasons for options. A `new` or `amend` in an option contract can be rejected for four
reasons of its own:

- `CONTRACT_NOT_LISTED`: the contract is not listed in this session.
- `CONTRACT_SUSPENDED`: the contract was suspended when the order message was applied,
  after its order delay. A `cancel` there still applies.
- `CONTRACT_REDUCING_ONLY`: the contract is reducing-only and the order would grow your
  team's absolute position in it, or take it across zero.
- `CONTRACT_HALTED`: a Director's halt of the contract's chain was in force. A `cancel`
  there still applies. Do not send it again until the book's state is no longer
  `OPTION_HALTED`.

A resting order in a reducing-only contract can also be cancelled, with an
`order_cancelled` whose reason is `CONTRACT_REDUCING_RECHECK_FAILED`, when it would grow
the position or cross zero: the whole order when the contract turns reducing-only, or
just the part that would, before a fill. When a Director halts a chain, every order
resting in it, yours included, is cancelled with an `order_cancelled` whose reason is
`CHAIN_HALTED`. Read these as any other reason, with `qte_sdk.orders.reason_code_name` or
against `ReasonCodes`. A session logs `QTE-OPTIONS-CHAIN-HALTED` through the
`qte_sdk.options` logger, at most once a minute for each underlying, when it reads a
`CONTRACT_HALTED` reject or a `CHAIN_HALTED` cancel (see docs/errors.md). A reject does
not name its contract, so the session names it from the `new` or `amend` it sent with that
`request_ref`; a reject of an order it did not send, or of a `request_ref` it sent again
for another contract before every send of it was answered, names no chain, and such
rejects share one minute. Another connection of your team's may read the same reject, so
use a `request_ref` on one connection only while its order awaits an answer: a session
cannot tell another connection's order from its own, so a `request_ref` reused on another
connection can name the other order's chain. Each session keeps its own minute
and record of the orders it sent, and a `ReconnectingSession` one across its connections.

Feed-only residual prints. A `TapePrint` in live `trades` may carry `feed_only`. It is
present, and true, only on an option contract's `RESIDUAL` print that went through at the
live print's own premium because the contract had no wall on the side the aggressor
took. It is never sent as false, and it is absent on every other print: an equity's, an
option contract's other kinds, and a residual that printed at the wall's price. Test it
with `HasField("feed_only")` or `is_feed_only`; absent means an ordinary print.

    >>> from qte_sdk.contract.v1.common_pb2 import RESIDUAL
    >>> from qte_sdk.contract.v1.market_data_pb2 import TapePrint
    >>> is_feed_only(TapePrint(kind=RESIDUAL, feed_only=True))
    True
    >>> is_feed_only(TapePrint(kind=RESIDUAL))
    False

Not published yet:

- Options in the history service: it serves no `option_chain` or `option_greeks`, so
  `qte_sdk.replay` replays no Greeks.
"""

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from google.protobuf.message import Message

from qte_sdk import errors as _errors
from qte_sdk import pacing as _pacing
from qte_sdk.books import _LatestByInstrument
from qte_sdk.connection import Event, Received
from qte_sdk.contract.v1.common_pb2 import AMEND, NEW, ReasonCodes
from qte_sdk.contract.v1.market_data_pb2 import (
    OPTION_GREEKS_NONE,
    OPTION_GREEKS_UNAVAILABLE,
    OPTION_GREEKS_VALID,
    OPTION_HALTED,
    OPTION_REDUCING_ONLY,
    OPTION_ROLE_ACTIVE,
    OPTION_ROLE_OBLIGATED,
    OPTION_ROLE_RETAINED,
    OPTION_SUSPENDED,
    OPTION_TRADING,
    OPTION_WINDOW_COMPUTED,
    OPTION_WINDOW_RETAINED,
    Book,
    OptionChain,
    OptionChainContract,
    OptionChainExpiry,
    OptionContractRole,
    OptionGreeks,
    OptionGreeksStatus,
    OptionTradingState,
    TapePrint,
)
from qte_sdk.contract.v1.order_events_pb2 import Accepted, OrderCancelled, Reject
from qte_sdk.contract.v1.session_pb2 import Instruments, OptionUnderlying

__all__ = [
    "CALL",
    "OPTION_GREEKS_NONE",
    "OPTION_GREEKS_UNAVAILABLE",
    "OPTION_GREEKS_VALID",
    "OPTION_HALTED",
    "OPTION_REDUCING_ONLY",
    "OPTION_ROLE_ACTIVE",
    "OPTION_ROLE_OBLIGATED",
    "OPTION_ROLE_RETAINED",
    "OPTION_SUSPENDED",
    "OPTION_TRADING",
    "OPTION_WINDOW_COMPUTED",
    "OPTION_WINDOW_RETAINED",
    "PUT",
    "LatestGreeks",
    "OptionChain",
    "OptionChainContract",
    "OptionChainExpiry",
    "OptionContractRole",
    "OptionGreeks",
    "OptionGreeksStatus",
    "OptionSymbol",
    "OptionTradingState",
    "chain_contracts",
    "expiry_date",
    "greek_to_decimal",
    "is_feed_only",
    "is_option_symbol",
    "limit_scope",
    "listed_contracts",
    "option_underlyings",
    "option_symbol",
    "parse_option_symbol",
    "strike_increment",
    "trading_state",
    "vol_to_decimal",
]

CALL: Literal["C"] = "C"
PUT: Literal["P"] = "P"

# The OCC strike field: eight digits of thousandths of a dollar.
_MICROS_PER_STRIKE_UNIT = 1_000
_STRIKE_DIGITS = 8
_MAX_STRIKE_UNITS = 10**_STRIKE_DIGITS - 1

# A root of one to six upper-case letters or digits, as the exchange accepts, then the
# fixed fifteen-character suffix. The suffix has a fixed shape, so the root is whatever
# comes before it.
_ROOT = re.compile(r"[A-Z0-9]{1,6}")
_SYMBOL = re.compile(r"([A-Z0-9]{1,6})([0-9]{2})([0-9]{2})([0-9]{2})([CP])([0-9]{8})")


@dataclass(frozen=True)
class OptionSymbol:
    """The parts of an option contract's symbol. `strike` is in micro-dollars."""

    underlying: str
    expiry: date
    right: Literal["C", "P"]
    strike: int

    def __str__(self) -> str:
        return option_symbol(self.underlying, self.expiry, self.right, self.strike)


def option_symbol(underlying: str, expiry: date, right: str, strike: int) -> str:
    """The symbol of an option contract: `underlying` is the root, such as "SPY",
    `expiry` the expiration date, `right` `CALL` ("C") or `PUT` ("P"), and `strike` the
    strike in micro-dollars, a whole number of thousandths of a dollar from 0.001 to
    99999.999. Raises `ValueError` for anything that has no symbol."""
    if not isinstance(underlying, str) or not _ROOT.fullmatch(underlying):
        raise ValueError("the underlying must be 1 to 6 upper-case letters or digits")
    if isinstance(expiry, datetime) or not isinstance(expiry, date):
        # A datetime is a date too, but its date can differ between UTC and New York.
        raise ValueError("the expiry must be a datetime.date, not a datetime")
    if not 2000 <= expiry.year <= 2099:
        raise ValueError("the expiry year must be between 2000 and 2099")
    if right not in (CALL, PUT):
        raise ValueError('the right must be "C" (CALL) or "P" (PUT)')
    units = _strike_units(strike)
    return f"{underlying}{expiry:%y%m%d}{right}{units:0{_STRIKE_DIGITS}d}"


def parse_option_symbol(symbol: str) -> OptionSymbol:
    """The parts of an option contract's symbol, such as "SPY240119C00470000". Raises
    `ValueError` if `symbol` is not one."""
    if not isinstance(symbol, str):
        raise ValueError("an option symbol must be a string")
    match = _SYMBOL.fullmatch(symbol)
    if match is None:
        raise ValueError(
            "not an option symbol: expected a root of 1 to 6 upper-case letters or digits, "
            "then YYMMDD, C or P, and an 8-digit strike in thousandths of a dollar"
        )
    root, yy, mm, dd, right, strike = match.groups()
    try:
        expiry = date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        raise ValueError("not an option symbol: its expiry is not a real date") from None
    units = int(strike)
    if units == 0:
        raise ValueError("not an option symbol: its strike is zero")
    return OptionSymbol(root, expiry, right, units * _MICROS_PER_STRIKE_UNIT)  # type: ignore[arg-type]


def is_option_symbol(instrument: str) -> bool:
    """True if `instrument` is an option contract's symbol rather than an equity's."""
    try:
        parse_option_symbol(instrument)
    except ValueError:
        return False
    return True


def _strike_units(strike: int) -> int:
    if isinstance(strike, bool) or not isinstance(strike, int):
        raise ValueError(
            "the strike must be an int of micro-dollars; use qte_sdk.units.to_micros to "
            'convert, such as to_micros("470")'
        )
    if strike % _MICROS_PER_STRIKE_UNIT:
        raise ValueError("the strike must be a whole number of thousandths of a dollar")
    units = strike // _MICROS_PER_STRIKE_UNIT
    if not 1 <= units <= _MAX_STRIKE_UNITS:
        raise ValueError("the strike must be from 0.001 to 99999.999 dollars")
    return units


def expiry_date(expiry: OptionChainExpiry) -> date:
    """The expiry date of one entry of the day's chain (its `expiry` is an ISO 8601 date)."""
    return date.fromisoformat(expiry.expiry)


def limit_scope(underlying: str, expiry: date) -> str:
    """The `scope` of a `LimitUtilisation` row for one underlying and expiry, such as
    "SPY 2026-11-20": the underlying, one space and the expiry date. Compare it with a
    `LIMIT_VEGA` row's `scope` to match the row to the chain."""
    return f"{underlying} {expiry.isoformat()}"


def chain_contracts(
    chain: OptionChain,
    *,
    underlying: str | None = None,
    expiry: date | None = None,
    role: OptionContractRole.ValueType | None = None,
) -> list[OptionChainContract]:
    """The listed contracts of `chain`, in the chain's order, keeping only those of
    `underlying`, of `expiry` and with `role` where each is given."""
    return [
        contract
        for entry in chain.expiries
        if underlying is None or entry.underlying == underlying
        if expiry is None or expiry_date(entry) == expiry
        for contract in entry.contracts
        if role is None or contract.role == role
    ]


def option_underlyings(table: Instruments) -> list[str]:
    """The id of every option underlying in the exchange's `instruments` message, in its
    order, including those with no contract listed yet."""
    return [entry.underlying for entry in table.option_underlyings]


def listed_contracts(table: Instruments, underlying: str) -> tuple[str, ...]:
    """The ids of the option contracts listed on `underlying`, as the exchange's
    `instruments` message gives them (sorted by byte order). Empty if none is listed yet
    or if the table does not name `underlying` as an option underlying."""
    entry = _underlying(table, underlying)
    return () if entry is None else tuple(entry.contracts)


def strike_increment(table: Instruments, underlying: str) -> int | None:
    """The published strike increment of `underlying`, in micro-dollars, or None if the
    table does not name it as an option underlying."""
    entry = _underlying(table, underlying)
    return None if entry is None else entry.strike_increment


def _underlying(table: Instruments, underlying: str) -> OptionUnderlying | None:
    for entry in table.option_underlyings:
        if entry.underlying == underlying:
            return entry
    return None


def trading_state(book: Book) -> OptionTradingState.ValueType | None:
    """What the contract of an option `book` may do now: `OPTION_TRADING`,
    `OPTION_REDUCING_ONLY`, `OPTION_SUSPENDED` or `OPTION_HALTED`, or None for the book of
    an instrument that is not an option contract.

    Send a `new` or `amend` only while it is `OPTION_TRADING`, or `OPTION_REDUCING_ONLY`
    for an order that reduces your position. While it is `OPTION_SUSPENDED` or
    `OPTION_HALTED` send only cancels. After `OPTION_HALTED`, place again what the new
    state allows, since the exchange cancelled every resting order in the chain when the
    halt started and the end restores none.

    A state this SDK does not know is read as `OPTION_SUSPENDED`, which accepts no new
    order, as the exchange asks. A state name from a newer contract cannot be decoded and leaves
    the field unset, but the exchange sets it on every option contract's book, so an
    option contract's book without it is read as `OPTION_SUSPENDED` too."""
    if not book.HasField("trading_state"):
        return OPTION_SUSPENDED if is_option_symbol(book.instrument) else None
    if book.trading_state in (
        OPTION_TRADING,
        OPTION_REDUCING_ONLY,
        OPTION_SUSPENDED,
        OPTION_HALTED,
    ):
        return book.trading_state
    return OPTION_SUSPENDED


def is_feed_only(tape_print: TapePrint) -> bool:
    """Whether a tape print is an option contract's feed-only residual print: one whose
    `feed_only` flag is present and true. An absent flag, as on every equity print, is an
    ordinary print."""
    return tape_print.HasField("feed_only") and tape_print.feed_only


def greek_to_decimal(value: int) -> Decimal:
    """A published delta or gamma, a fraction times 10^12, as an exact `Decimal` with
    twelve decimal places: 500_000_000_000 is 0.5."""
    return _fixed(value, 12, "a delta or gamma")


def vol_to_decimal(value: int) -> Decimal:
    """A published implied volatility, in millionths of a volatility point, as an exact
    `Decimal` count of volatility points with six decimal places: 18_000_000 is 18, or 18
    percent."""
    return _fixed(value, 6, "an implied volatility")


class LatestGreeks(_LatestByInstrument[OptionGreeks]):
    """The latest `OptionGreeks` received for each option contract, keyed by its symbol.

    It follows the rules of `qte_sdk.books.LatestBooks`. The Greeks with the latest
    `grid_time` win, so the snapshot a subscribe is answered with never replaces newer
    values. After missed messages, every contract held is listed in `stale` until newer
    Greeks for it arrive (or, after a reconnect, Greeks at the same `grid_time`). It keeps
    what it is given and ignores every other message."""

    _kind = OptionGreeks
    _type = "option_greeks"

    def update(self, item: object) -> bool:
        """Apply one market-data item. Returns True if it is an `OptionGreeks` that
        replaced the Greeks held for its contract; False for anything else."""
        return super().update(item)

    def get(self, instrument: str) -> OptionGreeks | None:
        """The latest Greeks held for the contract `instrument`, or None."""
        return super().get(instrument)

    @property
    def stale(self) -> frozenset[str]:
        """The contracts whose Greeks held may be out of date because messages were
        missed since they were received. They are still kept and returned by `get`."""
        return super().stale


def _fixed(value: int, places: int, what: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{what} is an int on the wire, not {type(value).__name__}")
    # Built from the integer's own digits, as `qte_sdk.units.to_decimal` is, so the result
    # does not depend on the decimal context.
    sign = 1 if value < 0 else 0
    digits = tuple(int(d) for d in str(abs(value)))
    return Decimal((sign, digits, -places))


# The warning a session logs when a chain halt rejects or cancels one of its orders, at most
# once a minute for each underlying: a halt's start cancels every resting order in the
# chain at once.
CHAIN_HALT_WARNING_INTERVAL = 60.0
_CHAIN_HALT_ACTIONS = {
    ReasonCodes.CONTRACT_HALTED: "rejected",
    ReasonCodes.CHAIN_HALTED: "cancelled",
}
# The most `new` and `amend` a session remembers the instrument of while it waits for their
# `accepted` or `reject`; the oldest is forgotten first.
_SENT_LIMIT = 4096
_log = logging.getLogger(__name__)
# The clock of that warning; replaced in tests.
_monotonic = time.monotonic


class _ChainHaltWarnings:
    """One session's `QTE-OPTIONS-CHAIN-HALTED` warnings: when each underlying's was last
    logged, and the instrument of each `new` and `amend` the session sent and has no answer
    to yet. A `reject` names no instrument (only `UNKNOWN_INSTRUMENT` does), so a
    `CONTRACT_HALTED` reject is matched to its order through the `request_ref` it echoes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._warned_at: dict[str, float] = {}
        # For each `request_ref` awaiting an answer: the instrument, or None once it was sent
        # again for another instrument before every earlier send was answered (an answer
        # could be for either order, so it names no chain), and how many sends await one.
        self._sent: dict[str, tuple[str | None, int]] = {}
        # The report_seq of the latest answers counted, so that one read again, as a
        # resume replays what was read before a reconnect, is not counted twice.
        self._counted: dict[int, None] = {}

    def sent(self, type_: str, payload: Message) -> None:
        """Remember the instrument of a `new` or `amend` the session is sending. Every one is
        counted, an option's or not, so a `request_ref` sent again is always noticed."""
        if type_ not in ("new", "amend"):
            return
        ref = getattr(payload, "request_ref", "")
        if not ref:
            return
        instrument = getattr(payload, "instrument", "")
        with self._lock:
            if ref in self._sent:
                earlier, waiting = self._sent.pop(ref)
                self._sent[ref] = (instrument if earlier == instrument else None, waiting + 1)
            else:
                self._sent[ref] = (instrument, 1)
            while len(self._sent) > _SENT_LIMIT:
                del self._sent[next(iter(self._sent))]

    def _answered(self, ref: str) -> str:
        """Count one answer to `ref`, forgetting it once every send of it is answered.
        Returns its option contract, or "" when it names no one option contract."""
        with self._lock:
            if ref not in self._sent:
                return ""
            instrument, waiting = self._sent.pop(ref)
            if waiting > 1:
                self._sent[ref] = (instrument, waiting - 1)
        return instrument if instrument and is_option_symbol(instrument) else ""

    def _new_term(self) -> None:
        """Report numbers start again in a new term: forget those already counted."""
        with self._lock:
            self._counted.clear()

    def _first_reading(self, event: Received) -> bool:
        """Whether this answer is read for the first time: False for a report read again,
        by its report_seq, which then counts against no order and names no chain."""
        seq = event.report_seq
        if seq is None:
            return True
        with self._lock:
            if seq in self._counted:
                return False
            self._counted[seq] = None
            while len(self._counted) > _SENT_LIMIT:
                del self._counted[next(iter(self._counted))]
        return True

    def observe(self, event: Event) -> None:
        """Forget the order an `accepted` or `reject` of a `new` or `amend` answers, and log
        `QTE-OPTIONS-CHAIN-HALTED` for a reject `CONTRACT_HALTED` or an `order_cancelled`
        `CHAIN_HALTED`, unless this session logged one for the same underlying in the last
        minute. A reject whose order this session did not send names no chain; such rejects
        share one minute between them."""
        if not isinstance(event, Received):
            return
        message = event.message
        if isinstance(message, Accepted):
            # Only a `new` or `amend` is recorded, so only its answer counts against one.
            if message.request_type in (NEW, AMEND) and self._first_reading(event):
                self._answered(message.request_ref)
            return
        if isinstance(message, Reject):
            instrument = ""
            if (
                message.HasField("request_ref")
                and message.HasField("request_type")
                and message.request_type in (NEW, AMEND)
                and self._first_reading(event)
            ):
                instrument = self._answered(message.request_ref)
        elif isinstance(message, OrderCancelled):
            instrument = message.instrument
        else:
            return
        action = _CHAIN_HALT_ACTIONS.get(message.reason_code)
        if action is None:
            return
        underlying = ""
        if is_option_symbol(instrument):
            underlying = parse_option_symbol(instrument).underlying
        now = _monotonic()
        with self._lock:
            warned = self._warned_at.get(underlying)
            if warned is not None and now - warned < CHAIN_HALT_WARNING_INTERVAL:
                return
            self._warned_at[underlying] = now
        _pacing._warn(
            _errors.OPTIONS_CHAIN_HALTED,
            target=_log,
            action=action,
            reason=ReasonCodes.ReasonCode.Name(message.reason_code),
            instrument=instrument or "an option contract",
            underlying=underlying or "its underlying",
        )
