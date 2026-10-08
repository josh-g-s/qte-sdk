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
    behind          1   a newer release is out. It prints the command that updates, and
                        whether the update is recommended, and why.
    can't tell      2   the install is not one pip made from the repository on GitHub (a
                        local or editable copy, a wheel, another repository) and is not
                        behind a release, its install record cannot be read, no release is
                        tagged yet, or the repository could not be reached.

Behind a release, the result is one line with the code `QTE-UPDATE-AVAILABLE`:

    QTE-UPDATE-AVAILABLE: qte-sdk 1.1.0 is behind 1.1.1, a recommended update on Windows:
    <why>. Update with <command>.

and, for an update that is not recommended, the same without the part from "a recommended
update" to the reason.

An install from a release archive of the repository, such as
`pip install https://github.com/josh-g-s/qte-sdk/archive/refs/tags/v1.0.1.zip`, which needs
no git, is current or behind like any other. So is one from an archive of a branch, such as
`main.zip`, though the check cannot say whether that branch has moved since.

`check_for_update()` returns the same result as an `UpdateCheck`, so a program can log it
when it starts. Nothing here runs when `qte_sdk` is imported.

Recommended updates

`releases.json`, at the root of the repository's `main` branch, lists each release with
whether it is a recommended update, why, in one sentence, and the platforms it is
recommended on (`sys.platform` names, such as `win32`; none for all). Only when the
installed SDK is behind a release does the check read it, from
`https://raw.githubusercontent.com/josh-g-s/qte-sdk/main/releases.json`, with what is left
of the same timeout, at most 64 KiB, and no redirect. The update is recommended when a
release above the installed version, up to the latest release tag, is marked recommended
for this platform; the reason given is that of the highest such release. The file only adds
that: whether the SDK is behind, and the latest release, still come from the release tags,
so an entry for a release not yet tagged is ignored, and when the file cannot be read, or
does not hold a list of entries in that form, the check says what it said before. An entry
that is not in that form is ignored, and keys it does not know are too. The reason is shown
as plain text, on one line, without control or formatting characters, and at most 200
characters long.

The automatic check

`open_session`, and a `ReconnectingSession` when it first connects, also start the check
in the background, at most once a day on each computer and once in each program. It runs
in a daemon thread, so it never delays, holds up the end of, or fails a session, and it
never raises or prints. When the installed SDK is behind a release, it logs that one line
at WARNING through the `qte_sdk.update` logger, with the code on the record as `code`, and
it is silent when the SDK is current, when it cannot tell, and when the network fails. The
warning is written from the check's own thread, never on the session's event loop, and with
the standard stream handlers it never holds up the session or the program's exit: when
stderr is a pipe or terminal that cannot take the whole line at that moment (a full pipe
that nothing reads, say), or the line is over 512 bytes, it is not written there, and the
next day's check says it again. On Windows, a pipe that cannot take the line, by the room
it reports, is also skipped, not waited on; a pipe whose reader is already waiting for
output can report none, so there the line may be skipped although it would have fit. A
Windows console that is paused, or in which text is being selected, can hold the line
until it resumes, as it holds the program's own output. Handlers the program configures
(a log file, pytest's caplog, a JSON formatter) still receive it, with its `code`; a
handler the SDK cannot see into, such as a custom emit or a queue listener, writes as it
always has. A stderr the program has replaced or wrapped (colorama, a rich progress
display, a tee) gets the same check when its `fileno()` leads to the pipe or terminal, and
then can wait only if something else fills the pipe in the instant between that check and
the write, or if the wrapper writes output of the program's own that it held back (rich
keeps a partial line until it ends) along with it; one with no usable `fileno()` writes as
it always has. It sends the same
requests as the command, and nothing more. The day is
counted from a file holding the time of the last check, written just before the check
starts, so a failed check is not retried until the next day: `qte-sdk/update-check` in
your cache folder
(`%LOCALAPPDATA%` on Windows, `~/Library/Caches` on macOS, and `$XDG_CACHE_HOME` or
`~/.cache` elsewhere). While a program reads and writes it, it holds
`update-check.lock` beside it, so two programs started together do not both check. When
the file cannot be written, the check does not run. A program
that calls `check_for_update()` itself is not checked again automatically. Set
`QTE_UPDATE_CHECK=0` in the environment to turn it off. Replays and past market data
(`qte_sdk.replay`, `qte_sdk.history`) never start it, and the SDK's own tests turn it off.

