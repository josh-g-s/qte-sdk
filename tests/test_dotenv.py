"""Reading QTE_URL and QTE_TOKEN from ./.env: sources and precedence, parsing, the
safeguards, and keeping the token out of everything the SDK shows."""

import logging
import os
import secrets
import shutil
import subprocess
import sys
import traceback
import warnings
from pathlib import Path

import pytest
from fake_exchange import CONTRACT_VERSION, frame, serve_local
from websockets.asyncio.server import ServerConnection

from qte_sdk import dotenv
from qte_sdk.dotenv import DotenvNotIgnored
from qte_sdk.reconnect import ReconnectingSession
from qte_sdk.session import (
    TOKEN_ENV_VAR,
    TOKEN_FILE_ENV_VAR,
    URL_ENV_VAR,
    MissingToken,
    MissingURL,
    open_session,
    resolve_token,
    resolve_url,
)

POSIX = os.name == "posix"
URL = "ws://127.0.0.1:8080/ws"


def synthetic_token() -> str:
    return secrets.token_urlsafe(32)


@pytest.fixture(autouse=True)
def no_token_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    monkeypatch.delenv(TOKEN_FILE_ENV_VAR, raising=False)


def write_dotenv(text: str | bytes, mode: int = 0o600, directory: Path | None = None) -> Path:
    """Write `.env` in the working directory (the conftest makes it a fresh temporary one)."""
    path = (directory or Path.cwd()) / ".env"
    data = text.encode() if isinstance(text, str) else text
    path.write_bytes(data)
    path.chmod(mode)
    return path


def shown(error: BaseException) -> str:
    """What a traceback showing local variables could print for `error` and its chain,
    leaving out this test module's own frames, which hold the token by design."""
    parts = [str(error), repr(error), repr(vars(error))]
    pending = [traceback.TracebackException.from_exception(error, capture_locals=True)]
    while pending:
        link = pending.pop()
        parts.extend(link.format_exception_only())
        for summary in link.stack:
            if summary.filename != __file__:
                parts.append(f"{summary.filename}:{summary.lineno} {summary.locals}")
        pending.extend(n for n in (link.__cause__, link.__context__) if n is not None)
    return "\n".join(parts)


def assert_token_absent(token: str, text: str) -> None:
    for start in range(len(token) - 7):
        assert token[start : start + 8] not in text


def assert_clean(error: BaseException, token: str) -> None:
    assert error.__cause__ is None and error.__context__ is None
    assert vars(error) == {}
    assert_token_absent(token, shown(error))


# Sources and precedence


def test_the_token_is_read_from_dotenv_when_nothing_else_is_set():
    token = synthetic_token()
    write_dotenv(f"QTE_TOKEN={token}\n")
    assert resolve_token() == token


def test_each_earlier_source_wins_over_dotenv(monkeypatch, tmp_path):
    write_dotenv(f"QTE_TOKEN={synthetic_token()}\n")
    from_file = synthetic_token()
    token_file = tmp_path / "token"
    token_file.write_text(from_file + "\n")
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(token_file))
    assert resolve_token() == from_file
    from_env = synthetic_token()
    monkeypatch.setenv(TOKEN_ENV_VAR, from_env)
    assert resolve_token() == from_env
    argument = synthetic_token()
    assert resolve_token(argument) == argument


def test_an_empty_qte_token_falls_through_to_dotenv(monkeypatch):
    token = synthetic_token()
    write_dotenv(f"QTE_TOKEN={token}\n")
    monkeypatch.setenv(TOKEN_ENV_VAR, "")
    assert resolve_token() == token


def test_an_empty_token_argument_is_still_refused():
    write_dotenv(f"QTE_TOKEN={synthetic_token()}\n")
    with pytest.raises(MissingToken):
        resolve_token("")


def test_a_bad_token_file_is_reported_rather_than_falling_through_to_dotenv(monkeypatch, tmp_path):
    write_dotenv(f"QTE_TOKEN={synthetic_token()}\n")
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(tmp_path / "absent"))
    with pytest.raises(MissingToken, match=TOKEN_FILE_ENV_VAR):
        resolve_token()


def test_only_the_working_directory_is_read(tmp_path, monkeypatch):
    write_dotenv(f"QTE_TOKEN={synthetic_token()}\nQTE_URL={URL}\n")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    with pytest.raises(MissingToken, match=r"\.env"):
        resolve_token()
    with pytest.raises(MissingURL):
        resolve_url()


def test_the_missing_token_message_names_every_source():
    with pytest.raises(MissingToken) as caught:
        resolve_token()
    message = str(caught.value)
    for name in ("token=", TOKEN_ENV_VAR, TOKEN_FILE_ENV_VAR, ".env"):
        assert name in message


