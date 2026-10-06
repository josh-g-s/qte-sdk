"""Who Windows lets read or change a token file: the SDDL parser, on canned access lists,
and the warning the SDK gives on (simulated) Windows when a broad group can read or change
the token, or change the exchange address."""

import logging
import secrets
import warnings
from pathlib import Path

import pytest

from qte_sdk import _fileaccess
from qte_sdk._fileaccess import BroadAccess
from qte_sdk.dotenv import (
    AddressFileShared,
    FileShared,
    TokenFileShared,
    read_value,
    shared_message,
)
from qte_sdk.session import (
    TOKEN_ENV_VAR,
    TOKEN_FILE_ENV_VAR,
    MissingToken,
    resolve_token,
    resolve_url,
)
from qte_sdk.session import _find_token as find_token

EVERYONE = "Everyone"
AUTHENTICATED = "NT AUTHORITY\\Authenticated Users"
USERS = "BUILTIN\\Users"
INTERACTIVE = "NT AUTHORITY\\INTERACTIVE"
DOMAIN_USERS = "Domain Users"

# A file under a user profile: SYSTEM, Administrators and the owner only. The group, in
# G:, ends in -513 like Domain Users, but it is not an entry of the access list.
PROFILE = (
    "O:S-1-5-21-1111111111-2222222222-3333333333-1001"
    "G:S-1-5-21-1111111111-2222222222-3333333333-513"
    "D:(A;ID;FA;;;SY)(A;ID;FA;;;BA)(A;ID;FA;;;S-1-5-21-1111111111-2222222222-3333333333-1001)"
)
# A file on a second drive, which inherits the drive root's entries: Authenticated Users
# may modify it and Users may read it.
SECOND_DRIVE = "D:AI(A;ID;FA;;;BA)(A;ID;FA;;;SY)(A;ID;0x1301bf;;;AU)(A;ID;0x1200a9;;;BU)"
# The same, as a real Windows 11 D:\ gave it: `icacls` showed Authenticated Users:(I)(F)
# and Users:(I)(RX), so Authenticated Users may read and change it, Users only read it.
REAL_SECOND_DRIVE = "D:AI(A;ID;FA;;;BA)(A;ID;FA;;;SY)(A;ID;FA;;;AU)(A;ID;0x1200a9;;;BU)"


def access(read: list[str], write: list[str]) -> BroadAccess:
    return BroadAccess(read=tuple(read), write=tuple(write))


# The parser