The install comes from the record pip keeps of where it installed the SDK from
(`direct_url.json`, PEP 610). The repository's tags and `main` come from git's own list of
references, read over HTTPS the way `git ls-remote` reads it, so neither `git` nor the
GitHub API is needed. Neither request carries any information about you beyond a
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
import io
import json
import logging
import math
import os
import re
import select
import stat
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from importlib import metadata
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

import qte_sdk

__all__ = [
    "ARCHIVE_URL",
    "CHECK_INTERVAL",
    "DEFAULT_TIMEOUT",
    "INSTALL_URL",
    "RELEASES_URL",
    "UPDATE_AVAILABLE",
    "UPDATE_CHECK_ENV_VAR",
    "Status",
    "UpdateCheck",
    "check_for_update",
    "check_in_background",
    "main",
    "update_command",
]

DISTRIBUTION = "qte-sdk"
REPOSITORY = "github.com/josh-g-s/qte-sdk"
INSTALL_URL = f"git+https://{REPOSITORY}"
# A release's archive, which pip installs with no git.
ARCHIVE_URL = f"https://{REPOSITORY}/archive/refs/tags/{{tag}}.zip"
DEFAULT_TIMEOUT = 5.0
# The code of the line that says the SDK is behind a release, as a log record's `code`.
UPDATE_AVAILABLE = "QTE-UPDATE-AVAILABLE"
# Set to 0 to turn the automatic check off.
UPDATE_CHECK_ENV_VAR = "QTE_UPDATE_CHECK"
# The least time between two automatic checks on one computer, in seconds.
CHECK_INTERVAL = 24 * 60 * 60
# Which releases are recommended updates, and why: read from main, and only when behind.
RELEASES_URL = "https://raw.githubusercontent.com/josh-g-s/qte-sdk/main/releases.json"

logger = logging.getLogger(__name__)

# Git's list of references, in the original protocol: no `Git-Protocol` header is sent.
_REFS_URL = f"https://{REPOSITORY}.git/info/refs?service=git-upload-pack"
_REFS_TYPE = "application/x-git-upload-pack-advertisement"
# Far more than the repository's list of references needs, and a bound on what is read.
_MAX_REFS_SIZE = 1 << 20
_CHUNK = 1 << 16
# Far more than releases.json needs, and a bound on what is read.
_MAX_RELEASES_SIZE = 64 * 1024
# The longest reason shown, in characters.
_MAX_WHY = 200
# A `sys.platform` name in releases.json, such as "win32", "darwin" or "linux".
_PLATFORM = re.compile(r"[a-z][a-z0-9_]{0,31}")
_PLATFORM_NAMES = {"win32": "Windows", "darwin": "macOS", "linux": "Linux"}
# The values of QTE_UPDATE_CHECK that turn the automatic check off.
_OFF = frozenset({"0", "false", "no", "off"})

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
    says it all in one line. `recommended` is true when the install is behind a release
    that `releases.json` marks as a recommended update for this platform, and `why` is the
    reason it gives, as plain text on one line (None when not recommended)."""

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
    recommended: bool = False
    why: str | None = None

    @property
    def exit_code(self) -> int:
        """0 when current, 1 when behind a release, 2 when the check cannot tell."""
        return _EXIT_CODES[self.status]

    @property
    def code(self) -> str | None:
        """`UPDATE_AVAILABLE` when behind a release, which `message` starts with; else None."""
        return UPDATE_AVAILABLE if self.status is Status.BEHIND else None


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
    the result is `Status.UNKNOWN`. Behind a release, it also reads `releases.json` within
    what is left of `timeout`, to say whether the update is recommended.

    A program that calls it is not checked again automatically (see `check_in_background`)."""
    _note_checked()
    version = qte_sdk.__version__
    install = _Install()
    deadline = time.monotonic() + timeout
    try:
        try:
            install = _installed(version)
        except _Unplaced as unplaced:
            return _compare_unplaced(version, unplaced, timeout, deadline)
        return _compare(version, install, _parse_refs(_fetch_refs(timeout)), deadline)
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
        imported = Path(qte_sdk.__file__ or "").resolve()
        own = Path(str(distribution.locate_file("qte_sdk/__init__.py"))).resolve()
        source = _editable_source(text)
        # An editable install is imported from its source folder, not from site-packages.
        same = own == imported or (source is not None and imported.is_relative_to(source))
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


