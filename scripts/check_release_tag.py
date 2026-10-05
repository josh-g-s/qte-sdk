"""Check a release tag before it is pushed, and again in CI once it is.

    python scripts/check_release_tag.py v1.0.1

It checks that the tag has the form vX.Y.Z, that X.Y.Z is `__version__` in
qte_sdk/__init__.py, and that CHANGELOG.md has a `## X.Y.Z` entry. It reads both files as
text, so it needs nothing installed. It prints what is wrong and exits 1, or exits 0.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# As qte_sdk/update.py reads a release: no leading zero, at most nine digits.
NUMBER = r"(?:0|[1-9][0-9]{0,8})"
RELEASE_TAG = re.compile(rf"v({NUMBER}\.{NUMBER}\.{NUMBER})")
VERSION_LINE = re.compile(r'^__version__ = "([^"]*)"$', re.MULTILINE)


def problems(tag: str, root: Path = ROOT) -> list[str]:
    """What keeps `tag` from being a release of the source in `root`, if anything."""
    match = RELEASE_TAG.fullmatch(tag)
    if match is None:
        return [f"{tag!r} is not a release tag: it must be vX.Y.Z, such as v1.0.1"]
    number = match[1]
    found = []
    versions = VERSION_LINE.findall((root / "qte_sdk" / "__init__.py").read_text())
    if len(versions) != 1:
        found.append("qte_sdk/__init__.py does not set __version__ exactly once")
    elif versions[0] != number:
        found.append(f"the tag is {tag} but __version__ is {versions[0]!r}")
    changelog = root / "CHANGELOG.md"
    lines = changelog.read_text().splitlines() if changelog.is_file() else []
    if f"## {number}" not in lines:
        found.append(f"CHANGELOG.md has no '## {number}' entry")
    return found


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python scripts/check_release_tag.py vX.Y.Z", file=sys.stderr)
        return 2
    found = problems(args[0])
    for problem in found:
        print(f"error: {problem}", file=sys.stderr)
    if found:
        return 1
    print(f"{args[0]} matches __version__ and has a CHANGELOG.md entry")
    return 0


if __name__ == "__main__":
    sys.exit(main())
