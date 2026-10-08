"""The codes on the SDK's warnings and errors: the registry in `qte_sdk.errors`, its
entries in docs/errors.md, the codes in the source, and that no message shows the token."""

import ast
import asyncio
import importlib
import json
import os
import pkgutil
import re
import shutil
import subprocess
import traceback
import warnings
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from fake_exchange import serve_local
from test_resume import Scripted, heartbeat, resume_reject
from test_session import Server, ack, assert_no_form_of, session_reject, synthetic_token
from test_token import PRIVATE_FILE, SECOND_DRIVE, answers, windows  # noqa: F401
from websockets.asyncio.server import serve

import qte_sdk
from qte_sdk import dotenv, errors, update
from qte_sdk import token as token_command
from qte_sdk.connection import (
    ContractVersionMismatch,
    HandshakeFailed,
    LivenessTimeout,
    SessionRejected,
)
from qte_sdk.dotenv import AddressFileShared, DotenvNotIgnored, TokenFileShared
from qte_sdk.errors import CODES, RETIRED, QteError, QteWarning, render
from qte_sdk.session import (
    TOKEN_ENV_VAR,
    TOKEN_FILE_ENV_VAR,
    URL_ENV_VAR,
    AuthNotSent,
    MissingToken,
    MissingURL,
    ResumeNotAcknowledged,
    ResumeRejected,
    SessionNotAcknowledged,
    SessionTimeout,
    _holds_token,
    _Secret,
    open_session,
    resolve_token,
    resolve_url,
)

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs" / "errors.md"
CODE_SHAPE = re.compile(
    r"^QTE-(TOKEN|ADDRESS|DOTENV|SESSION|CONNECT|HISTORY|REPLAY|UPDATE)(-[A-Z0-9]+)+$"
)
CODE_IN_TEXT = re.compile(r"QTE-[A-Z0-9]+(?:-[A-Z0-9]+)+")
URL = "ws://127.0.0.1:8080/ws"

# Codes in a tagged release. A code here must never disappear from `CODES` without moving
# to `RETIRED`. Add a release's new codes when it is tagged; none is released yet.
RELEASED: frozenset[str] = frozenset()

# A realistic value for every field a template may take. None is the token.
FIELD_VALUES: dict[str, object] = {
    "action": "write",
    "command": "`git rm --cached .env` in /home/student/algo",
    "fix": "Choose another path with --file PATH",
    "kind": "InvalidStatus",
    "latest": "1.2.0",
    "line": 3,
    "name": ".env",
    "path": "/home/student/algo/.env",
    "problem": "is not UTF-8 text",
    "reason": "NOT_AUTHENTICATED: unknown credentials",
    "reason_name": "NOT_AUTHENTICATED",
    "seconds": 10.0,
    "source": "the QTE_URL environment variable",
    "status": ", HTTP 401",
    "step": "Ask the Head of Technology about your team's access",
    "strerror": "Permission denied",
    "version": "1.1.1",
    "what": "the connection closed before session_ack (close code 1011)",
    "where": "token=, QTE_TOKEN, QTE_TOKEN_FILE or ./.env",
    "why": "The exchange is busy or down",
}


@pytest.fixture(autouse=True)
def no_token_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    monkeypatch.delenv(TOKEN_FILE_ENV_VAR, raising=False)


def shown(error: BaseException) -> str:
    """What a traceback showing local variables could print for `error` and its chain,
    leaving out this module's frames, which hold the token by design."""
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


# The registry and docs/errors.md


def documented() -> tuple[dict[str, str], set[str]]:
    """The `### QTE-...` entries in docs/errors.md, with their text, and the codes in its
    Retired list."""
    text = DOCS.read_text(encoding="utf-8")
    body, _, retired = text.partition("\n## Retired codes")
    entries: dict[str, str] = {}
    for chunk in re.split(r"^### ", body, flags=re.MULTILINE)[1:]:
        heading, _, rest = chunk.partition("\n")
        entries[heading.strip()] = rest.split("\n## ", 1)[0]
    gone = set(re.findall(r"^- (QTE-[A-Z0-9-]+):", retired, flags=re.MULTILINE))
    return entries, gone


