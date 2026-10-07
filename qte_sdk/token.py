"""Set up the exchange address and your token without shell commands.

    python -m qte_sdk.token set            # write QTE_URL and QTE_TOKEN to ./.env
    python -m qte_sdk.token set --file     # write the token alone to ~/.qte/token
    python -m qte_sdk.token check          # say where the SDK would find each

`set` asks for the exchange address and then the token, which is read without echo. Press
Enter at the address prompt to keep the address already set, in `QTE_URL` or in `./.env`.
Both go into `./.env`, which is created with mode 0600 before anything is written; an
existing `.env` is replaced in one step, keeping its other lines.

`set --file [PATH]` writes only the token, to `PATH` or by default `~/.qte/token` (in a
directory made 0700), and prints the `export QTE_TOKEN_FILE=...` line to add to your shell
profile (on Windows, the PowerShell and cmd commands that set `QTE_TOKEN_FILE`).

For either destination, if it is in a git working tree: when git tracks the file, `set`
stops before asking for the token and says to run `git rm --cached`, since `.gitignore`
alone would not keep the token out of a commit; when git cannot tell (it is not installed,
say), `set` stops too, rather than guess; and when git does not ignore the file, `set`
offers to add it to `.gitignore`.

On macOS and Linux the modes make the files readable only by you. Windows does not apply
them, and this command does not change Windows access lists. Instead, once the file is
written, it reads the access lists of the file and its folder, and the file's owner, and
says whether a broad group of users, such as Everyone, Authenticated Users or Users, can
read or change the file or add or remove files in its folder, or another account owns it,
and if so how to fix that: keep the file in a folder under your user profile
(%USERPROFILE%), which is private by default. If the file's access list cannot be read, it
says to keep the file in such a folder.

`check` reports where the SDK would take the token and the address from, as
`qte_sdk.session.resolve_token` and `resolve_url` would, without showing the token. On
Windows it also reports whether a broad group of users can read or change the file the
token is in, or add or remove files in its folder, and whether another account owns it.

The token is never printed, logged or put in an error message, and since it is typed at a
prompt rather than on the command line, it never reaches your shell history. `set` needs
a terminal: it refuses to run with its input redirected, rather than read the token from
somewhere that could echo it. On Windows that means a console: input redirected from `NUL`
(a device, so it passes for a terminal) is refused too, rather than wait for a key that
can never come.
"""

import argparse
import getpass
import os
import secrets
import shlex
import sys
import warnings
from collections.abc import Callable
from pathlib import Path

from qte_sdk import _fileaccess
from qte_sdk.dotenv import (
    DOTENV_NAME,
    MAX_DOTENV_SIZE,
    DotenvNotIgnored,
    FileShared,
    _join,
    dotenv_path,
    is_ignored_by_git,
    is_inside_git_work_tree,
    is_tracked_by_git,
    parse_assignment,
    read_value,
    shared_message,
    tracked_ignoring_case,
)
from qte_sdk.session import (
    TOKEN_ENV_VAR,
    TOKEN_FILE_ENV_VAR,
    URL_ENV_VAR,
    MissingToken,
    MissingURL,
    _Secret,
    token_source,
    url_source,
)

__all__ = ["DEFAULT_TOKEN_FILE", "main"]

DEFAULT_TOKEN_FILE = Path("~/.qte/token")

Prompt = Callable[[str], str]


class _Refused(Exception):
    """A reason to stop, shown to the person as it is. Never holds the token."""


