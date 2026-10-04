# Developing your algo

**Version:** 0.1

This guide is one path from an idea to a program trading on the exchange: explore past market data, run your strategy's loop on a replay of a past session, keep its order logic separate so you can test it, check your setup with the smoke test, then try it on the exchange during a session. Most of it works at any hour, so you can do it while the market is closed.

Install the SDK and set `QTE_URL` and `QTE_TOKEN` first ([quickstart steps 1 and 2](quickstart.md#1-install)). Steps 1 and 2 also need the history service's address, which the course team gives you, set as `QTE_HISTORY_URL` in the environment (it is never read from `.env`). The smoke test in step 4 is a worked example, so it needs a clone of this repository.

## 1. Explore past market data

The history service serves the market data of every session that has closed: exactly what the live feed published to everyone, message for message, as the same `Book`, `Trades`, `Mark` and `SessionState` classes ([quickstart step 10](quickstart.md#10-past-market-data-history)). Use it to learn how an instrument behaves before you write any trading logic. This counts one session's book changes for one instrument and finds the widest spread of the wall:

```python
import asyncio
from contextlib import aclosing

from qte_sdk.connection import DecodeFailed, Unknown
from qte_sdk.history import HistoryClient
from qte_sdk.market_data import Book
from qte_sdk.units import to_decimal


async def main() -> None:
    client = HistoryClient()  # address from QTE_HISTORY_URL, token from QTE_TOKEN
    changes, widest = 0, 0
    async with aclosing(client.fetch("2026-01-05", "AAPL", "book")) as items:
        async for item in items:
            if isinstance(item, (Unknown, DecodeFailed)):
                print("could not use a message:", item)
            elif isinstance(item, Book):
                changes += 1
                if item.bid_levels and item.ask_levels:
                    spread = item.ask_levels[0].price - item.bid_levels[0].price
                    widest = max(widest, spread)
    print(changes, "books; widest wall spread", to_decimal(widest))


asyncio.run(main())
```

- Take session dates from the exchange's calendar, never from a list of your own: [Outside session hours](out-of-hours.md#2-read-the-calendar) shows how to find the last session that closed. A day with no session raises `HistoryUnavailable`.
- A book is published only when it has changed, so the stream holds the session's first book and then only the changes. The book in force at any moment is the last one before it.
- A `Book` shows two kinds of depth: the wall (`bid_levels`, `ask_levels`) and participants' resting orders (`student_bid_levels`, `student_ask_levels`).
- `fetch(date, instrument, channel)` is one instrument and one of `book`, `trades` or `mark`; `fetch_session_state(date)` is the market session state; `fetch_range` fetches several instruments and channels over a span of dates in one download.

## 2. Run your strategy's loop on a replay

`qte_sdk.replay.replay` takes one closed session's books, trades, marks and market session state for the instruments you name and merges them into one stream in time order, like the live feed. It yields the same classes as `market_data(session)`, so the code that handles market data in your live loop runs on a replay unchanged. Put that code in one place that both can feed:

```python
from qte_sdk.books import LatestBooks
from qte_sdk.contract.v1.common_pb2 import MarketSessionPhase
from qte_sdk.market_data import SessionState


class Strategy:
    """What the strategy knows, built only from the messages it is given."""

    def __init__(self, instrument: str) -> None:
        self.instrument = instrument
        self.books = LatestBooks()
        self.open = False
        self.now: int | None = None  # the exchange's time, from the latest session state

    def on_market_data(self, item: object) -> None:
        self.books.update(item)
        if isinstance(item, SessionState):
            self.open = item.state == MarketSessionPhase.OPEN
            self.now = item.grid_time
```

Offline, feed it a replay:

```python
import asyncio
from contextlib import aclosing

from qte_sdk.connection import DecodeFailed, Unknown
from qte_sdk.history import HistoryClient
from qte_sdk.replay import replay


async def offline(session_date: str) -> None:
    strategy = Strategy("AAPL")
    client = HistoryClient()  # address from QTE_HISTORY_URL, token from QTE_TOKEN
    async with aclosing(replay(client, session_date, ["AAPL"])) as items:
        async for item in items:
            if isinstance(item, (Unknown, DecodeFailed)):
                print("could not use a message:", item)
            strategy.on_market_data(item)
            ...  # look at what your logic decides (step 3); nothing is sent


asyncio.run(offline("2026-01-05"))
```

During a session, feed it the live feed from your one loop over the session, which also brings your order events:

```python
from qte_sdk.market_data import as_market_data, subscribe
from qte_sdk.orders import is_order_event
from qte_sdk.session import open_session


async def live() -> None:
    strategy = Strategy("AAPL")
    async with await open_session() as session:
        await subscribe(session, ["AAPL"])
        async for event in session:
            item = as_market_data(event)
            if item is not None:
                strategy.on_market_data(item)
            elif is_order_event(event):
                ...  # your orders: accepted, rejected, resting, filled, cancelled
            ...  # decide, and send with qte_sdk.orders
```

What to know about a replay:

- **Time comes from the messages.** Take "now" from the messages' own times, as `Strategy.now` does above, never from your machine's clock: during a session a `SessionState` arrives at every grid point, live or replayed. A replay runs as fast as your loop reads it unless you pass `speed`: `speed=1.0` replays at the pace the session ran, `speed=10.0` ten times as fast.
- **The order within one grid point is not promised.** A replay puts the messages of one instant in a fixed order: books, then trades, then marks, then the session state, each by instrument. The contract does not say what order the live feed uses there, so treat the messages of one grid point as arriving together and do not depend on their order.
- **Each instrument and channel is its own download**, held open while the replay runs and read only as far as it needs, so a whole day never has to fit in memory. Name only the instruments you need.
- **Stop it with `aclosing`**, as above: leaving the block, by `break` or an error, closes every download.
- **Messages it cannot use are passed on.** `Unknown` (a type this SDK does not know) and `DecodeFailed` come where they were in their stream. Report them; a book may have been lost with one.
- **Errors come from the history service**, as `fetch` raises them: `HistoryUnavailable` for a day with no session or an instrument it does not know, `HistoryNotClosed` for a session that has not closed, `HistoryPending` for data not ready yet.

`examples/replay_book.py` replays one instrument's book and prints the best bid and ask as it changes:

```sh
python examples/replay_book.py --date 2026-01-05 --instrument AAPL
```

## 3. Keep the order logic separate, and test it

Write the decisions your strategy makes as plain functions of what it knows, apart from the code that sends orders. A function that takes a `Book` and returns the prices to quote can be tested with pytest on books you build yourself, in a fraction of a second, with no exchange and no history service:

```python
# strategy.py
from qte_sdk.market_data import Book


def quote_prices(book: Book, tick: int) -> tuple[int, int] | None:
    """A bid and an ask one tick inside the wall, or None when there is no room."""
    if not book.bid_levels or not book.ask_levels:
        return None
    bid = book.bid_levels[0].price + tick
    ask = book.ask_levels[0].price - tick
    return (bid, ask) if bid < ask else None
```

```python
# test_strategy.py
from qte_sdk.market_data import Book, WallLevel
from qte_sdk.units import to_micros

from strategy import quote_prices

TICK = to_micros("0.01")  # your own choice, passed in: the exchange does not send it


def book(bid: str, ask: str) -> Book:
    return Book(
        instrument="AAPL",
        bid_levels=[WallLevel(price=to_micros(bid), size=100)],
        ask_levels=[WallLevel(price=to_micros(ask), size=100)],
    )


def test_it_quotes_one_tick_inside_the_wall():
    assert quote_prices(book("199.95", "200.05"), TICK) == (
        to_micros("199.96"),
        to_micros("200.04"),
    )


def test_it_does_not_quote_when_the_wall_has_no_room():
    assert quote_prices(book("199.99", "200.00"), TICK) is None


def test_it_does_not_quote_a_one_sided_book():
    assert quote_prices(Book(instrument="AAPL"), TICK) is None
```

Prices are whole numbers of micro-dollars; build them with `to_micros` and never with `float` ([quickstart step 5](quickstart.md#5-prices-and-sizes)). Run the same functions on a replay to see what they decide over a whole session, and to find the books they did not expect: an empty or one-sided wall, an instrument whose condition is not `LIVE`, a quiet stretch with no new book at all.

The code that sends orders and follows them stays in the live loop. Test it against the exchange, as step 5 describes. `examples/quote_both_sides.py` is a full example of that part: it places quotes, follows them through their order events and cancels them when it stops.

## 4. Check your setup with the smoke test

Before you run anything on the exchange, check your setup end to end with `examples/smoke_test.py`, from a clone of this repository:

```sh
python examples/smoke_test.py --instruments AAPL MSFT
```

It prints one line for each check, `PASS`, `FAIL` or `SKIP` with a reason: where the SDK finds your token and the exchange address (it shows neither), the connection, the calendar, the market data of each instrument, your account, whether any message was missed, and, when `QTE_HISTORY_URL` is set, the history service. It exits with status 0 when no check failed. By default it sends no orders.

With `--place-test-order --strat-id ID --tick DOLLARS`, during a session, it also places one limit buy of one share and cancels it once the exchange reports it resting. The order is a real order and can fill. In a scored session it places nothing unless you also pass `--allow-scored`. Run `python examples/smoke_test.py --help` for the details.

## 5. Try it on the exchange during a session

A replay cannot show you what happens to your orders. Only the exchange can, and only during a session: read the hours from the calendar, and trade only while `SessionState.state` is `OPEN`.

- Start on the practice exchange the course team gives you, or a test exchange on your own machine. Point your program at the exchange you compete on only when you mean it to trade for real.
- Start small: one instrument, small sizes, and a bounded run that cancels your orders when it stops.
- Handle every reject by its reason. Rejects are where you meet the order delay, the minimum resting time, the price collar and your message budgets. The exchange sets all of these and can change them, so act on what it reports rather than building them into your code ([quickstart step 8](quickstart.md#8-values-the-exchange-sets)).
- Your orders are part of the market there: once one rests, it is in every participant's book, and others can trade with it.
- Once the session has closed, replay it next to the log of your live run to see the market your program was trading in.

## What the replay is not

The replay is market data only:

- It sends no orders and takes none. There is nothing in it to send an order to.
- It fills nothing. No order is matched, and no order event, `accepted`, `reject`, `execution` or `order_cancelled`, ever comes from it.
- It keeps no positions, cash or profit and loss.
- It shows the market as it was published to everyone that day, which never included any order of yours. Had your orders been there, they would have changed what others did, so the replay cannot tell you whether an order would have filled or what a strategy would have earned.

The SDK has no fill simulator. Use the replay to check that your loop runs over a whole session, handles every message it is given and makes the decisions you expect, and use the exchange to see what happens to your orders.
