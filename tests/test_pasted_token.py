"""A token pasted where a path, address, instrument, channel, date or name was expected is
never shown: not in a warning or an exception's str(), args or repr, not in a log record,
and not in the locals of the SDK's frames a traceback would show.

Each place is fed the token in the forms a message could show it: as written,
percent-encoded, as a repr writes it (a backslash and an n where the text holds a line
break) and in one line (a tab made a space). Where the SDK has the token at hand it looks
for it; where it has none (a `Session`'s address, say) it withholds text shaped like a token
the exchange issues, a run of 32 hex digits."""

import logging
import os
import secrets
import shutil
import subprocess
import traceback
import warnings
from collections.abc import Callable
from datetime import date
from pathlib import Path
from urllib.parse import quote

import pytest
from test_fileaccess import Windows

from qte_sdk import dotenv, errors, history, replay
from qte_sdk._fileaccess import BroadAccess, LinkFolder
from qte_sdk.connection import Connection, SessionInfo
from qte_sdk.contract.v1.session_pb2 import Auth
from qte_sdk.dotenv import (
    AddressFileShared,
    DotenvNotIgnored,
    FileShared,
    TokenFileShared,
    shared_message,
)
from qte_sdk.history import HistoryClient
from qte_sdk.reconnect import NotConnected, ReconnectingSession
from qte_sdk.session import (
    TOKEN_ENV_VAR,
    TOKEN_FILE_ENV_VAR,
    MissingToken,
    Session,
    _Secret,
    _token_forms,
    open_session,
    resolve_token,
    resolve_url,
)

POSIX = os.name == "posix"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not on the PATH")


def synthetic_token() -> str:
    return secrets.token_urlsafe(32)


def minted_token() -> str:
    """A token as the exchange issues one: 32 random bytes as 64 hex digits."""
    return secrets.token_hex(32)


@pytest.fixture(autouse=True)
def no_token_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    monkeypatch.delenv(TOKEN_FILE_ENV_VAR, raising=False)


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> Windows:
    return Windows(monkeypatch)


def assert_no_form_of(token: str, text: str) -> None:
    """No run of 8 characters of `token`, and none of the forms the SDK looks for, is in
    `text`."""
    for start in range(len(token) - 7):
        assert token[start : start + 8] not in text, text
    for form in _token_forms(_Secret(token)):
        assert form.value not in text, text


def shown(error: BaseException) -> str:
    """What a traceback showing local variables could print for `error`, its causes and
    contexts, leaving out this module's own frames, which hold the token by design."""
    parts = [str(error), repr(error), repr(error.args), repr(vars(error))]
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


def warning_shown(warning: Warning) -> str:
    return "\n".join([str(warning), repr(warning), repr(warning.args)])


# The forms a pasted token can take in a name, each as (the token, the name that holds it).
# A name in a path cannot hold a slash, nor, on Windows, a line break or a tab.


def as_written() -> tuple[str, str]:
    token = synthetic_token()
    return token, token


def percent_encoded() -> tuple[str, str]:
    token = synthetic_token() + "/+=" + synthetic_token()
    return token, quote(token, safe="")


def as_a_repr() -> tuple[str, str]:
    # A token holding a backslash and an n: a name with a line break there is shown so.
    token = synthetic_token() + "\\n" + synthetic_token()
    return token, token.replace("\\n", "\n")


def in_one_line() -> tuple[str, str]:
    # A token holding a tab: a message shows it, in one line, with a space there.
    token = synthetic_token() + "\t" + synthetic_token()
    return token, token.replace("\t", " ")


FORMS = [
    pytest.param(as_written, id="as-written"),
    pytest.param(percent_encoded, id="percent-encoded"),
    pytest.param(
        as_a_repr,
        id="as-a-repr",
        marks=pytest.mark.skipif(not POSIX, reason="a name with a line break: POSIX only"),
    ),
    pytest.param(in_one_line, id="in-one-line"),
]


def folder_named(name: str, tmp_path: Path) -> Path:
    folder = tmp_path / name
    folder.mkdir()
    return folder


# 1. The Windows warning about a token file whose path holds the token (simulated Windows)


