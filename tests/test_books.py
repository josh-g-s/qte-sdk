from qte_sdk.books import LatestBooks
from qte_sdk.connection import DecodeFailed, Disconnected, SeqGap, Unknown
from qte_sdk.contract.v1.common_pb2 import OPEN
from qte_sdk.contract.v1.market_data_pb2 import (
    Book,
    InstrumentCondition,
    Mark,
    SessionState,
    Trades,
    WallLevel,
)
from qte_sdk.history import Manifest


def book(instrument: str, grid_time: int, bid: int = 199_990_000, **fields) -> Book:
    return Book(
        instrument=instrument,
        grid_time=grid_time,
        bid_levels=[WallLevel(price=bid, size=10)],
        ask_levels=[WallLevel(price=200_010_000, size=10)],
        condition=fields.pop("condition", InstrumentCondition.LIVE),
        **fields,
    )


def state(grid_time: int) -> SessionState:
    return SessionState(state=OPEN, session_date="2026-01-05", grid_time=grid_time)


def test_the_first_publication_of_a_session_gives_every_instrument_its_book():
    books = LatestBooks()
    first = [state(1000), book("AAPL", 1000), book("MSFT", 1000, bid=99_990_000)]
    assert [books.update(item) for item in first] == [False, True, True]
    assert len(books) == 2
    assert "AAPL" in books and "MSFT" in books and "TSLA" not in books
    assert books.get("MSFT").bid_levels[0].price == 99_990_000
    assert books.get("TSLA") is None
    assert sorted(b.instrument for b in books) == ["AAPL", "MSFT"]
    assert books.stale == frozenset()


def test_a_subscribe_snapshot_older_than_the_latest_session_state_is_kept():
    # Subscribing mid-session: the last book published, at its own grid_time, arrives
    # after session states for later grid points. The session state never displaces it.
    books = LatestBooks()
    books.update(state(5000))
    assert books.update(book("AAPL", 2000)) is True
    books.update(state(5100))
    assert books.get("AAPL").grid_time == 2000


def test_an_identical_repeat_is_ignored():
    # The snapshot, then the same book again as the grid publication at the same grid_time.
    books = LatestBooks()
    snapshot = book("AAPL", 2000)
    assert books.update(snapshot) is True
    assert books.update(book("AAPL", 2000)) is False
    assert books.get("AAPL") is snapshot


def test_an_older_book_after_a_newer_one_is_ignored():
    books = LatestBooks()
    newer = book("AAPL", 3000, bid=199_980_000)
    assert books.update(newer) is True
    assert books.update(book("AAPL", 2000)) is False
    assert books.get("AAPL") is newer


def test_a_later_book_replaces_the_one_held_and_only_for_its_instrument():
    books = LatestBooks()
    books.update(book("AAPL", 1000))
    msft = book("MSFT", 1000)
    books.update(msft)
    changed = book("AAPL", 1300, condition=InstrumentCondition.FROZEN)
    assert books.update(changed) is True
    assert books.get("AAPL") is changed
    assert books.get("MSFT") is msft


def test_messages_other_than_books_change_nothing():
    books = LatestBooks()
    held = book("AAPL", 1000)
    books.update(held)
    others = [
        state(9000),
        Trades(instrument="AAPL", grid_time=9000),
        Mark(instrument="AAPL", value=1),
        Manifest(entries=(), retry_after=None),
        Unknown(type="something_new", payload={}, seq=9),
        None,
    ]
    assert [books.update(item) for item in others] == [False] * len(others)
    assert books.get("AAPL") is held
    assert books.stale == frozenset()


def test_a_sparse_history_stream_gives_the_book_in_force_at_any_time():
    # The session's first book, then only the books that changed.
    stream = [
        book("AAPL", 1000, bid=100),
        book("AAPL", 1500, bid=200),
        book("AAPL", 4000, bid=300),
    ]

    def book_at(t: int) -> Book | None:
        books = LatestBooks()
        for item in stream:
            if item.grid_time > t:
                break
            books.update(item)
        return books.get("AAPL")

    assert book_at(900) is None
    assert book_at(1000).bid_levels[0].price == 100
    assert book_at(1400).bid_levels[0].price == 100  # unchanged, so no line at 1100..1400
    assert book_at(1500).bid_levels[0].price == 200
    assert book_at(3900).bid_levels[0].price == 200
    assert book_at(10_000).bid_levels[0].price == 300


def test_missed_messages_keep_every_book_but_mark_it_stale():
    for uncertain in (SeqGap(expected=3, received=5), Disconnected(error=None)):
        books = LatestBooks()
        books.update(book("AAPL", 1000))
        books.update(book("MSFT", 1000))
        assert books.update(uncertain) is False
        assert books.stale == {"AAPL", "MSFT"}
        assert books.get("AAPL").grid_time == 1000


def test_a_book_at_or_after_the_one_held_clears_its_instrument_from_stale():
    books = LatestBooks()
    books.update(book("AAPL", 1000))
    books.update(book("MSFT", 1000))
    books.update(book("TSLA", 1000))
    books.update(Disconnected(error=ConnectionResetError()))
    # After reconnecting, the subscribe snapshot repeats a book that has not changed.
    assert books.update(book("AAPL", 1000)) is False
    # And carries a newer one for an instrument that changed while disconnected.
    assert books.update(book("MSFT", 1200)) is True
    # An older book confirms nothing.
    assert books.update(book("TSLA", 900)) is False
    assert books.stale == {"TSLA"}


def test_a_book_first_received_after_missed_messages_is_not_stale():
    books = LatestBooks()
    books.update(SeqGap(expected=1, received=2))
    books.update(book("AAPL", 1000))
    assert books.stale == frozenset()


def test_a_decode_failure_marks_books_stale_only_if_it_may_have_been_a_book():
    books = LatestBooks()
    books.update(book("AAPL", 1000))
    books.update(DecodeFailed(type="trades", error=ValueError("bad")))
    assert books.stale == frozenset()
    books.update(DecodeFailed(type="book", error=ValueError("bad")))
    assert books.stale == {"AAPL"}
    books.update(book("AAPL", 1100))
    books.update(DecodeFailed(type=None, error=ValueError("bad")))
    assert books.stale == {"AAPL"}
