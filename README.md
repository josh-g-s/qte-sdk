# qte-sdk

Public client and backtesting SDK for the Queen's Tower Exchange. This is what
competing teams use to connect their strategies to QTE and to backtest against
recorded sessions.

This repo contains **only** participant-facing material. Exchange internals (the
matching engine, risk parameters, the synthetic-L2 model, the bot fleet) live in a
separate private repo and are intentionally not here.

## Contents

| Path | What |
|---|---|
| `qte_sdk/` | Python client + fill-simulator |
| `examples/` | Worked examples: connect, quote, take, backtest |
| `docs/` | API reference and quickstart |

## Status

Scaffold only. The client interface and fill-simulator land in Sprint 1 (SDK alpha)
per the project calendar.

## Licence

MIT. See [LICENSE](LICENSE).
