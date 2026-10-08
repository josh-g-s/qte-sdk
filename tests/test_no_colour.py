"""The SDK's commands and examples print no colour codes, which coding agents would read as
text. Python 3.14's argparse colours usage lines, errors and help when the output is a
terminal, or when FORCE_COLOR is set, so every parser turns that off."""

import os
import subprocess
import sys
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
