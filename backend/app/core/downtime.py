"""
Bank / rail downtime signal (FR-20).

Razorpay publishes a Payment Downtime API that reports which banks, methods
and UPI handles are currently degraded. Almost nobody consumes it, which is
exactly why it is worth consuming: it turns "retry and hope" into "do not
retry, this rail is down for another 40 minutes".

In simulation we generate deterministic downtime windows from the batch seed
so runs are reproducible. In live mode the same interface is backed by the
real API, and a fetch failure degrades to `is_down=False` rather than
blocking the pipeline (NFR-8).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from ..domain import Method


@dataclass(frozen=True)
class DowntimeStatus:
    is_down: bool
    bank: str
    method: Method
    severity: str = "none"              # none | partial | high
    expected_minutes_remaining: int = 0
    source: str = "simulated"


class DowntimeOracle:
    """
    Deterministic simulated downtime.

    Each (bank, method) pair gets a small number of outage windows across the
    batch period, derived by hashing the seed. Identical for every policy run,
    so the naive baseline and the agent face the same outages.
    """

    def __init__(self, seed: int = 20260824, outage_rate: float = 0.16):
        self.seed = seed
        self.outage_rate = outage_rate
        self._cache: dict[tuple[str, str, int], DowntimeStatus] = {}

    def _hash01(self, *parts: object) -> float:
        raw = ":".join(str(p) for p in (self.seed, *parts))
        h = hashlib.sha256(raw.encode()).hexdigest()
        return int(h[:12], 16) / float(16 ** 12)

    def check(self, bank: str, method: Method, at: datetime) -> DowntimeStatus:
        """Is this rail degraded at time `at`? Resolution: 30-minute buckets."""
        bucket = int(at.timestamp() // 1800)
        key = (bank, method.value, bucket)
        if key in self._cache:
            return self._cache[key]

        roll = self._hash01(bank, method.value, bucket)
        if roll >= self.outage_rate:
            status = DowntimeStatus(False, bank, method)
        else:
            sev_roll = self._hash01("sev", bank, method.value, bucket)
            severity = "high" if sev_roll < 0.4 else "partial"
            remaining = int(15 + self._hash01("dur", bank, method.value, bucket) * 165)
            status = DowntimeStatus(
                is_down=True, bank=bank, method=method,
                severity=severity, expected_minutes_remaining=remaining,
            )

        self._cache[key] = status
        return status

    def snapshot(self, banks: list[str], methods: list[Method], at: datetime) -> list[DowntimeStatus]:
        """Everything currently degraded -- powers the live status strip in the UI."""
        out = []
        for b in banks:
            for m in methods:
                s = self.check(b, m, at)
                if s.is_down:
                    out.append(s)
        return sorted(out, key=lambda s: -s.expected_minutes_remaining)


class LiveDowntimeOracle(DowntimeOracle):
    """
    Real Razorpay Downtime API. Falls back to 'not down' on any failure so a
    gateway outage never stalls the recovery pipeline (NFR-8).
    """

    def __init__(self, key_id: str, key_secret: str, ttl_seconds: int = 60):
        super().__init__()
        self.auth = (key_id, key_secret)
        self.ttl = ttl_seconds
        self._fetched_at: Optional[datetime] = None
        self._live: list[dict] = []

    def _refresh(self, now: datetime) -> None:
        if self._fetched_at and (now - self._fetched_at).total_seconds() < self.ttl:
            return
        try:
            import httpx
            r = httpx.get("https://api.razorpay.com/v1/payments/downtimes",
                          auth=self.auth, timeout=4.0)
            r.raise_for_status()
            self._live = r.json().get("items", [])
        except Exception:
            self._live = []          # degrade quietly
        finally:
            self._fetched_at = now

    def check(self, bank: str, method: Method, at: datetime) -> DowntimeStatus:
        self._refresh(at)
        for item in self._live:
            if item.get("method") != method.value:
                continue
            inst = item.get("instrument", {}) or {}
            if inst.get("bank") == bank or inst.get("issuer") == bank:
                return DowntimeStatus(
                    is_down=True, bank=bank, method=method,
                    severity=item.get("severity", "partial"),
                    expected_minutes_remaining=45,
                    source="razorpay_api",
                )
        return DowntimeStatus(False, bank, method, source="razorpay_api")
