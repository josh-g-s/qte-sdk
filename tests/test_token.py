"""The `python -m qte_sdk.token` helper: set, set --file and check."""

import os
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from qte_sdk import _fileaccess
from qte_sdk import dotenv as dotenv_module
from qte_sdk import token as helper
from qte_sdk.session import TOKEN_ENV_VAR, TOKEN_FILE_ENV_VAR, URL_ENV_VAR, resolve_token

POSIX = os.name == "posix"
URL = "ws://127.0.0.1:8080/ws"
needs_posix = pytest.mark.skipif(not POSIX, reason="POSIX permissions")
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not on the PATH")


def synthetic_token() -> str:
    return secrets.token_urlsafe(32)


@pytest.fixture(autouse=True)
def no_token_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    monkeypatch.delenv(TOKEN_FILE_ENV_VAR, raising=False)


class UnexpectedPrompt(BaseException):
    """Raised by a test prompt asked once too often. A BaseException, so the helper's own
    handlers cannot swallow it and turn it into an ordinary refusal."""


def answers(*replies: str) -> Callable[[str], str]:
    """A prompt that gives each reply in turn and fails if asked once too often."""
    pending: Iterator[str] = iter(replies)

    def ask(prompt: str) -> str:
        try:
            return next(pending)
        except StopIteration:
            raise UnexpectedPrompt(prompt) from None

    return ask


def never(prompt: str) -> str:
    raise UnexpectedPrompt(prompt)


def run(
    argv: list[str],
    ask: Callable[[str], str] = never,
    ask_secret: Callable[[str], str] = never,
    interactive: bool = True,
) -> int:
    return helper.main(argv, ask=ask, ask_secret=ask_secret, interactive=lambda: interactive)


def dotenv() -> Path:
    return Path.cwd() / ".env"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def assert_token_absent(token: str, text: str) -> None:
    for start in range(len(token) - 7):
        assert token[start : start + 8] not in text


def no_leftovers() -> None:
    assert [p.name for p in Path.cwd().iterdir() if p.name.endswith(".tmp")] == []


# set: ./.env


def test_set_writes_the_address_and_token_to_a_private_dotenv(capsys):
    token = synthetic_token()
    assert run(["set"], ask=answers(URL), ask_secret=answers(token)) == 0
    assert dotenv().read_text() == f"QTE_URL={URL}\nQTE_TOKEN={token}\n"
    if POSIX:
        assert mode(dotenv()) == 0o600
    assert resolve_token() == token
    out, err = capsys.readouterr()
    assert_token_absent(token, out + err)
    assert ("readable only by you" in out) == POSIX
    no_leftovers()


@needs_posix
def test_the_file_is_private_whatever_the_umask():
    previous = os.umask(0)
    try:
        assert run(["set"], ask=answers(URL), ask_secret=answers(synthetic_token())) == 0
    finally:
        os.umask(previous)
    assert mode(dotenv()) == 0o600


@needs_posix
def test_the_file_is_private_before_the_token_is_written(monkeypatch):
    seen: list[int] = []
    real_fdopen = os.fdopen

    def checking_fdopen(fd, *args, **kwargs):
        seen.append(stat.S_IMODE(os.fstat(fd).st_mode))
        assert os.fstat(fd).st_size == 0
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(helper.os, "fdopen", checking_fdopen)
    assert run(["set"], ask=answers(URL), ask_secret=answers(synthetic_token())) == 0
    assert seen == [0o600]


def test_an_existing_dotenv_keeps_its_other_lines():
    old = synthetic_token()
    dotenv().write_bytes(
        (
            "# my project\n"
            "DATABASE=postgres://localhost\n"
            f"export QTE_TOKEN={old}\r\n"
            "QTE_URL=ws://old.example.test/ws\n"
            f"QTE_TOKEN='{old}'\n"
            "OTHER='x y'"
        ).encode()
    )
    if POSIX:
        dotenv().chmod(0o644)
    token = synthetic_token()
    assert run(["set"], ask=answers(URL), ask_secret=answers(token)) == 0
    assert dotenv().read_bytes().decode() == (
        "# my project\n"
        "DATABASE=postgres://localhost\n"
        f"QTE_TOKEN={token}\r\n"
        f"QTE_URL={URL}\n"
        "OTHER='x y'\n"
    )
    if POSIX:
        assert mode(dotenv()) == 0o600
    no_leftovers()


