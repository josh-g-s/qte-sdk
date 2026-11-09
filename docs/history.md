# The history service over plain HTTP

**Version:** 0.1

The history service serves the market data of sessions that have closed, and your own team's past private reports. Most teams use it through `HistoryClient` in `qte_sdk.history` (quickstart step 10), which does everything on this page for you. This page is for when you want to call it yourself, with `curl` or from another language, or want to know exactly what `HistoryClient` is sending.

The source of truth is the exchange's History API contract. If this page and the service ever disagree, the service wins; tell your Director.

## Where it is and how to sign in

- The address is `https://history.trading.queenstowerexchange.com`. It is a separate service from the exchange's own address (`wss://api.trading.queenstowerexchange.com/ws`). The SDK reads it from the `QTE_HISTORY_URL` environment variable and has no default, so set it yourself. It is never read from `.env`.
- Every call is a plain HTTPS `GET`. There is no handshake, no session to open and nothing kept between calls, and it works at any hour.
- Send your team token in the `Authorization` header, as `Authorization: Bearer <token>`. It is the same token the SDK uses for the exchange, from `QTE_TOKEN`. In a shell, keep it in an environment variable rather than typing it, so it stays out of your history and your screen:

```sh
export QTE_HISTORY_URL=https://history.trading.queenstowerexchange.com
# QTE_TOKEN is already set in your environment; do not paste the token into a command
curl -sS -H "Authorization: Bearer $QTE_TOKEN" "$QTE_HISTORY_URL/v1/history/sessions"
```

- A missing token, a token the service does not recognise, and a team whose arm is not open for the term all get the same answer, `401`, with no reason given. Do not retry; check the token (`python -m qte_sdk.token check`), and ask your Team Lead if it still fails.
- Treat the token like a password, here as everywhere: never put it in a script that is committed, a notebook, a chat or a screenshot.

## What the service holds

- The public market data of every session that has closed, going back to the first session held, exactly as the live feed published it to everyone, message for message. Any valid team token reads any session's public data, whichever term it was from.
- Your own team's private reports (your orders' `accepted`, `reject`, `execution`, `order_cancelled`, `order_state`, `risk_notice` and `term_close_fill` messages). **These are for the current term only:** a session from an earlier term, or another team's name, is refused with `403`. Nobody is ever served another team's reports.
- Nothing else: no raw feed, and nothing private about other teams.
- A session that has not closed yet is not served, however recent. Ask again after its close.

## The endpoints

All paths start with `/v1/history`. Dates are session dates, written `2026-09-29`. Parameters in `{braces}` are part of the path.

| Call | What it returns |
|---|---|
| `GET /v1/history/sessions` | Every session the service holds, oldest first, as a JSON object with a `sessions` list. Each entry gives the `session_date`, the `term_id` of the term it belonged to, whether it was `scored`, a status for each of `book`, `trades`, `mark` and `session_state` (`ready` or `pending`), and, when a feed gap touched the session, a `feed_gaps` list saying when and why. |
| `GET /v1/history/{session_date}/{instrument}/{channel}` | One session's stream for one instrument. `instrument` is one of the 180 names, or an option contract by its OCC symbol, such as `SPY261120C00650000`. `channel` is `book`, `trades` or `mark`, or `option_greeks` for an option contract. |
| `GET /v1/history/{session_date}/session_state` | One session's market session state messages. No instrument. |
| `GET /v1/history/{session_date}/option_chain` | The option chain for that session: the contracts listed for it. No instrument. |
| `GET /v1/history/{session_date}/{team}/reports` | Your team's own private reports for one session. `{team}` must be your team's name exactly as the exchange knows it (`session.info.team` in the SDK), with no change of case and no spaces added. |
| `GET /v1/history/range?from=...&to=...&instruments=...&channels=...` | The same public streams as the per-instrument call, for several instruments and channels over a span of dates, in one response. `from` and `to` are session dates, both included. `instruments` and `channels` are comma-separated lists, with no empty element. `channels` are drawn from `book`, `trades`, `mark` and `option_greeks`. |
| `GET /v1/history/range/{team}/reports?from=...&to=...` | Your team's own private reports over a span of dates, in one response. Only `from` and `to`, and the whole span must lie in the current term. |

