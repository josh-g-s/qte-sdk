"""Fundamentals tickets.

A Fundamentals pod sends no orders of its own. It sends its assigned Execution desk a
ticket, the instruction the desk works, and the desk works it with child orders that name
the ticket in `parent_ticket_id` (see `qte_sdk.orders.send_new`).

A pod sends three messages:

- `ticket` (`send_ticket`): a new ticket in one instrument. An ordinary ticket carries a
  hard execution limit price; a cure ticket carries none and is flagged `cure`. There is
  one working ticket per instrument, and a ticket never takes the position across zero.
  There is no amend: to change a ticket, cancel it, then send a new one naming it in
  `replaces_ticket_id`.
- `ticket_cancel` (`send_ticket_cancel`): cancel a working ordinary ticket the pod sent.
  The pod can never cancel a cure ticket.
- `ticket_urgency` (`send_ticket_urgency`): raise the urgency of the pod's working cure
  ticket. It may only go up.

    ref = await send_ticket(session, instrument="AAPL", side=BUY, shares=500,
                            limit_price=to_micros("201.50"), urgency=URGENCY_MEDIUM,
                            thesis_id="t-aapl", thesis_version=1)
    async for event in session:
        if is_ticket_event(event) and ticket_request_ref_of(event.message) == ref:
            ...

The exchange answers each with a `ticket_accepted` or a `ticket_reject`, and reports a
ticket's state in `ticket_state`:

- `ticket_accepted` echoes the `request_ref` and names the `ticket_id` the exchange
  assigned, or the ticket cancelled or changed. For a new ticket it also carries the
  decision price, the acceptance time and the planned completion time.
- `ticket_reject` echoes the `request_ref` when the exchange could read it, and gives a
  `TicketReasonCodes.TicketReasonCode`, a separate set from the order reasons. Name one
  with `ticket_reason_code_name`.
- `ticket_state` is the whole state of one ticket. It is sent to the pod that originated
  it and to its desk whenever the ticket changes. A pod is sent its own tickets only.
  `LatestTickets` keeps the newest state of each.

A `ticket_reject` refuses one message and changes nothing: it never stops a ticket. A
ticket stops only by a `ticket_state` whose `status` is not `TICKET_WORKING`, and a
stopped ticket never works again. Only a stopped ticket's state carries `stopped_time`
and `stopped_mark`, the official mark when it stopped; read them with `stopped_time_of`
and `stopped_mark_of`. A stop with no valid mark to record sends a mark of 0, which
`stopped_mark_of` returns as None, the same as a mark that is absent.

None of the three answers carries a `report_seq`, and a resume replays none of them.
Instead, once on each connection of the pod or desk, straight after the `session_ack`,
the calendar and the instruments table, the exchange sends one whole `ticket_state` for
every ticket of the current term, working or stopped, in ascending order of `ticket_id`.
It does not send the set again after a resume. If your program resumes (as a
`ReconnectingSession` does), the session keeps the states it reads while it waits for
the `resume_ack` and delivers them to your loop in order, so pass every event to
`LatestTickets` from the first. The set replaces whatever you held before: a ticket
missing from it is no longer one to follow. `LatestTickets` forgets every ticket on a
`Disconnected` so that the next connection's set rebuilds it.

A desk's fill of a child order carries the ticket's id in `Execution.parent_ticket_id`
(`parent_ticket_of`); the pod follows its ticket's fills in `ticket_state`. What becomes
of the desk's child orders still resting when a ticket stops depends on why it stopped:

- the pod cancelled it, or the mark moved beyond its limit: each child is cancelled with
  an `order_cancelled` whose reason is `PARENT_STOPPED`;
- the term's final close expired it: the close itself cancels the day's resting orders,
  so each child is cancelled `SESSION_CLOSE`;
- the exchange cancelled it (`TICKET_CANCELLED_BY_ENGINE`): each child is cancelled with
  one of the cancellation reasons (the 1800 band), but the contract does not yet say
  which, so do not expect `PARENT_STOPPED` there.

Every send checks what it can before sending, and raises `ValueError` or `TypeError`
and sends nothing if a check fails: `request_ref` is 1 to 32 bytes of UTF-8, `instrument`
and `thesis_id` at most 32 bytes, none containing NUL; a ticket id is a string of decimal
digits; `notes` is at most 280 characters with no NUL; an ordinary ticket has a
`limit_price` and a cure ticket has none. Whether a ticket is accepted (its thesis, its
size against the position, its limit against the wall, the schedule against the term) is
for the exchange to judge. Prices are integers in micro-dollars, and `shares` is a whole
number of shares.
"""

from collections.abc import Iterator
from typing import TypeGuard

from google.protobuf.message import Message

