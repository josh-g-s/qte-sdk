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
  for such a report carries it as `report_seq`, and `covers` and `AccountReports`
  (below) make the comparison for you.

Prices, cash and equity are whole numbers of micro-dollars; convert them with
`qte_sdk.units.to_decimal`. A query the exchange refuses is answered with a `reject`
that carries `request_type` `ACCOUNT_QUERY` and echoes its `request_ref` whenever the
exchange could read one, which
`qte_sdk.orders.is_order_event` and `request_ref_of` already pick out. Its reason is
`NOT_AUTHENTICATED` before the session authenticates, `MALFORMED_MESSAGE` for a
malformed query, and `TEAM_DISABLED` for a team that has been disabled.

A program that reads only `qte_sdk.market_data.market_data(session)` never sees the reply,
because that drops everything that is not market data.

Counting each report once: a program that keeps its own positions takes them from a reply
and then adds each later fill. Where a report arrives in the stream does not say whether
the reply includes it; only its `report_seq` does. So a fill the reply already counts can
arrive after the reply, and would be counted twice, and one the reply does not count can
arrive between the query and the reply, and would be lost when the reply replaces it.
`AccountReports` handles both. Send the query with it, pass it every event, and apply what
it returns, in order:

    account = AccountReports()
    await account.query(session)
    async for event in session:
        for item in account.update(event):
            if is_account_state(item):
                ...  # replace your account with the reply's cash and positions
            else:
                ...  # apply the report: a fill's size, a risk notice

