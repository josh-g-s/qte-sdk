from qte_sdk.contract.v1 import common_pb2 as _common_pb2
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Accepted(_message.Message):
    __slots__ = ("request_ref", "request_type", "receipt_time", "release_time")
    REQUEST_REF_FIELD_NUMBER: _ClassVar[int]
    REQUEST_TYPE_FIELD_NUMBER: _ClassVar[int]
    RECEIPT_TIME_FIELD_NUMBER: _ClassVar[int]
    RELEASE_TIME_FIELD_NUMBER: _ClassVar[int]
    request_ref: str
    request_type: _common_pb2.RequestType
    receipt_time: int
    release_time: int
    def __init__(self, request_ref: _Optional[str] = ..., request_type: _Optional[_Union[_common_pb2.RequestType, str]] = ..., receipt_time: _Optional[int] = ..., release_time: _Optional[int] = ...) -> None: ...

class Reject(_message.Message):
    __slots__ = ("request_ref", "request_type", "reason_code", "reason_detail", "receipt_time", "instrument")
    REQUEST_REF_FIELD_NUMBER: _ClassVar[int]
    REQUEST_TYPE_FIELD_NUMBER: _ClassVar[int]
    REASON_CODE_FIELD_NUMBER: _ClassVar[int]
    REASON_DETAIL_FIELD_NUMBER: _ClassVar[int]
    RECEIPT_TIME_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    request_ref: str
    request_type: _common_pb2.RequestType
    reason_code: _common_pb2.ReasonCodes.ReasonCode
    reason_detail: str
    receipt_time: int
    instrument: str
    def __init__(self, request_ref: _Optional[str] = ..., request_type: _Optional[_Union[_common_pb2.RequestType, str]] = ..., reason_code: _Optional[_Union[_common_pb2.ReasonCodes.ReasonCode, str]] = ..., reason_detail: _Optional[str] = ..., receipt_time: _Optional[int] = ..., instrument: _Optional[str] = ...) -> None: ...

class Execution(_message.Message):
    __slots__ = ("exec_id", "origin", "strat_id", "instrument", "side", "order_price", "fill_price", "fill_size", "remaining_size", "fill_kind", "liquidity", "fee", "timestamp")
    EXEC_ID_FIELD_NUMBER: _ClassVar[int]
    ORIGIN_FIELD_NUMBER: _ClassVar[int]
    STRAT_ID_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    SIDE_FIELD_NUMBER: _ClassVar[int]
    ORDER_PRICE_FIELD_NUMBER: _ClassVar[int]
    FILL_PRICE_FIELD_NUMBER: _ClassVar[int]
    FILL_SIZE_FIELD_NUMBER: _ClassVar[int]
    REMAINING_SIZE_FIELD_NUMBER: _ClassVar[int]
    FILL_KIND_FIELD_NUMBER: _ClassVar[int]
    LIQUIDITY_FIELD_NUMBER: _ClassVar[int]
    FEE_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    exec_id: str
    origin: _common_pb2.Origin
    strat_id: str
    instrument: str
    side: _common_pb2.Side
    order_price: int
    fill_price: int
    fill_size: int
    remaining_size: int
    fill_kind: _common_pb2.MatchKind
    liquidity: _common_pb2.Liquidity
    fee: int
    timestamp: int
    def __init__(self, exec_id: _Optional[str] = ..., origin: _Optional[_Union[_common_pb2.Origin, str]] = ..., strat_id: _Optional[str] = ..., instrument: _Optional[str] = ..., side: _Optional[_Union[_common_pb2.Side, str]] = ..., order_price: _Optional[int] = ..., fill_price: _Optional[int] = ..., fill_size: _Optional[int] = ..., remaining_size: _Optional[int] = ..., fill_kind: _Optional[_Union[_common_pb2.MatchKind, str]] = ..., liquidity: _Optional[_Union[_common_pb2.Liquidity, str]] = ..., fee: _Optional[int] = ..., timestamp: _Optional[int] = ...) -> None: ...

class OrderCancelled(_message.Message):
    __slots__ = ("origin", "strat_id", "instrument", "side", "price", "cancelled_size", "reason_code", "request_ref", "timestamp")
    ORIGIN_FIELD_NUMBER: _ClassVar[int]
    STRAT_ID_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    SIDE_FIELD_NUMBER: _ClassVar[int]
    PRICE_FIELD_NUMBER: _ClassVar[int]
    CANCELLED_SIZE_FIELD_NUMBER: _ClassVar[int]
    REASON_CODE_FIELD_NUMBER: _ClassVar[int]
    REQUEST_REF_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    origin: _common_pb2.Origin
    strat_id: str
    instrument: str
    side: _common_pb2.Side
    price: int
    cancelled_size: int
    reason_code: _common_pb2.ReasonCodes.ReasonCode
    request_ref: str
    timestamp: int
    def __init__(self, origin: _Optional[_Union[_common_pb2.Origin, str]] = ..., strat_id: _Optional[str] = ..., instrument: _Optional[str] = ..., side: _Optional[_Union[_common_pb2.Side, str]] = ..., price: _Optional[int] = ..., cancelled_size: _Optional[int] = ..., reason_code: _Optional[_Union[_common_pb2.ReasonCodes.ReasonCode, str]] = ..., request_ref: _Optional[str] = ..., timestamp: _Optional[int] = ...) -> None: ...

class OrderState(_message.Message):
    __slots__ = ("strat_id", "instrument", "side", "price", "state", "remaining_size", "stale_since", "timestamp")
    STRAT_ID_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    SIDE_FIELD_NUMBER: _ClassVar[int]
    PRICE_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    REMAINING_SIZE_FIELD_NUMBER: _ClassVar[int]
    STALE_SINCE_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    strat_id: str
    instrument: str
    side: _common_pb2.Side
    price: int
    state: _common_pb2.OrderLifecycleState
    remaining_size: int
    stale_since: int
    timestamp: int
    def __init__(self, strat_id: _Optional[str] = ..., instrument: _Optional[str] = ..., side: _Optional[_Union[_common_pb2.Side, str]] = ..., price: _Optional[int] = ..., state: _Optional[_Union[_common_pb2.OrderLifecycleState, str]] = ..., remaining_size: _Optional[int] = ..., stale_since: _Optional[int] = ..., timestamp: _Optional[int] = ...) -> None: ...

class RiskNotice(_message.Message):
    __slots__ = ("kind", "timestamp")
    KIND_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    kind: _common_pb2.RiskNoticeKind
    timestamp: int
    def __init__(self, kind: _Optional[_Union[_common_pb2.RiskNoticeKind, str]] = ..., timestamp: _Optional[int] = ...) -> None: ...
