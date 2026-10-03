"""Keep the latest book of each instrument from live or past market data.

The exchange publishes an instrument's `Book` only when it has changed. `SessionState`
still comes at every grid point of a session, but a grid point with no `Book` for an
instrument means that instrument's book is unchanged, so to know the book at any moment
you keep the last one you received. `LatestBooks` does that:

    books = LatestBooks()
    async for item in market_data(session):
        if books.update(item):
            ...  # a new book for item.instrument: books.get(item.instrument) is item
        book = books.get("AAPL")  # the latest book held for AAPL, or None

What it relies on:

- Each session's first publication carries the book of every instrument that has one.
  An instrument whose first book comes later in the session is published then.
- A `subscribe` during a session is answered at once with the last book published this
  session for each instrument named that has one, carrying the `grid_time` it was
  published at; an instrument with no book yet gets its first book when it is published.
  The book from a subscribe
  can be older than the latest `SessionState`, and the same book can then arrive again
  at the same `grid_time`. So the book with the latest `grid_time` wins, and a book whose
  `grid_time` is the same as or older than the one held is ignored.
- A history `book` stream is sparse in the same way: it starts with the session's first
  book and then holds only the books that changed. The book in force at time t is the
  last one with `grid_time` at or before t, so feed the stream in order and stop at the
  first book later than t:

        books = LatestBooks()
        async with aclosing(client.fetch("2026-01-05", "AAPL", "book")) as items:
            async for item in items:
                if isinstance(item, Book) and item.grid_time > t:
                    break
                if isinstance(item, Unknown | DecodeFailed):
                    # A line that may have been a book is lost, so book_at_t may be wrong.
                    raise RuntimeError(f"could not use a history message: {item}")
                books.update(item)
        book_at_t = books.get("AAPL")

Missed messages: after a `SeqGap` or `Disconnected` (any `DataUncertain`), or a
`DecodeFailed` that may have been a book, a changed book may have been lost, so every
book held is kept but listed in `stale`. Until an instrument leaves `stale`, treat its
book as possibly out of date:

- A book for it with a later `grid_time` than the one held clears it. Messages on one
  connection arrive in the order they were sent, so that book was sent after any book
  that was missed, and is the last one published for the instrument.
- After a `Disconnected`, a book at the same `grid_time` as the one held clears it too.
  `ReconnectingSession` subscribes again on the new connection, and the answer is the
  last book published this session, so a book the same age as the one held shows that
  it has not changed. After a `SeqGap` or `DecodeFailed` on the same connection, a book
  at the same `grid_time` is not taken as proof, and clears nothing.

An instrument with no book held is never listed in `stale`, so also watch for the
events themselves if you need to know that a first book may have been missed.

The helper only keeps what it is given. It does not clear books when a session closes:
a book held keeps the `grid_time` it was published at, so you can tell which session it
came from. Every other message (`Trades`, `Mark`, `SessionState`, `OfficialClose`,
`Reject`, a history `Manifest`, and so on) is ignored.
"""

from collections.abc import Iterator

from qte_sdk.connection import DataUncertain, DecodeFailed, Disconnected
from qte_sdk.contract.v1.market_data_pb2 import Book

__all__ = ["LatestBooks"]


class LatestBooks:
    """The latest `Book` received for each instrument, keyed by instrument id."""

    def __init__(self) -> None:
        self._books: dict[str, Book] = {}
        # Instrument -> whether a book at the grid_time held clears it (after a reconnect).
        self._stale: dict[str, bool] = {}

    def update(self, item: object) -> bool:
        """Apply one market-data item. Returns True if it is a `Book` that replaced the book
        held for its instrument, that is, the first book for that instrument or one with a
        later `grid_time`; False for anything else, including a book whose `grid_time` is
        the same as or older than the one held.
        """
        if isinstance(item, Book):
            return self._on_book(item)
        if isinstance(item, Disconnected):
            self._stale.update(dict.fromkeys(self._books, True))
        elif isinstance(item, DataUncertain) or (
            isinstance(item, DecodeFailed) and item.type in (None, "book")
        ):
            self._stale.update(dict.fromkeys(self._books, False))
        return False

    def get(self, instrument: str) -> Book | None:
        """The latest book held for `instrument`, or None if none has been received."""
        return self._books.get(instrument)

    @property
    def stale(self) -> frozenset[str]:
        """The instruments whose book held may be out of date because messages were missed
        since it was received. Their books are still kept and returned by `get`."""
        return frozenset(self._stale)

    def __contains__(self, instrument: object) -> bool:
        return instrument in self._books

    def __iter__(self) -> Iterator[Book]:
        """The books held, one per instrument."""
        return iter(list(self._books.values()))

    def __len__(self) -> int:
        return len(self._books)

    def _on_book(self, book: Book) -> bool:
        held = self._books.get(book.instrument)
        if held is not None and book.grid_time <= held.grid_time:
            if book.grid_time == held.grid_time and self._stale.get(book.instrument):
                del self._stale[book.instrument]
            return False
        self._stale.pop(book.instrument, None)
        self._books[book.instrument] = book
        return True
