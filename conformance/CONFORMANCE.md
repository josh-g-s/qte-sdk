# Conformance steps

**Version:** 1.7

The scripted checks a client and an exchange are run against, end to end. Each step names
the messages it exercises and the rule it proves, citing `SPEC.md` sections and naming
the `.proto` messages and fields of the published contract. A client that cannot produce
or consume every named message with every Required field has not met the contract.

There are two scripts: a WebSocket session (steps 1 to 16, with step 1a lettered
after 1 and steps 9a to 9c lettered between 9 and 10) and the read-only history
service (steps H1 to H19). The history service is a separate, stateless HTTP interface:
it has no `auth`, no `subscribe` and no step in the numbered session script.

## How to read the steps

Several steps depend on something only the exchange under test controls: a counterparty
that trades against the client, the close of a session, the state of a history archive.
Each is written as a **Precondition**: something the exchange under test provides for
that step. A precondition is not an instruction to the client, and these steps do not say
how an exchange provides one.

Message names are the lower-case `type` tokens of the envelope, and each is the
`.proto` message of the same name in CamelCase: `auth` is `Auth`, `session_ack` is
`SessionAck`, `calendar` is `Calendar`, `instruments` is `Instruments`, `subscribe` is
`Subscribe`, `heartbeat` is
`Heartbeat`, `resume` is `Resume`, `resume_ack` is `ResumeAck`, `order_snapshot` is
`OrderSnapshot`, `new` is `NewOrder`, `cancel` is `CancelOrder`,
`amend` is `AmendOrder`, `mass_cancel` is `MassCancel`, `accepted` is `Accepted`,
`reject` is `Reject`, `execution` is `Execution`, `order_cancelled` is `OrderCancelled`,
`order_state` is `OrderState`, `book` is `Book`, `trades` is `Trades`, `session_state`
is `SessionState` and `official_close` is `OfficialClose`. Reason codes are the values
of the `ReasonCode` enum.

Every timestamp a step names (`receipt_time`, `release_time`, `grid_time`, `server_time`,
`open_time`, `close_time` and the rest) is a signed 64-bit count of milliseconds since the
Unix epoch, UTC, a decimal string on the wire (SPEC 8.1, SPEC 9.2, SPEC 13.1). A step that
adds δ_oe to `receipt_time` adds milliseconds.

**Order within a grid point** (SPEC 4.3). A step that names several public messages
published at one grid point (a `book`, a `trades`, a `mark` and a `session_state`, for one
instrument or several) checks them as a set, never in a fixed order. Every message
published at a grid point (`grid_time` on `Book`, `Trades` and `SessionState`;
`sampled_at` on `Mark`) is sent on a connection before any message published at a later
grid point, and nothing orders the messages of one grid point among themselves. A script
that fails an exchange for the order in which it sends one grid point's messages, or that
takes `session_state` to mark the start or the end of its grid point, has not met the
contract. The subscribe snapshot of step 3 is sent at subscribe time, not at a grid point:
it repeats a `book` published at an earlier grid point and carries that grid point's
`grid_time`, which can be earlier than a grid point the connection has already received.

The session-layer parts of steps 1 to 3 and 16 (authentication, subscription and the
calendar) may change before the contract is frozen.

## WebSocket session

**Preconditions for the whole script.** The exchange under test has one team with two
registered strategies, `strat-a` and `strat-b`, and one instrument with a two-sided live
quote. A team holds at most one resting order per instrument, side and price, across all
of its strategies (SPEC 8.2), so the script never rests two of the team's orders at one
price and side; step 8 checks that the exchange refuses to.

1. **Connect and authenticate.** `auth`, then `session_ack` naming the team, then
   `calendar` listing the term's own sessions and holidays (the `Calendar` message).
1a. **Instruments.** Directly after step 1's `calendar`, with the next `seq` and no
   message between them, one `instruments` (the `Instruments` message) listing every
   instrument sorted by the byte order of `instrument`, each with `kind`, `tick_size`,
   `lot_size`, `status` and a `tradable` that is present, `false` included, and
   `option_underlyings` naming SPY and GOOGL with their `strike_increment` and
   `contracts`. An `OPTION` entry carries `option` terms and an id in Alpaca's unpadded
   OCC form; an `EQUITY` entry carries none. Precondition: the exchange under test lists
   the instrument of this script. A client that subscribes in step 2 names an
   `instrument` from this table. When the exchange resends `instruments` because a
   listing changed, the client replaces its table with the whole new message.
