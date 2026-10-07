# Developing qte-sdk

**Version:** 0.13

## Requirements

Python 3.11 or later. CI tests 3.11, 3.12, 3.13 and 3.14 on Linux, and 3.11 on Windows.

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

### Windows

The `windows` job runs on GitHub's hosted `windows-latest` runner, with Python 3.11, and is the only real Windows test. Tests that need the real Windows API are marked `windows` (`@pytest.mark.windows`, or `pytestmark = pytest.mark.windows` for a module); `tests/conftest.py` skips them unless `sys.platform == "win32"`, so the other jobs and a plain `pytest` on macOS or Linux skip them. `tests/test_windows_real.py` holds them: with access lists and owners made by `icacls`, it checks that a `.env` holding a fake token in a private folder gives no warning, that the same file in a folder opened to BUILTIN\Users warns about the folder, that a file opened to Users warns about the file too, which owners are reported, that a `.env` reached through symbolic links or a junction warns about each folder that holds a link on the way, a chain of two links included, and the folder of the file it leads to, that a Ctrl-C while warning carries no token, and what `python -m qte_sdk.token set` and `check` print; and that `python -m qte_sdk.token set` with its input from `NUL` or a pipe refuses at once. Nothing in `qte_sdk._fileaccess` or the console check is replaced there, and only fake tokens are used. The rest of the suite tests the same code on every system with canned access lists.

The job runs the tests marked `windows` first, and then `scripts/check_windows_tests.py` fails it if any test in `tests/test_windows_real.py`, marked or not, or any test marked `windows` elsewhere, is missing by name from the run's JUnit report, or was skipped. The script lists those tests in a pytest process of its own, with no `addopts`, no `PYTEST_ADDOPTS` or `PYTEST_PLUGINS`, no conftest (`--noconftest`) and no plugin loaded by entry point, as each test is collected, before any `-k`, `-m`, `--deselect` or hook that selects tests leaves one out; the report has no entry for a deselected test, so a test that loses its mark or is filtered out of the run is reported as missing. It then runs the rest of the suite, except eight files that do not work on Windows yet:

- `test_reconnect.py`, `test_resume.py`, `test_calendar.py`, `test_instruments.py`, `test_new_message_types.py` and `test_tickets.py`: their fake exchange drops a connection with `transport.abort()`, and on Windows the reset discards the frames sent just before it, so the client sees the drop a message early, and one test hangs.
- `test_history.py`: its tests of cancelling a fetch and of shutting down a TLS connection time out on Windows.
- `test_examples.py`: it sends `SIGINT` to child processes and uses `preexec_fn`, which Windows does not have.

A few other tests check what is printed on macOS and Linux, or need POSIX modes, FIFOs or symbolic links, and skip themselves on Windows. `.gitattributes` marks `proto/**` and `conformance/**` as `-text`, so git checks the vendored contract and conformance steps out byte for byte on every clone, a Windows clone with git's default `core.autocrlf=true` included, and their hash checks pass there; the job checks out with the runner's default settings to prove it. The generated `qte_sdk/contract/v1` is left to git's usual line-ending handling: nothing hashes it, and `scripts/generate_contract.py` writes it with the system's line endings, which git normalizes when it compares under the default `core.autocrlf=true`, so a regeneration on Windows shows no changes there. The `contract-drift` job, which compares the regenerated code with what is committed, runs on Linux. The runner works as an elevated administrator, and as the computer's built-in Administrator account, which a student usually is not; `tests/test_windows_real.py` says what that changes.

To run the Windows tests on a Windows machine:

```sh
pytest -m windows
```

## Releases

A release is a `vX.Y.Z` tag on a merge commit on `main`, whose number matches `__version__` in `qte_sdk/__init__.py`, with an entry in `CHANGELOG.md`. To make one:

1. In a pull request, set `__version__` to the new number and, in `CHANGELOG.md`, rename the "Unreleased" heading to the number (`## 1.0.1`) and start a new empty "Unreleased" section above it.
2. Merge it.
3. On the merge commit, check the tag before you make it:

   ```sh
   git switch main && git pull
   python scripts/check_release_tag.py v1.0.1
   ```

   It checks the tag's form, that it matches `__version__`, and that `CHANGELOG.md` has an entry for it.
4. Tag that commit and push the tag:

   ```sh
   git tag -a v1.0.1 -m "qte-sdk 1.0.1"
   git push origin v1.0.1
   ```

A change to the dependencies in `pyproject.toml` must ship in a new release, with a version bump: `python -m qte_sdk.update` takes newer commits between releases with `--force-reinstall --no-deps`, which does not reinstall dependencies, while its update to a new release resolves them as usual.

The `Release tag` workflow runs on every pushed `v*` tag. It runs the same script, builds the package and checks that the wheel and sdist carry the tag's version, and that the tagged commit is on `main`, and fails if not. A tag that fails it should be deleted and made again on the right commit. `python -m qte_sdk.update` reads the tags, so a release is what participants are told to update to as soon as its tag is pushed.

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

`tests/test_conformance.py` runs the WebSocket session of `conformance/CONFORMANCE.md` (steps 1 to 16, with 1a and 9a to 9c) against an exchange, through the SDK's public API, one test per step and one per sub-step of step 15. Step 15 checks what the SDK hides from its user on purpose: it answers every ping, absorbs heartbeats, parses payloads and drops a report it has already delivered. So most of its sub-steps also drive connections frame by frame with the `websockets` library, building and reading messages with the SDK's codec and contract types. It does not run the history service steps. It is skipped unless the exchange URL, a token and the instrument are all set, so CI and a plain `pytest` skip it. Run it against a test exchange on your own machine:

```sh
export QTE_CONFORMANCE_URL=ws://127.0.0.1:8080/ws
export QTE_CONFORMANCE_INSTRUMENT=QTEA
read -rs QTE_CONFORMANCE_TOKEN && export QTE_CONFORMANCE_TOKEN
pytest tests/test_conformance.py -v
```

`read -rs` takes the token without echoing it or keeping it in your shell history. The token is read from `QTE_CONFORMANCE_TOKEN` only, never from `QTE_TOKEN`. A URL whose host is not this machine is refused unless `QTE_CONFORMANCE_ALLOW_REMOTE=1` is also set, because the steps send real orders.

The exchange under test must provide what the script's preconditions name: one team with two registered strategies (`strat-a` and `strat-b` unless `QTE_CONFORMANCE_STRAT_A` and `QTE_CONFORMANCE_STRAT_B` name others), and the instrument with a two-sided live quote during an open session. Steps 3 to 14 are skipped while the session is not open. Other settings are the instrument's tick (`QTE_CONFORMANCE_TICK`, in dollars, default 0.01), the size of each resting order (`QTE_CONFORMANCE_SIZE`, default 10) and how long to wait for each message (`QTE_CONFORMANCE_TIMEOUT`, in seconds, default 10).

Each step opens its own session, and steps 4 to 14 and the sub-steps of step 15 that rest orders first mass cancel the team's orders, so a failing or skipped step does not affect the next. The steps whose preconditions need the exchange's operators run only when you declare them:

| Variable | Step | What the exchange under test does |
| --- | --- | --- |
| `QTE_CONFORMANCE_COUNTERPARTY=1` | 6, 7 | A scripted counterparty aggresses part of the step's resting buy. Step 7 amends down after that partial fill, and skips if it leaves fewer than 2 shares. |
| `QTE_CONFORMANCE_RESTING_SELL=1` | 9c | A counterparty of another team rests a sell of `QTE_CONFORMANCE_SIZE` shares strictly inside the band, above the two lowest buy prices inside it, with nothing else resting at or below it on the ask side. |
| `QTE_CONFORMANCE_WALL_ONLY=1` | 11 | Nothing but the wall trades against the step's market buy, and the instrument's quote holds steady while the order waits out its delay. The step skips if its ten ask levels do not all lie within mark x 1.05, or, once its fills and trade prints are checked, if no rebuilt book is published after the sweep: a book is published only when it changes, so a sweep whose impact is already at its clamp may leave the ladder as it was. The team's risk limits must allow buying through ten ask levels, and the position is left open. |
| `QTE_CONFORMANCE_CLOSE_WITHIN=<seconds>` | 14 | It runs a single configured session and closes it within that many seconds of the step resting its order. |
| `QTE_CONFORMANCE_RESTART_CMD=<command>` | 15 | The command restarts it on its own state, between two of step 15's sub-steps and before step 14's close, with the instrument's session still open afterwards. The command must not return until the exchange it restarts has stopped, though it may return before the new one accepts connections. The sub-step runs the command through the shell and waits up to `QTE_CONFORMANCE_RESTART_WITHIN` seconds (default 120) for it to finish, and again for the exchange to accept a connection. |
| `QTE_CONFORMANCE_RECORDED_SESSION=1` | 16 | It is fed the recorded market session the step names: one valid QTEA quote, a bid of 99.99 and an ask of 100.01 at the session open, never replaced, and no quote of QTEB or QTEC. `QTE_CONFORMANCE_INSTRUMENT` must then be `QTEA`, or step 16 fails. Step 16 then checks that QTEA's `official_close` has the value `100000000` and that QTEB and QTEC get none. Without the variable, step 16 makes its other checks and then skips those. If the exchange rejects QTEB or QTEC as unknown, step 16 makes every other check, QTEA's value included, and then skips. |

Step 15's precondition puts its restart before step 14's close and its empty marker after it, and step 16 needs no restart after that close, so the file runs step 15's heartbeat, silence, replay, snapshot, restart and market data sub-steps after step 13 and before step 14, then step 14, then the empty marker, then the heartbeat and silence sub-steps again outside a session, then step 16. The empty marker and step 16 run only after step 14 has closed the session in the same run, so run the whole file in order with step 14 enabled to cover them. The heartbeat and silence sub-steps wait out the exchange's own 15 s and 45 s timers, about three minutes each time. Every timestamp is milliseconds since the Unix epoch, UTC, as the steps state, and the steps only compare and subtract timestamps or add the order delay they learn from them, so their arithmetic stays in milliseconds. Steps 10 and 11 use the figure the script names for the buy collar, mark x 1.05, so the instrument must be an equity under that guard; an option's guard is wider. Step 10 skips if the mark in force at the order's release has risen enough to put its price inside the collar. Steps 3 and 11 check the wall ladder: up to ten levels a side, both sides spaced uniformly by the asks' step and every level at least one share. How many levels a side shows is the exchange's setting, so they accept fewer bid levels than ask levels only where one more step would take the next bid to zero or below, since the bid ladder of a low-priced instrument stops at its last positive price. Step 11 needs ten ask levels and skips otherwise. `tests/test_conformance_ladder.py` checks the ladder rule without an exchange. Step 13's "no budget consumed" is not checked, since no message reports a team's budget use. Step 1a checks each option entry's terms against its OCC symbol, and `tests/test_conformance_instruments.py` checks that rule without an exchange. Step 1a's resend of `instruments` after a listing changes is not checked, since nothing here can change a listing. Step 15's snapshots are of orders with no fills, so their remaining size is checked only as the whole order, and its replay is checked for a cursor inside the window and one above the newest report, not one older than the window.
