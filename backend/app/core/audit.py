"""
Append-only audit trail (FR-37..FR-39, NFR-27, NFR-32).

Two properties matter and both are easy to lose:

1. APPEND-ONLY. Entries are never mutated or deleted. A correction is a new
   entry that supersedes an old one, not an edit. `append` is the only public
   write method and there is deliberately no update or delete.

2. SUFFICIENT FOR REPLAY. Each entry carries enough provenance -- inputs,
   policy version, rule id, config hash -- that the decision can be
   recomputed and checked byte-for-byte later. An audit log you cannot replay
   is a diary, not an audit trail.

PII is masked on write (NFR-20). The log is what gets exported, screenshotted
and shared; it should not carry a customer's full contact details.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Iterator, Optional


_PHONE = re.compile(r"\b(\+?91)?[-\s]?([6-9]\d{9})\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")


def mask_pii(text: str) -> str:
    text = _PHONE.sub(lambda m: f"{m.group(2)[:2]}*****{m.group(2)[-2:]}", text)
    text = _EMAIL.sub(lambda m: m.group(0)[:2] + "***@" + m.group(0).split("@")[1], text)
    return text


@dataclass(frozen=True)
class AuditEntry:
    seq: int
    at: str
    case_id: str
    event: str                    # detected | classified | decided | executed | refused | closed
    summary: str                  # one plain-English line for the UI
    detail: dict[str, Any]
    policy_version: str
    config_hash: str
    correlation_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AuditLog:
    def __init__(self, policy_version: str = "v1", config_hash: str = ""):
        self._entries: list[AuditEntry] = []
        self.policy_version = policy_version
        self.config_hash = config_hash

    # -- write ------------------------------------------------------------
    def append(
        self,
        case_id: str,
        event: str,
        summary: str,
        detail: Optional[dict[str, Any]] = None,
        at: Optional[datetime] = None,
        correlation_id: str = "",
    ) -> AuditEntry:
        entry = AuditEntry(
            seq=len(self._entries) + 1,
            at=(at or datetime.utcnow()).isoformat(),
            case_id=case_id,
            event=event,
            summary=mask_pii(summary),
            detail=detail or {},
            policy_version=self.policy_version,
            config_hash=self.config_hash,
            correlation_id=correlation_id or case_id,
        )
        self._entries.append(entry)
        return entry

    # -- read -------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[AuditEntry]:
        return iter(self._entries)

    def for_case(self, case_id: str) -> list[AuditEntry]:
        return [e for e in self._entries if e.case_id == case_id]

    def tail(self, n: int = 100) -> list[AuditEntry]:
        return self._entries[-n:]

    def by_event(self, event: str) -> list[AuditEntry]:
        return [e for e in self._entries if e.event == event]

    # -- integrity ---------------------------------------------------------
    def digest(self) -> str:
        """
        Hash of the full ordered log. Two runs of the same batch under the
        same config must produce the same digest -- that is the machine-checkable
        form of 'deterministic replay' (NFR-32).
        """
        h = hashlib.sha256()
        for e in self._entries:
            h.update(f"{e.seq}|{e.case_id}|{e.event}|{e.summary}".encode())
        return h.hexdigest()[:16]

    def export_jsonl(self) -> str:
        return "\n".join(json.dumps(e.to_dict(), default=str) for e in self._entries)

    def clear(self) -> None:
        self._entries.clear()


def config_fingerprint(cfg: Any) -> str:
    """Short stable hash of a config object, stamped onto every entry."""
    payload = json.dumps(cfg.to_dict() if hasattr(cfg, "to_dict") else cfg,
                         sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]
