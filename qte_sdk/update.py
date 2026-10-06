"""Say whether the installed SDK is the latest release.

    python -m qte_sdk.update                 # check, with a 5 s limit on the network
    python -m qte_sdk.update --timeout 10    # allow longer

A release is a `vX.Y.Z` tag in the SDK's repository, github.com/josh-g-s/qte-sdk. The
check compares the installed `qte_sdk.__version__` with the highest release tag and prints
one of three results, with its exit status:

    current         0   the installed version is the latest release (or newer). For an
                        install that follows `main` (installed with git and no `@revision`,
                        or with `@main`), it also says whether `main` has newer commits,
                        with the command that takes them.
    behind          1   a newer release is out. It prints the command that updates.
    can't tell      2   the install is not one pip made from the repository on GitHub (a
                        local or editable copy, a wheel, another repository) and is not
                        behind a release, its install record cannot be read, no release is
                        tagged yet, or the repository could not be reached.

An install from a release archive of the repository, such as
`pip install https://github.com/josh-g-s/qte-sdk/archive/refs/tags/v1.0.1.zip`, which needs
no git, is current or behind like any other. So is one from an archive of a branch, such as
`main.zip`, though the check cannot say whether that branch has moved since.

`check_for_update()` returns the same result as an `UpdateCheck`, so a program can log it
when it starts. Nothing here runs when `qte_sdk` is imported: only that function and this
command use the network.

The install comes from the record pip keeps of where it installed the SDK from
(`direct_url.json`, PEP 610). The repository's tags and `main` come from git's own list of
references, read over HTTPS the way `git ls-remote` reads it, so neither `git` nor the
GitHub API is needed. The request carries no information about you beyond a
`qte-sdk/<version>` user agent. The install is identified before anything is fetched. An
install the check cannot place is never called current, but it is still compared with the
latest release, so a version below it is behind. Only when the install record cannot be
read, or does not describe the `qte_sdk` imported, is nothing fetched at all.

The command it prints depends on the case. Behind a release, it is a plain
`pip install --upgrade`, which installs the new version and resolves its dependencies as
usual; an install pinned to a release tag is pointed at the latest release tag. An install
from an archive is pointed at the latest release's archive, which pip installs over the old
version without `--upgrade`. For newer commits on `main` at the same version, pip would keep
the install as it is, so the command reinstalls the SDK with `--force-reinstall --no-deps`,
leaving your other packages alone. A change to the SDK's dependencies therefore always comes
in a new release.
"""

import argparse
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from enum import StrEnum
from importlib import metadata
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

import qte_sdk

__all__ = [
    "ARCHIVE_URL",
    "DEFAULT_TIMEOUT",
    "INSTALL_URL",
    "Status",
    "UpdateCheck",
    "check_for_update",
    "main",
    "update_command",
]

DISTRIBUTION = "qte-sdk"
REPOSITORY = "github.com/josh-g-s/qte-sdk"
INSTALL_URL = f"git+https://{REPOSITORY}"
# A release's archive, which pip installs with no git.
ARCHIVE_URL = f"https://{REPOSITORY}/archive/refs/tags/{{tag}}.zip"
DEFAULT_TIMEOUT = 5.0

# Git's list of references, in the original protocol: no `Git-Protocol` header is sent.
_REFS_URL = f"https://{REPOSITORY}.git/info/refs?service=git-upload-pack"
_REFS_TYPE = "application/x-git-upload-pack-advertisement"
# Far more than the repository's list of references needs, and a bound on what is read.
_MAX_REFS_SIZE = 1 << 20
_CHUNK = 1 << 16

# A part of a version: no leading zero, and short enough to be read as a number safely.
_NUMBER = r"(0|[1-9][0-9]{0,8})"
_VERSION = re.compile(rf"{_NUMBER}\.{_NUMBER}\.{_NUMBER}")
_RELEASE_TAG = re.compile(rf"v{_NUMBER}\.{_NUMBER}\.{_NUMBER}")
_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class Status(StrEnum):
    """The result of a check. Its exit status is `UpdateCheck.exit_code`."""

    CURRENT = "current"
    BEHIND = "behind"
    UNKNOWN = "unknown"


