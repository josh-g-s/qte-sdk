"""Who Windows lets read, change or replace a token file: the SDDL parser, on canned access
lists of files and folders and canned owners, and the warning the SDK gives on (simulated)
Windows when a broad group can read, change or replace the token, or change the exchange
address, or another account owns the file."""

import logging
import os
import secrets
import sys
import traceback
import warnings
from dataclasses import replace
from pathlib import Path

import pytest

from qte_sdk import _fileaccess, dotenv
from qte_sdk._fileaccess import BroadAccess, LinkFolder
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

# NT SERVICE\TrustedInstaller, as SDDL gives it: by its SID, having no alias.
TRUSTED_INSTALLER = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
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
    folder_owner: bool | None = None,
) -> BroadAccess:
    return BroadAccess(
        read=tuple(read),
        write=tuple(write),
        folder=None if folder is None else tuple(folder),
        other_owner=other_owner,
        folder_owner=folder_owner,
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
        # NT SERVICE\TrustedInstaller, which owns C:\ on current Windows: part of Windows.
        (f"O:{TRUSTED_INSTALLER}D:(A;;FA;;;SY)", USER_SID, False),
        (f"O:{TRUSTED_INSTALLER.lower()}", None, False),
        # Another service's SID, of the same form, is not trusted.
        ("O:S-1-5-80-1-2-3-4-5", USER_SID, True),
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
    `lists` gives the list of a particular path, ahead of `sddl` and `folder_sddl`.
    `os.name` itself cannot be changed: `pathlib` would then fail to make a path."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.sddl: str | Exception | None = SECOND_DRIVE
        self.folder_sddl: str | Exception | None = PROFILE_FOLDER
        self.lists: dict[str, str | Exception | None] = {}
        self.user: str | Exception | None = USER_SID
        self.asked: list[str] = []
        monkeypatch.setattr(_fileaccess, "on_windows", lambda: True)
        monkeypatch.setattr(_fileaccess, "_read_sddl", self._read_sddl)
        monkeypatch.setattr(_fileaccess, "_current_user_sid", self._current_user_sid)

    def _read_sddl(self, path: str) -> str | None:
        self.asked.append(path)
        if path in self.lists:
            return self._give(self.lists[path])
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
    assert found == access([], [], [], False, folder_owner=False)
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
    assert found == access([], [], [], True, folder_owner=False)
    assert found and found.changeable


def test_the_folder_of_a_relative_path_is_checked(windows, tmp_path, monkeypatch):
    windows.sddl = PRIVATE_FILE
    monkeypatch.chdir(tmp_path)
    (tmp_path / "token").write_text("")
    found = _fileaccess.broad_access("token")
    # The file is asked for by the path it resolves to, which is absolute.
    assert windows.asked == [str(tmp_path / "token"), str(tmp_path)]
    assert found is not None and found.links == () and found.link_folders == ()


@pytest.mark.parametrize("folder", [None, OSError("access denied")])
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


@pytest.mark.parametrize("sddl", [PROFILE, None])
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


@pytest.mark.parametrize("folder", [None, OSError("access denied")])
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
    assert opened == [f"list {path}", "sid", f"list {path.parent}", f"open {path}"]


AT = "C:\\Users\\me\\proj\\.env"
FOLDER = "C:\\Users\\me\\proj"
UNSEEN = "Windows would not let this check see who may open"


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
        (
            replace(access([], [], [], False), folder_owner=True),
            {},
            f"{AT} holds your token, and {FOLDER} is owned by another account, which can "
            "change who may add or remove files in it, so other people who use this computer "
            "could change QTE_URL in it to a server of their own, which would capture your "
            "token when you next connect. Move it into a folder under your user profile "
            "(%USERPROFILE%), which is private by default. To see",
            "private by default. To see",
        ),
        (
            replace(access([], [], [USERS], False), folder_owner=True),
            {"sets_address": False},
            f"{AT} holds your token, and other users can replace it: {USERS} may add or remove "
            f"files in {FOLDER}; and {FOLDER} is owned by another account, which can change "
            "who may add or remove files in it, so other people who use this computer could "
            "replace your token.",
            # Removing the group's access would not do: the folder's owner could give it back.
            "Move it into a folder under your user profile (%USERPROFILE%), which is private by "
            "default. To see",
        ),
        (
            replace(access([], [], [], None), unseen=(f"{UNSEEN} {AT}",)),
            {},
            f"{AT} holds your token, but it could not be fully checked: {UNSEEN} {AT}. Delete "
            "it and make it again yourself",
            "Delete it and make it again yourself",
        ),
        (
            replace(
                access([], [], [AUTHENTICATED], None),
                unseen=(
                    f"{UNSEEN} {AT}",
                    f"the access list of {FOLDER} is in a form this check cannot read",
                ),
            ),
            {"holds_token": False},
            f"{AT} sets QTE_URL, the exchange address, and other users can replace it: "
            f"{AUTHENTICATED} may add or remove files in {FOLDER}, so other people who use "
            "this computer could change QTE_URL in it to a server of their own, which would "
            "capture your token when you next connect. Also, it could not be fully checked: "
            f"{UNSEEN} {AT}; and the access list of {FOLDER} is in a form this check cannot "
            "read.",
            "which is private by default. To see",
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


# Links, on simulated Windows. A real symbolic link is used where the system allows one
# (the tests run on macOS and Linux too), and `os.path.realpath` is replaced where a link
# Windows has and POSIX lacks, such as a junction, is meant.


def linked(tmp_path: Path, link_in: str, target_in: str, name: str = ".env") -> tuple[Path, Path]:
    """A file `name` in the folder `target_in` and a symbolic link to it, of the same name,
    in the folder `link_in`, both under `tmp_path`: the link and the file."""
    target = tmp_path / target_in / name
    link = tmp_path / link_in / name
    target.parent.mkdir(exist_ok=True)
    link.parent.mkdir(exist_ok=True)
    target.write_text("")
    target.chmod(0o600)
    try:
        link.symlink_to(target)
    except OSError:  # on Windows, without the right to make one
        pytest.skip("symbolic links cannot be made here")
    return link, target


def test_a_link_is_checked_by_the_file_and_folder_it_links_to_and_its_own_folder(windows, tmp_path):
    link, target = linked(tmp_path, "profile", "open")
    windows.lists = {
        str(target): PRIVATE_FILE,
        str(target.parent): REAL_DRIVE_FOLDER,
        str(link.parent): PROFILE_FOLDER,
    }
    found = _fileaccess.broad_access(link)
    assert windows.asked == [str(target), str(target.parent), str(link.parent)]
    assert found == BroadAccess(
        folder=(AUTHENTICATED,), other_owner=False, link_folder=(), link_owner=False
    )
    assert found.changeable
    assert (found.file, found.folder_path) == (str(target), str(target.parent))
    assert found.links == (str(link),)
    assert found.link_folders == (LinkFolder(str(link.parent), (str(link),), (), False),)


def test_the_folder_of_a_link_is_checked_as_well_as_the_folder_it_links_into(windows, tmp_path):
    link, target = linked(tmp_path, "open", "profile")
    windows.lists = {
        str(target): PRIVATE_FILE,
        str(target.parent): PROFILE_FOLDER,
        str(link.parent): REAL_DRIVE_FOLDER,
    }
    found = _fileaccess.broad_access(link)
    assert found == BroadAccess(
        folder=(), other_owner=False, link_folder=(AUTHENTICATED,), folder_owner=False
    )
    assert found and found.changeable


@pytest.mark.parametrize("folder", [None, OSError("access denied")])
def test_a_link_folder_that_cannot_be_read_is_unknown(windows, tmp_path, folder):
    link, target = linked(tmp_path, "profile", "other")
    windows.lists = {str(target): PRIVATE_FILE, str(link.parent): folder}
    found = _fileaccess.broad_access(link)
    assert found == BroadAccess(folder=(), other_owner=False, link_folder=None, folder_owner=False)
    assert not found


def test_a_file_reached_through_a_linked_folder_checks_where_the_link_is(windows, tmp_path):
    # Like a project folder that is a junction to another drive: whoever may replace the
    # junction in the folder that holds it may point it elsewhere.
    (tmp_path / "private").mkdir()
    (tmp_path / "open").mkdir()
    alias = tmp_path / "open" / "project"
    alias.symlink_to(tmp_path / "private", target_is_directory=True)
    (tmp_path / "private" / ".env").write_text("")
    windows.sddl = PRIVATE_FILE
    windows.lists = {str(tmp_path / "open"): REAL_DRIVE_FOLDER}
    found = _fileaccess.broad_access(alias / ".env")
    assert windows.asked == [
        str(tmp_path / "private" / ".env"),
        str(tmp_path / "private"),
        str(tmp_path / "open"),
    ]
    assert found == BroadAccess(
        folder=(), other_owner=False, link_folder=(AUTHENTICATED,), folder_owner=False
    )
    assert found.links == (str(alias),)
    assert found.link_folders == (
        LinkFolder(str(tmp_path / "open"), (str(alias),), (AUTHENTICATED,)),
    )


def test_a_link_within_the_files_own_folder_adds_no_folder(windows, tmp_path):
    (tmp_path / "real.env").write_text("")
    (tmp_path / ".env").symlink_to(tmp_path / "real.env")
    windows.sddl = PRIVATE_FILE
    found = _fileaccess.broad_access(tmp_path / ".env")
    assert windows.asked == [str(tmp_path / "real.env"), str(tmp_path)]
    assert found is not None and found.links == (str(tmp_path / ".env"),)
    assert found.link_folders == ()  # the link is in the file's own folder


def test_a_folder_reached_by_another_name_is_not_a_link(windows, tmp_path, monkeypatch):
    # A mapped drive, or a short 8.3 name, resolves to another name for the same folder.
    path = tmp_path / "proj" / ".env"
    path.parent.mkdir()
    path.write_text("")

    def elsewhere(p: object, **kwargs: object) -> str:
        return str(p).replace(str(tmp_path), os.path.join(os.sep, "elsewhere"))

    monkeypatch.setattr(os.path, "realpath", elsewhere)
    windows.sddl = PRIVATE_FILE
    found = _fileaccess.broad_access(path)
    assert windows.asked == [elsewhere(path), elsewhere(path.parent)]
    assert found is not None and found.links == () and found.link_folders == ()


class Status:
    """What `os.lstat` gives on Windows: a mode, and the tag of a reparse point."""

    def __init__(self, tag: int) -> None:
        self.st_mode = 0o040755  # a folder
        self.st_reparse_tag = tag


@pytest.mark.parametrize(
    ("tag", "link"),
    [
        (0xA0000003, True),  # IO_REPARSE_TAG_MOUNT_POINT: a junction
        (0xA000000C, True),  # IO_REPARSE_TAG_SYMLINK
        (0x9000601A, False),  # a file kept in the cloud, such as by OneDrive
        (0x80000017, False),  # IO_REPARSE_TAG_WOF, a compressed file
        (0, False),
    ],
)
def test_junctions_and_symbolic_links_are_links_and_other_reparse_points_are_not(
    monkeypatch, tag, link
):
    monkeypatch.setattr(os, "lstat", lambda path: Status(tag))
    assert _fileaccess._is_link("C:\\proj") is link


def test_a_dotenv_linking_into_an_open_folder_warns_about_that_folder(
    windows, tmp_path, monkeypatch
):
    link, target = linked(tmp_path, "profile", "open")
    windows.sddl = PRIVATE_FILE
    windows.lists = {str(target.parent): REAL_DRIVE_FOLDER}
    token = synthetic_token()
    target.write_text(f"QTE_TOKEN={token}\n")
    monkeypatch.chdir(link.parent)
    with pytest.warns(TokenFileShared) as caught:
        assert resolve_token() == token
    message = str(caught[0].message)
    assert message.startswith(
        f"{link}, a link to {target}, holds your token, and other users can replace it: "
        f"{AUTHENTICATED} may add or remove files in {target.parent}, which holds the file "
        "it links to, so other people who use this computer could change QTE_URL in it"
    )
    assert "Move the file it links to, and the link, into a folder under your user" in message
    assert (
        f'To see who can open the folders and the file, run `icacls "{link.parent}"`, '
        f'`icacls "{target.parent}"` and `icacls "{target}"`.'
    ) in message
    assert "which holds the link" not in message
    assert_token_absent(token, message)


def test_a_dotenv_link_in_an_open_folder_warns_about_the_links_folder(
    windows, tmp_path, monkeypatch
):
    link, target = linked(tmp_path, "open", "profile")
    windows.sddl = PRIVATE_FILE
    windows.lists = {str(link.parent): REAL_DRIVE_FOLDER}
    token = synthetic_token()
    target.write_text(f"QTE_TOKEN={token}\n")
    monkeypatch.chdir(link.parent)
    with pytest.warns(TokenFileShared) as caught:
        assert resolve_token() == token
    message = str(caught[0].message)
    assert message.startswith(
        f"{link}, a link to {target}, holds your token, and other users can replace it: "
        f"{AUTHENTICATED} may add or remove files in {link.parent}, which holds the link, so"
    )
    assert "which holds the file it links to" not in message
    assert_token_absent(token, message)


def test_both_open_folders_of_a_linked_token_file_are_named(windows, tmp_path, monkeypatch):
    link, target = linked(tmp_path, "one", "two", name="token")
    windows.sddl = PRIVATE_FILE
    windows.lists = {
        str(target.parent): REAL_DRIVE_FOLDER,
        str(link.parent): "D:(A;OICI;FA;;;BA)(A;OICI;0x1301bf;;;BU)",
    }
    token = synthetic_token()
    target.write_text(token)
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(link))
    with pytest.warns(TokenFileShared) as caught:
        assert resolve_token() == token
    message = str(caught[0].message)
    assert (
        f"other users can replace it: {AUTHENTICATED} may add or remove files in "
        f"{target.parent}, which holds the file it links to, and {USERS} may add or remove "
        f"files in {link.parent}, which holds the link, so other people who use this "
        "computer could replace your token."
    ) in message
    assert "remove those groups' access." in message
    assert_token_absent(token, message)


def test_a_link_is_resolved_and_read_before_the_token(windows, tmp_path, monkeypatch):
    link, target = linked(tmp_path, "profile", "open")
    windows.sddl = PRIVATE_FILE
    windows.lists = {str(target.parent): REAL_DRIVE_FOLDER}
    token = synthetic_token()
    target.write_text(f"QTE_TOKEN={token}\n")
    monkeypatch.chdir(link.parent)
    events: list[str] = []
    real_open, real_realpath, fake_sddl = os.open, os.path.realpath, _fileaccess._read_sddl

    def recording_open(path, *args, **kwargs):
        events.append(f"open {path}")
        return real_open(path, *args, **kwargs)

    def recording_realpath(path, **kwargs):
        # Only the test's own paths: the SDK's own files are resolved later, by
        # `dotenv._caller_level`, to point the warning at the caller.
        if str(path).startswith(str(tmp_path)):
            events.append("realpath")
        return real_realpath(path, **kwargs)

    def recording_sddl(path: str) -> str | None:
        events.append(f"list {path}")
        return fake_sddl(path)

    real_lstat, real_readlink = os.lstat, os.readlink

    def recording_readlink(path, *args, **kwargs):
        events.append("readlink")
        return real_readlink(path, *args, **kwargs)

    def recording_lstat(path, *args, **kwargs):
        if str(path).startswith(str(tmp_path)):
            events.append("lstat")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "lstat", recording_lstat)
    monkeypatch.setattr(os, "readlink", recording_readlink)
    monkeypatch.setattr(os.path, "realpath", recording_realpath)
    monkeypatch.setattr(_fileaccess, "_read_sddl", recording_sddl)
    with pytest.warns(TokenFileShared):
        assert resolve_token() == token
    # realpath is a Windows API call on Windows: it too comes before the token is read.
    assert "realpath" in events and "lstat" in events and "readlink" in events
    last_call = max(i for i, e in enumerate(events) if not e.startswith("open"))
    assert events[last_call + 1 :] == [f"open {link}"]
    assert [e for e in events if e.startswith("list")] == [
        f"list {target}",
        f"list {target.parent}",
        f"list {link.parent}",
    ]


