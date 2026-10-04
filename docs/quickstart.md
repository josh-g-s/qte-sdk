# Quickstart

**Version:** 0.19

This guide takes you from a fresh install to a program that connects to the exchange, reads market data, places an order and cancels it. It then points you at the worked examples in `examples/` that you can run and adapt.

## What you need

- Python 3.11 or later.
- A clone of this repository, but only to run or read the worked examples (step 11).
- The address of the exchange you are trading on. The Head of Technology, Joshua, gives you the practice exchange's address; a test exchange you run on your own machine is usually `ws://127.0.0.1:8080/ws`.
- Your team's practice token. The Head of Technology, Joshua, gives it to you. Treat it like a password: never put it in a source file, a notebook, a screenshot or a repository.
- A strategy ID registered for your team, for any program that sends orders. Ask the Head of Technology, Joshua, to register one.

The practice exchange keeps the hours in the calendar it sends after you authenticate (step 3), so read them there rather than from this guide.

## 1. Install

Install the SDK into your own project's virtual environment:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install "git+https://github.com/josh-g-s/qte-sdk"
```

This installs the `qte_sdk` package only. The worked examples (step 11) are not installed with it: to run or read them, clone this repository and run them from the root of the clone, inside a virtual environment where the SDK is installed. You do not need a clone for anything else in this guide.

## 2. Set the exchange address and your token

The SDK needs two things: the exchange address, `QTE_URL`, and your team token, `QTE_TOKEN`. The simplest way to keep both is a `.env` file in your project folder, the folder you run your programs from. It lasts across terminals and needs no shell profile.

### A `.env` file (recommended)

From your project folder, with your virtual environment active, run the SDK's setup helper:

```sh
python -m qte_sdk.token set
```

It asks for the exchange address (a test exchange on your own machine is usually `ws://127.0.0.1:8080/ws`; otherwise use the address the course team gives you) and then for the token: paste it and press Enter. Nothing is shown as you paste, and since the token is typed at a prompt rather than on the command line, it never reaches your shell history. The helper writes both to `.env` as `QTE_URL` and `QTE_TOKEN`, in a file created readable only by you, keeping any other lines already in it. If the folder is a git repository that does not ignore `.env`, it offers to add `.env` to `.gitignore`; say yes. If git already tracks a `.env`, it stops before asking for the token and tells you to run `git rm --cached .env`, since `.gitignore` alone does not stop git committing a file it tracks. Inside a git repository it also stops if it cannot ask git (git is not installed, say), rather than guess. Run it again whenever the token changes.

On Windows the helper cannot make the file readable only by you: Windows does not apply the file's private mode, and the helper does not change Windows access lists. It says so when it saves the file. Keep your project, and any token file, in a folder only you can open, such as one inside your user profile, and do not share that folder. If `QTE_URL` is already set, press Enter at the address prompt to keep it.

To see where the SDK will take the token and the address from, without showing the token, run:

```sh
python -m qte_sdk.token check
```

To set it up by hand instead, first make sure git will never commit the file, then create it readable only by you:

```sh
printf '\n.env\n' >> .gitignore
touch .env && chmod 600 .env
ls -l .env
```

The last line should show `-rw-------`. If your project is a git repository, also check that git does not already track a `.env`, since adding it to `.gitignore` does not stop git committing a file it already tracks:

```sh
git ls-files --error-unmatch .env
```

An error saying `.env` did not match any file is what you want. If it prints `.env` instead, run `git rm --cached .env` and commit, before you put the token in.

Open `.env` in your editor, put in the address and your token, and save it. The token never passes through your shell, so it stays out of your shell history:

```sh
QTE_URL=ws://127.0.0.1:8080/ws
QTE_TOKEN=paste-your-token-here
```

The SDK reads `.env` itself, with no extra package: `open_session()` takes the address and the token from it, and so do the worked examples. It reads only the `.env` in the working directory (not a parent folder), so run your programs from the folder that holds it. It reads only `QTE_URL` and `QTE_TOKEN`; other lines are left alone, and nothing is put in your environment. Blank lines, `#` comments, an `export ` prefix and single or double quotes around a value are fine.

Two safeguards protect the token:

- If other users can read a `.env` that holds `QTE_TOKEN`, the SDK uses nothing in it and raises `MissingToken` (or `MissingURL`, when it was reading the address), saying to run `chmod 600 .env`. (This check applies on macOS and Linux.)
- If the `.env` (or, when it is a symbolic link, the file it points to) is inside a git repository and git tracks it or does not ignore it, the SDK warns you once, saying what to do: add `.env` to `.gitignore`, and if git already tracks it, run `git rm --cached .env` too. It still uses the file. The `.gitignore` of this SDK's repository protects only a clone of this repository, not your project. The same warning appears if you run a program inside a repository someone else made that ships a `.env`: check the address in it before you use it.

### Where the SDK looks, in order

For the token, the SDK uses the first of these that is set:

1. `token=` passed to `open_session` (or `ReconnectingSession` or `HistoryClient`);
2. the `QTE_TOKEN` environment variable;
3. the file named by the `QTE_TOKEN_FILE` environment variable;
4. `QTE_TOKEN` in `./.env`.

For the exchange address: the address passed to `open_session`, then the `QTE_URL` environment variable, then `QTE_URL` in `./.env`. If there is none, `open_session` raises `MissingURL`, which names both. A real environment variable always wins over `.env`, so an old `export QTE_TOKEN=...` in your terminal or shell profile hides the token in `.env`: run `unset QTE_TOKEN` and remove the line from your profile. If `QTE_TOKEN_FILE` is set but its file cannot be used, the SDK raises `MissingToken` rather than falling back to `.env`.

The history service (step 10) has its own address, which comes from `QTE_HISTORY_URL` in the environment, never from `.env`. Its token comes from the same places as above.

### Other ways

Environment variables work too. To try the SDK in one terminal, set the address and read the token without echoing it: run the second line, paste the token (nothing is shown) and press Enter.

```sh
export QTE_URL=ws://127.0.0.1:8080/ws
read -rs QTE_TOKEN && export QTE_TOKEN
```

An exported variable lasts only for that shell and the programs it starts. A new terminal does not have it, so a program run there falls back to `.env`, or raises `MissingToken` if there is none.

To keep the token in one place for all your projects, put it in a file outside any repository, readable only by you, and name that file in `QTE_TOKEN_FILE`. The helper does this with `python -m qte_sdk.token set --file`: it writes the token alone to `~/.qte/token` (or the path you give after `--file`) in a directory only you can open, and prints the `export QTE_TOKEN_FILE=...` line to add to your shell profile. If the path you give is inside a git repository, it applies the same checks as for `.env`: it stops if git tracks the file or cannot be asked, and offers to add the file to `.gitignore`. To do it by hand, the first command below makes a directory only you can open, then creates the file readable only by you before the token is written, replacing any old one. It reads the token without echo, so the token never appears on screen or in your shell history: run it, paste the token (nothing is shown) and press Enter. It works in zsh and bash, and running it again replaces the token. The second command checks the result, which should start with `-rw-------`.

```sh
(umask 077 && mkdir -p "$HOME/.qte" && chmod 700 "$HOME/.qte" && read -rs T && rm -f "$HOME/.qte/token" && printf '%s\n' "$T" > "$HOME/.qte/token")
ls -l "$HOME/.qte/token"
```

The parentheses run it in a subshell, so the `umask` and the variable `T` end with it. Then add this line to your shell profile, which is `~/.zshrc` for zsh (the macOS default) or `~/.bashrc` for bash (`~/.bash_profile` on macOS), and run it in your current terminal too:

```sh
export QTE_TOKEN_FILE="$HOME/.qte/token"
```

The line holds a path, not the token. The SDK reads the file each time it needs the token, removing one trailing newline. If the file is missing, unreadable, empty or not UTF-8 text, it raises `MissingToken` with a message that says which, and never shows the file's contents. You can still keep `QTE_URL` in a `.env`.

On macOS you can also keep the token in the Keychain. Store it once with `security add-generic-password -a "$USER" -s qte-token -w`, which prompts for it without echo, and load it with `export QTE_TOKEN="$(security find-generic-password -a "$USER" -s qte-token -w)"` in each terminal or in your shell profile.

Whichever you choose, never put the token in a source file or a notebook. The SDK never logs your token or puts it in an exception message, and a `.env` it cannot parse is reported by line number, never by its contents.

### Check your setup

Before you write any code, run the smoke test from a clone of this repository (step 11 says how to run the examples), naming an instrument or two:

```sh
python examples/smoke_test.py --instruments AAPL MSFT
```

It reports where the SDK finds the token and the address, without showing either; connects and names your team; reads the calendar; subscribes to each instrument and watches the market for `--seconds` (5 by default), and during a session waits up to `--book-wait` (60 by default, never less than `--seconds`) for the first book of an instrument that has none yet, since one with no valid quote has none; asks for your team's account; and, when `QTE_HISTORY_URL` is set, reads the start of the last closed session's books from the history service. Each check prints `PASS`, `FAIL` or `SKIP` with a one-line reason, then a summary, and the exit status is not 0 if any check failed. A `SKIP` is something it could not check, or that the exchange does not offer yet, such as the official close or the account query; the reason says which. Its output never shows your token or any account figure (only a fill of the test order is named, with its quantity and price), so you can send it to the course team when you ask for help.

It sends no orders unless you add `--place-test-order --strat-id <your strategy> --tick <tick>`, giving the instruments' tick in dollars (for example `0.01`), which the exchange does not send. Then, only while the market is open and no outage is in force, it places one buy of one share one tick above the wall's best bid, at least three ticks below every ask, waits for the exchange to report it resting, cancels exactly that price level and confirms the cancel; it never sends a mass cancel. It is a real order and can fill, since there is no post-only order: a fill fails the check, and a line on stderr names the position your team then holds. In a scored session it places nothing unless you also add `--allow-scored`. If it cannot confirm the cancel, it fails and names the level where the order may still rest; if you interrupt it while the order may rest, it first tries to cancel that level.

