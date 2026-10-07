"""Which broad groups of users Windows lets read, change or replace a file.

On Windows a file's access is set by its access list (its DACL), not by POSIX modes, and a
file inherits the list of its folder. A folder under your user profile is private by
default, but one on another drive, such as `D:\\`, usually lets Users read everything in it
and Authenticated Users change it too. `broad_access` reads a file's list through the
Windows API (with `ctypes`, so no extra package is needed) and names the broad groups it
lets read the file and those it lets change it. It also reads the list of the file's
folder, since whoever may add or remove files there can replace the file with one of their
own, and the file's owner, since an owner can always change the file's list. When the file
is reached through links (symbolic links or junctions), at the file itself, at a folder on
its path, or in what a link points to, the file and the folder looked at are those it
resolves to, and the folder that holds each link on the way is looked at too, since
whoever may replace a link may point it elsewhere.
`access_in_sddl`, `folder_access_in_sddl` and `owner_is_other` do the parsing, on the lists
in their text form (SDDL), and run on any system, so they can be tested anywhere.

Only paths and access lists are handled here, never a file's contents.
"""

import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path

__all__ = [
    "BROAD_GROUPS",
    "MAX_LINKS",
    "BroadAccess",
    "LinkFolder",
    "access_in_sddl",
    "broad_access",
    "folder_access_in_sddl",
    "links_on_the_way",
    "on_windows",
    "owner_is_other",
]

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
# The rights on a folder that let a user add a file to it or remove one from it, or take
# control of the folder and then do so: FILE_ADD_FILE (the bit FILE_WRITE_DATA has on a
# file), FILE_DELETE_CHILD, DELETE (of the folder itself, which lets it be renamed and
# another put in its place), GENERIC_WRITE, GENERIC_ALL, WRITE_DAC and WRITE_OWNER.
# FILE_LIST_DIRECTORY (0x1, CC) and FILE_ADD_SUBDIRECTORY (0x4, LC) do not.
_FOLDER_MASK = 0x2 | 0x40 | 0x10000 | 0x40000000 | 0x10000000 | 0x40000 | 0x80000
# The two-letter access rights SDDL writes, as masks, from Microsoft's table of ACE access
# rights. A file's or folder's list may use the directory service names for the low bits:
# CC is 0x1 (FILE_READ_DATA, or FILE_LIST_DIRECTORY on a folder), DC is 0x2
# (FILE_WRITE_DATA, or FILE_ADD_FILE), LC is 0x4 (FILE_APPEND_DATA, or
# FILE_ADD_SUBDIRECTORY) and DT is 0x40 (FILE_DELETE_CHILD on a folder).
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
# The owners that need no warning, besides the current user: BUILTIN\Administrators, who
# may take any file anyway (and own the files an elevated administrator creates), and
# SYSTEM.
_TRUSTED_OWNERS = frozenset({"BA", "S-1-5-32-544", "SY", "S-1-5-18"})
# The aliases SDDL may give an owner that a process can run as, by SID: LOCAL SERVICE and
# NETWORK SERVICE. The local Administrator and Guest accounts (LA and LG) stand for a SID
# in this computer's domain, which is not known here, so they cannot be compared.
_SERVICE_OWNERS = {"LS": "S-1-5-19", "NS": "S-1-5-20"}
# The other SID aliases Microsoft documents for SDDL (its "SID Strings" list), each a group
# or a well-known identity, never the current user. An owner given by LA, LG or an alias in
# none of these lists cannot be told, so it is unknown.
_GROUP_OWNERS = frozenset(
    "AA AC AN AO AP AS AU BG BO BU CA CD CG CN CO CY DA DC DD DG DU EA ED EK ER ES HA HI IS "
    "IU KA LU LW ME MP MS MU NO NU OW PA PO PS PU RA RC RD RE RM RO RS RU SA SI SO SS SU UD "
    "WD WR".split()
)
_SID = re.compile(r"S-1-\d+(-\d+)+")


