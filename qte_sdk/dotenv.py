"""Read `QTE_URL` and `QTE_TOKEN` from a `.env` file in the working directory.

The SDK falls back to this file only when nothing else gives a value: see
`qte_sdk.session.resolve_token` and `qte_sdk.session.resolve_url`. It reads `./.env` (the
working directory only, never a parent directory), takes only `QTE_URL` and `QTE_TOKEN`
from it, and never changes `os.environ`, so a real environment variable always wins.

The format is the common one:

    # a comment
    QTE_URL=ws://127.0.0.1:8080/ws
    export QTE_TOKEN='...'

Blank lines and comment lines are skipped, and an `export ` prefix is allowed. A value may
be unquoted, or in single or double quotes on one line; it is taken as written, with no
escapes and no `$VARIABLE` expansion. An unquoted value ends at whitespace followed by `#`,
and may not hold whitespace or quotes. If a name appears more than once, the last line
wins. Lines for other names are not read, so the rest of the file may use any syntax.

Three safeguards apply:

- On POSIX, a `.env` that holds `QTE_TOKEN` and that other users can read is refused, and
  nothing in it is used (`chmod 600 .env` fixes it).
- On Windows, where files have access lists rather than modes, a `TokenFileShared`
  warning is issued, once per process for each file, if the access list of a `.env` that
  holds `QTE_TOKEN` lets a broad group read or change it: Everyone, Authenticated Users,
  Users, INTERACTIVE or Domain Users. It is issued too if the access list of the file's
  folder lets such a group add or remove files there, since they could then replace the
  file with one of their own, or if the file's owner, or its folder's, is another account
  than you, Administrators, SYSTEM or Windows' own TrustedInstaller, since an owner can
  change who may open the file, or add or remove files in the folder. An
  `AddressFileShared` warning is issued instead if
  such a group may change or replace, or another account owns, a `.env` that sets only
  `QTE_URL`, since whoever changes the address can capture a token kept elsewhere when you
  next connect. Both are kinds of `FileShared`. A folder under your user profile is
  private by default; a folder on another drive, such as `D:\\`, usually is not. When
  the `.env` is reached through symbolic links or junctions, the file and the folder
  checked are those they lead to, and each folder that holds a link on the way, and its
  owner, is checked too. If a link cannot be followed, or Windows will not let the check
  see the access list of the file or of a folder it looks at, or the list is in a form it
  cannot read, or its owner cannot be told, the warning says the file could not be fully
  checked, and why. The file is
  still used, though a later release will refuse it. The same check applies to the file
  named by `QTE_TOKEN_FILE` (see `qte_sdk.session`). The warning's code is
  `QTE-TOKEN-SHARED` or `QTE-ADDRESS-SHARED`, or `QTE-TOKEN-UNCHECKED` or
  `QTE-ADDRESS-UNCHECKED` when the only finding is that it could not be fully checked.
- If the `.env`, or the file it links to, is inside a git working tree and git tracks
  it or does not ignore it, a `DotenvNotIgnored` warning is issued, once per process,
  since the token could be committed. It never stops the SDK: if a warnings filter makes
  it an error, it is logged instead. The check runs `git ls-files` and `git check-ignore`
  when git is on the PATH and is skipped otherwise; git is given only the environment
  variables it needs, never the token. Its code is `QTE-DOTENV-TRACKED` when git tracks
  the file and `QTE-DOTENV-NOT-IGNORED` otherwise.

Nothing here raises for a bad file, logs, or returns any of the file's text other than the
one value asked for: a problem is described by line number and name only. Each warning
carries its code in `code` and at the start of its message, and a warning logged instead
carries it as the record's `code`: docs/errors.md says what each code means.
"""

import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import unicodedata
import warnings
from collections.abc import Sequence
from pathlib import Path

from qte_sdk import _fileaccess
from qte_sdk import errors as _errors
from qte_sdk.errors import Problem, QteWarning, summary

__all__ = [
    "DOTENV_NAME",
    "AddressFileShared",
    "DotenvNotIgnored",
    "FileShared",
    "TokenFileShared",
    "parse_assignment",
]