def test_the_icacls_commands_name_both_folders_of_a_link():
    assert _icacls("D:\\data", "D:\\data\\.env", ["C:\\Users\\me\\proj"]) == (
        'run `icacls "C:\\Users\\me\\proj"`, `icacls "D:\\data"` and `icacls "D:\\data\\.env"`'
    )
    assert _icacls("D:\\50%", "D:\\50%\\.env", ["C:\\proj"]) == (
        "run icacls on the folder that holds the link, C:\\proj, on the folder that holds "
        "the file, D:\\50%, and on the file, D:\\50%\\.env"
    )


# Interruptions while warning, on simulated Windows


class Halt(BaseException):
    """An interruption of the SDK's own making, which no `except Exception` catches."""


def shown(error: BaseException) -> str:
    """What a traceback showing local variables could print for `error` and its chain, and
    every text or bytes local of its frames, leaving out this test module's own frames,
    which hold the token by design."""
    parts = [str(error), repr(error), repr(error.args), repr(vars(error))]
    pending = [traceback.TracebackException.from_exception(error, capture_locals=True)]
    while pending:
        link = pending.pop()
        parts.extend(link.format_exception_only())
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


def interrupt_with(kind: type[BaseException]):
    def interrupted(*args, **kwargs):
        raise kind()

    return interrupted


