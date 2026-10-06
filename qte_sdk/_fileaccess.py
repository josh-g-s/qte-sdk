"""Which broad groups of users Windows lets read or change a file.

On Windows a file's access is set by its access list (its DACL), not by POSIX modes, and a
file inherits the list of its folder. A folder under your user profile is private by
default, but one on another drive, such as `D:\\`, usually lets Users read everything in it
and Authenticated Users change it too. `broad_access` reads a file's list through the
Windows API (with `ctypes`, so no extra package is needed) and names the broad groups it
lets read the file and those it lets change it. `access_in_sddl` does the parsing, on the
list in its text form (SDDL), and runs on any system, so it can be tested anywhere.

Only paths and access lists are handled here, never a file's contents.
"""

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

__all__ = ["BROAD_GROUPS", "BroadAccess", "access_in_sddl", "broad_access", "on_windows"]

# The broad groups, by the names `icacls` shows. Each is matched by its SDDL alias or its
# SID; Domain Users is matched by its well-known last part (513), in any domain. On a
# computer outside a domain, the same part names the local group None, which also holds
# every local user.
EVERYONE = "Everyone"
BROAD_GROUPS = (
    EVERYONE,
    "NT AUTHORITY\\Authenticated Users",
    "BUILTIN\\Users",
    "NT AUTHORITY\\INTERACTIVE",
    "Domain Users",
)
_GROUP_OF_SID = {
    "WD": EVERYONE,
    "S-1-1-0": EVERYONE,
    "AU": BROAD_GROUPS[1],
    "S-1-5-11": BROAD_GROUPS[1],
    "BU": BROAD_GROUPS[2],
    "S-1-5-32-545": BROAD_GROUPS[2],
    "IU": BROAD_GROUPS[3],
    "S-1-5-4": BROAD_GROUPS[3],
    "DU": BROAD_GROUPS[4],
}
_DOMAIN_USERS_RID = "-513"

# The rights that let a user read a file's data: FILE_READ_DATA, GENERIC_READ and
# GENERIC_ALL. GENERIC_EXECUTE (GX) and FILE_GENERIC_EXECUTE (FX) do not.
_READ_MASK = 0x1 | 0x80000000 | 0x10000000
# The rights that let a user change a file, or take control of it and then change it:
# FILE_WRITE_DATA, FILE_APPEND_DATA, GENERIC_WRITE, GENERIC_ALL, WRITE_DAC and WRITE_OWNER.
_WRITE_MASK = 0x2 | 0x4 | 0x40000000 | 0x10000000 | 0x40000 | 0x80000
# The two-letter access rights SDDL writes, as masks. A file's list may use the directory
# service names for the low bits: CC is 0x1, which for a file is FILE_READ_DATA.
_RIGHTS = {
    "GA": 0x10000000,
    "GR": 0x80000000,
    "GW": 0x40000000,
    "GX": 0x20000000,
    "RC": 0x00020000,
    "SD": 0x00010000,
    "WD": 0x00040000,
    "WO": 0x00080000,
    "RP": 0x00000010,
    "WP": 0x00000020,
    "CC": 0x00000001,
    "DC": 0x00000002,
    "LC": 0x00000004,
    "SW": 0x00000008,
    "LO": 0x00000080,
    "DT": 0x00000040,
    "CR": 0x00000100,
    "FA": 0x001F01FF,
    "FR": 0x00120089,
    "FW": 0x00120116,
    "FX": 0x001200A0,
    "KA": 0x000F003F,
    "KR": 0x00020019,
    "KW": 0x00020006,
    "KX": 0x00020019,
    "NR": 0x00000002,
    "NW": 0x00000001,
    "NX": 0x00000004,
}
# The ACE types that allow access: plain, and conditional (which may allow it).
_ALLOW_TYPES = frozenset({"A", "XA"})
_DACL_FLAGS = ("NO_ACCESS_CONTROL", "AI", "AR", "P")


@dataclass(frozen=True)
class BroadAccess:
    """The broad groups an access list lets read a file, and those it lets change it (or
    take control of it), each in the order of `BROAD_GROUPS`. False when both are empty."""

    read: tuple[str, ...] = ()
    write: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.read or self.write)