def main(
    argv: list[str] | None = None,
    *,
    ask: Prompt = input,
    ask_secret: Prompt = getpass.getpass,
    interactive: Callable[[], bool] | None = None,
) -> int:
    """Run the command in `argv` and return its exit status. `ask`, `ask_secret` and
    `interactive` read the answers and tell whether a person is at the keyboard; tests
    replace them."""
    args = _parser().parse_args(argv)
    if interactive is None:
        interactive = _has_terminal
    try:
        if args.command == "check":
            return _check()
        if not interactive():
            raise _Refused(
                "this command asks for your token, so it needs a terminal; run it directly, "
                "without redirecting its input"
            )
        if args.file is not None:
            return _set_file(args.file, ask, ask_secret)
        return _set_dotenv(args.url, ask, ask_secret)
    except _Refused as refusal:
        reason = str(refusal)
    except (KeyboardInterrupt, EOFError):
        print(f"\nstopped; {DOTENV_NAME} and any token file were not changed", file=sys.stderr)
        return 130
    # Reported outside the handler, so nothing is chained to it.
    print(f"error: {reason}", file=sys.stderr)
    return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m qte_sdk.token",
        description="Set up the exchange address and your token for qte_sdk.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    set_ = commands.add_parser(
        "set",
        help="store the address and token in ./.env, or the token alone in a private file",
        description=(
            f"Ask for the exchange address and your token and store them in ./{DOTENV_NAME}, "
            "kept private. With --file, store only the token in a private file."
        ),
    )
    set_.add_argument(
        "--url",
        help="the exchange address, instead of being asked for it",
    )
    set_.add_argument(
        "--file",
        nargs="?",
        const=str(DEFAULT_TOKEN_FILE),
        metavar="PATH",
        help=f"store only the token, in PATH (default {DEFAULT_TOKEN_FILE})",
    )
    commands.add_parser(
        "check",
        help="say where the SDK would take the token and the address from",
        description="Say where the SDK would take the token and the address from.",
    )
    return parser


def _has_terminal() -> bool:
    """Whether the input is a terminal a person can type the token at. On Windows that is a
    console: `NUL` is a character device, so `isatty` is true for it, but `getpass` would
    wait for ever there, on a console that does not exist."""
    if not sys.stdin.isatty():
        return False
    if _fileaccess.on_windows():
        return _is_console(sys.stdin.fileno())
    return True


def _is_console(fileno: int) -> bool:
    """Whether `fileno` is a Windows console, which `GetConsoleMode` accepts and any other
    handle (`NUL`, a pipe, a file) fails. False if that cannot be asked. Only on Windows:
    `msvcrt` and `ctypes.WinDLL` exist nowhere else."""
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_console_mode = kernel32.GetConsoleMode
        get_console_mode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_console_mode.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(fileno)
        return bool(get_console_mode(handle, ctypes.byref(wintypes.DWORD())))
    except Exception:
        return False


# set: ./.env


def _set_dotenv(url: str | None, ask: Prompt, ask_secret: Prompt) -> int:
    path = dotenv_path()
    if path.is_symlink():
        raise _Refused(f"{path} is a symbolic link; edit the file it points to by hand")
    lines = _existing_lines(path)
    _refuse_if_tracked(_on_disk(path))
    address = _ask_address(url, ask)
    secret = _ask_token(ask_secret)
    _offer_gitignore(path, ask)
    text = _merge(lines, {URL_ENV_VAR: address, TOKEN_ENV_VAR: secret.value})
    if len(text.encode("utf-8")) > MAX_DOTENV_SIZE:
        del text, secret, lines
        raise _Refused(f"{path} would be larger than the SDK reads; make it smaller first")
    _write_private(path, text)
    # The old lines may hold the old token: none is kept for the access check below.
    del text, secret, lines
    print(f"Saved {URL_ENV_VAR} and {TOKEN_ENV_VAR} to {path}{_privacy(path)}")
    print("Run your programs from this folder, so the SDK finds it.")
    if os.environ.get(TOKEN_ENV_VAR) or os.environ.get(TOKEN_FILE_ENV_VAR):
        print(
            f"Note: {TOKEN_ENV_VAR} or {TOKEN_FILE_ENV_VAR} is set in this terminal, and the "
            f"SDK uses it before {DOTENV_NAME}. "
            + _unset_advice([TOKEN_ENV_VAR, TOKEN_FILE_ENV_VAR], "to use the new token")
        )
    if os.environ.get(URL_ENV_VAR) and os.environ[URL_ENV_VAR] != address:
        print(
            f"Note: {URL_ENV_VAR} is set in this terminal to a different address, and the "
            f"SDK uses it before {DOTENV_NAME}."
        )
    return 0