def test_every_code_has_an_entry_in_docs_errors_md_and_every_entry_a_code():
    entries, gone = documented()
    assert set(entries) == set(CODES)
    assert gone == set(RETIRED)
    assert not set(CODES) & set(RETIRED)


def test_every_docs_entry_says_its_cause_and_fix():
    entries, _ = documented()
    for code, text in entries.items():
        cause = re.search(r"^- Cause: (\S.*)$", text, flags=re.MULTILINE)
        fix = re.search(r"^- Fix: (\S.*)$", text, flags=re.MULTILINE)
        assert cause and fix, code


def source_files() -> list[Path]:
    return sorted((ROOT / "qte_sdk").rglob("*.py")) + sorted((ROOT / "examples").glob("*.py"))


def test_no_code_appears_in_the_source_without_a_registry_entry():
    found: dict[str, list[str]] = {}
    for path in source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for code in CODE_IN_TEXT.findall(node.value):
                    found.setdefault(code, []).append(f"{path.name}:{node.lineno}")
    unregistered = {code: where for code, where in found.items() if code not in CODES}
    assert unregistered == {}
    assert errors.UPDATE_AVAILABLE in found  # the scan does see the source


def test_every_coded_class_has_a_registered_default_code():
    for module in pkgutil.walk_packages(qte_sdk.__path__, "qte_sdk."):
        if module.name != "qte_sdk.token":  # a command, imported below without running
            importlib.import_module(module.name)
    importlib.import_module("qte_sdk.token")
    pending: list[type] = [QteError, QteWarning]
    classes = []
    while pending:
        cls = pending.pop()
        for sub in cls.__subclasses__():
            classes.append(sub)
            pending.append(sub)
    assert classes, "no coded class found"
    for cls in classes:
        if cls.__module__.startswith("qte_sdk."):
            assert cls.code in CODES, cls
    assert QteError.code is None and QteWarning.code is None


def test_every_code_has_the_agreed_shape_and_every_template_renders_one_line():
    for code in CODES:
        assert CODE_SHAPE.match(code), code
        message = render(code, **FIELD_VALUES)
        assert message.startswith(f"{code}: "), message
        assert message.endswith(".") and "\n" not in message, message
        assert ".." not in message, message
        assert message.count(". ") >= 2, message  # what, why and the next step


def test_templates_take_only_allowed_fields_and_none_names_the_token():
    assert set(FIELD_VALUES) == errors.FIELDS
    for name in errors.FIELDS:
        assert "token" not in name and "secret" not in name
    for code in CODES:
        assert errors.template_fields(code) <= errors.FIELDS, code


def test_released_codes_never_disappear():
    assert RELEASED <= set(CODES) | set(RETIRED)


def test_the_update_check_code_is_the_registered_one():
    assert update.UPDATE_AVAILABLE == errors.UPDATE_AVAILABLE
    assert update.UPDATE_AVAILABLE in CODES


def test_a_missing_field_gives_the_cause_rather_than_failing():
    assert render(errors.DOTENV_INVALID) == (
        "QTE-DOTENV-INVALID: A QTE_TOKEN or QTE_URL line in the .env cannot be parsed. "
        "See QTE-DOTENV-INVALID in docs/errors.md."
    )


def test_server_text_is_flattened_to_one_line():
    error = SessionRejected(1003, "line one\nline two\x1b[31m.", reason_name="TEAM_DISABLED")
    assert error.detail == "line one\nline two\x1b[31m."
    assert "(TEAM_DISABLED: line one line two[31m)" in str(error)
    assert "\n" not in str(error) and "\x1b" not in str(error)


# Compatibility: every class keeps its older base, and a program's own instance still works


