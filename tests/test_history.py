"""The history client against a local fake history service on 127.0.0.1."""

import asyncio
import hashlib
import json
import logging
import secrets
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from qte_sdk import errors, history
from qte_sdk.connection import DecodeFailed, Unknown
from qte_sdk.contract.v1.market_data_pb2 import (
    OPTION_GREEKS_UNAVAILABLE,
    OPTION_GREEKS_VALID,
    OPTION_ROLE_ACTIVE,
    OPTION_ROLE_OBLIGATED,
    OPTION_WINDOW_COMPUTED,
    Book,
    OptionChain,
    OptionGreeks,
    SessionState,
    Trades,
)
from qte_sdk.contract.v1.order_events_pb2 import (
    Accepted,
    Execution,
    OrderCancelled,
    OrderState,
    Reject,
    RiskNotice,
    TermCloseFill,
)
from qte_sdk.history import (
    ChannelStatus,
    FeedGap,
    HistoryChanged,
    HistoryClient,
    HistoryCorrupt,
    HistoryError,
    HistoryForbidden,
    HistoryInterrupted,
    HistoryNotClosed,
    HistoryNotImplemented,
    HistoryPending,
    HistoryRateLimited,
    HistoryRequestRejected,
    HistoryUnauthenticated,
    HistoryUnavailable,
    Manifest,
    MissingHistoryURL,
    ReportsManifest,
    ReportsManifestEntry,
    SessionSummary,
    TokenMalformed,
)
from qte_sdk.options import is_option_symbol
from qte_sdk.session import MissingToken

DAY = "2026-01-05"
CHANNELS = ("book", "trades", "mark")
# A range manifest's fixed channel order (contract 09); other tokens follow in byte order.
RANGE_CHANNELS = (*CHANNELS, "option_greeks")
# Option contracts, named by their unpadded OCC symbols, served like the fake's equities.
CALL = "SPY261120C00650000"
PUT = "GOOGL261120P00172500"
# The fake service's instrument universe: anything else is unavailable, whatever the date.
UNIVERSE = frozenset({"TEST", "AAA", "BBB"})
# The longest the fake service, or a test's gate, holds anything before going on alone: a
# watchdog only, far past the client's 60 second timeout in the tests that cancel, since
# every test releases what it holds (see `released`).
HOLD_LIMIT = 300.0


def synthetic_token() -> str:
    return secrets.token_urlsafe(32)


def line(type_: str, seq: int, payload: dict[str, Any], sent_at: str = "1000") -> bytes:
    envelope = {"version": "0.x", "type": type_, "seq": seq, "sent_at": sent_at}
    return json.dumps({**envelope, "payload": payload}, separators=(",", ":")).encode() + b"\n"


def book(seq: int, instrument: str = "TEST", bid: str = "99950000") -> bytes:
    payload = {
        "instrument": instrument,
        "grid_time": str(1000 * seq),
        "bid_levels": [{"price": bid, "size": "300"}],
        "ask_levels": [{"price": "100050000", "size": "200"}],
        "student_bid_levels": [],
        "student_ask_levels": [],
        "condition": "LIVE",
    }
    return line("book", seq, payload, str(1000 * seq))


def trades(seq: int, instrument: str = "TEST") -> bytes:
    prints = [
        {
            "price": "100050000",
            "size": "40",
            "aggressor_side": "BUY",
            "timestamp": str(1000 * seq),
            "kind": "STUDENT_TO_WALL",
        }
    ]
    payload = {"instrument": instrument, "grid_time": str(1000 * seq), "prints": prints}
    return line("trades", seq, payload, str(1000 * seq))


def etag_of(data: bytes) -> str:
    return f'"{hashlib.sha256(data).hexdigest()}"'


@dataclass
class Pending:
    times: int
    retry_after: str | None = "2"


@dataclass
class FakeHistory:
    """A history service that follows the contract, for a fixed set of objects.

    Objects are keyed by (session date, instrument, channel), `session_state` under
    (session date, None, "session_state") and `option_chain` under (session date, None,
    "option_chain"). An option contract is any well-formed OCC symbol, with the equities'
    channels plus `option_greeks`. A key in `pending` answers `pending` that many
    times first. `drop_after` makes the next whole-object response for a path stop after
    that many bytes, as a dropped connection would.
    """

    token: str
    objects: dict[tuple[str, str | None, str], bytes] = field(default_factory=dict)
    pending: dict[tuple[str, str | None, str], Pending] = field(default_factory=dict)
    # Session dates that have not closed yet: answered not_closed before any cache lookup.
    open_dates: set[str] = field(default_factory=set)
    # Answer a resume with the whole object (200), as for a Range the service does not
    # support, with these header overrides on that 200.
    whole_on_resume: bool = False
    # Cut that whole-object answer to a resume short, once, after this many bytes.
    drop_whole_resume_after: int | None = None
    whole_resume_headers: dict[str, str | None] = field(default_factory=dict)
    # Per (date, instrument, channel): the status a range manifest states instead.
    status_override: dict[tuple[str, str | None, str], str] = field(default_factory=dict)
    drop_after: dict[str, int] = field(default_factory=dict)
    etag_override: dict[str, str | None] = field(default_factory=dict)
    replace_after_drop: dict[tuple[str, str | None, str], bytes] = field(default_factory=dict)
    error_message: str = "not served"
    raw_error_body: bytes | None = None  # replaces the whole JSON error body
    raw_manifest: bytes | None = None  # replaces a range response's manifest line
    # Header overrides for a whole response and for a resumed one; None leaves a header
    # out, and "{auth}" echoes the token the request presented.
    first_headers: dict[str, str | None] = field(default_factory=dict)
    resume_headers: dict[str, str | None] = field(default_factory=dict)
    # Per path: send this many bytes, then wait for the event before sending the rest.
    hold_after: dict[str, tuple[int, threading.Event]] = field(default_factory=dict)
    # Set before any response is sent; the server waits for it.
    answer: threading.Event | None = None
    # Set before a response to a resume (a request with a Range header) is sent.
    answer_resume: threading.Event | None = None
    # Added to every ready entry's byte_offset in a range manifest, to break its layout.
    shift_offsets: int = 0
    # The private reports: the team the token resolves to, its term's date range, and its
    # reports object per session date. A date in `reports_pending` answers pending that
    # many times first.
    team: str = "team-a"
    term: tuple[str, str] = ("2026-01-01", "2026-03-31")
    reports: dict[str, bytes] = field(default_factory=dict)
    reports_pending: dict[str, Pending] = field(default_factory=dict)
    # The body of GET /v1/history/sessions, and its Content-Type.
    listing: bytes = b'{"sessions":[]}'
    listing_type: str = "application/json"
    # Send the listing chunked, without a Content-Length, as the service does for one
    # over about 2 KiB; a drop then cuts a chunk short.
    listing_chunked: bool = False
    # Paths answered 501, as an endpoint not built yet is.
    unbuilt: set[str] = field(default_factory=set)
    credential_term: bool = True  # False: the service's own credential term is missing
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def respond(self, handler: BaseHTTPRequestHandler) -> None:
        headers = {name.lower(): value for name, value in handler.headers.items()}
        self.requests.append((handler.path, headers))
        if self.answer is not None:
            self.answer.wait(HOLD_LIMIT)
        if self.answer_resume is not None and "range" in headers:
            self.answer_resume.wait(HOLD_LIMIT)
        if headers.get("authorization") != f"Bearer {self.token}":
            return self.json(handler, 401, "unauthenticated")
        parts = urlsplit(handler.path)
        segments = [unquote(s) for s in parts.path.split("/")[3:]]
        if parts.path in self.unbuilt:
            return self.json(handler, 501, "not_implemented")
        if segments == ["sessions"]:
            return self.sessions(handler)
        if segments[-1] == "reports":
            return self.private_reports(handler, segments, parse_qs(parts.query), headers)
        if segments == ["range"]:
            return self.range(handler, parse_qs(parts.query), headers)
        try:
            date.fromisoformat(segments[0])
        except ValueError:
            return self.json(handler, 400, "malformed_request")
        if len(segments) == 2 and segments[1] in ("session_state", "option_chain"):
            key: tuple[str, str | None, str] = (segments[0], None, segments[1])
        elif len(segments) == 3:
            key = (segments[0], segments[1], segments[2])
        else:
            return self.json(handler, 400, "malformed_request")
        state = self.status(key)
        if state == "pending":
            pending = self.pending[key]
            pending.times -= 1
            return self.json(handler, 202, "pending", pending.retry_after)
        if state == "unavailable":
            return self.json(handler, 404, "unavailable")
        if state == "not_closed":
            return self.json(handler, 409, "not_closed")
        body = self.objects[key]
        etag = self.etag_override.get(handler.path, etag_of(body))
        self.serve(handler, body, etag, headers)
        if handler.path in self.drop_after or key not in self.replace_after_drop:
            return
        self.objects[key] = self.replace_after_drop.pop(key)

    def status(self, key: tuple[str, str | None, str]) -> str:
        if key in self.status_override:
            return self.status_override[key]
        _, instrument, channel = key
        if instrument is None:
            known = channel in ("session_state", "option_chain")
        elif is_option_symbol(instrument):
            known = channel in RANGE_CHANNELS
        else:
            known = instrument in UNIVERSE and channel in CHANNELS
        if not known:
            return "unavailable"  # never not_closed: the session will never hold it
        if key[0] in self.open_dates:
            return "not_closed"
        pending = self.pending.get(key)
        if pending is not None and pending.times > 0:
            return "pending"
        return "ready" if key in self.objects else "unavailable"

    def range(
        self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]], headers: dict
    ) -> None:
        if not {"from", "to", "instruments", "channels"} <= query.keys():
            return self.json(handler, 400, "malformed_request")
        held = {d for d, _, _ in self.objects} | self.open_dates
        dates = sorted(d for d in held if query["from"][0] <= d <= query["to"][0])
        entries, blobs, offset = [], [], 0
        requested = query["channels"][0].split(",")
        others = sorted(set(requested) - set(RANGE_CHANNELS))
        channels = [c for c in RANGE_CHANNELS if c in requested] + others
        for day in dict.fromkeys(dates):
            for instrument in sorted(set(query["instruments"][0].split(","))):
                for channel in channels:
                    key = (day, instrument, channel)
                    status = self.status(key)
                    entry = {"session_date": day, "instrument": instrument, "channel": channel}
                    entry |= {"status": status, "byte_offset": None, "length": None}
                    entry["sha256"] = None
                    if status == "ready":
                        blob = self.objects[key]
                        entry |= {"byte_offset": offset + self.shift_offsets}
                        entry["length"] = len(blob)
                        entry["sha256"] = hashlib.sha256(blob).hexdigest()
                        blobs.append(blob)
                        offset += len(blob)
                    entries.append(entry)
        manifest = json.dumps({"manifest": entries}, separators=(",", ":")).encode() + b"\n"
        if self.raw_manifest is not None:
            manifest = self.raw_manifest
        digest_input = [
            {k: e[k] for k in ("session_date", "instrument", "channel", "status", "sha256")}
            for e in entries
        ]
        etag = etag_of(json.dumps(digest_input, separators=(",", ":")).encode())
        extra = {"Retry-After": "30"} if any(e["status"] == "pending" for e in entries) else {}
        self.serve(handler, manifest + b"".join(blobs), etag, headers, extra)

    def sessions(self, handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", self.listing_type)
        drop = self.drop_after.pop(handler.path, None)
        if self.listing_chunked:
            handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
            # One chunk, cut after `drop` bytes of its data, or whole with the last chunk.
            data = self.listing if drop is None else self.listing[:drop]
            handler.wfile.write(f"{len(self.listing):x}\r\n".encode() + data)
            if drop is None:
                handler.wfile.write(b"\r\n0\r\n\r\n")
            handler.wfile.flush()
            handler.close_connection = drop is not None
            return
        handler.send_header("Content-Length", str(len(self.listing)))
        handler.end_headers()
        handler.wfile.write(self.listing if drop is None else self.listing[:drop])
        if drop is not None:
            handler.close_connection = True

    def report_status(self, day: str) -> str:
        if day in self.open_dates:
            return "not_closed"
        pending = self.reports_pending.get(day)
        if pending is not None and pending.times > 0:
            return "pending"
        return "ready" if day in self.reports else "unavailable"

    def private_reports(
        self,
        handler: BaseHTTPRequestHandler,
        segments: list[str],
        query: dict[str, list[str]],
        headers: dict,
    ) -> None:
        """Both reports endpoints, answering in the contract's order: 400, 403 for a team
        not the token's, 403 for a date outside the token's term, then 409, then 200, 202
        or 404 (per entry, for the range form)."""
        ranged = len(segments) == 3 and segments[0] == "range"
        try:
            if ranged:
                first = date.fromisoformat(query["from"][0])
                last = date.fromisoformat(query["to"][0])
            elif len(segments) == 3:
                first = last = date.fromisoformat(segments[0])
            else:
                return self.json(handler, 400, "malformed_request")
        except (KeyError, ValueError):
            return self.json(handler, 400, "malformed_request")
        if last < first:
            return self.json(handler, 400, "malformed_request")
        if segments[1] != self.team:
            message = "private reports are served only to the token's own team"
            return self.json(handler, 403, "forbidden", message=message)
        if not self.credential_term:
            message = "the credential's term is not set on the service"
            return self.json(handler, 403, "forbidden", message=message)
        days = [
            date.fromordinal(n).isoformat() for n in range(first.toordinal(), last.toordinal() + 1)
        ]
        if not all(self.term[0] <= day <= self.term[1] for day in days):
            message = (
                "private reports are served only for a session of the term this token's "
                "credentials belong to"
            )
            return self.json(handler, 403, "forbidden", message=message)
        if not ranged:
            status = self.report_status(days[0])
            if status == "not_closed":
                return self.json(handler, 409, "not_closed")
            if status == "pending":
                pending = self.reports_pending[days[0]]
                pending.times -= 1
                return self.json(handler, 202, "pending", pending.retry_after)
            if status == "unavailable":
                return self.json(handler, 404, "unavailable")
            body = self.reports[days[0]]
            return self.serve(handler, body, etag_of(body), headers)
        entries, blobs, offset = [], [], 0
        for day in days:
            status = self.report_status(day)
            entry: dict[str, Any] = {"session_date": day, "status": status}
            entry |= {"byte_offset": None, "length": None, "sha256": None}
            if status == "ready":
                blob = self.reports[day]
                entry |= {"byte_offset": offset, "length": len(blob)}
                entry["sha256"] = hashlib.sha256(blob).hexdigest()
                blobs.append(blob)
                offset += len(blob)
            entries.append(entry)
        manifest = json.dumps({"manifest": entries}, separators=(",", ":")).encode() + b"\n"
        if self.raw_manifest is not None:
            manifest = self.raw_manifest
        digest_input = [{k: e[k] for k in ("session_date", "status", "sha256")} for e in entries]
        etag = etag_of(json.dumps(digest_input, separators=(",", ":")).encode())
        extra = {"Retry-After": "30"} if any(e["status"] == "pending" for e in entries) else {}
        self.serve(handler, manifest + b"".join(blobs), etag, headers, extra)

    def serve(
        self,
        handler: BaseHTTPRequestHandler,
        body: bytes,
        etag: str | None,
        headers: dict[str, str],
        extra: dict[str, str] | None = None,
    ) -> None:
        start = 0
        out: dict[str, str | None] = {"Content-Type": "application/x-ndjson", "ETag": etag}
        requested = headers.get("range", "")
        matches = etag and headers.get("if-range") == etag
        if requested.startswith("bytes=") and matches and not self.whole_on_resume:
            start = int(requested[len("bytes=") : -1])
            code = 206
            out["Content-Range"] = f"bytes {start}-{len(body) - 1}/{len(body)}"
            out |= self.resume_headers
        elif requested:
            code = 200  # the whole object again, from byte 0
            out |= self.whole_resume_headers
        else:
            code = 200
            out |= self.first_headers
        out = {"Content-Length": str(len(body) - start), **out, **(extra or {})}
        handler.send_response(code)
        presented = headers.get("authorization", "").removeprefix("Bearer ")
        for name, value in out.items():
            if value is not None:
                handler.send_header(name, value.replace("{auth}", presented))
        handler.end_headers()
        drop = self.drop_after.pop(handler.path, None) if start == 0 else None
        if drop is None and code == 200 and requested:
            drop, self.drop_whole_resume_after = self.drop_whole_resume_after, None
        hold = self.hold_after.pop(handler.path, None) if start == 0 else None
        if hold is not None:
            handler.wfile.write(body[: hold[0]])
            handler.wfile.flush()
            hold[1].wait(HOLD_LIMIT)
            body = body[hold[0] :]
        handler.wfile.write(body[start:] if drop is None else body[:drop])
        handler.wfile.flush()
        if drop is not None or out.get("Content-Length") is None:
            handler.close_connection = True  # the end of an unstated length is the close

    def json(
        self,
        handler: BaseHTTPRequestHandler,
        code: int,
        status: str,
        retry_after: str | None = None,
        message: str | None = None,
    ) -> None:
        # "{auth}" in the message echoes whatever token the request presented, and
        # "{auth_part}" its first twelve characters.
        presented = handler.headers.get("Authorization", "").removeprefix("Bearer ")
        message = (self.error_message if message is None else message).replace("{auth}", presented)
        message = message.replace("{auth_part}", presented[:12])
        body = json.dumps({"status": status, "message": message}).encode()
        if self.raw_error_body is not None:
            body = self.raw_error_body.replace(b"{auth}", presented.encode())
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        if retry_after is not None:
            handler.send_header("Retry-After", retry_after)
        handler.end_headers()
        handler.wfile.write(body)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # HTTPServer looks up this machine's fully qualified name, which can be slow.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class LocalServer6(LocalServer):
    address_family = socket.AF_INET6


def ipv6_loopback_available() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.bind(("::1", 0))
    except OSError:
        return False
    return True


needs_ipv6 = pytest.mark.skipif(
    not ipv6_loopback_available(), reason="needs an IPv6 loopback address"
)


@contextmanager
def serve_history(
    fake: FakeHistory, tls: ssl.SSLContext | None = None, host: str = "127.0.0.1"
) -> Iterator[str]:
    """Serve `fake` over http, or over https with `tls`, a server context, on `host`."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 (the http.server API)
            fake.respond(self)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = (LocalServer6 if ":" in host else LocalServer)((host, 0), Handler)
    if tls is not None:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        scheme = "http" if tls is None else "https"
        name = f"[{host}]" if ":" in host else host
        yield f"{scheme}://{name}:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The client's waits, recorded instead of slept."""
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(history, "_sleep", fake_sleep)
    return recorded


async def collect(items: Any) -> list[Any]:
    return [item async for item in items]


async def test_a_multi_line_stream_arrives_as_the_live_message_types():
    token = synthetic_token()
    body = book(1) + trades(2) + book(3, bid="99960000")
    fake = FakeHistory(token, {(DAY, "TEST", "book"): body})
    with serve_history(fake) as url:
        items = await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))
    assert [type(item) for item in items] == [Book, Trades, Book]
    assert items[0].bid_levels[0].price == 99_950_000
    assert items[2].bid_levels[0].price == 99_960_000
    assert items[1].prints[0].size == 40
    path, headers = fake.requests[0]
    assert path == f"/v1/history/{DAY}/TEST/book"
    assert headers["authorization"] == f"Bearer {token}"
    assert headers["accept-encoding"] == "identity"
    assert "range" not in headers