DOTENV_NAME = ".env"
_GIT_TIMEOUT = 5.0
_INLINE_COMMENT = re.compile(r"(?:^|\s)#")
MAX_DOTENV_SIZE = 64 * 1024
_TOKEN_NAME = "QTE_TOKEN"
_URL_NAME = "QTE_URL"
# What cmd (%NAME%, and !NAME! with delayed expansion) or PowerShell ($, the backtick, and
# its curly double quotes) treats specially inside double quotes.
_UNSAFE_IN_DOUBLE_QUOTES = "%!$`\u201c\u201d\u201e"
# A drive's root, such as D:\, which is given to icacls without quotes.
_DRIVE_ROOT = re.compile(r"[A-Za-z]:\\")
# What git needs to run and find your git configuration: nothing else is passed to it.
_GIT_ENV = frozenset(
    {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "XDG_CONFIG_HOME", "LANG", "LC_ALL", "TMPDIR"}
)

_log = logging.getLogger(__name__)
_git_checked: set[str] = set()  # the .env files already checked, so each warns once
_shared_warned: set[str] = set()  # the token files already warned about on Windows


class DotenvNotIgnored(QteWarning):
    """The `.env` the SDK read is inside a git working tree and git does not ignore it
    (code `QTE-DOTENV-NOT-IGNORED`), or tracks it (`QTE-DOTENV-TRACKED`)."""

    code = _errors.DOTENV_NOT_IGNORED


class FileShared(QteWarning):
    """On Windows, a broad group of users, such as Everyone or Users, may read, change or
    replace a file the SDK reads its setup from, or another account owns it or a folder it
    is in or reached through; or it could not be fully checked, since a link on the way to
    it could not be followed or an access list could not be seen. Catch this to handle both
    kinds below."""

    code = _errors.TOKEN_SHARED


class TokenFileShared(FileShared):
    """On Windows, a broad group of users may read, change or replace the `.env`, or the
    file named by `QTE_TOKEN_FILE`, that holds the token, or another account owns it or a
    folder it is in (code `QTE-TOKEN-SHARED`), or it could not be fully checked
    (`QTE-TOKEN-UNCHECKED`)."""

    code = _errors.TOKEN_SHARED


class AddressFileShared(FileShared):
    """On Windows, a broad group of users may change or replace a `.env` that sets
    `QTE_URL` but holds no token, or another account owns it or a folder it is in, or it
    could not be fully checked. Whoever changes the address could capture a token kept
    elsewhere. Code `QTE-ADDRESS-SHARED`, or `QTE-ADDRESS-UNCHECKED` when it could only not
    be fully checked."""

    code = _errors.ADDRESS_SHARED


def dotenv_path() -> Path:
    """The `.env` the SDK reads: the one in the working directory."""
    return Path.cwd() / DOTENV_NAME


def parse_assignment(line: str) -> tuple[str, str] | None:
    """The name and the raw text after `=` of a line that assigns a variable, or None for a
    blank line, a comment or a line with no `=`. An `export ` prefix is removed."""
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    if text.startswith("export") and text[6:7].isspace():
        text = text[6:].lstrip()
    name, equals, rest = text.partition("=")
    if not equals:
        return None
    return name.rstrip(), rest


def _parse_value(rest: str) -> tuple[str | None, str | None]:
    """The value written after `=`, and None; or None and what is wrong with it."""
    rest = rest.strip()
    if rest[:1] in ("'", '"'):
        quote = rest[0]
        end = rest.find(quote, 1)
        if end == -1:
            return None, "has an opening quote with no closing quote on the same line"
        after = rest[end + 1 :].strip()
        if after and not after.startswith("#"):
            return None, "has text after its closing quote"
        return rest[1:end], None
    comment = _INLINE_COMMENT.search(rest)
    if comment is not None:
        rest = rest[: comment.start()].rstrip()
    if any(c.isspace() for c in rest):
        return None, "has whitespace in a value without quotes"
    if "'" in rest or '"' in rest:
        return None, "has a quote inside a value without quotes"
    return rest, None


