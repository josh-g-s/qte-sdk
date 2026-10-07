from qte_sdk.contract.v1 import common_pb2 as _common_pb2
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class NewOrder(_message.Message):
    __slots__ = ("request_ref", "strat_id", "instrument", "side", "order_type", "price", "size", "parent_ticket_id", "house_team")
    REQUEST_REF_FIELD_NUMBER: _ClassVar[int]
    STRAT_ID_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    SIDE_FIELD_NUMBER: _ClassVar[int]
    ORDER_TYPE_FIELD_NUMBER: _ClassVar[int]
    PRICE_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    PARENT_TICKET_ID_FIELD_NUMBER: _ClassVar[int]
    HOUSE_TEAM_FIELD_NUMBER: _ClassVar[int]
    request_ref: str
    strat_id: str
    instrument: str
    side: _common_pb2.Side
    order_type: _common_pb2.OrderType
    price: int
    size: int
    parent_ticket_id: str
    house_team: str
    def __init__(self, request_ref: _Optional[str] = ..., strat_id: _Optional[str] = ..., instrument: _Optional[str] = ..., side: _Optional[_Union[_common_pb2.Side, str]] = ..., order_type: _Optional[_Union[_common_pb2.OrderType, str]] = ..., price: _Optional[int] = ..., size: _Optional[int] = ..., parent_ticket_id: _Optional[str] = ..., house_team: _Optional[str] = ...) -> None: ...

class CancelOrder(_message.Message):
    __slots__ = ("request_ref", "instrument", "side", "price", "house_team")
    REQUEST_REF_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    SIDE_FIELD_NUMBER: _ClassVar[int]
    PRICE_FIELD_NUMBER: _ClassVar[int]
    HOUSE_TEAM_FIELD_NUMBER: _ClassVar[int]
    request_ref: str
    instrument: str
    side: _common_pb2.Side
    price: int
    house_team: str
    def __init__(self, request_ref: _Optional[str] = ..., instrument: _Optional[str] = ..., side: _Optional[_Union[_common_pb2.Side, str]] = ..., price: _Optional[int] = ..., house_team: _Optional[str] = ...) -> None: ...

class AmendOrder(_message.Message):
    __slots__ = ("request_ref", "instrument", "side", "price", "new_price", "new_size", "house_team")
    REQUEST_REF_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    SIDE_FIELD_NUMBER: _ClassVar[int]
    PRICE_FIELD_NUMBER: _ClassVar[int]
    NEW_PRICE_FIELD_NUMBER: _ClassVar[int]
    NEW_SIZE_FIELD_NUMBER: _ClassVar[int]
    HOUSE_TEAM_FIELD_NUMBER: _ClassVar[int]
    request_ref: str
    instrument: str
    side: _common_pb2.Side
    price: int
    new_price: int
    new_size: int
    house_team: str
    def __init__(self, request_ref: _Optional[str] = ..., instrument: _Optional[str] = ..., side: _Optional[_Union[_common_pb2.Side, str]] = ..., price: _Optional[int] = ..., new_price: _Optional[int] = ..., new_size: _Optional[int] = ..., house_team: _Optional[str] = ...) -> None: ...

class MassCancel(_message.Message):
    __slots__ = ("request_ref",)
    REQUEST_REF_FIELD_NUMBER: _ClassVar[int]
    request_ref: str
    def __init__(self, request_ref: _Optional[str] = ...) -> None: ...

class SubmitTicket(_message.Message):
    __slots__ = ("request_ref", "thesis_id", "thesis_version", "instrument", "side", "shares", "limit_price", "urgency", "notes", "replaces_ticket_id", "cure")
    REQUEST_REF_FIELD_NUMBER: _ClassVar[int]
    THESIS_ID_FIELD_NUMBER: _ClassVar[int]
    THESIS_VERSION_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    SIDE_FIELD_NUMBER: _ClassVar[int]
    SHARES_FIELD_NUMBER: _ClassVar[int]
    LIMIT_PRICE_FIELD_NUMBER: _ClassVar[int]
    URGENCY_FIELD_NUMBER: _ClassVar[int]
    NOTES_FIELD_NUMBER: _ClassVar[int]
    REPLACES_TICKET_ID_FIELD_NUMBER: _ClassVar[int]
    CURE_FIELD_NUMBER: _ClassVar[int]
    request_ref: str
    thesis_id: str
    thesis_version: int
    instrument: str
    side: _common_pb2.Side
    shares: int
    limit_price: int
    urgency: _common_pb2.TicketUrgency
    notes: str
    replaces_ticket_id: str
    cure: bool
    def __init__(self, request_ref: _Optional[str] = ..., thesis_id: _Optional[str] = ..., thesis_version: _Optional[int] = ..., instrument: _Optional[str] = ..., side: _Optional[_Union[_common_pb2.Side, str]] = ..., shares: _Optional[int] = ..., limit_price: _Optional[int] = ..., urgency: _Optional[_Union[_common_pb2.TicketUrgency, str]] = ..., notes: _Optional[str] = ..., replaces_ticket_id: _Optional[str] = ..., cure: bool = ...) -> None: ...

class CancelTicket(_message.Message):
    __slots__ = ("request_ref", "ticket_id")
    REQUEST_REF_FIELD_NUMBER: _ClassVar[int]
    TICKET_ID_FIELD_NUMBER: _ClassVar[int]
    request_ref: str
    ticket_id: str
    def __init__(self, request_ref: _Optional[str] = ..., ticket_id: _Optional[str] = ...) -> None: ...

class RaiseTicketUrgency(_message.Message):
    __slots__ = ("request_ref", "ticket_id", "urgency")
    REQUEST_REF_FIELD_NUMBER: _ClassVar[int]
    TICKET_ID_FIELD_NUMBER: _ClassVar[int]
    URGENCY_FIELD_NUMBER: _ClassVar[int]
    request_ref: str
    ticket_id: str
    urgency: _common_pb2.TicketUrgency
    def __init__(self, request_ref: _Optional[str] = ..., ticket_id: _Optional[str] = ..., urgency: _Optional[_Union[_common_pb2.TicketUrgency, str]] = ...) -> None: ...
