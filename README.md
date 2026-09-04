# Recovery Console

**A failed-payment recovery agent that diagnoses why a payment failed, chooses the right
intervention, executes it under hard limits, and proves how much money it got back.**

Razorpay AI Buildathon 2026 — Track 03, AI Revenue Recovery.

---

## The problem

A merchant's payment fails. Today, almost every merchant does the same thing: a cron job
retries it three times, immediately, on the same instrument, regardless of why it failed.

That is wrong in three different ways at once. Retrying a blocked card can never work and is
penalised by card networks. Retrying a low-balance account thirty seconds later fails for the
same reason it failed the first time. And retrying an abandoned checkout does nothing at all,
because there is nobody at the keyboard to authorise it.

Each of those failures needs a different response. Telling them apart is the entire job.

## The result

500 synthetic failed payments, ₹8.1L at risk, identical batch across all three policies:

| Policy | Recovered | Spent | Net | Rate | Attempts |
|---|---:|---:|---:|---:|---:|
| Blind retry (what merchants do now) | ₹196,328 | ₹385 | ₹195,943 | 24.1% | 1,100 |
| **This agent** | **₹433,176** | ₹1,218 | **₹431,958** | **53.2%** | **881** |
| Perfect play (theoretical ceiling) | ₹496,092 | ₹876 | ₹495,216 | 60.9% | 359 |

**₹236,016 more recovered than blind retry — a 120.5% improvement — using 20% fewer
attempts.** The agent captures 87.2% of what perfect knowledge would achieve.

Reproduce it yourself:

```bash
cd backend && python run_eval.py --size 500 --seed 20260824
```

Same seed, same audit digest (`a49b2f44aa46aacf`), same numbers. Every time.

---

## Why the numbers are honest

Three deliberate choices, because a recovery figure with nothing to compare it against is
not evidence of anything.

**The baseline is a genuinely different policy, not this one with loose settings.** It is
easy to make an agent look good by comparing it against itself under a permissive config.
`NaivePolicyEngine` never consults the diagnosis, never checks downtime, never switches
method, never nudges anyone. It is what a retry cron actually does.

**The ceiling is reported alongside the result.** An oracle with perfect knowledge of what
would have worked — but bound by the *same* attempt caps, costs and never-retry rules the
agent obeys. It recovers 60.9%. We recover 53.2%. Stating the 7.7-point gap is more
credible than any single number, and it pre-empts "how do we know that's good?"

**All three policies face identical luck.** Random outcomes are pre-generated per case and
indexed by attempt number, derived from a SHA-256 of the batch seed. If two policies take
the same action at the same time, they get the same result. Differences come from decisions,
never from dice.

**The headline metric is net, not gross.** Every intervention costs money — a retry costs a
gateway fee, an SMS link costs more, a human escalation costs a few minutes of salary. Net
recovery is gross minus spend. This is what makes the stopping rules pay off numerically
instead of being a compliance slide.

---

## How it works

```
payment.failed ──► ingest ──► classify ──► decide ──► execute ──► audit
   (webhook          FR-9       FR-14        FR-19      FR-24       FR-37
    or batch)      normalise   rules→LLM   policy+     hard caps   append
                     FR-12      fallback   downtime    in code      only
```

**Ingest.** Real Razorpay webhooks and synthetic batch records normalise into one
`FailedPayment`. The live path is not a separate toy implementation — it is the same engine.

**Classify.** Three buckets (soft decline, hard decline, user drop-off) and twenty
subcauses. Deterministic rules resolve every mapped gateway code; the LLM is reserved for
unmapped codes and free-text error descriptions. A blocked card is a hard decline — that is
a fact, not a judgement call, and routing it through a language model would add latency,
cost and a failure mode in exchange for nothing.

**Decide.** Cause plus context maps to one of five actions: retry now, schedule retry,
alternate-method payment link, escalate to a human, or stop. Every decision carries the
identifier of the rule that produced it. "The model decided" is not an audit trail;
"`SOFT-IF-01` fired because the subcause is insufficient funds and this is attempt 1 of 3"
is.

**Execute.** Hard caps enforced here, in Python, by integer comparison. Never as instructions
to a language model. A prompt can be ignored, misread, or talked around. `if attempts >=
cap: refuse` cannot.

**Audit.** Append-only. Every entry carries the policy version, config fingerprint and
correlation id needed to recompute the decision later. There is deliberately no update or
delete method.

---

## What makes it an agent rather than a retry script

**It checks whether the rail is even alive.** Razorpay publishes a downtime API reporting
which banks and methods are currently degraded. Almost nobody consumes it. Before scheduling
any retry, the agent asks — and if HDFC UPI is down for another 40 minutes, it either waits
that long or switches the customer to a card, rather than burning an attempt on a rail it
knows is dead.

**It knows when *later* means later.** A low balance is not retried in thirty seconds; it is
scheduled for the following morning when accounts get funded. A network timeout, by contrast,
is retried immediately, because the authorisation window is still open. Same "soft decline"
bucket, opposite correct answers.

**It refuses to act, visibly.** Refusals are recorded as first-class events, not silent
skips. The console has a whole tab for them. An agent choosing *not* to spend the merchant's
money — because the customer has been contacted enough, or the chase would cost more than the
order is worth — is the behaviour worth demonstrating.

**It escalates instead of guessing.** When the classifier's confidence falls below threshold,
the case goes to a human review queue rather than the agent picking an action on a diagnosis
it does not trust.

**It counts the cost of its own actions.** The economic ceiling means the agent will not spend
₹40 chasing a ₹400 order, and will not chase a ₹40 order at all.

---

## Safety and reliability

These are the properties a judge will probe, and each one is tested rather than claimed.
Run `python tests/test_invariants.py`.

