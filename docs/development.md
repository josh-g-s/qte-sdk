# Developing qte-sdk

**Version:** 0.1

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
