"""The Windows token-file and console checks, against the real Windows API.

Each test makes real access lists and owners with `icacls` and lets the SDK read them back
through `ctypes`, as it does on a student's computer: nothing in `qte_sdk._fileaccess` or
`qte_sdk.token._is_console` is replaced. The tests are marked `windows`, which
`conftest.py` skips everywhere but Windows, and CI runs them on its `windows` job.

The tokens here are fakes. The SIDs are worked out when the tests run, from `whoami`.

CI's hosted runner runs the tests as an elevated administrator, which a student usually
is not. Three things differ because of that:

- A file an elevated administrator creates may be owned by BUILTIN\\Administrators rather
  than by the user, as Windows Server's default policy has it; a student's own files are
  owned by the student. The SDK accepts both owners, so the tests accept either for a new
  file, and set each owner explicitly to check both.
- An elevated administrator may give a file to another owner (`icacls /setowner`), which a
  student cannot do to their own files. The test of a file another account owns relies on
  it.
- The runner's account is the computer's built-in Administrator (its SID ends in -500),
  which SDDL writes as the alias LA rather than as its SID. The SDK cannot tell which
  account LA stands for, so a file this account owns has an owner it reports as unknown,
  not as yours. A student's own account is usually another one, written as its SID.
"""

import csv
import io
import os
import re
import subprocess
import sys
import tempfile
import traceback
import warnings
from pathlib import Path

import pytest

from qte_sdk import _fileaccess, dotenv
from qte_sdk import token as token_command
from qte_sdk.dotenv import AddressFileShared, FileShared, TokenFileShared
from qte_sdk.session import resolve_token

pytestmark = pytest.mark.windows

FAKE_TOKEN = "fake-token-not-real-3f9c2a71"
URL = "ws://127.0.0.1:8080/ws"
SYSTEM = "S-1-5-18"
ADMINISTRATORS = "S-1-5-32-544"
USERS = "S-1-5-32-545"
QTE_VARIABLES = ("QTE_TOKEN", "QTE_TOKEN_FILE", "QTE_URL")
# What `token set` and `token check` say when no broad group may read, change or replace
# the file and its owner is you, Administrators or SYSTEM.
CLEAN = (
    "None of Everyone, Authenticated Users, Users, INTERACTIVE or Domain Users can read or "
    "change it, or add or remove files in its folder, and it is owned by you, Administrators "
    "or SYSTEM (other groups and users are not checked)"
)
# How long a command may take before it counts as hanging.
TIMEOUT = 20


@pytest.fixture(autouse=True)
def no_token_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in QTE_VARIABLES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def user_sid() -> str:
    """The SID of the user the tests run as, from `whoami`, not from the SDK."""
    result = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=True,
    )
    sid = next(csv.reader(io.StringIO(result.stdout.strip())))[1]
    assert sid.startswith("S-1-5-"), result.stdout
    return sid


def icacls(path: Path, *args: str) -> None:
    result = subprocess.run(
        ["icacls", str(path), *args], capture_output=True, text=True, timeout=TIMEOUT
    )
    assert result.returncode == 0, f"icacls {' '.join(args)}: {result.stdout}{result.stderr}"


def make_folder_private(folder: Path, sid: str) -> None:
    """Give `folder` and what is made in it only the user's, SYSTEM's and Administrators'
    access, inheriting nothing from its parent."""
    grants = [f"*{who}:(OI)(CI)F" for who in (sid, SYSTEM, ADMINISTRATORS)]
    icacls(folder, "/inheritance:r", "/grant:r", *grants)


def make_file_private(path: Path, sid: str) -> None:
    """Give `path` only the user's, SYSTEM's and Administrators' access, inheriting nothing
    from its folder, so a later change to the folder's list does not reach it."""
    grants = [f"*{who}:F" for who in (sid, SYSTEM, ADMINISTRATORS)]
    icacls(path, "/inheritance:r", "/grant:r", *grants)


def open_folder_to_users(folder: Path) -> None:
    """Let BUILTIN\\Users change the folder and, by inheritance, the files in it, as on a
    folder at the root of a second drive."""
    icacls(folder, "/grant", f"*{USERS}:(OI)(CI)M")


def set_owner(path: Path, sid: str) -> None:
    icacls(path, "/setowner", f"*{sid}")


def sddl(path: Path) -> str:
    """The owner and access list of `path` as the SDK reads them."""
    text = _fileaccess._read_sddl(str(path))
    assert text is not None, f"could not read the access list of {path}"
    return text


