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
  the token is not used (`chmod 600 .env` fixes it).
- If the `.env` is inside a git working tree and git does not ignore it, a
  `DotenvNotIgnored` warning is issued, once per process, since the file could be
  committed. It never stops the SDK. The check runs `git check-ignore` when git is on the
  PATH and is skipped otherwise; no `QTE_` variable is passed to git.

Nothing here raises for a bad file, logs, or returns any of the file's text other than the
one value asked for: a problem is described by line number and name only.
"""

import os
import re
import shutil
import stat
import subprocess
import warnings
from pathlib import Path

__all__ = ["DOTENV_NAME", "DotenvNotIgnored", "parse_assignment"]

DOTENV_NAME = ".env"
_GIT_TIMEOUT = 5.0
_INLINE_COMMENT = re.compile(r"(?:^|\s)#")

_git_checked = False


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
        rest = rest[: comment.start()]
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


def read_value(name: str, *, private: bool = False) -> tuple[str | None, str | None]:
    """The value of `name` in `./.env`, and None; or None and what is wrong.

    (None, None) if there is no `./.env` or it does not assign `name`, or assigns it an
    empty value. With `private`, a file other users can read is refused on POSIX when it
    assigns `name`, and the value is not returned.

    Never raises for a bad file: a `UnicodeDecodeError` keeps the bytes it rejected, so
    neither it nor an `OSError` may reach the caller's exception as its cause or context.
    """
    path = dotenv_path()
    try:
        file = open(path, "rb")
    except FileNotFoundError:
        return None, None
    except OSError as error:
        return None, f"cannot be read ({error.strerror or type(error).__name__})"
    try:
        with file:
            _warn_if_not_ignored(path)
            mode = os.fstat(file.fileno()).st_mode
            data = file.read()
    except OSError as error:
        return None, f"cannot be read ({error.strerror or type(error).__name__})"
    if not stat.S_ISREG(mode):
        return None, "is not a regular file"
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, "is not UTF-8 text"
    del data
    value, problem = _parse(text, name)
    del text
    if problem is not None:
        return None, problem
    if not value:
        return None, None
    if private and os.name == "posix" and stat.S_IMODE(mode) & 0o044:
        return None, (
            f"holds {name} but other users can read it; run `chmod 600 {DOTENV_NAME}` "
            "so only you can"
        )
    return value, None


def _warn_if_not_ignored(path: Path) -> None:
    """Warn, once per process, if `path` is inside a git working tree and git does not
    ignore it. Called before the file is read, so no frame on the stack holds the token
    should a warnings filter turn the warning into an error."""
    global _git_checked
    if _git_checked:
        return
    _git_checked = True
    if not is_inside_git_work_tree(path.parent):
        return
    if is_ignored_by_git(path) is False:
        warnings.warn(
            f"{path} is inside a git working tree and git does not ignore it, so it could "
            f"be committed with your token. Add {DOTENV_NAME} to .gitignore. If you did not "
            "create this file (in a repository you cloned, say), check the exchange "
            "address in it before you use it.",
            DotenvNotIgnored,
            stacklevel=2,
        )


def is_inside_git_work_tree(directory: Path) -> bool:
    """Whether `directory` or one of its parents holds a `.git` entry."""
    return any((parent / ".git").exists() for parent in (directory, *directory.parents))


def is_ignored_by_git(path: Path) -> bool | None:
    """True if git ignores `path`, False if it does not (a tracked file counts as not
    ignored), or None if that cannot be told, for example when git is not on the PATH.

    No `QTE_` environment variable is passed to git."""
    git = shutil.which("git")
    if git is None:
        return None
    env = {k: v for k, v in os.environ.items() if not k.startswith("QTE_")}
    try:
        result = subprocess.run(
            [git, "check-ignore", "-q", "--", path.name],
            cwd=path.parent,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None
