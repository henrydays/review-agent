# The self-improvement loop

The loop changes the review agent one idea at a time, and keeps a change only if it helps on repositories the author of the change never saw.

There are two ways to run it. In `evaluate`, a person or a Claude Code session running Opus 5.5 writes the change. It reads the diagnosis and the teachers' traces, then hands over a directory, and this may restructure the skill. In `iterate`, an automated improver writes the change on its own, limited to prompts and settings. Reviewers are Haiku 5.5 through the Claude Code CLI in both.

## What happens in one round

```
         ┌───────────────────────────── champion: skill/pr-review ─────────────────────────────┐
         │                                                                                     │
  1. score it on training PRs and validation PRs                                               │
         │                                                                                     │
  2. diagnose: trace each missed finding through the stages                                    │
         │                                                                                     │
  3. improver (DeepSeek on OpenCode) edits a copy, given the diagnosis and all past experiments│
         │                                                                                     │
  4. guard: only prompts and knobs? no memorized answers? no bloat? ──── fail ──► ledger       │
         │                                                                                     │
  5. re-score on training PRs, re-running only tasks whose prompt changed                      │
         │  no gain ───────────────────────────────────────────────────────────► ledger        │
         │                                                                                     │
  6. score on validation PRs, from different repositories                                      │
         │                                                                                     │
  7. gate: paired resampling, cost limit, time limit ── fail ──────────────────► ledger        │
         │                                                                                     │
         └── pass: becomes the new champion, prompt/REVIEW_PROMPT.md is rebuilt ───────────────┘
```

## Running it

```sh
python3 bench/preflight.py                 # is the model reachable?
python3 bench/dataset.py fetch train_quick,val_quick
python3 loop/run_loop.py baseline --select val_quick
python3 loop/run_loop.py iterate --rounds 5
python3 loop/run_loop.py status
python3 loop/run_loop.py holdout           # rarely: the honest final estimate
```

To test a change you wrote, in a copy of the skill:

```sh
python3 loop/run_loop.py evaluate --candidate loop/work/my-change/pr-review --hypothesis "what it should do, and why"
```

Run `iterate` under `/loop` or cron to keep it going. It stops by itself when an OpenCode Go meter reaches `--max-usage` (80%), and it reads the 5-hour, weekly and monthly windows from OpenCode. Those meters only cover its improver.

Models are set by `--host`, `--model`, `--effort` and `--judge-model`. The defaults are `claude-haiku-5-5` through Claude Code for reviewing, and `claude-sonnet-5-5` for judging. `bench/judge.py` refuses any judge below Sonnet 5.5, and never accepts Sonnet 5. [Why that judge](JUDGES.md). `iterate`'s improver still defaults to `opencode-go/deepseek-v4-pro`, set by `--improver-model`.

`--commit` commits each accepted change. Without it, accepted changes stay in the working tree for you to look at.

### Learning from stronger reviewers

`bench/teacher.py` runs Opus 5.5 max and GPT-6 Astra max once over a PR, with the same prompt and read-only tools, and asks how they found each finding. `loop/compare.py` then lists what they caught and the student missed, on training PRs only. Read that for ways of working and classes of problem, and teach those in the skill. The teachers' words never go into it, and `loop/guard.py` checks changes for words tied to benchmark repositories.

### How much a single run can tell you

Two runs of the same version on the 16 training PRs can differ by more than a real change does. Fresh runs of one set of discovery prompts covered between 24 and 29 of the 34 high-severity findings. So test a change to verification or to which sessions run by replaying the same cached discovery, as `bench/run.py --skill-dir` does when the discovery prompts are unchanged, and compare only what changed. Pass `--validate-anyway` to `evaluate` when a controlled test already supports a change: it skips the filter on training PRs, which only saves money, and the decision on validation PRs still applies.

### Measuring time

Measure a session by when it answers. Claude Code keeps running after it has answered: with 24 sessions at once, a one-word prompt has its answer after about 13 seconds and exits after about 57. So `ClaudeHost` returns on the `result` event, stops the process, and turns off non-essential traffic, which on its own halves the time a session spends outside the model. This machine's load stays near 5 throughout, so CPU is not the limit. API time still grows with how many sessions run at once, about 4 seconds at 8 sessions and 14 at 24, so a PR's own sessions slow each other. Running a large PR alone took as long as running two PRs at once, since most of what is in flight then is that PR's own. Run nothing heavy alongside a run you are timing, and compare `seconds` with `api_seconds` before blaming the model or the account. A session killed at a deadline never reports its cost, so a run with many killed sessions looks cheaper than it was. Tasks replayed from the cache take no time, so a change to how the budget is split looks better than it is. Test that kind of change with `evaluate --candidate-salt NAME`, which runs the candidate fresh and keeps the champion's scores cached.

## Splits

Generated by `bench/dataset.py splits`, deterministically, and disjoint by repository.

