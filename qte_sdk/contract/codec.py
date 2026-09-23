"""JSON codec for the QTE wire contract.

The wire is JSON in the canonical protobuf JSON mapping: proto field
names are kept as written, 64-bit integers travel as decimal strings, and unknown fields
are ignored so the SDK keeps working when the contract adds fields.

Enum values travel as names. A name this SDK does not know, for example one added by a
newer contract, is tolerated like an unknown field: the typed field keeps its zero value
(`..._UNSPECIFIED`). `unknown_enum_names` recovers what the exchange actually sent. An
unknown numeric value needs no help: it is kept in the typed field as sent.
"""

import json
from typing import Any, NamedTuple, TypeVar

from google.protobuf import json_format
from google.protobuf.descriptor import Descriptor, FieldDescriptor
from google.protobuf.message import Message

from qte_sdk.contract.v1.envelope_pb2 import Envelope

M = TypeVar("M", bound=Message)


def to_dict(msg: Message) -> dict[str, Any]:
    # A no-presence field at its zero value still carries meaning (remaining_size 0 means
    # filled, an empty ladder means no wall), so it must always be printed.
    return json_format.MessageToDict(
        msg,
        preserving_proto_field_name=True,
        always_print_fields_with_no_presence=True,
    )


def from_dict(data: dict[str, Any], cls: type[M]) -> M:
    return json_format.ParseDict(data, cls(), ignore_unknown_fields=True)


def to_json(msg: Message) -> str:
    return json.dumps(to_dict(msg), separators=(",", ":"))


def from_json(text: str | bytes, cls: type[M]) -> M:
    return from_dict(json.loads(text), cls)


class Decoded(NamedTuple):
    envelope: Envelope
    payload: dict[str, Any]


def encode(version: str, type_: str, payload: Message) -> str:
    # The envelope's payload field is a google.protobuf.Struct, which stores every number
    # as a double; building the JSON directly keeps int32 fields as JSON integers.
    data = to_dict(Envelope(version=version, type=type_))
    data["payload"] = to_dict(payload)
    return json.dumps(data, separators=(",", ":"))


def decode(text: str | bytes) -> Decoded:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("envelope is not a JSON object")
    if "payload" not in data:
        raise ValueError("envelope has no payload")
    payload = data["payload"]
    if not isinstance(payload, dict):
        raise ValueError("envelope payload is not a JSON object")
    head = {key: value for key, value in data.items() if key != "payload"}
    return Decoded(from_dict(head, Envelope), payload)


def unpack(payload: dict[str, Any], cls: type[M]) -> M:
    return from_dict(payload, cls)


def unknown_enum_names(payload: dict[str, Any], cls: type[Message]) -> dict[str, str]:
    """The enum names in `payload` that `cls` does not know, keyed by field path.

    Paths use proto field names, with list indexes in brackets, for example
    `{"reason_code": "BRAND_NEW"}` or `{"prints[0].kind": "BRAND_NEW"}`. A payload with
    nothing unknown gives an empty dict. Values of the wrong JSON shape are skipped here;
    decoding is what reports them.
    """
    found: dict[str, str] = {}
    _collect_unknown_enum_names(payload, cls.DESCRIPTOR, "", found)
    return found


def _collect_unknown_enum_names(
    data: dict[str, Any], descriptor: Descriptor, prefix: str, found: dict[str, str]
) -> None:
    # Looked up the way the protobuf JSON parser does: JSON name first, then proto name.
    by_json_name = {field.json_name: field for field in descriptor.fields}
    for key, value in data.items():
        field = by_json_name.get(key) or descriptor.fields_by_name.get(key)
        if field is None:
            continue
        path = prefix + field.name
        if field.is_repeated and isinstance(value, list):
            for index, item in enumerate(value):
                _check_value(item, field, f"{path}[{index}]", found)
        elif not field.is_repeated:
            _check_value(value, field, path, found)


def _check_value(value: Any, field: FieldDescriptor, path: str, found: dict[str, str]) -> None:
    if field.type == FieldDescriptor.TYPE_ENUM:
        if isinstance(value, str) and _is_unknown_name(value, field):
            found[path] = value
    elif (
        field.type == FieldDescriptor.TYPE_MESSAGE
        and isinstance(value, dict)
        and not field.message_type.full_name.startswith("google.protobuf.")
    ):
        _collect_unknown_enum_names(value, field.message_type, path + ".", found)


def _is_unknown_name(value: str, field: FieldDescriptor) -> bool:
    # The same test the protobuf JSON parser applies: a string that is neither a known
    # name nor an integer is an unknown name, which tolerant decoding drops.
    if value in field.enum_type.values_by_name:
        return False
    try:
        int(value)
    except ValueError:
        return True
    return False