| Invariant | How it is enforced |
|---|---|
| **No double charge** | A case with any successful attempt refuses all further money actions. Checked against the money and the attempt log, not a state flag the caller might forget to set. |
| **At-least-once delivery, exactly-once effect** | Idempotency ledger keyed on `(case_id, seq)`. Duplicate webhooks re-read the prior result. Deferrals deliberately do not consume the slot. |
| **Limits cannot be argued with** | Every cap is a config constant read by the executor. None is expressed in a prompt. |
| **Works without the LLM** | Connection error or unparseable output falls through to a keyword heuristic. Full batch completes with the LLM down; degraded mode is flagged in the UI. |
| **Illegal states are impossible** | Explicit state machine. `detected → recovered` raises. Terminal states accept nothing. |
| **Byte-identical replay** | Same seed produces the same audit digest and the same net recovery, across processes. Any case can be recomputed from scratch and diffed via the Replay button. |
| **Quiet hours respected** | No customer contact between 21:00 and 08:00 IST. Zero violations across the batch. |
| **PII never logged** | Phone numbers and emails masked on write to the audit trail. |
| **Kill switch** | One toggle halts every action immediately. |
| **Dry run** | Full pipeline walks end to end and performs nothing. |

A note on how one of these was found: the state machine caught an illegal `blocked → decided`
transition during development. The underlying bug was that cooldown and quiet-hours blocks
were being treated as *terminal* rather than as "not yet" — which was silently destroying
about half the achievable recovery. The invariant found a real defect, which is what
invariants are for.

---

## Running it

```bash
cd backend
pip install -r requirements.txt

# headline numbers in the terminal
python run_eval.py --size 500

# all safety invariants
python tests/test_invariants.py

# the console
uvicorn app.main:app --reload
# open http://127.0.0.1:8000
```

Optional — the LLM path for ambiguous errors:

```bash
export GEMINI_API_KEY=your_key
```

Without it the header reads **rules engine** and everything works — the rules table
resolves every mapped gateway code without a model at all. That is a mode, not a fault.
The warning banner appears only when a key IS configured and the model is unreachable,
which is the case actually worth flagging.

Optional — real Razorpay test mode:

```bash
export RAZORPAY_KEY_ID=rzp_test_xxx
export RAZORPAY_KEY_SECRET=xxx
export RAZORPAY_WEBHOOK_SECRET=xxx
```

Point a Razorpay test-mode webhook at `POST /api/webhook/razorpay` (use ngrok for local
development) and subscribe to `payment.failed`. Signature verification activates
automatically once the webhook secret is present.

The **Live webhook** tab fires synthetic events through the identical handler — signature
check, duplicate suppression, normalisation, agent — so the flow can be demonstrated without
waiting on a sandbox event.

---

## Requirements traceability

Everything the track brief asks for, and where it lives.

### Stated in the brief

| | Requirement | Implementation |
|---|---|---|
| FR-1 | Detect revenue at risk | `agent.ingest`, webhook + batch |
| FR-2 | Diagnose the cause | `core/classifier.py` |
| FR-3 | Choose the right intervention | `core/policy.py` |
| FR-4 | Bounded recovery workflow | `core/executor.py` |
| FR-5 | Measured money across a batch | `eval/harness.py`, `run_eval.py` |
| FR-6 | Compliant escalation | quiet hours, frequency caps, opt-out |
| FR-7 | Stopping rules | `STOP-*`, `ECON-*`, `HARD-*` rules |
| FR-8 | Audit trail | `core/audit.py`, Audit tab |

### Added because the brief lists outcomes, not mechanisms

Signature verification, idempotent handling, normalisation, three-tier taxonomy with
subcauses, confidence scores, rules-before-LLM routing, downtime consultation, timing
intelligence, confidence-gated escalation, versioned policy config, per-payment and
per-customer caps, cooldowns, never-retry list, kill switch, dry run, naive baseline, oracle
ceiling, per-intervention costing, net recovery, per-cause breakdown, exception report,
policy simulator, deterministic replay, per-case drill-down.

Demo walkthrough with judge Q&A: [`docs/DEMO.md`](docs/DEMO.md).
Full numbered list in [`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md), including all 36
non-functional requirements and which test covers each.

---

## Layout

```
backend/
  app/
    domain.py              types, taxonomy, state machine
    config.py              every cap and cost, versioned
    core/
      classifier.py        rules → LLM → heuristic
      policy.py            intervention rules + naive baseline
      executor.py          hard caps, idempotency, invariants
      downtime.py          rail health (simulated + live Razorpay)
      audit.py             append-only trail, PII masking
      agent.py             the loop
    sim/world.py           synthetic batch + hidden ground truth
    eval/harness.py        naive vs agent vs oracle
    main.py                API + webhook
  tests/test_invariants.py
  run_eval.py
frontend/index.html        the console
```

---

## Known limits

Worth stating plainly rather than being found.

The batch is synthetic. Recoverability profiles are modelled on how Indian payment failures
actually behave, but they are a model. The agent's *relative* advantage over the baseline is
the meaningful result; the absolute recovery rate depends on the profile mix.

The oracle is a ceiling under the current action space. A richer intervention set — voice,
WhatsApp, partial payments — would raise it.

Live mode creates payment links rather than force-charging. That is deliberate: a link is the
only intervention that is honest to perform for real against a sandbox merchant.

The gap to the ceiling is mostly timing. The agent's scheduled retries are rule-based; a
learned timing model would close part of it.

Payment links cost seven times a retry and succeed less often (33% vs 45%) — visible on the
dashboard. The comparison isn't like-for-like, since links go to drop-offs and dead cards
where retries cannot work at all, but the economics are worth watching.
