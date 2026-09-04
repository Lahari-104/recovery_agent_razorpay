"""
The simulated payment world.

WHY THIS EXISTS
---------------
In Razorpay test mode no real money moves, so "money recovered" cannot be
measured by actually recovering money. Instead we build a world where
recoverability is *knowable but hidden*:

  * Every synthetic failure is assigned a RecoveryProfile -- the ground truth
    of what would actually have worked.
  * The agent never sees the profile. It sees only what a real
    `payment.failed` webhook carries.
  * The world scores each attempted intervention against the profile.

FAIRNESS
--------
Every policy (naive / agent / oracle) faces an identical stream of random
draws, pre-generated per case and indexed by attempt number. If two policies
take the same action at the same time, they get the same outcome. Differences
in recovery are therefore caused by *decisions*, never by luck. (NFR-30..32)
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from ..domain import (
    Action, Cause, CustomerProfile, FailedPayment, Method, SubCause,
    ERROR_CODE_MAP,
)

IST = timezone(timedelta(hours=5, minutes=30))


class RecoveryProfile(str, Enum):
    """Hidden truth: what it would actually take to recover this payment."""
    IMMEDIATE = "immediate"          # transient; retrying right away works
    DELAYED = "delayed"              # works only after funds/downtime clears
    METHOD_SWITCH = "method_switch"  # this rail is dead; another one works
    NUDGE = "nudge"                  # customer just needs reminding
    UNRECOVERABLE = "unrecoverable"  # nothing works, ever


@dataclass(frozen=True)
class GroundTruth:
    profile: RecoveryProfile
    #: Hours that must elapse before recovery becomes possible.
    ready_after_hours: float
    #: Ceiling probability once conditions are met.
    peak_success_prob: float
    #: Method that actually works (None = original method is fine).
    working_method: Optional[Method]
    #: How willing this customer is to respond to a nudge.
    nudge_responsiveness: float


# Which profiles each subcause can plausibly have, with weights.
_PROFILE_WEIGHTS: dict[SubCause, list[tuple[RecoveryProfile, float]]] = {
    SubCause.INSUFFICIENT_FUNDS: [(RecoveryProfile.DELAYED, 0.72), (RecoveryProfile.UNRECOVERABLE, 0.28)],
    SubCause.BANK_DOWNTIME:      [(RecoveryProfile.DELAYED, 0.65), (RecoveryProfile.METHOD_SWITCH, 0.30), (RecoveryProfile.UNRECOVERABLE, 0.05)],
    SubCause.ISSUER_UNAVAILABLE: [(RecoveryProfile.DELAYED, 0.55), (RecoveryProfile.METHOD_SWITCH, 0.35), (RecoveryProfile.UNRECOVERABLE, 0.10)],
    SubCause.NETWORK_TIMEOUT:    [(RecoveryProfile.IMMEDIATE, 0.80), (RecoveryProfile.UNRECOVERABLE, 0.20)],
    SubCause.GATEWAY_ERROR:      [(RecoveryProfile.IMMEDIATE, 0.70), (RecoveryProfile.DELAYED, 0.20), (RecoveryProfile.UNRECOVERABLE, 0.10)],
    SubCause.RATE_LIMITED:       [(RecoveryProfile.DELAYED, 0.85), (RecoveryProfile.UNRECOVERABLE, 0.15)],

    SubCause.CARD_EXPIRED:        [(RecoveryProfile.METHOD_SWITCH, 0.45), (RecoveryProfile.UNRECOVERABLE, 0.55)],
    SubCause.CARD_BLOCKED:        [(RecoveryProfile.METHOD_SWITCH, 0.30), (RecoveryProfile.UNRECOVERABLE, 0.70)],
    SubCause.INVALID_ACCOUNT:     [(RecoveryProfile.UNRECOVERABLE, 1.0)],
    SubCause.ACCOUNT_FROZEN:      [(RecoveryProfile.UNRECOVERABLE, 1.0)],
    SubCause.RISK_BLOCKED:        [(RecoveryProfile.UNRECOVERABLE, 1.0)],
    SubCause.METHOD_NOT_SUPPORTED:[(RecoveryProfile.METHOD_SWITCH, 0.60), (RecoveryProfile.UNRECOVERABLE, 0.40)],
    SubCause.MANDATE_REVOKED:     [(RecoveryProfile.UNRECOVERABLE, 1.0)],

    SubCause.OTP_NOT_ENTERED:    [(RecoveryProfile.NUDGE, 0.75), (RecoveryProfile.UNRECOVERABLE, 0.25)],
    SubCause.COLLECT_EXPIRED:    [(RecoveryProfile.NUDGE, 0.80), (RecoveryProfile.UNRECOVERABLE, 0.20)],
    SubCause.CANCELLED_BY_USER:  [(RecoveryProfile.NUDGE, 0.45), (RecoveryProfile.UNRECOVERABLE, 0.55)],
    SubCause.WINDOW_CLOSED:      [(RecoveryProfile.NUDGE, 0.70), (RecoveryProfile.UNRECOVERABLE, 0.30)],
    SubCause.INCORRECT_VPA:      [(RecoveryProfile.METHOD_SWITCH, 0.50), (RecoveryProfile.NUDGE, 0.35), (RecoveryProfile.UNRECOVERABLE, 0.15)],
}

_READY_HOURS = {
    RecoveryProfile.IMMEDIATE: (0.0, 0.0),
    RecoveryProfile.DELAYED: (1.0, 26.0),
    RecoveryProfile.METHOD_SWITCH: (0.0, 0.5),
    RecoveryProfile.NUDGE: (0.0, 1.0),
    RecoveryProfile.UNRECOVERABLE: (0.0, 0.0),
}

_PEAK_PROB = {
    RecoveryProfile.IMMEDIATE: (0.62, 0.88),
    RecoveryProfile.DELAYED: (0.55, 0.85),
    RecoveryProfile.METHOD_SWITCH: (0.50, 0.80),
    RecoveryProfile.NUDGE: (0.30, 0.62),
    RecoveryProfile.UNRECOVERABLE: (0.0, 0.0),
}


def _weighted(rng: random.Random, choices: list[tuple[RecoveryProfile, float]]) -> RecoveryProfile:
    total = sum(w for _, w in choices)
    r = rng.random() * total
    acc = 0.0
    for item, w in choices:
        acc += w
        if r <= acc:
            return item
    return choices[-1][0]


class World:
    """
    Holds ground truth for a batch and adjudicates attempted interventions.

    The draw stream is keyed by (case_id, attempt_seq) and derived from a
    SHA-256 of the seed, so it is stable across processes and policy runs.
    """

    def __init__(self, seed: int = 20260824):
        self.seed = seed
        self._truth: dict[str, GroundTruth] = {}

    # -- ground truth ------------------------------------------------------

    def assign(self, case_id: str, subcause: SubCause, customer: CustomerProfile) -> GroundTruth:
        rng = random.Random(f"{self.seed}:{case_id}:truth")
        weights = _PROFILE_WEIGHTS.get(subcause, [(RecoveryProfile.UNRECOVERABLE, 1.0)])
        profile = _weighted(rng, weights)

        lo, hi = _READY_HOURS[profile]
        ready = round(rng.uniform(lo, hi), 2)

        plo, phi = _PEAK_PROB[profile]
        peak = round(rng.uniform(plo, phi), 3)

        # Reliable payers are meaningfully easier to recover.
        peak = min(0.95, peak * (1.0 + 0.35 * (1.0 - customer.failure_rate)))

        working = None
        if profile is RecoveryProfile.METHOD_SWITCH:
            working = rng.choice([m for m in Method])

        responsiveness = round(min(0.9, rng.uniform(0.2, 0.7) * (1.0 + 0.5 * (1 - customer.failure_rate))), 3)

        gt = GroundTruth(profile, ready, peak, working, responsiveness)
        self._truth[case_id] = gt
        return gt

    def truth(self, case_id: str) -> GroundTruth:
        return self._truth[case_id]

    # -- adjudication ------------------------------------------------------

    def success_probability(
        self,
        case_id: str,
        action: Action,
        elapsed_hours: float,
        method_used: Method,
        original_method: Method,
    ) -> float:
        """Probability this specific intervention recovers the payment."""
        gt = self._truth[case_id]

        if gt.profile is RecoveryProfile.UNRECOVERABLE or action is Action.STOP:
            return 0.0

        if action is Action.ESCALATE:
            # A human can sometimes rescue what automation cannot.
            return 0.0 if gt.profile is RecoveryProfile.UNRECOVERABLE else gt.peak_success_prob * 0.55

        switched = method_used != original_method

        if gt.profile is RecoveryProfile.IMMEDIATE:
            # Decays: the transient window closes.
            decay = max(0.25, 1.0 - 0.08 * elapsed_hours)
            base = gt.peak_success_prob * decay

        elif gt.profile is RecoveryProfile.DELAYED:
            if elapsed_hours < gt.ready_after_hours:
                # Too early. Small chance, and you burn an attempt.
                base = gt.peak_success_prob * 0.12
            else:
                overshoot = elapsed_hours - gt.ready_after_hours
                base = gt.peak_success_prob * max(0.6, 1.0 - 0.015 * overshoot)

        elif gt.profile is RecoveryProfile.METHOD_SWITCH:
            if switched and (gt.working_method is None or method_used == gt.working_method):
                base = gt.peak_success_prob
            elif switched:
                base = gt.peak_success_prob * 0.35
            else:
                base = 0.03   # same dead rail

        elif gt.profile is RecoveryProfile.NUDGE:
            if action is Action.PAYMENT_LINK:
                base = gt.peak_success_prob * gt.nudge_responsiveness / 0.5
            else:
                # Silent retry on an abandoned checkout rarely works.
                base = gt.peak_success_prob * 0.10
        else:
            base = 0.0

        # A payment link carries a small intrinsic lift: it gives the customer
        # choice of rail and a fresh session.
        if action is Action.PAYMENT_LINK and gt.profile is not RecoveryProfile.NUDGE:
            base *= 1.10

        return max(0.0, min(0.97, base))

    def draw(self, case_id: str, seq: int) -> float:
        """Deterministic uniform draw for attempt `seq` of `case_id`."""
        h = hashlib.sha256(f"{self.seed}:{case_id}:{seq}".encode()).hexdigest()
        return int(h[:12], 16) / float(16 ** 12)

    def attempt(
        self,
        case_id: str,
        seq: int,
        action: Action,
        elapsed_hours: float,
        method_used: Method,
        original_method: Method,
    ) -> bool:
        p = self.success_probability(case_id, action, elapsed_hours, method_used, original_method)
        return self.draw(case_id, seq) < p


# --------------------------------------------------------------------------
# Batch generation
# --------------------------------------------------------------------------

_BANKS = ["HDFC", "ICICI", "SBI", "Axis", "Kotak", "PNB", "BoB", "Yes", "IndusInd", "Federal"]

_ERROR_TEXT: dict[str, list[str]] = {
    "payment_failed_insufficient_funds": [
        "Your account does not have sufficient balance to complete this transaction.",
        "Transaction declined due to low balance in the linked account.",
    ],
    "payment_upi_bank_down": [
        "The bank's UPI service is temporarily unavailable. Please try later.",
        "Remitter bank is currently not responding to UPI requests.",
    ],
    "issuer_down": ["The card issuing bank is not reachable right now."],
    "payment_timeout": ["The payment request timed out before the bank responded."],
    "network_error": ["A network error occurred while contacting the bank."],
    "gateway_technical_error": ["A technical error occurred at the payment gateway."],
    "payment_declined_try_later": ["The bank declined this payment. Please attempt again later."],
    "too_many_requests": ["Too many payment attempts from this account in a short period."],
    "card_expired": ["The card used for this payment has expired."],
    "card_blocked": ["This card has been blocked by the issuing bank."],
    "invalid_account": ["The account number provided is not valid."],
    "account_frozen": ["The customer's account is frozen and cannot transact."],
    "payment_blocked_risk": ["This transaction was blocked by risk checks."],
    "card_not_supported": ["This card type is not supported for this transaction."],
    "mandate_revoked": ["The recurring mandate for this customer has been revoked."],
    "payment_cancelled_by_user": ["The customer cancelled the payment on the bank page."],
    "upi_collect_expired": ["The UPI collect request expired without customer approval."],
    "otp_not_entered": ["The customer did not enter the OTP within the allowed time."],
    "payment_window_closed": ["The payment window was closed before completion."],
    "incorrect_vpa": ["The UPI ID entered by the customer could not be resolved."],
}

# Realistic mix, weighted toward what actually dominates Indian failure logs.
_CODE_WEIGHTS: list[tuple[str, float]] = [
    ("payment_failed_insufficient_funds", 0.19),
    ("payment_upi_bank_down", 0.13),
    ("payment_timeout", 0.11),
    ("upi_collect_expired", 0.10),
    ("payment_cancelled_by_user", 0.08),
    ("issuer_down", 0.07),
    ("otp_not_entered", 0.06),
    ("gateway_technical_error", 0.05),
    ("card_expired", 0.04),
    ("payment_declined_try_later", 0.04),
    ("network_error", 0.03),
    ("incorrect_vpa", 0.03),
    ("card_blocked", 0.025),
    ("payment_window_closed", 0.02),
    ("payment_blocked_risk", 0.015),
    ("invalid_account", 0.012),
    ("too_many_requests", 0.010),
    ("card_not_supported", 0.008),
    ("account_frozen", 0.005),
    ("mandate_revoked", 0.005),
]

_SOURCE_STEP = {
    Cause.SOFT_DECLINE: ("bank", "payment_authorization"),
    Cause.HARD_DECLINE: ("issuer", "payment_authorization"),
    Cause.USER_DROPOFF: ("customer", "payment_authentication"),
    Cause.UNKNOWN: ("gateway", "payment_response"),
}


def _pick_code(rng: random.Random) -> str:
    r = rng.random()
    acc = 0.0
    for code, w in _CODE_WEIGHTS:
        acc += w
        if r <= acc:
            return code
    return _CODE_WEIGHTS[-1][0]


def generate_batch(
    n: int = 500,
    seed: int = 20260824,
    start: Optional[datetime] = None,
) -> tuple[list[FailedPayment], World]:
    """
    Produce `n` failed payments plus the World holding their hidden truth.

    Amounts follow a long-tailed distribution: many small orders, a few large
    ones. That matters -- a policy that only chases big-ticket failures looks
    very different on gross vs net recovery.
    """
    rng = random.Random(seed)
    world = World(seed=seed)
    start = start or datetime(2026, 8, 18, 6, 0, tzinfo=IST)

    n_customers = max(20, int(n * 0.72))
    customers: list[CustomerProfile] = []
    for i in range(n_customers):
        lifetime = rng.randint(0, 40)
        fails = rng.randint(0, max(1, lifetime // 3))
        customers.append(CustomerProfile(
            customer_id=f"cust_{i:05d}",
            lifetime_payments=lifetime,
            lifetime_failures=fails,
            days_since_last_success=rng.randint(0, 120),
            opted_out=(rng.random() < 0.03),
        ))

    payments: list[FailedPayment] = []
    for i in range(n):
        code = _pick_code(rng)
        cause, subcause = ERROR_CODE_MAP[code]
        src, step = _SOURCE_STEP[cause]
        cust = rng.choice(customers)

        # Long tail: most orders are small, a few are large. The ceiling is
        # deliberately high -- clamping it low makes every large miss the same
        # number, which hides whether the agent prioritises by value at all.
        amount = int(max(2000, min(rng.lognormvariate(11.4, 1.05), 25_000_00)))

        method = rng.choices(
            [Method.UPI, Method.CARD, Method.NETBANKING, Method.WALLET],
            weights=[0.63, 0.22, 0.10, 0.05],
        )[0]

        failed_at = start + timedelta(minutes=rng.randint(0, 6 * 24 * 60))

        p = FailedPayment(
            payment_id=f"pay_SIM{i:06d}",
            order_id=f"order_SIM{i:06d}",
            customer=cust,
            amount_paise=amount,
            method=method,
            bank=rng.choice(_BANKS),
            error_code=code,
            error_description=rng.choice(_ERROR_TEXT[code]),
            error_source=src,
            error_step=step,
            failed_at=failed_at,
            is_synthetic=True,
        )
        payments.append(p)
        world.assign(p.payment_id, subcause, cust)

    payments.sort(key=lambda x: x.failed_at)
    return payments, world