_NO_DACL = BroadAccess(read=(EVERYONE,), write=(EVERYONE,))


def on_windows() -> bool:
    """Whether this is Windows, where access lists, not modes, say who can open a file."""
    return os.name == "nt"


def broad_access(path: Path | str) -> BroadAccess | None:
    """The broad groups (see `BROAD_GROUPS`) that Windows lets read or change `path`, or
    None if that cannot be told, for example when this is not Windows, the file does not
    exist or the access list cannot be read. Never raises."""
    if not on_windows():
        return None
    try:
        sddl = _read_sddl(str(path))
    except Exception:
        return None
    if sddl is None:
        return None
    return access_in_sddl(sddl)


def access_in_sddl(sddl: str) -> BroadAccess | None:
    """The broad groups that the access list in `sddl` lets read, and change, the object it
    belongs to, or None if `sddl` cannot be parsed.

    An allow entry counts when it applies to the object itself (it is not inherit-only)
    and grants a broad group FILE_READ_DATA, GENERIC_READ or GENERIC_ALL (read), or
    FILE_WRITE_DATA, FILE_APPEND_DATA, GENERIC_WRITE, GENERIC_ALL, WRITE_DAC or WRITE_OWNER
    (change). Deny entries are ignored, which can only add a warning, never hide one:
    Windows applies them first, so a deny could take away what an allow here gives. A
    string with no DACL (no `D:` part, or `D:NO_ACCESS_CONTROL`) means Windows checks
    nothing, so Everyone may read and change the object.
    """
    sections = _sections(sddl)
    if sections is None:
        return None
    dacl = sections.get("D")
    if dacl is None:
        return _NO_DACL
    flags, aces = _split_dacl(dacl)
    if flags is None or aces is None:
        return None
    if "NO_ACCESS_CONTROL" in flags:
        return _NO_DACL
    read: set[str] = set()
    write: set[str] = set()
    for ace in aces:
        fields = ace.split(";", 6)
        if len(fields) < 6:
            return None
        kind, ace_flags, rights, _, _, sid = (f.strip().upper() for f in fields[:6])
        mask = _mask(rights)
        if mask is None or not _is_flags(ace_flags):
            return None
        if kind not in _ALLOW_TYPES or "IO" in _pairs(ace_flags):
            continue
        group = _group(sid)
        if group is None:
            continue
        if mask & _READ_MASK:
            read.add(group)
        if mask & _WRITE_MASK:
            write.add(group)
    return BroadAccess(
        read=tuple(g for g in BROAD_GROUPS if g in read),
        write=tuple(g for g in BROAD_GROUPS if g in write),
    )