def token_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str) -> str:
    token = synthetic_token()
    if source == ".env":
        write_dotenv(f"QTE_URL=ws://127.0.0.1:8080/ws\nQTE_TOKEN={token}\n")
    else:
        path = tmp_path / "token"
        path.write_text(token + "\n")
        monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(path))
    return token


@pytest.mark.parametrize("source", [".env", TOKEN_FILE_ENV_VAR])
@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit, GeneratorExit, Halt])
def test_an_interruption_while_warning_carries_no_token(
    windows, monkeypatch, tmp_path, source, kind
):
    token = token_source(monkeypatch, tmp_path, source)
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.showwarning = interrupt_with(kind)
        with pytest.raises(kind) as caught:
            resolve_token()
    error = caught.value
    assert type(error) is kind
    assert error.__cause__ is None and error.__context__ is None
    assert_token_absent(token, shown(error))
    # The warning that was cut short is given next time.
    with pytest.warns(TokenFileShared):
        assert resolve_token() == token


@pytest.mark.parametrize("source", [".env", TOKEN_FILE_ENV_VAR])
def test_an_interruption_while_logging_a_warning_made_an_error_carries_no_token(
    windows, monkeypatch, tmp_path, source
):
    token = token_source(monkeypatch, tmp_path, source)

    class Interrupting(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise KeyboardInterrupt

    handler = Interrupting()
    log = logging.getLogger("qte_sdk.dotenv")
    log.addHandler(handler)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", TokenFileShared)
            with pytest.raises(KeyboardInterrupt) as caught:
                resolve_token()
    finally:
        log.removeHandler(handler)
    # Raised while the warning made an error was handled, so it had that as its context.
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert_token_absent(token, shown(caught.value))


def test_an_interruption_while_the_message_is_made_carries_no_token(windows, monkeypatch):
    token = synthetic_token()
    write_dotenv(f"QTE_TOKEN={token}\n")
    monkeypatch.setattr(dotenv, "shared_message", interrupt_with(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt) as caught:
        resolve_url()  # reading the address reads the file that holds the token
    assert caught.value.__context__ is None
    assert_token_absent(token, shown(caught.value))


def test_the_interrupted_frames_no_longer_hold_the_text(windows, monkeypatch):
    # The frames the interruption is raised from are still in its traceback: the text and
    # the value read are let go of first, so a debugger or a crash report sees neither.
    token = synthetic_token()
    write_dotenv(f"QTE_TOKEN={token}\n")
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.showwarning = interrupt_with(KeyboardInterrupt)
        with pytest.raises(KeyboardInterrupt) as caught:
            resolve_token()
    frames = []
    tb = caught.value.__traceback__
    while tb is not None:
        frames.append(tb.tb_frame.f_code.co_name)
        if tb.tb_frame.f_code.co_name == "read_value":
            assert not {"text", "value", "data"} & set(tb.tb_frame.f_locals)
        tb = tb.tb_next
    assert "read_value" in frames and "warn_shared" not in frames


def test_a_token_file_reached_through_a_linked_folder_names_the_link(
    windows, tmp_path, monkeypatch
):
    # A token file, since the working directory is resolved on POSIX, so a `.env` cannot
    # be reached through a link there.
    (tmp_path / "private").mkdir()
    (tmp_path / "open").mkdir()
    alias = tmp_path / "open" / "project"
    alias.symlink_to(tmp_path / "private", target_is_directory=True)
    target = tmp_path / "private" / "token"
    token = synthetic_token()
    target.write_text(token)
    windows.sddl = PRIVATE_FILE
    windows.lists = {str(tmp_path / "open"): REAL_DRIVE_FOLDER}
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(alias / "token"))
    with pytest.warns(TokenFileShared) as caught:
        assert resolve_token() == token
    message = str(caught[0].message)
    assert message.startswith(
        f"{alias / 'token'}, which leads to {target} through the link {alias}, holds your "
        f"token, and other users can replace it: {AUTHENTICATED} may add or remove files in "
        f"{tmp_path / 'open'}, which holds the link {alias}, so other people who use this "
        "computer could replace your token. Move the file it leads to, and the link, into a "
        "folder under your user profile"
    )
    assert_token_absent(token, message)


def test_a_clean_check_through_a_link_names_the_links_folder():
    from qte_sdk.token import _none_of_the_checked_groups

    clean = BroadAccess(
        folder=(),
        other_owner=False,
        link_folder=(),
        link_folders=(LinkFolder("C:\\p", ("C:\\p\\.env",), ()),),
    )
    assert (
        "or add or remove files in its folder or the folder that holds the link it is "
        "reached through, and it is owned by you"
    ) in _none_of_the_checked_groups(clean)
    unknown = replace(clean, link_folder=None, other_owner=None)
    assert (
        "(the folder that holds the link it is reached through and its owner could not be "
        "checked, and other groups and users are not checked)"
    ) in _none_of_the_checked_groups(unknown)


@pytest.mark.parametrize("source", [".env", TOKEN_FILE_ENV_VAR])
def test_an_interruption_before_the_warning_starts_carries_no_token(
    windows, monkeypatch, tmp_path, source
):
    token = token_source(monkeypatch, tmp_path, source)

    class Interrupting(set):
        """Asked first by the check before the file is read, then by `warn_shared`."""

        asked = 0

        def __contains__(self, item: object) -> bool:
            Interrupting.asked += 1
            if Interrupting.asked == 2:
                raise KeyboardInterrupt
            return super().__contains__(item)

    monkeypatch.setattr(dotenv, "_shared_warned", Interrupting())
    with pytest.raises(KeyboardInterrupt) as caught:
        resolve_token()
    assert Interrupting.asked == 2
    assert caught.value.__context__ is None
    assert_token_absent(token, shown(caught.value))


# Chains of links, on simulated Windows: every folder that holds a link on the way is
# checked, since whoever may replace any one of the links may point the chain elsewhere.


def chain(tmp_path: Path) -> tuple[Path, Path, Path]:
    """`private/.env` linking to `shared/redirect.env`, which links to `safe/config.env`:
    the first link, the middle one and the file."""
    for name in ("private", "shared", "safe"):
        (tmp_path / name).mkdir()
    target = tmp_path / "safe" / "config.env"
    target.write_text("")
    target.chmod(0o600)
    middle = tmp_path / "shared" / "redirect.env"
    middle.symlink_to(target)
    first = tmp_path / "private" / ".env"
    first.symlink_to(middle)
    return first, middle, target


def test_every_folder_in_a_chain_of_links_is_checked(windows, tmp_path):
    first, middle, target = chain(tmp_path)
    windows.sddl = PRIVATE_FILE
    windows.lists = {str(middle.parent): REAL_DRIVE_FOLDER}
    found = _fileaccess.broad_access(first)
    assert windows.asked == [str(target), str(target.parent), str(first.parent), str(middle.parent)]
    assert found == BroadAccess(
        folder=(), other_owner=False, link_folder=(AUTHENTICATED,), folder_owner=False
    )
    assert found.links == (str(first), str(middle))
    assert found.link_folders == (
        LinkFolder(str(first.parent), (str(first),), (), False),
        LinkFolder(str(middle.parent), (str(middle),), (AUTHENTICATED,)),
    )


def test_a_dotenv_at_the_start_of_a_chain_names_the_open_middle_folder(
    windows, tmp_path, monkeypatch
):
    first, middle, target = chain(tmp_path)
    windows.sddl = PRIVATE_FILE
    windows.lists = {str(middle.parent): REAL_DRIVE_FOLDER}
    token = synthetic_token()
    target.write_text(f"QTE_TOKEN={token}\n")
    monkeypatch.chdir(first.parent)
    with pytest.warns(TokenFileShared) as caught:
        assert resolve_token() == token
    message = str(caught[0].message)
    assert message.startswith(
        f"{first}, a link that leads to {target} through the link {middle}, holds your "
        f"token, and other users can replace it: {AUTHENTICATED} may add or remove files in "
        f"{middle.parent}, which holds the link {middle}, so other people who use this "
        "computer could change QTE_URL in it"
    )
    assert "Move the file it leads to, and the links, into a folder" in message
    assert (
        f'run `icacls "{first.parent}"`, `icacls "{middle.parent}"`, '
        f'`icacls "{target.parent}"` and `icacls "{target}"`.'
    ) in message
    assert_token_absent(token, message)


def test_a_junction_above_a_file_link_checks_both_holding_folders(windows, tmp_path):
    # open/project is a link to a folder (a junction, on Windows) and the .env in it is a
    # link to a file elsewhere: both open and project's real folder hold a link.
    for name in ("open", "project", "elsewhere"):
        (tmp_path / name).mkdir()
    target = tmp_path / "elsewhere" / "real.env"
    target.write_text("")
    (tmp_path / "project" / ".env").symlink_to(target)
    junction = tmp_path / "open" / "project"
    junction.symlink_to(tmp_path / "project", target_is_directory=True)
    windows.sddl = PRIVATE_FILE
    windows.lists = {str(tmp_path / "open"): REAL_DRIVE_FOLDER}
    found = _fileaccess.broad_access(junction / ".env")
    assert found == BroadAccess(
        folder=(), other_owner=False, link_folder=(AUTHENTICATED,), folder_owner=False
    )
    assert found.links == (str(junction), str(tmp_path / "project" / ".env"))
    assert found.link_folders == (
        LinkFolder(str(tmp_path / "open"), (str(junction),), (AUTHENTICATED,)),
        LinkFolder(str(tmp_path / "project"), (str(tmp_path / "project" / ".env"),), (), False),
    )


def test_a_relative_link_is_followed_from_the_links_folder(windows, tmp_path):
    first, middle, target = chain(tmp_path)
    first.unlink()
    first.symlink_to(os.path.join("..", "shared", "redirect.env"))
    windows.sddl = PRIVATE_FILE
    found = _fileaccess.broad_access(first)
    assert found is not None and found.file == str(target)
    assert found.links == (str(first), str(middle))


def test_links_in_one_folder_are_named_together(windows, tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "shared").mkdir()
    (tmp_path / "safe").mkdir()
    target = tmp_path / "safe" / "real.env"
    target.write_text("")
    second = tmp_path / "shared" / "b.env"
    second.symlink_to(target)
    first = tmp_path / "shared" / "a.env"
    first.symlink_to(second)
    (tmp_path / "proj" / ".env").symlink_to(first)
    windows.sddl = PRIVATE_FILE
    windows.lists = {str(tmp_path / "shared"): REAL_DRIVE_FOLDER}
    found = _fileaccess.broad_access(tmp_path / "proj" / ".env")
    assert found is not None
    assert found.link_folders == (
        LinkFolder(str(tmp_path / "proj"), (str(tmp_path / "proj" / ".env"),), (), False),
        LinkFolder(str(tmp_path / "shared"), (str(first), str(second)), (AUTHENTICATED,)),
    )
    message = shared_message(tmp_path / "proj" / ".env", found, sets_address=False)
    assert f"which holds the links {first} and {second}, so" in message


def test_a_folder_reached_twice_through_a_junction_above_it_is_not_a_loop(tmp_path):
    # proj/up links to proj itself, so proj/up/up/.env is proj/.env: the same link is met
    # twice, with fewer names left each time.
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / ".env").write_text("")
    up = tmp_path / "proj" / "up"
    up.symlink_to(tmp_path / "proj", target_is_directory=True)
    walked, unfollowed = _fileaccess.links_on_the_way(up / "up" / ".env")
    assert walked == [(str(up), str(tmp_path / "proj"))] * 2 and unfollowed is None


