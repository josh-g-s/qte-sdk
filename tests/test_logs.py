"""JSON logs (`qte_sdk.logs`), and the rule that no JSON the SDK prints holds the token.

The log lines: nothing happens at import; `QTE_LOG_FORMAT` is read only by `configure()`;
one handler, on the `qte_sdk` logger, with nothing else changed; the keys and their order;
the message and next step split so the plain line is `<code>: <message> <next_step>.`;
ASCII lines that read the same through a cp1252 pipe; and SDK warnings as log lines when
captured.

The token: a synthetic token is fed through every input the three `--json` commands read
(QTE_TOKEN, a QTE_TOKEN_FILE path, a folder named with it whose .env alone holds it, the
exchange and history addresses, the smoke test's arguments, the exchange's reject detail)
and through a log record's fields, and no form of it may appear in stdout or stderr."""

import io
import json
import logging
import os
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any

import pytest
from fake_exchange import serve_local
from test_examples import (
    CALENDAR,
    INSTRUMENT,
    SERVER_TIME,
    SMOKE_TEST,
    FakeExchange,
    example_env,
    run_example,
)
from test_pasted_token import assert_no_form_of, synthetic_token

from qte_sdk import errors, logs, update
from qte_sdk import token as token_command
from qte_sdk.errors import CODES
from qte_sdk.session import TOKEN_ENV_VAR, TOKEN_FILE_ENV_VAR, URL_ENV_VAR

KEYS = ["time", "level", "logger", "code", "message", "next_step", "fields"]


@pytest.fixture(autouse=True)
def plain_logs(monkeypatch: pytest.MonkeyPatch) -> Any:
    """No QTE_ variable from the developer's setup, and the SDK's logging as it was after
    each test: text, with no capture."""
    for name in (logs.LOG_FORMAT_ENV_VAR, TOKEN_ENV_VAR, TOKEN_FILE_ENV_VAR, "QTE_HISTORY_URL"):
        monkeypatch.delenv(name, raising=False)
    yield
    logs.configure("text")


def json_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger("qte_sdk").handlers if isinstance(h, logs._JsonHandler)]


def log_line(logger: str = "qte_sdk.update", message: str = "plain", **extra: Any) -> str:
    """One record through a JSON handler on a string, and the line it writes."""
    stream = io.StringIO()
    logs.configure("json", stream=stream)
    logging.getLogger(logger).warning("%s", message, extra=extra)
    return stream.getvalue()


# Configuring


def test_nothing_happens_at_import_even_with_the_variable_set():
    code = (
        "import importlib, logging, pkgutil, warnings\n"
        "shown = warnings.showwarning\n"
        "import qte_sdk\n"
        "for module in pkgutil.walk_packages(qte_sdk.__path__, 'qte_sdk.'):\n"
        "    importlib.import_module(module.name)\n"
        "assert 'qte_sdk.logs' in __import__('sys').modules\n"
        "print(len(logging.getLogger('qte_sdk').handlers), warnings.showwarning is shown)\n"
    )
    env = {**os.environ, logs.LOG_FORMAT_ENV_VAR: "json"}
    done = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["0", "True"]


@pytest.mark.parametrize(
    ("value", "json_lines"),
    [(None, False), ("", False), ("text", False), ("json", True), (" JSON\n", True)],
)
def test_the_variable_is_read_when_configure_is_called(
    monkeypatch: pytest.MonkeyPatch, value: str | None, json_lines: bool
):
    if value is not None:
        monkeypatch.setenv(logs.LOG_FORMAT_ENV_VAR, value)
    handler = logs.configure()
    assert (handler is not None) is json_lines
    assert len(json_handlers()) == int(json_lines)


def test_configure_is_idempotent_and_text_undoes_json():
    first = logs.configure("json")
    second = logs.configure("json")
    assert json_handlers() == [second] and first is not second
    assert logs.configure("text") is None
    assert json_handlers() == []


@pytest.mark.parametrize("value", ["xml", "jsonl", "  ", "1"])
def test_a_bad_value_raises_its_code_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, value: str
):
    handler = logs.configure("json")
    monkeypatch.setenv(logs.LOG_FORMAT_ENV_VAR, value)
    for call in (logs.configure, lambda: logs.configure(value)):
        if value == "  " and call is logs.configure:
            continue  # spaces alone in the variable mean unset: text
        with pytest.raises(logs.LogFormatInvalid) as raised:
            call()
        assert isinstance(raised.value, ValueError)
        assert raised.value.code == errors.LOG_FORMAT_INVALID
        assert str(raised.value).startswith("QTE-LOG-FORMAT-INVALID: the log format given in ")
        assert json_handlers() == [handler]


