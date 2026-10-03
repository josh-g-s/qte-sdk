# Using the SDK outside session hours

**Version:** 0.3

You can use almost all of the SDK when no session is running: connect, authenticate, read the calendar, subscribe, see the closed market, query your account and fetch past market data. Only order entry is closed. This guide walks through one run, step by step. Each step links to the [quickstart](quickstart.md) section that explains it in full.

`examples/out_of_hours.py` runs steps 1 to 3 for you, and sends no orders:

```sh
python examples/out_of_hours.py --instrument AAPL
```

Install the SDK and set `QTE_URL` and `QTE_TOKEN` first ([quickstart steps 1 and 2](quickstart.md#1-install)). For the worked example you also need a clone of this repository.

## 1. Connect and authenticate

This is the same at any hour. `open_session` connects, sends your token and waits for the exchange to acknowledge it ([quickstart step 3](quickstart.md#3-open-a-session)). The snippets below run inside the `async with session:` block.

```python
import asyncio
import os

from qte_sdk.session import open_session


async def main() -> None:
    session = await open_session(os.environ["QTE_URL"])  # the token comes from QTE_TOKEN
    async with session:
        print("team:", session.info.team)
        ...  # the snippets below go here


asyncio.run(main())
```

## 2. Read the calendar

Right after it acknowledges you, at any hour, the exchange sends its calendar: every session of the term with its open and close times. It is the only source of the term's full schedule, so never hard-code trading days or hours ([Reading the calendar](quickstart.md#reading-the-calendar)).

```python
from qte_sdk.calendar import next_session

last_closed = None  # the date of the last session that has closed, if any
calendar = await session.wait_for_calendar(timeout=5)
if calendar is None:
    print("no calendar from this exchange")
else:
    now = session.info.server_time
    closed = [entry for entry in calendar.sessions if entry.close_time <= now]
    if closed:
        last_closed = closed[-1].session_date
        print("last closed session:", last_closed)
    upcoming = next_session(calendar, now)
    if upcoming is not None:
        print("next session:", upcoming.session_date)
```

The times are exchange timestamps: compare and subtract them only with other exchange timestamps, such as `session.info.server_time`, never with your computer's clock. `last_closed` is the session date step 5 asks the history service for. It stays `None` with no calendar, or before the term's first session has closed.

## 3. Subscribe and see the closed market

Outside a session a subscribe is accepted and answered once, with a `SessionState` whose `state` is `CLOSED`. No `Book`, `Trades` or `Mark` arrives until a session opens ([quickstart step 4](quickstart.md#4-subscribe-to-market-data)).

```python
from qte_sdk.contract.v1.common_pb2 import MarketSessionPhase
from qte_sdk.market_data import (
    OfficialClose,
    SessionState,
    market_data,
    subscribe,
    until_next_open,
)
from qte_sdk.units import to_decimal

await subscribe(session, ["AAPL"])
try:
    async with asyncio.timeout(5):
        async for item in market_data(session):
            if isinstance(item, SessionState):
                print("market session:", MarketSessionPhase.Name(item.state))
                if item.HasField("next_session_date"):
                    wait = until_next_open(item, session.info.server_time)
                    print("next session:", item.next_session_date, "opens in", wait)
            elif isinstance(item, OfficialClose):
                print("official close:", item.instrument, to_decimal(item.value))
except TimeoutError:
    pass
```

On this out-of-hours reply the `SessionState` fields mean:

- `session_date`, `open_time` and `close_time` name the most recent session that has closed or, before the running exchange process has closed any session, the next scheduled one.
- `grid_time` is not a publication time: it equals `close_time`, so it can be in the future. Never use it as "now".
- `next_session_date`, `next_open_time` and `next_close_time` name the next scheduled session. They are set together, only on this reply, and are absent when the term has no later session, or from an exchange that predates them, so your code must work without them.

For "now", use the exchange's current time: `session.info.server_time`, the time your session was acknowledged, or a later exchange timestamp. `until_next_open(state, now)` gives `next_open_time - now` in the exchange's time units, or `None` when the next session is not named. These fields are a convenience; the calendar from step 2 is still the full schedule.

The contract also provides an `OfficialClose` for each subscribed instrument that has one, after the `SessionState`. **The exchange does not send it yet.** Until it does, the `CLOSED` state is all you receive, and that is expected, not a fault. Write your code so it works with or without one.

If the state is `OPEN`, a session is under way and this is the live market: stop here, and do not send the order in step 4.

## 4. Send one order and read the reject

Order entry is closed outside a session, so an order message is rejected. Only do this once you have seen the `CLOSED` state in step 3: during a session the same message is a real order. You need a strategy ID registered for your team ([quickstart step 6](quickstart.md#6-place-and-cancel-an-order)).

If a session opens between step 3 and your order, the order is accepted and can rest. The snippet then cancels it, once its `order_state` shows it resting, by naming its level, as in quickstart step 6.

```python
from qte_sdk.contract.v1.common_pb2 import BUY, LIMIT
from qte_sdk.orders import (
    is_order_event,
    reason_code_name,
    request_ref_of,
    send_cancel,
    send_new,
)
from qte_sdk.units import to_micros

STRATEGY, INSTRUMENT = "my-strategy", "AAPL"  # a strategy ID registered for your team
price = to_micros("100.00")
ref = await send_new(
    session,
    strat_id=STRATEGY,
    instrument=INSTRUMENT,
    side=BUY,
    order_type=LIMIT,
    price=price,
    size=1,
)
cancel_ref = None


def is_this_order(message, price_field):
    """Whether an order_state, execution or order_cancelled is about the order above."""
    return (
        message.strat_id == STRATEGY
        and message.instrument == INSTRUMENT
        and message.side == BUY
        and getattr(message, price_field) == price
    )


try:
    async with asyncio.timeout(10):  # never wait for ever
        async for event in session:
            if not is_order_event(event):
                continue
            message = event.message
            answers = request_ref_of(message)
            if event.type == "reject" and answers == ref:
                print("rejected:", reason_code_name(message.reason_code))
                break  # the expected outcome outside a session
            if event.type == "reject" and cancel_ref is not None and answers == cancel_ref:
                print("cancel rejected:", reason_code_name(message.reason_code))
                break  # the order may still rest: check your orders
            if event.type == "order_state" and is_this_order(message, "price"):
                if cancel_ref is None:  # a session has opened and the order rests
                    print("accepted and resting: cancelling it")
                    cancel_ref = await send_cancel(
                        session, instrument=INSTRUMENT, side=BUY, price=price
                    )
            elif event.type == "execution" and is_this_order(message, "order_price"):
                print("filled", message.fill_size, "left", message.remaining_size)
                if message.remaining_size == 0:
                    break
            elif event.type == "order_cancelled" and is_this_order(message, "price"):
                print("cancelled:", reason_code_name(message.reason_code))
                break
except TimeoutError:
    print("no final outcome within 10 seconds: check your orders")
```

An order message the exchange takes in is held for its order delay, currently 150 ms, before it is applied, but some rejects are sent as soon as the message arrives. So the reject can come at once or after the delay: wait for it either way. Which reject you see depends on your team and on when you send ([quickstart step 8](quickstart.md#8-values-the-exchange-sets) lists them):

| Reason | When |
|---|---|
| `NO_MARKET_ACCESS` | Your team may not send orders. This is checked first, so such a team sees it at any hour. Only teams on the trading arms and Execution teams send orders. |
| `MARKET_CLOSED` | Before the open, or on a day with no session at all, such as a weekend or an exchange holiday. |
| `RELEASE_AFTER_CLOSE` | After the close, on a day that had a session. |

Your account query is not an order message, so it works outside a session too. Send `send_account_query(session)` from `qte_sdk.account` and read the `account_state` that answers it in your one loop. Outside a session it values your positions at each instrument's latest official close, and `session_date` names the last session with an official close, even between terms, when positions carried over from the term before are returned too ([Query your account](quickstart.md#query-your-account)). It also needs an exchange that serves the query.

## 5. Fetch the last closed session

The history service serves the market data of sessions that have closed, at any hour ([quickstart step 10](quickstart.md#10-past-market-data-history)). It has its own address, set as `QTE_HISTORY_URL`, and needs no session. Use the session date from step 2:

```python
from contextlib import aclosing

from qte_sdk.history import HistoryClient, HistoryPending, HistoryUnavailable

client = HistoryClient()  # address from QTE_HISTORY_URL, token from QTE_TOKEN
if last_closed is None:
    print("no closed session to fetch")
else:
    try:
        states = client.fetch_session_state(last_closed)
        async with aclosing(states) as items:  # closes the download when you stop early
            async for item in items:
                if isinstance(item, SessionState):
                    print(item.grid_time, MarketSessionPhase.Name(item.state))
                    break  # the first one is enough here
    except HistoryUnavailable:
        print("there is no such data")
    except HistoryPending as error:
        print("not ready yet; ask again in", error.retry_after, "seconds")
```

`client.fetch(date, instrument, "book")` gives that session's books the same way, as the live feed published them: only when they changed.

## What this guide does not cover

- Trading. During a session, start from the [quickstart](quickstart.md) and the worked examples.
- When the next session opens. Read it from the calendar each time; do not copy dates or hours from here or anywhere else.