def _existing_lines(path: Path) -> list[str]:
    """The lines of the existing `.env`, with their endings, or none if there is no file."""
    if path.exists() and not path.is_file():
        raise _Refused(f"{path} is not a regular file, so it was left unchanged")
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as error:
        raise _Refused(
            f"{path} cannot be read ({error.strerror or type(error).__name__})"
        ) from None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is None:
        raise _Refused(f"{path} is not UTF-8 text, so it was left unchanged; fix it by hand")
    return text.splitlines(keepends=True)


def _ask_address(url: str | None, ask: Prompt) -> str:
    with warnings.catch_warnings():
        # `set` offers its own fix for each, once the file is written.
        warnings.simplefilter("ignore", DotenvNotIgnored)
        warnings.simplefilter("ignore", FileShared)
        current = os.environ.get(URL_ENV_VAR) or read_value(URL_ENV_VAR)[0]
    if url is None:
        hint = " [Enter keeps the address already set]" if current else ""
        url = ask(f"Exchange address (from the course team){hint}: ").strip()
        if not url and current:
            url = current
    if not url:
        raise _Refused("an exchange address is needed; ask the course team for it")
    if not url.startswith(("ws://", "wss://")) or not _is_plain(url):
        raise _Refused(
            "the exchange address should look like wss://host/ws or ws://127.0.0.1:8080/ws, "
            "with no spaces or quotes"
        )
    return url


def _ask_token(ask_secret: Prompt) -> _Secret:
    secret = _read_secret(ask_secret)
    problem = None
    if not secret.value:
        problem = "no token was entered"
    elif not _is_plain(secret.value):
        problem = (
            "the token has a space, quote or control character, which a token never has; "
            "check how it was copied"
        )
    if problem is not None:
        del secret
        raise _Refused(problem)
    return secret


def _read_secret(ask_secret: Prompt) -> _Secret:
    """Read the token without echo. If echo cannot be turned off, `getpass` would warn and
    read it visibly; the warning is made an error so it stops before reading anything. Any
    other failure is reported without the original error, whose frames may hold what was
    typed."""
    failed = False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return _Secret(ask_secret("Token (paste it; nothing is shown): ").strip())
    except (KeyboardInterrupt, EOFError):
        raise
    except Exception:
        failed = True
    # Raised outside the handler, so the original error and its traceback are dropped.
    assert failed
    raise _Refused(
        "could not read the token without showing it, so nothing was read; run this "
        "command in an ordinary terminal window"
    )


def _is_plain(value: str) -> bool:
    return value.isprintable() and not any(c.isspace() or c in "'\"#" for c in value)


def _refuse_if_tracked(path: Path) -> None:
    """Stop before the token is asked for if git tracks `path`: the token would then be
    committed with it, and adding it to `.gitignore` would not prevent that."""
    if not is_inside_git_work_tree(path.parent):
        return
    tracked = is_tracked_by_git(path)
    if tracked is False:
        # Compared ignoring case as well, in the whole path: on a filesystem that ignores
        # case, `config/readme.md` is the file git tracks as `Config/README.md`, even after
        # a rename or a deletion.
        matches = tracked_ignoring_case(path) if path.parent.is_dir() else []
        if matches is None:
            tracked = None
        elif not matches:
            return
        else:
            raise _Refused(
                f"git tracks {matches[0]}, which is the same file as {path} on this "
                "filesystem, so your token in it would be committed. Choose another path, "
                "or stop git tracking that file first. Nothing was changed."
            )
    if tracked is None:
        raise _Refused(
            f"{path} is inside a git repository, but git could not say whether it tracks "
            "the file (is git installed and on your PATH?), so nothing was changed. Install "
            f"git and run this again, or choose a location outside the repository."
        )
    raise _Refused(
        f"git tracks {path}, so your token in it would be committed, and adding it to "
        f".gitignore does not stop that. Run `{_untrack_command(path)}` and commit, then "
        "run this command again. Nothing was changed."
    )


def _on_disk(path: Path) -> Path:
    """`path` with its name spelled as the directory lists it. On a filesystem that ignores
    case, `readme.md` may be the file git tracks as `README.md`, and git compares names
    exactly, so it is asked about the listed spelling."""
    if not path.exists():
        return path
    try:
        names = os.listdir(path.parent)
    except OSError:
        return path
    if path.name in names:
        return path
    for name in names:
        if name.lower() == path.name.lower():
            candidate = path.parent / name
            try:
                if os.path.samefile(candidate, path):
                    return candidate
            except OSError:
                continue
    return path