def _parse(text: str, name: str) -> tuple[str | None, str | None]:
    """The value of `name` in the text of a `.env`, and None; or None and a problem, which
    names the line by number only. (None, None) if `name` is not assigned."""
    value: str | None = None
    problem: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        assignment = parse_assignment(line)
        if assignment is None or assignment[0] != name:
            continue
        value, problem = _parse_value(assignment[1])
        if problem is not None:
            problem = Problem(
                f"line {number}: {name} {problem}",
                _errors.DOTENV_INVALID,
                line=number,
                name=name,
                problem=problem,
            )
    return value, problem


def read_value(name: str) -> tuple[str | None, str | None]:
    """The value of `name` in `./.env`, and None; or None and what is wrong, as an
    `errors.Problem`, which is text that carries the code of the message to raise.

    (None, None) if there is no `./.env` or it does not assign `name`, or assigns it an
    empty value. On POSIX, a file that assigns `QTE_TOKEN` and that other users can read is
    refused whichever name is asked for, and no value is returned. On Windows, such a file
    that a broad group may read, change or replace, or a file setting `QTE_URL` that a
    broad group may change or replace, gives a `TokenFileShared` or `AddressFileShared`
    warning and is still used, as does either kind owned by another account, or in a
    folder another account owns, or that could not be fully checked.

    Never raises for a bad file: a `UnicodeDecodeError` keeps the bytes it rejected, so
    neither it nor an `OSError` may reach the caller's exception as its cause or context.
    """
    path = dotenv_path()
    _warn_if_not_ignored(path)
    # Read before the file is, so no frame that holds the token calls the Windows API.
    access = shared_access(path)
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None, None
    except OSError as error:
        return None, _unreadable(f"cannot be read ({error.strerror or type(error).__name__})", name)
    problem = None
    data = b""
    try:
        with os.fdopen(descriptor, "rb") as file:
            # Checked on the open file, before reading it: a symbolic link is judged by
            # the file it points to, and a FIFO or device is never read.
            mode = os.fstat(file.fileno()).st_mode
            if not stat.S_ISREG(mode):
                problem = "is not a regular file"
            else:
                data = file.read(MAX_DOTENV_SIZE + 1)
    except OSError as error:
        problem = f"cannot be read ({error.strerror or type(error).__name__})"
    if problem is not None:
        return None, _unreadable(problem, name)
    if len(data) > MAX_DOTENV_SIZE:
        return None, _unreadable(f"is larger than {MAX_DOTENV_SIZE // 1024} KiB", name)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = None
    del data
    if text is None:
        return None, _unreadable("is not UTF-8 text", name)
    holds_token = _assigns(text, _TOKEN_NAME)
    if os.name == "posix" and stat.S_IMODE(mode) & 0o044 and holds_token:
        return None, Problem(
            f"holds {_TOKEN_NAME} but other users can read it; run `chmod 600 {DOTENV_NAME}` "
            "so only you can",
            _errors.TOKEN_SHARED,
        )
    warn = access is not None and (
        (holds_token and access)
        or ((access.changeable or access.incomplete) and _assigns(text, _URL_NAME))
    )
    value, problem = _parse(text, name)
    del text  # released before the warning, which runs code that is not the SDK's
    if warn:
        assert access is not None
        failure = warn_shared(path, access, holds_token=holds_token, sets_address=True)
        if failure is not None:
            del value
            raise failure  # from a frame that no longer holds the token
    if problem is not None:
        return None, problem
    return value or None, None


def _unreadable(problem: str, name: str) -> Problem:
    return Problem(problem, _errors.DOTENV_UNREADABLE, problem=problem, name=name)


def _assigns(text: str, name: str) -> bool:
    """Whether some line of `text` gives `name` a value, well formed or not."""
    for line in text.splitlines():
        assignment = parse_assignment(line)
        if assignment is not None and assignment[0] == name:
            value, problem = _parse_value(assignment[1])
            if problem is not None or value:
                return True
    return False


