"""The check CI's windows job runs after its Windows tests,
`scripts/check_windows_tests.py`: every Windows test must be in the run's JUnit report, by
name, and none skipped, whatever filtered the run."""

import importlib.util
import subprocess
import sys
import textwrap
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

# A project with tests like the real ones: some marked windows, one not, one in another
# module, and parameter IDs that hold `::`, brackets and a character XML cannot hold.
WINDOWS_TESTS = """
    import pytest

    pytestmark = pytest.mark.windows


    def test_one():
        pass


    @pytest.mark.parametrize("value", ["a::b", "x[1]", "tab\\there", "bell\\x07"])
    def test_two(value):
        pass


    def test_dropped():
        pass
"""
OTHER_TESTS = """
    import pytest


    class TestThing:
        @pytest.mark.windows
        def test_marked(self):
            pass

        def test_not_marked(self):
            pass
"""
# A conftest whose hooks, a wrapper and a tryfirst one, each leave a Windows test out of
# the run, as a filter could.
FILTERING_CONFTEST = """
    import pytest


    @pytest.hookimpl(hookwrapper=True)
    def pytest_collection_modifyitems(config, items):
        items[:] = [i for i in items if not i.nodeid.endswith("test_dropped")]
        yield


    class Selector:
        @pytest.hookimpl(tryfirst=True)
        def pytest_collection_modifyitems(self, config, items):
            items[:] = [i for i in items if "a::b" not in i.nodeid]


    def pytest_configure(config):
        config.pluginmanager.register(Selector(), "selector")
"""


def project(tmp_path: Path, conftest: str = "") -> Path:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    (tests / "test_windows_real.py").write_text(textwrap.dedent(WINDOWS_TESTS))
    (tests / "test_other.py").write_text(textwrap.dedent(OTHER_TESTS))
    (tests / "conftest.py").write_text(textwrap.dedent(conftest))
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    windows: real Windows\n")
    return tmp_path


def run(root: Path, *args: str) -> Path:
    """Run the project's tests marked windows, as CI does, and return the JUnit report."""
    report = root / "report.xml"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-m",
            "windows",
            f"--junitxml={report}",
            *args,
        ],
        cwd=root,
        capture_output=True,
        check=False,
    )
    return report


EXPECTED = 7  # test_one, four of test_two, test_dropped and TestThing::test_marked


def test_a_full_run_passes(tmp_path):
    root = project(tmp_path)
    expected = check.inventory(root)
    assert len(expected) == EXPECTED
    assert "tests/test_other.py::TestThing::test_marked" in expected
    assert check.problems(run(root), expected) == []


def test_the_names_match_those_pytest_writes(tmp_path):
    # Checked against a report pytest wrote, not against names made by the script.
    root = project(tmp_path)
    report = run(root)
    import xml.etree.ElementTree as ET

    written = {(c.get("classname"), c.get("name")) for c in ET.parse(report).iter("testcase")}
    assert {check.junit_key(nodeid) for nodeid in check.inventory(root)} == written


@pytest.mark.parametrize(
    ("args", "left_out"),
    [
        (["-k", "not test_one"], "tests/test_windows_real.py::test_one"),
        (
            ["--deselect", "tests/test_windows_real.py::test_two[a::b]"],
            "tests/test_windows_real.py::test_two[a::b]",
        ),
    ],
)
def test_a_test_the_run_leaves_out_fails(tmp_path, args, left_out):
    root = project(tmp_path)
    assert check.problems(run(root, *args), check.inventory(root)) == [
        f"did not run (deselected, or not collected): {left_out}"
    ]


def test_filters_meant_for_the_run_do_not_shrink_the_list(tmp_path, monkeypatch):
    root = project(tmp_path)
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k test_one")
    assert len(check.inventory(root)) == EXPECTED
    report = run(root)  # with PYTEST_ADDOPTS still set
    assert len(check.problems(report, check.inventory(root))) == EXPECTED - 1


def test_conftest_hooks_that_leave_tests_out_do_not_shrink_the_list(tmp_path):
    root = project(tmp_path, FILTERING_CONFTEST)
    expected = check.inventory(root)
    assert len(expected) == EXPECTED
    assert check.problems(run(root), expected) == [
        "did not run (deselected, or not collected): tests/test_windows_real.py::test_two[a::b]",
        "did not run (deselected, or not collected): tests/test_windows_real.py::test_dropped",
    ]


def test_a_test_that_loses_its_mark_fails(tmp_path):
    root = project(tmp_path)
    module = root / "tests" / "test_windows_real.py"
    module.write_text(module.read_text().replace("pytestmark = pytest.mark.windows", ""))
    found = check.problems(run(root), check.inventory(root))
    assert "did not run (deselected, or not collected): tests/test_windows_real.py::test_one" in (
        found
    )


def report_of(tmp_path: Path, *cases: tuple[str, str, bool]) -> Path:
    body = "".join(
        f'<testcase classname="{c}" name="{n}">{"<skipped />" if skipped else ""}</testcase>'
        for c, n, skipped in cases
    )
    path = tmp_path / "report.xml"
    path.write_text(f"<testsuites><testsuite>{body}</testsuite></testsuites>")
    return path


def test_a_skipped_entry_is_not_hidden_by_a_passing_one_of_the_same_name(tmp_path):
    nodeid = "tests/test_windows_real.py::test_one"
    key = ("tests.test_windows_real", "test_one")
    path = report_of(tmp_path, (*key, True), (*key, False))
    assert check.problems(path, [nodeid]) == [f"skipped: {nodeid}"]


def test_two_listed_tests_need_two_entries(tmp_path):
    nodeid = "tests/test_windows_real.py::test_one"
    path = report_of(tmp_path, ("tests.test_windows_real", "test_one", False))
    assert check.problems(path, [nodeid, nodeid]) == [
        f"did not run (deselected, or not collected): {nodeid}"
    ]


def test_no_test_in_the_module_fails(tmp_path):
    path = report_of(tmp_path)
    assert check.problems(path, ["tests/test_other.py::test_marked"]) == [
        "no test was found in tests/test_windows_real.py"
    ]


def test_the_list_of_this_repository_has_the_real_windows_tests(monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k nothing_matches_this -m windows")
    listed = check.inventory()
    assert "tests/test_windows_real.py::test_nul_is_not_a_console" in listed
