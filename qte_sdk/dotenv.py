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

Two safeguards apply:

- On POSIX, a `.env` that holds `QTE_TOKEN` and that other users can read is refused, and
  nothing in it is used (`chmod 600 .env` fixes it).
- If the `.env`, or the file it links to, is inside a git working tree and git tracks
  it or does not ignore it, a `DotenvNotIgnored` warning is issued, once per process,
  since the token could be committed. It never stops the SDK: if a warnings filter makes
  it an error, it is logged instead. The check runs `git ls-files` and `git check-ignore`
  when git is on the PATH and is skipped otherwise; git is given only the environment
  variables it needs, never the token.

Nothing here raises for a bad file, logs, or returns any of the file's text other than the
one value asked for: a problem is described by line number and name only.
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
from pathlib import Path

__all__ = ["DOTENV_NAME", "DotenvNotIgnored", "parse_assignment"]

DOTENV_NAME = ".env"
_GIT_TIMEOUT = 5.0
_INLINE_COMMENT = re.compile(r"(?:^|\s)#")
MAX_DOTENV_SIZE = 64 * 1024
_TOKEN_NAME = "QTE_TOKEN"
# What git needs to run and find your git configuration: nothing else is passed to it.
_GIT_ENV = frozenset(
    {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "XDG_CONFIG_HOME", "LANG", "LC_ALL", "TMPDIR"}
)

_log = logging.getLogger(__name__)
_git_checked: set[str] = set()  # the .env files already checked, so each warns once


class DotenvNotIgnored(UserWarning):
    """The `.env` the SDK read is inside a git working tree and git does not ignore it."""


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
            problem = f"line {number}: {name} {problem}"
    return value, problem


def read_value(name: str) -> tuple[str | None, str | None]:
    """The value of `name` in `./.env`, and None; or None and what is wrong.

    (None, None) if there is no `./.env` or it does not assign `name`, or assigns it an
    empty value. On POSIX, a file that assigns `QTE_TOKEN` and that other users can read is
    refused whichever name is asked for, and no value is returned.

    Never raises for a bad file: a `UnicodeDecodeError` keeps the bytes it rejected, so
    neither it nor an `OSError` may reach the caller's exception as its cause or context.
    """
    path = dotenv_path()
    _warn_if_not_ignored(path)
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None, None
    except OSError as error:
        return None, f"cannot be read ({error.strerror or type(error).__name__})"
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
        return None, problem
    if len(data) > MAX_DOTENV_SIZE:
        return None, f"is larger than {MAX_DOTENV_SIZE // 1024} KiB"
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = None
    del data
    if text is None:
        return None, "is not UTF-8 text"
    if os.name == "posix" and stat.S_IMODE(mode) & 0o044 and _assigns_token(text):
        return None, (
            f"holds {_TOKEN_NAME} but other users can read it; run `chmod 600 {DOTENV_NAME}` "
            "so only you can"
        )
    value, problem = _parse(text, name)
    del text
    if problem is not None:
        return None, problem
    return value or None, None


def _assigns_token(text: str) -> bool:
    """Whether some line of `text` gives `QTE_TOKEN` a value, well formed or not."""
    for line in text.splitlines():
        assignment = parse_assignment(line)
        if assignment is not None and assignment[0] == _TOKEN_NAME:
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
    message = " ".join(exposures) + (
        " If you did not create this file (in a repository you cloned, say), check the "
        "exchange address in it before you use it."
    )
    try:
        warnings.warn(message, DotenvNotIgnored, stacklevel=_caller_level())
    except Warning:
        _log.warning("%s", message)


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


def _git_exposure(path: Path) -> str | None:
    """What could let git commit `path`, as advice for the person, or None if nothing
    could or git cannot tell."""
    if not is_inside_git_work_tree(path.parent):
        return None
    if is_tracked_by_git(path):
        return (
            f"git tracks {path}, so your token in it would be committed. Adding it to "
            f".gitignore does not stop that: run `git rm --cached {path.name}` in "
            f"{path.parent}, add {path.name} to .gitignore and commit."
        )
    if is_ignored_by_git(path) is False:
        return (
            f"{path} is inside a git working tree and git does not ignore it, so it could "
            f"be committed with your token. Add {path.name} to .gitignore."
        )
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
