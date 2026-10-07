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
    RESUME: _ClassVar[RequestType]
    ACCOUNT_QUERY: _ClassVar[RequestType]

class Origin(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ORIGIN_UNSPECIFIED: _ClassVar[Origin]
    TEAM: _ClassVar[Origin]
    CURE_TRADE: _ClassVar[Origin]
    AUTO_FLATTEN: _ClassVar[Origin]
    HOUSE: _ClassVar[Origin]

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

class ValuationBasis(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    VALUATION_BASIS_UNSPECIFIED: _ClassVar[ValuationBasis]
    LIVE_MARK: _ClassVar[ValuationBasis]
    LAST_OFFICIAL_CLOSE: _ClassVar[ValuationBasis]

class LossLevel(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    LOSS_LEVEL_UNSPECIFIED: _ClassVar[LossLevel]
    LOSS_LEVEL_NONE: _ClassVar[LossLevel]
    LOSS_LEVEL_WARNING: _ClassVar[LossLevel]
    LOSS_LEVEL_HALT: _ClassVar[LossLevel]

class LimitKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    LIMIT_KIND_UNSPECIFIED: _ClassVar[LimitKind]
    LIMIT_GROSS: _ClassVar[LimitKind]
    LIMIT_NET: _ClassVar[LimitKind]
    LIMIT_INSTRUMENT: _ClassVar[LimitKind]
    LIMIT_INSTRUMENT_HARD: _ClassVar[LimitKind]
    LIMIT_SECTOR: _ClassVar[LimitKind]
    LIMIT_SHORT_OF_GROSS: _ClassVar[LimitKind]
    LIMIT_VEGA: _ClassVar[LimitKind]
    LIMIT_GAMMA_DOLLAR: _ClassVar[LimitKind]
    LIMIT_NET_DELTA_UNDERLYING: _ClassVar[LimitKind]
    LIMIT_NET_DELTA_BOOK: _ClassVar[LimitKind]
    LIMIT_HEDGING_ALLOWANCE_UNDERLYING: _ClassVar[LimitKind]
    LIMIT_HEDGING_ALLOWANCE_BOOK: _ClassVar[LimitKind]
    LIMIT_VEGA_UNDERLYING_AGGREGATE: _ClassVar[LimitKind]
    LIMIT_HEDGE_SIGN: _ClassVar[LimitKind]
    LIMIT_HEDGE_MAGNITUDE_UNDERLYING: _ClassVar[LimitKind]
    LIMIT_HEDGE_MAGNITUDE_BOOK: _ClassVar[LimitKind]

class RiskNoticeKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    RISK_NOTICE_KIND_UNSPECIFIED: _ClassVar[RiskNoticeKind]
    LOSS_WARNING: _ClassVar[RiskNoticeKind]
    LOSS_HALT_ENTERED: _ClassVar[RiskNoticeKind]
    CURE_WINDOW_OPENED: _ClassVar[RiskNoticeKind]
    CURE_TRADE_PLACED: _ClassVar[RiskNoticeKind]
    AUTO_FLATTEN_STARTED: _ClassVar[RiskNoticeKind]
    KILL_SWITCH_ENGAGED: _ClassVar[RiskNoticeKind]

class TicketUrgency(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TICKET_URGENCY_UNSPECIFIED: _ClassVar[TicketUrgency]
    URGENCY_HIGH: _ClassVar[TicketUrgency]
    URGENCY_MEDIUM: _ClassVar[TicketUrgency]
    URGENCY_LOW: _ClassVar[TicketUrgency]

class TicketStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TICKET_STATUS_UNSPECIFIED: _ClassVar[TicketStatus]
    TICKET_WORKING: _ClassVar[TicketStatus]
    TICKET_COMPLETE: _ClassVar[TicketStatus]
    TICKET_EXPIRED: _ClassVar[TicketStatus]
    TICKET_LIMIT_CANCELLED: _ClassVar[TicketStatus]
    TICKET_CANCELLED_BY_POD: _ClassVar[TicketStatus]
    TICKET_CANCELLED_BY_ENGINE: _ClassVar[TicketStatus]

class TicketRequestKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TICKET_REQUEST_KIND_UNSPECIFIED: _ClassVar[TicketRequestKind]
    TICKET_REQUEST_SUBMIT: _ClassVar[TicketRequestKind]
    TICKET_REQUEST_CANCEL: _ClassVar[TicketRequestKind]
    TICKET_REQUEST_URGENCY: _ClassVar[TicketRequestKind]
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
RESUME: RequestType
ACCOUNT_QUERY: RequestType
ORIGIN_UNSPECIFIED: Origin
TEAM: Origin
CURE_TRADE: Origin
AUTO_FLATTEN: Origin
HOUSE: Origin
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
VALUATION_BASIS_UNSPECIFIED: ValuationBasis
LIVE_MARK: ValuationBasis
LAST_OFFICIAL_CLOSE: ValuationBasis
LOSS_LEVEL_UNSPECIFIED: LossLevel
LOSS_LEVEL_NONE: LossLevel
LOSS_LEVEL_WARNING: LossLevel
LOSS_LEVEL_HALT: LossLevel
LIMIT_KIND_UNSPECIFIED: LimitKind
LIMIT_GROSS: LimitKind
LIMIT_NET: LimitKind
LIMIT_INSTRUMENT: LimitKind
LIMIT_INSTRUMENT_HARD: LimitKind
LIMIT_SECTOR: LimitKind
LIMIT_SHORT_OF_GROSS: LimitKind
LIMIT_VEGA: LimitKind
LIMIT_GAMMA_DOLLAR: LimitKind
LIMIT_NET_DELTA_UNDERLYING: LimitKind
LIMIT_NET_DELTA_BOOK: LimitKind
LIMIT_HEDGING_ALLOWANCE_UNDERLYING: LimitKind
LIMIT_HEDGING_ALLOWANCE_BOOK: LimitKind
LIMIT_VEGA_UNDERLYING_AGGREGATE: LimitKind
LIMIT_HEDGE_SIGN: LimitKind
LIMIT_HEDGE_MAGNITUDE_UNDERLYING: LimitKind
LIMIT_HEDGE_MAGNITUDE_BOOK: LimitKind
RISK_NOTICE_KIND_UNSPECIFIED: RiskNoticeKind
LOSS_WARNING: RiskNoticeKind
LOSS_HALT_ENTERED: RiskNoticeKind
CURE_WINDOW_OPENED: RiskNoticeKind
CURE_TRADE_PLACED: RiskNoticeKind
AUTO_FLATTEN_STARTED: RiskNoticeKind
KILL_SWITCH_ENGAGED: RiskNoticeKind
TICKET_URGENCY_UNSPECIFIED: TicketUrgency
URGENCY_HIGH: TicketUrgency
URGENCY_MEDIUM: TicketUrgency
URGENCY_LOW: TicketUrgency
TICKET_STATUS_UNSPECIFIED: TicketStatus
TICKET_WORKING: TicketStatus
TICKET_COMPLETE: TicketStatus
TICKET_EXPIRED: TicketStatus
TICKET_LIMIT_CANCELLED: TicketStatus
TICKET_CANCELLED_BY_POD: TicketStatus
TICKET_CANCELLED_BY_ENGINE: TicketStatus
TICKET_REQUEST_KIND_UNSPECIFIED: TicketRequestKind
TICKET_REQUEST_SUBMIT: TicketRequestKind
TICKET_REQUEST_CANCEL: TicketRequestKind
TICKET_REQUEST_URGENCY: TicketRequestKind

class TicketReasonCodes(_message.Message):
    __slots__ = ()
    class TicketReasonCode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        TICKET_REASON_CODE_UNSPECIFIED: _ClassVar[TicketReasonCodes.TicketReasonCode]
        NOT_AUTHENTICATED: _ClassVar[TicketReasonCodes.TicketReasonCode]
        VERSION_MISMATCH: _ClassVar[TicketReasonCodes.TicketReasonCode]
        TEAM_DISABLED: _ClassVar[TicketReasonCodes.TicketReasonCode]
        MARKET_CLOSED: _ClassVar[TicketReasonCodes.TicketReasonCode]
        INSTRUMENT_DISABLED: _ClassVar[TicketReasonCodes.TicketReasonCode]
        MALFORMED_MESSAGE: _ClassVar[TicketReasonCodes.TicketReasonCode]
        UNKNOWN_INSTRUMENT: _ClassVar[TicketReasonCodes.TicketReasonCode]
        TICK_VIOLATION: _ClassVar[TicketReasonCodes.TicketReasonCode]
        INVALID_SIZE: _ClassVar[TicketReasonCodes.TicketReasonCode]
        NON_POSITIVE_PRICE: _ClassVar[TicketReasonCodes.TicketReasonCode]
        SIZE_OUT_OF_RANGE: _ClassVar[TicketReasonCodes.TicketReasonCode]
        REFERENCE_UNAVAILABLE: _ClassVar[TicketReasonCodes.TicketReasonCode]
        RISK_LIMIT_BREACH: _ClassVar[TicketReasonCodes.TicketReasonCode]
        TICKET_THESIS_NOT_FILED: _ClassVar[TicketReasonCodes.TicketReasonCode]
        TICKET_INSTRUMENT_WORKING: _ClassVar[TicketReasonCodes.TicketReasonCode]
        TICKET_CROSSES_ZERO: _ClassVar[TicketReasonCodes.TicketReasonCode]
        TICKET_LIMIT_INSIDE_WALL: _ClassVar[TicketReasonCodes.TicketReasonCode]
        TICKET_COMPLETES_AFTER_TERM: _ClassVar[TicketReasonCodes.TicketReasonCode]
        TICKET_MARK_FROZEN: _ClassVar[TicketReasonCodes.TicketReasonCode]
        TICKET_REPLACES_INVALID: _ClassVar[TicketReasonCodes.TicketReasonCode]
        TICKET_NOT_FOUND: _ClassVar[TicketReasonCodes.TicketReasonCode]
        TICKET_NOT_WORKING: _ClassVar[TicketReasonCodes.TicketReasonCode]
        TICKET_CURE_NOT_CANCELLABLE: _ClassVar[TicketReasonCodes.TicketReasonCode]
        TICKET_NOT_CURE: _ClassVar[TicketReasonCodes.TicketReasonCode]
        TICKET_URGENCY_NOT_INCREASED: _ClassVar[TicketReasonCodes.TicketReasonCode]
        TICKET_CURE_INVALID: _ClassVar[TicketReasonCodes.TicketReasonCode]
    TICKET_REASON_CODE_UNSPECIFIED: TicketReasonCodes.TicketReasonCode
    NOT_AUTHENTICATED: TicketReasonCodes.TicketReasonCode
    VERSION_MISMATCH: TicketReasonCodes.TicketReasonCode
    TEAM_DISABLED: TicketReasonCodes.TicketReasonCode
    MARKET_CLOSED: TicketReasonCodes.TicketReasonCode
    INSTRUMENT_DISABLED: TicketReasonCodes.TicketReasonCode
    MALFORMED_MESSAGE: TicketReasonCodes.TicketReasonCode
    UNKNOWN_INSTRUMENT: TicketReasonCodes.TicketReasonCode
    TICK_VIOLATION: TicketReasonCodes.TicketReasonCode
    INVALID_SIZE: TicketReasonCodes.TicketReasonCode
    NON_POSITIVE_PRICE: TicketReasonCodes.TicketReasonCode
    SIZE_OUT_OF_RANGE: TicketReasonCodes.TicketReasonCode
    REFERENCE_UNAVAILABLE: TicketReasonCodes.TicketReasonCode
    RISK_LIMIT_BREACH: TicketReasonCodes.TicketReasonCode
    TICKET_THESIS_NOT_FILED: TicketReasonCodes.TicketReasonCode
    TICKET_INSTRUMENT_WORKING: TicketReasonCodes.TicketReasonCode
    TICKET_CROSSES_ZERO: TicketReasonCodes.TicketReasonCode
    TICKET_LIMIT_INSIDE_WALL: TicketReasonCodes.TicketReasonCode
    TICKET_COMPLETES_AFTER_TERM: TicketReasonCodes.TicketReasonCode
    TICKET_MARK_FROZEN: TicketReasonCodes.TicketReasonCode
    TICKET_REPLACES_INVALID: TicketReasonCodes.TicketReasonCode
    TICKET_NOT_FOUND: TicketReasonCodes.TicketReasonCode
    TICKET_NOT_WORKING: TicketReasonCodes.TicketReasonCode
    TICKET_CURE_NOT_CANCELLABLE: TicketReasonCodes.TicketReasonCode
    TICKET_NOT_CURE: TicketReasonCodes.TicketReasonCode
    TICKET_URGENCY_NOT_INCREASED: TicketReasonCodes.TicketReasonCode
    TICKET_CURE_INVALID: TicketReasonCodes.TicketReasonCode
    def __init__(self) -> None: ...

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
        TRADING_CUTOFF: _ClassVar[ReasonCodes.ReasonCode]
        STRATEGY_NOT_REGISTERED: _ClassVar[ReasonCodes.ReasonCode]
        OUTSIDE_DECLARED_SCOPE: _ClassVar[ReasonCodes.ReasonCode]
        INSTRUMENT_NOT_PERMITTED: _ClassVar[ReasonCodes.ReasonCode]
        INSTRUMENT_SUSPENDED: _ClassVar[ReasonCodes.ReasonCode]
        INSTRUMENT_DISABLED: _ClassVar[ReasonCodes.ReasonCode]
        CONTRACT_NOT_LISTED: _ClassVar[ReasonCodes.ReasonCode]
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
        CONTRACT_SUSPENDED: _ClassVar[ReasonCodes.ReasonCode]
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
        CONTRACT_REDUCING_ONLY: _ClassVar[ReasonCodes.ReasonCode]
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
        LOSS_HALT: _ClassVar[ReasonCodes.ReasonCode]
        POSITION_REDUCING_RECHECK_FAILED: _ClassVar[ReasonCodes.ReasonCode]
        TERM_CUTOFF: _ClassVar[ReasonCodes.ReasonCode]
        CONTRACT_REDUCING_RECHECK_FAILED: _ClassVar[ReasonCodes.ReasonCode]
        PARENT_STOPPED: _ClassVar[ReasonCodes.ReasonCode]
    REASON_CODE_UNSPECIFIED: ReasonCodes.ReasonCode
    NOT_AUTHENTICATED: ReasonCodes.ReasonCode
    VERSION_MISMATCH: ReasonCodes.ReasonCode
    NO_MARKET_ACCESS: ReasonCodes.ReasonCode
    TEAM_DISABLED: ReasonCodes.ReasonCode
    MARKET_CLOSED: ReasonCodes.ReasonCode
    RELEASE_AFTER_CLOSE: ReasonCodes.ReasonCode
    EXCHANGE_OUTAGE: ReasonCodes.ReasonCode
    TRADING_CUTOFF: ReasonCodes.ReasonCode
    STRATEGY_NOT_REGISTERED: ReasonCodes.ReasonCode
    OUTSIDE_DECLARED_SCOPE: ReasonCodes.ReasonCode
    INSTRUMENT_NOT_PERMITTED: ReasonCodes.ReasonCode
    INSTRUMENT_SUSPENDED: ReasonCodes.ReasonCode
    INSTRUMENT_DISABLED: ReasonCodes.ReasonCode
    CONTRACT_NOT_LISTED: ReasonCodes.ReasonCode
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
    CONTRACT_SUSPENDED: ReasonCodes.ReasonCode
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
    CONTRACT_REDUCING_ONLY: ReasonCodes.ReasonCode
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
    LOSS_HALT: ReasonCodes.ReasonCode
    POSITION_REDUCING_RECHECK_FAILED: ReasonCodes.ReasonCode
    TERM_CUTOFF: ReasonCodes.ReasonCode
    CONTRACT_REDUCING_RECHECK_FAILED: ReasonCodes.ReasonCode
    PARENT_STOPPED: ReasonCodes.ReasonCode
    def __init__(self) -> None: ...