async def test_a_date_object_and_a_path_prefix_are_accepted():
    token = synthetic_token()
    fake = FakeHistory(token, {(DAY, "TEST", "trades"): trades(1)})
    with serve_history(fake) as url:
        client = HistoryClient(url + "/", token)
        items = await collect(client.fetch(date(2026, 1, 5), "TEST", "trades"))
    assert [type(item) for item in items] == [Trades]


async def test_an_empty_day_yields_nothing():
    token = synthetic_token()
    fake = FakeHistory(token, {(DAY, "TEST", "trades"): b""})
    with serve_history(fake) as url:
        assert await collect(HistoryClient(url, token).fetch(DAY, "TEST", "trades")) == []


async def test_session_state_has_its_own_endpoint():
    token = synthetic_token()
    payload = {"state": "OPEN", "session_date": DAY, "grid_time": "1000"}
    fake = FakeHistory(token, {(DAY, None, "session_state"): line("session_state", 1, payload)})
    with serve_history(fake) as url:
        items = await collect(HistoryClient(url, token).fetch_session_state(DAY))
    assert [type(item) for item in items] == [SessionState]
    assert fake.requests[0][0] == f"/v1/history/{DAY}/session_state"


def greeks(
    seq: int,
    instrument: str = CALL,
    status: str = "OPTION_GREEKS_VALID",
    calculated_at: int | None = None,
) -> bytes:
    """Greeks at grid time `1000 * seq`, calculated then unless `calculated_at` says when."""
    payload = {
        "instrument": instrument,
        "grid_time": str(1000 * seq),
        "status": status,
        "calculated_at": str(1000 * seq if calculated_at is None else calculated_at),
        "forward": "665200000",
        "implied_vol": "18000000",
        "delta": "500000000000",
        "gamma": "20000000000",
        "vega": "741900",
        "theta": "-310000",
    }
    return line("option_greeks", seq, payload, str(1000 * seq))


def chain(session_date: str = DAY) -> bytes:
    expiry = {
        "underlying": "SPY",
        "expiry": "2026-11-20",
        "window_status": "OPTION_WINDOW_COMPUTED",
        "reducing_only": False,
        "contracts": [{"instrument": CALL, "role": "OPTION_ROLE_OBLIGATED"}],
    }
    payload = {"session_date": session_date, "expiries": [expiry]}
    return line("option_chain", 1, payload)


async def test_an_option_contract_has_its_greeks_and_the_equity_channels():
    token = synthetic_token()
    objects = {
        (DAY, CALL, "option_greeks"): greeks(1) + greeks(301, status="OPTION_GREEKS_UNAVAILABLE"),
        (DAY, CALL, "book"): book(1, CALL),
    }
    fake = FakeHistory(token, objects)
    with serve_history(fake) as url:
        client = HistoryClient(url, token)
        items = await collect(client.fetch(DAY, CALL, "option_greeks"))
        books = await collect(client.fetch(date(2026, 1, 5), CALL, "book"))
    assert [type(item) for item in items] == [OptionGreeks, OptionGreeks]
    assert (items[0].instrument, items[0].grid_time, items[0].delta) == (
        CALL,
        1000,
        500_000_000_000,
    )
    assert items[0].status == OPTION_GREEKS_VALID
    assert (items[1].grid_time, items[1].status) == (301_000, OPTION_GREEKS_UNAVAILABLE)
    assert [(type(b), b.instrument) for b in books] == [(Book, CALL)]
    assert [path for path, _ in fake.requests] == [
        f"/v1/history/{DAY}/{CALL}/option_greeks",
        f"/v1/history/{DAY}/{CALL}/book",
    ]


@pytest.mark.parametrize(
    ("instrument", "channel"),
    [
        ("TEST", "option_greeks"),  # an equity has no Greeks
        ("SPY261320C00650000", "option_greeks"),  # month 13: not an OCC symbol
        ("SPY   261120C00650000", "book"),  # the padded form is not used on this wire
        (PUT, "option_greeks"),  # well formed, but the session holds nothing for it
        (CALL, "option_chain"),  # the chain is not a per-instrument channel
    ],
    ids=["equity-greeks", "bad-occ", "padded-occ", "unlisted", "chain-as-channel"],
)
async def test_an_option_object_the_session_does_not_hold_is_unavailable(
    instrument, channel, waits
):
    token = synthetic_token()
    fake = FakeHistory(token, {(DAY, CALL, "option_greeks"): greeks(1)})
    with serve_history(fake) as url:
        with pytest.raises(HistoryUnavailable) as caught:
            await collect(HistoryClient(url, token).fetch(DAY, instrument, channel))
    assert caught.value.http_status == 404
    assert len(fake.requests) == 1
    assert waits == []


async def test_the_option_chain_has_its_own_endpoint():
    token = synthetic_token()
    fake = FakeHistory(token, {(DAY, None, "option_chain"): chain()})
    with serve_history(fake) as url:
        client = HistoryClient(url, token)
        items = await collect(client.fetch_option_chain(DAY))
        again = await collect(client.fetch_option_chain(date(2026, 1, 5)))
    assert [type(item) for item in items] == [OptionChain]
    assert items[0].session_date == DAY
    assert [c.instrument for c in items[0].expiries[0].contracts] == [CALL]
    assert [type(item) for item in again] == [OptionChain]
    assert [path for path, _ in fake.requests] == [f"/v1/history/{DAY}/option_chain"] * 2


# Lines the platform's history rebuild writes, copied from qte-platform #2317 at dcd707d8
# (backtester/historyrebuild/testdata/option-history/); see tests/data/option-history.
PLATFORM_FIXTURES = Path(__file__).parent / "data" / "option-history"
FIXTURE_DAY = "2026-10-27"
FIXTURE_CALL = "SPY261120C00665000"


async def test_the_platform_fixture_chain_decodes_through_fetch_option_chain():
    token = synthetic_token()
    body = (PLATFORM_FIXTURES / f"{FIXTURE_DAY}-option_chain.ndjson").read_bytes()
    fake = FakeHistory(token, {(FIXTURE_DAY, None, "option_chain"): body})
    with serve_history(fake) as url:
        items = await collect(HistoryClient(url, token).fetch_option_chain(FIXTURE_DAY))
    assert [type(item) for item in items] == [OptionChain]
    assert items[0].session_date == FIXTURE_DAY
    [expiry] = items[0].expiries
    assert (expiry.underlying, expiry.expiry, expiry.reducing_only) == ("SPY", "2026-11-20", False)
    assert expiry.window_status == OPTION_WINDOW_COMPUTED
    roles = {c.instrument: c.role for c in expiry.contracts}
    assert list(roles) == [
        "SPY261120C00660000",
        "SPY261120C00665000",
        "SPY261120C00670000",
        "SPY261120P00660000",
        "SPY261120P00665000",
        "SPY261120P00670000",
    ]
    assert all(is_option_symbol(instrument) for instrument in roles)
    assert roles[FIXTURE_CALL] == OPTION_ROLE_OBLIGATED
    assert roles["SPY261120C00660000"] == OPTION_ROLE_ACTIVE
    assert fake.requests[0][0] == f"/v1/history/{FIXTURE_DAY}/option_chain"


async def test_the_platform_fixture_greeks_decode_through_the_option_greeks_channel():
    token = synthetic_token()
    name = f"{FIXTURE_DAY}-{FIXTURE_CALL}-option_greeks.ndjson"
    body = (PLATFORM_FIXTURES / name).read_bytes()
    fake = FakeHistory(token, {(FIXTURE_DAY, FIXTURE_CALL, "option_greeks"): body})
    with serve_history(fake) as url:
        client = HistoryClient(url, token)
        items = await collect(client.fetch(FIXTURE_DAY, FIXTURE_CALL, "option_greeks"))
    assert [type(item) for item in items] == [OptionGreeks] * 3
    first = items[0]
    assert first.instrument == FIXTURE_CALL
    assert first.status == OPTION_GREEKS_VALID
    assert (first.grid_time, first.calculated_at) == (1_793_107_800_000, 1_793_107_800_000)
    assert (first.forward, first.implied_vol) == (665_000_000, 20_478_077)
    assert (first.delta, first.gamma, first.vega, first.theta) == (
        509_031_907_257,
        11_388_393_054,
        678_130,
        -287_673,
    )
    # One calculation every five minutes from the open.
    assert [item.grid_time - first.grid_time for item in items] == [0, 300_000, 600_000]
    path = f"/v1/history/{FIXTURE_DAY}/{FIXTURE_CALL}/option_greeks"
    assert fake.requests[0][0] == path


async def test_the_option_chain_follows_the_session_rules(waits):
    token = synthetic_token()
    later = "2026-01-06"
    fake = FakeHistory(
        token,
        {(DAY, None, "option_chain"): chain(), (later, None, "option_chain"): chain(later)},
        pending={(later, None, "option_chain"): Pending(1, "3")},
        open_dates={"2026-01-07"},
    )
    with serve_history(fake) as url:
        client = HistoryClient(url, token)
        # Pending is waited on as for any stream, then served.
        assert [type(i) for i in await collect(client.fetch_option_chain(later))] == [OptionChain]
        with pytest.raises(HistoryNotClosed):
            await collect(client.fetch_option_chain("2026-01-07"))
        with pytest.raises(HistoryUnavailable):  # a session with no chain
            await collect(client.fetch_option_chain("2026-01-08"))
        with pytest.raises(HistoryRequestRejected) as caught:
            await collect(client.fetch_option_chain("2026-13-01"))
        assert caught.value.status == "malformed_request"
    assert waits == [3.0]


async def test_a_datetime_is_refused_for_the_option_chain():
    client = HistoryClient("http://127.0.0.1:1", synthetic_token())
    with pytest.raises(TypeError, match="not a datetime"):
        await collect(client.fetch_option_chain(datetime(2026, 1, 5, 12)))