def _warn_if_not_ignored(path: Path) -> None:
    """Warn, once per process for each `.env`, if `path`, or the file it links to, is inside
    a git working tree and git tracks it or does not ignore it. Called before the file is
    read, so no frame on the stack holds the token. If a warnings filter turns the warning
    into an error, it is logged instead, since this check must never stop the SDK."""
    key = str(path)
    if key in _git_checked or not path.exists():
        return
    _git_checked.add(key)
    candidates = [path]
    if path.is_symlink():
        candidates.append(path.resolve())
    # Every remedy is given: fixing the link alone would leave a tracked target exposed.
    exposures = [m for m in map(_git_exposure, candidates) if m is not None]
    if not exposures:
        return
    tracked = any(each == _errors.DOTENV_TRACKED for each, _ in exposures)
    code = _errors.DOTENV_TRACKED if tracked else _errors.DOTENV_NOT_IGNORED
    warning = DotenvNotIgnored(
        " ".join(summary(each, **fields) for each, fields in exposures), code=code
    )
    try:
        warnings.warn(warning, stacklevel=_caller_level())
    except Warning:
        _log.warning("%s", warning, extra={"code": code})


def shared_access(path: Path) -> "_fileaccess.BroadAccess | None":
    """The broad groups Windows lets read or change `path`, or add or remove files in its
    folder, whether another account owns it or a folder looked at, and what could not be
    seen (see `_fileaccess.broad_access`), if this
    is Windows, the SDK has not already warned about `path` in this process, and the access
    list can be read; None otherwise. Takes only the path, so call it before the file is
    read."""
    if not _fileaccess.on_windows() or os.path.abspath(path) in _shared_warned:
        return None
    return _fileaccess.broad_access(path)


def shared_code(access: "_fileaccess.BroadAccess", *, holds_token: bool = True) -> str:
    """The code for what `shared_message` says: `QTE-TOKEN-SHARED` or `QTE-ADDRESS-SHARED`
    if a broad group may read (a file that holds the token), change or replace the file, or
    another account owns it or a folder looked at; `QTE-TOKEN-UNCHECKED` or
    `QTE-ADDRESS-UNCHECKED` if it could only not be fully checked."""
    found = access.changeable or (holds_token and bool(access.read))
    if holds_token:
        return _errors.TOKEN_SHARED if found else _errors.TOKEN_UNCHECKED
    return _errors.ADDRESS_SHARED if found else _errors.ADDRESS_UNCHECKED


