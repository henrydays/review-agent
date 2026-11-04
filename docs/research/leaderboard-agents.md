# Leaderboard agents: what they actually do

Collected 2026-11-03 from public images, source repositories, vendor documentation and the
live site. Each claim carries its evidence type:

- **OBSERVED**: read in the image or repository named in that block.
- **VENDOR**: the vendor says so; could not be checked.
- **INFERRED**: my reading or calculation.

No code or prompts were copied from these projects, and nothing from the images was executed.

## The view you are looking at

The table on review-bench.ai defaults to the **High-severity** filter. Its F1, precision and
recall come from `/api/leaderboard/metrics?severities=High`. Comments per PR is all-severity. The
API's own ranking uses all severities, and that order is very different:

| Agent | High F1 | All-severity F1 | All-severity P / R |
|---|---:|---:|---|
| OhMyBug | 83.0 | 69.3 | 81.7 / 60.2 |
| intent-review-agent | 82.2 | 62.6 | 81.8 / 50.7 |
| Laude RevEngine | 73.3 | 59.6 | 83.6 / 46.3 |
| diffox | 57.6 | 58.7 | |
| block/review | 68.2 | 47.7 | 88.7 / 32.6 |
| Klaussy | 63.1 | 44.1 | 84.1 / 29.8 |
| Copilot Max | 75.7 | 42.4 | 87.8 / 28.0 |
| Qodo | 68.3 | | 85.3 / 22.1 |
| Devin AI | 66.1 | | 84.0 / 23.8 |
| Greptile | 33.4 | | 86.1 / 16.2 |
| Cubic | 35.8 | | 85.5 / 16.3 |
| DeepSeek Harness Reviewer | 64.8 | 39.9 | 89.5 / |
| Cursor Bugbot | 60.9 | | 87.7 / 9.6 |
| Codex Sol Max | 70.2 | 30.4 | 86.0 / 18.5 |

Across 39 rows, grounded precision spans 77.8-90.2 while recall spans 5.7-60.2 (INFERRED from
the API). Recall decides the ranking. Spearman of findings volume against overall grounded F1 is
+0.965 (recall +0.967, precision -0.36), but only +0.43 in the High view: on High, volume alone is
not enough and the model has to actually find the bug (INFERRED).

## How each agent works

### OhMyBug (rank 1): hosted
OBSERVED: the image is a thin client for `mcp.ohmybug.ai`. It sends the diff plus up to 30 changed
files and answers `needs_files` requests (20 per request). It never sends the title or body.
VENDOR (ohmybug.ai/technical.html): two independent Opus 5.5 reviewers, one running 19 focused
lenses (logic, state, contracts, error paths, calibration), one exploring freely. Duplicates are
combined, then a second pass tries to disprove each candidate through reachability and guards.
Message shape: `[severity] title`, failure scenario, `Suggested fix:`. 37.1 comments/PR, 5m22s.
Prompts and thresholds are not accessible. No license declared.

### intent-review-agent (rank 2): private
Private image. Manifest egress suggests hosted logic (INFERRED). 17.6 comments/PR. Nothing else
public.

### Laude RevEngine (rank 4): Apache-2.0, logic in image
OBSERVED: rules from the base branch plus up to 30 design docs, then one reviewer, then a "second
look" in the same conversation that names files not yet opened, then deterministic dedupe, verifier
and rank. No specialists: 13 lenses in one prompt. Two discovery passes, one verify. Opus 5.5 via
OpenRouter. Tools: read_diff, read_file, read_base_file, list_files, literal search; no static
analysis. 40 turns.
- Verifier: confirmed / rejected / uncertain. Lenient mode drops only rejected. A verifier failure
  keeps the findings.
- Dedupe: same file, within 3 lines, word Jaccard >= 0.45, plus the verifier's `duplicate_of`.
- Cap 25, by severity then confidence.
- The prompt demands exhaustiveness, expects ten or more findings on a mid-size change, and treats
  the same defect in two files as two findings.
