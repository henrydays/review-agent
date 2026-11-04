Compare what the text says with what the code does. Open the code; do not judge docs by how they read.

For each changed README, doc page, docstring, comment, help text, changelog entry and example:

- Check every concrete detail against the code: flag names, defaults, environment variables, paths, function names, return
  types, status codes, limits, supported values.
- Run the example in your head. Would it work as written? A missing `await`, a wrong import, an outdated argument.
- A table or list of features that claims more or less than is implemented.
- A comment that contradicts the line below it, or describes what the code used to do.

Then the reverse: for each behavior this diff changes, search the repository's docs and comments for the old name or the
old behavior. Stale documentation elsewhere in the repository counts, when this change made it stale.

Also report:

- A typo in user-visible text, an identifier or a config key, where it could mislead or break something.
- A version number or changelog that disagrees with the rest of the change.
- Public API or a configuration option added with no documentation where the repository documents its siblings.

Do not report that documentation could be longer in general, or that an already clear function lacks a docstring.
