from qte_sdk.contract.v1 import common_pb2 as _common_pb2
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class NewOrder(_message.Message):
    __slots__ = ("request_ref", "strat_id", "instrument", "side", "order_type", "price", "size")
    REQUEST_REF_FIELD_NUMBER: _ClassVar[int]
    STRAT_ID_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    SIDE_FIELD_NUMBER: _ClassVar[int]
    ORDER_TYPE_FIELD_NUMBER: _ClassVar[int]
    PRICE_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    request_ref: str
    strat_id: str
    instrument: str
    side: _common_pb2.Side
    order_type: _common_pb2.OrderType
    price: int
    size: int
    def __init__(self, request_ref: _Optional[str] = ..., strat_id: _Optional[str] = ..., instrument: _Optional[str] = ..., side: _Optional[_Union[_common_pb2.Side, str]] = ..., order_type: _Optional[_Union[_common_pb2.OrderType, str]] = ..., price: _Optional[int] = ..., size: _Optional[int] = ...) -> None: ...

class CancelOrder(_message.Message):
    __slots__ = ("request_ref", "instrument", "side", "price")
    REQUEST_REF_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    SIDE_FIELD_NUMBER: _ClassVar[int]
    PRICE_FIELD_NUMBER: _ClassVar[int]
    request_ref: str
    instrument: str
    side: _common_pb2.Side
    price: int
    def __init__(self, request_ref: _Optional[str] = ..., instrument: _Optional[str] = ..., side: _Optional[_Union[_common_pb2.Side, str]] = ..., price: _Optional[int] = ...) -> None: ...

class AmendOrder(_message.Message):
    __slots__ = ("request_ref", "instrument", "side", "price", "new_price", "new_size")
    REQUEST_REF_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    SIDE_FIELD_NUMBER: _ClassVar[int]
    PRICE_FIELD_NUMBER: _ClassVar[int]
    NEW_PRICE_FIELD_NUMBER: _ClassVar[int]
    NEW_SIZE_FIELD_NUMBER: _ClassVar[int]
    request_ref: str
    instrument: str
    side: _common_pb2.Side
    price: int
    new_price: int
    new_size: int
    def __init__(self, request_ref: _Optional[str] = ..., instrument: _Optional[str] = ..., side: _Optional[_Union[_common_pb2.Side, str]] = ..., price: _Optional[int] = ..., new_price: _Optional[int] = ..., new_size: _Optional[int] = ...) -> None: ...

class MassCancel(_message.Message):
    __slots__ = ("request_ref",)
    REQUEST_REF_FIELD_NUMBER: _ClassVar[int]
    request_ref: str
    def __init__(self, request_ref: _Optional[str] = ...) -> None: ...