def owner(path: Path) -> str:
    sections = _fileaccess._sections(sddl(path))
    assert sections is not None and "O" in sections, sddl(path)
    return sections["O"]


def grants_users(path: Path) -> bool:
    """Whether the access list of `path` has an entry for BUILTIN\\Users."""
    return re.search(r";(BU|S-1-5-32-545)\)", sddl(path)) is not None


def write_dotenv(folder: Path, *, token: bool = True) -> Path:
    path = folder / ".env"
    lines = [f"QTE_URL={URL}"] + ([f"QTE_TOKEN={FAKE_TOKEN}"] if token else [])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def assert_no_token(*texts: str) -> None:
    for text in texts:
        assert FAKE_TOKEN not in text


def run_sdk(
    *args: str, cwd: Path, env_token: bool = False, **kwargs
) -> subprocess.CompletedProcess[str]:
    """Run `python -m <args>` in a new process in `cwd`, with no QTE_ variable set but,
    if `env_token`, QTE_TOKEN set to the fake token. Fails the test if it has not finished
    within `TIMEOUT` seconds."""
    env = {k: v for k, v in os.environ.items() if k.upper() not in QTE_VARIABLES}
    if env_token:
        env["QTE_TOKEN"] = FAKE_TOKEN
    try:
        return subprocess.run(
            [sys.executable, "-m", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            **kwargs,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"python -m {' '.join(args)} was still running after {TIMEOUT} s")


def shared_warnings(read) -> list[warnings.WarningMessage]:
    """The `FileShared` warnings issued while `read()` runs. Recorded rather than made
    errors, since the SDK logs a warning that a filter makes an error instead of raising."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        read()
    return [w for w in caught if issubclass(w.category, FileShared)]


@pytest.fixture
def private_folder(tmp_path: Path, user_sid: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A folder only the user, SYSTEM and Administrators may open, made the working
    directory."""
    folder = tmp_path / "private"
    folder.mkdir()
    make_folder_private(folder, user_sid)
    monkeypatch.chdir(folder)
    return folder


def folder_of(path: Path) -> str:
    """The folder the SDK names in its warning about `path`."""
    return os.path.dirname(os.path.abspath(path))


# The account and the access lists


def test_the_sdk_reads_the_same_user_sid_as_whoami(user_sid):
    assert _fileaccess._current_user_sid() == user_sid


def test_a_new_file_is_owned_by_the_user_or_administrators(private_folder, user_sid):
    # On the elevated runner a new file may be owned by Administrators; a student's is
    # owned by the student. Either is no finding.
    path = write_dotenv(private_folder)
    assert owner(path) in (user_sid, "BA", ADMINISTRATORS)
    access = _fileaccess.broad_access(path)
    assert access is not None
    assert access.other_owner is False


# A private folder


def test_a_dotenv_in_a_private_folder_gives_no_warning(private_folder):
    path = write_dotenv(private_folder)
    assert not grants_users(path)
    access = _fileaccess.broad_access(path)
    assert access is not None
    assert (access.read, access.write, access.folder, access.other_owner) == ((), (), (), False)
    assert not access
    values: list[object] = []

    def read() -> None:
        values.extend(
            [dotenv.read_value("QTE_TOKEN"), dotenv.read_value("QTE_URL"), resolve_token()]
        )

    assert shared_warnings(read) == []
    assert values == [(FAKE_TOKEN, None), (URL, None), FAKE_TOKEN]


def test_token_check_in_a_private_folder_says_no_broad_group_can_open_it(private_folder):
    write_dotenv(private_folder)
    result = run_sdk("qte_sdk.token", "check", cwd=private_folder)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f".env; {CLEAN[0].lower()}{CLEAN[1:]}\n" in result.stdout
    assert "warning:" not in result.stdout
    assert_no_token(result.stdout, result.stderr)


def test_token_set_in_a_private_folder_says_no_broad_group_can_open_it(private_folder, capsys):
    status = token_command.main(
        ["set", "--url", URL],
        ask=lambda prompt: pytest.fail(f"unexpected prompt: {prompt}"),
        ask_secret=lambda prompt: FAKE_TOKEN,
        interactive=lambda: True,
    )
    out, err = capsys.readouterr()
    assert status == 0, out + err
    assert f"{CLEAN}." in out
    assert "Warning:" not in out
    assert_no_token(out, err)
    # The file `set` wrote took the folder's private list.
    assert not grants_users(private_folder / ".env")


def test_a_folder_under_the_user_profile_is_private_by_default():
    # What the SDK's advice rests on: a new folder under %USERPROFILE% that nobody changed.
    with tempfile.TemporaryDirectory(prefix="qte-sdk-test-", dir=os.environ["USERPROFILE"]) as d:
        path = write_dotenv(Path(d))
        access = _fileaccess.broad_access(path)
        assert access is not None
        assert not access, f"{sddl(path)} in a folder with {sddl(Path(d))}"


# A folder opened to BUILTIN\Users


def test_a_private_dotenv_in_a_folder_open_to_users_warns_about_the_folder(
    private_folder, user_sid
):
    path = write_dotenv(private_folder)
    make_file_private(path, user_sid)
    open_folder_to_users(private_folder)
    assert grants_users(private_folder)
    assert not grants_users(path)
    access = _fileaccess.broad_access(path)
    assert access is not None
    assert (access.read, access.write, access.other_owner) == ((), (), False)
    assert access.folder == ("BUILTIN\\Users",)

    caught = shared_warnings(lambda: dotenv.read_value("QTE_TOKEN"))
    assert [w.category for w in caught] == [TokenFileShared]
    message = str(caught[0].message)
    assert (
        f"other users can replace it: BUILTIN\\Users may add or remove files in "
        f"{folder_of(path)}" in message
    )
    assert "Windows lets" not in message
    assert_no_token(message)


def test_a_dotenv_that_inherits_users_access_warns_about_the_file_too(private_folder):
    open_folder_to_users(private_folder)
    path = write_dotenv(private_folder)
    assert grants_users(path)
    access = _fileaccess.broad_access(path)
    assert access is not None
    assert access.read == ("BUILTIN\\Users",)
    assert access.write == ("BUILTIN\\Users",)
    assert access.folder == ("BUILTIN\\Users",)

    caught = shared_warnings(lambda: dotenv.read_value("QTE_TOKEN"))
    assert [w.category for w in caught] == [TokenFileShared]
    message = str(caught[0].message)
    assert "Windows lets BUILTIN\\Users read or change it" in message
    assert f"BUILTIN\\Users may add or remove files in {folder_of(path)}" in message
    assert_no_token(message)


def test_token_check_in_a_folder_open_to_users_warns(private_folder, user_sid):
    path = write_dotenv(private_folder)
    make_file_private(path, user_sid)
    open_folder_to_users(private_folder)
    result = run_sdk("qte_sdk.token", "check", cwd=private_folder)
    assert "warning:" in result.stdout, result.stdout + result.stderr
    assert "BUILTIN\\Users may add or remove files in" in result.stdout
    assert "None of Everyone" not in result.stdout
    assert_no_token(result.stdout, result.stderr)


def test_an_address_only_dotenv_in_a_folder_open_to_users_warns(private_folder):
    open_folder_to_users(private_folder)
    write_dotenv(private_folder, token=False)
    caught = shared_warnings(lambda: dotenv.read_value("QTE_URL"))
    assert [w.category for w in caught] == [AddressFileShared]
    assert "change QTE_URL in it to a server of their own" in str(caught[0].message)


def test_a_token_file_in_a_folder_open_to_users_warns(private_folder, monkeypatch):
    open_folder_to_users(private_folder)
    path = private_folder / "token"
    path.write_text(FAKE_TOKEN + "\n", encoding="utf-8")
    monkeypatch.setenv("QTE_TOKEN_FILE", str(path))
    caught = shared_warnings(resolve_token)
    assert [w.category for w in caught] == [TokenFileShared]
    message = str(caught[0].message)
    assert "Windows lets BUILTIN\\Users read or change it" in message
    assert "replace your token" in message
    assert_no_token(message)


# Owners


def test_a_file_owned_by_the_user_has_no_owner_finding(private_folder, user_sid):
    path = write_dotenv(private_folder)
    set_owner(path, user_sid)
    if user_sid.startswith("S-1-5-21-") and user_sid.endswith("-500"):
        # CI's runner works as the computer's built-in Administrator account (RID 500),
        # whose SID SDDL writes as the alias LA. The SDK cannot tell which account LA
        # stands for, so it reports the owner as unknown rather than as another account:
        # no warning, and `token check` says the owner could not be checked. A student's
        # own account is usually another one, which SDDL writes as its SID.
        assert owner(path) == "LA"
        expected = None
    else:
        assert owner(path) == user_sid
        expected = False
    access = _fileaccess.broad_access(path)
    assert access is not None
    assert access.other_owner is expected
    assert not access
    assert shared_warnings(resolve_token) == []
    result = run_sdk("qte_sdk.token", "check", cwd=private_folder)
    assert "warning:" not in result.stdout, result.stdout + result.stderr
    assert ("its owner could not be checked" in result.stdout) is (expected is None)
    assert_no_token(result.stdout, result.stderr)


def test_a_file_owned_by_administrators_has_no_owner_finding(private_folder):
    path = write_dotenv(private_folder)
    set_owner(path, ADMINISTRATORS)
    assert owner(path) in ("BA", ADMINISTRATORS)
    access = _fileaccess.broad_access(path)
    assert access is not None
    assert access.other_owner is False
    assert not access
    assert shared_warnings(resolve_token) == []


def test_a_file_owned_by_another_account_warns(private_folder):
    # Only an elevated administrator may give a file to an owner not in its own token.
    path = write_dotenv(private_folder)
    set_owner(path, USERS)
    assert owner(path) in ("BU", USERS)
    access = _fileaccess.broad_access(path)
    assert access is not None
    assert access.other_owner is True
    caught = shared_warnings(lambda: dotenv.read_value("QTE_TOKEN"))
    assert [w.category for w in caught] == [TokenFileShared]
    message = str(caught[0].message)
    assert "it is owned by another account" in message
    assert_no_token(message)


# The console


def test_nul_is_not_a_console():
    with open(os.devnull, "rb") as nul:
        assert not token_command._is_console(nul.fileno())


def test_a_new_console_is_a_terminal_to_token_set():
    # The other side of the check below: in a console of its own, with its input not
    # redirected, `set` finds a terminal. The child says so by its exit status, since
    # capturing its output would redirect its handles away from the console.
    code = "import sys; from qte_sdk import token; sys.exit(0 if token._has_terminal() else 3)"
    startup = subprocess.STARTUPINFO(dwFlags=subprocess.STARTF_USESHOWWINDOW, wShowWindow=0)
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            startupinfo=startup,
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"the console check was still running after {TIMEOUT} s")
    assert result.returncode == 0


