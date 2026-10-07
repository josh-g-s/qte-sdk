"""Check, in CI's windows job, that every Windows test ran and none was skipped.

    python scripts/check_windows_tests.py windows-tests.xml

The tests that must run are every test in tests/test_windows_real.py, marked `windows` or
not, and every test marked `windows` anywhere. They are listed by a separate pytest
process, with no `addopts` and no `PYTEST_ADDOPTS`, before any `-k`, `-m`, `--deselect` or
conftest hook leaves a test out, so nothing that filters the real run can shrink the list
too. Each must be in the JUnit report the real run wrote, by name, and not skipped. The
report has no entry for a test the run deselected, so a deselected test is reported as one
that did not run. It prints what is wrong and exits 1, or exits 0.
"""

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE = "tests/test_windows_real.py"
MARKER = "windows"
INVENTORY = "--inventory"
PREFIX = "windows tests: "


def inventory() -> list[str]:
    """The node IDs of the tests that must run, listed by a pytest process of their own
    that no option meant for the real run reaches."""
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), INVENTORY],
        cwd=ROOT,
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
        ids: list[str] | None = None

        # First, so the list is taken before -k, -m, --deselect or a conftest hook, which
        # all act in this hook, leave a test out.
        @pytest.hookimpl(tryfirst=True)
        def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
            self.ids = [
                item.nodeid
                for item in items
                if item.nodeid.startswith(MODULE + "::") or item.get_closest_marker(MARKER)
            ]

    found = Inventory()
    args = ["--collect-only", "-q", "-o", "addopts=", "-p", "no:cacheprovider", "tests"]
    code = pytest.main(args, plugins=[found])
    if code != 0 or found.ids is None:
        return 1
    print(PREFIX + json.dumps(found.ids))
    return 0


def junit_key(nodeid: str) -> tuple[str, str]:
    """The classname and name pytest's JUnit report gives the test with `nodeid`."""
    path, *rest = nodeid.split("::")
    module = path.removesuffix(".py").replace("/", ".")
    return ".".join([module, *rest[:-1]]), rest[-1]


def problems(report: Path, expected: list[str]) -> list[str]:
    """What is wrong with the run that wrote `report`, given the tests that must run."""
    if not any(nodeid.startswith(MODULE + "::") for nodeid in expected):
        return [f"no test was found in {MODULE}"]
    cases = {
        (case.get("classname"), case.get("name")): case
        for case in ET.parse(report).getroot().iter("testcase")
    }
    found = []
    for nodeid in expected:
        case = cases.get(junit_key(nodeid))
        if case is None:
            found.append(f"did not run (deselected, or not collected): {nodeid}")
        elif case.find("skipped") is not None:
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
