Hold the change up against its own description, and against what it removed.

First, list what the title and description promise. For each promise, find the code that delivers it. Report:

- a promise with no code behind it, or one delivered on only some paths or platforms
- a fix that addresses the symptom named in the description but leaves the same bug on a sibling path
- behavior the code has that contradicts what the description or the docs in this PR say
- a feature that is only partly built: it has a backend and no way to reach it, or it writes data nothing reads

Then read what was removed, opening the old versions of the changed files. For each removal ask what that code was
doing, and whether the new code still does it. Report:

- a capability, validation, fallback or error path that disappeared with no replacement
- lines removed by accident along with the ones the PR meant to remove, such as an unrelated variable taken out of a
  block during a cleanup
- a resource or config removed from a file where another system still expects it

Also report unrelated changes that look unintentional, and anything that breaks a stated rule of this repository, citing
the rule.
