# The maintainer corpus

A second dataset beside ReviewBench: what the maintainers of high-stakes open source projects said in code review, as golden findings in ReviewBench's format.

It adds to ReviewBench and changes nothing about it. ReviewBench's PRs and splits stay as they are, the leaderboard never sees this data, and the loop uses it only if you ask.

## Why it exists

ReviewBench leaves two gaps, and both shape what an agent tuned on it learns.

**The kind of code.** Of its 181 repositories still reachable, the median has 5 stars and 9 have over 10,000. A race in a Kubernetes controller or an over-read in a TLS parser is a different problem from most of what it contains.

**The source of truth.** 96% of its high-severity true positives were produced by three LLMs, so a good score partly measures agreement with those models. Here, every finding starts as something a person who maintains the code said about it.

[The research](research/maintainer-projects.md) has the measurements behind the choice of projects.

## What's in it

221 PRs from 35 projects, labeled by Claude Sonnet 5.5 through Claude Code on 2026-11-03.

| Split | PRs | Projects | Findings | True | False | High | Medium | Low |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `maint_train` | 140 | 21 | 522 | 469 | 53 | 4 | 75 | 390 |
| `maint_val` | 40 | 7 | 186 | 170 | 16 | 1 | 21 | 148 |
| `maint_holdout` | 41 | 7 | 199 | 187 | 12 | 3 | 15 | 169 |
| `maint_train_quick` | 16 | 12 | 57 | 54 | 3 | 3 | 34 | 17 |
| `maint_val_quick` | 12 | 7 | 63 | 62 | 1 | 1 | 20 | 41 |

The severity columns count true findings. Linux is 23 of the PRs, from 10 of 12 mailing lists, and Kubernetes 10. By language: C 56, Rust 34, Go 34, C++ 24, Java 24, JavaScript 19, Python 18, TypeScript 6, and LLVM's C++ 6.

### What maintainers actually comment on

**This corpus is mostly not about bugs.** Of its 826 true findings, 86% are low severity and only 8 are high. 64% are maintainability or documentation, 11% correctness, and only 4 are security. ReviewBench's test set, for comparison, is 52% low and 38% correctness.

That is real and not a labeling artifact: reading the labels against the comments found these really are renames, redundant asserts, missing comments and style. Maintainers catching a bug in review is rarer than it seems, and much of what they write is about keeping a codebase in shape. As far as I recall, studies of code review have found the same.

The consequence is that **chasing overall recall here would teach the agent to nitpick**, the opposite of being more precise. The value is in the smaller part:

- 119 findings rated medium or high, such as a cache key collision that serves one user's page to another, or swapped arguments that leave a signing key uninitialized.
- 121 in correctness, reliability or security.

Read `by_severity` and `by_category` in a run's results rather than the headline. Nothing scores those subsets on their own yet.

### How far to trust the labels

A person should still check any of this that matters. What has been checked:

- **Reading them.** 64 labels from `maint_train` read against the raw comments by Claude Opus 5.5, which is one reader and not ground truth: 34 chosen at random, which turned out to be all true, plus all 4 high, 14 false and 12 not-a-finding. It disagreed with 3 cosmetic comments labeled true and 1 shaky label of false, and 2 severities looked one level low. It saw no real bug rated low or labeled false.
- **{{FIDELITY}}**
- **Model text.** That reading found comments starting `[Agent]`, an AI's output posted under a person's account. There were 8 in the corpus, all on one project, and they are now filtered. Such output that does not announce itself cannot be detected, so some may remain.

### A first run, end to end

Haiku 5.5 through Claude Code, judged by Sonnet 5.5, found 6 of 8 true findings on two small `maint_train` PRs, one from Linux and one from Tokio, with a clean audit of its tool calls. It missed the one high finding, a missing unregister that leads to use-after-free. Two PRs show that the chain works from checkout to score, and say nothing about quality.

## How it is built

```sh
python3 bench/maintainers.py survey     # measure candidates, writes .cache/maintainer/survey.json
python3 bench/maintainers.py assign     # give selected projects a split, once
python3 bench/maintainers.py collect    # download PRs and review threads
python3 bench/maintainers.py build      # choose snapshots, align comments
python3 bench/lore.py build             # the same for the Linux kernel
python3 bench/maintainers.py label      # comments to golden findings, through Claude Code
python3 bench/maintainers.py splits     # write bench/maintainer_splits.json
python3 bench/maintainers.py stats
```

