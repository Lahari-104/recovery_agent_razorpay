"""
Policy as configuration (FR-23, NFR-35).

Every number that bounds the agent's behaviour lives here, in one versioned,
diffable object. Nothing in this file is ever passed to an LLM as an
instruction -- the executor reads these values directly and enforces them in
code (NFR-4). An LLM cannot argue with an integer comparison.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, replace
from typing import Any

from .domain import Action, Method


@dataclass(frozen=True)
class PolicyConfig:
    version: str = "v1.3.0"

    # --- bounded execution: hard caps (FR-24..FR-27) ----------------------
    max_attempts_per_payment: int = 3
    max_touches_per_customer_24h: int = 4
    min_cooldown_minutes: int = 45
    max_recovery_window_hours: int = 72

    # --- economics (FR-32) ------------------------------------------------
    # Cost of performing each intervention, in paise.
    cost_retry_paise: int = 35            # gateway attempt fee
    cost_payment_link_paise: int = 250    # SMS + link generation
    cost_escalation_paise: int = 4000     # ~2 min of a human's time
    cost_stop_paise: int = 0

    # --- economic guardrails ---------------------------------------------
    # Never spend more chasing a payment than it is worth.
    max_spend_ratio: float = 0.08         # 8% of the payment value
    min_amount_to_chase_paise: int = 5000 # below Rs 50, chasing loses money

    # --- confidence gating (FR-22) ---------------------------------------
    escalate_below_confidence: float = 0.55

    # --- compliance (NFR-22..NFR-26) -------------------------------------
    quiet_hours_start: int = 21           # 21:00 IST
    quiet_hours_end: int = 8              # 08:00 IST
    timezone_offset_hours: float = 5.5    # IST
    respect_opt_out: bool = True

    # --- retry timing intelligence (FR-21) --------------------------------
    insufficient_funds_delay_hours: float = 14.0
    bank_downtime_delay_hours: float = 1.5
    network_timeout_delay_hours: float = 0.0
    generic_soft_delay_hours: float = 3.0

    # --- runtime switches -------------------------------------------------
    kill_switch: bool = False             # FR-28
    dry_run: bool = False                 # FR-29

    def cost_of(self, action: Action) -> int:
        return {
            Action.RETRY_NOW: self.cost_retry_paise,
            Action.SCHEDULE_RETRY: self.cost_retry_paise,
            Action.PAYMENT_LINK: self.cost_payment_link_paise,
            Action.ESCALATE: self.cost_escalation_paise,
            Action.STOP: self.cost_stop_paise,
        }[action]

    def spend_ceiling_paise(self, amount_paise: int) -> int:
        return int(amount_paise * self.max_spend_ratio)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def with_overrides(self, **kw: Any) -> "PolicyConfig":
        """Used by the policy simulator (FR-36) to fork config without mutation."""
        valid = {k: v for k, v in kw.items() if k in self.__dataclass_fields__}
        return replace(self, **valid)


#: Naive baseline: what most merchants actually do today.
#: Retry everything, immediately, three times, no brains.
NAIVE_CONFIG = PolicyConfig(
    version="naive-baseline",
    max_attempts_per_payment=3,
    max_touches_per_customer_24h=99,
    min_cooldown_minutes=0,
    escalate_below_confidence=0.0,
    max_spend_ratio=1.0,
    min_amount_to_chase_paise=0,
)

DEFAULT_CONFIG = PolicyConfig()

#: Alternate method preference when switching away from a failing rail.
METHOD_FALLBACK: dict[Method, Method] = {
    Method.UPI: Method.CARD,
    Method.CARD: Method.UPI,
    Method.NETBANKING: Method.UPI,
    Method.WALLET: Method.UPI,
}