2. **Subscribe.** `subscribe` for the instrument.
3. **First book.** The subscribe snapshot, a `book` carrying its original `grid_time`
   (or, if the instrument has no book yet this session, its first published book), with
   up to ten `bid_levels` and up to ten `ask_levels`, level 1 of each being the wall edge (SPEC 4.3,
   SPEC 5.3; the `Book` message).
4. **New limit order.** `new` for `strat-a`, a buy strictly inside the band. Step 5's
   cancel is sent immediately after this `new` is sent, without waiting for a response;
   step 4's own confirmations are checked only once step 5's reject has been checked.
   `accepted` with `release_time` equal to `receipt_time` plus δ_oe (SPEC 8.1).
   `order_state` shows `RESTING`.
5. **Early cancel.** A `cancel` at that price and side, sent immediately after step 4's
   `new` is sent (step 4's `accepted` and `order_state` arrive no sooner than
   `receipt_time` plus δ_oe = 150 ms, so waiting for them would put the cancel past the
   50 ms window). `reject` with `reason_code = MIN_REST_VIOLATION` (SPEC 8.1).
6. **Partial fill.** Precondition: a scripted counterparty aggresses part of the order.
   `execution` with `fill_kind = STUDENT_TO_STUDENT`, `liquidity = MAKER`, a positive
   `fee` (the rebate) and a non-zero `remaining_size` (SPEC 9.1, SPEC 12.1). `trades` at
   the next grid boundary carries a `STUDENT_TO_STUDENT` print with no counterparty
   (SPEC 10).
7. **Amend down.** `amend` at the same level with a smaller `new_size` and
   `new_price` equal to `price`. `accepted`; `order_state` shows `remaining_size` equal to
   `new_size`, the new remaining size, and `old_price` equal to `price`, since the amend
   changed only the size (SPEC 8.2; `OrderState.old_price`).
8. **Second strategy at the same price.** `new` for `strat-b` at the same price and side
   as `strat-a`'s resting order. `reject` with `reason_code = DUPLICATE_ORDER_AT_LEVEL`,
   logged at release (SPEC 8.2, SPEC 9.4.3 release step 4). `strat-a`'s order is
   unchanged, and nothing rests for `strat-b`: a script that sees an `accepted` for this
   `new`, or an `order_state` for a `strat-b` order at that price and side, fails this
   step.
9. **Cancel the level.** `cancel` at that price and side. One `order_cancelled`, for
   `strat-a`'s order, with `reason_code = CANCEL_REQUEST`, the cancel's `request_ref` and
   `cancelled_size` equal to the `remaining_size` step 7's `order_state` showed (SPEC 8.2).

   Steps 9a to 9c track a price-moving amend from outbound messages alone. They are
   lettered so that steps 10 to 16 keep their numbers. Every accepted amend sends one
   `order_state` for each order it acts on, carrying `old_price`, the order's price before
   the amend, and the amend's new price as `price`. A client keeping its own view of its
   resting orders applies one rule to it: remove the order at `old_price`, then add it at
   `price` if `state` is `RESTING` or `STALE` (the `OrderState` message).

9a. **Rest for the amend.** `new` for `strat-a`, a buy strictly inside the band at a
    price P1 where the team has no resting order. `accepted`; `order_state` shows
    `RESTING` at P1, with no `old_price`, since it reports no amend.
9b. **Price-moving amend, resting.** At least 50 ms after step 9a's `new` was received
    (SPEC 8.1), `amend` at P1 with `new_price` P2, a buy price strictly inside the band,
    below the best ask so not marketable, where the team has no resting order, and
    `new_size` equal to the size resting at P1. `accepted`; one `order_state` with
    `price` P2, `old_price` P1, `state = RESTING` and `remaining_size` equal to
    `new_size`; no `execution`. A client applying the rule above holds the order at P2
    and nothing at P1.
