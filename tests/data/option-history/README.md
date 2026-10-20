# Option history fixtures

Copied from qte-platform PR #2317 at dcd707d8, path `backtester/historyrebuild/testdata/option-history/`:

- `2026-10-27-option_chain.ndjson`: the whole file.
- `2026-10-27-SPY261120C00665000-option_greeks.ndjson`: the file's first three lines of 79.

`tests/test_history.py` serves them byte for byte from its fake history service and decodes them through `fetch_option_chain` and the `option_greeks` channel. Refresh them from qte-platform if the history contract (09) changes the lines' shape.