def test_enter_keeps_the_address_already_in_dotenv():
    dotenv().write_text("QTE_URL=ws://kept.example.test/ws\n")
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return ""

    token = synthetic_token()
    assert run(["set"], ask=ask, ask_secret=answers(token)) == 0
    assert "Enter keeps the address already set" in prompts[0]
    assert "kept.example.test" not in prompts[0]
    assert dotenv().read_text() == f"QTE_URL=ws://kept.example.test/ws\nQTE_TOKEN={token}\n"


def test_enter_keeps_the_address_in_the_environment(monkeypatch, capsys):
    monkeypatch.setenv(URL_ENV_VAR, URL)
    assert run(["set"], ask=answers(""), ask_secret=answers(synthetic_token())) == 0
    assert f"QTE_URL={URL}\n" in dotenv().read_text()
    assert "different address" not in capsys.readouterr().out


def test_the_address_can_be_given_on_the_command_line():
    token = synthetic_token()
    assert run(["set", "--url", URL], ask_secret=answers(token)) == 0
    assert dotenv().read_text() == f"QTE_URL={URL}\nQTE_TOKEN={token}\n"


@pytest.mark.parametrize("address", ["", "http://example.test", "ws://a b", "ws://x'"])
def test_a_missing_or_odd_address_is_refused_and_nothing_is_written(address: str, capsys):
    assert run(["set"], ask=answers(address)) == 1
    assert not dotenv().exists()
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("pasted", ["", "   ", "two words", "has'quote", "tab\there", "a\x07b"])
def test_an_odd_token_is_refused_without_echoing_it(pasted: str, capsys):
    assert run(["set"], ask=answers(URL), ask_secret=answers(pasted)) == 1
    assert not dotenv().exists()
    out, err = capsys.readouterr()
    assert "error:" in err
    if pasted.strip():
        assert pasted not in out + err


def test_surrounding_whitespace_from_a_paste_is_removed():
    token = synthetic_token()
    assert run(["set"], ask=answers(URL), ask_secret=answers(f"  {token}\n")) == 0
    assert resolve_token() == token


def test_set_needs_a_terminal_and_asks_for_nothing_without_one(capsys):
    assert run(["set"], interactive=False) == 1
    assert run(["set", "--file"], interactive=False) == 1
    assert not dotenv().exists()
    assert "needs a terminal" in capsys.readouterr().err


