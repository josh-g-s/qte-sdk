# qte-sdk

The Python client SDK for the Queen's Tower Exchange (QTE). Competing teams use it to connect their strategies to the exchange: authenticate with a team token, subscribe to market data, and send and manage orders.

This repository contains only participant-facing material. Exchange internals (the matching engine, risk parameters, the synthetic book model, the bot fleet) live elsewhere and are intentionally not here.

## Requirements

- Python 3.11 or later.
- The address of the exchange you are trading on, your team's practice token and, for any program that sends orders, a strategy ID registered for your team. Contact the Head of Technology, Joshua, for all three. Treat the token like a password: never put it in a source file, a notebook, a screenshot or a repository.

## Install

Install the SDK into your own project's virtual environment. On macOS and Linux:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install "git+https://github.com/josh-g-s/qte-sdk"
```

On Windows, first install Python 3.11 or later from python.org or with `winget install Python.Python.3.12`, then open a new terminal. Type `py`, not `python3`: on Windows `python3` is often only a stub that offers the Microsoft Store, even once Python is installed. (`python` works too if the installer added Python to your PATH.) In PowerShell:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install "git+https://github.com/josh-g-s/qte-sdk"
```

In cmd, activate with `.venv\Scripts\activate.bat` instead. If PowerShell says running scripts is disabled, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once and activate again.

The `git+https` install needs git. If you do not have it, either install git (on Windows, `winget install Git.Git`, then open a new terminal) or install a release from its zip, which needs no git:

```sh
pip install https://github.com/josh-g-s/qte-sdk/archive/refs/tags/v1.2.0.zip
```

That is the latest release, v1.2.0, as this is written. The [changelog](CHANGELOG.md) lists each release, and the update check below says when a newer one is out.

If you use a coding agent, such as Claude Code or Codex, have it read [AGENTS.md](AGENTS.md) first, the guidance for coding agents. It comes with the SDK from 1.2.0 (and in an install of `main`), where `python -m qte_sdk.agents` prints it. [`llms.txt`](llms.txt) points agents to it too.