@pytest.mark.parametrize("form", FORMS)
def test_a_shared_token_file_named_with_the_token_withholds_its_path(
    windows, monkeypatch, tmp_path, form
):
    token, name = form()
    path = folder_named(name, tmp_path) / "token"
    path.write_text(token + "\n")
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(path))
    with pytest.warns(TokenFileShared) as caught:
        assert resolve_token() == token
    message = caught[0].message
    assert errors.WITHHELD in str(message)
    assert "Everyone" in str(message) or "Authenticated Users" in str(message)
    assert_no_form_of(token, warning_shown(message))


@pytest.mark.parametrize("form", FORMS)
def test_a_shared_dotenv_in_a_folder_named_with_its_token_withholds_the_path(
    windows, monkeypatch, tmp_path, form
):
    token, name = form()
    monkeypatch.chdir(folder_named(name, tmp_path))
    Path(".env").write_text(f'QTE_TOKEN="{token}"\n')
    Path(".env").chmod(0o600)
    with pytest.warns(TokenFileShared) as caught:
        assert resolve_token() == token
    message = caught[0].message
    assert errors.WITHHELD in str(message)
    assert_no_form_of(token, warning_shown(message))


def test_an_address_file_in_a_folder_named_with_the_token_withholds_the_path(
    windows, monkeypatch, tmp_path
):
    token = minted_token()
    monkeypatch.chdir(folder_named(token, tmp_path))
    Path(".env").write_text("QTE_URL=ws://127.0.0.1:8080/ws\n")
    with pytest.warns(AddressFileShared) as caught:
        assert resolve_url() == "ws://127.0.0.1:8080/ws"
    assert errors.WITHHELD in str(caught[0].message)
    assert_no_form_of(token, warning_shown(caught[0].message))


def test_a_shared_token_file_warning_made_an_error_logs_no_form_of_the_token(
    windows, monkeypatch, tmp_path, caplog
):
    token, name = percent_encoded()
    path = folder_named(name, tmp_path) / "token"
    path.write_text(token)
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(path))
    with warnings.catch_warnings():
        warnings.simplefilter("error", FileShared)
        with caplog.at_level(logging.DEBUG):
            assert resolve_token() == token
    assert errors.WITHHELD in caplog.text
    assert_no_form_of(token, caplog.text)
    for record in caplog.records:
        assert_no_form_of(token, f"{record.getMessage()} {record.args!r} {vars(record)!r}")


def test_every_path_a_shared_file_warning_names_is_withheld_if_it_holds_the_token():
    token = synthetic_token()
    secret = _Secret(token)
    folder = f"C:\\Users\\me\\{token}"
    access = BroadAccess(
        read=("Everyone",),
        folder=("Everyone",),
        folder_owner=True,
        link_folder=("Everyone",),
        unseen=(f"the list of {folder}\\x could not be seen",),
        unfollowed=f"the link {folder}\\loop leads round in a loop",
        file=f"{folder}\\token",
        folder_path=folder,
        links=(f"D:\\{token}\\link", f"D:\\{token}\\again"),
        link_folders=(LinkFolder(f"D:\\{token}", (f"D:\\{token}\\link",), ("Everyone",), True),),
    )
    message = shared_message(
        Path(f"D:\\{token}\\link"),
        access,
        withhold=lambda text: token in text or secret.value in text,
    )
    assert errors.WITHHELD in message
    assert 'icacls "' + errors.WITHHELD not in message
    assert_no_form_of(token, message)


def test_a_token_file_named_with_the_token_that_cannot_be_read_names_no_path(monkeypatch, tmp_path):
    # On every system: the refusal names no path, so it cannot show one holding the token.
    token, name = as_written()
    folder = folder_named(name, tmp_path)
    for target in (folder, folder / "missing"):
        monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(target))
        with pytest.raises(MissingToken) as caught:
            resolve_token()
        assert_no_form_of(token, shown(caught.value))


# 2. The git warning about a .env whose path holds the token


def git(*args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "QTE_"))}
    subprocess.run(["git", *args], check=True, capture_output=True, env=env)


def git_warning(call: Callable[[], object]) -> Warning:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        call()
    found = [w.message for w in caught if issubclass(w.category, DotenvNotIgnored)]
    assert len(found) == 1, found
    return found[0]