def test_set_with_redirected_input_refuses_and_shows_nothing(tmp_path):
    token = synthetic_token()
    env = {k: v for k, v in os.environ.items() if not k.startswith("QTE_")}
    env["HOME"] = env["USERPROFILE"] = str(tmp_path / "home")
    env.pop("HOMEDRIVE", None)
    env.pop("HOMEPATH", None)
    for argv in (["set"], ["set", "--file"]):
        result = subprocess.run(
            [sys.executable, "-m", "qte_sdk.token", *argv],
            input=f"{URL}\n{token}\n",
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert result.returncode == 1
        assert "needs a terminal" in result.stderr
        assert_token_absent(token, result.stdout + result.stderr)
    assert not dotenv().exists()
    assert not (tmp_path / "home").exists()


def test_a_token_that_cannot_be_read_without_echo_is_not_read(capsys):
    import getpass

    read: list[str] = []

    def falls_back(prompt: str) -> str:
        warnings.warn("Can not control echo.", getpass.GetPassWarning, stacklevel=2)
        read.append("read with echo")
        return synthetic_token()

    assert run(["set"], ask=answers(URL), ask_secret=falls_back) == 1
    assert read == []
    assert not dotenv().exists()
    assert "without showing it" in capsys.readouterr().err


def test_a_terminal_failure_after_typing_does_not_show_the_token(capsys):
    token = synthetic_token()

    def fails_after_reading(prompt: str) -> str:
        typed = token  # noqa: F841  (what getpass holds when restoring the terminal fails)
        raise OSError("could not restore the terminal")

    assert run(["set"], ask=answers(URL), ask_secret=fails_after_reading) == 1
    out, err = capsys.readouterr()
    assert_token_absent(token, out + err)
    assert not dotenv().exists()


def test_an_interrupt_while_writing_leaves_no_copy_of_the_token(monkeypatch, capsys):
    def interrupted(fd):
        raise KeyboardInterrupt

    monkeypatch.setattr(helper.os, "fsync", interrupted)
    assert run(["set"], ask=answers(URL), ask_secret=answers(synthetic_token())) == 130
    assert list(Path.cwd().iterdir()) == []


def test_a_dotenv_that_would_grow_too_large_is_left_alone(capsys):
    from qte_sdk.dotenv import MAX_DOTENV_SIZE

    original = "#" * (MAX_DOTENV_SIZE - 10) + "\n"
    dotenv().write_text(original)
    assert run(["set"], ask=answers(URL), ask_secret=answers(synthetic_token())) == 1
    assert dotenv().read_text() == original
    assert "larger" in capsys.readouterr().err
    no_leftovers()


def test_an_interrupt_writes_nothing(capsys):
    def interrupted(prompt: str) -> str:
        raise KeyboardInterrupt

    assert run(["set"], ask=answers(URL), ask_secret=interrupted) == 130
    assert not dotenv().exists()


def test_a_dotenv_that_is_not_utf8_is_left_alone(capsys):
    dotenv().write_bytes(b"\xffQTE_URL=x\n")
    assert run(["set"]) == 1
    assert dotenv().read_bytes() == b"\xffQTE_URL=x\n"
    assert "not UTF-8" in capsys.readouterr().err


@needs_posix
def test_a_symlinked_dotenv_is_refused(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("")
    dotenv().symlink_to(target)
    assert run(["set"]) == 1
    assert target.read_text() == ""


@pytest.mark.skipif(not POSIX or os.geteuid() == 0, reason="needs POSIX permissions")
def test_a_failed_write_reports_the_reason_without_the_token(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "locked"
    folder.mkdir()
    monkeypatch.chdir(folder)
    folder.chmod(0o500)
    token = synthetic_token()
    try:
        assert run(["set"], ask=answers(URL), ask_secret=answers(token)) == 1
    finally:
        folder.chmod(0o700)
    out, err = capsys.readouterr()
    assert "could not write" in err
    assert_token_absent(token, out + err)
    assert list(folder.iterdir()) == []


def test_a_token_already_in_the_environment_is_pointed_out(monkeypatch, capsys):
    monkeypatch.setenv(TOKEN_ENV_VAR, synthetic_token())
    assert run(["set"], ask=answers(URL), ask_secret=answers(synthetic_token())) == 0
    out = capsys.readouterr().out
    assert "Run `unset QTE_TOKEN`, and remove it from your shell profile" in out
    assert "Remove-Item" not in out


# set: the .gitignore offer


def git(*args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "QTE_"))}
    subprocess.run(["git", *args], check=True, capture_output=True, env=env)


def ignored_path(name: str) -> bool:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "QTE_"))}
    return subprocess.run(["git", "check-ignore", "-q", name], env=env).returncode == 0


def ignored() -> bool:
    return ignored_path(".env")


@needs_git
@pytest.mark.parametrize("reply", ["", "y", "YES"])
def test_set_offers_to_ignore_dotenv_and_adds_it(reply: str, capsys):
    git("init", "-q", ".")
    Path(".gitignore").write_text("*.pyc")  # no final newline
    token = synthetic_token()
    assert run(["set"], ask=answers(URL, reply), ask_secret=answers(token)) == 0
    assert Path(".gitignore").read_text() == "*.pyc\n.env\n"
    assert ignored()
    out, err = capsys.readouterr()
    assert "Added .env" in out
    assert_token_absent(token, out + err)