from qte_sdk.connection import Disconnected, Event, Received
from qte_sdk.contract.v1.common_pb2 import (
    TICKET_WORKING,
    URGENCY_HIGH,
    URGENCY_LOW,
    URGENCY_MEDIUM,
    Side,
    TicketReasonCodes,
    TicketUrgency,
)
from qte_sdk.contract.v1.order_entry_pb2 import CancelTicket, RaiseTicketUrgency, SubmitTicket
from qte_sdk.contract.v1.order_events_pb2 import (
    Execution,
    TicketAccepted,
    TicketReject,
    TicketState,
)
from qte_sdk.orders import Sender, _id, _ref, _side, _ticket_id

__all__ = [
    "TICKET_EVENT_TYPES",
    "URGENCY_HIGH",
    "URGENCY_LOW",
    "URGENCY_MEDIUM",
    "LatestTickets",
    "TicketEvent",
    "is_ticket_event",
    "parent_ticket_of",
    "send_ticket",
    "send_ticket_cancel",
    "send_ticket_urgency",
    "stopped_mark_of",
    "stopped_time_of",
    "ticket_reason_code_name",
    "ticket_request_ref_of",
]

# The envelope `type` tokens of the exchange's answers about tickets.
TICKET_EVENT_TYPES = frozenset({"ticket_accepted", "ticket_reject", "ticket_state"})

TicketEvent = TicketAccepted | TicketReject | TicketState

_URGENCIES = (URGENCY_HIGH, URGENCY_MEDIUM, URGENCY_LOW)
_NOTES_MAX_CHARS = 280


def _urgency(urgency: TicketUrgency) -> TicketUrgency:
    if urgency not in _URGENCIES:
        raise ValueError(
            f"urgency must be URGENCY_HIGH, URGENCY_MEDIUM or URGENCY_LOW, got {urgency!r}"
        )
    return urgency


async def send_ticket(
    conn: Sender,
    *,
    instrument: str,
    side: Side,
    shares: int,
    urgency: TicketUrgency,
    limit_price: int | None = None,
    cure: bool = False,
    thesis_id: str | None = None,
    thesis_version: int | None = None,
    notes: str | None = None,
    replaces_ticket_id: str | None = None,
    request_ref: str | None = None,
) -> str:
    """Send `ticket`: a Fundamentals pod's instruction to its desk. Returns its
    `request_ref`, which the `ticket_accepted` or `ticket_reject` answering it echoes.

    An ordinary ticket needs `limit_price`, the hard execution limit in micro-dollars. A
    cure ticket (`cure=True`) has none, and names no `replaces_ticket_id`. `side` is the
    order's side: the exchange works out from the pod's position whether the ticket
    increases or reduces it. An opening or increasing ticket names the filed thesis that
    covers it in `thesis_id` and `thesis_version`, which go together; a reducing ticket
    may leave both out. `notes` is free text for the desk, at most 280 characters.
    """
    ref = _ref(request_ref)
    _id("instrument", instrument, required=False)
    _side(side)
    _urgency(urgency)
    if not isinstance(cure, bool):
        raise TypeError("cure must be a bool")
    if cure:
        if limit_price is not None:
            raise ValueError("a cure ticket carries no limit_price")
        if replaces_ticket_id is not None:
            raise ValueError("a cure ticket names no replaces_ticket_id")
    elif limit_price is None:
        raise ValueError("an ordinary ticket needs a limit_price")
    if (thesis_id is None) != (thesis_version is None):
        raise ValueError("thesis_id and thesis_version go together: give both or neither")
    if thesis_id is not None:
        _id("thesis_id", thesis_id, required=False)
    if notes is not None:
        if not isinstance(notes, str):
            raise TypeError("notes must be a str")
        if len(notes) > _NOTES_MAX_CHARS:
            raise ValueError(f"notes must be at most {_NOTES_MAX_CHARS} characters")
        if "\0" in notes:
            raise ValueError("notes must not contain the NUL character")
    if replaces_ticket_id is not None:
        _ticket_id("replaces_ticket_id", replaces_ticket_id)
    msg = SubmitTicket(
        request_ref=ref, instrument=instrument, side=side, shares=shares, urgency=urgency
    )
    if limit_price is not None:
        msg.limit_price = limit_price
    if cure:
        msg.cure = True
    if thesis_id is not None and thesis_version is not None:
        msg.thesis_id = thesis_id
        msg.thesis_version = thesis_version
    if notes is not None:
        msg.notes = notes
    if replaces_ticket_id is not None:
        msg.replaces_ticket_id = replaces_ticket_id
    await conn.send("ticket", msg)
    return ref


async def send_ticket_cancel(
    conn: Sender, *, ticket_id: str, request_ref: str | None = None
) -> str:
    """Send `ticket_cancel`: cancel a working ordinary ticket this pod sent. Returns
    its `request_ref`. The desk's child orders are cancelled with it; fills stand."""
    ref = _ref(request_ref)
    msg = CancelTicket(request_ref=ref, ticket_id=_ticket_id("ticket_id", ticket_id))
    await conn.send("ticket_cancel", msg)
    return ref


