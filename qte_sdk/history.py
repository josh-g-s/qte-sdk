"""Fetch past published market data from the exchange's history service.

    client = HistoryClient()  # address from QTE_HISTORY_URL, token from QTE_TOKEN(_FILE)
    async for item in client.fetch("2026-09-29", "AAPL", "book"):
        match item:
            case Book():
                ...
            case Unknown() | DecodeFailed():
                ...  # a message this SDK cannot use: report it, do not ignore it

The history service serves each closed session's published market data again, message for
message as the live feed carried it, so the items are the same generated classes that
`qte_sdk.market_data.market_data` yields and your handling code works on both. It serves
only what was published to everyone: there is no raw feed and no other team's data here.

Each request is a plain HTTPS GET authenticated with your team token. There is no
session, nothing to subscribe to and nothing to close. The service's address comes from
the `url` argument or the `QTE_HISTORY_URL` environment variable; there is no default.

- `fetch(session_date, instrument, channel)`: one session's `book`, `trades` or `mark`
  messages for one instrument, in publication order.
- `fetch_session_state(session_date)`: one session's `session_state` messages.
- `fetch_range(from_date, to_date, instruments, channels)`: several instruments and
  channels over a span of session dates in one response. It yields a `Manifest` first,
  which says which (session date, instrument, channel) entries are included, and then
  the messages of each included entry in the manifest's order.

A session that has closed but is not ready yet is `pending`: `fetch` waits and asks again
as the service's `Retry-After` header says, up to `max_wait` seconds in all, then raises
`HistoryPending`. Data that will never exist (a date before the service's coverage, a day
with no session, an instrument or channel it does not know) raises `HistoryUnavailable`
at once. A session that has not closed yet (one running now, or one in the future) is
`not_closed`: nothing is served for it before its close, and `fetch` raises
`HistoryNotClosed` at once, without waiting, since nothing is being built for it yet.
`fetch_range` never waits: its manifest marks each entry `ready`, `pending`,
`not_closed` or `unavailable`, and only `ready` entries are included in that response.
An endpoint the service does not serve yet raises `HistoryNotImplemented`, which, unlike
`HistoryUnavailable`, is not permanent.

The service may add new status words. The HTTP status code decides which error is raised,
and the service's own word is kept in `HistoryError.status`; a manifest entry with a
status this SDK does not know is passed on in the `Manifest` as it is, and its data is not
included.

Downloads are uncompressed so that a dropped connection can be resumed: the client asks
for the rest of the same data with an HTTP `Range` request, up to `max_resumes` times.
The service may answer with the whole data again instead of the rest. If its `ETag` shows
the data is unchanged, the bytes already delivered are read and discarded, so nothing is
yielded twice; if the data changed, `HistoryChanged` is raised. The client checks what
it received against the SHA-256 digest the service states for it (the `ETag` of a single
stream, the manifest's `sha256` of each range entry) and raises
`HistoryCorrupt` on a mismatch, after the messages have been yielded. A response without
the identity `ETag` the service sends is refused before any message, since it could be
neither checked nor resumed.

Every error this module raises has a code, such as `QTE-HISTORY-PENDING`, in its `code`
attribute and at the start of its message: docs/errors.md says what each means and how to
fix it. A network error, such as `TimeoutError`, keeps its own type and has none.

Cancelling a fetch, for example with `asyncio.timeout`, ends it at once, on Windows as on
macOS and Linux. The request and each read of the response run in a worker thread, and
the cancel wakes that thread wherever it waits on the service: in the TLS handshake,
sending the request, waiting for the answer, reading the body or resuming. The connection
is closed and the thread ends without waiting for `timeout`, so a cancelled fetch neither
holds a connection open, nor holds up the event loop, nor keeps the program from exiting.
Only looking up the service's address and opening the TCP connection cannot be woken: a
thread cancelled during them finishes that step and stops without sending the request.
`timeout` bounds each attempt to open the connection, one for each address the lookup
gives, but the lookup is bounded only by your system's resolver. A response that arrives
just as the fetch is cancelled is closed too. The cancelled task sees only
`asyncio.CancelledError`. Closing a fetch part way, with `aclose()`, closes its
connection as well.

Credentials: the token is sent only in the `Authorization` header and is kept to the same
standard as `qte_sdk.connection`: it never appears in a log record, an exception message,
attribute or chain, or a traceback local variable that this module creates or lets escape.
Errors from the network keep their type but lose their traceback and chain, because the
HTTP library's frames hold the request headers. Response headers and status lines are not
put in errors, and a header that may reflect the token is treated as absent. The `message`
of an error body is server text, passed on with the token removed should it ever appear,
or withheld if part of the token remains. An error raised by the fetch methods leaves
without its chain or the traceback it gathered inside the download, whose frames hold
the response's bytes. A line that fails to decode is reported as a `DecodeFailed` whose
error is a new `ValueError` naming only the type of the parser's error, since that error
keeps the line, which could reflect the token. Redirects are not followed, and a plain
`http://` address is refused unless it is this machine's own (a local test server). An
IPv6 address goes in square brackets, with or without a port, such as `http://[::1]:8080`.
"""

import asyncio
import hashlib
import http.client
import io
import ipaddress
import json
import logging
import math
import os
import re
import select
import socket
import ssl
import threading
import time
from collections.abc import AsyncGenerator, Iterable
from contextlib import aclosing
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, TypeVar, cast
from urllib.parse import quote, urlsplit

from qte_sdk import errors as _errors
from qte_sdk.connection import DecodeFailed, Unknown, _decode_error
from qte_sdk.contract import codec
from qte_sdk.contract.registry import INBOUND
from qte_sdk.errors import QteError, flatten, plain
from qte_sdk.market_data import MarketData
from qte_sdk.session import _holds_token, _redact, _Secret, _url_holds_token, resolve_token

__all__ = [
    "DEFAULT_MAX_RESUMES",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_MAX_WAIT",
    "DEFAULT_TIMEOUT",
    "HISTORY_URL_ENV_VAR",
    "HistoryAddressInvalid",
    "HistoryChanged",
    "HistoryClient",
    "HistoryCorrupt",
    "HistoryError",
    "HistoryForbidden",
    "HistoryInterrupted",
    "HistoryItem",
    "HistoryNotClosed",
    "HistoryNotImplemented",
    "HistoryPending",
    "HistoryRateLimited",
    "HistoryRequestRejected",
    "HistoryUnauthenticated",
    "HistoryUnavailable",
    "Manifest",
    "ManifestEntry",
    "MissingHistoryURL",
    "TokenMalformed",
]

HISTORY_URL_ENV_VAR = "QTE_HISTORY_URL"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_WAIT = 60.0
DEFAULT_MAX_RETRIES = 10
DEFAULT_MAX_RESUMES = 3

