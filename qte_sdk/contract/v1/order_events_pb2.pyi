from qte_sdk.contract.v1 import common_pb2 as _common_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
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
    __slots__ = ("strat_id", "instrument", "side", "price", "state", "remaining_size", "stale_since", "timestamp", "old_price")
    STRAT_ID_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    SIDE_FIELD_NUMBER: _ClassVar[int]
    PRICE_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    REMAINING_SIZE_FIELD_NUMBER: _ClassVar[int]
    STALE_SINCE_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    OLD_PRICE_FIELD_NUMBER: _ClassVar[int]
    strat_id: str
    instrument: str
    side: _common_pb2.Side
    price: int
    state: _common_pb2.OrderLifecycleState
    remaining_size: int
    stale_since: int
    timestamp: int
    old_price: int
    def __init__(self, strat_id: _Optional[str] = ..., instrument: _Optional[str] = ..., side: _Optional[_Union[_common_pb2.Side, str]] = ..., price: _Optional[int] = ..., state: _Optional[_Union[_common_pb2.OrderLifecycleState, str]] = ..., remaining_size: _Optional[int] = ..., stale_since: _Optional[int] = ..., timestamp: _Optional[int] = ..., old_price: _Optional[int] = ...) -> None: ...

class LimitUtilisation(_message.Message):
    __slots__ = ("kind", "scope", "used", "cap")
    KIND_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    USED_FIELD_NUMBER: _ClassVar[int]
    CAP_FIELD_NUMBER: _ClassVar[int]
    kind: _common_pb2.LimitKind
    scope: str
    used: int
    cap: int
    def __init__(self, kind: _Optional[_Union[_common_pb2.LimitKind, str]] = ..., scope: _Optional[str] = ..., used: _Optional[int] = ..., cap: _Optional[int] = ...) -> None: ...

class AccountSummary(_message.Message):
    __slots__ = ("equity", "cash", "previous_close_equity", "daily_pnl", "loss_warning_amount", "loss_halt_amount", "loss_level", "limits", "in_cure", "cure_deadline", "cure_paused", "timestamp")
    EQUITY_FIELD_NUMBER: _ClassVar[int]
    CASH_FIELD_NUMBER: _ClassVar[int]
    PREVIOUS_CLOSE_EQUITY_FIELD_NUMBER: _ClassVar[int]
    DAILY_PNL_FIELD_NUMBER: _ClassVar[int]
    LOSS_WARNING_AMOUNT_FIELD_NUMBER: _ClassVar[int]
    LOSS_HALT_AMOUNT_FIELD_NUMBER: _ClassVar[int]
    LOSS_LEVEL_FIELD_NUMBER: _ClassVar[int]
    LIMITS_FIELD_NUMBER: _ClassVar[int]
    IN_CURE_FIELD_NUMBER: _ClassVar[int]
    CURE_DEADLINE_FIELD_NUMBER: _ClassVar[int]
    CURE_PAUSED_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    equity: int
    cash: int
    previous_close_equity: int
    daily_pnl: int
    loss_warning_amount: int
    loss_halt_amount: int
    loss_level: _common_pb2.LossLevel
    limits: _containers.RepeatedCompositeFieldContainer[LimitUtilisation]
    in_cure: bool
    cure_deadline: int
    cure_paused: bool
    timestamp: int
    def __init__(self, equity: _Optional[int] = ..., cash: _Optional[int] = ..., previous_close_equity: _Optional[int] = ..., daily_pnl: _Optional[int] = ..., loss_warning_amount: _Optional[int] = ..., loss_halt_amount: _Optional[int] = ..., loss_level: _Optional[_Union[_common_pb2.LossLevel, str]] = ..., limits: _Optional[_Iterable[_Union[LimitUtilisation, _Mapping]]] = ..., in_cure: bool = ..., cure_deadline: _Optional[int] = ..., cure_paused: bool = ..., timestamp: _Optional[int] = ...) -> None: ...

class ObligationState(_message.Message):
    __slots__ = ("entries", "timestamp")
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    entries: _containers.RepeatedCompositeFieldContainer[ObligationEntry]
    timestamp: int
    def __init__(self, entries: _Optional[_Iterable[_Union[ObligationEntry, _Mapping]]] = ..., timestamp: _Optional[int] = ...) -> None: ...

