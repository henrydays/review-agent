# Architecture

Why this agent is built the way it is. Evidence for each decision is in
[research/leaderboard-agents.md](research/leaderboard-agents.md).

## What the score rewards

ReviewBench's table shows **grounded F1 at High severity** by default. Four facts about that metric drive
everything below. All are from the benchmark's source (`scripts/eval/scorer.ts`, `scripts/eval/matcher.ts`,
`docs/METHODOLOGY.md`).

1. **Unmatched findings cost nothing in grounded metrics.** Precision only counts findings that match a golden
   finding. Only matching one labeled false lowers it. This is why the top agents post 18 to 37 comments per PR.
2. **Recall decides the ranking.** Across the leaderboard, precision spans 78 to 90 while recall spans 6 to 60.
   Overall recall follows `-2.9 + 17.3 * ln(comments per PR)` with r = 0.95.
3. **Matching is per file, and semantic.** "Two findings match if fixing one would fix (or substantially address) the
   other." A finding in the wrong file can never match, while exact line numbers barely matter.
4. **Each PR counts the same** (the published numbers look macro-averaged). Of 181 high-severity golden TPs, 31 PRs have
   exactly one, so finding the single serious bug in a small PR is worth as much as finding many in a huge one.

What the golden set adds: 97% of TPs sit inside diff hunks, 78% of high-severity TPs need code beyond the diff, and golden
false positives are mostly factually wrong claims about the code. There are effectively no TPs in lockfiles, build output,
vendored or generated files.

