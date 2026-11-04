Most serious defects are a changed line that no longer agrees with code that did not change. Compare the two sides of
each boundary this diff touches. Search for every consumer; do not assume.

For each changed function, type, config key, route, event, schema or file format:

- Find the callers and readers. Do they pass the new arguments and handle the new return shape, new error, new
  `None`, new async behavior?
- For each new call, read the callee. Does it actually accept these arguments, return this shape, and raise what the
  caller expects? Does it need a flag or option the caller did not pass?

Names that have to agree on both sides, and often do not:

- environment variables and config keys between code, deployment manifests, CI and docs
- CLI flags, route paths, queue and topic names, database columns, JSON fields between writer and reader, translation keys
- file paths and output locations between the step that writes and the step that reads

Also check:

- Wiring: is the new code reachable? Registered, exported, imported, added to the build, passed through every layer
  (a new setting set on the API service but not on the web service)?
- Renames and removals: stale references to the old name, in code, tests, docs and config.
- Public API breaks: a required member added to a public interface, a removed alias or parameter, a changed default, a
  renamed attribute argument, a return type widened, a behavior change existing callers cannot detect.
- Data already stored: can existing records and existing clients still be read by the new code?