@dataclass(frozen=True)
class LinkFolder:
    """A folder that holds a link on the way to a file: its path, the links in it, as they
    were met, and the broad groups that may add or remove files in it, or None if its list
    could not be read."""

    path: str
    links: tuple[str, ...]
    groups: tuple[str, ...] | None


@dataclass(frozen=True)
class BroadAccess:
    """The broad groups an access list lets read a file, and those it lets change it (or
    take control of it), each in the order of `BROAD_GROUPS`; the broad groups that may add
    or remove files in its folder, and, when the file is reached through links, in the
    folders that hold them; and whether another account owns it.

    `folder` is None when the folder's list was not read, and `other_owner` None when the
    owner was not read or could not be compared with the current user. `link_folder` is
    None when no link leads to the file from another folder, and also when no group was
    found in those folders but a folder's list, or the links themselves, could not be read
    (see `link_folders`). False when no broad group may do any of these and no other
    account is known to own the file.

    `file`, `folder_path`, `links` and `link_folders` say where those lists were read: the
    file the path resolves to, its folder, every link met on the way, in order, and each
    folder that holds one of them, other than the file's own folder (see `LinkFolder`).
    `link_folders` is None when the links could not be followed: a loop, more than
    `MAX_LINKS` of them, or a link that could not be read. These are not compared: two
    results are equal when they find the same. When `file` or `folder_path` is None, the
    path the check was asked about, or its folder, stands for it."""

    read: tuple[str, ...] = ()
    write: tuple[str, ...] = ()
    folder: tuple[str, ...] | None = None
    other_owner: bool | None = None
    link_folder: tuple[str, ...] | None = None
    file: str | None = field(default=None, compare=False)
    folder_path: str | None = field(default=None, compare=False)
    links: tuple[str, ...] = field(default=(), compare=False)
    link_folders: tuple[LinkFolder, ...] | None = field(default=(), compare=False)

    def __bool__(self) -> bool:
        return bool(self.read or self.write or self.changeable)

    @property
    def changeable(self) -> bool:
        """Whether a broad group may change the file or replace it, or a link to it, with
        one of their own, or another account owns it (and so may change it)."""
        return bool(self.write or self.folder or self.link_folder or self.other_owner)


_ALL_RIGHTS = 0xFFFFFFFF


def on_windows() -> bool:
    """Whether this is Windows, where access lists, not modes, say who can open a file."""
    return os.name == "nt"


def broad_access(path: Path | str) -> BroadAccess | None:
    """The broad groups (see `BROAD_GROUPS`) that Windows lets read or change `path`, and
    add or remove files in the folder that holds it, and whether another account owns it;
    or None if the file's access list cannot be told, for example when this is not Windows,
    the file does not exist or the list cannot be read. If only a folder's list or the
    owner cannot be told, that part is None (see `BroadAccess`). Never raises.

    When `path` is reached through links, the file, its folder and its owner are those of
    the file it resolves to, and `link_folder` gives the groups that may add or remove
    files in the folders that hold the links. Every link is followed and every list read
    here, before the caller opens the file, since following a link is itself a call to the
    Windows API."""
    if not on_windows():
        return None
    try:
        file = os.path.realpath(path)
        folder_path = os.path.dirname(file)
        walked = links_on_the_way(path)
        # Microsoft does not document whether GetNamedSecurityInfoW follows a symbolic link,
        # and documents that GetFileSecurity reads the link itself, so the list of the file
        # the path resolves to is asked for by that file's own path.
        sddl = _read_sddl(file)
    except Exception:
        return None
    if sddl is None:
        return None
    access = access_in_sddl(sddl)
    if access is None:
        return None
    folder = _folder_access(folder_path)
    links: tuple[str, ...] = ()
    link_folders: tuple[LinkFolder, ...] | None = None
    if walked is not None:
        links = tuple(link for link, _ in walked)
        try:
            link_folders = _link_folders(walked, folder_path)
        except Exception:
            walked, links = None, ()  # the folders could not be named: unknown
    try:
        user = _current_user_sid()
    except Exception:
        user = None
    return replace(
        access,
        folder=folder,
        other_owner=owner_is_other(sddl, user),
        link_folder=_link_finding(walked, link_folders),
        file=file,
        folder_path=folder_path,
        links=links,
        link_folders=link_folders,
    )


