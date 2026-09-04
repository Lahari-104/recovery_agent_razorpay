"""
Domain layer: the vocabulary the whole system agrees on.

Nothing in here talks to a database, an LLM, or a payment gateway. It is pure
types and constants so every other layer can depend on it without cycles.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional


# --------------------------------------------------------------------------
# Failure taxonomy
# --------------------------------------------------------------------------

class Cause(str, Enum):
    """Top-level failure buckets. Determines whether recovery is even possible."""
    SOFT_DECLINE = "soft_decline"      # bank said no, but conditions may change
    HARD_DECLINE = "hard_decline"      # bank said no, permanently
    USER_DROPOFF = "user_dropoff"      # customer never completed the action
    UNKNOWN = "unknown"                # classifier could not decide


class SubCause(str, Enum):
    INSUFFICIENT_FUNDS = "insufficient_funds"
    BANK_DOWNTIME = "bank_downtime"
    ISSUER_UNAVAILABLE = "issuer_unavailable"
    NETWORK_TIMEOUT = "network_timeout"
    GATEWAY_ERROR = "gateway_error"
    RATE_LIMITED = "rate_limited"

    CARD_EXPIRED = "card_expired"
    CARD_BLOCKED = "card_blocked"
    INVALID_ACCOUNT = "invalid_account"
    ACCOUNT_FROZEN = "account_frozen"
    RISK_BLOCKED = "risk_blocked"
    METHOD_NOT_SUPPORTED = "method_not_supported"
    MANDATE_REVOKED = "mandate_revoked"

    OTP_NOT_ENTERED = "otp_not_entered"
    COLLECT_EXPIRED = "collect_expired"
    CANCELLED_BY_USER = "cancelled_by_user"
    WINDOW_CLOSED = "window_closed"
    INCORRECT_VPA = "incorrect_vpa"

    UNKNOWN = "unknown"


#: Deterministic map from gateway error code -> (cause, subcause).
#: This is the rules path. Anything not in this table goes to the LLM.
ERROR_CODE_MAP: dict[str, tuple[Cause, SubCause]] = {
    # --- soft declines -----------------------------------------------------
    "payment_failed_insufficient_funds": (Cause.SOFT_DECLINE, SubCause.INSUFFICIENT_FUNDS),
    "insufficient_balance":              (Cause.SOFT_DECLINE, SubCause.INSUFFICIENT_FUNDS),
    "payment_upi_bank_down":             (Cause.SOFT_DECLINE, SubCause.BANK_DOWNTIME),
    "issuer_down":                       (Cause.SOFT_DECLINE, SubCause.ISSUER_UNAVAILABLE),
    "payment_timeout":                   (Cause.SOFT_DECLINE, SubCause.NETWORK_TIMEOUT),
    "network_error":                     (Cause.SOFT_DECLINE, SubCause.NETWORK_TIMEOUT),
    "gateway_technical_error":           (Cause.SOFT_DECLINE, SubCause.GATEWAY_ERROR),
    "payment_declined_try_later":        (Cause.SOFT_DECLINE, SubCause.ISSUER_UNAVAILABLE),
    "too_many_requests":                 (Cause.SOFT_DECLINE, SubCause.RATE_LIMITED),

    # --- hard declines -----------------------------------------------------
    "card_expired":            (Cause.HARD_DECLINE, SubCause.CARD_EXPIRED),
    "card_blocked":            (Cause.HARD_DECLINE, SubCause.CARD_BLOCKED),
    "invalid_account":         (Cause.HARD_DECLINE, SubCause.INVALID_ACCOUNT),
    "account_frozen":          (Cause.HARD_DECLINE, SubCause.ACCOUNT_FROZEN),
    "payment_blocked_risk":    (Cause.HARD_DECLINE, SubCause.RISK_BLOCKED),
    "card_not_supported":      (Cause.HARD_DECLINE, SubCause.METHOD_NOT_SUPPORTED),
    "mandate_revoked":         (Cause.HARD_DECLINE, SubCause.MANDATE_REVOKED),

    # --- user drop-off -----------------------------------------------------
    "payment_cancelled_by_user": (Cause.USER_DROPOFF, SubCause.CANCELLED_BY_USER),
    "upi_collect_expired":       (Cause.USER_DROPOFF, SubCause.COLLECT_EXPIRED),
    "otp_not_entered":           (Cause.USER_DROPOFF, SubCause.OTP_NOT_ENTERED),
    "payment_window_closed":     (Cause.USER_DROPOFF, SubCause.WINDOW_CLOSED),
    "incorrect_vpa":             (Cause.USER_DROPOFF, SubCause.INCORRECT_VPA),
}

#: Subcauses that must NEVER be retried on the same instrument. NFR-4 / FR-27.
#: Enforced in the executor as a hard invariant, not as policy advice.
NEVER_RETRY: frozenset[SubCause] = frozenset({
    SubCause.CARD_EXPIRED,
    SubCause.CARD_BLOCKED,
    SubCause.INVALID_ACCOUNT,
    SubCause.ACCOUNT_FROZEN,
    SubCause.RISK_BLOCKED,
    SubCause.METHOD_NOT_SUPPORTED,
    SubCause.MANDATE_REVOKED,
})


# --------------------------------------------------------------------------
# Payment methods and interventions
# --------------------------------------------------------------------------

class Method(str, Enum):
    UPI = "upi"
    CARD = "card"
    NETBANKING = "netbanking"
    WALLET = "wallet"


class Action(str, Enum):
    """The complete allowlist of things the agent may do. NFR-5."""
    RETRY_NOW = "retry_now"
    SCHEDULE_RETRY = "schedule_retry"
    PAYMENT_LINK = "payment_link"        # alternate-method nudge
    ESCALATE = "escalate"                # human review queue
    STOP = "stop"                        # deliberate give-up


class CaseState(str, Enum):
    """Explicit state machine. Illegal transitions are rejected. NFR-2."""
    DETECTED = "detected"
    CLASSIFIED = "classified"
    DECIDED = "decided"
    SCHEDULED = "scheduled"
    IN_FLIGHT = "in_flight"
    RECOVERED = "recovered"
    ABANDONED = "abandoned"      # stopping rule fired
    ESCALATED = "escalated"      # awaiting human
    BLOCKED = "blocked"          # a cap or invariant refused the action


LEGAL_TRANSITIONS: dict[CaseState, frozenset[CaseState]] = {
    CaseState.DETECTED:   frozenset({CaseState.CLASSIFIED, CaseState.BLOCKED}),
    CaseState.CLASSIFIED: frozenset({CaseState.DECIDED, CaseState.ESCALATED, CaseState.BLOCKED}),
    CaseState.DECIDED:    frozenset({CaseState.IN_FLIGHT, CaseState.SCHEDULED,
                                     CaseState.ABANDONED, CaseState.ESCALATED, CaseState.BLOCKED}),
    CaseState.SCHEDULED:  frozenset({CaseState.IN_FLIGHT, CaseState.ABANDONED, CaseState.BLOCKED}),
    CaseState.IN_FLIGHT:  frozenset({CaseState.RECOVERED, CaseState.DECIDED,
                                     CaseState.ABANDONED, CaseState.ESCALATED, CaseState.BLOCKED}),
    # BLOCKED -> DECIDED is the deferral path: a cooldown or quiet-hours guard
    # said "not yet", so the case re-enters the decision loop at a later time.
    CaseState.BLOCKED:    frozenset({CaseState.ABANDONED, CaseState.ESCALATED,
                                     CaseState.DECIDED}),
    CaseState.RECOVERED:  frozenset(),   # terminal
    CaseState.ABANDONED:  frozenset(),   # terminal
    CaseState.ESCALATED:  frozenset({CaseState.ABANDONED}),
}


class IllegalTransition(Exception):
    """Raised when code attempts a state change the machine forbids."""


def assert_transition(src: CaseState, dst: CaseState) -> None:
    if dst not in LEGAL_TRANSITIONS[src]:
        raise IllegalTransition(f"{src.value} -> {dst.value} is not a legal transition")


# --------------------------------------------------------------------------
# Core records
# --------------------------------------------------------------------------

@dataclass
class CustomerProfile:
    """Everything the agent is allowed to know about the payer."""
    customer_id: str
    lifetime_payments: int
    lifetime_failures: int
    days_since_last_success: int
    opted_out: bool = False

    @property
    def failure_rate(self) -> float:
        total = self.lifetime_payments + self.lifetime_failures
        return self.lifetime_failures / total if total else 0.0


@dataclass
class FailedPayment:
    """
    The observable half of a failure. This is exactly what a real
    `payment.failed` webhook gives you, plus merchant-side context.
    The agent sees this and nothing more.
    """
    payment_id: str
    order_id: str
    customer: CustomerProfile
    amount_paise: int
    method: Method
    bank: str
    error_code: str
    error_description: str
    error_source: str
    error_step: str
    failed_at: datetime
    attempt_no: int = 1
    is_synthetic: bool = True

    @property
    def amount_rupees(self) -> float:
        return self.amount_paise / 100.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["failed_at"] = self.failed_at.isoformat()
        d["method"] = self.method.value
        d["amount_rupees"] = self.amount_rupees
        return d


@dataclass
class Classification:
    cause: Cause
    subcause: SubCause
    confidence: float
    reason: str
    source: str          # "rules" | "llm" | "llm_fallback_rules"
    latency_ms: float = 0.0


@dataclass
class Decision:
    action: Action
    delay_hours: float
    alt_method: Optional[Method]
    reason: str
    policy_rule: str          # which rule produced this. NFR-6.
    expected_value_paise: int = 0


@dataclass
class Attempt:
    """One executed intervention."""
    case_id: str
    seq: int
    action: Action
    method: Method
    executed_at: datetime
    succeeded: bool
    cost_paise: int
    note: str = ""


@dataclass
class Case:
    """A recovery case: one failed payment plus everything done about it."""
    case_id: str
    payment: FailedPayment
    state: CaseState = CaseState.DETECTED
    classification: Optional[Classification] = None
    decisions: list[Decision] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)
    recovered_paise: int = 0
    cost_paise: int = 0
    closed_reason: str = ""

    def transition(self, dst: CaseState) -> None:
        assert_transition(self.state, dst)
        self.state = dst

    @property
    def is_terminal(self) -> bool:
        return self.state in (CaseState.RECOVERED, CaseState.ABANDONED)

    @property
    def net_paise(self) -> int:
        return self.recovered_paise - self.cost_paise


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "Cause", "SubCause", "ERROR_CODE_MAP", "NEVER_RETRY", "Method", "Action",
    "CaseState", "LEGAL_TRANSITIONS", "IllegalTransition", "assert_transition",
    "CustomerProfile", "FailedPayment", "Classification", "Decision",
    "Attempt", "Case", "utcnow", "timedelta",
]