def test_configure_leaves_the_root_logger_levels_propagation_and_filters_alone():
    root = logging.getLogger()
    sdk = logging.getLogger("qte_sdk")
    before = (list(root.handlers), root.level, sdk.level, sdk.propagate, list(sdk.filters))
    filters = list(warnings.filters)
    logs.configure("json", capture_warnings=True)
    assert (list(root.handlers), root.level, sdk.level, sdk.propagate, list(sdk.filters)) == before
    assert warnings.filters == filters


def test_records_still_reach_the_root_logger(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.WARNING)
    line = log_line(code=errors.UPDATE_AVAILABLE)
    assert json.loads(line)["code"] == errors.UPDATE_AVAILABLE
    assert [r.code for r in caplog.records if r.name == "qte_sdk.update"] == [
        errors.UPDATE_AVAILABLE
    ]


def test_the_handler_writes_to_stderr_as_it_is_at_each_write(capsys: pytest.CaptureFixture[str]):
    logs.configure("json")
    logging.getLogger("qte_sdk.update").warning("first")
    assert json.loads(capsys.readouterr().err)["message"] == "first"
    replaced = io.StringIO()
    real = sys.stderr
    sys.stderr = replaced
    try:
        logging.getLogger("qte_sdk.update").warning("second")
    finally:
        sys.stderr = real
    assert json.loads(replaced.getvalue())["message"] == "second"


def test_the_handler_is_written_without_waiting_on_a_pipe(pipe: Any):
    # A plain stream handler on Python's own kind of stderr: the update check writes to it
    # in one os.write, never waiting (see qte_sdk.update).
    read, stream = pipe
    handler = logs.configure("json")
    real = sys.stderr
    sys.stderr = stream
    try:
        assert update._route(handler) == ("direct", stream.fileno())
    finally:
        sys.stderr = real


@pytest.fixture
def pipe() -> Any:
    read, write = os.pipe()
    stream = open(write, "w", encoding="utf-8")  # noqa: SIM115
    try:
        yield read, stream
    finally:
        stream.close()
        os.close(read)


# The line


def test_a_line_has_every_key_in_order_with_its_type():
    line = log_line(
        message="QTE-UPDATE-AVAILABLE: qte-sdk 1.0.0 is behind 1.0.1. Update with pip x.",
        code=errors.UPDATE_AVAILABLE,
        next_step="Update with pip x",
        fields={},
    )
    assert line.endswith("\n") and line.count("\n") == 1
    parsed = json.loads(line)
    assert list(parsed) == KEYS
    assert parsed["time"].endswith("Z") and len(parsed["time"]) == len("2026-10-08T12:34:56.789Z")
    assert parsed["level"] == "WARNING"
    assert parsed["logger"] == "qte_sdk.update"
    assert parsed["message"] == "qte-sdk 1.0.0 is behind 1.0.1."
    assert parsed["next_step"] == "Update with pip x"
    assert parsed["fields"] == {}


@pytest.mark.parametrize("code", sorted(CODES))
def test_the_plain_line_is_the_code_the_message_and_the_next_step(code: str):
    from test_errors import FIELD_VALUES

    text = errors.render(code, **FIELD_VALUES)
    parsed = json.loads(log_line(message=text, **logs.coded(code, **FIELD_VALUES)))
    assert parsed["code"] == code
    assert f"{code}: {parsed['message']} {parsed['next_step']}." == text


def test_a_record_with_no_code_gives_the_first_registered_code_in_its_text_or_none():
    parsed = json.loads(log_line(message="warning: QTE-NOT-A-CODE then QTE-TOKEN-MISSING: x"))
    assert parsed["code"] == errors.TOKEN_MISSING
    assert parsed["next_step"] == CODES[errors.TOKEN_MISSING].fix
    plain = json.loads(log_line(message="nothing coded here"))
    assert (plain["code"], plain["message"], plain["next_step"]) == (
        None,
        "nothing coded here",
        None,
    )


def test_an_exception_is_given_by_its_type_alone():
    stream = io.StringIO()
    logs.configure("json", stream=stream)
    try:
        raise ValueError("secret detail")
    except ValueError:
        logging.getLogger("qte_sdk.history").exception("failed")
    parsed = json.loads(stream.getvalue())
    assert parsed["exception"] == "ValueError"
    assert "secret detail" not in stream.getvalue()


def test_a_field_with_a_line_break_or_other_script_stays_one_ascii_line():
    path = "/home/élève/\U0001f600\nalgo/ .env"
    line = log_line(message=f"in {path}", code=errors.DOTENV_NOT_IGNORED, fields={"path": path})
    assert line.isascii() and line.count("\n") == 1
    assert json.loads(line)["fields"]["path"] == path