_CHUNK = 64 * 1024
_ERROR_BODY_LIMIT = 64 * 1024
_NDJSON = "application/x-ndjson"
_HEX_DIGEST = re.compile(r"[0-9a-fA-F]{64}")  # the shape; compared exactly, as sent
# An ETag: the lowercase hex SHA-256 digest, quoted, with "-gzip" inside the quotes on the
# compressed form of the same data.
_ETAG = re.compile(r'"([0-9a-f]{64})(-gzip)?"')
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")

_log = logging.getLogger(__name__)

_T = TypeVar("_T")
_sleep = asyncio.sleep  # a module attribute, so tests can observe the waits


HistoryItem = MarketData | Unknown | DecodeFailed
"""What `fetch` and `fetch_session_state` yield: a message of a type this SDK knows, one
of a type it does not (`Unknown`), or a line that could not be decoded (`DecodeFailed`)."""


class MissingHistoryURL(QteError, ValueError):
    """No address was given and the `QTE_HISTORY_URL` environment variable is unset or
    empty. Code `QTE-HISTORY-ADDRESS-MISSING`."""

    code = _errors.HISTORY_ADDRESS_MISSING


class HistoryAddressInvalid(QteError, ValueError):
    """The history service's address is not one the client will send the token to: not an
    `https://` URL (`http://` only to this machine), or one with credentials, a query, a
    fragment or a port that is not a number. The message never shows the address, which a
    mistake could have filled with the token. Code `QTE-HISTORY-ADDRESS-INVALID`."""

    code = _errors.HISTORY_ADDRESS_INVALID


class TokenMalformed(QteError, ValueError):
    """The token has a character no token has, one an HTTP header cannot carry, such as a
    newline or a letter outside ASCII. The message never shows the token. Code
    `QTE-TOKEN-MALFORMED`."""

    code = _errors.TOKEN_MALFORMED