def make_chain(tmp_path: Path, count: int) -> Path:
    """A chain of `count` links, each to the next, ending at a file; the first link."""
    target = tmp_path / "file.env"
    target.write_text("")
    following = target
    for number in range(count, 0, -1):
        link = tmp_path / f"link{number}.env"
        link.symlink_to(following)
        following = link
    return following


def test_a_chain_of_the_most_links_is_followed(tmp_path):
    walked, unfollowed = _fileaccess.links_on_the_way(make_chain(tmp_path, _fileaccess.MAX_LINKS))
    assert len(walked) == _fileaccess.MAX_LINKS and unfollowed is None


def test_a_chain_of_too_many_links_stops_and_says_so(tmp_path):
    walked, unfollowed = _fileaccess.links_on_the_way(
        make_chain(tmp_path, _fileaccess.MAX_LINKS + 1)
    )
    assert len(walked) == _fileaccess.MAX_LINKS + 1
    assert unfollowed == f"there are more than {_fileaccess.MAX_LINKS} links on the way"


def test_a_loop_of_links_stops_and_says_so(tmp_path):
    a, b = tmp_path / "a.env", tmp_path / "b.env"
    a.symlink_to(b)
    b.symlink_to(a)
    walked, unfollowed = _fileaccess.links_on_the_way(a)
    assert [link for link, _ in walked] == [str(a), str(b)]
    assert unfollowed == f"the link {a} leads round in a loop"