def test_a_field_json_cannot_write_is_written_as_text_or_dropped():
    parsed = json.loads(log_line(fields={"path": Path("/x/.env"), 3: "three"}))
    assert parsed["fields"] == {"path": str(Path("/x/.env")), "3": "three"}
    cycle: list[Any] = []
    cycle.append(cycle)
    assert json.loads(log_line(fields={"cycle": cycle}))["fields"] == {}


def test_lines_read_the_same_through_a_cp1252_pipe():
    code = (
        "import logging, qte_sdk.logs\n"
        "qte_sdk.logs.configure('json')\n"
        "logging.getLogger('qte_sdk.dotenv').warning('%s', '/home/\\u00e9\\U0001f600/.env')\n"
    )
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stderr.isascii()
    assert json.loads(done.stderr)["message"] == "/home/é\U0001f600/.env"


# SDK warnings


CAPTURE = """
import warnings, qte_sdk.logs
from qte_sdk import errors
shown = warnings.showwarning
qte_sdk.logs.configure("json", capture_warnings={capture})
warnings.simplefilter("always")
warnings.warn(
    errors.QteWarning(code=errors.DOTENV_NOT_IGNORED, fields={{"path": "/a/.env", "name": ".env"}})
)
warnings.warn("an ordinary warning")
qte_sdk.logs.configure("text")
assert warnings.showwarning is shown
"""


