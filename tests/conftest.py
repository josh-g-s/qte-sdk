from pathlib import Path

import pytest

from qte_sdk import dotenv


@pytest.fixture(autouse=True)
def isolated_from_the_developers_setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run each test in an empty working directory with no `QTE_URL`, so a developer's own
    `.env` or environment cannot reach it, and let each test see the git warning afresh.
    The modules that need it also clear `QTE_TOKEN` and `QTE_TOKEN_FILE`."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("QTE_URL", raising=False)
    monkeypatch.setattr(dotenv, "_git_checked", False)