def shared_message(
    path: Path,
    access: "_fileaccess.BroadAccess",
    *,
    holds_token: bool = True,
    sets_address: bool = True,
) -> str:
    """What to tell the person when broad groups of users may read, change or replace
    `path`, or another account owns it or a folder looked at, or it could not be fully
    checked. `path` holds the token if `holds_token`, and is a `.env` that can set the
    exchange address if `sets_address`. The text has no code: see `shared_code`. Names
    only the path, and when it is reached through links, the links and the file they lead
    to; their folders; and the groups; and, if a link on the way could not be followed or a
    list could not be seen, why. Resolves no path: where the lists were read is taken from
    `access`."""
    changers = list(access.write)
    readers = [group for group in access.read if group not in changers] if holds_token else []
    replacers = list(access.folder or ())
    link_replacers = list(access.link_folder or ())
    file = access.file or os.path.abspath(path)
    folder = access.folder_path or os.path.dirname(os.path.abspath(path))
    links = list(access.links)
    linked = bool(links)
    # Whether `path` is itself a link, and the links met after it (or all, if it is not).
    is_link = linked and _fileaccess._same(links[0], os.path.abspath(path))
    via = links[1:] if is_link else links
    verb = "leads to" if via else "links to"
    link_folders = list(access.link_folders or ())
    granted = []
    if changers:
        granted.append(f"{_join(changers)} {'read or change' if holds_token else 'change'} it")
    if readers:
        granted.append(f"{_join(readers)} read it")
    findings = []
    if granted:
        findings.append(f"Windows lets {', and '.join(granted)}")
    places = []
    if replacers:
        places.append(f"{_join(replacers)} may add or remove files in {folder}")
        if linked:
            places[-1] += f", which holds the file it {verb}"

    def holding(held: list[str]) -> str:
        """The links in a folder, as the message names them after "which holds"."""
        unlooked = [name for name in held if name == access.unlooked]
        confirmed = [name for name in held if name != access.unlooked]
        named = []
        if confirmed:
            named.append(
                "the link" if is_link and confirmed == links[:1] else _the_links(confirmed)
            )
        named += [f"{name}, which could not be looked at" for name in unlooked]
        return _join(named)

    for holder in link_folders:
        if holder.groups:
            the_links = holding(list(holder.links))
            places.append(
                f"{_join(list(holder.groups))} may add or remove files in {holder.path}, "
                f"which holds {the_links}"
            )
    if places:
        findings.append(f"other users can replace it: {', and '.join(places)}")
    if access.other_owner:
        findings.append("it is owned by another account, which can change who may open it")
    owned = []
    if access.folder_owner:
        owned.append(f"{folder}, which holds the file it {verb}," if linked else folder)
    for holder in link_folders:
        if holder.other_owner:
            owned.append(f"{holder.path}, which holds {holding(list(holder.links))},")
    for place in owned:
        findings.append(
            f"{place} is owned by another account, which can change who may add or remove "
            "files in it"
        )
    risks = []
    if holds_token and (access.read or access.other_owner):
        risks.append("read your token")
    if access.changeable and sets_address:
        risks.append(
            f"change {_URL_NAME} in it to a server of their own, which would capture your "
            "token when you next connect"
        )
    elif access.changeable:
        risks.append("replace your token")
    groups = set(changers + readers + replacers + link_replacers)
    if not findings and linked:
        fix = (
            "Keep the file itself, not a link to it, in a folder under your user profile "
            "(%USERPROFILE%), which is private by default."
        )
    elif access.other_owner or not findings:
        # Another account owns the file, or may have hidden who can open it.
        it = f"the file it {verb}" if linked else "it"
        fix = (
            f"Delete {it} and make it again yourself, in a folder under your user profile "
            "(%USERPROFILE%), which is private by default."
        )
    else:
        it = f"the file it {verb}, and the link{'s' if len(links) > 1 else ''}," if linked else "it"
        their = "that group's" if len(groups) == 1 else "those groups'"
        fix = (
            f"Move {it} into a folder under your user profile (%USERPROFILE%), which is "
            "private by default"
        )
        # Removing a group's access is enough only when no other account owns a folder
        # looked at, which could give it back, and every list was seen.
        enough = not (access.folder_owner or access.link_owner or access.unseen)
        fix += f", or remove {their} access." if groups and enough else "."
    what = "holds your token" if holds_token else f"sets {_URL_NAME}, the exchange address"
    name = f"{path}"
    if access.unfollowed:
        # Where the links lead is not known, so no file is named as their end. (A list that
        # was not seen does not change where they lead.)
        if is_link and via:
            name = f"{path}, a link that leads on through {_the_links(via)},"
        elif is_link:
            name = f"{path}, a link,"
        elif via:
            name = f"{path}, reached through {_the_links(via)},"
    elif is_link and via:
        name = f"{path}, a link that leads to {file} through {_the_links(via)},"
    elif is_link:
        name = f"{path}, a link to {file},"
    elif via:
        name = f"{path}, which leads to {file} through {_the_links(via)},"
    reasons = list(access.unseen)
    if access.unfollowed:
        reasons.append(
            f"{access.unfollowed}; a link on the way could not be followed, so check where it leads"
        )
    unchecked = f"it could not be fully checked: {_join(reasons, '; ')}"
    if findings:
        said = (
            f"{name} {what}, and {_join(findings, '; ')}, so other people who use this "
            f"computer could {' or '.join(risks)}."
        )
        if access.incomplete:
            said += f" Also, {unchecked}."
    else:
        said = f"{name} {what}, but {unchecked}."
    holders = [holder.path for holder in link_folders]
    folders = "folders" if holders else "folder"
    # The SDK still uses such a file (see the module's docstring), so this says why it
    # matters before the fix and the command that ends the message.
    return (
        f"{said} A later release will refuse such a file. {fix} To see who can open the "
        f"{folders} and the file, {_icacls(folder, file, holders)}."
    )


