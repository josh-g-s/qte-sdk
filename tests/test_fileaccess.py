"""Who Windows lets read, change or replace a token file: the SDDL parser, on canned access
lists of files and folders and canned owners, and the warning the SDK gives on (simulated)
Windows when a broad group can read, change or replace the token, or change the exchange
address, or another account owns the file."""

import logging
import os
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
    _icacls,
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

# The user the tests run as, and another account on the same computer.
USER_SID = "S-1-5-21-1111111111-2222222222-3333333333-1001"
OTHER_SID = "S-1-5-21-1111111111-2222222222-3333333333-1002"
# A file under a user profile: SYSTEM, Administrators and the owner only. The group, in
# G:, ends in -513 like Domain Users, but it is not an entry of the access list.
PROFILE = (
    f"O:{USER_SID}"
    "G:S-1-5-21-1111111111-2222222222-3333333333-513"
    f"D:(A;ID;FA;;;SY)(A;ID;FA;;;BA)(A;ID;FA;;;{USER_SID})"
)
# A user's profile folder, as a real Windows 11 gave it (owner added): SYSTEM,
# Administrators and the user, each for the folder and everything in it.
PROFILE_FOLDER = f"O:{USER_SID}D:(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{USER_SID})"
# The root of a second drive, as a real Windows 11 D:\ gave it: Authenticated Users have
# full control, so may add and remove files; Users may read and list (RX), nothing more.
REAL_DRIVE_FOLDER = (
    "D:AI(A;OICIID;FA;;;BA)(A;OICIID;FA;;;SY)(A;OICIID;FA;;;AU)(A;OICIID;0x1200a9;;;BU)"
)
# A file made private with `icacls /inheritance:r /grant:r <you>:F`, owned by you.
PRIVATE_FILE = f"O:{USER_SID}D:PAI(A;;FA;;;{USER_SID})"
# A file on a second drive, which inherits the drive root's entries: Authenticated Users
# may modify it and Users may read it.
SECOND_DRIVE = "D:AI(A;ID;FA;;;BA)(A;ID;FA;;;SY)(A;ID;0x1301bf;;;AU)(A;ID;0x1200a9;;;BU)"
# The same, as a real Windows 11 D:\ gave it: `icacls` showed Authenticated Users:(I)(F)
# and Users:(I)(RX), so Authenticated Users may read and change it, Users only read it.
REAL_SECOND_DRIVE = "D:AI(A;ID;FA;;;BA)(A;ID;FA;;;SY)(A;ID;FA;;;AU)(A;ID;0x1200a9;;;BU)"


def access(
    read: list[str],
    write: list[str],
    folder: list[str] | None = None,
    other_owner: bool | None = None,
) -> BroadAccess:
    return BroadAccess(
        read=tuple(read),
        write=tuple(write),
        folder=None if folder is None else tuple(folder),
        other_owner=other_owner,
    )


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


# A folder's access list


