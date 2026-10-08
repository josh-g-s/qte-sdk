"""Print the guidance for coding agents that ships with the SDK.

    python -m qte_sdk.agents

The guidance is `qte_sdk/AGENTS.md`, a copy of the repository's AGENTS.md that is installed
with the package, so it can be read after any install, including one from a release zip with
no clone. The command prints it to stdout and exits 0. `text()` returns the same text.
"""

import argparse
import sys
from importlib import resources


def text() -> str:
    """The packaged AGENTS.md, read through importlib.resources so any install can read it."""
    return resources.files("qte_sdk").joinpath("AGENTS.md").read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Print the packaged AGENTS.md and return 0."""
    parser = argparse.ArgumentParser(
        prog="python -m qte_sdk.agents",
        description="Print the guidance for coding agents that ships with qte-sdk (AGENTS.md).",
    )
    parser.parse_args(argv)
    sys.stdout.write(text())
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
