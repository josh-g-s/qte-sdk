"""Wire type tokens and the contract version the SDK speaks."""

from google.protobuf.message import Message

from qte_sdk.contract.v1.market_data_pb2 import (
    Book,
    Mark,
    OfficialClose,
    OptionChain,
    OptionGreeks,
    SessionState,
    Trades,
)
from qte_sdk.contract.v1.order_entry_pb2 import (
    AmendOrder,
    CancelOrder,
    CancelTicket,
    MassCancel,
    NewOrder,
    RaiseTicketUrgency,
    SubmitBreakRequest,
    SubmitTicket,
    WithdrawBreakRequest,
)
from qte_sdk.contract.v1.order_events_pb2 import (
    Accepted,
    AccountState,
    AccountSummary,
    BreakRequestReject,
    BreakRequestState,
    Execution,
    ObligationState,
    OrderCancelled,
    OrderState,
    Reject,
    RiskNotice,
    TermCloseFill,
    TermEnd,
    TicketAccepted,
    TicketReject,
    TicketState,
)
from qte_sdk.contract.v1.session_pb2 import (
    AccountQuery,
    Auth,
    Calendar,
    ExecutionRoute,
    Heartbeat,
    Instruments,
    OrderSnapshot,
    Resume,
    ResumeAck,
    SessionAck,
    SessionReject,
    Subscribe,
    Unsubscribe,
)

# The exchange serves one contract version and compares it exactly. Versioning policy is
# not settled yet; this matches the contract the vendored protos were pinned from.
CONTRACT_VERSION = "0.x"

# Every message type the exchange sends, keyed by the envelope's `type` token.
INBOUND: dict[str, type[Message]] = {
    "session_ack": SessionAck,
    "calendar": Calendar,
    "instruments": Instruments,
    "session_reject": SessionReject,
    "heartbeat": Heartbeat,
    "resume_ack": ResumeAck,
    "order_snapshot": OrderSnapshot,
    "accepted": Accepted,
    "reject": Reject,
    "execution": Execution,
    "order_cancelled": OrderCancelled,
    "order_state": OrderState,
    "risk_notice": RiskNotice,
    "account_summary": AccountSummary,
    "obligation_state": ObligationState,
    "account_state": AccountState,
    "ticket_accepted": TicketAccepted,
    "ticket_reject": TicketReject,
    "ticket_state": TicketState,
    "break_request_reject": BreakRequestReject,
    "break_request_state": BreakRequestState,
    "term_close_fill": TermCloseFill,
    "term_end": TermEnd,
    "execution_route": ExecutionRoute,
    "book": Book,
    "trades": Trades,
    "mark": Mark,
    "session_state": SessionState,
    "official_close": OfficialClose,
    "option_chain": OptionChain,
    "option_greeks": OptionGreeks,
}

# Every message type this SDK sends, keyed by the envelope's `type` token. A Director
# session's own messages (`house_ticket`, `house_ticket_cancel`) are not sent by this SDK.
OUTBOUND: dict[str, type[Message]] = {
    "auth": Auth,
    "resume": Resume,
    "subscribe": Subscribe,
    "unsubscribe": Unsubscribe,
    "account_query": AccountQuery,
    "new": NewOrder,
    "cancel": CancelOrder,
    "amend": AmendOrder,
    "mass_cancel": MassCancel,
    "ticket": SubmitTicket,
    "ticket_cancel": CancelTicket,
    "ticket_urgency": RaiseTicketUrgency,
    "break_request": SubmitBreakRequest,
    "break_request_withdraw": WithdrawBreakRequest,
}