class HistoryError(QteError, Exception):
    """A history request failed.

    `http_status` is the HTTP status of the response, if one arrived. `status` is the
    service's own status token (for example `unavailable`) and `message` its explanation,
    when the response carried them.

    Each subclass has its own code, such as `QTE-HISTORY-UNAVAILABLE`. A `HistoryError`
    itself has `QTE-HISTORY-BAD-RESPONSE` for a response the service would not send,
    `QTE-HISTORY-UNEXPECTED-STATUS` for an HTTP status this SDK does not know, and
    otherwise `QTE-HISTORY-REQUEST-FAILED`.
    """

    code = _errors.HISTORY_REQUEST_FAILED

    def __init__(
        self,
        text: str,
        *,
        http_status: int | None = None,
        status: str | None = None,
        message: str | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(text, code=code, fields={"what": plain(text)})
        self.http_status = http_status
        self.status = status
        self.message = message


class HistoryUnavailable(HistoryError):
    """The data will never exist: a date before the service's coverage, a day with no
    session, or an instrument or channel the service does not know. Do not retry. Code
    `QTE-HISTORY-UNAVAILABLE`."""

    code = _errors.HISTORY_UNAVAILABLE


class HistoryNotImplemented(HistoryError):
    """The service does not serve this endpoint yet. It is not a statement that the data
    will never exist (that is `HistoryUnavailable`): ask again after a later release of
    the service. Code `QTE-HISTORY-NOT-IMPLEMENTED`."""

    code = _errors.HISTORY_NOT_IMPLEMENTED


class HistoryNotClosed(HistoryError):
    """The session has not closed yet: it is running now, or it lies in the future.
    Nothing is served for a session before its close, and nothing is being built for it
    yet, so the client does not wait. Ask again after the session's close. Code
    `QTE-HISTORY-NOT-CLOSED`."""

    code = _errors.HISTORY_NOT_CLOSED


class HistoryPending(HistoryError):
    """The session has closed but its data is not ready yet, and waiting for it would have
    gone past `max_wait` or `max_retries`. It will become ready: ask again later. Code
    `QTE-HISTORY-PENDING`.

    `retry_after` is the service's suggested wait in seconds, or None if it gave none.
    """

    code = _errors.HISTORY_PENDING

    def __init__(self, text: str, *, retry_after: float | None, **details: Any) -> None:
        super().__init__(text, **details)
        self.retry_after = retry_after


class HistoryRateLimited(HistoryError):
    """Too many requests for this token, and waiting would have gone past `max_wait` or
    `max_retries`. `retry_after` is the service's suggested wait in seconds, or None. Code
    `QTE-HISTORY-RATE-LIMITED`."""

    code = _errors.HISTORY_RATE_LIMITED

    def __init__(self, text: str, *, retry_after: float | None, **details: Any) -> None:
        super().__init__(text, **details)
        self.retry_after = retry_after


class HistoryUnauthenticated(HistoryError):
    """The token is missing or not recognised. Code `QTE-HISTORY-UNAUTHENTICATED`."""

    code = _errors.HISTORY_UNAUTHENTICATED


class HistoryForbidden(HistoryError):
    """The token may not read this data. Code `QTE-HISTORY-FORBIDDEN`."""

    code = _errors.HISTORY_FORBIDDEN


class HistoryRequestRejected(HistoryError):
    """The request is wrong on its face, for example a date that does not parse, `to_date`
    before `from_date`, or a range larger than the service allows. Retrying the same
    request fails the same way. Code `QTE-HISTORY-REQUEST-REJECTED`."""

    code = _errors.HISTORY_REQUEST_REJECTED


class HistoryInterrupted(HistoryError):
    """The connection dropped during a download and could not be resumed. The messages
    yielded before it are incomplete, and were not checked against the stated digest.
    `bytes_received` counts what arrived. Code `QTE-HISTORY-INTERRUPTED`."""

    code = _errors.HISTORY_INTERRUPTED

    def __init__(self, text: str, *, bytes_received: int) -> None:
        super().__init__(text)
        self.bytes_received = bytes_received


class HistoryChanged(HistoryError):
    """A dropped download could not be resumed where it stopped, because the service now
    serves different data for the same request (for `fetch_range`, typically an entry that
    was pending has become ready). What was already yielded cannot be continued: start
    the download again. Code `QTE-HISTORY-CHANGED`."""

    code = _errors.HISTORY_CHANGED


class HistoryCorrupt(HistoryError):
    """What arrived does not match the length or SHA-256 digest the service stated for it,
    so the messages already yielded may be wrong. Code `QTE-HISTORY-CORRUPT`."""

    code = _errors.HISTORY_CORRUPT


@dataclass(frozen=True)
class ManifestEntry:
    """One requested (session date, instrument, channel) of a `fetch_range` response.

    `status` is `ready`, `pending` (closed, still being built), `not_closed` (the
    session has not closed yet; ask again after its close) or `unavailable` (will never
    exist). Only a `ready` entry's messages are in the response; for it, `byte_offset`
    and `length` locate its bytes after the manifest line and `sha256` is their digest,
    and all three are None otherwise.
    """

    session_date: str
    instrument: str
    channel: str
    status: str
    byte_offset: int | None
    length: int | None
    sha256: str | None


@dataclass(frozen=True)
class Manifest:
    """The first item `fetch_range` yields: every requested entry, in the order their
    messages follow, whether or not it is included.

    `retry_after` is set when at least one entry is `pending`: the service's suggested
    wait in seconds before asking again for the range, or None if it gave none. A
    `not_closed` entry does not set it. Read each entry's `status` to tell which entries
    are missing and whether they ever will arrive.
    """

    entries: tuple[ManifestEntry, ...]
    retry_after: float | None

    @property
    def pending(self) -> tuple[ManifestEntry, ...]:
        """The entries that are not ready yet but will be."""
        return tuple(entry for entry in self.entries if entry.status == "pending")

    @property
    def not_closed(self) -> tuple[ManifestEntry, ...]:
        """The entries whose session has not closed yet."""
        return tuple(entry for entry in self.entries if entry.status == "not_closed")


class HistoryClient:
    """A client for the exchange's history service.

    `url` is the service's address (`https://...`), or None to read the `QTE_HISTORY_URL`
    environment variable; it is never read from `.env`, and `QTE_URL` is the exchange's
    address, not this service's. `token` is your team token, or None to read `QTE_TOKEN`,
    `QTE_TOKEN_FILE` or `.env` (see `qte_sdk.session.resolve_token`). Raises
    `MissingHistoryURL` or `qte_sdk.session.MissingToken` if either is missing.

    `timeout` bounds each network operation (connecting, or one read), in seconds; a
    network failure raises the usual Python error, such as `TimeoutError`. A cancelled
    fetch stops without waiting out `timeout`, unless it is still opening its connection
    (see the module docstring). `max_wait`
    bounds the total of the waits the service asks for, with `pending` or rate-limited
    answers, before one request raises instead of waiting again, and `max_retries` the
    number of times it asks again; set `max_retries` to 0 to never ask again.
    `max_resumes` bounds how many times one download resumes after its connection drops.
    `ssl_context` replaces the default certificate checks, for example to trust a test
    certificate authority.

    Raises `TokenMalformed` for a token an HTTP header cannot carry, and
    `HistoryAddressInvalid` for an address it will not send the token to; both are
    `ValueError`s, and neither message shows the token or the address.
    """

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_wait: float = DEFAULT_MAX_WAIT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_resumes: int = DEFAULT_MAX_RESUMES,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._secret = _Secret(resolve_token(token))
        del token
        if not (self._secret.value.isascii() and self._secret.value.isprintable()):
            # Checked here, since the HTTP library's own error would quote the header.
            del url  # it could hold the token too, so it stays out of the traceback
            raise TokenMalformed(
                "the token has a character an HTTP header cannot carry, such as a newline "
                "or a non-ASCII character; check how it was copied",
                fields={},
            )
        if url is None:
            url = os.environ.get(HISTORY_URL_ENV_VAR)
        if not url:
            raise MissingHistoryURL(
                f"no history service address: pass url= or set {HISTORY_URL_ENV_VAR}",
                fields={},
            )
        failure: HistoryAddressInvalid | None = None
        try:
            parsed = _parse_url(url)
        except HistoryAddressInvalid as error:
            # Without _parse_url's frame, whose locals hold the address.
            failure = _stripped(error)
        if failure is not None:
            del url  # a mistake can put the token in it, so it stays out of the traceback
            raise failure
        self.url = url
        self._https, self._host, self._port, self._prefix = parsed
        self.timeout = timeout
        self.max_wait = max_wait
        self.max_retries = max_retries
        self.max_resumes = max_resumes
        self._ssl_context = ssl_context

    def __repr__(self) -> str:
        # A mistake can put the token in the address, and a traceback that shows locals
        # shows this.
        url = getattr(self, "url", None)
        if url is not None and _url_holds_token(url, self._secret):
            url = _errors.WITHHELD
        return f"HistoryClient({url!r})"

    def fetch(
        self, session_date: date | str, instrument: str, channel: str
    ) -> AsyncGenerator[HistoryItem, None]:
        """Yield one session's `book`, `trades` or `mark` messages for one instrument, in
        the order they were published.

        A day with nothing published yields nothing. Raises `HistoryUnavailable` if the
        data will never exist, and `HistoryPending` if it is still not ready after waiting.
        """
        return _Guarded(self._fetch(session_date, instrument, channel))

    async def _fetch(
        self, session_date: date | str, instrument: str, channel: str
    ) -> AsyncGenerator[HistoryItem, None]:
        target = self._target(_date_text(session_date), instrument, channel)
        async with aclosing(self._download(target, framed=False)) as lines:
            async for line in lines:
                assert isinstance(line, bytes)
                yield _decode_line(line)

    def fetch_session_state(self, session_date: date | str) -> AsyncGenerator[HistoryItem, None]:
        """Yield one session's `session_state` messages, in the order they were published."""
        return _Guarded(self._fetch_session_state(session_date))

    async def _fetch_session_state(
        self, session_date: date | str
    ) -> AsyncGenerator[HistoryItem, None]:
        target = self._target(_date_text(session_date), "session_state")
        async with aclosing(self._download(target, framed=False)) as lines:
            async for line in lines:
                assert isinstance(line, bytes)
                yield _decode_line(line)

    def fetch_range(
        self,
        from_date: date | str,
        to_date: date | str,
        instruments: Iterable[str],
        channels: Iterable[str],
    ) -> AsyncGenerator[Manifest | HistoryItem, None]:
        """Yield a `Manifest`, then the messages of each included entry in its order.

        `from_date` and `to_date` are session dates, both included. `channels` are drawn
        from `book`, `trades` and `mark`; `session_state` is only served by
        `fetch_session_state`. Entries are ordered by session date, then instrument, then
        channel. A message's own `seq` restarts at 1 for each entry. This does not wait
        for pending entries: check `Manifest.pending` and ask again later for those.
        """
        return _Guarded(self._fetch_range(from_date, to_date, instruments, channels))

    async def _fetch_range(
        self,
        from_date: date | str,
        to_date: date | str,
        instruments: Iterable[str],
        channels: Iterable[str],
    ) -> AsyncGenerator[Manifest | HistoryItem, None]:
        query = (
            f"from={quote(_date_text(from_date), safe='')}"
            f"&to={quote(_date_text(to_date), safe='')}"
            f"&instruments={_list_param(instruments, 'instruments')}"
            f"&channels={_list_param(channels, 'channels')}"
        )
        target = f"{self._target('range')}?{query}"
        async with aclosing(self._download(target, framed=True)) as items:
            async for item in items:
                yield item if isinstance(item, Manifest) else _decode_line(item)

    def _target(self, *segments: str) -> str:
        return self._prefix + "/v1/history/" + "/".join(quote(s, safe="") for s in segments)

    async def _download(
        self, target: str, *, framed: bool
    ) -> AsyncGenerator[bytes | Manifest, None]:
        """The response's lines, fetched incrementally, resumed after a drop and checked
        against the stated digests once complete. A range response's manifest line comes
        first, as a `Manifest`."""
        reply = await self._first_reply(target)
        try:
            etag = cast(_Validator, reply.etag)
            total = reply.length
            if framed:
                progress: _Progress = _FramedProgress(reply.retry_after)
            else:
                # A single stream's identity ETag is the SHA-256 digest of its bytes.
                progress = _StreamProgress(etag.value.strip('"'))
            resumes = 0
            # Bytes at the start of a whole-object answer to a resume that were already
            # delivered, to be read and discarded before anything is parsed again.
            skip = 0
            while True:
                dropped: str | None = None
                try:
                    chunk = await asyncio.to_thread(reply.response.read1, _CHUNK)
                except (OSError, http.client.HTTPException) as error:
                    chunk, dropped = b"", type(error).__name__
                if chunk and skip:
                    discarded = min(skip, len(chunk))
                    chunk, skip = chunk[discarded:], skip - discarded
                    if not chunk:
                        continue
                if chunk:
                    if total is not None and progress.received + len(chunk) > total:
                        raise HistoryCorrupt("the response is longer than the service stated")
                    for item in progress.feed(chunk):
                        yield item
                    continue
                # Ending while bytes already delivered are still being discarded is a
                # drop too, whether or not the length was stated.
                finished = skip == 0 and (total is None or progress.received == total)
                if dropped is None and finished:
                    break
                reply.close()
                if dropped is None:
                    dropped = f"closed after {progress.received} bytes"
                if resumes >= self.max_resumes:
                    raise HistoryInterrupted(
                        f"the download was interrupted ({dropped}) and resumes are used up",
                        bytes_received=progress.received,
                    )
                resumes += 1
                _log.debug(
                    "history download dropped (%s); resuming at byte %d", dropped, progress.received
                )
                reply, skip = await self._resume(target, progress.received, etag, total)
                # A length stated earlier still holds if this answer states none.
                if reply.complete_length is not None:
                    total = reply.complete_length
            for item in progress.finish(total):
                yield item
        finally:
            reply.close()

    async def _first_reply(self, target: str) -> "_Reply":
        waited = 0.0
        retries = 0
        while True:
            reply = await self._call(target, None)
            if reply.status == 200:
                if not reply.ndjson_identity:
                    reply.close()
                    raise HistoryError(
                        "the response is not uncompressed NDJSON as the history service sends",
                        http_status=200,
                        code=_errors.HISTORY_BAD_RESPONSE,
                    )
                if reply.etag is None:
                    # Without it the data can be neither checked nor resumed.
                    reply.close()
                    raise HistoryError(
                        "the response carries no identity ETag as the history service sends",
                        http_status=200,
                        code=_errors.HISTORY_BAD_RESPONSE,
                    )
                return reply
            reply.close()
            if reply.status in (202, 429):
                error = _error_for(reply, self._secret)
                assert isinstance(error, HistoryPending | HistoryRateLimited)
                wait = error.retry_after
                if wait is None or retries >= self.max_retries or waited + wait > self.max_wait:
                    raise error
                _log.debug(
                    "history %s not ready (HTTP %d); asking again in %s s",
                    target,
                    reply.status,
                    wait,
                )
                await _sleep(wait)
                waited += wait
                retries += 1
                continue
            raise _error_for(reply, self._secret)

    async def _resume(
        self, target: str, offset: int, etag: "_Validator", total: int | None
    ) -> "tuple[_Reply, int]":
        """The rest of the data from `offset`, checked to be the rest of the same data,
        and how many bytes at its start to discard as already delivered.

        The service may answer with the whole object (200) rather than the rest (206), for
        example when it does not support the range asked for. If its ETag, with any
        `-gzip` suffix set aside, is the one held, the data is unchanged: the body starts
        at byte 0, so the first `offset` bytes are discarded. Otherwise the data changed.
        """
        reply = await self._call(target, (offset, etag))
        if reply.status == 200:
            if reply.etag_digest is None or reply.etag_digest != etag:
                reply.close()
                raise HistoryChanged(
                    "the service now serves different data; start the download again",
                    http_status=200,
                )
            if not reply.ndjson_identity:
                reply.close()
                raise HistoryError(
                    "the response is not uncompressed NDJSON as the history service sends",
                    http_status=200,
                    code=_errors.HISTORY_BAD_RESPONSE,
                )
            if total is not None and reply.complete_length not in (None, total):
                reply.close()
                raise HistoryChanged(
                    "the service now serves different data; start the download again",
                    http_status=200,
                )
            return reply, offset
        if reply.status != 206:
            reply.close()
            raise _error_for(reply, self._secret)
        if not reply.ndjson_identity or reply.continues_from is None:
            reply.close()
            raise HistoryError(
                "the resumed response is not a well-formed slice of uncompressed NDJSON",
                http_status=206,
                code=_errors.HISTORY_BAD_RESPONSE,
            )
        changed = total is not None and reply.complete_length != total
        if changed or (reply.has_etag and reply.etag != etag):
            reply.close()
            raise HistoryChanged(
                "the service now serves different data; start the download again",
                http_status=206,
            )
        if reply.continues_from != offset:
            reply.close()
            raise HistoryError(
                "the resumed response does not continue from where the download stopped",
                http_status=206,
                code=_errors.HISTORY_BAD_RESPONSE,
            )
        return reply, 0

    async def _call(self, target: str, resume: "tuple[int, _Validator] | None") -> "_Reply":
        handoff = _Handoff()
        try:
            return await asyncio.to_thread(self._request_safely, target, resume, handoff)
        except BaseException:
            # Cancelled, most likely: wake the worker thread at once, and leave it to
            # close the connection, and any response that arrives after this, on its way
            # out. The error was already made safe.
            handoff.abandon()
            raise

    def _request_safely(
        self, target: str, resume: "tuple[int, _Validator] | None", handoff: "_Handoff"
    ) -> "_Reply":
        """`_request`, with any error made safe here in the worker thread, before anything
        (such as the awaiting task) can keep a reference to the original."""
        failure: BaseException
        try:
            return handoff.deliver(self._request(target, resume, handoff))
        except BaseException as error:
            failure = _sanitised(error, self._secret)
        # Raised outside the handler, so the original error, whose traceback holds the
        # HTTP library's frames and their copy of the request headers, is not chained.
        raise failure

    def _connect(self, handoff: "_Handoff") -> http.client.HTTPConnection:
        """A connection to the service, its socket handed to `handoff` before anything is
        sent or awaited on it, so a cancel can wake this thread from the TLS handshake on.
        Runs in a worker thread. Raises if the fetch was cancelled while connecting."""
        conn: http.client.HTTPConnection
        context: ssl.SSLContext | None = None
        if self._https:
            context = self._ssl_context or ssl.create_default_context()
            conn = http.client.HTTPSConnection(
                self._host, self._port, timeout=self.timeout, context=context
            )
        else:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=self.timeout)
        try:
            # The TCP connection alone, even for https: `HTTPSConnection.connect` would make
            # the TLS handshake too, before the socket could be handed over.
            http.client.HTTPConnection.connect(conn)
            if context is not None:
                # As `HTTPSConnection.connect` does, but with the handshake made separately.
                conn.sock = context.wrap_socket(
                    conn.sock, server_hostname=conn.host, do_handshake_on_connect=False
                )
            stream = _WakeableSocket(conn.sock, self.timeout)
            conn.sock = stream  # type: ignore[assignment]
            handoff.attach(stream)
            if context is not None:
                # Bounded by the connection's timeout, as each read is.
                stream.do_handshake()
            return conn
        except BaseException:
            conn.close()
            raise

    def _request(
        self, target: str, resume: "tuple[int, _Validator] | None", handoff: "_Handoff"
    ) -> "_Reply":
        """Send one GET and read the response status and headers. Runs in a worker thread."""
        conn = self._connect(handoff)
        try:
            conn.putrequest("GET", target, skip_accept_encoding=True)
            # Uncompressed, so byte offsets are valid for a resume.
            conn.putheader("Accept-Encoding", "identity")
            conn.putheader("Authorization", "Bearer " + self._secret.value)
            if resume is not None:
                conn.putheader("Range", f"bytes={resume[0]}-")
                conn.putheader("If-Range", resume[1].value)
            conn.endheaders()
            # Kept, since the connection lets go of its socket once a response ends it.
            sock = cast(_WakeableSocket, conn.sock)
            response = conn.getresponse()
            _log.debug("history GET %s: HTTP %d", target, response.status)
            reply = _Reply(conn, sock, response, self._secret)
            if response.status not in (200, 206):
                reply.body = response.read(_ERROR_BODY_LIMIT)
                reply.close()
            return reply
        except BaseException:
            conn.close()
            raise


