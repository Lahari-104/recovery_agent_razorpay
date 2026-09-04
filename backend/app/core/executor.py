"""
Bounded execution (FR-24..FR-29, NFR-1..NFR-6).

This is the layer that stands between a decision and someone's bank account.

Design commitment: every monetary limit is enforced HERE, in Python, by an
integer comparison. None of it is expressed as an instruction to a language
model. A prompt can be ignored, misread, or talked around. `if attempts >=
cap: refuse` cannot.

The executor also holds the system's single most important invariant:

    NO-DOUBLE-CHARGE -- no sequence of events, retries, duplicate webhooks,
    or process restarts may cause one order to be charged twice.

It is enforced by (a) a terminal RECOVERED state that refuses all further
action, and (b) an idempotency ledger keyed on (case_id, seq) so a replayed
event re-reads its previous outcome instead of performing a new charge.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

from ..config import PolicyConfig
from ..domain import (
    Action, Attempt, Case, CaseState, Method, NEVER_RETRY, utcnow,
)


class Refusal(str):
    """Reason an action was refused. Refusals are events, not silence (NFR-29)."""


#: Guards that mean "not yet", not "never". A case blocked by one of these
#: is rescheduled, not closed. Confusing the two silently destroys recovery
#: rate -- a cooldown is a wait instruction, not a verdict.
DEFERRABLE_GUARDS: frozenset[str] = frozenset({"CAP-COOLDOWN", "COMP-QUIET"})


@dataclass
class ExecutionResult:
    executed: bool
    succeeded: bool
    refused_reason: Optional[str]
    guard: Optional[str]          # which guard blocked it
    cost_paise: int
    note: str = ""
    #: For deferrable guards: the earliest time this action becomes legal.
    retry_after: Optional[datetime] = None

    @property
    def is_deferrable(self) -> bool:
        return self.guard in DEFERRABLE_GUARDS and self.retry_after is not None


class CustomerLedger:
    """
    Tracks contact frequency per customer across ALL of their payments
    (FR-25). Per-payment caps alone are not enough: three failed orders each
    allowed three touches means nine messages to one irritated human.
    """

    def __init__(self) -> None:
        self._touches: dict[str, list[datetime]] = {}
        self._opted_out: set[str] = set()

    def record(self, customer_id: str, at: datetime) -> None:
        self._touches.setdefault(customer_id, []).append(at)

    def touches_in_window(self, customer_id: str, at: datetime, hours: int = 24) -> int:
        cutoff = at - timedelta(hours=hours)
        return sum(1 for t in self._touches.get(customer_id, []) if t >= cutoff)

    def opt_out(self, customer_id: str) -> None:
        self._opted_out.add(customer_id)

    def is_opted_out(self, customer_id: str) -> bool:
        return customer_id in self._opted_out

    def reset(self) -> None:
        self._touches.clear()


class BoundedExecutor:
    """
    `perform` is the side-effecting callable that actually attempts money
    movement. In simulation it is the World; in live mode it wraps the
    Razorpay client. The executor does not care which -- it only guarantees
    that `perform` is never called when a guard says no.
    """

    def __init__(
        self,
        config: PolicyConfig,
        perform: Callable[[Case, Action, Method, datetime, int], bool],
        ledger: Optional[CustomerLedger] = None,
    ):
        self.cfg = config
        self.perform = perform
        self.ledger = ledger or CustomerLedger()
        #: idempotency ledger: (case_id, seq) -> result already produced
        self._idem: dict[tuple[str, int], ExecutionResult] = {}
        self.refusals: list[dict] = []

    # ------------------------------------------------------------- guards
    def _guards(
        self, case: Case, action: Action, at: datetime
    ) -> Optional[tuple[str, str, Optional[datetime]]]:
        """
        Return (guard_id, reason, retry_after) if the action must be refused.
        `retry_after` is non-None only for deferrable guards.
        """
        cfg = self.cfg
        p = case.payment

        if cfg.kill_switch:
            return ("KILL", "Global kill switch is engaged; no actions are executing.", None)

        # NO-DOUBLE-CHARGE, part 1: a recovered case accepts nothing further.
        #
        # This deliberately checks the MONEY, not just the state flag. State is
        # set by the caller; `recovered_paise` and the attempt log are facts.
        # An invariant that depends on the caller having remembered to update a
        # field is not an invariant.
        if case.state is CaseState.RECOVERED or case.recovered_paise > 0 \
                or any(a.succeeded for a in case.attempts):
            return ("INV-NDC", "Case already recovered. Refusing any further money action.", None)
        if case.state is CaseState.ABANDONED:
            return ("INV-TERM", "Case is closed. Refusing to reopen.", None)

        if action is Action.STOP:
            return None          # stopping is always permitted

        if cfg.respect_opt_out and (p.customer.opted_out or self.ledger.is_opted_out(p.customer.customer_id)):
            return ("COMP-OPTOUT", "Customer has opted out of contact.", None)

        if len(case.attempts) >= cfg.max_attempts_per_payment:
            return ("CAP-ATTEMPTS",
                    f"Payment already had {len(case.attempts)} attempts "
                    f"(cap {cfg.max_attempts_per_payment}).", None)

        touches = self.ledger.touches_in_window(p.customer.customer_id, at)
        if touches >= cfg.max_touches_per_customer_24h:
            return ("CAP-CUSTOMER",
                    f"Customer has had {touches} touches in 24h "
                    f"(cap {cfg.max_touches_per_customer_24h}).", None)

        if case.attempts:
            since = (at - case.attempts[-1].executed_at).total_seconds() / 60.0
            if since < cfg.min_cooldown_minutes:
                clears = case.attempts[-1].executed_at + timedelta(minutes=cfg.min_cooldown_minutes)
                return ("CAP-COOLDOWN",
                        f"Only {since:.0f} min since the last attempt "
                        f"(cooldown {cfg.min_cooldown_minutes} min). Deferring to "
                        f"{clears.isoformat(timespec='minutes')}.",
                        clears)

        # Never retry a dead instrument on the same rail.
        if case.classification and case.classification.subcause in NEVER_RETRY:
            if action in (Action.RETRY_NOW, Action.SCHEDULE_RETRY):
                return ("INV-NEVERRETRY",
                        f"{case.classification.subcause.value} is on the never-retry list; "
                        f"a same-rail retry is structurally forbidden.", None)

        # Economic ceiling.
        prospective = case.cost_paise + cfg.cost_of(action)
        if prospective > cfg.spend_ceiling_paise(p.amount_paise):
            return ("CAP-SPEND",
                    f"Would spend Rs {prospective/100:.2f} on a Rs {p.amount_rupees:.0f} "
                    f"payment, past the {cfg.max_spend_ratio:.0%} ceiling.", None)

        # Quiet hours apply to anything that reaches the customer.
        if action is Action.PAYMENT_LINK:
            local_hour = (at.hour + cfg.timezone_offset_hours) % 24
            if local_hour >= cfg.quiet_hours_start or local_hour < cfg.quiet_hours_end:
                hours_to_open = (cfg.quiet_hours_end - local_hour) % 24
                clears = at + timedelta(hours=hours_to_open)
                return ("COMP-QUIET",
                        f"Local time {local_hour:04.1f}h falls inside quiet hours "
                        f"({cfg.quiet_hours_start}:00-{cfg.quiet_hours_end}:00). Holding "
                        f"until {clears.isoformat(timespec='minutes')}.",
                        clears)

        return None

    # ------------------------------------------------------------- execute
    def execute(
        self,
        case: Case,
        action: Action,
        method: Method,
        at: datetime,
    ) -> ExecutionResult:
        seq = len(case.attempts) + 1
        idem_key = (case.case_id, seq)

        # NFR-1: at-least-once delivery must not produce at-least-once effects.
        if idem_key in self._idem:
            prior = self._idem[idem_key]
            return ExecutionResult(**{**prior.__dict__, "note": "idempotent replay"})

        blocked = self._guards(case, action, at)
        if blocked:
            guard, reason, retry_after = blocked
            self.refusals.append({
                "case_id": case.case_id, "guard": guard,
                "action": action.value, "reason": reason,
                "at": at.isoformat(),
            })
            result = ExecutionResult(False, False, reason, guard, 0,
                                     retry_after=retry_after)
            # A deferral is not a decision -- do not burn the idempotency slot,
            # or the rescheduled attempt would replay the refusal forever.
            if not result.is_deferrable:
                self._idem[idem_key] = result
            return result

        if action is Action.STOP:
            result = ExecutionResult(True, False, None, None, 0, "stopped deliberately")
            self._idem[idem_key] = result
            return result

        cost = self.cfg.cost_of(action)

        # FR-29: dry run walks the entire pipeline and touches nothing.
        if self.cfg.dry_run:
            result = ExecutionResult(True, False, None, None, cost, "dry-run: no side effect")
            self._idem[idem_key] = result
            return result

        succeeded = self.perform(case, action, method, at, seq)

        if action in (Action.PAYMENT_LINK, Action.ESCALATE):
            self.ledger.record(case.payment.customer.customer_id, at)

        case.attempts.append(Attempt(
            case_id=case.case_id, seq=seq, action=action, method=method,
            executed_at=at, succeeded=succeeded, cost_paise=cost,
        ))
        case.cost_paise += cost

        if succeeded:
            case.recovered_paise = case.payment.amount_paise

        result = ExecutionResult(True, succeeded, None, None, cost)
        self._idem[idem_key] = result
        return result

    # ------------------------------------------------------------- helpers
    def reset(self) -> None:
        self._idem.clear()
        self.refusals.clear()
        self.ledger.reset()
