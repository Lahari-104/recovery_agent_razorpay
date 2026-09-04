"""
API surface.

Two entry paths, one engine:

  /api/eval/*      the simulated batch -- where the measured numbers come from
  /api/webhook/*   a real Razorpay `payment.failed` event -- the live demo

Both normalise into the same FailedPayment and run through the same agent, so
the live path is not a separate toy implementation. That is the point of the
normalisation layer (FR-12).

The webhook handler acknowledges in milliseconds and processes out of band
(NFR-12). Doing LLM work inside a webhook request is how you end up with
Razorpay retrying because you timed out, which then produces exactly the
duplicate-event problem the idempotency layer exists to absorb.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import DEFAULT_CONFIG, NAIVE_CONFIG, PolicyConfig
from .core.agent import RecoveryAgent
from .core.audit import AuditLog, config_fingerprint
from .core.classifier import build_classifier, Classifier
from .core.downtime import DowntimeOracle
from .domain import (
    Action, Case, CaseState, CustomerProfile, ERROR_CODE_MAP, FailedPayment,
    Method,
)
from .eval.harness import (
    Comparison, make_world_perform, run_comparison, run_policy,
)
from .sim.world import World, generate_batch

IST = timezone(timedelta(hours=5, minutes=30))
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

app = FastAPI(
    title="Revenue Recovery Agent",
    description="Bounded, auditable recovery of failed payments.",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


# --------------------------------------------------------------------------
# Process state
# --------------------------------------------------------------------------

class AppState:
    def __init__(self) -> None:
        self.config: PolicyConfig = DEFAULT_CONFIG
        self.classifier: Classifier = build_classifier(use_llm=True)
        self.batch_size: int = 500
        self.seed: int = 20260824
        self.comparison: Optional[Comparison] = None
        self.payments: list[FailedPayment] = []
        self.world: Optional[World] = None
        self.last_run_ms: float = 0.0
        # live webhook path
        self.live_agent: Optional[RecoveryAgent] = None
        self.live_events: list[dict] = []
        self.seen_event_ids: set[str] = set()

    def llm_status(self) -> str:
        """
        Three distinct states, deliberately. Running without an LLM key is a
        CHOICE, not a fault -- the rules engine resolves every mapped gateway
        code on its own and calling that "degraded" is simply untrue. Real
        degradation is when a key IS configured and the model cannot be
        reached; that is worth flagging loudly, and only that.
        """
        if self.classifier.llm is not None:
            return "gemini + rules"
        if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
            return "rules fallback (LLM unreachable)"
        return "rules engine"

    def llm_degraded(self) -> bool:
        return self.classifier.llm is None and bool(
            os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))


state = AppState()


def ensure_batch(force: bool = False) -> Comparison:
    if state.comparison is not None and not force:
        return state.comparison
    t0 = time.perf_counter()
    state.payments, state.world = generate_batch(state.batch_size, seed=state.seed)
    state.comparison = run_comparison(
        state.payments, state.world, state.config, state.classifier)
    state.last_run_ms = (time.perf_counter() - t0) * 1000
    return state.comparison


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

class ConfigPatch(BaseModel):
    max_attempts_per_payment: Optional[int] = Field(None, ge=1, le=10)
    max_touches_per_customer_24h: Optional[int] = Field(None, ge=1, le=20)
    min_cooldown_minutes: Optional[int] = Field(None, ge=0, le=720)
    max_recovery_window_hours: Optional[int] = Field(None, ge=1, le=336)
    cost_retry_paise: Optional[int] = Field(None, ge=0, le=100000)
    cost_payment_link_paise: Optional[int] = Field(None, ge=0, le=100000)
    cost_escalation_paise: Optional[int] = Field(None, ge=0, le=1000000)
    max_spend_ratio: Optional[float] = Field(None, ge=0.0, le=1.0)
    min_amount_to_chase_paise: Optional[int] = Field(None, ge=0)
    escalate_below_confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    insufficient_funds_delay_hours: Optional[float] = Field(None, ge=0.0, le=72.0)
    quiet_hours_start: Optional[int] = Field(None, ge=0, le=23)
    quiet_hours_end: Optional[int] = Field(None, ge=0, le=23)
    kill_switch: Optional[bool] = None
    dry_run: Optional[bool] = None


class BatchRequest(BaseModel):
    size: int = Field(500, ge=10, le=5000)
    seed: Optional[int] = None


# --------------------------------------------------------------------------
# Health and config
# --------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "llm": state.llm_status(),
        "llm_degraded": state.llm_degraded(),
        "policy_version": state.config.version,
        "config_hash": config_fingerprint(state.config),
        "kill_switch": state.config.kill_switch,
        "dry_run": state.config.dry_run,
        "razorpay_keys_present": bool(os.getenv("RAZORPAY_KEY_ID")),
        "batch_loaded": state.comparison is not None,
    }


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    return state.config.to_dict()


@app.post("/api/config")
def patch_config(patch: ConfigPatch) -> dict[str, Any]:
    updates = {k: v for k, v in patch.model_dump().items() if v is not None}
    state.config = state.config.with_overrides(**updates)
    state.comparison = None          # invalidate: results no longer match config
    # A live agent holds its own copy of the config. Without this, toggling the
    # kill switch would appear to work in the UI and change nothing at all.
    if state.live_agent is not None:
        state.live_agent.apply_config(state.config)
    return {"config": state.config.to_dict(), "applied": updates}


@app.post("/api/config/reset")
def reset_config() -> dict[str, Any]:
    state.config = DEFAULT_CONFIG
    state.comparison = None
    if state.live_agent is not None:
        state.live_agent.apply_config(state.config)
    return {"config": state.config.to_dict()}


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

@app.post("/api/eval/run")
def run_batch(req: BatchRequest) -> dict[str, Any]:
    state.batch_size = req.size
    if req.seed is not None:
        state.seed = req.seed
    cmp = ensure_batch(force=True)
    return {
        **cmp.summary(),
        "runtime_ms": round(state.last_run_ms, 1),
        "batch_size": state.batch_size,
        "seed": state.seed,
        "audit_entries": len(cmp.audit) if cmp.audit else 0,
        "audit_digest": cmp.agent.audit_digest,
    }


@app.get("/api/eval/summary")
def eval_summary() -> dict[str, Any]:
    cmp = ensure_batch()
    return {
        **cmp.summary(),
        "runtime_ms": round(state.last_run_ms, 1),
        "batch_size": state.batch_size,
        "seed": state.seed,
        "classifier_stats": state.classifier.stats,
        "llm": state.llm_status(),
    }


@app.get("/api/eval/breakdown")
def eval_breakdown() -> dict[str, Any]:
    cmp = ensure_batch()

    def enrich(d: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for k, v in d.items():
            rec = dict(v)
            rec["key"] = k
            rec["recovered_rupees"] = round(v.get("recovered_paise", 0) / 100, 2)
            rec["at_risk_rupees"] = round(v.get("at_risk_paise", 0) / 100, 2)
            rec["spend_rupees"] = round(v.get("spend_paise", 0) / 100, 2)
            if v.get("at_risk_paise"):
                rec["rate"] = round(v["recovered_paise"] / v["at_risk_paise"], 4)
            if v.get("used"):
                rec["success_rate"] = round(v.get("succeeded", 0) / v["used"], 4)
            out.append(rec)
        return sorted(out, key=lambda r: -r.get("at_risk_paise", r.get("used", 0)))

    return {
        "by_cause": enrich(cmp.agent.by_cause),
        "by_action": enrich(cmp.agent.by_action),
    }


@app.get("/api/eval/exceptions")
def eval_exceptions(limit: int = 100) -> dict[str, Any]:
    cmp = ensure_batch()
    rows = [e.__dict__ for e in cmp.exceptions[:limit]]
    unrecoverable = sum(1 for e in cmp.exceptions if e.truth_profile == "unrecoverable")
    return {
        "exceptions": rows,
        "total": len(cmp.exceptions),
        "genuinely_unrecoverable": unrecoverable,
        "missed_but_recoverable": len(cmp.exceptions) - unrecoverable,
    }


@app.get("/api/eval/cases")
def eval_cases(limit: int = 60, state_filter: Optional[str] = None) -> dict[str, Any]:
    cmp = ensure_batch()
    cases = cmp.cases
    if state_filter:
        cases = [c for c in cases if c.state.value == state_filter]
    cases = sorted(cases, key=lambda c: -c.payment.amount_paise)[:limit]
    return {"cases": [_case_row(c) for c in cases], "total": len(cmp.cases)}


@app.get("/api/eval/case/{case_id}")
def eval_case(case_id: str) -> dict[str, Any]:
    cmp = ensure_batch()
    case = next((c for c in cmp.cases if c.case_id == case_id), None)
    if case is None:
        raise HTTPException(404, "case not found")
    trail = [e.to_dict() for e in cmp.audit.for_case(case_id)] if cmp.audit else []
    return {**_case_detail(case), "audit": trail}


def _case_row(c: Case) -> dict[str, Any]:
    return {
        "case_id": c.case_id,
        "amount_rupees": round(c.payment.amount_rupees, 2),
        "method": c.payment.method.value,
        "bank": c.payment.bank,
        "error_code": c.payment.error_code,
        "cause": c.classification.cause.value if c.classification else None,
        "subcause": c.classification.subcause.value if c.classification else None,
        "confidence": round(c.classification.confidence, 3) if c.classification else None,
        "state": c.state.value,
        "attempts": len(c.attempts),
        "recovered_rupees": round(c.recovered_paise / 100, 2),
        "spend_rupees": round(c.cost_paise / 100, 2),
        "failed_at": c.payment.failed_at.isoformat(),
        "rule": c.decisions[-1].policy_rule if c.decisions else None,
    }


def _case_detail(c: Case) -> dict[str, Any]:
    return {
        **_case_row(c),
        "payment": c.payment.to_dict(),
        "customer": {
            "id": c.payment.customer.customer_id,
            "lifetime_payments": c.payment.customer.lifetime_payments,
            "lifetime_failures": c.payment.customer.lifetime_failures,
            "failure_rate": round(c.payment.customer.failure_rate, 3),
            "opted_out": c.payment.customer.opted_out,
        },
        "classification": {
            "cause": c.classification.cause.value,
            "subcause": c.classification.subcause.value,
            "confidence": c.classification.confidence,
            "reason": c.classification.reason,
            "source": c.classification.source,
        } if c.classification else None,
        "decisions": [
            {"action": d.action.value, "rule": d.policy_rule, "reason": d.reason,
             "delay_hours": d.delay_hours,
             "alt_method": d.alt_method.value if d.alt_method else None}
            for d in c.decisions
        ],
        "attempts_detail": [
            {"seq": a.seq, "action": a.action.value, "method": a.method.value,
             "at": a.executed_at.isoformat(), "succeeded": a.succeeded,
             "cost_rupees": round(a.cost_paise / 100, 2)}
            for a in c.attempts
        ],
        "closed_reason": c.closed_reason,
    }


# --------------------------------------------------------------------------
# Policy simulator (FR-36)
# --------------------------------------------------------------------------

@app.post("/api/simulate")
def simulate(patch: ConfigPatch) -> dict[str, Any]:
    """
    Re-run the current batch under a forked config WITHOUT mutating live
    state. This is what makes the caps explorable instead of assertions --
    a judge can move a slider and watch net recovery respond.
    """
    if state.world is None:
        ensure_batch()
    updates = {k: v for k, v in patch.model_dump().items() if v is not None}
    forked = state.config.with_overrides(**updates)

    t0 = time.perf_counter()
    res, cases, _ = run_policy("agent-sim", state.payments, state.world,
                               forked, state.classifier, keep_cases=True)
    elapsed = (time.perf_counter() - t0) * 1000

    base = state.comparison.agent
    return {
        "config": forked.to_dict(),
        "applied": updates,
        "result": res.to_dict(),
        "baseline": base.to_dict(),
        "delta_net_rupees": round((res.net_paise - base.net_paise) / 100, 2),
        "delta_recovery_rate": round(res.recovery_rate - base.recovery_rate, 4),
        "runtime_ms": round(elapsed, 1),
    }


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------

@app.get("/api/audit/tail")
def audit_tail(n: int = 120, event: Optional[str] = None) -> dict[str, Any]:
    cmp = ensure_batch()
    if cmp.audit is None:
        return {"entries": [], "total": 0}
    entries = cmp.audit.by_event(event) if event else list(cmp.audit)
    return {
        "entries": [e.to_dict() for e in entries[-n:]][::-1],
        "total": len(cmp.audit),
        "digest": cmp.audit.digest(),
    }


@app.get("/api/audit/export")
def audit_export() -> PlainTextResponse:
    cmp = ensure_batch()
    body = cmp.audit.export_jsonl() if cmp.audit else ""
    return PlainTextResponse(body, media_type="application/x-ndjson")


@app.get("/api/audit/replay/{case_id}")
def audit_replay(case_id: str) -> dict[str, Any]:
    """
    NFR-32: recompute this case's decisions from scratch and confirm the
    result is identical. This is what "auditable" means operationally.
    """
    cmp = ensure_batch()
    original = next((c for c in cmp.cases if c.case_id == case_id), None)
    if original is None:
        raise HTTPException(404, "case not found")
    payment = next(p for p in state.payments if p.payment_id == case_id)

    agent = RecoveryAgent(
        config=state.config, classifier=state.classifier,
        perform=make_world_perform(state.world),
        downtime=DowntimeOracle(seed=state.world.seed),
    )
    replayed = agent.run_case(payment)

    same_state = replayed.state == original.state
    same_money = replayed.recovered_paise == original.recovered_paise
    same_rules = [d.policy_rule for d in replayed.decisions] == \
                 [d.policy_rule for d in original.decisions]

    return {
        "case_id": case_id,
        "identical": bool(same_state and same_money and same_rules),
        "checks": {"state": same_state, "money": same_money, "rule_sequence": same_rules},
        "original": {"state": original.state.value,
                     "recovered_rupees": round(original.recovered_paise / 100, 2),
                     "rules": [d.policy_rule for d in original.decisions]},
        "replayed": {"state": replayed.state.value,
                     "recovered_rupees": round(replayed.recovered_paise / 100, 2),
                     "rules": [d.policy_rule for d in replayed.decisions]},
    }


@app.get("/api/refusals")
def refusals() -> dict[str, Any]:
    """
    Every action the agent declined to take. Refusals are shown as
    first-class events, not silent no-ops (NFR-29) -- an agent choosing
    NOT to act is the behaviour worth demonstrating.
    """
    cmp = ensure_batch()
    if cmp.audit is None:
        return {"refusals": [], "by_guard": {}}
    entries = cmp.audit.by_event("refused")
    by_guard: dict[str, int] = {}
    for e in entries:
        g = e.detail.get("guard", "unknown")
        by_guard[g] = by_guard.get(g, 0) + 1
    return {
        "refusals": [e.to_dict() for e in entries[-200:]][::-1],
        "by_guard": dict(sorted(by_guard.items(), key=lambda kv: -kv[1])),
        "total": len(entries),
    }


@app.get("/api/downtime")
def downtime_now() -> dict[str, Any]:
    banks = ["HDFC", "ICICI", "SBI", "Axis", "Kotak", "PNB", "BoB", "Yes", "IndusInd", "Federal"]
    at = state.payments[len(state.payments) // 2].failed_at if state.payments \
        else datetime.now(IST)
    oracle = DowntimeOracle(seed=state.seed)
    snap = oracle.snapshot(banks, list(Method), at)
    return {
        "as_of": at.isoformat(),
        "degraded": [
            {"bank": s.bank, "method": s.method.value, "severity": s.severity,
             "minutes_remaining": s.expected_minutes_remaining}
            for s in snap
        ],
        "source": "simulated" if not os.getenv("RAZORPAY_KEY_ID") else "razorpay_api",
    }


# --------------------------------------------------------------------------
# Live Razorpay webhook (FR-9..FR-11)
# --------------------------------------------------------------------------

def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def normalise_razorpay(payload: dict[str, Any]) -> FailedPayment:
    """FR-12: a real webhook becomes the same FailedPayment the simulator emits."""
    ent = payload.get("payload", {}).get("payment", {}).get("entity", {})
    method_raw = (ent.get("method") or "upi").lower()
    method = {"upi": Method.UPI, "card": Method.CARD,
              "netbanking": Method.NETBANKING, "wallet": Method.WALLET}.get(method_raw, Method.UPI)

    return FailedPayment(
        payment_id=ent.get("id", f"pay_LIVE{int(time.time())}"),
        order_id=ent.get("order_id") or "order_unknown",
        customer=CustomerProfile(
            customer_id=ent.get("customer_id") or ent.get("email", "cust_live"),
            lifetime_payments=0, lifetime_failures=0, days_since_last_success=0,
        ),
        amount_paise=int(ent.get("amount", 0)),
        method=method,
        bank=ent.get("bank") or ent.get("wallet") or "UNKNOWN",
        error_code=ent.get("error_reason") or ent.get("error_code") or "unknown",
        error_description=ent.get("error_description") or "",
        error_source=ent.get("error_source") or "gateway",
        error_step=ent.get("error_step") or "payment_authorization",
        failed_at=datetime.fromtimestamp(ent.get("created_at", time.time()), tz=IST),
        is_synthetic=False,
    )


def _live_perform(case: Case, action: Action, method: Method,
                  at: datetime, seq: int) -> bool:
    """
    Live-mode side effect. In test mode we create a real Razorpay Payment Link
    rather than force-charging anyone -- a link is the only intervention that
    is safe and honest to perform for real against a sandbox merchant.
    """
    key_id, key_secret = os.getenv("RAZORPAY_KEY_ID"), os.getenv("RAZORPAY_KEY_SECRET")
    if not (key_id and key_secret) or action is not Action.PAYMENT_LINK:
        return False
    try:
        import httpx
        r = httpx.post(
            "https://api.razorpay.com/v1/payment_links",
            auth=(key_id, key_secret), timeout=8.0,
            json={
                "amount": case.payment.amount_paise,
                "currency": "INR",
                "accept_partial": False,
                "description": f"Recovery for {case.payment.order_id}",
                "notes": {"recovery_case": case.case_id, "seq": str(seq)},
            },
        )
        state.live_events.append({
            "at": at.isoformat(), "case_id": case.case_id,
            "action": "payment_link_created", "status": r.status_code,
            "link_id": r.json().get("id") if r.status_code < 300 else None,
        })
        return False   # link created; recovery is pending customer action
    except Exception as exc:
        state.live_events.append({
            "at": at.isoformat(), "case_id": case.case_id,
            "action": "payment_link_failed", "error": str(exc)[:160],
        })
        return False


def _process_live(payment: FailedPayment) -> None:
    if state.live_agent is None:
        state.live_agent = RecoveryAgent(
            config=state.config, classifier=state.classifier, perform=_live_perform,
        )
    state.live_agent.run_case(payment)


@app.post("/api/webhook/razorpay")
async def razorpay_webhook(
    request: Request,
    background: BackgroundTasks,
    x_razorpay_signature: Optional[str] = Header(None),
    x_razorpay_event_id: Optional[str] = Header(None),
) -> JSONResponse:
    """
    NFR-12: acknowledge fast, process out of band. Anything slower invites
    Razorpay's own retry, which manufactures the duplicate events the
    idempotency layer then has to absorb.
    """
    body = await request.body()
    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET")

    if secret:
        if not x_razorpay_signature or not verify_signature(body, x_razorpay_signature, secret):
            raise HTTPException(400, "invalid signature")

    # FR-11: at-least-once delivery, exactly-once effect.
    if x_razorpay_event_id:
        if x_razorpay_event_id in state.seen_event_ids:
            return JSONResponse({"status": "duplicate_ignored",
                                 "event_id": x_razorpay_event_id})
        state.seen_event_ids.add(x_razorpay_event_id)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(400, "malformed json")

    if payload.get("event") not in ("payment.failed", "payment.authorized"):
        return JSONResponse({"status": "ignored", "event": payload.get("event")})

    payment = normalise_razorpay(payload)
    background.add_task(_process_live, payment)

    return JSONResponse({
        "status": "accepted",
        "payment_id": payment.payment_id,
        "amount_rupees": payment.amount_rupees,
        "signature_verified": bool(secret),
    })


@app.get("/api/webhook/live")
def live_cases() -> dict[str, Any]:
    if state.live_agent is None:
        return {"cases": [], "events": state.live_events, "count": 0}
    cases = list(state.live_agent.cases.values())
    return {
        "cases": [_case_row(c) for c in cases][::-1],
        "events": state.live_events[-50:][::-1],
        "count": len(cases),
        "audit": [e.to_dict() for e in list(state.live_agent.audit)[-60:]][::-1],
    }


@app.post("/api/webhook/simulate")
def simulate_webhook(body: dict[str, Any]) -> dict[str, Any]:
    """
    Fires a synthetic `payment.failed` through the REAL webhook code path.
    Lets the live panel be demonstrated without waiting on a sandbox event.
    """
    code = body.get("error_code", "payment_upi_bank_down")
    if code not in ERROR_CODE_MAP:
        raise HTTPException(400, f"unknown error code: {code}")
    amount = int(body.get("amount_paise", 149900))

    payload = {
        "event": "payment.failed",
        "payload": {"payment": {"entity": {
            "id": f"pay_LIVE{int(time.time()*1000) % 10**8}",
            "order_id": f"order_LIVE{int(time.time()) % 10**6}",
            "amount": amount, "method": body.get("method", "upi"),
            "bank": body.get("bank", "HDFC"),
            "error_reason": code,
            "error_description": body.get("error_description", "Simulated failure event."),
            "error_source": "bank", "error_step": "payment_authorization",
            "created_at": int(time.time()),
            "customer_id": body.get("customer_id", "cust_live_demo"),
        }}},
    }
    payment = normalise_razorpay(payload)
    _process_live(payment)
    case = state.live_agent.cases[payment.payment_id]
    return {
        "injected": payment.payment_id,
        "case": _case_detail(case),
        "audit": [e.to_dict() for e in state.live_agent.audit.for_case(case.case_id)],
    }


# --------------------------------------------------------------------------
# Static frontend
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    idx = FRONTEND_DIR / "index.html"
    if idx.exists():
        return HTMLResponse(idx.read_text(encoding="utf-8"),
                            media_type="text/html; charset=utf-8")
    return HTMLResponse("<h1>Recovery Agent API</h1><p>See /docs</p>")


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
