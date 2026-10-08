"""Check that git has every generated contract file, with LF in the index and the tree.

    python scripts/check_contract_eol.py

CI's Windows job runs it before and after regenerating qte_sdk/contract/v1. It reads
`git ls-files --eol qte_sdk/contract/v1` and fails if git fails, if the listing is not
exactly the files scripts/generate_contract.py writes (`__init__.py`, and a `_pb2.py` and a
`_pb2.pyi` for each proto in proto/upstream.toml), or if any of them is not `i/lf` and
`w/lf`. An empty listing fails, so the check cannot pass by looking at nothing. It needs
nothing installed. It prints what is wrong and exits 1, or exits 0.
"""

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = "qte_sdk/contract/v1"


def expected_files(root: Path = ROOT) -> set[str]:
    """The paths scripts/generate_contract.py writes, from the protos the manifest pins."""
    blobs = tomllib.loads((root / "proto" / "upstream.toml").read_text())["blobs"]
    stems = [name.removesuffix(".proto") for name in blobs]
    return {f"{OUT}/__init__.py"} | {
        f"{OUT}/{s}_pb2{ext}" for s in stems for ext in (".py", ".pyi")
    }


def problems(listing: str, expected: set[str]) -> list[str]:
    """What is wrong with a `git ls-files --eol` listing of the generated code, if anything."""
    if not expected:
        return ["no generated files are expected: proto/upstream.toml lists no protos"]
    found = []
    seen = set()
    for line in listing.splitlines():
        info, sep, path = line.partition("\t")
        fields = info.split()
        if not sep or len(fields) < 2:
            found.append(f"cannot read the git ls-files line {line!r}")
            continue
        seen.add(path)
        if fields[0] != "i/lf" or fields[1] != "w/lf":
            found.append(f"{path} is {fields[0]} {fields[1]}, not i/lf w/lf")
    if not seen:
        found.append(f"git ls-files lists nothing in {OUT}")
    for path in sorted(expected - seen):
        found.append(f"{path} is generated but git does not list it")
    for path in sorted(seen - expected):
        found.append(f"{path} is listed but scripts/generate_contract.py does not write it")
    return found


def main() -> int:
    result = subprocess.run(
        ["git", "ls-files", "--eol", OUT], cwd=ROOT, capture_output=True, text=True
    )
    print(result.stdout, end="")
    if result.returncode != 0:
        print(f"error: git ls-files failed: {result.stderr.strip()}", file=sys.stderr)
        return 1
    expected = expected_files()
    found = problems(result.stdout, expected)
    for problem in found:
        print(f"error: {problem}", file=sys.stderr)
    if found:
        return 1
    print(f"all {len(expected)} generated contract files are in git with LF")
    return 0


if __name__ == "__main__":
    sys.exit(main())