- Their commit ad13adf7 reports a cautious "can't check" rule cost several recall points. Commit
  0df5787d reports that second independent copies added findings without lowering precision.
- Later experimental swarm, no public score: **DeepSeek V4.1 Flash, reasoning off, 10 chunks x 4
  tasks + 5 global passes, preloaded `git grep` mentions, at most 6 turns, 3 voting verifiers, 60s
  target.**

### block/review (rank 8): Apache-2.0, logic in image
OBSERVED: 6 parallel panelists (3 lenses x GPT-6.1 Sol, Opus 5.5), then a coordinator, then a judge.
Lenses: behavior/contracts; reliability/operations; security/trust boundaries (deliberately denied
the PR description). Candidates need confidence >= 0.5, root cause, trigger, impact and evidence. The
coordinator drops only with cited counter-evidence, and the judge can only restore drops. Tools:
shell, git, ripgrep; the prompt forbids builds, linters and tests. Body at most 350 chars. 5.9
comments/PR.

### DeepSeek Harness Reviewer (rank 12): MIT
OBSERVED: one session on deepseek-flash, effort high, 65,536 max tokens, then a validator and up to
2 format repairs. No verifier, no specialists requested. Prompt adapted from Codex's rubric: read the
diff, follow callers, callees, contracts, config, docs, tests. More than 30 findings is rejected.
4.3 comments/PR with the best overall precision of this group (89.5). **Shows what a single DeepSeek
pass with a conservative bar gets: high precision, low recall.**

### opswiki (rank 13), Sourceant: private
Labels only. opswiki: `hybrid-review-v2` on deepseek-v4-flash, 3.6 comments/PR. Sourceant:
deepseek-v4-pro, `discovery-passes=3, evaluation-passes=2, include-nitpicks=true`.

### Klaussy (rank 14): FSL-1.1-MIT (source-available)
OBSERVED: conventions scan (179 detector files) plus LLM enrichment when CLAUDE.md is absent, then
deterministic diff trim and triage at 150 lines. Small PRs: reviewer + hunk-sweep subagent. Large:
correctness x2, architecture, security, scope, conditional agents, then dedupe, validators, synthesis.
Validators get claims stripped of reasoning, at most 6 per batch, and argue the author's side; they
drop only on cited evidence. Sonnet 5.5. On one day: with nits F1 44.1 (P 84.1, R 29.8) versus 35.9
(P 86.5, R 22.6) without (images may differ).

### TraceHound, OCR, alma-reviewer, pr-review-agent, mesrai (not in the table)
- TraceHound (MIT): diff-only and context generators over 60k-char chunks, with deterministic
  `git grep` context and no tools. Pool dedupes same path within 3 lines. Own 25-PR grounded F1:
  verified 13.9, raw 16.0, raw x2 21.3.
- OCR (Apache-2.0, alibaba/open-code-review): deterministic file selection, file grouping, plan,
  per-group tool agent with 2 default rounds, a filter removing only provably wrong comments, then
  re-location.
- alma-reviewer: one diff-only call. Own baseline F1 27.8; with a strict gate, 0 true positives.
- pr-review-agent (MIT): no-tool Haiku reviewer with prefetched context, then a Sonnet judge.
- mesrai: image not pullable despite being marked public; hosted (INFERRED).

### Commercial products (from published material)
- **Copilot code review** (the `ccr` golden producer, INFERRED). Tool-calling agent, explicit plan,
  flags while reading. Lite merges several agents, Balanced uses a higher-reasoning model, Max runs
  independent multi-provider agents then an aggregation agent. A/B on Lite: recall +13.6%, addressed
  +8.0%, comments +61%, cost -8%; addressed high-severity +47%.
- **Codex**. One sub-agent thread with a rubric (`codex-rs/prompts/templates/review/rubric.md`):
  meaningful, discrete, introduced by the commit, author would fix, no unstated assumptions, provable,
  "prefer outputting no findings". Recall deliberately traded for signal. Sol Low to Max: recall
  9.7 to 18.5.