## 3. Open a session

Everything in the SDK is `async`. A session is an authenticated connection: `open_session` connects, sends your token and waits for the exchange to acknowledge it.

```python
import asyncio

from qte_sdk.session import open_session


async def main() -> None:
    session = await open_session()  # the address and token come from .env or the environment
    async with session:
        print("team:", session.info.team)
        print("unscored session:", session.info.unscored)


asyncio.run(main())
```

`session.info.unscored` is `True` when nothing in this session counts towards any score.

If the exchange refuses the session, `open_session` raises `SessionRejected` (from `qte_sdk.connection`), whose `reason_name` says why. With no token set it raises `MissingToken` before connecting, and with no address `MissingURL`.

Two rules about sessions:

- **Send on the session and read from it.** Every send function takes the session. Iterate the session itself, not its connection, so that you also receive anything the exchange sent before it acknowledged you.
- **Read from one place.** Have one loop read the session's events and do everything from there. Two loops reading the same session each get only some of the events.

### Reading the calendar

Right after it acknowledges your session, at any hour, the exchange sends a `calendar` message: every session of the term with its `open_time` and `close_time` (`early_close` marks the day that closes early), the named holidays inside the term, and the term's first and last dates. It is the only source of the term's full schedule; outside a session, the `SessionState` that answers a subscribe also names the next open and close (step 4). Never hard-code trading days, holidays or hours.

`open_session` does not wait for the calendar. `session.calendar` is `None` until it arrives, and is set as you iterate the session. To wait for it first:

```python
from qte_sdk.calendar import next_close, next_open
from qte_sdk.units import to_datetime, to_timedelta

calendar = await session.wait_for_calendar(timeout=5)
if calendar is None:
    print("no calendar from this exchange")
else:
    now = session.info.server_time  # the exchange's clock, not your computer's
    opens, closes = next_open(calendar, now), next_close(calendar, now)
    if opens is not None:
        print("next open:", to_datetime(opens), "in", to_timedelta(opens - now))
    if closes is not None:
        print("next close:", to_datetime(closes), "in", to_timedelta(closes - now))
```

- `wait_for_calendar` keeps every event it reads while it waits, so iterating the session afterwards still delivers all of them, the calendar included. Call it from the loop that reads the session, not from a second task.
- It returns `None` if no calendar arrives in time. An older exchange never sends one, so your program must still work without it.
- The times are the exchange's own timestamps, like `session.info.server_time`. A timestamp is a signed 64-bit count of milliseconds since the Unix epoch, in UTC, so the difference of two is a number of milliseconds. `qte_sdk.units` converts them exactly: `to_datetime` gives a timezone-aware `datetime` in UTC, `to_timedelta` turns a difference into a `timedelta`, and `to_timestamp` turns a `datetime` that has a time zone back into a timestamp. For "now", use `session.info.server_time` or a later timestamp from the exchange, never your computer's clock: the exchange's clock is the one that opens and closes the market. Show a time in New York time if you like, but take the trading hours from the calendar, never from time zone rules of your own.
- `next_open` skips days with no session. It returns `None` once the term's last session has opened, and `next_close` returns `None` once it has closed.
- The calendar is the schedule. Whether the market is open right now is what `SessionState` reports (step 4).
- A `ReconnectingSession` (step 9) keeps the calendar of its current session in its `calendar` attribute, which is `None` again after each reconnect until the new session's calendar arrives.

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

There is **one conflated market-data feed, the same for every participant**. Book, trades and the market session state are published on one 100 ms grid, and each carries the `grid_time` of the grid point it belongs to; the mark is published on its own, slower grid. During a session, the market session state is the one message sent at every grid point. Trades come only when there are prints, and a book only when it has changed:

- A `Book` for an instrument is published at a grid point only if it differs from the last one published for that instrument this session. No `Book` at a grid point means that instrument's book is unchanged, so keep the last one you received.
- The first grid point of each session publishes the book of every instrument that has one.
- A subscribe during a session is answered at once with the last book published this session for each instrument you named that has one, carrying the `grid_time` it was published at. An instrument with no book yet gets none on subscribe; its book arrives when it is first published. The book you get on subscribe can be older than the latest `SessionState`, and the same book can then arrive again at the same `grid_time`. Keep, per instrument, the book with the latest `grid_time`.

`qte_sdk.books.LatestBooks` keeps the latest book of each instrument for you:

```python
from qte_sdk.books import LatestBooks

books = LatestBooks()
async for item in market_data(session):
    if books.update(item):  # True only when item is a newer book for its instrument
        print("new book for", item.instrument)
    aapl = books.get("AAPL")  # the latest book held, or None before the first one
```

It ignores a book whose `grid_time` is the same as or older than the one it holds, and every message that is not a book. After a `SeqGap` or `Disconnected` it keeps the books but lists them in `books.stale`, since a change may have been missed. An instrument leaves `stale` when a newer book for it arrives or, after a reconnect, when the subscribe answer brings its last published book again at the same `grid_time`.

