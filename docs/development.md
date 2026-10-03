# Developing qte-sdk

**Version:** 0.7

## Requirements

Python 3.11 or later. CI tests 3.11, 3.12, 3.13 and 3.14.

The connection code depends on behaviour that differs between websockets releases, so the supported websockets range is tested at both ends. `pyproject.toml` allows `websockets>=15,<18`. The `test` job installs the newest websockets release inside the range on every Python version, and the `test-websockets-floor` job installs exactly websockets 15.0, the oldest release the range allows, and runs the whole test suite on Python 3.11. Raising the lower bound in `pyproject.toml` means updating the pin in that job and this paragraph together. To run the floor check locally:

```sh
pip install -e ".[dev]" "websockets==15.0"
pytest
```

Run `pip install --upgrade "websockets>=15,<18"` afterwards to go back to the newest release.

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

The wire contract is defined by six `.proto` files vendored from the exchange's contract repository into `proto/qte/contract/v1/`. `proto/upstream.toml` records the one commit of that repository they came from and each file's git blob hash at that commit. Generation and the tests fail if a vendored file's bytes do not hash to its recorded blob, so the protos cannot drift from the pin unnoticed. Only these six files are approved for publication in this repo; never add other platform files.

The Python types in `qte_sdk/contract/v1/` are generated from the vendored protos and are never edited by hand. Import them as `qte_sdk.contract.v1`, for example `from qte_sdk.contract.v1.order_entry_pb2 import NewOrder`, and use `qte_sdk.contract.codec` to encode and decode the JSON wire format.

To regenerate after changing the vendored protos:

```sh
pip install -e ".[dev,codegen]"
python scripts/generate_contract.py
```

CI regenerates on every pull request and fails if the result differs from what is committed.

To move to a newer contract (maintainers with access to the exchange's contract repository only): change `commit` in `proto/upstream.toml`, then run

```sh
python scripts/vendor_contract.py /path/to/contract-repository
python scripts/generate_contract.py
```

and commit the protos, the manifest and the generated code together. The vendor script rewrites the blob hashes itself. `python scripts/vendor_contract.py /path/to/contract-repository --check` confirms that the recorded hashes belong to the pinned commits without changing anything.

## Conformance steps

`conformance/CONFORMANCE.md` is the exchange's published conformance script, vendored byte for byte like the protos. `conformance/upstream.toml` is its own manifest, with its own pinned commit of the exchange's contract repository, path and blob hash. The vendor script copies and checks both sets, so the commands above cover it: bump `commit` in `conformance/upstream.toml` to move to a newer version. The tests fail if the file's bytes do not hash to the recorded blob, or if anything else appears in `conformance/`. It has no generated code, so the regeneration check in CI does not apply to it. Never edit it by hand, and never add other platform files.

## Running the conformance session

`tests/test_conformance.py` runs the WebSocket session of `conformance/CONFORMANCE.md` (steps 1 to 16, with 9a to 9c) against an exchange, through the SDK's public API, one test per step. It does not run the history service steps. It is skipped unless the exchange URL, a token and the instrument are all set, so CI and a plain `pytest` skip it. Run it against a test exchange on your own machine:

```sh
export QTE_CONFORMANCE_URL=ws://127.0.0.1:8080/ws
export QTE_CONFORMANCE_INSTRUMENT=TEST
read -rs QTE_CONFORMANCE_TOKEN && export QTE_CONFORMANCE_TOKEN
pytest tests/test_conformance.py -v
```

`read -rs` takes the token without echoing it or keeping it in your shell history. The token is read from `QTE_CONFORMANCE_TOKEN` only, never from `QTE_TOKEN`. A URL whose host is not this machine is refused unless `QTE_CONFORMANCE_ALLOW_REMOTE=1` is also set, because the steps send real orders.

The exchange under test must provide what the script's preconditions name: one team with two registered strategies (`strat-a` and `strat-b` unless `QTE_CONFORMANCE_STRAT_A` and `QTE_CONFORMANCE_STRAT_B` name others), and the instrument with a two-sided live quote during an open session. Steps 3 to 14 are skipped while the session is not open. Other settings are the instrument's tick (`QTE_CONFORMANCE_TICK`, in dollars, default 0.01), the size of each resting order (`QTE_CONFORMANCE_SIZE`, default 10) and how long to wait for each message (`QTE_CONFORMANCE_TIMEOUT`, in seconds, default 10).

Each step opens its own session, and steps 4 to 14 first mass cancel the team's orders, so a failing or skipped step does not affect the next. The steps whose preconditions need the exchange's operators run only when you declare them:

| Variable | Step | What the exchange under test does |
| --- | --- | --- |
| `QTE_CONFORMANCE_COUNTERPARTY=1` | 6, 7 | A scripted counterparty aggresses part of the step's resting buy. Step 7 amends down after that partial fill, and skips if it leaves fewer than 2 shares. |
| `QTE_CONFORMANCE_RESTING_SELL=1` | 9c | A counterparty of another team rests a sell of `QTE_CONFORMANCE_SIZE` shares strictly inside the band, above the two lowest buy prices inside it, with nothing else resting at or below it on the ask side. |
| `QTE_CONFORMANCE_WALL_ONLY=1` | 11 | Nothing but the wall trades against the step's market buy, and the instrument's quote holds steady while the order waits out its delay. The step skips if its ten ask levels do not all lie within mark x 1.05. The team's risk limits must allow buying through ten ask levels, and the position is left open. |
| `QTE_CONFORMANCE_CLOSE_WITHIN=<seconds>` | 14 | It runs a single configured session and closes it within that many seconds of the step resting its order. |

Step 16 runs only after step 14 has closed the session in the same run, so run the whole file in order with step 14 enabled to cover it. Steps 10 and 11 use the figure the script names for the buy collar, mark x 1.05, so the instrument must be an equity under that guard; an option's guard is wider. Step 13's "no budget consumed" is not checked, since no message reports a team's budget use. Step 15 (heartbeat and resume) is reported as an expected failure until it is specified (issue #12).