Finals allow about 6.4 minutes per PR on average, whatever the per-PR timeout says (ReviewBench #84).

## What works, per the evidence

| Technique | Evidence | Decision |
|---|---|---|
| Report more | r = 0.90 between comments and recall | Discovery prompts ask for everything with concrete grounds |
| Independent runs, union | SWR-Bench +118% recall at n=10, diminishing past 5. Copilot Lite A/B +13.6% | Several passes, reordered files in later passes |
| On-demand code tools | Bugbot's "largest gains" | Every pass can read and search the repository |
| Keep-biased verifier | RevEngine drops only rejected. TraceHound's verifier lowered F1. alma's strict gate reached 0 TPs | Refute only with quoted counter-evidence |
| Reasoning before verdict | BitsAI: 81.8% vs 69.0% recall | Verifier output puts reasoning first |
| Coverage gap pass | opswiki, pr-review-agent | Script measures uncovered hunks, a model looks at them |
| Same defect, one comment per file | RevEngine. Follows from fact 3 | `also_in`, expanded when the message names the file |
| Raw static analysis | Semgrep 5.5% TP. Hybrid-Review F1 4.87 | Never reported directly |
| Static context dumps | SWE-PRBench: 8 of 8 models degrade | Leads stay short and capped |
| Debate agents | CR-Agent F1 9.22 vs 18.73 | Not used |
| Specialists alone | One-seat RevEngine beats block's six and Klaussy's five | Lenses are a way to direct attention, not the point |
| Longer prompts | Cubic, Greptile | Word limits enforced by the loop's guard |

## The pipeline

```
                           prepare (script)
     parse diff · skip generated files · partition by directory · collect leads · old file versions
                                  │
                                  ▼
                        discover (model, parallel)
     one task per lens per partition, read-only tools, no web, reports everything with grounds
                                  │
                                  ▼
                             gap (model)
     script finds hunks nobody commented on, and a model looks at only those
                                  │
                                  ▼
                            dedupe (model)
     word overlap merges the obvious ones, then one call per file: "would fixing one fix the other?"
                                  │
                                  ▼
                            verify (model)
     small batches try to disprove each claim. A refutation needs a quote, and a script checks that
     the quote exists in the code. A verifier that invents a guard cannot delete a real finding
                                  │
                                  ▼
                           finalize (script)
     applies the profile · one comment per affected file · writes findings.json and review.md
```

### Scripts do what scripts can do exactly

`skill/pr-review/scripts/` is standard-library Python, so it runs anywhere:

- **`rpdiff.py`**: parses diffs, classifies files, packs partitions with head line numbers on every line. Big PRs get bigger
  partitions and never lose files.
- **`signals.py`**: short leads a machine finds easily and a model tends to miss: a removed name still used, a new name
  never used, a changed file that no longer parses (including shell inside CI `run:` blocks), a link to nothing, an environment
  variable nobody sets. These are hints. A model must confirm one before reporting it.
- **`review.py`**: the state machine. Output parsing tolerates prose, fences and truncation, so a cut-off answer still yields
  every finished finding. It normalizes paths, clamps line numbers, measures coverage, and enforces deadlines.
- **`hosts.py`**: the only part specific to an agent CLI. It runs one prompt on OpenCode, Claude Code or Codex.

### Models do what needs judgment

Everything a model does is a text file in `references/`, which is what the loop edits:

- **`discovery.md`** frames every pass. It says plainly that a wrong finding costs little and a missing one is lost.
- **`lenses/*.md`** are the focus of each pass, routed by kind of file. Their subject matter comes from what the golden
  set accepts on the training split.
- **`dedupe.md`** and **`verify.md`** are the two filters. Both default to keeping.

### Why the design is portable

The state machine is driven by files and one command, `next`. Each task is a prompt file and a result path. So:

- A host with subagents dispatches each task to a subagent.
- A host without them does the tasks in order.
- A script runs them through any CLI, in parallel, with a deadline. That is what the benchmark uses.
- `prompt/REVIEW_PROMPT.md` is generated from the same lens files, for agents with no skill support at all.

An improvement the loop makes reaches all four, because there is one source of prompts.

### Profiles

Grounded F1 rewards volume, which a real team would hate. So volume is a setting. All profiles run the same review, and differ only in
what gets reported:

| Profile | Reports | Use |
|---|---|---|
| `exhaustive` | Everything not disproved, up to 90 | The benchmark. A deep review on request |
| `balanced` | Confidence 0.5 and up, at most 25 | Everyday default |
| `strict` | Confirmed, medium and up, at most 10 | Blockers only |

## Treating the PR as hostile

A PR can contain text aimed at its reviewer, and when reviewing other people's code that is a realistic attack.

- Reviewers get **read and search tools only**. No shell, no web, no edits. There is nothing for an injected instruction to do except lie.
- **Repository rules come from the base commit**, so a PR cannot rewrite the rules it is judged by.
- The checkout's own **OpenCode config, agents and skills are switched off**, so a PR cannot configure its reviewer.
- Every session's **working directory is pinned to the checkout**. OpenCode takes it from the client's `PWD` variable, which a subprocess
  inherits from its parent, so getting this wrong gives no error, just a reviewer reading the wrong files. `bench/preflight.py` proves
  isolation with sentinel files before any run, and `bench/run.py` audits the saved tool calls after it, and refuses to score a run that touched
  a benchmark path.
- `tests/seeded_eval.py` plants a config file and an instruction in the diff, and reports either one being obeyed.

What this does not cover: the quote check stops a verifier from inventing counter-evidence, but a misleading comment in the code really exists, so a
verifier could still be persuaded by it. An injected instruction can only cost a finding, since reviewers have nothing else they can do.

## Fair play

The golden set is public, so the integrity of a score depends on the process:

- The improver never sees validation or holdout PRs. Splits are disjoint by repository, including siblings of validation PRs.
- A guard rejects any prompt edit that adds words tied to specific benchmark repositories.
- The agent runs with no web access and no remote, so it cannot read upstream review threads.
- Nothing from the corpus ships in the skill. The golden set lives in a gitignored pinned clone.

## Known limits

- **Our judge is a proxy.** The leaderboard judges with Claude Sonnet 5, and this project judges with Sonnet 5.5 or better, so offline numbers carry an offset nobody has measured. Use them to compare versions of this agent. See [JUDGES.md](JUDGES.md).
- **The test sample is small.** High severity has few findings per split, so short-run scores move by several points from chance alone.
- **Grounded scores partly measure agreement with the producers.** 96% of high-severity golden TPs came from three LLMs. Use
  `bench/judge.py --exclude-producer ccr:` to see how much depends on one.
- **`exhaustive` is noisy for humans by design.** Use it for depth, not as a default.