def _untrack_command(path: Path) -> str:
    """The command that stops git tracking `path`, to run from the working directory."""
    if path.parent == Path.cwd():
        return f"git rm --cached {shlex.quote(path.name)}"
    return f"git -C {shlex.quote(str(path.parent))} rm --cached -- {shlex.quote(path.name)}"


def _offer_gitignore(path: Path, ask: Prompt) -> None:
    """If git does not ignore `path`, offer to add it to the `.gitignore` beside it."""
    if not is_inside_git_work_tree(path.parent):
        return
    ignored = is_ignored_by_git(path)
    if ignored is True:
        return
    if ignored is None:
        print(
            f"Could not ask git whether it ignores {path.name}: make sure {path.name} is in "
            "your .gitignore before you commit anything."
        )
        return
    answer = ask(
        f"{path.name} is in a git repository and git does not ignore it, so it could be "
        f"committed with your token. Add {path.name} to .gitignore? [Y/n] "
    )
    if answer.strip().lower() not in ("", "y", "yes"):
        print(
            f"Not added. {path.name} is not ignored by git: add it to .gitignore before you "
            "commit anything, or your token could be published."
        )
        return
    gitignore = path.parent / ".gitignore"
    if gitignore.is_symlink() or (gitignore.exists() and not gitignore.is_file()):
        print(
            f"Not added: {gitignore} is a symbolic link or not a regular file. Add "
            f"{path.name} to your .gitignore by hand before you commit anything."
        )
        return
    try:
        existing = gitignore.read_bytes() if gitignore.exists() else b""
        with open(gitignore, "ab") as file:
            if existing and not existing.endswith(b"\n"):
                file.write(b"\n")
            file.write(f"{path.name}\n".encode())
    except OSError as error:
        raise _Refused(
            f"could not update {gitignore} ({error.strerror or type(error).__name__})"
        ) from None
    if is_ignored_by_git(path) is False:
        print(
            f"Added {path.name} to {gitignore}, but git still does not ignore it: check "
            f"{gitignore} and run `git check-ignore -v {path.name}`. If git tracks it, run "
            f"`{_untrack_command(path)}`."
        )
    else:
        print(f"Added {path.name} to {gitignore}.")


def _merge(lines: list[str], values: dict[str, str]) -> str:
    """The `.env` text with each name in `values` set: its first assignment replaced in
    place, any later ones removed, and the name added at the end if it was not there."""
    pending = dict(values)
    out: list[str] = []
    for line in lines:
        assignment = parse_assignment(line)
        name = assignment[0] if assignment is not None else None
        if name not in values:
            out.append(line)
            continue
        if name in pending:
            ending = line[len(line.rstrip("\r\n")) :] or "\n"
            out.append(f"{name}={pending.pop(name)}{ending}")
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    out.extend(f"{name}={value}\n" for name, value in pending.items())
    return "".join(out)


# set --file


def _set_file(target: str, ask: Prompt, ask_secret: Prompt) -> int:
    path = Path(target).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if path.is_symlink() or path.is_dir():
        raise _Refused(f"{path} is a directory or a symbolic link; choose another path")
    directory = path.parent
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if Path(target) == DEFAULT_TOKEN_FILE and os.name == "posix":
            directory.chmod(0o700)
    except OSError as error:
        raise _Refused(
            f"could not create {directory} ({error.strerror or type(error).__name__})"
        ) from None
    # The real directory, so a symbolic link on the way cannot hide a git working tree.
    path = _on_disk(directory.resolve() / path.name)
    if path.is_symlink() or path.is_dir():
        raise _Refused(f"{path} is a directory or a symbolic link; choose another path")
    if path.name.lower() == ".gitignore":
        raise _Refused("the token cannot go in a .gitignore file; choose another path")
    _refuse_if_tracked(path)
    secret = _ask_token(ask_secret)
    _write_private(path, secret.value + "\n")
    del secret
    print(f"Saved the token to {path}{_privacy(path, sets_address=False)}")
    interrupted = False
    try:
        _offer_gitignore(path, ask)
    except (KeyboardInterrupt, EOFError):
        interrupted = True
    if interrupted:
        print(
            f"\nstopped: the token was saved to {path}, but .gitignore was not changed. Add "
            f"{path.name} to .gitignore before you commit anything.",
            file=sys.stderr,
        )
        return 130
    _print_token_file_setting(path)
    if os.environ.get(TOKEN_ENV_VAR):
        print(
            f"{TOKEN_ENV_VAR} is set here and would be used first. "
            + _unset_advice([TOKEN_ENV_VAR], "to use the token file")
        )
    return 0


