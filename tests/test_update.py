"""The update check: what it reads from pip's install record and from the repository's
list of references, and what it says. No test here uses the network: every read of the
repository is answered by a canned list, and any other is an error."""

import asyncio
import email.message
import http.server
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest

import qte_sdk
from qte_sdk import update
from qte_sdk.update import Status, check_for_update, update_command

VERSION = qte_sdk.__version__
ORIGINAL_OPEN = update._open
INSTALLED = "1" * 40
MAIN = "2" * 40
TAG_OBJECT = "3" * 40
RELEASE_COMMIT = "4" * 40
REPOSITORY_URL = "https://github.com/josh-g-s/qte-sdk"
# Behind a release: a plain upgrade, which resolves the new release's dependencies.
COMMAND = 'pip install --upgrade "git+https://github.com/josh-g-s/qte-sdk"'
# Newer commits on main at the same version: the SDK alone, reinstalled.
REINSTALL = (
    'pip install --upgrade --force-reinstall --no-deps "git+https://github.com/josh-g-s/qte-sdk"'
)


def tag_for(version: str) -> str:
    return f"v{version}"


def behind_message(tag: str, command: str, recommended: str = "") -> str:
    """The line behind release `tag`: `recommended` is the part that says the update is
    recommended, such as ", a recommended update on Windows: <why>"."""
    return (
        f"QTE-UPDATE-AVAILABLE: qte-sdk {VERSION} is behind {tag.removeprefix('v')}"
        f"{recommended}. Update with {command}."
    )


def bumped(version: str, part: int) -> str:
    numbers = [int(n) for n in version.split(".")]
    numbers[part] += 1
    numbers[part + 1 :] = [0] * (2 - part)
    return ".".join(map(str, numbers))


# Canned answers


def pkt(line: bytes) -> bytes:
    return b"%04x" % (len(line) + 4) + line


def advertisement(refs: list[tuple[str, str]]) -> bytes:
    """A list of references as git's smart HTTP service sends it, in protocol v0."""
    body = pkt(b"# service=git-upload-pack\n") + b"0000"
    for index, (oid, name) in enumerate(refs):
        line = f"{oid} {name}".encode()
        if index == 0:
            line += b"\0multi_ack thin-pack side-band ofs-delta agent=git/github"
        body += pkt(line + b"\n")
    return body + b"0000"


def refs_with(*tags: tuple[str, str], main: str = MAIN) -> bytes:
    return advertisement(
        [(main, "HEAD"), (main, "refs/heads/main"), *((oid, f"refs/tags/{t}") for t, oid in tags)]
    )


REFS_TYPE = "application/x-git-upload-pack-advertisement"


class FakeResponse:
    def __init__(self, body: bytes, url: str, content_type: str = REFS_TYPE) -> None:
        self.body = body
        self.url = url
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def geturl(self) -> str:
        return self.url

    def read1(self, size: int = -1) -> bytes:
        chunk, self.body = (self.body, b"") if size < 0 else (self.body[:size], self.body[size:])
        return chunk


class Repository:
    """Answers the update check's request for the list of references with `body`, or raises
    `error`, and records the requests in `requests`. A request for releases.json is answered
    with `releases`, or raises `releases_error` (by default, as when the file is missing),
    and is recorded in `release_requests`."""

    def __init__(
        self,
        body: bytes = b"",
        error: BaseException | None = None,
        releases: bytes | None = None,
        releases_error: BaseException | None = None,
        **kwargs: Any,
    ):
        self.body = body
        self.error = error
        self.releases = releases
        self.releases_error = releases_error
        self.kwargs = kwargs
        self.requests: list[tuple[urllib.request.Request, float]] = []
        self.release_requests: list[tuple[urllib.request.Request, float]] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> FakeResponse:
        if request.full_url == update.RELEASES_URL:
            self.release_requests.append((request, timeout))
            if self.releases_error is not None:
                raise self.releases_error
            if self.releases is None:
                raise urllib.error.HTTPError(
                    request.full_url, 404, "Not Found", email.message.Message(), None
                )
            return FakeResponse(self.releases, request.full_url, "text/plain")
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        url = self.kwargs.pop("url", request.full_url)
        return FakeResponse(self.body, url, **self.kwargs)


class FakeDistribution:
    def __init__(
        self, direct_url: str | None, version: str = VERSION, package: str | None = None
    ) -> None:
        self.direct_url = direct_url
        self.version = version
        self.package = package or qte_sdk.__file__

    def locate_file(self, path: str) -> Path:
        assert path == "qte_sdk/__init__.py"
        return Path(self.package)

    def read_text(self, name: str) -> str | None:
        assert name == "direct_url.json"
        return self.direct_url


def no_network(*args: object, **kwargs: object) -> None:
    raise AssertionError("the network was used")


LOOPBACK = ("127.0.0.1", "::1", "localhost")
real_connect = socket.socket.connect
real_getaddrinfo = socket.getaddrinfo


def loopback_connect(self: socket.socket, address: Any) -> None:
    if not isinstance(address, tuple) or address[0] not in LOOPBACK:
        no_network()
    real_connect(self, address)