- **Qodo 2.0**. Focused agents (bugs, standards, risk, context), a PR-history agent, a deduping judge.
- **Devin Review**. Bugs, Flags, Security categories; custom reviewers; Normal/Ultra effort.
  Stages unpublished.
- **Cursor Bugbot**. V1: 8 passes with randomized diff order, buckets, majority vote, validator,
  dedupe. Later moved to an agentic loop with dynamic context, which gave the "largest gains". The
  agent was too cautious, so prompts were made more aggressive. Of 40 experiments many regressed.
  Votes favour precision, which shows: precision 87.7, recall 9.6.
- **Cubic**. Planner plus security, duplication, editorial micro-agents; reasoning before finding;
  tools cut to those used over 10% of the time. Longer prompts, temperature and sampling failed.
- **Greptile**. Parsed call/import graph, agentic loop since v3. LLM severity judgments were "nearly
  random"; prompt-only noise control and few-shot failed. Embedding filter on downvotes worked.
- **CodeRabbit** (not ranked). Sandbox, graph, 40+ linters, LLM-written scripts using grep and ast-grep.
- **Claude Code `/code-review` plugin**. Haiku gate, four parallel reviewers (two rule auditors, two
  bug hunters), one validator per issue, unvalidated dropped.

## Techniques ranked by evidence

### Recall
1. **Report more.** Candidates/PR versus grounded recall r = 0.90; versus augmented precision r = 0.05
   (INFERRED across rows). Klaussy with nits: recall 22.6 to 29.8, precision 86.5 to 84.1.
2. **Independent runs plus union.** Copilot Lite A/B recall +13.6%. SWR-Bench (arXiv 2509.01494):
   recall +118.83%, F1 +43.67% at n=10, diminishing past 5. c-CRAB (arXiv 2603.23448): union 41.5%
   versus best single 32.1%. RevEngine second copies: more recall, same precision. TraceHound x2: +33%.
3. **Reasoning effort.** Codex Sol Low to Max, 9.7 to 18.5 (run dates differ).
4. **On-demand tools.** Bugbot's largest gain. AACR-Bench (arXiv 2601.19494): only agents improve
   with wider scope.
5. **Execution** (build and test). Some vendor claims with no method. Not feasible to rely on in the
   benchmark container.

### Precision
1. Outcome-trained gates (Greptile, RovoDev, Uber). These need feedback data that does not exist at
   benchmark runtime.
2. Two-stage filters. BitsAI (arXiv 2501.15134): reasoning-first validator recall 81.8% versus
   conclusion-first 69.0%.
3. Falsification with restricted information. OpenCodeReview (arXiv 2608.09290): 33.9% versus 7.23%,
   authors' own system.
4. Explicit reasoning logs and tool pruning (Cubic: FP -51%).

### Backfired
- Static context dumps: SWE-PRBench (arXiv 2603.26130) 8/8 models degrade. AACR BM25 -31% F1 for
  Claude, though others gained.
- Raw static analysis as findings: Hybrid-Review F1 4.87; Semgrep 5.5% TP in the golden set.
- Debate agents: CR-Agent F1 9.22 versus 18.73.
- Strict gates: alma's gate left 0 true positives; TraceHound's verifier lowered F1.
- LLM severity judgments and few-shot noise control (Greptile).
- Cautious rules: RevEngine's "can't check" rule.

## Caveats
- Rows span June to October and were run with different models and dates.
- GitHub ran the initial vendor rows.
- The Klaussy comparison assumes identical images, unverified.
- "Bigger Isn't Always Better" (arXiv 2606.15689) says ensembles hurt, but its "union" lowers recall,
  which a true union cannot do. Not relied on.
- Production vendors deliberately comment less than this benchmark rewards. See the profile design in
  [ARCHITECTURE.md](../ARCHITECTURE.md).

## Numbers that shape the design

