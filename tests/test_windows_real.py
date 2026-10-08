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
  which SDDL writes as the alias LA rather than as its SID. The SDK asks Windows which SID
  LA stands for, so a file this account owns is reported as yours. A student's own account
  is usually another one, written as its SID.
- An owner always may read and change an object's list, and the runner's token holds
  Administrators, which own what it creates, so a list is hidden from it only once the
  object is given to an owner outside its token: the tests that hide one give it to SYSTEM
  (as only an elevated administrator may), which is no finding of its own.
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
from qte_sdk.session import MissingToken, resolve_token

pytestmark = pytest.mark.windows

FAKE_TOKEN = "fake-token-not-real-3f9c2a71"
URL = "ws://127.0.0.1:8080/ws"
SYSTEM = "S-1-5-18"
ADMINISTRATORS = "S-1-5-32-544"
USERS = "S-1-5-32-545"
TRUSTED_INSTALLER = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
QTE_VARIABLES = ("QTE_TOKEN", "QTE_TOKEN_FILE", "QTE_URL")
# What `token set` and `token check` say when no broad group may read, change or replace
# the file and its owner is you or a trusted system account.
CLEAN = (
    "None of Everyone, Authenticated Users, Users, INTERACTIVE or Domain Users can read or "
    "change it, or add or remove files in its folder, and it is owned by you or a trusted "
    "system account: Administrators, SYSTEM or TrustedInstaller (other groups and users are "
    "not checked)"
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


def uncoded(warning: Warning) -> str:
    """A coded warning's message without its code, once it is checked to start with it."""
    text = str(warning)
    code = getattr(warning, "code", None)
    assert code is not None and text.startswith(f"{code}: "), text
    return text[len(code) + 2 :]


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
    assert isinstance(text, str), f"could not read the access list of {path}: {text!r}"
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
    message = uncoded(caught[0].message)
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
    message = uncoded(caught[0].message)
    assert "Windows lets BUILTIN\\Users read or change it" in message
    assert f"BUILTIN\\Users may add or remove files in {folder_of(path)}" in message
    assert_no_token(message)


def test_token_check_in_a_folder_open_to_users_warns(private_folder, user_sid):
    path = write_dotenv(private_folder)
    make_file_private(path, user_sid)
    open_folder_to_users(private_folder)
    result = run_sdk("qte_sdk.token", "check", cwd=private_folder)
    assert "warning: QTE-TOKEN-SHARED: " in result.stdout, result.stdout + result.stderr
    assert result.returncode == 1, result.stdout + result.stderr  # a report; sessions warn
    assert "BUILTIN\\Users may add or remove files in" in result.stdout
    assert "None of Everyone" not in result.stdout
    assert_no_token(result.stdout, result.stderr)


def test_an_address_only_dotenv_in_a_folder_open_to_users_warns(private_folder):
    open_folder_to_users(private_folder)
    write_dotenv(private_folder, token=False)
    caught = shared_warnings(lambda: dotenv.read_value("QTE_URL"))
    assert [w.category for w in caught] == [AddressFileShared]
    assert "change QTE_URL in it to a server of their own" in uncoded(caught[0].message)


def test_a_token_file_in_a_folder_open_to_users_warns(private_folder, monkeypatch):
    open_folder_to_users(private_folder)
    path = private_folder / "token"
    path.write_text(FAKE_TOKEN + "\n", encoding="utf-8")
    monkeypatch.setenv("QTE_TOKEN_FILE", str(path))
    caught = shared_warnings(resolve_token)
    assert [w.category for w in caught] == [TokenFileShared]
    message = uncoded(caught[0].message)
    assert "Windows lets BUILTIN\\Users read or change it" in message
    assert "replace your token" in message
    assert_no_token(message)


# Owners


def test_a_file_owned_by_the_user_has_no_owner_finding(private_folder, user_sid):
    path = write_dotenv(private_folder)
    set_owner(path, user_sid)
    # CI's runner works as the computer's built-in Administrator account (RID 500), whose
    # SID SDDL writes as the alias LA, which the SDK resolves to that SID (#169). A
    # student's own account is usually another one, which SDDL writes as its SID.
    builtin = user_sid.startswith("S-1-5-21-") and user_sid.endswith("-500")
    assert owner(path) == ("LA" if builtin else user_sid)
    access = _fileaccess.broad_access(path)
    assert access is not None
    assert access.other_owner is False
    assert not access, access
    assert shared_warnings(resolve_token) == []
    result = run_sdk("qte_sdk.token", "check", cwd=private_folder)
    assert "warning:" not in result.stdout, result.stdout + result.stderr
    assert "could not be checked" not in result.stdout, result.stdout
    assert "owned by you or a trusted system account" in result.stdout, result.stdout
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
    message = uncoded(caught[0].message)
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


def make_junction(link: Path, target: Path | str) -> None:
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
    assert access.links == (str(link),)
    assert [f.path for f in access.link_folders] == [real(private_folder)]

    caught = shared_warnings(lambda: dotenv.read_value("QTE_TOKEN"))
    assert [w.category for w in caught] == [TokenFileShared]
    message = uncoded(caught[0].message)
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
    assert access.file == real(target)
    assert [f.path for f in access.link_folders] == [real(here)]

    caught = shared_warnings(lambda: dotenv.read_value("QTE_TOKEN"))
    assert [w.category for w in caught] == [TokenFileShared]
    message = uncoded(caught[0].message)
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
        assert access.links == (str(junction),)
        assert [f.path for f in access.link_folders] == [real(holder)]

        caught = shared_warnings(lambda: dotenv.read_value("QTE_TOKEN"))
        assert [w.category for w in caught] == [TokenFileShared]
        message = uncoded(caught[0].message)
        assert message.startswith(
            f"{Path.cwd() / '.env'}, which leads to {real(path)} through the link "
            f"{junction}, holds your token, and other users can replace it: BUILTIN\\Users "
            f"may add or remove files in {real(holder)}, which holds the link {junction}, so"
        ), message
        assert "Windows lets" not in message
        assert_no_token(message)
    finally:
        # The junction goes first, on its own, so no recursive delete goes through it.
        monkeypatch.chdir(tmp_path)
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


def test_a_chain_of_links_warns_about_an_open_folder_in_the_middle(
    private_folder, tmp_path, user_sid
):
    # private\.env -> shared\redirect.env -> safe\config.env: whoever may replace the
    # middle link may point the chain elsewhere, though both ends are private.
    shared = tmp_path / "shared"
    safe = tmp_path / "safe"
    for folder in (shared, safe):
        folder.mkdir()
        make_folder_private(folder, user_sid)
    target = write_dotenv(safe)
    target = target.rename(safe / "config.env")
    make_file_private(target, user_sid)
    middle = shared / "redirect.env"
    os.symlink(target, middle)
    os.symlink(middle, private_folder / ".env")
    open_folder_to_users(shared)
    access = _fileaccess.broad_access(private_folder / ".env")
    assert access is not None
    assert (access.read, access.write, access.folder) == ((), (), ())
    assert access.link_folder == ("BUILTIN\\Users",)
    assert access.links == (str(private_folder / ".env"), str(middle))
    assert [(f.path, f.groups) for f in access.link_folders] == [
        (real(private_folder), ()),
        (real(shared), ("BUILTIN\\Users",)),
    ]

    caught = shared_warnings(lambda: dotenv.read_value("QTE_TOKEN"))
    assert [w.category for w in caught] == [TokenFileShared]
    message = uncoded(caught[0].message)
    assert message.startswith(
        f"{Path.cwd() / '.env'}, a link that leads to {real(target)} through the link "
        f"{middle}, holds your token, and other users can replace it: BUILTIN\\Users may "
        f"add or remove files in {real(shared)}, which holds the link {middle}, so"
    ), message
    assert "Windows lets" not in message
    assert_no_token(message)


def test_a_junction_above_a_chain_of_links_warns_about_each_open_folder(
    tmp_path, user_sid, monkeypatch
):
    # holder\\proj is a junction to project, whose .env links to shared\\redirect.env,
    # which links to safe\\config.env. holder and shared are open to Users.
    folders = {name: tmp_path / name for name in ("holder", "project", "shared", "safe")}
    for folder in folders.values():
        folder.mkdir()
        make_folder_private(folder, user_sid)
    target = folders["safe"] / "config.env"
    target.write_text(f"QTE_TOKEN={FAKE_TOKEN}\n", encoding="utf-8")
    make_file_private(target, user_sid)
    middle = folders["shared"] / "redirect.env"
    os.symlink(target, middle)
    first = folders["project"] / ".env"
    os.symlink(middle, first)
    junction = folders["holder"] / "proj"
    make_junction(junction, folders["project"])
    open_folder_to_users(folders["holder"])
    open_folder_to_users(folders["shared"])
    try:
        monkeypatch.chdir(junction)
        access = _fileaccess.broad_access(junction / ".env")
        assert access is not None
        assert (access.read, access.write, access.folder) == ((), (), ())
        assert access.link_folder == ("BUILTIN\\Users",)
        assert access.links == (str(junction), str(first), str(middle))
        assert [(f.path, f.groups) for f in access.link_folders] == [
            (real(folders["holder"]), ("BUILTIN\\Users",)),
            (real(folders["project"]), ()),
            (real(folders["shared"]), ("BUILTIN\\Users",)),
        ]

        caught = shared_warnings(lambda: dotenv.read_value("QTE_TOKEN"))
        assert [w.category for w in caught] == [TokenFileShared]
        message = uncoded(caught[0].message)
        assert message.startswith(
            f"{Path.cwd() / '.env'}, which leads to {real(target)} through the links "
            f"{junction}, {first} and {middle}, holds your token, and other users can replace "
            f"it: BUILTIN\\Users may add or remove files in {real(folders['holder'])}, which "
            f"holds the link {junction}, and BUILTIN\\Users may add or remove files in "
            f"{real(folders['shared'])}, which holds the link {middle}, so"
        ), message
        assert_no_token(message)
    finally:
        monkeypatch.chdir(tmp_path)
        os.rmdir(junction)


# Links the SDK cannot follow, though Windows does: the file is still read, so the warning
# must come when it is.


def test_a_chain_longer_than_the_sdk_follows_warns_when_the_token_is_read(
    private_folder, tmp_path, user_sid
):
    # Windows follows up to 63 links; the SDK follows MAX_LINKS (40) and says when it stops.
    links = tmp_path / "links"
    links.mkdir()
    make_folder_private(links, user_sid)
    target = write_dotenv(links)
    make_file_private(target, user_sid)
    following = target
    for number in range(_fileaccess.MAX_LINKS + 1, 0, -1):
        link = links / f"link{number}.env"
        os.symlink(following, link)
        following = link
    os.symlink(following, private_folder / ".env")
    access = _fileaccess.broad_access(private_folder / ".env")
    assert access is not None
    assert access.unfollowed == f"there are more than {_fileaccess.MAX_LINKS} links on the way"
    assert access.file == real(target)  # Windows itself reached the file

    caught = shared_warnings(lambda: dotenv.read_value("QTE_TOKEN"))
    assert [w.category for w in caught] == [TokenFileShared]
    message = uncoded(caught[0].message)
    assert (
        "holds your token, but it could not be fully checked: there are more than "
        f"{_fileaccess.MAX_LINKS} links on the way; a link on the way could not be followed, "
        "so check where it leads."
    ) in message
    assert_no_token(message)


def volume_name(path: Path) -> str:
    """`path` written with its volume's GUID name, such as `\\\\?\\Volume{...}\\Users\\...`,
    from `mountvol`, not from the SDK."""
    drive, rest = os.path.splitdrive(real(path))
    result = subprocess.run(
        ["mountvol", drive + "\\", "/L"], capture_output=True, text=True, timeout=TIMEOUT
    )
    volume = result.stdout.strip()
    assert result.returncode == 0 and volume.startswith("\\\\?\\Volume{"), result.stdout
    return volume.rstrip("\\") + rest


def test_a_junction_to_a_volumes_guid_name_warns_with_the_open_folder_before_it(
    tmp_path, user_sid, monkeypatch
):
    # The reviewer's case: a folder open to Users holds a junction whose target is written
    # with a volume's GUID name, which Windows follows and the SDK does not.
    holder = tmp_path / "open"
    safe = tmp_path / "safe"
    for folder in (holder, safe):
        folder.mkdir()
        make_folder_private(folder, user_sid)
    path = write_dotenv(safe)
    make_file_private(path, user_sid)
    junction = holder / "project"
    make_junction(junction, volume_name(safe))
    open_folder_to_users(holder)
    try:
        monkeypatch.chdir(junction)
        access = _fileaccess.broad_access(junction / ".env")
        assert access is not None
        assert access.links == (str(junction),)
        assert access.link_folder == ("BUILTIN\\Users",)
        assert access.unfollowed is not None and str(junction) in access.unfollowed

        caught = shared_warnings(lambda: dotenv.read_value("QTE_TOKEN"))
        assert [w.category for w in caught] == [TokenFileShared]
        message = uncoded(caught[0].message)
        assert (
            f"BUILTIN\\Users may add or remove files in {real(holder)}, which holds the link "
            f"{junction}, so other people"
        ) in message, message
        assert (
            f"Also, it could not be fully checked: {access.unfollowed}; a link on the way "
            "could not be followed, so check where it leads."
        ) in message
        assert_no_token(message)
    finally:
        monkeypatch.chdir(tmp_path)
        os.rmdir(junction)
    assert path.exists()


# Lists the check is not shown, and folders another account owns. A list is hidden by an
# entry that denies the user READ_CONTROL; since an owner may always read the list, the
# object is then given to SYSTEM. Each is put back afterwards, so the folder can be removed.

NOT_SHOWN = "Windows would not let this check see who may"


def hide_list(path: Path, user_sid: str) -> None:
    """Deny the user READ_CONTROL on `path`, and give it to SYSTEM, so the user may still
    open it (and what is in it, if it is a folder) but not see its owner or list."""
    icacls(path, "/deny", f"*{user_sid}:(RC)")
    set_owner(path, SYSTEM)
    assert _fileaccess._read_sddl(str(path)) is _fileaccess.DENIED


def show_list(path: Path, user_sid: str) -> None:
    """Undo `hide_list`. icacls cannot give `path` back, since it reads the list first, so
    takeown gives it to Administrators, who then may read and change its list."""
    result = subprocess.run(
        ["takeown", "/f", str(path), "/a"], capture_output=True, text=True, timeout=TIMEOUT
    )
    assert result.returncode == 0, f"takeown: {result.stdout}{result.stderr}"
    icacls(path, "/remove:d", f"*{user_sid}")


def test_a_folder_whose_list_is_not_shown_warns_when_the_token_is_read(
    tmp_path, user_sid, monkeypatch
):
    locked = tmp_path / "locked"
    locked.mkdir()
    make_folder_private(locked, user_sid)
    path = locked / "token"
    path.write_text(FAKE_TOKEN + "\n", encoding="utf-8")
    make_file_private(path, user_sid)
    hide_list(locked, user_sid)
    try:
        access = _fileaccess.broad_access(path)
        assert access is not None
        folder = os.path.dirname(real(path))
        assert access.unseen == (f"{NOT_SHOWN} add or remove files in {folder}",)
        assert (access.read, access.write, access.folder) == ((), (), None)
        assert access

        monkeypatch.setenv("QTE_TOKEN_FILE", str(path))
        values: list[str] = []
        caught = shared_warnings(lambda: values.append(resolve_token()))
        assert values == [FAKE_TOKEN]
        assert [w.category for w in caught] == [TokenFileShared]
        message = uncoded(caught[0].message)
        assert (
            f"holds your token, but it could not be fully checked: {NOT_SHOWN} add or remove "
            f"files in {folder}."
        ) in message, message
        assert_no_token(message)

        write_dotenv(locked)
        result = run_sdk("qte_sdk.token", "check", cwd=locked)
        # The working directory the command is given may be written with short 8.3 names.
        assert result.stdout.count("warning:") == 1, result.stdout + result.stderr
        assert "warning: QTE-TOKEN-UNCHECKED: " in result.stdout, result.stdout
        assert result.returncode == 2, result.stdout + result.stderr  # it could not tell
        assert (
            f".env holds your token, but it could not be fully checked: {NOT_SHOWN} add or "
            f"remove files in {folder}."
        ) in result.stdout, result.stdout + result.stderr
        assert "none of Everyone" not in result.stdout
        assert_no_token(result.stdout, result.stderr)
    finally:
        show_list(locked, user_sid)


def test_a_file_whose_list_is_not_shown_cannot_be_read_and_its_folder_is_still_checked(
    private_folder, user_sid, monkeypatch
):
    # Python opens a file for GENERIC_READ, which needs READ_CONTROL, so a token file whose
    # list is hidden from you cannot be read at all, and so is never used; the check still
    # reports the folder's list, which here is open to Users.
    path = private_folder / "token"
    path.write_text(FAKE_TOKEN + "\n", encoding="utf-8")
    make_file_private(path, user_sid)
    open_folder_to_users(private_folder)
    hide_list(path, user_sid)
    try:
        access = _fileaccess.broad_access(path)
        assert access is not None
        assert access.unseen == (f"{NOT_SHOWN} open {real(path)}",)
        assert access.folder == ("BUILTIN\\Users",)
        assert (access.read, access.write, access.other_owner) == ((), (), None)
        message = dotenv.shared_message(path, access, sets_address=False)
        assert message.startswith(
            f"{path} holds your token, and other users can replace it: BUILTIN\\Users may add "
            f"or remove files in {os.path.dirname(real(path))}, so other people"
        ), message
        assert f"Also, it could not be fully checked: {NOT_SHOWN} open {real(path)}." in message

        monkeypatch.setenv("QTE_TOKEN_FILE", str(path))
        with pytest.raises(MissingToken, match="names a file that cannot be read"):
            shared_warnings(resolve_token)
    finally:
        show_list(path, user_sid)


def test_a_folder_owned_by_another_account_warns(private_folder, user_sid):
    path = write_dotenv(private_folder)
    make_file_private(path, user_sid)
    set_owner(private_folder, USERS)
    folder = os.path.dirname(real(path))
    access = _fileaccess.broad_access(path)
    assert access is not None
    assert (access.read, access.write, access.folder, access.folder_owner) == ((), (), (), True)

    caught = shared_warnings(lambda: dotenv.read_value("QTE_TOKEN"))
    assert [w.category for w in caught] == [TokenFileShared]
    message = uncoded(caught[0].message)
    assert message.startswith(
        f"{Path.cwd() / '.env'} holds your token, and {folder} is owned by another account, "
        "which can change who may add or remove files in it, so other people"
    ), message
    assert_no_token(message)
    result = run_sdk("qte_sdk.token", "check", cwd=private_folder)
    assert f"{folder} is owned by another account" in result.stdout, result.stdout
    assert result.returncode == 1, result.stdout + result.stderr
    assert_no_token(result.stdout, result.stderr)


def test_a_folder_that_holds_a_link_and_is_owned_by_another_account_warns(
    private_folder, tmp_path, user_sid, monkeypatch
):
    target = write_dotenv(private_folder)
    make_file_private(target, user_sid)
    holder = tmp_path / "holder"
    holder.mkdir()
    make_folder_private(holder, user_sid)
    os.symlink(target, holder / ".env")
    set_owner(holder, USERS)
    monkeypatch.chdir(holder)
    access = _fileaccess.broad_access(holder / ".env")
    assert access is not None
    assert (access.link_folder, access.link_owner) == ((), True)
    assert [(f.path, f.other_owner) for f in access.link_folders] == [(real(holder), True)]

    caught = shared_warnings(lambda: dotenv.read_value("QTE_TOKEN"))
    assert [w.category for w in caught] == [TokenFileShared]
    message = uncoded(caught[0].message)
    assert message.startswith(
        f"{Path.cwd() / '.env'}, a link to {real(target)}, holds your token, and "
        f"{real(holder)}, which holds the link, is owned by another account, which can "
        "change who may add or remove files in it, so other people"
    ), message
    assert_no_token(message)


def test_a_folder_that_holds_a_link_and_whose_list_is_not_shown_warns(
    private_folder, tmp_path, user_sid, monkeypatch
):
    target = private_folder / "token"
    target.write_text(FAKE_TOKEN + "\n", encoding="utf-8")
    make_file_private(target, user_sid)
    holder = tmp_path / "holder"
    holder.mkdir()
    make_folder_private(holder, user_sid)
    link = holder / "token"
    os.symlink(target, link)
    hide_list(holder, user_sid)
    try:
        access = _fileaccess.broad_access(link)
        assert access is not None
        assert access.unseen == (f"{NOT_SHOWN} add or remove files in {real(holder)}",)
        assert access.link_folder is None

        monkeypatch.setenv("QTE_TOKEN_FILE", str(link))
        values: list[str] = []
        caught = shared_warnings(lambda: values.append(resolve_token()))
        assert values == [FAKE_TOKEN]
        assert [w.category for w in caught] == [TokenFileShared]
        message = uncoded(caught[0].message)
        assert message.startswith(
            f"{link}, a link to {real(target)}, holds your token, but it could not be fully "
            f"checked: {NOT_SHOWN} add or remove files in {real(holder)}. A later release will "
            "refuse such a file. Keep the file "
            "itself, not a link to it,"
        ), message
        assert_no_token(message)
    finally:
        show_list(holder, user_sid)


def test_a_link_that_cannot_be_looked_at_has_its_folder_checked(tmp_path, user_sid):
    # Review of #163: a link the check cannot lstat, in a folder open to Users. Denying you
    # its attributes, and the folder's listing (which Python's lstat falls back to), makes
    # lstat fail where Windows still follows the link.
    target_folder = tmp_path / "safe"
    target_folder.mkdir()
    make_folder_private(target_folder, user_sid)
    target = write_dotenv(target_folder)
    make_file_private(target, user_sid)
    holder = tmp_path / "open"
    holder.mkdir()
    make_folder_private(holder, user_sid)
    open_folder_to_users(holder)
    link = holder / ".env"
    os.symlink(target, link)
    icacls(link, "/L", "/deny", f"*{user_sid}:(RA)")
    icacls(holder, "/deny", f"*{user_sid}:(RD)")
    try:
        with pytest.raises(OSError):
            os.lstat(link)
        access = _fileaccess.broad_access(link)
        assert access is not None
        assert access.unfollowed == f"{link} could not be looked at, or was not there"
        assert (access.links, access.unlooked) == ((), str(link))
        assert [(f.path, f.groups) for f in access.link_folders] == [
            (real(holder), ("BUILTIN\\Users",))
        ]
        assert access.link_folder == ("BUILTIN\\Users",)
        message = dotenv.shared_message(link, access)
        assert message.startswith(
            f"{link} holds your token, and other users can replace it: BUILTIN\\Users may add "
            f"or remove files in {real(holder)}, which holds {link}, which could not be looked "
            "at, so other people"
        ), message
    finally:
        icacls(holder, "/remove:d", f"*{user_sid}")
        icacls(link, "/L", "/remove:d", f"*{user_sid}")


# NT SERVICE\TrustedInstaller, the account Windows installs its own files as, owns folders
# of Windows itself. It is trusted as an owner, like SYSTEM, so a path through them, such
# as one through the junction C:\Documents and Settings, which C:\ holds, warns of nothing.


def test_windows_own_folders_are_owned_by_no_other_account(user_sid, capsys):
    folders = [Path("C:\\"), Path("C:\\Users"), Path(os.environ["USERPROFILE"])]
    owners = {str(folder): owner(folder) for folder in folders}
    with capsys.disabled():  # shown in CI's log, to say who owns them there
        print(f"\nowners of Windows' own folders: {owners}")
    # On the runner C:\ is TrustedInstaller's, and C:\Users and the profile SYSTEM's.
    for folder in folders:
        assert _fileaccess.owner_is_other(sddl(folder), user_sid) is False, owners


def test_a_dotenv_reached_through_documents_and_settings_has_no_owner_finding(
    private_folder, user_sid, monkeypatch, capsys
):
    junction = Path("C:\\Documents and Settings")
    assert _fileaccess._is_link(str(junction)) is True
    here = real(private_folder)
    assert here.lower().startswith("c:\\users\\"), here
    via = Path(str(junction) + here[len("C:\\Users") :])
    path = write_dotenv(private_folder)
    make_file_private(path, user_sid)
    access = _fileaccess.broad_access(via / ".env")
    assert access is not None
    root = Path("C:\\")
    with capsys.disabled():
        print(f"\n{root} is owned by {owner(root)}; through {junction}: {access!r}")
    # The owner that, untrusted, would be a finding: C:\, which holds the junction, is
    # TrustedInstaller's.
    assert owner(root) == TRUSTED_INSTALLER
    assert access.links == (str(junction),)
    assert [f.path for f in access.link_folders] == ["C:\\"]
    assert access.link_owner is False
    assert not access, access

    monkeypatch.chdir(via)
    caught = shared_warnings(lambda: dotenv.read_value("QTE_TOKEN"))
    assert caught == []


# The computer's built-in Administrator and Guest accounts, which SDDL writes as LA and LG.
# The runner works as the built-in Administrator, so LA is its own account.


def test_la_and_lg_are_resolved_to_this_computers_accounts(user_sid, capsys):
    by_alias = {alias: _fileaccess._alias_sid(alias) for alias in ("LA", "LG")}
    domain = _fileaccess._account_domain_sid()
    local = _fileaccess._local_accounts()
    with capsys.disabled():  # shown in CI's log, to say how they were resolved there
        print(
            f"\nConvertStringSidToSidW: {by_alias}; account domain: {domain}; "
            f"resolved: {local}; user: {user_sid}"
        )
    assert domain is not None and user_sid.startswith(domain + "-")
    assert local == {"LA": f"{domain}-500", "LG": f"{domain}-501"}
    assert local["LA"] == user_sid  # the runner is the built-in Administrator


@pytest.mark.parametrize("where", ["folder", "link folder"])
def test_a_folder_the_builtin_administrator_owns_gives_no_warning(
    private_folder, tmp_path, user_sid, monkeypatch, where
):
    # The runner is the built-in Administrator, so a folder it owns is its own.
    target = write_dotenv(private_folder)
    make_file_private(target, user_sid)
    if where == "folder":
        owned, path = private_folder, target
    else:
        owned = tmp_path / "holder"
        owned.mkdir()
        make_folder_private(owned, user_sid)
        path = owned / ".env"
        os.symlink(target, path)
        monkeypatch.chdir(owned)
    set_owner(owned, user_sid)
    assert owner(owned) == "LA"
    access = _fileaccess.broad_access(path)
    assert access is not None
    assert (access.folder_owner if where == "folder" else access.link_owner) is False, access
    assert not access, access
    assert shared_warnings(lambda: dotenv.read_value("QTE_TOKEN")) == []


# Drives that keep no access lists, such as a USB stick formatted FAT32 or exFAT: anyone
# using the computer may open any file on one. A small virtual disk of each is made with
# diskpart (the runner is elevated) and given a free drive letter.


def diskpart(script: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as file:
        file.write(script)
    try:
        result = subprocess.run(
            ["diskpart", "/s", file.name], capture_output=True, text=True, timeout=180
        )
    finally:
        os.unlink(file.name)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.fixture(params=["fat32", "exfat"])
def drive_without_lists(request, tmp_path):
    """The root of a new drive formatted `request.param`, such as `Q:\\`."""
    vhd = tmp_path / f"{request.param}.vhd"
    letter = next(c for c in "QRSTUVWXYZ" if not os.path.exists(f"{c}:\\"))
    diskpart(
        f'create vdisk file="{vhd}" maximum=64 type=expandable\n'
        f'select vdisk file="{vhd}"\n'
        "attach vdisk\n"
        "create partition primary\n"
        f"format fs={request.param} quick label=QTE\n"
        f"assign letter={letter}\n"
    )
    try:
        yield Path(f"{letter}:\\")
    finally:
        diskpart(f'select vdisk file="{vhd}"\ndetach vdisk\n')


def test_a_token_on_a_drive_without_access_lists_warns(drive_without_lists, monkeypatch, capsys):
    path = drive_without_lists / "token"
    path.write_text(FAKE_TOKEN + "\n", encoding="utf-8")
    raw = {str(p): _fileaccess._read_security(str(p)) for p in (path, drive_without_lists)}
    access = _fileaccess.broad_access(path)
    with capsys.disabled():  # shown in CI's log, to say what Windows gives there
        print(f"\nGetNamedSecurityInfoW (status, SDDL): {raw}; the check: {access!r}")
    # Windows gives an owner of Everyone and no DACL (`O:WDD:NO_ACCESS_CONTROL`), not an
    # error: the check takes it as Everyone may do anything.
    assert access is not None, raw
    assert (access.read, access.write, access.folder) == (("Everyone",),) * 3, (raw, access)
    assert access.other_owner is True and access.unseen == (), (raw, access)
    monkeypatch.setenv("QTE_TOKEN_FILE", str(path))
    values: list[str] = []
    caught = shared_warnings(lambda: values.append(resolve_token()))
    assert values == [FAKE_TOKEN]
    assert [w.category for w in caught] == [TokenFileShared], (raw, access)
    message = uncoded(caught[0].message)
    assert message.startswith(f"{path} holds your token, and Windows lets Everyone read or ")
    assert_no_token(message)


async def test_pacing_holds_the_burst_cap_on_the_real_windows_timers():
    # Windows' timers and monotonic clock tick about every 15.6 ms, and asyncio may run a
    # timer up to a tick early: the pacer must check again after each sleep rather than
    # trust it. 500 sends at a burst cap of 200, measured on the real clock.
    import time

    from qte_sdk.contract.v1.order_entry_pb2 import NewOrder
    from qte_sdk.pacing import Budget, Pacer

    sent: list[float] = []

    class Wire:
        async def send(self, type_, payload):
            sent.append(time.monotonic())

    pacer = Pacer(Budget(sustained_per_minute=1_000_000, burst_per_second=200))
    sender = pacer.wrap(Wire())
    for n in range(500):
        await sender.send("new", NewOrder(request_ref=f"r{n}"))
    most, start = 0, 0
    for end, t in enumerate(sent):
        while sent[start] <= t - 1.0:
            start += 1
        most = max(most, end - start + 1)
    assert most <= 160, most
    # And it is not stuck: 500 sends at 160 a second take a little over 3 s.
    assert sent[-1] - sent[0] < 10
