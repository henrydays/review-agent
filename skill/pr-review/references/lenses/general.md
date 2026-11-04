No single focus in this pass. Read the change the way the person who will be paged for it would.

Ask of each hunk, in this order:

- Does it run? Syntax, imports, names, types, arguments, build wiring.
- Does it do what the author says it does, on all paths and not just the obvious one?
- What did the old code guarantee, and does anything still rely on that?
- What input breaks it: empty, missing, zero, duplicate, very large, concurrent, repeated?
- If it fails, what happens? Is the failure visible, or does something report success?
- Is anything now inconsistent: a sibling function, a config value, a test, the docs, a comment?
- Can a user actually reach and use what was added?

Report everything you find, from crashes to misleading comments.
