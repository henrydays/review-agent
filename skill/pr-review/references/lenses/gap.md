Several reviewers have already been through this pull request, and none of them commented on the hunks below. That may mean
they are fine. It may also mean attention ran out, or that each reviewer assumed this was someone else's area. Look at them
fresh.

Take each hunk and answer, with the code open:

- What is this hunk for? Why did the author need to change it?
- What does it assume about its inputs, its callers and the code around it? Is each assumption true? Check.
- Compare it with the old version and with its nearest sibling. What differs that should not?
- If this hunk were wrong, which test or behavior would reveal it?

Unremarkable-looking changes are the point of this pass: a config value, a one-line condition, a default, a renamed key, a
moved block, a comment, a test fixture. Report what you find. If a hunk is really fine, say nothing about it, and do not invent
a problem to fill the space.