def _print_token_file_setting(path: Path) -> None:
    """Print the commands that set `QTE_TOKEN_FILE` to `path`, for this shell."""
    if not _fileaccess.on_windows():
        line = f"export {TOKEN_FILE_ENV_VAR}={shlex.quote(str(path))}"
        print("Add this line to your shell profile (~/.zshrc or ~/.bashrc), and run it here too:")
        print()
        print(f"    {line}")
        print()
        return
    quoted = _powershell_quote(str(path))
    print("To use it, set QTE_TOKEN_FILE. In PowerShell, for this window:")
    print()
    print(f"    $env:{TOKEN_FILE_ENV_VAR} = {quoted}")
    print()
    print("and to keep it for new windows too:")
    print()
    print(f"    [Environment]::SetEnvironmentVariable('{TOKEN_FILE_ENV_VAR}', {quoted}, 'User')")
    print()
    if not any(c in str(path) for c in "%!"):  # cmd expands %NAME% and !NAME! in quotes
        print(
            f'In cmd, run `set "{TOKEN_FILE_ENV_VAR}={path}"` for this window and '
            f'`setx {TOKEN_FILE_ENV_VAR} "{path}"` for new ones.'
        )
        print()


def _powershell_quote(text: str) -> str:
    """`text` as a PowerShell string that is taken as written, with no expansion. PowerShell
    takes the curly single quotes as quotes too, so each is doubled like `'`."""
    for quote in "'\u2018\u2019\u201a\u201b":
        text = text.replace(quote, quote * 2)
    return f"'{text}'"


def _unset_advice(names: list[str], purpose: str) -> str:
    """How to remove whichever of the environment variables `names` is set, `purpose` being
    what that is for, such as "to use the new token"."""
    names = [name for name in names if os.environ.get(name)] or names
    if not _fileaccess.on_windows():
        return f"Run `unset {' '.join(names)}`, and remove it from your shell profile, {purpose}."
    here = " and ".join(f"`Remove-Item Env:{name}`" for name in names)
    cmd = " and ".join(f"`set {name}=`" for name in names)
    saved = " and ".join(
        f"`[Environment]::SetEnvironmentVariable('{name}', $null, 'User')`" for name in names
    )
    return (
        f"{purpose[:1].upper()}{purpose[1:]}, remove it: run {here} in PowerShell, or {cmd} "
        f"in cmd. If you saved it as a user variable, also run {saved} in PowerShell, so new "
        "windows do not have it."
    )


# Writing


def _privacy(path: Path, *, sets_address: bool = True) -> str:
    """The end of the message saying where the token was saved: what protects it. On
    Windows, what the access lists of the file and its folder, and its owner, say: a
    warning naming the broad groups that can read, change or replace it, or another owner,
    or that none can; or, if the file's list cannot be read, where to keep the file.
    `sets_address` says whether the file is a `.env`, which can set the address."""
    if not _fileaccess.on_windows():
        return ", readable only by you."
    access = _fileaccess.broad_access(path)
    if access is None:
        return (
            ". Windows does not apply the file's private mode, so keep it in a folder only you "
            "can open, such as your user profile, and do not share that folder."
        )
    if not access:
        return f". {_none_of_the_checked_groups(access)}."
    return f".\nWarning: {shared_message(path, access, sets_address=sets_address)}"