A `Book` is the state of one instrument at the end of an interval, not a stream of individual changes. It shows two kinds of depth: `bid_levels` and `ask_levels` are the wall ladder, best price first, and `student_bid_levels` and `student_ask_levels` are the orders participants have resting, one entry per price, with no identity attached.

`market_data` yields `Book`, `Trades`, `Mark`, `SessionState` and `OfficialClose` messages, a `Reject` if a subscription is refused (an unknown instrument, for example), and two warnings:

- `SeqGap`: messages were missed, so anything you built from them may be wrong.
- `DecodeFailed`: a message could not be decoded.

**Outside a session** you can still connect and subscribe, but there is no live market. A subscribe is answered once, not on the grid, with a `SessionState` whose `state` is `CLOSED`.

That one `SessionState` can also name the next scheduled session in three optional fields: `next_session_date`, `next_open_time` and `next_close_time`. They are set together, only on this out-of-hours reply, never on the `SessionState` of a running session, and are absent when the term has no later session; an exchange from before these fields does not send them either, so write code that works without them. `until_next_open(state, session.info.server_time)` from `qte_sdk.market_data` gives the time until that open in milliseconds (`to_timedelta` turns it into a `timedelta`), or `None` when the fields are absent. `server_time` is the time your session was acknowledged; pass a later exchange timestamp instead if you have one. They are a convenience: the calendar is still where to read the full schedule.

The contract also provides an `OfficialClose` for each subscribed instrument that has one, after the `SessionState`, but **the exchange does not send it yet**. Until it does, the `CLOSED` state is all you receive, and that is expected, not a fault. When it is sent, `OfficialClose.value` is that instrument's last official close, the time-weighted average of the mark over the final five minutes of its session, in micro-dollars like every price; `frozen` is set if any of those marks was frozen. Write your code so it works with or without one. [Using the SDK outside session hours](out-of-hours.md) walks through a whole run when no session is open.

No `Book`, `Trades` or `Mark` arrives until a session opens, so a loop that waits for a book waits until then.

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

Like the snippets of step 4, this runs inside the `async with session:` block of step 3 and uses its `session`; it needs nothing else from earlier steps.

```python
import asyncio

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

Every send returns the `request_ref` it put on the message. The `accepted` or `reject` that answers the message echoes it. An `order_cancelled` is meant to carry it when its `reason_code` is `CANCEL_REQUEST` (your cancel), `MASS_CANCEL` (your mass cancel) or `AMEND_CUT` (your amend cut the order to nothing), echoing the `request_ref` of that message. These rules are provisional: the contract has not specified them yet, so your program must not rely on `request_ref` being present, or absent, for any reason code. Keep a fallback: when `request_ref` is missing, match the cancellation by its strategy, instrument, side and price, as `examples/quote_both_sides.py` does. Match on `request_ref` with `request_ref_of`.

An amend changes the orders at one level: `send_amend(session, instrument=..., side=..., price=..., new_size=...)`. `new_size` is the new total remaining size, not an amount to add. `send_mass_cancel(session)` cancels **every order your team has on the exchange**, including those of your teammates' strategies.

Because a cancel or amend names a level, not an order, it acts on whichever of your team's orders rests at that level when the exchange applies it, after the order delay. If your order fills in the meantime and a teammate's strategy enters an order at the same price, your cancel removes theirs. Agree within your team who trades which instruments or prices.

A send checks its identifiers before anything leaves your machine: `strat_id` and `request_ref` must be 1 to 32 bytes of UTF-8 and `instrument` at most 32 bytes. A send that breaks this raises `ValueError`.

`send_new` also takes `parent_ticket_id`, which is for Execution desks only: it names the working parent ticket a child order works. Leave it out on any other team: the exchange rejects a `new` from any other team that carries it, with `PARENT_NOT_WORKING` (or `MALFORMED_MESSAGE` if the value itself is malformed).

## 7. Read your order events

The exchange sends these events about your team's own orders:

| Event | Meaning |
|---|---|
| `accepted` | Your message was applied. Echoes your `request_ref`. |
| `reject` | Your message was refused. `reason_code` says why; use `reason_code_name` to print it. |
| `order_state` | The state of one of your orders: its instrument, side, price, strategy and remaining size. It is sent when an order rests and for every amend the exchange accepts. An amend's `order_state` also carries `old_price`, the order's price before the amend, and reads `FILLED` or `CANCELLED` with a remaining size of 0 when the amend ended the order. |
| `execution` | A fill: price, size, remaining size, fee or rebate. A fill of a limit order carries the order's price as `order_price`; a fill of a market order has none. |
| `order_cancelled` | One of your orders left the book unfilled, with the reason (your cancel, a mass cancel, the close, and others). |
| `risk_notice` | A risk warning for your team. |

`qte_sdk.resting.RestingOrders` keeps a view of your team's resting orders built only from these events. Read its docstring for what it cannot know. When an amend's `order_state` arrives, the view removes the order from `old_price` and, if it still rests, records it at `price` with its remaining size. So an amend that moves the price moves the entry to the new level, a size-only amend (where `old_price` equals `price`) updates it in place, and an amend that fills the order completely or ends it some other way leaves nothing at either price. An older exchange that does not send `old_price` leaves the entry at the old price in the view; against one, cancel and re-enter instead of amending the price if you rely on the view.

### Query your account

You can ask the exchange for your team's own account at any hour, inside a session or not: its positions, its cash and, for most teams, its equity, daily profit and loss and limit use. This needs an exchange that serves the query. Like step 6, it runs inside the `async with session:` block of step 3:

```python
import asyncio