class _Handoff:
    """Passes a response from the worker thread to the task awaiting it, or, once that
    task has given up on it, has the worker close it instead.

    The worker hands over its socket as soon as it is connected. Giving up wakes the
    worker at once from the TLS handshake, from sending the request or from waiting for
    the answer (see `_WakeableSocket`), so it closes the connection and ends rather than
    waiting for its timeout. Only the wake happens on the task's side: the worker closes
    what it holds, so the two threads never both work on the connection's objects. A
    response that arrives after the task has given up is closed by the worker too. This
    works without the event loop, which may already have stopped by the time the worker
    finishes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._abandoned = False
        self._sock: _WakeableSocket | None = None
        self._reply: _Reply | None = None

    def attach(self, sock: "_WakeableSocket") -> None:
        """Hold the worker's socket, to wake if the task gives up. Raises if it already
        has, before anything is sent: the worker then closes the connection."""
        with self._lock:
            if not self._abandoned:
                self._sock = sock
                return
        raise ConnectionAbortedError("the fetch was cancelled")

    def deliver(self, reply: "_Reply") -> "_Reply":
        with self._lock:
            if not self._abandoned:
                self._reply = reply
                return reply
        reply.close()
        return reply

    def abandon(self) -> None:
        with self._lock:
            self._abandoned = True
            sock, self._sock = self._sock, None
            reply, self._reply = self._reply, None
        if reply is not None:
            # Delivered, so the worker is done with it.
            reply.close()
        elif sock is not None:
            sock.wake()


class _WakeableSocket:
    """The connection's socket, plain or TLS, as `http.client` uses it, with every wait on
    the service one that another thread can end at once with `wake`.

    Shutting a socket down wakes a thread blocked on it on macOS and Linux but not on
    Windows, where only closing it does; and closing a socket that another thread is
    still using could leave that thread working on a new connection given the same
    handle (OpenSSL keeps the number, not the socket). So the socket is non-blocking, and
    each operation waits for it together with one end of a socket pair. `wake` marks this
    socket woken and writes to the other end: a thread waiting here raises
    `ConnectionAbortedError` at once, and every later operation raises it before it
    touches the connection, so nothing more is sent once `wake` has returned. Another
    thread may wake it at any time, but reads, writes and closes it only when no worker
    is using it: a response is closed through its reader, whose lock a read holds, after
    a wake has ended that read.

    `timeout` bounds each operation (the handshake, sending the request, one read), as a
    socket's own timeout does, and an operation that runs it out raises `TimeoutError`.
    Nothing here goes through `ssl.SSLSocket.shutdown` or `unwrap`, which drop the TLS
    layer, so nothing is ever written in the clear.
    """

    def __init__(self, sock: socket.socket, timeout: float | None) -> None:
        self._lock = threading.Lock()
        self._woken = False
        self._closed = False  # closed, though a reader made by `makefile` may be open
        self._io_refs = 0
        self._timeout = timeout
        self._wake_r: socket.socket | None
        self._wake_w: socket.socket | None
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)
        sock.setblocking(False)
        self._sock: socket.socket | None = sock

    def wake(self) -> None:
        """End any wait on this socket at once, and every later operation. Safe from any
        thread, at any time, even after the socket is closed."""
        with self._lock:
            self._woken = True
            if self._wake_w is not None:
                try:
                    self._wake_w.send(b"\0")
                except OSError:
                    pass  # full already, so a wait sees it anyway

    def fileno(self) -> int:
        return -1 if self._sock is None else self._sock.fileno()

    def recv_into(self, buffer: Any) -> int:
        return cast(int, self._io("recv_into", buffer, write=False))

    def sendall(self, data: Any) -> None:
        with memoryview(data) as view, view.cast("B") as rest:
            sent = 0
            deadline = self._deadline()
            while sent < len(rest):
                # A bound on the whole, as `socket.sendall` has, even if each send succeeds.
                if sent and deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("timed out")
                sent += self._io("send", rest[sent:], write=True, deadline=deadline)

    def do_handshake(self) -> None:
        self._io("do_handshake", write=False)

    def makefile(self, mode: str = "rb", buffering: int | None = None) -> io.BufferedReader:
        """A buffered reader of the socket, as `socket.makefile` makes: `http.client` reads
        the response through it."""
        if mode != "rb":
            raise ValueError("only reading in binary is supported")
        with self._lock:
            self._io_refs += 1
        raw = socket.SocketIO(self, "rb")  # type: ignore[arg-type]
        return io.BufferedReader(raw, buffering or io.DEFAULT_BUFFER_SIZE)

    def _decref_socketios(self) -> None:
        with self._lock:
            self._io_refs -= 1
            closing = self._closed and self._io_refs <= 0
        if closing:
            self._close_now()

    def close(self) -> None:
        """Close the socket, or, as `socket.close` does, once the readers made by
        `makefile` are closed too."""
        with self._lock:
            self._closed = True
            closing = self._io_refs <= 0
        if closing:
            self._close_now()

    def _close_now(self) -> None:
        with self._lock:
            sock, self._sock = self._sock, None
            ends = (self._wake_r, self._wake_w)
            self._wake_r = self._wake_w = None
        if sock is not None:
            sock.close()
        for end in ends:
            if end is not None:
                end.close()

    def _deadline(self) -> float | None:
        return None if self._timeout is None else time.monotonic() + self._timeout

    def _io(self, operation: str, *args: Any, write: bool, deadline: float | None = None) -> Any:
        """The socket's method `operation`, with `args`, waiting as it needs, until it
        completes, the socket is woken or `deadline` (by default, the timeout from now)
        passes."""
        if deadline is None:
            deadline = self._deadline()
        while True:
            # Checked and tried under the lock `wake` takes, so once a wake has returned,
            # nothing more is attempted. Each try is non-blocking, so it holds it briefly.
            with self._lock:
                if self._woken:
                    raise ConnectionAbortedError("the fetch was cancelled")
                sock, wake = self._sock, self._wake_r
                if sock is None or wake is None:
                    raise OSError("the connection is closed")
                try:
                    return getattr(sock, operation)(*args)
                # TLS says which way it waits, whichever way the operation goes: a read
                # may need to write, and a write to read.
                except ssl.SSLWantWriteError:
                    wants_write = True
                except ssl.SSLWantReadError:
                    wants_write = False
                except BlockingIOError:
                    wants_write = write
            self._wait(sock, wake, wants_write, deadline)

    def _wait(
        self, sock: socket.socket, wake: socket.socket, write: bool, deadline: float | None
    ) -> None:
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise TimeoutError("timed out")
        if hasattr(select, "poll"):
            # Not select.select, which cannot take a descriptor above FD_SETSIZE.
            poller = select.poll()
            poller.register(sock, select.POLLOUT if write else select.POLLIN)
            poller.register(wake, select.POLLIN)
            ms = None if remaining is None else math.ceil(remaining * 1000)
            ready = {fd for fd, _ in poller.poll(ms)}
            woke, other = wake.fileno() in ready, sock.fileno() in ready
        else:
            readable, writable, _ = select.select(
                [wake] if write else [wake, sock], [sock] if write else [], [], remaining
            )
            woke, other = wake in readable, bool(writable) or sock in readable
        if woke or self._woken:
            raise ConnectionAbortedError("the fetch was cancelled")
        if not other:
            raise TimeoutError("timed out")


class _Validator:
    """An identity ETag as the service sent it. Held here so no repr, and so no traceback
    that shows locals, reveals it: it is server text, which could reflect the token."""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Validator) and other.value == self.value

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return "<ETag withheld>"


class _Reply:
    """One response: its status, the parsed headers the client uses, and the open body.

    Header values are server text, which could in principle reflect the token, so only
    what the client parsed from them is kept here, never the raw text.
    """

    def __init__(
        self,
        conn: http.client.HTTPConnection,
        sock: _WakeableSocket | None,
        response: http.client.HTTPResponse,
        secret: _Secret,
    ) -> None:
        self.conn = conn
        self.sock = sock
        self.response = response
        self.status = response.status
        self.body: bytes | None = None
        self.has_etag = response.getheader("ETag") is not None
        # Screened for the whole token only: a hex token would often share a run of six
        # characters with a genuine hex digest, and the ETag is kept only in a
        # `_Validator`, which never shows it.
        etag = response.getheader("ETag")
        if etag is not None and secret.value in etag:
            etag = None
        # The identity ETag, and the ETag with any "-gzip" suffix set aside, both held
        # only as a `_Validator`.
        self.etag = _identity_etag(etag)
        self.etag_digest = _etag_digest(etag)
        del etag
        self.length = _count(_header(response, "Content-Length", secret))
        self.retry_after = _seconds(_header(response, "Retry-After", secret))
        self.ndjson_identity = _is_ndjson_identity(
            _header(response, "Content-Encoding", secret),
            _header(response, "Content-Type", secret),
        )
        # For a 206: where the slice starts, and the length of the whole data. Both are
        # None unless the Content-Range is a well-formed slice running to the end, whose
        # own length matches Content-Length where that is given.
        self.continues_from, self.complete_length = _slice_to_end(
            _header(response, "Content-Range", secret), self.length
        )
        if self.status == 200:
            self.continues_from, self.complete_length = 0, self.length

    def close(self) -> None:
        # Wake first: a worker thread may still be reading this response, holding its
        # reader's lock, which closing the response would otherwise wait on.
        if self.sock is not None:
            self.sock.wake()
        self.response.close()
        self.conn.close()


_REFLECTED_RUN = 6


def _header(response: http.client.HTTPResponse, name: str, secret: _Secret) -> str | None:
    """The header's value, or None if it is absent or shares a run of six or more
    characters with the token: a value that may reflect the token is treated as if the
    service had not sent it, so no part of it reaches anything the client keeps, even as
    a number."""
    value = response.getheader(name)
    return None if value is None else _screened(value, secret)


def _screened(text: str, secret: _Secret) -> str | None:
    """`text`, or None if it shares a run of six or more characters with the token (the
    whole token, if it is shorter)."""
    token = secret.value
    run = min(_REFLECTED_RUN, len(token))
    if any(text[i : i + run] in token for i in range(len(text) - run + 1)):
        return None
    return text


def _identity_etag(value: str | None) -> _Validator | None:
    """`value` if it is an identity ETag, `"<hex sha256>"`, else None: a weak, gzip or
    malformed validator cannot be checked against, nor resumed from."""
    match = _ETAG.fullmatch(value or "")
    if match is None or match.group(2):
        return None
    return _Validator(match.group(0))


def _etag_digest(value: str | None) -> _Validator | None:
    """The identity form of an identity or gzip ETag, `"<hex sha256>"`, else None."""
    match = _ETAG.fullmatch(value or "")
    return None if match is None else _Validator(f'"{match.group(1)}"')


def _count(value: str | None) -> int | None:
    return int(value) if value is not None and value.isascii() and value.isdigit() else None


def _seconds(value: str | None) -> float | None:
    value = (value or "").strip()
    return float(value) if value.isascii() and value.isdigit() else None


def _is_ndjson_identity(encoding: str | None, content_type: str | None) -> bool:
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    return (encoding or "identity").strip().lower() == "identity" and media_type == _NDJSON


def _slice_to_end(value: str | None, length: int | None) -> tuple[int | None, int | None]:
    match = _CONTENT_RANGE.fullmatch(value or "")
    if match is None:
        return None, None
    first, last, complete = (int(group) for group in match.groups())
    if not first <= last == complete - 1 or length not in (None, last - first + 1):
        return None, None
    return first, complete


class _Progress:
    """Splits the body into lines and tracks the bytes received."""

    def __init__(self) -> None:
        self.received = 0
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[Any]:
        position = self.received
        self.received += len(chunk)
        self._buffer += chunk
        *lines, rest = self._buffer.split(b"\n")
        self._buffer = bytearray(rest)
        items = [item for line in lines for item in self._line(bytes(line))]
        # After the lines, so a range response's manifest is known before its entries' bytes.
        self._digest(position, chunk)
        return items

    def finish(self, total: int | None) -> list[Any]:
        if total is not None and self.received != total:
            raise HistoryCorrupt(
                f"received {self.received} bytes, not the length the service stated"
            )
        # A last line without its newline is still delivered, so nothing is dropped.
        items = [*self._line(bytes(self._buffer))] if self._buffer else []
        self._buffer = bytearray()
        self._verify()
        return items

    def _line(self, line: bytes) -> list[Any]:
        return [line]

    def _digest(self, position: int, chunk: bytes) -> None:
        raise NotImplementedError

    def _verify(self) -> None:
        raise NotImplementedError


class _StreamProgress(_Progress):
    """A single stream, whose identity `ETag` is the SHA-256 digest of all of it."""

    def __init__(self, expected: str | None) -> None:
        super().__init__()
        self._expected = expected
        self._hash = hashlib.sha256()

    def _digest(self, position: int, chunk: bytes) -> None:
        self._hash.update(chunk)

    def _verify(self) -> None:
        if self._expected is not None and self._hash.hexdigest() != self._expected:
            raise HistoryCorrupt("the data does not match the digest the service stated")


class _FramedProgress(_Progress):
    """A range response: a manifest line, then each ready entry's bytes back to back."""

    def __init__(self, retry_after: float | None) -> None:
        super().__init__()
        self._retry_after = retry_after
        self._manifest: Manifest | None = None
        self._start = 0  # where the entries' bytes begin: just after the manifest line
        self._slices: list[tuple[int, int, str, Any]] = []
        self._next = 0

    def _line(self, line: bytes) -> list[Any]:
        if self._manifest is not None:
            return [line]
        self._manifest = _parse_manifest(line, self._retry_after)
        self._start = len(line) + 1
        # Ready entries follow the manifest back to back, in its order, with no gaps.
        position = self._start
        for entry in self._manifest.entries:
            if entry.status != "ready":
                continue
            if self._start + cast(int, entry.byte_offset) != position:
                raise HistoryCorrupt("the manifest's entries are not laid out back to back")
            end = position + cast(int, entry.length)
            self._slices.append((position, end, cast(str, entry.sha256), hashlib.sha256()))
            position = end
        return [self._manifest]

    def _digest(self, position: int, chunk: bytes) -> None:
        end = position + len(chunk)
        index = self._next
        while index < len(self._slices):
            start, stop, _, digest = self._slices[index]
            if start >= end:
                break
            low, high = max(start, position), min(stop, end)
            if high > low:
                digest.update(chunk[low - position : high - position])
            if stop > end:
                break
            index += 1
        self._next = index

    def finish(self, total: int | None) -> list[Any]:
        items = super().finish(total)
        if self._manifest is None:
            raise HistoryError(
                "the range response has no manifest", code=_errors.HISTORY_BAD_RESPONSE
            )
        expected = max((stop for _, stop, _, _ in self._slices), default=self._start)
        if self.received != expected:
            raise HistoryCorrupt(
                f"received {self.received} bytes, not the length the manifest describes"
            )
        return items

    def _verify(self) -> None:
        for _, _, expected, digest in self._slices:
            if digest.hexdigest().lower() != expected:
                raise HistoryCorrupt("an entry does not match the digest its manifest states")