_EXIT_CODES = {Status.CURRENT: 0, Status.BEHIND: 1, Status.UNKNOWN: 2}


@dataclass(frozen=True)
class UpdateCheck:
    """What `check_for_update` found.

    `installed_commit` and `installed_revision` are the commit pip installed and the
    `@revision` it was asked for (None for none), when the install came from the
    repository with git. `installed_archive` is the revision of the archive pip installed,
    such as "v1.0.1" or "main", when the install came from an archive of the repository
    instead (it has no commit). `latest_release` is the highest release tag, such as "v1.0.0", and
    `main_commit` the commit at the tip of `main`, when the repository was read.
    `main_ahead` is true when the install follows `main` and `main` has newer commits.
    `command` is the command that updates, when there is something to take, and `message`
    says it all in one line."""

    status: Status
    installed_version: str
    installed_commit: str | None
    installed_revision: str | None
    latest_release: str | None
    main_commit: str | None
    main_ahead: bool
    command: str | None
    message: str
    installed_archive: str | None = None

    @property
    def exit_code(self) -> int:
        """0 when current, 1 when behind a release, 2 when the check cannot tell."""
        return _EXIT_CODES[self.status]


def update_command(
    tag: str | None = None, *, reinstall: bool = False, archive: bool = False
) -> str:
    """The command that installs the SDK from `main`, or from release `tag`. With
    `reinstall`, it reinstalls the SDK alone, as is needed to take newer commits at the
    same version number: pip keeps an install whose version is unchanged otherwise. With
    `archive`, it installs release `tag` from its archive, which needs no git; pip replaces
    an older version installed from an archive without `--upgrade`."""
    if archive:
        if tag is None or reinstall:
            raise ValueError("an archive is installed from a release tag, and never reinstalled")
        return f"pip install {ARCHIVE_URL.format(tag=tag)}"
    url = INSTALL_URL if tag is None else f"{INSTALL_URL}@{tag}"
    if reinstall:
        return f'pip install --upgrade --force-reinstall --no-deps "{url}"'
    return f'pip install --upgrade "{url}"'


def check_for_update(timeout: float = DEFAULT_TIMEOUT) -> UpdateCheck:
    """Compare the installed SDK with the repository's latest release. Reads the
    repository over HTTPS, waiting at most `timeout` seconds for it, and only when the
    install came from it. Never raises: when the install or the repository cannot be read,
    the result is `Status.UNKNOWN`."""
    version = qte_sdk.__version__
    install = _Install()
    try:
        try:
            install = _installed(version)
        except _Unplaced as unplaced:
            return _compare_unplaced(version, unplaced, timeout)
        return _compare(version, install, _parse_refs(_fetch_refs(timeout)))
    except _CannotTell as reason:
        return _unknown(version, install, str(reason))
    except Exception as error:
        # Not expected: only the kind is shown, since the error's text could hold anything.
        return _unknown(version, install, f"the check failed ({type(error).__name__})")


class _CannotTell(Exception):
    """Why the check cannot tell, as a phrase. Never holds a path or an address."""


class _Unplaced(_CannotTell):
    """The SDK imported is the one pip installed, at a release number, but not from the
    repository: it can still be compared with the latest release. `archive` is true when pip
    installed it from an archive or wheel, which an archive of a release can replace. Unless
    `advice` is given, the reason ends by saying how to install a release that can be
    checked."""

    def __init__(self, reason: str, *, archive: bool = False, advice: str | None = None) -> None:
        super().__init__(f"{reason}; {advice or _INSTALL_ADVICE}")
        self.archive = archive


_INSTALL_ADVICE = (
    f"to have it checked, install a release from {REPOSITORY}, with git or from the release's "
    "zip, as its README says"
)


class _Install(NamedTuple):
    """An install from the repository: with git, the commit pip installed and the revision
    it was asked for; from an archive, the archive's revision."""

    commit: str | None = None
    revision: str | None = None
    archive: str | None = None