from qte_sdk.account import LIVE_MARK, is_account_state, send_account_query
from qte_sdk.orders import is_order_event, reason_code_name, request_ref_of
from qte_sdk.units import to_decimal

ref = await send_account_query(session)
try:
    async with asyncio.timeout(10):  # never wait for ever
        async for event in session:
            if is_account_state(event) and event.message.request_ref == ref:
                state = event.message
                live = state.valuation_basis == LIVE_MARK
                print("inside a session" if live else "outside a session")
                if state.HasField("session_date"):
                    print("trading date:", state.session_date)
                if state.HasField("cash"):
                    print("cash:", to_decimal(state.cash))
                for position in state.positions:
                    print(position.instrument, position.quantity, to_decimal(position.price))
                if state.HasField("summary"):
                    print("equity:", to_decimal(state.summary.equity))
                    print("daily P&L:", to_decimal(state.summary.daily_pnl))
                break
            if is_order_event(event) and event.type == "reject":
                if request_ref_of(event.message) == ref:
                    print("refused:", reason_code_name(event.message.reason_code))
                    break
            # a real program handles market data and order events here too
except TimeoutError:
    print("no answer within 10 seconds")
```

The reply, `account_state`, echoes your `request_ref` and arrives on the same stream as your order events, so read it in your one loop. A program that reads only `market_data(session)` never sees it. Positions are your team's, not one strategy's: every instrument you hold a nonzero quantity of, positive for long and negative for short, in order of instrument. `valuation_basis` says what the prices and the summary are valued at: `LIVE_MARK` inside a session, at each instrument's mark, or at its last official close until it has a valid mark in that session; `LAST_OFFICIAL_CLOSE` outside a session, at each instrument's latest official close, a break day's close included, or at 0 for an instrument that has never had an official close. An option is named by its 21-character OCC option symbol. `session_date` is a trading date. Inside a session it is the current session's date. Outside a session it is the date of the official closes the values use, those of the latest session or break day: on a break day, once that day's close has run, it is that day's date. That holds between terms too, when positions carried over from the term before are returned. Outside a session `daily_pnl` is the profit and loss of the session just finished. `session_date` is always there after the competition's first session, even if no instrument got an official close in it, and is absent only before that first session, when you hold nothing. `summary` is absent for an Execution desk and the house, and `cash` is absent only for an Execution desk, so check `HasField("summary")` and `HasField("cash")` first. A refused query is a `reject` with `request_type` `ACCOUNT_QUERY` that echoes your `request_ref` whenever the exchange could read it. Read the `qte_sdk.account` docstring for every field.

The reply also carries `as_of_report_seq`: the newest of your private order reports it already reflects. Each private report (`accepted`, a delayed `reject`, `execution`, `order_cancelled`, `order_state` and `risk_notice`) carries a report number, `event.report_seq` (None on other messages). The cut is for your account only: an `execution` or `risk_notice` at or below `as_of_report_seq` is already in the reply's cash, positions and summary, while `accepted`, `reject`, `order_cancelled` and `order_state` still apply to your view of your resting orders whatever their `report_seq`. `as_of_report_seq` is absent when your team has had no private report this term, and then every report applies.

Where a fill arrives in the stream does not tell you whether the reply counts it; only its number does. A fill the reply already counts can arrive after the reply, and would be counted twice if you added it to the reply's positions. A fill the reply does not count can arrive between your query and the reply, and would be lost when you replace your positions with the reply's. `qte_sdk.account.AccountReports` applies the cut for you and handles both. Send the query with it, pass it every event, and apply what it returns, in order:

```python
from qte_sdk.account import AccountReports, is_account_state
from qte_sdk.contract.v1.common_pb2 import BUY

account = AccountReports()
positions: dict[str, int] = {}
await account.query(session)
async for event in session:
    for item in account.update(event):  # every event, before you act on it
        if is_account_state(item):
            positions = {p.instrument: p.quantity for p in item.message.positions}
        elif item.type == "execution":
            fill = item.message
            change = fill.fill_size if fill.side == BUY else -fill.fill_size
            positions[fill.instrument] = positions.get(fill.instrument, 0) + change
    # a real program handles market data and order events here too
