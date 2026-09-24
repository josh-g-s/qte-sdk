# Developing qte-sdk

**Version:** 0.5

## Requirements

Python 3.11 or later. CI tests 3.11, 3.12, 3.13 and 3.14.

## Layout

The package uses a flat layout: the importable package is `qte_sdk/` at the repo root, tests live in `tests/`, and the build backend is hatchling, configured in `pyproject.toml`. The package version is read from `qte_sdk/__init__.py`.

## Set up

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Checks

These are the same checks CI runs on every pull request:

```sh
ruff check .
ruff format --check .
pytest
python -m build
```

CI also installs the built wheel into a clean virtualenv and imports `qte_sdk`, to catch packaging mistakes that an editable install hides. It then unpacks the built sdist, installs it with the dev extra in another clean virtualenv and runs the whole test suite from it, so a file missing from the sdist allowlist in `pyproject.toml` fails CI. To run the same check locally:

```sh
rm -rf dist /tmp/sdist /tmp/sdist-venv
python -m build --sdist
mkdir /tmp/sdist && tar -xzf dist/*.tar.gz -C /tmp/sdist
python3 -m venv /tmp/sdist-venv
(cd /tmp/sdist/qte_sdk-* && /tmp/sdist-venv/bin/pip install ".[dev]" && /tmp/sdist-venv/bin/pytest -q)
```

It clears earlier builds first, so it can be run again and each path matches one file.

## Contract types

The wire contract is defined by six `.proto` files vendored from qte-platform into `proto/qte/contract/v1/`. `proto/upstream.toml` records the one qte-platform commit they came from and each file's git blob hash at that commit. Generation and the tests fail if a vendored file's bytes do not hash to its recorded blob, so the protos cannot drift from the pin unnoticed. Only these six files are approved for publication in this repo; never add other platform files.

The Python types in `qte_sdk/contract/v1/` are generated from the vendored protos and are never edited by hand. Import them as `qte_sdk.contract.v1`, for example `from qte_sdk.contract.v1.order_entry_pb2 import NewOrder`, and use `qte_sdk.contract.codec` to encode and decode the JSON wire format.

To regenerate after changing the vendored protos:

```sh
pip install -e ".[dev,codegen]"
python scripts/generate_contract.py
```

CI regenerates on every pull request and fails if the result differs from what is committed.

To move to a newer contract (maintainers with qte-platform access only): change `commit` in `proto/upstream.toml`, then run

```sh
python scripts/vendor_contract.py /path/to/qte-platform
python scripts/generate_contract.py
```

and commit the protos, the manifest and the generated code together. The vendor script rewrites the blob hashes itself. `python scripts/vendor_contract.py /path/to/qte-platform --check` confirms that the recorded hashes belong to the pinned commit without changing anything.