@pytest.mark.parametrize(
    ("error", "base"),
    [
        (MissingToken("no token"), ValueError),
        (MissingURL("no address"), ValueError),
        (SessionNotAcknowledged("closed"), Exception),
        (AuthNotSent("could not send auth"), SessionNotAcknowledged),
        (ResumeNotAcknowledged("closed"), SessionNotAcknowledged),
        (ResumeRejected(1000, None), SessionRejected),
        (ContractVersionMismatch(1001, None), SessionRejected),
        (LivenessTimeout(1.0), TimeoutError),
        (SessionTimeout("not acknowledged", seconds=1.0), TimeoutError),
    ],
)
def test_coded_errors_keep_their_bases(error: BaseException, base: type):
    assert isinstance(error, base) and isinstance(error, QteError)
    assert error.code in CODES


def test_a_failed_handshake_is_still_an_invalid_handshake():
    from websockets.exceptions import InvalidHandshake

    error = HandshakeFailed("InvalidStatus", 503)
    assert isinstance(error, InvalidHandshake) and isinstance(error, QteError)
    assert str(error).startswith(
        "QTE-CONNECT-HANDSHAKE-FAILED: the opening handshake failed (InvalidStatus, HTTP 503); "
        "details withheld. The exchange is busy or down. "
    )


def test_coded_warnings_stay_user_warnings():
    for category in (DotenvNotIgnored, TokenFileShared, AddressFileShared):
        assert issubclass(category, UserWarning) and issubclass(category, QteWarning)
    # Issued the old way, by category and text, a warning still carries its code.
    with pytest.warns(TokenFileShared) as caught:
        warnings.warn("some text", TokenFileShared, stacklevel=1)
    assert str(caught[0].message) == "QTE-TOKEN-SHARED: some text"


def test_an_error_a_program_makes_shows_its_own_text_after_the_code():
    assert str(MissingToken("nothing here")) == "QTE-TOKEN-MISSING: nothing here"


def test_the_last_line_of_a_traceback_is_the_coded_summary():
    for error in (MissingToken("x", fields={"where": "token="}), LivenessTimeout(2.0)):
        last = traceback.format_exception_only(error)[-1]
        name = f"{type(error).__module__}.{type(error).__qualname__}"
        assert last.startswith(f"{name}: {error.code}: "), last


# token check: exit codes


def check(capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    status = token_command.main(["check"])
    out, err = capsys.readouterr()
    return status, out + err


def test_check_exits_0_when_all_is_found_and_safe(monkeypatch, capsys):
    monkeypatch.setenv(TOKEN_ENV_VAR, synthetic_token())
    monkeypatch.setenv(URL_ENV_VAR, URL)
    assert check(capsys)[0] == 0


def test_check_exits_1_without_a_token(monkeypatch, capsys):
    monkeypatch.setenv(URL_ENV_VAR, URL)
    status, out = check(capsys)
    assert status == 1
    assert "token:   none usable. QTE-TOKEN-MISSING: " in out


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not on the PATH")


def git(*args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "QTE_"))}
    subprocess.run(["git", *args], check=True, capture_output=True, env=env)


def private_dotenv(text: str) -> Path:
    path = Path.cwd() / ".env"
    path.write_text(text)
    path.chmod(0o600)
    return path


@needs_git
def test_check_exits_1_for_a_dotenv_git_does_not_ignore(capsys):
    git("init", "-q", ".")
    private_dotenv(f"QTE_URL={URL}\nQTE_TOKEN={synthetic_token()}\n")
    status, out = check(capsys)
    assert status == 1  # it was 0, with the warning, before codes
    assert "warning: QTE-DOTENV-NOT-IGNORED: " in out


@needs_git
def test_check_exits_1_for_a_dotenv_git_tracks(capsys):
    git("init", "-q", ".")
    private_dotenv(f"QTE_URL={URL}\nQTE_TOKEN={synthetic_token()}\n")
    git("add", "-f", ".env")
    status, out = check(capsys)
    assert status == 1
    assert "warning: QTE-DOTENV-TRACKED: " in out and "git rm --cached .env" in out


def test_check_exits_2_when_git_cannot_tell(monkeypatch, capsys):
    (Path.cwd() / ".git").mkdir()
    private_dotenv(f"QTE_URL={URL}\nQTE_TOKEN={synthetic_token()}\n")
    monkeypatch.setattr(dotenv, "_run_git", lambda *args, **kwargs: None)
    status, out = check(capsys)
    assert status == 2
    assert "warning: QTE-DOTENV-GIT-UNKNOWN: " in out


