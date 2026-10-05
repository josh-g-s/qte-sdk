from qte_sdk.contract.v1 import common_pb2 as _common_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class InstrumentKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    INSTRUMENT_KIND_UNSPECIFIED: _ClassVar[InstrumentKind]
    EQUITY: _ClassVar[InstrumentKind]
    OPTION: _ClassVar[InstrumentKind]

class InstrumentStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    INSTRUMENT_STATUS_UNSPECIFIED: _ClassVar[InstrumentStatus]
    INSTRUMENT_TRADING: _ClassVar[InstrumentStatus]
    INSTRUMENT_DISABLED: _ClassVar[InstrumentStatus]
    INSTRUMENT_REDUCING_ONLY: _ClassVar[InstrumentStatus]

class OptionRight(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    OPTION_RIGHT_UNSPECIFIED: _ClassVar[OptionRight]
    CALL: _ClassVar[OptionRight]
    PUT: _ClassVar[OptionRight]
INSTRUMENT_KIND_UNSPECIFIED: InstrumentKind
EQUITY: InstrumentKind
OPTION: InstrumentKind
INSTRUMENT_STATUS_UNSPECIFIED: InstrumentStatus
INSTRUMENT_TRADING: InstrumentStatus
INSTRUMENT_DISABLED: InstrumentStatus
INSTRUMENT_REDUCING_ONLY: InstrumentStatus
OPTION_RIGHT_UNSPECIFIED: OptionRight
CALL: OptionRight
PUT: OptionRight

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

class CalendarSession(_message.Message):
    __slots__ = ("session_date", "open_time", "close_time", "early_close")
    SESSION_DATE_FIELD_NUMBER: _ClassVar[int]
    OPEN_TIME_FIELD_NUMBER: _ClassVar[int]
    CLOSE_TIME_FIELD_NUMBER: _ClassVar[int]
    EARLY_CLOSE_FIELD_NUMBER: _ClassVar[int]
    session_date: str
    open_time: int
    close_time: int
    early_close: bool
    def __init__(self, session_date: _Optional[str] = ..., open_time: _Optional[int] = ..., close_time: _Optional[int] = ..., early_close: bool = ...) -> None: ...

class Holiday(_message.Message):
    __slots__ = ("date", "name")
    DATE_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    date: str
    name: str
    def __init__(self, date: _Optional[str] = ..., name: _Optional[str] = ...) -> None: ...

class Calendar(_message.Message):
    __slots__ = ("term_first_session", "term_last_session", "sessions", "holidays", "next_open", "term_start", "term_end")
    TERM_FIRST_SESSION_FIELD_NUMBER: _ClassVar[int]
    TERM_LAST_SESSION_FIELD_NUMBER: _ClassVar[int]
    SESSIONS_FIELD_NUMBER: _ClassVar[int]
    HOLIDAYS_FIELD_NUMBER: _ClassVar[int]
    NEXT_OPEN_FIELD_NUMBER: _ClassVar[int]
    TERM_START_FIELD_NUMBER: _ClassVar[int]
    TERM_END_FIELD_NUMBER: _ClassVar[int]
    term_first_session: str
    term_last_session: str
    sessions: _containers.RepeatedCompositeFieldContainer[CalendarSession]
    holidays: _containers.RepeatedCompositeFieldContainer[Holiday]
    next_open: int
    term_start: str
    term_end: str
    def __init__(self, term_first_session: _Optional[str] = ..., term_last_session: _Optional[str] = ..., sessions: _Optional[_Iterable[_Union[CalendarSession, _Mapping]]] = ..., holidays: _Optional[_Iterable[_Union[Holiday, _Mapping]]] = ..., next_open: _Optional[int] = ..., term_start: _Optional[str] = ..., term_end: _Optional[str] = ...) -> None: ...

class OptionTerms(_message.Message):
    __slots__ = ("underlying", "expiry", "right", "strike", "multiplier")
    UNDERLYING_FIELD_NUMBER: _ClassVar[int]
    EXPIRY_FIELD_NUMBER: _ClassVar[int]
    RIGHT_FIELD_NUMBER: _ClassVar[int]
    STRIKE_FIELD_NUMBER: _ClassVar[int]
    MULTIPLIER_FIELD_NUMBER: _ClassVar[int]
    underlying: str
    expiry: str
    right: OptionRight
    strike: int
    multiplier: int
    def __init__(self, underlying: _Optional[str] = ..., expiry: _Optional[str] = ..., right: _Optional[_Union[OptionRight, str]] = ..., strike: _Optional[int] = ..., multiplier: _Optional[int] = ...) -> None: ...

class InstrumentInfo(_message.Message):
    __slots__ = ("instrument", "display_name", "kind", "tick_size", "lot_size", "status", "tradable", "option")
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    TICK_SIZE_FIELD_NUMBER: _ClassVar[int]
    LOT_SIZE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    TRADABLE_FIELD_NUMBER: _ClassVar[int]
    OPTION_FIELD_NUMBER: _ClassVar[int]
    instrument: str
    display_name: str
    kind: InstrumentKind
    tick_size: int
    lot_size: int
    status: InstrumentStatus
    tradable: bool
    option: OptionTerms
    def __init__(self, instrument: _Optional[str] = ..., display_name: _Optional[str] = ..., kind: _Optional[_Union[InstrumentKind, str]] = ..., tick_size: _Optional[int] = ..., lot_size: _Optional[int] = ..., status: _Optional[_Union[InstrumentStatus, str]] = ..., tradable: bool = ..., option: _Optional[_Union[OptionTerms, _Mapping]] = ...) -> None: ...

class OptionUnderlying(_message.Message):
    __slots__ = ("underlying", "strike_increment", "contracts")
    UNDERLYING_FIELD_NUMBER: _ClassVar[int]
    STRIKE_INCREMENT_FIELD_NUMBER: _ClassVar[int]
    CONTRACTS_FIELD_NUMBER: _ClassVar[int]
    underlying: str
    strike_increment: int
    contracts: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, underlying: _Optional[str] = ..., strike_increment: _Optional[int] = ..., contracts: _Optional[_Iterable[str]] = ...) -> None: ...