# The install


def _installed(version: str) -> _Install:
    """How pip installed the SDK, if it installed it from the repository; otherwise
    `_CannotTell` says why not, as `_Unplaced` when the install can still be compared with
    the latest release."""
    try:
        distribution = metadata.distribution(DISTRIBUTION)
        recorded_version = distribution.version
        text = distribution.read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        raise _CannotTell("it is not installed as a package, so pip has no record of it") from None
    except (OSError, UnicodeDecodeError):
        raise _CannotTell("its install record (direct_url.json) cannot be read") from None
    try:
        own = Path(str(distribution.locate_file("qte_sdk/__init__.py"))).resolve()
        same = own == Path(qte_sdk.__file__ or "").resolve()
    except (OSError, RuntimeError, ValueError):
        same = False
    if not same:
        raise _CannotTell(
            "the qte_sdk imported is not the one pip installed (another copy comes first on "
            "the path), so its install record does not describe it"
        )
    if recorded_version != version:
        # The recorded version is shown only when it is a release number, as it should be.
        recorded = (
            f"version {recorded_version}"
            if isinstance(recorded_version, str) and _VERSION.fullmatch(recorded_version)
            else "another version"
        )
        raise _CannotTell(
            f"the package pip installed is {recorded}, but the qte_sdk imported is "
            f"{version}, so they are not the same copy"
        )
    if _VERSION.fullmatch(version) is None:
        raise _CannotTell(f"its version, {version}, is not a release number")
    return _from_direct_url(text)


def _from_direct_url(text: str | None) -> _Install:
    """The install that pip's `direct_url.json` records, if it records an install from the
    repository, with git or from an archive. The recorded address is never repeated: it can
    hold a password, and a local path can name you."""
    if text is None:
        raise _Unplaced(
            "pip did not record where it came from (no direct_url.json), so it was not "
            f"installed from {REPOSITORY}"
        )
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("url"), str):
        raise _CannotTell("its install record (direct_url.json) cannot be read")
    kinds = [kind for kind in ("vcs_info", "archive_info", "dir_info") if kind in data]
    if len(kinds) != 1 or not isinstance(data[kinds[0]], dict):
        raise _CannotTell("its install record (direct_url.json) cannot be read")
    kind = kinds[0]
    info = data[kind]
    if kind == "dir_info":
        if info.get("editable") is True:
            raise _Unplaced(
                "it is an editable install of a local copy (pip install -e)",
                advice="update that copy with git",
            )
        raise _Unplaced(f"it was installed from a local copy, not from {REPOSITORY}")
    if kind == "archive_info":
        archive = _archive_revision(data["url"])
        if archive is None:
            raise _Unplaced(
                f"it was installed from an archive or wheel, not from {REPOSITORY}", archive=True
            )
        return _Install(archive=archive)
    if info.get("vcs") != "git":
        raise _Unplaced("it was installed with another version control system, not git")
    if not _is_repository(data["url"]):
        raise _Unplaced(f"it was installed from another repository, not {REPOSITORY}")
    commit = info.get("commit_id")
    revision = info.get("requested_revision")
    if not isinstance(commit, str) or _OBJECT_ID.fullmatch(commit) is None:
        raise _Unplaced("its install record names no commit it was installed from")
    if revision is not None and not isinstance(revision, str):
        raise _CannotTell("its install record (direct_url.json) cannot be read")
    return _Install(commit=commit, revision=revision)