def test_a_name_that_is_not_there_cannot_be_looked_at(tmp_path):
    assert _fileaccess._is_link(str(tmp_path / "missing" / ".env")) is None


def test_a_link_removed_while_the_check_runs_stops_the_walk(windows, tmp_path, monkeypatch):
    # Whoever may write in the folder of the middle link could remove it after the file
    # was found and put it back before the file is read.
    first, middle, target = chain(tmp_path)
    real_lstat = os.lstat

    def gone(p, *args, **kwargs):
        if str(p) == str(middle) and sys._getframe(1).f_code.co_name == "_is_link":
            raise FileNotFoundError(str(p))
        return real_lstat(p, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", gone)
    windows.sddl = PRIVATE_FILE
    found = _fileaccess.broad_access(first)
    assert found is not None and found
    assert found.unfollowed == f"{middle} could not be looked at, or was not there"


# Links that cannot be followed. Whoever may write in a folder on the way could make their
# link one that cannot be followed, to hide where it leads, so the SDK warns when the file
# is read, as for an open folder, and still checks the folders of the links met before it.
# The file itself stays readable here: the walk is made to fail where the system's own
# resolution does not, as a link Windows follows but the SDK cannot would.


def three_links(tmp_path: Path, first: str, text: str) -> tuple[Path, Path, Path, Path]:
    """proj/<first> -> shared/redirect.env -> mnt/hop.env -> safe/real.env, with `text` in
    real.env: the first link, the second, the third and the file."""
    for name in ("proj", "shared", "mnt", "safe"):
        (tmp_path / name).mkdir(exist_ok=True)
    target = tmp_path / "safe" / "real.env"
    target.write_text(text)
    target.chmod(0o600)
    hop = tmp_path / "mnt" / "hop.env"
    hop.symlink_to(target)
    middle = tmp_path / "shared" / "redirect.env"
    middle.symlink_to(hop)
    link = tmp_path / "proj" / first
    link.symlink_to(middle)
    return link, middle, hop, target


def from_the_walk() -> bool:
    """Whether the caller's caller is the SDK's own link walk, not the system's resolution
    (`os.path.realpath`), which is left to succeed."""
    return sys._getframe(2).f_code.co_name == "links_on_the_way"


def unfollowable(monkeypatch: pytest.MonkeyPatch, cause: str, hop: Path) -> str:
    """Make the walk fail at `hop` for `cause`; what the warning should say about it."""
    real_readlink, real_lstat = os.readlink, os.lstat

    def readlink_giving(value):
        def readlink(p, *args, **kwargs):
            if str(p) == str(hop) and from_the_walk():
                if isinstance(value, Exception):
                    raise value
                return value
            return real_readlink(p, *args, **kwargs)

        monkeypatch.setattr(os, "readlink", readlink)

    if cause == "loop":
        readlink_giving(str(hop))
        return f"the link {hop} leads round in a loop"
    if cause == "too many":
        monkeypatch.setattr(_fileaccess, "MAX_LINKS", 2)
        return "there are more than 2 links on the way"
    if cause == "unreadable":
        readlink_giving(PermissionError("access denied"))
        return f"the link {hop} could not be read"
    if cause == "volume":
        readlink_giving("\\??\\Volume{12345678-1234-1234-1234-123456789abc}\\safe\\real.env")
        return (
            f"the link {hop} uses a form of path this check cannot follow, such as a volume's "
            "own name"
        )
    assert cause == "not looked at"

    def denied(p, *args, **kwargs):
        if str(p) == str(hop) and sys._getframe(1).f_code.co_name == "_is_link":
            raise PermissionError("access denied")
        return real_lstat(p, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", denied)
    return f"{hop} could not be looked at, or was not there"


CAUSES = ["loop", "too many", "unreadable", "volume", "not looked at"]


@pytest.mark.parametrize("cause", CAUSES)
@pytest.mark.parametrize("source", [".env", TOKEN_FILE_ENV_VAR])
def test_a_link_that_cannot_be_followed_warns_when_the_token_is_read(
    windows, tmp_path, monkeypatch, cause, source
):
    # The reviewer's chain: the folder of the second link is open, and the third cannot be
    # followed. Both are said.
    token = synthetic_token()
    if source == ".env":
        first, middle, hop, target = three_links(tmp_path, ".env", f"QTE_TOKEN={token}\n")
        monkeypatch.chdir(first.parent)
    else:
        first, middle, hop, target = three_links(tmp_path, "token", token)
        monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(first))
    windows.sddl = PRIVATE_FILE
    windows.lists = {str(middle.parent): REAL_DRIVE_FOLDER}
    reason = unfollowable(monkeypatch, cause, hop)
    with pytest.warns(TokenFileShared) as caught:
        assert resolve_token() == token
    message = str(caught[0].message)
    assert message.startswith(f"{first}, a link that leads on through the link")
    assert (
        f"other users can replace it: {AUTHENTICATED} may add or remove files in "
        f"{middle.parent}, which holds the link {middle}, so other people"
    ) in message
    assert (
        f"Also, it could not be fully checked: {reason}; a link on the way could not be "
        "followed, so check where it leads."
    ) in message
    assert_token_absent(token, message)


@pytest.mark.parametrize("cause", CAUSES)
@pytest.mark.parametrize("source", [".env", TOKEN_FILE_ENV_VAR])
def test_a_link_that_cannot_be_followed_warns_on_its_own(
    windows, tmp_path, monkeypatch, cause, source
):
    token = synthetic_token()
    if source == ".env":
        first, middle, hop, target = three_links(tmp_path, ".env", f"QTE_TOKEN={token}\n")
        monkeypatch.chdir(first.parent)
    else:
        first, middle, hop, target = three_links(tmp_path, "token", token)
        monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(first))
    windows.sddl = PRIVATE_FILE  # every folder private: the walk is the only finding
    reason = unfollowable(monkeypatch, cause, hop)
    found = _fileaccess.broad_access(first)
    assert found is not None and found.unfollowed == reason
    assert found and not found.changeable
    with pytest.warns(TokenFileShared) as caught:
        assert resolve_token() == token
    message = str(caught[0].message)
    assert message.startswith(
        f"{first}, a link that leads on through the link"
    ) and message.endswith(" A later release will refuse such a file.")
    assert (
        f"holds your token, but it could not be fully checked: {reason}; a link on the way "
        "could not be followed, so check where it leads. Keep the file itself, not a link to "
        "it, in a folder under your user profile (%USERPROFILE%), which is private by default."
    ) in message
    assert "other people" not in message
    assert_token_absent(token, message)