def _parse_manifest(line: bytes, retry_after: float | None) -> Manifest:
    try:
        data = json.loads(line)
        entries = tuple(_manifest_entry(raw) for raw in data["manifest"])
    except (ValueError, TypeError, KeyError, RecursionError) as error:
        failure = HistoryError(
            f"the range response's manifest could not be read ({type(error).__name__})",
            code=_errors.HISTORY_BAD_RESPONSE,
        )
    else:
        return Manifest(entries, retry_after)
    raise failure


def _manifest_entry(raw: dict[str, Any]) -> ManifestEntry:
    entry = ManifestEntry(
        session_date=raw["session_date"],
        instrument=raw["instrument"],
        channel=raw["channel"],
        status=raw["status"],
        byte_offset=raw["byte_offset"],
        length=raw["length"],
        sha256=raw["sha256"],
    )
    if entry.status == "ready":
        if not all(isinstance(v, int) and v >= 0 for v in (entry.byte_offset, entry.length)):
            raise ValueError("a ready entry needs a byte_offset and a length")
        if not isinstance(entry.sha256, str) or not _HEX_DIGEST.fullmatch(entry.sha256):
            raise ValueError("a ready entry needs a sha256")
    return entry


class _Guarded(AsyncGenerator[_T, None]):
    """What the fetch methods return: their download, with every error it raises detached
    from its traceback and chain on the way out, whether raised while fetching, decoding
    or closing. The download's frames hold the response's bytes, which could reflect the
    token, and this wrapper keeps no message of its own."""

    __slots__ = ("_items",)

    def __init__(self, items: AsyncGenerator[_T, None]) -> None:
        self._items = items

    async def asend(self, value: None) -> _T:
        failure: BaseException
        try:
            return await self._items.asend(value)
        except StopAsyncIteration:
            raise
        except BaseException as error:
            failure = _stripped(error)
        # Raised outside the handler, so the original is not chained to it.
        raise failure

    async def athrow(self, *args: Any) -> _T:
        failure: BaseException
        try:
            return await self._items.athrow(*args)
        except StopAsyncIteration:
            raise
        except BaseException as error:
            failure = _stripped(error)
        raise failure

    async def aclose(self) -> None:
        failure: BaseException
        try:
            return await self._items.aclose()
        except BaseException as error:
            failure = _stripped(error)
        raise failure


