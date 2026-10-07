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

The book is republished when the state changes, so the latest book holds the state in
force. The list of states may grow. Read it with `trading_state`, which treats a state
this SDK does not know as `OPTION_SUSPENDED`, the most restrictive, rather than the
field itself. An option contract's wall is
one level per side, or none at all while it is suspended or its quote is not two-sided.

Reasons for options. A `new` or `amend` in an option contract can be rejected for three
reasons of its own:

- `CONTRACT_NOT_LISTED`: the contract is not listed in this session.
- `CONTRACT_SUSPENDED`: the contract was suspended when the order message was applied,
  after its order delay. A `cancel` there still applies.
- `CONTRACT_REDUCING_ONLY`: the contract is reducing-only and the order would grow your
  team's absolute position in it, or take it across zero.

A resting order in a reducing-only contract can also be cancelled, with an
`order_cancelled` whose reason is `CONTRACT_REDUCING_RECHECK_FAILED`, when it would grow
the position or cross zero: the whole order when the contract turns reducing-only, or
just the part that would, before a fill. Read these as any other reason, with
`qte_sdk.orders.reason_code_name` or against `ReasonCodes`.

Not published yet:

- Options in the history service: it serves no `option_chain` or `option_greeks`, so
  `qte_sdk.replay` replays no Greeks.
- The residual print of an option trade. It is to be marked by a new optional flag on
  `TapePrint`; until that is published, nothing marks it.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from qte_sdk.books import _LatestByInstrument
from qte_sdk.contract.v1.market_data_pb2 import (
    OPTION_GREEKS_NONE,
    OPTION_GREEKS_UNAVAILABLE,
    OPTION_GREEKS_VALID,
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
)
from qte_sdk.contract.v1.session_pb2 import Instruments, OptionUnderlying

__all__ = [
    "CALL",
    "OPTION_GREEKS_NONE",
    "OPTION_GREEKS_UNAVAILABLE",
    "OPTION_GREEKS_VALID",
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
    `OPTION_REDUCING_ONLY` or `OPTION_SUSPENDED`, or None for the book of an instrument
    that is not an option contract.

    A state this SDK does not know is read as `OPTION_SUSPENDED`, the most restrictive,
    as the exchange asks. A state name from a newer contract cannot be decoded and leaves
    the field unset, but the exchange sets it on every option contract's book, so an
    option contract's book without it is read as `OPTION_SUSPENDED` too."""
    if not book.HasField("trading_state"):
        return OPTION_SUSPENDED if is_option_symbol(book.instrument) else None
    if book.trading_state in (OPTION_TRADING, OPTION_REDUCING_ONLY, OPTION_SUSPENDED):
        return book.trading_state
    return OPTION_SUSPENDED


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
