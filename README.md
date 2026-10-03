# qte-sdk

The Python client SDK for the Queen's Tower Exchange (QTE). Competing teams use it to connect their strategies to the exchange: authenticate with a team token, subscribe to market data, and send and manage orders.

This repository contains only participant-facing material. Exchange internals (the matching engine, risk parameters, the synthetic book model, the bot fleet) live elsewhere and are intentionally not here.

## Requirements

- Python 3.11 or later.
- The address of the exchange you are trading on, your team's practice token and, for any program that sends orders, a strategy ID registered for your team. Contact the Head of Technology, Joshua, for all three. Treat the token like a password: never put it in a source file, a notebook, a screenshot or a repository.

## Install

Install the SDK into your own project's virtual environment:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install "git+https://github.com/josh-g-s/qte-sdk"
```

This installs the `qte_sdk` package only, not the [worked examples](examples/). Clone this repository only to run or read the examples; you do not need a clone to use the SDK.

## A first program

Put the exchange address and your token in a `.env` file in your project folder, the folder you run your programs from. First make sure git will never commit it, and create it readable only by you:

```sh
printf '\n.env\n' >> .gitignore
touch .env && chmod 600 .env
```

Then open `.env` in your editor and add the two lines, with the address and token from the course team:

```sh
QTE_URL=<the exchange address from the course team>
QTE_TOKEN=<your team token>
```

The SDK reads `QTE_URL` and `QTE_TOKEN` from `./.env` itself, only when they are not passed in or set as real environment variables, which always take precedence. For the token the order is `token=`, then `QTE_TOKEN`, then the file named by `QTE_TOKEN_FILE`, then `.env`. It refuses a `.env` holding the token that other users can read, and warns if git does not ignore it. Step 2 of the [quickstart](docs/quickstart.md) explains the details and the alternatives: environment variables, or a private token file named by `QTE_TOKEN_FILE`.

Then run this. It opens a session, subscribes to one instrument and prints the best bid and ask as the book updates, for ten seconds. A book shows two kinds of depth: the wall (`bid_levels`, `ask_levels`) and participants' resting orders (`student_bid_levels`, `student_ask_levels`), each best price first, so the best bid and ask are taken across both.

```python
import asyncio

from qte_sdk.market_data import Book, market_data, subscribe
from qte_sdk.session import open_session
from qte_sdk.units import to_decimal


def best(book: Book) -> tuple[int | None, int | None]:
    """The best bid and ask across the wall and participants' resting orders."""
    bids = [levels[0].price for levels in (book.bid_levels, book.student_bid_levels) if levels]
    asks = [levels[0].price for levels in (book.ask_levels, book.student_ask_levels) if levels]
    return (max(bids) if bids else None, min(asks) if asks else None)


async def main() -> None:
    # The address and token come from .env or the environment; or pass them to open_session.
    async with await open_session() as session:
        print("connected as", session.info.team)
        await subscribe(session, ["XOM"])
        try:
            async with asyncio.timeout(10):
                async for item in market_data(session):
                    if isinstance(item, Book):
                        bid, ask = best(item)
                        if bid is not None and ask is not None:
                            print(item.instrument, to_decimal(bid), to_decimal(ask))
        except TimeoutError:
            pass


asyncio.run(main())
```

Outside market hours the exchange still accepts the connection and the subscription, but sends the market's closed state instead of a live book, so this prints nothing after "connected as". [Outside session hours](docs/out-of-hours.md) shows what you can do then.

## Documentation

| Where | What |
|---|---|
| [Quickstart](docs/quickstart.md) | Step by step: connect, read the calendar and market data, place and cancel an order, read order events, what each reject means, and fetch past market data |
| [Outside session hours](docs/out-of-hours.md) | What works when no session is running, with a runnable walk-through: the calendar, the closed market, an order's reject, past market data |
| [Worked examples](examples/) | Runnable programs: print the book, quote both sides and manage the quotes, take liquidity with a market order, see the closed market outside a session |
| [Development guide](docs/development.md) | Working on the SDK itself: setup, checks and CI, the vendored contract |

The main modules, each documented in its docstrings:

| Module | What |
|---|---|
| [`qte_sdk.session`](qte_sdk/session.py) | `open_session`: authenticate and get a `Session` you can send on and iterate |
| [`qte_sdk.market_data`](qte_sdk/market_data.py) | `subscribe`, `unsubscribe`, `market_data`: the book, trades, marks and market state |
| [`qte_sdk.books`](qte_sdk/books.py) | `LatestBooks`: the latest book of each instrument, since a book is sent only when it changes |
| [`qte_sdk.calendar`](qte_sdk/calendar.py) | `next_open`, `next_close`: when the market next opens and closes, from the exchange's calendar |
| [`qte_sdk.history`](qte_sdk/history.py) | `HistoryClient`: the published market data of sessions that have closed |
| [`qte_sdk.orders`](qte_sdk/orders.py) | `send_new`, `send_cancel`, `send_amend`, `send_mass_cancel`, and helpers for order events |
| [`qte_sdk.resting`](qte_sdk/resting.py) | `RestingOrders`: your team's resting orders, built only from exchange events |
| [`qte_sdk.reconnect`](qte_sdk/reconnect.py) | `ReconnectingSession`: reconnects and resubscribes after a dropped connection |
| [`qte_sdk.units`](qte_sdk/units.py) | `to_decimal`, `to_micros`: exact conversion between prices and micro-dollars |

## Things to know before you trade

- **Every order message is delayed** on its way into the exchange, cancels and amends included; the delay is currently 150 ms. The delay, the minimum time an order must rest, the price collar and your message budgets are set by the exchange; never hard-code them.
- **Prices are whole numbers of micro-dollars** ($199.97 is `199_970_000`). Convert with `qte_sdk.units`; never use `float` for prices.
- **There is no order ID.** Your team's orders are addressed by instrument, side and price, and your team holds at most one resting order at each price.
- **One market-data feed for everyone.** Market data is published on a fixed 100 ms grid: during a session the market state at every grid point, and an instrument's book only when it has changed, so keep the last book you received for each instrument.
- **Not every team sends orders.** Teams on the trading arms and Execution teams send orders; an order from a team without market access is rejected with a reason code that says so.

## Status

Working today: session authentication, the market calendar, market data, order entry and order events, the resting-order view, reconnect, past market data from the history service, the quickstart and the worked examples. Planned: the closed market's official close price, which the exchange does not send yet, a query for your team's positions and cash, and heartbeats with session resume. Progress is tracked in [the issues](https://github.com/josh-g-s/qte-sdk/issues).

## Licence

MIT. See [LICENSE](LICENSE).