def _stripped(error: BaseException) -> BaseException:
    error = error.with_traceback(None)
    error.__cause__ = error.__context__ = None
    return error


def _decode_line(line: bytes) -> HistoryItem:
    """One NDJSON line as the live connection would deliver it."""
    # Any failure to decode one line is reported for that line, and the download goes on:
    # besides ParseError and ValueError, the parsers raise RecursionError for JSON nested
    # too deeply and OverflowError for an out-of-range number.
    try:
        decoded = codec.decode(line)
    except Exception as error:
        return DecodeFailed(None, _decode_error(error))
    env = decoded.envelope
    cls = INBOUND.get(env.type)
    if cls is None:
        return Unknown(env.type, decoded.payload, env.seq if env.HasField("seq") else None)
    try:
        return cast(MarketData, codec.unpack(decoded.payload, cls))
    except Exception as error:
        return DecodeFailed(env.type, _decode_error(error))


_ERRORS: dict[int, tuple[type[HistoryError], str]] = {
    202: (HistoryPending, "the data is not ready yet"),
    400: (HistoryRequestRejected, "the request was rejected as malformed"),
    401: (HistoryUnauthenticated, "the token is missing or not recognised"),
    403: (HistoryForbidden, "the token may not read this data"),
    404: (HistoryUnavailable, "the data is unavailable and will never exist"),
    409: (HistoryNotClosed, "the session has not closed yet"),
    501: (HistoryNotImplemented, "the service does not serve this yet"),
    429: (HistoryRateLimited, "too many requests"),
}


