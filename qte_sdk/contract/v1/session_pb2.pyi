from qte_sdk.contract.v1 import common_pb2 as _common_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Auth(_message.Message):
    __slots__ = ("token",)
    TOKEN_FIELD_NUMBER: _ClassVar[int]
    token: str
    def __init__(self, token: _Optional[str] = ...) -> None: ...

class SessionAck(_message.Message):
    __slots__ = ("session_id", "team", "server_time", "contract_version", "unscored")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    TEAM_FIELD_NUMBER: _ClassVar[int]
    SERVER_TIME_FIELD_NUMBER: _ClassVar[int]
    CONTRACT_VERSION_FIELD_NUMBER: _ClassVar[int]
    UNSCORED_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    team: str
    server_time: int
    contract_version: str
    unscored: bool
    def __init__(self, session_id: _Optional[str] = ..., team: _Optional[str] = ..., server_time: _Optional[int] = ..., contract_version: _Optional[str] = ..., unscored: bool = ...) -> None: ...

class SessionReject(_message.Message):
    __slots__ = ("reason_code", "reason_detail")
    REASON_CODE_FIELD_NUMBER: _ClassVar[int]
    REASON_DETAIL_FIELD_NUMBER: _ClassVar[int]
    reason_code: _common_pb2.ReasonCodes.ReasonCode
    reason_detail: str
    def __init__(self, reason_code: _Optional[_Union[_common_pb2.ReasonCodes.ReasonCode, str]] = ..., reason_detail: _Optional[str] = ...) -> None: ...

class Heartbeat(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class Resume(_message.Message):
    __slots__ = ("session_id", "last_seq_received")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    LAST_SEQ_RECEIVED_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    last_seq_received: int
    def __init__(self, session_id: _Optional[str] = ..., last_seq_received: _Optional[int] = ...) -> None: ...

class OrderSnapshot(_message.Message):
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

class Subscribe(_message.Message):
    __slots__ = ("instruments",)
    INSTRUMENTS_FIELD_NUMBER: _ClassVar[int]
    instruments: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, instruments: _Optional[_Iterable[str]] = ...) -> None: ...

class Unsubscribe(_message.Message):
    __slots__ = ("instruments",)
    INSTRUMENTS_FIELD_NUMBER: _ClassVar[int]
    instruments: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, instruments: _Optional[_Iterable[str]] = ...) -> None: ...