def test_an_address_file_reached_through_a_link_that_cannot_be_followed_warns(
    windows, tmp_path, monkeypatch
):
    first, middle, hop, target = three_links(tmp_path, ".env", "QTE_URL=ws://127.0.0.1:8080/ws\n")
    monkeypatch.chdir(first.parent)
    windows.sddl = PRIVATE_FILE
    reason = unfollowable(monkeypatch, "volume", hop)
    with pytest.warns(AddressFileShared) as caught:
        assert resolve_url() == "ws://127.0.0.1:8080/ws"
    assert (
        f"sets QTE_URL, the exchange address, but it could not be fully checked: {reason};"
    ) in str(caught[0].message)


def test_the_folders_of_the_links_met_before_one_that_cannot_be_followed_are_checked(
    windows, tmp_path, monkeypatch
):
    first, middle, hop, target = three_links(tmp_path, ".env", "")
    windows.sddl = PRIVATE_FILE
    windows.lists = {str(middle.parent): REAL_DRIVE_FOLDER}
    unfollowable(monkeypatch, "unreadable", hop)
    found = _fileaccess.broad_access(first)
    assert found is not None
    assert found.links == (str(first), str(middle), str(hop))
    assert [(f.path, f.groups) for f in found.link_folders] == [
        (str(first.parent), ()),
        (str(middle.parent), (AUTHENTICATED,)),
        (str(hop.parent), ()),
    ]
    assert found.link_folder == (AUTHENTICATED,)


def test_a_check_that_fails_outright_still_gives_no_warning(windows, tmp_path, monkeypatch):
    # Only a walk that stops on Windows warns: an access list that cannot be read at all
    # gives an unknown result, as before.
    token = synthetic_token()
    first, middle, hop, target = three_links(tmp_path, ".env", f"QTE_TOKEN={token}\n")
    monkeypatch.chdir(first.parent)
    windows.sddl = None
    unfollowable(monkeypatch, "volume", hop)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert resolve_token() == token


def test_a_link_folder_that_cannot_be_resolved_is_checked_by_its_walked_name(
    windows, tmp_path, monkeypatch
):
    first, middle, target = chain(tmp_path)
    windows.sddl = PRIVATE_FILE
    windows.lists = {str(middle.parent): REAL_DRIVE_FOLDER}
    real = os.path.realpath

    def failing(path, *args, **kwargs):
        if str(path) == str(middle.parent):
            raise OSError("cannot resolve")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", failing)
    found = _fileaccess.broad_access(first)
    assert found is not None and found.unfollowed is None
    assert found.link_folder == (AUTHENTICATED,)


@pytest.mark.parametrize(
    ("target", "plain"),
    [
        ("\\\\?\\C:\\proj", "C:\\proj"),
        ("\\??\\C:\\proj", "C:\\proj"),
        ("\\\\?\\UNC\\server\\share\\proj", "\\\\server\\share\\proj"),
        ("C:\\proj", "C:\\proj"),
        ("..\\shared\\redirect.env", "..\\shared\\redirect.env"),
        ("\\\\?\\C:", "C:"),
        # A volume's GUID name, or another namespace, is not followed: the walk stops.
        ("\\??\\Volume{12345678-1234-1234-1234-123456789abc}\\shared", None),
        ("\\\\?\\GLOBALROOT\\Device\\HarddiskVolume2\\shared", None),
        ("\\\\?\\C:shared", None),
    ],
)
def test_the_prefix_windows_gives_a_junctions_target_is_removed(target, plain):
    assert _fileaccess._without_prefix(target) == plain


# Lists the check is not shown, or cannot read, and the owners of folders. Whoever owns a
# file or folder, or holds WRITE_DAC on it, can withhold READ_CONTROL from you, or write an
# entry the parser does not know, to hide who may open the file or replace it; and whoever
# owns a folder can change who may add or remove files in it. Each is a finding.

DENIED = _fileaccess.DENIED
# An access list the parser rejects, with an owner it can still read: a right it does not
# know, as an entry Windows would accept but the parser does not understand.
UNPARSABLE = "O:BAD:(A;;ZZ;;;WD)"
# A folder like a profile folder, but owned by another account.
OTHER_FOLDER = f"O:{OTHER_SID}D:(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{USER_SID})"


def not_shown(path: Path | str, folder: bool = False) -> str:
    what = "add or remove files in" if folder else "open"
    return f"Windows would not let this check see who may {what} {path}"


def not_parsed(path: Path | str) -> str:
    return f"the access list of {path} is in a form this check cannot read"