@pytest.mark.parametrize(
    ("sddl", "read", "write"),
    [
        (PROFILE, [], []),
        (SECOND_DRIVE, [AUTHENTICATED, USERS], [AUTHENTICATED]),
        (REAL_SECOND_DRIVE, [AUTHENTICATED, USERS], [AUTHENTICATED]),
        # Everyone, by alias and by SID.
        ("D:(A;;FR;;;WD)", [EVERYONE], []),
        ("D:(A;;FA;;;S-1-1-0)", [EVERYONE], [EVERYONE]),
        # Generic rights: GA reads and writes, GR reads, GW writes, GX does neither.
        ("D:(A;;GA;;;AU)", [AUTHENTICATED], [AUTHENTICATED]),
        ("D:(A;OICI;GR;;;BU)", [USERS], []),
        ("D:(A;;GW;;;BU)", [], [USERS]),
        ("D:(A;;GX;;;WD)", [], []),
        # File rights: FX neither reads nor writes, FW writes.
        ("D:(A;;FX;;;WD)(A;;FW;;;BU)", [], [USERS]),
        ("D:(A;;0x10000000;;;WD)", [EVERYONE], [EVERYONE]),
        ("D:(A;;0x80000000;;;WD)", [EVERYONE], []),
        ("D:(A;;0x40000000;;;WD)", [], [EVERYONE]),
        # Hex masks with neither: execute, and synchronize alone.
        ("D:(A;;0x1200a0;;;BU)(A;;0x100000;;;AU)", [], []),
        # Write data, append data, and the rights to take control (WRITE_DAC, WRITE_OWNER).
        ("D:(A;;0x2;;;BU)", [], [USERS]),
        ("D:(A;;0x4;;;BU)", [], [USERS]),
        ("D:(A;;0x40000;;;BU)(A;;0x80000;;;AU)", [], [AUTHENTICATED, USERS]),
        ("D:(A;;WO;;;IU)", [], [INTERACTIVE]),
        # Decimal is accepted too.
        ("D:(A;;1;;;BU)", [USERS], []),
        # The directory service names for a file's low bits: CC is FILE_READ_DATA, and DC
        # and LC are FILE_WRITE_DATA and FILE_APPEND_DATA.
        ("D:(A;;CCRC;;;BU)", [USERS], []),
        ("D:(A;;DC;;;BU)(A;;LC;;;AU)", [], [AUTHENTICATED, USERS]),
        # WD as a right is WRITE_DAC, not Everyone; AU is a SID here, not an audit entry.
        ("D:(A;;RCWD;;;WD)(A;;FR;;;AU)", [AUTHENTICATED], [EVERYONE]),
        # Interactive users and Domain Users of any domain.
        ("D:(A;;FR;;;IU)(A;;FR;;;S-1-5-21-1-2-3-513)", [INTERACTIVE, DOMAIN_USERS], []),
        ("D:(A;;FR;;;S-1-5-4)(A;;FR;;;DU)", [INTERACTIVE, DOMAIN_USERS], []),
        ("D:(A;;FR;;;S-1-5-11)(A;;FR;;;S-1-5-32-545)", [AUTHENTICATED, USERS], []),
        # Groups come back once each, in a fixed order.
        ("D:(A;;FR;;;BU)(A;;FA;;;WD)(A;ID;FR;;;BU)", [EVERYONE, USERS], [EVERYONE]),
        # Deny entries are ignored, so a deny never hides what an allow gives.
        ("D:(D;;FA;;;WD)(A;;FA;;;BA)", [], []),
        ("D:(D;;FA;;;BU)(A;;FR;;;BU)", [USERS], []),
        # An inherit-only entry applies to the folder's children, not to the object.
        ("D:(A;OICIIO;GA;;;BU)(A;;FA;;;BA)", [], []),
        ("D:(A;OICI;GA;;;BU)", [USERS], [USERS]),
        # A conditional entry, whose condition has parentheses and punctuation of its own.
        ('D:(XA;;FR;;;WD;(@User.Title == "PM" && (Member_of {SID(BA)})))', [EVERYONE], []),
        # Quoted text in a condition may hold any character, parentheses included.
        ('D:(A;;FR;;;BU)(XA;;FR;;;WD;(@User.Title == "Ops("))', [EVERYONE, USERS], []),
        ('D:(XA;;FR;;;AU;(@User.Dept == ":)"))', [AUTHENTICATED], []),
        # Empty rights are a mask of 0, which grants nothing.
        ("D:(A;;;;;BA)(A;;FR;;;BU)", [USERS], []),
        ("D:(A;;;;;WD)", [], []),
        # No DACL at all: Windows checks nothing, so Everyone may read and change it.
        ("D:NO_ACCESS_CONTROL", [EVERYONE], [EVERYONE]),
        ("O:BAG:SY", [EVERYONE], [EVERYONE]),
        ("", [EVERYONE], [EVERYONE]),
        # An empty, protected DACL lets no one in.
        ("D:P", [], []),
        ("D:PAI", [], []),
        # What a FAT or exFAT drive reports.
        ("D:(A;;FA;;;WD)", [EVERYONE], [EVERYONE]),
        # Letters in either case.
        ("d:(a;id;fr;;;bu)", [USERS], []),
        # A SACL after the DACL is skipped.
        ("D:(A;;FA;;;BA)S:(AU;SA;FA;;;WD)", [], []),
    ],
)
def test_who_an_access_list_lets_read_and_change(sddl: str, read: list[str], write: list[str]):
    assert _fileaccess.access_in_sddl(sddl) == access(read, write)