async def test_unknown_types_and_bad_lines_are_reported_in_place_never_dropped():
    token = synthetic_token()
    body = (
        book(1)
        + line("brand_new", 2, {"x": 1})
        + b"not json\n"
        + line("book", 4, {"grid_time": "soon"})
        + book(5).rstrip(b"\n")  # a last line without its newline still arrives
    )
    fake = FakeHistory(token, {(DAY, "TEST", "book"): body})
    with serve_history(fake) as url:
        items = await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))
    assert [type(item) for item in items] == [Book, Unknown, DecodeFailed, DecodeFailed, Book]
    assert items[1] == Unknown("brand_new", {"x": 1}, 2)
    assert items[2].type is None
    assert items[3].type == "book"


async def test_an_unknown_instrument_is_unavailable_and_not_retried(waits):
    token = synthetic_token()
    fake = FakeHistory(token, {(DAY, "TEST", "book"): book(1)})
    with serve_history(fake) as url:
        with pytest.raises(HistoryUnavailable) as caught:
            await collect(HistoryClient(url, token).fetch(DAY, "NOPE", "book"))
    assert caught.value.http_status == 404
    assert caught.value.status == "unavailable"
    assert caught.value.message == "not served"
    assert len(fake.requests) == 1
    assert waits == []


@pytest.mark.parametrize("endpoint", ["fetch", "fetch_session_state"])
async def test_a_session_not_closed_raises_at_once_and_is_never_retried(endpoint, waits):
    token = synthetic_token()
    key: tuple[str, str | None, str] = (DAY, "TEST", "book")
    if endpoint == "fetch_session_state":
        key = (DAY, None, "session_state")
    # Even with a cache for the session and a pending count set, not_closed comes first.
    fake = FakeHistory(token, {key: book(1)}, pending={key: Pending(3, "1")}, open_dates={DAY})
    with serve_history(fake) as url:
        client = HistoryClient(url, token)
        stream = client.fetch(DAY, "TEST", "book") if key[1] else client.fetch_session_state(DAY)
        with pytest.raises(HistoryNotClosed) as caught:
            await collect(stream)
    assert not isinstance(caught.value, HistoryPending)
    assert caught.value.http_status == 409
    assert caught.value.status == "not_closed"
    assert len(fake.requests) == 1
    assert waits == []


@pytest.mark.parametrize(
    ("instrument", "channel"),
    [("NOPE", "book"), ("TEST", "unknown")],
    ids=["instrument", "channel"],
)
async def test_an_unknown_object_is_unavailable_even_before_the_close(instrument, channel):
    token = synthetic_token()
    fake = FakeHistory(token, {(DAY, "TEST", "book"): book(1)}, open_dates={DAY})
    with serve_history(fake) as url:
        with pytest.raises(HistoryUnavailable):
            await collect(HistoryClient(url, token).fetch(DAY, instrument, channel))


async def test_a_rejected_token_raises_a_typed_error():
    token = synthetic_token()
    fake = FakeHistory(token, {(DAY, "TEST", "book"): book(1)})
    with serve_history(fake) as url:
        with pytest.raises(HistoryUnauthenticated) as caught:
            await collect(HistoryClient(url, synthetic_token()).fetch(DAY, "TEST", "book"))
    assert caught.value.http_status == 401
    assert caught.value.status == "unauthenticated"


async def test_pending_is_retried_as_retry_after_says_then_succeeds(waits):
    token = synthetic_token()
    key = (DAY, "TEST", "book")
    fake = FakeHistory(token, {key: book(1)}, pending={key: Pending(2, "3")})
    with serve_history(fake) as url:
        items = await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))
    assert [type(item) for item in items] == [Book]
    assert waits == [3.0, 3.0]
    assert len(fake.requests) == 3


async def test_pending_past_max_wait_raises_with_the_suggested_wait(waits):
    token = synthetic_token()
    key = (DAY, "TEST", "book")
    fake = FakeHistory(token, {key: book(1)}, pending={key: Pending(5, "30")})
    with serve_history(fake) as url:
        client = HistoryClient(url, token, max_wait=45)
        with pytest.raises(HistoryPending) as caught:
            await collect(client.fetch(DAY, "TEST", "book"))
    assert caught.value.retry_after == 30.0
    assert caught.value.http_status == 202
    assert waits == [30.0]


async def test_pending_without_retry_after_is_not_waited_on(waits):
    token = synthetic_token()
    key = (DAY, "TEST", "book")
    fake = FakeHistory(token, {key: book(1)}, pending={key: Pending(1, None)})
    with serve_history(fake) as url:
        with pytest.raises(HistoryPending) as caught:
            await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))
    assert caught.value.retry_after is None
    assert waits == []


async def test_max_retries_bounds_the_number_of_waits(waits):
    token = synthetic_token()
    key = (DAY, "TEST", "book")
    fake = FakeHistory(token, {key: book(1)}, pending={key: Pending(5, "0")})
    with serve_history(fake) as url:
        with pytest.raises(HistoryPending):
            await collect(HistoryClient(url, token, max_retries=2).fetch(DAY, "TEST", "book"))
    assert waits == [0.0, 0.0]
    assert len(fake.requests) == 3


class RateLimited(FakeHistory):
    def respond(self, handler: BaseHTTPRequestHandler) -> None:
        self.requests.append((handler.path, {}))
        self.json(handler, 429, "rate_limited", "7")


async def test_rate_limiting_is_waited_on_then_raised(waits):
    token = synthetic_token()
    with serve_history(RateLimited(token)) as url:
        with pytest.raises(HistoryRateLimited) as caught:
            await collect(HistoryClient(url, token, max_wait=10).fetch(DAY, "TEST", "book"))
    assert caught.value.retry_after == 7.0
    assert waits == [7.0]


async def test_a_malformed_request_is_rejected():
    token = synthetic_token()
    with serve_history(FakeHistory(token)) as url:
        with pytest.raises(HistoryRequestRejected) as caught:
            await collect(HistoryClient(url, token).fetch("not-a-date", "TEST", "book"))
    assert caught.value.status == "malformed_request"


def many_books(count: int) -> bytes:
    return b"".join(book(seq) for seq in range(1, count + 1))


async def test_a_dropped_download_resumes_with_a_range_request():
    token = synthetic_token()
    body = many_books(50)
    path = f"/v1/history/{DAY}/TEST/book"
    cut = len(body) // 2 + 7  # mid-line
    fake = FakeHistory(token, {(DAY, "TEST", "book"): body}, drop_after={path: cut})
    with serve_history(fake) as url:
        items = await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))
    assert [item.grid_time for item in items] == [1000 * seq for seq in range(1, 51)]
    assert len(fake.requests) == 2
    resumed = fake.requests[1][1]
    assert resumed["range"] == f"bytes={cut}-"
    assert resumed["if-range"] == etag_of(body)
    assert resumed["authorization"] == f"Bearer {token}"


async def test_a_resume_answered_with_different_data_raises_changed():
    token = synthetic_token()
    key = (DAY, "TEST", "book")
    body = many_books(20)
    path = f"/v1/history/{DAY}/TEST/book"
    fake = FakeHistory(token, {key: body}, drop_after={path: 100})
    fake.replace_after_drop[key] = many_books(21)
    with serve_history(fake) as url:
        with pytest.raises(HistoryChanged):
            await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))
    assert fake.requests[1][1]["if-range"] == etag_of(body)


@pytest.mark.parametrize(
    "etag",
    [None, "W/" + etag_of(book(1)), etag_of(book(1))[:-1] + '-gzip"', '"not-a-digest"'],
    ids=["missing", "weak", "gzip", "malformed"],
)
async def test_a_response_without_an_identity_etag_is_refused_before_any_message(etag):
    token = synthetic_token()
    path = f"/v1/history/{DAY}/TEST/book"
    fake = FakeHistory(token, {(DAY, "TEST", "book"): book(1)}, etag_override={path: etag})
    items: list[Any] = []
    with serve_history(fake) as url:
        with pytest.raises(HistoryError, match="ETag"):
            async for item in HistoryClient(url, token).fetch(DAY, "TEST", "book"):
                items.append(item)
    assert items == []


async def test_max_resumes_zero_never_resumes():
    token = synthetic_token()
    path = f"/v1/history/{DAY}/TEST/book"
    fake = FakeHistory(token, {(DAY, "TEST", "book"): many_books(10)}, drop_after={path: 100})
    with serve_history(fake) as url:
        with pytest.raises(HistoryInterrupted) as caught:
            await collect(HistoryClient(url, token, max_resumes=0).fetch(DAY, "TEST", "book"))
    assert caught.value.bytes_received == 100
    assert len(fake.requests) == 1


def dropped_once(token: str, **options: Any) -> FakeHistory:
    path = f"/v1/history/{DAY}/TEST/book"
    return FakeHistory(
        token, {(DAY, "TEST", "book"): many_books(10)}, drop_after={path: 100}, **options
    )


@pytest.mark.parametrize(
    ("resume_headers", "error"),
    [
        ({"Content-Range": "bytes 99-{last}/{total}"}, HistoryError),
        ({"Content-Range": "bytes 100-50/{total}"}, HistoryError),
        ({"Content-Range": "bytes 100-{last}/*"}, HistoryError),
        ({"Content-Range": None}, HistoryError),
        ({"Content-Encoding": "gzip"}, HistoryError),
        (
            {"Content-Range": "bytes 100-{total}/{bigger_total}", "Content-Length": "{longer}"},
            HistoryChanged,
        ),
        ({"ETag": etag_of(b"other data")}, HistoryChanged),
    ],
    ids=["wrong-start", "backwards", "unknown-length", "no-range", "gzip", "longer", "new-etag"],
)
async def test_a_resume_that_is_not_the_rest_of_the_same_data_is_refused(resume_headers, error):
    token = synthetic_token()
    total = len(many_books(10))
    sizes = {"last": total - 1, "total": total, "bigger_total": total + 1, "longer": total - 99}
    headers = {
        name: None if value is None else value.format(**sizes)
        for name, value in resume_headers.items()
    }
    fake = dropped_once(token, resume_headers=headers)
    with serve_history(fake) as url:
        with pytest.raises(error) as caught:
            await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))
    assert type(caught.value) is error
    assert len(fake.requests) == 2


async def test_data_that_does_not_match_its_digest_is_reported_corrupt():
    token = synthetic_token()
    path = f"/v1/history/{DAY}/TEST/book"
    wrong = etag_of(b"something else")
    fake = FakeHistory(token, {(DAY, "TEST", "book"): book(1)}, etag_override={path: wrong})
    items: list[Any] = []
    with serve_history(fake) as url:
        with pytest.raises(HistoryCorrupt):
            async for item in HistoryClient(url, token).fetch(DAY, "TEST", "book"):
                items.append(item)
    assert [type(item) for item in items] == [Book]


async def test_a_range_yields_the_manifest_then_each_ready_entry_in_order():
    token = synthetic_token()
    later = "2026-01-06"
    objects = {
        (DAY, "AAA", "book"): book(1, "AAA") + book(2, "AAA"),
        (DAY, "AAA", "trades"): trades(1, "AAA"),
        (DAY, "BBB", "book"): book(1, "BBB"),
        (later, "AAA", "book"): book(1, "AAA", bid="1"),
        (later, "BBB", "trades"): trades(1, "BBB"),
    }
    pending = {(later, "BBB", "trades"): Pending(1)}
    fake = FakeHistory(token, objects, pending=pending)
    with serve_history(fake) as url:
        client = HistoryClient(url, token)
        items = await collect(client.fetch_range(DAY, later, ["AAA", "BBB"], ["book", "trades"]))
    manifest = items[0]
    assert isinstance(manifest, Manifest)
    assert [(e.session_date, e.instrument, e.channel, e.status) for e in manifest.entries] == [
        (DAY, "AAA", "book", "ready"),
        (DAY, "AAA", "trades", "ready"),
        (DAY, "BBB", "book", "ready"),
        (DAY, "BBB", "trades", "unavailable"),
        (later, "AAA", "book", "ready"),
        (later, "AAA", "trades", "unavailable"),
        (later, "BBB", "book", "unavailable"),
        (later, "BBB", "trades", "pending"),
    ]
    assert [e.instrument for e in manifest.pending] == ["BBB"]
    assert manifest.retry_after == 30.0
    assert [(type(i).__name__, i.instrument) for i in items[1:]] == [
        ("Book", "AAA"),
        ("Book", "AAA"),
        ("Trades", "AAA"),
        ("Book", "BBB"),
        ("Book", "AAA"),
    ]
    path = fake.requests[0][0]
    assert path == (
        f"/v1/history/range?from={DAY}&to={later}&instruments=AAA,BBB&channels=book,trades"
    )


async def test_a_range_marks_a_session_not_closed_and_includes_only_ready_entries():
    token = synthetic_token()
    today = "2026-01-06"
    fake = FakeHistory(token, {(DAY, "AAA", "book"): book(1, "AAA")}, open_dates={today})
    with serve_history(fake) as url:
        client = HistoryClient(url, token)
        items = await collect(client.fetch_range(DAY, today, ["AAA", "NOPE"], ["book"]))
    manifest = items[0]
    assert isinstance(manifest, Manifest)
    assert [(e.session_date, e.instrument, e.status) for e in manifest.entries] == [
        (DAY, "AAA", "ready"),
        (DAY, "NOPE", "unavailable"),
        (today, "AAA", "not_closed"),
        (today, "NOPE", "unavailable"),
    ]
    assert [e.session_date for e in manifest.not_closed] == [today]
    assert manifest.pending == ()
    assert manifest.retry_after is None  # a not_closed entry alone does not set it
    closed = manifest.not_closed[0]
    assert (closed.byte_offset, closed.length, closed.sha256) == (None, None, None)
    assert [type(item) for item in items[1:]] == [Book]


async def test_a_range_mixes_option_contracts_and_equities_in_the_contract_order():
    token = synthetic_token()
    bad = "SPY261320C00650000"
    objects = {
        (DAY, "AAA", "book"): book(1, "AAA"),
        (DAY, CALL, "book"): book(1, CALL),
        (DAY, CALL, "option_greeks"): greeks(1) + greeks(2),
    }
    fake = FakeHistory(token, objects)
    instruments = [CALL, "AAA", bad, CALL]
    channels = ["option_greeks", "option_chain", "book"]
    with serve_history(fake) as url:
        client = HistoryClient(url, token)
        items = await collect(client.fetch_range(DAY, DAY, instruments, channels))
    manifest = items[0]
    assert isinstance(manifest, Manifest)
    assert [(e.instrument, e.channel, e.status) for e in manifest.entries] == [
        ("AAA", "book", "ready"),
        ("AAA", "option_greeks", "unavailable"),  # an equity has no Greeks
        ("AAA", "option_chain", "unavailable"),  # never a range channel
        (CALL, "book", "ready"),
        (CALL, "option_greeks", "ready"),
        (CALL, "option_chain", "unavailable"),
        (bad, "book", "unavailable"),  # the service checks the id, not the SDK
        (bad, "option_greeks", "unavailable"),
        (bad, "option_chain", "unavailable"),
    ]
    assert [(type(i).__name__, i.instrument) for i in items[1:]] == [
        ("Book", "AAA"),
        ("Book", CALL),
        ("OptionGreeks", CALL),
        ("OptionGreeks", CALL),
    ]
    # Sent as given, in the caller's order: the service sorts and checks.
    assert fake.requests[0][0] == (
        f"/v1/history/range?from={DAY}&to={DAY}&instruments={CALL},AAA,{bad},{CALL}"
        "&channels=option_greeks,option_chain,book"
    )