def loopback_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
    if host not in LOOPBACK:
        no_network()
    return real_getaddrinfo(host, *args, **kwargs)


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any use of the network beyond this machine fails the test, and so does any read of
    the repository that a test does not answer with a canned list."""
    monkeypatch.setattr(update, "_open", no_network)
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    monkeypatch.setattr(socket.socket, "connect", loopback_connect)
    monkeypatch.setattr(socket, "getaddrinfo", loopback_getaddrinfo)


def installed(monkeypatch: pytest.MonkeyPatch, record: Any, version: str = VERSION) -> None:
    """Make pip's install record `record`: a dict as JSON, a string as it is, or None for no
    file."""
    text = json.dumps(record) if isinstance(record, dict) else record
    monkeypatch.setattr(
        update.metadata, "distribution", lambda name: FakeDistribution(text, version)
    )


def git_install(revision: str | None = None, url: str = REPOSITORY_URL) -> dict[str, Any]:
    vcs_info: dict[str, Any] = {"vcs": "git", "commit_id": INSTALLED}
    if revision is not None:
        vcs_info["requested_revision"] = revision
    return {"url": url, "vcs_info": vcs_info}


def answer(monkeypatch: pytest.MonkeyPatch, body: bytes = b"", **kwargs: Any) -> Repository:
    repository = Repository(body, **kwargs)
    monkeypatch.setattr(update, "_open", repository)
    return repository


# Where it was installed from


@pytest.mark.parametrize("revision", [None, "main", "v1.0.0", INSTALLED, "some-branch"])
def test_a_git_install_from_the_repository_is_compared_with_its_releases(
    monkeypatch: pytest.MonkeyPatch, revision: str | None
):
    installed(monkeypatch, git_install(revision))
    repository = answer(monkeypatch, refs_with((tag_for(VERSION), INSTALLED), main=INSTALLED))
    result = check_for_update()
    assert result.status is Status.CURRENT
    assert result.exit_code == 0
    assert result.installed_commit == INSTALLED
    assert result.installed_revision == revision
    assert result.latest_release == tag_for(VERSION)
    assert len(repository.requests) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/josh-g-s/qte-sdk",
        "https://github.com/josh-g-s/qte-sdk.git",
        "https://github.com/josh-g-s/qte-sdk/",
        "https://github.com/josh-g-s/qte-sdk.git/",
        "https://GitHub.com/Josh-G-S/QTE-SDK.GIT",
        "http://github.com/josh-g-s/qte-sdk",
        "ssh://git@github.com/josh-g-s/qte-sdk.git",
        "https://someone:secret-password@github.com/josh-g-s/qte-sdk.git",
    ],
)
def test_each_spelling_of_the_repositorys_address_is_recognised(
    monkeypatch: pytest.MonkeyPatch, url: str
):
    installed(monkeypatch, git_install(url=url))
    answer(monkeypatch, refs_with((tag_for(VERSION), INSTALLED), main=INSTALLED))
    result = check_for_update()
    assert result.status is Status.CURRENT
    assert "secret-password" not in result.message


# Installs it cannot place: they are compared with the latest release, but never current.
UNPLACED = {
    "editable": (
        {"url": "file:///home/someone/qte-sdk", "dir_info": {"editable": True}},
        "it is an editable install of a local copy",
    ),
    "local directory": (
        {"url": "file:///home/someone/qte-sdk", "dir_info": {}},
        "it was installed from a local copy",
    ),
    "wheel": (
        {"url": "file:///home/someone/qte_sdk-1.0.0-py3-none-any.whl", "archive_info": {}},
        "it was installed from an archive or wheel",
    ),
    "another repository's archive": (
        {
            "url": "https://github.com/someone/qte-sdk/archive/refs/tags/v1.0.0.zip",
            "archive_info": {"hash": "sha256=" + "0" * 64},
        },
        "it was installed from an archive or wheel",
    ),
    "another vcs": (
        {"url": REPOSITORY_URL, "vcs_info": {"vcs": "hg", "commit_id": INSTALLED}},
        "another version control system",
    ),
    "a fork": (git_install(url="https://github.com/someone/qte-sdk"), "another repository"),
    "a similar name": (git_install(url="https://github.com/josh-g-s/qte-sdk-old"), "another"),
    "another host": (git_install(url="https://gitlab.com/josh-g-s/qte-sdk"), "another"),
    "a local repository": (git_install(url="file:///home/someone/qte-sdk"), "another"),
    "a query": (git_install(url=f"{REPOSITORY_URL}?x=1"), "another repository"),
    "a bad port": (git_install(url="https://github.com:x/josh-g-s/qte-sdk"), "another"),
    "no file": (None, "pip did not record where it came from"),
    "no commit": ({"url": REPOSITORY_URL, "vcs_info": {"vcs": "git"}}, "names no commit"),
    "a short commit": (
        {"url": REPOSITORY_URL, "vcs_info": {"vcs": "git", "commit_id": "abc123"}},
        "names no commit",
    ),
}
# The installs an archive of the latest release can replace: the rest are told the git one.
FROM_AN_ARCHIVE = {"wheel", "another repository's archive"}

# Install records that cannot be read: nothing is fetched.
UNREADABLE_RECORDS = {
    "not json": "{not json",
    "not an object": "[]",
    "no url": {"vcs_info": {"vcs": "git", "commit_id": INSTALLED}},
    "a url that is not text": {"url": 1, "dir_info": {}},
    "no kind": {"url": REPOSITORY_URL},
    "two kinds": {**git_install(), "dir_info": {}},
    "a kind that is not an object": {"url": REPOSITORY_URL, "vcs_info": "git"},
    "a revision that is not text": {
        "url": REPOSITORY_URL,
        "vcs_info": {**git_install()["vcs_info"], "requested_revision": 1},
    },
}


def assert_private(result: update.UpdateCheck) -> None:
    # The recorded address is never repeated: it can hold a password or name a user.
    assert "someone" not in result.message
    assert "file://" not in result.message


@pytest.mark.parametrize("shape", UNPLACED)
def test_an_install_it_cannot_place_at_the_latest_release_cannot_tell(
    monkeypatch: pytest.MonkeyPatch, shape: str
):
    record, reason = UNPLACED[shape]
    installed(monkeypatch, record)
    repository = answer(monkeypatch, refs_with((tag_for(VERSION), RELEASE_COMMIT)))
    result = check_for_update()
    assert result.status is Status.UNKNOWN
    assert result.exit_code == 2
    assert result.command is None
    assert result.message.startswith(f"cannot tell whether qte-sdk {VERSION} is current: ")
    assert reason in result.message
    assert result.latest_release == tag_for(VERSION)
    assert result.installed_commit is None
    assert result.installed_archive is None
    assert len(repository.requests) == 1
    assert_private(result)


@pytest.mark.parametrize("shape", UNPLACED)
def test_an_install_it_cannot_place_behind_a_release_is_behind(
    monkeypatch: pytest.MonkeyPatch, shape: str
):
    record, _ = UNPLACED[shape]
    newer = tag_for(bumped(VERSION, 2))
    installed(monkeypatch, record)
    answer(monkeypatch, refs_with((tag_for(VERSION), RELEASE_COMMIT), (newer, MAIN)))
    result = check_for_update()
    assert result.status is Status.BEHIND
    assert result.exit_code == 1
    assert result.latest_release == newer
    expected = archive_command(newer) if shape in FROM_AN_ARCHIVE else COMMAND
    assert result.command == expected
    assert result.message == behind_message(newer, expected)
    assert_private(result)


@pytest.mark.parametrize("shape", ["editable", "wheel", "no file"])
def test_an_install_it_cannot_place_newer_than_the_latest_release_is_not_current(
    monkeypatch: pytest.MonkeyPatch, shape: str
):
    if VERSION == "0.0.0":
        pytest.skip("no release is older than 0.0.0")
    installed(monkeypatch, UNPLACED[shape][0])
    answer(monkeypatch, refs_with(("v0.0.0", RELEASE_COMMIT)))
    assert check_for_update().status is Status.UNKNOWN


@pytest.mark.parametrize(
    "error",
    [TimeoutError("timed out"), urllib.error.URLError("http://user:pw@proxy.invalid")],
)
def test_an_install_it_cannot_place_gives_its_own_reason_when_github_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
):
    installed(monkeypatch, UNPLACED["editable"][0])
    answer(monkeypatch, error=error)
    result = check_for_update()
    assert result.status is Status.UNKNOWN
    assert result.latest_release is None
    assert result.message == (
        f"cannot tell whether qte-sdk {VERSION} is current: it is an editable install of a "
        "local copy (pip install -e); update that copy with git"
    )


@pytest.mark.parametrize("shape", sorted(set(UNPLACED) - {"editable"}))
def test_an_install_it_cannot_place_says_how_to_install_one_it_can(
    monkeypatch: pytest.MonkeyPatch, shape: str
):
    installed(monkeypatch, UNPLACED[shape][0])
    answer(monkeypatch, refs_with((tag_for(VERSION), RELEASE_COMMIT)))
    assert check_for_update().message.endswith(
        "; to have it checked, install a release from github.com/josh-g-s/qte-sdk, with git "
        "or from the release's zip, as its README says"
    )


def test_a_local_copy_says_why_it_cannot_tell_and_what_to_do(monkeypatch: pytest.MonkeyPatch):
    installed(monkeypatch, UNPLACED["local directory"][0])
    answer(monkeypatch, refs_with((tag_for(VERSION), RELEASE_COMMIT)))
    assert check_for_update().message == (
        f"cannot tell whether qte-sdk {VERSION} is current: it was installed from a local "
        "copy, not from github.com/josh-g-s/qte-sdk; to have it checked, install a release "
        "from github.com/josh-g-s/qte-sdk, with git or from the release's zip, as its README "
        "says"
    )


@pytest.mark.parametrize("shape", ["editable", "no file"])
def test_an_install_it_cannot_place_with_no_release_cannot_tell(
    monkeypatch: pytest.MonkeyPatch, shape: str
):
    installed(monkeypatch, UNPLACED[shape][0])
    answer(monkeypatch, refs_with())
    result = check_for_update()
    assert result.status is Status.UNKNOWN
    assert UNPLACED[shape][1] in result.message


@pytest.mark.parametrize("shape", UNREADABLE_RECORDS)
def test_an_install_record_that_cannot_be_read_cannot_tell_and_uses_no_network(
    monkeypatch: pytest.MonkeyPatch, shape: str
):
    installed(monkeypatch, UNREADABLE_RECORDS[shape])
    repository = answer(monkeypatch, refs_with((tag_for(bumped(VERSION, 0)), MAIN)))
    result = check_for_update()
    assert result.status is Status.UNKNOWN
    assert result.exit_code == 2
    assert result.command is None
    assert result.message == (
        f"cannot tell whether qte-sdk {VERSION} is current: its install record "
        "(direct_url.json) cannot be read"
    )
    assert repository.requests == []


# From a release archive, with no git

ARCHIVE = "https://github.com/josh-g-s/qte-sdk/archive"
CODELOAD = "https://codeload.github.com/josh-g-s/qte-sdk"


def archive_command(tag: str) -> str:
    return f"pip install {ARCHIVE}/refs/tags/{tag}.zip"


def archive_install(url: str) -> dict[str, Any]:
    return {"url": url, "archive_info": {"hashes": {"sha256": "0" * 64}}}


ARCHIVE_URLS = {
    f"{ARCHIVE}/refs/tags/v1.0.1.zip": "v1.0.1",
    f"{ARCHIVE}/refs/tags/v1.0.1.tar.gz": "v1.0.1",
    f"{ARCHIVE}/v1.0.1.zip": "v1.0.1",
    f"{ARCHIVE}/v1.0.1.tar.gz": "v1.0.1",
    f"{CODELOAD}/zip/refs/tags/v1.0.1": "v1.0.1",
    f"{CODELOAD}/tar.gz/refs/tags/v1.0.1": "v1.0.1",
    f"{CODELOAD}/zip/v1.0.1": "v1.0.1",
    f"{ARCHIVE}/refs/heads/main.zip": "main",
    f"{ARCHIVE}/main.zip": "main",
    f"{ARCHIVE}/main.tar.gz": "main",
    f"{CODELOAD}/zip/refs/heads/main": "main",
    f"{ARCHIVE}/refs/heads/some/branch.zip": "some/branch",
    f"{ARCHIVE}/{'a' * 40}.zip": "a" * 40,
    "https://GitHub.com/Josh-G-S/QTE-SDK/archive/refs/tags/v1.0.1.zip": "v1.0.1",
    "http://github.com/josh-g-s/qte-sdk/archive/refs/tags/v1.0.1.zip": "v1.0.1",
    "https://someone:secret-password@github.com/josh-g-s/qte-sdk/archive/v1.0.1.zip": "v1.0.1",
}


@pytest.mark.parametrize("url", ARCHIVE_URLS)
def test_each_form_of_an_archive_of_the_repository_is_recognised(url: str):
    assert update._archive_revision(url) == ARCHIVE_URLS[url]


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/someone/qte-sdk/archive/refs/tags/v1.0.1.zip",
        "https://github.com/josh-g-s/qte-sdk-old/archive/refs/tags/v1.0.1.zip",
        "https://gitlab.com/josh-g-s/qte-sdk/archive/refs/tags/v1.0.1.zip",
        "https://example.com/josh-g-s/qte-sdk/archive/refs/tags/v1.0.1.zip",
        "https://codeload.github.com/someone/qte-sdk/zip/refs/tags/v1.0.1",
        "https://github.com/josh-g-s/qte-sdk/zip/refs/tags/v1.0.1",
        "https://codeload.github.com/josh-g-s/qte-sdk/archive/v1.0.1.zip",
        "https://github.com/josh-g-s/qte-sdk/archive/refs/tags/v1.0.1",
        "https://github.com/josh-g-s/qte-sdk/archive/refs/tags/v1.0.1.whl",
        "https://github.com/josh-g-s/qte-sdk/archive/refs/tags/.zip",
        "https://github.com/josh-g-s/qte-sdk/archive/.zip",
        "https://github.com/josh-g-s/qte-sdk/archive//v1.0.1.zip",
        "https://github.com/josh-g-s/qte-sdk/archive/v1.0.1.zip?x=1",
        "https://github.com:x/josh-g-s/qte-sdk/archive/v1.0.1.zip",
        "ftp://github.com/josh-g-s/qte-sdk/archive/v1.0.1.zip",
        "file:///home/someone/josh-g-s/qte-sdk/archive/v1.0.1.zip",
        "https://github.com/josh-g-s/qte-sdk/releases/download/v1.0.1/qte_sdk-1.0.1.whl",
        "https://github.com/josh-g-s/qte-sdk",
    ],
)
def test_an_archive_from_elsewhere_is_not_the_repositorys(url: str):
    assert update._archive_revision(url) is None


@pytest.mark.parametrize("url", ARCHIVE_URLS)
def test_an_archive_install_at_the_latest_release_is_current(
    monkeypatch: pytest.MonkeyPatch, url: str
):
    installed(monkeypatch, archive_install(url))
    repository = answer(monkeypatch, refs_with((tag_for(VERSION), RELEASE_COMMIT)))
    result = check_for_update()
    assert result.status is Status.CURRENT
    assert result.exit_code == 0
    assert result.command is None
    assert not result.main_ahead
    assert result.installed_commit is None
    assert result.installed_revision is None
    assert result.installed_archive == ARCHIVE_URLS[url]
    # An archive has no commit, so nothing is said about main.
    assert result.message == f"qte-sdk {VERSION} is the latest release, {tag_for(VERSION)}"
    assert len(repository.requests) == 1
    assert "secret-password" not in result.message


@pytest.mark.parametrize("url", ARCHIVE_URLS)
def test_an_archive_install_behind_a_release_is_told_the_latest_releases_archive(
    monkeypatch: pytest.MonkeyPatch, url: str
):
    newer = tag_for(bumped(VERSION, 1))
    installed(monkeypatch, archive_install(url))
    answer(monkeypatch, refs_with((tag_for(VERSION), RELEASE_COMMIT), (newer, MAIN)))
    result = check_for_update()
    assert result.status is Status.BEHIND
    assert result.exit_code == 1
    assert result.latest_release == newer
    assert result.command == archive_command(newer)
    assert result.command == update_command(newer, archive=True)
    assert result.message == behind_message(
        newer, f"pip install https://github.com/josh-g-s/qte-sdk/archive/refs/tags/{newer}.zip"
    )
    assert "secret-password" not in result.message


def test_an_archive_install_with_no_release_cannot_tell(monkeypatch: pytest.MonkeyPatch):
    installed(monkeypatch, archive_install(f"{ARCHIVE}/main.zip"))
    answer(monkeypatch, refs_with())
    result = check_for_update()
    assert result.status is Status.UNKNOWN
    assert result.command is None
    assert not result.main_ahead
    assert result.message.endswith("no release is tagged yet")


def test_an_archive_command_needs_a_release_tag():
    with pytest.raises(ValueError):
        update_command(archive=True)
    with pytest.raises(ValueError):
        update_command("v2.0.0", archive=True, reinstall=True)


def test_no_installed_package_cannot_tell(monkeypatch: pytest.MonkeyPatch):
    def missing(name: str) -> None:
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(update.metadata, "distribution", missing)
    result = check_for_update()
    assert result.status is Status.UNKNOWN
    assert "not installed as a package" in result.message


def test_an_unreadable_record_cannot_tell(monkeypatch: pytest.MonkeyPatch):
    class Unreadable(FakeDistribution):
        def read_text(self, name: str) -> str | None:
            raise PermissionError(13, "Permission denied", "/home/someone/direct_url.json")

    monkeypatch.setattr(update.metadata, "distribution", lambda name: Unreadable(None))
    result = check_for_update()
    assert result.status is Status.UNKNOWN
    assert "cannot be read" in result.message
    assert "someone" not in result.message


def test_a_package_of_another_version_than_the_one_imported_cannot_tell(
    monkeypatch: pytest.MonkeyPatch,
):
    installed(monkeypatch, UNPLACED["editable"][0], version="0.0.1")
    repository = answer(monkeypatch, refs_with((tag_for(bumped(VERSION, 0)), MAIN)))
    result = check_for_update()
    assert result.status is Status.UNKNOWN
    assert "not the same copy" in result.message
    assert repository.requests == []


def test_a_copy_that_is_not_the_one_pip_installed_cannot_tell_and_uses_no_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    # A local copy first on the path, with the same version as a git install beside it.
    other = tmp_path / "site-packages" / "qte_sdk" / "__init__.py"
    text = json.dumps(git_install())
    monkeypatch.setattr(
        update.metadata, "distribution", lambda name: FakeDistribution(text, package=str(other))
    )
    repository = answer(monkeypatch, refs_with((tag_for(bumped(VERSION, 0)), MAIN)))
    result = check_for_update()
    assert result.status is Status.UNKNOWN
    assert "not the one pip installed" in result.message
    assert repository.requests == []
    assert str(tmp_path) not in result.message


def test_an_editable_install_imported_from_its_source_folder_is_compared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    # pip records the source folder; the package's own files are not in site-packages.
    source = Path(qte_sdk.__file__).resolve().parent.parent
    record = json.dumps({"url": source.as_uri(), "dir_info": {"editable": True}})
    elsewhere = str(tmp_path / "site-packages" / "qte_sdk" / "__init__.py")
    monkeypatch.setattr(
        update.metadata, "distribution", lambda name: FakeDistribution(record, package=elsewhere)
    )
    newer = tag_for(bumped(VERSION, 2))
    answer(monkeypatch, refs_with((newer, MAIN)))
    result = check_for_update()
    assert result.status is Status.BEHIND
    assert result.command == COMMAND
    assert str(source) not in result.message


def test_an_editable_install_of_another_folder_is_not_the_one_imported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    record = json.dumps({"url": tmp_path.as_uri(), "dir_info": {"editable": True}})
    elsewhere = str(tmp_path / "site-packages" / "qte_sdk" / "__init__.py")
    monkeypatch.setattr(
        update.metadata, "distribution", lambda name: FakeDistribution(record, package=elsewhere)
    )
    repository = answer(monkeypatch, refs_with((tag_for(bumped(VERSION, 0)), MAIN)))
    result = check_for_update()
    assert result.status is Status.UNKNOWN
    assert "not the one pip installed" in result.message
    assert repository.requests == []


def test_a_version_that_is_not_a_release_number_cannot_tell(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(qte_sdk, "__version__", "1.0.0.dev1")
    installed(monkeypatch, git_install(), version="1.0.0.dev1")
    result = check_for_update()
    assert result.status is Status.UNKNOWN
    assert "not a release number" in result.message


# What it reads from the repository


def test_it_asks_for_the_list_of_references_in_protocol_v0_saying_only_its_version(
    monkeypatch: pytest.MonkeyPatch,
):
    installed(monkeypatch, git_install())
    repository = answer(monkeypatch, refs_with((tag_for(VERSION), INSTALLED), main=INSTALLED))
    check_for_update(timeout=2.5)
    [(request, timeout)] = repository.requests
    assert request.full_url == (
        "https://github.com/josh-g-s/qte-sdk.git/info/refs?service=git-upload-pack"
    )
    assert request.get_method() == "GET"
    assert dict(request.header_items()) == {"User-agent": f"qte-sdk/{VERSION}"}
    assert request.data is None
    assert timeout == 2.5


def test_an_annotated_tag_resolves_to_its_commit(monkeypatch: pytest.MonkeyPatch):
    installed(monkeypatch, git_install())
    body = advertisement(
        [
            (MAIN, "HEAD"),
            (MAIN, "refs/heads/main"),
            (TAG_OBJECT, f"refs/tags/{tag_for(VERSION)}"),
            (RELEASE_COMMIT, f"refs/tags/{tag_for(VERSION)}^{{}}"),
        ]
    )
    assert update._releases(update._parse_refs(body)) == {
        tag_for(VERSION): (tuple(int(n) for n in VERSION.split(".")), RELEASE_COMMIT)
    }


@pytest.mark.parametrize(
    "tags, latest",
    [
        (["v1.0.9", "v1.0.10"], "v1.0.10"),
        (["v1.0.10", "v1.0.9"], "v1.0.10"),
        (["v1.9.9", "v1.10.0"], "v1.10.0"),
        (["v9.9.9", "v10.0.0"], "v10.0.0"),
        (["v0.0.0", "v0.0.1"], "v0.0.1"),
    ],
)
def test_releases_are_compared_by_number(tags: list[str], latest: str):
    refs = update._parse_refs(refs_with(*((tag, MAIN) for tag in tags)))
    releases = update._releases(refs)
    assert max(releases, key=lambda tag: releases[tag][0]) == latest


@pytest.mark.parametrize(
    "tag",
    [
        "1.0.0",
        "v1.0",
        "v1.0.0.0",
        "v1.0.0-rc1",
        "v1.0.0rc1",
        "V9.0.0",
        "v01.0.0",
        "v1.00.0",
        "v９.0.0",  # a fullwidth digit
        "v9.0.0 ",
        "release-9",
        "latest",
    ],
)
def test_tags_that_are_not_release_numbers_are_ignored(tag: str):
    refs = update._parse_refs(refs_with((tag, MAIN), ("v1.0.0", MAIN)))
    assert set(update._releases(refs)) == {"v1.0.0"}


def test_branches_and_pull_requests_are_not_releases():
    body = advertisement(
        [
            (MAIN, "refs/heads/v9.0.0"),
            (MAIN, "refs/pull/1/head"),
            (MAIN, "refs/tags/nested/v9.0.0"),
            (MAIN, "refs/tags/v9.0.0^{}"),  # a peeled line with no tag line before it
        ]
    )
    assert update._releases(update._parse_refs(body)) == {}


def test_an_empty_repository_has_no_references():
    body = advertisement([("0" * 40, "capabilities^{}")])
    assert update._parse_refs(body) == {}


def test_a_sha256_repository_is_read():
    body = advertisement([("a" * 64, "refs/heads/main")])
    assert update._parse_refs(body) == {"refs/heads/main": "a" * 64}


SERVICE = pkt(b"# service=git-upload-pack\n") + b"0000"
GOOD_REF = pkt(f"{MAIN} refs/heads/main\n".encode())

UNREADABLE = {
    "empty": b"",
    "only the service line": SERVICE,
    "no service line": GOOD_REF + b"0000",
    "another service": pkt(b"# service=git-receive-pack\n") + b"0000" + GOOD_REF + b"0000",
    "no flush after the service line": pkt(b"# service=git-upload-pack\n") + GOOD_REF + b"0000",
    "no final flush": SERVICE + GOOD_REF,
    "something after the final flush": SERVICE + GOOD_REF + b"0000" + GOOD_REF + b"0000",
    "a truncated line": SERVICE + GOOD_REF[:-5],
    "a length that is not hex": SERVICE + b"zzzz" + GOOD_REF + b"0000",
    "a length too short": SERVICE + b"0003" + b"0000",
    "a delimiter": SERVICE + b"0001" + GOOD_REF + b"0000",
    "a short tail": SERVICE + GOOD_REF + b"00",
    "a short object id": SERVICE + pkt(b"abc123 refs/heads/main\n") + b"0000",
    "an upper-case object id": SERVICE + pkt(f"{'A' * 40} refs/heads/main\n".encode()) + b"0000",
    "no name": SERVICE + pkt(f"{MAIN}\n".encode()) + b"0000",
    "an empty name": SERVICE + pkt(f"{MAIN} \n".encode()) + b"0000",
    "protocol v2": pkt(b"version 2\n") + pkt(b"agent=git/github\n") + b"0000",
    "a web page": b"<!DOCTYPE html><html></html>",
}


@pytest.mark.parametrize("case", UNREADABLE)
def test_a_list_of_references_that_cannot_be_read_cannot_tell(
    monkeypatch: pytest.MonkeyPatch, case: str
):
    installed(monkeypatch, git_install())
    answer(monkeypatch, UNREADABLE[case])
    result = check_for_update()
    assert result.status is Status.UNKNOWN
    assert result.exit_code == 2
    assert result.installed_commit == INSTALLED


def test_a_list_with_no_release_cannot_tell_and_says_so(monkeypatch: pytest.MonkeyPatch):
    installed(monkeypatch, git_install())
    answer(monkeypatch, refs_with(("not-a-release", MAIN), main=INSTALLED))
    result = check_for_update()
    assert result.status is Status.UNKNOWN
    assert result.latest_release is None
    assert result.command is None
    assert result.message == (
        f"cannot tell whether qte-sdk {VERSION} is current: no release is tagged yet"
    )


def test_a_list_with_no_release_still_says_when_main_has_newer_commits(
    monkeypatch: pytest.MonkeyPatch,
):
    installed(monkeypatch, git_install())
    answer(monkeypatch, refs_with())
    result = check_for_update()
    assert result.status is Status.UNKNOWN
    assert result.main_ahead
    assert result.command == REINSTALL
    assert "no release is tagged yet; main has newer commits" in result.message


def test_an_oversize_answer_cannot_tell(monkeypatch: pytest.MonkeyPatch):
    installed(monkeypatch, git_install())
    many = [(MAIN, f"refs/pull/{n}/head") for n in range(30_000)]
    body = advertisement([(INSTALLED, f"refs/tags/{tag_for(VERSION)}"), *many])
    assert len(body) > update._MAX_REFS_SIZE
    answer(monkeypatch, body)
    result = check_for_update()
    assert result.status is Status.UNKNOWN
    assert "longer list" in result.message


def test_an_answer_that_is_not_a_git_list_cannot_tell(monkeypatch: pytest.MonkeyPatch):
    installed(monkeypatch, git_install())
    answer(monkeypatch, refs_with((tag_for(VERSION), INSTALLED)), content_type="text/html")
    result = check_for_update()
    assert result.status is Status.UNKNOWN
    assert "did not answer as a git repository" in result.message


def test_a_deeply_nested_record_cannot_tell(monkeypatch: pytest.MonkeyPatch):
    installed(monkeypatch, "[" * 100_000)
    result = check_for_update()
    assert result.status is Status.UNKNOWN
    assert "cannot be read" in result.message


def test_a_recorded_version_that_is_not_a_release_number_is_not_shown(
    monkeypatch: pytest.MonkeyPatch,
):
    installed(monkeypatch, git_install(), version="/home/someone/secret")
    result = check_for_update()
    assert result.status is Status.UNKNOWN
    assert "another version" in result.message
    assert "someone" not in result.message


def test_a_release_number_too_long_to_read_is_ignored(monkeypatch: pytest.MonkeyPatch):
    installed(monkeypatch, git_install())
    answer(monkeypatch, refs_with(("v" + "9" * 5000 + ".0.0", MAIN), main=INSTALLED))
    result = check_for_update()
    assert result.status is Status.UNKNOWN
    assert result.message.endswith("no release is tagged yet")


def test_an_unexpected_failure_cannot_tell_and_shows_only_its_kind(
    monkeypatch: pytest.MonkeyPatch,
):
    installed(monkeypatch, git_install())

    def broken(*args: object) -> None:
        raise LookupError("/home/someone/private")

    monkeypatch.setattr(update, "_parse_refs", broken)
    answer(monkeypatch, b"")
    result = check_for_update()
    assert result.status is Status.UNKNOWN
    assert result.message.endswith("the check failed (LookupError)")
    assert result.installed_commit == INSTALLED


class SlowResponse(FakeResponse):
    """Sends its answer a byte at a time, each a second after the last."""

    def __init__(self, body: bytes, url: str, clock: list[float]) -> None:
        super().__init__(body, url)
        self.clock = clock

    def read1(self, size: int = -1) -> bytes:
        self.clock[0] += 1.0
        return super().read1(1)


def test_a_slow_answer_is_cut_off_at_the_timeout(monkeypatch: pytest.MonkeyPatch):
    installed(monkeypatch, git_install())
    clock = [0.0]
    monkeypatch.setattr(update.time, "monotonic", lambda: clock[0])
    body = refs_with((tag_for(VERSION), INSTALLED))
    monkeypatch.setattr(
        update, "_open", lambda request, timeout: SlowResponse(body, update._REFS_URL, clock)
    )
    result = check_for_update(timeout=5)
    assert result.status is Status.UNKNOWN
    assert "did not answer within 5 s" in result.message
    assert clock[0] <= 6


def test_an_answer_that_never_comes_is_not_waited_for(monkeypatch: pytest.MonkeyPatch):
    installed(monkeypatch, git_install())
    release = threading.Event()

    def hanging(request: object, timeout: float) -> None:
        release.wait(30)  # a lookup or a connection that outlasts the timeout
        raise TimeoutError

    monkeypatch.setattr(update, "_open", hanging)
    started = time.monotonic()
    result = check_for_update(timeout=0.2)
    release.set()
    assert time.monotonic() - started < 5
    assert result.status is Status.UNKNOWN
    assert result.message.endswith("did not answer within 0.2 s")


def test_a_redirect_elsewhere_cannot_tell(monkeypatch: pytest.MonkeyPatch):
    installed(monkeypatch, git_install())
    answer(
        monkeypatch,
        refs_with((tag_for(VERSION), INSTALLED)),
        url="https://example.com/josh-g-s/qte-sdk.git/info/refs?service=git-upload-pack",
    )
    result = check_for_update()
    assert result.status is Status.UNKNOWN
    assert "example.com" not in result.message


@pytest.mark.parametrize(
    "error, reason",
    [
        (urllib.error.URLError(OSError("nodename nor servname provided")), "(URLError)"),
        (TimeoutError("timed out"), "did not answer within 5 s"),
        (ConnectionResetError(54, "reset"), "(ConnectionResetError)"),
        (
            urllib.error.HTTPError(
                "https://github.com/x", 429, "Too Many Requests", email.message.Message(), None
            ),
            "answered HTTP 429",
        ),
        (
            urllib.error.URLError("Tunnel connection failed: http://user:pw@proxy.invalid"),
            "(URLError)",
        ),
        (ValueError("anything else"), "(ValueError)"),
        (
            urllib.error.HTTPError(
                "https://github.com/x", 301, "Moved", email.message.Message(), None
            ),
            "sent it elsewhere",
        ),
    ],
)
def test_an_unreachable_repository_cannot_tell(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, reason: str
):
    installed(monkeypatch, git_install())
    answer(monkeypatch, error=error)
    result = check_for_update()
    assert result.status is Status.UNKNOWN
    assert result.exit_code == 2
    assert reason in result.message
    assert "pw@" not in result.message


# What it says


def test_an_install_behind_a_release_is_told_the_command(monkeypatch: pytest.MonkeyPatch):
    newer = tag_for(bumped(VERSION, 2))
    installed(monkeypatch, git_install())
    answer(monkeypatch, refs_with((tag_for(VERSION), INSTALLED), (newer, MAIN)))
    result = check_for_update()
    assert result.status is Status.BEHIND
    assert result.exit_code == 1
    assert result.latest_release == newer
    assert result.command == COMMAND
    assert result.message == behind_message(newer, COMMAND)
    assert result.code == "QTE-UPDATE-AVAILABLE"
    assert not result.recommended
    assert result.why is None


def test_an_install_pinned_to_a_release_is_pointed_at_the_latest_release(
    monkeypatch: pytest.MonkeyPatch,
):
    newer = tag_for(bumped(VERSION, 1))
    installed(monkeypatch, git_install(tag_for(VERSION)))
    answer(monkeypatch, refs_with((tag_for(VERSION), INSTALLED), (newer, MAIN)))
    result = check_for_update()
    assert result.status is Status.BEHIND
    assert result.command == (f'pip install --upgrade "git+{REPOSITORY_URL}@{newer}"')
    assert result.command == update_command(newer)
    assert not result.main_ahead


def test_the_command_reinstalls_only_for_newer_commits_at_the_same_version():
    assert update_command() == COMMAND
    assert update_command(reinstall=True) == REINSTALL
    assert update_command("v2.0.0", reinstall=True) == (
        f'pip install --upgrade --force-reinstall --no-deps "git+{REPOSITORY_URL}@v2.0.0"'
    )


@pytest.mark.parametrize("revision", [INSTALLED, "some-branch"])
def test_an_install_pinned_elsewhere_is_pointed_at_main(
    monkeypatch: pytest.MonkeyPatch, revision: str
):
    installed(monkeypatch, git_install(revision))
    answer(monkeypatch, refs_with((tag_for(bumped(VERSION, 0)), MAIN)))
    result = check_for_update()
    assert result.status is Status.BEHIND
    assert result.command == COMMAND


@pytest.mark.parametrize("revision", [None, "main"])
def test_a_current_install_that_follows_main_is_told_of_newer_commits(
    monkeypatch: pytest.MonkeyPatch, revision: str | None
):
    installed(monkeypatch, git_install(revision))
    answer(monkeypatch, refs_with((tag_for(VERSION), INSTALLED)))
    result = check_for_update()
    assert result.status is Status.CURRENT
    assert result.exit_code == 0
    assert result.main_commit == MAIN
    assert result.main_ahead
    assert result.command == REINSTALL
    assert result.message == (
        f"qte-sdk {VERSION} is the latest release, {tag_for(VERSION)}; main has newer "
        f"commits than the one installed ({INSTALLED[:12]}): take them with {REINSTALL}"
    )


def test_a_current_install_at_mains_tip_is_told_so(monkeypatch: pytest.MonkeyPatch):
    installed(monkeypatch, git_install())
    answer(monkeypatch, refs_with((tag_for(VERSION), INSTALLED), main=INSTALLED))
    result = check_for_update()
    assert result.status is Status.CURRENT
    assert not result.main_ahead
    assert result.command is None
    assert result.message == (
        f"qte-sdk {VERSION} is the latest release, {tag_for(VERSION)}, and main has no "
        "newer commits"
    )


@pytest.mark.parametrize("revision", [tag_for(VERSION), INSTALLED, "some-branch"])
def test_a_current_pinned_install_is_not_told_of_mains_commits(
    monkeypatch: pytest.MonkeyPatch, revision: str
):
    installed(monkeypatch, git_install(revision))
    answer(monkeypatch, refs_with((tag_for(VERSION), INSTALLED)))
    result = check_for_update()
    assert result.status is Status.CURRENT
    assert not result.main_ahead
    assert result.command is None
    assert result.message == f"qte-sdk {VERSION} is the latest release, {tag_for(VERSION)}"


def test_an_install_newer_than_the_latest_release_is_current(monkeypatch: pytest.MonkeyPatch):
    major, minor, patch = (int(n) for n in VERSION.split("."))
    if (major, minor, patch) == (0, 0, 0):
        pytest.skip("no release is older than 0.0.0")
    older = "v0.0.0"
    installed(monkeypatch, git_install())
    answer(monkeypatch, refs_with((older, RELEASE_COMMIT), main=INSTALLED))
    result = check_for_update()
    assert result.status is Status.CURRENT
    assert result.message.startswith(f"qte-sdk {VERSION} is newer than the latest release, v0.0.0")


def test_the_result_cannot_be_changed(monkeypatch: pytest.MonkeyPatch):
    installed(monkeypatch, None)
    result = check_for_update()
    with pytest.raises(AttributeError):
        result.status = Status.CURRENT  # type: ignore[misc]


# The command


def test_the_command_prints_the_install_and_the_result_and_exits_with_its_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    newer = tag_for(bumped(VERSION, 2))
    installed(monkeypatch, git_install())
    answer(monkeypatch, refs_with((newer, MAIN)))
    assert update.main([]) == 1
    out, err = capsys.readouterr()
    assert out.splitlines() == [
        f"installed: qte-sdk {VERSION}, commit {INSTALLED[:12]}, following main",
        behind_message(newer, COMMAND),
    ]
    assert err == ""


def test_the_command_exits_0_when_current(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    installed(monkeypatch, git_install(tag_for(VERSION)))
    answer(monkeypatch, refs_with((tag_for(VERSION), INSTALLED)))
    assert update.main([]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first == f"installed: qte-sdk {VERSION}, commit {INSTALLED[:12]}, pinned to v{VERSION}"


def test_the_command_exits_2_when_it_cannot_tell(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    installed(monkeypatch, {"url": "file:///home/someone/qte-sdk", "dir_info": {"editable": True}})
    answer(monkeypatch, refs_with((tag_for(VERSION), MAIN)))
    assert update.main([]) == 2
    out = capsys.readouterr().out
    assert out.splitlines()[0] == f"installed: qte-sdk {VERSION}"
    assert "someone" not in out


@pytest.mark.parametrize(
    "url, described",
    [
        (f"{ARCHIVE}/refs/tags/v1.0.1.zip", "from the v1.0.1 release archive"),
        (f"{CODELOAD}/tar.gz/refs/tags/v1.0.1", "from the v1.0.1 release archive"),
        (f"{ARCHIVE}/main.zip", "from an archive of main"),
        (f"{ARCHIVE}/refs/heads/x%1b%5b2Jy.zip", "from an archive of another revision"),
        (f"{ARCHIVE}/refs/heads/some/branch.zip", "from an archive of another revision"),
    ],
)
def test_the_command_says_which_archive_was_installed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], url: str, described: str
):
    newer = tag_for(bumped(VERSION, 2))
    installed(monkeypatch, archive_install(url))
    answer(monkeypatch, refs_with((newer, MAIN)))
    assert update.main([]) == 1
    assert capsys.readouterr().out.splitlines() == [
        f"installed: qte-sdk {VERSION}, {described}",
        behind_message(newer, archive_command(newer)),
    ]


def test_the_command_exits_1_for_an_editable_install_behind_a_release(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    newer = tag_for(bumped(VERSION, 2))
    installed(monkeypatch, UNPLACED["editable"][0])
    answer(monkeypatch, refs_with((newer, MAIN)))
    assert update.main([]) == 1
    out = capsys.readouterr().out
    assert out.splitlines()[0] == f"installed: qte-sdk {VERSION}"
    assert "someone" not in out


def test_the_command_never_shows_an_odd_revision(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    installed(monkeypatch, git_install("x\x1b[2Jy"))
    answer(monkeypatch, refs_with((tag_for(VERSION), INSTALLED)))
    update.main([])
    assert "\x1b" not in capsys.readouterr().out


def test_the_command_passes_its_timeout(monkeypatch: pytest.MonkeyPatch):
    installed(monkeypatch, git_install())
    repository = answer(monkeypatch, refs_with((tag_for(VERSION), INSTALLED)))
    update.main(["--timeout", "1.5"])
    assert repository.requests[0][1] == 1.5


@pytest.mark.parametrize("value", ["0", "-1", "61", "nan", "inf", "soon"])
def test_the_command_refuses_a_timeout_out_of_range(value: str, capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as stopped:
        update.main(["--timeout", value])
    assert stopped.value.code == 2
    assert "give a number of seconds above 0 and at most 60" in capsys.readouterr().err


def test_the_command_runs_as_a_module():
    # An argument error stops before anything is read, so this uses no network.
    done = subprocess.run(
        [sys.executable, "-m", "qte_sdk.update", "--timeout", "0"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 2
    assert "usage: python -m qte_sdk.update" in done.stderr


# Importing


NO_NETWORK_IMPORT = """
import socket, sys, urllib.request