```

For the reply to your query, `update` returns the reply itself, then the reports that arrived while you waited and that the reply does not include: replace your positions with the reply's, then apply those again on top, as above. For an `execution` or `risk_notice`, it returns the event unless the latest reply already includes it. It returns nothing for anything else, so apply `accepted`, `reject`, `order_cancelled` and `order_state` to your resting orders as usual. That includes a reply to a query sent with `send_account_query`, or to an earlier query that a later one replaced: only the latest query sent with `account.query` is waited for, so wait for its answer before you query again, and take a reply as your positions only when `update` returns it. If you keep the reply yourself, `qte_sdk.account.covers(state, event)` says whether it already includes an event.

Report numbers start again each term, so a reply from the term before would take a new term's first reports for ones it already includes: before you trade in a new term, query again and wait for the reply, or start a new `AccountReports`. An exchange that does not number its reports sends no `as_of_report_seq`, and then every report applies; a reply can then be ordered against your fills only by when they arrive, which that exchange does not promise, so a fill close to a reply can be counted twice or missed.

The query is not an order message: the exchange does not hold it for the order delay or count it in your message budgets. It is a good way to check your positions again after a `SeqGap` or a dropped connection.

## 8. Values the exchange sets

**Every order message you send (new, cancel, amend and mass cancel) is held by the exchange for its order delay before it is applied. The delay is currently 150 ms.** An `accepted` therefore arrives at least that long after you send, and the book you acted on is at least that old by the time your order reaches it. Plan for it rather than around it.

The delay, the minimum time an order must rest before you may cancel or amend it, the price collar and your team's message budgets are all set by the exchange and can change. Do not build them into your code as constants. Instead:

- act on what the exchange reports: cancel or amend an order after its `order_state` arrives, not after a fixed sleep;
- keep your message rate well inside your budgets, and handle the rejects that say you went over one.

Some rejects you are likely to meet while learning (the exchange's rules decide exactly when each applies):

| Reason | What happened |
|---|---|
| `MARKET_CLOSED` | You sent an order message before the open, or on a day with no session at all (a weekend or an exchange holiday, for example). Check that `SessionState.state` is `OPEN` before you trade. |
| `RELEASE_AFTER_CLOSE` | On a day that had a session, your order message would have been applied after the close, once its order delay had passed. This includes anything sent after the close. |
| `MIN_REST_VIOLATION` | You cancelled or amended an order too soon after sending it. |
| `PRICE_COLLAR` | The price is outside the price collar. |
| `DUPLICATE_ORDER_AT_LEVEL` | Your team already has an order at that instrument, side and price. |
| `MESSAGE_BUDGET_EXCEEDED`, `BURST_CAP_EXCEEDED`, `NEW_ORDER_CAP_EXCEEDED` | You sent too many messages. Slow down. |

A reason from a newer contract than your SDK knows decodes as `REASON_CODE_UNSPECIFIED`; `event.unknown_enum_names()` returns the name the exchange sent.

## 9. If the connection drops

Iterating a session ends normally when the exchange closes the connection, and raises `websockets.exceptions.ConnectionClosedError` if it drops. A session from `open_session` does not reconnect, and neither do the worked examples: open a new session yourself. Your orders may still be resting after a drop, so check before you trade again.

The exchange sends a heartbeat at a regular interval, at any hour, so a working connection is never silent for long. The SDK absorbs heartbeats: they never appear among your events, and you send nothing back. Once the first heartbeat has arrived, if nothing at all arrives for `liveness_timeout` seconds, the SDK treats the link as dead, drops it and raises `qte_sdk.connection.LivenessTimeout`. The check starts only with that first heartbeat, so an exchange that does not send heartbeats is never dropped for being quiet. The default, 45 seconds, is the SDK's own choice, not a value the exchange sends; pass `liveness_timeout=` to `open_session` or `ReconnectingSession` to change it, or `None` to turn the check off.

The exchange also drops a connection that has sent it nothing for a while, with close code 4000 and reason `heartbeat timeout`. You do not need to send anything: in the background, the `websockets` library answers the exchange's pings and sends pings of its own. It can only answer a ping, or see the reply to its own, while it is reading the connection, and it pauses reading once more than 16 frames (its `max_queue` option) are waiting for your loop. If your loop stops reading for long, the connection is closed: by the library itself, with code 1011 and reason `keepalive ping timeout`, or by the exchange with 4000. Either way iterating raises `websockets.exceptions.ConnectionClosedError`, which `ReconnectingSession` treats as a drop and reconnects. Keep the loop that reads events quick, and do slow work in another task.

Each of your team's private order reports (`accepted`, a `reject` sent once the order delay is over, `execution`, `order_cancelled`, `order_state` and `risk_notice`) carries a report number, `event.report_seq`, which counts up by one for each report your team receives. A session delivers reports in that order and keeps `session.last_report_seq`, the number up to which it has delivered every one. If a number is skipped, a `ReportGap` event comes first. A report the exchange fails to build is dropped without a number, so it causes no gap, nothing tells you it is missing, and a replay does not bring it back. Only a snapshot puts your resting orders right again (a resume answered with one; `session.resume(0)` asks for one), and the dropped report itself, a fill for example, is never delivered.

`qte_sdk.reconnect.ReconnectingSession` reconnects for you and resumes each new session, so the reports you missed are not lost:

1. On a drop it delivers `Disconnected`. Treat it as the moment your positions, resting orders and book became uncertain.
2. It opens a new session, authenticates, and sends `resume` with the last report number it delivered. The exchange answers with `resume_ack`, and the new session subscribes again. You get `Connected`, whose `resume` is that answer.
3. The exchange then either replays every report you missed, exactly as first sent, or, if it no longer holds them all, sends one `order_snapshot` event per resting order of your team instead. Either way a `ResumeComplete` event follows, and then the reports that arrived meanwhile. A snapshot with no orders means your team has none resting, which is always so outside a session.

The first session resumes too, from 0, so it always starts with a snapshot of your resting orders. Report numbers start again each term, so after a reconnect in a new term (as the calendar's `term_start` and `term_end` say) the session also asks from 0 and gets a snapshot, rather than sending a number from the old term. With `resume=False` the number is still carried over, so that a report missed while disconnected is flagged with a `ReportGap`, and it is forgotten in a new term, so the new term's first reports are not dropped as duplicates. To check the term, a new session waits briefly for the calendar before it carries a number over. A `RestingOrders` view you pass as `resting=`, or that follows the session, reads incomplete from each `Connected` until that resume's `ResumeComplete`, is loaded from that snapshot, and is marked incomplete on `Disconnected`. A snapshot always makes it complete again at `ResumeComplete`. A replay does so only if the disconnect was the only thing that made it uncertain: after a `SeqGap`, a `ReportGap` or an order message that could not be decoded, it stays incomplete until a snapshot. Check `view.incomplete` before you trust it.

Market data is not replayed: what was published while you were disconnected is lost, and the new session's subscription delivers the latest books from then on. An order in flight when the connection dropped may or may not have reached the exchange, and the SDK never sends it again. If it arrived and the exchange replays what you missed, the replayed reports tell you what became of it; a snapshot shows only whether it is resting now, not whether it filled or was rejected. To resume a session you opened yourself, call `await session.resume(last_report_seq)` right after `open_session`, before reading events, passing the previous session's `last_report_seq` if the new session's calendar names the same term, or 0 for a snapshot. `await session.wait_for_calendar()` before it is fine, since it keeps every event it reads for you. A `RestingOrders` view that follows such a session learns of the resume only when the `resume_ack` event reaches it, and other events, such as the calendar, can come first, so call `view.mark_incomplete()` before `resume(0)`: the snapshot then makes the view complete.

An exchange that does not offer resume yet answers `resume` with a `reject`. `session.resume` then raises `qte_sdk.session.ResumeRejected` and the session carries on. `ReconnectingSession` carries on too: its `Connected.resume` is None, the `reject` is delivered as an event, and it behaves as it did before resume existed, so what you missed while disconnected is not recovered and the resting view stays incomplete after a reconnect.

## 10. Past market data (history)

The exchange's history service serves the market data of sessions that have closed, going back to the first session held, at any hour. It serves exactly what the live feed published to everyone, message for message, so you get the same `Book`, `Trades`, `Mark` and `SessionState` classes as from `market_data` and the same handling code works on both. It holds nothing else: no raw feed, and nothing about other teams.

It is a separate service with its own address, which the course team gives you. Set it as `QTE_HISTORY_URL`; the SDK has no default. Your token comes from `QTE_TOKEN` as before. No session is needed: this runs in any `async` function.

```python
from qte_sdk.connection import DecodeFailed, Unknown
from qte_sdk.history import HistoryClient, HistoryPending, HistoryUnavailable
from qte_sdk.market_data import Book
from qte_sdk.units import to_datetime, to_decimal

