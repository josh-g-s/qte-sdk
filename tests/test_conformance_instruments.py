"""Regressions for conformance step 1a's check of an option entry, which run without an
exchange."""

import pytest
from test_conformance import assert_option_entry

from qte_sdk.contract.v1.session_pb2 import (
    CALL,
    OPTION,
    OPTION_RIGHT_UNSPECIFIED,
    PUT,
    InstrumentInfo,
    OptionTerms,
)
from qte_sdk.units import to_micros

SYMBOL = "SPY261120C00665000"


def entry(instrument: str = SYMBOL, **changes: object) -> InstrumentInfo:
    """An option entry whose terms agree with `instrument`, but for `changes`."""
    terms = {
        "underlying": "SPY",
        "expiry": "2026-11-20",
        "right": CALL,
        "strike": to_micros("665"),
        "multiplier": 100,
        **changes,
    }
    return InstrumentInfo(
        instrument=instrument,
        kind=OPTION,
        tick_size=10_000,
        lot_size=1,
        option=OptionTerms(**terms),
    )


def test_terms_that_agree_with_the_symbol_pass():
    assert_option_entry(entry())


def test_a_put_with_a_fractional_strike_passes():
    assert_option_entry(
        entry("GOOGL261120P00182500", underlying="GOOGL", right=PUT, strike=to_micros("182.5"))
    )


def test_no_option_terms_fail():
    info = entry()
    info.ClearField("option")
    with pytest.raises(AssertionError, match="carries no option terms"):
        assert_option_entry(info)


def test_empty_option_terms_fail():
    info = InstrumentInfo(instrument=SYMBOL, kind=OPTION)
    info.option.SetInParent()  # "option": {} on the wire
    assert info.HasField("option")
    with pytest.raises(AssertionError, match="has no underlying"):
        assert_option_entry(info)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("underlying", "", "has no underlying"),
        ("expiry", "", "has no expiry"),
        ("right", OPTION_RIGHT_UNSPECIFIED, "has no right"),
        ("strike", 0, "has no positive strike"),
        ("strike", -to_micros("665"), "has no positive strike"),
        ("multiplier", 0, "multiplier of 0, not 100"),
        ("multiplier", 10, "multiplier of 10, not 100"),
    ],
)
def test_a_missing_or_wrong_field_fails(field: str, value: object, message: str):
    with pytest.raises(AssertionError, match=message):
        assert_option_entry(entry(**{field: value}))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("underlying", "QQQ", "underlying is QQQ"),
        ("expiry", "2026-12-18", "expiry is 2026-12-18"),
        ("expiry", "20261120", "not an ISO 8601 date"),
        ("expiry", "2026-02-30", "not a real date"),
        ("right", PUT, "right does not match"),
        ("strike", to_micros("670"), "strike is 670000000"),
        # The symbol's digits read as micro-dollars, or as whole dollars, not thousandths.
        ("strike", 665_000, "strike is 665000 micro-dollars"),
        ("strike", 665_000_000_000, "strike is 665000000000"),
    ],
)
def test_terms_that_disagree_with_the_symbol_fail(field: str, value: object, message: str):
    with pytest.raises(AssertionError, match=message):
        assert_option_entry(entry(**{field: value}))


@pytest.mark.parametrize("instrument", ["SPY   261120C00665000", "SPY261120X00665000", "SPY"])
def test_an_id_not_in_unpadded_occ_form_fails(instrument: str):
    with pytest.raises(AssertionError, match="not an unpadded OCC option symbol"):
        assert_option_entry(entry(instrument))
