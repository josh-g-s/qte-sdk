"""The history client against a local fake history service on 127.0.0.1."""

import asyncio
import hashlib
import json
import logging
import secrets
import socket
import socketserver
import threading
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from qte_sdk import history
from qte_sdk.connection import DecodeFailed, Unknown
from qte_sdk.contract.v1.market_data_pb2 import Book, SessionState, Trades
from qte_sdk.history import (
    HistoryChanged,
    HistoryClient,
    HistoryCorrupt,
    HistoryError,
    HistoryInterrupted,
    HistoryPending,
    HistoryRateLimited,
    HistoryRequestRejected,
    HistoryUnauthenticated,
    HistoryUnavailable,
    Manifest,
    MissingHistoryURL,
)
from qte_sdk.session import MissingToken

DAY = "2026-01-05"
CHANNELS = ("book", "trades", "mark")


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
    (session date, None, "session_state"). A key in `pending` answers `pending` that many
    times first. `drop_after` makes the next whole-object response for a path stop after
    that many bytes, as a dropped connection would.
    """

    token: str
    objects: dict[tuple[str, str | None, str], bytes] = field(default_factory=dict)
    pending: dict[tuple[str, str | None, str], Pending] = field(default_factory=dict)
    drop_after: dict[str, int] = field(default_factory=dict)
    etag_override: dict[str, str | None] = field(default_factory=dict)
    replace_after_drop: dict[tuple[str, str | None, str], bytes] = field(default_factory=dict)
    error_message: str = "not served"
    # Header overrides for a whole response and for a resumed one; None leaves a header
    # out, and "{auth}" echoes the token the request presented.
    first_headers: dict[str, str | None] = field(default_factory=dict)
    resume_headers: dict[str, str | None] = field(default_factory=dict)
    # Per path: send this many bytes, then wait for the event before sending the rest.
    hold_after: dict[str, tuple[int, threading.Event]] = field(default_factory=dict)
    # Set before any response is sent; the server waits for it.
    answer: threading.Event | None = None
    # Added to every ready entry's byte_offset in a range manifest, to break its layout.
    shift_offsets: int = 0
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def respond(self, handler: BaseHTTPRequestHandler) -> None:
        headers = {name.lower(): value for name, value in handler.headers.items()}
        self.requests.append((handler.path, headers))
        if self.answer is not None:
            self.answer.wait(10)
        if headers.get("authorization") != f"Bearer {self.token}":
            return self.json(handler, 401, "unauthenticated")
        parts = urlsplit(handler.path)
        segments = [unquote(s) for s in parts.path.split("/")[3:]]
        if segments == ["range"]:
            return self.range(handler, parse_qs(parts.query), headers)
        try:
            date.fromisoformat(segments[0])
        except ValueError:
            return self.json(handler, 400, "malformed_request")
        if len(segments) == 2 and segments[1] == "session_state":
            key: tuple[str, str | None, str] = (segments[0], None, "session_state")
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
        body = self.objects[key]
        etag = self.etag_override.get(handler.path, etag_of(body))
        self.serve(handler, body, etag, headers)
        if handler.path in self.drop_after or key not in self.replace_after_drop:
            return
        self.objects[key] = self.replace_after_drop.pop(key)

    def status(self, key: tuple[str, str | None, str]) -> str:
        pending = self.pending.get(key)
        if pending is not None and pending.times > 0:
            return "pending"
        return "ready" if key in self.objects else "unavailable"

    def range(
        self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]], headers: dict
    ) -> None:
        if not {"from", "to", "instruments", "channels"} <= query.keys():
            return self.json(handler, 400, "malformed_request")
        dates = sorted(d for d, _, _ in self.objects if query["from"][0] <= d <= query["to"][0])
        entries, blobs, offset = [], [], 0
        for day in dict.fromkeys(dates):
            for instrument in query["instruments"][0].split(","):
                for channel in CHANNELS:
                    if channel not in query["channels"][0].split(","):
                        continue
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
        digest_input = [
            {k: e[k] for k in ("session_date", "instrument", "channel", "status", "sha256")}
            for e in entries
        ]
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
        if requested.startswith("bytes=") and etag and headers.get("if-range") == etag:
            start = int(requested[len("bytes=") : -1])
            code = 206
            out["Content-Range"] = f"bytes {start}-{len(body) - 1}/{len(body)}"
            out |= self.resume_headers
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
        hold = self.hold_after.pop(handler.path, None) if start == 0 else None
        if hold is not None:
            handler.wfile.write(body[: hold[0]])
            handler.wfile.flush()
            hold[1].wait(10)
            body = body[hold[0] :]
        handler.wfile.write(body[start:] if drop is None else body[:drop])
        handler.wfile.flush()
        if drop is not None:
            handler.close_connection = True

    def json(
        self,
        handler: BaseHTTPRequestHandler,
        code: int,
        status: str,
        retry_after: str | None = None,
    ) -> None:
        # "{auth}" in the message echoes whatever token the request presented.
        presented = handler.headers.get("Authorization", "").removeprefix("Bearer ")
        message = self.error_message.replace("{auth}", presented)
        body = json.dumps({"status": status, "message": message}).encode()
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


@contextmanager
def serve_history(fake: FakeHistory) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 (the http.server API)
            fake.respond(self)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = LocalServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
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


def test_a_token_is_required(monkeypatch):
    monkeypatch.delenv("QTE_TOKEN", raising=False)
    with pytest.raises(MissingToken):
        HistoryClient("https://history.example.test")


@pytest.mark.parametrize(
    "url",
    [
        "http://history.example.test",
        "ws://127.0.0.1:1",
        "https://user:pass@history.example.test",
        "https://history.example.test?key=1",
    ],
)
def test_an_unsafe_address_is_refused(url):
    with pytest.raises(ValueError):
        HistoryClient(url, synthetic_token())


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


async def test_messages_arrive_while_the_rest_is_still_downloading():
    token = synthetic_token()
    path = f"/v1/history/{DAY}/TEST/book"
    release = threading.Event()
    fake = FakeHistory(token, {(DAY, "TEST", "book"): many_books(5)})
    fake.hold_after[path] = (len(book(1)), release)
    with serve_history(fake) as url:
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


async def test_cancelling_during_a_download_closes_its_connection(closes):
    token = synthetic_token()
    path = f"/v1/history/{DAY}/TEST/book"
    release = threading.Event()
    fake = FakeHistory(token, {(DAY, "TEST", "book"): many_books(5)})
    fake.hold_after[path] = (len(book(1)), release)
    with serve_history(fake) as url:
        stream = HistoryClient(url, token).fetch(DAY, "TEST", "book")
        await anext(stream)
        reading = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0.1)
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reading
        assert closes == [200]
        release.set()


async def test_cancelling_before_the_response_closes_it_once_it_arrives(closes):
    token = synthetic_token()
    answer = threading.Event()
    fake = FakeHistory(token, {(DAY, "TEST", "book"): many_books(5)}, answer=answer)
    with serve_history(fake) as url:
        client = HistoryClient(url, token)
        fetching = asyncio.ensure_future(collect(client.fetch(DAY, "TEST", "book")))
        await eventually(lambda: fake.requests)
        fetching.cancel()
        with pytest.raises(asyncio.CancelledError):
            await fetching
        assert closes == []
        answer.set()
        await eventually(lambda: closes == [200])


async def test_a_range_manifest_whose_entries_leave_a_gap_is_refused():
    token = synthetic_token()
    objects = {(DAY, "AAA", "book"): book(1, "AAA"), (DAY, "AAA", "trades"): trades(1, "AAA")}
    fake = FakeHistory(token, objects, shift_offsets=5)
    with serve_history(fake) as url:
        client = HistoryClient(url, token)
        with pytest.raises(HistoryCorrupt):
            await collect(client.fetch_range(DAY, DAY, ["AAA"], ["book", "trades"]))


async def test_cancelling_then_stopping_the_event_loop_still_closes_the_response(closes):
    token = synthetic_token()
    answer = threading.Event()
    fake = FakeHistory(token, {(DAY, "TEST", "book"): many_books(5)}, answer=answer)

    async def start_and_leave(url: str) -> None:
        client = HistoryClient(url, token)
        asyncio.ensure_future(collect(client.fetch(DAY, "TEST", "book")))
        await eventually(lambda: fake.requests)
        # Returning now leaves the fetch running: asyncio.run cancels it, then waits for
        # the worker thread, which the server answers only once the loop is shutting down.
        threading.Timer(0.2, answer.set).start()

    with serve_history(fake) as url:
        await asyncio.to_thread(asyncio.run, start_and_leave(url))
    assert closes == [200]


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


async def test_an_etag_carrying_part_of_a_hex_token_is_treated_as_absent():
    token = secrets.token_hex(32)
    etag = f'"{token[:32]}{"0" * 32}"'  # a well-formed identity ETag, reflecting the token
    fake = FakeHistory(token, {(DAY, "TEST", "book"): book(1)}, first_headers={"ETag": etag})
    with serve_history(fake) as url:
        with pytest.raises(HistoryError, match="no identity ETag") as caught:
            await collect(HistoryClient(url, token).fetch(DAY, "TEST", "book"))
    assert_token_absent(token, shown(caught.value))


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