async def test_a_dropped_range_download_resumes_inside_an_entry():
    token = synthetic_token()
    objects = {(DAY, "AAA", "book"): many_books(30), (DAY, "AAA", "trades"): trades(1, "AAA")}
    fake = FakeHistory(token, objects)
    path = f"/v1/history/range?from={DAY}&to={DAY}&instruments=AAA&channels=book,trades"
    fake.drop_after[path] = 1500
    with serve_history(fake) as url:
        client = HistoryClient(url, token)
        items = await collect(client.fetch_range(DAY, DAY, ["AAA"], ["book", "trades"]))
    assert isinstance(items[0], Manifest)
    assert [type(item) for item in items[1:]] == [Book] * 30 + [Trades]
    assert fake.requests[1][1]["range"] == "bytes=1500-"


async def test_a_range_list_must_be_a_list_of_ids():
    client = HistoryClient("http://127.0.0.1:1", synthetic_token())
    with pytest.raises(TypeError):
        await collect(client.fetch_range(DAY, DAY, "AAPL", ["book"]))
    with pytest.raises(ValueError):
        await collect(client.fetch_range(DAY, DAY, ["AAPL", ""], ["book"]))
    with pytest.raises(ValueError):
        await collect(client.fetch_range(DAY, DAY, ["AAPL,MSFT"], ["book"]))


def test_the_address_comes_from_the_environment_and_has_no_default(monkeypatch):
    monkeypatch.setenv("QTE_TOKEN", synthetic_token())
    monkeypatch.delenv("QTE_HISTORY_URL", raising=False)
    with pytest.raises(MissingHistoryURL):
        HistoryClient()
    monkeypatch.setenv("QTE_HISTORY_URL", "https://history.example.test")
    assert HistoryClient().url == "https://history.example.test"


def test_the_token_can_come_from_qte_token_file(monkeypatch, tmp_path):
    token = synthetic_token()
    path = tmp_path / "token"
    path.write_text(token + "\n")
    monkeypatch.delenv("QTE_TOKEN", raising=False)
    monkeypatch.setenv("QTE_TOKEN_FILE", str(path))
    assert HistoryClient("https://history.example.test")._secret.value == token


def test_a_token_is_required(monkeypatch):
    monkeypatch.delenv("QTE_TOKEN", raising=False)
    monkeypatch.delenv("QTE_TOKEN_FILE", raising=False)
    with pytest.raises(MissingToken):
        HistoryClient("https://history.example.test")


@pytest.mark.parametrize(
    ("make", "found"),
    [
        (lambda t: t + "\n", "an invisible or control character"),
        (lambda t: t + "\u200b", "an invisible or control character"),
        (lambda t: t[:32] + "\u00a0" + t[32:], "a space"),
        (lambda t: t + "\u00e9", "a character outside ASCII"),
    ],
)
def test_a_malformed_token_says_its_length_and_kinds_but_never_the_token(make, found):
    token = make(secrets.token_hex(32))
    with pytest.raises(TokenMalformed) as raised:
        HistoryClient("https://history.example.test", token)
    message = str(raised.value)
    assert message.startswith("QTE-TOKEN-MALFORMED: the token given to HistoryClient is 65 ")
    assert f"and has {found}, which no token has" in message
    assert "64 characters, 0-9 and a-f" in message
    for text in (message, repr(raised.value.args), repr(raised.value.fields)):
        for start in range(len(token) - 7):
            assert token[start : start + 8] not in text


def test_an_interruption_while_a_malformed_token_is_described_shows_no_frame_holding_it(
    monkeypatch,
):
    class Interrupt(BaseException):
        pass

    def interrupted(_):
        raise Interrupt

    monkeypatch.setattr(errors, "unicodedata", SimpleNamespace(category=interrupted))
    token = secrets.token_hex(32) + "\n"
    with pytest.raises(Interrupt) as caught:
        HistoryClient("https://history.example.test", token)
    rendered = traceback.TracebackException.from_exception(caught.value, capture_locals=True)
    frames = [frame for frame in rendered.stack if frame.filename != __file__]
    assert [frame.name for frame in frames] == ["__init__"]
    text = "\n".join(f"{frame.name} {frame.locals}" for frame in frames)
    for start in range(len(token) - 7):
        assert token[start : start + 8] not in text


@pytest.mark.parametrize(
    "url",
    [
        "http://history.example.test",
        "ws://127.0.0.1:1",
        "https://user:pass@history.example.test",
        "https://history.example.test?key=1",
        "http://[2001:db8::1]/",
        "http://[2001:db8::1]:8080/",
        "http://[::ffff:192.0.2.1]/",
    ],
)
def test_an_unsafe_address_is_refused(url):
    with pytest.raises(ValueError):
        HistoryClient(url, synthetic_token())


@pytest.mark.parametrize(
    ("url", "host", "port"),
    [
        ("http://[::1]/", "::1", 80),
        ("http://[::1]:8080/", "::1", 8080),
        ("https://[2001:db8::1]/", "2001:db8::1", 443),
        ("https://[2001:db8::1]:8443/", "2001:db8::1", 8443),
        ("https://history.example.test/", "history.example.test", 443),
        ("http://127.0.0.1/", "127.0.0.1", 80),
    ],
)
async def test_an_ipv6_address_reaches_its_host_and_port(monkeypatch, url, host, port):
    # The request goes to the address's host and port, with or without an explicit port:
    # http.client would otherwise split "::1" into the host ":" and the port 1.
    reached: list[tuple[str, int]] = []

    def refuse(address: tuple[str, int], *args: Any, **kwargs: Any) -> socket.socket:
        reached.append(address)
        raise ConnectionRefusedError("refused by the test")

    monkeypatch.setattr(history.http.client.socket, "create_connection", refuse)
    client = HistoryClient(url, synthetic_token())
    with pytest.raises(ConnectionRefusedError):
        await collect(client.fetch(DAY, "TEST", "book"))
    assert reached and set(reached) == {(host, port)}


# Token safety, to the standard of qte_sdk.connection.


def pieces(token: str, size: int = 6) -> set[str]:
    return {token[i : i + size] for i in range(len(token) - size + 1)}


def assert_token_absent(token: str, text: str) -> None:
    found = [piece for piece in pieces(token) if piece in text]
    assert not found, text


def shown(error: BaseException) -> str:
    """Everything a traceback of `error` can show, locals included, across its cause and
    context, leaving out this test module's own frames, which hold the token by design."""
    parts = [str(error), repr(error), repr(vars(error))]
    pending = [traceback.TracebackException.from_exception(error, capture_locals=True)]
    seen: set[int] = set()
    while pending:
        link = pending.pop()
        if id(link) in seen:
            continue
        seen.add(id(link))
        parts.extend(link.format_exception_only())
        for summary in link.stack:
            if summary.filename != __file__:
                parts.append(f"{summary.filename}:{summary.lineno} {summary.locals}")
        pending.extend(n for n in (link.__cause__, link.__context__) if n is not None)
    return "\n".join(parts)


def rendered_log(caplog: pytest.LogCaptureFixture) -> str:
    formatter = logging.Formatter("%(name)s %(message)s")
    return "\n".join(formatter.format(record) for record in caplog.records)


async def test_the_token_is_never_logged_at_debug(caplog):
    caplog.set_level(logging.DEBUG)
    token = synthetic_token()
    key = (DAY, "TEST", "book")
    path = f"/v1/history/{DAY}/TEST/book"
    fake = FakeHistory(
        token, {key: many_books(20)}, pending={key: Pending(1, "0")}, drop_after={path: 300}
    )
    with serve_history(fake) as url:
        items = await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))
        with pytest.raises(HistoryUnavailable):
            await collect(HistoryClient(url, token).fetch(DAY, "NOPE", "book"))
    assert len(items) == 20
    assert any(record.name == "qte_sdk.history" for record in caplog.records)
    assert_token_absent(token, rendered_log(caplog))


@pytest.mark.parametrize("status", [401, 404, 202])
async def test_an_error_response_never_carries_the_token(status, waits):
    token = synthetic_token()
    key = (DAY, "TEST", "book")
    fake = FakeHistory(token, {key: book(1)}, error_message="echo {auth}")
    if status == 202:
        fake.pending[key] = Pending(1, None)
    presented = synthetic_token() if status == 401 else token
    with serve_history(fake) as url:
        client = HistoryClient(url, presented)
        instrument = "NOPE" if status == 404 else "TEST"
        with pytest.raises(history.HistoryError) as caught:
            await collect(client.fetch(DAY, instrument, "book"))
    assert caught.value.http_status == status
    assert caught.value.message == "echo <token withheld>"
    assert_token_absent(presented, shown(caught.value))


async def test_an_error_message_echoing_part_of_the_token_is_withheld():
    token = synthetic_token()
    fake = FakeHistory(token, error_message="you sent {auth_part}")
    with serve_history(fake) as url:
        with pytest.raises(HistoryUnavailable) as caught:
            await collect(HistoryClient(url, token).fetch(DAY, "NOPE", "book"))
    assert caught.value.message is None
    assert caught.value.status == "unavailable"
    assert_token_absent(token, shown(caught.value))


@pytest.mark.parametrize("parser_gives_up", [False, True], ids=["as-parsed", "recursion"])
async def test_an_error_body_nested_deeply_never_carries_the_token(parser_gives_up, monkeypatch):
    token = synthetic_token()
    nested = b"[" * 5000 + b"]" * 5000
    body = b'{"status": "unavailable", "message": "{auth}", "extra": ' + nested + b"}"
    if parser_gives_up:
        # Whether this depth exhausts the parser depends on the Python version, so the
        # failure is forced here, raised from a frame that holds the body, as the real
        # parser's frames do.
        def loads(text: Any) -> Any:
            raise RecursionError("maximum recursion depth exceeded")

        monkeypatch.setattr(history.json, "loads", loads)
    fake = FakeHistory(token, raw_error_body=body)
    with serve_history(fake) as url:
        with pytest.raises(HistoryUnavailable) as caught:
            await collect(HistoryClient(url, token).fetch(DAY, "NOPE", "book"))
    # Parsed, the echoed token is redacted; unparsed, the body is ignored.
    expected = {None} if parser_gives_up else {None, "<token withheld>"}
    assert caught.value.message in expected
    assert_token_absent(token, shown(caught.value))


@pytest.mark.parametrize(
    "bad",
    [
        b'{"payload": ' + b"[" * 5000 + b"]" * 5000 + b"}\n",
        b'{"version":"0.x","type":"book","seq":2,"payload":{"condition":1e309}}\n',
    ],
    ids=["nested-too-deeply", "number-out-of-range"],
)
async def test_a_line_the_parsers_cannot_handle_is_a_decode_failure(bad):
    token = synthetic_token()
    body = book(1) + bad + book(2)
    fake = FakeHistory(token, {(DAY, "TEST", "book"): body})
    with serve_history(fake) as url:
        items = await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))
    assert [type(item) for item in items] == [Book, DecodeFailed, Book]
    assert str(items[1].error).endswith("; details withheld")


def escaped(text: str) -> str:
    return "".join(f"\\u{ord(c):04x}" for c in text)


@pytest.mark.parametrize(
    "bad",
    [
        lambda t: b'{"version":"0.x","note":"' + t.encode() + b'"\n',
        lambda t: b'{"version":"0.x","note":"' + escaped(t).encode() + b'"\n',
        lambda t: ('{"note":"' + t + '"}').encode("utf-16-le") + b"\n",
        lambda t: line("book", 2, {"instrument": "TEST", "grid_time": t, "note": t}),
    ],
    ids=["plain", "escaped", "utf-16", "payload"],
)
async def test_a_decode_failure_carries_nothing_of_the_line(bad):
    token = synthetic_token()
    body = book(1) + bad(token) + book(3)
    fake = FakeHistory(token, {(DAY, "TEST", "book"): body})
    with serve_history(fake) as url:
        items = await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))
    assert [type(item) for item in items] == [Book, DecodeFailed, Book]
    error = items[1].error
    # A fresh error: nothing of the parser's, so nothing of the line, in any form.
    assert type(error) is ValueError and str(error).endswith("; details withheld")
    assert error.args == (str(error),) and vars(error) == {}
    assert error.__traceback__ is None
    assert error.__cause__ is None and error.__context__ is None
    assert_token_absent(token, shown(error))


async def test_corrupt_data_reflecting_the_token_never_reaches_a_traceback():
    token = synthetic_token()
    path = f"/v1/history/{DAY}/TEST/book"
    body = book(1, instrument=token) + b'{"note":"' + token.encode() + b'"\n'
    wrong = etag_of(b"something else")
    fake = FakeHistory(token, {(DAY, "TEST", "book"): body}, etag_override={path: wrong})
    with serve_history(fake) as url:
        with pytest.raises(HistoryCorrupt) as caught:
            await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert_token_absent(token, shown(caught.value))


async def test_a_manifest_reflecting_the_token_never_reaches_a_traceback():
    token = synthetic_token()
    manifest = json.dumps({"manifest": token}).encode() + b"\n"
    fake = FakeHistory(token, {(DAY, "AAA", "book"): book(1)}, raw_manifest=manifest)
    with serve_history(fake) as url:
        client = HistoryClient(url, token)
        with pytest.raises(HistoryError, match="manifest could not be read") as caught:
            await collect(client.fetch_range(DAY, DAY, ["AAA"], ["book"]))
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert_token_absent(token, shown(caught.value))


async def test_an_error_raised_while_decoding_never_carries_the_line(monkeypatch):
    token = synthetic_token()
    decode = history.codec.decode

    def failing(line: bytes) -> Any:
        if token.encode() in line:
            raise KeyboardInterrupt
        return decode(line)

    monkeypatch.setattr(history.codec, "decode", failing)
    body = book(1) + book(2, instrument=token)
    fake = FakeHistory(token, {(DAY, "TEST", "book"): body})
    with serve_history(fake) as url:
        with pytest.raises(KeyboardInterrupt) as caught:
            await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert_token_absent(token, shown(caught.value))


