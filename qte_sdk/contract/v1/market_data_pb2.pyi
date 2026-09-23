from qte_sdk.contract.v1 import common_pb2 as _common_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class InstrumentCondition(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    INSTRUMENT_CONDITION_UNSPECIFIED: _ClassVar[InstrumentCondition]
    LIVE: _ClassVar[InstrumentCondition]
    ONE_SIDED: _ClassVar[InstrumentCondition]
    EMPTY: _ClassVar[InstrumentCondition]
    FROZEN: _ClassVar[InstrumentCondition]
    REFERENCE_UNAVAILABLE: _ClassVar[InstrumentCondition]
    DISABLED: _ClassVar[InstrumentCondition]
INSTRUMENT_CONDITION_UNSPECIFIED: InstrumentCondition
LIVE: InstrumentCondition
ONE_SIDED: InstrumentCondition
EMPTY: InstrumentCondition
FROZEN: InstrumentCondition
REFERENCE_UNAVAILABLE: InstrumentCondition
DISABLED: InstrumentCondition

class WallLevel(_message.Message):
    __slots__ = ("price", "size")
    PRICE_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    price: int
    size: int
    def __init__(self, price: _Optional[int] = ..., size: _Optional[int] = ...) -> None: ...

class StudentLevel(_message.Message):
    __slots__ = ("price", "size")
    PRICE_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    price: int
    size: int
    def __init__(self, price: _Optional[int] = ..., size: _Optional[int] = ...) -> None: ...

class Book(_message.Message):
    __slots__ = ("instrument", "grid_time", "bid_levels", "ask_levels", "student_bid_levels", "student_ask_levels", "condition")
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    GRID_TIME_FIELD_NUMBER: _ClassVar[int]
    BID_LEVELS_FIELD_NUMBER: _ClassVar[int]
    ASK_LEVELS_FIELD_NUMBER: _ClassVar[int]
    STUDENT_BID_LEVELS_FIELD_NUMBER: _ClassVar[int]
    STUDENT_ASK_LEVELS_FIELD_NUMBER: _ClassVar[int]
    CONDITION_FIELD_NUMBER: _ClassVar[int]
    instrument: str
    grid_time: int
    bid_levels: _containers.RepeatedCompositeFieldContainer[WallLevel]
    ask_levels: _containers.RepeatedCompositeFieldContainer[WallLevel]
    student_bid_levels: _containers.RepeatedCompositeFieldContainer[StudentLevel]
    student_ask_levels: _containers.RepeatedCompositeFieldContainer[StudentLevel]
    condition: InstrumentCondition
    def __init__(self, instrument: _Optional[str] = ..., grid_time: _Optional[int] = ..., bid_levels: _Optional[_Iterable[_Union[WallLevel, _Mapping]]] = ..., ask_levels: _Optional[_Iterable[_Union[WallLevel, _Mapping]]] = ..., student_bid_levels: _Optional[_Iterable[_Union[StudentLevel, _Mapping]]] = ..., student_ask_levels: _Optional[_Iterable[_Union[StudentLevel, _Mapping]]] = ..., condition: _Optional[_Union[InstrumentCondition, str]] = ...) -> None: ...

class TapePrint(_message.Message):
    __slots__ = ("price", "size", "aggressor_side", "timestamp", "kind")
    PRICE_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    AGGRESSOR_SIDE_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    price: int
    size: int
    aggressor_side: _common_pb2.Side
    timestamp: int
    kind: _common_pb2.MatchKind
    def __init__(self, price: _Optional[int] = ..., size: _Optional[int] = ..., aggressor_side: _Optional[_Union[_common_pb2.Side, str]] = ..., timestamp: _Optional[int] = ..., kind: _Optional[_Union[_common_pb2.MatchKind, str]] = ...) -> None: ...

class Trades(_message.Message):
    __slots__ = ("instrument", "grid_time", "prints")
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    GRID_TIME_FIELD_NUMBER: _ClassVar[int]
    PRINTS_FIELD_NUMBER: _ClassVar[int]
    instrument: str
    grid_time: int
    prints: _containers.RepeatedCompositeFieldContainer[TapePrint]
    def __init__(self, instrument: _Optional[str] = ..., grid_time: _Optional[int] = ..., prints: _Optional[_Iterable[_Union[TapePrint, _Mapping]]] = ...) -> None: ...

class Mark(_message.Message):
    __slots__ = ("instrument", "sampled_at", "value", "condition")
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    SAMPLED_AT_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    CONDITION_FIELD_NUMBER: _ClassVar[int]
    instrument: str
    sampled_at: int
    value: int
    condition: InstrumentCondition
    def __init__(self, instrument: _Optional[str] = ..., sampled_at: _Optional[int] = ..., value: _Optional[int] = ..., condition: _Optional[_Union[InstrumentCondition, str]] = ...) -> None: ...

class SessionState(_message.Message):
    __slots__ = ("state", "session_date", "open_time", "close_time", "grid_time", "outage_active")
    STATE_FIELD_NUMBER: _ClassVar[int]
    SESSION_DATE_FIELD_NUMBER: _ClassVar[int]
    OPEN_TIME_FIELD_NUMBER: _ClassVar[int]
    CLOSE_TIME_FIELD_NUMBER: _ClassVar[int]
    GRID_TIME_FIELD_NUMBER: _ClassVar[int]
    OUTAGE_ACTIVE_FIELD_NUMBER: _ClassVar[int]
    state: _common_pb2.MarketSessionPhase
    session_date: str
    open_time: int
    close_time: int
    grid_time: int
    outage_active: bool
    def __init__(self, state: _Optional[_Union[_common_pb2.MarketSessionPhase, str]] = ..., session_date: _Optional[str] = ..., open_time: _Optional[int] = ..., close_time: _Optional[int] = ..., grid_time: _Optional[int] = ..., outage_active: bool = ...) -> None: ...