# The most links followed on the way to a file, as an operating system bounds them; more
# gives an unknown result rather than a long walk.
MAX_LINKS = 40


def links_on_the_way(path: Path | str) -> list[tuple[str, str]] | None:
    """Each link met while `path` is resolved, in order, with the folder that holds it:
    links at the file and at folders on its path, and links in what each link points to.
    A link's folder is given resolved through every link before it. None if they cannot be
    followed: a loop, more than `MAX_LINKS` links, a name that cannot be looked at, or a
    link whose target cannot be read or is not an ordinary path (such as a volume's GUID
    name), so no link on the way is left out unsaid.

    The path is resolved one name at a time, from its root. A link's target is joined to
    the link's folder, so a relative one is taken from there, and `..` in it is applied to
    that already resolved folder, as Windows does."""
    root, pending = _split(os.path.abspath(path))
    current = root
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    while pending:
        name = pending.pop(0)
        candidate = os.path.join(current, name)
        link = _is_link(candidate)
        if link is None:
            return None
        if not link:
            current = candidate
            continue
        # The same link with the same names left to resolve is a loop; the same link met
        # again with fewer names left, through a junction to a folder above it, is not.
        state = (os.path.normcase(candidate), tuple(map(os.path.normcase, pending)))
        if state in seen or len(found) >= MAX_LINKS:
            return None
        seen.add(state)
        try:
            target = _without_prefix(os.readlink(candidate))
        except (OSError, ValueError):
            return None
        if target is None:
            return None
        found.append((candidate, current))
        root, names = _split(os.path.normpath(os.path.join(current, target)))
        current, pending = root, names + pending
    return found


def _split(path: str) -> tuple[str, list[str]]:
    """The root of an absolute, normalized `path` (such as `C:\\`, a share's
    `\\\\server\\share\\` or `/`), and the names after it."""
    drive, rest = os.path.splitdrive(path)
    return drive + os.sep, [name for name in rest.split(os.sep) if name]


def _without_prefix(target: str) -> str | None:
    """A link's target without the `\\\\?\\` or `\\??\\` that Windows puts before the
    target of a junction, so it is an ordinary path; or None if what follows is not a
    drive's path or a share's, such as a volume's GUID name, which is not followed."""
    for prefix in ("\\\\?\\", "\\??\\"):
        if target.startswith(prefix):
            rest = target[len(prefix) :]
            if rest[:4].upper() == "UNC\\":
                return "\\\\" + rest[4:]
            if _DRIVE_PATH.fullmatch(rest[:3]) or _DRIVE_PATH.fullmatch(rest):
                return rest
            return None
    return target


# A drive's name at the start of a path: `C:` or `C:\\`.
_DRIVE_PATH = re.compile(r"[A-Za-z]:\\?")


def _link_folders(walked: list[tuple[str, str]], folder: str) -> tuple[LinkFolder, ...]:
    """Each folder that holds a link in `walked`, by its resolved name, once, in the order
    first met, with its links and the broad groups that may add or remove files in it;
    leaving out `folder`, the file's own folder, which is checked anyway. Another name for
    the same folder, such as a mapped drive's or a short 8.3 name, counts as the same."""
    holders: dict[str, tuple[str, list[str]]] = {}
    for link, holder in walked:
        real = os.path.realpath(holder)
        if _same(real, folder):
            continue
        holders.setdefault(os.path.normcase(os.path.normpath(real)), (real, []))[1].append(link)
    return tuple(
        LinkFolder(real, tuple(links), _folder_access(real)) for real, links in holders.values()
    )