def test_check_does_not_ask_git_about_a_dotenv_the_sdk_does_not_read(monkeypatch, capsys):
    (Path.cwd() / ".git").mkdir()
    private_dotenv(f"QTE_URL={URL}\n")
    monkeypatch.setenv(TOKEN_ENV_VAR, synthetic_token())
    monkeypatch.setenv(URL_ENV_VAR, URL)
    monkeypatch.setattr(dotenv, "_run_git", lambda *args, **kwargs: None)
    assert check(capsys)[0] == 0


def test_a_must_fix_finding_outranks_one_that_could_not_be_told(
    windows,  # noqa: F811
    monkeypatch,
    capsys,
):
    windows(None)  # the token file's list cannot be read: could not tell
    path = Path.cwd() / "token"
    path.write_text(synthetic_token())
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(path))
    monkeypatch.setenv(URL_ENV_VAR, "https://example.org")  # must be fixed
    status, out = check(capsys)
    assert status == 1
    assert "QTE-TOKEN-UNCHECKED" in out and "QTE-ADDRESS-INVALID" in out


def test_check_withholds_a_token_file_path_that_holds_the_token(monkeypatch, capsys):
    token = synthetic_token()
    folder = Path.cwd() / token
    folder.mkdir()
    (folder / "token").write_text(token)
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(folder / "token"))
    monkeypatch.setenv(URL_ENV_VAR, URL)
    status, out = check(capsys)
    assert status == 0
    assert "the file named by QTE_TOKEN_FILE (path withheld: it holds the token)" in out
    assert_no_form_of(token, out)


# The token never appears: every template, then every code end to end


# Characters a mistaken paste can put in a token: each changes how the token is written
# once escaped, put in one line or flattened, and the withholding must catch every form.
SPECIAL = pytest.mark.parametrize(
    "special",
    ["\\", "\n", "\t", "\x01", "\x1b", "\x7f", " "],
    ids=["backslash", "newline", "tab", "control", "escape", "delete", "space"],
)


def token_bearing(token: str) -> list[str]:
    """Values a field could be given that hold the token in some form."""
    return [
        token,
        f"/home/student/{token}/algo/.env",
        f"bad token {token} here",
        repr(token),
        json.dumps(token),
        token.encode("unicode_escape").decode("ascii"),
        errors.one_line(token),
        errors.flatten(token),
    ]


@SPECIAL
def test_no_template_shows_a_token_given_in_any_field(special):
    token = synthetic_token() + special + synthetic_token()
    secret = _Secret(token)

    def withhold(text: str) -> bool:
        return _holds_token(text, secret)

    rendered = 0
    for code in CODES:
        for field in errors.template_fields(code):
            for value in token_bearing(token):
                fields = {**FIELD_VALUES, field: value}
                text = render(code, withhold=withhold, **fields)
                assert errors.WITHHELD in text, (code, field)
                assert_no_form_of(token, text)
                rendered += 1
    assert rendered > len(CODES)


@SPECIAL
def test_check_withholds_a_token_that_a_path_holds_in_any_form(
    windows,  # noqa: F811
    monkeypatch,
    capsys,
    tmp_path,
    special,
):
    if os.name == "nt" and special not in (" ", "\\"):
        pytest.skip("Windows allows no control character in a file name")
    if os.name == "nt":
        pytest.skip("a backslash separates folders on Windows")
    token = synthetic_token() + special + synthetic_token()
    folder = tmp_path / token
    folder.mkdir()
    monkeypatch.chdir(folder)
    (folder / ".git").mkdir()
    monkeypatch.setattr(dotenv, "is_tracked_by_git", lambda path: False)
    monkeypatch.setattr(dotenv, "is_ignored_by_git", lambda path: False)
    monkeypatch.setenv(TOKEN_ENV_VAR, token)
    private_dotenv(f"QTE_URL={URL}\n")
    windows(None)  # so check renders the .env's path in a finding of its own too
    status, out = check(capsys)
    assert status == 1
    assert "warning: QTE-DOTENV-NOT-IGNORED: " in out
    assert "warning: QTE-ADDRESS-UNCHECKED: " in out
    assert "path withheld: it holds the token" in out
    assert_no_form_of(token, out)
    assert errors.one_line(token) not in out and errors.flatten(token) not in out