def _editable_source(text: str | None) -> Path | None:
    """The local folder an editable install was made from, if `text` records one."""
    try:
        data = json.loads(text or "")
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("dir_info"), dict):
        return None
    url = data.get("url")
    if data["dir_info"].get("editable") is not True or not isinstance(url, str):
        return None
    parts = urlsplit(url)
    if parts.scheme.lower() != "file" or parts.netloc not in ("", "localhost"):
        return None
    return Path(urllib.request.url2pathname(parts.path)).resolve()


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
    """The repository's list of references, waiting at most `timeout` seconds in all."""
    return _fetch(_read_refs, timeout)


def _fetch(read_one: Callable[[float], bytes], timeout: float) -> bytes:
    """What `read_one(timeout)` reads, waiting at most `timeout` seconds in all. The
    request runs in a background thread, so a slow address lookup or an answer that
    trickles in is not waited for: it is left to end by itself, as the connection's own
    timeout ends it."""
    outcome: list[bytes | _CannotTell] = []

    def read() -> None:
        try:
            outcome.append(read_one(timeout))
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
            data = _read_at_most(response, _MAX_REFS_SIZE, deadline, timeout)
            if data is None:
                raise _CannotTell(f"{REPOSITORY} sent a longer list of releases than expected")
            return data
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


def _read_at_most(response: Any, limit: int, deadline: float, timeout: float) -> bytes | None:
    """The body of `response`, or None if it is longer than `limit` bytes. Raises
    `_CannotTell` once `deadline` has passed."""
    data = bytearray()
    while len(data) <= limit:
        if time.monotonic() > deadline:
            raise _CannotTell(f"{REPOSITORY} did not answer within {timeout:g} s")
        # At most one read from the connection, so the deadline is checked often, and never
        # more than one byte beyond the limit.
        chunk = response.read1(min(_CHUNK, limit + 1 - len(data)))
        if not chunk:
            return bytes(data)
        data += chunk
    return None


def _read_releases(timeout: float) -> bytes:
    """`releases.json`, as it is on `main`. Like the list of references, it is read from its
    own address or not at all, and says nothing about you beyond the SDK's version."""
    request = urllib.request.Request(
        RELEASES_URL, headers={"User-Agent": f"qte-sdk/{qte_sdk.__version__}"}
    )
    deadline = time.monotonic() + timeout
    try:
        with _open(request, timeout) as response:
            if response.geturl() != RELEASES_URL:
                raise _CannotTell("releases.json was sent elsewhere")
            data = _read_at_most(response, _MAX_RELEASES_SIZE, deadline, timeout)
    except _CannotTell:
        raise
    except Exception as error:
        # Only the kind: an error's text can name a proxy, with its password.
        raise _CannotTell(f"releases.json could not be read ({type(error).__name__})") from None
    if data is None:
        raise _CannotTell("releases.json is longer than expected")
    return data


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


# Recommended updates


class _Release(NamedTuple):
    """An entry of releases.json that is in the expected form."""

    number: tuple[int, int, int]
    recommended: bool
    why: str
    platforms: tuple[str, ...]


def _parse_releases(data: bytes) -> dict[tuple[int, int, int], _Release]:
    """The entries of releases.json that are in the expected form, by version: a JSON list
    of objects, each with `version` ("1.1.1"), `recommended` (true or false), `why` (text,
    not empty when recommended) and `platforms` (a list of `sys.platform` names, empty for
    all). Keys it does not know are ignored, and so is an entry not in that form, or a
    version listed twice. Anything else gives no entries."""
    if len(data) > _MAX_RELEASES_SIZE:
        return {}
    try:
        entries = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        return {}
    if not isinstance(entries, list):
        return {}
    found: dict[tuple[int, int, int], _Release] = {}
    twice: set[tuple[int, int, int]] = set()
    for entry in entries:
        release = _release(entry)
        if release is None:
            continue
        if release.number in found:
            twice.add(release.number)
        found[release.number] = release
    for number in twice:
        del found[number]
    return found


def _release(entry: object) -> _Release | None:
    """One entry of releases.json, or None when it is not in the expected form."""
    if not isinstance(entry, dict):
        return None
    version = entry.get("version")
    recommended = entry.get("recommended")
    why = entry.get("why")
    platforms = entry.get("platforms")
    if not isinstance(version, str) or (match := _VERSION.fullmatch(version)) is None:
        return None
    if not isinstance(recommended, bool) or not isinstance(why, str):
        return None
    if not isinstance(platforms, list) or not all(
        isinstance(name, str) and _PLATFORM.fullmatch(name) for name in platforms
    ):
        return None
    why = _plain(why)
    if recommended and not why:
        return None
    number = (int(match[1]), int(match[2]), int(match[3]))
    return _Release(number, recommended, why, tuple(platforms))