def refuse(*args, **kwargs):
    raise SystemExit("network used: " + repr(args)[:80])

socket.socket.connect = refuse
socket.socket.connect_ex = refuse
socket.create_connection = refuse
socket.getaddrinfo = refuse
urllib.request.urlopen = refuse

import importlib, pkgutil
import qte_sdk

names = [m.name for m in pkgutil.walk_packages(qte_sdk.__path__, "qte_sdk.")]
assert "qte_sdk.update" in names, names
for name in names:
    importlib.import_module(name)
print("imported", len(names))
"""


def test_importing_qte_sdk_and_every_module_uses_no_network():
    done = subprocess.run(
        [sys.executable, "-c", NO_NETWORK_IMPORT], capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.startswith("imported ")


# Redirects


class Redirecting(http.server.BaseHTTPRequestHandler):
    """Answers every request with a redirect to `target`, and counts the requests."""

    target = ""
    requests: list[str] = []

    def do_GET(self) -> None:
        self.requests.append(self.path)
        self.send_response(302)
        self.send_header("Location", self.target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def local_server() -> Any:
    servers = []

    def serve(handler: type[http.server.BaseHTTPRequestHandler]) -> str:
        server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}/"

    yield serve
    for server in servers:
        server.shutdown()
        server.server_close()


def test_a_redirect_is_never_followed(local_server: Any):
    class Target(Redirecting):
        requests: list[str] = []

    class Origin(Redirecting):
        requests: list[str] = []

    Origin.target = local_server(Target)
    origin = local_server(Origin)
    with pytest.raises(urllib.error.HTTPError) as raised:
        ORIGINAL_OPEN(urllib.request.Request(origin), 5)
    assert raised.value.code == 302
    assert Origin.requests == ["/"]
    assert Target.requests == []


# Recommended updates: releases.json

ROOT = Path(__file__).resolve().parent.parent
ORIGINAL_CACHE_DIR = update._cache_dir


def releases_json(*entries: dict[str, Any]) -> bytes:
    return json.dumps(list(entries)).encode()


def entry(
    version: str,
    recommended: bool = True,
    why: str = "it fixes something serious",
    platforms: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "version": version,
        "recommended": recommended,
        "why": why,
        "platforms": [] if platforms is None else platforms,
        **extra,
    }


def test_the_repositorys_releases_json_lists_every_release_in_the_expected_form():
    data = (ROOT / "releases.json").read_bytes()
    raw = json.loads(data)
    parsed = update._parse_releases(data)
    # Every entry is read, and none is changed by making it plain text.
    assert len(parsed) == len(raw)
    for item in raw:
        number = tuple(int(n) for n in item["version"].split("."))
        assert parsed[number].why == item["why"]
    # Every release tagged so far, and the release in progress.
    assert {(1, 0, 0), (1, 0, 1), (1, 0, 2), (1, 1, 0), (1, 1, 1)} <= set(parsed)
    windows_fix = parsed[(1, 1, 1)]
    assert windows_fix.recommended
    assert windows_fix.platforms == ("win32",)
    assert windows_fix.why.endswith("1.1.1 ends it at once")
    # The current version has an entry before it is released.
    assert tuple(int(n) for n in VERSION.split(".")) in parsed


def test_valid_entries_are_read_and_unknown_keys_ignored():
    data = releases_json(
        entry("1.0.0", recommended=False, why=""),
        entry("1.1.1", platforms=["win32", "linux"], severity="high", notes={"a": 1}),
    )
    assert update._parse_releases(data) == {
        (1, 0, 0): update._Release((1, 0, 0), False, "", ()),
        (1, 1, 1): update._Release(
            (1, 1, 1), True, "it fixes something serious", ("win32", "linux")
        ),
    }


INVALID_ENTRIES = {
    "not an object": "1.1.1",
    "no version": {k: v for k, v in entry("1.1.1").items() if k != "version"},
    "a version with a v": entry("v1.1.1"),
    "a short version": entry("1.1"),
    "a version with a suffix": entry("1.1.1rc1"),
    "a version with a leading zero": entry("1.01.1"),
    "a version that is a number": entry(1.1),  # type: ignore[arg-type]
    "no recommended": {k: v for k, v in entry("1.1.1").items() if k != "recommended"},
    "recommended as text": entry("1.1.1", recommended="true"),  # type: ignore[arg-type]
    "recommended as a number": entry("1.1.1", recommended=1),  # type: ignore[arg-type]
    "no why": {k: v for k, v in entry("1.1.1").items() if k != "why"},
    "a why that is not text": entry("1.1.1", why=["a", "b"]),  # type: ignore[arg-type]
    "recommended with an empty why": entry("1.1.1", why=" \x1b​. "),
    "no platforms": {k: v for k, v in entry("1.1.1").items() if k != "platforms"},
    "platforms as text": {**entry("1.1.1"), "platforms": "win32"},
    "a platform that is not text": entry("1.1.1", platforms=[32]),  # type: ignore[list-item]
    "an upper-case platform": entry("1.1.1", platforms=["Win32"]),
    "an empty platform": entry("1.1.1", platforms=[""]),
    "a platform with an escape": entry("1.1.1", platforms=["win32\x1b[2J"]),
}


@pytest.mark.parametrize("case", INVALID_ENTRIES)
def test_an_entry_not_in_the_expected_form_is_ignored_and_the_rest_are_read(case: str):
    data = json.dumps([INVALID_ENTRIES[case], entry("1.0.2", recommended=False)]).encode()
    assert set(update._parse_releases(data)) == {(1, 0, 2)}


INVALID_FILES = {
    "empty": b"",
    "not json": b"{not json",
    "an object": json.dumps({"releases": [entry("1.1.1")]}).encode(),
    "text": b'"1.1.1"',
    "not utf-8": b'[{"version": "1.1.1", "why": "\xff"}]',
    "deeply nested": b"[" * 100_000,
    "a web page": b"<!DOCTYPE html><html></html>",
    "oversize": releases_json(entry("1.1.1", why="x" * (update._MAX_RELEASES_SIZE + 1))),
}


@pytest.mark.parametrize("case", INVALID_FILES)
def test_a_file_not_in_the_expected_form_gives_no_entries(case: str):
    assert update._parse_releases(INVALID_FILES[case]) == {}


def test_a_version_listed_twice_is_ignored():
    data = releases_json(entry("1.1.1"), entry("1.1.1", recommended=False), entry("1.1.0"))
    assert set(update._parse_releases(data)) == {(1, 1, 0)}


@pytest.mark.parametrize(
    "why, shown",
    [
        ("plain words", "plain words"),
        ("ends with a full stop.", "ends with a full stop"),
        ("two\nlines\r\nand\ta tab", "two lines and a tab"),
        ("an \x1b[2Jescape and a bell\x07", "an [2Jescape and a bell"),
        ("a \x9b31m C1 control", "a 31m C1 control"),
        ("right-to-left ‮override‬", "right-to-left override"),
        ("zero​width and line separator", "zerowidth and line separator"),
        ("  spaced   out  ", "spaced out"),
    ],
)
def test_the_reason_is_plain_text_on_one_line(why: str, shown: str):
    [release] = update._parse_releases(releases_json(entry("1.1.1", why=why))).values()
    assert release.why == shown


def test_a_long_reason_is_cut_short():
    [release] = update._parse_releases(releases_json(entry("1.1.1", why="word " * 200))).values()
    assert len(release.why) == update._MAX_WHY
    assert release.why.endswith("...")


# Recommended updates: what the check says

WHY = "on Windows, a fetch could freeze the program"


def behind_with(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    *entries: dict[str, Any],
    tags: tuple[str, ...] = (),
    releases: bytes | None = None,
    **kwargs: Any,
) -> tuple[update.UpdateCheck, Repository, str]:
    """Check a git install of VERSION on `platform`, with release tags VERSION and the next
    patch (and `tags`), and releases.json holding `entries` (or `releases`)."""
    newer = tag_for(bumped(VERSION, 2))
    monkeypatch.setattr(sys, "platform", platform)
    installed(monkeypatch, git_install())
    refs = refs_with((tag_for(VERSION), INSTALLED), (newer, MAIN), *((tag, MAIN) for tag in tags))
    data = releases_json(*entries) if releases is None else releases
    repository = answer(monkeypatch, refs, releases=data, **kwargs)
    return check_for_update(), repository, newer


@pytest.mark.parametrize("platform, name", [("win32", "Windows"), ("linux", "Linux")])
def test_a_release_recommended_on_this_platform_says_so_with_its_reason(
    monkeypatch: pytest.MonkeyPatch, platform: str, name: str
):
    newer_version = bumped(VERSION, 2)
    result, repository, newer = behind_with(
        monkeypatch, platform, entry(newer_version, why=WHY, platforms=["win32", "linux"])
    )
    assert result.status is Status.BEHIND
    assert result.exit_code == 1
    assert result.recommended
    assert result.why == WHY
    assert result.code == "QTE-UPDATE-AVAILABLE"
    assert result.message == behind_message(
        newer, COMMAND, f", a recommended update on {name}: {WHY}"
    )
    assert len(repository.requests) == 1
    assert len(repository.release_requests) == 1


def test_a_release_recommended_on_every_platform_names_none(monkeypatch: pytest.MonkeyPatch):
    result, _, newer = behind_with(monkeypatch, "darwin", entry(bumped(VERSION, 2), why=WHY))
    assert result.recommended
    assert result.message == behind_message(newer, COMMAND, f", a recommended update: {WHY}")


def test_a_release_recommended_on_another_platform_is_an_ordinary_update(
    monkeypatch: pytest.MonkeyPatch,
):
    result, _, newer = behind_with(
        monkeypatch, "darwin", entry(bumped(VERSION, 2), why=WHY, platforms=["win32"])
    )
    assert result.status is Status.BEHIND
    assert not result.recommended
    assert result.why is None
    assert result.message == behind_message(newer, COMMAND)
    assert "recommended" not in result.message


def test_a_release_not_marked_recommended_is_an_ordinary_update(
    monkeypatch: pytest.MonkeyPatch,
):
    result, _, newer = behind_with(
        monkeypatch, "win32", entry(bumped(VERSION, 2), recommended=False, why=WHY)
    )
    assert result.status is Status.BEHIND
    assert not result.recommended
    assert result.message == behind_message(newer, COMMAND)


def test_a_recommended_release_between_the_installed_one_and_the_latest_counts(
    monkeypatch: pytest.MonkeyPatch,
):
    middle, latest = bumped(VERSION, 2), bumped(bumped(VERSION, 2), 2)
    result, _, newer = behind_with(
        monkeypatch,
        "win32",
        entry(middle, why="the older reason"),
        entry(latest, recommended=False),
        tags=(tag_for(latest),),
    )
    assert newer == tag_for(middle)
    assert result.latest_release == tag_for(latest)
    assert result.recommended
    assert result.message == behind_message(
        tag_for(latest), COMMAND, ", a recommended update: the older reason"
    )


def test_the_highest_recommended_release_gives_the_reason(monkeypatch: pytest.MonkeyPatch):
    middle, latest = bumped(VERSION, 2), bumped(bumped(VERSION, 2), 2)
    result, _, _ = behind_with(
        monkeypatch,
        "win32",
        entry(middle, why="the older reason"),
        entry(latest, why="the newer reason"),
        tags=(tag_for(latest),),
    )
    assert result.why == "the newer reason"


@pytest.mark.parametrize(
    "version",
    [
        VERSION,  # the installed one
        "0.0.0",  # older
        "999.0.0",  # newer than any release tag: not released yet
    ],
)
def test_a_recommended_release_not_above_the_installed_one_or_not_tagged_is_ignored(
    monkeypatch: pytest.MonkeyPatch, version: str
):
    result, _, newer = behind_with(monkeypatch, "win32", entry(version, why=WHY))
    assert result.status is Status.BEHIND
    assert result.latest_release == newer
    assert not result.recommended
    assert result.message == behind_message(newer, COMMAND)


def test_releases_json_never_changes_the_latest_release_or_makes_a_current_install_behind(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(sys, "platform", "win32")
    installed(monkeypatch, git_install(tag_for(VERSION)))
    repository = answer(
        monkeypatch,
        refs_with((tag_for(VERSION), INSTALLED)),
        releases=releases_json(entry("999.0.0", why=WHY)),
    )
    result = check_for_update()
    assert result.status is Status.CURRENT
    assert not result.recommended
    # It is read only when behind a release.
    assert repository.release_requests == []


RELEASES_FAILURES = {
    "missing": {},
    "unreachable": {"releases_error": urllib.error.URLError("http://user:pw@proxy.invalid")},
    "timed out": {"releases_error": TimeoutError("timed out")},
    "a server error": {
        "releases_error": urllib.error.HTTPError(
            update.RELEASES_URL, 500, "Server Error", email.message.Message(), None
        )
    },
    "a redirect": {
        "releases_error": urllib.error.HTTPError(
            update.RELEASES_URL, 302, "Found", email.message.Message(), None
        )
    },
    "unreadable": {"releases": b"<html>"},
    "oversize": {"releases": b"[" + b" " * (update._MAX_RELEASES_SIZE + 10) + b"]"},
    "an unexpected error": {"releases_error": LookupError("/home/someone/private")},
}


@pytest.mark.parametrize("case", RELEASES_FAILURES)
def test_when_releases_json_cannot_be_used_the_check_is_as_before(
    monkeypatch: pytest.MonkeyPatch, case: str
):
    options = dict(RELEASES_FAILURES[case])
    releases = options.pop("releases", None)
    newer = tag_for(bumped(VERSION, 2))
    monkeypatch.setattr(sys, "platform", "win32")
    installed(monkeypatch, git_install())
    repository = answer(
        monkeypatch,
        refs_with((tag_for(VERSION), INSTALLED), (newer, MAIN)),
        releases=releases,
        **options,
    )
    result = check_for_update()
    assert result.status is Status.BEHIND
    assert result.exit_code == 1
    assert result.latest_release == newer
    assert result.command == COMMAND
    assert not result.recommended
    assert result.message == behind_message(newer, COMMAND)
    assert len(repository.release_requests) == 1
    assert "pw@" not in result.message
    assert "someone" not in result.message


def test_releases_json_sent_elsewhere_is_not_used(monkeypatch: pytest.MonkeyPatch):
    newer = tag_for(bumped(VERSION, 2))
    monkeypatch.setattr(sys, "platform", "win32")
    installed(monkeypatch, git_install())
    body = refs_with((tag_for(VERSION), INSTALLED), (newer, MAIN))
    data = releases_json(entry(bumped(VERSION, 2), why=WHY))

    def elsewhere(request: urllib.request.Request, timeout: float) -> FakeResponse:
        if request.full_url == update.RELEASES_URL:
            return FakeResponse(data, "https://example.com/releases.json", "text/plain")
        return FakeResponse(body, request.full_url)

    monkeypatch.setattr(update, "_open", elsewhere)
    result = check_for_update()
    assert result.status is Status.BEHIND
    assert not result.recommended


def test_releases_json_is_asked_for_saying_only_the_sdks_version_within_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
):
    _, repository, _ = behind_with(monkeypatch, "win32", entry(bumped(VERSION, 2)))
    [(request, timeout)] = repository.release_requests
    assert request.full_url == (
        "https://raw.githubusercontent.com/josh-g-s/qte-sdk/main/releases.json"
    )
    assert request.get_method() == "GET"
    assert dict(request.header_items()) == {"User-agent": f"qte-sdk/{VERSION}"}
    assert request.data is None
    # What is left of the check's own timeout.
    assert 0 < timeout <= update.DEFAULT_TIMEOUT


def test_a_releases_json_that_never_comes_is_not_waited_for_beyond_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
):
    newer = tag_for(bumped(VERSION, 2))
    installed(monkeypatch, git_install())
    body = refs_with((tag_for(VERSION), INSTALLED), (newer, MAIN))
    release = threading.Event()

    def hanging(request: urllib.request.Request, timeout: float) -> FakeResponse:
        if request.full_url == update.RELEASES_URL:
            release.wait(30)
            raise TimeoutError
        return FakeResponse(body, request.full_url)

    monkeypatch.setattr(update, "_open", hanging)
    started = time.monotonic()
    result = check_for_update(timeout=0.5)
    release.set()
    assert time.monotonic() - started < 5
    assert result.status is Status.BEHIND
    assert result.message == behind_message(newer, COMMAND)


def test_an_install_it_cannot_place_behind_a_recommended_release_is_told_so(
    monkeypatch: pytest.MonkeyPatch,
):
    newer = tag_for(bumped(VERSION, 2))
    monkeypatch.setattr(sys, "platform", "win32")
    installed(monkeypatch, UNPLACED["wheel"][0])
    answer(
        monkeypatch,
        refs_with((tag_for(VERSION), RELEASE_COMMIT), (newer, MAIN)),
        releases=releases_json(entry(bumped(VERSION, 2), why=WHY, platforms=["win32"])),
    )
    result = check_for_update()
    assert result.status is Status.BEHIND
    assert result.recommended
    assert result.message == behind_message(
        newer, archive_command(newer), f", a recommended update on Windows: {WHY}"
    )


def test_the_command_says_when_an_update_is_recommended_and_why(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    newer = tag_for(bumped(VERSION, 2))
    monkeypatch.setattr(sys, "platform", "win32")
    installed(monkeypatch, git_install())
    answer(
        monkeypatch,
        refs_with((newer, MAIN)),
        releases=releases_json(entry(bumped(VERSION, 2), why=f"{WHY}\x1b[2J‮", platforms=["win32"])),
    )
    assert update.main([]) == 1
    out, err = capsys.readouterr()
    assert out.splitlines() == [
        f"installed: qte-sdk {VERSION}, commit {INSTALLED[:12]}, following main",
        behind_message(newer, COMMAND, f", a recommended update on Windows: {WHY}[2J"),
    ]
    assert "\x1b" not in out
    assert "‮" not in out
    assert err == ""


# The automatic check


@pytest.fixture
def automatic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Turn the automatic check back on, as it is outside the tests, with its cache folder
    in `tmp_path` (as the conftest sets it). Returns the file that records the last check."""
    monkeypatch.delenv(update.UPDATE_CHECK_ENV_VAR, raising=False)
    folder = update._cache_dir()
    assert folder is not None and folder.is_relative_to(tmp_path)
    return folder / "update-check"