9c. **Price-moving amend, filling completely.** Precondition: a counterparty of another
    team rests a sell at a price P3 strictly inside the band and above P2, of exactly the
    size resting at P2, with nothing else resting at or below P3 on the ask side. Then
    (the minimum rest still counts from step 9a's `new`, since an amend keeps the order's
    submission time, SPEC 8.1, SPEC 8.2) `amend` at P2 with `new_price` P3 and `new_size`
    equal to that size: marketable, so the order executes at once and fills completely
    (SPEC 8.2). `accepted`; `execution` with `liquidity = TAKER` and `remaining_size: 0`
    on its last fill; then one `order_state` with `price` P3, `old_price` P2,
    `state = FILLED` and `remaining_size: 0`, after the executions. A script that sees no
    `order_state` for this amend, or one without `old_price`, fails this step. Nothing of
    the team rests at P2 or P3 afterwards.
10. **Collar.** `new` for `strat-a`, a buy limit above mark x 1.05. `reject` with
    `reason_code = PRICE_COLLAR` (SPEC 8.1).
11. **Wall sweep with a market order.** Precondition: the instrument's book shows ten ask
    levels, and no counterparty trades against the order other than the wall. `new` for `strat-a`, `order_type = MARKET`,
    larger than the ten displayed ask levels. `execution` rows with
    `fill_kind = STUDENT_TO_WALL`, `liquidity = TAKER`, a negative `fee`, each at its
    level's price; then `order_cancelled` with `reason_code = MARKET_REMAINDER` (SPEC 5.3,
    SPEC 6.3). The next `book` shows a full rebuilt ladder at the shifted band (SPEC 5.4),
    and `trades` shows one `STUDENT_TO_WALL` print per wall fill (SPEC 10).
12. **Self-trade prevention.** Precondition: `strat-b` has a resting sell on the
    instrument. Then `strat-a` sends a buy that would trade with it. `order_cancelled` on
    the resting sell with `reason_code = SELF_TRADE`, and no print for it on `trades`
    (SPEC 8.1).
13. **Mass-cancel.** Rest one order for `strat-a` and one for `strat-b`, each at a price
    and side where the team has no other resting order (SPEC 8.2), then
    `mass_cancel`. One `order_cancelled` per order with `reason_code = MASS_CANCEL`,
    released δ_oe after receipt, and no budget consumed (SPEC 8.2).
14. **Close.** Precondition: the exchange under test runs a single configured session and
    closes it after step 13's cancellations. After step 13's cancellations, `new` for
    `strat-a`, a buy strictly inside the band, and wait for `order_state` showing
    `RESTING`, so at least one order rests into the close. `session_state` with
    `state = CLOSED`; that order, and every other order still resting, gets
    `order_cancelled` with `reason_code = SESSION_CLOSE`. A script that sees no
    `SESSION_CLOSE` cancellation fails this step. A later `new`, sent on the same session
    day, is rejected with `reason_code = RELEASE_AFTER_CLOSE`, not `MARKET_CLOSED`, since
    its δ_oe delay would release it after a close that has already happened, not before an
    open that has not (SPEC 8.1, SPEC 9.4.3 receipt step 4). A script that expects
    `MARKET_CLOSED` here fails this step: that reason is for a receipt before the open or
    on a day with no session, see step 16.
