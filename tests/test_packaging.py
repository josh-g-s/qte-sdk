"""The SDK's package: what installing it brings in."""

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def dependencies() -> list[str]:
    with (ROOT / "pyproject.toml").open("rb") as file:
        return tomllib.load(file)["project"]["dependencies"]


def test_windows_gets_the_time_zone_database():
    # Windows has none of its own, so zoneinfo cannot find New York's time zone without it.
    [tzdata] = [d for d in dependencies() if d.startswith("tzdata")]
    assert tzdata == "tzdata; sys_platform == 'win32'"
    try:
        from packaging.requirements import Requirement
    except ImportError:
        pytest.skip("packaging is not installed, so only the text is checked")
    requirement = Requirement(tzdata)
    assert requirement.name == "tzdata"
    assert requirement.marker is not None
    assert requirement.marker.evaluate({"sys_platform": "win32"})
    assert not requirement.marker.evaluate({"sys_platform": "linux"})
    assert not requirement.marker.evaluate({"sys_platform": "darwin"})
