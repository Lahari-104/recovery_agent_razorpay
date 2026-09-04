"""
Evaluation harness (FR-30..FR-36).

Three policies over one identical batch:

  NAIVE   what most merchants do now: retry everything, immediately, 3x.
  AGENT   classify, choose, bound, stop.
  ORACLE  perfect knowledge of the hidden truth -- the theoretical ceiling.

Reporting the oracle is the point. A recovery number alone invites the
question "is that good?" and has no answer. A number between a baseline and a
ceiling answers it before it is asked, and admitting the remaining gap is
more credible than any single figure.

Fairness is structural, not promised: all three policies draw from the same
pre-generated per-case random stream, so identical actions at identical times
produce identical outcomes. Differences come from decisions, never luck.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Any, Optional

from ..config import NAIVE_CONFIG, PolicyConfig
from ..core.agent import RecoveryAgent
from ..core.audit import AuditLog, config_fingerprint
from ..core.classifier import Classifier
from ..core.downtime import DowntimeOracle
from ..core.policy import NaivePolicyEngine, PolicyEngine
from ..domain import Action, Case, CaseState, FailedPayment, Method
from ..sim.world import RecoveryProfile, World


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------

@dataclass
class PolicyResult:
    name: str
    cases: int
    at_risk_paise: int
    recovered_paise: int
    spend_paise: int
    recovered_count: int
    escalated_count: int
    abandoned_count: int
    attempts_total: int
    refusals: int
    by_cause: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_action: dict[str, dict[str, Any]] = field(default_factory=dict)
    audit_digest: str = ""

    @property
    def net_paise(self) -> int:
        return self.recovered_paise - self.spend_paise

    @property
    def recovery_rate(self) -> float:
        return self.recovered_paise / self.at_risk_paise if self.at_risk_paise else 0.0

    @property
    def hit_rate(self) -> float:
        return self.recovered_count / self.cases if self.cases else 0.0

    @property
    def cost_per_recovery_paise(self) -> float:
        return self.spend_paise / self.recovered_count if self.recovered_count else 0.0

    @property
    def roi(self) -> float:
        return self.recovered_paise / self.spend_paise if self.spend_paise else 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.update({
            "net_paise": self.net_paise,
            "recovery_rate": round(self.recovery_rate, 4),
            "hit_rate": round(self.hit_rate, 4),
            "cost_per_recovery_paise": round(self.cost_per_recovery_paise, 2),
            "roi": round(self.roi, 2),
            "recovered_rupees": round(self.recovered_paise / 100, 2),
            "spend_rupees": round(self.spend_paise / 100, 2),
            "net_rupees": round(self.net_paise / 100, 2),
            "at_risk_rupees": round(self.at_risk_paise / 100, 2),
        })
        return d


@dataclass
class Exception_:
    """One case the agent could not recover, and why. FR-35."""
    case_id: str
    amount_rupees: float
    error_code: str
    cause: str
    subcause: str
    confidence: float
    final_state: str
    attempts: int
    spend_rupees: float
    reason: str
    truth_profile: str          # only shown in eval mode, never in live


# --------------------------------------------------------------------------
# World-backed executor callable
# --------------------------------------------------------------------------

def make_world_perform(world: World):
    """Adapts the simulated World into the executor's `perform` signature."""
    def perform(case: Case, action: Action, method: Method,
                at: datetime, seq: int) -> bool:
        elapsed = (at - case.payment.failed_at).total_seconds() / 3600.0
        return world.attempt(
            case_id=case.case_id, seq=seq, action=action,
            elapsed_hours=elapsed, method_used=method,
            original_method=case.payment.method,
        )
    return perform


# --------------------------------------------------------------------------
# Oracle
# --------------------------------------------------------------------------

def run_oracle(payments: list[FailedPayment], world: World,
               cfg: PolicyConfig) -> PolicyResult:
    """
    Perfect play under the SAME operational limits the agent obeys: same
    attempt cap, same costs, same never-retry rules. The oracle is not
    omnipotent, only omniscient -- it always picks the single best legal
    action, but it cannot exceed the caps.

    This keeps the ceiling honest. An unconstrained oracle would just be an
    upper bound on the data, not on achievable performance.
    """
    recovered = spend = attempts_total = 0
    recovered_count = 0
    at_risk = sum(p.amount_paise for p in payments)

    for p in payments:
        gt = world.truth(p.payment_id)
        if gt.profile is RecoveryProfile.UNRECOVERABLE:
            continue
        if p.customer.opted_out and cfg.respect_opt_out:
            continue
        if p.amount_paise < cfg.min_amount_to_chase_paise:
            continue

        best: Optional[tuple[float, Action, Method, float, int]] = None

        # Search the legal action space with perfect knowledge.
        candidates: list[tuple[Action, Method, float]] = []
        for delay in (0.0, 0.5, 1.5, 3.0, 6.0, 12.0, gt.ready_after_hours + 0.5, 24.0, 30.0):
            if delay < 0 or delay > cfg.max_recovery_window_hours:
                continue
            candidates.append((Action.RETRY_NOW if delay == 0 else Action.SCHEDULE_RETRY,
                               p.method, delay))
            alt = gt.working_method or Method.CARD
            if alt != p.method:
                candidates.append((Action.PAYMENT_LINK, alt, delay))
            candidates.append((Action.PAYMENT_LINK, p.method, delay))

        for action, method, delay in candidates:
            prob = world.success_probability(
                p.payment_id, action, delay, method, p.method)
            cost = cfg.cost_of(action)
            if cost > cfg.spend_ceiling_paise(p.amount_paise):
                continue
            ev = prob * p.amount_paise - cost
            if best is None or ev > best[0]:
                best = (ev, action, method, delay, cost)

        if best is None or best[0] <= 0:
            continue

        _, action, method, delay, cost = best
        spend += cost
        attempts_total += 1
        if world.attempt(p.payment_id, 1, action, delay, method, p.method):
            recovered += p.amount_paise
            recovered_count += 1

    return PolicyResult(
        name="oracle", cases=len(payments), at_risk_paise=at_risk,
        recovered_paise=recovered, spend_paise=spend,
        recovered_count=recovered_count, escalated_count=0,
        abandoned_count=len(payments) - recovered_count,
        attempts_total=attempts_total, refusals=0,
    )


