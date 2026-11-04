# Full benchmark results

Each full run over ReviewBench's 219 PRs goes here, newest first. [How to run one](LOOP.md#running-a-full-benchmark).

Every score on this page is judged by Claude Sonnet 5.5. The leaderboard judges with Sonnet 5, so these are not leaderboard scores, and comparing them with a leaderboard row carries an offset nobody has measured. Full runs include `val` and `holdout`, so this page reports aggregates only.

## `full-c9-1`, 2026-11-04

**It does not match OhMyBug yet.** Grounded precision is level, and grounded recall is 3 points short. Augmented precision is 6 points short, which is the clearest gap. Time is level with OhMyBug's slowest run.

The candidate is `c9-few` (fingerprint `4221b3dcb7`), reviewed by Haiku 5.5 through the Claude Code CLI. **This is not what `skill/` ships.** The gate rejected c9 for taking 347 seconds per PR against a 330 limit, so `skill/pr-review` is still the older champion (`c8e50defc8`), which has never had a full run. [The patch from one to the other](results/c9-few.patch), [the aggregates as JSON](results/full-c9-1.json).

### Against OhMyBug

| | c9, Haiku 5.5 | OhMyBug run 1 | Run 2 | Run 3 |
|---|---:|---:|---:|---:|
| Grounded precision | 82.8 | 81.9 | 81.9 | 81.2 |
| Grounded recall | 57.1 | 60.1 | 60.2 | 60.2 |
| Grounded F1 | 67.6 | 69.3 | 69.4 | 69.1 |
| Augmented precision | 86.1 | 92.5 | 92.7 | 92.0 |
| Augmented recall | 82.6 | 85.5 | 85.8 | 84.8 |
| Augmented F1 | 84.3 | 88.8 | 89.1 | 88.3 |
| Novel yield | 62.1 | 71.7 | 72.5 | 70.7 |
| Comments (candidates) | 7,269 | 8,057 | 8,341 | 7,973 |
| Novel true positives | about 4,980 | 6,093 | 6,382 | 6,034 |
| Mean time per PR | 340s | 339s | 313s | 315s |
| PRs finished | 219 | 219 | 219 | 219 |

OhMyBug's figures are from the ReviewBench leaderboard (site data is CC BY-NC 4.0). Grounded scores for c9 are macro-averaged over PRs, as the leaderboard's look to be, though that isn't confirmed. Pooled instead, they are P 84.7, R 56.2, F1 67.6.

Reasons to be careful with this table:

- **The judges differ**, as above.
- **Augmented scores here are an estimate**, from classifying 822 sampled findings out of 5,683 unmatched ones. 87.6% of them were true (95% interval 85.2 to 89.7). OhMyBug's are from full classification.
- **This is one run, not three.** Resampling PRs gives 95% intervals of P 79.9 to 85.6, R 54.4 to 59.8, and F1 65.3 to 69.9. So the lead on grounded precision is noise, the F1 gap is within the interval, and the recall gap is just outside it. The interval does not cover the difference between judges.
- **The reviewers differ.** OhMyBug's vendor describes two independent Opus 5.5 reviewers. c9 runs Haiku 5.5, at an estimated $0.38 per PR. OhMyBug's cost isn't published, so the table has no cost row.

### Where the scores come from

By severity:

| Severity | Macro P | Macro R | Macro F1 | Golden findings covered |
|---|---:|---:|---:|---:|
| High | 90.8 | 66.4 | 76.7 | 105 of 181 |
| Medium | 92.8 | 70.1 | 79.9 | 621 of 891 |
| Low | 76.5 | 48.8 | 59.6 | 748 of 1,551 |

The leaderboard's high-severity row for OhMyBug is R 75.6, P 92.0, so high-severity recall is about 9 points short. With so few high-severity findings it swings easily: redoing 6 PRs moved it from 68.0 to 66.4.

By split:

| Split | PRs | P | R | F1 | Comments per PR | Mean time | Cost per PR |
|---|---:|---:|---:|---:|---:|---:|---:|
| `train` | 114 | 81.2 | 55.6 | 66.0 | 31.8 | 338s | $0.39 |
| `val` | 22 | 88.9 | 60.4 | 71.9 | 39.5 | 345s | $0.41 |
| `holdout` | 74 | 83.6 | 56.9 | 67.7 | 35.9 | 355s | $0.39 |
| `val_siblings_unused` | 9 | 83.1 | 71.2 | 76.7 | 13.6 | 255s | $0.14 |