15. **Heartbeat and resume.** Precondition: the exchange under test can be restarted
    between two of the sub-steps, and the close of step 14 has happened before the
    empty-marker sub-step. A "report" below is one of the six private messages that carry a `report_seq`
    on the envelope: `accepted`, `reject` (when it is a reply to an order the exchange
    released), `execution`, `order_cancelled`, `order_state` and `risk_notice`. The
    sub-steps, in order, each on a connection the script opens itself:

    - **Heartbeat, nothing sent by the client.** From the opening of a connection, with
      `auth` sent or not, at any hour (inside a session and outside one), the server
      sends a WebSocket ping and a `heartbeat` 15 s after the connection opens and every
      15 s after that. A `heartbeat` is an envelope with `seq` and `sent_at`, a `seq`
      that increases from one message of the connection to the next, no payload fields
      and no `report_seq`. The client sends nothing, not even a pong, and stays
      connected for as long as the server's own timer allows.
    - **45 s of silence closes with 4000.** A connection on which the server sees no
      inbound frame for 45 s is closed with close code 4000 and reason
      `heartbeat timeout`. The 45 s counts from the later of the connection's opening and
      its last inbound frame: an authenticated client that sends one frame 44 s after
      opening and another 44 s later is still connected 88 s in, and is closed 45 s after
      its last frame. A connection that never authenticates is closed 45 s after it
      opens with the same code and reason, even if it answers every ping with a pong.
    - **Resume inside the window replays.** After `auth` (and the `session_ack` and
      `calendar` that answer it), `resume` with `last_report_seq` L, where
      1 <= L <= H, H is the team's newest `report_seq` and the exchange still holds every
      report above L. Then `resume_ack` with `replayed = true`, `as_of_report_seq` equal to H
      and `snapshot_count = 0`; then every report with `report_seq` above L up to H, in
      `report_seq` order, each with the `report_seq` and the payload bytes it was first
      sent with (its envelope `seq` and `sent_at` are the new connection's); then live
      reports from H + 1, with no gap and no duplicate. Nothing but those reports
      follows the `resume_ack`: no `book`, `trades`, `mark`, `session_state` or
      `order_snapshot`.
    - **Resume with no cursor, orders resting, gets a snapshot.** `resume` with
      `last_report_seq = 0` while the team has k resting orders. `resume_ack` with
      `replayed = false`, `as_of_report_seq` equal to N, the team's newest `report_seq`, and
      `snapshot_count = k`; then k `order_snapshot` messages in level-key order
      (instrument ascending, then `BUY` before `SELL`, then price ascending), each with
      `strat_id`, `instrument`, `side`, `price`, `remaining_size` and `timestamp` and
      with no `report_seq`, `remaining_size` being what is left after partial fills; then
      reports from `report_seq` N + 1. Replay is never partial: an L older than the
      window, above H, or sent to an exchange that has restarted since the report was
      first sent gets this answer too.
    - **Resume across a restart.** After the exchange restarts, a `resume` whose
      `last_report_seq` was inside the window before the restart gets
      `resume_ack(replayed = false)` with `as_of_report_seq` equal to the number of
      reports the team has had in the term, k `order_snapshot` for what rests, and then
      reports from N + 1, numbered as an uninterrupted run would have numbered them.
    - **Resume outside a session, past the window, gets the empty marker.** After the
      close has cancelled every order (step 14), `resume` with a `last_report_seq` the
      exchange cannot replay, for example 0. `resume_ack` with `replayed = false`,
      `as_of_report_seq` equal to the team's newest `report_seq` and
      `snapshot_count = 0`, and no `order_snapshot`. `snapshot_count = 0` is the whole
      answer: the team has no resting order. (A `last_report_seq` the exchange can still
      replay gets the replay of the third sub-step, including the close's
      `SESSION_CLOSE` cancellations.)
    - **Market data is not replayed.** Neither a replay nor a snapshot carries `book`,
      `trades`, `mark` or `session_state`. A client closes a gap in public data by
      sending `subscribe` again, which is answered with the latest `book` of step 3.
16. **Subscribe outside a session.** Precondition: the exchange under test closed the
    instrument's session in step 14 and has not restarted since. After step 14's close, a
    `subscribe` naming the instrument, sent on a new connection authenticated after the
    close (`auth`, `session_ack` and `calendar` are still served outside a session).
    `calendar.next_open` names the next scheduled session, absent if the script's one
    configured session was the term's last. Then, from the `subscribe`: one
    `session_state` with `state = CLOSED`, `session_date`, `open_time` and `close_time`
    naming the session step 14 just closed, and `grid_time` equal to `close_time`; no
    `book`, `trades` or `mark`. One `official_close` for the instrument, since the
    exchange itself closed it.

    The `value` of that `official_close` is the average of the instrument's valid
    one-second marks over the close window: the 300 marks from five minutes before the
    close to one second before it, each with equal weight, frozen marks included and a
    second in which the instrument has no valid mark left out (SPEC 7.1, SPEC 7.2,
    SPEC 9.4.4). This rule is provisional (SPEC 7.1). The script's own orders do not move
    it: the mark is the midpoint of the live quote and is never derived from the
    exchange's own book or any team's trades (SPEC 7.1). An instrument with no valid mark
    in any second of the window gets no `official_close`, and the previous official close
    stays in use (SPEC 7.1, SPEC 7.2).

    Expected value. Precondition: the recorded market session the exchange under test is
    fed holds one valid two-sided quote of QTEA, a bid of 99.99 and an ask of 100.01 at
    the session open, which it never replaces, and no quote of QTEB or QTEC. Each of the
    300 marks of the window is then that quote's midpoint, 100.00, since a quote stays in
    force until it is replaced (SPEC 7.2), so QTEA's `official_close` has
    `value = 100000000`, which is 100.000000 in int64-micro. QTEB and QTEC have no valid
    mark in any second of the window, since each is reference unavailable all session
    (SPEC 7.2), so a `subscribe` that also names either gets no `official_close` for it,
    the script having no earlier session whose close could stay in use (SPEC 7.1). A
    script that sees an `official_close` for QTEB or QTEC, or a `value` for QTEA other
    than `100000000`, fails this step.

## Not scripted yet

- **STALE and purge.** A STALE transition is not reported to the owner, and the purge that
  ends the order is, as `order_cancelled` with `reason_code = PURGE_STALE` (SPEC 4.3,
  SPEC 8.4). Scripting it needs a band move under a resting order, which the scripted
  session does not drive yet.
- **Trade-based matching and residual prints.** Need a scripted live print stream with
  the classifier and carry (SPEC 9.3); deferred to the golden fixtures.
- **Amend price tests.** The rejects of an amend's new price (`DUPLICATE_ORDER_AT_LEVEL`
  where the team already has another resting order at the new price and side,
  `AMEND_PRICE_AT_OR_BEYOND_WALL` and `AMEND_WOULD_MAKE_STALE_MARKETABLE`) are settled
  (SPEC 8.2); scripting them needs two of the team's orders resting at different prices
  on one side, and a band move under a resting order, which the scripted session does not
  drive yet. An amend acts on the team's one resting order at its price.