@pytest.mark.parametrize("command", [["set"], ["set", "--file"]])
@pytest.mark.parametrize("stdin", ["NUL", "pipe"])
def test_token_set_refuses_at_once_without_a_console(tmp_path, command, stdin):
    # Input from NUL passes `isatty`, so only the console check stops `getpass` waiting
    # for ever. A token in the environment must not be printed either.
    redirect = {"stdin": subprocess.DEVNULL} if stdin == "NUL" else {"input": ""}
    result = run_sdk("qte_sdk.token", *command, cwd=tmp_path, env_token=True, **redirect)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "needs a terminal" in result.stderr
    assert_no_token(result.stdout, result.stderr)
    assert not (tmp_path / ".env").exists()


# Links: a symbolic link to the file, and a junction on the way to it. The runner is an
# elevated administrator, so it may make symbolic links; a junction needs no such right.


def real(path: Path) -> str:
    """`path` as the SDK names a place it reached by resolving links: with every link,
    and any short 8.3 name, resolved."""
    return os.path.realpath(path)


def make_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_dotenv_link_to_a_file_in_a_folder_open_to_users_warns_about_that_folder(
    private_folder, tmp_path, user_sid
):
    elsewhere = tmp_path / "open"
    elsewhere.mkdir()
    make_folder_private(elsewhere, user_sid)
    open_folder_to_users(elsewhere)
    target = write_dotenv(elsewhere)  # made after the folder was opened: inherits Users:M
    link = private_folder / ".env"
    os.symlink(target, link)
    assert grants_users(target)
    access = _fileaccess.broad_access(link)
    assert access is not None
    assert (access.file, access.folder_path) == (real(target), real(elsewhere))
    assert access.read == access.write == access.folder == ("BUILTIN\\Users",)
    assert access.link_folder == ()  # the folder that holds the link is private
    assert access.link_folder_path == real(private_folder)

    caught = shared_warnings(lambda: dotenv.read_value("QTE_TOKEN"))
    assert [w.category for w in caught] == [TokenFileShared]
    message = str(caught[0].message)
    assert message.startswith(
        f"{Path.cwd() / '.env'}, a link to {real(target)}, holds your token, and Windows "
        "lets BUILTIN\\Users read or change it; and other users can replace it: "
        f"BUILTIN\\Users may add or remove files in {real(elsewhere)}, which holds the file "
        "it links to, so"
    ), message
    assert "which holds the link" not in message
    assert "Move the file it links to, and the link, into a folder" in message
    assert_no_token(message)