From the site's API (`/api/leaderboard`, `/api/leaderboard/metrics`, `/api/leaderboard/<id>/rounds`), captured
2026-11-03. Site data is CC BY-NC 4.0, so only derived figures are recorded here.

| Config | High R/P | All R/P | Aug P | C/PR | High C/PR | Duration |
|---|---|---|---:|---:|---:|---:|
| OhMyBug | 75.6/92.0 | 60.2/81.7 | 92.4 | 37.1 | 2.83 | 322s |
| intent-review-agent | 74.4/92.0 | 50.7/81.8 | 87.7 | 17.6 | 2.09 | 213s |
| Copilot Max | 62.2/96.8 | 28.0/87.8 | 88.3 | 7.1 | 0.89 | 973s |
| Laude RevEngine | 60.6/92.8 | 46.3/83.6 | 90.8 | 11.4 | 0.72 | 182s |
| Median of the other 16 | 51.1/94.8 | 22.5/86.7 | 87.6 | 4.9 | 0.49 | 275s |
| diffox | 41.8/93.1 | 47.1/77.8 | 73.0 | 20.6 | 0.48 | 53s |

- **Volume predicts overall recall.** recall = -2.9 + 17.3 * ln(comments/PR), r = 0.95 across 18 configs
  (INFERRED). 20 comments/PR predicts about 49%, 37 predicts about 60%.
- **High recall tracks high-rated findings** (r = 0.94 on log), not total comments (r = 0.57). Volume past
  about 18 comments/PR buys low-severity coverage, not high. Duration barely correlates (Spearman +0.14).
- **The top two find more of what matters and it survives judging.** Novel High TPs per round: 493 and 350,
  against a median of 41, at augmented precision 92.4 and 87.7. diffox is the contrast: high volume, 73.0
  augmented precision, mostly low severity, High recall 41.8.
- **The High target is small.** 181 TPs in 63 PRs. 31 PRs have exactly one; two PRs hold 53. Recall looks
  **macro-averaged** (INFERRED: per-round values are not integer counts), so one PR is about 1.6 points and
  giant PRs do not dominate.
- **Hard limit** (ReviewBench #84): finals get 4 shards x 350 minutes, so the mean must stay under about
  6.4 minutes per PR, whatever `pr_timeout_seconds` says.
- DeepSeek Harness Reviewer and opswiki both reach about 49 High recall at about 4 comments/PR, on the curve.
  DeepSeek Flash is not the obvious constraint (INFERRED).

## More mechanisms worth borrowing

- **opswiki** (`hybrid-review-v2`, deepseek-v4-flash). "Hybrid" means frozen change-analysis evidence plus
  grep/read tools, not hybrid retrieval. Two specialists, **a gap pass on uncovered hunks**, a critic that cannot
  add findings, then a gate requiring a head read covering the cited lines. Cap 20.
- **Sourceant** (MIT). 3 readings with different focuses (whole change; callers and dependencies; requirement
  by requirement), 2 independent challenges per candidate. Nitpick mode adds style and docs classes.
- **pr-review-agent** (MIT). Keep-or-drop judge, bundling, **breadth pass on files without findings**.
- **HeyDeer**. A different vendor's model verifies each candidate against an evidence chain. 99% is High-only;
  overall P 88.6, R 14.5.
- **diffox**. 17 narrow hunters routed by change, then anchor, corroboration, verbatim-quote and ranking gates.
- **Klaussy**. Cutting low-severity findings cost 13 high/medium recall points for 8 precision points.
- **RevEngine**. Miss taxonomy, `JUDGE_MODE=match` for fast grounded scores, tuned only on the 25 test PRs.
- **intent-review-agent**. Moving from 213s to 337s came with +9.3 overall recall (other changes may be included).

Both top agents are hosted black boxes. All golden findings are public; nothing suggests they fitted to them,
and nothing rules it out. High recall also partly measures agreement with three LLM producers, 96% of the
high-severity golden TPs, which `--exclude-producer ccr:` can test.