def _is_repository(url: str) -> bool:
    """Whether `url` is the SDK's repository on GitHub, ignoring case, a trailing slash and
    `.git`, over HTTPS, HTTP, SSH or git."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018  # raises for a port that is not a number
    except ValueError:
        return False
    if parts.scheme.lower() not in ("https", "http", "ssh", "git"):
        return False
    if host is None or host.lower() != "github.com" or parts.query or parts.fragment:
        return False
    path = parts.path.rstrip("/").lower().removesuffix(".git")
    return path == "/josh-g-s/qte-sdk"


def _archive_revision(url: str) -> str | None:
    """The revision of the archive of the SDK's repository that GitHub serves at `url`, such
    as "v1.0.1" for `.../archive/refs/tags/v1.0.1.zip`, or None when `url` is not one. Both
    of GitHub's addresses for an archive are recognised, zip or tar.gz:
    `github.com/josh-g-s/qte-sdk/archive/[refs/tags/|refs/heads/]<revision>.zip` and
    `codeload.github.com/josh-g-s/qte-sdk/zip/[refs/tags/|refs/heads/]<revision>`."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018  # raises for a port that is not a number
    except ValueError:
        return None
    if parts.scheme.lower() not in ("https", "http") or host is None or parts.query:
        return None
    segments = parts.path.split("/")
    if len(segments) < 5 or segments[0] or "/".join(segments[1:3]).lower() != "josh-g-s/qte-sdk":
        return None
    kind, rest = segments[3], segments[4:]
    if host.lower() == "github.com" and kind == "archive":
        name = rest[-1]
        suffix = next((s for s in (".zip", ".tar.gz") if name.endswith(s)), None)
        if suffix is None:
            return None
        rest = [*rest[:-1], name.removesuffix(suffix)]
    elif host.lower() != "codeload.github.com" or kind not in ("zip", "tar.gz"):
        return None
    if len(rest) > 2 and rest[0] == "refs" and rest[1] in ("tags", "heads"):
        rest = rest[2:]
    if not all(rest):
        return None
    return "/".join(rest)


# The repository


def _fetch_refs(timeout: float) -> bytes:
    """The repository's list of references, waiting at most `timeout` seconds in all. The
    request runs in a background thread, so a slow address lookup or an answer that
    trickles in is not waited for: it is left to end by itself, as the connection's own
    timeout ends it."""
    outcome: list[bytes | _CannotTell] = []

    def read() -> None:
        try:
            outcome.append(_read_refs(timeout))
        except _CannotTell as reason:
            outcome.append(reason)
        except BaseException as error:
            outcome.append(_CannotTell(f"the check failed ({type(error).__name__})"))

    worker = threading.Thread(target=read, name="qte-sdk update check", daemon=True)
    worker.start()
    worker.join(timeout)
    if not outcome:
        raise _CannotTell(f"{REPOSITORY} did not answer within {timeout:g} s")
    found = outcome[0]
    if isinstance(found, _CannotTell):
        raise found
    return found


def _read_refs(timeout: float) -> bytes:
    """The repository's list of references, as git's smart HTTP service sends it."""
    request = urllib.request.Request(
        _REFS_URL, headers={"User-Agent": f"qte-sdk/{qte_sdk.__version__}"}
    )
    deadline = time.monotonic() + timeout
    failure = None
    try:
        with _open(request, timeout) as response:
            if response.geturl() != _REFS_URL:
                raise _CannotTell(f"{REPOSITORY} sent it elsewhere")
            if response.headers.get_content_type() != _REFS_TYPE:
                raise _CannotTell(f"{REPOSITORY} did not answer as a git repository")
            data = bytearray()
            while len(data) <= _MAX_REFS_SIZE:
                if time.monotonic() > deadline:
                    raise _CannotTell(f"{REPOSITORY} did not answer within {timeout:g} s")
                # At most one read from the connection, so the deadline is checked often.
                chunk = response.read1(_CHUNK)
                if not chunk:
                    return bytes(data)
                data += chunk
            raise _CannotTell(f"{REPOSITORY} sent a longer list of releases than expected")
    except _CannotTell:
        raise
    except urllib.error.HTTPError as error:
        moved = 300 <= error.code < 400
        failure = f"{REPOSITORY} " + (
            "sent it elsewhere" if moved else f"answered HTTP {error.code}"
        )
    except TimeoutError:
        failure = f"{REPOSITORY} did not answer within {timeout:g} s"
    except Exception as error:
        # Only the kind: an error's text can name a proxy, with its password.
        failure = f"{REPOSITORY} could not be reached ({type(error).__name__})"
    raise _CannotTell(failure)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Follows no redirect: the list is read from the repository's own address or not at
    all, so nothing is sent to another host."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def _open(request: urllib.request.Request, timeout: float) -> Any:
    """Send `request`, following no redirect. A redirect is an `HTTPError` with its 3xx
    status."""
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


