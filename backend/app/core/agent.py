"""
The agent loop.

Orchestrates the four layers -- ingest, classify, decide, execute -- and
advances each case through its state machine until it reaches a terminal
state or runs out of recovery window.

Time is injected, never read from the clock. That is what lets a 72-hour
recovery campaign be evaluated in under a second, and it is also what makes
replay exact.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Optional

from ..config import PolicyConfig
from ..domain import (
    Action, Case, CaseState, Classification, Decision, FailedPayment, Method,
)
from .audit import AuditLog, config_fingerprint
from .classifier import Classifier
from .downtime import DowntimeOracle
from .executor import BoundedExecutor, CustomerLedger
from .policy import PolicyEngine


class RecoveryAgent:
    def __init__(
        self,
        config: PolicyConfig,
        classifier: Classifier,
        perform: Callable[[Case, Action, Method, datetime, int], bool],
        downtime: Optional[DowntimeOracle] = None,
        audit: Optional[AuditLog] = None,
        policy_cls=None,
    ):
        self.cfg = config
        self.classifier = classifier
        self.downtime = downtime or DowntimeOracle()
        self.policy = (policy_cls or PolicyEngine)(config, self.downtime)
        self.ledger = CustomerLedger()
        self.executor = BoundedExecutor(config, perform, self.ledger)
        self.audit = audit or AuditLog(
            policy_version=config.version,
            config_hash=config_fingerprint(config),
        )
        self.cases: dict[str, Case] = {}
        self._deferred_until: Optional[datetime] = None

    def apply_config(self, config: PolicyConfig) -> None:
        """
        Push a config change into every layer that holds a copy.

        Each layer captures config by value at construction, which is right for
        reproducible batch runs -- a run must not change under its own feet.
        But it means a long-lived agent keeps a stale copy forever unless the
        change is propagated explicitly. For an ordinary setting that would be
        an inconvenience; for the kill switch it is a safety control that
        silently does nothing. Hence this method, and the test that covers it.
        """
        self.cfg = config
        self.policy.cfg = config
        self.executor.cfg = config
        self.audit.policy_version = config.version
        self.audit.config_hash = config_fingerprint(config)

    # ------------------------------------------------------------- ingest
    def ingest(self, payment: FailedPayment) -> Case:
        """FR-1. Idempotent: the same payment_id never creates a second case."""
        if payment.payment_id in self.cases:
            return self.cases[payment.payment_id]

        case = Case(case_id=payment.payment_id, payment=payment)
        self.cases[case.case_id] = case
        self.audit.append(
            case.case_id, "detected",
            f"Detected failed payment of Rs {payment.amount_rupees:,.0f} on "
            f"{payment.method.value} via {payment.bank} ({payment.error_code}).",
            detail={"amount_paise": payment.amount_paise,
                    "error_code": payment.error_code,
                    "method": payment.method.value,
                    "bank": payment.bank},
            at=payment.failed_at,
        )
        return case

    # ----------------------------------------------------------- one cycle
    def step(self, case: Case, now: datetime) -> bool:
        """
        Advance a case by one decision-and-action. Returns True if the case
        is still open afterwards.
        """
        if case.is_terminal:
            return False

        # --- classify (once) ---------------------------------------------
        if case.classification is None:
            c: Classification = self.classifier.classify(case.payment)
            case.classification = c
            case.transition(CaseState.CLASSIFIED)
            self.audit.append(
                case.case_id, "classified",
                f"Classified as {c.cause.value.replace('_',' ')} / "
                f"{c.subcause.value.replace('_',' ')} at {c.confidence:.0%} confidence. {c.reason}",
                detail={"cause": c.cause.value, "subcause": c.subcause.value,
                        "confidence": c.confidence, "source": c.source,
                        "latency_ms": round(c.latency_ms, 2)},
                at=now,
            )

        # --- decide --------------------------------------------------------
        decision: Decision = self.policy.decide(case, now)
        case.decisions.append(decision)
        if case.state in (CaseState.CLASSIFIED, CaseState.IN_FLIGHT, CaseState.SCHEDULED):
            case.transition(CaseState.DECIDED)

        self.audit.append(
            case.case_id, "decided",
            f"[{decision.policy_rule}] {decision.action.value.replace('_',' ')}"
            + (f" via {decision.alt_method.value}" if decision.alt_method else "")
            + f" - {decision.reason}",
            detail={"action": decision.action.value, "rule": decision.policy_rule,
                    "delay_hours": decision.delay_hours,
                    "alt_method": decision.alt_method.value if decision.alt_method else None},
            at=now,
        )

        # --- terminal decisions -------------------------------------------
        if decision.action is Action.STOP:
            case.transition(CaseState.ABANDONED)
            case.closed_reason = decision.reason
            self.audit.append(case.case_id, "closed",
                              f"Stopped: {decision.reason}", at=now)
            return False

        if decision.action is Action.ESCALATE:
            case.transition(CaseState.ESCALATED)
            case.closed_reason = decision.reason
            self.audit.append(case.case_id, "closed",
                              f"Escalated for human review: {decision.reason}", at=now)
            return False

        # --- scheduling -----------------------------------------------------
        exec_at = now + timedelta(hours=decision.delay_hours)
        window_end = case.payment.failed_at + timedelta(hours=self.cfg.max_recovery_window_hours)
        if exec_at > window_end:
            case.transition(CaseState.ABANDONED)
            case.closed_reason = "Scheduled retry would land outside the recovery window."
            self.audit.append(case.case_id, "closed", case.closed_reason, at=now)
            return False

        method = decision.alt_method or case.payment.method

        # --- execute ---------------------------------------------------------
        case.transition(CaseState.IN_FLIGHT)
        result = self.executor.execute(case, decision.action, method, exec_at)

        if not result.executed:
            case.transition(CaseState.BLOCKED)
            self.audit.append(
                case.case_id, "refused",
                f"Refused by guard {result.guard}: {result.refused_reason}",
                detail={"guard": result.guard, "action": decision.action.value,
                        "deferrable": result.is_deferrable,
                        "retry_after": result.retry_after.isoformat() if result.retry_after else None},
                at=exec_at,
            )

            # A cooldown or quiet-hours block means "not yet", not "never".
            # Closing the case here is how a recovery agent quietly loses
            # most of its money.
            if result.is_deferrable:
                window_end = case.payment.failed_at + timedelta(
                    hours=self.cfg.max_recovery_window_hours)
                if result.retry_after <= window_end:
                    case.transition(CaseState.DECIDED)
                    self._deferred_until = result.retry_after
                    return True

            case.transition(CaseState.ABANDONED)
            case.closed_reason = result.refused_reason or "blocked"
            self.audit.append(case.case_id, "closed", case.closed_reason, at=exec_at)
            return False

        self.audit.append(
            case.case_id, "executed",
            f"{decision.action.value.replace('_',' ')} on {method.value}: "
            f"{'recovered Rs %s' % f'{case.payment.amount_rupees:,.0f}' if result.succeeded else 'no recovery'}"
            f" (cost Rs {result.cost_paise/100:.2f}).",
            detail={"action": decision.action.value, "method": method.value,
                    "succeeded": result.succeeded, "cost_paise": result.cost_paise,
                    "seq": len(case.attempts)},
            at=exec_at,
        )

        if result.succeeded:
            case.transition(CaseState.RECOVERED)
            case.closed_reason = f"Recovered on attempt {len(case.attempts)}."
            self.audit.append(case.case_id, "closed", case.closed_reason, at=exec_at)
            return False

        return True

    # ------------------------------------------------------------ run loop
    def run_case(self, payment: FailedPayment, max_cycles: int = 10) -> Case:
        case = self.ingest(payment)
        now = payment.failed_at
        self._deferred_until = None

        for _ in range(max_cycles):
            still_open = self.step(case, now)
            if not still_open:
                break

            if self._deferred_until is not None:
                # A guard told us exactly when this becomes legal. Wait for it.
                now = self._deferred_until
                self._deferred_until = None
            elif case.attempts:
                # Respect the cooldown proactively rather than walking into it.
                now = case.attempts[-1].executed_at + timedelta(
                    minutes=self.cfg.min_cooldown_minutes + 1)
            else:
                now = now + timedelta(minutes=1)
        if not case.is_terminal and case.state is not CaseState.ESCALATED:
            case.transition(CaseState.ABANDONED)
            case.closed_reason = "Exhausted recovery cycles."
        return case

    def run_batch(self, payments: list[FailedPayment]) -> list[Case]:
        return [self.run_case(p) for p in payments]
