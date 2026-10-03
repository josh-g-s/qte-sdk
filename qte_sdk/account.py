"""Query your team's own account: its positions, cash, equity and limit use.

Send `account_query` and the exchange answers with one `account_state` that echoes its
`request_ref`, on the same stream as your order events. Read it in the one loop that
reads your session:

    ref = await send_account_query(session)
    async for event in session:
        if is_account_state(event) and event.message.request_ref == ref:
            state = event.message
            ...
        elif is_order_event(event) and event.type == "reject":
            if request_ref_of(event.message) == ref:
                ...  # the query was refused: see reason_code

The query always asks about the team the session authenticated as; it names no team, so
no team can ask about another's account. It is not an order message: the exchange does
not hold it for the order delay or count it against the message budgets for new, cancel
and amend, and it may be sent at any hour, inside a session or not. Every team may send
it, including one whose account has no market access.

An `account_state` carries:

- `request_ref`, echoed from the query;
- `summary`, the team's equity, cash, daily profit and loss and limit use, as an
  `AccountSummary`. It is absent for an Execution desk and for the house, so check
  `state.HasField("summary")` before reading it. The exchange never sends an
  account summary unprompted: it is sent only inside this reply;
- `positions`, one `PositionValue` (instrument, signed quantity, price) for every
  instrument the team holds a nonzero position in, in ascending order of instrument, and
  empty when it holds no nonzero net position, whether or not it has traded. A position
  is the team's, not one strategy's. An equity is named by its symbol and an option
  contract by its 21-character OCC option symbol;
- `valuation_basis`, which says what `summary` and every position's `price` are valued
  at: `LIVE_MARK` inside a session, `LAST_OFFICIAL_CLOSE` outside one. Under `LIVE_MARK`
  an instrument with no valid mark yet in the session is still valued at its last
  official close, so a `LIVE_MARK` price is not always a mark. Under
  `LAST_OFFICIAL_CLOSE` each instrument is valued at its latest official close, a break
  day's close included;
- `session_date`, the date of the current session inside one. Outside a session it is
  the date of the last session that has an official close, even across the break
  between terms, when positions carried over from the term before are returned too. It
  names a session only and does not date the close the values use: on a break day that
  day's close is later. Inside a session it is always present. Outside one it is absent
  while no session has an official close yet, so check `state.HasField("session_date")`.
  Outside a session, `summary.daily_pnl` is the profit and loss of the session just
  finished;
- `as_of`, the exchange's timestamp of the state the reply reads. It does not order the
  reply against your private order reports; `as_of_report_seq` does;
- `cash`, the team's cash balance, equal to `summary.cash` when `summary` is present.
  Only an Execution desk, which holds no cash balance of its own, is sent none, so check
  `state.HasField("cash")`;
- `as_of_report_seq`, the highest report sequence number the team had been assigned
  when the state was read. Each private order report (`accepted`, a delayed `reject`,
  `execution`, `order_cancelled`, `order_state`, `risk_notice`) carries a `report_seq`
  on its envelope. The cut is for account effects only: an `execution` or `risk_notice`
  whose `report_seq` is at or below `as_of_report_seq` is already in the cash, positions
  and summary of the reply, so do not apply its account effect again, while `accepted`,
  `reject`, `order_cancelled` and `order_state` still apply whatever their `report_seq`,
  since the reply holds no resting-order state. It is absent when the team has had no
  private report this term, and then every report applies. Each event the SDK delivers
  for such a report carries it as `report_seq`, so the comparison is yours to make; the
  SDK does not yet apply the cut itself.

Prices, cash and equity are whole numbers of micro-dollars; convert them with
`qte_sdk.units.to_decimal`. A query the exchange refuses is answered with a `reject`
that carries `request_type` `ACCOUNT_QUERY` and echoes its `request_ref` whenever the
exchange could read one, which
`qte_sdk.orders.is_order_event` and `request_ref_of` already pick out. Its reason is
`NOT_AUTHENTICATED` before the session authenticates, `MALFORMED_MESSAGE` for a
malformed query, and `TEAM_DISABLED` for a team that has been disabled.

A program that reads only `qte_sdk.market_data.market_data(session)` never sees the reply,
because that drops everything that is not market data.
"""

from typing import TypeGuard

from qte_sdk.connection import Event, Received
from qte_sdk.contract.v1.common_pb2 import LAST_OFFICIAL_CLOSE, LIVE_MARK, ValuationBasis
from qte_sdk.contract.v1.order_events_pb2 import AccountState, AccountSummary, PositionValue
from qte_sdk.contract.v1.session_pb2 import AccountQuery
from qte_sdk.orders import Sender, _ref

__all__ = [
    "LAST_OFFICIAL_CLOSE",
    "LIVE_MARK",
    "AccountState",
    "AccountSummary",
    "PositionValue",
    "ValuationBasis",
    "is_account_state",
    "send_account_query",
]


async def send_account_query(conn: Sender, *, request_ref: str | None = None) -> str:
    """Send `account_query`: ask for your team's own account. Returns its `request_ref`.

    The exchange answers with one `account_state` echoing the `request_ref`, or with a
    `reject` echoing it. A fresh `request_ref` is made when none is given. A given one must
    be 1 to 32 bytes of UTF-8 without the NUL character, the limit the contract sets for
    it as for the order messages, or this raises `ValueError` and sends nothing.
    """
    ref = _ref(request_ref)
    await conn.send("account_query", AccountQuery(request_ref=ref))
    return ref


def is_account_state(event: Event) -> TypeGuard[Received]:
    """Whether `event` is a decoded `account_state`, the reply to an `account_query`.

    Its `message` is then an `AccountState`. Compare its `request_ref` with the one
    `send_account_query` returned to match it to your query.
    """
    return isinstance(event, Received) and event.type == "account_state"
