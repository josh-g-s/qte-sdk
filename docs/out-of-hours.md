# Using the SDK outside session hours

**Version:** 0.1

You can use almost all of the SDK when no session is running: connect, authenticate, read the calendar, subscribe, see the closed market and fetch past market data. Only order entry is closed. This guide walks through one run, step by step. Each step links to the [quickstart](quickstart.md) section that explains it in full.

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

Right after it acknowledges you, at any hour, the exchange sends its calendar: every session of the term with its open and close times. It is the only place to learn when the market trades, so never hard-code trading days or hours ([Reading the calendar](quickstart.md#reading-the-calendar)).

```python
from qte_sdk.calendar import next_session

calendar = await session.wait_for_calendar(timeout=5)
if calendar is not None:
    now = session.info.server_time
    closed = [entry for entry in calendar.sessions if entry.close_time <= now]
    upcoming = next_session(calendar, now)
    if closed:
        print("last closed session:", closed[-1].session_date)
    if upcoming is not None:
        print("next session:", upcoming.session_date)
```

The times are exchange timestamps: compare and subtract them only with other exchange timestamps, such as `session.info.server_time`, never with your computer's clock. The session date of the last closed session is what step 5 asks the history service for.

## 3. Subscribe and see the closed market

Outside a session a subscribe is accepted and answered once, with a `SessionState` whose `state` is `CLOSED`. No `Book`, `Trades` or `Mark` arrives until a session opens ([quickstart step 4](quickstart.md#4-subscribe-to-market-data)).

```python
from qte_sdk.contract.v1.common_pb2 import MarketSessionPhase
from qte_sdk.market_data import OfficialClose, SessionState, market_data, subscribe
from qte_sdk.units import to_decimal

await subscribe(session, ["AAPL"])
try:
    async with asyncio.timeout(5):
        async for item in market_data(session):
            if isinstance(item, SessionState):
                print("market session:", MarketSessionPhase.Name(item.state))
            elif isinstance(item, OfficialClose):
                print("official close:", item.instrument, to_decimal(item.value))
except TimeoutError:
    pass
```

The contract also provides an `OfficialClose` for each subscribed instrument that has one, after the `SessionState`. **The exchange does not send it yet.** Until it does, the `CLOSED` state is all you receive, and that is expected, not a fault. Write your code so it works with or without one.

If the state is `OPEN`, a session is under way and this is the live market: stop here, and do not send the order in step 4.

## 4. Send one order and read the reject

Order entry is closed outside a session, so an order message is rejected. Only do this once you have seen the `CLOSED` state in step 3: during a session the same message is a real order. You need a strategy ID registered for your team ([quickstart step 6](quickstart.md#6-place-and-cancel-an-order)).

```python
from qte_sdk.contract.v1.common_pb2 import BUY, LIMIT
from qte_sdk.orders import is_order_event, reason_code_name, request_ref_of, send_new
from qte_sdk.units import to_micros

ref = await send_new(
    session,
    strat_id="my-strategy",  # a strategy ID registered for your team
    instrument="AAPL",
    side=BUY,
    order_type=LIMIT,
    price=to_micros("100.00"),
    size=1,
)
try:
    async with asyncio.timeout(10):  # never wait for ever
        async for event in session:
            if is_order_event(event) and request_ref_of(event.message) == ref:
                if event.type == "reject":
                    print("rejected:", reason_code_name(event.message.reason_code))
                else:  # accepted: the order may now rest, so cancel it (quickstart step 6)
                    print("not rejected:", event.type)
                break
except TimeoutError:
    print("no answer within 10 seconds: check your orders")
```

Every order message is held for the exchange's order delay, currently 150 ms, so the answer comes at least that long after you send. Which reject you see depends on your team and on when you send ([quickstart step 8](quickstart.md#8-values-the-exchange-sets) lists them):

| Reason | When |
|---|---|
| `NO_MARKET_ACCESS` | Your team may not send orders. This is checked first, so such a team sees it at any hour. Only teams on the trading arms send orders. |
| `MARKET_CLOSED` | Before the open, or on a day with no session at all, such as a weekend or an exchange holiday. |
| `RELEASE_AFTER_CLOSE` | After the close, on a day that had a session. |

## 5. Fetch the last closed session

The history service serves the market data of sessions that have closed, at any hour ([quickstart step 10](quickstart.md#10-past-market-data-history)). It has its own address, set as `QTE_HISTORY_URL`, and needs no session. Use the session date from step 2:

```python
from contextlib import aclosing

from qte_sdk.history import HistoryClient, HistoryPending, HistoryUnavailable

client = HistoryClient()  # address from QTE_HISTORY_URL, token from QTE_TOKEN
try:
    states = client.fetch_session_state(closed[-1].session_date)
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
