# Steering doc for clarification on which commands agent should be using

- To run tests use `uv run python -m pytest ...`.
- To run the project use `uv run pyqenc` with required arguments.
- Don't use pipes when running pipeline - this ruins alive_progress bar display for the end-user.
- Use `steering/environment.md` for local environment details, like workdir, sample target, etc.
