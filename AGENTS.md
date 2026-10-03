# Using qte-sdk

**Version:** 1.5

This file is for anyone building a trading program for the Queen's Tower Exchange (QTE) with this SDK, and for the coding agent helping them. You can copy it into your own project so your agent follows it there too.

## What this is

- `qte_sdk` is the Python client for the QTE exchange. You run your program on your own machine and it trades through the exchange's API, using the exchange address and team token the course team gives you.
- Install it into your own project with `pip install "git+https://github.com/josh-g-s/qte-sdk"`. Clone this repository only to read or run the worked examples in `examples/`.
- Start with the [README](https://github.com/josh-g-s/qte-sdk#readme), then the [quickstart](https://github.com/josh-g-s/qte-sdk/blob/main/docs/quickstart.md), which covers sessions, market data, orders, the values the exchange sets, reconnecting and past market data.

## The token

- The SDK takes the team token from `token=` if you pass one, then the `QTE_TOKEN` environment variable, then the file named by `QTE_TOKEN_FILE`, then `QTE_TOKEN` in a `.env` file in the working directory. It takes the exchange address from the `url` you pass, then `QTE_URL`, then `QTE_URL` in `.env`, so `open_session()` needs neither argument.
- A `.env` is the recommended place for both. It must be listed in the project's `.gitignore` and readable only by its owner (`chmod 600 .env`). The SDK refuses a `.env` holding the token that others can read and warns when git does not ignore it: fix the cause, never silence the warning or loosen the check.
- Never write the token into source code, a notebook, a config file that is committed, a test, a log, printed output or a screenshot.
- An agent must never ask for the token in chat, print it, read or display the contents of `.env` or a token file, or add code that logs it. If you find a token anywhere other than a private, git-ignored `.env` or token file, such as in a tracked file or in output, stop and tell the person to have it replaced.
- The SDK keeps the token out of its own logs and errors. Do not defeat that, for example by logging request headers or the raw connection.

## Values the exchange sets

Never build these into code as constants. They are set by the exchange and can change:

- the order delay (every new, cancel, amend and mass cancel is held for it before it is applied; it is currently 150 ms);
- the minimum time an order must rest before it may be cancelled or amended;
- the price collar;
- your team's message budgets;
- the heartbeat interval, and how long the exchange waits before it drops a silent connection (the SDK's `liveness_timeout` is a client-side setting, not one of these);
- trading days, holidays and hours. Read them from the calendar the exchange sends after you authenticate (`session.wait_for_calendar()`, then `qte_sdk.calendar.next_open` and `next_close`).

Act on what the exchange reports instead: cancel or amend after the order's `order_state` arrives rather than after a fixed sleep, trade only while `SessionState.state` is `OPEN`, and handle each reject by its reason.

## Orders

- There is no order ID. Your team's orders are addressed by instrument, side and price, and your team holds at most one resting order at each, across all of its strategies. A second new order at a level where you already rest is rejected; send an amend to change its size.
- A cancel or amend acts on whichever of your team's orders holds that level when it is applied, which may be another strategy's. A mass cancel cancels every resting order your team has, on every instrument and for every strategy.
- A new order carries a strategy ID registered for your team. Ask the Head of Technology, Joshua, to register one.
- Prices are whole numbers of micro-dollars (`199_970_000` is $199.97). Convert with `qte_sdk.units` (`to_micros`, `to_decimal`) and never use `float` for a price.
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

## Where to test

Run your program against the practice exchange the course team gives you, or a test exchange on your own machine. Do not guess exchange addresses, and do not point an experiment at an exchange where it would trade for real unless you mean it to.

## Getting help

- For a bug in the SDK or its docs, open an issue on this repository. It is public: never include your token, your team's account details or your strategy's code.
- For tokens, strategy IDs, exchange addresses and access, ask the Head of Technology, Joshua.
