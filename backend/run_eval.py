#!/usr/bin/env python3
"""
Reproduce the headline numbers from a terminal, without the UI.

    python run_eval.py                  # 500 cases, default policy
    python run_eval.py --size 2000      # bigger batch
    python run_eval.py --seed 42        # different world
    python run_eval.py --no-llm         # force the rules-only path
    python run_eval.py --report out.md  # write a metrics report

Anyone can run this and get the same figures shown in the console. That is
the point: the number on the slide is a command away from being checked.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import DEFAULT_CONFIG
from app.core.classifier import Classifier, build_classifier
from app.eval.harness import run_comparison
from app.sim.world import RecoveryProfile, generate_batch

BAR = "=" * 72


def rupees(paise: int) -> str:
    return f"Rs {paise/100:,.0f}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Revenue recovery agent evaluation")
    ap.add_argument("--size", type=int, default=500)
    ap.add_argument("--seed", type=int, default=20260824)
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--report", type=str, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    clf = Classifier(llm_client=None) if args.no_llm else build_classifier(True)
    llm_state = "rules-only" if clf.llm is None else "gemini + rules"

    t0 = time.perf_counter()
    payments, world = generate_batch(args.size, seed=args.seed)
    cmp = run_comparison(payments, world, DEFAULT_CONFIG, clf)
    elapsed = (time.perf_counter() - t0) * 1000

    s = cmp.summary()
    if args.json:
        print(json.dumps(s, indent=2))
        return 0

    a, n, o = cmp.agent, cmp.naive, cmp.oracle

    print(BAR)
    print(f"REVENUE RECOVERY AGENT  |  {args.size} failed payments  |  seed {args.seed}")
    print(f"classifier: {llm_state}   policy: {DEFAULT_CONFIG.version}   "
          f"runtime: {elapsed:.0f} ms")
    print(BAR)
    print(f"\nAt risk in this batch: {rupees(a.at_risk_paise)}\n")

    print(f"{'policy':<10}{'recovered':>14}{'spent':>10}{'net':>14}{'rate':>8}{'tries':>8}")
    print("-" * 66)
    for label, r in (("blind", n), ("agent", a), ("perfect", o)):
        print(f"{label:<10}{rupees(r.recovered_paise):>14}{rupees(r.spend_paise):>10}"
              f"{rupees(r.net_paise):>14}{r.recovery_rate*100:>7.1f}%{r.attempts_total:>8}")

    print(f"\n  Agent beats blind retry by {rupees(a.net_paise - n.net_paise)} "
          f"({s['uplift_pct']}%)")
    print(f"  Agent captures {s['capture_of_ceiling_pct']}% of what perfect play would get")
    print(f"  Agent spends {rupees(int(a.cost_per_recovery_paise))} per recovered payment")
    print(f"  Agent uses {a.attempts_total/max(1,a.recovered_count):.1f} attempts per recovery "
          f"vs {n.attempts_total/max(1,n.recovered_count):.1f} for blind retry")

    print("\nBY DIAGNOSED CAUSE")
    print("-" * 66)
    for k, v in sorted(a.by_cause.items(), key=lambda kv: -kv[1]["at_risk_paise"]):
        rate = v["recovered_paise"] / v["at_risk_paise"] * 100 if v["at_risk_paise"] else 0
        print(f"  {k:<16}{v['count']:>5} cases  {rupees(v['recovered_paise']):>12}"
              f" of {rupees(v['at_risk_paise']):>12}  {rate:>5.1f}%")

    print("\nBY INTERVENTION")
    print("-" * 66)
    for k, v in sorted(a.by_action.items(), key=lambda kv: -kv[1]["used"]):
        sr = v["succeeded"] / v["used"] * 100 if v["used"] else 0
        print(f"  {k:<18}{v['used']:>5} used  {sr:>5.1f}% worked  "
              f"cost {rupees(v['spend_paise']):>10}")

    unrec = sum(1 for e in cmp.exceptions if e.truth_profile == "unrecoverable")
    print("\nWHAT WE DID NOT RECOVER")
    print("-" * 66)
    print(f"  {len(cmp.exceptions)} unrecovered payments listed")
    print(f"  {unrec} were never recoverable by any action")
    print(f"  {len(cmp.exceptions)-unrec} were winnable and we missed them")
    print("\n  Largest misses:")
    for e in cmp.exceptions[:5]:
        print(f"    Rs {e.amount_rupees:>9,.0f}  {e.subcause:<22}{e.reason[:44]}")

    print(f"\nAUDIT  {len(cmp.audit)} entries  digest {cmp.audit.digest()}")
    print("  Re-running this command with the same seed reproduces this digest exactly.")
    print(BAR)

    if args.report:
        Path(args.report).write_text(build_report(cmp, args, llm_state, elapsed))
        print(f"\nReport written to {args.report}")
    return 0


def build_report(cmp, args, llm_state: str, elapsed: float) -> str:
    a, n, o = cmp.agent, cmp.naive, cmp.oracle
    s = cmp.summary()
    unrec = sum(1 for e in cmp.exceptions if e.truth_profile == "unrecoverable")

    lines = [
        "# Recovery run",
        "",
        f"- Batch: **{args.size}** failed payments, seed `{args.seed}`",
        f"- Classifier: {llm_state}",
        f"- Runtime: {elapsed:.0f} ms",
        f"- Audit digest: `{cmp.audit.digest()}` ({len(cmp.audit)} entries)",
        f"- At risk: **Rs {a.at_risk_paise/100:,.0f}**",
        "",
        "## Result",
        "",
        "| Policy | Recovered | Spent | Net | Rate | Attempts |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for label, r in (("Blind retry", n), ("**This agent**", a), ("Perfect play", o)):
        lines.append(
            f"| {label} | Rs {r.recovered_paise/100:,.0f} | Rs {r.spend_paise/100:,.0f} "
            f"| Rs {r.net_paise/100:,.0f} | {r.recovery_rate*100:.1f}% | {r.attempts_total} |")

    lines += [
        "",
        f"The agent recovers **Rs {(a.net_paise-n.net_paise)/100:,.0f} more** than blind "
        f"retry ({s['uplift_pct']}%) on the identical batch, and captures "
        f"**{s['capture_of_ceiling_pct']}%** of what perfect knowledge would achieve.",
        "",
        f"It does this with **fewer** attempts than blind retry "
        f"({a.attempts_total} vs {n.attempts_total}), because most of the intelligence "
        f"is in not acting.",
        "",
        "## What we did not recover",
        "",
        f"{len(cmp.exceptions)} payments were not recovered. Of those, **{unrec}** could "
        f"not have been recovered by any action — dead instruments, frozen accounts, "
        f"customers who were never returning. The remaining **{len(cmp.exceptions)-unrec}** "
        f"were winnable and we missed them.",
        "",
        "| Amount | Diagnosis | Ended as | Reason |",
        "|---:|---|---|---|",
    ]
    for e in cmp.exceptions[:15]:
        lines.append(f"| Rs {e.amount_rupees:,.0f} | {e.subcause} | {e.final_state} "
                     f"| {e.reason[:70]} |")

    lines += [
        "",
        "## Reproducing this",
        "",
        "```",
        f"python run_eval.py --size {args.size} --seed {args.seed}"
        + (" --no-llm" if args.no_llm else ""),
        "```",
        "",
        f"Same seed, same digest (`{cmp.audit.digest()}`), same numbers.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
