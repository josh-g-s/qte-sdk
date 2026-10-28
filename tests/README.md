# Tests

**Version:** 0.1

Run the suite from the repository root:

```
pip install -e ".[dev]"
python -m pytest -q
```

Every test runs offline on 127.0.0.1. Nothing here needs a token, AWS or the network.

## Running against a real gateway

The gateway scenarios (`test_handover.py` and `test_gateway_conformance.py`) have two targets, picked by one setting, `QTE_GATEWAY_BIN`:

- Unset (the default, and what CI runs on every platform): they run against the in-test fake gateway.
- Set to the path of a `qte-gateway` binary: the same scenarios run through that binary, in front of two fake exchanges, A (live) and B (follower), on 127.0.0.1. The fakes speak the exchange side of qte-core's `hotswitch-link-protocol.md` v0.12 (`tests/link_exchange.py`).

Build the binary from a scratch worktree of qte-platform, so your own checkout is not touched:

```
git -C ~/Dev/qte-platform fetch origin
git -C ~/Dev/qte-platform worktree add --detach /tmp/qp-gateway origin/main
cd /tmp/qp-gateway && go build -o /tmp/qte-gateway ./gateway/cmd/qte-gateway
cd ~/Dev/qte-sdk && QTE_GATEWAY_BIN=/tmp/qte-gateway python -m pytest -q tests/test_gateway_conformance.py tests/test_handover.py
git -C ~/Dev/qte-platform worktree remove --force /tmp/qp-gateway
```

What runs depends on the binary's stage, read from its `-h` output:

- Stage 1 (no `-link-credential-file` flag): the section-0 checks only. A close from the exchange reaches the client with the same code and reason, an upgrade with no upstream is refused with HTTP 503, heartbeats pass through both ways, and after a SIGTERM restart the client sees 1006, redials and resumes. The handover scenarios skip, each with the reason.
- Stage 2: everything, including the handover outcomes of section 3 step 7 (a completed switch, a late promotion, an abort at drain, confirm or promote, and no exchange confirmed live).

To check a binary you expect to be stage 2, also set `QTE_GATEWAY_REQUIRE_STAGE_2=1`: the handover scenarios then fail on a stage-1 binary instead of skipping.

The tests start the binary themselves with every flag it needs (`gateway_command` in `tests/gateway_target.py` holds them all, in one place). They write a throwaway link credential into a temporary folder and give the gateway a random upstream header, so no real secret is used or needed. The gateway's own log for a failed run is quoted in the failure.

Tests marked fake-only (they check the fake gateway itself, or a contract break a real gateway cannot be made to show) skip in binary mode. The 45 s silence close (4000) is not run: it takes 45 s and needs a stage-2 gateway.

The switch command (`SWITCH_ROUTE`, its replies in `SWITCH_RESULTS`) and the stage-2 flag names are the ones qte-core set for qte-platform G3 (#2764), to go into link protocol v0.13; if they change, change `tests/gateway_target.py` only.