def run_warnings(capture: bool) -> list[str]:
    done = subprocess.run(
        [sys.executable, "-c", CAPTURE.format(capture=capture)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return done.stderr.splitlines()


def test_captured_sdk_warnings_are_json_lines_and_others_are_shown_as_before():
    first, *rest = run_warnings(True)
    parsed = json.loads(first)
    assert parsed["logger"] == logs.WARNINGS_LOGGER
    assert parsed["code"] == errors.DOTENV_NOT_IGNORED
    assert parsed["message"] == (
        "/a/.env is inside a git working tree and git does not ignore it. It could be "
        "committed with your token."
    )
    assert parsed["next_step"] == "Add .env to .gitignore"
    assert parsed["fields"] == {"category": "QteWarning"}
    assert "UserWarning: an ordinary warning" in rest[0]


def test_warnings_are_not_captured_unless_asked():
    lines = run_warnings(False)
    assert any("QteWarning: QTE-DOTENV-NOT-IGNORED: " in line for line in lines)
    assert not any(line.startswith("{") for line in lines)


def test_a_command_leaves_the_logging_setup_as_it_found_it(capsys: pytest.CaptureFixture[str]):
    shown = warnings.showwarning
    token_command.main(["check", "--json"])
    assert json_handlers() == [] and warnings.showwarning is shown
    mine = logs.configure("json")
    token_command.main(["check", "--json"])
    assert json_handlers() == [mine]


# The token


def test_coded_withholds_a_field_holding_the_token_and_would_not_without():
    token = synthetic_token()
    path = f"/home/ana/{token}/.env"

    def withhold(text: str) -> bool:
        return token in text

    extra = logs.coded(errors.DOTENV_NOT_IGNORED, withhold=withhold, path=path, name=".env")
    line = log_line(message="a .env that is not ignored", **extra)
    assert json.loads(line)["fields"]["path"] == errors.WITHHELD
    assert_no_form_of(token, line)
    # The control: given no way to know the token, the same record shows it.
    bare = log_line(**logs.coded(errors.DOTENV_TRACKED, path=path, name=".env", command="x"))
    with pytest.raises(AssertionError):
        assert_no_form_of(token, bare)


def run_command(*args: str, cwd: Path | None = None, **env: str) -> tuple[int, str, str]:
    """`python -m <args>` in a new process, as an agent runs it, with no network."""
    done = subprocess.run(
        [sys.executable, "-m", *args],
        cwd=cwd,
        env={**example_env(), **env},
        capture_output=True,
        text=True,
        timeout=60,
    )
    return done.returncode, done.stdout, done.stderr


def assert_one_document(out: str, code: int) -> dict[str, Any]:
    assert out.count("\n") == 1, out
    document = json.loads(out)
    assert document["exit_code"] == code
    return document


@pytest.fixture
def token_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    """A token, and a project folder named with it, whose .env alone holds it."""
    token = synthetic_token()
    folder = tmp_path / token
    folder.mkdir()
    dotenv = folder / ".env"
    dotenv.write_text(f"QTE_TOKEN={token}\n")
    dotenv.chmod(0o600)
    monkeypatch.chdir(folder)
    return token, folder


def test_no_command_shows_a_token_held_only_in_the_dotenv_of_a_folder_named_with_it(
    token_folder: tuple[str, Path],
):
    token, folder = token_folder
    url = f"wss://exchange.example/{token}"
    for args in (("qte_sdk.token", "check", "--json"), ("qte_sdk.update", "--json")):
        code, out, err = run_command(*args, cwd=folder, QTE_URL=url, QTE_LOG_FORMAT="json")
        assert_one_document(out, code)
        assert_no_form_of(token, out + err)
    (folder / "token").write_text(token)
    code, out, err = run_command(
        "qte_sdk.token", "check", "--json", cwd=folder, QTE_TOKEN_FILE=str(folder / "token")
    )
    document = assert_one_document(out, code)
    assert document["token"]["path"] == errors.WITHHELD
    assert_no_form_of(token, out + err)


def test_token_check_withholds_a_token_file_path_and_an_address_holding_the_token(
    tmp_path: Path,
):
    token = synthetic_token()
    folder = tmp_path / token
    folder.mkdir()
    path = folder / "token"
    path.write_text(token)
    path.chmod(0o600)
    for url in (f"wss://x/{token}", f"https://x/?t={token}"):
        code, out, err = run_command(
            "qte_sdk.token", "check", "--json", QTE_TOKEN_FILE=str(path), QTE_URL=url
        )
        document = assert_one_document(out, code)
        assert document["token"]["path"] == errors.WITHHELD
        assert_no_form_of(token, out + err)


def test_token_check_s_json_would_show_the_token_without_redaction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    # The control for the tests above: with the redaction and the withholding turned off,
    # a token file named with the token shows it, so the tests can tell.
    token = synthetic_token()
    path = tmp_path / token
    path.write_text(token)
    path.chmod(0o600)
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(path))
    monkeypatch.setenv(URL_ENV_VAR, "wss://x/ws")
    token_command.main(["check", "--json"])
    assert_no_form_of(token, capsys.readouterr().out)
    monkeypatch.setattr(token_command, "_redact", lambda text, secret: text)
    monkeypatch.setattr(token_command, "_holds_token", lambda value, secret: False)
    token_command.main(["check", "--json"])
    with pytest.raises(AssertionError):
        assert_no_form_of(token, capsys.readouterr().out)


async def test_the_smoke_test_s_json_never_shows_the_token_from_any_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    token = synthetic_token()
    # A session refused with the token as its detail, the token in the address's query and
    # in the history address, and as an instrument and a strategy.
    refusal = {"reason_code": "NOT_AUTHENTICATED", "reason_detail": token}
    async with serve_local(FakeExchange(session_reject=refusal)) as url:
        code, out, err = await run_example(
            SMOKE_TEST,
            f"{url}/ws?token={token}",
            token,
            "--json",
            "--seconds",
            "1",
            "--instruments",
            token,
            "--strat-id",
            token,
            QTE_HISTORY_URL=f"http://127.0.0.1:1/{token}",
        )
    assert_one_document(out, code)
    assert code == 1, out + err
    assert_no_form_of(token, out + err)


async def test_the_smoke_test_s_json_never_shows_a_token_held_only_in_dotenv(
    token_folder: tuple[str, Path],
):
    token, folder = token_folder
    exchange = FakeExchange(calendar=CALENDAR, server_time=SERVER_TIME)
    async with serve_local(exchange) as url:
        (folder / ".env").write_text(f"QTE_TOKEN={token}\nQTE_URL={url}/ws\n")
        code, out, err = await run_example(
            SMOKE_TEST, None, None, "--json", "--seconds", "1", "--instruments", INSTRUMENT
        )
    document = assert_one_document(out, code)
    assert code == 0, out + err
    assert {check["name"]: check["status"] for check in document["checks"]}["connect"] == "pass"
    assert_no_form_of(token, out + err)


def test_the_smoke_test_withholds_any_text_holding_the_token_from_its_document(
    monkeypatch: pytest.MonkeyPatch, token_folder: tuple[str, Path]
):
    # A text the document would hold, such as a check's reason, that names the token (here,
    # read from .env alone) is withheld whole, as is one shaped like a minted token.
    token, _ = token_folder
    smoke = load_smoke_test()
    report = smoke.Report(json_output=True)
    report.add(smoke.FAIL, "connect", f"refused: {token}")
    report.add(smoke.SKIP, "history", "a" * 32)
    report.add(smoke.PASS, "token", "found in ./.env (not shown)")
    document = json.loads(report.document(1))
    assert [c["message"] for c in document["checks"]] == [
        errors.WITHHELD,
        errors.WITHHELD,
        "found in ./.env (not shown)",
    ]


def load_smoke_test() -> Any:
    from test_examples import load_example

    return load_example(SMOKE_TEST)
