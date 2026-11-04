## Writing a finding

Report one issue per finding. Write the `message` as one plain paragraph of 30 to 90 words, in this order:

1. What is wrong, naming the function, variable, key or file involved and what it actually does.
2. The input or situation that triggers it.
3. The concrete result: what breaks, for whom, or what the reader is misled about.
4. The smallest fix.

Be specific enough that someone could check the claim from the message alone. Use no title, heading, bullet
list or severity tag in the message.

When the same defect lives in several files, or the cause is in one file and the failure shows up in another, name those
files in the message and list the locations in `also_in`, at most three. Leave `also_in` out when a file is merely related.

- `start_line` and `end_line` are line numbers of the file at head, spanning the code in question.
- `severity`: `high` must be fixed before merge (breaks a main workflow, loses or corrupts data, an exploitable
  hole). `medium` should be fixed. `low` is minor but worth saying.
- `category`: one of `correctness`, `reliability`, `security`, `performance`, `api`, `testing`, `docs`,
  `maintainability`, `accessibility`.
- `confidence`: from 0 to 1, how sure you are that this is real. Report low-confidence findings too.

## Output

End with a single JSON object and nothing after it. If you truly found nothing, `findings` is an empty list.

{"findings":[{"file":"path/from/repo/root.py","start_line":12,"end_line":15,"severity":"medium","category":"correctness","confidence":0.8,"message":"...","also_in":[{"file":"other/path.py","start_line":3,"end_line":3}]}]}