def test_the_address_comes_from_the_argument_then_the_environment_then_dotenv(monkeypatch):
    write_dotenv(f"QTE_URL={URL}\n")
    assert resolve_url() == URL
    monkeypatch.setenv(URL_ENV_VAR, "ws://127.0.0.1:9/env")
    assert resolve_url() == "ws://127.0.0.1:9/env"
    assert resolve_url("ws://127.0.0.1:9/arg") == "ws://127.0.0.1:9/arg"


def test_no_address_raises_an_error_naming_both_places():
    with pytest.raises(MissingURL) as caught:
        resolve_url()
    assert URL_ENV_VAR in str(caught.value) and ".env" in str(caught.value)
    with pytest.raises(MissingURL):
        resolve_url("")


def test_dotenv_never_changes_the_environment():
    before = dict(os.environ)
    write_dotenv(f"QTE_TOKEN={synthetic_token()}\nQTE_URL={URL}\nOTHER=1\n")
    resolve_token()
    resolve_url()
    assert dict(os.environ) == before


def test_the_file_is_read_on_each_call():
    first, second = synthetic_token(), synthetic_token()
    write_dotenv(f"QTE_TOKEN={first}\n")
    assert resolve_token() == first
    write_dotenv(f"QTE_TOKEN={second}\n")
    assert resolve_token() == second


async def test_a_session_opens_with_the_address_and_token_from_dotenv():
    token = synthetic_token()
    received: list[str] = []

    async def handler(ws: ServerConnection) -> None:
        received.append(str(await ws.recv()))
        payload = {
            "session_id": "s-1",
            "team": "team-a",
            "server_time": "1",
            "contract_version": CONTRACT_VERSION,
            "unscored": True,
        }
        await ws.send(frame("session_ack", payload, 1))
        await ws.wait_closed()

    async with serve_local(handler) as url:
        write_dotenv(f'QTE_URL="{url}"\nQTE_TOKEN={token}\n')
        async with await open_session() as session:
            assert session.connection.url == url
    assert token in received[0]


async def test_open_session_raises_missing_url_before_connecting():
    with pytest.raises(MissingURL):
        await open_session(token=synthetic_token())


def test_a_reconnecting_session_resolves_its_address_once():
    write_dotenv(f"QTE_URL={URL}\nQTE_TOKEN={synthetic_token()}\n")
    session = ReconnectingSession()
    assert session.url == URL
    with pytest.raises(MissingURL):
        ReconnectingSession(token=synthetic_token(), url="")


# Parsing


@pytest.mark.parametrize(
    "line",
    [
        "QTE_TOKEN={t}",
        "export QTE_TOKEN={t}",
        "export\tQTE_TOKEN={t}",
        "  QTE_TOKEN = {t}  ",
        "QTE_TOKEN='{t}'",
        'QTE_TOKEN="{t}"',
        "QTE_TOKEN={t} # the practice token",
        "QTE_TOKEN='{t}' # quoted, then a comment",
        'export QTE_TOKEN="{t}"#no space before the comment',
    ],
)
def test_each_written_form_gives_the_token(line: str):
    token = synthetic_token()
    write_dotenv(f"# a comment\n\n{line.format(t=token)}\n")
    assert resolve_token() == token


def test_windows_line_endings_and_a_byte_order_mark_are_accepted():
    token = synthetic_token()
    write_dotenv(f"﻿QTE_URL={URL}\r\nQTE_TOKEN={token}\r\n")
    assert resolve_token() == token
    assert resolve_url() == URL


def test_a_hash_inside_quotes_is_part_of_the_value():
    write_dotenv("QTE_TOKEN='abc #def'\n")
    assert resolve_token() == "abc #def"


def test_the_last_assignment_wins():
    token = synthetic_token()
    write_dotenv(f"QTE_TOKEN={synthetic_token()}\nQTE_TOKEN={token}\n")
    assert resolve_token() == token


def test_other_names_are_not_read_whatever_their_syntax():
    token = synthetic_token()
    write_dotenv(
        f'OTHER="unterminated\nnot an assignment\nMULTI="a b\nQTE_TOKENS=x\nQTE_TOKEN={token}\n'
    )
    assert resolve_token() == token


@pytest.mark.parametrize("value", ["", "''", '""', " # nothing"])
def test_an_empty_value_counts_as_unset(value: str):
    write_dotenv(f"QTE_TOKEN={value}\nQTE_URL={value}\n")
    with pytest.raises(MissingToken, match="pass token="):
        resolve_token()
    with pytest.raises(MissingURL, match="pass url="):
        resolve_url()


