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

class OptionTradingState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    OPTION_TRADING_STATE_UNSPECIFIED: _ClassVar[OptionTradingState]
    OPTION_TRADING: _ClassVar[OptionTradingState]
    OPTION_REDUCING_ONLY: _ClassVar[OptionTradingState]
    OPTION_SUSPENDED: _ClassVar[OptionTradingState]

class OptionWindowStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    OPTION_WINDOW_STATUS_UNSPECIFIED: _ClassVar[OptionWindowStatus]
    OPTION_WINDOW_COMPUTED: _ClassVar[OptionWindowStatus]
    OPTION_WINDOW_RETAINED: _ClassVar[OptionWindowStatus]

class OptionContractRole(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    OPTION_CONTRACT_ROLE_UNSPECIFIED: _ClassVar[OptionContractRole]
    OPTION_ROLE_ACTIVE: _ClassVar[OptionContractRole]
    OPTION_ROLE_OBLIGATED: _ClassVar[OptionContractRole]
    OPTION_ROLE_RETAINED: _ClassVar[OptionContractRole]

class OptionGreeksStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    OPTION_GREEKS_STATUS_UNSPECIFIED: _ClassVar[OptionGreeksStatus]
    OPTION_GREEKS_VALID: _ClassVar[OptionGreeksStatus]
    OPTION_GREEKS_UNAVAILABLE: _ClassVar[OptionGreeksStatus]
    OPTION_GREEKS_NONE: _ClassVar[OptionGreeksStatus]
INSTRUMENT_CONDITION_UNSPECIFIED: InstrumentCondition
LIVE: InstrumentCondition
ONE_SIDED: InstrumentCondition
EMPTY: InstrumentCondition
FROZEN: InstrumentCondition
REFERENCE_UNAVAILABLE: InstrumentCondition
DISABLED: InstrumentCondition
OPTION_TRADING_STATE_UNSPECIFIED: OptionTradingState
OPTION_TRADING: OptionTradingState
OPTION_REDUCING_ONLY: OptionTradingState
OPTION_SUSPENDED: OptionTradingState
OPTION_WINDOW_STATUS_UNSPECIFIED: OptionWindowStatus
OPTION_WINDOW_COMPUTED: OptionWindowStatus
OPTION_WINDOW_RETAINED: OptionWindowStatus
OPTION_CONTRACT_ROLE_UNSPECIFIED: OptionContractRole
OPTION_ROLE_ACTIVE: OptionContractRole
OPTION_ROLE_OBLIGATED: OptionContractRole
OPTION_ROLE_RETAINED: OptionContractRole
OPTION_GREEKS_STATUS_UNSPECIFIED: OptionGreeksStatus
OPTION_GREEKS_VALID: OptionGreeksStatus
OPTION_GREEKS_UNAVAILABLE: OptionGreeksStatus
OPTION_GREEKS_NONE: OptionGreeksStatus

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
    __slots__ = ("instrument", "grid_time", "bid_levels", "ask_levels", "student_bid_levels", "student_ask_levels", "condition", "trading_state")
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    GRID_TIME_FIELD_NUMBER: _ClassVar[int]
    BID_LEVELS_FIELD_NUMBER: _ClassVar[int]
    ASK_LEVELS_FIELD_NUMBER: _ClassVar[int]
    STUDENT_BID_LEVELS_FIELD_NUMBER: _ClassVar[int]
    STUDENT_ASK_LEVELS_FIELD_NUMBER: _ClassVar[int]
    CONDITION_FIELD_NUMBER: _ClassVar[int]
    TRADING_STATE_FIELD_NUMBER: _ClassVar[int]
    instrument: str
    grid_time: int
    bid_levels: _containers.RepeatedCompositeFieldContainer[WallLevel]
    ask_levels: _containers.RepeatedCompositeFieldContainer[WallLevel]
    student_bid_levels: _containers.RepeatedCompositeFieldContainer[StudentLevel]
    student_ask_levels: _containers.RepeatedCompositeFieldContainer[StudentLevel]
    condition: InstrumentCondition
    trading_state: OptionTradingState
    def __init__(self, instrument: _Optional[str] = ..., grid_time: _Optional[int] = ..., bid_levels: _Optional[_Iterable[_Union[WallLevel, _Mapping]]] = ..., ask_levels: _Optional[_Iterable[_Union[WallLevel, _Mapping]]] = ..., student_bid_levels: _Optional[_Iterable[_Union[StudentLevel, _Mapping]]] = ..., student_ask_levels: _Optional[_Iterable[_Union[StudentLevel, _Mapping]]] = ..., condition: _Optional[_Union[InstrumentCondition, str]] = ..., trading_state: _Optional[_Union[OptionTradingState, str]] = ...) -> None: ...

class TapePrint(_message.Message):
    __slots__ = ("price", "size", "aggressor_side", "timestamp", "kind", "feed_only")
    PRICE_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    AGGRESSOR_SIDE_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    FEED_ONLY_FIELD_NUMBER: _ClassVar[int]
    price: int
    size: int
    aggressor_side: _common_pb2.Side
    timestamp: int
    kind: _common_pb2.MatchKind
    feed_only: bool
    def __init__(self, price: _Optional[int] = ..., size: _Optional[int] = ..., aggressor_side: _Optional[_Union[_common_pb2.Side, str]] = ..., timestamp: _Optional[int] = ..., kind: _Optional[_Union[_common_pb2.MatchKind, str]] = ..., feed_only: bool = ...) -> None: ...

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
    __slots__ = ("state", "session_date", "open_time", "close_time", "grid_time", "outage_active", "next_session_date", "next_open_time", "next_close_time")
    STATE_FIELD_NUMBER: _ClassVar[int]
    SESSION_DATE_FIELD_NUMBER: _ClassVar[int]
    OPEN_TIME_FIELD_NUMBER: _ClassVar[int]
    CLOSE_TIME_FIELD_NUMBER: _ClassVar[int]
    GRID_TIME_FIELD_NUMBER: _ClassVar[int]
    OUTAGE_ACTIVE_FIELD_NUMBER: _ClassVar[int]
    NEXT_SESSION_DATE_FIELD_NUMBER: _ClassVar[int]
    NEXT_OPEN_TIME_FIELD_NUMBER: _ClassVar[int]
    NEXT_CLOSE_TIME_FIELD_NUMBER: _ClassVar[int]
    state: _common_pb2.MarketSessionPhase
    session_date: str
    open_time: int
    close_time: int
    grid_time: int
    outage_active: bool
    next_session_date: str
    next_open_time: int
    next_close_time: int
    def __init__(self, state: _Optional[_Union[_common_pb2.MarketSessionPhase, str]] = ..., session_date: _Optional[str] = ..., open_time: _Optional[int] = ..., close_time: _Optional[int] = ..., grid_time: _Optional[int] = ..., outage_active: bool = ..., next_session_date: _Optional[str] = ..., next_open_time: _Optional[int] = ..., next_close_time: _Optional[int] = ...) -> None: ...

class OfficialClose(_message.Message):
    __slots__ = ("instrument", "session_date", "value", "frozen")
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    SESSION_DATE_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    FROZEN_FIELD_NUMBER: _ClassVar[int]
    instrument: str
    session_date: str
    value: int
    frozen: bool
    def __init__(self, instrument: _Optional[str] = ..., session_date: _Optional[str] = ..., value: _Optional[int] = ..., frozen: bool = ...) -> None: ...

class OptionChainExpiry(_message.Message):
    __slots__ = ("underlying", "expiry", "window_status", "reducing_only", "contracts")
    UNDERLYING_FIELD_NUMBER: _ClassVar[int]
    EXPIRY_FIELD_NUMBER: _ClassVar[int]
    WINDOW_STATUS_FIELD_NUMBER: _ClassVar[int]
    REDUCING_ONLY_FIELD_NUMBER: _ClassVar[int]
    CONTRACTS_FIELD_NUMBER: _ClassVar[int]
    underlying: str
    expiry: str
    window_status: OptionWindowStatus
    reducing_only: bool
    contracts: _containers.RepeatedCompositeFieldContainer[OptionChainContract]
    def __init__(self, underlying: _Optional[str] = ..., expiry: _Optional[str] = ..., window_status: _Optional[_Union[OptionWindowStatus, str]] = ..., reducing_only: bool = ..., contracts: _Optional[_Iterable[_Union[OptionChainContract, _Mapping]]] = ...) -> None: ...

class OptionChainContract(_message.Message):
    __slots__ = ("instrument", "role")
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    instrument: str
    role: OptionContractRole
    def __init__(self, instrument: _Optional[str] = ..., role: _Optional[_Union[OptionContractRole, str]] = ...) -> None: ...

class OptionChain(_message.Message):
    __slots__ = ("session_date", "expiries")
    SESSION_DATE_FIELD_NUMBER: _ClassVar[int]
    EXPIRIES_FIELD_NUMBER: _ClassVar[int]
    session_date: str
    expiries: _containers.RepeatedCompositeFieldContainer[OptionChainExpiry]
    def __init__(self, session_date: _Optional[str] = ..., expiries: _Optional[_Iterable[_Union[OptionChainExpiry, _Mapping]]] = ...) -> None: ...

class OptionGreeks(_message.Message):
    __slots__ = ("instrument", "grid_time", "status", "calculated_at", "forward", "implied_vol", "delta", "gamma", "vega", "theta")
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    GRID_TIME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    CALCULATED_AT_FIELD_NUMBER: _ClassVar[int]
    FORWARD_FIELD_NUMBER: _ClassVar[int]
    IMPLIED_VOL_FIELD_NUMBER: _ClassVar[int]
    DELTA_FIELD_NUMBER: _ClassVar[int]
    GAMMA_FIELD_NUMBER: _ClassVar[int]
    VEGA_FIELD_NUMBER: _ClassVar[int]
    THETA_FIELD_NUMBER: _ClassVar[int]
    instrument: str
    grid_time: int
    status: OptionGreeksStatus
    calculated_at: int
    forward: int
    implied_vol: int
    delta: int
    gamma: int
    vega: int
    theta: int
    def __init__(self, instrument: _Optional[str] = ..., grid_time: _Optional[int] = ..., status: _Optional[_Union[OptionGreeksStatus, str]] = ..., calculated_at: _Optional[int] = ..., forward: _Optional[int] = ..., implied_vol: _Optional[int] = ..., delta: _Optional[int] = ..., gamma: _Optional[int] = ..., vega: _Optional[int] = ..., theta: _Optional[int] = ...) -> None: ...
