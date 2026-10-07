"""The check CI's windows job runs after its Windows tests,
`scripts/check_windows_tests.py`: every Windows test must be in the run's JUnit report, by
name, and none skipped, whatever filtered the run."""

import importlib.util
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_windows_tests.py"


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("check_windows_tests", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = load_script()
FIRST = "tests/test_windows_real.py::test_one"
SECOND = "tests/test_windows_real.py::test_two[NUL-command0]"
ELSEWHERE = "tests/test_other.py::TestThing::test_marked"


def report(tmp_path: Path, *cases: str, skipped: tuple[str, ...] = ()) -> Path:
    """A JUnit report, as pytest writes one, of `cases` (node IDs), `skipped` among them."""
    lines = []
    for nodeid in cases:
        classname, name = check.junit_key(nodeid)
        inner = '<skipped message="skip" />' if nodeid in skipped else ""
        lines.append(f'<testcase classname="{classname}" name="{name}">{inner}</testcase>')
    path = tmp_path / "report.xml"
    path.write_text(
        f'<testsuites><testsuite name="pytest">{"".join(lines)}</testsuite></testsuites>'
    )
    return path


def test_the_junit_names_of_a_test():
    assert check.junit_key(FIRST) == ("tests.test_windows_real", "test_one")
    assert check.junit_key(SECOND) == ("tests.test_windows_real", "test_two[NUL-command0]")
    assert check.junit_key(ELSEWHERE) == ("tests.test_other.TestThing", "test_marked")


def test_a_run_of_every_test_passes(tmp_path):
    path = report(tmp_path, FIRST, SECOND, ELSEWHERE)
    assert check.problems(path, [FIRST, SECOND, ELSEWHERE]) == []


@pytest.mark.parametrize("left_out", [FIRST, SECOND, ELSEWHERE])
def test_a_test_left_out_of_the_run_fails(tmp_path, left_out):
    ran = [nodeid for nodeid in (FIRST, SECOND, ELSEWHERE) if nodeid != left_out]
    path = report(tmp_path, *ran)
    assert check.problems(path, [FIRST, SECOND, ELSEWHERE]) == [
        f"did not run (deselected, or not collected): {left_out}"
    ]


def test_a_skipped_test_fails(tmp_path):
    path = report(tmp_path, FIRST, SECOND, skipped=(SECOND,))
    assert check.problems(path, [FIRST, SECOND]) == [f"skipped: {SECOND}"]


def test_no_test_in_the_module_fails(tmp_path):
    path = report(tmp_path, ELSEWHERE)
    assert check.problems(path, [ELSEWHERE]) == ["no test was found in tests/test_windows_real.py"]


def test_the_list_ignores_filters_meant_for_the_run(monkeypatch):
    # The list comes from a pytest process of its own, which PYTEST_ADDOPTS does not reach.
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k nothing_matches_this -m windows")
    listed = check.inventory()
    assert any(nodeid.startswith("tests/test_windows_real.py::") for nodeid in listed)
    assert "tests/test_windows_real.py::test_nul_is_not_a_console" in listed
