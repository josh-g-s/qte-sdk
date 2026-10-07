"""The exchange's instruments, and which of them your team may trade.

    table = await session.wait_for_instrument_table()
    if table is not None:
        info = instrument_info(table, "AAPL")
        if info is not None and can_trade(info):
            print("tick:", to_decimal(info.tick_size), "lot:", info.lot_size)

The exchange sends an `instruments` message straight after the calendar that follows its
acknowledgement of a session, at any hour: every instrument it runs, one `InstrumentInfo`
each, sorted by id, and each option underlying with its strike increment and listed
contracts (see `qte_sdk.options.listed_contracts`). It is the whole table, not a change: the
exchange sends it again, whole, whenever an instrument is listed, delisted or changes
status, and each one replaces the one before. `Session.instrument_table` and
`ReconnectingSession.instrument_table` keep the latest.

An `InstrumentInfo` carries:

- `instrument`, the id every other message and every order names it by: an equity's
  ticker, or an option contract's OCC symbol (see `qte_sdk.options`);
- `display_name`, a name to show, absent when the exchange has none (it may leave it unset
  for every instrument): show `instrument` instead, as
  `info.display_name if info.HasField("display_name") else info.instrument`. Never parse it;
- `kind`, `EQUITY` or `OPTION`. Any other value, `INSTRUMENT_KIND_UNSPECIFIED` or a
  number, is a kind this SDK does not know, from a newer exchange: treat the instrument
  as one you cannot trade;
- `tick_size`, the smallest price step in micro-dollars: every order price is a multiple
  of it. It is set by the exchange, so read it from here and never hard-code it;
- `lot_size`, the smallest order size and the step sizes go in: shares for an equity,
  contracts for an option;
- `status`: `INSTRUMENT_TRADING`, `INSTRUMENT_DISABLED` (no orders from anyone), or
  `INSTRUMENT_REDUCING_ONLY` (an option contract outside the day's window: only orders
  that reduce a position, never crossing zero). Any other value is a
  status this SDK does not know: treat it as not trading;
- `tradable`, whether your team is entitled to send orders in it at all, given its arm and
  assignment. It says nothing of the instrument's own status, or of a limit or halt your
  team is under: an instrument can be `tradable` and `INSTRUMENT_DISABLED`. To decide
  whether to send an order, use `can_trade` or `tradable_instruments`, never `tradable`
  alone;
- `option`, the contract's terms (underlying, expiry, right, strike in micro-dollars and
  multiplier), present only for an option;
- `sector_limit`, the sector the Fundamentals sector limit counts the instrument against,
  the same for every team: a GICS sector name such as `Information Technology`, or
  `Unsectored` for an instrument the sector limit leaves out. It is the empty string when
  the exchange has no sector to give, as for every option contract (`sector_of` returns
  None then). The list of sectors may grow: treat a name you do not know as a sector you
  know nothing about, never as an error.

An exchange that predates the message never sends one, so code that uses these helpers
must also work without a table.
"""

from qte_sdk.contract.v1.session_pb2 import (
    EQUITY,
    INSTRUMENT_DISABLED,
    INSTRUMENT_KIND_UNSPECIFIED,
    INSTRUMENT_REDUCING_ONLY,
    INSTRUMENT_STATUS_UNSPECIFIED,
    INSTRUMENT_TRADING,
    OPTION,
    InstrumentInfo,
    InstrumentKind,
    Instruments,
    InstrumentStatus,
    OptionTerms,
    OptionUnderlying,
)

__all__ = [
    "EQUITY",
    "INSTRUMENT_DISABLED",
    "INSTRUMENT_KIND_UNSPECIFIED",
    "INSTRUMENT_REDUCING_ONLY",
    "INSTRUMENT_STATUS_UNSPECIFIED",
    "INSTRUMENT_TRADING",
    "OPTION",
    "InstrumentInfo",
    "InstrumentKind",
    "InstrumentStatus",
    "Instruments",
    "OptionTerms",
    "OptionUnderlying",
    "can_trade",
    "instrument_info",
    "instruments_by_id",
    "on_tick",
    "sector_of",
    "tradable_instruments",
]


# A kind from a newer contract may arrive as a number this SDK has no name for.
_KNOWN_KINDS = (EQUITY, OPTION)


def instruments_by_id(table: Instruments) -> dict[str, InstrumentInfo]:
    """Every instrument in `table`, keyed by its id. Build it once per table when you look
    up many instruments; a later table replaces this one entirely."""
    return {info.instrument: info for info in table.instruments}


def instrument_info(table: Instruments, instrument: str) -> InstrumentInfo | None:
    """The entry for `instrument` in `table`, or None if the exchange does not list it."""
    for info in table.instruments:
        if info.instrument == instrument:
            return info
    return None


def can_trade(info: InstrumentInfo) -> bool:
    """True if your team may send any order in this instrument now: its kind is known, its
    status is `INSTRUMENT_TRADING` and it is `tradable` for your team.

    False for a reducing-only contract, which still accepts orders that reduce a position;
    check `info.status == INSTRUMENT_REDUCING_ONLY` for those. True does not mean an order
    will be accepted: limits, the price collar and the session's state still apply.
    """
    return info.tradable and info.status == INSTRUMENT_TRADING and info.kind in _KNOWN_KINDS


def tradable_instruments(
    table: Instruments, kind: InstrumentKind.ValueType | None = None
) -> list[str]:
    """The ids of every instrument in `table` that `can_trade`, in the table's order,
    optionally only those of one `kind` (`EQUITY` or `OPTION`)."""
    return [
        info.instrument
        for info in table.instruments
        if can_trade(info) and (kind is None or info.kind == kind)
    ]


def sector_of(info: InstrumentInfo) -> str | None:
    """The sector the Fundamentals sector limit counts this instrument against, such as
    `Information Technology` or `Unsectored`, or None when the exchange gives none (an
    option contract, or a term with no sectors set). A name this SDK has never seen is
    returned as it is: the list of sectors may grow."""
    return info.sector_limit or None


def on_tick(info: InstrumentInfo, price: int) -> bool:
    """True if `price`, in micro-dollars, is a whole number of the instrument's ticks.

    False if the table gives no usable tick size (0 or less), so a price is never taken as
    valid against a tick the exchange did not send.
    """
    if isinstance(price, bool) or not isinstance(price, int):
        raise TypeError("price must be an int of micro-dollars; see qte_sdk.units.to_micros")
    return info.tick_size > 0 and price % info.tick_size == 0
