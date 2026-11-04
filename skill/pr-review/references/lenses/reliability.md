Ask what happens when things go wrong, or happen at the same time, or happen a lot.

Failure:

- An exception swallowed or logged and then reported as success. A partial failure that leaves half the work applied.
- An error turned into a default value, so that a wrong answer looks like a right one.
- A missing timeout on a call that can hang, or a timeout too short for the job. A retry that is not idempotent and
  repeats a side effect.
- A fallback or default that silently selects the wrong environment, endpoint or mode when a setting is absent.
- Something that fails in a clean checkout or on first run: a directory that may not exist, a generated file that is
  ignored by git, a step depending on the order things happen to run in.

Concurrency and lifecycle:

- Shared state read or written without the protection used elsewhere. A check-then-act race. Work started on one thread
  touching data owned by another.
- Cancellation or shutdown that loses work or never finishes. Listeners, timers, subscriptions, handles and connections
  that are never released, especially on the error path.
- A cleanup that does not run on the path it is needed for.

Performance, when you can name the cost:

- Quadratic work over a collection that can realistically be large, a query in a loop, an expensive call repeated per
  item or per request, a whole file read to get one value.
- Unbounded growth of a queue, cache or list. Expensive validation run before a cheap check that could reject first.

State the realistic size or frequency. "This might be slow" with no specifics is not a finding.