def _link_finding(
    walked: list[tuple[str, str]] | None, link_folders: tuple[LinkFolder, ...] | None
) -> tuple[str, ...] | None:
    """The broad groups that may add or remove files in any of `link_folders`, in the order
    of `BROAD_GROUPS`; or None if there is none to look at, or none was found but a
    folder's list, or the links themselves (`walked` is None), could not be read."""
    if link_folders is None or not link_folders:
        return None
    found = {group for f in link_folders for group in (f.groups or ())}
    if found:
        return tuple(group for group in BROAD_GROUPS if group in found)
    if walked is None or any(f.groups is None for f in link_folders):
        return None
    return ()


def _is_link(path: str) -> bool | None:
    """Whether `path` is a symbolic link or a junction (a mount point, to Windows), or None
    if it cannot be looked at. A name that does not exist is not a link. Other reparse
    points, such as the placeholders of files kept in the cloud, are not links."""
    try:
        status = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except (OSError, ValueError):
        return None
    tag = getattr(status, "st_reparse_tag", 0)  # only on Windows
    return stat.S_ISLNK(status.st_mode) or tag in _LINK_TAGS


# IO_REPARSE_TAG_SYMLINK and IO_REPARSE_TAG_MOUNT_POINT (which a junction is), from
# Microsoft's list of reparse tags. The stat module names them only on Windows.
_LINK_TAGS = (0xA000000C, 0xA0000003)


def _same(one: str, other: str) -> bool:
    """Whether two paths name the same place, as Windows compares names."""
    return os.path.normcase(os.path.normpath(one)) == os.path.normcase(os.path.normpath(other))


def _folder_access(folder: str) -> tuple[str, ...] | None:
    """The broad groups that may add or remove files in `folder` (see
    `folder_access_in_sddl`), or None if its list cannot be read or parsed."""
    try:
        folder_sddl = _read_sddl(folder)
    except Exception:
        return None
    return None if folder_sddl is None else folder_access_in_sddl(folder_sddl)


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
    grants = _grants(sddl)
    if grants is None:
        return None
    return BroadAccess(read=_holding(grants, _READ_MASK), write=_holding(grants, _WRITE_MASK))


def folder_access_in_sddl(sddl: str) -> tuple[str, ...] | None:
    """The broad groups that the access list in `sddl`, a folder's, lets add files to the
    folder or remove files from it, in the order of `BROAD_GROUPS`; or None if `sddl`
    cannot be parsed.

    An allow entry counts when it applies to the folder itself (it is not inherit-only:
    such an entry is for the files in the folder, which their own lists show) and grants a
    broad group FILE_ADD_FILE, FILE_DELETE_CHILD, DELETE, GENERIC_WRITE, GENERIC_ALL,
    WRITE_DAC or WRITE_OWNER. Deny entries are ignored, and a missing DACL lets Everyone do
    anything, as for `access_in_sddl`.
    """
    grants = _grants(sddl)
    if grants is None:
        return None
    return _holding(grants, _FOLDER_MASK)


def owner_is_other(sddl: str, user: str | None) -> bool | None:
    """Whether the owner in `sddl` (its `O:` part) is an account other than `user` (the
    current user's SID), BUILTIN\\Administrators or SYSTEM; or None if that cannot be told,
    because `sddl` has no owner or cannot be parsed, or `user` is None and is needed.

    SDDL gives a well-known owner by its alias, such as `BA`, and any other by its SID. An
    alias other than those of an account a process may run as names a group or a
    well-known identity, so it is never the current user; an alias Microsoft does not
    document gives None."""
    sections = _sections(sddl)
    if sections is None or "O" not in sections:
        return None
    owner = sections["O"].strip().upper()
    if owner in _TRUSTED_OWNERS:
        return False
    if _SID.fullmatch(owner) is None:
        if owner in _GROUP_OWNERS:
            return True
        if owner not in _SERVICE_OWNERS:
            return None  # LA, LG, or not an alias at all
        owner = _SERVICE_OWNERS[owner]
    if user is None:
        return None
    return owner != user.strip().upper()


