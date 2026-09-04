# Requirements

Every requirement, where it is implemented, and what verifies it.

`tests/test_invariants.py` is the verification for anything marked with a test. Run it with
`python tests/test_invariants.py`.

---

## Functional — stated in the track brief

| ID | Requirement | Where | Verified by |
|---|---|---|---|
| FR-1 | Detect revenue at risk | `agent.ingest` | Console hero, `run_eval.py` |
| FR-2 | Diagnose the underlying cause | `core/classifier.py` | Cases tab, per-case drill-down |
| FR-3 | Select the right intervention per case | `core/policy.py` | Rule id on every decision |
| FR-4 | Execute a bounded recovery workflow | `core/executor.py` | `test_caps_enforced` |
| FR-5 | Measure money recovered across a batch | `eval/harness.py` | `test_comparison_fairness` |
| FR-6 | Compliant escalation | quiet hours, caps, opt-out | `test_quiet_hours` |
| FR-7 | Stopping rules | `STOP-*`, `ECON-*`, `HARD-*` | Exceptions tab |
| FR-8 | Audit trail | `core/audit.py` | Audit tab, digest |

---

## Functional — added

The brief lists outcomes. These are the mechanisms that produce them.

### Ingestion and integrity

| ID | Requirement | Where |
|---|---|---|
| FR-9 | Accept `payment.failed` webhooks | `POST /api/webhook/razorpay` |
| FR-10 | Verify signature; reject unsigned | `verify_signature` |
| FR-11 | Idempotent event handling | `seen_event_ids`, executor `_idem` |
| FR-12 | One schema for live and synthetic | `normalise_razorpay` |
| FR-13 | Batch ingestion for offline eval | `sim/world.generate_batch` |

### Classification

| ID | Requirement | Where |
|---|---|---|
| FR-14 | Three-tier taxonomy | `domain.Cause` |
| FR-15 | Subcause resolution (20 subcauses) | `domain.SubCause` |
| FR-16 | Confidence score on every classification | `Classification.confidence` |
| FR-17 | Rules before LLM | `Classifier.classify` path order |
| FR-18 | Human-readable reason | `Classification.reason` |

### Decision and policy

| ID | Requirement | Where |
|---|---|---|
| FR-19 | Five-action allowlist | `domain.Action` |
| FR-20 | Consult downtime before retrying | `core/downtime.py`, rules `DOWN-01/02` |
| FR-21 | Timing intelligence | `_compliant_delay`, `SOFT-IF-01` |
| FR-22 | Confidence-gated escalation | rule `CONF-01` |
| FR-23 | Versioned policy configuration | `config.PolicyConfig.version` |

### Bounded execution

| ID | Requirement | Where |
|---|---|---|
| FR-24 | Max attempts per payment | guard `CAP-ATTEMPTS` |
| FR-25 | Max touches per customer per 24h | guard `CAP-CUSTOMER`, `CustomerLedger` |
| FR-26 | Minimum cooldown | guard `CAP-COOLDOWN` (deferrable) |
| FR-27 | Never-retry list | guard `INV-NEVERRETRY` |
| FR-28 | Global kill switch | guard `KILL`, header toggle |
| FR-29 | Dry-run mode | `PolicyConfig.dry_run` |

### Measurement

| ID | Requirement | Where |
|---|---|---|
| FR-30 | Naive baseline policy | `NaivePolicyEngine` |
| FR-31 | Oracle ceiling | `harness.run_oracle` |
| FR-32 | Per-intervention cost accounting | `PolicyConfig.cost_of` |
| FR-33 | Net recovery metric | `PolicyResult.net_paise` |
| FR-34 | Per-cause breakdown | `/api/eval/breakdown` |
| FR-35 | Exception report | `/api/eval/exceptions` |
| FR-36 | Policy simulator | `/api/simulate`, What-if tab |

### Audit

| ID | Requirement | Where |
|---|---|---|
| FR-37 | Append-only log with provenance | `core/audit.py` |
| FR-38 | Deterministic replay | `/api/audit/replay/{id}`, Replay button |
| FR-39 | Per-transaction drill-down | Case drawer |