def _plain(text: str) -> str:
    """`text` as it may be shown in a terminal or a chat: one line, with no control or
    formatting characters (such as an escape or a change of writing direction), no full stop
    at the end, since one is added, and at most `_MAX_WHY` characters."""
    text = " ".join(text.split())
    text = "".join(c for c in text if not unicodedata.category(c).startswith("C"))
    text = " ".join(text.split()).rstrip(". ")
    if len(text) > _MAX_WHY:
        text = text[: _MAX_WHY - 3].rstrip() + "..."
    return text


def _applies_here(release: _Release) -> bool:
    return not release.platforms or sys.platform in release.platforms


def _recommendation(
    version: str, releases: dict[str, tuple[tuple[int, int, int], str]], deadline: float
) -> _Release | None:
    """The highest release above `version`, up to the latest release tag, that releases.json
    marks as a recommended update for this platform, read within what is left before
    `deadline`; None for none, or when releases.json cannot be read. Only tagged releases
    count, so the file can never make the check say more than the tags do."""
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        entries = _parse_releases(_fetch(_read_releases, remaining))
        installed = _number(version)
        tagged = {number for number, _ in releases.values()}
        candidates = [
            entry
            for number, entry in entries.items()
            if entry.recommended and number > installed and number in tagged
            if _applies_here(entry)
        ]
        return max(candidates, key=lambda entry: entry.number, default=None)
    except Exception:
        # releases.json only adds to the result: when it cannot be used, the check is as before.
        return None


# The result


