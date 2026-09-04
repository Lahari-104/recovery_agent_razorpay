"""
Cause classification (FR-14..FR-18).

Design rule: the LLM never touches a case the rules can settle.

A blocked card is a hard decline. That is a fact, not a judgement call, and
routing it through a language model would add latency, cost, and a failure
mode in exchange for nothing. The rules table resolves the overwhelming
majority of real traffic. The LLM earns its place only on the residue:
unmapped codes and free-text error descriptions from gateways that do not
return a clean code.

If the LLM is unavailable, unreachable, or returns junk, classification
degrades to a keyword heuristic and the system keeps running (NFR-7). The
degraded path is flagged in `Classification.source` so the UI can show it.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Optional

from ..domain import Cause, Classification, FailedPayment, SubCause, ERROR_CODE_MAP

# --------------------------------------------------------------------------
# Keyword heuristic -- the always-available fallback
# --------------------------------------------------------------------------

_KEYWORDS: list[tuple[re.Pattern, Cause, SubCause, float]] = [
    (re.compile(r"insufficient|low balance|not enough", re.I),      Cause.SOFT_DECLINE, SubCause.INSUFFICIENT_FUNDS, 0.86),
    (re.compile(r"bank .*(down|unavailable|not respond)", re.I),    Cause.SOFT_DECLINE, SubCause.BANK_DOWNTIME, 0.82),
    (re.compile(r"issuer .*(down|unreachable|not reachable)", re.I),Cause.SOFT_DECLINE, SubCause.ISSUER_UNAVAILABLE, 0.82),
    (re.compile(r"time(d)? ?out|timeout", re.I),                    Cause.SOFT_DECLINE, SubCause.NETWORK_TIMEOUT, 0.80),
    (re.compile(r"network error", re.I),                            Cause.SOFT_DECLINE, SubCause.NETWORK_TIMEOUT, 0.78),
    (re.compile(r"technical error|gateway", re.I),                  Cause.SOFT_DECLINE, SubCause.GATEWAY_ERROR, 0.70),
    (re.compile(r"too many|rate ?limit", re.I),                     Cause.SOFT_DECLINE, SubCause.RATE_LIMITED, 0.75),

    (re.compile(r"expired", re.I),                                  Cause.HARD_DECLINE, SubCause.CARD_EXPIRED, 0.88),
    (re.compile(r"blocked by .*bank|card .*blocked", re.I),         Cause.HARD_DECLINE, SubCause.CARD_BLOCKED, 0.88),
    (re.compile(r"frozen", re.I),                                   Cause.HARD_DECLINE, SubCause.ACCOUNT_FROZEN, 0.90),
    (re.compile(r"not valid|invalid account", re.I),                Cause.HARD_DECLINE, SubCause.INVALID_ACCOUNT, 0.86),
    (re.compile(r"risk|fraud", re.I),                               Cause.HARD_DECLINE, SubCause.RISK_BLOCKED, 0.84),
    (re.compile(r"not supported", re.I),                            Cause.HARD_DECLINE, SubCause.METHOD_NOT_SUPPORTED, 0.80),
    (re.compile(r"mandate .*(revoked|cancelled)", re.I),            Cause.HARD_DECLINE, SubCause.MANDATE_REVOKED, 0.86),

    (re.compile(r"cancel", re.I),                                   Cause.USER_DROPOFF, SubCause.CANCELLED_BY_USER, 0.80),
    (re.compile(r"collect .*expired|expired without", re.I),        Cause.USER_DROPOFF, SubCause.COLLECT_EXPIRED, 0.84),
    (re.compile(r"otp", re.I),                                      Cause.USER_DROPOFF, SubCause.OTP_NOT_ENTERED, 0.84),
    (re.compile(r"window .*closed|closed before", re.I),            Cause.USER_DROPOFF, SubCause.WINDOW_CLOSED, 0.80),
    (re.compile(r"upi ?id|vpa", re.I),                              Cause.USER_DROPOFF, SubCause.INCORRECT_VPA, 0.78),
]

_SOURCE_PRIOR: dict[str, Cause] = {
    "customer": Cause.USER_DROPOFF,
    "bank": Cause.SOFT_DECLINE,
    "issuer": Cause.HARD_DECLINE,
    "gateway": Cause.SOFT_DECLINE,
    "network": Cause.SOFT_DECLINE,
}


def _heuristic(p: FailedPayment) -> Classification:
    text = f"{p.error_description} {p.error_code}"
    for pattern, cause, sub, conf in _KEYWORDS:
        if pattern.search(text):
            return Classification(
                cause=cause,
                subcause=sub,
                confidence=conf,
                reason=f"Matched '{pattern.pattern}' in the gateway error text.",
                source="heuristic",
            )
    prior = _SOURCE_PRIOR.get(p.error_source, Cause.UNKNOWN)
    return Classification(
        cause=prior,
        subcause=SubCause.UNKNOWN,
        confidence=0.35 if prior is not Cause.UNKNOWN else 0.15,
        reason=f"No keyword matched; fell back to error_source='{p.error_source}' prior.",
        source="heuristic",
    )


# --------------------------------------------------------------------------
# LLM path
# --------------------------------------------------------------------------

_PROMPT = """You classify failed payment errors for an Indian payment gateway.