client = HistoryClient()  # address from QTE_HISTORY_URL, token from QTE_TOKEN
try:
    async for item in client.fetch("2026-01-05", "AAPL", "book"):
        if isinstance(item, Book) and item.bid_levels:
            print(to_datetime(item.grid_time), to_decimal(item.bid_levels[0].price))
        elif isinstance(item, (Unknown, DecodeFailed)):
            print("could not use a message:", item)
except HistoryUnavailable:
    print("there is no such data: not a session day, or an unknown instrument")
except HistoryPending as error:
    print("not ready yet; ask again in", error.retry_after, "seconds")
```

- `fetch(date, instrument, channel)` takes a session date, one instrument and one of `book`, `trades` or `mark`. `fetch_session_state(date)` gives the market session state. Messages arrive in the order they were published, as they download, so a whole day never has to fit in memory.
- A session that has closed is not ready straight away. The client waits and asks again after as long as the service says, spending at most a minute waiting in all (`max_wait`), then raises `HistoryPending`. A session still in progress, or one in the future, is not served until it closes: `fetch` raises `HistoryNotClosed` at once, without waiting, so ask again after the close.
- `HistoryUnavailable` means the data will never exist: a weekend or holiday, a date before the service began, or an instrument or channel it does not know. `HistoryNotImplemented` means the service does not serve that yet, which may change in a later release. Other answers from the service raise the other `HistoryError` classes in `qte_sdk.history`, such as `HistoryUnauthenticated` for a bad token; the service's own status word is in the error's `status`, and it may add new ones. If the service cannot be reached, you get the usual Python error, such as `ConnectionRefusedError`, or `TimeoutError` when one network step (connecting, or one read) takes longer than `timeout` seconds (30 by default). A download whose connection drops part way is resumed where it stopped, up to `max_resumes` times (3 by default); once those are used up you get `HistoryInterrupted`, and if the service cannot be reached to resume, the network error.
- `fetch_range(from_date, to_date, instruments, channels)` fetches several instruments and channels over a span of dates in one download. It yields a `Manifest` first, listing every entry you asked for as `ready`, `pending` (still being built), `not_closed` (its session has not closed yet) or `unavailable` (will never exist); only the ready ones follow, in the manifest's order. It never waits: ask again later for the pending ones (`Manifest.pending`), and after the close for the `not_closed` ones (`Manifest.not_closed`).
- If the connection drops, the client resumes the download where it stopped, and checks what arrived against the digest the service states for it. If the service sends the whole data again, unchanged, the client skips what you already have, so no message arrives twice; if the data has changed meanwhile, you get `HistoryChanged` and should start again.
- A manifest entry may carry a status word this SDK does not know yet. It is passed on in the `Manifest` as it is, and its data is not included.
- A `book` stream is sparse, like the live feed: it starts with the session's first book of the instrument and then holds only the books that changed. The book in force at time `t` is the last one with `grid_time` at or before `t`. To get it, feed the stream to a `LatestBooks` and stop at the first book later than `t`; `aclosing` closes the download when you stop early:

  ```python
  from contextlib import aclosing
  from datetime import UTC, datetime
  from qte_sdk.books import LatestBooks
  from qte_sdk.units import to_timestamp

  t = to_timestamp(datetime(2026, 1, 5, 15, 0, tzinfo=UTC))  # 15:00 UTC, 10:00 in New York
  books = LatestBooks()
  async with aclosing(client.fetch("2026-01-05", "AAPL", "book")) as items:
      async for item in items:
          if isinstance(item, Book) and item.grid_time > t:
              break
          if isinstance(item, (Unknown, DecodeFailed)):
              raise RuntimeError(f"a message could not be used, so the book may be wrong: {item}")
          books.update(item)
  book_at_t = books.get("AAPL")  # None if t is before the session's first book
  ```

- History tells you what the market published, not how your own orders would have filled against it.

## 11. Worked examples

Each example reads `QTE_URL` and `QTE_TOKEN` as step 2 describes, from the environment or a `.env` in the folder you run it from, runs for a bounded time and then stops by itself, prints every reject with its reason, and exits with status 0 when it has run cleanly. The instrument comes from `--instrument` (`--instruments` for the smoke test) or `QTE_INSTRUMENT`, and the examples that send orders take your strategy ID from `--strat-id` or `QTE_STRAT_ID`. Run any of them with `--help` for its options.

| Example | What it shows |
|---|---|
| `examples/smoke_test.py` | Run this first. Check a setup end to end: where the token and address come from, the session, the calendar, the market, the account query and past data, with `PASS`, `FAIL` or `SKIP` for each check (step 2). Sends no orders unless given `--place-test-order`. |
| `examples/print_book.py` | Connect, subscribe and print the book, trades, mark and market session state, or the official close outside a session. Sends no orders. Stops after `--seconds` or `--max-messages`. |
| `examples/quote_both_sides.py` | Rest a limit order on each side, inside the wall's best prices, and manage them: cancel and re-enter when the wall moves, amend the size back up after a partial fill, re-enter after a full fill. Keeps the latest book with `LatestBooks` and acts on it after each order event and on its own timer, not only when a new book arrives, since the exchange publishes a book only when it changes. Cancels its own orders when `--seconds` are up. |
| `examples/take_liquidity.py` | Send one market order once the latest book shows the side it trades against, and report its fills. Sends at most one order and never retries. Stops when the order is finished or after `--seconds`. |
| `examples/out_of_hours.py` | Outside a session: read the calendar, subscribe, and print the closed market's session state and the wait until the next open. Sends no orders, and stops at once if a session is under way. The runnable part of [Using the SDK outside session hours](out-of-hours.md). Stops after `--seconds`. |

```sh
python examples/smoke_test.py --instruments AAPL MSFT
python examples/print_book.py --instrument AAPL --seconds 10
python examples/quote_both_sides.py --instrument AAPL --strat-id my-strategy --seconds 30
python examples/take_liquidity.py --instrument AAPL --strat-id my-strategy --side buy --size 1
python examples/out_of_hours.py --instrument AAPL
```

The examples are for learning the SDK, not strategies: they make no attempt to make money.
