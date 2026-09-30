"""Wire type tokens and the contract version the SDK speaks."""

from google.protobuf.message import Message

from qte_sdk.contract.v1.market_data_pb2 import Book, Mark, OfficialClose, SessionState, Trades
from qte_sdk.contract.v1.order_events_pb2 import (
    Accepted,
    Execution,
    OrderCancelled,
    OrderState,
    Reject,
    RiskNotice,
)
from qte_sdk.contract.v1.session_pb2 import Heartbeat, OrderSnapshot, SessionAck, SessionReject

# The exchange serves one contract version and compares it exactly. Versioning policy is
# not settled yet; this matches the contract the vendored protos were pinned from.
CONTRACT_VERSION = "0.x"

# Every message type the exchange sends, keyed by the envelope's `type` token.
INBOUND: dict[str, type[Message]] = {
    "session_ack": SessionAck,
    "session_reject": SessionReject,
    "heartbeat": Heartbeat,
    "order_snapshot": OrderSnapshot,
    "accepted": Accepted,
    "reject": Reject,
    "execution": Execution,
    "order_cancelled": OrderCancelled,
    "order_state": OrderState,
    "risk_notice": RiskNotice,
    "book": Book,
    "trades": Trades,
    "mark": Mark,
    "session_state": SessionState,
    "official_close": OfficialClose,
}
