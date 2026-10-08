"""Machine-readable logs: the SDK's log records as one JSON object per line.

    import qte_sdk.logs

    qte_sdk.logs.configure()          # JSON lines on stderr when QTE_LOG_FORMAT=json
    qte_sdk.logs.configure("json")    # JSON lines on stderr whatever QTE_LOG_FORMAT says

Plain text is the default, and nothing here runs when `qte_sdk` or this module is imported,
even with `QTE_LOG_FORMAT` set: the variable is read only when `configure()` is called.
The SDK's own commands and examples call it at the start, so `QTE_LOG_FORMAT=json` works
for them; a program of your own calls it once, before it opens a session.

Each line is one JSON object with these keys, always in this order:

    {"time":"2026-10-08T12:34:56.789Z","level":"WARNING","logger":"qte_sdk.update",
     "code":"QTE-UPDATE-AVAILABLE","message":"qte-sdk 1.1.0 is behind 1.1.1.",
     "next_step":"Update with pip install --upgrade \\"git+https://github.com/josh-g-s/qte-sdk\\"",
     "fields":{}}

(one physical line; wrapped here). `time` is UTC to the millisecond; `level` is the
record's level name; `code` is the record's code, such as `QTE-UPDATE-AVAILABLE`, or null;
`message` says what happened and why, without the code in front or the next step at the
end; `next_step` is that step, without its full stop, or null; `fields` holds any values
the record names, such as a path (`{}` for none). A record logged with an exception also
has `exception`, the exception's type name only, since its text could hold anything. The
plain-text form of a coded record is `<code>: <message> <next_step>.`. Every line is
ASCII (any other character is escaped as JSON escapes it), so it reads the same through a
Windows pipe in any code page, and never holds the token.

`configure()` adds one handler, to the `qte_sdk` logger, writing to stderr (or `stream`).
It changes nothing else: not the root logger, not levels, and `propagate` stays on, so a
log file or pytest's caplog set up on the root logger still gets every SDK record. If you
also have a handler on the root logger that writes to the terminal, SDK records then show
twice; in that case put `JsonFormatter` on your own handler instead of calling
`configure()`. Calling it again replaces the handler, and `configure("text")` removes it.

SDK warnings (`QteWarning`, such as `QTE-DOTENV-NOT-IGNORED`) are Python warnings, not log
records. With `capture_warnings=True`, while the format is json, each one shown is logged
instead through the `qte_sdk.warnings` logger, as a JSON line with its code; warning
filters still apply, and other warnings are shown as before. The SDK's commands turn this
on.

A record the SDK logs carries its code, next step and fields on the record, as `code`,
`next_step` and `fields`; `coded()` makes that `extra` for a record of your own.
"""

import contextlib
import json
import logging
import os
import re
import sys
import warnings
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any, TextIO

from qte_sdk import errors as _errors

__all__ = [
    "FORMATS",
    "LOG_FORMAT_ENV_VAR",
    "WARNINGS_LOGGER",
    "JsonFormatter",
    "LogFormatInvalid",
    "coded",
    "configure",
]

# Set to json for JSON lines, or text (the default), before configure() is called.
LOG_FORMAT_ENV_VAR = "QTE_LOG_FORMAT"
FORMATS = ("json", "text")
# The logger that SDK warnings are logged through while they are captured.
WARNINGS_LOGGER = "qte_sdk.warnings"

_SDK_LOGGER = "qte_sdk"
_CODE = re.compile(r"QTE-[A-Z0-9]+(?:-[A-Z0-9]+)+")
# What warnings.showwarning was when this module was imported, and what it was when
# capture began (None while not captured).
_ORIGINAL_SHOWWARNING = warnings.showwarning
_shown_before: Callable[..., Any] | None = None


class LogFormatInvalid(_errors.QteError, ValueError):
    """`QTE_LOG_FORMAT`, or the format given to `configure`, is not json or text. The value
    is not shown: a mistake could have put anything there."""

    code = _errors.LOG_FORMAT_INVALID