def _grants(sddl: str) -> dict[str, int] | None:
    """The rights the DACL in `sddl` allows each broad group on the object itself, as one
    mask per group, or None if `sddl` cannot be parsed. With no DACL, Everyone has every
    right."""
    sections = _sections(sddl)
    if sections is None:
        return None
    dacl = sections.get("D")
    if dacl is None:
        return {EVERYONE: _ALL_RIGHTS}
    flags, aces = _split_dacl(dacl)
    if flags is None or aces is None:
        return None
    if "NO_ACCESS_CONTROL" in flags:
        return {EVERYONE: _ALL_RIGHTS}
    grants: dict[str, int] = {}
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
        if group is not None:
            grants[group] = grants.get(group, 0) | mask
    return grants


def _holding(grants: dict[str, int], rights: int) -> tuple[str, ...]:
    """The groups in `grants` that have any of `rights`, in the order of `BROAD_GROUPS`."""
    return tuple(g for g in BROAD_GROUPS if grants.get(g, 0) & rights)


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
    """The owner and DACL of `path`, a file or folder, as an SDDL string read with the
    Windows API, or None if they cannot be read. Only on Windows: `ctypes.WinDLL` exists
    nowhere else."""
    import ctypes
    from ctypes import wintypes

    se_file_object = 1
    owner_and_dacl = 0x1 | 0x4  # OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
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
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    status = get_info(
        path,
        se_file_object,
        owner_and_dacl,
        ctypes.byref(owner),  # these two point into the descriptor, so are not freed
        None,
        ctypes.byref(dacl),
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
            owner_and_dacl,
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


def _current_user_sid() -> str | None:
    """The SID of the user this process runs as, such as `S-1-5-21-...-1001`, read with
    the Windows API, or None if it cannot be read. Only on Windows."""
    import ctypes
    from ctypes import wintypes

    token_query = 0x8
    token_user = 1  # TOKEN_INFORMATION_CLASS TokenUser

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    current_process = kernel32.GetCurrentProcess
    current_process.argtypes = []
    current_process.restype = wintypes.HANDLE
    open_token = advapi32.OpenProcessToken
    open_token.argtypes = [
        wintypes.HANDLE,  # ProcessHandle
        wintypes.DWORD,  # DesiredAccess
        ctypes.POINTER(wintypes.HANDLE),  # TokenHandle
    ]
    open_token.restype = wintypes.BOOL
    token_info = advapi32.GetTokenInformation
    token_info.argtypes = [
        wintypes.HANDLE,  # TokenHandle
        ctypes.c_int,  # TokenInformationClass
        ctypes.c_void_p,  # TokenInformation
        wintypes.DWORD,  # TokenInformationLength
        ctypes.POINTER(wintypes.DWORD),  # ReturnLength
    ]
    token_info.restype = wintypes.BOOL
    sid_to_string = advapi32.ConvertSidToStringSidW
    sid_to_string.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]  # PSID, LPWSTR *
    sid_to_string.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    token = wintypes.HANDLE()
    # The process's pseudo handle needs no closing.
    if not open_token(current_process(), token_query, ctypes.byref(token)):
        return None
    text = ctypes.c_void_p()
    try:
        needed = wintypes.DWORD()
        token_info(token, token_user, None, 0, ctypes.byref(needed))  # asks only the size
        if not needed.value:
            return None
        # A buffer of pointers, so the TOKEN_USER in it is aligned; Python frees it.
        size = (needed.value + ctypes.sizeof(ctypes.c_void_p) - 1) // ctypes.sizeof(ctypes.c_void_p)
        buffer = (ctypes.c_void_p * size)()
        if not token_info(token, token_user, buffer, ctypes.sizeof(buffer), ctypes.byref(needed)):
            return None
        # TOKEN_USER starts with SID_AND_ATTRIBUTES, which starts with the PSID.
        sid = buffer[0]
        if not sid or not sid_to_string(sid, ctypes.byref(text)):
            return None
        return ctypes.wstring_at(text.value) if text.value else None
    finally:
        if text.value:
            local_free(text)
        close_handle(token)