@needs_git
def test_declining_says_plainly_that_dotenv_is_not_ignored(capsys):
    git("init", "-q", ".")
    assert run(["set"], ask=answers(URL, "n"), ask_secret=answers(synthetic_token())) == 0
    assert not Path(".gitignore").exists()
    assert "Not added" in capsys.readouterr().out
    assert dotenv().exists()


@needs_git
def test_an_ignored_dotenv_is_not_asked_about():
    git("init", "-q", ".")
    Path(".gitignore").write_text(".env\n")
    assert run(["set"], ask=answers(URL), ask_secret=answers(synthetic_token())) == 0
    assert Path(".gitignore").read_text() == ".env\n"


@needs_git
@needs_posix
def test_a_symlinked_gitignore_is_not_followed(tmp_path, capsys):
    git("init", "-q", ".")
    readme = Path("README.md")
    readme.write_text("hello\n")
    Path(".gitignore").symlink_to(readme)
    assert run(["set"], ask=answers(URL, "y"), ask_secret=answers(synthetic_token())) == 0
    assert readme.read_text() == "hello\n"
    assert "by hand" in capsys.readouterr().out


@needs_git
def test_a_tracked_dotenv_is_refused_before_the_token_is_asked_for(capsys):
    git("init", "-q", ".")
    dotenv().write_text("OTHER=1\n")
    git("add", ".env")
    assert run(["set"]) == 1  # neither prompt is reached
    assert dotenv().read_text() == "OTHER=1\n"
    err = capsys.readouterr().err
    assert "git tracks" in err and "git rm --cached .env" in err


@needs_git
def test_a_tracked_dotenv_that_gitignore_names_is_still_refused(capsys):
    git("init", "-q", ".")
    Path(".gitignore").write_text(".env\n")
    dotenv().write_text("OTHER=1\n")
    git("add", "-f", ".env")
    assert run(["set"]) == 1
    assert "git rm --cached .env" in capsys.readouterr().err


# set --file


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    # Every variable expanduser reads on any platform, so no test touches a real home.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    return home


def test_set_file_writes_only_the_token_to_a_private_default_file(home, capsys, monkeypatch):
    token = synthetic_token()
    assert run(["set", "--file"], ask_secret=answers(token)) == 0
    path = home / ".qte" / "token"
    assert path.read_text() == token + "\n"
    if POSIX:
        assert mode(path) == 0o600
        assert mode(path.parent) == 0o700
    assert not dotenv().exists()
    out, err = capsys.readouterr()
    assert f"export QTE_TOKEN_FILE={shlex.quote(str(path))}" in out
    assert_token_absent(token, out + err)
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(path))
    assert resolve_token() == token


@needs_posix
def test_set_file_makes_an_existing_default_directory_private(home):
    (home / ".qte").mkdir(mode=0o755)
    (home / ".qte").chmod(0o755)
    assert run(["set", "--file"], ask_secret=answers(synthetic_token())) == 0
    assert mode(home / ".qte") == 0o700


def test_set_file_replaces_an_old_token(home):
    first, second = synthetic_token(), synthetic_token()
    assert run(["set", "--file"], ask_secret=answers(first)) == 0
    assert run(["set", "--file"], ask_secret=answers(second)) == 0
    assert (home / ".qte" / "token").read_text() == second + "\n"
    assert sorted(p.name for p in (home / ".qte").iterdir()) == ["token"]


def test_set_file_takes_a_path(tmp_path, capsys):
    path = tmp_path / "secret dir" / "qte-token"
    token = synthetic_token()
    assert run(["set", "--file", str(path)], ask_secret=answers(token)) == 0
    assert path.read_text() == token + "\n"
    if POSIX:
        assert mode(path) == 0o600
    assert f"export QTE_TOKEN_FILE={shlex.quote(str(path.resolve()))}" in capsys.readouterr().out


def test_set_file_refuses_a_directory(tmp_path):
    assert run(["set", "--file", str(tmp_path)]) == 1