def test_access_is_false_only_when_no_broad_group_has_any():
    assert not BroadAccess()
    assert BroadAccess(read=(USERS,))
    assert BroadAccess(write=(USERS,))


@pytest.mark.parametrize(
    "sddl",
    [
        "garbage",
        "D:(A;;FA;;;WD",
        "D:A;;FA;;;WD)",
        "D:(A;;FA)",
        "D:(A;;ZZ;;;WD)",
        "D:(A;;F;;;WD)",
        "D:(A;;0xZZ;;;WD)",
        "D:(A;QQ;FA;;;WD)",
        "D:XX(A;;FA;;;WD)",
        "D:(A;;FA;;;WD)junk",
        "D:(A;;FA;;;BA)D:(A;;FA;;;WD)",
        "X:(A;;FA;;;WD)",
        ":D(A;;FA;;;WD)",
        "BAD:(A;;FA;;;WD)",
        'D:(XA;;FR;;;WD;(@User.Title == "Ops))',
    ],
)
def test_a_malformed_access_list_gives_unknown(sddl: str):
    assert _fileaccess.access_in_sddl(sddl) is None


def test_nothing_is_checked_off_windows(monkeypatch, tmp_path):
    def must_not_run(path: str) -> str:
        raise AssertionError("the Windows API was called off Windows")

    monkeypatch.setattr(_fileaccess, "on_windows", lambda: False)
    monkeypatch.setattr(_fileaccess, "_read_sddl", must_not_run)
    assert _fileaccess.broad_access(tmp_path / "token") is None


def test_an_api_failure_gives_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(_fileaccess, "on_windows", lambda: True)
    # Off Windows the real call fails at once (there is no ctypes.WinDLL), which stands in
    # for any failure of the API.
    assert _fileaccess.broad_access(tmp_path / "token") is None
    monkeypatch.setattr(_fileaccess, "_read_sddl", lambda path: None)
    assert _fileaccess.broad_access(tmp_path / "token") is None


# The warning, on simulated Windows


def synthetic_token() -> str:
    return secrets.token_urlsafe(32)


def assert_token_absent(token: str, text: str) -> None:
    for start in range(len(token) - 7):
        assert token[start : start + 8] not in text


@pytest.fixture(autouse=True)
def no_token_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    monkeypatch.delenv(TOKEN_FILE_ENV_VAR, raising=False)