def _the_links(links: list[str]) -> str:
    """ "the link A", or "the links A and B"."""
    return f"the link {links[0]}" if len(links) == 1 else f"the links {_join(links)}"


def _icacls(folder: str, file: str, link_folders: Sequence[str] = ()) -> str:
    """How to run icacls on `folder` and on `file`, the file in it, and on `link_folders`,
    the folders that hold the links on the way to the file, if any. A path with characters
    cmd or PowerShell would expand or end a quote at inside double quotes is not put in a
    command, so the command never names another folder; nor is one that ends in a
    backslash, where the closing quote would be taken as part of the path, except a drive's
    root, such as `D:\\`, which needs no quotes."""

    def command(target: str) -> str | None:
        if _DRIVE_ROOT.fullmatch(target):
            return f"`icacls {target}`"
        if any(c in target for c in _UNSAFE_IN_DOUBLE_QUOTES) or target.endswith(("\\", "/")):
            return None
        return f'`icacls "{target}"`'

    holders = list(link_folders)
    commands = [command(target) for target in [*holders, folder, file]]
    if holders and None in commands:
        which = "folder that holds the link" if len(holders) == 1 else "folders that hold the links"
        return (
            f"run icacls on the {which}, {_join(holders)}, on the folder that holds the file, "
            f"{folder}, and on the file, {file}"
        )
    if None in commands:
        return f"run icacls on the folder that holds it, {folder}, and on the file itself"
    return f"run {_join([c for c in commands if c is not None])}"


def warn_shared(
    path: Path,
    access: "_fileaccess.BroadAccess",
    *,
    holds_token: bool = True,
    sets_address: bool = True,
) -> BaseException | None:
    """Issue a `TokenFileShared` warning about `path`, or an `AddressFileShared` one if it
    does not hold the token (see `shared_message`), once per process for each path. If a
    warnings filter turns it into an error, it is logged instead, since this check must
    never stop the SDK.

    Its caller may hold the token, so nothing is raised from here. An error, from the
    warning or from a failure to show it (a closed stderr, say), is dropped. An
    interruption, such as a `KeyboardInterrupt` while the warning is shown, is returned
    without its traceback or chain, for the caller to raise once it has let go of the
    token; the warning is then given again next time. None otherwise."""
    key = None
    try:
        key = os.path.abspath(path)
        if key in _shared_warned:
            return None
        _shared_warned.add(key)
        message = shared_message(path, access, holds_token=holds_token, sets_address=sets_address)
        code = shared_code(access, holds_token=holds_token)
        category = TokenFileShared if holds_token else AddressFileShared
        warning = category(message, code=code)
        try:
            warnings.warn(warning, stacklevel=_caller_level())
        except Warning:
            _log.warning("%s", warning, extra={"code": code})
    except Exception:
        pass
    except BaseException as error:
        if key is not None:
            _shared_warned.discard(key)
        return _detached(error)
    return None


def _detached(error: BaseException) -> BaseException:
    """`error` without its traceback or chain, so the frames it came through, which a
    traceback that shows locals would display, go with neither."""
    error = error.with_traceback(None)
    error.__cause__ = error.__context__ = None
    return error


def _join(names: list[str], separator: str = ", ") -> str:
    """`names` as a list in a sentence: "A", "A and B" or "A, B and C". Clauses that have
    commas of their own are separated by `separator` "; " instead: "A; B; and C"."""
    if len(names) <= 1:
        return "".join(names)
    last = " and " if separator == ", " else f"{separator}and "
    return separator.join(names[:-1]) + last + names[-1]