def test_a_commented_out_token_is_not_read():
    write_dotenv(f"# QTE_TOKEN={synthetic_token()}\n")
    with pytest.raises(MissingToken, match="pass token="):
        resolve_token()


MALFORMED = {
    "no closing quote": "QTE_TOKEN='{t}\n",
    "no closing double quote": 'QTE_TOKEN="{t}\n',
    "text after the quote": "QTE_TOKEN='{t}'{t}\n",
    "whitespace": "QTE_TOKEN={t} {t}\n",
    "a stray quote": "QTE_TOKEN={t}'\n",
}


@pytest.mark.parametrize("case", MALFORMED)
def test_a_malformed_token_line_is_refused_without_echoing_it(case: str):
    token = synthetic_token()
    write_dotenv("# first\n" + MALFORMED[case].format(t=token))
    with pytest.raises(MissingToken, match=r"\.env line 2: QTE_TOKEN") as caught:
        resolve_token()
    assert_clean(caught.value, token)


def test_a_malformed_address_line_is_refused_without_echoing_it():
    write_dotenv(f"QTE_URL='{URL}\n")
    with pytest.raises(MissingURL, match=r"line 1: QTE_URL") as caught:
        resolve_url()
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "127.0.0.1" not in shown(caught.value)


@pytest.mark.parametrize("where", ["before", "after"])
def test_a_dotenv_that_is_not_utf8_is_refused_without_its_contents(where: str):
    token = synthetic_token()
    data = (
        b"\xff" + f"QTE_TOKEN={token}\n".encode()
        if where == "before"
        else f"QTE_TOKEN={token}\n".encode() + b"\xc3\x28\n"
    )
    write_dotenv(data)
    with pytest.raises(MissingToken, match="not UTF-8") as caught:
        resolve_token()
    assert_clean(caught.value, token)


def test_a_dotenv_that_is_a_directory_is_refused():
    (Path.cwd() / ".env").mkdir()
    with pytest.raises(MissingToken, match=r"\.env cannot be read"):
        resolve_token()


@pytest.mark.skipif(not POSIX or os.geteuid() == 0, reason="needs POSIX permissions")
def test_an_unreadable_dotenv_is_refused_without_its_contents():
    token = synthetic_token()
    write_dotenv(f"QTE_TOKEN={token}\n", mode=0)
    with pytest.raises(MissingToken, match=r"\.env cannot be read") as caught:
        resolve_token()
    assert_clean(caught.value, token)


def test_reading_dotenv_logs_nothing(caplog):
    token = synthetic_token()
    with caplog.at_level(logging.DEBUG):
        write_dotenv(f"QTE_TOKEN={token}\n")
        assert resolve_token() == token
        write_dotenv(f"QTE_TOKEN='{token}\n")
        with pytest.raises(MissingToken):
            resolve_token()
    assert caplog.records == []


# Permissions


@pytest.mark.skipif(not POSIX, reason="POSIX permissions")
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660])
def test_a_token_others_can_read_is_refused_with_the_fix(mode: int):
    token = synthetic_token()
    write_dotenv(f"QTE_TOKEN={token}\n", mode=mode)
    with pytest.raises(MissingToken, match="chmod 600 .env") as caught:
        resolve_token()
    assert_clean(caught.value, token)


@pytest.mark.skipif(not POSIX, reason="POSIX permissions")
def test_a_readable_dotenv_without_a_token_still_gives_the_address(monkeypatch):
    write_dotenv(f"QTE_URL={URL}\n", mode=0o644)
    assert resolve_url() == URL
    token = synthetic_token()
    monkeypatch.setenv(TOKEN_ENV_VAR, token)
    assert resolve_token() == token


@pytest.mark.skipif(not POSIX, reason="POSIX permissions")
def test_a_readable_dotenv_is_not_read_for_the_token_when_another_source_has_it(monkeypatch):
    write_dotenv(f"QTE_TOKEN={synthetic_token()}\nQTE_URL={URL}\n", mode=0o644)
    token = synthetic_token()
    monkeypatch.setenv(TOKEN_ENV_VAR, token)
    assert resolve_token() == token
    assert resolve_url() == URL


@pytest.mark.skipif(not POSIX, reason="POSIX permissions")
def test_a_symlinked_dotenv_is_judged_by_the_file_it_points_to(tmp_path):
    token = synthetic_token()
    target = tmp_path / "real.env"
    target.write_text(f"QTE_TOKEN={token}\n")
    target.chmod(0o644)
    (Path.cwd() / ".env").symlink_to(target)
    with pytest.raises(MissingToken, match="chmod 600"):
        resolve_token()
    target.chmod(0o600)
    assert resolve_token() == token