def _number(version: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(version)
    assert match is not None  # checked in _installed
    return (int(match[1]), int(match[2]), int(match[3]))


def _latest(releases: dict[str, tuple[tuple[int, int, int], str]]) -> str | None:
    return max(releases, key=lambda tag: releases[tag][0], default=None)


def _behind_message(
    version: str, latest: str, command: str, recommendation: _Release | None
) -> str:
    text = f"{UPDATE_AVAILABLE}: qte-sdk {version} is behind {latest.removeprefix('v')}"
    if recommendation is not None:
        where = ""
        if recommendation.platforms:
            where = f" on {_PLATFORM_NAMES.get(sys.platform, sys.platform)}"
        text += f", a recommended update{where}: {recommendation.why}"
    return f"{text}. Update with {command}."


def _behind(
    version: str,
    latest: str,
    releases: dict[str, tuple[tuple[int, int, int], str]],
    command: str,
    deadline: float,
) -> dict[str, Any]:
    """The fields of a result behind release `latest`, which `command` takes."""
    recommendation = _recommendation(version, releases, deadline)
    return {
        "status": Status.BEHIND,
        "command": command,
        "message": _behind_message(version, latest, command, recommendation),
        "recommended": recommendation is not None,
        "why": None if recommendation is None else recommendation.why,
    }


def _compare(version: str, install: _Install, refs: dict[str, str], deadline: float) -> UpdateCheck:
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
        behind = _behind(version, latest, releases, command, deadline)
        return replace(result(Status.BEHIND, command, ""), **behind)
    relation = "is" if installed == releases[latest][0] else "is newer than"
    text = f"qte-sdk {version} {relation} the latest release, {latest}"
    if follows_main and main is not None and not main_ahead:
        text += ", and main has no newer commits"
    command = update_command(reinstall=True) if main_ahead else None
    return result(Status.CURRENT, command, text + newer)


def _compare_unplaced(
    version: str, unplaced: _Unplaced, timeout: float, deadline: float
) -> UpdateCheck:
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
    return replace(unknown, **_behind(version, latest, releases, command, deadline))


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


# The automatic check

_STAMP = "update-check"
# Held by a program while it decides whether to check, and removed when it is older than
# `_STALE_LOCK` seconds.
_LOCK = "update-check.lock"
_STALE_LOCK = 60.0
_automatic_lock = threading.Lock()
# True once this program has started the automatic check, or called check_for_update.
_automatic_done = False


def check_in_background() -> threading.Thread | None:
    """Start the automatic check in a daemon thread, unless it is turned off
    (`QTE_UPDATE_CHECK=0`) or this program has already started it or called
    `check_for_update`; `open_session` calls it. The thread checks at most once in
    `CHECK_INTERVAL` on this computer, and logs `UpdateCheck.message` at WARNING through
    this module's logger only when the installed SDK is behind a release. Returns the
    thread, or None when none was started. Never raises, and does nothing else in the
    calling thread, so it never delays the caller.

    The warning is written from the check's own thread, never on the session's event loop,
    where a write that cannot complete would stop the session, and with the standard
    stream handlers it never holds up the program's exit either: a stream handler on a
    pipe, socket or terminal gets the line in one write holding no lock, and only when the
    stream can take it whole at once (on Windows, when the pipe reports the room for it,
    which a pipe whose reader is already waiting may not); otherwise, and for a line over
    512 bytes, the line is dropped there, and the next day's check says it again. Every
    other handler, a Windows console's included, gets the record, with its `code`, as
    logging would give it; a console that is paused, or in which text is being selected,
    can hold it until it resumes, as it holds the program's own output, and a handler
    the SDK cannot see into, such as a custom emit or a queue listener, writes as it
    always has. See `_log_without_waiting`."""
    global _automatic_done
    try:
        with _automatic_lock:
            if _automatic_done or not _automatic_wanted():
                return None
            _automatic_done = True
        thread = threading.Thread(
            target=_check_and_log, name="qte-sdk daily update check", daemon=True
        )
        thread.start()
        return thread
    except Exception:
        return None


def _note_checked() -> None:
    global _automatic_done
    with _automatic_lock:
        _automatic_done = True


def _automatic_wanted() -> bool:
    return os.environ.get(UPDATE_CHECK_ENV_VAR, "").strip().lower() not in _OFF


def _check_and_log() -> None:
    """The automatic check, in its own thread: silent unless the SDK is behind a release."""
    try:
        if not _claim_the_day():
            return
        result = check_for_update()
        if result.status is Status.BEHIND:
            _log_without_waiting(logging.WARNING, result.message, UPDATE_AVAILABLE)
    except BaseException:
        # Never into the program: a thread's uncaught error would be printed.
        return


# The longest line written straight to a pipe, socket or terminal, in bytes: POSIX's least
# PIPE_BUF, and macOS's. A pipe that is ready takes a line this long whole, at once.
_MAX_DIRECT_LINE = 512


def _log_without_waiting(
    level: int, message: str, code: str, target: logging.Logger | None = None
) -> None:
    """Log `message` through `target` (by default this module's `logger`), with `code` on
    the record, as `logger.log` would,
    except that the write can never hold up the program. Levels, filters, `propagate`,
    every handler and `logging.lastResort` are honoured, from their public attributes, and
    every handler gets the record through `handle` as usual, except a `StreamHandler` whose
    stream is a pipe, socket or terminal: whatever wrote to such a stream, while it is full,
    would wait holding the handler's lock (and the stream's), and `logging.shutdown()` at
    exit, and every later write to it, would wait for that. Such a stream gets the line
    only when the pipe, socket or terminal can take it whole now (see `_route`); otherwise
    it is dropped there. Python's own stderr, and any stream `open()` makes, gets it in one
    `os.write` on its file descriptor, holding no lock; a stream that wraps one (colorama's,
    rich's, a tee, Jupyter's) gets it through `handle`, so the wrapper writes it. It runs in
    the check's own thread, never touches an event loop, and never changes whether a file
    descriptor blocks.

    Another writer can still fill the pipe between the check and the write. A direct write
    then waits in this thread, holding no Python lock. On Windows, the C runtime's lock on
    that descriptor is held meanwhile, so any later write of the program's own to it, even
    a short one the pipe could have taken, and a flush of what it left in `sys.stderr` at
    exit, wait behind the line until the pipe is read. A program that writes nothing more
    to stderr exits as usual. A wrapper's write, in that race, waits holding the handler's
    lock, and the exit waits for it, as for any write to a full pipe."""
    log = logger if target is None else target
    if log.disabled or not log.isEnabledFor(level):
        return
    try:
        # The caller's file, line and function, as `logger.log` would record them.
        path, line, function, stack = log.findCaller(False, 2)
    except ValueError:
        path, line, function, stack = "(unknown file)", 0, "(unknown function)", None
    record = log.makeRecord(
        log.name, level, path, line, "%s", (message,), None, function, {"code": code}, stack
    )
    passed = log.filter(record)
    if not passed:
        return
    if isinstance(passed, logging.LogRecord):  # a filter may return a new record (3.12+)
        record = passed
    handlers: list[logging.Handler] = []
    node: logging.Logger | None = log
    while node is not None:
        handlers.extend(node.handlers)
        node = node.parent if node.propagate else None
    if not handlers and logging.lastResort is not None:
        handlers = [logging.lastResort]
    later: list[tuple[str, Any, int]] = []
    # Handlers that never wait first, so a write that does wait (another writer filled
    # the pipe after the check) cannot keep the record from a log file or caplog.
    for handler in handlers:
        if record.levelno < handler.level:
            continue
        route, fd = _route(handler)
        if route == "handle":
            handler.handle(record)
        elif route in ("direct", "ready") and fd is not None:
            later.append((route, handler, fd))
    for route, handler, fd in later:
        if route == "direct":
            _write_or_drop(handler, fd, record)
        else:
            _handle_if_ready(handler, fd, record)


def _route(handler: logging.Handler) -> tuple[str, int | None]:
    """How the warning reaches `handler`:

    ("handle", None): through `handler.handle`, as logging would, for a handler that is not
    a stream handler, and for a stream that is a file, a device such as /dev/null, a
    Windows console, or has no usable descriptor;
    ("direct", fd): in one `os.write` on `fd`, for a stream that writes straight to a
    pipe, socket or terminal;
    ("ready", fd): through `handler.handle`, but only when the pipe, socket or terminal
    behind `fd` can take the whole line now, for any other stream whose descriptor is one,
    such as a wrapper of stderr (colorama's, rich's progress display, a tee) or Jupyter's
    stream, whose descriptor is the kernel's own stderr while it writes to the notebook;
    ("skip", None): for a stream handler that has nowhere to write, or could only write by
    waiting."""
    if not isinstance(handler, logging.StreamHandler):
        return "handle", None  # caplog, a queue, JSON or custom handlers
    stream = handler.stream
    if stream is None:
        if not isinstance(handler, logging.FileHandler):
            return "skip", None  # no stderr at all, as under pythonw
        # A file handler with delay=True opens its file when it first writes. Opening a
        # FIFO, socket or terminal there (/dev/stderr, say) could wait, so it is skipped,
        # with nothing opened; a file, or one not made yet, is opened as usual. (Were a
        # FIFO made at a missing path between this look and the open, the open would wait;
        # that race is accepted, rather than lose the line for a log not yet made.)
        return ("skip", None) if _path_may_wait(handler.baseFilename) else ("handle", None)
    try:
        closed = getattr(stream, "closed", False)
    except Exception:
        return "skip", None  # a stream that cannot even say (a detached wrapper)
    if closed is True:
        # Logging would only report the failed write, on stderr, under the handler's lock.
        return "skip", None
    try:
        fd = stream.fileno()
        mode = os.fstat(fd).st_mode
    except Exception:
        return "handle", None
    if sys.platform == "win32":
        # os.fstat reports a pipe as a FIFO. A console (and NUL) is a character device,
        # written through handle(): its writes wait only while it is paused, or text is
        # being selected in it, as the program's own output does.
        may_wait = stat.S_ISFIFO(mode)
    else:
        may_wait = (
            stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or (stat.S_ISCHR(mode) and os.isatty(fd))
        )
    if not may_wait:
        return "handle", None  # a file, or a device such as /dev/null
    try:
        straight = _writes_to_its_fd(stream)
    except Exception:
        straight = False
    return ("direct" if straight else "ready"), fd


def _writes_to_its_fd(stream: Any) -> bool:
    """Whether everything written to `stream` goes to `stream.fileno()`: true only for the
    text stream `open()` and Python's own stdout and stderr are, a TextIOWrapper on a
    BufferedWriter (or, under `python -u`, straight) on a FileIO, checked by exact type.
    Not for any other stream, such as Jupyter's, whose descriptor is the kernel's own
    stderr while its writes go to the notebook."""
    if type(stream) is not io.TextIOWrapper:
        return False
    buffer = stream.buffer
    if type(buffer) is io.BufferedWriter:
        buffer = buffer.raw
    return type(buffer) is io.FileIO


def _path_may_wait(path: str) -> bool:
    """Whether opening and writing the file at `path` could wait for a reader: a FIFO, a
    socket or a terminal (any character device but the null device), on POSIX."""
    if sys.platform == "win32":
        return False
    try:
        found = os.stat(path)
    except OSError:
        return False  # not made yet: opening it makes a file
    if stat.S_ISFIFO(found.st_mode) or stat.S_ISSOCK(found.st_mode):
        return True
    if stat.S_ISCHR(found.st_mode):
        try:
            return not os.path.samestat(found, os.stat(os.devnull))
        except OSError:
            return True
    return False


def _write_or_drop(handler: logging.StreamHandler, fd: int, record: logging.LogRecord) -> None:
    """Write `record` as `handler` would, in one `os.write` on `fd` holding no lock, when
    the stream can take the whole line now; otherwise drop it. Never truncates, and never
    writes to a descriptor the program made non-blocking, where a write could stop part
    way (on Windows, from Python 3.12, which can tell). A short write is not retried,
    since a retry could wait; on a blocking pipe a line of at most 512 bytes is written
    whole, and only a terminal or socket write cut short by a signal could leave part of
    it.

    The line goes straight to the descriptor, so on a stream the program buffers (stdout
    on a pipe, say) it can appear before output the program wrote earlier but has not yet
    flushed."""
    try:
        passed = handler.filter(record)
        if not passed:
            return
        if isinstance(passed, logging.LogRecord):
            record = passed
        data = _encoded(handler, handler.format(record) + handler.terminator)
        if len(data) > _MAX_DIRECT_LINE:
            return
        try:
            if not os.get_blocking(fd):
                return
        except (AttributeError, OSError):
            pass  # Windows before 3.12 cannot say for a pipe; one made with CreatePipe blocks
        if _can_take(fd, len(data)):
            os.write(fd, data)
    except Exception:
        return


def _handle_if_ready(handler: logging.StreamHandler, fd: int, record: logging.LogRecord) -> None:
    """Write `record` to `handler`'s stream (a wrapper), as `handle` and `emit` would, only
    when the line is at most 512 bytes and the pipe, socket or terminal behind `fd` can
    take it whole now; otherwise drop it. The handler's filters run once, before the line
    is measured, and the line written is the one measured. The wrapper then passes on at
    most that much, so its write does not wait, unless another writer fills the pipe
    between this check and that write, or the wrapper holds back output of the program's
    own that it writes with the line (rich keeps a partial line until it ends): then the
    write waits holding the handler's lock, and the exit waits for it, as for any write to
    a full pipe. As with a direct write, a `StreamHandler` subclass's own `emit` is not
    called; its filters, formatter and terminator are used."""
    try:
        passed = handler.filter(record)
        if not passed:
            return
        if isinstance(passed, logging.LogRecord):
            record = passed
        text = handler.format(record) + handler.terminator
        data = _encoded(handler, text)
        if len(data) > _MAX_DIRECT_LINE or not _can_take(fd, len(data)):
            return
        handler.acquire()
        try:
            handler.stream.write(text)
            handler.flush()
        finally:
            handler.release()
    except Exception:
        return


def _encoded(handler: logging.StreamHandler, text: str) -> bytes:
    """The bytes `text` becomes on `handler`'s stream's descriptor: in its encoding (with
    backslashreplace, as Python's stderr writes it), and with the line ending a text
    stream writes on Windows."""
    if sys.platform == "win32":
        text = text.replace("\n", "\r\n")
    encoding = getattr(handler.stream, "encoding", None) or "utf-8"
    return text.encode(encoding, "backslashreplace")


def _can_take(fd: int, size: int) -> bool:
    """Whether `fd` can take `size` bytes now (at most `_MAX_DIRECT_LINE`), without waiting.
    False when it cannot tell."""
    if sys.platform == "win32":
        room = _pipe_write_quota(fd)
        return room is not None and room >= size
    try:
        # Writable means at least PIPE_BUF bytes free for a pipe (512 on macOS, a page on
        # Linux), so a line of at most 512 bytes is written whole without waiting.
        return bool(select.select([], [fd], [], 0)[1])
    except (OSError, ValueError):  # ValueError: a descriptor too high for select
        return False


def _pipe_write_quota(fd: int) -> int | None:
    """How many bytes the Windows pipe behind `fd` takes now without waiting: its
    `WriteQuotaAvailable`, from `NtQueryInformationFile(FilePipeLocalInformation)`
    (documented in the Windows Driver Kit). None when it cannot be read. A read waiting
    on the pipe takes its size off this, so it can be less than the pipe would take: the
    caller then drops a line it could have written rather than one the pipe has no room for. Nothing
    else the write end reports tells the two apart: measured on CI's Windows runner, its
    `ReadDataAvailable` is always 0, and a full 4 KiB pipe and an empty one with an 8 KiB
    read waiting report the same fields, `WriteQuotaAvailable` 0 in both."""
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class IoStatusBlock(ctypes.Structure):
            # IO_STATUS_BLOCK: a union of NTSTATUS Status and PVOID Pointer, then
            # ULONG_PTR Information; both pointer-sized.
            _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

        class FilePipeLocalInformation(ctypes.Structure):
            # FILE_PIPE_LOCAL_INFORMATION: ten ULONGs.
            _fields_ = [
                (name, wintypes.ULONG)
                for name in (
                    "NamedPipeType",
                    "NamedPipeConfiguration",
                    "MaximumInstances",
                    "CurrentInstances",
                    "InboundQuota",
                    "ReadDataAvailable",
                    "OutboundQuota",
                    "WriteQuotaAvailable",
                    "NamedPipeState",
                    "NamedPipeEnd",
                )
            ]

        query = ctypes.WinDLL("ntdll").NtQueryInformationFile
        query.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(IoStatusBlock),
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.c_int,  # FILE_INFORMATION_CLASS
        ]
        query.restype = ctypes.c_long  # NTSTATUS
        handle = msvcrt.get_osfhandle(fd)
        status_block = IoStatusBlock()
        info = FilePipeLocalInformation()
        file_pipe_local_information = 24
        status = query(
            handle,
            ctypes.byref(status_block),
            ctypes.byref(info),
            ctypes.sizeof(info),
            file_pipe_local_information,
        )
        if status != 0:  # anything but STATUS_SUCCESS
            return None
        return int(info.WriteQuotaAvailable)
    except Exception:
        return None


