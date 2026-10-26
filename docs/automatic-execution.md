# Automatic execution

**Version:** 0.26

The exchange can work a Fundamentals pod's tickets itself, with its automatic execution service, instead of sending them to the pod's Execution desk. This page says what changes for a pod and a desk while the service is on, what each SDK release shows of it, and the one mistake to avoid: adding ticket fills to a position of your own. The rules in full are in the [`qte_sdk.tickets`](../qte_sdk/tickets.py) docstring.

## What each release shows

1.5.0 and later releases, 1.5.1 the latest, read everything the service sends: a ticket's `progress` included, they name reason 1714 and the statuses 8 and 9, and they have the replacing cancel that begins an amendment. Teams that have not updated keep working while the service is on: with 1.4.0 or 1.3.0 nothing raises, disconnects or is delivered twice after a reconnect, but each shows less, as the table says. A field a release does not show is not in its Python types at all: reading it, or testing it with `HasField`, raises, so update before your code uses it. Older programs can also act on less: one built on 1.4.0 or 1.3.0 takes a Director's amend (status 9, which it reads as `TICKET_STATUS_UNSPECIFIED`) for the end of the ticket and never follows the replacement, so update and follow it as "A Director's cancel or amend" says. Check yours with `python -m qte_sdk.update`, which says whether a newer release exists and how to install it.

| What the exchange sends | 1.5.0 | 1.4.0 | 1.3.0 |
|---|---|---|---|
| `execution_route`, where a new ticket goes | `is_execution_route(event)`, `LatestTickets.route` | the same as 1.5.0 | an `Unknown` event |
| `ticket_accepted.algorithm` and `.executor` | read them | read them | not shown |
| A `SERVICE` ticket's `ticket_state` | `executor` `SERVICE`, `activity`, `behind_schedule`, `filled_notional` and the rest | the same as 1.5.0 | totals and `status` only; the one sign of a `SERVICE` ticket is an empty `desk` |
| A fill of the service's child order (`execution`) | `origin` `AUTO_EXECUTION`, `parent_ticket_id` set, no `strat_id` | the same as 1.5.0 | `origin` `ORIGIN_UNSPECIFIED`, with `event.unknown_enum_names() == {"origin": "AUTO_EXECUTION"}` |
| Reason 1713, a VWAP ticket refused | `TICKET_ALGORITHM_UNAVAILABLE` | `TICKET_ALGORITHM_UNAVAILABLE` | unnamed; the name is in `event.unknown_enum_names()` |
| `send_ticket` | `algorithm=TWAP` (the default) or `algorithm=VWAP` | the same as 1.5.0 | no `algorithm`: the exchange reads it as TWAP |
| `ticket_state.progress`, a ticket's shares due against shares filled (see "A ticket's progress") | read it | not shown | not shown |
| Reason 1714, `TICKET_SERVICE_OFF`, a ticket or a replacing cancel refused while the route is off | `TICKET_SERVICE_OFF` | unnamed; the name is in `event.unknown_enum_names()` | unnamed; the name is in `event.unknown_enum_names()` |
| `send_ticket_cancel` | a plain cancel, or `replacing=True` for the cancel that begins an amendment (see "Amending a ticket") | plain cancels only | plain cancels only |
| `director_stop_pending_time`, while a Director's cancel waits for the next open, and a cure ticket's `cure_deadline_date` | read them | not shown | not shown |
| Statuses 8 and 9, a Director's cancel or amend (see "A Director's cancel or amend") | `TICKET_CANCELLED_BY_DIRECTOR` and `TICKET_AMENDED_BY_DIRECTOR` | `TICKET_STATUS_UNSPECIFIED`, a stop, with the name in `event.unknown_enum_names()` | `TICKET_STATUS_UNSPECIFIED`, a stop, with the name in `event.unknown_enum_names()` |

A status, activity, origin or reason this SDK does not know decodes as its zero value, never as an error, and keeps the name the exchange sent in `event.unknown_enum_names()`. A ticket whose `status` is anything but `TICKET_WORKING` has stopped.

## Who works a ticket

