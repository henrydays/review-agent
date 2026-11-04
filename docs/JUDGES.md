# Choosing a judge

Which model should score this agent, and does a stronger one change the result? Measured on 2026-11-03, on training PRs only. Sonnet 5 is never used: the rule in `AGENTS.md` is Sonnet 5.5 or better. To repeat this, see `bench/judge_study.py`.

## The answer

| Job | Use | Why |
|---|---|---|
| Matching findings to the golden set | Sonnet 5.5, default effort | Stronger judges almost never decide differently |
| Classifying unmatched findings, every round | Sonnet 5.5, default effort, on a sample | Cheap and strict, so the loop steers toward findings even a strict judge accepts |
| Auditing a milestone | Opus 5.5 max, on about 100 findings | It checks the code. Report its number next to Sonnet's, never instead of it |

The judge has two separate jobs, and they gave opposite results.

## Matching: Sonnet 5.5 is enough

Three reviewers (Haiku pipeline, Opus 5.5 max, GPT-6 Astra max) on 8 PRs, 4,124 pairs of findings, each judged by three models.

| Judges | Pair agreement | Kappa | Golden decisions flipped, of 246 |
|---|---:|---:|---:|
| Sonnet 5.5 / Opus 5.5 max | 99.8% | 0.973 | 2 |
| Sonnet 5.5 / Astra max | 99.5% | 0.945 | 4 |
| Opus 5.5 max / Astra max | 99.7% | 0.962 | 2 |

All three rank the reviewers in the same order, with recall within 1.6 points. Nothing here justifies paying more.

**No family bias showed up.** Astra judging its own findings gives the same recall as both Claude judges (40.6). Opus judging its own findings scores them slightly lower than Sonnet does (56.6 against 57.4).

## Classifying: the judges disagree, and it matters

Classifying decides whether a finding outside the golden set is real, so it sets augmented precision. We took a balanced set of 40 findings from the Haiku pipeline, half of which Sonnet 5.5 had called true and half false, and asked stronger judges.

| Judge | Of Sonnet's true calls, overturned | Of Sonnet's false calls, overturned | Tool calls per finding | Cost per finding |
|---|---:|---:|---:|---:|
| Sonnet 5.5, high effort | 2 of 16 | 2 of 19 | 1.7 | $0.07 |
| Opus 5.5 max | 2 of 17 | 8 of 18 | 15.5 | $0.54 |
| Astra, high effort | 1 of 20 | 15 of 19 | 2.7 | not reported |

Sonnet 5.5 at default effort cost about $0.04 per finding.

What this shows:

- **When Sonnet says true, others agree.** Its false calls are where they part.
- **Sonnet is stricter, and part of that is shallow checking.** On a mostly-true sample of 43 findings, Sonnet at default effort said in its own reasoning that it had not verified the claim on 10, against 1 of 36 for Opus. Raising Sonnet to high effort did not bring it closer to the stronger judges.
- **Much of the disagreement is about value, not fact.** The stronger judges confirm a claim is true and accept it. Sonnet rejects it as not worth raising, most clearly for findings that ask for tests in a repository with none. Opus accepted 4 of 6 of those and Astra 6 of 6. The human-audited golden set also accepts this class more often than not: of 29 findings on training PRs that ask for missing tests, in any repository, 21 are labeled true.
- **Astra is the most lenient.** It called all 21 findings it answered true on the first sample, and overturned 15 of 19 here. A judge that rarely says no cannot show that a change made findings worse.

Weighted back to all unmatched findings, Sonnet's 74.5% true rate becomes about 87% if the Opus and Astra consensus is right. That estimate rests on 28 findings.

## An audit of a full run

On 2026-11-04, Opus 5.5 at max effort re-classified a sample of Sonnet 5.5's labels from [a full run](RESULTS.md): 50 findings Sonnet called true and 50 it called false, across all 219 PRs, read only in aggregate because most of those PRs are `val` or `holdout`. It answered 88.

| Sonnet 5.5 said | Opus 5.5 max agreed | Opus overturned |
|---|---:|---:|
| True | 39 of 44 | 5 |
| False | 29 of 44 | 15 |

Weighted back to all unmatched findings, Sonnet's 87.6% true rate becomes 81.9% (95% interval 73.5 to 90.3). So the earlier reading above, where the true rate rose under stronger judges, didn't repeat on this agent. On `train` PRs the two judges still differ mostly as described above: Opus accepts factually right findings that Sonnet rejects as not worth raising. It also rejects findings Sonnet accepted without checking enough code. Sonnet 5.5 remains a usable steering judge, but its number isn't a floor.

## Why not simply use the stronger judge?

- **Cost.** Opus max costs about 14 times as much per finding. Classifying a full benchmark run this way would cost thousands of dollars.
- **It would flatter the agent.** A lenient judge raises every number, which is the wrong direction for a loop whose goal is precision. Steering by the strict judge is the safer failure.
- **The leaderboard row we compare with uses Sonnet 5.** Neither judge reproduces it. Our Sonnet 5.5 numbers and OhMyBug's row differ by an offset that cannot be measured without running Sonnet 5, which this project does not do.

## What this does not establish

There is no human ground truth here. A consensus of Opus and Astra is two models agreeing, and they both read the same rubric. The samples are small: 40 findings, of which both stronger judges answered 34. So treat the matching result as firm and the classifying result as a direction: Sonnet 5.5 is a usable, strict steering judge, and its augmented numbers may understate or overstate the agent. They are not a verdict on it.
