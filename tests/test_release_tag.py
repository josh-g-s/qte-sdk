"""The release tag check, `scripts/check_release_tag.py`, that CI runs on a pushed tag."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import qte_sdk

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_release_tag.py"


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("check_release_tag", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source(tmp_path: Path, version: str = "1.2.3", changelog: str | None = "## 1.2.3\n") -> Path:
    (tmp_path / "qte_sdk").mkdir()
    (tmp_path / "qte_sdk" / "__init__.py").write_text(f'"""Doc."""\n\n__version__ = "{version}"\n')
    if changelog is not None:
        (tmp_path / "CHANGELOG.md").write_text(f"# Changelog\n\n## Unreleased\n\n{changelog}")
    return tmp_path


def test_this_sources_version_has_a_changelog_entry_and_would_take_its_tag():
    assert load_script().problems(f"v{qte_sdk.__version__}") == []


def test_a_tag_that_matches_passes(tmp_path: Path):
    assert load_script().problems("v1.2.3", source(tmp_path)) == []


@pytest.mark.parametrize(
    "tag", ["1.2.3", "v1.2", "v1.2.3-rc1", "v01.2.3", "V1.2.3", "v1.2.3 ", "v1234567890.0.0"]
)
def test_a_tag_that_is_not_a_release_number_fails(tmp_path: Path, tag: str):
    [problem] = load_script().problems(tag, source(tmp_path))
    assert "is not a release tag" in problem


def test_a_tag_that_differs_from_the_version_fails(tmp_path: Path):
    assert load_script().problems("v1.2.4", source(tmp_path, changelog="## 1.2.4\n")) == [
        "the tag is v1.2.4 but __version__ is '1.2.3'"
    ]


@pytest.mark.parametrize("changelog", [None, "", "## 1.2.30\n", "### 1.2.3\n", "## 1.2.3 x\n"])
def test_a_version_with_no_changelog_entry_fails(tmp_path: Path, changelog: str | None):
    assert load_script().problems("v1.2.3", source(tmp_path, changelog=changelog)) == [
        "CHANGELOG.md has no '## 1.2.3' entry"
    ]


def test_the_script_exits_1_with_what_is_wrong():
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "v999.0.0"], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 1
    assert "error: the tag is v999.0.0 but __version__ is" in done.stderr
    assert "error: CHANGELOG.md has no '## 999.0.0' entry" in done.stderr


def test_the_script_exits_0_for_this_sources_tag():
    done = subprocess.run(
        [sys.executable, str(SCRIPT), f"v{qte_sdk.__version__}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr


def test_the_script_needs_one_tag():
    assert load_script().main([]) == 2
