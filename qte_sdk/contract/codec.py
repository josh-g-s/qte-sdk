"""JSON codec for the QTE wire contract.

The wire is JSON in the canonical protobuf JSON mapping: proto field
names are kept as written, 64-bit integers travel as decimal strings, and unknown fields
are ignored so the SDK keeps working when the contract adds fields.
"""

import json
from typing import Any, NamedTuple, TypeVar

from google.protobuf import json_format
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