@needs_git
@pytest.mark.parametrize("form", FORMS)
def test_the_git_warning_withholds_a_dotenv_path_holding_the_token_in_the_environment(
    monkeypatch, tmp_path, form
):
    token, name = form()
    monkeypatch.chdir(folder_named(name, tmp_path))
    git("init", "-q", ".")
    Path(".env").write_text("QTE_URL=ws://127.0.0.1:8080/ws\n")
    monkeypatch.setenv(TOKEN_ENV_VAR, token)
    message = git_warning(resolve_url)
    assert errors.WITHHELD in str(message)
    assert_no_form_of(token, warning_shown(message))


@needs_git
@pytest.mark.parametrize("tracked", [False, True], ids=["not-ignored", "tracked"])
def test_the_git_warning_withholds_a_dotenv_path_shaped_like_a_minted_token(
    monkeypatch, tmp_path, tracked
):
    # No token is at hand before the .env is read: the shape of one the exchange issues is
    # withheld.
    token = minted_token()
    monkeypatch.chdir(folder_named(f"x{token}y", tmp_path))
    git("init", "-q", ".")
    Path(".env").write_text(f"QTE_TOKEN={token}\n")
    Path(".env").chmod(0o600)
    if tracked:
        git("add", ".env")
    message = git_warning(resolve_token)
    assert getattr(message, "code", None) == (
        errors.DOTENV_TRACKED if tracked else errors.DOTENV_NOT_IGNORED
    )
    assert errors.WITHHELD in str(message)
    assert_no_form_of(token, warning_shown(message))


@needs_git
def test_the_git_warning_made_an_error_logs_no_form_of_the_token(monkeypatch, tmp_path, caplog):
    token = minted_token()
    monkeypatch.chdir(folder_named(token, tmp_path))
    git("init", "-q", ".")
    Path(".env").write_text("QTE_URL=ws://127.0.0.1:8080/ws\n")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DotenvNotIgnored)
        with caplog.at_level(logging.DEBUG):
            resolve_url()
    assert errors.WITHHELD in caplog.text
    assert_no_form_of(token, caplog.text)


@needs_git
def test_the_git_warning_still_names_an_ordinary_path(monkeypatch, tmp_path):
    folder = folder_named("test_a_long_snake_case_folder_name_for_a_project_2026", tmp_path)
    monkeypatch.chdir(folder)
    git("init", "-q", ".")
    Path(".env").write_text("QTE_URL=ws://127.0.0.1:8080/ws\n")
    message = git_warning(resolve_url)
    assert errors.WITHHELD not in str(message)
    assert folder.name in str(message)


# 3. The reprs of a session, a connection, a reconnecting session and a history client


def percent_every_character(text: str) -> str:
    return "".join(f"%{ord(c):02X}" for c in text)


def session_at(url: str) -> Session:
    info = SessionInfo("s-1", "team-a", 0, "1.0", False)
    return Session(Connection(url), info, [])


@pytest.mark.parametrize(
    "place",
    [
        "wss://exchange.example.test/ws?token={}",
        "wss://{}@exchange.example.test/ws",
        "wss://exchange.example.test/{}/ws",
        "{}",
    ],
    ids=["query", "userinfo", "path", "whole"],
)
@pytest.mark.parametrize("encode", [str, percent_every_character], ids=["as-written", "encoded"])
def test_a_session_s_repr_withholds_an_address_holding_a_minted_token(place, encode):
    token = minted_token()
    url = place.format(encode(token))
    session = session_at(url)
    assert errors.WITHHELD in repr(session)
    assert_no_form_of(token, repr(session))
    assert_no_form_of(token, repr(session.connection))


def test_a_session_s_repr_still_names_an_ordinary_address():
    session = session_at("wss://api.trading.example.test/ws")
    assert repr(session).startswith("Session('wss://api.trading.example.test/ws', ")


def test_a_reconnecting_session_and_a_client_withhold_an_address_whose_repr_is_the_token():
    token, given = as_a_repr()
    client = HistoryClient(f"https://history.example.test/{given}", token)
    session = ReconnectingSession(f"wss://exchange.example.test/ws?token={given}", token)
    for text in (repr(client), repr(session)):
        assert errors.WITHHELD in text
        assert_no_form_of(token, text)