def run_in_background() -> threading.Thread | None:
    """Start the automatic check as `open_session` does, and wait for it to end."""
    thread = update.check_in_background()
    if thread is not None:
        thread.join(30)
        assert not thread.is_alive()
    return thread


def new_program(monkeypatch: pytest.MonkeyPatch) -> None:
    """As if another program started, on the same computer."""
    monkeypatch.setattr(update, "_automatic_done", False)


def behind_a_release(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> Repository:
    newer = tag_for(bumped(VERSION, 2))
    installed(monkeypatch, git_install())
    return answer(monkeypatch, refs_with((tag_for(VERSION), INSTALLED), (newer, MAIN)), **kwargs)


def update_records(caplog: pytest.LogCaptureFixture) -> list[Any]:
    return [r for r in caplog.records if r.name.startswith("qte_sdk")]


def test_the_automatic_check_logs_a_warning_with_the_code_when_behind(
    monkeypatch: pytest.MonkeyPatch,
    automatic: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
):
    newer = tag_for(bumped(VERSION, 2))
    monkeypatch.setattr(sys, "platform", "win32")
    repository = behind_a_release(
        monkeypatch,
        releases=releases_json(entry(bumped(VERSION, 2), why=WHY, platforms=["win32"])),
    )
    caplog.set_level(logging.DEBUG)
    assert run_in_background() is not None
    [record] = update_records(caplog)
    assert record.name == "qte_sdk.update"
    assert record.levelno == logging.WARNING
    assert record.code == "QTE-UPDATE-AVAILABLE"
    assert record.getMessage() == behind_message(
        newer, COMMAND, f", a recommended update on Windows: {WHY}"
    )
    assert len(repository.requests) == 1
    assert len(repository.release_requests) == 1
    # It never prints.
    assert capsys.readouterr() == ("", "")


def test_the_automatic_check_logs_an_ordinary_update_without_recommended(
    monkeypatch: pytest.MonkeyPatch, automatic: Path, caplog: pytest.LogCaptureFixture
):
    newer = tag_for(bumped(VERSION, 2))
    behind_a_release(monkeypatch)
    caplog.set_level(logging.DEBUG)
    run_in_background()
    [record] = update_records(caplog)
    assert record.levelno == logging.WARNING
    assert record.getMessage() == behind_message(newer, COMMAND)


SILENT = {
    "current": lambda m: (
        installed(m, git_install(tag_for(VERSION))),
        answer(m, refs_with((tag_for(VERSION), INSTALLED))),
    ),
    "main ahead": lambda m: (
        installed(m, git_install()),
        answer(m, refs_with((tag_for(VERSION), INSTALLED))),
    ),
    "cannot tell": lambda m: (installed(m, "{not json"),),
    "no release": lambda m: (installed(m, git_install()), answer(m, refs_with())),
    "network down": lambda m: (
        installed(m, git_install()),
        answer(m, error=urllib.error.URLError(OSError("nodename nor servname provided"))),
    ),
    "github error": lambda m: (
        installed(m, git_install()),
        answer(
            m,
            error=urllib.error.HTTPError(
                "https://github.com/x", 503, "Unavailable", email.message.Message(), None
            ),
        ),
    ),
}


@pytest.mark.parametrize("case", SILENT)
def test_the_automatic_check_is_silent_unless_behind(
    monkeypatch: pytest.MonkeyPatch,
    automatic: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    case: str,
):
    SILENT[case](monkeypatch)
    caplog.set_level(logging.DEBUG)
    assert run_in_background() is not None
    assert update_records(caplog) == []
    assert capsys.readouterr() == ("", "")
    # The check was still counted.
    assert automatic.exists()


def test_the_automatic_check_runs_at_most_once_a_day_on_a_computer(
    monkeypatch: pytest.MonkeyPatch, automatic: Path
):
    now = [1_800_000_000.0]
    monkeypatch.setattr(update.time, "time", lambda: now[0])
    repository = behind_a_release(monkeypatch)
    assert run_in_background() is not None
    assert len(repository.requests) == 1
    assert automatic.read_text() == "1800000000\n"
    # Another program the same day starts no check.
    for later in (1.0, 3600.0, update.CHECK_INTERVAL - 1):
        new_program(monkeypatch)
        now[0] = 1_800_000_000.0 + later
        run_in_background()
        assert len(repository.requests) == 1
    # A day after the last one, it checks again.
    new_program(monkeypatch)
    now[0] = 1_800_000_000.0 + update.CHECK_INTERVAL
    run_in_background()
    assert len(repository.requests) == 2
    assert automatic.read_text() == f"{1_800_000_000 + update.CHECK_INTERVAL}\n"


def test_the_automatic_check_runs_once_in_a_program(
    monkeypatch: pytest.MonkeyPatch, automatic: Path
):
    repository = behind_a_release(monkeypatch)
    assert run_in_background() is not None
    automatic.unlink()  # even with no record of it
    assert update.check_in_background() is None
    assert len(repository.requests) == 1


def test_a_program_that_checks_for_itself_is_not_checked_again(
    monkeypatch: pytest.MonkeyPatch, automatic: Path
):
    repository = behind_a_release(monkeypatch)
    check_for_update()
    assert update.check_in_background() is None
    assert len(repository.requests) == 1
    assert not automatic.exists()


@pytest.mark.parametrize("stamp", ["not a time", "nan", "inf", "", "\xff", "9" * 400])
def test_a_record_of_the_last_check_that_cannot_be_read_does_not_stop_it(
    monkeypatch: pytest.MonkeyPatch, automatic: Path, stamp: str
):
    automatic.parent.mkdir(parents=True)
    automatic.write_bytes(stamp.encode("latin-1"))
    repository = behind_a_release(monkeypatch)
    run_in_background()
    assert len(repository.requests) == 1


def test_a_last_check_in_the_future_does_not_stop_it(
    monkeypatch: pytest.MonkeyPatch, automatic: Path
):
    # The clock was set back: the check is not put off until the time comes again.
    automatic.parent.mkdir(parents=True)
    automatic.write_text(f"{time.time() + 7 * 24 * 3600:.0f}\n")
    repository = behind_a_release(monkeypatch)
    run_in_background()
    assert len(repository.requests) == 1


def test_a_failed_check_is_still_counted_so_it_is_not_retried_that_day(
    monkeypatch: pytest.MonkeyPatch, automatic: Path
):
    installed(monkeypatch, git_install())
    repository = answer(monkeypatch, error=TimeoutError("timed out"))
    run_in_background()
    new_program(monkeypatch)
    run_in_background()
    assert len(repository.requests) == 1


def test_the_time_is_recorded_before_the_check_starts(
    monkeypatch: pytest.MonkeyPatch, automatic: Path
):
    seen: list[bool] = []

    def checking(timeout: float = update.DEFAULT_TIMEOUT) -> update.UpdateCheck:
        seen.append(automatic.exists())
        raise RuntimeError("anything")

    monkeypatch.setattr(update, "check_for_update", checking)
    run_in_background()
    assert seen == [True]


def test_the_automatic_check_does_not_run_when_the_time_cannot_be_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, automatic: Path
):
    blocker = tmp_path / "a file"
    blocker.write_text("")
    monkeypatch.setattr(update, "_cache_dir", lambda: blocker / "qte-sdk")
    repository = behind_a_release(monkeypatch)
    run_in_background()
    monkeypatch.setattr(update, "_cache_dir", lambda: None)
    new_program(monkeypatch)
    run_in_background()
    assert repository.requests == []


