"""Copy the approved contract .proto files from a local qte-platform checkout.

Maintainers only: qte-platform is private. Reads the commit and file list from
proto/upstream.toml and copies exactly those files, byte for byte, from that commit.

    python scripts/vendor_contract.py /path/to/qte-platform
"""

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "proto" / "upstream.toml"
DEST = ROOT / "proto" / "qte" / "contract" / "v1"

# The Head-approved publication allowlist (decision recorded on qte-sdk #5).
ALLOWED = {
    "common.proto",
    "envelope.proto",
    "session.proto",
    "order_entry.proto",
    "order_events.proto",
    "market_data.proto",
}


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    platform = Path(sys.argv[1])
    manifest = tomllib.loads(MANIFEST.read_text())
    files = set(manifest["files"])
    if files != ALLOWED:
        print(f"manifest files {sorted(files)} differ from the approved allowlist", file=sys.stderr)
        return 1

    for existing in DEST.glob("*"):
        if existing.name not in ALLOWED:
            print(f"refusing to continue: unexpected file {existing}", file=sys.stderr)
            return 1

    for name in sorted(files):
        blob = f"{manifest['commit']}:{manifest['path']}/{name}"
        data = subprocess.run(
            ["git", "-C", str(platform), "show", blob], check=True, capture_output=True
        ).stdout
        (DEST / name).write_bytes(data)
        print(f"vendored {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