def _caller_level() -> int:
    """The `stacklevel` that points a warning issued by the caller of this function at the
    first frame outside the SDK."""
    package = str(Path(__file__).resolve().parent) + os.sep
    level = 1
    frame = sys._getframe(1)
    while frame is not None and str(Path(frame.f_code.co_filename).resolve()).startswith(package):
        level += 1
        frame = frame.f_back
    return level


def _git_exposure(path: Path) -> tuple[str, dict[str, object]] | None:
    """What could let git commit `path`, as the code and fields of its message, or None if
    nothing could or git cannot tell."""
    if not is_inside_git_work_tree(path.parent):
        return None
    if is_tracked_by_git(path):
        command = f"`git rm --cached {path.name}` in {path.parent}"
        return _errors.DOTENV_TRACKED, {"path": path, "name": path.name, "command": command}
    if is_ignored_by_git(path) is False:
        return _errors.DOTENV_NOT_IGNORED, {"path": path, "name": path.name}
    return None


def is_inside_git_work_tree(directory: Path) -> bool:
    """Whether `directory` or one of its parents holds a `.git` entry."""
    return any((parent / ".git").exists() for parent in (directory, *directory.parents))


def is_ignored_by_git(path: Path) -> bool | None:
    """True if git ignores `path`, False if it does not (a tracked file counts as not
    ignored), or None if that cannot be told, for example when git is not on the PATH.

    Git is given only the few environment variables it needs to find itself and your git
    configuration, so no token held in another variable reaches it."""
    result = _git(path, "check-ignore", "-q", "--", path.name)
    if result == 0:
        return True
    if result == 1:
        return False
    return None


def is_tracked_by_git(path: Path) -> bool | None:
    """True if git tracks `path`, False if it does not, or None if that cannot be told."""
    result = _git(path, "ls-files", "--error-unmatch", "--", path.name)
    if result == 0:
        return True
    if result == 1:
        return False
    return None


def tracked_ignoring_case(path: Path) -> list[str] | None:
    """The paths in git's index, relative to the working tree's top, that name `path` when
    case and Unicode normalization are ignored in every part of it (the index, so a tracked
    file deleted from disk is included); or None if that cannot be told. `path`'s directory
    must exist.

    On a filesystem that ignores case, `config/readme.md` is the file git tracks as
    `Config/README.md`, even after a rename, and git compares paths exactly, so the whole
    index is listed and each path compared after Unicode normalization and case folding."""
    directory = path.parent.resolve()
    top = _run_git(directory, "rev-parse", "--show-toplevel", capture=True)
    if top is None or top.returncode != 0:
        return None
    # Only git's terminating newline is removed: spaces may belong to the path.
    printed = top.stdout.decode("utf-8", "surrogateescape").removesuffix("\n")
    if not printed:
        return None
    root = Path(printed).resolve()
    try:
        relative = (directory / path.name).relative_to(root)
    except ValueError:
        return None
    result = _run_git(root, "ls-files", "-z", capture=True)
    if result is None or result.returncode != 0:
        return None
    wanted = _fold(relative.as_posix())
    entries = result.stdout.decode("utf-8", "surrogateescape").split("\0")
    return [entry for entry in entries if entry and _fold(entry) == wanted]


def _fold(text: str) -> str:
    """`text` as a filesystem that ignores case and Unicode normalization compares it."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFD", text).casefold())


def _git(path: Path, *args: str) -> int | None:
    """Run git with `args` in the directory of `path` and return its exit status, or None if
    git is not on the PATH or could not be run."""
    result = _run_git(path.parent, *args)
    return None if result is None else result.returncode


def _run_git(
    directory: Path, *args: str, capture: bool = False
) -> "subprocess.CompletedProcess[bytes] | None":
    """Run git with `args` in `directory`, or return None if git is not on the PATH or could
    not be run. Git is given only the environment variables in `_GIT_ENV`."""
    git = shutil.which("git")
    if git is None:
        return None
    env = {k: v for k, v in os.environ.items() if k in _GIT_ENV}
    try:
        return subprocess.run(
            [git, *args],
            cwd=directory,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