def _error_for(reply: _Reply, secret: _Secret) -> HistoryError:
    status: str | None = None
    message: str | None = None
    try:
        body = json.loads(reply.body or b"")
    except (ValueError, RecursionError):
        # Discarded here, with its traceback: the parser's frames hold the raw body.
        body = None
    if isinstance(body, dict):
        # Server text: an echoed token is redacted, and text that still shares a run of
        # characters with it (a fragment, say from an echoed header) is withheld, as sent
        # or as the error's message shows it, in one line.
        if isinstance(body.get("status"), str):
            status = _server_text(body["status"], secret)
        if isinstance(body.get("message"), str):
            message = _server_text(body["message"], secret)
    cls, meaning = _ERRORS.get(reply.status, (HistoryError, "unexpected response"))
    text = f"{meaning} (HTTP {reply.status})" + (f": {message}" if message else "")
    details: dict[str, Any] = {"http_status": reply.status, "status": status, "message": message}
    if cls is HistoryPending or cls is HistoryRateLimited:
        error: HistoryError = cls(text, retry_after=reply.retry_after, **details)
    elif cls is HistoryError:
        error = cls(text, code=_errors.HISTORY_UNEXPECTED_STATUS, **details)
    else:
        error = cls(text, **details)
    # The message shows the service's text flattened: withheld if, so shown, it holds the
    # token in any form.
    error.fields = _errors.withheld(lambda shown: _holds_token(shown, secret), **error.fields)
    return error


