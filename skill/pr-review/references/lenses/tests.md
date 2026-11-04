Judge whether the tests in this change would fail if the code broke, and whether the behavior this change adds is tested
at all.

In test code, report:

- A test that exercises only its mocks and never calls the code under test. An assertion that can never fail, or only
  checks that something is defined.
- A wrong expected value, or a test whose name says one thing and whose body checks another.
- A mock patched where it is defined instead of where it is used, so the real code still runs. A fixture whose shape has
  drifted from what production uses.
- Fragile tests: dependence on order, wall-clock time, sleeps, network, exact floating-point values, deep DOM structure,
  shared state left behind by another test. `.only` or skip left in.
- Missing test dependencies, so the suite cannot run on a clean install. Tests not picked up by the runner configuration.

For the code in this diff, report specific missing tests:

- Name the new branch, error path or edge case that nothing exercises, and what regression would go unnoticed. Read the
  existing tests first, so that you do not claim something is untested when it is covered.
- A bug fix with no test that fails without the fix.

"This needs more tests" without naming the behavior is not a finding.