def _claim_the_day() -> bool:
    """Whether the automatic check may run now: true when no check has started on this
    computer within `CHECK_INTERVAL`, recording now as the time of the last one before
    saying so. False when the time cannot be recorded, so a check that cannot be counted
    never runs, and while another program is deciding the same, so two programs started
    together do not both check."""
    folder = _cache_dir()
    if folder is None:
        return False
    lock = folder / _LOCK
    try:
        folder.mkdir(parents=True, exist_ok=True)
        held = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # Another program is deciding. A lock left by one that stopped part way is removed
        # once it is old, for the next program, or dated in the future, as one left before
        # the clock was set back is. Two programs can both find it old, and the second can
        # then remove a fresh lock a third has just taken. That race is accepted: its only
        # effect is that more than one program may check GitHub that day.
        try:
            if abs(time.time() - lock.stat().st_mtime) > _STALE_LOCK:
                lock.unlink()
        except OSError:
            pass
        return False
    except OSError:
        return False
    try:
        return _claim_with_the_lock(folder / _STAMP)
    finally:
        os.close(held)
        try:
            lock.unlink()
        except OSError:
            pass


def _claim_with_the_lock(stamp: Path) -> bool:
    now = time.time()
    try:
        with open(stamp, "rb") as file:
            last = float(file.read(64).decode("ascii").strip())
    except (OSError, ValueError, UnicodeDecodeError):
        last = math.nan
    if math.isfinite(last) and 0 <= now - last < CHECK_INTERVAL:
        return False
    try:
        # Rounded down, so the next check never finds it in the future.
        stamp.write_text(f"{math.floor(now)}\n", encoding="ascii")
    except OSError:
        return False
    return True


def _cache_dir() -> Path | None:
    """This user's cache folder for the SDK: under `%LOCALAPPDATA%` on Windows,
    `~/Library/Caches` on macOS, and `$XDG_CACHE_HOME` (when it is an absolute path) or
    `~/.cache` elsewhere. None when there is no home folder to put it in."""
    try:
        if sys.platform == "win32":
            local = os.environ.get("LOCALAPPDATA")
            base = Path(local) if local else Path.home() / "AppData" / "Local"
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Caches"
        else:
            xdg = os.environ.get("XDG_CACHE_HOME")
            base = Path(xdg) if xdg and os.path.isabs(xdg) else Path.home() / ".cache"
    except (RuntimeError, OSError, KeyError):
        return None
    return base / "qte-sdk"


# The command


def main(argv: list[str] | None = None) -> int:
    """Check, print the result and return its exit status."""
    parser = argparse.ArgumentParser(
        prog="python -m qte_sdk.update",
        description="Say whether the installed qte-sdk is the latest release.",
    )
    parser.color = False  # Python 3.14 colours argparse's output; it is read as plain text.
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