@pytest.mark.parametrize("value", ["0", "false", "no", "off", " OFF ", "False"])
def test_qte_update_check_0_turns_the_automatic_check_off(
    monkeypatch: pytest.MonkeyPatch, automatic: Path, value: str
):
    monkeypatch.setenv("QTE_UPDATE_CHECK", value)
    repository = behind_a_release(monkeypatch)
    assert update.check_in_background() is None
    assert repository.requests == []
    assert not automatic.exists()


@pytest.mark.parametrize("value", ["1", "", "yes"])
def test_other_values_of_qte_update_check_leave_it_on(
    monkeypatch: pytest.MonkeyPatch, automatic: Path, value: str
):
    monkeypatch.setenv("QTE_UPDATE_CHECK", value)
    repository = behind_a_release(monkeypatch)
    assert run_in_background() is not None
    assert len(repository.requests) == 1


def test_the_tests_turn_the_automatic_check_off(tmp_path: Path):
    # As tests/conftest.py sets it for every test.
    assert update.check_in_background() is None
    assert update._cache_dir() == tmp_path / "cache" / "qte-sdk"


def test_the_automatic_check_never_waits_for_the_network(
    monkeypatch: pytest.MonkeyPatch, automatic: Path
):
    installed(monkeypatch, git_install())
    entered = threading.Event()
    release = threading.Event()

    def hanging(request: object, timeout: float) -> None:
        entered.set()
        release.wait(30)
        raise TimeoutError

    monkeypatch.setattr(update, "_open", hanging)
    started = time.monotonic()
    thread = update.check_in_background()
    took = time.monotonic() - started
    try:
        assert thread is not None and thread.daemon
        assert entered.wait(10)
        assert took < 1
    finally:
        release.set()
        if thread is not None:
            thread.join(30)


