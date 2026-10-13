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
        values.get("approved_commit"),
    )
    written = out.manifest.read_bytes()
    assert b"\r" not in written
    assert written == committed


def test_the_contract_pin_records_its_approval_for_the_pinned_commit():
    # CI cannot see the contract repository, so this is what stops a pin nobody approved
    # passing as approved: the approval must name the very commit pinned.
    assert MANIFEST["approved"].startswith("Head, ")
    assert MANIFEST["approved_commit"] == MANIFEST["commit"]


@pytest.mark.parametrize("manifest", [MANIFEST, CONFORMANCE_MANIFEST], ids=["proto", "conformance"])
def test_an_approval_is_never_for_another_commit(manifest):
    if "approved" in manifest or "approved_commit" in manifest:
        assert manifest.get("approved_commit") == manifest["commit"]
        assert "approved" in manifest


def test_a_rewrite_writes_the_approval_it_is_given(tmp_path):
    target = dataclasses.replace(vendor.TARGETS[0], manifest=tmp_path / "upstream.toml")
    vendor.write_manifest(
        target, "repo", "abc", "path", {"a.proto": "1"}, "Head, 1 Jan 2026", "abc"
    )
    written = tomllib.loads(target.manifest.read_text())
    assert (written["approved"], written["approved_commit"]) == ("Head, 1 Jan 2026", "abc")
    vendor.write_manifest(target, "repo", "abc", "path", {"a.proto": "1"})
    written = tomllib.loads(target.manifest.read_text())
    assert "approved" not in written and "approved_commit" not in written


@pytest.fixture
def contract_repo(tmp_path, monkeypatch):
    """A contract repository made here, with two commits, and a target to vendor from it."""
    upstream = tmp_path / "upstream"
    (upstream / "protos").mkdir(parents=True)

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(upstream), "-c", "user.name=t", "-c", "user.email=t@t", *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q")
    commits = []
    for text in ('syntax = "proto3";\n', 'syntax = "proto3";\n// v2\n'):
        (upstream / "protos" / "a.proto").write_text(text)
        git("add", ".")
        git("commit", "-q", "-m", "contract")
        commits.append(git("rev-parse", "HEAD"))
    dest = tmp_path / "dest"
    dest.mkdir()
    manifest = tmp_path / "upstream.toml"
    target = vendor.Target(manifest=manifest, dest=dest, allowed=("a.proto",), what="a test")
    monkeypatch.setattr(vendor, "ROOT", tmp_path)  # run() names the manifest from it
    return upstream, target, commits, git


def write_pin(target, commit: str, approved_commit: str | None, git) -> None:
    """A manifest whose blobs are right for `commit`, so that only its approval can fail."""
    approval = (
        f'approved = "Head, 12 Oct 2026"\napproved_commit = "{approved_commit}"\n'
        if approved_commit is not None
        else ""
    )
    blob = git("rev-parse", f"{commit}:protos/a.proto")
    target.manifest.write_text(
        f'repo = "r"\ncommit = "{commit}"\n{approval}path = "protos"\n\n'
        f'[blobs]\n"a.proto" = "{blob}"\n'
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on the PATH")
def test_vendoring_the_approved_commit_keeps_the_approval(contract_repo):
    upstream, target, (first, _second), git = contract_repo
    write_pin(target, first, first, git)
    assert vendor.run(target, upstream, check=False) == 0
    written = tomllib.loads(target.manifest.read_text())
    assert written["approved"] == "Head, 12 Oct 2026"
    assert written["approved_commit"] == first
    assert written["blobs"] == {"a.proto": git("rev-parse", f"{first}:protos/a.proto")}
    assert (target.dest / "a.proto").read_text() == 'syntax = "proto3";\n'
    assert vendor.run(target, upstream, check=True) == 0


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on the PATH")
def test_vendoring_another_commit_drops_the_approval_and_check_fails(contract_repo, capsys):
    upstream, target, (first, second), git = contract_repo
    # `commit` bumped by hand, the approval left for the old one: --check refuses it...
    write_pin(target, second, first, git)
    assert vendor.run(target, upstream, check=True) == 1
    assert f"the approval is for {first}, not the pinned {second}" in capsys.readouterr().err
    # ...and vendoring drops it, loudly, so the pin stands unapproved until it is recorded.
    assert vendor.run(target, upstream, check=False) == 0
    (dropped,) = [x for x in capsys.readouterr().err.splitlines() if "APPROVAL DROPPED" in x]
    # Named by its manifest, not by the last file vendored.
    assert dropped.startswith(f"{target.manifest.relative_to(vendor.ROOT)}: APPROVAL DROPPED")
    written = tomllib.loads(target.manifest.read_text())
    assert written["commit"] == second
    assert "approved" not in written and "approved_commit" not in written
    assert (target.dest / "a.proto").read_text().endswith("// v2\n")
    assert vendor.run(target, upstream, check=True) == 0


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on the PATH")
def test_an_approval_without_its_commit_is_dropped(contract_repo, capsys):
    upstream, target, (first, _second), git = contract_repo
    write_pin(target, first, first, git)
    text = target.manifest.read_text()
    target.manifest.write_text(text.replace(f'approved_commit = "{first}"\n', ""))
    assert "approved_commit" not in target.manifest.read_text()
    assert vendor.run(target, upstream, check=True) == 1
    assert f"the approval is for no recorded commit, not the pinned {first}" in (
        capsys.readouterr().err
    )
    assert vendor.run(target, upstream, check=False) == 0
    assert "APPROVAL DROPPED" in capsys.readouterr().err
    assert vendor.run(target, upstream, check=True) == 0
    assert "approved" not in tomllib.loads(target.manifest.read_text())
