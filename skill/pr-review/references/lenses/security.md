Report a security problem only when you can show the path: where the untrusted value enters, how it travels, and the
line where it does harm, or the check that is missing and what an attacker gets by its absence. Follow the data through
the code and name the identifiers.

Look for:

- Injection: user input reaching SQL, a shell, a template, `innerHTML`, a file path, a URL fetched by the server, a
  deserializer, a regular expression.
- Missing authorization: a new endpoint without the check its siblings have, an object looked up by ID without an ownership
  check, an action available to the wrong role, an existence check that leaks what the read path hides.
- Authentication flows: tokens not validated, sessions not invalidated, a credential endpoint without the rate limit its
  siblings have, a development login reachable in production.
- Secrets and data: a real credential or private identifier committed, secrets or personal data written to logs, sensitive
  files included in a build or published package.
- CI: untrusted pull request content reaching a privileged workflow, with the specific trigger and the step it reaches.

Do not report the generic form of any of these. "Pin this action to a SHA", "sanitize this input", and "this could allow
injection" with no path from an attacker's input are not findings. Automated tools produce these in bulk and nearly all are
noise.
