"""The checks CI's windows-install job runs, `scripts/check_student_install.py`, against
canned output: each passes what the SDK prints and fails what it must not."""

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_student_install.py"


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("check_student_install", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = load_script()
Failed = check.Failed
AGENTS = (ROOT / "qte_sdk" / "AGENTS.md").read_text(encoding="utf-8")
TOKEN_MISSING = (
    "token:   none usable. QTE-TOKEN-MISSING: no token was found in token=, QTE_TOKEN, "
    "QTE_TOKEN_FILE or ./.env.\n"
    "address: none. QTE-ADDRESS-MISSING: no exchange address was found.\n"
)
BEHIND = "QTE-UPDATE-AVAILABLE: qte-sdk 1.0.0 is behind 1.3.0. Update with pip install x"


def update_document(code: int) -> str:
    return json.dumps({"command": "update", "exit_code": code, "status": "unknown"}) + "\n"


def test_the_python_of_a_virtual_environment_is_where_this_system_puts_it(tmp_path):
    expected = ("Scripts", "python.exe") if sys.platform == "win32" else ("bin", "python")
    assert check.venv_python(tmp_path) == tmp_path.joinpath(*expected)


def test_the_children_get_no_qte_variables_and_nothing_that_changes_their_encoding(
    monkeypatch,
):
    for name in ("QTE_TOKEN", "QTE_URL", "PYTHONPATH", "PYTHONUTF8", "PYTHONIOENCODING"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("KEEP_ME", "yes")
    env = check.child_env()
    assert env["KEEP_ME"] == "yes"
    assert not {"QTE_TOKEN", "QTE_URL", "PYTHONPATH", "PYTHONUTF8", "PYTHONIOENCODING"} & set(env)


@pytest.mark.parametrize("code", [0, 1, 2])
def test_update_json_passes_each_documented_status(code):
    check.check_update_json(code, update_document(code), "")


@pytest.mark.parametrize(
    "code, out",
    [
        (3, update_document(3)),
        (1, update_document(2)),
        (2, "not json\n"),
        (2, update_document(2) + update_document(2)),
        (2, ""),
        (2, "[2]\n"),
    ],
    ids=["status 3", "status not the document's", "not JSON", "two documents", "none", "list"],
)
def test_update_json_fails_what_is_not_one_document_with_its_status(code, out):
    with pytest.raises(Failed):
        check.check_update_json(code, out, "")


def test_agents_passes_the_packaged_doc():
    check.check_agents(0, AGENTS, "")


@pytest.mark.parametrize(
    "code, out", [(1, AGENTS), (0, AGENTS[:-1]), (0, "")], ids=["exit 1", "cut", "empty"]
)
def test_agents_fails_another_status_or_text(code, out):
    with pytest.raises(Failed):
        check.check_agents(code, out, "")


def test_token_check_passes_a_missing_token(tmp_path):
    check.check_token(1, TOKEN_MISSING, "", home=tmp_path / "Student Ü")


@pytest.mark.parametrize(
    "code, out",
    [
        (0, TOKEN_MISSING),
        (2, TOKEN_MISSING),
        (1, TOKEN_MISSING.replace("QTE-TOKEN-MISSING", "QTE-OTHER")),
        (1, TOKEN_MISSING.splitlines()[1]),
        (1, "{HOME}\n" + TOKEN_MISSING),
    ],
    ids=["exit 0", "exit 2", "another code", "no token line", "the folder printed"],
)
def test_token_check_fails_anything_else(tmp_path, code, out):
    home = tmp_path / "Student Ü"
    with pytest.raises(Failed):
        check.check_token(code, out.replace("{HOME}", str(home)), "", home=home)


def write_help(folder: Path, prefix: str, text: bytes = b"usage: example.py [-h]\n") -> None:
    for example in sorted((ROOT / "examples").glob("*.py")):
        (folder / f"{prefix}{example.stem}.txt").write_bytes(text)


def test_help_passes_a_usage_line_for_every_example(tmp_path):
    write_help(tmp_path, "help-cmd-", b"usage: example.py [-h]\r\n\r\noptions:\r\n")
    check.check_help("help-cmd-", tmp_path)


def test_help_fails_when_an_example_left_no_file(tmp_path):
    write_help(tmp_path, "help-bash-")
    next(tmp_path.glob("help-bash-*.txt")).unlink()
    with pytest.raises(Failed, match="left no"):
        check.check_help("help-bash-", tmp_path)


def test_help_fails_when_no_loop_ran(tmp_path):
    with pytest.raises(Failed, match="left no"):
        check.check_help("help-bash-", tmp_path)


def test_help_fails_output_that_is_not_usage(tmp_path):
    write_help(tmp_path, "help-bash-", b"Traceback (most recent call last):\n")
    with pytest.raises(Failed, match="usage"):
        check.check_help("help-bash-", tmp_path)


@pytest.mark.parametrize(
    "code, result",
    [(0, "qte-sdk 1.3.0 is the latest release."), (1, BEHIND), (2, "cannot tell whether ...")],
)
def test_update_text_passes_two_lines_and_each_documented_status(tmp_path, code, result):
    path = tmp_path / "update.txt"
    path.write_bytes(f"installed: qte-sdk 1.3.0\r\n{result}\r\n".encode())
    check.check_update_text(path, code)


@pytest.mark.parametrize(
    "code, text",
    [
        (3, "installed: qte-sdk 1.3.0\ncannot tell\n"),
        (1, "installed: qte-sdk 1.3.0\ncannot tell\n"),
        (2, f"installed: qte-sdk 1.3.0\n{BEHIND}\n"),
        (1, f"installed: qte-sdk 1.3.0\nQTE-UPDATE-AVAILABLE: qte-sdk 1.0.0 is behind\n{BEHIND}\n"),
        (2, "cannot tell\n"),
        (2, ""),
    ],
    ids=["status 3", "1 without the code", "the code with 2", "result on two lines", "one", "none"],
)
def test_update_text_fails_anything_else(tmp_path, code, text):
    path = tmp_path / "update.txt"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(Failed):
        check.check_update_text(path, code)


def test_a_qte_sdk_imported_from_outside_the_environment_fails(tmp_path):
    # This interpreter's qte_sdk is not under tmp_path, as one from the checkout or another
    # install would not be under the student's environment.
    with pytest.raises(Failed, match="not the one installed"):
        check.check_import(Path(sys.executable), tmp_path, tmp_path)


def test_the_command_line_is_checked(capsys):
    assert check.main(["install", "egg", "x"]) == 2
    assert check.main(["update", "file", "two"]) == 2
    assert "check_student_install.py install KIND PATH" in capsys.readouterr().err