Return ONLY a JSON object, no markdown fences, no prose:
{{"cause": "...", "subcause": "...", "confidence": 0.0, "reason": "..."}}

cause must be one of: soft_decline, hard_decline, user_dropoff, unknown
  soft_decline  = bank refused, but conditions may change (funds, downtime, timeouts)
  hard_decline  = permanently refused (expired/blocked card, frozen account, risk block)
  user_dropoff  = the customer never completed their part (OTP, cancel, expiry)

subcause must be one of: {subcauses}

confidence is your honest probability 0.0-1.0 that this classification is right.
reason is one short sentence a payments ops analyst would find useful.

Error code: {code}
Error description: {desc}
Reported source: {source}
Reported step: {step}
Payment method: {method}
"""


class Classifier:
    """
    Two-path classifier. `llm_client` is any object with
    `.generate(prompt: str) -> str`, or None to disable the LLM path entirely.
    """

    def __init__(self, llm_client=None, cache_enabled: bool = True):
        self.llm = llm_client
        self._cache: dict[str, Classification] = {}
        self.cache_enabled = cache_enabled
        self.stats = {"rules": 0, "llm": 0, "cache": 0, "fallback": 0}

    def classify(self, p: FailedPayment) -> Classification:
        t0 = time.perf_counter()

        # -- path 1: deterministic rules (FR-17) --------------------------
        mapped = ERROR_CODE_MAP.get(p.error_code)
        if mapped:
            cause, sub = mapped
            self.stats["rules"] += 1
            return Classification(
                cause=cause,
                subcause=sub,
                confidence=0.97,
                reason=f"Gateway code '{p.error_code}' maps deterministically to {sub.value}.",
                source="rules",
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

        # -- path 2: cache (NFR-33) ---------------------------------------
        key = f"{p.error_code}|{p.error_description[:120]}"
        if self.cache_enabled and key in self._cache:
            self.stats["cache"] += 1
            cached = self._cache[key]
            return Classification(**{**cached.__dict__, "source": cached.source + "+cache"})

        # -- path 3: LLM ---------------------------------------------------
        if self.llm is not None:
            try:
                raw = self.llm.generate(_PROMPT.format(
                    subcauses=", ".join(s.value for s in SubCause),
                    code=p.error_code,
                    desc=p.error_description,
                    source=p.error_source,
                    step=p.error_step,
                    method=p.method.value,
                ))
                parsed = self._parse(raw)
                if parsed:
                    parsed.latency_ms = (time.perf_counter() - t0) * 1000
                    self.stats["llm"] += 1
                    if self.cache_enabled:
                        self._cache[key] = parsed
                    return parsed
            except Exception:
                pass  # fall through to heuristic -- NFR-7

        # -- path 4: heuristic fallback ------------------------------------
        self.stats["fallback"] += 1
        out = _heuristic(p)
        out.latency_ms = (time.perf_counter() - t0) * 1000
        if self.cache_enabled:
            self._cache[key] = out
        return out

    @staticmethod
    def _parse(raw: str) -> Optional[Classification]:
        if not raw:
            return None
        cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        match = re.search(r"\{.*\}", cleaned, re.S)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
            cause = Cause(data["cause"])
            sub = SubCause(data.get("subcause", "unknown"))
            conf = float(data.get("confidence", 0.5))
        except (json.JSONDecodeError, KeyError, ValueError):
            return None
        return Classification(
            cause=cause,
            subcause=sub,
            confidence=max(0.0, min(1.0, conf)),
            reason=str(data.get("reason", ""))[:240],
            source="llm",
        )


# --------------------------------------------------------------------------
# Gemini adapter (optional)
# --------------------------------------------------------------------------

class GeminiClient:
    """
    Thin wrapper over google-genai. Absent key or absent package => the
    Classifier simply never uses the LLM path. Nothing breaks.
    """

    def __init__(self, model: str = "gemini-2.5-flash"):
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError("no GEMINI_API_KEY / GOOGLE_API_KEY in environment")
        from google import genai              # imported lazily
        from google.genai import types
        self._types = types
        self._client = genai.Client(api_key=key)
        self._model = model

    def generate(self, prompt: str) -> str:
        resp = self._client.models.generate_content(
            model=self._model,
            contents=prompt,
            config=self._types.GenerateContentConfig(
                max_output_tokens=400,
                temperature=0.0,
                thinking_config=self._types.ThinkingConfig(thinking_budget=0),
            ),
        )
        return resp.text or ""


def build_classifier(use_llm: bool = True) -> Classifier:
    """Best-effort LLM wiring. Never raises."""
    if not use_llm:
        return Classifier(llm_client=None)
    try:
        return Classifier(llm_client=GeminiClient())
    except Exception:
        return Classifier(llm_client=None)