async def test_a_length_the_manifest_states_never_reaches_an_error():
    token = str(secrets.randbelow(10**18) + 10**18)  # a token a stated length could echo
    entry = {"session_date": DAY, "instrument": "AAA", "channel": "book", "status": "ready"}
    entry |= {"byte_offset": 0, "length": int(token), "sha256": "0" * 64}
    manifest = json.dumps({"manifest": [entry]}).encode() + b"\n"
    fake = FakeHistory(token, {(DAY, "AAA", "book"): book(1)}, raw_manifest=manifest)
    with serve_history(fake) as url:
        client = HistoryClient(url, token)
        with pytest.raises(HistoryCorrupt) as caught:
            await collect(client.fetch_range(DAY, DAY, ["AAA"], ["book"]))
    assert_token_absent(token, shown(caught.value))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_a_network_failure_keeps_its_type_but_not_the_request():
    token = synthetic_token()
    client = HistoryClient(f"http://127.0.0.1:{free_port()}", token)
    with pytest.raises(ConnectionRefusedError) as caught:
        await collect(client.fetch(DAY, "TEST", "book"))
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert_token_absent(token, shown(caught.value))


async def test_an_interrupted_download_never_carries_the_token():
    token = synthetic_token()
    path = f"/v1/history/{DAY}/TEST/book"
    fake = FakeHistory(token, {(DAY, "TEST", "book"): many_books(10)}, drop_after={path: 100})
    with serve_history(fake) as url:
        with pytest.raises(HistoryInterrupted) as caught:
            await collect(HistoryClient(url, token, max_resumes=0).fetch(DAY, "TEST", "book"))
    assert_token_absent(token, shown(caught.value))


@pytest.mark.parametrize(
    ("first_headers", "resume_headers"),
    [
        ({"ETag": "{auth}"}, {}),
        ({"ETag": '"{auth}"'}, {}),
        ({"Content-Type": "{auth}"}, {}),
        ({"Content-Encoding": "{auth}"}, {}),
        ({"Content-Length": "{auth}"}, {}),
        ({}, {"Content-Range": "bytes 100-{auth}/1"}),
        ({}, {"ETag": "{auth}"}),
        ({}, {"Content-Type": "{auth}"}),
    ],
)
async def test_a_header_reflecting_the_token_never_reaches_an_error(first_headers, resume_headers):
    token = synthetic_token()
    fake = dropped_once(token, first_headers=first_headers, resume_headers=resume_headers)
    with serve_history(fake) as url:
        try:
            # A reflecting header is treated as absent, which may or may not end in an error
            # (a missing Content-Encoding simply means an uncompressed body).
            await collect(HistoryClient(url, token, timeout=2).fetch(DAY, "TEST", "book"))
        except Exception as error:
            assert_token_absent(token, shown(error))


class SilentServer:
    """Accepts connections and never answers, so the client's read times out."""

    def __enter__(self) -> str:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        return f"http://127.0.0.1:{self.sock.getsockname()[1]}"

    def __exit__(self, *exc_info: object) -> None:
        self.sock.close()


async def test_a_timeout_keeps_its_type_but_not_the_request():
    token = synthetic_token()
    with SilentServer() as url:
        client = HistoryClient(url, token, timeout=0.2)
        with pytest.raises(TimeoutError) as caught:
            await collect(client.fetch(DAY, "TEST", "book"))
    assert_token_absent(token, shown(caught.value))


@pytest.mark.parametrize("suffix", ["\n", "​", "\r\nX-Other: 1"])
def test_a_token_a_header_cannot_carry_is_refused_without_quoting_it(suffix):
    token = synthetic_token()
    with pytest.raises(ValueError) as caught:
        HistoryClient("https://history.example.test", token + suffix)
    assert_token_absent(token, shown(caught.value))


def test_an_error_quoting_the_token_escaped_is_withheld():
    token = synthetic_token()
    secret = history._Secret(token)
    header = b"Bearer " + token.encode() + b"\n"
    quoting = ValueError(f"Invalid header value {header!r}")  # escaped, not the plain token
    safe = history._sanitised(quoting, secret)
    assert isinstance(safe, HistoryError)
    assert_token_absent(token, shown(safe))


def test_a_network_error_holding_a_token_with_a_backslash_is_withheld():
    # An OSError shows its filename only as a repr, which doubles a backslash, so the
    # token is looked for in its attributes, as held.
    token = synthetic_token() + "\\" + synthetic_token()
    secret = history._Secret(token)
    safe = history._sanitised(OSError(2, "No such file", token), secret)
    assert isinstance(safe, HistoryError)
    assert_token_absent(token, shown(safe))
    assert repr(token)[1:-1] not in shown(safe)


def test_the_client_repr_holds_no_token():
    token = synthetic_token()
    client = HistoryClient("https://history.example.test", token)
    assert_token_absent(token, repr(client) + repr(vars(client)))


@pytest.fixture
def closes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The status of every response the client closes, in order."""
    closed: list[int] = []
    original = history._Reply.close

    def recording_close(reply: Any) -> None:
        closed.append(reply.status)
        original(reply)

    monkeypatch.setattr(history._Reply, "close", recording_close)
    return closed


async def eventually(condition: Any) -> None:
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(0.01)


@contextmanager
def released(*events: threading.Event) -> Iterator[None]:
    """Set `events` on leaving, however the block ends, so nothing stays held after it."""
    try:
        yield
    finally:
        for event in events:
            event.set()


async def test_messages_arrive_while_the_rest_is_still_downloading():
    token = synthetic_token()
    path = f"/v1/history/{DAY}/TEST/book"
    release = threading.Event()
    fake = FakeHistory(token, {(DAY, "TEST", "book"): many_books(5)})
    fake.hold_after[path] = (len(book(1)), release)
    with serve_history(fake) as url, released(release):
        stream = HistoryClient(url, token).fetch(DAY, "TEST", "book")
        async with asyncio.timeout(5):
            first = await anext(stream)
        assert isinstance(first, Book) and first.grid_time == 1000
        assert not release.is_set()
        release.set()
        rest = await collect(stream)
    assert [item.grid_time for item in rest] == [2000, 3000, 4000, 5000]


async def test_closing_a_fetch_part_way_closes_its_connection(closes):
    token = synthetic_token()
    fake = FakeHistory(token, {(DAY, "TEST", "book"): many_books(5)})
    with serve_history(fake) as url:
        stream = HistoryClient(url, token).fetch(DAY, "TEST", "book")
        first = await anext(stream)
        assert closes == []
        await stream.aclose()
    assert isinstance(first, Book)
    assert closes == [200]


# Cancelling. The client makes each request and each read in a worker thread. These tests
# give it a 60 second timeout and have the fake service hold its answer until the test
# has checked the worker, so a worker that ends within `eventually`'s five seconds was
# woken by the cancel.


WORKER = "history-test-worker"


class RecordingExecutor(ThreadPoolExecutor):
    """An event loop's default executor that keeps every call it is given."""

    def __init__(self) -> None:
        super().__init__(thread_name_prefix=WORKER)
        self.calls: list[Future[Any]] = []

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Future[Any]:
        call = super().submit(fn, *args, **kwargs)
        self.calls.append(call)
        return call


def worker_calls() -> list[Future[Any]]:
    """Every call the running event loop hands to a worker thread from now on."""
    executor = RecordingExecutor()
    asyncio.get_running_loop().set_default_executor(executor)
    return executor.calls


def in_a_socket_read() -> bool:
    """Whether a worker thread is inside a socket read. It then holds the lock of the
    response's buffered reader, so closing the response cannot get in before the read:
    only shutting the connection down wakes it."""
    frames = sys._current_frames()
    for thread in threading.enumerate():
        frame = frames.get(thread.ident or 0) if thread.name.startswith(WORKER) else None
        while frame is not None:
            if frame.f_code is socket.SocketIO.readinto.__code__:
                return True
            frame = frame.f_back
    return False


async def workers_end(calls: list[Future[Any]], token: str) -> list[BaseException]:
    """Wait for every call in `calls` to end, and return what they raised. The client
    discards a cancelled worker's error, but it must not carry the token either, and a
    worker that ran out its timeout was not woken."""
    assert calls
    await eventually(lambda: all(call.done() for call in calls))
    raised = [call.exception() for call in calls if not call.cancelled()]
    errors = [error for error in raised if error is not None]
    for error in errors:
        assert not isinstance(error, TimeoutError)
        assert_token_absent(token, shown(error))
    return errors


@pytest.fixture
def connections(monkeypatch: pytest.MonkeyPatch) -> list[socket.socket]:
    """Every TCP connection the client opens."""
    opened: list[socket.socket] = []
    connect = socket.create_connection

    def recording(*args: Any, **kwargs: Any) -> socket.socket:
        sock = connect(*args, **kwargs)
        opened.append(sock)
        return sock

    monkeypatch.setattr(socket, "create_connection", recording)
    return opened


def all_closed(opened: list[socket.socket]) -> bool:
    return bool(opened) and all(sock.fileno() == -1 for sock in opened)


async def test_cancelling_while_the_service_holds_its_answer_ends_the_worker_at_once(
    closes, connections
):
    token = synthetic_token()
    calls = worker_calls()
    answer = threading.Event()
    fake = FakeHistory(token, {(DAY, "TEST", "book"): many_books(5)}, answer=answer)
    with serve_history(fake) as url, released(answer):
        client = HistoryClient(url, token, timeout=60)
        fetching = asyncio.ensure_future(collect(client.fetch(DAY, "TEST", "book")))
        await eventually(lambda: fake.requests)
        fetching.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await fetching
        await workers_end(calls, token)
        assert all_closed(connections)
        assert closes == []  # no response ever arrived
    assert_token_absent(token, shown(caught.value))


@dataclass
class HeldErrorBody(FakeHistory):
    """Answers an error with the start of its body, then holds the rest until released."""

    sent: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def json(self, handler: BaseHTTPRequestHandler, code: int, status: str, *_: Any) -> None:
        body = json.dumps({"status": status, "message": self.error_message}).encode()
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body[:10])
        handler.wfile.flush()
        self.sent.set()
        self.release.wait(HOLD_LIMIT)
        handler.wfile.write(body[10:])


async def test_cancelling_while_an_error_body_is_held_ends_the_worker_at_once(connections):
    token = synthetic_token()
    calls = worker_calls()
    fake = HeldErrorBody(token)
    with serve_history(fake) as url, released(fake.release):
        client = HistoryClient(url, token, timeout=60)
        fetching = asyncio.ensure_future(collect(client.fetch(DAY, "NOPE", "book")))
        await eventually(fake.sent.is_set)
        fetching.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await fetching
        await workers_end(calls, token)
        assert all_closed(connections)
    assert_token_absent(token, shown(caught.value))


async def test_cancelling_during_a_download_ends_the_worker_and_closes_its_connection(
    closes, connections
):
    token = synthetic_token()
    calls = worker_calls()
    path = f"/v1/history/{DAY}/TEST/book"
    release = threading.Event()
    fake = FakeHistory(token, {(DAY, "TEST", "book"): many_books(5)})
    fake.hold_after[path] = (len(book(1)), release)
    with serve_history(fake) as url, released(release):
        stream = HistoryClient(url, token, timeout=60).fetch(DAY, "TEST", "book")
        await anext(stream)
        reading = asyncio.ensure_future(anext(stream))
        # The read of the rest, which the service is holding.
        await eventually(in_a_socket_read)
        reading.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await reading
        assert closes == [200]
        await workers_end(calls, token)
        assert all_closed(connections)
    assert_token_absent(token, shown(caught.value))


async def test_cancelling_while_a_resume_is_held_ends_the_worker_at_once(connections):
    token = synthetic_token()
    calls = worker_calls()
    path = f"/v1/history/{DAY}/TEST/book"
    resume = threading.Event()
    fake = FakeHistory(token, {(DAY, "TEST", "book"): many_books(10)}, drop_after={path: 100})
    fake.answer_resume = resume
    with serve_history(fake) as url, released(resume):
        client = HistoryClient(url, token, timeout=60)
        fetching = asyncio.ensure_future(collect(client.fetch(DAY, "TEST", "book")))
        await eventually(lambda: len(fake.requests) == 2)
        assert fake.requests[1][1]["range"] == "bytes=100-"
        fetching.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await fetching
        await workers_end(calls, token)
        assert len(connections) == 2 and all_closed(connections)
    assert_token_absent(token, shown(caught.value))