def test_a_dotenv_link_in_a_folder_open_to_users_warns_about_the_links_folder(
    private_folder, tmp_path, user_sid, monkeypatch
):
    target = write_dotenv(private_folder)
    make_file_private(target, user_sid)
    here = tmp_path / "open"
    here.mkdir()
    make_folder_private(here, user_sid)
    open_folder_to_users(here)
    os.symlink(target, here / ".env")
    monkeypatch.chdir(here)
    assert not grants_users(target)
    access = _fileaccess.broad_access(here / ".env")
    assert access is not None
    assert (access.read, access.write, access.folder) == ((), (), ())
    assert access.link_folder == ("BUILTIN\\Users",)
    assert (access.file, access.link_folder_path) == (real(target), real(here))

    caught = shared_warnings(lambda: dotenv.read_value("QTE_TOKEN"))
    assert [w.category for w in caught] == [TokenFileShared]
    message = str(caught[0].message)
    assert message.startswith(
        f"{Path.cwd() / '.env'}, a link to {real(target)}, holds your token, and other users "
        f"can replace it: BUILTIN\\Users may add or remove files in {real(here)}, which "
        "holds the link, so"
    ), message
    assert "Windows lets" not in message
    assert "which holds the file it links to" not in message
    assert_no_token(message)


def test_a_dotenv_reached_through_a_junction_warns_about_the_folder_holding_it(
    private_folder, tmp_path, user_sid, monkeypatch
):
    path = write_dotenv(private_folder)
    make_file_private(path, user_sid)
    holder = tmp_path / "open"
    holder.mkdir()
    make_folder_private(holder, user_sid)
    open_folder_to_users(holder)
    junction = holder / "project"
    make_junction(junction, private_folder)
    try:
        monkeypatch.chdir(junction)
        access = _fileaccess.broad_access(junction / ".env")
        assert access is not None
        assert (access.read, access.write, access.folder) == ((), (), ())
        assert access.link_folder == ("BUILTIN\\Users",)
        assert (access.file, access.folder_path) == (real(path), real(private_folder))
        assert (access.link, access.link_folder_path) == (str(junction), real(holder))

        caught = shared_warnings(lambda: dotenv.read_value("QTE_TOKEN"))
        assert [w.category for w in caught] == [TokenFileShared]
        message = str(caught[0].message)
        assert message.startswith(
            f"{Path.cwd() / '.env'}, which leads to {real(path)} through the link "
            f"{junction}, holds your token, and other users can replace it: BUILTIN\\Users "
            f"may add or remove files in {real(holder)}, which holds the link {junction}, so"
        ), message
        assert "Windows lets" not in message
        assert_no_token(message)
    finally:
        # The junction goes first, on its own, so no recursive delete goes through it.
        os.chdir(tmp_path)
        os.rmdir(junction)
    assert path.exists()


