"""Copy the approved files from a local checkout of the exchange's contract repository.

Maintainers only: the exchange's contract repository is not public.

    python scripts/vendor_contract.py /path/to/contract-repository           # vendor at the pins
    python scripts/vendor_contract.py /path/to/contract-repository --check   # verify the manifests

Two sets of files are vendored, each with its own manifest and pinned commit: the contract
.proto files (proto/upstream.toml) and the published conformance steps
(conformance/upstream.toml). For each set, reads the commit from its manifest, copies exactly
the approved files byte for byte from that commit, and rewrites the manifest with each
file's git blob hash as `git ls-tree` prints it. CI cannot see that repository, so it checks
the vendored bytes against those hashes instead; `--check` is how a maintainer confirms the
hashes really belong to the pinned commits.
"""

import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Target:
    """One set of vendored files: its manifest, where the files go, the files approved for
    publication in this public repo, and what the manifest's header calls them."""

    manifest: Path
    dest: Path
    allowed: tuple[str, ...]
    what: str


TARGETS = (
    Target(
        manifest=ROOT / "proto" / "upstream.toml",
        dest=ROOT / "proto" / "qte" / "contract" / "v1",
        allowed=(
            "common.proto",
            "envelope.proto",
            "session.proto",
            "order_entry.proto",
            "order_events.proto",
            "market_data.proto",
        ),
        what="the vendored contract",
    ),
    Target(
        manifest=ROOT / "conformance" / "upstream.toml",
        dest=ROOT / "conformance",
        allowed=("CONFORMANCE.md",),
        what="the vendored conformance steps",
    ),
)


def header(target: Target) -> str:
    return f"""\
# The one place that records where {target.what} came from.
# Written by scripts/vendor_contract.py; bump `commit` deliberately and re-run it.
# Each blob is the git blob hash of that file at `commit`, and CI checks the vendored
# bytes against it.
"""


def git(platform: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(platform), *args], capture_output=True)
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {result.stderr.decode().strip()}")
    return result.stdout


def upstream_blobs(
    platform: Path, commit: str, path: str, allowed: tuple[str, ...]
) -> dict[str, str]:
    blobs = {}
    for line in git(platform, "ls-tree", commit, "--", f"{path}/").decode().splitlines():
        meta, name = line.split("\t", 1)
        _mode, kind, sha = meta.split()
        if kind == "blob":
            blobs[Path(name).name] = sha
    missing = [name for name in allowed if name not in blobs]
    if missing:
        raise SystemExit(f"{commit}:{path} is missing {missing}")
    return {name: blobs[name] for name in allowed}


def write_manifest(
    target: Target, repo: str, commit: str, path: str, blobs: dict[str, str]
) -> None:
    lines = [
        header(target),
        f'repo = "{repo}"',
        f'commit = "{commit}"',
        f'path = "{path}"',
        "",
        "[blobs]",
    ]
    lines += [f'"{name}" = "{sha}"' for name, sha in blobs.items()]
    target.manifest.write_text("\n".join(lines) + "\n")


def run(target: Target, platform: Path, check: bool) -> int:
    name = target.manifest.relative_to(ROOT)
    manifest = tomllib.loads(target.manifest.read_text())
    commit, path = manifest["commit"], manifest["path"]
    blobs = upstream_blobs(platform, commit, path, target.allowed)

    if check:
        if manifest.get("blobs") != blobs:
            print(f"{name}: blobs do not match {commit}", file=sys.stderr)
            return 1
        print(f"{name} matches {commit}")
        return 0

    # The manifest may live in the same directory as the files it describes.
    expected = set(target.allowed)
    if target.manifest.parent == target.dest:
        expected.add(target.manifest.name)
    for existing in target.dest.glob("*"):
        if existing.name not in expected:
            print(f"refusing to continue: unexpected file {existing}", file=sys.stderr)
            return 1
    for name in target.allowed:
        (target.dest / name).write_bytes(git(platform, "show", f"{commit}:{path}/{name}"))
        print(f"vendored {name}")
    write_manifest(target, manifest["repo"], commit, path, blobs)
    return 0


def main() -> int:
    args = sys.argv[1:]
    check = "--check" in args
    args = [a for a in args if a != "--check"]
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    platform = Path(args[0])
    # Every set is checked or vendored, even after one fails, so one run reports them all.
    return max(run(target, platform, check) for target in TARGETS)


if __name__ == "__main__":
    sys.exit(main())