Case = Callable[[str, Any], Awaitable[str] | str]


class Context:
    def __init__(self, monkeypatch, capsys, windows) -> None:  # noqa: F811
        self.monkeypatch = monkeypatch
        self.capsys = capsys
        self.windows = windows

    def cli(self, argv: list[str], **kwargs: Any) -> str:
        token_command.main(argv, **kwargs)
        out, err = self.capsys.readouterr()
        return out + err

    def warned(self, call: Callable[[], object]) -> str:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            call()
        return "\n".join(f"{w.message}" for w in caught)


def raised(call: Callable[[], object]) -> str:
    try:
        call()
    except Exception as error:
        return shown(error)
    raise AssertionError("nothing was raised")


async def raised_async(call: Callable[[], Awaitable[object]]) -> str:
    try:
        await call()
    except Exception as error:
        return shown(error)
    raise AssertionError("nothing was raised")


def token_missing(token: str, ctx: Context) -> str:
    return raised(resolve_token)


def token_file_unreadable(token: str, ctx: Context) -> str:
    ctx.monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(Path.cwd() / token))  # no such file
    return raised(resolve_token)


def token_shared(token: str, ctx: Context) -> str:
    path = private_dotenv(f"QTE_TOKEN={token}\n")
    if os.name == "posix":
        path.chmod(0o644)
        posix = raised(resolve_token)
        path.chmod(0o600)
    else:
        posix = ""
    ctx.windows(SECOND_DRIVE)
    return posix + ctx.warned(resolve_token)


def token_unchecked(token: str, ctx: Context) -> str:
    private_dotenv(f"QTE_URL={URL}\nQTE_TOKEN={token}\n")
    ctx.windows(None)
    return ctx.cli(["check"])


def token_malformed(token: str, ctx: Context) -> str:
    return ctx.cli(
        ["set"], ask=answers(URL), ask_secret=answers(f"{token} x"), interactive=lambda: True
    )


def token_no_terminal(token: str, ctx: Context) -> str:
    return ctx.cli(["set"], interactive=lambda: False)


def token_set_path(token: str, ctx: Context) -> str:
    return ctx.cli(["set", "--file", ".gitignore"], interactive=lambda: True)


def token_set_write(token: str, ctx: Context) -> str:
    def full(*args: object) -> None:
        raise OSError(28, "No space left on device")

    ctx.monkeypatch.setattr(os, "replace", full)
    return ctx.cli(
        ["set", "--file", "tokenfile"], ask_secret=answers(token), interactive=lambda: True
    )


def address_missing(token: str, ctx: Context) -> str:
    return raised(resolve_url)


def address_invalid(token: str, ctx: Context) -> str:
    ctx.monkeypatch.setenv(TOKEN_ENV_VAR, token)
    ctx.monkeypatch.setenv(URL_ENV_VAR, f"https://{token}.example.org")
    return ctx.cli(["check"])


def address_shared(token: str, ctx: Context) -> str:
    ctx.monkeypatch.setenv(TOKEN_ENV_VAR, token)
    private_dotenv(f"QTE_URL={URL}\n")
    ctx.windows(SECOND_DRIVE)
    return ctx.warned(resolve_url)


def address_unchecked(token: str, ctx: Context) -> str:
    ctx.monkeypatch.setenv(TOKEN_ENV_VAR, token)
    private_dotenv(f"QTE_URL={URL}\n")
    ctx.windows(PRIVATE_FILE, folder="O:BAD:(A;;ZZ;;;WD)")
    return ctx.warned(resolve_url)


@needs_git
def dotenv_tracked(token: str, ctx: Context) -> str:
    git("init", "-q", ".")
    private_dotenv(f"QTE_URL={URL}\nQTE_TOKEN={token}\n")
    git("add", "-f", ".env")
    return ctx.warned(resolve_token) + ctx.cli(["check"])