`covers(state, event)` is the rule on its own, for a program that keeps the reply itself.
Report numbers start again each term, so a reply says nothing about a later term's
reports: before you trade in a new term (the calendar's `term_start` and `term_end` name
it), query again and wait for the reply.
"""

from typing import TypeGuard

from qte_sdk.connection import Disconnected, Event, Received
from qte_sdk.contract.v1.common_pb2 import (
    ACCOUNT_QUERY,
    LAST_OFFICIAL_CLOSE,
    LIVE_MARK,
    ValuationBasis,
)
from qte_sdk.contract.v1.order_events_pb2 import AccountState, AccountSummary, PositionValue
from qte_sdk.contract.v1.session_pb2 import AccountQuery
from qte_sdk.orders import Sender, _ref, new_request_ref, request_ref_of

__all__ = [
    "ACCOUNT_REPORT_TYPES",
    "LAST_OFFICIAL_CLOSE",
    "LIVE_MARK",
    "AccountReports",
    "AccountState",
    "AccountSummary",
    "PositionValue",
    "ValuationBasis",
    "covers",
    "is_account_state",
    "send_account_query",
]

ACCOUNT_REPORT_TYPES = frozenset({"execution", "risk_notice"})
"""The private reports that change the account, and so the only ones an `account_state`
can already include. The others (`accepted`, `reject`, `order_cancelled`, `order_state`)
change only your resting orders, which a reply does not hold."""


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


def covers(state: AccountState | None, event: object) -> bool:
    """Whether the reply `state` already includes the effect of `event` on the account.

    True only for an `execution` or `risk_notice` whose `report_seq` is at or below the
    reply's `as_of_report_seq`: its effect on cash, positions and summary is already in the
    reply, so do not apply it again. This holds wherever the event arrives in the stream,
    before the reply or after it.

    False means the reply does not include it, so apply it as usual. That is always the
    answer for:

    - every other event. `accepted`, `reject`, `order_cancelled` and `order_state` still
      apply to your view of your resting orders whatever their `report_seq`;
    - any event when `state` is None, or has no `as_of_report_seq`, which the exchange
      leaves out when your team has had no private report this term;
    - an event with no `report_seq`. Every `execution` and `risk_notice` carries one, so
      such an event comes from an exchange that does not number its reports, and that
      exchange sends no `as_of_report_seq` either, so every report applies. The reply can
      then be ordered against the reports only by when they arrive, which the exchange
      does not promise, so trust it fully only when none of your orders can fill meanwhile.

    A `DecodeFailed` for an `execution` or `risk_notice` is judged the same way: when this
    is True, the reply already includes what it would have told you. Report numbers start
    again each term, so a reply from an earlier term covers nothing in a later one. This
    function cannot tell the terms apart, so query again in each new term.
    """
    if state is None or not state.HasField("as_of_report_seq"):
        return False
    if getattr(event, "type", None) not in ACCOUNT_REPORT_TYPES:
        return False
    report_seq = getattr(event, "report_seq", None)
    return report_seq is not None and report_seq <= state.as_of_report_seq


class AccountReports:
    """Sorts your team's `execution` and `risk_notice` reports against its `account_state`
    replies, so that a program that keeps its own account applies each report once.

    Send the query with `query` and pass every event of your one loop to `update`, which
    returns what to apply to your account now, in order:

    - for the reply to your latest query: the reply itself, then the reports that arrived
      while you waited for it and that it does not include, in the order they arrived.
      Replace your account with the reply's cash and positions, then apply those reports
      again: you applied them once already, to the account the reply replaces. The reply
      becomes `state`;
    - for an `execution` or `risk_notice`: the event itself, unless `state` already
      includes it (see `covers`), and nothing if it does;
    - for anything else: nothing. That includes any other `account_state`: a reply to an
      earlier query that a later one replaced, or to a query sent with
      `send_account_query`. Take a reply as your account only when `update` returns it.
      Apply `accepted`, `reject`, `order_cancelled` and `order_state` to your resting
      orders as usual (`qte_sdk.resting.RestingOrders` does).

    Only the latest query is waited for, so wait for its answer before you query again.
    Reports are held from the moment it is sent until its reply, a `reject` of it, or a
    `Disconnected`, after which its answer never comes. Within a term, a report that
    arrived before the query was sent is always in the reply, so none from before is held.

    Only numbered reports are held. Against an exchange that does not number its reports,
    a reply is taken to include every report that arrived before it and none that arrive
    after it, which that exchange does not promise. After a `SeqGap`, a `ReportGap`, a
    `Disconnected` or a report that could not be decoded, your account may be wrong, so
    query again. Report numbers start again each term, and a reply from the term before
    would take a new term's first reports for ones it includes: before you trade in a new
    term, query and wait for the reply, or start a new `AccountReports`.
    """

    def __init__(self) -> None:
        self.state: AccountState | None = None
        """The latest reply `update` returned, or None before the first."""
        self._awaiting: str | None = None
        self._held: list[Received] = []

    async def query(self, conn: Sender) -> str:
        """Send `account_query` and wait for its answer in place of any earlier query.
        Returns its `request_ref`.

        The `request_ref` is always a fresh one, so no other reply can be taken for this
        query's. If the send fails, the query is not waited for, and unless a later query
        has taken its place meanwhile, none is until the next one."""
        ref = new_request_ref()
        # Waited for from before the send, so no report that arrives meanwhile is missed.
        self._awaiting = ref
        self._held = []
        try:
            await send_account_query(conn, request_ref=ref)
        except BaseException:
            if self._awaiting == ref:  # unless a later query has taken its place
                self._stop_waiting()
            raise
        return ref

    def update(self, event: object) -> list[Received]:
        """Take in one event of the session; return what to apply to your account now."""
        if isinstance(event, Disconnected):
            self._stop_waiting()
            return []
        if not isinstance(event, Received):
            return []
        state = event.message
        if isinstance(state, AccountState):
            if self._awaiting is None or state.request_ref != self._awaiting:
                return []
            self.state = state
            again = [report for report in self._held if not covers(state, report)]
            self._stop_waiting()
            return [event, *again]
        if event.type == "reject":
            if self._refuses_query(event):
                self._stop_waiting()
            return []
        if event.type not in ACCOUNT_REPORT_TYPES:
            return []
        if self._awaiting is not None and event.report_seq is not None:
            self._held.append(event)
        return [] if covers(self.state, event) else [event]

    def _refuses_query(self, event: Received) -> bool:
        message = event.message
        if self._awaiting is None or request_ref_of(message) != self._awaiting:
            return False
        if message.HasField("request_type"):
            return message.request_type == ACCOUNT_QUERY
        # It names no request type, not even one from a newer contract this SDK does not know.
        payload = event.payload or {}
        return "request_type" not in payload and "requestType" not in payload

    def _stop_waiting(self) -> None:
        self._awaiting = None
        self._held = []