This installs the `qte_sdk` package only, not the [worked examples](examples/); you do not need them to use the SDK. To run or read them, clone this repository or, without git, download the source zip of the release you installed (the address above, or the repository's Releases or Tags page on GitHub) and unzip it, so the examples match your SDK. A zip of `main` may be newer than the release you installed. Then copy its `examples` folder into your project folder, the one that holds your `.env` (below), and run the examples from the project folder: the SDK reads `.env` only from the folder you run in.

To see whether a newer release is out, run:

```sh
python -m qte_sdk.update
```

It says whether your SDK is the latest release and, if not, prints the command that updates it, in a line that starts `QTE-UPDATE-AVAILABLE` and says when the update is a recommended one, and why; after an install from a release zip, that command installs the latest release's zip, so it needs no git either. It exits 0 when your SDK is current, 1 when a newer release is out and 2 when it cannot tell, as when GitHub cannot be reached. An install it cannot trace to the repository, such as an editable install of a local copy, is never called current: it gets 1 when its version is behind the latest release and 2 otherwise. Each release is listed in the [changelog](CHANGELOG.md), and [`releases.json`](releases.json) marks the ones that are recommended updates. Opening a session also runs this check in the background, at most once a day, and logs that line as a warning when a newer release is out; set `QTE_UPDATE_CHECK=0` to turn it off (see the [quickstart](docs/quickstart.md)). To install one release and stay on it, name its tag:

```sh
pip install "git+https://github.com/josh-g-s/qte-sdk@v1.0.0"
```

## A first program

Put the exchange address and your token in a `.env` file in your project folder, the folder you run your programs from. The SDK's setup helper asks for both, reads the token without showing it, and writes the file. On macOS and Linux it makes the file readable only by you. On Windows it cannot, so before you run it, make sure your project folder is private: inside a folder only you can open, such as your user profile.

```sh
python -m qte_sdk.token set
```

If your project is a git repository, it offers to add `.env` to `.gitignore`; say yes. If git already tracks a `.env`, it stops before asking for the token and tells you to run `git rm --cached .env`, since `.gitignore` alone does not stop git committing a file it tracks. `python -m qte_sdk.token check` then says where the SDK will find each, without showing the token. Every SDK error or warning starts with a code such as `QTE-TOKEN-MISSING`: [docs/errors.md](docs/errors.md) lists each one with its fix, and every command's exit codes.

The SDK reads `QTE_URL` and `QTE_TOKEN` from `./.env` itself, only when they are not passed in or set as real environment variables, which always take precedence. For the token the order is `token=`, then `QTE_TOKEN`, then the file named by `QTE_TOKEN_FILE`, then `.env`. It refuses a `.env` holding the token that other users can read (on Windows, for now, it warns when other users can read or change it), and warns if git tracks it or does not ignore it. Step 2 of the [quickstart](docs/quickstart.md) explains the details and the alternatives: writing `.env` by hand, environment variables, or a private token file named by `QTE_TOKEN_FILE` (`python -m qte_sdk.token set --file` makes one).

Before anything else, check the whole setup with the smoke test. Run it from your project folder, with the `examples` folder copied into it (see Install):

```sh
python examples/smoke_test.py --instruments XOM
```

On Windows, type `py examples\smoke_test.py --instruments XOM`. To run them from any other folder, such as a clone, the SDK must find the address and token there some other way: run `python -m qte_sdk.token set` in that folder too, or set `QTE_URL` and `QTE_TOKEN_FILE` (a token file) in the environment (step 2 of the [quickstart](docs/quickstart.md)).

It says whether your SDK is the latest release, reports where the SDK finds the token and the address, connects, reads the calendar, watches the market for a few seconds and asks for your team's account, printing `PASS`, `FAIL` or `SKIP` with a reason for each check. It sends no orders unless you add `--place-test-order`; step 2 of the [quickstart](docs/quickstart.md) says more.

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
| [Developing your algo](docs/developing-your-algo.md) | From an idea to the exchange: explore past market data, run your strategy's loop on a replay of a past session, test its order logic, check your setup, then trade during a session |
| [Worked examples](examples/) | Runnable programs: check your setup end to end, print the book, quote both sides and manage the quotes, take liquidity with a market order, see the closed market outside a session, replay a past session's book |
| [Development guide](docs/development.md) | Working on the SDK itself: setup, checks and CI, the vendored contract |

The main modules, each documented in its docstrings:

| Module | What |
|---|---|
| [`qte_sdk.session`](qte_sdk/session.py) | `open_session`: authenticate and get a `Session` you can send on and iterate |
| [`qte_sdk.market_data`](qte_sdk/market_data.py) | `subscribe`, `unsubscribe`, `market_data`: the book, trades, marks and market state |
| [`qte_sdk.books`](qte_sdk/books.py) | `LatestBooks`: the latest book of each instrument, since a book is sent only when it changes |
| [`qte_sdk.calendar`](qte_sdk/calendar.py) | `next_open`, `next_close`: when the market next opens and closes, from the exchange's calendar |
| [`qte_sdk.instruments`](qte_sdk/instruments.py) | `instrument_info`, `can_trade`, `tradable_instruments`, `on_tick`, `sector_of`: the exchange's table of instruments, with each one's tick and lot size, its sector, and whether your team may trade it |
| [`qte_sdk.history`](qte_sdk/history.py) | `HistoryClient`: the published market data of sessions that have closed |
| [`qte_sdk.replay`](qte_sdk/replay.py) | `replay`: a past session's market data in one stream, in time order, to run your loop on; market data only, with no orders or fills |
| [`qte_sdk.orders`](qte_sdk/orders.py) | `send_new`, `send_cancel`, `send_amend`, `send_mass_cancel`, and helpers for order events |
| [`qte_sdk.tickets`](qte_sdk/tickets.py) | `send_ticket`, `send_ticket_cancel`, `send_ticket_urgency`: a Fundamentals pod's tickets to its Execution desk; `is_ticket_event`, `LatestTickets`: the exchange's answers and each ticket's state, for pods and desks |
| [`qte_sdk.account`](qte_sdk/account.py) | `send_account_query`, `is_account_state`: your team's positions, cash, equity and limit use; `AccountReports`: count each fill once against a reply |
| [`qte_sdk.resting`](qte_sdk/resting.py) | `RestingOrders`: your team's resting orders, built only from exchange events |
| [`qte_sdk.reconnect`](qte_sdk/reconnect.py) | `ReconnectingSession`: reconnects and resubscribes after a dropped connection |
| [`qte_sdk.options`](qte_sdk/options.py) | `option_symbol`, `parse_option_symbol`, `is_option_symbol`: build and read option contracts' OCC symbols, such as `SPY240119C00470000`; `chain_contracts`, `expiry_date`, `limit_scope`: read the day's `OptionChain`; `LatestGreeks`, `greek_to_decimal`, `vol_to_decimal`: published Greeks, exactly; `trading_state`: what an option contract may do now; `is_feed_only`: an option trade's feed-only residual print; `option_underlyings`, `listed_contracts`, `strike_increment`: the listed contracts and strike increment of each underlying, from the instruments table, for a first option subscribe |
| [`qte_sdk.update`](qte_sdk/update.py) | `check_for_update`: whether the installed SDK is the latest release, and whether an update is recommended, as `python -m qte_sdk.update` says; the daily background check that `open_session` starts (`QTE_UPDATE_CHECK=0` turns it off) |
| [`qte_sdk.units`](qte_sdk/units.py) | `to_decimal`, `to_micros`: exact conversion between prices and micro-dollars; `to_datetime`, `to_timedelta`, `to_timestamp`: exchange timestamps (milliseconds since the epoch, UTC) as `datetime` and `timedelta` |

## Things to know before you trade

- **Every order message is delayed** on its way into the exchange, cancels and amends included; the delay is currently 150 ms. The delay, the minimum time an order must rest, the price collar and your message budgets are set by the exchange; never hard-code them.
- **Prices are whole numbers of micro-dollars** ($199.97 is `199_970_000`). Convert with `qte_sdk.units`; never use `float` for prices.
- **There is no order ID.** Your team's orders are addressed by instrument, side and price, and your team holds at most one resting order at each price.
- **One market-data feed for everyone.** Market data is published on a fixed 100 ms grid: during a session the market state at every grid point, and an instrument's book only when it has changed, so keep the last book you received for each instrument.
- **Not every team sends orders.** Teams on the trading arms and Execution teams send orders; an order from a team without market access is rejected with a reason code that says so.

## Status

Working today: session authentication, the market calendar, market data, order entry and order events, the resting-order view, reconnect with heartbeats and session resume, past market data from the history service and its replay through your loop, a query for your team's positions, cash and limit use (it needs an exchange that serves the query), the closed market's official close price, the quickstart and the worked examples. Progress is tracked in [the issues](https://github.com/josh-g-s/qte-sdk/issues).

## Licence

MIT. See [LICENSE](LICENSE).
