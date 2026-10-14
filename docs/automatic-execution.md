# Automatic execution

**Version:** 0.21

The exchange can work a Fundamentals pod's tickets itself, with its automatic execution service, instead of sending them to the pod's Execution desk. This page says what changes for a pod and a desk while the service is on, what each SDK release shows of it, and the one mistake to avoid: adding ticket fills to a position of your own. The rules in full are in the [`qte_sdk.tickets`](../qte_sdk/tickets.py) docstring.

## What each release shows

Both releases keep working while the service is on: nothing raises, disconnects or is delivered twice after a reconnect. 1.4.0 can read everything the service sends; 1.3.0 can read the tickets' totals and status but not the service's own fields. Check yours with `python -m qte_sdk.update`. 1.4.0 is the next release: until that command names it as the latest, it is not yet released, and only `main` has what this page says of it.

| What the exchange sends | 1.4.0 | 1.3.0 |
|---|---|---|
| `execution_route`, where a new ticket goes | `is_execution_route(event)`, `LatestTickets.route` | an `Unknown` event |
| `ticket_accepted.algorithm` and `.executor` | read them | not shown |
| A `SERVICE` ticket's `ticket_state` | `executor` `SERVICE`, `activity`, `behind_schedule`, `filled_notional` and the rest | totals and `status` only; the one sign of a `SERVICE` ticket is an empty `desk` |
| A fill of the service's child order (`execution`) | `origin` `AUTO_EXECUTION`, `parent_ticket_id` set, no `strat_id` | `origin` `ORIGIN_UNSPECIFIED`, with `event.unknown_enum_names() == {"origin": "AUTO_EXECUTION"}` |
| Reason 1713, a VWAP ticket refused | `TICKET_ALGORITHM_UNAVAILABLE` | unnamed; the name is in `event.unknown_enum_names()` |
| `send_ticket` | `algorithm=TWAP` (the default) or `algorithm=VWAP` | no `algorithm`: the exchange reads it as TWAP |

Only `main`, not yet any release, names reason 1714, `TICKET_SERVICE_OFF` (a ticket or a replacing cancel refused while the route is off), and has `send_ticket_cancel(..., replacing=True)` for the cancel that begins an amendment (see "Amending a ticket"). 1.4.0 and 1.3.0 show 1714 unnamed, with the name in `event.unknown_enum_names()`, and send only plain cancels.

A status, activity, origin or reason this SDK does not know decodes as its zero value, never as an error, and keeps the name the exchange sent in `event.unknown_enum_names()`. A ticket whose `status` is anything but `TICKET_WORKING` has stopped.

## Who works a ticket

The exchange's routing when it accepts a ticket decides who works it, not the pod. With `ROUTING_SERVICE` the service works it (`executor` `SERVICE`, no desk); with `ROUTING_DESK` the pod's desk does (`executor` `DESK`, always TWAP). A ticket keeps its executor: a desk's ticket still working when the routing changes stays with the desk.

The service off (`routing_in_force` `ROUTING_DESK`) and the service paused (`paused` true) differ:

- **Off:** a ticket goes to the pod's Execution desk as before (`executor` `DESK`). The Execution arm is to pause, unscored, once the service takes over, so do not count on a desk working it, and the 15 October term has no fallback. From the Winter term (26 October) no desk is paired: in a term that pairs no pod with an Execution desk, a ticket received while `ROUTING_SERVICE` is not in force, a cure ticket included, is refused `TICKET_SERVICE_OFF` (1714) with the detail "the automatic execution route is off: no ticket is accepted until it is on", and nothing is logged (Jack's answer G-164 A; qte-platform #2315). A `SERVICE` ticket still working when the route goes off is worked by the service to completion, and a plain cancel of it is taken.
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

## The pause

When the Directors pause the service, `execution_route.paused` is true, and a working `SERVICE` ticket's `activity` can read `SERVICE_PAUSED`. No child is placed and the resting children are cancelled, but no ticket is cancelled, and a new ticket is still accepted. When the pause lifts, the service catches up on what fell due during it, so a long pause ends in a burst of child orders; `due_shares` and `behind_schedule` show how far behind a ticket is. (A pause because an equity's mark is frozen is different: the service catches up once the mark is no longer frozen.)

## VWAP

Under the service, `send_ticket(..., algorithm=VWAP)` asks for a volume-weighted schedule. While the instrument has no VWAP profile the ticket is refused `TICKET_ALGORITHM_UNAVAILABLE` (1713): send it again as TWAP. A ticket that goes to a desk, and a cure ticket, are TWAP whatever they name.

## Amending a ticket

There is no amend message. To change a ticket, send its cancel with `send_ticket_cancel(..., replacing=True)`, wait for its `ticket_state` to show `TICKET_CANCELLED_BY_POD`, and only then send the new ticket with `replaces_ticket_id` naming the old one. While the route is on, a replacing cancel acts exactly like a plain one.

In a term that pairs no pod with an Execution desk, while `ROUTING_SERVICE` is not in force, the replacement would be refused 1714, so the replacing cancel is refused whole: a `ticket_reject` with `request_kind` `TICKET_REQUEST_CANCEL`, reason `TICKET_SERVICE_OFF` (1714), your `request_ref`, and the detail "the automatic execution route is off: an amendment would lose the ticket, so its cancel is refused". Nothing is logged and the original ticket keeps working, its fills and children standing. Send no replacement. To stop the ticket, cancel it plainly (`replacing` left False), which is never refused for the route. The route check comes last, after the ticket's own checks, so a replacing cancel of no ticket is still `TICKET_NOT_FOUND`.

The flag protects the amendment only at the cancel's instant. Once the cancel is taken, the cancel and the replacement are not atomic: if the replacement is refused for any reason, the old ticket stays cancelled. That includes `TICKET_SERVICE_OFF` (1714) when the route is off by the time the replacement arrives, for example because `ROUTING_DESK` comes into force at the next open first, so send the replacement as soon as the cancel is taken.

## Handover

In a term whose configuration provides a fallback, the Directors can hand a desk's ticket over to the exchange: its `executor` turns `ENGINE` and its fills come with `origin` `FALLBACK`, treated like the service's above. The 15 October term has no fallback, so no ticket is handed over in it.

## Execution desks

The Execution arm is paused, so no desk trades (see "Who works a ticket"). The rules below are those of the exchange's contract for when desks work: a desk works only the tickets routed to it (`executor` `DESK`) and is sent nothing about a ticket the service works: no `ticket_state`, no fills. Its own children's fills name their ticket in `parent_ticket_id`, and the orders the service places never appear in a desk's `RestingOrders`.

## A checklist for your agent

- Pass every event to one `LatestTickets` and read the route from `tickets.route`.
- Treat any `status` other than `TICKET_WORKING` as a stop, including a name this SDK does not know.
- Take a pod's position from `account_state.positions`, never adding fills on top of ticket totals; keep it between queries with `AccountReports`, and query again after a ticket stops.
- On a 1713 reject, resend the ticket as TWAP.
- Amend with a cancel sent `replacing=True`, then the replacement once the cancel's `ticket_state` shows `TICKET_CANCELLED_BY_POD`. On a 1714 reject of that cancel, send no replacement: the ticket is still working; cancel it plainly to stop it.
- Before sending, check `routing_in_force` is `ROUTING_SERVICE`: while the service is off a ticket goes to the desk, and from the Winter term it is refused `TICKET_SERVICE_OFF` (1714). While `paused` is true a ticket is accepted and caught up when the pause lifts.
- Read `paused` and `activity` before deciding a ticket is stuck.