def configure(
    format: str | None = None,
    *,
    stream: TextIO | None = None,
    level: int = logging.WARNING,
    capture_warnings: bool = False,
) -> logging.Handler | None:
    """Set how the SDK's log records are written: `format` is "json" or "text", or None to
    read `QTE_LOG_FORMAT` now (unset or empty means text; case and surrounding spaces do
    not matter). Raises `LogFormatInvalid`, a `ValueError`, for any other value, changing
    nothing.

    "json" adds one handler to the `qte_sdk` logger, at `level`, writing each record as one
    JSON line to `stream`, or to whatever `sys.stderr` is at the time of each write, and
    returns it; with `capture_warnings`, SDK warnings shown are logged too (see the module's
    documentation). "text" removes that handler and stops the capture, if either was set,
    and returns None: records are then written as before `configure` was first called.
    Each call replaces what an earlier one set, so calling it twice leaves one handler."""
    chosen = _chosen(format)
    logger = logging.getLogger(_SDK_LOGGER)
    for handler in list(logger.handlers):
        if isinstance(handler, _JsonHandler):
            logger.removeHandler(handler)
    _release_warnings()
    if chosen == "text":
        return None
    handler = _JsonHandler(stream, level)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    if capture_warnings:
        _capture_warnings()
    return handler


@contextlib.contextmanager
def _for_command(json_output: bool) -> Iterator[None]:
    """`configure` as the SDK's commands call it, with warnings captured: "json" with
    `--json`, else as `QTE_LOG_FORMAT` says. Afterwards, the handlers and the capture are
    as they were before, so a program that runs a command's `main` keeps its own setup.
    Raises `LogFormatInvalid` as `configure` does, changing nothing."""
    logger = logging.getLogger(_SDK_LOGGER)
    handlers = [h for h in logger.handlers if isinstance(h, _JsonHandler)]
    showwarning, shown_before = warnings.showwarning, _shown_before
    configure("json" if json_output else None, capture_warnings=True)
    try:
        yield
    finally:
        _restore(logger, handlers, showwarning, shown_before)


def _restore(
    logger: logging.Logger,
    handlers: list[logging.Handler],
    showwarning: Callable[..., Any],
    shown_before: Callable[..., Any] | None,
) -> None:
    global _shown_before
    for handler in list(logger.handlers):
        if isinstance(handler, _JsonHandler):
            logger.removeHandler(handler)
    for handler in handlers:
        logger.addHandler(handler)
    warnings.showwarning, _shown_before = showwarning, shown_before


def _chosen(format: str | None) -> str:
    if format is None:
        value: object = os.environ.get(LOG_FORMAT_ENV_VAR, "")
        source = LOG_FORMAT_ENV_VAR
    else:
        value = format
        source = "the format argument of configure()"
    chosen = value.strip().casefold() if isinstance(value, str) else None
    if chosen == "" and format is None:
        return "text"
    if chosen not in FORMATS:
        raise LogFormatInvalid(fields={"source": source})
    return chosen


class _JsonHandler(logging.StreamHandler):
    """The handler `configure("json")` adds: a plain `StreamHandler`, whose stream is
    `sys.stderr` as it is at each write unless one was given, as logging's last resort
    does. It stays a plain `StreamHandler` so that the SDK's update check writes to it as it
    writes to any stream handler, without waiting (see `qte_sdk.update`)."""

    def __init__(self, stream: TextIO | None, level: int) -> None:
        logging.Handler.__init__(self, level)
        self._given = stream

    @property  # type: ignore[override]
    def stream(self) -> Any:
        return sys.stderr if self._given is None else self._given

    @stream.setter
    def stream(self, value: Any) -> None:
        self._given = value