The exchange's routing when it accepts a ticket decides who works it, not the pod. With `ROUTING_SERVICE` the service works it (`executor` `SERVICE`, no desk); with `ROUTING_DESK` the pod's desk does (`executor` `DESK`, always TWAP). A ticket keeps its executor: a desk's ticket still working when the routing changes stays with the desk.

The service off (`routing_in_force` `ROUTING_DESK`) and the service paused (`paused` true) differ:

- **Off:** a ticket goes to the pod's Execution desk as before (`executor` `DESK`). The Execution arm is to pause, unscored, once the service takes over, so do not count on a desk working it. From the Winter term (26 October) no desk is paired: in a term that pairs no pod with an Execution desk, a ticket received while `ROUTING_SERVICE` is not in force, a cure ticket included, is refused `TICKET_SERVICE_OFF` (1714) with the detail "the automatic execution route is off: no ticket is accepted until it is on", and nothing is logged (Jack's answer G-164 A; qte-platform #2315). A `SERVICE` ticket still working when the route goes off is worked by the service to completion, and a plain cancel of it is taken.
- **Paused:** a new ticket is accepted, and the service catches up when the pause lifts (see "The pause").

Read `routing_in_force` and `paused` before you send.

The routing changes only at a session's open. The pod is sent `execution_route` once on each connection, after the `session_ack`, the calendar and the instruments table and before the ticket states, and again whenever the exchange logs an automatic execution control or a pending one comes into force. It says `routing_in_force`, `routing_next_open` when the routing changes at the next open, `config_version`, and `paused`. A desk is never sent it.

```python
from qte_sdk.contract.v1.common_pb2 import ROUTING_SERVICE
from qte_sdk.tickets import LatestTickets

tickets = LatestTickets()
async for event in session:
    tickets.update(event)
    route = tickets.route
    if route is not None and route.routing_in_force == ROUTING_SERVICE:
        ...  # a ticket sent now is worked by the service
```

## Your position

The pod is sent every fill of the service's child orders as an `execution`, with `origin` `AUTO_EXECUTION`, the ticket in `parent_ticket_id` and no `strat_id`. Each fill is already in the ticket's `filled_shares`: a pod that adds these fills on top of totals it takes from its tickets counts every fill twice.

Take the position from the exchange's own count, `account_state.positions` (`qte_sdk.account`). A reply includes every fill up to its `as_of_report_seq`, and a fill numbered above it is new. To keep the position between queries, pass every event to `qte_sdk.account.AccountReports`, which sorts the fills against the reply so that each is applied once. Query again after a ticket stops all the same: while the service is on, the engine's own cure ticket is worked by the service and nothing of it is sent to anyone, so the pod sees the ticket it replaced stop (`TICKET_CANCELLED_BY_ENGINE`) but never the cure or its fills, and only a new `account_state` shows them. A position also carries across terms, while tickets do not.

```python
from qte_sdk.account import is_account_state, send_account_query
from qte_sdk.tickets import LatestTickets

tickets = LatestTickets()
positions: dict[str, int] = {}
ref = await send_account_query(session)
async for event in session:
    tickets.update(event)  # each ticket's filled_shares, due_shares and status
    if is_account_state(event) and event.message.request_ref == ref:
        positions = {p.instrument: p.quantity for p in event.message.positions}
    # Do not add an `execution` on top of a ticket's filled_shares. To keep positions
    # between queries, use AccountReports; ask again after a ticket stops.
```

On 1.3.0 those fills read `ORIGIN_UNSPECIFIED`, and the same rules hold.

## What a SERVICE ticket's state says

- `activity`: `WORKING`, `POSTING` (a passive child rests), `TAKING` (a child crosses the spread), `WAITING_LIQUIDITY`, `WAITING_LIMIT`, `PARTICIPATION_LIMIT`, `MARKET_CLOSED`, `TRADING_UNAVAILABLE`, `SERVICE_PAUSED` or `MESSAGE_BUDGET`. A name added later decodes as `TICKET_ACTIVITY_UNSPECIFIED`, with the name in `event.unknown_enum_names()`.
- `due_shares`: the shares due at the service's last assessment, 0 before the first. It moves only when the service assesses again, so it can lag the clock.
- `behind_schedule`: the fills were more than one slice behind `due_shares` at the last assessment, or the schedule reached its planned completion with shares unfilled.
- `filled_notional`: the exact sum of quantity times price over the fills; `filled_notional / filled_shares` is the exact average price.