def shown_with_locals(error: BaseException) -> str:
    """What a traceback that shows local variables could print for `error` and its chain,
    and every text or bytes local of its frames, leaving out this module's own frames."""
    parts = [str(error), repr(error), repr(error.args)]
    pending = [traceback.TracebackException.from_exception(error, capture_locals=True)]
    while pending:
        link = pending.pop()
        parts.extend(link.format())
        for summary in link.stack:
            if summary.filename != __file__:
                parts.append(f"{summary.filename}:{summary.lineno} {summary.locals}")
        pending.extend(n for n in (link.__cause__, link.__context__) if n is not None)
    tb = error.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_filename != __file__:
            for value in tb.tb_frame.f_locals.values():
                if isinstance(value, bytes):
                    parts.append(value.decode("utf-8", "replace"))
                elif isinstance(value, str):
                    parts.append(value)
        tb = tb.tb_next
    return "\n".join(parts)


def test_a_ctrl_c_while_warning_about_a_token_dotenv_carries_no_token(private_folder):
    open_folder_to_users(private_folder)
    write_dotenv(private_folder)

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.showwarning = interrupted
        with pytest.raises(KeyboardInterrupt) as caught:
            resolve_token()
    error = caught.value
    assert type(error) is KeyboardInterrupt
    assert error.__cause__ is None and error.__context__ is None
    frames = []
    tb = error.__traceback__
    while tb is not None:
        frames.append(tb.tb_frame.f_code.co_name)
        tb = tb.tb_next
    assert "read_value" in frames  # it came through the frame that read the file
    assert_no_token(shown_with_locals(error))
    # The warning cut short is given next time.
    caught_again = shared_warnings(resolve_token)
    assert [w.category for w in caught_again] == [TokenFileShared]