class JsonFormatter(logging.Formatter):
    """Formats a log record as one line of JSON, as the module's documentation describes.
    Put it on a handler of your own to have JSON lines there."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            text = record.getMessage()
        except Exception:
            text = str(record.msg)
        code = _code_of(record, text)
        next_step = getattr(record, "next_step", None)
        if not isinstance(next_step, str):
            next_step = _fix(code)
        fields = getattr(record, "fields", None)
        line: dict[str, Any] = {
            "time": _utc(record),
            "level": record.levelname,
            "logger": record.name,
            "code": code,
            "message": message_of(text, code, next_step),
            "next_step": next_step,
            "fields": {str(k): v for k, v in fields.items()} if isinstance(fields, dict) else {},
        }
        if record.exc_info and record.exc_info[0] is not None:
            line["exception"] = record.exc_info[0].__name__
        try:
            return dumps(line)
        except (TypeError, ValueError):  # fields that cannot be written, such as a cycle
            line["fields"] = {}
            return dumps(line)


def dumps(value: object) -> str:
    """`value` as the SDK writes JSON: one line, ASCII only, compact; anything JSON has no
    form for is written as its str()."""
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str)


def message_of(text: str, code: str | None, next_step: str | None) -> str:
    """`text` without `<code>: ` in front and ` <next_step>.` at the end, when it has them:
    the part of a coded message that says what happened and why."""
    if code and text.startswith(f"{code}: "):
        text = text[len(code) + 2 :]
    if next_step:
        end = f" {next_step}."
        if text.endswith(end) and len(text) > len(end):
            text = text[: -len(end)]
    return text


def coded(
    code: str, /, *, withhold: Callable[[str], bool] | None = None, **fields: Any
) -> dict[str, Any]:
    """The `extra` for a log record with `code`: its code, the next step filled in from
    `fields`, and the fields. With `withhold` (as for `qte_sdk.errors.summary`), each text
    field it says yes to, one that holds the token say, is replaced by
    `qte_sdk.errors.WITHHELD`, in the fields and in the next step.

        logger.warning(message, extra=qte_sdk.logs.coded(code, path=path))"""
    shown = _errors.withheld(withhold, **fields) if withhold is not None else dict(fields)
    return {
        "code": code,
        "next_step": _errors.next_step(code, withhold=withhold, **fields),
        "fields": shown,
    }


def _code_of(record: logging.LogRecord, text: str) -> str | None:
    code = getattr(record, "code", None)
    if isinstance(code, str):
        return code
    for found in _CODE.findall(text):
        if found in _errors.CODES:
            return found
    return None


def _fix(code: str | None) -> str | None:
    entry = _errors.CODES.get(code) if code else None
    return None if entry is None else entry.fix.strip().rstrip(".")


def _utc(record: logging.LogRecord) -> str:
    when = datetime.fromtimestamp(record.created, UTC)
    return f"{when:%Y-%m-%dT%H:%M:%S}.{int(record.msecs):03d}Z"


# SDK warnings as log records


def _capture_warnings() -> None:
    global _shown_before
    if _shown_before is None:
        _shown_before = warnings.showwarning
        warnings.showwarning = _show_warning


def _release_warnings() -> None:
    global _shown_before
    if _shown_before is not None:
        if warnings.showwarning is _show_warning:
            warnings.showwarning = _shown_before
        _shown_before = None


def _show_warning(
    message: Warning | str,
    category: type[Warning],
    filename: str,
    lineno: int,
    file: TextIO | None = None,
    line: str | None = None,
) -> None:
    """Log an SDK warning through `WARNINGS_LOGGER`, with its code; show any other warning
    as it was shown before."""
    if file is None and isinstance(message, _errors.QteWarning):
        code = message.code
        fields = message.fields
        if code is not None and isinstance(fields, dict):
            next_step: str | None = _errors.next_step(code, **fields)
        else:
            next_step = _fix(code)
        logging.getLogger(WARNINGS_LOGGER).warning(
            "%s",
            str(message),
            extra={"code": code, "next_step": next_step, "fields": {"category": category.__name__}},
        )
        return
    show = _shown_before or _ORIGINAL_SHOWWARNING
    show(message, category, filename, lineno, file, line)
