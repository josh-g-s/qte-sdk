"""The SDK's commands and examples print no colour codes, which coding agents would read as
text. Python 3.14's argparse colours usage lines, errors and help when the output is a
terminal, or when FORCE_COLOR is set, so every parser turns that off."""

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

COMMANDS = [
    ["-m", "qte_sdk.update"],
    ["-m", "qte_sdk.token"],
    ["-m", "qte_sdk.token", "set"],
    ["-m", "qte_sdk.token", "check"],
    ["-m", "qte_sdk.agents"],
    *[
        [str(path)]
        for path in sorted((ROOT / "examples").glob("*.py"))
        if "argparse" in path.read_text()
    ],
]


def run(args: list[str], tmp_path: Path) -> bytes:
    env = {
        key: value for key, value in os.environ.items() if key not in ("NO_COLOR", "PYTHON_COLORS")
    }
    env["FORCE_COLOR"] = "1"
    result = subprocess.run(
        [sys.executable, *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=60,
    )
    return result.stdout + result.stderr


@pytest.mark.parametrize(
    "command", COMMANDS, ids=lambda command: " ".join(Path(part).name for part in command)
)
def test_help_and_usage_errors_print_no_colour(command: list[str], tmp_path: Path):
    help_output = run([*command, "-h"], tmp_path)
    assert b"usage:" in help_output
    assert b"\x1b" not in help_output
    error_output = run([*command, "--no-such-option"], tmp_path)
    assert b"usage:" in error_output
    assert b"\x1b" not in error_output


def test_every_parser_turns_colour_off():
    # A new parser added without `color = False` would colour its output on Python 3.14.
    for path in [
        *sorted((ROOT / "qte_sdk").rglob("*.py")),
        *sorted((ROOT / "examples").glob("*.py")),
    ]:
        text = path.read_text()
        parsers = (
            text.count("argparse.ArgumentParser(")
            + text.count(".add_parser(")
            + text.count("= Parser(")  # the smoke test's subclass
        )
        assert text.count(".color = False") == parsers, path


# The JSON output (#180) and the log lines: no colour, no carriage return to redraw a line
# (a spinner or progress bar), on a pipe with FORCE_COLOR set and, on POSIX, on a terminal.

NO_NETWORK = ROOT / "tests" / "no_network"
SMOKE_TEST = str(ROOT / "examples" / "smoke_test.py")
JSON_RUNS = {
    "token check --json": (["-m", "qte_sdk.token", "check", "--json"], {}),
    "token check --json, found": (
        ["-m", "qte_sdk.token", "check", "--json"],
        {"QTE_TOKEN": "x" * 43, "QTE_URL": "wss://exchange.example/ws"},
    ),
    "update --json": (["-m", "qte_sdk.update", "--json", "--timeout", "1"], {}),
    "smoke_test --json, no token": ([SMOKE_TEST, "--json"], {}),
    "smoke_test --json, no exchange": (
        [SMOKE_TEST, "--json", "--seconds", "1"],
        {"QTE_TOKEN": "x" * 43, "QTE_URL": "ws://127.0.0.1:1/ws"},
    ),
    "smoke_test, JSON log lines": (
        [SMOKE_TEST, "--seconds", "1"],
        {"QTE_TOKEN": "x" * 43, "QTE_URL": "ws://127.0.0.1:1/ws", "QTE_LOG_FORMAT": "json"},
    ),
}


def json_env(extra: dict[str, str]) -> dict[str, str]:
    """No QTE_ variable but `extra`, colour forced, and no request leaving this machine."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("NO_COLOR", "PYTHON_COLORS") and not key.startswith("QTE_")
    }
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(NO_NETWORK), env.get("PYTHONPATH")]))
    return {**env, "FORCE_COLOR": "1", **extra}


def assert_plain(output: bytes) -> None:
    assert b"\x1b" not in output, output
    # A carriage return only as part of a line ending (a terminal's, or Windows').
    assert b"\r" not in output.replace(b"\r\n", b"\n"), output


@pytest.mark.parametrize("name", list(JSON_RUNS))
def test_json_output_on_a_pipe_has_no_colour_or_carriage_return(name: str, tmp_path: Path):
    args, extra = JSON_RUNS[name]
    result = subprocess.run(
        [sys.executable, *args], cwd=tmp_path, env=json_env(extra), capture_output=True, timeout=60
    )
    assert_plain(result.stdout + result.stderr)
    if "--json" in args:
        assert json.loads(result.stdout)["exit_code"] == result.returncode


@pytest.mark.skipif(sys.platform == "win32", reason="a terminal needs pty, which is POSIX only")
@pytest.mark.parametrize("name", list(JSON_RUNS))
def test_json_output_on_a_terminal_has_no_colour_or_carriage_return(name: str, tmp_path: Path):
    import pty

    args, extra = JSON_RUNS[name]
    controller, terminal = pty.openpty()
    try:
        process = subprocess.Popen(
            [sys.executable, *args],
            cwd=tmp_path,
            env=json_env(extra),
            stdin=subprocess.DEVNULL,
            stdout=terminal,
            stderr=terminal,
        )
        os.close(terminal)
        terminal = -1
        output = b""
        deadline = time.monotonic() + 60
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([controller], [], [], left)[0]:
                process.kill()
                process.wait()
                pytest.fail(f"{name} was still running after 60 s: {output!r}")
            try:
                chunk = os.read(controller, 65536)
            except OSError:  # Linux: the terminal's other end is closed
                break
            if not chunk:
                break
            output += chunk
        assert process.wait(timeout=60) is not None
    finally:
        if terminal != -1:
            os.close(terminal)
        os.close(controller)
    assert output, name
    assert_plain(output)
