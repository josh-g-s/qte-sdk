"""The update check: what it reads from pip's install record and from the repository's
list of references, and what it says. No test here uses the network: every read of the
repository is answered by a canned list, and any other is an error."""

import email.message
import http.server
import json
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
    """Answers the update check's one request with `body`, or raises `error`, and records
    the requests."""

    def __init__(self, body: bytes = b"", error: BaseException | None = None, **kwargs: Any):
        self.body = body
        self.error = error
        self.kwargs = kwargs
        self.requests: list[tuple[urllib.request.Request, float]] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> FakeResponse:
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
    assert result.message == (
        f"qte-sdk {VERSION} is behind the latest release, {newer}: update with {expected}"
    )
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
    assert result.message == (
        f"qte-sdk {VERSION} is behind the latest release, {newer}: update with "
        f"pip install https://github.com/josh-g-s/qte-sdk/archive/refs/tags/{newer}.zip"
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
    assert result.message == (
        f"qte-sdk {VERSION} is behind the latest release, {newer}: update with {COMMAND}"
    )


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
        f"qte-sdk {VERSION} is behind the latest release, {newer}: update with {COMMAND}",
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
        f"qte-sdk {VERSION} is behind the latest release, {newer}: update with "
        f"{archive_command(newer)}",
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