def dotenv_not_ignored(token: str, ctx: Context) -> str:
    (Path.cwd() / ".git").mkdir()
    private_dotenv(f"QTE_URL={URL}\nQTE_TOKEN={token}\n")
    ctx.monkeypatch.setattr(dotenv, "is_tracked_by_git", lambda path: False)
    ctx.monkeypatch.setattr(dotenv, "is_ignored_by_git", lambda path: False)
    return ctx.warned(resolve_token)


def dotenv_git_unknown(token: str, ctx: Context) -> str:
    (Path.cwd() / ".git").mkdir()
    private_dotenv(f"QTE_URL={URL}\nQTE_TOKEN={token}\n")
    ctx.monkeypatch.setattr(dotenv, "_run_git", lambda *args, **kwargs: None)
    return ctx.cli(["check"])


def dotenv_unreadable(token: str, ctx: Context) -> str:
    (Path.cwd() / ".env").mkdir()
    return raised(resolve_token)


def dotenv_invalid(token: str, ctx: Context) -> str:
    private_dotenv(f'QTE_TOKEN="{token}\n')
    return raised(resolve_token) + raised(resolve_url)


async def rejected(token: str, reason: str) -> str:
    server = Server(session_reject(reason, f"bad token {token}\nsecond line"))
    async with serve_local(server) as url:
        return await raised_async(lambda: open_session(url, token))


async def session_rejected(token: str, ctx: Context) -> str:
    return await rejected(token, "NOT_AUTHENTICATED")


async def session_version_mismatch(token: str, ctx: Context) -> str:
    return await rejected(token, "VERSION_MISMATCH")


async def session_not_acknowledged(token: str, ctx: Context) -> str:
    async with serve_local(Server(hold_open=False)) as url:
        return await raised_async(lambda: open_session(url, token))


async def session_auth_not_sent(token: str, ctx: Context) -> str:
    async with serve_local(Server(ack())) as url:
        return await raised_async(lambda: open_session(url, token + "\udc80"))


async def session_timeout(token: str, ctx: Context) -> str:
    async with serve_local(Server()) as url:
        return await raised_async(lambda: open_session(url, token, ack_timeout=0.2))


async def resumed(token: str, script: dict, timeout: float = 5.0) -> str:
    async with serve_local(Scripted(script)) as url:
        async with await open_session(url, token) as session:
            return await raised_async(lambda: session.resume(0, timeout=timeout))


async def session_resume_rejected(token: str, ctx: Context) -> str:
    return await resumed(token, {"answer": [resume_reject("no resume\nhere")]})


async def session_resume_not_acknowledged(token: str, ctx: Context) -> str:
    return await resumed(token, {"answer": [], "drop": True})


async def session_resume_timeout(token: str, ctx: Context) -> str:
    return await resumed(token, {"answer": []}, timeout=0.1)


async def connect_handshake_failed(token: str, ctx: Context) -> str:
    def refuse(connection, request):
        return connection.respond(401, f"denied {token}")

    async def nothing(ws) -> None:
        await ws.wait_closed()

    async with serve(nothing, "127.0.0.1", 0, process_request=refuse) as server:
        port = server.sockets[0].getsockname()[1]
        return await raised_async(lambda: open_session(f"ws://127.0.0.1:{port}", token))


async def connect_liveness_timeout(token: str, ctx: Context) -> str:
    async def handler(ws) -> None:
        await ws.recv()
        await ws.send(ack())
        await ws.send(heartbeat())
        await ws.wait_closed()

    async with serve_local(handler) as url:
        session = await open_session(url, token, liveness_timeout=0.1)
        async with session:

            async def read() -> None:
                async for _ in session:
                    pass

            return await raised_async(read)