class Instruments(_message.Message):
    __slots__ = ("instruments", "option_underlyings")
    INSTRUMENTS_FIELD_NUMBER: _ClassVar[int]
    OPTION_UNDERLYINGS_FIELD_NUMBER: _ClassVar[int]
    instruments: _containers.RepeatedCompositeFieldContainer[InstrumentInfo]
    option_underlyings: _containers.RepeatedCompositeFieldContainer[OptionUnderlying]
    def __init__(self, instruments: _Optional[_Iterable[_Union[InstrumentInfo, _Mapping]]] = ..., option_underlyings: _Optional[_Iterable[_Union[OptionUnderlying, _Mapping]]] = ...) -> None: ...

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
    __slots__ = ("last_report_seq",)
    LAST_REPORT_SEQ_FIELD_NUMBER: _ClassVar[int]
    last_report_seq: int
    def __init__(self, last_report_seq: _Optional[int] = ...) -> None: ...

class ResumeAck(_message.Message):
    __slots__ = ("replayed", "as_of_report_seq", "snapshot_count")
    REPLAYED_FIELD_NUMBER: _ClassVar[int]
    AS_OF_REPORT_SEQ_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOT_COUNT_FIELD_NUMBER: _ClassVar[int]
    replayed: bool
    as_of_report_seq: int
    snapshot_count: int
    def __init__(self, replayed: bool = ..., as_of_report_seq: _Optional[int] = ..., snapshot_count: _Optional[int] = ...) -> None: ...

class OrderSnapshot(_message.Message):
    __slots__ = ("strat_id", "instrument", "side", "price", "remaining_size", "timestamp")
    STRAT_ID_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    SIDE_FIELD_NUMBER: _ClassVar[int]
    PRICE_FIELD_NUMBER: _ClassVar[int]
    REMAINING_SIZE_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    strat_id: str
    instrument: str
    side: _common_pb2.Side
    price: int
    remaining_size: int
    timestamp: int
    def __init__(self, strat_id: _Optional[str] = ..., instrument: _Optional[str] = ..., side: _Optional[_Union[_common_pb2.Side, str]] = ..., price: _Optional[int] = ..., remaining_size: _Optional[int] = ..., timestamp: _Optional[int] = ...) -> None: ...

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

class AccountQuery(_message.Message):
    __slots__ = ("request_ref",)
    REQUEST_REF_FIELD_NUMBER: _ClassVar[int]
    request_ref: str
    def __init__(self, request_ref: _Optional[str] = ...) -> None: ...