def _parse_refs(data: bytes) -> dict[str, str]:
    """Each reference's name and object, from git's pkt-line list: a `# service=` line and
    a flush, then one reference per line and a flush. The first line's capabilities, after
    a NUL, are dropped. An annotated tag's line is followed by its `^{}` line, which names
    the commit it points at."""
    lines = _pkt_lines(data)
    if (
        len(lines) < 3
        or lines[0] != b"# service=git-upload-pack"
        or lines[1] is not None
        or lines[-1] is not None
        or None in lines[2:-1]
    ):
        raise _CannotTell(f"{REPOSITORY} sent a list of releases that cannot be read")
    refs: dict[str, str] = {}
    for line in lines[2:-1]:
        assert line is not None
        object_id, space, name = line.split(b"\0", 1)[0].partition(b" ")
        oid = object_id.decode("ascii", "replace")
        if not space or not name or _OBJECT_ID.fullmatch(oid) is None:
            raise _CannotTell(f"{REPOSITORY} sent a list of releases that cannot be read")
        refs[name.decode("utf-8", "replace")] = oid
    refs.pop("capabilities^{}", None)  # an empty repository's placeholder
    return refs


def _pkt_lines(data: bytes) -> list[bytes | None]:
    """The lines of a pkt-line stream, without their newlines, with None for each flush."""
    lines: list[bytes | None] = []
    at = 0
    while at < len(data):
        head = data[at : at + 4]
        if len(head) < 4 or not all(c in b"0123456789abcdefABCDEF" for c in head):
            raise _CannotTell(f"{REPOSITORY} sent a list of releases that cannot be read")
        size = int(head, 16)
        if size == 0:
            lines.append(None)
            at += 4
            continue
        if size < 4 or at + size > len(data):
            raise _CannotTell(f"{REPOSITORY} sent a list of releases that cannot be read")
        lines.append(data[at + 4 : at + size].removesuffix(b"\n"))
        at += size
    return lines


def _releases(refs: dict[str, str]) -> dict[str, tuple[tuple[int, int, int], str]]:
    """Each release tag's version and commit. Other tags are left out."""
    found = {}
    for name, oid in refs.items():
        if not name.startswith("refs/tags/") or name.endswith("^{}"):
            continue
        tag = name.removeprefix("refs/tags/")
        match = _RELEASE_TAG.fullmatch(tag)
        if match is not None:
            number = (int(match[1]), int(match[2]), int(match[3]))
            found[tag] = (number, refs.get(f"{name}^{{}}", oid))
    return found


# The result