@pytest.mark.parametrize(
    ("sddl", "groups"),
    [
        (PROFILE_FOLDER, []),
        (REAL_DRIVE_FOLDER, [AUTHENTICATED]),
        # Modify (0x1301bf) on a second drive's folder: add files and delete the folder.
        ("D:AI(A;OICIID;0x1301bf;;;AU)(A;OICIID;0x1200a9;;;BU)", [AUTHENTICATED]),
        # Each right that lets a group add or remove files, alone: FILE_ADD_FILE,
        # FILE_DELETE_CHILD, DELETE, GENERIC_WRITE, GENERIC_ALL, WRITE_DAC, WRITE_OWNER.
        ("D:(A;;0x2;;;BU)", [USERS]),
        ("D:(A;;0x40;;;BU)", [USERS]),
        ("D:(A;;0x10000;;;BU)", [USERS]),
        ("D:(A;;0x40000000;;;BU)", [USERS]),
        ("D:(A;;0x10000000;;;BU)", [USERS]),
        ("D:(A;;0x40000;;;BU)", [USERS]),
        ("D:(A;;0x80000;;;BU)", [USERS]),
        # The same as SDDL letters: DC is 0x2 and DT is 0x40 in the directory service names,
        # SD is DELETE, WD WRITE_DAC and WO WRITE_OWNER; and the combined rights that hold one.
        ("D:(A;;DC;;;BU)", [USERS]),
        ("D:(A;;DT;;;BU)", [USERS]),
        ("D:(A;;SD;;;BU)", [USERS]),
        ("D:(A;;WD;;;BU)", [USERS]),
        ("D:(A;;WO;;;BU)", [USERS]),
        ("D:(A;;GA;;;BU)", [USERS]),
        ("D:(A;;GW;;;BU)", [USERS]),
        ("D:(A;;FA;;;BU)", [USERS]),
        ("D:(A;;FW;;;BU)", [USERS]),
        ("D:(A;;KA;;;BU)", [USERS]),
        ("D:(A;;KW;;;BU)", [USERS]),
        # Rights that do not: list (CC, 0x1), add a subfolder (LC, 0x4), read and write
        # attributes, traverse, read the access list, synchronize, and read or execute.
        ("D:(A;;0x1;;;BU)(A;;0x4;;;BU)(A;;0x8;;;BU)(A;;0x10;;;BU)(A;;0x20;;;BU)", []),
        ("D:(A;;0x80;;;BU)(A;;0x100;;;BU)(A;;0x20000;;;BU)(A;;0x100000;;;BU)", []),
        ("D:(A;;CCLCSWRPWPLOCRRC;;;BU)", []),
        ("D:(A;;FR;;;WD)(A;;FX;;;AU)(A;;GR;;;BU)(A;;GX;;;IU)(A;;KR;;;DU)", []),
        ("D:(A;;0x1200a9;;;BU)(A;;0x1301bf;;;BA)", []),
        # Inherit-only entries are for the files and folders inside, not the folder itself,
        # whatever they grant; the files' own lists show what those get.
        ("D:(A;OICIIO;FA;;;AU)", []),
        ("D:(A;CIIO;GA;;;BU)(A;OIIO;GW;;;WD)(A;;0x1200a9;;;BU)", []),
        ("D:(A;OICIIOID;FA;;;AU)(A;OICIID;0x1200a9;;;AU)", []),
        # Entries that apply to the folder and to what is inside count.
        ("D:(A;OI;0x2;;;AU)(A;CINP;0x40;;;BU)", [AUTHENTICATED, USERS]),
        # Deny entries are ignored, as for a file.
        ("D:(D;;FA;;;AU)(A;;FA;;;AU)", [AUTHENTICATED]),
        ("D:(D;;FA;;;WD)(A;;FA;;;BA)", []),
        # Every broad group, by alias or SID, in a fixed order.
        (
            "D:(A;;0x2;;;S-1-5-21-1-2-3-513)(A;;0x2;;;S-1-5-4)(A;;0x2;;;WD)",
            [EVERYONE, INTERACTIVE, DOMAIN_USERS],
        ),
        # No DACL at all lets Everyone do anything; an empty, protected one, no one.
        ("D:NO_ACCESS_CONTROL", [EVERYONE]),
        (f"O:{USER_SID}", [EVERYONE]),
        ("D:P", []),
        # What a FAT or exFAT drive reports.
        ("D:(A;;FA;;;WD)", [EVERYONE]),
    ],
)
def test_who_a_folders_access_list_lets_add_or_remove_files(sddl: str, groups: list[str]):
    assert _fileaccess.folder_access_in_sddl(sddl) == tuple(groups)


@pytest.mark.parametrize("sddl", ["garbage", "D:(A;;FA;;;WD", "D:(A;;ZZ;;;WD)", "D:XX"])
def test_a_malformed_folder_access_list_gives_unknown(sddl: str):
    assert _fileaccess.folder_access_in_sddl(sddl) is None


# The owner