def test_a_reconnecting_session_and_a_client_withhold_a_minted_token_they_were_not_given():
    other = minted_token()
    client = HistoryClient(f"https://history.example.test/{other}", synthetic_token())
    session = ReconnectingSession(f"wss://exchange.example.test/{other}", synthetic_token())
    for text in (repr(client), repr(session)):
        assert errors.WITHHELD in text
        assert_no_form_of(other, text)


# 4. Replay's argument checks


# A history client refuses a token holding a tab, so it has no one-line form here.
@pytest.mark.parametrize("form", FORMS[:3])
@pytest.mark.parametrize("where", ["date", "channel"])
def test_replay_withholds_a_token_passed_as_a_date_or_channel(form, where):
    token, given = form()
    client = HistoryClient("http://127.0.0.1:9", token)
    if where == "date":
        arguments: tuple[object, ...] = (given, ["AAA"])
    else:
        arguments = (date(2026, 1, 5), ["AAA"], ["book", given])
    with pytest.raises(ValueError) as caught:
        replay.replay(client, *arguments)
    assert errors.WITHHELD in str(caught.value)
    assert_no_form_of(token, shown(caught.value))


@pytest.mark.parametrize("where", ["date", "channel"])
def test_replay_withholds_a_minted_token_from_a_client_without_one(where):
    token = minted_token()
    client = object()  # a stand-in with no token: only the shape is looked for
    if where == "date":
        arguments: tuple[object, ...] = (token, ["AAA"])
    else:
        arguments = (date(2026, 1, 5), ["AAA"], [token])
    with pytest.raises(ValueError) as caught:
        replay.replay(client, *arguments)  # type: ignore[arg-type]
    assert errors.WITHHELD in str(caught.value)
    assert_no_form_of(token, shown(caught.value))


def test_replay_keeps_an_ordinary_bad_argument_in_its_error():
    client = HistoryClient("http://127.0.0.1:9", synthetic_token())
    with pytest.raises(ValueError, match="Invalid isoformat string: '5 January 2026'"):
        replay.replay(client, "5 January 2026", ["AAA"])
    with pytest.raises(ValueError, match=r"unknown channels \['official_close'\]"):
        replay.replay(client, date(2026, 1, 5), ["AAA"], ["book", "official_close"])


# 5. Percent-encoded text: a send type, a replay instrument and the history service's text


async def test_a_percent_encoded_token_as_a_send_type_is_withheld():
    token, given = percent_encoded()
    session = ReconnectingSession("ws://127.0.0.1:9", token)
    with pytest.raises(NotConnected) as caught:
        await session.send(given, Auth())
    assert errors.WITHHELD in str(caught.value)
    assert_no_form_of(token, shown(caught.value))


def test_a_percent_encoded_token_as_a_replay_instrument_is_withheld():
    token, given = percent_encoded()
    client = HistoryClient("http://127.0.0.1:9", token)
    error = replay._out_of_order(client, "2026-01-05", replay._Stream(None, 0, given), 5)
    assert errors.WITHHELD in str(error)
    assert_no_form_of(token, shown(error))


@pytest.mark.parametrize("make", [synthetic_token, minted_token], ids=["urlsafe", "minted"])
def test_the_history_service_s_text_holding_the_token_percent_encoded_is_dropped(make):
    # Every character encoded: no run of the token as sent is left for the screen to find.
    token = make()
    given = percent_every_character(token)
    assert history._server_text(f"no such object {given}", _Secret(token)) is None
    assert history._server_text("no such object", _Secret(token)) == "no such object"


@pytest.mark.parametrize("given", [False, True], ids=["from-qte-url", "passed"])
async def test_a_failed_connect_to_an_address_holding_the_token_shows_it_in_no_frame(
    monkeypatch, given
):
    token = synthetic_token()
    address = f"ws://127.0.0.1:1/{token}"
    if not given:
        monkeypatch.setenv("QTE_URL", address)
    with pytest.raises(OSError) as caught:
        await open_session(address if given else None, token=token, ack_timeout=5)
    assert_no_form_of(token, shown(caught.value))