## History service

The history service lets a team fetch the exchange's own past published market data and,
for its own team only, its own past private reports (SPEC 20.3). These steps use plain
HTTP GETs, no WebSocket session, one step per server MUST in its response rules and
client rules.

**Preconditions for the whole script.** The service under test provides a token it
recognises for team A, a closed session that is cached, a closed session that is not yet
cached and a session that has not closed. Steps H1 to H13 send no `Accept-Encoding`
header (a client library that adds `Accept-Encoding: gzip` on its own is configured not to),
so every body they check is identity; steps H14 to H17 cover the gzip behaviour.

H1. **`Retry-After` on `202`.** GET the single-object endpoint for the closed,
    not-yet-cached session. `202`, `status = "pending"`, and a `Retry-After` header that
    is a non-negative decimal integer, not an HTTP-date.
H2. **`Retry-After` on `429`.** Precondition: the token's rate limit is exhausted when the
    step starts. `429`, `status = "rate_limited"`, `Retry-After` a non-negative decimal
    integer.
H3. **No `Retry-After` on `409`.** GET a single object of the session that has not
    closed. `409`, `status = "not_closed"`, no `Retry-After` header.
H4. **`ETag` on every `200` and `206`** (the `ETag` of the representation sent, here
    identity). Steps H4 to H6 use a cached object of at least
    10 bytes, such as a non-empty book channel. GET it with no `Range`: `200`,
    `ETag` present. GET it with `Range: bytes=0-9` and `If-Range` set to that `ETag`:
    `206`, `ETag` present and equal to the first. Repeat both on
    `GET /v1/history/range` for the same object.
H5. **`Content-Range` total.** The `206` of step H4 carries
    `Content-Range: bytes 0-9/<total>`, `<total>` a decimal integer equal to the
    `Content-Length` of the step's `200`, never `*`.
H6. **No `416`.** GET a cached object with `Range: bytes=<n>-`, `<n>` at or beyond the object's `Content-Length`, and `If-Range` set
    to its `ETag`, then with `Range: bytes=-5`, then with `Range: bytes=0-1,4-5`: each is a
    `200` with the whole object and its `ETag`, never `416` or `206`.
H7. **`200` to a resume request.** GET a cached object with `Range: bytes=10-` and no
    `If-Range`, then with an `If-Range` that is not its `ETag`: each is a `200` with the
    whole object and the object's `ETag`.
H8. **Lowercase hex.** The `ETag` of steps H4 and H7, and every `sha256` in a
    `GET /v1/history/range` manifest `ready` entry, match `[0-9a-f]{64}` (the `ETag`
    inside its quotes).
H9. **Limits are server-side.** `GET /v1/history/range` with a `from`/`to` span above
    the service's configured session-date limit: `400`, `status = "malformed_request"`.
