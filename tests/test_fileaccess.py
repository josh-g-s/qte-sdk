"""Who Windows lets read a token file: the SDDL parser, on canned access lists, and the
warning the SDK gives on (simulated) Windows when a broad group can read the token."""

import logging
import secrets
import warnings
from pathlib import Path

import pytest

from qte_sdk import _fileaccess
from qte_sdk.dotenv import TokenFileShared, shared_message
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


# The parser


@pytest.mark.parametrize(
    ("sddl", "readers"),
    [
        (PROFILE, []),
        (SECOND_DRIVE, [AUTHENTICATED, USERS]),
        # Everyone, by alias and by SID.
        ("D:(A;;FR;;;WD)", [EVERYONE]),
        ("D:(A;;FA;;;S-1-1-0)", [EVERYONE]),
        # Generic rights: GA and GR read, GX does not, and nor does FX.
        ("D:(A;;GA;;;AU)", [AUTHENTICATED]),
        ("D:(A;OICI;GR;;;BU)", [USERS]),
        ("D:(A;;GX;;;WD)", []),
        ("D:(A;;FX;;;WD)(A;;FW;;;BU)", []),
        ("D:(A;;0x10000000;;;WD)", [EVERYONE]),
        ("D:(A;;0x80000000;;;WD)", [EVERYONE]),
        # Hex masks without FILE_READ_DATA: execute, and synchronize alone.
        ("D:(A;;0x1200a0;;;BU)(A;;0x100000;;;AU)", []),
        # Decimal is accepted too.
        ("D:(A;;1;;;BU)", [USERS]),
        # The directory service names for a file's low bits: CC is FILE_READ_DATA.
        ("D:(A;;CCLCRC;;;BU)", [USERS]),
        # WD as a right is WRITE_DAC, not Everyone; AU is a SID here, not an audit entry.
        ("D:(A;;RCWD;;;WD)(A;;FR;;;AU)", [AUTHENTICATED]),
        # Interactive users and Domain Users of any domain.
        ("D:(A;;FR;;;IU)(A;;FR;;;S-1-5-21-1-2-3-513)", [INTERACTIVE, DOMAIN_USERS]),
        ("D:(A;;FR;;;S-1-5-4)(A;;FR;;;DU)", [INTERACTIVE, DOMAIN_USERS]),
        ("D:(A;;FR;;;S-1-5-11)(A;;FR;;;S-1-5-32-545)", [AUTHENTICATED, USERS]),
        # Groups come back once each, in a fixed order.
        ("D:(A;;FR;;;BU)(A;;FA;;;WD)(A;ID;FR;;;BU)", [EVERYONE, USERS]),
        # Deny entries are ignored, so a deny never hides a read an allow gives.
        ("D:(D;;FA;;;WD)(A;;FA;;;BA)", []),
        ("D:(D;;FA;;;BU)(A;;FR;;;BU)", [USERS]),
        # An inherit-only entry applies to the folder's children, not to the object.
        ("D:(A;OICIIO;GA;;;BU)(A;;FA;;;BA)", []),
        ("D:(A;OICI;GA;;;BU)", [USERS]),
        # A conditional entry, whose condition has parentheses and punctuation of its own.
        ('D:(XA;;FR;;;WD;(@User.Title == "PM" && (Member_of {SID(BA)})))', [EVERYONE]),
        # Quoted text in a condition may hold any character, parentheses included.
        ('D:(A;;FR;;;BU)(XA;;FR;;;WD;(@User.Title == "Ops("))', [EVERYONE, USERS]),
        ('D:(XA;;FR;;;AU;(@User.Dept == ":)"))', [AUTHENTICATED]),
        # Empty rights are a mask of 0, which grants nothing.
        ("D:(A;;;;;BA)(A;;FR;;;BU)", [USERS]),
        ("D:(A;;;;;WD)", []),
        # No DACL at all: Windows checks nothing, so Everyone may read.
        ("D:NO_ACCESS_CONTROL", [EVERYONE]),
        ("O:BAG:SY", [EVERYONE]),
        ("", [EVERYONE]),
        # An empty, protected DACL lets no one in.
        ("D:P", []),
        ("D:PAI", []),
        # What a FAT or exFAT drive reports.
        ("D:(A;;FA;;;WD)", [EVERYONE]),
        # Letters in either case.
        ("d:(a;id;fr;;;bu)", [USERS]),
        # A SACL after the DACL is skipped.
        ("D:(A;;FA;;;BA)S:(AU;SA;FA;;;WD)", []),
    ],
)
def test_the_readers_of_an_access_list(sddl: str, readers: list[str]):
    assert _fileaccess.readers_in_sddl(sddl) == readers


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
    assert _fileaccess.readers_in_sddl(sddl) is None


def test_nothing_is_checked_off_windows(monkeypatch, tmp_path):
    def must_not_run(path: str) -> str:
        raise AssertionError("the Windows API was called off Windows")

    monkeypatch.setattr(_fileaccess, "on_windows", lambda: False)
    monkeypatch.setattr(_fileaccess, "_read_sddl", must_not_run)
    assert _fileaccess.broad_readers(tmp_path / "token") is None


def test_an_api_failure_gives_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(_fileaccess, "on_windows", lambda: True)
    # Off Windows the real call fails at once (there is no ctypes.WinDLL), which stands in
    # for any failure of the API.
    assert _fileaccess.broad_readers(tmp_path / "token") is None
    monkeypatch.setattr(_fileaccess, "_read_sddl", lambda path: None)
    assert _fileaccess.broad_readers(tmp_path / "token") is None


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
    assert f"{AUTHENTICATED} and {USERS}" in message
    assert "%USERPROFILE%" in message and "icacls" in message
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


def test_a_shared_dotenv_without_a_token_does_not_warn(windows, monkeypatch):
    write_dotenv("QTE_URL=ws://127.0.0.1:8080/ws\n")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert resolve_url() == "ws://127.0.0.1:8080/ws"


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
    message = shared_message(tmp_path / name / ".env", [USERS])
    assert "`icacls" not in message
    assert f"run icacls on the folder that holds it, {tmp_path / name}." in message


def test_the_icacls_command_quotes_the_folder(tmp_path):
    message = shared_message(tmp_path / "my project" / ".env", [USERS])
    assert f'run `icacls "{tmp_path / "my project"}"`.' in message
