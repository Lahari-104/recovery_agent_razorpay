# Recovery run

- Batch: **500** failed payments, seed `20260824`
- Classifier: rules-only
- Runtime: 238 ms
- Audit digest: `a49b2f44aa46aacf` (3671 entries)
- At risk: **Rs 813,954**

## Result

| Policy | Recovered | Spent | Net | Rate | Attempts |
|---|---:|---:|---:|---:|---:|
| Blind retry | Rs 196,328 | Rs 385 | Rs 195,943 | 24.1% | 1100 |
| **This agent** | Rs 433,176 | Rs 1,218 | Rs 431,958 | 53.2% | 881 |
| Perfect play | Rs 496,092 | Rs 876 | Rs 495,216 | 60.9% | 359 |

The agent recovers **Rs 236,016 more** than blind retry (120.5%) on the identical batch, and captures **87.2%** of what perfect knowledge would achieve.

It does this with **fewer** attempts than blind retry (881 vs 1100), because most of the intelligence is in not acting.

## What we did not recover

200 payments were not recovered. Of those, **111** could not have been recovered by any action — dead instruments, frozen accounts, customers who were never returning. The remaining **89** were winnable and we missed them.

| Amount | Diagnosis | Ended as | Reason |
|---:|---|---|---|
| Rs 25,000 | network_timeout | abandoned | Reached the 3-attempt cap for this payment. |
| Rs 11,780 | insufficient_funds | abandoned | Customer has opted out of payment reminders; no contact permitted. |
| Rs 10,506 | cancelled_by_user | abandoned | Customer has been nudged twice without completing. Further contact wou |
| Rs 9,926 | card_blocked | abandoned | card blocked cannot be recovered by any automated action. Stopping del |
| Rs 8,839 | card_expired | abandoned | card expired cannot be recovered by any automated action. Stopping del |
| Rs 6,755 | cancelled_by_user | abandoned | Customer has been nudged twice without completing. Further contact wou |
| Rs 6,723 | cancelled_by_user | abandoned | Customer has been nudged twice without completing. Further contact wou |
| Rs 6,077 | insufficient_funds | abandoned | Reached the 3-attempt cap for this payment. |
| Rs 6,005 | network_timeout | abandoned | Reached the 3-attempt cap for this payment. |
| Rs 5,688 | issuer_unavailable | abandoned | Reached the 3-attempt cap for this payment. |
| Rs 5,663 | otp_not_entered | abandoned | Customer has been nudged twice without completing. Further contact wou |
| Rs 5,659 | issuer_unavailable | abandoned | Reached the 3-attempt cap for this payment. |
| Rs 5,498 | insufficient_funds | abandoned | Reached the 3-attempt cap for this payment. |
| Rs 5,388 | cancelled_by_user | abandoned | Customer has opted out of payment reminders; no contact permitted. |
| Rs 5,319 | card_expired | abandoned | card expired cannot be recovered by any automated action. Stopping del |

## Reproducing this

```
python run_eval.py --size 500 --seed 20260824 --no-llm
```

Same seed, same digest (`a49b2f44aa46aacf`), same numbers.