async def test_a_fetch_cancelled_while_connecting_sends_nothing(monkeypatch, connections):
    token = synthetic_token()
    calls = worker_calls()
    connecting, connect = threading.Event(), threading.Event()
    opening = socket.create_connection  # the recording one

    def held(*args: Any, **kwargs: Any) -> socket.socket:
        connecting.set()
        connect.wait(HOLD_LIMIT)
        return opening(*args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", held)
    fake = FakeHistory(token, {(DAY, "TEST", "book"): many_books(5)})
    with serve_history(fake) as url, released(connect):
        client = HistoryClient(url, token, timeout=60)
        fetching = asyncio.ensure_future(collect(client.fetch(DAY, "TEST", "book")))
        await eventually(connecting.is_set)
        fetching.cancel()
        with pytest.raises(asyncio.CancelledError):
            await fetching
        # Connecting cannot be woken: the worker finishes it, then stops before sending.
        connect.set()
        errors = await workers_end(calls, token)
        assert [type(error) for error in errors] == [ConnectionAbortedError]
        assert all_closed(connections)
        assert fake.requests == []


@pytest.fixture
def held_replies(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, threading.Event]:
    """Holds the worker just after it has read a response's status and headers: the first
    event is set when it gets there, and it goes on once the second is set. The test
    releases it, since a fixture's teardown would come after the event loop waits for its
    worker threads."""
    arrived, go_on = threading.Event(), threading.Event()
    build = history._Reply.__init__

    def held(reply: Any, *args: Any) -> None:
        build(reply, *args)
        arrived.set()
        go_on.wait(HOLD_LIMIT)

    monkeypatch.setattr(history._Reply, "__init__", held)
    return arrived, go_on


async def test_a_response_that_arrives_as_the_fetch_is_cancelled_is_closed(
    closes, connections, held_replies
):
    arrived, go_on = held_replies
    token = synthetic_token()
    calls = worker_calls()
    fake = FakeHistory(token, {(DAY, "TEST", "book"): many_books(5)})
    with serve_history(fake) as url, released(go_on):
        client = HistoryClient(url, token, timeout=60)
        fetching = asyncio.ensure_future(collect(client.fetch(DAY, "TEST", "book")))
        await eventually(arrived.is_set)
        fetching.cancel()
        with pytest.raises(asyncio.CancelledError):
            await fetching
        assert closes == []
        go_on.set()
        await workers_end(calls, token)
    assert closes == [200]
    assert all_closed(connections)


async def test_a_range_manifest_whose_entries_leave_a_gap_is_refused():
    token = synthetic_token()
    objects = {(DAY, "AAA", "book"): book(1, "AAA"), (DAY, "AAA", "trades"): trades(1, "AAA")}
    fake = FakeHistory(token, objects, shift_offsets=5)
    with serve_history(fake) as url:
        client = HistoryClient(url, token)
        with pytest.raises(HistoryCorrupt):
            await collect(client.fetch_range(DAY, DAY, ["AAA"], ["book", "trades"]))


async def test_cancelling_then_stopping_the_event_loop_still_closes_the_response(
    closes, held_replies, monkeypatch
):
    arrived, go_on = held_replies
    abandon = history._Handoff.abandon

    def abandoning(handoff: Any) -> None:
        abandon(handoff)
        go_on.set()  # the response reaches the handoff only after the fetch gave up

    monkeypatch.setattr(history._Handoff, "abandon", abandoning)
    token = synthetic_token()
    fake = FakeHistory(token, {(DAY, "TEST", "book"): many_books(5)})

    async def start_and_leave(url: str) -> None:
        client = HistoryClient(url, token)
        asyncio.ensure_future(collect(client.fetch(DAY, "TEST", "book")))
        await eventually(arrived.is_set)
        # Returning now leaves the fetch running: asyncio.run cancels it, then waits for
        # the worker thread, which delivers the response once the loop is shutting down.

    with serve_history(fake) as url:
        await asyncio.to_thread(asyncio.run, start_and_leave(url))
    assert closes == [200]


def make_certificate(directory: Any, names: str) -> tuple[str, str]:
    """A self-signed certificate for `names` (a subjectAltName value) and its key, made
    with the openssl command, so no key is kept in the repository."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("needs the openssl command")
    cert, key = str(directory / "cert.pem"), str(directory / "key.pem")
    command = [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1"]
    command += ["-keyout", key, "-out", cert, "-subj", "/CN=qte-sdk test"]
    command += ["-addext", f"subjectAltName={names}"]
    subprocess.run(command, check=True, capture_output=True)
    return cert, key


@pytest.fixture(scope="module")
def certificate(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    return make_certificate(tmp_path_factory.mktemp("tls"), "IP:127.0.0.1")


def server_context(certificate: tuple[str, str]) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*certificate)
    return context


def trusting(certificate: tuple[str, str]) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certificate[0])


async def test_https_checks_the_certificate_and_the_name_on_it(certificate, tmp_path):
    token = synthetic_token()
    body = many_books(3)
    path = f"/v1/history/{DAY}/TEST/book"
    fake = FakeHistory(token, {(DAY, "TEST", "book"): body}, drop_after={path: 100})
    with serve_history(fake, server_context(certificate)) as url:
        # Trusted, with the name the address uses, and resumed after a drop.
        client = HistoryClient(url, token, ssl_context=trusting(certificate))
        items = await collect(client.fetch(DAY, "TEST", "book"))
        assert [item.grid_time for item in items] == [1000, 2000, 3000]
        assert len(fake.requests) == 2
        # A certificate no authority it trusts has signed.
        with pytest.raises(ssl.SSLCertVerificationError) as caught:
            await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))
        assert_token_absent(token, shown(caught.value))
    # A trusted certificate for another name.
    other = make_certificate(tmp_path, "DNS:history.example.test")
    with serve_history(fake, server_context(other)) as url:
        client = HistoryClient(url, token, ssl_context=trusting(other))
        with pytest.raises(ssl.SSLCertVerificationError, match="IP address mismatch"):
            await collect(client.fetch(DAY, "TEST", "book"))
    assert len(fake.requests) == 2  # neither request was sent


@pytest.mark.parametrize("held", ["answer", "body"])
async def test_cancelling_an_https_fetch_ends_the_worker_at_once(certificate, held):
    token = synthetic_token()
    calls = worker_calls()
    path = f"/v1/history/{DAY}/TEST/book"
    release = threading.Event()
    fake = FakeHistory(token, {(DAY, "TEST", "book"): many_books(5)})
    if held == "answer":
        fake.answer = release
    else:
        fake.hold_after[path] = (len(book(1)), release)
    with serve_history(fake, server_context(certificate)) as url, released(release):
        client = HistoryClient(url, token, timeout=60, ssl_context=trusting(certificate))
        stream = client.fetch(DAY, "TEST", "book")
        if held == "answer":
            fetching = asyncio.ensure_future(anext(stream))
            await eventually(lambda: fake.requests)
        else:
            await anext(stream)
            fetching = asyncio.ensure_future(anext(stream))
            await eventually(in_a_socket_read)
        fetching.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await fetching
        await workers_end(calls, token)
    assert_token_absent(token, shown(caught.value))


@contextmanager
def connected(
    certificate: tuple[str, str], tls: bool
) -> Iterator[tuple[Any, socket.socket, socket.socket]]:
    """A `_WakeableSocket` connected to a server socket, with the handshake made if `tls`,
    and the plain socket under the server's TLS layer, to read what is on the wire."""
    plain_client, plain_server = socket.socketpair()
    client, server = plain_client, plain_server
    if tls:
        server = server_context(certificate).wrap_socket(
            plain_server, server_side=True, do_handshake_on_connect=False
        )
        client = trusting(certificate).wrap_socket(
            plain_client, server_hostname="127.0.0.1", do_handshake_on_connect=False
        )
    with server:
        stream = history._WakeableSocket(client, timeout=60)
        try:
            if tls:
                shaking = threading.Thread(target=server.do_handshake)  # type: ignore[attr-defined]
                shaking.start()
                stream.do_handshake()
                shaking.join()
            yield stream, client, server
        finally:
            stream.close()
    assert client.fileno() == -1 and plain_client.fileno() == -1


def on_the_wire(server: socket.socket) -> bytes:
    """Everything that reaches `server` within half a second, read below any TLS layer."""
    socket.socket.settimeout(server, 0.5)
    wire = b""
    try:
        while chunk := socket.socket.recv(server, 65536):
            wire += chunk
    except TimeoutError:
        pass
    return wire


def a_request(token: str) -> bytes:
    return b"GET / HTTP/1.1\r\nAuthorization: Bearer " + token.encode() + b"\r\n\r\n"


@pytest.mark.parametrize("tls", [False, True])
def test_a_woken_connection_sends_nothing_more(certificate, tls):
    # Once the fetch has given up, the worker must not send the request, token and all,
    # whatever it was about to do.
    token = synthetic_token()
    with connected(certificate, tls) as (stream, _, server):
        stream.wake()
        with pytest.raises(ConnectionAbortedError):
            stream.sendall(a_request(token))
        with pytest.raises(ConnectionAbortedError):
            stream.recv_into(bytearray(10))
        assert on_the_wire(server) == b""
    stream.wake()  # harmless once closed


@pytest.mark.parametrize("tls", [False, True])
def test_a_wake_while_sending_lets_nothing_more_out_and_nothing_in_the_clear(
    certificate, tls, monkeypatch
):
    # The fetch gives up at exactly the moment the worker sends its request, from inside
    # the send. What was being sent may still go out, encrypted over TLS, but nothing
    # after it, and never the token in the clear.
    token = synthetic_token()
    with connected(certificate, tls) as (stream, client, server):
        send = type(client).send
        wakers: list[threading.Thread] = []

        def woken_meanwhile(sock: socket.socket, *args: Any) -> int:
            if sock is client and not wakers:
                wakers.append(threading.Thread(target=stream.wake))
                wakers[0].start()
            return send(sock, *args)

        monkeypatch.setattr(type(client), "send", woken_meanwhile)
        try:
            stream.sendall(a_request(token))
        except ConnectionAbortedError:
            pass
        wakers[0].join(5)
        assert not wakers[0].is_alive()
        with pytest.raises(ConnectionAbortedError):
            stream.sendall(a_request(token))
        monkeypatch.undo()
        wire = on_the_wire(server)
    if tls:
        assert wire and token.encode() not in wire
    else:
        assert wire in (b"", a_request(token))


def test_sending_is_bounded_as_a_whole_even_if_each_send_succeeds(monkeypatch):
    plain_client, plain_server = socket.socketpair()
    with plain_server:
        stream = history._WakeableSocket(plain_client, timeout=0.2)
        send = socket.socket.send

        def slowly(sock: socket.socket, data: Any, *args: Any) -> int:
            if sock is not plain_client:
                return send(sock, data, *args)
            time.sleep(0.05)
            return send(sock, bytes(data[:1]))

        monkeypatch.setattr(socket.socket, "send", slowly)
        with pytest.raises(TimeoutError):
            stream.sendall(b"x" * 100)
        monkeypatch.undo()
        stream.close()


class HeldHandshake:
    """Accepts one connection, reads what the client sends first (for an https:// address,
    the opening of the TLS handshake) and never answers it."""

    def __enter__(self) -> "HeldHandshake":
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.url = f"https://127.0.0.1:{self.sock.getsockname()[1]}"
        self.hello, self.done = threading.Event(), threading.Event()
        threading.Thread(target=self.hold, daemon=True).start()
        return self

    def hold(self) -> None:
        conn, _ = self.sock.accept()
        with conn:
            conn.recv(65536)
            self.hello.set()
            self.done.wait(HOLD_LIMIT)

    def __exit__(self, *exc_info: object) -> None:
        self.done.set()
        self.sock.close()


async def test_cancelling_during_the_tls_handshake_ends_the_worker_at_once():
    token = synthetic_token()
    calls = worker_calls()
    with HeldHandshake() as server:
        client = HistoryClient(server.url, token, timeout=60)
        fetching = asyncio.ensure_future(collect(client.fetch(DAY, "TEST", "book")))
        await eventually(server.hello.is_set)
        fetching.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await fetching
        errors = await workers_end(calls, token)
    assert errors and all(isinstance(error, OSError) for error in errors)
    assert_token_absent(token, shown(caught.value))


class ReflectingStatusLine:
    """Answers one request with a malformed status line that repeats its token."""

    def __enter__(self) -> str:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        threading.Thread(target=self.answer, daemon=True).start()
        return f"http://127.0.0.1:{self.sock.getsockname()[1]}"

    def answer(self) -> None:
        conn, _ = self.sock.accept()
        with conn:
            request = conn.recv(65536).decode()
            token = request.split("Authorization: Bearer ", 1)[1].split("\r\n", 1)[0]
            conn.sendall(f"HTTP/1.1 {token} reflected\r\n\r\n".encode())

    def __exit__(self, *exc_info: object) -> None:
        self.sock.close()


async def test_a_status_line_reflecting_the_token_never_reaches_an_error():
    token = synthetic_token()
    with ReflectingStatusLine() as url:
        with pytest.raises(HistoryError, match="details withheld") as caught:
            await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))
    assert_token_absent(token, shown(caught.value))


async def test_an_etag_carrying_part_of_a_hex_token_never_reaches_an_error():
    token = secrets.token_hex(32)
    etag = f'"{token[:32]}{"0" * 32}"'  # a well-formed identity ETag, but the wrong digest
    fake = FakeHistory(token, {(DAY, "TEST", "book"): book(1)}, first_headers={"ETag": etag})
    with serve_history(fake) as url:
        with pytest.raises(HistoryCorrupt) as caught:
            await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))
    assert_token_absent(token, shown(caught.value))


async def test_a_genuine_etag_sharing_characters_with_a_hex_token_is_accepted():
    body = book(1)
    digest = hashlib.sha256(body).hexdigest()
    token = digest[10:16] + secrets.token_hex(29)  # shares a six-character run with it
    fake = FakeHistory(token, {(DAY, "TEST", "book"): body})
    with serve_history(fake) as url:
        items = await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))
    assert [type(item) for item in items] == [Book]


def numeric_token() -> str:
    """A token that starts with digits, so a header could reflect part of it as a number."""
    return "7391846205" + synthetic_token()


async def test_a_content_length_reflecting_part_of_the_token_never_reaches_an_error():
    token = numeric_token()
    fake = dropped_once(token, first_headers={"Content-Length": token[:10]})
    with serve_history(fake) as url:
        with pytest.raises(HistoryError) as caught:
            await collect(HistoryClient(url, token, timeout=2).fetch(DAY, "TEST", "book"))
    assert_token_absent(token, shown(caught.value))


@pytest.mark.parametrize("token", [numeric_token(), "73918"], ids=["prefix", "short-whole"])
async def test_a_retry_after_reflecting_the_token_is_not_kept(token, waits):
    key = (DAY, "TEST", "book")
    fake = FakeHistory(token, {key: book(1)}, pending={key: Pending(1, token[:10])})
    with serve_history(fake) as url:
        with pytest.raises(HistoryPending) as caught:
            await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))
    assert caught.value.retry_after is None
    assert waits == []
    assert token[:10] not in shown(caught.value)
    assert_token_absent(token, shown(caught.value))


# Rules the service states for resumes, digests and statuses.


def whole_body() -> bytes:
    return many_books(40) + trades(41)


async def uninterrupted(token: str) -> list[Any]:
    fake = FakeHistory(token, {(DAY, "TEST", "book"): whole_body()})
    with serve_history(fake) as url:
        return await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))


@pytest.mark.parametrize("gzip_etag", [False, True], ids=["identity-etag", "gzip-etag"])
async def test_a_whole_object_answer_to_a_resume_with_the_same_etag_continues(gzip_etag):
    token = synthetic_token()
    body = whole_body()
    path = f"/v1/history/{DAY}/TEST/book"
    cut = len(body) // 2 + 11  # mid-line
    assert body[cut - 1 : cut] != b"\n"
    fake = FakeHistory(
        token, {(DAY, "TEST", "book"): body}, drop_after={path: cut}, whole_on_resume=True
    )
    if gzip_etag:
        fake.whole_resume_headers = {"ETag": etag_of(body)[:-1] + '-gzip"'}
    with serve_history(fake) as url:
        items = await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))
    # The 200 restarted at byte 0; nothing is repeated or missing.
    assert items == await uninterrupted(token)
    assert len(items) == 41
    assert fake.requests[1][1]["range"] == f"bytes={cut}-"


@pytest.mark.parametrize(
    "etag",
    [etag_of(b"other"), etag_of(b"other")[:-1] + '-gzip"', None],
    ids=["identity", "gzip", "missing"],
)
async def test_a_whole_object_answer_to_a_resume_with_another_etag_is_a_change(etag):
    token = synthetic_token()
    path = f"/v1/history/{DAY}/TEST/book"
    fake = FakeHistory(
        token,
        {(DAY, "TEST", "book"): whole_body()},
        drop_after={path: 500},
        whole_on_resume=True,
        whole_resume_headers={"ETag": etag},
    )
    with serve_history(fake) as url:
        with pytest.raises(HistoryChanged):
            await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))


async def test_etags_are_compared_exactly_so_uppercase_hex_is_another_etag():
    token = synthetic_token()
    body = whole_body()
    path = f"/v1/history/{DAY}/TEST/book"
    upper = '"' + hashlib.sha256(body).hexdigest().upper() + '"'
    fake = FakeHistory(
        token,
        {(DAY, "TEST", "book"): body},
        drop_after={path: 500},
        whole_on_resume=True,
        whole_resume_headers={"ETag": upper},
    )
    with serve_history(fake) as url:
        with pytest.raises(HistoryChanged):
            await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))


