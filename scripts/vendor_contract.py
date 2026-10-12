"""Copy the approved files from a local checkout of the exchange's contract repository.

Maintainers only: the exchange's contract repository is not public.

    python scripts/vendor_contract.py /path/to/contract-repository           # vendor at the pins
    python scripts/vendor_contract.py /path/to/contract-repository --check   # verify the manifests

Two sets of files are vendored, each with its own manifest and pinned commit: the contract
.proto files (proto/upstream.toml) and the published conformance steps
(conformance/upstream.toml). For each set, reads the commit from its manifest, copies exactly
the approved files byte for byte from that commit, and rewrites the manifest with each
file's git blob hash as `git ls-tree` prints it. Its `approved` line (who approved the pin,
and when) is kept only while its `approved_commit` is the commit vendored: a re-vendor at
another commit drops both, and says so, since that approval was not for this commit.
CI cannot see that repository, so it checks the vendored bytes against those hashes
instead, and that the approval is for the pinned commit; `--check` is how a maintainer
confirms the hashes really belong to the pinned commits, and fails too on an approval for
another commit.
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
# `approved`, when present, records who approved the pin and when, and `approved_commit`
# the commit approved; set both with `commit`. Re-vendoring another commit drops them.
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
    target: Target,
    repo: str,
    commit: str,
    path: str,
    blobs: dict[str, str],
    approved: str | None = None,
    approved_commit: str | None = None,
) -> None:
    lines = [
        header(target),
        f'repo = "{repo}"',
        f'commit = "{commit}"',
        *([f'approved = "{approved}"'] if approved is not None else []),
        *([f'approved_commit = "{approved_commit}"'] if approved_commit is not None else []),
        f'path = "{path}"',
        "",
        "[blobs]",
    ]
    lines += [f'"{name}" = "{sha}"' for name, sha in blobs.items()]
    # LF on every OS: the manifest is vendored byte for byte (.gitattributes marks it -text),
    # so a CRLF written on Windows would change the committed file.
    target.manifest.write_text("\n".join(lines) + "\n", newline="\n")


def run(target: Target, platform: Path, check: bool) -> int:
    name = target.manifest.relative_to(ROOT)
    manifest = tomllib.loads(target.manifest.read_text())
    commit, path = manifest["commit"], manifest["path"]
    blobs = upstream_blobs(platform, commit, path, target.allowed)
    approved, approved_commit = manifest.get("approved"), manifest.get("approved_commit")
    misapproved = (approved is not None or approved_commit is not None) and (
        approved is None or approved_commit != commit
    )
    approved_for = approved_commit or "no recorded commit"

    if check:
        failed = 0
        if manifest.get("blobs") != blobs:
            print(f"{name}: blobs do not match {commit}", file=sys.stderr)
            failed = 1
        if misapproved:
            print(
                f"{name}: the approval is for {approved_for}, not the pinned {commit}",
                file=sys.stderr,
            )
            failed = 1
        if not failed:
            print(f"{name} matches {commit}")
        return failed

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
    if misapproved:
        print(
            f"{name}: APPROVAL DROPPED: {approved!r} approved {approved_for}, not {commit};"
            " record who approved this commit in `approved` and `approved_commit`",
            file=sys.stderr,
        )
        approved = approved_commit = None
    write_manifest(target, manifest["repo"], commit, path, blobs, approved, approved_commit)
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