class ObligationEntry(_message.Message):
    __slots__ = ("instrument", "eligible", "qualifying", "spread_compliant", "size_compliant", "session_qualifying_seconds", "session_eligible_seconds", "in_tier_m", "tier_m_qualifying_seconds", "tier_m_eligible_seconds")
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    ELIGIBLE_FIELD_NUMBER: _ClassVar[int]
    QUALIFYING_FIELD_NUMBER: _ClassVar[int]
    SPREAD_COMPLIANT_FIELD_NUMBER: _ClassVar[int]
    SIZE_COMPLIANT_FIELD_NUMBER: _ClassVar[int]
    SESSION_QUALIFYING_SECONDS_FIELD_NUMBER: _ClassVar[int]
    SESSION_ELIGIBLE_SECONDS_FIELD_NUMBER: _ClassVar[int]
    IN_TIER_M_FIELD_NUMBER: _ClassVar[int]
    TIER_M_QUALIFYING_SECONDS_FIELD_NUMBER: _ClassVar[int]
    TIER_M_ELIGIBLE_SECONDS_FIELD_NUMBER: _ClassVar[int]
    instrument: str
    eligible: bool
    qualifying: bool
    spread_compliant: bool
    size_compliant: bool
    session_qualifying_seconds: int
    session_eligible_seconds: int
    in_tier_m: bool
    tier_m_qualifying_seconds: int
    tier_m_eligible_seconds: int
    def __init__(self, instrument: _Optional[str] = ..., eligible: bool = ..., qualifying: bool = ..., spread_compliant: bool = ..., size_compliant: bool = ..., session_qualifying_seconds: _Optional[int] = ..., session_eligible_seconds: _Optional[int] = ..., in_tier_m: bool = ..., tier_m_qualifying_seconds: _Optional[int] = ..., tier_m_eligible_seconds: _Optional[int] = ...) -> None: ...

class RiskNotice(_message.Message):
    __slots__ = ("kind", "timestamp", "cure_deadline", "breached_limits")
    KIND_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    CURE_DEADLINE_FIELD_NUMBER: _ClassVar[int]
    BREACHED_LIMITS_FIELD_NUMBER: _ClassVar[int]
    kind: _common_pb2.RiskNoticeKind
    timestamp: int
    cure_deadline: int
    breached_limits: _containers.RepeatedCompositeFieldContainer[LimitUtilisation]
    def __init__(self, kind: _Optional[_Union[_common_pb2.RiskNoticeKind, str]] = ..., timestamp: _Optional[int] = ..., cure_deadline: _Optional[int] = ..., breached_limits: _Optional[_Iterable[_Union[LimitUtilisation, _Mapping]]] = ...) -> None: ...

class PositionValue(_message.Message):
    __slots__ = ("instrument", "quantity", "price")
    INSTRUMENT_FIELD_NUMBER: _ClassVar[int]
    QUANTITY_FIELD_NUMBER: _ClassVar[int]
    PRICE_FIELD_NUMBER: _ClassVar[int]
    instrument: str
    quantity: int
    price: int
    def __init__(self, instrument: _Optional[str] = ..., quantity: _Optional[int] = ..., price: _Optional[int] = ...) -> None: ...

class AccountState(_message.Message):
    __slots__ = ("request_ref", "summary", "positions", "valuation_basis", "session_date", "as_of", "cash", "as_of_report_seq")
    REQUEST_REF_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    POSITIONS_FIELD_NUMBER: _ClassVar[int]
    VALUATION_BASIS_FIELD_NUMBER: _ClassVar[int]
    SESSION_DATE_FIELD_NUMBER: _ClassVar[int]
    AS_OF_FIELD_NUMBER: _ClassVar[int]
    CASH_FIELD_NUMBER: _ClassVar[int]
    AS_OF_REPORT_SEQ_FIELD_NUMBER: _ClassVar[int]
    request_ref: str
    summary: AccountSummary
    positions: _containers.RepeatedCompositeFieldContainer[PositionValue]
    valuation_basis: _common_pb2.ValuationBasis
    session_date: str
    as_of: int
    cash: int
    as_of_report_seq: int
    def __init__(self, request_ref: _Optional[str] = ..., summary: _Optional[_Union[AccountSummary, _Mapping]] = ..., positions: _Optional[_Iterable[_Union[PositionValue, _Mapping]]] = ..., valuation_basis: _Optional[_Union[_common_pb2.ValuationBasis, str]] = ..., session_date: _Optional[str] = ..., as_of: _Optional[int] = ..., cash: _Optional[int] = ..., as_of_report_seq: _Optional[int] = ...) -> None: ...