@needs_git
@pytest.mark.parametrize("target", [".env", "secrets/qte-token"])
def test_set_file_refuses_a_tracked_destination_before_asking(target: str, capsys):
    git("init", "-q", ".")
    path = Path(target)
    path.parent.mkdir(exist_ok=True)
    path.write_text("old\n")
    git("add", "-f", target)
    assert run(["set", "--file", target]) == 1  # the token is never asked for
    assert path.read_text() == "old\n"
    err = capsys.readouterr().err
    if path.parent == Path("."):
        assert "git rm --cached .env" in err
    else:
        real = Path.cwd().resolve() / "secrets"
        assert f"git -C {shlex.quote(str(real))} rm --cached -- qte-token" in err


@needs_git
def test_set_file_offers_to_ignore_an_unignored_destination(capsys):
    git("init", "-q", ".")
    token = synthetic_token()
    assert run(["set", "--file", "qte-token"], ask=answers("y"), ask_secret=answers(token)) == 0
    assert Path(".gitignore").read_text() == "qte-token\n"
    assert ignored_path("qte-token")
    out, err = capsys.readouterr()
    assert_token_absent(token, out + err)


@needs_git
@pytest.mark.skipif(not POSIX, reason="symbolic links")
def test_set_file_through_a_linked_directory_still_checks_git(tmp_path, capsys):
    repository = tmp_path / "repo"
    (repository / "config").mkdir(parents=True)
    git("init", "-q", str(repository))
    token_file = repository / "config" / "token"
    token_file.write_text("old\n")
    git("-C", str(repository), "add", "config/token")
    alias = tmp_path / "alias"
    alias.symlink_to(repository / "config")
    assert run(["set", "--file", str(alias / "token")]) == 1
    assert token_file.read_text() == "old\n"
    assert "git tracks" in capsys.readouterr().err


@needs_git
@pytest.mark.parametrize("stop", [KeyboardInterrupt, EOFError])
def test_stopping_at_the_gitignore_offer_says_the_token_was_saved(stop, capsys):
    git("init", "-q", ".")

    def interrupted(prompt: str) -> str:
        raise stop

    token = synthetic_token()
    assert run(["set", "--file", "qte-token"], ask=interrupted, ask_secret=answers(token)) == 130
    assert Path("qte-token").exists()
    out, err = capsys.readouterr()
    assert "the token was saved" in err and ".gitignore was not changed" in err
    assert_token_absent(token, out + err)


def test_set_file_refuses_a_gitignore_destination(capsys):
    assert run(["set", "--file", ".gitignore"]) == 1
    assert run(["set", "--file", "sub/.GitIgnore"]) == 1
    assert not Path(".gitignore").exists()
    assert capsys.readouterr().err.count("cannot go in a .gitignore file") == 2


@needs_git
def test_a_tracked_file_named_in_another_case_is_still_refused(capsys):
    git("init", "-q", ".")
    Path("README.md").write_text("hello\n")
    git("add", "README.md")
    if not Path("readme.md").exists():
        pytest.skip("this filesystem tells names apart by case")
    assert run(["set", "--file", "readme.md"]) == 1
    assert Path("README.md").read_text() == "hello\n"
    assert "git tracks" in capsys.readouterr().err


def case_insensitive() -> bool:
    probe = Path("CaseProbe")
    probe.write_text("")
    try:
        return Path("caseprobe").exists()
    finally:
        probe.unlink()


@needs_git
@pytest.mark.parametrize("change", ["renamed", "deleted"])
def test_a_tracked_name_in_another_case_is_refused_after_a_rename_or_deletion(change: str, capsys):
    git("init", "-q", ".")
    Path("README.md").write_text("hello\n")
    git("add", "README.md")
    if change == "renamed":
        if not case_insensitive():
            pytest.skip("this filesystem tells names apart by case")
        Path("README.md").rename("readme.md")
    else:
        Path("README.md").unlink()
    assert run(["set", "--file", "readme.md"]) == 1  # the token is never asked for
    err = capsys.readouterr().err
    assert "git tracks" in err and "README.md" in err


