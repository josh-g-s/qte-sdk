import sys
from pathlib import Path

import pytest

from qte_sdk import dotenv, update


@pytest.fixture(autouse=True)
def isolated_from_the_developers_setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run each test in an empty working directory with no `QTE_URL`, so a developer's own
    `.env` or environment cannot reach it, and let each test see the git warning and the
    Windows access warning afresh. The modules that need it also clear `QTE_TOKEN` and
    `QTE_TOKEN_FILE`.

    The automatic update check is turned off (`QTE_UPDATE_CHECK=0`), so no session a test
    opens reads GitHub, and its cache folder is one of the test's own, so nothing is written
    to the developer's. The tests of the automatic check turn it back on."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("QTE_URL", raising=False)
    monkeypatch.setattr(dotenv, "_git_checked", set())
    monkeypatch.setattr(dotenv, "_shared_warned", set())
    monkeypatch.setenv(update.UPDATE_CHECK_ENV_VAR, "0")
    monkeypatch.setattr(update, "_cache_dir", lambda: tmp_path / "cache" / "qte-sdk")
    monkeypatch.setattr(update, "_automatic_done", False)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip the tests marked `windows` everywhere but Windows: they make and read real access
    lists and consoles through the Windows API. CI runs them on its `windows` job."""
    if sys.platform == "win32":
        return
    skip = pytest.mark.skip(reason="Windows only: needs the real Windows API (CI's windows job)")
    for item in items:
        if item.get_closest_marker("windows") is not None:
            item.add_marker(skip)
