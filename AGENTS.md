## Project Workflow

- Treat the `README.md` "Getting started" and "Run the verification suite"
  sections as the canonical instructions for running and testing this project.
- Before running project tests, activate the Conda environment with
  `conda activate general`.
- Use `./run_all_tests.sh` as the completion gate for coding changes. Targeted
  tests are fine while iterating, but they do not replace the README's full
  verification suite.

## Development Server

- Run the Flask development server on port **5007**. Always pass
  `--port 5007` when starting it, and use `http://localhost:5007` for browser
  or API checks.
- Agents may add `--skip-startup-tests` for iteration only after the relevant
  tests have already been run; the final verification still uses
  `./run_all_tests.sh`.

## Bug Tracking

Bug findings are documented in `docs/bugs/` with the following naming convention:

```
bug-XXX-<short-description>.md
```

Where `XXX` is a sequential number (e.g., `bug-001`, `bug-002`). Each bug doc should include:
- Status and test reference
- Symptom description
- Root cause analysis
- Relevant code locations
- Suggested fix