`holdout` scores above `train`, so there is no sign that c9 overfits the PRs it was designed from.

By lines changed:

| Lines | PRs | P | R | F1 | Comments per PR | Mean time | Cost per PR | Verify tasks lost |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Under 100 | 38 | 77.8 | 65.3 | 71.0 | 10.8 | 233s | $0.11 | 0 of 143 |
| 100 to 300 | 41 | 84.0 | 55.8 | 67.1 | 15.2 | 271s | $0.15 | 2 of 223 |
| 300 to 800 | 47 | 79.5 | 53.8 | 64.2 | 21.1 | 328s | $0.22 | 13 of 368 |
| 800 to 2,000 | 42 | 83.4 | 56.7 | 67.5 | 34.2 | 383s | $0.41 | 82 of 484 |
| 2,000 and up | 51 | 88.2 | 55.4 | 68.1 | 74.6 | 450s | $0.90 | 932 of 1,520 |

### Where the gap is

- **Volume.** c9 writes 33.2 comments per PR, and OhMyBug about 37. Across the leaderboard, recall follows `-2.9 + 17.3 * ln(comments per PR)`, which predicts 57.7 at c9's volume. c9 scores 57.1, so it finds about what its volume predicts and has no lead there.
- **False alarms.** The estimated share of unmatched findings that are false is 12.4%. That is what holds augmented precision at 86.1, and it is why novel yield falls short too. Before the full sample, a reading from 31 PRs had this at 6%.
- **Low-severity findings** are 1,551 of the 2,623 golden findings, with recall of only 48.8, and precision of 76.5.
- **Verification on large PRs.** 61% of verify tasks on PRs of 2,000 lines or more never finish, and unverified candidates are kept. These PRs still have the highest grounded precision, so the verifier's value there needs a controlled test before more time goes to it. Lost sessions don't clearly explain missed findings either. On 43 `train` PRs, PRs of 300 to 2,000 lines had the same recall with and without lost discovery sessions (50.2 and 50.0). Above 2,000 lines the difference was larger (60.0 and 52.3), but on only 14 PRs. What remains is probably findings no lens ever reported, which hasn't been traced across `train` yet.

### Time and cost