---

## Non-functional

### Correctness under concurrency

| ID | Requirement | Where | Test |
|---|---|---|---|
| NFR-1 | Exactly-once effects from at-least-once delivery | executor `_idem` | `test_idempotency` |
| NFR-2 | Explicit state machine, illegal transitions rejected | `LEGAL_TRANSITIONS` | `test_state_machine` |
| NFR-3 | **No double charge under any sequence** | guard `INV-NDC` | `test_no_double_charge` |

### Bounded autonomy

| ID | Requirement | Where | Test |
|---|---|---|---|
| NFR-4 | Limits in code, never in prompts | `executor._guards` | `test_caps_enforced` |
| NFR-5 | Allowlisted action set | `domain.Action` | type system |
| NFR-6 | Every action traceable to a rule | `Decision.policy_rule` | Audit tab |

### Reliability

| ID | Requirement | Where | Test |
|---|---|---|---|
| NFR-7 | LLM outage → rules fallback, flagged | `Classifier` path 4 | `test_llm_outage` |
| NFR-8 | Gateway outage → degrade, never stall | `LiveDowntimeOracle._refresh` | — |
| NFR-11 | Deferrals resume rather than closing | `DEFERRABLE_GUARDS` | recovery rate |

### Performance

| ID | Requirement | Target | Measured |
|---|---|---|---|
| NFR-12 | Webhook ack fast, process out of band | < 2s | `BackgroundTasks`, ms-level ack |
| NFR-13 | 500-case batch evaluation | < 60s | **~180 ms** |
| NFR-14 | UI interaction latency | < 200ms | cached summary |

### Security and compliance

| ID | Requirement | Where | Test |
|---|---|---|---|
| NFR-18 | Secrets from environment only | `os.getenv`, `.env.example` | no secrets in repo |
| NFR-19 | Signature verification mandatory | `verify_signature` | — |
| NFR-20 | PII masked in logs | `audit.mask_pii` | `test_pii_masking` |
| NFR-21 | Test keys only | `.env.example` | — |
| NFR-22 | Quiet hours enforced | guard `COMP-QUIET` | `test_quiet_hours` |
| NFR-23 | Contact frequency caps | `CustomerLedger` | `test_caps_enforced` |
| NFR-24 | Opt-out honoured | guard `COMP-OPTOUT` | `test_caps_enforced` |
| NFR-26 | Escalation informational, never coercive | rule reason text | review |

### Observability

| ID | Requirement | Where |
|---|---|---|
| NFR-27 | Structured entries with correlation id | `AuditEntry.correlation_id` |
| NFR-28 | Live counters | hero stat strip |
| NFR-29 | Refusals are first-class events | Refusals tab |

### Testability

| ID | Requirement | Where | Test |
|---|---|---|---|
| NFR-30 | Seeded, reproducible generation | `generate_batch(seed=)` | `test_determinism` |
| NFR-31 | Fair comparison across policies | pre-generated draw stream | `test_comparison_fairness` |
| NFR-32 | Deterministic replay | `World.draw`, audit digest | `test_determinism` |

### Cost control

| ID | Requirement | Where |
|---|---|---|
| NFR-33 | LLM cache for repeated signatures | `Classifier._cache` |
| NFR-34 | Rules resolve the majority | `ERROR_CODE_MAP` covers all mapped codes |

### Maintainability

| ID | Requirement | Where |
|---|---|---|
| NFR-35 | Policy as versioned config | `PolicyConfig` |
| NFR-36 | Clean layer separation | `domain` ← `core` ← `eval`/`api` |

---

## Deliberately out of scope

Named so the omissions read as decisions rather than gaps.

Voice recovery, multi-language messaging, real SMS/WhatsApp delivery, subscription mandate
handling, B2B receivables, authentication, multi-merchant tenancy. Each is a plausible
addition that would have cost a day and added nothing to the track bar.