Everything stays in `.cache/maintainer/`, which is gitignored. `bench/maintainer_projects.json` and `bench/maintainer_splits.json` hold only names and identifiers, as `bench/splits.json` does. Sampling windows count back from a fixed date, and PRs are taken in an order fixed by hash, so running this again selects the same PRs unless history on GitHub changed.

### Which comments count

The first comment of a review thread, on a specific line, from someone other than the PR's author, who is an established human reviewer of that project: push access, member or collaborator, or comments on at least three other people's PRs in what was collected. Replies become evidence for the label instead of findings of their own, as in ReviewBench's extraction.

Accounts that look like automation or AI reviewers are excluded by name, and so are comments with no claim in them, such as approvals. A person posting a model's output under their own name cannot be detected in general, though on the kernel's lists such mail usually says what it is and gets dropped.

A PR qualifies with two substantive comments, at least one of which led to a code change. It must be a human change that landed, of 10 to 1,500 lines in up to 40 files, and not a backport, revert or dependency bump.

### What the reviewer sees

This is where a mistake would quietly make every score meaningless, so it gets the most care.

- **The head is the commit reviewers commented on most, never the PR's last commit.** The last commit already contains the fixes. If GitHub has discarded that commit, which it does some time after a force push replaces one, the next most commented commit that survives is used.
- **A PR's own fix is never fetched.** Each PR fetches two commits at depth 1, its merge base and its head. What happened later is read through GitHub's API into the record the labeler uses. A test checks that a later commit is absent from the checkout and its objects.
- **Other PRs' snapshots do share the object store,** so a later PR from the same project can hold the merged result of an earlier one. A checkout has no ref to them and does not list them, and a reviewer with no shell cannot read git objects, so it cannot reach them. Anything given a shell and a commit hash could, the same as with ReviewBench's cached mirrors.
- **The title and description are the ones from review time.** Authors edit both during review, and an edit can state the outcome. GitHub keeps the history, so the corpus uses what they said just before the first kept comment. ReviewBench's corpus uses the final text. A PR whose history cannot answer this is left out.
- **A comment must describe the snapshot.** A comment on the head is kept where it is. A comment from a later commit is kept only if its lines are at the head, nearby. A comment from before the head's first review is dropped: the author may have fixed it by then, somewhere other than the lines it was on, and that finding would be impossible to find.
- **Checkouts are built as for ReviewBench:** no remote, and only refs for base and head.

### Linux

The kernel is not reviewed on GitHub, so `bench/lore.py` reads lore.kernel.org and makes each patch series look like a PR. The base is the series' `base-commit:` trailer, which must also exist in the GitHub mirror. Series without one are skipped, since guessing a base gives a reviewer the wrong code with no error. The head does not exist upstream, so it is built by applying the series with fixed committer details, and the series is kept so the same commit can be rebuilt.

A finding is a reply from someone listed in `MAINTAINERS`, written directly under a quote of the patch, and a series needs the same two substantive comments as a pull request. A quote that matches several places, with neither its file header nor more of its lines to settle which, is dropped, since a finding in the wrong file can never match. The outcome comes from the next version of the series: if the code the reviewer quoted is gone from it, the comment led to a change. A quote ending on an unchanged line says nothing either way, because a diff only shows unchanged lines near a change. Each series counts once, whichever versions were posted.

On a case-insensitive disk, a few kernel files overwrite each other in a checkout. PRs that touch them are excluded, since a reviewer would read the wrong file.

### From comment to finding

`maintainers.py label` runs one call per comment, through Claude Code, with Claude Sonnet 5.5, no tools, and an empty working directory outside this repository, so the labeler gets no project instructions. Its system prompt is ReviewBench's published classifier prompt, read from the pinned clone, plus a short addendum.

Maintainers write to a colleague, so a comment like "does this need the lock?" is hard for a matcher to compare with anything. So the labeler does what ReviewBench's extraction prescribes for inferred findings, and rewrites each comment to stand alone. The original is kept in `source.raw_comment`. It also decides that some comments are not findings at all, and those are dropped. A PR left with no finding labeled true is dropped too, since recall is undefined on it.

The labeler sees the diff hunk, the file around the comment, the replies and what happened afterwards, but it does not read the repository. ReviewBench's classifier has tools, and this one is much cheaper at the cost of some verification depth. The evidence of what the author did next is what makes up for it, since ReviewBench's classifier does not have that. That evidence is a patch only where the head is an ancestor of the final commit. After a rebase the patch would start at an older merge base and show the PR's own changes as if they came later, so the labeler gets only GitHub's flag that the lines changed.

