import json

import pytest

from qte_sdk.contract import codec
from qte_sdk.contract.v1.common_pb2 import BUY, LIMIT, TEAM, ReasonCodes
from qte_sdk.contract.v1.market_data_pb2 import Book, Trades
from qte_sdk.contract.v1.order_entry_pb2 import NewOrder
from qte_sdk.contract.v1.order_events_pb2 import Execution, Reject
from qte_sdk.contract.v1.session_pb2 import Resume

BIG = 9_007_199_254_740_993  # 2**53 + 1: not exactly representable as a float


def new_order() -> NewOrder:
    return NewOrder(
        request_ref="r-1",
        strat_id="strat-a",
        instrument="AAPL",
        side=BUY,
        order_type=LIMIT,
        price=BIG,
        size=100,
    )


def test_int64_fields_travel_as_decimal_strings_with_proto_field_names():
    data = json.loads(codec.to_json(new_order()))
    assert data["price"] == str(BIG)
    assert data["size"] == "100"
    assert data["strat_id"] == "strat-a"
    assert data["side"] == "BUY"


def test_typed_payload_round_trips_through_an_envelope_exactly():
    wire = codec.encode("0.x", "new", new_order())

    decoded = codec.decode(wire)
    assert decoded.envelope.version == "0.x"
    assert decoded.envelope.type == "new"
    assert codec.unpack(decoded.payload, NewOrder) == new_order()


def test_int32_payload_fields_stay_json_integers():
    wire = codec.encode("0.x", "resume", Resume(session_id="s-1", last_seq_received=42))
    value = json.loads(wire)["payload"]["last_seq_received"]
    assert value == 42
    assert type(value) is int


def test_unknown_fields_are_ignored_at_top_level_and_inside_the_payload():
    data = json.loads(codec.encode("0.x", "new", new_order()))
    data["field_from_a_newer_contract"] = 1
    data["payload"]["another_new_field"] = {"nested": True}

    decoded = codec.decode(json.dumps(data))
    assert codec.unpack(decoded.payload, NewOrder) == new_order()


def test_no_presence_zero_values_are_still_printed():
    fill = Execution(exec_id="e-1", origin=TEAM, instrument="AAPL", remaining_size=0)
    assert json.loads(codec.to_json(fill))["remaining_size"] == "0"

    book = json.loads(codec.to_json(Book(instrument="AAPL")))
    assert book["bid_levels"] == []
    assert book["ask_levels"] == []


def test_explicit_presence_field_is_omitted_when_unset():
    order = new_order()
    order.ClearField("price")
    assert "price" not in json.loads(codec.to_json(order))


@pytest.mark.parametrize("text", ['["not", "an", "object"]', '{"type": "new", "payload": 3}'])
def test_malformed_envelopes_are_rejected(text):
    with pytest.raises(ValueError):
        codec.decode(text)


def test_generated_messages_pickle():
    import pickle

    assert pickle.loads(pickle.dumps(new_order())) == new_order()


def test_an_unknown_enum_name_decodes_to_zero_and_is_reported():
    payload = {"reason_code": "BRAND_NEW_REASON", "reason_detail": "d"}
    reject = codec.unpack(payload, Reject)
    assert reject.reason_code == ReasonCodes.REASON_CODE_UNSPECIFIED
    assert reject.reason_detail == "d"
    assert codec.unknown_enum_names(payload, Reject) == {"reason_code": "BRAND_NEW_REASON"}


@pytest.mark.parametrize("code", [1999, "1999"])
def test_an_unknown_numeric_enum_value_is_kept_and_not_reported(code):
    payload = {"reason_code": code}
    assert codec.unpack(payload, Reject).reason_code == 1999
    assert codec.unknown_enum_names(payload, Reject) == {}


def test_known_names_and_unknown_fields_are_not_reported():
    payload = {"reason_code": "MARKET_CLOSED", "a_field_from_a_newer_contract": "BRAND_NEW"}
    assert codec.unknown_enum_names(payload, Reject) == {}


def test_unknown_enum_names_inside_repeated_messages_are_reported_by_path():
    payload = {
        "instrument": "AAPL",
        "prints": [
            {"aggressor_side": "BUY", "kind": "STUDENT_TO_WALL"},
            {"aggressorSide": "BRAND_NEW_SIDE", "kind": "BRAND_NEW_KIND"},
        ],
    }
    trades = codec.unpack(payload, Trades)
    assert trades.prints[1].kind == 0
    assert codec.unknown_enum_names(payload, Trades) == {
        "prints[1].aggressor_side": "BRAND_NEW_SIDE",
        "prints[1].kind": "BRAND_NEW_KIND",
    }


@pytest.mark.parametrize(
    "payload",
    [{"reason_code": {"not": "a name"}}, {"reason_code": None}, {"reason_code": ["X"]}],
)
def test_values_of_the_wrong_shape_are_left_to_decoding(payload):
    assert codec.unknown_enum_names(payload, Reject) == {}


def test_a_non_list_repeated_field_is_left_to_decoding():
    assert codec.unknown_enum_names({"prints": {"kind": "BRAND_NEW"}}, Trades) == {}