async def test_a_first_response_with_an_uppercase_etag_is_refused():
    token = synthetic_token()
    body = book(1)
    upper = '"' + hashlib.sha256(body).hexdigest().upper() + '"'
    fake = FakeHistory(token, {(DAY, "TEST", "book"): body}, first_headers={"ETag": upper})
    with serve_history(fake) as url:
        with pytest.raises(HistoryError, match="no identity ETag"):
            await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))


class UppercaseManifest(FakeHistory):
    """States each manifest sha256 in uppercase hex, which the service never does."""

    def serve(self, handler, body, etag, headers, extra=None):  # type: ignore[override]
        first, rest = body.split(b"\n", 1)
        manifest = json.loads(first)
        for entry in manifest["manifest"]:
            if entry["sha256"]:
                entry["sha256"] = entry["sha256"].upper()
        first = json.dumps(manifest, separators=(",", ":")).encode()
        super().serve(handler, first + b"\n" + rest, etag, headers, extra)


async def test_a_manifest_digest_is_compared_exactly_with_the_lowercase_computed_one():
    token = synthetic_token()
    fake = UppercaseManifest(token, {(DAY, "AAA", "book"): book(1, "AAA")})
    with serve_history(fake) as url:
        with pytest.raises(HistoryCorrupt):
            await collect(HistoryClient(url, token).fetch_range(DAY, DAY, ["AAA"], ["book"]))


async def test_an_endpoint_not_built_yet_is_not_implemented_not_unavailable(waits):
    token = synthetic_token()
    fake = FakeHistory(token, unbuilt={f"/v1/history/{DAY}/team-a/reports"})
    with serve_history(fake) as url:
        with pytest.raises(HistoryNotImplemented) as caught:
            await collect(HistoryClient(url, token).fetch_reports(DAY, "team-a"))
    assert not isinstance(caught.value, HistoryUnavailable)
    assert (caught.value.http_status, caught.value.status) == (501, "not_implemented")
    assert len(fake.requests) == 1 and waits == []


async def test_an_unknown_manifest_entry_status_is_surfaced_not_fatal():
    token = synthetic_token()
    objects = {(DAY, "AAA", "book"): book(1, "AAA"), (DAY, "AAA", "trades"): trades(1, "AAA")}
    fake = FakeHistory(token, objects, status_override={(DAY, "AAA", "book"): "archived"})
    with serve_history(fake) as url:
        client = HistoryClient(url, token)
        items = await collect(client.fetch_range(DAY, DAY, ["AAA"], ["book", "trades"]))
    manifest = items[0]
    assert [(e.channel, e.status) for e in manifest.entries] == [
        ("book", "archived"),
        ("trades", "ready"),
    ]
    assert [type(item) for item in items[1:]] == [Trades]


@dataclass
class Scripted(FakeHistory):
    """Answers every request with one JSON error response."""

    code: int = 500
    token_name: str = "internal_error"
    retry_after: str | None = None

    def respond(self, handler: BaseHTTPRequestHandler) -> None:
        self.requests.append((handler.path, {}))
        self.json(handler, self.code, self.token_name, self.retry_after)


@pytest.mark.parametrize(
    ("code", "token_name", "error"),
    [
        (404, "some_new_reason", HistoryUnavailable),
        (409, "some_new_reason", HistoryNotClosed),
        (501, "some_new_reason", HistoryNotImplemented),
        (500, "internal_error", HistoryError),
        (418, "some_new_reason", HistoryError),
    ],
)
async def test_the_http_status_decides_the_error_whatever_the_status_token(
    code, token_name, error, waits
):
    token = synthetic_token()
    with serve_history(Scripted(token, code=code, token_name=token_name)) as url:
        with pytest.raises(HistoryError) as caught:
            await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))
    assert type(caught.value) is error
    assert caught.value.status == token_name
    assert waits == []


async def test_a_not_closed_answer_is_not_retried_even_with_a_retry_after(waits):
    token = synthetic_token()
    fake = Scripted(token, code=409, token_name="not_closed", retry_after="1")
    with serve_history(fake) as url:
        with pytest.raises(HistoryNotClosed):
            await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))
    assert len(fake.requests) == 1 and waits == []


async def test_rate_limiting_without_retry_after_raises_at_once(waits):
    token = synthetic_token()
    fake = Scripted(token, code=429, token_name="rate_limited")
    with serve_history(fake) as url:
        with pytest.raises(HistoryRateLimited) as caught:
            await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))
    assert caught.value.retry_after is None
    assert len(fake.requests) == 1 and waits == []


async def test_a_416_answer_to_a_resume_is_an_error_not_a_change():
    token = synthetic_token()

    class RefusesRanges(FakeHistory):
        def respond(self, handler: BaseHTTPRequestHandler) -> None:
            if "Range" in handler.headers:
                self.requests.append((handler.path, {}))
                return self.json(handler, 416, "range_not_satisfiable")
            super().respond(handler)

    path = f"/v1/history/{DAY}/TEST/book"
    fake = RefusesRanges(token, {(DAY, "TEST", "book"): whole_body()}, drop_after={path: 500})
    with serve_history(fake) as url:
        with pytest.raises(HistoryError) as caught:
            await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))
    assert type(caught.value) is HistoryError
    assert caught.value.http_status == 416


@pytest.mark.parametrize("states_length", [True, False], ids=["length", "no-length"])
async def test_a_drop_while_discarding_the_repeated_prefix_resumes_again(states_length):
    token = synthetic_token()
    body = whole_body()
    path = f"/v1/history/{DAY}/TEST/book"
    cut = len(body) // 2 + 11
    fake = FakeHistory(
        token,
        {(DAY, "TEST", "book"): body},
        drop_after={path: cut},
        whole_on_resume=True,
        drop_whole_resume_after=cut // 3,  # inside the bytes already delivered
    )
    if not states_length:
        fake.whole_resume_headers = {"Content-Length": None}
    with serve_history(fake) as url:
        items = await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))
    assert items == await uninterrupted(token)
    assert len(fake.requests) == 3
    assert fake.requests[2][1]["range"] == f"bytes={cut}-"


@needs_ipv6
async def test_http_works_on_the_ipv6_loopback():
    token = synthetic_token()
    fake = FakeHistory(token, {(DAY, "TEST", "book"): many_books(3)})
    with serve_history(fake, host="::1") as url:
        assert url.startswith("http://[::1]:")
        items = await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))
    assert [item.grid_time for item in items] == [1000, 2000, 3000]
    assert fake.requests


@needs_ipv6
async def test_https_works_on_an_ipv6_address(tmp_path):
    token = synthetic_token()
    certificate = make_certificate(tmp_path, "IP:::1")
    fake = FakeHistory(token, {(DAY, "TEST", "book"): many_books(3)})
    with serve_history(fake, server_context(certificate), host="::1") as url:
        assert url.startswith("https://[::1]:")
        client = HistoryClient(url, token, ssl_context=trusting(certificate))
        items = await collect(client.fetch(DAY, "TEST", "book"))
    assert [item.grid_time for item in items] == [1000, 2000, 3000]


# GET /v1/history/sessions, and the team's own reports (#359)


def session(day: str, term_id: str, status: str = "ready", **extra: Any) -> dict[str, Any]:
    channels = [{"channel": c, "status": status} for c in (*CHANNELS, "session_state")]
    return {"session_date": day, "term_id": term_id, "scored": True, "channels": channels, **extra}


def listing(*sessions: dict[str, Any]) -> bytes:
    return json.dumps({"sessions": list(sessions)}).encode()


GAP_ENDED = {
    "link": "stock",
    "start_seq": 10,
    "start_ts": 1000,
    "start_cause": "exchange_stop",
    "exchange_restart": "clean_stop",
    "end_seq": 20,
    "end_ts": 2000,
    "end_cause": "feed_resumed",
}
GAP_ONGOING = {
    "link": "options",
    "instrument": CALL,
    "start_seq": 30,
    "start_ts": 3000,
    "start_cause": "a_cause_added_later",
    "ongoing": True,
}


async def test_sessions_are_listed_oldest_first_as_typed_summaries():
    token = synthetic_token()
    body = listing(
        session("2026-10-23", "2026-10-07", feed_gaps=[GAP_ENDED, GAP_ONGOING]),
        session("2026-10-26", "2026-10-26", status="pending"),
    )
    fake = FakeHistory(token, listing=body)
    with serve_history(fake) as url:
        sessions = await HistoryClient(url, token).list_sessions()
    assert fake.requests[0][0] == "/v1/history/sessions"
    assert fake.requests[0][1]["authorization"] == f"Bearer {token}"
    first, second = sessions
    assert isinstance(first, SessionSummary)
    assert (first.session_date, first.term_id, first.scored, first.ready) == (
        "2026-10-23",
        "2026-10-07",
        True,
        True,
    )
    assert first.channels[0] == ChannelStatus("book", "ready")
    assert [c.channel for c in first.channels] == ["book", "trades", "mark", "session_state"]
    assert first.feed_gaps == (
        FeedGap(
            link="stock",
            start_seq=10,
            start_ts=1000,
            start_cause="exchange_stop",
            exchange_restart="clean_stop",
            end_seq=20,
            end_ts=2000,
            end_cause="feed_resumed",
        ),
        FeedGap(
            link="options",
            start_seq=30,
            start_ts=3000,
            start_cause="a_cause_added_later",
            instrument=CALL,
            ongoing=True,
        ),
    )
    # A session without feed_gaps (absent on the wire) has none; pending is not ready.
    assert second.feed_gaps == () and not second.ready
    assert second.channels[0].status == "pending"
    with pytest.raises(AttributeError):
        first.term_id = "x"  # type: ignore[misc]


async def test_an_empty_listing_is_an_empty_tuple():
    token = synthetic_token()
    with serve_history(FakeHistory(token)) as url:
        assert await HistoryClient(url, token).list_sessions() == ()


async def test_fields_the_sdk_does_not_know_are_ignored_and_new_words_kept():
    token = synthetic_token()
    raw = session("2026-10-26", "2026-10-26", unscored_reason="later field", future={"a": 1})
    raw["channels"][0] |= {"status": "rebuilding", "progress": 0.5}
    raw["feed_gaps"] = [{**GAP_ENDED, "severity": "low"}]
    fake = FakeHistory(token, listing=json.dumps({"sessions": [raw], "next": None}).encode())
    with serve_history(fake) as url:
        (only,) = await HistoryClient(url, token).list_sessions()
    assert only.channels[0] == ChannelStatus("book", "rebuilding")
    assert only.feed_gaps[0].end_cause == "feed_resumed"


async def test_sessions_can_be_filtered_by_term_id():
    token = synthetic_token()
    body = listing(
        session("2026-10-06", "2026-09-28"),
        session("2026-10-07", "2026-10-07"),
        session("2026-10-08", "2026-10-07", status="pending"),
    )
    with serve_history(FakeHistory(token, listing=body)) as url:
        sessions = await HistoryClient(url, token).list_sessions()
    this_term = [s.session_date for s in sessions if s.term_id == "2026-10-07"]
    assert this_term == ["2026-10-07", "2026-10-08"]


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"listing": []}',
        b'{"sessions": [{"session_date": "2026-10-07"}]}',
        b'{"sessions": [{"session_date": "2026-10-07", "term_id": "x", "scored": "yes",'
        b' "channels": []}]}',
        b'{"sessions": [7]}',
    ],
)
async def test_a_listing_without_the_contract_fields_is_a_bad_response(body):
    token = synthetic_token()
    with serve_history(FakeHistory(token, listing=body)) as url:
        with pytest.raises(HistoryError) as caught:
            await HistoryClient(url, token).list_sessions()
    assert caught.value.code == errors.HISTORY_BAD_RESPONSE


async def test_a_listing_that_is_not_json_is_refused():
    token = synthetic_token()
    # Valid JSON, so only the Content-Type can refuse it.
    fake = FakeHistory(token, listing=listing(session(DAY, "t")), listing_type="text/html")
    with serve_history(fake) as url:
        with pytest.raises(HistoryError) as caught:
            await HistoryClient(url, token).list_sessions()
    assert caught.value.code == errors.HISTORY_BAD_RESPONSE


async def test_the_listing_needs_a_recognised_token_and_waits_on_a_rate_limit(waits):
    token = synthetic_token()
    with serve_history(FakeHistory(token)) as url:
        with pytest.raises(HistoryUnauthenticated):
            await HistoryClient(url, synthetic_token()).list_sessions()
    with serve_history(RateLimited(token)) as url:
        with pytest.raises(HistoryRateLimited) as caught:
            await HistoryClient(url, token, max_wait=10).list_sessions()
    # Raised without the frames of the download, or a chain.
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    frames = [frame.name for frame in traceback.extract_tb(caught.value.__traceback__)]
    assert "_list_sessions" not in frames and "_first_reply" not in frames
    assert waits == [7.0]


def accepted(seq: int, ref: str = "r-1") -> bytes:
    payload = {"request_ref": ref, "request_type": "NEW", "receipt_time": "1", "release_time": "2"}
    return line("accepted", seq, payload, "2")


def rejected(seq: int) -> bytes:
    payload = {
        "request_ref": "r-2",
        "request_type": "CANCEL",
        "reason_code": "NO_ORDER_AT_LEVEL",
        "reason_detail": "no order at that level",
        "receipt_time": "3",
    }
    return line("reject", seq, payload, "3")


async def test_your_reports_for_one_session_arrive_as_the_live_report_types():
    token = synthetic_token()
    body = accepted(1) + rejected(2) + line("a_report_added_later", 3, {})
    fake = FakeHistory(token, reports={DAY: body})
    with serve_history(fake) as url:
        items = await collect(HistoryClient(url, token).fetch_reports(date(2026, 1, 5), "team-a"))
    assert fake.requests[0][0] == f"/v1/history/{DAY}/team-a/reports"
    assert isinstance(items[0], Accepted) and items[0].request_ref == "r-1"
    assert isinstance(items[1], Reject) and items[1].request_ref == "r-2"
    assert isinstance(items[2], Unknown) and items[2].type == "a_report_added_later"


async def test_a_session_with_no_orders_yields_no_reports():
    token = synthetic_token()
    with serve_history(FakeHistory(token, reports={DAY: b""})) as url:
        assert await collect(HistoryClient(url, token).fetch_reports(DAY, "team-a")) == []


async def test_pending_reports_are_waited_on_then_served(waits):
    token = synthetic_token()
    fake = FakeHistory(token, reports={DAY: accepted(1)}, reports_pending={DAY: Pending(1)})
    with serve_history(fake) as url:
        items = await collect(HistoryClient(url, token).fetch_reports(DAY, "team-a"))
    assert len(items) == 1 and waits == [2.0]


