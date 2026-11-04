# Where this is going

Written on 2026-11-03. Update it when the direction changes, and trust the code over this page on details.

## The goal

A reviewer that runs on **Haiku 5.5 through Claude Code**, and is cheaper, faster and more precise than what is on the ReviewBench leaderboard today. Stronger models teach it: Opus 5.5 at max effort, and GPT-6 Astra at max effort through Codex. It improves in a loop driven by Opus 5.5.

The row to beat on ReviewBench's full set is OhMyBug's: **grounded precision 81.9, recall 60.1, augmented precision 92.5, recall 85.5**.

Everything is judged by **Claude Sonnet 5.5 or better**, never Sonnet 5 or DeepSeek, and `bench/judge.py` enforces that. The leaderboard judges with Sonnet 5, so our numbers carry an offset from its numbers that nobody has measured. Comparing with OhMyBug's row is a direction, not a result, until a run goes through the portal.

## Where things stand

| Piece | State |
|---|---|
| The skill | Built and tested. Being tuned for Haiku 5.5 |
| Reviewers through Claude Code and Codex | Working, isolated, and audited. `bench/preflight.py --host claude` checks it |
| Teachers | `bench/teacher.py` runs a strong model once over a PR, and `loop/compare.py` shows what it found that Haiku missed |
| First numbers | Haiku 5.5 on `train_quick` (16 PRs), judged by Sonnet 5.5: macro precision 85.5, recall 50.4. These are training PRs, one reading, and a different judge from the leaderboard's |
| The loop | Not yet moved. `loop/run_loop.py` still defaults to DeepSeek on OpenCode, its improver included, and no experiment has run, so `loop/ledger.jsonl` does not exist |
| The maintainer corpus | Built: 221 PRs and 826 true findings from 35 projects, including 23 Linux patch series. Works end to end with Haiku 5.5, on two PRs. No baseline yet |
| Commits | Nothing is committed. All of this is in the working tree |

## Two datasets, two jobs

**ReviewBench is the target.** The leaderboard scores it, so what ships is judged by it. Its weakness is that it is mostly small repositories, with most high-severity answers written by LLMs.

**[The maintainer corpus](MAINTAINER_CORPUS.md) is the check.** Maintainers of Linux, Kubernetes, React, OpenSSL and others said these things about real changes. It answers what ReviewBench cannot: whether the agent finds what experienced people find in code where bugs are expensive, or has only learned to agree with other models. It can also teach, through `maint_train`.

It should not replace ReviewBench or decide alone. Its grounded precision sits near 1 whatever the agent does, so on its own it rewards reporting more, and the loop refuses that.

**It is also mostly not about bugs,** which changes how to use it. 86% of its true findings are low severity and 64% are maintainability or documentation. Only 8 are high. So the hypothesis behind building it, that the best engineers' reviews show how to find serious bugs, holds only for a small part of it, about 120 findings. Chasing its overall recall would teach the agent to nitpick, against the goal of being more precise.

## What comes next, in order

1. **Measure before changing anything.** Run Haiku 5.5 on `maint_val_quick` with `bench/run.py --host claude`, and read recall on medium and high findings, not the headline. Until this exists, nobody knows if the corpus has anything to teach.
2. **Ask whether the teachers are right.** Run Opus and Astra on `maint_train_quick`. If they do not catch more of what maintainers caught than Haiku does, then learning from them improves agreement with them and not review. This needs `loop/compare.py` to accept `maint_train`, since it only reads ReviewBench's `train` today.
3. **Score the part that matters.** Let a run report, and the gate use, this corpus's medium and high findings alone. Without this, its low-severity majority steers the objective's all-severities term toward nitpicking.
4. **Move the loop to Claude Code,** with Haiku 5.5 reviewing and Opus 5.5 improving. The improver still goes through `OpencodeHost`.
5. **Then let the loop use both corpora:** `--train train_quick,maint_train_quick --val val_quick,maint_val_quick`. The guards against reporting more are in place. The defaults stay ReviewBench-only until steps 1 and 3 show the corpus earns its place.
6. **Read lessons, not cases.** Look at what Haiku misses in `maint_train` for classes of problem, as `docs/LOOP.md` asks. Nothing from a benchmark goes into `skill/`.
7. **Take `holdout` and `maint_holdout` only at milestones,** and submit through the portal when the numbers are worth it.

## Growing the corpus

In order of value for the effort, and only after step 1:

- **Bugs that escaped review.** The kernel's `Fixes:` tag names the commit that introduced a bug, and the fix's message explains it. This now looks like the most valuable next source: review comments gave 8 high-severity findings, and this gives real bugs, confirmed by a fix, that every reviewer missed. It is also what the objective weights most.
- **Go, through Gerrit.** Per-line comments and explicit patch sets, cleaner than GitHub or mail.
- **More mailing lists.** `bench/lore.py` works on any list on lore.kernel.org, including QEMU's.

## Open questions

- **Is a Sonnet 5.5 label good enough?** [The checks so far](MAINTAINER_CORPUS.md#how-far-to-trust-the-labels) look good, but the labeler does not read the repository, and no person has audited it yet.
- **Should this corpus steer the loop at all, or only check it?** Using it as a check is safe today. Letting it steer needs step 3 first.
- **`bench/splits.json` was edited by hand** to move three PRs whose golden findings had been read from `val` to `train`. `make_splits()` does not know, so `bench/dataset.py splits` would silently undo that. Either teach `make_splits` about `read_during_analysis`, or do not run that command.