@pytest.mark.parametrize(
    ("sddl", "user", "other"),
    [
        # The current user, by SID, in either case.
        (PROFILE, USER_SID, False),
        (PRIVATE_FILE.lower(), USER_SID, False),
        # BUILTIN\Administrators and SYSTEM, by alias or SID, need not be compared.
        ("O:BAD:(A;;FA;;;BA)", USER_SID, False),
        ("O:BAD:(A;;FA;;;BA)", None, False),
        ("O:SYD:(A;;FA;;;SY)", None, False),
        ("O:S-1-5-32-544G:SY", None, False),
        ("O:S-1-5-18", None, False),
        # Another account, by SID.
        (f"O:{OTHER_SID}D:(A;;FA;;;{OTHER_SID})", USER_SID, True),
        ("O:S-1-5-21-9-9-9-1001D:", USER_SID, True),
        # A group, or a well-known identity, by alias or SID: never the current user.
        ("O:BUD:(A;;FA;;;BA)", USER_SID, True),
        ("O:WD", None, True),
        ("O:AU", None, True),
        ("O:S-1-5-32-545", USER_SID, True),
        # LOCAL SERVICE and NETWORK SERVICE, which a process can run as.
        ("O:LS", "S-1-5-19", False),
        ("O:NS", USER_SID, True),
        # The local Administrator and Guest accounts cannot be compared with a SID.
        ("O:LA", USER_SID, None),
        ("O:LG", USER_SID, None),
        # An owner that cannot be compared, for want of the current user's SID.
        (PROFILE, None, None),
        (f"O:{OTHER_SID}", None, None),
        ("O:LS", None, None),
        # No owner, or one that cannot be read.
        ("D:(A;;FA;;;BA)", USER_SID, None),
        ("O:D:(A;;FA;;;BA)", USER_SID, None),
        ("O:S-1-5-D:(A;;FA;;;BA)", USER_SID, None),
        ("O:XYZ", USER_SID, None),
        ("O:B1", USER_SID, None),
        # Two letters that are not a documented alias.
        ("O:ZZ", USER_SID, None),
        ("O:\u00c5\u00d8", USER_SID, None),
        ("garbage", USER_SID, None),
        ("O:BA(", USER_SID, None),
    ],
)
def test_whether_another_account_owns_a_file(sddl: str, user: str | None, other: bool | None):
    assert _fileaccess.owner_is_other(sddl, user) is other


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


@pytest.mark.skipif(os.name == "nt", reason="ctypes.WinDLL exists on Windows")
def test_the_current_users_sid_is_unknown_off_windows():
    with pytest.raises(AttributeError):  # no ctypes.WinDLL
        _fileaccess._current_user_sid()