| Split | PRs | What it is for |
|---|---:|---|
| `train` | 114 | The improver may read these findings |
| `train_quick` | 16 | Stratified subset, used in each round |
| `val` | 22 | ReviewBench's official test set, so numbers line up with portal test runs. Never shown to the improver |
| `val_quick` | 9 | Stratified subset, used in each round |
| `holdout` | 74 | Never touched by the loop. Only `holdout` reads it |
| `val_siblings_unused` | 9 | Other PRs from validation repositories, excluded to keep repositories disjoint |
| `fixture_train`, `fixture_val` | 1 each | Seeded PRs written for this project, for testing the loop itself |

Note that the giant PRs (two with 53 of the 181 high-severity findings) landed in `train`, and are excluded from `train_quick` to keep rounds
affordable. Run on the full `train` occasionally.

### The maintainer corpus

[A second dataset](MAINTAINER_CORPUS.md), of review by the maintainers of high-stakes projects, has its own `maint_*` splits, disjoint from ReviewBench by organization. The defaults above do not use it. To include it:

```sh
python3 loop/run_loop.py iterate --train train_quick,maint_train_quick --val val_quick,maint_val_quick
```

Validation has to include ReviewBench's PRs, and the loop refuses to run without them. Human comments are rarely labeled false, so this corpus's grounded precision sits near 1 and its objective only measures recall, which would reward reporting more. On a mixed set, a change is also rejected if it raises comment volume on these PRs by over 25%, or loses more than 0.003 on ReviewBench's PRs. The ledger records both corpora separately, as `val_maintainer` and `val_reviewbench`.

## Why a gain is believable

**Paired resampling.** The objective is computed on the same PRs for both versions, then resampled 2,000 times over PRs. The change is accepted
only if at least 80% of resamples show a gain and the gain is at least 0.5 points. A win from one lucky PR does not pass.

**Unchanged work stays unchanged.** The task cache is keyed by prompt and model. If only one lens changed, every other lens replays its exact previous
answer, so the difference between two runs comes from the change and not from sampling noise. The judge caches by content in the same way.

**Selection bias is real and only slowed down.** Looking at validation scores over and over slowly overfits to validation. That is what `holdout` is for, and
why it refuses to run twice without `--force`. If validation climbs while holdout does not, the climb was not real.

## What keeps it honest

The golden set is public and the improver reads part of it, so the obvious failure is a prompt that memorizes answers. Layers against that:

1. **Repository-disjoint validation.** A memorized answer about one project cannot score on another.
2. **Leak guard** (`loop/guard.py`). Rejects added words that are specific to the repositories the improver saw. Whether a word is specific is judged by
   how many repositories across the full corpus use it, so `timeout` is fine and a project's function name is not.
3. **Scope limit.** The improver can edit `references/*.md` and `config.json` only. It cannot touch the scripts, the profiles or the judge, and works on a copy
   with no shell and no web.
4. **Bounds on knobs.** Time budgets, task counts and similar settings have limits that a change cannot exceed.
5. **Word limits.** A lens stays under 450 words, and prompts cannot grow by more than 25% in one step.

## The objective

`0.7 * F1(all severities) + 0.3 * F1(high severity)`, grounded and macro-averaged over PRs.

The row being chased is the unfiltered one, so all severities lead. High severity keeps some weight, since it is the site's default view, but with few findings it is too noisy to lead. Change `WEIGHTS` in `loop/gate.py` to steer elsewhere.

Volume buys grounded recall and grounded precision barely notices, so F1 alone would let a change trade quality for it. The gate also rejects a change that lowers grounded precision by more than 2 points, or estimated augmented precision by more than 3. It rejects one that costs over 1.4 times as much, in dollars when the host reports them, or that takes over 330 seconds per PR on average.

The augmented estimate comes from classifying a sample of unmatched findings each round. `sample_order` in `bench/judge.py` picks them by content, so two versions share most of their sample and the difference between them is not sampling noise.

## Reading the ledger

`loop/ledger.jsonl` holds one record per experiment, kept whatever the outcome, including the hypothesis, the changed files, scores, and the reason for the decision.
The improver reads a summary of it in each round, so rejected ideas aren't retried without a reason. Outcomes:

| Outcome | Meaning |
|---|---|
| `accepted` | Passed everything, and is now the champion |
| `rejected_by_guard` | Failed a check before any evaluation |
| `rejected_on_train` | No gain even on the PRs it was written for |
| `rejected_on_val` | Helped on training PRs but not on unseen repositories, or broke a cost or time limit |
| `improver_failed` | The improver call itself failed |
| `holdout` | A final estimate was taken |

## Lessons from building it

Two bugs in the first version of this loop are worth knowing about, because neither produced an error, and both would have produced believable, worthless numbers:

- **The improver edited the real skill.** OpenCode resolved its relative paths outside the sandbox, so the "candidate" was unchanged and the "champion" was modified. The loop would have compared
  the change with itself. `restore_if_tampered` now checks after each proposal, puts the skill back, and stops.
- **Reviewers ran in the wrong directory.** OpenCode takes its working directory from the client's `PWD`, not the process's actual directory. Started from this repository, a reviewer had the golden set within reach. `bench/preflight.py` now
  proves isolation with sentinel files before each run, and `bench/run.py` audits every saved tool call afterward.

If a score ever looks too good, read the `tool_calls` saved with each task in `runs/<name>/artifacts/<pr>/results/` before believing it.