`maintainers.py fidelity` checks the rewrites: ReviewBench's own matcher compares each rewrite with its original comment, and reports how often they describe the same issue.

### Splits

`maintainers.py assign` places projects in `train`, `val` and `holdout`, stratified by domain, and stores the decision in `bench/maintainer_projects.json`. A project keeps its split forever, so adding one cannot move a held-out project into training. Splits were fixed before any comment text was read. Linux, Kubernetes and React are forced into `train`, since a project outside training cannot teach the improver anything.

A project cannot share a repository or organization with a ReviewBench PR. `assign` refuses that, which is why `dotnet/*` is out.

## Using it

```sh
python3 bench/maintainers.py stats                                 # what is here
python3 bench/maintainers.py sample --n 20                         # read some labeled findings, train only
python3 bench/preflight.py --host claude                           # is the reviewer isolated?
python3 bench/run.py --select maint_val_quick --name maint-haiku --host claude --model claude-haiku-5-5
python3 loop/run_loop.py iterate --train train_quick,maint_train_quick --val val_quick,maint_val_quick
python3 loop/run_loop.py holdout --select maint_holdout            # rarely
```

Checkouts appear on first use, and Linux's take 1.7 GB each. Selectors are `maint_train`, `maint_val`, `maint_holdout`, `maint_train_quick`, `maint_val_quick` and `maint_all`. `all` still means ReviewBench's 219 PRs, and the loop's defaults do not use this corpus.

## Reading the numbers

**These are never leaderboard scores.** It is a different corpus, labeled by a different model from ReviewBench's. Say which corpus, reviewer and judge produced a number.

**Do not gate on this corpus alone.** Grounded metrics ignore unmatched findings, and human comments rarely get labeled false, so grounded precision here stays near 1 whatever the agent does. The objective then only measures recall, and reporting more always looks better. So `run_loop.py iterate` refuses a validation set made only of these PRs, and rejects a change if no ReviewBench PR ended up scored. On a mixed set, `gate.decide` rejects a change that takes comment volume on these PRs over 25% above the first version measured on them, or loses more than 0.003 on ReviewBench's PRs, whatever it gains here. The loop also refuses training PRs that overlap validation or holdout, since the improver reads what it trains on.

**Recall here has a low ceiling, and that is fine.** Maintainers do not write down everything they notice, and they skip what CI catches. A finding the agent reports that no maintainer mentioned may well be right, and grounded metrics simply do not see it. Compare versions of the agent with each other rather than reading the number as a share of all real problems.

**High severity is rare.** With 8 high findings in the corpus, the objective's 60% weight on high severity has almost nothing to work with here. See [what maintainers actually comment on](#what-maintainers-actually-comment-on).

**Repositories this large reward different skills.** A reviewer here has far more code to get lost in than on a typical ReviewBench PR, so time and token budgets behave differently too.

## Lessons from building it

Each of these produced no error, and a plausible result:

- **Sampling.** The survey went through three sampling methods before its numbers made sense, and each wrong one showed respected projects as never reviewing. [The research](research/maintainer-projects.md#how-each-project-was-measured) has the details.
- **Renamed repositories.** GitHub's search does not follow them, so a project that moved looks like it has no pull requests.
- **Force pushes.** GraphQL reports no original commit for a comment once a force push has replaced it. Projects that rewrite branches during review looked unusable until REST filled the gap.
- **An unreachable service looked like an empty one.** Python from python.org could not verify certificates, so every request to lore failed, and the adapter would have reported that twelve mailing lists had no reviewed patches. Failing to reach a service now raises.
- **Fetching without refs.** With no refs in the cache, git has nothing to tell the server it already holds, and each shallow fetch downloads a whole snapshot again.
- **Reshuffled splits.** `make_splits` shuffles over every repository in its input, so adding PRs to the shared manifest would have silently moved PRs from holdout into training. It now reads ReviewBench's manifest alone, and a test covers this.
- **Findings already fixed.** A comment from an earlier commit whose lines were untouched looked valid, even when the author had fixed the problem elsewhere by the snapshot. An independent review of this code caught that, along with evidence that misreported what happened after a rebase, a quote matched to the wrong file, a run compared across two versions of the labels, and more. Each now has a test in `tests/test_maintainers.py`.
