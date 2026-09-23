# Developing qte-sdk

**Version:** 0.2

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

CI also installs the built wheel into a clean virtualenv and imports `qte_sdk`, to catch packaging mistakes that an editable install hides.

## Contract types

The wire contract is defined by six `.proto` files vendored from qte-platform into `proto/qte/contract/v1/`. `proto/upstream.toml` records the one qte-platform commit they came from. Only these six files are approved for publication in this repo; never add other platform files.

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

and commit the protos, the manifest and the generated code together.