Any other path is not served.

### What a stream looks like

The stream calls answer `200` with `Content-Type: application/x-ndjson`: one JSON object per line, in the order the messages were published. Each line has five fields:

| Field | Meaning |
|---|---|
| `version` | The contract version the line was published under. It may be older than the version you get live today. |
| `type` | The message type, such as `book`, `trades`, `mark`, `option_greeks`, `session_state`, `option_chain` or, for reports, `accepted`, `reject`, `execution`, `order_cancelled`, `order_state`, `risk_notice`, `term_close_fill`. The same names the live feed uses. |
| `seq` | The line's position within this one stream, counting from 1. It is not the live connection's own `seq`. Use it to spot a missing line in this download. |
| `sent_at` | When the message was sent, taken from the message itself. |
| `payload` | The message body, exactly as the live feed carried it. |

Prices, sizes and times in a `payload` are the same decimal strings the live feed uses; `HistoryClient` turns them into the same classes (`Book`, `Trades`, `Mark` and so on) you get from a live session.

A `book` stream is sparse, like the live feed: it holds only the books that changed. See quickstart step 10 for how to get the book in force at a given time.

### What the range calls return

The range calls answer `200` (or `206` for a part of it) with one **manifest** line first, then the data of every entry that is ready, back to back.

- The manifest line is a JSON object with a single `manifest` list. It has no `type` and no `payload`, which is how you tell it from a data line.
- It has one entry for every session date, instrument and channel you asked for (for the reports call, every session date), in order of date, then instrument, then channel. An entry is never left out for not being ready.
- Each entry has a `status`: `ready` (its data follows), `pending` (the session has closed but the data is not built yet), `not_closed` (the session has not closed) or `unavailable` (it will never exist, such as a weekend, a date before the service began, or an option contract that session's chain did not list).
- A `ready` entry also has `byte_offset`, `length` and `sha256`: where its bytes are, counting from the first byte after the manifest line, how many there are, and their SHA-256. Cut the response with them and check each piece against its own `sha256`; each piece is exactly what the single-session call would have returned. The three are `null` for an entry that is not `ready`.
- If anything in the response is `pending`, it also carries a `Retry-After` header. Read each entry's own `status` to see which, not the header.
- `instruments` is sorted in byte order and any duplicate removed. A channel name the service does not know is an `unavailable` entry, not an error.
- A request can name only so many dates, instruments and (date, instrument, channel) combinations. The limits are generous (a whole term, all 180 names and every contract one session lists, with all four channels) but not published. A request over a limit gets `400`; split it, by dates first.

### Answers

| Status | `status` in the JSON body | Meaning and what to do |
|---|---|---|
| `200`, `206` | | The data. `206` is the part of it you asked for with a `Range` header. |
| `202` | `pending` | The session has closed but the data is not built yet. Wait the `Retry-After` seconds and ask again. |
| `400` | `malformed_request` | The request is wrong on its face: a date that does not parse, an empty element in a list, `to` before `from`, or a span over the limits. Change the request; do not retry it as it is. |
| `401` | `unauthenticated` | The token is missing or not recognised (or your team's arm is not open this term). Check the token. |
| `403` | `forbidden` | A private report for a team that is not yours, or for a session outside the current term. Not retryable. |
| `404` | `unavailable` | It will never exist: a weekend or holiday, a date before the service began, or an instrument or channel it does not know. |
| `409` | `not_closed` | The session has not closed yet. Ask again after its close. There is no `Retry-After`. |
| `429` | `rate_limited` | You have used up your requests for the minute. Wait the `Retry-After` seconds. |
| `500` | `internal_error` | A fault on the service's side, such as a stored object failing its integrity check. Not your mistake. Ask again later, and tell your Director if it keeps happening. |
| `501` | `not_implemented` | The service describes this call but has not built it in this deployment. It may exist later. |

Every error body is JSON with a `status` as above and a message. Check the status code first, then the `status` word.

## Examples with curl

These need `QTE_HISTORY_URL` and `QTE_TOKEN` in your environment, as under "Where it is and how to sign in". Add `--compressed` to ask for gzip, which makes a download 5 to 10 times smaller; `curl` unpacks it for you. Quote any URL that has a `?` or `&` in it, or your shell will cut it.

List the sessions held:

```sh
curl -sS -H "Authorization: Bearer $QTE_TOKEN" \
  "$QTE_HISTORY_URL/v1/history/sessions"
```

One instrument's `book` for one session, saved to a file:

```sh
curl -sS --compressed -H "Authorization: Bearer $QTE_TOKEN" \
  -o AAPL-book-2026-09-29.ndjson \
  "$QTE_HISTORY_URL/v1/history/2026-09-29/AAPL/book"
```

An option contract's Greeks:

```sh
curl -sS --compressed -H "Authorization: Bearer $QTE_TOKEN" \
  "$QTE_HISTORY_URL/v1/history/2026-09-29/SPY261120C00650000/option_greeks"
```

A session's state messages, and its option chain:

```sh
curl -sS --compressed -H "Authorization: Bearer $QTE_TOKEN" \
  "$QTE_HISTORY_URL/v1/history/2026-09-29/session_state"

curl -sS --compressed -H "Authorization: Bearer $QTE_TOKEN" \
  "$QTE_HISTORY_URL/v1/history/2026-09-29/option_chain"
```

Several instruments and channels over several days, in one download (the manifest is the first line):

```sh
curl -sS --compressed -H "Authorization: Bearer $QTE_TOKEN" \
  -o range.ndjson \
  "$QTE_HISTORY_URL/v1/history/range?from=2026-09-28&to=2026-09-30&instruments=AAPL,MSFT&channels=book,trades"
```

Your team's own reports for one session. Put your team's name in `TEAM` first, exactly as the exchange knows it, with the same capitals:

```sh
TEAM=YourTeamName
curl -sS --compressed -H "Authorization: Bearer $QTE_TOKEN" \
  "$QTE_HISTORY_URL/v1/history/2026-09-29/$TEAM/reports"
```

Your team's reports over a span of dates, all inside the current term:

```sh
curl -sS --compressed -H "Authorization: Bearer $QTE_TOKEN" \
  "$QTE_HISTORY_URL/v1/history/range/$TEAM/reports?from=2026-09-28&to=2026-09-30"
```

To see the status code and the `Retry-After` header as well as the body, add `-i`. A `Range` header (`curl -r`) gets just those bytes, with status `206`, always uncompressed. `HistoryClient` uses this to resume a download that drops, and checks what arrived against the digest the service states.

## Rate limits and being polite

- Each token has a cap on requests per minute. The number is not published, and it is set generously for 32 teams reading the whole history; a program that downloads what it needs once and keeps it will never meet it. When you do, the service answers `429` with `Retry-After`, and `HistoryClient` waits that long for you.
- Prefer one range call to many single calls, and keep what you download in files rather than fetching it again on every run. Closed sessions never change once they are `ready`.
- Do not poll a `pending` session faster than its `Retry-After` says.

## Using `HistoryClient` instead

`HistoryClient` does all of this: it sets the header from your token, asks for gzip and unpacks it, waits out `202` and `429` for you (up to `max_wait`), resumes a download that drops, checks what arrived against the service's digests, and gives you the same `Book`, `Trades`, `Mark` and report classes as a live session. Its methods are `fetch`, `fetch_session_state`, `fetch_option_chain`, `fetch_range`, `fetch_reports` and `fetch_reports_range`, and it raises one `HistoryError` class for each answer in the table above. See quickstart step 10 for worked examples, and [errors.md](errors.md) for the `QTE-HISTORY-` codes.