Reviews ran from 07:40 to 18:11, and judging finished at 18:35. Reviewing 6 PRs again and judging those took until 18:58. Mean time is over the 202 PRs with clean timing, leaving out those listed under [what disturbed the run](#what-disturbed-the-run) that weren't reviewed again. All 219 together average 337 seconds.

Cost is what Claude Code reports at API rates, since this account is on a subscription. It's a floor, because a session killed at a deadline never reports its cost. The total is $83.27, or $0.380 per PR, with these shares:

| Stage | Tasks | Not finished | Share of cost | Cost per task | Mean task time |
|---|---:|---:|---:|---:|---:|
| Discover | 1,668 | 166 (10%) | 87% | $0.044 | 185s |
| Dedupe | 1,603 | 116 (7%) | 1% | $0.0007 | 18s |
| Verify | 2,738 | 1,029 (38%) | 12% | $0.0037 | 80s |

"Not finished" counts tasks that failed, were skipped, or were cut off. Judging is separate, and came to at least $84 at API rates, nearly all of it classifying unmatched findings. Judging partway through the run isn't in that figure. The run moved the account's weekly meter from 47% to 58%, including this session's own use.

### Making it cheaper and faster

The goal before this run was to cut cost and time by choosing lenses and sessions better, while keeping quality. c9 made these changes:

| Change | Evidence |
|---|---|
| Turned off the `quick`, `config` and `ui` lenses | Replaying discovery showed they lose at most 2 golden findings. Further cuts trade quality for cost about 1 to 1 |
| Ran lenses by priority, under a limit of 16 discovery tasks | `general` first, with 2 passes, then `intent`, `drift`, `failure` |
| Set discovery effort to `high`, and left dedupe and verify unset | The effort study below |
| Sessions return when they answer (the champion has this too) | Claude Code runs on for up to minutes after its `result`, which used to count against the PR |
| A new verifier rule | A controlled test on identical discovery favored it |

**Effort.** One session per PR on 8 `train` PRs, judged by Sonnet 5.5:

| Effort | P | R | F1 | High-severity F1 | Cost per PR | Output tokens |
|---|---:|---:|---:|---:|---:|---:|
| `medium` | 86.5 | 41.5 | 56.1 | 64.3 | $0.038 | 21k |
| `high` | 91.7 | 54.6 | 68.4 | 90.2 | $0.083 | 44k |
| `max` | 89.4 | 56.4 | 69.2 | 85.1 | $0.504 | 161k |

`high` is the best value. `max` costs 6 times as much for under a point of F1. `xhigh` wasn't tried, and a lower effort for verification still needs a controlled test.

**PRs at once.** Three at once was tried for part of the run, and made every size slower. Time for PRs under 100 lines went from 210 to 297 seconds, and the share of PRs under 2,000 lines losing sessions went from 3 of 33 to 5 of 13. [Measuring time](LOOP.md#measuring-time) has what is known about why.

**The outcome is unconfirmed.** On 25 PRs, c9 was 13% faster and 10% cheaper than the champion, with a better F1 (319s against 368s, $0.35 against $0.39, F1 74.2 against 71.2). The full run came in above that estimate, at 340s and at least $0.380, but the champion has no full run, so the difference at full scale isn't known. The speed target isn't met: 340s is slower than all three of OhMyBug's runs, though only 1 second behind the slowest.

### Readings that did not hold

Partway through this run, some early readings were reported that the full run contradicted:

- **"Tied on F1."** At 148 PRs, F1 was 69.7. PRs run in alphabetical order, so repositories cluster, and the last 71 PRs scored 62.8. Don't call a partial run.
- **"The gap is coverage, not false alarms."** That came from 31 PRs where 94% of unmatched findings were true. The full sample says 87.6%.
- **"Lost sessions on large PRs are what costs recall."** 43 `train` PRs gave no clear support for this.
- **The projection of 321 seconds and $0.35 per PR,** which came from the 25 PRs. Those PRs flatter c9 in general: F1 was 74.2 on them, and 67.6 on all 219.

### What disturbed the run

- **Timing probes.** A burst of 20 sessions, run next to the live benchmark to investigate slow starts, pushed 2 PRs into their deadlines at a cost of 21 tasks. Nothing heavy should run next to a timed run.
- **Three at once.** 18 PRs ran under that test. 4 of them were under 2,000 lines and still lost tasks.
- **Restarts.** 5 PRs were interrupted and resumed, so they replayed finished tasks from the cache, and their times are too low.

The 6 PRs most affected, the first 2 and those 4, were reviewed again with a new salt, and those reviews replaced the originals. This moved the totals by about 0.1 point. The other 14 PRs from the three-at-once test, and the 5 replayed PRs, keep their scores but are left out of timing.

### What comes next

Design only from `train` PRs:

1. Sample false alarms on `train`, and find what they have in common. This is the largest gap, and costs recall nothing if it's done well.
2. Trace missed findings on all 114 `train` PRs with `loop/diagnose.py`, focusing on low severity and on PRs of 300 to 800 lines, where recall is lowest.
3. Test with controlled replays whether verification earns its time on large PRs, and whether verifying at `low` effort loses anything.
4. Run the champion in full too, so that there is a real baseline for cost and time.
5. Run again in full when a candidate clearly beats c9 on `train` and `val`. Each full run also looks at `holdout`, so these should stay rare, and `holdout` shouldn't choose between candidates.

### Reproducing it

Rebuild the candidate from `skill/pr-review` at commit `87245d5`:

```sh
mkdir -p loop/work/c9-few && cp -R skill/pr-review loop/work/c9-few/
(cd loop/work/c9-few && patch -p1 -E < ../../../docs/results/c9-few.patch)
```

Then run it:

```sh
python3 bench/run.py --select all --name full-c9-1 --skill-dir loop/work/c9-few/pr-review --host claude --model claude-haiku-5-5 --prs-at-once 2 --concurrency 24 --salt full1 --classify-sample 4 --max-usage 85
```

Model samples will differ, so expect scores within the intervals above, not the same scores.