async def test_reports_past_max_wait_raise_pending(waits):
    token = synthetic_token()
    fake = FakeHistory(token, reports_pending={DAY: Pending(5, "30")})
    with serve_history(fake) as url:
        with pytest.raises(HistoryPending) as caught:
            await collect(HistoryClient(url, token, max_wait=10).fetch_reports(DAY, "team-a"))
    assert caught.value.retry_after == 30.0 and waits == []


async def test_reports_of_a_day_with_no_session_are_unavailable_and_not_closed_raises(waits):
    token = synthetic_token()
    fake = FakeHistory(token, open_dates={"2026-01-06"})
    with serve_history(fake) as url:
        client = HistoryClient(url, token)
        with pytest.raises(HistoryUnavailable):
            await collect(client.fetch_reports(DAY, "team-a"))
        with pytest.raises(HistoryNotClosed):
            await collect(client.fetch_reports("2026-01-06", "team-a"))
    assert waits == []


async def test_another_teams_reports_are_forbidden_with_a_next_step(waits):
    token = synthetic_token()
    # Refused 403 even for a session that is pending, or that never was: never 202 or 404.
    fake = FakeHistory(token, reports_pending={DAY: Pending(5)})
    with serve_history(fake) as url:
        client = HistoryClient(url, token)
        for day in (DAY, "2026-01-10"):
            with pytest.raises(HistoryForbidden) as caught:
                await collect(client.fetch_reports(day, "team-b"))
            assert (caught.value.http_status, caught.value.status) == (403, "forbidden")
    text = str(caught.value)
    assert text.startswith("QTE-HISTORY-FORBIDDEN: ")
    assert "own team" in text and "session.info.team" in text
    assert waits == []


async def test_a_session_of_another_term_is_forbidden_with_a_next_step(waits):
    token = synthetic_token()
    fake = FakeHistory(token, reports={"2025-12-15": accepted(1)})
    with serve_history(fake) as url:
        with pytest.raises(HistoryForbidden) as caught:
            await collect(HistoryClient(url, token).fetch_reports("2025-12-15", "team-a"))
    text = str(caught.value)
    assert "term this token's credentials belong to" in text
    assert "current term" in text and "earlier term" in text
    assert waits == []


async def test_reports_need_a_recognised_token():
    token = synthetic_token()
    with serve_history(FakeHistory(token, reports={DAY: accepted(1)})) as url:
        with pytest.raises(HistoryUnauthenticated):
            await collect(HistoryClient(url, synthetic_token()).fetch_reports(DAY, "team-a"))


@pytest.mark.parametrize(
    ("team", "quoted"), [("Team A 1", "Team%20A%201"), ("Team A/1", "Team%20A%2F1")]
)
async def test_the_team_segment_is_sent_exactly_as_given(team, quoted):
    token = synthetic_token()
    fake = FakeHistory(token, team=team, reports={DAY: accepted(1)})
    with serve_history(fake) as url:
        client = HistoryClient(url, token)
        assert len(await collect(client.fetch_reports(DAY, team))) == 1
        assert len(await collect(client.fetch_reports_range(DAY, DAY, team))) == 2
        # No case folding or trimming: a near miss is another team's name.
        for near in (team.lower(), " " + team):
            with pytest.raises(HistoryForbidden):
                await collect(client.fetch_reports(DAY, near))
    assert fake.requests[0][0] == f"/v1/history/{DAY}/{quoted}/reports"
    assert urlsplit(fake.requests[1][0]).path == f"/v1/history/range/{quoted}/reports"


@pytest.mark.parametrize(("team", "error"), [("", ValueError), (None, TypeError), (7, TypeError)])
async def test_a_missing_team_is_refused_before_any_request(team, error):
    token = synthetic_token()
    fake = FakeHistory(token)
    with serve_history(fake) as url:
        client = HistoryClient(url, token)
        with pytest.raises(error, match="session.info.team"):
            await collect(client.fetch_reports(DAY, team))
        with pytest.raises(error, match="session.info.team"):
            await collect(client.fetch_reports_range(DAY, DAY, team))
    assert fake.requests == []


async def test_a_reports_range_yields_its_manifest_then_each_ready_session():
    token = synthetic_token()
    fake = FakeHistory(
        token,
        reports={DAY: accepted(1) + rejected(2), "2026-01-07": b"", "2026-01-08": accepted(1)},
        reports_pending={"2026-01-06": Pending(1)},
        open_dates={"2026-01-09"},
    )
    with serve_history(fake) as url:
        client = HistoryClient(url, token)
        items = await collect(client.fetch_reports_range(DAY, date(2026, 1, 10), "team-a"))
    path = urlsplit(fake.requests[0][0])
    assert path.path == "/v1/history/range/team-a/reports"
    assert parse_qs(path.query) == {"from": [DAY], "to": ["2026-01-10"]}
    manifest = items[0]
    assert isinstance(manifest, ReportsManifest)
    assert [(e.session_date, e.status) for e in manifest.entries] == [
        (DAY, "ready"),
        ("2026-01-06", "pending"),
        ("2026-01-07", "ready"),
        ("2026-01-08", "ready"),
        ("2026-01-09", "not_closed"),
        ("2026-01-10", "unavailable"),
    ]
    assert manifest.entries[2] == ReportsManifestEntry(
        "2026-01-07", "ready", len(accepted(1) + rejected(2)), 0, hashlib.sha256().hexdigest()
    )
    assert [e.session_date for e in manifest.pending] == ["2026-01-06"]
    assert [e.session_date for e in manifest.not_closed] == ["2026-01-09"]
    assert manifest.retry_after == 30.0
    assert [type(item) for item in items[1:]] == [Accepted, Reject, Accepted]


async def test_a_reports_range_with_only_a_not_closed_session_sets_no_retry_after():
    token = synthetic_token()
    fake = FakeHistory(token, reports={DAY: accepted(1)}, open_dates={"2026-01-06"})
    with serve_history(fake) as url:
        items = await collect(
            HistoryClient(url, token).fetch_reports_range(DAY, "2026-01-06", "team-a")
        )
    assert items[0].retry_after is None and len(items) == 2


async def test_a_reports_range_reaching_outside_the_term_is_refused_whole():
    token = synthetic_token()
    fake = FakeHistory(token, reports={DAY: accepted(1)})
    with serve_history(fake) as url:
        client = HistoryClient(url, token)
        with pytest.raises(HistoryForbidden) as caught:
            await collect(client.fetch_reports_range("2025-12-30", DAY, "team-a"))
        assert "earlier term" in str(caught.value)
        with pytest.raises(HistoryForbidden):
            await collect(client.fetch_reports_range(DAY, DAY, "team-b"))
        with pytest.raises(HistoryUnauthenticated):
            await collect(
                HistoryClient(url, synthetic_token()).fetch_reports_range(DAY, DAY, "team-a")
            )
        with pytest.raises(HistoryRequestRejected):
            await collect(client.fetch_reports_range("2026-01-06", DAY, "team-a"))


async def test_a_reports_range_entry_that_does_not_match_its_digest_is_corrupt():
    token = synthetic_token()

    class Tampered(FakeHistory):
        def serve(self, handler, body, etag, headers, extra=None):
            super().serve(handler, body.replace(b'"r-1"', b'"r-9"'), etag, headers, extra)

    with serve_history(Tampered(token, reports={DAY: accepted(1)})) as url:
        with pytest.raises(HistoryCorrupt):
            await collect(HistoryClient(url, token).fetch_reports_range(DAY, DAY, "team-a"))


async def test_every_report_type_decodes_and_sent_at_need_not_be_in_order():
    token = synthetic_token()
    body = (
        line("execution", 1, {"exec_id": "e-1", "instrument": "AAA", "fill_size": "5"}, "50")
        + line("order_cancelled", 2, {"instrument": "AAA", "cancelled_size": "5"}, "40")
        + line("order_state", 3, {"instrument": "AAA", "remaining_size": "3"}, "60")
        + line("risk_notice", 4, {"timestamp": "7"}, "10")
        + line("term_close_fill", 5, {"instrument": "AAA", "size": "2"}, "70")
        + accepted(6)
        + rejected(7)
    )
    fake = FakeHistory(token, reports={DAY: body})
    with serve_history(fake) as url:
        items = await collect(HistoryClient(url, token).fetch_reports(DAY, "team-a"))
    kinds = [Execution, OrderCancelled, OrderState, RiskNotice, TermCloseFill, Accepted, Reject]
    assert [type(item) for item in items] == kinds
    assert items[0].exec_id == "e-1" and items[4].size == 2


async def test_a_correct_request_refused_for_the_services_own_term_says_to_tell_the_course_team(
    waits,
):
    token = synthetic_token()
    fake = FakeHistory(token, reports={DAY: accepted(1)}, credential_term=False)
    with serve_history(fake) as url:
        with pytest.raises(HistoryForbidden) as caught:
            await collect(HistoryClient(url, token).fetch_reports(DAY, "team-a"))
    text = str(caught.value)
    assert "credential's term is not set" in text
    assert "if both are right and it is still refused" in text and "course team" in text
    assert waits == []


async def test_a_dropped_reports_range_resumes_without_repeating_a_report():
    token = synthetic_token()
    body = b"".join(accepted(n, f"r-{n}") for n in range(1, 41))
    fake = FakeHistory(token, reports={DAY: body, "2026-01-06": accepted(1, "s-1")})
    path = f"/v1/history/range/team-a/reports?from={DAY}&to=2026-01-06"
    fake.drop_after[path] = 1500
    with serve_history(fake) as url:
        client = HistoryClient(url, token)
        items = await collect(client.fetch_reports_range(DAY, "2026-01-06", "team-a"))
    assert isinstance(items[0], ReportsManifest)
    refs = [item.request_ref for item in items[1:]]
    assert refs == [f"r-{n}" for n in range(1, 41)] + ["s-1"]
    assert fake.requests[1][1]["range"] == "bytes=1500-"


async def test_a_dropped_session_listing_is_interrupted_not_partly_parsed():
    token = synthetic_token()
    sessions = [
        {
            "session_date": f"2026-01-{day:02d}",
            "term_id": "2026-spring",
            "scored": True,
            "channels": [{"channel": "book", "status": "ready"}],
        }
        for day in range(5, 30)
    ]
    fake = FakeHistory(token, listing=json.dumps({"sessions": sessions}).encode())
    fake.drop_after["/v1/history/sessions"] = 200
    with serve_history(fake) as url:
        with pytest.raises(HistoryInterrupted) as caught:
            await HistoryClient(url, token).list_sessions()
    assert "session listing was cut short" in str(caught.value)


async def test_a_dropped_chunked_session_listing_is_interrupted_not_partly_parsed():
    # A listing over about 2 KiB comes chunked, so a drop cuts a chunk: IncompleteRead.
    token = synthetic_token()
    body = listing(*(session(f"2026-01-{day:02d}", "2026-spring") for day in range(5, 30)))
    fake = FakeHistory(token, listing=body, listing_chunked=True)
    fake.drop_after["/v1/history/sessions"] = 200
    with serve_history(fake) as url:
        client = HistoryClient(url, token)
        with pytest.raises(HistoryInterrupted) as caught:
            await client.list_sessions()
        assert "session listing was cut short (IncompleteRead)" in str(caught.value)
        assert caught.value.bytes_received == 200
        # Whole, the same chunked listing is read.
        assert len(await client.list_sessions()) == 25


async def test_a_session_listing_longer_than_the_cap_is_refused(monkeypatch):
    assert history._LISTING_LIMIT == 16 * 1024 * 1024
    monkeypatch.setattr(history, "_LISTING_LIMIT", 1000)
    token = synthetic_token()
    body = listing(*(session(f"2026-01-{day:02d}", "2026-spring") for day in range(5, 30)))
    assert len(body) > 1000
    for chunked in (False, True):
        with serve_history(FakeHistory(token, listing=body, listing_chunked=chunked)) as url:
            with pytest.raises(HistoryError) as caught:
                await HistoryClient(url, token).list_sessions()
        assert caught.value.code == errors.HISTORY_BAD_RESPONSE
        assert "longer than the client accepts" in str(caught.value)


async def test_a_session_is_ready_only_when_every_channel_is():
    token = synthetic_token()
    mixed = session(DAY, "t")
    mixed["channels"][1]["status"] = "pending"
    with serve_history(FakeHistory(token, listing=listing(mixed))) as url:
        [summary] = await HistoryClient(url, token).list_sessions()
    assert not summary.ready


async def test_the_session_listing_reply_is_closed(closes):
    token = synthetic_token()
    with serve_history(FakeHistory(token, listing=listing(session(DAY, "t")))) as url:
        assert len(await HistoryClient(url, token).list_sessions()) == 1
    assert closes == [200]


async def test_a_network_error_reading_the_listing_that_shows_the_token_is_withheld(
    monkeypatch,
):
    token = synthetic_token()

    def reset(self: Any, *args: Any) -> bytes:
        raise ConnectionResetError(104, f"reset while sending {token}")

    with serve_history(FakeHistory(token, listing=listing(session(DAY, "t")))) as url:
        monkeypatch.setattr(history.http.client.HTTPResponse, "read1", reset)
        with pytest.raises(HistoryError) as caught:
            await HistoryClient(url, token).list_sessions()
    assert "ConnectionResetError; details withheld" in str(caught.value)
    assert_token_absent(token, shown(caught.value))


@pytest.mark.parametrize("ranged", [False, True])
async def test_breaking_out_of_reports_closes_the_response_at_once(closes, ranged):
    token = synthetic_token()
    body = b"".join(accepted(n, f"r-{n}") for n in range(1, 6))
    with serve_history(FakeHistory(token, reports={DAY: body})) as url:
        client = HistoryClient(url, token)
        items = (
            client.fetch_reports_range(DAY, DAY, "team-a")
            if ranged
            else (client.fetch_reports(DAY, "team-a"))
        )
        async for item in items:
            if isinstance(item, Accepted):
                break
        assert closes == []
        await items.aclose()
        # Closed by the close itself, not later by the event loop's finaliser.
        assert closes == [200]


async def test_a_ready_reports_range_entry_without_a_length_is_a_bad_response():
    token = synthetic_token()
    entry = {"session_date": DAY, "status": "ready", "byte_offset": 0, "length": None}
    entry["sha256"] = hashlib.sha256(accepted(1)).hexdigest()
    manifest = json.dumps({"manifest": [entry]}).encode() + b"\n"
    fake = FakeHistory(token, reports={DAY: accepted(1)}, raw_manifest=manifest)
    with serve_history(fake) as url:
        with pytest.raises(HistoryError) as caught:
            await collect(HistoryClient(url, token).fetch_reports_range(DAY, DAY, "team-a"))
    assert caught.value.code == errors.HISTORY_BAD_RESPONSE
    assert "manifest could not be read (ValueError)" in str(caught.value)