H10. **First value of a repeated key.** `GET /v1/history/range` with
    `channels=book&channels=mark`: the manifest names `book` only, as for `channels=book`.
H11. **Range `Retry-After` and `ETag` transition.** `GET /v1/history/range` naming a
    `pending` triple: `200` with `Retry-After` an integer. Naming only a `not_closed`
    triple: no `Retry-After`. Precondition: the session that has not closed closes between
    two requests of this step, as observed by the service. Take the `ETag` of a range
    naming a triple of that session before its close and repeat after it: the `ETag`
    differs (`not_closed` to `pending`).
H12. **Unbuilt endpoints.** A service that does not yet build an endpoint it documents
    answers it `501`, `status = "not_implemented"`, never `404`. A service that builds
    every documented endpoint has nothing to exercise in this step.
H13. **Unknown range channel.** `GET /v1/history/range` with `channels=book,foo`: `200`,
    with one `unavailable` entry per date and instrument for `foo`, after `book`.
H14. **Which requests get gzip.** GET a cached object with no `Range` and
    `Accept-Encoding: gzip`: `200`, `Content-Encoding: gzip`, an `ETag` equal to the
    identity `ETag` of step H4 with `-gzip` appended inside the quotes, and a body that
    gunzips to the bytes of the identity `200`. Repeat with `x-gzip`. Then with no
    `Accept-Encoding`, with `*`, with `identity`, with `gzip;q=0` and with `gzip;q=x`:
    each is an identity `200` with no `Content-Encoding` header. Repeat on
    `GET /v1/history/range` and on the `session_state` endpoint.
H15. **`Vary`.** Every `200` and `206` of step H14 and of steps H4 to H7, identity or
    gzip, carries `Vary: Accept-Encoding`. A `202`, a `409`, a `404` and the
    `GET /v1/history/sessions` `200` carry no `Vary`, and none is ever gzip-encoded, even
    with `Accept-Encoding: gzip`.
H16. **Identity signal.** GET a cached object with `Accept-Encoding: gzip`, `Range: bytes=0-9`
    and `If-Range` set to its identity `ETag`: `206`, `Content-Encoding: identity`, the
    identity `ETag`. With no `If-Range`: `200`, the whole identity object,
    `Content-Encoding: identity`, never gzip.
H17. **Gzip framing.** The gzip `200` of step H14 carries no `Content-Length` unless it
    equals the encoded body's byte length, and its body ends with an intact gzip trailer
    (CRC and ISIZE check, ISIZE equal to the identity length).
H18. **Private reports.** Precondition: the service holds private reports for team A and
    for a second team B in the same closed session. With team A's token, GET
    `/v1/history/{session_date}/A/reports`
    for a closed session of the token's term: `200`, `application/x-ndjson`, every line a
    report of team A and none of any other team, `seq` running 1, 2, 3 and so on, and each
    payload the bytes A's connection received live. With the same token, GET the same
    path for team B: `403`, `status = "forbidden"`, with none of B's bytes in the body,
    whether or not B holds a token, and also for a `session_date` that does not exist,
    that has not closed, or that lies outside the token's term. For A's own reports of a
    session that has not closed: `409`, `status = "not_closed"`; of a scheduled, closed
    session not yet rebuilt: `202`, `status = "pending"`, with `Retry-After`; of a date
    inside the term's range that is not a session day: `404`, `status = "unavailable"`.
H19. **Private reports range.** Precondition: as for step H18, over several closed
    sessions of the token's term. With team A's token, GET
    `/v1/history/range/A/reports?from=D1&to=D2`: `200`, `application/x-ndjson`, one
    manifest line naming each calendar
    day from D1 to D2 by `session_date` and `status`, then the `ready` sessions' bytes
    back to back; slicing the body by the manifest, each slice's SHA-256 equals its
    `sha256` and its bytes equal the body of GET `/v1/history/{date}/A/reports` for that
    date. A scheduled, closed, not yet rebuilt session is `pending` with `Retry-After`
    on the response, an unclosed one `not_closed`, a weekend inside the term
    `unavailable`. With the same token and team B in the path: `403`,
    `status = "forbidden"`, none of B's bytes. With `from` or `to` outside the token's
    term (for example the day before the term's first date): `403` for the whole request,
    no manifest. `from` after `to`, or a span over the session-date limit: `400`. Range
    resume and gzip behave as in H14 and H16.
