"""Check, in CI's windows job, that every Windows test ran and none was skipped.

    python scripts/check_windows_tests.py windows-tests.xml

The tests that must run are every test in tests/test_windows_real.py, marked `windows` or
not, and every test marked `windows` anywhere. They are listed by a separate pytest
process, with no `addopts`, no `PYTEST_ADDOPTS` or `PYTEST_PLUGINS`, no conftest and no
plugin loaded by entry point, as each test is collected, before `-k`, `-m`, `--deselect`
or any hook that selects tests can leave one out. So options, conftest files and plugins
named in the environment that filter or hide tests in the real run cannot shrink the list
too; a plugin a test module names in its `pytest_plugins` still loads in both. Each must
be in the JUnit report the real run wrote, by the name pytest gives it there, as many
times as it is listed, and not skipped. The report has no entry for a test the run
deselected, so a deselected test is reported as one that did not run. It prints what is
wrong and exits 1, or exits 0.
"""

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE = "tests/test_windows_real.py"
MARKER = "windows"
INVENTORY = "--inventory"
PREFIX = "windows tests: "


def inventory(root: Path = ROOT) -> list[str]:
    """The node IDs of the tests that must run in the project at `root`, listed by a pytest
    process of their own that no option meant for the real run reaches."""
    # No options, plugins or conftest that could leave a test out of the list.
    env = {k: v for k, v in os.environ.items() if k not in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")}
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), INVENTORY],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith(PREFIX)]
    if result.returncode != 0 or len(lines) != 1:
        raise SystemExit(
            f"listing the Windows tests failed ({result.returncode}):\n"
            f"{result.stdout}{result.stderr}"
        )
    return json.loads(lines[0][len(PREFIX) :])


def _list_tests() -> int:
    """Print the node IDs of the tests that must run, as JSON after `PREFIX`."""
    import pytest

    class Inventory:
        def __init__(self) -> None:
            self.ids: list[str] = []
            self.finished = False

        # Called as each test is collected, before pytest_collection_modifyitems, where -k,
        # -m, --deselect and any hook that selects tests act, whatever their order.
        def pytest_itemcollected(self, item: pytest.Item) -> None:
            if item.nodeid.startswith(MODULE + "::") or item.get_closest_marker(MARKER):
                self.ids.append(item.nodeid)

        def pytest_collection_finish(self) -> None:
            self.finished = True

    found = Inventory()
    args = [
        "--collect-only",
        "-q",
        "--noconftest",
        "-o",
        "addopts=",
        "-p",
        "no:cacheprovider",
        "tests",
    ]
    code = pytest.main(args, plugins=[found])
    if code != 0 or not found.finished:
        return 1
    print(PREFIX + json.dumps(found.ids))
    return 0


def junit_key(nodeid: str) -> tuple[str, str]:
    """The classname and name pytest's JUnit report gives the test with `nodeid`, by
    pytest's own rules, which keep a parameter ID whole and escape what XML cannot hold."""
    from _pytest.junitxml import bin_xml_escape, mangle_test_address

    names = mangle_test_address(nodeid)
    return ".".join(names[:-1]), bin_xml_escape(names[-1])


def problems(report: Path, expected: list[str]) -> list[str]:
    """What is wrong with the run that wrote `report`, given the tests that must run."""
    if not any(nodeid.startswith(MODULE + "::") for nodeid in expected):
        return [f"no test was found in {MODULE}"]
    cases: defaultdict[tuple[str | None, str | None], list[ET.Element]] = defaultdict(list)
    for case in ET.parse(report).getroot().iter("testcase"):
        cases[(case.get("classname"), case.get("name"))].append(case)
    wanted = Counter(junit_key(nodeid) for nodeid in expected)
    found = []
    for nodeid in dict.fromkeys(expected):
        key = junit_key(nodeid)
        ran = cases.get(key, [])
        if len(ran) < wanted[key]:
            found.append(f"did not run (deselected, or not collected): {nodeid}")
        if any(case.find("skipped") is not None for case in ran):
            found.append(f"skipped: {nodeid}")
    return found


def main(argv: list[str]) -> int:
    if argv == [INVENTORY]:
        return _list_tests()
    if len(argv) != 1:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    expected = inventory()
    found = problems(Path(argv[0]), expected)
    print(f"{len(expected)} Windows tests must run; {len(found)} problems")
    for problem in found:
        print(problem)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
