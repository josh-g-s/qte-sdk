from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar

DESCRIPTOR: _descriptor.FileDescriptor

class Side(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SIDE_UNSPECIFIED: _ClassVar[Side]
    BUY: _ClassVar[Side]
    SELL: _ClassVar[Side]

class OrderType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ORDER_TYPE_UNSPECIFIED: _ClassVar[OrderType]
    LIMIT: _ClassVar[OrderType]
    MARKET: _ClassVar[OrderType]

class RequestType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    REQUEST_TYPE_UNSPECIFIED: _ClassVar[RequestType]
    NEW: _ClassVar[RequestType]
    CANCEL: _ClassVar[RequestType]
    AMEND: _ClassVar[RequestType]
    MASS_CANCEL: _ClassVar[RequestType]
    SUBSCRIBE: _ClassVar[RequestType]
    UNSUBSCRIBE: _ClassVar[RequestType]

class Origin(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ORIGIN_UNSPECIFIED: _ClassVar[Origin]
    TEAM: _ClassVar[Origin]
    CURE_TRADE: _ClassVar[Origin]
    AUTO_FLATTEN: _ClassVar[Origin]

class Liquidity(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    LIQUIDITY_UNSPECIFIED: _ClassVar[Liquidity]
    MAKER: _ClassVar[Liquidity]
    TAKER: _ClassVar[Liquidity]

class MatchKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    MATCH_KIND_UNSPECIFIED: _ClassVar[MatchKind]
    STUDENT_TO_STUDENT: _ClassVar[MatchKind]
    STUDENT_TO_WALL: _ClassVar[MatchKind]
    TRADE_BASED_MATCH: _ClassVar[MatchKind]
    RESIDUAL: _ClassVar[MatchKind]

class OrderLifecycleState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ORDER_LIFECYCLE_STATE_UNSPECIFIED: _ClassVar[OrderLifecycleState]
    RESTING: _ClassVar[OrderLifecycleState]
    STALE: _ClassVar[OrderLifecycleState]
    FILLED: _ClassVar[OrderLifecycleState]
    CANCELLED: _ClassVar[OrderLifecycleState]
    PURGED: _ClassVar[OrderLifecycleState]

class MarketSessionPhase(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    MARKET_SESSION_PHASE_UNSPECIFIED: _ClassVar[MarketSessionPhase]
    OPEN: _ClassVar[MarketSessionPhase]
    CLOSED: _ClassVar[MarketSessionPhase]

class RiskNoticeKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    RISK_NOTICE_KIND_UNSPECIFIED: _ClassVar[RiskNoticeKind]
    LOSS_WARNING: _ClassVar[RiskNoticeKind]
SIDE_UNSPECIFIED: Side
BUY: Side
SELL: Side
ORDER_TYPE_UNSPECIFIED: OrderType
LIMIT: OrderType
MARKET: OrderType
REQUEST_TYPE_UNSPECIFIED: RequestType
NEW: RequestType
CANCEL: RequestType
AMEND: RequestType
MASS_CANCEL: RequestType
SUBSCRIBE: RequestType
UNSUBSCRIBE: RequestType
ORIGIN_UNSPECIFIED: Origin
TEAM: Origin
CURE_TRADE: Origin
AUTO_FLATTEN: Origin
LIQUIDITY_UNSPECIFIED: Liquidity
MAKER: Liquidity
TAKER: Liquidity
MATCH_KIND_UNSPECIFIED: MatchKind
STUDENT_TO_STUDENT: MatchKind
STUDENT_TO_WALL: MatchKind
TRADE_BASED_MATCH: MatchKind
RESIDUAL: MatchKind
ORDER_LIFECYCLE_STATE_UNSPECIFIED: OrderLifecycleState
RESTING: OrderLifecycleState
STALE: OrderLifecycleState
FILLED: OrderLifecycleState
CANCELLED: OrderLifecycleState
PURGED: OrderLifecycleState
MARKET_SESSION_PHASE_UNSPECIFIED: MarketSessionPhase
OPEN: MarketSessionPhase
CLOSED: MarketSessionPhase
RISK_NOTICE_KIND_UNSPECIFIED: RiskNoticeKind
LOSS_WARNING: RiskNoticeKind

class ReasonCodes(_message.Message):
    __slots__ = ()
    class ReasonCode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        REASON_CODE_UNSPECIFIED: _ClassVar[ReasonCodes.ReasonCode]
        NOT_AUTHENTICATED: _ClassVar[ReasonCodes.ReasonCode]
        VERSION_MISMATCH: _ClassVar[ReasonCodes.ReasonCode]
        NO_MARKET_ACCESS: _ClassVar[ReasonCodes.ReasonCode]
        TEAM_DISABLED: _ClassVar[ReasonCodes.ReasonCode]
        MARKET_CLOSED: _ClassVar[ReasonCodes.ReasonCode]
        RELEASE_AFTER_CLOSE: _ClassVar[ReasonCodes.ReasonCode]
        EXCHANGE_OUTAGE: _ClassVar[ReasonCodes.ReasonCode]
        STRATEGY_NOT_REGISTERED: _ClassVar[ReasonCodes.ReasonCode]
        OUTSIDE_DECLARED_SCOPE: _ClassVar[ReasonCodes.ReasonCode]
        INSTRUMENT_NOT_PERMITTED: _ClassVar[ReasonCodes.ReasonCode]
        INSTRUMENT_SUSPENDED: _ClassVar[ReasonCodes.ReasonCode]
        MALFORMED_MESSAGE: _ClassVar[ReasonCodes.ReasonCode]
        UNKNOWN_INSTRUMENT: _ClassVar[ReasonCodes.ReasonCode]
        TICK_VIOLATION: _ClassVar[ReasonCodes.ReasonCode]
        INVALID_SIZE: _ClassVar[ReasonCodes.ReasonCode]
        NON_POSITIVE_PRICE: _ClassVar[ReasonCodes.ReasonCode]
        NO_ORDER_AT_LEVEL: _ClassVar[ReasonCodes.ReasonCode]
        SIZE_OUT_OF_RANGE: _ClassVar[ReasonCodes.ReasonCode]
        PRICE_COLLAR: _ClassVar[ReasonCodes.ReasonCode]
        REFERENCE_UNAVAILABLE: _ClassVar[ReasonCodes.ReasonCode]
        NO_WALL_ON_SIDE: _ClassVar[ReasonCodes.ReasonCode]
        AMEND_PRICE_AT_OR_BEYOND_WALL: _ClassVar[ReasonCodes.ReasonCode]
        MIN_REST_VIOLATION: _ClassVar[ReasonCodes.ReasonCode]
        DUPLICATE_ORDER_AT_LEVEL: _ClassVar[ReasonCodes.ReasonCode]
        AMEND_WOULD_MAKE_STALE_MARKETABLE: _ClassVar[ReasonCodes.ReasonCode]
        MESSAGE_BUDGET_EXCEEDED: _ClassVar[ReasonCodes.ReasonCode]
        BURST_CAP_EXCEEDED: _ClassVar[ReasonCodes.ReasonCode]
        NEW_ORDER_CAP_EXCEEDED: _ClassVar[ReasonCodes.ReasonCode]
        RISK_LIMIT_BREACH: _ClassVar[ReasonCodes.ReasonCode]
        PARTICIPATION_CAP_EXCEEDED: _ClassVar[ReasonCodes.ReasonCode]
        POSITION_REDUCING_ONLY: _ClassVar[ReasonCodes.ReasonCode]
        RISK_REDUCING_ONLY: _ClassVar[ReasonCodes.ReasonCode]
        SANITY_GATE_REJECTED: _ClassVar[ReasonCodes.ReasonCode]
        HEDGE_ONLY: _ClassVar[ReasonCodes.ReasonCode]
        PARENT_NOT_WORKING: _ClassVar[ReasonCodes.ReasonCode]
        PARENT_MISMATCH: _ClassVar[ReasonCodes.ReasonCode]
        PARENT_QUANTITY_EXCEEDED: _ClassVar[ReasonCodes.ReasonCode]
        CANCEL_REQUEST: _ClassVar[ReasonCodes.ReasonCode]
        MASS_CANCEL: _ClassVar[ReasonCodes.ReasonCode]
        SELF_TRADE: _ClassVar[ReasonCodes.ReasonCode]
        PURGE_STALE: _ClassVar[ReasonCodes.ReasonCode]
        SESSION_CLOSE: _ClassVar[ReasonCodes.ReasonCode]
        MARKET_REMAINDER: _ClassVar[ReasonCodes.ReasonCode]
        REMAINDER_OUTSIDE_BAND: _ClassVar[ReasonCodes.ReasonCode]
        CURE_WINDOW: _ClassVar[ReasonCodes.ReasonCode]
        STRATEGY_NOT_LIVE: _ClassVar[ReasonCodes.ReasonCode]
        KILL_SWITCH: _ClassVar[ReasonCodes.ReasonCode]
        AMEND_CUT: _ClassVar[ReasonCodes.ReasonCode]
        HEDGE_RECHECK_FAILED: _ClassVar[ReasonCodes.ReasonCode]
        PARTICIPATION_LIMIT: _ClassVar[ReasonCodes.ReasonCode]
    REASON_CODE_UNSPECIFIED: ReasonCodes.ReasonCode
    NOT_AUTHENTICATED: ReasonCodes.ReasonCode
    VERSION_MISMATCH: ReasonCodes.ReasonCode
    NO_MARKET_ACCESS: ReasonCodes.ReasonCode
    TEAM_DISABLED: ReasonCodes.ReasonCode
    MARKET_CLOSED: ReasonCodes.ReasonCode
    RELEASE_AFTER_CLOSE: ReasonCodes.ReasonCode
    EXCHANGE_OUTAGE: ReasonCodes.ReasonCode
    STRATEGY_NOT_REGISTERED: ReasonCodes.ReasonCode
    OUTSIDE_DECLARED_SCOPE: ReasonCodes.ReasonCode
    INSTRUMENT_NOT_PERMITTED: ReasonCodes.ReasonCode
    INSTRUMENT_SUSPENDED: ReasonCodes.ReasonCode
    MALFORMED_MESSAGE: ReasonCodes.ReasonCode
    UNKNOWN_INSTRUMENT: ReasonCodes.ReasonCode
    TICK_VIOLATION: ReasonCodes.ReasonCode
    INVALID_SIZE: ReasonCodes.ReasonCode
    NON_POSITIVE_PRICE: ReasonCodes.ReasonCode
    NO_ORDER_AT_LEVEL: ReasonCodes.ReasonCode
    SIZE_OUT_OF_RANGE: ReasonCodes.ReasonCode
    PRICE_COLLAR: ReasonCodes.ReasonCode
    REFERENCE_UNAVAILABLE: ReasonCodes.ReasonCode
    NO_WALL_ON_SIDE: ReasonCodes.ReasonCode
    AMEND_PRICE_AT_OR_BEYOND_WALL: ReasonCodes.ReasonCode
    MIN_REST_VIOLATION: ReasonCodes.ReasonCode
    DUPLICATE_ORDER_AT_LEVEL: ReasonCodes.ReasonCode
    AMEND_WOULD_MAKE_STALE_MARKETABLE: ReasonCodes.ReasonCode
    MESSAGE_BUDGET_EXCEEDED: ReasonCodes.ReasonCode
    BURST_CAP_EXCEEDED: ReasonCodes.ReasonCode
    NEW_ORDER_CAP_EXCEEDED: ReasonCodes.ReasonCode
    RISK_LIMIT_BREACH: ReasonCodes.ReasonCode
    PARTICIPATION_CAP_EXCEEDED: ReasonCodes.ReasonCode
    POSITION_REDUCING_ONLY: ReasonCodes.ReasonCode
    RISK_REDUCING_ONLY: ReasonCodes.ReasonCode
    SANITY_GATE_REJECTED: ReasonCodes.ReasonCode
    HEDGE_ONLY: ReasonCodes.ReasonCode
    PARENT_NOT_WORKING: ReasonCodes.ReasonCode
    PARENT_MISMATCH: ReasonCodes.ReasonCode
    PARENT_QUANTITY_EXCEEDED: ReasonCodes.ReasonCode
    CANCEL_REQUEST: ReasonCodes.ReasonCode
    MASS_CANCEL: ReasonCodes.ReasonCode
    SELF_TRADE: ReasonCodes.ReasonCode
    PURGE_STALE: ReasonCodes.ReasonCode
    SESSION_CLOSE: ReasonCodes.ReasonCode
    MARKET_REMAINDER: ReasonCodes.ReasonCode
    REMAINDER_OUTSIDE_BAND: ReasonCodes.ReasonCode
    CURE_WINDOW: ReasonCodes.ReasonCode
    STRATEGY_NOT_LIVE: ReasonCodes.ReasonCode
    KILL_SWITCH: ReasonCodes.ReasonCode
    AMEND_CUT: ReasonCodes.ReasonCode
    HEDGE_RECHECK_FAILED: ReasonCodes.ReasonCode
    PARTICIPATION_LIMIT: ReasonCodes.ReasonCode
    def __init__(self) -> None: ...
