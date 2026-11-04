Review the files that decide how the code gets built, tested, shipped and configured. These fail silently and only in
production, and they get the least attention in review.

Check that each file does what it appears to do, by reading what it refers to:

- CI workflows: valid syntax, including the shell inside `run:` blocks. Conditions that can never be true or are always
  true. Jobs depending on outputs that are not set. Secrets and variables that are empty on some triggers. A test matrix that
  skips a version the project supports. A value baked in at build time from a variable that may be unset.
- Containers and deployment: `COPY` sources that exist, build context, ports that agree between app, Dockerfile, compose
  and manifests. Kubernetes selectors matching labels. Helm values referenced by templates and defined in `values.yaml`, and
  the reverse. Environment variables the app reads but the manifest never sets. Health checks pointed at real endpoints.
- Infrastructure: a resource removed along with others by accident, and what happens on the next apply. Hardcoded accounts,
  regions or names where a variable is used elsewhere.
- Dependencies and packaging: an import with no declared dependency, a test tool missing from dev dependencies, a range
  admitting a breaking version, a manifest edited without its lockfile, files needed at runtime excluded from the package.
- Scripts: paths that do not exist, commands that fail on a clean checkout, unquoted variables, `set -e` interplay, a command
  in `package.json` or a Makefile pointing at something removed.
- Conflicting defaults between two config files or between config and code.

Open the file each setting refers to before concluding it is wrong.
