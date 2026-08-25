## How to communicate

Say what a thing does in plain words. Do not invent a short label for an idea and then spend a paragraph explaining what the label means — if a term needs a paragraph to decode, the term is wrong, so write the plain description instead. This applies to every message, doc, code comment, and commit message.

- **No buzzwords, no coined jargon.** Words like "preamble," "fuse," "integrate all trains," "barrier" hide meaning behind a label. Replace each with what actually happens: "the first step of the tick," "compare the calculated position against the detector reading and make them agree," "calculate every train's new position before any train plans."
- **The explanation is the writing, not a footnote to it.** If you write a term and then a sentence that translates it, delete the term and keep the translation.
- **Real identifiers are allowed, defined once, plainly.** A word that names actual code — a field, method, class, or established project concept — may be used, but give it a one-line plain-English meaning the first time it appears. That is the only kind of "term" permitted, and only because it points at something concrete in the code.
- **Short beats clever.** Prefer the sentence a junior engineer understands on first read.

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
