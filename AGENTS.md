# Using qte-sdk

**Version:** 1.15

This file is for anyone building a trading program for the Queen's Tower Exchange (QTE) with this SDK, and for the coding agent helping them. You can copy it into your own project so your agent follows it there too.

## What this is

- `qte_sdk` is the Python client for the QTE exchange. You run your program on your own machine and it trades through the exchange's API, using the exchange address and team token the course team gives you.
- Install it into your own project with `pip install "git+https://github.com/josh-g-s/qte-sdk"`, or, with no git, from a release's zip (`pip install https://github.com/josh-g-s/qte-sdk/archive/refs/tags/v1.0.1.zip`). Clone this repository, or unzip that release's source, only to read or run the worked examples in `examples/`; copy that folder into the project folder that holds `.env` and run the examples from there, since `.env` is read only from the working directory. On Windows, use `py`, not `python3`.
- `python -m qte_sdk.update` says whether the installed SDK is the latest release and prints the command to update it; pin a release with `@v1.0.0` on the install URL.
- Start with the [README](https://github.com/josh-g-s/qte-sdk#readme), then the [quickstart](https://github.com/josh-g-s/qte-sdk/blob/main/docs/quickstart.md), which covers sessions, market data, orders, the values the exchange sets, reconnecting and past market data.
- To check a setup end to end, have the person run `python examples/smoke_test.py --instruments <symbols>` from the project folder, with `examples/` copied into it. It prints PASS, FAIL or SKIP per check and sends no orders unless given `--place-test-order`.

## The token

- The SDK takes the team token from `token=` if you pass one, then the `QTE_TOKEN` environment variable, then the file named by `QTE_TOKEN_FILE`, then `QTE_TOKEN` in a `.env` file in the working directory. It takes the exchange address from the `url` you pass, then `QTE_URL`, then `QTE_URL` in `.env`, so `open_session()` needs neither argument.
- A `.env` is the recommended place for both. The person creates it by running `python -m qte_sdk.token set` themselves (it needs a terminal and reads the token without echo); `python -m qte_sdk.token check` says where the SDK finds each without showing the token. It must be listed in the project's `.gitignore` and readable only by its owner (`chmod 600 .env`). The SDK refuses a `.env` holding the token that others can read (on Windows it warns when others can read or change it, and the fix is to keep the project under `%USERPROFILE%`) and warns when git does not ignore it: fix the cause, never silence the warning or loosen the check.
- Never write the token into source code, a notebook, a config file that is committed, a test, a log, printed output or a screenshot.
- An agent must never ask for the token in chat, print it, read or display the contents of `.env` or a token file, or add code that logs it. If you find a token anywhere other than a private, git-ignored `.env` or token file, such as in a tracked file or in output, stop and tell the person to have it replaced.
- The SDK keeps the token out of its own logs and errors. Do not defeat that, for example by logging request headers or the raw connection.

## Values the exchange sets

Never build these into code as constants. They are set by the exchange and can change:

- the order delay (every new, cancel, amend and mass cancel is held for it before it is applied; it is currently 150 ms);
- the minimum time an order must rest before it may be cancelled or amended;
- the price collar;
- each instrument's tick and lot size, and which instruments your team may trade. Read them from the instruments table the exchange sends after the calendar (`session.wait_for_instrument_table()`, then `qte_sdk.instruments`), and never hard-code the list of instruments;
- your team's message budgets;
- the heartbeat interval, and how long the exchange waits before it drops a silent connection (the SDK's `liveness_timeout` is a client-side setting, not one of these);
- trading days, holidays and hours. Read them from the calendar the exchange sends after you authenticate (`session.wait_for_calendar()`, then `qte_sdk.calendar.next_open` and `next_close`).

Act on what the exchange reports instead: cancel or amend after the order's `order_state` arrives rather than after a fixed sleep, trade only while `SessionState.state` is `OPEN`, and handle each reject by its reason.

## Orders

- There is no order ID. Your team's orders are addressed by instrument, side and price, and your team holds at most one resting order at each, across all of its strategies. A second new order at a level where you already rest is rejected; send an amend to change its size.
- A cancel or amend acts on whichever of your team's orders holds that level when it is applied, which may be another strategy's. A mass cancel cancels every resting order your team has, on every instrument and for every strategy.
- A new order carries a strategy ID registered for your team. Ask the Head of Technology, Joshua, to register one.
- Prices are whole numbers of micro-dollars (`199_970_000` is $199.97). Convert with `qte_sdk.units` (`to_micros`, `to_decimal`) and never use `float` for a price.
- An option contract's instrument id is its OCC symbol without spaces, such as `SPY240119C00470000`. Build and read it with `qte_sdk.options` (`option_symbol`, `parse_option_symbol`, `is_option_symbol`) rather than slicing strings. An option order's size is in contracts, never shares, and its prices are per share. Read an option book's `trading_state` with `qte_sdk.options.trading_state`, which treats a state it does not know as suspended, and keep the latest Greeks per contract with `qte_sdk.options.LatestGreeks`. Take a first option contract to subscribe to from the instruments table (`qte_sdk.options.listed_contracts`), never by guessing a symbol. Convert Greeks with `greek_to_decimal` and `vol_to_decimal`, never `float`.
- Send with the functions in `qte_sdk.orders` (`send_new`, `send_cancel`, `send_amend`, `send_mass_cancel`) and the generated message types in `qte_sdk.contract.v1`. Do not build JSON by hand, and do not edit the generated files or the `.proto` files.

## Market data

- There is one conflated market-data feed, the same for everyone: books, trades and the session state on a 100 ms grid, and the mark on its own, slower grid. There is no faster or raw feed.
- On the grid, a book is published only when it has changed, so a quiet instrument may send nothing for a long time. A subscribe during a session is answered at once with the last book published for each instrument that has one, which may be older than the latest grid point; an instrument with no book yet sends its first when it is published. Keep the latest book of each instrument (`qte_sdk.books.LatestBooks` does this) and do not make your program wait for a new book before it acts. During a session, `SessionState` arrives at every grid point.
- Handle the warning events: `DecodeFailed` and `Unknown` (a message that could not be used) and `SeqGap` (messages were missed). After a gap or a dropped connection, your book, positions and resting orders are uncertain until you check them again. To check your positions and cash, send `qte_sdk.account.send_account_query` and read the `account_state` that answers it in your one loop (`qte_sdk.account.is_account_state`). If you keep your own positions from fills, query with `qte_sdk.account.AccountReports` instead and apply only what its `update` returns: a fill can arrive after a reply that already counts it, or before a reply that does not, and adding fills to a reply by hand counts some twice and loses others.

## Sessions

- Open one session with `qte_sdk.session.open_session`, send on it, and have one loop read its events by iterating the session itself. Two loops reading the same session each get only some of the events, or fail. In that one loop, pick out market data with `qte_sdk.market_data.as_market_data` and order events with `qte_sdk.orders.is_order_event`. `market_data(session)` yields market data only and drops order events, so use it only in a program that sends no orders.
- A session from `open_session` does not reconnect. Iterating it ends when the exchange closes the connection and raises `websockets.exceptions.ConnectionClosedError` if the connection drops. `qte_sdk.reconnect.ReconnectingSession` reconnects for you, reports a drop as a `Disconnected` event, and resumes the new session: your team's private order reports sent while you were disconnected are replayed, or replaced by a snapshot of your resting orders. Market data sent while you were disconnected is not recovered, and an order in flight when the connection dropped is never sent again.

## Past market data

The history service (`qte_sdk.history.HistoryClient`) serves the market data that closed sessions published, message for message. It shows what the market published, not how your own orders would have filled against it. The SDK has no fill simulator.

`qte_sdk.replay.replay` merges a closed session's books, trades, marks and session state into one stream, grid point by grid point, as the classes `market_data` yields, so the same market-data handling runs offline. It sends no orders and fills nothing, and raises `qte_sdk.replay.ReplayOutOfOrder` if a stream's times go back. Take time from the messages, never the machine's clock. Grid points arrive in order, live and in a replay, but the messages of one grid point (books, trades, marks and the session state, across instruments) come in no promised order: treat them as a set and do not rely on any order within it. [Developing your algo](docs/developing-your-algo.md) walks through the path from history to the exchange.

## Times

- Every exchange timestamp (`server_time`, `grid_time`, a calendar session's `open_time` and `close_time`, and the rest) is a whole number of milliseconds since the Unix epoch, in UTC. Convert with `qte_sdk.units`: `to_datetime` for a timezone-aware UTC `datetime`, `to_timedelta` for the difference of two timestamps, and `to_timestamp` to turn a `datetime` with a time zone back into a timestamp.
- For "now", use the exchange's clock (`session.info.server_time` or a later exchange timestamp), never the computer's. Take trading hours from the calendar; a time zone such as New York's is only for showing times.

## Where to test

Run your program against the practice exchange the course team gives you, or a test exchange on your own machine. Do not guess exchange addresses, and do not point an experiment at an exchange where it would trade for real unless you mean it to.

## Getting help

- For a bug in the SDK or its docs, open an issue on this repository. It is public: never include your token, your team's account details or your strategy's code.
- For tokens, strategy IDs, exchange addresses and access, ask the Head of Technology, Joshua.
