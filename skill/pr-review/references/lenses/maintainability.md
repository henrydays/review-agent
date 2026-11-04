Find what will cost the next person, with evidence from this repository. Every finding here should cite the existing code
that makes it a problem.

- Inconsistency with an established local convention: naming, casing, file layout, error handling, or a helper everyone
  else uses. Name the sibling file or function that shows the convention.
- Duplicated domain logic that can drift apart: a mapping, a validation rule, a list of cases, hard-coded in a second place
  when one already exists. Name both places, and say what goes wrong when one changes.
- A hard-coded value where the repository already has a constant, a setting or a variable for it.
- Dead code added by this change: a parameter never read, an import never used, a branch that cannot be reached, a function
  nothing calls, commented-out code.
- A misleading name: a function that does more than its name says, a variable whose name says the opposite of its value, a
  public identifier with a typo.
- Something present on all sibling artifacts and missing from the new one: a file the other modules all have, a registration
  step, a required field.
- A workaround where the framework offers a direct way, when you can name it and it preserves behavior.
- Code in the wrong layer, or an abstraction with one caller that makes the change harder to follow, when you can say what
  the simpler form would be.

Do not report taste. "I would name this differently" with no convention behind it, formatting, and small refactors that change
nothing for anyone are not findings.
