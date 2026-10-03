# qte-sdk

The Python client SDK for the Queen's Tower Exchange (QTE). Competing teams use it to connect their strategies to the exchange: authenticate with a team token, subscribe to market data, and send and manage orders.

This repository contains only participant-facing material. Exchange internals (the matching engine, risk parameters, the synthetic book model, the bot fleet) live elsewhere and are intentionally not here.

## Requirements

- Python 3.11 or later.
- The address of the exchange you are trading on, and your team's practice token. The course team gives you both. Treat the token like a password: never put it in a source file, a notebook, a screenshot or a repository.

## Install

Install the SDK into your own project's virtual environment:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install "git+https://github.com/josh-g-s/qte-sdk"
```

This installs the `qte_sdk` package only, not the [worked examples](examples/). Clone this repository only to run or read the examples; you do not need a clone to use the SDK.

## A first program

Put the exchange address and your token in the environment. Read the token without echoing it, so it stays off your screen and out of your shell history: run the second line, paste the token (nothing is shown) and press Enter.

```sh
export QTE_URL="<the exchange address from the course team>"
read -rs QTE_TOKEN && export QTE_TOKEN
```

An exported `QTE_TOKEN` lasts only for that shell and the programs it starts. A new terminal does not have it, and closing the terminal loses it, so set it again in each new terminal.

To keep the token across terminal sessions, the recommended way is a file readable only by you, outside any repository, with `QTE_TOKEN_FILE` set to its path in your shell profile. The SDK reads the token from that file when `QTE_TOKEN` is unset or empty, so unset any old `QTE_TOKEN`, which would otherwise take precedence. Step 2 of the [quickstart](docs/quickstart.md) gives the exact commands, and the alternatives.

Then run this. It opens a session, subscribes to one instrument and prints the best bid and ask as the book updates, for ten seconds. A book shows two kinds of depth: the wall (`bid_levels`, `ask_levels`) and participants' resting orders (`student_bid_levels`, `student_ask_levels`), each best price first, so the best bid and ask are taken across both.

```python
import asyncio
import os

from qte_sdk.market_data import Book, market_data, subscribe
from qte_sdk.session import open_session
from qte_sdk.units import to_decimal


def best(book: Book) -> tuple[int | None, int | None]:
    """The best bid and ask across the wall and participants' resting orders."""
    bids = [levels[0].price for levels in (book.bid_levels, book.student_bid_levels) if levels]
    asks = [levels[0].price for levels in (book.ask_levels, book.student_ask_levels) if levels]
    return (max(bids) if bids else None, min(asks) if asks else None)


async def main() -> None:
    # The token comes from QTE_TOKEN or QTE_TOKEN_FILE; pass token=... to supply it another way.
    async with await open_session(os.environ["QTE_URL"]) as session:
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
- **Not every team sends orders.** Teams on the trading arms send orders; an order from a team without market access is rejected with a reason code that says so.

## Status

Working today: session authentication, the market calendar, market data, order entry and order events, the resting-order view, reconnect, past market data from the history service, the quickstart and the worked examples. Planned: the closed market's official close price, which the exchange does not send yet, a query for your team's positions and cash, and heartbeats with session resume. Progress is tracked in [the issues](https://github.com/josh-g-s/qte-sdk/issues).

## Licence

MIT. See [LICENSE](LICENSE).