The service's passive child rests as a limit order of the pod for a short wait, and any team's order may fill it. It never appears in `RestingOrders`: follow the ticket in `ticket_state`.

## A ticket's progress

Every ticket's state, whoever works it, carries `progress` (from 1.5.0; see "What each release shows"): the ticket's history of shares due against shares filled, so a program can show how far ahead or behind the schedule a ticket has been, and rebuild it after a reconnect. Each point is a `TicketProgressPoint` with `time` (an exchange timestamp in milliseconds, like `update_time`), `due_shares` and `filled_shares`, both cumulative from the ticket's start. The points are in strictly ascending `time`: one at each tick at or after `accepted_time` while the ticket works (every session's open, every 30 minutes after it inside the session, and the session's close), and an end point at `stopped_time` when the ticket stops, whatever the status. A point's `due_shares` is by the rule of the ticket's own `due_shares` as of its `time` (for a `SERVICE` ticket, the figure of the service's latest step), and its `filled_shares` counts every fill stamped at or before it. Every `ticket_state` carries the whole list, the connect-time set included: each one replaces the list held and is never appended to. The list is empty when the exchange holds no history for the ticket.

```python
state = tickets.get(ticket_id)
if state is not None and state.progress:
    point = state.progress[-1]  # the newest point
    ahead = point.filled_shares - point.due_shares  # below 0: behind the schedule
```

## The pause

When the Directors pause the service, `execution_route.paused` is true, and a working `SERVICE` ticket's `activity` can read `SERVICE_PAUSED`. No child is placed and the resting children are cancelled, but no ticket is cancelled, and a new ticket is still accepted. When the pause lifts, the service catches up on what fell due during it, so a long pause ends in a burst of child orders; `due_shares` and `behind_schedule` show how far behind a ticket is. (A pause because an equity's mark is frozen is different: the service catches up once the mark is no longer frozen.)

## VWAP

Under the service, `send_ticket(..., algorithm=VWAP)` asks for a volume-weighted schedule. The exchange offers VWAP from Monday 26 October 2026. A VWAP ticket for an instrument with no current VWAP profile is refused `TICKET_ALGORITHM_UNAVAILABLE` (1713), and the refusal's `reason_detail` names the case and ends "TWAP is available". The cases include: VWAP not enabled; no profile in force; no age limit set for profiles; a profile too old, one not made before this session, or one that cannot be counted on the calendar; a profile that does not list the instrument or has no valid weights for it; a profile that disables the instrument because it has not passed its quality tests ("the VWAP profile in force (...) disables" the symbol, with the reason), the case you will meet most; and no VWAP schedule that fits the ticket's window. Read the `reason_detail` rather than matching on this list. The refusal is of VWAP alone: the same ticket sent as TWAP is judged on its own.

The exchange never switches a refused VWAP ticket to TWAP: whether to send it again as TWAP is yours to decide, and `ticket_accepted.algorithm` always says what was accepted. A pod's ticket under the automatic route (`ROUTING_SERVICE`) keeps the VWAP it names. A cure ticket, a ticket routed to a desk (`ROUTING_DESK`) and a Director's house ticket are TWAP, and that field shows it.

## Amending a ticket

There is no amend message. To change a ticket, send its cancel with `send_ticket_cancel(..., replacing=True)`, wait for its `ticket_state` to show `TICKET_CANCELLED_BY_POD`, and only then send the new ticket with `replaces_ticket_id` naming the old one. While the route is on, a replacing cancel acts exactly like a plain one.

In a term that pairs no pod with an Execution desk, while `ROUTING_SERVICE` is not in force, the replacement would be refused 1714, so the replacing cancel is refused whole: a `ticket_reject` with `request_kind` `TICKET_REQUEST_CANCEL`, reason `TICKET_SERVICE_OFF` (1714), your `request_ref`, and the detail "the automatic execution route is off: an amendment would lose the ticket, so its cancel is refused". Nothing is logged and the original ticket keeps working, its fills and children standing. Send no replacement. To stop the ticket, cancel it plainly (`replacing` left False), which is never refused for the route. The route check comes last, after the ticket's own checks, so a replacing cancel of no ticket is still `TICKET_NOT_FOUND`.