def _server_text(text: str, secret: _Secret) -> str | None:
    """The service's `text` with any echo of the token redacted, or None if it still
    shares a run of characters with the token, as sent or flattened to one line (which
    drops the control and format characters that could split a run)."""
    screened = _screened(_redact(text, secret), secret)
    if screened is None or _screened(flatten(screened), secret) is None:
        return None
    return screened


def _sanitised(error: BaseException, secret: _Secret) -> BaseException:
    """`error` without its traceback or chain, or a replacement if it could carry server
    text or the token.

    Only network errors (`OSError` and its subclasses, such as `TimeoutError`) keep their
    type. Anything else raised while sending the request or reading the response can quote
    the request headers (an invalid header value, in escaped form) or the server's status
    line and headers, so it is replaced by a `HistoryError` naming only its type.
    """
    if isinstance(error, Exception) and not isinstance(error, OSError):
        return HistoryError(f"the request failed ({type(error).__name__}); details withheld")
    # Read in its arguments and attributes too, decoded: str() and repr() escape a token
    # with a backslash (and the filename of an OSError is shown only as its repr).
    if isinstance(error, OSError) and _holds_token(error, secret):
        return HistoryError(f"{type(error).__name__}; details withheld")
    error = error.with_traceback(None)
    error.__cause__ = error.__context__ = None
    return error


def _address_invalid(text: str, problem: str) -> HistoryAddressInvalid:
    return HistoryAddressInvalid(text, fields={"problem": problem})


def _parse_url(url: str) -> tuple[bool, str, int, str]:
    try:
        parts = urlsplit(url)
        given_port = parts.port
    except ValueError:
        well_formed = False
    else:
        well_formed = True
    if not well_formed:
        # Raised outside the handler, so the parser's error, which can quote the address,
        # is not chained to it.
        raise _address_invalid(
            "the history service address is not a well-formed URL",
            "is not a well-formed URL, or has a port that is not a number from 0 to 65535",
        )
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise _address_invalid(
            "the history service address must be an https:// URL", "is not an https:// URL"
        )
    if parts.username is not None or parts.password is not None:
        raise _address_invalid(
            "put no credentials in the history service address", "holds credentials"
        )
    if parts.query or parts.fragment:
        raise _address_invalid(
            "the history service address takes no query or fragment",
            "has a query or a fragment",
        )
    if parts.scheme == "http" and not _is_loopback(parts.hostname):
        raise _address_invalid(
            "the history service address must be https:// (http:// is local only)",
            "is a plain http:// address for another computer, which only a test server on "
            "this computer may use",
        )
    https = parts.scheme == "https"
    # Always a port: given none, http.client would split an IPv6 host such as "::1" at
    # its last colon into a host and a port.
    port = given_port if given_port is not None else (443 if https else 80)
    return https, parts.hostname, port, parts.path.rstrip("/")


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _date_text(value: date | str) -> str:
    if isinstance(value, datetime):
        raise TypeError("pass a session date (a datetime.date or 'YYYY-MM-DD'), not a datetime")
    return value.isoformat() if isinstance(value, date) else value


def _list_param(values: Iterable[str], name: str) -> str:
    # A bare string is iterable too, and would otherwise become a list of its letters.
    if isinstance(values, str):
        raise TypeError(f'pass {name} as a list, such as ["AAPL"], not a single string')
    items = list(values)
    if not items or any(not item or "," in item for item in items):
        raise ValueError(f"{name} must be a non-empty list of non-empty ids without commas")
    return ",".join(quote(item, safe="") for item in items)