async def send_ticket_urgency(
    conn: Sender,
    *,
    ticket_id: str,
    urgency: TicketUrgency,
    request_ref: str | None = None,
) -> str:
    """Send `ticket_urgency`: raise the urgency of the pod's working cure ticket. Returns
    its `request_ref`. The new urgency must be higher than the current one, which the
    exchange checks."""
    ref = _ref(request_ref)
    msg = RaiseTicketUrgency(
        request_ref=ref,
        ticket_id=_ticket_id("ticket_id", ticket_id),
        urgency=_urgency(urgency),
    )
    await conn.send("ticket_urgency", msg)
    return ref


def is_ticket_event(event: Event) -> TypeGuard[Received]:
    """Whether `event` is a decoded ticket answer, one of `TICKET_EVENT_TYPES`. Its
    `message` is then one of `TicketEvent`."""
    return isinstance(event, Received) and event.type in TICKET_EVENT_TYPES


def ticket_request_ref_of(message: Message) -> str | None:
    """The `request_ref` a ticket answer echoes, or None when it carries none.

    `ticket_accepted` always echoes one, and a `ticket_reject` does unless the exchange
    could not read it or it was empty. `ticket_state` never carries one."""
    if isinstance(message, TicketAccepted):
        return message.request_ref
    if isinstance(message, TicketReject):
        return message.request_ref if message.HasField("request_ref") else None
    return None


def ticket_reason_code_name(code: int) -> str:
    """The name of a `ticket_reject` reason code, or its number as text when this SDK
    does not know it. A reason token this SDK does not recognise decodes as
    `TICKET_REASON_CODE_UNSPECIFIED` (0); the event's `unknown_enum_names()` keeps it."""
    try:
        return TicketReasonCodes.TicketReasonCode.Name(code)
    except ValueError:
        return str(code)


def parent_ticket_of(execution: Execution) -> str | None:
    """The id of the ticket a desk's filled child order works, or None when the filled
    order is not a child of a ticket."""
    return execution.parent_ticket_id if execution.HasField("parent_ticket_id") else None


def stopped_time_of(state: TicketState) -> int | None:
    """When the ticket stopped, an exchange timestamp, or None while it is working."""
    return state.stopped_time if state.HasField("stopped_time") else None


def stopped_mark_of(state: TicketState) -> int | None:
    """The official mark when the ticket stopped, in micro-dollars, or None.

    None while the ticket is working, and also for a stop that had no valid mark to
    record, which the exchange sends as a mark of 0."""
    if not state.HasField("stopped_mark") or state.stopped_mark == 0:
        return None
    return state.stopped_mark


class LatestTickets:
    """The newest `ticket_state` of each ticket, by `ticket_id`.

    Pass it every event, or every `ticket_state` message, with `update`. A state replaces
    the one held for its ticket unless it is older by `update_time`. A `Disconnected` (from
    a `ReconnectingSession`) forgets every ticket, because the states the exchange sends
    on the next connection are the whole set: they carry the connection time and rebuild
    what is held. A session you open yourself never yields `Disconnected`, so start a new
    `LatestTickets`, or call `clear()`, for each session you open. A ticket is working
    while its `status` is `TICKET_WORKING`; any other status, including one this SDK does
    not know, is a stop, and a stopped ticket never works again.

    A ticket state missed in a `SeqGap` is not sent again until the ticket next changes
    or you reconnect, so after a gap a ticket held here may be out of date.

        tickets = LatestTickets()
        async for event in session:
            tickets.update(event)
            for state in tickets.working():
                ...
    """

    def __init__(self) -> None:
        self._states: dict[str, TicketState] = {}

    def update(self, item: Event | Disconnected | TicketState | object) -> bool:
        """Take one event or `ticket_state`. Returns True if it changed what is held."""
        if isinstance(item, Disconnected):
            changed = bool(self._states)
            self._states.clear()
            return changed
        if isinstance(item, Received):
            if item.type != "ticket_state":
                return False
            item = item.message
        if not isinstance(item, TicketState):
            return False
        held = self._states.get(item.ticket_id)
        if held is not None and item.update_time < held.update_time:
            return False
        self._states[item.ticket_id] = item
        return True

    def get(self, ticket_id: str) -> TicketState | None:
        """The newest state of `ticket_id`, or None if none has arrived."""
        return self._states.get(ticket_id)

    def working(self) -> list[TicketState]:
        """The tickets still working, in order of `ticket_id`."""
        return [s for s in self if s.status == TICKET_WORKING]

    def clear(self) -> None:
        """Forget every ticket, for example before reading a new session you opened."""
        self._states.clear()

    def __len__(self) -> int:
        return len(self._states)

    def __iter__(self) -> Iterator[TicketState]:
        """Every ticket's newest state, in order of `ticket_id` (shorter ids first, as
        decimal numbers sort)."""
        ordered = sorted(self._states, key=lambda ticket_id: (len(ticket_id), ticket_id))
        return iter([self._states[ticket_id] for ticket_id in ordered])
