"""Regenerate qte_sdk/contract/v1 from the vendored protos in proto/.

    pip install -e ".[dev,codegen]"
    python scripts/generate_contract.py

CI runs this and fails if the committed generated code differs.
"""

import hashlib
import re
import shutil
import sys
import tempfile
import tomllib
from importlib import resources
from pathlib import Path

from grpc_tools import protoc

ROOT = Path(__file__).resolve().parent.parent
PROTO_ROOT = ROOT / "proto"
PROTO_DIR = PROTO_ROOT / "qte" / "contract" / "v1"
MANIFEST = PROTO_ROOT / "upstream.toml"
OUT = ROOT / "qte_sdk" / "contract" / "v1"

INIT = '''"""Generated contract types. Do not edit: run scripts/generate_contract.py."""
'''

# protoc emits imports rooted at the proto package (qte.contract.v1); the SDK ships the
# modules under qte_sdk.contract.v1 so it never claims the top-level `qte` name.
IMPORT = re.compile(r"^from qte\.contract\.v1 import ", re.MULTILINE)
# The module name the generated code registers itself under must match where it lives,
# or pickling a message fails.
MODULE_NAME = re.compile(r"(BuildTopDescriptorsAndMessages\(DESCRIPTOR, )'qte\.contract\.v1\.")


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def main() -> int:
    manifest = tomllib.loads(MANIFEST.read_text())
    blobs: dict[str, str] = manifest["blobs"]
    listed = sorted(blobs)
    present = sorted(p.name for p in PROTO_DIR.iterdir())
    if present != listed:
        print(f"proto dir {present} does not match the manifest {listed}", file=sys.stderr)
        return 1
    for name, sha in blobs.items():
        actual = git_blob_sha((PROTO_DIR / name).read_bytes())
        if actual != sha:
            print(f"{name} hashes to {actual}, but the manifest pins {sha}", file=sys.stderr)
            return 1
    protos = [PROTO_DIR / name for name in listed]
    well_known = str(resources.files("grpc_tools") / "_proto")
    with tempfile.TemporaryDirectory() as tmp:
        args = [
            "protoc",
            f"-I{PROTO_ROOT}",
            f"-I{well_known}",
            f"--python_out={tmp}",
            f"--pyi_out={tmp}",
            *[str(p.relative_to(PROTO_ROOT)) for p in protos],
        ]
        if protoc.main(args) != 0:
            print("protoc failed", file=sys.stderr)
            return 1

        if OUT.exists():
            shutil.rmtree(OUT)
        OUT.mkdir(parents=True)
        (OUT / "__init__.py").write_text(INIT)
        generated = Path(tmp) / "qte" / "contract" / "v1"
        for src in sorted(generated.iterdir()):
            text = IMPORT.sub("from qte_sdk.contract.v1 import ", src.read_text())
            text = MODULE_NAME.sub(r"\1'qte_sdk.contract.v1.", text)
            (OUT / src.name).write_text(text)
    print(f"generated {len(protos)} protos into {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