def update_available(token: str, ctx: Context) -> str:
    from test_update import (
        INSTALLED,
        MAIN,
        VERSION,
        answer,
        bumped,
        git_install,
        installed,
        refs_with,
        tag_for,
    )

    ctx.monkeypatch.setenv(TOKEN_ENV_VAR, token)
    newer = tag_for(bumped(VERSION, 2))
    installed(ctx.monkeypatch, git_install())
    answer(ctx.monkeypatch, refs_with((tag_for(VERSION), INSTALLED), (newer, MAIN)))
    return update.check_for_update().message


CASES: dict[str, Case] = {
    errors.TOKEN_MISSING: token_missing,
    errors.TOKEN_FILE_UNREADABLE: token_file_unreadable,
    errors.TOKEN_SHARED: token_shared,
    errors.TOKEN_UNCHECKED: token_unchecked,
    errors.TOKEN_MALFORMED: token_malformed,
    errors.TOKEN_NO_TERMINAL: token_no_terminal,
    errors.TOKEN_SET_PATH: token_set_path,
    errors.TOKEN_SET_WRITE: token_set_write,
    errors.ADDRESS_MISSING: address_missing,
    errors.ADDRESS_INVALID: address_invalid,
    errors.ADDRESS_SHARED: address_shared,
    errors.ADDRESS_UNCHECKED: address_unchecked,
    errors.DOTENV_TRACKED: dotenv_tracked,
    errors.DOTENV_NOT_IGNORED: dotenv_not_ignored,
    errors.DOTENV_GIT_UNKNOWN: dotenv_git_unknown,
    errors.DOTENV_UNREADABLE: dotenv_unreadable,
    errors.DOTENV_INVALID: dotenv_invalid,
    errors.SESSION_REJECTED: session_rejected,
    errors.SESSION_VERSION_MISMATCH: session_version_mismatch,
    errors.SESSION_NOT_ACKNOWLEDGED: session_not_acknowledged,
    errors.SESSION_AUTH_NOT_SENT: session_auth_not_sent,
    errors.SESSION_TIMEOUT: session_timeout,
    errors.SESSION_RESUME_REJECTED: session_resume_rejected,
    errors.SESSION_RESUME_NOT_ACKNOWLEDGED: session_resume_not_acknowledged,
    errors.SESSION_RESUME_TIMEOUT: session_resume_timeout,
    errors.CONNECT_HANDSHAKE_FAILED: connect_handshake_failed,
    errors.CONNECT_LIVENESS_TIMEOUT: connect_liveness_timeout,
    errors.UPDATE_AVAILABLE: update_available,
}


def test_every_code_has_an_end_to_end_case():
    assert set(CASES) == set(CODES)


@pytest.mark.parametrize("code", list(CASES))
async def test_each_code_end_to_end_shows_its_code_and_never_the_token(
    code,
    monkeypatch,
    capsys,
    windows,  # noqa: F811
):
    case = CASES[code]
    if getattr(case, "pytestmark", None) and shutil.which("git") is None:
        pytest.skip("git is not on the PATH")
    token = synthetic_token()
    result = case(token, Context(monkeypatch, capsys, windows))
    text = await result if asyncio.iscoroutine(result) else result
    assert f"{code}: " in text, text
    assert_no_form_of(token, text)


# Found in review


def test_check_says_it_could_not_tell_who_may_change_an_address_only_dotenv(
    windows,  # noqa: F811
    monkeypatch,
    capsys,
):
    windows(None)
    monkeypatch.setenv(TOKEN_ENV_VAR, synthetic_token())
    private_dotenv(f"QTE_URL={URL}\n")
    status, out = check(capsys)
    assert status == 2
    assert "warning: QTE-ADDRESS-UNCHECKED: " in out


def test_check_asks_git_about_the_file_a_dotenv_links_to(monkeypatch, capsys, tmp_path):
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    target = repository / "settings.env"
    target.write_text(f"QTE_URL={URL}\nQTE_TOKEN={synthetic_token()}\n")
    target.chmod(0o600)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    (outside / ".env").symlink_to(target)
    monkeypatch.setattr(dotenv, "_run_git", lambda *args, **kwargs: None)
    status, out = check(capsys)
    assert status == 2
    assert "QTE-DOTENV-GIT-UNKNOWN" in out