# --------------------------------------------------------------------------
# Agent / naive runs
# --------------------------------------------------------------------------

def run_policy(
    name: str,
    payments: list[FailedPayment],
    world: World,
    cfg: PolicyConfig,
    classifier: Classifier,
    keep_cases: bool = False,
    policy_cls=None,
) -> tuple[PolicyResult, list[Case], AuditLog]:
    agent = RecoveryAgent(
        config=cfg,
        classifier=classifier,
        perform=make_world_perform(world),
        downtime=DowntimeOracle(seed=world.seed),
        audit=AuditLog(policy_version=cfg.version, config_hash=config_fingerprint(cfg)),
        policy_cls=policy_cls or PolicyEngine,
    )
    cases = agent.run_batch(payments)

    by_cause: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "recovered": 0, "at_risk_paise": 0,
                 "recovered_paise": 0, "spend_paise": 0})
    by_action: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"used": 0, "succeeded": 0, "spend_paise": 0})

    for c in cases:
        key = c.classification.cause.value if c.classification else "unknown"
        b = by_cause[key]
        b["count"] += 1
        b["at_risk_paise"] += c.payment.amount_paise
        b["spend_paise"] += c.cost_paise
        if c.state is CaseState.RECOVERED:
            b["recovered"] += 1
            b["recovered_paise"] += c.recovered_paise
        for a in c.attempts:
            ba = by_action[a.action.value]
            ba["used"] += 1
            ba["spend_paise"] += a.cost_paise
            if a.succeeded:
                ba["succeeded"] += 1

    result = PolicyResult(
        name=name,
        cases=len(cases),
        at_risk_paise=sum(p.amount_paise for p in payments),
        recovered_paise=sum(c.recovered_paise for c in cases),
        spend_paise=sum(c.cost_paise for c in cases),
        recovered_count=sum(1 for c in cases if c.state is CaseState.RECOVERED),
        escalated_count=sum(1 for c in cases if c.state is CaseState.ESCALATED),
        abandoned_count=sum(1 for c in cases if c.state is CaseState.ABANDONED),
        attempts_total=sum(len(c.attempts) for c in cases),
        refusals=len(agent.executor.refusals),
        by_cause=dict(by_cause),
        by_action=dict(by_action),
        audit_digest=agent.audit.digest(),
    )
    return result, (cases if keep_cases else []), agent.audit


def build_exceptions(cases: list[Case], world: World, limit: int = 200) -> list[Exception_]:
    out: list[Exception_] = []
    for c in cases:
        if c.state is CaseState.RECOVERED:
            continue
        cls = c.classification
        out.append(Exception_(
            case_id=c.case_id,
            amount_rupees=round(c.payment.amount_rupees, 2),
            error_code=c.payment.error_code,
            cause=cls.cause.value if cls else "unknown",
            subcause=cls.subcause.value if cls else "unknown",
            confidence=round(cls.confidence, 3) if cls else 0.0,
            final_state=c.state.value,
            attempts=len(c.attempts),
            spend_rupees=round(c.cost_paise / 100, 2),
            reason=c.closed_reason or "n/a",
            truth_profile=world.truth(c.case_id).profile.value,
        ))
    out.sort(key=lambda e: -e.amount_rupees)
    return out[:limit]


# --------------------------------------------------------------------------
# Full comparison
# --------------------------------------------------------------------------

@dataclass
class Comparison:
    naive: PolicyResult
    agent: PolicyResult
    oracle: PolicyResult
    exceptions: list[Exception_]
    cases: list[Case] = field(default_factory=list)
    audit: Optional[AuditLog] = None

    def summary(self) -> dict[str, Any]:
        cap = self.oracle.net_paise or 1
        return {
            "naive": self.naive.to_dict(),
            "agent": self.agent.to_dict(),
            "oracle": self.oracle.to_dict(),
            "uplift_net_rupees": round((self.agent.net_paise - self.naive.net_paise) / 100, 2),
            "uplift_pct": round(
                ((self.agent.net_paise - self.naive.net_paise) / abs(self.naive.net_paise) * 100)
                if self.naive.net_paise else 0.0, 1),
            "capture_of_ceiling_pct": round(self.agent.net_paise / cap * 100, 1),
            "exception_count": len(self.exceptions),
        }


def run_comparison(
    payments: list[FailedPayment],
    world: World,
    cfg: PolicyConfig,
    classifier: Optional[Classifier] = None,
) -> Comparison:
    clf = classifier or Classifier(llm_client=None)

    naive_res, _, _ = run_policy("naive", payments, world, NAIVE_CONFIG, clf,
                                 policy_cls=NaivePolicyEngine)
    agent_res, cases, audit = run_policy("agent", payments, world, cfg, clf, keep_cases=True)
    oracle_res = run_oracle(payments, world, cfg)

    return Comparison(
        naive=naive_res, agent=agent_res, oracle=oracle_res,
        exceptions=build_exceptions(cases, world),
        cases=cases, audit=audit,
    )