def test_a_file_whose_list_is_not_shown_still_has_its_folder_checked(windows, tmp_path):
    windows.sddl = DENIED
    windows.folder_sddl = REAL_DRIVE_FOLDER
    path = tmp_path / ".env"
    path.write_text("")
    found = _fileaccess.broad_access(path)
    assert windows.asked == [str(path), str(tmp_path)]
    assert found == replace(access([], [], [AUTHENTICATED], None), unseen=(not_shown(path),))
    assert found and found.incomplete and found.changeable


def test_a_file_whose_list_is_not_shown_still_has_its_link_folders_checked(windows, tmp_path):
    first, middle, target = chain(tmp_path)
    windows.lists = {str(target): DENIED, str(middle.parent): REAL_DRIVE_FOLDER}
    found = _fileaccess.broad_access(first)
    assert windows.asked == [str(target), str(target.parent), str(first.parent), str(middle.parent)]
    assert found is not None and found.unseen == (not_shown(target),)
    assert found.link_folder == (AUTHENTICATED,)
    message = shared_message(first, found)
    assert message.startswith(
        f"{first}, a link that leads to {target} through the link {middle}, holds your "
        f"token, and other users can replace it: {AUTHENTICATED} may add or remove files in "
        f"{middle.parent}, which holds the link {middle}, so other people"
    )
    assert f"Also, it could not be fully checked: {not_shown(target)}." in message


@pytest.mark.parametrize(
    ("folder", "unseen"),
    [(DENIED, lambda f: not_shown(f, folder=True)), (UNPARSABLE, not_parsed)],
)
def test_a_folder_whose_list_is_not_shown_or_not_parsed_is_a_finding(
    windows, tmp_path, folder, unseen
):
    windows.sddl = PRIVATE_FILE
    windows.folder_sddl = folder
    path = tmp_path / ".env"
    path.write_text("")
    found = _fileaccess.broad_access(path)
    # The owner of a list the parser rejects is still compared.
    owner = None if folder is DENIED else False
    assert found == replace(
        access([], [], None, False, folder_owner=owner), unseen=(unseen(tmp_path),)
    )
    assert found and not found.changeable


def test_a_file_whose_list_is_not_parsed_is_a_finding(windows, tmp_path):
    windows.sddl = UNPARSABLE
    windows.folder_sddl = REAL_DRIVE_FOLDER
    path = tmp_path / ".env"
    path.write_text("")
    found = _fileaccess.broad_access(path)
    # The owner, which the parser could still read, is compared.
    assert found == replace(access([], [], [AUTHENTICATED], False), unseen=(not_parsed(path),))


@pytest.mark.parametrize("folder", [DENIED, UNPARSABLE])
def test_a_link_folder_whose_list_is_not_shown_or_not_parsed_is_a_finding(
    windows, tmp_path, folder
):
    link, target = linked(tmp_path, "profile", "other")
    windows.sddl = PRIVATE_FILE
    windows.lists = {str(link.parent): folder}
    found = _fileaccess.broad_access(link)
    said = not_shown(link.parent, folder=True) if folder is DENIED else not_parsed(link.parent)
    assert found == BroadAccess(
        folder=(),
        other_owner=False,
        link_folder=None,
        folder_owner=False,
        link_owner=None if folder is DENIED else False,
        unseen=(said,),
    )
    assert found and not found.changeable


def test_a_folder_owned_by_another_account_is_a_finding(windows, tmp_path):
    windows.sddl = PRIVATE_FILE
    windows.folder_sddl = OTHER_FOLDER
    path = tmp_path / ".env"
    path.write_text("")
    found = _fileaccess.broad_access(path)
    assert found == access([], [], [], False, folder_owner=True)
    assert found and found.changeable


@pytest.mark.parametrize(
    ("owner", "other"),
    [
        (USER_SID, False),
        ("BA", False),
        ("SY", False),
        (OTHER_SID, True),
        ("BU", True),
        ("LA", None),
    ],
)
def test_a_folders_owner_is_compared_like_the_files(windows, tmp_path, owner, other):
    windows.sddl = PRIVATE_FILE
    windows.folder_sddl = f"O:{owner}D:(A;OICI;FA;;;{USER_SID})"
    path = tmp_path / ".env"
    path.write_text("")
    found = _fileaccess.broad_access(path)
    assert found is not None and found.folder_owner is other
    assert bool(found) is bool(other)


def test_a_link_folder_owned_by_another_account_is_a_finding(windows, tmp_path):
    first, middle, target = chain(tmp_path)
    windows.sddl = PRIVATE_FILE
    windows.lists = {str(middle.parent): OTHER_FOLDER}
    found = _fileaccess.broad_access(first)
    assert found == BroadAccess(
        folder=(), other_owner=False, link_folder=(), folder_owner=False, link_owner=True
    )
    assert found.link_folders[1] == LinkFolder(str(middle.parent), (str(middle),), (), True)
    message = shared_message(first, found)
    assert (
        f"holds your token, and {middle.parent}, which holds the link {middle}, is owned by "
        "another account, which can change who may add or remove files in it, so other "
        "people who use this computer could change QTE_URL in it"
    ) in message
    assert "Move the file it leads to, and the links, into a folder under your user profile" in (
        message
    )
    assert "remove" not in message.split("Move", 1)[1].split("To see")[0]


def test_the_folder_of_a_name_that_could_not_be_looked_at_is_checked(
    windows, tmp_path, monkeypatch
):
    # Review of #163: an lstat that fails on a link in an open folder stopped the walk
    # before that folder was recorded, so it was neither checked nor named.
    first, middle, hop, target = three_links(tmp_path, ".env", "")
    windows.sddl = PRIVATE_FILE
    windows.lists = {str(hop.parent): REAL_DRIVE_FOLDER}
    reason = unfollowable(monkeypatch, "not looked at", hop)
    found = _fileaccess.broad_access(first)
    assert found is not None and found.unfollowed == reason
    # The name is not known to be a link, so it is not named as one, but its folder, which
    # may hold one, is checked.
    assert (found.links, found.unlooked) == ((str(first), str(middle)), str(hop))
    assert found.link_folder == (AUTHENTICATED,)
    assert (str(hop.parent), (str(hop),), (AUTHENTICATED,)) in [
        (f.path, f.links, f.groups) for f in found.link_folders
    ]
    message = shared_message(first, found)
    assert message.startswith(f"{first}, a link that leads on through the link {middle}, ")
    assert (
        f"{AUTHENTICATED} may add or remove files in {hop.parent}, which holds {hop}, which "
        "could not be looked at, so other people"
    ) in message


@pytest.mark.parametrize("status", [None, OSError("the API failed")])
def test_other_failures_to_read_a_list_still_say_nothing(windows, tmp_path, status):
    windows.sddl = PRIVATE_FILE
    windows.folder_sddl = status
    path = tmp_path / ".env"
    path.write_text("")
    found = _fileaccess.broad_access(path)
    assert found is not None and found.unseen == () and not found
    windows.sddl = status
    assert _fileaccess.broad_access(path) is None


def test_nothing_is_reported_denied_off_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(_fileaccess, "on_windows", lambda: False)
    monkeypatch.setattr(_fileaccess, "_read_sddl", lambda path: DENIED)
    assert _fileaccess.broad_access(tmp_path / ".env") is None