class Windows:
    """Makes the SDK act as on Windows, with each file's access list, each folder's, and the
    current user's SID given by the test (an exception is raised instead of returned).
    `os.name` itself cannot be changed: `pathlib` would then fail to make a path."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.sddl: str | Exception | None = SECOND_DRIVE
        self.folder_sddl: str | Exception | None = PROFILE_FOLDER
        self.user: str | Exception | None = USER_SID
        self.asked: list[str] = []
        monkeypatch.setattr(_fileaccess, "on_windows", lambda: True)
        monkeypatch.setattr(_fileaccess, "_read_sddl", self._read_sddl)
        monkeypatch.setattr(_fileaccess, "_current_user_sid", self._current_user_sid)

    def _read_sddl(self, path: str) -> str | None:
        self.asked.append(path)
        return self._give(self.folder_sddl if os.path.isdir(path) else self.sddl)

    def _current_user_sid(self) -> str | None:
        return self._give(self.user)

    @staticmethod
    def _give(value: str | Exception | None) -> str | None:
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> Windows:
    return Windows(monkeypatch)


def test_a_private_file_in_a_profile_folder_has_no_broad_access(windows, tmp_path):
    windows.sddl = PRIVATE_FILE
    path = tmp_path / ".env"
    path.write_text("")
    found = _fileaccess.broad_access(path)
    assert found == access([], [], [], False)
    assert not found and not found.changeable
    assert windows.asked == [str(path), str(tmp_path)]


def test_a_private_file_in_an_open_folder_may_be_replaced(windows, tmp_path):
    windows.sddl = PRIVATE_FILE
    windows.folder_sddl = REAL_DRIVE_FOLDER
    path = tmp_path / ".env"
    path.write_text("")
    found = _fileaccess.broad_access(path)
    assert found == access([], [], [AUTHENTICATED], False)
    assert found and found.changeable


def test_a_file_owned_by_another_account(windows, tmp_path):
    windows.sddl = f"O:{OTHER_SID}D:PAI(A;;FA;;;{USER_SID})"
    path = tmp_path / ".env"
    path.write_text("")
    found = _fileaccess.broad_access(path)
    assert found == access([], [], [], True)
    assert found and found.changeable


def test_the_folder_of_a_relative_path_is_checked(windows, tmp_path, monkeypatch):
    windows.sddl = PRIVATE_FILE
    monkeypatch.chdir(tmp_path)
    (tmp_path / "token").write_text("")
    _fileaccess.broad_access("token")
    assert windows.asked == ["token", str(tmp_path)]


@pytest.mark.parametrize("folder", [None, "D:(A;;FA;;;WD", OSError("access denied")])
def test_a_folder_that_cannot_be_read_is_unknown_and_does_not_hide_the_file(
    windows, tmp_path, folder
):
    windows.sddl = REAL_SECOND_DRIVE
    windows.folder_sddl = folder
    path = tmp_path / ".env"
    path.write_text("")
    assert _fileaccess.broad_access(path) == access([AUTHENTICATED, USERS], [AUTHENTICATED])
    windows.sddl = PRIVATE_FILE
    found = _fileaccess.broad_access(path)
    assert found == access([], [], None, False)
    assert not found


@pytest.mark.parametrize("user", [None, OSError("no token")])
def test_an_owner_that_cannot_be_compared_is_unknown(windows, tmp_path, user):
    windows.sddl = PRIVATE_FILE
    windows.user = user
    path = tmp_path / ".env"
    path.write_text("")
    found = _fileaccess.broad_access(path)
    assert found == access([], [], [], None)
    assert not found
    windows.sddl = f"O:BAD:PAI(A;;FA;;;{USER_SID})"  # Administrators need no comparing
    assert _fileaccess.broad_access(path) == access([], [], [], False)


def test_a_file_that_cannot_be_read_asks_nothing_more(windows, tmp_path):
    windows.sddl = None
    path = tmp_path / ".env"
    path.write_text("")
    assert _fileaccess.broad_access(path) is None
    assert windows.asked == [str(path)]


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
    assert windows.asked == [str(path), str(tmp_path)]


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
    assert windows.asked == [
        str(tmp_path / name / part) for name in ("first", "second") for part in ("token", "")
    ]


@pytest.mark.parametrize("name", ["a$b", "50%off", "tick`s", "wow!", "\u201cteam\u201d"])
def test_no_icacls_command_is_given_for_a_folder_a_shell_would_expand(name, tmp_path):
    message = shared_message(tmp_path / name / ".env", access([USERS], []))
    assert "`icacls" not in message
    assert (
        f"To see who can open the folder and the file, run icacls on the folder that holds "
        f"it, {tmp_path / name}, and on the file itself."
    ) in message


@pytest.mark.parametrize("name", ["50%token", "$token", "token!"])
def test_no_icacls_command_is_given_for_a_file_a_shell_would_expand(name, tmp_path):
    message = shared_message(tmp_path / name, access([USERS], []), sets_address=False)
    assert "`icacls" not in message
    assert f"run icacls on the folder that holds it, {tmp_path}, and on the file itself." in (
        message
    )


def test_the_icacls_commands_quote_the_folder_and_the_file(tmp_path):
    folder = tmp_path / "my project"
    message = shared_message(folder / ".env", access([USERS], []))
    assert (
        f'To see who can open the folder and the file, run `icacls "{folder}"` and '
        f'`icacls "{folder / ".env"}"`.'
    ) in message


def test_a_folder_ending_in_a_backslash_is_not_quoted():
    # Inside double quotes, a backslash before the closing quote would escape it.
    assert _icacls("D:\\", "D:\\.env") == 'run `icacls D:\\` and `icacls "D:\\.env"`'
    assert _icacls("C:\\Users\\me\\proj", "C:\\Users\\me\\proj\\token") == (
        'run `icacls "C:\\Users\\me\\proj"` and `icacls "C:\\Users\\me\\proj\\token"`'
    )
    assert _icacls("\\\\server\\my share\\", "\\\\server\\my share\\.env") == (
        "run icacls on the folder that holds it, \\\\server\\my share\\, and on the file itself"
    )


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


# The folder and the owner, on simulated Windows


def test_a_private_dotenv_in_an_open_folder_warns_about_the_folder_only(windows):
    windows.sddl = PRIVATE_FILE
    windows.folder_sddl = REAL_DRIVE_FOLDER
    token = synthetic_token()
    path = write_dotenv(f"QTE_URL=ws://127.0.0.1:8080/ws\nQTE_TOKEN={token}\n")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert resolve_token() == token
        resolve_url()
    assert [w.category for w in caught] == [TokenFileShared]
    message = str(caught[0].message)
    assert message.startswith(
        f"{path} holds your token, and other users can replace it: {AUTHENTICATED} may add "
        f"or remove files in {path.parent}, so other people who use this computer could "
        "change QTE_URL in it to a server of their own, which would capture your token when "
        "you next connect. Move it into a folder under your user profile (%USERPROFILE%), "
        "which is private by default, or remove that group's access."
    )
    assert "Windows lets" not in message and USERS not in message
    assert "read your token" not in message and "owned by" not in message
    assert f'run `icacls "{path.parent}"` and `icacls "{path}"`' in message
    assert_token_absent(token, message)
    assert windows.asked == [str(path), str(path.parent)]


def test_a_dotenv_owned_by_another_account_warns(windows):
    windows.sddl = f"O:{OTHER_SID}D:PAI(A;;FA;;;{USER_SID})"
    token = synthetic_token()
    path = write_dotenv(f"QTE_TOKEN={token}\n")
    with pytest.warns(TokenFileShared) as caught:
        assert resolve_token() == token
    message = str(caught[0].message)
    assert message.startswith(
        f"{path} holds your token, and it is owned by another account, which can change "
        "who may open it, so other people who use this computer could read your token or "
        "change QTE_URL in it"
    )
    assert "Delete it and make it again yourself, in a folder under your user profile" in message
    assert "remove" not in message and "add or remove files" not in message
    assert_token_absent(token, message)


@pytest.mark.parametrize("owner", ["BA", "SY", USER_SID, "S-1-5-32-544"])
def test_a_dotenv_owned_by_you_administrators_or_system_does_not_warn(windows, owner):
    windows.sddl = f"O:{owner}D:PAI(A;;FA;;;{USER_SID})"
    write_dotenv(f"QTE_TOKEN={synthetic_token()}\n")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        resolve_token()


@pytest.mark.parametrize("user", [None, OSError("no token")])
def test_an_owner_that_cannot_be_compared_does_not_warn(windows, user):
    windows.sddl = f"O:{OTHER_SID}D:PAI(A;;FA;;;{USER_SID})"
    windows.user = user
    write_dotenv(f"QTE_TOKEN={synthetic_token()}\n")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        resolve_token()


@pytest.mark.parametrize("folder", [None, "garbage", OSError("access denied")])
def test_a_folder_that_cannot_be_read_does_not_warn(windows, folder):
    windows.sddl = PRIVATE_FILE
    windows.folder_sddl = folder
    write_dotenv(f"QTE_TOKEN={synthetic_token()}\n")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        resolve_token()


@pytest.mark.parametrize(
    ("folder", "owner"), [(REAL_DRIVE_FOLDER, USER_SID), (PROFILE_FOLDER, OTHER_SID)]
)
def test_a_replaceable_dotenv_setting_only_the_address_warns(windows, monkeypatch, folder, owner):
    windows.sddl = f"O:{owner}D:PAI(A;;FA;;;{USER_SID})"
    windows.folder_sddl = folder
    token = synthetic_token()
    monkeypatch.setenv(TOKEN_ENV_VAR, token)
    path = write_dotenv("QTE_URL=ws://127.0.0.1:8080/ws\n")
    with pytest.warns(AddressFileShared) as caught:
        assert resolve_url() == "ws://127.0.0.1:8080/ws"
    message = str(caught[0].message)
    assert message.startswith(f"{path} sets QTE_URL, the exchange address, and ")
    assert "change QTE_URL in it to a server of their own" in message
    assert "read your token" not in message
    assert_token_absent(token, message)


def test_a_token_file_in_an_open_folder_says_the_token_could_be_replaced(
    windows, monkeypatch, tmp_path
):
    windows.sddl = PRIVATE_FILE
    windows.folder_sddl = REAL_DRIVE_FOLDER
    token = synthetic_token()
    path = tmp_path / "token"
    path.write_text(token)
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(path))
    with pytest.warns(TokenFileShared) as caught:
        assert resolve_token() == token
    message = str(caught[0].message)
    assert f"may add or remove files in {tmp_path}, so" in message
    assert "could replace your token." in message and "QTE_URL" not in message
    assert_token_absent(token, message)


def test_the_folder_and_owner_are_read_before_the_token(windows, monkeypatch):
    windows.sddl = PRIVATE_FILE
    windows.folder_sddl = REAL_DRIVE_FOLDER
    token = synthetic_token()
    path = write_dotenv(f"QTE_TOKEN={token}\n")
    opened: list[str] = []
    real_open = os.open
    fake_sddl = _fileaccess._read_sddl

    def recording_sddl(path: str) -> str | None:
        opened.append(f"list {path}")
        return fake_sddl(path)

    def recording_open(path, *args, **kwargs):
        opened.append(f"open {path}")
        return real_open(path, *args, **kwargs)

    def recording_sid() -> str:
        opened.append("sid")
        return USER_SID

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(_fileaccess, "_read_sddl", recording_sddl)
    monkeypatch.setattr(_fileaccess, "_current_user_sid", recording_sid)
    with pytest.warns(TokenFileShared):
        assert resolve_token() == token
    # No Windows API call is made once the file, and so the token, has been read.
    assert opened == [f"list {path}", f"list {path.parent}", "sid", f"open {path}"]


AT = "C:\\Users\\me\\proj\\.env"
FOLDER = "C:\\Users\\me\\proj"


@pytest.mark.parametrize(
    ("found", "kwargs", "start", "fix"),
    [
        (
            access([AUTHENTICATED, USERS], [AUTHENTICATED], [], False),
            {},
            f"{AT} holds your token, and Windows lets {AUTHENTICATED} read or change it, and "
            f"{USERS} read it, so other people who use this computer could read your token or "
            "change QTE_URL in it",
            "or remove those groups' access.",
        ),
        (
            access([], [], [AUTHENTICATED], False),
            {},
            f"{AT} holds your token, and other users can replace it: {AUTHENTICATED} may add or "
            f"remove files in {FOLDER}, so other people who use this computer could change "
            "QTE_URL in it",
            "or remove that group's access.",
        ),
        (
            access([], [], [], True),
            {},
            f"{AT} holds your token, and it is owned by another account, which can change who "
            "may open it, so other people who use this computer could read your token or "
            "change QTE_URL in it",
            "Delete it and make it again yourself",
        ),
        (
            access([AUTHENTICATED, USERS], [AUTHENTICATED], [AUTHENTICATED], False),
            {},
            f"{AT} holds your token, and Windows lets {AUTHENTICATED} read or change it, and "
            f"{USERS} read it; and other users can replace it: {AUTHENTICATED} may add or "
            f"remove files in {FOLDER}, so other people",
            "or remove those groups' access.",
        ),
        (
            access([], [], [EVERYONE, USERS], True),
            {},
            f"{AT} holds your token, and other users can replace it: {EVERYONE} and {USERS} may "
            f"add or remove files in {FOLDER}; and it is owned by another account, which can "
            "change who may open it, so other people who use this computer could read your "
            "token or change QTE_URL in it",
            "Delete it and make it again yourself",
        ),
        (
            access([USERS], [USERS], [USERS], True),
            {},
            f"{AT} holds your token, and Windows lets {USERS} read or change it; other users "
            f"can replace it: {USERS} may add or remove files in {FOLDER}; and it is owned by "
            "another account, which can change who may open it, so other people",
            "Delete it and make it again yourself",
        ),
        (
            access([USERS], [], [AUTHENTICATED], False),
            {"holds_token": False},
            f"{AT} sets QTE_URL, the exchange address, and other users can replace it: "
            f"{AUTHENTICATED} may add or remove files in {FOLDER}, so other people who use "
            "this computer could change QTE_URL in it",
            "or remove that group's access.",
        ),
        (
            access([], [], [], True),
            {"holds_token": False},
            f"{AT} sets QTE_URL, the exchange address, and it is owned by another account, "
            "which can change who may open it, so other people who use this computer could "
            "change QTE_URL in it",
            "Delete it and make it again yourself",
        ),
        (
            access([], [], [AUTHENTICATED], True),
            {"sets_address": False},
            f"{AT} holds your token, and other users can replace it: {AUTHENTICATED} may add or "
            f"remove files in {FOLDER}; and it is owned by another account, which can change "
            "who may open it, so other people who use this computer could read your token or "
            "replace your token.",
            "Delete it and make it again yourself",
        ),
    ],
)
def test_the_message_for_each_combination(monkeypatch, found, kwargs, start, fix):
    # A Windows path, as the message would show it on Windows.
    monkeypatch.setattr(os.path, "abspath", lambda path: str(path))
    monkeypatch.setattr(os.path, "dirname", lambda path: FOLDER)
    message = shared_message(AT, found, **kwargs)  # type: ignore[arg-type]
    assert message.startswith(start)
    assert fix in message
    assert message.endswith(
        f'To see who can open the folder and the file, run `icacls "{FOLDER}"` and '
        f'`icacls "{AT}"`. A later release will refuse such a file.'
    )
    assert "  " not in message and message.count("; and") <= 1
