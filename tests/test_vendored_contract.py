import hashlib
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = tomllib.loads((ROOT / "proto" / "upstream.toml").read_text())
APPROVED = {
    "common.proto",
    "envelope.proto",
    "session.proto",
    "order_entry.proto",
    "order_events.proto",
    "market_data.proto",
}


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def test_manifest_lists_exactly_the_approved_files():
    assert set(MANIFEST["blobs"]) == APPROVED


def test_vendored_bytes_hash_to_the_pinned_blobs():
    for name, sha in MANIFEST["blobs"].items():
        data = (ROOT / "proto" / "qte" / "contract" / "v1" / name).read_bytes()
        assert git_blob_sha(data) == sha, f"{name} differs from the pinned blob"


def test_blob_hash_matches_git():
    path = ROOT / "proto" / "qte" / "contract" / "v1" / "common.proto"
    expected = subprocess.run(
        ["git", "hash-object", "--no-filters", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert git_blob_sha(path.read_bytes()) == expected


def test_manifest_pins_a_full_commit():
    assert re.fullmatch(r"[0-9a-f]{40}", MANIFEST["commit"])


def test_proto_tree_holds_only_the_approved_files():
    vendored = {p.relative_to(ROOT / "proto").as_posix() for p in (ROOT / "proto").rglob("*")}
    vendored = {p for p in vendored if (ROOT / "proto" / p).is_file()}
    expected = {"upstream.toml"} | {f"qte/contract/v1/{name}" for name in APPROVED}
    assert vendored == expected


def test_generated_modules_exist_for_every_vendored_proto():
    out = ROOT / "qte_sdk" / "contract" / "v1"
    for name in APPROVED:
        stem = name.removesuffix(".proto")
        assert (out / f"{stem}_pb2.py").is_file()
        assert (out / f"{stem}_pb2.pyi").is_file()
