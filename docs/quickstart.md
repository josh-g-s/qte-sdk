# Quickstart

**Version:** 0.2

This guide takes you from a fresh checkout to a program that connects to the exchange, reads market data, places an order and cancels it. It then points you at three worked examples in `examples/` that you can run and adapt.

## What you need

- Python 3.11 or later.
- A copy of this repository.
- The address of the exchange you are trading on. The course team tells you the practice exchange's address; a test exchange you run on your own machine is usually `ws://127.0.0.1:8080/ws`.
- Your team's practice token. The course team gives it to you. Treat it like a password: never put it in a source file, a notebook, a screenshot or a repository.
- A strategy ID registered for your team, for any program that sends orders.

## 1. Install

From the root of your copy of this repository:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

## 2. Set the exchange address and your token

The SDK reads your token from the `QTE_TOKEN` environment variable, and the examples read the exchange address from `QTE_URL`. Read the token without echoing it, so it stays out of your screen and your shell history:

```sh
export QTE_URL=ws://127.0.0.1:8080/ws   # or the address the course team gives you
read -rs QTE_TOKEN && export QTE_TOKEN   # paste the token, then press Enter
```

The SDK never logs your token or puts it in an exception message.

## 3. Open a session

Everything in the SDK is `async`. A session is an authenticated connection: `open_session` connects, sends your token and waits for the exchange to acknowledge it.

```python
import asyncio
import os

from qte_sdk.session import open_session


async def main() -> None:
    session = await open_session(os.environ["QTE_URL"])  # the token comes from QTE_TOKEN
    async with session:
        print("team:", session.info.team)
        print("unscored session:", session.info.unscored)


asyncio.run(main())
```

`session.info.unscored` is `True` when nothing in this session counts towards any score.

If the exchange refuses the session, `open_session` raises `SessionRejected` (from `qte_sdk.connection`), whose `reason_name` says why. With no token set it raises `MissingToken` before connecting.

Two rules about sessions:

- **Send on the session and read from it.** Every send function takes the session. Iterate the session itself, not its connection, so that you also receive anything the exchange sent before it acknowledged you.
- **Read from one place.** Have one loop read the session's events and do everything from there. Two loops reading the same session each get only some of the events.

## 4. Subscribe to market data

The snippets from here on run inside the `async with session:` block of step 3.

```python
from qte_sdk.market_data import Book, subscribe, market_data
from qte_sdk.units import to_decimal

await subscribe(session, ["AAPL"])
async for item in market_data(session):  # runs until you stop it
    if isinstance(item, Book) and item.bid_levels and item.ask_levels:
        bid, ask = item.bid_levels[0], item.ask_levels[0]
        print(
            f"{item.instrument}: {to_decimal(bid.price)} x {bid.size} | "
            f"{to_decimal(ask.price)} x {ask.size}"
        )
```

There is **one conflated market-data feed, the same for every participant**. Book, trades and the market session state are published on a fixed 100 ms grid; the mark is published on its own, slower grid. A `Book` is the state of one instrument at the end of an interval, not a stream of individual changes. It shows two kinds of depth: `bid_levels` and `ask_levels` are the wall ladder, best price first, and `student_bid_levels` and `student_ask_levels` are the orders participants have resting, one entry per price, with no identity attached.

`market_data` yields `Book`, `Trades`, `Mark` and `SessionState` messages, a `Reject` if a subscription is refused (an unknown instrument, for example), and two warnings:

- `SeqGap`: messages were missed, so anything you built from them may be wrong.
- `DecodeFailed`: a message could not be decoded.

`market_data` skips everything that is not market data, including your order events. When you also trade, loop over the session yourself and sort each event with `as_market_data(event)` and `is_order_event(event)`, as in the next step.

## 5. Prices and sizes

Every price on the wire is a whole number of **micro-dollars** in a 64-bit integer: $199.97 is `199_970_000`. Sizes are whole shares. Never use `float` for prices. Convert with `qte_sdk.units`:

```python
from qte_sdk.units import to_decimal, to_micros

to_micros("199.97")  # 199970000
to_decimal(199_970_000)  # Decimal('199.970000')
```

## 6. Place and cancel an order

There is **no order ID** on the wire. Your team's orders are addressed by **instrument, side and price**, and your team holds **at most one resting order per instrument, side and price**, across all of its strategies. So to cancel an order you name its level, not an ID. A second `new` at a level where your team already rests is rejected (`DUPLICATE_ORDER_AT_LEVEL`); to change its size, send an amend.

```python
from qte_sdk.contract.v1.common_pb2 import BUY, LIMIT
from qte_sdk.market_data import as_market_data
from qte_sdk.orders import (
    is_order_event,
    reason_code_name,
    request_ref_of,
    send_cancel,
    send_new,
)
from qte_sdk.units import to_micros

STRATEGY = "my-strategy"  # a strategy ID registered for your team
INSTRUMENT = "AAPL"
price = to_micros("199.97")
new_ref = await send_new(
    session,
    strat_id=STRATEGY,
    instrument=INSTRUMENT,
    side=BUY,
    order_type=LIMIT,
    price=price,
    size=10,
)
cancel_ref = None


def is_this_order(message, price_field):
    """Whether an order_state, execution or order_cancelled is about the order above.
    Your teammates' orders arrive on the same stream, so check every field."""
    return (
        message.strat_id == STRATEGY
        and message.instrument == INSTRUMENT
        and message.side == BUY
        and getattr(message, price_field) == price
    )


try:
    async with asyncio.timeout(10):  # never wait for ever
        async for event in session:
            if as_market_data(event) is not None or not is_order_event(event):
                continue  # a real program handles market data here too
            message = event.message
            ref = request_ref_of(message)
            if event.type == "reject" and ref is not None and ref in (new_ref, cancel_ref):
                print("rejected:", reason_code_name(message.reason_code))
                break  # if it was the cancel, the order may still rest
            if event.type == "accepted" and ref is not None and ref in (new_ref, cancel_ref):
                print("accepted:", "new" if ref == new_ref else "cancel")
            elif event.type == "order_state" and is_this_order(message, "price"):
                print("resting:", message.remaining_size)
                if cancel_ref is None:
                    # Cancel it by naming its level: instrument, side and price.
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

Every send returns the `request_ref` it put on the message. The `accepted` or `reject` that answers the message echoes it, and so does each `order_cancelled` that your cancel, amend or mass cancel causes. Match on it with `request_ref_of`.

An amend changes the orders at one level: `send_amend(session, instrument=..., side=..., price=..., new_size=...)`. `new_size` is the new total remaining size, not an amount to add. `send_mass_cancel(session)` cancels **every order your team has on the exchange**, including those of your teammates' strategies.

Because a cancel or amend names a level, not an order, it acts on whichever of your team's orders rests at that level when the exchange applies it, after the order delay. If your order fills in the meantime and a teammate's strategy enters an order at the same price, your cancel removes theirs. Agree within your team who trades which instruments or prices.

A send checks its identifiers before anything leaves your machine: `strat_id` and `request_ref` must be 1 to 32 bytes of UTF-8 and `instrument` at most 32 bytes. A send that breaks this raises `ValueError`.

## 7. Read your order events

The exchange sends these events about your team's own orders:

| Event | Meaning |
|---|---|
| `accepted` | Your message was applied. Echoes your `request_ref`. |
| `reject` | Your message was refused. `reason_code` says why; use `reason_code_name` to print it. |
| `order_state` | The state of one of your resting orders: its instrument, side, price, strategy and remaining size. |
| `execution` | A fill: price, size, remaining size, fee or rebate. A fill of a limit order carries the order's price as `order_price`; a fill of a market order has none. |
| `order_cancelled` | One of your orders left the book unfilled, with the reason (your cancel, a mass cancel, the close, and others). |
| `risk_notice` | A risk warning for your team. |

`qte_sdk.resting.RestingOrders` keeps a view of your team's resting orders built only from these events. Read its docstring for what it cannot know. In particular, after an amend that changes an order's price, the entry at the old price stays in the view, so cancel and re-enter instead of amending the price if you rely on it.

## 8. Values the exchange sets

**Every order message you send (new, cancel, amend and mass cancel) is held by the exchange for its order delay before it is applied. The delay is currently 150 ms.** An `accepted` therefore arrives at least that long after you send, and the book you acted on is at least that old by the time your order reaches it. Plan for it rather than around it.

The delay, the minimum time an order must rest before you may cancel or amend it, the price collar and your team's message budgets are all set by the exchange and can change. Do not build them into your code as constants. Instead:

- act on what the exchange reports: cancel or amend an order after its `order_state` arrives, not after a fixed sleep;
- keep your message rate well inside your budgets, and handle the rejects that say you went over one.

Some rejects you are likely to meet while learning (the exchange's rules decide exactly when each applies):

| Reason | What happened |
|---|---|
| `MARKET_CLOSED` | The market session is not open. `SessionState` says when it is. |
| `MIN_REST_VIOLATION` | You cancelled or amended an order too soon after sending it. |
| `PRICE_COLLAR` | The price is outside the price collar. |
| `DUPLICATE_ORDER_AT_LEVEL` | Your team already has an order at that instrument, side and price. |
| `MESSAGE_BUDGET_EXCEEDED`, `BURST_CAP_EXCEEDED`, `NEW_ORDER_CAP_EXCEEDED` | You sent too many messages. Slow down. |

A reason from a newer contract than your SDK knows decodes as `REASON_CODE_UNSPECIFIED`; `event.unknown_enum_names()` returns the name the exchange sent.

## 9. If the connection drops

Iterating a session ends normally when the exchange closes the connection, and raises `websockets.exceptions.ConnectionClosedError` if it drops. A session from `open_session` does not reconnect, and neither do the worked examples: open a new session yourself. Your orders may still be resting after a drop, so check before you trade again.

`qte_sdk.reconnect.ReconnectingSession` does reconnect for you: it opens a new session, authenticates and subscribes again, and delivers a `Disconnected` event first. It cannot recover what you missed. The exchange does not yet resume a session, so fills, order events and market data sent while you were disconnected are not recovered. An order in flight when the connection dropped may or may not have reached the exchange, and the SDK never sends it again. A `RestingOrders` view you pass it, or that follows it, is marked incomplete and stays incomplete after the reconnect, because no event reports the orders already resting when a session starts. Treat `Disconnected` as the moment your positions, resting orders and book became uncertain.

## 10. Worked examples

Each example reads `QTE_URL` and `QTE_TOKEN` from the environment, runs for a bounded time and then stops by itself, prints every reject with its reason, and exits with status 0 when it has run cleanly. The instrument comes from `--instrument` or `QTE_INSTRUMENT`, and the examples that send orders take your strategy ID from `--strat-id` or `QTE_STRAT_ID`. Run any of them with `--help` for its options.

| Example | What it shows |
|---|---|
| `examples/print_book.py` | Connect, subscribe and print the book, trades, mark and market session state. Sends no orders. Stops after `--seconds` or `--max-messages`. |
| `examples/quote_both_sides.py` | Rest a limit order on each side, inside the wall's best prices, and manage them: cancel and re-enter when the wall moves, amend the size back up after a partial fill, re-enter after a full fill. Cancels its own orders when `--seconds` are up. |
| `examples/take_liquidity.py` | Send one market order once the book shows the side it trades against, and report its fills. Stops when the order is finished or after `--seconds`. |

```sh
python examples/print_book.py --instrument AAPL --seconds 10
python examples/quote_both_sides.py --instrument AAPL --strat-id my-strategy --seconds 30
python examples/take_liquidity.py --instrument AAPL --strat-id my-strategy --side buy --size 1
```

The examples are for learning the SDK, not strategies: they make no attempt to make money.