def test_the_automatic_check_never_raises_or_prints(
    monkeypatch: pytest.MonkeyPatch, automatic: Path, capsys: pytest.CaptureFixture[str]
):
    raised: list[object] = []
    monkeypatch.setattr(threading, "excepthook", raised.append)

    def broken(timeout: float = update.DEFAULT_TIMEOUT) -> None:
        raise SystemExit("/home/someone/private")

    monkeypatch.setattr(update, "check_for_update", broken)
    run_in_background()
    assert raised == []
    assert capsys.readouterr() == ("", "")


class ThreadRecorder(logging.Handler):
    """Records each record it handles with the thread that logged it."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[tuple[logging.LogRecord, threading.Thread]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append((record, threading.current_thread()))


def test_the_automatic_check_started_in_a_loop_logs_its_warning_on_that_loop(
    monkeypatch: pytest.MonkeyPatch, automatic: Path, caplog: pytest.LogCaptureFixture
):
    behind_a_release(monkeypatch)
    caplog.set_level(logging.DEBUG)
    emitted = ThreadRecorder()
    update.logger.addHandler(emitted)

    async def start_and_wait() -> threading.Thread:
        thread = update.check_in_background()
        assert thread is not None
        # The warning was handed to the loop before the thread ended, so it is logged first.
        await asyncio.to_thread(thread.join, 30)
        return thread

    try:
        thread = asyncio.run(start_and_wait())
    finally:
        update.logger.removeHandler(emitted)
    [(record, on)] = emitted.records
    assert record.code == "QTE-UPDATE-AVAILABLE"
    assert on is threading.current_thread()
    assert on is not thread


def test_a_warning_ready_after_the_loop_has_closed_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
    automatic: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
):
    repository = behind_a_release(monkeypatch)
    caplog.set_level(logging.DEBUG)
    raised: list[object] = []
    monkeypatch.setattr(threading, "excepthook", raised.append)
    entered = threading.Event()
    release = threading.Event()

    def held(request: Any, timeout: float) -> Any:
        entered.set()
        release.wait(30)
        return repository(request, timeout)

    monkeypatch.setattr(update, "_open", held)

    async def start() -> threading.Thread | None:
        return update.check_in_background()

    loop = asyncio.new_event_loop()
    try:
        thread = loop.run_until_complete(start())
    finally:
        loop.close()
    assert thread is not None
    try:
        assert entered.wait(10)
    finally:
        release.set()
    thread.join(30)
    assert not thread.is_alive()
    # The check ran and found the SDK behind, and the warning went nowhere, without an error.
    assert len(repository.requests) == 1
    assert update_records(caplog) == []
    assert raised == []
    assert capsys.readouterr() == ("", "")


def test_a_thread_that_cannot_start_is_no_error(monkeypatch: pytest.MonkeyPatch, automatic: Path):
    def refuse(self: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", refuse)
    assert update.check_in_background() is None


# The cache folder


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    folder = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: folder))
    return folder


def test_the_cache_folder_on_windows_is_under_localappdata(
    monkeypatch: pytest.MonkeyPatch, home: Path, tmp_path: Path
):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert ORIGINAL_CACHE_DIR() == tmp_path / "Local" / "qte-sdk"
    monkeypatch.delenv("LOCALAPPDATA")
    assert ORIGINAL_CACHE_DIR() == home / "AppData" / "Local" / "qte-sdk"


def test_the_cache_folder_on_macos_is_under_library_caches(
    monkeypatch: pytest.MonkeyPatch, home: Path, tmp_path: Path
):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert ORIGINAL_CACHE_DIR() == home / "Library" / "Caches" / "qte-sdk"


@pytest.mark.parametrize("platform", ["linux", "freebsd14"])
def test_the_cache_folder_elsewhere_is_xdg_cache_home_or_dot_cache(
    monkeypatch: pytest.MonkeyPatch, home: Path, tmp_path: Path, platform: str
):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert ORIGINAL_CACHE_DIR() == tmp_path / "xdg" / "qte-sdk"
    # The XDG specification ignores a relative path.
    monkeypatch.setenv("XDG_CACHE_HOME", "relative/cache")
    assert ORIGINAL_CACHE_DIR() == home / ".cache" / "qte-sdk"
    monkeypatch.delenv("XDG_CACHE_HOME")
    assert ORIGINAL_CACHE_DIR() == home / ".cache" / "qte-sdk"


def test_no_home_folder_gives_no_cache_folder(monkeypatch: pytest.MonkeyPatch):
    def no_home(cls: object) -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", classmethod(no_home))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    assert ORIGINAL_CACHE_DIR() is None


@pytest.mark.windows
def test_the_cache_folder_on_real_windows_is_under_localappdata():
    assert ORIGINAL_CACHE_DIR() == Path(os.environ["LOCALAPPDATA"]) / "qte-sdk"


def test_two_programs_deciding_at_once_do_not_both_check(
    monkeypatch: pytest.MonkeyPatch, automatic: Path
):
    # Another program holds the lock while it decides.
    automatic.parent.mkdir(parents=True)
    lock = automatic.parent / "update-check.lock"
    lock.write_text("")
    repository = behind_a_release(monkeypatch)
    run_in_background()
    assert repository.requests == []
    assert not automatic.exists()
    assert lock.exists()


def test_a_lock_dated_in_the_future_is_cleared_for_the_next(
    monkeypatch: pytest.MonkeyPatch, automatic: Path
):
    # A program stopped while the clock was ahead, and the clock was then set back.
    automatic.parent.mkdir(parents=True)
    lock = automatic.parent / "update-check.lock"
    lock.write_text("")
    ahead = time.time() + 24 * 60 * 60
    os.utime(lock, (ahead, ahead))
    repository = behind_a_release(monkeypatch)
    run_in_background()
    assert not lock.exists()
    new_program(monkeypatch)
    run_in_background()
    assert len(repository.requests) == 1


def test_a_lock_left_by_a_program_that_stopped_is_cleared_for_the_next(
    monkeypatch: pytest.MonkeyPatch, automatic: Path
):
    automatic.parent.mkdir(parents=True)
    lock = automatic.parent / "update-check.lock"
    lock.write_text("")
    old = time.time() - 10 * 60
    os.utime(lock, (old, old))
    repository = behind_a_release(monkeypatch)
    run_in_background()
    assert not lock.exists()
    new_program(monkeypatch)
    run_in_background()
    assert len(repository.requests) == 1
    # The lock is released after each decision.
    assert not lock.exists()


def test_releases_json_is_read_no_further_than_one_byte_past_its_limit(
    monkeypatch: pytest.MonkeyPatch,
):
    body = b"[" + b" " * (4 * update._MAX_RELEASES_SIZE) + b"]"
    response = FakeResponse(body, update.RELEASES_URL, "text/plain")
    monkeypatch.setattr(update, "_open", lambda request, timeout: response)
    with pytest.raises(update._CannotTell):
        update._read_releases(5)
    assert len(body) - len(response.body) == update._MAX_RELEASES_SIZE + 1


@pytest.mark.windows
def test_the_daily_record_and_its_lock_work_on_real_windows(
    monkeypatch: pytest.MonkeyPatch, automatic: Path
):
    repository = behind_a_release(monkeypatch)
    assert run_in_background() is not None
    assert len(repository.requests) == 1
    assert automatic.exists()
    assert not (automatic.parent / "update-check.lock").exists()
    new_program(monkeypatch)
    run_in_background()
    assert len(repository.requests) == 1
