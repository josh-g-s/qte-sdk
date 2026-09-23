# qte-sdk

Public Python client SDK for the Queen's Tower Exchange. Competing teams use it to
connect their strategies to QTE: authenticate, subscribe to market data, and send
and manage orders.

This repo contains **only** participant-facing material. Exchange internals (the
matching engine, risk parameters, the synthetic-L2 model, the bot fleet) live in a
separate private repo and are intentionally not here.

## Contents

| Path | What |
|---|---|
| `qte_sdk/` | Python client |
| `examples/` | Worked examples: connect, quote, take |
| `docs/` | API reference and quickstart |

## Status

Scaffold only. The client skeleton is tracked in
[#1](https://github.com/josh-g-s/qte-sdk/issues/1).

## Licence

MIT. See [LICENSE](LICENSE).