def _number(version: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(version)
    assert match is not None  # checked in _installed
    return (int(match[1]), int(match[2]), int(match[3]))


def _latest(releases: dict[str, tuple[tuple[int, int, int], str]]) -> str | None:
    return max(releases, key=lambda tag: releases[tag][0], default=None)


def _behind_message(version: str, latest: str, command: str) -> str:
    return f"qte-sdk {version} is behind the latest release, {latest}: update with {command}"


def _compare(version: str, install: _Install, refs: dict[str, str]) -> UpdateCheck:
    installed = _number(version)
    commit, revision, archive = install
    releases = _releases(refs)
    latest = _latest(releases)
    main = refs.get("refs/heads/main")
    # Only a git install knows its commit, so only it can say whether main has moved.
    follows_main = commit is not None and revision in (None, "main")
    main_ahead = follows_main and main is not None and main != commit
    pinned = revision is not None and _RELEASE_TAG.fullmatch(revision) is not None
    newer = (
        f"; main has newer commits than the one installed ({commit[:12]}): take them with "
        f"{update_command(reinstall=True)}"
        if main_ahead
        else ""
    )

    def result(status: Status, command: str | None, message: str) -> UpdateCheck:
        return UpdateCheck(
            status=status,
            installed_version=version,
            installed_commit=commit,
            installed_revision=revision,
            latest_release=latest,
            main_commit=main,
            main_ahead=main_ahead,
            command=command,
            message=message,
            installed_archive=archive,
        )

    if latest is None:
        return result(
            Status.UNKNOWN,
            update_command(reinstall=True) if main_ahead else None,
            f"cannot tell whether qte-sdk {version} is current: no release is tagged yet{newer}",
        )
    if installed < releases[latest][0]:
        if archive is not None:
            command = update_command(latest, archive=True)
        else:
            command = update_command(latest if pinned else None)
        return result(Status.BEHIND, command, _behind_message(version, latest, command))
    relation = "is" if installed == releases[latest][0] else "is newer than"
    text = f"qte-sdk {version} {relation} the latest release, {latest}"
    if follows_main and main is not None and not main_ahead:
        text += ", and main has no newer commits"
    command = update_command(reinstall=True) if main_ahead else None
    return result(Status.CURRENT, command, text + newer)


def _compare_unplaced(version: str, unplaced: _Unplaced, timeout: float) -> UpdateCheck:
    """An install the check cannot place is never current, but it can be behind. When the
    repository cannot be read, the install's reason is given: it says more."""
    unknown = _unknown(version, _Install(), str(unplaced))
    try:
        refs = _parse_refs(_fetch_refs(timeout))
    except _CannotTell:
        return unknown
    releases = _releases(refs)
    latest = _latest(releases)
    unknown = replace(unknown, latest_release=latest, main_commit=refs.get("refs/heads/main"))
    if latest is None or _number(version) >= releases[latest][0]:
        return unknown
    command = update_command(latest, archive=True) if unplaced.archive else update_command()
    return replace(
        unknown,
        status=Status.BEHIND,
        command=command,
        message=_behind_message(version, latest, command),
    )


def _unknown(version: str, install: _Install, reason: str) -> UpdateCheck:
    return UpdateCheck(
        status=Status.UNKNOWN,
        installed_version=version,
        installed_commit=install.commit,
        installed_revision=install.revision,
        latest_release=None,
        main_commit=None,
        main_ahead=False,
        command=None,
        message=f"cannot tell whether qte-sdk {version} is current: {reason}",
        installed_archive=install.archive,
    )


# The command


def main(argv: list[str] | None = None) -> int:
    """Check, print the result and return its exit status."""
    parser = argparse.ArgumentParser(
        prog="python -m qte_sdk.update",
        description="Say whether the installed qte-sdk is the latest release.",
    )
    parser.add_argument(
        "--timeout",
        type=_seconds,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=f"how long to wait for GitHub (default {DEFAULT_TIMEOUT:g})",
    )
    args = parser.parse_args(argv)
    result = check_for_update(args.timeout)
    print(f"installed: {_describe_install(result)}")
    print(result.message)
    return result.exit_code


def _seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = 0.0
    if not 0 < value <= 60:
        raise argparse.ArgumentTypeError("give a number of seconds above 0 and at most 60")
    return value


def _describe_install(result: UpdateCheck) -> str:
    text = f"qte-sdk {result.installed_version}"
    archive = result.installed_archive
    if archive is not None:
        # As for a revision, a branch's name is not shown, other than main's.
        if _RELEASE_TAG.fullmatch(archive):
            return f"{text}, from the {archive} release archive"
        if archive == "main":
            return f"{text}, from an archive of main"
        return f"{text}, from an archive of another revision"
    if result.installed_commit is None:
        return text
    revision = result.installed_revision
    if revision in (None, "main"):
        following = "following main"
    elif _RELEASE_TAG.fullmatch(revision) or re.fullmatch(r"[0-9a-f]{7,64}", revision):
        following = f"pinned to {revision}"
    else:
        # A branch's name is not shown: a mistaken install record could hold anything.
        following = "pinned to another revision"
    return f"{text}, commit {result.installed_commit[:12]}, {following}"


if __name__ == "__main__":
    sys.exit(main())