def _sections(sddl: str) -> dict[str, str] | None:
    """The parts of `sddl` by their letter (O, G, D or S), or None if it is malformed. A
    part starts at a `:` outside parentheses; the letter before it names the part."""
    sections: dict[str, str] = {}
    depth = 0
    letter = None
    start = 0
    text = sddl.strip()
    for index, char in _structure(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return None
        elif char == ":" and depth == 0:
            if index == 0:
                return None
            if letter is not None:
                sections[letter] = text[start : index - 1]
            elif index != 1:
                return None  # text before the first part
            letter = text[index - 1].upper()
            if letter not in "OGDS" or letter in sections:
                return None
            start = index + 1
    if depth != 0:
        return None
    if letter is not None:
        sections[letter] = text[start:]
    elif text:
        return None
    return sections


def _split_dacl(dacl: str) -> tuple[str | None, list[str] | None]:
    """The flags before a DACL's first entry, and the text of each entry; or None, None if
    it is malformed."""
    opening = dacl.find("(")
    flags = dacl if opening == -1 else dacl[:opening]
    rest = flags.upper()
    while rest:
        for flag in _DACL_FLAGS:
            if rest.startswith(flag):
                rest = rest[len(flag) :]
                break
        else:
            return None, None
    aces: list[str] = []
    depth = 0
    start = 0
    for index, char in _structure(dacl, len(flags)):
        if char == "(":
            if depth == 0:
                start = index + 1
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                aces.append(dacl[start:index])
        elif depth == 0 and not char.isspace():
            return None, None  # text between entries
    if depth != 0:
        return None, None
    return flags.upper(), aces


def _structure(text: str, start: int = 0) -> Iterator[tuple[int, str]]:
    """Each character of `text` from `start`, with its index, except those inside a quoted
    string in an entry (a conditional entry's condition may quote any character)."""
    depth = 0
    quoted = False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            quoted = char != '"'
            continue
        if char == '"' and depth > 0:
            quoted = True
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        yield index, char
    if quoted:
        yield len(text), "("  # an unclosed quote leaves the text unbalanced, so malformed


def _mask(rights: str) -> int | None:
    """The access mask that `rights` writes, as hex, decimal or two-letter codes (none at
    all is a mask of 0), or None if it cannot be read."""
    try:
        if rights.startswith("0X"):
            return int(rights[2:], 16)
        if rights.isdigit():
            return int(rights)
    except ValueError:
        return None
    if len(rights) % 2:
        return None
    mask = 0
    for code in _pairs(rights):
        if code not in _RIGHTS:
            return None
        mask |= _RIGHTS[code]
    return mask


_ACE_FLAGS = frozenset({"CI", "OI", "NP", "IO", "ID", "SA", "FA", "TP", "CR"})


def _is_flags(flags: str) -> bool:
    return len(flags) % 2 == 0 and all(pair in _ACE_FLAGS for pair in _pairs(flags))


def _pairs(text: str) -> list[str]:
    return [text[i : i + 2] for i in range(0, len(text), 2)]


def _group(sid: str) -> str | None:
    """The broad group `sid` names, by alias or SID string, or None."""
    if sid in _GROUP_OF_SID:
        return _GROUP_OF_SID[sid]
    if sid.startswith("S-1-5-21-") and sid.endswith(_DOMAIN_USERS_RID):
        return BROAD_GROUPS[4]
    return None


def _read_sddl(path: str) -> str | None:
    """The DACL of `path` as an SDDL string, read with the Windows API, or None if it
    cannot be read. Only on Windows: `ctypes.WinDLL` exists nowhere else."""
    import ctypes
    from ctypes import wintypes

    se_file_object = 1
    dacl_security_information = 0x4
    sddl_revision_1 = 1

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    get_info = advapi32.GetNamedSecurityInfoW
    get_info.argtypes = [
        wintypes.LPCWSTR,  # pObjectName
        ctypes.c_int,  # ObjectType (SE_OBJECT_TYPE)
        wintypes.DWORD,  # SecurityInfo
        ctypes.POINTER(ctypes.c_void_p),  # ppsidOwner
        ctypes.POINTER(ctypes.c_void_p),  # ppsidGroup
        ctypes.POINTER(ctypes.c_void_p),  # ppDacl
        ctypes.POINTER(ctypes.c_void_p),  # ppSacl
        ctypes.POINTER(ctypes.c_void_p),  # ppSecurityDescriptor
    ]
    get_info.restype = wintypes.DWORD
    to_string = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
    to_string.argtypes = [
        ctypes.c_void_p,  # SecurityDescriptor
        wintypes.DWORD,  # RequestedStringSDRevision
        wintypes.DWORD,  # SecurityInformation
        ctypes.POINTER(ctypes.c_void_p),  # StringSecurityDescriptor (LPWSTR *)
        ctypes.POINTER(wintypes.ULONG),  # StringSecurityDescriptorLen
    ]
    to_string.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    descriptor = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    status = get_info(
        path,
        se_file_object,
        dacl_security_information,
        None,
        None,
        ctypes.byref(dacl),  # points into the descriptor, so it is not freed itself
        None,
        ctypes.byref(descriptor),
    )
    if status != 0:
        return None
    text = ctypes.c_void_p()
    try:
        length = wintypes.ULONG()
        if not to_string(
            descriptor,
            sddl_revision_1,
            dacl_security_information,
            ctypes.byref(text),
            ctypes.byref(length),
        ):
            return None
        return ctypes.wstring_at(text.value) if text.value else None
    finally:
        if text.value:
            local_free(text)
        if descriptor.value:
            local_free(descriptor)
