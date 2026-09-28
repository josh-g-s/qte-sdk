# qte-sdk

The Python client SDK for the Queen's Tower Exchange (QTE). Competing teams use it to connect their strategies to the exchange: authenticate with a team token, subscribe to market data, and send and manage orders.

This repository contains only participant-facing material. Exchange internals (the matching engine, risk parameters, the synthetic book model, the bot fleet) live elsewhere and are intentionally not here.

## Requirements

- Python 3.11 or later.
- The address of the exchange you are trading on, and your team's practice token. The course team gives you both. Treat the token like a password: never put it in a source file, a notebook, a screenshot or a repository.

## Install

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install "git+https://github.com/josh-g-s/qte-sdk"
```

## A first program

Put the exchange address and your token in the environment. Read the token without echoing it, so it stays off your screen and out of your shell history:

```sh
export QTE_URL="<the exchange address from the course team>"
read -rs QTE_TOKEN && export QTE_TOKEN   # paste the token, then press Enter
```

Then run this. It opens a session, subscribes to one instrument and prints the best bid and ask as the book updates, for ten seconds.

```python
import asyncio
import os

from qte_sdk.market_data import Book, market_data, subscribe
from qte_sdk.session import open_session
from qte_sdk.units import to_decimal


async def main() -> None:
    # The token comes from QTE_TOKEN; pass token=... to supply it another way.
    async with await open_session(os.environ["QTE_URL"]) as session:
        print("connected as", session.info.team)
        await subscribe(session, ["XOM"])
        try:
            async with asyncio.timeout(10):
                async for item in market_data(session):
                    if isinstance(item, Book) and item.bid_levels and item.ask_levels:
                        bid, ask = item.bid_levels[0].price, item.ask_levels[0].price
                        print(to_decimal(bid), to_decimal(ask))
        except TimeoutError:
            pass


asyncio.run(main())
```

Outside market hours the exchange still accepts the connection and the subscription, but sends the market's closed state instead of a live book, so this prints nothing after "connected as".

## Documentation

| Where | What |
|---|---|
| [Quickstart](docs/quickstart.md) | Step by step: connect, read market data, place and cancel an order, read order events, and what each reject means |
| [Worked examples](examples/) | Runnable programs: print the book, quote both sides and manage the quotes, take liquidity with a market order |
| [Development guide](docs/development.md) | Working on the SDK itself: setup, checks and CI, the vendored contract |

The main modules, each documented in its docstrings:

| Module | What |
|---|---|
| [`qte_sdk.session`](qte_sdk/session.py) | `open_session`: authenticate and get a `Session` you can send on and iterate |
| [`qte_sdk.market_data`](qte_sdk/market_data.py) | `subscribe`, `unsubscribe`, `market_data`: the book, trades, marks and market state |
| [`qte_sdk.orders`](qte_sdk/orders.py) | `send_new`, `send_cancel`, `send_amend`, `send_mass_cancel`, and helpers for order events |
| [`qte_sdk.resting`](qte_sdk/resting.py) | `RestingOrders`: your team's resting orders, built only from exchange events |
| [`qte_sdk.reconnect`](qte_sdk/reconnect.py) | `ReconnectingSession`: reconnects and resubscribes after a dropped connection |
| [`qte_sdk.units`](qte_sdk/units.py) | `to_decimal`, `to_micros`: exact conversion between prices and micro-dollars |

## Things to know before you trade

- **Every order message is delayed 150 ms** on its way into the exchange, cancels and amends included. The delay, the minimum time an order must rest, the price collar and your message budgets are set by the exchange; never hard-code them.
- **Prices are whole numbers of micro-dollars** ($199.97 is `199_970_000`). Convert with `qte_sdk.units`; never use `float` for prices.
- **There is no order ID.** Your team's orders are addressed by instrument, side and price, and your team holds at most one resting order at each price.
- **One market-data feed for everyone.** The book, trades and market state arrive on a fixed 100 ms grid.
- **Not every team sends orders.** Teams on the trading arms send orders; an order from a team without market access is rejected with a reason code that says so.

## Status

Working today: session authentication, market data, order entry and order events, the resting-order view, reconnect, the quickstart and the worked examples. Planned: the market calendar, handling of the closed market's official close price, a query for your team's positions and cash, access to past market data, and heartbeats with session resume. Progress is tracked in [the issues](https://github.com/josh-g-s/qte-sdk/issues).

## Licence

MIT. See [LICENSE](LICENSE).