@needs_git
def test_a_tracked_file_under_a_directory_renamed_in_case_is_refused(capsys):
    git("init", "-q", ".")
    Path("Config").mkdir()
    Path("Config/README.md").write_text("hello\n")
    git("add", "Config/README.md")
    if not case_insensitive():
        pytest.skip("this filesystem tells names apart by case")
    Path("Config").rename("config")
    assert run(["set", "--file", "config/readme.md"]) == 1
    assert Path("config/README.md").read_text() == "hello\n"
    assert "Config/README.md" in capsys.readouterr().err


@needs_git
@pytest.mark.parametrize("tracked", ["\u00c9.txt", "E\u0301.txt"])
def test_a_deleted_tracked_name_differing_in_unicode_case_is_refused(tracked: str, capsys):
    git("init", "-q", ".")
    Path(tracked).write_text("hello\n")
    git("add", "--", tracked)
    Path(tracked).unlink()
    assert run(["set", "--file", "\u00e9.txt"]) == 1  # the token is never asked for
    assert "git tracks" in capsys.readouterr().err


def test_a_repository_whose_top_ends_in_a_space_still_works(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("git is not on the PATH")
    top = tmp_path / "project "
    top.mkdir()
    monkeypatch.chdir(top)
    git("init", "-q", ".")
    token = synthetic_token()
    assert run(["set", "--file", "qte-token"], ask=answers("n"), ask_secret=answers(token)) == 0


@needs_git
def test_an_untracked_name_that_differs_from_a_tracked_one_is_allowed():
    git("init", "-q", ".")
    Path("README.md").write_text("hello\n")
    git("add", "README.md")
    token = synthetic_token()
    assert run(["set", "--file", "token.txt"], ask=answers("n"), ask_secret=answers(token)) == 0
    assert Path("token.txt").read_text() == token + "\n"


@needs_git
def test_set_file_says_plainly_when_the_offer_is_declined(capsys):
    git("init", "-q", ".")
    token = synthetic_token()
    assert run(["set", "--file", "qte-token"], ask=answers("n"), ask_secret=answers(token)) == 0
    assert "Not added" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["set"], ["set", "--file", "qte-token"]])
def test_without_git_inside_a_repository_nothing_is_written(monkeypatch, argv, capsys):
    (Path.cwd() / ".git").mkdir()
    monkeypatch.setattr(dotenv_module.shutil, "which", lambda name: None)
    assert run(argv) == 1  # stops before either prompt
    assert sorted(p.name for p in Path.cwd().iterdir()) == [".git"]
    assert "could not say whether it tracks" in capsys.readouterr().err