class Windows:
    """Makes the SDK act as on Windows, with each file's access list given by the test.
    `os.name` itself cannot be changed: `pathlib` would then fail to make a path."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.sddl: str | None = SECOND_DRIVE
        self.asked: list[str] = []
        monkeypatch.setattr(_fileaccess, "on_windows", lambda: True)
        monkeypatch.setattr(_fileaccess, "_read_sddl", self._read_sddl)

    def _read_sddl(self, path: str) -> str | None:
        self.asked.append(path)
        return self.sddl


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> Windows:
    return Windows(monkeypatch)


def write_dotenv(text: str) -> Path:
    path = Path.cwd() / ".env"
    path.write_text(text)
    path.chmod(0o600)
    return path


def test_a_shared_dotenv_warns_once_naming_the_groups_and_the_fix(windows):
    token = synthetic_token()
    path = write_dotenv(f"QTE_URL=ws://127.0.0.1:8080/ws\nQTE_TOKEN={token}\n")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert resolve_token() == token
        assert resolve_token() == token
        resolve_url()
    shared = [w for w in caught if issubclass(w.category, TokenFileShared)]
    assert len(shared) == 1
    message = str(shared[0].message)
    assert str(path) in message
    assert f"{AUTHENTICATED} read or change it, and {USERS} read it" in message
    assert "read your token or change QTE_URL in it" in message
    assert "%USERPROFILE%" in message and "icacls" in message
    assert "A later release will refuse such a file." in message
    assert_token_absent(token, message)
    # The warning points at the caller, not at the SDK.
    assert shared[0].filename == __file__
    assert windows.asked and all(token not in path for path in windows.asked)


@pytest.mark.parametrize("sddl", [PROFILE, None, "D:(A;;FA;;;WD"])
def test_a_private_or_unknown_dotenv_does_not_warn(windows, sddl):
    windows.sddl = sddl
    token = synthetic_token()
    write_dotenv(f"QTE_TOKEN={token}\n")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert resolve_token() == token


def test_a_readable_dotenv_without_a_token_does_not_warn(windows):
    windows.sddl = "D:(A;;FA;;;BA)(A;ID;0x1200a9;;;BU)"  # Users may read, not change
    write_dotenv("QTE_URL=ws://127.0.0.1:8080/ws\n")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert resolve_url() == "ws://127.0.0.1:8080/ws"


def test_a_changeable_dotenv_setting_only_the_address_warns(windows, monkeypatch):
    windows.sddl = REAL_SECOND_DRIVE
    token = synthetic_token()
    monkeypatch.setenv(TOKEN_ENV_VAR, token)
    path = write_dotenv("QTE_URL=ws://127.0.0.1:8080/ws\n")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert resolve_url() == "ws://127.0.0.1:8080/ws"
        assert resolve_token() == token
    assert len(caught) == 1
    assert caught[0].category is AddressFileShared
    message = str(caught[0].message)
    assert message.startswith(f"{path} sets QTE_URL, the exchange address, and Windows lets ")
    assert f"lets {AUTHENTICATED} change it, so" in message
    assert USERS not in message  # reading the address alone gives nothing away
    assert "change QTE_URL in it to a server of their own" in message
    assert "capture your token when you next connect" in message
    assert "that group's access" in message
    assert_token_absent(token, message)


def test_a_dotenv_with_neither_name_does_not_warn(windows):
    windows.sddl = "D:(A;;FA;;;WD)"
    write_dotenv("OTHER=1\n")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert read_value("QTE_URL") == (None, None)


def test_a_changeable_token_file_says_the_token_could_be_replaced(windows, monkeypatch, tmp_path):
    windows.sddl = "D:(A;;FA;;;BA)(A;;FW;;;AU)"
    token = synthetic_token()
    path = tmp_path / "token"
    path.write_text(token)
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(path))
    with pytest.warns(TokenFileShared) as caught:
        assert resolve_token() == token
    message = str(caught[0].message)
    assert f"lets {AUTHENTICATED} read or change it, so" in message
    assert "could replace your token." in message and "QTE_URL" not in message


def test_a_shared_token_file_warns(windows, monkeypatch, tmp_path):
    windows.sddl = "D:(A;;FR;;;WD)(A;;FA;;;BA)"
    token = synthetic_token()
    path = tmp_path / "token"
    path.write_text(token + "\n")
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(path))
    with pytest.warns(TokenFileShared) as caught:
        assert resolve_token() == token
    assert len(caught) == 1
    message = str(caught[0].message)
    assert str(path) in message and "Everyone" in message
    assert "that group's access" in message
    assert "could read your token." in message and "QTE_URL" not in message
    assert_token_absent(token, message)
    assert windows.asked == [str(path)]


def test_a_missing_token_file_does_not_warn(windows, monkeypatch, tmp_path):
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(tmp_path / "missing"))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(MissingToken):
            resolve_token()


def test_a_warning_made_an_error_is_logged_and_does_not_block(windows, caplog):
    token = synthetic_token()
    write_dotenv(f"QTE_TOKEN={token}\n")
    with warnings.catch_warnings():
        warnings.simplefilter("error", TokenFileShared)
        with caplog.at_level(logging.DEBUG):
            assert resolve_token() == token
    assert "Authenticated Users" in caplog.text
    assert_token_absent(token, caplog.text)


def test_the_token_reaches_no_warning_log_or_exception(windows, caplog, monkeypatch):
    token = synthetic_token()
    write_dotenv(f"QTE_TOKEN={token}\n")
    seen: list[str] = []

    def record(message, category, filename, lineno, file=None, line=None):
        seen.append(f"{message} {category} {filename}:{lineno}")

    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.showwarning = record
        with caplog.at_level(logging.DEBUG):
            found, source, problem = find_token(None)
    assert found == token and problem is None
    assert seen and "Authenticated Users" in seen[0]
    assert_token_absent(token, "\n".join(seen) + caplog.text)


def test_a_failing_access_check_never_carries_the_token(windows, monkeypatch):
    token = synthetic_token()
    write_dotenv(f"QTE_TOKEN={token}\n")

    def failing(path: str) -> str:
        raise OSError("access denied")

    monkeypatch.setattr(_fileaccess, "_read_sddl", failing)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert resolve_token() == token


def test_a_warning_that_cannot_be_shown_neither_blocks_nor_escapes(windows, monkeypatch):
    token = synthetic_token()
    write_dotenv(f"QTE_TOKEN={token}\n")

    def broken(*args, **kwargs):
        raise ValueError("I/O operation on closed file")

    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.showwarning = broken
        assert resolve_token() == token


def test_a_relative_token_file_is_checked_in_each_folder(windows, monkeypatch, tmp_path):
    token = synthetic_token()
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, "token")
    for name in ("first", "second"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "token").write_text(token)
        monkeypatch.chdir(folder)
        with pytest.warns(TokenFileShared) as caught:
            assert resolve_token() == token
        assert str(folder / "token") in str(caught[0].message)
    assert windows.asked == [str(tmp_path / n / "token") for n in ("first", "second")]


@pytest.mark.parametrize("name", ["a$b", "50%off", "tick`s", "wow!", "\u201cteam\u201d"])
def test_no_icacls_command_is_given_for_a_folder_a_shell_would_expand(name, tmp_path):
    message = shared_message(tmp_path / name / ".env", access([USERS], []))
    assert "`icacls" not in message
    assert f"run icacls on the folder that holds it, {tmp_path / name}." in message


def test_the_icacls_command_quotes_the_folder(tmp_path):
    message = shared_message(tmp_path / "my project" / ".env", access([USERS], []))
    assert f'run `icacls "{tmp_path / "my project"}"`.' in message


def test_the_two_kinds_of_shared_file_are_both_file_shared():
    assert issubclass(TokenFileShared, FileShared)
    assert issubclass(AddressFileShared, FileShared)
    assert issubclass(FileShared, UserWarning)
    assert not issubclass(TokenFileShared, AddressFileShared)
    assert not issubclass(AddressFileShared, TokenFileShared)


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("QTE_URL=ws://127.0.0.1:8080/ws\nQTE_TOKEN={token}\n", TokenFileShared),
        ("QTE_TOKEN={token}\n", TokenFileShared),
        ("QTE_URL=ws://127.0.0.1:8080/ws\n", AddressFileShared),
    ],
)
def test_each_kind_is_issued_and_caught_as_file_shared(windows, monkeypatch, text, kind):
    windows.sddl = REAL_SECOND_DRIVE
    token = synthetic_token()
    monkeypatch.setenv(TOKEN_ENV_VAR, token)  # used only when the .env has none
    write_dotenv(text.format(token=token))
    with pytest.warns(FileShared) as caught:
        read_value("QTE_URL")
    assert [w.category for w in caught] == [kind]
    assert_token_absent(token, str(caught[0].message))


def test_a_shared_token_file_issues_token_file_shared(windows, monkeypatch, tmp_path):
    token = synthetic_token()
    path = tmp_path / "token"
    path.write_text(token)
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(path))
    with pytest.warns(FileShared) as caught:
        assert resolve_token() == token
    assert [w.category for w in caught] == [TokenFileShared]
