"""
Intervention policy (FR-19..FR-23).

The policy engine answers: given what we believe about this failure, what is
the right next move?

Every returned Decision carries `policy_rule` -- the identifier of the exact
rule that fired. That is what makes the audit trail meaningful (NFR-6). "The
model decided" is not an audit trail. "Rule SOFT-IF-01 fired because subcause
is insufficient_funds and attempt 1 of 3" is.

The engine is deterministic and pure: same inputs, same output, no I/O. That
is what allows byte-identical replay (NFR-32).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..config import METHOD_FALLBACK, PolicyConfig
from ..domain import (
    Action, Case, Cause, Classification, Decision, Method, NEVER_RETRY,
    SubCause,
)
from .downtime import DowntimeOracle


class PolicyEngine:
    def __init__(self, config: PolicyConfig, downtime: DowntimeOracle | None = None):
        self.cfg = config
        self.downtime = downtime or DowntimeOracle()

    # ----------------------------------------------------------------- main
    def decide(self, case: Case, now: datetime) -> Decision:
        cfg = self.cfg
        p = case.payment
        c: Classification = case.classification
        attempts = len(case.attempts)

        # ---- economic guardrails ----------------------------------------
        # These come first: it is never right to chase a payment that costs
        # more to recover than it returns.
        if p.amount_paise < cfg.min_amount_to_chase_paise:
            return self._stop("ECON-01",
                              f"Payment is Rs {p.amount_rupees:.0f}, below the "
                              f"Rs {cfg.min_amount_to_chase_paise/100:.0f} floor where chasing pays for itself.")

        if case.cost_paise >= cfg.spend_ceiling_paise(p.amount_paise):
            return self._stop("ECON-02",
                              f"Already spent Rs {case.cost_paise/100:.2f} chasing a "
                              f"Rs {p.amount_rupees:.0f} payment; that is the {cfg.max_spend_ratio:.0%} ceiling.")

        # ---- consent ----------------------------------------------------
        if cfg.respect_opt_out and p.customer.opted_out:
            return self._stop("COMP-01",
                              "Customer has opted out of payment reminders; no contact permitted.")

        # ---- confidence gate (FR-22) -------------------------------------
        if c.confidence < cfg.escalate_below_confidence:
            return Decision(
                action=Action.ESCALATE, delay_hours=0.0, alt_method=None,
                policy_rule="CONF-01",
                reason=(f"Classifier confidence {c.confidence:.0%} is below the "
                        f"{cfg.escalate_below_confidence:.0%} threshold. Routing to human "
                        f"review rather than guessing with the customer's money."),
            )

        # ---- attempt exhaustion ------------------------------------------
        if attempts >= cfg.max_attempts_per_payment:
            return self._stop("STOP-01",
                              f"Reached the {cfg.max_attempts_per_payment}-attempt cap for this payment.")

        elapsed_h = (now - p.failed_at).total_seconds() / 3600.0
        if elapsed_h > cfg.max_recovery_window_hours:
            return self._stop("STOP-02",
                              f"{elapsed_h:.0f}h since failure exceeds the "
                              f"{cfg.max_recovery_window_hours}h recovery window.")

        # ---- hard declines ------------------------------------------------
        if c.cause is Cause.HARD_DECLINE:
            return self._hard_decline(case, c, attempts)

        # ---- user drop-off -------------------------------------------------
        if c.cause is Cause.USER_DROPOFF:
            return self._dropoff(case, c, attempts, now)

        # ---- soft declines --------------------------------------------------
        if c.cause is Cause.SOFT_DECLINE:
            return self._soft_decline(case, c, attempts, now)

        # ---- unknown ---------------------------------------------------------
        return Decision(
            action=Action.ESCALATE, delay_hours=0.0, alt_method=None,
            policy_rule="UNK-01",
            reason="Cause could not be determined; a human should look before we act.",
        )

    # ------------------------------------------------------------ branches
    def _hard_decline(self, case: Case, c: Classification, attempts: int) -> Decision:
        """
        A hard decline means the instrument is dead. Retrying it is not just
        useless -- repeated declines on a dead card are penalised by card
        networks. The only legitimate move is to offer a different rail once.
        """
        if attempts == 0 and c.subcause in (SubCause.CARD_EXPIRED, SubCause.METHOD_NOT_SUPPORTED,
                                            SubCause.CARD_BLOCKED):
            alt = METHOD_FALLBACK[case.payment.method]
            return Decision(
                action=Action.PAYMENT_LINK, delay_hours=0.0, alt_method=alt,
                policy_rule="HARD-01",
                reason=(f"{c.subcause.value.replace('_',' ')} is permanent on this instrument. "
                        f"Retrying it would fail every time, so we offer a {alt.value} link once "
                        f"and let the customer choose."),
            )
        if c.subcause in (SubCause.RISK_BLOCKED, SubCause.ACCOUNT_FROZEN):
            return self._stop("HARD-02",
                              f"{c.subcause.value.replace('_',' ')} is a compliance-side block. "
                              f"Automated recovery is not appropriate here.")
        return self._stop("HARD-03",
                          f"{c.subcause.value.replace('_',' ')} cannot be recovered by any "
                          f"automated action. Stopping deliberately.")

    def _dropoff(self, case: Case, c: Classification, attempts: int, now: datetime) -> Decision:
        """
        Nobody's bank refused anything -- the customer walked away. A silent
        retry does nothing because there is no one at the keyboard. What is
        needed is a reason to come back.
        """
        if attempts >= 2:
            return self._stop("DROP-03",
                              "Customer has been nudged twice without completing. "
                              "Further contact would be pestering, not recovery.")

        if c.subcause is SubCause.INCORRECT_VPA:
            alt = METHOD_FALLBACK[case.payment.method]
            return Decision(
                action=Action.PAYMENT_LINK, delay_hours=0.25, alt_method=alt,
                policy_rule="DROP-01",
                reason="The UPI ID could not be resolved, so the same rail will fail again. "
                       "Sending a link that lets them re-enter or switch method.",
            )

        delay = self._compliant_delay(now, 0.5)
        return Decision(
            action=Action.PAYMENT_LINK, delay_hours=delay, alt_method=None,
            policy_rule="DROP-02",
            reason=(f"Checkout was abandoned at the {case.payment.error_step} step. "
                    f"A retry has nobody to authorise it, so we send a payment link "
                    f"{self._delay_phrase(delay)}."),
        )

    def _soft_decline(self, case: Case, c: Classification, attempts: int, now: datetime) -> Decision:
        """
        Soft declines are the whole game. The bank may say yes later -- but
        'later' means something different for each subcause, and retrying at
        the wrong moment burns an attempt for nothing.
        """
        cfg = self.cfg
        p = case.payment

        # Live downtime signal (FR-20) -- the highest-value input we have.
        dt = self.downtime.check(p.bank, p.method, now)
        if dt.is_down:
            if dt.expected_minutes_remaining > 90 and attempts == 0:
                alt = METHOD_FALLBACK[p.method]
                return Decision(
                    action=Action.PAYMENT_LINK, delay_hours=0.0, alt_method=alt,
                    policy_rule="DOWN-01",
                    reason=(f"{p.bank} {p.method.value} is currently degraded with roughly "
                            f"{dt.expected_minutes_remaining} min to go. Waiting that long risks "
                            f"the order, so we offer {alt.value} instead."),
                )
            wait = max(0.5, dt.expected_minutes_remaining / 60.0 + 0.25)
            wait = self._compliant_delay(now, wait)
            return Decision(
                action=Action.SCHEDULE_RETRY, delay_hours=wait, alt_method=None,
                policy_rule="DOWN-02",
                reason=(f"{p.bank} {p.method.value} is down right now. Retrying immediately "
                        f"would fail and consume an attempt, so we wait "
                        f"{self._delay_phrase(wait)} for the rail to recover."),
            )

        if c.subcause is SubCause.INSUFFICIENT_FUNDS:
            if attempts >= 2:
                alt = METHOD_FALLBACK[p.method]
                return Decision(
                    action=Action.PAYMENT_LINK, delay_hours=0.0, alt_method=alt,
                    policy_rule="SOFT-IF-02",
                    reason="Balance has stayed short across two attempts. Offering an "
                           "alternate method rather than draining more attempts.",
                )
            delay = self._compliant_delay(now, cfg.insufficient_funds_delay_hours)
            return Decision(
                action=Action.SCHEDULE_RETRY, delay_hours=delay, alt_method=None,
                policy_rule="SOFT-IF-01",
                reason=(f"Balance was short. Accounts are most likely to be funded in the "
                        f"morning, so we retry {self._delay_phrase(delay)} rather than "
                        f"immediately."),
            )

        if c.subcause is SubCause.NETWORK_TIMEOUT:
            return Decision(
                action=Action.RETRY_NOW, delay_hours=0.0, alt_method=None,
                policy_rule="SOFT-NT-01",
                reason="A timeout is transient and the authorisation window is still open. "
                       "This is the one case where retrying immediately is correct.",
            )

        if c.subcause is SubCause.RATE_LIMITED:
            delay = self._compliant_delay(now, 2.0)
            return Decision(
                action=Action.SCHEDULE_RETRY, delay_hours=delay, alt_method=None,
                policy_rule="SOFT-RL-01",
                reason="The bank rate-limited this account. Backing off "
                       f"{self._delay_phrase(delay)} instead of adding to the pressure.",
            )

        if c.subcause in (SubCause.BANK_DOWNTIME, SubCause.ISSUER_UNAVAILABLE):
            if attempts >= 1:
                alt = METHOD_FALLBACK[p.method]
                return Decision(
                    action=Action.PAYMENT_LINK, delay_hours=0.0, alt_method=alt,
                    policy_rule="SOFT-BD-02",
                    reason=f"{p.bank} has now failed twice. Switching rail to {alt.value} "
                           f"rather than waiting on a bank we do not control.",
                )
            delay = self._compliant_delay(now, cfg.bank_downtime_delay_hours)
            return Decision(
                action=Action.SCHEDULE_RETRY, delay_hours=delay, alt_method=None,
                policy_rule="SOFT-BD-01",
                reason=f"{p.bank} reported unavailable. Waiting {self._delay_phrase(delay)} "
                       f"for the rail to come back.",
            )

        delay = self._compliant_delay(now, cfg.generic_soft_delay_hours)
        return Decision(
            action=Action.SCHEDULE_RETRY, delay_hours=delay, alt_method=None,
            policy_rule="SOFT-GEN-01",
            reason=f"Recoverable bank-side failure with no specific signal. "
                   f"Standard backoff of {self._delay_phrase(delay)}.",
        )

    # ------------------------------------------------------------- helpers
    def _stop(self, rule: str, reason: str) -> Decision:
        return Decision(action=Action.STOP, delay_hours=0.0, alt_method=None,
                        policy_rule=rule, reason=reason)

    def _compliant_delay(self, now: datetime, want_hours: float) -> float:
        """
        Push a proposed delay forward until it lands outside quiet hours
        (NFR-22). Returns hours from `now`.
        """
        cfg = self.cfg
        target = now + timedelta(hours=want_hours)
        local_hour = (target.hour + cfg.timezone_offset_hours) % 24

        if local_hour >= cfg.quiet_hours_start or local_hour < cfg.quiet_hours_end:
            # advance to the start of the next permitted window
            hours_to_open = (cfg.quiet_hours_end - local_hour) % 24
            want_hours += hours_to_open
        return round(want_hours, 2)

    @staticmethod
    def _delay_phrase(hours: float) -> str:
        if hours <= 0.01:
            return "immediately"
        if hours < 1:
            return f"in {int(hours * 60)} min"
        if hours < 24:
            return f"in {hours:.1f}h"
        return f"in {hours/24:.1f} days"


class NaivePolicyEngine(PolicyEngine):
    """
    The honest baseline: what a merchant's retry cron actually does today.

    Retry the same instrument, immediately, up to the cap, regardless of why
    it failed. No classification is consulted, no downtime is checked, no
    method is switched, nobody is ever nudged.

    This has to be a genuinely different POLICY, not the smart policy with
    looser constants. Running the intelligent engine under a permissive config
    and calling it "naive" would flatter the agent by comparing it against
    itself -- the resulting uplift number would be meaningless.
    """

    def decide(self, case: Case, now: datetime) -> Decision:
        if len(case.attempts) >= self.cfg.max_attempts_per_payment:
            return self._stop("NAIVE-STOP",
                              f"Retry cron exhausted its {self.cfg.max_attempts_per_payment} attempts.")

        elapsed_h = (now - case.payment.failed_at).total_seconds() / 3600.0
        if elapsed_h > self.cfg.max_recovery_window_hours:
            return self._stop("NAIVE-STOP", "Outside the retry window.")

        return Decision(
            action=Action.RETRY_NOW, delay_hours=0.0, alt_method=None,
            policy_rule="NAIVE-RETRY",
            reason="Blind retry on the original instrument, no diagnosis performed.",
        )