def _write_private(path: Path, text: str) -> None:
    """Replace `path` with `text`, in a file created readable and writable only by you
    before anything is written to it, so the token is never in a file others can read."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as error:
        raise _Refused(
            f"could not write in {path.parent} ({error.strerror or type(error).__name__})"
        ) from None
    failure = None
    replaced = False
    try:
        with os.fdopen(descriptor, "wb") as file:
            if hasattr(os, "fchmod"):
                os.fchmod(file.fileno(), 0o600)  # exact, whatever the umask
            file.write(text.encode("utf-8"))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        replaced = True
    except OSError as error:
        failure = f"could not write {path} ({error.strerror or type(error).__name__})"
    finally:
        # Also on an interrupt: a partial copy of the token is never left behind.
        if not replaced:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    if failure is not None:
        # Raised outside the handler, so no frame that holds the text is chained to it.
        raise _Refused(failure)


# check


def _check() -> int:
    ok = True
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DotenvNotIgnored)
        warnings.simplefilter("always", FileShared)
        problem = None
        try:
            source = token_source()
        except MissingToken as error:
            problem = str(error)
        if problem is None:
            print(f"token:   {_describe(source)}{_access_note(source)}")
        else:
            ok = False
            print(f"token:   none usable. {problem}")
        problem = None
        try:
            source, url = url_source()
        except MissingURL as error:
            problem = str(error)
        if problem is None:
            # The address itself is not shown: a mistake could have put the token there.
            shape = (
                ""
                if url.startswith(("ws://", "wss://"))
                else ("; it does not start with ws:// or wss://, so check it")
            )
            print(f"address: set, from {_describe(source)}{shape}")
            ok = ok and not shape
        else:
            ok = False
            print(f"address: none. {problem}")
    shown: set[str] = set()
    for warning in caught:
        message = str(warning.message)
        if issubclass(warning.category, (DotenvNotIgnored, FileShared)):
            if message not in shown:
                shown.add(message)
                print(f"warning: {message}")
    return 0 if ok else 1


def _access_note(source: str) -> str:
    """On Windows, a note that no broad group of users can read, change or replace the file
    the token comes from, when the access lists of the file and its folder say so. A file
    they can, or that another account owns, gets a warning instead."""
    if source == DOTENV_NAME:
        path = dotenv_path()
    elif source == TOKEN_FILE_ENV_VAR:
        path = Path(os.environ[TOKEN_FILE_ENV_VAR])
    else:
        return ""
    access = _fileaccess.broad_access(path)
    if access is not None and not access:
        text = _none_of_the_checked_groups(access)
        return f"; {text[0].lower()}{text[1:]}"
    return ""


def _none_of_the_checked_groups(access: _fileaccess.BroadAccess) -> str:
    """What a clean Windows access check shows: none of the broad groups it looks at may
    read or change the file, or add or remove files in its folder (or, when it is reached
    through a link, in the link's folder), and its owner is you, Administrators or SYSTEM.
    It says which of the folders and the owner it could not check, and it looks at no other
    group or user, so it never says the file is private to you."""
    names = [group.rsplit("\\", 1)[-1] for group in _fileaccess.BROAD_GROUPS]
    listed = ", ".join(names[:-1]) + f" or {names[-1]}"
    text = f"None of {listed} can read or change it"
    folders: list[tuple[str, object]] = [("its folder", access.folder)]
    if access.link_folders is None or access.link_folders:
        which = (
            "the folder that holds the link"
            if access.link_folders and len(access.link_folders) == 1
            else "the folders that hold the links"
        )
        folders.append((f"{which} it is reached through", access.link_folder))
    checked = [part for part, known in folders if known is not None]
    if checked:
        text += f", or add or remove files in {' or '.join(checked)}"
    if access.other_owner is False:
        text += ", and it is owned by you, Administrators or SYSTEM"
    unchecked = [
        part for part, known in (*folders, ("its owner", access.other_owner)) if known is None
    ]
    note = "other groups and users are not checked"
    if unchecked:
        note = f"{_join(unchecked)} could not be checked, and {note}"
    return f"{text} ({note})"


def _describe(source: str) -> str:
    if source == DOTENV_NAME:
        return f"{dotenv_path()}"
    if source == TOKEN_FILE_ENV_VAR:
        return f"the file named by {TOKEN_FILE_ENV_VAR} ({os.environ[TOKEN_FILE_ENV_VAR]})"
    return f"the {source} environment variable"


if __name__ == "__main__":
    sys.exit(main())
