import dataclasses
import hashlib
import importlib.util
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

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


# The published conformance steps, vendored the same way from their own pinned commit.
CONFORMANCE_DIR = ROOT / "conformance"
CONFORMANCE_MANIFEST = tomllib.loads((CONFORMANCE_DIR / "upstream.toml").read_text())
CONFORMANCE_APPROVED = {"CONFORMANCE.md"}


def test_conformance_manifest_lists_exactly_the_approved_files():
    assert set(CONFORMANCE_MANIFEST["blobs"]) == CONFORMANCE_APPROVED


def test_conformance_bytes_hash_to_the_pinned_blob():
    for name, sha in CONFORMANCE_MANIFEST["blobs"].items():
        data = (CONFORMANCE_DIR / name).read_bytes()
        assert git_blob_sha(data) == sha, f"{name} differs from the pinned blob"


def test_conformance_manifest_pins_a_full_commit():
    assert re.fullmatch(r"[0-9a-f]{40}", CONFORMANCE_MANIFEST["commit"])


def test_conformance_tree_holds_only_the_approved_files():
    vendored = {p.relative_to(CONFORMANCE_DIR).as_posix() for p in CONFORMANCE_DIR.rglob("*")}
    assert vendored == {"upstream.toml"} | CONFORMANCE_APPROVED


def load_vendor_script() -> Any:
    path = ROOT / "scripts" / "vendor_contract.py"
    spec = importlib.util.spec_from_file_location("vendor_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vendor = load_vendor_script()


@pytest.mark.parametrize(
    "target", vendor.TARGETS, ids=[t.manifest.relative_to(ROOT).as_posix() for t in vendor.TARGETS]
)
def test_write_manifest_reproduces_the_committed_manifest_with_lf(target, tmp_path):
    # The manifests are checked out byte for byte (-text), so rewriting one from its own
    # values must give the same bytes on every OS, Windows included: LF, never CRLF.
    committed = target.manifest.read_bytes()
    values = tomllib.loads(committed.decode())
    out = dataclasses.replace(target, manifest=tmp_path / "upstream.toml")
    vendor.write_manifest(
        out,
        values["repo"],
        values["commit"],
        values["path"],
        values["blobs"],
        values.get("approved"),
    )
    written = out.manifest.read_bytes()
    assert b"\r" not in written
    assert written == committed


def test_the_contract_pin_records_its_approval_and_a_rewrite_keeps_it(tmp_path):
    # Who approved the pin is recorded beside it, and re-running the vendor script, which
    # rewrites the manifest, must not drop it.
    assert MANIFEST["approved"].startswith("Head, ")
    target = dataclasses.replace(vendor.TARGETS[0], manifest=tmp_path / "upstream.toml")
    vendor.write_manifest(target, "repo", "abc", "path", {"a.proto": "1"}, "Head, 1 Jan 2026")
    assert tomllib.loads(target.manifest.read_text())["approved"] == "Head, 1 Jan 2026"
    vendor.write_manifest(target, "repo", "abc", "path", {"a.proto": "1"})
    assert "approved" not in tomllib.loads(target.manifest.read_text())


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on the PATH")
def test_vendoring_keeps_the_approved_line_of_the_manifest(tmp_path, monkeypatch):
    # run() itself, against a contract repository made here, must carry `approved` over.
    upstream = tmp_path / "upstream"
    (upstream / "protos").mkdir(parents=True)
    (upstream / "protos" / "a.proto").write_text('syntax = "proto3";\n')

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(upstream), "-c", "user.name=t", "-c", "user.email=t@t", *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q")
    git("add", ".")
    git("commit", "-q", "-m", "contract")
    commit = git("rev-parse", "HEAD")
    dest = tmp_path / "dest"
    dest.mkdir()
    manifest = tmp_path / "upstream.toml"
    manifest.write_text(
        f'repo = "r"\ncommit = "{commit}"\napproved = "Head, 12 Oct 2026"\npath = "protos"\n'
    )
    target = vendor.Target(manifest=manifest, dest=dest, allowed=("a.proto",), what="a test")
    monkeypatch.setattr(vendor, "ROOT", tmp_path)  # run() names the manifest from it
    assert vendor.run(target, upstream, check=False) == 0
    written = tomllib.loads(manifest.read_text())
    assert written["approved"] == "Head, 12 Oct 2026"
    assert written["blobs"] == {"a.proto": git("rev-parse", "HEAD:protos/a.proto")}
    assert (dest / "a.proto").read_text() == 'syntax = "proto3";\n'