# The git warning

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not on the PATH")


def git(*args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "QTE_"))}
    subprocess.run(["git", *args], check=True, capture_output=True, env=env)


@pytest.fixture
def repository() -> Path:
    git("init", "-q", ".")
    return Path.cwd()


def warned(call) -> list[warnings.WarningMessage]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        call()
    return [w for w in caught if issubclass(w.category, DotenvNotIgnored)]


@needs_git
def test_a_dotenv_git_does_not_ignore_warns_once_and_is_still_used(repository):
    token = synthetic_token()
    write_dotenv(f"QTE_TOKEN={token}\nQTE_URL={URL}\n")
    first = warned(lambda: resolve_token())
    assert len(first) == 1
    assert ".gitignore" in str(first[0].message)
    assert_token_absent(token, str(first[0].message))
    assert warned(lambda: resolve_url()) == []
    assert resolve_token() == token


@needs_git
def test_a_dotenv_in_a_subdirectory_of_a_repository_warns(repository, monkeypatch):
    child = repository / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    write_dotenv(f"QTE_URL={URL}\n")
    assert len(warned(lambda: resolve_url())) == 1


@needs_git
def test_an_ignored_dotenv_does_not_warn(repository):
    (repository / ".gitignore").write_text(".env\n")
    write_dotenv(f"QTE_TOKEN={synthetic_token()}\n")
    assert warned(lambda: resolve_token()) == []


@needs_git
def test_a_tracked_dotenv_warns_even_when_gitignore_names_it(repository):
    (repository / ".gitignore").write_text(".env\n")
    write_dotenv(f"QTE_URL={URL}\n")
    git("add", "-f", ".env")
    assert len(warned(lambda: resolve_url())) == 1


def test_no_warning_outside_a_repository_and_git_is_not_run(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("git should not run outside a working tree")

    monkeypatch.setattr(dotenv.subprocess, "run", refuse)
    write_dotenv(f"QTE_URL={URL}\n")
    assert warned(lambda: resolve_url()) == []


def test_no_warning_and_no_failure_without_git(monkeypatch):
    (Path.cwd() / ".git").mkdir()
    monkeypatch.setattr(dotenv.shutil, "which", lambda name: None)
    write_dotenv(f"QTE_URL={URL}\n")
    assert warned(lambda: resolve_url()) == []
    assert resolve_url() == URL


@pytest.mark.parametrize("failure", ["oserror", "timeout", "exit 128"])
def test_a_failing_git_neither_warns_nor_blocks(monkeypatch, failure: str):
    (Path.cwd() / ".git").mkdir()

    def broken(args, **kwargs):
        if failure == "oserror":
            raise OSError("no such file")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 5)
        return subprocess.CompletedProcess(args, 128)

    monkeypatch.setattr(dotenv.subprocess, "run", broken)
    write_dotenv(f"QTE_URL={URL}\n")
    assert warned(lambda: resolve_url()) == []
    assert resolve_url() == URL


def test_git_is_never_given_the_token(monkeypatch):
    (Path.cwd() / ".git").mkdir()
    token = synthetic_token()
    monkeypatch.setenv(TOKEN_ENV_VAR, token)
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, "")
    calls: list[tuple] = []

    def record(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 1)

    monkeypatch.setattr(dotenv.subprocess, "run", record)
    write_dotenv(f"QTE_URL={URL}\n")
    assert len(warned(lambda: resolve_url())) == 1
    ((args, kwargs),) = calls
    assert token not in " ".join(args)
    assert not any(name.startswith("QTE_") for name in kwargs["env"])
    assert token not in repr(kwargs)


def test_the_warning_as_an_error_carries_no_token(monkeypatch):
    (Path.cwd() / ".git").mkdir()
    monkeypatch.setattr(
        dotenv.subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 1)
    )
    token = synthetic_token()
    write_dotenv(f"QTE_TOKEN={token}\n")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DotenvNotIgnored)
        with pytest.raises(DotenvNotIgnored) as caught:
            resolve_token()
    assert_token_absent(token, shown(caught.value))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths")
def test_the_warning_names_the_file(monkeypatch):
    (Path.cwd() / ".git").mkdir()
    monkeypatch.setattr(
        dotenv.subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 1)
    )
    path = write_dotenv(f"QTE_URL={URL}\n")
    (warning,) = warned(lambda: resolve_url())
    assert str(path) in str(warning.message)