@pytest.mark.parametrize("failure", ["timeout", "exit 128"])
def test_a_failing_git_inside_a_repository_stops_set(monkeypatch, failure: str):
    (Path.cwd() / ".git").mkdir()

    def broken(args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 5)
        return subprocess.CompletedProcess(args, 128)

    monkeypatch.setattr(dotenv_module.shutil, "which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(dotenv_module.subprocess, "run", broken)
    assert run(["set"]) == 1
    assert not dotenv().exists()


def test_outside_a_repository_git_is_not_needed(monkeypatch):
    monkeypatch.setattr(dotenv_module.shutil, "which", lambda name: None)
    assert run(["set"], ask=answers(URL), ask_secret=answers(synthetic_token())) == 0


# On simulated Windows. `os.name` itself cannot be changed: `pathlib` would then fail to
# make a path, so the SDK's own test for Windows is replaced, with each access list.

PROFILE = "D:(A;ID;FA;;;SY)(A;ID;FA;;;BA)(A;ID;FA;;;S-1-5-21-1-2-3-1001)"
SECOND_DRIVE = "D:AI(A;ID;FA;;;BA)(A;ID;FA;;;SY)(A;ID;0x1301bf;;;AU)(A;ID;0x1200a9;;;BU)"


@pytest.fixture
def windows(monkeypatch) -> Callable[[str | None], None]:
    """Act as on Windows; call the result with the access list every file is to have."""
    sddl: list[str | None] = [None]
    monkeypatch.setattr(_fileaccess, "on_windows", lambda: True)
    monkeypatch.setattr(_fileaccess, "_read_sddl", lambda path: sddl[0])

    def set_sddl(value: str | None) -> None:
        sddl[0] = value

    return set_sddl


def test_the_saved_message_promises_privacy_only_off_windows():
    assert "readable only by you" in helper._privacy(dotenv())


def test_the_saved_message_does_not_promise_privacy_when_windows_cannot_tell(windows):
    windows(None)
    message = helper._privacy(dotenv())
    assert "readable only by you" not in message
    assert "user profile" in message


def test_set_on_windows_warns_when_broad_groups_can_read_the_file(windows, capsys):
    windows(SECOND_DRIVE)
    token = synthetic_token()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert run(["set"], ask=answers(URL), ask_secret=answers(token)) == 0
    out, err = capsys.readouterr()
    assert "Warning:" in out
    assert "NT AUTHORITY\\Authenticated Users and BUILTIN\\Users" in out
    assert "%USERPROFILE%" in out and f'icacls "{Path.cwd()}"' in out
    assert "No group of other users" not in out
    assert_token_absent(token, out + err)


def test_set_on_windows_says_when_no_broad_group_can_read_the_file(windows, capsys):
    windows(PROFILE)
    token = synthetic_token()
    assert run(["set"], ask=answers(URL), ask_secret=answers(token)) == 0
    out, err = capsys.readouterr()
    assert "No group of other users, such as Everyone or Users, can read it." in out
    assert "Warning:" not in out and "readable only by you" not in out
    assert_token_absent(token, out + err)


def test_set_file_on_windows_reports_access_and_prints_windows_commands(windows, tmp_path, capsys):
    windows("D:(A;;FR;;;WD)")
    path = tmp_path / "it's here" / "token"
    token = synthetic_token()
    assert run(["set", "--file", str(path)], ask_secret=answers(token)) == 0
    out, err = capsys.readouterr()
    real = path.parent.resolve() / path.name
    assert f"Warning: {real} holds your token, and Windows lets Everyone read it" in out
    quoted = "'" + str(real).replace("'", "''") + "'"
    assert f"$env:QTE_TOKEN_FILE = {quoted}" in out
    assert f"[Environment]::SetEnvironmentVariable('QTE_TOKEN_FILE', {quoted}, 'User')" in out
    assert f'set "QTE_TOKEN_FILE={real}"' in out and f'setx QTE_TOKEN_FILE "{real}"' in out
    assert "export" not in out
    assert_token_absent(token, out + err)


def test_set_file_on_windows_gives_no_cmd_line_for_a_path_cmd_would_expand(
    windows, tmp_path, capsys
):
    windows(PROFILE)
    path = tmp_path / "!USERNAME!" / "token"
    assert run(["set", "--file", str(path)], ask_secret=answers(synthetic_token())) == 0
    out = capsys.readouterr().out
    assert "$env:QTE_TOKEN_FILE = '" in out
    assert "setx" not in out and "In cmd" not in out


def test_powershell_quoting_doubles_every_kind_of_single_quote():
    assert helper._powershell_quote("C:\\it's") == "'C:\\it''s'"
    assert helper._powershell_quote("a\u2019b$c") == "'a\u2019\u2019b$c'"


@pytest.mark.parametrize("argv", [["set"], ["set", "--file", "token"]])
def test_the_unset_hint_on_windows_gives_powershell_and_cmd(windows, monkeypatch, argv, capsys):
    windows(PROFILE)
    monkeypatch.setenv(TOKEN_ENV_VAR, synthetic_token())
    ask = answers(URL) if argv == ["set"] else never
    assert run(argv, ask=ask, ask_secret=answers(synthetic_token())) == 0
    out = capsys.readouterr().out
    assert "`Remove-Item Env:QTE_TOKEN`" in out
    assert "`set QTE_TOKEN=`" in out
    assert "`[Environment]::SetEnvironmentVariable('QTE_TOKEN', $null, 'User')`" in out
    assert "`unset" not in out


def test_the_unset_hint_names_the_token_file_variable_when_that_is_set(
    windows, monkeypatch, capsys
):
    windows(PROFILE)
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, "elsewhere")
    assert run(["set"], ask=answers(URL), ask_secret=answers(synthetic_token())) == 0
    out = capsys.readouterr().out
    assert "`Remove-Item Env:QTE_TOKEN_FILE`" in out
    assert "Env:QTE_TOKEN`" not in out