async def test_a_token_passed_as_the_address_with_no_token_shows_it_in_no_frame():
    token = synthetic_token()
    with pytest.raises(MissingToken) as caught:
        await open_session(f"ws://127.0.0.1:1/{token}")
    assert_no_form_of(token, shown(caught.value))


async def test_a_refused_connection_option_shows_an_address_holding_the_token_in_no_frame():
    token = synthetic_token()
    with pytest.raises(ValueError, match="liveness_timeout") as caught:
        await open_session(f"ws://127.0.0.1:1/{token}", token=token, liveness_timeout=0)
    assert_no_form_of(token, shown(caught.value))


def interrupt_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(warnings, "showwarning", interrupted)


def test_a_ctrl_c_while_warning_about_a_shared_token_file_shows_its_path_in_no_frame(
    windows, monkeypatch, tmp_path
):
    token, name = as_written()
    path = folder_named(name, tmp_path) / "token"
    path.write_text(token)
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(path))
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        interrupt_warnings(monkeypatch)
        with pytest.raises(KeyboardInterrupt) as caught:
            resolve_token()
    assert_no_form_of(token, shown(caught.value))


def test_a_ctrl_c_while_warning_about_a_shared_dotenv_shows_its_path_in_no_frame(
    windows, monkeypatch, tmp_path
):
    token, name = as_written()
    monkeypatch.chdir(folder_named(name, tmp_path))
    Path(".env").write_text(f"QTE_TOKEN={token}\n")
    Path(".env").chmod(0o600)
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        interrupt_warnings(monkeypatch)
        with pytest.raises(KeyboardInterrupt) as caught:
            resolve_token()
    assert_no_form_of(token, shown(caught.value))


@needs_git
def test_a_ctrl_c_while_giving_the_git_warning_shows_the_path_in_no_frame(monkeypatch, tmp_path):
    token = minted_token()
    monkeypatch.chdir(folder_named(token, tmp_path))
    git("init", "-q", ".")
    Path(".env").write_text("QTE_URL=ws://127.0.0.1:8080/ws\n")
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        interrupt_warnings(monkeypatch)
        with pytest.raises(KeyboardInterrupt) as caught:
            resolve_url()
    assert_no_form_of(token, shown(caught.value))


@needs_git
def test_a_ctrl_c_while_logging_the_git_warning_made_an_error_shows_the_path_in_no_frame(
    monkeypatch, tmp_path
):
    token = minted_token()
    monkeypatch.chdir(folder_named(token, tmp_path))
    git("init", "-q", ".")
    Path(".env").write_text("QTE_URL=ws://127.0.0.1:8080/ws\n")

    def interrupted(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(dotenv._log, "warning", interrupted)
    with warnings.catch_warnings():
        warnings.simplefilter("error", DotenvNotIgnored)
        with pytest.raises(KeyboardInterrupt) as caught:
            resolve_url()
    assert_no_form_of(token, shown(caught.value))


@needs_git
def test_the_git_warning_made_an_error_is_logged_without_its_traceback(
    monkeypatch, tmp_path, caplog
):
    token = minted_token()
    monkeypatch.chdir(folder_named(token, tmp_path))
    git("init", "-q", ".")
    Path(".env").write_text("QTE_URL=ws://127.0.0.1:8080/ws\n")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DotenvNotIgnored)
        with caplog.at_level(logging.WARNING):
            resolve_url()
    logged = [arg for r in caplog.records for arg in r.args if isinstance(arg, Warning)]
    assert logged
    for warning in logged:
        assert warning.__traceback__ is None
        assert_no_form_of(token, shown(warning))


def test_a_shared_file_warning_made_an_error_is_logged_without_its_traceback(
    windows, monkeypatch, tmp_path, caplog
):
    token, name = as_written()
    path = folder_named(name, tmp_path) / "token"
    path.write_text(token)
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(path))
    with warnings.catch_warnings():
        warnings.simplefilter("error", FileShared)
        with caplog.at_level(logging.WARNING):
            assert resolve_token() == token
    logged = [arg for r in caplog.records for arg in r.args if isinstance(arg, Warning)]
    assert logged
    for warning in logged:
        assert warning.__traceback__ is None
        assert_no_form_of(token, shown(warning))