# Each, when the token is read, from a .env and from the file named by QTE_TOKEN_FILE.


def token_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str, token: str) -> Path:
    """The file holding `token` that `source` names, in `tmp_path`, the working directory."""
    if source == ".env":
        return write_dotenv(f"QTE_URL=ws://127.0.0.1:8080/ws\nQTE_TOKEN={token}\n")
    path = tmp_path / "token"
    path.write_text(token)
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(path))
    return path


# What each case sets, and what the warning then says, after the file's name.
UNSEEN_CASES = {
    "file not shown": (
        {"sddl": DENIED, "folder_sddl": PROFILE_FOLDER},
        lambda path: (
            f"holds your token, but it could not be fully checked: {not_shown(path)}. "
            "Delete it and make it again yourself, in a folder under your user profile"
        ),
    ),
    "file not parsed": (
        {"sddl": UNPARSABLE, "folder_sddl": PROFILE_FOLDER},
        lambda path: f"holds your token, but it could not be fully checked: {not_parsed(path)}.",
    ),
    "folder not shown": (
        {"sddl": PRIVATE_FILE, "folder_sddl": DENIED},
        lambda path: (
            "holds your token, but it could not be fully checked: "
            f"{not_shown(path.parent, folder=True)}."
        ),
    ),
    "folder not parsed": (
        {"sddl": PRIVATE_FILE, "folder_sddl": UNPARSABLE},
        lambda path: (
            f"holds your token, but it could not be fully checked: {not_parsed(path.parent)}."
        ),
    ),
    "folder owner": (
        {"sddl": PRIVATE_FILE, "folder_sddl": OTHER_FOLDER},
        lambda path: (
            f"holds your token, and {path.parent} is owned by another account, which "
            "can change who may add or remove files in it, so other people who use this computer "
            "could"
        ),
    ),
}


@pytest.mark.parametrize("case", list(UNSEEN_CASES))
@pytest.mark.parametrize("source", [".env", TOKEN_FILE_ENV_VAR])
def test_each_case_warns_when_the_token_is_read(windows, monkeypatch, tmp_path, case, source):
    settings, said = UNSEEN_CASES[case]
    for name, value in settings.items():
        setattr(windows, name, value)
    token = synthetic_token()
    path = token_in(monkeypatch, tmp_path, source, token)
    with pytest.warns(TokenFileShared) as caught:
        assert resolve_token() == token
    assert len(caught) == 1
    message = str(caught[0].message)
    assert message.startswith(f"{path} {said(path)}"), message
    assert message.endswith(" A later release will refuse such a file.")
    assert_token_absent(token, message)


@pytest.mark.parametrize("case", list(UNSEEN_CASES))
def test_each_case_warns_about_an_address_only_dotenv(windows, monkeypatch, case):
    settings, _ = UNSEEN_CASES[case]
    for name, value in settings.items():
        setattr(windows, name, value)
    token = synthetic_token()
    monkeypatch.setenv(TOKEN_ENV_VAR, token)
    path = write_dotenv("QTE_URL=ws://127.0.0.1:8080/ws\n")
    with pytest.warns(AddressFileShared) as caught:
        assert resolve_url() == "ws://127.0.0.1:8080/ws"
    assert str(caught[0].message).startswith(f"{path} sets QTE_URL, the exchange address, ")
    assert_token_absent(token, str(caught[0].message))


@pytest.mark.parametrize("source", [".env", TOKEN_FILE_ENV_VAR])
def test_a_link_folder_not_shown_warns_when_the_token_is_read(
    windows, monkeypatch, tmp_path, source
):
    token = synthetic_token()
    if source == ".env":
        first, middle, hop, target = three_links(tmp_path, ".env", f"QTE_TOKEN={token}\n")
        monkeypatch.chdir(first.parent)
    else:
        first, middle, hop, target = three_links(tmp_path, "token", token)
        monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(first))
    windows.sddl = PRIVATE_FILE
    windows.lists = {str(middle.parent): DENIED, str(hop.parent): OTHER_FOLDER}
    with pytest.warns(TokenFileShared) as caught:
        assert resolve_token() == token
    message = str(caught[0].message)
    assert (
        f"holds your token, and {hop.parent}, which holds the link {hop}, is owned by another "
        "account, which can change who may add or remove files in it, so other people"
    ) in message
    assert (
        f"Also, it could not be fully checked: {not_shown(middle.parent, folder=True)}."
    ) in message
    assert_token_absent(token, message)


def test_a_list_not_shown_is_read_before_the_token(windows, monkeypatch):
    windows.sddl = PRIVATE_FILE
    windows.folder_sddl = DENIED
    token = synthetic_token()
    path = write_dotenv(f"QTE_TOKEN={token}\n")
    calls: list[str] = []
    real_open, fake_sddl = os.open, _fileaccess._read_sddl

    def recording_sddl(p: str) -> object:
        calls.append(f"list {p}")
        return fake_sddl(p)

    def recording_open(p, *args, **kwargs):
        calls.append(f"open {p}")
        return real_open(p, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(_fileaccess, "_read_sddl", recording_sddl)
    with pytest.warns(TokenFileShared):
        assert resolve_token() == token
    assert calls == [f"list {path}", f"list {path.parent}", f"open {path}"]


# NT SERVICE\TrustedInstaller owns folders of Windows itself, such as C:\, which holds the
# junction C:\Documents and Settings: a folder or link folder it owns is no finding.
# A list like that of C:\ on current Windows: Users may read and list it, Authenticated
# Users may make folders in it (LC), and it is owned by TrustedInstaller.
DRIVE_ROOT = (
    f"O:{TRUSTED_INSTALLER}D:PAI(A;OICI;FA;;;BA)(A;OICI;FA;;;SY)(A;OICIIO;GA;;;CO)"
    "(A;OICI;0x1200a9;;;BU)(A;CIIO;SDGXGWGR;;;AU)(A;;LC;;;AU)"
)


def test_a_folder_trusted_installer_owns_is_no_finding(windows, tmp_path):
    windows.sddl = PRIVATE_FILE
    windows.folder_sddl = DRIVE_ROOT
    path = tmp_path / ".env"
    path.write_text("")
    found = _fileaccess.broad_access(path)
    assert found == access([], [], [], False, folder_owner=False)
    assert not found


def test_a_link_folder_trusted_installer_owns_is_no_finding(windows, tmp_path, monkeypatch):
    link, target = linked(tmp_path, "root", "profile")
    windows.sddl = PRIVATE_FILE
    windows.lists = {str(link.parent): DRIVE_ROOT}
    found = _fileaccess.broad_access(link)
    assert found == BroadAccess(
        folder=(), other_owner=False, link_folder=(), folder_owner=False, link_owner=False
    )
    assert not found
    token = synthetic_token()
    target.write_text(f"QTE_TOKEN={token}\n")
    monkeypatch.chdir(link.parent)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert resolve_token() == token
    assert [w for w in caught if issubclass(w.category, FileShared)] == []