The flag protects the amendment only at the cancel's instant. Once the cancel is taken, the cancel and the replacement are not atomic: if the replacement is refused for any reason, the old ticket stays cancelled. That includes `TICKET_SERVICE_OFF` (1714) when the route is off by the time the replacement arrives, for example because `ROUTING_DESK` comes into force at the next open first, so send the replacement as soon as the cancel is taken.

## A Director's cancel or amend

A Director may cancel a `SERVICE` ticket: it stops `TICKET_CANCELLED_BY_DIRECTOR`, its fills standing. A cancel logged between sessions takes effect at the next open, and until then the ticket keeps working with `director_stop_pending_time` set.

A Director may also amend a `SERVICE` ticket, in a session only. The ticket stops `TICKET_AMENDED_BY_DIRECTOR`, its fills standing, and at the same instant the exchange accepts a replacement for your pod: a new ticket with its own `ticket_id`, whose `replaces_ticket_id` names the amended one. You are sent the amended ticket's `ticket_state` and then the replacement's (`TICKET_WORKING`), and no `ticket_accepted`, since your pod did not send it. Follow the replacement from then on: it is your working ticket in that instrument. Neither the reason nor the Director is sent.

A pod's `ticket_reject` never carries `TICKET_DESK_NOT_EXECUTION` (1717), `TICKET_RAISE_PENDING` (1718), `TICKET_CURE_NOT_AMENDABLE` (1719) or `TICKET_AMEND_AT_OR_BELOW_FILLED` (1720): they answer a Director. 1718 is the one that touches a pod: it refuses a Director's cancel of a ticket whose urgency your pod raised with `send_ticket_urgency` at the same instant, so that the raise already acknowledged to you is not undone. Do nothing on it: your raise stands and the ticket keeps working. If the Director cancels again later, the ticket stops `TICKET_CANCELLED_BY_DIRECTOR` as above.

A house ticket, one a Director places for the house rather than for a pod, carries `house_instruction_id` and no pod, and stops `TICKET_CANCELLED_BY_HOUSE` when a Director cancels it. A pod is never sent one.

## Execution desks

The Execution arm is paused, so no desk trades (see "Who works a ticket"). The rules below are those of the exchange's contract for when desks work: a desk works only the tickets routed to it (`executor` `DESK`) and is sent nothing about a ticket the service works: no `ticket_state`, no fills. Its own children's fills name their ticket in `parent_ticket_id`, and the orders the service places never appear in a desk's `RestingOrders`. In a term that sets the exchange's `urgency_clock_stops` (the Winter term does), a `DESK` ticket's schedule clock stops while its instrument is halted or the Directors declare an outage, as a `SERVICE` ticket's does: its `due_shares`, and each progress point's, do not grow during them, though the points still come at every tick. It gets no `revised_completion_time`, and its `planned_completion_time` is not changed (qte-platform #2676).

## A checklist for your agent

- Pass every event to one `LatestTickets` and read the route from `tickets.route`.
- Treat any `status` other than `TICKET_WORKING` as a stop, including a name this SDK does not know. After `TICKET_AMENDED_BY_DIRECTOR`, follow the new ticket whose `replaces_ticket_id` names the stopped one.
- Read a ticket's `progress` from the newest state only: each `ticket_state` carries the whole list, so never append one to another.
- Take a pod's position from `account_state.positions`, never adding fills on top of ticket totals; keep it between queries with `AccountReports`, and query again after a ticket stops.
- On a 1713 reject, resend the ticket as TWAP.
- Amend with a cancel sent `replacing=True`, then the replacement once the cancel's `ticket_state` shows `TICKET_CANCELLED_BY_POD`. On a 1714 reject of that cancel, send no replacement: the ticket is still working; cancel it plainly to stop it.
- Before sending, check `routing_in_force` is `ROUTING_SERVICE`: while the service is off a ticket goes to the desk, and from the Winter term it is refused `TICKET_SERVICE_OFF` (1714). While `paused` is true a ticket is accepted and caught up when the pause lifts.
- Read `paused` and `activity` before deciding a ticket is stuck.