def test_check_on_windows_warns_about_a_shared_dotenv(windows, capsys):
    windows(SECOND_DRIVE)
    token = synthetic_token()
    dotenv().write_text(f"QTE_URL={URL}\nQTE_TOKEN={token}\n")
    dotenv().chmod(0o600)
    assert run(["check"]) == 0
    out, err = capsys.readouterr()
    assert out.count("warning:") == 1
    assert "BUILTIN\\Users" in out and "icacls" in out
    assert "no group of other users" not in out
    assert_token_absent(token, out + err)


def test_check_on_windows_reports_a_private_token_file(windows, monkeypatch, tmp_path, capsys):
    windows(PROFILE)
    token = synthetic_token()
    path = tmp_path / "token"
    path.write_text(token)
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(path))
    monkeypatch.setenv(URL_ENV_VAR, URL)
    assert run(["check"]) == 0
    out, err = capsys.readouterr()
    assert f"({path}); no group of other users, such as Everyone or Users, can read it" in out
    assert "warning:" not in out
    assert_token_absent(token, out + err)


def test_check_on_windows_says_nothing_extra_when_it_cannot_tell(windows, capsys):
    windows(None)
    dotenv().write_text(f"QTE_URL={URL}\nQTE_TOKEN={synthetic_token()}\n")
    dotenv().chmod(0o600)
    assert run(["check"]) == 0
    out = capsys.readouterr().out
    assert "warning:" not in out and "no group" not in out


# check


def test_check_reports_dotenv_without_the_token(capsys):
    token = synthetic_token()
    dotenv().write_text(f"QTE_URL={URL}\nQTE_TOKEN={token}\n")
    dotenv().chmod(0o600)
    assert run(["check"]) == 0
    out, err = capsys.readouterr()
    assert f"token:   {dotenv()}" in out
    assert f"address: set, from {dotenv()}" in out
    assert URL not in out
    assert_token_absent(token, out + err)


def test_check_reports_the_environment_and_the_token_file(monkeypatch, tmp_path, capsys):
    token = synthetic_token()
    path = tmp_path / "token"
    path.write_text(token)
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(path))
    monkeypatch.setenv(URL_ENV_VAR, URL)
    assert run(["check"]) == 0
    out = capsys.readouterr().out
    assert f"the file named by QTE_TOKEN_FILE ({path})" in out
    assert "the QTE_URL environment variable" in out
    monkeypatch.setenv(TOKEN_ENV_VAR, token)
    assert run(["check"]) == 0
    out, err = capsys.readouterr()
    assert "the QTE_TOKEN environment variable" in out
    assert_token_absent(token, out + err)


def test_check_reports_what_is_missing(capsys):
    assert run(["check"], interactive=False) == 1
    out = capsys.readouterr().out
    assert "token:   none usable" in out and "address: none" in out


def test_check_never_shows_the_address_in_case_it_is_the_token(monkeypatch, capsys):
    token = synthetic_token()
    monkeypatch.setenv(URL_ENV_VAR, token)
    monkeypatch.setenv(TOKEN_ENV_VAR, synthetic_token())
    assert run(["check"]) == 1
    out, err = capsys.readouterr()
    assert "does not start with ws://" in out
    assert_token_absent(token, out + err)


@needs_posix
def test_check_reports_a_dotenv_others_can_read(capsys):
    token = synthetic_token()
    dotenv().write_text(f"QTE_TOKEN={token}\n")
    dotenv().chmod(0o644)
    assert run(["check"]) == 1
    out, err = capsys.readouterr()
    assert "chmod 600" in out
    assert_token_absent(token, out + err)


@needs_git
def test_check_reports_a_dotenv_git_does_not_ignore(capsys):
    git("init", "-q", ".")
    dotenv().write_text(f"QTE_URL={URL}\n")
    run(["check"])
    assert "warning:" in capsys.readouterr().out
