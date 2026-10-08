"""The line-ending check of the generated contract code, `scripts/check_contract_eol.py`,
that CI's Windows job runs before and after regenerating it."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_contract_eol.py"
OUT = "qte_sdk/contract/v1"


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("check_contract_eol", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def line(path: str, index: str = "i/lf", tree: str = "w/lf") -> str:
    return f"{index:<8}{tree:<8}attr/text eol=lf      \t{path}\n"


EXPECTED = {f"{OUT}/__init__.py", f"{OUT}/common_pb2.py", f"{OUT}/common_pb2.pyi"}
GOOD = "".join(line(p) for p in sorted(EXPECTED))


def test_the_expected_files_are_init_and_a_py_and_pyi_per_pinned_proto():
    expected = load_script().expected_files()
    protos = list((ROOT / "proto" / "qte" / "contract" / "v1").glob("*.proto"))
    assert len(expected) == 1 + 2 * len(protos)
    assert f"{OUT}/__init__.py" in expected
    for proto in protos:
        assert f"{OUT}/{proto.stem}_pb2.py" in expected
        assert f"{OUT}/{proto.stem}_pb2.pyi" in expected


def test_this_checkout_passes():
    git = subprocess.run(
        ["git", "ls-files", "--eol", OUT], cwd=ROOT, capture_output=True, text=True
    )
    assert git.returncode == 0
    script = load_script()
    assert script.problems(git.stdout, script.expected_files()) == []


def test_every_expected_file_with_lf_passes():
    assert load_script().problems(GOOD, EXPECTED) == []


def test_an_empty_listing_fails():
    found = load_script().problems("", EXPECTED)
    assert f"git ls-files lists nothing in {OUT}" in found
    assert len(found) == 1 + len(EXPECTED)


def test_a_missing_file_fails():
    listing = "".join(line(p) for p in sorted(EXPECTED) if not p.endswith(".pyi"))
    assert load_script().problems(listing, EXPECTED) == [
        f"{OUT}/common_pb2.pyi is generated but git does not list it"
    ]


def test_an_extra_file_fails():
    found = load_script().problems(GOOD + line(f"{OUT}/stale_pb2.py"), EXPECTED)
    assert found == [
        f"{OUT}/stale_pb2.py is listed but scripts/generate_contract.py does not write it"
    ]


def test_crlf_in_the_index_or_the_tree_fails():
    for index, tree in (("i/crlf", "w/lf"), ("i/lf", "w/crlf"), ("i/mixed", "w/mixed")):
        listing = GOOD.replace(
            line(f"{OUT}/common_pb2.py"), line(f"{OUT}/common_pb2.py", index, tree)
        )
        assert load_script().problems(listing, EXPECTED) == [
            f"{OUT}/common_pb2.py is {index} {tree}, not i/lf w/lf"
        ]


def test_an_unreadable_line_fails():
    found = load_script().problems(GOOD + "garbage\n", EXPECTED)
    assert found == ["cannot read the git ls-files line 'garbage'"]


def test_no_expected_files_fails():
    assert load_script().problems(GOOD, set()) != []


def test_the_script_fails_when_git_lists_nothing(tmp_path: Path):
    # A copy of the script in a fresh repository with the manifest but no generated code:
    # git ls-files succeeds and lists nothing, which must fail the check.
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "check_contract_eol.py").write_bytes(SCRIPT.read_bytes())
    (tmp_path / "proto").mkdir()
    (tmp_path / "proto" / "upstream.toml").write_text('[blobs]\n"common.proto" = "0"\n')
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    run = subprocess.run(
        [sys.executable, str(tmp_path / "scripts" / "check_contract_eol.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 1
    assert f"git ls-files lists nothing in {OUT}" in run.stderr


def test_the_script_fails_when_git_fails(tmp_path: Path):
    # Outside any repository git ls-files exits non-zero.
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "check_contract_eol.py").write_bytes(SCRIPT.read_bytes())
    (tmp_path / "proto").mkdir()
    (tmp_path / "proto" / "upstream.toml").write_text('[blobs]\n"common.proto" = "0"\n')
    run = subprocess.run(
        [sys.executable, str(tmp_path / "scripts" / "check_contract_eol.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path.parent)},
    )
    assert run.returncode == 1
    assert "git ls-files failed" in run.stderr
