# Demo script

Four minutes, rehearsed. Every claim has a click behind it.

**Before you start:** server running, browser at `http://127.0.0.1:8000`, batch already
loaded (open the page once beforehand so the first render isn't a wait). Have a terminal
open on a second window with the test suite ready to run.

---

## 0:00 – 0:30 — The problem

> "A customer's payment fails. Every merchant does the same thing: a cron retries it three
> times, immediately, on the same card.
>
> That's wrong three different ways at once. Retrying a blocked card can never work.
> Retrying a low balance thirty seconds later fails for the same reason it failed the
> first time. And retrying an abandoned checkout does nothing at all — there's nobody at
> the keyboard to approve it.
>
> Each of those needs a different answer. Telling them apart is the whole job."

Don't touch the screen yet. Let them hear the problem before they see anything.

---

## 0:30 – 1:30 — The result

Point at the hero band.

> "500 failed payments, ₹8.1 lakh at risk.
>
> Grey is blind retry — what merchants do today. 24%.
> Brass is us. 53%.
> The blue line is perfect play, if you knew in advance exactly what would work. 61%.
>
> We recover **120% more than blind retry, using 20% fewer attempts**."

Pause on that last sentence. Recovering more while doing less is the counterintuitive bit —
give them a second to notice it.

> "And we capture 87% of the theoretical ceiling. The 13% we're missing is on the screen
> too. I'll show you."

---

## 1:30 – 2:30 — One case, all the way down

**Cases tab.** Click any row where the diagnosis is `bank downtime`.

Point at the decision chain in the drawer.

> "This is one payment. HDFC UPI was down when it failed.
>
> The agent didn't retry. It checked Razorpay's downtime API — which almost nobody
> consumes — saw the rail had forty minutes left, and scheduled for after that.
>
> Every decision carries the rule that produced it. `SOFT-BD-01`. Not 'the model decided'
> — a named rule, with the reason in plain English, and the money it cost."

**Click Replay.**

> "That just recomputed the entire decision from scratch. Identical outcome, identical rule
> sequence. Nothing here depends on a coin flip — which means any decision the agent made
> can be audited after the fact."

---

## 2:30 – 3:15 — What it refused to do

**Refusals tab.**

> "Ninety-two times the agent wanted to act and a hard limit said no.
>
> Customer already contacted enough today. Quiet hours — we don't message people at
> midnight. Would cost more to chase than the order is worth. Card is dead, so a same-rail
> retry is structurally forbidden.
>
> These are recorded as events, not silent skips. An agent that declines to spend the
> merchant's money is doing its job, and you should be able to see it happen."

If they look interested, add:

> "Every one of those limits is an integer comparison in Python. None of them is a prompt.
> A prompt can be ignored — `if attempts >= cap: refuse` can't."

---

## 3:15 – 4:00 — Make the limits move

**What-if tab.** Drag cooldown to 0. Click **Run with these limits**.

> "The limits aren't claims on a slide. Move one and watch the money move.
>
> Zero cooldown is exactly what a retry cron does. It burns attempts against banks that
> haven't changed their mind yet."

Then drag **max spend** down to 2% and run again.

> "And here's the other direction — starve the budget and the agent stops chasing things
> worth chasing.
>
> The whole policy is configuration. It's versioned, it's diffable, and every result on
> this screen is reproducible from a seed."

---

## Hold in reserve for Q&A

Don't volunteer these. Deploy when asked.

### "How do you know it beat doing nothing?"
**Recovery tab → hero band.** The baseline is a genuinely separate policy — `NaivePolicyEngine`
— that never consults the diagnosis. Running the smart policy under loose settings and calling
it a baseline would just be comparing the agent against itself.

### "Is that number cherry-picked?"
**Not recovered tab.**
> "200 payments we didn't recover. 111 could never have been recovered by anything — dead
> cards, frozen accounts, customers who were never coming back. The other **89 were winnable
> and we missed them.** That's the honest number."

Volunteering your own failure count is the most credible thing in the demo.

### "What stops it draining a customer's account?"
Terminal: `python tests\test_invariants.py`
> "Twenty-nine invariants. The no-double-charge one checks the money and the attempt log,
> not a status flag — an invariant that depends on someone remembering to set a field isn't
> an invariant."

### "What if the LLM goes down?"
Point at that same output:
> `full batch completes with the LLM down -- recovered Rs 117,184`
>
> "Rules resolve every mapped gateway code without a model at all. The LLM only handles
> unmapped codes and free text. If it's unreachable we fall back to keywords, flag it in the
> header, and keep recovering."

### "Is the LLM deciding money movement?"
> "No. It classifies and it writes explanations. Every limit is enforced in the executor,
> in code. The model has no path to the money."

### "Does the kill switch actually work?"
Toggle it in the header, then go to **Live webhook** and inject a failure.
> "Zero attempts. And there's a regression test for it — the first version flipped in the
> UI and changed nothing, because the running agent held its own copy of the config. That's
> the worst possible bug for a safety control, so it's now covered by a test."

### "Does this work on real Razorpay?"
**Live webhook tab.** Inject a failure.
> "That went through the same handler Razorpay calls — signature check, duplicate
> suppression, normalisation, then the agent. Not a separate demo path. Same engine."

### "What about compliance?"
> "Quiet hours, per-customer frequency caps across all their payments not just this one,
> permanent opt-out, and an economic floor so we don't chase a ₹40 order. All visible in
> the Refusals tab when they fire."

---

## Things to say before they're found

Weaknesses stated first read as rigour. Found by a judge, they read as gaps.

**The batch is synthetic.** Recoverability profiles are modelled on how Indian payment
failures behave, but they're a model. The agent's *relative* advantage over the baseline is
the meaningful claim.

**Payment links cost 7× a retry and succeed less often.** Visible on your own dashboard —
33% vs 45%. The reason is that links go to drop-offs and dead cards where retries can't work
at all, so the comparison isn't like-for-like. Have that ready.

**13% short of the ceiling, mostly on timing.** Scheduled retries are rule-based. A learned
timing model would close part of it. That's the honest next step, not a hidden flaw.

---

## If something breaks live

Don't debug on stage. Go to the terminal and run `python run_eval.py --size 500`. Every
headline number is in that output, in ten seconds, with no browser involved.
