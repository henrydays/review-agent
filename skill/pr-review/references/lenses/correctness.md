Find logic that does the wrong thing. Trace each changed branch with concrete values instead of reading it for
plausibility: an empty collection, zero, null or missing, a negative number, the first and last element, a
duplicate, a value at a boundary.

Look for:

- Wrong variable, inverted condition, `and` where `or` is meant, off-by-one, wrong comparison, wrong units.
- `break` where `continue` is meant, an early return that skips cleanup or later items, a branch that can never run.
- A missing `await`, a promise or future used as its value, an iterator consumed twice.
- API misuse: a property the type does not have (`.size` on an array), a method that returns a copy being treated
  as if it mutates, an argument order that compiles but means something else, a flag the callee requires to return
  the format the caller parses.
- State that goes inconsistent: a flag set and never cleared, an update that skips one of two stores, an event that
  leaves status out of step with data, work done before the validation that should have prevented it.
- Data quietly lost or changed: a mapping that drops fields, a default that overwrites a real value, rounding, time
  zones, encoding, integer overflow, sort order, a float compared for equality.
- A copied block where one branch was updated and its sibling was not.
- UI state: hooks called conditionally, stale closures, a control removed so that an action can no longer be
  performed at all, an empty list that hides the only way to add the first item.

Check each claim against the code. The test for this pass: if you can name the input and the wrong output, it is a
finding.
