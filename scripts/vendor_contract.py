"""Copy the approved contract .proto files from a local qte-platform checkout.

Maintainers only: qte-platform is private.

    python scripts/vendor_contract.py /path/to/qte-platform           # vendor at the pinned commit
    python scripts/vendor_contract.py /path/to/qte-platform --check   # verify the manifest only

Reads the commit from proto/upstream.toml, copies exactly the approved files byte for byte
from that commit, and rewrites the manifest with each file's git blob hash as
`git ls-tree` prints it. CI cannot see qte-platform, so it checks the vendored bytes
against those hashes instead; `--check` is how a maintainer confirms the hashes really
belong to the pinned commit.
"""

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "proto" / "upstream.toml"
DEST = ROOT / "proto" / "qte" / "contract" / "v1"

# The files approved for publication in this public repo.
ALLOWED = (
    "common.proto",
    "envelope.proto",
    "session.proto",
    "order_entry.proto",
    "order_events.proto",
    "market_data.proto",
)

HEADER = """\
# The one place that records where the vendored contract came from.
# Written by scripts/vendor_contract.py; bump `commit` deliberately and re-run it.
# Each blob is the git blob hash of that file at `commit`, and CI checks the vendored
# bytes against it.
"""


def git(platform: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(platform), *args], capture_output=True)
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {result.stderr.decode().strip()}")
    return result.stdout


def upstream_blobs(platform: Path, commit: str, path: str) -> dict[str, str]:
    blobs = {}
    for line in git(platform, "ls-tree", commit, "--", f"{path}/").decode().splitlines():
        meta, name = line.split("\t", 1)
        _mode, kind, sha = meta.split()
        if kind == "blob":
            blobs[Path(name).name] = sha
    missing = [name for name in ALLOWED if name not in blobs]
    if missing:
        raise SystemExit(f"{commit}:{path} is missing {missing}")
    return {name: blobs[name] for name in ALLOWED}


def write_manifest(repo: str, commit: str, path: str, blobs: dict[str, str]) -> None:
    lines = [HEADER, f'repo = "{repo}"', f'commit = "{commit}"', f'path = "{path}"', "", "[blobs]"]
    lines += [f'"{name}" = "{sha}"' for name, sha in blobs.items()]
    MANIFEST.write_text("\n".join(lines) + "\n")


def main() -> int:
    args = sys.argv[1:]
    check = "--check" in args
    args = [a for a in args if a != "--check"]
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    platform = Path(args[0])
    manifest = tomllib.loads(MANIFEST.read_text())
    commit, path = manifest["commit"], manifest["path"]
    blobs = upstream_blobs(platform, commit, path)

    if check:
        if manifest.get("blobs") != blobs:
            print(f"manifest blobs do not match {commit}", file=sys.stderr)
            return 1
        print(f"manifest matches {commit}")
        return 0

    for existing in DEST.glob("*"):
        if existing.name not in ALLOWED:
            print(f"refusing to continue: unexpected file {existing}", file=sys.stderr)
            return 1
    for name in ALLOWED:
        (DEST / name).write_bytes(git(platform, "show", f"{commit}:{path}/{name}"))
        print(f"vendored {name}")
    write_manifest(manifest["repo"], commit, path, blobs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
