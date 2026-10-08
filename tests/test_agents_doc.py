"""The guidance for coding agents: the packaged AGENTS.md, `python -m qte_sdk.agents`, llms.txt."""

import os
import re
import subprocess
import sys
import tarfile
import zipfile
from importlib import resources
from pathlib import Path

import pytest

import qte_sdk.agents

ROOT = Path(__file__).resolve().parent.parent
REPO_AGENTS = ROOT / "AGENTS.md"
PACKAGED_AGENTS = ROOT / "qte_sdk" / "AGENTS.md"
LLMS = ROOT / "llms.txt"
ERRORS = ROOT / "docs" / "errors.md"

GITHUB = re.compile(
    r"https://(github\.com/josh-g-s/qte-sdk(/blob/main/[\w./-]+|#[\w-]+)?"
    r"|raw\.githubusercontent\.com/josh-g-s/qte-sdk/main/[\w./-]+)"
)
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def test_the_packaged_agents_md_is_the_repositorys():
    # One file, two places: the repository's for a clone, the package's for any install.
    # Edit AGENTS.md, then copy it over qte_sdk/AGENTS.md (or the other way round).
    assert PACKAGED_AGENTS.read_bytes() == REPO_AGENTS.read_bytes(), (
        "AGENTS.md and qte_sdk/AGENTS.md differ: copy the one you edited over the other"
    )


def test_the_packaged_agents_md_links_only_to_github():
    # An install has no docs/ folder, so a relative link would lead nowhere.
    links = LINK.findall(PACKAGED_AGENTS.read_text(encoding="utf-8"))
    assert links
    for link in links:
        assert GITHUB.fullmatch(link), link


def test_the_packaged_agents_md_is_plain_ascii():
    # So printing it works on any console, such as a Windows one with a legacy code page.
    data = PACKAGED_AGENTS.read_bytes()
    assert data.isascii()


def test_every_code_the_agents_md_names_is_in_errors_md():
    text = PACKAGED_AGENTS.read_text(encoding="utf-8")
    listed = set(re.findall(r"^### (QTE-[A-Z-]+)$", ERRORS.read_text(encoding="utf-8"), re.M))
    named = set(re.findall(r"QTE-[A-Z]+(?:-[A-Z]+)+(?![A-Z-])", text))
    assert named
    assert named <= listed, named - listed


def test_the_agents_md_covers_what_an_agent_needs():
    text = PACKAGED_AGENTS.read_text(encoding="utf-8")
    for needed in [
        "python -m qte_sdk.agents",
        "python -m qte_sdk.update",
        "QTE-UPDATE-AVAILABLE",
        "py -m venv .venv",
        "archive/refs/tags/",
        "python -m qte_sdk.token set",
        "https://github.com/josh-g-s/qte-sdk/blob/main/docs/errors.md",
        "## Setup",
        "## Updates",
        "## The token",
        "## Message codes",
    ]:
        assert needed in text, needed


def test_the_agents_md_is_installed_with_the_package():
    resource = resources.files("qte_sdk").joinpath("AGENTS.md")
    assert resource.is_file()
    assert qte_sdk.agents.text() == resource.read_text(encoding="utf-8")


def test_python_m_qte_sdk_agents_prints_it(tmp_path):
    # Run away from the repository, so the installed package is the one that answers.
    result = subprocess.run(
        [sys.executable, "-m", "qte_sdk.agents"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout == qte_sdk.agents.text()


def test_main_prints_it_and_returns_0(capsys):
    assert qte_sdk.agents.main([]) == 0
    assert capsys.readouterr().out == qte_sdk.agents.text()


def test_main_takes_no_arguments(capsys):
    with pytest.raises(SystemExit) as exit_info:
        qte_sdk.agents.main(["--nope"])
    assert exit_info.value.code == 2


# The built packages


@pytest.fixture(scope="module")
def dist(tmp_path_factory):
    """A wheel and an sdist built from this source tree, as `python -m build` builds them."""
    out = tmp_path_factory.mktemp("dist")
    # --no-isolation uses the hatchling the dev extra installs, so no network is needed.
    command = [sys.executable, "-m", "build", "--no-isolation", "--sdist", "--wheel"]
    result = subprocess.run(
        command + ["--outdir", str(out), str(ROOT)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    [wheel] = out.glob("*.whl")
    [sdist] = out.glob("*.tar.gz")
    return wheel, sdist


def test_the_wheel_holds_the_agents_md(dist):
    wheel, _ = dist
    with zipfile.ZipFile(wheel) as archive:
        assert archive.read("qte_sdk/AGENTS.md") == PACKAGED_AGENTS.read_bytes()
        assert "qte_sdk/agents.py" in archive.namelist()


def test_the_sdist_holds_the_agents_md_and_llms_txt(dist):
    _, sdist = dist
    with tarfile.open(sdist) as archive:
        names = {name.split("/", 1)[1] for name in archive.getnames() if "/" in name}
    assert {"qte_sdk/AGENTS.md", "qte_sdk/agents.py", "AGENTS.md", "llms.txt"} <= names


@pytest.mark.parametrize("zipped", [False, True], ids=["installed", "zipimport"])
def test_the_wheel_prints_it_from_an_install(dist, tmp_path, zipped):
    # Install the wheel's files alone (no network), then run the command from them.
    wheel, _ = dist
    target = tmp_path / "site"
    installed = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--no-index"]
        + ["--target", str(target), str(wheel)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    path = target
    if zipped:
        path = tmp_path / "site.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for file in (target / "qte_sdk").rglob("*"):
                archive.write(file, file.relative_to(target).as_posix())
    env = {**os.environ, "PYTHONPATH": str(path)}
    where = subprocess.run(
        [sys.executable, "-c", "import qte_sdk.agents as a; print(a.__file__)"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=60,
    )
    assert where.returncode == 0, where.stderr
    assert Path(where.stdout.strip()).is_relative_to(path), where.stdout
    result = subprocess.run(
        [sys.executable, "-m", "qte_sdk.agents"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == PACKAGED_AGENTS.read_text(encoding="utf-8")


# llms.txt


def test_llms_txt_follows_the_convention():
    lines = LLMS.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# qte-sdk"
    assert lines[1] == ""
    assert lines[2].startswith("> ")
    assert sum(line.startswith("# ") for line in lines) == 1
    assert any(line.startswith("## ") for line in lines)
    assert LLMS.read_bytes().isascii()


def test_llms_txt_points_to_the_agents_md_quickstart_and_errors():
    text = LLMS.read_text(encoding="utf-8")
    links = LINK.findall(text)
    for needed in ["qte_sdk/AGENTS.md", "docs/quickstart.md", "docs/errors.md"]:
        assert needed in links, needed
    assert "python -m qte_sdk.agents" in text


def test_llms_txt_links_resolve():
    links = LINK.findall(LLMS.read_text(encoding="utf-8"))
    assert links
    for link in links:
        if link.startswith("https://"):
            assert GITHUB.fullmatch(link), link
            path = re.match(r"https://github\.com/josh-g-s/qte-sdk/blob/main/(.+)", link)
            if path:
                assert (ROOT / path[1]).is_file(), link
        else:
            assert "://" not in link and not link.startswith("/"), link
            assert (ROOT / link).is_file(), link