def test_check_withholds_the_token_from_a_warning_that_names_a_token_file(
    windows,  # noqa: F811
    monkeypatch,
    capsys,
):
    windows(SECOND_DRIVE)
    token = synthetic_token()
    folder = Path.cwd() / token
    folder.mkdir()
    (folder / "token").write_text(token)
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(folder / "token"))
    monkeypatch.setenv(URL_ENV_VAR, URL)
    status, out = check(capsys)
    assert status == 1
    assert "warning: QTE-TOKEN-SHARED: " in out
    assert_no_form_of(token, out)


def test_a_session_timeout_survives_pickling():
    import pickle

    error = pickle.loads(pickle.dumps(SessionTimeout("not acknowledged", seconds=2.0)))
    assert isinstance(error, TimeoutError) and error.seconds == 2.0
    assert str(error).startswith(
        "QTE-SESSION-TIMEOUT: the session was not acknowledged within 2.0 s."
    )


def test_a_warning_takes_any_arguments_as_before():
    assert TokenFileShared("a", "b").args == ("a", "b")
    assert DotenvNotIgnored().args == ()


def test_a_field_with_a_line_break_still_gives_one_line():
    message = render(errors.DOTENV_NOT_IGNORED, path="/home/a\nb/.env", name=".env")
    assert "\n" not in message and "/home/a b/.env" in message


def test_check_gives_the_same_answer_however_often_it_runs(monkeypatch, capsys):
    (Path.cwd() / ".git").mkdir()
    monkeypatch.setattr(dotenv, "is_tracked_by_git", lambda path: False)
    monkeypatch.setattr(dotenv, "is_ignored_by_git", lambda path: False)
    private_dotenv(f"QTE_URL={URL}\nQTE_TOKEN={synthetic_token()}\n")
    with warnings.catch_warnings(record=True) as before:
        warnings.simplefilter("always")
        resolve_url()  # the SDK warns once per process for each .env
    assert [w.category for w in before] == [DotenvNotIgnored]
    assert check(capsys)[0] == 1
    assert check(capsys)[0] == 1
    # check leaves the record as it found it: no second warning in this process.
    with warnings.catch_warnings(record=True) as after:
        warnings.simplefilter("always")
        resolve_url()
    assert after == []


def test_check_records_nothing_so_a_session_still_warns_once(monkeypatch, capsys):
    (Path.cwd() / ".git").mkdir()
    monkeypatch.setattr(dotenv, "is_tracked_by_git", lambda path: False)
    monkeypatch.setattr(dotenv, "is_ignored_by_git", lambda path: False)
    private_dotenv(f"QTE_URL={URL}\nQTE_TOKEN={synthetic_token()}\n")
    assert check(capsys)[0] == 1
    assert dotenv._git_checked == set() and dotenv._shared_warned == set()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        resolve_url()
    assert [w.category for w in caught] == [DotenvNotIgnored]


def test_a_token_shrunk_by_flattening_does_not_count_in_its_short_form():
    secret = _Secret("\x01e\x02")  # malformed: flattened, it would be the letter e
    assert not _holds_token("/home/student/algo/.env", secret)
    assert _holds_token("a \x01e\x02 b", secret)


def test_check_interrupted_while_printing_keeps_the_token_out_of_its_locals(monkeypatch):
    if os.name == "nt":
        pytest.skip("Windows allows no control character in a file name")
    token = synthetic_token() + "\t" + synthetic_token()
    folder = Path.cwd() / token
    folder.mkdir()
    monkeypatch.chdir(folder)
    (folder / ".git").mkdir()
    monkeypatch.setattr(dotenv, "is_tracked_by_git", lambda path: False)
    monkeypatch.setattr(dotenv, "is_ignored_by_git", lambda path: False)
    monkeypatch.setenv(TOKEN_ENV_VAR, token)
    private_dotenv(f"QTE_URL={URL}\n")

    def closed(*args: object, **kwargs: object) -> None:
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr("builtins.print", closed)
    with pytest.raises(BrokenPipeError) as caught:
        token_command.main(["check"])
    assert_no_form_of(token, shown(caught.value))
