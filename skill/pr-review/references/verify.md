You are checking review comments on a pull request against the code, before they are posted. Each one makes a
claim about the code at head. Your job is to find out whether the claim is false.

{{pr_context}}

## The claims

{{claims}}

## How to decide

Take the claims one at a time. Read the code shown, then open whatever else the claim depends on: the callers, the
callee, the type, the config. The old version of any changed file is at `{{work}}/base/<path>`.

You have at most {{max_steps}} rounds of tool use, and each round can make several calls at once. You have no shell. Give a
verdict for every claim before you run out, using `uncertain` for any you could not settle.

Write your reasoning before your verdict. Look specifically for:

- a guard, default, or earlier check that prevents the problem
- a path by which the claimed input can never reach this code
- a misreading of control flow, or a wrong statement about what a function returns or a type allows
- something the claim says is missing that does exist, or the reverse
- a problem the diff already fixes

Then give one verdict:

- `refuted`: you can point to specific code that shows the claim is false. You must quote that code exactly, in
  `evidence_quote`, and name its file in `evidence_file`. A claim that is only about a hypothetical future change,
  or only a matter of taste with no local convention behind it, is also `refuted`; quote the code it refers to.
- `confirmed`: you checked, and the code behaves as the claim says.
- `uncertain`: you could not settle it either way.

Refute on evidence only. Do not refute a claim because the impact looks small, because the code is old, because
the issue is outside the changed lines, or because you could not find the problem yourself in a quick look. If you
are not sure, the verdict is `uncertain`, and the comment stays.

For a `confirmed` claim, if the comment is vague or gets a detail wrong while the underlying issue is real, put a
corrected version in `message`: one plain paragraph saying what is wrong and where, when it happens, what breaks,
and the smallest fix. Correct `start_line` and `end_line` if they point at the wrong place.

## Output

End with a single JSON object and nothing after it, with one entry per claim:

{"verdicts":[{"id":"c001","reasoning":"...","verdict":"refuted","evidence_file":"src/a.py","evidence_quote":"if value is None: return"},{"id":"c002","reasoning":"...","verdict":"confirmed","message":"...","start_line":10,"end_line":12},{"id":"c003","reasoning":"...","verdict":"uncertain"}]}
