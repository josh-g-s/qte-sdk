"""Set up the exchange address and your token without shell commands.

    python -m qte_sdk.token set            # write QTE_URL and QTE_TOKEN to ./.env
    python -m qte_sdk.token set --file     # write the token alone to ~/.qte/token
    python -m qte_sdk.token check          # say where the SDK would find each

`set` asks for the exchange address and then the token, which is read without echo. Press
Enter at the address prompt to keep the address already set, in `QTE_URL` or in `./.env`.
Both go into `./.env`, which is created readable only by you before anything is written;
an existing `.env` is replaced in one step, keeping its other lines. If the `.env` is in a
git working tree and git does not ignore it, `set` offers to add it to `.gitignore`; if git
already tracks it, `set` stops before asking for the token and says to run
`git rm --cached .env`, since `.gitignore` alone would not keep the token out of a commit.

`set --file [PATH]` writes only the token, to `PATH` or by default `~/.qte/token` (in a
directory only you can open), and prints the `export QTE_TOKEN_FILE=...` line to add to
your shell profile.

`check` reports where the SDK would take the token and the address from, as
`qte_sdk.session.resolve_token` and `resolve_url` would, without showing the token.

The token is never printed, logged or put in an error message, and since it is typed at a
prompt rather than on the command line, it never reaches your shell history. `set` needs
a terminal: it refuses to run with its input redirected, rather than read the token from
somewhere that could echo it.
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

from qte_sdk.dotenv import (
    DOTENV_NAME,
    MAX_DOTENV_SIZE,
    DotenvNotIgnored,
    dotenv_path,
    is_ignored_by_git,
    is_inside_git_work_tree,
    is_tracked_by_git,
    parse_assignment,
    read_value,
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
            return _set_file(args.file, ask_secret)
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
            "readable only by you. With --file, store only the token in a private file."
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
    return sys.stdin.isatty()


# set: ./.env


def _set_dotenv(url: str | None, ask: Prompt, ask_secret: Prompt) -> int:
    path = dotenv_path()
    if path.is_symlink():
        raise _Refused(f"{path} is a symbolic link; edit the file it points to by hand")
    lines = _existing_lines(path)
    _refuse_if_tracked(path)
    address = _ask_address(url, ask)
    secret = _ask_token(ask_secret)
    _offer_gitignore(path, ask)
    text = _merge(lines, {URL_ENV_VAR: address, TOKEN_ENV_VAR: secret.value})
    if len(text.encode("utf-8")) > MAX_DOTENV_SIZE:
        del text, secret
        raise _Refused(f"{path} would be larger than the SDK reads; make it smaller first")
    _write_private(path, text)
    del text, secret
    print(f"Saved {URL_ENV_VAR} and {TOKEN_ENV_VAR} to {path}, readable only by you.")
    print("Run your programs from this folder, so the SDK finds it.")
    if os.environ.get(TOKEN_ENV_VAR) or os.environ.get(TOKEN_FILE_ENV_VAR):
        print(
            f"Note: {TOKEN_ENV_VAR} or {TOKEN_FILE_ENV_VAR} is set in this terminal, and the "
            f"SDK uses it before {DOTENV_NAME}. Unset it, and remove it from your shell "
            "profile, to use the new token."
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
        warnings.simplefilter("ignore", DotenvNotIgnored)  # `set` offers its own fix
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
    if not is_inside_git_work_tree(path.parent) or is_tracked_by_git(path) is not True:
        return
    raise _Refused(
        f"git tracks {path}, so your token in it would be committed, and adding it to "
        f".gitignore does not stop that. Run `git rm --cached {path.name}` and commit, then "
        "run this command again. Nothing was changed."
    )


def _offer_gitignore(path: Path, ask: Prompt) -> None:
    """If git does not ignore `path`, offer to add it to the `.gitignore` beside it."""
    if not is_inside_git_work_tree(path.parent) or is_ignored_by_git(path) is not False:
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
            f"`git rm --cached {path.name}`."
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


def _set_file(target: str, ask_secret: Prompt) -> int:
    path = Path(target).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if path.is_symlink() or path.is_dir():
        raise _Refused(f"{path} is a directory or a symbolic link; choose another path")
    secret = _ask_token(ask_secret)
    directory = path.parent
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if Path(target) == DEFAULT_TOKEN_FILE and os.name == "posix":
            directory.chmod(0o700)
    except OSError as error:
        raise _Refused(
            f"could not create {directory} ({error.strerror or type(error).__name__})"
        ) from None
    _write_private(path, secret.value + "\n")
    del secret
    line = f"export {TOKEN_FILE_ENV_VAR}={shlex.quote(str(path))}"
    print(f"Saved the token to {path}, readable only by you.")
    print("Add this line to your shell profile (~/.zshrc or ~/.bashrc), and run it here too:")
    print()
    print(f"    {line}")
    print()
    if os.environ.get(TOKEN_ENV_VAR):
        print(f"Then run `unset {TOKEN_ENV_VAR}`: it is set here and would be used first.")
    return 0


# Writing


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
        problem = None
        try:
            source = token_source()
        except MissingToken as error:
            problem = str(error)
        if problem is None:
            print(f"token:   {_describe(source)}")
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
    for warning in caught:
        if issubclass(warning.category, DotenvNotIgnored):
            print(f"warning: {warning.message}")
            break
    return 0 if ok else 1


def _describe(source: str) -> str:
    if source == DOTENV_NAME:
        return f"{dotenv_path()}"
    if source == TOKEN_FILE_ENV_VAR:
        return f"the file named by {TOKEN_FILE_ENV_VAR} ({os.environ[TOKEN_FILE_ENV_VAR]})"
    return f"the {source} environment variable"


if __name__ == "__main__":
    sys.exit(main())
